"""Смуга керування зрізом на вкладці «Результати».

Тут лише кнопки, повзунок і поля — без жодного VTK. Смуга каже, ЩО людина
хоче (пересунути площину, змінити поле, задати шкалу), а вікно вирішує, як
це намалювати. Тому її можна перевірити окремо, не піднімаючи вʼюпорт.

Позиція площини існує в ТРЬОХ місцях: повзунок, число в міліметрах і сама
площина у вʼюпорті, яку можна тягнути мишею. Вони мусять показувати одне й
те саме, а кожне може змінити решту. Щоб це не крутилося по колу (повзунок
міняє число, число міняє повзунок…), зміна ЗСЕРЕДИНИ смуги викликає сигнал,
а зміна ЗЗОВНІ (`set_position`) — ні.
"""

from __future__ import annotations

from PySide6 import QtCore, QtWidgets

import fs_help as fshelp
import fs_panels as panels
from fs_i18n import tr

# Ті самі ключі й назви, що в fs_views. Тут — копія, а не імпорт: смуга
# свідомо без VTK (див. вище), а fs_views тягне його за собою. Що вони
# не розійшлися, перевіряє tests/test_views.py.
SEED_MODES = (
    ("auto", "Гвинти й перед носом"),
    ("nose", "Перед носом"),
    ("rotors", "Від гвинтів"),
    ("line", "Відрізок — тягнути у вікні"),
    ("sphere", "Куля — тягнути у вікні"),
    ("slice", "На площині зрізу"),
)
SEED_DIRECTIONS = (
    ("forward", "За потоком"),
    ("both", "В обидва боки"),
    ("backward", "Проти потоку"),
)
SEED_COUNT = 75

SLIDER_STEPS = 4000       # роздільність повзунка: хід / 4000
_STEPS_MM = (0.1, 0.2, 0.5, 1.0, 2.0, 5.0, 10.0, 20.0, 50.0, 100.0)


