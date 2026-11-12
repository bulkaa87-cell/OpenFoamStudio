"""y+ and the turbulence model: which first-cell height each model needs.

Three jobs, all without Qt so that the offline tests can call them:

  check_yplus()  says if a y+ suits a model and a wall treatment
                 (ok / warn / error, a short Ukrainian text, the fixes);
  best_yplus()   gives the y+ that suits the model for this Re;
  auto_pick()    gives the layer count and the expansion ratio that reach
                 that y+ within the mesh limits (layer cap, maxGlobalCells,
                 memory).

The wall treatment is what the case really writes (FlowCfg.wall_treatment,
the «Обробка стінки» list; fs_case.field_nut / field_k use the same
case_wall_treatment):

  spalding  nut nutUSpaldingWallFunction, k kqRWallFunction — continuous wall
            law, correct from y+ ~1 to ~300 (the default);
  wallfn    nut nutkWallFunction, k kqRWallFunction — log law, y+ 30-300;
  lowre     nut nutLowReWallFunction (nut = 0), k fixedValue 1e-10 — the
            layer is resolved to the wall, y+ ~1.

omega is always omegaWallFunction (it blends the viscous and the log value),
epsilon epsilonWallFunction, nuTilda fixedValue 0, alphat
compressible::alphatWallFunction. All these names are in the v2512 libraries
(checked in WSL, libincompressible/compressibleTurbulenceModels.so).
"""

from __future__ import annotations

import copy
import math
from dataclasses import dataclass, field, replace

import fs_physics as phys
import fs_i18n
from fs_i18n import tr

# ---------------------------------------------------------------------------
# Wall treatments
# ---------------------------------------------------------------------------

SPALDING = "spalding"
WALLFN = "wallfn"
LOWRE = "lowre"

WALL_TREATMENTS = {
    SPALDING: "nutUSpaldingWallFunction — continuous law of the wall, "
              "from y+ ≈ 1 to ~300",
    WALLFN: "nutkWallFunction — law of the wall, log layer only (30–300)",
    LOWRE: "nutLowReWallFunction — layer resolved down to the wall (y+ ≈ 1)",
}


TREATMENT_ORDER = (SPALDING, WALLFN, LOWRE)

# The list text says the y+ range itself: the choice is then hard to get
# wrong (the user asked for this).
TREATMENT_LABELS = {
    SPALDING: "Spalding (any y+)",
    WALLFN: "Standard wall function (nutkWallFunction) — y+ 30–300 only",
    LOWRE: "Low-Re (nutLowReWallFunction) — y+ ≈ 1 only",
}


def why_not_treatment(model: str, treatment: str) -> str:
    """"" if the treatment is valid for the model, else why not (Ukrainian).

    nutkWallFunction takes the friction from k: models without k
    (SpalartAllmaras, WALE) cannot use it. A transition model and LES must
    resolve the wall, the log law jumps over it. kEpsilon has no near-wall
    damping, so a low-Re wall is wrong for it.
    """
    fam = model_family(model)
    if treatment == SPALDING:
        return ""
    if treatment == WALLFN:
        if fam == "transition":
            return (tr("{model} computes transition and needs a resolved "
                       "layer (y+ ≈ 1), but the law of the wall skips "
                       "over it.", model=model))
        if fam == "les":
            return (tr("{model} is LES: the log-layer law does not fit "
                       "here. Use Spalding (a wall model) or Low-Re (y+ "
                       "≈ 1).", model=model))
        if fam in ("sa", "lowre"):
            return (tr("nutkWallFunction computes friction from k, but "
                       "{model} does not solve a k field (or damps the "
                       "wall itself).", model=model))
        return ""
    if treatment == LOWRE:
        if fam == "ke":
            return (tr("{model} has no near-wall damping: at y+ ≈ 1 it "
                       "is wrong. For a resolved layer, use kOmegaSST.", model=model))
        return ""
    return tr("Unknown wall treatment.")


