"""Читання результатів OpenFOAM через vtkOpenFOAMReader (обгортка PyVista).

Читаємо реконструйований кейс із диска Windows. Для цього потрібен порожній
файл <назва>.foam у корені кейсу — це стандартний спосіб вказати рідеру на
каталог кейсу.
"""

from __future__ import annotations

import os
import re
from pathlib import Path

import numpy as np
import pyvista as pv

TIME_DIR = re.compile(r"^\d+(\.\d+)?$")


def ensure_foam_file(case_dir: str, name: str = "case") -> str:
    """Створює <name>.foam, якщо його ще немає, і повертає шлях."""
    path = Path(case_dir) / f"{name}.foam"
    if not path.exists():
        path.write_text("")
    return str(path)


def _time_entries(case_dir: str) -> list[tuple[float, str]]:
    """Часові каталоги як (момент, імʼя на диску), за зростанням моменту.

    Імʼя береться з диска, а не відновлюється з числа через «%g»: у кейсі з
    timePrecision 8–10 таке відновлення дає рядок, якого на диску нема.
    """
    out = []
    try:
        entries = os.listdir(case_dir)
    except OSError:
        return []
    for e in entries:
        if TIME_DIR.match(e) and os.path.isdir(os.path.join(case_dir, e)):
            t = float(e)
            if t > 0:
                out.append((t, e))
    return sorted(out)


def time_dirs(case_dir: str) -> list[float]:
    """Часові каталоги кейсу, окрім 0 і 0.orig."""
    return [t for t, _ in _time_entries(case_dir)]


def has_results(case_dir: str) -> bool:
    return bool(time_dirs(case_dir))


# --------------------------------------------------------------------------
# Чи відповідають поля сітці
# --------------------------------------------------------------------------
#
# Кейс може накопичити часові каталоги від різних прогонів, а сітка в
# constant/polyMesh завжди одна — від останнього. Поля попереднього прогону
# на ній не читаються: ParaView видає «Number of cells/points in mesh and
# field do not match», а vtkOpenFOAMReader повертає не те або нічого.
#
# І сітка, і поля носять свої розміри у власному заголовку, тому перевірка
# коштує кілька прочитаних кілобайтів, а не завантаження кейсу.

RE_NCELLS = re.compile(r"nCells\s*:\s*(\d+)")
RE_INTERNAL = re.compile(r"internalField\s+(uniform|nonuniform)\b")
RE_COUNT = re.compile(r"^\s*(\d+)\s*$", re.M)
HEADER_BYTES = 4096

FIELD_NAMES = ("U", "p", "k", "omega", "epsilon", "nut", "nuTilda")


def _head(path: str, limit: int = HEADER_BYTES) -> str:
    """Початок файлу як текст. Поля бувають бінарні, але заголовок у них
    завжди ASCII."""
    try:
        with open(path, "rb") as fh:
            return fh.read(limit).decode("utf-8", errors="replace")
    except OSError:
        return ""


def _poly_cells(poly_dir: str) -> int:
    """Скільки комірок у сітці з цього каталогу polyMesh. Нуль — не прочиталося."""
    for name in ("owner", "faces", "neighbour"):
        text = _head(os.path.join(poly_dir, name))
        m = RE_NCELLS.search(text)
        if m:
            return int(m.group(1))
    return 0


def mesh_cells(case_dir: str) -> int:
    """Скільки комірок у сітці кейсу. Нуль — сітки немає або не прочиталася."""
    return _poly_cells(os.path.join(case_dir, "constant", "polyMesh"))


def _mesh_sizes(case_dir: str, entries: list[tuple[float, str]]):
    """(момент, імʼя каталогу, комірок у сітці цього моменту) — одним проходом.

    Сітку OpenFOAM бере з ОСТАННЬОГО каталогу polyMesh, не пізнішого за
    момент, а коли такого нема — з constant. Читач ParaView робить так само. У
    звичайному кейсі сітка одна, constant. Але в кейсі з адаптацією чи рухомою
    сіткою polyMesh пишеться в кожен каталог, де сітка змінилася, і поля цього
    каталогу лежать на НІЙ, а не на constant.

    Нуль у третьому полі — сітка не прочиталася, і судити про поля не можна.
    """
    cells = mesh_cells(case_dir)
    for t, name in entries:
        poly = os.path.join(case_dir, name, "polyMesh")
        if os.path.isdir(poly):
            cells = _poly_cells(poly)
        yield t, name, cells


