"""«File → Set up a server…»: the wizard that connects a calculation server.

Steps (pages):
  1. Start: connect a server that is ready, or prepare a NEW, empty server.
  2. Tailscale on this computer: is it installed, is it connected. The login
     happens in the browser of the person; the program never asks for an
     auth key or a password.
  3. (new server only) The setup script for the server: Tailscale, the SSH
     server, OpenFOAM v2512, the key of this computer. The person runs it on
     the server.
  4. The server: from the tailnet list, from the SSH config, or typed. User
     name and an optional jump host (ProxyJump).
  5. The SSH key: make one if there is none, copy it to the server (the
     server password is typed one time, in a terminal window), test the
     login.
  6. The check: OpenFOAM (native, docker, podman), cores, memory, free disk.
     «Finish» adds the server to the program (and a Host entry to the SSH
     config, if necessary).

The logic is in fs_servers.py. The guide (Help button) is
docs/INSTALL.<lang>.md.
"""

from __future__ import annotations

import json
import os
import subprocess
import tempfile
import threading
from pathlib import Path

from PySide6 import QtCore, QtGui, QtWidgets

import fs_i18n
import fs_paths
import fs_servers as srv
import fs_settings as fset
from fs_i18n import tr

fs_i18n.install()

P_START, P_TAILSCALE, P_SCRIPT, P_SERVER, P_KEY, P_CHECK = range(6)
OPENFOAM_CHOICES = (
    ("apt", "Package openfoam2512-default (Ubuntu, Debian): recommended"),
    ("docker", "Docker image (other Linux systems)"),
    ("none", "Do not install OpenFOAM"),
)


def guide_path(lang: str | None = None) -> Path:
    """docs/INSTALL.<lang>.md (English if there is no file for `lang`)."""
    lang = lang or fs_i18n.language()
    docs = fs_paths.program_dir() / "docs"
    p = docs / f"INSTALL.{lang}.md"
    return p if p.is_file() else docs / "INSTALL.en.md"


def open_guide(parent=None) -> QtWidgets.QDialog:
    """The install and server guide in a window of the program."""
    dlg = QtWidgets.QDialog(parent)
    dlg.setObjectName("InstallGuide")
    dlg.setWindowTitle(tr("Install and server guide"))
    dlg.resize(820, 700)
    lay = QtWidgets.QVBoxLayout(dlg)
    view = QtWidgets.QTextBrowser()
    view.setOpenExternalLinks(True)
    path = guide_path()
    try:
        view.setMarkdown(path.read_text(encoding="utf-8"))
    except OSError:
        view.setPlainText(tr("Expected the file {path}", path=path))
    lay.addWidget(view)
    bb = QtWidgets.QDialogButtonBox(QtWidgets.QDialogButtonBox.Close)
    bb.button(QtWidgets.QDialogButtonBox.Close).setText(tr("Close"))
    bb.rejected.connect(dlg.reject)
    lay.addWidget(bb)
    dlg.setAttribute(QtCore.Qt.WA_DeleteOnClose)
    dlg.show()
    return dlg


def _hint(text) -> QtWidgets.QLabel:
    lbl = QtWidgets.QLabel(text)
    lbl.setWordWrap(True)
    lbl.setTextInteractionFlags(QtCore.Qt.TextSelectableByMouse
                                | QtCore.Qt.LinksAccessibleByMouse)
    lbl.setOpenExternalLinks(True)
    return lbl


def _mono() -> QtGui.QFont:
    f = QtGui.QFontDatabase.systemFont(QtGui.QFontDatabase.FixedFont)
    return f


class _Jobs(QtCore.QObject):
    """Run a function in a thread; `done(tag, result)` in the GUI thread."""

    done = QtCore.Signal(str, object)

    def start(self, tag: str, fn) -> None:
        def job():
            try:
                res = fn()
            except Exception as exc:                        # noqa: BLE001
                res = exc
            try:
                self.done.emit(tag, res)
            except RuntimeError:                            # wizard is gone
                pass
        threading.Thread(target=job, daemon=True).start()


