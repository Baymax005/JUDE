from __future__ import annotations

import os
import shutil
import subprocess
import time
from pathlib import Path

import requests

from app.config import OLLAMA_BASE_URL, OLLAMA_KEEP_ALIVE, OLLAMA_MODEL, OLLAMA_MODELS_DIR, OLLAMA_NUM_CTX

JUDE_SYSTEM_PROMPT = (
    "You are J.U.D.E., a local-first Windows 11 personal desktop AI agent "
    "with VMware Linux guest control. Always identify yourself strictly as J.U.D.E. "
    "(never say Qwen or Alibaba Cloud). "
    "STRICT GROUNDING RULE: NEVER claim you opened, closed, typed, edited, sent a message, "
    "or saved anything unless you called a tool in THIS turn and got ok=true. "
    "If no tool exists for an action (such as sending a WhatsApp message), honestly say so. "
    "Keep answers concise (1-2 natural sentences) without repeating the user's name on every reply."
)


class OllamaClient:
    """HTTP client for local Ollama supporting chat, native tool-calling, and num_ctx=4096."""

    def __init__(
        self,
        base_url: str = OLLAMA_BASE_URL,
        model: str = OLLAMA_MODEL,
        keep_alive: str = OLLAMA_KEEP_ALIVE,
        num_ctx: int = OLLAMA_NUM_CTX,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.keep_alive = keep_alive
        self.num_ctx = int(num_ctx)
        self._history: list[dict[str, str]] = []

    def clear_history(self) -> None:
        """Clear conversational turn history."""
        self._history.clear()

    def record_turn(self, user_text: str, assistant_text: str) -> None:
        """Store a concise conversation turn in rolling history (capped at 8 turns)."""
        u = " ".join((user_text or "").split())[:500]
        a = " ".join((assistant_text or "").split())[:900]
        if not u or not a:
            return
        self._history.append({"role": "user", "content": u})
        self._history.append({"role": "assistant", "content": a})
        if len(self._history) > 16:
            self._history = self._history[-16:]

    def get_history(self) -> list[dict[str, str]]:
        return list(self._history[-12:])

    def is_reachable(self, timeout: float = 2.0) -> bool:
        try:
            resp = requests.get(f"{self.base_url}/api/tags", timeout=timeout)
            return resp.status_code == 200
        except requests.RequestException:
            return False

    def ensure_server_running(self, wait_seconds: float = 25.0) -> bool:
        """Return True if local Ollama is reachable, attempting a hidden local start if needed."""
        if self.is_reachable(timeout=1.5):
            return True

        ollama_exe = shutil.which("ollama")
        if not ollama_exe:
            candidate = Path(os.environ.get("LOCALAPPDATA", "")) / "Programs" / "Ollama" / "ollama.exe"
            if candidate.exists():
                ollama_exe = str(candidate)

        if not ollama_exe:
            return False

        env = os.environ.copy()
        env["OLLAMA_MODELS"] = str(OLLAMA_MODELS_DIR)
        creation_flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        try:
            subprocess.Popen(
                [ollama_exe, "serve"],
                env=env,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                creationflags=creation_flags,
            )
        except OSError:
            return False

        deadline = time.monotonic() + wait_seconds
        while time.monotonic() < deadline:
            if self.is_reachable(timeout=1.0):
                return True
            time.sleep(0.5)
        return False

    def chat(
        self,
        prompt: str,
        system: str | None = None,
        temperature: float = 0.25,
        max_tokens: int = 600,
        use_history: bool = True,
    ) -> str:
        """Send a chat request to local Ollama and return the reply text."""
        if not self.ensure_server_running():
            return (
                "Local Tier 2 engine (Ollama) is not reachable on localhost:11434. "
                "Start Ollama and try again."
            )

        messages: list[dict[str, str]] = [{"role": "system", "content": system or JUDE_SYSTEM_PROMPT}]
        if use_history and self._history:
            messages.extend(self._history[-10:])
        messages.append({"role": "user", "content": prompt})

        try:
            resp = requests.post(
                f"{self.base_url}/api/chat",
                json={
                    "model": self.model,
                    "stream": False,
                    "keep_alive": self.keep_alive,
                    "options": {
                        "temperature": temperature,
                        "num_predict": max_tokens,
                        "num_ctx": self.num_ctx,
                    },
                    "messages": messages,
                },
                timeout=90,
            )
            resp.raise_for_status()
            content = resp.json().get("message", {}).get("content", "").strip()
            if content and use_history:
                self.record_turn(prompt, content)
            return content or "Tier 2 returned an empty response."
        except requests.RequestException as exc:
            return f"Local Tier 2 request failed: {exc}"

    def chat_with_tools(
        self,
        messages: list[dict],
        tools: list[dict],
        temperature: float = 0.2,
        max_tokens: int = 600,
    ) -> dict:
        """Send a native Ollama /api/chat tool-calling turn and return {'ok', 'message', 'error'}."""
        if not self.ensure_server_running():
            return {
                "ok": False,
                "message": {},
                "error": "Local Tier 2 engine (Ollama) is not reachable on localhost:11434.",
            }

        try:
            resp = requests.post(
                f"{self.base_url}/api/chat",
                json={
                    "model": self.model,
                    "stream": False,
                    "keep_alive": self.keep_alive,
                    "tools": tools,
                    "options": {
                        "temperature": temperature,
                        "num_predict": max_tokens,
                        "num_ctx": self.num_ctx,
                    },
                    "messages": messages,
                },
                timeout=90,
            )
            resp.raise_for_status()
            data = resp.json()
            return {"ok": True, "message": data.get("message") or {}, "error": ""}
        except requests.RequestException as exc:
            return {"ok": False, "message": {}, "error": f"Local Tier 2 request failed: {exc}"}
