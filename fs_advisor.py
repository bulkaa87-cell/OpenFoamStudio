"""Помічник: рахує налаштування сітки й розвʼязувача з геометрії та фізики.

Логіка тут навмисно відокремлена від інтерфейсу — її можна ганяти тестами.
Кожна порада несе причину: без пояснення користувач не знає, коли її можна
порушити, а порушувати доводиться постійно.

Порядок міркування такий самий, як робить це людина:

  1. від довжини тіла -> потрібний розмір комірки на поверхні;
  2. від нього -> рівень рефайнменту і розмір фонової комірки;
  3. перевірка, чи влізе в памʼять, і зниження рівня, якщо ні;
  4. від швидкості й цільового y+ -> кількість призматичних шарів;
  5. від стану STL -> чи взагалі варто нарощувати шари;
  6. від швидкості й якості сітки -> налаштування розвʼязувача.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field

import fs_case as fc
import fs_memfit as memfit
import fs_physics as phys
import fs_yplus as fy
from fs_i18n import tr

# Скільки комірок на довжину тіла хочемо на поверхні. 150 — робочий компроміс
# між «видно форму» і «влазить у памʼять» для зовнішньої аеродинаміки.
CELLS_PER_BODY_LENGTH = 150

# Нижня межа подрібнення. Навіть якщо STL грубий, сітка дрібніша за трикутники
# все одно корисна для сліду й пограншару, тому нижче цього не опускаємось.
MIN_CELLS_PER_LENGTH = 80

# Пристінні функції нормально працюють у лог-шарі. 5..30 — буферний шар, туди
# цілитися не можна: там і закон стінки не діє, і розвʼязок залежить від сітки.
TARGET_YPLUS = 60.0

# Яку частку памʼяті WSL дозволяємо зайняти сітці.
#
# 0.6, а не 0.75: памʼять WSL — це не вся памʼять машини. Паралельно живуть
# сам Windows, це вікно з VTK і PyVista, і віртуальна машина WSL уміє роздути
# свій обсяг під тиском. Прогін, у якому сітка передбачала 0.6 ГБ, система
# зняла саме через брак памʼяті на боці Windows, а не всередині WSL.
# Тепер це fs_memfit.RAM_HEADROOM_SHARED; ім'я лишаємо для старих викликів.
RAM_HEADROOM = memfit.RAM_HEADROOM_SHARED


@dataclass
class Tip:
    """Одна порада."""
    where: str            # вкладка, до якої належить
    what: str             # назва параметра
    current: str
    suggested: str
    reason: str
    level: str = "info"   # info | warn | bad

    @property
    def changed(self) -> bool:
        return self.current != self.suggested


@dataclass
class Plan:
    cfg: fc.CaseCfg
    tips: list[Tip] = field(default_factory=list)
    notes: list[tuple[str, str]] = field(default_factory=list)

    @property
    def changes(self) -> list[Tip]:
        return [t for t in self.tips if t.changed]


def auto_layer_cap(diag) -> int:
    """Layer limit for the auto y+ pick: the same cap as below on messy
    geometry (layers grow in patches there), else the spin maximum."""
    return 5 if _is_assembly(diag) else fy.LAYER_CAP


def _is_assembly(diag) -> bool:
    """Чи це збірка з окремих деталей, а не одне чисте тіло.

    Один критерій на всі рішення: раніше кромки й шари користувалися різними
    порогами, і виходило, що та сама модель для однієї поради «брудна», а для
    іншої «чиста». Ознаки рівноправні — досить будь-якої.
    """
    if diag is None:
        return False
    return bool(diag.parts > 5
                or diag.non_manifold_edges > 50
                or diag.open_edges > 20
                or diag.bad_winding > 0
                or diag.flipped_fraction > 0.25)


def _fmt(v) -> str:
    if isinstance(v, float):
        return f"{v:.4g}"
    return str(v)


def advise(cfg: fc.CaseCfg, diag, ram_gb: float = 8.0,
           cores: int = 6, target_yplus: "float | None" = TARGET_YPLUS,
           mem=None, layer_fit=None) -> Plan:
    """Повертає план: копію конфігу з порадами і список пояснень.

    `mem` — функція cfg -> fs_memfit.MemFit, та сама, що в рядку стану й у
    перевірці перед запуском (машини, збережена сітка, розподіл). Без неї —
    одна машина з `ram_gb` і запасом для WSL.

    `target_yplus` None means «Авто»: the y+ and the layers come from
    fs_yplus.auto_pick for the turbulence model, the same as the «Сітка»
    panel does, so that the auto pick after «Застосувати все» gives the same
    numbers. `layer_fit` is the memory check for that pick (cfg -> reason).
    """
    import copy

    if mem is None:
        host = memfit.MemHost("this computer", ram_gb, RAM_HEADROOM)
        mem = lambda c: memfit.memory_fit(c.estimate(), host)   # noqa: E731

    new = copy.deepcopy(cfg)
    tips: list[Tip] = []
    notes: list[tuple[str, str]] = []

    L = cfg.body_length
    span = cfg.body_span
    height = cfg.body_height

    # ---------------------------------------------------------------- домен
    # Для тіла з великим подовженням слід довший: вихор із законцівки треба
    # встигнути «випустити» з домену, інакше вихід ріже його впоперек.
    slender = span / max(L, 1e-9)
    downstream = 15.0 if slender > 1.5 else 12.0
    _add(tips, "Domain", tr("Downstream", ctx="domain"), cfg.domain.downstream, downstream,
         ("the wake behind a body with a large span is longer — we use 15 lengths"
          if slender > 1.5 else "12 lengths are enough for the wake to settle"))
    new.domain.downstream = downstream

    _add(tips, "Domain", tr("Upstream", ctx="domain"), cfg.domain.upstream, 5.0,
         "5 lengths upstream: a closer inlet already affects the pressure near the nose")
    new.domain.upstream = 5.0

    # ------------------------------------------------- комірка на поверхні
    target_surface = L / CELLS_PER_BODY_LENGTH

    # Дрібніше за тесселяцію STL немає сенсу: snappy однаково бачить поверхню
    # з точністю до трикутника.
    #
    # Міряємо це НАЙКОРОТШИМ ребром трикутника, а не медіанним. Різниця не
    # академічна: у лофтованого крила трикутники витягнуті вздовж розмаху,
    # і медіанне ребро показує крок між перерізами. На літаку з тестів воно
    # виходило 18.7 мм при хорді 140 мм — і порадник казав «дрібніша сітка
    # вже не додасть форми» там, де передня кромка описана з кроком 2 мм і
    # дрібніша сітка була потрібна найбільше. Коротке ребро для того самого
    # файлу — 2.3 мм, і відповідь стає правильною.
    stl_edge = 0.0
    if diag is not None:
        # Злі́пки, зняті до появи short_edge, його не мають — тоді лишається
        # медіанне, як і було.
        stl_edge = getattr(diag, "short_edge", 0.0) or diag.median_edge
    if stl_edge > 0:
        # Тесселяція обмежує ГЕОМЕТРИЧНУ вірність, але не корисність дрібнішої
        # сітки: навіть на фасетованому тілі дрібніша комірка краще розвʼязує
        # слід і пограншар. Тому клампуємо лише до половини трикутника і НІКОЛИ
        # не опускаємося нижче MIN_CELLS_PER_LENGTH — інакше груба тестова
        # модель зводила всі поради до однакових 25 тис. комірок незалежно від
        # розміру тіла, швидкості й доступної памʼяті.
        floor_by_stl = min(stl_edge * 0.5, L / MIN_CELLS_PER_LENGTH)
        if target_surface < floor_by_stl:
            target_surface = floor_by_stl
            cells_per_L = L / max(target_surface, 1e-12)
            if stl_edge * 0.5 > L / MIN_CELLS_PER_LENGTH:
                # Тесселяція настільки груба, що обмежує не точність, а сенс:
                # на 24 комірки вздовж тіла форми крила вже не видно.
                notes.append(("bad", (
                    tr("The STL is too coarse: its tessellation allows "
                       "only ~{cells_per_L:.0f} cells per body length, "
                       "and aerodynamics needs 100 or more. Click "
                       "“Refine” on the “Geometry” tab or save the STL "
                       "again with a smaller deviation.", cells_per_L=cells_per_L))))
            else:
                notes.append(("info", (
                    tr("The cell size is limited from below by the STL "
                       "tessellation ({stl_edge} short triangle edge): a "
                       "finer mesh will not add shape any more, only "
                       "time.", stl_edge=phys.fmt_len(stl_edge)))))

    # Дірки: комірка має бути БІЛЬШОЮ за найбільшу дірку, інакше протече.
    if diag is not None and diag.hole_sizes:
        biggest = diag.largest_hole
        if biggest > target_surface:
            notes.append(("bad", (
                tr("The largest hole {biggest} is larger than the "
                   "required cell {target_surface}. The mesh will leak. "
                   "Either patch the STL, or make the surface coarser on "
                   "purpose to {fmt_len} — but then the shape will blur.", biggest=phys.fmt_len(biggest), target_surface=phys.fmt_len(target_surface), fmt_len=phys.fmt_len(biggest * 1.5)))))

    # -------------------------------------------- рівень і фонова комірка
    level = 5
    base_cell = target_surface * (2 ** level)
    n_x = int(round(cfg.domain_size[0] / base_cell)) if base_cell > 0 else 40
    n_x = max(16, min(120, n_x))

    new.domain.base_cells_along_x = n_x
    new.mesh.surface_level_max = level
    new.mesh.surface_level_min = level - 1
    new.mesh.feature_level = level

    # Коробка згущення: тіло зі слідом.
    new.mesh.boxes = [new.default_box(factor=1.8, level=2, wake=4.0)]

    # ------------------------------------------------- памʼять і зниження
    for _ in range(4):
        fit = mem(new)
        if fit.verdict != "over" or new.mesh.surface_level_max <= 3:
            break
        w = fit.worst
        new.mesh.surface_level_max -= 1
        new.mesh.surface_level_min = max(1, new.mesh.surface_level_max - 1)
        new.mesh.feature_level = new.mesh.surface_level_max
        what = "snappyHexMesh" if w.stage == "snappy" else tr("solver")
        notes.append(("warn", (
            tr("Refinement level lowered to {surface_level_max}: at the "
               "previous level, {what} did not fit on {name} "
               "(~{need_gb:.1f} GB, with {budget_gb} of {ram_gb} GB "
               "available for the job).", surface_level_max=new.mesh.surface_level_max, what=what, name=w.host.name, need_gb=w.need_gb, budget_gb=memfit.fmt_gb(w.host.budget_gb), ram_gb=memfit.fmt_gb(w.host.ram_gb)))))

    _add(tips, "Mesh", "Surface level", cfg.mesh.surface_level_max,
         new.mesh.surface_level_max,
         tr("surface cell {surface_cell} ≈ L/{v:.0f}", surface_cell=phys.fmt_len(new.surface_cell), v=L / max(new.surface_cell, 1e-12)))
    _add(tips, "Domain", "Cells along X", cfg.domain.base_cells_along_x, n_x,
         tr("background cell {base_cell}; level {surface_level_max} "
            "brings it to the required size", base_cell=phys.fmt_len(new.base_cell), surface_level_max=new.mesh.surface_level_max))

    # ----------------------------------------------------- кромки й шари
    dirty = _is_assembly(diag)
    included = 170 if dirty else 150
    _add(tips, "Mesh", "includedAngle", cfg.mesh.included_angle, included,
         ("on an assembly of intersecting parts, a smaller angle gives junk edges"
          if dirty else "150° — the standard for clean geometry"))
    new.mesh.included_angle = included

    auto = target_yplus is None
    if auto:
        target_yplus, _ = fy.best_yplus(
            cfg.flow.turbulence, fy.cfg_treatment(cfg), cfg.flow.u_inf,
            cfg.flow.l_ref or L,
            cfg.flow.nu)
    n_layers = phys.layers_for_target_yplus(
        target_yplus, cfg.flow.u_inf, cfg.flow.l_ref or L, cfg.flow.nu,
        new.surface_cell, new.mesh.expansion, new.mesh.final_layer_rel)

    if diag is not None and not diag.closed and diag.largest_hole > new.surface_cell:
        # Нема сенсу нарощувати шари на поверхні, крізь яку тече.
        new.mesh.add_layers = False
        _add(tips, "Mesh", "Layers", cfg.mesh.n_layers, 0,
             "while the STL leaks, layers will not grow — fix the geometry first",
             level="warn")
    else:
        new.mesh.add_layers = True
        delta = phys.bl_thickness(cfg.flow.u_inf, cfg.flow.l_ref or L, cfg.flow.nu)

        # Три важелі, і вони конфліктують. Щоб перша комірка потрапила в
        # цільовий y+, шар біля стінки має бути тонким; щоб дотягнутися від
        # нього до поверхневої комірки, потрібно багато шарів; але сумарний
        # стек не має вилазити за пограншар, бо призми в зовнішньому потоці
        # нічого не розвʼязують і псують перехід до гексаедрів.
        #
        # Виходить із цього finalLayerThickness: зменшуючи останній шар, ми
        # робимо весь стек тоншим, не жертвуючи тонкістю першого. Тому
        # перебираємо його від щедрого 0.5 до скупого 0.15 і беремо перший
        # варіант, у якому і y+ близький до цілі, і стек влазить у пограншар.
        # ВИМІРЯНО на plane5: чим більше шарів просиш, тим гірше покриття.
        #   3 шари  -> наросло на 78.6% граней (2.36 шару)
        #   5 шарів -> 65.2% (3.26)
        #  10 шарів -> 36.6% (3.66)
        # Плямисті шари гірші за рівні тонкі: там, де шару немає, перша комірка
        # дорівнює поверхневій, і локальний y+ злітає в сотні. Тому на брудній
        # геометрії ставимо жорстку стелю, навіть якщо теорія просить більше.
        messy = _is_assembly(diag)
        layer_cap = 5 if messy else 25
        if messy:
            notes.append(("info", (
                tr("No more than {layer_cap} layers: the geometry has "
                   "{parts} parts and {non_manifold_edges} non-manifold "
                   "edges. On such a surface snappy grows layers in "
                   "patches, and the more layers you ask for, the fewer "
                   "faces get them (measured: 3 layers give 79% "
                   "coverage, 10 layers — 37%).", layer_cap=layer_cap, parts=diag.parts, non_manifold_edges=diag.non_manifold_edges))))

        best = None
        for final_rel in (0.5, 0.4, 0.3, 0.25, 0.2, 0.15):
            n = phys.layers_for_target_yplus(
                target_yplus, cfg.flow.u_inf, cfg.flow.l_ref or L, cfg.flow.nu,
                new.surface_cell, new.mesh.expansion, final_rel)
            n = max(3, min(n, layer_cap))
            st = phys.LayerStack(n, new.mesh.expansion, final_rel,
                                 new.surface_cell)
            yp = st.yplus(cfg.flow.u_inf, cfg.flow.l_ref or L, cfg.flow.nu)
            fits = delta <= 0 or st.total <= delta * 1.2
            score = abs(math.log10(max(yp, 1e-6) / max(target_yplus, 1e-6)))
            if fits and (best is None or score < best[0]):
                best = (score, n, final_rel, yp)

        if best is not None:
            _, new.mesh.n_layers, new.mesh.final_layer_rel, _ = best
        else:
            # Жоден варіант не влазить у пограншар — беремо мінімум шарів.
            new.mesh.n_layers = 3
        if auto:
            # The count and the expansion as «Авто» will pick them for the
            # chosen finalLayerThickness.
            pick = fy.auto_pick(new, fits=layer_fit,
                                layer_cap=auto_layer_cap(diag))
            if not pick.skipped:
                new.mesh.n_layers, new.mesh.expansion = (pick.n_layers,
                                                         pick.expansion)
                new.mesh.target_yplus = target_yplus = pick.target
                notes.append(("info", fy.auto_line(
                    pick, cfg.flow.turbulence)))
            if pick.problem:
                notes.append(("bad" if pick.level == "error" else "warn",
                              pick.problem))
            if abs(new.mesh.expansion - cfg.mesh.expansion) > 1e-9:
                _add(tips, "Mesh", "expansionRatio", cfg.mesh.expansion,
                     new.mesh.expansion,
                     "otherwise the first layer will not reach the "
                     "required y+ within the number of layers")

        got_yplus = _predicted_yplus(new, target_yplus)
        if got_yplus > target_yplus * 4:
            notes.append(("warn", (
                tr("The target y+ {target_yplus:.0f} cannot be reached: "
                   "the result is {got_yplus:.0f}. The first layer is "
                   "limited by the surface cell size ({surface_cell}). "
                   "Raise the refinement level or decrease "
                   "finalLayerThickness from {final_layer_rel:.2f} to "
                   "0.2.", target_yplus=target_yplus, got_yplus=got_yplus, surface_cell=phys.fmt_len(new.surface_cell), final_layer_rel=new.mesh.final_layer_rel))))

        _add(tips, "Mesh", "finalLayerThickness", cfg.mesh.final_layer_rel,
             new.mesh.final_layer_rel,
             "a thinner last layer makes the whole stack thinner without "
             "giving up a thin first layer — this way y+ and the stack "
             "thickness agree")
        _add(tips, "Mesh", tr("Layers", ctx="count"), cfg.mesh.n_layers, new.mesh.n_layers,
             tr("gives y+ ≈ {new:.0f} at the target {target_yplus:.0f}; "
                "the stack is {u_inf:.0%} of the boundary layer", new=_predicted_yplus(new, target_yplus), target_yplus=target_yplus, u_inf=new.layer_stack.covers_bl(cfg.flow.u_inf, cfg.flow.l_ref or L, cfg.flow.nu)))

    # ------------------------------------------------------- розвʼязувач
    est = new.estimate()
    # Два ядра лишаємо системі: інакше інтерфейс і сам WSL починають голодувати
    # і прогін від цього не пришвидшується.
    usable = cores - 2 if cores > 4 else cores
    n_procs = max(1, min(usable, max(1, est.total // 40_000)))
    _add(tips, "Solver", tr("Processes", ctx="count"), cfg.solver.n_procs, n_procs,
         tr("{replace} cells per process; of {cores} cores, two are left to the system", replace=f"~{est.total // max(n_procs, 1):,}".replace(",", " "), cores=cores))
    new.solver.n_procs = n_procs

    iters = 1000 if est.total < 1_000_000 else 2000
    _add(tips, "Solver", "Iterations", cfg.solver.n_iterations, iters,
         "steady SIMPLE on such a mesh usually converges sooner — "
         "residualControl will stop it")
    new.solver.n_iterations = iters
    new.solver.write_interval = iters

    if not cfg.solver.steady:
        # У нестаціонарі «скільки рахувати» задається в секундах, а секунди
        # залежать від розміру тіла й швидкості. Тому рахуємо з прольоту
        # домену, а не пропонуємо число з голови.
        flight = new.flow_through_time
        end = _round_nice(flight * 4)
        _add(tips, "Solver", "End time", cfg.solver.end_time, end,
             tr("four domain flow-throughs ({flight:.3g} s each): the "
                "first one is for the flow to reach the body, the rest "
                "are for the wake to form", flight=flight))
        new.solver.end_time = end

        every = _round_nice(end / 40.0)
        _add(tips, "Solver", "Write every", cfg.solver.write_every, every,
             "forty time steps — enough to see the motion, and not so "
             "many that they fill the disk")
        new.solver.write_every = every

        # Початковий крок беремо від комірки на поверхні: Куранта на ній і
        # вирішує, чи не зірветься перший крок, поки adjustTimeStep ще не
        # встиг нічого підібрати.
        dt = _round_nice(new.surface_cell / max(new.flow.u_inf, 1e-9) * 0.5)
        dt = min(dt, every / 10.0)
        _add(tips, "Solver", "Initial time step", cfg.solver.time_step, dt,
             "half a Courant number on the surface cell — after that "
             "adjustTimeStep controls the step itself")
        new.solver.time_step = dt

    # Двоетапність: що швидший потік, то довший стійкий розгін.
    u = cfg.flow.u_inf
    robust = 150 if u <= 40 else (300 if u <= 90 else 500)
    _add(tips, "Solver", "Robust iterations", cfg.solver.robust_iterations,
         robust,
         tr("at {u:.0f} m/s, typical second-order schemes diverge in the "
            "first few dozen iterations; we run this many first "
            "iterations with upwind", u=u))
    new.solver.robust_iterations = robust

    # ------------------------------------------------------------ нотатки
    _flow_notes(new, diag, notes, target_yplus, mem(new))
    return Plan(cfg=new, tips=tips, notes=notes)


def _round_nice(value: float) -> float:
    """Округлює до 1, 2 або 5 у відповідному порядку.

    Поради в секундах інакше виглядають як 0.7314285714: число правильне, а
    довіри не викликає, і людина не може його повторити з голови.
    """
    if value <= 0:
        return value
    order = 10.0 ** math.floor(math.log10(value))
    for step in (1.0, 2.0, 5.0, 10.0):
        if value <= step * order * 1.2:
            return step * order
    return 10.0 * order


def _predicted_yplus(cfg: fc.CaseCfg, fallback: float) -> float:
    st = cfg.layer_stack
    if not st.n_layers:
        return fallback
    return st.yplus(cfg.flow.u_inf, cfg.flow.l_ref or cfg.body_length, cfg.flow.nu)


def _add(tips: list[Tip], where: str, what: str, current, suggested,
         reason: str, level: str = "info") -> None:
    # Translated here: the tips go to tables and texts in several places.
    tips.append(Tip(where=tr(where), what=tr(what), current=tr(_fmt(current)),
                    suggested=tr(_fmt(suggested)), reason=tr(reason),
                    level=level))


def _flow_notes(cfg: fc.CaseCfg, diag, notes: list, target_yplus: float,
                fit: "memfit.MemFit") -> None:
    f = cfg.flow
    L = f.l_ref or cfg.body_length
    re = phys.reynolds(f.u_inf, L, f.nu)
    ma = f.mach
    est = cfg.estimate()

    notes.append(("info", (
        tr("Re = {re:.3g}, M = {ma:.2f}, mesh estimate {total} cells. "
           "Memory: {join}.", re=re, ma=ma, total=phys.fmt_cells(est.total), join="; ".join(ln.strip() for ln
                                   in fit.details().splitlines()[1:])))))
    if fit.verdict == "unknown":
        notes.append(("warn", fit.warning().lstrip("ℹ ")))

    if ma > 0.3 and not f.compressible:
        notes.append(("warn", (
            tr("M = {ma:.2f}: compressibility is already noticeable, but "
               "simpleFoam does not see it and underestimates drag. "
               "Switch the flow mode to compressible — the case will go "
               "through rhoSimpleFoam.", ma=ma))))
    elif f.compressible and ma < 0.3:
        # Не помилка, але й не безкоштовно: стисливий SIMPLE релаксує густину
        # на 0.01, і на ту саму збіжність йому треба в рази більше ітерацій.
        notes.append(("info", (
            tr("M = {ma:.2f}: compressibility changes nothing here, and "
               "a compressible solver converges noticeably slower. If "
               "the speed does not increase, the incompressible mode "
               "will give the same result faster.", ma=ma))))
    elif f.compressible and ma > 0.8:
        notes.append(("warn", (
            tr("M = {ma:.2f}: this is already transonic. rhoSimpleFoam "
               "is steady and not reliable on shock waves — this needs "
               "rhoCentralFoam, which is not in this version.", ma=ma))))

    # Модель переходу має сенс лише на розвʼязаному пограншарі: вона рахує,
    # ДЕ ламінарне тече в турбулентне, а пристінна функція саме цю ділянку й
    # перестрибує. Тому тут скрізь фігурує y+, а не сам факт малого Re.
    yp_now = _predicted_yplus(cfg, target_yplus)
    resolved = yp_now <= 2.0

    # y+ against the model: the same rules as the «Сітка» panel and the
    # check before a run (fs_yplus).
    chk = fy.check_yplus(f.turbulence, fy.cfg_treatment(cfg), target_yplus,
                         yp_now)
    if not chk.ok:
        fix = "; ".join(label for _, _, label in chk.fixes)
        notes.append(("bad" if chk.level == "error" else "warn",
                      chk.text + (tr(" What to do: {fix}.", fix=fix) if fix else "")))
    if (phys.is_transitional(re)
            and fy.model_family(f.turbulence) != "transition"):
        common = (tr("Re = {re:.2g} is below 5·10⁵: the boundary layer "
                     "is partly laminar, but RAS treats it as turbulent "
                     "everywhere and overestimates skin friction drag.", re=re))
        if resolved:
            notes.append(("warn", tr("{common} The mesh already resolves "
                                     "the wall (y+ ≈ {yp_now:.1f}), so "
                                     "here it is better to use "
                                     "kOmegaSSTLM — it calculates the "
                                     "transition instead of ignoring it.", common=common, yp_now=yp_now)))
        else:
            notes.append(("warn", tr("{common} kOmegaSSTLM can fix this, "
                                     "but it needs y+ ≈ 1, and now the "
                                     "result is {yp_now:.0f}: first make "
                                     "the first layer finer, and only "
                                     "then use the transition model.", common=common, yp_now=yp_now)))

    if cfg.mesh.add_layers:
        yp = _predicted_yplus(cfg, target_yplus)
        label, lvl = phys.yplus_regime(yp)
        st = cfg.layer_stack
        cover = st.covers_bl(f.u_inf, L, f.nu)
        # Скільки звичайних комірок лишається на решту пограншару над стеком.
        delta = phys.bl_thickness(f.u_inf, L, f.nu)
        rest = max(delta - st.total, 0.0)
        cells_above = rest / cfg.surface_cell if cfg.surface_cell > 0 else 0

        msg = (tr("Expected y+ ≈ {yp:.0f} — {label}. The layer stack "
                  "{total} covers {cover:.0%} of the boundary layer "
                  "({delta}), the rest is held by ~{cells_above:.1f} "
                  "regular cells.", yp=yp, label=label, total=phys.fmt_len(st.total), cover=cover, delta=phys.fmt_len(delta), cells_above=cells_above))
        # Тонкий стек — це не помилка, а ціна за потрібний y+: щоб перша
        # комірка була тонкою, увесь стек виходить низьким. Тривожно стає
        # тоді, коли над ним на весь зовнішній пограншар лишається менше
        # двох комірок — тоді профіль швидкості там просто нема на чому
        # розвʼязати.
        level = lvl if lvl != "ok" else "info"
        if cells_above < 2.0 and cover < 0.5:
            level = "warn"
            msg += (tr(" This is too little: raise the surface refinement "
                    "level by one or allow more layers."))
        notes.append((level, msg))
    else:
        notes.append(("warn", (
            tr("Without layers, the first cell is equal to the surface "
            "cell, and y+ will be in the hundreds. Pressure and the wake "
            "shape still make sense, skin friction drag does not."))))

    if diag is not None and diag.bad_winding:
        notes.append(("bad", (
            tr("{bad_winding} edges with inconsistent winding. First use "
               "“Repair” (meshfix method): on such a surface snappy "
               "cannot reliably tell where “inside” is, and the layers "
               "grow in patches.", bad_winding=diag.bad_winding))))
    elif diag is not None and diag.inverted_parts:
        notes.append(("warn", (
            tr("{inverted_parts} parts are turned inside out "
               "({flipped_faces} faces). For layers this is less harmful "
               "than for refinementRegions with mode inside, but one "
               "click on “Repair” (meshfix method) fixes it.", inverted_parts=diag.inverted_parts, flipped_faces=diag.flipped_faces))))


# --------------------------------------------------------------------------
# Плямисті шари: що з ними робити
# --------------------------------------------------------------------------

# Частка граней, які справді отримали призми. Пороги не з голови:
#   gui_wing (чисте крило, одна оболонка)      — 89.7%
#   plane5 (збірка з 44 частин), 3 шари        — 78.6%
#   plane5, 5 шарів                            — 65.2%
#   plane5, 10 шарів                           — 36.6%
# Тобто 0.9 буває лише на чистій геометрії, 0.7 — робочий рівень для збірки,
# нижче 0.5 пограншару фактично немає.
LAYER_FINE = 0.9
LAYER_WORKABLE = 0.7
LAYER_POOR = 0.5


@dataclass
class LayerDiagnosis:
    """Розбір покриття шарами: що сталося, чим це погано, що підкрутити."""

    coverage: float = 0.0
    level: str = "ok"                   # ok | warn | bad
    headline: str = ""
    effect: str = ""
    tips: list[Tip] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    cfg: fc.CaseCfg | None = None       # конфіг із застосованими правками

    @property
    def fixable(self) -> bool:
        return self.cfg is not None and bool(self.tips)

    def report(self) -> str:
        """Текст для рядка стану: заголовок, наслідок, що підкрутити, чому.

        Окремими рядками, а не суцільним абзацом: це читають один раз і
        похапцем, одразу після прогону.
        """
        lines = [self.headline]
        if self.effect:
            lines.append(self.effect)
        if self.tips:
            lines.append(tr("What to adjust: {join}.", join="; ".join(
                f"{t.what} {t.current} → {t.suggested}" for t in self.tips)))
        lines.extend(self.notes)
        return "\n".join(lines)


def layer_diagnosis(cfg: fc.CaseCfg, coverage: float,
                    diag=None) -> LayerDiagnosis:
    """Що робити, коли шари наросли не всюди.

    `coverage` — частка граней із «Added N out of M cells» у логу snappy.
    Саме її раніше показував рядок стану словом «замало», не кажучи ні чого
    замало, ні що з цим робити.

    Порядок правок — за виміряною користю, а не за красою. Найдужче на
    покриття впливає товщина стеку: чим тонший стек, тим у вужчі щілини він
    влазить. Тому спершу шари й пороги товщини, і лише потім — рівень
    рефайнменту, який коштує памʼяті. Ручки самого алгоритму нарощування
    (`featureAngle`, `medialRatio`, згладжування) у списку свідомо відсутні:
    A/B на plane5 дав 36.7% проти 36.4%, тобто вони не лікують.
    """
    import copy

    out = LayerDiagnosis(coverage=coverage)
    m, f = cfg.mesh, cfg.flow
    L = f.l_ref or cfg.body_length
    pct = coverage * 100

    if not m.add_layers or m.n_layers <= 0:
        out.headline = tr("No layers were requested — the boundary layer is on a “bare” mesh.")
        return out

    if coverage >= LAYER_FINE:
        out.headline = tr("Layers grew on {pct:.0f}% of faces — normal.", pct=pct)
        out.notes.append(
            tr("The rest are sharp edges and joints where there is no space for a prism."))
        return out

    # ---- наслідок, у числах ----
    stack = cfg.layer_stack
    yp_layers = stack.yplus(f.u_inf, L, f.nu)
    yp_bare = phys.yplus_for_wall_distance(cfg.surface_cell / 2.0,
                                           f.u_inf, L, f.nu)
    out.effect = (
        tr("Where there are no layers (that is {v:.0f}% of the surface), "
           "the first cell is equal to the surface cell — {surface_cell} "
           "instead of {first_abs}, so the local y+ jumps from "
           "{yp_layers:.0f} to {yp_bare:.0f}. Skin friction drag on "
           "these areas is too low, and the y+ from the “Mesh” tab is "
           "not valid for them.", v=100 - pct, surface_cell=phys.fmt_len(cfg.surface_cell), first_abs=phys.fmt_len(stack.first_abs), yp_layers=yp_layers, yp_bare=yp_bare))

    if coverage >= LAYER_WORKABLE:
        out.level = "warn"
        out.headline = (tr("Layers grew on {pct:.0f}% of faces — usable, "
                           "but not uniform.", pct=pct))
    elif coverage >= LAYER_POOR:
        out.level = "warn"
        out.headline = tr("Layers grew on only {pct:.0f}% of faces — too little.", pct=pct)
    else:
        out.level = "bad"
        out.headline = (tr("Layers grew on only {pct:.0f}% of faces — "
                           "the boundary layer is in fact not resolved.", pct=pct))

    # ---- правки ----
    #
    # Ключове: просто зменшити кількість шарів — погана порада. Перший шар
    # рахується від останнього вниз (`final / expansion^(n-1)`), тому кожен
    # прибраний шар РОБИТЬ ПРИСТІННУ КОМІРКУ ТОВЩОЮ і псує y+ там, де шари
    # якраз наросли. Тому разом із кількістю перераховуємо
    # finalLayerThickness так, щоб перший шар лишився тим самим: стек стає
    # коротшим (це й дає покриття), а роздільність біля стінки не страдає.
    new = copy.deepcopy(cfg)
    tips: list[Tip] = []
    first_before = stack.first_abs

    if m.n_layers > 3:
        want = max(3, m.n_layers - 2 if m.n_layers <= 8 else m.n_layers // 2)
        new.mesh.n_layers = want
        _add(tips, "Mesh", tr("Layers", ctx="count"), m.n_layers, want,
             "the strongest lever: a shorter stack fits into narrower "
             "places. Measured on plane5 — 10 layers gave 37% coverage, "
             "5 layers 65%, 3 layers 79%")

    if m.expansion > 1.3:
        new.mesh.expansion = 1.3
        _add(tips, "Mesh", "expansionRatio", m.expansion, 1.3,
             "growth faster than 1.3 makes the stack long with only a "
             "few layers and gives a sharp jump in size where the stack "
             "ends")

    if tips and cfg.surface_cell > 0:
        # Тримаємо перший шар незмінним: final_rel * cell / exp^(n-1) = const.
        want_final = (first_before * new.mesh.expansion ** (new.mesh.n_layers - 1)
                      / cfg.surface_cell)
        want_final = round(min(max(want_final, 0.05), 1.0), 2)
        if abs(want_final - m.final_layer_rel) >= 0.01:
            new.mesh.final_layer_rel = want_final
            _add(tips, "Mesh", "finalLayerThickness", m.final_layer_rel,
                 want_final,
                 "recalculated for the new number of layers: the first "
                 "layer stays the same, so y+ does not get worse, but "
                 "the stack gets shorter")

    if m.min_thickness_rel > 0.02:
        new.mesh.min_thickness_rel = 0.02
        _add(tips, "Mesh", "minThickness", m.min_thickness_rel, 0.02,
             "a face where the layer comes out thinner than this "
             "threshold, snappy leaves with no layer at all. A lower "
             "threshold gives a thin layer instead of none — at the cost "
             "of more stretched cells, so checkMesh can start to complain")

    out.tips = tips
    out.cfg = new if tips else None

    if tips:
        after = new.layer_stack
        if after.total < stack.total * 0.95:
            out.notes.append(
                tr("After the changes: first layer {first_abs} (was "
                   "{first_before}), so y+ ≈ {u_inf:.0f} instead of "
                   "{yp_layers:.0f} — almost the same, but the stack "
                   "gets shorter from {total} to {total2}. This is "
                   "exactly what gives the coverage.", first_abs=phys.fmt_len(after.first_abs), first_before=phys.fmt_len(first_before), u_inf=after.yplus(f.u_inf, L, f.nu), yp_layers=yp_layers, total=phys.fmt_len(stack.total), total2=phys.fmt_len(after.total)))
        else:
            out.notes.append(
                tr("The stack does not get shorter with this — only the "
                   "lower threshold works: faces where the layer came "
                   "out thinner than {min_thickness_rel:g} of a cell and "
                   "so did not appear at all will now get a thin layer.", min_thickness_rel=m.min_thickness_rel))

    # ---- причини, яких ручками не полагодити ----
    if _is_assembly(diag):
        out.notes.append(
            tr("The main cause here is not the settings: the geometry "
               "has {parts} parts and {non_manifold_edges} non-manifold "
               "edges. Along the lines where the shells cut into each "
               "other, there is no place to grow a layer. A/B tested: "
               "featureAngle 60→130 and medialRatio 0.3→0.6 gave 36.7% "
               "vs 36.4% — these knobs do not fix it. What fixes it is "
               "joining the parts into one body (Boolean union in CAD or "
               "shrink wrap).", parts=diag.parts, non_manifold_edges=diag.non_manifold_edges))
    if diag is not None and not diag.closed and diag.largest_hole > cfg.surface_cell:
        out.notes.append(
            tr("The surface leaks through holes larger than a cell — while "
            "this is so, layers will not grow at all. Start with the "
            "“Geometry” tab."))
    if not tips:
        out.notes.append(
            tr("Nothing is left to adjust: {n_layers} layers, "
               "minThickness {min_thickness_rel:g}, finalLayerThickness "
               "{final_layer_rel:g} — all at the minimum. What remains "
               "is to raise the surface refinement level (the cell will "
               "become smaller, and the stack with it — but there will "
               "be four times as many cells) or to repair the geometry.", n_layers=m.n_layers, min_thickness_rel=m.min_thickness_rel, final_layer_rel=m.final_layer_rel))
    if out.level == "bad":
        out.notes.append(
            tr("If this does not help, what remains is the surface "
               "refinement level {surface_level_max} → {v}: the cell "
               "will become {fmt_len} instead of {surface_cell}, and the "
               "stack will become half as thick with it. The cost is "
               "about four times as many cells.", surface_level_max=m.surface_level_max, v=m.surface_level_max + 1, fmt_len=phys.fmt_len(cfg.surface_cell / 2), surface_cell=phys.fmt_len(cfg.surface_cell)))
        out.notes.append(
            tr("The “give up” option also works: turn off layers fully. The "
            "wall function handles a uniform high y+ over the whole "
            "surface predictably, but a patchy one it does not. Skin "
            "friction drag will be too low, but equally over the whole "
            "model."))
    return out


def apply_layer_fix(target: fc.CaseCfg, diagnosis: LayerDiagnosis) -> None:
    """Переносить лише правки шарів — решти конфігу не чіпає."""
    if diagnosis.cfg is None:
        return
    for name in ("add_layers", "n_layers", "expansion", "final_layer_rel",
                 "min_thickness_rel"):
        setattr(target.mesh, name, getattr(diagnosis.cfg.mesh, name))


def apply_plan(target: fc.CaseCfg, plan: Plan) -> None:
    """Переносить пораховані значення у робочий конфіг.

    Копіюємо поля, а не підміняємо обʼєкт: на нього вже тримають посилання
    панелі інтерфейсу.
    """
    src = plan.cfg
    for name in ("upstream", "downstream", "side", "above", "below",
                 "base_cells_along_x"):
        setattr(target.domain, name, getattr(src.domain, name))
    for name in ("surface_level_min", "surface_level_max", "feature_level",
                 "included_angle", "add_layers", "n_layers",
                 "final_layer_rel", "boxes", "expansion", "target_yplus"):
        setattr(target.mesh, name, getattr(src.mesh, name))
    for name in ("n_procs", "n_iterations", "write_interval",
                 "robust_iterations"):
        setattr(target.solver, name, getattr(src.solver, name))
