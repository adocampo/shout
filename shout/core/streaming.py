"""Real-time streaming transcription via faster-whisper + LocalAgreement-2.

Unlike :mod:`shout.core.transcriber` (which spawns one ``whisper-cli`` process
per chunk), this backend keeps a single faster-whisper model loaded in GPU
memory and is fed audio incrementally. Every ``step_ms`` it re-decodes the
*uncommitted* audio window and emits the prefix that two consecutive
hypotheses agree on (LocalAgreement-2). Word-level timestamps are then used
to physically discard committed audio from the buffer, keeping the decode
window bounded and the throughput stable in real-time.

Optional dependency: ``pip install faster-whisper``.
"""
from __future__ import annotations

import logging
import os
import threading
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


def _preload_nvidia_libs() -> bool:
    """Pre-load CUDA runtime libs shipped as `nvidia-*` wheels.

    faster-whisper / CTranslate2 expect libcublas.so.12 + libcudnn*.so.9
    in the dynamic loader path. PyPI wheels install them under
    ``site-packages/nvidia/<pkg>/lib`` but do *not* register them with the
    loader, so a normal ``import faster_whisper`` cannot find them.
    Loading them via absolute path with ``ctypes.CDLL(..., RTLD_GLOBAL)``
    publishes the symbols process-wide so subsequent dlopens of the
    short-name (e.g. ``libcublas.so.12``) succeed.
    """
    import ctypes

    try:
        import nvidia  # type: ignore
    except ImportError:
        return False
    # cuDNN has internal cross-deps; load 'ops' and 'graph' first.
    preferred_order = [
        "cudnn/lib/libcudnn_graph.so.9",
        "cudnn/lib/libcudnn_ops.so.9",
        "cudnn/lib/libcudnn_cnn.so.9",
        "cudnn/lib/libcudnn.so.9",
        "cublas/lib/libcublasLt.so.12",
        "cublas/lib/libcublas.so.12",
    ]
    loaded_any = False
    for root in nvidia.__path__:
        for rel in preferred_order:
            full = os.path.join(root, rel)
            if not os.path.isfile(full):
                continue
            try:
                ctypes.CDLL(full, mode=ctypes.RTLD_GLOBAL)
                loaded_any = True
            except OSError as exc:  # noqa: BLE001
                log.debug("Could not preload %s: %s", full, exc)
    return loaded_any


def _cuda_runtime_loadable() -> bool:
    """Return True only if the CUDA runtime libs CTranslate2 needs are loadable."""
    import ctypes

    # First chance: try the nvidia-* wheels (silent if not present).
    _preload_nvidia_libs()
    for lib in ("libcublas.so.12", "libcudnn_ops.so.9"):
        try:
            ctypes.CDLL(lib)
        except OSError:
            log.warning(
                "CUDA runtime library %s is not loadable. Install the bundled "
                "wheels into the venv: `pip install nvidia-cublas-cu12 "
                "nvidia-cudnn-cu12`. Falling back to CPU for now.",
                lib,
            )
            return False
    return True


def _detect_device(preferred: str) -> str:
    """Resolve ``preferred`` ('auto'|'cuda'|'cpu') to an actual device."""
    if preferred == "cpu":
        return "cpu"
    has_cuda = False
    try:
        import ctranslate2  # type: ignore

        has_cuda = ctranslate2.get_cuda_device_count() > 0
    except Exception:  # noqa: BLE001
        has_cuda = False
    if not has_cuda:
        try:
            import torch  # type: ignore

            has_cuda = torch.cuda.is_available()
        except ImportError:
            has_cuda = False
    if preferred == "cuda":
        # Trust the user but still warn if libs are missing; CTranslate2 will
        # error later anyway.
        return "cuda"
    if has_cuda and _cuda_runtime_loadable():
        return "cuda"
    return "cpu"


