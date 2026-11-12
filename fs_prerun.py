"""Pre-run (tuning): a short run of the same case before the main run.

What the pre-run does:

  1. It builds the SAME mesh as the main run. The mesh store keeps it
     (fs_backend.mesh_key), so the main run does not build it again. A
     coarser mesh would not help: the layer coverage, y+ and the Courant
     number all depend on the cell size, so values from a coarse mesh do
     not apply to the fine one.
  2. It runs the solver for a short time: PRE_ITERS iterations (steady) or
     PRE_STEPS time steps (transient), with the same processes.
  3. analyse() reads the logs and gives a Report: for each value, what was
     measured, the current value, the proposed value and why.

The window (fs_prerunwin) shows the Report. The person accepts, edits or
skips each value. This module changes nothing by itself: apply() writes
only the values that the person accepted.

What the pre-run tunes, and why only these:

  * layers (count, expansion, thicknesses) from the measured coverage, with
    the existing fs_advisor.layer_diagnosis (no second copy of that logic);
  * finalLayerThickness from the measured y+ (steady or warm-start only:
    a short transient has no developed boundary layer);
  * maxCo and nOuterCorrectors (transient) from a divergence or from the
    early signs of one. Only DOWN, in small steps: forty steps cannot show
    that a larger step is stable for hours;
  * the stable start (two stages) or a lower relaxation (steady) after a
    divergence;
  * nothing for the iteration count: see _steady_rows (measured: a trend
    from 200 iterations does not predict the plateau).

Information only (no value to change): the run time of the main run,
the settled time step, the checkMesh result, the convergence in the
pre-run. The core count is not tuned: one run gives one point, and a
scaling curve needs two or more.

No Qt here: the tests call all of this offline.
"""

from __future__ import annotations

import copy
import math
import re
from dataclasses import dataclass, field, replace

import fs_advisor as adv
import fs_backend as be
import fs_case as fc
import fs_yplus as fy
import fs_i18n
import fs_fmt as fmt
from fs_i18n import tr

# Length of the pre-run.
PRE_ITERS = 200         # steady: iterations
MIN_ITERS = 60          # steady: not shorter (the trend needs points)
PRE_STEPS = 40          # transient: time steps. Measured crashes of the
                        # user cases came at steps 11, 13 and 23.
PRE_WARM = 100          # warm start: steady iterations before the transient
GUARD_EXTRA = 5         # the step limit script acts this many steps later

# The file that limits a transient pre-run to PRE_STEPS time steps. It is
# in system/, so each backend copies it with the case.
LIMIT_FILE = "system/ofsPrerunLimit"

# sh, not bash: it must also work in a minimal container image. It passes
# the solver output through unchanged. After N «Time =» lines it asks the
# solver to stop (stopAt noWriteNow, the same way as the soft stop of
# fs_backend). The solver then exits with code 0 at the next time step.
LIMIT_SCRIPT = """#!/bin/sh
# OpenFOAM Studio pre-run: stop the solver after $1 time steps.
n=${1:-40}; c=0; s=""
while IFS= read -r line; do
  printf '%s\\n' "$line"
  case "$line" in
    "Time = "*)
      c=$((c+1))
      if [ -z "$s" ] && [ "$c" -ge "$n" ]; then
        sed -i -E 's/^([[:space:]]*stopAt[[:space:]]+)[A-Za-z]+;/\\1noWriteNow;/' system/controlDict
        s=1
      fi;;
  esac
done
"""

# Steps that the pre-run does not need: nobody looks at its fields.
_SKIP_STEPS = ("reconstructParMesh", "reconstructPar")

# Decisions.
CO_DIVERGED_FACTOR = 10.0   # Co max over maxCo × this: the step blew up
CO_OVERSHOOT = 1.5          # Co max regularly above maxCo × this: warning
CO_CUT_DIVERGED = 0.5       # maxCo × this after a divergence
CO_CUT_WARNING = 0.75       # maxCo × this after early signs only
CO_MIN = 0.5                # not lower: the cause is then elsewhere
CO_MIN_CENTRAL = 0.1        # rhoCentralFoam
OUTER_MAX = 3               # nOuterCorrectors: propose up to this
RELAX_STEP = 0.8            # relaxation × this after a steady divergence
RELAX_MIN = 0.5
ROBUST_DEFAULT = 300        # stable start iterations, as in SolverCfg
SLOPE_FLAT = -2e-4          # decades per iteration: flatter is a plateau
YPLUS_FACTOR = 1.5          # y+ off the aim by more than × this: propose
LIMITED_GROWTH = 2.0        # limited cells: last part over first part × this
BOUNDING_SHARE = 0.25       # bounding in more than this share of the steps

RE_RANK = re.compile(r"^\[\d+\]\s*")
RE_TIME = re.compile(r"^Time = (\S+)")
RE_CO = re.compile(r"^Courant Number mean: (\S+) max: (\S+)")
RE_DT = re.compile(r"^deltaT = (\S+)")
RE_RESID = re.compile(r"Solving for (\w+), Initial residual = ([^,]+),")
RE_CONT = re.compile(r"time step continuity errors : sum local = ([^,]+),")
RE_BOUND = re.compile(r"^bounding (\w+),")
RE_LIMITED = re.compile(r"LimitedCells=(\d+)")
RE_EXEC = re.compile(r"^ExecutionTime = (\S+) s\s+ClockTime = (\S+) s")
RE_YPLUS = re.compile(r"patch (\S+) y\+ : min = ([^,]+), max = ([^,]+), "
                      r"average = (\S+)")
