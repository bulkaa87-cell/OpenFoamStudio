"""MeshFix Studio: a small program that repairs one surface mesh.

Open a mesh (STL, 3MF, OBJ, PLY, OFF, glTF, VTK), look at its problems in
3D, repair it, export it in one of these formats. Start it with
«MeshFix Studio.bat» or:

    .venv\\Scripts\\pythonw.exe fs_meshfix_app.py [file]

NO SECOND COPY OF THE REPAIR. The repair, the method list and the check
are the code of OpenFOAM Studio:
- fs_repair: the methods (meshfix, alpha wrap, Windows, volume rebuild) and
  repair_meshes(); the «Repair» button of OpenFOAM Studio calls the same
  function (tests/test_meshfix_studio.py checks it);
- fs_meshfix (meshfix.repair of the package meshfix-core, see
  fs_shared.py): the repair itself;
- fs_geometry: reading the file, the check (diagnose) and the problem
  layers;
- foam_studio.Viewport: the 3D view of the «3D» tab (foamview with the
  fallbacks GPU -> WARP/lavapipe -> VTK -> CPU), with its problem layers;
- foam_studio.RepairWorker and end_process: the worker thread and a safe
  end of the process while a repair still runs (T26).

UNITS. Most mesh files have no units. The program guesses them from the
size (fs_geometry.guess_scale), as OpenFOAM Studio does, and the person
can change them in «Units». A 3MF or glTF file gives its unit. The repair
works in metres (its tolerances are in mm). The export writes a format
without a unit in the units of the input file, 3MF in mm, glTF in m.

FORMATS AND BODIES. The file formats are in the shared package
(meshfix.formats, meshfix-core 0.3.0 and later, see fs_geometry.formats).
With such a package: the open and export dialogs have all formats, each
body of a file is repaired alone (meshfix.methods.repair_bodies), and
the export keeps the bodies where the format can. The repair then runs
without its step stl_safe_output: the export applies it to STL and glTF
only. With an older package: STL export only, as before.

LANGUAGE. English by default. «View → Language» selects Ukrainian; FS_LANG
overrides the setting. The texts are English keys of fs_i18n
(i18n/uk/fs_meshfix_app.json).
"""

from __future__ import annotations

import math
import os
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path

import fs_compat  # noqa: F401  — first (six + PySide6 fix)

import numpy as np
from PySide6 import QtCore, QtGui, QtWidgets

import foam_studio as fs
import fs_geometry as geo
import fs_i18n
import fs_icons
import fs_meshfix
import fs_repair
import fs_fmt as fmt
from fs_i18n import tr

APP_NAME = "MeshFix Studio"
APP_USER_MODEL_ID = "OpenFOAMStudio.MeshFixStudio"
SETTINGS_ORG = fs.SETTINGS_ORG
SETTINGS_APP = "meshfix-studio"
DEFAULT_LANGUAGE = "en"
SUFFIXES = geo.surface_suffixes()
HISTORY = 10            # undo steps that the program keeps
CLOSE_GRACE_MS = 1500   # wait for a running repair at the end, then exit

# Units of a mesh file: (key, label, metres per unit).
UNITS = (("mm", "mm", 0.001), ("cm", "cm", 0.01), ("m", "m", 1.0),
         ("in", "in", 0.0254), ("km", "km", 1000.0))

# The viewport check boxes that only OpenFOAM Studio needs.
_NOT_HERE = ("domain", "box", "point", "flow")


def _num(n) -> str:
    """A whole number with groups of three digits (fs_fmt.count)."""
    return fmt.count(n)


def _real(x: float) -> str:
    """About 4 significant digits, without an exponent for usual sizes."""
    a = abs(x)
    if a == 0:
        return "0"
    if not 1e-3 <= a < 1e12:
        return f"{x:.4g}"
    digits = max(0, 3 - math.floor(math.log10(a)))
    return fmt.decimal(x, digits)


def _seconds(sec: float) -> str:
    return f"{sec:.2f}" if sec < 10 else f"{sec:.1f}" if sec < 100 else f"{sec:.0f}"


@dataclass
class State:
    """One version of the mesh: the mesh (m), its check and what the 3D view
    shows. The view data is kept, so Undo does not compute it again."""
    mesh: object
    diag: object
    display: object
    layers: dict = field(default_factory=dict)


def _state_of(mesh) -> State:
    """Check a mesh and prepare it for the view. Worker thread."""
    diag = geo.diagnose(mesh)
    return State(mesh, diag, geo.display_mesh(mesh),
                 fs.MainWindow._problem_layers(mesh, diag))


class MeshViewport(fs.Viewport):
    """The «3D» view of OpenFOAM Studio, without the CFD layers."""

    def __init__(self, parent=None):
        super().__init__(parent)
        for key in _NOT_HERE:
            self.chk[key].setChecked(False)
            self.chk[key].hide()
        self.btn_fit_all.hide()
        self.to_model_requested.connect(self.fit_body)


