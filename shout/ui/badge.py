"""Floating microphone badge with hoverable language selector."""
from __future__ import annotations

import logging

from PySide6.QtCore import QPoint, Qt, Signal
from PySide6.QtGui import QGuiApplication, QIcon, QPixmap
from PySide6.QtWidgets import (
    QHBoxLayout,
    QLabel,
    QMenu,
    QPushButton,
    QToolButton,
    QWidget,
)

from ..config import BadgeConfig
from ..platform.atspi import caret_position
from ..platform.session import DisplayServer, SessionInfo
from .resources import icon_for_state
from .tray import lang_label

log = logging.getLogger(__name__)


class Badge(QWidget):
    language_changed = Signal(str)
    clicked = Signal()  # Click on mic icon = manual toggle

    def __init__(
        self,
        cfg: BadgeConfig,
        session: SessionInfo,
        languages: list[str],
        active: str,
    ) -> None:
        super().__init__(
            None,
            Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.Tool
            | Qt.WindowType.WindowStaysOnTopHint
            | Qt.WindowType.WindowDoesNotAcceptFocus,
        )
        self.cfg = cfg
        self.session = session
        self._languages = list(languages)
        self._active = active

        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating)

        self._mic = QPushButton()
        self._mic.setFlat(True)
        self._mic.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self._mic.setIconSize(self._mic.iconSize() * 1.2)
        self._mic.setIcon(QIcon(str(icon_for_state("idle"))))
        self._mic.clicked.connect(self.clicked.emit)

        self._lang_btn = QToolButton()
        self._lang_btn.setText("▾")
        self._lang_btn.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self._lang_btn.setPopupMode(QToolButton.ToolButtonPopupMode.InstantPopup)
        self._lang_menu = QMenu(self._lang_btn)
        self._lang_btn.setMenu(self._lang_menu)
        self._rebuild_menu()

        self._label = QLabel(lang_label(active))
        self._label.setStyleSheet("color: white; padding: 0 4px; font-weight: 600;")

        layout = QHBoxLayout(self)
        layout.setContentsMargins(8, 4, 8, 4)
        layout.setSpacing(4)
        layout.addWidget(self._mic)
        layout.addWidget(self._label)
        layout.addWidget(self._lang_btn)

        self.setStyleSheet(
            "QWidget { background: rgba(20, 20, 20, 200); border-radius: 12px; }"
            "QPushButton, QToolButton { background: transparent; border: none; color: white; }"
            "QToolButton::menu-indicator { image: none; }"
        )

    # --- Public --------------------------------------------------------------

    def set_state(self, state: str) -> None:
        self._mic.setIcon(QIcon(str(icon_for_state(state))))

    def set_languages(self, languages: list[str], active: str) -> None:
        self._languages = list(languages)
        self._active = active
        self._label.setText(lang_label(active))
        self._rebuild_menu()

    def show_at_best_position(self) -> None:
        """Position the badge using AT-SPI / cursor / corner fallback, then show."""
        pos = self._compute_position()
        self.move(pos)
        self.show()
        self.raise_()

    # --- Internals -----------------------------------------------------------

    def _rebuild_menu(self) -> None:
        self._lang_menu.clear()
        for code in self._languages:
            act = self._lang_menu.addAction(lang_label(code))
            act.setCheckable(True)
            act.setChecked(code == self._active)
            act.triggered.connect(lambda _c, code=code: self._on_lang_chosen(code))

    def _on_lang_chosen(self, code: str) -> None:
        self._active = code
        self._label.setText(lang_label(code))
        self.language_changed.emit(code)

    def _compute_position(self) -> QPoint:
        placement = self.cfg.placement
        # Force layout to know our size
        self.adjustSize()
        w, h = self.width() or 200, self.height() or 36

        if placement in ("auto",):
            xy = caret_position()
            if xy is not None:
                x, y = xy
                return QPoint(x + self.cfg.offset_x, y + self.cfg.offset_y)
            if self.session.server == DisplayServer.X11:
                cursor = self._x11_cursor()
                if cursor is not None:
                    cx, cy = cursor
                    return QPoint(cx + self.cfg.offset_x, cy + self.cfg.offset_y)
            placement = "corner"

        if placement == "cursor" and self.session.server == DisplayServer.X11:
            cursor = self._x11_cursor()
            if cursor is not None:
                cx, cy = cursor
                return QPoint(cx + self.cfg.offset_x, cy + self.cfg.offset_y)

        # Corner fallback
        screen = QGuiApplication.primaryScreen()
        geo = screen.availableGeometry() if screen else None
        if geo is None:
            return QPoint(self.cfg.offset_x, self.cfg.offset_y)
        ox, oy = self.cfg.offset_x, self.cfg.offset_y
        corner = self.cfg.corner
        if corner == "tl":
            return QPoint(geo.x() + ox, geo.y() + oy)
        if corner == "tr":
            return QPoint(geo.right() - w - ox, geo.y() + oy)
        if corner == "bl":
            return QPoint(geo.x() + ox, geo.bottom() - h - oy)
        return QPoint(geo.right() - w - ox, geo.bottom() - h - oy)  # br

    def _x11_cursor(self) -> tuple[int, int] | None:
        try:
            from Xlib import display
            d = display.Display()
            data = d.screen().root.query_pointer()
            return int(data.root_x), int(data.root_y)
        except Exception:  # noqa: BLE001
            return None


def make_pixmap_icon(path: str, size: int = 24) -> QIcon:
    pix = QPixmap(path)
    if not pix.isNull():
        pix = pix.scaled(size, size, Qt.AspectRatioMode.KeepAspectRatio,
                         Qt.TransformationMode.SmoothTransformation)
    return QIcon(pix)
