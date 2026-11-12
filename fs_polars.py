"""Polars: Cl, Cd, Cm, L/D against the angle of attack, from saved cases.

This module has no Qt. The «Polars» window (fs_polarwin), the simple report
(fs_report) and a command line use the same code.

What it does:

* reads the force coefficients of each case from postProcessing, file by
  file (see `read_segments`);
* averages them over a window: steady - the last N iterations; transient -
  a time window (default: the last 25 % of the computed time);
* gives each point a convergence state (`Point.verdict`);
* reads the reference values that OpenFOAM really used (the header of
  coefficient.dat), and finds the differences between the cases;
* finds angle series in a case folder: `<base>_aoa<angle>` and the repeated
  runs `<base>_aoa<angle>_r2` (T43);
* writes the table as CSV and draws the plots with matplotlib (Agg).

The warm start of a transient run is a steady solver. Its iterations
(time 1, 2, 3, ...) go into the same postProcessing/forceCoeffs/0 folder as
the transient (time 0.00001 ...). If you merge the two files and sort by
time, the iterations come after the seconds, and "the last 50 records" are
the warm start, not the transient. Thus a file that has only whole-number
times is dropped when another file has fractional times: the rule of
`fs_backend.warm_start_files`, which `read_force_coeffs` (the Library, the
live plots, the history) also uses.
"""

from __future__ import annotations

import csv
import math
import os
import re
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np

import fs_backend as be
import fs_caseguard as guard
import fs_coeffs as coeffs
import fs_library as lib
import fs_physics as phys
import fs_post as post
import fs_i18n
from fs_i18n import tr

# Coefficients that the table shows (the names of the OpenFOAM columns).
COEFFS = coeffs.COEFFS
JUDGED = ("Cd", "Cl", "CmPitch")
# The spread of a coefficient near zero (Cl at 0°, Cm near trim) is not
# divided by the value itself, but by this floor. Else a Cl of 0.003 with a
# spread of 0.001 looks like an oscillation of 33 %.
FLOOR = {"Cl": 0.1, "Cd": 0.01, "CmPitch": 0.05, "Cs": 0.05}

STEADY_N = 50              # the window of the Library (post.summarise_coeffs)
TRANSIENT_FRAC = 0.25      # the last quarter of the computed time
# A transient mean that moves by more than this between the two halves of
# the window is not a mean yet.
TRANSIENT_DRIFT = 0.02
MIN_SAMPLES = 20

# Verdict ids (ASCII: they go into the CSV). The worst one wins.
ORDER = ("ok", "few", "osc", "drift", "none")

RE_SERIES = re.compile(r"^(?P<base>.+?)_aoa(?P<angle>[-+]?\d+(?:\.\d+)?)$")
RE_RES = re.compile(
    r"Solving for (\w+), Initial residual = ([0-9.eE+-]+)")
RE_HEADER_NUM = coeffs.RE_HEADER_NUM
RE_HEADER_VEC = coeffs.RE_HEADER_VEC


# ==========================================================================
# Reading the coefficient files
# ==========================================================================

# The reader is fs_coeffs (T51: one parser for fs_backend.read_force_coeffs
# and the polars). The names stay here for the callers of this module.
Segment = coeffs.Segment
_parse_dat = coeffs.parse_dat
read_segments = coeffs.read_segments


def merge(segments: list[Segment], steady: bool) -> tuple[dict, dict, bool]:
    """(series, meta, warm_dropped): the series of the run itself.

    `fs_coeffs.merge`: the newest record of a time wins, the warm start of a
    transient case is dropped (`steady` is not needed for it: a steady case
    has no fractional times).
    """
    return coeffs.merge(segments)


# ==========================================================================
# Averaging and convergence
# ==========================================================================

def _weights(t: np.ndarray) -> np.ndarray:
    """Time weights of samples with a variable time step (trapezoid)."""
    if t.size < 2:
        return np.ones_like(t)
    d = np.diff(t)
    w = np.zeros_like(t)
    w[:-1] += 0.5 * d
    w[1:] += 0.5 * d
    if not np.any(w > 0):
        return np.ones_like(t)
    return w