def allowed_treatments(model: str) -> tuple:
    return tuple(t for t in TREATMENT_ORDER if not why_not_treatment(model, t))


def case_wall_treatment(model: str, chosen: str = SPALDING) -> str:
    """The wall treatment that fs_case writes: the chosen one if it is valid
    for the model, else Spalding (valid for every model)."""
    if chosen in TREATMENT_ORDER and not why_not_treatment(model, chosen):
        return chosen
    return SPALDING


def cfg_treatment(cfg) -> str:
    f = cfg.flow
    return case_wall_treatment(f.turbulence,
                               getattr(f, "wall_treatment", SPALDING))


# ---------------------------------------------------------------------------
# Model families
# ---------------------------------------------------------------------------

# Pure LES: the eddies are resolved also near the wall.
LES_PURE = ("WALE", "kEqn", "Smagorinsky", "dynamicKEqn", "dynamicLagrangian")
# RANS models with low-Re damping: they are integrated to the wall.
LOWRE_MODELS = ("LaunderSharmaKE", "LienLeschziner", "qZeta", "v2f")


def model_family(model: str) -> str:
    """transition / lowre / les / ke / sa / komega."""
    m = model or ""
    low = m.lower()
    if ("gamma" in low or "retheta" in low or m.endswith("LM")
            or m == "kkLOmega"):
        return "transition"
    if m in LOWRE_MODELS:
        return "lowre"
    if m in LES_PURE:
        return "les"
    if m.startswith("SpalartAllmaras"):
        return "sa"
    if "epsilon" in low or m.endswith("KE"):
        return "ke"
    return "komega"


def _resolved_family(model: str, treatment: str) -> bool:
    """Needs y+ ~1: the wall is resolved, not modelled."""
    fam = model_family(model)
    if fam in ("transition", "lowre", "les"):
        return True
    return treatment == LOWRE and fam != "ke"


# ---------------------------------------------------------------------------
# Check
# ---------------------------------------------------------------------------

FIX_Y1 = ("yplus", 1.0, "Set y+ = 1 and fit the layers")
FIX_Y50 = ("yplus", 50.0, "Set y+ = 50 and fit the layers")
FIX_SST = ("model", "kOmegaSST", "Switch to kOmegaSST")
FIX_SPALDING = ("treatment", SPALDING, "Switch to Spalding (any y+)")

_SEVERITY = {"ok": 0, "warn": 1, "error": 2}


@dataclass
class YplusCheck:
    level: str                      # ok / warn / error
    yplus: float                    # the value that was checked
    short: str                      # for the status bar
    text: str                       # for the panel and the run checks
    fixes: list = field(default_factory=list)   # (kind, value, label)

    @property
    def ok(self) -> bool:
        return self.level == "ok"


def _g(y: float) -> str:
    """y+ in a short form: 0.82, 1.6, 44."""
    return f"{y:.2g}" if y < 10 else f"{y:.0f}"


