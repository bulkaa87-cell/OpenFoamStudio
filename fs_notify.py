"""Simple notifications of the program: a run ended, failed, stopped, the
results are downloaded (T44).

The core has no Qt. The window, a run queue or a command line tool call
the same function:

    import fs_notify
    fs_notify.notify("done", tr("Run finished"), tr("{name}: done", name=n),
                     case=case_dir)

- `kind`: one of KINDS ("done", "fail", "stop", "fetched", "info").
- `title`, `text`: texts for the person. Give a TrStr (`tr()`): the line in
  the window then changes with the interface language.
- `case`: the local case folder, or "". A click on the notification opens it.
- `action`: optional, for the sink of the window (for example
  ("attach", host, remote_dir)). Without it, a click opens `case`.

`notify()` returns the Note, or None when the notifications are off
(«File → Settings», key "notify"). Each sink (`add_sink`) gets each Note.
Without a sink (a command line tool), the Note goes to stderr as one line.

The Qt part (`install`) is only for the window: a tray icon with the logo
of the program, a tray message (a Windows toast; macOS and Linux show it if
the desktop has a tray) and a line in the status bar. A click on the
message or on the line calls `on_click(note)` and brings the window up.
Qt is imported only in `install`.
"""

from __future__ import annotations

import os
import sys
import threading
import time
from dataclasses import dataclass, field

KINDS = ("done", "fail", "stop", "fetched", "info")
# The setting that turns the notifications off (fs_settings.DEFAULTS).
SETTING = "notify"
_KEEP = 30


@dataclass
class Note:
    kind: str
    title: str
    text: str
    case: str = ""
    action: "tuple | None" = None
    at: float = field(default_factory=time.time)


_sinks: list = []
_recent: "list[Note]" = []
_lock = threading.Lock()


def enabled() -> bool:
    """False when the person turned the notifications off."""
    try:
        import fs_settings
        return bool(fs_settings.get(SETTING, True))
    except Exception:                                      # noqa: BLE001
        return True


def add_sink(fn) -> None:
    """`fn(note)` gets each Note (in the thread of the caller)."""
    with _lock:
        if fn not in _sinks:
            _sinks.append(fn)


def remove_sink(fn) -> None:
    with _lock:
        if fn in _sinks:
            _sinks.remove(fn)


def recent() -> "list[Note]":
    """The last notes, the newest last."""
    with _lock:
        return list(_recent)


def _console(note: Note) -> None:
    try:
        print(f"[OpenFOAM Studio] {note.title}: {note.text}", file=sys.stderr)
    except (OSError, ValueError):
        pass


def notify(kind: str, title: str, text: str = "", case: str = "",
           action: "tuple | None" = None) -> "Note | None":
    """Tell the person about an event of a run. See the module docstring."""
    if kind not in KINDS:
        kind = "info"
    if not enabled():
        return None
    note = Note(kind=kind, title=title, text=text, case=str(case or ""),
                action=action)
    with _lock:
        _recent.append(note)
        del _recent[:-_KEEP]
        sinks = list(_sinks)
    if not sinks:
        _console(note)
    for fn in sinks:
        try:
            fn(note)
        except Exception:                                  # noqa: BLE001
            pass
    return note


# --------------------------------------------------------------------------
# Qt: tray icon, tray message, line in the status bar (only the window)
# --------------------------------------------------------------------------

_COLORS = {"done": "#4caf50", "fetched": "#4caf50", "fail": "#e05252",
           "stop": "#d49a00", "info": "#4a9eda"}


