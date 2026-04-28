"""System tray icon and menu."""
from __future__ import annotations

import logging

from PySide6.QtCore import QObject, Signal
from PySide6.QtGui import QAction, QActionGroup, QIcon
from PySide6.QtWidgets import QMenu, QSystemTrayIcon

from .resources import icon_for_state

log = logging.getLogger(__name__)

# Common ISO -> human label
LANG_LABELS = {
    "es": "Español",
    "en": "English",
    "fr": "Français",
    "de": "Deutsch",
    "it": "Italiano",
    "pt": "Português",
    "ca": "Català",
    "gl": "Galego",
    "eu": "Euskara",
    "auto": "Auto-detect",
}


def lang_label(code: str) -> str:
    return LANG_LABELS.get(code, code)


class TrayIcon(QObject):
    open_settings_requested = Signal()
    quit_requested = Signal()
    language_changed = Signal(str)
    paused_changed = Signal(bool)

    def __init__(self, languages: list[str], active: str, parent=None) -> None:
        super().__init__(parent)
        self._tray = QSystemTrayIcon(QIcon(str(icon_for_state("idle"))))
        self._tray.setToolTip("Shout — voice dictation")
        self._menu = QMenu()
        self._lang_actions: dict[str, QAction] = {}
        self._lang_group = QActionGroup(self._menu)
        self._lang_group.setExclusive(True)
        self._build_menu(languages, active)
        self._tray.setContextMenu(self._menu)
        self._tray.activated.connect(self._on_activated)

    # --- Public --------------------------------------------------------------

    def show(self) -> None:
        self._tray.show()

    def hide(self) -> None:
        self._tray.hide()

    def is_visible(self) -> bool:
        return self._tray.isVisible()

    def set_state(self, state: str) -> None:
        self._tray.setIcon(QIcon(str(icon_for_state(state))))
        self._tray.setToolTip(f"Shout — {state}")

    def set_languages(self, languages: list[str], active: str) -> None:
        self._build_menu(languages, active)
        self._tray.setContextMenu(self._menu)

    # --- Internals -----------------------------------------------------------

    def _build_menu(self, languages: list[str], active: str) -> None:
        self._menu.clear()
        self._lang_actions.clear()

        lang_menu = self._menu.addMenu("Idioma")
        for code in languages:
            act = QAction(lang_label(code), self._menu)
            act.setCheckable(True)
            act.setChecked(code == active)
            act.triggered.connect(lambda _checked, c=code: self.language_changed.emit(c))
            self._lang_group.addAction(act)
            lang_menu.addAction(act)
            self._lang_actions[code] = act

        self._menu.addSeparator()
        pause = QAction("Pausar", self._menu)
        pause.setCheckable(True)
        pause.toggled.connect(self.paused_changed.emit)
        self._menu.addAction(pause)

        settings = QAction("Configuración…", self._menu)
        settings.triggered.connect(self.open_settings_requested.emit)
        self._menu.addAction(settings)

        self._menu.addSeparator()
        quit_act = QAction("Salir", self._menu)
        quit_act.triggered.connect(self.quit_requested.emit)
        self._menu.addAction(quit_act)

    def _on_activated(self, reason: QSystemTrayIcon.ActivationReason) -> None:
        if reason == QSystemTrayIcon.ActivationReason.Trigger:
            self.open_settings_requested.emit()
