"""Microphone capture (PipeWire/PulseAudio via sounddevice)."""
from __future__ import annotations

import logging
import threading
from collections import deque
from typing import Callable

import numpy as np
import sounddevice as sd

log = logging.getLogger(__name__)

# Frame size used for streaming (20 ms @ 16 kHz = 320 samples). Aligns with WebRTC VAD.
FRAME_MS = 20


class AudioCapture:
    """Capture mono PCM int16 from the system microphone.

    Streams 20 ms frames to an optional callback (used by VAD) and accumulates
    them internally; `stop()` returns the full PCM buffer.
    """

    def __init__(
        self,
        sample_rate: int = 16000,
        device: str | int | None = None,
        on_frame: Callable[[bytes], None] | None = None,
    ) -> None:
        self.sample_rate = sample_rate
        self.device = device
        self.on_frame = on_frame
        self._frame_samples = int(sample_rate * FRAME_MS / 1000)
        self._buffer: deque[bytes] = deque()
        self._stream: sd.RawInputStream | None = None
        self._lock = threading.Lock()
        self._running = False

    # --- Lifecycle -----------------------------------------------------------

    def start(self) -> None:
        if self._running:
            return
        log.info(
            "Starting audio capture rate=%d device=%r frame=%d samples",
            self.sample_rate, self.device, self._frame_samples,
        )
        self._buffer.clear()
        self._stream = sd.RawInputStream(
            samplerate=self.sample_rate,
            blocksize=self._frame_samples,
            dtype="int16",
            channels=1,
            device=self.device,
            callback=self._callback,
        )
        self._stream.start()
        self._running = True

    def stop(self) -> bytes:
        if not self._running:
            return b""
        log.info("Stopping audio capture")
        try:
            self._stream.stop()  # type: ignore[union-attr]
            self._stream.close()  # type: ignore[union-attr]
        finally:
            self._stream = None
            self._running = False
        with self._lock:
            data = b"".join(self._buffer)
            self._buffer.clear()
        log.info("Captured %d bytes (%.2f s)", len(data), len(data) / 2 / self.sample_rate)
        return data

    @property
    def is_running(self) -> bool:
        return self._running

    # --- Internals -----------------------------------------------------------

    def _callback(self, indata, frames, time, status) -> None:  # noqa: D401
        if status:
            log.warning("Audio status: %s", status)
        # `indata` is a CFFI buffer of int16 mono; copy to bytes for thread safety.
        chunk = bytes(indata)
        with self._lock:
            self._buffer.append(chunk)
        if self.on_frame is not None:
            try:
                self.on_frame(chunk)
            except Exception:  # noqa: BLE001
                log.exception("on_frame callback failed")


def list_input_devices() -> list[dict]:
    """Return a list of available input devices (id, name, default_samplerate)."""
    out: list[dict] = []
    for idx, dev in enumerate(sd.query_devices()):
        if dev.get("max_input_channels", 0) <= 0:
            continue
        out.append({
            "id": idx,
            "name": dev.get("name", f"device-{idx}"),
            "default_samplerate": dev.get("default_samplerate"),
        })
    return out


def pcm_int16_to_float32(pcm: bytes) -> np.ndarray:
    """Convert little-endian PCM int16 bytes to float32 in [-1, 1]."""
    arr = np.frombuffer(pcm, dtype=np.int16).astype(np.float32)
    return arr / 32768.0