def _rule(model: str, treatment: str, y: float) -> YplusCheck:
    fam = model_family(model)
    ys = _g(y)

    def mk(level, text, fixes=(), short=""):
        return YplusCheck(level, y, short or tr("y+ ≈ {ys} is not for {model}", ys=ys, model=model),
                          text, list(fixes))

    if fam == "transition":
        if y <= 1.0:
            return mk("ok", tr("y+ ≈ {ys} — the layer is resolved down "
                               "to the wall, as {model} needs.", ys=ys, model=model))
        if y <= 2.0:
            return mk("warn", (
                tr("y+ ≈ {ys} — borderline for {model}. A transition "
                   "model wants y+ ≤ 1 on the whole body (Langtry–Menter "
                   "guideline), and at the nose and edges y+ is higher "
                   "than the average.", ys=ys, model=model)), [FIX_Y1],
                short=tr("y+ ≈ {ys} borderline for {model} (needs ≤ 1)", ys=ys, model=model))
        return mk("error", (
            tr("{model} computes the transition of the laminar layer to "
               "turbulent and needs y+ ≈ 1 (no more than 1–2) on the "
               "whole body. At y+ ≈ {ys} the layer is not resolved: the "
               "transition will be wrong, and the two extra equations "
               "only slow down the run.", model=model, ys=ys)), [FIX_Y1, FIX_SST],
            short=tr("y+ ≈ {ys} is not for {model} (needs ≈ 1)", ys=ys, model=model))

    if fam == "les":
        if y <= 2.0:
            return mk("ok", tr("y+ ≈ {ys} — LES sees the near-wall eddies.", ys=ys))
        return mk("warn", (
            tr("{model} is LES. At y+ ≈ {ys} the near-wall eddies are "
               "not resolved: near the wall, nutUSpalding works as a "
               "wall model (this is already WMLES). True LES needs y+ ≈ "
               "1, and then the mesh is many times larger.", model=model, ys=ys)), [FIX_Y1],
            short=tr("y+ ≈ {ys}: {model} becomes WMLES (needs ≈ 1)", ys=ys, model=model))

    if fam == "lowre" or (treatment == LOWRE and fam != "ke"):
        what = (tr("the {model} model has near-wall damping", model=model)
                if fam == "lowre" else "nutLowReWallFunction")
        # A chosen low-Re wall can go back to Spalding; a low-Re model cannot.
        alt = [FIX_SPALDING] if fam != "lowre" else []
        if y <= 1.0:
            return mk("ok", tr("y+ ≈ {ys} — the layer is resolved down to the wall.", ys=ys))
        if y <= 5.0:
            return mk("warn", (
                tr("y+ ≈ {ys}: {what} and wants y+ ≈ 1. In the viscous "
                   "sublayer it still works, but coarsely.", ys=ys, what=what)), [FIX_Y1] + alt,
                short=tr("y+ ≈ {ys} coarse for {model} (needs ≈ 1)", ys=ys, model=model))
        return mk("error", (
            tr("The layer is resolved down to the wall ({what}), so y+ ≈ "
               "1 is needed. At y+ ≈ {ys} the first cell is already "
               "outside the viscous sublayer, and the friction will be "
               "wrong.", what=what, ys=ys)), [FIX_Y1] + alt,
            short=tr("y+ ≈ {ys} is not for {model} (needs ≈ 1)", ys=ys, model=model))

    # From here on: the wall is modelled (ke, sa, komega).
    high = None
    if y > 1000.0:
        high = mk("error", (
            tr("y+ ≈ {ys} — far beyond the log layer: the law of the "
               "wall does not apply here, and the friction is not "
               "reliable. Aim for 30–150.", ys=ys)), [FIX_Y50],
            short=tr("y+ ≈ {ys}: far beyond the log layer", ys=ys))
    elif y > 300.0:
        high = mk("warn", (
            tr("y+ ≈ {ys} — too high: the first cell is at the edge of "
               "the log layer, and the friction accuracy drops. Aim for "
               "30–150.", ys=ys)), [FIX_Y50],
            short=tr("y+ ≈ {ys}: too high (needs 30–150)", ys=ys))
    if high is not None:
        return high
    if y >= 30.0:
        tail = "" if y <= 150.0 else tr(" Acceptable, but 30–150 is more accurate.")
        return mk("ok", tr("y+ ≈ {ys} — log layer, the law of the wall works.{tail}", ys=ys, tail=tail))

    buffer_text = (
        tr("y+ ≈ {ys} — buffer layer (5–30): neither the viscous "
           "sublayer nor the log layer, the worst zone for the law of "
           "the wall.", ys=ys))

    if fam == "ke":
        if y < 5.0:
            return mk("warn", (
                tr("{model} has no near-wall damping: a small first cell "
                   "(y+ ≈ {ys}) does not help it, the law of the wall "
                   "computes the friction anyway. Aim for 30–150, or use "
                   "kOmegaSST if you need a resolved layer.", model=model, ys=ys)), [FIX_Y50, FIX_SST],
                short=tr("y+ ≈ {ys}: {model} wants 30–150", ys=ys, model=model))
        return mk("warn", tr("{buffer_text} Aim for 30–150.", buffer_text=buffer_text), [FIX_Y50],
                  short=tr("y+ ≈ {ys}: buffer layer", ys=ys))

    if treatment == SPALDING:
        if y <= 5.0:
            return mk("ok", (
                tr("y+ ≈ {ys} — viscous sublayer. nutUSpalding and "
                   "omegaWallFunction work here; this is the more "
                   "accurate option.", ys=ys)))
        if fam == "sa":
            return mk("ok", (
                tr("y+ ≈ {ys}. SpalartAllmaras with nutUSpalding works "
                   "over the whole range, also in the buffer layer.", ys=ys)))
        return mk("warn", tr("{buffer_text} nutUSpalding bridges it, so "
                             "the run does not break, but the accuracy "
                             "is lowest here. Better y+ ≈ 1 or 30–150.", buffer_text=buffer_text),
            [FIX_Y1, FIX_Y50], short=tr("y+ ≈ {ys}: buffer layer", ys=ys))

    # Standard log-law wall function.
    if y < 5.0:
        return mk("warn", (
            tr("y+ ≈ {ys}: nutkWallFunction assumes that the first cell "
               "is in the log layer, but it is in the viscous sublayer. "
               "Aim for 30–150 or use a continuous function "
               "(nutUSpalding).", ys=ys)),
            [FIX_Y50, FIX_SPALDING],
            short=tr("y+ ≈ {ys}: too low for the law of the wall", ys=ys))
    return mk("warn", tr("{buffer_text} Aim for 30–150.", buffer_text=buffer_text),
              [FIX_Y50, FIX_SPALDING], short=tr("y+ ≈ {ys}: buffer layer", ys=ys))


