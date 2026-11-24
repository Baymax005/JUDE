from __future__ import annotations

import os
import queue
import re
import threading
import time

import pythoncom
import win32com.client

_MD_LINK_RE = re.compile(r"\[([^\]]+)\]\([^)]+\)")
_URL_RE = re.compile(r"https?://\S+")
_JUDE_ACRONYM_RE = re.compile(r"\bJ\.U\.D\.E\.?", flags=re.IGNORECASE)
_JUDE_CAPS_RE = re.compile(r"\bJUDE\b")


def normalize_for_speech(text: str) -> str:
    """Clean markdown, URLs, and 'J.U.D.E.' punctuation so SAPI5 pronounces 'Jude' naturally."""
    cleaned = _MD_LINK_RE.sub(r"\1", text)
    cleaned = _URL_RE.sub("", cleaned)
    cleaned = _JUDE_ACRONYM_RE.sub("Jude", cleaned)
    cleaned = _JUDE_CAPS_RE.sub("Jude", cleaned)
    cleaned = re.sub(r"[`*_#>]+", "", cleaned)
    cleaned = re.sub(r"^\s*[-•]\s+", "", cleaned, flags=re.MULTILINE)
    cleaned = re.sub(r"\s*\n+\s*", ". ", cleaned)
    cleaned = re.sub(r"\.\s*\.", ".", cleaned)
    return " ".join(cleaned.split()).strip()


class VoiceSpeaker:
    """Non-blocking thread-safe SAPI5 COM speaker with instant mute purge and echo guard."""

    def __init__(
        self,
        muted: bool | None = None,
        _force_silent_volume: bool = False,
        _voice_factory=None,
    ) -> None:
        headless = os.environ.get("JUDE_HEADLESS_TEST") == "1"
        self._headless = headless
        self._force_silent_volume = _force_silent_volume
        self._voice_factory = _voice_factory
        self._muted: bool = True if (headless and not _force_silent_volume and _voice_factory is None) else bool(muted) if muted is not None else False
        self._queue: queue.Queue[str | None] = queue.Queue()
        self._lock = threading.Lock()
        self._speaking: bool = False
        self._last_spoken_end: float = 0.0
        self._spoken_count: int = 0
        self._stop_requested: bool = False
        self._thread = threading.Thread(target=self._worker_loop, daemon=True, name="JudeTTS")
        self._thread.start()

    @property
    def muted(self) -> bool:
        with self._lock:
            return self._muted

    @property
    def spoken_count(self) -> int:
        with self._lock:
            return self._spoken_count

    @property
    def is_speaking_or_cooling(self) -> bool:
        """Return True while TTS is speaking or within 0.8s after speech finishes."""
        with self._lock:
            if self._speaking:
                return True
            if self._last_spoken_end > 0.0 and (time.monotonic() - self._last_spoken_end) < 0.8:
                return True
            return False

    def set_muted(self, muted: bool) -> None:
        with self._lock:
            self._muted = bool(muted)
        if muted:
            while not self._queue.empty():
                try:
                    self._queue.get_nowait()
                except queue.Empty:
                    break

    def speak(self, text: str) -> None:
        if self.muted or (self._headless and not self._force_silent_volume and self._voice_factory is None):
            return
        spoken = normalize_for_speech(text)
        if spoken:
            self._queue.put(spoken[:450])

    def stop(self) -> None:
        with self._lock:
            self._stop_requested = True
        self._queue.put(None)

    def _init_sapi_voice(self):
        if self._voice_factory is not None:
            return self._voice_factory()
        voice = win32com.client.Dispatch("SAPI.SpVoice")
        try:
            voices = voice.GetVoices()
            for i in range(voices.Count):
                desc = voices.Item(i).GetDescription()
                if "david" in desc.lower():
                    voice.Voice = voices.Item(i)
                    break
        except Exception:
            pass
        voice.Rate = 1
        voice.Volume = 0 if self._force_silent_volume else 100
        return voice

    def _worker_loop(self) -> None:
        pythoncom.CoInitialize()
        voice = None
        try:
            voice = self._init_sapi_voice()
        except Exception:
            voice = None

        try:
            while True:
                item = self._queue.get()
                if item is None:
                    break
                if self.muted or voice is None:
                    continue

                with self._lock:
                    self._speaking = True
                try:
                    # SVSFlagsAsync = 1 | SVSFPurgeBeforeSpeak = 2
                    voice.Speak(item, 1 | 2)
                    deadline = time.monotonic() + 35.0
                    while not voice.WaitUntilDone(80):
                        pythoncom.PumpWaitingMessages()
                        if self.muted or self._stop_requested or time.monotonic() > deadline:
                            voice.Speak("", 2)  # Purge speech immediately
                            break
                    with self._lock:
                        self._spoken_count += 1
                except Exception:
                    try:
                        voice = self._init_sapi_voice()
                    except Exception:
                        voice = None
                finally:
                    with self._lock:
                        self._speaking = False
                        self._last_spoken_end = time.monotonic()
        finally:
            pythoncom.CoUninitialize()
