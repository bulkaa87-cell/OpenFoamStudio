"""Вигляди течії: зріз, який можна рухати, контур тіла й кадри для звіту.

Вкладка «Результати» і картинка, яку людина надсилає колезі, мусять малювати
ОДНЕ Й ТЕ САМЕ. Інакше виходить дві програми: у вікні зріз один, а в PNG —
інший, з іншою шкалою, і звірити їх ніхто не зможе. Тому весь малюнок живе
тут і від Qt не залежить: сцена малює в будь-який `pyvista.Plotter` — і в
живий вʼюпорт вікна, і в позаекранний, звідки виходять кадри для звіту.

Що робить картинку читабельною — це правила, а не смак:

  * ВІДНОСНІ ПОЛЯ. Швидкість і тиск самі по собі майже скрізь дорівнюють
    набігаючому потоку, тож картинка виходить рівною плямою з рідкими
    відмінностями. Віднімаємо потік (U − U∞, p − p∞) — і лишається тільки
    те, що тіло зробило з повітрям: слід, обдув гвинтом, розрідження над
    крилом.
  * СИМЕТРИЧНА ШКАЛА навколо нуля для знакових полів. Інакше нуль ховається
    десь збоку, і за кольором не відрізнити прискорення від гальмування.
    Полюси — різних кольорів (синій «менше», теплий «більше»), середина —
    нейтральна.
  * ШКАЛА НЕ ПЛИВЕ. Межі рахуються один раз на весь обʼєм, а не на кожен
    зріз: коли площину рухають, колір міняється лише там, де змінилося
    поле, а не тому, що шкала підлаштувалася під нову картинку. З тієї ж
    причини два кейси для порівняння мусять брати ОДНУ шкалу.
  * ТЕМНА СЕРЕДИНА. Ділянка «нічого не сталося» на темному тлі має бути
    темною — тоді світиться лише те, що відбувається. Світла середина
    (coolwarm) заливає весь домен бежевою плямою, і слід тонне в ній.
  * КОНТУР ТІЛА поверх зрізу. У самому тілі сітки немає, тож воно виходить
    чорною плямою без краю; тонкий світлий контур показує, ЧИЙ це переріз.
  * ОДИНИЦІ Й ПОХОДЖЕННЯ в підписі: що показано, де і при якій швидкості.
"""

from __future__ import annotations

import math
import os
import time
from dataclasses import dataclass, field, replace
from functools import lru_cache

import matplotlib
import numpy as np
import pyvista as pv
import vtk
from matplotlib.colors import Colormap, LinearSegmentedColormap, Normalize
from matplotlib.ticker import MaxNLocator
from fs_i18n import tr

BG = "#0b0d10"            # тло вʼюпорта результатів
INK = "#e6ebf1"           # основний текст і контур тіла
INK_DIM = "#9aa4b0"       # другорядний текст, ділення шкали
EDGE = "#4a525c"          # ребра комірок на зрізі

AXES = "xyz"

# У VTK нема власного кириличного шрифта: без файла підписи виходять
# квадратиками (див. коментар біля лінійки у вʼюпорті). DejaVu Sans лежить у
# matplotlib, який і так обовʼязковий, — тож окремого шрифта не возимо.
FONT = os.path.join(matplotlib.get_data_path(), "fonts", "ttf", "DejaVuSans.ttf")


# ==========================================================================
# Кольори
# ==========================================================================
#
# Двобічну шкалу з темною серединою будуємо в OKLab, а не в sRGB: там рівні
# кроки числа — це рівні кроки ЯСКРАВОСТІ для ока, і від темного центру до
# країв яскравість росте рівномірно з обох боків. У sRGB та сама лінійна
# інтерполяція дає «провал» посередині одного з плечей.

_M1 = np.array([[0.4122214708, 0.5363325363, 0.0514459929],
                [0.2119034982, 0.6806995451, 0.1073969566],
                [0.0883024619, 0.2817188376, 0.6299787005]])
_M2 = np.array([[0.2104542553, 0.7936177850, -0.0040720468],
                [1.9779984951, -2.4285922050, 0.4505937099],
                [0.0259040371, 0.7827717662, -0.8086757660]])


def _to_lab(hexes) -> np.ndarray:
    rgb = np.array([[int(h[i:i + 2], 16) / 255.0 for i in (1, 3, 5)]
                    for h in hexes])
    lin = np.where(rgb <= 0.04045, rgb / 12.92, ((rgb + 0.055) / 1.055) ** 2.4)
    return np.cbrt(lin @ _M1.T) @ _M2.T


def _from_lab(lab: np.ndarray) -> np.ndarray:
    lin = ((lab @ np.linalg.inv(_M2).T) ** 3) @ np.linalg.inv(_M1).T
    lin = np.clip(lin, 0.0, 1.0)
    return np.where(lin <= 0.0031308, 12.92 * lin,
                    1.055 * lin ** (1 / 2.4) - 0.055)


def _ramp(anchors: list[str], n: int) -> np.ndarray:
    lab = _to_lab(anchors)
    at = np.linspace(0.0, 1.0, len(anchors))
    s = np.linspace(0.0, 1.0, n)
    return _from_lab(np.column_stack(
        [np.interp(s, at, lab[:, k]) for k in range(3)]))


@lru_cache(maxsize=1)
def diverging_cmap() -> LinearSegmentedColormap:
    """Синє — «менше», теплий — «більше», посередині майже чорне.

    Пара синій/помаранчевий, а не червоний/зелений: вона лишається
    розрізненою при найпоширеніших порушеннях кольоророзрізнення. Кінці
    світліші за середину, тож найсильніше відхилення — найяскравіше.
    """
    cold = _ramp(["#15181d", "#1b2f6e", "#2a6fd6", "#4fc3f7", "#b6ecff"], 128)
    warm = _ramp(["#15181d", "#6e1d1b", "#d2451f", "#ff9d3c", "#ffe08a"], 128)
    return LinearSegmentedColormap.from_list(
        "fs_diverging", np.vstack([cold[::-1], warm]), N=256)


@lru_cache(maxsize=1)
def size_cmap() -> LinearSegmentedColormap:
    """magma навпаки, але без чорного кінця: крупні комірки — темно-пурпурові,
    а не чорні. Чорне на чорному тлі не видно, і кінець шкали виглядав би
    обрізаним."""
    base = matplotlib.colormaps["magma_r"](np.linspace(0.0, 0.86, 256))
    return LinearSegmentedColormap.from_list("fs_size", base, N=256)


def get_cmap(name: str) -> Colormap:
    if name == "fs_diverging":
        return diverging_cmap()
    if name == "fs_size":
        return size_cmap()
    if name == "fs_sep":
        return sep_cmap()
    return matplotlib.colormaps[name]


# ==========================================================================
# Набігаючий потік і поля
# ==========================================================================

@dataclass(frozen=True)
class Freestream:
    """Від чого віднімати й уздовж чого рахувати.

    Окремий маленький тип, а не сама конфігурація кейсу: кадри роблять і з
    кейсів, у яких конфігурації немає (старі прогони без маніфесту), і тоді
    ці числа беруться з controlDict.
    """
    u_inf: float = 1.0
    direction: tuple = (1.0, 0.0, 0.0)
    rho: float = 1.225
    p_inf: float = 0.0
    nu: float = 1.47e-5
    compressible: bool = False

    @classmethod
    def from_flow(cls, flow) -> "Freestream":
        """З `FlowCfg`. Відлік тиску — як у fs_post.cp_scale: у нестисливому
        режимі тиск кінематичний і рахується від нуля, у стисливому —
        абсолютний, від p_inf."""
        comp = bool(getattr(flow, "compressible", False))
        return cls(u_inf=float(flow.u_inf),
                   direction=tuple(float(x) for x in flow.direction),
                   rho=float(flow.rho),
                   p_inf=float(flow.p_inf) if comp else 0.0,
                   nu=float(flow.nu), compressible=comp)

    @property
    def velocity(self) -> np.ndarray:
        return self.u_inf * np.asarray(self.direction, dtype=float)

    @property
    def dynamic_pressure(self) -> float:
        return 0.5 * self.rho * self.u_inf ** 2


@dataclass(frozen=True)
class FieldSpec:
    key: str
    title: str          # рядок у списку вибору
    label: str          # підпис шкали, з одиницями
    kind: str           # "diverging" — симетрична навколо нуля; "sequential"
    cmap: str
    arrays: tuple       # які масиви мусять бути в сітці
    about: str = ""     # як це читати — одним реченням
    # Як обирається шкала (див. Sampler). Верхній процентиль різний, бо
    # «хвіст» у полів різний: у швидкості він — сама стінка (там U = 0, тобто
    # U∥ − U∞ = −U∞ по всьому тонкому шару коло тіла), і його треба
    # відрізати; у тиску хвіст — це якраз те, заради чого дивляться
    # (розрідження на кінцях лопаті), і відрізати його не можна.
    pct: float = 99.0
    floor: float | None = None   # фіксований низ односторонньої шкали
    weighted: bool = True        # зважувати площею, а не рахувати комірки

    def __post_init__(self):
        # The texts are shown in the interface language. The language is
        # read before this module is imported, and a change needs a restart.
        for name in ("title", "label", "about"):
            object.__setattr__(self, name, tr(getattr(self, name)))


FIELDS = (
    FieldSpec("dU", "Speed gain along the flow  U∥ − U∞",
              "U∥ − U∞,  m/s", "diverging", "fs_diverging", ("U",),
              "warm — the air is faster than the freestream (propeller "
              "wash, flow around the body), blue — slower (wake, "
              "deceleration)", pct=99.0),
    FieldSpec("swirl", "Swirl  |U⊥|",
              "cross-flow speed U⊥,  m/s", "sequential", "magma", ("U",),
              "speed across the flow: swirl behind the propeller, vortices",
              pct=99.9, floor=0.0),
    FieldSpec("umag", "Speed  |U|",
              "speed U,  m/s", "sequential", "turbo", ("U",),
              "speed magnitude", pct=99.5, floor=0.0),
    FieldSpec("dp", "Pressure  p − p∞",
              "p − p∞,  Pa", "diverging", "fs_diverging", ("p",),
              "warm — pressure higher than the freestream (stagnation), "
              "blue — lower (suction)", pct=99.9),
    FieldSpec("cp", "Pressure coefficient  Cp",
              "Cp", "diverging", "fs_diverging", ("p",),
              "the same pressure, divided by the dynamic pressure of the flow",
              pct=99.9),
    FieldSpec("nut", "Eddy viscosity  νt/ν",
              "νt / ν", "sequential", "magma", ("nut",),
              "how many times the turbulent viscosity is larger than the molecular one",
              pct=99.5, floor=0.0),
    FieldSpec("cell", "Mesh cell size",
              "lg of cell size,  mm", "sequential", "fs_size", (),
              "lighter — smaller cell; strokes — the cells themselves",
              pct=99.5, weighted=False),
)

# Розмір комірки — не поле розвʼязку, а властивість сітки: рахується з
# обʼєму комірки й кладеться в обʼєм окремим масивом.
CELL_LG = "fs_lgsize"
VALUE = "fs_val"


def spec_by_key(key: str) -> FieldSpec:
    for spec in FIELDS:
        if spec.key == key:
            return spec
    raise KeyError(key)


def available_fields(volume) -> list[FieldSpec]:
    """Які поля можна показати на цьому обʼємі: усе, чиї масиви є в ньому."""
    have = set(volume.point_data.keys()) | set(volume.cell_data.keys())
    return [s for s in FIELDS if all(a in have for a in s.arrays)]


def add_cell_size(volume, chunk: int = 400_000) -> None:
    """Кладе в обʼєм lg(розміру комірки, мм). Раз на обʼєм — далі береться з
    нього.

    Розмір — кубічний корінь із габаритної коробки комірки: для
    вирівняної по осях комірки (а сітка snappyHexMesh здебільшого така) це
    те саме, що ребро рівновеликого куба. Точний обʼєм рахував би
    vtkCellSizeFilter, але на 2.5 млн комірок із 300 тисячами багатогранників
    це 14–25 секунд, коли вікно стоїть. Габарит виходить за пів секунди:
    мінімум і максимум координат вершин комірки, шматками, щоб не роздувати
    память.
    """
    if CELL_LG in volume.cell_data:
        return
    pts = np.asarray(volume.points)
    conn = np.asarray(volume.cell_connectivity)
    offs = np.asarray(volume.cell_offsets)
    n = volume.n_cells
    size = np.empty(n)
    for a in range(0, n, chunk):
        b = min(a + chunk, n)
        first = offs[a]
        p = pts[conn[first:offs[b]]]
        starts = offs[a:b] - first
        ext = np.maximum(np.maximum.reduceat(p, starts, axis=0)
                         - np.minimum.reduceat(p, starts, axis=0), 1e-12)
        size[a:b] = np.cbrt(ext.prod(axis=1))
    volume.cell_data[CELL_LG] = np.log10(np.maximum(size * 1000.0, 1e-9))


def values(mesh, spec: FieldSpec, fs: Freestream, where: str = "point"):
    """(де, масив): значення поля на точках або комірках `mesh`.

    Гладкіше на точках, чесніше на комірках — так порахував розвʼязувач.
    Що просили, те й беремо, а якщо цього немає — друге. Немає жодного —
    (None, None).
    """
    if spec.key == "cell":
        arr = mesh.cell_data.get(CELL_LG)
        return ("cell", np.asarray(arr)) if arr is not None else (None, None)
    got = None
    for w in (("point", "cell") if where == "point" else ("cell", "point")):
        store = mesh.point_data if w == "point" else mesh.cell_data
        if all(n in store for n in spec.arrays):
            got = {n: np.asarray(store[n], dtype=float) for n in spec.arrays}
            break
    if got is None:
        return None, None

    d = np.asarray(fs.direction, dtype=float)
    if spec.key == "umag":
        v = np.linalg.norm(got["U"], axis=1)
    elif spec.key in ("dU", "swirl"):
        dU = got["U"] - fs.velocity
        along = dU @ d
        v = along if spec.key == "dU" else np.linalg.norm(
            dU - along[:, None] * d, axis=1)
    elif spec.key in ("dp", "cp"):
        # Кінематичний тиск помножити на густину, абсолютний — відняти
        # відлік. Плутати їх не можна: різниця — у сотню тисяч разів.
        p = got["p"]
        dp = (p - fs.p_inf) if fs.compressible else p * fs.rho
        if spec.key == "dp":
            v = dp
        else:
            q = fs.dynamic_pressure
            if q <= 0:
                return None, None
            v = dp / q
    elif spec.key == "nut":
        if fs.nu <= 0:
            return None, None
        v = got["nut"] / fs.nu
    else:
        return None, None
    return w, v


def nice_limits(lo: float, hi: float, symmetric: bool):
    """Межі шкали, округлені назовні до «круглих» ділень, і самі ділення.

    Кількість написів на шкалі береться з цих ділень, а VTK розставляє їх
    рівномірно — тож межі мусять бути кратні кроку, інакше підписи вийдуть
    на кшталт 7.5 і 22.5.
    """
    if symmetric:
        h = max(abs(lo), abs(hi))
        ticks = MaxNLocator(nbins=6, steps=[1, 2, 2.5, 5, 10]).tick_values(-h, h)
    else:
        ticks = MaxNLocator(nbins=5, steps=[1, 2, 2.5, 5, 10]).tick_values(lo, hi)
    ticks = [float(t) for t in ticks]
    return ticks[0], ticks[-1], ticks


def ticks_for(lo: float, hi: float, symmetric: bool) -> list[float]:
    """Позначки шкали В МЕЖАХ [lo, hi]. Межі, задані рукою, «круглими» не
    бувають — позначки за ними просто відкидаються."""
    eps = 1e-9 * max(abs(hi - lo), 1e-12)
    got = [t for t in nice_limits(lo, hi, symmetric)[2]
           if lo - eps <= t <= hi + eps]
    return got if len(got) >= 2 else [lo, hi]


def _wpercentile(v: np.ndarray, w: np.ndarray, q: float) -> float:
    """Процентиль із вагами. Вага — площа полігона: кожен піксель кадру
    важить однаково, а не кожна комірка (дрібних комірок коло стінки
    тисячі, і без ваг вони б одні й задавали шкалу)."""
    order = np.argsort(v)
    v, w = v[order], w[order]
    cum = np.cumsum(w)
    if not cum[-1] > 0:
        return float(np.percentile(v, q))
    return float(np.interp(q / 100.0, cum / cum[-1], v))


class Sampler:
    """Кілька зрізів навколо тіла, з яких береться шкала.

    ЧОМУ НЕ ВЕСЬ ОБʼЄМ. Спершу шкала рахувалася по всіх комірках, і вийшло
    ±40 м/с там, де слід і обдув гвинта дають 10–20. Причина проста: сітка
    густа коло стінки, а на стінці U = 0, тобто U∥ − U∞ = −U∞ у кожній із
    сотень тисяч тонких комірок шару. Їх стільки, що вони самі задають
    верхній процентиль, а те, що хотіли побачити, тоне.

    Тому беремо ЗРІЗИ по всьому розмаху тіла, обмежені рамкою навколо нього,
    і рахуємо процентиль, зваживши на площу: шкала визначається тим, що
    справді займає місце на картинці, а не кількістю комірок. Порівняно з
    ручним вибором на реальному кейсі (гвинти, 28 м/с) вийшло близько до
    того самого: ±15 м/с за швидкістю проти вибраних ±20.

    Зрізи беруться раз на вісь і лишаються: зміна поля не різатиме обʼєм
    заново — це найдорожча частина.
    """

    def __init__(self, volume, bodies: dict, axis: int, planes: int = 7):
        self.axis = axis
        self.slices: list = []
        parts = [m for m in (bodies or {}).values()
                 if m is not None and m.n_points]
        if parts:
            bb = np.asarray([m.bounds for m in parts]).reshape(-1, 3, 2)
            lo3, hi3 = bb[:, :, 0].min(axis=0), bb[:, :, 1].max(axis=0)
        else:
            vb = np.asarray(volume.bounds).reshape(3, 2)
            lo3, hi3 = vb[:, 0], vb[:, 1]
        u, v = plane_axes(axis)
        pad = 0.3 * float(max(hi3[u] - lo3[u], hi3[v] - lo3[v], 1e-9))
        self.box = (float(lo3[u] - pad), float(hi3[u] + pad),
                    float(lo3[v] - pad), float(hi3[v] + pad))
        along = (float(lo3[axis]), float(hi3[axis]))
        # Краї розмаху не беремо: там площина ледь торкається тіла й нічого
        # характерного на ній немає.
        for pos in np.linspace(along[0], along[1], planes + 2)[1:-1]:
            s = cut(volume, axis, float(pos))
            if s.n_cells:
                self.slices.append(self._prepare(s))

    def _prepare(self, s):
        """(зріз, площі полігонів, маска «у рамці»)."""
        u, v = plane_axes(self.axis)
        # Клітинні дані потрібні, бо вага — на полігон. Якщо в обʼємі є
        # самі точкові (свій синтетичний обʼєм, чужий формат), переносимо.
        if not s.cell_data.keys() and s.point_data.keys():
            s = s.point_data_to_cell_data(pass_point_data=True)
        s = s.compute_cell_sizes(length=False, area=True, volume=False)
        c = s.cell_centers().points
        u0, u1, v0, v1 = self.box
        keep = ((c[:, u] > u0) & (c[:, u] < u1)
                & (c[:, v] > v0) & (c[:, v] < v1))
        return s, np.asarray(s.cell_data["Area"], dtype=float), keep

    @staticmethod
    def _gather(items, spec: FieldSpec, fs: Freestream):
        vals, wts = [], []
        for s, area, keep in items:
            where, v = values(s, spec, fs, "cell")
            if v is None or where != "cell":
                continue
            ok = keep & np.isfinite(v)
            vals.append(v[ok])
            wts.append(area[ok] if spec.weighted else np.ones(int(ok.sum())))
        if not vals:
            return None
        v, w = np.concatenate(vals), np.concatenate(wts)
        return (v, w) if v.size else None

    @staticmethod
    def _reduce(v: np.ndarray, w: np.ndarray, spec: FieldSpec):
        if spec.kind == "diverging":
            h = _wpercentile(np.abs(v), w, spec.pct)
            if not h > 1e-12:                # поле скрізь нуль: шкала «одиниця»
                h = 1.0
            lo, hi, _ = nice_limits(-h, h, True)
            return lo, hi
        hi = _wpercentile(v, w, spec.pct)
        lo = spec.floor if spec.floor is not None else _wpercentile(v, w, 0.5)
        # Рівномірна сітка дає однакові розміри комірок, і шкала стискається в
        # точку — з нею ні позначок, ні кольору. Тому не «hi <= lo», а з запасом.
        if hi - lo <= 1e-9 * max(abs(hi), 1.0):
            hi = lo + 1.0
        lo, hi, _ = nice_limits(lo, hi, False)
        return lo, hi

    def limits(self, spec: FieldSpec, fs: Freestream):
        """Межі шкали для поля за всіма зрізами. None — поля на них немає."""
        got = self._gather(self.slices, spec, fs)
        return self._reduce(*got, spec) if got else None

    def fit(self, mesh, spec: FieldSpec, fs: Freestream):
        """Межі, підігнані під ОДИН зріз (той, що зараз на екрані), — ті самі
        правила й та сама рамка, що й для загальної шкали."""
        got = self._gather([self._prepare(mesh)], spec, fs)
        return self._reduce(*got, spec) if got else None

    def inplane_ref(self, fs: Freestream) -> float:
        """Швидкість, якій відповідає стрілка завдовжки в клітинку.

        Береться раз на вісь з тих самих зрізів, що й шкала, і далі не
        міняється, — з тієї ж причини, що й шкала: коли площину рухають,
        довжина стрілки має мінятися через потік, а не через те, що її
        перемасштабували. Для площини вздовж потоку це близько U∞, а для
        площини впоперек — швидкість закрутки й скосу, у рази менша: за U∞
        там стрілки були б точками.
        """
        u, v = plane_axes(self.axis)
        vals, wts = [], []
        for s, area, keep in self.slices:
            vel = s.cell_data.get("U")
            if vel is None:
                continue
            vel = np.asarray(vel, dtype=float)
            vals.append(np.hypot(vel[keep, u], vel[keep, v]))
            wts.append(area[keep])
        if vals:
            v_all, w_all = np.concatenate(vals), np.concatenate(wts)
            if v_all.size and w_all.sum() > 0:
                ref = _wpercentile(v_all, w_all, 95.0)
                if ref > 1e-3 * max(fs.u_inf, 1e-9):
                    return float(ref)
        return float(max(fs.u_inf, 1e-9))


