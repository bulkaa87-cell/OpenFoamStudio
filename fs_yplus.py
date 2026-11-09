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

# ---------------------------------------------------------------------------
# Wall treatments
# ---------------------------------------------------------------------------

SPALDING = "spalding"
WALLFN = "wallfn"
LOWRE = "lowre"

WALL_TREATMENTS = {
    SPALDING: "nutUSpaldingWallFunction — неперервний закон стінки, "
              "від y+ ≈ 1 до ~300",
    WALLFN: "nutkWallFunction — закон стінки, лише лог-шар (30–300)",
    LOWRE: "nutLowReWallFunction — шар розвʼязано до стінки (y+ ≈ 1)",
}


TREATMENT_ORDER = (SPALDING, WALLFN, LOWRE)

# The list text says the y+ range itself: the choice is then hard to get
# wrong (the user asked for this).
TREATMENT_LABELS = {
    SPALDING: "Spalding (будь-який y+)",
    WALLFN: "Стандартна пристінна функція (nutkWallFunction) — лише y+ 30–300",
    LOWRE: "Low-Re (nutLowReWallFunction) — лише y+ ≈ 1",
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
            return (f"{model} рахує перехід і потребує розвʼязаного шару "
                    f"(y+ ≈ 1), а закон стінки його перестрибує.")
        if fam == "les":
            return (f"{model} — LES: закон лог-шару тут не годиться. Беріть "
                    f"Spalding (модель стінки) або Low-Re (y+ ≈ 1).")
        if fam in ("sa", "lowre"):
            return (f"nutkWallFunction рахує тертя з k, а {model} поля k не "
                    f"розвʼязує (або демпфує стінку сама).")
        return ""
    if treatment == LOWRE:
        if fam == "ke":
            return (f"{model} не має пристінного демпфування: на y+ ≈ 1 вона "
                    f"хибна. Для розвʼязаного шару візьміть kOmegaSST.")
        return ""
    return "Невідома обробка стінки."


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

FIX_Y1 = ("yplus", 1.0, "Поставити y+ = 1 і підібрати шари")
FIX_Y50 = ("yplus", 50.0, "Поставити y+ = 50 і підібрати шари")
FIX_SST = ("model", "kOmegaSST", "Перейти на kOmegaSST")
FIX_SPALDING = ("treatment", SPALDING, "Перейти на Spalding (будь-який y+)")

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
        return YplusCheck(level, y, short or f"y+ ≈ {ys} не для {model}",
                          text, list(fixes))

    if fam == "transition":
        if y <= 1.0:
            return mk("ok", f"y+ ≈ {ys} — шар розвʼязано до стінки, як і "
                            f"треба для {model}.")
        if y <= 2.0:
            return mk("warn", (
                f"y+ ≈ {ys} — на межі для {model}. Модель переходу хоче "
                f"y+ ≤ 1 на всьому тілі (настанова Ленгтрі–Ментера), а на "
                f"носку й кромках y+ вищий за середній."), [FIX_Y1],
                short=f"y+ ≈ {ys} на межі для {model} (треба ≤ 1)")
        return mk("error", (
            f"{model} рахує перехід ламінарного шару в турбулентний і "
            f"потребує y+ ≈ 1 (не більше 1–2) на всьому тілі. При y+ ≈ {ys} "
            f"шар не розвʼязано: перехід вийде хибним, а два зайві рівняння "
            f"лише сповільнять рахунок."), [FIX_Y1, FIX_SST],
            short=f"y+ ≈ {ys} не для {model} (треба ≈ 1)")

    if fam == "les":
        if y <= 2.0:
            return mk("ok", f"y+ ≈ {ys} — LES бачить пристінні вихори.")
        return mk("warn", (
            f"{model} — LES. При y+ ≈ {ys} пристінні вихори не розвʼязано: "
            f"біля стінки працює nutUSpalding як модель стінки (це вже "
            f"WMLES). Для чесного LES потрібен y+ ≈ 1, а сітка тоді в рази "
            f"більша."), [FIX_Y1],
            short=f"y+ ≈ {ys}: {model} стає WMLES (треба ≈ 1)")

    if fam == "lowre" or (treatment == LOWRE and fam != "ke"):
        what = (f"модель {model} має пристінне демпфування"
                if fam == "lowre" else "nutLowReWallFunction")
        # A chosen low-Re wall can go back to Spalding; a low-Re model cannot.
        alt = [FIX_SPALDING] if fam != "lowre" else []
        if y <= 1.0:
            return mk("ok", f"y+ ≈ {ys} — шар розвʼязано до стінки.")
        if y <= 5.0:
            return mk("warn", (
                f"y+ ≈ {ys}: {what} і хоче y+ ≈ 1. У вʼязкому підшарі ще "
                f"працює, але грубо."), [FIX_Y1] + alt,
                short=f"y+ ≈ {ys} грубо для {model} (треба ≈ 1)")
        return mk("error", (
            f"Шар розвʼязується до стінки ({what}), тому потрібен y+ ≈ 1. "
            f"При y+ ≈ {ys} перша комірка вже поза вʼязким підшаром, і тертя "
            f"буде хибним."), [FIX_Y1] + alt,
            short=f"y+ ≈ {ys} не для {model} (треба ≈ 1)")

    # From here on: the wall is modelled (ke, sa, komega).
    high = None
    if y > 1000.0:
        high = mk("error", (
            f"y+ ≈ {ys} — далеко за лог-шаром: закон стінки тут не діє, "
            f"тертя недостовірне. Цільтеся в 30–150."), [FIX_Y50],
            short=f"y+ ≈ {ys}: далеко за лог-шаром")
    elif y > 300.0:
        high = mk("warn", (
            f"y+ ≈ {ys} — зависоко: перша комірка на краю лог-шару, точність "
            f"тертя падає. Цільтеся в 30–150."), [FIX_Y50],
            short=f"y+ ≈ {ys}: зависоко (треба 30–150)")
    if high is not None:
        return high
    if y >= 30.0:
        tail = "" if y <= 150.0 else " Допустимо, але 30–150 точніше."
        return mk("ok", f"y+ ≈ {ys} — лог-шар, закон стінки працює.{tail}")

    buffer_text = (
        f"y+ ≈ {ys} — буферний шар (5–30): ні вʼязкий підшар, ні лог-шар, "
        f"найгірша зона для закону стінки.")

    if fam == "ke":
        if y < 5.0:
            return mk("warn", (
                f"{model} не має пристінного демпфування: дрібна перша "
                f"комірка (y+ ≈ {ys}) їй не допомагає, тертя однаково рахує "
                f"закон стінки. Цільтеся в 30–150 або візьміть kOmegaSST, "
                f"якщо потрібен розвʼязаний шар."), [FIX_Y50, FIX_SST],
                short=f"y+ ≈ {ys}: {model} хоче 30–150")
        return mk("warn", buffer_text + " Цільтеся в 30–150.", [FIX_Y50],
                  short=f"y+ ≈ {ys}: буферний шар")

    if treatment == SPALDING:
        if y <= 5.0:
            return mk("ok", (
                f"y+ ≈ {ys} — вʼязкий підшар. nutUSpalding і "
                f"omegaWallFunction тут працюють, це точніший варіант."))
        if fam == "sa":
            return mk("ok", (
                f"y+ ≈ {ys}. SpalartAllmaras з nutUSpalding працює в усьому "
                f"діапазоні, і в буферному шарі теж."))
        return mk("warn", buffer_text + (
            " nutUSpalding його зшиває, тож рахунок не зламається, але "
            "точність тут найменша. Краще y+ ≈ 1 або 30–150."),
            [FIX_Y1, FIX_Y50], short=f"y+ ≈ {ys}: буферний шар")

    # Standard log-law wall function.
    if y < 5.0:
        return mk("warn", (
            f"y+ ≈ {ys}: nutkWallFunction вважає, що перша комірка в "
            f"лог-шарі, а вона у вʼязкому підшарі. Цільтеся в 30–150 або "
            f"візьміть неперервну функцію (nutUSpalding)."),
            [FIX_Y50, FIX_SPALDING],
            short=f"y+ ≈ {ys}: замало для закону стінки")
    return mk("warn", buffer_text + " Цільтеся в 30–150.",
              [FIX_Y50, FIX_SPALDING], short=f"y+ ≈ {ys}: буферний шар")


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
        return (f"{model} — модель переходу: цільтеся в y+ ≈ 1 на всьому "
                f"тілі (не більше 1–2). З пристінною функцією вона безсила.")
    if fam == "les":
        return (f"{model} — LES: для розвʼязаних пристінних вихорів y+ ≈ 1. "
                f"Більший y+ робить з неї WMLES.")
    if fam == "lowre" or (treatment == LOWRE and fam != "ke"):
        return f"{model}: шар розвʼязується до стінки — цільтеся в y+ ≈ 1."
    if fam == "ke":
        return (f"{model} — лише з законом стінки: y+ 30–150 (до 300). "
                f"Дрібніша стінка їй не допоможе, а 5–30 — буферний шар.")
    if treatment == SPALDING and fam == "sa":
        return (f"{model} з nutUSpalding працює від y+ ≈ 1 до ~300: точніше "
                f"біля 1, дешевше 30–150.")
    if treatment == SPALDING:
        return (f"{model} з nutUSpalding: або y+ ≤ 5 (шар розвʼязано, "
                f"точніше), або 30–150 (до 300, дешевше). 5–30 — буферний "
                f"шар, найменш точна зона.")
    return (f"{model} із законом стінки: y+ 30–150 (до 300). Не ставте 5–30 "
            f"— це буферний шар.")


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
        return 1.0, f"{model} потребує розвʼязаного шару"
    if fam == "les":
        return 1.0, f"{model}: LES розвʼязує вихори біля стінки"
    if fam == "lowre" or (treatment == LOWRE and fam != "ke"):
        return 1.0, f"{model}: шар розвʼязується до стінки"

    top = log_layer_top(u, length, nu)
    if top >= LOG_WINDOW_MIN:
        # Geometric middle of 30..top, so that the cell stays in the log
        # layer also where the local friction differs from the mean.
        y = max(30.0, min(100.0, math.sqrt(30.0 * top)))
        y = round(y / 5.0) * 5.0
        return y, f"{model}: закон стінки, середина лог-шару 30–{top:.0f}"
    if fam == "ke" or treatment == WALLFN:
        return 30.0, (f"{model}: лог-шар при цьому Re вузький (до y+ ≈ "
                      f"{top:.0f}), тому нижній край")
    return 1.0, (f"лог-шар при цьому Re завузький (до y+ ≈ {top:.0f}) — "
                 f"{model} з nutUSpalding краще розвʼязати до стінки")


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
    if n % 10 == 1 and n % 100 != 11:
        return f"{n} шар"
    if 2 <= n % 10 <= 4 and not 12 <= n % 100 <= 14:
        return f"{n} шари"
    return f"{n} шарів"


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
        pick.problem = "Немає швидкості, вʼязкості або опорної довжини."
        return pick

    target, reason = best_yplus(model, treat, f.u_inf, L, f.nu)
    pick.target, pick.reason = target, reason
    if not cfg.mesh.add_layers:
        pick.skipped = True
        pick.level = "warn"
        pick.problem = ("Шари вимкнено — «Авто» їх не підбирає. Увімкніть "
                        "«Нарощувати пограншар».")
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
        pick.problem = "Не вдалося підібрати шари: перевірте швидкість і Lref."
        return pick
    n, e, y, reached = got

    # A wall-function target, but even one layer is far below it: the
    # surface cell is so small that the wall can be resolved for little.
    fam = model_family(model)
    if reached and target >= 30.0 and n == 1 and y < 0.6 * target:
        if fam in ("komega", "sa") and treat == SPALDING:
            target = 1.0
            reason = (f"комірка на поверхні така дрібна, що й один шар дає "
                      f"y+ ≈ {_g(y)} — дешевше розвʼязати шар до стінки")
            pick.target, pick.reason = target, reason
            got = solve(target)
            n, e, y, reached = got

    pick.n_layers, pick.expansion, pick.yplus = n, e, y
    pick.first_abs = stack(n, e).first_abs

    if not reached:
        pick.level = "error" if _resolved_family(model, treat) else "warn"
        pick.problem = (
            f"Цільового y+ ≈ {_g(target)} не досягти: {plural_layers(n)} з "
            f"expansionRatio {e:.2f} дають лише y+ ≈ {_g(y)}. Перший шар "
            f"упирається в комірку на поверхні ({phys.fmt_len(cell)}): "
            f"підніміть рівень поверхні або зменшіть finalLayerThickness.")

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
            why = (f"оцінка {phys.fmt_cells(est.total)} комірок більша за "
                   f"maxGlobalCells {phys.fmt_cells(cfg.mesh.max_global_cells)}")
    if why:
        if target <= 2.0:
            pick.level = "error"
            alt = ("" if fam == "ke" else
                   " Або перейдіть на kOmegaSST із законом стінки (y+ ≈ 50): "
                   "шарів буде в кілька разів менше.")
            pick.problem = (
                f"Розвʼязаний шар (y+ ≈ 1, {plural_layers(n)}) не влазить: "
                f"{why.lstrip('⚠ ')}. Дайте більше памʼяті (машини розподілу, "
                f"maxGlobalCells) або грубішу поверхню.{alt}")
            pick.fixes = [FIX_SST] if fam in ("transition", "les",
                                               "lowre") else []
        else:
            pick.level = "warn"
            pick.problem = f"Сітка не влазить: {why.lstrip('⚠ ')}."
    return pick


def auto_line(pick: AutoPick, model: str) -> str:
    """The result line under the y+ field, one short line."""
    if pick.skipped:
        return "Авто: " + pick.problem
    line = (f"Авто: y+ ≈ {_g(pick.target)} ({pick.reason}) → "
            f"{plural_layers(pick.n_layers)}, 1-й "
            f"{phys.fmt_len(pick.first_abs)}")
    if abs(pick.expansion - 1.2) > 1e-9:
        line += f", expansionRatio {pick.expansion:.2f}"
    if pick.yplus and abs(pick.yplus - pick.target) > 0.3 * pick.target:
        line += f", вийде y+ ≈ {_g(pick.yplus)}"
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
    return [("warn", c.text + (f" Що зробити: {fix}." if fix else ""))]