class SliceBar(QtWidgets.QWidget):
    toggled = QtCore.Signal(bool)           # зріз увімкнено/вимкнено
    moved = QtCore.Signal(float)            # нова позиція площини, м
    axis_changed = QtCore.Signal(int)       # 0 = X, 1 = Y, 2 = Z
    field_changed = QtCore.Signal(str)
    limits_changed = QtCore.Signal(float, float)
    limits_auto = QtCore.Signal()           # повернути шкалу «сама»
    limits_fit = QtCore.Signal()            # підігнати шкалу під цей зріз
    streams_toggled = QtCore.Signal(bool)   # лінії потоку
    seeding_changed = QtCore.Signal()       # звідки, скільки, куди — лінії
    seed_reset = QtCore.Signal()            # «На місце»: відрізок / кулю
    contour_toggled = QtCore.Signal(bool)
    edges_toggled = QtCore.Signal(bool)
    body_toggled = QtCore.Signal(bool)
    widget_toggled = QtCore.Signal(bool)    # чи показувати площину-«ручку»
    arrows_toggled = QtCore.Signal(bool)    # стрілки швидкості на зрізі
    layers_changed = QtCore.Signal()        # шари поверх тіла: відрив, слід…
    face_on_requested = QtCore.Signal()
    iso_requested = QtCore.Signal()
    model_requested = QtCore.Signal()       # «До моделі» — камера до тіла
    save_requested = QtCore.Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self._silent = False
        self._fine = (0.0, 1.0)             # де діє повзунок, м
        self._full = (0.0, 1.0)             # де взагалі можна поставити, м
        self._diverging = True

        lay = QtWidgets.QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(3)

        # --- ряд A: що показувати (завжди видно) і кнопки кадру
        ra = QtWidgets.QHBoxLayout()
        self.chk_enable = QtWidgets.QCheckBox("Зріз")
        self.chk_enable.setToolTip(
            "Переріз течії площиною. Площину можна тягнути мишею в вікні "
            "(у косому вигляді) або повзунком.")
        self.chk_streams = QtWidgets.QCheckBox("Лінії потоку")
        self.chk_streams.setToolTip(
            "Лінії потоку, розфарбовані швидкістю. Звідки вони ростуть — "
            "вибір у рядку нижче. Рахуються кілька секунд.")
        self.btn_face = QtWidgets.QPushButton("Вид на зріз")
        self.btn_face.setToolTip("Дивитися рівно на площину, без перспективи")
        self.btn_iso = QtWidgets.QPushButton("3D")
        self.btn_model = QtWidgets.QPushButton("До моделі")
        self.btn_model.setToolTip(
            "Камера — до моделі в УСІХ вʼюпортах: і тут, і в 3D. Бік, з якого "
            "ви дивитесь, лишається. Ctrl+0")
        self.btn_save = QtWidgets.QPushButton("Зберегти кадр…")
        self.btn_save.setToolTip(
            "Кадр для звіту: з підписом, шкалою й масштабом. Зрізу — якщо він "
            "ввімкнений, інакше ліній потоку.")
        ra.addWidget(self.chk_enable)
        ra.addWidget(self.chk_streams)
        ra.addStretch(1)
        for b in (self.btn_face, self.btn_iso, self.btn_model, self.btn_save):
            ra.addWidget(b)
        lay.addLayout(ra)
        self.btn_face.setEnabled(False)

        # --- звідки ростуть лінії потоку: видно, лише коли вони ввімкнені
        self.streams_box = QtWidgets.QWidget()
        sl = QtWidgets.QVBoxLayout(self.streams_box)
        sl.setContentsMargins(0, 0, 0, 0)
        sl.setSpacing(3)
        rs = QtWidgets.QHBoxLayout()
        self.cmb_seed = QtWidgets.QComboBox()
        for key, title in SEED_MODES:
            self.cmb_seed.addItem(title, key)
        self.spin_seeds = QtWidgets.QSpinBox()
        self.spin_seeds.setRange(2, 2000)
        self.spin_seeds.setValue(SEED_COUNT)
        self.spin_seeds.setKeyboardTracking(False)
        self.spin_seeds.setToolTip(
            "Скільки насінин — стільки й ліній. Кільця перед гвинтами мають "
            "свою густину й цього числа не слухають.")
        self.lbl_radius = QtWidgets.QLabel("Радіус")
        self.spin_radius = panels.dspin(0.1, 1e6, 50.0, step=5.0, decimals=1,
                                        suffix=" мм")
        self.spin_radius.setKeyboardTracking(False)
        self.spin_radius.setToolTip(
            "Радіус кулі, в обʼємі якої сидять насінини. У вікні його "
            "можна змінити й правою кнопкою миші.")
        self.cmb_seed_dir = QtWidgets.QComboBox()
        for key, title in SEED_DIRECTIONS:
            self.cmb_seed_dir.addItem(title, key)
        self.cmb_seed_dir.setToolTip(
            "За потоком — куди йде повітря від насінин. Проти потоку — звідки "
            "воно прийшло. В обидва боки — для насіння посеред течії: у "
            "слід, у вихор, на площину зрізу.")
        self.btn_seed_reset = QtWidgets.QPushButton("На місце")
        self.btn_seed_reset.setToolTip(
            "Поставити відрізок чи кулю туди, де вони стоять спочатку: "
            "відрізок — перед носом, куля — за тілом.")
        rs.addWidget(QtWidgets.QLabel("Звідки"))
        rs.addWidget(self.cmb_seed)
        rs.addWidget(QtWidgets.QLabel("Ліній"))
        rs.addWidget(self.spin_seeds)
        rs.addWidget(self.lbl_radius)
        rs.addWidget(self.spin_radius)
        rs.addWidget(self.cmb_seed_dir)
        rs.addWidget(self.btn_seed_reset)
        rs.addStretch(1)
        sl.addLayout(rs)
        block = panels.explain(self.cmb_seed, "streams_seed")
        if block is not None:
            sl.addWidget(block)
        lay.addWidget(self.streams_box)
        self.streams_box.setVisible(False)
        self._seed_mode_shown()

        # --- шари поверх тіла: видно завжди, як і зріз із лініями. Кожен
        # рахується лише ввімкнений — обʼєм і так читається один раз.
        rl = QtWidgets.QHBoxLayout()
        rl.addWidget(QtWidgets.QLabel("Шари"))
        self.chk_reverse = QtWidgets.QCheckBox("Зворотна течія")
        self.chk_loss = QtWidgets.QCheckBox("Слід")
        self.chk_wall = QtWidgets.QCheckBox("Лінії на тілі")
        self.chk_wash = QtWidgets.QCheckBox("Обдув гвинтів")
        self._layer_boxes = {"reverse": self.chk_reverse, "loss": self.chk_loss,
                             "wall": self.chk_wall, "wash": self.chk_wash}
        for key, box in self._layer_boxes.items():
            rl.addWidget(box)
            h = fshelp.get("layer_" + key)
            if h is not None:
                box.setToolTip(h.tooltip())
        rl.addStretch(1)
        lay.addLayout(rl)
        # Один блок на ряд, а не по блоку на галочку: див. fs_help["layers"].
        block = panels.explain(None, "layers")
        if block is not None:
            lay.addWidget(block)

        # --- решта ховається, доки зріз вимкнено
        self.details = QtWidgets.QWidget()
        dl = QtWidgets.QVBoxLayout(self.details)
        dl.setContentsMargins(0, 0, 0, 0)
        dl.setSpacing(3)

        # ряд B: вісь, повзунок, число, кроки, швидкі переходи
        rb = QtWidgets.QHBoxLayout()
        self.cmb_axis = QtWidgets.QComboBox()
        for label, axis in (("Y — по розмаху", 1), ("X — уздовж потоку", 0),
                            ("Z — по висоті", 2)):
            self.cmb_axis.addItem(label, axis)
        self.slider = QtWidgets.QSlider(QtCore.Qt.Horizontal)
        self.slider.setRange(0, SLIDER_STEPS)
        self.slider.setToolTip(
            "Повзунок ходить по габариту тіла з запасом. Далі площину можна "
            "поставити числом праворуч — воно приймає весь домен.")
        self.spin = panels.dspin(-1e6, 1e6, 0.0, step=5.0, decimals=1,
                                 suffix=" мм")
        self.spin.setMinimumWidth(110)
        self.btn_prev = QtWidgets.QToolButton()
        self.btn_prev.setText("◀")
        self.btn_next = QtWidgets.QToolButton()
        self.btn_next.setText("▶")
        for b in (self.btn_prev, self.btn_next):
            b.setAutoRepeat(True)
            b.setAutoRepeatInterval(60)
            b.setToolTip("Крок площини: 5 мм")
        self.cmb_stops = QtWidgets.QComboBox()
        self.cmb_stops.setToolTip("Поставити площину в помітне місце")
        rb.addWidget(QtWidgets.QLabel("Вісь"))
        rb.addWidget(self.cmb_axis)
        rb.addWidget(self.slider, 1)
        rb.addWidget(self.spin)
        rb.addWidget(self.btn_prev)
        rb.addWidget(self.btn_next)
        rb.addWidget(self.cmb_stops)
        dl.addLayout(rb)

        # ряд C: поле й шкала
        r2 = QtWidgets.QHBoxLayout()
        r2.addWidget(QtWidgets.QLabel("Поле"))
        self.cmb_field = QtWidgets.QComboBox()
        self.cmb_field.setMinimumWidth(260)
        r2.addWidget(self.cmb_field)
        self.lbl_scale = QtWidgets.QLabel("Шкала")
        r2.addWidget(self.lbl_scale)
        self.spin_lo = panels.dspin(-1e9, 1e9, 0.0, step=1.0, decimals=3)
        self.spin_hi = panels.dspin(-1e9, 1e9, 1.0, step=1.0, decimals=3)
        r2.addWidget(self.spin_lo)
        r2.addWidget(self.spin_hi)
        self.btn_auto = QtWidgets.QPushButton("Авто")
        self.btn_auto.setToolTip(
            "Шкала сама: за швидкістю й тиском у зрізах навколо тіла, а не "
            "за пристінковими комірками, які роздувають її.")
        self.btn_fit = QtWidgets.QPushButton("Під зріз")
        self.btn_fit.setToolTip(
            "Підігнати шкалу під ЦЕЙ зріз. Далі вона стоїть, поки площину "
            "рухають, — щоб колір міняло поле, а не шкала.")
        r2.addWidget(self.btn_auto)
        r2.addWidget(self.btn_fit)
        r2.addStretch(1)
        dl.addLayout(r2)

        # ряд D: що на зрізі показувати
        r3 = QtWidgets.QHBoxLayout()
        self.chk_contour = QtWidgets.QCheckBox("Контур тіла")
        self.chk_contour.setChecked(True)
        self.chk_edges = QtWidgets.QCheckBox("Сітка")
        self.chk_edges.setToolTip("Показати ребра комірок на зрізі")
        self.chk_body = QtWidgets.QCheckBox("Тіло")
        self.chk_body.setChecked(True)
        self.chk_widget = QtWidgets.QCheckBox("Площина в вікні")
        self.chk_widget.setChecked(True)
        self.chk_widget.setToolTip(
            "Напівпрозора «ручка», за яку площину можна тягнути мишею. "
            "Вимкніть, щоб вона не потрапляла в знімок.")
        self.chk_arrows = QtWidgets.QCheckBox("Стрілки")
        for c in (self.chk_contour, self.chk_edges, self.chk_body,
                  self.chk_widget, self.chk_arrows):
            r3.addWidget(c)
        r3.addStretch(1)
        dl.addLayout(r3)
        block = panels.explain(self.chk_arrows, "slice_arrows")
        if block is not None:
            dl.addWidget(block)
        lay.addWidget(self.details)
        self.details.setVisible(False)

        self._wire()
        self.setEnabled(False)

    # --- звʼязки --------------------------------------------------------

    def _wire(self):
        self.chk_enable.toggled.connect(self._enable_toggled)
        self.cmb_axis.currentIndexChanged.connect(
            lambda _: self._emit(self.axis_changed, self.axis()))
        self.slider.valueChanged.connect(self._slider_moved)
        self.spin.valueChanged.connect(self._spin_moved)
        self.btn_prev.clicked.connect(lambda: self._step(-1))
        self.btn_next.clicked.connect(lambda: self._step(+1))
        self.cmb_stops.activated.connect(self._stop_chosen)
        self.cmb_field.currentIndexChanged.connect(
            lambda _: self._emit(self.field_changed, self.field()))
        self.spin_lo.valueChanged.connect(lambda _: self._limits_edited())
        self.spin_hi.valueChanged.connect(lambda _: self._limits_edited())
        self.btn_auto.clicked.connect(lambda: self.limits_auto.emit())
        self.btn_fit.clicked.connect(lambda: self.limits_fit.emit())
        self.chk_streams.toggled.connect(self.streams_box.setVisible)
        self.chk_streams.toggled.connect(self.streams_toggled)
        self.cmb_seed.currentIndexChanged.connect(self._seed_mode_changed)
        self.spin_seeds.valueChanged.connect(
            lambda _: self._emit(self.seeding_changed))
        self.spin_radius.valueChanged.connect(
            lambda _: self._emit(self.seeding_changed))
        self.cmb_seed_dir.currentIndexChanged.connect(
            lambda _: self._emit(self.seeding_changed))
        self.btn_seed_reset.clicked.connect(lambda: self.seed_reset.emit())
        self.chk_contour.toggled.connect(self.contour_toggled)
        self.chk_edges.toggled.connect(self.edges_toggled)
        self.chk_body.toggled.connect(self.body_toggled)
        self.chk_widget.toggled.connect(self.widget_toggled)
        self.chk_arrows.toggled.connect(self.arrows_toggled)
        for box in self._layer_boxes.values():
            box.toggled.connect(lambda _: self._emit(self.layers_changed))
        self.btn_face.clicked.connect(lambda: self.face_on_requested.emit())
        self.btn_iso.clicked.connect(lambda: self.iso_requested.emit())
        self.btn_model.clicked.connect(lambda: self.model_requested.emit())
        self.btn_save.clicked.connect(lambda: self.save_requested.emit())

    def _emit(self, signal, *args):
        if not self._silent:
            signal.emit(*args)

    def _seed_mode_shown(self):
        """Поля, які щось значать лише для деяких місць: радіус — для кулі,
        «На місце» — для того, що тягнуть, число — не для самих гвинтів."""
        mode = self.seed_mode()
        self.lbl_radius.setVisible(mode == "sphere")
        self.spin_radius.setVisible(mode == "sphere")
        self.btn_seed_reset.setVisible(mode in ("line", "sphere"))
        self.spin_seeds.setEnabled(mode != "rotors")

    def _seed_mode_changed(self, _index: int):
        self._seed_mode_shown()
        self._emit(self.seeding_changed)

    def _enable_toggled(self, on: bool):
        self.details.setVisible(on)
        self.btn_face.setEnabled(on)
        self._emit(self.toggled, on)

    # --- позиція: повзунок ↔ число --------------------------------------

    def _to_slider(self, pos: float) -> int:
        lo, hi = self._fine
        t = (pos - lo) / (hi - lo) if hi > lo else 0.0
        return int(round(min(max(t, 0.0), 1.0) * SLIDER_STEPS))

    def _from_slider(self, v: int) -> float:
        lo, hi = self._fine
        return lo + (hi - lo) * v / SLIDER_STEPS

    def _slider_moved(self, v: int):
        if self._silent:
            return
        pos = self._from_slider(v)
        self._silent = True
        self.spin.setValue(pos * 1000.0)
        self._silent = False
        self.moved.emit(pos)

    def _spin_moved(self, mm: float):
        if self._silent:
            return
        pos = mm / 1000.0
        self._silent = True
        self.slider.setValue(self._to_slider(pos))
        self._silent = False
        self.moved.emit(pos)

    def _step(self, sign: int):
        self.spin.setValue(self.spin.value() + sign * self.spin.singleStep())

    def _stop_chosen(self, index: int):
        pos = self.cmb_stops.itemData(index)
        self.cmb_stops.setCurrentIndex(0)          # 0 — «Перейти до…»
        if pos is not None:
            self.set_position(float(pos), silent=False)

    # --- шкала ----------------------------------------------------------

    def _limits_edited(self):
        if self._silent:
            return
        hi = self.spin_hi.value()
        lo = -hi if self._diverging else self.spin_lo.value()
        if hi > lo:
            self.limits_changed.emit(lo, hi)

    # --- що просить вікно -----------------------------------------------

    def is_on(self) -> bool:
        return self.chk_enable.isChecked()

    def streams_on(self) -> bool:
        return self.chk_streams.isChecked()

    def seed_mode(self) -> str:
        """Звідки ростуть лінії: ключ із SEED_MODES."""
        return str(self.cmb_seed.currentData() or "auto")

    def seed_count(self) -> int:
        return int(self.spin_seeds.value())

    def seed_direction(self) -> str:
        return str(self.cmb_seed_dir.currentData() or "forward")

    def seed_radius(self) -> float:
        """Радіус кулі, м."""
        return self.spin_radius.value() / 1000.0

    def set_seeding(self, mode: str | None = None, count: int | None = None,
                    direction: str | None = None,
                    radius: float | None = None) -> None:
        """Показати вибір, що прийшов ззовні (налаштування, віджет у вікні),
        — без сигналу: це відгомін, а не нова воля людини."""
        was = self._silent
        self._silent = True
        if mode is not None:
            i = self.cmb_seed.findData(mode)
            if i >= 0:
                self.cmb_seed.setCurrentIndex(i)
        if count is not None:
            self.spin_seeds.setValue(int(count))
        if direction is not None:
            i = self.cmb_seed_dir.findData(direction)
            if i >= 0:
                self.cmb_seed_dir.setCurrentIndex(i)
        if radius is not None:
            self.spin_radius.setValue(float(radius) * 1000.0)
        self._silent = was
        self._seed_mode_shown()

    def layers(self) -> set:
        """Які шари ввімкнено: {"reverse", "loss", "wall", "wash"}."""
        return {k for k, box in self._layer_boxes.items() if box.isChecked()}

    def axis(self) -> int:
        return int(self.cmb_axis.currentData())

    def field(self) -> str:
        return str(self.cmb_field.currentData() or "")

    def position(self) -> float:
        return self.spin.value() / 1000.0

    def set_ranges(self, fine: tuple, full: tuple) -> None:
        """Де ходить повзунок (габарит тіла з запасом) і де взагалі можна
        поставити площину (весь домен)."""
        self._silent = True
        self._fine, self._full = tuple(fine), tuple(full)
        self.spin.setRange(full[0] * 1000.0, full[1] * 1000.0)
        # Крок стрілок і кнопок — соті частки ходу повзунка, «круглим» числом.
        # Фіксований крок не годився б: на дрібній моделі 5 мм — це пів-
        # тіла, а на великій — жодного руху.
        span_mm = (fine[1] - fine[0]) * 1000.0
        step = max((s for s in _STEPS_MM if s <= span_mm / 100.0),
                   default=_STEPS_MM[0])
        self.spin.setSingleStep(step)
        for b in (self.btn_prev, self.btn_next):
            b.setToolTip(tr("Крок площини: {step:g} мм",
                            step=self.spin.singleStep()))
        self._silent = False

    def set_position(self, pos: float, *, silent: bool = True) -> None:
        """Поставити позицію. `silent=True` — це відгомін того, що вже
        сталося у вʼюпорті, і сигналу він не подає."""
        was = self._silent
        self._silent = True
        self.spin.setValue(pos * 1000.0)
        self.slider.setValue(self._to_slider(pos))
        self._silent = was
        if not silent:
            self.moved.emit(pos)

    def set_fields(self, fields: list, current: str = "") -> None:
        """fields — [(ключ, назва)]."""
        self._silent = True
        self.cmb_field.clear()
        for key, title in fields:
            self.cmb_field.addItem(title, key)
        for i in range(self.cmb_field.count()):
            if self.cmb_field.itemData(i) == current:
                self.cmb_field.setCurrentIndex(i)
                break
        self._silent = False

    def set_stops(self, stops: list) -> None:
        """stops — [(підпис, позиція, м)]."""
        self._silent = True
        self.cmb_stops.clear()
        self.cmb_stops.addItem("Перейти до…", None)
        for label, pos in stops:
            self.cmb_stops.addItem(label, float(pos))
        self._silent = False

    def set_limits(self, lo: float, hi: float, diverging: bool) -> None:
        """Показати межі шкали. Для знакової шкали — одне число «±»."""
        self._silent = True
        self._diverging = bool(diverging)
        self.spin_lo.setVisible(not diverging)
        self.lbl_scale.setText("Шкала ±" if diverging else "Шкала від / до")
        self.spin_lo.setValue(lo)
        self.spin_hi.setValue(hi)
        self._silent = False

    def set_axis(self, axis: int) -> None:
        self._silent = True
        for i in range(self.cmb_axis.count()):
            if self.cmb_axis.itemData(i) == axis:
                self.cmb_axis.setCurrentIndex(i)
        self._silent = False
