"""Resource path helpers."""
from __future__ import annotations

from importlib.resources import files
from pathlib import Path


def resource_path(name: str) -> Path:
    return Path(str(files("shout.resources").joinpath(name)))


def icon_for_state(state: str) -> Path:
    mapping = {
        "idle": "mic-idle.svg",
        "recording": "mic-recording.svg",
        "transcribing": "mic-transcribing.svg",
        "injecting": "mic-transcribing.svg",
        "error": "mic-idle.svg",
    }
    return resource_path(mapping.get(state, "mic-idle.svg"))
