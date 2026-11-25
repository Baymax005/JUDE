from __future__ import annotations

import json
import re
import threading
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from app.config import (
    BACKUPS_DIR,
    DATA_DIR,
    MEMORY_DB_PATH,
    TIER1_CONFIDENCE_THRESHOLD,
    TIER1_CONFIRM_THRESHOLD,
    TIER1_MODEL_DIR,
)
from app.router.intent_router import IntentRouter, RouteResult
from app.tier1.actions import (
    execute_close_app,
    execute_compression,
    get_system_info,
    open_target,
    plan_close_app,
    plan_compression,
)
from app.tier1.memory import (
    delete_memory_by_query,
    infer_word_style_preference,
    log_activity,
    recall_memory,
    save_memory,
)
from app.tier2.ollama_client import OllamaClient
from app.tier2.screen_reader import read_highlighted_text
from app.tier2.word_editor import apply_word_edit, execute_word_create, prepare_word_edit
from app.tools.registry import run_agent_turn
from app.tools.vmware_linux_tool import execute_vmware_ssh_command


@dataclass
class PendingConfirmation:
    """Action awaiting explicit user confirmation before executing."""

    id: str
    kind: str  # "compress", "word_edit", "word_create", "close_app", or "vmware_cmd"
    title: str
    summary: str
    intent: str
    confidence: float
    target_path: str = ""
    backup_note: str = ""
    before_text: str = ""
    after_text: str = ""
    payload: dict = field(default_factory=dict)


@dataclass
class EngineResponse:
    """Unified output returned by JudeEngine to the UI or CLI."""

    reply: str
    spoken_summary: str
    intent: str
    tier_used: str  # "tier1", "tier2", or "pending"
    confidence: float
    gate_action: str
    router_ms: float
    total_ms: float
    badge_text: str
    sources: list[tuple[str, str]] = field(default_factory=list)
    pending: PendingConfirmation | None = None


class _FallbackRouter:
    """Lightweight keyword fallback if the ONNX router bundle has not been installed yet."""

    active_provider = "RuleFallback"

    def predict(self, text: str, top_k: int = 3) -> RouteResult:
        t0 = time.perf_counter()
        low = text.lower()
        if any(k in low for k in ("zip", "compress", "archive", "bundle")):
            intent, tier = "COMPRESS_FILES", "tier1"
        elif any(k in low for k in ("remember", "save ", "note down", "store ")):
            intent, tier = "SAVE_MEMORY", "tier1"
        elif any(k in low for k in ("recall", "remind me", "what did i save", "look up my")):
            intent, tier = "RECALL_MEMORY", "tier1"
        elif any(k in low for k in ("open ", "launch ", "start ")):
            intent, tier = "OPEN_TARGET", "tier1"
        elif any(k in low for k in ("battery", "free space", "ram", "cpu", "what time", "system status")):
            intent, tier = "SYSTEM_INFO", "tier1"
        elif "word" in low and any(k in low for k in ("rewrite", "polish", "shorten", "fix", "edit")):
            intent, tier = "EDIT_DOCUMENT", "tier2"
        elif any(k in low for k in ("on my screen", "read my screen", "traceback", "monitor")):
            intent, tier = "EXPLAIN_SCREEN", "tier2"
        elif any(k in low for k in ("search the web", "search online", "duckduckgo", "weather")):
            intent, tier = "WEB_SEARCH", "tier2"
        else:
            intent, tier = "GENERAL_CHAT", "tier2"

        latency_ms = (time.perf_counter() - t0) * 1000.0
        return RouteResult(
            text=text,
            intent=intent,
            tier=tier,
            confidence=0.90,
            gate_action="direct",
            effective_tier=tier,
            latency_ms=latency_ms,
            provider=self.active_provider,
            top_candidates=[(intent, 0.90)],
        )


_FENCED_CODE_RE = re.compile(r"```.*?```", flags=re.DOTALL)


def _spoken_excerpt(text: str, max_chars: int = 280) -> str:
    """Keep up to 2 clean, conversational sentences for TTS (no code blocks or raw symbols)."""
    body = text.split("\n\nSources:", 1)[0].strip()
    body = _FENCED_CODE_RE.sub(" I placed the code in the chat window. ", body)
    body = re.sub(r"[`*#_>]+", "", body)
    cleaned = " ".join(body.split())
    if len(cleaned) <= max_chars:
        return cleaned
    sentences = re.split(r"(?<=[.!?])\s+", cleaned)
    picked: list[str] = []
    total = 0
    for s in sentences:
        if total + len(s) + 1 > max_chars and picked:
            break
        picked.append(s)
        total += len(s) + 1
        if len(picked) >= 2:
            break
    return " ".join(picked) if picked else cleaned[:max_chars]