# ---------------------------------------------------------------- pages

class StartPage(QtWidgets.QWizardPage):
    def __init__(self):
        super().__init__()
        self.setTitle(tr("Set up a calculation server"))
        self.setSubTitle(tr("OpenFOAM Studio can run a case on another "
                            "computer: a Linux server that you reach "
                            "through Tailscale and SSH."))
        lay = QtWidgets.QVBoxLayout(self)
        self.rb_ready = QtWidgets.QRadioButton(
            tr("Connect a server that is ready (it has SSH, and you can log "
               "in to it)"))
        self.rb_new = QtWidgets.QRadioButton(
            tr("Prepare a NEW, empty Linux server (the program makes a "
               "setup script for it)"))
        self.rb_ready.setChecked(True)
        lay.addWidget(self.rb_ready)
        lay.addWidget(self.rb_new)
        lay.addSpacing(12)
        lay.addWidget(_hint(tr(
            "What you need: an account on the server, Tailscale on this "
            "computer and on the server (the same Tailscale account), and "
            "OpenFOAM on the server. The wizard checks each item and tells "
            "you what to do. It never asks for a password: you type the "
            "server password only one time, in a terminal window, when the "
            "SSH key is copied.")))
        lay.addWidget(_hint(tr("The «Help» button opens the full guide.")))
        lay.addStretch(1)

    def nextId(self) -> int:
        return P_TAILSCALE