# The banner «trapFpe: Floating point exception trapping enabled» is in
# EACH log. Only the signal itself is a crash.
RE_FPE = re.compile(r"Signal: Floating point exception|sigFpe::sigHandler|"
                    r"Floating point exception \(core dumped\)")
RE_FATAL = re.compile(r"FOAM FATAL (IO )?ERROR")


def _num(text: str) -> float:
    try:
        return float(text.strip().rstrip(","))
    except ValueError:
        return float("nan")


# --------------------------------------------------------------------------
# The log of one solver step
# --------------------------------------------------------------------------

@dataclass
class StepRec:
    """One time step (transient) or one iteration (steady)."""
    time: float
    dt: float = 0.0
    co_mean: float = 0.0
    co_max: float = 0.0
    res: dict = field(default_factory=dict)     # field -> worst initial
    cont: float = 0.0                           # continuity, sum local
    bounding: int = 0                           # «bounding k» lines
    limited: int = 0                            # limitTemperature cells
    exec_s: float = -1.0
    clock_s: float = -1.0


@dataclass
class SolverLog:
    steps: list = field(default_factory=list)
    first_co_max: float = 0.0       # Courant before the first step
    fatal: str = ""
    fpe: bool = False
    nan: bool = False
    converged: bool = False
    ended: bool = False             # the line «End»
    yplus: dict = field(default_factory=dict)   # patch -> (min, max, avg)

    @property
    def n(self) -> int:
        return len(self.steps)

    @property
    def crashed(self) -> bool:
        return bool(self.fpe or self.nan or self.fatal)


def parse_log(text: str) -> SolverLog:
    """Read a solver log (simpleFoam, rhoPimpleFoam ...) into a SolverLog."""
    out = SolverLog()
    cur: StepRec | None = None
    co = None
    dt = 0.0
    fatal_left = 0
    fatal: list[str] = []
    for raw in text.split("\n"):
        line = RE_RANK.sub("", raw).strip()
        if not line:
            continue
        if fatal_left:
            if not line.startswith(("From ", "in file", "FOAM exiting",
                                    "FOAM parallel")):
                if line not in fatal:
                    fatal.append(line.rstrip("."))
                fatal_left -= 1
            else:
                fatal_left = 0
            continue
        if RE_FATAL.search(line):
            fatal_left = 3
            continue
        if RE_FPE.search(line):
            out.fpe = True
            continue
        m = RE_CO.match(line)
        if m:
            co = (_num(m.group(1)), _num(m.group(2)))
            if not out.steps and cur is None:
                out.first_co_max = out.first_co_max or co[1]
            continue
        m = RE_DT.match(line)
        if m:
            dt = _num(m.group(1))
            continue
        m = RE_TIME.match(line)
        if m:
            cur = StepRec(time=_num(m.group(1)), dt=dt)
            if co is not None:
                cur.co_mean, cur.co_max = co
            co = None
            out.steps.append(cur)
            continue
        if line == "End":
            out.ended = True
            continue
        if "solution converged in" in line:
            out.converged = True
            continue
        m = RE_YPLUS.search(line)
        if m:
            out.yplus[m.group(1)] = (_num(m.group(2)), _num(m.group(3)),
                                     _num(m.group(4)))
            continue
        if cur is None:
            continue
        m = RE_RESID.search(line)
        if m:
            name = m.group(1)
            name = "U" if name in ("Ux", "Uy", "Uz") else name
            v = _num(m.group(2))
            if math.isnan(v) or math.isinf(v):
                out.nan = True
                continue
            cur.res[name] = max(cur.res.get(name, 0.0), v)
            continue
        m = RE_CONT.search(line)
        if m:
            v = _num(m.group(1))
            if math.isnan(v):
                out.nan = True
            else:
                cur.cont = max(cur.cont, v)
            continue
        m = RE_BOUND.match(line)
        if m:
            cur.bounding += 1
            continue
        if line.startswith("limitTemperature"):
            m = RE_LIMITED.search(line)
            if m:
                cur.limited += int(m.group(1))
            continue
        m = RE_EXEC.match(line)
        if m:
            cur.exec_s, cur.clock_s = _num(m.group(1)), _num(m.group(2))
    out.fatal = ". ".join(fatal[:3])
    return out


def _median(values):
    v = sorted(x for x in values if not math.isnan(x))
    if not v:
        return 0.0
    k = len(v) // 2
    return v[k] if len(v) % 2 else 0.5 * (v[k - 1] + v[k])


def seconds_per_step(log: SolverLog, last: int = 10) -> float:
    """Wall time of one step, from the last `last` steps (0 = unknown).

    ClockTime is whole seconds, so take the span over several steps and
    divide. If the span is zero, use ExecutionTime (CPU time).
    """
    pts = [s for s in log.steps if s.clock_s >= 0]
    if len(pts) < 3:
        return 0.0
    pts = pts[-(last + 1):]
    span = pts[-1].clock_s - pts[0].clock_s
    if span > 0:
        return span / (len(pts) - 1)
    span = pts[-1].exec_s - pts[0].exec_s
    return span / (len(pts) - 1) if span > 0 else 0.0


def residual_series(log: SolverLog) -> list[tuple[int, float]]:
    """(iteration number, worst initial residual of U and p)."""
    out = []
    for i, s in enumerate(log.steps, start=1):
        vals = [s.res[k] for k in ("U", "p", "p_rgh") if k in s.res]
        if vals:
            out.append((i, max(vals)))
    return out


