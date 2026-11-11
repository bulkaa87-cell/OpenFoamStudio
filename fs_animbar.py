"""Смуга «Анімація» на вкладці результатів.

Дві половини:
  * ПІДГОТОВКА — проміжок часу, крок, що анімувати (галочки, Q33) і які
    поля рахувати (галочки, Q34), «рухомий зріз» (Q35b);
  * ПРОГРАВАННЯ — ▶/⏸, повзунок кадрів, швидкість, «по колу», вибір поля
    з уже порахованих і «зберегти GIF».

Логіки тут немає: смуга лише збирає вибір і випромінює сигнали. Рахує
fs_anim, програє вікно.
"""

from __future__ import annotations

from PySide6 import QtCore, QtWidgets

import fs_anim as fa
import fs_views as fv
from fs_i18n import tr


class AnimBar(QtWidgets.QGroupBox):
    prepare_requested = QtCore.Signal(dict)
    reconstruct_requested = QtCore.Signal(list)
    play_toggled = QtCore.Signal(bool)
    frame_changed = QtCore.Signal(int)
    fields_changed = QtCore.Signal(str, str)        # тіло, зріз
    export_requested = QtCore.Signal()
    stop_requested = QtCore.Signal()

    def __init__(self, parent=None):
        super().__init__("Анімація (нестаціонарний розвʼязок)", parent)
        self.setCheckable(True)
        self.setChecked(False)
        self._times: list[str] = []
        self._missing: list[str] = []
        outer = QtWidgets.QVBoxLayout(self)
        outer.setContentsMargins(6, 4, 6, 4)
        self.body_box = QtWidgets.QWidget()
        outer.addWidget(self.body_box)
        top = QtWidgets.QVBoxLayout(self.body_box)
        top.setContentsMargins(0, 0, 0, 0)
        # Налаштування — окремим блоком: коли кадри готові, він ховається,
        # і вʼюпорт результатів дістає місце назад (на першому знімку смуга
        # з'їдала п'ять рядків, а GIF виходив 1119×335).
        self.setup_box = QtWidgets.QWidget()
        top.addWidget(self.setup_box)
        v = QtWidgets.QVBoxLayout(self.setup_box)
        v.setContentsMargins(0, 0, 0, 0)

        # --- що й коли ------------------------------------------------------
        row = QtWidgets.QHBoxLayout()
        self.cmb_from = QtWidgets.QComboBox()
        self.cmb_to = QtWidgets.QComboBox()
        self.spin_every = QtWidgets.QSpinBox()
        self.spin_every.setRange(1, 1000)
        self.spin_every.setPrefix("кожен ")
        self.spin_every.setSuffix("-й")
        for w in (self.cmb_from, self.cmb_to):
            w.currentIndexChanged.connect(lambda _: self._sync_count())
        self.spin_every.valueChanged.connect(lambda _: self._sync_count())
        row.addWidget(QtWidgets.QLabel("Від"))
        row.addWidget(self.cmb_from)
        row.addWidget(QtWidgets.QLabel("до"))
        row.addWidget(self.cmb_to)
        row.addWidget(self.spin_every)
        self.lbl_count = QtWidgets.QLabel("")
        self.lbl_count.setStyleSheet("color:#9aa7b4;")
        row.addWidget(self.lbl_count, 1)
        v.addLayout(row)

        row = QtWidgets.QHBoxLayout()
        self.chk_body = QtWidgets.QCheckBox("тіло")
        self.chk_body.setChecked(True)
        self.chk_slice = QtWidgets.QCheckBox("зріз")
        self.chk_slice.setChecked(True)
        self.chk_streams = QtWidgets.QCheckBox("лінії потоку (довго)")
        self.chk_streams.setToolTip(
            "Лінії трасуються наново для кожного кадру — це найдорожча "
            "частина: секунди на кадр на великій сітці.")
        self.chk_moving = QtWidgets.QCheckBox("рухомий зріз (більше памʼяті)")
        self.chk_moving.setToolTip(
            "Типово зріз стоїть там, де був на момент підготовки. З цією "
            "галочкою в памʼяті лежать поля всього обʼєму для кожного кадру, "
            "і площину можна рухати під час програвання.")
        for w in (self.chk_body, self.chk_slice, self.chk_streams,
                  self.chk_moving):
            row.addWidget(w)
        row.addStretch(1)
        v.addLayout(row)

        # Поля — галочками: рахується лише вибране.
        row = QtWidgets.QHBoxLayout()
        row.addWidget(QtWidgets.QLabel("На тілі:"))
        self.chk_bfield = {}
        for key, title in fa.BODY_TITLES.items():
            c = QtWidgets.QCheckBox(tr(fa.BODY_FIELDS[key][0]).split(",")[0])
            c.setToolTip(tr(title))
            c.setChecked(key == "cp")
            self.chk_bfield[key] = c
            row.addWidget(c)
        row.addStretch(1)
        v.addLayout(row)
        row = QtWidgets.QHBoxLayout()
        row.addWidget(QtWidgets.QLabel("На зрізі:"))
        self.chk_sfield = {}
        for key in fa.SLICE_KEYS:
            spec = fv.spec_by_key(key)
            short = tr(spec.title).split("  ")[0]
            c = QtWidgets.QCheckBox(short)
            c.setToolTip(tr(spec.title) + "\n" + tr(spec.about))
            c.setChecked(key == "dU")
            self.chk_sfield[key] = c
            row.addWidget(c)
        row.addStretch(1)
        v.addLayout(row)

        row = QtWidgets.QHBoxLayout()
        self.btn_reconstruct = QtWidgets.QPushButton("")
        self.btn_reconstruct.setVisible(False)
        self.btn_reconstruct.clicked.connect(
            lambda: self.reconstruct_requested.emit(list(self._missing)))
        row.addWidget(self.btn_reconstruct)
        self.btn_prepare = QtWidgets.QPushButton("Підготувати кадри")
        self.btn_prepare.clicked.connect(self._prepare)
        row.addWidget(self.btn_prepare)
        self.lbl_info = QtWidgets.QLabel("")
        self.lbl_info.setWordWrap(True)
        self.lbl_info.setStyleSheet("color:#9aa7b4;")
        row.addWidget(self.lbl_info, 1)
        v.addLayout(row)

        # --- програвання ----------------------------------------------------
        self.play_box = QtWidgets.QWidget()
        p = QtWidgets.QHBoxLayout(self.play_box)
        p.setContentsMargins(0, 0, 0, 0)
        self.btn_play = QtWidgets.QPushButton("▶")
        self.btn_play.setCheckable(True)
        self.btn_play.setFixedWidth(40)
        self.btn_play.toggled.connect(self._play)
        self.slider = QtWidgets.QSlider(QtCore.Qt.Horizontal)
        self.slider.setMinimum(0)
        self.slider.valueChanged.connect(self.frame_changed.emit)
        self.lbl_time = QtWidgets.QLabel("")
        # Ширина під найдовший підпис: інакше «1/24» обрізалось до «1/2».
        self.lbl_time.setMinimumWidth(self.lbl_time.fontMetrics().horizontalAdvance(
            "t = 0.0000000 с   9999/9999") + 6)
        self.spin_fps = QtWidgets.QSpinBox()
        self.spin_fps.setRange(1, 60)
        self.spin_fps.setValue(10)
        self.spin_fps.setSuffix(" кадр/с")
        self.chk_loop = QtWidgets.QCheckBox("по колу")
        self.chk_loop.setChecked(True)
        self.slider.setMinimumWidth(160)
        self.cmb_body = QtWidgets.QComboBox()
        self.cmb_slice = QtWidgets.QComboBox()
        for c in (self.cmb_body, self.cmb_slice):
            # Вузько: довга назва поля інакше забирала місце в повзунка, і
            # той стискався до точки (видно на першому знімку).
            c.setSizeAdjustPolicy(
                QtWidgets.QComboBox.AdjustToMinimumContentsLengthWithIcon)
            c.setMinimumContentsLength(10)
            c.setMinimumWidth(110)
            c.currentIndexChanged.connect(
                lambda _: self.fields_changed.emit(
                    self.cmb_body.currentData() or "",
                    self.cmb_slice.currentData() or ""))
        self.btn_export = QtWidgets.QPushButton("Зберегти GIF…")
        self.btn_export.clicked.connect(self.export_requested.emit)
        self.btn_stop = QtWidgets.QPushButton("Закрити анімацію")
        self.btn_stop.clicked.connect(self.stop_requested.emit)
        self.btn_setup = QtWidgets.QPushButton("⚙")
        self.btn_setup.setCheckable(True)
        self.btn_setup.setFixedWidth(32)
        self.btn_setup.setToolTip("Показати налаштування: проміжок, галочки, "
                                  "поля — щоб підготувати кадри наново.")
        self.btn_setup.toggled.connect(self.setup_box.setVisible)
        for w in (self.btn_play, self.slider, self.lbl_time, self.spin_fps,
                  self.chk_loop, QtWidgets.QLabel("тіло"), self.cmb_body,
                  QtWidgets.QLabel("зріз"), self.cmb_slice, self.btn_export,
                  self.btn_stop, self.btn_setup):
            p.addWidget(w, 1 if w is self.slider else 0)
        self.play_box.setVisible(False)
        top.addWidget(self.play_box)

        self.toggled.connect(self.body_box.setVisible)
        self.body_box.setVisible(False)

    # --- заповнення ---------------------------------------------------------

    def set_times(self, times: list[str], missing: list[str] | None = None):
        """Моменти, з яких можна анімувати. `missing` — є лише в processor*."""
        self._times = list(times)
        self._missing = list(missing or [])
        for c in (self.cmb_from, self.cmb_to):
            c.blockSignals(True)
            c.clear()
            c.addItems(self._times)
            c.blockSignals(False)
        if self._times:
            self.cmb_to.setCurrentIndex(len(self._times) - 1)
        self.btn_reconstruct.setVisible(bool(self._missing))
        self.btn_reconstruct.setText(
            tr("Зібрати ще {n_missing} моментів (reconstructPar)", n_missing=len(self._missing)))
        self.setEnabled(len(self._times) >= 2 or bool(self._missing))
        self._sync_count()

    def chosen_times(self) -> list[str]:
        a, b = self.cmb_from.currentIndex(), self.cmb_to.currentIndex()
        if a < 0 or b < 0:
            return []
        if a > b:
            a, b = b, a
        return self._times[a:b + 1:self.spin_every.value()]

    def _sync_count(self):
        n = len(self.chosen_times())
        self.lbl_count.setText(tr("{n} кадрів", n=n) if n else "")

    def _prepare(self):
        self.prepare_requested.emit({
            "times": self.chosen_times(),
            "body": self.chk_body.isChecked(),
            "slice": self.chk_slice.isChecked(),
            "streams": self.chk_streams.isChecked(),
            "moving": self.chk_moving.isChecked(),
            "body_fields": [k for k, c in self.chk_bfield.items()
                            if c.isChecked()],
            "slice_fields": [k for k, c in self.chk_sfield.items()
                             if c.isChecked()],
        })

    def show_ready(self, frames: "fa.Frames"):
        """Кадри готові: показати програвач і поля, які є."""
        self.slider.blockSignals(True)
        self.slider.setMaximum(max(frames.n - 1, 0))
        self.slider.setValue(0)
        self.slider.blockSignals(False)
        for combo, keys, titles in (
                (self.cmb_body, list(frames.body_vals),
                 {k: tr(fa.BODY_FIELDS[k][0]) for k in fa.BODY_FIELDS}),
                (self.cmb_slice, list(frames.slice_vals),
                 {k: tr(fv.spec_by_key(k).title).split("  ")[0]
                  for k in fa.SLICE_KEYS})):
            combo.blockSignals(True)
            combo.clear()
            combo.addItem("—", "")
            for k in keys:
                combo.addItem(titles.get(k, k), k)
            combo.setCurrentIndex(1 if keys else 0)
            combo.blockSignals(False)
        self.play_box.setVisible(True)
        self.btn_setup.blockSignals(True)
        self.btn_setup.setChecked(False)
        self.btn_setup.blockSignals(False)
        self.setup_box.setVisible(False)
        mb = frames.nbytes() / 1e6
        how = "напряму з файлів" if frames.fast else "через VTK"
        self.lbl_info.setText(tr("{n} кадрів, {mb:.0f} МБ, {how}.{v}", n=frames.n, mb=mb, how=how, v="  " + " ".join(frames.notes)
                                 if frames.notes else ""))

    def show_frame(self, i: int, t: float):
        self.slider.blockSignals(True)
        self.slider.setValue(i)
        self.slider.blockSignals(False)
        n = self.slider.maximum() + 1
        self.lbl_time.setText(tr("t = {t:.6g} с   {v}/{n}", t=t, v=i + 1, n=n))

    def _play(self, on: bool):
        self.btn_play.setText("⏸" if on else "▶")
        self.play_toggled.emit(on)

    def reset_player(self):
        self.btn_play.blockSignals(True)
        self.btn_play.setChecked(False)
        self.btn_play.setText("▶")
        self.btn_play.blockSignals(False)
        self.play_box.setVisible(False)
        self.setup_box.setVisible(True)
        self.lbl_info.setText("")
