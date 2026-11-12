"""Смуга «Проміжний результат» на вкладці «Результати».

Видно її лише тоді, коли прогін іде. Вона питає машину прогону, які
моменти вже записано (fs_live.Source.probe), забирає вибраний (fetch) і
віддає вікну готовий `fs_live.LiveResults` сигналом `show_live`. Малює
вікно — тим самим кодом, що й остаточні результати.

Усі запити до машини йдуть в окремому потоці: ssh до сервера може думати
секунди, а вікно не мусить стояти.
"""

from __future__ import annotations

import threading
import time

from PySide6 import QtCore, QtWidgets

import fs_live as fl
from fs_i18n import tr
import fs_i18n

# The Qt text setters translate and keep the key of each text, also in a
# widget that is made before the main window (T28).
fs_i18n.install()

LIVE_STYLE = "color:#d35400; font-weight:bold;"


class LiveBar(QtWidgets.QGroupBox):
    # (LiveResults, LiveTime, тека кейсу, чи просила людина сама)
    show_live = QtCore.Signal(object, object, str, bool)
    show_final = QtCore.Signal()
    _job_done = QtCore.Signal(str, object, object)     # вид, результат, виняток

    def __init__(self, parent=None):
        super().__init__(tr("Intermediate result (the run continues)"), parent)
        self._src: fl.Source | None = None
        self._cfg_fn = None
        self._probe: fl.Probe | None = None
        self._folders: dict[float, str] = {}
        self._info: dict[float, fl.LiveTime] = {}
        self._shown: float | None = None
        self._busy = False

        lay = QtWidgets.QVBoxLayout(self)
        lay.setContentsMargins(6, 4, 6, 4)
        row = QtWidgets.QHBoxLayout()
        self.cmb_times = QtWidgets.QComboBox()
        self.cmb_times.setMinimumContentsLength(28)
        self.cmb_times.setSizeAdjustPolicy(
            QtWidgets.QComboBox.AdjustToMinimumContentsLengthWithIcon)
        self.cmb_times.setToolTip(tr(
            "Times that the run has written. \"Surfaces\" are small files "
            "that the run writes at each write time (the body and the "
            "mid-plane). \"From the full time step\": the program makes "
            "the body surface from the full time step on the run machine "
            "(foamToVTK at the lowest priority)."))
        self.btn_refresh = QtWidgets.QPushButton(tr("Update the list"))
        self.btn_load = QtWidgets.QPushButton(
            tr("Load the intermediate result"))
        self.btn_load.setToolTip(tr(
            "Get the selected time and show it here. This does not stop "
            "or slow the solver: the program reads only files that are "
            "already written."))
        self.chk_auto = QtWidgets.QCheckBox(tr("update every"))
        self.chk_auto.setToolTip(tr(
            "Get and show the newest time automatically when the run writes it."))
        self.spin_min = QtWidgets.QSpinBox()
        self.spin_min.setRange(1, 120)
        self.spin_min.setValue(10)
        self.spin_min.setSuffix(tr(" min"))
        row.addWidget(QtWidgets.QLabel(tr("Time", ctx="snapshot")))
        row.addWidget(self.cmb_times, 1)
        row.addWidget(self.btn_refresh)
        row.addWidget(self.btn_load)
        row.addWidget(self.chk_auto)
        row.addWidget(self.spin_min)
        lay.addLayout(row)

        row2 = QtWidgets.QHBoxLayout()
        self.lbl_state = QtWidgets.QLabel("")
        self.lbl_state.setWordWrap(True)
        self.btn_enable = QtWidgets.QPushButton(
            tr("Turn on the surfaces for this run"))
        self.btn_enable.setToolTip(tr(
            "Add a function to system/controlDict of the run. At each "
            "write time it writes a small body surface and the "
            "mid-plane. The solver reads it at the next step."))
        self.btn_final = QtWidgets.QPushButton(tr("Final results"))
        self.btn_final.setToolTip(tr(
            "Go back to the results in the case on this computer."))
        row2.addWidget(self.lbl_state, 1)
        row2.addWidget(self.btn_enable)
        row2.addWidget(self.btn_final)
        lay.addLayout(row2)
        self.btn_enable.hide()
        self.btn_final.hide()

        self.btn_refresh.clicked.connect(lambda: self.refresh())
        self.btn_load.clicked.connect(lambda: self.load(explicit=True))
        self.btn_enable.clicked.connect(lambda: self.enable())
        self.btn_final.clicked.connect(self._final)
        self.chk_auto.toggled.connect(lambda _: self._arm())
        self.spin_min.valueChanged.connect(lambda _: self._arm())
        self._job_done.connect(self._on_job)
        self._timer = QtCore.QTimer(self)
        self._timer.timeout.connect(self._auto_tick)
        self.hide()

    # --- звідки --------------------------------------------------------

    def set_source(self, src: "fl.Source | None", cfg_fn=None) -> None:
        """Прогін почався (src) або скінчився (None)."""
        self._src = src
        self._cfg_fn = cfg_fn
        # Налаштування прогону — з маніфесту його кейсу (див. fs_live.run_cfg).
        self._run_cfg = fl.run_cfg(src.case_dir) if src is not None else None
        self._probe = None
        self._folders.clear()
        self._info.clear()
        self.cmb_times.clear()
        self.btn_enable.hide()
        if src is None:
            self._timer.stop()
            self.btn_final.hide()
            self._shown = None
            self.hide()
            return
        self.show()
        self._state(tr("Asking the run machine which times are written…"))
        # Перший запит — не одразу: на старті сценарій ще кладе кейс і
        # будує сітку, а вікно саме зайняте.
        QtCore.QTimer.singleShot(3000, lambda: self.refresh())
        self._arm()

    def source(self) -> "fl.Source | None":
        return self._src

    def showing_live(self) -> bool:
        return self._shown is not None

    def mark_final(self) -> None:
        """Вікно знову показує остаточні результати."""
        self._shown = None
        self.btn_final.hide()

    # --- дії -----------------------------------------------------------

    def refresh(self) -> None:
        src = self._src
        if src is None:
            return
        self._run("probe", src.probe)

    def load(self, explicit: bool = True, lt: "fl.LiveTime | None" = None
             ) -> None:
        src = self._src
        if src is None:
            return
        if lt is None:
            i = self.cmb_times.currentData()
            usable = self._probe.usable() if self._probe else []
            if i is None or not 0 <= int(i) < len(usable):
                self._state(tr("There is no time that can be loaded."))
                return
            lt = usable[int(i)]
        patches = self._patches()
        self._state(tr("Getting time {t}…", t=lt.name))
        self._run("fetch", lambda: (lt, src.fetch(lt, patches), explicit))

    def enable(self) -> None:
        src, cfg = self._src, self._cfg()
        if src is None or cfg is None:
            return
        answer = QtWidgets.QMessageBox.question(
            self, tr("Turn on the intermediate surfaces?"),
            tr("The program adds two functions to system/controlDict of "
               "this run (the body surface and the mid-plane, VTK "
               "format). The solver reads the dictionary again by itself "
               "and writes them from the next write time, together with "
               "the fields. This does not stop the run: each write takes "
               "a fraction of a second more, and the files are a few MB "
               "for each time.\n\nA copy of the old dictionary stays in "
               "studio/controlDict.before-live.\n\nAdd them?"))
        if answer != QtWidgets.QMessageBox.Yes:
            return
        self._state(tr("Adding the functions to controlDict…"))
        self._run("enable", lambda: src.enable(cfg))

    def _final(self) -> None:
        self.mark_final()
        self.show_final.emit()

    # --- потік ---------------------------------------------------------

    def _run(self, kind: str, job) -> None:
        if self._busy:
            return
        self._busy = True
        self._buttons()

        def work():
            try:
                res, err = job(), None
            except Exception as exc:                       # noqa: BLE001
                res, err = None, exc
            self._job_done.emit(kind, res, err)

        threading.Thread(target=work, daemon=True).start()

    @QtCore.Slot(str, object, object)
    def _on_job(self, kind: str, res, err) -> None:
        self._busy = False
        self._buttons()
        if self._src is None:
            return
        if err is not None:
            self._state(tr("Failed: {err}", err=err), bad=True)
            return
        if kind == "probe":
            self._take_probe(res)
        elif kind == "auto":
            self._on_auto(res)
        elif kind == "enable":
            msg = {"added": tr("The functions are added. The surfaces "
                               "come from the next write time."),
                   "present": tr("The functions are already in the "
                                 "dictionary of this run.")}.get(res, tr(
                       "Cannot add them: system/controlDict has no "
                       "functions block."))
            self._state(msg, bad=res not in ("added", "present"))
            self.refresh()
        elif kind == "fetch":
            lt, (folder, stats), explicit = res
            self._folders[lt.time] = folder
            self._info[lt.time] = lt
            self._shown = lt.time
            self.btn_final.show()
            mb = stats.get("bytes", 0) / 1e6
            self._state(tr("This is an INTERMEDIATE result: {what}. Got "
                           "{mb:.1f} MB in {s:.1f} s.",
                           what=fl.label(lt, self._steady()), mb=mb,
                           s=stats.get("seconds", 0.0)), live=True)
            cfg = self._run_cfg or fl.run_cfg(
                "", self._win_cfg(),
                fl.LiveResults({lt.time: folder}).patch_names(lt.time))
            self.show_live.emit(
                fl.LiveResults(self._folders, self._info, cfg=cfg),
                lt, self._src.case_dir, bool(explicit))

    def _take_probe(self, pr: fl.Probe) -> None:
        self._probe = pr
        keep = self.cmb_times.currentText()
        self.cmb_times.blockSignals(True)
        self.cmb_times.clear()
        usable = pr.usable() if pr.ok else []
        steady = self._steady()
        for i, lt in enumerate(usable):
            self.cmb_times.addItem(fl.label(lt, steady), i)
        if usable:
            j = self.cmb_times.findText(keep)
            self.cmb_times.setCurrentIndex(j if j >= 0 else len(usable) - 1)
        self.cmb_times.blockSignals(False)
        can_enable = pr.ok and not pr.has_fo and self._cfg() is not None
        self.btn_enable.setVisible(can_enable)
        self._buttons()
        if self._shown is not None:
            return
        if not pr.ok:
            self._state(tr("Cannot ask the run machine: {why}",
                           why=pr.why), bad=True)
        elif any(t.surface for t in usable):
            self._state(tr("Times written: {n}. Select one and click \"Load\".", n=len(usable)))
        elif pr.has_fo:
            self._state(tr("The surfaces come after the next write of the run."))
        elif usable:
            self._state(tr("This run started without the intermediate "
                           "surfaces. You can make the body surface from "
                           "a full time step (slower), or turn on the "
                           "surfaces for the next write times."))
        elif pr.distributed:
            self._state(tr("The run is distributed over machines and "
                           "started without the intermediate surfaces: "
                           "the full time steps are on the other "
                           "machines. Turn on the surfaces: they come "
                           "from the next write time."))
        else:
            self._state(tr("The run has not written a time yet."))

    # --- автооновлення -------------------------------------------------

    def _arm(self) -> None:
        if self._src is not None and self.chk_auto.isChecked():
            self._timer.start(self.spin_min.value() * 60_000)
        else:
            self._timer.stop()

    def _auto_tick(self) -> None:
        """Раз на N хв: спитати, і якщо є новіший момент — показати його."""
        src = self._src
        if src is None or self._busy:
            return

        def job():
            pr = src.probe()
            best = None
            for lt in pr.usable() if pr.ok else []:
                if lt.surface and (best is None or lt.time > best.time):
                    best = lt
            got = None
            if best is not None and best.time not in self._folders:
                got = (best, src.fetch(best, self._patches()), False)
            return pr, got

        self._run("auto", job)

    # Відповідь автооновлення — дві відповіді разом.
    def _on_auto(self, res) -> None:
        pr, got = res
        self._take_probe(pr)
        if got is not None:
            self._on_job("fetch", got, None)

    # --- дрібниці ------------------------------------------------------

    def _cfg(self):
        """Налаштування прогону, а якщо маніфесту немає — вікна."""
        return self._run_cfg if self._run_cfg is not None else self._win_cfg()

    def _win_cfg(self):
        try:
            return self._cfg_fn() if self._cfg_fn else None
        except Exception:                                   # noqa: BLE001
            return None

    def _patches(self) -> "list[str]":
        cfg = self._cfg()
        return [b.name for b in cfg.body_patches] if cfg is not None else []

    def _steady(self) -> bool:
        cfg = self._cfg()
        return bool(cfg.solver.steady) if cfg is not None else False

    def _buttons(self) -> None:
        idle = not self._busy
        has = self.cmb_times.count() > 0
        self.btn_refresh.setEnabled(idle)
        self.btn_load.setEnabled(idle and has)
        self.btn_enable.setEnabled(idle)

    def _state(self, text: str, bad: bool = False, live: bool = False) -> None:
        stamp = time.strftime("%H:%M")
        self.lbl_state.setText(stamp + "  " + text)    # "+" keeps the key
        self.lbl_state.setStyleSheet(
            "color:#c0392b;" if bad else LIVE_STYLE if live else "")


def banner(lt: "fl.LiveTime", steady: bool) -> str:
    """Перший рядок під екраном, поки показано проміжний результат."""
    return tr("⚠ INTERMEDIATE RESULT, not the final one: {what}. The run "
              "continues, and the data will change.", what=fl.label(lt, steady))