class TailscalePage(QtWidgets.QWizardPage):
    def __init__(self, jobs: _Jobs):
        super().__init__()
        self.jobs = jobs
        self.state = srv.TailscaleState()
        self.setTitle(tr("Tailscale on this computer"))
        self.setSubTitle(tr("Tailscale makes a private network between your "
                            "computers. The server is then reachable from "
                            "any network, without open ports."))
        lay = QtWidgets.QVBoxLayout(self)
        self.lbl_state = _hint("…")
        self.lbl_state.setStyleSheet("font-weight:bold;")
        lay.addWidget(self.lbl_state)
        self.lbl_todo = _hint("")
        lay.addWidget(self.lbl_todo)
        row = QtWidgets.QHBoxLayout()
        self.btn_download = QtWidgets.QPushButton(tr("Download Tailscale"))
        self.btn_download.setToolTip(srv.TAILSCALE_DOWNLOAD)
        self.btn_download.clicked.connect(lambda: QtGui.QDesktopServices.openUrl(
            QtCore.QUrl(srv.TAILSCALE_DOWNLOAD)))
        self.btn_login = QtWidgets.QPushButton(tr("Log in (opens the browser)"))
        self.btn_login.clicked.connect(self.login)
        self.btn_check = QtWidgets.QPushButton(tr("Check again"))
        self.btn_check.clicked.connect(self.refresh)
        for b in (self.btn_download, self.btn_login, self.btn_check):
            row.addWidget(b)
        row.addStretch(1)
        lay.addLayout(row)
        self.chk_skip = QtWidgets.QCheckBox(
            tr("Skip: the server is in my local network (no Tailscale)"))
        self.chk_skip.toggled.connect(self.completeChanged)
        lay.addWidget(self.chk_skip)
        lay.addStretch(1)
        self.timer = QtCore.QTimer(self)
        self.timer.setInterval(3000)
        self.timer.timeout.connect(self.refresh)
        self._busy = False
        jobs.done.connect(self._done)

    def initializePage(self):
        self.refresh()
        self.timer.start()

    def cleanupPage(self):
        self.timer.stop()

    def validatePage(self) -> bool:
        self.timer.stop()
        return True

    def refresh(self):
        if self._busy:
            return
        self._busy = True
        self.jobs.start("ts_status", srv.tailscale_status)

    def login(self):
        st = self.state
        if st.auth_url:
            QtGui.QDesktopServices.openUrl(QtCore.QUrl(st.auth_url))
            return
        argv = srv.login_argv(st)
        self.lbl_todo.setText(tr("Waiting for the login address…"))

        def job():
            try:
                p = subprocess.Popen(argv, stdout=subprocess.PIPE,
                                     stderr=subprocess.STDOUT,
                                     stdin=subprocess.DEVNULL,
                                     creationflags=srv.CREATE_NO_WINDOW)
            except OSError as exc:
                return exc
            for raw in p.stdout:
                m = srv.AUTH_URL_RE.search(raw.decode("utf-8", "replace"))
                if m:
                    return m.group(0)
            p.wait()
            return ""
        self.jobs.start("ts_login", job)

    def _done(self, tag, res):
        if tag == "ts_login":
            if isinstance(res, str) and res:
                QtGui.QDesktopServices.openUrl(QtCore.QUrl(res))
                self.lbl_todo.setText(tr("Log in in the browser. This page "
                                         "sees the result in a few seconds."))
            elif isinstance(res, Exception):
                self.lbl_todo.setText(tr("Could not start Tailscale: {error}",
                                         error=res))
            self.refresh()
            return
        if tag != "ts_status":
            return
        self._busy = False
        if isinstance(res, Exception):
            res = srv.TailscaleState(error=str(res))
        self.state = res
        self.wizard().ts_state = res
        self.lbl_state.setText(res.text())
        self.btn_download.setVisible(not res.installed)
        self.btn_login.setVisible(res.installed and not res.running
                                  and not res.error)
        if not res.installed:
            todo = tr("1. Click «Download Tailscale» and install it (the "
                      "official installer).{NL}2. Log in with your account "
                      "in the Tailscale window.{NL}3. Come back here: this "
                      "page sees the change by itself.", NL="\n")
        elif res.error:
            todo = tr("Start Tailscale (Windows: the Tailscale icon in the "
                      "taskbar, or restart the Tailscale service as "
                      "administrator).")
        elif not res.running:
            todo = tr("Click «Log in» and log in in the browser with the "
                      "same account as on the server.")
        else:
            n = len([p for p in res.peers if p.online])
            todo = tr("Ready. Computers online in your tailnet: {n}.", n=n)
            self.timer.stop()
        self.lbl_todo.setText(todo)
        self.completeChanged.emit()

    def isComplete(self) -> bool:
        return self.state.running or self.chk_skip.isChecked()

    def nextId(self) -> int:
        return P_SCRIPT if self.wizard().new_server() else P_SERVER