_QUICK_SOCIAL_MAP: list[tuple[set[str], str]] = [
    (
        {
            "hello", "hi", "hey", "yo", "hola", "jude", "hey jude", "hello jude", "hi jude",
            "good morning", "good morning jude", "good afternoon", "good evening",
        },
        "Hello! I'm J.U.D.E., your local desktop assistant. I'm listening — how can I help you?",
    ),
    (
        {
            "who are you", "tell me about yourself", "what is your name", "what are you",
        },
        "I am J.U.D.E., your local-first Windows 11 desktop AI agent. I handle fast commands locally and use my tool-calling brain for your PC, Word documents, and VMware Linux guests.",
    ),
    (
        {
            "who made you", "who created you", "who built you", "who developed you", "who is your creator",
        },
        "I am J.U.D.E., a local-first Windows 11 desktop AI assistant built as a custom two-tier AI project running locally on your PC.",
    ),
    (
        {
            "can you hear me", "hey jude can you hear me", "jude can you hear me",
            "are you listening", "are you there", "you there", "testing",
        },
        "Loud and clear! Ask me anything or give me a command.",
    ),
    (
        {
            "how are you", "how are you doing", "how are you jude",
            "whats up", "what's up", "whats up jude", "what's up jude",
        },
        "Running smoothly and ready to help! What would you like to do?",
    ),
    (
        {"thanks", "thank you", "thanks jude", "thank you jude", "ty"},
        "You're welcome! Let me know if you need anything else.",
    ),
]


def _match_quick_social(cleaned: str) -> str | None:
    norm = re.sub(r"[^\w\s']", "", cleaned.lower()).strip()
    norm = " ".join(norm.split())
    stripped = re.sub(r"^(?:(?:hey|hi|hello)\s+)?jude\s+", "", norm).strip()
    stripped = re.sub(r"\s+jude$", "", stripped).strip()
    for phrases, reply in _QUICK_SOCIAL_MAP:
        if norm in phrases or stripped in phrases:
            return reply
    return None


_MEMORY_LIST_RE = re.compile(
    r"\b(?:what(?:'s|\s+is)\s+(?:inside|in)\s+your\s+memory|(?:show|list)\s+(?:my\s+)?memor(?:y|ies)|what\s+do\s+you\s+remember)\b",
    flags=re.IGNORECASE,
)
_MEMORY_DELETE_RE = re.compile(
    r"^(?:(?:hey\s+)?jude[\s,]+)?(?:uh\s+)?(?:please\s+|can\s+you\s+)?"
    r"(?:forget\b|(?:delete|remove|erase|clear)\s+(?:[\w\s]+\s+)?memor(?:y|ies)\b|(?:delete|remove|erase)\s+(?:my|the)\s+(?:saved\s+)?)",
    flags=re.IGNORECASE,
)
_DRIVE_OPEN_RE = re.compile(
    r"^(?:(?:hey\s+)?jude[\s,]+)?(?:please\s+)?(?:open|take\s+me\s+to|go\s+(?:to|in))\s+(?:local\s+disk\s+[a-z]|[a-z]\s+drive|drive\s+[a-z])\s*[.?!]?$",
    flags=re.IGNORECASE,
)
_OPEN_APP_RE = re.compile(
    r"^(?:(?:hey|hi|hello)\s+)?jude[\s,:-]+(?:please\s+|can\s+you\s+)?(?:open|launch|start)\s+.+$"
    r"|^(?:please\s+|can\s+you\s+)?(?:open|launch|start)\s+.+$",
    flags=re.IGNORECASE,
)
_CLOSE_APP_RE = re.compile(
    r"^(?:(?:(?:hey|hi|hello)\s+)?jude[\s,:-]+)?(?:please\s+|can\s+you\s+)?close\s+(.+?)[.?!]*$",
    flags=re.IGNORECASE,
)
_WORD_EDIT_RE = re.compile(
    r"\bword\s+(?:doc(?:ument)?|file)\b.*\b(?:fill|edit|rewrite|polish|update|change|insert|fix|shorten|add|type|put|append|write)\b"
    r"|\b(?:fill|edit|rewrite|polish|update|change|insert|fix|shorten|add|type|put|append|write)\b.*\b(?:word\s+(?:doc(?:ument)?|file)|highlighted\s+(?:area|text|section|part)|on\s+the\s+highlighted)\b",
    flags=re.IGNORECASE,
)
# Requests mentioning multi-step PC/agent concepts always belong to the Tier 2 Tool-Calling Agent
_AGENT_BYPASS_RE = re.compile(
    r"\b(?:terminal|powershell|cmd|close|focus|switch\s+to|windows|create\s+(?:a\s+)?word|new\s+word|"
    r"executive\s+summary|read\s+(?:the\s+|my\s+)?(?:word|doc|document|section|paragraph)|"
    r"vmware|linux|ubuntu|vm\b|ssh|whatsapp|google\s+for|youtube|inspect\s+ui|buttons?\s+on)\b",
    flags=re.IGNORECASE,
)


