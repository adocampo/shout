"""Shout — application bootstrap.

Wires Core <-> Platform <-> UI together. Listens to the global hotkey,
manages the dictation session and presents the badge / tray.
"""
from __future__ import annotations

import argparse
import logging
import os
import signal
import sys
import traceback

from PySide6.QtCore import QObject, Qt, QTimer, Signal
from PySide6.QtGui import QGuiApplication
from PySide6.QtWidgets import QApplication, QMessageBox, QSystemTrayIcon

from . import config as cfg_mod
from .core import models as models_mod
from .core.session import DictationSession, State
from .logging_setup import setup_logging
from .platform import autostart as autostart_mod
from .platform import dbus_service
from .platform import desktop_install
from .platform.hotkey import make_hotkey_provider
from .platform.injector import CompositeInjector, InjectionError, ManualPasteRequired
from .platform.session import detect_session
from .ui.badge import Badge
from .ui.settings import SettingsWindow
from .ui.tray import TrayIcon

log = logging.getLogger(__name__)


class _Bridge(QObject):
    """Marshals callbacks from background threads onto the Qt event loop."""

    hotkey_pressed = Signal()
    hotkey_released = Signal()
    state_changed = Signal(str, str)
    inject_requested = Signal(str)


class App:
    def __init__(self, qt_app: QApplication, disable_hotkey: bool = False) -> None:
        self.qt = qt_app
        self.cfg = cfg_mod.load()
        self.session_info = detect_session()
        self._disable_hotkey = disable_hotkey

        # Bridge for thread-safe signals
        self.bridge = _Bridge()
        self.bridge.hotkey_pressed.connect(self._on_hotkey_press)
        self.bridge.hotkey_released.connect(self._on_hotkey_release)
        self.bridge.state_changed.connect(self._on_state_changed)
        self.bridge.inject_requested.connect(self._inject_text)

        # Injection
        self.injector = CompositeInjector(self.session_info, self.cfg.injection)

        # Tray
        self.tray = TrayIcon(self.cfg.languages.enabled, self.cfg.languages.active)
        self.tray.open_settings_requested.connect(self.open_settings)
        self.tray.quit_requested.connect(self.quit)
        self.tray.language_changed.connect(self._on_language_changed)
        self.tray.paused_changed.connect(self._on_paused)
        if self.cfg.tray.enabled and QSystemTrayIcon.isSystemTrayAvailable():
            self.tray.show()

        # Badge
        self.badge: Badge | None = None
        if self.cfg.badge.enabled:
            self.badge = Badge(
                self.cfg.badge, self.session_info,
                self.cfg.languages.enabled, self.cfg.languages.active,
            )
            self.badge.language_changed.connect(self._on_language_changed)
            self.badge.clicked.connect(self._toggle)

        # Dictation session
        try:
            model_path = models_mod.model_path(self.cfg.models.selected)
        except ValueError:
            model_path = models_mod.models_dir() / "missing.bin"
        self.dictation = DictationSession(
            self.cfg, model_path,
            text_sink=lambda t: self.bridge.inject_requested.emit(t),
            listener=lambda s, m: self.bridge.state_changed.emit(s.value, m),
        )

        self.paused = False
        self.settings_window: SettingsWindow | None = None

        # D-Bus service: lets `shout-stt --toggle` (etc.) talk to this instance
        # and lets the user bind a manual KDE/GNOME shortcut as a fallback to
        # the portal hotkey.
        self.dbus = dbus_service.ShoutService()
        self.dbus.toggle_requested.connect(self._toggle)
        self.dbus.start_requested.connect(self._start)
        self.dbus.stop_requested.connect(self.dictation.stop)
        self.dbus.cancel_requested.connect(self.dictation.cancel)
        self.dbus.open_settings_requested.connect(self.open_settings)
        self.dbus.quit_requested.connect(self.quit)
        if not dbus_service.register(self.dbus):
            log.warning("D-Bus service NOT registered. `shout-stt --toggle` will not work.")

        # Hotkey
        self.hotkey = make_hotkey_provider(self.session_info, self.cfg.hotkey)
        if self._disable_hotkey:
            log.warning("Hotkey disabled by --no-hotkey flag")
        else:
            try:
                self.hotkey.start(
                    on_press=lambda: self.bridge.hotkey_pressed.emit(),
                    on_release=(lambda: self.bridge.hotkey_released.emit())
                    if self.cfg.hotkey.mode == "push_to_talk" and self.hotkey.supports_release
                    else None,
                )
                log.info(
                    "Hotkey provider started: mode=%s supports_release=%s",
                    self.cfg.hotkey.mode, self.hotkey.supports_release,
                )
            except Exception as exc:  # noqa: BLE001
                log.error("Could not start hotkey provider: %s", exc, exc_info=True)
        if self.cfg.hotkey.mode == "push_to_talk" and not self.hotkey.supports_release:
            log.warning(
                "Push-to-talk requested but the active backend does not support key release; "
                "behaving as toggle. See plan.md / TODO.md."
            )

    # --- Hotkey handlers -----------------------------------------------------

    def _on_hotkey_press(self) -> None:
        if self.paused:
            return
        if self.cfg.hotkey.mode == "push_to_talk" and self.hotkey.supports_release:
            self._start()
        else:
            self._toggle()

    def _on_hotkey_release(self) -> None:
        if self.cfg.hotkey.mode == "push_to_talk":
            self.dictation.stop()

    def _toggle(self) -> None:
        self.dictation.toggle(self.cfg.languages.active)
        if self.dictation.state == State.RECORDING and self.badge is not None:
            self.badge.show_at_best_position()

    def _start(self) -> None:
        self.dictation.start(self.cfg.languages.active)
        if self.badge is not None:
            self.badge.show_at_best_position()

    # --- State transitions ---------------------------------------------------

    def _on_state_changed(self, state: str, message: str) -> None:
        log.debug("UI state: %s (%s)", state, message)
        self.tray.set_state(state)
        if self.badge is not None:
            self.badge.set_state(state)
            if state == "idle":
                # Hide the badge after a short delay so the user sees the final state
                QTimer.singleShot(400, self.badge.hide)

    def _inject_text(self, text: str) -> None:
        # Hide the floating badge BEFORE injecting. On KDE Wayland the badge,
        # being an always-on-top tool window, can intercept synthesized keys
        # (ydotool/wtype/xdotool) instead of the real focused text field.
        if self.badge is not None and self.badge.isVisible():
            self.badge.hide()
            # Pump the event loop so the compositor processes the unmap and
            # restores keyboard focus to the previously-focused window.
            self.qt.processEvents()
        try:
            self.injector.inject(text)
        except ManualPasteRequired as exc:
            log.info("Manual paste required: %s", exc)
            self.tray._tray.showMessage(
                "Shout", str(exc),
                QSystemTrayIcon.MessageIcon.Information, 5000,
            )
        except InjectionError as exc:
            log.error("Injection failed: %s", exc)
            self.tray._tray.showMessage(
                "Shout", f"No se pudo escribir el texto: {exc}",
                QSystemTrayIcon.MessageIcon.Warning, 4000,
            )

    # --- UI actions ----------------------------------------------------------

    def open_settings(self) -> None:
        if self.settings_window is None:
            self.settings_window = SettingsWindow(self.cfg)
            self.settings_window.applied.connect(self._on_settings_applied)
        self.settings_window.show()
        self.settings_window.raise_()
        self.settings_window.activateWindow()

    def _on_settings_applied(self) -> None:
        # Refresh derived state. A robust impl would diff and restart subsystems
        # only when needed; for v1 we restart the hotkey and rebuild menus.
        self.tray.set_languages(self.cfg.languages.enabled, self.cfg.languages.active)
        if self.badge is not None:
            self.badge.set_languages(self.cfg.languages.enabled, self.cfg.languages.active)

        # Restart hotkey
        try:
            self.hotkey.stop()
        except Exception:  # noqa: BLE001
            pass
        self.hotkey = make_hotkey_provider(self.session_info, self.cfg.hotkey)
        self.hotkey.start(
            on_press=lambda: self.bridge.hotkey_pressed.emit(),
            on_release=(lambda: self.bridge.hotkey_released.emit())
            if self.cfg.hotkey.mode == "push_to_talk" and self.hotkey.supports_release
            else None,
        )

        # Rebuild injector (backend may have changed)
        self.injector = CompositeInjector(self.session_info, self.cfg.injection)

        # Rebuild dictation session if model changed
        try:
            model_path = models_mod.model_path(self.cfg.models.selected)
        except ValueError:
            model_path = models_mod.models_dir() / "missing.bin"
        if model_path != self.dictation.model_path:
            self.dictation = DictationSession(
                self.cfg, model_path,
                text_sink=self._inject_text,
                listener=lambda s, m: self.bridge.state_changed.emit(s.value, m),
            )

    def _on_language_changed(self, code: str) -> None:
        self.cfg.languages.active = code
        try:
            cfg_mod.save(self.cfg)
        except Exception as exc:  # noqa: BLE001
            log.warning("Could not persist language change: %s", exc)
        self.tray.set_languages(self.cfg.languages.enabled, code)
        if self.badge is not None:
            self.badge.set_languages(self.cfg.languages.enabled, code)

    def _on_paused(self, paused: bool) -> None:
        self.paused = paused
        log.info("Paused=%s", paused)

    def quit(self) -> None:
        log.info("Quitting")
        try:
            self.hotkey.stop()
        except Exception:  # noqa: BLE001
            pass
        try:
            dbus_service.unregister()
        except Exception:  # noqa: BLE001
            pass
        self.qt.quit()


