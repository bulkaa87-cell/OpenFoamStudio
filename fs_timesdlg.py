"""Dialog «Times on the Server» (T40): select times, see the sizes, download.

The listing comes from fs_servertimes.list_times. The person selects the
times and what to take (fields for viewing, all fields, surfaces only); the
dialog shows the download size and how many times the server must reconstruct
first. Nothing runs before the click on «Download».
"""

from __future__ import annotations

import shutil
import threading

from PySide6 import QtCore, QtWidgets

import fs_i18n
import fs_servertimes as fst
import fs_timeline as ftl
from fs_i18n import tr

fs_i18n.install()


def human_bytes(n: float) -> str:
    """Bytes as text with a unit in the interface language (ГБ, МБ, кБ)."""
    n = float(n)
    if n >= 1e9:
        return tr("{v:.1f} GB", v=n / 1e9)
    if n >= 1e6:
        return tr("{v:.0f} MB", v=n / 1e6)
    return tr("{v:.0f} kB", v=n / 1e3)


class _Job(QtCore.QObject):
    progress = QtCore.Signal(str, float)
    done = QtCore.Signal(object, object)            # result, exception

    def __init__(self, job):
        super().__init__()
        self.job = job

    @QtCore.Slot()
    def run(self):
        try:
            got = self.job(lambda text, frac: self.progress.emit(str(text),
                                                                 float(frac)))
        except Exception as exc:                          # noqa: BLE001
            self.done.emit(None, exc)
            return
        self.done.emit(got, None)


