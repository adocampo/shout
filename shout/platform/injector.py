"""Inject text into the focused field on X11 and Wayland.

Strategy:
  - Wayland: prefer `wtype` for direct typing. For long/complex text, fall back
    to clipboard (`wl-copy`) + `wtype -M ctrl -k v -m ctrl`. If `wtype` is not
    available at all, use clipboard + best-effort paste via `ydotool` (requires
    user setup) or warn.
  - X11: prefer `xdotool type`. For long text, use `xclip` selection clipboard
    + `xdotool key ctrl+v`.

The previous clipboard contents are saved and restored after pasting so the
user's clipboard is preserved.
"""
from __future__ import annotations

import logging
import os
import shutil
import subprocess
import time
from abc import ABC, abstractmethod
from typing import Protocol

from ..config import InjectionConfig
from .session import DisplayServer, SessionInfo

log = logging.getLogger(__name__)


class InjectionError(RuntimeError):
    pass


class ManualPasteRequired(InjectionError):
    """Text was copied to the clipboard but the compositor refused synthetic paste.
    The user must press Ctrl+V manually."""


_VK_UNSUPPORTED_MARKERS = (
    "virtual keyboard protocol",
    "virtual-keyboard",
    "compositor does not support",
)


def _is_vk_unsupported(err: Exception) -> bool:
    msg = str(err).lower()
    return any(m in msg for m in _VK_UNSUPPORTED_MARKERS)


class TextInjector(Protocol):
    name: str

    def inject(self, text: str) -> None: ...
    def is_available(self) -> bool: ...


# --- Helpers ----------------------------------------------------------------

def _which(name: str) -> str | None:
    return shutil.which(name)


def _run(cmd: list[str], stdin: bytes | None = None, timeout: float = 10.0) -> None:
    log.debug("exec: %s", " ".join(cmd))
    proc = subprocess.run(
        cmd, input=stdin, capture_output=True, timeout=timeout, check=False,
    )
    if proc.returncode != 0:
        raise InjectionError(
            f"{cmd[0]} failed (rc={proc.returncode}): {proc.stderr.decode(errors='replace').strip()}"
        )


def _run_detached_writer(cmd: list[str], stdin: bytes, timeout: float = 5.0) -> None:
    """Run a command that daemonizes after reading stdin (e.g. wl-copy).

    `subprocess.run(capture_output=True)` would block waiting for the inherited
    stdout/stderr pipes that the daemon child keeps open. We send only stdin
    and discard the daemon's output via DEVNULL so we return as soon as the
    parent fork exits.
    """
    log.debug("exec (detached): %s", " ".join(cmd))
    proc = subprocess.Popen(
        cmd,
        stdin=subprocess.PIPE,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )
    try:
        proc.communicate(input=stdin, timeout=timeout)
    except subprocess.TimeoutExpired:
        # Don't kill it: the daemon child is the whole point. Just close stdin
        # and move on; the foreground parent should already have exited.
        try:
            if proc.stdin and not proc.stdin.closed:
                proc.stdin.close()
        except Exception:  # noqa: BLE001
            pass
        # Best-effort wait for the foreground fork.
        try:
            proc.wait(timeout=0.5)
        except subprocess.TimeoutExpired:
            pass
    if proc.returncode not in (None, 0):
        raise InjectionError(f"{cmd[0]} failed (rc={proc.returncode})")


# --- Backends ---------------------------------------------------------------

class _Base(ABC):
    name: str = "?"

    @abstractmethod
    def inject(self, text: str) -> None: ...

    def is_available(self) -> bool:  # default: always
        return True


class WtypeInjector(_Base):
    name = "wtype"

    def is_available(self) -> bool:
        return _which("wtype") is not None

    def inject(self, text: str) -> None:
        # `wtype -` reads the text from stdin to avoid argv length / quoting issues.
        _run(["wtype", "-"], stdin=text.encode("utf-8"))


class XdotoolInjector(_Base):
    name = "xdotool"

    def is_available(self) -> bool:
        return _which("xdotool") is not None

    def inject(self, text: str) -> None:
        _run([
            "xdotool", "type", "--clearmodifiers", "--delay", "1", "--", text,
        ])