def mesh_cells_at(case_dir: str, time: float) -> int:
    """Скільки комірок у сітці, на якій лежать поля цього моменту часу."""
    cells = mesh_cells(case_dir)
    for t, _name, size in _mesh_sizes(case_dir, _time_entries(case_dir)):
        if t > time:
            break
        cells = size
    return cells


def field_size(path: str) -> int:
    """Скільки значень у полі: 0 — не прочиталося, -1 — однорідне поле.

    Однорідне (`internalField uniform ...`) годиться для будь-якої сітки:
    саме таким лежить початкове наближення в 0/.
    """
    text = _head(path)
    m = RE_INTERNAL.search(text)
    if not m:
        return 0
    if m.group(1) == "uniform":
        return -1
    tail = text[m.end():]
    # Кількість стоїть або тим самим рядком, або наступним — залежно від
    # того, як саме OpenFOAM переносив рядки при записі.
    same = re.match(r"\s+List<[^>]*>\s+(\d+)", tail)
    if same:
        return int(same.group(1))
    found = RE_COUNT.search(tail)
    return int(found.group(1)) if found else 0


def _folder_field_size(folder: str) -> int:
    """Розмір першого знайденого поля в цьому каталозі."""
    for name in FIELD_NAMES:
        path = os.path.join(folder, name)
        if os.path.isfile(path):
            size = field_size(path)
            if size != 0:
                return size
    return 0


def time_field_size(case_dir: str, time: float) -> int:
    """Розмір першого знайденого поля в часовому каталозі."""
    return _folder_field_size(os.path.join(case_dir, "%g" % time))


def _has_fields(folder: str) -> bool:
    """Чи лежить у каталозі хоч один файл поля. Самі polyMesh і uniform — це
    каталоги, а не результат. Якщо каталог не прочитався, не ховаємо його."""
    try:
        with os.scandir(folder) as it:
            return any(e.is_file() for e in it)
    except OSError:
        return True


# ЧОМУ ТУТ ВРАХОВАНА СІТКА КОЖНОГО МОМЕНТУ. Скарга: «відкрив прогін — його не
# видно». Кейс filled_holes1 (адаптація сітки): 44 часові каталоги, поля лише у
# двох — 0.0158407 і 0.0783998, по 1.37 млн значень. Решта 42 — самі polyMesh.
# У constant/polyMesh 381 097 комірок (початкова сітка), а в 0.0783998/polyMesh
# рівно 1 371 933, стільки ж, як у полях. Раніше поля порівнювалися лише з
# constant, тож ці два каталоги оголошувалися «чужими» й ховалися, а список
# складався з 42 порожніх; вибирався останній із них, на тілі не було жодного
# масиву, і екран лишався порожнім без слова пояснення.

def matching_times(case_dir: str) -> tuple[list[float], list[float]]:
    """(зрізи, які відповідають сітці; зрізи від інших сіток).

    Поля моменту звіряються з сіткою ЦЬОГО моменту (див. `_mesh_sizes`), а не
    лише з constant. Каталоги без жодного поля (лише polyMesh — так лишається
    кейс з адаптацією) результатом не є й не потрапляють ні в один список, якщо
    поля є бодай десь; а коли їх нема ніде, лишаємо все як було.

    Якщо сітку прочитати не вдалося, вважаємо придатними всі: краще показати
    результат, ніж відмовити через власну невпевненість.
    """
    entries = _time_entries(case_dir)
    good, stale = [], []
    for t, name, cells in _mesh_sizes(case_dir, entries):
        folder = os.path.join(case_dir, name)
        if not _has_fields(folder):
            continue
        size = _folder_field_size(folder)
        (good if cells <= 0 or size in (0, -1, cells) else stale).append(t)
    if not good and not stale:
        return [t for t, _ in entries], []
    return good, stale


