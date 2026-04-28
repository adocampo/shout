"""Widget to capture a hotkey accelerator from the user."""
from __future__ import annotations

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QKeyEvent, QKeySequence
from PySide6.QtWidgets import QPushButton

_MOD_BIT = (
    Qt.KeyboardModifier.ControlModifier
    | Qt.KeyboardModifier.AltModifier
    | Qt.KeyboardModifier.ShiftModifier
    | Qt.KeyboardModifier.MetaModifier
)


class HotkeyCaptureButton(QPushButton):
    accelerator_changed = Signal(str)

    def __init__(self, accelerator: str = "", parent=None) -> None:
        super().__init__(parent)
        self._capturing = False
        self.set_accelerator(accelerator)
        self.clicked.connect(self._begin_capture)

    def set_accelerator(self, accel: str) -> None:
        self._accelerator = accel
        self.setText(accel or "Pulsa para asignar…")

    def accelerator(self) -> str:
        return self._accelerator

    def _begin_capture(self) -> None:
        self._capturing = True
        self.setText("Pulsa la combinación…")
        self.setFocus(Qt.FocusReason.OtherFocusReason)
        self.grabKeyboard()

    def keyPressEvent(self, event: QKeyEvent) -> None:  # noqa: N802
        if not self._capturing:
            super().keyPressEvent(event)
            return
        key = event.key()
        # Ignore lone modifier presses
        if key in (
            Qt.Key.Key_Control, Qt.Key.Key_Shift, Qt.Key.Key_Alt,
            Qt.Key.Key_Meta, Qt.Key.Key_AltGr, Qt.Key.Key_Super_L, Qt.Key.Key_Super_R,
        ):
            return

        mods = event.modifiers() & _MOD_BIT
        seq = QKeySequence(int(mods) | int(key))
        accel = seq.toString(QKeySequence.SequenceFormat.PortableText)

        self._accelerator = accel
        self._capturing = False
        self.releaseKeyboard()
        self.setText(accel)
        self.accelerator_changed.emit(accel)
