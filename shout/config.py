"""Persistent configuration (TOML)."""
from __future__ import annotations

import logging
import sys
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

import tomli_w

if sys.version_info >= (3, 11):
    import tomllib
else:  # pragma: no cover
    import tomli as tomllib  # type: ignore

from .paths import config_file

log = logging.getLogger(__name__)


# --- Dataclasses --------------------------------------------------------------

@dataclass
class HotkeyConfig:
    # Human-readable accelerator. Backends translate it.
    accelerator: str = "Ctrl+Alt+Space"
    # "toggle" or "push_to_talk"
    mode: str = "toggle"


@dataclass
class AudioConfig:
    input_device: str | None = None  # None = system default
    sample_rate: int = 16000
    channels: int = 1
    vad_enabled: bool = True
    # Aggressiveness 0-3 for webrtcvad
    vad_aggressiveness: int = 2
    # Stop recording after this many ms of trailing silence
    silence_timeout_ms: int = 800
    # Hard cap to avoid runaway sessions
    max_recording_seconds: int = 120
    play_feedback_sounds: bool = True


@dataclass
class ModelsConfig:
    selected: str = "small-q5_1"
    # Auto-detect language. If False, use the active language from LanguagesConfig.
    auto_detect_language: bool = False


@dataclass
class LanguagesConfig:
    # ISO codes (e.g. "es", "en"). First is the default.
    enabled: list[str] = field(default_factory=lambda: ["es", "en"])
    active: str = "es"


@dataclass
class InjectionConfig:
    # "auto" | "wtype" | "xdotool" | "clipboard"
    backend: str = "auto"
    # If text length exceeds this threshold use clipboard+paste even if direct typing is available.
    clipboard_threshold: int = 200


@dataclass
class BadgeConfig:
    enabled: bool = True
    # "auto" | "cursor" | "corner"
    placement: str = "auto"
    # Used when placement falls back to "corner": tl, tr, bl, br
    corner: str = "br"
    # Pixel offset from corner / cursor
    offset_x: int = 16
    offset_y: int = 16


@dataclass
class TrayConfig:
    enabled: bool = True


@dataclass
class GeneralConfig:
    autostart: bool = False


@dataclass
class LLMConfig:
    enabled: bool = False
    # OpenAI-compatible endpoint base URL (Ollama: http://localhost:11434/v1)
    base_url: str = "http://localhost:11434/v1"
    api_key: str = ""
    model: str = "llama3.2:3b-instruct"
    prompt: str = (
        "Reescribe el siguiente texto dictado para que sea claro y bien puntuado. "
        "Conserva el idioma y el significado. No añadas comentarios."
    )
    timeout_seconds: int = 30


@dataclass
class Config:
    general: GeneralConfig = field(default_factory=GeneralConfig)
    hotkey: HotkeyConfig = field(default_factory=HotkeyConfig)
    audio: AudioConfig = field(default_factory=AudioConfig)
    languages: LanguagesConfig = field(default_factory=LanguagesConfig)
    models: ModelsConfig = field(default_factory=ModelsConfig)
    injection: InjectionConfig = field(default_factory=InjectionConfig)
    badge: BadgeConfig = field(default_factory=BadgeConfig)
    tray: TrayConfig = field(default_factory=TrayConfig)
    llm: LLMConfig = field(default_factory=LLMConfig)


# --- Load / save -------------------------------------------------------------

def _merge(dc: Any, data: dict) -> Any:
    """Merge a dict into an existing dataclass instance, in place, recursively."""
    for f_name, f_value in dc.__dataclass_fields__.items():
        if f_name not in data:
            continue
        new_value = data[f_name]
        current = getattr(dc, f_name)
        if hasattr(current, "__dataclass_fields__") and isinstance(new_value, dict):
            _merge(current, new_value)
        else:
            setattr(dc, f_name, new_value)
    return dc


def load(path: Path | None = None) -> Config:
    cfg = Config()
    p = path or config_file()
    if not p.exists():
        log.info("No config file at %s, using defaults", p)
        return cfg
    try:
        with open(p, "rb") as f:
            data = tomllib.load(f)
        _merge(cfg, data)
        log.info("Loaded config from %s", p)
    except (OSError, tomllib.TOMLDecodeError) as exc:
        log.error("Failed to read config %s: %s — using defaults", p, exc)
    return cfg


def _strip_none(value: Any) -> Any:
    """Recursively drop keys whose value is None — TOML cannot represent null."""
    if isinstance(value, dict):
        return {k: _strip_none(v) for k, v in value.items() if v is not None}
    if isinstance(value, list):
        return [_strip_none(v) for v in value if v is not None]
    return value


def save(cfg: Config, path: Path | None = None) -> None:
    p = path or config_file()
    p.parent.mkdir(parents=True, exist_ok=True)
    data = _strip_none(asdict(cfg))
    tmp = p.with_suffix(p.suffix + ".tmp")
    with open(tmp, "wb") as f:
        tomli_w.dump(data, f)
    tmp.replace(p)
    log.info("Saved config to %s", p)
