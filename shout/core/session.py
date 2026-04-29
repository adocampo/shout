"""Dictation session state machine.

Coordinates audio capture, VAD, transcription, optional LLM post-processing
and text injection. Runs on a background thread; emits state changes via a
simple observer callback so the UI can subscribe (typically forwarded to a
Qt signal).
"""
from __future__ import annotations

import logging
import re
import threading
import time
from enum import Enum
from pathlib import Path
from typing import Callable

from ..config import Config
from .audio import AudioCapture
from .postprocess import postprocess
from .sounds import play_start, play_stop
from .transcriber import Transcriber
from .vad import SilenceDetector

log = logging.getLogger(__name__)

# Regex to detect Whisper hallucinations: text that is entirely composed of
# bracketed annotations like [silencio], [música], [Silbido], etc.
_HALLUCINATION_RE = re.compile(
    r"^\s*(\[[^\]]*\]\s*)+$"
)
# Common short hallucination phrases Whisper produces on silence/noise
_HALLUCINATION_PHRASES = {
    "you", "thank you", "thanks for watching",
    "gracias", "gracias por ver", "subtítulos",
    "subtítulos realizados por la comunidad de amara.org",
    "...", "…",
}


def _filter_hallucinations(text: str | None) -> str:
    """Return empty string if text looks like a Whisper hallucination."""
    if not text:
        return ""
    text = text.strip()
    if not text:
        return ""
    # Bracketed annotations: [silencio], [música], [silbido], etc.
    if _HALLUCINATION_RE.match(text):
        log.debug("Filtered hallucination (bracketed): %r", text)
        return ""
    # Known short hallucination phrases
    if text.lower().rstrip(".,!¡") in _HALLUCINATION_PHRASES:
        log.debug("Filtered hallucination (phrase): %r", text)
        return ""
    return text


def _has_speech_energy(pcm: bytes, threshold: float = 200.0) -> bool:
    """Check if PCM audio has enough RMS energy to contain speech."""
    import struct as _struct
    n_samples = len(pcm) // 2
    if n_samples == 0:
        return False
    samples = _struct.unpack(f"<{n_samples}h", pcm)
    rms = (sum(s * s for s in samples) / n_samples) ** 0.5
    return rms > threshold


_WORD_RE = re.compile(r"\w+|[^\w\s]", re.UNICODE)


