"""Сітка з проєкту: замок на налаштуваннях, що її змінюють.

Проєкт, збережений «із сіткою», несе готову сітку (constant/polyMesh) і її
ключ. Поки людина не змінила нічого, від чого залежить сітка, прогін бере
цю сітку, і snappyHexMesh не йде (на сервері це ~10 хв на 2.4 млн комірок).

Ключ сітки рахується з тексту словників і triSurface (fs_backend.mesh_key).
Нова версія програми може записати ті самі налаштування трохи іншим текстом
(так було 2026-11-10: y+ авто дописав товщину першого шару), і тоді ключ
інший, хоча людина нічого не міняла. Тому правило таке: сітка проєкту діє,
доки людина не змінила налаштування сітки, а не доки збігається ключ.
«Не змінила» перевіряємо двічі:
  * замок: поля, що впливають на сітку, приглушені, і клік чи клавіша на
    них спершу питає (MeshLock нижче);
  * перед прогоном: ключ теперішніх налаштувань (з цією ж версією коду)
    порівнюємо з тим, що був одразу після відкриття (`baseline`).

Які поля замикати, визначено з коду (tools/mesh_inputs_probe.py міняє
кожне поле й дивиться, чи змінився ключ) і доповнено тими, що діють лише в
певному стані (роль деталі, оболонка, двигуни). Список — LOCKED нижче.
"""

from __future__ import annotations

from dataclasses import dataclass

from PySide6 import QtCore, QtGui, QtWidgets

import fs_panels as panels
from fs_i18n import tr

# Поля, що діють на сітку (словники сітки або triSurface).
#   "*"          — усі поля вкладки;
#   ("*", ...)   — усі, крім названих;
#   (...)        — лише названі (контейнер замикається разом із вмістом).
LOCKED: dict = {
    # blockMesh: розміри домену, комірок уздовж X, земля-стінка.
    "panel_dom": "*",
    # Рівні, кромки, коробки, оболонки, шари, y+ (він підбирає шари).
    # Адаптація — під час розрахунку, а не в початковій сітці: вільна.
    "panel_mesh": ("*", "adapt_mode", "adapt_level", "adapt_frac",
                   "adapt_every", "adapt_curl"),
    # Склад сцени, ролі, рівні деталей, одиниці, ремонт, пересування,
    # завіси, «кожна модель — окремий патч», згущення на кришках і в диску.
    # "+buttons" — і всі кнопки вкладки: поворот збірки, центрувати,
    # посадити на z=0, методи ремонту (без імен в атрибутах).
    "panel_geo": ("+buttons", "btn_open", "btn_add", "btn_prop", "btn_disk",
                  "btn_drop",
                  "cmb_role", "role_axis", "role_level", "eng_name",
                  "btn_flip", "jet_refine", "disk_refine", "box_ctl",
                  "box_one", "chk_split", "cmb_units", "btn_repair"),
    # Обертова зона (MRF) згущує сітку коробкою навколо себе.
    "panel_flow": ("rot_on", "rot_cx", "rot_cy", "rot_cz", "rot_r",
                   "rot_len", "btn_rot_fit"),
    # «Перебудувати сітку» — прямо проти сітки проєкту.
    "panel_run": ("chk_remesh",),
}
# Поля всередині замкнених контейнерів, що сітки не чіпають.
FREE_INSIDE = {"panel_geo": ("move_step",)}

INPUTS = (QtWidgets.QAbstractSpinBox, QtWidgets.QComboBox,
          QtWidgets.QAbstractButton, QtWidgets.QLineEdit,
          QtWidgets.QAbstractSlider, QtWidgets.QAbstractItemView,
          QtWidgets.QGroupBox)

# Клавіші, що лише ходять між полями: їх не питаємо.
_NAV_KEYS = {QtCore.Qt.Key_Tab, QtCore.Qt.Key_Backtab, QtCore.Qt.Key_Shift,
             QtCore.Qt.Key_Control, QtCore.Qt.Key_Alt, QtCore.Qt.Key_Meta,
             QtCore.Qt.Key_Escape, QtCore.Qt.Key_F1, QtCore.Qt.Key_F2,
             QtCore.Qt.Key_F3, QtCore.Qt.Key_F4, QtCore.Qt.Key_F5}

DIM = 0.55          # непрозорість замкненого поля


def snappy_minutes(cells: int) -> int:
    """Грубо: скільки хвилин snappyHexMesh на сервері для `cells` комірок.

    Міряно на сервері: case_jet ~2.4 млн комірок — близько 10 хв."""
    return max(1, round(max(0, cells) / 250_000))