def _wstats(t: np.ndarray, v: np.ndarray, timed: bool) -> tuple[float, float]:
    if v.size == 0:
        return float("nan"), float("nan")
    w = _weights(t) if timed else np.ones_like(v)
    mean = float(np.average(v, weights=w))
    std = float(math.sqrt(max(np.average((v - mean) ** 2, weights=w), 0.0)))
    return mean, std


@dataclass
class Window:
    """Which records of a run give the mean."""
    steady: bool
    start: float = 0.0          # first time (or iteration) in the window
    end: float = 0.0
    count: int = 0              # records in the window
    run_start: float = 0.0      # first time of the run (transient)

    def text(self) -> str:
        if self.count == 0:
            return "—"
        if self.steady:
            return tr("iterations {a:g}…{b:g} ({n})", a=self.start,
                      b=self.end, n=self.count)
        return tr("t = {a:.4g}…{b:.4g} s ({n} steps)", a=self.start,
                  b=self.end, n=self.count)


def window_of(series: dict, steady: bool, *, steady_n: int = STEADY_N,
              frac: float = TRANSIENT_FRAC,
              t_from: float | None = None) -> Window:
    """The averaging window of a run. `t_from` (transient) overrides `frac`."""
    pts = series.get("Cd") or next(iter(series.values()), [])
    if not pts:
        return Window(steady)
    t = np.array([p[0] for p in pts], dtype=float)
    if steady:
        n = max(1, min(int(steady_n), t.size))
        return Window(True, float(t[-n]), float(t[-1]), n, float(t[0]))
    t0, t1 = float(t[0]), float(t[-1])
    if t_from is not None and t_from > 0:
        start = min(max(float(t_from), t0), t1)
    else:
        f = min(max(float(frac), 0.01), 1.0)
        start = t1 - f * (t1 - t0)
    n = int(np.count_nonzero(t >= start - 1e-15))
    return Window(False, start, t1, n, t0)


def _in_window(pts, win: Window) -> tuple[np.ndarray, np.ndarray]:
    arr = np.array(pts, dtype=float).reshape(-1, 2)
    if win.steady:
        arr = arr[-win.count:] if win.count else arr[:0]
    else:
        arr = arr[arr[:, 0] >= win.start - 1e-15]
    return arr[:, 0], arr[:, 1]


def transient_verdict(t: np.ndarray, v: np.ndarray,
                      floor: float = 0.0) -> tuple[str, str]:
    """Is the mean over the window a mean? Oscillation is the flow itself;
    a mean that still moves is not."""
    if v.size < MIN_SAMPLES:
        return "few", tr("too few time steps in the window ({n})", n=v.size)
    half = t[0] + 0.5 * (t[-1] - t[0])
    a, b = t < half, t >= half
    if a.sum() < 2 or b.sum() < 2:
        return "few", tr("too few time steps in the window ({n})", n=v.size)
    m1, _ = _wstats(t[a], v[a], True)
    m2, _ = _wstats(t[b], v[b], True)
    mean, std = _wstats(t, v, True)
    scale = max(abs(mean), floor, 1e-12)
    drift = abs(m2 - m1) / scale
    if drift > TRANSIENT_DRIFT:
        return "drift", tr("the mean moves by {v:.1f}% between the halves of "
                           "the window — start the window later or compute "
                           "longer", v=drift * 100)
    return "ok", tr("time mean, std {v:.1f}% of the value",
                    v=std / scale * 100)


def worst(verdicts) -> str:
    vs = [v for v in verdicts if v in ORDER]
    return max(vs, key=ORDER.index) if vs else "none"


VERDICT_TEXT = {
    "ok": "converged",
    "few": "too few records",
    "osc": "oscillates",
    "drift": "not converged",
    "none": "no coefficients",
}


def verdict_word(v: str) -> str:
    return tr(VERDICT_TEXT.get(v, v), ctx="polar")