def _strip_overlap(prev_text: str, new_text: str, max_words: int = 8) -> str:
    """Remove from the start of ``new_text`` any tokens that overlap the end of
    ``prev_text``. Used to dedupe transcription overlap when chunks share audio.

    Looks for the largest suffix of ``prev_text`` (up to ``max_words`` tokens,
    case-insensitive) that prefixes ``new_text``.
    """
    if not prev_text or not new_text:
        return new_text
    prev_tokens = [(m.group(), m.start(), m.end()) for m in _WORD_RE.finditer(prev_text)]
    new_tokens = [(m.group(), m.start(), m.end()) for m in _WORD_RE.finditer(new_text)]
    if not prev_tokens or not new_tokens:
        return new_text
    max_n = min(max_words, len(prev_tokens), len(new_tokens))
    for n in range(max_n, 0, -1):
        prev_tail = [t[0].lower() for t in prev_tokens[-n:]]
        new_head = [t[0].lower() for t in new_tokens[:n]]
        if prev_tail == new_head:
            cut = new_tokens[n - 1][2]  # end offset of last matched token
            return new_text[cut:].lstrip(" \t")
    return new_text


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
        self._pending_overlap_text: str = ""

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
            if self.cfg.audio.play_feedback_sounds:
                play_start()
            t = threading.Thread(target=self._run, name="shout-session", daemon=True)
            t.start()

    def stop(self) -> None:
        if self._state == State.RECORDING:
            if self.cfg.audio.play_feedback_sounds:
                play_stop()
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
        """Continuous dictation loop.

        Records until the user presses the hotkey again (stop_requested).
        Behaviour depends on the configured backend:
          - ``whisper_cpp``: chunked transcription on VAD silence / max-chunk.
          - ``streaming``: real-time decoding with LocalAgreement-2 commits.
        """
        if self.cfg.models.backend == "streaming":
            try:
                self._run_streaming()
                return
            except Exception:  # noqa: BLE001
                log.exception("Streaming backend failed; falling back to whisper_cpp")

        a = self.cfg.audio
        self._needs_separator = False
        self._pending_overlap_text = ""
        self._vad = SilenceDetector(
            sample_rate=a.sample_rate,
            aggressiveness=a.vad_aggressiveness,
            silence_timeout_ms=a.silence_timeout_ms,
        ) if a.vad_enabled else None

        silence_triggered = threading.Event()

        # Blanking period: ignore audio for the first ~300ms so the start beep
        # doesn't get picked up by the VAD as speech.
        blanking_frames = int(a.sample_rate * 0.3 / (320 / 1))  # ~15 frames at 20ms each
        frame_counter = [0]

        def on_frame(frame: bytes) -> None:
            frame_counter[0] += 1
            if frame_counter[0] <= blanking_frames:
                return  # Ignore beep period
            if self._vad and self._vad.push(frame):
                silence_triggered.set()

        self._capture = AudioCapture(
            sample_rate=a.sample_rate,
            device=a.input_device,
            on_frame=on_frame,
        )
        try:
            self._capture.start()
        except Exception as exc:  # noqa: BLE001
            log.exception("Audio start failed")
            self._set_state(State.ERROR, str(exc))
            self._set_state(State.IDLE, "")
            return

        try:
            # Wait for blanking period to pass (discard beep audio)
            time.sleep(0.35)
            self._capture.flush()  # Discard audio containing the start beep

            max_chunk_s = max(2.0, float(a.max_chunk_seconds))
            while not (self._stop_requested.is_set() or self._cancel.is_set()):
                # Wait for silence, max-chunk timeout, or stop
                forced_flush = False
                while not (
                    silence_triggered.is_set()
                    or self._stop_requested.is_set()
                    or self._cancel.is_set()
                ):
                    self._stop_requested.wait(0.02)
                    if self._capture.buffer_duration_seconds() >= max_chunk_s:
                        forced_flush = True
                        break

                if self._cancel.is_set():
                    break

                # Flush audio accumulated so far. On forced flushes (mid-speech)
                # we keep a small tail in the buffer so the cut word reappears
                # at the start of the next chunk; we then dedupe overlapping
                # text below.
                overlap_ms = a.forced_flush_overlap_ms if forced_flush else 0
                pcm = self._capture.flush(keep_trailing_ms=overlap_ms)

                if not pcm or len(pcm) < a.sample_rate * 2 * 0.5:
                    # Less than 500ms of audio — skip (probably just noise/silence)
                    silence_triggered.clear()
                    if self._vad and not forced_flush:
                        self._vad.reset()
                    continue

                # Transcribe this chunk
                self._set_state(State.TRANSCRIBING, "")
                language = self._language or self.cfg.languages.active
                if self.cfg.models.auto_detect_language:
                    language = "auto"
                text = self._transcriber.transcribe(pcm, language=language)

                if self.cfg.llm.enabled and text:
                    text = postprocess(text, self.cfg)

                # Filter Whisper hallucinations
                text = _filter_hallucinations(text)

                # Drop overlap that was already injected on the previous chunk
                # to avoid duplicated words at the cut boundary.
                if text and self._pending_overlap_text:
                    text = _strip_overlap(self._pending_overlap_text, text)

                if text and not self._cancel.is_set():
                    out = (" " + text) if self._needs_separator else text
                    self._set_state(State.INJECTING, out)
                    self.text_sink(out)
                    self._needs_separator = True

                # Remember the tail we just injected so the next chunk (which
                # contains the audio overlap) can dedupe it.
                self._pending_overlap_text = text if forced_flush else ""

                # Reset and keep recording
                silence_triggered.clear()
                if self._vad and not forced_flush:
                    # Real silence pause: reset speech-detection state.
                    self._vad.reset()
                elif self._vad and forced_flush:
                    # Forced cut while still speaking: only clear the trailing
                    # silence counter, keep _has_spoken=True so the next pause
                    # is detected immediately.
                    self._vad.reset_silence_only()

                if not self._stop_requested.is_set():
                    self._set_state(State.RECORDING, "")

        except Exception as exc:  # noqa: BLE001
            log.exception("Dictation session failed")
            self._set_state(State.ERROR, str(exc))
        finally:
            # Stop microphone and process any remaining audio
            remaining = self._capture.stop() if self._capture and self._capture.is_running else b""
            if (
                remaining
                and len(remaining) >= a.sample_rate * 2 * 0.5
                and not self._cancel.is_set()
            ):
                self._set_state(State.TRANSCRIBING, "")
                language = self._language or self.cfg.languages.active
                if self.cfg.models.auto_detect_language:
                    language = "auto"
                try:
                    text = self._transcriber.transcribe(remaining, language=language)
                    if self.cfg.llm.enabled and text:
                        text = postprocess(text, self.cfg)
                    text = _filter_hallucinations(text)
                    if text:
                        if self._needs_separator:
                            text = " " + text
                        self._set_state(State.INJECTING, text)
                        self.text_sink(text)
                except Exception:  # noqa: BLE001
                    log.exception("Final transcription failed")
            self._set_state(State.IDLE, "")

    # --- Streaming backend ---------------------------------------------------

    def _run_streaming(self) -> None:
        """Real-time loop using ``StreamingTranscriber`` (faster-whisper)."""
        from .streaming import StreamingTranscriber

        a = self.cfg.audio
        m = self.cfg.models
        language = self._language or self.cfg.languages.active
        if m.auto_detect_language:
            language = None

        self._needs_separator = False

        def _emit(text: str) -> None:
            if not text or self._cancel.is_set():
                return
            text = _filter_hallucinations(text)
            if not text:
                return
            out = (" " + text) if self._needs_separator else text
            self._set_state(State.INJECTING, out)
            try:
                self.text_sink(out)
            finally:
                self._needs_separator = True
                if not self._stop_requested.is_set():
                    self._set_state(State.RECORDING, "")

        st = StreamingTranscriber(
            model=m.streaming_model,
            language=language,
            device=m.streaming_device,
            compute_type=m.streaming_compute_type,
            sample_rate=a.sample_rate,
            step_ms=m.streaming_step_ms,
            min_chunk_ms=m.streaming_min_chunk_ms,
            on_commit=_emit,
        )
        st.start()

        # Blanking period: discard the start beep frames.
        blanking_frames = int(a.sample_rate * 0.3 / (320 / 1))
        frame_counter = [0]

        def on_frame(frame: bytes) -> None:
            frame_counter[0] += 1
            if frame_counter[0] <= blanking_frames:
                return
            st.feed(frame)

        self._capture = AudioCapture(
            sample_rate=a.sample_rate,
            device=a.input_device,
            on_frame=on_frame,
        )
        try:
            self._capture.start()
        except Exception as exc:  # noqa: BLE001
            log.exception("Audio start failed (streaming)")
            self._set_state(State.ERROR, str(exc))
            self._set_state(State.IDLE, "")
            st.finish()
            return

        try:
            time.sleep(0.35)  # let the beep pass
            while not (self._stop_requested.is_set() or self._cancel.is_set()):
                self._stop_requested.wait(0.05)
        except Exception as exc:  # noqa: BLE001
            log.exception("Streaming session failed")
            self._set_state(State.ERROR, str(exc))
        finally:
            try:
                if self._capture and self._capture.is_running:
                    self._capture.stop()
            except Exception:  # noqa: BLE001
                log.exception("Error stopping capture (streaming)")
            try:
                st.finish()
            except Exception:  # noqa: BLE001
                log.exception("Streaming finish failed")
            self._set_state(State.IDLE, "")