class TimesDialog(QtWidgets.QDialog):
    COL_TIME, COL_SERVER, COL_HERE, COL_SIZE = range(4)

    def __init__(self, parent, backend, case_dir: str, remote: str,
                 listing: "fst.Listing"):
        super().__init__(parent)
        self.backend, self.case_dir, self.remote = backend, case_dir, remote
        self.listing = listing
        self.result_info: dict | None = None
        self._cancel = threading.Event()
        self._thread = None
        self.setWindowTitle("Times on the Server")
        self.resize(720, 560)
        lay = QtWidgets.QVBoxLayout(self)
        self.lbl_head = QtWidgets.QLabel()
        self.lbl_head.setWordWrap(True)
        lay.addWidget(self.lbl_head)

        box = QtWidgets.QGroupBox("What to download")
        g = QtWidgets.QVBoxLayout(box)
        self.rb = {}
        for mode, text, tip in (
                ("view", "Fields for viewing (U, p, T, nut, y+, τ)",
                 "The fields that the «Results» tab shows. About a quarter "
                 "of a time folder."),
                ("all", "All fields",
                 "Each file of each time folder, as on the server (also "
                 "for ParaView or a restart)."),
                ("surf", "Surfaces only (light)",
                 "The body and the mid plane that the run writes at each "
                 "time (postProcessing/studioLive*). A few MB per time; no "
                 "reconstructPar on the server.")):
            r = QtWidgets.QRadioButton(text)
            r.setToolTip(tip)
            r.toggled.connect(lambda on: on and self._refresh())
            g.addWidget(r)
            self.rb[mode] = r
        lay.addWidget(box)

        self.table = QtWidgets.QTableWidget(0, 4)
        self.table.setHorizontalHeaderLabels(
            ["Time, s", "On the server", "Here", "Size"])
        self.table.verticalHeader().setVisible(False)
        self.table.setEditTriggers(QtWidgets.QAbstractItemView.NoEditTriggers)
        self.table.setSelectionMode(QtWidgets.QAbstractItemView.NoSelection)
        self.table.itemChanged.connect(lambda _: self._total())
        lay.addWidget(self.table, 1)

        row = QtWidgets.QHBoxLayout()
        for text, how in (("Everything Not Here", "missing"),
                          ("All", "all"), ("None", "none"),
                          ("Every Second", "every2")):
            b = QtWidgets.QPushButton(text)
            b.clicked.connect(lambda _=False, h=how: self._select(h))
            row.addWidget(b)
        row.addStretch(1)
        lay.addLayout(row)
        # «From … to …»: a range of times of the server.
        row = QtWidgets.QHBoxLayout()
        row.addWidget(QtWidgets.QLabel("From"))
        self.cmb_from = QtWidgets.QComboBox()
        row.addWidget(self.cmb_from)
        row.addWidget(QtWidgets.QLabel("to"))
        self.cmb_to = QtWidgets.QComboBox()
        row.addWidget(self.cmb_to)
        for t in listing.times:
            self.cmb_from.addItem(t.name, t.time)
            self.cmb_to.addItem(t.name, t.time)
        if listing.times:
            self.cmb_to.setCurrentIndex(len(listing.times) - 1)
        self.btn_range = QtWidgets.QPushButton("Select the Range")
        self.btn_range.setToolTip("Select each time from … to … (both "
                                  "included) that this mode can give.")
        self.btn_range.clicked.connect(lambda: self.select_range(
            self.cmb_from.currentData(), self.cmb_to.currentData()))
        row.addWidget(self.btn_range)
        row.addStretch(1)
        lay.addLayout(row)

        self.lbl_total = QtWidgets.QLabel()
        self.lbl_total.setWordWrap(True)
        lay.addWidget(self.lbl_total)
        self.bar = QtWidgets.QProgressBar()
        self.bar.setRange(0, 1000)
        self.bar.hide()
        lay.addWidget(self.bar)
        self.lbl_step = QtWidgets.QLabel()
        self.lbl_step.setStyleSheet("color:#9aa7b4;")
        lay.addWidget(self.lbl_step)

        self.buttons = QtWidgets.QDialogButtonBox()
        self.btn_go = self.buttons.addButton(
            "Download", QtWidgets.QDialogButtonBox.AcceptRole)
        self.btn_cancel = self.buttons.addButton(
            "Cancel", QtWidgets.QDialogButtonBox.RejectRole)
        self.btn_go.clicked.connect(self._start)
        self.btn_cancel.clicked.connect(self._cancel_clicked)
        lay.addWidget(self.buttons)

        here = {f"{e.time:.12g}": e for e in ftl.case_times(case_dir)}
        self._here = here
        self.rb["surf" if self._only_surf() else "view"].setChecked(True)
        self._fill()
        self._select("missing")

    # --- table ----------------------------------------------------------------

    def _only_surf(self) -> bool:
        L = self.listing
        return not any(L.usable(t, "view") and not t.reconstructed
                       for t in L.times)

    def mode(self) -> str:
        return next((m for m, r in self.rb.items() if r.isChecked()), "view")

    def _fill(self) -> None:
        L = self.listing
        self.table.blockSignals(True)
        self.table.setRowCount(len(L.times))
        for r, t in enumerate(L.times):
            it = QtWidgets.QTableWidgetItem(t.name)
            it.setFlags(it.flags() | QtCore.Qt.ItemIsUserCheckable)
            it.setCheckState(QtCore.Qt.Unchecked)
            it.setData(QtCore.Qt.UserRole, t.name)
            self.table.setItem(r, self.COL_TIME, it)
            if t.reconstructed:
                where = tr("reconstructed")
            elif L.can_reconstruct(t):
                where = tr("processor*/ only — the server reconstructs it")
            elif t.proc:
                where = tr("processor*/ incomplete ({k} of {n})",
                           k=t.parts, n=L.nproc)
            else:
                where = tr("surfaces only")
            if t.surf and (t.reconstructed or t.proc):
                where = where + tr(", + surfaces")
            self.table.setItem(r, self.COL_SERVER,
                               QtWidgets.QTableWidgetItem(where))
            for c in (self.COL_HERE, self.COL_SIZE):
                self.table.setItem(r, c, QtWidgets.QTableWidgetItem(""))
        self.table.blockSignals(False)
        self.table.resizeColumnsToContents()
        self.table.horizontalHeader().setStretchLastSection(True)
        L = self.listing
        # The display name of the backend («Server server1»), as in the
        # list of machines; the host name only if it has no label.
        server = (getattr(self.backend, "label", "")
                  or getattr(self.backend, "host", ""))
        if L.cells:
            head = tr("{server}, run folder {remote}: {n} times, {cells:,} "
                      "cells, {nproc} processor* folders.", server=server,
                      remote=self.remote, n=len(L.times), cells=L.cells,
                      nproc=L.nproc)
        else:
            head = tr("{server}, run folder {remote}: {n} times, {nproc} "
                      "processor* folders.", server=server,
                      remote=self.remote, n=len(L.times), nproc=L.nproc)
        self.lbl_head.setText(
            head + (tr(" The run is still going: its newest time is not "
                       "offered.") if L.running else ""))

    def _refresh(self) -> None:
        if not hasattr(self, "table"):
            return
        mode, L = self.mode(), self.listing
        if self.table.rowCount() != len(L.times):
            return                      # the table is not filled yet
        self.table.blockSignals(True)
        for r, t in enumerate(L.times):
            ok = L.usable(t, mode)
            it = self.table.item(r, self.COL_TIME)
            flags = it.flags()
            if ok:
                it.setFlags(flags | QtCore.Qt.ItemIsEnabled)
            else:
                it.setFlags(flags & ~QtCore.Qt.ItemIsEnabled)
                it.setCheckState(QtCore.Qt.Unchecked)
            h = self._here.get(f"{t.time:.12g}")
            have = bool(h and (h.surf if mode == "surf" else h.full))
            self.table.item(r, self.COL_HERE).setText(tr("yes") if have else "")
            self.table.item(r, self.COL_SIZE).setText(
                human_bytes(L.size(t, mode)) if ok else "—")
        self.table.blockSignals(False)
        self._total()

    def _select(self, how: str) -> None:
        mode, L = self.mode(), self.listing
        self.table.blockSignals(True)
        k = 0
        for r, t in enumerate(L.times):
            it = self.table.item(r, self.COL_TIME)
            if not L.usable(t, mode):
                continue
            h = self._here.get(f"{t.time:.12g}")
            have = bool(h and (h.surf if mode == "surf" else h.full))
            on = {"all": True, "none": False, "every2": k % 2 == 0,
                  "missing": not have}[how]
            k += 1
            it.setCheckState(QtCore.Qt.Checked if on else QtCore.Qt.Unchecked)
        self.table.blockSignals(False)
        self._refresh()

    def select_range(self, a, b) -> None:
        """Select exactly the times from `a` to `b` (the others off)."""
        if a is None or b is None:
            return
        want = set(fst.range_names(self.listing, self.mode(), a, b))
        self.table.blockSignals(True)
        for r in range(self.table.rowCount()):
            it = self.table.item(r, self.COL_TIME)
            on = it.data(QtCore.Qt.UserRole) in want
            it.setCheckState(QtCore.Qt.Checked if on else QtCore.Qt.Unchecked)
        self.table.blockSignals(False)
        self._refresh()

    def chosen(self) -> list[str]:
        out = []
        for r in range(self.table.rowCount()):
            it = self.table.item(r, self.COL_TIME)
            if it.checkState() == QtCore.Qt.Checked:
                out.append(it.data(QtCore.Qt.UserRole))
        return out

    def _total(self) -> None:
        mode, L = self.mode(), self.listing
        by = L.by_name()
        names = self.chosen()
        size = sum(L.size(by[n], mode) for n in names)
        rec = [n for n in names if mode != "surf"
               and L.needs_reconstruct(by[n])]
        rec_bytes = sum(L.full_bytes(by[n]) for n in rec)
        try:
            free = shutil.disk_usage(self.case_dir).free
        except OSError:
            free = 0
        text = tr("Selected: {n} times, download about {size}.",
                  n=len(names), size=human_bytes(size))
        if rec:
            text = text + " " + tr(
                "First the server reconstructs {k} times (reconstructPar, "
                "nice -n 19): +{disk} on the server disk.",
                k=len(rec), disk=human_bytes(rec_bytes))
        if free:
            text = text + " " + tr("Free here: {free}.", free=human_bytes(free))
        why = fst.mesh_problem(L, self.case_dir, mode)
        if why:
            text = text + "\n⚠ " + why
        self.lbl_total.setText(text)
        self.btn_go.setEnabled(bool(names) and not why
                               and (not free or size < free * 0.95)
                               and self._thread is None)

    # --- work -------------------------------------------------------------------

    def _start(self) -> None:
        names, mode = self.chosen(), self.mode()
        if not names:
            return
        self.btn_go.setEnabled(False)
        for w in list(self.rb.values()) + [self.table]:
            w.setEnabled(False)
        self.bar.show()
        self._cancel.clear()
        backend, case, remote, L = (self.backend, self.case_dir, self.remote,
                                    self.listing)
        job = _Job(lambda say: fst.get_times(
            backend, case, remote, L, names, mode, say=say,
            cancelled=self._cancel.is_set))
        th = QtCore.QThread(self)
        th.setObjectName("times-download")
        job.moveToThread(th)
        th.started.connect(job.run)
        job.progress.connect(self._progress)
        job.done.connect(self._done)
        self._thread, self._job = th, job
        th.start()

    def _progress(self, text: str, frac: float) -> None:
        self.bar.setValue(int(1000 * max(0.0, min(frac, 1.0))))
        self.lbl_step.setText(text)

    def _done(self, got, exc) -> None:
        th, self._thread = self._thread, None
        if th is not None:
            th.quit()
            th.wait()
        if isinstance(exc, fst.Cancelled):
            self.lbl_step.setText(tr("Stopped."))
            self.reject()
            return
        if exc is not None:
            self.bar.hide()
            for w in list(self.rb.values()) + [self.table]:
                w.setEnabled(True)
            self._total()
            QtWidgets.QMessageBox.critical(self, tr("Times not downloaded"),
                                           str(exc)[:600])
            return
        self.result_info = dict(got or {}, mode=self.mode())
        self.accept()

    def _cancel_clicked(self) -> None:
        if self._thread is None:
            self.reject()
            return
        # Between two steps: a reconstructPar group or the download in
        # progress finishes first.
        self._cancel.set()
        self.lbl_step.setText(tr("Stopping after the current step…"))
        self.btn_cancel.setEnabled(False)

    def closeEvent(self, ev) -> None:                      # noqa: N802
        if self._thread is not None:
            self._cancel_clicked()
            ev.ignore()
            return
        super().closeEvent(ev)

    def reject(self) -> None:
        if self._thread is not None:
            self._cancel_clicked()
            return
        super().reject()


