"""The «Polars» window: Cl, Cd, Cm, L/D against α from saved cases.

The data and the plots come from fs_polars, the report from fs_report (both
have no Qt). This window only shows them:

* the source: the cases selected in «Saved», or an angle series of the
  case folder (`<base>_aoa<angle>`, the reruns `_r2` of one point are one
  point: the newest run is on, the others are in the list but off);
* the averaging window: steady - the last N iterations; transient - the
  last part of the time, or from a time;
* the table (a check box per case), the reference values and the warnings,
  the five plots;
* «Save Table (CSV)…», «Save Plots…», «Simple Report» (one click: a folder
  in <data>/reports, the page opens in the browser).
"""

from __future__ import annotations

import datetime as _dt
import math
import threading
from pathlib import Path

import numpy as np
from PySide6 import QtCore, QtGui, QtWidgets

import fs_case as fc
import fs_i18n
import fs_icons
import fs_paths
import fs_polars as pol
import fs_report as rpt
from fs_i18n import tr

fs_i18n.install()

_COLORS = {"bad": "#c0392b", "warn": "#b8860b", "ok": "#4c9a52",
           "dim": "#888888"}
SELECTED = "__selected__"

COLUMNS = ("Use", "Case", "α, °", "Cl", "Cd", "Cm", "L/D", "Cs", "State",
           "Averaged over", "Max residual")
COL_CS = 7


def reports_dir() -> Path:
    return fs_paths.data_dir() / "reports"


def _num_item(text: str, value) -> QtWidgets.QTableWidgetItem:
    item = QtWidgets.QTableWidgetItem(text)
    item.setTextAlignment(QtCore.Qt.AlignRight | QtCore.Qt.AlignVCenter)
    item.setData(QtCore.Qt.UserRole + 1, value)
    return item


