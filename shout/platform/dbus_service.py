"""D-Bus service exposed by the running Shout instance, plus a client helper.

This gives a 100%-reliable hotkey path on Wayland: the user binds a manual
custom shortcut in their compositor (KDE Settings → Shortcuts → Custom →
"Run command: shout-stt --toggle") and it talks to the running instance
through the session bus.
"""
from __future__ import annotations

import logging

from PySide6.QtCore import QObject, Signal, Slot
from PySide6.QtDBus import QDBusConnection, QDBusMessage

log = logging.getLogger(__name__)

BUS_NAME = "org.shout.Shout"
OBJECT_PATH = "/org/shout/Shout"
INTERFACE = "org.shout.Shout"


class ShoutService(QObject):
    """Server-side object. Slots become D-Bus methods under `org.shout.Shout`."""

    toggle_requested = Signal()
    start_requested = Signal()
    stop_requested = Signal()
    cancel_requested = Signal()
    open_settings_requested = Signal()
    quit_requested = Signal()

    @Slot()
    def Toggle(self) -> None:  # noqa: N802 - D-Bus convention
        log.debug("D-Bus: Toggle")
        self.toggle_requested.emit()

    @Slot()
    def Start(self) -> None:  # noqa: N802
        log.debug("D-Bus: Start")
        self.start_requested.emit()

    @Slot()
    def Stop(self) -> None:  # noqa: N802
        log.debug("D-Bus: Stop")
        self.stop_requested.emit()

    @Slot()
    def Cancel(self) -> None:  # noqa: N802
        log.debug("D-Bus: Cancel")
        self.cancel_requested.emit()

    @Slot()
    def OpenSettings(self) -> None:  # noqa: N802
        log.debug("D-Bus: OpenSettings")
        self.open_settings_requested.emit()

    @Slot()
    def Quit(self) -> None:  # noqa: N802
        log.debug("D-Bus: Quit")
        self.quit_requested.emit()

    @Slot(result=str)
    def Ping(self) -> str:  # noqa: N802
        return "pong"


def register(service: ShoutService) -> bool:
    """Register the service on the session bus. Returns True on success."""
    bus = QDBusConnection.sessionBus()
    if not bus.isConnected():
        log.warning("Cannot register D-Bus service: session bus not connected")
        return False
    if not bus.registerService(BUS_NAME):
        log.warning(
            "Cannot register D-Bus service %s (already taken?). Another Shout running?",
            BUS_NAME,
        )
        return False
    options = (
        QDBusConnection.RegisterOption.ExportAllSlots
        | QDBusConnection.RegisterOption.ExportAllSignals
    )
    if not bus.registerObject(OBJECT_PATH, service, options):
        log.warning("Cannot register D-Bus object %s", OBJECT_PATH)
        bus.unregisterService(BUS_NAME)
        return False
    log.info("D-Bus service registered: %s %s", BUS_NAME, OBJECT_PATH)
    return True


def unregister() -> None:
    bus = QDBusConnection.sessionBus()
    if bus.isConnected():
        bus.unregisterObject(OBJECT_PATH)
        bus.unregisterService(BUS_NAME)


def is_running() -> bool:
    """True if another instance has already claimed the service name."""
    bus = QDBusConnection.sessionBus()
    if not bus.isConnected():
        return False
    msg = QDBusMessage.createMethodCall(
        "org.freedesktop.DBus", "/org/freedesktop/DBus",
        "org.freedesktop.DBus", "NameHasOwner",
    )
    msg.setArguments([BUS_NAME])
    reply = bus.call(msg)
    if reply.type() == QDBusMessage.MessageType.ErrorMessage:
        return False
    args = reply.arguments()
    return bool(args) and bool(args[0])


def call_remote(method: str) -> bool:
    """Call a method on the running Shout instance. Returns True on success."""
    bus = QDBusConnection.sessionBus()
    if not bus.isConnected():
        log.error("D-Bus session not available")
        return False
    msg = QDBusMessage.createMethodCall(BUS_NAME, OBJECT_PATH, INTERFACE, method)
    reply = bus.call(msg, mode=QDBusMessage.MessageType.MethodCallMessage, timeout=2000)
    if reply.type() == QDBusMessage.MessageType.ErrorMessage:
        log.error("D-Bus call %s failed: %s", method, reply.errorMessage())
        return False
    return True
