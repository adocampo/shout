"""Logging setup with rotating file handler."""
from __future__ import annotations

import logging
import logging.handlers
import sys

from .paths import log_file

_FMT = "%(asctime)s [%(levelname)s] %(name)s: %(message)s"
_initialised = False


def setup_logging(level: int = logging.INFO) -> None:
    global _initialised
    if _initialised:
        return

    root = logging.getLogger()
    root.setLevel(level)
    root.handlers.clear()

    fmt = logging.Formatter(_FMT)

    stream = logging.StreamHandler(sys.stderr)
    stream.setFormatter(fmt)
    root.addHandler(stream)

    try:
        fh = logging.handlers.RotatingFileHandler(
            log_file(), maxBytes=2 * 1024 * 1024, backupCount=3, encoding="utf-8"
        )
        fh.setFormatter(fmt)
        root.addHandler(fh)
    except OSError as exc:  # pragma: no cover - degraded mode
        root.warning("Could not open log file: %s", exc)

    _initialised = True
