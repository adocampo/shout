"""Global hotkey providers.

Backends:
  - X11 (`_X11Hotkey`): low-level XGrabKey via python-xlib, supports key release.
  - evdev (`_EvdevHotkey`): reads raw keyboard events from /dev/input via
    python-evdev. Works on Wayland without the portal. Requires the user to be
    in the ``input`` group. Supports key release.
  - Wayland portal (`_PortalHotkey`): full xdg-desktop-portal GlobalShortcuts
    flow (CreateSession → BindShortcuts → Activated). Toggle-only — the portal
    spec does not deliver key release events to clients. Used as fallback when
    evdev is not available.

All invoke their callbacks from a background thread; the higher-level glue
forwards them to the Qt event loop using a QObject + Signal.

If none of the above work, Shout still works: the user can configure a custom
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


# --- evdev backend (Wayland / any) ------------------------------------------

class _EvdevHotkey(HotkeyProvider):
    """Read raw keyboard events via evdev (/dev/input).

    Works on Wayland without the portal.  Requires read access to
    ``/dev/input/event*`` — the user must be in the ``input`` group.
    """

    supports_release = True

    # Lazily initialised class-level lookup tables (need ``evdev`` import).
    _MOD_GROUPS: dict[str, set[int]] | None = None
    _ALL_MOD_CODES: set[int] | None = None
    _KEY_MAP: dict[str, int] | None = None

    # Qt PortableText modifier name → canonical group name
    _MOD_ALIASES: dict[str, str] = {
        "ctrl": "ctrl", "control": "ctrl",
        "alt": "alt",
        "shift": "shift",
        "meta": "super",  # Qt Meta == Super/Win on Linux
        "super": "super", "win": "super",
    }

    def __init__(self, accelerator: str) -> None:
        self.accelerator = accelerator
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._on_press: ToggleCallback | None = None
        self._on_release: ToggleCallback | None = None

    # -- public interface -----------------------------------------------------

    def start(self, on_press, on_release=None) -> None:
        self._on_press = on_press
        self._on_release = on_release
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._run, name="shout-evdev-hotkey", daemon=True,
        )
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    # -- internal helpers -----------------------------------------------------

    @classmethod
    def _ensure_maps(cls) -> None:
        """Build the modifier-group and key lookup tables (once)."""
        if cls._MOD_GROUPS is not None:
            return
        from evdev import ecodes as e  # type: ignore[import-untyped]

        cls._MOD_GROUPS = {
            "ctrl":  {e.KEY_LEFTCTRL,  e.KEY_RIGHTCTRL},
            "alt":   {e.KEY_LEFTALT,   e.KEY_RIGHTALT},
            "shift": {e.KEY_LEFTSHIFT, e.KEY_RIGHTSHIFT},
            "super": {e.KEY_LEFTMETA,  e.KEY_RIGHTMETA},
        }
        cls._ALL_MOD_CODES = set()
        for codes in cls._MOD_GROUPS.values():
            cls._ALL_MOD_CODES |= codes

        km: dict[str, int] = {}
        # Letters a-z
        for c in range(ord("a"), ord("z") + 1):
            km[chr(c)] = getattr(e, f"KEY_{chr(c).upper()}")
        # Digits 0-9
        for i in range(10):
            km[str(i)] = getattr(e, f"KEY_{i}")
        # Function keys F1-F12
        for i in range(1, 13):
            km[f"f{i}"] = getattr(e, f"KEY_F{i}")
        # Named keys (Qt PortableText names, lowercased)
        km.update({
            "space": e.KEY_SPACE, "tab": e.KEY_TAB,
            "return": e.KEY_ENTER, "enter": e.KEY_ENTER,
            "backspace": e.KEY_BACKSPACE,
            "del": e.KEY_DELETE, "delete": e.KEY_DELETE,
            "ins": e.KEY_INSERT, "insert": e.KEY_INSERT,
            "home": e.KEY_HOME, "end": e.KEY_END,
            "pgup": e.KEY_PAGEUP, "pgdown": e.KEY_PAGEDOWN,
            "up": e.KEY_UP, "down": e.KEY_DOWN,
            "left": e.KEY_LEFT, "right": e.KEY_RIGHT,
            "esc": e.KEY_ESC, "escape": e.KEY_ESC,
            "capslock": e.KEY_CAPSLOCK,
            "print": e.KEY_PRINT, "pause": e.KEY_PAUSE,
            # Physical-key mappings for non-US layouts
            "º": e.KEY_GRAVE, "ª": e.KEY_GRAVE,
            "`": e.KEY_GRAVE, "~": e.KEY_GRAVE,
            "-": e.KEY_MINUS, "=": e.KEY_EQUAL,
            "[": e.KEY_LEFTBRACE, "]": e.KEY_RIGHTBRACE,
            "\\": e.KEY_BACKSLASH,
            ";": e.KEY_SEMICOLON, "'": e.KEY_APOSTROPHE,
            ",": e.KEY_COMMA, ".": e.KEY_DOT, "/": e.KEY_SLASH,
        })
        cls._KEY_MAP = km

    def _parse(self) -> tuple[frozenset[str], int]:
        """Return (required modifier group names, target evdev keycode)."""
        self._ensure_maps()
        assert self._KEY_MAP is not None

        parts = [p.strip() for p in self.accelerator.split("+") if p.strip()]
        if not parts:
            raise ValueError("Empty accelerator")

        key_name = parts[-1].lower()
        mod_names: set[str] = set()
        for p in parts[:-1]:
            group = self._MOD_ALIASES.get(p.lower())
            if group is None:
                raise ValueError(f"Unknown modifier: {p}")
            mod_names.add(group)

        key_code = self._KEY_MAP.get(key_name)
        if key_code is None:
            raise ValueError(
                f"Cannot map key {key_name!r} to evdev code. "
                "Use a letter, number, function key, or common key name."
            )
        return frozenset(mod_names), key_code

    # -- background thread ----------------------------------------------------

    def _run(self) -> None:  # pragma: no cover
        try:
            import evdev  # type: ignore[import-untyped]
            from evdev import ecodes
        except ImportError:
            log.error("python-evdev not installed; evdev hotkey disabled")
            return

        try:
            required_mods, target_key = self._parse()
        except ValueError as exc:
            log.error("Bad accelerator %r for evdev: %s", self.accelerator, exc)
            return

        assert self._MOD_GROUPS is not None

        # Discover keyboard devices — skip virtual / injector devices
        _VIRTUAL_NAMES = ("dotool", "ydotool", "virtual")
        devices: list = []
        for path in evdev.list_devices():
            try:
                dev = evdev.InputDevice(path)
                name_lower = dev.name.lower()
                if any(v in name_lower for v in _VIRTUAL_NAMES):
                    log.debug("evdev: skipping virtual device %s (%s)", dev.path, dev.name)
                    dev.close()
                    continue
                caps = dev.capabilities(verbose=False)
                if ecodes.EV_KEY in caps:
                    key_caps = caps[ecodes.EV_KEY]
                    if ecodes.KEY_A in key_caps and ecodes.KEY_Z in key_caps:
                        devices.append(dev)
                    else:
                        dev.close()
                else:
                    dev.close()
            except (PermissionError, OSError) as exc:
                log.debug("Cannot open %s: %s", path, exc)

        if not devices:
            log.error(
                "evdev: no keyboard devices accessible. "
                "Add your user to the 'input' group: "
                "sudo usermod -aG input $USER  (then log out and back in)"
            )
            return

        log.info(
            "evdev hotkey: monitoring %d device(s) for %s",
            len(devices), self.accelerator,
        )

        import select as _sel

        pressed: set[int] = set()
        _last_press = 0.0  # debounce: ignore duplicate presses within 100ms
        _DEBOUNCE = 0.10

        while not self._stop.is_set():
            try:
                readable, _, _ = _sel.select(devices, [], [], 0.1)
            except (ValueError, OSError):
                devices = [d for d in devices if d.fd >= 0]
                if not devices:
                    log.error("evdev: all keyboard devices disconnected")
                    return
                continue

            for dev in list(readable):
                try:
                    for event in dev.read():
                        if event.type != ecodes.EV_KEY:
                            continue
                        code = event.code
                        if event.value == 1:  # key down
                            pressed.add(code)
                            if code == target_key:
                                active = frozenset(
                                    name
                                    for name, codes in self._MOD_GROUPS.items()
                                    if codes & pressed
                                )
                                if active == required_mods:
                                    now = time.monotonic()
                                    if now - _last_press < _DEBOUNCE:
                                        continue  # duplicate from another interface
                                    _last_press = now
                                    if self._on_press:
                                        try:
                                            self._on_press()
                                        except Exception:  # noqa: BLE001
                                            log.exception("on_press callback failed")
                        elif event.value == 0:  # key up
                            if code == target_key and self._on_release:
                                try:
                                    self._on_release()
                                except Exception:  # noqa: BLE001
                                    log.exception("on_release callback failed")
                            pressed.discard(code)
                        # value == 2 → key repeat, ignored
                except OSError:
                    log.debug("evdev: device disconnected: %s", dev.path)
                    try:
                        dev.close()
                    except Exception:  # noqa: BLE001
                        pass
                    devices.remove(dev)

        for dev in devices:
            try:
                dev.close()
            except Exception:  # noqa: BLE001
                pass


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
    # Wayland: prefer evdev (reliable, works like Handy's handy-keys),
    # fall back to portal if python-evdev is not installed.
    try:
        import evdev  # type: ignore[import-untyped]  # noqa: F401
        return _EvdevHotkey(cfg.accelerator)
    except ImportError:
        log.info("python-evdev not available; using portal for Wayland hotkey")
        return _PortalHotkey(cfg.accelerator)