def auto_limits(volume, spec: FieldSpec, fs: Freestream, bodies: dict | None = None,
                axis: int = 1, sampler: "Sampler | None" = None):
    """Межі шкали на цьому обʼємі. None — такого поля в ньому немає."""
    if spec.key == "cell":
        add_cell_size(volume)
    return (sampler or Sampler(volume, bodies or {}, axis)).limits(spec, fs)


# ==========================================================================
# Переріз
# ==========================================================================

def axis_normal(axis: int) -> list[float]:
    n = [0.0, 0.0, 0.0]
    n[axis] = 1.0
    return n


def plane_axes(axis: int) -> tuple[int, int]:
    """(вісь праворуч, вісь угору), коли дивимося вздовж `axis`."""
    return {0: (1, 2), 1: (0, 2), 2: (0, 1)}[axis]


def cut(mesh, axis: int, pos: float):
    """Переріз площиною, перпендикулярною до осі, на відстані `pos`."""
    origin = list(mesh.center)
    origin[axis] = pos
    return mesh.slice(normal=axis_normal(axis), origin=origin)


def view_bounds(bodies: dict, volume=None, pad: float = 0.15) -> tuple:
    """Коробка навколо тіла з полями — що вписувати в камеру. Домен для цього
    не годиться: він у десятки разів більший, і тіло в ньому — цятка."""
    parts = [m.bounds for m in (bodies or {}).values()
             if m is not None and m.n_points]
    if parts:
        b = (min(p[0] for p in parts), max(p[1] for p in parts),
             min(p[2] for p in parts), max(p[3] for p in parts),
             min(p[4] for p in parts), max(p[5] for p in parts))
    else:
        b = tuple(volume.bounds)
    d = pad * max(b[1] - b[0], b[3] - b[2], b[5] - b[4])
    return (b[0] - d, b[1] + d, b[2] - d, b[3] + d, b[4] - d, b[5] + d)


def _append(polys: list):
    if not polys:
        return pv.PolyData()
    if len(polys) == 1:
        return polys[0]
    app = vtk.vtkAppendPolyData()
    for p in polys:
        app.AddInputData(p)
    app.Update()
    return pv.wrap(app.GetOutput())


class BodyCutter:
    """Різання поверхні тіла площиною багато разів поспіль.

    Звичайний `mesh.slice` на кожен крок проходить усі грані: у тіла на 330
    тисяч трикутників це близько 80 мс, і більше половини кроку площини
    йшло на контур. `vtkPlaneCutter` будує дерево один раз, а далі бере лише
    грані коло площини — близько 8 мс, і результат той самий (перевірено:
    ті самі точки).
    """

    def __init__(self, mesh):
        self._mesh = mesh                    # тримаємо, доки живе фільтр
        self._plane = vtk.vtkPlane()
        self._cutter = vtk.vtkPlaneCutter()
        self._cutter.SetInputData(mesh)
        self._cutter.SetPlane(self._plane)
        self._cutter.SetBuildTree(True)
        self._cutter.SetGeneratePolygons(False)
        self._cutter.SetInterpolateAttributes(False)
        self._cutter.SetMergePoints(True)

    def cut(self, axis: int, pos: float):
        origin = [0.0, 0.0, 0.0]
        origin[axis] = pos
        self._plane.SetNormal(*axis_normal(axis))
        self._plane.SetOrigin(*origin)
        self._cutter.Modified()
        self._cutter.Update()
        # Виходом фільтра він же й володіє й перезапише його на наступному
        # кроці — тому віддаємо копію, а не вікно в його память.
        return pv.wrap(self._cutter.GetOutput()).copy()


ARROWS_ACROSS = 40      # стрілок уздовж довшої сторони рамки
BACK = "fs_back"        # 1 — стрілка дивиться проти потоку


def slice_arrows(s, axis: int, pos: float, box: tuple, ref: float, *,
                 across: int = ARROWS_ACROSS, direction=None):
    """Стрілки швидкості В ПЛОЩИНІ зрізу — лініями, по одній на клітинку.

    Стрілка стоїть у справжньому центрі комірки, найближчому до центру
    клітинки, а не в інтерпольованій точці: значення — те, що порахував
    розвʼязувач. Клітинки рівні, хоч сітка й ні: біля стінки комірок тисячі,
    і стрілка на кожну злилася б у суцільну пляму, а вдалині не було б
    жодної.

    Довжина: клітинка × |U в площині| / `ref`, не більше ніж 1.8 клітинки, —
    інакше кілька стрілок у струмені гвинта перекрили б сусідів. Стрілки —
    лінії, а не обʼємні конуси: лінія лягає на площину без «товщини», і її
    не ховає зріз з ближнього боку.

    Стрілки, що дивляться ПРОТИ потоку (`direction`), позначаються BACK = 1 і
    малюються червоним: це зворотна течія, тобто відрив. Вона повільна —
    на `gui_wing` до 7 м/с проти 34 м/с масштабу, — і за самою довжиною
    такі стрілки виходили рисками, яких ніхто не помітить. Тому вони не
    коротші за третину клітинки: колір тут каже більше, ніж довжина.
    """
    u, v = plane_axes(axis)
    u0, u1, v0, v1 = box
    h = max(u1 - u0, v1 - v0) / max(int(across), 1)
    empty = pv.PolyData()
    if s is None or not s.n_points or h <= 0:
        return empty
    if "U" in s.cell_data:
        at = np.asarray(s.cell_centers().points)
        vel = np.asarray(s.cell_data["U"], dtype=float)
    elif "U" in s.point_data:
        at = np.asarray(s.points)
        vel = np.asarray(s.point_data["U"], dtype=float)
    else:
        return empty
    keep = (at[:, u] > u0) & (at[:, u] < u1) & (at[:, v] > v0) & (at[:, v] < v1)
    if not keep.any():
        return empty
    at, vel = at[keep], vel[keep]
    iu = np.floor((at[:, u] - u0) / h).astype(np.int64)
    iv = np.floor((at[:, v] - v0) / h).astype(np.int64)
    off = (at[:, u] - (u0 + (iu + 0.5) * h)) ** 2 + \
        (at[:, v] - (v0 + (iv + 0.5) * h)) ** 2
    key = iu * (int((v1 - v0) / h) + 2) + iv
    order = np.lexsort((off, key))
    _, first = np.unique(key[order], return_index=True)
    pick = order[first]
    plane = np.column_stack([vel[pick, u], vel[pick, v]])
    mag = np.linalg.norm(plane, axis=1)
    length = h * np.minimum(mag / max(ref, 1e-12), 1.8)
    back = np.zeros(len(pick), dtype=bool)
    if direction is not None:
        dd = np.asarray(direction, dtype=float)
        flow2 = np.array([dd[u], dd[v]])
        # Площина впоперек потоку «назад» не має: там його не видно.
        if np.linalg.norm(flow2) > 0.2:
            back = (plane @ flow2) < -0.02 * max(ref, 1e-12)
            length = np.where(back, np.maximum(length, 0.33 * h), length)
    ok = length > 0.06 * h                 # коротше — це крапка, не стрілка
    if not ok.any():
        return empty
    plane, mag, length, back = plane[ok], mag[ok], length[ok], back[ok]
    centre = at[pick][ok]
    e = plane / mag[:, None]
    tail2 = np.column_stack([centre[:, u], centre[:, v]]) - 0.5 * length[:, None] * e
    tip2 = tail2 + length[:, None] * e
    head = np.minimum(0.38 * length, 0.45 * h)[:, None]
    c, sn = math.cos(math.radians(28)), math.sin(math.radians(28))
    rear = -e
    left = np.column_stack([c * rear[:, 0] - sn * rear[:, 1],
                            sn * rear[:, 0] + c * rear[:, 1]])
    right = np.column_stack([c * rear[:, 0] + sn * rear[:, 1],
                             -sn * rear[:, 0] + c * rear[:, 1]])
    n = len(tail2)
    pts2 = np.vstack([tail2, tip2, tip2 + head * left, tip2 + head * right])
    pts = np.zeros((4 * n, 3))
    pts[:, u], pts[:, v], pts[:, axis] = pts2[:, 0], pts2[:, 1], pos
    i = np.arange(n)
    lines = np.column_stack([np.full(n, 2), i, n + i,
                             np.full(n, 2), n + i, 2 * n + i,
                             np.full(n, 2), n + i, 3 * n + i]).ravel()
    out = pv.PolyData(pts, lines=lines)
    # Три лінії на стрілку, підряд: древко й два вуса.
    out.cell_data[BACK] = np.repeat(back.astype(np.float32), 3)
    return out


def _arrow_lut():
    from matplotlib.colors import ListedColormap
    return ListedColormap([INK, "#ff3b30"])


def _unique_title(plotter, label: str) -> str:
    """PyVista тримає шкали за заголовком і на повтор мовчки повертає None.
    Шкала тіла поруч зі шкалою зрізу легко мають однаковий підпис («Cp»), і
    тоді друга не створюється, а наступний `_style_bar` падає. Пробіл у
    кінці робить ключ іншим, а на вигляд нічого не міняє."""
    title = label
    while title in plotter.scalar_bars.keys():
        title += " "            # TrStr + " " keeps the key (fs_i18n)
    return title


def _bar_fmt(lo: float, hi: float) -> str:
    """Формат позначок шкали. «%.3g» друкує 1500 як «1.5e+03», а у вікні
    Па легко сягають тисяч — тоді шкала у вікні не схожа на шкалу кадру."""
    return "%.0f" if max(abs(lo), abs(hi)) >= 1000 else "%.3g"


def _style_bar(bar) -> None:
    if bar is None:
        return
    for prop in (bar.GetTitleTextProperty(), bar.GetLabelTextProperty()):
        prop.SetFontFamily(4)              # VTK_FONT_FILE
        prop.SetFontFile(FONT)


style_bar = _style_bar          # для шкал, які вікно ставить само


