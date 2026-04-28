"""Smoke test: every module imports cleanly without side effects on import."""
from __future__ import annotations

import importlib

MODULES = [
    "shout",
    "shout.paths",
    "shout.logging_setup",
    "shout.config",
    "shout.core.audio",
    "shout.core.vad",
    "shout.core.transcriber",
    "shout.core.models",
    "shout.core.postprocess",
    "shout.core.session",
    "shout.platform.session",
    "shout.platform.injector",
    "shout.platform.hotkey",
    "shout.platform.atspi",
    "shout.platform.autostart",
    # UI modules require Qt; tested separately when PySide6 is installed.
]


def test_imports():
    for name in MODULES:
        importlib.import_module(name)


def test_default_config_roundtrip(tmp_path):
    from shout import config as cfg_mod
    cfg = cfg_mod.Config()
    p = tmp_path / "c.toml"
    cfg_mod.save(cfg, p)
    loaded = cfg_mod.load(p)
    assert loaded.languages.enabled == cfg.languages.enabled
    assert loaded.hotkey.accelerator == cfg.hotkey.accelerator
    assert loaded.audio.sample_rate == cfg.audio.sample_rate