def _is_help(w) -> bool:
    """Кнопка-розкривачка підказки (panels.Fold): лишається вільною."""
    p = w.parentWidget()
    return isinstance(p, panels.Fold) and w is getattr(p, "btn", None)


def _inputs(root) -> list:
    """Поля вводу під `root` (без внутрішніх частин спінбоксів і списків)."""
    out = []
    for w in root.findChildren(QtWidgets.QWidget):
        if not isinstance(w, INPUTS) or isinstance(w, QtWidgets.QScrollBar):
            continue
        if isinstance(w, QtWidgets.QGroupBox) and not w.isCheckable():
            continue
        if _is_help(w):
            continue
        parent = w.parentWidget()
        if isinstance(parent, (QtWidgets.QAbstractSpinBox, QtWidgets.QComboBox)):
            continue
        out.append(w)
    return out


def _covered(w, roots) -> bool:
    p = w
    while p is not None:
        if p in roots:
            return True
        p = p.parentWidget()
    return False


def _owner_of(t, top):
    """Поле, якому належить подія на `t` (усередині замкненого `top`).

    Внутрішні частини спінбокса чи списку належать йому самому; підписи й
    порожні місця контейнера — нікому (None), крім самого контейнера."""
    if t is top:
        return top
    w = t
    while w is not None:
        parent = w.parentWidget()
        if isinstance(parent, (QtWidgets.QAbstractSpinBox, QtWidgets.QComboBox,
                               QtWidgets.QAbstractScrollArea)):
            w = parent              # частина поля — подія належить полю
            if w is top:
                return top
            continue
        if w is top:
            return None             # підпис чи порожнє місце контейнера
        if isinstance(w, INPUTS) and not isinstance(w, QtWidgets.QScrollBar):
            return None if _is_help(w) else w
        w = parent
    return None


def free_controls(win) -> list:
    """Поля всередині замкнених контейнерів, що сітки не чіпають."""
    out = []
    for pname, names in FREE_INSIDE.items():
        panel = getattr(win, pname, None)
        for n in names:
            w = getattr(panel, n, None)
            if isinstance(w, QtWidgets.QWidget):
                out.append(w)
    return out


def mesh_controls(win) -> list:
    """Поля вікна `win`, що змінюють сітку, — за LOCKED і станом сцени."""
    out: list = []
    for pname, spec in LOCKED.items():
        panel = getattr(win, pname, None)
        if panel is None:
            continue
        if spec == "*" or (isinstance(spec, tuple) and spec and spec[0] == "*"):
            skip = {getattr(panel, n, None) for n in spec[1:]} if spec != "*" else set()
            skip.discard(None)
            out += [w for w in _inputs(panel)
                    if w not in skip and not _covered(w, skip)]
            continue
        for n in spec:
            if n == "+buttons":
                out += [w for w in _inputs(panel)
                        if isinstance(w, QtWidgets.QPushButton)]
                continue
            w = getattr(panel, n, None)
            if isinstance(w, QtWidgets.QWidget):
                out.append(w)
    cfg = getattr(win, "cfg", None)
    # Кришки двигунів: стисливий дозвуковий режим робить їх порталами, а
    # не стінками сітки (CaseCfg.portal_mode) — тоді й режим міняє сітку.
    if cfg is not None and cfg.port_patches:
        w = getattr(getattr(win, "panel_flow", None), "cmb_regime", None)
        if w is not None:
            out.append(w)
    # Пульт рухає поверхні лише через завіси; без завіс він сітки не чіпає.
    parts = [p for p in getattr(win, "parts", []) if p.loaded and p.enabled]
    if any(getattr(p, "hinge", None) is not None for p in parts):
        tx = getattr(win, "panel_tx", None)
        if tx is not None:
            out += [w for w in _inputs(tx) if not _is_help(w)]
            out += [w for w in tx.findChildren(panels.StickPad)]
    seen, uniq = set(), []
    for w in out:
        if id(w) not in seen:
            seen.add(id(w))
            uniq.append(w)
    return uniq


@dataclass
class ProjectMesh:
    """Сітка, що прийшла з проєкту, і чи вона ще діє."""
    key: str = ""           # ключ, під яким сітка лежить у проєкті
    cells: int = 0
    project: str = ""       # файл проєкту
    baseline: str = ""      # ключ теперішніх налаштувань одразу після відкриття
    active: bool = False    # сітка діє, поля замкнені
    released: bool = False  # людина погодилась перебудувати

    def label(self) -> str:
        if self.active:
            return (tr("Сітка з проєкту: {v:.2f} млн комірок, "
                    "зафіксована", v=self.cells / 1e6)).replace(".", ",")
        if self.released:
            return ("Сітку з проєкту не використовую: налаштування сітки "
                    "змінено, прогін збудує нову")
        return ""