class PolarWindow(QtWidgets.QDialog):
    """`cases`: CaseInfo list (the selection); `library`: all cases of the
    folder (for the series list)."""

    def __init__(self, cases: list, library: list | None = None, parent=None,
                 source: str = SELECTED):
        super().__init__(parent)
        self.setWindowTitle(tr("Polars"))
        self.setWindowIcon(fs_icons.icon(fs_icons.POLARS))
        self.setWindowFlag(QtCore.Qt.WindowMaximizeButtonHint, True)
        self.resize(1280, 900)
        screen = (parent.screen() if parent is not None else
                  QtGui.QGuiApplication.primaryScreen())
        if screen is not None:
            # A laptop screen: the buttons at the bottom must stay visible.
            room = screen.availableGeometry()
            self.resize(min(1280, room.width() - 40),
                        min(900, room.height() - 60))
        self.selected = list(cases or [])
        self.library = list(library or [])
        self.series = pol.find_series(self.library or self.selected)
        self.points: list[pol.Point] = []
        self._cases: list = []           # CaseInfo of the table rows
        self._on: dict[str, bool] = {}   # path -> check box state
        self._filling = False
        self.last_report = None

        v = QtWidgets.QVBoxLayout(self)

        top = QtWidgets.QHBoxLayout()
        top.addWidget(QtWidgets.QLabel("Cases:"))
        self.cmb_source = QtWidgets.QComboBox()
        self.cmb_source.setMinimumWidth(320)
        top.addWidget(self.cmb_source, 1)
        top.addSpacing(12)
        top.addWidget(QtWidgets.QLabel("Steady: last"))
        self.spin_n = QtWidgets.QSpinBox()
        self.spin_n.setRange(5, 100000)
        self.spin_n.setValue(pol.STEADY_N)
        self.spin_n.setSuffix(tr(" iterations"))
        self.spin_n.setToolTip(
            "The mean and the spread of a steady run are taken over the last "
            "N iterations (the «Saved» tab uses 50).")
        top.addWidget(self.spin_n)
        top.addSpacing(12)
        top.addWidget(QtWidgets.QLabel("Transient: last"))
        self.spin_frac = QtWidgets.QSpinBox()
        self.spin_frac.setRange(1, 100)
        self.spin_frac.setValue(round(pol.TRANSIENT_FRAC * 100))
        self.spin_frac.setSuffix(" %")
        self.spin_frac.setToolTip(
            "A transient run: the time mean and the standard deviation over "
            "this last part of the computed time. Use whole periods of the "
            "oscillation.")
        top.addWidget(self.spin_frac)
        top.addWidget(QtWidgets.QLabel("or from t ="))
        self.spin_from = QtWidgets.QDoubleSpinBox()
        self.spin_from.setRange(0.0, 1e6)
        self.spin_from.setDecimals(5)
        self.spin_from.setSingleStep(0.001)
        self.spin_from.setSuffix(" s")
        self.spin_from.setSpecialValueText("—")
        self.spin_from.setToolTip(
            "A transient run: average from this time to the end. «—»: use "
            "the percentage.")
        top.addWidget(self.spin_from)
        v.addLayout(top)

        split = QtWidgets.QSplitter(QtCore.Qt.Vertical)
        upper = QtWidgets.QWidget()
        ul = QtWidgets.QVBoxLayout(upper)
        ul.setContentsMargins(0, 0, 0, 0)
        self.table = QtWidgets.QTableWidget(0, len(COLUMNS))
        self.table.setHorizontalHeaderLabels(list(COLUMNS))
        self.table.verticalHeader().setVisible(False)
        self.table.setEditTriggers(QtWidgets.QAbstractItemView.NoEditTriggers)
        self.table.setSelectionBehavior(QtWidgets.QAbstractItemView.SelectRows)
        self.table.setAlternatingRowColors(True)
        head = self.table.horizontalHeader()
        head.setSectionResizeMode(QtWidgets.QHeaderView.ResizeToContents)
        head.setSectionResizeMode(1, QtWidgets.QHeaderView.Stretch)
        self.table.itemChanged.connect(self._item_changed)
        ul.addWidget(self.table, 1)
        self.lbl_ref = QtWidgets.QLabel("")
        self.lbl_ref.setTextFormat(QtCore.Qt.RichText)
        self.lbl_ref.setWordWrap(True)
        self.lbl_ref.setTextInteractionFlags(QtCore.Qt.TextSelectableByMouse)
        ul.addWidget(self.lbl_ref)
        split.addWidget(upper)

        from matplotlib.backends.backend_agg import FigureCanvasAgg
        from matplotlib.figure import Figure
        self.fig = Figure(figsize=(12, 6), dpi=100, layout="constrained")
        self.canvas = FigureCanvasAgg(self.fig)
        self.plot = QtWidgets.QLabel()
        self.plot.setAlignment(QtCore.Qt.AlignCenter)
        self.plot.setMinimumSize(480, 320)
        self.plot.setSizePolicy(QtWidgets.QSizePolicy.Ignored,
                                QtWidgets.QSizePolicy.Ignored)
        split.addWidget(self.plot)
        split.setStretchFactor(0, 2)
        split.setStretchFactor(1, 3)
        v.addWidget(split, 1)

        self.lbl_status = QtWidgets.QLabel("")
        self.lbl_status.setWordWrap(True)
        self.lbl_status.setTextFormat(QtCore.Qt.RichText)
        self.lbl_status.setOpenExternalLinks(True)
        v.addWidget(self.lbl_status)

        row = QtWidgets.QHBoxLayout()
        self.btn_csv = QtWidgets.QPushButton("Save Table (CSV)…")
        self.btn_plots = QtWidgets.QPushButton("Save Plots…")
        self.btn_plots.setToolTip(
            "Each plot and one picture of all plots, as PNG or SVG.")
        self.btn_report = QtWidgets.QPushButton("Simple Report")
        self.btn_report.setToolTip(
            "One folder with a page (settings, reference values, table, "
            "polars, Cp on the body and a slice), the plots and the CSV. "
            "The page opens in the browser.")
        f = self.btn_report.font()
        f.setBold(True)
        self.btn_report.setFont(f)
        self.btn_close = QtWidgets.QPushButton("Close")
        self.btn_csv.clicked.connect(self.save_csv)
        self.btn_plots.clicked.connect(self.save_plots)
        self.btn_report.clicked.connect(self.make_report)
        self.btn_close.clicked.connect(self.close)
        for b in (self.btn_csv, self.btn_plots, self.btn_report):
            row.addWidget(b)
        row.addStretch(1)
        row.addWidget(self.btn_close)
        v.addLayout(row)

        self._fill_sources(source)
        self.cmb_source.currentIndexChanged.connect(lambda _: self._load())
        for w in (self.spin_n, self.spin_frac):
            w.valueChanged.connect(lambda _: self._recompute())
        self.spin_from.valueChanged.connect(lambda _: self._recompute())
        fs_i18n.on_language_changed(self._language_changed)
        self._load()

    # --- sources ---------------------------------------------------------

    def _fill_sources(self, want: str = SELECTED) -> None:
        self.cmb_source.blockSignals(True)
        self.cmb_source.clear()
        if self.selected:
            self.cmb_source.addItem(
                fs_i18n.plural(len(self.selected), "Selected case ({n})",
                               "Selected cases ({n})"), SELECTED)
        for s in self.series:
            angles = ", ".join(f"{a:g}°" for a in s.angles)
            self.cmb_source.addItem(
                tr("Series “{base}”: {angles}", base=s.base, angles=angles),
                s.base)
        i = self.cmb_source.findData(want)
        self.cmb_source.setCurrentIndex(max(i, 0))
        self.cmb_source.blockSignals(False)

    def _source_cases(self) -> tuple[list, set]:
        """(cases of the table, paths that are off at the start)."""
        key = self.cmb_source.currentData()
        if key == SELECTED or key is None:
            latest, older = pol.latest_runs(self.selected)
            ordered = [c for c in self.selected]
            return ordered, {c.path for c in older}
        for s in self.series:
            if s.base == key:
                return list(s.members) + list(s.superseded), {
                    c.path for c in s.superseded}
        return [], set()

    def _load(self) -> None:
        cases, off = self._source_cases()
        self._cases = cases
        self._on = {c.path: c.path not in off for c in cases}
        self._recompute()

    # --- data ------------------------------------------------------------

    def _settings(self) -> dict:
        t_from = self.spin_from.value()
        return dict(steady_n=self.spin_n.value(),
                    frac=self.spin_frac.value() / 100.0,
                    t_from=t_from if t_from > 0 else None)

    def _recompute(self) -> None:
        QtWidgets.QApplication.setOverrideCursor(QtCore.Qt.WaitCursor)
        try:
            kw = self._settings()
            self._all = pol.read_points(self._cases, **kw)
        finally:
            QtWidgets.QApplication.restoreOverrideCursor()
        self._refresh()

    def used(self) -> list[pol.Point]:
        return [p for p in self._all if self._on.get(p.path, True)]

    def _refresh(self) -> None:
        self.points = self.used()
        self._fill_table()
        self._fill_ref()
        self._draw()
        has = bool(self.points)
        for b in (self.btn_csv, self.btn_plots, self.btn_report):
            b.setEnabled(has)

    def _fill_table(self) -> None:
        self._filling = True
        try:
            pts = self._all
            self.table.setRowCount(len(pts))
            older = {c.path for c in pol.latest_runs(self._cases)[1]}
            for r, p in enumerate(pts):
                use = QtWidgets.QTableWidgetItem("")
                use.setFlags(QtCore.Qt.ItemIsUserCheckable
                             | QtCore.Qt.ItemIsEnabled
                             | QtCore.Qt.ItemIsSelectable)
                use.setCheckState(QtCore.Qt.Checked if self._on.get(p.path, True)
                                  else QtCore.Qt.Unchecked)
                use.setData(QtCore.Qt.UserRole, p.path)
                self.table.setItem(r, 0, use)
                name = QtWidgets.QTableWidgetItem(p.name)
                tip = [p.path]
                if p.path in older:
                    tip.append(tr("an older run of this point: the newest "
                                  "run is on"))
                    name.setForeground(QtGui.QColor(_COLORS["dim"]))
                tip += list(p.notes)
                name.setToolTip(fs_i18n.join("\n", tip))
                self.table.setItem(r, 1, name)
                self.table.setItem(r, 2, _num_item(pol.fmt(p.alpha), p.alpha))

                def coef(col, n):
                    v, s = p.value(n), p.std(n)
                    item = _num_item(pol.fmt(v), v)
                    if s is not None:
                        item.setToolTip(tr("± {s} (standard deviation in the "
                                           "window)", s=pol.fmt(s, 2)))
                    self.table.setItem(r, col, item)
                coef(3, "Cl")
                coef(4, "Cd")
                coef(5, "CmPitch")
                ld = _num_item(pol.fmt(p.ld), p.ld)
                if p.ld_std is not None:
                    ld.setToolTip(tr("± {s}", s=pol.fmt(p.ld_std, 2)))
                self.table.setItem(r, 6, ld)
                coef(COL_CS, "Cs")
                state = QtWidgets.QTableWidgetItem(pol.verdict_word(p.verdict))
                state.setForeground(QtGui.QColor(
                    _COLORS["ok"] if p.converged else _COLORS["warn"]))
                why = p.why()
                verdicts = [f"{k}: {t}" for k, (_i, t) in p.verdicts.items()]
                state.setToolTip(fs_i18n.join("\n", verdicts) if verdicts
                                 else why)
                self.table.setItem(r, 8, state)
                self.table.setItem(r, 9, QtWidgets.QTableWidgetItem(
                    p.window.text()))
                res = ("%.1e" % p.residual_max
                       if math.isfinite(p.residual_max) else "—")
                ri = _num_item(res, p.residual_max)
                if p.residuals:
                    ri.setToolTip(fs_i18n.join("\n", [
                        f"{k}: {v:.2e}" for k, v in p.residuals.items()]))
                self.table.setItem(r, 10, ri)
            self.table.setColumnHidden(COL_CS, not pol.has_cs(pts))
        finally:
            self._filling = False

    def _item_changed(self, item) -> None:
        if self._filling or item.column() != 0:
            return
        path = item.data(QtCore.Qt.UserRole)
        self._on[path] = item.checkState() == QtCore.Qt.Checked
        self.points = self.used()
        self._fill_ref()
        self._draw()
        has = bool(self.points)
        for b in (self.btn_csv, self.btn_plots, self.btn_report):
            b.setEnabled(has)

    def _fill_ref(self) -> None:
        pts = self.points
        if not pts:
            self.lbl_ref.setText(tr("<span style='color:#888'>No case is "
                                    "on.</span>"))
            return
        ref = pol.common_ref(pts)
        cofr = ", ".join("%g" % x for x in ref.cofr)
        text = tr("<b>Reference values</b> ({src}): Aref = {a:.6g} m², "
                  "lRef = {l:.6g} m, U∞ = {u:.6g} m/s, ρ = {rho:.5g} kg/m³, "
                  "CofR = ({c}) m", src=ref.source or "—", a=ref.a_ref,
                  l=ref.l_ref, u=ref.u_inf, rho=ref.rho, c=cofr)
        lines = [text]
        for c in pol.checks(pts):
            lines.append(f"<span style='color:{_COLORS[c.level]}'>• "
                         f"{_esc(c.text)}</span>")
        self.lbl_ref.setText(fs_i18n.join("<br>", lines))

    # --- plots -----------------------------------------------------------

    def _draw(self) -> None:
        pol.figure(self.points, fig=self.fig)
        self._blit()

    def _blit(self) -> None:
        ratio = self.plot.devicePixelRatioF() or 1.0
        w = max(self.plot.width(), 480)
        h = max(self.plot.height(), 320)
        self.fig.set_dpi(100 * ratio)
        self.fig.set_size_inches(w / 100.0, h / 100.0)
        self.canvas.draw()
        cw, ch = self.canvas.get_width_height()
        buf = np.asarray(self.canvas.buffer_rgba())
        img = QtGui.QImage(buf.tobytes(), cw, ch, QtGui.QImage.Format_RGBA8888)
        pix = QtGui.QPixmap.fromImage(img)
        pix.setDevicePixelRatio(ratio)
        self.plot.setPixmap(pix)

    def resizeEvent(self, event):
        super().resizeEvent(event)
        if hasattr(self, "canvas"):
            self._blit()

    def showEvent(self, event):
        super().showEvent(event)
        QtCore.QTimer.singleShot(0, self._blit)

    # --- export ----------------------------------------------------------

    def _stem(self) -> str:
        key = self.cmb_source.currentData()
        if key and key != SELECTED:
            return fc.safe_case_name(f"{key}_polar")
        return "polar"

    def _start_dir(self) -> str:
        first = self.points[0].path if self.points else ""
        return str(Path(first).parent) if first else str(fs_paths.data_dir())

    def save_csv(self, path: str = "") -> str:
        if not path:
            path, _ = QtWidgets.QFileDialog.getSaveFileName(
                self, tr("Save Table"),
                str(Path(self._start_dir()) / f"{self._stem()}.csv"),
                "CSV (*.csv)")
        if not path:
            return ""
        try:
            pol.write_csv(path, self.points)
        except OSError as exc:
            self._status(tr("Could not write {path}: {why}", path=path,
                            why=exc), "bad")
            return ""
        self._status(tr("Table saved: {path}", path=path))
        return path

    def save_plots(self, path: str = "") -> list:
        if not path:
            path, chosen = QtWidgets.QFileDialog.getSaveFileName(
                self, tr("Save Plots"),
                str(Path(self._start_dir()) / f"{self._stem()}.png"),
                "PNG (*.png);;SVG (*.svg)")
            if not path:
                return []
        p = Path(path)
        ext = p.suffix.lower().lstrip(".")
        if ext not in ("png", "svg"):
            ext = "png"
        try:
            out = pol.save_plots(self.points, p.parent, p.stem, (ext,))
        except OSError as exc:
            self._status(tr("Could not write {path}: {why}", path=path,
                            why=exc), "bad")
            return []
        self._status(tr("Plots saved: {n} files in {folder}", n=len(out),
                        folder=str(p.parent)))
        return out

    def report_folder(self) -> Path:
        stamp = _dt.datetime.now().strftime("%Y-%m-%d_%H%M%S")
        return reports_dir() / fc.safe_case_name(f"{self._stem()}_{stamp}")

    def make_report(self, folder: str = "", open_page: bool = True):
        """«Simple Report»: one click, no questions."""
        if not self.points:
            return None
        folder = Path(folder) if folder else self.report_folder()
        for b in (self.btn_csv, self.btn_plots, self.btn_report):
            b.setEnabled(False)
        QtWidgets.QApplication.setOverrideCursor(QtCore.Qt.WaitCursor)
        try:
            rep = rpt.build(self.points, folder, loader=self._load_off_thread,
                            progress=self._progress)
        except OSError as exc:
            self._status(tr("Could not write the report: {why}", why=exc),
                         "bad")
            return None
        finally:
            QtWidgets.QApplication.restoreOverrideCursor()
            for b in (self.btn_csv, self.btn_plots, self.btn_report):
                b.setEnabled(bool(self.points))
        self.last_report = rep
        url = QtCore.QUrl.fromLocalFile(rep.index).toString()
        dir_url = QtCore.QUrl.fromLocalFile(rep.folder).toString()
        text = tr("Report: <a href=\"{url}\">{path}</a> · <a href=\"{dir}\">"
                  "folder</a>", url=url, path=_esc(rep.index), dir=dir_url)
        if rep.notes:
            notes = fs_i18n.join(" ", rep.notes)
            text += "<br>" + (notes.map(_esc) if isinstance(
                notes, fs_i18n.TrStr) else _esc(notes))
        self._status(text, "warn" if rep.notes else "ok", rich=True)
        if open_page:
            QtGui.QDesktopServices.openUrl(QtCore.QUrl.fromLocalFile(rep.index))
        return rep

    def _progress(self, text: str) -> None:
        self._status("⏳ " + text)
        QtWidgets.QApplication.processEvents(
            QtCore.QEventLoop.ExcludeUserInputEvents)

    def _load_off_thread(self, case_dir: str):
        """Read the case in a worker thread; the window keeps drawing. The
        pictures are drawn after it, in this thread (OpenGL)."""
        import fs_viewcase as vc
        box: dict = {}

        def job():
            try:
                box["value"] = vc.load_case(case_dir)
            except Exception as exc:              # noqa: BLE001
                box["error"] = exc

        t = threading.Thread(target=job, daemon=True)
        t.start()
        while t.is_alive():
            QtWidgets.QApplication.processEvents(
                QtCore.QEventLoop.ExcludeUserInputEvents, 50)
            t.join(0.05)
        if "error" in box:
            err = box["error"]
            raise err if isinstance(err, (ValueError, KeyError, RuntimeError,
                                          MemoryError)) else ValueError(str(err))
        return box["value"]

    def _status(self, text: str, level: str = "", rich: bool = False) -> None:
        self._last_status = (text, level, rich)
        colour = _COLORS.get(level)
        body = text if rich else _esc(text)
        if colour:
            body = f"<span style='color:{colour}'>{body}</span>"
        self.lbl_status.setText(body)

    # --- language --------------------------------------------------------

    def _language_changed(self, _lang: str) -> None:
        self.retranslate_ui()

    def retranslate_ui(self) -> None:
        self.setWindowTitle(tr("Polars"))
        self._fill_sources(self.cmb_source.currentData() or SELECTED)
        self._recompute()       # the verdict texts are made in the language
        last = getattr(self, "_last_status", None)
        if last:
            text = last[0]
            if isinstance(text, fs_i18n.TrStr):
                text = text.again()
            self._status(text, last[1], last[2])


def _esc(text) -> str:
    import html
    return html.escape(str(text))
