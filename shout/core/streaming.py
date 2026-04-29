"""Real-time streaming transcription via faster-whisper + LocalAgreement-2.

Unlike :mod:`shout.core.transcriber` (which spawns one ``whisper-cli`` process
per chunk), this backend keeps a single faster-whisper model loaded in GPU
memory and is fed audio incrementally. Every ``step_ms`` it re-decodes the
sliding audio window and emits the *confirmed* prefix using the
LocalAgreement-2 strategy: a token is committed only when two consecutive
hypotheses agree on it.

This produces a "writes as you speak" experience with ~0.5\u20131.5 s end-to-end
latency on a modern NVIDIA GPU, while keeping Whisper's accuracy and
multilingual support.

Optional dependency: ``pip install faster-whisper``.
"""
from __future__ import annotations

import logging
import threading
import time
from collections import deque
from typing import Callable

import numpy as np

log = logging.getLogger(__name__)


def _pcm_int16_to_float32(pcm: bytes) -> np.ndarray:
    return np.frombuffer(pcm, dtype=np.int16).astype(np.float32) / 32768.0


def _common_prefix(a: list[str], b: list[str]) -> int:
    """Length of the longest common (case-insensitive) word prefix."""
    n = 0
    for x, y in zip(a, b):
        if x.lower() == y.lower():
            n += 1
        else:
            break
    return n


class StreamingTranscriber:
    """Continuously transcribe a live PCM int16 mono stream.

    Usage:
        st = StreamingTranscriber(model="small", language="es",
                                  on_commit=lambda txt: ...)
        st.start()
        st.feed(pcm_bytes)   # repeatedly, from the audio callback
        ...
        final = st.finish()  # flush remaining audio, returns last committed text
    """

    def __init__(
        self,
        model: str = "small",
        language: str | None = "es",
        device: str = "auto",
        compute_type: str = "int8_float16",
        sample_rate: int = 16000,
        step_ms: int = 500,
        min_chunk_ms: int = 1000,
        max_window_seconds: float = 25.0,
        on_commit: Callable[[str], None] | None = None,
    ) -> None:
        try:
            from faster_whisper import WhisperModel  # type: ignore
        except ImportError as exc:  # noqa: BLE001
            raise RuntimeError(
                "faster-whisper is not installed. Run: "
                "pip install faster-whisper"
            ) from exc

        if device == "auto":
            try:
                import torch  # type: ignore

                device = "cuda" if torch.cuda.is_available() else "cpu"
            except ImportError:
                device = "cpu"
        if device == "cpu" and compute_type in {"int8_float16", "float16"}:
            compute_type = "int8"

        log.info(
            "Loading faster-whisper model=%s device=%s compute=%s",
            model, device, compute_type,
        )
        self._model = WhisperModel(model, device=device, compute_type=compute_type)
        self.language = language
        self.sample_rate = sample_rate
        self.step_ms = step_ms
        self.min_chunk_ms = min_chunk_ms
        self.max_window_seconds = max_window_seconds
        self.on_commit = on_commit

        self._audio_buffer = bytearray()  # raw PCM int16 mono
        self._buffer_lock = threading.Lock()
        self._committed_words: list[str] = []
        self._last_hypothesis: list[str] = []
        self._committed_audio_offset_samples = 0  # samples already permanently flushed

        self._stop = threading.Event()
        self._loop_thread: threading.Thread | None = None

    # --- Public API ----------------------------------------------------------

    def start(self) -> None:
        if self._loop_thread is not None:
            return
        self._stop.clear()
        self._committed_words.clear()
        self._last_hypothesis.clear()
        self._audio_buffer.clear()
        self._committed_audio_offset_samples = 0
        self._loop_thread = threading.Thread(
            target=self._loop, name="shout-streaming", daemon=True
        )
        self._loop_thread.start()

    def feed(self, pcm: bytes) -> None:
        """Append raw PCM int16 mono audio to the streaming buffer."""
        if not pcm:
            return
        with self._buffer_lock:
            self._audio_buffer.extend(pcm)

    def finish(self) -> str:
        """Stop the worker, flush the remaining audio, return last committed text."""
        self._stop.set()
        if self._loop_thread is not None:
            self._loop_thread.join(timeout=10.0)
            self._loop_thread = None
        # Final pass: commit whatever's still in the buffer (no agreement needed).
        with self._buffer_lock:
            tail = bytes(self._audio_buffer)
            self._audio_buffer.clear()
        if len(tail) >= self.sample_rate * 2 * 0.3:
            audio = _pcm_int16_to_float32(tail)
            text = self._decode(audio)
            if text:
                new_words = text.split()
                committed_text = " ".join(new_words).strip()
                if committed_text and self.on_commit is not None:
                    self.on_commit(committed_text)
                self._committed_words.extend(new_words)
        return " ".join(self._committed_words).strip()

    # --- Worker loop ---------------------------------------------------------

    def _loop(self) -> None:
        last_decode = 0.0
        min_chunk_samples = int(self.sample_rate * self.min_chunk_ms / 1000)
        while not self._stop.is_set():
            time.sleep(self.step_ms / 1000.0)
            now = time.monotonic()
            if now - last_decode < self.step_ms / 1000.0:
                continue
            last_decode = now

            with self._buffer_lock:
                if len(self._audio_buffer) < min_chunk_samples * 2:
                    continue
                window_pcm = bytes(self._audio_buffer)

            try:
                audio = _pcm_int16_to_float32(window_pcm)
                # Cap the decoding window to avoid drifting past Whisper's 30 s ctx.
                max_samples = int(self.max_window_seconds * self.sample_rate)
                if audio.size > max_samples:
                    audio = audio[-max_samples:]
                hypothesis_text = self._decode(audio)
            except Exception:  # noqa: BLE001
                log.exception("Streaming decode failed")
                continue

            new_words = hypothesis_text.split()
            if not new_words:
                continue

            # LocalAgreement-2: commit the longest common prefix of the new
            # hypothesis and the previous one.
            agree = _common_prefix(new_words, self._last_hypothesis)
            self._last_hypothesis = new_words

            if agree <= 0:
                continue

            committed_now = new_words[:agree]
            # New committed words are those beyond what we have already committed.
            already = len(self._committed_words)
            actual_new = committed_now[already:]
            if not actual_new:
                continue

            self._committed_words.extend(actual_new)
            text_out = " ".join(actual_new)
            if self.on_commit is not None:
                try:
                    self.on_commit(text_out)
                except Exception:  # noqa: BLE001
                    log.exception("on_commit failed")

            # Trim buffer: once we've committed N words, drop the audio that
            # corresponds to roughly that many words so the next decode window
            # stays bounded. We approximate by trimming whenever buffer exceeds
            # max_window_seconds.
            with self._buffer_lock:
                max_bytes = int(self.max_window_seconds * self.sample_rate * 2)
                if len(self._audio_buffer) > max_bytes:
                    drop = len(self._audio_buffer) - max_bytes
                    del self._audio_buffer[:drop]

    def _decode(self, audio: np.ndarray) -> str:
        segments, _info = self._model.transcribe(
            audio,
            language=self.language if self.language and self.language != "auto" else None,
            beam_size=1,
            vad_filter=False,
            condition_on_previous_text=False,
            without_timestamps=True,
        )
        return " ".join(s.text.strip() for s in segments).strip()