class MeshLock(QtCore.QObject):
    """Замок на полях: приглушені, а клік чи клавіша спершу питає.

    `ask(widget) -> bool` показує питання; True — людина погодилась
    перебудувати сітку. Тоді `on_release()` знімає замок з УСІХ полів, а
    дія людини доходить до поля (клік — кліком, клавіша — клавішею).
    Програмні зміни (load_cfg, відкриття проєкту) замок не чіпає: він
    ловить лише ввід людини.
    """

    def __init__(self, ask, on_release, parent=None):
        super().__init__(parent)
        self._ask = ask
        self._on_release = on_release
        self._owner: dict = {}       # id(обʼєкт, що отримує події) -> поле
        self._watched: list = []
        self._dimmed: list = []
        self._asking = False

    @property
    def locked(self) -> bool:
        return bool(self._dimmed)

    def widgets(self) -> list:
        return [w for w in self._dimmed]

    def lock(self, widgets, free=()) -> None:
        """Замкнути `widgets` (контейнер — разом із полями в ньому, крім
        `free`)."""
        self.unlock()
        free = list(free)
        for w in widgets:
            targets = [w] + [c for c in w.findChildren(QtWidgets.QWidget)]
            for t in targets:
                if id(t) in self._owner or _covered(t, free):
                    continue
                owner = _owner_of(t, w)
                if owner is None:
                    continue
                self._owner[id(t)] = owner
                t.installEventFilter(self)
                self._watched.append(t)
            if w.graphicsEffect() is None:
                eff = QtWidgets.QGraphicsOpacityEffect(w)
                eff.setOpacity(DIM)
                w.setGraphicsEffect(eff)
            w.setProperty("mesh_locked", True)
            tip = w.toolTip()
            if not tip.startswith("🔒"):     # not locked yet (any language)
                w.setProperty("mesh_tip", tip)
                w.setToolTip(tr("🔒 Змінює сітку з проєкту — клік спитає, "
                                "чи перебудувати її.{v}", v="\n\n" + tip if tip else ""))
            self._dimmed.append(w)

    def unlock(self) -> None:
        for t in self._watched:
            try:
                t.removeEventFilter(self)
            except RuntimeError:        # віджет уже знищено
                pass
        for w in self._dimmed:
            try:
                eff = w.graphicsEffect()
                if isinstance(eff, QtWidgets.QGraphicsOpacityEffect):
                    w.setGraphicsEffect(None)
                w.setProperty("mesh_locked", False)
                w.setToolTip(w.property("mesh_tip") or "")
            except RuntimeError:
                pass
        self._owner.clear()
        self._watched.clear()
        self._dimmed.clear()

    # --- події ------------------------------------------------------------

    def eventFilter(self, obj, event):
        t = event.type()
        owner = self._owner.get(id(obj))
        if owner is None:
            return False
        if t in (QtCore.QEvent.ContextMenu, QtCore.QEvent.Drop,
                 QtCore.QEvent.DragEnter):
            # Контекстне меню (вставити) і перетягування — тихо не пускаємо.
            return True
        if t == QtCore.QEvent.KeyPress:
            if event.key() in _NAV_KEYS or event.matches(
                    QtGui.QKeySequence.Copy):
                return False
            return not self._confirm(owner, deliver=True)
        if t in (QtCore.QEvent.MouseButtonPress,
                 QtCore.QEvent.MouseButtonDblClick):
            if isinstance(owner, QtWidgets.QGroupBox) and obj is owner:
                # Групу міняє лише галочка в заголовку; клік по порожньому
                # місці групи нічого не робить — і не питаємо.
                if event.position().y() > 1.8 * owner.fontMetrics().height():
                    return False
            if self._confirm(owner, deliver=False):
                self._replay(owner)
            return True
        return False

    def _confirm(self, owner, deliver: bool) -> bool:
        if self._asking:
            return False
        self._asking = True
        try:
            ok = bool(self._ask(owner))
        finally:
            self._asking = False
        if ok:
            self.unlock()
            self._on_release()
        return ok

    @staticmethod
    def _replay(w) -> None:
        """Дія людини після згоди: те, що зробив би сам клік."""
        def go():
            try:
                if isinstance(w, QtWidgets.QAbstractButton):
                    w.click()
                elif isinstance(w, QtWidgets.QGroupBox) and w.isCheckable():
                    w.setChecked(not w.isChecked())
                elif isinstance(w, QtWidgets.QComboBox):
                    w.showPopup()
                else:
                    w.setFocus(QtCore.Qt.MouseFocusReason)
            except RuntimeError:
                pass
        QtCore.QTimer.singleShot(0, go)