class DotoolInjector(_Base):
    """Kernel-level uinput injection that respects the keyboard layout.

    Unlike ydotool, dotool links libxkbcommon and uses the active xkb layout,
    so it can type accented characters / \u00f1 / etc. directly without going
    through the clipboard. Works on any compositor (X11/Wayland/KDE/GNOME/sway)
    because the events are injected via /dev/uinput, below the compositor.

    Requires the `dotoold` daemon (or membership in the `input` group plus
    udev rules) and the `dotool` CLI. See https://git.sr.ht/~geb/dotool .

    Protocol: dotool reads commands from stdin, one per line. We send:
        keyup ctrl alt shift super
        typedelay 1
        type <text>
    """

    name = "dotool"

    def is_available(self) -> bool:
        return _which("dotool") is not None

    def inject(self, text: str) -> None:
        # Release any modifier still held from the activation hotkey, then
        # type the text. Newlines in the text would be interpreted as command
        # separators by dotool, so split on them and emit explicit `key enter`
        # between chunks.
        lines = text.split("\n")
        cmds: list[str] = [
            "keyup ctrl",
            "keyup alt",
            "keyup shift",
            "keyup super",
            "typedelay 1",
        ]
        for i, chunk in enumerate(lines):
            if i > 0:
                cmds.append("key enter")
            if chunk:
                cmds.append("type " + chunk)
        payload = ("\n".join(cmds) + "\n").encode("utf-8")
        _run(["dotool"], stdin=payload, timeout=20)


