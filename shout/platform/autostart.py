"""XDG autostart .desktop file management."""
from __future__ import annotations

import logging
import os
import shutil
import sys
from pathlib import Path

log = logging.getLogger(__name__)


def autostart_dir() -> Path:
    base = os.environ.get("XDG_CONFIG_HOME") or str(Path.home() / ".config")
    p = Path(base) / "autostart"
    p.mkdir(parents=True, exist_ok=True)
    return p


def autostart_file() -> Path:
    return autostart_dir() / "shout.desktop"


def is_enabled() -> bool:
    return autostart_file().exists()


def _executable() -> str:
    """Best-effort path to invoke `shout-stt` non-interactively."""
    exe = shutil.which("shout-stt")
    if exe:
        return exe
    # Fallback: python -m shout
    return f"{sys.executable} -m shout"


def enable() -> Path:
    p = autostart_file()
    contents = (
        "[Desktop Entry]\n"
        "Type=Application\n"
        "Name=Shout\n"
        "Comment=Voice dictation engine\n"
        f"Exec={_executable()}\n"
        "Icon=audio-input-microphone\n"
        "X-GNOME-Autostart-enabled=true\n"
        "NoDisplay=false\n"
        "Categories=Utility;\n"
    )
    p.write_text(contents, encoding="utf-8")
    log.info("Autostart enabled at %s", p)
    return p


def disable() -> None:
    p = autostart_file()
    if p.exists():
        p.unlink()
        log.info("Autostart disabled (%s removed)", p)


def set_enabled(enabled: bool) -> None:
    if enabled:
        enable()
    else:
        disable()