class SliceScene:
    """Зріз обʼєму площиною + контур тіла + шкала, у будь-якому Plotter.

    Рухається площина, а не сцена: при кожному кроці міняються лише дані в
    двох уже створених акторах. Нічого не створюється й не видаляється —
    тож камера, шкала й решта сцени стоять на місці, а крок коштує самого
    перерізу.
    """

    def __init__(self, plotter, volume, bodies: dict, fs: Freestream, *,
                 axis: int = 1, field: str = "dU", pos: float | None = None,
                 limits: tuple | None = None, edges: bool = False,
                 contour: bool = True, bar: bool = True, caption: bool = True,
                 line_width: float = 2.0, arrows: bool = False):
        self.plotter = plotter
        self.volume = volume
        self.bodies = {n: m for n, m in (bodies or {}).items()
                       if m is not None and m.n_points}
        self._cutters = {n: BodyCutter(m) for n, m in self.bodies.items()}
        self.fs = fs
        self.axis = int(axis)
        self.edges = bool(edges)
        self.contour = bool(contour)
        self.arrows = bool(arrows)
        self.show_bar = bool(bar)
        self.show_caption = bool(caption)
        self.line_width = float(line_width)
        self.cut_names: list[str] = []
        self.spec = spec_by_key(field)
        self._require()
        self.limits: tuple = limits or (-1.0, 1.0)
        self._user_limits = limits is not None

        self._actor = None
        self._lines = None
        self._arrow_actor = None
        # Дані стрілок тримаємо самі: мапер PyVista тримає свій набір лише
        # слабким посиланням, і тимчасовий обʼєкт зникав одразу після
        # присвоєння — актор лишався з порожнім входом (так і було, доки
        # не тримали).
        self._arrow_data = pv.PolyData()
        self._arrow_refs: dict[int, float] = {}
        self._text = None
        self._bar_title = ""
        self._slice = None
        self._outline = None
        self._widget = None
        self._rep = None
        self._on_move = None
        self._where = "point"
        self._cost = 0.0
        self._last = 0.0
        self._samplers: dict[int, Sampler] = {}

        self.pos = self.clamp(self.centre() if pos is None else pos)
        self._build()

    # --- геометрія ------------------------------------------------------

    @property
    def range(self) -> tuple[float, float]:
        """Уздовж якої відстані площина взагалі може стояти: весь домен."""
        b = self.volume.bounds
        return float(b[2 * self.axis]), float(b[2 * self.axis + 1])

    def clamp(self, pos: float) -> float:
        """Рівно на нижню межу площину не ставимо: там переріз порожній —
        усі комірки лежать по один бік від неї, і різати нічого. Відступ
        мізерний, але не нульовий."""
        lo, hi = self.range
        eps = 1e-6 * max(hi - lo, 1e-9)
        return float(min(max(pos, lo + eps), hi - eps))

    def body_range(self) -> tuple[float, float] | None:
        """Де по цій осі лежить тіло — для чутливого повзунка."""
        if not self.bodies:
            return None
        lo = min(m.bounds[2 * self.axis] for m in self.bodies.values())
        hi = max(m.bounds[2 * self.axis + 1] for m in self.bodies.values())
        return float(lo), float(hi)

    def body_bounds(self) -> tuple | None:
        if not self.bodies:
            return None
        bs = [m.bounds for m in self.bodies.values()]
        return (min(b[0] for b in bs), max(b[1] for b in bs),
                min(b[2] for b in bs), max(b[3] for b in bs),
                min(b[4] for b in bs), max(b[5] for b in bs))

    def centre(self) -> float:
        r = self.body_range() or self.range
        return 0.5 * (r[0] + r[1])

    def fine_range(self) -> tuple[float, float]:
        """Де ходить повзунок: габарит тіла з запасом. Весь домен на повзунок
        не влазить: тіло займе в ньому кілька відсотків, і на один піксель
        повзунка припаде більше, ніж товщина крила."""
        lo, hi = self.range
        r = self.body_range()
        if r is None:
            return lo, hi
        b = self.body_bounds()
        big = max(b[1] - b[0], b[3] - b[2], b[5] - b[4])
        margin = max(0.5 * (r[1] - r[0]), 0.3 * big)
        return max(lo, r[0] - margin), min(hi, r[1] + margin)

    def view_bounds(self) -> tuple:
        """Що вписати в кадр: тіло з полями, а не весь домен (зріз іде на
        весь домен, і камера, вписана в нього, показала б тіло цяткою)."""
        return view_bounds(self.bodies, self.volume)

    # --- побудова -------------------------------------------------------

    def _sampler(self) -> Sampler:
        """Зрізи для шкали — по одному набору на вісь, надалі без різання."""
        if self.spec.key == "cell":
            # Розмір комірки береться з обʼєму, тож зрізи мусять бути зняті
            # вже ПІСЛЯ того, як він туди потрапив.
            if CELL_LG not in self.volume.cell_data:
                add_cell_size(self.volume)
                self._samplers.clear()
        if self.axis not in self._samplers:
            self._samplers[self.axis] = Sampler(self.volume, self.bodies,
                                                self.axis)
        return self._samplers[self.axis]

    def _limits_for(self) -> tuple:
        if self._user_limits:
            return self.limits
        found = auto_limits(self.volume, self.spec, self.fs,
                            sampler=self._sampler())
        return found or (-1.0, 1.0)

    def _where_for(self) -> str:
        """Де лежить поле: у точках чи в комірках — за ОБʼЄМОМ, а не за
        першим зрізом. Порожній перший переріз (площина в дірі) не каже
        нічого, і вибір «за замовчуванням» міг би не збігтися з даними."""
        if self.spec.key == "cell":
            return "cell"
        have = self.volume.point_data
        return "point" if all(a in have for a in self.spec.arrays) else "cell"

    def _require(self) -> None:
        """Поля, чиїх масивів у обʼємі нема, не малюємо мовчки нулями: це
        порожній кадр із правдоподібним заголовком і шкалою."""
        if self.spec.key == "cell":
            return
        have = set(self.volume.point_data.keys()) | set(
            self.volume.cell_data.keys())
        lost = [a for a in self.spec.arrays if a not in have]
        if lost:
            raise ValueError(
                tr("The volume has no {lost} — nothing to show the "
                   "“{key}” field with.", lost=', '.join(lost), key=self.spec.key))

    def _decorate(self, s):
        """Кладе в переріз скаляр для розфарбування під іменем VALUE."""
        where, v = values(s, self.spec, self.fs, self._where)
        if v is None:
            v = np.zeros(s.n_points)
            where = "point"
        (s.point_data if where == "point" else s.cell_data)[VALUE] = v
        return s

    def _cut_slice(self):
        return self._decorate(cut(self.volume, self.axis, self.pos))

    def _cut_outline(self):
        parts, names = [], []
        for name, cutter in self._cutters.items():
            c = cutter.cut(self.axis, self.pos)
            if c.n_points:
                c.clear_data()
                parts.append(c)
                names.append(name)
        self.cut_names = names
        return _append(parts)

    def _build(self):
        p = self.plotter
        self.limits = self._limits_for()
        if self.spec.key == "cell":
            add_cell_size(self.volume)

        # Де брати значення — вирішується раз, на першому зрізі: мапер
        # прив'язаний до точкових або до комірчастих даних, і зміна посеред
        # руху площини мовчки зламала б колір.
        self._where = self._where_for()
        self._slice = self._cut_slice()

        # Порожній переріз (площина в дірі між частинами обʼєму) актором не
        # додати: PyVista відмовляється малювати порожню сітку. Тоді ставимо
        # крихітну заглушку з тим самим масивом, а справжній, порожній, зріз
        # підставляємо в мапер одразу — далі площину можна рухати як завжди.
        seed = self._slice
        if not seed.n_points:
            seed = pv.PolyData(np.zeros((1, 3)))
            seed.point_data[VALUE] = np.zeros(1)
        cmap = get_cmap(self.spec.cmap)
        self._actor = p.add_mesh(
            seed, scalars=VALUE, cmap=cmap, clim=self.limits,
            preference=self._where, lighting=False, show_edges=self.edges,
            edge_color=EDGE, name="fs_slice", show_scalar_bar=False,
            reset_camera=False, render=False)
        if seed is not self._slice:
            self._actor.mapper.dataset = self._slice
        # Режим мапера ставимо ЯВНО: заглушка має лише точкові дані, і без
        # цього поле з комірок лишилося б розфарбованим «по точках».
        m = self._actor.mapper
        (m.SetScalarModeToUseCellData if self._where == "cell"
         else m.SetScalarModeToUsePointData)()
        m.SelectColorArray(VALUE)

        # Контур: тонкі світлі лінії. Створюється завжди, навіть порожній
        # (площина може не зачіпати тіла) — тоді просто нічого не малює, а
        # не зникає актор, який потім довелося б створювати наново.
        self._outline = self._cut_outline()
        seed = pv.Line((0, 0, 0), (1e-9, 0, 0))
        self._lines = p.add_mesh(
            seed, color=INK, line_width=self.line_width, lighting=False,
            name="fs_contour", show_scalar_bar=False, reset_camera=False,
            render=False)
        self._lines.mapper.dataset = self._outline
        self._lines.SetVisibility(self.contour)

        # Стрілки — так само один актор на всю сцену, якому міняють дані.
        # Рахуються лише ввімкнені: на кожен крок площини це ще один прохід
        # по зрізу, і платити за нього, коли стрілок не видно, нема чого.
        seed = pv.Line((0, 0, 0), (1e-9, 0, 0))
        seed.cell_data[BACK] = np.zeros(seed.n_cells, dtype=np.float32)
        self._arrow_actor = p.add_mesh(
            seed, scalars=BACK, cmap=_arrow_lut(), clim=(0.0, 1.0), n_colors=2,
            preference="cell", line_width=max(1.0, 0.7 * self.line_width),
            lighting=False, name="fs_arrows", show_scalar_bar=False,
            reset_camera=False, render=False)
        self._arrow_data = self._cut_arrows()
        self._arrow_actor.mapper.dataset = self._arrow_data
        self._arrow_actor.SetVisibility(self.arrows)

        if self.show_bar:
            self._bar_title = _unique_title(p, tr(self.spec.label))
            bar = p.add_scalar_bar(
                title=self._bar_title, mapper=self._actor.mapper,
                n_labels=len(ticks_for(*self.limits,
                                       self.spec.kind == "diverging")),
                fmt=_bar_fmt(*self.limits), vertical=False, position_x=0.27,
                position_y=0.035,
                width=0.46, height=0.05, color=INK, title_font_size=15,
                label_font_size=13, render=False)
            _style_bar(bar)
        if self.show_caption:
            self._text = p.add_text(
                self.caption(), position="upper_left", font_size=11,
                color=INK, font_file=FONT, name="fs_caption", shadow=False,
                render=False)

    def caption(self) -> str:
        cut_txt = ", ".join(self.cut_names) if self.cut_names else tr("does not cut the body")
        text = (tr("slice {AXES} = {v:.1f} mm   ·   contour: {cut_txt}", AXES=AXES[self.axis], v=self.pos * 1000, cut_txt=cut_txt))
        if self.arrows:
            text += tr("   ·   arrow per grid square = {arrow_ref:.3g} m/s", arrow_ref=self.arrow_ref())
        return text

    # --- стрілки --------------------------------------------------------

    def arrow_box(self) -> tuple:
        """Де ставити стрілки: та сама рамка навколо тіла, з якої береться
        шкала. Увесь домен засіяти не можна: на тіло припало б кілька стрілок."""
        return self._sampler().box

    def arrow_ref(self) -> float:
        if self.axis not in self._arrow_refs:
            self._arrow_refs[self.axis] = self._sampler().inplane_ref(self.fs)
        return self._arrow_refs[self.axis]

    def _cut_arrows(self):
        if not self.arrows:
            return pv.PolyData()
        return slice_arrows(self._slice, self.axis, self.pos, self.arrow_box(),
                            self.arrow_ref(), direction=self.fs.direction)

    def set_arrows(self, on: bool) -> None:
        self.arrows = bool(on)
        self._arrow_data = self._cut_arrows()
        self._arrow_actor.mapper.dataset = self._arrow_data
        self._arrow_actor.SetVisibility(self.arrows)
        if self._text is not None:
            self._text.SetText(2, self.caption())
        self.plotter.render()

    def missed_names(self) -> list[str]:
        return [n for n in self.bodies if n not in self.cut_names]

    # --- керування ------------------------------------------------------

    def set_position(self, pos: float, *, render: bool = True,
                     sync: bool = True) -> float:
        """Пересуває площину. Повертає позицію, яка справді стала (межі!)."""
        t0 = time.perf_counter()
        self.pos = self.clamp(pos)
        self._slice = self._cut_slice()
        self._actor.mapper.dataset = self._slice
        self._outline = self._cut_outline()
        self._lines.mapper.dataset = self._outline
        if self.arrows:
            self._arrow_data = self._cut_arrows()
            self._arrow_actor.mapper.dataset = self._arrow_data
        if self._text is not None:
            self._text.SetText(2, self.caption())      # 2 — верхній лівий кут
        if sync and self._rep is not None:
            o = list(self._rep.GetOrigin())
            o[self.axis] = self.pos
            self._rep.SetOrigin(o)
        if render:
            self.plotter.render()
        self._cost = time.perf_counter() - t0
        self._last = time.perf_counter()
        return self.pos

    def set_field(self, key: str, *, limits: tuple | None = None) -> None:
        keep, self.spec = self.spec, spec_by_key(key)
        try:
            self._require()
        except ValueError:
            self.spec = keep            # відмова не має лишати сцену наполовину
            raise
        self._teardown(keep_widget=True)
        self._user_limits = limits is not None
        self.limits = limits or (-1.0, 1.0)
        self._build()
        self.plotter.render()

    def set_limits(self, lo: float, hi: float) -> None:
        self._teardown(keep_widget=True)
        self._user_limits = True
        self.limits = (float(lo), float(hi))
        self._build()
        self.plotter.render()

    def set_axis(self, axis: int, pos: float | None = None) -> None:
        """Інша вісь — інша площина: перебудовується й віджет, бо в нього
        нормаль зафіксована на осі."""
        was = self._widget is not None
        on_move = self._on_move
        self._teardown()
        self.axis = int(axis)
        self.pos = self.clamp(self.centre() if pos is None else pos)
        self._build()
        if was:
            self.enable_widget(on_move)
        self.plotter.render()

    def set_edges(self, on: bool) -> None:
        self.edges = bool(on)
        self._actor.GetProperty().SetEdgeVisibility(self.edges)
        self.plotter.render()

    def fit_limits(self):
        """Межі, підігнані під поточний зріз. None — нема чого мірити."""
        return self._sampler().fit(self._slice, self.spec, self.fs)

    def set_contour(self, on: bool) -> None:
        self.contour = bool(on)
        self._lines.SetVisibility(self.contour)
        self.plotter.render()

    # --- віджет у вʼюпорті ----------------------------------------------

    def _widget_bounds(self) -> tuple:
        """Коробка віджета — і вона ж його ручка.

        Площина віджета — це переріз коробки, а тягнути можна за будь-яку її
        точку. Якби коробка обіймала все тіло, півекрана було б «ручкою», і
        спроба покрутити камеру мишею над моделлю замість цього штовхала б
        площину. Тому це МАЛЕНЬКИЙ квадрат у куті навколо тіла, а решта вікна
        крутить камеру, як і скрізь.

        Уздовж осі коробка — увесь домен: площину треба вміти відвести від
        тіла далі, ніж на його габарит.
        """
        lo, hi = self.range
        b = self.body_bounds() or tuple(self.volume.bounds)
        u, v = plane_axes(self.axis)
        ext = max(b[2 * u + 1] - b[2 * u], b[2 * v + 1] - b[2 * v], 1e-9)
        grip = 0.25 * ext
        out = [0.0] * 6
        out[2 * self.axis], out[2 * self.axis + 1] = lo, hi
        for a in (u, v):
            far = b[2 * a + 1] + 0.35 * ext
            out[2 * a], out[2 * a + 1] = far - grip, far
        return tuple(out)

    def enable_widget(self, on_move=None) -> bool:
        """Площина, яку можна тягнути мишею по вʼюпорту.

        Тягнеться лише вздовж нормалі й не обертається: нормаль прибита до
        осі. Обмеження вроджене — коли дивитися вздовж нормалі, рух миші
        не має складової вздовж неї, і площина не рушить; для цього є
        повзунок у смузі керування.
        """
        self._drop_widget()
        iren = getattr(self.plotter, "iren", None)
        interactor = getattr(iren, "interactor", None)
        if interactor is None:
            # foamview (fs_view3d.Plotter): своя ручка з тими ж методами
            # (GetOrigin, SetOrigin, AddObserver), що й у VTK.
            make = getattr(self.plotter, "add_drag_widget", None)
            if make is None:
                return False
            self._on_move = on_move
            origin = list(self.volume.center)
            origin[self.axis] = self.pos
            wid = make("plane", axis=self.axis, bounds=self._widget_bounds(),
                       origin=origin, color=pv.Color(INK).float_rgb)
            wid.AddObserver("InteractionEvent", lambda *_: self._dragged(False))
            wid.AddObserver("EndInteractionEvent", lambda *_: self._dragged(True))
            wid.On()
            self._widget, self._rep = wid, wid
            return True
        self._on_move = on_move

        rep = vtk.vtkImplicitPlaneRepresentation()
        rep.SetPlaceFactor(1.0)
        rep.PlaceWidget(self._widget_bounds())
        origin = list(self.volume.center)
        origin[self.axis] = self.pos
        rep.SetOrigin(origin)
        rep.SetNormal(axis_normal(self.axis))
        (rep.NormalToXAxisOn, rep.NormalToYAxisOn,
         rep.NormalToZAxisOn)[self.axis]()
        rep.SetOutlineTranslation(False)
        rep.SetScaleEnabled(False)
        rep.SetConstrainToWidgetBounds(True)
        rep.SetDrawOutline(False)
        rep.SetDrawPlane(True)
        # Кольори віджета задаються його ВЛАСНИМИ методами. Властивість
        # країв (`GetEdgesProperty().SetColor`) на них не діє: краї
        # фарбуються через таблицю кольорів, і без цього вони лишаються
        # червоними, хоч би що виставили у властивості.
        ink = pv.Color(INK).float_rgb
        rep.SetEdgeColor(*ink)
        rep.SetInteractionColor(*ink)
        rep.GetPlaneProperty().SetColor(*ink)
        rep.GetPlaneProperty().SetOpacity(0.08)
        rep.GetSelectedPlaneProperty().SetColor(*ink)
        rep.GetSelectedPlaneProperty().SetOpacity(0.16)
        # Стрілка нормалі тут не потрібна: обертати нема чого, а вона
        # лише плутає, нібито за неї можна потягти.
        for prop in (rep.GetNormalProperty(), rep.GetSelectedNormalProperty()):
            prop.SetOpacity(0.0)

        wid = vtk.vtkImplicitPlaneWidget2()
        wid.SetInteractor(interactor)
        wid.SetRepresentation(rep)
        wid.SetCurrentRenderer(self.plotter.renderer)
        wid.AddObserver("InteractionEvent", lambda *_: self._dragged(False))
        wid.AddObserver("EndInteractionEvent", lambda *_: self._dragged(True))
        wid.On()
        self._widget, self._rep = wid, rep
        return True

    def _dragged(self, final: bool) -> None:
        """Площину потягли. Проміжні кроки пропускаємо, якщо попередній ще не
        вляглася: черга з кількох сотень зрізів робила б із перетягування
        слайд-шоу. Останній крок (відпущена кнопка) не пропускається ніколи."""
        if self._rep is None:
            return
        if not final and (time.perf_counter() - self._last) < self._cost:
            return
        pos = float(self._rep.GetOrigin()[self.axis])
        self.set_position(pos, sync=False)
        if self._on_move is not None:
            self._on_move(self.pos)

    def has_widget(self) -> bool:
        return self._widget is not None

    def disable_widget(self) -> None:
        self._drop_widget()

    def _drop_widget(self) -> None:
        if self._widget is not None:
            try:
                self._widget.Off()
                self._widget.RemoveAllObservers()
                self._widget.SetInteractor(None)
            except Exception:
                pass
        self._widget = self._rep = None

    # --- прибирання -----------------------------------------------------

    def _teardown(self, keep_widget: bool = False) -> None:
        p = self.plotter
        if not keep_widget:
            self._drop_widget()
        for actor in (self._actor, self._lines, self._arrow_actor, self._text):
            if actor is not None:
                try:
                    p.remove_actor(actor, render=False)
                except Exception:
                    pass
        self._actor = self._lines = self._arrow_actor = self._text = None
        if self._bar_title:
            try:
                p.remove_scalar_bar(self._bar_title, render=False)
            except Exception:
                pass
            self._bar_title = ""

    def close(self) -> None:
        self._teardown()


# ==========================================================================
# Кадри для звіту
# ==========================================================================
#
# Кадр — це та сама сцена, зіграна позаекранно, дивлячись рівно на площину, у
# паралельній проєкції. Паралельній — щоб масштаб був однаковим по всьому
# кадру й можна було намалювати лінійку в міліметрах: перспектива робить
# ближче вдвічі більшим, і «у кадрі так далеко» перестає щось означати.
#
# Підписи, шкалу й заголовок малює не VTK, а matplotlib: він має кириличні
# шрифти, справжню шкалу з підписами, і головне — ОДНУ шкалу на кілька кадрів.
# Порівняння двох кейсів, у кожного з яких своя шкала, нічого не порівнює.

@dataclass
class Frame:
    image: np.ndarray                 # (H, W, 3) uint8
    label: str = ""                   # підпис у кутку кадру
    metres_per_px: float = 0.0        # 0 — лінійку не малювати
    cut: tuple = ()                   # які тіла площина перетинає
    missed: tuple = ()                # які на цій відстані вже не зачіпає
    flow_dir: tuple = (0.0, 0.0)      # напрямок потоку в площині кадру


def face_on(plotter, axis: int) -> None:
    """Дивитися рівно на площину, у паралельній проєкції."""
    (plotter.view_yz, plotter.view_xz, plotter.view_xy)[axis]()
    plotter.camera.parallel_projection = True


def default_rect(scene: SliceScene) -> tuple:
    """Що показати за замовчуванням: тіло з широкими полями, а не весь домен.

    Домен у десять разів більший за літак; на кадрі з усім доменом літак —
    крихітна цятка, і в ній нічого не видно. Поля навколо тіла лишають місце
    для сліду й обдуву, які якраз і цікавлять.
    """
    u, v = plane_axes(scene.axis)
    b = scene.body_bounds() or tuple(scene.volume.bounds)
    du, dv = b[2 * u + 1] - b[2 * u], b[2 * v + 1] - b[2 * v]
    pad = 0.3 * max(du, dv, 1e-9)
    return (b[2 * u] - pad, b[2 * u + 1] + pad,
            b[2 * v] - pad, b[2 * v + 1] + pad)


def fit_camera(plotter, axis: int, depth: float, rect: tuple,
               size: tuple[int, int], reach: float) -> float:
    """Ставить паралельну камеру, що дивиться вздовж `axis`, так, щоб `rect`
    (u0, u1, v0, v1) влізло в кадр `size`. `depth` — де по осі фокус, `reach`
    — на якій відстані стоїть камера. Повертає метрів на піксель."""
    u, v = plane_axes(axis)
    u0, u1, v0, v1 = rect
    w, h = size
    focal = [0.0, 0.0, 0.0]
    focal[u], focal[v], focal[axis] = 0.5 * (u0 + u1), 0.5 * (v0 + v1), depth
    cam = plotter.camera
    look = np.asarray(cam.position) - np.asarray(cam.focal_point)
    look = look / (np.linalg.norm(look) or 1.0)
    cam.focal_point = focal
    cam.position = list(np.asarray(focal) + look * reach)
    scale = max(0.5 * (v1 - v0), 0.5 * (u1 - u0) * h / w)
    cam.parallel_scale = scale
    plotter.reset_camera_clipping_range()
    return 2.0 * scale / h


def render_frame(volume, bodies: dict, fs: Freestream, *, field: str,
                 axis: int = 1, pos: float, limits: tuple | None = None,
                 size: tuple[int, int] = (1600, 640), focus: tuple | None = None,
                 edges: bool = False, contour: bool = True, bg: str = "#000000",
                 label: str = "", ss: int = 2, arrows: bool = False) -> Frame:
    """Один кадр зрізу. `ss` — надвибірка: рендер у `ss`-разів більшому
    розмірі й усереднення назад дає гладкі лінії без залежності від того,
    чи є в цій відеокарті вбудоване згладжування."""
    w, h = size
    pl = pv.Plotter(off_screen=True, window_size=[w * ss, h * ss])
    try:
        pl.set_background(bg)
        scene = SliceScene(pl, volume, bodies, fs, axis=axis, field=field,
                           pos=pos, limits=limits, edges=edges,
                           contour=contour, bar=False, caption=False,
                           line_width=2.0 * ss, arrows=arrows)
        face_on(pl, axis)
        reach = 10.0 * max(np.ptp(np.asarray(volume.bounds).reshape(3, 2),
                                  axis=1).max(), 1e-6)
        mpp = fit_camera(pl, axis, scene.pos, focus or default_rect(scene),
                         (w, h), reach)
        shot = pl.screenshot(return_img=True)
    finally:
        pl.close()
    img = shot[:h * ss, :w * ss, :3].astype(np.float64)
    img = img.reshape(h, ss, w, ss, 3).mean(axis=(1, 3)).round().astype(np.uint8)
    u, v = plane_axes(axis)
    d = np.asarray(fs.direction, dtype=float)
    return Frame(img, label, mpp, tuple(scene.cut_names),
                 tuple(scene.missed_names()), (float(d[u]), float(d[v])))


def _nice_length(x: float) -> float:
    """Найближче «кругле» число (1, 2, 5 · 10^k) не більше за x."""
    if x <= 0:
        return 0.0
    e = math.floor(math.log10(x))
    for m in (5, 2, 1):
        if m * 10.0 ** e <= x:
            return m * 10.0 ** e
    return 10.0 ** e


def _length_text(metres: float) -> str:
    return tr("{v:g} mm", v=metres * 1000) if metres < 1.0 else tr("{metres:g} m", metres=metres)


