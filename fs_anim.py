"""Анімація нестаціонарного розвʼязку: кадри рахуються наперед.

Що анімується, вибирає людина галочками (Q33): тіло, зріз, лінії потоку.
Рахується рівно вибране (Q34): для кожного кадру — значення вибраних
полів на гранях тіла й на полігонах зрізу. Під час програвання лишається
тільки підставляти готові масиви в актори — тож модель можна вертіти, а
поле перемикати, не чекаючи.

ЧОМУ ТАК ШВИДКО. Геометрія — тіло й зріз — будується ОДИН раз. Зріз
памʼятає, з якої комірки сітки вирізано кожен свій полігон, і кадр — це
вибірка значень за цими номерами. Поля кадру читаються напряму з файлів
OpenFOAM (fs_fast: Rust або numpy), без повного розбору сітки через VTK:
на 1.4 млн комірок це 0.02 с на поле замість ≈ 2 с на момент.

Повільний шлях лишається для того, де готової відповідності комірок нема:
сітка, що рухається чи адаптується від кадру до кадру. Тоді кожен кадр
читається через VTK і ріжеться наново.

Зріз типово фіксований (Q35). «Рухомий зріз» — опція: тоді в памʼяті
лежать сирі поля кожного кадру по всьому обʼєму, і пересунути площину
можна без нового читання — ціною памʼяті.

Qt тут немає: модуль тестується без вікна.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import numpy as np
import pyvista as pv

import fs_fast
import fs_views as fv
from fs_i18n import tr

# Поля на тілі: ключ → (підпис шкали, симетрична шкала, звідки береться).
BODY_FIELDS = {
    "cp": ("Cp", True),
    "dp": ("p − p∞,  Па", True),
    "yplus": ("y+", False),
    "tau": ("|τ стінки|,  Па", False),
}
BODY_TITLES = {
    "cp": "Коефіцієнт тиску  Cp",
    "dp": "Тиск  p − p∞",
    "yplus": "y+ на стінці",
    "tau": "Тертя на стінці  |τ|",
}
# Поля зрізу — ті самі, що й у статичному зрізі, крім розміру комірки
# (він від часу не залежить).
SLICE_KEYS = tuple(s.key for s in fv.FIELDS if s.key != "cell")

# Скільки кадрів читати разом. Більше — краще навантажує ядра, але
# внутрішні поля кадрів лежать у памʼяті одночасно: на 1.4 млн комірок
# це ~70 МБ на кадр на поле U.
CHUNK = 4

CID = "fs_cid"          # номер комірки, з якої вирізано полігон зрізу
ANIM = "fs_anim"        # масив, який актори показують


@dataclass
class Plan:
    """Що підготувати: моменти часу й галочки."""
    case_dir: str
    times: list[str]                     # імена тек моментів, за порядком
    walls: list[str]                     # патчі тіла
    body_fields: list[str] = field(default_factory=lambda: ["cp"])
    slice_fields: list[str] = field(default_factory=lambda: ["dU"])
    body: bool = True
    slice: bool = True
    streams: bool = False
    moving: bool = False                 # рухомий зріз (Q35b)
    axis: int = 1
    pos: float | None = None


@dataclass
class Frames:
    """Готові кадри."""
    times: list[float]
    body: pv.PolyData | None = None
    body_vals: dict = field(default_factory=dict)    # ключ → [масив кадру]
    slices: list = field(default_factory=list)       # геометрія: 1 або по кадру
    slice_vals: dict = field(default_factory=dict)   # ключ → [масив кадру]
    raw: dict = field(default_factory=dict)          # рухомий зріз: сирі поля
    streams: list = field(default_factory=list)      # PolyData кадру
    limits: dict = field(default_factory=dict)       # ключ → (lo, hi)
    fast: bool = False                               # читали напряму з файлів
    notes: list = field(default_factory=list)
    axis: int = 1
    pos: float = 0.0
    volume: object = None                            # для рухомого зрізу

    @property
    def n(self) -> int:
        return len(self.times)

    def slice_at(self, i: int):
        if not self.slices:
            return None
        return self.slices[0] if len(self.slices) == 1 else self.slices[i]

    def nbytes(self) -> int:
        total = 0
        for store in (self.body_vals, self.slice_vals, self.raw):
            for arrs in store.values():
                total += sum(a.nbytes for a in arrs)
        total += sum(s.n_points * 12 + s.n_cells * 16 for s in self.streams)
        return total


# --------------------------------------------------------------------------
# Формули — ті самі, що в статичному кадрі (fv.values).
# --------------------------------------------------------------------------

def _dp(p: np.ndarray, fs) -> np.ndarray:
    return (p - fs.p_inf) if fs.compressible else p * fs.rho


def body_values(key: str, arrays: dict, fs) -> np.ndarray | None:
    """Значення поля на гранях тіла з сирих масивів граней."""
    if key in ("cp", "dp"):
        p = arrays.get("p")
        if p is None:
            return None
        dp = _dp(np.asarray(p, float), fs)
        if key == "dp":
            return dp
        q = fs.dynamic_pressure
        return dp / q if q > 0 else None
    if key == "yplus":
        y = arrays.get("yPlus")
        return None if y is None else np.asarray(y, float)
    if key == "tau":
        t = arrays.get("wallShearStress")
        if t is None:
            return None
        mag = np.linalg.norm(np.asarray(t, float).reshape(-1, 3), axis=1)
        # У нестисливому функція пише КІНЕМАТИЧНЕ тертя (м²/с²).
        return mag if fs.compressible else mag * fs.rho
    return None


def body_needs(keys) -> set[str]:
    need = set()
    for k in keys:
        need |= {"cp": {"p"}, "dp": {"p"}, "yplus": {"yPlus"},
                 "tau": {"wallShearStress"}}.get(k, set())
    return need


def slice_needs(keys) -> set[str]:
    need = set()
    for k in keys:
        need |= set(fv.spec_by_key(k).arrays)
    return need


class _Cells:
    """Мінімум того, що fv.values хоче від сітки: cell_data / point_data."""

    def __init__(self, cells: dict, n: int):
        self.cell_data = {k: np.asarray(v) for k, v in cells.items()}
        self.point_data = {}
        self.n_cells = n


# --------------------------------------------------------------------------
# Межі шкали — спільні на всі кадри.
# --------------------------------------------------------------------------

def common_limits(arrays: list, symmetric: bool, pct: float = 99.5,
                  floor: float | None = None, sample: int = 400_000):
    """Межі шкали з УСІХ кадрів разом.

    Окрема шкала на кадр — найгірше, що може бути в анімації: колір
    «дихав» би сам по собі, і бачити, як поле змінюється, було б не можна.
    """
    vals = [a[np.isfinite(a)] for a in arrays if a is not None and a.size]
    if not vals:
        return (0.0, 1.0)
    allv = np.concatenate(vals)
    if allv.size > sample:
        allv = allv[np.random.default_rng(0).choice(allv.size, sample,
                                                    replace=False)]
    if symmetric:
        h = float(np.percentile(np.abs(allv), pct)) or 1.0
        lo, hi, _ = fv.nice_limits(-h, h, True)
    else:
        lo = float(floor) if floor is not None else float(
            np.percentile(allv, 100 - pct))
        hi = float(np.percentile(allv, pct))
        if hi <= lo:
            hi = lo + 1.0
        lo, hi, _ = fv.nice_limits(lo, hi, False)
    return (float(lo), float(hi))


# --------------------------------------------------------------------------
# Підготовка.
# --------------------------------------------------------------------------

def _say(cb, text: str, frac: float) -> None:
    if cb is not None:
        cb(text, float(frac))


def _slice_geometry(volume, axis: int, pos: float):
    """Зріз обʼєму площиною з номерами комірок на полігонах."""
    vol = volume.copy(deep=False)
    vol.cell_data[CID] = np.arange(vol.n_cells, dtype=np.int64)
    normal = [0.0, 0.0, 0.0]
    normal[axis] = 1.0
    origin = list(vol.center)
    origin[axis] = pos
    sl = vol.slice(normal=normal, origin=origin, generate_triangles=False)
    keep = [k for k in sl.cell_data.keys() if k != CID]
    for k in keep:
        del sl.cell_data[k]
    for k in list(sl.point_data.keys()):
        del sl.point_data[k]
    return sl


def prepare(plan: Plan, results, fs, *, rotors=(), say=None,
            force_vtk: bool = False) -> Frames:
    """Кадри для `plan`. `results` — fs_post.Results цього кейсу."""
    case = plan.case_dir
    times = list(plan.times)
    if not times:
        raise ValueError("Не вибрано жодного моменту часу.")
    tvals = [float(t) for t in times]
    frames = Frames(times=tvals, axis=plan.axis)

    _say(say, "читаю сітку першого кадру", 0.0)
    volume = results.internal(tvals[0])
    if volume is None:
        raise ValueError(tr("У моменті {times} немає сітки з полями.", times=times[0]))
    n_cells = fs_fast.n_cells(case, times[0])
    moving = fs_fast.mesh_moves(case, times)
    fast = (not force_vtk and not moving and n_cells == volume.n_cells
            and all((Path(case) / t).is_dir() for t in times))
    frames.fast = fast
    if moving:
        frames.notes.append("Сітка змінюється від кадру до кадру (рух чи "
                            "адаптація) — кожен кадр читається повністю, "
                            "повільніше.")
    elif not fast and not force_vtk:
        frames.notes.append("Кількість комірок у полях і в сітці VTK "
                            "різна — читаю через VTK, повільніше.")

    # --- геометрія тіла ---------------------------------------------------
    walls = [w for w in plan.walls]
    body_parts, body_n = [], {}
    if plan.body and walls:
        for w in walls:
            m = results.patch(tvals[0], w)
            if m is not None and m.n_cells:
                clean = pv.PolyData(m.points.copy(), faces=m.faces.copy()) \
                    if isinstance(m, pv.PolyData) else m.extract_surface()
                body_parts.append(clean)
                body_n[w] = clean.n_cells
        if body_parts:
            frames.body = (body_parts[0] if len(body_parts) == 1 else
                           pv.merge(body_parts, merge_points=False))
    walls = [w for w in walls if w in body_n]
    owners = {}
    if fast and walls:
        try:
            owners = fs_fast.patch_owner_cells(case, walls, times[0])
            if any(owners.get(w) is None or owners[w].size != body_n[w]
                   for w in walls):
                frames.notes.append("Грані тіла у VTK і в сітці не "
                                    "збіглися — тіло читаю через VTK.")
                owners = None
        except (OSError, ValueError) as exc:
            frames.notes.append(tr("Власники граней не прочиталися ({exc}) — "
                                "тіло через VTK.", exc=exc))
            owners = None

    # --- геометрія зрізу --------------------------------------------------
    b = volume.bounds
    pos = plan.pos if plan.pos is not None else 0.5 * (
        b[2 * plan.axis] + b[2 * plan.axis + 1])
    frames.pos = float(pos)
    if plan.slice and plan.slice_fields and not moving:
        frames.slices = [_slice_geometry(volume, plan.axis, pos)]

    bneed = body_needs(plan.body_fields) if frames.body is not None else set()
    sneed = slice_needs(plan.slice_fields) if plan.slice else set()
    if plan.streams:
        sneed = sneed | {"U"}
    keep_raw = plan.moving and not moving
    if keep_raw:
        frames.volume = volume
    names = sorted(bneed | sneed)

    for k in plan.body_fields:
        frames.body_vals[k] = []
    for k in plan.slice_fields:
        frames.slice_vals[k] = []
    for k in sneed if keep_raw else ():
        frames.raw[k] = []

    def frame_from(internal: dict, patch_vals: dict, i: int, vol=None):
        # --- тіло
        if frames.body is not None and plan.body_fields:
            per = {}
            for name in bneed:
                pieces = []
                for w in walls:
                    pv_ = patch_vals.get((name, w))
                    if pv_ is None and owners and name in internal:
                        pv_ = np.asarray(internal[name])[owners[w]]
                    if pv_ is None:
                        pv_ = np.full(body_n[w] if np.ndim(
                            internal.get(name, 0)) < 2 else (body_n[w], 3),
                            np.nan)
                    pieces.append(np.asarray(pv_, float))
                per[name] = np.concatenate(pieces, axis=0) if pieces else None
            for k in plan.body_fields:
                v = body_values(k, per, fs)
                frames.body_vals[k].append(
                    None if v is None else v.astype(np.float32))
        # --- зріз
        if plan.slice and plan.slice_fields:
            sl = frames.slices[0] if frames.slices and not moving else (
                _slice_geometry(vol, plan.axis, pos) if vol is not None
                else None)
            if moving and sl is not None:
                frames.slices.append(sl)
            if sl is not None and sl.n_cells:
                cid = np.asarray(sl.cell_data[CID])
                cells = {n: np.asarray(internal[n])[cid] for n in sneed
                         if n in internal}
                for k in plan.slice_fields:
                    _, v = fv.values(_Cells(cells, cid.size),
                                     fv.spec_by_key(k), fs, "cell")
                    frames.slice_vals[k].append(
                        None if v is None else np.asarray(v, np.float32))
            else:
                for k in plan.slice_fields:
                    frames.slice_vals[k].append(None)
        if keep_raw:
            for n in frames.raw:
                frames.raw[n].append(np.asarray(internal[n], np.float32))
        # --- лінії потоку
        if plan.streams:
            vol_i = vol if vol is not None else volume
            u = internal.get("U")
            if u is not None:
                work = vol_i.copy(deep=False)
                work.cell_data["U"] = np.asarray(u, np.float32)
                pt = work.cell_data_to_point_data(pass_cell_data=False)
                bodies = {"body": frames.body} if frames.body is not None else {}
                try:
                    lines = fv.case_streamlines(pt, bodies, fs, rotors)
                except Exception as exc:                  # noqa: BLE001
                    frames.notes.append(tr("Лінії кадру {times}: {exc}", times=times[i], exc=exc))
                    lines = pv.PolyData()
                if lines.n_points and "U" in lines.point_data:
                    lines.point_data[ANIM] = np.linalg.norm(
                        lines.point_data["U"], axis=1).astype(np.float32)
                frames.streams.append(lines)
            else:
                frames.streams.append(pv.PolyData())

    total = len(times)
    if fast:
        patch_req = [(w, body_n[w]) for w in walls]
        for a in range(0, total, CHUNK):
            chunk = times[a:a + CHUNK]
            _say(say, tr("кадри {v}–{v2} з {total}", v=a + 1, v2=a + len(chunk), total=total),
                 a / max(total, 1))
            paths, index = [], []
            for t in chunk:
                for name in names:
                    p = Path(case) / t / name
                    if p.is_file():
                        paths.append(str(p))
                        index.append((t, name))
            got = fs_fast.read_fields(paths, n_cells, patch_req, True)
            by_t: dict = {t: ({}, {}) for t in chunk}
            for (t, name), (inner, pats) in zip(index, got):
                internal, patch_vals = by_t[t]
                internal[name] = inner
                for w, v in pats.items():
                    patch_vals[(name, w)] = v
            for j, t in enumerate(chunk):
                internal, patch_vals = by_t[t]
                frame_from(internal, patch_vals, a + j)
    else:
        for i, t in enumerate(tvals):
            _say(say, tr("кадр {v} з {total} (через VTK)", v=i + 1, total=total), i / max(total, 1))
            vol = results.internal(t)
            if vol is None:
                raise ValueError(tr("У моменті {times} немає полів.", times=times[i]))
            internal = {n: np.asarray(vol.cell_data[n]) for n in names
                        if n in vol.cell_data}
            patch_vals = {}
            for w in walls:
                pm = results.patch(t, w)
                if pm is None:
                    continue
                for n in bneed:
                    if n in pm.cell_data:
                        patch_vals[(n, w)] = np.asarray(pm.cell_data[n])
            frame_from(internal, patch_vals, i, vol if moving else None)

    _say(say, "шкали", 0.98)
    for k, arrs in frames.body_vals.items():
        sym = BODY_FIELDS[k][1]
        frames.limits["body:" + k] = common_limits(
            arrs, sym, floor=None if sym else 0.0)
    for k, arrs in frames.slice_vals.items():
        spec = fv.spec_by_key(k)
        frames.limits["slice:" + k] = common_limits(
            arrs, spec.kind == "diverging", pct=spec.pct, floor=spec.floor)
    if frames.streams:
        frames.limits["streams"] = common_limits(
            [s.point_data[ANIM] for s in frames.streams
             if s.n_points and ANIM in s.point_data], False, floor=0.0)
    _say(say, "готово", 1.0)
    return frames


def reslice(frames: Frames, pos: float, fs) -> None:
    """Рухомий зріз: нова площина без нового читання (сирі поля в памʼяті)."""
    if frames.volume is None or not frames.raw:
        raise ValueError("Кадри підготовлено без «рухомого зрізу».")
    sl = _slice_geometry(frames.volume, frames.axis, pos)
    frames.slices = [sl]
    frames.pos = float(pos)
    cid = np.asarray(sl.cell_data[CID])
    for k in list(frames.slice_vals):
        out = []
        for i in range(frames.n):
            cells = {n: frames.raw[n][i][cid] for n in frames.raw}
            _, v = fv.values(_Cells(cells, cid.size), fv.spec_by_key(k), fs,
                             "cell")
            out.append(None if v is None else np.asarray(v, np.float32))
        frames.slice_vals[k] = out


def estimate_bytes(plan: Plan, n_cells: int, n_slice: int,
                   n_faces: int) -> int:
    """Скільки памʼяті займуть кадри — показати ДО підготовки."""
    n = len(plan.times)
    per = 0
    if plan.body:
        per += n_faces * len(plan.body_fields) * 4
    if plan.slice:
        per += n_slice * len(plan.slice_fields) * 4
    if plan.moving:
        comps = sum(3 if a == "U" else 1 for a in slice_needs(plan.slice_fields))
        per += n_cells * comps * 4
    if plan.streams:
        per += 2_000_000          # грубо: ~100 тис. точок ліній на кадр
    return per * n


def time_dirs(case_dir: str) -> list[str]:
    """Імена тек моментів часу з полями (крім 0), за зростанням часу."""
    out = []
    for d in Path(case_dir).iterdir():
        if not d.is_dir():
            continue
        try:
            t = float(d.name)
        except ValueError:
            continue
        if t <= 0:
            continue
        if any((d / f).is_file() for f in ("U", "p")):
            out.append((t, d.name))
    return [name for _, name in sorted(out)]
