"""«Файл → Налаштування…» (Ctrl+,): the dialog for the program settings.

The values live in fs_settings (one JSON file per user). The dialog only
reads and writes them; the main window reacts through fs_settings.subscribe.

Sections: Загальне, Машини, 3D, Сітка. Buttons: OK / Скасувати /
Застосувати, «Скинути до типових» (fills the form with the defaults; they
are stored only after OK or «Застосувати»).

User-visible texts go through fs_i18n.tr(); the English texts are in
i18n/uk/fs_settingsdlg.json.
"""

from __future__ import annotations

import os
import threading

from PySide6 import QtCore, QtWidgets

import fs_settings as fset
import fs_i18n
from fs_i18n import tr

# The Qt text setters translate and keep the key of each text, also in a
# widget that is made before the main window (T28).
fs_i18n.install()


LANG_TITLES = tuple(fs_i18n.LANGUAGES.items())   # own names: no tr
VIEW3D_TITLES = (
    ("auto", "Automatic (recommended)"),
    ("gpu", "GPU (foamview)"),
    ("soft", "Software GPU (WARP, llvmpipe): slower"),
    ("vtk", "VTK (OpenGL)"),
    ("cpu", "CPU without GPU: slow"),
)


class SettingsDialog(QtWidgets.QDialog):
    """Program settings. `backends()` gives the lines for «Знайдені машини»,
    `level_text()` the line «Зараз малює» of the 3D tab; both optional."""

    graphics_checked = QtCore.Signal(str)

    SECTIONS = ("General", "Machines", "3D", "Mesh")

    def __init__(self, parent=None, default_cases: str = "",
                 default_projects: str = "", backends=None, level_text=None):
        super().__init__(parent)
        self.setWindowTitle(tr("Settings"))
        self.setObjectName("SettingsDialog")
        self._default_cases = default_cases
        self._default_projects = default_projects
        self._backends = backends
        self._level_text = level_text
        self._checking = False

        self.list = QtWidgets.QListWidget()
        self.list.setFixedWidth(150)
        self.stack = QtWidgets.QStackedWidget()
        for title, page in zip(self.SECTIONS, (self._page_general(),
                                               self._page_machines(),
                                               self._page_3d(),
                                               self._page_mesh())):
            self.list.addItem(tr(title))
            scroll = QtWidgets.QScrollArea()
            scroll.setWidgetResizable(True)
            scroll.setFrameShape(QtWidgets.QFrame.NoFrame)
            scroll.setHorizontalScrollBarPolicy(QtCore.Qt.ScrollBarAlwaysOff)
            scroll.setWidget(page)
            self.stack.addWidget(scroll)
        self.list.currentRowChanged.connect(self.stack.setCurrentIndex)
        self.list.setCurrentRow(0)

        bb = QtWidgets.QDialogButtonBox(
            QtWidgets.QDialogButtonBox.Ok | QtWidgets.QDialogButtonBox.Cancel
            | QtWidgets.QDialogButtonBox.Apply
            | QtWidgets.QDialogButtonBox.RestoreDefaults)
        bb.button(QtWidgets.QDialogButtonBox.Ok).setText(tr("OK"))
        bb.button(QtWidgets.QDialogButtonBox.Cancel).setText(tr("Cancel"))
        bb.button(QtWidgets.QDialogButtonBox.Apply).setText(tr("Apply"))
        rd = bb.button(QtWidgets.QDialogButtonBox.RestoreDefaults)
        rd.setText(tr("Restore defaults"))
        rd.setToolTip(tr("Fill all fields with the default values. They "
                         "take effect only after «OK» or «Apply»"))
        bb.accepted.connect(self.accept)
        bb.rejected.connect(self.reject)
        bb.button(QtWidgets.QDialogButtonBox.Apply).clicked.connect(self.apply)
        rd.clicked.connect(self.restore_defaults)
        self.buttons = bb

        top = QtWidgets.QHBoxLayout()
        top.addWidget(self.list)
        top.addWidget(self.stack, 1)
        lay = QtWidgets.QVBoxLayout(self)
        lay.addLayout(top, 1)
        self.lbl_where = QtWidgets.QLabel(
            tr("Settings file: {path}", path=fset.path()))
        self.lbl_where.setStyleSheet("color:#777;")
        self.lbl_where.setTextInteractionFlags(QtCore.Qt.TextSelectableByMouse)
        self.lbl_where.setWordWrap(True)
        lay.addWidget(self.lbl_where)
        lay.addWidget(bb)
        self.graphics_checked.connect(self._graphics_done)
        self.load(fset.all_values())
        self.resize(740, 540)

    # --- pages -------------------------------------------------------------

    @staticmethod
    def _hint(text: str) -> QtWidgets.QLabel:
        lbl = QtWidgets.QLabel(text)
        lbl.setWordWrap(True)
        lbl.setStyleSheet("color:#666;")
        return lbl

    def _dir_row(self, edit: QtWidgets.QLineEdit, title: str) -> QtWidgets.QWidget:
        w = QtWidgets.QWidget()
        h = QtWidgets.QHBoxLayout(w)
        h.setContentsMargins(0, 0, 0, 0)
        h.addWidget(edit, 1)
        btn = QtWidgets.QPushButton(tr("Browse…"))

        def pick():
            start = edit.text().strip() or edit.placeholderText()
            path = QtWidgets.QFileDialog.getExistingDirectory(self, title, start)
            if path:
                edit.setText(path)
        btn.clicked.connect(pick)
        h.addWidget(btn)
        return w

    def _page_general(self) -> QtWidgets.QWidget:
        w = QtWidgets.QWidget()
        f = QtWidgets.QFormLayout(w)
        self.cmb_lang = QtWidgets.QComboBox()
        for key, title in LANG_TITLES:
            self.cmb_lang.addItem(title, key)
        f.addRow(tr("Interface language"), self.cmb_lang)
        f.addRow("", self._hint(tr("The new language takes effect at "
                                   "once, after «OK» or «Apply».")))
        env = os.environ.get("FS_LANG", "").strip()
        self.lbl_lang_env = self._hint(
            tr("The environment variable FS_LANG={value} has priority over this field.",
               value=env))
        self.lbl_lang_env.setStyleSheet("color:#a05a00;")
        self.lbl_lang_env.setVisible(bool(env))
        f.addRow("", self.lbl_lang_env)

        self.chk_rookie = QtWidgets.QCheckBox(tr("Beginner mode"))
        self.chk_rookie.setToolTip(tr("An explanation for each setting: "
                                      "what it is, what it changes and "
                                      "what occurs if you go too far. "
                                      "The same as «Help → Beginner "
                                      "mode» (F2)"))
        f.addRow(self.chk_rookie)
        self.chk_session = QtWidgets.QCheckBox(tr("Restore the last session"))
        self.chk_session.setToolTip(tr("At start, restore the settings, "
                                       "the geometry and the case that "
                                       "were open when you closed the "
                                       "program"))
        f.addRow(self.chk_session)

        self.ed_cases = QtWidgets.QLineEdit()
        self.ed_cases.setPlaceholderText(self._default_cases)
        f.addRow(tr("Cases folder"), self._dir_row(self.ed_cases, tr("Cases folder")))
        f.addRow("", self._hint(tr("OpenFOAM makes the cases in this "
                                   "folder. The same field «Folder» is "
                                   "on the «Run» tab. An empty field: "
                                   "the folder cases next to the program.")))
        self.ed_projects = QtWidgets.QLineEdit()
        self.ed_projects.setPlaceholderText(self._default_projects)
        f.addRow(tr("Projects folder"),
                 self._dir_row(self.ed_projects, tr("Projects folder")))
        f.addRow("", self._hint(tr("The default folder for projects. The "
                                   "automatic save before a run writes "
                                   "here.")))
        return w

    def _page_machines(self) -> QtWidgets.QWidget:
        w = QtWidgets.QWidget()
        f = QtWidgets.QFormLayout(w)
        self.ed_ssh = QtWidgets.QLineEdit()
        self.ed_ssh.setPlaceholderText(tr("user@server (empty: no server)"))
        row = QtWidgets.QHBoxLayout()
        row.setContentsMargins(0, 0, 0, 0)
        row.addWidget(self.ed_ssh, 1)
        self.btn_wizard = QtWidgets.QPushButton(tr("Set up a server…"))
        self.btn_wizard.setToolTip(tr("The wizard: Tailscale, SSH key, "
                                      "login test, OpenFOAM check"))
        self.btn_wizard.clicked.connect(self.server_wizard)
        row.addWidget(self.btn_wizard)
        f.addRow(tr("Server (SSH)"), row)
        self.chk_check = QtWidgets.QCheckBox(tr("Find the machines at start"))
        f.addRow(self.chk_check)
        f.addRow("", self._hint(tr("When off, the program does not look "
                                   "for OpenFOAM (WSL, blueCFD, Docker, "
                                   "server) at start, and the window "
                                   "opens faster. Then click «Search» on "
                                   "the «Run» tab.")))

        self.chk_notify = QtWidgets.QCheckBox(tr("Notify about the runs"))
        self.chk_notify.setToolTip(tr("A message of the system (a Windows "
                                      "notification) and a line in the "
                                      "window: a run finished, failed or "
                                      "stopped, the results are "
                                      "downloaded. A click opens the run"))
        f.addRow(self.chk_notify)
        self.chk_reconnect = QtWidgets.QCheckBox(
            tr("Connect to the server runs again by itself"))
        self.chk_reconnect.setToolTip(tr("At start, after a sleep of the "
                                         "computer and after a new network, "
                                         "the program finds the runs that it "
                                         "started on a server and connects "
                                         "to them, like «Connect to Run». "
                                         "With unsaved changes in the "
                                         "scene, it only tells"))
        f.addRow(self.chk_reconnect)

        box = QtWidgets.QGroupBox(tr("Distribute between machines (experimental)"))
        g = QtWidgets.QFormLayout(box)
        self.ed_dist_user = QtWidgets.QLineEdit()
        g.addRow(tr("User"), self.ed_dist_user)
        self.ed_dist_key = QtWidgets.QLineEdit()
        g.addRow(tr("SSH key"), self.ed_dist_key)
        self.ed_dist_net = QtWidgets.QLineEdit()
        g.addRow(tr("Network"), self.ed_dist_net)
        g.addRow("", self._hint(tr("The list of machines and "
                                   "«Distribute» are on the «Run» tab.")))
        f.addRow(box)

        self.lst_found = QtWidgets.QListWidget()
        self.lst_found.setMaximumHeight(110)
        self.lst_found.setSelectionMode(QtWidgets.QAbstractItemView.NoSelection)
        rows = []
        if self._backends is not None:
            try:
                rows = list(self._backends())
            except Exception:                              # noqa: BLE001
                rows = []
        for row in rows or [tr("Nothing found yet.")]:
            self.lst_found.addItem(row)
        f.addRow(tr("Found machines"), self.lst_found)
        return w

    def _page_3d(self) -> QtWidgets.QWidget:
        w = QtWidgets.QWidget()
        f = QtWidgets.QFormLayout(w)
        self.cmb_3d = QtWidgets.QComboBox()
        self.cmb_3d.setSizeAdjustPolicy(
            QtWidgets.QComboBox.AdjustToMinimumContentsLengthWithIcon)
        self.cmb_3d.setMinimumContentsLength(18)
        for key, title in VIEW3D_TITLES:
            self.cmb_3d.addItem(tr(title), key)
        f.addRow(tr("Renderer of «3D»"), self.cmb_3d)
        f.addRow("", self._hint(tr("Takes effect after you restart the "
                                   "program. «Automatic» tries GPU → "
                                   "software GPU → VTK → CPU and uses "
                                   "the first one that draws.")))
        env = os.environ.get("FS_3D", "").strip()
        self.lbl_env = self._hint(
            tr("The environment variable FS_3D={value} has priority over this field.",
               value=env))
        self.lbl_env.setStyleSheet("color:#a05a00;")
        self.lbl_env.setVisible(bool(env))
        f.addRow("", self.lbl_env)

        now = ""
        if self._level_text is not None:
            try:
                now = str(self._level_text() or "")
            except Exception:                              # noqa: BLE001
                now = ""
        self.lbl_level = QtWidgets.QLabel(now or "—")
        self.lbl_level.setWordWrap(True)
        self.lbl_level.setTextInteractionFlags(QtCore.Qt.TextSelectableByMouse)
        f.addRow(tr("Draws now"), self.lbl_level)
        self.btn_check = QtWidgets.QPushButton(tr("Check the graphics"))
        self.btn_check.setToolTip(tr("Try all adapters again (~1 s each) "
                                     "and remember which one draws. Open "
                                     "3D views do not change until a "
                                     "restart"))
        self.btn_check.clicked.connect(self.check_graphics)
        self.lbl_check = QtWidgets.QLabel("")
        self.lbl_check.setWordWrap(True)
        self.lbl_check.setTextInteractionFlags(QtCore.Qt.TextSelectableByMouse)
        f.addRow(self.btn_check, self.lbl_check)
        return w

    def _page_mesh(self) -> QtWidgets.QWidget:
        w = QtWidgets.QWidget()
        f = QtWidgets.QFormLayout(w)
        self.chk_fetch = QtWidgets.QCheckBox(
            tr("After a run, copy the mesh from the server to this computer"))
        f.addRow(self.chk_fetch)
        f.addRow("", self._hint(tr("A mesh is hundreds of MB; on a slow "
                                   "connection this takes minutes. When "
                                   "off, only «Save project with mesh…» "
                                   "copies the mesh.")))
        self.spn_keep = QtWidgets.QSpinBox()
        lo, hi = fset.LIMITS["mesh_keep"]
        self.spn_keep.setRange(lo, hi)
        self.spn_keep.setMaximumWidth(90)
        f.addRow(tr("Meshes to keep on the server"), self.spn_keep)
        f.addRow("", self._hint(tr("After each run, the server keeps "
                                   "this number of the latest meshes and "
                                   "deletes the older ones.")))
        return w

    # --- values ------------------------------------------------------------

    @staticmethod
    def _select(cmb: QtWidgets.QComboBox, key: str) -> None:
        i = cmb.findData(key)
        cmb.setCurrentIndex(max(i, 0))

    def load(self, v: dict) -> None:
        self._select(self.cmb_lang, v["ui_language"])
        self.chk_rookie.setChecked(bool(v["rookie"]))
        self.chk_session.setChecked(bool(v["restore_session"]))
        self.ed_cases.setText(v["cases_dir"])
        self.ed_projects.setText(v["projects_dir"])
        self.ed_ssh.setText(v["ssh_host"])
        self.chk_check.setChecked(bool(v["check_machines_on_start"]))
        self.chk_notify.setChecked(bool(v["notify"]))
        self.chk_reconnect.setChecked(bool(v["auto_reconnect"]))
        self.ed_dist_user.setText(v["dist_user"])
        self.ed_dist_key.setText(v["dist_key"])
        self.ed_dist_net.setText(v["dist_net"])
        self._select(self.cmb_3d, v["view3d_backend"])
        self.chk_fetch.setChecked(bool(v["mesh_auto_fetch"]))
        self.spn_keep.setValue(int(v["mesh_keep"]))

    def values(self) -> dict:
        return {
            "ui_language": self.cmb_lang.currentData(),
            "rookie": self.chk_rookie.isChecked(),
            "restore_session": self.chk_session.isChecked(),
            "cases_dir": self.ed_cases.text().strip(),
            "projects_dir": self.ed_projects.text().strip(),
            "ssh_host": self.ed_ssh.text().strip(),
            "check_machines_on_start": self.chk_check.isChecked(),
            "notify": self.chk_notify.isChecked(),
            "auto_reconnect": self.chk_reconnect.isChecked(),
            "dist_user": self.ed_dist_user.text().strip(),
            "dist_key": self.ed_dist_key.text().strip(),
            "dist_net": self.ed_dist_net.text().strip(),
            "view3d_backend": self.cmb_3d.currentData(),
            "mesh_auto_fetch": self.chk_fetch.isChecked(),
            "mesh_keep": self.spn_keep.value(),
        }

    def restore_defaults(self) -> None:
        self.load({**fset.all_values(), **{k: fset.DEFAULTS[k] for k in self.values()}})

    def apply(self) -> dict:
        """Store the form. Returns the changed keys."""
        return fset.update(self.values())

    def accept(self) -> None:
        self.apply()
        super().accept()

    def server_wizard(self) -> None:
        """«Set up a server…»: the wizard puts the new name in the field."""
        import fs_serverwiz
        alias = fs_serverwiz.run_wizard(self)
        if alias:
            self.ed_ssh.setText(alias)

    # --- «Перевірити графіку» ----------------------------------------------

    def check_graphics(self) -> None:
        if self._checking:
            return
        self._checking = True
        self.btn_check.setEnabled(False)
        self.lbl_check.setText(tr("Checking… (up to half a minute for each adapter)"))

        def job():
            try:
                import fs_view3d
                lv = fs_view3d.check_graphics()
                text = lv.text() + (f"\n{lv.note_text()}" if lv.note else "")
            except Exception as exc:                       # noqa: BLE001
                text = f"{type(exc).__name__}: {exc}"
            try:
                self.graphics_checked.emit(text)
            except RuntimeError:                           # dialog is gone
                pass
        threading.Thread(target=job, daemon=True).start()

    def _graphics_done(self, text: str) -> None:
        self._checking = False
        self.btn_check.setEnabled(True)
        self.lbl_check.setText(tr("Automatic choice: {result}", result=text))

