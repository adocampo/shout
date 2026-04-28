"""Global hotkey providers.

Backends:
  - X11 (`_X11Hotkey`): low-level XGrabKey via python-xlib, supports key release.
  - Wayland portal (`_PortalHotkey`): full xdg-desktop-portal GlobalShortcuts
    flow (CreateSession → BindShortcuts → Activated). Toggle-only — the portal
    spec does not deliver key release events to clients.

Both invoke their callbacks from a background thread; the higher-level glue
forwards them to the Qt event loop using a QObject + Signal.

If the portal flow fails, Shout still works: the user can configure a custom
shortcut in their compositor (KDE Settings → Shortcuts) to call
`shout-stt --toggle`, which routes to the running instance via the D-Bus
service in `dbus_service.py`.
"""
from __future__ import annotations

import logging
import threading
import time
from abc import ABC, abstractmethod
from typing import Callable

from ..config import HotkeyConfig
from .session import DisplayServer, SessionInfo

log = logging.getLogger(__name__)

ToggleCallback = Callable[[], None]


class HotkeyProvider(ABC):
    @abstractmethod
    def start(self, on_press: ToggleCallback, on_release: ToggleCallback | None = None) -> None: ...
    @abstractmethod
    def stop(self) -> None: ...
    @property
    @abstractmethod
    def supports_release(self) -> bool: ...


# --- X11 backend ------------------------------------------------------------

class _X11Hotkey(HotkeyProvider):
    """Grab a global key combination on X11."""

    supports_release = True

    _MODS = {
        "ctrl": "ControlMask", "control": "ControlMask",
        "alt": "Mod1Mask", "meta": "Mod1Mask",
        "shift": "ShiftMask",
        "super": "Mod4Mask", "win": "Mod4Mask",
    }

    def __init__(self, accelerator: str) -> None:
        self.accelerator = accelerator
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._on_press: ToggleCallback | None = None
        self._on_release: ToggleCallback | None = None

    def start(self, on_press, on_release=None) -> None:
        self._on_press = on_press
        self._on_release = on_release
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="shout-x11-hotkey", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def _parse(self, X) -> tuple[str, int]:
        parts = [p.strip().lower() for p in self.accelerator.split("+") if p.strip()]
        if not parts:
            raise ValueError("Empty accelerator")
        key = parts[-1]
        mods = parts[:-1]
        mask = 0
        for m in mods:
            attr = self._MODS.get(m)
            if attr is None:
                raise ValueError(f"Unknown modifier: {m}")
            mask |= getattr(X, attr)
        return key, mask

    def _run(self) -> None:  # pragma: no cover - requires X server
        try:
            from Xlib import X, XK, display
            from Xlib.error import BadAccess
        except ImportError:
            log.error("python-xlib not installed; X11 hotkey disabled")
            return
        try:
            disp = display.Display()
        except Exception as exc:  # noqa: BLE001
            log.error("Cannot open X display: %s", exc)
            return

        root = disp.screen().root
        try:
            key_str, base_mask = self._parse(X)
        except ValueError as exc:
            log.error("Bad accelerator %r: %s", self.accelerator, exc)
            return

        if len(key_str) == 1:
            keysym = XK.string_to_keysym(key_str)
        else:
            keysym = XK.string_to_keysym(key_str.upper()) or XK.string_to_keysym(key_str)
        if not keysym:
            log.error("Unknown key: %r", key_str)
            return
        keycode = disp.keysym_to_keycode(keysym)
        if not keycode:
            log.error("No keycode for %r", key_str)
            return

        for lm in (0, X.LockMask, X.Mod2Mask, X.LockMask | X.Mod2Mask):
            try:
                root.grab_key(keycode, base_mask | lm, True, X.GrabModeAsync, X.GrabModeAsync)
            except BadAccess:
                log.error("Hotkey already grabbed by another app: %s", self.accelerator)
                return

        log.info("X11 hotkey grabbed: %s", self.accelerator)
        root.change_attributes(event_mask=X.KeyPressMask | X.KeyReleaseMask)

        while not self._stop.is_set():
            if disp.pending_events() == 0:
                if self._stop.wait(0.05):
                    break
                continue
            event = disp.next_event()
            if event.type == X.KeyPress and event.detail == keycode:
                if self._on_press:
                    self._on_press()
            elif event.type == X.KeyRelease and event.detail == keycode:
                if self._on_release:
                    self._on_release()


