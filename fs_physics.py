"""Інженерні кореляції та оцінки для зовнішньої аеродинаміки.

Усе в СІ: довжина — м, швидкість — м/с, nu — м^2/с, rho — кг/м^3.

Формули пласкої пластини використовуються лише для *оцінки* y+ і товщини
першого шару. Це стартова точка для побудови сітки, а не результат розрахунку:
реальний y+ треба перевіряти після прогону через yPlus function object.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

C_MU = 0.09

# Частка граней, на яких snappyHexMesh реально нарощує шари. Виміряно на
# plane5: просили 3 -> наросло 2.36 шару на 79% граней; просили 10 -> 3.66
# шару на 37%. Внесок шарів у кількість комірок виходить приблизно вдвічі
# з половиною менший за наївний добуток.
LAYER_YIELD = 0.4

# Повітря на рівні моря, 15 °C (ISA)
AIR_NU_15C = 1.470e-5
AIR_RHO_15C = 1.225
AIR_SOUND_15C = 340.3
AIR_T_15C = 288.15        # К
AIR_P_SEA = 101325.0      # Па

# Повітря як досконалий газ. Потрібне для стисливого режиму: там густина й
# вʼязкість не задаються, а рахуються з тиску й температури.
AIR_R = 287.058           # Дж/(кг·К), питома газова стала
AIR_GAMMA = 1.4
AIR_CP = 1005.0           # Дж/(кг·К)
AIR_MOL_WEIGHT = 28.96    # кг/кмоль
# Сазерленд для повітря: mu = As * T^1.5 / (T + Ts). Ті самі числа стоять у
# туторіалах OpenFOAM, тому кейс і програма рахують вʼязкість однаково.
SUTHERLAND_AS = 1.4792e-6
SUTHERLAND_TS = 116.0


def density_ideal_gas(pressure: float, temperature: float) -> float:
    """Густина з рівняння стану, кг/м³."""
    return pressure / (AIR_R * max(temperature, 1.0))


def viscosity_sutherland(temperature: float) -> float:
    """Динамічна вʼязкість за Сазерлендом, Па·с."""
    t = max(temperature, 1.0)
    return SUTHERLAND_AS * t ** 1.5 / (t + SUTHERLAND_TS)


def speed_of_sound(temperature: float = AIR_T_15C) -> float:
    """Швидкість звуку, м/с."""
    return math.sqrt(AIR_GAMMA * AIR_R * max(temperature, 1.0))


# --------------------------------------------------------------------------
# Базові безрозмірні числа
# --------------------------------------------------------------------------

def reynolds(u: float, length: float, nu: float) -> float:
    if nu <= 0:
        return 0.0
    return u * length / nu


def mach(u: float, a: float = AIR_SOUND_15C) -> float:
    if a <= 0:
        return 0.0
    return u / a


RE_TRANSITION = 5.0e5


def skin_friction(re: float, turbulent: bool = True) -> float:
    """Локальний Cf на пласкій пластині.

    За замовчуванням повертає ТУРБУЛЕНТНЕ значення (кореляція Шліхтінга
    Cf = 0.026 / Re^(1/7)) незалежно від Re. Це навмисно: сітку ми будуємо
    під RANS-модель, яка вважає пограншар турбулентним по всій довжині.
    Якби тут при низьких Re підставлявся ламінарний Блазіус, оцінка y+
    розійшлася б із тим, що потім порахує розвʼязувач, і перший шар вийшов
    би завеликим.

    turbulent=False дає ламінарну гілку — для довідки, не для сітки.
    """
    if re <= 0:
        return 0.0
    if not turbulent and re < RE_TRANSITION:
        return 0.664 / math.sqrt(re)
    return 0.026 / re ** (1.0 / 7.0)


def is_transitional(re: float) -> bool:
    """Чи потрапляє Re в зону, де припущення «все турбулентне» сумнівне."""
    return 0 < re < RE_TRANSITION


def friction_velocity(u: float, re: float) -> float:
    """u* = sqrt(tau_w / rho) = U * sqrt(Cf / 2)."""
    return u * math.sqrt(skin_friction(re) / 2.0)


# --------------------------------------------------------------------------
# y+ та висота першої комірки
# --------------------------------------------------------------------------

def wall_distance_for_yplus(y_plus: float, u: float, length: float, nu: float) -> float:
    """Відстань від стінки до ЦЕНТРА першої комірки, що дає заданий y+."""
    u_tau = friction_velocity(u, reynolds(u, length, nu))
    if u_tau <= 0:
        return 0.0
    return y_plus * nu / u_tau


def yplus_for_wall_distance(dist: float, u: float, length: float, nu: float) -> float:
    """Зворотна задача: який y+ дасть центр комірки на відстані dist."""
    u_tau = friction_velocity(u, reynolds(u, length, nu))
    if nu <= 0:
        return 0.0
    return dist * u_tau / nu


def yplus_regime(y_plus: float) -> tuple[str, str]:
    """Повертає (мітка, рівень), де рівень один із ok / warn / bad."""
    if y_plus < 1.0:
        return ("розвʼязаний підшар", "ok")
    if y_plus < 5.0:
        return ("вʼязкий підшар", "ok")
    if y_plus < 30.0:
        return ("буферний шар — найгірша зона для пристінних функцій", "bad")
    if y_plus <= 300.0:
        return ("лог-шар, пристінні функції працюють", "ok")
    if y_plus <= 1000.0:
        return ("зависоко, точність пристінних функцій падає", "warn")
    return ("далеко за лог-шаром", "bad")


# --------------------------------------------------------------------------
# Пограншар
# --------------------------------------------------------------------------

def bl_thickness(u: float, length: float, nu: float) -> float:
    """Товщина турбулентного пограншару на кінці пластини: 0.37 L / Re^0.2."""
    re = reynolds(u, length, nu)
    if re <= 0:
        return 0.0
    return 0.37 * length / re ** 0.2


@dataclass
class LayerStack:
    """Розкладка призматичних шарів snappyHexMesh при relativeSizes true."""

    n_layers: int
    expansion: float
    final_rel: float       # finalLayerThickness, частка розміру поверхневої комірки
    surface_cell: float    # розмір комірки на поверхні, м

    @property
    def final_abs(self) -> float:
        return self.final_rel * self.surface_cell

    @property
    def first_abs(self) -> float:
        """Товщина шару, що прилягає до стінки."""
        if self.n_layers <= 0:
            return 0.0
        return self.final_abs / self.expansion ** (self.n_layers - 1)

    @property
    def total(self) -> float:
        """Сумарна товщина всього стеку шарів."""
        if self.n_layers <= 0:
            return 0.0
        r = self.expansion
        if abs(r - 1.0) < 1e-9:
            return self.first_abs * self.n_layers
        return self.first_abs * (r ** self.n_layers - 1.0) / (r - 1.0)

    @property
    def first_centre(self) -> float:
        """Центр першої комірки. Саме він визначає y+."""
        return self.first_abs / 2.0

    def thicknesses(self) -> list[float]:
        return [self.first_abs * self.expansion ** i for i in range(self.n_layers)]

    def yplus(self, u: float, length: float, nu: float) -> float:
        return yplus_for_wall_distance(self.first_centre, u, length, nu)

    def covers_bl(self, u: float, length: float, nu: float) -> float:
        """Яку частку пограншару накриває стек шарів. Норма — 0.5 і більше."""
        delta = bl_thickness(u, length, nu)
        return self.total / delta if delta > 0 else 0.0


def layers_for_target_yplus(
    y_plus: float,
    u: float,
    length: float,
    nu: float,
    surface_cell: float,
    expansion: float = 1.2,
    final_rel: float = 0.5,
    max_layers: int = 40,
) -> int:
    """Скільки шарів треба, щоб перша комірка потрапила в заданий y+."""
    target_first = 2.0 * wall_distance_for_yplus(y_plus, u, length, nu)
    final_abs = final_rel * surface_cell
    if target_first <= 0 or final_abs <= 0 or expansion <= 1.0:
        return 0
    if target_first >= final_abs:
        return 1
    n = 1 + math.log(final_abs / target_first) / math.log(expansion)
    return max(1, min(max_layers, int(math.ceil(n))))


# --------------------------------------------------------------------------
# Турбулентність на вході
# --------------------------------------------------------------------------

@dataclass
class InletTurbulence:
    k: float
    omega: float
    epsilon: float
    nut: float


def inlet_turbulence_from_viscosity_ratio(
    u: float, intensity: float, nu: float, ratio: float
) -> InletTurbulence:
    """Задання через відношення nut/nu.

    Для зовнішньої аеродинаміки зручніше за масштаб довжини: ratio = 1..10
    відповідає чистому набігаючому потоку.
    """
    k = 1.5 * (u * intensity) ** 2
    nut = max(ratio * nu, 1e-30)
    omega = k / nut
    epsilon = C_MU * k * omega
    return InletTurbulence(k=k, omega=omega, epsilon=epsilon, nut=nut)


def re_theta_t_freestream(intensity: float) -> float:
    """Набігаючий рівень ReΘt для моделі переходу kOmegaSSTLM.

    Кореляція Ленґтрі–Ментера. Tu тут у ВІДСОТКАХ, тому частка з `intensity`
    множиться на 100; дві гілки сходяться на Tu = 1.3%:

        Tu <= 1.3:  1173.51 - 589.428*Tu + 0.2196/Tu²
        Tu >  1.3:  331.50 * (Tu - 0.5658)^(-0.671)

    Нижню гілку рве на нулі (ділення на Tu²), а нульова інтенсивність —
    цілком реальне значення в полі вводу. Тому Tu тут не менше 0.01%: нижче
    цього число однаково не має фізичного сенсу, бо жодна течія не буває
    настільки чистою.

    Нижня межа 20 — та сама, що й усередині kOmegaSSTLM: модель сама
    обрізає ReΘt знизу, і віддавати їй менше немає сенсу.
    """
    tu = max(intensity, 1e-4) * 100.0
    if tu <= 1.3:
        value = 1173.51 - 589.428 * tu + 0.2196 / tu ** 2
    else:
        value = 331.50 * (tu - 0.5658) ** -0.671
    return max(value, 20.0)


def inlet_turbulence_from_length_scale(
    u: float, intensity: float, length_scale: float
) -> InletTurbulence:
    """Задання через турбулентний масштаб довжини."""
    k = 1.5 * (u * intensity) ** 2
    ls = max(length_scale, 1e-12)
    omega = math.sqrt(k) / (C_MU ** 0.25 * ls)
    epsilon = C_MU ** 0.75 * k ** 1.5 / ls
    nut = k / omega if omega > 0 else 0.0
    return InletTurbulence(k=k, omega=omega, epsilon=epsilon, nut=nut)


# --------------------------------------------------------------------------
# Оцінка розміру сітки та памʼяті
# --------------------------------------------------------------------------

@dataclass
class MeshEstimate:
    background: int
    regions: int
    surface: int
    layers: int
    # Калібровано на реальних прогонах plane5 (163 тис. трикутників, рівень 5):
    #   0 шарів: оцінка 510k -> реально 414k
    #   3 шари:  637k -> 466k
    #   5 шарів: 723k -> 486k
    # Звідси fudge 1.15 для обʼємної частини й LAYER_YIELD для шарів: snappy
    # нарощує їх лише на частині граней, тому наївне n_layers * A / h^2
    # завищує внесок шарів утричі.
    fudge: float = 1.15

    @property
    def total(self) -> int:
        raw = (self.background + self.regions + self.surface
               + self.layers * LAYER_YIELD)
        return int(raw * self.fudge)

    @property
    def snappy_peak_gb(self) -> float:
        """Пікова памʼять snappyHexMesh, сумарно по всіх процесах.

        ВИМІРЯНО на plane5 (490 тис. комірок, 10 процесів, OpenFOAM v2512):
        приріст 1138 МБ, тобто 2.3 ГБ на мільйон комірок. Попередній
        коефіцієнт 1.2 занижував удвічі. Число включає октодерево пошуку по
        поверхні й копії в кожному процесі, тому з меншою кількістю процесів
        воно буде дещо нижчим.
        """
        return self.total / 1.0e6 * 2.3

    @property
    def solver_gb(self) -> float:
        """simpleFoam з kOmegaSST, сумарно.

        ВИМІРЯНО там само: приріст 808 МБ на 490 тис. комірок = 1.65 ГБ на
        мільйон. Попередні 0.35 занижували майже вп'ятеро.
        """
        return self.total / 1.0e6 * 1.65


def estimate_mesh(
    domain_volume: float,
    base_cell: float,
    surface_area: float,
    surface_level: int,
    refinement_regions: list[tuple[float, int]],
    n_layers: int,
    cells_between_levels: int = 3,
) -> MeshEstimate:
    """Груба оцінка кількості комірок.

    refinement_regions — список пар (обʼєм у м^3, рівень). Оцінка свідомо
    консервативна: калібрована по motorBike і дає похибку в межах ~1.5x,
    чого достатньо, щоб зрозуміти, чи влізе задача в памʼять.
    """
    if base_cell <= 0:
        return MeshEstimate(0, 0, 0, 0)

    background = int(domain_volume / base_cell ** 3)

    regions = 0
    for volume, level in refinement_regions:
        h = base_cell / 2 ** max(level, 0)
        if h > 0:
            regions += int(volume / h ** 3)

    h_surf = base_cell / 2 ** max(surface_level, 0)
    surface = 0
    layers = 0
    if h_surf > 0 and surface_area > 0:
        # Оболонка завтовшки ~cells_between_levels комірок навколо поверхні.
        surface = int(cells_between_levels * surface_area / h_surf ** 2)
        layers = int(n_layers * surface_area / h_surf ** 2)

    return MeshEstimate(background=background, regions=regions,
                        surface=surface, layers=layers)


def fmt_cells(n: int) -> str:
    """Кількість комірок по-людськи. Мільярди теж: «20628.18M» не читається."""
    if n >= 1_000_000_000:
        return f"{n / 1_000_000_000:.1f} млрд"
    if n >= 1_000_000:
        return f"{n / 1_000_000:.2f} млн"
    if n >= 1_000:
        return f"{n / 1_000:.0f} тис."
    return str(n)


def memory_breakdown(est: "MeshEstimate", cfg_level: int,
                     budget_gb: float) -> tuple[str, int]:
    """(пояснення, який рівень рефайнменту влізе в бюджет).

    Коли оцінка виходить абсурдною, користувачеві потрібна не цифра, а
    відповідь на два питання: що саме роздуло сітку і що натиснути. Тому
    називаємо найбільший доданок і рахуємо рівень, на якому задача влізе.
    """
    parts = [
        ("фонова сітка", est.background),
        ("коробки згущення", est.regions),
        ("рефайнмент поверхні", est.surface),
        ("призматичні шари", int(est.layers * LAYER_YIELD)),
    ]
    name, value = max(parts, key=lambda kv: kv[1])
    total_raw = sum(v for _, v in parts) or 1
    share = value / total_raw

    # Кожен рівень нижче зменшує поверхневий доданок учетверо, а обʼємний —
    # у вісім разів. Беремо консервативну четвірку.
    level = cfg_level
    gb = est.snappy_peak_gb
    while level > 1 and gb > budget_gb:
        level -= 1
        gb /= 4.0

    text = (f"Найбільший внесок — {name}: {fmt_cells(value)} комірок "
            f"({share:.0%} від усіх).")
    return text, level


def fmt_len(metres: float) -> str:
    """Довжина у зручній одиниці."""
    a = abs(metres)
    if a == 0:
        return "0"
    if a < 1e-6:
        return f"{metres * 1e9:.1f} нм"
    if a < 1e-3:
        return f"{metres * 1e6:.1f} мкм"
    if a < 1.0:
        return f"{metres * 1e3:.3g} мм"
    return f"{metres:.4g} м"