# --- Entry point -------------------------------------------------------------

def main(argv: list[str] | None = None) -> int:
    setup_logging()
    parser = argparse.ArgumentParser(prog="shout", description="Linux voice dictation")
    parser.add_argument("--debug", action="store_true", help="Verbose logging")
    parser.add_argument("--no-hotkey", action="store_true", help="Disable global hotkey (debug)")
    parser.add_argument("--settings", action="store_true", help="Open settings window on start")
    parser.add_argument("--install-desktop-file", action="store_true",
                        help="Install ~/.local/share/applications/shout-stt.desktop and exit")
    parser.add_argument("--toggle", action="store_true",
                        help="Send Toggle to the running instance and exit (use this from a global shortcut)")
    parser.add_argument("--start", action="store_true",
                        help="Send Start to the running instance and exit")
    parser.add_argument("--stop", action="store_true",
                        help="Send Stop to the running instance and exit")
    parser.add_argument("--cancel", action="store_true",
                        help="Send Cancel to the running instance and exit")
    parser.add_argument("--quit", action="store_true",
                        help="Send Quit to the running instance and exit")
    args = parser.parse_args(argv)
    if args.debug:
        logging.getLogger().setLevel(logging.DEBUG)

    # --- Standalone helpers (no GUI) ----------------------------------------
    if args.install_desktop_file:
        path = desktop_install.install(force=True)
        if path is None:
            print("Could not install desktop file", file=sys.stderr)
            return 1
        print(f"Installed: {path}")
        return 0

    # --- Client mode (talk to the running instance over D-Bus) -------------
    client_methods = []
    if args.toggle: client_methods.append("Toggle")
    if args.start: client_methods.append("Start")
    if args.stop: client_methods.append("Stop")
    if args.cancel: client_methods.append("Cancel")
    if args.quit: client_methods.append("Quit")
    if client_methods:
        # We need a QCoreApplication for QtDBus to work.
        from PySide6.QtCore import QCoreApplication
        _ = QCoreApplication.instance() or QCoreApplication(sys.argv)
        if not dbus_service.is_running():
            print(
                "Shout is not running. Start it first with `shout-stt`.",
                file=sys.stderr,
            )
            return 3
        ok = all(dbus_service.call_remote(m) for m in client_methods)
        return 0 if ok else 1

    # Reduce PortAudio JACK noise on stderr unless debugging.
    if not args.debug:
        os.environ.setdefault("JACK_NO_AUDIO_RESERVATION", "1")
        os.environ.setdefault("JACK_NO_START_SERVER", "1")

    # Qt boilerplate
    QGuiApplication.setApplicationName("Shout")
    QGuiApplication.setApplicationDisplayName("Shout")
    # MUST match the basename of the .desktop file so xdg-desktop-portal can
    # resolve our app id ("App info not found" error otherwise).
    QGuiApplication.setDesktopFileName("shout-stt")
    qt_app = QApplication.instance() or QApplication(sys.argv)
    qt_app.setQuitOnLastWindowClosed(False)
    qt_app.setAttribute(Qt.ApplicationAttribute.AA_DontShowIconsInMenus, False)

    # Refuse to run twice — D-Bus name is single-owner.
    if dbus_service.is_running():
        print(
            "Shout is already running. Use `shout-stt --toggle` to dictate, "
            "or `shout-stt --quit` to stop it.",
            file=sys.stderr,
        )
        return 4

    # First-run: install the desktop file so the portal recognises us.
    if not desktop_install.is_installed():
        desktop_install.install()

    try:
        app = App(qt_app, disable_hotkey=args.no_hotkey)
    except Exception:  # noqa: BLE001
        log.error("Fatal error during startup:\n%s", traceback.format_exc())
        QMessageBox.critical(
            None, "Shout",
            "Shout no pudo arrancar. Detalles en "
            "~/.local/state/shout/shout.log\n\n" + traceback.format_exc(limit=3),
        )
        return 1

    # Allow Ctrl+C in the controlling terminal
    signal.signal(signal.SIGINT, lambda *_: app.quit())
    timer = QTimer()
    timer.start(250)
    timer.timeout.connect(lambda: None)  # let Python signal handlers run

    tray_ok = QSystemTrayIcon.isSystemTrayAvailable()
    log.info(
        "Shout %s ready. tray_available=%s tray_enabled=%s badge_enabled=%s hotkey=%r",
        __import__("shout").__version__, tray_ok, app.cfg.tray.enabled,
        app.cfg.badge.enabled, app.cfg.hotkey.accelerator,
    )
    print(
        f"Shout en marcha. Atajo global: {app.cfg.hotkey.accelerator}. "
        f"{'Bandeja activa.' if (tray_ok and app.cfg.tray.enabled) else 'Sin bandeja.'} "
        "Cierra con Ctrl+C o desde el menú de la bandeja.\n"
        "Si el atajo no funciona en Wayland, configura un atajo personalizado "
        "en KDE/GNOME que invoque: `shout-stt --toggle`.",
        file=sys.stderr, flush=True,
    )

    if args.settings or (not tray_ok and not app.cfg.badge.enabled):
        app.open_settings()

    if not tray_ok and not app.cfg.badge.enabled:
        QMessageBox.warning(
            None, "Shout",
            "No hay bandeja del sistema disponible y el badge está desactivado. "
            "Se ha abierto la configuración.",
        )

    return qt_app.exec()


if __name__ == "__main__":
    sys.exit(main())