class ScriptPage(QtWidgets.QWizardPage):
    def __init__(self):
        super().__init__()
        self.setTitle(tr("Setup script for the new server"))
        self.setSubTitle(tr("Run this script on the server. The program does "
                            "not run it: you see each command before it "
                            "runs."))
        lay = QtWidgets.QVBoxLayout(self)
        form = QtWidgets.QFormLayout()
        self.chk_ts = QtWidgets.QCheckBox(tr("Install Tailscale (official "
                                             "install script) and log in"))
        self.chk_ssh = QtWidgets.QCheckBox(tr("Install and start the SSH "
                                              "server"))
        self.chk_key = QtWidgets.QCheckBox(tr("Add the SSH key of this "
                                              "computer (then no password "
                                              "copy is necessary)"))
        self.cmb_foam = QtWidgets.QComboBox()
        for key, title in OPENFOAM_CHOICES:
            self.cmb_foam.addItem(tr(title), key)
        for w in (self.chk_ts, self.chk_ssh, self.chk_key):
            w.setChecked(True)
            w.toggled.connect(self.update_script)
            form.addRow(w)
        self.cmb_foam.currentIndexChanged.connect(self.update_script)
        form.addRow(tr("OpenFOAM v2512"), self.cmb_foam)
        key_row = QtWidgets.QHBoxLayout()
        self.lbl_key = _hint("")
        self.btn_make_key = QtWidgets.QPushButton(tr("Make an SSH key"))
        self.btn_make_key.clicked.connect(self.make_key)
        key_row.addWidget(self.lbl_key, 1)
        key_row.addWidget(self.btn_make_key)
        form.addRow(key_row)
        lay.addLayout(form)
        self.text = QtWidgets.QPlainTextEdit()
        self.text.setReadOnly(True)
        self.text.setFont(_mono())
        self.text.setLineWrapMode(QtWidgets.QPlainTextEdit.NoWrap)
        lay.addWidget(self.text, 1)
        row = QtWidgets.QHBoxLayout()
        self.btn_save = QtWidgets.QPushButton(tr("Save as file…"))
        self.btn_save.clicked.connect(self.save)
        self.btn_copy = QtWidgets.QPushButton(tr("Copy"))
        self.btn_copy.clicked.connect(lambda: QtWidgets.QApplication.clipboard()
                                      .setText(self.text.toPlainText()))
        row.addWidget(self.btn_save)
        row.addWidget(self.btn_copy)
        row.addStretch(1)
        lay.addLayout(row)
        lay.addWidget(_hint(tr(
            "1. Put the file on the server (a USB stick, scp, or paste the "
            "text into «nano ofs-server-setup.sh» in a terminal on the "
            "server).{NL}2. On the server, as the account for the "
            "calculations: bash ofs-server-setup.sh{NL}3. Open the login "
            "address that «tailscale up» prints, and log in with the same "
            "account as on this computer.{NL}4. At the end, the script "
            "prints the account name and the Tailscale name. Click «Next» "
            "and select the server.", NL="\n")))

    def initializePage(self):
        self.update_script()

    def make_key(self):
        priv, why = srv.create_key()
        if priv is None:
            QtWidgets.QMessageBox.warning(self, tr("SSH key"), why)
        self.update_script()

    def update_script(self, *_):
        key = srv.find_key()
        self.btn_make_key.setVisible(key is None)
        self.chk_key.setEnabled(key is not None)
        self.lbl_key.setText(tr("Key: {path}", path=key) if key else
                             tr("This computer has no SSH key yet."))
        pub = srv.public_key_text(key) if (key and self.chk_key.isChecked()) else ""
        self.text.setPlainText(srv.setup_script(
            tailscale=self.chk_ts.isChecked(), ssh_server=self.chk_ssh.isChecked(),
            openfoam=self.cmb_foam.currentData(), public_key=pub))

    def save(self):
        start = str(Path(os.path.expanduser("~")) / "ofs-server-setup.sh")
        path, _ = QtWidgets.QFileDialog.getSaveFileName(
            self, tr("Save the setup script"), start, "bash (*.sh)")
        if path:
            with open(path, "w", encoding="utf-8", newline="\n") as fh:
                fh.write(self.text.toPlainText())

    def nextId(self) -> int:
        return P_SERVER