def _match_deterministic_guard(cleaned: str) -> tuple[str, str] | None:
    """Catch deterministic Tier 1 / Word / Agent commands before the 9-class ONNX classifier."""
    low = cleaned.lower()
    if _MEMORY_LIST_RE.search(low):
        return ("RECALL_MEMORY", "tier1")
    if _MEMORY_DELETE_RE.search(low):
        return ("DELETE_MEMORY", "tier1")
    if ("highlight" in low or "selected" in low) and "read" in low and not any(
        w in low for w in ("edit", "rewrite", "polish", "shorten", "fix", "add", "type", "insert", "put")
    ):
        return ("READ_SELECTION", "tier1")
    if _DRIVE_OPEN_RE.search(low):
        return ("OPEN_TARGET", "tier1")
    if _CLOSE_APP_RE.match(cleaned):
        return ("CLOSE_APP_DIRECT", "tier2")
    if (
        _OPEN_APP_RE.match(cleaned)
        and not re.search(r"\b(?:terminal|powershell|cmd|create|new\s+word|search|vmware|linux|ubuntu|compress|zip|archive)\b", low)
        and open_target(cleaned, dry_run=True)[0]
    ):
        return ("OPEN_TARGET", "tier1")
    if _WORD_EDIT_RE.search(low) and not re.search(r"\b(?:create|new|make)\b", low):
        return ("EDIT_DOCUMENT", "tier2")
    if _AGENT_BYPASS_RE.search(low):
        return ("AGENT_TASK", "tier2")
    return None