class MeshFixWindow(QtWidgets.QMainWindow):
    def __init__(self):
        fs_i18n.install()
        super().__init__()
        self.setObjectName("MeshFixStudio")
        self.setWindowIcon(fs_icons.icon(fs_icons.MESHFIX))
        self.setAcceptDrops(True)
        self.store = QtCore.QSettings(SETTINGS_ORG, SETTINGS_APP)

        self.path: Path | None = None
        self.factor = 1.0               # metres per unit of the file
        self.unit_note = ""
        self.loaded: State | None = None
        self.state: State | None = None
        self.history: list[State] = []
        self.repaired: dict | None = None     # the last repair, for the info
        self.export_check = None              # Diagnosis of the written file
        self.export_info = None               # meshfix.check.SaveInfo
        self.unit_given = False               # the file gives its unit
        self._spots: list = []
        self._spot_at = -1

        self._thread: QtCore.QThread | None = None
        self._worker = None
        self._job = ""
        self._closing = False
        self._t0 = 0.0

        self._build()
        fs_i18n.on_language_changed(self._language_changed)
        self._update_title()
        self._sync()
        geom = self.store.value("geometry")
        if geom is not None:
            self.restoreGeometry(geom)
        else:
            self.resize(1280, 800)

    # ------------------------------------------------------------- the window
    def _build(self):
        bar = QtWidgets.QToolBar("Main")
        bar.setObjectName("main_bar")
        bar.setMovable(False)
        self.addToolBar(bar)

        self.btn_open = QtWidgets.QPushButton("Open…")
        self.btn_open.setToolTip("Open a mesh file: STL, 3MF, OBJ, PLY, OFF, "
                                 "glTF, VTP, VTK. You can also drop a file "
                                 "on the window.")
        self.btn_open.clicked.connect(self.open_dialog)
        bar.addWidget(self.btn_open)
        bar.addSeparator()

        bar.addWidget(QtWidgets.QLabel(" Units "))
        self.cmb_units = QtWidgets.QComboBox()
        for key, text, _factor in UNITS:
            self.cmb_units.addItem(text, key)
        self.cmb_units.setToolTip(
            "Units of the numbers in the file. The program guesses them from "
            "the size (a 3MF or glTF file gives its unit). The repair "
            "tolerances are in mm, thus the units must be correct. The "
            "export keeps these units; 3MF is always in mm, glTF in m.")
        self.cmb_units.activated.connect(self._units_chosen)
        bar.addWidget(self.cmb_units)
        bar.addSeparator()

        bar.addWidget(QtWidgets.QLabel(" Method "))
        self.cmb_method = QtWidgets.QComboBox()
        self.cmb_method.setSizeAdjustPolicy(
            QtWidgets.QComboBox.AdjustToContents)
        self._fill_methods()
        self.cmb_method.currentIndexChanged.connect(self._method_changed)
        bar.addWidget(self.cmb_method)

        self.btn_repair = QtWidgets.QPushButton("Repair")
        self.btn_repair.setToolTip("Repair the mesh with the selected method")
        self.btn_repair.clicked.connect(lambda _=False: self.repair())
        bar.addWidget(self.btn_repair)
        bar.addSeparator()

        self.btn_export = QtWidgets.QPushButton("Export…")
        self.btn_export.setToolTip(
            "Save the mesh: STL, 3MF, OBJ, PLY, OFF, glTF or VTK. The "
            "extension selects the format. A format without a unit gets the "
            "units of the input file; 3MF is in mm, glTF in m. 3MF, OBJ, "
            "ASCII STL, glTF and VTK keep the bodies apart; the other "
            "formats merge them.\nOnly for STL and glTF (float32 "
            "coordinates): the program first moves the points that the "
            "rounding would join or make into a flat triangle, by about "
            "0.001 mm (the step «STL-safe output»).")
        self.btn_export.clicked.connect(self.export_dialog)
        bar.addWidget(self.btn_export)
        bar.addSeparator()

        self.btn_undo = QtWidgets.QPushButton("Undo")
        self.btn_undo.setToolTip("Go back one step (Ctrl+Z)")
        self.btn_undo.clicked.connect(self.undo)
        bar.addWidget(self.btn_undo)
        self.btn_reset = QtWidgets.QPushButton("Reset")
        self.btn_reset.setToolTip("Go back to the mesh as it was loaded")
        self.btn_reset.clicked.connect(self.reset)
        bar.addWidget(self.btn_reset)

        split = QtWidgets.QSplitter(QtCore.Qt.Horizontal)
        self.split = split
        self.view = MeshViewport()
        self.view.btn_spot.clicked.connect(self.next_problem)
        split.addWidget(self.view)
        split.addWidget(self._build_info())
        split.setStretchFactor(0, 1)
        split.setStretchFactor(1, 0)
        split.setSizes([920, 360])
        self.setCentralWidget(split)

        sb = self.statusBar()
        self.lbl_step = QtWidgets.QLabel("")
        self.busy = QtWidgets.QProgressBar()
        self.busy.setRange(0, 0)
        self.busy.setMaximumWidth(160)
        self.busy.setTextVisible(False)
        self.busy.hide()
        sb.addWidget(self.busy)
        sb.addWidget(self.lbl_step, 1)
        self.lbl_3d = QtWidgets.QLabel("")
        sb.addPermanentWidget(self.lbl_3d)
        self._show_level()
        self.view.level_changed.connect(lambda *_a: self._show_level())
        self.tick = QtCore.QTimer(self)
        self.tick.setInterval(1000)
        self.tick.timeout.connect(self._tick)

        self._build_menu()

    def _build_info(self) -> QtWidgets.QWidget:
        box = QtWidgets.QWidget()
        lay = QtWidgets.QVBoxLayout(box)
        lay.setContentsMargins(8, 6, 8, 6)
        form = QtWidgets.QFormLayout()
        form.setLabelAlignment(QtCore.Qt.AlignLeft)
        self.info: dict[str, QtWidgets.QLabel] = {}
        colours = {"open_edges": fs.COL_HOLE, "flipped_faces": fs.COL_FLIP,
                   "non_manifold_edges": fs.COL_NM,
                   "degenerate": fs.COL_DEGEN}
        rows = [("file", "File"), ("units", "Units"), ("size", "Size"),
                ("triangles", "Triangles"), ("parts", "Parts"),
                ("closed", "Closed")]
        rows += list(fs_repair.COUNTS)
        rows += [("volume", "Volume"), ("method", "Method"),
                 ("time", "Repair time")]
        for key, text in rows:
            name = QtWidgets.QLabel(text)
            if key in colours:
                name.setStyleSheet(f"color:{colours[key]}; font-weight:bold;")
            value = QtWidgets.QLabel("—")
            value.setTextInteractionFlags(QtCore.Qt.TextSelectableByMouse)
            value.setWordWrap(True)
            form.addRow(name, value)
            self.info[key] = value
        lay.addLayout(form)
        head = QtWidgets.QLabel("What the repair did")
        head.setStyleSheet("font-weight:bold; margin-top:6px;")
        lay.addWidget(head)
        self.lbl_summary = QtWidgets.QLabel("—")
        self.lbl_summary.setWordWrap(True)
        self.lbl_summary.setTextInteractionFlags(
            QtCore.Qt.TextSelectableByMouse)
        lay.addWidget(self.lbl_summary)
        head = QtWidgets.QLabel("Log")
        head.setStyleSheet("font-weight:bold; margin-top:6px;")
        lay.addWidget(head)
        self.log = QtWidgets.QPlainTextEdit()
        self.log.setReadOnly(True)
        self.log.setMaximumBlockCount(500)
        lay.addWidget(self.log, 1)
        box.setMinimumWidth(300)
        return box

    def _build_menu(self):
        mb = self.menuBar()
        m = mb.addMenu("&File")
        self.act_open = m.addAction("Open…", self.open_dialog)
        self.act_open.setShortcut(QtGui.QKeySequence.Open)
        self.act_export = m.addAction("Export…", self.export_dialog)
        self.act_export.setShortcut(QtGui.QKeySequence("Ctrl+E"))
        m.addSeparator()
        m.addAction("Quit", self.close).setShortcut(QtGui.QKeySequence.Quit)

        m = mb.addMenu("&Edit")
        self.act_repair = m.addAction("Repair", lambda: self.repair())
        self.act_repair.setShortcut(QtGui.QKeySequence("Ctrl+R"))
        self.act_undo = m.addAction("Undo", self.undo)
        self.act_undo.setShortcut(QtGui.QKeySequence.Undo)
        self.act_reset = m.addAction("Reset", self.reset)

        m = mb.addMenu("&View")
        m.addAction("Fit Model", self.view.fit_body).setShortcut(
            QtGui.QKeySequence("Ctrl+0"))
        for text, which in (("Iso", "iso"), ("X", "yz"), ("Y", "xz"),
                            ("Z", "xy")):
            m.addAction(text, lambda v=which: self.view.set_view(v))
        m.addSeparator()
        lang = m.addMenu("Language")
        self.lang_group = QtGui.QActionGroup(self)
        self.act_lang = {}
        for code, name in fs_i18n.LANGUAGES.items():
            act = lang.addAction(name)
            act.setCheckable(True)
            act.setChecked(code == fs_i18n.language())
            act.triggered.connect(lambda _=False, c=code: self.set_language(c))
            self.lang_group.addAction(act)
            self.act_lang[code] = act

        m = mb.addMenu("&Help")
        m.addAction("About MeshFix Studio", self.about)

    def _fill_methods(self):
        """The method list of fs_repair; a method whose tool is missing is
        gray, and its tooltip tells why."""
        cmb = self.cmb_method
        cmb.blockSignals(True)
        want = cmb.currentData() or self.store.value("method",
                                                     fs_repair.DEFAULT)
        cmb.clear()
        model = cmb.model()
        for key, label, _tip in fs_repair.METHODS:
            cmb.addItem(label, key)
            row = cmb.count() - 1
            item = model.item(row)
            if fs_repair.available(key):
                item.setToolTip(fs_repair.tooltip(key))
            else:
                item.setEnabled(False)
                item.setToolTip(tr("Not available: {why}",
                                   why=fs_repair.why_not(key)))
        rows = [r for r in range(cmb.count()) if model.item(r).isEnabled()]
        pick = next((r for r in rows if cmb.itemData(r) == want),
                    rows[0] if rows else 0)
        cmb.setCurrentIndex(pick)
        cmb.blockSignals(False)
        self._method_changed(pick)

    def method(self) -> str:
        return self.cmb_method.currentData() or fs_repair.DEFAULT

    def _method_changed(self, row: int):
        item = self.cmb_method.model().item(row)
        if item is None:
            return
        self.cmb_method.setToolTip(item.toolTip())
        if item.isEnabled():
            self.store.setValue("method", item.data(QtCore.Qt.UserRole))
        self._sync()

    def _show_level(self):
        lv = getattr(self.view, "level", None)
        text = lv.text() if lv is not None else f"3D: {self.view.backend}"
        self.lbl_3d.setText(text)

    # ----------------------------------------------------------------- state
    @property
    def busy_now(self) -> bool:
        return self._thread is not None

    def _sync(self):
        """Enable the buttons for the current state."""
        if not hasattr(self, "btn_reset"):
            return
        idle = not self.busy_now
        has = self.state is not None
        item = self.cmb_method.model().item(self.cmb_method.currentIndex())
        can_fix = item is not None and item.isEnabled()
        for w in (self.btn_open, self.act_open if hasattr(self, "act_open")
                  else None):
            if w is not None:
                w.setEnabled(idle)
        self.cmb_units.setEnabled(idle and has)
        self.cmb_method.setEnabled(idle)
        self.btn_repair.setEnabled(idle and has and can_fix)
        self.btn_export.setEnabled(idle and has)
        self.btn_undo.setEnabled(idle and bool(self.history))
        self.btn_reset.setEnabled(idle and has and self.state is not self.loaded)
        if hasattr(self, "act_repair"):
            self.act_repair.setEnabled(self.btn_repair.isEnabled())
            self.act_export.setEnabled(self.btn_export.isEnabled())
            self.act_undo.setEnabled(self.btn_undo.isEnabled())
            self.act_reset.setEnabled(self.btn_reset.isEnabled())

    def _update_title(self):
        name = self.path.name if self.path else ""
        self.setWindowTitle(f"{name} — {APP_NAME}" if name else APP_NAME)

    def _say(self, text: str):
        self.lbl_step.setText(text)

    def _note(self, text: str):
        self.log.appendPlainText(f"{time.strftime('%H:%M:%S')}  {text}")

    # ------------------------------------------------------------ the worker
    def _start(self, name: str, job, text: str):
        """Run job(meshes, say) in a worker thread (foam_studio.RepairWorker).
        The window stays live; one job at a time."""
        if self.busy_now:
            return False
        self._job = name
        self._t0 = time.monotonic()
        self._busy_text = text
        self._say(text)
        self.busy.show()
        self.tick.start()
        self._worker = fs.RepairWorker(job, [])
        self._thread = QtCore.QThread(self)
        self._thread.setObjectName(f"meshfix-{name}")
        self._worker.moveToThread(self._thread)
        self._thread.started.connect(self._worker.run)
        self._worker.step.connect(self._on_step)
        self._worker.ready.connect(self._on_ready)
        self._worker.failed.connect(self._on_failed)
        self._thread.start()
        self._sync()
        return True

    def _end(self):
        self.tick.stop()
        self.busy.hide()
        if self._thread is not None:
            self._thread.quit()
            self._thread.wait(5000)
            self._thread = None
        self._worker = None
        self._job = ""
        self._sync()

    def _tick(self):
        sec = int(time.monotonic() - self._t0)
        self._say(tr("{text} {sec} s", text=self._busy_text, sec=sec))

    @QtCore.Slot(str)
    def _on_step(self, text: str):
        if self._closing:
            return
        verdict = text.endswith(fs_meshfix.VERDICT)
        text = text.rstrip(fs_meshfix.VERDICT)
        self._busy_text = text
        self._tick()
        if verdict:
            self._note(text)

    @QtCore.Slot(str)
    def _on_failed(self, message: str):
        job = self._job
        self._end()
        if self._closing:
            return
        self._say("")
        self._note(tr("Failed: {message}", message=message))
        title = tr("Open failed") if job == "load" else tr("Repair failed")
        QtWidgets.QMessageBox.critical(self, title, message)

    @QtCore.Slot(object)
    def _on_ready(self, result):
        job = self._job
        self._end()
        if self._closing:
            return
        if job == "load":
            self._loaded(result)
        elif job == "repair":
            self._repaired(result)
        elif job == "units":
            self._units_done(result)
        elif job == "check":
            self._export_checked(result)

    # ------------------------------------------------------------------ open
    def open_dialog(self):
        if self.busy_now:
            return
        start = self.store.value("folder", str(Path.home()))
        path, _ = QtWidgets.QFileDialog.getOpenFileName(
            self, tr("Open a mesh"), start, geo.open_filter())
        if path:
            self.open_file(path)

    def open_file(self, path) -> bool:
        """Read a mesh file in the worker thread."""
        path = Path(path)
        if self.busy_now or not path.is_file():
            return False
        self.store.setValue("folder", str(path.parent))

        def job(_meshes, say):
            say(tr("Reading {name}…", name=path.name))
            raw = geo.load_surface(str(path))
            factor, note = geo.guess_scale(raw)
            chk = geo.formats()
            given = bool(chk) and chk.UNIT in raw.field_data
            mesh = geo.apply_scale(raw, factor)
            say(tr("Checking {name}…", name=path.name))
            return path, factor, (note, given), _state_of(mesh)
        return self._start("load", job, tr("Reading {name}…", name=path.name))

    def _loaded(self, result):
        path, factor, (note, given), state = result
        self.path = path
        self.factor = factor
        self.unit_note = note
        self.unit_given = given
        self.loaded = self.state = state
        self.history.clear()
        self.repaired = None
        self._select_units(factor)
        self._update_title()
        self._show(reset_camera=True)
        self._note(tr("Opened {name}: {n} triangles", name=path.name,
                      n=_num(state.diag.n_triangles)))
        self._say(tr("Opened {name}", name=path.name))
        self._sync()

    def _select_units(self, factor: float):
        for i, (_key, _text, f) in enumerate(UNITS):
            if abs(f - factor) <= 1e-12 * max(f, 1.0):
                self.cmb_units.setCurrentIndex(i)
                return

    def _units_chosen(self, row: int):
        new = UNITS[row][2]
        if self.state is None or abs(new - self.factor) < 1e-15:
            return
        k = new / self.factor
        states = [self.loaded, self.state] + self.history

        def job(_meshes, say):
            say(tr("Changing the units…"))
            done: dict[int, State] = {}
            for s in states:
                if s is not None and id(s) not in done:
                    done[id(s)] = _state_of(geo.apply_scale(s.mesh, k))
            return new, done
        if not self._start("units", job, tr("Changing the units…")):
            self._select_units(self.factor)

    def _units_done(self, result):
        new, done = result
        self.factor = new
        self.unit_note = ""
        self.loaded = done[id(self.loaded)]
        self.state = done[id(self.state)]
        self.history = [done[id(s)] for s in self.history]
        self.repaired = None
        self._show(reset_camera=True)
        self._note(tr("Units: {units}", units=self.units_text()))
        self._say("")

    def units_text(self) -> str:
        for _key, text, f in UNITS:
            if abs(f - self.factor) <= 1e-12 * max(f, 1.0):
                return text
        return f"× {self.factor:g} m"

    # ---------------------------------------------------------------- repair
    def repair(self, key: str | None = None) -> bool:
        """Repair the mesh with fs_repair (the same call as the «Repair»
        button of OpenFOAM Studio), in the worker thread."""
        if self.state is None or self.busy_now:
            return False
        key = key or self.method()
        if not fs_repair.available(key):
            QtWidgets.QMessageBox.information(self, "Repair",
                                              fs_repair.why_not(key))
            return False
        before = self.state
        label = fs_repair.label(key)

        def job(_meshes, say):
            t0 = time.perf_counter()
            if geo.formats() and hasattr(fs_repair, "repair_bodies"):
                # Each body alone, without the step stl_safe_output: the
                # export applies it to STL and glTF only.
                out = fs_repair.repair_bodies(key, before.mesh, say,
                                              stl_safe=False)
            else:
                out = fs_repair.repair_meshes(key, [before.mesh], say)[0]
            sec = time.perf_counter() - t0
            say(tr("Checking the result…"))
            return key, sec, fs_repair.summary(key), _state_of(out)
        self._note(tr("Repair: {method}", method=label))
        return self._start("repair", job,
                           tr("Repairing ({method})…", method=label))

    def _repaired(self, result):
        key, sec, summary, state = result
        before = self.state
        self._push(before)
        self.state = state
        self.repaired = {"key": key, "seconds": sec, "summary": summary,
                         "steps": fs_repair.steps(key),
                         "before": before.diag}
        self._show(reset_camera=False)
        d0, d1 = before.diag, state.diag
        text = tr("Repaired with {method} in {sec} s: {result}",
                  method=fs_repair.label(key), sec=_seconds(sec),
                  result=(tr("closed and clean") if fs_repair.clean(d1)
                          else tr("problems are left")))
        self._say(text)
        self._note(text)
        if d0.n_triangles != d1.n_triangles:
            self._note(tr("Triangles: {a} → {b}", a=_num(d0.n_triangles),
                          b=_num(d1.n_triangles)))

    def _push(self, state: State):
        self.history.append(state)
        del self.history[:-HISTORY]

    def undo(self):
        if self.busy_now or not self.history:
            return
        self.state = self.history.pop()
        self.repaired = None
        self._show(reset_camera=False)
        self._say(tr("Undone"))
        self._note(tr("Undo"))
        self._sync()

    def reset(self):
        if self.busy_now or self.loaded is None or self.state is self.loaded:
            return
        self._push(self.state)
        self.state = self.loaded
        self.repaired = None
        self._show(reset_camera=False)
        self._say(tr("Back to the loaded mesh"))
        self._note(tr("Reset to the loaded mesh"))
        self._sync()

    # ---------------------------------------------------------------- export
    def export_dialog(self):
        if self.state is None or self.busy_now:
            return
        folder = self.store.value("export_folder",
                                  str(self.path.parent if self.path
                                      else Path.home()))
        stem = self.path.stem if self.path else "mesh"
        if self.repaired is not None or self.state is not self.loaded:
            stem += "_repaired"
        chk = geo.formats()
        if chk is None:
            path, _ = QtWidgets.QFileDialog.getSaveFileName(
                self, tr("Export binary STL"),
                str(Path(folder) / f"{stem}.stl"), tr("STL binary (*.stl)"))
            if path:
                self.export_file(path)
            return
        key = str(self.store.value("export_format", "stl") or "stl")
        if key not in chk.FORMATS:
            key = "stl"
        filters = chk.export_filters(chk.body_count(self.state.mesh))
        texts = [text for _k, text in filters]
        path, picked = QtWidgets.QFileDialog.getSaveFileName(
            self, tr("Export"),
            str(Path(folder) / (stem + chk.FORMATS[key].suffix)),
            ";;".join(texts), dict(filters)[key])
        if path:
            keys = [k for k, text in filters if text == picked]
            self.export_file(path, keys[0] if keys else None)

    def export_file(self, path, fmt: str | None = None) -> Path | None:
        """Write the current mesh to `path`, in the units of the input file
        for a format without a unit. The format: `fmt` (the filter of the
        dialog) when it has the extension of the name, else the format of
        the extension (meshfix.check.format_for). Without the formats of
        the shared package: binary STL. Returns the path, or None on an
        error (a message box)."""
        if self.state is None:
            return None
        path = Path(path)
        chk = geo.formats()
        info = None
        try:
            if chk is None:
                if path.suffix.lower() != ".stl":
                    path = path.with_suffix(".stl")
                out = geo.apply_scale(self.state.mesh, 1.0 / self.factor)
                geo.write_stl(out, str(path), binary=True)
            else:
                if path.suffix.lower() not in chk.READERS:
                    path = Path(str(path) + chk.FORMATS[fmt or "stl"].suffix)
                form = chk.format_for(path, fmt)
                info = chk.save_mesh(self.state.mesh, path, form,
                                     unit_m=self.factor)
        except Exception as exc:                          # noqa: BLE001
            QtWidgets.QMessageBox.critical(self, tr("Export failed"), str(exc))
            return None
        self.store.setValue("export_folder", str(path.parent))
        self.export_info = info
        if info is None:
            text = tr("Exported {name} ({n} triangles, {units})",
                      name=path.name, n=_num(self.state.diag.n_triangles),
                      units=self.units_text())
        else:
            self.store.setValue("export_format", info.fmt.key)
            text = tr("Exported {name} ({format}, {n} triangles, {units})",
                      name=path.name, format=chk.format_label(info.fmt),
                      n=_num(self.state.diag.n_triangles), units=info.unit)
        self._say(text)
        self._note(text)
        if info is not None:
            self._note(chk.save_note(info))
            moved = sum((info.stl_safe or {}).get(k, 0)
                        for k in ("moved", "collapsed", "flipped"))
            if moved:
                self._note(tr("STL-safe output: {n} changes (points moved by "
                              "up to {d} {units})", n=_num(moved),
                              d=f"{info.stl_safe.get('max_move', 0):.2g}",
                              units=info.unit))
        self._check_export(path)
        return path

    def _check_export(self, path: Path):
        """Read the written STL again and check it (worker thread).

        STL keeps float32 coordinates and no topology: a reader joins equal
        points. Vertices that the repair keeps apart (the two copies of a
        pinched edge, points nearer than the float32 step) are then one
        point again, and the file can show problems that the mesh in memory
        does not have. The person must know that.
        """
        want = self.state.diag
        self.export_check = None

        def job(_meshes, say):
            say(tr("Checking {name}…", name=path.name))
            return path, want, geo.diagnose(geo.load_surface(str(path)))
        self._start("check", job, tr("Checking {name}…", name=path.name))

    def _export_checked(self, result):
        path, want, got = result
        self.export_check = got
        lost = [(label, fs_repair.counts(want)[attr], fs_repair.counts(got)[attr])
                for attr, label in fs_repair.COUNTS
                if fs_repair.counts(got)[attr] != fs_repair.counts(want)[attr]]
        if got.n_triangles != want.n_triangles:
            lost.append(("Triangles", want.n_triangles, got.n_triangles))
        if not lost:
            text = tr("Check of {name}: the same as the mesh", name=path.name)
            self._say(text)
            self._note(text)
            return
        parts = ", ".join(f"{tr(label)} {_num(a)} → {_num(b)}"
                          for label, a, b in lost)
        if path.suffix.lower() == ".stl":
            text = tr("Check of {name}: the STL file is not the same as the "
                      "mesh ({parts}). STL keeps float32 coordinates, and a "
                      "reader joins points at the same place: vertices that "
                      "the repair keeps apart become one point again.",
                      name=path.name, parts=parts)
        else:
            text = tr("Check of {name}: the file is not the same as the mesh "
                      "({parts}).", name=path.name, parts=parts)
        self._say(tr("Check of {name}: the file has problems",
                     name=path.name))
        self._note(text)
        QtWidgets.QMessageBox.warning(self, "Export check", text)

    # ------------------------------------------------------------ the 3D view
    def _show(self, reset_camera: bool):
        st = self.state
        v = self.view
        v.clear_problems()
        self._spots, self._spot_at = [], -1
        if st is None:
            v.show_body(None)
            self._refresh_info()
            return
        v.show_body(st.display, reset=reset_camera)
        d = st.diag
        counts = {"holes": d.open_edges, "flip": d.flipped_faces,
                  "nm": d.non_manifold_edges, "degen": d.degenerate}
        for key, data in st.layers.items():
            v.show_problem(key, data, counts[key])
        v.btn_spot.setEnabled(bool(d.open_edges or d.flipped_faces
                                   or d.degenerate))
        self._refresh_info()
        self._sync()

    def next_problem(self):
        """Fly the camera to the next hole, flipped part or degenerate
        face, the largest first (as in OpenFOAM Studio)."""
        st = self.state
        if st is None:
            return
        if not self._spots:
            self._spots = geo.problem_spots(st.mesh, st.diag)
            self._spot_at = -1
        if not self._spots:
            self._say(tr("No problem spots found"))
            return
        self._spot_at = (self._spot_at + 1) % len(self._spots)
        spot = self._spots[self._spot_at]
        radius = spot["size"] if spot["size"] > 0 else st.diag.diagonal * 0.01
        centre = np.asarray(spot["centre"], dtype=float)
        self.view.look_at(spot["centre"], radius,
                          outward=centre - np.asarray(st.mesh.center))
        self.view.mark_spot(spot["centre"], radius)
        self._say(tr("{what} {i} of {n}", what=tr(spot["what"]).capitalize(),
                     i=self._spot_at + 1, n=len(self._spots)))

    # ------------------------------------------------------------- info panel
    def _refresh_info(self):
        st = self.state
        info = self.info
        if st is None:
            for lab in info.values():
                lab.setText("—")
            self.lbl_summary.setText("—")
            self.lbl_summary.setToolTip("")
            return
        d = st.diag
        rep = self.repaired
        d0 = rep["before"] if rep else None
        unit = self.units_text()
        f = self.factor

        def pair(fmt, a, b=None):
            if b is None or fmt(a) == fmt(b):
                return fmt(b if b is not None else a)
            return f"{fmt(a)} → {fmt(b)}"

        info["file"].setText(self.path.name if self.path else "—")
        info["file"].setToolTip(str(self.path) if self.path else "")
        if not self.unit_note:
            info["units"].setText(unit)
        elif self.unit_given:
            info["units"].setText(unit + "  (" + self.unit_note + ")")
        else:
            info["units"].setText(unit + tr("  (guessed: {note})",
                                            note=self.unit_note))
        sx, sy, sz = (s / f for s in d.size)
        info["size"].setText(f"{_real(sx)} × {_real(sy)} × {_real(sz)} {unit}")
        info["triangles"].setText(pair(_num, d0.n_triangles if d0 else d.n_triangles,
                                       d.n_triangles if d0 else None))
        info["parts"].setText(pair(_num, d0.parts if d0 else d.parts,
                                   d.parts if d0 else None))
        yes_no = (lambda c: tr("yes") if c else tr("no"))
        info["closed"].setText(pair(yes_no, d0.closed if d0 else d.closed,
                                    d.closed if d0 else None))
        for attr, _label in fs_repair.COUNTS:
            a = getattr(d0 if d0 else d, attr)
            b = getattr(d, attr) if d0 else None
            info[attr].setText(pair(_num, a, b))
        vol = (lambda v: f"{_real(v / f ** 3)} {unit}³")
        info["volume"].setText(pair(vol, d0.volume if d0 else d.volume,
                                    d.volume if d0 else None))
        if rep:
            info["method"].setText(fs_repair.label(rep["key"]))
            info["time"].setText(tr("{sec} s", sec=_seconds(rep["seconds"])))
            self.lbl_summary.setText(rep["summary"] or "—")
            self.lbl_summary.setToolTip(rep.get("steps") or "")
        else:
            info["method"].setText("—")
            info["time"].setText("—")
            self.lbl_summary.setText("—")

    # ---------------------------------------------------------------- language
    def set_language(self, code: str):
        self.store.setValue("language", code)
        fs_i18n.set_language(code)
        for c, act in self.act_lang.items():
            act.setChecked(c == fs_i18n.language())

    def _language_changed(self, _lang: str):
        self._fill_methods()
        self._show_level()
        self._refresh_info()

    def about(self):
        QtWidgets.QMessageBox.about(
            self, "About MeshFix Studio",
            tr("MeshFix Studio repairs a triangle mesh (STL, 3MF, OBJ, PLY, "
               "OFF, glTF, VTK) with the repair of OpenFOAM Studio: meshfix "
               "(Rust), alpha wrap, "
               "the Windows repair service and the volume rebuild.\n\n"
               "Problems in the 3D view: red — holes (open edges), magenta "
               "— flipped faces, yellow — non-manifold edges, green — "
               "degenerate faces.\n\n3D: {level}",
               level=self.lbl_3d.text()))

    # ------------------------------------------------------- drag and drop
    @staticmethod
    def _dropped_file(event) -> str | None:
        md = event.mimeData()
        if not md.hasUrls():
            return None
        for url in md.urls():
            p = url.toLocalFile()
            if p and Path(p).suffix.lower() in SUFFIXES:
                return p
        return None

    def dragEnterEvent(self, event):                      # noqa: N802
        if not self.busy_now and self._dropped_file(event):
            event.acceptProposedAction()
        else:
            event.ignore()

    def dragMoveEvent(self, event):                       # noqa: N802
        self.dragEnterEvent(event)

    def dropEvent(self, event):                           # noqa: N802
        path = self._dropped_file(event)
        if path and self.open_file(path):
            event.acceptProposedAction()

    # ------------------------------------------------------------------ close
    def closeEvent(self, event):                          # noqa: N802
        """Close at once, also during a repair.

        A repair in Rust cannot stop in the middle. The window does not wait
        for it and does not destroy the running thread (that is a qFatal,
        T26): the result is ignored (_closing), and run() ends the process
        with foam_studio.end_process after a short wait.
        """
        self._closing = True
        self.tick.stop()
        try:
            self.store.setValue("geometry", self.saveGeometry())
            self.store.sync()
        except Exception:                                 # noqa: BLE001
            pass
        if self._thread is not None and not self._thread.isRunning():
            self._end()
        try:
            self.view.plotter.close()
        except Exception:                                 # noqa: BLE001
            pass
        super().closeEvent(event)