def check_yplus(model: str, treatment: str = "", target: float = 0.0,
                predicted: float | None = None) -> YplusCheck:
    """Does the y+ suit the model and the wall treatment?

    `target` is what the person aims at, `predicted` is what the layers give
    (after «Підібрати шари»). Both are checked; the worse result is returned,
    and on equal level the predicted one (it is what the solver will see).
    """
    treatment = treatment or case_wall_treatment(model)
    values = []
    if predicted is not None and predicted > 0:
        values.append(float(predicted))
    if target and target > 0:
        values.append(float(target))
    if not values:
        return YplusCheck("ok", 0.0, "", "")
    best = None
    for y in values:
        c = _rule(model, treatment, y)
        if best is None or _SEVERITY[c.level] > _SEVERITY[best.level]:
            best = c
    return best


def good_range(model: str, treatment: str = "") -> tuple[float, float]:
    """The y+ range that check_yplus calls ok (upper end of the main band)."""
    treatment = treatment or case_wall_treatment(model)
    fam = model_family(model)
    if fam == "transition" or fam == "lowre":
        return (0.0, 1.0)
    if fam == "les":
        return (0.0, 2.0)
    if treatment == LOWRE and fam != "ke":
        return (0.0, 1.0)
    if fam == "sa" and treatment == SPALDING:
        return (0.0, 300.0)
    return (30.0, 300.0)


def model_hint(model: str, treatment: str = "") -> str:
    """One or two lines under the y+ field: where to aim for this model."""
    treatment = treatment or case_wall_treatment(model)
    fam = model_family(model)
    if fam == "transition":
        return (tr("{model} is a transition model: aim for y+ ≈ 1 on the "
                   "whole body (no more than 1–2). With a wall function "
                   "it cannot work.", model=model))
    if fam == "les":
        return (tr("{model} is LES: resolved near-wall eddies need y+ ≈ "
                   "1. A larger y+ turns it into WMLES.", model=model))
    if fam == "lowre" or (treatment == LOWRE and fam != "ke"):
        return tr("{model}: the layer is resolved down to the wall — aim for y+ ≈ 1.", model=model)
    if fam == "ke":
        return (tr("{model} works only with the law of the wall: y+ "
                   "30–150 (up to 300). A finer wall does not help it, "
                   "and 5–30 is the buffer layer.", model=model))
    if treatment == SPALDING and fam == "sa":
        return (tr("{model} with nutUSpalding works from y+ ≈ 1 to ~300: "
                   "more accurate near 1, cheaper at 30–150.", model=model))
    if treatment == SPALDING:
        return (tr("{model} with nutUSpalding: either y+ ≤ 5 (layer "
                   "resolved, more accurate) or 30–150 (up to 300, "
                   "cheaper). 5–30 is the buffer layer, the least "
                   "accurate zone.", model=model))
    return (tr("{model} with the law of the wall: y+ 30–150 (up to 300). "
               "Do not use 5–30 — that is the buffer layer.", model=model))


