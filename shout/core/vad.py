"""Voice Activity Detection — webrtcvad-based silence detector.

Tracks trailing silence on a stream of 20 ms PCM int16 frames and signals
when N ms of continuous silence have elapsed.
"""
from __future__ import annotations

import logging

try:
    import webrtcvad  # type: ignore
    _HAVE_WEBRTC = True
except ImportError:  # pragma: no cover
    webrtcvad = None  # type: ignore
    _HAVE_WEBRTC = False

log = logging.getLogger(__name__)


class SilenceDetector:
    """Detect that the user has stopped speaking after some initial speech."""

    def __init__(
        self,
        sample_rate: int = 16000,
        aggressiveness: int = 2,
        silence_timeout_ms: int = 800,
        frame_ms: int = 20,
    ) -> None:
        if not _HAVE_WEBRTC:
            log.warning("webrtcvad not installed; SilenceDetector will be inert")
        self.sample_rate = sample_rate
        self.frame_ms = frame_ms
        self.silence_timeout_ms = silence_timeout_ms
        self._vad = webrtcvad.Vad(aggressiveness) if _HAVE_WEBRTC else None
        self._silence_ms = 0
        self._has_spoken = False

    def reset(self) -> None:
        self._silence_ms = 0
        self._has_spoken = False

    def push(self, frame: bytes) -> bool:
        """Feed a PCM int16 frame. Returns True when silence threshold reached.

        Always requires at least some speech before triggering, so a missing
        microphone or a moment of initial silence does not auto-stop.
        """
        if self._vad is None:
            return False
        try:
            is_speech = self._vad.is_speech(frame, self.sample_rate)
        except Exception:  # noqa: BLE001
            # Wrong frame size etc. — fail open (do not auto-stop)
            return False

        if is_speech:
            self._has_spoken = True
            self._silence_ms = 0
            return False

        if not self._has_spoken:
            return False

        self._silence_ms += self.frame_ms
        return self._silence_ms >= self.silence_timeout_ms