class YdotoolInjector(_Base):
    """Kernel-level uinput injection. Works on any compositor (X11/Wayland/KDE/GNOME/sway).

    Requires the `ydotoold` daemon running and access to /dev/uinput. The daemon
    socket is usually at $XDG_RUNTIME_DIR/.ydotool_socket or /tmp/.ydotool_socket.
    See README for setup instructions.
    """

    name = "ydotool"

    # Linux input event codes for the keys we want to release before typing,
    # because the user usually still holds Ctrl+Alt (the hotkey) when our
    # injection starts. If we don't release them, ydotool's typed letters get
    # combined with those modifiers (Ctrl+Alt+v, Ctrl+Alt+m, …) and the apps
    # treat them as shortcuts instead of text.
    _MOD_RELEASE_KEYS = (
        29,   # KEY_LEFTCTRL
        97,   # KEY_RIGHTCTRL
        56,   # KEY_LEFTALT
        100,  # KEY_RIGHTALT
        42,   # KEY_LEFTSHIFT
        54,   # KEY_RIGHTSHIFT
        125,  # KEY_LEFTMETA / Super
        126,  # KEY_RIGHTMETA
    )

    def __init__(self, clipboard_policy: str = "never") -> None:
        # "never" | "unicode" | "always"
        self.clipboard_policy = clipboard_policy

    @staticmethod
    def _socket_path() -> str | None:
        env = os.environ.get("YDOTOOL_SOCKET")
        if env and os.path.exists(env):
            return env
        candidates = [
            os.path.join(os.environ.get("XDG_RUNTIME_DIR", ""), ".ydotool_socket"),
            "/tmp/.ydotool_socket",
            "/run/user/{}/.ydotool_socket".format(os.getuid()),
        ]
        for p in candidates:
            if p and os.path.exists(p):
                return p
        return None

    def is_available(self) -> bool:
        if _which("ydotool") is None:
            return False
        return self._socket_path() is not None

    def _exec(self, args: list[str], stdin: bytes | None = None, timeout: float = 15.0) -> None:
        sock = self._socket_path()
        env = os.environ.copy()
        if sock:
            env["YDOTOOL_SOCKET"] = sock
        proc = subprocess.run(
            ["ydotool", *args],
            input=stdin, capture_output=True, timeout=timeout, check=False, env=env,
        )
        if proc.returncode != 0:
            raise InjectionError(
                f"ydotool failed (rc={proc.returncode}): "
                f"{proc.stderr.decode(errors='replace').strip()}"
            )

    def inject(self, text: str) -> None:
        # 1) Release any modifier the user might still be holding from the hotkey.
        #    `ydotool key 29:0 56:0 …` sends key-up events for those scancodes.
        try:
            self._exec(["key", *(f"{k}:0" for k in self._MOD_RELEASE_KEYS)], timeout=2)
        except InjectionError as exc:
            log.debug("ydotool modifier release failed (ignored): %s", exc)

        # 2) Tiny grace period so the kernel/compositor processes the releases
        #    AND so the user has time to physically release the hotkey keys.
        time.sleep(0.12)

        # 3) Decide whether THIS text needs the clipboard path.
        #    ydotool only knows Linux scancodes (US layout). Non-ASCII chars
        #    like accents/ñ/¿ get mangled on other layouts. The clipboard
        #    path uses Ctrl+V which is layout-independent. We save & restore
        #    the previous clipboard so the user doesn't notice.
        needs_clipboard = False
        if self.clipboard_policy == "always":
            needs_clipboard = True
        elif self.clipboard_policy == "unicode":
            needs_clipboard = not text.isascii()
        if needs_clipboard and _which("wl-copy"):
            previous = None
            if _which("wl-paste"):
                try:
                    p = subprocess.run(
                        ["wl-paste", "-n"], capture_output=True, timeout=2, check=False,
                    )
                    if p.returncode == 0:
                        previous = p.stdout
                except Exception:  # noqa: BLE001
                    previous = None
            try:
                _run_detached_writer(["wl-copy"], stdin=text.encode("utf-8"))
                # Ctrl down (29:1), V down (47:1), V up (47:0), Ctrl up (29:0).
                log.debug("ydotool: paste %d chars via clipboard", len(text))
                self._exec(["key", "29:1", "47:1", "47:0", "29:0"], timeout=3)
                return
            except InjectionError as exc:
                log.warning("ydotool clipboard-paste failed (%s); falling back to type", exc)
            finally:
                # Best-effort restore of the previous clipboard.
                if previous is not None:
                    try:
                        # Tiny delay so the paste keystrokes are consumed first.
                        time.sleep(0.08)
                        _run_detached_writer(["wl-copy"], stdin=previous)
                    except Exception:  # noqa: BLE001
                        log.warning("Could not restore previous clipboard contents")

        # 4) Direct typing. NEVER touches the clipboard. On non-US keyboard
        #    layouts some characters may be mistyped; set
        #    `injection.clipboard_policy` to "unicode" or "always" if that's
        #    a problem.
        log.debug("exec: ydotool type -- (len=%d) socket=%s", len(text), self._socket_path())
        self._exec(["type", "--key-delay", "12", "--", text], timeout=20)


class KdotoolPasteInjector(_Base):
    """KDE-only: copy to clipboard then send Ctrl+V via KWin scripting (kdotool).

    Does NOT need root or uinput. Works because KWin's scripting API can
    synthesize key events without the virtual-keyboard protocol.
    Only suitable for paste shortcuts; cannot type arbitrary Unicode.
    """

    name = "kdotool-paste"

    def is_available(self) -> bool:
        return _which("kdotool") is not None and _which("wl-copy") is not None

    def inject(self, text: str) -> None:
        _run_detached_writer(["wl-copy"], stdin=text.encode("utf-8"))
        # kdotool key syntax mirrors xdotool.
        _run(["kdotool", "key", "ctrl+v"], timeout=5)


