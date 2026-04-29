"""Feedback sounds for dictation start/stop."""
from __future__ import annotations

import logging
import threading
import wave
from pathlib import Path

import sounddevice as sd
import numpy as np

log = logging.getLogger(__name__)

_RESOURCES = Path(__file__).resolve().parent.parent / "resources"


def _play_wav(path: Path) -> None:
    """Play a short WAV file asynchronously (non-blocking)."""
    try:
        with wave.open(str(path), "rb") as wf:
            rate = wf.getframerate()
            frames = wf.readframes(wf.getnframes())
            dtype = "<i2"  # 16-bit signed LE
            data = np.frombuffer(frames, dtype=dtype).astype(np.float32) / 32768.0
            sd.play(data, samplerate=rate)
    except Exception:  # noqa: BLE001
        log.debug("Could not play sound %s", path, exc_info=True)


def play_start() -> None:
    """Play the 'recording started' chirp."""
    threading.Thread(
        target=_play_wav,
        args=(_RESOURCES / "start.wav",),
        daemon=True,
    ).start()


def play_stop() -> None:
    """Play the 'recording stopped' blip."""
    threading.Thread(
        target=_play_wav,
        args=(_RESOURCES / "stop.wav",),
        daemon=True,
    ).start()
