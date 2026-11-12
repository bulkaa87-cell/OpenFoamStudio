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
    "dp": ("p − p∞,  Pa", True),
    "yplus": ("y+", False),
    "tau": ("|τ wall|,  Pa", False),
}
BODY_TITLES = {
    "cp": "Pressure coefficient  Cp",
    "dp": "Pressure  p − p∞",
    "yplus": "y+ at the wall",
    "tau": "Wall friction  |τ|",
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
                total += sum(a.nbytes for a in arrs if a is not None)
        total += sum(s.n_points * 12 + s.n_cells * 16 for s in self.streams
                     if s is not None)
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


def read_times(case: str, time_names, names, n_cells: int,
               patches) -> list:
    """The arrays of some times: [({field: cell values},
    {(field, patch): face values}) for each time].

    The only place where the frames (prepare and the timeline player) read
    the field files of a time. A faster reader (a Rust OpenFOAM reader in
    foamrs, T42) can replace it here without a change to the player.
    `patches` — [(patch, face count)]. One call for some times reads their
    files in parallel (fs_fast).
    """
    time_names = list(time_names)
    paths, index = [], []
    for j, t in enumerate(time_names):
        for name in names:
            p = Path(case) / t / name
            if p.is_file():
                paths.append(str(p))
                index.append((j, name))
    out = [({}, {}) for _ in time_names]
    got = (fs_fast.read_fields(paths, n_cells, list(patches), True)
           if paths else [])
    for (j, name), (inner, pats) in zip(index, got):
        internal, patch_vals = out[j]
        internal[name] = inner
        for w, v in pats.items():
            patch_vals[(name, w)] = v
    return out


class Loader:
    """Frames one at a time: the geometry once, then the values of each frame.

    `prepare` loads all frames at once. The timeline player (T40) loads them
    in the background, in the order in which it will show them, and shows each
    frame as soon as it is here. The values go into `frames` at their index;
    a frame that is not loaded yet has None in each list.

    The volume is read only when a slice, lines or a moving slice need it: on
    2.4 million cells it is seconds and a gigabyte, and the body alone does
    not need it (the face owners give the cells of the body).
    """

    def __init__(self, plan: Plan, results, fs, *, rotors=(), say=None,
                 force_vtk: bool = False, cache=None):
        case = plan.case_dir
        # fs_timeline.FrameCache: values of frames, kept across field changes.
        self.cache = cache
        times = list(plan.times)
        if not times:
            raise ValueError(tr("No time moment is selected."))
        self.plan, self.results, self.fs = plan, results, fs
        self.rotors = tuple(rotors)
        self.case, self.names_t = case, times
        tvals = [float(t) for t in times]
        n = len(times)
        frames = self.frames = Frames(times=tvals, axis=plan.axis)
        self.done: set[int] = set()

        need_volume = bool((plan.slice and plan.slice_fields) or plan.streams
                           or plan.moving)
        volume = None
        if need_volume:
            _say(say, tr("reading the mesh of the first frame"), 0.0)
            volume = results.internal(tvals[0])
            if volume is None:
                raise ValueError(tr("The moment {times} has no mesh with fields.", times=times[0]))
        n_cells = fs_fast.n_cells(case, times[0])
        moving = fs_fast.mesh_moves(case, times)
        fast = (not force_vtk and not moving and n_cells > 0
                and (volume is None or n_cells == volume.n_cells)
                and all((Path(case) / t).is_dir() for t in times))
        frames.fast = fast
        if moving:
            frames.notes.append(tr("The mesh changes from frame to frame "
                                "(motion or adaptation) — each frame is read "
                                "in full, slower."))
        elif not fast and not force_vtk and volume is not None and \
                n_cells and n_cells != volume.n_cells:
            frames.notes.append(tr("The cell count in the fields and in the VTK "
                                "mesh is different — reading through VTK, "
                                "slower."))
        self.n_cells, self.moving, self.volume = n_cells, moving, volume

        # --- body geometry ----------------------------------------------------
        walls = list(plan.walls)
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
                    frames.notes.append(tr("The body faces in VTK and in the "
                                        "mesh did not match — reading the "
                                        "body through VTK."))
                    owners = None
            except (OSError, ValueError) as exc:
                frames.notes.append(tr("Face owners could not be read "
                                       "({exc}) — body through VTK.", exc=exc))
                owners = None
        self.walls, self.body_n, self.owners = walls, body_n, owners

        # --- slice geometry ---------------------------------------------------
        if volume is not None:
            b = volume.bounds
            pos = plan.pos if plan.pos is not None else 0.5 * (
                b[2 * plan.axis] + b[2 * plan.axis + 1])
        else:
            pos = plan.pos if plan.pos is not None else 0.0
        frames.pos = self.pos = float(pos)
        if plan.slice and plan.slice_fields and not moving and volume is not None:
            frames.slices = [_slice_geometry(volume, plan.axis, pos)]
        elif plan.slice and plan.slice_fields and moving:
            frames.slices = [None] * n

        self.bneed = (body_needs(plan.body_fields)
                      if frames.body is not None else set())
        self.sneed = slice_needs(plan.slice_fields) if plan.slice else set()
        if plan.streams:
            self.sneed = self.sneed | {"U"}
        self.keep_raw = plan.moving and not moving
        if self.keep_raw:
            frames.volume = volume
        self.names = sorted(self.bneed | self.sneed)

        for k in plan.body_fields:
            frames.body_vals[k] = [None] * n
        for k in plan.slice_fields:
            frames.slice_vals[k] = [None] * n
        for k in self.sneed if self.keep_raw else ():
            frames.raw[k] = [None] * n
        if plan.streams:
            frames.streams = [None] * n

    # --- cache ------------------------------------------------------------------

    @property
    def _cached(self) -> bool:
        return (self.cache is not None and not self.plan.streams
                and not self.keep_raw)

    def _ckey(self, kind: str, key: str, i: int):
        if kind == "body":
            geom = tuple(self.walls)
        else:
            geom = (self.plan.axis, round(self.pos, 9), self.moving)
        return (self.case, self.names_t[i], kind, key, geom)

    def _from_cache(self, i: int) -> bool:
        """Fill frame i from the cache. False if a value is not there."""
        if not self._cached:
            return False
        want = []
        if self.frames.body is not None:
            want += [("body", k) for k in self.plan.body_fields]
        if self.plan.slice and self.frames.slices and not self.moving:
            want += [("slice", k) for k in self.plan.slice_fields]
        keys = [self._ckey(kind, k, i) for kind, k in want]
        if not want or any(key not in self.cache for key in keys):
            return False
        for (kind, k), key in zip(want, keys):
            store = (self.frames.body_vals if kind == "body"
                     else self.frames.slice_vals)
            store[k][i] = self.cache.get(key)
        self.done.add(i)
        return True

    def _to_cache(self, i: int) -> None:
        if not self._cached:
            return
        if self.frames.body is not None:
            for k in self.plan.body_fields:
                self.cache.put(self._ckey("body", k, i),
                               self.frames.body_vals[k][i])
        if self.plan.slice and self.frames.slices and not self.moving:
            for k in self.plan.slice_fields:
                self.cache.put(self._ckey("slice", k, i),
                               self.frames.slice_vals[k][i])

    def refield(self, body_fields, slice_fields) -> "Loader | None":
        """The same geometry with other fields (no new read of the mesh).

        None when the geometry is not enough: a slice field and no slice
        geometry (the slice was off), lines, a moving slice.
        """
        import copy
        import dataclasses
        if self.plan.streams or self.keep_raw or self.moving:
            return None
        if slice_fields and not self.frames.slices:
            return None
        if body_fields and self.frames.body is None:
            return None
        new = copy.copy(self)
        new.plan = dataclasses.replace(
            self.plan, body_fields=list(body_fields),
            slice_fields=list(slice_fields), body=bool(body_fields),
            slice=bool(slice_fields))
        old = self.frames
        n = old.n
        fr = new.frames = Frames(times=list(old.times), body=old.body,
                                 slices=list(old.slices), fast=old.fast,
                                 notes=list(old.notes), axis=old.axis,
                                 pos=old.pos)
        new.bneed = body_needs(body_fields) if fr.body is not None else set()
        new.sneed = slice_needs(slice_fields) if slice_fields else set()
        new.names = sorted(new.bneed | new.sneed)
        for k in body_fields:
            fr.body_vals[k] = [None] * n
        for k in slice_fields:
            fr.slice_vals[k] = [None] * n
        new.done = set()
        return new

    # --- one frame ------------------------------------------------------------

    def _frame_from(self, internal: dict, patch_vals: dict, i: int, vol=None):
        plan, frames, fs = self.plan, self.frames, self.fs
        walls, owners, body_n = self.walls, self.owners, self.body_n
        # --- body
        if frames.body is not None and plan.body_fields:
            per = {}
            for name in self.bneed:
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
                frames.body_vals[k][i] = (
                    None if v is None else v.astype(np.float32))
        # --- slice
        if plan.slice and plan.slice_fields:
            if self.moving:
                sl = (_slice_geometry(vol, plan.axis, self.pos)
                      if vol is not None else None)
                if frames.slices:
                    frames.slices[i] = sl
            else:
                sl = frames.slices[0] if frames.slices else None
            if sl is not None and sl.n_cells:
                cid = np.asarray(sl.cell_data[CID])
                cells = {n: np.asarray(internal[n])[cid] for n in self.sneed
                         if n in internal}
                for k in plan.slice_fields:
                    _, v = fv.values(_Cells(cells, cid.size),
                                     fv.spec_by_key(k), fs, "cell")
                    frames.slice_vals[k][i] = (
                        None if v is None else np.asarray(v, np.float32))
        if self.keep_raw:
            for n in frames.raw:
                frames.raw[n][i] = np.asarray(internal[n], np.float32)
        # --- streamlines
        if plan.streams:
            vol_i = vol if vol is not None else self.volume
            u = internal.get("U")
            lines = pv.PolyData()
            if u is not None and vol_i is not None:
                work = vol_i.copy(deep=False)
                work.cell_data["U"] = np.asarray(u, np.float32)
                pt = work.cell_data_to_point_data(pass_cell_data=False)
                bodies = {"body": frames.body} if frames.body is not None else {}
                try:
                    lines = fv.case_streamlines(pt, bodies, fs, self.rotors)
                except Exception as exc:                  # noqa: BLE001
                    frames.notes.append(tr("Lines of frame {times}: {exc}", times=self.names_t[i], exc=exc))
                    lines = pv.PolyData()
                if lines.n_points and "U" in lines.point_data:
                    lines.point_data[ANIM] = np.linalg.norm(
                        lines.point_data["U"], axis=1).astype(np.float32)
            frames.streams[i] = lines
        self.done.add(i)
        self._to_cache(i)

    def load(self, idxs) -> None:
        """Load the frames `idxs` (indices into plan.times) that are not here."""
        idxs = [int(i) for i in idxs if int(i) not in self.done]
        idxs = [i for i in idxs if not self._from_cache(i)]
        if not idxs:
            return
        case, times = self.case, self.names_t
        if self.frames.fast:
            patch_req = [(w, self.body_n[w]) for w in self.walls]
            got = read_times(case, [times[i] for i in idxs], self.names,
                             self.n_cells, patch_req)
            for i, (internal, patch_vals) in zip(idxs, got):
                self._frame_from(internal, patch_vals, i)
            return
        results = self.results
        for i in idxs:
            t = self.frames.times[i]
            vol = None
            internal = {}
            if self.sneed or self.moving:
                vol = results.internal(t)
                if vol is None:
                    raise ValueError(tr("The moment {times} has no fields.", times=times[i]))
                internal = {n: np.asarray(vol.cell_data[n]) for n in self.names
                            if n in vol.cell_data}
            patch_vals = {}
            for w in self.walls:
                pm = results.patch(t, w)
                if pm is None:
                    continue
                if pm.n_cells != self.body_n[w]:
                    # Another face count: these values do not fit the
                    # geometry of the first frame.
                    continue
                for n in self.bneed:
                    if n in pm.cell_data:
                        patch_vals[(n, w)] = np.asarray(pm.cell_data[n])
            self._frame_from(internal, patch_vals, i,
                             vol if self.moving else None)

    def missing(self) -> list[int]:
        return [i for i in range(self.frames.n) if i not in self.done]

    def finish(self) -> Frames:
        """Common scale limits from the loaded frames."""
        set_limits(self.frames)
        return self.frames


def set_limits(frames: Frames) -> None:
    """Common scale limits of all frames that are loaded (None is skipped)."""
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
             if s is not None and s.n_points and ANIM in s.point_data],
            False, floor=0.0)