class ClipboardPasteInjector(_Base):
    """Copy to clipboard then simulate Ctrl+V; restore previous clipboard."""

    def __init__(self, server: DisplayServer) -> None:
        self.server = server
        self._wayland_paste = ["wtype", "-M", "ctrl", "-k", "v", "-m", "ctrl"]
        self._x11_paste = ["xdotool", "key", "--clearmodifiers", "ctrl+v"]

    @property
    def name(self) -> str:
        return f"clipboard-{self.server.value}"

    def is_available(self) -> bool:
        if self.server == DisplayServer.WAYLAND:
            return _which("wl-copy") is not None and _which("wtype") is not None
        if self.server == DisplayServer.X11:
            return _which("xclip") is not None and _which("xdotool") is not None
        return False

    def _read_clip(self) -> bytes | None:
        try:
            if self.server == DisplayServer.WAYLAND:
                p = subprocess.run(["wl-paste", "-n"], capture_output=True, timeout=2, check=False)
            else:
                p = subprocess.run(
                    ["xclip", "-selection", "clipboard", "-o"],
                    capture_output=True, timeout=2, check=False,
                )
            if p.returncode == 0:
                return p.stdout
        except Exception:  # noqa: BLE001
            return None
        return None

    def _write_clip(self, data: bytes) -> None:
        if self.server == DisplayServer.WAYLAND:
            _run_detached_writer(["wl-copy"], stdin=data)
        else:
            _run(["xclip", "-selection", "clipboard", "-i"], stdin=data)

    def inject(self, text: str) -> None:
        previous = self._read_clip()
        try:
            self._write_clip(text.encode("utf-8"))
            paste = self._wayland_paste if self.server == DisplayServer.WAYLAND else self._x11_paste
            _run(paste)
        finally:
            if previous is not None:
                try:
                    self._write_clip(previous)
                except Exception:  # noqa: BLE001
                    log.warning("Could not restore previous clipboard contents")


# --- Façade -----------------------------------------------------------------

