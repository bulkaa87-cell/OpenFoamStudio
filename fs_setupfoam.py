"""Prepare OpenFOAM on this computer (WSL), without questions.

The installer starts the program with --prepare-openfoam (the task «Prepare
OpenFOAM in WSL», on by default), and the button «Install in WSL» of the
«Run» tab opens the same window. The steps:

1. WSL is not installed: `wsl --install -d Ubuntu-24.04` with administrator
   rights (Windows asks). Windows must restart then. The setting
   PENDING_KEY makes the program open this window again at the next start.
2. WSL has no Linux distribution with apt (Ubuntu, Debian): install
   Ubuntu-24.04 (`--no-launch`) and register it with the user root.
3. OpenFOAM: the newest openfoam<N>-default package of openfoam.com (not a
   release candidate), installed AS ROOT (`wsl -u root`). Thus apt asks for
   no sudo password, and the step needs no console.
4. The check: fs_backend.detect_wsl() must find OpenFOAM.

The steps are plain functions (plan(), the argv builders) and a worker that
runs them with an injectable runner: tests/test_setupfoam.py runs them with
a fake WSL.
"""

from __future__ import annotations

import os
import subprocess
import sys

from PySide6 import QtCore, QtGui, QtWidgets

import fs_backend as be
import fs_settings as fset
from fs_i18n import tr

FLAG = "--prepare-openfoam"
PENDING_KEY = "prepare_openfoam_pending"
DISTRO = "Ubuntu-24.04"

# The install script of fs_backend, as root: no sudo, no questions of apt.
# curl can be missing in a new distribution; add-debian-repo.sh needs it.
ROOT_SCRIPT = (
    "export DEBIAN_FRONTEND=noninteractive; "
    "apt-get update && "
    "(command -v curl >/dev/null || apt-get install -y curl ca-certificates) && "
    + be.INSTALL_SCRIPT.replace("sudo ", "")
)

_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


def _decode(raw: bytes) -> str:
    """wsl.exe writes its own messages in UTF-16; Linux programs in UTF-8."""
    text = raw.decode("utf-8", errors="replace")
    return text.replace("\x00", "")


def run_lines(argv, on_line, timeout: float | None = None) -> int:
    """Run argv, give each output line to on_line, return the exit code."""
    try:
        p = subprocess.Popen(argv, stdout=subprocess.PIPE,
                             stderr=subprocess.STDOUT,
                             stdin=subprocess.DEVNULL,
                             creationflags=_NO_WINDOW)
    except OSError as exc:
        on_line(str(exc))
        return 127
    assert p.stdout is not None
    for raw in iter(p.stdout.readline, b""):
        line = _decode(raw).rstrip()
        if line:
            on_line(line)
    try:
        return p.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        p.kill()
        return 124


def wsl_distros(run=run_lines) -> list | None:
    """The WSL distributions, or None when WSL is not installed."""
    lines: list = []
    rc = run(["wsl.exe", "-l", "-q"], lines.append)
    if rc != 0:
        return None
    return [ln.strip() for ln in lines if ln.strip()]


def apt_distro(distros) -> str:
    """The first distribution with apt (Ubuntu or Debian), or ""."""
    for d in distros or []:
        if any(k in d.lower() for k in ("ubuntu", "debian")):
            return d
    return ""


def plan(distros, ready: bool) -> list:
    """The step ids for this state: distros None = no WSL."""
    if ready:
        return ["check"]
    if distros is None:
        return ["wsl"]
    steps = [] if apt_distro(distros) else ["distro"]
    return steps + ["openfoam", "check"]


def root_argv(distro: str, script: str) -> list:
    """bash as root in the distribution. `-e`, not `--`: with `--` one more
    shell expands the variables of the script first, and $PKG, $p and $v
    become empty (fs_backend.wsl_argv; measured 2026-11-22: the package
    choice gave "" and apt-get then installed nothing with exit code 0)."""
    return ["wsl.exe", "-d", distro, "-u", "root", "-e", "bash", "-c", script]


def install_wsl_argv() -> list:
    """`wsl --install` with administrator rights; waits for the end."""
    return ["powershell.exe", "-NoProfile", "-Command",
            "Start-Process wsl.exe -Verb RunAs -Wait -ArgumentList "
            f"'--install','-d','{DISTRO}'"]


def install_distro_argv() -> list:
    return ["wsl.exe", "--install", "-d", DISTRO, "--no-launch"]


def register_argvs() -> list:
    """Make the new distribution usable with root (no user questions).
    The first one that works is enough."""
    exe = "ubuntu" + DISTRO.split("-")[-1].replace(".", "") + ".exe"
    return [[exe, "install", "--root"],
            ["wsl.exe", "-d", DISTRO, "-u", "root", "-e", "true"]]


