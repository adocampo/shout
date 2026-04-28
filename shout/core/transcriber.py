"""Whisper.cpp transcription wrapper (pywhispercpp)."""
from __future__ import annotations

import logging
import threading
from pathlib import Path

import numpy as np

from .audio import pcm_int16_to_float32

log = logging.getLogger(__name__)


class Transcriber:
    """Thin wrapper around pywhispercpp.Model with lazy loading."""

    def __init__(self, model_path: Path) -> None:
        self.model_path = Path(model_path)
        self._model = None  # pywhispercpp.Model
        self._lock = threading.Lock()

    def load(self) -> None:
        if self._model is not None:
            return
        if not self.model_path.exists():
            raise FileNotFoundError(f"Whisper model not found: {self.model_path}")
        try:
            from pywhispercpp.model import Model  # type: ignore
        except ImportError as exc:
            raise RuntimeError(
                "pywhispercpp is not installed. Install with `pip install pywhispercpp`."
            ) from exc
        log.info("Loading Whisper model: %s", self.model_path)
        # `print_realtime=False, print_progress=False` keep stdout clean.
        self._model = Model(str(self.model_path), print_realtime=False, print_progress=False)

    def unload(self) -> None:
        with self._lock:
            self._model = None

    def transcribe(
        self,
        pcm: bytes,
        language: str | None = None,
        translate: bool = False,
    ) -> str:
        """Transcribe PCM int16 mono @16kHz audio. Returns the joined text."""
        with self._lock:
            if self._model is None:
                self.load()
            samples: np.ndarray = pcm_int16_to_float32(pcm)
            log.debug("Transcribing %.2f s, language=%s", len(samples) / 16000, language)
            kwargs = {"translate": translate}
            if language and language != "auto":
                kwargs["language"] = language
            segments = self._model.transcribe(samples, **kwargs)  # type: ignore[union-attr]
            text = "".join(seg.text for seg in segments).strip()
            log.info("Transcribed %d chars", len(text))
            return text
