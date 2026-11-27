from __future__ import annotations

import inspect
import os
import re
import threading
import time
from typing import Callable

import numpy as np
import sounddevice as sd
from PySide6.QtCore import QObject, Signal

from app.config import AUDIO_MODELS_DIR

_INITIAL_PROMPT = "Jude, WhatsApp, Chrome, Microsoft Word."

_PROMPT_VOCAB = {
    "jude",
    "whatsapp",
    "chrome",
    "microsoft",
    "word",
    "google",
    "explorer",
    "terminal",
    "cmd",
    "vmware",
}

_ACTION_VERBS = {
    "open",
    "launch",
    "start",
    "close",
    "focus",
    "run",
    "type",
    "execute",
    "explain",
    "read",
    "rewrite",
    "polish",
    "edit",
    "create",
    "remember",
    "recall",
    "delete",
    "forget",
    "minimize",
    "maximize",
    "restore",
    "search",
    "compress",
    "show",
    "give",
    "what",
    "how",
    "who",
    "where",
    "why",
}

_HALLUCINATION_PHRASES = {
    "you",
    "you.",
    "i do",
    "i do.",
    "thank you",
    "thank you.",
    "thanks for watching",
    "thanks for watching.",
    "bye",
    "bye.",
    "a-mail",
    "a-mail.",
    "okay",
    "okay.",
}


