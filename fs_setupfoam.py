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

REMOVE (the uninstaller starts the program with --remove-backend, and
--remove-results when the person said yes): the program removes ONLY what
it installed itself. The file MARKER (next to the settings) records it at
the install: the distribution (when the program installed it), the
OpenFOAM package and the new apt files of the openfoam.com repository.

- A distribution that the program installed: `wsl --unregister` (all of
  it goes, also the working copies of the cases in it).
- A distribution of the person: only the package (apt purge, autoremove)
  and the repository files.
- WSL itself stays: it is a part of Windows, other programs can use it.
- --remove-results: the folders cases, projects, session and meshes of
  fs_paths.data_dir() (Documents/OpenFOAM Studio) and ~/openfoam-studio in
  each WSL distribution. Servers are not touched.

The steps are plain functions (plan(), the argv builders) and workers that
run them with an injectable runner: tests/test_setupfoam.py runs them with
a fake WSL.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

from PySide6 import QtCore, QtGui, QtWidgets

import fs_backend as be
import fs_paths
import fs_settings as fset
from fs_i18n import tr

FLAG = "--prepare-openfoam"
REMOVE_FLAG = "--remove-backend"
RESULTS_FLAG = "--remove-results"
MARKER = "installed-backend.json"
# The data folders of fs_paths.data_dir() that --remove-results deletes.
RESULT_DIRS = ("cases", "projects", "session", "meshes")
# The apt files of a repository: the difference before/after the install
# is what add-debian-repo.sh added.
APT_FILES_SCRIPT = ("find /etc/apt/sources.list.d /etc/apt/keyrings "
                    "/etc/apt/trusted.gpg.d /usr/share/keyrings -maxdepth 1 "
                    "-type f 2>/dev/null; true")
PENDING_KEY = "prepare_openfoam_pending"
DISTRO = "Ubuntu-24.04"

# The install script of fs_backend, as root: no sudo, no questions of apt.
# curl can be missing in a new distribution; add-debian-repo.sh needs it.
ROOT_SCRIPT = (
    "export DEBIAN_FRONTEND=noninteractive; "
    "apt-get update && "
    "(command -v curl >/dev/null || apt-get install -y curl ca-certificates) && "
    # APT::Status-Fd=1: apt writes «dlstatus:…:<percent>:…» and
    # «pmstatus:…:<percent>:…» lines: the percent of the progress bar.
    + be.INSTALL_SCRIPT.replace("sudo ", "").replace(
        'apt-get install -y "$PKG"',
        'apt-get install -y -o APT::Status-Fd=1 "$PKG"')
    # An ASCII line for the record of the installed package (MARKER).
    + ' && echo "OFS_PACKAGE=$PKG"'
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


_PCT = re.compile(r"(\d{1,3}(?:[.,]\d+)?)\s*%")


def apt_progress(line: str):
    """(percent 0..100 or None, keep the line in the log). The download is
    the first half of the bar, the install the second half."""
    for head, base in (("dlstatus:", 0.0), ("pmstatus:", 50.0)):
        if line.startswith(head):
            parts = line.split(":")
            try:
                return base + float(parts[2]) / 2.0, False
            except (IndexError, ValueError):
                return None, False
    return None, True


def wsl_progress(line: str):
    """The percent of a line of «wsl --install» (e.g. «[====  45.0%  ]»)."""
    m = _PCT.search(line)
    if not m:
        return None
    try:
        v = float(m.group(1).replace(",", "."))
    except ValueError:
        return None
    return v if 0.0 <= v <= 100.0 else None


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


def marker_path() -> Path:
    return fset.path().parent / MARKER


def read_marker() -> dict:
    try:
        return json.loads(marker_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def write_marker(data: dict) -> None:
    path = marker_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=1), encoding="utf-8")


def remove_script(package: str, files) -> str:
    """Root script: purge the package and its dependencies, delete the
    repository files that the install added."""
    rm = " ".join("'" + f.replace("'", "") + "'" for f in files
                  if f.startswith("/etc/apt/") or f.startswith("/usr/share/keyrings/"))
    script = ("export DEBIAN_FRONTEND=noninteractive; "
              f"apt-get remove --purge -y '{package}' && "
              "apt-get autoremove --purge -y")
    if rm:
        script += f" && rm -f {rm} && (apt-get update -qq || true)"
    return script