class ServerPage(QtWidgets.QWizardPage):
    def __init__(self):
        super().__init__()
        self.setTitle(tr("Select the server"))
        self.setSubTitle(tr("Select a computer of your tailnet or a name from "
                            "the SSH config, or type the address."))
        lay = QtWidgets.QVBoxLayout(self)
        self.list = QtWidgets.QListWidget()
        self.list.currentItemChanged.connect(self._picked)
        lay.addWidget(self.list, 1)
        form = QtWidgets.QFormLayout()
        self.ed_host = QtWidgets.QLineEdit()
        self.ed_host.setPlaceholderText("server1.tail1234.ts.net, 100.x.y.z")
        self.ed_user = QtWidgets.QLineEdit()
        self.ed_user.setPlaceholderText(tr("your account on the server"))
        self.ed_jump = QtWidgets.QLineEdit()
        self.ed_jump.setPlaceholderText(tr("empty, or the server in front of "
                                           "it (ProxyJump)"))
        self.ed_alias = QtWidgets.QLineEdit()
        form.addRow(tr("Server address"), self.ed_host)
        form.addRow(tr("User name"), self.ed_user)
        form.addRow(tr("Jump host"), self.ed_jump)
        form.addRow(tr("Name in the program"), self.ed_alias)
        lay.addLayout(form)
        lay.addWidget(_hint(tr(
            "Jump host: for a server that is not in the tailnet, but is in "
            "the local network of another server (for example lab computers "
            "behind a server). SSH goes through that server first.")))
        self.lbl_note = _hint("")
        self.lbl_note.setStyleSheet("color:#7f8c8d;")
        lay.addWidget(self.lbl_note)
        self.ed_host.textChanged.connect(self._host_changed)
        for ed in (self.ed_user, self.ed_jump, self.ed_alias):
            ed.textChanged.connect(self._fields_changed)

    def initializePage(self):
        self.list.clear()
        ts = self.wizard().ts_state
        for p in ts.peers:
            if p.name == ts.self_name:
                continue
            it = QtWidgets.QListWidgetItem(tr("Tailscale: {line}", line=p.line()))
            it.setData(QtCore.Qt.UserRole, ("peer", p.address))
            if not p.online:
                it.setForeground(QtGui.QBrush(QtGui.QColor("#999999")))
            self.list.addItem(it)
        self.hosts = srv.config_hosts()
        for alias, opts in self.hosts.items():
            via = opts.get("proxyjump", "")
            line = alias + "  ·  " + opts.get("hostname", alias)
            it = QtWidgets.QListWidgetItem(
                tr("SSH config: {line}", line=line)
                + ("  ·  " + tr("via {jump}", jump=via) if via else ""))
            it.setData(QtCore.Qt.UserRole, ("alias", alias))
            self.list.addItem(it)
        if self.list.count() == 0:
            self.list.addItem(tr("No servers found. Type the address below."))

    def _picked(self, item, _prev=None):
        if item is None or not item.data(QtCore.Qt.UserRole):
            return
        kind, value = item.data(QtCore.Qt.UserRole)
        self.ed_host.setText(value)
        if kind == "alias":
            self.ed_user.clear()
            self.ed_jump.clear()

    def _host_changed(self, text):
        host = text.strip()
        if host in getattr(self, "hosts", {}):
            self.ed_alias.setText(host)
        else:
            self.ed_alias.setText(srv.suggest_alias(host) if host else "")
        self._fields_changed()

    def _fields_changed(self, *_):
        t = self.target()
        if self.uses_alias():
            note = tr("«{alias}» is already in the SSH config: the program "
                      "uses that entry.", alias=t.host)
        elif t.host:
            note = tr("At the end, the wizard adds «Host {alias}» to the SSH "
                      "config ({path}).", alias=self.ed_alias.text().strip(),
                      path=srv.ssh_config_path())
        else:
            note = ""
        self.lbl_note.setText(note)
        self.completeChanged.emit()

    def uses_alias(self) -> bool:
        host = self.ed_host.text().strip()
        return (host in getattr(self, "hosts", {})
                and not self.ed_user.text().strip()
                and not self.ed_jump.text().strip())

    def target(self) -> srv.Target:
        host = self.ed_host.text().strip()
        user = self.ed_user.text().strip()
        if "@" in host and not user:
            user, host = host.split("@", 1)
        return srv.Target(host=host, user=user,
                          jump=self.ed_jump.text().strip())

    def isComplete(self) -> bool:
        if not self.target().host:
            return False
        return self.uses_alias() or srv.valid_alias(self.ed_alias.text().strip())

    def nextId(self) -> int:
        return P_KEY