def solver_residuals(case_dir: str | os.PathLike,
                     last: int = STEADY_N) -> tuple[dict, float]:
    """({field: initial residual of the last iteration}, max over the last N).

    The main solver log only (not potentialFoam, not the robust start or
    the warm start). ({}, nan) if there is no log.
    """
    logs = Path(case_dir) / "logs"
    if not logs.is_dir():
        return {}, float("nan")
    cand = [p for p in logs.glob("*Foam.log") if p.stem not in lib.SKIP_LOGS]
    if not cand:
        return {}, float("nan")
    path = max(cand, key=lambda p: p.stat().st_mtime)
    text = lib.tail_text(path)
    blocks = lib.RE_ITER.split(text)
    # split() with a group gives [head, t1, body1, t2, body2, ...]
    bodies = blocks[2::2]
    done = [b for b in bodies if "ExecutionTime" in b] or bodies
    rows = []
    for body in done[-max(1, last):]:
        res: dict[str, float] = {}
        for name, value in RE_RES.findall(body):
            if name not in res:
                try:
                    res[name] = float(value)
                except ValueError:
                    pass
        if res:
            rows.append(res)
    if not rows:
        return {}, float("nan")
    peak = max(max(r.values()) for r in rows)
    return rows[-1], float(peak)


# ==========================================================================
# One point of a polar
# ==========================================================================

@dataclass
class Ref:
    """The values that the coefficients are divided by."""
    a_ref: float = float("nan")      # m²
    l_ref: float = float("nan")      # m
    u_inf: float = float("nan")      # m/s
    rho: float = float("nan")        # kg/m³
    cofr: tuple = (float("nan"),) * 3
    source: str = ""                 # "coefficient.dat" / "studio.json"


@dataclass
class Point:
    name: str
    path: str
    alpha: float | None = None
    beta: float = 0.0
    steady: bool = True
    coeffs: dict = field(default_factory=dict)     # {"Cl": (mean, std)}
    verdicts: dict = field(default_factory=dict)   # {"Cl": (id, text)}
    window: Window = field(default_factory=lambda: Window(True))
    residuals: dict = field(default_factory=dict)
    residual_max: float = float("nan")
    residual_tol: float = float("nan")
    ref: Ref = field(default_factory=Ref)
    warm_dropped: bool = False
    notes: list = field(default_factory=list)
    info: object = None              # fs_library.CaseInfo
    series: dict = field(default_factory=dict)

    @property
    def verdict(self) -> str:
        if not self.coeffs:
            return "none"
        return worst(v[0] for k, v in self.verdicts.items() if k in JUDGED)

    @property
    def converged(self) -> bool:
        return self.verdict == "ok"

    def value(self, name: str) -> float | None:
        pair = self.coeffs.get(name)
        return None if pair is None else pair[0]

    def std(self, name: str) -> float | None:
        pair = self.coeffs.get(name)
        return None if pair is None else pair[1]

    @property
    def ld(self) -> float | None:
        cl, cd = self.value("Cl"), self.value("Cd")
        if cl is None or cd is None or abs(cd) < 1e-12:
            return None
        return cl / cd

    @property
    def ld_std(self) -> float | None:
        ld = self.ld
        cl, cd = self.coeffs.get("Cl"), self.coeffs.get("Cd")
        if ld is None or not cl or not cd:
            return None
        rl = cl[1] / abs(cl[0]) if abs(cl[0]) > 1e-12 else 0.0
        rd = cd[1] / abs(cd[0])
        return abs(ld) * math.hypot(rl, rd)

    def why(self) -> str:
        """Why the point is not converged: one line per coefficient."""
        out = []
        for name in JUDGED:
            v = self.verdicts.get(name)
            if v and v[0] != "ok":
                out.append(f"{name}: {v[1]}")
        return fs_i18n.join("\n", out)


def _alpha_from_dir(meta: dict) -> float | None:
    d = meta.get("dragDir")
    if not d or len(d) != 3:
        return None
    return math.degrees(math.atan2(d[2], d[0]))


def _rho_inf(case_dir: Path) -> float | None:
    try:
        text = (case_dir / "system" / "controlDict").read_text(
            encoding="utf-8", errors="replace")
    except OSError:
        return None
    m = re.search(r"rhoInf\s+([-+0-9.eE]+)\s*;", text)
    try:
        return float(m.group(1)) if m else None
    except ValueError:
        return None


