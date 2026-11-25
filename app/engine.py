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
from app.tier1.actions import execute_compression, get_system_info, open_target, plan_compression
from app.tier1.memory import delete_memory_by_query, recall_memory, save_memory
from app.tier2.ollama_client import OllamaClient
from app.tier2.screen_reader import explain_screen, read_highlighted_text
from app.tier2.web_search import run_web_search
from app.tier2.word_editor import apply_word_edit, prepare_word_edit


@dataclass
class PendingConfirmation:
    """Action awaiting explicit user confirmation before executing."""

    id: str
    kind: str  # "compress", "word_edit", or "intent_confirm"
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
            confidence=0.85,
            gate_action="direct",
            effective_tier=tier,
            latency_ms=latency_ms,
            provider=self.active_provider,
            top_candidates=[(intent, 0.85)],
        )


def _spoken_excerpt(text: str, max_chars: int = 280) -> str:
    """Keep up to 2 complete sentences for natural TTS replies (stripping source links)."""
    body = text.split("\n\nSources:", 1)[0].strip()
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
        "I am J.U.D.E., your local-first Windows 11 desktop AI assistant. I handle fast commands locally and wake my reasoning model when you need deeper help.",
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
    for phrases, reply in _QUICK_SOCIAL_MAP:
        if norm in phrases:
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
    r"\b(?:open|take\s+me\s+to|go\s+(?:to|in))\s+(?:local\s+disk\s+[a-z]|[a-z]\s+drive|drive\s+[a-z])\b",
    flags=re.IGNORECASE,
)
_WORD_EDIT_RE = re.compile(
    r"\bword\s+doc(?:ument)?\b.*\b(?:fill|edit|rewrite|polish|update|change|insert|fix|shorten)\b"
    r"|\b(?:fill|edit|rewrite|polish|update|change|insert|fix|shorten)\b.*\bword\s+doc(?:ument)?\b",
    flags=re.IGNORECASE,
)


def _match_deterministic_guard(cleaned: str) -> tuple[str, str] | None:
    """Catch high-value commands deterministically before the 9-class ONNX classifier."""
    low = cleaned.lower()
    if _MEMORY_LIST_RE.search(low):
        return ("RECALL_MEMORY", "tier1")
    if _MEMORY_DELETE_RE.search(low):
        return ("DELETE_MEMORY", "tier1")
    if ("highlight" in low or "selected" in low) and "read" in low and not any(
        w in low for w in ("edit", "rewrite", "polish", "shorten", "fix")
    ):
        return ("READ_SELECTION", "tier1")
    if _DRIVE_OPEN_RE.search(low):
        return ("OPEN_TARGET", "tier1")
    if _WORD_EDIT_RE.search(low):
        return ("EDIT_DOCUMENT", "tier2")
    return None


