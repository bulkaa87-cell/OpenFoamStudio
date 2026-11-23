"""The version badge and the update of the program with one click.

The badge is a small button in the right corner of the menu bar. It shows
the version of the program. After the start, a thread asks GitHub for the
latest release. When the release is newer, the badge shows «Update to X».

A click on that badge:
1. downloads the installer of the release (a progress bar: the percent, the
   MB and the speed; the rule «Progress bars»);
2. compares its SHA-256 with SHA256SUMS.txt of the release;
3. closes the program (the normal close: the program can still ask
   questions and stay open);
4. starts the installer silent. The installer waits until this process
   ends (/WAITPID), removes the old version, installs the new one and
   starts the program again (/RELAUNCH=1). See the [Code] of the .iss.

Only the installed program on Windows updates itself. A copy from source
(and macOS and Linux) opens the release page.

The same file is in OpenFOAM Studio (fs_update.py) and in MeshFix Studio
(app/meshfix_studio/update.py): change both the same way. The import of tr
takes fs_i18n in OpenFOAM Studio and meshfix.i18n in MeshFix Studio.

Environment (for the tests):
- NO_UPDATE_CHECK=1: no check at the start (tests/_isolate.py sets it);
- UPDATE_API_URL: the URL of the JSON of the latest release (a local
  server in a test);
- UPDATE_AS_VERSION: compare as if this were the version of the program.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import sys
import tempfile
import threading
import time
import urllib.request
from dataclasses import dataclass

from PySide6 import QtCore, QtGui, QtWidgets

try:                                        # OpenFOAM Studio
    from fs_i18n import tr                  # type: ignore
    import fs_i18n as _i18n                 # type: ignore
except ImportError:                         # MeshFix Studio
    from meshfix.i18n import tr
    from meshfix import i18n as _i18n

TIMEOUT = 10.0
CHUNK = 256 * 1024
START_DELAY_MS = 4000
# The installer waits at most this long for the program to end.
WAIT_SECONDS = 300


def parse_version(text) -> tuple:
    """(0, 2, 4) of «v0.2.4» or «0.2.4-beta». () when there is no number."""
    m = re.search(r"(\d+(?:\.\d+)*)", str(text or ""))
    if not m:
        return ()
    return tuple(int(p) for p in m.group(1).split("."))


def newer(remote, local) -> bool:
    a, b = parse_version(remote), parse_version(local)
    if not a or not b:
        return False
    n = max(len(a), len(b))
    return a + (0,) * (n - len(a)) > b + (0,) * (n - len(b))


@dataclass
class Release:
    version: str
    page: str
    setup_name: str = ""
    setup_url: str = ""
    setup_size: int = 0
    sums_url: str = ""


def api_url(repo: str) -> str:
    return (os.environ.get("UPDATE_API_URL")
            or f"https://api.github.com/repos/{repo}/releases/latest")


def _get(url: str, agent: str, timeout: float = TIMEOUT):
    req = urllib.request.Request(url, headers={
        "User-Agent": agent, "Accept": "application/vnd.github+json"})
    return urllib.request.urlopen(req, timeout=timeout)


def parse_release(data: dict, setup_suffix: str) -> Release:
    """The release of the JSON of GitHub (releases/latest)."""
    tag = str(data.get("tag_name") or "")
    rel = Release(version=".".join(str(p) for p in parse_version(tag)),
                  page=str(data.get("html_url") or ""))
    for a in data.get("assets") or []:
        name = str(a.get("name") or "")
        if name.endswith(setup_suffix):
            rel.setup_name = name
            rel.setup_url = str(a.get("browser_download_url") or "")
            rel.setup_size = int(a.get("size") or 0)
        elif name == "SHA256SUMS.txt":
            rel.sums_url = str(a.get("browser_download_url") or "")
    return rel


def latest(repo: str, setup_suffix: str, agent: str) -> Release:
    with _get(api_url(repo), agent) as resp:
        data = json.loads(resp.read().decode("utf-8"))
    return parse_release(data, setup_suffix)


def sha256_of(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for block in iter(lambda: fh.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


def expected_sha(sums_text: str, name: str) -> str:
    """The SHA-256 of `name` in a SHA256SUMS.txt («<hash>  <name>»)."""
    for line in sums_text.splitlines():
        parts = line.strip().split()
        if len(parts) >= 2 and parts[-1].lstrip("*") == name:
            return parts[0].lower()
    return ""


def progress_text(done: int, total: int, seconds: float) -> str:
    mb = 1024 * 1024
    speed = done / mb / seconds if seconds > 0.2 else 0.0
    if total > 0:
        return tr("{done:.1f} of {total:.1f} MB · {speed:.1f} MB/s",
                  done=done / mb, total=total / mb, speed=speed)
    return tr("{done:.1f} MB · {speed:.1f} MB/s", done=done / mb, speed=speed)


def download(url: str, dest: str, agent: str, total: int = 0,
             on_progress=None, cancel: threading.Event | None = None) -> None:
    """Download `url` to `dest` (first to dest + «.part»)."""
    part = dest + ".part"
    try:
        with _get(url, agent, timeout=30) as resp, open(part, "wb") as out:
            total = total or int(resp.headers.get("Content-Length") or 0)
            done = 0
            if on_progress:
                on_progress(0, total)
            while True:
                if cancel is not None and cancel.is_set():
                    raise InterruptedError("cancelled")
                block = resp.read(CHUNK)
                if not block:
                    break
                out.write(block)
                done += len(block)
                if on_progress:
                    on_progress(done, total)
        os.replace(part, dest)
    except BaseException:
        try:
            os.remove(part)
        except OSError:
            pass
        raise


def all_users_install(app_dir: str) -> bool:
    """True when the program is in Program Files (an install for all
    users): the installer then needs /ALLUSERS (and asks for the rights)."""
    d = os.path.normcase(os.path.abspath(app_dir))
    for var in ("ProgramW6432", "ProgramFiles", "ProgramFiles(x86)"):
        base = os.environ.get(var)
        if base and d.startswith(os.path.normcase(os.path.abspath(base))
                                 + os.sep):
            return True
    return False


def installer_args(app_dir: str, lang: str, pid: int) -> list:
    return ["/SILENT", "/SUPPRESSMSGBOXES", "/NORESTART", "/CLOSEAPPLICATIONS",
            "/RELAUNCH=1", f"/WAITPID={pid}",
            f"/LANG={'uk' if lang == 'uk' else 'en'}",
            "/ALLUSERS" if all_users_install(app_dir) else "/CURRENTUSER"]


def start_installer(path: str, args: list) -> str:
    """Start the installer (ShellExecute: Windows can ask for the rights).
    '' when it started, else the error."""
    import ctypes
    params = " ".join(f'"{a}"' if " " in a else a for a in args)
    rc = ctypes.windll.shell32.ShellExecuteW(None, "open", path, params,
                                             None, 1)
    return "" if rc > 32 else f"ShellExecute error {rc}"


class _Signals(QtCore.QObject):
    found = QtCore.Signal(object)           # Release
    failed = QtCore.Signal(str)
    progress = QtCore.Signal(object, object)
    downloaded = QtCore.Signal(str)         # the path of the checked file
    dl_failed = QtCore.Signal(str)


class UpdateBadge(QtWidgets.QToolButton):
    """The badge «v0.2.4» / «v0.2.4 · Update to 0.2.5» of a main window."""

    def __init__(self, window, *, app_name: str, version: str, repo: str,
                 setup_suffix: str = "-windows-x64-setup.exe",
                 frozen: bool | None = None, app_dir: str | None = None):
        super().__init__(window)
        self.window_ = window
        self.app_name = app_name
        self.version = version
        self.repo = repo
        self.setup_suffix = setup_suffix
        self.frozen = (bool(getattr(sys, "frozen", False))
                       if frozen is None else frozen)
        self.app_dir = app_dir or os.path.dirname(sys.executable)
        self.agent = f"{app_name.replace(' ', '-')}/{version} (update check)"
        self.release: Release | None = None
        self.error = ""
        self.state = "idle"                 # idle, checking, latest, new, failed
        self._sig = _Signals()
        self._sig.found.connect(self._found)
        self._sig.failed.connect(self._failed)
        self._sig.progress.connect(self._dl_progress)
        self._sig.downloaded.connect(self._dl_done)
        self._sig.dl_failed.connect(self._dl_failed)
        self._cancel = threading.Event()
        self._box = None
        self.setObjectName("updateBadge")
        self.setAutoRaise(True)
        self.setCursor(QtCore.Qt.PointingHandCursor)
        # Right click: «Look for a newer version», «Open the release page».
        # (A QMenu, not addAction: actions of a QToolButton show an arrow.)
        self.setContextMenuPolicy(QtCore.Qt.CustomContextMenu)
        self.customContextMenuRequested.connect(self._menu)
        self.clicked.connect(self._clicked)
        self._show()
        try:
            _i18n.on_language_changed(self._language_changed)
        except Exception:                   # noqa: BLE001
            pass

    def _language_changed(self, _lang) -> None:
        self._show()

    # ------------------------------------------------------------- state
    def local_version(self) -> str:
        return os.environ.get("UPDATE_AS_VERSION") or self.version

    def _show(self) -> None:
        v = "v" + self.version
        style = ("QToolButton#updateBadge { padding: 1px 8px; "
                 "border-radius: 8px; font-size: 11px; color: palette(mid); }")
        if self.state == "new" and self.release is not None:
            text = tr("{v} · Update to {new}", v=v, new=self.release.version)
            tip = tr("Version {new} of {app} is available. Click: download "
                     "it, close the program, install it and start the "
                     "program again.", new=self.release.version,
                     app=self.app_name)
            if not (self.frozen and sys.platform == "win32"):
                tip = tr("Version {new} of {app} is available. Click: open "
                         "the release page (this copy does not update "
                         "itself).", new=self.release.version,
                         app=self.app_name)
            style = ("QToolButton#updateBadge { padding: 1px 8px; "
                     "border-radius: 8px; font-size: 11px; font-weight: bold;"
                     " color: white; background: #2e7d32; }"
                     "QToolButton#updateBadge:hover { background: #388e3c; }")
        elif self.state == "checking":
            text = tr("{v} · checking…", v=v)
            tip = tr("Looking for a newer version…")
        elif self.state == "latest":
            text = v
            tip = tr("{app} {v} is the latest version. Click: check again.",
                     app=self.app_name, v=v)
        elif self.state == "failed":
            text = v
            tip = tr("Could not look for a newer version: {err}. Click: "
                     "try again.", err=self.error)
        else:
            text = v
            tip = tr("{app} {v}. Click: look for a newer version.",
                     app=self.app_name, v=v)
        self.setText(str(text))
        self.setToolTip(str(tip))
        self.setStyleSheet(style)
        self.adjustSize()
        bar = self.parentWidget()
        if isinstance(bar, QtWidgets.QMenuBar):
            # The menu bar sizes its corner widget only when it is set.
            bar.setCornerWidget(self, QtCore.Qt.TopRightCorner)
            self.show()

    def _menu(self, pos) -> None:
        menu = QtWidgets.QMenu(self)
        menu.addAction(str(tr("Look for a newer version")), self.check)
        menu.addAction(str(tr("Open the release page")), self.open_page)
        menu.exec(self.mapToGlobal(pos))

    def check_at_start(self) -> None:
        if os.environ.get("NO_UPDATE_CHECK") == "1":
            return
        QtCore.QTimer.singleShot(START_DELAY_MS, self.check)

    @QtCore.Slot()
    def check(self) -> None:
        if self.state == "checking":
            return
        self.state = "checking"
        self._show()

        def job():
            try:
                rel = latest(self.repo, self.setup_suffix, self.agent)
            except Exception as exc:        # noqa: BLE001
                self._emit(self._sig.failed, str(exc))
                return
            self._emit(self._sig.found, rel)
        threading.Thread(target=job, daemon=True,
                         name="update-check").start()

    @staticmethod
    def _emit(signal, *args) -> None:
        try:
            signal.emit(*args)
        except RuntimeError:                # the window is gone
            pass

    def _found(self, rel: Release) -> None:
        self.release = rel
        self.state = ("new" if newer(rel.version, self.local_version())
                      else "latest")
        self._show()

    def _failed(self, err: str) -> None:
        self.error = err
        self.state = "failed"
        self._show()

    def open_page(self) -> None:
        page = (self.release.page if self.release and self.release.page
                else f"https://github.com/{self.repo}/releases/latest")
        QtGui.QDesktopServices.openUrl(QtCore.QUrl(page))

    def _clicked(self) -> None:
        if self.state != "new":
            self.check()
            return
        if not (self.frozen and sys.platform == "win32"):
            self.open_page()
            return
        self.start_update()

    # ---------------------------------------------------------- download
    def target_path(self) -> str:
        folder = os.path.join(tempfile.gettempdir(),
                              self.app_name + " update")
        os.makedirs(folder, exist_ok=True)
        return os.path.join(folder, self.release.setup_name)

    def start_update(self) -> None:
        rel = self.release
        if rel is None or not rel.setup_url:
            QtWidgets.QMessageBox.warning(
                self.window_, self.app_name,
                str(tr("The release {new} has no installer for Windows. "
                       "The release page opens.", new=getattr(rel, "version",
                                                              "?"))))
            self.open_page()
            return
        if self._box is not None:
            return
        self._head = tr("Downloading {app} {new}…", app=self.app_name,
                        new=rel.version)
        box = QtWidgets.QProgressDialog(str(self._head), str(tr("Cancel")),
                                        0, 1000, self.window_)
        box.setWindowTitle(self.app_name)
        box.setWindowModality(QtCore.Qt.WindowModal)
        box.setMinimumDuration(0)
        box.setAutoClose(False)
        box.setAutoReset(False)
        box.canceled.connect(self._cancel.set)
        box.setValue(0)
        box.show()
        self._box = box
        self._t0 = time.monotonic()
        self._cancel.clear()
        dest = self.target_path()

        def job():
            try:
                if not rel.sums_url:
                    raise RuntimeError(str(tr(
                        "the release has no SHA256SUMS.txt")))
                with _get(rel.sums_url, self.agent) as resp:
                    want = expected_sha(resp.read().decode("utf-8", "replace"),
                                        rel.setup_name)
                if not want:
                    raise RuntimeError(str(tr(
                        "SHA256SUMS.txt has no line for {name}",
                        name=rel.setup_name)))
                if not (os.path.isfile(dest) and sha256_of(dest) == want):
                    download(rel.setup_url, dest, self.agent, rel.setup_size,
                             lambda d, t: self._emit(self._sig.progress, d, t),
                             self._cancel)
                    got = sha256_of(dest)
                    if got != want:
                        os.remove(dest)
                        raise RuntimeError(str(tr(
                            "the checksum of the download is wrong "
                            "({got} instead of {want})", got=got[:12],
                            want=want[:12])))
            except InterruptedError:
                self._emit(self._sig.dl_failed, "")
                return
            except Exception as exc:        # noqa: BLE001
                self._emit(self._sig.dl_failed, str(exc))
                return
            self._emit(self._sig.downloaded, dest)
        threading.Thread(target=job, daemon=True,
                         name="update-download").start()

    def _dl_progress(self, done, total) -> None:
        box = self._box
        if box is None:
            return
        if total > 0:
            if box.maximum() != 1000:
                box.setRange(0, 1000)
            box.setValue(min(1000, int(done * 1000 / total)))
        elif box.maximum() != 0:
            box.setRange(0, 0)
        box.setLabelText(str(self._head) + "\n" + str(progress_text(
            done, total, time.monotonic() - self._t0)))

    def _close_box(self) -> None:
        if self._box is not None:
            self._box.close()
            self._box.deleteLater()
            self._box = None

    def _dl_failed(self, err: str) -> None:
        self._close_box()
        if not err:                         # cancelled
            return
        QtWidgets.QMessageBox.warning(
            self.window_, self.app_name,
            str(tr("The update did not download: {err}", err=err)))

    def _dl_done(self, path: str) -> None:
        if self._box is not None:
            self._box.setRange(0, 1000)
            self._box.setValue(1000)
            self._box.setLabelText(str(tr("Closing the program and starting "
                                          "the installer…")))
        self.install(path)

    # ----------------------------------------------------------- install
    def install(self, path: str) -> bool:
        """Close the program, then start the installer. False when the
        program stays open (a question at close was answered «Cancel»)."""
        self._close_box()
        win = self.window_
        win.close()
        if win.isVisible():
            win.statusBar().showMessage(str(tr(
                "The program did not close: the update waits. Click the "
                "badge again.")), 10000)
            return False
        args = installer_args(self.app_dir, str(_i18n.language()),
                              os.getpid())
        err = start_installer(path, args)
        if err:
            QtWidgets.QMessageBox.critical(
                None, self.app_name,
                str(tr("The installer did not start: {err}{NEWLINE}"
                       "Start it yourself: {path}", err=err, path=path,
                       NEWLINE="\n")))
        QtWidgets.QApplication.quit()
        return not err


def add_badge(window, **kw) -> UpdateBadge:
    """Put the badge in the right corner of the menu bar of `window` and
    start the check (after START_DELAY_MS)."""
    badge = UpdateBadge(window, **kw)
    window.menuBar().setCornerWidget(badge, QtCore.Qt.TopRightCorner)
    badge.check_at_start()
    return badge