def _close(a: float, b: float, rel: float = 1e-4) -> bool:
    if not (math.isfinite(a) and math.isfinite(b)):
        return True
    return math.isclose(a, b, rel_tol=rel, abs_tol=1e-12)


def read_point(case, *, steady_n: int = STEADY_N, frac: float = TRANSIENT_FRAC,
               t_from: float | None = None) -> Point:
    """One point from a case folder or a `fs_library.CaseInfo`."""
    info = case if isinstance(case, lib.CaseInfo) else lib.read_case(
        case, with_size=False, with_coeffs=False)
    root = Path(info.path)
    p = Point(info.name, info.path, info=info)
    cfg = lib.cfg_from_dict(info.cfg) if info.has_cfg else None
    segments = read_segments(root)
    steady = info.steady if info.has_cfg else (
        all(s.whole_times for s in segments) if segments else True)
    p.steady = steady
    series, meta, p.warm_dropped = merge(segments, steady)
    p.series = series

    # Angle: the settings, then the force directions, then the name.
    if cfg is not None:
        p.alpha, p.beta = float(cfg.flow.aoa_deg), float(cfg.flow.aos_deg)
    else:
        p.alpha = _alpha_from_dir(meta)
        if p.alpha is None:
            m = RE_SERIES.match(info.name)
            if m:
                p.alpha = float(m.group("angle"))
        if p.alpha is not None:
            p.notes.append(tr("no studio.json: the angle comes from the case "
                              "files"))

    # Reference values: what OpenFOAM used (the file header) wins.
    ref = Ref()
    if cfg is not None:
        f = cfg.flow
        ref = Ref(float(f.a_ref), float(f.l_ref), float(f.u_inf),
                  float(f.rho), tuple(float(x) for x in f.cofr), "studio.json")
    if meta:
        got = Ref(meta.get("Aref", ref.a_ref), meta.get("lRef", ref.l_ref),
                  meta.get("magUInf", ref.u_inf), ref.rho,
                  tuple(meta.get("CofR", ref.cofr)), "coefficient.dat")
        if cfg is not None and not (
                _close(got.a_ref, ref.a_ref) and _close(got.l_ref, ref.l_ref)
                and _close(got.u_inf, ref.u_inf)):
            p.notes.append(tr("the reference values in the coefficient file "
                              "differ from studio.json: the file is used"))
        ref = got
    if not math.isfinite(ref.rho):
        rho = _rho_inf(root)
        if rho is not None:
            ref.rho = rho
    p.ref = ref

    if not series:
        p.notes.append(tr("no coefficients in postProcessing/forceCoeffs"))
        return p

    p.window = win = window_of(series, steady, steady_n=steady_n, frac=frac,
                               t_from=t_from)
    for name in COEFFS:
        pts = series.get(name)
        if not pts:
            continue
        t, v = _in_window(pts, win)
        if v.size == 0:
            continue
        p.coeffs[name] = _wstats(t, v, not steady)
        if steady:
            p.verdicts[name] = post.coeff_verdict(pts, steady=True,
                                                  floor=FLOOR.get(name, 0.0))
        else:
            p.verdicts[name] = transient_verdict(t, v, FLOOR.get(name, 0.0))
    if p.warm_dropped:
        p.notes.append(tr("the warm start (steady iterations) is not in the "
                          "mean"))
    if steady:
        p.residuals, p.residual_max = solver_residuals(root, steady_n)
        if cfg is not None:
            p.residual_tol = float(cfg.solver.residual_tol)
    return p


def read_points(cases, **kw) -> list[Point]:
    """Points sorted by the angle (a point without an angle goes last)."""
    pts = [read_point(c, **kw) for c in cases]
    return sort_points(pts)


def sort_points(pts: list[Point]) -> list[Point]:
    return sorted(pts, key=lambda p: (p.alpha is None,
                                      p.alpha if p.alpha is not None else 0.0,
                                      p.name))


# ==========================================================================
# Do the cases belong on one polar?
# ==========================================================================

