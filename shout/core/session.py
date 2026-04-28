"""Dictation session state machine.

Coordinates audio capture, VAD, transcription, optional LLM post-processing
and text injection. Runs on a background thread; emits state changes via a
simple observer callback so the UI can subscribe (typically forwarded to a
Qt signal).
"""
from __future__ import annotations

import logging
import threading
from enum import Enum
from pathlib import Path
from typing import Callable

from ..config import Config
from .audio import AudioCapture
from .postprocess import postprocess
from .transcriber import Transcriber
from .vad import SilenceDetector

log = logging.getLogger(__name__)


class State(str, Enum):
    IDLE = "idle"
    RECORDING = "recording"
    TRANSCRIBING = "transcribing"
    INJECTING = "injecting"
    ERROR = "error"


Listener = Callable[[State, str], None]
TextSink = Callable[[str], None]


class DictationSession:
    def __init__(
        self,
        cfg: Config,
        model_path: Path,
        text_sink: TextSink,
        listener: Listener | None = None,
    ) -> None:
        self.cfg = cfg
        self.model_path = model_path
        self.text_sink = text_sink
        self.listener = listener
        self._state = State.IDLE
        self._lock = threading.Lock()
        self._cancel = threading.Event()
        self._capture: AudioCapture | None = None
        self._vad: SilenceDetector | None = None
        self._stop_requested = threading.Event()
        self._transcriber = Transcriber(model_path)
        self._language: str | None = None

    # --- Public API ----------------------------------------------------------

    @property
    def state(self) -> State:
        return self._state

    def set_language(self, language: str | None) -> None:
        self._language = language

    def toggle(self, language: str | None = None) -> None:
        if self._state == State.IDLE:
            self.start(language)
        elif self._state == State.RECORDING:
            self.stop()
        # Other states are transient; ignore extra triggers.

    def start(self, language: str | None = None) -> None:
        with self._lock:
            if self._state != State.IDLE:
                log.debug("start() ignored, state=%s", self._state)
                return
            self._cancel.clear()
            self._stop_requested.clear()
            if language:
                self._language = language
            self._set_state(State.RECORDING, "")
            t = threading.Thread(target=self._run, name="shout-session", daemon=True)
            t.start()

    def stop(self) -> None:
        if self._state == State.RECORDING:
            self._stop_requested.set()

    def cancel(self) -> None:
        log.info("Dictation cancelled")
        self._cancel.set()
        self._stop_requested.set()
        if self._capture is not None and self._capture.is_running:
            try:
                self._capture.stop()
            except Exception:  # noqa: BLE001
                log.exception("Error stopping capture during cancel")
        self._set_state(State.IDLE, "")

    # --- Internals -----------------------------------------------------------

    def _set_state(self, new: State, message: str) -> None:
        self._state = new
        log.debug("state -> %s (%s)", new.value, message)
        if self.listener is not None:
            try:
                self.listener(new, message)
            except Exception:  # noqa: BLE001
                log.exception("listener failed")

    def _run(self) -> None:
        try:
            pcm = self._record()
            if self._cancel.is_set():
                self._set_state(State.IDLE, "")
                return
            if not pcm:
                self._set_state(State.IDLE, "no audio")
                return

            self._set_state(State.TRANSCRIBING, "")
            language = self._language or self.cfg.languages.active
            if self.cfg.models.auto_detect_language:
                language = "auto"
            text = self._transcriber.transcribe(pcm, language=language)

            if self.cfg.llm.enabled and text:
                text = postprocess(text, self.cfg)

            if self._cancel.is_set() or not text:
                self._set_state(State.IDLE, "")
                return

            self._set_state(State.INJECTING, text)
            self.text_sink(text)
            self._set_state(State.IDLE, "")
        except Exception as exc:  # noqa: BLE001
            log.exception("Dictation session failed")
            self._set_state(State.ERROR, str(exc))
            # Auto-return to IDLE so the next hotkey works.
            self._set_state(State.IDLE, "")

    def _record(self) -> bytes:
        a = self.cfg.audio
        self._vad = SilenceDetector(
            sample_rate=a.sample_rate,
            aggressiveness=a.vad_aggressiveness,
            silence_timeout_ms=a.silence_timeout_ms,
        ) if a.vad_enabled else None

        max_frames = int(a.max_recording_seconds * 1000 / 20)
        frame_count = [0]
        stop_due_to_vad = threading.Event()

        def on_frame(frame: bytes) -> None:
            frame_count[0] += 1
            if self._vad and self._vad.push(frame):
                stop_due_to_vad.set()
            if frame_count[0] >= max_frames:
                stop_due_to_vad.set()

        self._capture = AudioCapture(
            sample_rate=a.sample_rate,
            device=a.input_device,
            on_frame=on_frame,
        )
        try:
            self._capture.start()
        except Exception as exc:  # noqa: BLE001
            log.exception("Audio start failed")
            raise RuntimeError(f"Could not start microphone: {exc}") from exc

        # Wait until either the user releases (stop_requested), VAD triggers,
        # or session is cancelled.
        while not (
            self._stop_requested.is_set()
            or stop_due_to_vad.is_set()
            or self._cancel.is_set()
        ):
            if self._stop_requested.wait(0.05):
                break

        return self._capture.stop()
