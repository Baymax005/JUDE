from __future__ import annotations

import os
import queue
import threading

import pythoncom
import pyttsx3


class VoiceSpeaker:
    """Non-blocking thread-safe SAPI5 text-to-speech speaker with mute control."""

    def __init__(self, muted: bool | None = None) -> None:
        headless = os.environ.get("JUDE_HEADLESS_TEST") == "1"
        self._muted: bool = True if headless else bool(muted) if muted is not None else False
        self._queue: queue.Queue[str | None] = queue.Queue()
        self._lock = threading.Lock()
        self._thread = threading.Thread(target=self._worker_loop, daemon=True, name="JudeTTS")
        self._thread.start()

    @property
    def muted(self) -> bool:
        with self._lock:
            return self._muted

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
        if self.muted or os.environ.get("JUDE_HEADLESS_TEST") == "1":
            return
        cleaned = " ".join(text.strip().split())
        if cleaned:
            self._queue.put(cleaned[:400])

    def stop(self) -> None:
        self._queue.put(None)

    def _worker_loop(self) -> None:
        pythoncom.CoInitialize()
        engine = None
        try:
            engine = pyttsx3.init()
            engine.setProperty("rate", 185)
        except Exception:
            engine = None

        try:
            while True:
                item = self._queue.get()
                if item is None:
                    break
                if self.muted or engine is None:
                    continue
                try:
                    engine.say(item)
                    engine.runAndWait()
                except Exception:
                    pass
        finally:
            pythoncom.CoUninitialize()