class FetchBar(QtWidgets.QFrame):
    """The small step after a download from the server (T40).

    The normal download takes only the last time. When the server has more
    times, this bar asks in one line: «only the last time» (the default:
    nothing more to download) or «from … to …» (two lists of the server
    times, with the size), and what to take (fields for viewing, all fields,
    surfaces only). «All Times…» opens the dialog with the whole list.
    """

    done = QtCore.Signal(object)           # the result of get_times (dict)
    more_requested = QtCore.Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setFrameShape(QtWidgets.QFrame.StyledPanel)
        self.backend = None
        self.case_dir = ""
        self.remote = ""
        self.listing: "fst.Listing | None" = None
        self._thread = None
        self._cancel = threading.Event()
        # Two short lines, not one long: one long line made the «Results»
        # side wider and the panel on the left narrower.
        outer = QtWidgets.QVBoxLayout(self)
        outer.setContentsMargins(6, 3, 6, 3)
        outer.setSpacing(2)
        row = QtWidgets.QHBoxLayout()
        outer.addLayout(row)
        self.lbl = QtWidgets.QLabel()
        row.addWidget(self.lbl)
        self.rb_last = QtWidgets.QRadioButton("Only the last time")
        self.rb_last.setToolTip("The last time is here already: nothing more "
                                "to download.")
        self.rb_range = QtWidgets.QRadioButton("From")
        self.rb_last.setChecked(True)
        row.addWidget(self.rb_last)
        row.addWidget(self.rb_range)
        self.cmb_from = QtWidgets.QComboBox()
        self.cmb_to = QtWidgets.QComboBox()
        row.addWidget(self.cmb_from)
        row.addWidget(QtWidgets.QLabel("to"))
        row.addWidget(self.cmb_to)
        self.cmb_mode = QtWidgets.QComboBox()
        for mode, text in (("view", "fields for viewing"),
                           ("all", "all fields"),
                           ("surf", "surfaces only")):
            self.cmb_mode.addItem(tr(text), mode)
        self.cmb_mode.setToolTip(
            "Fields for viewing: the fields of the «Results» tab (about a "
            "quarter of a time folder). All fields: as on the server. "
            "Surfaces only: the body and the mid plane that the run writes, "
            "a few MB per time.")
        row.addStretch(1)
        row = QtWidgets.QHBoxLayout()
        outer.addLayout(row)
        row.addWidget(self.cmb_mode)
        self.lbl_size = QtWidgets.QLabel()
        self.lbl_size.setStyleSheet("color:#9aa7b4;")
        row.addWidget(self.lbl_size)
        self.btn_go = QtWidgets.QPushButton("Download")
        self.btn_go.clicked.connect(self._go)
        row.addWidget(self.btn_go)
        self.progress = QtWidgets.QProgressBar()
        self.progress.setRange(0, 1000)
        self.progress.setMaximumWidth(160)
        self.progress.hide()
        row.addWidget(self.progress)
        row.addStretch(1)
        self.btn_more = QtWidgets.QPushButton("All Times…")
        self.btn_more.setToolTip("The whole list of the server times, with "
                                 "check boxes.")
        self.btn_more.clicked.connect(self.more_requested.emit)
        row.addWidget(self.btn_more)
        self.btn_close = QtWidgets.QToolButton()
        self.btn_close.setText("✕")
        self.btn_close.setAutoRaise(True)
        self.btn_close.setToolTip("Close (only the last time stays)")
        self.btn_close.clicked.connect(self._close)
        row.addWidget(self.btn_close)
        for w in (self.cmb_from, self.cmb_to, self.cmb_mode):
            w.currentIndexChanged.connect(lambda _: self._sync())
        self.cmb_from.currentIndexChanged.connect(
            lambda _: self.rb_range.setChecked(True))
        self.cmb_to.currentIndexChanged.connect(
            lambda _: self.rb_range.setChecked(True))
        self.rb_last.toggled.connect(lambda _: self._sync())
        self.hide()

    # --- state ------------------------------------------------------------------

    def offer(self, backend, case_dir: str, remote: str,
              listing: "fst.Listing") -> bool:
        """Show the bar if the server has times that are not here.
        → True if it is shown."""
        self.backend, self.case_dir, self.remote = backend, case_dir, remote
        self.listing = listing
        more = [t for t in listing.times
                if listing.usable(t, "view") or listing.usable(t, "surf")]
        have = fst.local_have(case_dir, "view")
        extra = [t for t in more if fst._key(t.time) not in have]
        if not extra:
            self.hide()
            return False
        for c in (self.cmb_from, self.cmb_to):
            c.blockSignals(True)
            c.clear()
            for t in more:
                c.addItem(t.name, t.time)
            c.blockSignals(False)
        self.cmb_from.setCurrentIndex(0)
        self.cmb_to.setCurrentIndex(max(len(more) - 1, 0))
        self.rb_last.setChecked(True)
        self._n_extra = len(extra)
        self._label()
        self._sync()
        self.show()
        return True

    def _label(self) -> None:
        self.lbl.setText(fs_i18n.plural(
            getattr(self, "_n_extra", 0),
            "The server has {n} more time:",
            "The server has {n} more times:"))

    def retranslate_ui(self) -> None:
        self._label()
        self._sync()

    def mode(self) -> str:
        return self.cmb_mode.currentData() or "view"

    def chosen(self) -> list[str]:
        """Times to download: none for «only the last time»."""
        if self.listing is None or self.rb_last.isChecked():
            return []
        names = fst.range_names(self.listing, self.mode(),
                                self.cmb_from.currentData() or 0.0,
                                self.cmb_to.currentData() or 0.0)
        have = fst.local_have(self.case_dir, self.mode())
        return [n for n in names
                if fst._key(self.listing.by_name()[n].time) not in have]

    def _sync(self) -> None:
        if self.listing is None:
            return
        names = self.chosen()
        if self.rb_last.isChecked():
            self.lbl_size.setText("")
        else:
            self.lbl_size.setText(tr(
                "{n} times, about {size}", n=len(names),
                size=human_bytes(fst.size_of(self.listing, names,
                                             self.mode()))))
        why = fst.mesh_problem(self.listing, self.case_dir, self.mode())
        self.btn_go.setToolTip(why)
        self.btn_go.setEnabled(bool(names) and not why
                               and self._thread is None)

    # --- work -------------------------------------------------------------------

    def _go(self) -> None:
        names, mode = self.chosen(), self.mode()
        if not names or self._thread is not None:
            return
        self.btn_go.setEnabled(False)
        self.progress.setValue(0)
        self.progress.show()
        self._cancel.clear()
        backend, case, remote, L = (self.backend, self.case_dir, self.remote,
                                    self.listing)
        job = _Job(lambda say: fst.get_times(
            backend, case, remote, L, names, mode, say=say,
            cancelled=self._cancel.is_set))
        th = QtCore.QThread(self)
        th.setObjectName("times-download")
        job.moveToThread(th)
        th.started.connect(job.run)
        job.progress.connect(self._progress)
        job.done.connect(self._done)
        self._thread, self._job = th, job
        th.start()

    def _progress(self, text: str, frac: float) -> None:
        self.progress.setValue(int(1000 * max(0.0, min(frac, 1.0))))
        self.progress.setToolTip(text)
        self.lbl_size.setText(text)

    def _done(self, got, exc) -> None:
        th, self._thread = self._thread, None
        if th is not None:
            th.quit()
            th.wait()
        self.progress.hide()
        if exc is not None and not isinstance(exc, fst.Cancelled):
            self.lbl_size.setText(tr("not downloaded: {exc}",
                                     exc=str(exc)[:200]))
            self.btn_go.setEnabled(True)
            return
        if exc is None:
            self.hide()
            self.done.emit(dict(got or {}, mode=self.mode()))

    def _close(self) -> None:
        if self._thread is not None:
            self._cancel.set()
            return
        self.hide()

    def stop(self, wait_ms: int = 20000) -> None:
        """At the end of the window: stop after the current step and wait."""
        if self._thread is not None:
            self._cancel.set()
            self._thread.quit()
            self._thread.wait(wait_ms)
