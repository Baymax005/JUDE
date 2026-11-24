from __future__ import annotations

import os
import shutil
import subprocess
import time
from pathlib import Path

import requests

from app.config import OLLAMA_BASE_URL, OLLAMA_KEEP_ALIVE, OLLAMA_MODEL, OLLAMA_MODELS_DIR


class OllamaClient:
    """Lightweight HTTP client for local Ollama with lazy auto-start."""

    def __init__(
        self,
        base_url: str = OLLAMA_BASE_URL,
        model: str = OLLAMA_MODEL,
        keep_alive: str = OLLAMA_KEEP_ALIVE,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.keep_alive = keep_alive

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
        temperature: float = 0.3,
        max_tokens: int = 450,
    ) -> str:
        """Send a single-turn chat request to local Ollama and return the reply text."""
        if not self.ensure_server_running():
            return (
                "Local Tier 2 engine (Ollama) is not reachable on localhost:11434. "
                "Start Ollama and try again."
            )

        messages = []
        if system:
            messages.append({"role": "system", "content": system})
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
                    },
                    "messages": messages,
                },
                timeout=90,
            )
            resp.raise_for_status()
            content = resp.json().get("message", {}).get("content", "").strip()
            return content or "Tier 2 returned an empty response."
        except requests.RequestException as exc:
            return f"Local Tier 2 request failed: {exc}"