def _preprocess_audio(clip: np.ndarray, sample_rate: int = 16000) -> tuple[np.ndarray, float]:
    """Remove DC/low rumble with 1-pole HPF (alpha=0.95), measure active RMS, and normalize gain."""
    x = np.asarray(clip, dtype=np.float32).reshape(-1)
    if x.size == 0:
        return x, 0.0
    x = x - float(np.mean(x))

    alpha = 0.95
    filtered = np.empty_like(x)
    y_prev = 0.0
    x_prev = 0.0
    for i in range(x.size):
        xi = float(x[i])
        y_prev = alpha * (y_prev + xi - x_prev)
        filtered[i] = y_prev
        x_prev = xi

    block = max(1, int(sample_rate // 10))
    n_blocks = filtered.size // block
    if n_blocks > 0:
        frames = filtered[: n_blocks * block].reshape(n_blocks, block)
        frame_rms = np.sqrt(np.mean(frames * frames, axis=1))
        max_frame_rms = float(np.max(frame_rms))
    else:
        max_frame_rms = float(np.sqrt(np.mean(filtered * filtered)))

    max_abs = float(np.max(np.abs(filtered)))
    if max_abs > 0.015:
        gain = min(0.85 / max_abs, 12.0)
        filtered = np.clip(filtered * gain, -1.0, 1.0)
    return filtered, max_frame_rms


def _is_prompt_echo(text: str) -> bool:
    """Detect pure noun echoes of _INITIAL_PROMPT/hotwords when no action/question was spoken."""
    words = [w for w in re.sub(r"[^\w\s]", " ", text.lower()).split() if w]
    if not words or len(words) > 6:
        return False
    if any(w in _ACTION_VERBS for w in words):
        return False
    return all(w in _PROMPT_VOCAB for w in words)


def normalize_wake_word(text: str) -> str:
    """Correct common Whisper acoustic near-misses for J.U.D.E. and desktop app names."""
    fixed = re.sub(r"\b(hey|hi|hello)\s+(?:dude|jood|june|jewel|judi)\b", r"\1 Jude", text, flags=re.IGNORECASE)
    fixed = re.sub(r"^(?:dude|jood)\b", "Jude", fixed, flags=re.IGNORECASE)
    fixed = re.sub(r"\b(?:wordset|word\s+set|watts\s*app|what['’]s\s+app)\b", "WhatsApp", fixed, flags=re.IGNORECASE)
    fixed = re.sub(
        r"\b(open|launch|start|close|focus)\s+(?:word\s+app|word\s*,\s*sir|word\s+sir|what\s+app|concept)\b",
        r"\1 WhatsApp",
        fixed,
        flags=re.IGNORECASE,
    )
    return fixed


class VoiceListener(QObject):
    """Hands-free local microphone listener backed by faster-whisper (small.en INT8)."""

    speech_started = Signal()
    speech_ended = Signal()
    utterance_ready = Signal(str)
    status_changed = Signal(str)

    def __init__(
        self,
        enabled: bool | None = None,
        echo_guard_fn: Callable[[], bool] | None = None,
        parent: QObject | None = None,
    ) -> None:
        super().__init__(parent)
        headless = os.environ.get("JUDE_HEADLESS_TEST") == "1"
        self._enabled: bool = False if headless else (True if enabled is None else bool(enabled))
        self._paused: bool = False
        self._stop_event = threading.Event()
        self._lock = threading.Lock()
        self._echo_guard_fn = echo_guard_fn
        self._model = None
        self._transcribe_params: set[str] = set()

        self._thread: threading.Thread | None = None
        if not headless:
            self._thread = threading.Thread(
                target=self._listen_loop,
                daemon=True,
                name="JudeMicListener",
            )
            self._thread.start()

    @property
    def enabled(self) -> bool:
        with self._lock:
            return self._enabled

    def set_enabled(self, enabled: bool) -> None:
        with self._lock:
            self._enabled = False if os.environ.get("JUDE_HEADLESS_TEST") == "1" else bool(enabled)

    def set_paused(self, paused: bool) -> None:
        with self._lock:
            self._paused = bool(paused)

    def stop(self) -> None:
        self._stop_event.set()
        if self._thread is not None and self._thread.is_alive():
            self._thread.join(timeout=1.0)

    def _should_listen(self) -> bool:
        with self._lock:
            if not self._enabled or self._paused:
                return False
        if self._echo_guard_fn is not None and self._echo_guard_fn():
            return False
        return True

    def _ensure_model(self):
        if self._model is None:
            from faster_whisper import WhisperModel

            AUDIO_MODELS_DIR.mkdir(parents=True, exist_ok=True)
            self._model = WhisperModel(
                "small.en",
                device="cpu",
                compute_type="int8",
                download_root=str(AUDIO_MODELS_DIR),
                cpu_threads=4,
            )
            self._transcribe_params = set(inspect.signature(self._model.transcribe).parameters)
        return self._model

    def _listen_loop(self) -> None:
        sample_rate = 16000
        block_size = 1600  # 100 ms frames
        noise_floor = 0.004
        consecutive_errors = 0

        while not self._stop_event.is_set():
            if not self._should_listen():
                time.sleep(0.15)
                continue

            try:
                model = self._ensure_model()
                with sd.InputStream(
                    samplerate=sample_rate,
                    channels=1,
                    dtype="float32",
                    blocksize=block_size,
                ) as stream:
                    consecutive_errors = 0
                    in_speech = False
                    speech_hits = 0
                    silence_frames = 0
                    frames: list[np.ndarray] = []
                    pre_roll: list[np.ndarray] = []

                    while not self._stop_event.is_set() and self._should_listen():
                        data, _overflowed = stream.read(block_size)
                        chunk = data[:, 0].copy()
                        rms = float(np.sqrt(np.mean(chunk * chunk)))

                        if not in_speech:
                            pre_roll.append(chunk)
                            if len(pre_roll) > 6:  # 600 ms pre-roll protects word starts
                                pre_roll.pop(0)

                            thresh = max(0.016, noise_floor * 3.2)
                            if rms >= thresh:
                                speech_hits += 1
                                if speech_hits >= 3:
                                    in_speech = True
                                    silence_frames = 0
                                    frames = list(pre_roll)
                                    self.speech_started.emit()
                            else:
                                speech_hits = 0
                                noise_floor = 0.92 * noise_floor + 0.08 * min(rms, 0.02)
                        else:
                            frames.append(chunk)
                            cont_thresh = max(0.010, noise_floor * 2.1)
                            if rms < cont_thresh:
                                silence_frames += 1
                            else:
                                silence_frames = 0

                            # Stop after ~1.5s silence (15 frames) or 14s max (140 frames)
                            if silence_frames >= 15 or len(frames) >= 140:
                                clip = np.concatenate(frames)
                                in_speech = False
                                speech_hits = 0
                                silence_frames = 0
                                frames = []

                                dur_sec = len(clip) / float(sample_rate)
                                proc_clip, active_rms = _preprocess_audio(clip, sample_rate)
                                if dur_sec >= 0.45 and active_rms >= max(0.016, noise_floor * 3.0):
                                    text = self._transcribe_clip(model, proc_clip, active_rms)
                                    if text:
                                        self.utterance_ready.emit(text)
                                        break
                                self.speech_ended.emit()
            except Exception as exc:
                consecutive_errors += 1
                if consecutive_errors >= 2:
                    self.status_changed.emit(f"Mic paused: {exc}")
                    time.sleep(2.0)
                else:
                    time.sleep(0.6)

    def _transcribe_clip(self, model, clip: np.ndarray, peak_rms: float) -> str:
        raw_kwargs: dict = {
            "language": "en",
            "beam_size": 5,
            "temperature": 0.0,
            "compression_ratio_threshold": 2.2,
            "log_prob_threshold": -0.95,
            "no_speech_threshold": 0.60,
            "vad_filter": True,
            "vad_parameters": {
                "threshold": 0.55,
                "min_speech_duration_ms": 250,
                "min_silence_duration_ms": 500,
                "speech_pad_ms": 250,
            },
            "initial_prompt": _INITIAL_PROMPT,
            "condition_on_previous_text": False,
            "hotwords": "Jude WhatsApp Chrome Google Word Explorer Terminal CMD VMware",
        }
        kwargs = {
            k: v
            for k, v in raw_kwargs.items()
            if not self._transcribe_params or k in self._transcribe_params
        }

        segments, _info = model.transcribe(clip, **kwargs)
        parts: list[str] = []
        logprobs: list[float] = []
        for seg in segments:
            no_speech = getattr(seg, "no_speech_prob", 0.0)
            avg_lp = getattr(seg, "avg_logprob", 0.0)
            comp_ratio = getattr(seg, "compression_ratio", 0.0)
            if no_speech > 0.60 or avg_lp < -0.95 or comp_ratio > 2.2:
                continue
            txt = seg.text.strip()
            if txt:
                parts.append(txt)
                logprobs.append(float(avg_lp))

        full_text = " ".join(parts).strip()
        if not full_text:
            return ""

        if _is_prompt_echo(full_text):
            return ""

        mean_lp = sum(logprobs) / len(logprobs) if logprobs else 0.0
        low_text = full_text.lower()
        if low_text in _HALLUCINATION_PHRASES and (peak_rms < 0.025 or mean_lp < -0.55):
            return ""
        if len(full_text.split()) <= 2 and mean_lp < -0.80:
            return ""

        return normalize_wake_word(full_text)