class Worker(QtCore.QObject):
    """Runs the steps in a thread. `run` and `detect` are injectable."""

    line = QtCore.Signal(str)
    status = QtCore.Signal(str)
    done = QtCore.Signal(str, str)      # result id ("ok", "restart",
    #                                     "failed"), text for the person

    def __init__(self, run=run_lines, detect=None):
        super().__init__()
        self._run = run
        self._detect = detect or be.detect_wsl

    def _ready(self) -> list:
        return [b for b in self._detect() if b.ready]

    @QtCore.Slot()
    def work(self):
        try:
            self._work()
        except Exception as exc:                        # noqa: BLE001
            self.done.emit("failed", f"{type(exc).__name__}: {exc}")

    def _work(self):
        emit = self.line.emit
        if os.name != "nt":
            self.done.emit("failed", tr(
                "WSL exists only on Windows. On this system, install "
                "OpenFOAM with the packages of your Linux, or use a server."))
            return
        self.status.emit(tr("Checking WSL…"))
        distros = wsl_distros(self._run)
        ready = self._ready() if distros else []
        if ready:
            self.done.emit("ok", tr("OpenFOAM is ready: {what}",
                                    what=ready[0].label + " " + ready[0].version))
            return
        steps = plan(distros, False)
        if "wsl" in steps:
            self.status.emit(tr("Installing WSL and Ubuntu (Windows asks for "
                                "administrator rights)…"))
            rc = self._run(install_wsl_argv(), emit)
            fset.set(PENDING_KEY, True)
            if rc != 0:
                self.done.emit("failed", tr(
                    "WSL was not installed (code {rc}). Install it by hand: "
                    "open PowerShell as administrator and run “wsl --install”.",
                    rc=rc))
                return
            self.done.emit("restart", tr(
                "WSL is installed. Restart Windows, then start OpenFOAM "
                "Studio again: it continues with OpenFOAM by itself."))
            return
        distro = apt_distro(distros)
        if "distro" in steps:
            self.status.emit(tr("Installing {distro}…", distro=DISTRO))
            rc = self._run(install_distro_argv(), emit)
            if rc != 0:
                self.done.emit("failed", tr(
                    "{distro} was not installed (code {rc}).",
                    distro=DISTRO, rc=rc))
                return
            for argv in register_argvs():
                if self._run(argv, emit) == 0:
                    break
            distro = apt_distro(wsl_distros(self._run)) or DISTRO
        self.status.emit(tr("Installing OpenFOAM in {distro} (about 1-2 GB, "
                            "10-30 min)…", distro=distro))
        rc = self._run(root_argv(distro, ROOT_SCRIPT), emit)
        if rc != 0:
            self.done.emit("failed", tr(
                "The OpenFOAM install in {distro} ended with code {rc}. The "
                "log above shows why.", distro=distro, rc=rc))
            return
        self.status.emit(tr("Checking OpenFOAM…"))
        ready = self._ready()
        if not ready:
            self.done.emit("failed", tr(
                "The install ended, but OpenFOAM was not found in WSL."))
            return
        fset.set(PENDING_KEY, False)
        self.done.emit("ok", tr("OpenFOAM is ready: {what}",
                                what=ready[0].label + " " + ready[0].version))


class PrepareDialog(QtWidgets.QDialog):
    """The window with the log of the steps. It starts at once."""

    finished_ok = QtCore.Signal()

    def __init__(self, parent=None, run=run_lines, detect=None):
        super().__init__(parent)
        self.setWindowTitle(tr("Prepare OpenFOAM"))
        self.resize(720, 440)
        lay = QtWidgets.QVBoxLayout(self)
        self.lbl = QtWidgets.QLabel(tr("Starting…"))
        self.lbl.setWordWrap(True)
        lay.addWidget(self.lbl)
        self.bar = QtWidgets.QProgressBar()
        self.bar.setRange(0, 0)
        lay.addWidget(self.bar)
        self.log = QtWidgets.QPlainTextEdit()
        self.log.setReadOnly(True)
        self.log.setMaximumBlockCount(5000)
        font = QtGui.QFontDatabase.systemFont(QtGui.QFontDatabase.FixedFont)
        self.log.setFont(font)
        lay.addWidget(self.log, 1)
        self.btn = QtWidgets.QPushButton("Close")
        self.btn.setEnabled(False)
        self.btn.clicked.connect(self.accept)
        row = QtWidgets.QHBoxLayout()
        row.addStretch(1)
        row.addWidget(self.btn)
        lay.addLayout(row)
        self.result = ""
        self._thread = QtCore.QThread(self)
        self._worker = Worker(run, detect)
        self._worker.moveToThread(self._thread)
        self._thread.started.connect(self._worker.work)
        self._worker.line.connect(self.log.appendPlainText)
        self._worker.status.connect(self._status)
        self._worker.done.connect(self._done)
        self._thread.start()

    def _status(self, text: str):
        self.lbl.setText(text)
        self.log.appendPlainText("== " + text)

    def _done(self, result: str, text: str):
        self.result = result
        self._thread.quit()
        self._thread.wait(5000)
        self.bar.setRange(0, 1)
        self.bar.setValue(1)
        self.lbl.setText(text)
        self.log.appendPlainText("== " + text)
        self.btn.setEnabled(True)
        if result == "ok":
            self.finished_ok.emit()

    def reject(self):
        # Esc / the close box: not during a step (apt must not stop in the
        # middle); the button «Close» is on at the end.
        if self.btn.isEnabled():
            super().reject()

    def closeEvent(self, event):                          # noqa: N802
        if self.btn.isEnabled():
            event.accept()
        else:
            event.ignore()


def wanted_at_start(argv=None) -> bool:
    """Open the window at the start: the installer flag, or a step that
    waits for the restart of Windows."""
    argv = sys.argv[1:] if argv is None else argv
    return FLAG in argv or bool(fset.get(PENDING_KEY))

