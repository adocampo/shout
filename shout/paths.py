"""Common path helpers (XDG Base Directory)."""
from __future__ import annotations

import os
from pathlib import Path

APP_NAME = "shout"


def _xdg(env_var: str, default_subdir: str) -> Path:
    base = os.environ.get(env_var)
    if base:
        return Path(base)
    return Path.home() / default_subdir


def config_dir() -> Path:
    p = _xdg("XDG_CONFIG_HOME", ".config") / APP_NAME
    p.mkdir(parents=True, exist_ok=True)
    return p


def data_dir() -> Path:
    p = _xdg("XDG_DATA_HOME", ".local/share") / APP_NAME
    p.mkdir(parents=True, exist_ok=True)
    return p


def state_dir() -> Path:
    p = _xdg("XDG_STATE_HOME", ".local/state") / APP_NAME
    p.mkdir(parents=True, exist_ok=True)
    return p


def cache_dir() -> Path:
    p = _xdg("XDG_CACHE_HOME", ".cache") / APP_NAME
    p.mkdir(parents=True, exist_ok=True)
    return p


def models_dir() -> Path:
    p = data_dir() / "models"
    p.mkdir(parents=True, exist_ok=True)
    return p


def config_file() -> Path:
    return config_dir() / "config.toml"


def log_file() -> Path:
    return state_dir() / "shout.log"