class KeyPage(QtWidgets.QWizardPage):
    def __init__(self, jobs: _Jobs):
        super().__init__()
        self.jobs = jobs
        self.ok = False
        self.setTitle(tr("SSH key and login"))
        self.setSubTitle(tr("The program logs in to the server with a key, "
                            "without a password."))
        lay = QtWidgets.QVBoxLayout(self)
        row = QtWidgets.QHBoxLayout()
        self.lbl_key = _hint("")
        self.btn_make = QtWidgets.QPushButton(tr("Make an SSH key"))
        self.btn_make.clicked.connect(self.make_key)
        row.addWidget(self.lbl_key, 1)
        row.addWidget(self.btn_make)
        lay.addLayout(row)
        self.btn_copy_key = QtWidgets.QPushButton(
            tr("Copy the key to the server (asks the server password one "
               "time)"))
        self.btn_copy_key.clicked.connect(self.copy_key)
        lay.addWidget(self.btn_copy_key)
        lay.addWidget(_hint(tr("Or run this command yourself in a terminal:")))
        self.ed_cmd = QtWidgets.QLineEdit()
        self.ed_cmd.setReadOnly(True)
        self.ed_cmd.setFont(_mono())
        lay.addWidget(self.ed_cmd)
        row = QtWidgets.QHBoxLayout()
        self.btn_test = QtWidgets.QPushButton(tr("Test the login"))
        self.btn_test.clicked.connect(self.test)
        row.addWidget(self.btn_test)
        row.addStretch(1)
        lay.addLayout(row)
        self.lbl_result = _hint("")
        self.lbl_result.setStyleSheet("font-weight:bold;")
        lay.addWidget(self.lbl_result)
        self.lbl_advice = _hint("")
        lay.addWidget(self.lbl_advice)
        lay.addStretch(1)
        jobs.done.connect(self._done)

    def initializePage(self):
        self.ok = False
        self._show_key()
        self.test()

    def _show_key(self):
        key = srv.find_key()
        self.btn_make.setVisible(key is None)
        self.btn_copy_key.setEnabled(key is not None)
        t = self.wizard().target()
        if key:
            self.lbl_key.setText(tr("Key: {path}", path=key))
            self.ed_cmd.setText(srv.copy_key_command(t, key))
        else:
            self.lbl_key.setText(tr("This computer has no SSH key yet."))
            self.ed_cmd.clear()

    def make_key(self):
        priv, why = srv.create_key()
        if priv is None:
            QtWidgets.QMessageBox.warning(self, tr("SSH key"), why)
        self._show_key()

    def copy_key(self):
        key = srv.find_key()
        if key is None:
            return
        folder = Path(tempfile.gettempdir()) / "OpenFOAM Studio"
        started, why = srv.open_copy_key_terminal(self.wizard().target(), key,
                                                  folder)
        if started:
            self.lbl_advice.setText(tr("Type the server password in the "
                                       "terminal window. Then click «Test "
                                       "the login»."))
        else:
            QtWidgets.QApplication.clipboard().setText(self.ed_cmd.text())
            self.lbl_advice.setText(
                tr("The command is in the clipboard: paste it in a "
                   "terminal, type the server password, then click «Test "
                   "the login».") + (("\n" + why) if why else ""))

    def test(self):
        self.ok = False
        self.btn_test.setEnabled(False)
        self.lbl_result.setText(tr("Testing the login…"))
        self.lbl_advice.clear()
        t = self.wizard().target()
        self.jobs.start("login", lambda: srv.test_login(t))
        self.completeChanged.emit()

    def _done(self, tag, res):
        if tag != "login":
            return
        self.btn_test.setEnabled(True)
        if isinstance(res, Exception):
            res = (False, "other", str(res))
        ok, code, msg = res
        self.ok = bool(ok)
        if ok:
            self.lbl_result.setText(tr("✔ The key login works."))
            self.lbl_result.setStyleSheet("font-weight:bold; color:#1e8449;")
            self.lbl_advice.clear()
        else:
            self.lbl_result.setText(tr("✖ The login failed: {message}",
                                       message=msg))
            self.lbl_result.setStyleSheet("font-weight:bold; color:#c0392b;")
            self.lbl_advice.setText(srv.ssh_error_advice(code))
        self.completeChanged.emit()

    def isComplete(self) -> bool:
        return self.ok

    def nextId(self) -> int:
        return P_CHECK


