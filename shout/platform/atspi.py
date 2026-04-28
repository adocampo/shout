"""AT-SPI caret locator — best-effort placement of the badge near the text caret.

Returns None silently whenever AT-SPI / pygobject is not available or the
focused widget doesn't expose a usable caret. Callers fall back to other
strategies (mouse position on X11, screen corner).
"""
from __future__ import annotations

import logging

log = logging.getLogger(__name__)


def caret_position() -> tuple[int, int] | None:
    """Return absolute (x, y) screen coords of the focused text caret, or None."""
    try:
        import gi
        gi.require_version("Atspi", "2.0")
        from gi.repository import Atspi  # type: ignore
    except (ImportError, ValueError):
        return None

    try:
        desktop = Atspi.get_desktop(0)
    except Exception:  # noqa: BLE001
        return None

    try:
        # Walk active applications looking for the focused text component.
        for i in range(desktop.get_child_count()):
            app = desktop.get_child_at_index(i)
            if app is None:
                continue
            focused = _find_focused_text(app)
            if focused is None:
                continue
            try:
                ext = focused.get_extents(Atspi.CoordType.SCREEN)
                # Place near caret if the iface gives us caret offset.
                text_iface = focused.queryText()
                offset = text_iface.get_caret_offset()
                rect = text_iface.get_character_extents(offset, Atspi.CoordType.SCREEN)
                if rect.x > 0 and rect.y > 0:
                    return int(rect.x), int(rect.y + rect.height)
                return int(ext.x + ext.width / 2), int(ext.y + ext.height)
            except Exception:  # noqa: BLE001
                continue
    except Exception as exc:  # noqa: BLE001
        log.debug("AT-SPI walk failed: %s", exc)
    return None


def _find_focused_text(node) -> object | None:  # pragma: no cover - depends on AT-SPI
    try:
        state = node.get_state_set()
        from gi.repository import Atspi  # type: ignore
        if state.contains(Atspi.StateType.FOCUSED):
            try:
                node.queryText()
                return node
            except Exception:  # noqa: BLE001
                pass
        for i in range(node.get_child_count()):
            child = node.get_child_at_index(i)
            if child is None:
                continue
            found = _find_focused_text(child)
            if found is not None:
                return found
    except Exception:  # noqa: BLE001
        return None
    return None