def prepare(plan: Plan, results, fs, *, rotors=(), say=None,
            force_vtk: bool = False) -> Frames:
    """Кадри для `plan`. `results` — fs_post.Results цього кейсу."""
    ld = Loader(plan, results, fs, rotors=rotors, say=say, force_vtk=force_vtk)
    total = ld.frames.n
    if ld.frames.fast:
        for a in range(0, total, CHUNK):
            n = min(CHUNK, total - a)
            _say(say, tr("frames {v}–{v2} of {total}", v=a + 1, v2=a + n, total=total),
                 a / max(total, 1))
            ld.load(range(a, a + n))
    else:
        for i in range(total):
            _say(say, tr("frame {v} of {total} (through VTK)", v=i + 1, total=total), i / max(total, 1))
            ld.load([i])
    _say(say, tr("scales"), 0.98)
    ld.finish()
    _say(say, "done", 1.0)
    return ld.frames


def reslice(frames: Frames, pos: float, fs) -> None:
    """Рухомий зріз: нова площина без нового читання (сирі поля в памʼяті)."""
    if frames.volume is None or not frames.raw:
        raise ValueError(tr("Frames were prepared without “moving slice”."))
    sl = _slice_geometry(frames.volume, frames.axis, pos)
    frames.slices = [sl]
    frames.pos = float(pos)
    cid = np.asarray(sl.cell_data[CID])
    for k in list(frames.slice_vals):
        out = []
        for i in range(frames.n):
            if any(frames.raw[n][i] is None for n in frames.raw):
                out.append(None)            # not loaded yet (player, T40)
                continue
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
    # One rule for the program: fs_timeline.time_folders (T51).
    import fs_timeline as ftl
    return [name for _, name in ftl.time_folders(case_dir, fields=("U", "p"))]