class JudeEngine:
    """Two-tier J.U.D.E. orchestrator connecting ONNX Tier 1 routing to Tier 1/2 handlers."""

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

    @property
    def router_status(self) -> str:
        if self.using_onnx:
            return (
                f"ONNX INT8 ({self.router.active_provider}, "
                f"gates >={TIER1_CONFIDENCE_THRESHOLD:.2f}/{TIER1_CONFIRM_THRESHOLD:.2f})"
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
            _, msg = open_target(text)
            return msg
        if intent == "SYSTEM_INFO":
            return get_system_info(text)
        return f"Unsupported Tier 1 action: {intent}"

    def process(self, text: str, target_hwnd: int | None = None) -> EngineResponse:
        """Route and handle a user command through Tier 1 or Tier 2 with safety gates."""
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
                return EngineResponse(
                    reply=reply,
                    spoken_summary=_spoken_excerpt(reply),
                    intent=guard_intent,
                    tier_used="tier1",
                    confidence=1.0,
                    gate_action="direct",
                    router_ms=0.1,
                    total_ms=total_ms,
                    badge_text=f"Tier 1 · Fast · {total_ms:.1f} ms",
                )
        else:
            route = self.router.predict(cleaned)

        # Low confidence (<0.55): escalate safely to Tier 2 general reasoning
        if route.gate_action == "escalate":
            reply = self.ollama.chat(cleaned)
            total_ms = (time.perf_counter() - t_start) * 1000.0
            return EngineResponse(
                reply=reply,
                spoken_summary=_spoken_excerpt(reply),
                intent=route.intent,
                tier_used="tier2",
                confidence=route.confidence,
                gate_action=route.gate_action,
                router_ms=route.latency_ms,
                total_ms=total_ms,
                badge_text=f"Escalated · Thinking · {total_ms / 1000:.2f} s",
            )

        # File compression ALWAYS requires explicit user confirmation
        if route.intent == "COMPRESS_FILES":
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
                gate_action=route.gate_action,
                router_ms=route.latency_ms,
                total_ms=total_ms,
                badge_text=f"Tier 1 · Confirm ({route.confidence:.0%})",
                pending=pending,
            )

        # Medium confidence (0.55 - 0.80) on Tier 1 actions asks confirmation first
        if route.gate_action == "confirm" and route.tier == "tier1":
            pid = uuid.uuid4().hex[:10]
            pending = PendingConfirmation(
                id=pid,
                kind="intent_confirm",
                title=f"Confirm Action ({route.intent})",
                summary=(
                    f"Confidence is {route.confidence:.0%}. Run deterministic `{route.intent}` "
                    "or escalate to Tier 2 reasoning instead?"
                ),
                intent=route.intent,
                confidence=route.confidence,
                payload={"original_text": cleaned, "intent": route.intent, "router_ms": route.latency_ms},
            )
            self._pending[pid] = pending
            total_ms = (time.perf_counter() - t_start) * 1000.0
            return EngineResponse(
                reply=f"Confirm `{route.intent}` ({route.confidence:.0%}) or send to Tier 2.",
                spoken_summary="Please confirm that action.",
                intent=route.intent,
                tier_used="pending",
                confidence=route.confidence,
                gate_action=route.gate_action,
                router_ms=route.latency_ms,
                total_ms=total_ms,
                badge_text=f"Gate · Confirm ({route.confidence:.0%})",
                pending=pending,
            )

        # Tier 1 direct execution
        if route.tier == "tier1":
            reply = self._run_tier1_action(route.intent, cleaned)
            total_ms = (time.perf_counter() - t_start) * 1000.0
            return EngineResponse(
                reply=reply,
                spoken_summary=_spoken_excerpt(reply),
                intent=route.intent,
                tier_used="tier1",
                confidence=route.confidence,
                gate_action=route.gate_action,
                router_ms=route.latency_ms,
                total_ms=total_ms,
                badge_text=f"Tier 1 · Fast · {total_ms:.1f} ms",
            )

        # Tier 2: EDIT_DOCUMENT always stages a diff preview + confirmation first
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
                    badge_text=f"Tier 2 · Thinking · {total_ms / 1000:.2f} s",
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
                payload={**prep, "original_text": cleaned, "router_ms": route.latency_ms},
            )
            self._pending[pid] = pending
            return EngineResponse(
                reply=f"Drafted a proposed edit for `{prep['doc_name']}`. Review and confirm to apply.",
                spoken_summary="I drafted an edit for your Word document. Please review it.",
                intent=route.intent,
                tier_used="pending",
                confidence=route.confidence,
                gate_action=route.gate_action,
                router_ms=route.latency_ms,
                total_ms=total_ms,
                badge_text=f"Tier 2 · Review Diff ({total_ms / 1000:.2f} s)",
                pending=pending,
            )

        # Tier 2: EXPLAIN_SCREEN, WEB_SEARCH, GENERAL_CHAT
        sources: list[tuple[str, str]] = []
        if route.intent == "EXPLAIN_SCREEN":
            reply = explain_screen(cleaned, self.ollama, target_hwnd=target_hwnd)
        elif route.intent == "WEB_SEARCH":
            reply, sources = run_web_search(cleaned, self.ollama)
        else:
            reply = self.ollama.chat(cleaned)

        total_ms = (time.perf_counter() - t_start) * 1000.0
        return EngineResponse(
            reply=reply,
            spoken_summary=_spoken_excerpt(reply),
            intent=route.intent,
            tier_used="tier2",
            confidence=route.confidence,
            gate_action=route.gate_action,
            router_ms=route.latency_ms,
            total_ms=total_ms,
            badge_text=f"Tier 2 · Thinking · {total_ms / 1000:.2f} s",
            sources=sources,
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
            total_ms = (time.perf_counter() - t_start) * 1000.0
            return EngineResponse(
                reply=reply,
                spoken_summary=_spoken_excerpt(reply),
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
                badge_text=f"Tier 2 · Applied · {total_ms:.0f} ms",
            )

        # intent_confirm
        reply = self._run_tier1_action(pending.intent, pending.payload["original_text"])
        total_ms = (time.perf_counter() - t_start) * 1000.0
        return EngineResponse(
            reply=reply,
            spoken_summary=_spoken_excerpt(reply),
            intent=pending.intent,
            tier_used="tier1",
            confidence=pending.confidence,
            gate_action="direct",
            router_ms=router_ms,
            total_ms=total_ms,
            badge_text=f"Tier 1 · Confirmed · {total_ms:.1f} ms",
        )

    def cancel_pending(
        self, pending_id: str, escalate_to_tier2: bool = False
    ) -> EngineResponse:
        """Cancel a staged action and log feedback, optionally escalating to Tier 2."""
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
            reply = self.ollama.chat(original_text)
            total_ms = (time.perf_counter() - t_start) * 1000.0
            return EngineResponse(
                reply=reply,
                spoken_summary=_spoken_excerpt(reply),
                intent="GENERAL_CHAT",
                tier_used="tier2",
                confidence=pending.confidence,
                gate_action="escalate",
                router_ms=float(pending.payload.get("router_ms", 0.0)),
                total_ms=total_ms,
                badge_text=f"Tier 2 · Thinking · {total_ms / 1000:.2f} s",
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
