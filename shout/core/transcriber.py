"""Whisper.cpp transcription — CUDA-accelerated CLI or in-process fallback.

Prefers the system whisper-cli binary (GPU via CUDA) when available.
Falls back to pywhispercpp (CPU-only, in-process) automatically.
"""
from __future__ import annotations

import logging
import os
import shutil
import struct
import subprocess
import tempfile
import threading
from pathlib import Path

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# CLI backend (CUDA / GPU)
# ---------------------------------------------------------------------------

_CLI_NAMES = ("whisper-cli", "whisper-cpp", "main")


def _find_cli() -> str | None:
    """Return path to whisper-cli if it's installed and actually loadable."""
    for name in _CLI_NAMES:
        path = shutil.which(name)
        if path:
            # Quick sanity: can the binary start at all (shared libs present)?
            try:
                r = subprocess.run(
                    [path, "--help"],
                    capture_output=True, timeout=5,
                )
                if r.returncode == 0:
                    return path
            except Exception:  # noqa: BLE001
                pass
    return None


class _CLITranscriber:
    """Calls whisper-cli subprocess for transcription (GPU-accelerated)."""

    def __init__(self, cli_path: str, model_path: Path) -> None:
        self.cli = cli_path
        self.model_path = model_path
        self._lock = threading.Lock()

    def load(self) -> None:
        if not self.model_path.exists():
            raise FileNotFoundError(f"Whisper model not found: {self.model_path}")
        log.info("Whisper CLI ready (%s), model: %s", self.cli, self.model_path)

    def unload(self) -> None:
        pass

    def transcribe(self, pcm: bytes, language: str | None = None, translate: bool = False) -> str:
        with self._lock:
            n_samples = len(pcm) // 2
            duration = n_samples / 16000
            log.debug("CLI transcribing %.2f s, language=%s", duration, language)

            with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as f:
                wav_path = f.name
                _write_wav(f, pcm)
                f.flush()
                try:
                    os.fsync(f.fileno())
                except OSError:
                    pass

            try:
                cmd = [
                    self.cli,
                    "-m", str(self.model_path),
                    "-f", wav_path,
                    "--no-timestamps", "-np",
                    "-t", "4",
                ]
                if language and language != "auto":
                    cmd += ["-l", language]
                if translate:
                    cmd += ["--translate"]

                result = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
                if result.returncode != 0:
                    log.error(
                        "whisper-cli FAILED rc=%s duration=%.2fs\n  stderr: %s",
                        result.returncode, duration,
                        (result.stderr or "").strip()[:500],
                    )
                    return ""

                text = result.stdout.strip()
                log.info("Transcribed %d chars in %.2fs (CLI/GPU)", len(text), duration)
                return text
            finally:
                Path(wav_path).unlink(missing_ok=True)


# ---------------------------------------------------------------------------
# In-process fallback (CPU via pywhispercpp)
# ---------------------------------------------------------------------------

class _PyWhisperTranscriber:
    """In-process whisper.cpp via pywhispercpp (CPU-only)."""

    def __init__(self, model_path: Path) -> None:
        self.model_path = model_path
        self._lock = threading.Lock()
        self._model = None

    def load(self) -> None:
        if not self.model_path.exists():
            raise FileNotFoundError(f"Whisper model not found: {self.model_path}")
        from pywhispercpp.model import Model as WhisperModel
        self._model = WhisperModel(str(self.model_path), print_progress=False, n_threads=4)
        log.info("Whisper model loaded (pywhispercpp/CPU): %s", self.model_path)

    def unload(self) -> None:
        self._model = None

    def transcribe(self, pcm: bytes, language: str | None = None, translate: bool = False) -> str:
        import numpy as np
        with self._lock:
            if self._model is None:
                self.load()
            n_samples = len(pcm) // 2
            duration = n_samples / 16000
            audio = np.frombuffer(pcm, dtype=np.int16).astype(np.float32) / 32768.0
            kwargs: dict = {}
            if language and language != "auto":
                kwargs["language"] = language
            if translate:
                kwargs["translate"] = True
            try:
                segments = self._model.transcribe(audio, **kwargs)
            except Exception:
                log.exception("pywhispercpp transcription failed")
                return ""
            text = " ".join(seg.text.strip() for seg in segments).strip()
            log.info("Transcribed %d chars in %.2fs (pywhispercpp/CPU)", len(text), duration)
            return text


# ---------------------------------------------------------------------------
# Public API — auto-selects the best backend
# ---------------------------------------------------------------------------

class Transcriber:
    """Unified transcriber: prefers CLI+CUDA, falls back to pywhispercpp/CPU."""

    def __init__(self, model_path: Path) -> None:
        self.model_path = Path(model_path)
        cli = _find_cli()
        if cli:
            log.info("Using whisper-cli backend (CUDA/GPU): %s", cli)
            self._backend = _CLITranscriber(cli, self.model_path)
        else:
            log.info("whisper-cli not available, falling back to pywhispercpp (CPU)")
            self._backend = _PyWhisperTranscriber(self.model_path)

    def load(self) -> None:
        self._backend.load()

    def unload(self) -> None:
        self._backend.unload()

    def transcribe(self, pcm: bytes, language: str | None = None, translate: bool = False) -> str:
        return self._backend.transcribe(pcm, language=language, translate=translate)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _write_wav(f, pcm: bytes) -> None:
    """Write raw PCM int16 mono 16kHz as a WAV file."""
    n_samples = len(pcm) // 2
    sample_rate = 16000
    bits_per_sample = 16
    num_channels = 1
    byte_rate = sample_rate * num_channels * bits_per_sample // 8
    block_align = num_channels * bits_per_sample // 8
    data_size = n_samples * block_align
    f.write(b"RIFF")
    f.write(struct.pack("<I", 36 + data_size))
    f.write(b"WAVE")
    f.write(b"fmt ")
    f.write(struct.pack("<I", 16))
    f.write(struct.pack("<HHIIHH", 1, num_channels, sample_rate, byte_rate, block_align, bits_per_sample))
    f.write(b"data")
    f.write(struct.pack("<I", data_size))
    f.write(pcm)