# Які масиви потрібні ОБʼЄМУ, щоб показати зріз. Решта (k, omega та інші) тут
# не читається: вона подвоює і час читання, і памʼять, а зріз її не показує.
VOLUME_ARRAYS = ("U", "p", "nut")


class Results:
    """Лінива обгортка над OpenFOAMReader.

    Читачів ДВА, і це не примха. Поверхня тіла (Cp, yPlus, тертя) й обʼєм
    (зріз, лінії потоку) — різні дані, а обʼєм майже завжди в десятки разів
    більший: у кейсі на 2.5 млн комірок поверхня — кілька відсотків. Раніше
    один читач тягнув усе, і кожен показ тіла коштував читання обʼєму, якого
    ніхто не просив. Тепер поверхня читається без обʼєму, а обʼєм — лише
    коли ввімкнено зріз, і лише з тими масивами, які зріз показує.

    Дані поверхні від цього не змінилися ні на біт: обидва способи читання
    порівняно на кейсах у 60 тис. і 2.5 млн комірок — геометрія, точкові й
    комірчасті масиви кожного патча однакові.
    """

    def __init__(self, case_dir: str, name: str = "case"):
        self.case_dir = case_dir
        self.foam = ensure_foam_file(case_dir, name)
        self.reader = pv.OpenFOAMReader(self.foam)
        self.reader.enable_all_patch_arrays()
        self.reader.disable_patch_array("internalMesh")
        self._vreader: pv.OpenFOAMReader | None = None
        self._cache: dict[tuple[float, str], pv.DataSet] = {}
        # Моменти часу, для яких патчі вже прочитано й розкладено в _cache.
        self._scanned: set[float] = set()
        # Обʼєм тримаємо лише ОДИН — останнього моменту часу: гігабайт на
        # кожен переглянутий момент швидко не вміщається в памʼять.
        self._volume: tuple[float, pv.UnstructuredGrid | None] | None = None

    @property
    def times(self) -> list[float]:
        """Часові зрізи з результатами.

        Нуль відкидаємо: там лежить лише початкове наближення, а поля на
        кшталт Cp у ньому суцільні нулі й лише збивають з пантелику.
        """
        try:
            times = [float(t) for t in self.reader.time_values]
        except Exception:
            times = time_dirs(self.case_dir)
        return [t for t in times if t > 0] or times

    @staticmethod
    def _read_at(reader, time: float):
        # Час у вікні проходить через текст «%g» (6 значущих цифр), а в кейсі з
        # timePrecision 8–10 такий рядок не збігається з жодним каталогом. Тоді
        # брати «який був активний» — це показати іншу мить під чужим підписом,
        # тож беремо НАЙБЛИЖЧУ з наявних.
        try:
            have = list(reader.time_values)
            reader.set_active_time_value(
                min(have, key=lambda x: abs(x - time)) if have else time)
        except Exception:
            pass
        return reader.read()

    def _read(self, time: float):
        return self._read_at(self.reader, time)

    def _volume_reader(self) -> pv.OpenFOAMReader:
        if self._vreader is None:
            r = pv.OpenFOAMReader(self.foam)
            r.disable_all_patch_arrays()
            r.enable_patch_array("internalMesh")
            r.disable_all_cell_arrays()
            have = set(r.cell_array_names)
            for name in VOLUME_ARRAYS:
                if name in have:
                    r.enable_cell_array(name)
            self._vreader = r
        return self._vreader

    def internal(self, time: float) -> pv.UnstructuredGrid | None:
        """Обʼєм сітки з U, p, nut на цей момент часу."""
        if self._volume is not None and self._volume[0] == time:
            return self._volume[1]
        # Попередній обʼєм відпускаємо ДО читання нового: інакше на мить у
        # памʼяті лежали б обидва.
        self._volume = None
        try:
            grid = self._read_at(self._volume_reader(), time)["internalMesh"]
        except (KeyError, TypeError):
            grid = None
        self._volume = (time, grid)
        return grid

    def patch_names(self, time: float) -> list[str]:
        """Імена всіх патчів цього моменту часу (без шляху), у порядку файла."""
        self.patch(time, "")                 # прочитати й розкласти, якщо ще ні
        return [name for (t, name) in self._cache if t == time]

    def patch(self, time: float, patch: str) -> pv.PolyData | None:
        key = (time, patch)
        if key not in self._cache and time not in self._scanned:
            # Один прохід на момент часу — і розкласти ВСІ патчі. Тіл у кейсі
            # з роздільними патчами багато, а раніше кожне читалося окремо:
            # прохід по всіх файлах поля на кожну частину.
            # У «прочитано» момент потрапляє ПІСЛЯ читання: виняток посередині
            # не мусить запамʼятатися назавжди (старе читання пробувало знову).
            try:
                bnd = self._read(time)["boundary"]
                for nm in bnd.keys():
                    # Імʼя патча — це останній відрізок шляху; перший із
                    # однакових виграє, як і в пошуку по одному.
                    self._cache.setdefault((time, nm.rsplit("/", 1)[-1]),
                                           bnd[nm])
            except (KeyError, TypeError):
                pass
            self._scanned.add(time)
        return self._cache.get(key)

    @staticmethod
    def fields(mesh) -> list[str]:
        if mesh is None:
            return []
        names = list(mesh.point_data.keys()) + list(mesh.cell_data.keys())
        seen, out = set(), []
        for n in names:
            if n not in seen:
                seen.add(n)
                out.append(n)
        return out