class CheckPage(QtWidgets.QWizardPage):
    def __init__(self, jobs: _Jobs):
        super().__init__()
        self.jobs = jobs
        self.info: srv.ServerInfo | None = None
        self.setTitle(tr("Check the server"))
        self.setSubTitle(tr("The program reads what the server has. It "
                            "changes nothing on the server."))
        self.setFinalPage(True)
        lay = QtWidgets.QVBoxLayout(self)
        self.text = QtWidgets.QPlainTextEdit()
        self.text.setReadOnly(True)
        lay.addWidget(self.text, 1)
        self.lbl_warn = _hint("")
        self.lbl_warn.setStyleSheet("color:#a05a00;")
        lay.addWidget(self.lbl_warn)
        self.btn_again = QtWidgets.QPushButton(tr("Check again"))
        self.btn_again.clicked.connect(self.run)
        lay.addWidget(self.btn_again, 0, QtCore.Qt.AlignLeft)
        row = QtWidgets.QHBoxLayout()
        self.lbl_foam = QtWidgets.QLabel(tr("OpenFOAM for the runs"))
        self.cmb_foam = QtWidgets.QComboBox()
        self.cmb_foam.setToolTip(tr(
            "Native: the OpenFOAM that is installed on the server. A "
            "container: each OpenFOAM command runs in that docker or podman "
            "image (the same way as «Distribute between machines»)."))
        row.addWidget(self.lbl_foam)
        row.addWidget(self.cmb_foam, 1)
        lay.addLayout(row)
        self.chk_use = QtWidgets.QCheckBox(
            tr("Run the cases on this server (File → Settings → Machines)"))
        self.chk_use.setChecked(True)
        lay.addWidget(self.chk_use)
        self.lbl_config = _hint("")
        lay.addWidget(self.lbl_config)
        jobs.done.connect(self._done)

    def initializePage(self):
        wiz = self.wizard()
        if wiz.needs_config_entry():
            self.lbl_config.setText(tr("«Finish» adds this entry to {path}:",
                                       path=srv.ssh_config_path())
                                    + "\n" + wiz.config_block())
        else:
            self.lbl_config.clear()
        self.run()

    def run(self):
        self.info = None
        self.btn_again.setEnabled(False)
        self.text.setPlainText(tr("Checking the server…"))
        self.lbl_warn.clear()
        t = self.wizard().target()
        self.jobs.start("probe", lambda: srv.probe_server(t))
        self.completeChanged.emit()

    def _done(self, tag, res):
        if tag != "probe":
            return
        self.btn_again.setEnabled(True)
        if isinstance(res, Exception):
            res = srv.ServerInfo(error=str(res))
        self.info = res
        self.wizard().info = res
        self._fill_choices(res)
        self.text.setPlainText("\n".join(str(s) for s in res.lines()))
        self.lbl_warn.setText("\n".join(str(s) for s in res.warnings()))
        self.completeChanged.emit()

    def _fill_choices(self, info: srv.ServerInfo) -> None:
        """The OpenFOAM installs of the server: native and container images."""
        self.cmb_foam.clear()
        if info.native:
            self.cmb_foam.addItem(tr("Native: {version}", version=info.native[1]), "")
        for c in info.container_choices():
            engine, image = c.split("|", 1)
            self.cmb_foam.addItem(tr("Container ({engine}): {image}",
                                     engine=engine, image=image), c)
        i = self.cmb_foam.findData(info.default_container())
        self.cmb_foam.setCurrentIndex(max(i, 0))
        self.lbl_foam.setVisible(self.cmb_foam.count() > 0)
        self.cmb_foam.setVisible(self.cmb_foam.count() > 0)

    def container(self) -> str:
        """"" (native) or "engine|image" for the runs."""
        return self.cmb_foam.currentData() or ""

    def isComplete(self) -> bool:
        return bool(self.info and self.info.ok and self.cmb_foam.count() > 0)

    def nextId(self) -> int:
        return -1