def compose(grid: list[list[Frame]], *, title: str, subtitle: str = "",
            bar: tuple | None = None, col_labels: tuple = (), path: str | None = None,
            bg: str = "#000000", dpi: int = 100, legend: tuple = ()) -> np.ndarray:
    """Кадри в сітці рядків × стовпців + заголовок + спільна шкала знизу.

    `bar` — (поле, нижня межа, верхня межа). Одна шкала на всі кадри — це
    умова, щоб їх можна було порівнювати очима.

    `legend` — ((колір, підпис), …): кольорові позначки під кадрами для
    того, що шкалою не передати (зворотна течія, частини, яких торкнувся
    обдув).
    """
    from matplotlib.backends.backend_agg import FigureCanvasAgg
    from matplotlib.cm import ScalarMappable
    from matplotlib.figure import Figure
    from matplotlib.lines import Line2D
    from matplotlib.patches import FancyArrowPatch

    rows, cols = len(grid), max(len(r) for r in grid)
    fh, fw = grid[0][0].image.shape[:2]
    margin, gap = 40, 14
    n_sub = len(subtitle.splitlines()) if subtitle else 0
    head = margin + 34 + 24 * n_sub + (30 if col_labels else 0) + 10
    foot = (140 if bar else margin) + 26 * len(legend)
    width = 2 * margin + cols * fw + (cols - 1) * gap
    height = head + rows * fh + (rows - 1) * gap + foot

    fig = Figure(figsize=(width / dpi, height / dpi), dpi=dpi, facecolor=bg)
    FigureCanvasAgg(fig)

    def fx(px: float) -> float:
        return px / width

    def fy(px: float) -> float:              # px — від верхнього краю
        return 1.0 - px / height

    fig.text(fx(margin), fy(margin), title, color=INK, fontsize=15.5,
             fontweight="bold", va="top", ha="left")
    for i, line in enumerate(subtitle.splitlines()):
        fig.text(fx(margin), fy(margin + 34 + 24 * i), line, color=INK_DIM,
                 fontsize=10.5, va="top", ha="left")
    for c, text in enumerate(col_labels):
        fig.text(fx(margin + c * (fw + gap) + fw / 2),
                 fy(head - 30 - 4), text, color=INK_DIM, fontsize=11,
                 va="top", ha="center")

    for r, row in enumerate(grid):
        for c, frame in enumerate(row):
            x = margin + c * (fw + gap)
            y = head + r * (fh + gap)
            fig.figimage(frame.image, xo=x, yo=height - y - fh, origin="upper")
            if frame.label:
                fig.text(fx(x + 14), fy(y + 12), frame.label, color=INK,
                         fontsize=11.5, va="top", ha="left",
                         bbox=dict(facecolor=bg, alpha=0.7, edgecolor="none",
                                   pad=4))
            mpp = frame.metres_per_px
            if mpp > 0:
                length = _nice_length(0.16 * fw * mpp)
                px = length / mpp
                x0, y0 = x + 16, y + fh - 18
                fig.add_artist(Line2D([fx(x0), fx(x0 + px)], [fy(y0), fy(y0)],
                                      color=INK, lw=1.6, transform=fig.transFigure))
                for xe in (x0, x0 + px):
                    fig.add_artist(Line2D([fx(xe), fx(xe)],
                                          [fy(y0 - 5), fy(y0 + 5)], color=INK,
                                          lw=1.6, transform=fig.transFigure))
                fig.text(fx(x0), fy(y0 - 9), _length_text(length), color=INK,
                         fontsize=10, va="bottom", ha="left")
            du, dv = frame.flow_dir
            if math.hypot(du, dv) > 0.2:
                # Куди дме потік у площині кадру: без цього незрозуміло, де
                # перед, а де зад.
                n = math.hypot(du, dv)
                ax0, ay0 = x + fw - 90 - 30 * du / n, y + fh - 30 + 0.0
                ax1, ay1 = ax0 + 60 * du / n, ay0 - 60 * dv / n
                fig.add_artist(FancyArrowPatch(
                    (fx(ax0), fy(ay0)), (fx(ax1), fy(ay1)),
                    transform=fig.transFigure, arrowstyle="-|>",
                    mutation_scale=14, color=INK_DIM, lw=1.4))
                fig.text(fx(x + fw - 16), fy(y + fh - 12), "flow",
                         color=INK_DIM, fontsize=10, va="bottom", ha="right")

    if bar is not None:
        spec, lo, hi = bar
        bw = min(0.5 * width, 640)
        bx, by = 0.5 * (width - bw), height - foot + 30
        cax = fig.add_axes([fx(bx), fy(by + 20), bw / width, 20 / height])
        cb = fig.colorbar(ScalarMappable(norm=Normalize(lo, hi),
                                         cmap=get_cmap(spec.cmap)),
                          cax=cax, orientation="horizontal",
                          ticks=ticks_for(lo, hi, spec.kind == "diverging"))
        cb.outline.set_visible(False)
        cax.tick_params(colors=INK_DIM, labelsize=10, length=3, width=0.8)
        cb.set_label(spec.label, color=INK, fontsize=11.5, labelpad=8)

    if legend:
        from matplotlib.patches import Rectangle
        top = height - foot + (140 if bar else 0) - (12 if bar else 0)
        for i, (colour, text) in enumerate(legend):
            y = top + 26 * i
            fig.add_artist(Rectangle((fx(margin), fy(y + 14)), 16 / width,
                                     14 / height, transform=fig.transFigure,
                                     facecolor=colour, edgecolor="none"))
            fig.text(fx(margin + 26), fy(y + 7), text, color=INK, fontsize=10.5,
                     va="center", ha="left")

    canvas = fig.canvas
    canvas.draw()
    rgba = np.asarray(canvas.buffer_rgba()).copy()
    if path:
        fig.savefig(path, dpi=dpi, facecolor=bg)
    return rgba


@dataclass
class CaseView:
    """Один кейс у звіті: обʼєм, тіла, потік і підпис кадру."""
    volume: object
    bodies: dict
    fs: Freestream
    label: str = ""


def shared_limits(views: list[CaseView], spec: FieldSpec, axis: int = 1):
    """ОДНА шкала на всі кейси: обʼєднання їхніх власних. Кожен зі своєю
    шкалою — це не порівняння, а два гарні малюнки."""
    found = []
    for v in views:
        if spec.key == "cell":
            add_cell_size(v.volume)
        lim = auto_limits(v.volume, spec, v.fs, v.bodies, axis)
        if lim is not None:
            found.append(lim)
    if not found:
        return None
    lo, hi = min(f[0] for f in found), max(f[1] for f in found)
    if spec.kind == "diverging":
        hi = max(abs(lo), abs(hi))
        lo = -hi
    return lo, hi


def slice_report(views: list[CaseView], *, field: str, axis: int = 1,
                 pos: float, name: str = "", limits: tuple | None = None,
                 size: tuple[int, int] = (1600, 640), focus: tuple | None = None,
                 edges: bool = False, contour: bool = True,
                 path: str | None = None,
                 subtitle: str | None = None, arrows: bool = False) -> np.ndarray:
    """Кадр (або кадри одного під одним) зрізу зі спільною шкалою й підписами.

    Кілька кейсів — це порівняння: усі беруть ту саму шкалу, ту саму
    площину й той самий кадр, тож відрізняються лише самим потоком.
    """
    spec = spec_by_key(field)
    lim = limits or shared_limits(views, spec, axis) or (-1.0, 1.0)
    frames = [render_frame(v.volume, v.bodies, v.fs, field=field, axis=axis,
                           pos=pos, limits=lim, size=size, focus=focus,
                           edges=edges, contour=contour, label=v.label,
                           arrows=arrows)
              for v in views]
    fs = views[0].fs
    head = tr("{v}slice {AXES} = {v2:.0f} mm,   U∞ = {u_inf:g} m/s", v=f"{name} — " if name else "", AXES=AXES[axis], v2=pos * 1000, u_inf=fs.u_inf)
    if subtitle is None:
        cut = sorted({n for f in frames for n in f.cut})
        missed = sorted({n for f in frames for n in f.missed} - set(cut))
        lines = [spec.about] if spec.about else []
        if cut:
            note = tr("contour: {cut}", cut=", ".join(cut))
            if missed:
                note += tr(" — {missed} no longer intersects at this distance", missed=", ".join(missed))
            lines.append(note)
        if arrows:
            # Масштаб стрілок — з першого кейса: свій у кожного означав би, що
            # однакові стрілки в двох кадрах — різні швидкості.
            ref = Sampler(views[0].volume, views[0].bodies, axis).inplane_ref(fs)
            lines.append(tr("arrows — velocity in the slice plane; arrow "
                            "per grid square = {ref:.3g} m/s; red — the "
                            "air goes against the flow (separation)", ref=ref))
        subtitle = "\n".join(lines)
    return compose([[f] for f in frames], title=head, subtitle=subtitle,
                   bar=(spec, lim[0], lim[1]), path=path)


# ==========================================================================
# Лінії потоку
# ==========================================================================
#
# Гарні лінії потоку — це в основному ВДАЛЕ НАСІННЯ. Випадкова хмара точок дає
# кашу, у якій не видно ні обдуву гвинта, ні того, де потік огинає тіло.
# Тому насіння ставиться там, де воно щось означає:
#
#   * КІЛЬЦЯ ПЕРЕД ДИСКОМ кожного гвинта: розходяться від осі, тож видно,
#     як струмінь стискається за диском і закручується;
#   * ГРЕБІНКА ПЕРЕД НОСОМ: сітка упоперек потоку по габариту тіла — видно,
#     що обтікає, а що проходить повз.

SPEED = "fs_speed"


@dataclass(frozen=True)
class Rotor:
    """Гвинт для насіння: де він і якого розміру. З `RotatingZone` кейсу або
    з MRFProperties (старі кейси без маніфесту)."""
    name: str
    centre: tuple
    axis: tuple
    radius: float
    length: float = 0.0        # довжина обертової зони вздовж осі

    @classmethod
    def from_zone(cls, zone) -> "Rotor":
        return cls(zone.name, tuple(zone.centre), tuple(zone.axis),
                   float(zone.blade_radius), float(zone.length))


def _unit(v) -> np.ndarray:
    v = np.asarray(v, dtype=float)
    n = np.linalg.norm(v)
    return v / n if n > 0 else v


def _basis(axis) -> tuple:
    """Права трійка (вісь, e1, e2) з осі: e1, e2 лежать у площині диска."""
    a = _unit(axis)
    ref = np.array([0.0, 0.0, 1.0]) if abs(a[2]) < 0.9 else np.array([1.0, 0, 0])
    e1 = _unit(np.cross(a, ref))
    return a, e1, np.cross(a, e1)


def rotor_seeds(rotor: Rotor, fs: Freestream, *,
                rings: tuple = (0.3, 0.55, 0.8, 1.05),
                per_ring: tuple = (8, 14, 20, 26),
                standoff: float = 0.25) -> np.ndarray:
    """Кільця насінин перед диском гвинта — з боку, звідки набігає потік.

    Радіуси — частки радіуса лопаті, останнє трохи більше за 1: воно ловить
    кінцевий вихор. Кількість точок у кільці росте з радіусом, щоб густота
    вздовж кільця була приблизно однакова.
    """
    a, e1, e2 = _basis(rotor.axis)
    front = -a if a @ _unit(fs.direction) >= 0 else a
    origin = (np.asarray(rotor.centre, dtype=float)
              + front * (0.5 * rotor.length + standoff * rotor.radius))
    pts = []
    for r, n in zip(rings, per_ring):
        th = np.linspace(0.0, 2.0 * np.pi, n, endpoint=False)
        pts.append(origin + r * rotor.radius * (
            np.cos(th)[:, None] * e1 + np.sin(th)[:, None] * e2))
    return np.vstack(pts)


def nose_seeds(bounds: tuple, fs: Freestream, *, across: int = 25,
               levels: int = 3, standoff: float = 0.08) -> np.ndarray:
    """Гребінка перед носом: сітка упоперек потоку по габариту тіла.

    Площина насіння перпендикулярна до потоку й стоїть перед тілом на
    `standoff` його довжини. Габарит береться проєкцією коробки тіла на цю
    площину, тому працює й під кутом атаки.
    """
    d = _unit(fs.direction)
    lo, hi = np.asarray(bounds[0::2], float), np.asarray(bounds[1::2], float)
    corners = np.array([[x, y, z] for x in (lo[0], hi[0])
                        for y in (lo[1], hi[1]) for z in (lo[2], hi[2])])
    centre = 0.5 * (lo + hi)
    ref = np.array([0.0, 0.0, 1.0]) if abs(d[2]) < 0.9 \
        else np.array([1.0, 0.0, 0.0])
    e1 = _unit(np.cross(ref, d))          # завжди ⟂ потоку, і під кутом теж
    e2 = np.cross(d, e1)
    rel = corners - centre
    along = rel @ d
    u, v = rel @ e1, rel @ e2
    origin = centre + d * (along.min() - standoff * max(np.ptp(along), 1e-9))
    us = np.linspace(u.min() * 1.05, u.max() * 1.05, across)
    vs = np.linspace(0.5 * (v.min() + v.max()) - 0.35 * max(np.ptp(v), 1e-9),
                     0.5 * (v.min() + v.max()) + 0.35 * max(np.ptp(v), 1e-9),
                     levels) if levels > 1 else np.array([v.mean()])
    return np.array([origin + a * e1 + b * e2 for b in vs for a in us])


def trace(volume, seeds: np.ndarray, *, length: float,
          direction: str = "forward"):
    """Лінії потоку від насінин, довжиною не більше `length` у кожен бік.
    `direction` — "forward" (за потоком), "backward" (звідки прийшло) або
    "both". Кожна точка отримує швидкість SPEED — для розфарбування."""
    if direction not in ("forward", "backward", "both"):
        raise ValueError(tr("unknown line direction: {direction!r}", direction=direction))
    lines = volume.streamlines_from_source(
        pv.PolyData(np.asarray(seeds, dtype=float)), vectors="U",
        integration_direction=direction, max_length=float(length),
        initial_step_length=0.5, step_unit="cl", max_step_length=1.0,
        terminal_speed=1e-2, max_steps=200000, compute_vorticity=False,
        # Пошук комірки — локатором, а не «кроком від сусідньої точки»:
        # на шарі проміжного результату другий губив комірку посеред домену,
        # і лінії обривалися за 0.2–0.5 м (2026-11-11, case_jet: медіана
        # довжини 0.46 м → 2.98 м). На повній сітці — так само або краще.
        interpolator_type="cell")
    if lines.n_points:
        lines.point_data[SPEED] = np.linalg.norm(
            np.asarray(lines.point_data["U"], dtype=float), axis=1)
    return lines


# --- Де сіяти: вибір людини -----------------------------------------------
#
# «Авто» — це те, що було завжди: кільця перед гвинтами й гребінка перед
# носом. Але цікаве буває й деінде: кінцевий вихор, слід за хвостом, канал.
# Тому місце насіння можна вибрати: відрізок або кулю, які тягнуть мишею у
# вікні, або площину зрізу. Лінії рахуються секунди, тож перераховуються,
# коли кнопку миші ВІДПУСТИЛИ, а не на кожному кроці перетягування.

SEED_MODES = (
    ("auto", "Propellers and in front of the nose"),
    ("nose", "In front of the nose"),
    ("rotors", "From the propellers"),
    ("line", "Line segment — drag in the view"),
    ("sphere", "Sphere — drag in the view"),
    ("slice", "On the slice plane"),
)
SEED_DIRECTIONS = (
    ("forward", "Downstream"),
    ("both", "Both ways"),
    ("backward", "Upstream"),
)
SEED_COUNT = 75            # 25 × 3 — гребінка, яка була до вибору
SEED_DRAGGABLE = ("line", "sphere")


@dataclass
class Seeding:
    """Звідки ростуть лінії потоку. Координати — у метрах, як у кейсі.

    `line`, `centre`, `radius` — None, доки людина нічого не тягнула: тоді
    місце ставиться саме, за габаритом тіла (`default_line`, `default_ball`).
    `count` — скільки насінин; кільця перед гвинтами його не слухають, у
    них своя густина за радіусом.
    """
    mode: str = "auto"
    count: int = SEED_COUNT
    direction: str = "forward"
    line: tuple | None = None          # ((x, y, z), (x, y, z))
    centre: tuple | None = None
    radius: float | None = None
    plane: tuple | None = None         # (вісь 0/1/2, позиція)

    def forget_place(self) -> None:
        """Забути, куди тягнули: для іншого кейса місце ставиться заново."""
        self.line = self.centre = self.radius = None


def bodies_bounds(volume, bodies: dict) -> tuple:
    """Габарит усіх частин тіла (xmin, xmax, ymin, …). Без тіла — обʼєм."""
    parts = [m for m in (bodies or {}).values() if m is not None and m.n_points]
    src = np.asarray([m.bounds for m in parts] if parts else [volume.bounds])
    bb = src.reshape(-1, 3, 2)
    lo3, hi3 = bb[:, :, 0].min(axis=0), bb[:, :, 1].max(axis=0)
    return tuple(np.column_stack([lo3, hi3]).ravel())


def default_line(bounds: tuple, fs: Freestream) -> tuple:
    """Відрізок, з якого починати: упоперек потоку перед носом, на всю
    ширину тіла й на середині висоти — середній ряд гребінки «Перед носом»."""
    ends = nose_seeds(bounds, fs, across=2, levels=1)
    return tuple(map(float, ends[0])), tuple(map(float, ends[-1]))


def default_ball(bounds: tuple, fs: Freestream) -> tuple:
    """Куля, з якої починати: одразу за тілом, по осі потоку. Радіус — 8 %
    діагоналі габариту, щоб було видно й тіло, й кулю. → (центр, радіус)."""
    d = _unit(fs.direction)
    lo, hi = np.asarray(bounds[0::2], float), np.asarray(bounds[1::2], float)
    centre = 0.5 * (lo + hi)
    reach = stream_reach(lo, hi, d)
    diag = float(np.linalg.norm(hi - lo))
    radius = 0.08 * diag
    at = centre + d * (0.5 * reach + 1.5 * radius)
    return tuple(map(float, at)), float(radius)


def line_seeds(p0, p1, count: int) -> np.ndarray:
    """`count` насінин рівно по відрізку, обидва кінці включно."""
    t = np.linspace(0.0, 1.0, max(int(count), 2))
    a, b = np.asarray(p0, float), np.asarray(p1, float)
    return a + t[:, None] * (b - a)


def ball_seeds(centre, radius: float, count: int) -> np.ndarray:
    """`count` насінин рівномірно в ОБʼЄМІ кулі. Розклад сталий (той самий
    генератор щоразу): інакше лінії мінялися б від кожного перерахунку, і
    не було б видно, що змінилося від того, що кулю зсунули."""
    n = max(int(count), 1)
    rng = np.random.default_rng(0)
    dirs = rng.normal(size=(n, 3))
    dirs /= np.maximum(np.linalg.norm(dirs, axis=1), 1e-12)[:, None]
    r = float(radius) * np.cbrt(rng.random(n))
    return np.asarray(centre, float) + dirs * r[:, None]


def plane_seeds(axis: int, pos: float, bounds: tuple, count: int, *,
                margin: float = 0.25) -> np.ndarray:
    """Рівна сітка насінин на площині зрізу, в межах габариту тіла з запасом
    `margin` з кожного боку. Комірки сітки — майже квадратні."""
    u, v = [a for a in (0, 1, 2) if a != axis]
    lo = np.asarray(bounds[0::2], float)
    hi = np.asarray(bounds[1::2], float)
    span = np.maximum(hi - lo, 1e-9)
    lo, hi = lo - margin * span, hi + margin * span
    eu, ev = hi[u] - lo[u], hi[v] - lo[v]
    n = max(int(count), 1)
    nu = max(1, int(round(math.sqrt(n * eu / ev))))
    nv = max(1, int(round(n / nu)))
    # Центри комірок, а не краї: на краю габариту з запасом течія
    # нецікава, а насінина на самому тілі нічого не покаже.
    us = lo[u] + (np.arange(nu) + 0.5) * eu / nu
    vs = lo[v] + (np.arange(nv) + 0.5) * ev / nv
    pts = np.zeros((nu * nv, 3))
    pts[:, axis] = pos
    gu, gv = np.meshgrid(us, vs)
    pts[:, u], pts[:, v] = gu.ravel(), gv.ravel()
    return pts


def seed_points(seeding: Seeding, bounds: tuple, fs: Freestream,
                rotors=()) -> np.ndarray:
    """Насінини за вибором людини. ValueError — якщо сіяти нема звідки
    (гвинтів немає, площину не задано): це треба сказати, а не мовчки
    показати порожнечу."""
    mode = seeding.mode
    rotors = list(rotors or [])
    count = max(int(seeding.count), 1)
    if mode == "rotors" and not rotors:
        raise ValueError(tr("The case has no propellers (rotating zones) — "
                         "there is nothing to seed from. Select a "
                         "different place."))
    if mode in ("auto", "nose", "rotors"):
        pts = [rotor_seeds(r, fs) for r in rotors] if mode != "nose" else []
        if mode != "rotors":
            levels = 3 if count >= 6 else 1
            across = max(2, int(round(count / levels)))
            pts.append(nose_seeds(bounds, fs, across=across, levels=levels))
        return np.vstack(pts)
    if mode == "line":
        p0, p1 = seeding.line or default_line(bounds, fs)
        return line_seeds(p0, p1, count)
    if mode == "sphere":
        centre, radius = default_ball(bounds, fs)
        if seeding.centre is not None:
            centre = seeding.centre
        if seeding.radius:
            radius = seeding.radius
        return ball_seeds(centre, radius, count)
    if mode == "slice":
        if seeding.plane is None:
            raise ValueError(tr("The slice plane is not set."))
        axis, pos = seeding.plane
        return plane_seeds(int(axis), float(pos), bounds, count)
    raise ValueError(tr("unknown seed place: {mode!r}", mode=mode))