@dataclass
class Check:
    level: str          # "bad" — the numbers cannot be compared; "warn"
    text: str


def _differ(values, rel: float = 1e-4) -> bool:
    vals = [v for v in values if v is not None and math.isfinite(v)]
    return any(not math.isclose(v, vals[0], rel_tol=rel, abs_tol=1e-12)
               for v in vals[1:])


def _fmt_set(values, fmt="{:.5g}") -> str:
    seen = []
    for v in values:
        if v is None or not math.isfinite(v):
            continue
        s = fmt.format(v)
        if s not in seen:
            seen.append(s)
    return ", ".join(seen)


def checks(points: list[Point]) -> list[Check]:
    """What makes the points of one polar not comparable, or less so."""
    out: list[Check] = []
    if not points:
        return out
    refs = [p.ref for p in points]
    for label, get in (("Aref", lambda r: r.a_ref), ("lRef", lambda r: r.l_ref)):
        vals = [get(r) for r in refs]
        if _differ(vals):
            out.append(Check("bad", tr(
                "{what} differs between the cases ({vals}): the coefficients "
                "are divided by different values", what=label,
                vals=_fmt_set(vals))))
    for k, axis in enumerate("xyz"):
        vals = [r.cofr[k] if len(r.cofr) == 3 else None for r in refs]
        if _differ(vals):
            out.append(Check("bad", tr(
                "the moment center (CofR {axis}) differs between the cases: "
                "Cm cannot be compared", axis=axis)))
            break
    speeds = [r.u_inf for r in refs]
    if _differ(speeds, 1e-3):
        out.append(Check("warn", tr(
            "U∞ differs between the cases ({vals} m/s): the Reynolds numbers "
            "differ", vals=_fmt_set(speeds, "{:.4g}"))))
    rhos = [r.rho for r in refs]
    if _differ(rhos, 1e-3):
        out.append(Check("warn", tr(
            "ρ differs between the cases ({vals} kg/m³)",
            vals=_fmt_set(rhos, "{:.4g}"))))
    betas = [p.beta for p in points]
    if _differ(betas + [0.0], 1e-9) and max(abs(b) for b in betas) > 1e-6:
        out.append(Check("warn", tr(
            "some cases have a sideslip angle β: {vals}°",
            vals=_fmt_set(betas, "{:g}"))))
    if len({p.steady for p in points}) > 1:
        out.append(Check("warn", tr(
            "steady and transient runs are mixed")))
    models = {p.info.turbulence for p in points
              if p.info is not None and p.info.turbulence}
    if len(models) > 1:
        out.append(Check("warn", tr(
            "the turbulence models differ: {vals}",
            vals=", ".join(sorted(models)))))
    srcs = {os.path.basename(p.info.stl_source) for p in points
            if p.info is not None and p.info.stl_source}
    if len(srcs) > 1:
        out.append(Check("warn", tr(
            "the cases come from different geometry files: {vals}",
            vals=", ".join(sorted(srcs)))))
    cells = [p.info.cells for p in points if p.info is not None and p.info.cells]
    if cells and max(cells) > 1.2 * min(cells):
        out.append(Check("warn", tr(
            "the mesh size differs by more than 20 % ({a}…{b})",
            a=phys.fmt_cells(min(cells)), b=phys.fmt_cells(max(cells)))))
    angles = [round(p.alpha, 6) for p in points if p.alpha is not None]
    if len(angles) != len(set(angles)):
        out.append(Check("warn", tr(
            "two or more cases have the same angle of attack")))
    if any(p.alpha is None for p in points):
        out.append(Check("warn", tr(
            "the angle of attack of some cases is not known")))
    bad = [p.name for p in points if p.coeffs and not p.converged]
    if bad:
        out.append(Check("warn", tr(
            "not converged: {names} — open markers on the plots",
            names=", ".join(bad))))
    empty = [p.name for p in points if not p.coeffs]
    if empty:
        out.append(Check("warn", tr("no coefficients: {names}",
                                    names=", ".join(empty))))
    return out


def common_ref(points: list[Point]) -> Ref:
    """The reference values of the first point that has them."""
    for p in points:
        if math.isfinite(p.ref.a_ref):
            return p.ref
    return Ref()


