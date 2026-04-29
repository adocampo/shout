"""Whisper.cpp transcription via CLI subprocess."""
from __future__ import annotations

import logging
import shutil
import struct
import subprocess
import tempfile
import threading
from pathlib import Path

log = logging.getLogger(__name__)

# Possible binary names in PATH
_CLI_NAMES = ("whisper-cli", "whisper-cpp", "main")
_WHISPER_CLI: str | None = None


def _find_cli() -> str:
    """Locate the whisper-cli binary."""
    global _WHISPER_CLI
    if _WHISPER_CLI:
        return _WHISPER_CLI
    for name in _CLI_NAMES:
        path = shutil.which(name)
        if path:
            _WHISPER_CLI = path
            return path
    raise FileNotFoundError(
        "whisper-cli not found in PATH. Build whisper.cpp with CUDA and install the binary."
    )


class Transcriber:
    """Calls whisper-cli subprocess for transcription (GPU-accelerated)."""

    def __init__(self, model_path: Path) -> None:
        self.model_path = Path(model_path)
        self._lock = threading.Lock()

    def load(self) -> None:
        if not self.model_path.exists():
            raise FileNotFoundError(f"Whisper model not found: {self.model_path}")
        _find_cli()
        log.info("Whisper CLI ready, model: %s", self.model_path)

    def unload(self) -> None:
        pass

    def transcribe(
        self,
        pcm: bytes,
        language: str | None = None,
        translate: bool = False,
    ) -> str:
        """Transcribe PCM int16 mono @16kHz audio via whisper-cli."""
        with self._lock:
            cli = _find_cli()
            n_samples = len(pcm) // 2
            duration = n_samples / 16000
            log.debug("Transcribing %.2f s, language=%s", duration, language)

            # Write PCM as WAV to a temp file
            with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as f:
                wav_path = f.name
                self._write_wav(f, pcm)

            try:
                cmd = [
                    cli,
                    "-m", str(self.model_path),
                    "-f", wav_path,
                    "--no-timestamps",
                    "-np",
                    "-t", "4",
                    "-fa",
                ]
                if language and language != "auto":
                    cmd += ["-l", language]
                if translate:
                    cmd += ["--translate"]

                result = subprocess.run(
                    cmd,
                    capture_output=True,
                    text=True,
                    timeout=30,
                )
                if result.returncode != 0:
                    log.error("whisper-cli error: %s", result.stderr[:500])
                    return ""

                text = result.stdout.strip()
                log.info("Transcribed %d chars", len(text))
                return text
            finally:
                Path(wav_path).unlink(missing_ok=True)

    @staticmethod
    def _write_wav(f, pcm: bytes) -> None:
        """Write raw PCM int16 mono 16kHz as a WAV file."""
        n_samples = len(pcm) // 2
        sample_rate = 16000
        bits_per_sample = 16
        num_channels = 1
        byte_rate = sample_rate * num_channels * bits_per_sample // 8
        block_align = num_channels * bits_per_sample // 8
        data_size = n_samples * block_align
        # RIFF header
        f.write(b"RIFF")
        f.write(struct.pack("<I", 36 + data_size))
        f.write(b"WAVE")
        # fmt chunk
        f.write(b"fmt ")
        f.write(struct.pack("<I", 16))
        f.write(struct.pack("<HHIIHH", 1, num_channels, sample_rate, byte_rate, block_align, bits_per_sample))
        # data chunk
        f.write(b"data")
        f.write(struct.pack("<I", data_size))
        f.write(pcm)

