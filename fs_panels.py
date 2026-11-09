"""Панелі налаштувань. Кожна пише прямо в CaseCfg і сигналить про зміну."""

from __future__ import annotations

import math
import os
from html import escape as html_escape

from PySide6 import QtCore, QtGui, QtWidgets

import fs_alphawrap as alphawrap
import fs_backend as be
import fs_case as fc
import fs_help as fshelp
import fs_library as lib
import fs_meshfix as meshfix
import fs_physics as phys
import fs_winfix as winfix

# Усі блоки пояснень, створені панелями. Режим новачка просто показує або
# ховає їх усі — так не треба протягувати прапорець крізь кожну панель.
_HELP_BLOCKS: list[QtWidgets.QWidget] = []
_ROOKIE = [False]

# Сірі примітки, які повністю поглинає розгорнуте пояснення: (мітка, ключ).
# У режимі новачка вони ховаються, щоб те саме не стояло двічі поспіль.
_SHADOWED: list[tuple[QtWidgets.QWidget, str]] = []
_EXPLAINED: set[str] = set()


def set_rookie(on: bool) -> None:
    _ROOKIE[0] = bool(on)
    for w in _HELP_BLOCKS:
        try:
            w.setVisible(_ROOKIE[0])
        except RuntimeError:      # віджет уже знищено
            pass
    for w, key in _SHADOWED:
        try:
            w.setVisible(not (_ROOKIE[0] and key in _EXPLAINED))
        except RuntimeError:
            pass


def is_rookie() -> bool:
    return _ROOKIE[0]


class WrapLabel(QtWidgets.QLabel):
    """Мітка з переносом рядків, яка чесно повідомляє свою висоту.

    Звичайний QLabel із wordWrap каже, що йому досить одного рядка: поки його
    не показали, він не знає своєї ширини. Всередині QScrollArea це руйнує
    вкладку — прокрутка рахує потрібну висоту за цим заниженим мінімумом,
    смуга не зʼявляється, і замість неї стискаються в кількапіксельні смужки
    всі поля поряд.

    Тому висоту рахуємо самі через QTextDocument і прибиваємо її як
    мінімальну: так її бачать і QFormLayout, і QScrollArea, які
    heightForWidth крізь себе не пропускають.
    """

    PAD_W = 0   # скільки пікселів ширини зʼїдають рамка й відступи стилю
    PAD_H = 2
    hide_when_empty = False

    def __init__(self, text: str = "", parent=None):
        super().__init__(text, parent)
        self.setWordWrap(True)
        policy = self.sizePolicy()
        policy.setHeightForWidth(True)
        policy.setVerticalPolicy(QtWidgets.QSizePolicy.Minimum)
        self.setSizePolicy(policy)
        self._doc = QtGui.QTextDocument()
        self._doc.setDocumentMargin(0)
        self._last_w = 0
        self._sync_doc()

    def _sync_doc(self):
        if self.textFormat() == QtCore.Qt.RichText:
            self._doc.setHtml(self.text())
        else:
            self._doc.setPlainText(self.text())

    def setText(self, text):
        super().setText(text)
        self._sync_doc()
        self._last_w = 0
        if self.hide_when_empty:
            # Порожня мітка все одно займає рядок і лишає дірку в панелі.
            self.setVisible(bool(text))
        self._refresh_height()

    def heightForWidth(self, width: int) -> int:
        # Шрифт беремо щоразу заново: стиль застосовується після створення
        # віджета, і font-size зі стилю зʼявляється в self.font() лише потім.
        self._doc.setDefaultFont(self.font())
        self._doc.setTextWidth(max(width - self.PAD_W, 80))
        own = int(self._doc.size().height()) + self.PAD_H
        # Qt рахує те саме по-своєму (інакше враховує поля стилю). Беремо
        # більше з двох: зайвий піксель нікому не шкодить, а зрізаний рядок —
        # так, і саме на цьому ламалася вкладка.
        return max(own, QtWidgets.QLabel.heightForWidth(self, width))

    def sizeHint(self) -> QtCore.QSize:
        w = self.width() or 380
        return QtCore.QSize(w, self.heightForWidth(w))

    def minimumSizeHint(self) -> QtCore.QSize:
        w = self.width() or 380
        return QtCore.QSize(140, self.heightForWidth(w))

    def _refresh_height(self):
        w = self.width()
        if not w:
            return
        h = self.heightForWidth(w)
        if h != self.minimumHeight():
            self.setMinimumHeight(h)
            self.updateGeometry()

    def resizeEvent(self, event):
        super().resizeEvent(event)
        w = self.width()
        if w and abs(w - self._last_w) > 2:
            self._last_w = w
            self._refresh_height()


class HelpLabel(WrapLabel):
    """Розгорнуте пояснення режиму новачка."""

    # Поля коробки зі стилю нижче: ліворуч рамка 3 + відступ 8, праворуч 8;
    # згори 2 + 6, знизу 6 + 6.
    PAD_W = 19
    PAD_H = 22

    def __init__(self, html: str, parent=None):
        super().__init__(html, parent)
        self.setTextFormat(QtCore.Qt.RichText)
        self._sync_doc()
        self.setStyleSheet(
            "QLabel { background:#2b2f36; border-left:3px solid #5a7d9a;"
            " padding:6px 8px; margin:2px 0 6px 0; color:#b9c4cf;"
            " font-size:11px; }")


def explain(widget, key: str):
    """Вішає підказку на віджет і повертає блок пояснення для розкладки.

    Підказка доступна завжди — вона нікому не заважає. Розгорнутий блок
    зʼявляється лише в режимі новачка, інакше панелі стають нечитабельними
    від суцільного тексту.
    """
    h = fshelp.get(key)
    if h is None:
        return None
    if widget is not None:
        widget.setToolTip(h.tooltip())

    block = HelpLabel(h.html())
    block.setVisible(_ROOKIE[0])
    _HELP_BLOCKS.append(block)
    _EXPLAINED.add(key)
    return block


def explain_tip(widget, key: str) -> None:
    """Лише підказка, БЕЗ блоку пояснення.

    Для полів, під якими блоку немає куди лягти (рядок, що ховається
    разом з полем). Викликати `explain` і не вкласти блок у розкладку не
    можна: блок без батька Qt показує ОКРЕМИМ ВІКНОМ, і в режимі новачка
    кожна панель плодила по віконцю «python» поверх усього — так і
    сталося з полями двигуна й диска.
    """
    h = fshelp.get(key)
    if h is None:
        return
    if widget is not None:
        widget.setToolTip(h.tooltip())
    _EXPLAINED.add(key)

NEWLINE = chr(10)

LEVEL_COLOR = {
    "ok": "#4c9a52",
    "warn": "#b8860b",
    "bad": "#c0392b",
}

MEDIA = {
    "Повітря, 15 °C": (1.470e-5, 1.225),
    "Повітря, 0 °C": (1.338e-5, 1.292),
    "Повітря, 25 °C": (1.562e-5, 1.184),
    "Вода, 20 °C": (1.004e-6, 998.2),
    "Своє значення": (None, None),
}


def media_name(nu: float, rho: float) -> str:
    """Який готовий набір відповідає цим nu і rho. Зворотний бік MEDIA:
    у конфігу лежать самі числа, а комбобокс має показати їх назву."""
    for name, (mnu, mrho) in MEDIA.items():
        if mnu is None:
            continue
        if abs(nu - mnu) <= 1e-6 * mnu and abs(rho - mrho) <= 1e-6 * mrho:
            return name
    return "Своє значення"


# --------------------------------------------------------------------------
# Коліщатко гортає панель, а не міняє налаштування
# --------------------------------------------------------------------------

class _WheelGuard(QtCore.QObject):
    """Коліщатко над полем вводу має гортати ПАНЕЛЬ, а не крутити значення.

    Типова поведінка Qt тут шкідлива саме в цій програмі. Людина гортає
    довгу вкладку, курсор проходить над полем «Рівень max» — і колесо
    міняє його з 5 на 7. Ні звуку, ні підсвітки: сітка просто стане в
    шістдесят чотири рази дрібнішою, і дізнатися про це можна буде хіба
    з того, що прогін не влазить у памʼять.

    Це той самий рід вади, що ловився весь день: НІЧОГО НЕ ПАДАЄ,
    а рахується не те, що задумано.

    Тому подія колеса до таких віджетів не доходить узагалі, а
    пересилається смузі прокрутки, всередині якої вони лежать. Значення
    лишається змінним кліком по стрілках, набором із клавіатури й
    стрілками вгору-вниз — тобто всім, що робиться НАВМИСНЕ.

    Фокус тут навмисно НЕ перевіряється. «Крутити можна, якщо поле в
    фокусі» — половинчастий захист: клацнув, щоб набрати число, повів
    коліщатком, щоб погортати далі, і значення поїхало. Саме цей випадок
    і є найчастіший.
    """

    # QScrollBar теж походить від QAbstractSlider, і його чіпати НЕ МОЖНА:
    # це власна смуга прокрутки, якою все й гортається.
    TOUCHY = (QtWidgets.QAbstractSpinBox, QtWidgets.QComboBox,
              QtWidgets.QAbstractSlider)

    def eventFilter(self, obj, event):
        if event.type() != QtCore.QEvent.Wheel:
            return False
        if isinstance(obj, QtWidgets.QScrollBar):
            return False
        if not isinstance(obj, self.TOUCHY):
            return False
        area = self._area(obj)
        if area is None:
            # Гортати нема чого — не відбираємо звичну поведінку там, де
            # вона нікому не шкодить (наприклад, у діалозі без прокрутки).
            return False
        QtWidgets.QApplication.sendEvent(area.viewport(), event)
        return True

    @staticmethod
    def _area(obj):
        w = obj.parentWidget()
        while w is not None:
            if isinstance(w, QtWidgets.QScrollArea):
                return w
            w = w.parentWidget()
        return None


_WHEEL_GUARD = _WheelGuard()


class FlowLayout(QtWidgets.QLayout):
    """Рядок віджетів, що ПЕРЕНОСИТЬСЯ, як текст, коли ширини бракує.

    Звичайний QHBoxLayout вимагає місця під усі віджети разом, і саме так
    нижній рядок вʼюпорту (десять позначок і шість кнопок) не давав вікну
    стати вужчим за ~1750 px: на екрані з масштабом 125 % вікно вилазило за
    край, а повзунок між панелями впирався й не рухався. Тут мінімум — це
    найширший ОДИН віджет, а решта лягає на наступні рядки.
    """

    def __init__(self, parent=None, spacing: int = 6):
        super().__init__(parent)
        self._items: list = []
        self._gap = spacing

    def addItem(self, item):
        self._items.append(item)

    def count(self):
        return len(self._items)

    def itemAt(self, i):
        return self._items[i] if 0 <= i < len(self._items) else None

    def takeAt(self, i):
        return self._items.pop(i) if 0 <= i < len(self._items) else None

    def expandingDirections(self):
        return QtCore.Qt.Orientation(0)

    def hasHeightForWidth(self):
        return True

    def heightForWidth(self, width):
        return self._place(QtCore.QRect(0, 0, width, 0), apply=False)

    def setGeometry(self, rect):
        super().setGeometry(rect)
        self._place(rect, apply=True)

    def sizeHint(self):
        return self.minimumSize()

    def minimumSize(self):
        size = QtCore.QSize()
        for item in self._items:
            size = size.expandedTo(item.minimumSize())
        m = self.contentsMargins()
        return size + QtCore.QSize(m.left() + m.right(), m.top() + m.bottom())

    def _place(self, rect, apply: bool) -> int:
        m = self.contentsMargins()
        area = rect.adjusted(m.left(), m.top(), -m.right(), -m.bottom())
        x, y, line = area.x(), area.y(), 0
        for item in self._items:
            if item.widget() is not None and item.widget().isHidden():
                continue
            hint = item.sizeHint()
            if x > area.x() and x + hint.width() > area.right() + 1:
                x, y, line = area.x(), y + line + self._gap, 0
            if apply:
                item.setGeometry(QtCore.QRect(QtCore.QPoint(x, y), hint))
            x += hint.width() + self._gap
            line = max(line, hint.height())
        return y + line - rect.y() + m.bottom()


def compact_combos(widget, chars: int = 14) -> None:
    """Випадні списки не мають задавати ширину панелі.

    Типово QComboBox хоче місця під НАЙДОВШИЙ пункт. Пункти тут із
    поясненнями («повітрозабірник — кришка отвору: тут двигун забирає
    повітря з потоку»), і один такий розпирав панель «Геометрія» до 552 px
    при видимих 430: зʼявлялася горизонтальна прокрутка. Тепер поле
    обрізає текст трикрапкою, а розгорнутий список — на всю ширину пунктів.
    """
    for c in widget.findChildren(QtWidgets.QComboBox):
        c.setSizeAdjustPolicy(
            QtWidgets.QComboBox.AdjustToMinimumContentsLengthWithIcon)
        c.setMinimumContentsLength(chars)
        # Самої політики мало: `sizeHint` після неї 144 px, а
        # `minimumSizeHint` лишається за найдовшим пунктом (438 px на
        # списку адаптації сітки — вкладка «Сітка» прокручувалась убік).
        # Розкладка бере саме мінімум, тож ставимо його явно: явний
        # `minimumWidth` для неї важливіший за підказку.
        if c.minimumWidth() == 0:
            c.setMinimumWidth(min(c.sizeHint().width(),
                                  c.minimumSizeHint().width()))
        view = c.view()
        if view is not None and c.count():
            view.setMinimumWidth(view.sizeHintForColumn(0) + 24)


def guard_wheel(widget) -> None:
    """Поставити захист на віджет і на все, що всередині нього.

    Викликається і з помічників `dspin`/`ispin` (щоб накрити те, що
    створюється пізніше), і один раз на цілу панель (щоб накрити
    випадаючі списки, які створюються в місці вжитку).
    """
    if widget is None:
        return
    if isinstance(widget, _WheelGuard.TOUCHY) \
            and not isinstance(widget, QtWidgets.QScrollBar):
        widget.installEventFilter(_WHEEL_GUARD)
        # Щоб колесо ще й не давало полю фокус мимохідь.
        widget.setFocusPolicy(QtCore.Qt.StrongFocus)
    for child in widget.findChildren(QtWidgets.QWidget):
        if isinstance(child, _WheelGuard.TOUCHY) \
                and not isinstance(child, QtWidgets.QScrollBar):
            child.installEventFilter(_WHEEL_GUARD)
            child.setFocusPolicy(QtCore.Qt.StrongFocus)


# --------------------------------------------------------------------------
# Дрібні помічники
# --------------------------------------------------------------------------

def _wrap(layout) -> QtWidgets.QWidget:
    """Загорнути layout у віджет — QFormLayout приймає в рядок саме віджет."""
    w = QtWidgets.QWidget()
    layout.setContentsMargins(0, 0, 0, 0)
    w.setLayout(layout)
    return w


def dspin(lo, hi, val, step=0.1, decimals=3, suffix="") -> QtWidgets.QDoubleSpinBox:
    w = QtWidgets.QDoubleSpinBox()
    w.setRange(lo, hi)
    w.setDecimals(decimals)
    w.setSingleStep(step)
    w.setValue(val)
    w.setSuffix(suffix)
    w.setKeyboardTracking(False)
    w.setMinimumWidth(96)
    guard_wheel(w)
    return w


def ispin(lo, hi, val, step=1, suffix="") -> QtWidgets.QSpinBox:
    w = QtWidgets.QSpinBox()
    w.setRange(lo, hi)
    w.setSingleStep(step)
    w.setValue(val)
    w.setSuffix(suffix)
    w.setKeyboardTracking(False)
    w.setMinimumWidth(96)
    guard_wheel(w)
    return w


class SciSpin(QtWidgets.QDoubleSpinBox):
    """Поле для дрібних величин на кшталт nu: показує 1.004e-06.

    Звичайний QDoubleSpinBox має фіксовану кількість знаків після коми і на
    такі числа не годиться. При восьми знаках в’язкість води 1.004e-06
    заокруглювалася до 1e-06: набір «Вода, 20 °C» ставив у конфіг точне
    значення, а в полі стояло округлене — тобто вікно показувало Re на 0.4%
    інший, ніж рахувався. А стоїло торкнутися поля, як округлене значення
    ставало справжнім.

    Тому і показ, і читання йдуть через звичайний запис числа, а крок —
    множенням: від 1e-06 додавання 1e-06 було б подвоєнням, а від 1e-03 —
    нічим.
    """

    FMT = "%.5g"

    def textFromValue(self, value: float) -> str:
        return self.FMT % value

    def valueFromText(self, text: str) -> float:
        try:
            return float(text.strip().replace(",", "."))
        except ValueError:
            return self.value()

    def validate(self, text: str, pos: int):
        raw = text.strip().replace(",", ".").lower()
        try:
            float(raw)
        except ValueError:
            # Незакінчений набір: «1e», «1e-», «-». Заборонити його означало б
            # не дати ввести показник степеня взагалі.
            if raw in ("", "-", "+", ".") or raw.endswith(("e", "e-", "e+")):
                return (QtGui.QValidator.Intermediate, text, pos)
            return (QtGui.QValidator.Invalid, text, pos)
        return (QtGui.QValidator.Acceptable, text, pos)

    def stepBy(self, steps: int):
        value = self.value()
        self.setValue(value * (1.1 ** steps) if value > 0
                      else self.minimum())


def sci(lo, hi, val, decimals=12) -> QtWidgets.QDoubleSpinBox:
    """Поле для дрібних величин на кшталт nu."""
    w = SciSpin()
    w.setRange(lo, hi)
    w.setDecimals(decimals)
    w.setValue(val)
    w.setKeyboardTracking(False)
    w.setMinimumWidth(120)
    return w


class HintLabel(WrapLabel):
    """Сірий рядок-примітка під полем."""

    PAD_H = 3


def hint(text: str, replaced_by: str = "") -> QtWidgets.QLabel:
    """Сіра примітка під полем.

    `replaced_by` — ключ довідки, чиє розгорнуте пояснення каже те саме.
    Така примітка ховається в режимі новачка: інакше під полем стоять
    два тексти про одне й те саме.
    """
    lab = HintLabel(text)
    lab.setStyleSheet("color: #777; font-size: 11px;")
    if replaced_by:
        _SHADOWED.append((lab, replaced_by))
        lab.setVisible(not (_ROOKIE[0] and replaced_by in _EXPLAINED))
    return lab


def set_tol(combo: QtWidgets.QComboBox, value: float) -> None:
    """Ставить поріг збіжності в комбобокс, де він лежить текстом.

    Просте форматування тут не годиться: `"%g" % 1e-4` дає «0.0001», такого
    пункту в списку немає, і комбобокс мовчки лишився б на старому значенні —
    вікно показувало б один поріг, а кейс рахувався б з іншим. Тому рядок
    будуємо з показника степеня й перевіряємо, що він читається назад у те
    саме число; чого немає в списку, те додаємо.
    """
    if value <= 0:
        return
    text = "1e-%d" % round(-math.log10(value))
    if combo.findText(text) < 0 or abs(float(text) - value) > value * 1e-9:
        text = "%g" % value
        if combo.findText(text) < 0:
            combo.addItem(text)
    combo.setCurrentText(text)


def section(title: str) -> QtWidgets.QLabel:
    lab = QtWidgets.QLabel(title)
    f = lab.font()
    f.setBold(True)
    lab.setFont(f)
    lab.setContentsMargins(0, 10, 0, 2)
    return lab


class Panel(QtWidgets.QWidget):
    """База: форма + сигнал зміни."""

    changed = QtCore.Signal()

    def __init__(self, cfg: fc.CaseCfg, parent=None):
        super().__init__(parent)
        self.cfg = cfg
        self._loading = False
        self.v = QtWidgets.QVBoxLayout(self)
        self.v.setContentsMargins(10, 8, 10, 8)
        self.v.setSpacing(4)
        self.form = QtWidgets.QFormLayout()
        self.form.setLabelAlignment(QtCore.Qt.AlignRight)
        self.form.setFieldGrowthPolicy(QtWidgets.QFormLayout.AllNonFixedFieldsGrow)
        self.v.addLayout(self.form)

    def emit_changed(self):
        if not self._loading:
            self.changed.emit()

    def bind(self, widget, setter):
        """Прив'язує віджет до записувача в конфіг."""
        sig = getattr(widget, "valueChanged", None) or \
              getattr(widget, "currentTextChanged", None) or \
              getattr(widget, "toggled", None)

        def handler(*_):
            if self._loading:
                return
            setter()
            self.emit_changed()

        sig.connect(handler)
        return widget

    def load_cfg(self):
        """Переносить значення з конфігу назад у віджети.

        Потрібне двом речам: пораднику, який править конфіг сам, і відкриттю
        збереженої симуляції. Кожна панель називає власні поля — база лишає
        цей метод порожнім, щоб виклик можна було робити циклом по панелях.

        Під час завантаження `_loading` тримається піднятим: сетери, які
        сидять на сигналах віджетів, писали б у конфіг те саме, що ми з
        нього щойно прочитали, і на кожному полі смикали б перерахунок
        усього вікна.
        """

    def _loaded(self, fn):
        """Виконує fn із піднятим прапорцем завантаження."""
        self._loading = True
        try:
            fn()
        finally:
            self._loading = False


# --------------------------------------------------------------------------
# Геометрія
# --------------------------------------------------------------------------

# Одиниці STL і множник у метри. Порядок — той, що в полі «Одиниці STL».
UNITS = ("авто", "метри", "міліметри", "сантиметри", "дюйми")
DEFAULT_UNITS = "міліметри"
_UNIT_FACTOR = {"метри": 1.0, "міліметри": 0.001, "сантиметри": 0.01,
                "дюйми": 0.0254}


def units_factor(units: str, auto_factor: float) -> float:
    """Множник у метри для одиниць `units`; «авто» — те, що вгадано."""
    return auto_factor if units == "авто" else _UNIT_FACTOR[units]