# ==========================================================================
# Angle series in a case folder
# ==========================================================================

def rerun(name: str) -> tuple[str, int]:
    """(name of the first run, run number): «plane_aoa3_r2» -> («plane_aoa3», 2).

    T43: a repeated run of a case goes to «<name>_r2», «<name>_r3»… These
    are runs of ONE point, not new points.
    """
    base = guard.run_base(name or "")
    if base == (name or ""):
        return base, 1
    try:
        return base, int((name or "")[len(base) + 2:])
    except ValueError:
        return base, 1


def series_key(name: str):
    """(base, angle, run number) of `<base>_aoa<angle>[_r<n>]`, or None."""
    first, rep = rerun(name)
    m = RE_SERIES.match(first)
    if not m:
        return None
    try:
        angle = float(m.group("angle"))
    except ValueError:
        return None
    return m.group("base"), angle, rep


def latest_runs(infos) -> tuple[list, list]:
    """(latest, older): for each point only its newest run is «latest».

    A point is the name without «_r<n>». The highest run number wins; at
    the same number, the newer folder.
    """
    by: dict[str, list] = {}
    for info in infos:
        first, rep = rerun(info.name)
        by.setdefault(first.lower(), []).append((rep, getattr(info, "modified", 0.0), info))
    latest, older = [], []
    for runs in by.values():
        runs.sort(key=lambda r: (r[0], r[1]))
        latest.append(runs[-1][2])
        older.extend(r[2] for r in runs[:-1])
    order = {id(i): k for k, i in enumerate(infos)}
    latest.sort(key=lambda i: order[id(i)])
    older.sort(key=lambda i: order[id(i)])
    return latest, older


@dataclass
class Series:
    base: str
    members: list = field(default_factory=list)      # CaseInfo, by angle
    superseded: list = field(default_factory=list)   # older runs of an angle

    @property
    def angles(self) -> list[float]:
        return [series_key(c.name)[1] for c in self.members]


def find_series(infos) -> list[Series]:
    """Angle series: two or more angles with one base name.

    For each angle the run with the highest `_r<n>` is used; the older ones
    go to `superseded` (the window shows them, not selected).
    """
    groups: dict[str, dict[float, list]] = {}
    for info in infos:
        key = series_key(info.name)
        if key is None:
            continue
        base, angle, rep = key
        groups.setdefault(base, {}).setdefault(round(angle, 6), []).append(
            (rep, info))
    out = []
    for base, by_angle in groups.items():
        if len(by_angle) < 2:
            continue
        s = Series(base)
        for angle in sorted(by_angle):
            runs = sorted(by_angle[angle], key=lambda r: r[0])
            s.members.append(runs[-1][1])
            s.superseded.extend(r[1] for r in runs[:-1])
        out.append(s)
    out.sort(key=lambda s: s.base.lower())
    return out


# ==========================================================================
# Table and CSV
# ==========================================================================

def has_cs(points) -> bool:
    return any("Cs" in p.coeffs for p in points)


def csv_rows(points: list[Point]) -> tuple[list[str], list[list]]:
    """(header, rows). Column names are symbols and units: no translation."""
    cs = has_cs(points)
    head = ["case", "alpha_deg", "beta_deg", "Cl", "Cl_std", "Cd", "Cd_std",
            "Cm", "Cm_std", "L_D", "L_D_std"]
    if cs:
        head += ["Cs", "Cs_std"]
    head += ["converged", "verdict", "steady", "window_start", "window_end",
             "window_records", "residual_max", "Aref_m2", "lRef_m",
             "U_inf_m_s", "rho_kg_m3", "CofR_x", "CofR_y", "CofR_z", "path"]

    def num(x, fmt="%.6g"):
        return "" if x is None or not math.isfinite(x) else fmt % x

    rows = []
    for p in points:
        r = [p.name, num(p.alpha, "%g"), num(p.beta, "%g")]
        for n in ("Cl", "Cd", "CmPitch"):
            r += [num(p.value(n)), num(p.std(n))]
        r += [num(p.ld), num(p.ld_std)]
        if cs:
            r += [num(p.value("Cs")), num(p.std("Cs"))]
        w = p.window
        cofr = list(p.ref.cofr) + [float("nan")] * 3
        r += [1 if p.converged else 0, p.verdict, 1 if p.steady else 0,
              num(w.start, "%.8g") if w.count else "",
              num(w.end, "%.8g") if w.count else "", w.count,
              num(p.residual_max, "%.3e"), num(p.ref.a_ref), num(p.ref.l_ref),
              num(p.ref.u_inf), num(p.ref.rho), num(cofr[0]), num(cofr[1]),
              num(cofr[2]), p.path]
        rows.append(r)
    return head, rows