def cp_scale(flow) -> tuple[float, float]:
    """Від чого віднімати тиск і на що ділити, щоб вийшов Cp.

    Два режими рахують у різних одиницях, і це не дрібниця:

      * simpleFoam і pimpleFoam тримають тиск КІНЕМАТИЧНИЙ, p/rho, і
        відлічують його від нуля. Густина в знаменнику не потрібна.
      * rhoSimpleFoam і rhoPimpleFoam тримають АБСОЛЮТНИЙ тиск у паскалях,
        близько 101325. Якщо поділити його на 0.5*U², вийде 7.9 замість
        приблизно нуля — тобто повна дурня замість Cp.

    Тому знаменник і початок відліку беруться звідси, з одного місця, а не
    вгадуються на кожному виклику.
    """
    u = float(getattr(flow, "u_inf", 0.0) or 0.0)
    if getattr(flow, "compressible", False):
        return float(getattr(flow, "p_inf", 0.0)), 0.5 * float(
            getattr(flow, "rho", 1.0)) * u * u
    return 0.0, 0.5 * u * u


def add_cp(mesh, flow):
    """Додає поле Cp = (p - p_inf) / q.

    Приймає саму конфігурацію течії, а не голу швидкість: інакше на
    стисливому режимі легко забути густину й тиск відліку — і Cp мовчки
    виходить у сім із гаком разів більшим, з правдоподібною картинкою й
    брехливою шкалою.
    """
    if mesh is None:
        return mesh
    p_ref, q = cp_scale(flow)
    if q <= 0:
        return mesh
    for store in (mesh.point_data, mesh.cell_data):
        if "p" in store:
            store["Cp"] = (np.asarray(store["p"]) - p_ref) / q
    return mesh


def add_umag(mesh):
    if mesh is None:
        return mesh
    for store in (mesh.point_data, mesh.cell_data):
        if "U" in store:
            store["Umag"] = np.linalg.norm(np.asarray(store["U"]), axis=1)
    return mesh


def slice_through(mesh, normal=(0, 1, 0), origin=None):
    if mesh is None:
        return None
    try:
        return mesh.slice(normal=normal, origin=origin or mesh.center)
    except Exception:
        return None


def _tail_mean(series, window: int = 50) -> np.ndarray | None:
    """Середнє останніх `window` записів ряду [(t, (числа…)), …]."""
    if not series:
        return None
    vals = np.array([v for _, v in series[-window:]], dtype=float)
    return vals.mean(axis=0) if vals.size else None