# ---------------------------------------------------------------------------
# Best y+ for the case
# ---------------------------------------------------------------------------

# Below this upper edge of the log layer (in y+), the band 30..edge is too
# narrow to put a wall-function cell into it with a margin.
LOG_WINDOW_MIN = 60.0


def log_layer_top(u: float, length: float, nu: float) -> float:
    """Upper edge of the log layer in y+: ~0.15 of the turbulent BL height.

    delta+ = delta * u_tau / nu, with delta = 0.37 L / Re^0.2 (end of a flat
    plate). At Re 1e5 this is ~30 (no room for a wall function), at 1e6 ~150,
    at 1e7 ~800.
    """
    if u <= 0 or length <= 0 or nu <= 0:
        return 0.0
    delta = phys.bl_thickness(u, length, nu)
    u_tau = phys.friction_velocity(u, phys.reynolds(u, length, nu))
    return 0.15 * delta * u_tau / nu


def best_yplus(model: str, treatment: str, u: float, length: float,
               nu: float) -> tuple[float, str]:
    """(target y+, short reason in Ukrainian)."""
    treatment = treatment or case_wall_treatment(model)
    fam = model_family(model)
    if fam == "transition":
        return 1.0, tr("{model} needs a resolved layer", model=model)
    if fam == "les":
        return 1.0, tr("{model}: LES resolves the eddies near the wall", model=model)
    if fam == "lowre" or (treatment == LOWRE and fam != "ke"):
        return 1.0, tr("{model}: the layer is resolved down to the wall", model=model)

    top = log_layer_top(u, length, nu)
    if top >= LOG_WINDOW_MIN:
        # Geometric middle of 30..top, so that the cell stays in the log
        # layer also where the local friction differs from the mean.
        y = max(30.0, min(100.0, math.sqrt(30.0 * top)))
        y = round(y / 5.0) * 5.0
        return y, tr("{model}: law of the wall, middle of the log layer 30–{top:.0f}", model=model, top=top)
    if fam == "ke" or treatment == WALLFN:
        return 30.0, (tr("{model}: the log layer at this Re is narrow "
                         "(up to y+ ≈ {top:.0f}), so the lower edge", model=model, top=top))
    return 1.0, (tr("the log layer at this Re is too narrow (up to y+ ≈ "
                    "{top:.0f}) — for {model} with nutUSpalding it is "
                    "better to resolve the layer down to the wall", top=top, model=model))


# ---------------------------------------------------------------------------
# Auto layer picking
# ---------------------------------------------------------------------------

LAYER_CAP = 40                    # the «Кількість шарів» spin maximum
EXPANSION_LADDER = (1.25, 1.3)    # tried in this order if the cap is hit
EXPANSION_MAX = 1.3


@dataclass
class AutoPick:
    target: float = 0.0
    reason: str = ""
    n_layers: int = 0
    expansion: float = 1.2
    yplus: float = 0.0            # predicted y+ of the picked stack
    first_abs: float = 0.0        # first layer thickness, m
    level: str = "ok"             # ok / warn / error
    problem: str = ""             # why the pick is not good, or ""
    fixes: list = field(default_factory=list)
    skipped: bool = False         # nothing to pick (no flow / layers off)


