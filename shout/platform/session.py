"""Detect display server and desktop environment."""
from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from enum import Enum

log = logging.getLogger(__name__)


class DisplayServer(str, Enum):
    WAYLAND = "wayland"
    X11 = "x11"
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class SessionInfo:
    server: DisplayServer
    desktop: str  # XDG_CURRENT_DESKTOP, lowercase
    is_kde: bool
    is_gnome: bool


def detect_session() -> SessionInfo:
    session_type = (os.environ.get("XDG_SESSION_TYPE") or "").lower()
    if session_type == "wayland" or os.environ.get("WAYLAND_DISPLAY"):
        server = DisplayServer.WAYLAND
    elif session_type == "x11" or os.environ.get("DISPLAY"):
        server = DisplayServer.X11
    else:
        server = DisplayServer.UNKNOWN

    desktop = (os.environ.get("XDG_CURRENT_DESKTOP") or "").lower()
    info = SessionInfo(
        server=server,
        desktop=desktop,
        is_kde="kde" in desktop or "plasma" in desktop,
        is_gnome="gnome" in desktop,
    )
    log.info(
        "Detected session: server=%s desktop=%r kde=%s gnome=%s",
        info.server.value, info.desktop, info.is_kde, info.is_gnome,
    )
    return info