def engine_forces(cfg, probes: dict, window: int = 50) -> dict[str, dict]:
    """Сила кожного двигуна на апарат, виміряна на його кришках.

    Обʼєм двигуна в сітку не входить, тож баланс імпульсу для нього
    пишеться по самих кришках. Сила газу на конструкцію двигуна:

        F = Σ по кришках ( Σ phi·U  +  Σ (p − p∞)·Sf )

    де phi і Sf — назовні з домену, тобто В ДВИГУН. Перевірка знаком: на
    соплі струмінь виходить, phi < 0, і перший доданок дає силу ПРОТИ
    струменя — це тяга. На повітрозабірнику phi > 0, і сила йде за
    повітрям, у двигун — це опір забору (ram drag). Обидва знаки
    прибиті тестом.

    У нестисливому phi — обʼємний потік, тому імпульс множиться на ρ∞.
    Тиск приходить уже силою в ньютонах з функції `forces` на кришці —
    з відліком p∞ і з ρ, тож його не чіпаємо.

    Повертає {двигун: {"force": (x, y, z) Н, "mdot": кг/с крізь сопло,
    "thrust": складова вперед, Н}}. Двигуни без вимірів пропускаються.
    """
    flow = cfg.flow
    scale = 1.0 if flow.compressible else float(flow.rho)
    ahead = -np.asarray(flow.drag_dir, dtype=float)
    out: dict[str, dict] = {}
    if cfg.portal_mode:
        return _portal_forces(cfg, probes, ahead, window)
    for e in cfg.engines:
        ports = [(e.nozzle, e.nozzle_area)] + list(
            zip(e.intakes, e.intake_areas or (0.0,) * len(e.intakes)))
        total = np.zeros(3)
        mdot = None
        seen = False
        for patch, area in ports:
            got = probes.get(patch) or {}
            mom = _tail_mean(got.get("momentum"), window)
            prs = _tail_mean(got.get("pressure"), window)
            if mom is None or prs is None or mom.size != 3 or prs.size < 3:
                continue
            seen = True
            # Тиск приходить із функції `forces`: уже в ньютонах, уже з
            # відліком pRef = p∞ і вже помножений на ρ у нестисливому.
            # Перші три числа — повна сила (тиск + вʼязкість).
            total += scale * mom + prs[:3]
            if patch == e.nozzle:
                m = _tail_mean(got.get("mdot"), window)
                if m is not None and m.size:
                    # На соплі phi < 0: повітря йде в домен. Витрата — модуль.
                    mdot = abs(float(m[0])) * scale
        if not seen:
            continue
        out[e.name] = {"force": tuple(float(x) for x in total),
                       "mdot": mdot,
                       "thrust": float(total @ ahead)}
    return out


def _portal_forces(cfg, probes: dict, ahead, window: int) -> dict[str, dict]:
    """Сила порталів — реакція їхніх джерел імпульсу.

    Сопло вносить у повітря ṁ·Vₑ·n̂, і повітря штовхає апарат на стільки ж
    назад: −ṁ·Vₑ·n̂. Вхід забирає з повітря ṁ·⟨U⟩ — те, що в його шарі
    справді рухалося, — і апарат отримує +ṁ·⟨U⟩. Перше задане, друге
    виміряне (`volAverage` U у шарі входу).
    """
    import fs_case as fc
    out: dict[str, dict] = {}
    for e in cfg.engines:
        if not (e.nozzle and e.intakes):
            continue
        pt = fc.engine_point(e, cfg.flow)
        n = np.asarray(fc._unit(e.nozzle_normal), dtype=float)
        total = -e.eta * pt.mdot_nozzle * pt.v_exit * n
        seen = False
        for name, m in zip(e.intakes, pt.mdot_intakes):
            u = _tail_mean((probes.get(name) or {}).get(fc.PORTAL_PROBE),
                           window)
            if u is None or u.size != 3:
                continue
            seen = True
            total = total + m * u
        if not seen:
            continue
        out[e.name] = {"force": tuple(float(x) for x in total),
                       "mdot": pt.mdot_nozzle,
                       "thrust": float(total @ ahead)}
    return out