# ---------------------------------------------------------------- wizard

class ServerWizard(QtWidgets.QWizard):
    """The wizard. After «Finish»: `alias` is the server name in the program
    (fs_settings «ssh_host» when «Run the cases on this server» is on)."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("ServerWizard")
        self.setWindowTitle(tr("Set up a server"))
        self.setOption(QtWidgets.QWizard.HaveHelpButton, True)
        self.setOption(QtWidgets.QWizard.NoBackButtonOnStartPage, True)
        self.helpRequested.connect(lambda: open_guide(self))
        self.resize(760, 620)
        self.jobs = _Jobs(self)
        self.ts_state = srv.TailscaleState()
        self.info: srv.ServerInfo | None = None
        self.alias = ""
        self.config_written = ""
        self.p_start = StartPage()
        self.p_ts = TailscalePage(self.jobs)
        self.p_script = ScriptPage()
        self.p_server = ServerPage()
        self.p_key = KeyPage(self.jobs)
        self.p_check = CheckPage(self.jobs)
        for pid, page in ((P_START, self.p_start), (P_TAILSCALE, self.p_ts),
                          (P_SCRIPT, self.p_script), (P_SERVER, self.p_server),
                          (P_KEY, self.p_key), (P_CHECK, self.p_check)):
            self.setPage(pid, page)
        self.setStartId(P_START)
        self._button_texts()
        fs_i18n.on_language_changed(self._button_texts)

    def _button_texts(self, _lang: str = "") -> None:
        """QWizard keeps its own button texts (no Qt translation files)."""
        W = QtWidgets.QWizard
        for which, text in ((W.BackButton, tr("< Back")),
                            (W.NextButton, tr("Next >")),
                            (W.FinishButton, tr("Finish")),
                            (W.CancelButton, tr("Cancel")),
                            (W.HelpButton, tr("Help"))):
            try:
                self.setButtonText(which, text)
            except RuntimeError:                            # wizard is gone
                return

    def new_server(self) -> bool:
        return self.p_start.rb_new.isChecked()

    def target(self) -> srv.Target:
        return self.p_server.target()

    def needs_config_entry(self) -> bool:
        return not self.p_server.uses_alias()

    def config_block(self) -> str:
        t = self.target()
        return srv.host_block(self.p_server.ed_alias.text().strip(), t.host,
                              t.user, t.jump)

    def accept(self):
        """«Finish»: SSH config entry (if necessary), then the setting."""
        t = self.target()
        if self.needs_config_entry():
            alias = self.p_server.ed_alias.text().strip()
            ok, note = srv.add_host(alias, t.host, t.user, t.jump)
            if not ok:
                QtWidgets.QMessageBox.warning(self, tr("SSH config"), note)
                return
            self.config_written = note or str(srv.ssh_config_path())
        else:
            alias = t.host
        self.alias = alias
        # The OpenFOAM of the runs: native (no entry) or a container image.
        import fs_backend
        cont = fs_backend.containers_setting(fset.get("ssh_containers"))
        choice = self.p_check.container()
        if choice:
            cont[alias] = choice
        else:
            cont.pop(alias, None)
        fset.set("ssh_containers", json.dumps(cont, sort_keys=True))
        if self.p_check.chk_use.isChecked():
            fset.set("ssh_host", alias)
        super().accept()


def run_wizard(parent=None) -> str:
    """Show the wizard (modal). The new server name, or "" if cancelled."""
    wiz = ServerWizard(parent)
    if wiz.exec() == QtWidgets.QDialog.Accepted:
        return wiz.alias
    return ""