class Worker(QtCore.QObject):
    """Runs the steps in a thread. `run` and `detect` are injectable."""

    line = QtCore.Signal(str)
    status = QtCore.Signal(str)
    # 0..100: the percent of the step; -1: the step has no percent (busy).
    progress = QtCore.Signal(int)
    done = QtCore.Signal(str, str)      # result id ("ok", "restart",
    #                                     "failed"), text for the person

    def __init__(self, run=run_lines, detect=None):
        super().__init__()
        self._run = run
        self._detect = detect or be.detect_wsl

    def _ready(self) -> list:
        return [b for b in self._detect() if b.ready]

    def _apt_files(self, distro: str) -> list:
        lines: list = []
        self._run(root_argv(distro, APT_FILES_SCRIPT), lines.append)
        return [ln.strip() for ln in lines if ln.strip().startswith("/")]

    def _step(self, text) -> None:
        self.progress.emit(-1)
        self.status.emit(text)

    def _wsl_emit(self, line: str) -> None:
        v = wsl_progress(line)
        if v is not None:
            self.progress.emit(int(v))
        self.line.emit(line)

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
            self._step(tr("Installing WSL and Ubuntu (Windows asks for "
                          "administrator rights)…"))
            rc = self._run(install_wsl_argv(), self._wsl_emit)
            fset.set(PENDING_KEY, True)
            if rc == 0:
                write_marker({"wsl_installed": True, "distro": DISTRO,
                              "distro_installed": True})
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
            self._step(tr("Installing {distro}…", distro=DISTRO))
            rc = self._run(install_distro_argv(), self._wsl_emit)
            if rc != 0:
                self.done.emit("failed", tr(
                    "{distro} was not installed (code {rc}).",
                    distro=DISTRO, rc=rc))
                return
            for argv in register_argvs():
                if self._run(argv, emit) == 0:
                    break
            distro = apt_distro(wsl_distros(self._run)) or DISTRO
            mark = read_marker()
            mark.update(distro=distro, distro_installed=True)
            write_marker(mark)
        self._step(tr("Installing OpenFOAM in {distro} (about 1-2 GB, "
                      "10-30 min)…", distro=distro))
        before = self._apt_files(distro)
        got = []

        def emit_pkg(line):
            m = re.match(r"OFS_PACKAGE=(openfoam[0-9]+-default)\s*$",
                         line.strip())
            if m:
                got.append(m.group(1))
            pct, keep = apt_progress(line)
            if pct is not None:
                self.progress.emit(int(pct))
            if keep:
                emit(line)
        rc = self._run(root_argv(distro, ROOT_SCRIPT), emit_pkg)
        if got:
            mark = read_marker()
            if mark.get("distro") not in (None, distro):
                mark = {}
            mark.update(distro=distro, package=got[-1],
                        apt_files=sorted(set(self._apt_files(distro)) - set(before)))
            write_marker(mark)
        if rc != 0:
            self.done.emit("failed", tr(
                "The OpenFOAM install in {distro} ended with code {rc}. The "
                "log above shows why.", distro=distro, rc=rc))
            return
        self._step(tr("Checking OpenFOAM…"))
        ready = self._ready()
        if not ready:
            self.done.emit("failed", tr(
                "The install ended, but OpenFOAM was not found in WSL."))
            return
        fset.set(PENDING_KEY, False)
        self.done.emit("ok", tr("OpenFOAM is ready: {what}",
                                what=ready[0].label + " " + ready[0].version))