def seed_label(seeding: Seeding | None, rotors=()) -> str:
    """Підпис «засів: …» для кадру звіту — словами, як у смузі керування."""
    mode = seeding.mode if seeding is not None else "auto"
    if mode == "auto":
        text = (tr("rings in front of the propeller disks and a rake in front of the nose")
                if rotors else tr("rake in front of the nose"))
    elif mode == "nose":
        text = tr("rake in front of the nose")
    elif mode == "rotors":
        text = tr("rings in front of the propeller disks")
    elif mode == "line":
        text = tr("line segment")
    elif mode == "sphere":
        text = tr("sphere")
    else:
        axis = int(seeding.plane[0]) if seeding.plane else 0
        pos = float(seeding.plane[1]) if seeding.plane else 0.0
        text = tr("plane {upper} = {v:g} mm", upper=AXES[axis].upper(), v=pos * 1000)
    if seeding is not None and seeding.direction != "forward":
        text += ", " + tr(dict(SEED_DIRECTIONS)[seeding.direction]).map(
            str.lower)
    return text


class SeedWidget:
    """Насіння, яке тягнуть мишею у вʼюпорті: відрізок або куля.

    Відрізок — два кінці-ручки (тягнути кінець) і сама лінія (тягнути
    цілим). Куля — тягнути лівою кнопкою, радіус — правою або числом у
    смузі. `on_change` викликається, коли кнопку ВІДПУСТИЛИ, з новим станом:
    {"line": (p0, p1)} або {"centre": c, "radius": r}.
    """

    def __init__(self, plotter, kind: str, *, bounds: tuple,
                 line: tuple | None = None, centre=None,
                 radius: float | None = None, on_change=None):
        if kind not in SEED_DRAGGABLE:
            raise ValueError(tr("only {SEED_DRAGGABLE} can be dragged: {kind!r}", SEED_DRAGGABLE=SEED_DRAGGABLE, kind=kind))
        self.kind = kind
        self.plotter = plotter
        self._on_change = on_change
        self._widget = None
        self._rep = None
        iren = getattr(plotter, "iren", None)
        interactor = getattr(iren, "interactor", None)
        ink = pv.Color(INK).float_rgb
        if interactor is None:
            # foamview (fs_view3d.Plotter): свої ручки з тими ж методами VTK.
            make = getattr(plotter, "add_drag_widget", None)
            if make is None:
                return
            if kind == "line":
                p0, p1 = line
                w = make("line", point1=p0, point2=p1, bounds=bounds, color=ink,
                         line_width=3.0)
                self._widget = w
            else:
                w = make("sphere", centre=centre, radius=float(radius), bounds=bounds,
                         color=ink)
                self._widget, self._rep = w, w
            w.AddObserver("EndInteractionEvent", lambda *_: self._released())
            w.On()
            return
        if kind == "line":
            w = vtk.vtkLineWidget()
            w.SetInteractor(interactor)
            w.SetCurrentRenderer(plotter.renderer)
            w.SetPlaceFactor(1.0)
            w.PlaceWidget(bounds)
            # Без цього віджет сам повертає відрізок уздовж осі X.
            w.SetAlignToNone()
            w.SetClampToBounds(False)
            p0, p1 = line
            w.SetPoint1(*map(float, p0))
            w.SetPoint2(*map(float, p1))
            w.GetLineProperty().SetColor(*ink)
            w.GetLineProperty().SetLineWidth(3.0)
            w.GetHandleProperty().SetColor(*ink)
            self._widget = w
        else:
            rep = vtk.vtkSphereRepresentation()
            rep.SetPlaceFactor(1.0)
            rep.PlaceWidget(bounds)
            rep.SetCenter(*map(float, centre))
            rep.SetRadius(float(radius))
            rep.SetRepresentationToWireframe()
            rep.SetHandleVisibility(0)
            rep.SetThetaResolution(24)
            rep.SetPhiResolution(12)
            rep.GetSphereProperty().SetColor(*ink)
            rep.GetSphereProperty().SetOpacity(0.6)
            rep.GetSelectedSphereProperty().SetColor(*ink)
            w = vtk.vtkSphereWidget2()
            w.SetInteractor(interactor)
            w.SetRepresentation(rep)
            w.SetCurrentRenderer(plotter.renderer)
            w.SetTranslationEnabled(True)
            w.SetScalingEnabled(True)
            self._widget, self._rep = w, rep
        w.AddObserver("EndInteractionEvent", lambda *_: self._released())
        w.On()

    def has_widget(self) -> bool:
        return self._widget is not None

    def state(self) -> dict:
        w = self._widget
        if w is None:
            return {}
        if self.kind == "line":
            return {"line": (tuple(w.GetPoint1()), tuple(w.GetPoint2()))}
        return {"centre": tuple(self._rep.GetCenter()),
                "radius": float(self._rep.GetRadius())}

    def set_radius(self, radius: float) -> None:
        """Радіус числом зі смуги — без сигналу: він і так відомий."""
        if self._rep is not None:
            self._rep.SetRadius(float(radius))

    def _released(self) -> None:
        if self._on_change is not None and self._widget is not None:
            self._on_change(self.state())

    def close(self) -> None:
        w, self._widget, self._rep = self._widget, None, None
        if w is not None:
            try:
                w.Off()
                w.RemoveAllObservers()
                w.SetInteractor(None)
            except Exception:
                pass


def stream_limits(lines) -> tuple:
    """Шкала швидкості вздовж ліній: від 1-го до 99-го процентиля, округлена.
    Беремо саме лінії, а не обʼєм: вони йдуть там, де цікаво, а не по всьому
    домену, де майже скрізь U∞."""
    if not lines.n_points:
        return 0.0, 1.0
    lo, hi = np.percentile(np.asarray(lines.point_data[SPEED]), (1.0, 99.0))
    if hi - lo <= 1e-9 * max(abs(hi), 1.0):
        hi = lo + 1.0
    lo, hi, _ = nice_limits(float(lo), float(hi), False)
    return lo, hi


class StreamScene:
    """Лінії потоку в будь-якому Plotter: розфарбовані швидкістю, з шкалою."""

    def __init__(self, plotter, lines, *, limits: tuple | None = None,
                 line_width: float = 2.0, bar: bool = True,
                 vertical: bool = False, label: str = "speed U,  m/s"):
        self.plotter = plotter
        self.lines = lines
        self.limits = limits or stream_limits(lines)
        self._actor = None
        self._bar_title = ""
        if lines.n_points:
            self._actor = plotter.add_mesh(
                lines, scalars=SPEED, cmap="turbo", clim=self.limits,
                line_width=line_width, render_lines_as_tubes=True,
                lighting=False, name="fs_streams", show_scalar_bar=False,
                reset_camera=False, render=False)
            if bar:
                self._bar_title = _unique_title(plotter, tr(label))
                # Уздовж низу вже стоїть шкала зрізу, якщо він увімкнений, —
                # тоді шкала ліній іде вертикально ліворуч.
                place = (dict(vertical=True, position_x=0.03, position_y=0.22,
                              width=0.05, height=0.56) if vertical else
                         dict(vertical=False, position_x=0.27, position_y=0.035,
                              width=0.46, height=0.05))
                b = plotter.add_scalar_bar(
                    title=self._bar_title, mapper=self._actor.mapper,
                    n_labels=len(ticks_for(*self.limits, False)),
                    fmt=_bar_fmt(*self.limits), color=INK, title_font_size=15,
                    label_font_size=13, render=False, **place)
                _style_bar(b)

    def close(self) -> None:
        p = self.plotter
        if self._actor is not None:
            try:
                p.remove_actor(self._actor, render=False)
            except Exception:
                pass
        if self._bar_title:
            try:
                p.remove_scalar_bar(self._bar_title, render=False)
            except Exception:
                pass
        self._actor, self._bar_title = None, ""


BODY_COLOR = "#b9c4cf"


def _nearest_up(d: np.ndarray) -> np.ndarray:
    """«Угору» для потоку `d`: вісь z без складової вздовж потоку."""
    up = np.array([0.0, 0.0, 1.0])
    up = up - (up @ d) * d
    return _unit(up) if np.linalg.norm(up) > 1e-9 else np.array([0.0, 1.0, 0.0])


def rotor_camera(plotter, rotor: Rotor, fs: Freestream, *, back: float = 3.0,
                 up: float = 2.0, side: float = 4.5) -> None:
    """Камера зблизька на гвинт: ззаду-збоку-зверху, з боку ЗОВНІШНЬОГО краю.

    Відстані — у радіусах гвинта, тож кадр однаково працює для гвинта в
    десять сантиметрів і в метр. Сторона береться за положенням гвинта:
    правий дивимось справа, лівий — зліва, щоб обидва кадри для порівняння
    (з ходом і без) дивилися однаково.
    """
    d = _unit(fs.direction)
    e_up = _nearest_up(d)
    e_side = np.cross(e_up, d)
    c = np.asarray(rotor.centre, dtype=float)
    s = 1.0 if c @ e_side >= 0 else -1.0
    r = rotor.radius
    cam = plotter.camera
    cam.focal_point = list(c + r * d)
    cam.position = list(c + r * (-back * d + up * e_up + s * side * e_side))
    cam.up = list(e_up)
    cam.parallel_projection = False
    plotter.reset_camera_clipping_range()


def render_streams_frame(lines, bodies: dict, fs: Freestream, *, view: str,
                         rotor: Rotor | None = None, limits: tuple | None = None,
                         size: tuple[int, int] = (1300, 700), bg: str = "#000000",
                         label: str = "", ss: int = 2, line_width: float = 2.0,
                         reach: float = 10.0) -> Frame:
    """Кадр ліній потоку: "rotor" — зблизька на гвинт, "top" — згори, "iso" —
    усе тіло в 3D. Згори камера паралельна, тож там є масштаб і потік."""
    w, h = size
    pl = pv.Plotter(off_screen=True, window_size=[w * ss, h * ss])
    mpp, flow = 0.0, (0.0, 0.0)
    try:
        pl.set_background(bg)
        StreamScene(pl, lines, limits=limits, line_width=line_width * ss,
                    bar=False)
        parts = [m for m in bodies.values() if m is not None and m.n_points]
        for i, m in enumerate(parts):
            pl.add_mesh(m, color=BODY_COLOR, smooth_shading=True, ambient=0.25,
                        name=f"fs_body_{i}", show_scalar_bar=False,
                        reset_camera=False, render=False)
        # Без тіл (кейс, де їх не вдалося визначити) кадр міряється по самих
        # лініях: краще показати потік, ніж упасти на порожньому мінімумі.
        bb = np.asarray([m.bounds for m in parts] if parts
                        else [lines.bounds]).reshape(-1, 3, 2)
        lo3, hi3 = bb[:, :, 0].min(axis=0), bb[:, :, 1].max(axis=0)
        span = float(np.max(hi3 - lo3))
        d = _unit(fs.direction)
        if view == "top":
            pl.view_xy()
            pl.camera.parallel_projection = True
            # Перед носом насіння нема, тож рамка бере мало повітря спереду й
            # багато ззаду: там слід, заради якого кадр і робиться.
            rect = (lo3[0] - 0.2 * span, hi3[0] + 0.8 * span,
                    lo3[1] - 0.2 * span, hi3[1] + 0.2 * span)
            mpp = fit_camera(pl, 2, 0.5 * (lo3[2] + hi3[2]), rect, (w, h),
                             reach * span)
            flow = (float(d[0]), float(d[1]))
        elif view == "rotor" and rotor is not None:
            rotor_camera(pl, rotor, fs)
        else:
            c = 0.5 * (lo3 + hi3)
            e_up = _nearest_up(d)
            e_side = np.cross(e_up, d)
            cam = pl.camera
            cam.focal_point = list(c + 0.15 * span * d)
            cam.position = list(c + span * (-0.9 * d + 0.7 * e_up - 1.3 * e_side))
            cam.up = list(e_up)
            cam.parallel_projection = False
            pl.reset_camera_clipping_range()
        shot = pl.screenshot(return_img=True)
    finally:
        pl.close()
    img = shot[:h * ss, :w * ss, :3].astype(np.float64)
    img = img.reshape(h, ss, w, ss, 3).mean(axis=(1, 3)).round().astype(np.uint8)
    return Frame(img, label, mpp, (), (), flow)


def stream_reach(lo3, hi3, direction) -> float:
    """Довжина коробки тіла ВЗДОВЖ потоку: сума модулів складових, а не
    модуль суми. Від'ємний кут атаки дає від'ємну складову, і «модуль суми»
    скорочував довжину ліній удвічі вже на −30°."""
    return float(np.abs(_unit(direction)) @ (np.asarray(hi3) - np.asarray(lo3)))


def case_streamlines(volume, bodies: dict, fs: Freestream, rotors=(), *,
                     across: int = 25, levels: int = 3,
                     length: float | None = None,
                     seeding: Seeding | None = None):
    """Насіння і лінії від нього. Одна функція і для вікна, і для звіту:
    вони мусять показувати однакове.

    Без `seeding` — як було завжди: кільця перед гвинтами + гребінка перед
    носом (`across` × `levels`). З `seeding` — там, де вибрала людина.
    """
    if "U" not in volume.point_data:
        raise ValueError(tr("The volume has no velocity U — there will be no streamlines."))
    bounds = bodies_bounds(volume, bodies)
    lo3, hi3 = np.asarray(bounds[0::2]), np.asarray(bounds[1::2])
    direction = "forward"
    if seeding is None:
        seeds = [rotor_seeds(r, fs) for r in rotors]
        seeds.append(nose_seeds(bounds, fs, across=across, levels=levels))
        seeds = np.vstack(seeds)
    else:
        seeds = seed_points(seeding, bounds, fs, rotors)
        direction = seeding.direction
    plane = stream_plane(volume)
    if plane is not None:
        seeds = plane_only_seeds(seeds, plane, bounds, fs, seeding,
                                 across * levels)
    # Довжина ліній — втричі більша за тіло вздовж потоку: цього досить, щоб
    # було видно слід, а далі лінії лише витрачають час і кадр.
    reach = stream_reach(lo3, hi3, fs.direction)
    lines = trace(volume, seeds, length=length or 3.0 * reach,
                  direction=direction)
    # Скільки насінин дали лінію: решта — у тілі чи поза доменом (чи поза
    # 3D-вибіркою проміжного результату). Вікно це каже словами.
    lines.field_data[SEED_COUNT_KEY] = np.array([len(seeds)])
    return lines


# Мітка шару «швидкість лише в одній площині» (fs_live.PLANE_MARK) і
# кількість насінин у field_data ліній.
STREAM_PLANE = "fs_stream_plane"
SEED_COUNT_KEY = "fs_seeds"


def stream_plane(volume) -> tuple | None:
    """(вісь, позиція), якщо швидкість в обʼємі є лише в одній площині."""
    try:
        got = np.asarray(volume.field_data[STREAM_PLANE], dtype=float)
    except (KeyError, AttributeError):
        return None
    return (int(got[0]), float(got[1])) if got.size >= 2 else None


def started_lines(lines) -> int:
    """Скільки насінин дали лінію (у «обидва боки» насінина дає дві)."""
    if not lines.n_cells or "SeedIds" not in lines.cell_data:
        return int(lines.n_cells)
    return int(np.unique(np.asarray(lines.cell_data["SeedIds"])).size)


def plane_only_seeds(seeds, plane: tuple, bounds: tuple, fs: Freestream,
                     seeding: Seeding | None, count: int) -> np.ndarray:
    """Насінини для обʼєму, де швидкість є лише в площині `plane`.

    Насінина поза площиною лінії не дає, тож усе переноситься на площину.
    Гребінка перед носом і площина зрізу при перенесенні злиплися б у кілька
    точок, тому їх ставимо наново, у самій площині: рядок упоперек потоку
    перед носом і рівна сітка на площині.
    """
    axis, pos = plane
    mode = seeding.mode if seeding is not None else "auto"
    n = max(int(seeding.count if seeding is not None else count), 2)
    if mode == "slice":
        return plane_seeds(axis, pos, bounds, n)
    pts = np.array(seeds, dtype=float)
    if mode in ("auto", "nose"):
        d = _unit(fs.direction)
        normal = np.zeros(3)
        normal[axis] = 1.0
        across = np.cross(d, normal)
        if np.linalg.norm(across) < 1e-9:
            across = np.roll(normal, 1)
        across = _unit(across)
        lo, hi = np.asarray(bounds[0::2], float), np.asarray(bounds[1::2], float)
        corners = np.array([[x, y, z] for x in (lo[0], hi[0])
                            for y in (lo[1], hi[1]) for z in (lo[2], hi[2])])
        centre = 0.5 * (lo + hi)
        along = (corners - centre) @ d
        side = (corners - centre) @ across
        origin = centre + d * (along.min() - 0.08 * max(np.ptp(along), 1e-9))
        t = np.linspace(side.min() * 1.3, side.max() * 1.3, n)
        pts = origin + t[:, None] * across
    pts[:, axis] = pos
    # Перенесені точки можуть збігтися (кільця гвинта, куля).
    _, keep = np.unique(np.round(pts, 9), axis=0, return_index=True)
    return pts[np.sort(keep)]


def streams_report(views: list[CaseView], *, rotors: list[Rotor] | None = None,
                   name: str = "", limits: tuple | None = None,
                   size: tuple[int, int] = (1300, 700), path: str | None = None,
                   across: int = 25, levels: int = 3,
                   length: float | None = None,
                   traced: list | None = None,
                   seeding: Seeding | None = None) -> np.ndarray:
    """Лінії потоку для одного або кількох кейсів: рядок — кейс, стовпці —
    зблизька на гвинт (або 3D, якщо гвинтів нема) і згори. Шкала швидкості
    одна на всі кадри — з нею порівнюються кейси."""
    rotors = list(rotors or [])
    # Лінії вже могли бути пораховані (вікно їх показує): тоді не рахуємо
    # вдруге — це найдорожча частина кадру.
    if traced is None:
        traced = [case_streamlines(v.volume, v.bodies, v.fs, rotors,
                                   across=across, levels=levels, length=length,
                                   seeding=seeding)
                  for v in views]
    if limits is None:
        found = [stream_limits(t) for t in traced if t.n_points]
        limits = (min(f[0] for f in found), max(f[1] for f in found)) \
            if found else (0.0, 1.0)
    grid = []
    for v, lines in zip(views, traced):
        row = []
        if rotors:
            row.append(render_streams_frame(
                lines, v.bodies, v.fs, view="rotor", rotor=rotors[0],
                limits=limits, size=size, label=v.label))
        else:
            row.append(render_streams_frame(
                lines, v.bodies, v.fs, view="iso", limits=limits, size=size,
                label=v.label))
        row.append(render_streams_frame(
            lines, v.bodies, v.fs, view="top", limits=limits, size=size))
        grid.append(row)
    fs = views[0].fs
    spec = replace(spec_by_key("umag"),
                   label=tr("speed U,  m/s      (freestream {u_inf:g})", u_inf=fs.u_inf))
    seeded = seed_label(seeding, rotors)
    head = tr("{v}streamlines,   U∞ = {u_inf:g} m/s", v=f"{name} — " if name else "", u_inf=fs.u_inf)
    cols = (tr("propeller “{name}” close up", name=rotors[0].name) if rotors else "3D", "top")
    return compose(grid, title=head, subtitle=tr("seeding: {seeded}", seeded=seeded),
                   bar=(spec, limits[0], limits[1]), col_labels=cols, path=path)