def plural_layers(n: int) -> str:
    return fs_i18n.plural(n, "{n} layer", "{n} layers")


def _ref_length(cfg) -> float:
    return cfg.flow.l_ref or cfg.body_length


def auto_pick(cfg, fits=None, layer_cap: int = LAYER_CAP) -> AutoPick:
    """Pick the target y+ for the model and the layers that reach it.

    `fits(trial_cfg) -> str` gives "" if the mesh fits (maxGlobalCells and
    memory), else the reason. Without it only maxGlobalCells is checked.
    The result is not applied: the caller writes n_layers and expansion.
    """
    f = cfg.flow
    model = f.turbulence
    treat = cfg_treatment(cfg)
    L = _ref_length(cfg)
    cell = cfg.surface_cell
    pick = AutoPick(expansion=cfg.mesh.expansion)
    if f.u_inf <= 0 or f.nu <= 0 or L <= 0 or cell <= 0:
        pick.skipped = True
        pick.problem = tr("No speed, viscosity or reference length.")
        return pick

    target, reason = best_yplus(model, treat, f.u_inf, L, f.nu)
    pick.target, pick.reason = target, reason
    if not cfg.mesh.add_layers:
        pick.skipped = True
        pick.level = "warn"
        pick.problem = (tr("Layers are off — “Auto” does not fit them. Turn "
                        "on “Grow boundary layer”."))
        return pick

    cap = max(1, min(int(layer_cap), LAYER_CAP))
    final_rel = cfg.mesh.final_layer_rel

    def stack(n, e):
        return phys.LayerStack(n, e, final_rel, cell)

    def solve(t):
        """(n, expansion, y+, reached) for the target t."""
        ladder = [cfg.mesh.expansion] + [e for e in EXPANSION_LADDER
                                         if e > cfg.mesh.expansion + 1e-9]
        last = None
        for e in ladder:
            n = phys.layers_for_target_yplus(t, f.u_inf, L, f.nu, cell, e,
                                             final_rel, max_layers=cap)
            if not n:
                continue
            y = stack(n, e).yplus(f.u_inf, L, f.nu)
            last = (n, e, y, y <= t * 1.05)
            if last[3]:
                return last
        return last

    got = solve(target)
    if got is None:
        pick.skipped = True
        pick.problem = tr("Could not fit the layers: check the speed and Lref.")
        return pick
    n, e, y, reached = got

    # A wall-function target, but even one layer is far below it: the
    # surface cell is so small that the wall can be resolved for little.
    fam = model_family(model)
    if reached and target >= 30.0 and n == 1 and y < 0.6 * target:
        if fam in ("komega", "sa") and treat == SPALDING:
            target = 1.0
            reason = (tr("the surface cell is so small that even one "
                         "layer gives y+ ≈ {y} — it is cheaper to "
                         "resolve the layer down to the wall", y=_g(y)))
            pick.target, pick.reason = target, reason
            got = solve(target)
            n, e, y, reached = got

    pick.n_layers, pick.expansion, pick.yplus = n, e, y
    pick.first_abs = stack(n, e).first_abs

    if not reached:
        pick.level = "error" if _resolved_family(model, treat) else "warn"
        pick.problem = (
            tr("Target y+ ≈ {target} cannot be reached: {n} with "
               "expansionRatio {e:.2f} give only y+ ≈ {y}. The first "
               "layer is limited by the surface cell ({cell}): raise the "
               "surface level or decrease finalLayerThickness.", target=_g(target), n=plural_layers(n), e=e, y=_g(y), cell=phys.fmt_len(cell)))

    # Mesh size and memory.
    trial = copy.copy(cfg)
    trial.mesh = replace(cfg.mesh, n_layers=n, expansion=e)
    why = ""
    if fits is not None:
        try:
            why = fits(trial) or ""
        except Exception:                                 # noqa: BLE001
            # A failed memory check must not stop the pick; the status bar
            # and the run gate check the memory again.
            why = ""
    else:
        est = trial.estimate()
        if est.total > cfg.mesh.max_global_cells:
            why = (tr("the estimate of {total} cells is larger than "
                      "maxGlobalCells {max_global_cells}", total=phys.fmt_cells(est.total), max_global_cells=phys.fmt_cells(cfg.mesh.max_global_cells)))
    if why:
        if target <= 2.0:
            pick.level = "error"
            alt = ("" if fam == "ke" else
                   tr(" Or switch to kOmegaSST with the law of the wall (y+ "
                   "≈ 50): there will be several times fewer layers."))
            pick.problem = (
                tr("The resolved layer (y+ ≈ 1, {n}) does not fit: "
                   "{lstrip}. Give more memory (distribution machines, "
                   "maxGlobalCells) or use a coarser surface.{alt}", n=plural_layers(n), lstrip=why.lstrip('⚠ '), alt=alt))
            pick.fixes = [FIX_SST] if fam in ("transition", "les",
                                               "lowre") else []
        else:
            pick.level = "warn"
            pick.problem = tr("The mesh does not fit: {lstrip}.", lstrip=why.lstrip('⚠ '))
    return pick


