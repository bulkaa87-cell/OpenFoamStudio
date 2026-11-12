"""OpenFOAM Studio — підготовка і запуск зовнішнього обтікання зі STL.

Запуск:  .venv\\Scripts\\python.exe foam_studio.py
"""

from __future__ import annotations

import dataclasses
import os
import sys
import shutil
import tempfile
import threading
import time
import traceback
from dataclasses import dataclass, field
from pathlib import Path

os.environ.setdefault("QT_API", "pyside6")

import fs_compat  # noqa: F401  — лагодить six+PySide6, має бути найпершим

import matplotlib
matplotlib.use("Agg")          # Qt-бекенд не потрібен: графіки статичні
from matplotlib.backends.backend_agg import FigureCanvasAgg
from matplotlib.figure import Figure

import numpy as np
from PySide6 import QtCore, QtGui, QtWidgets

import pyvista as pv
from pyvistaqt import QtInteractor

import fs_advisor as adv
import fs_anim as fa
import fs_player
import fs_servertimes as fst
import fs_timebar
import fs_timesdlg
import fs_timeline as ftl
import fs_live
import fs_livebar
import fs_backend as be
import fs_case as fc
import fs_caseguard as guard
import fs_crashlog as crashlog
import fs_geometry as geo
import fs_i18n
import fs_icons
import fs_library as lib
import fs_memfit as memfit
import fs_meshlock as meshlock
import fs_meshfix as meshfix
import fs_repair
import fs_panels as panels
import fs_physics as phys
import fs_settings as fset
import fs_settingsdlg
import fs_serverwiz
import fs_restart
import fs_post as post
import fs_prerun as prerun
import fs_prerunwin
import fs_queue as fq
import fs_queuewin
import fs_runs
import fs_recover
import fs_slicebar as slicebar
import fs_statusbar as sbar
import fs_views as fv
import fs_yplus as fy
import fs_fmt as fmt
from fs_i18n import tr

import fs_paths

# Program files (help pages): read only in an installed copy.
APP_DIR = fs_paths.program_dir()
# Cases, projects, session: next to the program in a source copy,
# Documents/OpenFOAM Studio in an installed copy (fs_paths).
DATA_DIR = fs_paths.data_dir()
# Рядок журналу: почалося повернення результатів (індикатор «не вимикайте»).
FETCH_MARK = "===== copying results back to the Windows disk ====="
DEFAULT_CASES = DATA_DIR / "cases"

# Куди складаємо дрібні налаштування вікна (реєстр Windows). Виділено в
# константи, щоб тести могли взяти власну область і не лізти в справжні
# налаштування користувача: прогін тестів не має міняти те, з чим людина
# потім відкриє програму.
SETTINGS_ORG = "OpenFOAM Studio"
SETTINGS_APP = "ui"


def _flag(value, default: bool) -> bool:
    """QSettings віддає то bool, то рядок «false» — залежно від платформи."""
    if isinstance(value, bool):
        return value
    if value is None:
        return default
    return str(value).strip().lower() in ("true", "1", "yes")


MAX_PARTS = 12          # скільки моделей памʼятає список
# Від скількох трикутників перевірка й підготовка до показу йдуть поза
# потоком вікна. На 4.8 млн самі лише нормалі й підсвітка тримали вікно
# 3.6 с, а діагностика при додаванні другої моделі — ще 19; Windows пише
# «не відповідає» вже після п'яти. Дрібне рахується на місці: потік і
# вікно поступу коштують більше, ніж сама робота.
BIG_MESH = 200_000
NEWLINE = chr(10)

COL_BODY = "#b9c4cf"
COL_DOMAIN = "#5a7d9a"
COL_BOX = "#d49a3a"
COL_POINT = "#3fa34d"
COL_FLOW = "#c0504d"
COL_HOLE = "#ff2d2d"
COL_FLIP = "#e252d8"      # грані вивернутих частин
COL_NM = "#ffd23f"        # non-manifold ребра
COL_DEGEN = "#5ef08a"     # вироджені трикутники
COL_SPOT = "#00e5ff"      # куди підлетіла камера
COL_COP = "#ff17c4"       # центр тиску в результатах
COL_ROTOR_AXIS = "#a855f7"  # вісь обертання вибраного гвинта

# Шари проблемної геометрії: ключ -> (підпис, колір, що з цим робити).
# Підпис у смузі під вʼюпортом фарбується в колір самого шару — щоб не
# гадати, що означає рожеве на моделі, і відразу бачити, чого саме в цій
# моделі бракує.
PROBLEM_LAYERS = {
    "holes": ("Holes", COL_HOLE,
              "Open edges. A hole larger than a cell becomes a channel "
              "into the body. “Repair” closes it (meshfix method)."),
    "flip": ("Flipped", COL_FLIP,
             "Faces of parts whose normals point inward. They break the "
             "“inside/outside” checks in snappy. “Repair” flips them "
             "back."),
    "nm": ("Non-manifold", COL_NM,
           "Edges shared by more than two triangles — traces of parts "
           "that cut into each other. Castellation tolerates this; "
           "feature edges are fixed by includedAngle 170 or a Boolean "
           "union in CAD."),
    "degen": ("Degenerate", COL_DEGEN,
              "Triangles with zero area. “Repair” removes them."),
}

_LAYER_STYLE = {
    "holes": dict(color=COL_HOLE, line_width=6, render_lines_as_tubes=True),
    "nm": dict(color=COL_NM, line_width=3),
    "flip": dict(color=COL_FLIP, opacity=0.9, show_edges=False),
    "degen": dict(color=COL_DEGEN, point_size=13,
                  render_points_as_spheres=True),
}


def _drop_progress(box) -> None:
    """Прибрати вікно поступу НАЗАВЖДИ, а не лише сховати.

    QProgressDialog ще в конструкторі заводить таймер «показатись через 4 с»
    (setMinimumDuration при діапазоні 0..0 його не переставляє). Зупиняє
    таймер лише cancel(); close() доходить до нього через closeEvent →
    canceled → cancel. Але відколи у вікні є рідне вікно foamview
    (WA_NativeWindow, 4f680f8), Qt дає діалогу свій QWindow ще до показу, і
    close() схованого діалогу йде в QWindow::close() без closeEvent. Таймер
    живе, і через 4 с після початку читання вискакує вже закрите модальне
    «Читаю… Перевіряю геометрію…» — і висить, бо закривати його вже нікому
    (2026-11-09: будь-яке читання коротше 4 с; з VTK такого не було).
    """
    if box is None:
        return
    box.cancel()
    box.close()
    box.deleteLater()


def _mesh_key(mesh):
    """Відбиток сітки: та сама геометрія — той самий ключ (кеш перевірки).

    За вмістом, а не за `id`: ремонт і масштаб міняють сітку на місці, і
    ключ за обʼєктом віддав би стару відповідь. 250 тис. трикутників — ~10 мс.
    """
    import hashlib
    if mesh is None:
        return None
    h = hashlib.blake2b(digest_size=16)
    h.update(np.ascontiguousarray(mesh.points).tobytes())
    faces = getattr(mesh, "faces", None)
    if faces is not None:
        h.update(np.ascontiguousarray(faces).tobytes())
    return (int(mesh.n_points), int(mesh.n_cells), h.hexdigest())


@dataclass
class AngleSeries:
    """Серія прогонів, що різняться лише кутом атаки (див. start_series)."""
    base_name: str                  # назва й кут, які повернути наприкінці
    base_aoa: float
    angles: list
    at: int = -1                    # котрий кут рахується зараз
    done: list = field(default_factory=list)      # (кут, каталог кейсу)
    failed: list = field(default_factory=list)    # (кут, чому)
    stopped: bool = False           # людина натиснула «Стоп»


# ==========================================================================
# Журнал
# ==========================================================================

@dataclass
class Part:
    """Одна модель у сцені — або рядок історії, якщо `mesh` ще немає.

    `mesh` тримаємо ВЖЕ в метрах: множник одиниць у нього вбито при
    завантаженні, а `factor` памʼятає, який саме. Без цього збірка з файлів у
    різних одиницях (фюзеляж у метрах, антена в міліметрах) склалася б у
    безглуздя, і жоден спільний множник її б не врятував.
    """

    path: str
    mesh: object = None
    factor: float = 1.0        # множник, уже застосований до mesh
    auto: float = 1.0          # що підказало автовизначення для цього файлу
    why: str = ""
    enabled: bool = True
    open_edges: int = 0
    bad_winding: int = 0
    inverted: int = 0
    wrapped: bool = False      # геометрію перерізано воксельним обгортанням
    notes: list = field(default_factory=list)
    # ЧИМ ця модель є в розрахунку: body | rotor | motion. Див. PART_ROLES
    # у fs_case. Типово «body», і поки всі такі, кейс виходить точно той
    # самий, що й до появи ролей.
    role: str = "body"
    rpm: float = 3000.0        # оберти, якщо роль «rotor»
    # Вісь обертання. Задає ЛЮДИНА, а не геометрія: плаский
    # двулопатевий гвинт габаритом не відрізнити від пластини, і
    # автовизначення давало для нього Y замість X.
    rotor_axis: str = "x"
    # Якому двигуну належить кришка — для ролей intake і nozzle.
    engine: str = "engine1"
    # Параметри двигуна (fc.Engine) — їх тримає СОПЛО, бо сопло в двигуна
    # одне, а повітрозабірників буває кілька. None — типові.
    jet: object = None
    # Числа гвинта-диска (fc.ActuatorDisk) для ролі «disk». Центр, вісь і
    # радіус сюди не входять — вони щоразу беруться з геометрії.
    disk: object = None
    # КЕШ геометрії, якою деталь була в сцені, коли пішла в історію: уже в
    # метрах, розвернута й відремонтована. Повторна галочка читає ЙОГО, а
    # не вихідний файл — інакше ремонт мовчки пропадав (2026-11-08).
    cache: str = ""
    # ВЛАСНИЙ рівень згущення поверхні для цієї деталі: (min, max) або
    # None — «як усі».
    #
    # Потрібен там, де дрібна деталь стоїть поруч із великою. Лопать
    # гвинта 8x4.5 має хорду близько 20 мм і товщину 2 — на спільній
    # комірці 4.4 мм від неї в сітці лишається пластинка в одну комірку
    # завтовшки, тимчасом як фюзеляж на тій самій комірці описаний добре.
    # Підняти рівень усім означало б покласти ті самі 1.1 мм на весь
    # фюзеляж і отримати десятки мільйонів комірок ні за що.
    #
    # Поле в BodyPatch було давно, і snappyHexMesh його поважав, але
    # поставити його не могло ніщо: _sync_bodies перезбирав патчі без
    # нього на кожну зміну сцени. Тобто важіль існував і не діяв.
    #
    # Зветься НЕ `level`, хоч у BodyPatch воно зветься саме так: у Part
    # це імʼя вже зайняте властивістю, яка каже «ok/warn/bad» про якість
    # геометрії й розфарбовує список деталей. Dataclass не сперечається —
    # він тихо створює поле поверх властивості, і падає воно аж при
    # СТВОРЕННІ деталі, за кадром нитки завантаження: «property level has
    # no setter», жодного вікна, порожня сцена.
    refine: tuple[int, int] | None = None
    # ОДИНИЦІ ВИРІШЕНО НАЗАВЖДИ. Деталь із сесії, збереженого кейсу,
    # проєкту чи з генератора гвинта вже лежить у метрах за побудовою:
    # її не читали з чужого файлу, її записала ця сама програма. Поле
    # «Одиниці STL» — це правило для НОВИХ файлів, і до такої деталі воно
    # не стосується.
    #
    # Без цього прапорця поле одиниць, відновлене з сесії як «міліметри»,
    # перемасштабовувало геометрію з сесії ще раз: модель 0.514 м стала
    # 0.5 мм, домен стиснувся з нею, а коробка згущення на абсолютних
    # координатах лишилася — і виглядала гігантською. Людина побачила це
    # раніше за мене, бо я звіряв файл, а не картинку.
    pinned: bool = False
    # ЗАВІСА: де вісь відхилення й від якого каналу пульта воно залежить.
    # None — деталь нерухома, і тоді `posed` віддає рівно `mesh`, тобто
    # шлях без керування лишається той самий, що й був.
    hinge: object = None
    # Кеш пози: рахувати поворот на кожне перемальовування означало б
    # копіювати сотні тисяч трикутників на кожен рух стіка.
    _posed: object = field(default=None, repr=False)
    _posed_key: object = field(default=None, repr=False)

    @property
    def name(self) -> str:
        return Path(self.path).name or self.path or tr("model")

    def angle_at(self, channels) -> float:
        """Кут відхилення цієї деталі при даному положенні стіків."""
        h = self.hinge
        if h is None or not h.on or h.channel == "throttle":
            return 0.0
        return h.angle(float(channels.get(h.channel, 0.0)))

    def posed(self, channels):
        """Геометрія В ПОЗІ — тобто з відхиленою поверхнею.

        Рахується ЗАВЖДИ ВІД `mesh`, ніколи від попередньої пози. Інакше
        нейтраль повзла б: стік туди-сюди десять разів — і елерон уже не
        там, де був на початку. `mesh` при цьому не змінюється зовсім.
        """
        if not self.loaded:
            return None
        a = self.angle_at(channels)
        h = self.hinge
        if h is None or not h.on or (abs(a) < 1e-12
                                     and not any(abs(float(v)) > 1e-12
                                                 for v in h.offset)):
            return self.mesh
        key = (id(self.mesh), round(a, 9), tuple(h.pivot), tuple(h.axis),
               tuple(h.offset))
        if self._posed_key != key:
            self._posed = geo.hinge_pose(self.mesh, h.pivot, h.axis, a,
                                         h.offset)
            self._posed_key = key
        return self._posed

    def live_rpm(self, channels) -> float:
        """Оберти з урахуванням газу, якщо деталь на нього призначена."""
        h = self.hinge
        if h is not None and h.on and h.channel == "throttle":
            return float(self.rpm) * float(channels.get("throttle", 0.0))
        return float(self.rpm)

    @property
    def loaded(self) -> bool:
        return self.mesh is not None

    @property
    def tris(self) -> int:
        return int(self.mesh.n_cells) if self.loaded else 0

    @property
    def span(self) -> float:
        if not self.loaded:
            return 0.0
        b = self.mesh.bounds
        return max(b[1] - b[0], b[3] - b[2], b[5] - b[4])

    @property
    def level(self) -> str:
        if not self.loaded:
            return "ok"
        # Кришка двигуна — відкрита поверхня за побудовою: диск, що закриває
        # отвір. Її край і є «відкритими ребрами», і червоною вона бути не
        # повинна. Замкненість тіла разом із кришками перевіряється окремо.
        holes = self.open_edges if self.role not in fc.PORT_ROLES else 0
        if holes or self.bad_winding:
            return "bad"
        return "warn" if self.inverted else "ok"


# Ті поля двигуна, які задає ЛЮДИНА. Решту (кришки, площі, газ) вікно
# щоразу бере зі сцени, і зберігати їх у проєкті означало б тягнути
# застарілі площі з минулого разу.
JET_USER_FIELDS = ("kind", "thrust_mode", "thrust", "v_jet", "t_jet",
                   "fuel_frac", "direct", "mdot", "v_exit", "from_throttle",
                   "refine")


DISK_USER_FIELDS = ("thrust", "torque", "profile", "hub", "from_throttle",
                    "refine")


def _disk_to_row(disk) -> dict:
    return {k: getattr(disk, k) for k in DISK_USER_FIELDS}


def _disk_from_row(row):
    """Числа гвинта-диска з проєкту чи панелі. Криве — типові."""
    if not isinstance(row, dict):
        return None
    base = fc.ActuatorDisk()
    out = {}
    for k in DISK_USER_FIELDS:
        if k not in row:
            continue
        want = type(getattr(base, k))
        try:
            out[k] = want(row[k])
        except (TypeError, ValueError):
            continue
    if out.get("profile") not in fc.DISK_PROFILE_KEYS:
        out.pop("profile", None)
    for k in ("thrust", "torque", "hub"):
        if k in out and not (out[k] >= 0):
            out.pop(k)
    return dataclasses.replace(base, **out)


def _jet_to_row(jet) -> dict:
    return {k: getattr(jet, k) for k in JET_USER_FIELDS}


def _jet_from_row(row):
    """Параметри двигуна з проєкту. Кривий запис — типові, а не падіння."""
    if not isinstance(row, dict):
        return None
    base = fc.Engine()
    out = {}
    for k in JET_USER_FIELDS:
        if k not in row:
            continue
        want = type(getattr(base, k))
        try:
            out[k] = want(row[k])
        except (TypeError, ValueError):
            continue
    if out.get("kind") not in fc.ENGINE_KIND_KEYS:
        out.pop("kind", None)
    # Проєкт, збережений до Q31, ключа не має — тоді тяга означала «у
    # польоті», і так її й читаємо, щоб старий двигун тягнув, як тягнув.
    if "thrust_mode" not in row and "thrust" in row:
        out["thrust_mode"] = "flight"
    if out.get("thrust_mode", "static") not in fc.THRUST_MODE_KEYS:
        out.pop("thrust_mode", None)
    return dataclasses.replace(base, **out)


def tail_lines(text: str, n: int) -> str:
    """Останні `n` рядків тексту (рядки через "\\n")."""
    cut = len(text)
    for _ in range(n):
        cut = text.rfind("\n", 0, cut)
        if cut < 0:
            return text
    return text[cut + 1:]


class LogView(QtWidgets.QPlainTextEdit):
    """Журнал з буферизацією — інакше десятки тисяч рядків підвішують вікно."""

    MAX_BLOCKS = 20000

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setReadOnly(True)
        self.setMaximumBlockCount(self.MAX_BLOCKS)
        self.setLineWrapMode(QtWidgets.QPlainTextEdit.NoWrap)
        font = QtGui.QFont("Consolas", 9)
        font.setStyleHint(QtGui.QFont.Monospace)
        self.setFont(font)
        self._buffer: list[str] = []
        self._timer = QtCore.QTimer(self)
        self._timer.setInterval(120)
        self._timer.timeout.connect(self.flush)
        self._timer.start()

    def push(self, line: str):
        self._buffer.append(line)

    def flush(self):
        if not self._buffer:
            return
        chunk, self._buffer = self._buffer, []
        bar = self.verticalScrollBar()
        at_end = bar.value() >= bar.maximum() - 4
        # Більше за MAX_BLOCKS рядків вікно однаково не тримає. Без обрізки
        # підключення до довгого прогону вливало б сюди весь журнал (40 МБ),
        # і Qt довго верстав би рядки, які одразу ж і викине.
        self.appendPlainText(tail_lines("\n".join(chunk), self.MAX_BLOCKS))
        if at_end:
            bar.setValue(bar.maximum())

    def clear_log(self):
        self._buffer.clear()
        self.clear()


# ==========================================================================
# Графіки збіжності
# ==========================================================================

class PlotView(QtWidgets.QWidget):
    """Резидуали й коефіцієнти. Рендер через Agg у QLabel — див. коментар
    біля імпорту matplotlib."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.fig = Figure(figsize=(8, 7), dpi=100, layout="constrained")
        self.canvas = FigureCanvasAgg(self.fig)
        self.ax_res = self.fig.add_subplot(211)
        self.ax_co = self.fig.add_subplot(212)

        self.label = QtWidgets.QLabel()
        self.label.setAlignment(QtCore.Qt.AlignCenter)
        self.label.setMinimumSize(400, 300)
        self.label.setSizePolicy(QtWidgets.QSizePolicy.Ignored,
                                 QtWidgets.QSizePolicy.Ignored)

        bar = QtWidgets.QHBoxLayout()
        self.btn_save = QtWidgets.QPushButton("Save Plot…")
        self.btn_save.clicked.connect(self._save)
        bar.addWidget(self.btn_save)
        bar.addStretch(1)

        lay = QtWidgets.QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.addWidget(self.label, 1)
        lay.addLayout(bar)

        self._last = ({}, {}, 1e-4)
        self._steady = True
        self._setup()

    def _save(self):
        path, _ = QtWidgets.QFileDialog.getSaveFileName(
            self, tr("Save Plot"), "convergence.png", "PNG (*.png)")
        if path:
            self.fig.savefig(path, dpi=150)

    def resizeEvent(self, event):
        super().resizeEvent(event)
        w = max(self.label.width(), 320)
        h = max(self.label.height(), 240)
        self.fig.set_size_inches(w / 100.0, h / 100.0)
        self._blit()

    def _blit(self):
        self.canvas.draw()
        w, h = self.canvas.get_width_height()
        buf = np.asarray(self.canvas.buffer_rgba())
        img = QtGui.QImage(buf.tobytes(), w, h, QtGui.QImage.Format_RGBA8888)
        self.label.setPixmap(QtGui.QPixmap.fromImage(img))

    def _setup(self):
        # Вісь у стаціонарі — номер ітерації, у нестаціонарі — секунди. Це та
        # сама колонка «Time» в логу, але підпис «ітерація» над секундами
        # збивав би з пантелику, а заголовок «Збіжність» над нестаціонаром —
        # тим паче: там ніщо не сходиться, розрахунок просто йде далі.
        axis = tr("iteration") if self._steady else tr("time, s")
        self.ax_res.set_yscale("log")
        self.ax_res.set_xlabel(axis)
        self.ax_res.set_ylabel(tr("initial residual"))
        self.ax_res.grid(True, which="both", alpha=0.25)
        self.ax_res.set_title(tr("Convergence") if self._steady else tr("Residuals"),
                              fontsize=10)
        self.ax_co.set_xlabel(axis)
        self.ax_co.set_ylabel(tr("coefficient"))
        self.ax_co.grid(True, alpha=0.25)
        self.ax_co.set_title(tr("Aerodynamic coefficients"), fontsize=10)

    def set_steady(self, steady: bool) -> None:
        if steady != self._steady:
            self._steady = steady
            self.update_plots(*self._last)

    def update_plots(self, residuals: dict, coeffs: dict, tol: float = 1e-4):
        self._last = (residuals, coeffs, tol)
        self.ax_res.clear()
        self.ax_co.clear()
        self._setup()

        # Ряди наповнює потік прогону просто зараз, тому спершу знімок, і вже
        # з нього обидві осі. Два окремі проходи по «живому» списку час від
        # часу дають xs і ys різної довжини, і malplotlib кидає ValueError.
        for name, series in sorted(list(residuals.items())):
            pts = list(series)
            if len(pts) < 2:
                continue
            xs = [p[0] for p in pts]
            ys = [max(p[1], 1e-20) for p in pts]
            self.ax_res.plot(xs, ys, lw=1.1, label=name)
        if residuals:
            self.ax_res.axhline(tol, color="#888", ls="--", lw=0.8)
            self.ax_res.legend(fontsize=8, ncol=4, loc="upper right")

        for name, series in sorted(list(coeffs.items())):
            pts = list(series)
            if len(pts) < 2 or name not in ("Cd", "Cl", "CmPitch"):
                continue
            xs = [p[0] for p in pts]
            ys = [p[1] for p in pts]
            self.ax_co.plot(xs, ys, lw=1.1, label=name)
        if coeffs:
            self.ax_co.legend(fontsize=8, loc="best")
            # Ранні ітерації дають величезні викиди й ховають корисну частину.
            allv = [v for n, p in coeffs.items() if n in ("Cd", "Cl")
                    for _, v in p[len(p) // 4:]]
            if allv:
                lo, hi = min(allv), max(allv)
                pad = max((hi - lo) * 0.2, 1e-3)
                self.ax_co.set_ylim(lo - pad, hi + pad)

        self._blit()


# ==========================================================================
# 3D-вʼюпорт
# ==========================================================================

class Viewport(QtWidgets.QWidget):
    # «До моделі» тут лише ПРОСЯТЬ. Камеру ставить вікно: вʼюпортів два
    # (3D і результати), а команда одна — на обидва.
    to_model_requested = QtCore.Signal()
    # Хто малює «3D» (текст для рядка стану, причина вибору) — і коли
    # віджет сам перейшов на запасний рівень під час роботи.
    level_changed = QtCore.Signal(str, str)

    def __init__(self, parent=None):
        super().__init__(parent)
        lay = QtWidgets.QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        self.plotter = self._make_plotter()
        # Stretch 1: ALL the free height goes to the view. Without it, the
        # free height went to the view AND to the controls row: the
        # foamview widget has the "Preferred" policy (the VTK widget has
        # "Expanding"), so the layout shared the space. The FlowLayout
        # puts the controls at the top of its row, and the remainder below
        # the controls was an empty grey area (T13, 2026-11-10).
        lay.addWidget(self.plotter.interactor, 1)
        self.plotter.set_background("#22262b", top="#3a4149")
        self.plotter.add_axes(line_width=2)
        self.actors: dict[str, object] = {}
        self._body_bounds: tuple | None = None
        self._label: dict[str, str] = {}

        # Рядок, що ПЕРЕНОСИТЬСЯ: у звичайному рядку ці шістнадцять віджетів
        # не давали вікну стати вужчим за ~1750 px (див. panels.FlowLayout).
        bar_box = QtWidgets.QWidget()
        policy = QtWidgets.QSizePolicy(QtWidgets.QSizePolicy.Preferred,
                                       QtWidgets.QSizePolicy.Preferred)
        policy.setHeightForWidth(True)
        bar_box.setSizePolicy(policy)
        bar = panels.FlowLayout(bar_box, spacing=6)
        bar.setContentsMargins(6, 2, 6, 4)
        self.chk = {}
        for key, label, default in (
            ("body", "Model", True),
            ("holes", "Holes", True),
            ("flip", "Flipped", True),
            ("nm", "Non-manifold", False),
            ("degen", "Degenerate", True),
            ("domain", "Domain", True),
            ("box", "Refinement", True),
            ("point", "Point in Flow", True),
            ("flow", "Flow Direction", True),
            ("ruler", "Ruler", False),
        ):
            c = QtWidgets.QCheckBox(label)
            c.setChecked(default)
            if key == "ruler":
                # Лінійку не ховаємо, а БУДУЄМО на вимогу: її побудова
                # чіпає камеру (див. show_ruler), тож тримати її готовою
                # про всяк випадок дорого.
                c.toggled.connect(lambda v: self.show_ruler()
                                  if v else self._drop("ruler"))
            else:
                c.toggled.connect(lambda v, k=key: self.set_visible(k, v))
            bar.addWidget(c)
            self.chk[key] = c
            self._label[key] = label
        for key in PROBLEM_LAYERS:
            self.set_problem(key, 0, "")
        for label, view in (("Iso", "iso"), ("X", "yz"), ("Y", "xz"), ("Z", "xy")):
            b = QtWidgets.QPushButton(label)
            b.setFixedWidth(38)
            b.clicked.connect(lambda _=False, v=view: self.set_view(v))
            bar.addWidget(b)
        self.btn_spot = QtWidgets.QPushButton("Next Problem ▸")
        self.btn_spot.setToolTip(
            "Fly to the next hole, flipped part or degenerate triangle — "
            "largest first")
        self.btn_spot.setEnabled(False)
        bar.addWidget(self.btn_spot)
        # Колись тут було «Модель» — так само, як галочка зліва, що ховає
        # тіло. Дві різні речі під одним словом в одній смузі.
        self.btn_fit = QtWidgets.QPushButton("Fit to model")
        self.btn_fit.setToolTip(
            "Camera fits the model in ALL viewports: both here and in "
            "the results. The side you look from stays the same. Ctrl+0")
        self.btn_fit.clicked.connect(lambda: self.to_model_requested.emit())
        bar.addWidget(self.btn_fit)
        self.btn_fit_all = QtWidgets.QPushButton("All")
        self.btn_fit_all.setToolTip("Fit the whole domain in the view")
        self.btn_fit_all.clicked.connect(self.fit_all)
        bar.addWidget(self.btn_fit_all)
        lay.addWidget(bar_box)

    def _make_plotter(self):
        """Хто малює вкладку «3D» — ланцюжок fs_view3d.select_level():
        foamview на GPU → foamview на програмному адаптері → VTK → foamview
        на CPU. Вкладка ЗАВЖДИ показує картинку; повільніше — можна.

        Решта класу однакова для всіх: fs_view3d.Plotter розуміє ті самі
        виклики pyvista, що й QtInteractor. FS_3D=gpu|soft|vtk|cpu — примусово.
        """
        self.backend = "vtk"
        self.backend_note = ""
        self.level = None
        try:
            import fs_view3d
            lv = fs_view3d.select_level()
            if lv.level in ("gpu", "soft"):
                # Кеш каже «цей адаптер малює» — перевірка в ЦЬОМУ процесі
                # (драйвер могли оновити без зміни версії). Не так — кеш
                # геть, VTK зараз, повний перебір — на наступному старті.
                ok, note = fs_view3d.probe(lv)
                if not ok:
                    fs_view3d.forget_level()
                    lv = fs_view3d.Level("vtk", note=f"{lv.text()}: {note}")
            if lv.foamview:
                pl = fs_view3d.Plotter(self, lv, on_level=self._level_changed)
                self.backend = "foamview"
                self._set_level(lv)
                return pl
            self._set_level(lv)
        except Exception as exc:                          # noqa: BLE001
            self.backend_note = str(exc)
        return QtInteractor(self)

    def _set_level(self, lv):
        self.level = lv
        self.backend_note = lv.note
        self.level_changed.emit(lv.text(), lv.note_text())

    def _level_changed(self, lv):
        """Віджет сам перейшов на CPU (GPU відмовив під час роботи)."""
        self._set_level(lv)

    # --- керування акторами ---------------------------------------------

    def _drop(self, key: str):
        if key in self.actors:
            # Шар може бути з КІЛЬКОХ акторів: лінійка це три лінійки по
            # осях плюс підпис. Тому список — не виняток, а звичайний
            # випадок, і обробляти його треба тут, а не в кожному місці.
            item = self.actors[key]
            for a in (item if isinstance(item, list) else [item]):
                try:
                    self.plotter.remove_actor(a, render=False)
                except Exception:
                    pass
            del self.actors[key]

    def set_visible(self, key: str, visible: bool):
        item = self.actors.get(key)
        if item is None:
            return
        for a in (item if isinstance(item, list) else [item]):
            try:
                a.SetVisibility(visible)
            except Exception:
                pass
        try:
            self.plotter.render()
        except Exception:
            pass

    def set_view(self, which: str):
        p = self.plotter
        {"iso": p.view_isometric, "xy": p.view_xy,
         "xz": p.view_xz, "yz": p.view_yz}[which]()

    def show_ruler(self, bounds=None):
        # ВИМКНЕНУ ЛІНІЙКУ НЕ БУДУЄМО ВЗАГАЛІ.
        #
        # Перша версія будувала її на кожен показ тіла й одразу ховала.
        # Виглядало дешево, а коштувало камери: `add_ruler` усередині
        # кличе `add_actor`, той — `reset_camera`, і наближений вид
        # відлітав від кожної зміни ролі чи налаштування деталі.
        # Користувач це помітив, а я ні: я дивився на видимість шару, а
        # не на те, що його побудова робить із камерою.
        if not self.chk["ruler"].isChecked():
            self._drop("ruler")
            return
        self._show_ruler_now(bounds)

    def _show_ruler_now(self, bounds=None):
        """Лінійки по трьох осях і габарит числами.

        НАВІЩО ЧИСЛА, А НЕ ЛИШЕ ЛІНІЇ. Питання, заради якого лінійку й
        просили, — «чи правильно прочиталися одиниці». На нього відповідає
        не вигляд, а розмір: літак має бути близько метра, а не міліметра
        й не кілометра. Тому підпис угорі показує габарит у міліметрах і
        метрах одразу — щоб помилку в тисячу разів було видно з одного
        погляду.

        Підписи ЛАТИНКОЮ й цифрами: у VTK немає кириличного шрифта, і
        українські літери вийшли б квадратиками.
        """
        self._drop("ruler")
        b = tuple(bounds) if bounds else self._body_bounds
        keep = (tuple(self.plotter.camera.position),
                tuple(self.plotter.camera.focal_point),
                float(self.plotter.camera.parallel_scale))
        if not b or b[1] <= b[0]:
            return
        lx, ly, lz = b[1] - b[0], b[3] - b[2], b[5] - b[4]
        # Лінійки ставимо ЗА тілом, а не крізь нього: інакше вони
        # ховаються в моделі й полічити по них нічого не можна.
        pad = 0.06 * max(lx, ly, lz, 1e-9)
        actors = []
        rulers = (
            ((b[0], b[2] - pad, b[4] - pad), (b[1], b[2] - pad, b[4] - pad),
             "X"),
            ((b[1] + pad, b[2], b[4] - pad), (b[1] + pad, b[3], b[4] - pad),
             "Y"),
            ((b[1] + pad, b[2] - pad, b[4]), (b[1] + pad, b[2] - pad, b[5]),
             "Z"),
        )
        for p1, p2, title in rulers:
            try:
                actors.append(self.plotter.add_ruler(
                    p1, p2, title=tr(title), font_size_factor=0.5,
                    label_format="%.3g", number_labels=5))
            except Exception:
                pass
        txt = (f"{lx * 1000:.0f} x {ly * 1000:.0f} x {lz * 1000:.0f} mm"
               f"   =   {lx:.3f} x {ly:.3f} x {lz:.3f} m")
        try:
            actors.append(self.plotter.add_text(
                txt, position="upper_left", font_size=11,
                color="#e8e8e8", name="ruler_text"))
        except Exception:
            pass
        if not actors:
            self.plotter.camera.position = keep[0]
            self.plotter.camera.focal_point = keep[1]
            self.plotter.camera.parallel_scale = keep[2]
            return
        self.actors["ruler"] = actors
        self.set_visible("ruler", self.chk["ruler"].isChecked())
        # ПОВЕРТАЄМО КАМЕРУ ТУДИ, ДЕ ВОНА БУЛА. `add_ruler` не приймає
        # `reset_camera`, тож інакше її не вбережеш: вона скидається
        # всередині, і побачити це можна лише по кадру.
        self.plotter.camera.position = keep[0]
        self.plotter.camera.focal_point = keep[1]
        self.plotter.camera.parallel_scale = keep[2]
        self.plotter.render()

    def show_ghosts(self, meshes) -> None:
        """Гвинти-диски — напівпрозорими, поверх тіла (Q20).

        Напівпрозорість тут несе зміст: модель видно, щоб розуміти, де
        гвинт, але в сітку вона не йде, і непрозорою виглядала б так,
        ніби йде.
        """
        self._drop("ghosts")
        if meshes:
            merged = (meshes[0] if len(meshes) == 1 else
                      meshes[0].merge(meshes[1:], merge_points=False))
            self.actors["ghosts"] = self.plotter.add_mesh(
                merged, color=COL_ROTOR_AXIS, opacity=0.3,
                smooth_shading=False, show_edges=False, name="ghosts",
                reset_camera=False)
        self.plotter.render()

    def show_body(self, mesh, reset=False):
        """Показує тіло. Нормалі мають бути ВЖЕ пораховані.

        `smooth_shading=True` тут стояти не може: pyvista на кожен показ
        перераховує нормалі з `non_manifold_traversal=True`, і на збірці з
        перетинних деталей вивертає кожну сьому грань — вони темніють, і
        модель виглядає рваною, хоч у файлі все гаразд. Готуємо нормалі
        самі (`geo.display_mesh`), а тут лише малюємо.
        """
        self._drop("body")
        if mesh is None:
            # Тіла нема — нема й куди підлітати. Без цього «До моделі» йшла
            # б туди, де тіло стояло востаннє.
            self._body_bounds = None
            self.plotter.render()
            return
        if "Normals" not in mesh.point_data:
            mesh = geo.display_mesh(mesh)
        self.actors["body"] = self.plotter.add_mesh(
            mesh, color=COL_BODY, smooth_shading=False, specular=0.3,
            show_edges=False, name="body", reset_camera=False)
        self.set_visible("body", self.chk["body"].isChecked())
        self._body_bounds = tuple(mesh.bounds)
        # Лінійка живе разом із тілом: габарит міняється від кожного
        # повороту, зсуву й зміни одиниць, і стара лінійка біля нового
        # тіла брехала б упевнено.
        self.show_ruler()
        if reset:
            self.fit_body()
        self.plotter.render()

    @property
    def has_model(self) -> bool:
        """Чи є в сцені тіло, до якого можна підлетіти."""
        return self._body_bounds is not None

    def fit_body(self):
        """Вписує в кадр МОДЕЛЬ, а не всю сцену.

        Домен більший за тіло разів у пʼятнадцять, тому reset_camera() без
        обмежень робить модель цяткою на пів екрана порожнечі — і поворот на
        90° стає візуально непомітним.
        """
        if self._body_bounds is not None:
            self.plotter.reset_camera(bounds=self._body_bounds)
        else:
            self.plotter.reset_camera()
        # Інакше наступний add_mesh знову вважатиме камеру «невстановленою»
        # і сам перескочить на межі всієї сцени.
        self.plotter.renderer.camera_set = True
        self.plotter.render()

    def fit_all(self):
        self.plotter.reset_camera()
        self.plotter.render()

    def look_at(self, point, radius: float, outward=None):
        """Підлітає до точки, по змозі не міняючи напрямку погляду.

        Напрямок лишаємо той самий свідомо: людина щойно дивилася на модель
        під зрозумілим їй кутом, і раптова зміна ракурсу збиває з пантелику
        сильніше, ніж допомагає. Але якщо з цього боку місце затулене самим
        тілом, дивитися туди марно — тоді заходимо ззовні, з боку `outward`.
        """
        camera = self.plotter.camera
        eye = np.array(camera.position, dtype=float)
        focus = np.array(camera.focal_point, dtype=float)
        direction = eye - focus
        length = float(np.linalg.norm(direction))
        if length < 1e-12:
            direction = np.array([1.0, -1.0, 0.6])
            length = float(np.linalg.norm(direction))
        direction = direction / length
        if outward is not None:
            out = np.asarray(outward, dtype=float)
            out_len = float(np.linalg.norm(out))
            if out_len > 1e-12:
                out = out / out_len
                if float(np.dot(direction, out)) < 0.25:
                    # Камера дивилася б крізь тіло: заходимо з вулиці,
                    # трохи збоку — так видно і саме місце, і його оточення.
                    direction = out * 0.8 + direction * 0.2
                    direction = direction / float(np.linalg.norm(direction))
        target = np.asarray(point, dtype=float)
        away = max(radius * 6.0, 1e-4)
        camera.focal_point = tuple(target)
        camera.position = tuple(target + direction * away)
        self.plotter.renderer.reset_camera_clipping_range()
        self.plotter.renderer.camera_set = True
        self.plotter.render()

    def show_problem(self, key: str, data, count: int = 0):
        """Шар проблемної геометрії: сама підсвітка плюс стан чекбокса.

        Показати МІСЦЕ важливіше, ніж назвати кількість: «38 дірок» у таблиці
        не каже, чи вони в хвостовому обтічнику, чи рівно там, де ви збираєтесь
        рахувати відрив.
        """
        self._drop(key)
        if data is not None and data.n_points:
            actor = self.plotter.add_mesh(
                data, name=key, reset_camera=False, **_LAYER_STYLE[key])
            self.actors[key] = actor
            # Підсвітка граней лежить рівно на поверхні тіла, тому без зсуву
            # по глибині вони з тілом бʼються за пікселі й підсвітка блимає
            # плямами або зникає зовсім.
            try:
                mapper = actor.GetMapper()
                mapper.SetResolveCoincidentTopologyToPolygonOffset()
                mapper.SetRelativeCoincidentTopologyPolygonOffsetParameters(
                    -6, -6)
                mapper.SetRelativeCoincidentTopologyLineOffsetParameters(-6, -6)
            except AttributeError:
                pass
            self.set_visible(key, self.chk[key].isChecked())
        self.set_problem(key, count)
        self.plotter.render()

    def set_problem(self, key: str, count: int, tip: str = ""):
        """Підпис чекбокса: скільки цього в моделі й що з цим робити."""
        chk = self.chk.get(key)
        if chk is None:
            return
        label, colour, advice = PROBLEM_LAYERS[key]
        if count:
            chk.setEnabled(True)
            chk.setText(tr("{label} {count}", label=label,
                           count=fmt.count(count)))
            chk.setStyleSheet(f"color:{colour}; font-weight:bold;")
            chk.setToolTip(advice)
        else:
            chk.setEnabled(False)
            chk.setText(label)
            chk.setStyleSheet("color:#6b7278;")
            chk.setToolTip(tr("{label}: this model does not have it", label=label))

    def mark_spot(self, centre, radius: float):
        """Ставить помітну кулю там, куди щойно підлетіли.

        Саму дірку буває затулено обшивкою — тоді людина дивиться в
        потрібне місце й не бачить нічого. Куля видно завжди, і з неї
        зрозуміло, що камера не промахнулася.
        """
        self._drop("spot")
        if centre is None:
            self.plotter.render()
            return
        sphere = pv.Sphere(radius=max(radius, 1e-5) * 0.75, center=centre)
        self.actors["spot"] = self.plotter.add_mesh(
            sphere, color=COL_SPOT, opacity=0.35, name="spot",
            reset_camera=False)
        self.plotter.render()

    def clear_problems(self):
        self._drop("spot")
        for key in PROBLEM_LAYERS:
            self._drop(key)
            self.set_problem(key, 0)

    def show_domain(self, cfg: fc.CaseCfg):
        self._drop("domain")
        box = pv.Box(bounds=cfg.bounds)
        self.actors["domain"] = self.plotter.add_mesh(
            box, style="wireframe", color=COL_DOMAIN, line_width=2,
            name="domain", reset_camera=False)
        self.set_visible("domain", self.chk["domain"].isChecked())

    def show_boxes(self, cfg: fc.CaseCfg):
        self._drop("box")
        if not cfg.mesh.boxes:
            return
        merged = None
        for b in cfg.mesh.boxes:
            blk = pv.Box(bounds=(b.min[0], b.max[0], b.min[1], b.max[1],
                                 b.min[2], b.max[2]))
            merged = blk if merged is None else merged.merge(blk)
        self.actors["box"] = self.plotter.add_mesh(
            merged, style="wireframe", color=COL_BOX, line_width=2, name="box",
            reset_camera=False)
        self.set_visible("box", self.chk["box"].isChecked())

    def show_point(self, cfg: fc.CaseCfg):
        self._drop("point")
        r = max(cfg.domain_size) * 0.012
        sphere = pv.Sphere(radius=r, center=cfg.location_in_mesh)
        self.actors["point"] = self.plotter.add_mesh(
            sphere, color=COL_POINT, name="point", reset_camera=False)
        self.set_visible("point", self.chk["point"].isChecked())

    def show_flow(self, cfg: fc.CaseCfg):
        self._drop("flow")
        x0, x1, y0, y1, z0, z1 = cfg.bounds
        # Стрілку маштабуємо від домену, а не від тіла: інакше на тонкому
        # крилі в широкій трубі її просто не видно.
        length = max(cfg.domain_size[0] * 0.22, cfg.body_length)
        start = (x0 + (cfg.bbox_min[0] - x0) * 0.1,
                 cfg.centre[1], cfg.centre[2])
        arrow = pv.Arrow(start=start, direction=cfg.flow.direction,
                         scale=length, tip_length=0.25, shaft_radius=0.035,
                         tip_radius=0.09)
        self.actors["flow"] = self.plotter.add_mesh(
            arrow, color=COL_FLOW, name="flow", reset_camera=False)
        self.set_visible("flow", self.chk["flow"].isChecked())

    def show_rotor_axis(self, centre, direction: tuple[float, float, float],
                        length: float, rpm: float = 0.0,
                        label: str | None = None, label_at_tail: bool = False):
        """Стрілка осі обертання вибраного гвинта, з підписом напрямку.

        Без стрілки єдиний спосіб перевірити вісь — текстовий напис і те,
        зійшлася зона чи ні. Стрілка ж одразу видно поряд із самими
        лопатями: якщо вона стирчить убік від площини, у якій вони явно
        лежать, вісь обрано не ту.

        Напрямок обертання задає ЗНАК обертів (від'ємні — у другий бік),
        а не вісь: вісь у RotatingZone завжди додатна. Підпис написано так,
        щоб не залежати від того, з якого боку людина дивиться на екран:
        «з вістря» — це дивлячись від кінчика стрілки на її основу.
        """
        self._drop("rotor_axis")
        self._drop("rotor_spin_label")
        self._drop("rotor_spin")
        if centre is None:
            self.plotter.render()
            return
        length = max(length, 1e-4)
        start = tuple(c - d * length / 2 for c, d in zip(centre, direction))
        tip = tuple(c + d * length / 2 for c, d in zip(centre, direction))
        arrow = pv.Arrow(start=start, direction=direction, scale=length,
                         tip_length=0.3, shaft_radius=0.05, tip_radius=0.12)
        self.actors["rotor_axis"] = self.plotter.add_mesh(
            arrow, color=COL_ROTOR_AXIS, name="rotor_axis",
            reset_camera=False)
        # ЗІГНУТА СТРІЛКА, А НЕ ЛИШЕ ПІДПИС.
        #
        # «CCW з вістря» технічно правильне, але словами цього не
        # розвʼязати: те саме обертання з одного боку читається як за
        # годинниковою, з протилежного — як проти. Людина дивиться на
        # модель з того боку, з якого їй зручно, а не з того, який мав
        # на увазі підпис.
        #
        # Дуга з наконечником такої двозначності не має: вона показує рух
        # у просторі, і з будь-якого боку показує той самий рух.
        self._drop("rotor_spin")
        if abs(rpm) > 1e-9:
            self.actors["rotor_spin"] = self._spin_arrow(
                centre, direction, length, rpm)
        # У ВЮВПОРТІ — ЛИШЕ ЧИСЛО, І ЛАТИНКОЮ.
        #
        # У VTK немає кириличного шрифта: українські літери виходять
        # квадратиками. Переносу рядка в підписі точки теж немає — довгий
        # текст просто обрізається, і «+6000 об/хв, проти годинникової...»
        # перетворилося на «+6000 /».
        #
        # Тому пояснення живе в ПАНЕЛІ, де його видно повністю й рідною
        # мовою, а тут лишається те, що в підпис влазить і читається
        # однаково будь-де: число обертів. Сам напрямок показує дуга.
        self._spin_text = label if label is not None else f"{rpm:+.0f} rpm"
        self.actors["rotor_spin_label"] = self.plotter.add_point_labels(
            [start if label_at_tail else tip], [self._spin_text],
            text_color=COL_ROTOR_AXIS,
            font_size=13, shape=None, always_visible=True,
            name="rotor_spin_label")
        self.plotter.render()

    def _spin_arrow(self, centre, direction, length: float, rpm: float):
        """Дуга навколо осі з наконечником на кінці — куди крутиться.

        Знак обертів задає напрямок обходу за ПРАВИЛОМ ПРАВОЇ РУКИ
        навколо осі: саме так його розуміє MRF в OpenFOAM, і саме так
        його має показувати картинка, інакше вона брехатиме про те, що
        порахується.
        """
        d = np.asarray(direction, float)
        n = float(np.linalg.norm(d))
        if n < 1e-12:
            return None
        d = d / n
        # Будь-який вектор, не паралельний осі, дає нам перший орт.
        seed = np.array([0.0, 0.0, 1.0]) if abs(d[2]) < 0.9 \
            else np.array([1.0, 0.0, 0.0])
        u = np.cross(seed, d)
        u /= max(float(np.linalg.norm(u)), 1e-12)
        # v підбираємо так, щоб u x v = d: тоді зростання кута — це
        # додатний оберт за правою рукою, тобто те саме, що й omega > 0.
        v = np.cross(d, u)
        r = max(length * 0.42, 1e-5)
        turn = 1.0 if rpm >= 0 else -1.0
        ang = np.linspace(0.0, turn * np.deg2rad(260.0), 60)
        c = np.asarray(centre, float)
        pts = c + r * (np.outer(np.cos(ang), u) + np.outer(np.sin(ang), v))
        tube = pv.Spline(pts, 120).tube(radius=max(r * 0.055, 1e-6))
        # Наконечник — за дотичною в кінцевій точці.
        t = turn * (-np.sin(ang[-1]) * u + np.cos(ang[-1]) * v)
        tip_len = max(r * 0.30, 1e-5)
        head = pv.Cone(center=pts[-1] + t * tip_len * 0.5, direction=t,
                       height=tip_len, radius=max(r * 0.13, 1e-6),
                       resolution=24)
        return self.plotter.add_mesh(
            tube.merge(head, merge_points=False), color=COL_ROTOR_AXIS,
            name="rotor_spin", reset_camera=False)

    def refresh_scene(self, cfg: fc.CaseCfg):
        self.show_domain(cfg)
        self.show_boxes(cfg)
        self.show_point(cfg)
        self.show_flow(cfg)
        self.plotter.render()


# ==========================================================================
# Робочий потік
# ==========================================================================

class GeometryLoader(QtCore.QObject):
    """Читання файлу, визначення одиниць і діагностика — поза потоком вікна."""

    ready = QtCore.Signal(object, float, str, object)   # mesh, factor, why, diag
    failed = QtCore.Signal(str)
    # Що саме зараз робиться. На 4.8 млн трикутників файл читається 5 с, а
    # перевірка — ще 20; без цього рядка вікно поступу всі 25 с казало
    # «Читаю…», і це скидалося на зависання на читанні.
    step = QtCore.Signal(str)

    def __init__(self, path: str):
        super().__init__()
        self.path = path

    @QtCore.Slot()
    def run(self):
        try:
            self.step.emit(tr("Reading file…"))
            mesh = geo.load_surface(self.path)
            if mesh.n_cells == 0:
                self.failed.emit(tr("The file has no triangles."))
                return
            self.step.emit(
                tr("Checking the geometry ({n_cells} triangles): holes, "
                   "flipped faces, maximum cross-section…", n_cells=phys.fmt_cells(mesh.n_cells)))
            factor, why = geo.guess_scale(mesh)
            diag = geo.diagnose(geo.apply_scale(mesh, factor))
            self.ready.emit(mesh, factor, why, diag)
        except Exception as exc:
            self.failed.emit(f"{type(exc).__name__}: {exc}")


class BusyWorker(QtCore.QObject):
    """Будь-яка довга обчислювальна робота поза потоком вікна.

    Віддає або результат, або виняток — рішення, що з ними робити, лишається
    за тим, хто чекає (`MainWindow._off_thread`).
    """

    done = QtCore.Signal(object, object)    # результат, виняток

    def __init__(self, job):
        super().__init__()
        self.job = job

    @QtCore.Slot()
    def run(self):
        try:
            result = self.job()
        except Exception as exc:            # noqa: BLE001
            self.done.emit(None, exc)
            return
        self.done.emit(result, None)


class _BusyReceiver(QtCore.QObject):
    """Приймач результату в потоці вікна.

    Окремий QObject тут не для краси. Сигнал із чужого потоку, підʼєднаний
    до простої функції чи лямбди, Qt виконує в потоці ВІДПРАВНИКА — тобто
    в робочому, а не тут. Щоб відповідь прийшла сюди, отримувач мусить
    жити в потоці вікна.
    """

    def __init__(self, loop: QtCore.QEventLoop):
        super().__init__()
        self.loop = loop
        self.result = None
        self.exc = None

    @QtCore.Slot(object, object)
    def take(self, result, exc):
        self.result, self.exc = result, exc
        self.loop.quit()


class _Receiver(QtCore.QObject):
    """Calls `fn(result, exc)` in the thread of this object (the window
    thread): the reason is the same as for _BusyReceiver."""

    def __init__(self, fn, parent=None):
        super().__init__(parent)
        self.fn = fn

    @QtCore.Slot(object, object)
    def take(self, result, exc):
        self.fn(result, exc)


class RepairWorker(QtCore.QObject):
    """Ремонт геометрії поза потоком вікна.

    На збірці з 775 тис. трикутників ланцюжок ремонту йде 15 секунд, а
    обгортання — 14. Стільком часу замерзле вікно без жодної ознаки життя
    виглядає як зависання: люди в такі хвилини вбивають програму. Читання
    файлу вже давно в потоці; ремонт лишався останнім довгим кроком у
    головному.
    """

    ready = QtCore.Signal(object)      # список нових сіток
    failed = QtCore.Signal(str)
    step = QtCore.Signal(str)

    def __init__(self, job, meshes):
        super().__init__()
        self.job = job
        self.meshes = meshes

    @QtCore.Slot()
    def run(self):
        try:
            self.ready.emit(self.job(self.meshes, self.step.emit))
        except Exception as exc:
            self.failed.emit(f"{exc}")


class RunWorker(QtCore.QObject):
    line = QtCore.Signal(str)
    step = QtCore.Signal(str, int, int)
    done = QtCore.Signal(bool, str)
    # (name, value): "detached" (the run lives on the server), "fetch"
    # (results are copied back). The window does not match log texts.
    event = QtCore.Signal(str, str)

    def __init__(self, backend, case_dir, steps, native_dir=None,
                 sync_back=False, name="", stop_on_bad_mesh=True,
                 attach_to: str = "", mesh_key: "str | None" = None):
        super().__init__()
        self.backend = backend
        self.case_dir = case_dir
        self.steps = steps
        self.native_dir = native_dir
        self.sync_back = sync_back
        self.name = name
        self.stop_on_bad_mesh = stop_on_bad_mesh
        # Тека сесії на сервері, до якої ПІДКЛЮЧАЄМОСЬ замість того, щоб
        # пускати нову. Порожньо — звичайний запуск.
        self.attach_to = attach_to
        # Ключ сітки прогону (той, під яким вона лежить у сховищі бекенда).
        # Сітку на цей компʼютер НЕ забираємо після кожного прогону: це сотні
        # МБ. Її забирає «Зберегти проєкт із сіткою…», коли людина просить.
        if mesh_key is None:
            mesh_key = (be.mesh_key(case_dir) if case_dir
                        and be.mesh_cache_supported(backend) else "")
        self.mesh_key = mesh_key
        # The download of the results after the run (T44): None (no
        # download), True (done), False (failed).
        self.fetched: "bool | None" = None
        # The settings of THIS run (a copy made at the start). None: not
        # known here (attach) -> MainWindow._run_cfg reads the case manifest.
        self.cfg: "fc.CaseCfg | None" = None
        self.runner: be.Runner | None = None
        # Звідки читати postProcessing під час прогону. Коли кейс у рідній ФС
        # WSL, Windows бачить його через \\wsl.localhost.
        if native_dir and backend.kind == "wsl":
            self.poll_dir = be.wsl_to_unc(backend.distro, native_dir)
        else:
            self.poll_dir = case_dir

    @QtCore.Slot()
    def run(self):
        try:
            self.runner = be.Runner(
                self.backend, self.case_dir,
                on_line=self.line.emit,
                # Блок журналу — одним сигналом, а не сигналом на рядок:
                # при підключенні це пів мільйона рядків. Журнал у вікні
                # однаково тримає лише останні LogView.MAX_BLOCKS.
                on_block=lambda text: self.line.emit(
                    tail_lines(text, LogView.MAX_BLOCKS)),
                on_step=lambda n, i, t: self.step.emit(n, i, t),
                on_done=self._finish,
                on_event=self.event.emit,
                native_dir=self.native_dir,
                stop_on_bad_mesh=self.stop_on_bad_mesh,
            )
            if self.attach_to:
                # Підключення до вже наявної сесії: нічого не пускаємо,
                # лише читаємо. Кроки передаємо, щоб смужка поступу знала
                # їхній порядок.
                self.runner.attach(self.attach_to, steps=self.steps or None)
            elif self.backend.kind == "ssh":
                # НА СЕРВЕРІ — ЗАВЖДИ ВІДЧЕПЛЕНО. Прогін там триває
                # годинами, а вікно на ноуті закривають, вимикають і
                # присипляють; привʼязувати одне до одного означало б
                # губити рахунок через закриту кришку.
                self.runner.run_detached(self.steps, name=self.name,
                                         remote_dir=self.native_dir or "")
            else:
                self.runner.run(self.steps)
        except Exception:
            self.done.emit(False, traceback.format_exc(limit=3))

    def _finish(self, ok, msg):
        # The window let the run go (leave() at close): the run continues on
        # the server. Do not copy back. sync_from_ssh first deletes the local
        # results and then downloads the whole case; at close this ran in
        # this thread after closeEvent stopped waiting, and the end of the
        # process destroyed the still running QThread (qFatal, c0000409 in
        # Qt6Core.dll, T26).
        if self.runner is not None and getattr(self.runner, "left", False):
            self.done.emit(ok, msg)
            return
        if self.native_dir and self.sync_back:
            self.line.emit("")
            self.line.emit(tr(FETCH_MARK))
            # The value: how the run ended ("ok" / "fail"), for the
            # notification «Run finished» (T44).
            self.event.emit("fetch", "ok" if ok else "fail")
            self.fetched = False
            try:
                if self.backend.kind == "ssh":
                    be.sync_from_ssh(self.backend, self.native_dir,
                                     self.case_dir)
                else:
                    be.sync_from_wsl(self.backend, self.native_dir,
                                     self.case_dir)
                self.fetched = True
            except Exception as exc:
                self.line.emit(tr("could not copy back: {exc}", exc=exc))
        self.done.emit(ok, msg)

    def stop(self):
        if self.runner:
            self.runner.stop()

    def soft_stop(self) -> tuple[bool, str]:
        if not self.runner:
            return (False, tr("no run"))
        return self.runner.soft_stop()

    def leave(self) -> bool:
        """Відпустити прогін, не вбиваючи. True — прогін лишився жити.

        Потрібне при закритті вікна. «Зупинити» і «закрити програму» — це
        різні наміри, і доки вони вели в той самий `stop`, відчеплений
        прогін гинув від закритої кришки ноута.
        """
        r = self.runner
        if r is not None and r.remote_dir:
            r.detach()
            return True
        self.stop()
        return False


# ==========================================================================
# Головне вікно
# ==========================================================================

class MainWindow(QtWidgets.QMainWindow):
    # Знайдені на сервері прогони. Сигнал, а не прямий виклик: шукає
    # окрема нитка, а чіпати вікно з чужої нитки в Qt не можна.
    sessions_seen = QtCore.Signal(object)
    mesh_fetched = QtCore.Signal(str, str)      # ключ, помилка ("" — вийшло)

    def __init__(self):
        # Налаштування програми (fs_settings, T22): один файл на людину.
        # Старі значення з QSettings і session.json переносимо один раз.
        # До fs_i18n: мова інтерфейсу — теж налаштування.
        fset.bind_qsettings(SETTINGS_ORG, SETTINGS_APP)
        fset.migrate(DATA_DIR)
        be.MESH_KEEP = fset.get("mesh_keep")
        fs_i18n.install()       # the interface language (before any widget)
        super().__init__()
        self.setWindowTitle("OpenFOAM Studio — external aerodynamics")
        self.setWindowIcon(fs_icons.icon(fs_icons.OFS))
        self.resize(1560, 950)

        self.cfg = fc.CaseCfg()
        # Сцена — це список моделей. Зібране з увімкнених тіло лежить у
        # `mesh`; `mesh_display` — його копія з нормалями для екрана.
        self.parts: list[Part] = []
        # ПОЛОЖЕННЯ СТІКІВ. Позиція, в якій рахуватиметься кейс: сцена
        # збирається з деталей, відхилених саме на ці значення. Типово
        # все в нейтралі, тож доки пультом ніхто не рухав, геометрія та
        # сама, що й була до появи керування.
        self.channels: dict[str, float] = {
            k: mid for k, _, _, _, mid, _ in fc.RC_CHANNELS}
        self.mesh_original: pv.PolyData | None = None
        self.mesh: pv.PolyData | None = None
        self.mesh_display: pv.PolyData | None = None
        self._load_add = False
        self.diag: geo.Diagnosis | None = None
        self.hole_edges: pv.PolyData | None = None
        # Готова підсвітка проблем. Тримаємо її, щоб на повороті не
        # виколупувати ребра з нуля, а просто повернути разом із тілом.
        self.layers: dict[str, object] = {}
        # Перелік проблемних місць рахується на першу потребу й живе, доки
        # геометрія не змінилася.
        self._spots: list[dict] = []
        self._spot_at = -1
        self.stl_path = ""
        # Вихідний файл, з якого все почалося. Окремо від stl_path, бо
        # той після відновлення сесії вказує на кеш у session/: інакше
        # кожне відновлення й закриття підмінювало б походження
        # геометрії власним кешем, і за кілька циклів від назви
        # справжнього файлу не лишалося б і сліду.
        self.stl_source = ""
        self.backends: list[be.Backend] = []
        self.thread: QtCore.QThread | None = None
        # Типовий рівень коробки навколо зони гвинта. Рівень,
        # поставлений руками в таблиці, цим не перетирається.
        self.zone_box_level = 4
        # Ядра й памʼять кожного бекенда — раз на бекенд і назавжди.
        #
        # Створюється ТУТ, а не при пошуку бекендів, і це не дрібниця:
        # заповнення списку саме собою сигналить про зміну вибору, тобто
        # питання про ресурси приходить РАНІШЕ, ніж пошук устигає щось
        # зробити. Доки словника не було на цьому місці, будь-яке вікно
        # падало на AttributeError одразу при запуску.
        self._res_cache: dict[str, tuple[int, int, float]] = {}
        # Чи візьме прогін збережену сітку (тоді snappyHexMesh не буде) —
        # для перевірки памʼяті. Кеш хешів усередині.
        self._saved_mesh = memfit.SavedMesh(
            fc.mesh_dicts, be.mesh_key, be.local_mesh_dir, be.MESH_INPUTS,
            lib.project_mesh_source)
        self.worker: RunWorker | None = None
        # The case to open after an attach (start_attach, T37).
        self._attach_open = ""
        self._load_thread: QtCore.QThread | None = None
        self._loader: GeometryLoader | None = None
        self._load_head = ""
        # Одиниці, які людина назвала у вікні перед читанням, — для всієї
        # пачки; і свій множник деталі при повторній галочці.
        self._load_units: str | None = None
        self._batch_units: str | None = None
        self._load_factor: float | None = None
        self._series: AngleSeries | None = None
        # The run queue on the server (T46): while not None, _start_one_run
        # makes a job for the queue instead of starting the run.
        self._queue_ctx: "dict | None" = None
        self._queued_job: "dict | None" = None
        self._queue_win = None
        self.queue_watch = fs_queuewin.QueueWatch(self)
        # Pre-run (T39, fs_prerun): "pre" while the short run goes. The
        # warnings that the person accepted for the pre-run are not asked
        # again for the main run that follows it.
        self._prerun_phase = ""
        self._warn_quiet: set = set()
        self._prerun_dialog = None
        # Глибина `_off_thread`: усередині вже запущеної роботи нову не
        # пускаємо в потік, а рахуємо на місці (див. там).
        self._busy_depth = 0
        self._fix_thread: QtCore.QThread | None = None
        self._fixer: RepairWorker | None = None
        self._fix_progress: QtWidgets.QProgressDialog | None = None
        self._fix_snapshot: list = []
        self._fix_live: list = []
        self._fix_key = ""
        self._fix_mode = ""
        self._fix_before = None
        self._progress: QtWidgets.QProgressDialog | None = None
        # Що зробити, коли геометрія прочитається: відкриття збереженого кейсу
        # й відновлення сесії різні, а чекають на ту саму сітку.
        self._pending_restore = None
        # Решта частин кейсу з роздільними патчами. Читаються по черзі, бо
        # load_stl працює в окремому потоці й другого виклику не приймає,
        # поки не скінчився перший.
        self._restore_queue: list[str] = []
        # ЧЕРГА ПАЧКИ — окрема від `_restore_queue`, і це не дублювання.
        # Та черга відновлює частини вже зібраного кейсу: вони лежать у
        # метрах, і автовизначення одиниць там шкідливе. А файли, вибрані
        # людиною, можуть бути в різних одиницях кожен — фюзеляж у метрах,
        # антена в міліметрах, — тож кожному потрібне своє визначення.
        self._batch_queue: list[str] = []
        self._batch_failed: list[str] = []
        self._batch_total = 0
        self._want_backend = ""
        self.case_dir = ""
        self.results: post.Results | None = None
        self._cop: tuple[float, float, float] | None = None
        # Зріз течії. Позицію площини й межі шкали памʼятаємо ТУТ, а не в
        # сцені: сцена перебудовується при кожній зміні часу, а вибір
        # людини має пережити це. Позиція — окремо для кожної осі, шкала —
        # окремо для кожного поля (у швидкості й тиску вони різні).
        self._scene: fv.SliceScene | None = None
        self._streams: fv.StreamScene | None = None
        self._streams_ctx: dict = {}
        # Звідки ростуть лінії. Як і позиція зрізу, живе тут, а не в сцені:
        # куди потягли відрізок чи кулю, має пережити зміну часу й поля.
        self._seeding = fv.Seeding()
        self._seed_widget: fv.SeedWidget | None = None
        # Лінії рахуються секунди, а число в полі міняють стрілками по
        # одиниці: перераховуємо, коли людина на мить зупинилася.
        self._stream_timer = QtCore.QTimer(self)
        self._stream_timer.setSingleShot(True)
        self._stream_timer.setInterval(450)
        self._stream_timer.timeout.connect(self._retrace_streams)
        self._slice_case = ""              # для якого кейса памʼятаємо вибір
        self._view_box = None              # де тіло — для косого вигляду
        self._model_box = None             # що вписує «До моделі» в результатах
        self._face_prev = None             # галочки до «Виду на зріз»
        self._slice_pos: dict[int, float] = {}
        self._slice_limits: dict[str, tuple[float, float]] = {}
        self._body_actor = None
        # Шари поверх тіла (зворотна течія, слід, лінії на тілі, обдув) і
        # поля стінки. Поля стінки — розбір тиску й тертя по кожній грані —
        # коштують секунди на великому тілі, тож рахуються раз на момент часу.
        self._overlay: fv.Overlay | None = None
        self._wash: fv.WashScene | None = None
        self._wall: tuple = ()             # (ключ, тіло, планер, гвинти)
        # ПРОЄКТ. `_project_path` — куди зберегли востаннє (туди ж іде
        # автозбереження перед прогоном), `_project_mark` — відбиток сцени
        # на той момент. Питати при виході «чи зберегти» можна лише
        # порівнянням: ловити кожну зміну в дюжині панелей — це десятки
        # місць, де легко забути одне й тихо втратити роботу.
        self._project_path = ""
        self._project_mark = ""
        # СІТКА З ПРОЄКТУ (fs_meshlock): ключ, комірки, чи вона ще діє. Поки
        # діє, поля, що змінюють сітку, замкнені, а прогін бере цю сітку.
        self._pmesh = meshlock.ProjectMesh()
        self._mlock = meshlock.MeshLock(lambda w: self._ask_unlock_mesh(w),
                                        lambda: self._release_project_mesh(),
                                        self)
        # Сітки, які точно лежать у сховищі бекенда: {(бекенд, ключ)}. З них
        # «Зберегти проєкт із сіткою» знає, що сітку є звідки забрати.
        self._remote_meshes: set = set()
        self._mesh_menu_cache: tuple = ()
        # Діалог при закритті. Тести відкривають і закривають вікно самі,
        # і модальне питання, на яке нема кому відповісти, просто вішає їх.
        self.ask_save_on_close = True

        self._build_ui()
        self._wire()
        # Новачок за замовчуванням: людина, яка відкрила програму вперше,
        # має побачити пояснення без пошуку по меню. Вибір запамʼятовується.
        store = self._store()
        self.act_rookie.setChecked(bool(fset.get("rookie")))
        panels.set_rookie(self.act_rookie.isChecked())
        self.act_session.setChecked(bool(fset.get("restore_session")))
        self.act_session.toggled.connect(
            lambda on: fset.set("restore_session", on))
        fset.subscribe(self._setting_changed)
        # A new language in «Файл → Налаштування» changes the interface at
        # once (T28): fs_i18n translates each widget again from its key,
        # and retranslate_ui makes the texts that are composed from parts.
        fs_i18n.on_language_changed(self.language_changed)
        self.destroyed.connect(
            lambda _=None, fn=self._setting_changed: fset.unsubscribe(fn))
        geometry = store.value("geometry")
        if isinstance(geometry, QtCore.QByteArray):
            self.restoreGeometry(geometry)
        state = store.value("splitter")
        if isinstance(state, QtCore.QByteArray):
            self.split.restoreState(state)

        self.sessions_seen.connect(self._say_sessions)
        self.mesh_fetched.connect(self._mesh_fetched)
        # T44: notifications about the runs, and the window connects to its
        # server runs again by itself (start, after a sleep, a new network).
        self.run_guard = fs_recover.RunGuard(self)
        # T49b: the last read of the run folders of the servers, for the
        # «Runs» window; the full reads of the run watch go there too.
        self.runs_snapshot = fs_runs.Snapshot()
        self.run_guard.probed.connect(self._runs_probe)
        self._detecting = False
        self._start_tries = 0
        QtCore.QTimer.singleShot(150, self._detect_on_start)
        self._load_models()
        self.refresh_all()

    def _detect_on_start(self):
        """Пошук машин на старті, а ПІСЛЯ нього — сесія й сервер.

        Пошук блокує вікно (так і лишається, T13c), але показує, яку машину
        питає зараз. Тому спершу чекаємо, доки вікно показалося (до ~2 с):
        інакше на екрані було б порожньо, поки йдуть перевірки.
        """
        if not self.isVisible() and self._start_tries < 20:
            self._start_tries += 1
            QtCore.QTimer.singleShot(100, self._detect_on_start)
            return
        try:
            if fset.get("check_machines_on_start"):
                self.detect_backends()
            else:
                self.statusBar().showMessage(
                    tr("The program does not find the machines at start "
                    "(File → Settings). To find OpenFOAM, click «Search» "
                    "on the «Run» tab."),
                    10000)
        finally:
            # Ті самі кроки й той самий порядок, що й раніше: пошук завжди
            # закінчувався до них, бо блокував цикл подій. Тепер між
            # перевірками цикл подій крутиться (щоб малювати індикатор),
            # тож ці таймери ставимо лише тут, а не в __init__.
            #
            # Сесію відновлюємо після того, як вікно вже показалося: читання
            # STL забирає секунди, і робити це до першого кадру означає
            # показати користувачеві порожній прямокутник.
            QtCore.QTimer.singleShot(100, self._restore_session_if_wanted)
            # А тоді тихо питаємо сервер, чи не лишилося там чогось живого.
            # Пізніше за все інше: це найдовше й найменш потрібне для першого
            # кадру, і без мережі воно просто мовчить.
            QtCore.QTimer.singleShot(1050, self._peek_sessions)
            # T44: after the session restore (the guard waits for it).
            QtCore.QTimer.singleShot(1500, self.run_guard.start)

    def _save_models(self):
        """Список моделей переживає закриття програми незалежно від сесії.

        Сама сесія (з геометрією, розворотами й ремонтом) — окрема річ, і її
        можна вимкнути; перелік того, що ви відкривали, від цього страждати
        не повинен.
        """
        import json
        data = [dict(path=p.path, enabled=bool(p.enabled and p.loaded),
                     auto=float(p.auto), cache=p.cache, factor=float(p.factor))
                for p in self.parts if p.path]
        self._store().setValue("models", json.dumps(data, ensure_ascii=False))

    def _load_models(self):
        """Повертає список моделей історією: шляхи памʼятаємо, файли не читаємо."""
        import json
        raw = self._store().value("models")
        if not isinstance(raw, str) or not raw.strip():
            return
        try:
            data = json.loads(raw)
        except ValueError:
            return
        if not isinstance(data, list):
            return
        for item in data[:MAX_PARTS]:
            if not isinstance(item, dict):
                continue
            path = str(item.get("path") or "")
            if not path or not os.path.isfile(path):
                continue
            if any(self._key(p.path) == self._key(path) for p in self.parts):
                continue
            try:
                auto = float(item.get("auto", 1.0))
                factor = float(item.get("factor", 1.0))
            except (TypeError, ValueError):
                auto, factor = 1.0, 1.0
            # Кеш — лише якщо файл досі на місці: інакше вихідний файл.
            cache = str(item.get("cache") or "")
            if cache and not os.path.isfile(cache):
                cache = ""
            self.parts.append(Part(path=path, mesh=None, auto=auto,
                                   enabled=False, cache=cache,
                                   factor=factor if cache else 1.0))
        self.panel_geo.show_parts(self._part_rows(), self._scene_line())

    def _store(self) -> QtCore.QSettings:
        """Дрібні налаштування вікна. Область читається щоразу: тести
        підміняють її, щоб не чіпати справжні налаштування."""
        return QtCore.QSettings(SETTINGS_ORG, SETTINGS_APP)

    # --- побудова інтерфейсу --------------------------------------------

    def _build_ui(self):
        self.panel_geo = panels.GeometryPanel(self.cfg)
        self.panel_dom = panels.DomainPanel(self.cfg)
        self.panel_mesh = panels.MeshPanel(self.cfg)
        self.panel_flow = panels.FlowPanel(self.cfg)
        self.panel_solve = panels.SolverPanel(self.cfg)
        self.panel_adv = panels.AdvisorPanel(self.cfg)
        self.panel_run = panels.RunPanel(self.cfg)
        self.panel_run.ed_dir.setText(str(fset.get("cases_dir") or DEFAULT_CASES))
        self.panel_tx = panels.TransmitterPanel()
        # Сітка з проєкту — і на вкладці «Сітка», де її найперше шукають.
        self.lbl_pmesh_tab = QtWidgets.QLabel("")
        self.lbl_pmesh_tab.setWordWrap(True)
        self.lbl_pmesh_tab.setStyleSheet(
            "color:#1f6f8b; font-weight:bold; padding:4px;")
        self.lbl_pmesh_tab.setVisible(False)
        self.panel_mesh.v.insertWidget(0, self.lbl_pmesh_tab)

        self.tabs_left = QtWidgets.QTabWidget()
        for w, name in (
            (self.panel_geo, "Geometry"),
            (self.panel_dom, "Domain"),
            (self.panel_mesh, "Mesh"),
            (self.panel_flow, "Flow"),
            (self.panel_solve, "Solver"),
            (self.panel_tx, "Controls"),
            (self.panel_adv, "Assistant"),
            (self.panel_run, "Run"),
        ):
            scroll = QtWidgets.QScrollArea()
            scroll.setWidgetResizable(True)
            scroll.setWidget(w)
            scroll.setFrameShape(QtWidgets.QFrame.NoFrame)
            # Коліщатко над полем має гортати ПАНЕЛЬ, а не міняти
            # налаштування. Помічники dspin/ispin ставлять захист самі, але
            # випадаючі списки створюються в місці вжитку — їх накриває цей
            # прохід по всьому дереву панелі.
            panels.guard_wheel(w)
            panels.compact_combos(w)
            self.tabs_left.addTab(scroll, name)
        self.tabs_left.setMinimumWidth(430)
        self.tabs_left.setMaximumWidth(560)

        self.viewport = Viewport()
        self.log = LogView()
        self.plots = PlotView()
        self.results_view = self._build_results_tab()
        self.panel_lib = panels.LibraryPanel(str(DEFAULT_CASES))

        self.tabs_right = QtWidgets.QTabWidget()
        self.tabs_right.addTab(self.viewport, "3D")
        self.tabs_right.addTab(self.log, "Log")
        self.tabs_right.addTab(self.plots, "Convergence")
        self.tabs_right.addTab(self.results_view, "Results")
        self.tabs_right.addTab(self.panel_lib, "Saved")

        self.split = QtWidgets.QSplitter(QtCore.Qt.Horizontal)
        self.split.addWidget(self.tabs_left)
        self.split.addWidget(self.tabs_right)
        self.split.setStretchFactor(1, 1)
        self.setCentralWidget(self.split)

        bar = self.menuBar()
        file_menu = bar.addMenu("File")

        act = QtGui.QAction("Open Geometry…", self)
        act.setStatusTip("Open a mesh file: STL, 3MF, OBJ, PLY, OFF, glTF "
                         "or VTK (the formats of the shared package "
                         "meshfix-core)")
        act.setShortcut("Ctrl+O")
        act.triggered.connect(self.open_stl)
        file_menu.addAction(act)

        act = QtGui.QAction("Save Project…", self)
        act.setShortcut("Ctrl+Shift+S")
        act.setStatusTip("One file that holds EVERYTHING: the geometry "
                         "of each part as it is in the scene, roles, "
                         "rotations, axes, hinges and all settings. "
                         "Without the mesh (except a locked mesh from "
                         "the project)")
        act.triggered.connect(lambda: self.save_project())
        file_menu.addAction(act)

        self.act_save_mesh = QtGui.QAction("Save Project with Mesh…", self)
        self.act_save_mesh.setStatusTip(
            "The project together with the finished mesh (compressed). "
            "When opened, it uses this mesh instead of snappyHexMesh, "
            "and the fields that change the mesh are locked")
        self.act_save_mesh.triggered.connect(self.save_project_with_mesh)
        file_menu.addAction(self.act_save_mesh)
        file_menu.setToolTipsVisible(True)
        file_menu.aboutToShow.connect(self._update_mesh_menu)

        act = QtGui.QAction("Open Project…", self)
        act.setStatusTip("Restore the scene as it was saved")
        act.triggered.connect(self.open_project)
        file_menu.addAction(act)

        act = QtGui.QAction("Projects Folder…", self)
        act.setStatusTip("Where projects go by default: autosave before "
                         "a run writes here, and the “Save Project” "
                         "window starts here")
        act.triggered.connect(self.choose_projects_dir)
        file_menu.addAction(act)
        file_menu.addSeparator()

        act = QtGui.QAction("Save Geometry…", self)
        act.setStatusTip("Write what is in the scene now — already in "
                         "meters, rotated and repaired. STL, 3MF, OBJ, "
                         "PLY, OFF, glTF or VTK; 3MF, OBJ, ASCII STL, glTF "
                         "and VTK keep each model as a separate object")
        act.triggered.connect(self.save_geometry)
        file_menu.addAction(act)
        file_menu.addSeparator()

        act = QtGui.QAction("Save Settings…", self)
        act.setShortcut("Ctrl+S")
        act.setStatusTip("All settings in a separate file — to move to "
                         "another machine or to keep as a template")
        act.triggered.connect(self.save_settings)
        file_menu.addAction(act)

        act = QtGui.QAction("Open Settings…", self)
        act.setShortcut("Ctrl+Shift+O")
        act.setStatusTip("Accepts both a settings file and the "
                         "studio.json of any case")
        act.triggered.connect(self.load_settings)
        file_menu.addAction(act)

        act = QtGui.QAction("Reset Settings", self)
        act.setStatusTip("Return all settings to the defaults. The "
                         "geometry in the scene stays in place")
        act.triggered.connect(self.reset_settings)
        file_menu.addAction(act)
        file_menu.addSeparator()

        self.act_session = QtGui.QAction("Restore the last session", self)
        self.act_session.setCheckable(True)
        self.act_session.setStatusTip(
            "At start, restore the settings, the geometry and the case "
            "that were open when you closed the program")
        file_menu.addAction(self.act_session)

        act = QtGui.QAction("Restore Now", self)
        act.setShortcut("F4")
        act.triggered.connect(self.restore_session)
        file_menu.addAction(act)
        file_menu.addSeparator()

        act = QtGui.QAction("Exit", self)
        act.setShortcut("Ctrl+Q")
        act.triggered.connect(self.close)
        self.act_settings = QtGui.QAction("Settings…", self)
        self.act_settings.setShortcut("Ctrl+,")
        self.act_settings.setMenuRole(QtGui.QAction.PreferencesRole)
        self.act_settings.setStatusTip(
            "Program settings: language, folders, machines, 3D, mesh")
        self.act_settings.triggered.connect(self.open_settings)
        file_menu.addAction(self.act_settings)
        self.act_server_wizard = QtGui.QAction("Set up a server…", self)
        self.act_server_wizard.setStatusTip(
            "Connect a calculation server: Tailscale, SSH key, OpenFOAM check")
        self.act_server_wizard.triggered.connect(self.open_server_wizard)
        file_menu.addAction(self.act_server_wizard)
        file_menu.addSeparator()
        file_menu.addAction(act)

        view_menu = bar.addMenu("View")

        self.act_to_model = QtGui.QAction("Fit to model", self)
        self.act_to_model.setShortcut("Ctrl+0")
        self.act_to_model.setStatusTip(
            "Camera fits the model in all viewports: both in 3D and in "
            "the results. The side you look from stays the same")
        self.act_to_model.triggered.connect(self.to_model)
        view_menu.addAction(self.act_to_model)

        queue_menu = bar.addMenu("Runs")
        act = QtGui.QAction("Runs…", self)
        act.setStatusTip("The plan of the run queue and the runs on the "
                         "servers: states, order, start time, connect, "
                         "results")
        act.triggered.connect(self.open_queue)
        queue_menu.addAction(act)
        act = QtGui.QAction("Add the Case to the Queue", self)
        act.setStatusTip("The server starts it when the machine is free; "
                         "the laptop can sleep")
        act.triggered.connect(lambda: self.queue_add(series=False))
        queue_menu.addAction(act)
        act = QtGui.QAction("Add the Angle Series to the Queue", self)
        act.setStatusTip("One job per angle of the «Angles» field")
        act.triggered.connect(lambda: self.queue_add(series=True))
        queue_menu.addAction(act)

        help_menu = bar.addMenu("Help")
        self._help_menu = help_menu

        self.act_rookie = QtGui.QAction("Beginner mode", self)
        self.act_rookie.setCheckable(True)
        self.act_rookie.setShortcut("F2")
        self.act_rookie.setStatusTip(
            "An explanation for each setting: what it is, what it "
            "affects and what happens if you overdo it")
        self.act_rookie.toggled.connect(self.set_rookie)
        help_menu.addAction(self.act_rookie)
        help_menu.addSeparator()

        act = QtGui.QAction("Guide: Meshing and Solving", self)
        act.setShortcut("F1")
        act.triggered.connect(self.open_manual)
        help_menu.addAction(act)

        act = QtGui.QAction("Install and server guide", self)
        act.setStatusTip("Install the program, connect a server through "
                         "Tailscale, and what to do when the connection hangs")
        act.triggered.connect(self.open_install_guide)
        help_menu.addAction(act)

        act = QtGui.QAction("Glossary of Terms and Formulas", self)
        act.setShortcut("F5")
        act.setStatusTip("What Cd, Aref, y+, skewness… mean and how "
                         "forces come from the coefficients — in the "
                         "browser")
        act.triggered.connect(self.open_glossary)
        help_menu.addAction(act)

        act = QtGui.QAction("Saved Simulations", self)
        act.setShortcut("F3")
        act.setStatusTip("List of cases in the folder: settings, "
                         "results, comparison")
        act.triggered.connect(self.show_library)
        help_menu.addAction(act)

        # Свій рядок стану: тексти в ньому не лягають один на один (T13b,
        # див. fs_statusbar). Мітки скорочують текст трикрапкою.
        self.setStatusBar(sbar.StatusBar(self))
        self.status = sbar.StatusText("—")
        self.status.setStyleSheet("font-family: Consolas, monospace;")
        self.statusBar().addWidget(self.status, 1)
        self.lbl_warn = sbar.StatusText("")
        self.statusBar().addPermanentWidget(self.lbl_warn)
        # Сітка з проєкту: зафіксована чи вже ні. Видно з будь-якої вкладки.
        self.lbl_pmesh = sbar.StatusText("")
        self.lbl_pmesh.setStyleSheet("color:#1f6f8b; font-weight:bold;")
        self.lbl_pmesh.setVisible(False)
        self.statusBar().addPermanentWidget(self.lbl_pmesh)
        # «Ноут можна вимикати»: з'являється, щойно прогін відчепився на
        # сервері. Раніше це був лише рядок посеред журналу, і питання «коли
        # можна вимикати?» доводилося ставити вголос.
        self.lbl_detached = sbar.StatusText(
            "✔ The run lives on the server — you can turn off the laptop")
        self.lbl_detached.setStyleSheet("color:#7ee787; font-weight:600;")
        # Its text and tooltip come from the state (_laptop,
        # RunPanel.LAPTOP_TIPS), never a fixed tooltip of one state (T49).
        self.lbl_detached.setVisible(False)
        self.statusBar().addPermanentWidget(self.lbl_detached)
        # Хто малює «3D»: щоб було видно, коли працює повільний запасний
        # шлях (програмний GPU, CPU), а не «програма гальмує».
        self.lbl_3d = sbar.StatusText("")
        self.statusBar().addPermanentWidget(self.lbl_3d)
        # Що зараз перевіряє пошук машин (T13c). Він блокує вікно на
        # секунди, і без цього рядка вікно виглядало завислим.
        self.lbl_busy = sbar.StatusText("")
        self.lbl_busy.setStyleSheet("color:#f0b429; font-weight:600;")
        self.lbl_busy.setVisible(False)
        # Не стискати: «(4 з 5)» у кінці — головне. Хай скорочуються інші.
        self.lbl_busy.setSizePolicy(QtWidgets.QSizePolicy.Minimum,
                                    QtWidgets.QSizePolicy.Preferred)
        self.statusBar().insertPermanentWidget(0, self.lbl_busy)
        self.viewport.level_changed.connect(self._show_3d_level)
        lv = getattr(self.viewport, "level", None)
        if lv is not None:
            self._show_3d_level(lv.text(), lv.note_text())

    def _show_3d_level(self, text: str, note: str):
        self.lbl_3d.setText(text)
        slow = not text.startswith(("3D: GPU", "3D: VTK"))
        self.lbl_3d.setStyleSheet("color:#f0b429; font-weight:600;" if slow else "")
        self.lbl_3d.setToolTip(
            tr("What draws the “3D” tab. Order: GPU → software GPU → VTK "
               "→ CPU; the choice is remembered for this computer. To "
               "force one: environment variable "
               "FS_3D=gpu|soft|vtk|cpu.{v}", v=f"\n\n{note}" if note else ""))

    def _build_results_tab(self) -> QtWidgets.QWidget:
        w = QtWidgets.QWidget()
        lay = QtWidgets.QVBoxLayout(w)
        lay.setContentsMargins(6, 6, 6, 6)

        bar = QtWidgets.QHBoxLayout()
        self.btn_load_res = QtWidgets.QPushButton("Download Results")
        self.cmb_time = QtWidgets.QComboBox()
        # Items: the field (an array name, or a key of fv.BODY_FIELDS) is
        # the item data; the person sees its translated title.
        self.cmb_field = panels.ValueCombo()
        self.btn_paraview = QtWidgets.QPushButton("Open in ParaView")
        # Через лямбду: clicked передає перший аргумент checked, і він
        # приїхав би замість шляху до кейсу.
        self.btn_paraview.clicked.connect(lambda: self.open_in_paraview())
        bar.addWidget(self.btn_load_res)
        bar.addWidget(self.btn_paraview)
        # The time of the view: the timeline below (T40). The list stays as
        # the model of the current time (hidden): the code and the tests read
        # cmb_time.currentText().
        self.cmb_time.hide()
        bar.addWidget(self.cmb_time)
        bar.addWidget(QtWidgets.QLabel("Field on Body"))
        bar.addWidget(self.cmb_field)
        bar.addStretch(1)
        # Тут, а не лише в меню: саме на цій вкладці стоять Cd, Cl і Cp, і
        # саме тут питають, що вони означають і як із них вийде сила.
        self.btn_glossary = QtWidgets.QPushButton("Glossary")
        self.btn_glossary.setToolTip(
            "Open the program glossary with the force formulas in the "
            "browser: Cd, Cl, CdA, Aref, Cp, y+… (F5)")
        self.btn_glossary.clicked.connect(lambda: self.open_glossary())
        bar.addWidget(self.btn_glossary)
        lay.addLayout(bar)

        # Timeline: all times of the case, a mini player (T40). It is also
        # the animation: play, scrub and step show fast frames (only the
        # values change), loaded in the background.
        self.time_bar = fs_timebar.TimeBar()
        lay.addWidget(self.time_bar)
        # After a download from the server: «only the last time» or «from …
        # to …» in one line (T40). Hidden until the server has more times.
        self.fetch_bar = fs_timesdlg.FetchBar()
        self.fetch_bar.done.connect(self._times_came)
        self.fetch_bar.more_requested.connect(
            lambda: self.times_from_server(self.fetch_bar.backend,
                                           self.fetch_bar.listing,
                                           self.fetch_bar.remote))
        lay.addWidget(self.fetch_bar)
        self._run_folders: dict = {}      # case → run folder on the server
        self._offer_job = (None, None, None)
        self._source = "full"           # full fields or run surfaces
        self._frames_cache = ftl.FrameCache()
        self._frames_case = ""
        self._frame_loader = fs_player.FrameLoader(self)
        self._frame_loader.ready.connect(self._frames_ready)
        self._frame_loader.loaded.connect(self._frame_loaded)
        self._frame_loader.failed.connect(self._frames_failed)
        self._frames_plan = None          # (body key, slice key, options)
        self._frames_final = False        # common limits of all frames set
        self._frames_ld = None            # fs_anim.Loader of the frames
        self._frames_geom = None
        self._anim_built = False          # actors of the frames are built
        self._tl_times: list = []         # times of the timeline = cmb_time rows

        # Проміжний результат прогону, що йде (fs_live). Видно лише під час
        # прогону; малює той самий код, що й остаточні результати.
        self.live_bar = fs_livebar.LiveBar()
        self.live_bar.show_live.connect(self._show_live)
        self.live_bar.show_final.connect(self.load_results)
        lay.addWidget(self.live_bar)

        # Друга смуга — зріз: площина, яку рухають, поле, шкала. Галочка
        # «Зріз» лишається під старим імʼям: так її знає решта коду.
        self.slice_bar = slicebar.SliceBar()
        self.chk_slice = self.slice_bar.chk_enable
        lay.addWidget(self.slice_bar)

        # Frames of the player (fs_anim): built from the view, in the
        # background (self._frame_loader).
        self._anim = None                 # fa.Frames, коли кадри готові
        self._anim_i = 0
        self._anim_keys = ("", "")
        self._anim_actors: dict = {}
        self._anim_meshes: dict = {}
        self._anim_timer = QtCore.QTimer(self)
        self._anim_timer.timeout.connect(self._anim_tick)

        # Попередження просто над видом (T29): лінії потоку лише в одній
        # площині. Рядок під видом легко не помітити, а картина інакше
        # схожа на зламану.
        self.lbl_view_note = QtWidgets.QLabel()
        self.lbl_view_note.setWordWrap(True)
        self.lbl_view_note.setStyleSheet(fs_livebar.LIVE_STYLE)
        self.lbl_view_note.hide()
        lay.addWidget(self.lbl_view_note)

        self.res_plotter = self._make_res_plotter(w)
        self.res_plotter.set_background("#22262b", top="#3a4149")
        self.res_plotter.add_axes(line_width=2)
        lay.addWidget(self.res_plotter.interactor, 1)

        self.lbl_coeffs = QtWidgets.QLabel("No results yet.")
        self.lbl_coeffs.setStyleSheet("font-family: Consolas, monospace;")
        self.lbl_coeffs.setWordWrap(True)
        lay.addWidget(self.lbl_coeffs)
        return w

    def _make_res_plotter(self, parent):
        """Хто малює «Результати» й анімацію: той самий рушій, що й «3D»
        (foamview на GPU, на програмному адаптері чи на CPU), а як ні — VTK.
        Решта коду вкладки однакова: fs_view3d.Plotter розуміє ті самі
        виклики pyvista. FS_RESULTS=vtk — VTK лише тут (порівняти, обійти)."""
        self.res_backend = "vtk"
        vp = self.viewport
        if (os.environ.get("FS_RESULTS", "").strip().lower() != "vtk"
                and getattr(vp, "backend", "") == "foamview" and vp.level is not None):
            try:
                import fs_view3d
                # Шрифт той самий, що й у кадрах звіту (DejaVu Sans з
                # matplotlib): у ньому є «∥», «∞» і кирилиця на всіх ОС.
                pl = fs_view3d.Plotter(parent, vp.level, font=fv.FONT)
                # Початковий бік — як у pyvista: ізометрія з (1, 1, 1).
                pl.view_isometric()
                self.res_backend = "foamview"
                return pl
            except Exception:                             # noqa: BLE001
                pass
        return QtInteractor(parent)

    def _wire(self):
        self.viewport.btn_spot.clicked.connect(self.next_problem)
        self.viewport.to_model_requested.connect(self.to_model)
        self.panel_geo.load_requested.connect(self.open_stl)
        self.panel_geo.add_requested.connect(self.add_stl)
        self.panel_geo.part_toggled.connect(self.part_toggled)
        self.panel_geo.part_removed.connect(
            lambda i: self._geo_guard(i, tr("remove part")) and self.part_removed(i))
        self.panel_geo.part_role.connect(self.part_role_set)
        self.panel_geo.part_engine.connect(self.part_engine_set)
        self.panel_geo.part_flip.connect(self.part_flip)
        self.panel_geo.part_disk.connect(self.part_disk_set)
        self.panel_geo.disk_requested.connect(self.make_disk)
        self.panel_geo.part_moved.connect(self.part_move)
        self.panel_geo.part_role_query.connect(self.show_part_role)
        self.panel_geo.propeller_requested.connect(self.make_propeller)
        self.panel_geo.part_hinge.connect(self.part_hinge_set)
        self.panel_geo.hinge_guess.connect(self.guess_hinge)
        self.panel_tx.channels_changed.connect(self.set_channels)
        self.panel_geo.part_solo.connect(
            lambda i: self._geo_guard(i, tr("keep only this part"), solo=True)
            and self.part_solo(i))
        self.panel_geo.repair_requested.connect(self.repair)
        self.panel_geo.transform_requested.connect(self.transform)
        for p in (self.panel_geo, self.panel_dom, self.panel_mesh,
                  self.panel_flow, self.panel_solve):
            p.changed.connect(self.refresh_all)
        # Auto y+: a new pick after each change that can affect it. The
        # timer collects a burst of changes (typing a number, a project
        # load) into one pick; fs_yplus.signature says if a pick is needed.
        self._yplus_sig = None
        self._yplus_timer = QtCore.QTimer(self)
        self._yplus_timer.setSingleShot(True)
        self._yplus_timer.setInterval(400)
        self._yplus_timer.timeout.connect(self._auto_yplus)
        self.panel_mesh.model_fix_requested.connect(self._set_turbulence)
        self.panel_mesh.treatment_fix_requested.connect(self._set_treatment)

        self.panel_adv.compute_requested.connect(self.compute_advice)
        self.panel_adv.apply_requested.connect(self.apply_advice)
        self.panel_run.detect_requested.connect(self.detect_backends)
        self.panel_run.btn_server.clicked.connect(self.recheck_server)
        self.panel_run.ed_server.returnPressed.connect(self.recheck_server)
        # Адресу сервера памʼятаємо між запусками: вписувати її щоразу
        # заново було б тим самим, що й не памʼятати каталог кейсу.
        self.panel_run.ed_server.setText(fset.get("ssh_host"))
        # Розподіл між машинами (експериментально): теж памʼятаємо.
        pr = self.panel_run
        st = self._store()
        pr.box_dist.setChecked(bool(fset.get("dist_on")))
        # Машини — окремими записами (JSON). Старий рядок «адреса:процеси»
        # переносимо один раз, щоб нічого не вписувати наново.
        machines = be.machines_from_json(fset.get("dist_machines"))
        if not machines:
            machines = be.machines_from_legacy(str(st.value("dist_hosts", "") or ""))
        pr.set_machines(machines)
        pr.ed_dist_key.setText(fset.get("dist_key") or fset.DEFAULTS["dist_key"])
        pr.ed_dist_user.setText(fset.get("dist_user") or fset.DEFAULTS["dist_user"])
        pr.ed_dist_net.setText(fset.get("dist_net"))
        pr.box_dist.toggled.connect(lambda on: fset.set("dist_on", on))
        pr.dist_machines_changed.connect(
            lambda: fset.set("dist_machines", be.machines_to_json(pr.machines())))
        for key, ed in (("dist_key", pr.ed_dist_key),
                        ("dist_user", pr.ed_dist_user),
                        ("dist_net", pr.ed_dist_net)):
            ed.editingFinished.connect(
                lambda k=key, e=ed: fset.set(k, e.text().strip()))
        pr.ed_dir.editingFinished.connect(self._store_cases_dir)
        pr.btn_dist_probe.clicked.connect(self.probe_dist)
        pr.btn_dist_scan.clicked.connect(self.scan_dist)
        self.panel_run.install_requested.connect(self.install_openfoam)
        self.panel_run.create_requested.connect(lambda: self.create_case(True))
        self.panel_run.run_requested.connect(self.start_run)
        self.panel_run.run_server_requested.connect(self.start_run_on_server)
        self.panel_run.attach_requested.connect(self.attach_to_server)
        self.panel_run.queue_requested.connect(lambda: self.queue_add())
        self.panel_run.stop_requested.connect(self.stop_run)
        self.panel_run.soft_stop_requested.connect(self.soft_stop_run)
        self.panel_run.open_dir_requested.connect(self.open_case_dir)
        self.panel_run.fix_layers_requested.connect(self.fix_layers)
        self.panel_run.cmb_backend.currentIndexChanged.connect(
            lambda _: (self.panel_run.update_backend_label(),
                       self._show_resources(), self.update_status()))
        # Перевірка памʼяті залежить і від розподілу, і від «перебудувати».
        pr.box_dist.toggled.connect(lambda _: self.update_status())
        pr.dist_machines_changed.connect(self.update_status)
        pr.chk_remesh.toggled.connect(lambda _: self.update_status())
        self.panel_flow.btn_auto.clicked.connect(self.auto_reference)
        self.panel_flow.btn_rot_fit.clicked.connect(self.fit_rotor_zone)

        self.btn_load_res.clicked.connect(self.load_results)
        tb = self.time_bar
        tb.seek.connect(self._timeline_seek)
        tb.play_toggled.connect(self._anim_play)
        tb.fps_changed.connect(lambda _: self._anim_play(tb.timeline.playing))
        tb.full_view.connect(lambda: self._anim_stop(redraw=True))
        tb.source_changed.connect(self._timeline_source)
        tb.options_changed.connect(self._frames_options)
        tb.export_requested.connect(self._anim_export)
        tb.reconstruct_requested.connect(
            lambda: self._anim_reconstruct(self._missing_times()))
        tb.server_requested.connect(self.times_from_server)
        self.slice_bar.moved.connect(self._anim_slice_moved)
        self.cmb_time.currentTextChanged.connect(lambda _: self.draw_results())
        self.cmb_field.currentTextChanged.connect(lambda _: self._field_changed())
        sb = self.slice_bar
        sb.toggled.connect(lambda on: self._view_toggled(on, sb.streams_on()))
        sb.streams_toggled.connect(lambda on: self._view_toggled(on, sb.is_on()))
        self._restore_seeding()
        sb.seeding_changed.connect(self._seeding_changed)
        sb.seed_reset.connect(self._seed_reset)
        sb.moved.connect(self._slice_moved)
        sb.axis_changed.connect(self._slice_axis)
        sb.field_changed.connect(self._slice_field)
        sb.field_changed.connect(self._frames_view_changed)
        sb.axis_changed.connect(self._frames_view_changed)
        sb.limits_changed.connect(self._slice_limits_set)
        sb.limits_auto.connect(self._slice_limits_auto)
        sb.limits_fit.connect(self._slice_limits_fit)
        sb.contour_toggled.connect(
            lambda on: self._scene and self._scene.set_contour(on))
        sb.edges_toggled.connect(
            lambda on: self._scene and self._scene.set_edges(on))
        sb.body_toggled.connect(lambda _: (self._apply_body_visibility(),
                                           self.res_plotter.render()))
        sb.widget_toggled.connect(self._slice_widget)
        sb.arrows_toggled.connect(
            lambda on: self._scene and self._scene.set_arrows(on))
        sb.layers_changed.connect(self.draw_results)
        sb.face_on_requested.connect(lambda: self._slice_view(True))
        sb.iso_requested.connect(lambda: self._slice_view(False))
        sb.model_requested.connect(self.to_model)
        sb.save_requested.connect(self._save_frame)

        self.panel_lib.open_requested.connect(self.open_saved)
        self.panel_lib.paraview_requested.connect(self.open_saved_in_paraview)
        self.panel_lib.changed_on_disk.connect(self.on_case_moved)
        self.panel_lib.busy_reason = self._run_busy
        # Каталог кейсів задається на вкладці «Запуск», а бібліотека його
        # читає — інакше вони показували б різні місця.
        self.panel_run.ed_dir.textChanged.connect(
            lambda t: self.panel_lib.set_root(t.strip() or str(DEFAULT_CASES)))
        self.tabs_right.currentChanged.connect(self._right_tab_changed)

        self.timer = QtCore.QTimer(self)
        self.timer.setInterval(1500)
        self.timer.timeout.connect(self.refresh_plots)

    # --- геометрія -------------------------------------------------------

    # --- ПРОЄКТ: УСЕ В ОДНОМУ ФАЙЛІ ----------------------------------------

    def _part_blob(self, part) -> bytes:
        """STL однієї деталі в байтах — так, як вона зараз у сцені.

        Пишемо саме `mesh`, а не позу з пульта: поза це стан перегляду, а
        зберігаємо ми НЕЙТРАЛЬ. Відкривши проєкт, людина знову поставить
        стіки як схоче; якби ми записали позу, вона стала б новою
        нейтраллю й вернути її назад було б нічим.
        """
        with tempfile.TemporaryDirectory(prefix="foamproj_") as d:
            f = Path(d) / "part.stl"
            geo.write_stl(part.mesh, str(f), binary=True)
            return f.read_bytes()

    def _part_row(self, p) -> dict:
        """Деталь для проєкту чи сесії: усе, що задала людина, і геометрія."""
        h = p.hinge
        return {
            "name": p.name,
            "path": p.path,
            "factor": float(p.factor),
            "auto": float(p.auto),
            "why": p.why,
            "enabled": bool(p.enabled),
            "role": p.role,
            "rpm": float(p.rpm),
            "rotor_axis": p.rotor_axis,
            "engine": p.engine,
            "jet": (None if p.jet is None else _jet_to_row(p.jet)),
            "disk": (None if p.disk is None else _disk_to_row(p.disk)),
            # Список, а не кортеж: JSON кортежів не знає, і назад він
            # усе одно прийде списком. Хай це буде видно тут.
            "refine": (None if p.refine is None else list(p.refine)),
            "hinge": (None if h is None else {
                "pivot": list(h.pivot), "axis": list(h.axis),
                "offset": list(h.offset), "channel": h.channel,
                "gain": float(h.gain), "trim": float(h.trim),
                "limit": float(h.limit), "reverse": bool(h.reverse)}),
            "stl": self._part_blob(p),
        }

    def projects_dir(self) -> Path:
        """Тека, куди проєкти лягають за замовчуванням.

        Окрема від теки кейсів: кейс — це те, що рахує OpenFOAM, а проєкт —
        те, з чого людина його зібрала. Тримати їх разом означає щоразу
        вибирати потрібне з-поміж десятків каталогів кейсів.
        """
        raw = str(fset.get("projects_dir") or "").strip()
        d = Path(raw) if raw else DEFAULT_CASES.parent / "projects"
        try:
            d.mkdir(parents=True, exist_ok=True)
        except OSError:
            # Тека може бути на знятій флешці чи лише для читання. Тоді
            # нехай вікно збереження відкриється там, де точно можна.
            return DEFAULT_CASES
        return d

    def choose_projects_dir(self):
        path = QtWidgets.QFileDialog.getExistingDirectory(
            self, tr("Projects Folder"), str(self.projects_dir()))
        if not path:
            return
        fset.set("projects_dir", path)
        self.log.push(tr("Projects will be saved in {path}", path=path))
        self.statusBar().showMessage(tr("Projects folder: {path}", path=path), 8000)

    # --- налаштування програми (Файл → Налаштування, T22) ----------------

    def open_settings(self):
        """«Файл → Налаштування…» (Ctrl+,)."""
        host_before = fset.get("ssh_host")
        dlg = fs_settingsdlg.SettingsDialog(
            self, default_cases=str(DEFAULT_CASES),
            default_projects=str(DEFAULT_CASES.parent / "projects"),
            backends=lambda: [f"{'✔' if b.ready else '✖'} {b.label}"
                              for b in self.backends],
            level_text=self._level_text)
        dlg.exec()
        if fset.get("ssh_host") != host_before:
            QtCore.QTimer.singleShot(0, self.recheck_server)

    def open_server_wizard(self) -> str:
        """«File → Set up a server…» (T34): Tailscale, SSH key, check."""
        host_before = fset.get("ssh_host")
        alias = fs_serverwiz.run_wizard(self)
        if fset.get("ssh_host") != host_before:
            QtCore.QTimer.singleShot(0, self.recheck_server)
        return alias

    def offer_server_wizard(self) -> None:
        """First start of an installed copy: offer the server wizard once.

        Not modal: the window stays usable, and the question does not block
        an automatic start (tests, the installer).
        """
        fset.set("server_wizard_offered", True)
        box = QtWidgets.QMessageBox(self)
        box.setObjectName("ServerWizardOffer")
        box.setIcon(QtWidgets.QMessageBox.Question)
        box.setWindowTitle("OpenFOAM Studio")
        box.setText(tr("Do you want to set up a calculation server now?"))
        box.setInformativeText(tr(
            "A Linux server through Tailscale and SSH. You can also do it "
            "later: File → Set up a server…"))
        box.setStandardButtons(QtWidgets.QMessageBox.Yes
                               | QtWidgets.QMessageBox.No)
        box.setWindowModality(QtCore.Qt.NonModal)
        box.setAttribute(QtCore.Qt.WA_DeleteOnClose)
        box.buttonClicked.connect(
            lambda b: box.standardButton(b) == QtWidgets.QMessageBox.Yes
            and QtCore.QTimer.singleShot(0, self.open_server_wizard))
        self._server_offer = box
        box.show()

    def apply_language(self, lang: str) -> None:
        """The language setting changed: switch the interface at once.

        FS_LANG of the environment wins over the setting: then only a note
        (fs_restart.ask_restart tells what to do).
        """
        if fs_i18n.env_language():
            if fs_i18n.env_language() != lang:
                fs_restart.ask_restart(self)
            return
        fs_i18n.set_language(lang)

    def language_changed(self, lang: str = "") -> None:
        """After a live change of the language: make again the texts that
        the window composes from parts and values (fs_i18n has already
        translated each widget text that has a key)."""
        try:
            self.refresh_all()
        except Exception:                                  # noqa: BLE001
            traceback.print_exc()
        # Texts in the 3D views (scale titles, captions, labels) are drawn
        # from the scene: draw it again.
        try:
            if self.results is not None:
                self.draw_results()
            self.viewport.plotter.render()
        except Exception:                                  # noqa: BLE001
            traceback.print_exc()

    def _level_text(self) -> str:
        lv = getattr(self.viewport, "level", None)
        if lv is None:
            return "VTK"
        return lv.text() + (f" ({lv.note_text()})" if lv.note else "")

    def _store_cases_dir(self):
        text = self.panel_run.ed_dir.text().strip()
        fset.set("cases_dir", "" if text == str(DEFAULT_CASES) else text)

    def _setting_changed(self, key: str, value):
        """Зміна налаштування (діалог чи інше вікно) → віджети цього вікна."""
        try:
            pr = self.panel_run
            if key == "rookie":
                self.act_rookie.setChecked(bool(value))
            elif key == "restore_session":
                self.act_session.setChecked(bool(value))
            elif key == "cases_dir":
                text = value or str(DEFAULT_CASES)
                if pr.ed_dir.text().strip() != text:
                    pr.ed_dir.setText(text)
            elif key == "ssh_host":
                if pr.ed_server.text().strip() != value:
                    pr.ed_server.setText(value)
            elif key in ("dist_user", "dist_key", "dist_net"):
                ed = getattr(pr, "ed_" + key)
                if ed.text().strip() != value:
                    ed.setText(value)
            elif key == "mesh_keep":
                be.MESH_KEEP = int(value)
            elif key == "ui_language":
                self.apply_language(value)
            elif key == "view3d_backend":
                self.statusBar().showMessage(
                    tr("The change takes effect after you restart the program."), 8000)
        except RuntimeError:            # вікно вже знищене (тести)
            fset.unsubscribe(self._setting_changed)

    def _auto_fetch_mesh(self, backend, key: str) -> None:
        """Після прогону — сітку на цей компʼютер, якщо так налаштовано
        (типово вимкнено: сотні МБ на повільному каналі)."""
        if (not fset.get("mesh_auto_fetch") or backend is None
                or backend.kind == "native" or be.local_mesh_dir(key) is not None):
            return
        self.log.push(fs_i18n.tr(
            "Copying the mesh {key} from {host} (File → Settings → Mesh)…",
            key=key, host=backend.host or backend.label))

        def job():
            try:
                err = be.fetch_mesh(backend, key)
            except Exception as exc:                       # noqa: BLE001
                err = str(exc)
            try:
                self.mesh_fetched.emit(key, err)
            except RuntimeError:
                pass
        threading.Thread(target=job, daemon=True).start()

    def _mesh_fetched(self, key: str, err: str) -> None:
        if err:
            self.log.push(fs_i18n.tr("The mesh {key} was not copied: {err}", key=key, err=err))
        else:
            self.log.push(fs_i18n.tr("The mesh {key} is now on this computer.", key=key))

    def default_project_path(self) -> Path:
        return self.projects_dir() / f"{self.cfg.name or 'project'}{lib.PROJECT_EXT}"

    def _project_signature(self) -> str:
        """Відбиток того, що піде у проєкт: налаштування плюс склад сцени.

        Геометрію саму не чіпаємо: перечитувати мегабайти сіток заради
        перевірки «чи змінилося» дорожче, ніж вона того варта. Зміну
        геометрії видно по складу деталей і множниках.
        """
        import json

        live = [p for p in self.parts if p.loaded]
        body = {
            "cfg": lib.cfg_to_dict(self.cfg),
            "parts": [[p.name, p.path, round(float(p.factor), 9), p.role,
                       bool(p.enabled), round(float(p.rpm), 6),
                       p.hinge is not None] for p in live],
        }
        return json.dumps(body, sort_keys=True, ensure_ascii=False, default=str)

    def project_is_dirty(self) -> bool:
        if not any(p.loaded for p in self.parts):
            return False
        return self._project_signature() != self._project_mark

    def save_project(self, path: str = "", ask: bool = True,
                     with_mesh: bool = False) -> str:
        """Зберігає проєкт. Порожній `path` і `ask=False` — у типову теку.

        Саме це й робить автозбереження перед прогоном: мовчки, туди, де
        проєкт уже лежить, або в типову теку під імʼям кейсу.

        Сітка: `with_mesh` — покласти сітку теперішніх налаштувань (вона
        має вже лежати на цьому компʼютері, див. save_project_with_mesh).
        Без нього сітки в проєкті немає — крім зафіксованої сітки з
        проєкту: вона діє, і викинути її мовчки означало б зламати проєкт.
        """
        live = [p for p in self.parts if p.loaded]
        if not live:
            if ask:
                QtWidgets.QMessageBox.warning(
                    self, tr("Nothing to save"),
                    tr("No model in the scene has been read."))
            return ""
        if not path:
            start = str(Path(self._project_path) if self._project_path
                        else self.default_project_path())
            if ask:
                path, _ = QtWidgets.QFileDialog.getSaveFileName(
                    self, ("Save Project with Mesh" if with_mesh
                           else "Save Project"), start,
                    tr("OpenFOAM Studio project (*{PROJECT_EXT});;All files (*)", PROJECT_EXT=lib.PROJECT_EXT))
                if not path:
                    return ""
            else:
                path = start
        rows = [self._part_row(p) for p in live]
        extra = {
            "channels": dict(self.channels),
            "units": self.panel_geo.cmb_units.currentText(),
            "case_dir": self.case_dir or "",
            "cases_dir": self.panel_run.ed_dir.text().strip(),
        }
        mkey, mdir, keep = "", None, False
        if self._pmesh.active:
            # Зафіксована сітка з проєкту їде далі під СВОЇМ ключем.
            mkey = self._pmesh.key
            mdir = be.local_mesh_dir(mkey)
            keep = True
        elif with_mesh:
            mkey = self.mesh_inputs_key()
            mdir = be.local_mesh_dir(mkey)
            if mdir is None:
                if ask:
                    QtWidgets.QMessageBox.warning(
                        self, tr("No mesh"),
                        tr("There is no mesh on this computer for the "
                        "current settings."))
                return ""
        try:
            out = self._off_thread(
                tr("Writing the project{v}", v=" with mesh…" if mdir else "…"),
                lambda: lib.write_project(path, self.cfg, rows, extra,
                                          mesh_key=mkey, mesh_dir=mdir,
                                          keep_mesh=keep),
                size=BIG_MESH if mdir else 0)
        except OSError as exc:
            if ask:
                QtWidgets.QMessageBox.critical(
                    self, tr("Could not save"), str(exc))
            else:
                self.log.push(tr("Project not saved: {exc}", exc=exc))
            return ""
        size = os.path.getsize(out) / 1e6
        self._project_path = out
        self._project_mark = self._project_signature()
        has = lib.project_mesh_key(out)
        if has and self._pmesh.active:
            self._pmesh.project = out
        self.log.push(tr("Project saved: {out} ({size:.1f} MB, {n_rows} "
                         "parts{v})", out=out, size=size, n_rows=len(rows), v=tr(", with mesh {has}", has=has) if has else ", without mesh"))
        self.statusBar().showMessage(
            tr("Project saved: {name}", name=Path(out).name), 8000)
        return out

    # --- сітка в проєкті (fs_meshlock) -------------------------------------

    def _mesh_for_save(self) -> "tuple[str, str, str]":
        """(ключ, де сітка, чому ні) для «Зберегти проєкт із сіткою».

        Де: "local" — уже на цьому компʼютері; "remote" — у сховищі бекенда
        (треба забрати); "" — сітки немає, а «чому ні» каже чому.
        """
        if not any(p.loaded for p in self.parts):
            return "", "", tr("There is no model in the scene.")
        if self._pmesh.active:
            if be.local_mesh_dir(self._pmesh.key) is not None:
                return self._pmesh.key, "local", ""
        sig = (self._project_signature(), id(self.mesh), self.case_dir)
        if self._mesh_menu_cache and self._mesh_menu_cache[0] == sig:
            key = self._mesh_menu_cache[1]
        else:
            try:
                key = self.mesh_inputs_key()
            except Exception:                              # noqa: BLE001
                key = ""
            self._mesh_menu_cache = (sig, key)
        if not key:
            return "", "", tr("There is no geometry in the scene.")
        if be.local_mesh_dir(key) is not None:
            return key, "local", ""
        b = self.panel_run.current_backend()
        if b is not None and (self._res_key(b), key) in self._remote_meshes:
            return key, "remote", ""
        return key, "", (tr("There is no mesh for the current settings yet: "
                         "the first run will build it (snappyHexMesh). "
                         "Or the mesh settings or the geometry were "
                         "changed after the run."))

    def _update_mesh_menu(self) -> None:
        """Пункт «Зберегти проєкт із сіткою» — лише коли сітка є."""
        try:
            key, where, why = self._mesh_for_save()
        except Exception as exc:                           # noqa: BLE001
            key, where, why = "", "", str(exc)
        act = self.act_save_mesh
        act.setEnabled(bool(where))
        if where == "remote":
            act.setToolTip(tr("Mesh {key} is on the server: it will be "
                              "downloaded to this computer (hundreds of "
                              "MB) and put into the project.", key=key))
        elif where:
            act.setToolTip(tr("Mesh {key} is already on this computer.", key=key))
        else:
            act.setToolTip(why)
        b = self.panel_run.current_backend()
        if (not where and key and b is not None and b.ready
                and be.mesh_cache_supported(b)):
            # Можливо, сітка є на сервері від прогону до перезапуску програми:
            # питаємо у фоні, пункт оживе при наступному відкритті меню.
            rk = self._res_key(b)

            def probe(b=b, key=key, rk=rk):
                try:
                    if be.remote_mesh_exists(b, key):
                        self._remote_meshes.add((rk, key))
                except Exception:                          # noqa: BLE001
                    pass
            threading.Thread(target=probe, daemon=True).start()

    def save_project_with_mesh(self) -> str:
        """«Зберегти проєкт із сіткою…»: сітка теперішніх налаштувань у файл.

        Сітки ще немає на цьому компʼютері, але вона є на сервері — спершу
        забираємо (з вікном поступу), потім пишемо проєкт.
        """
        key, where, why = self._mesh_for_save()
        if not where:
            QtWidgets.QMessageBox.information(self, tr("No mesh yet"), why)
            return ""
        if where == "remote":
            b = self.panel_run.current_backend()
            try:
                err = self._off_thread(
                    tr("Getting mesh {key} from {v} (hundreds of MB, "
                       "this can take minutes)…", key=key, v=b.host or b.label),
                    lambda: be.fetch_mesh(b, key), size=BIG_MESH)
            except Exception as exc:                       # noqa: BLE001
                err = str(exc)
            if err:
                QtWidgets.QMessageBox.warning(self, tr("Mesh not downloaded"), err)
                return ""
            self.log.push(tr("The mesh {key} is now on this computer.", key=key))
        return self.save_project(with_mesh=True)

    def _show_pmesh(self) -> None:
        text = self._pmesh.label()
        for lbl in (self.lbl_pmesh, getattr(self, "lbl_pmesh_tab", None)):
            if lbl is None:
                continue
            lbl.setText(("🔒 " if self._pmesh.active else "") + text)
            lbl.setVisible(bool(text))
            lbl.setToolTip(
                (tr("Mesh key {key}, from project {name}. Fields that "
                    "change the mesh are dimmed: a click on one asks "
                    "whether to rebuild the mesh.", key=self._pmesh.key, name=Path(self._pmesh.project).name)) if self._pmesh.active else "")

    def _pmesh_activate(self, key: str, project: str) -> bool:
        """Сітка проєкту `key` діє: запамʼятати базовий ключ і замкнути поля."""
        d = be.local_mesh_dir(key)
        if d is None or self.mesh is None:
            return False
        self._pmesh = meshlock.ProjectMesh(
            key=key, cells=memfit.mesh_cells(d), project=str(project),
            baseline=self.mesh_inputs_key(), active=True)
        # Перебудову, поставлену до відкриття, знімаємо: вона проти сітки.
        if self.panel_run.chk_remesh.isChecked():
            self.panel_run.chk_remesh.setChecked(False)
        self._mlock.lock(meshlock.mesh_controls(self),
                         meshlock.free_controls(self))
        self._show_pmesh()
        same = self._pmesh.baseline == key
        self.log.push(
            tr("Mesh from project {key} ({cells} cells) is locked: the "
               "run uses it, snappyHexMesh will not run.", key=key,
               cells=fmt.count(self._pmesh.cells))
            + ("" if same else
               tr(" This version of the program would write the settings "
                  "a bit differently (key {baseline}), but you changed "
                  "nothing — the project mesh applies.", baseline=self._pmesh.baseline)))
        return True

    def _snappy_minutes(self) -> int:
        cells = self._pmesh.cells or self.cfg.estimate().total
        return meshlock.snappy_minutes(int(cells))

    def _ask_unlock_mesh(self, widget=None, what: str = "") -> bool:
        """Питання замка. True — людина погодилась перебудувати сітку."""
        name = what or (panels._field_name(widget) if widget is not None else "")
        box = QtWidgets.QMessageBox(self)
        box.setIcon(QtWidgets.QMessageBox.Warning)
        box.setWindowTitle("Mesh from Project")
        box.setText(
            tr("If you change this value, the mesh must be rebuilt "
               "(snappyHexMesh, ~{snappy_minutes} min on the server). "
               "Change it?", snappy_minutes=self._snappy_minutes()))
        if name:
            box.setInformativeText(tr("Field: {name}", name=name))
        cancel = box.addButton(tr("Cancel"), QtWidgets.QMessageBox.RejectRole)
        go = box.addButton(tr("Change and Rebuild"),
                           QtWidgets.QMessageBox.AcceptRole)
        box.setDefaultButton(cancel)
        box.setEscapeButton(cancel)
        box.exec()
        return box.clickedButton() is go

    def _release_project_mesh(self) -> None:
        """Сітка проєкту більше не діє (людина погодилась перебудувати)."""
        if not self._pmesh.active:
            return
        self._mlock.unlock()
        self._pmesh.active = False
        self._pmesh.released = True
        self._show_pmesh()
        self.log.push(tr("The project mesh is released: the mesh settings "
                      "are unlocked again, the next run will build a new "
                      "mesh."))

    def _geo_guard(self, index: int, what: str, solo: bool = False) -> bool:
        """Перед зміною складу сцени зі списку деталей (не через замкнене
        поле): прибрати деталь чи лишити саму одну. Деталь, що в кейс не
        йде, сітки не міняє — тоді не питаємо."""
        if not self._pmesh.active or not (0 <= index < len(self.parts)):
            return True
        part = self.parts[index]
        live = [p for p in self.parts if p.loaded and p.enabled]
        if solo:
            touches = any(p is not part for p in live) or part not in live
        else:
            touches = part in live
        if not touches:
            return True
        if self.mesh_guard(f"{what}: «{part.name}»"):
            return True
        self.panel_geo.show_parts(self._part_rows(), self._scene_line())
        return False

    def mesh_guard(self, what: str = "") -> bool:
        """Перед дією, що змінює сітку, не через поле (меню, кнопка вікна).

        True — можна (сітки з проєкту немає, або людина погодилась)."""
        if not self._pmesh.active:
            return True
        if not self._ask_unlock_mesh(what=what):
            return False
        self._release_project_mesh()
        return True

    def _autosave_project(self) -> str:
        """Тихе збереження проєкту перед прогоном.

        Імʼя беремо від кейсу — так збережений кейс і проєкт знаходять одне
        одного потім, коли симуляцію відкривають із «Збережених».
        """
        if not any(p.loaded for p in self.parts):
            return ""
        target = self._project_path or str(self.default_project_path())
        try:
            return self.save_project(path=target, ask=False)
        except Exception as exc:                       # noqa: BLE001
            # Автозбереження не має права зірвати запуск: людина натиснула
            # «Запустити», а не «Зберегти».
            self.log.push(tr("Project not saved automatically: {exc}", exc=exc))
            return ""

    def project_for_case(self, case_name: str) -> Path | None:
        """Проєкт, що належить цьому кейсу, — або None."""
        if not case_name:
            return None
        p = self.projects_dir() / f"{case_name}{lib.PROJECT_EXT}"
        return p if p.is_file() else None

    def open_project(self, path: str = ""):
        if self._load_thread is not None:
            self.statusBar().showMessage(
                tr("Wait: the previous geometry is still being read."), 6000)
            return
        if not path:
            path, _ = QtWidgets.QFileDialog.getOpenFileName(
                self, tr("Open Project"), str(self.projects_dir()),
                tr("OpenFOAM Studio project (*{PROJECT_EXT});;All files (*)", PROJECT_EXT=lib.PROJECT_EXT))
        if not path:
            return
        data = lib.read_project(path)
        if data is None:
            QtWidgets.QMessageBox.critical(
                self, tr("This is not a project"),
                tr("Could not read the file {name} as an OpenFOAM Studio "
                   "project.", name=Path(path).name))
            return
        # Нова сцена: сітка попереднього проєкту більше не діє.
        self._mlock.unlock()
        self._pmesh = meshlock.ProjectMesh()
        self._pmesh_pending = None
        self._show_pmesh()
        # Сітка з проєкту — у сховище на цьому компʼютері; на сервер вона
        # піде перед прогоном, якщо там її немає.
        if lib.project_mesh_key(path):
            try:
                key = self._off_thread(
                    tr("Unpacking the mesh from the project…"),
                    lambda: lib.extract_project_mesh(path, be.LOCAL_MESH_STORE))
                if key:
                    # Діяти почне, коли сцена збереться (_apply_project).
                    self._pmesh_pending = (key, str(path))
            except Exception as exc:                      # noqa: BLE001
                self.log.push(tr("Project mesh not unpacked: {exc}", exc=exc))
        self._open_project_data(data, path)

    def _open_project_data(self, data: dict, path: str,
                           session: dict | None = None) -> bool:
        """Відкрити вже прочитаний проєкт. False — геометрії в ньому немає.

        `session` — це не проєкт людини, а сцена останньої сесії: тоді
        файл не стає «поточним проєктом» (автозбереження не має писати в
        службовий файл), історія моделей лишається, а після ролей
        доробляється решта сесії.
        """
        rows = [r for r in data["parts"] if r.get("stl")]
        lost = [r.get("name", "?") for r in data["parts"] if not r.get("stl")]
        if not rows:
            if session is None:
                QtWidgets.QMessageBox.critical(
                    self, tr("The project has no geometry"),
                    tr("Could not read any part."))
            return False
        if lost:
            # Мовчки проковтнути зниклу деталь не можна: сцена виглядатиме
            # цілою, а в кейс піде не те, що людина зберігала.
            QtWidgets.QMessageBox.warning(
                self, tr("Part of the geometry is missing"),
                tr("The file has no STL for: "
                   "{join}{v}{NEWLINE}{NEWLINE}The rest will be opened.", join=", ".join(lost[:8]), v="…" if len(lost) > 8 else "", NEWLINE=NEWLINE))

        # Розпаковуємо у ТИМЧАСОВУ теку, а не поруч із проєктом: проєкт
        # може лежати на флешці чи в теці лише для читання.
        tmp = Path(tempfile.mkdtemp(prefix="foamproj_open_"))
        self._project_tmp = tmp
        files = []
        for i, r in enumerate(rows):
            f = tmp / f"{i:03d}_{r.get('name') or 'part'}"
            if f.suffix.lower() != ".stl":
                f = f.with_suffix(".stl")
            f.write_bytes(r["stl"])
            files.append(str(f))
            r.pop("stl", None)

        self._project_rows = rows
        self._project_extra = data.get("extra") or {}
        if session is None:
            self.parts = []
            # Запамʼятовуємо, що саме відкрито: далі автозбереження перед
            # прогоном піде в цей же файл, а не заведе другий поруч.
            self._project_path = str(path)
        # Налаштування ВЛИВАЄМО в наявний cfg, а не підміняємо обʼєкт:
        # панелі тримають посилання на нього, і підміна лишила б їх
        # редагувати вже нікому не потрібну копію.
        lib.apply_dict(self.cfg, lib.cfg_to_dict(data["cfg"]))
        # Той самий шлях, яким відкривається збережений кейс: черга
        # відновлення САМА ставить метри й множник 1.0, а це тут і
        # потрібно — геометрія в проєкті вже в метрах і вже розвернута.
        if session is None:
            self._pending_restore = self._apply_project
        else:
            ui = session.get("ui") if isinstance(session.get("ui"), dict) else {}
            pm = ui.get("project_mesh") if isinstance(ui.get("project_mesh"), dict) else {}
            if pm.get("key"):
                # Підбір y+ не має переписати шари, поки сцена збирається.
                self._pmesh_pending = (str(pm["key"]), str(pm.get("project") or ""))

            def finish(s=session):
                pending = getattr(self, "_pmesh_pending", None)
                # Тримаємо підбір y+, але сітку вмикаємо лише в кінці, коли
                # сесія вже влила свій конфіг.
                self._pmesh_pending = None
                self._pmesh_hold = bool(pending)
                self._apply_project()
                self._dedupe_history()
                self._apply_session(s)
                # Сесія вливає свій конфіг ПОВЕРХ — разом зі списками
                # патчів, двигунів і дисків. Правда ж — у деталях, тож
                # перезбираємо з них.
                self.rebuild_geometry()
                self.refresh_all()
                self._pmesh_hold = False
                if pending:
                    self._pmesh_activate(*pending)
            self._pending_restore = finish
        self._restore_queue = files[1:]
        self.load_stl(files[0])
        return True

    def _dedupe_history(self) -> None:
        """Прибрати з історії рядки тих файлів, що вже повернулися в сцену.

        Сесія відкривається поверх історії моделей, а деталі сцени
        дістають свої вихідні шляхи лише в `_apply_project` — уже після
        `_remember`, який і мав би злити дублі. Без цього той самий файл
        стояв би в списку двічі: живою деталлю й нечитаною історією.
        """
        live = {self._key(p.path) for p in self.parts if p.loaded}
        self.parts = [p for p in self.parts
                      if p.loaded or self._key(p.path) not in live]
        self.panel_geo.show_parts(self._part_rows(), self._scene_line())

    def _apply_project(self):
        """Роздати ролі, завіси й пульт — коли всі деталі вже в сцені."""
        rows = getattr(self, "_project_rows", [])
        extra = getattr(self, "_project_extra", {})
        # Порядок той самий, у якому клали: саме він і звʼязує деталь із
        # її роллю. Звіряти за іменем не можна — імена бувають однакові.
        # `_remember` кладе кожну нову деталь НА ПОЧАТОК списку, тож
        # після читання по черзі порядок у `self.parts` зворотний до
        # порядку файлів. Звіряти за іменем не можна: імена бувають
        # однакові, і тоді гвинт дістав би роль фюзеляжа.
        live = list(reversed([p for p in self.parts if p.loaded]))
        if len(live) != len(rows):
            self.log.push(
                tr("Project: {n_live} parts in the scene, but {n_rows} "
                   "in the file — the roles were assigned in order, "
                   "check them.", n_live=len(live), n_rows=len(rows)))
        for part, row in zip(live, rows):
            part.path = row.get("path") or part.path
            part.enabled = bool(row.get("enabled", True))
            part.role = row.get("role", "body")
            part.rpm = float(row.get("rpm", 3000.0))
            part.rotor_axis = row.get("rotor_axis", "x")
            part.engine = fc.patch_name_from(
                str(row.get("engine") or "engine1"))
            part.jet = _jet_from_row(row.get("jet"))
            part.disk = _disk_from_row(row.get("disk"))
            # Старий проєкт цього ключа не має — і тоді деталь згущується
            # як усі, тобто рівно так, як і згущувалася досі.
            lv = row.get("refine")
            part.refine = (tuple(int(v) for v in lv[:2])
                          if isinstance(lv, (list, tuple)) and len(lv) >= 2
                          else None)
            h = row.get("hinge")
            part.hinge = None if not h else fc.Hinge(
                pivot=tuple(h.get("pivot", (0, 0, 0))),
                axis=tuple(h.get("axis", (0, 1, 0))),
                offset=tuple(h.get("offset", (0, 0, 0))),
                channel=h.get("channel", ""), gain=float(h.get("gain", 20.0)),
                trim=float(h.get("trim", 0.0)),
                limit=float(h.get("limit", 30.0)),
                reverse=bool(h.get("reverse", False)))
        ch = extra.get("channels")
        if isinstance(ch, dict):
            self.channels.update({k: float(v) for k, v in ch.items()
                                  if k in fc.CHANNEL_KEYS})
        if extra.get("cases_dir"):
            self.panel_run.ed_dir.setText(str(extra["cases_dir"]))
        self.case_dir = extra.get("case_dir") or ""
        # Коли проєкт відкрили через «Збережені», кейс диктує список, а не
        # запис у проєкті: його могли перейменувати чи перенести.
        forced = getattr(self, "_project_force_case", "")
        if forced:
            self.case_dir = forced
            self._project_force_case = ""
        units = str(extra.get("units") or "")
        if units and self.panel_geo.cmb_units.findText(units) >= 0:
            self.panel_geo.cmb_units.blockSignals(True)
            self.panel_geo.cmb_units.setCurrentText(units)
            self.panel_geo.cmb_units.blockSignals(False)
        self._project_rows = []
        # КОРОБКУ ЗГУЩЕННЯ ВІДНОВЛЮЄМО КООРДИНАТАМИ, а не параметрами.
        #
        # Панель тримає коробку трьома числами (запас, слід, рівень) і
        # перебудовує її з них перед кожним записом. Щоб показати
        # збережену коробку, ці числа доводиться діставати ОБЕРНЕНОЮ
        # задачею — а поля округлюють їх до двох і одного знака. Туди-сюди
        # виходить 0.2% різниці: на фізику не впливає, але «зберігає все»
        # перестає бути правдою.
        #
        # Тепер, якщо параметри не відтворюють збережену коробку точно,
        # вмикаємо ручний режим і кладемо координати як є.
        saved_box = next((b for b in self.cfg.mesh.boxes
                          if b.name == "nearBody"), None)
        self.reload_panels()
        if saved_box is not None:
            self.panel_mesh.sync_box()
            rebuilt = next((b for b in self.cfg.mesh.boxes
                            if b.name == "nearBody"), None)
            off = 0.0 if rebuilt is None else max(
                max(abs(a - b) for a, b in zip(saved_box.min, rebuilt.min)),
                max(abs(a - b) for a, b in zip(saved_box.max, rebuilt.max)))
            if rebuilt is None or off > 1e-9:
                p = self.panel_mesh
                p.box_manual.blockSignals(True)
                p.box_manual.setChecked(True)
                p.box_manual.blockSignals(False)
                for sp, v in zip(p.box_lo, saved_box.min):
                    sp.blockSignals(True)
                    sp.setValue(float(v))
                    sp.blockSignals(False)
                for sp, v in zip(p.box_hi, saved_box.max):
                    sp.blockSignals(True)
                    sp.setValue(float(v))
                    sp.blockSignals(False)
                p._box_manual_toggled(True)
        self.rebuild_geometry()
        self.refresh_all()
        self._show_controls()
        # Щойно відкрите — це і є «збережений стан»: інакше вихід одразу
        # після відкриття питав би, чи зберегти нічим не змінений проєкт.
        self._project_mark = self._project_signature()
        self.log.push(
            tr("Project opened: {n_live} parts, roles and hinges restored.", n_live=len(live)))
        pending = getattr(self, "_pmesh_pending", None)
        self._pmesh_pending = None
        if pending:
            self._pmesh_activate(*pending)
        self.statusBar().showMessage(tr("Project opened."), 8000)

    def save_geometry(self):
        """Віддає назовні те, що зараз у сцені.

        Полагоджена геометрія — це результат роботи, а не проміжний стан:
        її несуть назад у Blender, шлють колезі, кладуть у власний кейс.
        Досі вийняти її з програми можна було тільки через створення кейсу.

        Пишемо рівно те, що у вʼюпорті: у метрах, розвернуте, з усіма
        ремонтами. Тому файл придатний і для OpenFOAM, і для перевірки в
        будь-якому іншому переглядачі.
        """
        if self.mesh is None:
            QtWidgets.QMessageBox.warning(
                self, tr("No geometry"), tr("The scene is empty."))
            return
        start = self.cfg.name or "body"
        for part in self.parts:
            if part.enabled and part.path:
                start = str(Path(part.path).with_name(
                    Path(part.path).stem + "_fixed.stl"))
                break
        # FORMATS: the shared package (meshfix.check.save_mesh, 0.3.0 and
        # later) writes each format; each model of the scene is one body
        # (an object in 3MF, OBJ, ASCII STL, glTF and VTK). The formats
        # without a unit get meters, as the scene; 3MF gets mm, glTF m.
        # Without such a package: binary STL only, as before.
        chk = geo.formats()
        bodies = self.scene_bodies()
        filters = (chk.export_filters(len(bodies)) if chk
                   else [("stl", tr("STL binary") + " (*.stl)")])
        texts = [text for _key, text in filters]
        path, picked = QtWidgets.QFileDialog.getSaveFileName(
            self, tr("Save Geometry"), start, ";;".join(texts), texts[0])
        if not path:
            return
        keys = [key for key, text in filters if text == picked]
        try:
            if chk is None:
                if Path(path).suffix.lower() != ".stl":
                    path = str(Path(path).with_suffix(".stl"))
                geo.write_stl(self.mesh, path, binary=True)
                note = tr("The geometry is in meters.")
            else:
                if Path(path).suffix.lower() not in chk.READERS:
                    path = path + chk.FORMATS[keys[0] if keys else "stl"].suffix
                mesh = (chk.join_bodies(bodies) if len(bodies) > 1
                        else self.mesh)
                info = chk.save_mesh(mesh, path,
                                     chk.format_for(path,
                                                    keys[0] if keys else None),
                                     unit_m=1.0)
                note = chk.save_note(info)
        except Exception as exc:
            QtWidgets.QMessageBox.critical(self, tr("Could not write"),
                                           f"{type(exc).__name__}: {exc}")
            return
        size = os.path.getsize(path) / 1e6
        text = tr("Wrote {name}: {n_cells} triangles, {size:.1f} MB, exactly "
                  "what is in the viewport.", name=Path(path).name,
                  n_cells=phys.fmt_cells(self.mesh.n_cells), size=size)
        self.log.push(text + " " + note)
        self.statusBar().showMessage(text + " " + note, 12000)

    def scene_bodies(self) -> list:
        """[(name, mesh)] of the models of the scene, in the pose of the
        sticks (as in the viewport and in the case). The name is the patch
        name of the case, else the file name."""
        out = []
        for part in self.enabled_parts():
            name = self._patch_of(part) or Path(part.path).stem or "body"
            out.append((name, part.posed(self.channels) or part.mesh))
        return out

    def open_stl(self):
        self._ask_file(add=False)

    def add_stl(self):
        self._ask_file(add=True)

    def _ask_file(self, add: bool):
        if not self.mesh_guard(tr("add geometry") if add
                               else tr("open other geometry")):
            return
        start = ""
        for part in self.parts:
            if part.path:
                start = str(Path(part.path).parent)
                break
        paths, _ = QtWidgets.QFileDialog.getOpenFileNames(
            self, "Add Geometry" if add else "Open Geometry", start,
            geo.open_filter())
        if not paths:
            return
        if not add and self._pmesh.released:
            self._pmesh = meshlock.ProjectMesh()     # нова сцена — без підпису
            self._show_pmesh()
        # Порядок ТОЙ, ЩО ВІДДАВ ДІАЛОГ, але впорядкований за іменем:
        # Qt повертає файли в порядку виділення, а людина, вибравши
        # Ctrl+A, чекає побачити їх так, як бачила в каталозі.
        paths = sorted(paths)
        # Одиниці питаємо ЩОРАЗУ, типово міліметри: поле «Одиниці STL»
        # могло лишитися «метрами» від відкритого кейсу чи сесії.
        # A 3MF or glTF file gives its unit: then no question («авто» takes
        # the unit of the file, fs_geometry.guess_scale).
        if all(geo.unit_in_file(p) for p in paths):
            units = "авто"
        else:
            units = panels.UnitsDialog.ask(self, paths)
        if units is None:
            return
        self.start_batch(paths, add=add, units=units)

    def start_batch(self, paths, add: bool = False,
                    units: str | None = None):
        """Прочитати кілька файлів один за одним.

        По одному, а не всі разом: читання йде в окремому потоці, і
        другий потік на ту саму сцену дав би гонитву за `self.parts`.
        Черга тут дешевша за блокування.
        """
        paths = [str(p) for p in paths if str(p).strip()]
        if not paths:
            return
        if self._load_thread is not None:
            self.statusBar().showMessage(
                tr("Wait: the previous geometry is still being read."), 6000)
            return
        self._batch_queue = list(paths[1:])
        self._batch_failed = []
        self._batch_total = len(paths)
        self._batch_units = units
        self.load_stl(paths[0], add=add, units=units)

    def load_stl(self, path: str, add: bool = False,
                 units: str | None = None, factor: float | None = None):
        """Читання і діагностика йдуть в окремому потоці.

        Стомегабайтний STL читається секунд сім, і ще стільки ж забирає перший
        рендер — тримати на цей час замерзле вікно не можна.
        """
        if self._load_thread is not None:
            return
        self._load_add = add
        self._load_units = units
        self._load_factor = factor

        # У пачці кажемо, котрий саме файл: без цього довге читання
        # десятка моделей виглядає як зависання на першій.
        head = tr("Reading {name}…", name=Path(path).name)
        if self._batch_total > 1:
            done = self._batch_total - len(self._batch_queue)
            head = (tr("Reading {done} of {batch_total}: {name}…", done=done, batch_total=self._batch_total, name=Path(path).name))
        self._progress = QtWidgets.QProgressDialog(head, "", 0, 0, self)
        self._progress.setWindowTitle("OpenFOAM Studio")
        self._progress.setCancelButton(None)
        self._progress.setWindowModality(QtCore.Qt.WindowModal)
        self._progress.setMinimumDuration(400)

        self._load_head = head
        self._loader = GeometryLoader(path)
        self._load_thread = QtCore.QThread(self)
        self._load_thread.setObjectName("load")
        self._loader.moveToThread(self._load_thread)
        self._load_thread.started.connect(self._loader.run)
        self._loader.ready.connect(self._on_geometry_ready)
        self._loader.failed.connect(self._on_geometry_failed)
        # Саме метод вікна, а не лямбда: лямбду Qt виконав би в потоці
        # читання, і підпис віджета мінявся б із чужого потоку.
        self._loader.step.connect(self._on_load_step)
        self._load_thread.start()

    @QtCore.Slot(str)
    def _on_load_step(self, text: str):
        if self._progress is not None:
            self._progress.setLabelText(self._load_head + NEWLINE + text)

    def _end_load(self):
        if self._progress is not None:
            _drop_progress(self._progress)
            self._progress = None
        if self._load_thread is not None:
            self._load_thread.quit()
            self._load_thread.wait(5000)
            self._load_thread = None
        self._loader = None

    @QtCore.Slot(str)
    def _on_geometry_failed(self, msg: str):
        """Один зіпсований файл НЕ МАЄ спиняти пачку.

        Інакше десять моделей, де третя бита, дають одне модальне вікно й
        сім непрочитаних файлів — причому мовчки: черга просто лишається
        в памʼяті, і зовні це «натиснув, а нічого не сталося».

        Тому збій у пачці лише записуємо, читаємо далі, а поскаржимося
        один раз наприкінці й одразу про всі.
        """
        bad = self._loader.path if self._loader else ""
        self._end_load()
        if self._batch_queue or self._batch_total > 1:
            self._batch_failed.append(f"{Path(bad).name}: {msg}"
                                      if bad else msg)
            if self._batch_queue:
                self.load_stl(self._batch_queue.pop(0), add=True,
                              units=self._batch_units)
                return
            self._finish_batch()
            return
        QtWidgets.QMessageBox.critical(self, tr("Could not read"), msg)

    def _finish_batch(self):
        """Підсумок пачки: скільки прочиталося і що не далося."""
        total, failed = self._batch_total, list(self._batch_failed)
        self._batch_total, self._batch_failed = 0, []
        self._batch_units = None
        if not total:
            return
        ok = total - len(failed)
        if failed:
            # Модальне вікно тут доречне: мовчки проковтнути непрочитані
            # файли гірше, ніж перебити роботу. Але воно ОДНЕ на пачку.
            QtWidgets.QMessageBox.warning(
                self, tr("Not all files could be read"),
                tr("Read {ok} of {total}. Failed:\n\n{join}{v}", ok=ok, total=total, join="\n".join(failed[:10]), v="\n…" if len(failed) > 10 else ""))
        elif total > 1:
            self.statusBar().showMessage(
                tr("Models added: {ok}.", ok=ok), 8000)
        if ok:
            self.log.push(tr("Batch: read {ok} of {total} files.", ok=ok, total=total))

    @QtCore.Slot(object, float, str, object)
    def _on_geometry_ready(self, mesh, factor, why, diag):
        path = self._loader.path if self._loader else ""
        self._end_load()
        self._finish_load(path, mesh, factor, why, diag)

    def _finish_load(self, path, mesh, factor, why, diag):
        add = self._load_add and bool(self.enabled_parts())
        self._load_add = False
        self.stl_path = path
        if not add:
            self.stl_source = path
        self._auto_factor = factor
        self.panel_geo.set_file(path + NEWLINE + why)
        # Поки черга частин не порожня, `restore` не забираємо: він має
        # спрацювати аж коли сцена повна. Але сама частина мусить дійти до
        # сцени — тому перевірка черги стоїть НИЖЧЕ, після _remember().
        restore = None if self._restore_queue else self._pending_restore
        if restore is not None:
            self._pending_restore = None
        if self._restore_queue or (restore is not None and add):
            # Частини кейсу вже в метрах — таким їх туди й записали.
            self.panel_geo.cmb_units.blockSignals(True)
            self.panel_geo.cmb_units.setCurrentText("метри")
            self.panel_geo.cmb_units.blockSignals(False)
            self._auto_factor = 1.0
        if restore is not None:
            # STL із кейсу вже в метрах — таким його туди й записали.
            # Автовизначення одиниць тут не просто зайве, а шкідливе: у
            # дрібної моделі габарит той самий, що в моделі в міліметрах,
            # і відкритий кейс поїхав би в тисячу разів.
            self.panel_geo.cmb_units.blockSignals(True)
            self.panel_geo.cmb_units.setCurrentText("метри")
            self.panel_geo.cmb_units.blockSignals(False)
            self._auto_factor = 1.0
        # ОДИНИЦІ, ЯКІ ЛЮДИНА НАЗВАЛА ДЛЯ ЦЬОГО ФАЙЛУ (вікно перед читанням)
        # або множник, з яким деталь уже лежала (повторна галочка). Для
        # відновлення кейсу чи сесії їх немає: там метри, і це вирішено вище.
        units, self._load_units = self._load_units, None
        own, self._load_factor = self._load_factor, None
        explicit = None          # свій множник деталі — не з поля «Одиниці»
        if restore is None and not self._restore_queue:
            if own is not None:
                explicit = own
            elif units and not add:
                # «Відкрити» починає сцену заново: поле одиниць стає тим,
                # що назвали, — і вже не тим, що лишилося від минулого кейсу.
                self.panel_geo.set_units(units)
            elif units and units != self.panel_geo.cmb_units.currentText():
                # «Додати» в інших одиницях, ніж решта сцени. Поле одне на
                # всі деталі, і перемкнути його означало б перемасштабувати
                # ті, що вже лежать. Тому ця деталь отримує свій множник і
                # прибивається до нього, як деталь із кейсу.
                explicit = panels.units_factor(units, factor)
        if restore is None and own is None and (units or not add):
            # ВИБІР ЛЮДИНИ НЕ ПЕРЕБИВАЄМО.
            #
            # Раніше тут стояло примусове перемикання на «авто» — і воно
            # робило налаштування одиниць безглуздим: поставив міліметри,
            # відкрив файл, а в полі знову «авто».
            #
            # Натомість кажемо, коли вибір розходиться з тим, що видно з
            # габариту. Розходження в тисячу разів — це не дрібниця, і
            # мовчати про нього гірше, ніж перебити вибір.
            chosen = units or self.panel_geo.cmb_units.currentText()
            if chosen != "авто":
                got = (explicit if explicit is not None
                       else self.panel_geo.unit_factor(factor))
                if got > 0 and factor > 0 and max(got / factor,
                                                  factor / got) > 4.0:
                    text = (tr("Units: “{chosen}” is selected, but the "
                               "overall size suggests a factor of "
                               "{factor:g} ({why}). If the model in the "
                               "scene is many times too large or too "
                               "small, open it again with other units.", chosen=chosen, factor=factor, why=why))
                    self.log.push(text)
                    self.statusBar().showMessage(text, 20000)

        auto = 1.0 if restore is not None else factor
        target = (explicit if explicit is not None
                  else self.panel_geo.unit_factor(auto))
        if not add:
            # «Відкрити» починає сцену спочатку: попередні моделі лишаються
            # в списку історією, але памʼять під їхні сітки тримати нема за що.
            # Геометрію ж — з ремонтом, поворотами й одиницями — паркуємо на
            # диск: інакше повторна галочка перечитала б вихідний файл, і
            # ремонт пропав би мовчки.
            for stale in self.parts:
                self._park(stale)
                stale.enabled = False
                stale.mesh = None
        # Прибиваємо одиниці всьому, що йде через відновлення: і першому
        # файлу (restore не None), і решті черги (черга ще не порожня).
        part = Part(path=path, mesh=geo.apply_scale(mesh, target),
                    factor=target, auto=auto, why=why,
                    pinned=(restore is not None) or bool(self._restore_queue)
                    or explicit is not None)
        # Лічильники топології від масштабу не залежать, тому діагностика з
        # потоку годиться для позначки в списку за будь-якого множника.
        self._note_part(part, diag)
        self._remember(part)

        # Саму діагностику передаємо далі тільки тоді, коли масштаб той
        # самий і модель у сцені одна: інакше в ній чужі габарити й площі.
        same_scale = abs(target - auto) < 1e-12
        if self._restore_queue or self._batch_queue:
            # ЩЕ НЕ ОСТАННЯ ДЕТАЛЬ: сцену не перебудовуємо. Раніше кожна
            # деталь проєкту перевіряла всю сцену заново — на моделі з 250
            # тис. трикутників це 4 перевірки по ~1.3 с замість однієї, і
            # вікна «Читаю…» висіли одне над одним (2026-11-09). Перевіримо
            # раз, коли прийде остання.
            self._scale_parts()
        else:
            self.apply_units(precomputed=diag if same_scale and not add else None)
        if self._restore_queue:
            # Частина вже в сцені — беремо наступну. Потік до цього моменту
            # вже закритий у _end_load, тож load_stl приймає новий виклик
            # одразу, без відкладання.
            self.load_stl(self._restore_queue.pop(0), add=True)
            return
        if self._batch_queue:
            # Те саме для пачки, але БЕЗ примусових метрів: кожен файл
            # людини визначає свої одиниці сам.
            self.load_stl(self._batch_queue.pop(0), add=True,
                          units=self._batch_units)
            return
        if restore is not None:
            # Хто замовляв геометрію, той і доробляє: відкриття збереженого
            # кейсу й відновлення сесії роблять далі різні речі.
            restore()
            return
        if not add:
            name = fc.safe_case_name(Path(path).stem)
            self.cfg.name = name
            self.panel_run.ed_name.setText(name)
        self.auto_reference()
        self.refresh_all()
        self.tabs_right.setCurrentWidget(self.viewport)
        self.suggest_decimation()
        if self._batch_total:
            self._finish_batch()
        if add:
            self.statusBar().showMessage(
                tr("Added {name}: {n_enabled_parts} models in the scene, "
                   "{n_cells} triangles. Their union goes into the case.", name=part.name, n_enabled_parts=len(self.enabled_parts()), n_cells=phys.fmt_cells(self.mesh.n_cells)), 12000)

    # --- моделі у сцені -------------------------------------------------

    def enabled_parts(self) -> list[Part]:
        return [p for p in self.parts if p.enabled and p.loaded]

    def _remember(self, part: Part):
        """Кладе модель на початок списку, не лишаючи дублів того самого файлу."""
        key = self._key(part.path)
        self.parts = [p for p in self.parts if self._key(p.path) != key]
        self.parts.insert(0, part)
        # Історія не безмежна, але викидаємо тільки те, чого зараз немає в
        # сцені: мовчки прибрати модель, яку рахують, було б диверсією.
        while len(self.parts) > MAX_PARTS:
            spare = [i for i, p in enumerate(self.parts) if not p.enabled]
            if not spare:
                break
            self.parts.pop(spare[-1])

    @staticmethod
    def _key(path: str) -> str:
        return os.path.normcase(os.path.abspath(path)) if path else ""

    def _cache_dir(self) -> Path:
        return Path(DATA_DIR) / lib.SESSION_DIR / "parts"

    def _park(self, part: Part) -> None:
        """Покласти геометрію деталі в кеш перед тим, як забути сітку.

        Пишемо щоразу, а не лише «якщо змінена»: ознаки зміни в деталі
        немає, а ремонт, поворот окремої деталі й одиниці міняють сітку
        по-різному. Запис STL — копійки проти ремонту, який він береже.
        """
        if not part.loaded or not part.path:
            return
        if part.mesh.n_cells > lib.MESH_CACHE_LIMIT:
            return
        import hashlib
        name = hashlib.sha1(self._key(part.path).encode("utf-8")).hexdigest()
        target = self._cache_dir() / f"{name[:16]}.stl"
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            geo.write_stl(part.mesh, str(target), binary=True)
            part.cache = str(target)
        except Exception as exc:                     # noqa: BLE001
            # Кеш — послуга, а не умова: без нього лишається вихідний файл.
            part.cache = ""
            self.log.push(tr("Could not save the geometry of “{name}” "
                             "before moving it to history: {exc}. "
                             "Checking it again re-reads the source file.", name=part.name, exc=exc))

    def _note_part(self, part: Part, diag=None):
        """Позначка біля моделі в списку: чим саме цей файл поганий."""
        if diag is not None:
            part.open_edges = diag.open_edges
            part.bad_winding = diag.bad_winding
            part.inverted = diag.inverted_parts
        elif part.loaded:
            part.open_edges, part.bad_winding = geo.quick_facts(part.mesh)
            # Раніше тут стояв нуль — і після ремонту список показував
            # «вивернутих немає» просто тому, що їх ніхто не рахував.
            part.inverted = geo.inverted_part_count(part.mesh)[0]

    def _scene_line(self) -> str:
        """Рядок під списком: що саме піде в кейс.

        Зі списку з галочками не видно результату: коли моделей кілька,
        людина має знати, що в кейс поїде їхнє обʼєднання, і скільки це
        разом трикутників.
        """
        live = self.enabled_parts()
        if not live:
            return tr("the scene is empty")
        if self.mesh is None:
            return ""
        # Гвинти-диски в сітку не йдуть — і в цьому рядку їх не рахуємо,
        # а називаємо окремо.
        disks = [p for p in live if p.role == fc.DISK_ROLE]
        meshed = [p for p in live if p.role != fc.DISK_ROLE]
        tail = (tr("; actuator disks: {n_disks} (not in the mesh)", n_disks=len(disks))
                if disks else "")
        if not meshed:
            return tr("nothing goes into the mesh{tail}", tail=tail)
        tris = phys.fmt_cells(sum(p.tris for p in meshed))
        if len(meshed) == 1:
            return tr("the case gets {name}: {tris} triangles{tail}", name=meshed[0].name, tris=tris, tail=tail)
        return (tr("the case gets a union of {n_meshed} models: {tris} "
                   "triangles{tail}", n_meshed=len(meshed), tris=tris, tail=tail))

    def _part_rows(self) -> list[dict]:
        rows = []
        for part in self.parts:
            if part.loaded:
                info = (tr("{tris} tri. · {span}{v}", tris=phys.fmt_cells(part.tris), span=phys.fmt_len(part.span), v="  (wrap)" if part.wrapped else ""))
                bad = []
                if part.open_edges:
                    bad.append(tr("{open_edges} open edges", open_edges=part.open_edges))
                if part.bad_winding:
                    bad.append(tr("{bad_winding} inconsistent edges", bad_winding=part.bad_winding))
                if part.inverted:
                    bad.append(tr("{inverted} inside-out parts", inverted=part.inverted))
                tip = part.path + NEWLINE + ("; ".join(bad) if bad
                                             else tr("no obvious defects"))
            else:
                info = tr("not read")
                tip = tr("{path}{NEWLINE}checkbox — read and add to the scene", path=part.path, NEWLINE=NEWLINE)
            # `key` — ПОВНИЙ ШЛЯХ, і це не те саме, що `name`. Імена тут
            # базові, тож два різні файли легко звуться однаково
            # (wing.stl у двох теках). Панель тримає за цим ключем
            # виділення при перезаповненні списку, і на імені воно
            # перескакувало б на однойменний чужий рядок.
            rows.append(dict(name=part.name, key=part.path, info=info,
                             enabled=part.enabled, level=part.level, tip=tip))
        return rows

    def rebuild_geometry(self, precomputed=None, reset=False, moved=None):
        """Збирає сцену з увімкнених моделей і перемальовує все, що з неї живе."""
        live = self.enabled_parts()
        if not live:
            self.mesh = self.mesh_original = self.mesh_display = None
            self._diag_mesh = None
            self._show_mesh = None
            self.viewport.show_ghosts([])
            self.diag = None
            self.hole_edges = None
            self.viewport.clear_problems()
            self.viewport.show_body(None)
            self.panel_geo.show_diagnosis([], [])
            self.panel_geo.show_parts(self._part_rows(), self._scene_line())
            self.cfg.bodies = []
            self.cfg.engines = []
            self.cfg.disks = []
            self._show_split()
            return
        # СЦЕНА ЗБИРАЄТЬСЯ З ДЕТАЛЕЙ У ПОЗІ, а не з базових. Поза — це
        # відхилення керованих поверхонь під поточне положення стіків;
        # для деталі без завіси `posed` віддає рівно її ж `mesh`, тож
        # доки керування ніхто не налаштував, сцена та сама, що й була.
        posed = [p.posed(self.channels) for p in live]
        if len(posed) == 1:
            merged = posed[0]
        else:
            # merge_points=False: різні тіла не мають зшиватися між собою
            # тільки через те, що десь торкаються вершинами.
            merged = posed[0].merge(posed[1:], merge_points=False)
        self.mesh_original = merged
        self.mesh = merged
        # ДІРКИ ШУКАЄМО ЛИШЕ В СТІНКАХ. Кришка чи портал — відкрита
        # поверхня за побудовою: її край діагностика сцени рахувала б
        # діркою й кричала «протече» на кожному двигуні. Показ і габарит
        # лишаються з усіма поверхнями.
        #
        # Гвинт-диск — теж: від нього в сітку не йде нічого, і показується
        # він напівпрозорим (Q20), окремо від решти.
        special = any(p.role in fc.PORT_ROLES or p.role == fc.DISK_ROLE
                      for p in live)
        walls = [m for p, m in zip(live, posed)
                 if p.role not in fc.PORT_ROLES and p.role != fc.DISK_ROLE]
        if special and not walls:
            # Сцена з самих порталів (стисливий режим це дозволяє): стінок
            # немає зовсім. Тоді діагностика дістає замкнену «плитку»
            # кожної поверхні — у неї немає краю, тож немає й хибного
            # «дірки 60 — протече», а габарит той самий.
            walls = [geo.portal_slab(m, 1e-3 * max(m.length, 1e-9))
                     for m in posed]
        if special:
            self._diag_mesh = (walls[0] if len(walls) == 1 else
                               walls[0].merge(walls[1:], merge_points=False))
            precomputed = None
        else:
            self._diag_mesh = None
        solid = [m for p, m in zip(live, posed) if p.role != fc.DISK_ROLE]
        ghosts = [m for p, m in zip(live, posed) if p.role == fc.DISK_ROLE]
        self._show_mesh = (None if not ghosts or not solid else
                           solid[0] if len(solid) == 1 else
                           solid[0].merge(solid[1:], merge_points=False))
        self._sync_bodies(live)
        self._show_split()
        self.rediagnose(precomputed, moved=moved)
        self.viewport.show_body(self.mesh_display, reset=reset)
        self.viewport.show_ghosts(ghosts)
        self.panel_geo.show_parts(self._part_rows(), self._scene_line())

    def _sync_bodies(self, live: list[Part]) -> None:
        """Перезбирає список патчів під поточну сцену.

        Саме перезбирає, а не доповнює: моделі додають, прибирають і
        перейменовують, і список, який лише росте, швидко почав би посилатися
        на те, чого в сцені вже немає — а це патч без жодної грані, на якому
        падає вже snappyHexMesh.
        """
        if not self.cfg.split_patches or len(live) < 2:
            # Одна модель — один патч, і хай він зветься так, як звався:
            # інакше кейси, зроблені раніше, отримали б іншу границю.
            self.cfg.bodies = []
            self._sync_rotors([])
            self.cfg.engines = []
            self.cfg.disks = []
            return
        taken: set[str] = set()
        bodies = []
        for part in live:
            name = fc.patch_name_from(part.name, frozenset(taken))
            taken.add(name)
            # Габарит беремо з ПОЗИ: відхилена поверхня займає інше
            # місце, і рівень згущення має рахуватися по тому, що справді
            # піде в сітку.
            posed = part.posed(self.channels) or part.mesh
            b = posed.bounds
            span = min(b[1] - b[0], b[3] - b[2], b[5] - b[4])
            # Площа потрібна оцінці сітки: лише знаючи її, вона може
            # порахувати частину з власним рівнем за ЇЇ рівнем, а не за
            # спільним. Беремо з пози з тієї ж причини, що й габарит.
            try:
                area = float(posed.area)
            except Exception:
                area = 0.0
            port = part.role in fc.PORT_ROLES
            bodies.append(fc.BodyPatch(name=name, stl_file=f"{name}.stl",
                                       span=float(span), area=area,
                                       role=part.role,
                                       rpm=part.live_rpm(self.channels),
                                       level=part.refine,
                                       engine=part.engine if port else ""))
        self.cfg.bodies = bodies
        had = bool(self.cfg.engines or self.cfg.disks)
        self._sync_rotors(list(zip(live, bodies)))
        self._sync_engines(list(zip(live, bodies)))
        self._sync_disks(list(zip(live, bodies)))
        self._full_throttle_once(had)

    def _sync_disks(self, pairs) -> None:
        """Гвинти-диски з деталей у ролі «disk», щоразу з геометрії.

        Центр і радіус — тим самим способом, що й обертова зона гвинта
        (`rotor_zone_from_points`): модель могли посунути чи замінити, і
        памʼятати координати означало б дути там, де гвинта вже немає.
        Вісь — та, яку назвала людина: гвинт габаритом не відрізнити від
        пластини. Бік закрутки — знак обертів.
        """
        disks, refused = [], []
        axes = {"x": (1.0, 0.0, 0.0), "y": (0.0, 1.0, 0.0),
                "z": (0.0, 0.0, 1.0)}
        for part, body in pairs:
            if body.role != fc.DISK_ROLE:
                continue
            mesh = part.posed(self.channels) or part.mesh
            z = fc.rotor_zone_from_points(body.name, mesh.points, 1.0,
                                          part.rotor_axis, margin=1.0,
                                          allow_flat=True)
            if z is None:
                refused.append(part.name)
                continue
            radius = z.radius
            src = part.disk or fc.ActuatorDisk()
            hub = src.hub if src.hub > 0 else fc.DISK_HUB_FRACTION * radius
            disks.append(dataclasses.replace(
                src, name=body.name, centre=tuple(z.centre),
                axis=axes.get(part.rotor_axis, (1.0, 0.0, 0.0)),
                radius=radius, hub=min(hub, 0.9 * radius),
                spin=1 if part.rpm >= 0 else -1,
                throttle=float(self.channels.get("throttle", 0.0))))
        self.cfg.disks = disks
        if refused:
            self.log.push(
                tr("These models are marked as actuator disks, but their "
                   "shape is not like a disk (they are not thin along "
                   "the selected axis): {refused}. Try another rotation "
                   "axis.", refused=", ".join(refused)))

    def _full_throttle_once(self, had: bool) -> None:
        """Q15: перший двигун чи диск ставить газ на повний.

        Газ типово повний, але людина могла прибрати його в нуль раніше
        (або так збережено старий проєкт). Тоді нова сцена з тягою мала б
        її рівно нуль — а людина дізналася б про це лише з результатів.
        """
        if had or not (self.cfg.engines or self.cfg.disks):
            return
        if float(self.channels.get("throttle", 0.0)) > 0.0:
            return
        self.channels["throttle"] = 1.0
        self.panel_tx.set_quiet("throttle", 1.0)
        for x in list(self.cfg.engines) + list(self.cfg.disks):
            x.throttle = 1.0
        self.log.push(tr("Throttle set to full: the scene now has thrust "
                      "(an engine or an actuator disk), and at zero "
                      "throttle there would be none."))

    def _sync_engines(self, pairs) -> None:
        """Двигуни з кришок, щоразу наново — як і обертові зони.

        Площа сопла береться з ГЕОМЕТРІЇ кришки, а не памʼятається: кришку
        могли перемасштабувати чи замінити, і стара площа дала б іншу
        швидкість струменя за тієї самої тяги — мовчки.

        Параметри, які задає людина, живуть на соплі (`Part.jet`).
        """
        groups: dict[str, list] = {}
        for part, body in pairs:
            if body.role in fc.PORT_ROLES:
                groups.setdefault(body.engine or "engine1", []).append(
                    (part, body))
        engines = []
        for name, items in groups.items():
            nozzles = [(p, b) for p, b in items if b.role == "nozzle"]
            intakes = [(p, b) for p, b in items if b.role == "intake"]
            src = next((p.jet for p, _ in nozzles if p.jet is not None),
                       None) or fc.Engine()
            n_area, n_dh = 0.0, 0.0
            n_dir, n_proj = (1.0, 0.0, 0.0), 0.0
            if nozzles:
                p0 = nozzles[0][0]
                m0 = p0.posed(self.channels) or p0.mesh
                a, per = geo.cap_metrics(m0)
                n_area, n_dh = a, (4.0 * a / per if per > 0 else 0.0)
                # Напрямок струменя — з нормалей STL, як вони є (Q27).
                s = geo.area_vector(m0)
                n_proj = float(np.linalg.norm(s))
                n_dir = (tuple(float(x) / n_proj for x in s) if n_proj > 0
                         else (0.0, 0.0, 0.0))
            i_areas = tuple(
                geo.cap_metrics(p.posed(self.channels) or p.mesh)[0]
                for p, _ in intakes)
            engines.append(dataclasses.replace(
                src, name=name,
                nozzle=nozzles[0][1].name if nozzles else "",
                intakes=tuple(b.name for _, b in intakes),
                nozzle_area=n_area, nozzle_dh=n_dh, intake_areas=i_areas,
                nozzle_normal=n_dir, nozzle_proj=n_proj,
                throttle=float(self.channels.get("throttle", 0.0))))
        self.cfg.engines = engines

    def _sync_rotors(self, pairs) -> None:
        """Обертові зони з тих частин, які позначені гвинтами.

        Зона будується з ГЕОМЕТРІЇ частини щоразу наново, а не памʼятається:
        модель могли посунути, повернути чи перечитати в інших одиницях, і
        зона, що лишилася на старому місці, крутила б порожнє повітря — без
        жодної помилки й без тяги. Це головна пастка MRF, і памʼятати тут
        координати означало б самому її й підготувати.

        Ручну зону з вкладки «Потік» не чіпаємо, поки жодна частина не
        позначена гвинтом: інакше роль, якої ніхто не давав, мовчки
        стирала б те, що людина задала руками.
        """
        rotor_pairs = [(p, b) for p, b in pairs if p.role == "rotor"]
        if not rotor_pairs:
            if any(z.from_part for z in self.cfg.rotating):
                self.cfg.rotating = [z for z in self.cfg.rotating
                                     if not z.from_part]
            self.cfg.sync_zone_boxes(level=self.zone_box_level)
            return
        zones, refused = [], []
        for part, body in rotor_pairs:
            # Оберти беруть газ із пульта: гвинт на нульовому газі
            # стоїть, і зона має це знати, інакше вона крутитиме повітря
            # там, де двигун не працює.
            z = fc.rotor_zone_from_points(body.name, part.mesh.points,
                                          part.live_rpm(self.channels),
                                          part.rotor_axis)
            if z is None:
                refused.append(part.name)
                continue
            z.from_part = True
            # НЕРУХОМІ ПАТЧІ В ЗОНІ НАЗИВАЄМО САМІ, і це не косметика.
            #
            # Зона гвинта майже завжди накриває мотогондолу: вона стоїть
            # одразу за диском. Якщо не сказати MRF, що ця поверхня
            # НЕРУХОМА, він крутитиме повітря впритул до стінки — а це
            # нескінченний зсув, і розвʼязувач падає з sigFpe на першій же
            # ітерації енергії.
            #
            # Саме так і сталося на справжньому апараті користувача:
            # кожна з чотирьох зон ковтала по 29-37 тисяч граней корпусу,
            # а `nonRotatingPatches` лишався порожнім.
            #
            # Нерухоме тут — УСЕ, крім лопатей самої цієї зони. Інші
            # гвинти теж: вони обертаються у СВОЇХ зонах, а в цій мають
            # стояти.
            z.non_rotating = tuple(b.name for _, b in pairs
                                   if b.name != body.name)
            zones.append(z)
        self.cfg.rotating = zones
        # Коробки згущення їдуть слідом за зонами. Поверхневе згущення
        # обʼєму не розвʼязує: воно спадає сходинками від лопаті, а
        # закрутка й приріст швидкості живуть саме в обʼємі диска.
        self.cfg.sync_zone_boxes(level=self.zone_box_level)
        if refused:
            self.log.push(
                tr("These models are marked as propellers, but their "
                   "shape is not like a disk, and no zones were made for "
                   "them: {refused}. A disk is thin along the axis and "
                   "ROUND in the two other directions; a wing does not "
                   "have such a shape.", refused=", ".join(refused)))

    def _show_split(self) -> None:
        # Усі поверхні, а не лише стінки: інакше мотор із двома кришками
        # підписувався б «у сцені одна модель».
        self.panel_geo.show_split(self.cfg.surface_patches if self.cfg.bodies
                                  else [], self.cfg.guard_bodies())

    @staticmethod
    def _merged_bounds(parts: list[Part]):
        lo = [min(p.mesh.bounds[i] for p in parts) for i in (0, 2, 4)]
        hi = [max(p.mesh.bounds[i] for p in parts) for i in (1, 3, 5)]
        return (lo[0], hi[0], lo[1], hi[1], lo[2], hi[2])

    def part_toggled(self, index: int, on: bool):
        if not (0 <= index < len(self.parts)):
            return
        part = self.parts[index]
        if (self._pmesh.active and bool(on) != bool(part.enabled and part.loaded)
                and not self.mesh_guard(tr("part “{name}”", name=part.name))):
            # Галочку вертаємо: деталь у сцені лишається, як була.
            self.panel_geo.show_parts(self._part_rows(), self._scene_line())
            return
        if on and not part.loaded and part.cache and os.path.isfile(part.cache):
            # З КЕШУ: такою деталь і була в сцені — з ремонтом і в метрах.
            # Читаємо поза потоком вікна, але чекаємо тут: це одна деталь,
            # і розбивати її на потоки з відкладеним доробленням нема чого.
            try:
                mesh = self._off_thread(
                    tr("Restoring “{name}” to how it was…", name=part.name),
                    lambda: geo.load_surface(part.cache))
            except Exception as exc:                   # noqa: BLE001
                self.log.push(tr("Cache of “{name}” could not be read "
                                 "({exc}) — reading the source file.", name=part.name, exc=exc))
                part.cache = ""
                return self.part_toggled(index, on)
            part.mesh, part.enabled = mesh, True
            part._posed, part._posed_key = None, None
            self._note_part(part)
            self.rebuild_geometry()
            self.auto_reference()
            self.refresh_all()
            return
        if on and not part.loaded:
            if not os.path.isfile(part.path):
                QtWidgets.QMessageBox.warning(
                    self, tr("File disappeared"),
                    tr("{path}{NEWLINE}{NEWLINE}It was renamed, moved or "
                       "deleted. The row stays in the list — remove it "
                       "or put the file back.", path=part.path, NEWLINE=NEWLINE))
                self.panel_geo.show_parts(self._part_rows(), self._scene_line())
                return
            if self._load_thread is not None:
                # Інший файл ще читається: позначка без читання лишила б
                # список і сцену в різних станах.
                self.statusBar().showMessage(
                    tr("Wait: the previous geometry is still being read."), 6000)
                self.panel_geo.show_parts(self._part_rows(), self._scene_line())
                return
            part.enabled = True
            # Прибита деталь (свої одиниці, з кейсу чи сесії) читається з
            # тим самим множником, а не з поля: інакше зняв і поставив
            # галочку — і деталь поїхала в тисячу разів.
            self.load_stl(part.path, add=True,
                          factor=part.factor if part.pinned else None)
            return
        part.enabled = on
        # Камеру НЕ чіпаємо: людина вибирає, що лишити в сцені, а не куди
        # дивитися. Перша версія летіла до моделі на кожну галочку, і
        # вʼюпорт «скидався» посеред роботи. Підлетіти — «До моделі».
        self.rebuild_geometry()
        self.auto_reference()
        self.refresh_all()

    def part_removed(self, index: int):
        if not (0 <= index < len(self.parts)):
            return
        part = self.parts.pop(index)
        if part.enabled and part.loaded:
            self.rebuild_geometry()
            self.auto_reference()
            self.refresh_all()
        else:
            self.panel_geo.show_parts(self._part_rows(), self._scene_line())

    def part_solo(self, index: int):
        """Подвійний клік: лишити в сцені саму цю модель."""
        if not (0 <= index < len(self.parts)):
            return
        if not self.parts[index].loaded:
            self.part_toggled(index, True)
            return
        for i, part in enumerate(self.parts):
            part.enabled = (i == index)
        # Без перельоту камери — див. part_toggled.
        self.rebuild_geometry()
        self.auto_reference()
        self.refresh_all()

    def apply_units(self, precomputed=None):
        """Переводить у метри КОЖНУ модель окремо й перезбирає сцену.

        Спільного множника тут бути не може: у сцені лежать різні файли, і
        антена в міліметрах поруч із фюзеляжем у метрах від одного множника
        стане або нитяною, або завбільшки з крило. Кожна модель памʼятає свій.
        """
        changed = self._scale_parts()
        self.rebuild_geometry(None if changed else precomputed)

    def _scale_parts(self) -> bool:
        """Множник одиниць кожній деталі — без перебудови сцени."""
        changed = False
        for part in self.parts:
            if not part.loaded or part.factor <= 0 or part.pinned:
                continue
            target = self.panel_geo.unit_factor(part.auto)
            if abs(target - part.factor) > 1e-15:
                part.mesh = geo.apply_scale(part.mesh, target / part.factor)
                part.factor = target
                changed = True
        return changed

    def _off_thread(self, text: str, job, size: int = 0):
        """Довга робота поза потоком вікна — але з результатом тут і зараз.

        Для того, хто кличе, це звичайний виклик `job()`: повертає те саме
        й кидає той самий виняток. Різниця в тому, що поки `job` рахує в
        окремому потоці, вікно крутить свій цикл подій і лишається живим.
        Перебудова сцени має понад десяток викликачів, і всі вони чекають
        готових `self.diag` і `self.mesh_display` одразу після виклику;
        переписати кожен на зворотний виклик означало б десяток нових місць,
        де стан сцени розходиться з картинкою.

        Від того, що вікно живе, захищає модальне вікно поступу: клацати по
        сцені, поки вона перебудовується, не можна — так само, як під час
        читання файлу.

        На місці, без потоку, рахуємо дрібне (`size` < BIG_MESH) і все, що
        прийшло вже зсередини іншої такої роботи: вкладений цикл подій
        може викликати перебудову ще раз (таймер, сигнал), і другий потік
        поверх першого на ту саму сцену дав би гонитву.

        Потік вікна впізнаємо через `threading`, а не через `self.thread()`:
        у вікні `self.thread` — це потік прогону, і він затуляє метод QObject.
        """
        if (size < BIG_MESH or self._busy_depth
                or threading.current_thread() is not threading.main_thread()):
            return job()
        self._busy_depth += 1
        loop = QtCore.QEventLoop(self)
        got = _BusyReceiver(loop)
        worker = BusyWorker(job)
        thread = QtCore.QThread(self)
        thread.setObjectName("busy")
        worker.moveToThread(thread)
        thread.started.connect(worker.run)
        worker.done.connect(got.take)
        box = QtWidgets.QProgressDialog(text, "", 0, 0, self)
        box.setWindowTitle("OpenFOAM Studio")
        box.setCancelButton(None)
        box.setWindowModality(QtCore.Qt.WindowModal)
        box.setMinimumDuration(400)
        thread.start()
        try:
            loop.exec()
        finally:
            _drop_progress(box)
            thread.quit()
            thread.wait()
            self._busy_depth -= 1
        if got.exc is not None:
            raise got.exc
        return got.result

    def rediagnose(self, precomputed=None, moved=None):
        if self.mesh is None:
            return
        mesh = self.mesh
        # Без кришок двигунів це та сама сцена; з ними — лише стінки
        # (див. rebuild_geometry).
        probe = getattr(self, "_diag_mesh", None) or mesh
        # Поворот і зсув не міняють ні нормалей відносно тіла, ні того, де в
        # ньому дірки: рухаємо готове, а не рахуємо наново.
        fresh = moved is None or self.mesh_display is None

        # Підсвітку рахуємо тут, а не в refresh_all: сітка змінюється
        # рідко, а refresh_all смикається на кожен рух спінбокса.
        #
        # `job` не чіпає ні `self`, ні віджетів — лише сітку, яку дістав
        # аргументом: він може піти в інший потік (див. _off_thread).
        shown = getattr(self, "_show_mesh", None) or mesh
        # КЕШ: та сама геометрія — та сама відповідь. Перебудову сцени
        # кличуть звідусіль (ролі, одиниці, відкриття проєкту), і без кешу
        # одна сцена перевірялася по кілька разів поспіль.
        key = (_mesh_key(probe), _mesh_key(shown)) if precomputed is None else None
        hit = getattr(self, "_diag_cache", None)

        def job():
            if key is not None and hit is not None and hit[0] == key:
                d, disp, lay = hit[1]
                if not fresh:
                    return d, None, None
                if disp is not None:
                    return d, disp, lay
            elif precomputed is not None:
                d = precomputed
            else:
                d = geo.diagnose(probe)
            if not fresh:
                return d, None, None
            return d, geo.display_mesh(shown), self._problem_layers(probe, d)

        QtWidgets.QApplication.setOverrideCursor(QtCore.Qt.WaitCursor)
        try:
            self.diag, display, layers = self._off_thread(
                tr("Checking the geometry and preparing it for display…"), job,
                mesh.n_cells)
            if key is not None:
                self._diag_cache = (key, (self.diag, display if fresh else None,
                                          layers if fresh else None))
            if fresh:
                self.mesh_display = display
                self._show_problem_layers(layers)
            else:
                self.mesh_display = moved(self.mesh_display)
                self._move_layers(moved)
        finally:
            QtWidgets.QApplication.restoreOverrideCursor()
        d = self.diag
        # Габарит — з УСІЄЇ сцени: портал за мотором теж має влізти в домен.
        b = mesh.bounds if probe is not mesh else d.bounds
        self.cfg.bbox_min = (b[0], b[2], b[4])
        self.cfg.bbox_max = (b[1], b[3], b[5])
        self.cfg.surface_area = d.area

    def next_problem(self):
        """Веде камеру по проблемних місцях, найбільші спершу."""
        if self.mesh is None or self.diag is None:
            return
        if not self._spots:
            QtWidgets.QApplication.setOverrideCursor(QtCore.Qt.WaitCursor)
            try:
                self._spots = geo.problem_spots(
                    getattr(self, "_diag_mesh", None) or self.mesh, self.diag)
            finally:
                QtWidgets.QApplication.restoreOverrideCursor()
            self._spot_at = -1
        if not self._spots:
            self.statusBar().showMessage(
                tr("No problem spots found — nothing to show."), 6000)
            return
        self._spot_at = (self._spot_at + 1) % len(self._spots)
        spot = self._spots[self._spot_at]
        radius = spot["size"] if spot["size"] > 0 else self.diag.diagonal * 0.01
        centre = np.asarray(spot["centre"], dtype=float)
        self.viewport.look_at(spot["centre"], radius,
                              outward=centre - np.asarray(self.mesh.center))
        self.viewport.mark_spot(spot["centre"], radius)
        size = (f", {phys.fmt_len(spot['size'])}" if spot["size"] > 0 else "")
        self.statusBar().showMessage(
            tr("{capitalize} {v} of {n_spots}{size}.  The “Model” button "
               "returns to the full view.", capitalize=tr(spot['what']).map(lambda s: s[:1].upper() + s[1:]), v=self._spot_at + 1, n_spots=len(self._spots), size=size), 12000)

    def _move_layers(self, move):
        """Повертає/зсуває вже пораховану підсвітку замість перерахунку."""
        for key, data in list(self.layers.items()):
            if data is None:
                continue
            self.layers[key] = move(data)
        self.hole_edges = self.layers.get("holes")
        # Місця ті самі, але координати поїхали разом із тілом.
        self._spots, self._spot_at = [], -1
        d = self.diag
        counts = {"holes": d.open_edges, "flip": d.flipped_faces,
                  "nm": d.non_manifold_edges, "degen": d.degenerate}
        for key, data in self.layers.items():
            self.viewport.show_problem(key, data, counts.get(key, 0))

    @staticmethod
    def _problem_layers(mesh, d) -> dict:
        """Шари підсвітки — сама геометрія, без жодного віджета.

        Окремо від показу, бо на великій сітці це секунди роботи, і вона
        йде в робочому потоці (див. rediagnose), а показ — лише тут.
        """
        return {
            "holes": (geo.open_edges_polydata(mesh)
                      if d.open_edges else None),
            "flip": (geo.inverted_polydata(mesh, d.inverted_cells)
                     if d.flipped_faces else None),
            "nm": (geo.non_manifold_polydata(mesh)
                   if d.non_manifold_edges else None),
            "degen": (geo.degenerate_points(mesh) if d.degenerate else None),
        }

    def _show_problem_layers(self, layers: dict | None = None):
        """Підсвічує в 3D усе, що доведеться лагодити, і пише це на чекбоксах.

        Числа в таблиці діагностики кажуть, ЩО не так, але не кажуть ДЕ.
        «38 дірок» на моделі літака — це зовсім різні новини залежно від
        того, сидять вони в декоративному гравіюванні чи в носку крила.

        `layers` — уже пораховані шари; без них рахуємо тут.
        """
        d, mesh = self.diag, self.mesh
        if d is None or mesh is None:
            self.viewport.clear_problems()
            return
        self.layers = (layers if layers is not None
                       else self._problem_layers(mesh, d))
        self.hole_edges = self.layers["holes"]
        self._spots, self._spot_at = [], -1
        self.viewport.btn_spot.setEnabled(
            bool(d.open_edges or d.flipped_faces or d.degenerate))
        counts = {"holes": d.open_edges, "flip": d.flipped_faces,
                  "nm": d.non_manifold_edges, "degen": d.degenerate}
        for key, data in self.layers.items():
            self.viewport.show_problem(key, data, counts[key])

    def transform(self, key: str, value: float):
        """Повертає й зсуває ВСЮ ЗБІРКУ, а не вибрану деталь.

        ПРИКЛАДАЄМО ДО ВСІХ ЗАВАНТАЖЕНИХ, а не лише до видимих у сцені.
        Деталі приходять із CAD уже складеними, і взаємне розташування в
        них — це дані, а не випадковість. Якщо повернути тільки видимі,
        то знята галочка означала б, що деталь лишилася в старій системі
        координат: поставив галочку назад — а вона лежить упоперек.
        Мовчки, бо жодної помилки в цьому немає.

        А от ЦЕНТР беремо з видимих: цілимося тим, що бачимо.
        """
        live = self.enabled_parts()
        if not live or self.mesh is None:
            return
        every = [p for p in self.parts if p.loaded]
        moved = None
        if key in ("x", "y", "z"):
            # Крутимо кожну модель навколо СПІЛЬНОГО центра збірки: навколо
            # власних центрів вони роз'їхалися б хто куди.
            pivot = self.mesh.center
            for part in every:
                part.mesh = geo.rotate(part.mesh, key, value, pivot=pivot)
            moved = lambda m: geo.rotate(m, key, value, pivot=pivot)  # noqa: E731
        elif key == "centre":
            c = self.mesh.center
            shift = (-c[0], -c[1], -c[2])
            for part in every:
                part.mesh = geo.translate(part.mesh, shift)
            moved = lambda m: geo.translate(m, shift)  # noqa: E731
        elif key == "ground":
            shift = (0.0, 0.0, -self.mesh.bounds[4])
            for part in every:
                part.mesh = geo.translate(part.mesh, shift)
            moved = lambda m: geo.translate(m, shift)  # noqa: E731

        # Діагностика після повороту — це та сама діагностика з переставленими
        # осями. Рахувати її наново на 775 тис. трикутників означало б чотири
        # секунди замерзлого вікна на кожен натиск.
        fresh = None
        if self.diag is not None:
            live_bounds = self._merged_bounds(live)
            if key in ("x", "y", "z"):
                fresh = geo.reoriented(self.diag, key, value, live_bounds)
            else:
                fresh = geo.reoriented(self.diag, "z", 0.0, live_bounds)
        self.rebuild_geometry(precomputed=fresh, reset=True,
                              moved=moved if fresh is not None else None)
        # Після повороту габарит інший, тому Lref і Aref, узяті з попередньої
        # орієнтації, стають безглуздими. У реальному кейсі це давало
        # lRef = розмах замість довжини фюзеляжу: Re й CmPitch рахувалися по
        # чужій довжині, і ніщо про це не сигналізувало.
        self.auto_reference()
        self.refresh_all()
        if self.diag is not None:
            self.statusBar().showMessage(
                tr("After the rotation, Lref and Aref are calculated "
                   "again: Lref {lref}, Aref {aref:.5g} m²", lref=phys.fmt_len(self.cfg.flow.l_ref),
                   aref=self.cfg.flow.a_ref),
                10000)

    def repair(self, key: str):
        """Ремонт іде ПО КОЖНІЙ моделі окремо й ПОЗА потоком вікна.

        Зшивати збірку як одне ціле не можна: зварювання вершин злило б
        сусідні деталі в спільну оболонку, а проріднення перетягувало б
        трикутники з крила на антену.

        Усе, що треба спитати, питається ТУТ, до запуску потоку: діалог із
        чужого потоку в Qt — це падіння.
        """
        live = self.enabled_parts()
        if self.mesh is None or not live:
            return
        if self._fix_thread is not None:
            self.statusBar().showMessage(tr("Wait: the repair is still running."), 5000)
            return
        try:
            job, mode = self._repair_job(key, live)
        except Exception as exc:
            QtWidgets.QMessageBox.critical(self, tr("Repair failed"), str(exc))
            return
        if job is None:
            return
        self._start_repair(key, live, job, mode)

    def _repair_job(self, key: str, live: list[Part]):
        """(робота, режим) або (None, "") якщо людина передумала.

        Робота виконується в іншому потоці, тому всередині неї не можна ні
        питати, ні малювати — лише рахувати.
        """
        if key == "reset":
            missing = [p.name for p in live if not os.path.isfile(p.path)]
            if missing:
                raise FileNotFoundError(
                    tr("not found on disk: {missing}", missing=", ".join(missing)))
            plan = [(p.path, p.factor) for p in live]

            for part in live:
                part.wrapped = False

            def job(meshes, say):
                out = []
                for path, factor in plan:
                    say(tr("re-reading {name}", name=Path(path).name))
                    out.append(geo.apply_scale(geo.load_surface(path), factor))
                return out
            return job, "parts"

        if key in fs_repair.KEYS:
            # meshfix, alpha wrap, Windows, volume: the same code as in
            # MeshFix Studio (fs_repair.repair_meshes). Do not repair here.
            # The button is disabled without the tool, but a session from
            # another computer can still come here.
            if not fs_repair.available(key):
                QtWidgets.QMessageBox.information(
                    self, tr("Repair"), fs_repair.why_not(key))
                return None, ""
            mode = fs_repair.mode(key)
            # "wrap": one shell from the whole scene, thus the job gets the
            # merged mesh of the window.
            merged = self.mesh if mode == "wrap" else None

            def job(meshes, say, key=key, merged=merged):
                return fs_repair.repair_meshes(key, meshes, say, merged=merged)
            return job, mode

        if key == "subdivide":
            if self.mesh.n_cells > 400_000:
                QtWidgets.QMessageBox.warning(
                    self, tr("Too many"),
                    tr("More than 400k triangles — refining four times will "
                    "make the file unmanageable."))
                return None, ""

            def job(meshes, say):
                out = []
                for i, mesh in enumerate(meshes, 1):
                    say(tr("refining, model {i} of {n_meshes}", i=i, n_meshes=len(meshes)))
                    out.append(geo.subdivide(mesh, 1))
                return out
            return job, "parts"

        if key == "decimate":
            total = self.mesh.n_cells
            n, ok = QtWidgets.QInputDialog.getInt(
                self, tr("Decimate"), tr("How many triangles to keep:"),
                min(60000, total), 100, total)
            if not ok:
                return None, ""
            return self._decimate_job(n, total), "parts"

        return None, ""

    def _decimate_job(self, target: int, total: int):
        """Ділить бажану кількість трикутників між моделями за їхньою вагою."""
        def job(meshes, say):
            out = []
            for i, mesh in enumerate(meshes, 1):
                say(tr("decimating, model {i} of {n_meshes}", i=i, n_meshes=len(meshes)))
                share = mesh.n_cells / total if total else 1.0
                out.append(geo.decimate(mesh, max(100, int(target * share))))
            return out
        return job

    # --- виконання ремонту ----------------------------------------------

    def _start_repair(self, key: str, live: list[Part], job, mode: str):
        # Знімок ДО ремонту: збій на третій моделі з пʼяти лишав би сцену
        # напівполагодженою, без жодного способу зрозуміти, що встигло
        # змінитися.
        self._fix_snapshot = [(p, p.mesh, p.enabled) for p in live]
        self._fix_live = live
        self._fix_key = key
        self._fix_mode = mode
        self._fix_before = self.diag

        self._fix_progress = QtWidgets.QProgressDialog(
            "Repairing geometry…", "", 0, 0, self)
        self._fix_progress.setWindowTitle("OpenFOAM Studio")
        self._fix_progress.setCancelButton(None)
        self._fix_progress.setWindowModality(QtCore.Qt.WindowModal)
        self._fix_progress.setMinimumDuration(300)

        # Поки ремонт читає ці сітки в іншому потоці, чіпати їх із вікна не
        # можна: віконце поступу модальне, але зʼявляється лише за 300 мс,
        # і цієї щілини досить, щоб натиснути ще щось.
        self.panel_geo.setEnabled(False)
        self._fixer = RepairWorker(job, [p.mesh for p in live])
        self._fix_thread = QtCore.QThread(self)
        self._fix_thread.setObjectName("fix")
        self._fixer.moveToThread(self._fix_thread)
        self._fix_thread.started.connect(self._fixer.run)
        self._fixer.step.connect(self._on_repair_step)
        self._fixer.ready.connect(self._on_repair_ready)
        self._fixer.failed.connect(self._on_repair_failed)
        self._fix_thread.start()

    def _end_repair(self):
        self.panel_geo.setEnabled(True)
        if self._fix_progress is not None:
            _drop_progress(self._fix_progress)
            self._fix_progress = None
        if self._fix_thread is not None:
            self._fix_thread.quit()
            self._fix_thread.wait(5000)
            self._fix_thread = None
        self._fixer = None

    def _undo_repair(self):
        for part, mesh, was_on in self._fix_snapshot:
            part.mesh, part.enabled = mesh, was_on

    @QtCore.Slot(str)
    def _on_repair_step(self, text: str):
        verdict = text.endswith(meshfix.VERDICT)
        text = text.rstrip(meshfix.VERDICT)
        if self._fix_progress is not None:
            self._fix_progress.setLabelText(text)
        # ВИРОК ЗАЛИШАЄТЬСЯ В ЖУРНАЛІ, а не лише блимає в діалозі.
        #
        # Доти весь вивід ремонту жив рівно стільки, скільки вікно
        # поступу, — тобто зникав саме тоді, коли його можна було читати.
        # Для кроків це байдуже («зшиваю вершини…»), а для чисел, якими
        # правка міряє себе, — ні: людина мусить мати змогу повернутися й
        # подивитися, чи стало краще.
        # The verdict is marked with meshfix.VERDICT, not found by its words:
        # the words are translated.
        if verdict:
            self.log.push(text)

    @QtCore.Slot(str)
    def _on_repair_failed(self, message: str):
        self._end_repair()
        self._undo_repair()
        QtWidgets.QMessageBox.critical(
            self, tr("Repair failed"),
            tr("{message}{NEWLINE}{NEWLINE}The geometry is restored to "
               "its state before the repair.", message=message, NEWLINE=NEWLINE))

    @QtCore.Slot(object)
    def _on_repair_ready(self, meshes):
        key, live, mode = self._fix_key, self._fix_live, self._fix_mode
        before = self._fix_before
        self._end_repair()

        if mode == "wrap":
            # Обгортка — це одне тіло замість усієї сцени: оболонка одна на
            # всіх (що воксельна, що alpha wrap), і розділити її назад на
            # моделі вже нічим.
            keeper = live[0]
            for part in live[1:]:
                part.enabled = False
                part.mesh = None
            keeper.mesh = meshes[0]
            keeper.wrapped = True
            self._wrap_from = len(live)
            # self.mesh тут — ще ДОобгортковий стан сцени: перезбирання
            # буде нижче. Саме з ним і треба порівнювати.
            wrapped, before_wrap = meshes[0], self.mesh
            self._wrap_dev = self._off_thread(
                tr("Measuring how far the wrap moved from the body…"),
                lambda: geo.surface_deviation(wrapped, before_wrap),
                wrapped.n_cells + before_wrap.n_cells)
        else:
            for part, mesh in zip(live, meshes):
                part.mesh = mesh

        # Тільки серед тих, що лишилися в сцені: обгортання навмисно зʼїдає
        # решту моделей в одну оболонку, і вважати їх «зіпсованими» не можна.
        empty = [p.name for p in live
                 if p.enabled and (p.mesh is None or p.mesh.n_cells == 0)]
        if empty:
            self._undo_repair()
            QtWidgets.QMessageBox.critical(
                self, tr("Repair failed"),
                tr("Empty surface after the repair: "
                   "{empty}{NEWLINE}{NEWLINE}The geometry was returned "
                   "to its state before the repair.", empty=", ".join(empty), NEWLINE=NEWLINE))
            return

        noted = [p for p in live if p.enabled and p.loaded]
        facts = self._off_thread(
            tr("Checking the repaired geometry…"),
            lambda: [(geo.quick_facts(p.mesh),
                      geo.inverted_part_count(p.mesh)[0]) for p in noted],
            sum(p.mesh.n_cells for p in noted))
        for part, ((edges, winding), inverted) in zip(noted, facts):
            part.open_edges, part.bad_winding = edges, winding
            part.inverted = inverted
        self.rebuild_geometry(reset=(mode == "wrap"))
        self.refresh_all()
        self._report_repair(key, before, self.diag, mode)

    def _report_repair(self, key, before, after, mode="parts"):
        """Каже, що саме змінилося. Ремонт часто робить менше, ніж очікують —
        мовчазна кнопка лишає користувача гадати, чи вона взагалі спрацювала."""
        if before is None or after is None:
            return
        deltas = []
        if before.n_triangles != after.n_triangles:
            deltas.append(tr("triangles {n_triangles} → {n_triangles2}", n_triangles=phys.fmt_cells(before.n_triangles), n_triangles2=phys.fmt_cells(after.n_triangles)))
        if before.open_edges != after.open_edges:
            deltas.append(tr("open edges {open_edges} → {open_edges2}", open_edges=before.open_edges, open_edges2=after.open_edges))
        if before.non_manifold_edges != after.non_manifold_edges:
            deltas.append(f"non-manifold {before.non_manifold_edges} → "
                          f"{after.non_manifold_edges}")
        if before.parts != after.parts:
            deltas.append(tr("parts {parts} → {parts2}", parts=before.parts, parts2=after.parts))

        if mode != "wrap" and before.volume > 0 and after.volume > 0:
            # Для обгортки це число оманливе: у вихідній збірці вивернуті
            # частини входили в суму зі знаком мінус, тому «обʼєм виріс на
            # 29%» означало б лише те, що їх розвернули.
            change = after.volume / before.volume - 1.0
            if abs(change) > 0.005:
                deltas.append(tr("volume {v:+.1f}%", v=change * 100))
        msg = fs_i18n.join(", ", deltas) if deltas else tr("nothing changed")
        if mode == "wrap":
            n = getattr(self, "_wrap_from", 1)
            self._wrap_from = 1
            msg = (tr("{n} models wrapped into one closed shell.  ", n=n) if n > 1
                   else tr("Wrap done.  ")) + msg
            if after.open_edges == 0:
                msg += tr(".  No more holes and non-manifold edges")
            med, p90, worst = getattr(self, "_wrap_dev", (0.0, 0.0, 0.0))
            if worst > 0:
                msg += (tr(".  The surface moved: median {med}, 90% "
                           "within {p90}, maximum {worst}", med=phys.fmt_len(med), p90=phys.fmt_len(p90), worst=phys.fmt_len(worst)))
        if key == "decimate" and after.open_edges > before.open_edges:
            # Квадричне проріднення рве тонкі місця й зони взаємного врізання.
            msg += (tr(".  There are more open edges: decimation tears thin "
                    "walls. If the holes are now larger than a cell, "
                    "decimate less"))
        if key == "winfix" and before.open_edges and after.open_edges == 0:
            msg += (tr(".  No more holes, and the model was not decimated — "
                    "unlike with wrapping"))
        if key in meshfix.METHODS and meshfix.last_report:
            msg += (".  " + tr(meshfix.METHODS[key][0]) + ": "
                    + meshfix.summary(meshfix.last_report))
        self.statusBar().showMessage(tr("Repair: {msg}", msg=msg), 12000)

    def set_rookie(self, on: bool):
        """Показує або ховає блоки пояснень біля всіх налаштувань."""
        panels.set_rookie(on)
        fset.set("rookie", bool(on))
        self.statusBar().showMessage(
            "Beginner mode is on: each setting now has an explanation of "
            "what it affects." if on
            else "Beginner mode is off. Tooltips still show on mouse hover.", 8000)

    def open_manual(self):
        path = fs_i18n.localized(APP_DIR / "NASTANOVA.md")
        if not path.is_file():
            QtWidgets.QMessageBox.information(
                self, tr("Guide not found"), tr("Expected the file {path}", path=path))
            return
        try:
            os.startfile(str(path))
        except OSError:
            QtGui.QDesktopServices.openUrl(
                QtCore.QUrl.fromLocalFile(str(path)))

    def open_install_guide(self):
        """«Help → Install and server guide»: docs/INSTALL.<lang>.md."""
        self._guide = fs_serverwiz.open_guide(self)

    def open_glossary(self) -> bool:
        """Словник термінів і формул — у браузері за замовчуванням.

        Саме в браузері, а не у вікні програми: там пошук по таблицях,
        темна тема й посилання на розділи, а QTextBrowser не тягне ні CSS
        змінних, ні скриптів. Шрифти підтягуються з Google Fonts; без
        мережі сторінка йде системними, читається так само.
        """
        path = fs_i18n.localized(APP_DIR / "SLOVNYK.html")
        if not path.is_file():
            QtWidgets.QMessageBox.information(
                self, tr("Glossary not found"), tr("Expected the file {path}", path=path))
            return False
        if not QtGui.QDesktopServices.openUrl(
                QtCore.QUrl.fromLocalFile(str(path))):
            try:
                os.startfile(str(path))
            except OSError as exc:
                QtWidgets.QMessageBox.warning(
                    self, tr("Could not open the dictionary"),
                    f"{path}{NEWLINE}{NEWLINE}{exc}")
                return False
        self.statusBar().showMessage(tr("Glossary: {path}", path=path), 6000)
        return True

    # --- помічник -------------------------------------------------------

    def compute_advice(self):
        if self.diag is None:
            QtWidgets.QMessageBox.information(
                self, tr("No geometry"),
                tr("Load an STL first — the assistant computes from it."))
            return
        QtWidgets.QApplication.setOverrideCursor(QtCore.Qt.WaitCursor)
        try:
            self._plan = adv.advise(
                self.cfg, self.diag, cores=self._backend_cores(),
                target_yplus=(None if self.cfg.mesh.yplus_auto
                              else self.panel_adv.yplus.value()),
                layer_fit=self._layers_fit, mem=self._mem_fit)
        finally:
            QtWidgets.QApplication.restoreOverrideCursor()
        self.panel_adv.show_plan(self._plan)

    def apply_advice(self):
        plan = getattr(self, "_plan", None)
        if plan is None:
            return
        if self._pmesh.active:
            import copy
            trial = copy.deepcopy(self.cfg)
            adv.apply_plan(trial, plan)
            if (fc.mesh_dicts(trial) != fc.mesh_dicts(self.cfg)
                    and not self.mesh_guard(tr("the Assistant changes"))):
                return
        adv.apply_plan(self.cfg, plan)
        self.reload_panels()
        self.refresh_all()
        self.statusBar().showMessage(
            tr("Applied {n_changes} changes. Compute again if you "
               "changed the speed or the geometry.", n_changes=len(plan.changes)), 10000)

    def reload_panels(self):
        """Переносить значення з конфігу назад у віджети — усі, а не вибрані.

        Раніше тут стояв список із півтора десятка полів, і цього вистачало,
        поки конфіг правив лише порадник: він міняє саме їх. Відкриття
        збереженої симуляції ставить питання інакше — у конфіг приходить
        геть усе, і будь-яке невідновлене поле означає, що вкладка показує
        одне, а кейс запишеться з іншим. Тому кожна панель відновлює себе
        сама (`load_cfg`), а тут лишається порядок виклику.
        """
        for panel in (self.panel_geo, self.panel_dom, self.panel_mesh,
                      self.panel_flow, self.panel_solve):
            panel.load_cfg()
        self.panel_run.ed_name.setText(self.cfg.name)

    def suggest_decimation(self):
        """Пропонує проріднити STL, який набагато дрібніший за потрібну сітку.

        Зайві трикутники нічого не дають: snappy однаково бачить поверхню з
        точністю до розміру комірки. Зате вони роздувають октодерево пошуку,
        сповільнюють кожну операцію в програмі й додають non-manifold сміття.
        """
        if self.mesh is None or self.diag is None:
            return
        cell = self.cfg.surface_cell
        if cell <= 0 or self.diag.area <= 0:
            return
        # Дві комірки на ребро трикутника — уже надлишок для snap.
        need = int(self.diag.area / (cell / 2.0) ** 2)
        have = self.mesh.n_cells
        if need <= 0 or have < 150_000 or have < need * 6:
            return

        target = max(need * 2, 20_000)
        ans = QtWidgets.QMessageBox.question(
            self, tr("Decimate the geometry?"),
            tr("The model has {have} triangles, but with a cell of "
               "{cell} about {need} are enough.\n\nExtra tessellation "
               "does not add accuracy, but it slows down snappyHexMesh "
               "and the program itself.\n\nDecimate to {target} "
               "triangles?", have=fmt.count(have), cell=phys.fmt_len(cell),
               need=fmt.count(need), target=fmt.count(target)),
            QtWidgets.QMessageBox.Yes | QtWidgets.QMessageBox.No,
            QtWidgets.QMessageBox.Yes)
        if ans != QtWidgets.QMessageBox.Yes:
            return
        live = self.enabled_parts()
        self._start_repair("decimate", live,
                           self._decimate_job(target, self.mesh.n_cells),
                           "parts")

    def auto_reference(self):
        """Підставляє Lref і Aref.

        Aref беремо як РЕАЛЬНИЙ силует, а не добуток сторін габариту: у
        габарит входять порожні кути, і для літака він завищує площу крила
        в кілька разів, а Cd і Cl обернено пропорційні цій площі.
        Для крилатого апарата опорна площа — планформа (силует згори),
        для компактного тіла — мідель (силует спереду).

        Lref для крилатого — СЕРЕДНЯ ХОРДА, планформа поділена на розмах, а
        не габарит по X. Довжина фюзеляжу тут не при чому: пограншар, який
        вирішує все, росте на крилі й живе за хордою. На тестовому літаку
        різниця чотирикратна — 0.80 м проти 0.195 м, — і тягне за собою все
        інше: Re виходив 1.4 мільйона замість 330 тисяч, оцінка пограншару
        17.6 мм замість 5.7, тобто шари будувалися б утричі товщими за той
        пограншар, який насправді є. Cm при цьому нормувався б на довжину
        фюзеляжу — величину, до якої момент тангажу стосунку не має.
        """
        if self.diag is None:
            return
        d = self.diag
        sx, sy, sz = d.size

        winged = d.planform > d.frontal * 2.5
        area = d.planform if winged else d.frontal
        if area <= 0:
            area = max(sx * sy, 1e-9)
        chord = area / sy if winged and sy > 0 else 0.0
        self.panel_flow.lref.setValue(max(chord or sx, 1e-6))
        self.panel_flow.aref.setValue(area)
        self.panel_flow.set_area_hint(winged, d)

    # --- оновлення ------------------------------------------------------

    def _units_stale(self) -> bool:
        """Чи розходиться вибір одиниць із тим, що вже вбито в моделі."""
        for part in self.parts:
            if not part.loaded or part.pinned:
                continue
            if abs(self.panel_geo.unit_factor(part.auto) - part.factor) > 1e-15:
                return True
        return False

    def refresh_all(self):
        # Зміна одиниць приходить сюди ж, бо панель геометрії шле той самий
        # `changed`, що й решта налаштувань.
        if self._units_stale():
            self.apply_units()
            self.viewport.show_body(self.mesh_display, reset=True)

        # Перемикач роздільних патчів приходить сюди ж. Перезбирати список
        # треба саме тут: сцена не мінялася, тож rebuild_geometry не
        # покличеться, а патчі мусять зʼявитися одразу.
        self._sync_bodies(self.enabled_parts())
        self._show_split()

        self.panel_mesh.sync_box()
        # Про коробку кажемо ОДРАЗУ, а не при створенні кейсу: людина
        # править числа й має бачити наслідок під полями, а не через
        # п'ять кроків у модальному вікні.
        box_notes = self.cfg.guard_boxes() + self.cfg.guard_scale()
        self.panel_mesh.show_box_note(
            "  ".join(t for _, t in box_notes) if box_notes
            else (tr("Box: {volume:.4g} m³, level {level}.", volume=self.cfg.mesh.boxes[0].volume, level=self.cfg.mesh.boxes[0].level)
                  if self.cfg.mesh.boxes else ""))
        self.panel_dom.refresh()
        self.panel_mesh.refresh()
        self.panel_flow.refresh()
        self.panel_solve.refresh()
        if getattr(self, "_yplus_timer", None) is not None:
            self._yplus_timer.start()

        if self.diag is not None:
            extra = []
            lvl, msg = geo.leak_advice(self.diag, self.cfg.surface_cell)
            if msg:
                extra.append((tr("Leak risk"), msg, lvl))
            lvl, msg = geo.tesselation_advice(self.diag, self.cfg.surface_cell)
            if msg:
                extra.append((tr("Tessellation vs cell"), msg, lvl))
            self.panel_geo.show_diagnosis(
                geo.diagnosis_rows(self.diag), self.diag.notes, extra)
            self.viewport.refresh_scene(self.cfg)
            self._update_rotor_axis_preview()

        self.update_status()

    # --- y+ under the turbulence model ----------------------------------

    def _layer_cap(self) -> int:
        return adv.auto_layer_cap(self.diag)

    def _layers_fit(self, trial) -> str:
        """"" if the trial mesh fits maxGlobalCells and memory, else why."""
        est = trial.estimate()
        if est.total > trial.mesh.max_global_cells:
            return (tr("the estimate of {total} cells is larger than "
                       "maxGlobalCells {max_global_cells}", total=phys.fmt_cells(est.total), max_global_cells=phys.fmt_cells(trial.mesh.max_global_cells)))
        try:
            fit = self._mem_fit(trial)
        except Exception:                                  # noqa: BLE001
            return ""
        return fit.warning() if fit.verdict == "over" else ""

    def _auto_yplus(self, force: bool = False) -> None:
        """Pick the y+ and the layers for the model, if «Авто» is on."""
        if not self.cfg.mesh.yplus_auto or self.mesh is None:
            return
        if (self._pmesh.active or getattr(self, "_pmesh_pending", None)
                or getattr(self, "_pmesh_hold", False)):
            # Сітка з проєкту зафіксована: шари в ній уже є, і підбір не
            # має права переписати товщину першого шару (так 2026-11-10
            # змінився ключ і пішов snappyHexMesh). Перевірка y+ у рядку
            # стану попереджає й далі.
            return
        cap = self._layer_cap()
        sig = fy.signature(self.cfg, cap)
        if sig == self._yplus_sig and not force:
            return
        pick = fy.auto_pick(self.cfg, fits=self._layers_fit, layer_cap=cap)
        self.panel_mesh.apply_auto(pick)
        # After the write: an expansion that the pick raised is an input too,
        # and it must not cause one more pick.
        self._yplus_sig = fy.signature(self.cfg, cap)

    def _set_turbulence(self, model: str) -> None:
        """Fix button of the y+ check: another turbulence model."""
        self.panel_flow.cmb_turb.setCurrentText(model)
        self.statusBar().showMessage(tr("Turbulence model: {model}.", model=model), 6000)

    def _set_treatment(self, key: str) -> None:
        """Fix button of the y+ check: another wall treatment."""
        i = self.panel_flow.cmb_wall.findData(key)
        if i >= 0:
            self.panel_flow.cmb_wall.setCurrentIndex(i)
            self.statusBar().showMessage(
                tr("Wall treatment: {TREATMENT_LABELS}.", TREATMENT_LABELS=fy.TREATMENT_LABELS[key]), 6000)

    def update_status(self):
        s = fc.summary(self.cfg)
        est = self.cfg.estimate()
        parts = [
            f"Re {s['Re']}",
            f"M {s['Mach']}",
            tr("cells ~{s}", s=s['Cells (estimate)']),
            f"snappy ~{s['snappy memory']}",
            f"y+ {s['y+ (estimate)']}",
            tr("cell {s} → {s2}", s=s['Background cell'], s2=s['Surface cell']),
        ]
        self.status.setText(fs_i18n.join("   |   ", parts))

        warn = []
        fit = self._mem_fit()
        if fit.warning():
            warn.append(fit.warning())
        tip = fit.details()
        self.status.setToolTip(tip)
        self.lbl_warn.setToolTip(tip)
        if est.total > self.cfg.mesh.max_global_cells:
            warn.append(tr("⚠ estimate exceeds maxGlobalCells"))
        if self.diag is not None and not self.diag.closed:
            lvl, _ = geo.leak_advice(self.diag, self.cfg.surface_cell)
            warn.append(tr("⚠ holes larger than a cell — it will leak") if lvl == "bad"
                        else tr("⚠ surface not closed"))
        f = self.cfg.flow
        if f.farfield_mode == "tunnel" and (abs(f.aoa_deg) > 0.01
                                            or abs(f.aos_deg) > 0.01):
            warn.append(tr("⚠ slip will cut the angle of attack"))
        yc = fy.case_check(self.cfg)
        if not yc.ok:
            warn.append("⚠ " + yc.short)
        self.lbl_warn.setText(fs_i18n.join("   ", warn))
        # Лише «ℹ» (памʼять ще невідома) — не тривога: сірим, не червоним.
        alarm = any("⚠" in t for t in warn)
        self.lbl_warn.setStyleSheet("color:#c0392b; font-weight:bold;" if alarm
                                    else "color:#7f8c8d;")

    def refresh_plots(self):
        if not (self.worker and self.worker.runner):
            return
        st = self.worker.runner.state
        try:
            coeffs = be.read_force_coeffs(self.worker.poll_dir)
        except OSError:
            coeffs = {}
        self.plots.update_plots(st.residuals, coeffs,
                                self.cfg.solver.residual_tol)

    # --- бекенд ---------------------------------------------------------

    def _busy(self, text: str) -> None:
        """Показати, що зараз робить пошук машин, і дати вікну це
        намалювати. Ввід користувача тут не обробляємо: пошук ще йде."""
        self.lbl_busy.setText(text)
        self.lbl_busy.setVisible(bool(text))
        if text:
            # Повідомлення (напр. «Режим новачка увімкнено…» на старті)
            # стискало б індикатор до половини рядка, і «(4 з 5)» зникало
            # за трикрапкою. Поки йде пошук, важливіший індикатор.
            self.statusBar().clearMessage()
        QtWidgets.QApplication.processEvents(
            QtCore.QEventLoop.ExcludeUserInputEvents)

    def detect_backends(self):
        # Між перевірками крутиться цикл подій (щоб індикатор малювався):
        # другий виклик звідти не має починати другий пошук.
        if self._detecting:
            return
        self._detecting = True
        QtWidgets.QApplication.setOverrideCursor(QtCore.Qt.WaitCursor)
        try:
            self._busy(tr("⏳ Searching for OpenFOAM…"))
            self.backends = be.detect_all(
                ssh_hosts=self._ssh_hosts(),
                containers=be.containers_setting(fset.get("ssh_containers")),
                progress=lambda name, i, n: self._busy(
                    tr("⏳ Checking machines: {name}… ({i} of {n})", name=name, i=i, n=n)))
            self._detect_done()
        finally:
            self._busy("")
            QtWidgets.QApplication.restoreOverrideCursor()
            self._detecting = False
        self.statusBar().showMessage(self._backends_summary(), 10000)

    def _backends_summary(self) -> str:
        """Коротко: які машини відповіли. Для рядка стану після пошуку."""
        if not self.backends:
            return tr("Machines checked: OpenFOAM was not found anywhere.")
        parts = []
        for b in self.backends:
            name = b.host if b.kind == "ssh" and b.host else b.label.split(" / ")[0]
            parts.append(f"{name} {'✔' if b.ready else '✖'}")
        return tr("Machines checked: {parts}", parts=", ".join(parts))

    def _detect_done(self):
        """Що робимо зі знайденими бекендами: список, вибір, ресурси."""
        # Хто не відповів минулого разу, тепер може бути готовий: питаємо
        # знову. Відповіді, що вже є, лишаються.
        self._res_cache = {k: v for k, v in self._res_cache.items()
                           if v[0] > 0 and v[2] > 0}
        _, script = be.install_command()
        self.panel_run.set_backends(self.backends, script)
        # Пошук може завершитися і після відновлення сесії — тоді вибір
        # бекенда чекає тут.
        self._select_backend()
        b = self.panel_run.current_backend()
        if b is not None:
            # Для сервера це ще один запит по SSH.
            self._busy(tr("⏳ Reading cores and memory: {v}…", v=b.host or b.label))
        self._show_resources(clamp=True)
        self.update_status()

    def _show_resources(self, clamp: bool = False) -> None:
        """Показати ядра й памʼять ТОГО бекенда, який вибрано.

        Доти показувався завжди WSL, хоч би що стояло в списку. Для того,
        хто рахує на сервері, це просто чужі числа: у ноута шість ядер і
        7.6 ГБ, у сервера вісім і чотирнадцять, і поради «скільки
        процесів» із них виходять різні.
        """
        b = self.panel_run.current_backend()
        if b is None:
            return
        key = self._res_key(b)
        if key not in self._res_cache:
            # Питаємо раз на бекенд: кожен запит — це ssh чи запуск WSL,
            # а список гортають туди-сюди.
            if b.kind == "wsl":
                self._res_cache[key] = be.wsl_resources(b)
            elif b.kind == "ssh" and b.ready:
                self._res_cache[key] = be.ssh_resources(b)
            elif b.kind in ("docker", "bluecfd", "native"):
                n = os.cpu_count() or 4
                self._res_cache[key] = (max(1, n // 2), n, be.local_ram_gb())
            # Сервер ще не готовий — НЕ кешуємо нулі: інакше, коли він стане
            # готовим, його памʼять так і лишилася б невідомою (а раніше
            # бралися 8 ГБ чи памʼять попереднього бекенда). Спитаємо при
            # наступному пошуку бекендів.
        where = b.host if b.kind == "ssh" else tr("this computer", ctx="locative")
        cores, threads, ram = self._res_cache.get(key, (0, 0, 0.0))
        if cores <= 0:
            self.panel_solve.lbl_res.setText(
                tr("Cores and memory on {where} are not known yet{v}", where=where, v=" — the server has not answered yet." if b.kind == "ssh" else "."))
            return
        self.panel_solve.set_resources(cores, ram, threads=threads, where=where)
        # Число процесів пересуваємо лише при першому пошуку: інакше
        # перемикання бекенда мовчки правило б те, що людина задала руками.
        if clamp and self.cfg.solver.n_procs > cores:
            self.panel_solve.procs.setValue(max(1, cores - 2))

    @staticmethod
    def _res_key(b) -> str:
        return f"{b.kind}:{b.host or b.distro}"

    def _backend_resources(self, b=None) -> "tuple[int, int, float]":
        """(ядер, потоків, ГБ) ВИБРАНОГО бекенда; нулі — ще невідомо.

        Завжди з кешу саме цього бекенда, а не з того, що лишилось від
        попереднього вибору."""
        b = b if b is not None else self.panel_run.current_backend()
        if b is None:
            return (0, 0, 0.0)
        return self._res_cache.get(self._res_key(b), (0, 0, 0.0))

    def _backend_cores(self) -> int:
        return self._backend_resources()[0] or os.cpu_count() or 6

    def _mem_fit(self, cfg=None) -> "memfit.MemFit":
        """Чи влізе задача в памʼять — для рядка стану, помічника й запуску.

        Сітку будує вибраний бекенд (і в розподіленому прогоні теж: сервер
        будує її сам на всі процеси); розвʼязувач іде або там же, або по
        машинах розподілу — кожна своєю часткою проти своєї памʼяті.
        Збережена сітка, що підходить до цих налаштувань, — snappyHexMesh
        не буде, а комірок рівно стільки, скільки в ній.
        """
        cfg = cfg or self.cfg
        pr = self.panel_run
        b = pr.current_backend()
        ram = self._backend_resources(b)[2]
        if b is None:
            name, kind = "the backend", "ssh"
        elif b.kind == "ssh":
            name, kind = b.host, "ssh"
        else:
            name = tr("this computer{v}", v=" (WSL)" if b.kind == "wsl" else "")
            kind = b.kind
        mesher = memfit.MemHost(name, ram, memfit.headroom_for(kind),
                                ranks=max(1, cfg.solver.n_procs))
        solvers = None
        if b is not None and b.kind == "ssh" and pr.box_dist.isChecked():
            machines, why = be.check_machines(pr.machines())
            if machines and not why:
                mesher.ranks = sum(m.procs for m in machines)
                solvers = memfit.dist_hosts(machines, cfg.solver.proc_weights,
                                            head_ram_gb=ram)
        cells = 0
        pmesh = getattr(self, "_pmesh", None)
        if pmesh is not None and pmesh.active and cfg is self.cfg:
            cells = pmesh.cells
        elif (b is not None and be.mesh_cache_supported(b)
                and not pr.chk_remesh.isChecked()):
            try:
                _, cells = self._saved_mesh.find(
                    cfg, self.case_dir or "",
                    getattr(self, "_project_path", "") or "")
            except Exception:                              # noqa: BLE001
                cells = 0
        return memfit.memory_fit(cfg.estimate(), mesher, solvers,
                                 saved_cells=cells)

    def install_openfoam(self):
        b = self.panel_run.current_backend()
        distro = b.distro if b and b.distro else "Ubuntu"
        argv, script = be.install_command(distro)
        ans = QtWidgets.QMessageBox.question(
            self, tr("Install OpenFOAM"),
            tr("A WSL console will open and run:\n\n{script}\n\nThis will "
               "download about 1-2 GB and ask for the sudo password. "
               "Continue?", script=script),
            QtWidgets.QMessageBox.Yes | QtWidgets.QMessageBox.No)
        if ans != QtWidgets.QMessageBox.Yes:
            return
        try:
            import subprocess
            subprocess.Popen(argv)
            self.panel_run.lbl_status.setText(
                "Console opened. When it is done, click “Search”.")
        except OSError as exc:
            QtWidgets.QMessageBox.critical(self, tr("Could not start"), str(exc))

    # --- кейс -----------------------------------------------------------

    def create_case(self, report=True, build_cfg=None) -> str | None:
        """Write the case. `build_cfg`: write the dictionaries from this
        configuration (the pre-run copy). The checks and the manifest use
        `self.cfg` all the same."""
        if self.mesh is None:
            QtWidgets.QMessageBox.warning(self, tr("No geometry"),
                                          tr("Load an STL first."))
            return None
        # Точка в потоці — та, від якої snappy заливає повітря. Якщо вона
        # сидить усередині габариту тіла або стоїть на межі домену, кейс
        # зберуться й порахується, але порахує повітря В БАКУ замість
        # обтікання. Мовчки такого пропускати не можна.
        if self.cfg.point_clearance(self.cfg.location_in_mesh) <= 0:
            QtWidgets.QMessageBox.critical(
                self, tr("There is no place for the point in flow"),
                tr("The point from which snappyHexMesh fills the air is "
                   "inside the body bounding box or exactly on the "
                   "domain boundary.{NEWLINE}{NEWLINE}Point: "
                   "{location_in_mesh}{NEWLINE}Domain: "
                   "{tuple}{NEWLINE}{NEWLINE}This happens with zero "
                   "domain margins or with a custom point that came from "
                   "imported settings. Increase the margins on the "
                   "“Domain” tab — and the case will build.", NEWLINE=NEWLINE, location_in_mesh=self.cfg.location_in_mesh, tuple=tuple(round(v, 4) for v in self.cfg.bounds)))
            return None
        # Налаштування часу в нестаціонарі легко задати так, що прогін
        # триватиме тижні або забʼє диск тисячами тек. Це видно ЩЕ ДО
        # запуску, тому впиратися треба тут, а не після трьох годин рахунку.
        checks = (self.cfg.guard_unsteady() + self.cfg.guard_bodies()
                  + self.cfg.guard_porous() + self.cfg.guard_resolution()
                  + self.cfg.guard_boxes() + self.cfg.guard_scale()
                  + self.cfg.guard_refine_zones()
                  + self.cfg.guard_zone_vs_shell()
                  + self.cfg.guard_adapt()
                  + self.cfg.guard_turbulence()
                  + self.cfg.guard_yplus()
                  + self.cfg.guard_regime())
        blocking = [text for level, text in checks if level == "bad"]
        warnings = [text for level, text in checks if level == "warn"]
        if blocking:
            QtWidgets.QMessageBox.critical(
                self, tr("Cannot run like this"),
                tr("{blocking}{NEWLINE}{NEWLINE}The case was not "
                   "created. The same message is also shown on the tabs, "
                   "next to the setting it applies to.", blocking=(NEWLINE + NEWLINE).join(blocking), NEWLINE=NEWLINE))
            return None
        if warnings and set(map(str, warnings)) <= self._warn_quiet:
            for text in warnings:
                self.log.push(tr("Warning (accepted before the pre-run): "
                                 "{text}", text=text))
        elif warnings and self._series_quiet():
            # На першому куті серії людина це вже бачила й погодилась.
            for text in warnings:
                self.log.push(tr("Warning (accepted at the first angle "
                                 "of the series): {text}", text=text))
        elif warnings:
            ans = QtWidgets.QMessageBox.question(
                self, tr("Check the settings"),
                tr("{warnings}{NEWLINE}{NEWLINE}Create the case anyway?", warnings=(NEWLINE + NEWLINE).join(warnings), NEWLINE=NEWLINE),
                QtWidgets.QMessageBox.Yes | QtWidgets.QMessageBox.No,
                QtWidgets.QMessageBox.No)
            if ans != QtWidgets.QMessageBox.Yes:
                return None
            if self._prerun_phase == "pre":
                self._warn_quiet = set(map(str, warnings))

        if report:
            # «Create Case»: not over the folder of an earlier run (T43).
            self._new_run_folder()
            if self._run_note:
                self.log.push(self._run_note)
                self._run_note = ""
        base = Path(self.panel_run.ed_dir.text().strip() or DEFAULT_CASES)
        # Імʼя йде в теку: «11:15» тут валило створення ([WinError 123]).
        name = fc.safe_case_name(self.cfg.name)
        if name != self.cfg.name:
            self.log.push(tr("Case name “{name}” changed to “{name2}”: a "
                             "folder name cannot have a colon, spaces, "
                             "quotes and so on.", name=self.cfg.name, name2=name))
            self.cfg.name = name
            self.panel_run.ed_name.blockSignals(True)
            self.panel_run.ed_name.setText(name)
            self.panel_run.ed_name.blockSignals(False)
        case_dir = base / name
        try:
            for sub in ("system", "constant/triSurface", "0.orig"):
                (case_dir / sub).mkdir(parents=True, exist_ok=True)
            files = fc.build_case(build_cfg or self.cfg)
            # ПРИБРАТИ СТАЛЕ ПЕРЕД ЗАПИСОМ НОВОГО.
            #
            # build_case() пише лише те, що ЗАРАЗ потрібне. Якщо востаннє
            # тут лежали обертові зони (constant/MRFProperties,
            # system/topoSetDict), а зараз ролі деталей уже не «гвинт» —
            # ці два файли build_case() більше не поверне, а писальний
            # цикл нижче лише ДОПИСУЄ те, що дали, і НІКОЛИ не видаляє
            # те, чого не дали. Стара MRFProperties лишалася б на диску
            # з чотирма зонами, а topoSet, що мав їх створити в сітці,
            # більше не запускався б — розвʼязувач падав би з «cannot
            # find MRF cellZone», хоча сама постановка виглядала чистою.
            stale = fc.prune_stale_files(case_dir, files)
            if stale:
                self.log.push(tr("Removed stale case files (their "
                                 "condition no longer applies): {stale}", stale=", ".join(stale)))
            for rel, text in files.items():
                p = case_dir / rel
                p.parent.mkdir(parents=True, exist_ok=True)
                # LF обовʼязково: OpenFOAM у WSL не читає dict з CRLF надійно,
                # а Allrun з CRLF взагалі не запуститься.
                p.write_text(text, encoding="utf-8", newline="\n")
            # triSurface — заново: STL від минулого збирання (інші патчі,
            # інші деталі) лишалися б у ключі сітки, хоч snappy їх не бере.
            tri_dir = case_dir / "constant" / "triSurface"
            shutil.rmtree(tri_dir, ignore_errors=True)
            tri_dir.mkdir(parents=True, exist_ok=True)
            self._write_tri(tri_dir)
            post.ensure_foam_file(str(case_dir), name)
            # Маніфест — єдине, з чого кейс потім відкривається назад у вікні:
            # зі згенерованих словників вихідні налаштування не відновити.
            lib.write_manifest(case_dir, self.cfg,
                               stl_source=self.stl_source or self.stl_path)
        except Exception:
            QtWidgets.QMessageBox.critical(self, tr("Could not write the case"),
                                           traceback.format_exc(limit=3))
            return None

        self.case_dir = str(case_dir)
        self.refresh_library(keep=self.case_dir)
        if report:
            est = self.cfg.estimate()
            QtWidgets.QMessageBox.information(
                self, tr("Case created"),
                tr("{case_dir}\n\nCells (estimate): {total}\nsnappy memory: "
                   "   {snappy_peak_gb} GB\nSTL triangles:    "
                   "{n_cells}", case_dir=case_dir, total=phys.fmt_cells(est.total),
                   snappy_peak_gb=fmt.decimal(est.snappy_peak_gb, 1),
                   n_cells=fmt.count(self.mesh.n_cells)))
        return self.case_dir

    def _ssh_hosts(self) -> list[str]:
        """Адреси серверів для розпізнавання. Порожнє поле — жодного."""
        try:
            host = self.panel_run.ed_server.text().strip()
        except AttributeError:
            host = ""
        return [host] if host else []

    def recheck_server(self):
        """Перевіряє сервер і оновлює список бекендів.

        Результат кажемо вголос у будь-якому разі. Мовчазне «нічого не
        сталося» тут найгірше: людина не знає, чи сервер не відповів, чи
        вона просто не туди натиснула.
        """
        host = self.panel_run.ed_server.text().strip()
        fset.set("ssh_host", host)
        if not host:
            self.detect_backends()
            self.statusBar().showMessage(tr("Server removed, running here."), 8000)
            return
        self.statusBar().showMessage(tr("Checking {host}…", host=host))
        QtWidgets.QApplication.processEvents()
        found = be.detect_ssh(
            [host], be.containers_setting(fset.get("ssh_containers")))
        self.detect_backends()
        if found and found[0].ready:
            b = found[0]
            self.statusBar().showMessage(
                f"{host}: OpenFOAM {b.version}, {b.detail}", 15000)
        else:
            why = found[0].detail if found else tr("no response")
            QtWidgets.QMessageBox.warning(
                self, tr("Server not ready"),
                tr("{host}: {why}{NEWLINE}{NEWLINE}Key login without a "
                   "password and an installed OpenFOAM are necessary. To "
                   "check manually: ssh {host} and then ls "
                   "/usr/lib/openfoam/", host=host, why=why, NEWLINE=NEWLINE))

    # --- підключення до прогону, що вже йде --------------------------------

    def _peek_sessions(self):
        """Тихо спитати сервер, чи не йде там прогін, і сказати про це.

        Саме тихо: ні вікон, ні скарг. Сервера може не бути, мережі може не
        бути, кнопка «Перевірити» могла бути не натиснута жодного разу — і
        це все нормальні стани, а не помилки, про які варто говорити.

        Але якщо прогін ТАМ Є, людина має дізнатися про це одразу після
        запуску програми, а не здогадатися натиснути кнопку. Заради цього
        все й робилося: закрив вікно — прогін іде; відкрив — бачиш його.
        """
        host = self.panel_run.ed_server.text().strip()
        if not host:
            return
        self._peek_thread = threading.Thread(
            target=self._peek_worker, args=(host,), daemon=True)
        self._peek_thread.start()

    def _peek_worker(self, host: str):
        """Опитування в окремій нитці: ssh думає секунди, вікно не чекає."""
        try:
            found = [b for b in be.detect_ssh(
                [host], be.containers_setting(fset.get("ssh_containers")))
                if b.ready]
            live = be.list_sessions(found[0]) if found else []
        except Exception:
            live = []          # мовчки: див. docstring вище
        worth = [s for s in live if s.worth_greeting()]
        if worth:
            self.sessions_seen.emit(worth)

    # How long ago: fs_fmt (T51).
    _how_long = staticmethod(fmt.ago)

    @QtCore.Slot(object)
    def _say_sessions(self, found):
        """Сказати про знайдені прогони — уже в нитці вікна.

        Окремо про ті, що ЙДУТЬ, і окремо про ті, що СКІНЧИЛИСЯ, поки
        програми не було. Друге не менш важливе за перше: саме по це
        людина найчастіше й повертається вранці.
        """
        going = [s for s in found if s.running and s.alive]
        over = [s for s in found if not (s.running and s.alive)]
        self.log.push("")
        if going:
            self.log.push(tr("RUNNING ON THE SERVER NOW:"))
            for s in going:
                self.log.push(tr("  • {name} — step “{v}”, started {v2}, "
                                 "{log_lines} log lines", name=s.name, v=s.step_text or '?', v2=s.started or 'at an unknown time', log_lines=s.log_lines))
        if over:
            self.log.push(tr("FINISHED WHILE THE PROGRAM WAS CLOSED:"))
            for s in over:
                tail = f" — {s.message}" if s.message else ""
                self.log.push(f"  • {s.name}: {s.human()} "
                              f"{self._how_long(s.age)}{tail}")
        self.log.push(tr("The “Connect to Run” button on the “Run” tab will "
                      "show the log and bring back the results."))
        # T49: plural forms, not English words put into the text («1 run
        # there is on the server»).
        if going:
            note = fs_i18n.plural(len(going), "{n} run is on the server now",
                                  "{n} runs are on the server now")
        else:
            note = fs_i18n.plural(
                len(over), "{n} run on the server ended while you were away",
                "{n} runs on the server ended while you were away")
        self.statusBar().showMessage(tr("{note} — see the “Run” tab.", note=note), 20000)
        # Кнопку підсвічуємо: журнал легко проминути, а це те саме
        # повідомлення, тільки там, де на нього можна натиснути.
        self.panel_run.btn_attach.setStyleSheet(
            "font-weight:bold; color:#4caf50;")

    def attach_to_server(self):
        """«Connect to Run»: the «Runs» window, on the runs on the servers
        (T49b; before: the dialog «Runs on the Server»).

        The window reads each known server in a thread (fs_runs.scan) and
        connects by the path of start_attach (RunGuard.connect_run).
        """
        self.open_queue(focus_server=True)

    def _ssh_backend_or_complain(self):
        """Готовий ssh-бекенд або None — і тоді вже сказано, чому."""
        host = self.panel_run.ed_server.text().strip()
        if not host:
            self.panel_run.ed_server.setFocus()
            QtWidgets.QMessageBox.information(
                self, tr("No server specified"),
                tr("Enter the server in the “Server” field — a name from "
                "~/.ssh/config or user@host."))
            return None
        if not self.panel_run.select_ssh_backend():
            self.recheck_server()
            if not self.panel_run.select_ssh_backend():
                return None
        b = self.panel_run.current_backend()
        return b if b is not None and b.kind == "ssh" and b.ready else None

    def start_attach(self, backend, session):
        """Почати читати журнал сесії з сервера."""
        if self.thread is not None and self.thread.isRunning():
            QtWidgets.QMessageBox.information(
                self, tr("Wait"), tr("Stop the current run first."))
            return
        # Кейс беремо той, з якого прогін пустили, якщо він ще на місці.
        # Якщо ні — читаємо самий лише журнал: результати нікуди не
        # подінуться, їх можна забрати й потім.
        case_dir = session.case_dir if (
            session.case_dir and Path(session.case_dir).is_dir()) else ""
        # The window can show another case (or no case) than the run. Then
        # its settings (patch names, steady or transient) do not fit the
        # results: after the run, open the case of the run (T37).
        self._attach_open = (case_dir if case_dir
                             and not self._shows_case(case_dir) else "")
        if case_dir:
            self.case_dir = case_dir
        self.log.clear_log()
        self.log.push(tr("Connecting to {host}:{remote_dir}", host=backend.host, remote_dir=session.remote_dir))
        self.log.push(tr("Case “{name}”, status: {human}{v}", name=session.name, human=session.human(), v=tr(", step “{step}”", step=session.step_text) if session.step else ""))
        if not case_dir and session.case_dir:
            self.log.push(
                tr("The folder {case_dir} is not here — if necessary, "
                   "get the results with the “Get from Server” button.", case_dir=session.case_dir))
        if session.stale:
            self.log.push(tr("WARNING: the status says “running”, but there "
                          "is no process on the server. The log will be "
                          "shown, but the run is no longer in progress."))
        self.plots.set_steady(self._case_steady(case_dir))
        self.plots.update_plots({}, {})
        self._run_started = time.monotonic()
        self.tabs_right.setCurrentWidget(self.log)
        self.panel_run.set_running(True)
        self.panel_run.progress.setValue(0)

        self.worker = RunWorker(
            backend, case_dir or self.case_dir or str(DEFAULT_CASES),
            [], native_dir=session.remote_dir,
            sync_back=bool(case_dir), name=session.name,
            attach_to=session.remote_dir)
        self.thread = QtCore.QThread(self)
        self.thread.setObjectName("run")
        self.worker.moveToThread(self.thread)
        self.thread.started.connect(self.worker.run)
        self.worker.line.connect(self._run_line)
        self.worker.event.connect(self._run_event)
        # «You can turn off the laptop» only for a run that still runs on
        # the server. An ended run (T44 connects to it by itself at start,
        # to get its results) goes from here to the download (T49).
        self._laptop(self.attach_laptop_state(session))
        self.worker.step.connect(self.on_step)
        self.worker.done.connect(self.on_done)
        self.thread.start()
        self.timer.start()
        self._live_start()

    @staticmethod
    def attach_laptop_state(session) -> str:
        """The laptop indicator when the window connects to `session`:
        "free" for a live run, "" for an ended one (its download sets
        "fetch")."""
        return "free" if session.running and session.alive else ""

    def _shows_case(self, case_dir: str) -> bool:
        """True if the window shows this case now: its folder and its name.

        The name is the name of the case folder (_apply_restore), so a
        window with only the folder of the case (start_attach sets it) and
        the settings of another case does not count.
        """
        if not case_dir or not self.case_dir:
            return False
        return (os.path.normcase(os.path.abspath(str(self.case_dir)))
                == os.path.normcase(os.path.abspath(str(case_dir)))
                and self.cfg.name == Path(case_dir).name)

    def _case_steady(self, case_dir: str) -> bool:
        """Steady or transient: from the manifest of the case, if it has one.

        The window can show another case than the run (attach, T37); its
        setting would give wrong plots and a wrong end time.
        """
        if case_dir and not self._shows_case(case_dir):
            try:
                solver = (lib.read_manifest(case_dir) or {}).get(
                    "cfg", {}).get("solver", {})
                if isinstance(solver, dict) and "steady" in solver:
                    return bool(solver["steady"])
            except (OSError, ValueError, AttributeError):
                pass
        return bool(self.cfg.solver.steady)

    def _open_attached(self, case_dir: str) -> None:
        """After an attach: open the case of the run, like «Open» on the
        «Saved» tab (settings, geometry, results), and select it there.

        Unsaved changes in the scene stay: then the case does not open, and
        the window tells where it is.
        """
        name = Path(case_dir).name
        if self.project_is_dirty():
            self.log.push(tr("The results are in the case “{name}”. The scene "
                             "has unsaved changes, so the case did not open: "
                             "it is selected on the “Saved” tab, click "
                             "“Open” there.", name=name))
            # Only the list of the «Saved» tab goes to the folder of the case
            # (like its «Browse…»); the cases folder of the scene stays.
            self.panel_lib.set_root(str(Path(case_dir).parent))
            self.show_library()
            self.panel_lib.select(case_dir)
            return
        self.log.push(tr("Opening the case “{name}” of the run.", name=name))
        self.open_saved(case_dir)
        if self.panel_lib.loaded:
            self.panel_lib.select(case_dir)

    def open_case_dir(self):
        target = self.case_dir or self.panel_run.ed_dir.text()
        if target and os.path.isdir(target):
            os.startfile(target)

    # --- збережені симуляції --------------------------------------------

    def show_library(self):
        self.tabs_right.setCurrentWidget(self.panel_lib)
        self.panel_lib.refresh()

    def _right_tab_changed(self, index: int):
        """Каталог читаємо аж тоді, коли на список справді дивляться:
        обхід дерева кейсів на кожен запуск програми — це секунди на порожньому
        місці."""
        if self.tabs_right.widget(index) is self.panel_lib:
            self.panel_lib.refresh_if_needed()

    def refresh_library(self, keep: str = ""):
        if not self.panel_lib.loaded:
            return
        self.panel_lib.refresh()
        if keep:
            self.panel_lib.select(keep)

    def open_saved(self, path: str):
        """Відкриває збережену симуляцію: налаштування, геометрію, результати."""
        if self._load_thread is not None:
            self.statusBar().showMessage(
                tr("Wait: the previous geometry is still being read."), 6000)
            return
        # ПРОЄКТ МАЄ ПЕРЕВАГУ НАД КЕЙСОМ, якщо він є. У кейсі лежить уже
        # злита геометрія, а в проєкті — кожна деталь окремо, з роллю,
        # обертами й завісою. Проєкт кладе туди автозбереження перед
        # прогоном, тож для всього, що рахувалося з цієї програми, він є.
        proj = self.project_for_case(Path(path).name)
        if proj is not None:
            self.log.push(tr("There is a project {name} for the case — opening it", name=proj.name))
            # Кейс беремо той, що вибрали в списку, а не той, що записаний
            # у проєкті: кейс могли перейменувати чи перенести.
            self._project_force_case = path
            self.open_project(str(proj))
            return

        info = lib.read_case(path, with_size=False)
        if not info.has_cfg:
            self._open_legacy(info)
            return
        if info.stl and os.path.isfile(info.stl):
            # Геометрію читає той самий потік, що й звичайне відкриття STL;
            # решта відновлення — у _finish_load, коли сітка вже в руках.
            self._pending_restore = lambda: self._apply_restore(info)
            self._restore_queue = self._split_parts_of(info)
            first = self._restore_queue.pop(0) if self._restore_queue else info.stl
            self.load_stl(first)
        else:
            self._apply_restore(info)

    def _split_parts_of(self, info) -> list[str]:
        """Файли всіх частин кейсу з роздільними патчами, якщо вони є.

        Порядок той самий, що в маніфесті, і це не косметика: імена патчів
        роздаються за порядком частин у сцені, тож переставивши їх, ми
        віддали б сили крила патчу фюзеляжу.
        """
        bodies = ((info.cfg or {}).get("bodies") or []) if info.cfg else []
        if len(bodies) < 2:
            return []
        tri = Path(info.path) / "constant" / "triSurface"
        out = []
        for b in bodies:
            name = (b or {}).get("stl_file") if isinstance(b, dict) else None
            if not name:
                continue
            p = tri / str(name)
            if p.is_file():
                out.append(str(p))
        # Усе або нічого: відновити дві частини з трьох гірше, ніж одну —
        # патчі роздалися б не тим шматкам, і сили поїхали б мовчки.
        return out if len(out) == len(bodies) else []

    def _open_legacy(self, info):
        """Кейс без маніфесту: налаштування невідомі, лишається перегляд."""
        if not info.times:
            QtWidgets.QMessageBox.information(
                self, tr("Nothing to open"),
                tr("The case “{name}” has neither a {MANIFEST} file with "
                   "settings nor computed time directories.\n\nYou can "
                   "read the logs in the case directory, but to continue "
                   "the run, create the case again.", name=info.name, MANIFEST=lib.MANIFEST))
            return
        ans = QtWidgets.QMessageBox.question(
            self, tr("Results Only"),
            tr("The case “{name}” has no {MANIFEST} file, so the "
               "settings cannot be restored — system/ holds only the "
               "finished dictionaries.\n\nShow just the results? Cp will "
               "be computed from the current speed {u_inf:.4g} m/s, not "
               "from the speed the case was run with.", name=info.name, MANIFEST=lib.MANIFEST, u_inf=self.cfg.flow.u_inf),
            QtWidgets.QMessageBox.Yes | QtWidgets.QMessageBox.No,
            QtWidgets.QMessageBox.Yes)
        if ans != QtWidgets.QMessageBox.Yes:
            return
        self.case_dir = info.path
        self.cfg.name = Path(info.path).name
        self.panel_run.ed_name.setText(self.cfg.name)
        self.load_results()
        self.tabs_right.setCurrentWidget(self.results_view)

    def _apply_restore(self, info):
        """Переносить збережений кейс у вікно."""
        dropped = lib.apply_dict(self.cfg, info.cfg)
        # Маніфест може бути сам собі суперечливим: список частин є, а
        # намір «різати на патчі» знято. Таке трапляється в кейсах,
        # зібраних скриптом, а не вікном. Віримо СПИСКУ: якщо частин кілька,
        # то кейс роздільний, хоч би що стояло в галочці. Інакше _sync_bodies
        # мовчки обнулить список, і вкладка «Результати» шукатиме границю
        # `body`, якої в такому кейсі немає.
        if len(self.cfg.bodies) > 1:
            self.cfg.split_patches = True
        # STL кейсу — це копія в constant/triSurface; справжнє походження
        # записано в маніфесті.
        self.stl_source = info.stl_source or info.stl
        if self.diag is not None:
            # Габарит і площу беремо з щойно прочитаного STL: це той самий
            # файл, що в маніфесті, зате числа рахувала ця сама програма.
            d = self.diag
            self.cfg.bbox_min = (d.bounds[0], d.bounds[2], d.bounds[4])
            self.cfg.bbox_max = (d.bounds[1], d.bounds[3], d.bounds[5])
            self.cfg.surface_area = d.area
        # Кейс пишеться в <каталог>/<назва>, тому назва мусить дорівнювати
        # каталогу: інакше «Запустити» створив би поряд другий кейс, а цей
        # лишився б недоторканим.
        self.cfg.name = Path(info.path).name
        self.case_dir = info.path
        parent = str(Path(info.path).parent)
        if os.path.normcase(parent) != os.path.normcase(
                self.panel_run.ed_dir.text().strip()):
            self.panel_run.ed_dir.setText(parent)
        self.reload_panels()
        if self.mesh is not None:
            self.viewport.show_body(self.mesh_display, reset=True)
        self.refresh_all()

        says = [tr("Opened “{name}”.", name=self.cfg.name)]
        if not info.stl:
            says.append(tr("There is no STL in the case — open a model before you run."))
        if dropped:
            says.append(tr("Not accepted from the manifest: {join}.", join=", ".join(dropped[:8])))
        if info.times:
            self.load_results()
            self.tabs_right.setCurrentWidget(self.results_view)
            says.append(tr("Time directories: {n_times}.", n_times=len(info.times)))
        else:
            self.results = None
            self.slice_bar.setEnabled(False)
            self._drop_scene()
            self.lbl_coeffs.setText("There are no results in this case.")
            if self.mesh is not None:
                self.tabs_right.setCurrentWidget(self.viewport)
            says.append(tr("No results — you can run."))
        self.statusBar().showMessage("  ".join(says), 20000)

    def open_saved_in_paraview(self, path: str):
        self.open_in_paraview(path, Path(path).name)

    # --- налаштування і остання сесія ------------------------------------

    def save_settings(self):
        """Усі налаштування окремим файлом."""
        name = tr("{v}-settings.json", v=self.cfg.name or "case")
        start = str(Path(self.panel_run.ed_dir.text().strip()
                         or DEFAULT_CASES) / name)
        path, _ = QtWidgets.QFileDialog.getSaveFileName(
            self, tr("Save Settings"), start, "JSON (*.json)")
        if not path:
            return
        try:
            lib.export_settings(path, self.cfg)
        except OSError as exc:
            QtWidgets.QMessageBox.critical(self, tr("Could not save"),
                                           str(exc))
            return
        self.statusBar().showMessage(tr("Settings saved: {path}", path=path), 12000)

    def load_settings(self):
        """Читає налаштування з файлу — свого або studio.json будь-якого кейсу."""
        path, _ = QtWidgets.QFileDialog.getOpenFileName(
            self, tr("Open Settings"),
            self.panel_run.ed_dir.text().strip() or str(DEFAULT_CASES),
            "Settings (*.json);;All files (*)")
        if not path:
            return
        data = lib.import_settings(path)
        if data is None:
            QtWidgets.QMessageBox.warning(
                self, tr("Wrong settings"),
                tr("The file “{name}” has no case settings.\n\nUse a file "
                   "saved by this program, or {MANIFEST} from the "
                   "directory of any case.", name=Path(path).name, MANIFEST=lib.MANIFEST))
            return
        if not self.mesh_guard(tr("settings from file")):
            return

        # Назва кейсу, файл STL і габарит належать відкритій геометрії, а не
        # файлу налаштувань: інакше налаштування, знятi з іншої моделі,
        # перейменували б кейс і зсунули домен під чужий габарит.
        keep = (self.cfg.name, self.cfg.stl_file, self.cfg.bbox_min,
                self.cfg.bbox_max, self.cfg.surface_area, self.cfg.inside_point)
        dropped = lib.apply_dict(self.cfg, data)
        (self.cfg.name, self.cfg.stl_file, self.cfg.bbox_min,
         self.cfg.bbox_max, self.cfg.surface_area,
         self.cfg.inside_point) = keep

        self.reload_panels()
        self.refresh_all()
        says = [tr("Settings from “{name}” applied — the case name and "
                   "the bounding box stayed yours.", name=Path(path).name)]
        if dropped:
            says.append(tr("Not accepted: {join}.", join=", ".join(dropped[:6])))
        self.statusBar().showMessage("  ".join(says), 15000)

    def reset_settings(self):
        """Повернути всі налаштування до типових, НЕ чіпаючи геометрії.

        Геометрія лишається саме тому, що читається вона довго — на
        збірці це десятки секунд, — і скидати її разом із галочками
        означало б карати за бажання почати налаштування спочатку.

        З тієї ж причини лишаються назва кейсу, габарит, площа й точка в
        потоці: вони належать відкритій моделі, а не налаштуванням. Скинути
        їх до типових означало б зсунути домен під чужий габарит — рівно
        те, від чого бережеться `load_settings`.
        """
        if QtWidgets.QMessageBox.question(
                self, tr("Reset Settings"),
                tr("Reset all settings to defaults?{NEWLINE}{NEWLINE}The "
                   "geometry in the scene, the case name and the "
                   "bounding box stay. The flow mode, speed, mesh, "
                   "solver, part roles and zones will be reset.", NEWLINE=NEWLINE),
                QtWidgets.QMessageBox.Yes | QtWidgets.QMessageBox.No,
                QtWidgets.QMessageBox.No) != QtWidgets.QMessageBox.Yes:
            return
        if not self.mesh_guard(tr("reset settings")):
            return

        keep = (self.cfg.name, self.cfg.stl_file, self.cfg.bbox_min,
                self.cfg.bbox_max, self.cfg.surface_area,
                self.cfg.inside_point)
        # ПРАВИМО НА МІСЦІ, А НЕ ПІДМІНЮЄМО ОБʼЄКТ, і це не дрібниця.
        #
        # Панелі привʼязують свої поля ЗАМИКАННЯМИ: `lambda: setattr(f,
        # "u_inf", self.u.value())`, де `f` — та тека потоку, яку панель
        # дістала при створенні. Підміниш конфіг у вікна — і замикання
        # писатимуть у старий обʼєкт, якого вже ніхто не читає. Поле
        # крутиться, число в ньому міняється, а в кейс іде інше; побачити
        # це можна лише прогоном.
        #
        # Тому беремо той самий шлях, що й `load_settings`: накладаємо
        # типові значення на НАЯВНИЙ конфіг.
        lib.apply_dict(self.cfg, lib.cfg_to_dict(fc.CaseCfg()))
        (self.cfg.name, self.cfg.stl_file, self.cfg.bbox_min,
         self.cfg.bbox_max, self.cfg.surface_area,
         self.cfg.inside_point) = keep
        # Ролі деталей теж належать налаштуванням, а не геометрії.
        for part in self.parts:
            part.role, part.refine, part.hinge = "body", None, None
        self.channels = {k: mid for k, _, _, _, mid, _ in fc.RC_CHANNELS}

        self.reload_panels()
        self.rebuild_geometry()
        self.refresh_all()
        self.log.push(tr("Settings reset to the defaults. The geometry, "
                      "case name and overall size stayed."))
        self.statusBar().showMessage(tr("Settings reset to the defaults."), 12000)

    def save_session(self):
        """Запамʼятовує сесію: налаштування, геометрію і де що лежало.

        Геометрію кладемо вже переведеною в метри й розвернутою — тобто рівно
        те, що було у вʼюпорті. Інакше після відновлення довелося б повторювати
        і вибір одиниць, і всі розвороти з ремонтом: у вихідному файлі їх немає.
        """
        # ЗБЕРІГАЄМО НЕЙТРАЛЬ, а не поточну позу.
        #
        # `self.mesh` — це сцена з ВІДХИЛЕНИМИ поверхнями, бо саме її
        # видно у вʼюпорті. Якби в сесію лягала вона, то закривши
        # програму на повному крені, людина відкрила б літак із
        # елеронами назавжди відхиленими: сесія зберігає одну зліплену
        # модель без поділу на деталі, а отже й без завіс, і вернути
        # нейтраль було б уже нічим.
        #
        # Поза — стан перегляду, геометрія запису — базова.
        live = self.enabled_parts()
        neutral = None
        if live:
            neutral = (live[0].mesh if len(live) == 1
                       else live[0].mesh.merge([p.mesh for p in live[1:]],
                                               merge_points=False))
        geometry = ""
        if neutral is not None and neutral.n_cells <= lib.MESH_CACHE_LIMIT:
            try:
                target = lib.session_stl(DATA_DIR)
                target.parent.mkdir(parents=True, exist_ok=True)
                geo.write_stl(neutral, str(target), binary=True)
                geometry = str(target)
            except Exception:
                geometry = ""      # сесія без геометрії краща за відмову

        # СЦЕНА ПО ДЕТАЛЯХ — тим самим форматом, що й проєкт: кожна деталь
        # окремо, з роллю, двигуном, диском, завісою й геометрією (з
        # ремонтом). Злитий body.stl вище лишається: його читає стара
        # версія програми, і на нього ж відкат, якщо цей файл не вийде.
        project = ""
        loaded = [p for p in self.parts if p.loaded and p.enabled]
        if loaded and sum(p.mesh.n_cells for p in loaded) <= lib.MESH_CACHE_LIMIT:
            try:
                target = lib.session_project(DATA_DIR)
                target.parent.mkdir(parents=True, exist_ok=True)
                lib.write_project(target, self.cfg,
                                  [self._part_row(p) for p in loaded],
                                  {"channels": dict(self.channels)})
                project = str(target)
            except Exception:
                project = ""

        backend = self.panel_run.current_backend()
        ui = {
            # "backend" (the label) stays for older versions of the
            # program that read the same session file; this version reads
            # "backend_id", which does not change with the language.
            "backend": backend.label if backend else "",
            "backend_id": backend.id if backend else "",
            "native": self.panel_run.chk_native.isChecked(),
            "left_tab": self.tabs_left.currentIndex(),
            "target_yplus": self.panel_adv.yplus.value(),
            "units": self.panel_geo.cmb_units.currentText(),
        }
        if self._pmesh.active:
            # Сітка з проєкту діє й після перезапуску: сцена та сама.
            ui["project_mesh"] = {"key": self._pmesh.key,
                                  "project": self._pmesh.project}
        lib.write_session(
            DATA_DIR, self.cfg,
            stl_source=self.stl_source or self.stl_path, geometry=geometry,
            case_dir=self.case_dir,
            cases_dir=self.panel_run.ed_dir.text().strip(), ui=ui,
            project=project)

    def _restore_session_if_wanted(self):
        """Прапорець перевіряємо в момент спрацювання, а не при заведенні
        таймера: за ці чверть секунди його могли зняти."""
        if self.act_session.isChecked() or fs_restart.restore_requested():
            self.restore_session(auto=True)

    def restore_session(self, auto: bool = False):
        """Повертає стан, з яким закрили програму."""
        data = lib.read_session(DATA_DIR)
        if not data:
            if not auto:
                QtWidgets.QMessageBox.information(
                    self, tr("No session yet"),
                    tr("The file {SESSION} appears when you close the "
                       "program: it stores the settings, geometry and "
                       "case you worked with.", SESSION=lib.SESSION))
            return
        if self._load_thread is not None:
            if not auto:
                self.statusBar().showMessage(
                    tr("Wait: the geometry is still being read."), 6000)
            return

        # СПЕРШУ СЦЕНА ПО ДЕТАЛЯХ (сесії з 2026-11-08): ролі, двигуни,
        # диски й ремонт кожної деталі окремо. Сесія старішого формату цього
        # файлу не має — тоді, як і раніше, злитий body.stl.
        scene = lib.session_scene(DATA_DIR, data)
        if scene:
            pack = lib.read_project(scene)
            if pack is not None and self._open_project_data(pack, scene,
                                                            session=data):
                return
            self.log.push(tr("Could not read the per-part session scene — "
                          "restoring the merged geometry, as before."))
        geometry = lib.session_geometry(DATA_DIR, data)
        if geometry:
            self._pending_restore = lambda: self._apply_session(data)
            self.load_stl(geometry)
        else:
            self._apply_session(data)

    def _apply_session(self, data: dict):
        dropped = lib.apply_dict(self.cfg, data.get("cfg"))
        if self.diag is not None:
            d = self.diag
            self.cfg.bbox_min = (d.bounds[0], d.bounds[2], d.bounds[4])
            self.cfg.bbox_max = (d.bounds[1], d.bounds[3], d.bounds[5])
            self.cfg.surface_area = d.area

        source = str(data.get("stl_source") or "")
        self.stl_source = source
        if self.mesh is not None:
            # `stl_path` лишаємо на кеші сесії, а не на вихідному файлі:
            # «Скинути» має повертати до відновленого стану (метри, розвороти),
            # а не до файлу в міліметрах, який ще й міг переїхати.
            self.panel_geo.set_file(
                tr("restored from the last session{v}\n“Reset” returns to "
                   "this same state", v=tr("\nsource file: {source}", source=source) if source else ""))

        # Каталог кейсів вертаємо як є, навіть якщо його ще немає: «Створити
        # кейс» його й створює, і забирати в людини щойно набраний шлях
        # тільки через те, що вона ще не натиснула кнопку, — безглуздо.
        cases = str(data.get("cases_dir") or "")
        if cases:
            self.panel_run.ed_dir.setText(cases)
        case_dir = str(data.get("case_dir") or "")
        self.case_dir = case_dir if os.path.isdir(case_dir) else ""

        ui = data.get("ui") if isinstance(data.get("ui"), dict) else {}
        # An id (this version) or the label of an older version; both
        # find the backend through be.backend_matches.
        self._want_backend = str(ui.get("backend_id") or ui.get("backend") or "")
        self._select_backend()
        if isinstance(ui.get("native"), bool):
            self.panel_run.chk_native.setChecked(ui["native"])
        units = str(ui.get("units") or "")
        if units and self.panel_geo.cmb_units.findText(units) >= 0:
            self.panel_geo.cmb_units.blockSignals(True)
            self.panel_geo.cmb_units.setCurrentText(units)
            self.panel_geo.cmb_units.blockSignals(False)
        if isinstance(ui.get("target_yplus"), (int, float)):
            self.panel_adv.yplus.setValue(float(ui["target_yplus"]))

        self.reload_panels()
        if self.mesh is not None:
            self.viewport.show_body(self.mesh_display, reset=True)
        self.refresh_all()
        tab = ui.get("left_tab")
        if isinstance(tab, int) and 0 <= tab < self.tabs_left.count():
            self.tabs_left.setCurrentIndex(tab)

        says = [tr("Session from {get} restored.", get=data.get('saved', '—'))]
        if self.mesh is None:
            says.append(tr("There is no geometry in the session — open an STL."))
        if self.case_dir:
            says.append(tr("Case: {name}.", name=Path(self.case_dir).name))
            if post.has_results(self.case_dir):
                says.append(tr("Results are in place — “Load Results”."))
        elif case_dir:
            says.append(tr("The case folder {case_dir} is gone.", case_dir=case_dir))
        if dropped:
            says.append(tr("Not accepted: {join}.", join=", ".join(dropped[:6])))
        self.statusBar().showMessage("  ".join(says), 20000)

    def _select_backend(self):
        """Повертає вибір бекенда, якщо той самий ще знайдено."""
        if not self._want_backend:
            return
        combo = self.panel_run.cmb_backend
        for i in range(combo.count()):
            backend = combo.itemData(i)
            if be.backend_matches(backend, self._want_backend):
                combo.setCurrentIndex(i)
                self._want_backend = ""
                return

    def on_case_moved(self, path: str):
        """Кейс перейменували, почистили або видалили у «Збережених».

        Якщо це той самий кейс, що відкритий у вікні, читач результатів
        тримає шлях, який уже нікуди не веде, і малював би поля, яких на
        диску немає.
        """
        if not self.case_dir:
            return
        if os.path.normcase(self.case_dir) != os.path.normcase(str(path)):
            return
        self.results = None
        self.slice_bar.setEnabled(False)
        self._drop_scene()
        # Вʼюпорт зараз спорожніє — тож і «До моделі» там нема до чого йти.
        self._model_box = None
        self.cmb_time.blockSignals(True)
        self.cmb_time.clear()
        self.cmb_time.blockSignals(False)
        try:
            self.res_plotter.clear()
            self.res_plotter.add_axes(line_width=2)
        except Exception:
            pass
        if not os.path.isdir(self.case_dir):
            self.case_dir = ""
            self.lbl_coeffs.setText(
                "The case was deleted from the disk. The settings stayed "
                "in the tabs — you can create it again.")
        else:
            self.lbl_coeffs.setText(
                "The case changed on the disk. Click “Load Results” to "
                "read it again.")

    # --- прогін ---------------------------------------------------------

    def make_propeller(self):
        """Гвинт із чисел — окремою моделлю в сцені, з уже даною роллю.

        Ставимо його ПЕРЕД носом уздовж потоку, а не в нуль: у нулі він
        опинився б усередині фюзеляжа, і snappyHexMesh дістав би дві
        поверхні, що перетинаються там, де їм не треба.

        Роль «гвинт» призначаємо одразу — інакше модель лягла б у сцену
        нерухомою поверхнею, і це виглядало б як «гвинт не тягне».
        """
        p = self.panel_geo.propeller_params()
        d = float(p["diameter"])
        if d <= 0:
            return
        # Перед носом: півдіаметра запасу, щоб лопаті не різали обшивку.
        if self.mesh is not None:
            b = self.mesh.bounds
            nose = b[0]
            centre = (nose - d * 0.12, 0.5 * (b[2] + b[3]),
                      0.5 * (b[4] + b[5]))
        else:
            centre = (0.0, 0.0, 0.0)
        try:
            mesh = geo.make_propeller(
                diameter=d, blades=int(p["blades"]), pitch=float(p["pitch"]),
                hub_diameter=float(p["hub_diameter"]),
                chord=float(p["chord"]), centre=centre, axis="x")
        except Exception as exc:
            QtWidgets.QMessageBox.critical(
                self, tr("Propeller was not built"), str(exc))
            return

        path = os.path.join(
            tempfile.gettempdir(),
            f"propeller_{int(p['blades'])}x{int(round(d * 1000))}.stl")
        mesh.save(path)
        part = Part(path=path, mesh=mesh, factor=1.0, auto=1.0,
                    why="made from numbers", enabled=True, pinned=True,
                    role="rotor", rpm=float(p["rpm"]), rotor_axis="x")
        d2 = geo.diagnose(mesh)
        part.open_edges = d2.open_edges
        part.bad_winding = getattr(d2, "bad_winding", 0)
        part.inverted = getattr(d2, "inverted", 0)
        self.parts.append(part)

        if len(self.enabled_parts()) > 1 and not self.cfg.split_patches:
            self.cfg.split_patches = True
            self.panel_geo.chk_split.setChecked(True)
        self.log.push(
            tr("Propeller from numbers: {int} blades, diameter {d}, "
               "pitch {fmt_len}, {p:.0f} rpm. Center {join}. File: {path}", int=int(p['blades']), d=phys.fmt_len(d), fmt_len=phys.fmt_len(float(p['pitch'])), p=p['rpm'], join=" ".join(f"{v:+.4g}" for v in centre), path=path))
        self.log.push(
            tr("The blade section here is an ELLIPSE, not an airfoil: take "
            "its numbers as an order of magnitude, not as the answer. "
            "For real numbers you need a blade model from CAD."))
        self.rebuild_geometry(reset=True)
        self.auto_reference()
        self.refresh_all()
        self.panel_geo.parts.setCurrentRow(len(self.parts) - 1)
        self.show_part_role(len(self.parts) - 1)

    def part_role_set(self, index: int, role: str, rpm: float,
                      axis: str = "x", level: int = 0):
        """Змінити роль моделі: тіло, гвинт чи рухома частина."""
        if not (0 <= index < len(self.parts)):
            return
        part = self.parts[index]
        # Нуль із панелі означає «як усі», тобто ВІДСУТНІСТЬ власного
        # рівня, а не рівень нуль. Мінімум беремо на один нижче максимума:
        # так само влаштований і спільний рівень (4-5), і snappyHexMesh
        # користується цим проміжком, щоб перейти до дрібної комірки не
        # стрибком.
        want = None if level <= 0 else (max(int(level) - 1, 0), int(level))
        if (part.role == role and abs(part.rpm - rpm) < 1e-9
                and part.rotor_axis == axis and part.refine == want):
            return
        part.role, part.rpm, part.rotor_axis = role, float(rpm), axis
        part.refine = want

        # Ролі мають сенс ЛИШЕ з роздільними патчами: без них усі моделі
        # злиті в одну границю, і сказати «крутиться оця» нема про що.
        # Тому вмикаємо їх самі — і кажемо про це, а не робимо нишком.
        if role != "body" and not self.cfg.split_patches and len(
                self.enabled_parts()) > 1:
            self.cfg.split_patches = True
            self.panel_geo.chk_split.setChecked(True)
            self.log.push(
                tr("Separate patches turned on: without them the roles have "
                "no meaning — all models would be one boundary."))
        self.rebuild_geometry()
        self.refresh_all()
        self.show_part_role(index)

    def part_engine_set(self, index: int, data: dict):
        """Кришці дали двигун або соплу — нові числа."""
        if not (0 <= index < len(self.parts)):
            return
        part = self.parts[index]
        if part.role not in fc.PORT_ROLES:
            return
        part.engine = fc.patch_name_from(str(data.get("engine") or "engine1"))
        if part.role == "nozzle":
            part.jet = _jet_from_row(data)
        self.rebuild_geometry()
        self.refresh_all()
        self.show_part_role(index)

    def part_disk_set(self, index: int, data: dict):
        """Гвинту-диску дали нові числа."""
        if not (0 <= index < len(self.parts)):
            return
        part = self.parts[index]
        if part.role != fc.DISK_ROLE:
            return
        part.disk = _disk_from_row(data)
        self.rebuild_geometry()
        self.refresh_all()
        self.show_part_role(index)

    def make_disk(self):
        """Гвинт-диск із чисел: плаский диск того ж діаметра, що й гвинт.

        Ставимо туди ж, куди й гвинт із чисел, — перед носом. Роль дається
        одразу, і тягу людина задає вже на ньому.
        """
        p = self.panel_geo.propeller_params()
        d = float(p["diameter"])
        if d <= 0:
            return
        if self.mesh is not None:
            b = self.mesh.bounds
            centre = (b[0] - d * 0.12, 0.5 * (b[2] + b[3]),
                      0.5 * (b[4] + b[5]))
        else:
            centre = (0.0, 0.0, 0.0)
        hub = max(float(p["hub_diameter"]) / 2.0, 0.0)
        mesh = pv.Disc(center=centre, inner=min(hub, 0.45 * d), outer=d / 2,
                       normal=(1.0, 0.0, 0.0), r_res=2,
                       c_res=96).triangulate().clean()
        path = os.path.join(tempfile.gettempdir(),
                            f"propdisk_{int(round(d * 1000))}.stl")
        mesh.save(path)
        part = Part(path=path, mesh=mesh, factor=1.0, auto=1.0,
                    why="made from numbers", enabled=True, pinned=True,
                    role=fc.DISK_ROLE, rpm=float(p["rpm"]), rotor_axis="x",
                    disk=fc.ActuatorDisk(hub=hub))
        self.parts.append(part)
        if len(self.enabled_parts()) > 1 and not self.cfg.split_patches:
            self.cfg.split_patches = True
            self.panel_geo.chk_split.setChecked(True)
        self.log.push(tr("Actuator disk from numbers: diameter {d}, "
                         "spinner {hub}. Set the thrust and torque in "
                         "the role block.", d=phys.fmt_len(d), hub=phys.fmt_len(hub)))
        self.rebuild_geometry(reset=True)
        self.auto_reference()
        self.refresh_all()
        self.panel_geo.parts.setCurrentRow(len(self.parts) - 1)
        self.show_part_role(len(self.parts) - 1)

    def _disk_note(self, part) -> str:
        """Що з цього гвинта-диска вийде — числами."""
        d = next((x for x in self.cfg.disks if x.name == self._patch_of(part)),
                 None)
        if d is None:
            live = self.enabled_parts()
            if len(live) < 2 or not self.cfg.split_patches:
                return (tr("There will be no disk while the scene has only "
                        "one model: separate patches and at least one "
                        "more model are necessary."))
            return (tr("No disk created: the model is not thin along the "
                    "selected axis, so it does not look like a disk. Try "
                    "another rotation axis."))
        gas = (tr(" at throttle {throttle:.2f} (×{throttle_factor:.2f})", throttle=d.throttle, throttle_factor=d.throttle_factor)
               if d.from_throttle else "")
        way = tr("COUNTERCLOCKWISE") if d.spin >= 0 else tr("CLOCKWISE")
        swirl = (tr(", torque {torque_now:.3g} N·m, swirl {way} as seen "
                    "from the arrow tip", torque_now=d.torque_now, way=way) if d.torque_now > 0 else tr(", no swirl"))
        off = (tr(" At supersonic speed the disks are off: rhoCentralFoam ignores sources.")
               if self.cfg.flow.supersonic else "")
        return (tr("Actuator disk: radius {radius}, spinner {hub}, "
                   "thickness {fmt_len}. Thrust {thrust_now:.3g} "
                   "N{gas}{swirl}. The jet goes along the arrow; the "
                   "model does not go into the mesh.{off}", radius=phys.fmt_len(d.radius), hub=phys.fmt_len(d.hub), fmt_len=phys.fmt_len(self.cfg.disk_thickness(d)), thrust_now=d.thrust_now, gas=gas, swirl=swirl, off=off))

    def part_flip(self, index: int):
        """Перевернути активний бік кришки: обійти трикутники навпаки.

        Міняється сам `mesh`, і саме тому зміна переживає проєкт: він
        зберігає геометрію деталі, а з нею й порядок обходу.
        """
        if not (0 <= index < len(self.parts)):
            return
        part = self.parts[index]
        if part.role not in fc.PORT_ROLES or not part.loaded:
            return
        part.mesh = part.mesh.flip_faces()
        part._posed, part._posed_key = None, None
        self.log.push(tr("“{name}”: active side flipped.", name=part.name))
        self.rebuild_geometry()
        self.refresh_all()
        self.show_part_role(index)

    def _write_tri(self, tri_dir) -> None:
        """Геометрія кейсу (constant/triSurface) — так, як її пише create_case."""
        if self.cfg.split_bodies:
            self._write_case_stls(tri_dir)
        else:
            geo.write_stl(self.mesh, str(Path(tri_dir) / self.cfg.stl_file),
                          binary=True)

    def mesh_inputs_key(self) -> str:
        """Ключ сітки (fs_backend.mesh_key), який дали б ТЕПЕРІШНІ налаштування
        й сцена — без запису кейсу. "" — геометрії немає.

        Пишемо в тимчасову теку рівно те, з чого рахується ключ: словники
        сітки й triSurface. Так ключ завжди той самий, що й у справжнього
        кейсу, і окремого «другого» обчислення, яке могло б розійтися з
        create_case, немає.
        """
        if self.mesh is None:
            return ""
        with tempfile.TemporaryDirectory(prefix="ofs-mkey-") as d:
            root = Path(d)
            for rel, text in fc.mesh_dicts(self.cfg).items():
                f = root / rel
                f.parent.mkdir(parents=True, exist_ok=True)
                f.write_text(text, encoding="utf-8", newline="\n")
            tri = root / "constant" / "triSurface"
            tri.mkdir(parents=True, exist_ok=True)
            push = self.log.push
            self.log.push = lambda *_a, **_k: None   # нотатки create_case тут зайві
            try:
                self._write_tri(tri)
            finally:
                self.log.push = push
            return be.mesh_key(d)

    def _write_case_stls(self, tri_dir) -> None:
        """STL кожної поверхні сцени — у свій файл кейсу.

        Порядок той самий, що й у _sync_bodies, бо обидва йдуть по
        enabled_parts(): інакше крило дісталося б патчу фюзеляжу, і помітити
        це можна було б хіба за дивними силами. Ідуть УСІ поверхні, а не лише
        стінки: кришкам двигунів теж потрібен свій файл.

        У розрахунок іде ПОЗА, а не базова геометрія: інакше пульт рухав би
        картинку на екрані, а рахувався б літак з нейтральними поверхнями.

        Двигуни додають дві речі:
          * у режимі кришок тіло під кришкою, притуленою без зазору,
            вирізається (Q29) — інакше дві поверхні в одній площині;
          * у режимі порталів поруч кладеться «плитка» кожного порталу —
            з неї topoSet бере шар комірок для джерел.
        """
        # Зшиваємо з УСІМА патчами (`bodies`), бо деталі йдуть у тому самому
        # порядку, а гвинти-диски потім просто пропускаємо: у сітку від них
        # не йде нічого.
        pairs = [(b, part) for b, part in zip(self.cfg.bodies,
                                              self.enabled_parts())
                 if b.role != fc.DISK_ROLE]
        meshes = {b.name: part.posed(self.channels) or part.mesh
                  for b, part in pairs}
        ports = [b for b, _ in pairs if b.role in fc.PORT_ROLES]
        portal = self.cfg.portal_mode
        if ports and not portal:
            for b, _ in pairs:
                if b.role in fc.PORT_ROLES:
                    continue
                for cap in ports:
                    # Допуск — соту частку комірки біля кришки: «впритул» у
                    # CAD означає збіг до похибки експорту, а справжній
                    # зазор людина робить помітно більшим.
                    tol = 0.01 * self.cfg.portal_cell(cap)
                    cut, n = geo.cut_under_cap(meshes[b.name],
                                               meshes[cap.name], tol)
                    if n:
                        meshes[b.name] = cut
                        self.log.push(
                            tr("Under cap {name}, {n} triangles were cut "
                               "from body {name2}: the cap lay flush, in "
                               "the same plane.", name=cap.name, name2=b.name, n=n))
        for b, _ in pairs:
            geo.write_stl(meshes[b.name], str(tri_dir / b.stl_file),
                          binary=True)
        if portal:
            for b in ports:
                slab = geo.portal_slab(meshes[b.name],
                                       self.cfg.portal_thickness(b))
                geo.write_stl(slab, str(tri_dir / fc.portal_slab_file(b.name)),
                              binary=True)

    def _patch_of(self, part) -> str:
        """Імʼя патча цієї деталі в кейсі — за тим самим порядком, що й
        `_sync_bodies`. Порожньо, якщо деталь у кейс не йде."""
        live = self.enabled_parts()
        for p, b in zip(live, self.cfg.bodies):
            if p is part:
                return b.name
        return ""

    def _engine_note(self, part) -> str:
        """Що з цієї кришки вийде в розрахунку — числами."""
        e = next((x for x in self.cfg.engines if x.name == part.engine), None)
        if e is None or not part.loaded:
            return (tr("The cap is not in an engine yet: it needs separate "
                    "patches and at least one more model in the scene."))
        pt = fc.engine_point(e, self.cfg.flow)
        gas = (tr(" at throttle {throttle:.2f} (×{throttle_factor:.2f})", throttle=e.throttle, throttle_factor=e.throttle_factor)
               if e.from_throttle else "")
        if part.role == "nozzle":
            missing = "" if e.intakes else (
                tr(" There is no intake yet — mark an intake cap with the "
                "same engine name."))
            if e.thrust_mode == "static" and not e.direct:
                # Статична — це вхід; тяга в польоті — наслідок. Показуємо
                # обидві, щоб падіння зі швидкістю було видно числом.
                g = tr("throttle {gas:.2f}", gas=e.gas) if e.from_throttle else tr("full throttle")
                return (tr("Engine {name}: maximum static {thrust:.1f} N "
                           "→ {g}: on the test stand {v:.1f} N, in "
                           "flight at {u_inf:g} m/s {thrust2:.1f} N. "
                           "Nozzle {v2:.3g} cm² → mass flow "
                           "{mdot_nozzle:.4g} kg/s, jet {v_exit:.0f} m/s "
                           "(M = {mach_exit:.2f}), {v3:.0f} °C. Intakes: "
                           "{n_intakes}.{missing}", name=e.name, thrust=e.thrust, g=g, v=e.thrust * e.throttle_factor, u_inf=self.cfg.flow.u_inf, thrust2=pt.thrust, v2=e.nozzle_area * 1e4, mdot_nozzle=pt.mdot_nozzle, v_exit=pt.v_exit, mach_exit=pt.mach_exit, v3=pt.t_exit - 273.15, n_intakes=len(e.intakes), missing=missing))
            return (tr("Engine {name}: thrust {thrust:.1f} N{gas}. "
                       "Nozzle {v:.3g} cm² → mass flow {mdot_nozzle:.4g} "
                       "kg/s, jet {v_exit:.0f} m/s (M = "
                       "{mach_exit:.2f}), {v2:.0f} °C. Intakes: "
                       "{n_intakes}.{missing}", name=e.name, thrust=pt.thrust, gas=gas, v=e.nozzle_area * 1e4, mdot_nozzle=pt.mdot_nozzle, v_exit=pt.v_exit, mach_exit=pt.mach_exit, v2=pt.t_exit - 273.15, n_intakes=len(e.intakes), missing=missing))
        mine = self._patch_of(part)
        i = e.intakes.index(mine) if mine in e.intakes else -1
        if i < 0:
            return tr("Engine {name}.", name=e.name)
        missing = "" if e.nozzle else (
            tr(" No nozzle yet — mark an exit cap with the same engine name."))
        return (tr("Engine {name}: {mdot_intakes:.4g} kg/s goes in here, "
                   "speed at the cap {v_intakes:.0f} m/s (M = "
                   "{mach_intakes:.2f}).{missing}", name=e.name, mdot_intakes=pt.mdot_intakes[i], v_intakes=pt.v_intakes[i], mach_intakes=pt.mach_intakes[i], missing=missing))

    def show_part_role(self, index: int):
        """Показати в панелі роль саме цієї моделі."""
        if not (0 <= index < len(self.parts)):
            self.panel_geo.show_part_role("", "body", 0.0)
            self.viewport.show_rotor_axis(None, (1.0, 0.0, 0.0), 1.0, 0.0)
            return
        part = self.parts[index]
        note = ""
        if part.role == "rotor":
            zone = next((z for z in self.cfg.rotating
                         if z.from_part and part.loaded
                         and self._zone_matches(z, part)), None)
            if zone is None:
                # ПРИЧИН ДВІ, І ВОНИ РІЗНІ. Валити все на форму — значить
                # відправити людину крутити вісь там, де проблема зовсім
                # в іншому. Я на цьому сам спіймався: побачив «не диск»
                # і порадив міняти вісь, хоча в сцені просто була одна
                # модель.
                live = self.enabled_parts()
                fits = None
                if part.loaded:
                    fits = fc.rotor_zone_from_points(
                        "probe", part.mesh.points, part.rpm or 1.0,
                        part.rotor_axis)
                if len(live) < 2:
                    note = (tr("There will be no zone while the scene has "
                            "only ONE model: a rotating zone needs "
                            "separate patches, and they make sense only "
                            "when there are several bodies. Add the "
                            "other models — the zone builds itself."))
                elif not self.cfg.split_patches:
                    note = (tr("There will be no zone without separate "
                            "patches: without them all models are one "
                            "boundary, and there is nothing to say “this "
                            "one spins” about. Turn on “Each model — "
                            "separate patch”."))
                elif fits is None:
                    note = (tr("Zone not created: along axis {upper} "
                               "this model is not thinner than its "
                               "radius, so it does not look like a disk. "
                               "Try another axis: a disk is thin ALONG "
                               "the rotation axis and round in the two "
                               "other directions.", upper=part.rotor_axis.upper()))
                else:
                    note = (tr("The zone is not rebuilt for this model yet "
                            "— switch the role or move the part so that "
                            "it builds again."))
            else:
                ma = phys.mach(zone.tip_speed, self.cfg.flow.sound_speed)
                # НАПРЯМОК СЛОВАМИ — саме тут, а не у вювпорті: у VTK
                # немає кирилиці й немає переносу рядка. У вювпорті
                # напрямок показує зігнута стрілка, тут — називається.
                way = (tr("COUNTERCLOCKWISE") if part.rpm >= 0
                       else tr("CLOCKWISE"))
                note = (tr("Zone: radius {radius}, thickness {length}. "
                           "Blade tip {tip_speed:.4g} m/s, M = {ma:.2f}. "
                           "Rotation {way}, looking FROM THE TIP of the "
                           "axis arrow toward its base; in the viewport "
                           "it is the same arc. The sign of the rpm "
                           "changes the direction.", radius=phys.fmt_len(zone.radius), length=phys.fmt_len(zone.length), tip_speed=zone.tip_speed, ma=ma, way=way))
        elif part.role == "motion":
            note = (tr("Only this part oscillates, the rest stays still. "
                    "The motion law is set on the “Solver” tab.")
                    if self.cfg.motion.on else
                    tr("Oscillation is off on the “Solver” tab — for now "
                    "this part stays still with the rest."))
        elif part.role == fc.DISK_ROLE:
            note = self._disk_note(part)
        elif part.role in fc.PORT_ROLES:
            note = self._engine_note(part)
            if self.cfg.flow.compressible and not self.cfg.flow.supersonic:
                note += (tr(" Compressible mode — this is a PORTAL: the "
                         "surface is permeable, air disappears at the "
                         "inlet and appears at the outlet. The active "
                         "side is where the arrow points."))
            else:
                note += (tr(" Incompressible mode — this is a CAP on the "
                         "engine: it must close the end of the closed "
                         "body that you modeled. Portals exist only in "
                         "compressible mode."))
        self.panel_geo.show_part_role(part.name, part.role, part.rpm,
                                      note, part.rotor_axis, part.refine)
        self.panel_geo.show_part_engine(part.role, part.engine,
                                        part.jet or fc.Engine())
        self.panel_geo.show_part_disk(part.role,
                                      part.disk or fc.ActuatorDisk(hub=0.0))
        self._update_rotor_axis_preview(index)
        hints = [t for _, t in fc.guard_hinges([(part.name, part.hinge)])] \
            if part.hinge is not None else []
        if not hints and part.hinge is not None and part.hinge.on:
            hints = [tr("Now deflected by {channels:+.1f}°.", channels=part.angle_at(self.channels))]
        self.panel_geo.show_part_hinge(part.hinge, " ".join(hints))

    def _update_rotor_axis_preview(self, index: int | None = None) -> None:
        """Стрілка обраної осі обертання у в'юпорті геометрії.

        Показуємо ЇЇ, а не лише текст, саме тому, що вісь задає людина, а
        не геометрія (гвинт габаритом не відрізнити від пластини — див.
        `fc.rotor_zone_from_points`). Стрілка, що стирчить убік від площини
        лопатей, видно одразу; текстове «X» саме по собі — ні.
        """
        if index is None:
            index = self.panel_geo.parts.currentRow()
        part = self.parts[index] if 0 <= index < len(self.parts) else None
        if (part is not None and part.role in fc.PORT_ROLES
                and part.mesh is not None):
            # АКТИВНИЙ БІК кришки чи порталу — куди дивляться нормалі STL
            # (Q27). Стрілка завжди лежить з активного боку й показує ТЕЧІЮ:
            #   сопло — стартує на поверхні й летить геть: звідси дме;
            #   вхід  — прилітає з активного боку й упирається кінчиком у
            #           грань: звідси повітря йде в двигун.
            # Перша версія малювала обидві однаково, «геть від поверхні», і
            # стрілка входу читалася як струмінь — навпаки до того, що є.
            mesh = part.posed(self.channels) or part.mesh
            s = geo.area_vector(mesh)
            n = float(np.linalg.norm(s))
            if n > 0:
                nrm = tuple(float(x) / n for x in s)
                length = max(mesh.length * 0.8, 1e-3)
                c = tuple(float(x) + dd * length / 2
                          for x, dd in zip(mesh.center, nrm))
                nozzle = part.role == "nozzle"
                d = nrm if nozzle else tuple(-x for x in nrm)
                # Підпис — там, де течія ПОЧИНАЄТЬСЯ: у сопла на кінчику
                # (туди дме), у входу на хвості (звідти йде), а не на грані.
                self.viewport.show_rotor_axis(
                    c, d, length, 0.0, label="out" if nozzle else "in",
                    label_at_tail=not nozzle)
                return
        if (part is None or part.role not in ("rotor", fc.DISK_ROLE)
                or part.mesh is None):
            self.viewport.show_rotor_axis(None, (1.0, 0.0, 0.0), 1.0, 0.0)
            return
        axis_vec = {"x": (1.0, 0.0, 0.0), "y": (0.0, 1.0, 0.0),
                   "z": (0.0, 0.0, 1.0)}.get(part.rotor_axis, (1.0, 0.0, 0.0))
        zone = next((z for z in self.cfg.rotating
                    if z.from_part and self._zone_matches(z, part)), None)
        if zone is not None:
            centre, length = zone.centre, max(zone.radius * 2.4,
                                              zone.length * 3.0)
        else:
            centre, length = tuple(part.mesh.center), part.mesh.length * 0.9
        self.viewport.show_rotor_axis(centre, axis_vec, length,
                                      part.live_rpm(self.channels))

    @staticmethod
    def _zone_matches(zone, part) -> bool:
        b = part.mesh.bounds
        c = (0.5 * (b[0] + b[1]), 0.5 * (b[2] + b[3]), 0.5 * (b[4] + b[5]))
        return all(abs(a - d) < 1e-9 for a, d in zip(zone.centre, c))

    def part_move(self, index: int, key: str, value: float):
        """Повернути чи зсунути ОДНУ модель, не чіпаючи решти збірки.

        Кнопки вгорі крутять усю збірку навколо спільного центра — ними
        літак ставлять уздовж потоку. А тут модель крутиться навколо
        ВЛАСНОГО центра: саме так гвинт ставлять на вал, а елерон — у
        гніздо, не зрушивши фюзеляжа.
        """
        if not (0 <= index < len(self.parts)):
            return
        part = self.parts[index]
        if not part.loaded or part.mesh is None:
            QtWidgets.QMessageBox.information(
                self, tr("Model not read"),
                tr("“{name}” is not in the scene yet. Check it in the list.", name=part.name))
            return
        if key in ("x", "y", "z"):
            part.mesh = geo.rotate(part.mesh, key, value,
                                   pivot=part.mesh.center)
            what = tr("rotated by {value:g}° around {upper}", value=value, upper=key.upper())
        elif len(key) == 2 and key[0] in "+-" and key[1] in "xyz":
            sign = 1.0 if key[0] == "+" else -1.0
            i = "xyz".index(key[1])
            shift = tuple(sign * value if j == i else 0.0 for j in range(3))
            part.mesh = geo.translate(part.mesh, shift)
            what = tr("shifted by {v:+.4g} m along {upper}", v=sign * value, upper=key[1].upper())
        else:
            return
        self.log.push(f"«{part.name}»: {what}")
        self.rebuild_geometry()
        self.auto_reference()
        self.refresh_all()
        self.show_part_role(index)

    # --- КЕРУВАННЯ ТА ПУЛЬТ ------------------------------------------------

    def set_channels(self, values: dict):
        """Стіки зрушили — переставити поверхні й перезібрати сцену.

        Сцена збирається з ПОЗИ щоразу наново, від базової геометрії.
        Саме тому стік можна ганяти туди-сюди скільки завгодно: нейтраль
        не їде, бо кут ніколи не кладеться в саму модель.
        """
        self.channels = {k: float(v) for k, v in values.items()}
        self.rebuild_geometry()
        self.refresh_all()
        self._show_controls()

    def _controlled(self):
        """[(частина, завіса), ...] серед увімкнених — для застороги й підпису."""
        return [(p, p.hinge) for p in self.enabled_parts()
                if p.hinge is not None and p.hinge.on]

    def _show_controls(self):
        """Показати на пульті, ЩО саме зараз рухається і на скільки.

        Без цього список каналів — це стіки, які невідомо що роблять:
        канал легко призначити не тій деталі, і на екрані це виглядатиме
        як «нічого не сталося».
        """
        lines = []
        for part, h in self._controlled():
            label = dict((k, lab) for k, lab, *_ in fc.RC_CHANNELS).get(
                h.channel, h.channel)
            if h.channel == "throttle":
                lines.append(tr("{name}: {label} -> {channels:.0f} rpm", name=part.name, label=label, channels=part.live_rpm(self.channels)))
            else:
                lines.append(f"{part.name}: {label} -> "
                             f"{part.angle_at(self.channels):+.1f}°")
        self.panel_tx.show_mapping(lines)
        for lvl, text in fc.guard_hinges(
                [(p.name, h) for p, h in self._controlled()]):
            if lvl == "bad":
                self.log.push(tr("Control: {text}", text=text))

    def part_hinge_set(self, index: int, data: dict):
        """Записати завісу вибраної деталі з полів панелі."""
        if not (0 <= index < len(self.parts)):
            return
        part = self.parts[index]
        if not data.get("on") or not data.get("channel"):
            changed = part.hinge is not None
            part.hinge = None
            if changed:
                self.rebuild_geometry()
                self.refresh_all()
                self._show_controls()
            self.show_part_role(index)
            return
        part.hinge = fc.Hinge(
            pivot=tuple(float(v) for v in data["pivot"]),
            axis=tuple(float(v) for v in data["axis"]),
            offset=tuple(float(v) for v in data["offset"]),
            channel=str(data["channel"]),
            gain=float(data["gain"]), trim=float(data["trim"]),
            limit=float(data["limit"]), reverse=bool(data["reverse"]))
        # Роль «тіло» тут доречна: керована поверхня НЕ обертова зона й
        # не коливальна частина — вона просто стоїть відхиленою.
        if part.role != "body" and part.hinge.channel != "throttle":
            self.log.push(
                tr("“{name}” has the role “{role}”, but a "
                   "control-surface channel is assigned to it. For a "
                   "deflecting surface the role must be “body”: it does "
                   "not spin or oscillate, it stays in the set position.", name=part.name, role=part.role))
        self.rebuild_geometry()
        self.refresh_all()
        self._show_controls()
        self.show_part_role(index)

    def guess_hinge(self, index: int):
        """Підібрати завісу з габариту деталі.

        ПРАВИЛО. У керованої поверхні три розміри: розмах, хорда,
        товщина — саме в такому порядку. Завіса йде вздовж РОЗМАХУ, тобто
        найдовшого напрямку, і лежить на передній кромці, тобто в
        мінімумі СЕРЕДНЬОГО напрямку. Товщина не бере участі ні в чому.

        Це здогад, а не істина: у стрілоподібного крила завіса не
        паралельна жодній осі. Тому поруч і сказано подивитися у вювпорт.
        """
        if not (0 <= index < len(self.parts)):
            return
        part = self.parts[index]
        if not part.loaded:
            QtWidgets.QMessageBox.information(
                self, tr("Model not read"),
                tr("“{name}” is not in the scene yet. Check it in the list.", name=part.name))
            return
        b = part.mesh.bounds
        ext = [(b[1] - b[0], 0), (b[3] - b[2], 1), (b[5] - b[4], 2)]
        ext.sort(reverse=True)
        span_ax, chord_ax = ext[0][1], ext[1][1]
        lo = (b[0], b[2], b[4])
        hi = (b[1], b[3], b[5])
        pivot = [0.5 * (lo[i] + hi[i]) for i in range(3)]
        pivot[chord_ax] = lo[chord_ax]          # передня кромка
        axis = [0.0, 0.0, 0.0]
        axis[span_ax] = 1.0
        h = part.hinge or fc.Hinge()
        part.hinge = fc.Hinge(
            pivot=tuple(pivot), axis=tuple(axis), offset=h.offset,
            channel=h.channel, gain=h.gain, trim=h.trim, limit=h.limit,
            reverse=h.reverse)
        names = "XYZ"
        self.log.push(
            tr("“{name}”: hinge along {names} at the point ({pivot:.4g}, "
               "{pivot2:.4g}, {pivot3:.4g}) m — leading edge along "
               "{names2}. Check in the viewport: the guess works for a "
               "straight control surface, not for a swept one.", name=part.name, names=names[span_ax], pivot=pivot[0], pivot2=pivot[1], pivot3=pivot[2], names2=names[chord_ax]))
        self.rebuild_geometry()
        self.refresh_all()
        self._show_controls()
        self.show_part_role(index)

    def fit_rotor_zone(self):
        """Підігнати обертову зону під вибрану модель гвинта.

        Тут лише вікно: саму зону рахує `fc.rotor_zone_from_points`, та
        сама, якою користуються ролі частин. Двох правд про те, що таке
        гвинт, у програмі бути не повинно — перша версія цієї кнопки мала
        власну логіку з габаритної коробки, і та логіка відхиляла
        ДВУЛОПАТЕВИЙ гвинт, бо коробкою він на диск не схожий.

        Вісь бере людина, а не геометрія: плаский двулопатевий гвинт
        габаритом не відрізнити від пластини — він і справді міг би
        крутитися навколо обох осей.
        """
        row = self.panel_geo.parts.currentRow()
        if not (0 <= row < len(self.parts)):
            QtWidgets.QMessageBox.information(
                self, tr("No model selected"),
                tr("Select the propeller model in the list on the "
                "“Geometry” tab — the button will use its bounding box."))
            return
        part = self.parts[row]
        if not part.loaded or part.mesh is None:
            QtWidgets.QMessageBox.information(
                self, tr("Model not read"),
                tr("“{name}” is not in the scene yet. Check it in the "
                   "list to read it.", name=part.name))
            return

        zone = fc.rotor_zone_from_points("rotor", part.mesh.points,
                                         self.panel_flow.rot_rpm.value(),
                                         part.rotor_axis)
        if zone is None:
            QtWidgets.QMessageBox.warning(
                self, tr("Around this axis this is not a propeller"),
                tr("“{name}” is thicker along axis {upper} than it is "
                   "wide.{NEWLINE}{NEWLINE}A propeller is thin along its "
                   "axis and wide in the cross-section across it. Check "
                   "the axis in the role block below the model list, or "
                   "set the zone manually.", name=part.name, upper=part.rotor_axis.upper(), NEWLINE=NEWLINE))
            return

        centre, radius, length = zone.centre, zone.radius, zone.length
        cell = self.cfg.local_cell_at(tuple(centre))
        coarse = cell > 0 and length < cell * 2.0

        self.panel_flow.set_rotating_zone(tuple(centre), radius, length)
        self.log.push(
            tr("Propeller zone from “{name}”: center {join}, radius "
               "{radius}, thickness {length}, axis {upper}", name=part.name, join=" ".join(f"{v:+.4g}" for v in centre), radius=phys.fmt_len(radius), length=phys.fmt_len(length), upper=part.rotor_axis.upper()))
        if coarse:
            # Сказати вголос, а не роздути зону. Роздута зона крутила б
            # повітря там, де лопатей немає, і це саме той різновид
            # «поправки», після якої число виглядає правдоподібно й бреше.
            self.log.push(
                tr("WARNING: the cell here is {cell}, but the zone is "
                   "{length} thick — not one layer of cells fits in it. "
                   "Refine the mesh around the propeller, or MRF does "
                   "nothing.", cell=phys.fmt_len(cell), length=phys.fmt_len(length)))
            QtWidgets.QMessageBox.warning(
                self, tr("The mesh is too coarse for this propeller"),
                tr("The zone is {length} thick, but the cell at this "
                   "place is {cell}.{NEWLINE}{NEWLINE}Not one full layer "
                   "of cells fits in such a zone, and MRF does nothing — "
                   "silently, with no error.{NEWLINE}{NEWLINE}The zone "
                   "is placed correctly. Refine the mesh around the "
                   "propeller: a refinement box on the “Mesh” tab or a "
                   "higher surface level.", length=phys.fmt_len(length), cell=phys.fmt_len(cell), NEWLINE=NEWLINE))
        self.statusBar().showMessage(
            tr("Zone fitted to “{name}”: radius {radius}, axis {upper}.{v}", name=part.name, radius=phys.fmt_len(radius), upper=part.rotor_axis.upper(), v=" The mesh is too coarse — see the log." if coarse else ""), 15000)
        self.refresh_all()

    def start_run_on_server(self):
        """Один клац замість чотирьох кроків, третій з яких легко проминути.

        Шлях «вписати адресу — перевірити — ВИБРАТИ СЕРВЕР У СПИСКУ —
        запустити» має одну тиху пастку: якщо забути про третій крок,
        «Запустити» порахує на цьому ж комп'ютері, і скаржитися не буде
        ніхто. Різниця між ноутом і сервером тут — години.

        Тому кнопка робить усе сама й каже вголос, якщо не може.
        """
        host = self.panel_run.ed_server.text().strip()
        if not host:
            self.panel_run.ed_server.setFocus()
            QtWidgets.QMessageBox.information(
                self, tr("No server specified"),
                tr("Enter the server in the “Server” field on this tab — "
                   "a name from ~/.ssh/config or "
                   "user@host.{NEWLINE}{NEWLINE}You need key login "
                   "without a password and OpenFOAM installed there.", NEWLINE=NEWLINE))
            return
        if not self.panel_run.select_ssh_backend():
            # Сервера ще немає серед бекендів: перевіряємо його зараз.
            # recheck_server сам поскаржиться, якщо той не відповів.
            self.recheck_server()
            if not self.panel_run.select_ssh_backend():
                return
        b = self.panel_run.current_backend()
        self.log.push(tr("Running on the server: {label}", label=b.label))
        self.start_run()

    def start_run(self):
        """«Запустити» — один прогін, а з галочкою серії — увесь список кутів."""
        if self.panel_run.chk_series.isChecked() and self._series is None:
            if self.panel_run.chk_prerun.isChecked():
                self.log.push(tr("The pre-run is not used for an angle "
                                 "series: the series runs as it is."))
            self.start_series()
            return
        if self.panel_run.chk_prerun.isChecked() and self._series is None:
            self.start_prerun()
            return
        self._start_one_run()

    # --- PRE-RUN (T39): a short run that tunes the main run ---------------

    def start_prerun(self) -> bool:
        """Run the short pre-run. Its report window starts the main run."""
        backend = self.panel_run.current_backend()
        why = prerun.why_not(self.cfg, backend.kind if backend else "")
        if why:
            QtWidgets.QMessageBox.information(
                self, tr("Run with a pre-run (tuning)"), why)
            return False
        self._prerun_phase = "pre"
        if not self._start_one_run(pre=True):
            self._prerun_phase = ""
            return False
        return True

    def _prerun_report(self, ok: bool, msg: str):
        """The Report of the pre-run that just ended (None: it was stopped)."""
        w = self.worker
        runner = w.runner if w else None
        stop = getattr(runner, "_stop", None)
        if stop is not None and stop.is_set():
            return None
        case_dir = Path((w.case_dir if w else "") or self.case_dir or ".")
        logs = {}
        for sid in prerun.log_names(getattr(w, "cfg", None)
                                    or self.cfg).values():
            f = case_dir / "logs" / f"{be._sh_name(sid)}.log"
            if f.is_file():
                logs[sid] = f.read_text(encoding="utf-8", errors="replace")
        f = case_dir / "logs" / f"{be._sh_name(be.STEP_MESH_CHECK)}.log"
        hit = f.is_file() and "mesh=hit" in f.read_text(
            encoding="utf-8", errors="replace")
        st = runner.state if runner else None
        level, why = st.mesh_severity() if st else ("ok", "")
        facts = prerun.RunFacts(
            ok=ok, msg=msg,
            step=be.step_id(runner.current_step) if runner else "",
            cells=st.cells if st else 0,
            coverage=st.layer_coverage if st else -1.0,
            faces=getattr(st, "layer_faces", -1.0) if st else -1.0,
            mesh_level=level, mesh_why=why, mesh_from_store=hit,
            mesh_cache=bool(w) and be.mesh_cache_supported(w.backend))
        prerun.remove_limit_file(case_dir)
        # The configuration of the run itself (T38: RunWorker.cfg is a copy
        # made at the start), not what the window holds now.
        cfg = getattr(w, "cfg", None) or self.cfg
        return prerun.analyse(cfg, facts, logs,
                              mesh_locked=self._pmesh.active, diag=self.diag)

    def _prerun_finished(self, ok: bool, msg: str) -> None:
        """The pre-run ended: show the report, then start the main run."""
        self._prerun_phase = ""
        rep = self._prerun_report(ok, msg)
        if rep is None:
            self.panel_run.lbl_status.setText(tr("Pre-run stopped."))
            return
        self.log.push("")
        self.log.push(tr("— Pre-run (tuning): {headline} —",
                         headline=rep.headline))
        for r in rep.rows:
            if r.change:
                self.log.push(tr("  proposed: {title} {old} → {new} ({m})",
                                 title=r.title, old=r.current, new=r.proposed,
                                 m=r.measured))
            else:
                self.log.push(f"  · {r.title}: {r.measured}")
        for n in rep.notes:
            self.log.push(f"  · {n}")
        self.panel_run.lbl_status.setText(rep.headline)
        dlg = fs_prerunwin.PreRunDialog(rep, self)
        self._prerun_dialog = dlg
        dlg.exec()
        self._prerun_dialog = None
        if dlg.action == "apply":
            done = prerun.apply(self.cfg, dlg.values())
            for key, old, new in done:
                self.log.push(tr("Pre-run: {key} {old} → {new}",
                                 key=key, old=old, new=new))
            if done:
                self.reload_panels()
                self.refresh_all()
        if dlg.action in ("apply", "skip"):
            QtCore.QTimer.singleShot(0, self._start_main_after_prerun)
        else:
            self._warn_quiet = set()

    def _start_main_after_prerun(self) -> None:
        # The main run uses the folder of its pre-run (T43): no «_r2».
        self._keep_folder = True
        try:
            if self._start_one_run():
                self.log.push(tr("Main run after the pre-run."))
        finally:
            self._keep_folder = False
            self._warn_quiet = set()

    # --- СЕРІЯ КУТІВ ДЛЯ НЕЙТРАЛЬНОЇ ТОЧКИ ---------------------------------

    def _series_quiet(self) -> bool:
        """Чи йде вже другий і далі кут серії.

        Тоді питань, на які людина відповіла на першому куті, не ставимо
        вдруге: попередження щодо налаштувань і памʼяті від кута не
        залежать, а серія, що вночі стала на «Створити кейс попри це?»,
        не порахувала б нічого.
        """
        return self._series is not None and self._series.at > 0

    def start_series(self):
        """Прогони один за одним, по одному на кут, і нейтральна точка в кінці.

        Між прогонами змінюються ЛИШЕ кут і назва кейсу. Усе інше — сітка,
        розвʼязувач, Lref, Aref, центр моментів — однакове, і саме це робить
        похідну моменту за кутом осмисленою: `lib.stability` відмовиться
        рахувати, якщо нормування розійдеться.
        """
        angles, why = self.panel_run.series_angles()
        if not why:
            why = lib.series_problem(self.cfg)
        if why:
            QtWidgets.QMessageBox.warning(
                self, tr("Series not started"), why[0].upper() + why[1:] + ".")
            return
        backend = self.panel_run.current_backend()
        if (fset.get("queue_on") and backend is not None
                and backend.kind == "ssh"):
            # T46: the queue on the server runs the series; the laptop can
            # sleep. The window loop below is the fallback without a queue.
            self._series_to_queue(list(angles), backend)
            return
        f = self.cfg.flow
        self._series = AngleSeries(base_name=self.cfg.name or "case",
                                   base_aoa=f.aoa_deg, angles=list(angles))
        self.log.push(
            tr("Series for the neutral point: {runs_word}, angles "
               "{join}. Cases {base_name} … {base_name2}.", runs_word=lib.runs_word(len(angles)), join=", ".join(f"{a:g}°" for a in angles), base_name=lib.series_case_name(self._series.base_name, angles[0]), base_name2=lib.series_case_name(self._series.base_name, angles[-1])))
        for note in lib.series_notes(angles):
            self.log.push(tr("Warning: {note}.", note=note))
        self._series_next()

    def _set_angle(self, name: str, aoa: float) -> None:
        """Кут і назва — і в конфіг, і у віджети, щоб вікно не брехало."""
        self.cfg.flow.aoa_deg = float(aoa)
        self.cfg.name = name
        self.panel_flow.aoa.blockSignals(True)
        self.panel_flow.aoa.setValue(float(aoa))
        self.panel_flow.aoa.blockSignals(False)
        self.panel_run.ed_name.setText(name)
        self.refresh_all()

    def _series_next(self):
        s = self._series
        if s is None:
            return
        s.at += 1
        if s.stopped or s.at >= len(s.angles):
            self._finish_series()
            return
        angle = s.angles[s.at]
        self._set_angle(lib.series_case_name(s.base_name, angle), angle)
        if not self._start_one_run():
            # Прогін навіть не стартував: немає бекенда, кейс не зібрався,
            # людина відмовилась на питанні. Далі буде те саме — зупиняємось.
            s.failed.append((angle, tr("the run did not start")))
            s.stopped = True
            self._finish_series()
            return
        # Після старту, а не до: прогін починається з чистого журналу, і
        # рядок, написаний раніше, зник би разом зі старим.
        done = ", ".join(f"{a:g}°" for a, _ in s.done)
        self.log.push(
            tr("— Series for the neutral point: angle {angle:g}° ({v} of "
               "{n_angles}), case {name}.{v2} —", angle=angle, v=s.at + 1, n_angles=len(s.angles), name=self.cfg.name, v2=tr(" Already computed: {done}.", done=done) if done else ""))

    def _series_after_run(self, ok: bool, msg: str, case_dir: str) -> None:
        """Прогін серії скінчився: записати й узяти наступний кут.

        Невдалий кут серію НЕ зупиняє. На більшому куті розвʼязувач буває
        розходиться, а решта кутів від цього не гірші — серія, кинута
        вночі на третьому з пʼяти, коштувала б ранку. Зупиняє лише «Стоп».
        """
        s = self._series
        if s is None or not (0 <= s.at < len(s.angles)):
            return
        angle = s.angles[s.at]
        if ok and case_dir:
            s.done.append((angle, case_dir))
        else:
            last = (msg or "").strip().splitlines()
            s.failed.append((angle, last[-1][:160] if last else tr("error")))
        # Наступний — уже з циклу подій: on_done ще доробляє цей прогін
        # (результати, бібліотека), і новий потік поверх нього не потрібен.
        QtCore.QTimer.singleShot(0, self._series_next)

    def _finish_series(self):
        """Повернути кут і назву, порахувати точку й показати порівняння."""
        s, self._series = self._series, None
        if s is None:
            return
        self._set_angle(s.base_name, s.base_aoa)
        for angle, why in s.failed:
            self.log.push(tr("Series: angle {angle:g}° did not compute — {why}", angle=angle, why=why))
        cases = [lib.read_case(d) for _, d in s.done if os.path.isdir(d)]
        head = (tr("Series stopped") if s.stopped else tr("Series complete"))
        self._series_report(head, cases, len(s.angles))

    def _series_report(self, head: str, cases: list, n_angles: int) -> None:
        """The neutral point of the computed angles and the comparison (the
        window loop and the queue, T46, end here)."""
        head += tr(": {n_cases} of {n_angles} angles computed.", n_cases=len(cases), n_angles=n_angles)
        if len(cases) < 2:
            text = (tr("{head} The neutral point needs at least two "
                       "computed angles.", head=head))
            self.log.push(text)
            self.panel_run.lbl_status.setText(text)
            return
        st = lib.stability(cases)
        if st.point is not None:
            p = st.point
            where = ""
            if st.nose_x is not None and st.length:
                where = (tr(", {v:.0f} mm from the nose ({v2:.1f}% of "
                            "the length)", v=1000 * (p.x - st.nose_x), v2=100 * (p.x - st.nose_x) / st.length))
            cg = st.cg_range()
            text = (tr("{head} Neutral point x = {x:.4f} m{where}. "
                       "Center of gravity — between x = {cg:.4f} and "
                       "{cg2:.4f} m (margin {v:.0f}–{v2:.0f}% Lref).", head=head, x=p.x, where=where, cg=cg[0], cg2=cg[1], v=100 * lib.MARGIN_LOW, v2=100 * lib.MARGIN_HIGH))
        else:
            text = tr("{head} Cannot compute the neutral point: {problem}.", head=head, problem=st.problem)
        self.log.push(text)
        for note in st.notes:
            self.log.push(f"  · {note}")
        self.panel_run.lbl_status.setText(text)
        self.refresh_library()
        # Не модально: серія могла закінчитися, коли людини немає, і вікно,
        # що чекає на клік, тримало б програму. Порівняння — те саме, що
        # «Порівняти вибрані» в «Збережених», з блоком нейтральної точки.
        dlg = panels.CompareDialog(cases, self)
        dlg.setAttribute(QtCore.Qt.WA_DeleteOnClose)
        dlg.setModal(False)
        dlg.show()
        self._series_dialog = dlg

    # --- THE RUN QUEUE ON THE SERVER (T46, fs_queue, fs_queuewin) -----------

    def open_queue(self, focus_server: bool = False) -> None:
        """The «Runs» window (not modal): the plan of the queue and the runs
        on the servers (T49b)."""
        if self._queue_win is None:
            self._queue_win = fs_queuewin.RunsWindow(
                self, self.queue_watch,
                add_case=lambda nb: self.queue_add(series=False, not_before=nb),
                add_series=lambda nb: self.queue_add(series=True, not_before=nb),
                fetch=self.queue_fetch, connect=self._runs_connect,
                hosts=self._runs_hosts, snapshot=self.runs_snapshot)
        shown = self._queue_win.isVisible()
        self._queue_win.show()           # a new show reads all (showEvent)
        if shown:
            self._queue_win.refresh_all()
        self._queue_win.raise_()
        self._queue_win.activateWindow()
        if focus_server:
            self._queue_win.focus_server()

    def _runs_hosts(self) -> list:
        """The servers that the «Runs» window reads: the ones of the run
        watch (settings, «Server» field, the case) and each server backend
        of the «Run» tab."""
        hosts = list(self.run_guard._hosts())
        cmb = self.panel_run.cmb_backend
        for i in range(cmb.count()):
            b = cmb.itemData(i)
            if getattr(b, "kind", "") == "ssh" and b.host and                     b.host not in hosts:
                hosts.append(b.host)
        return hosts

    def _runs_probe(self, host: str, sessions, mode: str) -> None:
        """A full read of a server by the run watch (T44): keep it for the
        «Runs» window, no second ssh call."""
        if mode == "recover" and sessions is not None:
            self.runs_snapshot.update({host: sessions})
            if self._queue_win is not None:
                self._queue_win._fill_runs()

    def _runs_connect(self, session) -> None:
        """«Connect» in the «Runs» window: like «Connect to Run» before."""
        if self.thread is not None and self.thread.isRunning():
            QtWidgets.QMessageBox.information(
                self, tr("Wait"), tr("Stop the current run first."))
            return
        self.run_guard.connect_run(session, user=True)
        self.raise_()
        self.activateWindow()

    def _queue_client(self) -> "fq.Client | None":
        """The client of the queue, or None after a note to the person."""
        if not fset.get("queue_on") or not fq.queue_host():
            QtWidgets.QMessageBox.information(
                self, tr("Run Queue"),
                tr("The queue is not set up yet. In «Runs → Runs…», "
                   "enter the queue server and click «Set Up»."))
            self.open_queue()
            return None
        return fq.Client(fq.queue_host())

    def _queue_backend(self):
        """The server backend for a queued run (like «Run on Server»)."""
        b = self.panel_run.current_backend()
        if b is not None and b.kind == "ssh" and b.ready:
            return b
        host = self.panel_run.ed_server.text().strip() or fq.queue_host()
        if host and not self.panel_run.ed_server.text().strip():
            self.panel_run.ed_server.setText(host)
        if not self.panel_run.select_ssh_backend():
            self.recheck_server()
            if not self.panel_run.select_ssh_backend():
                QtWidgets.QMessageBox.information(
                    self, tr("Run Queue"),
                    tr("The queue runs a case on a server. Enter the server "
                       "in the «Server» field on the «Run» tab (a name from "
                       "~/.ssh/config) and check it."))
                return None
        return self.panel_run.current_backend()

    def queue_add(self, series: "bool | None" = None,
                  not_before: float = 0.0) -> bool:
        """Put the case (or, with the series check box, the angle series) in
        the queue. `series` None: the check box decides."""
        th = getattr(self, "thread", None)
        if isinstance(th, QtCore.QThread) and th.isRunning():
            QtWidgets.QMessageBox.information(
                self, tr("Run Queue"),
                tr("A run of this window is going on. Add to the queue "
                   "after it, or stop it."))
            return False
        if series is None:
            series = self.panel_run.chk_series.isChecked()
        if self._queue_client() is None:
            return False
        backend = self._queue_backend()
        if backend is None:
            return False
        if series:
            angles, why = self.panel_run.series_angles()
            if not why:
                why = lib.series_problem(self.cfg)
            if why:
                QtWidgets.QMessageBox.warning(
                    self, tr("Series not started"),
                    why[0].upper() + why[1:] + ".")
                return False
            return self._series_to_queue(list(angles), backend, not_before)
        self._queue_ctx = {"queue": fq.queue_host(), "not_before": not_before,
                           "meta": {"aoa": self.cfg.flow.aoa_deg}}
        self._queued_job = None
        try:
            ok = self._start_one_run()
        finally:
            self._queue_ctx = None
        if not ok or self._queued_job is None:
            return False
        return self._queue_put([self._queued_job])

    def _series_to_queue(self, angles: list, backend,
                         not_before: float = 0.0) -> bool:
        """The angle series as jobs of the queue: one case and one job per
        angle, the same rule for names and settings as the window loop."""
        if self._queue_client() is None:
            return False
        f = self.cfg.flow
        base, base_aoa = self.cfg.name or "case", f.aoa_deg
        sid = f"{fc.safe_case_name(base)}-{time.strftime('%m%d-%H%M%S')}"
        self.log.push(tr("Series for the neutral point in the queue: "
                         "{runs_word}, angles {join}.",
                         runs_word=lib.runs_word(len(angles)),
                         join=", ".join(f"{a:g}°" for a in angles)))
        for note in lib.series_notes(angles):
            self.log.push(tr("Warning: {note}.", note=note))
        jobs = []
        # _series_quiet: the questions of the first angle are not asked again.
        self._series = AngleSeries(base_name=base, base_aoa=base_aoa,
                                   angles=list(angles))
        try:
            for i, a in enumerate(angles):
                self._series.at = i
                self._set_angle(lib.series_case_name(base, a), a)
                self._queue_ctx = {
                    "queue": fq.queue_host(), "series": sid,
                    "not_before": not_before,
                    "meta": {"aoa": a, "base": base, "base_aoa": base_aoa}}
                self._queued_job = None
                if not self._start_one_run() or self._queued_job is None:
                    self.log.push(tr("Series: angle {angle:g}° was not "
                                     "added; the series stops here.", angle=a))
                    break
                jobs.append(self._queued_job)
        finally:
            self._queue_ctx = None
            self._series = None
            self._set_angle(base, base_aoa)
            if self.panel_run.chk_remesh.isChecked():
                self.panel_run.chk_remesh.setChecked(False)
        if not jobs:
            return False
        return self._queue_put(jobs)

    def _queue_put(self, jobs: list) -> bool:
        """Give the prepared jobs to the queue on the server."""
        c = self.queue_watch.client()
        try:
            ids = self._off_thread(tr("Putting the jobs in the queue…"),
                                   lambda: (c.ready(), c.add(jobs))[1])
        except Exception as exc:                          # noqa: BLE001
            QtWidgets.QMessageBox.critical(self, tr("Run Queue"), str(exc))
            return False
        for i, j in zip(ids, jobs):
            self.log.push(tr("Queue: {id} — {name} on {host}.", id=i,
                             name=j["name"], host=j.get("via") or c.host))
        text = tr("{n} jobs are in the queue on {host}. The server starts "
                  "them when the machine is free: you can turn off the "
                  "laptop.", n=len(ids), host=c.host)
        self.panel_run.lbl_status.setText(text)
        self.statusBar().showMessage(text, 20000)
        self.open_queue()
        self.queue_watch.poll()
        return True

    def queue_fetch(self, jobs: list) -> None:
        """Download the results of ended jobs; the neutral point for a
        series whose jobs are all here."""
        queue = fq.queue_host()
        try:
            got, bad = self._off_thread(
                tr("Downloading the results…"),
                lambda: fs_queuewin.fetch_jobs(jobs, queue), BIG_MESH)
        except Exception as exc:                          # noqa: BLE001
            QtWidgets.QMessageBox.critical(self, tr("Run Queue"), str(exc))
            return
        for j, why in bad:
            self.log.push(tr("Queue: {name} not downloaded — {why}",
                             name=j.get("name", ""), why=why))
        self.log.push(tr("Queue: results of {n} jobs downloaded.", n=len(got)))
        self.refresh_library()
        # The download takes only the last time: for the case of the
        # window, offer the other times of the run (T40 FetchBar).
        for j in jobs:
            d = j.get("case_dir", "")
            if d in got and self._shows_case(d):
                host = j.get("via") or j.get("host") or queue
                self._offer_more_times(
                    be.Backend(kind="ssh", label=host, host=host), d,
                    j.get("dir", ""))
        series = {j.get("series") for j in jobs if j.get("series")}
        for sid in sorted(series):
            mine = [j for j in jobs if j.get("series") == sid
                    and j.get("case_dir") in got]
            cases = [lib.read_case(j["case_dir"]) for j in mine]
            all_jobs = [j for j in self.queue_watch.jobs or []
                        if j.get("series") == sid] or mine
            head = tr("Series {s} (queue)", s=sid)
            self._series_report(head, cases, len(all_jobs))

    def queue_series_ended(self, sid: str, jobs: list) -> None:
        """QueueWatch: the last job of a series ended (on the server)."""
        n_ok = sum(1 for j in jobs if j.get("state") == "done")
        self.log.push(tr("Queue: series {s} ended, {ok} of {n} jobs done. "
                         "«Runs → Runs… → Download Results» gets "
                         "them and shows the neutral point.", s=sid, ok=n_ok,
                         n=len(jobs)))

    # --- розподіл між машинами (експериментально, rust/foamdist) ----------

    def _dist_hosts(self):
        """([DistMachine увімкнені], помилка) з панелі; ([], "") — вимкнено."""
        pr = self.panel_run
        if not pr.box_dist.isChecked():
            return [], ""
        return be.check_machines(pr.machines())

    def scan_dist(self) -> None:
        """«Знайти машини»: сервер обходить свою мережу й ~/.ssh/config."""
        pr = self.panel_run
        backend = self._ssh_backend_or_complain()
        if backend is None:
            return
        key, user = pr.ed_dist_key.text().strip(), pr.ed_dist_user.text().strip()
        try:
            # foamdist на сервері не перекладаємо: пошук лише читає, і
            # підміняти бінарник посеред чужої роботи над ним не варто.
            # Кладемо, тільки якщо його там немає зовсім.
            if not be.has_foamdist(backend):
                err = self._off_thread(tr("Putting foamdist on the server…"),
                                       lambda: be.ensure_foamdist(backend))
                if err:
                    pr.lbl_dist.setText(err)
                    return
            found = self._off_thread(
                tr("Looking for machines in the server network…"),
                lambda: be.scan_dist(backend, key, user))
        except Exception as exc:                          # noqa: BLE001
            pr.lbl_dist.setText(tr("search failed: {exc}", exc=exc))
            return
        have = pr.machines()
        rows = be.scan_to_machines(found, have)
        if not rows:
            pr.lbl_dist.setText("No machine with open ssh found in the "
                                "server network.")
            return
        dlg = panels.ScanDialog(rows, self)
        if dlg.exec() != QtWidgets.QDialog.Accepted:
            return
        new = dlg.selected()
        if not new:
            return
        pr.set_machines(have + new)
        pr.lst_dist.setCurrentRow(len(have))
        pr.dist_machines_changed.emit()
        if not pr.box_dist.isChecked():
            pr.box_dist.setChecked(True)
        pr.lbl_dist.setText(
            tr("Added: {join}{v}.", join=", ".join(m.label() for m in new), v=tr(". "
                                                                                        "Unchecked "
                                                                                        "(not "
                                                                                        "ready) "
                                                                                        "— "
                                                                                        "{join}", join=", ".join(m.label() for m in new if not m.on))
               if any(not m.on for m in new) else ""))

    def probe_dist(self) -> None:
        pr = self.panel_run
        hosts, why = be.check_machines(pr.machines())
        if why and not hosts:
            pr.lbl_dist.setText(why)
            return
        backend = self._ssh_backend_or_complain()
        if backend is None:
            return
        key, user = pr.ed_dist_key.text().strip(), pr.ed_dist_user.text().strip()
        rebench = pr.chk_dist_rebench.isChecked()
        try:
            err = self._off_thread(tr("Putting foamdist on the server…"),
                                   lambda: be.ensure_foamdist(backend))
            if err:
                pr.lbl_dist.setText(err)
                return
            got = self._off_thread(tr("Checking the machines…"),
                                   lambda: be.probe_dist(backend, hosts, key, user))
            plan = self._off_thread(
                tr("Measuring the machine speed (the first time — up to a minute)…"),
                lambda: be.dist_weights(backend, hosts, key, user, rebench))
        except Exception as exc:                          # noqa: BLE001
            pr.lbl_dist.setText(tr("failed: {exc}", exc=exc))
            return
        if rebench:
            pr.chk_dist_rebench.setChecked(False)
        pr.set_probe(got)
        hosts = be.check_machines(pr.machines())[0]   # назви з hostname
        pr.lbl_dist.setText(self._dist_report(hosts, got) + "\n"
                            + self._plan_report(plan, hosts)
                            + (f"\n{why}" if why else "")
                            + "\n" + self._mem_fit().details())

    @staticmethod
    def _plan_report(plan: dict, hosts=()) -> str:
        """Як сітка поділиться між машинами — за швидкістю й вільними ядрами."""
        if plan.get("error"):
            return tr("Speed not measured: {plan}", plan=plan['error'])
        lines = [tr("Mesh shares (by speed and by how free each machine is):")]
        names = {m.addr: m.name for m in hosts if getattr(m, "name", "")}
        for h in plan.get("hosts", []):
            src = {"cached": "saved measurement", "measured": "just measured"}.get(
                h.get("bench", ""), str(h.get("bench", "")))
            lines.append(
                tr("  {get}: {v:.1f}% — {v2:.0f}k cells/s per process "
                   "({src}), {v3:.0f}% free", get=names.get(h.get('host'), h.get('host')), v=100 * float(h.get('share', 0)), v2=float(h.get('speed', 0)) / 1000, src=src, v3=100 * float(h.get('factor', 1))))
        return "\n".join(lines)

    @staticmethod
    def _dist_report(hosts, got) -> str:
        by = {g.get("host"): g for g in got}
        lines, head = [], False
        for m in be._as_machines(hosts):
            addr, n = m.addr, m.procs
            who = f"{m.name} ({addr})" if m.name else addr
            g = by.get(addr)
            if g is None or not g.get("ok"):
                lines.append(f"✗ {who}: " + (str(g.get("error", "")) if g
                                             else tr("did not answer")))
                continue
            head = head or g.get("head") == "yes"
            cores = int(g.get("cores", 0) or 0)
            bits = [tr("{cores} cores", cores=cores), tr("load {get}", get=g.get('load')),
                    tr("image present") if g.get("image") == "yes" else tr("NO IMAGE")]
            ram = be.probe_ram_gb(g)
            if ram > 0:
                bits.insert(1, tr("{ram} GB memory", ram=memfit.fmt_gb(ram)))
            if n > cores > 0:
                bits.append(tr("processes {n} > cores {cores}", n=n, cores=cores))
            if g.get("head") == "yes":
                bits.append(tr("server (case is here)"))
            ok = g.get("image") == "yes" and not (n > cores > 0)
            lines.append(f"{'✓' if ok else '!'} {who}: " + ", ".join(bits))
        if not head:
            lines.append(tr("✗ The server itself is not among the machines "
                         "— enter its local network address first."))
        total = sum(m.procs for m in be._as_machines(hosts))
        lines.append(tr("Total processes: {total}.", total=total))
        return "\n".join(lines)

    def _project_mesh_for_run(self, case_key: str) -> "str | None":
        """Ключ сітки для прогону, коли діє сітка з проєкту. None — не пускати.

        Ключ щойно записаного кейсу (`case_key`) той самий, що одразу після
        відкриття (`baseline`), — людина нічого не міняла: беремо сітку
        проєкту під ЇЇ ключем, хоч би як ця версія програми писала словники.
        Інший — щось змінило сітку в обхід замка: питаємо.
        """
        pm = self._pmesh
        # Порівнюємо ключ сцени (mesh_inputs_key), а не теки кейсу: у теці
        # кейсу triSurface може тримати старі STL від минулих збирань.
        now = self.mesh_inputs_key()
        if case_key == pm.key or now == pm.baseline:
            self.log.push(tr("Mesh from project {key}: snappyHexMesh will not run.", key=pm.key))
            return pm.key
        box = QtWidgets.QMessageBox(self)
        box.setIcon(QtWidgets.QMessageBox.Warning)
        box.setWindowTitle("Mesh from Project")
        box.setText(
            "The mesh settings or the geometry are no longer the same as "
            "when the project was opened, although the fields were "
            "locked (this happens after changes the program makes "
            "itself).")
        box.setInformativeText(
            tr("The key is now {now}, after opening it was {baseline}. "
               "Rebuild the mesh (~{snappy_minutes} min on the server) "
               "or use the mesh from the project anyway?", now=now, baseline=pm.baseline, snappy_minutes=self._snappy_minutes()))
        cancel = box.addButton(tr("Cancel"), QtWidgets.QMessageBox.RejectRole)
        rebuild = box.addButton(tr("Rebuild"), QtWidgets.QMessageBox.AcceptRole)
        use = box.addButton(tr("Use Project Mesh"), QtWidgets.QMessageBox.ActionRole)
        box.setDefaultButton(cancel)
        box.setEscapeButton(cancel)
        box.exec()
        hit = box.clickedButton()
        if hit is use:
            self.log.push(tr("Mesh from project {key} — by choice, "
                             "although the settings key is different "
                             "({case_key}).", key=pm.key, case_key=case_key))
            return pm.key
        if hit is rebuild:
            self._release_project_mesh()
            return case_key
        return None

    def _new_run_folder(self, backend=None) -> None:
        """A run of a case that already ran goes into a new folder (T43).

        «plane» has a run (here, on the server or in WSL) -> «plane_r2»,
        then «plane_r3»… The old folder and its results stay; the
        «Saved» list and «Connect to Run» show each folder. Changes
        cfg.name and the «Name» field. `backend` None: only local folders.
        """
        self._run_note = ""
        name = fc.safe_case_name(self.cfg.name)
        base = Path(self.panel_run.ed_dir.text().strip() or DEFAULT_CASES)
        used, taken = guard.local_names(base)
        remote: set = set()
        if backend is not None and (
                backend.kind == "ssh" or (backend.kind == "wsl"
                                          and self.panel_run.chk_native.isChecked())):
            where = backend.host or backend.label
            try:
                remote = self._off_thread(
                    tr("Checking the case folders on {where}…", where=where),
                    lambda: be.server_case_names(backend))
            except Exception as exc:                      # noqa: BLE001
                # The upload checks the folder itself (fs_caseguard).
                self._run_note = tr("Could not read the case folders on "
                                    "{where}: {exc}", where=where, exc=exc)
        new = guard.new_run_name(name, used | remote, taken | remote)
        if new == name:
            return
        self._run_note = tr("Case “{name}” already has a run: this run goes "
                            "to the new folder “{new}”. The old results "
                            "stay.", name=name, new=new)
        self.cfg.name = new
        self.panel_run.ed_name.blockSignals(True)
        self.panel_run.ed_name.setText(new)
        self.panel_run.ed_name.blockSignals(False)
        self.statusBar().showMessage(self._run_note, 15000)

    def _run_busy(self, case_dir: str) -> str:
        """Why this local case folder must not be deleted now ("" = no):
        a run of this window uses it (T43, «Saved» → Delete)."""
        w = self.worker
        if (w is None or self.thread is None or not self.thread.isRunning()
                or not case_dir or not getattr(w, "case_dir", "")):
            return ""
        if os.path.normcase(os.path.abspath(w.case_dir)) != os.path.normcase(
                os.path.abspath(case_dir)):
            return ""
        return tr("A run uses this case now. Nothing was deleted. Stop the "
                  "run or wait until it ends.")

    def _start_one_run(self, pre: bool = False, same_folder: bool = False) -> bool:
        """Один прогін. True — прогін пішов. `pre`: the pre-run (T39).
        `same_folder` (or `_keep_folder`): the main run after the pre-run,
        in the folder of the pre-run (T43)."""
        same_folder = same_folder or getattr(self, "_keep_folder", False)
        backend = self.panel_run.current_backend()
        if backend is None or not backend.ready:
            QtWidgets.QMessageBox.warning(
                self, tr("Backend not ready"),
                tr("OpenFOAM not found. Use the install button on the “Run” tab."))
            return False
        dist_hosts, why = self._dist_hosts()
        if self.panel_run.box_dist.isChecked():
            if why:
                QtWidgets.QMessageBox.warning(
                    self, tr("Distribution across machines"), why)
                return False
            if backend.kind != "ssh":
                QtWidgets.QMessageBox.information(
                    self, tr("Distribution across machines"),
                    tr("Distribution works only for a run on the server: "
                    "the “Run on server” button. Clear the check box to "
                    "run here."))
                return False
            total = sum(m.procs for m in dist_hosts)
            if self.cfg.solver.n_procs != total:
                # Сітка ріжеться на стільки шматків, скільки разом процесів.
                self.log.push(tr("Distribution across machines: "
                                 "processes {n_procs} → {total} (sum "
                                 "over the machines).", n_procs=self.cfg.solver.n_procs, total=total))
                self.cfg.solver.n_procs = total
                self.panel_solve.load_cfg()
            # Шматки за швидкістю машин: ваги в decomposeParDict ще до
            # сітки — їх бачить і snappy, коли вирівнює її наприкінці.
            pr = self.panel_run
            key, user = pr.ed_dist_key.text().strip(), pr.ed_dist_user.text().strip()
            try:
                err = self._off_thread(tr("Putting foamdist on the server…"),
                                       lambda: be.ensure_foamdist(backend))
                plan = {"error": err} if err else self._off_thread(
                    tr("Measuring the speed of the machines…"),
                    lambda: be.dist_weights(backend, dist_hosts, key, user))
            except Exception as exc:                      # noqa: BLE001
                plan = {"error": str(exc)}
            w = plan.get("weights") or []
            if len(w) == total:
                self.cfg.solver.proc_weights = [int(x) for x in w]
                self.log.push(self._plan_report(plan, dist_hosts))
            else:
                self.cfg.solver.proc_weights = []
                self.log.push(tr("Equal pieces: {plan}", plan=self._plan_report(plan, dist_hosts)))
        else:
            self.cfg.solver.proc_weights = []
        if not same_folder:
            self._new_run_folder(backend)
        if self.create_case(report=False, build_cfg=(
                prerun.prerun_cfg(self.cfg) if pre else None)) is None:
            return False
        # The step limit of a transient pre-run lives in the case (system/),
        # so each backend takes it with the case. Not in a main run.
        if pre:
            try:
                prerun.prepare_case(self.case_dir, self.cfg)
            except (OSError, ValueError) as exc:
                QtWidgets.QMessageBox.critical(
                    self, tr("Run with a pre-run (tuning)"),
                    tr("Could not limit the pre-run: {exc}", exc=exc))
                return False
        else:
            prerun.remove_limit_file(self.case_dir)

        # Памʼять — до того, як «перебудувати» скинеться: від нього залежить,
        # чи буде snappyHexMesh.
        mem_fit = self._mem_fit()
        if not self._series_quiet() and not self._memory_gate():
            return False

        # ПРОЄКТ ЗБЕРІГАЄМО ПЕРЕД ПРОГОНОМ, а не після. Результат рахується
        # годинами, і за цей час сцену встигають покрутити; знімок саме того
        # стану, з якого пішов розрахунок, потім нічим не відтворити.
        # Мовчки й у типову теку: питати дорогою до кнопки «Запустити» —
        # означає ставати на дорозі.
        self._autosave_project()

        native_dir = None
        if backend.kind == "wsl" and self.panel_run.chk_native.isChecked():
            try:
                self.log.push(tr("Copying the case to the native WSL file system…"))
                native_dir = be.sync_to_wsl(backend, self.case_dir, self.cfg.name,
                                            own=same_folder)
                self.log.push(f"→ {native_dir}")
            except Exception as exc:
                QtWidgets.QMessageBox.critical(self, tr("Copy failed"), str(exc))
                return False
        elif backend.kind == "ssh":
            # Для сервера це не вибір, а обовʼязок: шляху Windows на тому
            # боці не існує, тож рахувати «на місці» там нема де.
            try:
                self.log.push(tr("Uploading the case to {host}…", host=backend.host))
                self._laptop("upload")
                case_dir, name = self.case_dir, self.cfg.name
                # Поза потоком вікна: інакше вікно завмирало на весь час
                # вивантаження і індикатор «не вимикайте» навіть не
                # встигав намалюватися.
                native_dir = self._off_thread(
                    tr("Uploading the case to {host}…", host=backend.host),
                    lambda: be.sync_to_ssh(backend, case_dir, name,
                                           own=same_folder))
                self.log.push(f"→ {backend.host}:{native_dir}")
                # The exact folder of this run on the server (T43).
                try:
                    lib.set_server(case_dir, backend.host, native_dir)
                except OSError:
                    pass
                if dist_hosts:
                    err = self._off_thread(tr("Putting foamdist on the server…"),
                                           lambda: be.ensure_foamdist(backend))
                    if err:
                        raise RuntimeError(err)
            except Exception as exc:
                self._laptop("")
                QtWidgets.QMessageBox.critical(
                    self, tr("Could not upload to the server"), str(exc))
                return False

        # Збережена сітка: ключ — з файлів кейсу, щойно записаних.
        mkey = be.mesh_key(self.case_dir) if be.mesh_cache_supported(backend) else ""
        remesh = self.panel_run.chk_remesh.isChecked()
        if mkey and self._pmesh.active:
            got = self._project_mesh_for_run(mkey)
            if got is None:
                return False
            mkey = got
            remesh = remesh and not self._pmesh.active
        # Сітка є тут (з проєкту чи з минулого прогону), а там, де рахуємо,
        # — ні: вивантажуємо. Це хвилини проти десятка хвилин snappy.
        # The same rule as the queue and `ofs`: be.ensure_remote_mesh (T51).
        if mkey and not remesh:
            try:
                err = self._off_thread(
                    tr("Uploading the saved mesh…"),
                    lambda: be.ensure_remote_mesh(backend, mkey))
                if err is not None:
                    self.log.push(err or tr("Mesh {mkey} uploaded — no "
                                            "need to build it.", mkey=mkey))
            except Exception as exc:                      # noqa: BLE001
                self.log.push(tr("Mesh not uploaded, building it: {exc}", exc=exc))
        # The steps: the same function for «Run», the queue and `ofs` (T46).
        dist_key = self.panel_run.ed_dist_key.text().strip()
        dist_user = self.panel_run.ed_dist_user.text().strip()
        steps = fq.run_steps(
            self.cfg, backend,
            dist=(be.dist_command(dist_hosts, dist_key, dist_user,
                                  self.panel_run.ed_dist_net.text().strip())
                  if dist_hosts else ""),
            mesh_key=mkey, remesh=remesh)
        if pre:
            steps = prerun.prerun_steps(steps, self.cfg)
        # «Перебудувати» — на один запуск; посеред серії лишається до кінця
        # серії, щоб кожен кут збудував свою.
        if remesh and self._series is None:
            self.panel_run.chk_remesh.setChecked(False)

        ctx = self._queue_ctx
        if ctx is not None:
            # T46: a job for the queue on the server. The script is put next
            # to the case; the queue starts it later.
            case_dir, name = self.case_dir, self.cfg.name
            stop_bad = self.cfg.solver.stop_on_bad_mesh
            nodes = (fq.dist_nodes(dist_hosts, dist_key, dist_user)
                     if dist_hosts else [])
            try:
                self._queued_job = self._off_thread(
                    tr("Putting the run script on {host}…", host=backend.host),
                    lambda: fq.script_job(
                        backend, native_dir, steps, name=name,
                        case_dir=case_dir, queue=ctx["queue"],
                        stop_on_bad_mesh=stop_bad,
                        series=ctx.get("series", ""),
                        not_before=ctx.get("not_before", 0.0), nodes=nodes,
                        meta=ctx.get("meta")))
            except Exception as exc:                      # noqa: BLE001
                self._laptop("")
                QtWidgets.QMessageBox.critical(
                    self, tr("Could not upload to the server"), str(exc))
                return False
            self._laptop("")
            if getattr(self, "_run_note", ""):
                self.log.push(self._run_note)
                self._run_note = ""
            return True

        self.log.clear_log()
        if getattr(self, "_run_note", ""):
            self.log.push(self._run_note)       # T43: the new folder
            self._run_note = ""
        if pre:
            self.log.push(prerun.start_text(self.cfg))
        if mkey:
            self.log.push(tr("Mesh key: {mkey}{v}", mkey=mkey, v=" (rebuild)" if remesh else ""))
        self.log.push(mem_fit.details())
        if mem_fit.verdict == "unknown":
            # Памʼяті не знаємо — запуску не заважаємо здогадкою, але кажемо.
            self.log.push(mem_fit.warning().lstrip("ℹ ") + ".")
        if dist_hosts:
            self.log.push(tr("Distribution across machines (experimental): {join}", join=", ".join(f"{m.label()} × {m.procs}"
                                      for m in dist_hosts)))
        self.plots.set_steady(self.cfg.solver.steady)
        self.plots.update_plots({}, {})
        self._run_started = time.monotonic()
        self.tabs_right.setCurrentWidget(self.log)
        self.panel_run.set_running(True)
        self.panel_run.progress.setValue(0)

        self.worker = RunWorker(backend, self.case_dir, steps,
                                native_dir=native_dir,
                                sync_back=native_dir is not None,
                                name=self.cfg.name,
                                stop_on_bad_mesh=self.cfg.solver.stop_on_bad_mesh,
                                mesh_key=mkey)
        # The advice after the run is about THIS run, also when the fields
        # change while it goes (T38).
        self.worker.cfg = lib.cfg_from_dict(lib.cfg_to_dict(self.cfg))
        self.thread = QtCore.QThread(self)
        self.thread.setObjectName("run")
        self.worker.moveToThread(self.thread)
        self.thread.started.connect(self.worker.run)
        self.worker.line.connect(self._run_line)
        self.worker.event.connect(self._run_event)
        self.worker.step.connect(self.on_step)
        self.worker.done.connect(self.on_done)
        if self.panel_run.laptop_state() != "upload":
            self._laptop("")
        self.thread.start()
        self.timer.start()
        self._live_start()
        return True

    def _laptop(self, state: str) -> None:
        """Індикатор «чи можна вимикати ноут»: у панелі «Запуск» і внизу."""
        self.panel_run.set_laptop_state(state)
        got = self.panel_run.LAPTOP_STATES.get(state)
        if got:
            _text, _bg, fg, short = got
            self.lbl_detached.setText(short)
            self.lbl_detached.setStyleSheet(f"color:{fg}; font-weight:600;")
        # The tooltip of THIS state: the status bar said «getting the
        # results back» with the tooltip «the upload is complete, turn off
        # the laptop» (T49).
        self.lbl_detached.setToolTip(
            self.panel_run.LAPTOP_TIPS.get(state, "") if got else "")
        self.lbl_detached.setVisible(bool(got))
        QtWidgets.QApplication.processEvents()

    def _run_line(self, text: str) -> None:
        self.log.push(text)

    def _run_event(self, name: str, value: str = "") -> None:
        """An event of the run (RunWorker.event): no log text to compare."""
        # T44: "fetch" (the run ended, the download starts) and "link"
        # (the connection to the server is lost / back).
        self.run_guard.run_event(name, value)
        if name == "detached":
            self._laptop("free")
            self.statusBar().showMessage(
                tr("The run is detached on the server — you can turn off the laptop."), 15000)
        elif name == "fetch" and (self.panel_run.laptop_state() == "free"
                                  or self._follows_server_run()):
            # The results come from a server: keep the laptop on. A copy
            # back from WSL is on this computer: no indicator.
            self._laptop("fetch")

    def _follows_server_run(self) -> bool:
        w = self.worker
        busy = self.thread is not None and self.thread.isRunning()
        return bool(busy and w is not None
                    and getattr(getattr(w, "backend", None), "kind", "")
                    == "ssh")

    def _memory_gate(self) -> bool:
        """Пропускає прогін далі або пропонує його полагодити.

        Питання «сітка не влізе, запустити попри це?» — глухий кут: «так»
        гарантує падіння через кілька хвилин, «ні» лишає користувача сам на сам
        із десятком налаштувань. Тому називаємо винуватця, рахуємо рівень, на
        якому задача влізе, і даємо кнопку, що це робить.
        """
        est = self.cfg.estimate()
        fit = self._mem_fit()
        # Памʼяті не знаємо ("unknown") — запуску не заважаємо здогадкою;
        # про це скаже журнал прогону.
        if fit.verdict != "over":
            return True
        w = fit.worst
        budget = w.host.budget_gb

        why, level = phys.memory_breakdown(
            est, self.cfg.mesh.surface_level_max, budget, need_gb=w.need_gb)

        box = QtWidgets.QMessageBox(self)
        box.setIcon(QtWidgets.QMessageBox.Warning)
        box.setWindowTitle("The mesh will not fit in memory")
        what = (tr("snappyHexMesh (builds the mesh)") if w.stage == "snappy"
                else tr("solver ({ranks} proc.)", ranks=w.host.ranks))
        box.setText(
            tr("{what} on {name}: {cells} cells, that is about "
               "{need_gb:.1f} GB.\nMemory there is {ram_gb} GB, of which "
               "{budget} GB is for the job.", what=what, name=w.host.name, cells=phys.fmt_cells(w.cells), need_gb=w.need_gb, ram_gb=memfit.fmt_gb(w.host.ram_gb), budget=memfit.fmt_gb(budget)))
        detail = fit.details() + "\n\n" + why + "\n\n"
        if fit.saved:
            detail += (tr("The mesh is already saved, but the solver does "
                       "not have enough memory for it: only a smaller "
                       "mesh will help (it must be built). "))
        if level < self.cfg.mesh.surface_level_max:
            detail += (tr("To fit, the surface refinement level must be "
                          "{level} or lower (now {surface_level_max}). "
                          "Each level lower makes the mesh about four "
                          "times smaller.", level=level, surface_level_max=self.cfg.mesh.surface_level_max))
        else:
            detail += (tr("The refinement level is not the cause here — the "
                       "domain or the refinement box makes it grow. "
                       "Check the “Domain” tab."))
        box.setInformativeText(detail)

        fix = box.addButton(tr("Fit Automatically"),
                            QtWidgets.QMessageBox.AcceptRole)
        anyway = box.addButton(tr("Run Anyway"),
                               QtWidgets.QMessageBox.DestructiveRole)
        box.addButton(tr("Cancel"), QtWidgets.QMessageBox.RejectRole)
        box.setDefaultButton(fix)
        box.exec()

        clicked = box.clickedButton()
        if clicked is anyway:
            return True
        if clicked is not fix:
            return False

        if self.diag is not None:
            adv.apply_plan(self.cfg, adv.advise(
                self.cfg, self.diag, cores=self._backend_cores(),
                target_yplus=(None if self.cfg.mesh.yplus_auto
                              else adv.TARGET_YPLUS),
                layer_fit=self._layers_fit, mem=self._mem_fit))
        while (self._mem_fit().verdict == "over"
               and self.cfg.mesh.surface_level_max > 1):
            self.cfg.mesh.surface_level_max -= 1
            self.cfg.mesh.surface_level_min = max(
                1, self.cfg.mesh.surface_level_max - 1)
            self.cfg.mesh.feature_level = self.cfg.mesh.surface_level_max
        self.reload_panels()
        self.refresh_all()

        new_est = self.cfg.estimate()
        peak = max((r.need_gb for r in self._mem_fit().rows), default=0.0)
        QtWidgets.QMessageBox.information(
            self, tr("Fitted"),
            tr("Surface level {surface_level_max}, cells {total}, memory "
               "up to {peak:.1f} GB per machine.\n\nClick “Run” again.", surface_level_max=self.cfg.mesh.surface_level_max, total=phys.fmt_cells(new_est.total), peak=peak))
        return False

    @QtCore.Slot(str, int, int)
    def on_step(self, name: str, index: int, total: int):
        self.panel_run.progress.setMaximum(total)
        self.panel_run.progress.setValue(index)
        # Котрий кут серії зараз — видно з самої смужки, а не лише з журналу.
        s = self._series
        lead = (tr("angle {angles:g}° ({v}/{n_angles}) · ", angles=s.angles[s.at], v=s.at + 1, n_angles=len(s.angles))
                if s is not None and 0 <= s.at < len(s.angles) else "")
        title = be.step_title(name)
        self.panel_run.progress.setFormat(
            f"{lead}{title}  ({index + 1}/{total})")
        self.panel_run.lbl_status.setText(tr("Running: {name}", name=title))

    @QtCore.Slot(bool, str)
    def on_done(self, ok: bool, msg: str):
        if getattr(self, "_closing", False):
            # The window closes and let the run go (leave()): the end of the
            # reading is not the end of the run. Do not record it, do not
            # load results.
            return
        self._laptop("")
        self.live_bar.set_source(None)
        self.timer.stop()
        self.log.flush()
        self.panel_run.set_running(False)
        self.panel_run.progress.setValue(self.panel_run.progress.maximum())
        self.panel_run.progress.setFormat(tr("done") if ok else tr("error"))
        if self._prerun_phase == "pre":
            if self.thread:
                self.thread.quit()
                self.thread.wait(4000)
            self._prerun_finished(ok, msg)
            return

        runner = self.worker.runner if self.worker else None
        soft = getattr(runner, "soft_result", "") if ok else ""
        if soft:
            # Мʼяка зупинка: зріз записано, зібрано й перевірено.
            # soft_result: the slice for the person, already translated.
            parts = [tr("Stopped with a time step written: {strip}. You "
                        "can see it in “Results”.", strip=soft)]
        else:
            parts = [tr("Run finished.") if ok else tr("Stopped: {msg}", msg=msg)]
        st = self.worker.runner.state if (self.worker and self.worker.runner) else None
        run_cfg = self._run_cfg()
        pending_fix = None
        if st:
            if st.cells:
                parts.append(tr("Cells: {cells}", cells=phys.fmt_cells(st.cells)))
            level, why = st.mesh_severity()
            if level == "fatal":
                parts.append(tr("⚠ The mesh is unusable: {why}.", why=why))
            elif level == "warn":
                parts.append(tr("checkMesh: {why} — within normal limits.", why=why))
            elif st.mesh_ok:
                parts.append(tr("checkMesh: no issues."))
            self._layer_fix = None
            self.panel_run.show_layer_fix(None)
            if st.layer_coverage >= 0 and run_cfg is not None:
                # Не просто «замало»: розбір каже, чого саме замало, чим це
                # погано для результату і що підкрутити — а кнопка нижче
                # підкручує. Числа — з налаштувань САМОГО прогону (T38:
                # після «Підключитися» у вікні був інший проєкт). Кнопку
                # дає _offer_layer_fix нижче: після підключення вікно саме
                # відкриває кейс прогону (T37), і тоді поля вже ті.
                diag = self.diag if self._same_model(run_cfg) else None
                fix = adv.layer_diagnosis(
                    run_cfg, st.layer_coverage, diag,
                    faces=getattr(st, "layer_faces", -1.0))
                parts.append(fix.report())
                if fix.fixable and fix.level in ("warn", "bad"):
                    pending_fix = fix
            elif st.layer_coverage >= 0:
                # A run we attached to, without its case on this computer:
                # the share only, no numbers from settings we do not know.
                parts.append(tr("Layers: {pct:.0f}% of the planned layer "
                                "cells grew.", pct=st.layer_coverage * 100))
                self.panel_run.show_layer_fix(None)
            else:
                self.panel_run.show_layer_fix(None)
            # The residual threshold stops only a steady run. A transient
            # run ends at endTime, and its residuals do not go below the
            # threshold (T38).
            steady = (run_cfg.solver.steady if run_cfg is not None
                      else float(getattr(st, "moment", 0.0)).is_integer())
            if ok and steady and not st.converged:
                parts.append(tr("⚠ The residuals did not drop below the "
                                "threshold — convergence is not reached, "
                                "this is not the final result."))
        # Окремими рядками: суцільний абзац на десять рядків ніхто не читає.
        self.panel_run.lbl_status.setText(fs_i18n.join("\n", parts))
        if self.thread:
            self.thread.quit()
            self.thread.wait(4000)
        self.refresh_plots()
        self.record_run(ok, msg, st)
        # Сітка прогону тепер лежить у сховищі бекенда: «Зберегти проєкт із
        # сіткою…» знає, звідки її забрати.
        key = getattr(self.worker, "mesh_key", "") if self.worker else ""
        if ok and key and self.worker is not None:
            self._remote_meshes.add((self._res_key(self.worker.backend), key))
            self._auto_fetch_mesh(self.worker.backend, key)
        # An attach to a run of a case that the window does not show: open
        # that case (its settings, geometry and results). load_results with
        # the settings of the window found no body patch and showed an empty
        # view (T37).
        opening = (self._attach_open
                   if getattr(self.worker, "attach_to", "") else "")
        self._attach_open = ""
        if ok and opening and lib.read_manifest(opening) is not None:
            self._open_attached(opening)
        elif ok:
            self.load_results()
            self.tabs_right.setCurrentWidget(self.results_view)
        # A download from a server takes only the last time: offer the other
        # times of the run (T40).
        w = self.worker
        wb = getattr(w, "backend", None)
        if (ok and w is not None and getattr(wb, "kind", "") == "ssh"
                and getattr(w, "native_dir", "")):
            self._offer_more_times(
                wb, getattr(w, "case_dir", "") or self.case_dir,
                w.native_dir)
        if pending_fix is not None:
            self._offer_layer_fix(pending_fix, parts)
        if self._series is not None:
            self._series_after_run(
                ok, msg, (self.worker.case_dir if self.worker else "")
                or self.case_dir or "")
        # T44: a notification (not for each angle of a series).
        self.run_guard.run_done(ok, msg)

    def record_run(self, ok: bool, msg: str, st) -> None:
        """Дописує прогін у маніфест кейсу — це те, що потім видно в
        «Збережених» без повторного читання логів.

        Кейс беремо з самого прогону, а не з вікна: поки він ішов, у
        «Збережених» могли відкрити інший, і запис пішов би не туди.
        """
        case_dir = (self.worker.case_dir if self.worker else "") or self.case_dir
        if not case_dir or not os.path.isdir(case_dir):
            return
        backend = self.panel_run.current_backend()
        try:
            coeffs = post.summarise_coeffs(be.read_force_coeffs(case_dir),
                                           steady=self._case_steady(case_dir))
        except OSError:
            coeffs = {}
        # The folder of this run on the server (T43, be.run_server_dir).
        wb = getattr(self.worker, "backend", None) if self.worker else None
        server = ({"host": wb.host, "dir": self.worker.native_dir}
                  if wb is not None and wb.kind == "ssh"
                  and getattr(self.worker, "native_dir", "") else None)
        try:
            lib.record_run(
                case_dir, ok=ok,
                seconds=time.monotonic() - getattr(self, "_run_started",
                                                   time.monotonic()),
                # Кроки, а не останній «Time»: у нестаціонарі Time — це
                # секунди, і int(0.2) дав би нуль ітерацій.
                iterations=st.steps if st else 0,
                end_time=(st.moment if (st and not self._case_steady(case_dir))
                          else 0.0),
                cells=st.cells if st else 0,
                converged=bool(st and st.converged),
                coeffs=coeffs,
                backend=f"{backend.label} {backend.version}" if backend else "",
                backend_id=backend.id if backend else "",
                layers=st.layer_coverage if st else -1.0,
                server=server,
                note="" if ok else msg.strip().splitlines()[-1][:200])
        except (OSError, IndexError):
            pass
        self.refresh_library(keep=case_dir)

    def stop_run(self):
        # «Стоп» посеред серії зупиняє всю серію, а не лише поточний кут:
        # інакше наступний прогін стартував би за мить після зупинки.
        if self._series is not None:
            self._series.stopped = True
        if self.worker:
            self.run_guard.user_stopped()
            self.panel_run.lbl_status.setText("Stopping…")
            self.worker.stop()

    def soft_stop_run(self):
        """«Стоп зі зрізом»: дописати поточний момент, зібрати, перевірити."""
        if not self.worker:
            return
        QtWidgets.QApplication.setOverrideCursor(QtCore.Qt.WaitCursor)
        try:
            ok, why = self.worker.soft_stop()
        finally:
            QtWidgets.QApplication.restoreOverrideCursor()
        if not ok:
            QtWidgets.QMessageBox.warning(self, tr("Stop with Time Step"), why)
            return
        if self._series is not None:
            self._series.stopped = True
        self.panel_run.btn_soft_stop.setEnabled(False)
        self.panel_run.lbl_status.setText(tr("Stopping with time step: {why}.", why=why))

    _LAYER_FIELDS = adv.LAYER_FIX_FIELDS

    def _run_cfg(self) -> "fc.CaseCfg | None":
        """The settings of the run that has just ended (T38).

        A run started here keeps a copy (RunWorker.cfg). A run we attached
        to: the manifest (studio.json) of its case, written when the case
        was written. None: an attached run without its case here — then we
        do not know its settings, and the window ones are of another run.
        """
        w = self.worker
        if w is None:
            return self.cfg
        cfg = getattr(w, "cfg", None)
        if cfg is not None:
            return cfg
        case_dir = getattr(w, "case_dir", "") or ""
        if getattr(w, "attach_to", "") or case_dir:
            data = lib.read_manifest(case_dir) if case_dir else None
            if data and isinstance(data.get("cfg"), dict):
                try:
                    return lib.cfg_from_dict(data["cfg"])
                except Exception:                          # noqa: BLE001
                    pass
            if getattr(w, "attach_to", ""):
                return None
        return self.cfg

    def _same_model(self, run_cfg) -> bool:
        """The window holds the same model as `run_cfg` (bodies, extent)."""
        if run_cfg is None:
            return False
        if run_cfg is self.cfg:
            return True
        a, b = run_cfg, self.cfg
        if [x.name for x in a.bodies] != [x.name for x in b.bodies]:
            return False
        size = max(a.body_length, a.body_span, a.body_height)
        return all(abs(p - q) <= 1e-6 * max(size, 1e-9) + 1e-9
                   for p, q in zip(tuple(a.bbox_min) + tuple(a.bbox_max),
                                   tuple(b.bbox_min) + tuple(b.bbox_max)))

    def _same_layer_cfg(self, run_cfg) -> bool:
        """Same model and the same layer settings as the run: only then do
        the "Repair Layers" numbers belong to the fields in the window."""
        if not self._same_model(run_cfg):
            return False
        return all(getattr(run_cfg.mesh, k) == getattr(self.cfg.mesh, k)
                   for k in self._LAYER_FIELDS + ("surface_level_max",)) \
            and run_cfg.domain.base_cells_along_x == \
            self.cfg.domain.base_cells_along_x

    def _offer_layer_fix(self, fix, parts: list) -> None:
        """«Repair Layers» only when the window holds the settings of the
        run; else say why there is no button (T38)."""
        if self._same_layer_cfg(fix.source):
            self._layer_fix = fix
            self.panel_run.show_layer_fix(fix)
            return
        self.panel_run.lbl_status.setText(fs_i18n.join("\n", parts + [tr(
            "The settings in the window are not the settings of this run "
            "(case “{name}”), so “Repair Layers” is not shown. Open this "
            "case from “Saved” to change it.", name=fix.source.name)]))

    def fix_layers(self):
        """Застосовує правки шарів із розбору минулого прогону."""
        diagnosis = getattr(self, "_layer_fix", None)
        if diagnosis is None or not diagnosis.fixable:
            return
        # The fields changed after the advice (or another project is open):
        # the numbers of the advice are no longer about these fields.
        if diagnosis.source is not None and not self._same_layer_cfg(
                diagnosis.source):
            self.panel_run.show_layer_fix(None)
            self._layer_fix = None
            self.panel_run.lbl_status.setText(tr(
                "The layer settings were changed after the run, so the "
                "advice is no longer valid. Run again to get a new one."))
            return
        # A mesh from the project (T16): new layers mean a new mesh. Ask
        # as the locked fields ask.
        if not self.mesh_guard(tr("Repair Layers")):
            return
        stack_before = (self.cfg.mesh.n_layers, self.cfg.mesh.expansion,
                        self.cfg.mesh.final_layer_rel)
        adv.apply_layer_fix(self.cfg, diagnosis)
        # A layer fix from the last run is a hand choice: auto y+ must not
        # undo it on the next change. A wall-resolved fix keeps the stack
        # (coverage knobs only), so auto y+ stays as it was.
        if stack_before != (self.cfg.mesh.n_layers, self.cfg.mesh.expansion,
                            self.cfg.mesh.final_layer_rel):
            self.cfg.mesh.yplus_auto = False
        self.reload_panels()
        self.refresh_all()
        self.panel_run.show_layer_fix(None)
        self._layer_fix = None
        changed = adv.tips_text(diagnosis.tips)
        self.panel_run.lbl_status.setText(
            tr("Changed: {changed}. The mesh must be rebuilt — click "
               "“Run”, the case will be overwritten and snappy will run "
               "again. If the coverage does not go up, the cause is in "
               "the geometry, not in the settings.", changed=changed))
        self.statusBar().showMessage(
            tr("Layers reconfigured. A new run is necessary."), 12000)

    # --- результати ------------------------------------------------------

    def _no_results_text(self) -> str:
        """Чому результатів немає — сказати ПРАВДИВУ причину.

        Раніше тут завжди стояло «не зібрано через reconstructPar». Після
        прогону, що впав до першого запису, це посилало людину збирати те,
        чого не існує (2026-11-09: rhoPimpleFoam упав на 11-му кроці, а
        вкладка радила reconstructPar).
        """
        case = Path(self.case_dir)
        procs = sorted(case.glob("processor*"))
        if procs and fa.time_dirs(str(procs[0])):
            return (tr("The results are in processor*/ and are not "
                    "reconstructed yet. Reconstruct them: reconstructPar "
                    "(or the timeline → “Reconstruct…”)."))
        runs = (lib.read_manifest(self.case_dir) or {}).get("runs") or []
        last = runs[-1] if runs else None
        if last and not last.get("ok"):
            note = " ".join(str(last.get("note") or "").split())[:300]
            note = (note.rstrip(".") + ".") if note else ""
            return (tr("No results: the last run ({get}) crashed without "
                       "writing a single time step.{v} See the “Log”, "
                       "fix the problem and run again.", get=last.get('finished', '?'), v=tr(" Reason: {note}", note=note[:300]) if note else ""))
        return (tr("No results yet: the case was not run, or the run did "
                "not reach the first write."))

    def load_results(self):
        if not self.case_dir or not os.path.isdir(self.case_dir):
            return
        if not post.has_results(self.case_dir):
            # Only the surfaces of the run («surfaces only» download, T40):
            # show them.
            if len(ftl.surface_folders(self.case_dir)) >= 1:
                self._timeline_source("surf")
                return
            self.lbl_coeffs.setText(self._no_results_text())
            return
        self._source = "full"
        try:
            self.results = post.Results(self.case_dir, self.cfg.name)
            self._wall = ()            # поля стінки — від старого читача
            self.live_bar.mark_final()
            times = self.results.times
            # Позиція площини й шкала памʼятаються між моментами часу, але
            # не між кейсами: у чужому домені вона стояла б на межі, а шкала
            # була б у чужих одиницях.
            here = os.path.normcase(str(self.case_dir))
            if here != self._slice_case:
                self._slice_pos.clear()
                self._slice_limits.clear()
                self._seeding.forget_place()
                self._slice_case = here
        except Exception as exc:
            self.lbl_coeffs.setText(tr("Could not read the case: {exc}", exc=exc))
            return

        # Зрізи від попередніх прогонів сітці вже не відповідають: читач
        # видасть або сміття, або нічого. Показувати їх у списку — значить
        # запрошувати на граблі, тому лишаємо лише придатні, а про решту
        # кажемо прямо.
        #
        # Те саме з каталогами БЕЗ ПОЛІВ: у кейсі з адаптацією це самі
        # polyMesh, і вибір такого моменту (а за замовчуванням береться
        # останній) лишає екран порожнім. Відсіюємо лише те, що знаємо за
        # іменем каталогу: момент, якого `time_dirs` не бачить, лишається.
        good, stale = post.matching_times(self.case_dir)
        skip = set(post.time_dirs(self.case_dir)) - set(good)
        if skip:
            times = [t for t in times if t not in skip] or good

        self.cmb_time.blockSignals(True)
        self.cmb_time.clear()
        self.cmb_time.addItems([f"{t:g}" for t in times])
        if times:
            self.cmb_time.setCurrentIndex(len(times) - 1)
        self.cmb_time.blockSignals(False)
        self.slice_bar.setEnabled(True)

        coeffs = be.read_force_coeffs(self.case_dir)
        steady = bool(self.cfg.solver.steady)
        stats = post.summarise_coeffs(coeffs, steady=steady)
        self._cop = be.center_of_pressure(
            stats, self.cfg.flow.l_ref, self.cfg.flow.aoa_deg,
            self.cfg.flow.cofr)
        if stats:
            txt = []
            for name in ("Cd", "Cl", "CmPitch"):
                if name in stats:
                    mean, spread = stats[name]
                    if steady:
                        txt.append(tr("{name} = {mean:+.5f}  (spread over "
                                      "the last 50 iterations {spread:.2e})", name=name, mean=mean, spread=spread))
                    else:
                        # T51 (K2b): the time mean of the polars.
                        txt.append(tr("{name} = {mean:+.5f}  (time mean over "
                                      "the last quarter of the run, spread "
                                      "{spread:.2e})", name=name, mean=mean, spread=spread))
                    # Саме число «0.35» нічого не каже: це може бути й
                    # недобіглий прогін, і коливання, і лікуються вони
                    # протилежно. Тому поряд із розкидом пишемо, ЩО ЦЕ.
                    mark, why = post.coeff_verdict(coeffs.get(name, []),
                                                   steady=steady)
                    if mark != "ok":
                        sign = "⚠" if mark in ("drift", "osc") else "·"
                        txt.append(f"    {sign} {name}: {why}")
            # Сили по частинах — заради них роздільні патчі й робилися. Сума
            # тут не мусить точно збігатися з рядком вище лише в одному
            # випадку: коли частини перетинаються й спільна поверхня
            # потрапляє в обидві. Про це краще знати, ніж дивуватися.
            for body, series in sorted(be.read_body_coeffs(
                    self.case_dir).items()):
                part = post.summarise_coeffs(series, steady=steady)
                if "Cd" in part:
                    bits = [f"{n} = {part[n][0]:+.5f}"
                            for n in ("Cd", "Cl") if n in part]
                    txt.append(f"    {body}:  " + ",  ".join(bits))
            txt.extend(self._engine_report(stats))
            self.lbl_coeffs.setText(fs_i18n.join("\n", txt))
        else:
            self.lbl_coeffs.setText("No coefficients found in postProcessing.")
        if stale:
            cells = post.mesh_cells(self.case_dir)
            self.lbl_coeffs.setText(
                tr("{text}\n⚠ Time directories {join} do not match the "
                   "mesh ({cells} cells): these are fields from earlier "
                   "runs, with a different cell count. ParaView shows "
                   "“cells/points do not match” on such time "
                   "directories. Remove them with the “Free up space” "
                   "button on the “Saved” tab.", text=fs_i18n.shown(self.lbl_coeffs), join=", ".join("%g" % t for t in stale), cells=phys.fmt_cells(cells)))
        self._timeline_fill(times)
        self.draw_results()

    # --- проміжний результат прогону, що йде (fs_live, fs_livebar) ---------

    def _live_start(self) -> None:
        """Прогін пішов: смуга проміжного результату дивиться на нього."""
        w = self.worker
        if w is None:
            return
        self.live_bar.set_source(
            fs_live.Source(w.backend, w.case_dir, w.native_dir or ""),
            lambda: self.cfg)

    def _show_live(self, results, lt, case_dir: str, explicit: bool) -> None:
        """Показати проміжний результат тим самим кодом, що й остаточний.

        `results` має ті самі методи, що й post.Results (patch, internal,
        times), тож малювання не знає, звідки дані.
        """
        if os.path.normcase(str(case_dir or "")) != os.path.normcase(
                str(self.case_dir or "")):
            self.live_bar.mark_final()
            self.statusBar().showMessage(
                tr("The intermediate result belongs to a different case: {case}",
                   case=case_dir), 10000)
            return
        self._anim_stop(redraw=False)
        self.results = results
        self._wall = ()
        self.cmb_time.blockSignals(True)
        self.cmb_time.clear()
        self.cmb_time.addItems([f"{t:g}" for t in results.times])
        self.cmb_time.setCurrentText(f"{lt.time:g}")
        self.cmb_time.blockSignals(False)
        # The live times join the timeline (the times that the live bar
        # downloaded and keeps in its cache).
        self._timeline_fill(list(results.times), lt.time)
        self.slice_bar.setEnabled(True)
        self.lbl_coeffs.setText(fs_livebar.banner(
            lt, bool(self._rcfg.solver.steady)))
        self.draw_results()
        if explicit:
            self.tabs_right.setCurrentWidget(self.results_view)

    # --- timeline and the frames of the player (T40: fs_timebar, fs_anim) ----
    #
    # The timeline lists the times of the view (self.results.times; the rows
    # of the hidden cmb_time are the same times). Each interaction with the
    # timeline (scrub, step, play) shows FAST frames: the geometry of the
    # body and of the slice once, then only the values of each time
    # (update_scalars on the GPU). The frames load in the background
    # (fs_player), the frame on the screen first. «Full View» (or a change
    # of the view: a layer, the slice on or off) draws the full view of the
    # current time again.

    def _timeline_fill(self, times=None, current: float | None = None) -> None:
        """The times of the view → the timeline, and what the case has."""
        if times is None:
            times = list(self.results.times) if self.results is not None else []
        self._tl_times = [float(t) for t in times]
        if current is None:
            i = self.cmb_time.currentIndex()
            current = self._tl_times[i] if 0 <= i < len(self._tl_times) else None
        self.time_bar.set_times(self._tl_times, current)
        entries = ftl.case_times(self.case_dir) if self.case_dir else []
        self.time_bar.set_missing(len(self._missing_times(entries)))
        self.time_bar.set_sources(sum(e.full for e in entries),
                                  sum(e.surf for e in entries), self._source)

    def _missing_times(self, entries=None) -> list[str]:
        """Times that are only in processor*/ of the local case."""
        if not self.case_dir:
            return []
        if entries is None:
            entries = ftl.case_times(self.case_dir)
        return [e.name for e in entries if e.proc and not e.full]

    def _timeline_source(self, kind: str) -> None:
        """«Data»: the full fields of the case or the run surfaces."""
        self._source = "surf" if kind == "surf" else "full"
        self._anim_stop(redraw=False)
        if self._source == "full":
            self.load_results()
            return
        try:
            res = ftl.surface_results(self.case_dir, cfg=self._rcfg)
        except Exception as exc:                          # noqa: BLE001
            self.lbl_coeffs.setText(tr("Could not read the case: {exc}", exc=exc))
            return
        if not res.times:
            return
        self.results = res
        self._wall = ()
        self.cmb_time.blockSignals(True)
        self.cmb_time.clear()
        self.cmb_time.addItems([f"{t:g}" for t in res.times])
        self.cmb_time.setCurrentIndex(len(res.times) - 1)
        self.cmb_time.blockSignals(False)
        self._timeline_fill(res.times, res.times[-1])
        self.slice_bar.setEnabled(True)
        self.draw_results()

    def _timeline_seek(self, i: int) -> None:
        """The person went to frame i: the slider, a step, first or last."""
        if not (0 <= i < len(self._tl_times)):
            return
        self.cmb_time.blockSignals(True)
        self.cmb_time.setCurrentIndex(i)
        self.cmb_time.blockSignals(False)
        if not self._frames_on():
            return
        self._frames_show_or_wait(i)

    def _view_keys(self) -> tuple:
        """What the frames show, from the view: (body, slice, lines, moving)."""
        f = self.cmb_field.currentText()
        body = {"Cp": "cp", "p": "dp", "yPlus": "yplus",
                "wallShearStress": "tau"}.get(f, "cp")
        sl = ""
        if self.slice_bar.is_on():
            k = self.slice_bar.field()
            sl = k if k in fa.SLICE_KEYS else "dU"
        tb = self.time_bar
        streams = self.slice_bar.streams_on() and tb.act_streams.isChecked()
        moving = bool(sl) and tb.act_moving.isChecked()
        return (body, sl, bool(streams), bool(moving))

    def _frames_on(self) -> bool:
        """Frame mode for the current view. False: there is nothing to show."""
        if self.results is None or not self.case_dir or not self._tl_times:
            return False
        keys = self._view_keys()
        if self._frames_plan == keys:
            return True
        return self._frames_start(keys)

    def _time_names(self, live: bool) -> list[str]:
        """Folder names of the timeline times (the fast reader needs them)."""
        names = {}
        if not live:
            for e in ftl.case_times(self.case_dir):
                if e.full:
                    names[f"{e.time:.12g}"] = e.name
        return [names.get(f"{t:.12g}", f"{t:.12g}") for t in self._tl_times]

    def _frames_start(self, keys: tuple) -> bool:
        body, sl, streams, moving = keys
        live = bool(getattr(self.results, "live", False))
        names = self._time_names(live)
        cfg = self._rcfg
        axis = 1 if live else self.slice_bar.axis()
        pos = None if live or not sl else self.slice_bar.position()
        plan = fa.Plan(self.case_dir, names, [b.name for b in cfg.body_patches],
                       body_fields=[body] if body else [],
                       slice_fields=[sl] if sl else [], body=bool(body),
                       slice=bool(sl), streams=streams, moving=moving,
                       axis=axis, pos=pos)
        if moving:
            import fs_fast
            n_cells = fs_fast.n_cells(self.case_dir, names[0]) or 1_000_000
            est = fa.estimate_bytes(plan, n_cells, int(2 * n_cells ** (2 / 3)),
                                    int(n_cells ** (2 / 3)))
            if est > 2e9:
                answer = QtWidgets.QMessageBox.question(
                    self, tr("A lot of memory"),
                    tr("The frames will take about {v:.1f} GB of memory "
                       "({n_times} frames). Clear “moving slice” in the "
                       "frame options (⚙).\n\nLoad them anyway?",
                       v=est / 1e9, n_times=len(names)))
                if answer != QtWidgets.QMessageBox.Yes:
                    self.time_bar.act_moving.setChecked(False)
                    return False
        here = os.path.normcase(str(self.case_dir))
        if here != self._frames_case:
            self._frames_cache.clear()
            self._frames_case = here
        geom = (tuple(names), axis, pos, live, moving, streams,
                tuple(plan.walls))
        old = self._frames_ld
        reuse = None
        if (old is not None and old.frames.fast and not live
                and getattr(self, "_frames_geom", None) == geom):
            reuse = old.refield(plan.body_fields, plan.slice_fields)
        fs = fv.Freestream.from_flow(cfg.flow)
        rotors = [fv.Rotor.from_zone(z) for z in cfg.rotating]
        cache = self._frames_cache
        if reuse is not None:
            make = (lambda r=reuse: r)
        else:
            # Own reader for the thread: the window reads self.results.
            if live:
                src = self.results
                results = fs_live.LiveResults(src.folders, src.info, src.cfg)
            else:
                results = post.Results(self.case_dir, self.cfg.name)
            make = (lambda: fa.Loader(plan, results, fs, rotors=rotors,
                                      force_vtk=live, cache=cache))
        self._frame_loader.stop()
        self._anim_timer_hold = False
        self._frames_ld = None
        self._anim = None
        self._anim_built = False
        self._frames_final = False
        self._frames_plan = keys
        self._frames_geom = geom
        self._anim_keys = (body, sl)
        self.time_bar.set_frame_mode(True)
        self.time_bar.set_progress(0, len(names))
        self._frame_loader.start(make)
        return True

    def _frames_ready(self, ld) -> None:
        """The geometry is here: load the frames, the current one first."""
        self._frames_ld = ld
        self._anim = ld.frames
        for note in ld.frames.notes:
            self.log.push(tr("Timeline: {note}", note=note))
        self._frames_want()

    def _frames_want(self) -> None:
        ld = self._frames_ld
        if ld is not None:
            self._frame_loader.want(self.time_bar.timeline.load_order(ld.done))
        self._frames_progress()

    def _frames_progress(self) -> None:
        ld = self._frames_ld
        n = len(self._tl_times)
        self.time_bar.set_progress(len(ld.done) if ld is not None else 0, n)

    def _frame_loaded(self, i: int) -> None:
        fr, ld = self._anim, self._frames_ld
        if fr is None or ld is None:
            return
        cur = self.time_bar.timeline.index
        if not self._anim_built:
            # Scale limits from the frames that are here; when all frames
            # are here, the common limits of all (one rebuild).
            fa.set_limits(fr)
            self._drop_scene()
            self._anim_build(keep_camera=True)
            self._anim_built = True
            self._anim_show(i if cur not in ld.done else cur)
        elif len(ld.done) == fr.n and not self._frames_final:
            old = dict(fr.limits)
            fa.set_limits(fr)
            self._frames_final = True
            if fr.limits != old:
                self._anim_build(keep_camera=True)
                self._anim_show(self._anim_i if self._anim_i in ld.done else i)
        if i == cur:
            self._anim_show(i)
        self._frames_progress()

    def _frames_failed(self, msg: str) -> None:
        self.log.push(tr("Timeline: the frames could not be loaded: {msg}",
                         msg=msg))
        self.statusBar().showMessage(
            tr("Frames not loaded: {msg}", msg=msg[:200]), 12000)
        self._anim_stop(redraw=self._anim_built)

    def _frames_show_or_wait(self, i: int) -> None:
        ld = self._frames_ld
        if ld is not None and self._anim_built and i in ld.done:
            self._anim_show(i)
        else:
            self._frames_want()

    def _frames_options(self) -> None:
        """⚙: lines in the frames or a moving slice changed."""
        if self._frames_plan is not None:
            self._frames_start(self._view_keys())

    def _frames_view_changed(self, *_args) -> None:
        """The slice field or axis changed while the player shows frames."""
        if self._frames_plan is not None and self._frames_plan != self._view_keys():
            self._frames_start(self._view_keys())

    def _field_changed(self) -> None:
        """«Field on Body»: in frame mode the frames change the field (the
        values of a field loaded once stay in the cache); else a new view."""
        if self._frames_plan is not None:
            self._frames_start(self._view_keys())
            return
        self.draw_results()

    def _anim_build(self, keep_camera: bool = True) -> None:
        """Актори анімації з першого кадру. Далі кадри лише міняють масиви."""
        fr = self._anim
        if fr is None:
            return
        pl = self.res_plotter
        cam = pl.camera_position if keep_camera else None
        pl.clear()
        pl.set_background(fv.BG)
        pl.add_axes(line_width=2)
        self._anim_actors, self._anim_meshes = {}, {}
        bkey, skey = self._anim_keys
        if fr.body is not None:
            m = fr.body.copy()
            m.cell_data[fa.ANIM] = np.zeros(m.n_cells, np.float32)
            if bkey and bkey in fr.body_vals:
                title, sym = fa.BODY_FIELDS[bkey]
                act = pl.add_mesh(
                    m, scalars=fa.ANIM, preference="cell",
                    cmap=fv.diverging_cmap() if sym else "viridis",
                    clim=fr.limits["body:" + bkey], smooth_shading=False,
                    scalar_bar_args=dict(
                        title=tr(title), vertical=True, position_x=0.92,
                        position_y=0.22, width=0.05, height=0.56,
                        color=fv.INK, n_labels=5, fmt="%.3g"))
                bars = pl.scalar_bars
                fv.style_bar(bars[title] if title in bars.keys() else None)
            else:
                act = pl.add_mesh(m, color="#c8ccd0", smooth_shading=False)
            self._anim_actors["body"], self._anim_meshes["body"] = act, m
        first = fr.slice_at(0) if fr.slices else None
        if first is None and fr.slices:
            first = next((s for s in fr.slices if s is not None), None)
        if skey and first is not None and skey in fr.slice_vals:
            spec = fv.spec_by_key(skey)
            sl = first.copy()
            sl.cell_data[fa.ANIM] = np.zeros(sl.n_cells, np.float32)
            act = pl.add_mesh(
                sl, scalars=fa.ANIM, preference="cell",
                cmap=fv.get_cmap(spec.cmap), clim=fr.limits["slice:" + skey],
                scalar_bar_args=dict(title=tr(spec.label), position_x=0.25,
                                     position_y=0.04, width=0.5, height=0.06,
                                     color=fv.INK, n_labels=5, fmt="%.3g"))
            bars = pl.scalar_bars
            fv.style_bar(bars[spec.label] if spec.label in bars.keys() else None)
            self._anim_actors["slice"], self._anim_meshes["slice"] = act, sl
        if fr.streams:
            first = next((s for s in fr.streams if s is not None and s.n_points),
                         None)
            if first is not None:
                act = pl.add_mesh(first.copy(), scalars=fa.ANIM, cmap="turbo",
                                  clim=fr.limits.get("streams", (0.0, 1.0)),
                                  line_width=1.5, show_scalar_bar=False)
                self._anim_actors["streams"] = act
        if cam is not None and keep_camera:
            pl.camera_position = cam
        else:
            pl.reset_camera()
        pl.render()

    def _anim_show(self, i: int) -> None:
        """Показати кадр i: лише підставити готові масиви в актори."""
        fr = self._anim
        ld = self._frames_ld
        if fr is None or not (0 <= i < fr.n):
            return
        if ld is not None and i not in ld.done:
            return
        self._anim_i = i
        bkey, skey = self._anim_keys
        m = self._anim_meshes.get("body")
        if m is not None and bkey in fr.body_vals:
            v = fr.body_vals[bkey][i]
            self._anim_values("body", m, v if v is not None else
                              np.full(m.n_cells, np.nan, np.float32))
        act = self._anim_actors.get("slice")
        if act is not None and skey in fr.slice_vals:
            v = fr.slice_vals[skey][i]
            if len(fr.slices) > 1:
                # Сітка рухається: у кожного кадру своя геометрія зрізу.
                if fr.slices[i] is not None:
                    sl = fr.slices[i].copy()
                    sl.cell_data[fa.ANIM] = (
                        v if v is not None else
                        np.full(sl.n_cells, np.nan, np.float32))
                    act.mapper.SetInputData(sl)
                    self._anim_meshes["slice"] = sl
            else:
                sl = self._anim_meshes["slice"]
                self._anim_values("slice", sl, v if v is not None else
                                  np.full(sl.n_cells, np.nan, np.float32))
        act = self._anim_actors.get("streams")
        if act is not None and fr.streams and fr.streams[i] is not None:
            act.mapper.SetInputData(fr.streams[i])
        self.res_plotter.render()
        self.time_bar.set_index(i)
        self.cmb_time.blockSignals(True)
        self.cmb_time.setCurrentIndex(i)
        self.cmb_time.blockSignals(False)

    def _anim_values(self, key: str, mesh, values) -> None:
        """Нові значення кадру в актор `key`. foamview (fs_view3d) кладе на
        GPU лише значення — геометрія там уже лежить; VTK — через масив
        сітки й Modified()."""
        act = self._anim_actors.get(key)
        if hasattr(act, "set_scalars"):
            act.set_scalars(values)
        else:
            mesh.cell_data[fa.ANIM] = values
            mesh.Modified()

    def _anim_tick(self) -> None:
        tl = self.time_bar.timeline
        ld = self._frames_ld
        n = len(tl)
        if not n:
            self._anim_play(False)
            return
        nxt = tl.index + 1
        if nxt >= n:
            if not tl.loop:
                self.time_bar.set_playing(False)
                self._anim_timer.stop()
                return
            nxt = 0
        if ld is None or not self._anim_built or nxt not in ld.done:
            # The next frame is not here yet: wait for it (it loads next).
            self._frames_want()
            return
        tl.go(nxt)
        self._anim_show(nxt)
        self._frames_want()

    def _anim_play(self, on: bool) -> None:
        if on and len(self._tl_times) >= 2 and self._frames_on():
            self.time_bar.set_playing(True)
            self._anim_timer.start(self.time_bar.timeline.interval_ms())
            self._frames_want()
        else:
            self._anim_timer.stop()
            self.time_bar.set_playing(False)

    def _anim_slice_moved(self, pos: float) -> None:
        """Рухомий зріз (Q35b): пересунути площину без нового читання."""
        fr = self._anim
        if fr is None or not fr.raw:
            return
        try:
            fa.reslice(fr, pos, fv.Freestream.from_flow(self.cfg.flow))
        except ValueError:
            return
        self._anim_build(keep_camera=True)
        self._anim_show(self._anim_i)

    def _anim_export(self) -> None:
        """GIF з поточного ракурсу: кадр за кадром, як на екрані."""
        if not self._frames_on():
            return
        path, _ = QtWidgets.QFileDialog.getSaveFileName(
            self, tr("Save Animation"), str(Path(self.case_dir or ".")
                                           / "animation.gif"),
            "GIF (*.gif)")
        if not path:
            return
        self._anim_play(False)
        # All frames first (the loader of the player can still be busy:
        # wait for it with the busy dialog).
        progress = QtWidgets.QProgressDialog("Loading frames…", "", 0, 0, self)
        progress.setCancelButton(None)
        progress.setWindowModality(QtCore.Qt.WindowModal)
        while (self._frames_ld is None or len(self._frames_ld.done)
               < len(self._tl_times)):
            if self._frames_plan is None:
                _drop_progress(progress)
                return
            self._frames_want()
            QtWidgets.QApplication.processEvents(
                QtCore.QEventLoop.AllEvents, 100)
            time.sleep(0.02)
        _drop_progress(progress)
        fr = self._anim
        from PIL import Image
        keep = self._anim_i
        shots = []
        progress = QtWidgets.QProgressDialog("Writing frames…", "", 0, fr.n, self)
        progress.setCancelButton(None)
        progress.setWindowModality(QtCore.Qt.WindowModal)
        for i in range(fr.n):
            self._anim_show(i)
            img = self.res_plotter.screenshot(return_img=True)
            shots.append(Image.fromarray(np.asarray(img)).convert(
                "P", palette=Image.Palette.ADAPTIVE, colors=255))
            progress.setValue(i + 1)
            QtWidgets.QApplication.processEvents()
        _drop_progress(progress)
        delay = self.time_bar.timeline.interval_ms()
        shots[0].save(path, save_all=True, append_images=shots[1:],
                      duration=delay, loop=0, optimize=False)
        self._anim_show(keep)
        self.log.push(tr("Animation saved: {path} ({n} frames)", path=path, n=fr.n))
        self.statusBar().showMessage(tr("GIF saved: {name}", name=Path(path).name), 8000)

    def _anim_stop(self, redraw: bool = True) -> None:
        """Back from the frames to the full view of the current time."""
        self._anim_timer.stop()
        was = self._anim is not None or self._frames_plan is not None
        self._frame_loader.stop()
        self._anim = None
        self._frames_ld = None
        self._frames_plan = None
        self._anim_built = False
        self._anim_actors, self._anim_meshes = {}, {}
        tb = self.time_bar
        tb.set_playing(False)
        tb.set_frame_mode(False)
        tb.set_progress(0, 0)
        if redraw and was:
            self.draw_results()

    def _anim_reconstruct(self, times: list) -> None:
        """Зібрати моменти з processor*/ у корінь кейсу (Q36a)."""
        if not times or not self.case_dir:
            return
        backend = self.panel_run.current_backend()
        if backend is None or backend.kind == "ssh":
            QtWidgets.QMessageBox.information(
                self, tr("No place to reconstruct"),
                tr("The time steps of a parallel run are reconstructed "
                "where the processor*/ folders of this case are — on a "
                "local backend (WSL, Docker). For a run on a server, use "
                "“Times from Server…”: the server reconstructs them."))
            return
        import subprocess
        cmd = "reconstructPar -newTimes -time '" + ",".join(times) + "'"
        argv = backend.shell(self.case_dir, cmd)
        try:
            proc = self._off_thread(
                tr("Assembling {n_times} times (reconstructPar)…", n_times=len(times)),
                lambda: subprocess.run(argv, capture_output=True,
                                       timeout=6 * 3600,
                                       creationflags=getattr(
                                           subprocess, "CREATE_NO_WINDOW", 0)))
        except Exception as exc:                          # noqa: BLE001
            QtWidgets.QMessageBox.critical(self, "reconstructPar", str(exc))
            return
        tail = (proc.stdout or b"")[-600:].decode("utf-8", "replace")
        self.log.push(tr("reconstructPar ({n_times} time steps): code "
                         "{returncode}. {strip}", n_times=len(times), returncode=proc.returncode, strip=tail.strip()[-300:]))
        self.load_results()

    # --- all times from the server (T40, fs_servertimes, fs_timesdlg) -------

    def times_from_server(self, backend=None, listing=None,
                          remote: str = "") -> None:
        """«Times from Server…»: list the times of the run, then download
        «everything not here», a range «from … to …» or a selection.

        The run folder: fs_servertimes.run_folder (the folder of the last run
        of this case that the window knows, else the name rule).
        """
        if not self.case_dir:
            return
        backend = backend or self._ssh_backend_or_complain()
        if backend is None:
            return
        remote = remote or fst.run_folder(
            self.case_dir, self._run_folders.get(
                os.path.normcase(str(self.case_dir)), ""))
        if listing is None:
            try:
                listing = self._off_thread(
                    tr("Asking {host} which times the run wrote…",
                       host=backend.host),
                    lambda: fst.list_times(backend, remote), BIG_MESH)
            except Exception as exc:                      # noqa: BLE001
                listing = fst.Listing(ok=False, why=str(exc))
        if not listing.ok or not listing.times:
            QtWidgets.QMessageBox.information(
                self, tr("Times on the Server"),
                tr("No times of this case on {host} ({remote}): {why}",
                   host=backend.host, remote=remote,
                   why=listing.why or tr("the run wrote no time")))
            return
        dlg = fs_timesdlg.TimesDialog(self, backend, self.case_dir, remote,
                                      listing)
        if dlg.exec() != QtWidgets.QDialog.Accepted or not dlg.result_info:
            return
        self.fetch_bar.hide()
        self._times_came(dlg.result_info)

    def _times_came(self, got: dict) -> None:
        """Times came from the server (the dialog or the bar): log, reload."""
        self.log.push(tr(
            "Times from the server: {n} times ({mode}), {mb:,.0f} MB in "
            "{s:.0f} s; reconstructed on the server: {rec}.",
            n=len(got.get("came", [])),
            mode={"view": tr("fields for viewing"), "all": tr("all fields"),
                  "surf": tr("surfaces only")}.get(got.get("mode"), ""),
            mb=got.get("bytes", 0) / 1e6, s=got.get("seconds", 0.0),
            rec=len(got.get("reconstructed", []))))
        self._anim_stop(redraw=False)
        if got.get("mode") == "surf":
            self._timeline_source("surf")
        else:
            self._source = "full"
            self.load_results()

    def _offer_more_times(self, backend, case_dir: str, remote: str) -> None:
        """After a download from the server (it takes only the last time):
        ask the server for its other times in the background, and offer
        them in the small bar of the «Results» tab (fs_timesdlg.FetchBar)."""
        if not case_dir or not remote or backend is None:
            return
        self._run_folders[os.path.normcase(str(case_dir))] = remote
        self._stop_offer_job()
        job = fs_timesdlg._Job(lambda _say: fst.list_times(backend, remote))
        th = QtCore.QThread(self)
        th.setObjectName("times-list")
        job.moveToThread(th)
        th.started.connect(job.run)

        def got(listing, exc, case=case_dir):
            self._stop_offer_job()
            if (exc is not None or listing is None or not listing.ok
                    or os.path.normcase(str(self.case_dir or ""))
                    != os.path.normcase(str(case))):
                return
            self.fetch_bar.offer(backend, case, remote, listing)

        # The receiver lives in the window thread (T49). Connected to the
        # plain function, the answer ran in the «times-list» thread: there
        # _stop_offer_job() waited on its own thread («QThread::wait: Thread
        # tried to wait on itself») and FetchBar.offer() made widgets in a
        # worker thread («QObject::setParent: … in a different thread»).
        recv = _Receiver(got, self)
        job.done.connect(recv.take)
        self._offer_job = (th, job, recv)
        th.start()

    def _stop_offer_job(self) -> None:
        th, _job, recv = getattr(self, "_offer_job", None) or (None,) * 3
        self._offer_job = (None, None, None)
        if th is not None:
            th.quit()
            th.wait(20000)
        if recv is not None:
            recv.deleteLater()

    def _engine_report(self, stats) -> list[str]:
        """Рядки звіту про двигуни: виміряна сила проти заданої й підсумок.

        Cd вище — лише планер: кришки двигуна в сили не входять, бо сила
        на них — це імпульс струменя, а не опір. Тому тяга рахується
        окремо, з вимірів на кришках, і тільки тут зводиться з опором.
        """
        if not (self.cfg.engines or self.cfg.disks_on) or not self.case_dir:
            return []
        probes = be.read_port_probes(
            self.case_dir, [b.name for b in self.cfg.port_patches])
        got = post.engine_forces(self.cfg, probes)
        lines = []
        for d in self.cfg.disks_on:
            # Сила диска задана самим диском — її не міряють, а ставлять.
            lines.append(tr("Actuator disk {name}: thrust "
                            "{thrust_now:.2f} N, torque {torque_now:.3g} "
                            "N·m (set).", name=d.name, thrust_now=d.thrust_now, torque_now=d.torque_now))
        for e in self.cfg.engines:
            want = fc.engine_point(e, self.cfg.flow)
            m = got.get(e.name)
            if m is None:
                lines.append(tr("Engine {name}: no measurements on the "
                                "caps (set {thrust:.2f} N).", name=e.name, thrust=want.thrust))
                continue
            mdot = (tr(", mass flow {m:.4g} kg/s", m=m['mdot'])
                    if m["mdot"] is not None else "")
            lines.append(
                tr("Engine {name}: force on the vehicle {m:.2f} N{mdot}. "
                   "Nominal (ṁ·Vₑ − ṁ·V∞) — {thrust:.2f} N.", name=e.name, m=m['thrust'], mdot=mdot, thrust=want.thrust))
            if abs(m["thrust"] - want.thrust) > 0.05 * max(abs(want.thrust),
                                                           1e-9):
                # РОЗБІЖНІСТЬ ТУТ — НЕ ПОМИЛКА, і сказати це треба прямо.
                # Паспортна формула вважає, що вхід бере повітря, яке йде
                # з набігаючою швидкістю. Перед корпусом воно вже
                # загальмоване (на перевірці — 11.8 м/с замість 30), і
                # вхід забирає менше імпульсу. Різницю віддає тиск на
                # корпус, тому сенс має лише сума — чиста сила нижче.
                lines.append(
                    tr("    The difference comes from the air in front of "
                    "the intake not moving at freestream speed. The "
                    "pressure on the body takes it up, so compare the "
                    "net force, not this line."))
        cd = stats.get("Cd", (None, 0.0))[0] if stats else None
        net = post.net_force_line(self.cfg, cd, got, post.disks_thrust(self.cfg))
        if net:
            lines.append(net)
        return lines

    def open_in_paraview(self, case_dir: str = "", name: str = ""):
        """Віддає кейс у ParaView через файл-заглушку <назва>.foam.

        Сам файл порожній: читачеві OpenFOAM потрібен лише його шлях, щоб
        зрозуміти, який каталог вважати кейсом. Створюється він ще на етапі
        «Створити кейс».

        Кейс можна назвати явно — так вкладка «Збережені» відкриває будь-який
        кейс зі списку, не чіпаючи того, що зараз у вікні.
        """
        case_dir = str(case_dir or self.case_dir)
        if not case_dir or not os.path.isdir(case_dir):
            QtWidgets.QMessageBox.information(
                self, tr("No case"), tr("First create the case and run it."))
            return

        foam = post.ensure_foam_file(case_dir, name or self.cfg.name)
        exe = be.find_paraview()
        if not exe:
            box = QtWidgets.QMessageBox(self)
            box.setWindowTitle("ParaView not found")
            box.setTextFormat(QtCore.Qt.RichText)
            box.setText(
                tr("ParaView is not installed.<br><br>You need the "
                   "native Windows build: paraFoam inside WSL needs an X "
                   "server, and WSLg exists only in Windows "
                   "11.<br><br><a "
                   "href=\"{PARAVIEW_URL}\">{PARAVIEW_URL}</a><br><br>After "
                   "you install it, open this file in "
                   "it:<br><code>{foam}</code>", PARAVIEW_URL=be.PARAVIEW_URL, foam=foam))
            copy = box.addButton(tr("Copy Path"), QtWidgets.QMessageBox.ActionRole)
            box.addButton(QtWidgets.QMessageBox.Close)
            box.exec()
            if box.clickedButton() is copy:
                QtWidgets.QApplication.clipboard().setText(foam)
            return

        try:
            import subprocess
            subprocess.Popen([exe, foam])
            self.statusBar().showMessage(f"ParaView: {foam}", 8000)
        except OSError as exc:
            QtWidgets.QMessageBox.critical(self, tr("Could not start"), str(exc))

    def _set_no_fields_note(self, t) -> None:
        """Рядок під екраном про момент, у якому на тілі немає полів. `None` —
        прибрати його. Одна примітка, а не купа: її кожен раз пишуть наново."""
        mark = tr("no fields on the body")
        lines = [ln for ln in fs_i18n.lines(fs_i18n.shown(self.lbl_coeffs))
                 if mark not in ln and tr(mark) not in ln]
        if t is not None:
            lines.append(tr("At time {t:g} {mark} (only the mesh is "
                            "there) — nothing to draw. Pick another time.", t=t, mark=mark))
        self.lbl_coeffs.setText(fs_i18n.join(NEWLINE, lines))

    @property
    def _rcfg(self):
        """Налаштування для показу результатів: ті, що прийшли з ними
        (проміжний результат несе налаштування свого прогону), інакше вікна."""
        return getattr(self.results, "cfg", None) or self.cfg

    def draw_results(self):
        # Звичайний кадр і анімація ділять один вʼюпорт: що б не
        # перемальовувало вкладку, анімацію спершу зупиняємо.
        if self._anim is not None or self._frames_plan is not None:
            self._anim_stop(redraw=False)
        if self.results is None or not self.cmb_time.currentText():
            return
        try:
            t = float(self.cmb_time.currentText())
        except ValueError:
            return
        # Примітка про момент без полів стосується ЛИШЕ того моменту, для
        # якого її написано: перейшли на інший — вона зникає.
        self._set_no_fields_note(None)

        # З роздільними патчами тіло в кейсі не одне: беремо всі й зшиваємо
        # в одну поверхню для показу. Шукати самé `cfg.patch`, як було
        # раніше, означало не знайти нічого — у такому кейсі границі звуться
        # інакше, і вкладка лишалася порожньою без жодного пояснення.
        # Зріз прибираємо ДО будь-якого раннього виходу нижче: інакше зріз
        # попереднього кейса лишався б на вкладці разом із живою ручкою, а
        # підпис під нею описував би вже інший.
        self._drop_scene()
        cfg = self._rcfg
        parts = {b.name: self.results.patch(t, b.name)
                 for b in cfg.body_patches}
        found = [p for p in parts.values() if p is not None]
        if not found:
            names = ", ".join(b.name for b in cfg.body_patches)
            self.lbl_coeffs.setText(
                tr("{text}\nThe results have no patches: {names}.", text=fs_i18n.shown(self.lbl_coeffs), names=names))
            return
        fs = fv.Freestream.from_flow(cfg.flow)
        rotors = [fv.Rotor.from_zone(z) for z in cfg.rotating]
        body, loads, spin = self._wall_fields(t, parts, fs, rotors)
        if body is None:
            body = found[0] if len(found) == 1 else found[0].merge(
                found[1:], merge_points=False)
        post.add_cp(body, cfg.flow)
        post.add_umag(body)

        # `adaptErr` тут не заради повноти переліку. Це поле відповідає на
        # питання, якого інакше ніде не поставити: ЩО САМЕ програма вважає
        # вартим згущення. Поки його не було видно, помилка в адаптації
        # читалася з журналу одним рядком «Selected 0 cells» — і означала
        # вона зовсім не те, на що схожа.
        fields = [f for f in self.results.fields(body)
                  if f in ("Cp", "p", "yPlus", "Umag", "wallShearStress",
                           "nut", "adaptErr")]
        # Розбір по гранях — під людськими назвами (масиви звуться
        # латиницею). Відрив — лише якщо в моменті записане тертя.
        fields += [title for title, arr in fv.BODY_FIELDS.items()
                   if arr in body.cell_data]
        current = self.cmb_field.currentText()
        self.cmb_field.blockSignals(True)
        self.cmb_field.clear()
        self.cmb_field.add_values(fields)
        if current in fields:
            self.cmb_field.setCurrentText(current)
        elif "Cp" in fields:
            self.cmb_field.setCurrentText("Cp")
        self.cmb_field.blockSignals(False)

        field = self.cmb_field.currentText()
        if not field:
            # Порожній екран без жодного слова — найгірше, що тут можна
            # показати: людина шукає, що зламалося. А це лише момент, де в
            # каталозі сітка без полів.
            self._set_no_fields_note(t)
            return

        # Зріз прибираємо першим, а тло вибираємо за тим, чи він буде: на
        # зрізі «нічого не сталося» — це темрява, і світиться лише поле.
        # Без зрізу вигляд лишається колишнім.
        self._drop_scene()
        slice_on = self.slice_bar.is_on()
        streams_on = self.slice_bar.streams_on()
        layers = self.slice_bar.layers()
        dark = slice_on or streams_on or bool(layers)
        self.res_plotter.clear()
        if dark:
            self.res_plotter.set_background(fv.BG)
        else:
            self.res_plotter.set_background("#22262b", top="#3a4149")
        self.res_plotter.add_axes(line_width=2)
        # Показник адаптації — окремою шкалою. «Тепла» двостороння тут
        # збрехала б: у Cp нуль щось означає й середина шкали доречна, а
        # показник нормований на [0, 1], і середина в ньому — просто
        # середина. Одностороння «magma» читається як «темне — спокійно,
        # світле — сюди підуть комірки».
        cmap = ("coolwarm" if field in ("Cp", "p")
                else "magma" if field == "adaptErr" else "viridis")
        bar_args = dict(title=tr(field), n_labels=5, fmt="%.3g")
        if dark:
            # Низ кадру займає шкала зрізу чи ліній, тож шкала тіла йде
            # вертикально праворуч. І в кольорі тексту: типовий чорний на
            # темному тлі не читається.
            bar_args.update(vertical=True, position_x=0.92, position_y=0.22,
                            width=0.05, height=0.56, color=fv.INK)
        if field in fv.BODY_FIELDS:
            # Розбір по гранях — по гранях і малюється: згладжування по
            # точках розмазало б межу відриву на кілька комірок.
            key = fv.BODY_FIELDS[field]
            self._body_actor = self.res_plotter.add_mesh(
                body, scalars=key, preference="cell", smooth_shading=False,
                scalar_bar_args=bar_args, **fv.body_field_style(key, body))
            # Назва кирилицею: без файла шрифта VTK малює її квадратиками.
            bars = self.res_plotter.scalar_bars
            fv.style_bar(bars[field] if field in bars.keys() else None)
        else:
            self._body_actor = self.res_plotter.add_mesh(
                body, scalars=field, cmap=cmap, smooth_shading=True,
                scalar_bar_args=bar_args)
        notes = []
        if loads is not None and (layers or field in fv.BODY_FIELDS):
            notes.append(fv.loads_note(loads, fs, self._rcfg.flow.a_ref,
                                       spin))
            notes.append(fv.separation_note(loads))
        if self._cop is not None:
            sphere = pv.Sphere(radius=body.length * 0.02, center=self._cop)
            self.res_plotter.add_mesh(sphere, color=COL_COP, name="cop")
        if slice_on:
            self._draw_slice(t, parts)
        if streams_on:
            notes += self._draw_streams(t, parts, beside_slice=slice_on) or []
        if layers:
            notes += self._draw_layers(t, parts, body, fs, rotors, layers)
        self._set_layer_notes(notes)
        self._apply_body_visibility()
        # Зріз іде на весь домен, а лінії — далеко за тіло, і камера, вписана
        # в усе це, показала б тіло цяткою. Тому вписуємо тіло з полями.
        # «До моделі» вписує те саме, що вкладка вписала сама: з полями, коли
        # є зріз чи лінії (ручка зрізу стоїть поза тілом), а без них — тіло.
        if dark:
            self._view_box = fv.view_bounds(parts)
            self._model_box = self._view_box
            self.res_plotter.reset_camera(bounds=self._view_box)
        else:
            self._model_box = tuple(body.bounds)
            self.res_plotter.reset_camera()
        self.res_plotter.render()

    # --- зріз -----------------------------------------------------------
    #
    # Вкладка «Результати» не малює зріз сама: цим займається fs_views, той
    # самий код, що робить кадри для звіту. Тут — лише звʼязок смуги керування
    # зі сценою й памʼять про те, що людина вже виставила (позиція й шкала
    # окремо для кожної осі й поля), щоб зміна часу не скидала її вибір.

    # --- шари поверх тіла ------------------------------------------------
    #
    # Малює їх fs_views (ті самі функції роблять кадри для звіту); тут —
    # лише що ввімкнено, звідки взяти обʼєм і що сказати під екраном, коли
    # показати нічим.

    LAYER_MARK = "▸ "

    def _set_layer_notes(self, lines: list) -> None:
        """Рядки про шари під екраном: старі прибрати, нові дописати. Свої
        рядки позначені, і решту тексту (Cd, Cl, попередження) не чіпаємо."""
        keep = [ln for ln in fs_i18n.lines(fs_i18n.shown(self.lbl_coeffs))
                if not ln.startswith(self.LAYER_MARK)]
        self.lbl_coeffs.setText(fs_i18n.join(NEWLINE,
            keep + [self.LAYER_MARK + ln for ln in lines]))

    def _wall_fields(self, t: float, parts: dict, fs, rotors):
        """(тіло, планер, гвинти): тиск і тертя по гранях, раз на момент.
        Не вийшло — (None, None, None), і тіло зшивається як раніше."""
        # Потік і гвинти — теж у ключі: густина множить і тиск, і тертя, а
        # гвинти вирішують, що піде в «планер».
        key = (os.path.normcase(str(self.case_dir)), t, fs, tuple(rotors))
        if self._wall and self._wall[0] == key:
            return self._wall[1:]
        got = (None, None, None)
        try:
            got = fv.part_loads({n: m for n, m in parts.items()
                                 if m is not None}, fs, rotors)
        except Exception:
            pass
        self._wall = (key,) + tuple(got)
        return got

    def _draw_layers(self, t: float, parts: dict, body, fs, rotors,
                     layers: set) -> list:
        """Шари поверх тіла. Повертає рядки для підпису під екраном."""
        notes = []
        self._overlay = fv.Overlay(self.res_plotter)
        need_volume = layers & {"reverse", "loss", "wash"}
        volume = None
        QtWidgets.QApplication.setOverrideCursor(QtCore.Qt.WaitCursor)
        try:
            if need_volume:
                volume = self.results.internal(t)
                if volume is None or "U" not in volume.point_data:
                    notes.append(tr("the results have no velocity in the "
                                 "volume — cannot show reverse flow, "
                                 "wake or slipstream"))
                    volume = None
            if "reverse" in layers and volume is not None:
                blobs = fv.reverse_flow(volume, fs)
                self._overlay.blobs(blobs, fv.REVERSE_COLOR, 0.45, "fs_rev")
                notes.append(fv.reverse_note(blobs))
            if "loss" in layers and volume is not None:
                if "p" in volume.point_data:
                    wake = fv.loss_blobs(volume, fs, bodies=parts)
                    self._overlay.blobs(wake, fv.LOSS_COLOR, 0.2, "fs_loss")
                else:
                    notes.append(tr("no pressure in the volume — cannot "
                                 "compute the loss wake"))
            if "wall" in layers:
                if fv.NEAR in body.point_data:
                    self._overlay.lines(fv.wall_lines(body), fv.WALL_LINE_COLOR,
                                        1.5, "fs_wall")
                else:
                    notes.append(tr("no lines on the body: this time has no "
                                 "wallShearStress"))
            if "wash" in layers and volume is not None:
                if not rotors:
                    notes.append(tr("no source for the slipstream: the case "
                                 "has no propellers"))
                else:
                    wash = fv.prop_wash(volume, parts, fs, rotors)
                    zones, _ = fv.wash_zones(parts, rotors, fs)
                    others = {n: m for n, m in parts.items()
                              if m is not None and n not in zones}
                    self._wash = fv.WashScene(self.res_plotter, wash,
                                              {**zones, **others})
                    notes.append(tr("slipstream: {seeds} lines from the "
                                    "disks; shares — in the legend in "
                                    "the window", seeds=wash.seeds))
        except Exception as exc:
            notes.append(tr("could not build the layer: {exc}", exc=exc))
        finally:
            QtWidgets.QApplication.restoreOverrideCursor()
        return notes

    def _view_toggled(self, on: bool, other_on: bool) -> None:
        """Зріз чи лінії потоку ввімкнули або вимкнули.

        Вмикаючи ПЕРШЕ з двох, ставимо косий вигляд у перспективі: площину
        тягнуть саме в ньому (рівно вздовж нормалі рух миші її не зрушить), а
        лінії потоку здалеку — це смуга. Коли друге вмикають поверх першого,
        вигляд лишаємо, який був: людина його вже вибрала.
        """
        if on and not other_on:
            p = self.res_plotter
            p.camera.parallel_projection = False
            p.view_isometric()
            self._restore_face_prev()
        self.draw_results()
        plane = self._streams_ctx.get("plane") if on and not other_on else None
        if plane is not None and self._scene is None:
            # Лінії лише в одній площині (проміжний результат): у косому
            # вигляді вони злипаються в одну смугу — дивимося рівно на неї.
            p = self.res_plotter
            fv.face_on(p, plane[0])
            if self._view_box is not None:
                p.reset_camera(bounds=self._view_box)
            p.render()

    def _drop_scene(self) -> None:
        """Прибрати зріз і відпустити його віджет. Робити ДО `clear()`: віджет
        чіпляється до вʼюпорта, і очищення сцени його не бачить."""
        scene, self._scene = self._scene, None
        streams, self._streams = self._streams, None
        overlay, self._overlay = self._overlay, None
        wash, self._wash = self._wash, None
        seed, self._seed_widget = self._seed_widget, None
        self._stream_timer.stop()
        self._streams_ctx = {}
        self.lbl_view_note.hide()
        for item in (scene, streams, overlay, wash, seed):
            if item is not None:
                try:
                    item.close()
                except Exception:
                    pass

    def _draw_slice(self, t: float, parts: dict) -> None:
        """Зріз течії: обʼєм на цей момент, контур тіла й площина, яку тягнуть.

        Обʼєм читається лише тут: доки зріз вимкнено, вкладка його не чіпає
        й памʼяті на нього не тратить.
        """
        bar = self.slice_bar
        # Перше читання обʼєму — секунди на великому кейсі, і вікно в цей час
        # стоїть: курсор «очікування» хоч скаже, що воно не повисло.
        QtWidgets.QApplication.setOverrideCursor(QtCore.Qt.WaitCursor)
        try:
            volume = self.results.internal(t)
        finally:
            QtWidgets.QApplication.restoreOverrideCursor()
        fields = fv.available_fields(volume) if volume is not None else []
        if not fields:
            self.lbl_coeffs.setText(
                tr("{text}\nThe results have no volume with U or p — no "
                   "slice to show.", text=fs_i18n.shown(self.lbl_coeffs)))
            return
        bar.set_fields([(s.key, s.title) for s in fields], bar.field())
        axis, key = bar.axis(), bar.field()
        scene = fv.SliceScene(
            self.res_plotter, volume, parts,
            fv.Freestream.from_flow(self._rcfg.flow), axis=axis, field=key,
            pos=self._slice_pos.get(axis), limits=self._slice_limits.get(key),
            edges=bar.chk_edges.isChecked(),
            contour=bar.chk_contour.isChecked(),
            arrows=bar.chk_arrows.isChecked())
        self._scene = scene
        self._slice_pos[axis] = scene.pos
        if bar.chk_widget.isChecked():
            scene.enable_widget(self._slice_dragged)
        self._sync_slice_bar()

    def _sync_slice_bar(self) -> None:
        """Привести смугу у відповідність до сцени: хід повзунка, позицію,
        межі шкали й швидкі переходи."""
        scene, bar = self._scene, self.slice_bar
        if scene is None:
            return
        bar.set_ranges(scene.fine_range(), scene.range)
        bar.set_position(scene.pos)
        bar.set_limits(scene.limits[0], scene.limits[1],
                       scene.spec.kind == "diverging")
        stops = [(tr("Model Center"), scene.centre())]
        span = scene.body_range()
        if span:
            stops += [(tr("Body limit: minimum"), span[0]),
                      (tr("Body limit: maximum"), span[1])]
        for zone in self._rcfg.rotating:
            stops.append((tr("Propeller “{name}”", name=zone.name), zone.centre[scene.axis]))
        bar.set_stops(stops)

    def _apply_body_visibility(self) -> None:
        """Тіло (і його шкала) видно, якщо зрізу нема або галочку не знято."""
        if self._body_actor is None:
            return
        on = not self.slice_bar.is_on() or self.slice_bar.chk_body.isChecked()
        # Обдув фарбує частини тіла сам — у кольори частин; звичайне тіло під
        # ними лише мерехтіло б крізь них.
        on = on and self._wash is None
        self._body_actor.SetVisibility(on)
        field = self.cmb_field.currentText()
        try:
            self.res_plotter.scalar_bars[field].SetVisibility(on)
        except Exception:
            pass

    def _slice_dragged(self, pos: float) -> None:
        """Площину потягли мишею у вʼюпорті: повзунок і число йдуть слідом."""
        if self._scene is not None:
            self._slice_pos[self._scene.axis] = pos
        self.slice_bar.set_position(pos)

    def _slice_moved(self, pos: float) -> None:
        self._slice_moved_streams()
        scene = self._scene
        if scene is None:
            return
        got = scene.set_position(pos)
        self._slice_pos[scene.axis] = got
        if abs(got - pos) > 1e-12:            # позицію підрізали межі домену
            self.slice_bar.set_position(got)

    def _slice_axis(self, axis: int) -> None:
        self._slice_moved_streams()
        scene = self._scene
        if scene is None:
            return
        scene.set_axis(axis, self._slice_pos.get(axis))
        self._slice_pos[axis] = scene.pos
        self._sync_slice_bar()

    def _slice_field(self, key: str) -> None:
        scene = self._scene
        if scene is None or not key:
            return
        scene.set_field(key, limits=self._slice_limits.get(key))
        self._sync_slice_bar()

    def _slice_limits_set(self, lo: float, hi: float) -> None:
        scene = self._scene
        if scene is None:
            return
        self._slice_limits[scene.spec.key] = (lo, hi)
        scene.set_limits(lo, hi)

    def _slice_limits_auto(self) -> None:
        scene = self._scene
        if scene is None:
            return
        self._slice_limits.pop(scene.spec.key, None)
        scene.set_field(scene.spec.key)
        self._sync_slice_bar()

    def _slice_limits_fit(self) -> None:
        scene = self._scene
        if scene is None:
            return
        found = scene.fit_limits()
        if found is not None:
            self._slice_limits_set(*found)
            self.slice_bar.set_limits(found[0], found[1],
                                      scene.spec.kind == "diverging")

    def _slice_widget(self, on: bool) -> None:
        scene = self._scene
        if scene is None:
            return
        if on:
            scene.enable_widget(self._slice_dragged)
        else:
            scene.disable_widget()
        self.res_plotter.render()

    def _restore_face_prev(self) -> None:
        """Повернути галочки «Тіло» й «Площина в вікні» такими, якими людина
        їх залишила до «Виду на зріз» (а не просто «увімкнено»)."""
        if self._face_prev is not None:
            body, widget = self._face_prev
            self._face_prev = None
            self.slice_bar.chk_body.setChecked(body)
            self.slice_bar.chk_widget.setChecked(widget)

    def to_model(self) -> None:
        """«До моделі»: камера в КОЖНОМУ вʼюпорті підлітає до тіла.

        Одна команда на обидва вікна, а не «те, що зараз на екрані»: людина
        тисне кнопку в одному, перемикає вкладку — і хоче побачити модель, а
        не те місце, де камеру лишили.

        Міняється відстань і те, КУДИ камера дивиться. БІК, з якого дивляться
        (спереду, зверху, збоку), і вид проєкції лишаються людині: стрибок на
        інший ракурс збиває з пантелику сильніше, ніж допомагає.
        """
        framed = []
        if self.viewport.has_model:
            self.viewport.fit_body()
            framed.append("3D")
        if self._results_to_model():
            framed.append(tr("results"))
        if framed:
            self.statusBar().showMessage(
                tr("Camera — fit to model: {framed}.", framed=", ".join(framed)), 5000)
        else:
            self.statusBar().showMessage(
                tr("There is no model yet — the camera has nothing to fly to."), 6000)

    def _results_to_model(self) -> bool:
        """«До моделі» у вкладці результатів. False — тіла там ще немає."""
        box = self._model_box
        if box is None or not (box[0] <= box[1] and box[2] <= box[3]
                               and box[4] <= box[5]):
            return False
        p = self.res_plotter
        # `p.camera` читаємо ДО скидання, а не після: pyvista при першому
        # зверненні до «непоставленої» камери мовчки скидає її на всю сцену,
        # і таке звернення ПІСЛЯ нашого скидання затерло б його.
        if self._scene is not None and p.camera.parallel_projection:
            # Дивимося рівно на площину («Вид на зріз»). Вписувати тут за кулею
            # навколо тіла, як у косому вигляді, означало б ВІДДАЛИТИ камеру
            # від того, що вона щойно показувала: куля враховує й глибину, а
            # у плоскому вигляді її нема. Тому вписуємо так само, як сам «Вид
            # на зріз» — за габаритом тіла в площині кадру.
            self._fit_face_on(self._scene)
        else:
            p.reset_camera(bounds=box, render=False)
        # Інакше наступний add_mesh вважатиме камеру «невстановленою» і сам
        # перескочить на межі всієї сцени (те саме, що у Viewport.fit_body).
        p.renderer.camera_set = True
        p.render()
        return True

    def _fit_face_on(self, scene) -> None:
        """Вписує тіло в кадр, коли камера дивиться рівно на площину зрізу."""
        p = self.res_plotter
        w, h = p.window_size
        reach = 10.0 * max(scene.volume.length, 1e-6)
        fv.fit_camera(p, scene.axis, scene.pos, fv.default_rect(scene),
                      (w, h), reach)

    def _slice_view(self, face_on: bool) -> None:
        scene = self._scene
        p = self.res_plotter
        bar = self.slice_bar
        if scene is None:
            # Лише лінії потоку: «3D» усе одно має працювати — це єдиний
            # спосіб повернути косий вигляд після того, як його покрутили.
            if not face_on and self._view_box is not None:
                p.camera.parallel_projection = False
                p.view_isometric()
                p.reset_camera(bounds=self._view_box)
                p.render()
            return
        # Рівно на площину тіло затулило б зріз із ближнього боку, а «ручку»
        # тягнути звідти не можна — рух миші не має складової вздовж нормалі.
        # Тому «Вид на зріз» ховає обидві (контур лишається), а «3D» вертає
        # їх такими, якими вони були.
        if face_on:
            if self._face_prev is None:
                self._face_prev = (bar.chk_body.isChecked(),
                                   bar.chk_widget.isChecked())
            bar.chk_body.setChecked(False)
            bar.chk_widget.setChecked(False)
        else:
            self._restore_face_prev()
        if face_on:
            fv.face_on(p, scene.axis)
            self._fit_face_on(scene)
        else:
            p.camera.parallel_projection = False
            p.view_isometric()
            p.reset_camera(bounds=scene.view_bounds())
        p.render()

    def _draw_streams(self, t: float, parts: dict, beside_slice: bool) -> None:
        """Лінії потоку — звідти, звідки вибрано в смузі (`_seeding`).

        Рахуються кілька секунд, тож курсор — «очікування». Обʼєм той самий,
        що й для зрізу, і читається один раз.
        """
        volume, step = self._stream_volume(t)
        if volume is None or "U" not in volume.point_data:
            self.lbl_coeffs.setText(
                tr("{text}\nThe results have no velocity in the volume — "
                   "there will be no streamlines.", text=fs_i18n.shown(self.lbl_coeffs)))
            return []
        notes = []
        plane = fv.stream_plane(volume)
        if plane is not None:
            notes.append(tr(
                "streamlines: the intermediate result has the velocity only in "
                "the plane {axis} = {pos:.0f} mm, so the lines are in this "
                "plane and follow its in-plane velocity (the seeds are moved "
                "onto it)",
                axis=fv.AXES[plane[0]].upper(),
                pos=round(plane[1] * 1000) + 0.0))
            self.lbl_view_note.setText(tr(
                "⚠ Streamlines only in the plane {axis} = {pos:.0f} mm: this "
                "intermediate result has no 3D velocity. 3D lines come with a "
                "later intermediate result (when the run writes the 3D "
                "velocity sample) or with the final result.",
                axis=fv.AXES[plane[0]].upper(),
                pos=round(plane[1] * 1000) + 0.0))
            self.lbl_view_note.show()
        elif step > 0:
            notes.append(tr(
                "streamlines: intermediate result, a coarse 3D sample of the "
                "velocity with a step of {step:.0f} mm, not the full mesh",
                step=step * 1000))
        fs = fv.Freestream.from_flow(self._rcfg.flow)
        rotors = [fv.Rotor.from_zone(z) for z in self._rcfg.rotating]
        # Обʼєм тут НЕ тримаємо: він гігабайтний, а живе й так у Results
        # рівно один. Кадр ліній бере вже пораховані лінії.
        self._streams_ctx = dict(t=t, bodies=parts, fs=fs, rotors=rotors,
                                 beside_slice=beside_slice,
                                 bounds=fv.bodies_bounds(volume, parts),
                                 plane=plane)
        self._trace_streams(volume)
        self._place_seed_widget()
        return notes

    def _stream_volume(self, t: float):
        """(обʼєм для ліній, крок вибірки в м). У проміжного результату свій
        (fs_live.LiveResults.stream_volume); в остаточного — повна сітка."""
        get = getattr(self.results, "stream_volume", None)
        if get is not None:
            return get(t)
        return self.results.internal(t), 0.0

    def _current_seeding(self) -> fv.Seeding:
        """Вибір зі смуги + те, що памʼятає вікно (куди потягли)."""
        sb, s = self.slice_bar, self._seeding
        s.mode = sb.seed_mode()
        s.count = sb.seed_count()
        s.direction = sb.seed_direction()
        # Радіус зі смуги — лише коли кулю вже поставили: до того в полі
        # стоїть заглушка, а справжній радіус іде від габариту тіла.
        if s.mode == "sphere" and s.radius is not None:
            s.radius = sb.seed_radius()
        s.plane = (sb.axis(), sb.position())
        return s

    def _trace_streams(self, volume) -> None:
        """Порахувати лінії й поставити їх у сцену замість старих. Віджет
        насіння не чіпає: його можуть саме тягнути."""
        ctx = self._streams_ctx
        old, self._streams = self._streams, None
        if old is not None:
            old.close()
        seeding = self._current_seeding()
        QtWidgets.QApplication.setOverrideCursor(QtCore.Qt.WaitCursor)
        try:
            lines = fv.case_streamlines(volume, ctx["bodies"], ctx["fs"],
                                        ctx["rotors"], seeding=seeding)
        except ValueError as exc:
            self.statusBar().showMessage(tr("Streamlines: {exc}", exc=exc), 10000)
            return
        finally:
            QtWidgets.QApplication.restoreOverrideCursor()
        seeds = int(np.asarray(lines.field_data[fv.SEED_COUNT_KEY])[0]) \
            if fv.SEED_COUNT_KEY in lines.field_data else 0
        started = fv.started_lines(lines)
        if not lines.n_points:
            self.statusBar().showMessage(
                tr("Streamlines: no line came out of this place — the seeds "
                   "are outside the domain or inside the body."), 10000)
        elif seeds and started < seeds:
            # «Ліній 1000», а на екрані менше — людина мусить знати чому.
            self.statusBar().showMessage(
                tr("Streamlines: {started} of {seeds} seeds; the other seeds "
                   "are outside the domain, in the body or outside the "
                   "velocity data.",
                   started=started, seeds=seeds), 10000)
        self._streams = fv.StreamScene(self.res_plotter, lines,
                                       vertical=ctx["beside_slice"])
        ctx["label"] = fv.seed_label(seeding, ctx["rotors"])
        ctx["seeding"] = dataclasses.replace(seeding)

    def _place_seed_widget(self) -> None:
        """Відрізок чи кулю — у вʼюпорт, якщо місце таке, що його тягнуть."""
        old, self._seed_widget = self._seed_widget, None
        if old is not None:
            old.close()
        ctx = self._streams_ctx
        s = self._seeding
        if not ctx or s.mode not in fv.SEED_DRAGGABLE:
            return
        bounds, fs = ctx["bounds"], ctx["fs"]
        if s.mode == "line":
            kw = dict(line=s.line or fv.default_line(bounds, fs))
        else:
            centre, radius = fv.default_ball(bounds, fs)
            if s.centre is None:
                s.centre = centre
            if not s.radius:
                s.radius = radius
                self.slice_bar.set_seeding(radius=radius)
            kw = dict(centre=s.centre, radius=s.radius)
        self._seed_widget = fv.SeedWidget(
            self.res_plotter, s.mode, bounds=fv.view_bounds(ctx["bodies"]),
            on_change=self._seed_dragged, **kw)

    def _seed_dragged(self, state: dict) -> None:
        """Відрізок чи кулю відпустили: запамʼятати й перерахувати лінії.
        Не прямо тут — ми всередині події віджета, а перерахунок прибирає
        й ставить актори; тож одразу після неї."""
        s = self._seeding
        if "line" in state:
            s.line = state["line"]
        else:
            s.centre, s.radius = state["centre"], state["radius"]
            self.slice_bar.set_seeding(radius=s.radius)
        QtCore.QTimer.singleShot(0, self._retrace_streams)

    def _retrace_streams(self) -> None:
        """Перерахувати лише лінії: тіло, зріз і шари лишаються як були."""
        self._stream_timer.stop()
        ctx = self._streams_ctx
        if not ctx or self.results is None or not self.slice_bar.streams_on():
            return
        volume = self._stream_volume(ctx["t"])[0]
        if volume is None:
            return
        self._trace_streams(volume)
        self.res_plotter.render()

    def _seeding_changed(self) -> None:
        """Смуга: інше місце, кількість, напрямок чи радіус."""
        sb = self.slice_bar
        mode_was = self._seeding.mode
        self._current_seeding()
        st = self._store()
        st.setValue("streams_seed", sb.seed_mode())
        st.setValue("streams_count", sb.seed_count())
        st.setValue("streams_direction", sb.seed_direction())
        if not self._streams_ctx:
            return
        if self._seeding.mode != mode_was:
            self._place_seed_widget()
            self.res_plotter.render()
        elif self._seed_widget is not None and self._seeding.mode == "sphere":
            self._seed_widget.set_radius(self._seeding.radius)
        self._stream_timer.start()

    def _seed_reset(self) -> None:
        """«На місце»: відрізок і кулю — туди, де вони стоять спочатку."""
        self._seeding.forget_place()
        if not self._streams_ctx:
            return
        self._place_seed_widget()
        self._stream_timer.start()

    def _restore_seeding(self) -> None:
        """Місце, кількість і напрямок — як були минулого разу. Куди тягнули
        відрізок чи кулю, не памʼятаємо: у кожного кейса воно своє."""
        st = self._store()
        try:
            count = int(st.value("streams_count", fv.SEED_COUNT))
        except (TypeError, ValueError):
            count = fv.SEED_COUNT
        self.slice_bar.set_seeding(
            mode=str(st.value("streams_seed", "auto") or "auto"),
            count=count,
            direction=str(st.value("streams_direction", "forward")
                          or "forward"))
        self._current_seeding()

    def _slice_moved_streams(self) -> None:
        """Площину зрізу посунули: якщо лінії ростуть з неї — перерахувати,
        коли рух на мить зупиниться."""
        if self._streams_ctx and self.slice_bar.seed_mode() == "slice":
            self._stream_timer.start()

    def _save_frame(self) -> None:
        """Кадр для звіту: та сама сцена, але позаекранно, з підписом, шкалою
        і масштабом. Зріз, якщо він увімкнений; інакше — лінії потоку. Не
        знімок вікна: знімок бере те, що зараз на екрані, разом з ручкою."""
        scene, streams = self._scene, self._streams
        layers = self.slice_bar.layers()
        field = self.cmb_field.currentText()
        # Без зрізу й ліній кадр — за шарами: обдув, слід або опір, відрив.
        report = ""
        if scene is None and streams is None:
            if "wash" in layers and self._wash is not None:
                report = "wash"
            elif "loss" in layers or field == fv.DRAG_TITLE:
                report = "drag"
            elif layers & {"reverse", "wall"} or field == fv.SEP_TITLE:
                report = "stall"
        if scene is None and streams is None and not report:
            self.statusBar().showMessage(
                tr("Turn on a slice, streamlines or a layer (reverse flow, "
                "wake, surface lines on the body, airflow over the body) "
                "— the frame is made from them."), 8000)
            return
        if scene is not None:
            name = (f"{self.cfg.name}_{scene.spec.key}_"
                    f"{fv.AXES[scene.axis]}{scene.pos * 1000:.0f}mm.png")
        elif streams is not None:
            name = f"{self.cfg.name}_streams.png"
        else:
            name = f"{self.cfg.name}_{report}.png"
        start = str(Path(self.case_dir or ".") / name)
        path, _ = QtWidgets.QFileDialog.getSaveFileName(
            self, tr("Save Frame"), start, "PNG (*.png)")
        if not path:
            return
        QtWidgets.QApplication.setOverrideCursor(QtCore.Qt.WaitCursor)
        try:
            if scene is not None:
                fv.slice_report(
                    [fv.CaseView(scene.volume, scene.bodies, scene.fs, "")],
                    field=scene.spec.key, axis=scene.axis, pos=scene.pos,
                    name=self.cfg.name, limits=scene.limits, edges=scene.edges,
                    contour=scene.contour, path=path, arrows=scene.arrows)
            elif report:
                self._save_layers_report(report, path)
            else:
                ctx = self._streams_ctx
                fv.streams_report(
                    [fv.CaseView(None, ctx["bodies"], ctx["fs"], "")],
                    rotors=ctx["rotors"], name=self.cfg.name,
                    traced=[streams.lines], path=path,
                    seeding=ctx.get("seeding"))
        except Exception as exc:
            QtWidgets.QApplication.restoreOverrideCursor()
            QtWidgets.QMessageBox.critical(self, tr("Could not save the frame"),
                                           str(exc))
            return
        QtWidgets.QApplication.restoreOverrideCursor()
        self.statusBar().showMessage(tr("Frame saved: {path}", path=path), 10000)

    def _save_layers_report(self, report: str, path: str) -> None:
        """Кадр шарів: той самий розбір, що й у вікні, але згори й у 3D."""
        t = float(self.cmb_time.currentText())
        parts = {b.name: self.results.patch(t, b.name)
                 for b in self._rcfg.body_patches}
        parts = {n: m for n, m in parts.items() if m is not None}
        view = fv.CaseView(self.results.internal(t), parts,
                           fv.Freestream.from_flow(self._rcfg.flow), "")
        rotors = [fv.Rotor.from_zone(z) for z in self._rcfg.rotating]
        if report == "wash":
            fv.wash_report(view, rotors, name=self.cfg.name, path=path)
        elif report == "drag":
            fv.drag_report(view, name=self.cfg.name, rotors=rotors,
                           a_ref=self._rcfg.flow.a_ref, path=path)
        else:
            fv.stall_report(view, name=self.cfg.name, rotors=rotors, path=path)

    # --- закриття --------------------------------------------------------

    def tell_last_crash(self, what: str) -> None:
        """The previous start ended with a fatal record in crash.log."""
        path = str(crashlog.log_path())
        self.log.push(tr("Last time the program closed with a crash "
                         "({what}). The record of it: {path}", what=what, path=path))
        self.statusBar().showMessage(
            tr("Last time the program closed with a crash. Record: {path}",
               path=path), 30000)

    def closeEvent(self, event):
        # Пошук машин ще йде (цикл подій крутиться між перевірками, і
        # закриття могло б прийти посеред нього). Закриємо після.
        if getattr(self, "_detecting", False):
            event.ignore()
            self.statusBar().showMessage(
                tr("Wait: still checking the machines, then you can close."), 5000)
            return
        # ПИТАЄМО ПРО ПРОЄКТ ПЕРШИМ — доки нічого не зупинено й не записано.
        #
        # «Скасувати» тут має означати «я лишаюся працювати», а не
        # «половина закриття вже сталася». Тому питання стоїть до всього
        # іншого, і на відмову вікно просто лишається жити.
        if self.ask_save_on_close and self.project_is_dirty():
            answer = QtWidgets.QMessageBox.question(
                self, tr("Save the project?"),
                tr("The scene has unsaved changes.{NEWLINE}{NEWLINE}A "
                   "project is the geometry of each part, the roles, the "
                   "hinges and all settings in one file.", NEWLINE=NEWLINE),
                QtWidgets.QMessageBox.Save | QtWidgets.QMessageBox.Discard
                | QtWidgets.QMessageBox.Cancel,
                QtWidgets.QMessageBox.Save)
            if answer == QtWidgets.QMessageBox.Cancel:
                event.ignore()
                return
            if answer == QtWidgets.QMessageBox.Save:
                # Вікно вибору відкриється в типовій теці з готовим імʼям:
                # натиснути «Зберегти» й нічого не вводити — цілком робочий
                # шлях. А якщо людина передумала просто там, закриття теж
                # скасовується: інакше «Скасувати» тихо означало б «викинь».
                if not self.save_project():
                    event.ignore()
                    return

        # From here the window closes (no way back): on_done must not treat
        # the end of the log reading as the end of the run.
        self._closing = True
        # T44: no more server questions; the tray icon goes away.
        self.run_guard.stop()
        # Сесію пишемо першою: далі йде зупинка потоків, і якщо щось там
        # піде не так, стан уже на диску.
        try:
            self.save_session()
        except Exception:
            pass
        # Кожна деталь, чия сітка в памʼяті, лягає в кеш: після перезапуску
        # вона буде історією, і галочка мусить повернути її такою, якою
        # була — з ремонтом, — а не перечитати вихідний файл.
        for part in self.parts:
            try:
                self._park(part)
            except Exception:
                pass
        try:
            self._save_models()
        except Exception:
            pass
        store = self._store()
        fset.set("restore_session", self.act_session.isChecked())
        store.setValue("geometry", self.saveGeometry())
        store.setValue("splitter", self.split.saveState())

        # ЗАКРИТИ ВІКНО — НЕ ЗНАЧИТЬ ЗУПИНИТИ РАХУНОК.
        #
        # Прогін на сервері пускається відчепленим саме для того, щоб
        # пережити це закриття: людина йде додому, а сервер рахує далі.
        # Доки тут стояв `stop`, наміри «закрити програму» і «зупинити
        # прогін» вели в те саме місце, і годинний рахунок гинув від
        # закритої кришки ноута — тобто рівно від того, заради чого його
        # й відчіплювали. Місцевий прогін гаситься, як і раніше: його
        # процеси все одно нікуди не переживуть.
        if self.worker:
            self.worker.leave()
        if self.thread and self.thread.isRunning():
            self.thread.quit()
            self.thread.wait(3000)
        if self._load_thread and self._load_thread.isRunning():
            self._load_thread.quit()
            self._load_thread.wait(10000)
        if self._fix_thread and self._fix_thread.isRunning():
            self._fix_thread.quit()
            self._fix_thread.wait(10000)
        # The background loader of the timeline frames (T40).
        self._anim_timer.stop()
        self._frame_loader.close()
        self._stop_offer_job()
        self.fetch_bar.stop()
        self._drop_scene()
        try:
            self.viewport.plotter.close()
            self.res_plotter.close()
        except Exception:
            pass
        super().closeEvent(event)


def end_process(win, rc: int, grace_ms: int = 5000) -> None:
    """End the process after app.exec() without a qFatal.

    closeEvent waits a limited time for each worker thread. A thread that
    still runs after that is a child of the window, and the end of Python
    destroys the window and its children: QThread::~QThread on a running
    thread is qFatal("QThread: Destroyed while thread ... is still running"),
    then abort() (c0000409 in Qt6Core.dll, T26). So: wait a short time more,
    and if a thread still runs, write it to crash.log and end the process
    with os._exit, without the destruction of the window. The session and
    the settings are already on the disk (closeEvent).
    """
    running = [t for t in win.findChildren(QtCore.QThread) if t.isRunning()]
    deadline = time.monotonic() + grace_ms / 1000.0
    for t in running:
        t.quit()
        t.wait(max(0, int((deadline - time.monotonic()) * 1000)))
    still = [t for t in running if t.isRunning()]
    if still:
        names = ", ".join(t.objectName() or type(t).__name__ for t in still)
        crashlog.note(f"exit: QThread still running ({names}); "
                      "os._exit instead of a qFatal")
        crashlog.finish()
        try:
            QtCore.QSettings(SETTINGS_ORG, SETTINGS_APP).sync()
        except Exception:                                    # noqa: BLE001
            pass
        for stream in (sys.stdout, sys.stderr):
            try:
                if stream is not None:
                    stream.flush()
            except Exception:                                # noqa: BLE001
                pass
        os._exit(rc)
    # Delete the windows while the QApplication lives: a window or dialog
    # that lived on to the end of Python crashed the exit with PySide6 6.12
    # (0xC0000005, T51).
    for w in QtWidgets.QApplication.topLevelWidgets():
        try:
            w.deleteLater()
        except RuntimeError:
            pass
    QtCore.QCoreApplication.sendPostedEvents(None, QtCore.QEvent.DeferredDelete)
    crashlog.finish()
    sys.exit(rc)


SERVER_WIZARD_FLAG = "--server-wizard"


def main():
    crashed = crashlog.last_crash()     # before install() starts a new record
    crashlog.install()
    QtCore.QCoreApplication.setAttribute(QtCore.Qt.AA_ShareOpenGLContexts)
    fs_icons.own_taskbar_button(fs_icons.OFS_APP_ID)
    app = QtWidgets.QApplication(sys.argv)
    app.setStyle("Fusion")
    app.setWindowIcon(fs_icons.icon(fs_icons.OFS))
    win = MainWindow()
    win.show()
    if crashed:
        win.tell_last_crash(crashed)
    if len(sys.argv) > 1 and os.path.isfile(sys.argv[1]):
        win.load_stl(sys.argv[1])
    if SERVER_WIZARD_FLAG in sys.argv[1:]:
        # The installer starts the program with this flag (its last page).
        QtCore.QTimer.singleShot(800, win.open_server_wizard)
    elif (fs_paths.FROZEN and not fset.get("server_wizard_offered")
          and not fset.get("ssh_host")):
        QtCore.QTimer.singleShot(2500, win.offer_server_wizard)
    end_process(win, app.exec())


if __name__ == "__main__":
    main()