# ==========================================================================
# Відрив, опір і стрілки
# ==========================================================================
#
# Три відповіді на «що з обтіканням не так», кожна зі свого джерела:
#
#   * ЗВОРОТНА ТЕЧІЯ — в обʼємі: де повітря йде назад, U·ê∞ < 0. Це сама
#     зірвана зона, а не її ознака. Ізоповерхня — трохи нижче нуля, а не на
#     нулі: на стінці U = 0 скрізь, і поверхня «U∥ = 0» обгорнула б усе тіло.
#   * ВІДРИВ НА ПОВЕРХНІ — з тертя: воно тягне тіло туди, куди біжить
#     пристінкове повітря. Де воно біжить проти потоку — течія відірвалася.
#   * ВНЕСОК В ОПІР — тиск і тертя на кожній грані, спроєктовані на потік.
#     Сума по тілу — рівно Cd із forceCoeffs, і це перевіряється тестом.
#
# ЗНАК wallShearStress. OpenFOAM пише τ = −n·R, де n дивиться З ПОВІТРЯ В
# СТІНКУ: це напруження, з яким СТІНКА гальмує повітря, і воно напрямлене
# ПРОТИ пристінкової течії. Сила тертя на тіло — −ρ·τ. Прибито двома
# незалежними способами на `gui_wing`: сума ρ(−(p − p∞)·n − τ) по тілу дає
# Cd 0.039179 проти 0.039144 у forceCoeffs (з другим знаком τ — 0.0269), а
# грані, де пристінкова течія йде назад, лежать рівно там, де в обʼємі U∥ < 0.
#
# Нормалі. Читач віддає грані патча в порядку OpenFOAM, і нормаль за
# правилом правої руки дивиться В ТІЛО (з повітря). На це не покладаємось:
# напрямок перевіряється по самій поверхні (див. `outward`).

WALL_TAU = "wallShearStress"
SEP = "fs_sep"            # пристінкова течія відносно потоку: −1 … +1
DRAG = "fs_drag"          # внесок грані в опір, Па
NEAR = "fs_near"          # пристінкова течія (−τ), точкове: для ліній на тілі
LIFTED = "fs_lifted"      # field_data: лінії вже підняті над поверхнею
REVERSE = "fs_rev"        # U·ê∞ / U∞ у точках обʼєму
LOSS = "fs_loss"          # втрата повного тиску, частка напору

REVERSE_LEVEL = 0.02      # «назад» — швидше за 2% U∞: менше — шум і стінка
LOSS_LEVEL = 0.1          # слід: де загублено 10% напору

REVERSE_COLOR = "#ff3b30"
LOSS_COLOR = "#4fc3f7"
WALL_LINE_COLOR = "#111418"
UNTOUCHED = "#5b636d"

# Підписи полів тіла у вікні. Масиви звуться латиницею (так їх шукає VTK), а
# людині показується назва з одиницями.
# The titles are also the values of the field combo box of the window (its
# item data), thus they are not translated here: tr() where they are shown.
SEP_TITLE = "Separation"            # коротко: це й підпис вертикальної шкали,
DRAG_TITLE = "Face drag, Pa"  # а довша назва там обрізається
BODY_FIELDS = {SEP_TITLE: SEP, DRAG_TITLE: DRAG}

SEP_SPEC = FieldSpec(
    "sep", SEP_TITLE,
    "near-wall flow relative to the stream:   −1 backward  ·  0 across  ·  +1 along",
    "diverging", "fs_sep", (WALL_TAU,),
    "red — the air near the wall moves against the flow: the flow has separated here")
DRAG_SPEC = FieldSpec(
    "drag", DRAG_TITLE,
    "contribution to drag: (pressure + friction) along the flow,  Pa",
    "diverging", "fs_diverging", ("p",),
    "warm — the face pulls the body back (drag), blue — pushes it "
    "forward (suction at the nose, thrust)")


@lru_cache(maxsize=1)
def sep_cmap() -> LinearSegmentedColormap:
    """Червоне — назад, сіре — уздовж. Не симетрична «синє/тепле»: тут
    цікавий лише один бік. Прилипла течія — це норма, і вона мусить бути
    спокійною, а відрив — кричати."""
    back = _ramp(["#ff3b30", "#c83a2c", "#7a3a33", "#5d646d"], 128)
    along = _ramp(["#5d646d", "#8792a0", "#c9d3dd"], 128)
    return LinearSegmentedColormap.from_list("fs_sep", np.vstack([back, along]),
                                             N=256)


def outward(mesh) -> tuple[np.ndarray, np.ndarray]:
    """(одинична нормаль НАЗОВНІ з тіла, площа) для кожної грані.

    Напрямок нормалі перевіряється за самою поверхнею: Σ (c − c̄)·n·A — це
    потроєний обʼєм усередині для зовнішніх нормалей і мінус він — для
    внутрішніх. Працює й на відкритій поверхні (половинка моделі на площині
    симетрії): там бракує лише плоскої латки, і знак не міняється.
    """
    got = mesh.compute_normals(cell_normals=True, point_normals=False,
                               consistent_normals=False,
                               auto_orient_normals=False, split_vertices=False)
    n = np.asarray(got.cell_data["Normals"], dtype=float)
    area = np.asarray(mesh.compute_cell_sizes(
        length=False, area=True, volume=False).cell_data["Area"], dtype=float)
    c = np.asarray(mesh.cell_centers().points, dtype=float)
    mid = (c * area[:, None]).sum(axis=0) / max(float(area.sum()), 1e-30)
    if float(np.einsum("ij,ij->i", c - mid, n) @ area) < 0:
        n = -n
    return n, area


@dataclass
class SurfaceLoads:
    """Сила на тіло, складена з граней. Не з forceCoeffs: це перевірка, що
    картинка «хто скільки дає опору» складається в те саме число."""
    force: np.ndarray            # Н, вектор
    drag: float                  # Н, уздовж потоку
    pressure: float              # Н, з неї від тиску
    friction: float              # Н, і від тертя
    area: float                  # м², уся поверхня
    separated: float             # м², де пристінкова течія йде назад
    judged: float                # м², де про це взагалі можна судити
    has_friction: bool

    def cd(self, q: float, a_ref: float) -> float:
        return self.drag / (q * a_ref) if q > 0 and a_ref > 0 else float("nan")


def add_wall_fields(body, fs: Freestream) -> SurfaceLoads | None:
    """Кладе в тіло DRAG і, якщо є тертя, SEP (на гранях) та NEAR (у точках).
    Повертає сумарні сили. None — на тілі немає тиску на гранях.

    Без wallShearStress (кейс рахувався без цієї функції) опір складається
    лише з тиску, а відриву на поверхні не показати: напрямок пристінкової
    течії взяти нізвідки. Тоді `has_friction` = False — і хто показує, мусить
    це сказати.
    """
    if body is None or not body.n_cells or "p" not in body.cell_data:
        return None
    n, area = outward(body)
    d = _unit(fs.direction)
    p = np.asarray(body.cell_data["p"], dtype=float)
    dp = (p - fs.p_inf) if fs.compressible else p * fs.rho
    # Тиск тисне В тіло — проти зовнішньої нормалі.
    f_p = -dp[:, None] * n
    f = f_p
    has_tau = WALL_TAU in body.cell_data
    f_t = np.zeros_like(f_p)
    sep_area = judged_area = 0.0
    if has_tau:
        tau = np.asarray(body.cell_data[WALL_TAU], dtype=float)
        # Кінематичне напруження — у нестисливому, як і тиск.
        f_t = -(tau if fs.compressible else tau * fs.rho)
        f = f_p + f_t
        near = -tau
        # Напрямок потоку, «покладений» на грань. На грані, що стоїть
        # упоперек потоку (торець, днище), він вироджується — там про «назад»
        # не судимо. Як і на гранях, звернених проти потоку: на носі повітря
        # від точки гальмування законно біжить уперед-угору, огинаючи його, і
        # це не відрив.
        t_inf = d - (n @ d)[:, None] * n
        tn = np.linalg.norm(t_inf, axis=1)
        wn = np.linalg.norm(near, axis=1)
        floor = 1e-6 * float(wn.max()) if wn.size else 0.0
        ok = (tn > 0.2) & (wn > floor) & ((n @ d) > -0.5)
        cos = np.zeros(body.n_cells)
        cos[ok] = np.einsum("ij,ij->i", near[ok], t_inf[ok]) / (wn[ok] * tn[ok])
        body.cell_data[SEP] = cos
        judged_area = float(area[ok].sum())
        sep_area = float(area[ok & (cos < 0)].sum())
        if WALL_TAU in body.point_data:
            body.point_data[NEAR] = -np.asarray(body.point_data[WALL_TAU],
                                                dtype=float)
    body.cell_data[DRAG] = f @ d
    total = (f * area[:, None]).sum(axis=0)
    return SurfaceLoads(
        force=total, drag=float(total @ d),
        pressure=float(((f_p * area[:, None]).sum(axis=0)) @ d),
        friction=float(((f_t * area[:, None]).sum(axis=0)) @ d),
        area=float(area.sum()), separated=sep_area, judged=judged_area,
        has_friction=has_tau)


ROTOR = "fs_rotor"         # 1 — грань належить гвинту


def sum_loads(items: list) -> SurfaceLoads | None:
    items = [x for x in items if x is not None]
    if not items:
        return None
    return SurfaceLoads(
        force=sum((x.force for x in items), np.zeros(3)),
        drag=sum(x.drag for x in items),
        pressure=sum(x.pressure for x in items),
        friction=sum(x.friction for x in items),
        area=sum(x.area for x in items),
        separated=sum(x.separated for x in items),
        judged=sum(x.judged for x in items),
        has_friction=all(x.has_friction for x in items))


def part_loads(bodies: dict, fs: Freestream, rotors=()):
    """(тіло з полями стінки, планер, гвинти).

    Гвинти — окремо, бо їхня «сила вздовж потоку» — це тяга. У сумі з
    планером вона давала на `p7b_run` «опір −14.5 Н»: правдиве число, але
    відповідь на інше питання, ніж «звідки опір». І шкала: лопаті дають до
    ±400 Па, і в шкалі «за всіма гранями» планер ставав рівно-чорним.
    Кожна частина рахується окремо — нормалі теж перевіряються окремо, бо в
    кожної своя «середина»."""
    meshes, frame, spin = [], [], []
    for name, m in (bodies or {}).items():
        if m is None or not m.n_points:
            continue
        m = m.copy()
        got = add_wall_fields(m, fs)
        rotor = _is_rotor(name, m, rotors)
        (spin if rotor else frame).append(got)
        m.cell_data[ROTOR] = np.full(m.n_cells, 1 if rotor else 0, dtype=np.int8)
        meshes.append(m)
    if not meshes:
        raise ValueError(tr("The case has no body surface — nothing to show."))
    body = meshes[0] if len(meshes) == 1 else meshes[0].merge(
        meshes[1:], merge_points=False)
    return body, sum_loads(frame), sum_loads(spin)


def drag_limits(body) -> tuple:
    """Симетрична шкала внеску в опір: 98-й процентиль за ПЛОЩЕЮ. Точка
    гальмування на носі дає в десятки разів більше за решту, і шкала «від
    мінімуму до максимуму» зробила б усе тіло одним кольором. Гвинти в шкалу
    не входять (див. `part_loads`): вони просто вийдуть насиченими."""
    v = np.asarray(body.cell_data[DRAG], dtype=float)
    area = np.asarray(body.compute_cell_sizes(
        length=False, area=True, volume=False).cell_data["Area"], dtype=float)
    ok = np.isfinite(v)
    if ROTOR in body.cell_data:
        frame = np.asarray(body.cell_data[ROTOR]) == 0
        if (ok & frame).any():
            ok &= frame
    h = _wpercentile(np.abs(v[ok]), area[ok], 98.0) if ok.any() else 1.0
    lo, hi, _ = nice_limits(-max(h, 1e-9), max(h, 1e-9), True)
    return lo, hi


def body_field_style(key: str, body) -> dict:
    """Як фарбувати тіло полем BODY_FIELDS: мапа кольорів і межі шкали."""
    if key == SEP:
        return dict(cmap=sep_cmap(), clim=(-1.0, 1.0))
    return dict(cmap=diverging_cmap(), clim=drag_limits(body))


# --- обʼєм: зворотна течія й слід ------------------------------------------

@dataclass
class Blobs:
    """Ізоповерхня в обʼємі й те, скільки під нею повітря."""
    surface: object                 # pv.PolyData
    level: float
    volume: float = 0.0             # м³, комірок по той бік рівня
    cells: int = 0


def _cell_volume(volume, ids: np.ndarray) -> float:
    """Обʼєм вибраних комірок. Точно, а не за габаритом, — бо їх мало: точний
    фільтр на всьому обʼємі коштує десятки секунд, на тисячі комірок — ні."""
    if not len(ids):
        return 0.0
    part = volume.extract_cells(ids).compute_cell_sizes(
        length=False, area=False, volume=True)
    return float(np.abs(np.asarray(part.cell_data["Volume"])).sum())


def reverse_flow(volume, fs: Freestream, level: float = REVERSE_LEVEL) -> Blobs:
    """Бульбашки зворотної течії: ізоповерхня U·ê∞ = −level·U∞."""
    if "U" not in volume.point_data:
        raise ValueError(tr("The volume has no velocity U — reverse flow cannot be found."))
    d = _unit(fs.direction)
    u = max(fs.u_inf, 1e-9)
    volume.point_data[REVERSE] = np.asarray(volume.point_data["U"], dtype=float) @ d / u
    iso = volume.contour([-level], scalars=REVERSE, compute_normals=True,
                         compute_scalars=False)
    ids = np.empty(0, dtype=np.int64)
    if "U" in volume.cell_data:
        ua = np.asarray(volume.cell_data["U"], dtype=float) @ d / u
        ids = np.flatnonzero(ua < -level)
    return Blobs(iso, level, _cell_volume(volume, ids), int(len(ids)))


def total_pressure_loss(volume, fs: Freestream) -> np.ndarray:
    """Втрата повного тиску, частка напору: (p0∞ − p0) / q∞.

    Повний тиск p0 = p + ½ρ|U|² — це енергія, яку несе повітря. Поза слідом
    вона та сама, що на вході, а в пограншарі й сліді губиться: саме в ці
    втрати й іде робота сили опору. Від'ємна втрата — повітря отримало
    енергію: так виглядає струмінь гвинта. У стисливому — нестислива
    формула; до M ≈ 0.3 різниця менша за 2%.

    Струменя гвинта (втрата < 0) тут окремою поверхнею немає, хоч він і
    рахується: у кейсі з обертовою зоною вона розсипається хмарою клаптів
    навколо лопатей і ховає під собою все. Куди йде струмінь — показують
    лінії обдуву (`prop_wash`).
    """
    U = np.asarray(volume.point_data["U"], dtype=float)
    p = np.asarray(volume.point_data["p"], dtype=float)
    q = fs.dynamic_pressure
    dp = (p - fs.p_inf) if fs.compressible else p * fs.rho
    p0 = dp + 0.5 * fs.rho * np.einsum("ij,ij->i", U, U)
    loss = (q - p0) / max(q, 1e-12)
    volume.point_data[LOSS] = loss
    return loss


def _proxy(mesh, cell: float):
    """Спрощена копія поверхні: вершини зведені в кубики з ребром `cell`."""
    tri = mesh.extract_surface(algorithm="dataset_surface").triangulate()         if not isinstance(mesh, pv.PolyData) else mesh.triangulate()
    b = np.asarray(tri.bounds, dtype=float).reshape(3, 2)
    n = np.maximum(np.ceil(np.ptp(b, axis=1) / max(cell, 1e-12)), 1).astype(int)
    if int(np.prod(n.astype(np.int64))) > 2 ** 31 - 1 or tri.n_cells < 20000:
        return tri                  # мале тіло й так швидке
    q = vtk.vtkQuadricClustering()
    q.SetInputData(tri)
    q.SetAutoAdjustNumberOfDivisions(False)
    q.SetNumberOfDivisions(*map(int, n))
    q.Update()
    out = pv.wrap(q.GetOutput())
    return out if out.n_cells else tri


def strip_wall(surface, bodies: dict, gap: float):
    """Прибрати з ізоповерхні те, що ближче до тіла, ніж `gap`.

    Пограншар губить напір по всій стінці (на самій стінці U = 0, тобто
    втрата — увесь напір), тож ізоповерхня втрат обгортає тіло суцільним
    коконом. Крізь нього не видно ні тіла, ні того, що на ньому, а нового
    кокон не каже нічого: те, що в пограншарі є тертя, відомо й так.
    Лишається те, що відійшло від стінки далі за пограншар, — слід і
    відірвані зони.

    Відстань міряється не до самого тіла, а до його спрощеної копії
    (`vtkQuadricClustering`, вузол — чверть `gap`). Точна відстань від 384
    тис. точок сліду до тіла на 334 тис. трикутників (`p7b_run`) — 45 с, а до
    копії — кілька секунд: похибка 1.5 мм (99-й процентиль) при `gap` 24 мм,
    інакше класифіковано 0.016% точок."""
    if surface is None or not surface.n_points or gap <= 0:
        return surface
    parts = [m for m in (bodies or {}).values() if m is not None and m.n_points]
    if not parts:
        return surface
    # Усі частини — в одну копію й один прохід: гвинти окремо коштували б
    # ще по проходу на кожен.
    whole = _append([m if isinstance(m, pv.PolyData)
                     else m.extract_surface(algorithm="dataset_surface")
                     for m in parts])
    got = surface.compute_implicit_distance(_proxy(whole, 0.25 * gap))
    surface.point_data["fs_gap"] = np.abs(np.asarray(
        got.point_data["implicit_distance"]))
    kept = surface.clip_scalar(scalars="fs_gap", value=gap, invert=False)
    return kept.extract_surface(algorithm="dataset_surface")         if kept.n_points else pv.PolyData()


def wake_box(bodies: dict, fs: Freestream, behind: float = 1.0) -> tuple:
    """Коробка навколо тіла, витягнута за ним на `behind` його довжини.

    Слід тягнеться до самого виходу з домену — у десять разів далі за тіло.
    Цілий він лише зменшує тіло в кадрі до цятки, а відстань до стінки
    (`strip_wall`) на кожній його точці коштувала б хвилину на кейсі з
    2.5 млн комірок."""
    b = np.asarray(view_bounds(bodies, pad=0.0), dtype=float).reshape(3, 2)
    span = float(np.ptp(b, axis=1).max())
    d = _unit(fs.direction)
    lo, hi = b[:, 0] - 0.1 * span, b[:, 1] + 0.1 * span
    hi = hi + behind * span * np.clip(d, 0.0, None)
    lo = lo + behind * span * np.clip(d, None, 0.0)
    return tuple(np.column_stack([lo, hi]).ravel())


def loss_blobs(volume, fs: Freestream, level: float = LOSS_LEVEL,
               bodies: dict | None = None, gap: float | None = None) -> Blobs:
    """Слід: ізоповерхня втрати повного тиску `level`. З `bodies` вона
    обрізається коробкою за тілом (`wake_box`) і біля стінки (`strip_wall`);
    `gap` — типово 2% найбільшого розміру тіла."""
    if not all(a in volume.point_data for a in ("U", "p")):
        raise ValueError(tr("The volume has no U or p — total pressure cannot be computed."))
    total_pressure_loss(volume, fs)
    wake = volume.contour([level], scalars=LOSS, compute_normals=True,
                          compute_scalars=False)
    if bodies and wake.n_points:
        box = wake_box(bodies, fs)
        wb = wake.bounds
        if not all(box[2 * k] <= wb[2 * k] and wb[2 * k + 1] <= box[2 * k + 1]
                   for k in range(3)):
            wake = wake.clip_box(box, invert=False)
            wake = wake.extract_surface(algorithm="dataset_surface") \
                if wake.n_points else pv.PolyData()
        if gap is None:
            b = view_bounds(bodies, pad=0.0)
            gap = 0.02 * max(b[1] - b[0], b[3] - b[2], b[5] - b[4])
        wake = strip_wall(wake, bodies, gap)
    return Blobs(wake, level)