def disks_thrust(cfg) -> float:
    """Тяга гвинтів-дисків уперед, Н — задана, бо диск її й ставить.

    Диск штовхає повітря вздовж своєї осі, апарат отримує стільки ж назад
    — тобто вперед, якщо вісь дивиться за потоком.
    """
    drag = np.asarray(cfg.flow.drag_dir, dtype=float)
    total = 0.0
    for d in cfg.disks_on:
        a = np.asarray(d.axis, dtype=float)
        n = np.linalg.norm(a)
        if n > 0:
            total += d.thrust_now * float((a / n) @ drag)
    return total


def net_force_line(cfg, cd: float | None, engines: dict,
                   disks_thrust: float = 0.0) -> str:
    """«Чиста сила вперед = тяга − опір» одним рядком, з поясненням знака."""
    if cd is None or not (engines or disks_thrust):
        return ""
    q = 0.5 * float(cfg.flow.rho) * float(cfg.flow.u_inf) ** 2
    drag = cd * q * float(cfg.flow.a_ref)
    thrust = sum(v["thrust"] for v in engines.values()) + disks_thrust
    net = thrust - drag
    how = "розганяється" if net > 0 else "гальмує"
    return (f"Чиста сила вперед: тяга {thrust:.2f} Н − опір планера "
            f"{drag:.2f} Н = {net:+.2f} Н ({how}).")


def summarise_coeffs(series: dict[str, list[tuple[float, float]]],
                     window: int = 50) -> dict[str, tuple[float, float]]:
    """Середнє й розкид коефіцієнтів на останніх `window` ітераціях.

    Розкид — це головний індикатор: якщо Cd гуляє на відсотки, розрахунок
    ще не зійшовся, і одне «останнє значення» нічого не варте.
    """
    out = {}
    for name, pts in series.items():
        if not pts:
            continue
        vals = np.array([v for _, v in pts[-window:]])
        if vals.size == 0:
            continue
        out[name] = (float(vals.mean()), float(vals.max() - vals.min()))
    return out


# Пороги зняті з ПОМІРЯНИХ прогонів, а не вигадані. Той самий літак, дві
# сітки, стаціонарний розвʼязувач (зсув — між половинами хвоста):
#
#   0°, середня сітка   зсув 0.08%   розмах  0.9%   напрямок —     зійшлося
#   0°, дорога сітка    зсув 0.06%   розмах  1.1%   напрямок —     зійшлося
#  10°, середня, Cl     зсув 0.07%   розмах 61.7%   міняється 3/6  коливання
#  10°, дорога,  Cl     зсув 0.47%   розмах 79.6%   міняється 3/6  коливання
#  10°, середня, Cd     зсув 1.46%   розмах 45.0%   міняється 3/6  коливання
#  10°, дорога,  Cd     зсув 1.39%   розмах 61.9%   міняється 4/6  коливання
#
# Останні два рядки — та причина, через яку тут зʼявився третій порог.
# За самим зсувом обидва Cd виглядали як «прогін не добіг»: півтора
# відсотка це немало. Але середні по блоках у них ходять туди-сюди, і
# додаткові ітерації не дали б нічого.
DRIFT_LIMIT = 0.005          # 0.5% — менший зсув не вартий тривоги
RANGE_LIMIT = 0.10           # 10% розмаху: точки розвʼязку більше немає
MARCH_LIMIT = 0.75           # частка кроків в один бік, щоб це був ХІД


