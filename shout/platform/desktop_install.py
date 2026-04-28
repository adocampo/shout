"""Install the application's .desktop file under XDG_DATA_HOME/applications/.

Required so xdg-desktop-portal can resolve our app ID (`shout-stt`) and the
GlobalShortcuts portal stops complaining with "App info not found".
"""
from __future__ import annotations

import logging
import os
import shutil
from importlib.resources import files
from pathlib import Path

log = logging.getLogger(__name__)

DESKTOP_BASENAME = "shout-stt.desktop"


def _user_apps_dir() -> Path:
    base = os.environ.get("XDG_DATA_HOME") or str(Path.home() / ".local" / "share")
    p = Path(base) / "applications"
    p.mkdir(parents=True, exist_ok=True)
    return p


def installed_path() -> Path:
    return _user_apps_dir() / DESKTOP_BASENAME


def is_installed() -> bool:
    return installed_path().exists()


def install(force: bool = False) -> Path | None:
    """Copy the bundled desktop file into the user applications dir.

    Returns the destination path on success, None if skipped.
    """
    dest = installed_path()
    if dest.exists() and not force:
        return dest
    try:
        src = Path(str(files("shout.resources").joinpath(DESKTOP_BASENAME)))
        if not src.exists():
            log.warning("Bundled desktop file not found at %s", src)
            return None
        # If `shout-stt` is on PATH use absolute path; otherwise leave as-is.
        contents = src.read_text(encoding="utf-8")
        exe = shutil.which("shout-stt")
        if exe:
            contents = contents.replace("Exec=shout-stt", f"Exec={exe}")
        dest.write_text(contents, encoding="utf-8")
        log.info("Installed desktop file at %s", dest)
        return dest
    except Exception as exc:  # noqa: BLE001
        log.warning("Could not install desktop file: %s", exc)
        return None