# --- лінії на поверхні тіла («масляна плівка») ------------------------------

def surface_seeds(body, count: int = 600) -> np.ndarray:
    """Насіння по поверхні, рівномірно за площею: одна точка на кубик із
    ребром √(площа / count). Сітка тіла густа там, де складна геометрія, і
    насіння «по гранях» засипало б ніс, лишивши боки голими."""
    c = np.asarray(body.cell_centers().points, dtype=float)
    area = np.asarray(body.compute_cell_sizes(
        length=False, area=True, volume=False).cell_data["Area"], dtype=float)
    h = math.sqrt(max(float(area.sum()), 1e-30) / max(count, 1))
    key = np.floor((c - c.min(axis=0)) / h).astype(np.int64)
    _, first = np.unique(key, axis=0, return_index=True)
    return c[np.sort(first)]


def wall_lines(body, *, count: int = 600, length: float | None = None):
    """Лінії вздовж пристінкової течії, по самій поверхні.

    Так у аеротрубі мажуть модель олією з сажею: потік розтягує її смугами,
    і видно лінію, де смуги сходяться (відрив), і де розходяться
    (приєднання). Тут те саме з поля тертя: лінії біжать за −τ.
    """
    if NEAR not in body.point_data:
        raise ValueError(tr("The body has no wallShearStress — there is no "
                         "source for the near-wall flow direction."))
    import fs_fast
    if fs_fast.has("surface_lines"):
        return wall_lines_rs(body, count=count, length=length)
    seeds = surface_seeds(body, count)
    span = float(max(body.length, 1e-9))
    lines = body.streamlines_from_source(
        pv.PolyData(seeds), vectors=NEAR, surface_streamlines=True,
        integration_direction="both", max_length=length or 0.2 * span,
        initial_step_length=0.3, step_unit="cl", max_step_length=0.6,
        terminal_speed=1e-9, max_steps=3000, compute_vorticity=False)
    return lines


def _outward_vertex_normals(pts: np.ndarray, tri: np.ndarray) -> np.ndarray:
    """Нормалі вершин, повернуті ВІД тіла — у бік повітря.

    Грані патчів OpenFOAM дивляться з потоку в тіло, але покладатися на це
    не можна (тіло могло прийти й зі сцени). Тому знак вирішує більшість:
    сума площ граней, чия нормаль дивиться від центру тіла, проти тих, що до
    нього. На неопуклому літаку окремі грані дивляться «не так», а сума — ні.
    """
    a, b, c = pts[tri[:, 0]], pts[tri[:, 1]], pts[tri[:, 2]]
    fn = np.cross(b - a, c - a)                      # довжина = 2·площа
    centre = pts.mean(axis=0)
    out = np.einsum("ij,ij->i", fn, (a + b + c) / 3.0 - centre)
    if out.sum() < 0:
        fn = -fn
    vn = np.zeros_like(pts)
    for k in range(3):
        np.add.at(vn, tri[:, k], fn)
    m = np.linalg.norm(vn, axis=1)
    m[m == 0] = 1.0
    return vn / m[:, None]


def wall_lines_rs(body, *, count: int = 600, length: float | None = None):
    """`wall_lines` на Rust (foamrs.surface_lines): лінія переходить лише в
    СУСІДНЮ по ребру грань.

    Навіщо. vtkStreamTracer у режимі поверхні шукає наступну грань як
    найближчу в просторі. На тонкому крилі найближча часто з іншого боку —
    лінія перестрибувала з низу на верх і йшла поперек течії; біля входу
    двигуна летіла навпростець крізь отвір; а від запуску до запуску (потоки
    VTK) лінії були різні. Тут вони однакові щоразу й не сходять з тіла.

    Лінія зупиняється там, де сходиться течія з обох боків ребра (лінія
    відриву — саме там і збирається олія в аеротрубі), на краю поверхні й
    там, де тертя немає.
    """
    import fs_fast
    tri_mesh = body.triangulate()
    if not tri_mesh.is_all_triangles:
        tri_mesh = tri_mesh.extract_surface(algorithm="dataset_surface") \
            .triangulate()
    # Патчі тіла приходять окремими шматками зі своїми копіями точок на
    # швах. Без злиття шов був би краєм поверхні, і лінії на ньому рвалися б.
    tri_mesh = tri_mesh.clean(tolerance=0.0, point_merging=True)
    if not tri_mesh.n_cells or NEAR not in tri_mesh.point_data:
        return pv.PolyData()
    pts = np.asarray(tri_mesh.points, dtype=float)
    tri = np.asarray(tri_mesh.faces).reshape(-1, 4)[:, 1:]
    vec = np.asarray(tri_mesh.point_data[NEAR], dtype=float)
    span = float(max(tri_mesh.length, 1e-9))
    # РІВНОМІРНИЙ ВІДСТУП (Jobard–Lefer, у foamrs): лінія зупиняється, коли
    # підходить до вже намальованої ближче за пів відступу. Без нього лінії
    # з сусіднього насіння сходилися в темні пучки і закривали тіло.
    # Відступ — з `count`: стільки «клітинок» відступ×відступ на площі,
    # поділеній на 4, тобто ліній помітно густіше, ніж насіння в VTK.
    c = (pts[tri[:, 0]] + pts[tri[:, 1]] + pts[tri[:, 2]]) / 3.0
    area = 0.5 * np.linalg.norm(np.cross(pts[tri[:, 1]] - pts[tri[:, 0]],
                                         pts[tri[:, 2]] - pts[tri[:, 0]]),
                                axis=1)
    spacing = 0.5 * math.sqrt(max(float(area.sum()), 1e-30) / max(count, 1))
    # Кандидати в насіння — по одній грані на кубик з ребром у пів відступу,
    # у сталому випадковому порядку: обхід «по номерах граней» спершу
    # засівав би одну частину тіла, а решта діставала б лише обрізки.
    key = np.floor((c - c.min(axis=0)) / (0.5 * spacing)).astype(np.int64)
    _, first = np.unique(key, axis=0, return_index=True)
    seeds = np.random.default_rng(0).permutation(first)
    mag = np.linalg.norm(vec, axis=1)
    floor = 1e-6 * float(mag.max()) if mag.size else 0.0
    got, counts = fs_fast.surface_lines(
        pts, tri, vec, _outward_vertex_normals(pts, tri), seeds,
        length or 0.2 * span, max_faces=200000,
        # Лінії трохи над поверхнею: інакше вони б'ються з гранями за
        # глибину й тремтять, коли крутиш модель.
        offset=4e-4 * span, floor=floor,
        spacing=spacing, min_length=2.0 * spacing)
    got = np.asarray(got)
    counts = np.asarray(counts, dtype=np.int64)
    if not counts.size:
        return pv.PolyData()
    starts = np.concatenate([[0], np.cumsum(counts)[:-1]])
    conn = np.concatenate([np.concatenate([[n], s + np.arange(n)])
                           for s, n in zip(starts, counts)])
    # Лише лінії. pv.PolyData(точки) сам додає кожну точку вершиною, і
    # поверх ліній малювалися 100+ тис. квадратиків — суцільні чорні смуги.
    out = pv.PolyData(got, lines=conn)
    out.field_data[LIFTED] = np.array([1], dtype=np.int8)
    return out


# --- обдув гвинтів: до чого дістає струмінь ----------------------------------
#
# Питання з ескізу — «чи дістає обдув гвинта до хвоста», «чи йде він у канал
# і по верху крила». Відповідь — лінії від диска кожного гвинта, пофарбовані
# частиною тіла, до якої вони ВЖЕ доторкнулися: лінія сіра, доки не
# підійшла до жодної, далі — колір першої, а пройшовши повз наступну — її.
# Тож на одній лінії видно й послідовність: гвинт → канал → хвіст.
#
# Частини — це патчі тіла. Якщо тіло одне (кейс без роздільних патчів), воно
# ріжеться на третини вздовж потоку: «хвіст» тоді — задня третина. Грубо, але
# відповідає на те саме питання й без перебудови кейсу.

WASH = "fs_wash"
WASH_RINGS = (0.3, 0.5, 0.7, 0.9)        # частки радіуса — усі ВСЕРЕДИНІ диска
WASH_PER_RING = (6, 10, 14, 18)
THIRDS = ("front third", "middle third", "rear third (tail)")


@dataclass
class WashReach:
    lines: object                    # pv.PolyData з точковим WASH
    zones: list                      # назви частин, порядок = індекс у WASH
    shares: dict                     # частина → частка ліній, що до неї дійшли
    sides: dict                      # частина → (згори, знизу, збоку), частки
    seeds: int                       # скільки ліній
    near: float                      # «доторкнулася» — ближче за це, м
    split: bool                      # тіло різалося на третини


def _is_rotor(name: str, mesh, rotors) -> bool:
    for r in rotors:
        if name == r.name:
            return True
        b = np.asarray(mesh.bounds).reshape(3, 2)
        centre, size = b.mean(axis=1), float(np.ptp(b, axis=1).max())
        if (np.linalg.norm(centre - np.asarray(r.centre)) < 0.5 * r.radius
                and size < 2.6 * r.radius):
            return True
    return False


def wash_zones(bodies: dict, rotors, fs: Freestream) -> tuple[dict, bool]:
    """({назва: поверхня}, чи різали на третини). Гвинти — не частини: лінії
    стартують біля них і «торкалися» б їх усі."""
    parts = {n: m for n, m in (bodies or {}).items()
             if m is not None and m.n_points and not _is_rotor(n, m, rotors)}
    if len(parts) >= 2:
        return parts, False
    if not parts:
        return {}, False
    body = next(iter(parts.values()))
    d = _unit(fs.direction)
    s = np.asarray(body.cell_centers().points, dtype=float) @ d
    lo, hi = float(s.min()), float(s.max())
    k = np.clip(((s - lo) / max(hi - lo, 1e-12) * 3).astype(int), 0, 2)
    out = {}
    for i, name in enumerate(THIRDS):
        ids = np.flatnonzero(k == i)
        if len(ids):
            out[name] = body.extract_cells(ids).extract_surface(
                algorithm="dataset_surface")
    return out, True


def _polylines(lines) -> list[np.ndarray]:
    """Індекси точок кожної полілінії."""
    conn = np.asarray(lines.lines)
    out, i = [], 0
    while i < len(conn):
        n = int(conn[i])
        out.append(conn[i + 1:i + 1 + n])
        i += n + 1
    return out


def prop_wash(volume, bodies: dict, fs: Freestream, rotors, *,
              near: float | None = None, length: float | None = None) -> WashReach:
    """Лінії від дисків гвинтів і те, яких частин тіла вони торкаються."""
    if "U" not in volume.point_data:
        raise ValueError(tr("The volume has no velocity U — there will be no wash lines."))
    rotors = list(rotors or [])
    if not rotors:
        raise ValueError(tr("The case has no propellers — there is nothing to start the wash from."))
    zones, split = wash_zones(bodies, rotors, fs)
    seeds = np.vstack([rotor_seeds(r, fs, rings=WASH_RINGS,
                                   per_ring=WASH_PER_RING, standoff=0.15)
                       for r in rotors])
    d = _unit(fs.direction)
    parts = [m for m in (bodies or {}).values() if m is not None and m.n_points]
    corners = np.vstack([np.asarray(m.points) for m in parts]) if parts \
        else np.asarray(volume.points)
    start = min(float(np.asarray(r.centre) @ d) for r in rotors)
    span = float(np.ptp(corners, axis=0).max())
    reach = float((corners @ d).max())
    lines = trace(volume, seeds, length=length or (reach - start + 0.4 * span))
    radius = float(np.mean([r.radius for r in rotors]))
    near = float(near if near is not None else 0.1 * radius)
    names = list(zones)
    poly = _polylines(lines) if lines.n_points else []
    tag = np.full(lines.n_points, -1, dtype=np.int32)
    hits = {n: 0 for n in names}
    sides = {n: [0, 0, 0] for n in names}
    up = _nearest_up(d)
    first_touch = []                     # [(лінія, частина, точка)]
    signed = []
    for name in names:
        z = zones[name]
        got = lines.compute_implicit_distance(z) if lines.n_points else None
        signed.append(np.asarray(got.point_data["implicit_distance"])
                      if got is not None else np.empty(0))
    for li, ids in enumerate(poly):
        events = []
        pts = np.asarray(lines.points)[ids]
        step = np.linalg.norm(np.diff(pts, axis=0), axis=1)
        for k, name in enumerate(names):
            sd = signed[k][ids]
            close = np.abs(sd) < near
            # Точки лінії стоять на кроці інтегрування, а він далеко від
            # стінки — сантиметри: лінія може перескочити частину між двома
            # точками, жодна з яких не ближча за `near`. Перетин видно за
            # зміною знака відстані. Знак сам по собі нічого не каже (у
            # патчів OpenFOAM нормалі всередину, а третини тіла — відкриті
            # поверхні), тож зміна рахується лише там, де відрізок справді
            # дістає до поверхні: обидва кінці ближчі, ніж його довжина.
            if len(sd) > 1:
                flip = (np.sign(sd[:-1]) != np.sign(sd[1:])) & (
                    np.maximum(np.abs(sd[:-1]), np.abs(sd[1:])) <= step + near)
                close[1:] |= flip
            close = np.flatnonzero(close)
            if len(close):
                events.append((int(close[0]), k))
                hits[name] += 1
                first_touch.append((k, ids[close[0]]))
        for start_at, k in sorted(events):
            tag[ids[start_at:]] = k
    # З якого боку торкнулася: нормаль найближчої грані в точці дотику.
    for k, name in enumerate(names):
        z = zones[name]
        pts = [lines.points[i] for kk, i in first_touch if kk == k]
        if not pts:
            continue
        n, _ = outward(z)
        loc = vtk.vtkStaticCellLocator()
        loc.SetDataSet(z)
        loc.BuildLocator()
        cp, cid, sid, d2 = [0.0, 0.0, 0.0], vtk.mutable(0), vtk.mutable(0), \
            vtk.mutable(0.0)
        for x in pts:
            loc.FindClosestPoint(list(map(float, x)), cp, cid, sid, d2)
            s = float(n[int(cid)] @ up)
            sides[name][0 if s > 0.3 else 1 if s < -0.3 else 2] += 1
    total = len(poly)
    lines.point_data[WASH] = tag
    shares = {n: (hits[n] / total if total else 0.0) for n in names}
    side_share = {n: tuple(c / max(sum(sides[n]), 1) for c in sides[n])
                  for n in names}
    return WashReach(lines, names, shares, side_share, total, near, split)


def wash_colors(count: int) -> list[str]:
    return [CASE_COLORS[i % len(CASE_COLORS)] for i in range(count)]


def wash_lut(count: int):
    """Мапа кольорів для WASH: −1 — сірий, далі частини по черзі."""
    from matplotlib.colors import ListedColormap
    return ListedColormap([UNTOUCHED] + wash_colors(count))


def wash_text(w: WashReach) -> list[tuple[str, str]]:
    """[(колір, рядок)] — по частині: скільки ліній дійшло й з якого боку."""
    out = []
    for colour, name in zip(wash_colors(len(w.zones)), w.zones):
        share = w.shares[name]
        top, bottom, side = w.sides.get(name, (0, 0, 0))
        text = tr("{name}: {v:.0f}% of lines", name=name, v=share * 100)
        if share > 0:
            bits = [f"{lbl} {v * 100:.0f}%" for lbl, v in
                    (("top", top), ("bottom", bottom), ("side", side)) if v > 0]
            text += "  (" + ", ".join(bits) + ")"
        out.append((colour, text))
    out.append((UNTOUCHED, tr("has not touched any part yet  ·  "
                              "“touched” — closer than {v:.0f} mm", v=w.near * 1000)))
    return out


class WashScene:
    """Лінії обдуву й частини тіла в їхніх кольорах — у будь-якому Plotter."""

    def __init__(self, plotter, wash: WashReach, zones: dict, *,
                 line_width: float = 2.5, legend: bool = True):
        self.plotter = plotter
        self.wash = wash
        # Мапери PyVista тримають дані слабким посиланням — тримаємо ми.
        self.zones = dict(zones)
        self._actors = []
        k = len(wash.zones)
        lut = wash_lut(k)
        for colour, name in zip(wash_colors(k), wash.zones):
            m = zones.get(name)
            if m is not None and m.n_points:
                tint = _mix(colour, BODY_COLOR, 0.55)
                self._actors.append(plotter.add_mesh(
                    m, color=tint, smooth_shading=True, ambient=0.25,
                    name=f"fs_wash_zone_{name}", show_scalar_bar=False,
                    reset_camera=False, render=False))
        # Решта (гвинти) — звичайним кольором тіла: вони не частина відповіді,
        # але без них незрозуміло, звідки лінії починаються.
        for i, (name, m) in enumerate(zones.items()):
            if name not in wash.zones and m is not None and m.n_points:
                self._actors.append(plotter.add_mesh(
                    m, color=BODY_COLOR, smooth_shading=True, ambient=0.25,
                    name=f"fs_wash_other_{i}", show_scalar_bar=False,
                    reset_camera=False, render=False))
        if wash.lines.n_points:
            self._actors.append(plotter.add_mesh(
                wash.lines, scalars=WASH, cmap=lut, clim=(-1.5, k - 0.5),
                n_colors=k + 1, line_width=line_width,
                render_lines_as_tubes=True, lighting=False, name="fs_wash",
                show_scalar_bar=False, reset_camera=False, render=False))
        if legend:
            for i, (colour, text) in enumerate(wash_text(wash)):
                self._actors.append(plotter.add_text(
                    text, position=(0.015, 0.90 - 0.04 * i), viewport=True,
                    font_size=10, color=colour, font_file=FONT,
                    name=f"fs_wash_legend_{i}", shadow=False, render=False))

    def close(self) -> None:
        for a in self._actors:
            try:
                self.plotter.remove_actor(a, render=False)
            except Exception:
                pass
        self._actors = []


def _mix(a: str, b: str, t: float) -> str:
    ca, cb = np.asarray(pv.Color(a).float_rgb), np.asarray(pv.Color(b).float_rgb)
    c = (1 - t) * ca + t * cb
    return "#%02x%02x%02x" % tuple(int(round(255 * x)) for x in c)


# --- шари поверх тіла у вікні ----------------------------------------------

class Overlay:
    """Кілька акторів, які прибираються разом: бульбашки, слід, лінії."""

    def __init__(self, plotter):
        self.plotter = plotter
        self.actors = []
        # Мапери PyVista тримають дані слабким посиланням: без цього списку
        # поверхня, порахована у функції, зникала б, щойно та повернеться.
        self.data = []
        self.notes: list[str] = []       # що сказати під екраном

    def blobs(self, b: Blobs, colour: str, opacity: float, name: str) -> None:
        if b.surface is None or not b.surface.n_points:
            return
        self.data.append(b.surface)
        self.actors.append(self.plotter.add_mesh(
            b.surface, color=colour, opacity=opacity, smooth_shading=True,
            specular=0.2, name=name, show_scalar_bar=False, reset_camera=False,
            render=False))

    def lines(self, lines, colour: str, width: float, name: str) -> None:
        if lines is None or not lines.n_points:
            return
        self.data.append(lines)
        a = self.plotter.add_mesh(lines, color=colour, line_width=width,
                                  lighting=False, name=name,
                                  show_scalar_bar=False, reset_camera=False,
                                  render=False)
        # Лінії, що лежать РІВНО на поверхні (vtkStreamTracer), без зсуву
        # глибини мерехтять, пробиваючись крізь грані через раз. Підняті
        # над поверхнею (foamrs, LIFTED) зсуву не потребують — і не повинні
        # його мати: зсув залежить від нахилу, і під косим кутом витягав
        # лінії з ІНШОГО боку тонкого крила крізь нього (виміряно: у 2–3
        # рази більше пікселів чужого боку), а з поворотом камери вони
        # то з'являлися, то зникали.
        if LIFTED not in lines.field_data:
            a.mapper.SetResolveCoincidentTopologyToPolygonOffset()
            a.mapper.SetRelativeCoincidentTopologyLineOffsetParameters(-1.0, -4.0)
        self.actors.append(a)

    def close(self) -> None:
        for a in self.actors:
            try:
                self.plotter.remove_actor(a, render=False)
            except Exception:
                pass
        self.actors = []
        self.data = []


