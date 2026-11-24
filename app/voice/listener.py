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

_INITIAL_PROMPT = (
    "Jude, Hey Jude, Hello Jude. Commands for Jude: remember, recall memory, "
    "open folder, compress zip, system status, explain screen, edit Word document, "
    "search the web, tell me about yourself."
)

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


def normalize_wake_word(text: str) -> str:
    """Correct common acoustic near-misses for the 'Jude' wake word."""
    fixed = re.sub(r"\b(hey|hi|hello)\s+(?:dude|jood|june|jewel|judi)\b", r"\1 Jude", text, flags=re.IGNORECASE)
    fixed = re.sub(r"^(?:dude|jood)\b", "Jude", fixed, flags=re.IGNORECASE)
    return fixed


class VoiceListener(QObject):
    """Hands-free local microphone listener backed by faster-whisper (small.en INT8)."""

    speech_started = Signal()
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

                            thresh = max(0.015, noise_floor * 3.0)
                            if rms >= thresh:
                                speech_hits += 1
                                if speech_hits >= 2:
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

                            # Stop after ~1.1s silence (11 frames) or 12s max (120 frames)
                            if silence_frames >= 11 or len(frames) >= 120:
                                clip = np.concatenate(frames)
                                in_speech = False
                                speech_hits = 0
                                silence_frames = 0
                                frames = []

                                dur_sec = len(clip) / float(sample_rate)
                                peak_rms = float(np.sqrt(np.mean(clip * clip)))
                                if dur_sec >= 0.45:
                                    text = self._transcribe_clip(model, clip, peak_rms)
                                    if text:
                                        self.utterance_ready.emit(text)
                                        break
            except Exception as exc:
                self.status_changed.emit(f"Mic paused: {exc}")
                time.sleep(2.0)

    def _transcribe_clip(self, model, clip: np.ndarray, peak_rms: float) -> str:
        kwargs: dict = {
            "language": "en",
            "beam_size": 2,
            "vad_filter": True,
            "initial_prompt": _INITIAL_PROMPT,
            "condition_on_previous_text": False,
        }
        if "hotwords" in self._transcribe_params:
            kwargs["hotwords"] = "Jude"

        segments, _info = model.transcribe(clip, **kwargs)
        parts: list[str] = []
        logprobs: list[float] = []
        for seg in segments:
            no_speech = getattr(seg, "no_speech_prob", 0.0)
            avg_lp = getattr(seg, "avg_logprob", 0.0)
            if no_speech > 0.55 or avg_lp < -0.90:
                continue
            txt = seg.text.strip()
            if txt:
                parts.append(txt)
                logprobs.append(float(avg_lp))

        full_text = " ".join(parts).strip()
        if not full_text:
            return ""

        mean_lp = sum(logprobs) / len(logprobs) if logprobs else 0.0
        low_text = full_text.lower()
        if low_text in _HALLUCINATION_PHRASES and (peak_rms < 0.030 or mean_lp < -0.45):
            return ""
        if len(full_text.split()) <= 2 and mean_lp < -0.65:
            return ""

        return normalize_wake_word(full_text)