class RemoveWorker(Worker):
    """Remove what the program installed (MARKER), and on request the
    results. Starts from the uninstaller."""

    def __init__(self, run=run_lines, detect=None, results: bool = False):
        super().__init__(run, detect)
        self.results = results

    def _work(self):
        emit = self.line.emit
        mark = read_marker()
        notes = []
        distros = wsl_distros(self._run) if os.name == "nt" else None
        distro = mark.get("distro", "")
        if mark.get("distro_installed") and distros and distro in distros:
            self.status.emit(tr("Removing {distro} (the program installed "
                                "it)…", distro=distro))
            rc = self._run(["wsl.exe", "--unregister", distro], emit)
            if rc == 0:
                distros = [d for d in distros if d != distro]
                notes.append(tr("{distro} removed.", distro=distro))
            else:
                notes.append(tr("{distro} was not removed (code {rc}).",
                                distro=distro, rc=rc))
        elif mark.get("package") and distros and distro in distros:
            pkg = mark["package"]
            self.status.emit(tr("Removing {package} from {distro}…",
                                package=pkg, distro=distro))
            rc = self._run(root_argv(
                distro, remove_script(pkg, mark.get("apt_files", []))), emit)
            notes.append(tr("{package} removed.", package=pkg) if rc == 0
                         else tr("{package} was not removed (code {rc}).",
                                 package=pkg, rc=rc))
        else:
            notes.append(tr("The program installed no OpenFOAM: nothing to "
                            "remove in WSL."))
        if mark.get("wsl_installed"):
            notes.append(tr("WSL stays: it is a part of Windows. To remove "
                            "it, run “wsl --uninstall” as administrator."))
        if self.results:
            self.status.emit(tr("Removing the results…"))
            notes.append(self._remove_results(distros or [], emit))
        try:
            marker_path().unlink()
        except OSError:
            pass
        fset.set(PENDING_KEY, False)
        self.done.emit("ok", " ".join(str(n) for n in notes if n))

    def _remove_results(self, distros, emit) -> str:
        # The source copy keeps its data in the repository folder: never
        # delete there, only in the installed program or with FS_DATA_DIR.
        if not (fs_paths.FROZEN or os.environ.get("FS_DATA_DIR", "").strip()):
            return tr("Results: not removed (source copy of the program).")
        root = fs_paths.data_dir()
        for name in RESULT_DIRS:
            path = root / name
            if path.exists():
                emit(str(path))
                shutil.rmtree(path, ignore_errors=True)
        try:
            root.rmdir()                  # only when nothing else is left
        except OSError:
            pass
        for d in distros:
            self._run(be.wsl_argv(d, 'rm -rf "$HOME/openfoam-studio"'), emit)
        return tr("Results removed: {folder} and ~/openfoam-studio in WSL.",
                  folder=str(root))


class PrepareDialog(QtWidgets.QDialog):
    """The window with the log of the steps. It starts at once.
    remove: the window of the uninstaller (RemoveWorker)."""

    finished_ok = QtCore.Signal()

    def __init__(self, parent=None, run=run_lines, detect=None,
                 remove: bool = False, results: bool = False):
        super().__init__(parent)
        self.setWindowTitle(tr("Remove OpenFOAM Studio") if remove
                            else tr("Prepare OpenFOAM"))
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
        self._worker = (RemoveWorker(run, detect, results) if remove
                        else Worker(run, detect))
        self._worker.moveToThread(self._thread)
        self._thread.started.connect(self._worker.work)
        self._worker.line.connect(self.log.appendPlainText)
        self._worker.status.connect(self._status)
        self._worker.progress.connect(self._progress)
        self._worker.done.connect(self._done)
        self._thread.start()

    def _progress(self, value: int):
        if value < 0:
            self.bar.setRange(0, 0)
        else:
            self.bar.setRange(0, 100)
            self.bar.setValue(value)

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


def run_remove(argv) -> int:
    """--remove-backend: the window of the uninstaller, without the main
    window. Returns the exit code."""
    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication(argv)
    app.setApplicationName(fs_paths.APP_NAME)
    import fs_i18n
    fs_i18n.install()
    dlg = PrepareDialog(None, remove=True, results=RESULTS_FLAG in argv)
    dlg.exec()
    return 0 if dlg.result == "ok" else 1


def wanted_at_start(argv=None) -> bool:
    """Open the window at the start: the installer flag, or a step that
    waits for the restart of Windows."""
    if os.name != "nt":                 # WSL is Windows only
        return False
    argv = sys.argv[1:] if argv is None else argv
    return FLAG in argv or bool(fset.get(PENDING_KEY))