def residual_trend(series, tol: float, offset: int = 0):
    """(predicted iteration to reach `tol` or None, slope, fitted last).

    A straight line through log10 of the residual over the last half of
    the series. The slope is in decades per iteration. None: the line is
    flat (a plateau) or the series is too short.
    """
    if len(series) < 30 or tol <= 0:
        return None, 0.0, 0.0
    part = series[len(series) // 2:]
    xs = [float(i) for i, _ in part]
    ys = [math.log10(max(r, 1e-30)) for _, r in part]
    n = len(xs)
    mx, my = sum(xs) / n, sum(ys) / n
    sxx = sum((x - mx) ** 2 for x in xs)
    if sxx <= 0:
        return None, 0.0, 0.0
    slope = sum((x - mx) * (y - my) for x, y in zip(xs, ys)) / sxx
    end = my + slope * (xs[-1] - mx)
    if slope >= SLOPE_FLAT:
        return None, slope, 10 ** end
    need = (math.log10(tol) - end) / slope
    return offset + int(xs[-1] + max(0.0, need)), slope, 10 ** end


# --------------------------------------------------------------------------
# The pre-run case and its steps
# --------------------------------------------------------------------------

def run_flags(cfg: fc.CaseCfg) -> tuple:
    """What decides the step list. It must be the same in the pre-run."""
    return (fc.solver_name(cfg), cfg.use_potential_init,
            cfg.solver.two_stage, cfg.warm_start, cfg.needs_topo_set,
            cfg.solver.steady)


def prerun_cfg(cfg: fc.CaseCfg) -> fc.CaseCfg:
    """A copy of `cfg` for the pre-run. Only solver lengths change.

    The mesh settings stay the same: then the pre-run mesh is the main
    run mesh (the same key in the mesh store).
    """
    pre = copy.deepcopy(cfg)
    s = pre.solver
    if s.steady:
        n = max(MIN_ITERS, min(PRE_ITERS, s.n_iterations // 3))
        n = min(n, s.n_iterations)
        if cfg.solver.two_stage:
            s.robust_iterations = max(1, min(s.robust_iterations, n // 2))
        s.n_iterations = n
        s.write_interval = n        # the last iteration writes (y+)
    elif cfg.warm_start:
        s.warm_iterations = max(1, min(s.warm_iterations, PRE_WARM))
    return pre


def why_not(cfg: fc.CaseCfg, backend_kind: str) -> str:
    """Why a pre-run cannot run here ("" = it can)."""
    if not cfg.solver.steady and backend_kind == "bluecfd":
        return tr("A transient pre-run needs a bash shell (WSL, Docker, a "
                  "server). blueCFD runs commands in cmd.exe.")
    if run_flags(prerun_cfg(cfg)) != run_flags(cfg):
        return tr("The pre-run would need other run steps than the main "
                  "run.")
    return ""


def limit_command(command: str, n: int = PRE_STEPS) -> str:
    """The solver step command, limited to `n` time steps (LIMIT_FILE)."""
    return (f"set -o pipefail; {{ {command} ; }} 2>&1 | "
            f"sh {LIMIT_FILE} {int(n)}")


def prerun_steps(steps: list, cfg: fc.CaseCfg, n: int = PRE_STEPS) -> list:
    """The steps of the main run, made into pre-run steps.

    Without reconstruct (nobody opens the pre-run fields). In a transient,
    the last solver step goes through the step limit.
    """
    out = [s for s in steps if s.name not in _SKIP_STEPS]
    if cfg.solver.steady:
        return out
    idx = [i for i, s in enumerate(out)
           if s.solver and not s.name.endswith(be.WARM_SUFFIX)]
    if idx:
        i = idx[-1]
        # The dictionaries stop the run at step n (prepare_case). The
        # script is the second guard, a few steps later.
        out[i] = replace(out[i], command=limit_command(out[i].command,
                                                         n + GUARD_EXTRA))
    return out


def start_text(cfg: fc.CaseCfg) -> str:
    """The first log line of a pre-run: what it does and how long."""
    pre = prerun_cfg(cfg)
    if cfg.solver.steady:
        length = tr("{n} iterations", n=pre.solver.n_iterations)
    else:
        length = tr("{n} time steps", n=PRE_STEPS)
        if cfg.warm_start:
            length = tr("{w} warm start iterations, then {n} time steps",
                        w=pre.solver.warm_iterations, n=PRE_STEPS)
    return tr("Pre-run (tuning): the same mesh, {length}, processes: {p}. "
              "After it, a window shows the proposed values. The main run "
              "starts only after your click.", length=length,
              p=cfg.solver.n_procs)


def write_limit_file(case_dir) -> None:
    from pathlib import Path
    p = Path(case_dir) / LIMIT_FILE
    p.write_text(LIMIT_SCRIPT, encoding="utf-8", newline="\n")


# Top-level entries of a controlDict (column 0). The function objects have
# their own indented writeControl / writeInterval: they stay as they are.
_TOP = {
    "stopAt": re.compile(r"^stopAt[ \t]+[A-Za-z]+;", re.M),
    "writeControl": re.compile(r"^writeControl[ \t]+[A-Za-z]+;", re.M),
    "writeInterval": re.compile(r"^writeInterval[ \t]+[^;\n]+;", re.M),
}
_TRANSIENT = re.compile(r"^adjustTimeStep[ \t]+", re.M)


def limit_control_dict(text: str, n: int = PRE_STEPS) -> str:
    """A transient controlDict that stops by itself after `n` time steps.

    `stopAt nextWrite` with a write each `n` time steps: the solver ends at
    step `n` (or at endTime, if that comes first). This is in the file
    BEFORE the start, so it does not depend on how fast OpenFOAM reads a
    changed controlDict during the run. Measured 2026-11-12: with 2.5 ms per
    step (cavity 20x20) the change of the step limit script came too late,
    and the run went to its endTime. The script stays as a second guard.
    """
    want = {"stopAt": "stopAt          nextWrite;",
            "writeControl": "writeControl    timeStep;",
            "writeInterval": f"writeInterval   {int(n)};"}
    for key, rx in _TOP.items():
        text, count = rx.subn(want[key], text)
        if count != 1:
            raise ValueError(f"controlDict: {count} top-level {key} entries")
    return text


def prepare_case(case_dir, cfg: fc.CaseCfg, n: int = PRE_STEPS) -> list:
    """Make the written case a pre-run case. Returns the changed files.

    Transient: each transient controlDict (system/controlDict, or
    system/controlDict.stage2 of a warm start) stops after `n` time
    steps, and the step limit script is written as a second guard.
    Steady: nothing to do, the pre-run copy already has endTime = its
    iteration count (prerun_cfg).
    """
    from pathlib import Path
    root = Path(case_dir)
    if cfg.solver.steady:
        remove_limit_file(root)
        return []
    changed = []
    for rel in ("system/controlDict", "system/controlDict.stage2"):
        f = root / rel
        if not f.is_file():
            continue
        text = f.read_text(encoding="utf-8")
        if not _TRANSIENT.search(text):
            continue                    # the steady warm-start stage
        f.write_text(limit_control_dict(text, n), encoding="utf-8",
                     newline="\n")
        changed.append(rel)
    write_limit_file(root)
    return changed


def remove_limit_file(case_dir) -> None:
    from pathlib import Path
    (Path(case_dir) / LIMIT_FILE).unlink(missing_ok=True)


def log_names(cfg: fc.CaseCfg) -> dict[str, str]:
    """role -> step id of the solver logs: "main", "stable", "warm"."""
    solver = fc.solver_name(cfg)
    out = {"main": solver}
    if cfg.solver.two_stage:
        out["stable"] = solver + be.STABLE_SUFFIX
    if cfg.warm_start:
        out["warm"] = fc.SOLVER_OF[(cfg.flow.regime, True)] + be.WARM_SUFFIX
    return out


# --------------------------------------------------------------------------
# The report
# --------------------------------------------------------------------------

@dataclass
class Row:
    """One line of the report.

    A row with `kind` ("float" or "int") proposes a value for the field
    `key` ("solver.max_courant"). A row without `kind` is information.
    """
    key: str
    title: str
    measured: str = ""
    current: object = None
    proposed: object = None
    why: str = ""
    level: str = "info"         # info | warn | bad
    kind: str = ""
    lo: float = 0.0
    hi: float = 1e9
    decimals: int = 2
    step: float = 0.1

    @property
    def change(self) -> bool:
        return bool(self.kind) and self.proposed is not None \
            and self.proposed != self.current


@dataclass
class Report:
    outcome: str = "ok"         # ok | warn | diverged | failed
    headline: str = ""
    rows: list = field(default_factory=list)
    notes: list = field(default_factory=list)
    mesh_locked: bool = False
    can_start: bool = True

    @property
    def changes(self) -> list:
        return [r for r in self.rows if r.change]


@dataclass
class RunFacts:
    """What the window knows about the finished pre-run."""
    ok: bool = True
    msg: str = ""
    step: str = ""              # id of the step at the end
    cells: int = 0
    coverage: float = -1.0      # snappy «Added N out of M cells» (-1 = none)
    faces: float = -1.0         # snappy «Extruding N out of M faces» (T38)
    mesh_level: str = "ok"      # RunState.mesh_severity
    mesh_why: str = ""
    mesh_from_store: bool = False
    mesh_cache: bool = True     # the backend keeps meshes


# One format of a time span for the program (fs_fmt, T51).
fmt_seconds = fmt.duration


def _nice(v: float, step: float) -> float:
    """Round down to `step` (0.25 → 1.75), not below one step."""
    return max(step, math.floor(v / step + 1e-9) * step)


def _g(v: float) -> str:
    return f"{v:.3g}"


def _share(part: list, test) -> float:
    return sum(1 for s in part if test(s)) / len(part) if part else 0.0


def transient_signs(log: SolverLog, max_co: float) -> list[str]:
    """Early signs of a divergence in a transient log that did not crash."""
    st = log.steps
    if len(st) < 8:
        return []
    k = max(3, len(st) // 4)
    head, tail = st[:k], st[-k:]
    out = []
    lh = sum(s.limited for s in head) / k
    lt = sum(s.limited for s in tail) / k
    if lt > 0 and lt > LIMITED_GROWTH * max(lh, 1.0):
        out.append(tr("limitTemperature clips more and more cells: "
                      "{a:.0f} → {b:.0f} per step", a=lh, b=lt))
    b = _share(st, lambda s: s.bounding > 0)
    if b > BOUNDING_SHARE:
        out.append(tr("“bounding” (a negative turbulence value) in {p:.0f}% "
                      "of the steps", p=100 * b))
    over = _share(tail, lambda s: s.co_max > CO_OVERSHOOT * max_co)
    if over > 0.5:
        out.append(tr("the Courant number stays above {v:g} (maxCo {m:g}): "
                      "the time step does not hold it",
                      v=CO_OVERSHOOT * max_co, m=max_co))
    ch = _median([s.cont for s in head])
    ct = _median([s.cont for s in tail])
    if ch > 0 and ct > 10 * ch:
        out.append(tr("the continuity error grows: {a} → {b}",
                      a=_g(ch), b=_g(ct)))
    return out


def _diverged(log: SolverLog, max_co: float) -> str:
    """Why this log is a divergence ("" = it is not one)."""
    if log.fpe:
        return tr("arithmetic error (floating point exception)")
    if log.nan:
        return tr("a residual became NaN")
    if max_co > 0 and any(s.co_max > CO_DIVERGED_FACTOR * max_co
                          for s in log.steps[-5:]):
        return tr("the Courant number jumped to {v:.0f}",
                  v=max(s.co_max for s in log.steps[-5:]))
    tail = residual_series(log)[-5:]
    if tail and min(r for _, r in tail) > 1.0:
        return tr("the residuals are above 1 and do not fall")
    return ""


def _transient_rows(cfg, log: SolverLog, rep: Report, crash: str) -> None:
    s = cfg.solver
    central = fc.solver_name(cfg) == "rhoCentralFoam"
    lo = CO_MIN_CENTRAL if central else CO_MIN
    signs = [] if crash else transient_signs(log, s.max_courant)
    co_tail = [x.co_max for x in log.steps[-10:] if x.co_max > 0]
    co_text = (tr("Co max {v:.2g} (last steps)", v=_median(co_tail))
               if co_tail else tr("not measured"))
    if crash or signs:
        cut = CO_CUT_DIVERGED if crash else CO_CUT_WARNING
        new = _nice(s.max_courant * cut, 0.05 if central else 0.25)
        capped = (cfg.flow.compressible and not cfg.flow.supersonic
                  and new > fc.COMPRESSIBLE_MAX_CO)
        if capped:
            new = fc.COMPRESSIBLE_MAX_CO
        if new < s.max_courant and new >= lo:
            why = (tr("The run diverged at maxCo {m:g}. Half the step is "
                      "the first thing to try: the crash came within the "
                      "first {n} steps, so the time step is the main "
                      "suspect.", m=s.max_courant, n=log.n)
                   if crash else
                   tr("Early signs of a divergence at maxCo {m:g}: {signs}. "
                      "A small step down (× {c:g}) is safer than a large "
                      "one: a large change can stop a long run that works.",
                      m=s.max_courant, signs=fs_i18n.join("; ", signs),
                      c=CO_CUT_WARNING))
            if capped:
                why = why + " " + tr(
                    "For a compressible transient the value is not more than "
                    "{v:g}: in the measured cases such a run is clean at 2 "
                    "and dies at about 4.", v=fc.COMPRESSIBLE_MAX_CO)
            rep.rows.append(Row(
                "solver.max_courant", "maxCo", co_text, s.max_courant, new,
                why, "bad" if crash else "warn", "float", lo,
                fc.MAX_COURANT, 2, 0.05))
        elif crash:
            rep.notes.append(tr(
                "maxCo is already {m:g}. A smaller time step is not the "
                "cure here: examine the mesh (checkMesh) and the boundary "
                "conditions.", m=s.max_courant))
        if crash and s.n_outer_correctors < OUTER_MAX and not central:
            rep.rows.append(Row(
                "solver.n_outer_correctors", "nOuterCorrectors",
                tr("diverged with {n}", n=s.n_outer_correctors),
                s.n_outer_correctors, s.n_outer_correctors + 1,
                tr("One more outer pass per time step makes each step more "
                   "stable. Each step costs about {p:.0f}% more time.",
                   p=100.0 / max(1, s.n_outer_correctors)),
                "warn", "int", 1, 10, 0, 1))
    elif co_tail:
        rep.rows.append(Row(
            "info.courant", "maxCo", co_text, s.max_courant, None,
            tr("Stable at maxCo {m:g} for {n} steps. The pre-run does not "
               "propose a larger step: a short run cannot show that it "
               "stays stable for hours.", m=s.max_courant, n=log.n)))

    dts = [x.dt for x in log.steps[-10:] if x.dt > 0]
    sps = seconds_per_step(log)
    if dts and not crash:
        dt = _median(dts)
        t_last = log.steps[-1].time if log.steps else 0.0
        steps_left = max(0.0, (s.end_time - t_last) / dt)
        text = tr("time step {dt} s, {n} steps to {end:g} s",
                  dt=_g(dt), n=fc.fmt_count(steps_left), end=s.end_time)
        why = tr("The time step that the solver settled on at maxCo {m:g}.",
                 m=s.max_courant)
        if sps > 0:
            total = steps_left * sps
            text = text + "; " + tr("{v} per step", v=fmt_seconds(sps))
            why = tr("At {v} per step the main run takes about {t} "
                     "(processes: {n}).", v=fmt_seconds(sps),
                     t=fmt_seconds(total), n=s.n_procs)
        rep.rows.append(Row("info.time", tr("Main run time"), text, None,
                            None, why,
                            "warn" if sps and steps_left * sps > 48 * 3600
                            else "info"))


def _steady_rows(cfg, log: SolverLog, stable: SolverLog | None,
                 rep: Report, crash: str, pre_iters: int) -> None:
    s = cfg.solver
    if crash:
        if not s.two_stage and not s.robust_start:
            n = min(ROBUST_DEFAULT, max(50, s.n_iterations // 3))
            rep.rows.append(Row(
                "solver.robust_iterations", tr("Stable start, iterations"),
                tr("diverged after {n} iterations", n=log.n),
                s.robust_iterations, n,
                tr("The first iterations run with first-order schemes and "
                   "low relaxation, then the run continues with the normal "
                   "schemes. This takes the field past the start, where "
                   "most divergences occur."),
                "bad", "int", 0, max(1, s.n_iterations - 1), 0, 50))
        elif s.relaxation_u > RELAX_MIN:
            new = max(RELAX_MIN, round(s.relaxation_u * RELAX_STEP, 2))
            rep.rows.append(Row(
                "solver.relaxation_u", tr("Relaxation U"),
                tr("diverged after {n} iterations", n=log.n),
                s.relaxation_u, new,
                tr("The stable start is already on. A lower relaxation "
                   "makes each iteration a smaller step. Convergence is "
                   "slower but it does not diverge."),
                "bad", "float", 0.1, 1.0, 2, 0.05))
        else:
            rep.notes.append(tr(
                "The relaxation is already low. The cause is not in the "
                "solver settings: examine the mesh (checkMesh) and the "
                "geometry."))
        return

    series = residual_series(log)
    offset = stable.n if stable is not None else 0
    if log.converged:
        rep.rows.append(Row(
            "info.converged", tr("Convergence"),
            tr("reached in {n} iterations", n=offset + log.n), None, None,
            tr("The residuals fell below {tol:g} in the pre-run. The main "
               "run stops at the same point.", tol=s.residual_tol)))
    elif series:
        pred, slope, last = residual_trend(series, s.residual_tol, offset)
        measured = tr("residual {r} after {n} iterations, {d:.2f} decades "
                      "per 100", r=_g(last or series[-1][1]),
                      n=offset + log.n, d=-100 * slope)
        if pred is None:
            rep.rows.append(Row(
                "info.plateau", tr("Convergence"), measured, None, None,
                tr("The residuals do not fall any more (a plateau). The "
                   "threshold {tol:g} is not reached, and more iterations "
                   "do not help. Judge the result by the force "
                   "coefficients, not by the residuals.",
                   tol=s.residual_tol), "warn"))
        else:
            # No iteration count from this trend. Measured on four real
            # cases: the line through iterations 100-200 predicted the
            # threshold at iteration 1197 (case_aoa0) and 1603 (v1_aoa0),
            # but the full runs stayed on a plateau at 3e-3 and 6e-3.
            rep.rows.append(Row(
                "info.falling", tr("Convergence"), measured, None, None,
                tr("The residuals still fall. A short run cannot tell if "
                   "they reach {tol:g}: in the measured cases they stopped "
                   "falling at 1e-3 … 1e-2 after 150-300 iterations. Judge "
                   "the result by the force coefficients.",
                   tol=s.residual_tol)))

    sps = seconds_per_step(log)
    if sps > 0:
        rep.rows.append(Row(
            "info.time", tr("Main run time"),
            tr("{v} per iteration", v=fmt_seconds(sps)), None, None,
            tr("{n} iterations take about {t} (processes: {p}).",
               n=s.n_iterations, t=fmt_seconds(sps * s.n_iterations),
               p=s.n_procs)))


def _yplus_rows(cfg, log: SolverLog | None, facts: RunFacts, rep: Report,
                layer_tips: bool) -> None:
    if log is None or not log.yplus:
        if not cfg.solver.steady and not cfg.warm_start:
            rep.notes.append(tr(
                "y+ is not measured: in a short transient pre-run the "
                "boundary layer has no time to develop."))
        return
    avgs = [v[2] for v in log.yplus.values() if not math.isnan(v[2])]
    if not avgs:
        return
    y = sum(avgs) / len(avgs)
    m = cfg.mesh
    measured = tr("y+ average {y:.3g}", y=y)
    model = cfg.flow.turbulence
    treat = fy.cfg_treatment(cfg)
    check = fy.check_yplus(model, treat, predicted=y)
    if check.level == "ok":
        rep.rows.append(Row("info.yplus", "y+", measured, None, None,
                            tr("Suits {model}.", model=model)))
        return
    if not m.add_layers or m.n_layers <= 0:
        rep.notes.append(tr("y+ average {y:.3g} does not suit {model}, and "
                            "the mesh has no layers.", y=y, model=model))
        return
    share = _face_share(facts)
    if layer_tips or share < adv.LAYER_FINE:
        rep.notes.append(tr(
            "y+ average {y:.3g} includes the faces without layers. Repair "
            "the layer coverage first, then measure y+ again.", y=y))
        return
    aim = m.target_yplus or fy.best_yplus(
        model, treat, cfg.flow.u_inf, cfg.flow.l_ref or cfg.body_length,
        cfg.flow.nu)[0]
    if aim <= 0 or max(y / aim, aim / y) < YPLUS_FACTOR:
        return
    want = m.final_layer_rel * aim / y
    new = round(min(max(want, 0.05), 1.0), 2)
    if abs(new - m.final_layer_rel) < 0.01:
        return
    why = tr("y+ grows with the thickness of the first layer. To get y+ ≈ "
             "{aim:g}, the layers get {f:.2g} × their thickness. The layer "
             "count and the expansion stay the same.", aim=aim, f=new /
             m.final_layer_rel)
    if new != round(want, 2):
        why = why + " " + tr("This value is at its limit and does not reach "
                             "the aim fully: also use “Fit layers” on the "
                             "“Mesh” tab.")
    rep.rows.append(Row("mesh.final_layer_rel", "finalLayerThickness",
                        measured, m.final_layer_rel, new, why, "warn",
                        "float", 0.05, 1.0, 2, 0.05))


# The field of each layer tip (fs_advisor.layer_diagnosis).
_LAYER_FIELDS = (("n_layers", "int", 1, fy.LAYER_CAP, 0, 1),
                 ("expansion", "float", 1.0, 2.0, 2, 0.05),
                 ("final_layer_rel", "float", 0.05, 1.0, 2, 0.05),
                 ("min_thickness_rel", "float", 0.001, 1.0, 3, 0.01),
                 # Coverage knobs of a wall-resolved case (T38).
                 ("max_thickness_medial", "float", 0.1, 1.0, 2, 0.05),
                 ("layer_min_medial_angle", "int", 60, 180, 0, 5),
                 ("layer_feature_angle", "int", 30, 180, 0, 5),
                 ("n_smooth_surface_normals", "int", 0, 20, 0, 1),
                 ("n_smooth_normals", "int", 0, 20, 0, 1),
                 ("layer_n_smooth_thickness", "int", 0, 50, 0, 1),
                 ("layer_n_relax_iter", "int", 1, 30, 0, 1))
_LAYER_TITLES = {"n_layers": "nSurfaceLayers", "expansion": "expansionRatio",
                 "final_layer_rel": "finalLayerThickness",
                 "min_thickness_rel": "minThickness",
                 "max_thickness_medial": "maxThicknessToMedialRatio",
                 "layer_min_medial_angle": "minMedialAxisAngle",
                 "layer_feature_angle": "featureAngle",
                 "n_smooth_surface_normals": "nSmoothSurfaceNormals",
                 "n_smooth_normals": "nSmoothNormals",
                 "layer_n_smooth_thickness": "nSmoothThickness",
                 "layer_n_relax_iter": "nRelaxIter"}
# Fields that set the stack itself: a change turns off the auto y+ pick.
_STACK_FIELDS = ("mesh.n_layers", "mesh.expansion", "mesh.final_layer_rel",
                 "mesh.add_layers")


def _face_share(facts: RunFacts) -> float:
    """The share of faces with layers: «Extruding» if known, else «Added»."""
    return facts.faces if facts.faces >= 0 else facts.coverage


def wall_resolved(cfg: fc.CaseCfg) -> bool:
    """True: the case needs y+ ≈ 1 (fs_advisor.wall_resolved, T38).

    For these, y+ ≈ 1 is a hard target: the layer advice keeps the stack
    and proposes only coverage changes (the user's decision, 2026-11-12).
    """
    return adv.wall_resolved(cfg)


def _layer_rows(cfg, facts: RunFacts, rep: Report, diag) -> bool:
    """Rows from the layer advice. True: there are layer changes."""
    if facts.coverage < 0 and facts.faces < 0:
        if facts.mesh_from_store:
            rep.notes.append(tr(
                "The mesh came from the mesh store, so snappyHexMesh did not "
                "run: the layer coverage is not measured in this pre-run."))
        return False
    share = _face_share(facts)
    measured = tr("layers on {p:.0f}% of faces", p=100 * share)
    if facts.faces >= 0 and facts.coverage >= 0:
        measured = measured + "; " + tr(
            "{p:.0f}% of the planned layer cells", p=100 * facts.coverage)
    d = adv.layer_diagnosis(cfg, facts.coverage if facts.coverage >= 0
                            else share, diag, faces=facts.faces)
    notes = list(d.notes)
    if d.wall_resolved and d.level != "ok":
        # The advice keeps the stack (y+ ≈ 1): its first note says why.
        # The coverage rows follow.
        rep.rows.append(Row(
            "info.layers", tr("Layers", ctx="count"), measured, None, None,
            fs_i18n.join(" ", [d.headline, notes.pop(0)]) if notes
            else d.headline, d.level))
    elif d.cfg is None:
        rep.rows.append(Row("info.layers", tr("Layers", ctx="count"),
                            measured, None, None, d.headline,
                            "info" if d.level == "ok" else d.level))
    if d.cfg is None:
        rep.notes.extend(n for n in notes if d.level != "ok")
        return False
    reasons = {t.what: t.reason for t in d.tips}
    for name, kind, lo, hi, dec, step in _LAYER_FIELDS:
        old, new = getattr(cfg.mesh, name), getattr(d.cfg.mesh, name)
        if old == new:
            continue
        title = _LAYER_TITLES[name]
        why = reasons.get(title) or reasons.get(tr("Layers", ctx="count"),
                                                d.headline)
        rep.rows.append(Row("mesh." + name, title, measured, old, new,
                            why, d.level, kind, lo, hi, dec, step))
    rep.notes.extend(notes)
    return True


def analyse(cfg: fc.CaseCfg, facts: RunFacts, logs: dict,
            mesh_locked: bool = False, diag=None) -> Report:
    """The report of a pre-run.

    `cfg` is the MAIN run configuration (not the pre-run copy). `logs`:
    step id -> log text of the solver steps (see log_names).
    """
    rep = Report(mesh_locked=mesh_locked)
    names = log_names(cfg)
    pre = prerun_cfg(cfg)
    parsed = {role: parse_log(logs[sid]) for role, sid in names.items()
              if logs.get(sid)}
    main = parsed.get("main")
    stable = parsed.get("stable")
    warm = parsed.get("warm")
    solver_ids = set(names.values())
    in_solver = facts.step in solver_ids

    # 1. A failure before the solver: nothing to tune, say why.
    if not facts.ok and not in_solver:
        rep.outcome, rep.can_start = "failed", False
        rep.headline = tr("The pre-run stopped before the solver: {msg}",
                          msg=facts.msg)
        if facts.mesh_level == "fatal":
            rep.notes.append(tr("The mesh is not usable: {why}.",
                                why=facts.mesh_why))
        return rep

    # The log of the step that failed (or of the last solver step).
    failed = None
    if not facts.ok:
        role = next((r for r, sid in names.items() if sid == facts.step),
                    "main")
        failed = parsed.get(role)
    crash = ""
    if failed is not None:
        crash = _diverged(failed, cfg.solver.max_courant)
        if not crash:
            rep.outcome, rep.can_start = "failed", False
            rep.headline = tr("The solver stopped with an error that the "
                              "settings of this window do not repair: {msg}",
                              msg=failed.fatal or facts.msg)
            return rep
    elif not facts.ok:
        rep.outcome, rep.can_start = "failed", False
        rep.headline = tr("The pre-run stopped: {msg}", msg=facts.msg)
        return rep

    if crash:
        rep.outcome = "diverged"
        rep.headline = tr("The pre-run diverged after {n} steps: {why}.",
                          n=failed.n, why=crash)
    else:
        n = main.n if main is not None else 0
        rep.headline = (tr("The pre-run is stable: {n} iterations.",
                           n=(stable.n if stable else 0) + n)
                        if cfg.solver.steady else
                        tr("The pre-run is stable: {n} time steps.", n=n))

    # 2. The solver.
    if cfg.solver.steady:
        log = failed if crash else main
        if log is not None:
            _steady_rows(cfg, log, None if crash else stable, rep, crash,
                         pre.solver.n_iterations)
    else:
        if crash and failed is warm:
            # The warm start (steady) diverged, not the transient.
            rep.rows.append(Row(
                "solver.relaxation_u", tr("Relaxation U"),
                tr("warm start diverged after {n} iterations", n=warm.n),
                cfg.solver.relaxation_u,
                max(RELAX_MIN, round(cfg.solver.relaxation_u * RELAX_STEP,
                                     2)),
                tr("The warm start is a steady run. A lower relaxation "
                   "makes each of its iterations a smaller step."),
                "bad", "float", 0.1, 1.0, 2, 0.05))
        elif main is not None:
            _transient_rows(cfg, main, rep, crash)
        if cfg.portal_mode:
            rep.notes.append(tr(
                "The engine sources grow slowly at the start. The pre-run "
                "is shorter than this growth, so its Courant number is "
                "lower than in the main run later."))

    if (rep.outcome == "ok" and not cfg.solver.steady
            and any(r.key == "solver.max_courant" for r in rep.rows)):
        rep.outcome = "warn"
        rep.headline = tr("No crash in {n} time steps, but there are early "
                          "signs of a divergence.", n=main.n)

    # 3. The mesh.
    if facts.mesh_level in ("warn", "fatal") and facts.mesh_why:
        rep.rows.append(Row("info.mesh", "checkMesh", facts.mesh_why, None,
                            None, tr("Mesh quality remarks of checkMesh."),
                            "bad" if facts.mesh_level == "fatal" else
                            "info"))
    if mesh_locked:
        rep.notes.append(tr(
            "The mesh is locked (mesh from the project): the pre-run does "
            "not propose mesh changes."))
    else:
        tips = _layer_rows(cfg, facts, rep, diag)
        _yplus_rows(cfg, main if cfg.solver.steady else warm, facts, rep,
                    tips)
        if any(r.change and r.key.startswith("mesh.") for r in rep.rows):
            rep.notes.append(
                tr("A mesh change makes the main run build the mesh again "
                   "(snappyHexMesh).") if facts.mesh_cache else
                tr("This backend does not keep meshes: the main run builds "
                   "the mesh again in any case."))
    if not facts.mesh_cache and not any(
            r.change and r.key.startswith("mesh.") for r in rep.rows):
        rep.notes.append(tr("This backend does not keep meshes: the main "
                            "run builds the mesh again."))
    return rep


def apply(cfg: fc.CaseCfg, values: dict) -> list[tuple[str, object, object]]:
    """Write the accepted values into `cfg`. Returns (key, old, new).

    `values`: key ("solver.max_courant") -> the value to set. Other keys
    are ignored. A layer change turns off the automatic y+ pick, as the
    «Repair Layers» button does: else it would undo the change.
    """
    done = []
    for key, value in values.items():
        part, _, name = key.partition(".")
        target = {"solver": cfg.solver, "mesh": cfg.mesh}.get(part)
        if target is None or not hasattr(target, name):
            continue
        old = getattr(target, name)
        new = int(round(value)) if isinstance(old, int) and not isinstance(
            old, bool) else float(value)
        if new != old:
            setattr(target, name, new)
            done.append((key, old, new))
    # Coverage knobs (T38) do not change the stack: auto y+ stays.
    if any(k in _STACK_FIELDS for k, _, _ in done):
        cfg.mesh.yplus_auto = False
    return done
