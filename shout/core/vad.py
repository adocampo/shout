"""Voice Activity Detection — webrtcvad + RMS energy fallback.

Tracks trailing silence on a stream of 20 ms PCM int16 frames and signals
when N ms of continuous silence have elapsed.
"""
from __future__ import annotations

import logging
import struct

try:
    import webrtcvad  # type: ignore
    _HAVE_WEBRTC = True
except (ImportError, ModuleNotFoundError):
    webrtcvad = None  # type: ignore
    _HAVE_WEBRTC = False

log = logging.getLogger(__name__)


class SilenceDetector:
    """Detect that the user has stopped speaking after some initial speech.

    Uses webrtcvad when available, otherwise falls back to an RMS energy
    threshold detector.
    """

    def __init__(
        self,
        sample_rate: int = 16000,
        aggressiveness: int = 2,
        silence_timeout_ms: int = 2000,
        frame_ms: int = 20,
        rms_threshold: float = 60.0,
    ) -> None:
        self.sample_rate = sample_rate
        self.frame_ms = frame_ms
        self.silence_timeout_ms = silence_timeout_ms
        self._rms_threshold = rms_threshold

        if _HAVE_WEBRTC:
            try:
                self._vad = webrtcvad.Vad(aggressiveness)
                log.info("VAD: using webrtcvad (aggressiveness=%d)", aggressiveness)
            except Exception:  # noqa: BLE001
                self._vad = None
                log.info("VAD: webrtcvad init failed, using RMS energy fallback")
        else:
            self._vad = None
            log.info("VAD: webrtcvad unavailable, using RMS energy fallback (threshold=%.0f)", rms_threshold)

        self._silence_ms = 0
        self._has_spoken = False

    def reset(self) -> None:
        self._silence_ms = 0
        self._has_spoken = False

    def reset_silence_only(self) -> None:
        """Reset only the trailing silence counter, keep speech-detected state.

        Used when we force-flush mid-speech (max chunk reached) and want the
        next real pause to trigger immediately without re-arming on speech.
        """
        self._silence_ms = 0

    def push(self, frame: bytes) -> bool:
        """Feed a PCM int16 frame. Returns True when silence threshold reached.

        Always requires at least some speech before triggering, so a missing
        microphone or a moment of initial silence does not auto-stop.
        """
        is_speech = self._detect_speech(frame)

        if is_speech:
            self._has_spoken = True
            self._silence_ms = 0
            return False

        if not self._has_spoken:
            return False

        self._silence_ms += self.frame_ms
        return self._silence_ms >= self.silence_timeout_ms

    def _detect_speech(self, frame: bytes) -> bool:
        """Determine if frame contains speech."""
        if self._vad is not None:
            try:
                return self._vad.is_speech(frame, self.sample_rate)
            except Exception:  # noqa: BLE001
                pass  # fall through to RMS

        # RMS energy fallback
        n_samples = len(frame) // 2
        if n_samples == 0:
            return False
        samples = struct.unpack(f"<{n_samples}h", frame)
        rms = (sum(s * s for s in samples) / n_samples) ** 0.5
        return rms > self._rms_threshold