# --- Portal backend (Wayland) ----------------------------------------------

# Map our human accelerator (e.g. "Ctrl+Alt+Space") to the portal's preferred
# trigger syntax (e.g. "CTRL+ALT+space"). The portal accepts X11-style keysym
# names; uppercase modifier names + lowercase keysym is the common form.
_PORTAL_MOD = {
    "ctrl": "CTRL", "control": "CTRL",
    "alt": "ALT",
    "shift": "SHIFT",
    "meta": "LOGO", "super": "LOGO", "win": "LOGO",
}


def _accel_to_portal(accel: str) -> str:
    parts = [p.strip() for p in accel.split("+") if p.strip()]
    if not parts:
        return ""
    out = []
    for p in parts[:-1]:
        out.append(_PORTAL_MOD.get(p.lower(), p.upper()))
    key = parts[-1]
    out.append(key if len(key) == 1 else key.lower())
    return "+".join(out)


class _PortalHotkey(HotkeyProvider):
    """xdg-desktop-portal GlobalShortcuts. Toggle-only."""

    supports_release = False
    SHORTCUT_ID = "toggle-dictation"

    def __init__(self, accelerator: str) -> None:
        self.accelerator = accelerator
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._on_press: ToggleCallback | None = None
        self._connection = None
        self._session_handle: str | None = None

    def start(self, on_press, on_release=None) -> None:
        self._on_press = on_press
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="shout-portal-hotkey", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def _run(self) -> None:  # pragma: no cover - requires portal
        try:
            from jeepney import DBusAddress, MessageType, new_method_call
            from jeepney.bus_messages import message_bus
            from jeepney.io.blocking import open_dbus_connection
            from jeepney.low_level import HeaderFields
        except ImportError:
            log.error("jeepney not installed; portal hotkey disabled")
            return

        try:
            conn = open_dbus_connection(bus="SESSION")
        except Exception as exc:  # noqa: BLE001
            log.error("Cannot open D-Bus session bus for portal hotkey: %s", exc)
            return
        self._connection = conn
        sender_token = conn.unique_name.lstrip(":").replace(".", "_")

        portal = DBusAddress(
            "/org/freedesktop/portal/desktop",
            bus_name="org.freedesktop.portal.Desktop",
            interface="org.freedesktop.portal.GlobalShortcuts",
        )

        for rule in (
            "type='signal',interface='org.freedesktop.portal.Request',member='Response'",
            "type='signal',interface='org.freedesktop.portal.GlobalShortcuts',member='Activated'",
        ):
            try:
                conn.send_and_get_reply(
                    new_method_call(message_bus, "AddMatch", "s", (rule,))
                )
            except Exception as exc:  # noqa: BLE001
                log.error("Portal AddMatch failed: %s", exc)
                return

        # 1) CreateSession
        ts = int(time.time() * 1000) & 0xFFFFFFFF
        session_token = f"shout_{ts}"
        create_handle_token = f"shout_create_{ts}"
        create_request_path = (
            f"/org/freedesktop/portal/desktop/request/{sender_token}/{create_handle_token}"
        )
        options = {
            "session_handle_token": ("s", session_token),
            "handle_token": ("s", create_handle_token),
        }
        try:
            reply = conn.send_and_get_reply(
                new_method_call(portal, "CreateSession", "a{sv}", (options,))
            )
        except Exception as exc:  # noqa: BLE001
            log.error("Portal CreateSession failed: %s", exc)
            return
        if reply.header.message_type == MessageType.error:
            log.error("Portal CreateSession error: %s", reply.body)
            return

        log.info("Portal CreateSession sent, awaiting Response…")
        result = self._await_response(conn, create_request_path, HeaderFields, MessageType, timeout=30)
        if result is None:
            log.error("Portal CreateSession: no Response (compositor lacks GlobalShortcuts portal?)")
            return
        code, results = result
        if code != 0:
            log.error("Portal CreateSession denied (response=%d)", code)
            return
        self._session_handle = results.get("session_handle")
        if not self._session_handle:
            log.error("Portal CreateSession: missing session_handle in results: %r", results)
            return
        log.info("Portal session created: %s", self._session_handle)

        # 2) BindShortcuts
        bind_handle_token = f"shout_bind_{ts}"
        bind_request_path = (
            f"/org/freedesktop/portal/desktop/request/{sender_token}/{bind_handle_token}"
        )
        portal_trigger = _accel_to_portal(self.accelerator)
        shortcut_options = {
            "description": ("s", "Toggle voice dictation"),
            "preferred_trigger": ("s", portal_trigger),
        }
        shortcuts = [(self.SHORTCUT_ID, shortcut_options)]
        bind_options = {"handle_token": ("s", bind_handle_token)}
        try:
            reply = conn.send_and_get_reply(new_method_call(
                portal, "BindShortcuts", "oa(sa{sv})sa{sv}",
                (self._session_handle, shortcuts, "", bind_options),
            ))
        except Exception as exc:  # noqa: BLE001
            log.error("Portal BindShortcuts failed: %s", exc)
            return
        if reply.header.message_type == MessageType.error:
            log.error("Portal BindShortcuts error: %s", reply.body)
            return

        log.info(
            "Portal BindShortcuts sent (trigger=%r). The compositor may show a confirmation dialog.",
            portal_trigger,
        )
        result = self._await_response(conn, bind_request_path, HeaderFields, MessageType, timeout=120)
        if result is None:
            log.error("Portal BindShortcuts: no Response")
            return
        code, results = result
        if code != 0:
            log.error(
                "Portal BindShortcuts denied (response=%d). User declined or compositor unsupported.",
                code,
            )
            return
        log.info("Portal hotkey bound. Press %s to dictate.", self.accelerator)

        # 3) Listen for Activated
        while not self._stop.is_set():
            try:
                msg = conn.receive(timeout=0.5)
            except TimeoutError:
                continue
            except Exception as exc:  # noqa: BLE001
                log.debug("portal receive: %s", exc)
                continue
            if msg is None:
                continue
            if msg.header.message_type != MessageType.signal:
                continue
            if msg.header.fields.get(HeaderFields.member) != "Activated":
                continue
            if msg.header.fields.get(HeaderFields.interface) != "org.freedesktop.portal.GlobalShortcuts":
                continue
            try:
                session_h, shortcut_id, _timestamp, _opts = msg.body
            except ValueError:
                continue
            if session_h == self._session_handle and shortcut_id == self.SHORTCUT_ID:
                log.debug("Portal Activated received")
                if self._on_press:
                    try:
                        self._on_press()
                    except Exception:  # noqa: BLE001
                        log.exception("on_press callback failed")

    def _await_response(self, conn, request_path: str, HeaderFields, MessageType, timeout: float):
        """Wait for a Response signal on `request_path`. Returns (code, results) or None."""
        deadline = time.monotonic() + timeout
        while not self._stop.is_set() and time.monotonic() < deadline:
            try:
                msg = conn.receive(timeout=0.5)
            except TimeoutError:
                continue
            except Exception as exc:  # noqa: BLE001
                log.debug("portal receive: %s", exc)
                continue
            if msg is None:
                continue
            if msg.header.message_type != MessageType.signal:
                continue
            if msg.header.fields.get(HeaderFields.member) != "Response":
                continue
            if msg.header.fields.get(HeaderFields.path) != request_path:
                continue
            try:
                code, results = msg.body
                # results is a{sv}: each value is a (signature, value) variant tuple.
                unwrapped = {k: (v[1] if isinstance(v, tuple) and len(v) == 2 else v)
                             for k, v in dict(results).items()}
                return int(code), unwrapped
            except Exception as exc:  # noqa: BLE001
                log.error("Malformed Response signal: %s", exc)
                return None
        return None


# --- Factory ----------------------------------------------------------------

def make_hotkey_provider(session: SessionInfo, cfg: HotkeyConfig) -> HotkeyProvider:
    if session.server == DisplayServer.X11:
        return _X11Hotkey(cfg.accelerator)
    return _PortalHotkey(cfg.accelerator)