class GeometryPanel(Panel):
    load_requested = QtCore.Signal()
    add_requested = QtCore.Signal()
    repair_requested = QtCore.Signal(str)
    transform_requested = QtCore.Signal(str, float)
    part_toggled = QtCore.Signal(int, bool)     # номер у списку, у сцені?
    part_removed = QtCore.Signal(int)
    part_solo = QtCore.Signal(int)              # подвійний клік: лишити саму цю
    # номер, роль, оберти, вісь, власний рівень згущення (0 — як усі)
    part_role = QtCore.Signal(int, str, float, str, int)
    part_hinge = QtCore.Signal(int, dict)   # номер, поля завіси
    hinge_guess = QtCore.Signal(int)        # «підбери завісу з геометрії»
    part_moved = QtCore.Signal(int, str, float)  # номер, вісь чи зсув, скільки
    part_role_query = QtCore.Signal(int)        # покажіть роль цього рядка
    # Двигун вибраної кришки: імʼя й, для сопла, його параметри.
    part_engine = QtCore.Signal(int, dict)
    part_flip = QtCore.Signal(int)              # перевернути нормалі кришки
    propeller_requested = QtCore.Signal()       # зробити гвинт із чисел
    disk_requested = QtCore.Signal()            # той самий гвинт, але диском
    part_disk = QtCore.Signal(int, dict)        # числа гвинта-диска

    def __init__(self, cfg, parent=None):
        super().__init__(cfg, parent)

        row = QtWidgets.QHBoxLayout()
        self.btn_open = QtWidgets.QPushButton("Відкрити STL…")
        self.btn_open.setToolTip(
            "Замінити сцену цією моделлю.\n"
            "Можна вибрати кілька файлів за раз: Ctrl або Shift у діалозі.")
        self.btn_open.clicked.connect(self.load_requested)
        row.addWidget(self.btn_open, 2)
        self.btn_add = QtWidgets.QPushButton("Додати…")
        self.btn_add.setToolTip("Додати моделі до сцени (можна кілька за раз), не прибираючи "
                                "наявні: крило + хвіст + антена рахуються разом")
        self.btn_add.clicked.connect(self.add_requested)
        row.addWidget(self.btn_add, 1)
        self.v.insertLayout(0, row)

        # --- ГВИНТ ІЗ ЧИСЕЛ ----------------------------------------------
        #
        # Зліпити гвинт руками легко неправильно, і помилка при цьому
        # мовчазна: зона MRF накриє криву фігуру цілком законно, тяги не
        # дасть, а помилки не буде. Тому — з чисел.
        self.box_prop = QtWidgets.QGroupBox("Зробити гвинт із чисел")
        self.box_prop.setCheckable(True)
        self.box_prop.setChecked(False)
        pv_ = QtWidgets.QFormLayout(self.box_prop)
        self.prop_d = dspin(0.01, 20.0, 0.30, 0.01, 4, " м")
        # Мінімум ДВІ, і це не примха. В однолопатевого гвинта немає
        # обертової симетрії, тож центр мас не лежить на осі — і
        # радіус зони виходив 97 мм замість 165. Такий гвинт існує
        # (з противагою), але робити його з чисел ми не вміємо.
        self.prop_n = ispin(2, 12, 2)
        self.prop_pitch = dspin(0.005, 20.0, 0.15, 0.01, 4, " м")
        self.prop_hub = dspin(0.0, 5.0, 0.03, 0.005, 4, " м")
        self.prop_chord = dspin(0.0, 5.0, 0.0, 0.005, 4, " м")
        self.prop_rpm = dspin(-100000.0, 100000.0, 6000.0, 100.0, 1, " об/хв")
        pv_.addRow("Діаметр", self.prop_d)
        pv_.addRow("Лопатей", self.prop_n)
        pv_.addRow("Крок", self.prop_pitch)
        pv_.addRow("Діаметр втулки", self.prop_hub)
        pv_.addRow("Хорда (0 — авто)", self.prop_chord)
        pv_.addRow("Оберти", self.prop_rpm)
        self.prop_note = WrapLabel("")
        self.prop_note.setWordWrap(True)
        self.prop_note.setStyleSheet("color:#7c8894; font-size:11px;")
        pv_.addRow(self.prop_note)
        self.btn_prop = QtWidgets.QPushButton("Додати гвинт у сцену")
        self.btn_prop.clicked.connect(self.propeller_requested)
        pv_.addRow(self.btn_prop)
        # Той самий гвинт, але БЕЗ ЛОПАТЕЙ: плаский диск того ж діаметра з
        # отвором під втулку. Сітці не треба розвʼязувати лопаті, тягу й
        # закрутку задають числа на ньому.
        self.btn_disk = QtWidgets.QPushButton("Додати гвинт-диск (без лопатей)")
        self.btn_disk.setToolTip(
            "Плаский диск із тим самим діаметром і втулкою. У сітку він не "
            "йде: це місце, де діє сила гвинта. Тягу й момент задайте на "
            "ньому в блоці ролі.")
        self.btn_disk.clicked.connect(self.disk_requested)
        pv_.addRow(self.btn_disk)
        pv_.addRow(hint(
            "Крок — це відстань, на яку гвинт угвинтився б за один оберт у "
            "твердому тілі. З нього рахується закрутка лопаті. "
            "ЧЕСНО ПРО МЕЖУ: переріз тут еліпс, а не профіль, тож числа з "
            "такого гвинта беріть як ПОРЯДОК. Для справжніх потрібна модель "
            "лопаті з CAD."))
        for w in (self.prop_d, self.prop_n, self.prop_pitch, self.prop_hub,
                  self.prop_chord):
            w.valueChanged.connect(lambda _: self.refresh_propeller())
        self.box_prop.toggled.connect(lambda _: self.refresh_propeller())
        self.v.insertWidget(1, self.box_prop)

        # Поки панель заповнює поля з моделі, сигнали мовчать: інакше
        # вибір іншої деталі переписував би її налаштування полями
        # попередньої, і зовні це виглядало б як «саме змінилося».
        self._muted = False
        self.parts = QtWidgets.QListWidget()
        self.parts.setMaximumHeight(120)
        self.parts.setToolTip(
            "Галочка — модель у сцені й у кейсі. Знята — файл лишається "
            "в списку, але в розрахунок не йде. Подвійний клік лишає в "
            "сцені саму цю модель.")
        self.parts.itemChanged.connect(self._part_changed)
        self.parts.itemDoubleClicked.connect(
            lambda it: self.part_solo.emit(self.parts.row(it)))
        self.parts.currentRowChanged.connect(self._part_selected)
        self._filling = False
        # Імена рядків і виділене — щоб пережити перезаповнення списку.
        # Див. show_parts: воно починається з clear(), а clear() знищує
        # виділення разом із рядками.
        self._part_names: list[str] = []      # тотожність: повні шляхи
        self._part_labels: list[str] = []     # підписи: базові імена
        self._sel_name: str | None = None
        self.v.insertWidget(2, self.parts)

        self.lbl_scene = WrapLabel("")
        self.lbl_scene.setStyleSheet("color:#7c8894; font-size:11px;")
        self.v.insertWidget(3, self.lbl_scene)

        self.btn_drop = QtWidgets.QPushButton("Прибрати зі списку")
        self.btn_drop.setToolTip("Забути вибрану модель. Сам файл лишається "
                                 "на диску.")
        self.btn_drop.clicked.connect(self._drop_current)
        self.v.insertWidget(4, self.btn_drop)

        # --- РОЛЬ І ОРІЄНТАЦІЯ ВИБРАНОЇ ЧАСТИНИ --------------------------
        #
        # Усе тут стосується ОДНІЄЇ моделі — тієї, що виділена в списку.
        # Тому блок і стоїть одразу під списком, а підпис називає її на
        # ім'я: інакше легко задати оберти не тому тілу й не помітити.
        self.box_role = QtWidgets.QWidget()
        rv = QtWidgets.QVBoxLayout(self.box_role)
        rv.setContentsMargins(0, 4, 0, 0)
        self.lbl_role_for = WrapLabel("")
        self.lbl_role_for.setStyleSheet("color:#9aa7b4; font-size:11px;")
        rv.addWidget(self.lbl_role_for)

        rf = QtWidgets.QFormLayout()
        rf.setContentsMargins(0, 0, 0, 0)
        self.cmb_role = QtWidgets.QComboBox()
        for key, label, why in fc.PART_ROLES:
            self.cmb_role.addItem(f"{label} — {why}", key)
        self.cmb_role.currentIndexChanged.connect(self._role_changed)
        rf.addRow("Це", self.cmb_role)
        self.role_rpm = dspin(-100000.0, 100000.0, 3000.0, 100.0, 1, " об/хв")
        self.role_rpm.valueChanged.connect(lambda _: self._role_changed())
        self.row_rpm = rf.rowCount()
        rf.addRow("Оберти", self.role_rpm)
        # ВІСЬ ЗАДАЄ ЛЮДИНА, і це не лінощі, а межа способу: плаский
        # двулопатевий гвинт габаритом НЕ ВІДРІЗНИТИ від пластини — він і
        # справді міг би крутитися навколо обох осей. Автовизначення давало
        # для нього Y замість X, мовчки й упевнено.
        self.role_axis = QtWidgets.QComboBox()
        for key, label in (("x", "X — уздовж потоку (звично для гвинта)"),
                           ("y", "Y — уздовж розмаху"),
                           ("z", "Z — угору (ротор гелікоптера)")):
            self.role_axis.addItem(label, key)
        self.role_axis.currentIndexChanged.connect(
            lambda _: self._role_changed())
        self.row_axis = rf.rowCount()
        rf.addRow("Вісь обертання", self.role_axis)

        # ВЛАСНЕ ЗГУЩЕННЯ ЦІЄЇ ДЕТАЛІ.
        #
        # Потрібне там, де дрібне стоїть поруч із великим. Лопать 8x4.5
        # має хорду близько 20 мм і товщину 2 — на спільній комірці 4.4 мм
        # від неї лишається пластинка в одну комірку завтовшки, тимчасом
        # як фюзеляж на тій самій комірці описаний добре. Підняти рівень
        # УСІМ означало б покласти ті самі 1.1 мм і на фюзеляж — це
        # десятки мільйонів комірок ні за що.
        #
        # Нуль означає «як усі», і саме він стоїть типово: поки його ніхто
        # не чіпає, сітка виходить точно та сама, що й була.
        self.role_level = ispin(0, 9, 0, 1)
        self.role_level.setSpecialValueText("як усі")
        self.role_level.setToolTip(
            "Скільки разів ділити комірку НА ЦІЙ поверхні. Кожен рівень "
            "ділить її вдвічі по кожному напрямку, тобто множить кількість "
            "комірок на поверхні вчетверо. «Як усі» — спільний рівень із "
            "вкладки «Сітка».")
        self.role_level.valueChanged.connect(lambda _: self._role_changed())
        self.row_level = rf.rowCount()
        rf.addRow("Згущення поверхні", self.role_level)
        # ДВИГУН — лише для кришок. Імʼя звʼязує повітрозабірник із соплом:
        # кришки з однаковим імʼям — один двигун.
        self.eng_name = QtWidgets.QLineEdit("engine1")
        self.eng_name.setToolTip(
            "Кришки з однаковим імʼям двигуна — один двигун. Два "
            "повітрозабірники й одне сопло з імʼям engine1 — це один "
            "двигун із двома бічними входами.")
        self.eng_name.editingFinished.connect(self._engine_changed)
        self.row_engine = rf.rowCount()
        rf.addRow("Двигун", self.eng_name)
        explain_tip(self.eng_name, "engine_name")
        # Активний бік — куди дивляться нормалі STL. Стрілка у вʼюпорті
        # показує його; ця кнопка міняє бік, не вертаючись у CAD.
        self.btn_flip = QtWidgets.QPushButton("Перевернути активний бік")
        self.btn_flip.setToolTip(
            "Сопло дме, а вхід смокче з того боку, куди показує стрілка у "
            "вʼюпорті. Кнопка перевертає нормалі цієї поверхні.")
        self.btn_flip.clicked.connect(self._flip_current)
        self.row_flip = rf.rowCount()
        rf.addRow("", self.btn_flip)
        rv.addLayout(rf)
        self.form_role = rf

        # Параметри двигуна. Тримає їх СОПЛО: воно в двигуна одне, а
        # повітрозабірників буває кілька, і числа в них не мають
        # розходитися.
        self.box_jet = QtWidgets.QWidget()
        jf = QtWidgets.QFormLayout(self.box_jet)
        jf.setContentsMargins(0, 0, 0, 0)
        jf.setRowWrapPolicy(QtWidgets.QFormLayout.WrapLongRows)
        self.jet_kind = QtWidgets.QComboBox()
        for key, label, why in fc.ENGINE_KINDS:
            self.jet_kind.addItem(f"{label} — {why}", key)
        self.jet_kind.currentIndexChanged.connect(self._engine_changed)
        jf.addRow("Вид", self.jet_kind)
        _b = explain(self.jet_kind, "engine_kind")
        if _b: jf.addRow(_b)
        self.jet_mode = QtWidgets.QComboBox()
        for key, label, why in fc.THRUST_MODES:
            self.jet_mode.addItem(f"{label} — {why}", key)
        self.jet_mode.currentIndexChanged.connect(self._engine_changed)
        jf.addRow("Тяга задана як", self.jet_mode)
        _b = explain(self.jet_mode, "engine_thrust_mode")
        if _b: jf.addRow(_b)
        self.jet_thrust = dspin(0.0, 100000.0, 20.0, 1.0, 2, " Н")
        self.jet_thrust.valueChanged.connect(lambda _: self._engine_changed())
        jf.addRow("Тяга", self.jet_thrust)
        _b = explain(self.jet_thrust, "engine_thrust")
        if _b: jf.addRow(_b)
        self.jet_vjet = dspin(0.0, 3000.0, 0.0, 10.0, 0, " м/с")
        self.jet_vjet.setSpecialValueText("з площі сопла")
        self.jet_vjet.valueChanged.connect(lambda _: self._engine_changed())
        jf.addRow("Швидкість вихлопу", self.jet_vjet)
        _b = explain(self.jet_vjet, "engine_vjet")
        if _b: jf.addRow(_b)
        self.jet_temp = dspin(-50.0, 2000.0, 627.0, 10.0, 0, " °C")
        self.jet_temp.valueChanged.connect(lambda _: self._engine_changed())
        self.row_jet_temp = jf.rowCount()
        jf.addRow("Температура струменя", self.jet_temp)
        explain_tip(self.jet_temp, "engine_tjet")
        self.jet_fuel = dspin(0.0, 10.0, 0.0, 0.5, 1, " %")
        self.jet_fuel.valueChanged.connect(lambda _: self._engine_changed())
        jf.addRow("Частка палива", self.jet_fuel)
        _b = explain(self.jet_fuel, "engine_fuel")
        if _b: jf.addRow(_b)
        self.jet_throttle = QtWidgets.QCheckBox("від газу на пульті (тяга ∝ газ²)")
        self.jet_throttle.setChecked(True)
        self.jet_throttle.toggled.connect(lambda _: self._engine_changed())
        jf.addRow(self.jet_throttle)
        _b = explain(self.jet_throttle, "engine_throttle")
        if _b: jf.addRow(_b)
        self.jet_refine = QtWidgets.QCheckBox("згущувати сітку на кришках")
        self.jet_refine.setChecked(True)
        self.jet_refine.toggled.connect(lambda _: self._engine_changed())
        jf.addRow(self.jet_refine)
        _b = explain(self.jet_refine, "engine_refine")
        if _b: jf.addRow(_b)
        self.jet_direct = QtWidgets.QCheckBox(
            "задати витрату й швидкість напряму")
        self.jet_direct.toggled.connect(lambda _: self._engine_changed())
        jf.addRow(self.jet_direct)
        _b = explain(self.jet_direct, "engine_direct")
        if _b: jf.addRow(_b)
        self.jet_mdot = dspin(0.0, 1000.0, 0.5, 0.01, 4, " кг/с")
        self.jet_mdot.valueChanged.connect(lambda _: self._engine_changed())
        self.row_jet_mdot = jf.rowCount()
        jf.addRow("Витрата крізь сопло", self.jet_mdot)
        self.jet_vexit = dspin(0.0, 3000.0, 50.0, 1.0, 1, " м/с")
        self.jet_vexit.valueChanged.connect(lambda _: self._engine_changed())
        self.row_jet_vexit = jf.rowCount()
        jf.addRow("Швидкість на зрізі", self.jet_vexit)
        self.form_jet = jf
        self.box_jet.setVisible(False)

        # ГВИНТ-ДИСК: тяга й закрутка числами. Вісь і бік обертання беруться
        # з полів вище («Вісь обертання», знак «Обертів»).
        self.box_disk = QtWidgets.QWidget()
        df = QtWidgets.QFormLayout(self.box_disk)
        df.setContentsMargins(0, 0, 0, 0)
        df.setRowWrapPolicy(QtWidgets.QFormLayout.WrapLongRows)
        self.disk_thrust = dspin(0.0, 100000.0, 5.0, 0.5, 2, " Н")
        self.disk_thrust.valueChanged.connect(lambda _: self._disk_changed())
        df.addRow("Тяга", self.disk_thrust)
        _b = explain(self.disk_thrust, "disk_thrust")
        if _b: df.addRow(_b)
        self.disk_torque = dspin(0.0, 10000.0, 0.0, 0.01, 3, " Н·м")
        self.disk_torque.valueChanged.connect(lambda _: self._disk_changed())
        df.addRow("Крутний момент", self.disk_torque)
        _b = explain(self.disk_torque, "disk_torque")
        if _b: df.addRow(_b)
        self.disk_profile = QtWidgets.QComboBox()
        for key, label, why in fc.DISK_PROFILES:
            self.disk_profile.addItem(f"{label} — {why}", key)
        self.disk_profile.currentIndexChanged.connect(self._disk_changed)
        df.addRow("Розподіл", self.disk_profile)
        _b = explain(self.disk_profile, "disk_profile")
        if _b: df.addRow(_b)
        self.disk_hub = dspin(0.0, 5000.0, 0.0, 1.0, 1, " мм")
        self.disk_hub.setSpecialValueText("15 % радіуса")
        self.disk_hub.valueChanged.connect(lambda _: self._disk_changed())
        df.addRow("Радіус кока", self.disk_hub)
        explain_tip(self.disk_hub, "disk_hub")
        self.disk_throttle = QtWidgets.QCheckBox("від газу на пульті (тяга ∝ газ²)")
        self.disk_throttle.setChecked(True)
        self.disk_throttle.toggled.connect(lambda _: self._disk_changed())
        df.addRow(self.disk_throttle)
        self.disk_refine = QtWidgets.QCheckBox("згущувати сітку в диску")
        self.disk_refine.setChecked(True)
        self.disk_refine.toggled.connect(lambda _: self._disk_changed())
        df.addRow(self.disk_refine)
        _b = explain(self.disk_refine, "disk_refine")
        if _b: df.addRow(_b)
        self.box_disk.setVisible(False)
        rv.addWidget(self.box_disk)
        self._set_form_row(rf, self.row_engine, False)
        self._set_form_row(rf, self.row_flip, False)
        rv.addWidget(self.box_jet)

        self.lbl_level = WrapLabel("")
        self.lbl_level.setStyleSheet("color:#7c8894; font-size:11px;")
        self.lbl_level.setVisible(False)
        rv.addWidget(self.lbl_level)

        self.lbl_role = WrapLabel("")
        self.lbl_role.setStyleSheet("color:#7c8894; font-size:11px;")
        rv.addWidget(self.lbl_role)

        # --- КЕРУВАННЯ ЦІЄЮ ДЕТАЛЛЮ -------------------------------------
        #
        # Завіса — це ПРЯМА, а не точка: елерон крутиться навколо своєї
        # передньої кромки. Якщо задати поворот навколо центра деталі,
        # вона сповзе з гнізда — передня кромка підніметься над крилом,
        # задня втопиться в обшивці, і в сітці зʼявиться дірка.
        self.box_ctl = QtWidgets.QGroupBox("Керування (пульт)")
        self.box_ctl.setCheckable(True)
        self.box_ctl.setChecked(False)
        cf = QtWidgets.QFormLayout(self.box_ctl)
        cf.setContentsMargins(8, 6, 8, 6)
        # Трійки X/Y/Z разом із підписом «Напрямок завіси» хотіли 508 px
        # при видимих ~430: панель «Геометрія» отримувала горизонтальну
        # прокрутку, щойно вибрано будь-яку деталь. WrapLongRows кладе поле
        # ПІД підпис, коли поруч воно не влазить.
        cf.setRowWrapPolicy(QtWidgets.QFormLayout.WrapLongRows)

        self.ctl_channel = QtWidgets.QComboBox()
        self.ctl_channel.addItem("— не керована —", "")
        # Змінну НЕ звати `hint`: так зветься модульний помічник, яким
        # цей самий __init__ користується вище, і локальне ім'я зробило б
        # його «використаним до присвоєння» по всій функції.
        for key, label, _, _, _, what in fc.RC_CHANNELS:
            self.ctl_channel.addItem(f"{label} — {what}" if what else label,
                                     key)
        cf.addRow("Канал", self.ctl_channel)

        self.btn_hinge_guess = QtWidgets.QPushButton(
            "Підібрати завісу з геометрії")
        self.btn_hinge_guess.setToolTip(
            "Поставити вісь уздовж найдовшого напрямку деталі, а точку — "
            "на середину її передньої кромки. Для елерона й керма це "
            "майже завжди те, що треба; перевірте у вювпорті.")
        cf.addRow(self.btn_hinge_guess)

        self.ctl_pivot = []
        prow = QtWidgets.QHBoxLayout()
        for ax in "xyz":
            sp = dspin(-1000.0, 1000.0, 0.0, 0.005, 4, " м")
            sp.setToolTip(f"{ax.upper()} точки, через яку проходить завіса")
            self.ctl_pivot.append(sp)
            prow.addWidget(QtWidgets.QLabel(ax.upper()))
            prow.addWidget(sp, 1)
        cf.addRow("Точка завіси", _wrap(prow))

        self.ctl_axis = []
        arow = QtWidgets.QHBoxLayout()
        for i, ax in enumerate("xyz"):
            sp = dspin(-1.0, 1.0, 1.0 if i == 1 else 0.0, 0.1, 3, "")
            sp.setToolTip(f"{ax.upper()} напрямку завіси (довжина не важить)")
            self.ctl_axis.append(sp)
            arow.addWidget(QtWidgets.QLabel(ax.upper()))
            arow.addWidget(sp, 1)
        cf.addRow("Напрямок завіси", _wrap(arow))

        self.ctl_offset = []
        orow = QtWidgets.QHBoxLayout()
        for ax in "xyz":
            sp = dspin(-10.0, 10.0, 0.0, 0.001, 4, " м")
            sp.setToolTip(
                "Поправка ПІСЛЯ повороту — дослати деталь на місце, якщо "
                "в моделі вона знята з вузла")
            self.ctl_offset.append(sp)
            orow.addWidget(QtWidgets.QLabel(ax.upper()))
            orow.addWidget(sp, 1)
        cf.addRow("Поправка", _wrap(orow))

        grow = QtWidgets.QHBoxLayout()
        self.ctl_gain = dspin(-90.0, 90.0, 20.0, 1.0, 1, "°")
        self.ctl_gain.setToolTip("Скільки градусів дає повний хід стіка")
        self.ctl_trim = dspin(-90.0, 90.0, 0.0, 0.5, 1, "°")
        self.ctl_trim.setToolTip("Відхилення на нейтралі")
        self.ctl_limit = dspin(0.0, 90.0, 30.0, 1.0, 1, "°")
        self.ctl_limit.setToolTip("Обмежувач: далі поверхня не піде")
        for lab, w in (("хід", self.ctl_gain), ("трим", self.ctl_trim),
                       ("межа", self.ctl_limit)):
            grow.addWidget(QtWidgets.QLabel(lab))
            grow.addWidget(w, 1)
        cf.addRow("Кути", _wrap(grow))
        # По три поля в рядку: з типовими 96 px на поле рядок хотів 391 px,
        # і група розпирала панель. 72 px досить для «-0,0000 м».
        for sp in (self.ctl_pivot + self.ctl_axis + self.ctl_offset
                   + [self.ctl_gain, self.ctl_trim, self.ctl_limit]):
            sp.setMinimumWidth(72)

        self.ctl_reverse = QtWidgets.QCheckBox(
            "Реверс (для другого елерона — щоб ходили назустріч)")
        cf.addRow(self.ctl_reverse)

        self.lbl_ctl = WrapLabel("")
        self.lbl_ctl.setStyleSheet("color:#7c8894; font-size:11px;")
        cf.addRow(self.lbl_ctl)

        for w in ([self.ctl_channel, self.ctl_gain, self.ctl_trim,
                   self.ctl_limit, self.ctl_reverse, self.box_ctl]
                  + self.ctl_pivot + self.ctl_axis + self.ctl_offset):
            sig = (w.toggled if isinstance(w, (QtWidgets.QCheckBox,
                                               QtWidgets.QGroupBox))
                   else w.currentIndexChanged
                   if isinstance(w, QtWidgets.QComboBox) else w.valueChanged)
            sig.connect(self._ctl_changed)
        self.btn_hinge_guess.clicked.connect(
            lambda: self.hinge_guess.emit(self.parts.currentRow()))
        rv.addWidget(self.box_ctl)

        # Орієнтація ОКРЕМОЇ моделі. Кнопки вгорі крутять усю збірку
        # навколо спільного центра — це потрібно, щоб літак дивився вздовж
        # потоку. А тут модель крутиться навколо ВЛАСНОГО центра: так
        # ставлять гвинт на вал і елерон у своє гніздо, не зрушивши решти.
        # ОКРЕМА ДЕТАЛЬ — ПІД ЗАМКОМ.
        #
        # Раніше тут стояв ряд кнопок «↻ X 90°», точнісінько такий, як у
        # блоці «Орієнтація» нижче, за десять сантиметрів. Різнилися вони
        # лише словом у заголовку — і переплутати їх було не помилкою
        # людини, а вадою панелі.
        #
        # Ціна плутанини нерівна. Деталі зазвичай приходять із CAD уже
        # складеними, і поворот ОДНІЄЇ з них ламає складання: решта
        # лишається як була. Тому цей блок тепер згорнутий і названий
        # тим, що він робить.
        self.box_one = QtWidgets.QGroupBox(
            "Посунути ОКРЕМУ деталь (ламає складання з CAD)")
        self.box_one.setCheckable(True)
        self.box_one.setChecked(False)
        self.box_one.setToolTip(
            "Потрібне лише тоді, коли деталь приїхала окремо й не на "
            "місці — наприклад, гвинт, який треба поставити на вал.\n"
            "Щоб розвернути ВЕСЬ літак, користуйтеся блоком «Орієнтація "
            "всієї збірки» нижче.")
        ov = QtWidgets.QVBoxLayout(self.box_one)
        ov.setContentsMargins(8, 4, 8, 6)
        grow = QtWidgets.QHBoxLayout()
        for axis in ("x", "y", "z"):
            b = QtWidgets.QPushButton(f"↻ {axis.upper()} 90° (лише ця)")
            b.setToolTip(f"Повернути ЛИШЕ вибрану деталь на 90° навколо "
                         f"{axis.upper()}, навколо її власного центра. "
                         f"Решта збірки лишиться як була.")
            b.clicked.connect(
                lambda _=False, a=axis: self._move_current(a, 90.0))
            grow.addWidget(b)
        ov.addLayout(grow)
        rv = ov

        mrow = QtWidgets.QHBoxLayout()
        self.move_step = dspin(0.0001, 100.0, 0.01, 0.005, 4, " м")
        self.move_step.setToolTip("На скільки зсувати за одне натискання")
        mrow.addWidget(QtWidgets.QLabel("Крок"))
        mrow.addWidget(self.move_step, 1)
        for label, key in (("−X", "-x"), ("+X", "+x"), ("−Y", "-y"),
                           ("+Y", "+y"), ("−Z", "-z"), ("+Z", "+z")):
            b = QtWidgets.QPushButton(label)
            b.setFixedWidth(34)
            b.setToolTip(f"Зсунути ЦЮ модель по {key[1].upper()}")
            b.clicked.connect(
                lambda _=False, k=key: self._move_current(
                    k, self.move_step.value()))
            mrow.addWidget(b)
        rv.addLayout(mrow)
        self.box_role.layout().addWidget(self.box_one)
        self.v.insertWidget(5, self.box_role)
        self.box_role.setVisible(False)

        self.lbl_file = WrapLabel("файл не завантажено")
        self.lbl_file.setWordWrap(True)
        self.lbl_file.setStyleSheet("color:#555; font-size:11px;")
        # Індекси тут зсунулися на одиницю, коли під списком зʼявився блок
        # ролі: box_role стоїть на 4, тож усе, що було нижче, посунулося.
        self.v.insertWidget(6, self.lbl_file)

        self.chk_split = QtWidgets.QCheckBox("Кожна модель — окремий патч")
        self.chk_split.setToolTip(
            "Крило, фюзеляж і мотогондола стануть окремими границями, і "
            "сили порахуються ДЛЯ КОЖНОЇ ОКРЕМО, а не лише сумарно. "
            "Працює, коли в сцені більше однієї моделі.")
        self.chk_split.setChecked(cfg.split_patches)
        self.chk_split.toggled.connect(self._split_changed)
        self.v.insertWidget(7, self.chk_split)
        _b = explain(self.chk_split, "split_patches")
        if _b:
            self.v.insertWidget(8, _b)
        self.lbl_split = WrapLabel("")
        self.lbl_split.setWordWrap(True)
        self.lbl_split.setStyleSheet("color:#7c8894; font-size:11px;")
        # Індекс рахується від уже вставленого блоку пояснення, тому не
        # фіксоване число: у режимі новачка він є, без нього — ні.
        self.v.insertWidget(self.v.indexOf(self.chk_split) + (2 if _b else 1),
                            self.lbl_split)

        self.cmb_units = QtWidgets.QComboBox()
        self.cmb_units.addItems(list(UNITS))
        # ТИПОВО МІЛІМЕТРИ, а не «авто».
        #
        # «Авто» вгадує за габаритом і майже завжди має рацію — але
        # «майже» тут і є бідою: у дрібної деталі в метрах габарит такий
        # самий, як у великої в міліметрах, і помилка виходить у тисячу
        # разів, мовчки. Хто знає, в чому в нього креслення, той нехай і
        # каже; «авто» лишається в списку для тих, хто не знає.
        self.cmb_units.setCurrentText(DEFAULT_UNITS)
        self.cmb_units.currentTextChanged.connect(lambda _: self.emit_changed())
        self.form.addRow("Одиниці STL", self.cmb_units)
        _b = explain(self.cmb_units, "units")
        if _b: self.form.addRow(_b)
        self.v.addWidget(hint(
            "STL не зберігає одиниць, а OpenFOAM працює в метрах. "
            "«Авто» вгадує за габаритом.", "units"))

        self.v.addWidget(section("Орієнтація всієї збірки"))
        rot = QtWidgets.QHBoxLayout()
        for axis in ("x", "y", "z"):
            # Коротко: «усе разом» вже сказано заголовком секції, а з ним
            # три кнопки в рядку хотіли 389 px — «Геометрія» на 2 px не
            # влазила в стовпчик і прокручувалась убік.
            b = QtWidgets.QPushButton(f"↻ {axis.upper()} 90°")
            b.setToolTip(f"Повернути на 90° навколо {axis.upper()} УСІ "
                         f"деталі одразу, навколо спільного центра. "
                         f"Складання з CAD при цьому не ламається.")
            b.clicked.connect(lambda _=False, a=axis: self.transform_requested.emit(a, 90.0))
            rot.addWidget(b)
        self.v.addLayout(rot)
        _b = explain(None, "rotate")
        if _b: self.v.addWidget(_b)
        self.v.addWidget(hint(
            "Потік іде вздовж +X, розмах — по Y, вгору — Z. "
            "Розверніть модель, якщо вона експортована інакше.\n"
            "Крутить УСІ деталі разом навколо спільного центра — і ті, що "
            "зараз без галочки, теж, щоб складання не розʼїхалося.",
            "rotate"))

        pos = QtWidgets.QHBoxLayout()
        for label, key in (("Центрувати", "centre"), ("Посадити на z=0", "ground")):
            b = QtWidgets.QPushButton(label)
            b.clicked.connect(lambda _=False, k=key: self.transform_requested.emit(k, 0.0))
            pos.addWidget(b)
        self.v.addLayout(pos)

        self.v.addWidget(section("Діагностика"))
        self.table = QtWidgets.QTableWidget(0, 2)
        self.table.horizontalHeader().setVisible(False)
        self.table.verticalHeader().setVisible(False)
        self.table.setEditTriggers(QtWidgets.QAbstractItemView.NoEditTriggers)
        self.table.setSelectionMode(QtWidgets.QAbstractItemView.NoSelection)
        self.table.horizontalHeader().setSectionResizeMode(
            0, QtWidgets.QHeaderView.ResizeToContents)
        self.table.horizontalHeader().setSectionResizeMode(
            1, QtWidgets.QHeaderView.Stretch)
        self.table.setMinimumHeight(210)
        self.table.setShowGrid(False)
        self.v.addWidget(self.table)

        self.notes = QtWidgets.QTextBrowser()
        self.notes.setMaximumHeight(120)
        self.notes.setOpenExternalLinks(False)
        self.v.addWidget(self.notes)

        self.v.addWidget(section("Ремонт"))
        grid = QtWidgets.QGridLayout()
        repairs = [
            ("Полагодити все", "all",
             "Зшити вершини, узгодити нормалі, закрити дірки — одним "
             "натиском, у тому ж порядку, що й у слайсерах"),
            ("Полагодити як слайсер", "winfix",
             "Те саме, що кнопка «Fix model» у Bambu Studio, Orca й "
             "PrusaSlicer: служба Netfabb, вбудована у Windows. Закриває "
             "дірки, яких «Закрити дірки» не бачить, і при цьому не "
             "проріднює модель — на відміну від обгортання. Іде довго: "
             "близько пів хвилини на 250 тис. трикутників"),
            ("Обгорнути…", "wrap",
             "КРАЙНІЙ ЗАСІБ: перерізати модель наново по воксельній сітці. "
             "Оболонка виходить гарантовано замкнена, але дрібні деталі "
             "зникають, а тонкі кромки грубіють"),
            # The grid has 2 columns: this position puts the button directly
            # below «Полагодити як слайсер». It does the same repair steps
            # in our Rust code.
            ("meshfix", "meshfix",
             "Ті самі 12 кроків, що й «Полагодити як слайсер», але на Rust "
             "у самій програмі (rust/meshfix), без служби Windows: зшити "
             "щілини до 0.01 мм, прибрати дублі, узгодити нормалі, закрити "
             "всі дірки, обʼєднати тіла, що врізаються одне в одне, "
             "прибрати вкладені й крихітні оболонки. На відміну від служби "
             "не згущує сітку й не зсуває вершини."),
            ("Обгорнути (alpha wrap)…", "alphawrap",
             "Те саме обгортання, але поверхня ведеться по самій геометрії, "
             "а не по осях воксельної сітки — тому кромки лишаються "
             "кромками, а не драбинкою. Замкненість і 2-manifold тут "
             "гарантовані алгоритмом (CGAL Alpha Wrapping). Ціна — час і те, "
             "що тіло трохи роздувається; на скільки саме, вікно скаже "
             "заздалегідь"),
            ("Фаска на тонкому…", "bevel",
             "ЕКСПЕРИМЕНТ, і ось чому: зрізати смугу з трикутного супу й "
             "залатати — це не фаска, а шрам. На справжньому гвинті 8x4.5 "
             "три різні підходи дали гірше, ніж було: тонкого стало 26.6% "
             "площі замість 16.7%, а зріз пішов глибший за заданий поріг. "
             "Кнопка міряє себе сама й каже, чи стало краще; якщо ні — "
             "тисніть «Скинути». Справжня фаска робиться в CAD, де кромка "
             "параметрична, а не мільйон трикутників"),
            ("Зшити вершини", "clean", "Зливає дублі й прибирає вироджені трикутники"),
            ("Закрити дірки", "fill", "vtkFillHolesFilter — плоскі латки на дірки"),
            ("Виправити нормалі", "normals", "Узгодити напрямок і розвернути назовні"),
            ("Проріднити…", "decimate", "Менше трикутників — швидший пошук у snappy"),
            ("Згустити", "subdivide", "Розбити кожен трикутник — якщо STL надто грубий"),
            ("Скинути", "reset", "Повернутися до вихідного файлу"),
        ]
        for i, (label, key, tip) in enumerate(repairs):
            b = QtWidgets.QPushButton(label)
            b.setToolTip(tip)
            # Чужого інструмента може не бути (не Windows, немає пакунків
            # WinRT, не покладено wrapwrap). Сіра кнопка з поясненням у
            # підказці чесніша за живу, яка відповідає діалогом «нічого не
            # вийде». Обидва модулі дають однакові available()/why_not() —
            # саме для цього ця пара й заведена.
            gate = {"winfix": winfix, "alphawrap": alphawrap,
                    "meshfix": meshfix}.get(key)
            if gate is not None and not gate.available():
                b.setEnabled(False)
                b.setToolTip(gate.why_not())
            b.clicked.connect(lambda _=False, k=key: self.repair_requested.emit(k))
            grid.addWidget(b, i // 2, i % 2)
        self.v.addLayout(grid)
        self.v.addStretch(1)

    def load_cfg(self):
        """Відновлює те, що панель геометрії тримає в конфігу.

        Одиниці сюди не входять: їх ставить сама програма з множника моделі,
        а не з конфігу — там їх просто немає.
        """
        def apply():
            self.chk_split.setChecked(self.cfg.split_patches)

        self._loaded(apply)

    def _split_changed(self, on: bool):
        if self._loading:
            return
        self.cfg.split_patches = on
        self.emit_changed()

    def show_split(self, bodies, guard) -> None:
        """Показує, на які саме патчі розпалася сцена, і що з цим не так.

        Показати ІМЕНА важливо: вони не збігаються з назвами файлів (OpenFOAM
        приймає лише латиницю), і побачити «plane7.stl → plane7» треба тут, а
        не в журналі snappyHexMesh.
        """
        if not self.cfg.split_patches:
            self.lbl_split.setVisible(False)
            return
        if len(bodies) < 2:
            text = ("Поки в сцені одна модель, патч лишається один — "
                    "розділяти нема чого.")
            colour = "#7c8894"
        else:
            text = "Патчі: " + ", ".join(b.name for b in bodies)
            colour = "#7c8894"
        bad = [t for lvl, t in guard if lvl == "bad"]
        if bad:
            text += "<br>" + "<br>".join(bad)
            colour = "#e05252"
        self.lbl_split.setStyleSheet(f"color:{colour}; font-size:11px;")
        self.lbl_split.setText(text)
        self.lbl_split.setVisible(True)

    def _part_changed(self, item):
        if self._filling:
            return
        self.part_toggled.emit(self.parts.row(item),
                               item.checkState() == QtCore.Qt.Checked)

    def _part_selected(self, row: int):
        """Запамʼятати вибране ІМЕНЕМ, а не номером.

        Номер після прибирання рядка означає вже інший файл, а ім'я — той
        самий. Під час перезаповнення сигнал приходить із -1 від clear(),
        і його треба пропустити, інакше памʼять зітреться рівно тоді,
        коли вона потрібна.
        """
        if self._filling:
            return
        self._sel_name = (self._part_names[row]
                          if 0 <= row < len(self._part_names) else None)
        self._sync_drop_button()
        # Блок ролі стосується САМЕ вибраної моделі, тож при зміні вибору
        # його треба перепитати у вікна — інакше на екрані лишилася б роль
        # попередньої, і задати оберти можна було б не тому тілу.
        self.part_role_query.emit(row)

    def propeller_params(self) -> dict:
        """Числа гвинта з полів — одним місцем, щоб вікно й виріб не розійшлись."""
        return dict(diameter=self.prop_d.value(), blades=self.prop_n.value(),
                    pitch=self.prop_pitch.value(),
                    hub_diameter=self.prop_hub.value(),
                    chord=self.prop_chord.value(),
                    rpm=self.prop_rpm.value())

    def refresh_propeller(self):
        """Показати наслідки чисел ДО того, як гвинт зроблено.

        Головне тут — кут установки біля кореня. При великому кроці й
        малому радіусі він підходить до 90°, тобто лопать стає впоперек
        потоку: геометрія будується, а сенсу в ній немає.
        """
        if not self.box_prop.isChecked():
            self.prop_note.setText("")
            return
        import math
        d = self.prop_d.value()
        pitch = self.prop_pitch.value()
        hub = self.prop_hub.value() or d * 0.18
        r_root, r_tip = max(hub / 2.0, 1e-6), max(d / 2.0, 1e-6)
        b_root = math.degrees(math.atan2(pitch, 2 * math.pi * r_root))
        b_tip = math.degrees(math.atan2(pitch, 2 * math.pi * r_tip))
        text = (f"Кут установки: {b_root:.0f}° біля втулки, {b_tip:.0f}° на "
                f"кінці. Крок/діаметр = {pitch / max(d, 1e-9):.2f}.")
        if b_root > 80:
            text += ("  УВАГА: біля втулки лопать стає майже впоперек "
                     "потоку — крок завеликий для такої втулки.")
        self.prop_note.setText(text)

    def _show_level_cost(self, level) -> None:
        """У що власний рівень перетворюється насправді — у міліметрах.

        Саме число рівня ні про що не говорить: воно стає зрозумілим лише
        поруч із товщиною деталі, яку видно очима. А другим числом іде
        ціна, бо кожен рівень множить кількість граней на цій поверхні
        вчетверо, і дізнаватися про це з оцінки сітки постфактум пізно.
        """
        base = self.cfg.base_cell
        common = self.cfg.mesh.surface_level_max
        if base <= 0:
            self.lbl_level.setVisible(False)
            return
        if not level:
            self.lbl_level.setText(
                f"Спільний рівень {common} — комірка "
                f"{phys.fmt_len(base / 2 ** common)}.")
        else:
            own = int(level[1])
            cost = 4.0 ** (own - common)
            how = (f"у {cost:.0f} разів більше" if cost >= 1
                   else f"у {1 / cost:.0f} разів менше")
            self.lbl_level.setText(
                f"Рівень {own} — комірка {phys.fmt_len(base / 2 ** own)} "
                f"проти спільних {phys.fmt_len(base / 2 ** common)}. "
                f"Граней на цій поверхні {how}.")
        self.lbl_level.setVisible(True)

    def _role_changed(self, *_):
        """Роль або оберти вибраної частини змінилися."""
        if self._filling:
            return
        row = self.parts.currentRow()
        if not (0 <= row < len(self._part_names)):
            return
        self.part_role.emit(row, self.cmb_role.currentData() or "body",
                            self.role_rpm.value(),
                            self.role_axis.currentData() or "x",
                            self.role_level.value())

    def _disk_changed(self, *_):
        if self._filling:
            return
        row = self.parts.currentRow()
        if not (0 <= row < len(self._part_names)):
            return
        self.part_disk.emit(row, {
            "thrust": self.disk_thrust.value(),
            "torque": self.disk_torque.value(),
            "profile": self.disk_profile.currentData() or "realistic",
            "hub": self.disk_hub.value() / 1000.0,
            "from_throttle": self.disk_throttle.isChecked(),
            "refine": self.disk_refine.isChecked(),
        })

    def show_part_disk(self, role: str, disk) -> None:
        """Заповнити поля гвинта-диска, нічого не випромінюючи."""
        self._filling = True
        try:
            self.box_disk.setVisible(role == fc.DISK_ROLE)
            if disk is not None:
                self.disk_thrust.setValue(float(disk.thrust))
                self.disk_torque.setValue(float(disk.torque))
                i = self.disk_profile.findData(disk.profile)
                self.disk_profile.setCurrentIndex(max(i, 0))
                self.disk_hub.setValue(float(disk.hub) * 1000.0)
                self.disk_throttle.setChecked(bool(disk.from_throttle))
                self.disk_refine.setChecked(bool(disk.refine))
        finally:
            self._filling = False

    def _flip_current(self):
        row = self.parts.currentRow()
        if 0 <= row < len(self._part_names):
            self.part_flip.emit(row)

    def _engine_changed(self, *_):
        """Імʼя двигуна чи параметри сопла змінилися."""
        if self._filling:
            return
        row = self.parts.currentRow()
        if not (0 <= row < len(self._part_names)):
            return
        self._show_jet_rows()
        self.part_engine.emit(row, {
            "engine": self.eng_name.text().strip() or "engine1",
            "kind": self.jet_kind.currentData() or "edf",
            "thrust_mode": self.jet_mode.currentData() or "static",
            "thrust": self.jet_thrust.value(),
            "v_jet": self.jet_vjet.value(),
            "t_jet": self.jet_temp.value() + 273.15,
            "fuel_frac": self.jet_fuel.value() / 100.0,
            "from_throttle": self.jet_throttle.isChecked(),
            "refine": self.jet_refine.isChecked(),
            "direct": self.jet_direct.isChecked(),
            "mdot": self.jet_mdot.value(),
            "v_exit": self.jet_vexit.value(),
        })

    def _set_form_row(self, form, row: int, visible: bool) -> None:
        for role in (QtWidgets.QFormLayout.LabelRole,
                     QtWidgets.QFormLayout.FieldRole):
            item = form.itemAt(row, role)
            if item is not None and item.widget() is not None:
                item.widget().setVisible(visible)

    def _show_jet_rows(self) -> None:
        """Показати лише ті поля сопла, що мають сенс за поточного вибору."""
        hot = self.jet_kind.currentData() == "turbojet"
        direct = self.jet_direct.isChecked()
        self._set_form_row(self.form_jet, self.row_jet_temp, hot)
        self._set_form_row(self.form_jet, self.row_jet_mdot, direct)
        self._set_form_row(self.form_jet, self.row_jet_vexit, direct)
        self.jet_thrust.setEnabled(not direct)

    def show_part_engine(self, role: str, engine: str, jet) -> None:
        """Заповнити поля двигуна для вибраної кришки, нічого не випромінюючи."""
        self._filling = True
        try:
            port = role in fc.PORT_ROLES
            self._set_form_row(self.form_role, self.row_engine, port)
            self._set_form_row(self.form_role, self.row_flip, port)
            self.box_jet.setVisible(role == "nozzle")
            self.eng_name.setText(engine or "engine1")
            if jet is not None:
                i = self.jet_kind.findData(jet.kind)
                self.jet_kind.setCurrentIndex(max(i, 0))
                i = self.jet_mode.findData(jet.thrust_mode)
                self.jet_mode.setCurrentIndex(max(i, 0))
                self.jet_thrust.setValue(float(jet.thrust))
                self.jet_vjet.setValue(float(jet.v_jet))
                self.jet_temp.setValue(float(jet.t_jet) - 273.15)
                self.jet_fuel.setValue(float(jet.fuel_frac) * 100.0)
                self.jet_throttle.setChecked(bool(jet.from_throttle))
                self.jet_refine.setChecked(bool(jet.refine))
                self.jet_direct.setChecked(bool(jet.direct))
                self.jet_mdot.setValue(float(jet.mdot))
                self.jet_vexit.setValue(float(jet.v_exit))
            self._show_jet_rows()
        finally:
            self._filling = False

    def _move_current(self, key: str, value: float):
        """Повернути чи зсунути ВИБРАНУ модель, не чіпаючи решти."""
        row = self.parts.currentRow()
        if 0 <= row < len(self._part_names):
            self.part_moved.emit(row, key, value)

    def _ctl_changed(self, *_):
        """Зібрати поля завіси й віддати нагору."""
        if self._muted:
            return
        row = self.parts.currentRow()
        if row < 0:
            return
        self.part_hinge.emit(row, {
            "on": self.box_ctl.isChecked(),
            "channel": self.ctl_channel.currentData() or "",
            "pivot": tuple(s.value() for s in self.ctl_pivot),
            "axis": tuple(s.value() for s in self.ctl_axis),
            "offset": tuple(s.value() for s in self.ctl_offset),
            "gain": self.ctl_gain.value(),
            "trim": self.ctl_trim.value(),
            "limit": self.ctl_limit.value(),
            "reverse": self.ctl_reverse.isChecked(),
        })

    def show_part_hinge(self, hinge, note: str = ""):
        """Показати завісу вибраної деталі, нічого не випромінюючи.

        `_muted` тут не косметика: без нього заповнення полів саме
        викликало б `_ctl_changed`, той послав би сигнал назад у програму,
        і вибір іншої деталі мовчки переписував би її завісу полями
        попередньої.
        """
        self._muted = True
        try:
            self.box_ctl.setChecked(hinge is not None)
            if hinge is not None:
                i = self.ctl_channel.findData(hinge.channel or "")
                self.ctl_channel.setCurrentIndex(max(i, 0))
                for s, v in zip(self.ctl_pivot, hinge.pivot):
                    s.setValue(float(v))
                for s, v in zip(self.ctl_axis, hinge.axis):
                    s.setValue(float(v))
                for s, v in zip(self.ctl_offset, hinge.offset):
                    s.setValue(float(v))
                self.ctl_gain.setValue(float(hinge.gain))
                self.ctl_trim.setValue(float(hinge.trim))
                self.ctl_limit.setValue(float(hinge.limit))
                self.ctl_reverse.setChecked(bool(hinge.reverse))
            self.lbl_ctl.setText(note)
        finally:
            self._muted = False

    def show_part_role(self, label: str, role: str, rpm: float,
                       note: str = "", axis: str = "x",
                       level: tuple[int, int] | None = None) -> None:
        """Показати роль вибраної частини. Порожній підпис — блок ховається.

        Сигнали на час підстановки глушимо тим самим прапорцем `_filling`,
        що й список: інакше показ чужої ролі негайно ж її й перезаписав би
        назад у модель.
        """
        self._filling = True
        try:
            if not label:
                self.box_role.setVisible(False)
                return
            self.lbl_role_for.setText(f"Для моделі: {label}")
            idx = self.cmb_role.findData(role)
            self.cmb_role.setCurrentIndex(max(idx, 0))
            self.role_rpm.setValue(rpm)
            ai = self.role_axis.findData(axis)
            self.role_axis.setCurrentIndex(max(ai, 0))
            self.role_level.setValue(0 if not level else int(level[1]))
            self._show_level_cost(level)
            # Гвинту-диску теж потрібні вісь і бік обертання (знак обертів):
            # з них ідуть напрямок тяги й закрутки.
            is_rotor = role in ("rotor", fc.DISK_ROLE)
            for widget, row in ((self.role_rpm, self.row_rpm),
                                (self.role_axis, self.row_axis)):
                widget.setVisible(is_rotor)
                lab = self.form_role.itemAt(
                    row, QtWidgets.QFormLayout.LabelRole)
                if lab is not None and lab.widget() is not None:
                    lab.widget().setVisible(is_rotor)
            # Гвинт-диск не поверхня: від нього в сітку не йде нічого, і
            # згущувати «його поверхню» нема чого — для нього своя галочка.
            self._set_form_row(self.form_role, self.row_level,
                               role != fc.DISK_ROLE)
            self.lbl_role.setText(note)
            self.lbl_role.setVisible(bool(note))
            self.box_role.setVisible(True)
        finally:
            self._filling = False

    def _sync_drop_button(self):
        """Кнопка має називати те, що прибере, і гаснути, коли нема чого.

        Доти вона була ввімкнена завжди, коли в списку є хоч щось, а
        прибирала за `currentRow()`. Після кожного перезаповнення
        `currentRow()` дорівнював -1, і натискання не робило НІЧОГО —
        мовчки, без жодного знаку. Саме на це й поскаржилися.
        """
        row = self.parts.currentRow()
        ok = 0 <= row < len(self._part_names)
        self.btn_drop.setEnabled(ok)
        if ok:
            # У підписі — коротке ім'я, у підказці — повний шлях: два
            # файли можуть зватися однаково, і тоді саме шлях відповідає
            # на питання «а котрий із них».
            label = (self._part_labels[row] if row < len(self._part_labels)
                     else self._part_names[row])
            self.btn_drop.setText(f"Прибрати зі списку: {label}")
            self.btn_drop.setToolTip(
                f"Забути цю модель. Сам файл лишається на диску."
                f"\n{self._part_names[row]}")
        else:
            self.btn_drop.setText("Прибрати зі списку")
            self.btn_drop.setToolTip(
                "Спершу виберіть рядок у списку — кнопка прибере саме його. "
                "Сам файл лишається на диску.")

    def _drop_current(self):
        row = self.parts.currentRow()
        if row < 0:
            # Сюди вже не мало б доходити: кнопка при порожньому виборі
            # вимкнена. Але як дійде — хай скаже, а не змовчить.
            self.btn_drop.setToolTip("Немає вибраного рядка.")
            return
        self.part_removed.emit(row)

    def show_parts(self, rows: list[dict], summary: str = ""):
        """Список моделей. Галочка = модель у сцені.

        Один список на дві потреби: те, що зараз у сцені, і те, що відкривали
        раніше. Знята галочка лишає рядок історією — файл памʼятається, але
        не читається й у кейс не йде.
        """
        # Що було вибране ДО перезаповнення. Беремо не номер, а ім'я:
        # після прибирання рядка номер означає вже інший файл.
        prev_name = self._sel_name
        prev_row = self.parts.currentRow()
        # Тотожність рядка — ПОВНИЙ ШЛЯХ, а не базове ім'я: два різні
        # файли легко звуться однаково, і на імені виділення перескакувало
        # на чужий рядок. Ім'я лишається для підпису кнопки.
        self._part_names = [r.get("key") or r["name"] for r in rows]
        self._part_labels = [r["name"] for r in rows]

        self._filling = True        # setCheckState сам смикає itemChanged
        try:
            self.parts.clear()
            for r in rows:
                it = QtWidgets.QListWidgetItem(
                    f"{r['name']}   {r['info']}" if r["info"] else r["name"])
                it.setFlags(it.flags() | QtCore.Qt.ItemIsUserCheckable)
                it.setCheckState(QtCore.Qt.Checked if r["enabled"]
                                 else QtCore.Qt.Unchecked)
                it.setToolTip(r["tip"])
                colour = LEVEL_COLOR.get(r["level"])
                if not r["enabled"]:
                    it.setForeground(QtGui.QColor("#777"))
                elif colour and r["level"] != "ok":
                    it.setForeground(QtGui.QColor(colour))
                self.parts.addItem(it)
            # Повертаємо виділення, поки прапорець ще стоїть: інакше
            # setCurrentRow смикне currentRowChanged і памʼять зітреться.
            #
            # Три випадки, і кожен потрібен:
            #   той самий файл на місці   -> вибрати його, хай навіть
            #                                номер змінився;
            #   файл щойно прибрали       -> вибрати той, що став на його
            #                                місце, щоб прибирати далі
            #                                можна було поспіль;
            #   вибору не було взагалі    -> перший рядок, бо кнопка без
            #                                вибору однаково не працює.
            if rows:
                if prev_name in self._part_names:
                    row = self._part_names.index(prev_name)
                elif prev_row >= 0:
                    row = min(prev_row, len(rows) - 1)
                else:
                    row = 0
                self.parts.setCurrentRow(row)
                self._sel_name = self._part_names[row]
            else:
                self._sel_name = None
        finally:
            self._filling = False
        self._sync_drop_button()
        self.parts.setVisible(bool(rows))
        self.lbl_scene.setText(summary)
        self.lbl_scene.setVisible(bool(summary))

    def set_file(self, path: str):
        self.lbl_file.setText(path)

    def show_diagnosis(self, rows, notes, extra=()):
        rows = list(rows) + list(extra)
        self.table.setRowCount(len(rows))
        for i, (name, value, level) in enumerate(rows):
            a = QtWidgets.QTableWidgetItem(name)
            b = QtWidgets.QTableWidgetItem(str(value))
            if level != "ok":
                color = QtGui.QColor(LEVEL_COLOR[level])
                b.setForeground(color)
                f = b.font()
                f.setBold(True)
                b.setFont(f)
            self.table.setItem(i, 0, a)
            self.table.setItem(i, 1, b)
        self.table.resizeRowsToContents()

        html = []
        for level, text in notes:
            html.append(
                f'<p style="margin:2px 0; color:{LEVEL_COLOR[level]}">• {text}</p>')
        self.notes.setHtml("".join(html) or "<p style='color:#777'>—</p>")

    def unit_factor(self, auto_factor: float) -> float:
        return units_factor(self.cmb_units.currentText(), auto_factor)

    def set_units(self, units: str) -> None:
        """Поставити одиниці мовчки — без перебудови сцени від сигналу."""
        if self.cmb_units.findText(units) < 0:
            return
        self.cmb_units.blockSignals(True)
        self.cmb_units.setCurrentText(units)
        self.cmb_units.blockSignals(False)


# --------------------------------------------------------------------------
# Домен
# --------------------------------------------------------------------------

class DomainPanel(Panel):
    def __init__(self, cfg, parent=None):
        super().__init__(cfg, parent)
        d = cfg.domain

        self.v.insertWidget(0, hint(
            "Розміри задаються в довжинах тіла вздовж потоку. Для крила "
            "нормою є 5 попереду і 10-15 позаду, щоб слід встиг вирівнятися.", "upstream"))

        self.up = self.bind(dspin(0.5, 50, d.upstream, 0.5, 1, " L"),
                            lambda: setattr(d, "upstream", self.up.value()))
        self.down = self.bind(dspin(1, 80, d.downstream, 0.5, 1, " L"),
                              lambda: setattr(d, "downstream", self.down.value()))
        self.side = self.bind(dspin(0.5, 50, d.side, 0.5, 1, " L"),
                              lambda: setattr(d, "side", self.side.value()))
        self.above = self.bind(dspin(0.5, 50, d.above, 0.5, 1, " L"),
                               lambda: setattr(d, "above", self.above.value()))
        self.below = self.bind(dspin(0.5, 50, d.below, 0.5, 1, " L"),
                               lambda: setattr(d, "below", self.below.value()))
        self.form.addRow("Попереду", self.up)
        _b = explain(self.up, "upstream")
        if _b: self.form.addRow(_b)
        self.form.addRow("Позаду", self.down)
        _b = explain(self.down, "downstream")
        if _b: self.form.addRow(_b)
        self.form.addRow("З боків", self.side)
        _b = explain(self.side, "side")
        if _b: self.form.addRow(_b)
        self.form.addRow("Згори", self.above)
        self.form.addRow("Знизу", self.below)

        self.v.addWidget(section("Фонова сітка"))
        self.ncells = self.bind(ispin(5, 400, d.base_cells_along_x, 5),
                                lambda: setattr(d, "base_cells_along_x",
                                                self.ncells.value()))
        self.form.addRow("Комірок уздовж X", self.ncells)
        _b = explain(self.ncells, "base_cells_x")
        if _b: self.form.addRow(_b)
        self.v.addWidget(hint(
            "blockMesh робить кубічні комірки: поділ по Y і Z рахується "
            "автоматично з цього числа.", "base_cells_x"))

        self.v.addWidget(section("Земля"))
        self.ground = QtWidgets.QCheckBox("Нижня межа — стінка (ефект землі)")
        self.ground.setChecked(d.ground)
        self.ground.toggled.connect(self._ground_changed)
        self.v.addWidget(self.ground)
        self.ground_z = self.bind(dspin(-100, 100, d.ground_offset, 0.05, 4, " м"),
                                  lambda: setattr(d, "ground_offset",
                                                  self.ground_z.value()))
        self.ground_z.setEnabled(d.ground)
        self.form2 = QtWidgets.QFormLayout()
        self.form2.addRow("Висота землі", self.ground_z)
        self.v.addLayout(self.form2)

        self.v.addWidget(section("Результат"))
        self.info = WrapLabel()
        self.info.setWordWrap(True)
        self.info.setStyleSheet("font-family: Consolas, monospace; font-size: 11px;")
        self.v.addWidget(self.info)
        self.v.addStretch(1)

    def _ground_changed(self, on):
        self.cfg.domain.ground = on
        self.ground_z.setEnabled(on)
        self.emit_changed()

    def load_cfg(self):
        d = self.cfg.domain

        def apply():
            self.up.setValue(d.upstream)
            self.down.setValue(d.downstream)
            self.side.setValue(d.side)
            self.above.setValue(d.above)
            self.below.setValue(d.below)
            self.ncells.setValue(d.base_cells_along_x)
            # Прапорці ставимо не блокуючи сигналів: їх обробники, окрім
            # запису в конфіг, ще й перемикають доступність сусідніх полів.
            self.ground.setChecked(d.ground)
            self.ground_z.setValue(d.ground_offset)
            self.ground_z.setEnabled(d.ground)

        self._loaded(apply)

    def refresh(self):
        c = self.cfg
        sx, sy, sz = c.domain_size
        nx, ny, nz = c.block_divisions
        x0, x1, y0, y1, z0, z1 = c.bounds
        self.info.setText(
            f"Домен      {sx:.4g} × {sy:.4g} × {sz:.4g} м\n"
            f"X          {x0:.4g} … {x1:.4g}\n"
            f"Y          {y0:.4g} … {y1:.4g}\n"
            f"Z          {z0:.4g} … {z1:.4g}\n"
            f"blockMesh  {nx} × {ny} × {nz} = {phys.fmt_cells(nx * ny * nz)}\n"
            f"Комірка    {phys.fmt_len(c.base_cell)}"
        )


# --------------------------------------------------------------------------
# Сітка
# --------------------------------------------------------------------------

class MeshPanel(Panel):
    def __init__(self, cfg, parent=None):
        super().__init__(cfg, parent)
        m = cfg.mesh

        self.v.insertWidget(0, section("Рефайнмент поверхні"))
        self.lmin = self.bind(ispin(0, 10, m.surface_level_min),
                              lambda: setattr(m, "surface_level_min", self.lmin.value()))
        self.lmax = self.bind(ispin(0, 10, m.surface_level_max),
                              lambda: setattr(m, "surface_level_max", self.lmax.value()))
        self.lfeat = self.bind(ispin(0, 10, m.feature_level),
                               lambda: setattr(m, "feature_level", self.lfeat.value()))
        self.form.addRow("Рівень min", self.lmin)
        self.form.addRow("Рівень max", self.lmax)
        _b = explain(self.lmax, "level_max")
        if _b: self.form.addRow(_b)
        self.form.addRow("Рівень кромок", self.lfeat)
        _b = explain(self.lfeat, "feature_level")
        if _b: self.form.addRow(_b)
        self.v.addWidget(hint(
            "Кожен рівень ділить комірку навпіл. Рівень 5 від фонової комірки "
            "означає поділ у 32 рази.", "level_max"))

        self.angle = self.bind(ispin(0, 180, m.resolve_feature_angle, 5, "°"),
                               lambda: setattr(m, "resolve_feature_angle",
                                               self.angle.value()))
        self.incl = self.bind(ispin(0, 180, m.included_angle, 5, "°"),
                              lambda: setattr(m, "included_angle", self.incl.value()))
        self.between = self.bind(ispin(1, 10, m.n_cells_between_levels),
                                 lambda: setattr(m, "n_cells_between_levels",
                                                 self.between.value()))
        self.form.addRow("resolveFeatureAngle", self.angle)
        self.form.addRow("includedAngle", self.incl)
        _b = explain(self.incl, "included_angle")
        if _b: self.form.addRow(_b)
        self.form.addRow("Комірок між рівнями", self.between)
        _b = explain(self.between, "cells_between_levels")
        if _b: self.form.addRow(_b)
        self.v.addWidget(hint(
            "includedAngle нижче 150° на фасетованій моделі перетворює кожен "
            "шов між трикутниками на «кромку». Для органіки ставте 160-170.", "included_angle"))

        self.maxcells = self.bind(ispin(100_000, 200_000_000, m.max_global_cells,
                                        500_000),
                                  lambda: setattr(m, "max_global_cells",
                                                  self.maxcells.value()))
        self.form.addRow("maxGlobalCells", self.maxcells)

        self.v.addWidget(section("Коробка згущення"))
        self.box_on = QtWidgets.QCheckBox("Коробка навколо тіла зі слідом")
        self.box_on.setChecked(True)
        self.box_on.toggled.connect(lambda _: self.emit_changed())
        self.v.addWidget(self.box_on)
        bf = QtWidgets.QFormLayout()
        self.box_factor = dspin(1.0, 6.0, 1.6, 0.1, 2, " ×")
        self.box_wake = dspin(0.0, 30.0, 4.0, 0.5, 1, " L")
        self.box_level = ispin(0, 8, 2)
        for w in (self.box_factor, self.box_wake, self.box_level):
            w.valueChanged.connect(lambda _: self.emit_changed())
        bf.addRow("Запас навколо тіла", self.box_factor)
        bf.addRow("Довжина сліду", self.box_wake)
        _b = explain(self.box_wake, "box_wake")
        if _b: bf.addRow(_b)
        bf.addRow("Рівень", self.box_level)
        self.v.addLayout(bf)

        # --- КООРДИНАТИ РУКАМИ ------------------------------------------
        #
        # Порахована коробка симетрична навколо тіла й витягнута назад
        # слідом. Для літака в потоці цього досить, але не для всього:
        # слід за гвинтом іде вбік, зрив сидить НАД крилом, а не навколо
        # нього, а в трубі бувають стінки, до яких лізти не треба.
        #
        # Прапорець окремо від чисел навмисно: зняв — і повернулася
        # порахована коробка, а введене лишилося й чекає. Інакше кожне
        # перемикання коштувало б шести чисел наново.
        self.box_manual = QtWidgets.QCheckBox(
            "Задати координати руками")
        self.box_manual.setToolTip(
            "Зняти галочку — повернеться коробка, порахована з габариту "
            "тіла. Введені числа при цьому не пропадуть.")
        self.box_manual.toggled.connect(self._box_manual_toggled)
        self.v.addWidget(self.box_manual)

        self.box_lo, self.box_hi = [], []
        mf = QtWidgets.QFormLayout()
        for label, store in (("Від (мін)", self.box_lo),
                             ("До (макс)", self.box_hi)):
            row = QtWidgets.QHBoxLayout()
            for ax in "XYZ":
                sp = dspin(-1000.0, 1000.0, 0.0, 0.01, 4, " м")
                sp.setToolTip(f"{ax} межі коробки згущення, у метрах "
                              f"кейсу")
                sp.valueChanged.connect(lambda _: self.emit_changed())
                store.append(sp)
                row.addWidget(QtWidgets.QLabel(ax))
                row.addWidget(sp, 1)
            mf.addRow(label, _wrap(row))
        self.v.addLayout(mf)

        self.btn_box_fit = QtWidgets.QPushButton(
            "Підставити пораховану коробку")
        self.btn_box_fit.setToolTip(
            "Заповнити поля тим, що дали б «Запас» і «Довжина сліду» — "
            "щоб правити від чогось розумного, а не від нулів.")
        self.btn_box_fit.clicked.connect(self.fill_box_from_auto)
        self.v.addWidget(self.btn_box_fit)

        self.lbl_box = WrapLabel("")
        self.lbl_box.setStyleSheet("color:#7c8894; font-size:11px;")
        self.v.addWidget(self.lbl_box)
        self._box_manual_toggled(False)

        # --- УСІ КОРОБКИ ПООДИНЦІ ----------------------------------------
        #
        # Поля вище правлять РІВНО ОДНУ коробку — ту, що навколо тіла. А в
        # кейсі їх буває скільки завгодно: на чотиримоторному апараті до
        # неї додаються чотири навколо гвинтів, і всі вони досі жили без
        # жодного важеля. Побачити їх можна було тільки у вювпорті
        # жовтими рамками, а змінити — ніяк.
        self.v.addWidget(section("Кожна коробка окремо"))
        self.boxes = QtWidgets.QTableWidget(0, 4)
        self.boxes.setHorizontalHeaderLabels(
            ["Коробка", "Звідки", "Розмір, мм", "Рівень"])
        self.boxes.verticalHeader().setVisible(False)
        self.boxes.setSelectionBehavior(
            QtWidgets.QAbstractItemView.SelectRows)
        self.boxes.setEditTriggers(QtWidgets.QAbstractItemView.NoEditTriggers)
        self.boxes.setMaximumHeight(170)
        hh = self.boxes.horizontalHeader()
        hh.setStretchLastSection(False)
        hh.setSectionResizeMode(0, QtWidgets.QHeaderView.Stretch)
        self.v.addWidget(self.boxes)
        self.v.addWidget(hint(
            "Рівень кожної коробки правиться просто тут. Коробки навколо "
            "гвинтів зʼявляються самі з обертових зон і їдуть за ними, "
            "якщо гвинт посунути — а рівень, поставлений руками, "
            "перебудову переживає."))
        self.lbl_boxes = WrapLabel("")
        self.lbl_boxes.setStyleSheet("color:#7c8894; font-size:11px;")
        self.v.addWidget(self.lbl_boxes)

        self.v.addWidget(section("Оболонка навколо тіла"))
        self.shell_on = QtWidgets.QCheckBox(
            "Згущувати шаром, що повторює форму тіла")
        self.shell_on.setChecked(m.shell_on)
        self.shell_on.toggled.connect(lambda _: self.emit_changed())
        self.v.addWidget(self.shell_on)
        sf = QtWidgets.QFormLayout()
        self.shell_reach = dspin(0.0, 10.0, m.shell_reach, 0.005, 3, " м")
        self.shell_level = ispin(0, 9, m.shell_level)
        self.shell_steps = ispin(1, 5, m.shell_steps)
        for w in (self.shell_reach, self.shell_level, self.shell_steps):
            w.valueChanged.connect(lambda _: self.emit_changed())
        sf.addRow("Товщина (0 — авто)", self.shell_reach)
        _s = explain(self.shell_reach, "shell_reach")
        if _s: sf.addRow(_s)
        sf.addRow("Рівень біля стінки (0 — авто)", self.shell_level)
        sf.addRow("Сходинок", self.shell_steps)
        self.v.addLayout(sf)
        self.v.addWidget(hint(
            "Коробка навколо тонкого тіла згущує гори порожнього повітря над "
            "ним і під ним. Оболонка задається ВІДСТАННЮ від поверхні, тому "
            "повторює форму: тонка на кромці, товща там, де тіло товсте. На "
            "тестовому літаку коробка займає 2.3 м³, оболонка 0.077 — у "
            "тридцять разів менше при тій самій роздільності біля стінки. "
            "Зона виходить і перед носом теж: відстань міряється в усі боки.",
            "shell_reach"))

        self.v.addWidget(section("Призматичні шари"))
        self.layers_on = QtWidgets.QCheckBox("Нарощувати пограншар")
        self.layers_on.setChecked(m.add_layers)
        self.layers_on.toggled.connect(self._layers_toggled)
        self.v.addWidget(self.layers_on)

        lf = QtWidgets.QFormLayout()
        self.nlayers = self.bind(ispin(1, 40, m.n_layers),
                                 lambda: setattr(m, "n_layers", self.nlayers.value()))
        self.expansion = self.bind(dspin(1.01, 2.0, m.expansion, 0.05, 2),
                                   lambda: setattr(m, "expansion",
                                                   self.expansion.value()))
        self.final = self.bind(dspin(0.05, 1.0, m.final_layer_rel, 0.05, 2),
                               lambda: setattr(m, "final_layer_rel", self.final.value()))
        self.minthick = self.bind(dspin(0.001, 0.5, m.min_thickness_rel, 0.01, 3),
                                  lambda: setattr(m, "min_thickness_rel",
                                                  self.minthick.value()))
        lf.addRow("Кількість шарів", self.nlayers)
        _b = explain(self.nlayers, "n_layers")
        if _b: lf.addRow(_b)
        lf.addRow("expansionRatio", self.expansion)
        lf.addRow("finalLayerThickness", self.final)
        _b = explain(self.final, "final_layer")
        if _b: lf.addRow(_b)
        lf.addRow("minThickness", self.minthick)
        self.v.addLayout(lf)

        row = QtWidgets.QHBoxLayout()
        self.target_yplus = dspin(0.3, 500, 50, 5, 1)
        self.btn_fit = QtWidgets.QPushButton("Підібрати шари")
        self.btn_fit.clicked.connect(self._fit_layers)
        row.addWidget(QtWidgets.QLabel("Цільовий y+"))
        row.addWidget(self.target_yplus)
        row.addWidget(self.btn_fit)
        self.v.addLayout(row)
        self.fit_note = WrapLabel("")
        self.fit_note.hide_when_empty = True
        self.fit_note.setVisible(False)
        self.fit_note.setWordWrap(True)
        self.fit_note.setStyleSheet("color:#b8860b; font-size:11px;")
        self.v.addWidget(self.fit_note)
        self.v.addWidget(hint(
            "y+ між 5 і 30 — буферний шар, найгірший вибір для пристінних "
            "функцій. Цільтеся або в ~1, або в 30-150."))

        self.v.addWidget(section("Адаптація: згущувати за розвʼязком"))
        self.adapt_mode = QtWidgets.QComboBox()
        # Порядок не випадковий: спершу те, що нічого не змінює, далі
        # дешеве, далі дороге. Підписи кажуть ЦІНУ, а не назву механізму —
        # людині вибирати між «швидко» й «точно», а не між іменами
        # класів OpenFOAM.
        for key, label in (
                ("none", "немає — сітка така, як задано вище"),
                ("rebuild", "перебудова: дешевий прогін, потім сітка "
                            "наново (стаціонарний, зі шарами)"),
                ("native", "рідна OpenFOAM: ділити під час прогону "
                           "(лише нестаціонарний)")):
            self.adapt_mode.addItem(label, key)
        idx = self.adapt_mode.findData(getattr(m := cfg.adapt, "mode", "none"))
        self.adapt_mode.setCurrentIndex(max(idx, 0))
        self.adapt_mode.currentIndexChanged.connect(self._adapt_changed)
        self.v.addWidget(self.adapt_mode)

        af = QtWidgets.QFormLayout()
        self.adapt_level = ispin(1, 4, m.max_level)
        self.adapt_frac = dspin(0.01, 0.40, m.fraction, 0.01, 2)
        self.adapt_every = ispin(1, 500, m.interval)
        self.adapt_curl = QtWidgets.QCheckBox(
            "брати ще завихреність — щоб бачити слід")
        self.adapt_curl.setChecked(m.use_curl)
        for w in (self.adapt_level, self.adapt_frac, self.adapt_every):
            w.valueChanged.connect(lambda _: self._adapt_changed())
        self.adapt_curl.toggled.connect(lambda _: self._adapt_changed())
        # «Глибше НА» — а не «до»: у словнику OpenFOAM це абсолютна стеля
        # рівня, і плутанина між «на» і «до» коштувала тут прогону, у якому
        # не поділилася жодна комірка. Число, що піде у словник, показуємо
        # нижче окремим рядком, щоб не треба було тримати перерахунок у
        # голові.
        af.addRow("Ділити глибше на, рівнів", self.adapt_level)
        af.addRow("Частка комірок", self.adapt_frac)
        af.addRow("Перевіряти кожні, кроків", self.adapt_every)
        self.v.addLayout(af)
        self.v.addWidget(self.adapt_curl)
        self.adapt_note = WrapLabel()
        self.adapt_note.setWordWrap(True)
        self.v.addWidget(self.adapt_note)
        self.v.addWidget(hint(
            "Показник — градієнт тиску, помножений на розмір комірки, плюс "
            "завихреність. Множення на розмір робить із крутизни поля "
            "оцінку похибки на комірці. Завихреність додається заради "
            "СЛІДУ: у ньому тиск рівний, і градієнт тиску там сліпий. У "
            "перевірці зона за самим градієнтом тягнулася на 0.4 м від "
            "носа, а з завихреністю — на 6.8 м, тобто разом зі слідом."))

        self.v.addWidget(section("Оцінка"))
        self.info = WrapLabel()
        self.info.setWordWrap(True)
        self.info.setStyleSheet("font-family: Consolas, monospace; font-size: 11px;")
        self.v.addWidget(self.info)
        self.v.addStretch(1)

    def _adapt_changed(self, *_):
        a = self.cfg.adapt
        a.mode = self.adapt_mode.currentData() or "none"
        a.max_level = self.adapt_level.value()
        a.fraction = self.adapt_frac.value()
        a.interval = self.adapt_every.value()
        a.use_curl = self.adapt_curl.isChecked()
        self.refresh_adapt()
        self.emit_changed()

    def refresh_adapt(self):
        """Показує наслідки вибору — доти, доки їх ще можна змінити.

        Головне тут не перелік полів, а те, що вибір режиму ТЯГНЕ ЗА
        СОБОЮ розвʼязувача. Рідна адаптація міняє сітку під час прогону, а
        стаціонарний розвʼязувач цього не вміє взагалі. Сказати про це
        треба тут, а не падінням через двадцять хвилин рахунку.
        """
        a = self.cfg.adapt
        native = a.mode == "native"
        for w in (self.adapt_every,):
            w.setEnabled(native)
        for w in (self.adapt_level, self.adapt_frac, self.adapt_curl):
            w.setEnabled(a.mode != "none")
        if a.mode == "none":
            self.adapt_note.setText("")
            return
        bad = [t for lvl, t in self.cfg.guard_adapt() if lvl == "bad"]
        warn = [t for lvl, t in self.cfg.guard_adapt() if lvl == "warn"]
        lines = []
        if native:
            # Спершу найдорожчий наслідок вибору, а тоді подробиці. Вибір
            # режиму адаптації тягне за собою РОЗВʼЯЗУВАЧА, і дізнатися
            # про це треба тут, а не падінням через двадцять хвилин.
            lines.append("Потрібен НЕСТАЦІОНАРНИЙ розвʼязувач: сітка "
                         "міняється під час прогону.")
            # Далі — число, у якому найлегше помилитися: людина ставить
            # «на скільки», а OpenFOAM читає «до якого». Показуємо обидва
            # й розмір комірки, бо саме він відповідає на «чи не занадто».
            base = self.cfg.finest_level()
            top = base + max(a.max_level, 1)
            small = self.cfg.base_cell / 2 ** top
            lines.append(
                f"Сітка вже має рівень {base}; ділити дозволено до {top}. "
                f"Найдрібніша комірка стане {small * 1000:.2f} мм "
                f"(зараз {self.cfg.base_cell / 2 ** base * 1000:.2f} мм).")
            # Пристінний пояс — не подробиця, а третина-половина сітки,
            # якої адаптація не торкнеться. Краще побачити це тут, ніж
            # дивуватися потім, чому біля тіла нічого не змінилося.
            guard = self.cfg.adapt_wall_guard
            ref = self.cfg.flow.l_ref or self.cfg.body_length
            share = f", тобто {guard / ref * 100:.0f}% опорної довжини" \
                if ref > 0 else ""
            lines.append(
                f"Пристінний пояс {guard * 1000:.1f} мм{share} адаптація "
                f"не чіпає: hexRef8 не вміє ділити пригнані до поверхні "
                f"комірки, а там цього й не треба.")
            # Стеля на комірки мʼяка, і це виміряно: при 1 200 000 сітка
            # стала на 1 461 595. Сказати про це треба там, де людина
            # прикидає памʼять, а не постфактум у журналі.
            soft = int(a.max_cells * 1.25)
            lines.append(
                f"Стеля {a.max_cells / 1e6:.1f} млн комірок — мʼяка: "
                f"OpenFOAM додає цілий рівень, перш ніж перевірити запас, "
                f"а тоді ще дописує сусідів заради правила 2:1. На "
                f"перевірці вийшло на 22% більше. Памʼять рахуйте "
                f"приблизно на {soft / 1e6:.1f} млн.")
        else:
            lines.append("Працює зі стаціонарним і з призматичними шарами. "
                         "Ціна — два проходи замість одного.")
        for t in bad:
            lines.append("⛔ " + t)
        for t in warn:
            lines.append("⚠ " + t)
        self.adapt_note.setText("\n".join(lines))

    def _layers_toggled(self, on):
        self.cfg.mesh.add_layers = on
        self.emit_changed()

    def _fit_layers(self):
        c = self.cfg
        target = self.target_yplus.value()
        n = phys.layers_for_target_yplus(
            target, c.flow.u_inf, c.flow.l_ref, c.flow.nu,
            c.surface_cell, c.mesh.expansion, c.mesh.final_layer_rel)
        if not n:
            self.fit_note.setText("Не вдалося підібрати: перевірте швидкість і Lref.")
            return
        self.nlayers.setValue(n)

        got = phys.LayerStack(n, c.mesh.expansion, c.mesh.final_layer_rel,
                              c.surface_cell).yplus(c.flow.u_inf, c.flow.l_ref,
                                                    c.flow.nu)
        if n == 1 and got > target * 1.25:
            # Навіть один шар виходить товщим за потрібний: обмежує не кількість
            # шарів, а розмір комірки на поверхні.
            self.fit_note.setText(
                f"Нижче y+ ≈ {got:.0f} не опуститися: перший шар уже дорівнює "
                f"finalLayerThickness. Зменшіть finalLayerThickness або "
                f"підніміть рівень рефайнменту поверхні.")
        elif abs(got - target) / max(target, 1e-9) > 0.3:
            self.fit_note.setText(f"Найближче можливе: {n} шарів дає y+ ≈ {got:.1f}.")
        else:
            self.fit_note.setText(f"{n} шарів дає y+ ≈ {got:.1f}.")

    def load_cfg(self):
        m = self.cfg.mesh

        def apply():
            self.lmin.setValue(m.surface_level_min)
            self.lmax.setValue(m.surface_level_max)
            self.lfeat.setValue(m.feature_level)
            self.angle.setValue(m.resolve_feature_angle)
            self.incl.setValue(m.included_angle)
            self.between.setValue(m.n_cells_between_levels)
            self.maxcells.setValue(m.max_global_cells)
            self.nlayers.setValue(m.n_layers)
            self.expansion.setValue(m.expansion)
            self.final.setValue(m.final_layer_rel)
            self.minthick.setValue(m.min_thickness_rel)
            self.layers_on.setChecked(m.add_layers)
            # Коробка в конфігу лежить абсолютними координатами, а панель
            # тримає три числа, з яких її будують. Відновлюємо саме їх:
            # інакше перший же sync_box замінив би збережену коробку
            # типовою — див. CaseCfg.box_params.
            near = self.cfg.box_named("nearBody")
            self.box_on.setChecked(near is not None)
            if near is not None:
                factor, wake, level = self.cfg.box_params(near)
                self.box_factor.setValue(factor)
                self.box_wake.setValue(wake)
                self.box_level.setValue(level)
            if any(m.box_min) or any(m.box_max):
                for sp, v in zip(self.box_lo, m.box_min):
                    sp.setValue(float(v))
                for sp, v in zip(self.box_hi, m.box_max):
                    sp.setValue(float(v))
            self.box_manual.setChecked(bool(m.box_manual))
            self.shell_on.setChecked(m.shell_on)
            self.shell_reach.setValue(m.shell_reach)
            self.shell_level.setValue(m.shell_level)
            self.shell_steps.setValue(m.shell_steps)

        self._loaded(apply)

    def _box_manual_toggled(self, on: bool):
        """Показати потрібні поля і, якщо треба, заповнити їх розумним."""
        for w in (self.box_factor, self.box_wake):
            w.setEnabled(not on)
        for w in self.box_lo + self.box_hi + [self.btn_box_fit]:
            w.setVisible(on)
        # Переходячи на ручний режим із порожніми полями, підставляємо
        # пораховану коробку: правити від нулів незручно, а коробка з
        # нульовим обсягом ще й заборонена.
        if on and not any(w.value() for w in self.box_lo + self.box_hi):
            self.fill_box_from_auto()
        else:
            self.emit_changed()

    def fill_box_from_auto(self):
        """Заповнити поля тим, що дала б порахована коробка."""
        b = self.cfg.default_box(factor=self.box_factor.value(),
                                 level=self.box_level.value(),
                                 wake=self.box_wake.value())
        for sp, v in zip(self.box_lo, b.min):
            sp.blockSignals(True)
            sp.setValue(float(v))
            sp.blockSignals(False)
        for sp, v in zip(self.box_hi, b.max):
            sp.blockSignals(True)
            sp.setValue(float(v))
            sp.blockSignals(False)
        self.emit_changed()

    def show_box_note(self, text: str):
        self.lbl_box.setText(text)

    def fill_boxes(self):
        """Перемалювати таблицю коробок під поточний конфіг.

        Таблицю ПЕРЕБУДОВУЄМО лише тоді, коли змінився сам набір коробок.
        Інакше редагування власного рівня руйнувало б віджет, у якому
        людина саме зараз крутить число: правка -> emit_changed ->
        refresh -> перебудова таблиці -> лічильник знищено посеред
        власного сигналу. Тому при тому самому наборі лише оновлюємо
        підписи, а лічильники лишаємо жити.
        """
        boxes = list(self.cfg.mesh.boxes)
        key = [(b.name, b.source) for b in boxes]
        rebuild = key != getattr(self, "_box_key", None)
        self._box_key = key
        self._filling_boxes = True
        try:
            if rebuild:
                self.boxes.setRowCount(len(boxes))
            base = self.cfg.base_cell
            for row, b in enumerate(boxes):
                size = [(h - lo) * 1000 for lo, h in zip(b.min, b.max)]
                for col, text in enumerate((
                        b.name,
                        {"body": "тіло", "zone": "гвинт",
                         "manual": "руками"}.get(b.source, b.source),
                        f"{size[0]:.0f} × {size[1]:.0f} × {size[2]:.0f}")):
                    item = self.boxes.item(row, col)
                    if item is None:
                        item = QtWidgets.QTableWidgetItem(text)
                        self.boxes.setItem(row, col, item)
                    else:
                        item.setText(text)
                cell = base / 2 ** max(b.level, 0) if base > 0 else 0.0
                sp = self.boxes.cellWidget(row, 3)
                if sp is None:
                    sp = ispin(0, 9, b.level, 1)
                    sp.valueChanged.connect(
                        lambda v, n=b.name: self._box_level_changed(n, v))
                    self.boxes.setCellWidget(row, 3, sp)
                elif sp.value() != b.level:
                    sp.blockSignals(True)
                    sp.setValue(b.level)
                    sp.blockSignals(False)
                sp.setToolTip(f"Комірка в цій коробці: "
                              f"{phys.fmt_len(cell)}"
                              + ("  (рівень поставлено руками)"
                                 if b.level_by_hand else ""))
            self.boxes.resizeColumnsToContents()
            self.boxes.horizontalHeader().setSectionResizeMode(
                0, QtWidgets.QHeaderView.Stretch)
        finally:
            self._filling_boxes = False
        if not boxes:
            self.lbl_boxes.setText("Коробок згущення немає.")
        else:
            worst = min(base / 2 ** max(b.level, 0) for b in boxes) \
                if base > 0 else 0.0
            self.lbl_boxes.setText(
                f"Коробок: {len(boxes)}. Найдрібніша комірка в них — "
                f"{phys.fmt_len(worst)}.")

    def _box_level_changed(self, name: str, level: int):
        if getattr(self, "_filling_boxes", False):
            return
        for b in self.cfg.mesh.boxes:
            if b.name == name:
                b.level = int(level)
                # Позначаємо саме тут: далі перебудова коробок зон
                # дивиться на цей прапорець і НЕ повертає типовий рівень.
                b.level_by_hand = True
        self.emit_changed()

    def sync_box(self):
        """Перебудовує коробки згущення з поточних полів.

        Заразом переписує оболонку: вона задається не координатами, а
        відстанню від поверхні, тож нічого будувати не треба.
        """
        m = self.cfg.mesh
        # ЧУЖІ КОРОБКИ ЛИШАЮТЬСЯ. Панель відповідає рівно за одну —
        # `nearBody`, ту, що під її полями. Решта могла прийти з проєкту
        # чи бути поставлена навколо гвинтів, і витирати її тут означало б
        # мовчки скасовувати чуже рішення на кожну зміну сцени: sync_box
        # кличеться з refresh_all, тобто раз у раз.
        #
        # І модель даних, і запис словника, і guard_boxes багато коробок
        # підтримували завжди — лише панель зводила список до однієї, тож
        # дістатися решти було нічим.
        others = [b for b in m.boxes if b.name != "nearBody"]
        own = []
        if self.box_on.isChecked():
            if self.box_manual.isChecked():
                # РУКАМИ — беремо числа як є, не «виправляючи» їх.
                # Мовчки переставити місцями переплутані межі означало б
                # приховати помилку; на неї є заборона в guard_boxes.
                own.append(fc.RefinementBox(
                    name="nearBody",
                    min=tuple(w.value() for w in self.box_lo),
                    max=tuple(w.value() for w in self.box_hi),
                    level=self.box_level.value(),
                    source="body"))
            else:
                own.append(self.cfg.default_box(
                    factor=self.box_factor.value(),
                    level=self.box_level.value(),
                    wake=self.box_wake.value(),
                ))
        m.box_manual = self.box_manual.isChecked()
        m.box_min = tuple(w.value() for w in self.box_lo)
        m.box_max = tuple(w.value() for w in self.box_hi)
        # `nearBody` попереду: доки чужих коробок немає, список виходить
        # той самий, що й раніше, — а отже, і словник байт у байт.
        m.boxes = own + others
        # Оболонка координат не має — вона живе трьома числами в самому
        # MeshCfg, тому перебудовувати нічого, лише переписати.
        m.shell_on = self.shell_on.isChecked()
        m.shell_reach = self.shell_reach.value()
        m.shell_level = self.shell_level.value()
        m.shell_steps = self.shell_steps.value()

    def refresh(self):
        c = self.cfg
        # Наслідки вибору адаптації залежать не лише від самої адаптації, а
        # й від галочки «стаціонарна» на сусідній вкладці. Тому перемальовуємо
        # їх на кожному оновленні панелі, а не лише коли чіпають цей список.
        self.refresh_adapt()
        self.fill_boxes()
        stack = c.layer_stack
        f = c.flow
        lines = [
            f"Комірка на поверхні  {phys.fmt_len(c.surface_cell)}",
            f"Комірка на кромках   {phys.fmt_len(c.base_cell / 2 ** c.mesh.feature_level)}",
        ]
        if stack.n_layers:
            yp = stack.yplus(f.u_inf, f.l_ref, f.nu)
            label, level = phys.yplus_regime(yp)
            cover = stack.covers_bl(f.u_inf, f.l_ref, f.nu)
            lines += [
                f"Перший шар           {phys.fmt_len(stack.first_abs)}",
                f"Стек шарів           {phys.fmt_len(stack.total)}",
                f"Пограншар            {phys.fmt_len(phys.bl_thickness(f.u_inf, f.l_ref, f.nu))}",
                # Не «покриття»: так називається ще й частка граней, які
                # отримали шари після прогону, а це зовсім інше число.
                f"Стек / пограншар     {cover:.2f}  "
                f"({'досить' if cover >= 0.5 else 'стек надто тонкий'})",
                f"y+ (оцінка)          {yp:.1f} — {label}",
            ]
        else:
            lines.append("Шарів немає — пристінні функції на «голій» сітці")
        self.info.setText("\n".join(lines))


# --------------------------------------------------------------------------
# Потік
# --------------------------------------------------------------------------

class FlowPanel(Panel):
    def __init__(self, cfg, parent=None):
        super().__init__(cfg, parent)
        f = cfg.flow

        self.u = self.bind(dspin(0.001, 2000, f.u_inf, 1, 3, " м/с"),
                           lambda: setattr(f, "u_inf", self.u.value()))
        self.aoa = self.bind(dspin(-90, 90, f.aoa_deg, 0.5, 2, "°"),
                             lambda: setattr(f, "aoa_deg", self.aoa.value()))
        self.aos = self.bind(dspin(-90, 90, f.aos_deg, 0.5, 2, "°"),
                             lambda: setattr(f, "aos_deg", self.aos.value()))
        self.form.addRow("Швидкість", self.u)
        _b = explain(self.u, "u_inf")
        if _b: self.form.addRow(_b)
        self.form.addRow("Кут атаки", self.aoa)
        _b = explain(self.aoa, "aoa")
        if _b: self.form.addRow(_b)
        self.form.addRow("Кут ковзання", self.aos)
        self.v.addWidget(hint(
            "Кут задається поворотом набігаючого потоку, а не геометрії — "
            "напрямки підйомної сили й опору переставляються автоматично.", "aoa"))

        self.v.addWidget(section("Дальнє поле"))
        self.cmb_far = QtWidgets.QComboBox()
        self.cmb_far.addItem("freestream (рекомендовано)", "freestream")
        self.cmb_far.addItem("аеротруба: вхід-вихід + slip", "tunnel")
        self.cmb_far.currentIndexChanged.connect(self._far_changed)
        self.form_far = QtWidgets.QFormLayout()
        self.form_far.addRow("Тип меж", self.cmb_far)
        _b = explain(self.cmb_far, "farfield")
        if _b: self.form_far.addRow(_b)
        self.v.addLayout(self.form_far)
        self.lbl_far = WrapLabel("")
        self.lbl_far.hide_when_empty = True
        self.lbl_far.setVisible(False)
        self.lbl_far.setWordWrap(True)
        self.v.addWidget(self.lbl_far)
        self.v.addWidget(hint(
            "freestream сам визначає, де вхід, а де вихід, за напрямком "
            "потоку крізь грань. Режим аеротруби зі slip на бічних межах "
            "занулює нормальну складову швидкості, тобто зрізає кут атаки.", "farfield"))

        self.v.addWidget(section("Режим течії"))
        self.cmb_regime = QtWidgets.QComboBox()
        self.cmb_regime.addItem("нестислива — simpleFoam", "incompressible")
        self.cmb_regime.addItem("стислива — rhoSimpleFoam", "compressible")
        index = self.cmb_regime.findData(f.regime)
        if index >= 0:
            self.cmb_regime.setCurrentIndex(index)
        self.cmb_regime.currentIndexChanged.connect(self._regime_changed)
        self.form_reg = QtWidgets.QFormLayout()
        self.form_reg.addRow("Режим", self.cmb_regime)
        _b = explain(self.cmb_regime, "regime")
        if _b: self.form_reg.addRow(_b)
        self.v.addLayout(self.form_reg)

        # Температура й тиск живуть в окремому віджеті, а не рядками спільної
        # форми: так їх можна сховати цілком, коли режим нестисливий, і вони
        # не лишають по собі порожніх підписів.
        self.box_thermo = QtWidgets.QWidget()
        form_thermo = QtWidgets.QFormLayout(self.box_thermo)
        form_thermo.setContentsMargins(0, 0, 0, 0)
        self.t_inf = self.bind(dspin(50, 3000, f.t_inf, 1, 2, " К"),
                               self._thermo_changed)
        self.p_inf = self.bind(dspin(100, 1e7, f.p_inf, 1000, 1, " Па"),
                               self._thermo_changed)
        form_thermo.addRow("Температура", self.t_inf)
        form_thermo.addRow("Тиск", self.p_inf)
        self.v.addWidget(self.box_thermo)
        self.lbl_regime = WrapLabel("")
        self.lbl_regime.setWordWrap(True)
        self.lbl_regime.setStyleSheet("color:#7c8894; font-size:11px;")
        self.v.addWidget(self.lbl_regime)
        self.v.addWidget(hint(
            "simpleFoam не бачить стисливості й занижує опір при M > 0.3. "
            "rhoSimpleFoam рахує густину з тиску й температури — тому там "
            "nu і rho не задаються, а виходять самі.", "regime"))

        self.v.addWidget(section("Середовище"))
        self.cmb_media = QtWidgets.QComboBox()
        self.cmb_media.addItems(list(MEDIA))
        self.cmb_media.currentTextChanged.connect(self._media_changed)
        self.form2 = QtWidgets.QFormLayout()
        self.form2.addRow("Готовий набір", self.cmb_media)
        self.nu = self.bind(sci(1e-9, 1.0, f.nu),
                            lambda: setattr(f, "nu", self.nu.value()))
        self.rho = self.bind(dspin(0.001, 20000, f.rho, 0.1, 4, " кг/м³"),
                             lambda: setattr(f, "rho", self.rho.value()))
        self.form2.addRow("nu (м²/с)", self.nu)
        self.form2.addRow("rho", self.rho)
        self.v.addLayout(self.form2)

        self.v.addWidget(section("Турбулентність"))
        self.cmb_turb = QtWidgets.QComboBox()
        self.cmb_turb.addItems(list(fc.TURBULENCE_MODELS))
        self.cmb_turb.setCurrentText(f.turbulence)
        self.cmb_turb.currentTextChanged.connect(
            lambda t: (setattr(f, "turbulence", t), self.emit_changed()))
        self.form3 = QtWidgets.QFormLayout()
        self.form3.addRow("Модель", self.cmb_turb)
        _b = explain(self.cmb_turb, "turbulence")
        if _b: self.form3.addRow(_b)
        self.intensity = self.bind(dspin(0.01, 30, f.intensity * 100, 0.1, 2, " %"),
                                   lambda: setattr(f, "intensity",
                                                   self.intensity.value() / 100))
        self.ratio = self.bind(dspin(0.1, 1000, f.viscosity_ratio, 1, 1),
                               lambda: setattr(f, "viscosity_ratio", self.ratio.value()))
        self.form3.addRow("Інтенсивність", self.intensity)
        self.form3.addRow("nut / nu на вході", self.ratio)
        self.v.addLayout(self.form3)
        self.v.addWidget(hint(
            "SpalartAllmaras — класика для зовнішньої аеродинаміки при малих "
            "відривах. kOmegaSST точніший на відривних режимах. "
            "kOmegaSSTLM додає до нього перехід ламінарного в турбулентне, "
            "але вимагає y+ ≈ 1 — на сітці з пристінною функцією вона "
            "безсила.", "turbulence"))
        self.v.addWidget(hint(
            "Моделі з DES у назві — вихрові. Вони рахують вихори за часом і "
            "вимагають нестаціонарної постановки: зі стаціонарним "
            "розвʼязувачем дадуть ту саму усереднену картину, що й звичайна "
            "RANS, тільки втричі дорожче. Беріть їх для відриву й зриву — "
            "там RANS не збігається взагалі: на тестовому крилі при 10 "
            "градусах Cl гуляв від 0.31 до 0.77 і не зупинявся.", "les"))

        self.v.addWidget(section("Пориста зона"))
        self.por_on = QtWidgets.QCheckBox("Радіатор, сітка або фільтр у потоці")
        self.por_on.setToolTip(
            "Коробка, всередині якої потік гальмується так, ніби крізь нього "
            "стоїть перешкода. Саме так рахують радіатори, вхідні сітки, "
            "фільтри й парашути: моделювати кожен отвір нема сенсу.")
        self.por_on.setChecked(bool(cfg.porous))
        self.por_on.toggled.connect(self._porous_toggled)
        self.v.addWidget(self.por_on)
        _b = explain(self.por_on, "porous")
        if _b: self.v.addWidget(_b)

        self.box_por = QtWidgets.QWidget()
        pf = QtWidgets.QFormLayout(self.box_por)
        pf.setContentsMargins(0, 0, 0, 0)
        # Перепад тиску, а не коефіцієнти Дарсі: у паспорті радіатора стоїть
        # саме він, а d = 5.8e6 не введе усвідомлено ніхто.
        self.por_dp = dspin(0.1, 1e6, 200.0, 10.0, 2, " Па")
        self.por_u = dspin(0.1, 500.0, 10.0, 1.0, 2, " м/с")
        self.cmb_por_mode = QtWidgets.QComboBox()
        self.cmb_por_mode.addItem("сітка або радіатор (квадратично)", "inertial")
        self.cmb_por_mode.addItem("дрібний фільтр (лінійно)", "viscous")
        self.por_cx = dspin(-1e4, 1e4, 0.0, 0.05, 4, " м")
        self.por_cy = dspin(-1e4, 1e4, 0.0, 0.05, 4, " м")
        self.por_cz = dspin(-1e4, 1e4, 0.0, 0.05, 4, " м")
        self.por_sx = dspin(1e-4, 1e4, 0.05, 0.01, 4, " м")
        self.por_sy = dspin(1e-4, 1e4, 0.3, 0.01, 4, " м")
        self.por_sz = dspin(1e-4, 1e4, 0.3, 0.01, 4, " м")
        for w in (self.por_dp, self.por_u, self.por_cx, self.por_cy,
                  self.por_cz, self.por_sx, self.por_sy, self.por_sz):
            w.valueChanged.connect(lambda _: self._porous_changed())
        self.cmb_por_mode.currentIndexChanged.connect(
            lambda _: self._porous_changed())
        pf.addRow("Втрата тиску", self.por_dp)
        _b = explain(self.por_dp, "porous_dp")
        if _b: pf.addRow(_b)
        pf.addRow("при швидкості", self.por_u)
        pf.addRow("Характер втрат", self.cmb_por_mode)
        for label, w in (("Центр X", self.por_cx), ("Центр Y", self.por_cy),
                         ("Центр Z", self.por_cz), ("Розмір X", self.por_sx),
                         ("Розмір Y", self.por_sy), ("Розмір Z", self.por_sz)):
            pf.addRow(label, w)
        self.v.addWidget(self.box_por)
        self.lbl_por = WrapLabel("")
        self.lbl_por.setWordWrap(True)
        self.lbl_por.setStyleSheet("color:#7c8894; font-size:11px;")
        self.v.addWidget(self.lbl_por)

        self.v.addWidget(section("Обертова зона (гвинт)"))
        self.rot_on = QtWidgets.QCheckBox("Гвинт або ротор у потоці")
        self.rot_on.setToolTip(
            "Циліндр, усередині якого повітря рахується в обертовій системі "
            "відліку (MRF). Так рахують гвинти, не рухаючи сітку. "
            "ВАЖЛИВО: тягу створює не зона, а лопаті всередині неї — "
            "геометрія гвинта має бути в сцені.")
        self.rot_on.setChecked(bool(cfg.rotating))
        self.rot_on.toggled.connect(self._rotating_toggled)
        self.v.addWidget(self.rot_on)
        _b = explain(self.rot_on, "rotating")
        if _b: self.v.addWidget(_b)

        # КОЛИ ЗОНИ ЙДУТЬ ІЗ РОЛЕЙ, ця галочка не робить нічого: зони
        # перезбираються з деталей на кожне оновлення й перекривають те,
        # що тут задано. Мовчазна галочка, яка виглядає робочою, — це
        # запрошення крутити її й дивуватися; тому пишемо прямо.
        self.lbl_rot_roles = WrapLabel("")
        self.lbl_rot_roles.setWordWrap(True)
        self.lbl_rot_roles.setStyleSheet(
            "color:#8fd18f; font-size:11px;")
        self.lbl_rot_roles.setVisible(False)
        self.v.addWidget(self.lbl_rot_roles)

        self.box_rot = QtWidgets.QWidget()
        rf = QtWidgets.QFormLayout(self.box_rot)
        rf.setContentsMargins(0, 0, 0, 0)
        self.rot_rpm = dspin(-100000.0, 100000.0, 3000.0, 100.0, 1, " об/хв")
        self.rot_r = dspin(1e-4, 100.0, 0.15, 0.01, 4, " м")
        self.rot_len = dspin(1e-4, 100.0, 0.05, 0.01, 4, " м")
        self.rot_cx = dspin(-1e4, 1e4, 0.0, 0.05, 4, " м")
        self.rot_cy = dspin(-1e4, 1e4, 0.0, 0.05, 4, " м")
        self.rot_cz = dspin(-1e4, 1e4, 0.0, 0.05, 4, " м")
        for w in (self.rot_rpm, self.rot_r, self.rot_len, self.rot_cx,
                  self.rot_cy, self.rot_cz):
            w.valueChanged.connect(lambda _: self._rotating_changed())
        rf.addRow("Оберти", self.rot_rpm)
        _b = explain(self.rot_rpm, "rpm")
        if _b: rf.addRow(_b)
        rf.addRow("Радіус зони", self.rot_r)
        rf.addRow("Товщина зони", self.rot_len)
        for label, w in (("Центр X", self.rot_cx), ("Центр Y", self.rot_cy),
                         ("Центр Z", self.rot_cz)):
            rf.addRow(label, w)
        # Вписувати координати зони руками — найкоротший шлях до головної
        # пастки MRF: зона повз лопаті рахується БЕЗ ПОМИЛОК і просто не
        # дає тяги. Кнопка бере габарит самої моделі гвинта, і промахнутися
        # стає ніде.
        self.btn_rot_fit = QtWidgets.QPushButton(
            "Підігнати зону до вибраної моделі")
        self.btn_rot_fit.setToolTip(
            "Виберіть у списку на вкладці «Геометрія» модель гвинта — "
            "кнопка візьме її габарит і поставить зону так, щоб лопаті "
            "напевно опинилися всередині.")
        rf.addRow("", self.btn_rot_fit)
        self.v.addWidget(self.box_rot)
        self.lbl_rot = WrapLabel("")
        self.lbl_rot.setWordWrap(True)
        self.lbl_rot.setStyleSheet("color:#7c8894; font-size:11px;")
        self.v.addWidget(self.lbl_rot)

        self.v.addWidget(section("Опорні величини для коефіцієнтів"))
        self.form4 = QtWidgets.QFormLayout()
        self.aref = self.bind(dspin(1e-6, 1e6, f.a_ref, 0.01, 6, " м²"),
                              lambda: setattr(f, "a_ref", self.aref.value()))
        self.lref = self.bind(dspin(1e-6, 1e6, f.l_ref, 0.01, 6, " м"),
                              lambda: setattr(f, "l_ref", self.lref.value()))
        self.form4.addRow("Aref", self.aref)
        _b = explain(self.aref, "aref")
        if _b: self.form4.addRow(_b)
        self.form4.addRow("Lref", self.lref)
        _b = explain(self.lref, "lref")
        if _b: self.form4.addRow(_b)
        self.btn_auto = QtWidgets.QPushButton("Взяти з габариту моделі")
        self.form4.addRow("", self.btn_auto)
        self.v.addLayout(self.form4)
        self.lbl_area = WrapLabel("")
        self.lbl_area.hide_when_empty = True
        self.lbl_area.setVisible(False)
        self.lbl_area.setWordWrap(True)
        self.lbl_area.setStyleSheet("color:#777; font-size:11px;")
        self.v.addWidget(self.lbl_area)
        self.v.addWidget(hint(
            "Aref — площа, до якої нормуються Cd і Cl. Для крилатого апарата "
            "це площа в плані, для компактного тіла — міделевий переріз. "
            "Кнопка бере справжній силует моделі, а не добуток сторін "
            "габариту: різниця буває в кілька разів."))

        self.v.addWidget(section("Розраховані величини"))
        self.info = WrapLabel()
        self.info.setWordWrap(True)
        self.info.setStyleSheet("font-family: Consolas, monospace; font-size: 11px;")
        self.v.addWidget(self.info)
        self.v.addStretch(1)

    def load_cfg(self):
        f = self.cfg.flow

        def apply():
            self.u.setValue(f.u_inf)
            self.aoa.setValue(f.aoa_deg)
            self.aos.setValue(f.aos_deg)
            self.nu.setValue(f.nu)
            self.rho.setValue(f.rho)
            self.aref.setValue(f.a_ref)
            self.lref.setValue(f.l_ref)
            self.intensity.setValue(f.intensity * 100)
            self.ratio.setValue(f.viscosity_ratio)
            self.cmb_turb.setCurrentText(f.turbulence)
            index = self.cmb_far.findData(f.farfield_mode)
            if index >= 0:
                self.cmb_far.setCurrentIndex(index)
            # Єдиний комбобокс, якому доводиться глушити сигнали: його
            # обробник сам задає nu і rho з готового набору і затер би
            # збережені значення. Тому шукаємо набір під числа, а не навпаки.
            self.cmb_media.blockSignals(True)
            self.cmb_media.setCurrentText(media_name(f.nu, f.rho))
            self.cmb_media.blockSignals(False)
            index = self.cmb_regime.findData(f.regime)
            if index >= 0:
                self.cmb_regime.setCurrentIndex(index)
            self.t_inf.setValue(f.t_inf)
            self.p_inf.setValue(f.p_inf)
            self.rot_on.setChecked(bool(self.cfg.rotating))
            self._show_zone_source()
            if self.cfg.rotating:
                r = self.cfg.rotating[0]
                for w, v in zip((self.rot_cx, self.rot_cy, self.rot_cz),
                                r.centre):
                    w.setValue(v)
                self.rot_rpm.setValue(r.rpm)
                self.rot_r.setValue(r.radius)
                self.rot_len.setValue(r.length)
            self.por_on.setChecked(bool(self.cfg.porous))
            if self.cfg.porous:
                z = self.cfg.porous[0]
                for w, v in zip((self.por_cx, self.por_cy, self.por_cz),
                                z.centre):
                    w.setValue(v)
                for w, v in zip((self.por_sx, self.por_sy, self.por_sz),
                                z.size):
                    w.setValue(v)
                self.por_dp.setValue(z.dp)
                self.por_u.setValue(z.u_ref)
                i = self.cmb_por_mode.findData(z.mode)
                if i >= 0:
                    self.cmb_por_mode.setCurrentIndex(i)

        self._loaded(apply)
        self._sync_porous()
        self._sync_rotating()
        self._sync_regime()
        self._check_farfield()

    def set_area_hint(self, winged: bool, diag) -> None:
        kind = "планформа (силует згори)" if winged else "мідель (силует спереду)"
        self.lbl_area.setText(
            f"Aref взято як {kind}. Силует X/Y/Z: "
            f"{diag.projected[0]:.4g} / {diag.projected[1]:.4g} / "
            f"{diag.projected[2]:.4g} м². Габаритна планформа була б "
            f"{diag.size[0] * diag.size[1]:.4g} м².")

    def _far_changed(self, _):
        self.cfg.flow.farfield_mode = self.cmb_far.currentData()
        self.emit_changed()

    def _check_farfield(self):
        """Режим аеротруби при ненульових кутах дає грубо занижену підйомну силу."""
        f = self.cfg.flow
        bad = (f.farfield_mode == "tunnel"
               and (abs(f.aoa_deg) > 0.01 or abs(f.aos_deg) > 0.01))
        if bad:
            self.lbl_far.setText(
                "⚠ Аеротруба зі slip несумісна з ненульовим кутом: бічні межі "
                "зріжуть поперечну складову потоку і Cl впаде в рази. "
                "Перемкніться на freestream.")
            self.lbl_far.setStyleSheet(
                f"color:{LEVEL_COLOR['bad']}; font-size:11px; font-weight:bold;")
        else:
            self.lbl_far.setText("")
        return bad

    def _media_changed(self, name):
        nu, rho = MEDIA[name]
        if nu is None:
            return
        self._loading = True
        self.nu.setValue(nu)
        self.rho.setValue(rho)
        self._loading = False
        self.cfg.flow.nu = nu
        self.cfg.flow.rho = rho
        self.emit_changed()

    # --- пориста зона -----------------------------------------------------

    def _porous_toggled(self, on: bool):
        if self._loading:
            return
        self._porous_changed()

    def _porous_changed(self):
        """Збирає зону з полів. Одна зона — не обмеження формату, а межа
        інтерфейсу: список зон із власним редактором тут поки надмір."""
        if self._loading:
            return
        c = self.cfg
        if self.por_on.isChecked():
            c.porous = [fc.PorousZone(
                name="porous",
                centre=(self.por_cx.value(), self.por_cy.value(),
                        self.por_cz.value()),
                size=(self.por_sx.value(), self.por_sy.value(),
                      self.por_sz.value()),
                # Напрямок «крізь» — уздовж потоку. Це вісь X: програма
                # домовилася, що потік іде саме так, а кут атаки задається
                # поворотом набігаючого потоку, не геометрії.
                axis=(1.0, 0.0, 0.0),
                dp=self.por_dp.value(), u_ref=self.por_u.value(),
                mode=self.cmb_por_mode.currentData())]
        else:
            c.porous = []
        self._sync_porous()
        self.emit_changed()

    def _sync_porous(self):
        self.box_por.setVisible(self.por_on.isChecked())
        if not self.cfg.porous:
            self.lbl_por.setVisible(False)
            return
        z = self.cfg.porous[0]
        d, f = z.darcy(self.cfg.flow.nu, self.cfg.flow.rho)
        text = (f"Шар завтовшки {phys.fmt_len(z.thickness)} уздовж потоку. "
                f"Коефіцієнти для OpenFOAM: d = {d:.4g}, f = {f:.4g}.")
        notes = [t for lvl, t in self.cfg.guard_porous()]
        worst = next((lvl for lvl, _ in self.cfg.guard_porous()
                      if lvl == "bad"), None)
        if notes:
            text += "<br>" + "<br>".join(notes)
        colour = "#e05252" if worst else ("#d6a419" if notes else "#7c8894")
        self.lbl_por.setStyleSheet(f"color:{colour}; font-size:11px;")
        self.lbl_por.setText(text)
        self.lbl_por.setVisible(True)

    # --- обертова зона ----------------------------------------------------

    def _rotating_toggled(self, on: bool):
        if self._loading:
            return
        self._rotating_changed()

    def _rotating_changed(self):
        if self._loading:
            return
        c = self.cfg
        if self.rot_on.isChecked():
            c.rotating = [fc.RotatingZone(
                name="rotor",
                centre=(self.rot_cx.value(), self.rot_cy.value(),
                        self.rot_cz.value()),
                # Вісь обертання — уздовж потоку: гвинт тягне вперед. Інші
                # осі (ротор гелікоптера) поки не задаються з вікна.
                axis=(1.0, 0.0, 0.0),
                radius=self.rot_r.value(), length=self.rot_len.value(),
                rpm=self.rot_rpm.value())]
        else:
            c.rotating = []
        self._sync_rotating()
        self.emit_changed()

    def set_rotating_zone(self, centre, radius: float, length: float) -> None:
        """Поставити зону ззовні — з габариту моделі гвинта.

        Значення кладемо в поля, а не просто в конфіг: інакше вікно
        показувало б одне, а рахувалося б інше. Сигнали на час
        підстановки глушимо, щоб три `valueChanged` не перезбирали зону
        тричі поспіль із напівзаповненими числами.
        """
        self._loading = True
        try:
            self.rot_on.setChecked(True)
            self.rot_cx.setValue(centre[0])
            self.rot_cy.setValue(centre[1])
            self.rot_cz.setValue(centre[2])
            self.rot_r.setValue(radius)
            self.rot_len.setValue(length)
        finally:
            self._loading = False
        self._rotating_changed()

    def _sync_rotating(self):
        self.box_rot.setVisible(self.rot_on.isChecked())
        if not self.cfg.rotating:
            self.lbl_rot.setVisible(False)
            return
        z = self.cfg.rotating[0]
        ma = phys.mach(z.tip_speed, self.cfg.flow.sound_speed)
        text = (f"Кінець лопаті йде {z.tip_speed:.4g} м/с — це M = {ma:.2f} "
                f"при M = {self.cfg.flow.mach:.2f} у набігаючому потоці.")
        # Однакові застороги про однакові гвинти складаємо в одну: на
        # чотиримоторному апараті їх інакше вчетверо більше, і серед копій
        # губиться те, заради чого список і читають.
        notes = fc.collapse_zone_notes(self.cfg.guard_rotating(),
                                       [r.name for r in self.cfg.rotating])
        if notes:
            text += "<br>" + "<br>".join(t for _, t in notes)
        worst = next((l for l, _ in notes if l == "bad"), None)
        colour = "#e05252" if worst else ("#d6a419" if notes else "#7c8894")
        self.lbl_rot.setStyleSheet(f"color:{colour}; font-size:11px;")
        self.lbl_rot.setText(text)
        self.lbl_rot.setVisible(True)

    # --- режим течії ------------------------------------------------------

    def _regime_changed(self, _index=0):
        if self._loading:
            return
        self.cfg.flow.regime = self.cmb_regime.currentData()
        self.cfg.flow.apply_thermo()
        self._sync_regime()
        self.emit_changed()

    def _thermo_changed(self):
        f = self.cfg.flow
        f.t_inf = self.t_inf.value()
        f.p_inf = self.p_inf.value()
        f.apply_thermo()
        self._sync_regime()

    def _sync_regime(self):
        """Показує наслідки вибору режиму: що зникає з рук, що зʼявляється.

        У стисливому nu і rho вже не налаштування, а результат рівняння
        стану. Лишити їх редагованими означало б дати людині крутити ручки,
        які нічого не крутять: OpenFOAM однаково порахує своє.
        """
        f = self.cfg.flow
        on = f.compressible
        self.box_thermo.setVisible(on)
        for w in (self.cmb_media, self.nu, self.rho):
            w.setEnabled(not on)
        if on:
            # _loading глушить bind-обробники: інакше setValue на nu і rho
            # записав би похідні числа назад у конфіг як задані вручну.
            self._loading = True
            self.nu.setValue(f.nu)
            self.rho.setValue(f.rho)
            self._loading = False
            self.lbl_regime.setText(
                f"З цих тиску й температури: rho = {f.rho:.4f} кг/м³, "
                f"nu = {f.nu:.4g} м²/с, швидкість звуку "
                f"{f.sound_speed:.1f} м/с, M = {f.mach:.3f}.")
        else:
            self.lbl_regime.setText("")
        self.lbl_regime.setVisible(on)

    def _show_zone_source(self):
        """Сказати, звідки взялися обертові зони — з ролей чи з полів.

        Зони, зібрані з ролей, перезбираються на кожне оновлення сцени й
        перекривають те, що задано тут руками. Галочка, яка виглядає
        робочою й не робить нічого, — це запрошення крутити її й
        дивуватися, тому поля глушимо й пишемо прямо.
        """
        from_parts = [z for z in self.cfg.rotating
                      if getattr(z, "from_part", False)]
        self.lbl_rot_roles.setVisible(bool(from_parts))
        self.box_rot.setEnabled(not from_parts)
        self.rot_on.setEnabled(not from_parts)
        if not from_parts:
            return
        names = ", ".join(z.name for z in from_parts[:6])
        self.lbl_rot_roles.setText(
            f"Зони беруться З РОЛЕЙ ДЕТАЛЕЙ: їх {len(from_parts)} "
            f"({names}). Кожна зібрана з габариту своєї моделі — свій "
            f"радіус, своє місце, свої оберти. Поля нижче ні на що не "
            f"впливають: оберти й вісь міняйте на вкладці «Геометрія», "
            f"вибравши деталь у списку.")

    def refresh(self):
        f = self.cfg.flow
        self._show_zone_source()
        # МІТКУ ОБЕРТОВИХ ЗОН ТЕЖ ОНОВЛЮЄМО ТУТ.
        #
        # Досі її оновлювало саме лише редагування полів обертання. А зони
        # народжуються не тільки звідти: їх перебудовує кожна зміна ролі
        # деталі й кожне відкриття проєкту — і тоді в мітці лишалося те,
        # що було ДО перебудови.
        #
        # Побачити це з коду було неможливо: cfg увесь час правильний,
        # бреше тільки намальоване. На справжньому проєкті мітка казала
        # «гвинт 224 мм, J = 6.6», тимчасом як у cfg стояло 204 мм і
        # J = 7.3 — тобто показувала радіус зони замість радіуса лопаті.
        self._sync_rotating()
        t = self.cfg.turbulence_inlet()
        re = phys.reynolds(f.u_inf, f.l_ref, f.nu)
        ma = f.mach
        self._check_farfield()
        warn = ""
        if ma > 0.3:
            warn += "\n⚠ M > 0.3 — стисливість уже помітна, simpleFoam занижує опір"
        if phys.is_transitional(re):
            warn += ("\n⚠ Re < 5·10⁵ — пограншар частково ламінарний. RANS "
                     "вважає його турбулентним усюди й завищить опір тертя.")
        d = f.direction
        self.info.setText(
            f"Re            {re:.4g}\n"
            f"Mach          {ma:.3f}\n"
            f"U вектор      ({d[0] * f.u_inf:.4g}, {d[1] * f.u_inf:.4g}, {d[2] * f.u_inf:.4g})\n"
            f"k             {t.k:.5g} м²/с²\n"
            f"omega         {t.omega:.5g} 1/с\n"
            f"epsilon       {t.epsilon:.5g} м²/с³\n"
            f"nut вхід      {t.nut:.4g} м²/с" + warn
        )


# --------------------------------------------------------------------------
# Розв'язувач
# --------------------------------------------------------------------------

class SolverPanel(Panel):
    def __init__(self, cfg, parent=None):
        super().__init__(cfg, parent)
        s = cfg.solver

        self.cmb_time = QtWidgets.QComboBox()
        self.cmb_time.addItem("стаціонарна — один усталений стан", True)
        self.cmb_time.addItem("нестаціонарна — розвиток у часі", False)
        self.cmb_time.setCurrentIndex(0 if s.steady else 1)
        self.cmb_time.currentIndexChanged.connect(self._time_mode_changed)
        self.form.addRow("Постановка", self.cmb_time)
        _b = explain(self.cmb_time, "steady")
        if _b: self.form.addRow(_b)

        self.iters = self.bind(ispin(10, 100000, s.n_iterations, 50),
                               lambda: setattr(s, "n_iterations", self.iters.value()))
        self.write = self.bind(ispin(1, 10000, s.write_interval, 50),
                               lambda: setattr(s, "write_interval", self.write.value()))
        # Стаціонарні поля живуть у власному віджеті, щоб нестаціонар міг
        # сховати їх цілком, не лишаючи по собі порожніх підписів.
        self.box_steady = QtWidgets.QWidget()
        form_steady = QtWidgets.QFormLayout(self.box_steady)
        form_steady.setContentsMargins(0, 0, 0, 0)
        form_steady.addRow("Ітерацій", self.iters)
        _b = explain(self.iters, "iterations")
        if _b: form_steady.addRow(_b)
        form_steady.addRow("Запис кожні", self.write)
        self.v.addWidget(self.box_steady)

        self.box_unsteady = QtWidgets.QWidget()
        form_un = QtWidgets.QFormLayout(self.box_unsteady)
        form_un.setContentsMargins(0, 0, 0, 0)
        self.end_time = self.bind(dspin(1e-6, 1e6, s.end_time, 0.1, 6, " с"),
                                  lambda: self._unsteady_changed(
                                      "end_time", self.end_time.value()))
        self.dt = self.bind(sci(1e-9, 1.0, s.time_step),
                            lambda: self._unsteady_changed(
                                "time_step", self.dt.value()))
        self.write_every = self.bind(dspin(1e-6, 1e5, s.write_every, 0.01, 6, " с"),
                                     lambda: self._unsteady_changed(
                                         "write_every", self.write_every.value()))
        self.courant = self.bind(dspin(0.05, fc.MAX_COURANT, s.max_courant, 0.5, 2),
                                 lambda: self._unsteady_changed(
                                     "max_courant", self.courant.value()))
        self.outer = self.bind(ispin(1, 10, s.n_outer_correctors),
                               lambda: self._unsteady_changed(
                                   "n_outer_correctors", self.outer.value()))
        form_un.addRow("Кінцевий час", self.end_time)
        _b = explain(self.end_time, "end_time")
        if _b: form_un.addRow(_b)
        form_un.addRow("Початковий крок", self.dt)
        form_un.addRow("Запис кожні", self.write_every)
        form_un.addRow("Куранта (стеля)", self.courant)
        _b = explain(self.courant, "courant")
        if _b: form_un.addRow(_b)
        form_un.addRow("Зовнішніх проходів", self.outer)
        self.warm = self.bind(ispin(0, 5000, s.warm_iterations),
                              lambda: self._unsteady_changed(
                                  "warm_iterations", self.warm.value()))
        self.warm.setSuffix(" ітер.")
        self.warm.setSpecialValueText("вимкнено")
        self.warm.setToolTip("Лише для стисливого нестаціонару: перед ним "
                             "стільки ітерацій rhoSimpleFoam.")
        form_un.addRow("Теплий старт", self.warm)
        _b = explain(self.warm, "warm_iterations")
        if _b: form_un.addRow(_b)
        self.v.addWidget(self.box_unsteady)

        # Сюди йде і арифметика («стільки кроків, стільки зрізів»), і сам
        # захист від дурня. Однією міткою, бо це одна думка: у що обійдеться
        # те, що зараз задано.
        self.lbl_time = WrapLabel("")
        self.lbl_time.setWordWrap(True)
        self.v.addWidget(self.lbl_time)

        self.tol = QtWidgets.QComboBox()
        self.tol.addItems(["1e-3", "1e-4", "1e-5", "1e-6"])
        self.tol.setCurrentText("1e-4")
        self.tol.currentTextChanged.connect(
            lambda t: (setattr(s, "residual_tol", float(t)), self.emit_changed()))
        self.form.addRow("Поріг збіжності", self.tol)
        _b = explain(self.tol, "residual_tol")
        if _b: self.form.addRow(_b)
        self.v.addWidget(hint(
            "Розрахунок зупиниться раніше за ліміт ітерацій, щойно всі "
            "резидуали впадуть нижче порога.", "residual_tol"))

        self.v.addWidget(section("Паралельність"))
        self.procs = self.bind(ispin(1, 256, s.n_procs),
                               lambda: setattr(s, "n_procs", self.procs.value()))
        self.form2 = QtWidgets.QFormLayout()
        self.form2.addRow("Процесів", self.procs)
        _b = explain(self.procs, "procs")
        if _b: self.form2.addRow(_b)
        self.cmb_decomp = QtWidgets.QComboBox()
        self.cmb_decomp.addItems(["scotch", "hierarchical", "simple"])
        self.cmb_decomp.currentTextChanged.connect(
            lambda t: (setattr(s, "decompose_method", t), self.emit_changed()))
        self.form2.addRow("Декомпозиція", self.cmb_decomp)
        self.v.addLayout(self.form2)
        self.lbl_res = WrapLabel()
        self.lbl_res.setStyleSheet("color:#777; font-size:11px;")
        self.lbl_res.setWordWrap(True)
        self.v.addWidget(self.lbl_res)

        self.v.addWidget(section("Стабілізація"))
        self.chk_potential = QtWidgets.QCheckBox(
            "Стартувати з potentialFoam (швидша збіжність)")
        self.chk_potential.setChecked(s.potential_init)
        self.chk_potential.toggled.connect(
            lambda v: (setattr(s, "potential_init", v), self.emit_changed()))
        self.v.addWidget(self.chk_potential)

        self.chk_consistent = QtWidgets.QCheckBox("SIMPLEC (consistent yes)")
        self.chk_consistent.setChecked(s.consistent)
        self.chk_consistent.toggled.connect(self._consistent_changed)
        self.v.addWidget(self.chk_consistent)

        self.chk_check = QtWidgets.QCheckBox("Проганяти checkMesh після сітки")
        self.chk_check.setChecked(s.run_check_mesh)
        self.chk_check.toggled.connect(
            lambda v: (setattr(s, "run_check_mesh", v), self.emit_changed()))
        self.v.addWidget(self.chk_check)

        self.chk_stop_bad = QtWidgets.QCheckBox(
            "Зупинятися, якщо checkMesh не пройшов")
        self.chk_stop_bad.setChecked(s.stop_on_bad_mesh)
        self.chk_stop_bad.toggled.connect(
            lambda v: (setattr(s, "stop_on_bad_mesh", v), self.emit_changed()))
        self.v.addWidget(self.chk_stop_bad)

        self.v.addWidget(section("Стійкий старт"))
        self.chk_robust = QtWidgets.QCheckBox(
            "Схеми першого порядку, низька релаксація, стеля на швидкість")
        self.chk_robust.setChecked(s.robust_start)
        self.chk_robust.toggled.connect(self._robust_changed)
        self.v.addWidget(self.chk_robust)
        self.v.addWidget(hint(
            "Вмикайте, якщо розрахунок вибухає в перші сотні ітерацій. "
            "Замість linearUpwind ставиться upwind, замість SIMPLEC 0.9 — "
            "класичний SIMPLE з p 0.3 / U 0.7, додається корекція "
            "неортогональності й обмежувач швидкості через fvOptions. "
            "Розвʼязок виходить розмитішим — це спосіб дійти до збіжності, "
            "а не отримати остаточні цифри. Дійшовши, вимкніть і перезапустіть "
            "із готового поля."))
        self.form_cap = QtWidgets.QFormLayout()
        self.cap = self.bind(dspin(1.05, 10.0, s.velocity_cap_factor, 0.1, 2, " × U"),
                             lambda: setattr(s, "velocity_cap_factor", self.cap.value()))
        self.robust_it = self.bind(
            ispin(0, 20000, s.robust_iterations, 50),
            lambda: setattr(s, "robust_iterations", self.robust_it.value()))
        self.form_cap.addRow("Стеля швидкості", self.cap)
        _b = explain(self.cap, "velocity_cap")
        if _b: self.form_cap.addRow(_b)
        self.form_cap.addRow("Стійких ітерацій", self.robust_it)
        _b = explain(self.robust_it, "robust_iterations")
        if _b: self.form_cap.addRow(_b)
        self.v.addLayout(self.form_cap)
        self.v.addWidget(hint(
            "«Стійких ітерацій» — це двоетапний запуск: стільки перших "
            "ітерацій рахуються стійко, далі розрахунок ПРОДОВЖУЄТЬСЯ з "
            "отриманого поля звичайними схемами другого порядку. Так виходить "
            "і стабільний старт, і точний результат за один прохід. Нуль "
            "вимикає другий етап.", "robust_iterations"))
        self.lbl_cap = QtWidgets.QLabel("")
        self.lbl_cap.setStyleSheet("color:#777; font-size:11px;")
        self.v.addWidget(self.lbl_cap)

        self.form3 = QtWidgets.QFormLayout()
        self.relax_u = self.bind(dspin(0.1, 1.0, s.relaxation_u, 0.05, 2),
                                 lambda: setattr(s, "relaxation_u", self.relax_u.value()))
        self.relax_p = self.bind(dspin(0.05, 1.0, s.relaxation_p, 0.05, 2),
                                 lambda: setattr(s, "relaxation_p", self.relax_p.value()))
        self.form3.addRow("Релаксація U", self.relax_u)
        self.form3.addRow("Релаксація p", self.relax_p)
        self.v.addLayout(self.form3)
        self._consistent_changed(s.consistent)
        self.v.addWidget(hint(
            "З SIMPLEC тиск не релаксують окремо, а решту можна тримати на "
            "0.9 — це помітно швидше за класичний SIMPLE."))

        self.v.addStretch(1)

    def load_cfg(self):
        s = self.cfg.solver

        def apply():
            self.iters.setValue(s.n_iterations)
            self.write.setValue(s.write_interval)
            self.procs.setValue(s.n_procs)
            self.cap.setValue(s.velocity_cap_factor)
            self.robust_it.setValue(s.robust_iterations)
            self.relax_u.setValue(s.relaxation_u)
            self.relax_p.setValue(s.relaxation_p)
            self.cmb_decomp.setCurrentText(s.decompose_method)
            set_tol(self.tol, s.residual_tol)
            self.chk_potential.setChecked(s.potential_init)
            self.chk_check.setChecked(s.run_check_mesh)
            self.chk_stop_bad.setChecked(s.stop_on_bad_mesh)
            self.chk_consistent.setChecked(s.consistent)
            self.chk_robust.setChecked(s.robust_start)
            self.cmb_time.setCurrentIndex(0 if s.steady else 1)
            self.end_time.setValue(s.end_time)
            self.dt.setValue(s.time_step)
            self.write_every.setValue(s.write_every)
            self.courant.setValue(s.max_courant)
            self.outer.setValue(s.n_outer_correctors)
            self.warm.setValue(int(s.warm_iterations))

        self._loaded(apply)
        self._sync_time_mode()
        # Прапорець, який уже стояв у потрібному положенні, не смикне свого
        # обробника — а доступність сусідніх полів залежить саме від них.
        for w in (self.chk_consistent, self.relax_u, self.relax_p):
            w.setEnabled(not s.robust_start)
        if not s.robust_start:
            self.relax_p.setEnabled(not s.consistent)
        self.robust_it.setEnabled(not s.robust_start)

    # --- постановка в часі ------------------------------------------------

    def _time_mode_changed(self, _index=0):
        if self._loading:
            return
        self.cfg.solver.steady = bool(self.cmb_time.currentData())
        self._sync_time_mode()
        self.emit_changed()

    def _unsteady_changed(self, name, value):
        setattr(self.cfg.solver, name, value)
        self._sync_time_mode()

    def _sync_time_mode(self):
        """Показує потрібні поля й одразу — ціну того, що задано.

        Захист від дурня живе в `cfg.guard_unsteady()`, а не тут: те саме
        рішення потрібне і цій мітці, і кнопці «Створити кейс», і воно має
        бути перевірним тестами без жодного вікна.
        """
        s = self.cfg.solver
        self.box_steady.setVisible(s.steady)
        self.box_unsteady.setVisible(not s.steady)
        # Стійкий старт — поняття стаціонарне: у нестаціонарі запобіжником
        # від зриву служить малий крок, а не схеми першого порядку.
        self.chk_robust.setEnabled(s.steady)
        self.robust_it.setEnabled(s.steady and not s.robust_start)
        self.chk_consistent.setEnabled(s.steady and not s.robust_start)
        if s.steady:
            self.lbl_time.setText("")
            self.lbl_time.setVisible(False)
            return

        notes = self.cfg.guard_unsteady()
        worst = ("bad" if any(l == "bad" for l, _ in notes)
                 else "warn" if notes else "ok")
        head = (f"Кроків за часом: {fc.fmt_count(s.n_time_steps)}, "
                f"часових зрізів: {fc.fmt_count(s.n_write_dirs)}, "
                f"проліт домену {self.cfg.flow_through_time:.4g} с "
                f"({s.end_time / max(self.cfg.flow_through_time, 1e-12):.1f} "
                f"прольотів за прогін).")
        text = head + ("<br><br>" + "<br>".join(t for _, t in notes)
                       if notes else "")
        colour = {"bad": "#e05252", "warn": "#d6a419", "ok": "#7c8894"}[worst]
        self.lbl_time.setStyleSheet(f"color:{colour}; font-size:11px;")
        self.lbl_time.setText(text)
        self.lbl_time.setVisible(True)

    def _consistent_changed(self, on):
        self.cfg.solver.consistent = on
        self.relax_p.setEnabled(not on)
        self.emit_changed()

    def _robust_changed(self, on):
        self.cfg.solver.robust_start = on
        # У стійкому режимі релаксацію і SIMPLEC задає він сам — щоб не
        # створювати враження, ніби ці поля ще на щось впливають.
        for w in (self.chk_consistent, self.relax_u, self.relax_p):
            w.setEnabled(not on)
        # Двоетапність і «стійко весь час» — взаємовиключні.
        self.robust_it.setEnabled(not on)
        self.emit_changed()

    def refresh(self):
        s = self.cfg.solver
        cap = self.cfg.flow.u_inf * s.velocity_cap_factor
        if s.robust_start:
            self.lbl_cap.setText(
                f"Увесь прогін стійкий. limitVelocity зріже швидкість вище "
                f"{cap:.4g} м/с.")
        elif s.two_stage:
            self.lbl_cap.setText(
                f"Етап 1: ітерації 1–{s.robust_iterations}, upwind, "
                f"SIMPLE 0.3/0.7, стеля {cap:.4g} м/с.\n"
                f"Етап 2: ітерації {s.robust_iterations + 1}–{s.n_iterations}, "
                f"linearUpwind, SIMPLEC {s.relaxation_u}, стеля знята.")
        else:
            self.lbl_cap.setText(
                "Двоетапність вимкнена — увесь прогін другим порядком.")

    def set_resources(self, cores: int, ram_gb: float, threads: int = 0,
                      where: str = ""):
        """Сказати, скільки ядер є насправді — і чим вони відрізняються.

        Доти тут стояло одне число, взяте з `nproc`, тобто ПОТОКИ. Напис
        казав «12 ядер», а наступним реченням радив не перевищувати
        фізичних — не назвавши, скільки їх. Людина робила єдиний висновок,
        який із такого напису й випливає, і ставила дванадцять процесів на
        шість ядер.
        """
        threads = max(threads, cores)
        # Стеля — по ПОТОКАХ: заборонити те, що фізично можливо, не наша
        # справа. Але радимо по ядрах, і кажемо чому.
        self.procs.setMaximum(max(1, threads))
        place = f"на {where}: " if where else "Доступно: "
        if threads > cores:
            self.lbl_res.setText(
                f"{place}{cores} фізичних ядер ({threads} потоків із "
                f"гіпертредингом), {ram_gb:.1f} ГБ памʼяті."
                + chr(10)
                + f"Ставте не більше {cores}. Два потоки одного ядра ділять "
                  f"кеш і памʼять, а розвʼязувач упирається саме в них — "
                  f"тож {threads} процесів дадуть те саме або повільніше, "
                  f"зате вдвічі більше обміну між підобластями.")
        else:
            self.lbl_res.setText(
                f"{place}{cores} ядер, {ram_gb:.1f} ГБ памʼяті. "
                f"Більше процесів, ніж ядер, користі не дасть.")


# --------------------------------------------------------------------------
# Запуск
# --------------------------------------------------------------------------

class AdvisorPanel(Panel):
    """Показує пораховані налаштування з причинами і застосовує їх."""

    compute_requested = QtCore.Signal()
    apply_requested = QtCore.Signal()

    def __init__(self, cfg, parent=None):
        super().__init__(cfg, parent)

        self.v.insertWidget(0, hint(
            "Помічник читає габарит і діагностику завантаженої моделі, вашу "
            "швидкість і доступну памʼять — і рахує з них розмір комірки, "
            "рівень рефайнменту, кількість шарів і режим розвʼязувача. "
            "Кожне число з поясненням, звідки воно."))

        row = QtWidgets.QHBoxLayout()
        self.btn_calc = QtWidgets.QPushButton("Порахувати")
        self.btn_calc.clicked.connect(self.compute_requested)
        self.btn_apply = QtWidgets.QPushButton("Застосувати все")
        self.btn_apply.clicked.connect(self.apply_requested)
        self.btn_apply.setEnabled(False)
        f = self.btn_apply.font()
        f.setBold(True)
        self.btn_apply.setFont(f)
        row.addWidget(self.btn_calc)
        row.addWidget(self.btn_apply)
        self.v.insertLayout(1, row)

        self.form.addRow("Цільовий y+", self._yplus_widget())

        self.v.addWidget(section("Що змінити"))
        self.table = QtWidgets.QTableWidget(0, 4)
        self.table.setHorizontalHeaderLabels(["Де", "Що", "Було → стане", "Чому"])
        self.table.verticalHeader().setVisible(False)
        self.table.setEditTriggers(QtWidgets.QAbstractItemView.NoEditTriggers)
        self.table.setWordWrap(True)
        hh = self.table.horizontalHeader()
        hh.setSectionResizeMode(0, QtWidgets.QHeaderView.ResizeToContents)
        hh.setSectionResizeMode(1, QtWidgets.QHeaderView.ResizeToContents)
        hh.setSectionResizeMode(2, QtWidgets.QHeaderView.ResizeToContents)
        hh.setSectionResizeMode(3, QtWidgets.QHeaderView.Stretch)
        self.table.setMinimumHeight(230)
        self.v.addWidget(self.table)

        self.v.addWidget(section("Висновки"))
        self.notes = QtWidgets.QTextBrowser()
        self.notes.setMinimumHeight(200)
        self.v.addWidget(self.notes)
        self.v.addStretch(1)

    def _yplus_widget(self):
        self.yplus = dspin(0.5, 300.0, 60.0, 5.0, 1)
        self.yplus.setToolTip(
            "60 — середина лог-шару, надійний вибір із пристінними функціями.\n"
            "Не ставте 5..30: це буферний шар, найгірша зона для них.\n"
            "1 означає розвʼязувати підшар — потрібні десятки шарів і чистий STL.")
        return self.yplus

    def show_plan(self, plan):
        changes = plan.changes
        self.table.setRowCount(len(changes))
        for i, t in enumerate(changes):
            cells = [t.where, t.what, f"{t.current} → {t.suggested}", t.reason]
            for col, text in enumerate(cells):
                item = QtWidgets.QTableWidgetItem(text)
                if t.level != "info" and col == 2:
                    item.setForeground(QtGui.QColor(LEVEL_COLOR[
                        "bad" if t.level == "bad" else "warn"]))
                self.table.setItem(i, col, item)
        self.table.resizeRowsToContents()

        html = []
        for level, text in plan.notes:
            colour = LEVEL_COLOR.get(level, "#888")
            html.append(f'<p style="margin:4px 0; color:{colour}">• {text}</p>')
        if not changes:
            html.insert(0, '<p style="color:#4c9a52"><b>Налаштування вже '
                           'відповідають розрахунку — міняти нічого.</b></p>')
        self.notes.setHtml("".join(html))
        self.btn_apply.setEnabled(bool(changes))


class ScanDialog(QtWidgets.QDialog):
    """Що знайшов «Знайти машини»: галочками — що додати до списку.

    Готові (ssh пускає, є образ) позначені одразу; решту теж можна додати —
    образ можна поставити й потім, — але без галочки, щоб не потрапили в
    прогін випадково. Ті, що вже в списку, показано, але не додаються вдруге.
    """

    def __init__(self, rows: list, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Знайдені машини")
        self._rows = rows
        v = QtWidgets.QVBoxLayout(self)
        n_ready = sum(1 for _m, _w, ready, known in rows if ready and not known)
        v.addWidget(WrapLabel(
            f"Знайдено машин із відкритим ssh: {len(rows)}. Нових готових: "
            f"{n_ready}. Позначте, які додати."))
        self.lst = QtWidgets.QListWidget()
        for m, why, ready, known in rows:
            title = f"{m.name} — {m.addr}" if m.name else m.addr
            mark = "✓" if ready else "!"
            text = f"{mark} {title}: {why}"
            if known:
                text += " (уже в списку)"
            it = QtWidgets.QListWidgetItem(text)
            if known:
                it.setFlags(it.flags() & ~QtCore.Qt.ItemIsEnabled)
            else:
                it.setFlags(it.flags() | QtCore.Qt.ItemIsUserCheckable)
                it.setCheckState(QtCore.Qt.Checked if ready
                                 else QtCore.Qt.Unchecked)
            it.setToolTip(text)
            self.lst.addItem(it)
        self.lst.setMinimumWidth(560)
        v.addWidget(self.lst)
        bb = QtWidgets.QDialogButtonBox()
        self.btn_add = bb.addButton("Додати вибрані",
                                    QtWidgets.QDialogButtonBox.AcceptRole)
        bb.addButton("Скасувати", QtWidgets.QDialogButtonBox.RejectRole)
        bb.accepted.connect(self.accept)
        bb.rejected.connect(self.reject)
        v.addWidget(bb)

    def selected(self) -> list:
        out = []
        for i, (m, _why, ready, known) in enumerate(self._rows):
            it = self.lst.item(i)
            if not known and it.checkState() == QtCore.Qt.Checked:
                # Не готову додаємо вимкненою: у прогін вона піде, лише
                # коли людина сама поставить їй галочку.
                out.append(be.DistMachine(**{**m.to_dict(), "on": ready}))
        return out


class RunPanel(Panel):
    detect_requested = QtCore.Signal()
    install_requested = QtCore.Signal()
    create_requested = QtCore.Signal()
    run_requested = QtCore.Signal()
    run_server_requested = QtCore.Signal()
    # Підхопити прогін, що вже йде на сервері.
    attach_requested = QtCore.Signal()
    stop_requested = QtCore.Signal()
    soft_stop_requested = QtCore.Signal()
    # Список машин розподілу змінився (зберегти).
    dist_machines_changed = QtCore.Signal()
    open_dir_requested = QtCore.Signal()
    fix_layers_requested = QtCore.Signal()

    def __init__(self, cfg, parent=None):
        super().__init__(cfg, parent)

        self.v.insertWidget(0, section("Бекенд OpenFOAM"))
        row = QtWidgets.QHBoxLayout()
        self.cmb_backend = QtWidgets.QComboBox()
        self.btn_detect = QtWidgets.QPushButton("Пошук")
        self.btn_detect.clicked.connect(self.detect_requested)
        row.addWidget(self.cmb_backend, 1)
        row.addWidget(self.btn_detect)
        self.v.addLayout(row)

        self.lbl_backend = WrapLabel("—")
        self.lbl_backend.setWordWrap(True)
        self.lbl_backend.setStyleSheet("font-size:11px; color:#555;")
        self.v.addWidget(self.lbl_backend)

        self.box_install = QtWidgets.QGroupBox("OpenFOAM не знайдено")
        bi = QtWidgets.QVBoxLayout(self.box_install)
        bi.addWidget(hint(
            "У WSL уже є Ubuntu — лишилося поставити сам OpenFOAM. Кнопка "
            "відкриє консоль WSL і виконає команду; apt запитає пароль sudo, "
            "тому крок інтерактивний."))
        self.txt_install = QtWidgets.QPlainTextEdit()
        self.txt_install.setReadOnly(True)
        self.txt_install.setMaximumHeight(70)
        self.txt_install.setStyleSheet("font-family: Consolas, monospace; font-size:11px;")
        bi.addWidget(self.txt_install)
        brow = QtWidgets.QHBoxLayout()
        self.btn_install = QtWidgets.QPushButton("Встановити у WSL")
        self.btn_install.clicked.connect(self.install_requested)
        self.btn_copy = QtWidgets.QPushButton("Копіювати команду")
        self.btn_copy.clicked.connect(self._copy_install)
        brow.addWidget(self.btn_install)
        brow.addWidget(self.btn_copy)
        bi.addLayout(brow)
        self.v.addWidget(self.box_install)
        self.box_install.setVisible(False)

        self.v.addWidget(section("Каталог кейсу"))
        self.ed_dir = QtWidgets.QLineEdit()
        self.ed_name = QtWidgets.QLineEdit(cfg.name)
        self.ed_name.textChanged.connect(
            lambda t: setattr(self.cfg, "name", fc.safe_case_name(t)))
        # Що піде в імʼя теки, видно одразу після вводу: «11:15» стає
        # «11_15» у самому полі, а не мовчки десь на диску.
        self.ed_name.editingFinished.connect(self._tidy_name)
        self.ed_name.setToolTip("Це імʼя теки кейсу: літери, цифри, «_», «.» "
                                "і «-». Решта (двокрапка, пробіл, лапки…) "
                                "заміниться на «_».")
        f2 = QtWidgets.QFormLayout()
        f2.addRow("Каталог", self.ed_dir)
        f2.addRow("Назва", self.ed_name)
        self.v.addLayout(f2)

        srow = QtWidgets.QHBoxLayout()
        srow.addWidget(QtWidgets.QLabel("Сервер"))
        self.ed_server = QtWidgets.QLineEdit()
        self.ed_server.setPlaceholderText("імʼя з ~/.ssh/config або user@host")
        self.ed_server.setClearButtonEnabled(True)
        srow.addWidget(self.ed_server, 1)
        self.btn_server = QtWidgets.QPushButton("Перевірити")
        srow.addWidget(self.btn_server)
        self.v.addLayout(srow)
        self.v.addWidget(hint(
            "Порожнє поле — рахуємо тут. Вписали адресу й натиснули "
            "«Перевірити» — сервер стає ще одним пунктом у списку бекендів "
            "вище, і рахується там, якщо його вибрати. Потрібен ssh без "
            "пароля (ключ) і встановлений OpenFOAM на тому боці; версію "
            "програма покаже сама.", "server"))

        self.chk_native = QtWidgets.QCheckBox(
            "Рахувати в рідній ФС WSL (значно швидше)")
        self.chk_native.setChecked(True)
        self.v.addWidget(self.chk_native)
        self.v.addWidget(hint(
            "Диск Windows видно у WSL через 9p, і файлові операції там у рази "
            "повільніші. Кейс копіюється в ~/openfoam-studio, а результати "
            "повертаються назад після прогону."))

        # ЕКСПЕРИМЕНТАЛЬНО: один прогін на кількох машинах (rust/foamdist).
        self.box_dist = QtWidgets.QGroupBox(
            "Розподілити між машинами (експериментально)")
        self.box_dist.setCheckable(True)
        self.box_dist.setChecked(False)
        dl = QtWidgets.QFormLayout(self.box_dist)
        # КОЖНА МАШИНА — ОКРЕМИЙ ЗАПИС зі своїми назвою, адресою,
        # користувачем і ключем. Доти це був один рядок «адреса:процеси»
        # і ОДИН ключ та користувач на всіх — тобто працювало лише тоді,
        # коли всі машини одного власника в одній мережі.
        self._machines: list = []
        self._probe: dict = {}
        self.lst_dist = QtWidgets.QListWidget()
        self.lst_dist.setMaximumHeight(140)
        self.lst_dist.setToolTip(
            "Машини прогону. Галочка — брати в прогін. Серед них має бути сам "
            "сервер (його адреса в локальній мережі): там лежить кейс і "
            "рахується процес 0.")
        self.btn_m_add = QtWidgets.QPushButton("+ Додати машину")
        self.btn_m_del = QtWidgets.QPushButton("− Прибрати")
        mrow = QtWidgets.QHBoxLayout()
        # ЗНАЙТИ МАШИНИ: сервер сам обходить свою мережу і ~/.ssh/config,
        # а вписувати адреси руками лишається для того, чого він не бачить.
        self.btn_dist_scan = QtWidgets.QPushButton("Знайти машини…")
        self.btn_dist_scan.setToolTip(
            "Сервер перебирає свою локальну мережу й хости зі свого "
            "~/.ssh/config, шукає машини з відкритим ssh і перевіряє, чи "
            "пускає ключ і чи є образ OpenFOAM. Знайдені можна додати "
            "галочками. Займає кілька секунд.")
        mrow.addWidget(self.btn_m_add)
        mrow.addWidget(self.btn_m_del)
        mrow.addWidget(self.btn_dist_scan)
        mrow.addStretch(1)
        self.box_m = QtWidgets.QGroupBox("Вибрана машина")
        ml = QtWidgets.QFormLayout(self.box_m)
        self.chk_m_on = QtWidgets.QCheckBox("Брати в прогін")
        self.ed_m_name = QtWidgets.QLineEdit()
        self.ed_m_name.setPlaceholderText("напр. node1 — сама підставиться "
                                          "після «Перевірити»")
        self.ed_m_addr = QtWidgets.QLineEdit()
        self.ed_m_addr.setPlaceholderText("10.0.0.21")
        self.sp_m_procs = QtWidgets.QSpinBox()
        self.sp_m_procs.setRange(1, 256)
        self.sp_m_procs.setToolTip("Не більше фізичних ядер машини.")
        self.ed_m_user = QtWidgets.QLineEdit()
        self.ed_m_user.setPlaceholderText("типовий (нижче)")
        self.ed_m_key = QtWidgets.QLineEdit()
        self.ed_m_key.setPlaceholderText("типовий (нижче)")
        self.ed_m_key.setToolTip(
            "Шлях до ключа НА СЕРВЕРІ, яким він заходить саме на цю машину. "
            "Для самого сервера не потрібен.")
        ml.addRow(self.chk_m_on)
        ml.addRow("Назва", self.ed_m_name)
        ml.addRow("Адреса (IP)", self.ed_m_addr)
        ml.addRow("Процеси", self.sp_m_procs)
        ml.addRow("Користувач", self.ed_m_user)
        ml.addRow("Ключ на сервері", self.ed_m_key)
        self.ed_dist_key = QtWidgets.QLineEdit("~/.ssh/id_ed25519")
        self.ed_dist_key.setToolTip("Ключ НА СЕРВЕРІ для машин, у яких свій "
                                    "не вказано.")
        self.ed_dist_user = QtWidgets.QLineEdit()
        self.ed_dist_user.setToolTip("Користувач для машин, у яких свій не "
                                     "вказано.")
        self.ed_dist_net = QtWidgets.QLineEdit()
        self.ed_dist_net.setPlaceholderText("авто — з адрес машин (/24)")
        self.ed_dist_net.setToolTip(
            "Мережа, якою процеси обмінюються даними (MPI), напр. "
            "10.0.0.0/24; кілька — через кому. Порожньо — з адрес машин.")
        self.lst_dist.currentRowChanged.connect(self._m_load)
        self.lst_dist.itemChanged.connect(self._m_item_checked)
        self.btn_m_add.clicked.connect(self._m_add)
        self.btn_m_del.clicked.connect(self._m_del)
        for ed in (self.ed_m_name, self.ed_m_addr, self.ed_m_user, self.ed_m_key):
            ed.editingFinished.connect(self._m_save)
        self.sp_m_procs.valueChanged.connect(lambda _v: self._m_save())
        self.chk_m_on.toggled.connect(lambda _v: self._m_save())
        self.btn_dist_probe = QtWidgets.QPushButton("Перевірити машини")
        self.chk_dist_rebench = QtWidgets.QCheckBox("Переміряти швидкість")
        self.chk_dist_rebench.setToolTip(
            "Швидкість кожної машини міряється коротким бенчмарком і "
            "памʼятається на сервері (повтор — коли змінився процесор, минуло "
            "30 днів або минулий замір був під навантаженням). З галочкою — "
            "переміряти всі машини наступною перевіркою.")
        self.lbl_dist = WrapLabel("")
        self.lbl_dist.hide_when_empty = True
        dl.addRow(self.lst_dist)
        dl.addRow(mrow)
        dl.addRow(self.box_m)
        dl.addRow("Типовий ключ", self.ed_dist_key)
        dl.addRow("Типовий користувач", self.ed_dist_user)
        dl.addRow("Мережа MPI", self.ed_dist_net)
        prow = QtWidgets.QHBoxLayout()
        prow.addWidget(self.btn_dist_probe)
        prow.addWidget(self.chk_dist_rebench)
        prow.addStretch(1)
        dl.addRow(prow)
        dl.addRow(self.lbl_dist)
        dl.addRow(hint(
            "Сітку будує сервер, як завжди, — одразу на стільки шматків, "
            "скільки разом процесів. Розвʼязувач іде на всіх машинах: кожна "
            "отримує лише свої шматки, наприкінці вони повертаються на "
            "сервер. Усі процеси — в однаковому контейнері OpenFOAM "
            "(opencfd/openfoam-run:2512). Шматки — за швидкістю: кожна "
            "машина один раз проходить короткий бенчмарк (памʼятається на "
            "сервері), а зайнята зараз отримує менше. Виміряно: 3 машини по "
            "4 ядра — утричі швидше за одну. Лише для «Запустити на сервері».",
            "dist"))
        self.v.addWidget(self.box_dist)
        self._m_load(-1)

        # ЗБЕРЕЖЕНА СІТКА. Готова сітка лежить на сервері під ключем
        # «геометрія + налаштування сітки» і береться без snappyHexMesh, навіть
        # на іншу кількість процесів. Галочка — будувати однаково (і
        # зберегти нову); знімається після запуску.
        self.chk_remesh = QtWidgets.QCheckBox(
            "Перебудувати сітку, навіть якщо є збережена")
        self.chk_remesh.setToolTip(
            "Без галочки: якщо геометрія й налаштування сітки ті самі, що в "
            "одному з минулих прогонів, сітку не будують, а беруть готову й "
            "лише ріжуть на потрібну кількість процесів. Галочка — збудувати "
            "заново (і зберегти нову). Знімається після запуску.")
        self.v.addWidget(self.chk_remesh)

        self.v.addWidget(section("Дії"))

        # СЕРІЯ КУТІВ ДЛЯ НЕЙТРАЛЬНОЇ ТОЧКИ.
        #
        # Точку з одного кута не знайти — потрібна похідна моменту, тобто
        # кілька прогонів, що різняться ЛИШЕ кутом. Руками це три цикли
        # «змінити кут — перейменувати кейс — запустити — дочекатися», і
        # помилитися легко в кожному. З галочкою обидві кнопки запуску
        # рахують увесь список одним заходом, а наприкінці відкривають
        # порівняння з нейтральною точкою.
        self.chk_series = QtWidgets.QCheckBox(
            "Серія кутів атаки → нейтральна точка")
        self.chk_series.setToolTip(
            "Запустити прогони один за одним, по одному на кожен кут, і "
            "наприкінці порахувати нейтральну точку й де ставити центр ваги.")
        self.ed_angles = QtWidgets.QLineEdit(lib.DEFAULT_SERIES)
        self.ed_angles.setPlaceholderText("напр. 0, 3, 6")
        self.ed_angles.setToolTip(
            "Кути атаки в градусах через кому чи пробіл. Із крапкою з комою "
            "кома читається як десяткова: «0; 2,5; 5».")
        self.ed_angles.setEnabled(False)
        self.lbl_series = WrapLabel("")
        self.lbl_series.hide_when_empty = True
        self.lbl_series.setVisible(False)
        self.lbl_series.setWordWrap(True)
        self.lbl_series.setStyleSheet("font-size:11px; color:#555;")
        self.chk_series.toggled.connect(self.ed_angles.setEnabled)
        self.chk_series.toggled.connect(self.update_series_label)
        self.ed_angles.textChanged.connect(self.update_series_label)
        self.ed_name.textChanged.connect(self.update_series_label)
        arow = QtWidgets.QHBoxLayout()
        arow.addWidget(QtWidgets.QLabel("Кути, °"))
        arow.addWidget(self.ed_angles, 1)
        self.v.addWidget(self.chk_series)
        self.v.addLayout(arow)
        self.v.addWidget(self.lbl_series)
        self.v.addWidget(hint(
            "Кожен кут — окремий кейс із кутом у назві (назва_aoa3). Сітка, "
            "розвʼязувач і решта налаштувань у всіх однакові — інакше "
            "похідна порівнювала б різні постановки. Беріть кути в лінійній "
            "ділянці, до зриву: нейтральна точка — це нахил моменту, а на "
            "відриві стаціонарний розвʼязувач не сходиться. «Стоп» зупиняє "
            "всю серію."))

        grid = QtWidgets.QGridLayout()
        self.btn_create = QtWidgets.QPushButton("Створити кейс")
        self.btn_run = QtWidgets.QPushButton("▶  Запустити")
        self.btn_stop = QtWidgets.QPushButton("■  Стоп")
        self.btn_open = QtWidgets.QPushButton("Відкрити каталог")
        self.btn_create.clicked.connect(self.create_requested)
        self.btn_run.clicked.connect(self.run_requested)
        self.btn_stop.clicked.connect(self.stop_requested)
        self.btn_open.clicked.connect(self.open_dir_requested)
        self.btn_stop.setEnabled(False)
        self.btn_stop.setToolTip(
            "Обірвати одразу. Пораховане після останнього запланованого "
            "зрізу пропаде, а зріз у паралельному прогоні лишиться "
            "незібраним у processor*/.")
        # МʼЯКА ЗУПИНКА. «Стоп» убиває, і OpenFOAM при цьому нічого не
        # дописує: на нестаціонарі це година рахунку в нікуди. Ця кнопка
        # просить розвʼязувач дописати поточний момент, а прогін — зібрати
        # його й перевірити, що він справді є.
        self.btn_soft_stop = QtWidgets.QPushButton("■  Стоп зі зрізом")
        self.btn_soft_stop.setToolTip(
            "Розвʼязувач допише поточний момент (до одного кроку) і "
            "зупиниться. Далі зріз буде зібрано (reconstructPar) і "
            "перевірено — його можна відкрити в «Результатах». Працює, "
            "лише поки розвʼязувач рахує, а не будує сітку.")
        self.btn_soft_stop.clicked.connect(self.soft_stop_requested)
        self.btn_soft_stop.setEnabled(False)
        f = self.btn_run.font()
        f.setBold(True)
        self.btn_run.setFont(f)

        # ОКРЕМА КНОПКА ДЛЯ СЕРВЕРА.
        #
        # Рахувати на сервері й доти було можна, але шлях складався з
        # чотирьох кроків, і третій — «вибрати сервер у списку бекендів» —
        # найлегше проминути. Тоді «Запустити» мовчки рахує на ноуті, і
        # дізнатися про це можна хіба з того, що вентилятор загув.
        #
        # Ця кнопка робить усі кроки сама: перевіряє сервер, вибирає його
        # бекендом і запускає. Помилитися нема де.
        self.btn_run_server = QtWidgets.QPushButton("▶  Запустити на сервері")
        self.btn_run_server.setFont(f)
        self.btn_run_server.clicked.connect(self.run_server_requested)

        # ПІДКЛЮЧИТИСЯ ДО ПРОГОНУ, ЯКИЙ УЖЕ ЙДЕ.
        #
        # Прогін на сервері пускається відчепленим і живе сам по собі, тож
        # закрита програма йому нічого не робить. Але доти не було чим
        # повернутися до нього: журнал лився в те вікно, якого вже немає.
        # Ця кнопка питає сервер, що там діється, і дає підхопити будь-який
        # прогін — хоч той, що його пустили вчора з іншого ноута.
        self.btn_attach = QtWidgets.QPushButton("⇄  Підключитися до прогону")
        self.btn_attach.setToolTip(
            "Показати прогони, що є на сервері, і підхопити журнал того, "
            "який виберете. Працює й для прогону, пущеного іншою "
            "програмою чи з іншого компʼютера.")
        self.btn_attach.clicked.connect(self.attach_requested)
        grid.addWidget(self.btn_create, 0, 0)
        grid.addWidget(self.btn_open, 0, 1)
        grid.addWidget(self.btn_run, 1, 0)
        grid.addWidget(self.btn_stop, 1, 1)
        grid.addWidget(self.btn_soft_stop, 2, 0, 1, 2)
        grid.addWidget(self.btn_run_server, 3, 0, 1, 2)
        grid.addWidget(self.btn_attach, 4, 0, 1, 2)
        self.v.addLayout(grid)
        self.v.addWidget(hint(
            "Кнопка сама перевірить сервер, вибере його бекендом і "
            "запустить. Звичайне «Запустити» рахує там, що вибрано в "
            "списку бекендів угорі, — а це найчастіше цей комп'ютер."
            + chr(10) + chr(10)
            + "Прогін на сервері не залежить від цього вікна: його "
            "можна закрити, а потім повернутися кнопкою «Підключитися».",
            "server"))

        self.progress = QtWidgets.QProgressBar()
        self.progress.setTextVisible(True)
        self.progress.setFormat("готовий")
        self.v.addWidget(self.progress)

        # Окремий індикатор «чи можна вимикати ноут» для прогону на сервері.
        # Три стани, і кожен — відповідь на одне питання людини.
        self.lbl_laptop = QtWidgets.QLabel("")
        self.lbl_laptop.setWordWrap(True)
        self.lbl_laptop.setVisible(False)
        self.v.addWidget(self.lbl_laptop)

        self.lbl_status = WrapLabel("")
        self.lbl_status.hide_when_empty = True
        self.lbl_status.setVisible(False)
        self.lbl_status.setWordWrap(True)
        self.v.addWidget(self.lbl_status)

        # Зʼявляється після прогону, у якому шари наросли не всюди: порада
        # без кнопки означає, що людина мусить перекладати її в поля руками.
        self.btn_fix_layers = QtWidgets.QPushButton("Полагодити шари")
        self.btn_fix_layers.clicked.connect(self.fix_layers_requested)
        self.btn_fix_layers.setVisible(False)
        self.v.addWidget(self.btn_fix_layers)
        self.v.addStretch(1)

    def show_layer_fix(self, diagnosis) -> None:
        """Показує кнопку, лише коли справді є що виправляти."""
        show = bool(diagnosis is not None and diagnosis.fixable
                    and diagnosis.level in ("warn", "bad"))
        if show:
            self.btn_fix_layers.setToolTip("\n".join(
                f"{t.what}: {t.current} → {t.suggested} — {t.reason}"
                for t in diagnosis.tips))
        self.btn_fix_layers.setVisible(show)

    def _copy_install(self):
        QtWidgets.QApplication.clipboard().setText(self.txt_install.toPlainText())
        self.lbl_status.setText("Команду скопійовано в буфер обміну.")

    def set_backends(self, backends, install_script=""):
        self.cmb_backend.clear()
        for b in backends:
            mark = "" if b.ready else "  (не готовий)"
            self.cmb_backend.addItem(f"{b.label}{mark}", b)
        if not backends:
            self.cmb_backend.addItem("нічого не знайдено", None)
        self.txt_install.setPlainText(install_script)
        self.update_backend_label()

    def current_backend(self):
        return self.cmb_backend.currentData()

    def select_ssh_backend(self) -> bool:
        """Вибрати сервер у списку бекендів. False — серверів там немає."""
        for i in range(self.cmb_backend.count()):
            b = self.cmb_backend.itemData(i)
            if b is not None and b.kind == "ssh" and b.ready:
                self.cmb_backend.setCurrentIndex(i)
                self.update_backend_label()
                return True
        return False

    def update_backend_label(self):
        b = self.current_backend()
        if b is None:
            self.lbl_backend.setText("Бекенд не вибрано.")
            self.box_install.setVisible(True)
            self._name_run_button(None)
            return
        if b.ready:
            self.lbl_backend.setText(f"OpenFOAM {b.version}\n{b.detail}")
        else:
            self.lbl_backend.setText(b.detail)
        self.box_install.setVisible(not b.ready)
        self._name_run_button(b)

    def _name_run_button(self, b):
        """Кнопка має казати, ДЕ рахуватиме.

        «Запустити» без уточнення читається як «запустити там, де я думаю»,
        а рахує там, що стоїть у списку бекендів. Різниця між ноутом і
        сервером — це години, і помітити її постфактум важко.
        """
        if b is None:
            self.btn_run.setText("▶  Запустити")
            self.btn_run.setToolTip("Спершу знайдіть OpenFOAM кнопкою «Пошук».")
            return
        where = b.host if b.kind == "ssh" and b.host else b.label
        self.btn_run.setText(f"▶  Запустити: {where}")
        self.btn_run.setToolTip(
            f"Рахувати на «{b.label}» — це те, що вибрано в списку "
            f"бекендів угорі.")

    def series_angles(self) -> tuple[list[float], str]:
        """Кути серії з поля: (кути, чому не так — або "")."""
        return lib.parse_angles(self.ed_angles.text())

    def _tidy_name(self) -> None:
        text = self.ed_name.text()
        safe = fc.safe_case_name(text)
        if safe != text.strip():
            self.ed_name.setText(safe)

    def update_series_label(self, *_):
        """Під галочкою — що саме буде: скільки прогонів і як звуться кейси.

        Серія — це години рахунку, і дізнатися, що «0,3,6» прочиталося як
        один кут, краще до натискання, а не після.
        """
        if not self.chk_series.isChecked():
            self.lbl_series.setText("")
            return
        angles, why = self.series_angles()
        if why:
            self.lbl_series.setStyleSheet("font-size:11px; color:#c0392b;")
            self.lbl_series.setText(f"Серію не запустити: {why}.")
            return
        base = self.cfg.name or "case"
        notes = lib.series_notes(angles)
        self.lbl_series.setStyleSheet(
            "font-size:11px; color:%s;" % ("#b9770e" if notes else "#555"))
        self.lbl_series.setText(
            f"{lib.runs_word(len(angles))} один за одним: "
            + ", ".join(f"{a:g}°" for a in angles)
            + f". Кейси {lib.series_case_name(base, angles[0])} … "
            + f"{lib.series_case_name(base, angles[-1])}."
            + "".join(f" Увага: {n}." for n in notes))

    # стан → (текст, колір тла, колір тексту)
    LAPTOP_STATES = {
        "upload": ("⏳  Вивантажую кейс на сервер — НЕ вимикайте ноут",
                   "#5c3b00", "#ffd27a"),
        "free": ("✔  Сервер рахує сам — ноут МОЖНА вимикати. Потім: "
                 "«Підключитися до прогону»", "#123d1f", "#7ee787"),
        "fetch": ("⬇  Повертаю результати на ноут — НЕ вимикайте ноут",
                  "#5c3b00", "#ffd27a"),
    }

    def set_laptop_state(self, state: str) -> None:
        """'upload' | 'free' | 'fetch' | '' (сховати)."""
        got = self.LAPTOP_STATES.get(state)
        if not got:
            self.lbl_laptop.setVisible(False)
            self.lbl_laptop.setProperty("state", "")
            return
        text, bg, fg = got
        self.lbl_laptop.setText(text)
        self.lbl_laptop.setProperty("state", state)
        self.lbl_laptop.setStyleSheet(
            f"background:{bg}; color:{fg}; font-weight:600; padding:6px 8px;"
            f"border-radius:4px;")
        self.lbl_laptop.setVisible(True)

    def laptop_state(self) -> str:
        return str(self.lbl_laptop.property("state") or "")

    # --- машини розподілу -------------------------------------------------

    def set_machines(self, machines: list) -> None:
        self._machines = [be.DistMachine(**m.to_dict()) for m in machines]
        self._m_refresh()
        self.lst_dist.setCurrentRow(0 if self._machines else -1)
        self._m_load(self.lst_dist.currentRow())

    def machines(self) -> list:
        self._m_save(emit=False)
        return [be.DistMachine(**m.to_dict()) for m in self._machines]

    def set_probe(self, got: list) -> None:
        """Відповіді `foamdist probe`: позначки ✓/✗ і назви з hostname там,
        де людина назви не вписала."""
        self._probe = {g.get("host"): g for g in got}
        named = False
        for m in self._machines:
            g = self._probe.get(m.addr)
            if g and g.get("ok") and not m.name and g.get("hostname"):
                m.name = str(g["hostname"])
                named = True
        self._m_refresh()
        self._m_load(self.lst_dist.currentRow())
        if named:
            self.dist_machines_changed.emit()

    def _m_text(self, m) -> str:
        g = self._probe.get(m.addr)
        mark = "" if g is None else ("✓ " if g.get("ok") else "✗ ")
        who = f" · {m.user}" if m.user else ""
        own = " · свій ключ" if m.key else ""
        return (f"{mark}{m.name or '(без назви)'} — {m.addr or '?'} × "
                f"{m.procs}{who}{own}")

    def _m_refresh(self) -> None:
        row = self.lst_dist.currentRow()
        self.lst_dist.blockSignals(True)
        self.lst_dist.clear()
        for m in self._machines:
            it = QtWidgets.QListWidgetItem(self._m_text(m))
            it.setFlags(it.flags() | QtCore.Qt.ItemIsUserCheckable)
            it.setCheckState(QtCore.Qt.Checked if m.on else QtCore.Qt.Unchecked)
            g = self._probe.get(m.addr)
            if g is not None and not g.get("ok"):
                it.setToolTip(str(g.get("error", ""))[:300])
            self.lst_dist.addItem(it)
        if 0 <= row < len(self._machines):
            self.lst_dist.setCurrentRow(row)
        self.lst_dist.blockSignals(False)

    def _m_load(self, row: int) -> None:
        ok = 0 <= row < len(self._machines)
        self.box_m.setEnabled(ok)
        self.btn_m_del.setEnabled(ok)
        m = self._machines[row] if ok else be.DistMachine()
        for w in (self.chk_m_on, self.ed_m_name, self.ed_m_addr,
                  self.sp_m_procs, self.ed_m_user, self.ed_m_key):
            w.blockSignals(True)
        self.chk_m_on.setChecked(m.on)
        self.ed_m_name.setText(m.name)
        self.ed_m_addr.setText(m.addr)
        self.sp_m_procs.setValue(m.procs)
        self.ed_m_user.setText(m.user)
        self.ed_m_key.setText(m.key)
        for w in (self.chk_m_on, self.ed_m_name, self.ed_m_addr,
                  self.sp_m_procs, self.ed_m_user, self.ed_m_key):
            w.blockSignals(False)

    def _m_save(self, emit: bool = True) -> None:
        row = self.lst_dist.currentRow()
        if not 0 <= row < len(self._machines):
            return
        m = self._machines[row]
        new = be.DistMachine(addr=self.ed_m_addr.text().strip(),
                             procs=self.sp_m_procs.value(),
                             name=self.ed_m_name.text().strip(),
                             user=self.ed_m_user.text().strip(),
                             key=self.ed_m_key.text().strip(),
                             on=self.chk_m_on.isChecked())
        if new == m:
            return
        self._machines[row] = new
        it = self.lst_dist.item(row)
        if it is not None:
            self.lst_dist.blockSignals(True)
            it.setText(self._m_text(new))
            it.setCheckState(QtCore.Qt.Checked if new.on else QtCore.Qt.Unchecked)
            self.lst_dist.blockSignals(False)
        if emit:
            self.dist_machines_changed.emit()

    def _m_item_checked(self, it) -> None:
        row = self.lst_dist.row(it)
        if 0 <= row < len(self._machines):
            on = it.checkState() == QtCore.Qt.Checked
            if self._machines[row].on != on:
                self._machines[row].on = on
                if row == self.lst_dist.currentRow():
                    self._m_load(row)
                self.dist_machines_changed.emit()

    def _m_add(self) -> None:
        self._m_save()
        self._machines.append(be.DistMachine())
        self._m_refresh()
        self.lst_dist.setCurrentRow(len(self._machines) - 1)
        self._m_load(len(self._machines) - 1)
        self.ed_m_addr.setFocus()
        self.dist_machines_changed.emit()

    def _m_del(self) -> None:
        row = self.lst_dist.currentRow()
        if 0 <= row < len(self._machines):
            del self._machines[row]
            self._m_refresh()
            row = min(row, len(self._machines) - 1)
            self.lst_dist.setCurrentRow(row)
            self._m_load(row)
            self.dist_machines_changed.emit()

    def set_running(self, running: bool):
        self.btn_run.setEnabled(not running)
        self.btn_run_server.setEnabled(not running)
        self.btn_create.setEnabled(not running)
        self.btn_stop.setEnabled(running)
        self.btn_soft_stop.setEnabled(running)
        # Кути посеред серії не міняють: прогін, що йде, їх уже не почує,
        # а наступні пішли б уже з іншого списку.
        self.chk_series.setEnabled(not running)
        self.ed_angles.setEnabled(not running and self.chk_series.isChecked())
        if running:
            # Порада з минулого прогону стосувалася минулої сітки.
            self.btn_fix_layers.setVisible(False)


# --------------------------------------------------------------------------
# Збережені симуляції
# --------------------------------------------------------------------------

class SortItem(QtWidgets.QTableWidgetItem):
    """Клітинка, що сортується за числом, а не за своїм текстом.

    Інакше «1.2 ГБ» стало б менше за «980 МБ», а 90 тис. комірок — більше за
    488 тис.: сортування за рядком порівнює символи.
    """

    def __init__(self, text: str, key=None):
        super().__init__(text)
        self.key = key
        self.setFlags(QtCore.Qt.ItemIsSelectable | QtCore.Qt.ItemIsEnabled)

    def __lt__(self, other):
        a = self.key if self.key is not None else -math.inf
        b = getattr(other, "key", None)
        if b is None:
            b = -math.inf
        try:
            return a < b
        except TypeError:
            return str(a) < str(b)


class LibraryPanel(QtWidgets.QWidget):
    """Список кейсів у каталозі: що вже прораховано і з чим.

    Кейс на диску — єдине, що лишається від симуляції після закриття вікна,
    тому вкладка читає саме диск, а не якийсь власний список: кейси
    зʼявляються тут і тоді, коли їх створили руками, скопіювали з іншої
    машини чи дорахували через `Allrun` у консолі.
    """

    COLUMNS = ("Назва", "Змінено", "Стан", "Прогін", "Комірок", "Ітерацій",
               "Cd", "Cl", "U, м/с", "α°", "Модель", "Розмір")

    open_requested = QtCore.Signal(str)
    paraview_requested = QtCore.Signal(str)
    changed_on_disk = QtCore.Signal(str)

    def __init__(self, root: str = "", parent=None):
        super().__init__(parent)
        self.root = str(root)
        self.cases: list = []
        self.loaded = False
        self._notes_path = ""
        self._notes_text = ""

        lay = QtWidgets.QVBoxLayout(self)
        lay.setContentsMargins(6, 6, 6, 6)
        lay.setSpacing(4)

        bar = QtWidgets.QHBoxLayout()
        self.ed_root = QtWidgets.QLineEdit(self.root)
        self.ed_root.setReadOnly(True)
        self.btn_browse = QtWidgets.QPushButton("Огляд…")
        self.btn_refresh = QtWidgets.QPushButton("Оновити")
        self.ed_filter = QtWidgets.QLineEdit()
        self.ed_filter.setPlaceholderText("фільтр за назвою, моделлю, приміткою")
        self.ed_filter.setClearButtonEnabled(True)
        self.btn_browse.clicked.connect(self._browse)
        self.btn_refresh.clicked.connect(self.refresh)
        self.ed_filter.textChanged.connect(lambda _: self._apply_filter())
        bar.addWidget(QtWidgets.QLabel("Каталог"))
        bar.addWidget(self.ed_root, 2)
        bar.addWidget(self.btn_browse)
        bar.addWidget(self.btn_refresh)
        bar.addWidget(self.ed_filter, 1)
        lay.addLayout(bar)

        self.table = QtWidgets.QTableWidget(0, len(self.COLUMNS))
        self.table.setHorizontalHeaderLabels(self.COLUMNS)
        self.table.verticalHeader().setVisible(False)
        self.table.setSelectionBehavior(QtWidgets.QAbstractItemView.SelectRows)
        self.table.setSelectionMode(QtWidgets.QAbstractItemView.ExtendedSelection)
        self.table.setEditTriggers(QtWidgets.QAbstractItemView.NoEditTriggers)
        self.table.setSortingEnabled(True)
        self.table.setAlternatingRowColors(True)
        self.table.setMinimumHeight(180)
        head = self.table.horizontalHeader()
        head.setSectionResizeMode(QtWidgets.QHeaderView.ResizeToContents)
        head.setSectionResizeMode(0, QtWidgets.QHeaderView.Stretch)
        self.table.itemSelectionChanged.connect(self._selection_changed)
        self.table.doubleClicked.connect(lambda _: self._open())

        self.details = QtWidgets.QTextBrowser()
        self.details.setOpenExternalLinks(False)
        self.details.setMinimumHeight(140)

        notes_box = QtWidgets.QWidget()
        nl = QtWidgets.QVBoxLayout(notes_box)
        nl.setContentsMargins(0, 0, 0, 0)
        nl.setSpacing(3)
        nl.addWidget(section("Примітка до кейсу"))
        self.ed_notes = QtWidgets.QPlainTextEdit()
        self.ed_notes.setPlaceholderText(
            "Що саме перевірялося цим прогоном і що з нього вийшло.\n"
            "Зберігається в studio.json поряд із кейсом.")
        self.ed_notes.setMinimumHeight(90)
        self.btn_notes = QtWidgets.QPushButton("Зберегти примітку")
        self.btn_notes.clicked.connect(self._save_notes)
        nl.addWidget(self.ed_notes, 1)
        nl.addWidget(self.btn_notes)

        lower = QtWidgets.QSplitter(QtCore.Qt.Horizontal)
        lower.addWidget(self.details)
        lower.addWidget(notes_box)
        lower.setStretchFactor(0, 3)
        lower.setStretchFactor(1, 2)

        split = QtWidgets.QSplitter(QtCore.Qt.Vertical)
        split.addWidget(self.table)
        split.addWidget(lower)
        split.setStretchFactor(0, 3)
        split.setStretchFactor(1, 2)
        lay.addWidget(split, 1)

        row = QtWidgets.QHBoxLayout()
        self.btn_open = QtWidgets.QPushButton("Відкрити")
        self.btn_open.setToolTip(
            "Перенести налаштування кейсу в усі вкладки, завантажити його STL "
            "і результати")
        f = self.btn_open.font()
        f.setBold(True)
        self.btn_open.setFont(f)
        self.btn_compare = QtWidgets.QPushButton("Порівняти вибрані")
        self.btn_para = QtWidgets.QPushButton("У ParaView")
        self.btn_dir = QtWidgets.QPushButton("Каталог")
        self.btn_rename = QtWidgets.QPushButton("Перейменувати…")
        self.btn_slim = QtWidgets.QPushButton("Звільнити місце…")
        self.btn_delete = QtWidgets.QPushButton("Видалити…")
        self.btn_open.clicked.connect(self._open)
        self.btn_compare.clicked.connect(self._compare)
        self.btn_para.clicked.connect(self._paraview)
        self.btn_dir.clicked.connect(self._open_dir)
        self.btn_rename.clicked.connect(self._rename)
        self.btn_slim.clicked.connect(self._slim)
        self.btn_delete.clicked.connect(self._delete)
        for b in (self.btn_open, self.btn_compare, self.btn_para, self.btn_dir,
                  self.btn_rename, self.btn_slim, self.btn_delete):
            row.addWidget(b)
        row.addStretch(1)
        lay.addLayout(row)

        self.lbl_status = WrapLabel("")
        self.lbl_status.hide_when_empty = True
        self.lbl_status.setVisible(False)
        self.lbl_status.setWordWrap(True)
        lay.addWidget(self.lbl_status)

        self._selection_changed()

    # --- дані ------------------------------------------------------------

    def set_root(self, root: str):
        if str(root) != self.root:
            self.root = str(root)
            self.ed_root.setText(self.root)
            self.loaded = False

    def refresh(self):
        """Перечитує каталог. Розмір кейсу рахується обходом дерева, тому
        список оновлюється на вимогу, а не сам собою на кожен чих."""
        QtWidgets.QApplication.setOverrideCursor(QtCore.Qt.WaitCursor)
        try:
            self.cases = lib.scan(self.root)
        finally:
            QtWidgets.QApplication.restoreOverrideCursor()
        self.loaded = True
        self._fill()

    def refresh_if_needed(self):
        if not self.loaded:
            self.refresh()

    def _fill(self):
        keep = self._current_path()
        self.table.setSortingEnabled(False)
        self.table.setRowCount(len(self.cases))
        for r, c in enumerate(self.cases):
            cd, cl = c.coeff("Cd"), c.coeff("Cl")
            cells = [
                SortItem(c.name, c.name.lower()),
                SortItem(lib.fmt_time(c.modified), c.modified),
                SortItem(c.state, c.state),
                SortItem(c.run_status or "—", c.run_status),
                SortItem(phys.fmt_cells(c.cells) if c.cells else "—", c.cells),
                # Показуємо ітерації чи секунди — залежно від постановки,
                # а сортуємо однаково по кроках: інакше стовпець із двома
                # різними одиницями сортувався б безглуздо.
                SortItem(c.progress_label[1], c.iterations),
                SortItem(_fmt_coeff(cd), cd[0] if cd else None),
                SortItem(_fmt_coeff(cl), cl[0] if cl else None),
                SortItem(_fmt_num(c.u_inf), c.u_inf),
                SortItem(_fmt_num(c.aoa, "%g"), c.aoa),
                SortItem(c.turbulence or "—", c.turbulence or ""),
                SortItem(lib.fmt_size(c.size), c.size),
            ]
            tip = c.path + (("\n\n" + c.notes) if c.notes else "")
            for col, item in enumerate(cells):
                item.setData(QtCore.Qt.UserRole, c.path)
                item.setToolTip(tip)
                if col >= 4:
                    item.setTextAlignment(QtCore.Qt.AlignRight
                                          | QtCore.Qt.AlignVCenter)
                self.table.setItem(r, col, item)
            if not c.has_cfg:
                # Кейс без маніфесту відкривається лише на перегляд.
                self.table.item(r, 0).setForeground(QtGui.QColor("#888"))
            if c.last_run and not c.last_run.get("ok"):
                self.table.item(r, 3).setForeground(
                    QtGui.QColor(LEVEL_COLOR["bad"]))
        self.table.setSortingEnabled(True)
        self._apply_filter()

        if keep:
            self.select(keep)
        self.lbl_status.setText(
            "" if self.cases else
            f"У каталозі {self.root} кейсів немає. Створіть кейс на вкладці "
            f"«Запуск» — він зʼявиться тут.")
        self._selection_changed()

    def _apply_filter(self):
        needle = self.ed_filter.text().strip().lower()
        for r in range(self.table.rowCount()):
            info = self._info_at(r)
            if info is None:
                continue
            hay = " ".join([info.name, info.notes, info.turbulence or "",
                            info.state]).lower()
            self.table.setRowHidden(r, bool(needle) and needle not in hay)

    def _path_at(self, row: int) -> str:
        item = self.table.item(row, 0)
        return "" if item is None else (item.data(QtCore.Qt.UserRole) or "")

    def _current_path(self) -> str:
        row = self.table.currentRow()
        return self._path_at(row) if row >= 0 else ""

    def _info_at(self, row: int):
        path = self._path_at(row)
        for c in self.cases:
            if c.path == path:
                return c
        return None

    def current(self):
        row = self.table.currentRow()
        return self._info_at(row) if row >= 0 else None

    def selected(self) -> list:
        rows = sorted({i.row() for i in self.table.selectedItems()})
        out = [self._info_at(r) for r in rows if not self.table.isRowHidden(r)]
        return [c for c in out if c is not None]

    def select(self, path: str) -> bool:
        for r in range(self.table.rowCount()):
            if self._path_at(r) == path:
                self.table.selectRow(r)
                return True
        return False

    # --- деталі ----------------------------------------------------------

    def _selection_changed(self):
        self._flush_notes()
        info = self.current()
        chosen = len(self.selected())
        self.btn_open.setEnabled(info is not None)
        self.btn_para.setEnabled(bool(info and info.times))
        self.btn_dir.setEnabled(info is not None)
        self.btn_rename.setEnabled(info is not None)
        self.btn_slim.setEnabled(info is not None)
        self.btn_delete.setEnabled(info is not None)
        self.btn_compare.setEnabled(chosen >= 2)
        self.btn_notes.setEnabled(info is not None)
        self.ed_notes.setEnabled(info is not None)
        if info is None:
            self.details.setHtml("<p style='color:#777'>Кейс не вибрано.</p>")
            self._notes_path = ""
            self._notes_text = ""
            self.ed_notes.setPlainText("")
            return
        self.details.setHtml(_details_html(info))
        self._notes_path = info.path
        self._notes_text = info.notes
        self.ed_notes.setPlainText(info.notes)

    def _flush_notes(self):
        """Зберігає примітку, яку не встигли зберегти кнопкою.

        Інакше клік по сусідньому рядку мовчки викидав би написане: примітка
        живе в маніфесті, а не у вікні, і виглядає збереженою.
        """
        path = self._notes_path
        if not path or not os.path.isdir(path):
            return
        text = self.ed_notes.toPlainText()
        if text == self._notes_text:
            return
        try:
            lib.set_notes(path, text)
        except OSError:
            return
        for c in self.cases:
            if c.path == path:
                c.notes = text
        self._notes_text = text
        self.lbl_status.setText("Примітку збережено.")

    # --- дії -------------------------------------------------------------

    def _browse(self):
        path = QtWidgets.QFileDialog.getExistingDirectory(
            self, "Каталог із кейсами", self.root)
        if path:
            self.set_root(path)
            self.refresh()

    def _open(self):
        info = self.current()
        if info is not None:
            self.open_requested.emit(info.path)

    def _paraview(self):
        info = self.current()
        if info is not None:
            self.paraview_requested.emit(info.path)

    def _open_dir(self):
        info = self.current()
        if info is None:
            return
        try:
            os.startfile(info.path)
        except OSError as exc:
            self.lbl_status.setText(str(exc))

    def _save_notes(self):
        info = self.current()
        if info is None:
            return
        text = self.ed_notes.toPlainText()
        try:
            lib.set_notes(info.path, text)
        except OSError as exc:
            QtWidgets.QMessageBox.warning(self, "Не вдалося записати", str(exc))
            return
        info.notes = text
        self._notes_text = text
        self.lbl_status.setText(f"Примітку збережено в {lib.MANIFEST}.")

    def _rename(self):
        info = self.current()
        if info is None:
            return
        name, ok = QtWidgets.QInputDialog.getText(
            self, "Перейменувати кейс", "Нова назва каталогу:", text=info.name)
        name = (name or "").strip()
        if not ok or not name or name == info.name:
            return
        if any(ch in name for ch in '\\/:*?"<>|'):
            QtWidgets.QMessageBox.warning(
                self, "Недопустима назва",
                "У назві каталогу не можна вживати \\ / : * ? \" < > |")
            return
        target = os.path.join(os.path.dirname(info.path), name)
        if os.path.exists(target):
            QtWidgets.QMessageBox.warning(
                self, "Уже існує", f"Каталог «{name}» у цьому каталозі вже є.")
            return
        try:
            os.rename(info.path, target)
        except OSError as exc:
            QtWidgets.QMessageBox.critical(
                self, "Не вдалося перейменувати",
                f"{exc}\n\nЯкщо кейс відкритий у ParaView або в консолі "
                f"WSL — закрийте його і спробуйте ще раз.")
            return
        self.changed_on_disk.emit(info.path)
        self.refresh()
        self.select(target)

    def _slim(self):
        """Прибирає з кейсу те, що вже не несе результату."""
        info = self.current()
        if info is None:
            return
        parts = lib.reclaimable(info.path)
        if not parts:
            QtWidgets.QMessageBox.information(
                self, "Нічого прибирати",
                f"У кейсі «{info.name}» немає ні каталогів processor*, ні "
                f"проміжних часових зрізів.")
            return
        total = sum(size for _, size in parts)
        listing = "\n".join(f"    {name}    {lib.fmt_size(size)}"
                            for name, size in parts)
        ans = QtWidgets.QMessageBox.question(
            self, "Звільнити місце",
            f"З кейсу «{info.name}» буде видалено:\n\n{listing}\n\n"
            f"Усього {lib.fmt_size(total)} із {lib.fmt_size(info.size)}.\n\n"
            f"Результат лишається: останній часовий зріз, postProcessing, "
            f"логи й налаштування не чіпаються. Але продовжити цей прогін з "
            f"місця зупинки вже не вийде — тільки перезапустити.\n\nВидалити?",
            QtWidgets.QMessageBox.Yes | QtWidgets.QMessageBox.No,
            QtWidgets.QMessageBox.No)
        if ans != QtWidgets.QMessageBox.Yes:
            return
        freed = lib.reclaim(info.path, [name for name, _ in parts])
        self.changed_on_disk.emit(info.path)
        self.refresh()
        self.select(info.path)
        self.lbl_status.setText(f"Звільнено {lib.fmt_size(freed)}.")

    def _delete(self):
        info = self.current()
        if info is None:
            return
        ans = QtWidgets.QMessageBox.question(
            self, "Видалити кейс",
            f"Видалити каталог цілком?\n\n{info.path}\n\n"
            f"{lib.fmt_size(info.size)}, "
            f"{'є результати' if info.times else 'результатів немає'}. "
            f"Кошик Windows це не проходить — відновити не вийде.",
            QtWidgets.QMessageBox.Yes | QtWidgets.QMessageBox.No,
            QtWidgets.QMessageBox.No)
        if ans != QtWidgets.QMessageBox.Yes:
            return
        if not lib.delete_case(info.path):
            QtWidgets.QMessageBox.critical(
                self, "Не вдалося видалити",
                f"Каталог {info.path} лишився на місці. Можливо, його файли "
                f"тримає ParaView або консоль WSL.")
            return
        self.changed_on_disk.emit(info.path)
        self.refresh()
        self.lbl_status.setText(f"Кейс «{info.name}» видалено.")

    def _compare(self):
        chosen = self.selected()
        if len(chosen) >= 2:
            CompareDialog(chosen, self).exec()


def _fmt_num(value, fmt: str = "%.4g") -> str:
    return "—" if value is None else fmt % value


def _fmt_coeff(pair) -> str:
    """Коефіцієнт для таблиці. Зрив розрахунку видно одразу: у файлі після
    нього лежить не число, а щось на кшталт 3e+38."""
    if pair is None:
        return "—"
    mean = pair[0]
    if not math.isfinite(mean) or abs(mean) > 1e3:
        return "розійшлося"
    return f"{mean:+.5f}"


def _details_html(info) -> str:
    """Усе, що відомо про кейс, одним блоком."""
    rows: list[str] = []

    def head(text):
        rows.append(f'<p style="margin:8px 0 2px 0"><b>{text}</b></p>')

    def line(name, value):
        rows.append(f'<p style="margin:1px 0">{name} &nbsp;'
                    f'<code>{value}</code></p>')

    head(info.name)
    line("каталог", info.path)
    line("змінено", lib.fmt_time(info.modified))
    if info.created:
        line("створено", info.created)
    line("на диску", lib.fmt_size(info.size))
    if info.stl_source:
        line("з файлу", info.stl_source)

    if info.has_cfg:
        cfg = lib.cfg_from_dict(info.cfg)
        f, d, m, s = cfg.flow, cfg.domain, cfg.mesh, cfg.solver
        head("Потік")
        line("швидкість", f"{f.u_inf:.4g} м/с")
        line("кути", f"атаки {f.aoa_deg:g}°, ковзання {f.aos_deg:g}°")
        line("Re", f"{phys.reynolds(f.u_inf, f.l_ref, f.nu):.4g}")
        line("модель", f"{f.turbulence}, дальнє поле {f.farfield_mode}")
        line("опорні", f"Aref {f.a_ref:.5g} м², Lref {f.l_ref:.5g} м")
        line("середовище", f"nu {f.nu:.4g} м²/с, rho {f.rho:g} кг/м³")
        head("Сітка")
        line("домен", "%.4g × %.4g × %.4g м" % cfg.domain_size)
        line("фон", f"{d.base_cells_along_x} комірок уздовж X, "
                    f"комірка {phys.fmt_len(cfg.base_cell)}")
        line("рівні", f"поверхня {m.surface_level_min}–{m.surface_level_max}, "
                      f"кромки {m.feature_level}, "
                      f"комірка {phys.fmt_len(cfg.surface_cell)}")
        line("шари", f"{m.n_layers} × {m.expansion:g}, покриття "
                     f"{cfg.layer_stack.covers_bl(f.u_inf, f.l_ref, f.nu):.2f}"
                     if m.add_layers else "немає")
        line("коробка", f"рівень {m.boxes[0].level}" if m.boxes else "немає")
        head("Розвʼязувач")
        line("ітерацій", f"{s.n_iterations}, поріг {s.residual_tol:g}")
        line("процесів", f"{s.n_procs} ({s.decompose_method})")
        line("стійкий старт",
             f"перші {s.robust_iterations} ітерацій" if s.two_stage
             else ("увесь прогін" if s.robust_start else "вимкнено"))
    else:
        rows.append(
            f'<p style="margin:8px 0; color:{LEVEL_COLOR["warn"]}">'
            f'Файла <code>{lib.MANIFEST}</code> у кейсі немає: його створено '
            f'до появи бібліотеки або скопійовано без маніфесту. Налаштування '
            f'відновити не вийде — у <code>system/</code> лежать уже готові '
            f'словники. Результати, логи й ParaView доступні.</p>')

    head("Результат")
    facts = info.logs
    line("стан", info.state + (f", прогін {info.run_status}"
                               if info.run_status else ""))
    if facts.cells:
        line("комірок", phys.fmt_cells(facts.cells))
    if facts.mesh_failed >= 0:
        line("checkMesh", "без зауважень" if facts.mesh_failed == 0
             else f"{facts.mesh_failed} перевірок не пройдено")
    if info.layers >= 0:
        line("шари", f"наросли на {info.layers * 100:.0f}% граней"
                     + ("" if info.layers >= 0.7 else " — замало"))
    label, value = info.progress_label
    if value != "—":
        line(label, value + (f", {lib.fmt_seconds(info.seconds)} рахунку"
                             if info.seconds else ""))
    if info.times:
        line("часові зрізи", ", ".join("%g" % t for t in info.times[-6:]))
    if info.stale_times:
        rows.append(
            f'<p style="margin:6px 0; color:{LEVEL_COLOR["warn"]}">'
            f'Зрізи <code>'
            + ", ".join("%g" % t for t in info.stale_times)
            + '</code> не відповідають сітці кейсу: це поля попередніх '
              'прогонів, у них інша кількість комірок. ParaView на них '
              'видає «cells/points do not match», а читач результатів їх '
              'не показує. «Звільнити місце» їх прибере.</p>')

    found = False
    for name in ("Cd", "Cl", "CmPitch"):
        pair = info.coeff(name)
        if pair is None:
            continue
        found = True
        mean, spread = pair
        if not math.isfinite(mean) or abs(mean) > 1e3:
            line(name, f"{mean:.4g} — розрахунок розійшовся")
            continue
        note = ""
        if abs(mean) > 1e-6 and spread > abs(mean) * 0.02:
            note = "  — ще не зійшлося"
        line(name, f"{mean:+.5f}   розкид {spread:.2e}{note}")
    if not found and info.times:
        line("коефіцієнти", "у postProcessing не знайдено")

    if len(info.runs) > 1:
        head("Прогони")
        for run in reversed(info.runs[-5:]):
            line(run.get("finished", "—"),
                 f"{'ok' if run.get('ok') else 'обірвано'}, "
                 f"{run.get('iterations', 0)} ітерацій, "
                 f"{lib.fmt_seconds(run.get('seconds', 0.0))}")
    return "".join(rows)


class UnitsDialog(QtWidgets.QDialog):
    """Питання перед читанням: у яких одиницях креслення.

    STL одиниць не зберігає — лише числа. Поле «Одиниці STL» відповідало на
    це питання мовчки, і відповідь там могла лишитися від ЧУЖОЇ геометрії:
    відкритий збережений кейс чи відновлена сесія ставлять «метри» (там уже
    метри), і наступний міліметровий файл ішов у сцену в тисячу разів
    більшим. Фюзеляж 514 мм став 514-метровим, а людина про поле й не
    думала. Тому питаємо щоразу, і типово — міліметри, незалежно від того,
    що стоїть у полі.
    """

    def __init__(self, paths, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Одиниці моделі")
        names = [os.path.basename(str(p)) for p in paths]
        shown = ", ".join(names[:3]) + (f" і ще {len(names) - 3}"
                                        if len(names) > 3 else "")
        lay = QtWidgets.QVBoxLayout(self)
        head = WrapLabel(f"У яких одиницях креслення <b>{html_escape(shown)}</b>?")
        head.setWordWrap(True)
        lay.addWidget(head)
        self.cmb = QtWidgets.QComboBox()
        self.cmb.addItems(list(UNITS))
        self.cmb.setCurrentText(DEFAULT_UNITS)
        form = QtWidgets.QFormLayout()
        form.addRow("Одиниці", self.cmb)
        lay.addLayout(form)
        lay.addWidget(hint(
            "STL зберігає лише числа, без одиниць. Помилка тут — це модель у "
            "тисячу разів більша чи менша, і сітка, сили й число Рейнольдса "
            "від неї. Не знаєте — «авто»: програма вгадає за габаритом, але "
            "дрібну деталь у метрах вона прийме за міліметрову."))
        buttons = QtWidgets.QDialogButtonBox(
            QtWidgets.QDialogButtonBox.Ok | QtWidgets.QDialogButtonBox.Cancel)
        buttons.button(QtWidgets.QDialogButtonBox.Ok).setText("Відкрити")
        buttons.button(QtWidgets.QDialogButtonBox.Cancel).setText("Скасувати")
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        lay.addWidget(buttons)
        self.resize(460, self.sizeHint().height())

    def units(self) -> str:
        return self.cmb.currentText()

    @classmethod
    def ask(cls, parent, paths) -> str | None:
        """Одиниці для `paths` або None, якщо людина передумала."""
        dlg = cls(paths, parent)
        if dlg.exec() != QtWidgets.QDialog.Accepted:
            return None
        return dlg.units()


class WrapDialog(QtWidgets.QDialog):
    """Вибір роздільної здатности обгортання.

    Одним числом у порожньому полі тут не відбутися: воксель одночасно
    вирішує, що зникне з моделі, скільки вийде трикутників і чи влізе сітка
    в памʼять. Тому все це показується одразу, поки ви крутите значення, а
    не після натиску.
    """

    def __init__(self, parent, size_m, cell_m, smallest_m, start_mm=0.0):
        super().__init__(parent)
        self.setWindowTitle("Обгортання вокселями")
        self.size_m = tuple(float(v) for v in size_m)
        self.cell_m = float(cell_m)
        self.smallest_mm = float(smallest_m) * 1000.0
        span_mm = max(self.size_m) * 1000.0
        self.largest_mm = max(span_mm / 20.0, self.smallest_mm * 1.5)

        lay = QtWidgets.QVBoxLayout(self)
        lay.addWidget(WrapLabel(
            "Модель буде перерізано наново по воксельній сітці: оболонка "
            "вийде гарантовано замкнена — ні дірок, ні non-manifold ребер, "
            "ні вивернутих частин. Ціна — втрата дрібних деталей, тому це "
            "крайній засіб, а не звичайний ремонт."))

        row = QtWidgets.QHBoxLayout()
        row.addWidget(QtWidgets.QLabel("Розмір вокселя"))
        self.spin = QtWidgets.QDoubleSpinBox()
        self.spin.setDecimals(2)
        self.spin.setSuffix(" мм")
        self.spin.setRange(round(self.smallest_mm, 2) + 0.01,
                           round(self.largest_mm, 2))
        self.spin.setSingleStep(0.25)
        default = start_mm if start_mm > 0 else (
            self.cell_m * 1000.0 / 2.0 if self.cell_m > 0 else span_mm / 400.0)
        self.spin.setValue(min(max(default, self.spin.minimum()),
                               self.spin.maximum()))
        self.spin.valueChanged.connect(lambda _: self._refresh())
        row.addWidget(self.spin, 1)
        lay.addLayout(row)

        presets = QtWidgets.QHBoxLayout()
        for label, value in (("комірка / 2", self.cell_m * 500.0),
                             ("комірка / 4", self.cell_m * 250.0),
                             ("найдрібніший", self.smallest_mm * 1.02)):
            if value <= 0:
                continue
            b = QtWidgets.QPushButton(label)
            b.setToolTip(f"{value:.2f} мм")
            b.clicked.connect(lambda _=False, v=value: self.spin.setValue(
                min(max(v, self.spin.minimum()), self.spin.maximum())))
            presets.addWidget(b)
        lay.addLayout(presets)

        self.info = WrapLabel("")
        self.info.setStyleSheet("font-size:11px;")
        lay.addWidget(self.info)

        self.warn = WrapLabel("")
        self.warn.setStyleSheet(
            f"font-size:11px; font-weight:bold; color:{LEVEL_COLOR['warn']};")
        lay.addWidget(self.warn)

        buttons = QtWidgets.QDialogButtonBox(
            QtWidgets.QDialogButtonBox.Ok | QtWidgets.QDialogButtonBox.Cancel)
        buttons.button(QtWidgets.QDialogButtonBox.Ok).setText("Обгорнути")
        buttons.button(QtWidgets.QDialogButtonBox.Cancel).setText("Скасувати")
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        lay.addWidget(buttons)
        self.setMinimumWidth(430)
        self._refresh()

    def voxel_m(self) -> float:
        return self.spin.value() / 1000.0

    def _refresh(self):
        import math

        voxel = self.voxel_m()
        dims = [int(math.ceil((s + 8 * voxel) / voxel)) + 1
                for s in self.size_m]
        nodes = dims[0] * dims[1] * dims[2]
        # uint8-маски (матеріал, оболонка, повітря) плюс float-поле під гаус.
        ram = nodes * 7 / 1e6
        self.info.setText(
            f"Сітка {dims[0]} × {dims[1]} × {dims[2]} = "
            f"{nodes / 1e6:.1f} млн вузлів, памʼяті ≈ {ram:.0f} МБ."
            + NEWLINE
            + f"Зникне все, дрібніше за {self.spin.value():.2f} мм; поверхня "
            f"зсунеться приблизно на {self.spin.value() / 2:.2f} мм.")
        says = []
        if self.cell_m > 0:
            ratio = voxel / self.cell_m
            if ratio > 1:
                says.append(
                    f"Воксель у {ratio:.1f} раза БІЛЬШИЙ за комірку сітки на "
                    f"поверхні ({self.cell_m * 1000:.1f} мм) — огрубіння буде "
                    f"видно в розрахунку.")
            elif ratio > 0.5:
                says.append(
                    f"Воксель порівнянний із коміркою ({self.cell_m * 1000:.1f}"
                    f" мм). Надійніше взяти вдвічі дрібніший.")
        if nodes > 60e6:
            says.append("Сітка не влізе в памʼять — візьміть більший воксель.")
        self.warn.setText("  ".join(says))

    @staticmethod
    def ask(parent, size_m, cell_m, smallest_m,
            start_mm=0.0) -> tuple[float, bool]:
        """(воксель у метрах, натиснуто «Обгорнути»)."""
        dlg = WrapDialog(parent, size_m, cell_m, smallest_m, start_mm)
        ok = dlg.exec() == QtWidgets.QDialog.Accepted
        return (dlg.voxel_m(), ok)


class AlphaWrapDialog(QtWidgets.QDialog):
    """Щільність alpha wrap — у міліметрах, а не власними числами wrapwrap.

    ЧОМУ НЕ ЙОГО ЧИСЛАМИ. Обидва параметри там — ДІЛЬНИКИ діагоналі габариту,
    тобто більше число дає менший розмір. Перша версія цього вікна питала саме
    їх, і напрямок читався навпаки: поле звалося «зазор», а більше значення
    зазор зменшувало. Тому тут два звичайні розміри в міліметрах, а дільники
    рахуються самі.

    ЩО ПОКАЗУЄМО. Найважливіше для розрахунку — що обгортка не лагодить, а
    охоплює модель зверху: тіло виходить більшим за вихідне рівно на зазор
    (виміряно: відхилення поверхні збігається із заданим зазором до третьої
    цифри). Друге — що радіус кулі вирішує не «точність», а чи пролізе
    обгортка у щілини: усе, вужче за нього, заростає.
    """

    # Нижче 0.001 мм сходити немає сенсу: обмін із wrapwrap іде через STL, а
    # це float32 — на моделі в пів метра сама сітка зберігається з точністю
    # десятих мікрона.
    MIN_MM = 0.001

    # Скільки трикутників брати за дефолт, коли комірки сітки ще немає.
    # Виміряно: 278 тис. обгорнулися за 10 с, мільйони — хвилинами.
    DEFAULT_TRIANGLES = 150_000

    def __init__(self, parent, mesh, cell_m, start_mm=(0.0, 0.0)):
        super().__init__(parent)
        self.setWindowTitle("Обгортання alpha wrap")
        self.mesh = mesh
        self.cell_m = float(cell_m)
        self.diagonal_mm = alphawrap.absolute_mm(mesh, 1.0)

        lay = QtWidgets.QVBoxLayout(self)
        lay.addWidget(WrapLabel(
            "Поверхня проводиться по самій геометрії, а не по осях сітки, — "
            "тому кромки лишаються кромками, на відміну від воксельного "
            "обгортання. Результат гарантовано замкнений і 2-manifold навіть "
            "із дуже поламаного входу. Ціна — час і те, що змінюється вся "
            "модель, а не лише зламане місце."))

        form = QtWidgets.QFormLayout()
        self.spin_ball = QtWidgets.QDoubleSpinBox()
        self.spin_ball.setDecimals(3)
        self.spin_ball.setSuffix(" мм")
        self.spin_ball.setRange(self.MIN_MM, max(self.diagonal_mm / 5.0, 1.0))
        self.spin_ball.setToolTip(
            "Радіус кулі, яка мусить пролізти по поверхні. Усе, вужче за "
            "нього, обгортка перекриє зверху — тож щоб зберегти щілину, "
            "беріть удвічі-вчетверо менше за її ширину. Трикутників від "
            "цього — у квадраті")
        form.addRow("Найдрібніше, що зберегти", self.spin_ball)

        self.spin_gap = QtWidgets.QDoubleSpinBox()
        self.spin_gap.setDecimals(3)
        self.spin_gap.setSuffix(" мм")
        self.spin_gap.setRange(self.MIN_MM, max(self.diagonal_mm / 10.0, 1.0))
        self.spin_gap.setToolTip(
            "Наскільки оболонка відстоїть від поверхні — рівно на стільки "
            "тіло й виросте з кожного боку. Менше = ближче до оригіналу; "
            "нуля не буває, бо оболонка мусить охоплювати вхід")
        form.addRow("Зазор над поверхнею", self.spin_gap)
        lay.addLayout(form)

        ball_mm, gap_mm = self._defaults()
        self._set_mm(self.spin_ball, start_mm[0] if start_mm[0] > 0 else ball_mm)
        self._set_mm(self.spin_gap, start_mm[1] if start_mm[1] > 0 else gap_mm)

        presets = QtWidgets.QHBoxLayout()
        presets.addWidget(QtWidgets.QLabel("Заготовки:"))
        if self.cell_m > 0:
            for label, share in (("комірка", 1.0), ("комірка / 2", 0.5),
                                 ("комірка / 4", 0.25)):
                b = QtWidgets.QPushButton(label)
                b.setToolTip(f"найдрібніше {self.cell_m * share * 1000:.3f} мм")
                b.clicked.connect(lambda _=False, s=share: self._set_mm(
                    self.spin_ball, self.cell_m * s * 1000.0))
                presets.addWidget(b)
        tight = QtWidgets.QPushButton("щільно до оригіналу")
        tight.setToolTip(
            "Найменший зазор, який має сенс: відхилення поверхні виходить "
            "таким самим числом, а коштує це лише трохи більше трикутників")
        tight.clicked.connect(lambda _=False: self._set_mm(
            self.spin_gap, self.MIN_MM))
        presets.addWidget(tight)
        lay.addLayout(presets)

        self.info = WrapLabel("")
        self.info.setStyleSheet("font-size:11px;")
        lay.addWidget(self.info)

        self.warn = WrapLabel("")
        self.warn.setStyleSheet(
            f"font-size:11px; font-weight:bold; color:{LEVEL_COLOR['warn']};")
        lay.addWidget(self.warn)

        buttons = QtWidgets.QDialogButtonBox(
            QtWidgets.QDialogButtonBox.Ok | QtWidgets.QDialogButtonBox.Cancel)
        buttons.button(QtWidgets.QDialogButtonBox.Ok).setText("Обгорнути")
        buttons.button(QtWidgets.QDialogButtonBox.Cancel).setText("Скасувати")
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        lay.addWidget(buttons)
        self.setMinimumWidth(470)

        self.spin_ball.valueChanged.connect(lambda _: self._refresh())
        self.spin_gap.valueChanged.connect(lambda _: self._refresh())
        self._refresh()

    @staticmethod
    def _set_mm(spin, millimetres: float):
        spin.setValue(min(max(millimetres, spin.minimum()), spin.maximum()))

    def _defaults(self) -> tuple[float, float]:
        """(куля, зазор) у міліметрах для першого відкриття.

        Орієнтир — комірка сітки на поверхні, як і в воксельному обгортанні:
        дрібніше за комірку snappy усе одно не відтворить, тому куля в пів
        комірки нічого потрібного не заростить. Зазор беремо сотою частиною
        комірки: він коштує лише кілька відсотків трикутників, а в результаті
        його не видно.

        Але на великій моделі пів комірки — це мільйони трикутників (модель у
        пів метра з коміркою 2 мм: 1.1 млн), тож беремо ГРУБІШЕ з двох —
        комірку/2 або те, що влазить у бюджет. Вікно, яке відкривається вже з
        червоним попередженням, не інформує, а лише дратує; тиснути дрібніше
        людина може сама, і тоді попередження буде до речі.
        """
        budget = alphawrap.ball_mm_for_triangles(self.mesh,
                                                 self.DEFAULT_TRIANGLES)
        if self.cell_m > 0:
            cell_mm = self.cell_m * 1000.0
            return (max(cell_mm / 2.0, budget),
                    max(cell_mm / 100.0, self.MIN_MM))
        if budget <= 0:
            budget = self.diagonal_mm / 50.0
        return budget, max(budget / 50.0, self.MIN_MM)

    def values(self) -> tuple[float, float]:
        """(alpha, offset) — уже у власних відносних числах wrapwrap."""
        return (alphawrap.relative_for_mm(self.mesh, self.spin_ball.value()),
                alphawrap.relative_for_mm(self.mesh, self.spin_gap.value()))

    def _refresh(self):
        ball = self.spin_ball.value()
        gap = self.spin_gap.value()
        alpha, offset = self.values()
        triangles = alphawrap.estimate_triangles(self.mesh, alpha)
        self.info.setText(
            f"Заростуть щілини вужчі за ≈ {ball * 2:.3f} мм; тіло виросте на "
            f"{gap:.3f} мм з кожного боку (габарит +{gap * 2:.3f} мм)."
            + NEWLINE
            + f"Трикутників вийде ≈ {triangles / 1000:.0f} тис."
            + NEWLINE
            + f"Для wrapwrap це alpha={alpha:.0f}, offset={offset:.0f} "
              f"(діагональ габариту {self.diagonal_mm:.1f} мм).")
        says = []
        if triangles > 500_000:
            says.append(
                f"≈{triangles / 1e6:.1f} млн трикутників — обгортання піде "
                f"хвилинами, а пошук по такій поверхні гальмуватиме snappy. "
                f"Для розрахунку стільки не потрібно.")
        if self.cell_m > 0:
            cell_mm = self.cell_m * 1000
            if ball > cell_mm:
                says.append(
                    f"Куля більша за комірку на поверхні ({cell_mm:.3f} мм) — "
                    f"сітка відтворила б більше, ніж лишиться від моделі.")
            if gap > cell_mm:
                says.append(
                    f"Тіло роздується більше, ніж на комірку — це вже видно "
                    f"в результаті. Зменшіть зазор.")
        self.warn.setText("  ".join(says))

    @staticmethod
    def ask(parent, mesh, cell_m,
            start_mm=(0.0, 0.0)) -> tuple[float, float, bool]:
        """(alpha, offset, натиснуто «Обгорнути»).

        Назовні йдуть відносні числа, які просить `wrapwrap`, а `start_mm` —
        міліметри: запамʼятовувати варто саме їх, бо звірятися вони будуть із
        коміркою сітки, а вона теж абсолютна.
        """
        dlg = AlphaWrapDialog(parent, mesh, cell_m, start_mm)
        ok = dlg.exec() == QtWidgets.QDialog.Accepted
        alpha, offset = dlg.values()
        return (alpha, offset, ok)


class CompareDialog(QtWidgets.QDialog):
    """Кілька кейсів поряд.

    Рядки, де значення розійшлися, підсвічені: у серії прогонів по кутах
    атаки чи рівнях рефайнменту питання завжди одне — що саме відрізняється,
    крім числа в назві.
    """

    def __init__(self, cases: list, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Порівняння кейсів")

        rows = _compare_rows(cases)
        table = QtWidgets.QTableWidget(len(rows), len(cases) + 1)
        table.setHorizontalHeaderLabels(["Параметр"] + [c.name for c in cases])
        table.verticalHeader().setVisible(False)
        table.setEditTriggers(QtWidgets.QAbstractItemView.NoEditTriggers)
        table.setAlternatingRowColors(True)

        for r, (label, values) in enumerate(rows):
            differs = len(set(values)) > 1
            item = QtWidgets.QTableWidgetItem(label)
            if differs:
                font = item.font()
                font.setBold(True)
                item.setFont(font)
            table.setItem(r, 0, item)
            for col, value in enumerate(values, start=1):
                cell = QtWidgets.QTableWidgetItem(value)
                if differs:
                    cell.setForeground(QtGui.QColor(LEVEL_COLOR["warn"]))
                table.setItem(r, col, cell)
        table.resizeColumnsToContents()
        # Вікно під таблицю, а не навпаки: два кейси — вузьке вікно, вісім —
        # широке. Інакше поряд із трьома стовпцями зяяла б половина екрана.
        # Знизу блок про нейтральну точку: текст у ньому переноситься, але
        # зовсім вузьке вікно зробило б із нього стовпчик.
        need = sum(table.columnWidth(i) for i in range(table.columnCount()))
        self.resize(max(min(need + 70, 1500), 820), 800)

        lay = QtWidgets.QVBoxLayout(self)
        lay.addWidget(hint(
            "Підсвічено рядки, де кейси відрізняються. Порожня клітинка — "
            "кейс без маніфесту: його налаштування невідомі."))
        lay.addWidget(table, 1)
        lay.addWidget(self._stability_box(cases))
        btn = QtWidgets.QPushButton("Закрити")
        btn.clicked.connect(self.accept)
        strip = QtWidgets.QHBoxLayout()
        strip.addStretch(1)
        strip.addWidget(btn)
        lay.addLayout(strip)

    def _stability_box(self, cases: list) -> QtWidgets.QGroupBox:
        """Нейтральна точка за виділеними кейсами й перевірка центру ваги.

        Центр ваги ставлять не за центром тиску, а за нейтральною точкою, а
        вона знаходиться лише з кількох кутів — тому живе тут, де кейси вже
        лежать поряд.
        """
        self.stab = st = lib.stability(cases)
        box = QtWidgets.QGroupBox("Нейтральна точка — від неї ставлять центр ваги")
        v = QtWidgets.QVBoxLayout(box)
        self.lbl_np = QtWidgets.QLabel(_stability_html(st))
        self.lbl_np.setTextFormat(QtCore.Qt.RichText)
        self.lbl_np.setWordWrap(True)
        v.addWidget(self.lbl_np)
        self.spin_cg = None
        self.lbl_margin = None
        if st.point is not None:
            row = QtWidgets.QHBoxLayout()
            where = "від носа" if st.nose_x is not None else "x у системі кейсу"
            row.addWidget(QtWidgets.QLabel(f"Ваш центр ваги, {where}:"))
            # «Не задано» — найменше значення поля: порожнього стану в
            # QDoubleSpinBox нема, а підставляти за замовчуванням якесь число
            # значило б видати його за ваш центр ваги.
            self.spin_cg = dspin(-1e5, 1e5, -1e5, step=5.0, decimals=1,
                                 suffix=" мм")
            self.spin_cg.setSpecialValueText("не задано")
            self.spin_cg.setToolTip(
                "Відстань від найпередньої точки тіла вздовж потоку. Для "
                "стійкості центр ваги має лежати ПЕРЕД нейтральною точкою.")
            self.spin_cg.valueChanged.connect(self._cg_changed)
            row.addWidget(self.spin_cg)
            self.lbl_margin = QtWidgets.QLabel("")
            self.lbl_margin.setTextFormat(QtCore.Qt.RichText)
            self.lbl_margin.setWordWrap(True)
            row.addWidget(self.lbl_margin, 1)
            v.addLayout(row)
        return box

    def _cg_changed(self, value_mm: float):
        st = self.stab
        if st.point is None or value_mm <= self.spin_cg.minimum():
            self.lbl_margin.setText("")
            return
        x_cg = value_mm / 1000.0 + (st.nose_x if st.nose_x is not None else 0.0)
        m = st.margin(x_cg)
        text, colour = _margin_verdict(m)
        self.lbl_margin.setText(
            f'<span style="color:{colour}"><b>запас стійкості {m * 100:+.1f}% '
            f"Lref</b> — {text}</span>")


def _margin_verdict(margin: float) -> tuple[str, str]:
    """Слово про запас стійкості й колір, яким його писати."""
    if margin < 0:
        return ("НЕстійкий: центр ваги позаду нейтральної точки, "
                "збурення наростатиме", LEVEL_COLOR["bad"])
    if margin < lib.MARGIN_LOW:
        return ("ледь стійкий, запасу замало (норма %d–%d%%)"
                % (round(lib.MARGIN_LOW * 100), round(lib.MARGIN_HIGH * 100)),
                LEVEL_COLOR["warn"])
    if margin <= lib.MARGIN_HIGH:
        return ("стійкий, у нормі", LEVEL_COLOR["ok"])
    return ("дуже стійкий: центр ваги далеко попереду, стабілізатор "
            "утримуватиме кут з великим зусиллям", LEVEL_COLOR["warn"])


def _stability_html(st) -> str:
    """Текст блоку про нейтральну точку: число, де ставити центр ваги, і що
    зважити — або, якщо порахувати не вдалося, що саме зробити."""
    notes = "".join(
        f'<li style="color:{LEVEL_COLOR["warn"]}">{html_escape(n)}</li>'
        for n in st.notes)
    notes = f"<ul style='margin:2px 0'>{notes}</ul>" if notes else ""
    if st.point is None:
        return (
            f"<p>Нейтральну точку поки не порахувати: {html_escape(st.problem)}.</p>"
            f'<p style="color:#777">Як її отримати: прорахуйте ТОЙ САМИЙ літак на '
            f"кількох кутах атаки (наприклад 2°, 5° і 8°), змінюючи лише кут. "
            f"Виділіть ці кейси у «Збережених» і натисніть «Порівняти вибрані».</p>"
            + notes)
    p = st.point
    angles = ", ".join(f"{a:g}°" for a in p.angles)
    names = html_escape(", ".join(st.names))
    where = f"x = <b>{p.x:+.4f} м</b> у системі кейсу"
    if st.nose_x is not None and st.length:
        d = p.x - st.nose_x
        where += (f"; <b>від носа {d * 1000:.0f} мм</b> "
                  f"({d / st.length * 100:.1f}% довжини тіла)")
    pairs = ""
    if len(p.angles) > 2 and p.pairs:
        pairs = (f"<br>За сусідніми парами кутів: від {min(p.pairs):+.4f} до "
                 f"{max(p.pairs):+.4f} м — так видно, чи залежність лінійна.")
    lo, hi = st.cg_range()
    if st.nose_x is not None:
        span = (f"від <b>{(lo - st.nose_x) * 1000:.0f}</b> до "
                f"<b>{(hi - st.nose_x) * 1000:.0f} мм від носа</b>")
    else:
        span = f"x від <b>{lo:+.4f}</b> до <b>{hi:+.4f} м</b>"
    return (
        f"<p><b>Нейтральна точка</b> за кутами {angles} (кейси: {names}):<br>"
        f"{where}.<br>"
        f"dCn/dα = {p.cn_alpha:+.5f} на градус, dCm/dα = {p.cm_alpha:+.5f} на "
        f"градус — відносно центру моментів x = {st.x_cofr:+.3f} м.{pairs}</p>"
        f"<p><b>Центр ваги</b> ставте ПЕРЕД нею (ближче до носа) із запасом "
        f"{round(lib.MARGIN_LOW * 100)}–{round(lib.MARGIN_HIGH * 100)}% Lref "
        f"(Lref = {st.l_ref:.4g} м): {span}.</p>" + notes)


def _compare_rows(cases: list) -> list[tuple[str, list[str]]]:
    """Рядки таблиці порівняння: (назва, значення по кейсах)."""
    prepared = [(info, lib.cfg_from_dict(info.cfg) if info.has_cfg else None)
                for info in cases]

    def row(label, fn):
        return (label, [("" if cfg is None else str(fn(cfg)))
                        for _, cfg in prepared])

    def coeff(name):
        return (name, [_fmt_coeff(info.coeff(name)) for info, _ in prepared])

    def spread(name):
        out = []
        for info, _ in prepared:
            pair = info.coeff(name)
            out.append("—" if pair is None else "%.2e" % pair[1])
        return (f"розкид {name}", out)

    def yplus(cfg):
        if not cfg.mesh.add_layers:
            return "—"
        f = cfg.flow
        return "%.1f" % cfg.layer_stack.yplus(f.u_inf, f.l_ref, f.nu)

    return [
        ("стан", [info.state for info, _ in prepared]),
        ("прогін", [info.run_status or "—" for info, _ in prepared]),
        ("комірок", [phys.fmt_cells(info.cells) if info.cells else "—"
                     for info, _ in prepared]),
        # Підпис однин на стовпець, а постановка в кейсів може бути різна,
        # тому пишемо нейтрально: «пройдено» однаково читається і про
        # ітерації стаціонару, і про секунди нестаціонару.
        ("пройдено", [info.progress_label[1] for info, _ in prepared]),
        ("час рахунку", [lib.fmt_seconds(info.seconds)
                         for info, _ in prepared]),
        coeff("Cd"),
        spread("Cd"),
        coeff("Cl"),
        spread("Cl"),
        coeff("CmPitch"),
        row("швидкість, м/с", lambda c: "%.4g" % c.flow.u_inf),
        row("кут атаки", lambda c: "%g°" % c.flow.aoa_deg),
        row("кут ковзання", lambda c: "%g°" % c.flow.aos_deg),
        row("Re", lambda c: "%.4g" % phys.reynolds(
            c.flow.u_inf, c.flow.l_ref, c.flow.nu)),
        row("модель турбулентності", lambda c: c.flow.turbulence),
        row("дальнє поле", lambda c: c.flow.farfield_mode),
        row("Aref, м²", lambda c: "%.5g" % c.flow.a_ref),
        row("Lref, м", lambda c: "%.5g" % c.flow.l_ref),
        row("домен: перед/за/бік", lambda c: "%g / %g / %g" % (
            c.domain.upstream, c.domain.downstream, c.domain.side)),
        row("комірок уздовж X", lambda c: c.domain.base_cells_along_x),
        row("фонова комірка", lambda c: phys.fmt_len(c.base_cell)),
        row("рівні поверхні", lambda c: "%d–%d" % (
            c.mesh.surface_level_min, c.mesh.surface_level_max)),
        row("рівень кромок", lambda c: c.mesh.feature_level),
        row("комірка на поверхні", lambda c: phys.fmt_len(c.surface_cell)),
        row("шарів", lambda c: ("%d × %g" % (c.mesh.n_layers, c.mesh.expansion)
                                if c.mesh.add_layers else "немає")),
        row("y+ (оцінка)", yplus),
        row("ітерацій задано", lambda c: c.solver.n_iterations),
        row("поріг збіжності", lambda c: "%g" % c.solver.residual_tol),
        row("процесів", lambda c: c.solver.n_procs),
        row("стійких ітерацій", lambda c: (
            c.solver.robust_iterations if c.solver.two_stage else "—")),
        ("примітка", [info.notes.replace("\n", " ")[:120]
                      for info, _ in prepared]),
    ]


# ============================================================================
# ВІРТУАЛЬНИЙ ПУЛЬТ
# ============================================================================


class StickPad(QtWidgets.QWidget):
    """Стік як на апаратурі: квадратний майданчик і ручка, що тягається мишею.

    ЧОМУ НЕ ДВА ПОВЗУНКИ. Бо позицію задають не двома числами, а рукою:
    «трохи правого крену з невеликим пікіруванням» — це один рух, а не
    два введення. Повзунки для цього годяться погано, і саме через них
    легко поставити не ту комбінацію й не помітити.

    САМОПОВЕРНЕННЯ. Горизонталь і вертикаль можуть повертатися в нейтраль,
    як пружина справжнього стіка, а можуть лишатися — газ на апаратурі
    саме такий, у нього храповик. Тому це вмикається окремо для кожної осі.
    """

    changed = QtCore.Signal(float, float)   # x, y у своїх одиницях

    def __init__(self, x_range=(-1.0, 1.0), y_range=(-1.0, 1.0),
                 x_spring=True, y_spring=True, labels=("", ""), parent=None):
        super().__init__(parent)
        self.x_range, self.y_range = x_range, y_range
        self.x_spring, self.y_spring = x_spring, y_spring
        self.labels = labels
        self.x = 0.5 * (x_range[0] + x_range[1]) if not x_spring else 0.0
        self.y = 0.5 * (y_range[0] + y_range[1]) if not y_spring else 0.0
        if not x_spring:
            self.x = x_range[0]
        if not y_spring:
            self.y = y_range[0]
        self.setMinimumSize(150, 150)
        self.setSizePolicy(QtWidgets.QSizePolicy.Expanding,
                           QtWidgets.QSizePolicy.Expanding)
        self.setCursor(QtCore.Qt.CrossCursor)
        self._drag = False

    # --- перетворення між значенням і пікселем ------------------------------
    # Поля під підписи осей: назва горизонтальної осі пишеться ПІД
    # майданчиком, вертикальної — ЛІВОРУЧ. Без цих полів вони виходять
    # за край віджета й просто не видно, що яким стіком керує.
    PAD_LEFT, PAD_BOTTOM, PAD = 16, 16, 5

    def _rect(self) -> QtCore.QRectF:
        w = self.width() - self.PAD_LEFT - self.PAD
        h = self.height() - self.PAD_BOTTOM - self.PAD
        side = max(min(w, h), 20)
        return QtCore.QRectF(self.PAD_LEFT + (w - side) / 2,
                             self.PAD + (h - side) / 2, side, side)

    def _to_px(self, r: QtCore.QRectF):
        fx = (self.x - self.x_range[0]) / (self.x_range[1] - self.x_range[0])
        fy = (self.y - self.y_range[0]) / (self.y_range[1] - self.y_range[0])
        # Вертикаль на екрані росте ВНИЗ, а газ — угору, тож 1 - fy.
        return r.left() + fx * r.width(), r.top() + (1.0 - fy) * r.height()

    def _from_px(self, pos, r: QtCore.QRectF):
        fx = min(max((pos.x() - r.left()) / r.width(), 0.0), 1.0)
        fy = min(max(1.0 - (pos.y() - r.top()) / r.height(), 0.0), 1.0)
        return (self.x_range[0] + fx * (self.x_range[1] - self.x_range[0]),
                self.y_range[0] + fy * (self.y_range[1] - self.y_range[0]))

    # --- миша ---------------------------------------------------------------
    def mousePressEvent(self, e):
        self._drag = True
        self._apply(e.position())

    def mouseMoveEvent(self, e):
        if self._drag:
            self._apply(e.position())

    def mouseReleaseEvent(self, e):
        self._drag = False
        # ПРУЖИНА. Відпустив — стік повернувся, як на апаратурі. Без
        # цього легко лишити крен і забути про нього, а потім дивуватися
        # несиметричному результату.
        moved = False
        if self.x_spring and abs(self.x) > 1e-12:
            self.x, moved = 0.0, True
        if self.y_spring and abs(self.y) > 1e-12:
            self.y, moved = 0.0, True
        if moved:
            self.update()
            self.changed.emit(self.x, self.y)

    def _apply(self, pos):
        r = self._rect()
        x, y = self._from_px(pos, r)
        if (abs(x - self.x) > 1e-9) or (abs(y - self.y) > 1e-9):
            self.x, self.y = x, y
            self.update()
            self.changed.emit(self.x, self.y)

    def set_values(self, x, y):
        self.x, self.y = float(x), float(y)
        self.update()

    # --- малювання ----------------------------------------------------------
    def paintEvent(self, _):
        p = QtGui.QPainter(self)
        p.setRenderHint(QtGui.QPainter.Antialiasing)
        r = self._rect()
        p.fillRect(self.rect(), QtGui.QColor("#12161a"))
        p.setPen(QtGui.QPen(QtGui.QColor("#3a444e"), 1.2))
        p.setBrush(QtGui.QColor("#1a2026"))
        p.drawRoundedRect(r, 6, 6)
        # Хрест нейтралі — там, де стік стоїть відпущеним.
        p.setPen(QtGui.QPen(QtGui.QColor("#2c353d"), 1.0,
                            QtCore.Qt.DashLine))
        cx0, cy0 = self.x, self.y
        self.x = 0.0 if self.x_spring else self.x_range[0]
        self.y = 0.0 if self.y_spring else self.y_range[0]
        nx, ny = self._to_px(r)
        self.x, self.y = cx0, cy0
        p.drawLine(QtCore.QPointF(r.left(), ny), QtCore.QPointF(r.right(), ny))
        p.drawLine(QtCore.QPointF(nx, r.top()), QtCore.QPointF(nx, r.bottom()))
        # Ручка.
        hx, hy = self._to_px(r)
        p.setPen(QtGui.QPen(QtGui.QColor("#0d1013"), 1.5))
        p.setBrush(QtGui.QColor("#f2b13c"))
        p.drawEllipse(QtCore.QPointF(hx, hy), 9, 9)
        # Підписи осей — щоб не гадати, яка де.
        p.setPen(QtGui.QColor("#7c8894"))
        f = p.font()
        f.setPointSize(8)
        p.setFont(f)
        if self.labels[0]:
            p.drawText(QtCore.QRectF(r.left(), r.bottom() + 1, r.width(), 14),
                       QtCore.Qt.AlignHCenter, self.labels[0])
        if self.labels[1]:
            p.save()
            p.translate(r.left() - 2, r.center().y())
            p.rotate(-90)
            p.drawText(QtCore.QRectF(-r.height() / 2, -14, r.height(), 14),
                       QtCore.Qt.AlignHCenter, self.labels[1])
            p.restore()
        p.end()


class TransmitterPanel(QtWidgets.QWidget):
    """Пульт: у якому положенні рахувати.

    Це НЕ симуляція польоту. Стіки задають ОДНУ позицію — ту, в якій
    поверхні відхилені й у якій будується сітка. Розрахунок стаціонарний,
    тож літак у ньому завмер у цій позі, як у аеродинамічній трубі з
    зафіксованими кермами.

    Розкладка друга («мode 2»), як у більшості: лівий стік — газ і курс,
    правий — тангаж і крен. Газ без пружини, решта самоповертається.
    """

    channels_changed = QtCore.Signal(dict)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.values = {k: mid for k, _, _, _, mid, _ in fc.RC_CHANNELS}
        v = QtWidgets.QVBoxLayout(self)

        head = WrapLabel(
            "Стіки задають ПОЗИЦІЮ, в якій рахуватиметься кейс: поверхні "
            "відхиляються на свої кути, і саме така геометрія йде в сітку. "
            "Це не політ — розрахунок стаціонарний, літак у ньому завмер.")
        head.setStyleSheet("color:#9aa7b4; font-size:11px;")
        v.addWidget(head)

        sticks = QtWidgets.QHBoxLayout()
        self.left = StickPad(x_range=(-1.0, 1.0), y_range=(0.0, 1.0),
                             x_spring=True, y_spring=False,
                             labels=("курс", "газ"))
        self.right = StickPad(x_range=(-1.0, 1.0), y_range=(-1.0, 1.0),
                              labels=("крен", "тангаж"))
        for pad in (self.left, self.right):
            box = QtWidgets.QVBoxLayout()
            box.addWidget(pad, 1)
            sticks.addLayout(box, 1)
        self.left.changed.connect(
            lambda x, y: self._stick("yaw", x, "throttle", y))
        self.right.changed.connect(
            lambda x, y: self._stick("roll", x, "pitch", y))
        v.addLayout(sticks, 1)

        # Додаткові канали — повзунками: закрилки й дод. ставлять і
        # лишають, тягати їх мишею як стік не треба.
        f = QtWidgets.QFormLayout()
        self.sliders = {}
        for key, label, lo, hi, mid, _ in fc.RC_CHANNELS:
            if key in ("roll", "pitch", "yaw", "throttle"):
                continue
            s = QtWidgets.QSlider(QtCore.Qt.Horizontal)
            s.setRange(int(lo * 100), int(hi * 100))
            s.setValue(int(mid * 100))
            s.valueChanged.connect(
                lambda val, k=key: self._set(k, val / 100.0))
            self.sliders[key] = s
            f.addRow(label, s)
        v.addLayout(f)

        self.lbl = WrapLabel("")
        self.lbl.setStyleSheet("font-family:Consolas,monospace; "
                               "font-size:11px; color:#c8d2dc;")
        v.addWidget(self.lbl)

        row = QtWidgets.QHBoxLayout()
        b = QtWidgets.QPushButton("Все в нейтраль")
        b.setToolTip("Повернути всі канали в нейтраль: газ у нуль, "
                     "керма прямо")
        b.clicked.connect(self.to_neutral)
        row.addWidget(b)
        row.addStretch(1)
        v.addLayout(row)

        self.lbl_map = WrapLabel("")
        self.lbl_map.setStyleSheet("color:#7c8894; font-size:11px;")
        v.addWidget(self.lbl_map)
        v.addStretch(1)
        self._show()

    def _stick(self, kx, x, ky, y):
        self.values[kx] = float(x)
        self.values[ky] = float(y)
        self._show()
        self.channels_changed.emit(dict(self.values))

    def _set(self, key, val):
        self.values[key] = float(val)
        self._show()
        self.channels_changed.emit(dict(self.values))

    def set_quiet(self, key: str, value: float) -> None:
        """Поставити канал, НЕ випромінюючи сигналу.

        Потрібно, коли канал ставить сама програма (перший двигун ставить
        газ на повний): вона вже знає нове значення, і відлуння від пульта
        лише перезібрало б сцену вдруге.
        """
        if key not in self.values:
            return
        self.values[key] = float(value)
        self.left.set_values(self.values["yaw"], self.values["throttle"])
        self.right.set_values(self.values["roll"], self.values["pitch"])
        if key in self.sliders:
            s = self.sliders[key]
            s.blockSignals(True)
            s.setValue(int(self.values[key] * 100))
            s.blockSignals(False)
        self._show()

    def to_neutral(self):
        for key, _, _, _, mid, _ in fc.RC_CHANNELS:
            self.values[key] = mid
        self.left.set_values(self.values["yaw"], self.values["throttle"])
        self.right.set_values(self.values["roll"], self.values["pitch"])
        for key, s in self.sliders.items():
            s.blockSignals(True)
            s.setValue(int(self.values[key] * 100))
            s.blockSignals(False)
        self._show()
        self.channels_changed.emit(dict(self.values))

    def _show(self):
        parts = []
        for key, label, _, _, _, _ in fc.RC_CHANNELS:
            parts.append(f"{label} {self.values[key]:+.2f}")
        self.lbl.setText("   ".join(parts))

    def show_mapping(self, lines):
        """Що саме зараз рухається — списком, з кутами.

        Без цього пульт — це стіки, які невідомо що роблять: канал можна
        призначити й помилитися деталлю, і на екрані це буде схоже на
        «нічого не сталося».
        """
        if not lines:
            self.lbl_map.setText(
                "Жодної керованої поверхні. Виберіть деталь на вкладці "
                "«Геометрія», задайте їй канал і завісу.")
        else:
            self.lbl_map.setText("\n".join(lines))