def reverse_note(b: Blobs) -> str:
    if not b.cells:
        return tr("no reverse flow in the volume (faster than 2% U∞ backward — nowhere)")
    return (tr("reverse flow: ≈ {v:.3g} cm³ of air goes backward "
               "({cells} cells)", v=b.volume * 1e6, cells=b.cells))


def loads_note(loads: SurfaceLoads, fs: Freestream, a_ref: float = 0.0,
               rotors: SurfaceLoads | None = None) -> str:
    """Рядок про суму по поверхні — тим самим числом, що й Cd у forceCoeffs."""
    who = tr("airframe") if rotors is not None else tr("sum over the surface")
    text = (tr("{who}: drag {drag:.4g} N = pressure {pressure:.4g} + "
               "friction {friction:.4g}", who=who, drag=loads.drag, pressure=loads.pressure, friction=loads.friction))
    if a_ref > 0:
        text += f",  Cd = {loads.cd(fs.dynamic_pressure, a_ref):.5f}"
    if not loads.has_friction:
        text += tr("  (friction is not written in the case — pressure only)")
    if rotors is not None:
        text += (tr(";   propellers along the flow: {drag:+.4g} N (minus "
                    "= thrust)", drag=rotors.drag))
    return text


def separation_note(loads: SurfaceLoads) -> str:
    if not loads.has_friction:
        return (tr("cannot show separation on the surface: the moment has "
                "no wallShearStress (the function was not enabled in the "
                "run)"))
    share = loads.separated / loads.area if loads.area > 0 else 0.0
    return (tr("near-wall flow goes backward on {v:.1f}% of the surface "
               "({v2:.3g} cm²)", v=share * 100, v2=loads.separated * 1e4))


# --- кадри тіла --------------------------------------------------------------

def _screen_flow(plotter, d: np.ndarray) -> tuple:
    """Напрямок потоку в площині кадру (праворуч, угору)."""
    cam = plotter.camera
    look = _unit(np.asarray(cam.focal_point) - np.asarray(cam.position))
    up = _unit(cam.up)
    right = _unit(np.cross(look, up))
    return float(d @ right), float(d @ up)


def body_camera(plotter, bounds: tuple, fs: Freestream, view: str,
                size: tuple[int, int], *, behind: float = 0.15) -> float:
    """Камера на тіло: "top" — згори, "bottom" — знизу, "side" — збоку (усі
    паралельні, з масштабом), "iso" — 3D. Повертає метрів на піксель (0 у 3D).

    `behind` — скільки довжини тіла лишити за ним: для сліду більше."""
    b = np.asarray(bounds, dtype=float).reshape(3, 2)
    lo3, hi3 = b[:, 0], b[:, 1]
    span = float(max(np.ptp(b, axis=1).max(), 1e-9))
    d = _unit(fs.direction)
    reach = 10.0 * span
    if view in ("top", "bottom", "side"):
        if view == "side":
            plotter.view_xz()
            axis = 1
        else:
            plotter.view_xy(negative=(view == "bottom"))
            axis = 2
        plotter.camera.parallel_projection = True
        u, v = plane_axes(axis)
        pad = 0.08 * span
        rect = (lo3[u] - pad, hi3[u] + pad + behind * span * max(abs(d[u]), 0.0),
                lo3[v] - pad, hi3[v] + pad)
        return fit_camera(plotter, axis, 0.5 * (lo3[axis] + hi3[axis]), rect,
                          size, reach)
    c = 0.5 * (lo3 + hi3)
    e_up = _nearest_up(d)
    e_side = np.cross(e_up, d)
    cam = plotter.camera
    cam.focal_point = list(c + 0.1 * span * d)
    cam.position = list(c + span * (-0.9 * d + 0.8 * e_up - 1.2 * e_side))
    cam.up = list(e_up)
    cam.parallel_projection = False
    plotter.reset_camera(bounds=tuple(np.column_stack(
        [lo3 - 0.05 * span, hi3 + 0.05 * span]).ravel()))
    # reset_camera вписує кулю навколо коробки, і довге тонке тіло (крило,
    # фюзеляж) лягає в кадр смужкою посередині. Ближче — без обрізання кінців.
    cam.zoom(1.5)
    return 0.0


def render_body_frame(body, fs: Freestream, *, view: str, field: str | None = None,
                      limits: tuple | None = None, blobs: tuple = (),
                      lines: tuple = (), wash: "WashReach | None" = None,
                      zones: dict | None = None, size: tuple[int, int] = (1300, 700),
                      bg: str = "#000000", label: str = "", ss: int = 2,
                      behind: float = 0.15) -> Frame:
    """Кадр тіла: пофарбоване полем (SEP, DRAG) або рівним кольором, з
    бульбашками `blobs` ((Blobs, колір, прозорість), …) і лініями `lines`
    ((PolyData, колір, товщина), …)."""
    w, h = size
    pl = pv.Plotter(off_screen=True, window_size=[w * ss, h * ss])
    try:
        pl.set_background(bg)
        pl.enable_depth_peeling(number_of_peels=6)
        if wash is not None:
            WashScene(pl, wash, zones or {}, line_width=2.2 * ss, legend=False)
        elif field is not None and field in body.cell_data:
            style = body_field_style(field, body)
            if limits is not None:
                style["clim"] = limits
            # Обрис — бо «нічого не відбувається» на шкалі темне, як і тло:
            # без нього грані з нульовим внеском зливаються з чорним.
            pl.add_mesh(body, scalars=field, preference="cell", smooth_shading=False,
                        show_scalar_bar=False, ambient=0.3, diffuse=0.75,
                        silhouette=dict(color=INK_DIM, line_width=1.2 * ss),
                        name="fs_body", reset_camera=False, render=False, **style)
        else:
            pl.add_mesh(body, color=BODY_COLOR, smooth_shading=True, ambient=0.25,
                        name="fs_body", show_scalar_bar=False, reset_camera=False,
                        render=False)
        ov = Overlay(pl)
        for i, (b, colour, opacity) in enumerate(blobs):
            ov.blobs(b, colour, opacity, f"fs_blob_{i}")
        for i, (ln, colour, width) in enumerate(lines):
            ov.lines(ln, colour, width * ss, f"fs_lines_{i}")
        mpp = body_camera(pl, tuple(body.bounds), fs, view, (w, h), behind=behind)
        flow = _screen_flow(pl, _unit(fs.direction)) if mpp > 0 else (0.0, 0.0)
        shot = pl.screenshot(return_img=True)
    finally:
        pl.close()
    img = shot[:h * ss, :w * ss, :3].astype(np.float64)
    img = img.reshape(h, ss, w, ss, 3).mean(axis=(1, 3)).round().astype(np.uint8)
    return Frame(img, label, mpp, (), (), flow)


def _merged(bodies: dict):
    parts = [m for m in (bodies or {}).values() if m is not None and m.n_points]
    if not parts:
        raise ValueError(tr("The case has no body surface — nothing to show."))
    return parts[0].copy() if len(parts) == 1 else parts[0].merge(
        parts[1:], merge_points=False)


def stall_report(view: CaseView, *, name: str = "", path: str | None = None,
                 size: tuple[int, int] = (1000, 700), wall: bool = True,
                 reverse: bool = True, rotors=()) -> np.ndarray:
    """Відрив: тіло пофарбоване напрямком пристінкової течії, лінії на ньому,
    і червоні бульбашки зворотної течії — згори й у 3D."""
    fs = view.fs
    body, loads, _spin = part_loads(view.bodies, fs, rotors)
    notes = []
    blobs = []
    if reverse and view.volume is not None:
        rb = reverse_flow(view.volume, fs)
        blobs.append((rb, REVERSE_COLOR, 0.45))
        notes.append(reverse_note(rb))
    lines = []
    has_tau = loads is not None and loads.has_friction
    if has_tau:
        notes.insert(0, separation_note(loads))
        if wall and NEAR in body.point_data:
            lines.append((wall_lines(body), WALL_LINE_COLOR, 1.3))
    elif loads is not None:
        notes.insert(0, separation_note(loads))
    field = SEP if has_tau else None
    frames = [[render_body_frame(body, fs, view=v, field=field, blobs=tuple(blobs),
                                 lines=tuple(lines), size=size, label=lbl)
               for v, lbl in (("top", "top"), ("iso", "3D"))]]
    head = tr("{v}where the flow separates,   U∞ = {u_inf:g} m/s", v=f"{name} — " if name else "", u_inf=fs.u_inf)
    legend = [(REVERSE_COLOR, tr("red bubbles — reverse flow in the "
                                 "volume (U∥ < −{v:g}% U∞)", v=REVERSE_LEVEL * 100))]
    if lines:
        legend.append((INK_DIM,
                       tr("dark lines on the body — where the air near the "
                       "wall moves (like oil in a wind tunnel): where "
                       "they converge — separation line")))
    return compose(frames, title=head, subtitle="\n".join(notes),
                   bar=(SEP_SPEC, -1.0, 1.0) if field else None,
                   legend=tuple(legend), path=path)


def drag_report(view: CaseView, *, name: str = "", a_ref: float = 0.0,
                path: str | None = None, size: tuple[int, int] = (900, 700),
                loss: bool = True, rotors=()) -> np.ndarray:
    """Хто скільки дає опору: тіло згори й знизу, пофарбоване внеском граней,
    і слід втрат повного тиску за ним."""
    fs = view.fs
    body, loads, spin = part_loads(view.bodies, fs, rotors)
    if loads is None:
        raise ValueError(tr("The body has no face pressure — drag cannot be summed."))
    lim = drag_limits(body)
    blobs = []
    notes = [loads_note(loads, fs, a_ref, spin)]
    if loss and view.volume is not None:
        wake = loss_blobs(view.volume, fs, bodies=view.bodies)
        if wake.surface.n_points:
            blobs.append((wake, LOSS_COLOR, 0.2))
    # Згори й знизу — саме тіло: слід ліг би поверх і притлумив кольори.
    # Слід — окремим кадром у 3D, де видно, куди він тягнеться.
    row = [render_body_frame(body, fs, view=v, field=DRAG, limits=lim,
                             size=size, label=lbl)
           for v, lbl in (("top", "top"), ("bottom", "bottom"))]
    if blobs:
        row.append(render_body_frame(body, fs, view="iso", field=DRAG,
                                     limits=lim, blobs=tuple(blobs), size=size,
                                     label="3D, with wake"))
    frames = [row]
    head = tr("{v}where the drag comes from,   U∞ = {u_inf:g} m/s", v=f"{name} — " if name else "", u_inf=fs.u_inf)
    legend = []
    if blobs:
        legend.append((LOSS_COLOR, tr("light-blue shell — wake: the air "
                                      "has lost more than {v:g}% of its "
                                      "total pressure (this is where the "
                                      "drag work goes)", v=LOSS_LEVEL * 100)))
    return compose(frames, title=head, subtitle="\n".join(notes),
                   bar=(DRAG_SPEC, lim[0], lim[1]),
                   legend=tuple(legend), path=path)


def wash_report(view: CaseView, rotors, *, name: str = "",
                path: str | None = None, size: tuple[int, int] = (1000, 700),
                near: float | None = None) -> np.ndarray:
    """Чи дістає обдув гвинтів до хвоста, каналу, верху крила: лінії від
    дисків, пофарбовані частиною, якої вже торкнулися."""
    w = prop_wash(view.volume, view.bodies, view.fs, rotors, near=near)
    zones, _ = wash_zones(view.bodies, rotors, view.fs)
    rot = {n: m for n, m in view.bodies.items()
           if m is not None and m.n_points and _is_rotor(n, m, rotors)}
    body = _merged(view.bodies)
    frames = [render_body_frame(body, view.fs, view=v, wash=w,
                                zones={**zones, **rot}, size=size, label=lbl,
                                behind=0.3)
              for v, lbl in (("iso", "3D"), ("top", "top"))]
    head = tr("{v}where the propeller wash goes,   U∞ = {u_inf:g} m/s", v=f"{name} — " if name else "", u_inf=view.fs.u_inf)
    sub = (tr("{seeds} lines from the disks of {n_rotors} propeller(s), "
              "rings at {join} of the radius; line color — the part it "
              "has already touched", seeds=w.seeds, n_rotors=len(rotors), join=', '.join('%g' % r for r in WASH_RINGS)))
    if w.split:
        sub += tr("\nthe body is one patch, so it is divided into thirds along the flow")
    return compose([frames], title=head, subtitle=sub,
                   legend=tuple(wash_text(w)), path=path)


# ==========================================================================
# Історія сил
# ==========================================================================
#
# Той самий кадр відповідає на два питання: «чи зійшлося» і «чим кейси
# відрізняються». Тому на кожній панелі — одна величина, кейси накладені, а
# праворуч підсвічено ВІКНО, за яким береться середнє: людина бачить не лише
# число, а й те, по чому воно взяте. Числа й вирок («усталилося» / «коливання»
# / «не добіг») — ті самі, що й на вкладці «Результати»: це `coeff_verdict`,
# а не друга думка.
#
# Кольори — перші слоти палітри для темного тла з `validate_palette.js`
# (усі перевірки проходять на чорному, в тому числі попарно). Колір ніс би
# КЕЙС, а не місце в списку, тож кейс не перефарбовується, коли його знімають
# зі звіту. Штрих (суцільна, пунктир, крапки) несе ЧАСТИНУ — це друге
# кодування, яке рятує, коли колір не розрізнити.

CASE_COLORS = ("#3987e5", "#d95926", "#199e70", "#c98500")
PART_STYLES = ("-", (0, (5, 2)), (0, (1.5, 2)), (0, (5, 2, 1.5, 2)))
VERDICT_TEXT = {"ok": "settled", "osc": "oscillation", "drift": "did not finish",
                "few": "few points"}
GRID = "#2c2c2a"           # сітка й осі: суцільні волоски, майже тло
AXIS = "#383835"


@dataclass
class HistoryCase:
    """Один кейс на графіку історії: сумарні ряди й ряди по частинах."""
    label: str
    series: dict                                  # {"Cd": [(крок, значення)]}
    parts: dict = field(default_factory=dict)     # {частина: {"Cd": [...]}}
    steady: bool = True


def history_report(cases: list[HistoryCase], *, name: str = "",
                   quantities: tuple = ("Cd", "Cl"),
                   size: tuple[int, int] = (1400, 900), path: str | None = None,
                   dpi: int = 100) -> np.ndarray:
    """Коефіцієнти за ітераціями: сумарні, а якщо є — і по частинах."""
    from matplotlib.backends.backend_agg import FigureCanvasAgg
    from matplotlib.figure import Figure

    import fs_post as post

    names = {"Cd": "Cd — drag", "Cl": "Cl — lift",
             "CmPitch": "Cm — pitching moment"}
    panels = []          # (заголовок, [(номер кейса, підпис, штрих, точки)])
    for q in quantities:
        rows = [(i, "", PART_STYLES[0], c.series[q])
                for i, c in enumerate(cases) if len(c.series.get(q, ())) > 1]
        if rows:
            panels.append((tr("{q}, total", q=names.get(q, q)), rows))
    for q in quantities:
        rows = []
        for i, c in enumerate(cases):
            for k, part in enumerate(sorted(c.parts)):
                pts = c.parts[part].get(q, ())
                if len(pts) > 1:
                    rows.append((i, part, PART_STYLES[k % len(PART_STYLES)], pts))
        if rows and len({r[1] for r in rows}) > 1:
            panels.append((tr("{q}, by part", q=names.get(q, q)), rows))
    if not panels:
        raise ValueError(tr("No coefficient series: "
                         "postProcessing/forceCoeffs is empty or the "
                         "case has not run yet."))
    if len({c.steady for c in cases}) > 1:
        # Стаціонарний рахується ітераціями (сотні), нестаціонарний — секундами
        # (соті): на одній осі другий стискається в цятку біля нуля, і графік
        # виходить «правдоподібним», але нічого не показує.
        raise ValueError(tr("Steady and transient cases cannot go on one "
                         "axis: they have different units (iterations "
                         "and seconds)."))

    w, h = size
    fig = Figure(figsize=(w / dpi, h / dpi), dpi=dpi, facecolor="#000000")
    FigureCanvasAgg(fig)
    top = 0.13
    gap, bottom = 0.045, 0.085
    ph = (1.0 - top - bottom - gap * (len(panels) - 1)) / len(panels)
    fig.text(40 / w, 1 - 40 / h, tr("{v}forces by iteration", v=f"{name} — " if name else ""), color=INK, fontsize=15.5,
             fontweight="bold", va="top", ha="left")
    fig.text(40 / w, 1 - 74 / h,
             "light band on the right — the window used for the mean; "
             "solid — total, dashed — a single part",
             color=INK_DIM, fontsize=10.5, va="top", ha="left")

    for i, (title, rows) in enumerate(panels):
        y0 = 1.0 - top - (i + 1) * ph - i * gap
        ax = fig.add_axes([64 / w, y0, 1 - (64 + 420) / w, ph])
        ax.set_facecolor("#000000")
        ax.set_title(title, color=INK, fontsize=11.5, loc="left", pad=8)
        ax.grid(True, color=GRID, lw=0.8, ls="-")
        ax.set_axisbelow(True)
        for side in ("top", "right"):
            ax.spines[side].set_visible(False)
        for side in ("left", "bottom"):
            ax.spines[side].set_color(AXIS)
        ax.tick_params(colors=INK_DIM, labelsize=9.5, length=3, width=0.8)
        tails = []
        for i_case, part, style, pts in rows:
            c = cases[i_case]
            colour = CASE_COLORS[i_case % len(CASE_COLORS)]
            arr = np.asarray(pts, dtype=float)
            # Розійшовся прогін пише в файл nan/inf, і саме тоді історію й
            # хочуть побачити. Нескінченне відкидаємо й кажемо про це прямо,
            # а не падаємо на межах осі чи не називаємо «усталилося».
            finite = np.isfinite(arr).all(axis=1)
            diverged = not bool(finite.all())
            arr = arr[finite]
            if len(arr) < 2:
                continue
            cut_at = max(len(arr) - max(50, len(arr) // 4), 0)
            mean = float(arr[cut_at:, 1].mean())
            mark, _ = post.coeff_verdict(list(map(tuple, arr)), steady=c.steady)
            label = c.label + (f" · {part}" if part else "")
            verdict = (tr("diverged (nan/inf present)") if diverged
                       else VERDICT_TEXT.get(mark, mark))
            label += f":  {mean:+.4f},  {verdict}"
            ax.plot(arr[:, 0], arr[:, 1], color=colour, lw=1.7, ls=style,
                    label=label, solid_capstyle="round", solid_joinstyle="round")
            ax.axvspan(arr[cut_at, 0], arr[-1, 0], color=colour, alpha=0.12,
                       lw=0)
            tails.append(arr[len(arr) // 4:, 1])
        if not tails:
            ax.text(0.5, 0.5, "no finite values", color=INK_DIM,
                    ha="center", va="center", transform=ax.transAxes)
            continue
        both = np.concatenate(tails)
        lo, hi = float(both.min()), float(both.max())
        pad = max((hi - lo) * 0.15, 1e-6)
        ax.set_ylim(lo - pad, hi + pad)
        # Легенда — поза полем, праворуч: у полі вона лягала б на початковий
        # перехідний процес, якраз там, де крива найкрутіша.
        ax.legend(loc="center left", bbox_to_anchor=(1.015, 0.5), frameon=False,
                  fontsize=9.5, labelcolor=INK_DIM, handlelength=2.6)
        if i == len(panels) - 1:
            ax.set_xlabel(tr("iteration") if cases[0].steady else tr("time, s"),
                          color=INK_DIM, fontsize=10.5)
        else:
            ax.tick_params(labelbottom=False)

    canvas = fig.canvas
    canvas.draw()
    rgba = np.asarray(canvas.buffer_rgba()).copy()
    if path:
        fig.savefig(path, dpi=dpi, facecolor="#000000")
    return rgba