class StreamingTranscriber:
    """Continuously transcribe a live PCM int16 mono stream.

    Usage::

        st = StreamingTranscriber(model="small", language="es",
                                  on_commit=lambda txt: ...)
        st.start()
        st.feed(pcm_bytes)   # repeatedly, from the audio callback
        ...
        final = st.finish()  # flush remaining audio, returns committed text
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
        max_window_seconds: float = 20.0,
        on_commit: Callable[[str], None] | None = None,
    ) -> None:
        try:
            from faster_whisper import WhisperModel  # type: ignore
        except ImportError as exc:  # noqa: BLE001
            raise RuntimeError(
                "faster-whisper is not installed. Run: "
                "pip install faster-whisper"
            ) from exc

        device = _detect_device(device)
        if device == "cpu":
            if compute_type in {"int8_float16", "float16"}:
                compute_type = "int8"
            log.warning(
                "Streaming backend running on CPU \u2014 expect 1\u20132 s "
                "decode per step. Install CUDA-enabled ctranslate2 for "
                "real-time performance, or use the chunked `whisper_cpp` "
                "backend."
            )

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

        # Audio buffer holds only the *uncommitted* tail of speech; committed
        # audio is discarded as soon as we know its end-timestamp.
        self._audio_buffer = bytearray()
        self._buffer_lock = threading.Lock()
        # Hypothesis from the previous iteration (for LocalAgreement-2).
        self._last_hypothesis_words: list[str] = []
        # Words already emitted to ``on_commit`` (across the whole session).
        self._committed_words: list[str] = []

        self._stop = threading.Event()
        self._loop_thread: threading.Thread | None = None

    # --- Public API ----------------------------------------------------------

    def start(self) -> None:
        if self._loop_thread is not None:
            return
        self._stop.clear()
        with self._buffer_lock:
            self._audio_buffer.clear()
        self._last_hypothesis_words.clear()
        self._committed_words.clear()
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
        """Stop the worker, flush the remaining audio, return committed text."""
        self._stop.set()
        if self._loop_thread is not None:
            self._loop_thread.join(timeout=15.0)
            self._loop_thread = None
        with self._buffer_lock:
            tail = bytes(self._audio_buffer)
            self._audio_buffer.clear()
        if len(tail) >= self.sample_rate * 2 * 0.3:
            try:
                audio = _pcm_int16_to_float32(tail)
                words = self._decode_words(audio)
                new_words = [w["word"].strip() for w in words if w["word"].strip()]
                if new_words:
                    text_out = " ".join(new_words).strip()
                    self._committed_words.extend(new_words)
                    if self.on_commit is not None and text_out:
                        try:
                            self.on_commit(text_out)
                        except Exception:  # noqa: BLE001
                            log.exception("on_commit failed (final)")
            except Exception:  # noqa: BLE001
                log.exception("Final streaming decode failed")
        return " ".join(self._committed_words).strip()

    # --- Worker loop ---------------------------------------------------------

    def _loop(self) -> None:
        min_chunk_bytes = int(self.sample_rate * self.min_chunk_ms / 1000) * 2
        step_s = self.step_ms / 1000.0
        while not self._stop.is_set():
            self._stop.wait(step_s)
            if self._stop.is_set():
                break
            with self._buffer_lock:
                if len(self._audio_buffer) < min_chunk_bytes:
                    continue
                window_pcm = bytes(self._audio_buffer)
                buffer_len_at_decode = len(self._audio_buffer)

            audio = _pcm_int16_to_float32(window_pcm)
            # Cap to avoid drifting past Whisper's 30 s context.
            max_samples = int(self.max_window_seconds * self.sample_rate)
            if audio.size > max_samples:
                audio = audio[-max_samples:]

            try:
                words = self._decode_words(audio)
            except Exception:  # noqa: BLE001
                log.exception("Streaming decode failed")
                continue

            new_word_objs = [w for w in words if w["word"].strip()]
            new_word_strs = [w["word"].strip() for w in new_word_objs]
            if not new_word_strs:
                self._last_hypothesis_words = []
                continue

            # LocalAgreement-2 with the previous hypothesis.
            agree = _common_prefix(new_word_strs, self._last_hypothesis_words)
            self._last_hypothesis_words = new_word_strs

            if agree <= 0:
                continue

            committed_now_objs = new_word_objs[:agree]
            committed_now_strs = new_word_strs[:agree]
            text_out = " ".join(committed_now_strs).strip()
            if not text_out:
                continue

            self._committed_words.extend(committed_now_strs)
            if self.on_commit is not None:
                try:
                    self.on_commit(text_out)
                except Exception:  # noqa: BLE001
                    log.exception("on_commit failed")

            # ---- Trim the buffer up to the end of the last committed word ----
            # `last_end` is in seconds, relative to the start of `audio` (which
            # is the tail of the buffer when capped).
            last_end = float(committed_now_objs[-1]["end"])
            cut_bytes_in_decoded = int(last_end * self.sample_rate) * 2
            decoded_byte_len = audio.size * 2
            with self._buffer_lock:
                # Translate cut offset (relative to decoded window) into an
                # absolute offset inside the live buffer, accounting for any
                # bytes that may have been appended while we were decoding.
                tail_offset = max(0, buffer_len_at_decode - decoded_byte_len)
                absolute_cut = tail_offset + cut_bytes_in_decoded
                if 0 < absolute_cut <= len(self._audio_buffer):
                    del self._audio_buffer[:absolute_cut]
            # After committing we drop the agreement memory because the next
            # decode will operate on a freshly trimmed buffer; the previous
            # hypothesis no longer corresponds to the same audio offsets.
            self._last_hypothesis_words = []

    def _decode_words(self, audio: np.ndarray) -> list[dict]:
        segments, _info = self._model.transcribe(
            audio,
            language=self.language if self.language and self.language != "auto" else None,
            beam_size=1,
            vad_filter=False,
            condition_on_previous_text=False,
            word_timestamps=True,
        )
        out: list[dict] = []
        for s in segments:
            if not s.words:
                continue
            for w in s.words:
                out.append({"word": w.word, "start": w.start, "end": w.end})
        return out