def install(window, on_click=None, status_bar=None):
    """Show the notes of this process in `window`. Returns the Notifier.

    `on_click(note)`: a click on the tray message or on the line. The window
    is brought up first. `status_bar`: where the line goes (default: the
    status bar of `window`).
    """
    from PySide6 import QtCore, QtWidgets
    import fs_icons
    import fs_statusbar as sbar

    class NoteLine(sbar.StatusText):
        """The line in the status bar: the last note, a click opens it."""
        clicked = QtCore.Signal()

        def __init__(self):
            super().__init__("")
            self.setCursor(QtCore.Qt.PointingHandCursor)
            self.setVisible(False)

        def mousePressEvent(self, event):                  # noqa: N802
            if event.button() == QtCore.Qt.LeftButton:
                self.clicked.emit()
            super().mousePressEvent(event)

    class Notifier(QtCore.QObject):
        posted = QtCore.Signal(object)

        def __init__(self):
            super().__init__(window)
            self.window = window
            self.on_click = on_click
            self.last: "Note | None" = None
            self.tray = None
            self.line = NoteLine()
            self.line.setToolTip(
                "Click to open the run. «File → Settings» turns the "
                "notifications off.")
            self.line.clicked.connect(self._line_clicked)
            (status_bar or window.statusBar()).addPermanentWidget(self.line)
            # A note can come from a thread: Qt runs _show in the thread of
            # the window.
            self.posted.connect(self._show, QtCore.Qt.QueuedConnection)
            add_sink(self._sink)
            window.destroyed.connect(lambda *_: remove_sink(self._sink))

        def _sink(self, note: Note) -> None:
            try:
                self.posted.emit(note)
            except RuntimeError:                           # window is gone
                remove_sink(self._sink)

        def _tray(self):
            if self.tray is not None:
                return self.tray
            # FS_NO_TRAY=1: no tray (the tests, tests/_isolate.py).
            if (os.environ.get("FS_NO_TRAY", "").strip() not in ("", "0")
                    or not QtWidgets.QSystemTrayIcon.isSystemTrayAvailable()):
                return None
            tray = QtWidgets.QSystemTrayIcon(
                fs_icons.icon(fs_icons.OFS), self)
            tray.setToolTip("OpenFOAM Studio")
            tray.messageClicked.connect(self._message_clicked)
            tray.activated.connect(self._tray_activated)
            tray.show()
            self.tray = tray
            return tray

        def wants_message(self) -> bool:
            """A tray message only when the person does not look at the
            window: the line in the window is enough then."""
            w = self.window
            return not (w.isVisible() and w.isActiveWindow()
                        and not w.isMinimized())

        @QtCore.Slot(object)
        def _show(self, note: Note) -> None:
            self.last = note
            color = _COLORS.get(note.kind, _COLORS["info"])
            self.line.setStyleSheet(f"color:{color}; font-weight:600;")
            self.line.setText(note.title + ": " + note.text
                              if note.text else note.title)
            self.line.setVisible(True)
            if not self.wants_message():
                return
            tray = self._tray()
            if tray is None or not tray.supportsMessages():
                return
            icon = {"fail": QtWidgets.QSystemTrayIcon.Warning,
                    "stop": QtWidgets.QSystemTrayIcon.Warning}.get(
                note.kind, QtWidgets.QSystemTrayIcon.Information)
            tray.showMessage(str(note.title), str(note.text), icon, 15000)

        def bring_up(self) -> None:
            w = self.window
            if w.isMinimized():
                w.showNormal()
            w.show()
            w.raise_()
            w.activateWindow()

        def _open(self, note: "Note | None") -> None:
            self.bring_up()
            if note is not None and self.on_click is not None:
                self.on_click(note)

        def _message_clicked(self) -> None:
            self._open(self.last)

        def _tray_activated(self, reason) -> None:
            if reason in (QtWidgets.QSystemTrayIcon.Trigger,
                          QtWidgets.QSystemTrayIcon.DoubleClick):
                self.bring_up()

        def _line_clicked(self) -> None:
            note, self.last = self.last, None
            self.line.setVisible(False)
            self._open(note)

        def clear(self) -> None:
            self.last = None
            self.line.setVisible(False)

    return Notifier()