class JudeEngine:
    """Two-tier J.U.D.E. orchestrator connecting high-confidence Tier 1 to Tier 2 Tool Agent."""

    def __init__(
        self,
        model_dir: Path = TIER1_MODEL_DIR,
        memory_db_path: Path = MEMORY_DB_PATH,
        backups_dir: Path = BACKUPS_DIR,
        feedback_path: Path | None = None,
        ollama: OllamaClient | None = None,
    ) -> None:
        self.memory_db_path = Path(memory_db_path)
        self.backups_dir = Path(backups_dir)
        self.feedback_path = Path(feedback_path) if feedback_path else (DATA_DIR / "feedback.jsonl")
        self.ollama = ollama or OllamaClient()

        try:
            self.router: IntentRouter | _FallbackRouter = IntentRouter(model_dir=model_dir)
            self.using_onnx = True
        except FileNotFoundError:
            self.router = _FallbackRouter()
            self.using_onnx = False

        self._pending: dict[str, PendingConfirmation] = {}
        self._last_opened_target: str = ""

    @property
    def router_status(self) -> str:
        if self.using_onnx:
            return (
                f"ONNX INT8 ({self.router.active_provider}, "
                f"fast >={TIER1_CONFIDENCE_THRESHOLD:.2f}, silent Tier 2 escalation)"
            )
        return "Rule Fallback (ONNX bundle not found)"

    def _log_feedback(self, event: str, text: str, intent: str, confidence: float) -> None:
        self.feedback_path.parent.mkdir(parents=True, exist_ok=True)
        record = {
            "timestamp": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "event": event,
            "text": text,
            "intent": intent,
            "confidence": round(confidence, 4),
        }
        with self.feedback_path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(record) + "\n")

    def _run_tier1_action(self, intent: str, text: str, target_hwnd: int | None = None) -> str:
        if intent == "SAVE_MEMORY":
            return save_memory(text, db_path=self.memory_db_path)
        if intent == "RECALL_MEMORY":
            return recall_memory(text, db_path=self.memory_db_path)
        if intent == "DELETE_MEMORY":
            return delete_memory_by_query(text, db_path=self.memory_db_path)
        if intent == "READ_SELECTION":
            _, msg = read_highlighted_text(target_hwnd=target_hwnd)
            return msg
        if intent == "OPEN_TARGET":
            ok, msg = open_target(text)
            if ok:
                self._remember_opened_target(msg)
                log_activity("open_target", text[:120], msg, db_path=self.memory_db_path)
            return msg
        if intent == "SYSTEM_INFO":
            return get_system_info(text)
        return f"Unsupported Tier 1 action: {intent}"

    def _remember_opened_target(self, open_msg: str) -> None:
        m = re.match(r"^(?:Launched|Opened)\s+`?([^`]+?)`?\.?$", open_msg.strip(), flags=re.I)
        if m:
            raw = m.group(1).strip()
            if raw.lower().startswith("google chrome"):
                self._last_opened_target = "chrome"
            elif raw.lower().startswith("microsoft word"):
                self._last_opened_target = "word"
            elif raw.lower().startswith("whatsapp"):
                self._last_opened_target = "whatsapp"
            elif raw.lower().endswith("\\") or "\\" in raw:
                self._last_opened_target = "file manager"
            else:
                self._last_opened_target = raw.split(" in File Explorer", 1)[0].strip()

    @staticmethod
    def _clean_open_spoken(open_msg: str) -> str:
        low = open_msg.lower()
        if "in file explorer" in low:
            m = re.search(r"([A-Za-z]):\\", open_msg)
            return f"Opened {m.group(1).upper()} drive." if m else "Opened File Explorer."
        if "\\" in open_msg:
            m = re.search(r"`([^`]+)`", open_msg)
            if m:
                name = Path(m.group(1)).name or m.group(1)
                return f"Opened {name}."
        return _spoken_excerpt(open_msg)

    def _run_tier2_agent(
        self,
        cleaned: str,
        intent: str,
        confidence: float,
        gate_action: str,
        router_ms: float,
        t_start: float,
        target_hwnd: int | None,
    ) -> EngineResponse:
        agent_out = run_agent_turn(
            user_text=cleaned,
            ollama=self.ollama,
            target_hwnd=target_hwnd,
            memory_db_path=self.memory_db_path,
            backups_dir=self.backups_dir,
        )
        total_ms = (time.perf_counter() - t_start) * 1000.0
        pending_spec = agent_out.get("pending_spec")
        if pending_spec is not None:
            pid = uuid.uuid4().hex[:10]
            pending = PendingConfirmation(
                id=pid,
                kind=pending_spec["kind"],
                title=pending_spec["title"],
                summary=pending_spec["summary"],
                intent=pending_spec.get("intent", intent),
                confidence=float(pending_spec.get("confidence", confidence)),
                target_path=pending_spec.get("target_path", ""),
                backup_note=pending_spec.get("backup_note", ""),
                before_text=pending_spec.get("before_text", ""),
                after_text=pending_spec.get("after_text", ""),
                payload={
                    **pending_spec.get("payload", {}),
                    "original_text": cleaned,
                    "router_ms": router_ms,
                },
            )
            self._pending[pid] = pending
            reply = agent_out["reply"]
            return EngineResponse(
                reply=reply,
                spoken_summary=_spoken_excerpt(reply),
                intent=pending.intent,
                tier_used="pending",
                confidence=pending.confidence,
                gate_action="confirm",
                router_ms=router_ms,
                total_ms=total_ms,
                badge_text=f"Agent · Confirm ({total_ms / 1000:.1f}s)",
                sources=agent_out.get("sources") or [],
                pending=pending,
            )

        reply = agent_out.get("reply") or "Done."
        for opened in agent_out.get("opened_targets") or []:
            if opened:
                self._last_opened_target = opened
        tools_used = agent_out.get("tools_used") or []
        badge = (
            f"Agent · {len(tools_used)} tool(s) · {total_ms / 1000:.2f} s"
            if tools_used
            else f"Tier 2 · {total_ms / 1000:.2f} s"
        )
        return EngineResponse(
            reply=reply,
            spoken_summary=_spoken_excerpt(reply),
            intent=intent,
            tier_used="tier2",
            confidence=confidence,
            gate_action=gate_action,
            router_ms=router_ms,
            total_ms=total_ms,
            badge_text=badge,
            sources=agent_out.get("sources") or [],
        )

    def process(self, text: str, target_hwnd: int | None = None) -> EngineResponse:
        """Route and handle a user command through fast Tier 1 or Tier 2 Tool Agent."""
        t_start = time.perf_counter()
        cleaned = " ".join(text.strip().split())
        if not cleaned:
            return EngineResponse(
                reply="Type or ask a command for J.U.D.E.",
                spoken_summary="",
                intent="GENERAL_CHAT",
                tier_used="tier1",
                confidence=1.0,
                gate_action="direct",
                router_ms=0.0,
                total_ms=0.0,
                badge_text="Ready",
            )

        quick_reply = _match_quick_social(cleaned)
        if quick_reply is not None:
            threading.Thread(target=self.ollama.ensure_server_running, daemon=True).start()
            total_ms = (time.perf_counter() - t_start) * 1000.0
            return EngineResponse(
                reply=quick_reply,
                spoken_summary=quick_reply,
                intent="GENERAL_CHAT",
                tier_used="tier1",
                confidence=1.0,
                gate_action="direct",
                router_ms=0.0,
                total_ms=total_ms,
                badge_text=f"Tier 1 · Instant · {total_ms:.1f} ms",
            )

        guard = _match_deterministic_guard(cleaned)
        if guard is not None:
            guard_intent, guard_tier = guard
            route = RouteResult(
                text=cleaned,
                intent=guard_intent,
                tier=guard_tier,
                confidence=1.0,
                gate_action="direct",
                effective_tier=guard_tier,
                latency_ms=0.1,
                provider="RuleGuard",
                top_candidates=[(guard_intent, 1.0)],
            )
            if guard_tier == "tier1":
                reply = self._run_tier1_action(guard_intent, cleaned, target_hwnd=target_hwnd)
                total_ms = (time.perf_counter() - t_start) * 1000.0
                spoken = (
                    self._clean_open_spoken(reply)
                    if guard_intent == "OPEN_TARGET"
                    else _spoken_excerpt(reply)
                )
                return EngineResponse(
                    reply=reply,
                    spoken_summary=spoken,
                    intent=guard_intent,
                    tier_used="tier1",
                    confidence=1.0,
                    gate_action="direct",
                    router_ms=0.1,
                    total_ms=total_ms,
                    badge_text=f"Tier 1 · Fast · {total_ms:.1f} ms",
                )
            if guard_intent == "CLOSE_APP_DIRECT":
                m = _CLOSE_APP_RE.match(cleaned)
                raw_target = (m.group(1) if m else "").strip()
                raw_target = re.sub(r"[\s,]+jude$", "", raw_target, flags=re.I).strip()
                if raw_target.lower() in {"it", "that", "this", "this app", "that app", "the app", "that window", "this window"}:
                    raw_target = self._last_opened_target
                if not raw_target:
                    total_ms = (time.perf_counter() - t_start) * 1000.0
                    msg = "Tell me which open app or window you want me to close."
                    return EngineResponse(
                        reply=msg,
                        spoken_summary=msg,
                        intent="CLOSE_APP",
                        tier_used="tier2",
                        confidence=1.0,
                        gate_action="direct",
                        router_ms=0.1,
                        total_ms=total_ms,
                        badge_text=f"Agent · Clarify · {total_ms:.1f} ms",
                    )
                plan = plan_close_app(raw_target)
                total_ms = (time.perf_counter() - t_start) * 1000.0
                if not plan.get("ok"):
                    err = plan.get("error", f"Couldn't find an open window for {raw_target}.")
                    return EngineResponse(
                        reply=err,
                        spoken_summary=_spoken_excerpt(err),
                        intent="CLOSE_APP",
                        tier_used="tier2",
                        confidence=1.0,
                        gate_action="direct",
                        router_ms=0.1,
                        total_ms=total_ms,
                        badge_text=f"Agent · Close · {total_ms:.1f} ms",
                    )
                pid = uuid.uuid4().hex[:10]
                pending = PendingConfirmation(
                    id=pid,
                    kind="close_app",
                    title=f"Confirm Closing {plan['process']}",
                    summary=f"Send a close request to window `{plan['title']}` ({plan['process']})?",
                    intent="CLOSE_APP",
                    confidence=1.0,
                    target_path=f"{plan['title']} ({plan['process']})",
                    payload={**plan, "original_text": cleaned, "router_ms": 0.1},
                )
                self._pending[pid] = pending
                msg = f"Ready to close `{plan['title']}`. Confirm to continue."
                return EngineResponse(
                    reply=msg,
                    spoken_summary=f"Confirm closing {plan['process'].replace('.exe', '')}.",
                    intent="CLOSE_APP",
                    tier_used="pending",
                    confidence=1.0,
                    gate_action="confirm",
                    router_ms=0.1,
                    total_ms=total_ms,
                    badge_text=f"Agent · Confirm ({total_ms:.0f} ms)",
                    pending=pending,
                )
        else:
            route = self.router.predict(cleaned)

        # File compression ALWAYS requires explicit user confirmation (trigger from >=0.55)
        if route.intent == "COMPRESS_FILES" and route.confidence >= TIER1_CONFIRM_THRESHOLD:
            plan = plan_compression(cleaned)
            if not plan["ok"]:
                total_ms = (time.perf_counter() - t_start) * 1000.0
                return EngineResponse(
                    reply=plan["error"],
                    spoken_summary=plan["error"],
                    intent=route.intent,
                    tier_used="tier1",
                    confidence=route.confidence,
                    gate_action=route.gate_action,
                    router_ms=route.latency_ms,
                    total_ms=total_ms,
                    badge_text=f"Tier 1 · Fast · {total_ms:.1f} ms",
                )

            pid = uuid.uuid4().hex[:10]
            pending = PendingConfirmation(
                id=pid,
                kind="compress",
                title="Confirm Zip Archive Creation",
                summary=(
                    f"Compress {plan['file_count']} file(s) ({plan['total_mb']:.2f} MB) "
                    f"into a zip archive?"
                ),
                intent=route.intent,
                confidence=route.confidence,
                target_path=plan["archive_path"],
                backup_note="Original source files remain untouched.",
                payload={**plan, "original_text": cleaned, "router_ms": route.latency_ms},
            )
            self._pending[pid] = pending
            total_ms = (time.perf_counter() - t_start) * 1000.0
            return EngineResponse(
                reply=f"Ready to create `{plan['archive_path']}`. Confirm to proceed.",
                spoken_summary="Please confirm before I create the zip archive.",
                intent=route.intent,
                tier_used="pending",
                confidence=route.confidence,
                gate_action="confirm",
                router_ms=route.latency_ms,
                total_ms=total_ms,
                badge_text=f"Tier 1 · Confirm ({route.confidence:.0%})",
                pending=pending,
            )

        # Tier 1 direct execution ONLY when high confidence (>= 0.88). Zero nagging confirm popups!
        if route.tier == "tier1" and route.confidence >= TIER1_CONFIDENCE_THRESHOLD:
            if route.intent == "OPEN_TARGET":
                ok, msg = open_target(cleaned)
                if ok:
                    log_activity("open_target", cleaned[:120], msg, db_path=self.memory_db_path)
                    total_ms = (time.perf_counter() - t_start) * 1000.0
                    return EngineResponse(
                        reply=msg,
                        spoken_summary=_spoken_excerpt(msg),
                        intent=route.intent,
                        tier_used="tier1",
                        confidence=route.confidence,
                        gate_action="direct",
                        router_ms=route.latency_ms,
                        total_ms=total_ms,
                        badge_text=f"Tier 1 · Fast · {total_ms:.1f} ms",
                    )
                # Unknown open target -> let Tier 2 agent handle (browser, navigate, focus, etc.)
            else:
                reply = self._run_tier1_action(route.intent, cleaned, target_hwnd=target_hwnd)
                total_ms = (time.perf_counter() - t_start) * 1000.0
                return EngineResponse(
                    reply=reply,
                    spoken_summary=_spoken_excerpt(reply),
                    intent=route.intent,
                    tier_used="tier1",
                    confidence=route.confidence,
                    gate_action="direct",
                    router_ms=route.latency_ms,
                    total_ms=total_ms,
                    badge_text=f"Tier 1 · Fast · {total_ms:.1f} ms",
                )

        # Tier 2: EDIT_DOCUMENT always stages an editable diff preview + confirmation first
        if route.intent == "EDIT_DOCUMENT":
            prep = prepare_word_edit(cleaned, self.ollama, memory_db_path=self.memory_db_path)
            total_ms = (time.perf_counter() - t_start) * 1000.0
            if not prep.get("ok"):
                err = prep.get("error", "Could not inspect Microsoft Word.")
                return EngineResponse(
                    reply=err,
                    spoken_summary=err,
                    intent=route.intent,
                    tier_used="tier2",
                    confidence=route.confidence,
                    gate_action=route.gate_action,
                    router_ms=route.latency_ms,
                    total_ms=total_ms,
                    badge_text=f"Tier 2 · {total_ms / 1000:.2f} s",
                )

            pid = uuid.uuid4().hex[:10]
            pending = PendingConfirmation(
                id=pid,
                kind="word_edit",
                title=f"Confirm Word Edit ({prep['doc_name']})",
                summary="Review the proposed revision below. A lightweight text undo snapshot will be saved first.",
                intent=route.intent,
                confidence=route.confidence,
                target_path=prep["doc_full_path"] or prep["doc_name"],
                backup_note=f"Text undo snapshot saved into {self.backups_dir.name}/ before applying.",
                before_text=prep["before_text"],
                after_text=prep["after_text"],
                payload={**prep, "instruction": cleaned, "original_text": cleaned, "router_ms": route.latency_ms},
            )
            self._pending[pid] = pending
            return EngineResponse(
                reply=f"Drafted a proposed edit for `{prep['doc_name']}`. Review and confirm to apply.",
                spoken_summary="I drafted an edit for your Word document. Please review it.",
                intent=route.intent,
                tier_used="pending",
                confidence=route.confidence,
                gate_action="confirm",
                router_ms=route.latency_ms,
                total_ms=total_ms,
                badge_text=f"Tier 2 · Review Diff ({total_ms / 1000:.2f} s)",
                pending=pending,
            )

        # Everything else (including silent <0.88 Tier 1 escalations) goes to Tier 2 Tool Agent
        return self._run_tier2_agent(
            cleaned=cleaned,
            intent=route.intent,
            confidence=route.confidence,
            gate_action="escalate" if route.tier == "tier1" else route.gate_action,
            router_ms=route.latency_ms,
            t_start=t_start,
            target_hwnd=target_hwnd,
        )

    def confirm_pending(
        self, pending_id: str, edited_after_text: str | None = None
    ) -> EngineResponse:
        """Execute a staged confirmation action."""
        t_start = time.perf_counter()
        pending = self._pending.pop(pending_id, None)
        if pending is None:
            return EngineResponse(
                reply="That pending action is no longer active.",
                spoken_summary="",
                intent="GENERAL_CHAT",
                tier_used="tier1",
                confidence=1.0,
                gate_action="direct",
                router_ms=0.0,
                total_ms=0.0,
                badge_text="Ready",
            )

        router_ms = float(pending.payload.get("router_ms", 0.0))
        if pending.kind == "compress":
            reply = execute_compression(
                Path(pending.payload["source_path"]),
                Path(pending.payload["archive_path"]),
            )
            log_activity("compress", Path(pending.payload["archive_path"]).name, "", db_path=self.memory_db_path)
            total_ms = (time.perf_counter() - t_start) * 1000.0
            return EngineResponse(
                reply=reply,
                spoken_summary="Your zip archive has been created.",
                intent=pending.intent,
                tier_used="tier1",
                confidence=pending.confidence,
                gate_action="direct",
                router_ms=router_ms,
                total_ms=total_ms,
                badge_text=f"Tier 1 · Confirmed · {total_ms:.1f} ms",
            )

        if pending.kind == "word_edit":
            reply = apply_word_edit(
                pending.payload,
                revised_text=edited_after_text,
                backups_dir=self.backups_dir,
            )
            final_text = edited_after_text if edited_after_text is not None else pending.after_text
            infer_word_style_preference(
                instruction=pending.payload.get("instruction") or pending.payload.get("original_text", ""),
                before_text=pending.before_text,
                after_text=final_text,
                db_path=self.memory_db_path,
            )
            doc_stem = Path(pending.payload.get("doc_name", "Word")).stem or "Word"
            log_activity("word_edit", pending.payload.get("doc_name", "Word"), "", db_path=self.memory_db_path)
            total_ms = (time.perf_counter() - t_start) * 1000.0
            return EngineResponse(
                reply=reply,
                spoken_summary=f"Your Word document {doc_stem} has been updated, and an undo backup was saved.",
                intent=pending.intent,
                tier_used="tier2",
                confidence=pending.confidence,
                gate_action="direct",
                router_ms=router_ms,
                total_ms=total_ms,
                badge_text=f"Tier 2 · Applied · {total_ms:.0f} ms",
            )

        if pending.kind == "word_create":
            reply = execute_word_create(
                pending.payload,
                edited_content=edited_after_text,
                open_after=True,
            )
            doc_stem = Path(pending.target_path).stem or "document"
            log_activity("word_create", Path(pending.target_path).name, "", db_path=self.memory_db_path)
            total_ms = (time.perf_counter() - t_start) * 1000.0
            return EngineResponse(
                reply=reply,
                spoken_summary=f"Created and opened {doc_stem}.",
                intent=pending.intent,
                tier_used="tier2",
                confidence=pending.confidence,
                gate_action="direct",
                router_ms=router_ms,
                total_ms=total_ms,
                badge_text=f"Agent · Created · {total_ms:.0f} ms",
            )

        if pending.kind == "close_app":
            reply = execute_close_app(pending.payload)
            proc_clean = str(pending.payload.get("process", "application")).replace(".exe", "")
            log_activity("close_app", pending.payload.get("process", "app"), "", db_path=self.memory_db_path)
            total_ms = (time.perf_counter() - t_start) * 1000.0
            return EngineResponse(
                reply=reply,
                spoken_summary=f"Closed {proc_clean}.",
                intent=pending.intent,
                tier_used="tier2",
                confidence=pending.confidence,
                gate_action="direct",
                router_ms=router_ms,
                total_ms=total_ms,
                badge_text=f"Agent · Closed · {total_ms:.0f} ms",
            )

        if pending.kind == "vmware_cmd":
            cmd = pending.payload.get("command", "")
            res = execute_vmware_ssh_command(
                command=cmd,
                host_ip=pending.payload.get("host_ip", ""),
                vmx_path=pending.payload.get("vmx_path", ""),
            )
            if res.get("ok"):
                log_activity("vmware_ssh", cmd[:80], res.get("host_ip", ""), db_path=self.memory_db_path)
                out_part = res.get("stdout") or "(no output)"
                reply = f"VMware Linux (`{res.get('host_ip')}`) exit {res.get('exit_code')}:\n{out_part}"
            else:
                reply = res.get("error") or res.get("stderr") or "VMware SSH command failed."
            total_ms = (time.perf_counter() - t_start) * 1000.0
            return EngineResponse(
                reply=reply,
                spoken_summary=_spoken_excerpt(reply),
                intent=pending.intent,
                tier_used="tier2",
                confidence=pending.confidence,
                gate_action="direct",
                router_ms=router_ms,
                total_ms=total_ms,
                badge_text=f"VMware · {total_ms / 1000:.2f} s",
            )

        total_ms = (time.perf_counter() - t_start) * 1000.0
        return EngineResponse(
            reply="Confirmed.",
            spoken_summary="Confirmed.",
            intent=pending.intent,
            tier_used="tier1",
            confidence=pending.confidence,
            gate_action="direct",
            router_ms=router_ms,
            total_ms=total_ms,
            badge_text="Confirmed",
        )

    def cancel_pending(
        self, pending_id: str, escalate_to_tier2: bool = False
    ) -> EngineResponse:
        """Cancel a staged action and log feedback."""
        t_start = time.perf_counter()
        pending = self._pending.pop(pending_id, None)
        if pending is None:
            return EngineResponse(
                reply="Cancelled.",
                spoken_summary="",
                intent="GENERAL_CHAT",
                tier_used="tier1",
                confidence=1.0,
                gate_action="direct",
                router_ms=0.0,
                total_ms=0.0,
                badge_text="Ready",
            )

        original_text = pending.payload.get("original_text", pending.summary)
        self._log_feedback(
            event="escalated" if escalate_to_tier2 else "cancelled",
            text=original_text,
            intent=pending.intent,
            confidence=pending.confidence,
        )

        if escalate_to_tier2:
            return self._run_tier2_agent(
                cleaned=original_text,
                intent="GENERAL_CHAT",
                confidence=pending.confidence,
                gate_action="escalate",
                router_ms=float(pending.payload.get("router_ms", 0.0)),
                t_start=t_start,
                target_hwnd=None,
            )

        return EngineResponse(
            reply="Cancelled — no files or settings were modified.",
            spoken_summary="Cancelled.",
            intent=pending.intent,
            tier_used="tier1",
            confidence=pending.confidence,
            gate_action="confirm",
            router_ms=float(pending.payload.get("router_ms", 0.0)),
            total_ms=(time.perf_counter() - t_start) * 1000.0,
            badge_text="Cancelled",
        )