def coeff_verdict(pts: list[tuple[float, float]],
                  steady: bool = True) -> tuple[str, str]:
    """Чи можна брати середнє — і що робити, якщо ні.

    Розкид сам собою нічого не каже. Те саме число «0.35» означає дві різні
    біди, які лікуються протилежно:

      прогін ще ЇДЕ        — середнє рано брати, треба більше ітерацій;
      прогін КОЛИВАЄТЬСЯ   — більше ітерацій не дадуть нічого, бо точки
                             розвʼязку не існує; потрібен нестаціонарний.

    Розрізняються вони не розмахом, а тим, чи рухається САМЕ середнє: у
    коливання воно стоїть, у недобіглого прогону повзе. Тому тут дивимося
    на обидва числа, а не на одне.

    Для нестаціонарного прогону коливання — не біда, а сама фізика (зрив,
    доріжка Кармана), і казати про нього треба інакше.

    Повертає (ознака, пояснення); ознака — одне з "ok", "osc", "drift",
    "few".
    """
    if len(pts) < 20:
        return "few", "записів замало, щоб судити про збіжність"
    arr = np.array(pts, dtype=float)
    it, vals = arr[:, 0], arr[:, 1]
    cut = max(len(vals) - max(50, len(vals) // 4), 0)
    tail, t_it = vals[cut:], it[cut:]
    mean = float(tail.mean())
    scale = abs(mean) if abs(mean) > 1e-12 else 1.0
    rng = float(tail.max() - tail.min()) / scale

    # Наскільки середнє зсунулося — і, ОКРЕМО, чи воно при цьому йшло в
    # один бік. Другий доданок тут головний, і саме його я спершу не
    # міряв, через що обидва Cd на 10° отримали вирок «прогін не добіг».
    #
    # Самого зсуву мало. Повільне коливання, період якого не ділить хвіст
    # націло, дає такий самий зсув, як справжній хід, і жодна статистика
    # кінців їх не розрізнить. Відрізняє їх те, як ідуть середні по
    # послідовних блоках хвоста — у ходу вони марширують, у коливання
    # тупцюють. На тих самих прогонах (Cd, середні по семи блоках):
    #
    #   середня сітка   0.135  0.113  0.124  0.131  0.114  0.128  0.126
    #   дорога сітка    0.140  0.104  0.138  0.112  0.129  0.127  0.118
    #
    # Зсув кінців — півтора відсотка, а напрямок міняється чотири рази.
    # Це коливання, і казати «дайте більше ітерацій» тут шкідливо: їх
    # можна дати скільки завгодно, і не зміниться нічого.
    half = len(tail) // 2
    drift = abs(float(tail[half:].mean() - tail[:half].mean())) / scale
    steady_march = 0.0
    nb = 7
    if len(tail) >= nb * 2:
        edges = np.linspace(0, len(tail), nb + 1).astype(int)
        blocks = np.array([tail[edges[i]:edges[i + 1]].mean()
                           for i in range(nb)])
        diffs = np.diff(blocks)
        overall = blocks[-1] - blocks[0]
        if overall != 0 and diffs.size:
            same = float(np.count_nonzero(
                np.sign(diffs) == np.sign(overall))) / diffs.size
            steady_march = same

    marching = drift > DRIFT_LIMIT and steady_march >= MARCH_LIMIT
    oscillating = rng > RANGE_LIMIT
    moving = (f"середнє повзе в один бік ({drift * 100:.1f}% за хвіст, "
              f"{steady_march * 100:.0f}% кроків туди само)")

    # ХІД питається першим, і це важливо. Ранній прогін, який ще
    # розганяється, гуляє не менше за зірваний: той самий кейс на 90-й
    # ітерації дав розмах Cl у 41%, хоч до 700-ї спокійно зійшовся до
    # 0.9%. Якби першим питався розмах, програма радила б переходити на
    # нестаціонарний саме тоді, коли треба просто дорахувати.
    #
    # Безпечним цей порядок робить умова монотонності: розгін іде в один
    # бік, а коливання тупцює. Без неї хід-першим ловив би й коливання —
    # так у мене й вийшло, поки цієї умови не було.
    if marching:
        if steady:
            return "drift", f"{moving} — прогін не добіг, дайте більше ітерацій"
        return "drift", (f"{moving} — пусковий перехідний процес ще не згас, "
                         f"усереднювати рано")
    if oscillating:
        if steady:
            return "osc", (
                f"розмах {rng * 100:.0f}% від значення — стаціонарний "
                f"розвʼязувач дійшов до своєї межі: точки розвʼязку тут уже "
                f"немає, потрібен нестаціонарний")
        return "osc", (
            f"розмах {rng * 100:.0f}% — це коливання самої течії; "
            f"усереднюйте по цілих періодах")
    return "ok", f"усталилося (розмах {rng * 100:.1f}% від значення)"
