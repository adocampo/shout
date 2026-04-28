"""Whisper.cpp GGML model registry, download and storage."""
from __future__ import annotations

import hashlib
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

import requests

from ..paths import models_dir

log = logging.getLogger(__name__)

# All models live in this Hugging Face repo, file naming follows whisper.cpp.
_HF_BASE = "https://huggingface.co/ggerganov/whisper.cpp/resolve/main"


@dataclass(frozen=True)
class ModelInfo:
    key: str               # short id used in config
    filename: str          # file name on disk and in the HF repo
    size_mb: int           # approximate
    description: str
    # SHA256 is optional; if present we verify after download.
    sha256: str | None = None

    @property
    def url(self) -> str:
        return f"{_HF_BASE}/{self.filename}"


# Curated list. More can be added without code changes.
AVAILABLE: list[ModelInfo] = [
    ModelInfo("tiny",         "ggml-tiny.bin",          75,  "Tiny multilingual"),
    ModelInfo("tiny-q5_1",    "ggml-tiny-q5_1.bin",     32,  "Tiny multilingual, quantized q5_1"),
    ModelInfo("base",         "ggml-base.bin",         142,  "Base multilingual"),
    ModelInfo("base-q5_1",    "ggml-base-q5_1.bin",     60,  "Base multilingual, quantized q5_1"),
    ModelInfo("small",        "ggml-small.bin",        466,  "Small multilingual"),
    ModelInfo("small-q5_1",   "ggml-small-q5_1.bin",   190,  "Small multilingual, quantized q5_1 (recommended default)"),
    ModelInfo("medium",       "ggml-medium.bin",      1500,  "Medium multilingual"),
    ModelInfo("medium-q5_0",  "ggml-medium-q5_0.bin",  539,  "Medium multilingual, quantized q5_0"),
    ModelInfo("large-v3",     "ggml-large-v3.bin",    3100,  "Large v3 multilingual (best quality)"),
    ModelInfo("large-v3-q5_0","ggml-large-v3-q5_0.bin",1080, "Large v3 multilingual, quantized q5_0"),
]


def by_key(key: str) -> ModelInfo | None:
    for m in AVAILABLE:
        if m.key == key:
            return m
    return None


def model_path(key: str) -> Path:
    info = by_key(key)
    if info is None:
        raise ValueError(f"Unknown model key: {key}")
    return models_dir() / info.filename


def is_downloaded(key: str) -> bool:
    return model_path(key).exists()


def installed_keys() -> list[str]:
    return [m.key for m in AVAILABLE if is_downloaded(m.key)]


def download(
    key: str,
    on_progress: Callable[[int, int], None] | None = None,
    chunk_size: int = 1 << 20,
) -> Path:
    """Download a model with optional progress callback (downloaded, total)."""
    info = by_key(key)
    if info is None:
        raise ValueError(f"Unknown model key: {key}")
    dest = model_path(key)
    if dest.exists():
        log.info("Model %s already present at %s", key, dest)
        return dest

    tmp = dest.with_suffix(dest.suffix + ".part")
    log.info("Downloading %s -> %s", info.url, dest)

    with requests.get(info.url, stream=True, timeout=60) as resp:
        resp.raise_for_status()
        total = int(resp.headers.get("Content-Length", 0))
        downloaded = 0
        sha = hashlib.sha256()
        with open(tmp, "wb") as f:
            for chunk in resp.iter_content(chunk_size=chunk_size):
                if not chunk:
                    continue
                f.write(chunk)
                sha.update(chunk)
                downloaded += len(chunk)
                if on_progress is not None:
                    on_progress(downloaded, total)

    if info.sha256 and sha.hexdigest() != info.sha256:
        tmp.unlink(missing_ok=True)
        raise RuntimeError(f"SHA256 mismatch for {info.filename}")

    tmp.replace(dest)
    log.info("Downloaded %s (%d bytes)", info.filename, downloaded)
    return dest


def remove(key: str) -> bool:
    p = model_path(key)
    if p.exists():
        p.unlink()
        log.info("Removed model %s", p)
        return True
    return False