class CompositeInjector:
    """Selects the best backend per call, honoring user override and length thresholds."""

    def __init__(self, session: SessionInfo, cfg: InjectionConfig) -> None:
        self.session = session
        self.cfg = cfg
        self._direct: _Base | None = None
        self._clipboard: _Base | None = None
        self._dotool: _Base | None = None
        self._ydotool: _Base | None = None
        self._kdotool_paste: _Base | None = None
        self._direct_broken = False
        self._clipboard_paste_broken = False
        self._init_backends()

    def _init_backends(self) -> None:
        if self.session.server == DisplayServer.WAYLAND:
            wt = WtypeInjector()
            if wt.is_available():
                self._direct = wt
        else:
            xd = XdotoolInjector()
            if xd.is_available():
                self._direct = xd

        policy = self.cfg.clipboard_policy
        allow_clipboard = policy != "never"

        if allow_clipboard:
            cb = ClipboardPasteInjector(self.session.server)
            if cb.is_available():
                self._clipboard = cb

        # dotool: layout-aware uinput injection. Best of both worlds (works on
        # KDE Wayland AND respects xkb layout for Unicode).
        dt = DotoolInjector()
        if dt.is_available():
            self._dotool = dt

        yd = YdotoolInjector(clipboard_policy=policy)
        if yd.is_available():
            self._ydotool = yd

        if (
            allow_clipboard
            and self.session.server == DisplayServer.WAYLAND
            and self.session.is_kde
        ):
            kp = KdotoolPasteInjector()
            if kp.is_available():
                self._kdotool_paste = kp

        log.info(
            "Injector backends: direct=%s dotool=%s ydotool=%s clipboard=%s "
            "kdotool_paste=%s clipboard_policy=%s",
            self._direct.name if self._direct else None,
            self._dotool.name if self._dotool else None,
            self._ydotool.name if self._ydotool else None,
            self._clipboard.name if self._clipboard else None,
            self._kdotool_paste.name if self._kdotool_paste else None,
            policy,
        )

    def available_backends(self) -> list[str]:
        out = []
        if self._direct:
            out.append(self._direct.name)
        if self._clipboard:
            out.append(self._clipboard.name)
        return out

    def _copy_only(self, text: str) -> None:
        """Last-resort: just copy to the clipboard and ask the user to paste."""
        if self.cfg.clipboard_policy == "never":
            raise InjectionError(
                "El compositor bloquea la inyección directa y el uso del "
                "portapapeles está desactivado (injection.clipboard_policy=\"never\"). "
                "Cambia la política a \"unicode\" o \"always\" si quieres que "
                "Shout pueda copiar al portapapeles como último recurso."
            )
        server = self.session.server
        if server == DisplayServer.WAYLAND and _which("wl-copy"):
            _run_detached_writer(["wl-copy"], stdin=text.encode("utf-8"))
        elif server == DisplayServer.X11 and _which("xclip"):
            _run(["xclip", "-selection", "clipboard", "-i"], stdin=text.encode("utf-8"))
        else:
            raise InjectionError(
                "Compositor blocks synthetic typing/paste and no clipboard tool is available. "
                "Install `wl-clipboard` (Wayland) or `xclip` (X11)."
            )
        raise ManualPasteRequired(
            "Texto copiado al portapapeles. Pulsa Ctrl+V para pegar (el compositor "
            "bloquea la inyección directa)."
        )

    def inject(self, text: str) -> None:
        if not text:
            return

        forced = self.cfg.backend

        # Forced backends: try once; on VK-unsupported, degrade to copy-only.
        if forced in ("wtype", "xdotool", "clipboard", "ydotool", "dotool", "kdotool-paste"):
            if forced in ("clipboard", "kdotool-paste") and self.cfg.clipboard_policy == "never":
                raise InjectionError(
                    f"Forced backend {forced!r} requires `injection.clipboard_policy != 'never'`."
                )
            backend: _Base | None = {
                "wtype": WtypeInjector(),
                "xdotool": XdotoolInjector(),
                "clipboard": self._clipboard,
                "dotool": DotoolInjector(),
                "ydotool": YdotoolInjector(clipboard_policy=self.cfg.clipboard_policy),
                "kdotool-paste": KdotoolPasteInjector(),
            }[forced]
            if backend is None or not backend.is_available():
                raise InjectionError(
                    f"Forced backend {forced!r} not available."
                )
            try:
                log.info("Injecting %d chars via %s (forced)", len(text), backend.name)
                backend.inject(text)
                return
            except InjectionError as exc:
                if _is_vk_unsupported(exc):
                    log.warning(
                        "Forced backend %s blocked by compositor (no virtual-keyboard "
                        "protocol); falling back to copy-only.", backend.name,
                    )
                    self._copy_only(text)
                    return
                raise

        # Auto mode with self-healing fallbacks.
        order: list[_Base] = []
        policy = self.cfg.clipboard_policy
        # dotool wins whenever it's installed: layout-aware AND works on every
        # compositor (uinput-based, but reads xkb).
        if self._dotool:
            order.append(self._dotool)
        if policy == "never":
            # No clipboard at all: after dotool, try the layout-aware direct
            # typers (wtype/xdotool); last resort `ydotool type` (US-only).
            if self._direct and not self._direct_broken:
                order.append(self._direct)
            if self._ydotool:
                order.append(self._ydotool)
        else:
            # ydotool next (it decides per-text whether to type or paste
            # based on `clipboard_policy`).
            if self._ydotool:
                order.append(self._ydotool)
            if self._direct and not self._direct_broken and len(text) <= self.cfg.clipboard_threshold:
                order.append(self._direct)
            if self._kdotool_paste:
                order.append(self._kdotool_paste)
            if self._clipboard and not self._clipboard_paste_broken:
                order.append(self._clipboard)
            if self._direct and not self._direct_broken and self._direct not in order:
                order.append(self._direct)

        last_exc: InjectionError | None = None
        for backend in order:
            try:
                log.info("Injecting %d chars via %s", len(text), backend.name)
                backend.inject(text)
                return
            except InjectionError as exc:
                last_exc = exc
                if _is_vk_unsupported(exc):
                    if backend is self._direct:
                        self._direct_broken = True
                        log.warning(
                            "%s blocked by compositor (no virtual-keyboard protocol); "
                            "will avoid it for the rest of the session.", backend.name,
                        )
                    elif backend is self._clipboard:
                        self._clipboard_paste_broken = True
                        log.warning(
                            "%s paste blocked by compositor; switching to copy-only mode.",
                            backend.name,
                        )
                    continue
                # Other errors: try next backend too.
                log.warning("%s failed: %s; trying next backend", backend.name, exc)
                continue

        # All synthetic-input paths failed: copy-only.
        self._copy_only(text)
        # _copy_only always raises; the line below is unreachable but keeps mypy quiet.
        if last_exc:
            raise last_exc
