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
        host = memfit.MemHost("цей компʼютер", ram_gb, RAM_HEADROOM)
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
    _add(tips, "Домен", "Позаду", cfg.domain.downstream, downstream,
         ("слід за тілом із великим розмахом довший — беремо 15 довжин"
          if slender > 1.5 else "12 довжин вистачає, щоб слід вирівнявся"))
    new.domain.downstream = downstream

    _add(tips, "Домен", "Попереду", cfg.domain.upstream, 5.0,
         "5 довжин попереду: ближче вхід уже впливає на тиск біля носа")
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
                    f"STL надто грубий: його тесселяція дозволяє лише "
                    f"~{cells_per_L:.0f} комірок на довжину тіла, а для "
                    f"аеродинаміки треба від 100. Натисніть «Згустити» на "
                    f"вкладці «Геометрія» або перезбережіть STL з меншим "
                    f"відхиленням.")))
            else:
                notes.append(("info", (
                    f"Комірку обмежено знизу тесселяцією STL "
                    f"({phys.fmt_len(stl_edge)} коротке ребро трикутника): "
                    f"дрібніша сітка вже не додасть форми, лише часу.")))

    # Дірки: комірка має бути БІЛЬШОЮ за найбільшу дірку, інакше протече.
    if diag is not None and diag.hole_sizes:
        biggest = diag.largest_hole
        if biggest > target_surface:
            notes.append(("bad", (
                f"Найбільша дірка {phys.fmt_len(biggest)} більша за потрібну "
                f"комірку {phys.fmt_len(target_surface)}. Сітка протече. "
                f"Або залатайте STL, або свідомо загрубіть поверхню до "
                f"{phys.fmt_len(biggest * 1.5)} — але тоді форма розмиється.")))

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
        what = "snappyHexMesh" if w.stage == "snappy" else "розвʼязувач"
        notes.append(("warn", (
            f"Рівень рефайнменту знижено до {new.mesh.surface_level_max}: "
            f"на попередньому {what} не влазив на {w.host.name} "
            f"(~{w.need_gb:.1f} ГБ, під задачу "
            f"{memfit.fmt_gb(w.host.budget_gb)} з "
            f"{memfit.fmt_gb(w.host.ram_gb)} ГБ).")))

    _add(tips, "Сітка", "Рівень поверхні", cfg.mesh.surface_level_max,
         new.mesh.surface_level_max,
         f"комірка на поверхні {phys.fmt_len(new.surface_cell)} ≈ "
         f"L/{L / max(new.surface_cell, 1e-12):.0f}")
    _add(tips, "Домен", "Комірок уздовж X", cfg.domain.base_cells_along_x, n_x,
         f"фонова комірка {phys.fmt_len(new.base_cell)}; "
         f"рівень {new.mesh.surface_level_max} доводить її до потрібної")

    # ----------------------------------------------------- кромки й шари
    dirty = _is_assembly(diag)
    included = 170 if dirty else 150
    _add(tips, "Сітка", "includedAngle", cfg.mesh.included_angle, included,
         ("на збірці з перетинних деталей менший кут дає сміттєві кромки"
          if dirty else "150° — стандарт для чистої геометрії"))
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
        _add(tips, "Сітка", "Шари", cfg.mesh.n_layers, 0,
             "поки STL протікає, шари не наростуть — спершу геометрія",
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
                f"Шарів не більше {layer_cap}: у геометрії {diag.parts} частин "
                f"і {diag.non_manifold_edges} non-manifold ребер. На такій "
                f"поверхні snappy нарощує шари плямами, і чим більше їх "
                f"просити, тим менше граней їх отримає (виміряно: 3 шари дають "
                f"79% покриття, 10 шарів — 37%).")))

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
                _add(tips, "Сітка", "expansionRatio", cfg.mesh.expansion,
                     new.mesh.expansion,
                     "інакше перший шар не дотягнеться до потрібного y+ "
                     "у межах кількості шарів")

        got_yplus = _predicted_yplus(new, target_yplus)
        if got_yplus > target_yplus * 4:
            notes.append(("warn", (
                f"Цільового y+ {target_yplus:.0f} досягти не вдається: виходить "
                f"{got_yplus:.0f}. Перший шар упирається у розмір поверхневої "
                f"комірки ({phys.fmt_len(new.surface_cell)}). Підніміть рівень "
                f"рефайнменту або зменшіть finalLayerThickness з "
                f"{new.mesh.final_layer_rel:.2f} до 0.2.")))

        _add(tips, "Сітка", "finalLayerThickness", cfg.mesh.final_layer_rel,
             new.mesh.final_layer_rel,
             "тонший останній шар робить увесь стек тоншим, не жертвуючи "
             "тонкістю першого — так y+ і товщина стеку миряться")
        _add(tips, "Сітка", "Шарів", cfg.mesh.n_layers, new.mesh.n_layers,
             f"дає y+ ≈ {_predicted_yplus(new, target_yplus):.0f} "
             f"при цільовому {target_yplus:.0f}; стек "
             f"{new.layer_stack.covers_bl(cfg.flow.u_inf, cfg.flow.l_ref or L, cfg.flow.nu):.0%} "
             f"пограншару")

    # ------------------------------------------------------- розвʼязувач
    est = new.estimate()
    # Два ядра лишаємо системі: інакше інтерфейс і сам WSL починають голодувати
    # і прогін від цього не пришвидшується.
    usable = cores - 2 if cores > 4 else cores
    n_procs = max(1, min(usable, max(1, est.total // 40_000)))
    _add(tips, "Розвʼязувач", "Процесів", cfg.solver.n_procs, n_procs,
         f"~{est.total // max(n_procs, 1):,}".replace(",", " ")
         + f" комірок на процес; з {cores} ядер два лишаємо системі")
    new.solver.n_procs = n_procs

    iters = 1000 if est.total < 1_000_000 else 2000
    _add(tips, "Розвʼязувач", "Ітерацій", cfg.solver.n_iterations, iters,
         "стаціонарний SIMPLE на такій сітці зазвичай сходиться раніше — "
         "зупинить residualControl")
    new.solver.n_iterations = iters
    new.solver.write_interval = iters

    if not cfg.solver.steady:
        # У нестаціонарі «скільки рахувати» задається в секундах, а секунди
        # залежать від розміру тіла й швидкості. Тому рахуємо з прольоту
        # домену, а не пропонуємо число з голови.
        flight = new.flow_through_time
        end = _round_nice(flight * 4)
        _add(tips, "Розвʼязувач", "Кінцевий час", cfg.solver.end_time, end,
             f"чотири прольоти домену ({flight:.3g} с кожен): перший іде на "
             f"те, щоб потік дійшов до тіла, решта — на формування сліду")
        new.solver.end_time = end

        every = _round_nice(end / 40.0)
        _add(tips, "Розвʼязувач", "Запис кожні", cfg.solver.write_every, every,
             "сорок часових зрізів — досить, щоб побачити рух, і не стільки, "
             "щоб забити диск")
        new.solver.write_every = every

        # Початковий крок беремо від комірки на поверхні: Куранта на ній і
        # вирішує, чи не зірветься перший крок, поки adjustTimeStep ще не
        # встиг нічого підібрати.
        dt = _round_nice(new.surface_cell / max(new.flow.u_inf, 1e-9) * 0.5)
        dt = min(dt, every / 10.0)
        _add(tips, "Розвʼязувач", "Початковий крок", cfg.solver.time_step, dt,
             "половина Куранта на поверхневій комірці — далі крок веде "
             "adjustTimeStep сам")
        new.solver.time_step = dt

    # Двоетапність: що швидший потік, то довший стійкий розгін.
    u = cfg.flow.u_inf
    robust = 150 if u <= 40 else (300 if u <= 90 else 500)
    _add(tips, "Розвʼязувач", "Стійких ітерацій", cfg.solver.robust_iterations,
         robust,
         f"при {u:.0f} м/с типові схеми другого порядку зриваються в перші "
         f"десятки ітерацій; стільки перших рахуємо upwind-ом")
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
    tips.append(Tip(where=where, what=what, current=_fmt(current),
                    suggested=_fmt(suggested), reason=reason, level=level))


def _flow_notes(cfg: fc.CaseCfg, diag, notes: list, target_yplus: float,
                fit: "memfit.MemFit") -> None:
    f = cfg.flow
    L = f.l_ref or cfg.body_length
    re = phys.reynolds(f.u_inf, L, f.nu)
    ma = f.mach
    est = cfg.estimate()

    notes.append(("info", (
        f"Re = {re:.3g}, M = {ma:.2f}, оцінка сітки "
        f"{phys.fmt_cells(est.total)} комірок. "
        + "Памʼять: " + "; ".join(ln.strip() for ln
                                   in fit.details().splitlines()[1:]) + ".")))
    if fit.verdict == "unknown":
        notes.append(("warn", fit.warning().lstrip("ℹ ")))

    if ma > 0.3 and not f.compressible:
        notes.append(("warn", (
            f"M = {ma:.2f}: стисливість уже помітна, а simpleFoam її не "
            f"бачить і занижує опір. Перемкніть режим течії на стисливий — "
            f"кейс піде через rhoSimpleFoam.")))
    elif f.compressible and ma < 0.3:
        # Не помилка, але й не безкоштовно: стисливий SIMPLE релаксує густину
        # на 0.01, і на ту саму збіжність йому треба в рази більше ітерацій.
        notes.append(("info", (
            f"M = {ma:.2f}: стисливість тут нічого не змінює, а стисливий "
            f"розвʼязувач збігається помітно повільніше. Якщо швидкість не "
            f"зростатиме, нестислива дасть той самий результат швидше.")))
    elif f.compressible and ma > 0.8:
        notes.append(("warn", (
            f"M = {ma:.2f}: це вже трансзвук. rhoSimpleFoam стаціонарний і "
            f"на ударних хвилях ненадійний — для такого потрібен "
            f"rhoCentralFoam, якого в цій версії немає.")))

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
                      chk.text + (f" Що зробити: {fix}." if fix else "")))
    if (phys.is_transitional(re)
            and fy.model_family(f.turbulence) != "transition"):
        common = (f"Re = {re:.2g} нижче 5·10⁵: пограншар частково ламінарний, "
                  f"а RAS вважає його турбулентним усюди й завищує опір тертя.")
        if resolved:
            notes.append(("warn", common + (
                f" Сітка вже розвʼязує стінку (y+ ≈ {yp_now:.1f}), тож тут "
                f"варто взяти kOmegaSSTLM — вона рахує перехід замість того, "
                f"щоб його ігнорувати.")))
        else:
            notes.append(("warn", common + (
                f" Полагодити це може kOmegaSSTLM, але вона вимагає y+ ≈ 1, "
                f"а зараз виходить {yp_now:.0f}: спершу дрібніший перший "
                f"шар, і аж потім модель переходу.")))

    if cfg.mesh.add_layers:
        yp = _predicted_yplus(cfg, target_yplus)
        label, lvl = phys.yplus_regime(yp)
        st = cfg.layer_stack
        cover = st.covers_bl(f.u_inf, L, f.nu)
        # Скільки звичайних комірок лишається на решту пограншару над стеком.
        delta = phys.bl_thickness(f.u_inf, L, f.nu)
        rest = max(delta - st.total, 0.0)
        cells_above = rest / cfg.surface_cell if cfg.surface_cell > 0 else 0

        msg = (f"Очікуваний y+ ≈ {yp:.0f} — {label}. Стек шарів "
               f"{phys.fmt_len(st.total)} накриває {cover:.0%} пограншару "
               f"({phys.fmt_len(delta)}), решту тримають ~{cells_above:.1f} "
               f"звичайних комірок.")
        # Тонкий стек — це не помилка, а ціна за потрібний y+: щоб перша
        # комірка була тонкою, увесь стек виходить низьким. Тривожно стає
        # тоді, коли над ним на весь зовнішній пограншар лишається менше
        # двох комірок — тоді профіль швидкості там просто нема на чому
        # розвʼязати.
        level = lvl if lvl != "ok" else "info"
        if cells_above < 2.0 and cover < 0.5:
            level = "warn"
            msg += (" Це замало: підніміть рівень рефайнменту поверхні "
                    "на одиницю або дозвольте більше шарів.")
        notes.append((level, msg))
    else:
        notes.append(("warn", (
            "Без шарів перша комірка дорівнює поверхневій, y+ буде в сотнях. "
            "Тиск і форма сліду ще осмислені, опір тертя — ні.")))

    if diag is not None and diag.bad_winding:
        notes.append(("bad", (
            f"{diag.bad_winding} ребер із неузгодженою намоткою. Спершу "
            f"«Виправити нормалі»: на такій поверхні snappy не може надійно "
            f"сказати, де «всередині», і шари лягають плямами.")))
    elif diag is not None and diag.inverted_parts:
        notes.append(("warn", (
            f"{diag.inverted_parts} частин вивернуті навиворіт "
            f"({diag.flipped_faces} граней). Для шарів це менш болісно, ніж "
            f"для refinementRegions з mode inside, але лагодиться одним "
            f"натиском «Виправити нормалі».")))


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
            lines.append("Що підкрутити: " + "; ".join(
                f"{t.what} {t.current} → {t.suggested}" for t in self.tips)
                + ".")
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
        out.headline = "Шарів не просили — пограншар на «голій» сітці."
        return out

    if coverage >= LAYER_FINE:
        out.headline = f"Шари наросли на {pct:.0f}% граней — норма."
        out.notes.append(
            "Решта — гострі кромки й стики, де призму нікуди вставити.")
        return out

    # ---- наслідок, у числах ----
    stack = cfg.layer_stack
    yp_layers = stack.yplus(f.u_inf, L, f.nu)
    yp_bare = phys.yplus_for_wall_distance(cfg.surface_cell / 2.0,
                                           f.u_inf, L, f.nu)
    out.effect = (
        f"Там, де шарів немає (це {100 - pct:.0f}% поверхні), перша комірка "
        f"дорівнює поверхневій — {phys.fmt_len(cfg.surface_cell)} замість "
        f"{phys.fmt_len(stack.first_abs)}, тобто локальний y+ підскакує з "
        f"{yp_layers:.0f} до {yp_bare:.0f}. Опір тертя на цих ділянках "
        f"занижений, і y+ із вкладки «Сітка» для них не має сили.")

    if coverage >= LAYER_WORKABLE:
        out.level = "warn"
        out.headline = (f"Шари наросли на {pct:.0f}% граней — робоче, але не "
                        f"рівномірно.")
    elif coverage >= LAYER_POOR:
        out.level = "warn"
        out.headline = f"Шари наросли лише на {pct:.0f}% граней — замало."
    else:
        out.level = "bad"
        out.headline = (f"Шари наросли лише на {pct:.0f}% граней — пограншар "
                        f"фактично не розвʼязано.")

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
        _add(tips, "Сітка", "Шарів", m.n_layers, want,
             "найдужчий важіль: коротший стек влазить у вужчі місця. "
             "Виміряно на plane5 — 10 шарів дали 37% покриття, 5 шарів 65%, "
             "3 шари 79%")

    if m.expansion > 1.3:
        new.mesh.expansion = 1.3
        _add(tips, "Сітка", "expansionRatio", m.expansion, 1.3,
             "зростання швидше за 1.3 робить стек довгим на кількох шарах і "
             "дає різкий стрибок розміру на виході з нього")

    if tips and cfg.surface_cell > 0:
        # Тримаємо перший шар незмінним: final_rel * cell / exp^(n-1) = const.
        want_final = (first_before * new.mesh.expansion ** (new.mesh.n_layers - 1)
                      / cfg.surface_cell)
        want_final = round(min(max(want_final, 0.05), 1.0), 2)
        if abs(want_final - m.final_layer_rel) >= 0.01:
            new.mesh.final_layer_rel = want_final
            _add(tips, "Сітка", "finalLayerThickness", m.final_layer_rel,
                 want_final,
                 "перерахунок під нову кількість шарів: перший шар лишається "
                 "такий самий, тобто y+ не псується, а стек коротшає")

    if m.min_thickness_rel > 0.02:
        new.mesh.min_thickness_rel = 0.02
        _add(tips, "Сітка", "minThickness", m.min_thickness_rel, 0.02,
             "грань, де шар виходить тонший за цей поріг, snappy лишає зовсім "
             "без шару. Нижчий поріг дає тонкий шар замість жодного — ціною "
             "витягнутіших комірок, тож checkMesh може почати бурчати")

    out.tips = tips
    out.cfg = new if tips else None

    if tips:
        after = new.layer_stack
        if after.total < stack.total * 0.95:
            out.notes.append(
                f"Після правок: перший шар {phys.fmt_len(after.first_abs)} "
                f"(було {phys.fmt_len(first_before)}), тобто y+ ≈ "
                f"{after.yplus(f.u_inf, L, f.nu):.0f} замість "
                f"{yp_layers:.0f} — майже те саме, а от стек коротшає з "
                f"{phys.fmt_len(stack.total)} до "
                f"{phys.fmt_len(after.total)}. Саме це й дає покриття.")
        else:
            out.notes.append(
                f"Стек при цьому не коротшає — працює лише нижчий поріг: "
                f"грані, на яких шар виходив тонший за "
                f"{m.min_thickness_rel:g} комірки й тому не зʼявлявся "
                f"зовсім, тепер отримають тонкий шар.")

    # ---- причини, яких ручками не полагодити ----
    if _is_assembly(diag):
        out.notes.append(
            f"Головна причина тут не в налаштуваннях: у геометрії "
            f"{diag.parts} частин і {diag.non_manifold_edges} non-manifold "
            f"ребер. Уздовж ліній взаємного врізання оболонок шар нарощувати "
            f"нікуди. Перевірено A/B: featureAngle 60→130 і medialRatio "
            f"0.3→0.6 дали 36.7% проти 36.4% — ці ручки не лікують. Лікує "
            f"зшивання деталей в одне тіло (булеве обʼєднання в CAD або "
            f"shrink wrap).")
    if diag is not None and not diag.closed and diag.largest_hole > cfg.surface_cell:
        out.notes.append(
            "Поверхня протікає дірками більшими за комірку — поки це так, "
            "шари не наростуть узагалі. Спершу вкладка «Геометрія».")
    if not tips:
        out.notes.append(
            f"Крутити вже нічого: шарів {m.n_layers}, minThickness "
            f"{m.min_thickness_rel:g}, finalLayerThickness "
            f"{m.final_layer_rel:g} — усе на мінімумі. Лишається підняти "
            f"рівень рефайнменту поверхні (комірка стане меншою, а з нею й "
            f"стек — але комірок буде вчетверо більше) або лагодити "
            f"геометрію.")
    if out.level == "bad":
        out.notes.append(
            f"Якщо не допоможе — лишається рівень рефайнменту поверхні "
            f"{m.surface_level_max} → {m.surface_level_max + 1}: комірка "
            f"стане {phys.fmt_len(cfg.surface_cell / 2)} замість "
            f"{phys.fmt_len(cfg.surface_cell)}, а з нею вдвічі тоншим і "
            f"стек. Ціна — комірок приблизно вчетверо більше.")
        out.notes.append(
            "Варіант «здатися» теж робочий: вимкнути шари зовсім. Рівний "
            "високий y+ по всій поверхні пристінна функція обробляє "
            "передбачувано, а плямистий — ні. Опір тертя буде занижений, "
            "зате однаково по всій моделі.")
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