def auto_line(pick: AutoPick, model: str) -> str:
    """The result line under the y+ field, one short line."""
    if pick.skipped:
        return tr("Auto: {problem}", problem=pick.problem)
    line = (tr("Auto: y+ ≈ {target} ({reason}) → {n_layers}, 1st {first_abs}", target=_g(pick.target), reason=pick.reason, n_layers=plural_layers(pick.n_layers), first_abs=phys.fmt_len(pick.first_abs)))
    if abs(pick.expansion - 1.2) > 1e-9:
        line += f", expansionRatio {pick.expansion:.2f}"
    if pick.yplus and abs(pick.yplus - pick.target) > 0.3 * pick.target:
        line += tr(", giving y+ ≈ {yplus}", yplus=_g(pick.yplus))
    return line + "."


def signature(cfg, layer_cap: int = LAYER_CAP) -> tuple:
    """The inputs of auto_pick. A new pick is needed only when this changes."""
    f, m = cfg.flow, cfg.mesh
    r = lambda v: round(float(v), 9)                      # noqa: E731
    return (f.turbulence, cfg_treatment(cfg), r(f.u_inf),
            r(f.nu), r(_ref_length(cfg)), r(cfg.surface_cell),
            r(m.final_layer_rel), r(m.expansion), bool(m.add_layers),
            int(m.max_global_cells), int(cfg.solver.n_procs),
            int(layer_cap), bool(getattr(m, "yplus_auto", True)))


# ---------------------------------------------------------------------------
# For the run checks
# ---------------------------------------------------------------------------

def predicted_yplus(cfg) -> float:
    """y+ of the first cell with the current settings (no layers: bare cell)."""
    f = cfg.flow
    L = _ref_length(cfg)
    st = cfg.layer_stack
    if st.n_layers:
        return st.yplus(f.u_inf, L, f.nu)
    return phys.yplus_for_wall_distance(cfg.surface_cell / 2.0, f.u_inf, L,
                                        f.nu)


def case_check(cfg) -> YplusCheck:
    """check_yplus for the case as it will be written."""
    m = cfg.mesh
    target = getattr(m, "target_yplus", 0.0) or 0.0
    if not m.add_layers:
        target = 0.0           # the target means nothing without layers
    return check_yplus(cfg.flow.turbulence,
                       cfg_treatment(cfg),
                       target=target, predicted=predicted_yplus(cfg))


def guard(cfg) -> list[tuple[str, str]]:
    """(level, text) for the checks before a run. Never blocks the run:
    a wrong y+ gives a wrong answer, not a crash, so the person decides."""
    c = case_check(cfg)
    if c.ok:
        return []
    fix = "; ".join(label for _, _, label in c.fixes)
    return [("warn", c.text + (tr(" What to do: {fix}.", fix=fix) if fix else ""))]
