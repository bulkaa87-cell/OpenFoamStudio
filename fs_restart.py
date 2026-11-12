"""A change of the interface language, and the restart of the program (T27).

Since T28 the language changes LIVE (fs_i18n.set_language): each widget
keeps the key of its text and is translated again. This module stays for
two cases: FS_LANG of the environment wins over the setting (then
`ask_restart()` only tells how to change the language), and `restart()`,
which closes the main window (closeEvent saves the session and asks about
an unsaved project) and starts the same program again with `RESTORE_FLAG`.
The new process restores the session also when «Відновлювати останню
сесію» is off (`restore_requested()`).

The texts go through fs_i18n.tr (i18n/uk/fs_restart.json).
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

from PySide6 import QtCore, QtWidgets

from fs_i18n import tr
import fs_i18n

# The Qt text setters translate and keep the key of each text, also in a
# widget that is made before the main window (T28).
fs_i18n.install()

RESTORE_FLAG = "--restore-session"


def restore_requested(argv=None) -> bool:
    """True if this process was started by `restart()`."""
    return RESTORE_FLAG in (sys.argv if argv is None else argv)


def env_language() -> str:
    """FS_LANG of the environment ("" if not set): it overrides the setting."""
    return (os.environ.get("FS_LANG") or "").strip()


def ask_restart(parent) -> bool:
    """Tell that the new language needs a restart. True: restart now."""
    env = env_language()
    if env:
        QtWidgets.QMessageBox.information(
            parent, tr("Interface language"),
            tr("The environment variable FS_LANG={value} has priority "
               "over the setting. To change the language, remove this "
               "variable and restart the program.", value=env))
        return False
    box = QtWidgets.QMessageBox(parent)
    box.setIcon(QtWidgets.QMessageBox.Information)
    box.setWindowTitle(tr("Interface language"))
    box.setText(tr("The language changes after you restart the program."))
    box.setInformativeText(tr("Before the restart, the program saves the "
                              "session. Then it opens the session again."))
    now = box.addButton(tr("Restart now"),
                        QtWidgets.QMessageBox.AcceptRole)
    box.addButton(tr("Later"), QtWidgets.QMessageBox.RejectRole)
    box.setDefaultButton(now)
    box.exec()
    return box.clickedButton() is now


def command(script) -> tuple[str, list[str]]:
    """The program and the arguments that start this program again."""
    if getattr(sys, "frozen", False):      # installed copy: the exe itself
        return sys.executable, [RESTORE_FLAG]
    return sys.executable, [str(Path(script).resolve()), RESTORE_FLAG]


def _start_detached(program: str, args: list[str], cwd: str) -> bool:
    res = QtCore.QProcess.startDetached(program, args, cwd)
    return bool(res[0] if isinstance(res, tuple) else res)


def restart(window, script) -> bool:
    """Close `window` and start the program again.

    False if the window did not close (the person selected «Скасувати», or
    the program is busy) or if the new process did not start.
    """
    window.close()
    if window.isVisible():
        window.statusBar().showMessage(tr("The restart is cancelled."), 8000)
        return False
    program, args = command(script)
    if not _start_detached(program, args, str(Path(script).resolve().parent)):
        QtWidgets.QMessageBox.critical(
            None, tr("Interface language"),
            tr("The program did not start again. Start it manually."))
        return False
    app = QtWidgets.QApplication.instance()
    if app is not None:
        app.quit()
    return True