def start_language(store: QtCore.QSettings | None = None) -> str:
    """FS_LANG, else the setting of this program, else English."""
    store = store or QtCore.QSettings(SETTINGS_ORG, SETTINGS_APP)
    lang = fs_i18n.env_language() or str(store.value("language", "") or "")
    lang = lang if lang in fs_i18n.LANGUAGES else DEFAULT_LANGUAGE
    fs_i18n.set_language(lang)
    return lang


def run(app, win, grace_ms: int = CLOSE_GRACE_MS) -> None:
    """The event loop, then the end of the process without a qFatal: a
    repair that still runs is not waited for longer than grace_ms."""
    rc = app.exec()
    fs.end_process(win, rc, grace_ms=grace_ms)


def main(argv=None):
    argv = list(sys.argv if argv is None else argv)
    QtCore.QCoreApplication.setAttribute(QtCore.Qt.AA_ShareOpenGLContexts)
    fs_icons.own_taskbar_button(APP_USER_MODEL_ID)
    app = QtWidgets.QApplication(argv)
    app.setStyle("Fusion")
    app.setApplicationName(APP_NAME)
    app.setWindowIcon(fs_icons.icon(fs_icons.MESHFIX))
    start_language()
    win = MeshFixWindow()
    win.show()
    if len(argv) > 1 and os.path.isfile(argv[1]):
        win.open_file(argv[1])
    run(app, win)


if __name__ == "__main__":
    main()