def write_csv(path: str | os.PathLike, points: list[Point]) -> str:
    """UTF-8 with a BOM (Excel then reads the names), comma, decimal point."""
    head, rows = csv_rows(points)
    path = str(path)
    with open(path, "w", encoding="utf-8-sig", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(head)
        w.writerows(rows)
    return path


def fmt(value, digits: int = 4) -> str:
    if value is None or not math.isfinite(value):
        return "—"
    return f"{value:.{digits}g}" if abs(value) >= 1e-3 or value == 0 else f"{value:.3e}"


# ==========================================================================
# Plots
# ==========================================================================

# Light chart surface, ink and the first three categorical slots of the
# validated reference palette (dataviz skill, light mode).
SURFACE = "#fcfcfb"
INK = "#0b0b0b"
INK_2 = "#52514e"
GRID = "#e4e3df"
COLORS = ("#2a78d6", "#eb6834", "#1baf7a")
OTHER = "#8a8985"

KINDS = ("cl", "cd", "cm", "polar", "ld")
TITLES = {
    "cl": "Lift  Cl(α)",
    "cd": "Drag  Cd(α)",
    "cm": "Pitching moment  Cm(α)",
    "polar": "Drag polar  Cl(Cd)",
    "ld": "Lift-to-drag ratio  L/D(α)",
}


@dataclass
class Group:
    label: str
    points: list


def groups_of(points: list[Point]) -> list[Group]:
    """One line per series base; cases without a series form one group."""
    by: dict[str, list] = {}
    for p in points:
        key = series_key(p.name)
        by.setdefault(key[0] if key else "", []).append(p)
    if len(by) == 1:
        return [Group("", sort_points(next(iter(by.values()))))]
    return [Group(k or tr("other cases"), sort_points(v))
            for k, v in sorted(by.items())]


def _xy(kind: str, p: Point):
    """(x, y, y_err) of a point on one plot, or None."""
    if kind == "polar":
        x, y = p.value("Cd"), p.value("Cl")
        return (x, y, p.std("Cl")) if x is not None and y is not None else None
    if p.alpha is None:
        return None
    y, e = {"cl": (p.value("Cl"), p.std("Cl")),
            "cd": (p.value("Cd"), p.std("Cd")),
            "cm": (p.value("CmPitch"), p.std("CmPitch")),
            "ld": (p.ld, p.ld_std)}[kind]
    return None if y is None else (p.alpha, y, e)


def draw(ax, kind: str, groups: list[Group], *, legend: bool = True) -> int:
    """One plot. Returns the number of drawn points."""
    ax.set_facecolor(SURFACE)
    for side in ("top", "right"):
        ax.spines[side].set_visible(False)
    for side in ("left", "bottom"):
        ax.spines[side].set_color(GRID)
    ax.tick_params(colors=INK_2, labelsize=9)
    ax.grid(True, color=GRID, lw=0.8)
    ax.set_axisbelow(True)
    ax.set_title(tr(TITLES[kind]), color=INK, fontsize=11, loc="left")
    ax.set_xlabel("Cd" if kind == "polar" else tr("α, °"), color=INK_2,
                  fontsize=9.5)
    ax.set_ylabel({"cl": "Cl", "cd": "Cd", "cm": "Cm", "polar": "Cl",
                   "ld": "L/D"}[kind], color=INK_2, fontsize=9.5)
    n = 0
    open_seen = False
    for i, g in enumerate(groups):
        colour = COLORS[i] if i < len(COLORS) else OTHER
        rows = [(p, _xy(kind, p)) for p in g.points]
        rows = [(p, xy) for p, xy in rows if xy is not None]
        if not rows:
            continue
        xs = [xy[0] for _, xy in rows]
        ys = [xy[1] for _, xy in rows]
        ax.plot(xs, ys, "-", color=colour, lw=2, label=g.label or None,
                zorder=2)
        for p, (x, y, e) in rows:
            filled = p.converged
            open_seen |= not filled
            if e and math.isfinite(e) and e > 0:
                ax.errorbar([x], [y], yerr=[e], fmt="none", ecolor=colour,
                            elinewidth=1, capsize=3, zorder=2)
            ax.plot([x], [y], "o", ms=7, color=colour, mec=colour, mew=1.6,
                    mfc=colour if filled else SURFACE, zorder=3)
            n += 1
    if kind == "polar":
        # A small label per point: on Cl(Cd) the angle is not on an axis.
        for g in groups:
            for p in g.points:
                xy = _xy(kind, p)
                if xy is not None and p.alpha is not None:
                    ax.annotate(f"{p.alpha:g}°", (xy[0], xy[1]),
                                textcoords="offset points", xytext=(6, -10),
                                fontsize=8, color=INK_2)
    if kind in ("cm", "cl") and n:
        ax.axhline(0.0, color=INK_2, lw=0.8, zorder=1)
    if legend:
        from matplotlib.lines import Line2D
        handles, labels = ax.get_legend_handles_labels()
        if open_seen:
            handles.append(Line2D([], [], ls="", marker="o", ms=7,
                                  color=INK_2, mfc=SURFACE, mew=1.6))
            labels.append(tr("not converged"))
        if handles:
            ax.legend(handles, labels, fontsize=8.5, frameon=False,
                      labelcolor=INK_2)
    return n


def figure(points: list[Point], kinds=KINDS, *, fig=None, cols: int = 3,
           title: str = ""):
    """All plots in one matplotlib Figure (Agg), a grid of `cols` columns."""
    from matplotlib.backends.backend_agg import FigureCanvasAgg
    from matplotlib.figure import Figure

    kinds = list(kinds)
    rows = max(1, math.ceil(len(kinds) / cols))
    if fig is None:
        fig = Figure(figsize=(4.6 * min(cols, len(kinds)), 3.6 * rows),
                     dpi=100, layout="constrained")
        FigureCanvasAgg(fig)
    fig.clear()
    fig.set_facecolor(SURFACE)
    groups = groups_of(points)
    for i, kind in enumerate(kinds):
        ax = fig.add_subplot(rows, min(cols, len(kinds)), i + 1)
        draw(ax, kind, groups, legend=(i == 0))
    if title:
        fig.suptitle(title, color=INK, fontsize=12.5, x=0.01, ha="left")
    return fig


def single(points: list[Point], kind: str, size=(6.0, 4.2)):
    """One plot as its own Figure (for the export and the report)."""
    from matplotlib.backends.backend_agg import FigureCanvasAgg
    from matplotlib.figure import Figure

    fig = Figure(figsize=size, dpi=100, layout="constrained")
    FigureCanvasAgg(fig)
    fig.set_facecolor(SURFACE)
    draw(fig.add_subplot(1, 1, 1), kind, groups_of(points))
    return fig


def save_plots(points: list[Point], folder: str | os.PathLike, stem: str = "polar",
               formats=("png", "svg")) -> list[str]:
    """Each plot and the grid of all plots, in each format. Returns the paths."""
    folder = Path(folder)
    folder.mkdir(parents=True, exist_ok=True)
    out = []
    for kind in KINDS:
        fig = single(points, kind)
        for ext in formats:
            path = folder / f"{stem}_{kind}.{ext}"
            fig.savefig(path, dpi=150, facecolor=SURFACE)
            out.append(str(path))
    fig = figure(points)
    for ext in formats:
        path = folder / f"{stem}_all.{ext}"
        fig.savefig(path, dpi=150, facecolor=SURFACE)
        out.append(str(path))
    return out
