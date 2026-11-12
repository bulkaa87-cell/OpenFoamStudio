"""Генерація кейсу OpenFOAM для зовнішньої аеродинаміки (стаціонар, нестислива).

Один виклик build_case(cfg) повертає {відносний шлях: текст файлу} для всього
кейсу: 0.orig/, constant/, system/, Allrun, Allclean. STL записується окремо
(див. fs_geometry.write_stl_for_case), бо це бінарні дані.

Схема дискретизації і fvSolution узяті з tutorial-кейсу motorBike
(incompressible/simpleFoam/motorBike) — це найперевіреніша конфігурація
для зовнішнього обтікання, і вона стабільно збігається на брудних сітках.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field

import fs_physics as phys
from fs_i18n import tr

HEADER = """/*--------------------------------*- C++ -*----------------------------------*\\
|       OpenFOAM Studio — згенеровано автоматично                             |
\\*---------------------------------------------------------------------------*/
FoamFile
{{
    version     2.0;
    format      ascii;
    class       {cls};
    {extra}object      {obj};
}}
// * * * * * * * * * * * * * * * * * * * * * * * * * * * * * * * * * * * * * //

"""

FOOTER = "\n// ************************************************************************* //\n"


def _head(cls: str, obj: str, location: str | None = None) -> str:
    extra = f'location    "{location}";\n    ' if location else ""
    return HEADER.format(cls=cls, obj=obj, extra=extra)


def fmt_count(value) -> str:
    """Число з нерозривними пробілами між тисячами."""
    return f"{value:,.0f}".replace(",", " ")


def _vec(v) -> str:
    return "(%.10g %.10g %.10g)" % (v[0], v[1], v[2])


# ==========================================================================
# Конфігурація
# ==========================================================================

TURBULENCE_MODELS = ("kOmegaSST", "kOmegaSSTLM", "kEpsilon", "SpalartAllmaras",
                     "kOmegaSSTDDES", "kOmegaSSTIDDES", "SpalartAllmarasDDES",
                     "WALE", "kEqn")

# Моделі, які розвʼязують вихори, а не замінюють їх усередненою вʼязкістю.
# Для OpenFOAM це інший simulationType, тому список потрібен окремо, а не
# «за назвою містить DES».
#
# Значення — який спосіб міряти розмір фільтра брати. IDDES вимагає свого,
# IDDESDelta: він враховує відстань до стінки, і саме на ньому тримається
# лагодження «сірої зони», заради якого IDDES і роблять.
LES_MODELS = {
    # Гібридні: RANS біля стінки, LES у відірваній зоні.
    "kOmegaSSTDDES": "cubeRootVol",
    "kOmegaSSTIDDES": "IDDESDelta",
    "SpalartAllmarasDDES": "cubeRootVol",
    # Чисті: розвʼязують вихори скрізь, зокрема біля стінки. Чесніше й
    # набагато дорожче — сітка біля стінки потрібна така, щоб у неї
    # вміщалися пристінні вихори, а вони найдрібніші з усіх.
    "WALE": "cubeRootVol",
    "kEqn": "cubeRootVol",
}

# Моделі, які взагалі не везуть транспортних рівнянь: вʼязкість рахується
# алгебраїчно з градієнта швидкості просто тут і зараз.
ALGEBRAIC_MODELS = ("WALE",)

# Режим течії. Це не просто інший розвʼязувач: у стисливому тиск стає
# абсолютним у Па (у нестисливому він кінематичний, p/rho), додаються поля T
# і alphat, а густина й вʼязкість більше не задаються — вони рахуються з
# тиску й температури.
REGIMES = ("incompressible", "compressible", "supersonic")

# Стаціонарність — вісь, незалежна від стисливості, тому розвʼязувач шукається
# по парі. Ключ — (режим, стаціонарний).
SOLVER_OF = {
    ("incompressible", True): "simpleFoam",
    ("incompressible", False): "pimpleFoam",
    ("compressible", True): "rhoSimpleFoam",
    ("compressible", False): "rhoPimpleFoam",
    # Стаціонарного надзвукового немає й бути не може: схема явна, крок
    # обмежений Курантом, і «усталений стан» тут набирається інтегруванням
    # за часом, а не ітераціями. Пару (supersonic, True) ловить guard_regime.
    ("supersonic", False): "rhoCentralFoam",
}

# Межі, за якими налаштування перестають мати сенс. Не поради: за них не
# пускають. Числа не з голови — пояснення при кожному в guard_unsteady().
MAX_TIME_STEPS = 500_000      # кроків за прогін
MAX_WRITE_DIRS = 2_000        # часових тек на диску
MAX_COURANT = 20.0            # вище PIMPLE зриває розрахунок
# Для явної схеми rhoCentralFoam стеля зовсім інша. Одиниця — це межа
# стійкості за побудовою: збурення за крок не має перестрибнути комірку.
# Половина — звичний робочий запас, бо на косих стрибках локальний Курант
# виходить більшим за той, що рахує солвер у середньому.
MAX_COURANT_CENTRAL = 0.5
# Стеля Куранта для стисливого нестаціонарного (rhoPimpleFoam) — виміряна,
# див. guard_unsteady.
COMPRESSIBLE_MAX_CO = 2.0


_WIN_RESERVED = {"CON", "PRN", "AUX", "NUL",
                 *(f"COM{i}" for i in range(1, 10)),
                 *(f"LPT{i}" for i in range(1, 10))}


def safe_case_name(name: str, fallback: str = "case") -> str:
    """Назва кейсу, придатна за ТЕКУ — у Windows, у WSL і на сервері по ssh.

    Лишаються літери (і кирилиця), цифри, `_`, `.` і `-`; решта — `_`.
    Двокрапка з «09.11_11:15» валила створення кейсу ([WinError 123]):
    у Windows вона в імені заборонена. Пробіли, лапки, `$` та інше
    ламали б ще й команди оболонки, де шлях іде в рядок.

    Ще Windows: імʼя не може кінчатися крапкою, а CON, NUL, COM1… —
    зарезервовані. Початковий `-` прибирається, щоб імʼя не читалося як
    ключ команди.
    """
    import re as _re
    text = _re.sub(r"[^\w.-]+", "_", str(name or "").strip(),
                   flags=_re.UNICODE)
    text = _re.sub(r"_{2,}", "_", text).strip("._-")[:100].rstrip("._-")
    if not text:
        return fallback
    if text.split(".")[0].upper() in _WIN_RESERVED:
        text = f"{text}_"
    return text


def solver_name(cfg: "CaseCfg") -> str:
    key = (cfg.flow.regime, cfg.solver.steady)
    if key not in SOLVER_OF and cfg.flow.regime == "supersonic":
        # Щоб не падати посеред складання словників: за такий вибір не
        # пускає guard_regime, а тут просто називаємо той розвʼязувач, який
        # для надзвуку є.
        return SOLVER_OF[("supersonic", False)]
    return SOLVER_OF[key]


@dataclass
class BodyPatch:
    """Одна іменована частина тіла: свій STL і свій патч у сітці.

    Потрібна, коли треба знати сили ОКРЕМО по крилу й фюзеляжу, а не лише
    сумарні. Для OpenFOAM це різні границі, тож і в snappyHexMesh вони
    йдуть окремими поверхнями, і в кожному полі мають власний запис.
    """
    name: str
    stl_file: str
    # Найменший габарит частини, м. Потрібен лише для перевірки, чи сітка її
    # взагалі розвʼяже; нуль означає «невідомо» (наприклад, кейс відкрито з
    # маніфесту, де цього поля ще не було).
    span: float = 0.0
    # Площа поверхні частини, м². Потрібна рівно для одного — щоб оцінка
    # сітки бачила ВЛАСНИЙ рівень цієї частини (`level` нижче). Без неї
    # оцінка рахувала б усю поверхню за спільним рівнем і показувала б
    # мільйон там, де сітка виходить на чотири; людина побачила б
    # затишне число й дізналася правду, коли snappyHexMesh забракне
    # памʼяті. Нуль — «невідомо», і тоді ця частина рахується як усі.
    area: float = 0.0
    # ЧИМ ця частина є в розрахунку. Від цього залежить, що з нею робити:
    #
    #     body     нерухоме тіло — фюзеляж, крило, пілон;
    #     rotor    гвинт: навколо нього стає обертова зона (MRF);
    #     motion   рухома частина: саме вона коливається, а решта стоїть.
    #
    # Типово «body», і поки всі частини такі, словники виходять точно ті
    # самі, що й до появи ролей. Це перевіряється дифом.
    role: str = "body"
    # Оберти, якщо роль «rotor». Тримаємо тут, а не в зоні, бо зона
    # будується з ГЕОМЕТРІЇ частини, а оберти задає людина.
    rpm: float = 3000.0
    # ВЛАСНИЙ рівень згущення поверхні: (min, max). None — спільний для
    # всіх, як було.
    #
    # Потрібно там, де на великому тілі висить дрібна деталь. Лопать
    # 10-дюймового гвинта завтовшки 2.4 мм, а літак — метр завдовжки:
    # спільний рівень, якого досить для крила, робить лопать тоншою за
    # комірку. Snappy тоді не розвʼязує її взагалі, і тяга виходить
    # довільною — мовчки, без жодної помилки.
    #
    # Ціна теж своя: кожен зайвий рівень ділить комірку вдвічі по кожній
    # осі. Тому це окреме поле, а не підняття спільного рівня.
    level: tuple[int, int] | None = None
    # Якому двигуну належить кришка — лише для ролей intake і nozzle.
    engine: str = ""


# Ролі частин тіла: ключ, підпис у вікні, коротке пояснення.
PART_ROLES = (
    ("body", "body", "fixed part: fuselage, wing, pylon"),
    ("rotor", "propeller", "a rotating zone (MRF) will be placed around it"),
    ("motion", "moving part", "only this part oscillates, the rest stays still"),
    ("intake", "intake",
     "cap of an opening: here the engine takes air from the flow"),
    ("nozzle", "nozzle", "cap of the nozzle exit: the jet comes out from here"),
    ("disk", "actuator disk",
     "a propeller without blades: thrust and swirl are set by numbers"),
)
ROLE_KEYS = tuple(k for k, _, _ in PART_ROLES)

# Роль, з якої в сітку не йде НІЧОГО: ні стінки, ні отвору. Модель лише
# каже, де стоїть гвинт і якого він розміру; сам гвинт — це сила в диску
# комірок (`ActuatorDisk`).
DISK_ROLE = "disk"

# Профілі тяги по радіусу гвинта-диска.
DISK_PROFILES = (
    ("realistic", "realistic", "zero at the spinner and at the tip, maximum closer to the tip"),
    ("uniform", "uniform", "the same thrust on each square centimeter"),
)
DISK_PROFILE_KEYS = tuple(k for k, _, _ in DISK_PROFILES)
# Скільки комірок має лягати на діаметр диска, щоб тяга лягла диском, а
# не плямою.
DISK_CELLS_ACROSS = 20
# Найменша товщина шару диска, у комірках.
DISK_CELLS_THICK = 3
# Типовий радіус кока, частка радіуса гвинта (Q11).
DISK_HUB_FRACTION = 0.15


@dataclass
class ActuatorDisk:
    """Гвинт без лопатей: тяга T і крутний момент Q, розкладені по диску.

    Повітря не бачить лопатей, лише силу: осьову (тяга) і дотичну
    (закрутка). Сітці не треба розвʼязувати лопаті завтовшки два міліметри,
    а обдув крила й хвоста виходить.

    `axis` — куди диск ШТОВХАЄ ПОВІТРЯ, тобто напрямок струменя за ним. Сила
    на апарат — протилежна. `spin` — бік закрутки за правилом правої руки
    навколо `axis`.

    Сила задається виразом (`exprField`), нормованим на самій сітці:
    скільки б комірок не потрапило в диск, сума сил рівно T, сума
    моментів рівно Q. Перевірено на кубі 40³.
    """
    name: str = "disk1"
    centre: tuple[float, float, float] = (0.0, 0.0, 0.0)
    axis: tuple[float, float, float] = (1.0, 0.0, 0.0)
    radius: float = 0.1          # м, радіус гвинта
    hub: float = 0.015           # м, радіус кока
    thickness: float = 0.0       # м; 0 — вибере програма (DISK_CELLS_THICK)
    thrust: float = 5.0          # Н при повному газі
    torque: float = 0.0          # Н·м при повному газі; 0 — без закрутки
    spin: int = 1                # +1 / −1
    profile: str = "realistic"
    from_throttle: bool = True
    refine: bool = True
    throttle: float = 1.0        # поточний газ; кладе вікно

    @property
    def throttle_factor(self) -> float:
        if not self.from_throttle:
            return 1.0
        g = min(max(float(self.throttle), 0.0), 1.0)
        return g * g

    @property
    def thrust_now(self) -> float:
        return max(float(self.thrust), 0.0) * self.throttle_factor

    @property
    def torque_now(self) -> float:
        return max(float(self.torque), 0.0) * self.throttle_factor

    @property
    def area(self) -> float:
        return math.pi * max(self.radius ** 2 - self.hub ** 2, 0.0)

# Ролі, з яких виходить НЕ стінка, а отвір у тілі: повітря крізь цю
# поверхню входить у двигун або виходить із нього. Самого двигуна в
# сітці немає — обʼєм між кришками лишається всередині тіла, і саме це
# спрощення дозволяє не моделювати ні каналу, ні лопаток.
PORT_ROLES = ("intake", "nozzle")

# Види двигунів. Різниця лише в струмені: EDF дає холодний, турбіна —
# гарячий, і гарячий легший, тож за тієї самої тяги він виходить швидше.
ENGINE_KINDS = (
    ("edf", "impeller (EDF)", "cold jet"),
    ("turbojet", "turbojet", "hot jet"),
)
ENGINE_KIND_KEYS = tuple(k for k, _, _ in ENGINE_KINDS)

# Що означає введена тяга двигуна (Q31).
THRUST_MODES = (
    ("static", "maximum static", "from the spec sheet: test stand, full throttle"),
    ("flight", "in flight", "at this speed, full throttle"),
)
THRUST_MODE_KEYS = tuple(k for k, _, _ in THRUST_MODES)

# Турбулентність на зрізі сопла. Струмінь із двигуна значно турбулентніший
# за набігаючий потік, і 5 % — звичне значення для виходу каналу. Масштаб
# довжини — 7 % гідравлічного діаметра, класична оцінка для розвиненої
# течії в трубі.
NOZZLE_INTENSITY = 0.05
NOZZLE_MIXING_FRACTION = 0.07
# На скільки рівнів вище за спільний програма сама може згустити кришку.
PORT_LEVEL_HEADROOM = 4
# Скільки комірок має лягати на діаметр кришки, щоб струмінь був струменем.
PORT_CELLS_ACROSS = 10
# Товщина шару джерел порталу, у комірках.
PORTAL_LAYER_CELLS = 2
# За скільки проходів струменя крізь свій діаметр джерела порталу
# наростають до повної сили (див. portal_ramp).
PORTAL_RAMP_PASSES = 10.0
# Скільки комірок шару порталу струмінь може пролетіти за один крок часу.
PORTAL_JET_CO = 1.0
# Відлікова температура чутливої ентальпії в hConst (Tstd OpenFOAM), К.
# Енергія, яку приносить струмінь, рахується від неї: Hs = Cp·(T − Tref).
FOAM_T_REF = 298.15


@dataclass
class Engine:
    """Двигун без двигуна: пара кришок, intake і nozzle, і числа між ними.

    Сітка двигуна не бачить. Вона бачить дві поверхні: на одній повітря
    йде з потоку геть (його «ковтнув» двигун), на другій — повертається
    струменем. Цього досить, щоб планер відчув і підсмоктування перед
    повітрозабірником, і струмінь біля хвоста.

    Числа задаються ТЯГОЮ, бо її знають із паспорта. Витрату й швидкість
    струменя програма рахує сама з площі сопла (`engine_point`).
    """
    name: str = "engine1"
    kind: str = "edf"
    # Що означає `thrust` (Q31):
    #   static — МАКСИМАЛЬНА СТАТИЧНА тяга з паспорта (стенд, V = 0, повний
    #            газ). З неї визначається потік двигуна, ṁ_max і Vₑ_max, а
    #            газ масштабує сам потік, як оберти. Тяга в польоті
    #            виходить сама й падає зі швидкістю;
    #   flight — тяга В ПОЛЬОТІ на цій швидкості при повному газі, газ²
    #            (як було до Q31; так відкриваються старі проєкти).
    thrust_mode: str = "static"
    thrust: float = 20.0
    # Швидкість вихлопу з паспорта, м/с, на повному газі (Q38). Нуль —
    # невідома: тоді вона виходить із тяги й площі сопла. Задана — витрата
    # береться з тяги (ṁ = F/Vₑ), і площа на числа не впливає.
    v_jet: float = 0.0
    # Температура струменя на зрізі сопла, К. Лише для турбіни.
    t_jet: float = 900.0
    # Частка палива в струмені: з сопла виходить на стільки більше маси,
    # ніж увійшло в повітрозабірник. EDF — нуль.
    fuel_frac: float = 0.0
    # Розширений режим: витрата й швидкість струменя напряму, а тяга вже
    # лише показується.
    direct: bool = False
    mdot: float = 0.5           # кг/с крізь сопло
    v_exit: float = 50.0        # м/с на зрізі сопла
    # Чи залежить від газу на пульті. Газ — частка обертів, тому тяга
    # іде як газ² (див. `thrust_now`).
    from_throttle: bool = True
    # Чи згущувати сітку на кришках самій програмі (Q14).
    refine: bool = True
    # --- Що прийшло з геометрії. Заповнює вікно, а не людина. ---------
    nozzle: str = ""
    intakes: tuple[str, ...] = ()
    nozzle_area: float = 0.0     # м²
    nozzle_dh: float = 0.0       # гідравлічний діаметр, м
    intake_areas: tuple[float, ...] = ()
    # Напрямок струменя: середня нормаль сопла, зважена площею. Для порталу
    # це й АКТИВНИЙ бік: куди дивиться нормаль STL, туди й дме (Q27).
    nozzle_normal: tuple[float, float, float] = (1.0, 0.0, 0.0)
    # |Σ n·dA| сопла, м². Для плаского дорівнює площі; для кривого менше,
    # і лише ця частка імпульсу струменя йде вздовж осі (`eta`).
    nozzle_proj: float = 0.0

    @property
    def eta(self) -> float:
        """Частка імпульсу струменя вздовж осі: 1 для плаского сопла."""
        if self.nozzle_proj <= 0 or self.nozzle_area <= 0:
            return 1.0
        return min(max(self.nozzle_proj / self.nozzle_area, 1e-3), 1.0)
    # Поточний газ, 0..1. Його знає пульт, а не конфіг; вікно кладе сюди.
    throttle: float = 1.0

    @property
    def hot(self) -> bool:
        return self.kind == "turbojet"

    @property
    def gas(self) -> float:
        """Газ 0..1, або 1, якщо двигун від газу не залежить."""
        if not self.from_throttle:
            return 1.0
        return min(max(float(self.throttle), 0.0), 1.0)

    @property
    def throttle_factor(self) -> float:
        """Множник ТЯГИ від газу на стенді: газ² (газ — частка обертів)."""
        return self.gas ** 2


@dataclass
class EnginePoint:
    """Робоча точка двигуна: усе, що піде в граничні умови й у звіт."""
    mdot_nozzle: float          # кг/с
    mdot_intakes: tuple[float, ...]
    v_exit: float               # м/с
    rho_exit: float             # кг/м³
    t_exit: float               # К
    thrust: float               # Н, F = ṁₙ·Vₑ − ṁᵢ·V∞
    mach_exit: float
    v_intakes: tuple[float, ...]
    mach_intakes: tuple[float, ...]
    # Площа зрізу, через яку ця витрата пройде з цією швидкістю, м².
    area_needed: float = 0.0

    @property
    def mdot_intake(self) -> float:
        return sum(self.mdot_intakes)


def engine_point(e: Engine, flow: "FlowCfg") -> EnginePoint:
    """Витрата й швидкість струменя з тяги — або навпаки, у розширеному.

    Звʼязок тут один, імпульсний: F = ṁₙ·Vₑ − ṁᵢ·V∞, де ṁₙ = ṁᵢ·(1 + f)
    і ṁₙ = ρₑ·Aₑ·Vₑ. Тиск на зрізі вважаємо рівним атмосферному —
    так працює дозвукове сопло. Підстановка дає квадратне рівняння на Vₑ:

        ρₑ·Aₑ·Vₑ² − ρₑ·Aₑ·b·Vₑ − F = 0,   b = V∞ / (1 + f)

    з єдиним додатним коренем. Перевіряється підстановкою назад у тестах.

    Кілька повітрозабірників ділять витрату пропорційно площі: так вони
    й працюють, коли канали однакові, а інакше сказати про них нема з чого.
    """
    v_inf = max(float(flow.u_inf), 0.0)
    rho_inf = float(flow.rho)
    f = max(float(e.fuel_frac), 0.0)
    if flow.compressible:
        r_gas = flow.p_inf / max(rho_inf * flow.t_inf, 1e-12)
        t_exit = float(e.t_jet) if e.hot else float(flow.t_inf)
        rho_exit = flow.p_inf / max(r_gas * t_exit, 1e-12)
        a_exit = phys.speed_of_sound(t_exit)
    else:
        # У нестисливому температури немає: струмінь має ту саму густину,
        # що й повітря довкола, хоч би який гарячий був насправді.
        t_exit = float(flow.t_inf)
        rho_exit = rho_inf
        a_exit = flow.sound_speed
    area = max(float(e.nozzle_area), 0.0)
    k = e.throttle_factor
    # Криве сопло дме по своїх нормалях, і вздовж осі йде лише частка η
    # імпульсу. Тоді F = η·ṁₙ·Vₑ − ṁᵢ·V∞, і корінь:
    #     Vₑ = (b + √(b² + 4·η·F/(ρₑ·A))) / (2·η)
    # Для плаского η = 1, і це рівно формула вище.
    eta = e.eta
    v_jet = max(float(e.v_jet), 0.0)
    if e.direct:
        mdot_n = max(float(e.mdot), 0.0) * k
        v_exit = max(float(e.v_exit), 0.0)
    elif v_jet > 0 and e.thrust_mode == "static":
        # Q38: ШВИДКІСТЬ ВИХЛОПУ З ПАСПОРТА — тоді витрата не з площі, а з
        # тяги: на стенді F_max = η·ṁ_max·Vₑ_max. Газ, як і без швидкості,
        # масштабує потік: Vₑ і ṁ ∝ газ. Площа кришки на числа вже не
        # впливає — для порталу це правильно (він вкидає рівно ṁ і ṁ·Vₑ),
        # а для кришки-патча guard_engines скаже, якщо діаметр не той.
        f_max = max(float(e.thrust), 0.0)
        v_exit = e.gas * v_jet
        mdot_n = e.gas * f_max / (eta * v_jet) if f_max > 0 else 0.0
    elif v_jet > 0:
        # Q38, тяга в польоті: F = η·ṁ·Vₑ − ṁ·V∞/(1+f) ⇒ ṁ = F/(η·Vₑ − b).
        thrust = max(float(e.thrust), 0.0) * k
        v_exit = v_jet
        room = eta * v_jet - v_inf / (1.0 + f)
        mdot_n = thrust / room if thrust > 0 and room > 0 else 0.0
    elif e.thrust_mode == "static":
        # Q31: МАКСИМАЛЬНА СТАТИЧНА тяга визначає потік двигуна. На стенді
        # V = 0 і повітрозабірник імпульсу не забирає, тож
        #     F_max = η·ṁ_max·Vₑ_max = η·ρₑ·A·Vₑ_max²
        # Газ — частка обертів: і швидкість струменя, і витрата йдуть як
        # газ, а статична тяга — як газ². У польоті тяга вже не задана, а
        # ВИХОДИТЬ: F = η·ṁ·Vₑ − ṁ_вх·V∞, і падає зі швидкістю.
        f_max = max(float(e.thrust), 0.0)
        if area <= 0 or f_max <= 0:
            mdot_n, v_exit = 0.0, 0.0
        else:
            v_max = math.sqrt(f_max / (rho_exit * area * eta))
            v_exit = e.gas * v_max
            mdot_n = rho_exit * area * v_exit
    else:
        thrust = max(float(e.thrust), 0.0) * k
        if area <= 0 or thrust <= 0:
            mdot_n, v_exit = 0.0, 0.0
        else:
            b = v_inf / (1.0 + f)
            v_exit = (b + math.sqrt(b * b + 4.0 * eta * thrust
                                    / (rho_exit * area))) / (2.0 * eta)
            mdot_n = rho_exit * area * v_exit
    mdot_i_total = mdot_n / (1.0 + f)
    areas = tuple(max(float(a), 0.0) for a in e.intake_areas)
    if not areas:
        areas = (1.0,) * max(len(e.intakes), 1)
    total = sum(areas) or 1.0
    shares = tuple(mdot_i_total * a / total for a in areas)
    a_inf = flow.sound_speed
    v_in = tuple((m / (rho_inf * a) if a > 0 and e.intake_areas else 0.0)
                 for m, a in zip(shares, areas))
    return EnginePoint(
        mdot_nozzle=mdot_n,
        mdot_intakes=shares,
        v_exit=v_exit,
        rho_exit=rho_exit,
        t_exit=t_exit,
        thrust=eta * mdot_n * v_exit - mdot_i_total * v_inf,
        mach_exit=v_exit / a_exit if a_exit > 0 else 0.0,
        v_intakes=v_in,
        mach_intakes=tuple(v / a_inf if a_inf > 0 else 0.0 for v in v_in),
        area_needed=(mdot_n / (rho_exit * v_exit)
                     if mdot_n > 0 and v_exit > 0 else 0.0),
    )

# --- ВІРТУАЛЬНИЙ ПУЛЬТ ------------------------------------------------------
#
# Канали названо так, як на апаратурі, а не так, як в аеродинаміці: людина,
# що ставить літак у позицію, думає стіками, а не моментами. Межі теж
# пультові: стік іде від -1 до +1, газ — від 0 до 1, бо назад він не ходить.
# Газ типово повний (1.0): кейс із тягою найчастіше рахують на повному газі,
# а на нульовому тяги немає зовсім.
#
# (ключ, підпис, мінімум, максимум, типове значення, що зазвичай чіпляють)
RC_CHANNELS = (
    ("roll", "roll", -1.0, 1.0, 0.0, "ailerons"),
    ("pitch", "pitch", -1.0, 1.0, 0.0, "elevator"),
    ("yaw", "yaw", -1.0, 1.0, 0.0, "rudder"),
    ("throttle", "throttle", 0.0, 1.0, 1.0, "propeller rpm"),
    ("flaps", "flaps", 0.0, 1.0, 0.0, "high-lift devices"),
    ("aux", "aux channel", -1.0, 1.0, 0.0, "anything"),
)
CHANNEL_KEYS = tuple(k for k, *_ in RC_CHANNELS)
CHANNEL_RANGE = {k: (lo, hi, mid) for k, _, lo, hi, mid, _ in RC_CHANNELS}


@dataclass
class Hinge:
    """Завіса однієї деталі: де вісь, куди відхиляється, від чого залежить.

    ЗАВІСА — ЦЕ ПРЯМА, А НЕ ТОЧКА. Елерон крутиться навколо своєї передньої
    кромки, а не навколо власного центра; якщо переплутати, деталь
    сповзе з гнізда й у крилі зʼявиться дірка. Тому `pivot` і `axis`
    задаються окремо, а не виводяться з габариту.

    `offset` — поправка ПІСЛЯ повороту, на випадок коли завіса в моделі не
    там, де у вузлі: CAD часто віддає деталь, зняту з літака.

    Кут рахується з каналу пульта, а не вводиться руками:

        кут = trim + gain * стік        (і обрізається по limit)

    `gain` — скільки градусів дає повний хід стіка, `trim` — відхилення на
    нейтралі, `reverse` — той самий перемикач, що й на апаратурі: два
    елерони сидять на одному каналі й мають ходити НАЗУСТРІЧ, тож одному
    з них реверс і ставлять.
    """

    pivot: tuple[float, float, float] = (0.0, 0.0, 0.0)
    axis: tuple[float, float, float] = (0.0, 1.0, 0.0)
    offset: tuple[float, float, float] = (0.0, 0.0, 0.0)
    channel: str = ""          # порожньо — деталь не керована
    gain: float = 20.0         # градусів на повний хід стіка
    trim: float = 0.0          # градусів на нейтралі
    limit: float = 30.0        # більше не відхилиться, хоч би що
    reverse: bool = False

    @property
    def on(self) -> bool:
        return bool(self.channel) and self.channel in CHANNEL_KEYS

    def angle(self, value: float) -> float:
        """Кут відхилення в градусах для даного положення стіка.

        Обрізання по `limit` — не примха, а модель залізяки: справжня
        поверхня впирається в обмежувач, а не відхиляється далі. Без
        обрізання великий `gain` тихо дав би елерон під 90°, і сітка
        будувалася б навколо геометрії, якої в літаку не буває.
        """
        v = -float(value) if self.reverse else float(value)
        a = float(self.trim) + float(self.gain) * v
        lim = abs(float(self.limit))
        return max(-lim, min(lim, a))

    def axis_length(self) -> float:
        return math.sqrt(sum(float(v) ** 2 for v in self.axis))


def guard_hinges(items) -> list[tuple[str, str]]:
    """Що не так у налаштуванні керування. `items` — [(імʼя, Hinge), ...].

    Кожна з цих вад дає РОЗРАХУНОК БЕЗ ПОМИЛКИ і геометрію, яка виглядає
    правдоподібно. Саме тому вони тут, а не лишені на око.
    """
    out: list[tuple[str, str]] = []
    live = [(n, h) for n, h in items if h is not None and h.channel]
    for name, h in live:
        if h.channel not in CHANNEL_KEYS:
            out.append(("bad", (
                tr("“{name}”: there is no channel “{channel}”. "
                   "Available: {CHANNEL_KEYS}.", name=name, channel=h.channel, CHANNEL_KEYS=', '.join(CHANNEL_KEYS)))))
        if h.axis_length() < 1e-9:
            out.append(("bad", (
                tr("“{name}”: the hinge axis has zero length — there "
                   "will be no rotation at all, and no error either.", name=name))))
        if abs(float(h.limit)) < 1e-9:
            out.append(("bad", (
                tr("“{name}”: the deflection limit is zero, the surface "
                   "will stay still, even though a channel is assigned.", name=name))))
        elif abs(float(h.trim)) > abs(float(h.limit)) + 1e-9:
            out.append(("warn", (
                tr("“{name}”: trim {trim:g}° is greater than the limit "
                   "{limit:g}° — the surface hits the stop already at "
                   "neutral and will not move further.", name=name, trim=h.trim, limit=h.limit))))
        if abs(float(h.gain)) < 1e-9:
            out.append(("warn", (
                tr("“{name}”: the channel travel is zero — the stick "
                   "will move, but the surface will not. If this is "
                   "intended, remove the channel.", name=name))))

    # ДВІ ПОВЕРХНІ НА ОДНОМУ КАНАЛІ мають ходити НАЗУСТРІЧ, інакше це не
    # елерони, а закрилки. Найтихіша вада в усьому наборі: модель
    # будується, сітка будується, момент крену просто не виникає.
    by_channel: dict[str, list[tuple[str, Hinge]]] = {}
    for name, h in live:
        by_channel.setdefault(h.channel, []).append((name, h))
    for ch, group in by_channel.items():
        if ch in ("throttle", "flaps") or len(group) < 2:
            continue
        signs = {(-1 if h.reverse else 1) * (1 if h.gain >= 0 else -1)
                 for _, h in group}
        if len(signs) == 1:
            names = ", ".join(n for n, _ in group)
            out.append(("warn", (
                tr("On channel “{ch}”, several surfaces ({names}) move "
                   "IN THE SAME DIRECTION. Ailerons need reverse on one "
                   "of them — otherwise they work as flaps and give no "
                   "moment.", ch=ch, names=names))))
    return out


def collapse_zone_notes(notes: list[tuple[str, str]],
                        names) -> list[tuple[str, str]]:
    """Скласти застороги, що різняться лише іменем зони, в одну.

    Чотиримоторний апарат дає чотири копії кожного попередження — слово в
    слово, з різним лише іменем. У панелі це стіна тексту, у якій тоне те
    єдине, що справді різне. А саме заради того єдиного список і читають.

    Імена решти зон ДОПИСУЮТЬСЯ окремим рядком, а не викидаються: зникле
    імʼя означало б, що про ту зону змовчали, і людина шукала б її біду
    окремо.

    Імена приміряються від найдовшого. На справжньому апараті вони були
    `prop_8x4_5_2_2` і `prop_8x4_5_2_2_1_1` — коротке є початком довгого,
    і в зворотному порядку від довгого імені лишався б хвіст `_1_1`.
    """
    mark = "\x00"
    order: list[str] = []
    bag: dict[str, list] = {}
    for lvl, text in notes:
        key, hit = text, []
        for nm in sorted((n for n in names if n), key=len, reverse=True):
            if nm in key:
                key = key.replace(nm, mark)
                hit.append(nm)
        if key in bag:
            bag[key][2].extend(n for n in hit if n not in bag[key][2])
        else:
            order.append(key)
            bag[key] = [lvl, text, hit]
    out = []
    for key in order:
        lvl, text, hit = bag[key]
        if len(hit) > 1:
            text += tr("  The same in the zones: {join}.", join=', '.join(hit[1:]))
        out.append((lvl, text))
    return out


# Наскільки зона ширша за сам гвинт. Запас потрібен, щоб лопаті не
# торкалися стінки циліндра, але це запас ЗОНИ, а не метал. Там, де
# потрібен діаметр гвинта (коефіцієнт ходу), його звідси й віднімають —
# тому число одне на обидва місця, щоб вони не розʼїхалися.
ROTOR_ZONE_MARGIN = 1.10

# Коефіцієнт ходу J = V/(nD) — скільки гвинт проходить уздовж осі за один
# оберт, у діаметрах. Тяга є приблизно до J ≈ P/D, тобто до кроку гвинта,
# а далі лопать переходить у режим вітряка й ГАЛЬМУЄ.
#
# Межі нижче не з голови:
#     модельні гвинти фіксованого кроку      P/D 0.4...1.0
#     літакові гвинти змінного кроку         J до 1.5...2
#     швидкісні турбогвинтові (Ту-95, A400M) J до 3, і це вже край
ADVANCE_RATIO_NORMAL = 1.0      # робоча точка, від якої рахується підказка
ADVANCE_RATIO_WARN = 2.5
ADVANCE_RATIO_BAD = 4.0


def rotor_zone_from_points(name: str, points, rpm: float, axis: str = "x",
                           margin: float = ROTOR_ZONE_MARGIN,
                           allow_flat: bool = False):
    """Обертова зона з ТОЧОК моделі гвинта навколо заданої осі, або None.

    Міряємо ВІДСТАНЬ ВІД ОСІ, а не габарит. Різниця принципова, і вона
    коштувала попередньої версії цієї функції.

    Та версія брала габаритну коробку й визначала вісь як найтонший
    напрямок. На суцільному диску це працює. На СПРАВЖНЬОМУ ГВИНТІ — ні:

        лопатей   габарит, мм        найтонший   радіус від X   товщина
        2         44 x 39 x 300      Y (!)       150 мм         44 мм
        3         44 x 261 x 226     X           150 мм         44 мм
        4         44 x 300 x 300     X           150 мм         44 мм

    Двулопатевий гвинт габаритом на диск не схожий узагалі — це хрест
    завтовшки з хорду. Стара перевірка його відхиляла, а вісь визначила б
    як Y замість X. І це не вада перевірки, а межа самого способу:
    плаский двулопатевий гвинт геометрично НЕ ВІДРІЗНИТИ від пластини, він
    і справді міг би крутитися навколо обох осей.

    Тому вісь задає людина — типово вздовж потоку, бо саме так стоїть
    гвинт на літаку. А з геометрії береться те, що з неї справді
    вимірюється однозначно: найбільший радіус від осі й товщина вздовж
    неї. У таблиці вище обидва числа однакові для будь-якої кількості
    лопатей.

    None повертається, коли вздовж осі модель товща, ніж широка: це вже не
    гвинт навколо цієї осі, і мовчки ставити зону там не можна.
    """
    p = [tuple(float(v) for v in q) for q in points]
    if len(p) < 3:
        return None
    i = {"x": 0, "y": 1, "z": 2}.get(str(axis).lower())
    if i is None:
        return None
    j, k = [t for t in range(3) if t != i]

    lo_a = min(q[i] for q in p)
    hi_a = max(q[i] for q in p)
    # Вісь шукаємо через СЕРЕДНЄ точок, а не через середину габариту.
    # У гвинта з парним числом лопатей це те саме, а з непарним — ні:
    # габаритна коробка трилопатевого гвинта не центрована на осі, і
    # радіус від її середини виходив 189 мм замість 165. Центр маси
    # лежить на осі за симетрією, скільки б лопатей не було.
    cj = sum(q[j] for q in p) / len(p)
    ck = sum(q[k] for q in p) / len(p)
    thick = hi_a - lo_a
    radius = max(math.hypot(q[j] - cj, q[k] - ck) for q in p)
    # Плаский диск — законна форма для гвинта-диска (його товщину задає
    # сітка, а не модель). Для MRF — ні: зона нульової товщини порожня.
    if allow_flat and thick <= 0 and radius > 0:
        thick = 1e-9 * radius
    if radius <= 0 or thick <= 0:
        return None
    # Уздовж осі гвинт має бути ТОНШИЙ за свій радіус. Інакше це або не
    # гвинт, або не та вісь — і в обох випадках зона стала б не туди.
    if thick > radius:
        return None

    centre = [0.0, 0.0, 0.0]
    centre[i] = 0.5 * (lo_a + hi_a)
    centre[j], centre[k] = cj, ck
    return RotatingZone(name=name, centre=tuple(centre),
                        axis=tuple(1.0 if t == i else 0.0 for t in range(3)),
                        radius=radius * margin,
                        length=thick * 2.0, rpm=float(rpm))


def patch_name_from(source: str, taken: frozenset[str] = frozenset()) -> str:
    """Імʼя патча з назви файлу — придатне для OpenFOAM і неповторне.

    OpenFOAM приймає за імʼя границі лише «слово»: латиниця, цифри й
    підкреслення. Файл «Крило ліве.stl» дав би імʼя, на якому падає розбір
    словника, і побачила б це людина аж у журналі snappyHexMesh. Тому все
    незугарне стає підкресленням, кирилиця — транслітерується, а порожній
    результат замінюється на body.
    """
    stem = source.rsplit("/", 1)[-1].rsplit("\\", 1)[-1]
    stem = stem.rsplit(".", 1)[0] if "." in stem else stem
    out = []
    for ch in stem.lower():
        if ch.isascii() and (ch.isalnum() or ch == "_"):
            out.append(ch)
        else:
            out.append(_TRANSLIT.get(ch, "_"))
    name = "".join(out).strip("_")
    while "__" in name:
        name = name.replace("__", "_")
    # Імʼя, що починається з цифри, OpenFOAM теж не любить.
    if not name or not name[0].isalpha():
        name = "body" + ("_" + name if name else "")
    if name not in taken:
        return name
    n = 2
    while f"{name}_{n}" in taken:
        n += 1
    return f"{name}_{n}"


_TRANSLIT = {
    "а": "a", "б": "b", "в": "v", "г": "h", "ґ": "g", "д": "d", "е": "e",
    "є": "ie", "ж": "zh", "з": "z", "и": "y", "і": "i", "ї": "i", "й": "i",
    "к": "k", "л": "l", "м": "m", "н": "n", "о": "o", "п": "p", "р": "r",
    "с": "s", "т": "t", "у": "u", "ф": "f", "х": "kh", "ц": "ts", "ч": "ch",
    "ш": "sh", "щ": "shch", "ь": "", "ю": "iu", "я": "ia", "ы": "y",
    "э": "e", "ё": "e", "ъ": "", " ": "_", "-": "_",
}


POROUS_MODES = ("inertial", "viscous")


@dataclass
class PorousZone:
    """Пориста зона: радіатор, сітка, фільтр, парашут.

    Задається НЕ коефіцієнтами Дарсі — їх ніхто не знає напамʼять, — а тим,
    що є в паспорті: скільки паскалів втрачається при такій-то швидкості.
    Звідси й рахуються d та f (`darcy()`).

    `axis` — напрямок «крізь». Опір уздовж нього створює перепад тиску;
    поперек ставимо більший, щоб потік не обтікав зону боком замість
    проходити крізь неї.
    """
    name: str = "porous"
    centre: tuple[float, float, float] = (0.0, 0.0, 0.0)
    size: tuple[float, float, float] = (0.1, 0.1, 0.02)
    axis: tuple[float, float, float] = (1.0, 0.0, 0.0)
    dp: float = 200.0          # перепад тиску, Па
    u_ref: float = 10.0        # при якій швидкості крізь зону, м/с
    mode: str = "inertial"     # inertial (сітки, радіатори) | viscous (фільтри)
    # У скільки разів опір поперек більший за опір уздовж. Нуль тут означав
    # би, що зона нічим не відрізняється від порожнечі збоку, і потік просто
    # обтікав би її.
    cross_factor: float = 10.0

    @property
    def thickness(self) -> float:
        """Товщина зони вздовж `axis` — та довжина, на якій падає тиск.

        Проєкція коробки на напрямок «крізь», а не просто одна зі сторін:
        для похилої зони довжина шляху крізь неї більша за будь-яку сторону.
        """
        return max(sum(abs(u) * s for u, s in zip(_unit(self.axis), self.size)),
                   1e-9)

    def darcy(self, nu: float, rho: float) -> tuple[float, float]:
        """(d, f) — вʼязкий і інерційний коефіцієнти вздовж `axis`.

        Перепад на пористому шарі: dp/L = mu*d*U + 0.5*rho*f*U².
        Режим каже, який із доданків вважати головним: у сітки чи радіатора
        втрати ростуть як квадрат швидкості, у дрібного фільтра — лінійно.
        """
        L = self.thickness
        u = max(self.u_ref, 1e-9)
        if self.mode == "viscous":
            mu = max(nu * rho, 1e-30)
            return (self.dp / (mu * L * u), 0.0)
        return (0.0, 2.0 * self.dp / (max(rho, 1e-30) * L * u * u))

    @property
    def bounds(self) -> tuple[tuple[float, float, float],
                              tuple[float, float, float]]:
        lo = tuple(c - s / 2 for c, s in zip(self.centre, self.size))
        hi = tuple(c + s / 2 for c, s in zip(self.centre, self.size))
        return (lo, hi)


def _unit(v) -> tuple[float, float, float]:
    n = math.sqrt(sum(x * x for x in v)) or 1.0
    return (v[0] / n, v[1] / n, v[2] / n)


def _perpendicular(v) -> tuple[float, float, float]:
    """Будь-який одиничний вектор, перпендикулярний до `v`.

    Беремо ту з осей, з якою `v` найменше збігається: векторний добуток із
    майже паралельною віссю дає майже нуль, і нормування його підсилює шум
    замість напрямку.
    """
    a = _unit(v)
    axes = ((1.0, 0.0, 0.0), (0.0, 1.0, 0.0), (0.0, 0.0, 1.0))
    pick = min(axes, key=lambda e: abs(sum(x * y for x, y in zip(a, e))))
    cross = (a[1] * pick[2] - a[2] * pick[1],
             a[2] * pick[0] - a[0] * pick[2],
             a[0] * pick[1] - a[1] * pick[0])
    return _unit(cross)


def _square_pair(lo0: float, hi0: float, lo1: float, hi1: float,
                 ) -> tuple[float, float, float, float]:
    """Розсовує дві пари меж до однакової півширини — довшу лишає, коротшу
    росте до неї, обидві навколо своїх власних середин."""
    c0, h0 = (lo0 + hi0) / 2.0, (hi0 - lo0) / 2.0
    c1, h1 = (lo1 + hi1) / 2.0, (hi1 - lo1) / 2.0
    h = max(h0, h1)
    return (c0 - h, c0 + h, c1 - h, c1 + h)


@dataclass
class RotatingZone:
    """Циліндрична зона в обертовій системі відліку (MRF).

    Так рахують гвинти, ротори й вентилятори, не рухаючи сітку: усередині
    зони до рівнянь додаються відцентрова й коріолісова сили, тобто повітря
    «не знає», що воно обертається, а лопаті стоять на місці.

    Це наближення, і його межі варто знати. MRF дає СЕРЕДНІЙ за оберт стан,
    тому не бачить ні проходу лопаті повз пілон, ні нестаціонарних
    навантажень. І головне: він працює лише тоді, коли в зоні справді є
    ЛОПАТІ. Порожня зона просто закрутить повітря, не створивши тяги — і
    розрахунок при цьому пройде без жодної скарги.
    """
    name: str = "rotor"
    centre: tuple[float, float, float] = (0.0, 0.0, 0.0)
    axis: tuple[float, float, float] = (1.0, 0.0, 0.0)
    radius: float = 0.15
    length: float = 0.05        # довжина циліндра вздовж осі
    rpm: float = 3000.0
    # Патчі, які лишаються нерухомими, хоч і потрапили в зону: пілон,
    # фюзеляж. Типово порожньо — усе в зоні обертається разом із нею.
    non_rotating: tuple[str, ...] = ()
    # Зона зібрана з ГЕОМЕТРІЇ частини, позначеної гвинтом, а не задана
    # руками. Такі зони перебудовуються щоразу, коли сцена змінюється, і
    # вручну задану цим не стирає: інакше роль, якої ніхто не давав,
    # мовчки прибирала б чужу роботу.
    from_part: bool = False

    @property
    def omega(self) -> float:
        """Кутова швидкість, рад/с — саме в цих одиницях її хоче OpenFOAM."""
        return self.rpm * 2.0 * math.pi / 60.0

    @property
    def tip_speed(self) -> float:
        """Колова швидкість кінця лопаті, м/с. ЗАВЖДИ ДОДАТНА.

        Це швидкість, а не проєкція: напрямок обертання несе знак `rpm`, і
        кожен, кому він потрібен, має його там.

        Модуль тут не косметика. Раніше властивість віддавала знак, і в
        застороги стояло `mach(tip_speed) > 0.9`. Для гвинта, що крутиться
        в інший бік, це число виходило ВІДʼЄМНИМ — а отже, перевірка
        надзвукового кінця лопаті для нього не спрацьовувала ніколи. У
        панелі те саме бачили очима: «M = -0.16», чого не буває.

        Знайшлося на чотиримоторному апараті, де два гвинти з чотирьох
        крутяться проти годинникової: половина машини лишалася без
        перевірки, і мовчки.
        """
        return abs(self.omega) * self.radius

    @property
    def blade_radius(self) -> float:
        """Радіус САМОГО ГВИНТА, м — без запасу, з яким будується зона.

        Різниця в десять відсотків, і для стелі Маха вона дрібниця, а для
        коефіцієнта ходу — ні: діаметр стоїть у знаменнику.

        Ручну зону не чіпаємо. Її радіус задала людина, і запасу там може
        не бути зовсім; ділити навмання гірше, ніж не ділити.
        """
        return (self.radius / ROTOR_ZONE_MARGIN if self.from_part
                else self.radius)

    def advance_ratio(self, u_inf: float) -> float:
        """Коефіцієнт ходу J = V/(nD), безрозмірний.

        Скільки гвинт проходить уздовж осі за один оберт, поділене на його
        діаметр. Це головне число режиму гвинта: тяга є приблизно до
        J ≈ P/D, а за ним лопать працює вітряком.

        Нерухомий гвинт дає нескінченність, а не помилку: ділення на нуль
        тут має зміст «оберти не задані», і про самі нульові оберти
        сказано окремо.
        """
        n = abs(self.rpm) / 60.0
        d = 2.0 * self.blade_radius
        if n <= 0.0 or d <= 0.0:
            return math.inf
        return u_inf / (n * d)

    def inflow_angle(self, u_inf: float) -> float:
        """Кут набігання на кінці лопаті до площини обертання, градуси.

        Те саме, що й коефіцієнт ходу, тільки в градусах: tan(φ) = J/π.
        Двома числами, бо дивляться на різні — J шукають у таблицях
        гвинта, а кут одразу видно очима. Рахується ЧЕРЕЗ J, щоб вони не
        могли розійтися.

        Сенс простий: лопать тягне, поки її власний кут більший за цей.
        Кут у 60° означає, що потрібна лопать, закручена на 60°, — таких
        не буває.
        """
        j = self.advance_ratio(u_inf)
        return 90.0 if not math.isfinite(j) else math.degrees(
            math.atan(j / math.pi))

    @property
    def ends(self) -> tuple[tuple[float, float, float],
                            tuple[float, float, float]]:
        """Дві точки на осі — торці циліндра."""
        a = _unit(self.axis)
        half = self.length / 2.0
        return (tuple(c - u * half for c, u in zip(self.centre, a)),
                tuple(c + u * half for c, u in zip(self.centre, a)))


@dataclass
class RefinementBox:
    """Коробка обʼємного рефайнменту, в метрах, в координатах кейсу."""
    name: str
    min: tuple[float, float, float]
    max: tuple[float, float, float]
    level: int
    # ЗВІДКИ ЦЯ КОРОБКА ВЗЯЛАСЯ. Від цього залежить, чи можна її
    # перебудовувати:
    #
    #     body    з габариту тіла — перебудовується з полів панелі;
    #     zone    навколо обертової зони — їде слідом за нею, бо гвинт
    #             можуть посунути чи перечитати в інших одиницях;
    #     manual  поставлена руками — не чіпати НІКОЛИ.
    #
    # Без цієї позначки перегенерація мовчки затирала б чужу роботу, а
    # відмова від перегенерації лишала б коробку на старому місці —
    # згущувати порожнє повітря там, звідки гвинт уже поїхав.
    source: str = "manual"
    # Рівень, який ПОСТАВИЛА ЛЮДИНА. Перебудова бере координати наново, а
    # рівень — звідси: інакше кожен порух сцени повертав би його до
    # типового, і правити рівень було б неможливо.
    level_by_hand: bool = False

    @property
    def volume(self) -> float:
        return abs(
            (self.max[0] - self.min[0])
            * (self.max[1] - self.min[1])
            * (self.max[2] - self.min[2])
        )


@dataclass
class DomainCfg:
    """Розміри аеротруби у кратностях довжини тіла (вздовж потоку)."""
    upstream: float = 5.0
    downstream: float = 12.0
    side: float = 5.0
    above: float = 5.0
    below: float = 5.0
    base_cells_along_x: int = 40
    ground: bool = False          # zmin як стінка (ефект землі)
    ground_offset: float = 0.0    # абсолютна висота нижньої межі, якщо ground=True


WINDWARD_STL = "windward.stl"


@dataclass
class RefineSurface:
    """Поверхня, яка існує лише щоб від неї міряти відстань.

    У geometry вона є, у refinementSurfaces — ні, і це принципово: інакше
    snappyHexMesh зробив би з неї ще одну границю сітки. Тіло розрізалося б
    на частини, сили порахувалися б окремо по кожній, і зрозуміти, чому їх
    раптом стало три замість однієї, було б важко.

    `levels` — ті самі сходинки, що й в оболонки: [(відстань, рівень), ...],
    відстані ростуть, рівні спадають.
    """
    name: str
    stl_file: str
    levels: list[tuple[float, int]] = field(default_factory=list)


@dataclass
class MeshCfg:
    surface_level_min: int = 4
    surface_level_max: int = 5
    feature_level: int = 5
    resolve_feature_angle: int = 30
    included_angle: int = 150
    n_cells_between_levels: int = 3
    max_global_cells: int = 6_000_000
    max_local_cells: int = 2_000_000
    boxes: list[RefinementBox] = field(default_factory=list)

    # РУЧНА КОРОБКА. Типово коробка рахується з габариту тіла трьома
    # числами (запас, слід, рівень) і виходить симетричною. Цього досить
    # для літака в потоці, але не для всього: слід за гвинтом іде вбік,
    # зривна зона сидить над крилом, а не навколо нього. Тому координати
    # можна задати й руками.
    #
    # Прапорець окремо від координат навмисно: вимкнув — і повернулася
    # порахована коробка, а введене лишилося на місці й чекає.
    box_manual: bool = False
    box_min: tuple[float, float, float] = (0.0, 0.0, 0.0)
    box_max: tuple[float, float, float] = (0.0, 0.0, 0.0)

    # Оболонка згущення: зона, задана ВІДСТАННЮ від поверхні, а не
    # координатами. Коробка навколо тонкого тіла згущує гори порожнього
    # повітря над ним і під ним: на тестовому літаку коробка займає 0.80 м³,
    # а оболонка завтовшки 60 мм — 0.040 м³, тобто вдвадцятеро менше, і при
    # цьому щільно облягає всю поверхню, а не тільки її передню частину.
    # У snappyHexMesh це refinementRegions з mode distance.
    shell_on: bool = False
    # Зовнішня межа оболонки, м. Нуль — порахувати від поверхневої комірки.
    shell_reach: float = 0.0
    # Рівень біля самої поверхні. Нуль — на одиницю вище за поверхневий.
    shell_level: int = 0
    shell_steps: int = 3

    # Навітряна зона: та сама оболонка, але лише на гранях, куди бʼє потік,
    # і товща. Там сидить пік розрідження, який робить підйомну силу; решта
    # тіла такої роздільності не потребує.
    windward_on: bool = False
    windward_reach: float = 0.0
    windward_level: int = 0
    # Косинус кута між нормаллю грані й потоком, вище якого грань уже не
    # вважається навітряною. Нуль — рівно навітряна половина тіла.
    #
    # Поріг міряний, не вгаданий. На тестовому літаку:
    #   cos < +0.17  бере 97% граней — для тонкого крила це все тіло, бо
    #                верх і низ мають нормаль майже впоперек потоку
    #   cos < 0.00   бере 59%, на крилі це передні 0-32% хорди
    #   cos < -0.34  бере 32%, на крилі лише передні 5% — саму кромку,
    #                без розгону за нею
    windward_cos: float = 0.0

    # Довільні поверхні, від яких згущувати. Сюди лягає зона, знайдена
    # дешевим прогоном: він показує, ДЕ потік активний, і це не виводиться
    # з геометрії ніяк — слід, вихор із законцівки, відрив.
    refine_surfaces: list[RefineSurface] = field(default_factory=list)

    add_layers: bool = True
    n_layers: int = 5
    expansion: float = 1.2
    final_layer_rel: float = 0.5
    min_thickness_rel: float = 0.05
    # y+ target of the layers. With yplus_auto the program picks it from the
    # turbulence model and Re (fs_yplus.auto_pick) and fits the layer count.
    # A y+ typed by hand, the «Підібрати шари» button or a typed layer count
    # turns auto off. Old projects have no such keys: auto is on for them.
    yplus_auto: bool = True
    target_yplus: float = 0.0     # 0 = not set yet
    # ПЕРЕВІРЕНО A/B на plane5 (494 тис. комірок, 10 шарів):
    #   featureAngle 60,  medialRatio 0.3, згладжування 1/3 -> покриття 36.7%
    #   featureAngle 130, medialRatio 0.6, згладжування 3/5 -> покриття 36.4%
    # Тобто підкручування цих ручок не рятує: коли шари не ростуть через саму
    # геометрію (44 незвʼязані частини, перетинні оболонки), snappy однаково
    # не має де їх нарощувати. Лишаємо консервативні значення, які дають
    # трохи кращий перекіс (4.25 проти 4.34).
    layer_feature_angle: int = 60
    max_thickness_medial: float = 0.3
    n_smooth_normals: int = 3
    n_smooth_surface_normals: int = 1
    # Further addLayersControls knobs. The layer advice for a wall-resolved
    # model (y+ ≈ 1, T38) changes them instead of the layer count. The
    # defaults give the same dictionary text as before (same mesh key).
    layer_min_medial_angle: int = 90
    layer_n_smooth_thickness: int = 10
    layer_n_relax_iter: int = 5

    snap_feature_iter: int = 10
    explicit_feature_snap: bool = True
    leak_detection: bool = True


@dataclass
class FlowCfg:
    u_inf: float = 30.0
    aoa_deg: float = 0.0          # тангаж, площина x-z
    aos_deg: float = 0.0          # ковзання, площина x-y
    nu: float = phys.AIR_NU_15C
    rho: float = phys.AIR_RHO_15C
    turbulence: str = "kOmegaSST"
    # Wall treatment, see fs_yplus (spalding / wallfn / lowre). Spalding is
    # valid for every model and every y+; old projects have no key -> it.
    wall_treatment: str = "spalding"
    regime: str = "incompressible"
    # Мають значення лише у стисливому режимі. Там саме з них і виходять
    # rho та nu — див. apply_thermo().
    t_inf: float = phys.AIR_T_15C
    p_inf: float = phys.AIR_P_SEA
    # freestream — коректно за будь-якого кута; tunnel — лише для нульових
    # кутів атаки й ковзання (див. _outer).
    farfield_mode: str = "freestream"
    intensity: float = 0.01
    viscosity_ratio: float = 10.0
    a_ref: float = 1.0
    l_ref: float = 1.0
    cofr: tuple[float, float, float] = (0.0, 0.0, 0.0)

    @property
    def compressible(self) -> bool:
        """Чи потрібна термодинаміка: густина змінна, є рівняння енергії.

        Надзвук теж сюди: у нього та сама термодинаміка, ті самі поля T і
        alphat, той самий абсолютний тиск у паскалях. Різниця не в фізиці
        газу, а в тому, ЯК рахуються потоки крізь грані.
        """
        return self.regime in ("compressible", "supersonic")

    @property
    def supersonic(self) -> bool:
        """Чи беремо густинний розвʼязувач зі схемою для ударних хвиль."""
        return self.regime == "supersonic"

    @property
    def les(self) -> bool:
        """Чи вибрана модель розвʼязує вихори, а не моделює їх усі."""
        return self.turbulence in LES_MODELS

    @property
    def sound_speed(self) -> float:
        """У стисливому — з температури, інакше стандартна ISA.

        Інакше вийшло б смішне: людина ставить −50 °C, а число Маха
        рахується так, ніби за бортом плюс пʼятнадцять.
        """
        return (phys.speed_of_sound(self.t_inf) if self.compressible
                else phys.AIR_SOUND_15C)

    @property
    def mach(self) -> float:
        return phys.mach(self.u_inf, self.sound_speed)

    def apply_thermo(self) -> None:
        """Перерахувати rho й nu з тиску й температури.

        У стисливому режимі вони НЕ є незалежними величинами: OpenFOAM
        отримує досконалий газ і Сазерленда, і рахує їх сам. Якби програма
        далі користувалася старими rho й nu, вона оцінювала б Re, y+ і шари
        не для тієї течії, яку насправді запустить.

        Викликати після кожної зміни t_inf, p_inf або самого режиму.
        """
        if not self.compressible:
            return
        self.rho = phys.density_ideal_gas(self.p_inf, self.t_inf)
        self.nu = phys.viscosity_sutherland(self.t_inf) / self.rho

    @property
    def direction(self) -> tuple[float, float, float]:
        a = math.radians(self.aoa_deg)
        b = math.radians(self.aos_deg)
        return (math.cos(a) * math.cos(b), math.sin(b), math.sin(a) * math.cos(b))

    @property
    def velocity(self) -> tuple[float, float, float]:
        d = self.direction
        return (self.u_inf * d[0], self.u_inf * d[1], self.u_inf * d[2])

    @property
    def drag_dir(self) -> tuple[float, float, float]:
        return self.direction

    @property
    def lift_dir(self) -> tuple[float, float, float]:
        a = math.radians(self.aoa_deg)
        return (-math.sin(a), 0.0, math.cos(a))


# Режими адаптації сітки. Різниця між ними не в якості, а в тому, КОЛИ
# сітка міняється — і це тягне за собою розвʼязувач.
ADAPT_MODES = ("none", "rebuild", "native")


@dataclass
class AdaptCfg:
    """Адаптація сітки: згущення там, де воно потрібне за розвʼязком.

    Два способи, і вибір між ними — це вибір ціни, а не якості.

    `rebuild` — наш. Дешевий прогін на грубій сітці, з нього карта «де
    потік працює», і сітка будується наново з цією картою. Сітка при
    цьому ЗВИЧАЙНА, тому працює зі стаціонарним розвʼязувачем і не
    боїться призматичних шарів. Платня — два проходи замість одного.

    `native` — рідний OpenFOAM, `dynamicRefineFvMesh`. Комірки діляться
    ПІД ЧАС прогону за полем-показником. Точніше, бо показник рахується
    на поточному розвʼязку, а не на грубому. Але:

      це ДИНАМІЧНА сітка, тобто потрібен розвʼязувач, що вміє її міняти —
      simpleFoam не вміє, тож прогін стає нестаціонарним і дорожчає;

      ділить через hexRef8, а той вимагає комірку, придатну до поділу
      навпіл по кожній осі.

    Показник — не сам градієнт, а `|grad(p)|` помножений на РОЗМІР
    КОМІРКИ, плюс завихреність. Обидва доданки нормуються й беруться за
    максимумом.

    Множення на розмір — загальноприйнята практика: воно перетворює
    крутизну поля на оцінку похибки НА КОМІРЦІ, бо дрібна комірка має малу
    похибку навіть при великому градієнті. На нашій грубій сітці дешевого
    проходу воно, втім, не змінило нічого вимірного: частка відібраних
    комірок біля стінки вийшла 8.2% і з ним, і без нього. Ймовірно, воно
    важить там, де розміри комірок різняться на порядки — з призматичними
    шарами.

    А от завихреність змінює вибір різко й перевірено. Зона за самим
    градієнтом тиску тягнулася по потоку від -0.41 до +0.40 м, тобто
    облягала тіло й далі не бачила нічого. З завихреністю — від -2.21 до
    +6.80, тобто разом зі слідом. У сліді тиск рівний, і градієнт тиску
    там сліпий, а слід вирішує опір.

    І окремо про те, чого показник НЕ спричиняє. Провал першої адаптації
    (-43% підйомної сили) стався не через вибір комірок, а через те, що
    зона піднімала рівень згущення САМОЇ ПОВЕРХНІ плямами: y+ уздовж
    хорди стрибав усемеро. Це ловить guard_refine_zones, і до показника
    стосунку не має.
    """
    mode: str = "none"
    # Скільки разів поспіль можна поділити ту саму комірку.
    max_level: int = 2
    # Частка найгірших комірок, що йде в згущення. Це саме ЧАСТКА КОМІРОК,
    # а не поріг на нормованому показнику: поріг тут нічого не обіцяє.
    # Виміряно — на 0.1 від максимуму опинилося 36% усіх комірок, тобто
    # людина просила десяту частину, а діставала третину. Поріг тепер
    # рахує сам показник, гістограмою, і підганяє поле так, щоб різ ліг
    # рівно на 0.5 — саме те число стоїть у словнику сталою.
    fraction: float = 0.05
    # Як часто перевіряти, що ділити (лише native, у кроках).
    interval: int = 20
    # Стеля кількості комірок — щоб адаптація не зʼїла памʼять.
    max_cells: int = 2_000_000
    # Чи брати завихреність на додачу до градієнта тиску.
    use_curl: bool = True
    # ПРИСТІННИЙ ПОЯС, у якому адаптація не чіпає нічого, у розмірах
    # поверхневої комірки (понад товщину призматичних шарів).
    #
    # Це не обережність, а обхід твердого обмеження. hexRef8 ділить лише
    # шестигранники, а комірки, пригнані до поверхні й обкладені шарами,
    # ними не є. OpenFOAM їх позначає («Detected 3109 cells that are
    # protected from refinement») і з відбору викидає — але одразу після
    # відбору викликає consistentRefinement(..., true), яка ДОПИСУЄ
    # сусідів заради правила 2:1 і про захист уже не питає. Досить
    # відібрати комірку поруч зі стінкою — і каскад втягує непридатну:
    #
    #     cell 12604 of level 5 uses more than 8 points of equal or
    #     lower level
    #     hexRef8.C, рядок 3763 — і прогін падає
    #
    # ЧИСЛО ПОМІРЯНЕ, А НЕ ОБРАНЕ. Спершу тут стояло шість — із
    # міркування «один прошарок непридатних комірок, плюс два на
    # nBufferLayers, плюс каскад, плюс запас». Міркування виявилося
    # завеликим ушестеро.
    #
    # Сходинка прогонів, кожен повної довжини:
    #
    #     0 комірок (сам стек шарів)   ВПАВ на t=0.0025
    #                                  cell 15461 of level 5 uses more
    #                                  than 8 points of equal or lower
    #     1 комірка                    дорахував
    #     2 комірки                    дорахував
    #     6 комірок                    дорахував
    #
    # Тобто пояс справді потрібен — без нього прогін гине, — а межа
    # лежить між нулем і одиницею. Ставимо дві: найменше значення, під
    # яким є ще одне ПЕРЕВІРЕНЕ, а не просто менше.
    #
    # Ціна поясу вимірюється часткою заглушених комірок, і вона не
    # дрібна: 36% при одній комірці, 45% при двох, 56% при шести.
    #
    # Другий доказ на користь поясу — фізичний, і його здобуто раніше:
    # згущення пристінних комірок ламає пограншар. Стрибок y+ усемеро,
    # підйомна сила на 43% менша. Тобто ділити там не тільки НЕ МОЖНА, а
    # й НЕ ТРЕБА.
    wall_guard: float = 2.0

    @property
    def on(self) -> bool:
        return self.mode in ("rebuild", "native")

    @property
    def native(self) -> bool:
        return self.mode == "native"


@dataclass
class MotionCfg:
    """Задані коливання тіла по тангажу.

    Кут тут В ГРАДУСАХ, а частота в герцах — бо саме так їх називають у
    задачі. OpenFOAM хоче радіани й кругову частоту, і переводить це
    `angular_*`: тримати в конфігу те, що людина не вміє назвати вголос,
    означає плодити помилки на рівному місці.
    """
    on: bool = False
    # Вісь повороту. Для тангажу це розмах, тобто Y: крило підіймає ніс,
    # обертаючись навколо осі, що йде від законцівки до законцівки.
    axis: tuple[float, float, float] = (0.0, 1.0, 0.0)
    # Точка, навколо якої повертається. Для крила це зазвичай чверть хорди
    # від носка — там лежить аеродинамічний фокус, і поворот навколо нього
    # найменше змішує тангаж із вертикальним рухом.
    origin: tuple[float, float, float] = (0.0, 0.0, 0.0)
    amplitude_deg: float = 5.0
    frequency_hz: float = 2.0
    angle0_deg: float = 0.0
    # Наскільки далеко від тіла рух ще розноситься. Ближче за inner вузли
    # їдуть майже як тіло, далі за outer майже стоять.
    inner: float = 0.0        # нуль — порахувати з габариту
    outer: float = 0.0

    @property
    def amplitude_rad(self) -> float:
        return math.radians(self.amplitude_deg)

    @property
    def angle0_rad(self) -> float:
        return math.radians(self.angle0_deg)

    @property
    def omega(self) -> float:
        """Кругова частота, рад/с. OpenFOAM хоче саме її."""
        return 2.0 * math.pi * self.frequency_hz

    @property
    def period(self) -> float:
        return 1.0 / self.frequency_hz if self.frequency_hz > 0 else 0.0


@dataclass
class SolverCfg:
    n_iterations: int = 1500
    write_interval: int = 250
    n_procs: int = 6
    potential_init: bool = True
    residual_tol: float = 1e-4
    relaxation_u: float = 0.9
    relaxation_p: float = 0.9      # використовується лише коли consistent=no
    consistent: bool = True        # SIMPLEC
    write_format: str = "binary"
    decompose_method: str = "scotch"
    run_check_mesh: bool = True
    # Зупинятися, якщо checkMesh знайшов зауваження. Дешевше впертися тут,
    # ніж через десять хвилин отримати FPE в розвʼязувачі.
    stop_on_bad_mesh: bool = True
    # Стійкий старт: схеми першого порядку, класичний SIMPLE із низькою
    # релаксацією, корекція неортогональності й стеля на швидкість.
    # robust_start=True — так рахується ВЕСЬ прогін.
    robust_start: bool = False
    velocity_cap_factor: float = 1.5
    # Двоетапний запуск: стільки перших ітерацій рахуються стійко, далі
    # розрахунок ПРОДОВЖУЄТЬСЯ з отриманого поля звичайними схемами
    # (startFrom latestTime). Нуль вимикає другий етап.
    robust_iterations: int = 300

    # ---------------- нестаціонар ----------------
    # steady=True — усе вище працює як і працювало. Нестаціонар — окрема
    # гілка, і поля нижче поза нею не мають жодного значення.
    steady: bool = True
    # Типові числа підібрані під типову ж геометрію (тіло 1 м, 30 м/с):
    # проліт домену там 0.6 с, тож 2 с — це понад три прольоти, а 0.05 с між
    # зрізами дають сорок кадрів. Для справжньої геометрії їх перераховує
    # помічник, бо в секундах вони залежать від розміру тіла й швидкості.
    end_time: float = 2.0          # с
    time_step: float = 1e-4        # ПОЧАТКОВИЙ крок; далі його веде Куранта
    max_courant: float = 5.0
    write_every: float = 0.05      # с між часовими зрізами
    # Скільки разів за крок перераховується вся система. 1 — це PISO, швидко
    # й вимагає малого Куранта; 2-3 дозволяють Куранта більше за одиницю.
    n_outer_correctors: int = 2
    n_correctors: int = 2
    # Теплий старт стисливого нестаціонару: стільки ітерацій rhoSimpleFoam
    # перед rhoPimpleFoam, і його поле стає моментом 0. Нуль вимикає.
    # Див. CaseCfg.warm_start.
    warm_iterations: int = 300
    # Розподіл між машинами (foamdist): вага кожного процесу за його
    # швидкістю, у порядку процесів. Порожньо — шматки однакові. Ставить
    # вікно перед прогоном з `foamdist weights`; діє, лише коли їх рівно
    # n_procs (decompose_par_dict).
    proc_weights: list = field(default_factory=list)

    @property
    def two_stage(self) -> bool:
        # Для нестаціонару двоетапність безглузда: там запобіжником від
        # зриву служить малий крок, а не схеми першого порядку. Та й
        # «продовжити з latestTime» у нестаціонарі означало б інше.
        return (self.steady
                and not self.robust_start
                and 0 < self.robust_iterations < self.n_iterations)

    @property
    def n_time_steps(self) -> float:
        """Скільки кроків вийде за прогін — за ПОЧАТКОВИМ кроком.

        Оцінка згори: adjustTimeStep зазвичай збільшує крок, коли поле
        вирівнюється, тож справжня кількість буде меншою. Але саме за цим
        числом видно замовлення на два мільйони кроків.
        """
        return self.end_time / max(self.time_step, 1e-12)

    @property
    def n_write_dirs(self) -> float:
        return self.end_time / max(self.write_every, 1e-12)


@dataclass
class CaseCfg:
    name: str = "case"
    patch: str = "body"
    stl_file: str = "body.stl"
    bbox_min: tuple[float, float, float] = (0.0, 0.0, 0.0)
    bbox_max: tuple[float, float, float] = (1.0, 1.0, 1.0)
    surface_area: float = 1.0
    inside_point: tuple[float, float, float] | None = None
    # Роздільні патчі. `bodies` — самі частини; порожній список означає «усе
    # тіло — один патч», тобто рівно те, що було до їхньої появи: `patch` і
    # `stl_file` вище. `split_patches` — намір користувача, з якого програма
    # перезбирає `bodies` щоразу, коли міняється сцена.
    split_patches: bool = False
    bodies: list[BodyPatch] = field(default_factory=list)
    # Пористі зони: радіатори, сітки, фільтри. Порожній список означає, що
    # ні topoSet, ні пористих джерел у кейсі немає зовсім.
    porous: list[PorousZone] = field(default_factory=list)
    motion: MotionCfg = field(default_factory=MotionCfg)
    adapt: AdaptCfg = field(default_factory=lambda: AdaptCfg())
    # Обертові зони (MRF): гвинти, ротори, вентилятори.
    rotating: list[RotatingZone] = field(default_factory=list)
    # Двигуни: пари кришок intake/nozzle. Порожній список — словники ті
    # самі, що й до появи двигунів.
    engines: list[Engine] = field(default_factory=list)
    # Гвинти-диски: тяга й закрутка без лопатей. Порожній список — словники
    # ті самі, що й до їхньої появи.
    disks: list[ActuatorDisk] = field(default_factory=list)
    domain: DomainCfg = field(default_factory=DomainCfg)
    mesh: MeshCfg = field(default_factory=MeshCfg)
    flow: FlowCfg = field(default_factory=FlowCfg)
    solver: SolverCfg = field(default_factory=SolverCfg)

    # ---------------- похідні величини ----------------

    @property
    def body_length(self) -> float:
        return max(self.bbox_max[0] - self.bbox_min[0], 1e-9)

    @property
    def body_span(self) -> float:
        return max(self.bbox_max[1] - self.bbox_min[1], 1e-9)

    @property
    def body_height(self) -> float:
        return max(self.bbox_max[2] - self.bbox_min[2], 1e-9)

    @property
    def centre(self) -> tuple[float, float, float]:
        return tuple((a + b) / 2.0 for a, b in zip(self.bbox_min, self.bbox_max))

    @property
    def bounds(self) -> tuple[float, float, float, float, float, float]:
        """Межі розрахункової області: (xmin, xmax, ymin, ymax, zmin, zmax).

        Відступи задані в довжинах тіла ВЗДОВЖ ПОТОКУ, але сама межа рахується
        від габариту в кожному напрямку окремо. Інакше тіло з великим
        подовженням — крило, у якого розмах уп'ятеро більший за хорду —
        просто вилізло б за бічні межі домену: 5 хорд від центра менше за
        половину розмаху, і snappy різав би сітку прямо по консолях.
        """
        L = self.body_length
        d = self.domain
        xmin = self.bbox_min[0] - d.upstream * L
        xmax = self.bbox_max[0] + d.downstream * L
        ymin = self.bbox_min[1] - d.side * L
        ymax = self.bbox_max[1] + d.side * L
        zmax = self.bbox_max[2] + d.above * L
        if d.ground:
            zmin = d.ground_offset
        else:
            zmin = self.bbox_min[2] - d.below * L
        return self._squared_for_rotors((xmin, xmax, ymin, ymax, zmin, zmax))

    def _squared_for_rotors(self, bounds: tuple[float, float, float, float,
                                                float, float],
                            ) -> tuple[float, float, float, float, float,
                                      float]:
        """Домен впоперек осі кожного гвинта — квадратний, не перекошений.

        Бічні відступи рахуються від довжини тіла ВЗДОВЖ ПОТОКУ
        (`body_length`), а гвинт — тонкий диск: для нього це геть інший
        масштаб, ніж для розмаху крила чи висоти фюзеляжу. Впоперек осі
        обертання домен через це виходить перекошеним — з одного боку
        затісно. Беремо БІЛЬШИЙ з двох відступів і застосовуємо до
        обох: домен тільки росте, ніколи не звужується.

        З підлогою (`ground`) пари, що включають Z, не чіпаємо: там
        асиметрія навмисна, і «квадратити» її означало б підняти підлогу
        чи стелю довільно.
        """
        xmin, xmax, ymin, ymax, zmin, zmax = bounds
        for z in self.rotating:
            ax, ay, az = (round(v) for v in _unit(z.axis))
            if az:
                xmin, xmax, ymin, ymax = _square_pair(xmin, xmax, ymin, ymax)
            elif ay and not self.domain.ground:
                xmin, xmax, zmin, zmax = _square_pair(xmin, xmax, zmin, zmax)
            elif ax and not self.domain.ground:
                ymin, ymax, zmin, zmax = _square_pair(ymin, ymax, zmin, zmax)
        return (xmin, xmax, ymin, ymax, zmin, zmax)

    @property
    def domain_size(self) -> tuple[float, float, float]:
        x0, x1, y0, y1, z0, z1 = self.bounds
        return (x1 - x0, y1 - y0, z1 - z0)

    @property
    def domain_volume(self) -> float:
        sx, sy, sz = self.domain_size
        return sx * sy * sz

    @property
    def base_cell(self) -> float:
        """Розмір фонової комірки. Тримаємо кубічною — так snappy працює краще."""
        sx = self.domain_size[0]
        return sx / max(self.domain.base_cells_along_x, 1)

    @property
    def block_divisions(self) -> tuple[int, int, int]:
        h = self.base_cell
        sx, sy, sz = self.domain_size
        return (
            max(1, int(round(sx / h))),
            max(1, int(round(sy / h))),
            max(1, int(round(sz / h))),
        )

    @property
    def surface_cell(self) -> float:
        return self.base_cell / 2 ** self.mesh.surface_level_max

    @property
    def adapt_wall_guard(self) -> float:
        """Товщина пристінного поясу, де адаптація не чіпає нічого, м.

        Складається з двох частин, і кожна потрібна окремо:

            стек призматичних шарів   комірки в ньому взагалі не
                                      шестигранники, hexRef8 їх не подужає;
            кілька поверхневих комірок  пригнані до поверхні комірки теж
                                      не шестигранники, а понад те —
                                      потрібен запас під nBufferLayers і
                                      під каскад 2:1, який дописує сусідів
                                      УЖЕ ПІСЛЯ перевірки на захист.

        Саме друга частина й пояснює, чому одного шару мало. Падіння
        сталося не на комірці, яку відібрав показник, а на тій, яку
        каскад дописав до неї.
        """
        return (self.layer_stack.total
                + max(float(self.adapt.wall_guard), 0.0) * self.surface_cell)

    def finest_level(self) -> int:
        """Найдрібніший рівень комірки, який snappyHexMesh може зробити.

        Потрібно це рівно в одному місці — рідній адаптації, — але саме там
        помилитися найдорожче, і помилка виходить мовчазна.

        `maxRefinement` у `dynamicRefineFvMesh` — це АБСОЛЮТНА стеля рівня,
        а не «скільки разів іще поділити». У коді умова відбору така:

            cellLevel[celli] < maxRefinement

        Тобто комірка, яка вже на рівні 5, при `maxRefinement 2` не
        проходить взагалі. А біля тіла в нас саме такі. Показник адаптації
        вказує туди ж, де сітка вже найдрібніша, — і відбір дає нуль
        комірок, повідомляючи про це рядком «Selected 0 cells for
        refinement», який легко прочитати як «нема чого ділити».

        Рівень береться не з одного місця, і взяти лише поверхневий — та
        сама помилка, тільки менша. Джерел пʼять:

            поверхня       surface_level_max
            кромки         feature_level
            коробки        boxes[].level
            оболонка       верхня сходинка shell_ladder
            зони           верхні сходинки refine_only_levels

        На підсумковій сітці з кожухом верх дає саме оболонка, а не
        поверхня: кожух стоїть на рівень вище.
        """
        top = max(int(self.mesh.surface_level_max), int(self.mesh.feature_level))
        for box in self.mesh.boxes:
            top = max(top, int(box.level))
        shell = self.shell_ladder()
        if shell:
            top = max(top, max(lvl for _, lvl in shell))
        for _name, ladder in self.refine_only_levels():
            if ladder:
                top = max(top, max(lvl for _, lvl in ladder))
        return max(top, 0)

    @property
    def layer_stack(self) -> phys.LayerStack:
        return phys.LayerStack(
            n_layers=self.mesh.n_layers if self.mesh.add_layers else 0,
            expansion=self.mesh.expansion,
            final_rel=self.mesh.final_layer_rel,
            surface_cell=self.surface_cell,
        )

    def point_clearance(self, point) -> float:
        """Наскільки точка годиться на «точку в потоці»: 0 — не годиться.

        Число — це менша з двох відстаней, у частках розміру домену: до
        найближчої межі домену й за габарит тіла. Нуль означає, що точка
        або стоїть на межі, або сидить усередині габариту — і тоді snappy
        заллє нутрощі тіла замість повітря навколо.
        """
        sx, sy, sz = self.domain_size
        if min(sx, sy, sz) <= 0:
            return 0.0
        x0, x1, y0, y1, z0, z1 = self.bounds
        px, py, pz = point
        wall = min((px - x0) / sx, (x1 - px) / sx,
                   (py - y0) / sy, (y1 - py) / sy,
                   (pz - z0) / sz, (z1 - pz) / sz)
        if wall <= 0:
            return 0.0
        body = max((self.bbox_min[0] - px) / sx, (px - self.bbox_max[0]) / sx,
                   (self.bbox_min[1] - py) / sy, (py - self.bbox_max[1]) / sy,
                   (self.bbox_min[2] - pz) / sz, (pz - self.bbox_max[2]) / sz)
        return min(wall, body) if body > 0 else 0.0

    @property
    def location_in_mesh(self) -> tuple[float, float, float]:
        """Точка в потоці — та, від якої snappy заливає повітря.

        Типово це місце між входом і носом тіла, зміщене вбік від площини
        симетрії, щоб не сісти рівно на грань комірки.

        АЛЕ на тісному домені такої точки там може не бути: при відступі
        0.5 довжини й довгому носі зазор перед тілом стає вужчим за комірку,
        а при відступі 0 (таке приїжджає з імпортованих налаштувань, бо
        поля вікна такого не дають) точка сідає рівно на межу домену й
        усередину габариту тіла. Ціна помилки тут максимальна: snappy
        заливає НУТРОЩІ тіла, і замість обтікання виходить розрахунок
        повітря в баку.

        Тому кандидатів кілька, і вибирається той, що найдалі і від межі
        домену, і від габариту тіла. Перший у списку — історичний, тому на
        нормальному домені результат не змінюється.
        """
        if self.inside_point is not None:
            return self.inside_point
        x0, x1, y0, y1, z0, z1 = self.bounds
        bx0, by0, bz0 = self.bbox_min
        bx1, by1, bz1 = self.bbox_max
        cx, cy, cz = self.centre
        candidates = (
            (x0 + 0.37 * (bx0 - x0), cy + 0.131 * (y1 - cy),
             cz + 0.117 * (z1 - cz)),
            ((x0 + bx0) / 2.0, cy, cz),                 # перед носом
            (cx, cy, (bz1 + z1) / 2.0),                 # над тілом
            (cx, (by1 + y1) / 2.0, cz),                 # збоку
            (cx, cy, (bz0 + z0) / 2.0),                 # під тілом
            ((bx1 + x1) / 2.0, cy, cz),                 # за хвостом
        )
        # Перший кандидат — історичний, і поки він годиться, беремо саме
        # його: він видний у вʼюпорті зеленою кулькою, описаний у настанові
        # й звичний. Двох відсотків розміру домену як запасу досить —
        # це кілька фонових комірок навіть на грубій сітці.
        first = self.point_clearance(candidates[0])
        if first >= 0.02:
            return candidates[0]
        best, best_score = candidates[0], first
        for point in candidates[1:]:
            score = self.point_clearance(point)
            if score > best_score:
                best, best_score = point, score
        return best

    @property
    def location_outside_mesh(self) -> tuple[float, float, float]:
        """Точка свідомо поза доменом — для детекції витоків snappy."""
        x0, x1, y0, y1, z0, z1 = self.bounds
        sx, sy, sz = self.domain_size
        return (x0 - 0.13 * sx, y0 - 0.17 * sy, z0 - 0.19 * sz)

    def estimate(self) -> phys.MeshEstimate:
        boxes = [(b.volume, b.level) for b in self.mesh.boxes]
        # Оболонки на відстані рахуються ТАК САМО, як коробки, бо для
        # snappyHexMesh це той самий refinementRegion — просто заданий
        # відстанню, а не координатами.
        #
        # Раніше їх не було в оцінці взагалі, і це не дрібниця: кожух
        # завтовшки 40 мм навколо цього літака дає більше комірок, ніж уся
        # решта сітки разом, а прогноз показував те саме число, що й без
        # нього. Людина бачила «251 тисяча» й ставила задачу, яка не
        # влізала в памʼять.
        boxes += self._shell_regions()
        est = phys.estimate_mesh(
            domain_volume=self.domain_volume,
            base_cell=self.base_cell,
            surface_area=self.surface_area - self._own_level_area(),
            surface_level=self.mesh.surface_level_max,
            refinement_regions=boxes,
            n_layers=self.mesh.n_layers if self.mesh.add_layers else 0,
            cells_between_levels=self.mesh.n_cells_between_levels,
        )
        # Частини з ВЛАСНИМ рівнем доводяться окремо, кожна за своїм.
        #
        # Інакше оцінка рахувала б їх за спільним рівнем і мовчки
        # брехала б у той бік, у який брехати найдорожче: чотири лопаті
        # на рівні 7 замість 5 — це не поправка, це множник 16 на їхню
        # частку. Людина побачила б затишне число, поставила задачу й
        # дізналася правду тоді, коли snappyHexMesh забракне памʼяті.
        for b in self.surface_patches:
            own = self.level_of(b)
            if not own or b.area <= 0:
                continue
            part = phys.estimate_mesh(
                domain_volume=0.0,
                base_cell=self.base_cell,
                surface_area=b.area,
                surface_level=own[1],
                refinement_regions=[],
                n_layers=self.mesh.n_layers if self.mesh.add_layers else 0,
                cells_between_levels=self.mesh.n_cells_between_levels,
            )
            est = phys.MeshEstimate(
                background=est.background, regions=est.regions,
                surface=est.surface + part.surface,
                layers=est.layers + part.layers)
        return est

    def _own_level_area(self) -> float:
        """Площа частин, які згущуються за ВЛАСНИМ рівнем, м².

        Її віднімають зі спільного доданка, щоб ті самі лопаті не
        порахувалися двічі — один раз за спільним рівнем, другий за своїм.
        Обмежено зверху всією площею: якщо площі частин чомусь більші за
        спільну (кейс із маніфесту, де їх міряли інакше), результат має
        лишитися невідʼємним, а не поїхати в мінус.
        """
        own = sum(b.area for b in self.surface_patches
                  if self.level_of(b) and b.area > 0)
        return min(own, max(self.surface_area, 0.0))

    def _shell_regions(self) -> list[tuple[float, int]]:
        """Обʼєми сходинок оболонки: [(обʼєм у м³, рівень), ...].

        Обʼєм шару завтовшки d навколо тіла — це приблизно площа поверхні
        на d. Наближення тим точніше, чим шар тонший за саме тіло; для
        товстих шарів воно занижує, бо не враховує, що зовнішня поверхня
        шару більша за внутрішню.

        Сходинки ВКЛАДЕНІ одна в одну, тому кожна рахується лише за свій
        приріст: інакше найтонша порахувалася б тричі.

        Окремо віднімається те, що вже враховано в поверхневому доданку:
        snappyHexMesh і без жодної оболонки тримає навколо тіла смугу
        завтовшки `n_cells_between_levels` комірок найдрібнішого рівня.
        Без цього віднімання перші міліметри кожуха рахувалися б двічі.
        """
        ladder = self.shell_ladder()
        if not ladder or self.surface_area <= 0:
            return []
        h_surf = self.surface_cell
        natural = self.mesh.n_cells_between_levels * h_surf
        out: list[tuple[float, int]] = []
        prev = 0.0
        for dist, level in ladder:
            lo = prev
            if level >= self.mesh.surface_level_max:
                lo = max(lo, natural)
            vol = self.surface_area * max(dist - lo, 0.0)
            if vol > 0:
                out.append((vol, level))
            prev = max(dist, prev)
        return out

    @property
    def flow_through_time(self) -> float:
        """Скільки секунд потік летить крізь домен від входу до виходу.

        Природна одиниця часу для нестаціонару: до тіла потік має спершу
        долетіти, а слід — встигнути сформуватися.
        """
        return self.domain_size[0] / max(self.flow.u_inf, 1e-9)

    @property
    def motion_pivot(self) -> tuple[float, float, float]:
        """Точка повороту. Нулі означають «постав розумно сам».

        Типово — чверть ДОВЖИНИ ТІЛА від носа. Для цілого літака це
        приблизно там, де зазвичай лежить центр мас, тож поворот навколо
        неї найменше домішує до тангажу паразитний вертикальний рух.

        Це саме здогад за габаритом, а не розрахунок: справжній центр мас
        програма не знає й знати не може. Для окремого крила правильніше
        чверть ХОРДИ від носка — там аеродинамічний фокус, — і тоді точку
        треба задати руками.
        """
        o = self.motion.origin
        if any(abs(v) > 1e-12 for v in o):
            return o
        cx = self.bbox_min[0] + 0.25 * self.body_length
        cy, cz = self.centre[1], self.centre[2]
        return (cx, cy, cz)

    def guard_motion(self) -> list[tuple[str, str]]:
        """Що не так із рухомою сіткою."""
        out: list[tuple[str, str]] = []
        m = self.motion
        if not m.on:
            return out
        if self.solver.steady:
            out.append(("bad", (
                tr("A moving mesh runs only in time: the body moves, and by "
                "definition this problem has no steady state. Clear the "
                "“Steady” check box on the “Solver” tab."))))
        if m.frequency_hz <= 0:
            out.append(("bad", tr("The oscillation frequency must be greater than zero.")))
        if abs(m.amplitude_deg) > 45:
            out.append(("bad", (
                tr("Amplitude {amplitude_deg:g}° is too large. At such "
                   "an angle the nodes near the body fold over each "
                   "other, and the mesh becomes invalid before anything "
                   "is calculated.", amplitude_deg=m.amplitude_deg))))
        if all(abs(v) < 1e-12 for v in m.axis):
            out.append(("bad", tr("The rotation axis is zero.")))
        # НАЙВАЖЛИВІШЕ ТУТ. Стінка за один крок не має пройти більше, ніж
        # товщина комірки біля неї. Інакше вузли перекидаються через сусідні,
        # комірка вивертається — і розрахунок гине не поступово, а миттєво.
        #
        # У перевірці це виглядало так: 20 Гц, 8°, крок 2e-4 с. Хвіст за крок
        # проходив 1.2 мм при першому призматичному шарі близько 0.5 мм, і
        # на ПЕРШОМУ ж кроці k вилетіло до 1e+73. Ні попередження, ні
        # зрозумілої помилки — просто арифметична пастка всередині рівняння
        # турбулентності через двадцять секунд.
        if not self.solver.steady and self.solver.time_step > 0:
            # Найдальша точка тіла від осі повороту — там швидкість стінки
            # найбільша.
            px, py, pz = self.motion_pivot
            reach = max(
                max(abs(self.bbox_min[0] - px), abs(self.bbox_max[0] - px)),
                max(abs(self.bbox_min[2] - pz), abs(self.bbox_max[2] - pz)),
            )
            speed = abs(m.amplitude_rad) * m.omega * reach
            travel = speed * self.solver.time_step
            stack = self.layer_stack
            near = stack.first_abs if stack.n_layers else self.surface_cell
            # Пороги ПОМІРЯНІ, а не вгадані, і виявилися жорсткішими, ніж
            # здається на перший погляд. Перша спроба ставила межу на цілій
            # товщині комірки — мовляв, поки вузол не перестрибнув сусіда,
            # усе гаразд. Насправді прогін, у якому стінка проходила 84%
            # першого шару (2.11 мм при шарі 2.51), загинув на першому ж
            # кроці. Комірка вивертається не тоді, коли вузол перестрибнув,
            # а набагато раніше: шар стискається, скіс росте, і рівняння
            # турбулентності отримує вироджений обʼєм.
            if near > 0 and travel > near * 0.25:
                out.append(("bad", (
                    tr("In one step the wall moves {travel} with the "
                       "cell next to it {near} — that is {v:.0f}% of its "
                       "thickness. The cell will turn inside out, and "
                       "the run will die on the very first step, not "
                       "gradually but instantly: in a test at 84% k blew "
                       "up to 1e+73 in one step. Reduce the time step, "
                       "frequency or amplitude; or remove the prism "
                       "layers if friction does not matter here — "
                       "without them the cell at the wall is three times "
                       "thicker.", travel=phys.fmt_len(travel), near=phys.fmt_len(near), v=travel / near * 100))))
            elif near > 0 and travel > near * 0.1:
                out.append(("warn", (
                    tr("In one step the wall moves {travel} with a cell "
                       "of {near} — {v:.0f}% of its thickness. It will "
                       "run, but the cell quality at the wall will drop, "
                       "and the friction with it.", travel=phys.fmt_len(travel), near=phys.fmt_len(near), v=travel / near * 100))))
        # Найтихіше з усього: крок за часом, надто великий для коливання.
        # Прогін піде, нічого не впаде, а коливання просто не буде видно —
        # солвер перестрибне через нього.
        if not self.solver.steady and m.period > 0 and self.solver.time_step > 0:
            per_period = m.period / self.solver.time_step
            if per_period < 100:
                out.append(("warn", (
                    tr("One oscillation period gets {per_period:.0f} "
                       "time steps. That is too few: the oscillation "
                       "shape will be distorted, and the force amplitude "
                       "will come out too low. You need at least a "
                       "hundred, better several hundred. Reduce the step "
                       "or the frequency.", per_period=per_period))))
        if not self.solver.steady and m.period > 0 and self.solver.end_time > 0:
            periods = self.solver.end_time / m.period
            if periods < 3:
                out.append(("warn", (
                    tr("The run covers {periods:.1f} oscillation "
                       "periods. The first period always goes to the "
                       "transient from the standing start, so you can "
                       "take nothing from it. Use at least three or four.", periods=periods))))
        return out

    def guard_regime(self) -> list[tuple[str, str]]:
        """Що не так із надзвуковим режимом.

        Дві речі, і обидві ловляться тут, а не в журналі OpenFOAM через
        двадцять хвилин прогону.
        """
        out: list[tuple[str, str]] = []
        if not self.flow.supersonic:
            return out
        if self.solver.steady:
            out.append(("bad", (
                tr("Supersonic mode runs only in time. The scheme is "
                "explicit, the time step is limited by the Courant "
                "number, and the “steady state” here builds up through "
                "time integration, not through iterations. Clear the "
                "“Steady” check box on the “Solver” tab."))))
        if self.solver.max_courant > MAX_COURANT_CENTRAL:
            out.append(("bad", (
                tr("Courant number {max_courant:g} is too large for an "
                   "explicit scheme. PIMPLE can hold even five, because "
                   "it does several outer iterations per step; "
                   "rhoCentralFoam cannot do this — the new value is "
                   "calculated from what is already there, and a Courant "
                   "number larger than one means that a disturbance "
                   "jumps over a cell in one step. Set "
                   "{MAX_COURANT_CENTRAL:g} or less.", max_courant=self.solver.max_courant, MAX_COURANT_CENTRAL=MAX_COURANT_CENTRAL))))
        m = self.flow.mach
        if 0 < m < 0.8:
            out.append(("warn", (
                tr("Mach number {m:.2f} is not supersonic yet. "
                   "rhoCentralFoam will work here, but its time step is "
                   "orders of magnitude smaller than with PIMPLE, so the "
                   "same calculation will take much longer. Up to M = "
                   "0.8 the normal compressible mode is better.", m=m))))
        # Часу має вистачити, щоб потік ЗАПОВНИВ домен. На надзвуку
        # початкове поле нерухоме (див. field_U): інакше база за тілом
        # спорожняється за мікросекунди й розрахунок гине. Платою за це є
        # пускова хвиля, яка мусить пройти домен наскрізь, і поки вона не
        # вийшла, коефіцієнти показують не задачу, а її.
        u = float(getattr(self.flow, "u_inf", 0.0) or 0.0)
        if u > 0 and not self.solver.steady:
            length = float(self.domain_size[0])
            fill = length / u
            if self.solver.end_time < fill * 1.5:
                out.append(("warn", (
                    tr("The end time {end_time:.3g} s is too small: for "
                       "the flow to cross the domain ({length:.2f} m) at "
                       "{u:.0f} m/s, it takes {fill:.3g} s, and settling "
                       "needs at least one and a half times that. A "
                       "supersonic run starts from a STILL field, so the "
                       "domain fills first, and everything you measure "
                       "before that is the startup wave, not the answer.", end_time=self.solver.end_time, length=length, u=u, fill=fill))))
        if self.flow.farfield_mode != "supersonic":
            out.append(("warn", (
                tr("Here the boundary conditions are set by the mode "
                   "itself, not by the choice “{farfield_mode}”: at the "
                   "inlet everything is fixed, on the sides a zero "
                   "gradient, and at the outlet the pressure is held by "
                   "a NON-REFLECTING boundary. A freestream condition at "
                   "a shock gives a state that cannot exist, and the run "
                   "dies with an arithmetic error; and a plain zero "
                   "gradient on pressure is good only while the outflow "
                   "is supersonic — behind a blunt body the wake is "
                   "subsonic, and then nothing fixes the pressure level "
                   "and it creeps up until the drag becomes negative.", farfield_mode=self.flow.farfield_mode))))
        return out

    def guard_unsteady(self) -> list[tuple[str, str]]:
        """Що не так із налаштуваннями часу: [(рівень, пояснення)].

        Рівень `bad` означає «за це не пускаємо», `warn` — «запитаємо».
        Окремою функцією, а не перевірками у вікні, з двох причин: її можна
        накрити тестами, і той самий висновок потрібен і кнопці «Створити
        кейс», і поради́ ще до натиску.
        """
        if self.solver.steady:
            return []
        s = self.solver
        out: list[tuple[str, str]] = []

        if s.end_time <= 0 or s.time_step <= 0:
            out.append(("bad", tr("Time and time step must be positive.")))
            return out

        steps = s.n_time_steps
        if steps > MAX_TIME_STEPS:
            out.append(("bad",
                        tr("This gives {steps} time steps ({end_time:g} "
                           "s with a step of {time_step:g} s) — more "
                           "than the limit {MAX_TIME_STEPS}. That is "
                           "weeks of computing. Increase the step or "
                           "shorten the time.", steps=fmt_count(steps), end_time=s.end_time, time_step=s.time_step, MAX_TIME_STEPS=fmt_count(MAX_TIME_STEPS))))
        elif steps > MAX_TIME_STEPS / 10:
            out.append(("warn",
                        tr("{steps} time steps — the run will be very long.", steps=fmt_count(steps))))

        # СТИСЛИВИЙ НЕСТАЦІОНАРНИЙ: стеля Куранта — 2, а не довідкові 3–10.
        # Виміряно 2026-09-22 на filled_holes1 (rhoPimpleFoam, 1.4 млн
        # комірок, nOuterCorrectors 2): maxCo 2 рахує годинами, на Co ≈ 4
        # limitTemperature затискає половину сітки, далі sigFpe в рівнянні
        # турбулентності. Той самий почерк — на прогоні користувача
        # 2026-11-08 з maxCo 5.
        if (self.flow.compressible and not self.flow.supersonic
                and s.max_courant > COMPRESSIBLE_MAX_CO):
            out.append(("warn", (
                tr("maxCo {max_courant:g} is too much for compressible "
                   "transient: in our measurements such a case runs "
                   "cleanly at 2 and dies at ~4 — first temperature "
                   "clipping in a growing part of the mesh, then an "
                   "arithmetic error. Set ≤ {COMPRESSIBLE_MAX_CO:g}.", max_courant=s.max_courant, COMPRESSIBLE_MAX_CO=COMPRESSIBLE_MAX_CO))))

        dirs = s.n_write_dirs
        if dirs > MAX_WRITE_DIRS:
            out.append(("bad",
                        tr("There will be {dirs} time directories — more "
                           "than the limit {MAX_WRITE_DIRS}. Each time "
                           "directory is a folder with all the fields; "
                           "this many will fill the disk. Increase the "
                           "write interval.", dirs=fmt_count(dirs), MAX_WRITE_DIRS=fmt_count(MAX_WRITE_DIRS))))
        elif dirs < 2:
            out.append(("warn",
                        tr("The whole run will have fewer than two time "
                        "directories — there will be nothing to look at "
                        "in the result. Decrease the write interval.")))

        if s.max_courant > MAX_COURANT:
            out.append(("bad",
                        tr("Courant {max_courant:g} is too much. Above "
                           "{MAX_COURANT:g}, PIMPLE makes the "
                           "calculation diverge. The typical value is "
                           "3-10.", max_courant=s.max_courant, MAX_COURANT=MAX_COURANT)))
        elif s.max_courant > 1.0 and s.n_outer_correctors < 2:
            out.append(("warn",
                        tr("Courant {max_courant:g} with one outer pass "
                           "is PISO, and PISO needs Courant below one. "
                           "Either reduce Courant or set 2-3 passes.", max_courant=s.max_courant)))

        flight = self.flow_through_time
        if flight > 0 and s.end_time < flight:
            out.append(("bad",
                        tr("End time {end_time:g} s is less than one "
                           "domain flow-through ({flight:.4g} s): the "
                           "flow will not even get from the inlet to the "
                           "outlet. Use at least {v:.3g} s.", end_time=s.end_time, flight=flight, v=flight * 3)))
        elif flight > 0 and s.end_time < flight * 3:
            out.append(("warn",
                        tr("The end time {end_time:g} s is less than "
                           "three flow-throughs of the domain "
                           "({flight:.4g} s each). The wake behind the "
                           "body will not have time to form.", end_time=s.end_time, flight=flight)))
        return out

    @property
    def use_potential_init(self) -> bool:
        """Чи ставити potentialFoam перед розвʼязувачем.

        Лише для стаціонарної нестисливої задачі. Два випадки, коли ні:

        **Стислива.** potentialFoam нестисливий: він пише phi як ОБʼЄМНИЙ
        потік, а rhoSimpleFoam чекає МАСОВОГО. Це не «трохи неточне
        наближення», а інша розмірність.

        **Нестаціонарна.** Тут причина тонша й коштувала дня. potentialFoam
        дає потік ідеальної рідини: крізь стінку не тече, але й не
        прилипає. SIMPLE таку неузгодженість між phi і U розсмоктує за
        кілька ітерацій під релаксацією — йому байдуже, з чого починати.
        PIMPLE так не може: у нього перший же крок мусить задовольнити
        ddt(U), і різниця між потенційним потоком і в'язким дає сплеск
        тиску, за яким adjustTimeStep женеться, зменшуючи крок, доки той не
        схлопнеться. Виміряно на еліпсоїді: без potentialFoam Cd = 0.0941
        (стаціонарний дає 0.0946), з ним — Cd = -1.58 і розліт на 0.012 с.
        """
        return (self.solver.potential_init
                and self.solver.steady
                and not self.flow.compressible)

    @property
    def warm_start(self) -> bool:
        """Чи починати стисливий нестаціонар зі стаціонарного поля.

        ЧОМУ. Без цього rhoPimpleFoam стартує з поля «усе повітря вже летить
        з u∞». Біля стінок і в каналах воно мусить зупинитися за ОДИН крок —
        це гідроудар: на кейсі користувача (канал EDF усередині корпусу,
        150 м/с) за перші кроки тиск у каналі став 275 кПа, густина 5.4,
        на губі входу температура впала до межі 72 К; на 11-му кроці k
        пішло в мінус і пристінна функція Сполдінга впала з FPE. Заміна
        моделі турбулентності й схем лапласіана не змінила НІЧОГО — той
        самий крок, те саме k. Нестисливий так не болить: там тиск миттєво
        підлаштовується, а хвиль стиснення немає.

        rhoSimpleFoam ту саму неузгодженість розсмоктує релаксацією, а не
        хвилями, тож після кількох сотень ітерацій нестаціонар стартує з
        поля, де стінки й канали вже «знають» про потік.

        Не для надзвукового (там rhoCentralFoam, стаціонарного двійника
        немає) і не для рухомої сітки (стаціонарний її не крутить).
        """
        return (not self.solver.steady
                and self.flow.compressible
                and not self.flow.supersonic
                and not self.motion.on
                and int(self.solver.warm_iterations) > 0)

    def turbulence_inlet(self) -> phys.InletTurbulence:
        return phys.inlet_turbulence_from_viscosity_ratio(
            self.flow.u_inf, self.flow.intensity, self.flow.nu,
            self.flow.viscosity_ratio,
        )

    @property
    def surface_patches(self) -> list[BodyPatch]:
        """УСІ поверхні, що йдуть у сітку: стінки й кришки двигунів.

        Без роздільних патчів — одна, з тими самими іменем і файлом, що й
        раніше, тож словники виходять тими самими.

        Гвинти-диски сюди не входять: від їхньої моделі в сітку не йде нічого.
        """
        if not self.bodies:
            return [BodyPatch(self.patch, self.stl_file)]
        return [b for b in self.bodies if b.role != DISK_ROLE]

    @property
    def disks_on(self) -> list["ActuatorDisk"]:
        """Диски, які справді підуть у розрахунок.

        rhoCentralFoam джерел не застосовує (див. `_limit_temperature`), тож
        на надзвуку диски вимикаються, а guard_disks каже про це (Q19).
        """
        return [] if self.flow.supersonic else list(self.disks)

    def disk_level(self, d: "ActuatorDisk") -> int:
        """Рівень згущення в диску: ≥ 20 комірок на діаметр, із стелею."""
        if self.base_cell <= 0 or d.radius <= 0:
            return self.mesh.surface_level_max
        need = math.ceil(math.log2(self.base_cell * DISK_CELLS_ACROSS
                                   / (2.0 * d.radius)))
        top = self.mesh.surface_level_max + PORT_LEVEL_HEADROOM
        return max(min(need, top), 0)

    def disk_thickness(self, d: "ActuatorDisk") -> float:
        """Товщина шару диска: задана або три комірки його рівня."""
        cell = self.base_cell / 2 ** (self.disk_level(d) if d.refine
                                      else self.mesh.surface_level_max)
        return max(float(d.thickness), DISK_CELLS_THICK * cell)

    @property
    def body_patches(self) -> list[BodyPatch]:
        """Частини тіла, тобто СТІНКИ. Кришки двигунів сюди не входять.

        Це розділення несе вагу. Кришка — не обшивка: на ній немає
        прилипання, пристінних функцій і шарів, і сили на ній — не опір
        планера, а імпульс струменя. Усе, що питає «які в нас стінки»
        (сили, шари, оболонка згущення, умови на стінках), питає тут, і
        кришок не бачить. Поки кришок немає, список той самий, що й був.
        """
        return [b for b in self.surface_patches if b.role not in PORT_ROLES]

    @property
    def port_patches(self) -> list[BodyPatch]:
        """Кришки intake і nozzle — отвори, крізь які дихає двигун."""
        return [b for b in self.surface_patches if b.role in PORT_ROLES]

    @property
    def split_bodies(self) -> bool:
        # По ВСІХ деталях, включно з дисками: тіло й гвинт-диск — це дві
        # деталі, і писати їх треба окремо, щоб диск у сітку не потрапив.
        return len(self.bodies) > 1

    def engine_of(self, patch: str) -> "Engine | None":
        """Двигун, якому належить ця кришка."""
        for e in self.engines:
            if patch == e.nozzle or patch in e.intakes:
                return e
        return None

    def engine_points(self) -> dict[str, EnginePoint]:
        return {e.name: engine_point(e, self.flow) for e in self.engines}

    def port_auto_level(self, b: BodyPatch) -> int | None:
        """Рівень згущення кришки, щоб на її діаметр лягало ≥ 10 комірок.

        Менше — і струмінь на зрізі вийде кількома комірками, профіль
        швидкості розмажеться, а тяга через це не зміниться: вона задана
        витратою. Тобто помилки знову ніхто не побачить, а картина струменя
        буде вигадана.
        """
        e = self.engine_of(b.name)
        if e is None or not e.refine:
            return None
        if b.name == e.nozzle:
            dh = e.nozzle_dh
        else:
            i = e.intakes.index(b.name)
            area = e.intake_areas[i] if i < len(e.intake_areas) else 0.0
            dh = math.sqrt(4.0 * area / math.pi) if area > 0 else 0.0
        if dh <= 0 or self.base_cell <= 0:
            return None
        need = math.ceil(math.log2(self.base_cell * PORT_CELLS_ACROSS / dh))
        # Стеля — на чотири рівні над спільним. Далі кожен рівень множить
        # комірки біля кришки вчетверо, і крихітна кришка на великому
        # літаку з'їла б усю памʼять. Недобір тоді ловить guard_engines.
        top = self.mesh.surface_level_max + PORT_LEVEL_HEADROOM
        return min(max(need, self.mesh.surface_level_max), top)

    def level_of(self, b: BodyPatch) -> tuple[int, int] | None:
        """Власний рівень поверхні: заданий людиною або, для кришки, свій.

        Для стінок це рівно `b.level`, тож для кейсів без двигунів нічого
        не міняється.
        """
        if b.level or b.role not in PORT_ROLES:
            return b.level
        lvl = self.port_auto_level(b)
        return None if lvl is None else (max(lvl - 1, 0), lvl)

    def guard_bodies(self) -> list[tuple[str, str]]:
        """Що не так із роздільними патчами.

        OpenFOAM приймає за імʼя границі лише «слово», а однакові імена двох
        границь — це словник, який не прочитається. Обидві вади виявляються
        аж у журналі snappyHexMesh, тому ловимо їх тут.
        """
        out: list[tuple[str, str]] = []
        if not self.bodies:
            return out
        seen: dict[str, int] = {}
        for b in self.bodies:
            seen[b.name] = seen.get(b.name, 0) + 1
        twice = sorted(n for n, k in seen.items() if k > 1)
        if twice:
            out.append(("bad", (
                tr("Two patches with the same name: {twice}. Rename the "
                   "models or turn off separate patches.", twice=", ".join(twice)))))
        bad = sorted(b.name for b in self.bodies
                     if b.name != patch_name_from(b.name))
        if bad:
            out.append(("bad", (
                tr("Names not usable in OpenFOAM: {bad}. Allowed: Latin "
                   "letters, digits and underscores.", bad=", ".join(bad)))))
        reserved = sorted(set(b.name for b in self.bodies)
                          & {"inlet", "outlet", "farfield", "ground"})
        if reserved:
            out.append(("bad", (
                tr("These names are already used by domain boundaries: "
                   "{reserved}. Rename the models.", reserved=", ".join(reserved)))))

        # --- РОЛІ. Кожна з цих вад мовчить, і в цьому вся річ. -----------
        bad_role = sorted(b.name for b in self.bodies
                          if b.role not in ROLE_KEYS)
        if bad_role:
            out.append(("bad", (
                tr("Unknown part role: {bad_role}. Available: {ROLE_KEYS}.", bad_role=', '.join(bad_role), ROLE_KEYS=', '.join(ROLE_KEYS)))))

        rotors = [b for b in self.bodies if b.role == "rotor"]
        # Роль «гвинт» без зони — найтихіший випадок: частина просто стоїть
        # нерухомою поверхнею, тяги немає, і жодна помилка не зʼявиться.
        #
        # Перевіряємо КОЖНОГО окремо, а не «чи є хоч якась зона». Перша
        # версія питала саме так — і на сцені з двома гвинтами, де зона
        # зібралася лише для одного, мовчала. Другий лишався нерухомою
        # поверхнею, і сказати про це не було кому.
        zoned = {z.name for z in self.rotating}
        orphans = sorted(b.name for b in rotors if b.name not in zoned)
        if orphans:
            out.append(("bad", (
                tr("Marked as propellers, but have no rotating zone: "
                   "{orphans}. Without a zone these are just fixed "
                   "surfaces: there will be no thrust, and no error "
                   "either. The most common cause — the shape is not a "
                   "disk: a disk is thin along the rotation axis and "
                   "ROUND in the other two directions.", orphans=", ".join(orphans)))))
        dead = sorted(b.name for b in rotors if abs(b.rpm) < 1e-9)
        if dead:
            out.append(("warn", (
                tr("Propellers with zero rpm: {dead}. They will do nothing.", dead=', '.join(dead)))))

        movers = [b for b in self.bodies if b.role == "motion"]
        if movers and not self.motion.on:
            out.append(("warn", (
                tr("Parts are marked as moving ({join}), but oscillation "
                   "is off on the “Solver” tab. They will stay still "
                   "with the rest.", join=", ".join(
                    b.name for b in movers)))))
        if self.motion.on and self.bodies and not movers:
            out.append(("warn", (
                tr("Oscillation is on, but no part is marked as moving — so "
                "the WHOLE BODY moves. If only the aileron was meant to "
                "move, give it the role “moving part”."))))

        # Найпідступніше з усього. Тіло, менше за кілька комірок, snappy
        # читає, створює для нього патч — і лишає на ньому НУЛЬ граней. Кейс
        # рахується, помилок немає, а сили на цьому патчі виходять рівно
        # нулі. Побачити різницю між «опору немає» і «тіла немає» без цієї
        # перевірки годі: обидва виглядають як 0.00000.
        cell = self.surface_cell
        if cell > 0:
            # Кришки двигунів плоскі за побудовою: найменший їхній габарит —
            # нуль, і «надто дрібними» вони були б завжди. Їхню сітку
            # перевіряє guard_engines, по діаметру.
            thin = [(b.name, b.span) for b in self.bodies
                    if 0 < b.span < cell * 4 and b.role not in PORT_ROLES
                    and b.role != DISK_ROLE]
            if thin:
                out.append(("warn", (
                    tr("These parts are too small for the mesh: {join}. "
                       "snappyHexMesh can leave their patches WITHOUT A "
                       "SINGLE face — then the forces on them come out "
                       "exactly zero, and it will look like “there is no "
                       "drag”. Raise the refinement level or remove "
                       "these models from the scene.", join=", ".join(tr("{n} ({s} at "
                                                        "cell {cell})", n=n, s=phys.fmt_len(s), cell=phys.fmt_len(cell)) for n, s in thin)))))
        out.extend(self.guard_engines())
        out.extend(self.guard_disks())
        return out

    def guard_disks(self) -> list[tuple[str, str]]:
        """Що не так із гвинтами-дисками. Порожньо без них.

        Тиха вада тут одна, але головна: диск, у який не потрапила жодна
        комірка, не дає сили зовсім — вираз ділить на нуль і множить на
        нуль. Звідси перевірки розміру проти сітки.
        """
        out: list[tuple[str, str]] = []
        if not self.disks:
            return out
        if self.flow.supersonic:
            out.append(("warn", (
                tr("Actuator disks are off in supersonic mode: "
                   "rhoCentralFoam does not apply sources, and they "
                   "would give no thrust. Disks: {join}.", join=", ".join(d.name for d in self.disks)))))
            return out
        lo, hi = self.bounds[0::2], self.bounds[1::2]
        for d in self.disks:
            if d.radius <= 0 or d.hub >= d.radius:
                out.append(("bad", (
                    tr("Actuator disk {name}: the hub ({hub}) is not "
                       "smaller than the radius ({radius}) — there is no "
                       "ring for thrust.", name=d.name, hub=phys.fmt_len(d.hub), radius=phys.fmt_len(d.radius)))))
                continue
            if math.sqrt(sum(x * x for x in d.axis)) < 1e-12:
                out.append(("bad", tr("Actuator disk {name}: the axis is zero.", name=d.name)))
                continue
            if d.profile not in DISK_PROFILE_KEYS:
                out.append(("bad", tr("Actuator disk {name}: unknown "
                                      "profile “{profile}”.", name=d.name, profile=d.profile)))
            if not all(l < c < h for l, c, h in zip(lo, d.centre, hi)):
                out.append(("bad", (
                    tr("Actuator disk {name} is outside the domain — its "
                       "force will not reach any cell.", name=d.name))))
            if d.thrust <= 0 and d.torque <= 0:
                out.append(("warn", tr("Actuator disk {name}: thrust and "
                                       "torque are zero — it will do "
                                       "nothing.", name=d.name)))
            elif d.from_throttle and d.throttle_factor <= 0:
                out.append(("warn", (
                    tr("Actuator disk {name}: throttle at zero — there "
                       "is no thrust. Move the throttle on the control "
                       "panel or clear “from throttle”.", name=d.name))))
            level = (self.disk_level(d) if d.refine
                     else self.mesh.surface_level_max)
            cell = self.base_cell / 2 ** level if self.base_cell > 0 else 0
            if cell > 0:
                across = 2 * d.radius / cell
                if across < DISK_CELLS_ACROSS * 0.99:
                    out.append(("warn", (
                        tr("Actuator disk {name}: {across:.0f} cells "
                           "across the diameter (needs "
                           "{DISK_CELLS_ACROSS}). The thrust does not "
                           "change because of this — it is normalized — "
                           "but it lands as a blot, not a disk.{v}", name=d.name, across=across, DISK_CELLS_ACROSS=DISK_CELLS_ACROSS, v="" if d.refine else
                           " Turn on “refine mesh in the disk”.")
                        )))
                ring = d.radius - d.hub
                if ring < 2 * cell:
                    out.append(("bad", (
                        tr("Actuator disk {name}: the ring from the hub "
                           "to the tip ({ring}) is narrower than two "
                           "cells ({cell}) — it may not contain a single "
                           "cell center, and there will be no force.", name=d.name, ring=phys.fmt_len(ring), cell=phys.fmt_len(cell)))))
            if d.torque > 0 and d.thrust > 0:
                # Q/(T·R) — безрозмірна «закрутка». Для звичайних гвинтів
                # 0.05–0.3; на порядок більше — найпевніше, переплутано
                # одиниці (Н·мм замість Н·м).
                ratio = d.torque / (d.thrust * d.radius)
                if ratio > 2.0:
                    out.append(("warn", (
                        tr("Actuator disk {name}: the torque is too "
                           "large for this thrust (Q/(T·R) = "
                           "{ratio:.1f}, usually 0.05–0.3). Check the "
                           "units: N·m, not N·mm.", name=d.name, ratio=ratio))))
        return out

    def guard_engines(self) -> list[tuple[str, str]]:
        """Що не так із двигунами. Порожньо, коли двигунів немає.

        Більшість цих вад тиха: кришка без двигуна стає отвором із нульовою
        витратою, тобто плоскою стінкою; двигун без сопла лише ковтає
        повітря. Розрахунок пройде, а тяги не буде.
        """
        out: list[tuple[str, str]] = []
        ports = self.port_patches
        if not ports and not self.engines:
            return out
        names = {e.name for e in self.engines}
        loose = sorted(b.name for b in ports
                       if not b.engine or b.engine not in names)
        if loose:
            out.append(("bad", (
                tr("Caps without an engine: {loose}. Each intake or "
                   "nozzle cap must belong to an engine — otherwise it "
                   "becomes a flat wall with zero flow rate.", loose=", ".join(loose)))))
        for e in self.engines:
            extra = sorted(b.name for b in ports if b.role == "nozzle"
                           and b.engine == e.name and b.name != e.nozzle)
            if extra:
                out.append(("bad", (
                    tr("Engine {name} has more than one nozzle: "
                       "{nozzle}, {extra}. An engine has one nozzle; for "
                       "a second jet, add a second engine.", name=e.name, nozzle=e.nozzle, extra=", ".join(extra)))))
            if not e.nozzle:
                out.append(("bad", (
                    tr("Engine {name} has no nozzle: it would only "
                       "swallow air. Give the exit cap the “nozzle” role.", name=e.name))))
            if not e.intakes:
                out.append(("bad", (
                    tr("Engine {name} has no intake: the jet would come "
                       "from nowhere. Give the opening cap the “intake” "
                       "role.", name=e.name))))
            if e.kind not in ENGINE_KIND_KEYS:
                out.append(("bad", tr("Engine {name}: unknown type “{kind}”.", name=e.name, kind=e.kind)))
            if not (e.nozzle and e.intakes):
                continue
            if e.nozzle_area <= 0:
                out.append(("bad", (
                    tr("Engine {name}: the nozzle area is unknown or "
                       "zero. Without it, neither the mass flow nor the "
                       "jet speed can be calculated from the thrust.", name=e.name))))
                continue
            pt = engine_point(e, self.flow)
            if self.portal_mode and math.sqrt(
                    sum(x * x for x in e.nozzle_normal)) < 1e-12:
                out.append(("bad", (
                    tr("Engine {name}: the nozzle has no direction — the "
                       "normals of its surface cancel each other (a "
                       "closed or flipped surface). The portal needs an "
                       "open surface whose normals point in the "
                       "direction of the jet.", name=e.name))))
            if e.from_throttle and e.throttle_factor <= 0:
                out.append(("warn", (
                    tr("Engine {name}: throttle at zero — there is no "
                       "thrust, both caps act as walls. Move the "
                       "throttle on the control panel or clear “from "
                       "throttle”.", name=e.name))))
            if pt.mach_exit > 0.9:
                out.append(("warn", (
                    tr("Engine {name}: at the nozzle exit M = "
                       "{mach_exit:.2f} ({v_exit:.0f} m/s). Such a "
                       "nozzle is most likely choked, and here the exit "
                       "pressure is taken as atmospheric — the model "
                       "does not fit a choked jet. Use a larger nozzle "
                       "or less thrust.", name=e.name, mach_exit=pt.mach_exit, v_exit=pt.v_exit))))
            for n, m in zip(e.intakes, pt.mach_intakes):
                if m > 0.6:
                    out.append(("warn", (
                        tr("Intake {n}: the speed at the cap is M = "
                           "{m:.2f}. The opening is too small for this "
                           "mass flow.", n=n, m=m))))
            if e.hot and not self.flow.compressible:
                out.append(("warn", (
                    tr("Engine {name} is a turbine, and the mode is "
                       "incompressible: the jet will come out cold, with "
                       "the density of the freestream air. A hot jet "
                       "needs the compressible mode.", name=e.name))))
            if e.thrust_mode not in THRUST_MODE_KEYS:
                out.append(("bad", tr("Engine {name}: unknown meaning of "
                                      "thrust (“{thrust_mode}”).", name=e.name, thrust_mode=e.thrust_mode)))
            if e.direct and pt.thrust < 0:
                out.append(("warn", (
                    tr("Engine {name}: with this ṁ and Vₑ the thrust is "
                       "negative ({thrust:.1f} N) — the jet is slower "
                       "than the flight speed.", name=e.name, thrust=pt.thrust))))
            elif not e.direct and e.v_jet > 0 and pt.mdot_nozzle <= 0 \
                    and e.thrust > 0 and e.gas > 0:
                out.append(("warn", (
                    tr("Engine {name}: exhaust {v_jet:g} m/s is not "
                       "faster than the flight speed {u_inf:g} m/s — "
                       "with such a jet there will be no thrust in "
                       "flight.", name=e.name, v_jet=e.v_jet, u_inf=self.flow.u_inf))))
            if (not e.direct and e.v_jet > 0 and not self.portal_mode
                    and pt.area_needed > 0 and e.nozzle_area > 0):
                ratio = e.nozzle_area / pt.area_needed
                if abs(ratio - 1.0) > 0.2:
                    need = 2.0 * math.sqrt(pt.area_needed / math.pi)
                    have = 2.0 * math.sqrt(e.nozzle_area / math.pi)
                    out.append(("warn", (
                        tr("Engine {name}: a nozzle cap of {v:.0f} mm "
                           "will not pass {mdot_nozzle:.3g} kg/s at "
                           "{v_exit:.0f} m/s — this needs ≈ {v2:.0f} mm. "
                           "On a cap patch the speed is set by its area, "
                           "so the jet will come out at {v3:.0f} m/s. "
                           "Portals (compressible mode) do not depend on "
                           "the disk size.", name=e.name, v=have * 1000, mdot_nozzle=pt.mdot_nozzle, v_exit=pt.v_exit, v2=need * 1000, v3=pt.mdot_nozzle / (pt.rho_exit * e.nozzle_area)))))
            if (not e.direct and e.v_jet <= 0 and e.hot
                    and 0 < pt.v_exit < 250.0):
                need = 2.0 * math.sqrt(
                    pt.mdot_nozzle * pt.v_exit / 450.0 ** 2
                    / max(pt.rho_exit, 1e-9) / math.pi) if pt.v_exit else 0
                out.append(("warn", (
                    tr("Engine {name}: the turbine jet came out at only "
                       "{v_exit:.0f} m/s — in a micro turbojet it is "
                       "400–600. Most likely the nozzle cap is too "
                       "large: the mass flow comes from its area. Set "
                       "the “exhaust velocity” from the datasheet or "
                       "make the nozzle smaller (for ~450 m/s at this "
                       "thrust — ≈ {v:.0f} mm).", name=e.name, v_exit=pt.v_exit, v=need * 1000))))
            if (not e.direct and e.thrust_mode == "static"
                    and pt.mdot_nozzle > 0 and pt.thrust <= 0):
                out.append(("warn", (
                    tr("Engine {name}: at {u_inf:g} m/s and this "
                       "throttle it no longer pulls — the jet at "
                       "{v_exit:.0f} m/s is not faster than the flight, "
                       "thrust {thrust:.1f} N. More throttle or less "
                       "speed.", name=e.name, u_inf=self.flow.u_inf, v_exit=pt.v_exit, thrust=pt.thrust))))
            for b in ports:
                if self.engine_of(b.name) is not e or not e.refine:
                    continue
                if b.name == e.nozzle:
                    dh = e.nozzle_dh
                else:
                    i = e.intakes.index(b.name)
                    a = e.intake_areas[i] if i < len(e.intake_areas) else 0.0
                    dh = math.sqrt(4.0 * a / math.pi) if a > 0 else 0.0
                lvl = self.port_auto_level(b)
                if lvl is None or dh <= 0 or self.base_cell <= 0:
                    continue
                across = dh / (self.base_cell / 2 ** lvl)
                if across < PORT_CELLS_ACROSS * 0.99:
                    out.append(("warn", (
                        tr("Cap {name}: only {across:.0f} cells across "
                           "its diameter (needs {PORT_CELLS_ACROSS}), "
                           "because the refinement is already at its "
                           "maximum. The jet will be smeared, though the "
                           "thrust will not change.", name=b.name, across=across, PORT_CELLS_ACROSS=PORT_CELLS_ACROSS))))
        if self.engines and self.flow.supersonic:
            out.append(("warn", (
                tr("Engines in supersonic mode: OpenFOAM itself calls the "
                "conditions with a fixed flow rate unreliable in the "
                "transonic range. Check the measured engine force in the "
                "results."))))
        return out

    def guard_turbulence(self) -> list[tuple[str, str]]:
        """Чи має сенс вибрана модель у вибраній постановці.

        Вихрові методи рахують вихори ЗА ЧАСОМ. Запустити їх стаціонарним
        солвером — це попросити усталений стан у течії, яка усталеного стану
        не має: вихори просто розмажуться в ту саму усереднену картину, що й
        у RANS, тільки коштуватиме це втричі дорожче.
        """
        out: list[tuple[str, str]] = []
        if not self.flow.les:
            return out
        if self.solver.steady:
            out.append(("bad", (
                tr("{turbulence} is an eddy-resolving model, it "
                   "calculates eddies in time. With a steady solver it "
                   "gives the same averaged picture as normal RANS, only "
                   "at a higher cost. Clear the “Steady” check box on "
                   "the “Solver” tab.", turbulence=self.flow.turbulence))))
        # Вихори треба чимось розвʼязувати. Комірка, у яку їх поміститься
        # менше десятка на хорду, перетворює LES на дорогий RANS.
        ref = self.flow.l_ref or self.body_length
        cell = self.surface_cell
        if cell > 0 and ref > 0 and ref / cell < 40:
            out.append(("warn", (
                tr("The reference length {ref} has {v:.0f} cells. An "
                   "eddy-resolving model on such a mesh will resolve "
                   "only the largest eddies and will model the rest "
                   "anyway — so it turns into an expensive RANS. LES and "
                   "DES need a mesh at least two times finer than RANS.", ref=phys.fmt_len(ref), v=ref / cell))))
        return out

    def guard_yplus(self) -> list[tuple[str, str]]:
        """Does the first-cell y+ suit the turbulence model? See fs_yplus."""
        import fs_yplus
        return fs_yplus.guard(self)

    def guard_refine_zones(self) -> list[tuple[str, str]]:
        """Чи не згущує зона саму СТІНКУ нерівномірно.

        Зона, що лежить на тілі й задає рівень вищий за поверхневий, робить
        комірку біля стінки різною в різних місцях. За нею різною стає
        товщина першого призматичного шару, а отже й y+ — і пристінні
        функції по обидва боки стрибка працюють по-різному.

        Ціна поміряна. Сітка, у якої поверхня згущена плямами за градієнтом
        тиску, дала на нульовому куті Cl = 0.0702 проти 0.1241 у
        рівномірної: на 43% менше. y+ уздовж хорди в неї стрибав 51, 58,
        87, 15 замість плавних 52, 38, 28, 25.

        Це попередження, а не заборона: якщо потрібен лише пік розрідження
        на носку, а не сили в цілому, така сітка згодиться й буде дешевшою.
        """
        out: list[tuple[str, str]] = []
        top = self.mesh.surface_level_max
        loud = [(name, lv[0][1]) for name, lv in self.refine_only_levels()
                if lv and lv[0][1] > top]
        if not loud:
            return out
        where = ", ".join(tr("{n} (level {l})", n=n, l=l) for n, l in loud)
        out.append(("warn", (
            tr("These zones refine the surface itself more than the rest "
               "of the body: {where} vs {top} on the surface. Prism "
               "layers grow from the LOCAL cell, so at the zone boundary "
               "the first layer thickness jumps, and y+ with it. In a "
               "test on a wing, y+ along the chord went 51, 58, 87, 15 "
               "instead of a smooth 52, 38, 28, 25 — and lift at zero "
               "angle came out 43% lower. Such a mesh is good for the "
               "suction peak at the leading edge, but not for the forces "
               "as a whole. Either make the surface level equal, or "
               "refine only the volume.", where=where, top=top))))
        return out

    def guard_zone_vs_shell(self) -> list[tuple[str, str]]:
        """Чи не просить зона грубішого, ніж уже дає оболонка.

        Зона згущення, яка на своїй відстані просить рівень НЕ ВИЩИЙ за той,
        що там уже стоїть, не робить нічого. snappyHexMesh бере максимум зі
        всіх вимог, тому слабша вимога просто не помічається — ні
        попередження, ні різниці в сітці.

        Тиха ця вада тим, що кейс рахується й дає правильні числа. Помітити
        її можна лише порівнявши кількість комірок із варіантом БЕЗ зони: у
        перевірці вийшло 405 541 проти 405 544, тобто три комірки з
        чотирьохсот тисяч. Я мало не доповів із цього висновок «карта
        активності не потрібна», хоч насправді карта жодного разу не
        спрацювала.

        Порівняння наближене: відстань до зони й відстань до тіла — різні
        величини. Але зона активності будується навколо тіла й до нього
        притиснута, тому на її внутрішніх сходинках ці відстані близькі, і
        груба перевірка тут ловить саме той випадок, який треба.
        """
        out: list[tuple[str, str]] = []
        ladder = self.shell_ladder()
        if not ladder:
            return out

        def shell_level_at(dist: float) -> int:
            """Який рівень уже є на цій відстані — з оболонкою й переходом.

            Оболонкою справа не кінчається. За її останньою сходинкою
            snappyHexMesh САМ розтягує перехід до фонового рівня: кожен
            рівень тримається приблизно `n_cells_between_levels` своїх
            комірок, далі падає на одиницю. Ця смуга широка — для рівня 3
            це два по 29 мм, для рівня 2 два по 58.

            Не врахувавши її, я вдруге поставив зону, яка нічого не
            зробила: вона просила рівень 3 до 120 мм і рівень 2 до 240, а
            природний перехід давав там рівно те саме. Різниця вийшла
            252 комірки з чотирьохсот тисяч.
            """
            for d, lvl in ladder:
                if dist <= d:
                    return lvl
            # За оболонкою — природний спад. Ідемо від її останнього рівня
            # й віднімаємо по одиниці кожні n комірок цього рівня.
            edge, lvl = ladder[-1]
            n = max(int(self.mesh.n_cells_between_levels), 1)
            base = self.base_cell
            while lvl > 0:
                edge += n * base / (2 ** lvl)
                if dist <= edge:
                    return lvl
                lvl -= 1
            return 0

        weak = []
        for name, levels in self.refine_only_levels():
            for dist, lvl in levels:
                have = shell_level_at(dist)
                if have >= lvl:
                    weak.append((name, dist, lvl, have))
        if not weak:
            return out
        where = "; ".join(
            tr("{n}: level {l} at {v:.0f} mm, but the shell there "
               "already gives {h}", n=n, l=l, v=d * 1000, h=h) for n, d, l, h in weak[:3])
        out.append(("warn", (
            tr("These refinement region steps will do nothing: {where}. "
               "snappyHexMesh takes the highest requirement, so the "
               "weaker one simply disappears — silently, with no "
               "difference in the mesh. For the zone to make sense, it "
               "must ask for refinement where the shell no longer "
               "reaches (beyond {v:.0f} mm), or ask for a level higher "
               "than the level there.", where=where, v=ladder[-1][0] * 1000))))
        return out

    def guard_adapt(self) -> list[tuple[str, str]]:
        """Що не так із вибраним режимом адаптації.

        Тут саме та логіка, заради якої режими й розділено: вибір способу
        тягне за собою розвʼязувача, і мовчки це поєднати не можна.
        """
        out: list[tuple[str, str]] = []
        a = self.adapt
        if not a.on:
            return out
        if a.mode not in ADAPT_MODES:
            out.append(("bad", tr("Unknown adaptation mode “{mode}”. "
                                  "Available: {ADAPT_MODES}.", mode=a.mode, ADAPT_MODES=', '.join(ADAPT_MODES))))
            return out

        if a.native:
            # Це не порада, а заборона. simpleFoam динамічної сітки не
            # вміє взагалі, і прогін упаде без зрозумілого повідомлення.
            if self.solver.steady:
                out.append(("bad", (
                    tr("Native adaptation changes the mesh DURING the run, "
                    "and a steady solver cannot do this: simpleFoam does "
                    "not work with a dynamic mesh. Clear the “Steady” "
                    "check box — or use the “rebuild” mode, which works "
                    "with a steady solver."))))
            if self.motion.on:
                out.append(("bad", (
                    tr("Native adaptation and a moving mesh are two "
                    "different dynamicFvMesh, and a case can have only "
                    "one. Choose one of them."))))
            if self.mesh.add_layers and self.solver.max_courant > 2.0:
                # Шари самі по собі адаптації НЕ заважають — це поміряно:
                # прогін із трьома призматичними шарами поділив сітку зі
                # 162 793 комірок до 457 857 і спокійно дорахував до
                # кінця. Ходова думка, що hexRef8 не вміє шарів, на нашій
                # сітці не підтвердилася.
                #
                # Заважає інше, і воно справжнє. Комірка в шарі тонка, і
                # після поділу стає ще тоншою — а межа стійкості за часом
                # рахується саме з неї. При Куранті 5 той самий кейс
                # загинув на 47-му кроці: k пішло до 5.7e+73. При одиниці
                # пройшов повністю.
                out.append(("warn", (
                    tr("Courant number {max_courant:g} is too large with "
                       "layers and native adaptation. A cell in the "
                       "layer is already thin, and after splitting it "
                       "becomes half as thick — the stability limit is "
                       "computed from exactly that cell. In a test at 5 "
                       "the run died at step 47 (k blew up to 5.7e+73), "
                       "and at 1 it ran to the end. Set 1-2 or remove "
                       "the layers.", max_courant=self.solver.max_courant))))
            if self.solver.max_courant > 0 and a.interval < 5:
                out.append(("warn", (
                    tr("Checking the mesh every {interval} steps is "
                       "expensive: each check is a topology rebuild and "
                       "a redistribution between processes. Twenty to "
                       "fifty steps are usually enough.", interval=a.interval))))
        else:
            # Перебудова: два проходи, і дешевий має бути справді дешевим.
            if self.mesh.surface_level_max >= 6 and self.mesh.add_layers:
                out.append(("warn", (
                    tr("The rebuild makes TWO passes, and the first one is "
                    "only needed to find out where to refine. Putting "
                    "the same fine surface and layers on it means paying "
                    "twice. Level 3-4 without layers is enough for the "
                    "first pass."))))

        if not 0.0 < a.fraction < 0.5:
            out.append(("bad", (
                tr("A refinement cell fraction of {fraction:.0%} is "
                   "outside common sense. Use 2 to 20 percent: less, and "
                   "the region has no time to affect anything; more, and "
                   "it is no longer adaptation but uniform refinement.", fraction=a.fraction))))
        if a.max_level > 3:
            out.append(("warn", (
                tr("Splitting up to {max_level} levels means a cell can "
                   "become {v} times finer along each axis, that is, "
                   "{v2} times smaller in volume. The cell-count cap "
                   "here is not a safeguard but the last limit: two "
                   "levels are almost always enough.", max_level=a.max_level, v=2 ** a.max_level, v2=8 ** a.max_level))))
        return out

    def guard_boxes(self) -> list[tuple[str, str]]:
        """Що не так із коробками згущення.

        Коробка — це найтихіша з усіх налаштовуваних речей. Вона не падає
        й не скаржиться: просто згущує те місце, яке їй указали. Якщо
        вказати повз тіло, snappyHexMesh сумлінно подрібнить порожнє
        повітря, сітка виросте вдесятеро, а біля моделі лишиться як була.
        """
        out: list[tuple[str, str]] = []
        if not self.mesh.boxes:
            return out
        lo_d, hi_d = self.bounds[0::2], self.bounds[1::2]
        for b in self.mesh.boxes:
            bad_axis = [nm for nm, lo, hi in zip("XYZ", b.min, b.max)
                        if hi - lo <= 0]
            if bad_axis:
                out.append(("bad", (
                    tr("Box “{name}”: on {bad_axis} the upper bound is "
                       "not greater than the lower one. Such a box "
                       "cannot exist, and snappyHexMesh will simply skip "
                       "it.", name=b.name, bad_axis=', '.join(bad_axis)))))
                continue

            # ЧИ НАКРИВАЄ ВОНА ТІЛО. Коробка повз модель — це згущення
            # порожнечі: помилки немає, сітка росте, користі нуль.
            overlap = all(hi > lo_b and lo < hi_b for lo, hi, lo_b, hi_b
                          in zip(b.min, b.max, self.bbox_min, self.bbox_max))
            if not overlap:
                out.append(("bad", (
                    tr("Box “{name}” does not intersect the body at all. "
                       "It will refine empty air: the mesh will grow, "
                       "nothing will change near the model, and there "
                       "will be no error.", name=b.name))))
                continue

            inside = all(lo >= d0 - 1e-9 and hi <= d1 + 1e-9
                         for lo, hi, d0, d1 in zip(b.min, b.max, lo_d, hi_d))
            if not inside:
                out.append(("warn", (
                    tr("Box “{name}” goes outside the domain. The part "
                       "outside does nothing — snappyHexMesh cuts it at "
                       "the boundary. There will be no error, but that "
                       "part gives no benefit either.", name=b.name))))

            # ОБСЯГ. Рівень 2 усередині коробки — це у 8 разів більше
            # комірок на той самий обсяг, рівень 3 — у 64. Коробка на пів
            # домену при рівні 3 це не згущення, це інша сітка.
            dom = 1.0
            for d0, d1 in zip(lo_d, hi_d):
                dom *= max(d1 - d0, 1e-12)
            share = b.volume / dom
            if share > 0.25 and b.level >= 2:
                out.append(("warn", (
                    tr("Box “{name}” takes {v:.0f}% of the domain at "
                       "level {level}. This multiplies the cells in that "
                       "volume by {v2} — compute the mesh estimate "
                       "before you run.", name=b.name, v=share * 100, level=b.level, v2=2 ** (3 * b.level)))))
        return out

    def guard_scale(self) -> list[tuple[str, str]]:
        """Чи правдоподібний сам РОЗМІР тіла.

        Найтихіша з усіх вад одиниць: модель, зменшена чи збільшена в
        тисячу разів, виглядає нормально — бо камера підганяється під
        неї. Помітно стає лише тоді, коли поруч є щось із абсолютними
        координатами: коробка згущення, домен, точка в потоці.

        Саме так і сталося: геометрія з сесії дістала множник удруге,
        стала 0.5 мм, а коробка лишилася двометровою — і виглядало це
        як «зона згущення стала гігантською». Числа в файлі при цьому
        були правильні, тож звірка з файлом нічого не показувала.

        Межі свідомо широкі. Комаха 2 мм і літак 60 м — це справжні
        задачі; 0.5 мм і 5 км — майже завжди переплутані одиниці.
        """
        out: list[tuple[str, str]] = []
        size = max(self.body_length, self.body_span, self.body_height)
        if size <= 0:
            return out
        if size < 1e-3:
            out.append(("bad", (
                tr("The body is {v:.0f} µm in size — smaller than a "
                   "poppy seed. The units are almost certainly mixed up: "
                   "check the “STL units” field on the “Geometry” tab. A "
                   "mesh on such a body will compute, just not the thing "
                   "you want.", v=size * 1e6))))
        elif size > 1e3:
            out.append(("bad", (
                tr("The body is {v:.1f} km long. The units are almost "
                   "certainly mixed up: check the “STL units” field. The "
                   "domain around such a body will be tens of "
                   "kilometers, and a cell will be the size of a house.", v=size / 1000))))

        # КОРОБКА, НАБАГАТО БІЛЬША ЗА ТІЛО, — та сама хвороба з іншого боку:
        # тіло зменшилося, а коробка лишилася в абсолютних координатах.
        for b in self.mesh.boxes:
            span = max(b.max[i] - b.min[i] for i in range(3))
            if span > size * 200:
                out.append(("warn", (
                    tr("Box “{name}” is {v:.0f} times larger than the "
                       "body. This happens when the body was rescaled "
                       "but the box stayed at the old coordinates — "
                       "check the units and click “Use computed box”.", name=b.name, v=span / size))))
        return out

    def guard_resolution(self) -> list[tuple[str, str]]:
        """Чи вистачає комірок на опорну довжину, щоб силам можна було вірити.

        Це найтихіша з усіх вад. Кейс рахується, збігається, журнал чистий,
        коефіцієнти виходять красиві — просто неправильні. На тестовому
        літаку та сама модель на 167 тисячах комірок і на 543 тисячах дала:

            комірок на хорду     27        54
            Cd на нульовому    0.0388    0.0248     -36%
            Cl на нульовому    0.1932    0.1241     -36%
            нахил Cl(alpha)    0.0261    0.0445     +70%

        Тобто число з грубої сітки помилялося в півтора раза й ніяк про це
        не повідомляло. І навіть 54 комірки на хорду — ще не збіжність:
        теорія скінченного крила дає для того крила 0.0826.

        Чому саме хорда, а не довжина тіла: підйомну силу робить пік
        розрідження на носку профілю, а він завширшки з радіус кромки.
        Довжина фюзеляжу до цього стосунку не має.
        """
        out: list[tuple[str, str]] = []
        cell = self.surface_cell
        ref = self.flow.l_ref or self.body_length
        if cell <= 0 or ref <= 0:
            return out
        per_ref = ref / cell
        if per_ref >= 60:
            return out
        # Напрямок помилки не один і той самий: на нульовому куті груба
        # сітка ЗАВИЩУЄ і Cd, і Cl, а нахил кривої підйомної сили — занижує.
        # Тому кажемо «змінюються», а не «занижуються»: обіцяти напрямок
        # означало б збрехати в половині випадків.
        how = (tr("The coefficients will be wrong, and noticeably so")
               if per_ref < 30 else tr("The answer is not yet mesh-converged"))
        out.append(("warn", (
            tr("The reference length {ref} has only {per_ref:.0f} cells "
               "(surface cell {cell}). Aerodynamics needs 60 or more, "
               "and a converged answer needs more. {how}: in a test on a "
               "wing, going from 27 cells to 54 changed Cd and Cl by "
               "36%, and the lift curve slope by 70%. Raise the surface "
               "refinement level or the number of cells along X.", ref=phys.fmt_len(ref), per_ref=per_ref, cell=phys.fmt_len(cell), how=how))))
        return out

    def local_cell_at(self, point) -> float:
        """Розмір комірки в цій точці — з урахуванням коробок згущення.

        Порівнювати зону з ФОНОВОЮ коміркою було б нечесно: коробка
        рефайнменту часто саме для того й стоїть, щоб згустити сітку там, де
        лежить радіатор. Попередження, яке кричить завжди, привчає його не
        читати.
        """
        level = 0
        for b in self.mesh.boxes:
            if all(lo <= p <= hi for p, lo, hi in zip(point, b.min, b.max)):
                level = max(level, b.level)
        return self.base_cell / (2 ** level)

    def guard_porous(self) -> list[tuple[str, str]]:
        """Що не так із пористими зонами.

        Найважливіше тут — зона, у яку не потрапила жодна комірка. topoSet
        такого не вважає помилкою: він створює порожню зону, fvOptions її
        приймає, розрахунок іде, і опору просто немає. Виглядає це як
        «радіатор нічого не гальмує», а не як вада.
        """
        out: list[tuple[str, str]] = []
        if not self.porous:
            return out
        seen: dict[str, int] = {}
        for z in self.porous:
            seen[z.name] = seen.get(z.name, 0) + 1
        twice = sorted(n for n, k in seen.items() if k > 1)
        if twice:
            out.append(("bad", tr("Two zones with the same name: {twice}.", twice=", ".join(twice))))
        bad = sorted(z.name for z in self.porous
                     if z.name != patch_name_from(z.name))
        if bad:
            out.append(("bad", tr("Zone names that OpenFOAM cannot use: "
                                  "{bad}. Only Latin letters, digits and "
                                  "underscores are allowed.", bad=", ".join(bad))))

        lo_d, hi_d = self.bounds[0::2], self.bounds[1::2]
        for z in self.porous:
            if min(z.size) <= 0 or z.dp <= 0 or z.u_ref <= 0:
                out.append(("bad", tr("Zone {name}: the size, pressure "
                                      "drop and velocity must be "
                                      "positive.", name=z.name)))
                continue
            lo, hi = z.bounds
            if any(h <= d0 or l >= d1 for l, h, d0, d1
                   in zip(lo, hi, lo_d, hi_d)):
                out.append(("bad", (
                    tr("Zone {name} is outside the domain — no cell will "
                       "fall into it, and there will be no drag at all.", name=z.name))))
                continue
            # Зона, тонша за комірку, не отримає жодного шару комірок.
            thin = min(z.size)
            local = self.local_cell_at(z.centre)
            if local > 0 and thin < local * 2:
                out.append(("warn", (
                    tr("Zone {name} is {thin} thick with a cell of "
                       "{local} at this place. It may get no cell at "
                       "all, and then there will be no drag at all — "
                       "without any error. Make the zone thicker or "
                       "cover it with a refinement box.", name=z.name, thin=phys.fmt_len(thin), local=phys.fmt_len(local)))))
        return out

    @property
    def needs_topo_set(self) -> bool:
        return bool(self.porous or self.rotating or self.portal_mode
                    or self.disks_on)

    @property
    def portal_mode(self) -> bool:
        """Чи кришки двигунів — ПОРТАЛИ, а не патчі.

        Портал — проникна поверхня: на ній повітря зникає (вхід) або
        з'являється (вихід), а крізь неактивний бік проходить вільно. Для
        цього потрібне джерело МАСИ, а його вміють лише стисливі
        тиск-орієнтовані розвʼязувачі: у `pEqn.H` simpleFoam і pimpleFoam
        `fvOptions` немає зовсім, у rhoSimpleFoam і rhoPimpleFoam є
        `fvOptions(psi, p, rho)`. rhoCentralFoam джерел не застосовує.

        Тож: стисливий дозвуковий — портали; решта — кришки-патчі на моторі,
        який моделює людина.
        """
        return (bool(self.port_patches) and self.flow.compressible
                and not self.flow.supersonic)

    @property
    def mesh_surfaces(self) -> list[BodyPatch]:
        """Поверхні, з яких snappy робить ГРАНИЦІ.

        У режимі порталів кришки границями не стають: вони лише згущують
        сітку довкола себе й задають шар комірок для джерел.
        """
        if self.portal_mode:
            return self.body_patches
        return self.surface_patches

    def portal_cell(self, b: BodyPatch) -> float:
        """Розмір комірки біля порталу, м."""
        lvl = self.level_of(b)
        level = lvl[1] if lvl else self.mesh.surface_level_max
        return self.base_cell / 2 ** level

    def portal_thickness(self, b: BodyPatch) -> float:
        """Товщина шару джерел з активного боку порталу: дві комірки.

        Одна — і шар місцями проскакував би між центрами комірок, лишаючи
        дірки; більше — і струмінь народжувався б не на поверхні, а в
        товстій «подушці» перед нею.
        """
        return PORTAL_LAYER_CELLS * self.portal_cell(b)

    def guard_rotating(self) -> list[tuple[str, str]]:
        """Що не так із обертовими зонами.

        Найважливіше тут — не словники, а дві речі, яких OpenFOAM не
        перевіряє й перевіряти не буде.
        """
        out: list[tuple[str, str]] = []
        if not self.rotating:
            return out
        lo_d, hi_d = self.bounds[0::2], self.bounds[1::2]
        for z in self.rotating:
            if z.radius <= 0 or z.length <= 0:
                out.append(("bad", tr("Zone {name}: the radius and "
                                      "length must be positive.", name=z.name)))
                continue
            if z.name != patch_name_from(z.name):
                out.append(("bad", tr("Zone name “{name}” cannot be used "
                                      "in OpenFOAM: only Latin letters, "
                                      "digits and underscores.", name=z.name)))
            if abs(z.rpm) < 1e-9:
                out.append(("warn", (
                    tr("Zone {name} rotates at zero speed — it does "
                       "nothing. It is not worth running for the zone "
                       "alone.", name=z.name))))

            # Зона має лежати в домені й накривати геометрію.
            p1, p2 = z.ends
            zlo = tuple(min(a, b) - z.radius for a, b in zip(p1, p2))
            zhi = tuple(max(a, b) + z.radius for a, b in zip(p1, p2))
            if any(h <= d0 or l >= d1 for l, h, d0, d1
                   in zip(zlo, zhi, lo_d, hi_d)):
                out.append(("bad", (
                    tr("Zone {name} is outside the domain — no cell will "
                       "fall into it, and there will be nothing to "
                       "rotate.", name=z.name))))
                continue
            # ЧАСТКОВО ЗА МЕЖЕЮ — окрема біда, і перевірка вище її НЕ ЛОВИТЬ:
            # вона питає лише, чи є перетин узагалі.
            #
            # Спіймалося на голому гвинті: домен будується з габариту тіла,
            # а в диска габарит уздовж потоку 30 мм при діаметрі 254. Домен
            # вийшов 152 мм завширшки, лопаті стирчали з нього назовні — і
            # це прийшло як «усе гаразд». Обрізаний гвинт крутився б із
            # обрізаною тягою, і зрозуміти це з чисел було б нелегко.
            stick = [nm for nm, l, h, d0, d1
                     in zip("XYZ", zlo, zhi, lo_d, hi_d)
                     if l < d0 or h > d1]
            if stick:
                out.append(("bad", (
                    tr("Zone {name} sticks out of the domain along "
                       "{stick}. The domain is computed from the OVERALL "
                       "SIZE of the body, and for a disk the size along "
                       "the axis is much smaller than the diameter — so "
                       "the box comes out too tight. Increase the domain "
                       "margins.", name=z.name, stick=', '.join(stick)))))
                continue

            # ГОЛОВНЕ. MRF крутить систему відліку, а не створює тягу. Тягу
            # створюють ЛОПАТІ. Якщо зона не накриває геометрію, повітря
            # просто закрутиться, тяги не буде — а розрахунок пройде без
            # жодної скарги, і це виглядатиме як «гвинт не тягне».
            touches_body = all(hi > self.bbox_min[i] and lo < self.bbox_max[i]
                               for i, (lo, hi) in enumerate(zip(zlo, zhi)))
            if not touches_body:
                out.append(("bad", (
                    tr("Zone {name} does not cover any geometry. MRF "
                       "does not create thrust by itself — the blades "
                       "inside the zone create it. An empty zone will "
                       "only swirl the air, and it will look like “the "
                       "propeller does not pull”, though there will be "
                       "no error anywhere.", name=z.name))))

            # Кінець лопаті легко переганяє звук навіть тоді, коли сам літак
            # летить повільно. Нестислива постановка цього не бачить.
            tip = z.tip_speed
            ma_tip = phys.mach(tip, self.flow.sound_speed)
            if ma_tip > 0.9 and not self.flow.compressible:
                out.append(("bad", (
                    tr("The blade tip moves at {tip:.0f} m/s — that is M "
                       "= {ma_tip:.2f}. An incompressible setup gives "
                       "nonsense here, even though the aircraft flies "
                       "slowly. Switch the flow mode to compressible or "
                       "reduce the rpm.", tip=tip, ma_tip=ma_tip))))
            elif ma_tip > 0.6 and not self.flow.compressible:
                out.append(("warn", (
                    tr("The blade tip moves at {tip:.0f} m/s (M = "
                       "{ma_tip:.2f}), though the freestream is only M = "
                       "{mach:.2f}. Compressibility on the blade is "
                       "already noticeable — the result will be "
                       "optimistic.", tip=tip, ma_tip=ma_tip, mach=self.flow.mach))))

            # РЕЖИМ ГВИНТА — найтихіша з усіх бід у цьому списку.
            #
            # Решта або не дає порахувати, або видно одразу. А тут кейс
            # зійдеться, сили порахуються, звіт вийде охайний — і в ньому
            # буде великий опір. Нікому й на думку не спаде, що майже
            # половину того опору дали гвинти, які замість тяги працюють
            # вітряком. Помилка прийде як результат.
            #
            # Спіймалося на чотиримоторному Body68: J = 7.3 при кроку 0.56,
            # і чотири гвинти дали 0.459 з сумарних 1.005 коефіцієнта
            # опору. Числа були правильні; правильним був і висновок, що
            # апарат жахливо тупий. Неправильною була постановка.
            j = z.advance_ratio(self.flow.u_inf)
            if math.isfinite(j) and j > ADVANCE_RATIO_WARN:
                ang = z.inflow_angle(self.flow.u_inf)
                dia = 2.0 * z.blade_radius
                need = (60.0 * self.flow.u_inf / (ADVANCE_RATIO_NORMAL * dia)
                        if dia > 0 else 0.0)
                # ОКРУГЛЮЄМО ДО СОТЕНЬ, і не лише заради охайності.
                #
                # Це підказка порядку величини, а не уставка: писати
                # «32710 об/хв» означає обіцяти точність, якої тут нема.
                # А ще чотири однакові гвинти міряються з геометрії
                # НЕЗАЛЕЖНО, і їхні радіуси різняться на соті міліметра —
                # без округлення чотири однакові застороги виходять
                # чотирма різними рядками й перестають складатися в одну.
                need = round(need / 100.0) * 100.0
                # Розряди через нерозривний пробіл: «32 700» читається, а
                # «32700» — ні. Складати це просто в рядку повідомлення не
                # можна: `.replace(",", ...)` зачепив би й коми речення.
                need_txt = f"{need:,.0f}".replace(",", " ")
                out.append((
                    "bad" if j > ADVANCE_RATIO_BAD else "warn",
                    tr("Zone {name}: advance ratio J = {j:.1f} at "
                       "{u_inf:.0f} m/s and {rpm:.0f} rpm (propeller "
                       "{dia} in diameter). The flow meets the blade tip "
                       "at {ang:.0f}° to the plane of rotation, so the "
                       "blade could pull only if it were twisted more "
                       "steeply than this angle. There will be no thrust "
                       "here but BRAKING — and it will come not as an "
                       "error but as extra drag in the report. To reach "
                       "a working J ≈ {ADVANCE_RATIO_NORMAL:g}, you need "
                       "about {need_txt} rpm — or a lower speed.", name=z.name, j=j, u_inf=self.flow.u_inf, rpm=abs(z.rpm), dia=phys.fmt_len(dia), ang=ang, ADVANCE_RATIO_NORMAL=ADVANCE_RATIO_NORMAL, need_txt=need_txt)))

            local = self.local_cell_at(z.centre)
            if local > 0 and z.radius < local * 4:
                out.append(("warn", (
                    tr("Zone {name} with radius {radius} at a cell of "
                       "{local} — fewer than four cells fall on it. "
                       "Rotation is not resolved on such a mesh.", name=z.name, radius=phys.fmt_len(z.radius), local=phys.fmt_len(local)))))
        return out

    def patch_names(self) -> list[str]:
        names = ["inlet", "outlet", "farfield"]
        if self.domain.ground:
            names.append("ground")
        names.extend(b.name for b in self.surface_patches)
        return names

    def default_box(self, factor: float = 1.6, level: int = 2,
                    wake: float = 4.0) -> RefinementBox:
        """Коробка навколо тіла зі слідом — стандартний стартовий рефайнмент."""
        L = self.body_length
        cx, cy, cz = self.centre
        hx = self.body_length * factor / 2
        hy = self.body_span * factor / 2
        hz = self.body_height * factor / 2
        # мінімальний запас, щоб тонкі тіла (крило) не отримали пласку коробку
        pad = 0.15 * L
        hy = max(hy, pad)
        hz = max(hz, pad)
        return RefinementBox(
            name="nearBody",
            min=(cx - hx, cy - hy, cz - hz),
            max=(cx + hx + wake * L, cy + hy, cz + hz),
            level=level,
            source="body",
        )

    def zone_box(self, zone: "RotatingZone", pad: float = 1.18,
                 wake: float = 0.5, level: int = 4) -> RefinementBox:
        """Коробка навколо обертової зони — там, де живе струмінь.

        Поверхневе згущення саме собою обʼєму не розвʼязує: воно спадає
        сходинками від лопаті, і вже за кілька сантиметрів комірка
        повертається до фонової. А закрутка й приріст швидкості живуть
        саме в обʼємі диска й одразу за ним.

        Слід дописується ПОЗАДУ по осі, а не навколо: струмінь іде туди, і
        симетрична коробка витратила б половину комірок на порожнє повітря
        перед гвинтом.
        """
        a = [abs(v) for v in zone.axis]
        i = a.index(max(a))
        half = zone.length / 2.0
        r = zone.radius * pad
        lo, hi = [], []
        for k in range(3):
            if k == i:
                lo.append(zone.centre[k] - half * (1.0 + pad))
                hi.append(zone.centre[k] + half * (1.0 + pad)
                          + zone.length * wake)
            else:
                lo.append(zone.centre[k] - r)
                hi.append(zone.centre[k] + r)
        return RefinementBox(name=f"zone_{zone.name}"[:60],
                             min=tuple(lo), max=tuple(hi), level=level,
                             source="zone")

    def sync_zone_boxes(self, level: int = 4, pad: float = 1.18,
                        wake: float = 0.5) -> None:
        """Перебудувати коробки зон під поточні зони.

        Координати беруться наново — гвинт могли посунути, повернути чи
        перечитати в інших одиницях, і коробка, що лишилася на старому
        місці, згущувала б порожнє повітря.

        А от РІВЕНЬ, поставлений руками, переживає перебудову: інакше
        кожен порух сцени повертав би його до типового, і правити його
        було б неможливо. Коробки зон, яких більше немає, прибираються —
        згущення навколо знятого гвинта нікому не потрібне.
        """
        fresh = [self.zone_box(z, pad=pad, wake=wake, level=level)
                 for z in self.rotating]
        mine = {b.name for b in fresh}
        # Витісняємо за ІМЕНЕМ, а не за походженням. Коробка з тим самим
        # іменем — це та сама коробка, хоч би ким вона була позначена;
        # поруч у списку вони дали б snappyHexMesh два однакові записи
        # refinementRegions. Саме так і сталося на проєкті, збереженому до
        # появи позначки: чотири коробки зон лежали в ньому як «руками», і
        # перебудова додала до них четвірку своїх.
        was = {b.name: b for b in self.mesh.boxes if b.name in mine}
        # Лишаємо чуже, але не СИРІТ. Коробка, позначена як зонна, чиєї
        # зони вже немає, згущувала б порожнє повітря там, звідки гвинт
        # прибрали, — і мовчки, бо помилкою це ніде не вважається.
        others = [b for b in self.mesh.boxes
                  if b.name not in mine and b.source != "zone"]
        for box in fresh:
            old = was.get(box.name)
            if old is not None and old.level_by_hand:
                box.level, box.level_by_hand = old.level, True
        self.mesh.boxes = others + fresh

    def box_named(self, name: str) -> RefinementBox | None:
        """Коробка за іменем, або None. Шукаємо саме так, а не за номером:
        порядок у списку — не контракт."""
        for b in self.mesh.boxes:
            if b.name == name:
                return b
        return None

    def shell_ladder(self) -> list[tuple[float, int]]:
        """Сходинки оболонки: [(відстань у метрах, рівень), ...].

        Читається так: ближче ніж d1 від поверхні — рівень L1, ближче d2 —
        L2, і так далі. Відстані ростуть, рівні спадають: інакше
        snappyHexMesh словник не прийме.

        Сходинок кілька, а не одна, і кожна вдвічі товща за попередню. На
        різкому стрибку рівня рішення відбивається, тому перехід від густої
        сітки до фонової розтягується.

        Порожній список означає «оболонки немає»: тоді у словник не піде
        нічого зайвого й він лишиться таким, як був.
        """
        if not self.mesh.shell_on:
            return []
        steps = max(int(self.mesh.shell_steps), 1)
        top = int(self.mesh.shell_level) or self.mesh.surface_level_max + 1
        reach = self.mesh.shell_reach
        if reach <= 0:
            # Типово беремо так, щоб найтонша сходинка вийшла завтовшки з
            # кілька поверхневих комірок. Тонше не має сенсу: сходинка стає
            # вужчою за саму комірку й не потрапляє ні в що.
            reach = self.surface_cell * 4 * (2 ** (steps - 1))
        out: list[tuple[float, int]] = []
        for i in range(steps):
            lvl = top - i
            if lvl < 1:
                break
            out.append((reach / (2 ** (steps - 1 - i)), lvl))
        return out

    def refine_only_levels(self) -> list[tuple[str, list[tuple[float, int]]]]:
        """Поверхні, які згущують, але патчами не стають: (імʼя, сходинки).

        Навітряна тут перша, бо будується з геометрії й потоку. Далі —
        те, що приїхало ззовні: зона з дешевого прогону, ручна зона,
        будь-що інше.
        """
        out: list[tuple[str, list[tuple[float, int]]]] = []
        wind = self.windward_ladder()
        if wind:
            out.append(("windward", wind))
        for rs in self.mesh.refine_surfaces:
            if rs.levels:
                out.append((rs.name, list(rs.levels)))
        return out

    def refine_only_surfaces(self) -> list[tuple[str, str]]:
        """Ті самі поверхні, але (імʼя, файл) — для секції geometry."""
        out: list[tuple[str, str]] = []
        if self.windward_ladder():
            out.append(("windward", WINDWARD_STL))
        for rs in self.mesh.refine_surfaces:
            if rs.levels:
                out.append((rs.name, rs.stl_file))
        return out

    def windward_ladder(self) -> list[tuple[float, int]]:
        """Сходинки навітряної зони — ті самі правила, що й в оболонки.

        Типово вона вдвічі товща й на рівень густіша за загальну оболонку:
        інакше вона нічого б не додала, а лише повторила б її.
        """
        if not self.mesh.windward_on:
            return []
        base = self.shell_ladder()
        top = int(self.mesh.windward_level) or (
            (base[0][1] if base else self.mesh.surface_level_max) + 1)
        reach = self.mesh.windward_reach
        if reach <= 0:
            reach = (base[-1][0] * 2 if base else self.surface_cell * 16)
        steps = max(int(self.mesh.shell_steps), 1)
        out: list[tuple[float, int]] = []
        for i in range(steps):
            lvl = top - i
            if lvl < 1:
                break
            out.append((reach / (2 ** (steps - 1 - i)), lvl))
        return out

    def shell_volume(self) -> float:
        """Скільки обʼєму займе оболонка — щоб порівняти з коробкою.

        Груба оцінка «площа поверхні на товщину»: для опуклого тіла вона
        трохи занижує, для тонкого крила майже точна. Для порівняння з
        коробкою цього досить.
        """
        ladder = self.shell_ladder()
        if not ladder or self.surface_area <= 0:
            return 0.0
        return self.surface_area * ladder[-1][0]

    def box_params(self, box: RefinementBox) -> tuple[float, float, int]:
        """Обернене до default_box: (запас, довжина сліду, рівень) з коробки.

        Потрібне, щоб відкритий збережений кейс показував ті самі числа, з
        яких коробку колись зробили: у маніфесті лежать уже абсолютні
        координати, а панель «Сітка» перед кожним записом перебудовує коробку
        з власних полів. Не відновивши поля, ми б затерли збережену коробку
        типовою — мовчки й на етапі запису, коли цього вже ніхто не бачить.

        Рахується з поточного габариту: у відкритого кейсу це той самий STL,
        з якого коробку й будували.
        """
        L = self.body_length
        cx = self.centre[0]
        hx = max(cx - box.min[0], 0.0)
        if L <= 0:
            return (1.6, 4.0, int(box.level))
        return (2.0 * hx / L, max((box.max[0] - cx - hx) / L, 0.0),
                int(box.level))


# ==========================================================================
# blockMesh
# ==========================================================================

def block_mesh_dict(cfg: CaseCfg) -> str:
    x0, x1, y0, y1, z0, z1 = cfg.bounds
    nx, ny, nz = cfg.block_divisions

    verts = [
        (x0, y0, z0), (x1, y0, z0), (x1, y1, z0), (x0, y1, z0),
        (x0, y0, z1), (x1, y0, z1), (x1, y1, z1), (x0, y1, z1),
    ]
    v = "\n".join("    " + _vec(p) for p in verts)

    # Порядок вершин кожної грані дає нормаль ЗА правилом правої руки назовні
    # домену — цього вимагає blockMesh.
    faces = {
        "xmin": "(0 4 7 3)",
        "xmax": "(1 2 6 5)",
        "ymin": "(0 1 5 4)",
        "ymax": "(3 7 6 2)",
        "zmin": "(0 3 2 1)",
        "zmax": "(4 5 6 7)",
    }

    far = [faces["ymin"], faces["ymax"], faces["zmax"]]
    boundary = [
        ("inlet", "patch", [faces["xmin"]]),
        ("outlet", "patch", [faces["xmax"]]),
    ]
    if cfg.domain.ground:
        boundary.append(("ground", "wall", [faces["zmin"]]))
    else:
        far.append(faces["zmin"])
    boundary.append(("farfield", "patch", far))

    parts = []
    for name, ptype, flist in boundary:
        faces_txt = "\n".join("            " + f for f in flist)
        parts.append(
            f"    {name}\n    {{\n        type {ptype};\n"
            f"        faces\n        (\n{faces_txt}\n        );\n    }}"
        )

    return (
        _head("dictionary", "blockMeshDict", "system")
        + "scale   1;\n\n"
        + f"vertices\n(\n{v}\n);\n\n"
        + f"blocks\n(\n    hex (0 1 2 3 4 5 6 7) ({nx} {ny} {nz}) simpleGrading (1 1 1)\n);\n\n"
        + "edges\n(\n);\n\n"
        + "boundary\n(\n" + "\n\n".join(parts) + "\n);\n\n"
        + "mergePatchPairs\n(\n);\n"
        + FOOTER
    )


# ==========================================================================
# snappyHexMesh
# ==========================================================================

def snappy_dict(cfg: CaseCfg) -> str:
    m = cfg.mesh
    stl = cfg.stl_file

    # Кожна частина тіла — окрема поверхня з власним іменем. Без роздільних
    # патчів список має рівно один запис, і текст виходить той самий, що й
    # раніше.
    geom = [
        f"    {b.stl_file}\n    {{\n        type triSurfaceMesh;\n"
        f"        name {b.name};\n    }}"
        for b in cfg.surface_patches
    ]
    for b in m.boxes:
        geom.append(
            f"    {b.name}\n    {{\n        type   searchableBox;\n"
            f"        min    {_vec(b.min)};\n        max    {_vec(b.max)};\n    }}"
        )
    # Циліндр згущення навколо кожного диска: трохи ширший за гвинт і на
    # товщину диска довший у кожен бік, щоб і сам шар, і перехід за ним
    # лягли на дрібну сітку.
    disk_regions = [d for d in cfg.disks_on if d.refine]
    for d in disk_regions:
        a = _unit(d.axis)
        half = cfg.disk_thickness(d) * 1.5
        p1 = tuple(c - x * half for c, x in zip(d.centre, a))
        p2 = tuple(c + x * half for c, x in zip(d.centre, a))
        geom.append(
            f"    {disk_region(d.name)}\n    {{\n        type   searchableCylinder;\n"
            f"        point1 {_vec(p1)};\n        point2 {_vec(p2)};\n"
            f"        radius {d.radius * 1.1:.6g};\n    }}")
    # Навітряна поверхня йде в geometry, але НЕ в refinementSurfaces: вона
    # потрібна лише як мірка відстані, патчем ставати не повинна. Інакше
    # snappy розрізав би тіло на дві границі й сили порахувалися б окремо
    # по кожній — а це зовсім не те, чого просили.
    for name, stl in cfg.refine_only_surfaces():
        geom.append(
            f"    {stl}\n    {{\n        type triSurfaceMesh;\n"
            f"        name {name};\n    }}")

    zones = [
        f"        {b.name}\n        {{\n            mode    inside;\n"
        f"            levels  ((1e15 {b.level}));\n        }}"
        for b in m.boxes
    ]
    zones += [
        f"        {disk_region(d.name)}\n        {{\n            mode    inside;\n"
        f"            levels  ((1e15 {cfg.disk_level(d)}));\n        }}"
        for d in disk_regions
    ]
    # Оболонка вішається на саму поверхню тіла: у geometry вона вже лежить
    # під своїм імʼям, тому окремого тіла заводити не треба. З роздільними
    # патчами оболонка йде на кожен — інакше крило отримало б згущення, а
    # фюзеляж ні, і на стику вийшов би стрибок.
    ladder = cfg.shell_ladder()
    if ladder:
        pairs = " ".join(f"({d:.6g} {lvl})" for d, lvl in ladder)
        for b in cfg.body_patches:
            zones.append(
                f"        {b.name}\n        {{\n"
                f"            mode    distance;\n"
                f"            levels  ({pairs});\n        }}")
    for name, levels in cfg.refine_only_levels():
        pairs = " ".join(f"({d:.6g} {lvl})" for d, lvl in levels)
        zones.append(
            f"        {name}\n        {{\n"
            f"            mode    distance;\n"
            f"            levels  ({pairs});\n        }}")
    # Портал границею не стає, але сітка біля нього має бути дрібна: на
    # ній лежить шар джерел, і саме з нього народжується струмінь. Тому
    # він згущує обʼєм довкола себе на відстань кількох своїх комірок.
    if cfg.portal_mode:
        for b in cfg.port_patches:
            lvl = cfg.level_of(b)
            level = lvl[1] if lvl else m.surface_level_max
            reach = 4.0 * cfg.portal_cell(b)
            zones.append(
                f"        {b.name}\n        {{\n"
                f"            mode    distance;\n"
                f"            levels  (({reach:.6g} {level}));\n        }}")
    regions = "\n".join(zones) or ""

    leak = ""
    if m.leak_detection:
        leak = (
            f"\n    // Якщо потік прорветься крізь дірку в STL назовні домену,\n"
            f"    // snappy напише шлях витоку в postProcessing/leakPath.\n"
            f"    locationsOutsideMesh (({_vec(cfg.location_outside_mesh)[1:-1]}));\n"
        )

    layers = ""
    if m.add_layers:
        layers = "".join(
            f'        "{b.name}.*"\n        {{\n'
            f"            nSurfaceLayers {m.n_layers};\n        }}\n"
            for b in cfg.body_patches
        )

    # Кромки йдуть за тілом: якщо частині дано власний рівень поверхні,
    # тримати її кромки на спільному означало б згладити саме те, заради
    # чого рівень і піднімали.
    features = "".join(
        '        {\n            file "'
        + b.stl_file.rsplit(".", 1)[0] + '.eMesh";\n'
        f"            level {cfg.level_of(b)[1] if cfg.level_of(b) else m.feature_level};\n"
        "        }\n"
        for b in cfg.mesh_surfaces
    )
    # Рівень беремо ВЛАСНИЙ, якщо частина його має. Без цього дрібна
    # деталь на великому тілі лишається тоншою за комірку: лопать
    # 10-дюймового гвинта 2.4 мм проти метрового літака.
    #
    # Кришка двигуна — не стінка, а отвір, тому `patch`. Тип тут не
    # формальність: на `wall` OpenFOAM рахує відстань до стінки й
    # пристінні функції, і струмінь «прилипав» би до власного сопла.
    surfaces = "".join(
        f"        {b.name}\n        {{\n"
        f"            level ({(cfg.level_of(b) or (m.surface_level_min, m.surface_level_max))[0]}"
        f" {(cfg.level_of(b) or (m.surface_level_min, m.surface_level_max))[1]});\n"
        f"            patchInfo {{ type {'patch' if b.role in PORT_ROLES else 'wall'}; }}\n"
        "        }\n"
        for b in cfg.mesh_surfaces
    )

    return (
        _head("dictionary", "snappyHexMeshDict", "system")
        + f"castellatedMesh true;\nsnap            true;\naddLayers       "
        f"{'true' if m.add_layers else 'false'};\n\n"
        + "geometry\n{\n" + "\n\n".join(geom) + "\n}\n\n"
        + f"""castellatedMeshControls
{{
    maxLocalCells       {m.max_local_cells};
    maxGlobalCells      {m.max_global_cells};
    minRefinementCells  10;
    maxLoadUnbalance    0.10;
    nCellsBetweenLevels {m.n_cells_between_levels};

    features
    (
{features}    );

    refinementSurfaces
    {{
{surfaces}    }}

    resolveFeatureAngle {m.resolve_feature_angle};

    refinementRegions
    {{
{regions}
    }}

    // Точка в потоці. Усе, що з нею не звʼязане (зокрема нутрощі тіла),
    // snappy викидає — саме так тіло вирізається з домену.
    locationInMesh {_vec(cfg.location_in_mesh)};
{leak}
    allowFreeStandingZoneFaces true;
}}

snapControls
{{
    nSmoothPatch         3;
    tolerance            2.0;
    nSolveIter           30;
    nRelaxIter           5;
    nFeatureSnapIter     {m.snap_feature_iter};
    implicitFeatureSnap  {'false' if m.explicit_feature_snap else 'true'};
    explicitFeatureSnap  {'true' if m.explicit_feature_snap else 'false'};
    multiRegionFeatureSnap false;
}}

addLayersControls
{{
    relativeSizes    true;

    layers
    {{
{layers}    }}

    expansionRatio        {m.expansion};
    finalLayerThickness   {m.final_layer_rel};
    minThickness          {m.min_thickness_rel};
    nGrow                 0;

    featureAngle          {m.layer_feature_angle};
    slipFeatureAngle      30;
    nRelaxIter            {m.layer_n_relax_iter};
    nSmoothSurfaceNormals {m.n_smooth_surface_normals};
    nSmoothNormals        {m.n_smooth_normals};
    nSmoothThickness      {m.layer_n_smooth_thickness};
    maxFaceThicknessRatio 0.5;
    maxThicknessToMedialRatio {m.max_thickness_medial};
    minMedialAxisAngle    {m.layer_min_medial_angle};
    nBufferCellsNoExtrude 0;
    nLayerIter            50;
    nRelaxedIter          20;
}}

meshQualityControls
{{
    #include "meshQualityDict"

    // ЗАПАСНИЙ НАБІР КРИТЕРІЇВ, без якого прогін падає.
    //
    // Вище стоїть `nRelaxedIter 20`: коли шари не вкладаються у звичайні
    // критерії якості, snappy пробує ще двадцять разів із послабленими.
    // Шукає він їх саме тут, у підсловнику `relaxed`.
    //
    // У типовому `etc/caseDicts/meshQualityDict` від OpenFOAM цього
    // підсловника НЕМАЄ — перевірено grep'ом, нуль згадок. Тобто
    // `nRelaxedIter` без цього блоку — це прохання піти по набір, якого
    // не існує:
    //
    //     Entry 'relaxed' not found in dictionary
    //     "system/snappyHexMeshDict/meshQualityControls"
    //
    // Найгірше в цій ваді те, КОЛИ вона спрацьовує. Доки шари лягають з
    // першого разу, її не видно взагалі: усі попередні прогони цього
    // проєкту пройшли. Вона чекає на складну геометрію — і валить сітку
    // через кілька хвилин після початку, на стадії шарів, тобто вже
    // витративши час на розрізання й пригонку.
    //
    // Послаблюємо лише неортогональність: саме вона й заважає шарам у
    // вузьких місцях, а решта критеріїв лишається строгою.
    relaxed
    {{
        maxNonOrtho 75;
    }}
}}

writeFlags ( scalarLevels layerSets layerFields );

mergeTolerance 1e-6;
"""
        + FOOTER
    )


def mesh_quality_dict(cfg: CaseCfg) -> str:
    return (
        _head("dictionary", "meshQualityDict", "system")
        + '#includeEtc "caseDicts/meshQualityDict"\n\n'
        + "nSmoothScale   4;\nerrorReduction 0.75;\n"
        + FOOTER
    )


def surface_features_dict(cfg: CaseCfg) -> str:
    """Кромки — по запису на кожен STL.

    Це той словник, який найлегше забути при роздільних патчах: усе решта
    вже вміло список, а тут лишався один файл, і surfaceFeatureExtract падав
    ще до blockMesh — на те, що названого файлу в кейсі просто немає.
    """
    return (
        _head("dictionary", "surfaceFeatureExtractDict", "system")
        + "\n".join(_one_surface_features(cfg, b.stl_file)
                    for b in cfg.mesh_surfaces)
        + FOOTER
    )


def _one_surface_features(cfg: CaseCfg, stl_file: str) -> str:
    return f"""{stl_file}
{{
    extractionMethod    extractFromSurface;

    // На фасетованій геометрії малий кут перетворює кожен шов між
    // трикутниками на "кромку". 150 — розумний старт, для органіки
    // (крило, фюзеляж) піднімайте до 160-170.
    //
    // Сучасні версії читають includedAngle з верхнього рівня, старіші (до
    // OpenFOAM 5) — з extractFromSurfaceCoeffs. Дублюємо: зайві записи в
    // словнику OpenFOAM ігнорує, а так дикт читається обома.
    includedAngle       {cfg.mesh.included_angle};

    extractFromSurfaceCoeffs
    {{
        includedAngle   {cfg.mesh.included_angle};
    }}

    subsetFeatures
    {{
        nonManifoldEdges    no;
        openEdges           yes;
    }}

    writeObj                yes;
}}
"""


# ==========================================================================
# constant/
# ==========================================================================

def transport_properties(cfg: CaseCfg) -> str:
    return (
        _head("dictionary", "transportProperties", "constant")
        + "transportModel  Newtonian;\n\n"
        + f"nu              [0 2 -1 0 0 0 0] {cfg.flow.nu:.6g};\n"
        + FOOTER
    )


def thermophysical_properties(cfg: CaseCfg) -> str:
    """Досконалий газ + Сазерленд — стандартний набір для повітря.

    `sensibleInternalEnergy`, а не ентальпія: тоді рівняння енергії зветься
    `e`, і саме під цим іменем його чекають схеми `div(phi,e)` й
    residualControl. Переплутати тут означає отримати «keyword div(phi,h) is
    undefined» посеред прогону.
    """
    return (
        _head("dictionary", "thermophysicalProperties", "constant")
        + f"""thermoType
{{
    type            hePsiThermo;
    mixture         pureMixture;
    transport       sutherland;
    thermo          hConst;
    equationOfState perfectGas;
    specie          specie;
    energy          sensibleInternalEnergy;
}}

mixture
{{
    specie
    {{
        molWeight   {phys.AIR_MOL_WEIGHT:.6g};
    }}
    thermodynamics
    {{
        Cp          {phys.AIR_CP:.6g};
        Hf          0;
    }}
    transport
    {{
        As          {phys.SUTHERLAND_AS:.6g};
        Ts          {phys.SUTHERLAND_TS:.6g};
    }}
}}
"""
        + FOOTER
    )


def dynamic_mesh_dict_refine(cfg: CaseCfg) -> str:
    """Рідна адаптація: комірки діляться під час прогону.

    Механізм дає OpenFOAM, показник — ми. У словнику лише називається поле
    (`adaptErr`), а звідки воно береться, OpenFOAM не знає: його рахує
    функція, дописана в controlDict, і саме там уся суть.

    Вилка `lowerRefineLevel..upperRefineLevel` читається так: ділиться те,
    що потрапило МІЖ ними. Показник у нас нормований на одиницю, тому
    нижня межа — це просто «частка від найгіршої комірки».

    `nBufferLayers` розмазує межу зони: без нього дрібні комірки
    закінчувалися б стрибком, а на стрибку рівня рішення відбивається.
    """
    a = cfg.adapt
    return (
        _head("dictionary", "dynamicMeshDict", "constant")
        + f"""dynamicFvMesh   dynamicRefineFvMesh;

// Як часто дивитися, що ділити. Кожна перевірка — перебудова топології
// й перерозподіл між процесами, тому надто часто дорого.
refineInterval  {max(int(a.interval), 1)};

// Поле-показник. Його рахує функція adaptErr із controlDict.
field           adaptErr;

// Ділимо те, що потрапило у вилку.
//
// Числа тут СТАЛІ, і це навмисно. Показник сам рахує поріг так, щоб за
// ним опинилася задана частка комірок ({a.fraction:.0%}), і масштабує поле так,
// щоб різ ліг рівно на 0.5. Коли поріг стояв тут, він нічого не обіцяв:
// на 0.1 від максимуму випадало 36% комірок замість 10%.
lowerRefineLevel 0.5;
upperRefineLevel 10;

// Нижче цього комірку можна склеїти назад, коли потік звідти пішов.
// Не 0.5, щоб між поділом і склеюванням був люфт: інакше комірки на
// самій межі ділилися б і склеювалися щоперевірки.
unrefineLevel   0.2;

// Скільки шарів навколо відібраного теж поділити.
nBufferLayers   2;

// СТЕЛЯ РІВНЯ комірки, а не «скільки разів іще поділити».
//
// У коді OpenFOAM умова відбору звучить так:
//     cellLevel[celli] < maxRefinement
// Тобто це абсолютний рівень, до якого дозволено дійти, і комірка,
// яка вже на ньому, не ділиться, хоч би показник і кричав.
//
// Різниця не косметична. Ця сітка вже має рівень {cfg.finest_level()},
// і якби тут стояло просто «{max(int(a.max_level), 1)}» — тобто на скільки
// ділити, — жодна комірка біля тіла не пройшла б умову. Показник же
// вказує саме туди. У журналі це виглядало як «Selected 0 cells for
// refinement» і читалося як «нема чого ділити».
//
// Тому: найдрібніший рівень сітки ПЛЮС дозволена глибина.
//     {cfg.finest_level()} + {max(int(a.max_level), 1)} = {cfg.finest_level() + max(int(a.max_level), 1)}
maxRefinement   {cfg.finest_level() + max(int(a.max_level), 1)};

// Остання межа — але МʼЯКА, і це виміряно, а не вичитано.
//
// На перевірці стеля стояла 1 200 000, а сітка стала на 1 461 595 —
// тобто на 22% вище. Це не вада OpenFOAM, а наслідок того, як він
// рахує запас (selectRefineCells):
//
//     nTotToRefine = (maxCells - nTotalCells) / 7
//
// далі він перебирає РІВНІ й додає до набору ВСІ комірки поточного
// рівня, а вже потім дивиться, чи не перебрав. Один рівень може дати
// більше за весь запас. Понад те, consistentRefinement дописує сусідів
// заради правила 2:1 УЖЕ ПІСЛЯ цієї перевірки.
//
// Практичний висновок: беріть стелю з запасом чверті від того, що
// справді вміщається в памʼять.
maxCells        {int(a.max_cells)};

// Потоки після зміни топології треба перебудувати.
correctFluxes
(
    (phi none)
    (phi_0 none)
    (rhoPhi none)
    (nHatf none)
    (ghf none)
);

dumpLevel       false;
"""
        + FOOTER
    )


def adapt_indicator_function(cfg: CaseCfg) -> str:
    """Функція, що рахує показник адаптації просто в кейсі.

    Чому не готове поле. OpenFOAM уміє писати `grad(p)`, але саме градієнт
    для адаптації НЕ ГОДИТЬСЯ: він найбільший там, де комірки вже
    найдрібніші — біля стінки. Згущення за ним псує пристінний шар, і на
    перевірці це дало стрибок y+ у сім разів і підйомну силу на 43%
    меншу.

    Тому тут показник — градієнт, ПОМНОЖЕНИЙ НА РОЗМІР КОМІРКИ. Це вже не
    крутизна поля, а оцінка похибки на комірці: дрібна комірка має малу
    похибку навіть при великому градієнті.

    Друге поле — завихреність. У сліді тиск рівний, градієнт тиску там
    сліпий, а саме слід і треба ловити.
    """
    if not cfg.adapt.on:
        return ""
    curl = cfg.adapt.use_curl
    guard = cfg.adapt_wall_guard
    # ПРИСТІННИЙ ПОЯС І ЧЕСНА ЧАСТКА — ЛИШЕ ДЛЯ РІДНОГО РЕЖИМУ.
    #
    # Спокуса ввімкнути їх усюди велика, і вона хибна.
    #
    # Пояс існує тому, що hexRef8 не вміє ділити пригнані до поверхні
    # комірки ВЖЕ ІСНУЮЧОЇ сітки. У перебудові сітка будується НАНОВО, і
    # snappyHexMesh стінку згущувати вміє — обмеження там просто немає.
    # Заглушити показник біля стінки означало б відібрати в перебудови те,
    # що вона вміє, заради чужої вади.
    #
    # Частка так само: перебудова читає `adaptErr` власним відбором, коли
    # будує карту, і той відбір відкалібровано на полі, нормованому на
    # максимум. Перемасштабувати поле під потреби рідного режиму — це
    # мовчки зрушити те, що вже поміряно на трьох кутах.
    #
    # Тому перебудова лишається байт у байт такою, як була.
    native = cfg.adapt.native
    band = f"""            // ПРИСТІННИЙ ПОЯС: там показник мовчить.
            //
            // hexRef8 ділить лише шестигранники, а пригнані до поверхні й
            // обкладені шарами комірки ними не є. Сам OpenFOAM їх
            // позначає й з відбору викидає, але потім дописує сусідів
            // заради правила 2:1, уже не питаючи про захист, — і падає:
            //
            //     cell 12604 of level 5 uses more than 8 points of equal
            //     or lower level          (hexRef8.C, 3763)
            //
            // Ділити там і не треба: згущення пристінних комірок ламає
            // пограншар, і це коштувало 43% підйомної сили.
            const volScalarField& yw = wallDist::New(mesh()).y();
            forAll(e, i)
            {{
                if (yw[i] < {guard:.6g})
                {{
                    e[i] = 0.0;
                }}
            }}

            // ЧАСТКА КОМІРОК, А НЕ ПОРІГ НА ПОЛІ.
            //
            // Просити «десяту частину» й діставати третину — це не
            // налаштування, а лотерея: при сталому порозі 0.1 на
            // нормованому полі вище за нього опинилося 51600 комірок зі
            // 141806, тобто 36%.
            //
            // Тому поріг рахуємо тут, гістограмою, і масштабуємо поле
            // так, щоб різ ліг рівно на 0.5. У словнику тоді стоїть
            // стала, і вона означає те саме за будь-якого розвʼязку.
            const label nBin = 400;
            labelList hist(nBin, Zero);
            forAll(e, i)
            {{
                label b = min(label(e[i] * nBin), nBin - 1);
                hist[b]++;
            }}
            Pstream::listCombineAllGather(hist, plusEqOp<label>());
            const label nTot = returnReduce(mesh().nCells(), sumOp<label>());
            const label want = max(label({cfg.adapt.fraction:.6g} * nTot), 1);
            label acc = 0;
            label cut = nBin - 1;
            for (label b = nBin - 1; b >= 0; --b)
            {{
                acc += hist[b];
                if (acc >= want)
                {{
                    cut = b;
                    break;
                }}
            }}
            const scalar thr = max(scalar(cut) / scalar(nBin), 1e-6);
            forAll(e, i)
            {{
                e[i] = min(e[i] * 0.5 / thr, 1.0);
            }}

""" if native else ""
    # Звіт показника про себе. Без нього «Selected 0 cells for refinement»
    # лишається загадкою: сам dynamicRefineFvMesh друкує, скільки
    # ВІДІБРАНО, але не з чого вибирав. Саме цей рядок і показав, що
    # показник у порядку, а стеля рівня — ні.
    report = f"""
            label loud = 0;
            label muted = 0;
            forAll(e, i)
            {{
                if (e[i] >= 0.5)
                {{
                    loud++;
                }}
                if (yw[i] < {guard:.6g})
                {{
                    muted++;
                }}
            }}
            reduce(loud, sumOp<label>());
            reduce(muted, sumOp<label>());
            Info<< "adaptErr: за порогом " << loud << " з " << nTot
                << " (" << 100.0 * loud / max(nTot, 1) << "%, просили "
                << 100.0 * {cfg.adapt.fraction:.6g} << "%)"
                << ", пристінний пояс мовчить у " << muted
                << ", різ на " << thr << endl;
""" if native else ""
    head = """        codeInclude
        #{
            #include "wallDist.H"
        #};

        // Явно, а не покладаючись на те, що коджений обʼєкт і так із чимось
        // злінкується: wallDist живе в libfiniteVolume, і мовчазний збій
        // складання коштував би цілого прогону.
        codeOptions
        #{
            -I$(LIB_SRC)/finiteVolume/lnInclude -I$(LIB_SRC)/meshTools/lnInclude
        #};

        codeLibs
        #{
            -lfiniteVolume -lmeshTools
        #};

""" if native else ""
    return f"""
    adaptErr
    {{
        type            coded;
        libs            (utilityFunctionObjects);
        name            adaptErr;
        writeControl    timeStep;
        writeInterval   {max(int(cfg.adapt.interval), 1)};
{head}        codeExecute
        #{{
            const volScalarField& p =
                mesh().lookupObject<volScalarField>("p");
            volScalarField* fp =
                mesh().getObjectPtr<volScalarField>("adaptErr");
            if (!fp)
            {{
                fp = new volScalarField
                (
                    IOobject
                    (
                        "adaptErr",
                        mesh().time().timeName(),
                        mesh(),
                        IOobject::NO_READ,
                        IOobject::AUTO_WRITE
                    ),
                    mesh(),
                    dimensionedScalar("zero", dimless, 0.0),
                    zeroGradientFvPatchScalarField::typeName
                );
                fp->store();
            }}
            volScalarField& err = *fp;

            // Розмір комірки — явним циклом. pow(mesh().V(), 1.0/3.0) не
            // компілюється: V() несе розмірність, і корінь кубічний із
            // неї безглуздий. Перша спроба лягла саме тут.
            scalarField h(mesh().nCells());
            forAll(h, i)
            {{
                h[i] = Foam::cbrt(mesh().V()[i]);
            }}

            scalarField e(mag(fvc::grad(p))().primitiveField() * h);
            e /= max(gMax(e), SMALL);
{'''
            const volVectorField& U =
                mesh().lookupObject<volVectorField>("U");
            scalarField eU(mag(fvc::curl(U))().primitiveField() * h);
            eU /= max(gMax(eU), SMALL);
            e = max(e, eU);
''' if curl else ''}
{band}            err.primitiveFieldRef() = e;
            err.correctBoundaryConditions();
{report}        #}};
    }}
"""


def dynamic_mesh_dict(cfg: CaseCfg) -> str:
    """Як саме сітка рухається.

    `displacementLaplacian` розносить зміщення границі по домену рівнянням
    Лапласа. Дифузія обернена до відстані від тіла: біля нього вузли їдуть
    майже разом із поверхнею, далі щоразу менше, а біля дальнього поля
    стоять. Саме це не дає коміркам вивернутися.

    `inverseDistance` бере відстань до перелічених границь — тобто до
    самого тіла, а не до чогось абстрактного. З роздільними патчами треба
    перелічити ВСІ: інакше рух рознесеться відносно однієї частини, а решта
    поїде разом із доменом і сітку розірве.
    """
    names = " ".join(b.name for b in cfg.body_patches)
    return (
        _head("dictionary", "dynamicMeshDict", "constant")
        + f"""dynamicFvMesh   dynamicMotionSolverFvMesh;

motionSolverLibs (fvMotionSolvers);

motionSolver    displacementLaplacian;

displacementLaplacianCoeffs
{{
    diffusivity     inverseDistance ({names});
}}
"""
        + FOOTER
    )


def field_point_displacement(cfg: CaseCfg) -> str:
    """Зміщення вузлів. Рух задається саме тут, на границі тіла.

    `angularOscillatingDisplacement` хоче кругову частоту в рад/с і кути в
    радіанах — у налаштуваннях вони в герцах і градусах, бо так їх називає
    людина, а переводить конфіг.

    Поле `p0` не задаємо: без нього OpenFOAM бере поточні координати вузлів
    патча за початкові, а це рівно те, що треба, бо сітка стартує з
    нерухомого стану.
    """
    m = cfg.motion
    ax, org = m.axis, cfg.motion_pivot
    body = _bc(
        "type            angularOscillatingDisplacement;",
        f"axis            {_vec(ax)};",
        f"origin          {_vec(org)};",
        f"angle0          {m.angle0_rad:.8g};",
        f"amplitude       {m.amplitude_rad:.8g};",
        f"omega           {m.omega:.8g};",
        "value           uniform (0 0 0);",
    )
    still = _bc("type            fixedValue;",
                "value           uniform (0 0 0);")
    bcs = {"inlet": still, "outlet": still, "farfield": still}
    if cfg.domain.ground:
        bcs["ground"] = still

    # РУХАЄТЬСЯ НЕ ВСЕ, А ПРИЗНАЧЕНЕ.
    #
    # Коли частинам роздано ролі, коливається лише та, що позначена
    # «рухомою»: елерон ходить, фюзеляж стоїть. Решта дістає нерухому
    # умову — інакше вони поїхали б разом із ним, і сенс поділу зник би.
    #
    # Якщо ролей ніхто не роздавав, рухається ВСЕ ТІЛО, як і раніше. Це не
    # люб'язність до старих кейсів, а умова того, щоб їхні словники
    # лишилися байт у байт тими самими — і це перевіряється дифом.
    movers = [b for b in cfg.body_patches if b.role == "motion"]
    if not movers:
        movers = list(cfg.body_patches)
    moving = {b.name for b in movers}
    for b in cfg.body_patches:
        bcs[b.name] = body if b.name in moving else still
    bcs.update(_port_bcs(cfg, "still"))
    return _field("pointVectorField", "pointDisplacement",
                  "[0 1 0 0 0 0 0]", "(0 0 0)", bcs)


def turbulence_properties(cfg: CaseCfg) -> str:
    model = cfg.flow.turbulence
    delta = LES_MODELS.get(model)
    if delta is None:
        return (
            _head("dictionary", "turbulenceProperties", "constant")
            + "simulationType  RAS;\n\nRAS\n{\n"
            + f"    RASModel        {model};\n"
            + "    turbulence      on;\n    printCoeffs     on;\n}\n"
            + FOOTER
        )
    # delta — це розмір фільтра, тобто межа між вихорами, які рахуються, і
    # тими, що моделюються. cubeRootVol бере корінь кубічний з обʼєму
    # комірки. IDDESDelta додає до цього відстань до стінки: без неї
    # перемикання RANS на LES відбувається не там, де треба, і на межі
    # виникає «сіра зона» — ділянка, де вже не RANS, але ще не LES.
    if delta == "IDDESDelta":
        coeffs = ("    IDDESDeltaCoeffs\n    {\n"
                  "        deltaCoeff      2;\n"
                  "        hmax            maxDeltaxyz;\n"
                  "        maxDeltaxyzCoeffs\n        {\n"
                  "            deltaCoeff      1;\n"
                  "        }\n    }\n")
    else:
        coeffs = ("    cubeRootVolCoeffs\n    {\n"
                  "        deltaCoeff      1;\n    }\n")
    return (
        _head("dictionary", "turbulenceProperties", "constant")
        + "simulationType  LES;\n\nLES\n{\n"
        + f"    LESModel        {model};\n"
        + "    turbulence      on;\n    printCoeffs     on;\n"
        + f"    delta           {delta};\n\n"
        + coeffs
        + "}\n"
        + FOOTER
    )


# ==========================================================================
# 0.orig/
# ==========================================================================

def _field(cls: str, obj: str, dims: str, internal: str, bcs: dict[str, str]) -> str:
    body = "\n".join(f"    {name}\n    {{\n{txt}    }}\n" for name, txt in bcs.items())
    # Без setConstraintTypes паралельний прогін падає одразу після
    # decomposePar: у декомпозованій сітці зʼявляються службові границі
    # procBoundaryXtoY, а в полі для них немає запису, і читання валиться з
    # "Cannot find patchField entry for procBoundary2to0". Цей інклюд задає
    # типові записи для всіх constraint-границь (processor, symmetry, empty…),
    # і OpenFOAM підбирає їх за ТИПОМ границі, а не за назвою.
    return (
        _head(cls, obj, "0")
        + f"dimensions      {dims};\n\ninternalField   uniform {internal};\n\n"
        + "boundaryField\n{\n"
        + '    #includeEtc "caseDicts/setConstraintTypes"\n\n'
        + body + "}\n"
        + FOOTER
    )


def _bc(*lines: str) -> str:
    return "".join(f"        {ln}\n" for ln in lines)


def _port_bcs(cfg: CaseCfg, kind: str, value: str = "0") -> dict[str, str]:
    """Умови на кришках двигунів для поля `kind`.

    Двигун тримає ВИТРАТУ, а не швидкість і не тиск. Тому на обох кришках
    швидкість — `flowRate*Velocity`, а тиск вільний (`zeroGradient`): саме
    він і підлаштовується, щоб крізь отвір пройшло рівно стільки повітря.

    Повітрозабірник — вихід із домену (`flowRateOutletVelocity`): повітря
    тут іде з потоку геть, у двигун. Сопло — вхід (`flowRateInletVelocity`):
    звідси воно повертається струменем. Через це скалярні поля на intake
    `inletOutlet` (що прийшло, те й пішло), а на nozzle задані: струмінь
    має свою температуру й свою, значно більшу, турбулентність.

    У нестисливому OpenFOAM рахує об'єми, тому витрата там об'ємна:
    ṁ / ρ∞. У стисливому — масова, і густину на старті треба назвати
    (`rhoInlet` / `rhoOutlet`): поля rho ще немає, а витрату вже питають.
    """
    out: dict[str, str] = {}
    # Портали — не границі: записів у полях для них немає.
    if not cfg.engines or cfg.portal_mode:
        return out
    points = cfg.engine_points()
    flow = cfg.flow
    for b in cfg.port_patches:
        e = cfg.engine_of(b.name)
        if e is None:
            continue
        pt = points[e.name]
        nozzle = b.name == e.nozzle
        if nozzle:
            mdot = pt.mdot_nozzle
            rho = pt.rho_exit
        else:
            i = e.intakes.index(b.name)
            mdot = pt.mdot_intakes[i] if i < len(pt.mdot_intakes) else 0.0
            rho = flow.rho
        if kind == "U":
            bc = "flowRateInletVelocity" if nozzle else "flowRateOutletVelocity"
            if flow.compressible:
                key = "rhoInlet" if nozzle else "rhoOutlet"
                out[b.name] = _bc(f"type            {bc};",
                                  f"massFlowRate    {mdot:.8g};",
                                  "rho             rho;",
                                  f"{key:<16}{rho:.8g};",
                                  "value           uniform (0 0 0);")
            else:
                out[b.name] = _bc(f"type            {bc};",
                                  f"volumetricFlowRate {mdot / flow.rho:.8g};",
                                  "value           uniform (0 0 0);")
        elif kind == "p":
            out[b.name] = _bc("type            zeroGradient;")
        elif kind == "T":
            out[b.name] = (_bc("type            fixedValue;",
                               f"value           uniform {pt.t_exit:.6g};")
                           if nozzle else
                           _bc("type            inletOutlet;",
                               f"inletValue      uniform {value};",
                               f"value           uniform {value};"))
        elif kind == "calculated":
            out[b.name] = _bc("type            calculated;",
                              "value           uniform 0;")
        elif kind == "still":
            out[b.name] = _bc("type            fixedValue;",
                              "value           uniform (0 0 0);")
        elif nozzle and kind == "k":
            out[b.name] = _bc("type            turbulentIntensityKineticEnergyInlet;",
                              f"intensity       {NOZZLE_INTENSITY:.6g};",
                              f"value           uniform {value};")
        elif nozzle and kind in ("omega", "epsilon"):
            bc = ("turbulentMixingLengthFrequencyInlet" if kind == "omega"
                  else "turbulentMixingLengthDissipationRateInlet")
            mix = max(e.nozzle_dh, 1e-6) * NOZZLE_MIXING_FRACTION
            out[b.name] = _bc(f"type            {bc};",
                              f"mixingLength    {mix:.6g};",
                              f"value           uniform {value};")
        elif nozzle:
            # nuTilda, gammaInt, ReThetat — тим самим числом, що й набігаючий:
            # окремої оцінки для струменя тут немає.
            out[b.name] = _bc("type            fixedValue;",
                              f"value           uniform {value};")
        else:
            out[b.name] = _bc("type            inletOutlet;",
                              f"inletValue      uniform {value};",
                              f"value           uniform {value};")
    return out


def _outer(cfg: CaseCfg, kind: str, value: str) -> dict[str, str]:
    """Умови на зовнішніх межах: inlet, outlet, farfield.

    kind — 'U', 'p' або 'scalar'.

    Режим freestream (типовий) ставить freestream* на ВСІ зовнішні межі.
    Ці умови самі вирішують, де вхід, а де вихід, за знаком потоку крізь
    грань, тому працюють за будь-якого кута атаки й ковзання.

    Режим tunnel — класична аеротруба: фіксований вхід, фіксований тиск на
    виході, slip з боків. Він КОРЕКТНИЙ ЛИШЕ ПРИ НУЛЬОВИХ КУТАХ: slip
    занулює нормальну до межі складову швидкості, тобто зрізає вертикальну
    складову набігаючого потоку і фактично вбиває кут атаки. На крилі при
    5° це занижує Cl приблизно на порядок.

    На НАДЗВУКУ обидва режими неправильні, тому там вибір робиться не за
    налаштуванням, а за самим режимом течії — див. нижче.
    """
    if cfg.flow.supersonic:
        # Надзвук міняє не значення на межах, а саму їхню природу.
        #
        # Проти потоку інформація тут НЕ ЙДЕ: усі характеристики на вході
        # спрямовані всередину, тому на вході фіксується все — швидкість,
        # тиск, температура. На виході навпаки: всі характеристики йдуть
        # назовні, і задавати там нема чого взагалі.
        #
        # З боків — теж нульовий градієнт, і це головне. Якщо тримати там
        # значення набігаючого потоку, ударна хвиля, дійшовши до межі,
        # ВІДБИВАЄТЬСЯ назад у домен і повертається на тіло. Ніякого
        # повідомлення при цьому не буде — просто числа стануть не ті.
        #
        # Що ставилося тут раніше — `freestream` на всіх межах — на
        # надзвуку не просто неточне, а згубне: ця умова перемикається між
        # заданим значенням і нульовим градієнтом за знаком потоку крізь
        # грань, і на стрибку ущільнення видає стан, якого не буває.
        # Сазерленд бере з такої температури корінь, і прогін гине з
        # SIGFPE усередині термодинаміки — без жодного натяку на причину.
        # Саме так упав перший прогін: 608 кроків, а потім стек виклику
        # в libfluidThermophysicalModels.
        zero = _bc("type            zeroGradient;")
        outlet = zero
        if kind == "p":
            # ТИСК на виході — окремо, і це третя пастка надзвуку.
            #
            # Нульовий градієнт на виході правильний, поки витікання
            # НАДЗВУКОВЕ: тоді всі характеристики йдуть назовні й рівень
            # тиску приходить зсередини. Але за ТУПИМ тілом слід
            # дозвуковий, і якщо він доходить до виходу таким, рівень
            # тиску не закріплений нічим і повзе.
            #
            # Поміряно на кулі. На виході домену 100% комірок виявилися
            # дозвуковими (M від 0.09 до 0.64), а тиск там доріс до
            # 434 кПа проти набігаючих 101. Опір при цьому пройшов
            # +0.64 -> +0.32 -> +0.09 -> -0.33: тиск за кулею став вищим
            # за тиск перед нею, і тіло «потягнуло вперед».
            #
            # waveTransmissive — межа, що не відбиває: збурення крізь неї
            # виходять, а рівень тиску м'яко тягнеться до набігаючого на
            # довжині lInf. Для надзвукового витікання вона поводиться як
            # нульовий градієнт, тож правильний випадок не псується.
            p_inf = float(getattr(cfg.flow, "p_inf", 101325.0) or 101325.0)
            l_inf = max(cfg.domain_size[0], 1e-3)
            outlet = _bc(
                "type            waveTransmissive;",
                "field           p;",
                "psi             thermo:psi;",
                "gamma           1.4;",
                f"fieldInf        {p_inf:.6g};",
                f"lInf            {l_inf:.6g};",
                f"value           uniform {value};")
        return {
            "inlet": _bc("type            fixedValue;",
                         f"value           uniform {value};"),
            "outlet": outlet,
            "farfield": zero,
        }

    if cfg.flow.farfield_mode == "freestream":
        if kind == "U":
            bc = _bc("type            freestreamVelocity;",
                     f"freestreamValue uniform {value};")
        elif kind == "p":
            bc = _bc("type            freestreamPressure;",
                     f"freestreamValue uniform {value};")
        else:
            bc = _bc("type            freestream;",
                     f"freestreamValue uniform {value};")
        return {"inlet": bc, "outlet": bc, "farfield": bc}

    # --- аеротруба ---
    if kind == "U":
        return {
            "inlet": _bc("type            fixedValue;",
                         f"value           uniform {value};"),
            "outlet": _bc("type            inletOutlet;",
                          "inletValue      uniform (0 0 0);",
                          f"value           uniform {value};"),
            "farfield": _bc("type            slip;"),
        }
    if kind == "p":
        # На виході тримаємо ТЕ САМЕ значення, що прийшло сюди аргументом:
        # у нестисливому це нуль, у стисливому — абсолютний тиск у паскалях.
        # Жорсткий нуль тут означав би вакуум на виході з домену.
        return {
            "inlet": _bc("type            zeroGradient;"),
            "outlet": _bc("type            fixedValue;",
                          f"value           uniform {value};"),
            "farfield": _bc("type            slip;"),
        }
    return {
        "inlet": _bc("type            fixedValue;",
                     f"value           uniform {value};"),
        "outlet": _bc("type            inletOutlet;",
                      f"inletValue      uniform {value};",
                      f"value           uniform {value};"),
        "farfield": _bc("type            slip;"),
    }


def field_U(cfg: CaseCfg) -> str:
    u = _vec(cfg.flow.velocity)
    bcs = _outer(cfg, "U", u)
    # ПОЧАТКОВЕ поле на надзвуку — нерухоме, а не набігаюче. Це не
    # налаштування зручності, а умова того, що задача взагалі порахується.
    #
    # Коли в початковому полі стоїть набігаюча швидкість, вона стоїть і
    # ОДРАЗУ ЗА ТІЛОМ. На стінці прилипання, тобто нуль, а на відстані
    # міліметра — шістсот метрів за секунду геть. За першу ж мить база
    # спорожняється, а зайти туди нема чому: справжня зворотна течія
    # встановлюється за десятки прольотів тіла, не за мікросекунду.
    # Виходить вакуум, і розрахунок гине не від нестійкості схеми, а від
    # того, що ми попросили порахувати неможливе.
    #
    # Перевірено на кулі, де інших підозр немає взагалі. З набігаючим
    # полем: тиск у базі 97 735 -> 5 024 Па за чотирнадцять кроків,
    # температура 285 -> 72 К, загибель на 7e-07 с. З нерухомим — та сама
    # сітка, ті самі схеми — пройшло 1.7e-05 с і далі, тобто вісімнадцятеро
    # довше, і без жодної ознаки біди.
    #
    # Ціна: надзвуковий вхід мусить спершу заповнити домен. Це
    # враховується в guard_regime, який перевіряє, чи вистачає на це часу.
    internal = "(0 0 0)" if cfg.flow.supersonic else u
    if cfg.domain.ground:
        bcs["ground"] = _bc("type            noSlip;")
    # На РУХОМІЙ стінці noSlip неправильний, і помиляється він тихо.
    #
    # noSlip каже лише «швидкість на стінці нуль». Для нерухомої цього
    # досить. Коли стінка рухається сама, крізь її грані йде потік від
    # самого руху — і його треба врахувати в рівнянні нерозривності.
    # movingWallVelocity це й робить: віднімає швидкість руху грані, тож
    # прилипання лишається, а баланс маси сходиться.
    #
    # Ціна помилки: прогін не падає одразу, а розходиться. У перевірці
    # pimpleFoam рахував сімнадцять секунд і вилетів на арифметичній
    # помилці всередині рівняння турбулентності — бо саме туди приходить
    # накопичений дисбаланс.
    wall = ("type            movingWallVelocity;",
            "value           uniform (0 0 0);") if cfg.motion.on else (
        "type            noSlip;",)
    for _b in cfg.body_patches:
        bcs[_b.name] = _bc(*wall)
    bcs.update(_port_bcs(cfg, "U"))
    return _field("volVectorField", "U", "[0 1 -1 0 0 0 0]", internal, bcs)


def field_p(cfg: CaseCfg) -> str:
    """У simpleFoam тиск кінематичний (p/rho, м²/с²) і відлічується від нуля.
    У rhoSimpleFoam він АБСОЛЮТНИЙ, у паскалях — там ним годується рівняння
    стану, і нуль означав би вакуум."""
    if cfg.flow.compressible:
        value, dims = f"{cfg.flow.p_inf:.6g}", "[1 -1 -2 0 0 0 0]"
    else:
        value, dims = "0", "[0 2 -2 0 0 0 0]"
    bcs = _outer(cfg, "p", value)
    if cfg.domain.ground:
        bcs["ground"] = _bc("type            zeroGradient;")
    for _b in cfg.body_patches:
        bcs[_b.name] = _bc("type            zeroGradient;")
    bcs.update(_port_bcs(cfg, "p"))
    return _field("volScalarField", "p", dims, value, bcs)


def field_T(cfg: CaseCfg) -> str:
    """Температура. На обшивці zeroGradient — стінка адіабатична.

    Це припущення, а не істина: у справжньому польоті обшивка гріється від
    тертя й віддає тепло назовні. Для опору й піднімальної сили на дозвукових
    режимах різниця нехтовна, а задавати теплообмін тут нема з чого.
    """
    value = f"{cfg.flow.t_inf:.6g}"
    bcs = _outer(cfg, "scalar", value)
    wall = _bc("type            zeroGradient;")
    if cfg.domain.ground:
        bcs["ground"] = wall
    for _b in cfg.body_patches:
        bcs[_b.name] = wall
    bcs.update(_port_bcs(cfg, "T", value))
    return _field("volScalarField", "T", "[0 0 0 1 0 0 0]", value, bcs)


def field_alphat(cfg: CaseCfg) -> str:
    """Турбулентна теплопровідність. Рахується моделлю, тому на межах
    calculated, а на стінках — своя пристінна функція."""
    calc = _bc("type            calculated;", "value           uniform 0;")
    bcs = {"inlet": calc, "outlet": calc, "farfield": calc}
    wall = _bc("type            compressible::alphatWallFunction;",
               "Prt             0.85;",
               "value           uniform 0;")
    if cfg.domain.ground:
        bcs["ground"] = wall
    for _b in cfg.body_patches:
        bcs[_b.name] = wall
    bcs.update(_port_bcs(cfg, "calculated"))
    return _field("volScalarField", "alphat", "[1 -1 -1 0 0 0 0]", "0", bcs)


def _wall_treatment(cfg: CaseCfg) -> str:
    """spalding / wallfn / lowre — what the walls get (see fs_yplus)."""
    import fs_yplus
    return fs_yplus.cfg_treatment(cfg)


def field_k(cfg: CaseCfg) -> str:
    k = f"{cfg.turbulence_inlet().k:.6g}"
    bcs = _outer(cfg, "scalar", k)
    if _wall_treatment(cfg) == "lowre":
        # The layer is resolved to the wall: k goes to zero there. A very
        # small value instead of 0 keeps k/omega and log(k) finite.
        wall = _bc("type            fixedValue;", "value           uniform 1e-10;")
    else:
        wall = _bc("type            kqRWallFunction;",
                   f"value           uniform {k};")
    if cfg.domain.ground:
        bcs["ground"] = wall
    for _b in cfg.body_patches:
        bcs[_b.name] = wall
    bcs.update(_port_bcs(cfg, "k", k))
    return _field("volScalarField", "k", "[0 2 -2 0 0 0 0]", k, bcs)


def field_omega(cfg: CaseCfg) -> str:
    w = f"{cfg.turbulence_inlet().omega:.6g}"
    bcs = _outer(cfg, "scalar", w)
    wall = _bc("type            omegaWallFunction;", f"value           uniform {w};")
    if cfg.domain.ground:
        bcs["ground"] = wall
    for _b in cfg.body_patches:
        bcs[_b.name] = wall
    bcs.update(_port_bcs(cfg, "omega", w))
    return _field("volScalarField", "omega", "[0 0 -1 0 0 0 0]", w, bcs)


def field_epsilon(cfg: CaseCfg) -> str:
    e = f"{cfg.turbulence_inlet().epsilon:.6g}"
    bcs = _outer(cfg, "scalar", e)
    wall = _bc("type            epsilonWallFunction;", f"value           uniform {e};")
    if cfg.domain.ground:
        bcs["ground"] = wall
    for _b in cfg.body_patches:
        bcs[_b.name] = wall
    bcs.update(_port_bcs(cfg, "epsilon", e))
    return _field("volScalarField", "epsilon", "[0 2 -3 0 0 0 0]", e, bcs)


def field_nuTilda(cfg: CaseCfg) -> str:
    # Стандартний набігаючий рівень для Spalart-Allmaras: nuTilda = 3..5 * nu
    nt = f"{3.0 * cfg.flow.nu:.6g}"
    bcs = _outer(cfg, "scalar", nt)
    wall = _bc("type            fixedValue;", "value           uniform 0;")
    if cfg.domain.ground:
        bcs["ground"] = wall
    for _b in cfg.body_patches:
        bcs[_b.name] = wall
    bcs.update(_port_bcs(cfg, "nuTilda", nt))
    return _field("volScalarField", "nuTilda", "[0 2 -1 0 0 0 0]", nt, bcs)


def field_gamma_int(cfg: CaseCfg) -> str:
    """Перемежовуваність: 0 — ламінарно, 1 — повністю турбулентно.

    На стінці zeroGradient, а не нуль: перехід визначає сама модель за
    станом пограншару, і примусовий нуль на обшивці підмінив би її рішення.
    """
    bcs = _outer(cfg, "scalar", "1")
    wall = _bc("type            zeroGradient;")
    if cfg.domain.ground:
        bcs["ground"] = wall
    for _b in cfg.body_patches:
        bcs[_b.name] = wall
    bcs.update(_port_bcs(cfg, "gammaInt", "1"))
    return _field("volScalarField", "gammaInt", "[0 0 0 0 0 0 0]", "1", bcs)


def field_re_theta_t(cfg: CaseCfg) -> str:
    """ReΘt набігаючого потоку — з інтенсивності турбулентності."""
    value = f"{phys.re_theta_t_freestream(cfg.flow.intensity):.6g}"
    bcs = _outer(cfg, "scalar", value)
    wall = _bc("type            zeroGradient;")
    if cfg.domain.ground:
        bcs["ground"] = wall
    for _b in cfg.body_patches:
        bcs[_b.name] = wall
    bcs.update(_port_bcs(cfg, "ReThetat", value))
    return _field("volScalarField", "ReThetat", "[0 0 0 0 0 0 0]", value, bcs)


def field_nut(cfg: CaseCfg) -> str:
    calc = _bc("type            calculated;", "value           uniform 0;")
    bcs = {"inlet": calc, "outlet": calc, "farfield": calc}
    # nutUSpalding працює в усьому діапазоні y+ (від ~1 до ~300), тому це
    # найбезпечніший вибір, поки реальний y+ невідомий. The other two come
    # from the «Обробка стінки» list (fs_yplus.TREATMENT_LABELS).
    wall_type = {"spalding": "nutUSpaldingWallFunction",
                 "wallfn": "nutkWallFunction",
                 "lowre": "nutLowReWallFunction"}[_wall_treatment(cfg)]
    wall = _bc(f"type            {wall_type};",
               "value           uniform 0;")
    if cfg.domain.ground:
        bcs["ground"] = wall
    for _b in cfg.body_patches:
        bcs[_b.name] = wall
    bcs.update(_port_bcs(cfg, "calculated"))
    return _field("volScalarField", "nut", "[0 2 -1 0 0 0 0]", "0", bcs)


def turbulence_fields(cfg: CaseCfg) -> dict[str, str]:
    model = cfg.flow.turbulence
    out = {"nut": field_nut(cfg)}
    if model == "kOmegaSSTLM":
        out["k"] = field_k(cfg)
        out["omega"] = field_omega(cfg)
        out["gammaInt"] = field_gamma_int(cfg)
        out["ReThetat"] = field_re_theta_t(cfg)
    elif model == "kOmegaSST":
        out["k"] = field_k(cfg)
        out["omega"] = field_omega(cfg)
    elif model == "kEpsilon":
        out["k"] = field_k(cfg)
        out["epsilon"] = field_epsilon(cfg)
    elif model in ("SpalartAllmaras", "SpalartAllmarasDDES"):
        out["nuTilda"] = field_nuTilda(cfg)
    elif model in ("kOmegaSSTDDES", "kOmegaSSTIDDES"):
        # DES над kOmegaSST живе на тих самих полях, що й сама kOmegaSST:
        # перемикається лише те, як рахується масштаб довжини.
        out["k"] = field_k(cfg)
        out["omega"] = field_omega(cfg)
    elif model == "kEqn":
        out["k"] = field_k(cfg)
    # WALE не потребує нічого, крім nut: вʼязкість у неї алгебраїчна.
    return out


def turbulence_field_names(model: str) -> list[str]:
    return {
        "kOmegaSST": ["k", "omega"],
        # Порядок важить: цей список склеюється в регулярку для fvSolution,
        # і саме з нього беруться solvers, residualControl і релаксація.
        "kOmegaSSTLM": ["k", "omega", "gammaInt", "ReThetat"],
        "kEpsilon": ["k", "epsilon"],
        "SpalartAllmaras": ["nuTilda"],
        "kOmegaSSTDDES": ["k", "omega"],
        "kOmegaSSTIDDES": ["k", "omega"],
        "SpalartAllmarasDDES": ["nuTilda"],
        "kEqn": ["k"],
        # WALE не розвʼязує нічого: порожній список тут правильний, а не
        # прогалина. Місця, які з цього списку будують регулярки, мусять
        # уміти його обробити — див. fv_solution.
        "WALE": [],
    }[model]


# ==========================================================================
# system/
# ==========================================================================

def _safe_write_interval(n_iterations: int, requested: int) -> int:
    """Інтервал запису, який гарантовано влучає в останню ітерацію.

    При `writeControl timeStep` OpenFOAM пише лише коли номер ітерації кратний
    інтервалу, і фінальний крок сам НЕ записує. Якщо інтервал не ділить
    endTime, результат останніх ітерацій просто зникає — при достроковій
    збіжності рятує writeAndEnd(), а при виході по endTime вже ні.

    Тому беремо найбільший дільник endTime, не більший за бажаний інтервал:
    намір користувача зберігається, а останній крок гарантовано на місці.
    """
    n = max(1, int(n_iterations))
    want = max(1, min(int(requested), n))
    for step in range(want, 0, -1):
        if n % step == 0:
            return step
    return n


def control_dict(cfg: CaseCfg) -> str:
    s = cfg.solver
    f = cfg.flow
    write_interval = _safe_write_interval(s.n_iterations, s.write_interval)
    # У стисливому густина — поле, і сили рахуються з нього. rhoInf тут лишає
    # сенс опорної величини для самих коефіцієнтів (0.5*rho*U²*A), тому
    # пишеться в обох режимах, але значить різне.
    rho_line = ("        rho             rho;\n" if f.compressible
                else "        rho             rhoInf;\n")
    all_patches = " ".join(b.name for b in cfg.body_patches)
    # Показник адаптації йде в той самий блок функцій. Порожній
    # рядок, коли адаптації немає, — щоб словник лишався тим самим.
    adapt_fn = adapt_indicator_function(cfg)
    # Малі поверхні на кожен запис — щоб проміжний результат можна було
    # подивитися, поки прогін іде (fs_live). Стоять ПІСЛЯ yPlus і
    # wallShearStress: функції виконуються в порядку словника.
    import fs_live
    live_fn = fs_live.functions(cfg)
    # Сумарні сили лишаються під тим самим іменем `forceCoeffs` — щоб усе,
    # що їх читає, працювало як раніше. Окремі по частинах додаються ПОРУЧ,
    # своїми функціями: інакше довелося б вибирати між «сумарно» і «по
    # частинах», а потрібні обидва.
    per_body = ""
    if len(cfg.body_patches) > 1:
        per_body = "".join(f"""
    forceCoeffs_{b.name}
    {{
        $forceCoeffs;
        patches         ({b.name});
    }}
""" for b in cfg.body_patches)
    if s.steady:
        # У стаціонарі «час» — це номер ітерації, тому крок рівно 1, а
        # запис — що стільки-то ітерацій.
        timing = (f"endTime         {max(1, s.n_iterations)};\n"
                  "deltaT          1;\n"
                  "\n"
                  "writeControl    timeStep;\n"
                  f"writeInterval   {write_interval};\n")
    else:
        # adjustableRunTime, а не runTime: інакше запис припадає на
        # найближчий крок ПІСЛЯ потрібного моменту, і зрізи виходять у
        # нерівні проміжки. З adjustableRunTime крок підтискається так, щоб
        # влучити точно, і часові теки звуться круглими числами.
        # Порталам — своя стеля кроку (див. portal_dt). Без порталів обидва
        # числа ті самі, що й були.
        cap = portal_dt(cfg)
        first = min(s.time_step, cap) if cap > 0 else s.time_step
        ceiling = (min(s.write_every / 4, cap) if cap > 0
                   else s.write_every / 4)
        timing = (f"endTime         {s.end_time:.10g};\n"
                  f"deltaT          {first:.10g};\n"
                  "\n"
                  "writeControl    adjustableRunTime;\n"
                  f"writeInterval   {s.write_every:.10g};\n"
                  "\n"
                  "adjustTimeStep  yes;\n"
                  f"maxCo           {s.max_courant:.10g};\n"
                  # Стеля на крок — щоб adjustTimeStep не розігнався на
                  # вирівняному полі так, що пропустить саме́ явище, яке
                  # заради нього й рахують. Чверть інтервалу запису дає
                  # щонайменше чотири кроки між зрізами.
                  f"maxDeltaT       {ceiling:.10g};\n")
    return (
        _head("dictionary", "controlDict", "system")
        + f"""application     {solver_name(cfg)};

startFrom       latestTime;
startTime       0;
stopAt          endTime;
{timing}purgeWrite      0;
writeFormat     {s.write_format};
writePrecision  8;
writeCompression off;

timeFormat      general;
timePrecision   6;
runTimeModifiable true;

functions
{{
    forceCoeffs
    {{
        type            forceCoeffs;
        libs            (forces);
        writeControl    timeStep;
        writeInterval   1;
        log             no;

        patches         ({all_patches});
{rho_line}        rhoInf          {f.rho:.6g};
{_pref_line(cfg)}
        magUInf         {f.u_inf:.6g};
        lRef            {f.l_ref:.6g};
        Aref            {f.a_ref:.6g};

        liftDir         {_vec(f.lift_dir)};
        dragDir         {_vec(f.drag_dir)};
        CofR            {_vec(f.cofr)};
        pitchAxis       (0 1 0);
    }}
{per_body}
    yPlus
    {{
        type            yPlus;
        libs            (fieldFunctionObjects);
        writeControl    writeTime;
    }}

    wallShearStress
    {{
        type            wallShearStress;
        libs            (fieldFunctionObjects);
        writeControl    writeTime;
        patches         ({all_patches});
    }}
{adapt_fn}{_engine_functions(cfg)}{live_fn}}}
"""
        + FOOTER
    )


def _pref_line(cfg: CaseCfg) -> str:
    """Відлік тиску для сил на стінках — лише коли тіло вже не замкнене.

    Сили рахуються інтегралом тиску по стінках. Поки тіло замкнене, сталий
    тиск довкола дає нуль: скільки тисне спереду, стільки й ззаду. Кришки
    двигуна ці стінки РОЗМИКАЮТЬ, і в стисливому, де тиск абсолютний,
    сто тисяч паскалів, помножені на площу отворів, з'являються в опорі як
    сотні ньютонів, яких немає. `pRef = p∞` віднімає цей сталий тиск.

    У нестисливому тиск і так відлічується від нуля на нескінченності, тож
    рядок там не потрібен — і не пишеться, щоб словник лишився тим самим.
    """
    if not (cfg.port_patches and cfg.flow.compressible):
        return ""
    return f"        pRef            {cfg.flow.p_inf:.6g};\n"


# Що міряємо на кожній кришці. Імʼя → (операція, поле, поле ваги).
#
# З трьох чисел складається сила двигуна (`fs_post.engine_force`):
#   ṁ          = Σ phi                — скільки пройшло;
#   імпульс    = Σ phi·U              — weightedSum зі ЗНАКОВОЮ вагою phi;
#   тиск       = Σ (p − pRef)·Sf      — функція `forces` на самій кришці.
# Знак phi — назовні з домену, тобто В ДВИГУН. Так його й читаємо.
#
# Тиск — НЕ surfaceFieldValue. Перша версія брала areaNormalIntegrate(p),
# і на справжньому прогоні (ref_caps, simpleFoam) він давав рівно нуль:
# ця операція має сенс лише для векторних полів, для скаляра мовчки пише
# 0. А тиск на кришці — половина справи: вхід, що засмоктує повітря з
# 54 м/с, стоїть у розрідженні, і саме воно тягне апарат уперед. `forces`
# рахує Σ (p − pRef)·Sf і вже множить на ρ у нестисливому.
ENGINE_PROBES = (
    ("mdot", "sum", "phi", ""),
    ("momentum", "weightedSum", "U", "phi"),
    ("pressure", "forces", "p", ""),
)


# Вимір порталу входу: середня швидкість у шарі джерел.
PORTAL_PROBE = "zoneU"


def engine_probe_name(patch: str, probe: str) -> str:
    return f"port_{patch}_{probe}"


def _engine_functions(cfg: CaseCfg) -> str:
    """Функції, що міряють потоки крізь кришки. Порожньо без двигунів."""
    if not cfg.port_patches:
        return ""
    out = []
    if cfg.portal_mode:
        # Портал — не границя, мірятиме нічого на кришці. Сила порталу —
        # це реакція його джерел: сопла задана, а входу залежить від того,
        # ЯКИЙ імпульс він забрав, тобто від середньої швидкості в шарі.
        for b in cfg.port_patches:
            if b.role != "intake":
                continue
            out.append(f"""
    {engine_probe_name(b.name, PORTAL_PROBE)}
    {{
        type            volFieldValue;
        libs            (fieldFunctionObjects);
        writeControl    timeStep;
        writeInterval   1;
        log             no;
        writeFields     false;
        regionType      cellZone;
        name            {portal_zone(b.name)};
        operation       volAverage;
        fields          (U);
    }}
""")
        return "".join(out)
    flow = cfg.flow
    rho_line = ("rho             rho;" if flow.compressible
                else "rho             rhoInf;")
    pref = (f"\n        pRef            {flow.p_inf:.6g};"
            if flow.compressible else "")
    for b in cfg.port_patches:
        for probe, op, fld, weight in ENGINE_PROBES:
            if op == "forces":
                out.append(f"""
    {engine_probe_name(b.name, probe)}
    {{
        type            forces;
        libs            (forces);
        writeControl    timeStep;
        writeInterval   1;
        log             no;
        patches         ({b.name});
        {rho_line}
        rhoInf          {flow.rho:.6g};{pref}
        CofR            {_vec(flow.cofr)};
    }}
""")
                continue
            wline = (f"        weightField     {weight};\n" if weight else "")
            out.append(f"""
    {engine_probe_name(b.name, probe)}
    {{
        type            surfaceFieldValue;
        libs            (fieldFunctionObjects);
        writeControl    timeStep;
        writeInterval   1;
        log             no;
        writeFields     false;
        regionType      patch;
        name            {b.name};
        operation       {op};
{wline}        fields          ({fld});
    }}
""")
    return "".join(out)


def fv_schemes_central(cfg: CaseCfg) -> str:
    """Схеми для rhoCentralFoam.

    Головне тут — `fluxScheme` і трійка `reconstruct`. Схема Курганова
    рахує потік крізь грань із двох реконструйованих станів, лівого й
    правого, а `vanLeer` — це обмежувач, який не дає реконструкції
    вигадати нове екстремальне значення. Саме він відповідає за те, щоб
    ударна хвиля лишилася розривом, а не обросла коливаннями.

    Для швидкості обмежувач ВЕКТОРНИЙ, vanLeerV: покомпонентне обмеження
    вектора не зберігає його напрямку, і на косому стрибку швидкість
    повертало б.

    `div(tauMC)` — єдина дивергенція, яка тут узагалі є: вʼязкі напруження.
    Конвекцію рахує fluxScheme, не divSchemes.
    """
    turb = "\n".join(
        f"    div(phi,{n})    Gauss limitedLinear 1;"
        for n in turbulence_field_names(cfg.flow.turbulence)
    )
    return (
        _head("dictionary", "fvSchemes", "system")
        + f"""fluxScheme      Kurganov;

ddtSchemes
{{
    default         Euler;
}}

gradSchemes
{{
    default         Gauss linear;
}}

divSchemes
{{
    default         none;
    div(tauMC)      Gauss linear;
{turb}
}}

laplacianSchemes
{{
    default         Gauss linear corrected;
}}

interpolationSchemes
{{
    default         linear;
    reconstruct(rho) vanLeer;
    reconstruct(U)   vanLeerV;
    reconstruct(T)   vanLeer;
}}

snGradSchemes
{{
    default         corrected;
}}

// kOmegaSST і решта моделей із пристінними функціями питають ВІДСТАНЬ ДО
// СТІНКИ, а спосіб її рахувати задається саме тут. Без цього блоку солвер
// падає аж на створенні моделі турбулентності: «Entry 'method' not found
// in dictionary fvSchemes/wallDist». Помилка приходить не з рівнянь, а зі
// словника, і саме тому її легко пропустити, копіюючи схеми з підручника
// для ламінарного надзвуку.
wallDist
{{
    method          meshWave;
}}
"""
        + FOOTER
    )


def fv_solution_central(cfg: CaseCfg) -> str:
    """Розвʼязувачі для rhoCentralFoam.

    Збережувані величини — rho, rhoU, rhoE — оновлюються ЯВНО, тому для них
    стоїть `diagonal`: це не ітераційний розвʼязувач, а просто ділення на
    діагональ. Неявно розвʼязуються лише вʼязкі члени: швидкість, енергія й
    турбулентність.

    Блоку PIMPLE немає й не повинно бути: зовнішніх ітерацій у цій схемі
    нема, крок за часом тримається малим Курантом, а не переобчисленням.
    """
    turb = turbulence_field_names(cfg.flow.turbulence)
    extra = ""
    if turb:
        extra = (f'\n    "({"|".join(turb)})"\n    {{\n'
                 "        $U;\n"
                 "        tolerance       1e-10;\n"
                 "        relTol          0;\n    }}\n")
    return (
        _head("dictionary", "fvSolution", "system")
        + """solvers
{
    "(rho|rhoU|rhoE)"
    {
        solver          diagonal;
    }

    U
    {
        solver          smoothSolver;
        smoother        GaussSeidel;
        nSweeps         2;
        tolerance       1e-09;
        relTol          0.01;
    }

    e
    {
        $U;
        tolerance       1e-10;
        relTol          0;
    }"""
        + extra.replace("{{", "{").replace("}}", "}")
        + """
}
"""
        + FOOTER
    )


def fv_schemes(cfg: CaseCfg) -> str:
    # linearUpwind — це другий порядок. На перекошених комірках він дає
    # локальні викиди, які SIMPLE підхоплює і розганяє за кілька десятків
    # ітерацій. upwind розмиває розвʼязок, зате не вибухає — саме тому це
    # правильний спосіб СТАРТУВАТИ на сітці з зауваженнями.
    if cfg.solver.steady:
        ddt = "steadyState"
        # `bounded` віднімає похибку неперервності, яка є тільки в SIMPLE:
        # там поле швидкості між ітераціями не бездивергентне. У PIMPLE воно
        # бездивергентне за побудовою, і цей модифікатор там зайвий.
        bound = "bounded "
    else:
        # Euler першого порядку — для стійкого старту; інакше backward,
        # другий порядок за часом. У нестаціонарі точність за часом і є тим,
        # заради чого його рахують, тож першим порядком весь прогін не йде.
        ddt = "Euler" if cfg.solver.robust_start else "backward"
        bound = ""
    if cfg.solver.robust_start:
        u_div = f"{bound}Gauss upwind"
        t_div = f"{bound}Gauss upwind"
    elif cfg.flow.les:
        # LUST — суміш linear і linearUpwind у пропорції 3:1. Для вихрових
        # методів це стандарт, і причина проста: linearUpwind надто
        # дисипативний. Він гасить саме ті дрібні вихори, заради яких LES і
        # запускають, і тоді дорогий розрахунок дає ту саму розмиту картину,
        # що й дешевий RANS. Чистий linear у свою чергу розхитується.
        u_div = f"{bound}Gauss LUST grad(U)"
        t_div = f"{bound}Gauss limitedLinear 1"
    else:
        u_div = f"{bound}Gauss linearUpwind limited"
        t_div = f"{bound}Gauss limitedLinear 1"
    turb_div = "\n".join(
        f"    div(phi,{n}) {t_div};"
        for n in turbulence_field_names(cfg.flow.turbulence)
    )
    # Стисливий розвʼязувач везе за собою ще чотири потоки: енергію (e),
    # кінетичну (K), її роботу (Ekp) і два члени з тиском. Пропустити хоч
    # один — і прогін падає на «keyword ... is undefined», бо default тут
    # свідомо none: краще впертися в явну відсутність схеми, ніж мовчки
    # порахувати конвекцію чимось випадковим.
    if cfg.flow.compressible:
        energy_div = (
            f"    div(phi,e)      {t_div};\n"
            f"    div(phi,K)      {t_div};\n"
            f"    div(phi,Ekp)    {t_div};\n"
            "    div(phid,p)     Gauss upwind;\n"
            f"    div((phi|interpolate(rho)),p)  {bound}Gauss upwind;\n"
        )
        if not cfg.solver.steady:
            # rhoPimpleFoam бере тиск іншим шляхом, ніж rhoSimpleFoam, і
            # вимагає ще двох схем. Додаються лише тут, а не в обидва
            # режими: зайвий запис нічому не шкодить, але стаціонарний
            # словник має лишитися рівно таким, яким був.
            energy_div += ("    div(phiv,p)     Gauss linear;\n"
                           "    div(U)          Gauss linear;\n")
        if cfg.rotating:
            # MRF у СТИСЛИВОМУ розвʼязувачі несе власний потік `phiMRF` —
            # відносний, за вирахуванням руху системи відліку. Без цієї
            # схеми rhoPimpleFoam падає на першій же ітерації:
            #
            #     Entry 'div(phiMRF,p)' not found in dictionary "divSchemes"
            #
            # Нестислива постановка цього не просить, тому вада сиділа
            # тихо, доки хтось не поставив гвинт у стисливий кейс.
            energy_div += "    div(phiMRF,p)   Gauss linear;\n"
        stress_div = "div(((rho*nuEff)*dev2(T(grad(U)))))"
    else:
        energy_div = ""
        stress_div = "div((nuEff*dev2(T(grad(U)))))"
    return (
        _head("dictionary", "fvSchemes", "system")
        + f"""ddtSchemes
{{
    default         {ddt};
}}

gradSchemes
{{
    default         Gauss linear;
    limited         cellLimited Gauss linear 1;
    grad(U)         $limited;
    grad(k)         $limited;
    grad(omega)     $limited;
    grad(epsilon)   $limited;
    grad(nuTilda)   $limited;
    grad(gammaInt)  $limited;
    grad(ReThetat)  $limited;
}}

divSchemes
{{
    default         none;
    div(phi,U)      {u_div};
{turb_div}
{energy_div}    {stress_div} Gauss linear;
}}

laplacianSchemes
{{
    default         Gauss linear corrected;
}}

interpolationSchemes
{{
    default         linear;
}}

snGradSchemes
{{
    default         corrected;
}}

wallDist
{{
    method          meshWave;
}}
"""
        + FOOTER
    )


def topo_set_dict(cfg: CaseCfg) -> str:
    """Зони комірок для пористих джерел.

    Спершу `cellSet` із коробки, потім із нього `cellZone`: fvOptions
    вибирає саме зону, а не набір. Два кроки замість одного — не примха
    OpenFOAM, а те, як воно влаштоване.
    """
    actions = []
    for z in cfg.rotating:
        p1, p2 = z.ends
        actions.append(
            f"    // {z.name}\n"
            f"    {{\n        name    {z.name}Cells;\n"
            f"        type    cellSet;\n        action  new;\n"
            f"        source  cylinderToCell;\n"
            f"        p1      {_vec(p1)};\n"
            f"        p2      {_vec(p2)};\n"
            f"        radius  {z.radius:.10g};\n    }}\n"
            f"    {{\n        name    {z.name};\n"
            f"        type    cellZoneSet;\n        action  new;\n"
            f"        source  setToCellZone;\n"
            f"        set     {z.name}Cells;\n    }}\n")
    for z in cfg.porous:
        lo, hi = z.bounds
        actions.append(
            f"    // {z.name}\n"
            f"    {{\n        name    {z.name}Cells;\n"
            f"        type    cellSet;\n        action  new;\n"
            f"        source  boxToCell;\n"
            f"        box     {_vec(lo)} {_vec(hi)};\n    }}\n"
            f"    {{\n        name    {z.name};\n"
            f"        type    cellZoneSet;\n        action  new;\n"
            f"        source  setToCellZone;\n        set     {z.name}Cells;\n"
            f"    }}\n")
    # Гвинт-диск: кільце від кока до кінця лопаті завтовшки з шар диска.
    for d in cfg.disks_on:
        a = _unit(d.axis)
        h = 0.5 * cfg.disk_thickness(d)
        p1 = tuple(c - x * h for c, x in zip(d.centre, a))
        p2 = tuple(c + x * h for c, x in zip(d.centre, a))
        zone = disk_zone(d.name)
        actions.append(
            f"    // гвинт-диск {d.name}\n"
            f"    {{\n        name    {zone}Cells;\n"
            f"        type    cellSet;\n        action  new;\n"
            f"        source  cylinderToCell;\n"
            f"        p1      {_vec(p1)};\n"
            f"        p2      {_vec(p2)};\n"
            f"        radius  {d.radius:.10g};\n"
            f"        innerRadius {d.hub:.10g};\n    }}\n"
            f"    {{\n        name    {zone};\n"
            f"        type    cellZoneSet;\n        action  new;\n"
            f"        source  setToCellZone;\n        set     {zone}Cells;\n"
            f"    }}\n")
    # Шар порталу: комірки, чиї центри всередині «плитки» — поверхні
    # порталу, видавленої на дві комірки в АКТИВНИЙ бік. Плитку пише вікно
    # (`portal_slab_file`). Розрізані комірки не беремо: інакше шар
    # залазив би на неактивний бік, і портал дув би в обидва.
    if cfg.portal_mode:
        out_pt = _vec(cfg.location_in_mesh)
        for b in cfg.port_patches:
            zone = portal_zone(b.name)
            actions.append(
                f"    // портал {b.name}\n"
                f"    {{\n        name    {zone}Cells;\n"
                f"        type    cellSet;\n        action  new;\n"
                f"        source  surfaceToCell;\n"
                f'        file    "constant/triSurface/{portal_slab_file(b.name)}";\n'
                f"        outsidePoints ({out_pt});\n"
                f"        includeCut      false;\n"
                f"        includeInside   true;\n"
                f"        includeOutside  false;\n"
                f"        nearDistance    -1;\n"
                f"        curvature       -100;\n"
                f"        useSurfaceOrientation true;\n    }}\n"
                f"    {{\n        name    {zone};\n"
                f"        type    cellZoneSet;\n        action  new;\n"
                f"        source  setToCellZone;\n        set     {zone}Cells;\n"
                f"    }}\n")
    return (
        _head("dictionary", "topoSetDict", "system")
        + "actions\n(\n" + "\n".join(actions) + ");\n"
        + FOOTER
    )


def mrf_properties(cfg: CaseCfg) -> str:
    """Обертові системи відліку.

    `nonRotatingPatches` типово порожній: усе, що потрапило в зону,
    обертається разом із нею. Для гвинта це правильно — там усередині лише
    лопаті й кок. Але якщо в зону заходить пілон чи фюзеляж, його треба
    назвати тут, інакше він теж «закрутиться», і сили на ньому вийдуть
    вигадані.
    """
    body = []
    for z in cfg.rotating:
        patches = " ".join(z.non_rotating)
        body.append(f"""{z.name}
{{
    cellZone    {z.name};
    active      yes;

    // {z.rpm:.6g} об/хв; кінець лопаті йде {z.tip_speed:.4g} м/с
    nonRotatingPatches ({patches});

    origin      {_vec(z.centre)};
    axis        {_vec(_unit(z.axis))};
    omega       {z.omega:.10g};
}}
""")
    return (
        _head("dictionary", "MRFProperties", "constant")
        + "\n".join(body)
        + FOOTER
    )


def _porous_sources(cfg: CaseCfg) -> str:
    out = []
    for i, z in enumerate(cfg.porous, 1):
        d, f = z.darcy(cfg.flow.nu, cfg.flow.rho)
        # Поперек — більший опір, щоб потік не обтікав зону боком замість
        # проходити крізь неї. Саме поперечний опір і робить із коробки
        # «радіатор», а не «місце, де трохи важче летіти».
        cross_d, cross_f = d * z.cross_factor, f * z.cross_factor
        e1, e2 = _unit(z.axis), _perpendicular(z.axis)
        out.append(f"""porosity{i}
{{
    type            explicitPorositySource;
    active          yes;

    explicitPorositySourceCoeffs
    {{
        selectionMode   cellZone;
        cellZone        {z.name};
        type            DarcyForchheimer;

        // {z.dp:.6g} Па при {z.u_ref:.6g} м/с крізь шар {z.thickness:.6g} м
        d   ({d:.6g} {cross_d:.6g} {cross_d:.6g});
        f   ({f:.6g} {cross_f:.6g} {cross_f:.6g});

        coordinateSystem
        {{
            origin  {_vec(z.centre)};
            e1      {_vec(e1)};
            e2      {_vec(e2)};
        }}
    }}
}}
""")
    return "\n".join(out)


def portal_zone(patch: str) -> str:
    return f"{patch}_portal"


def disk_zone(name: str) -> str:
    return f"{name}_disk"


def disk_region(name: str) -> str:
    return f"{name}_diskRegion"


def _disk_sources(cfg: CaseCfg) -> str:
    """Сила гвинтів-дисків: один вираз на диск, нормований на сітці.

    Вага комірки w — профіль тяги по радіусу; поза кільцем диска нуль.
    Сила на одиницю обʼєму:

        f = T·w / Σ(w·V) · a  +  Q·w / Σ(w·r·V) · t

    де a — вісь (куди диск штовхає повітря), t — дотичний одиничний
    вектор (spin·a × r̂). Суми беруться по ВСІЙ сітці, але поза диском
    w = 0, тож це суми по диску. Звідси головне: Σ f·V = T·a і
    Σ r·f_t·V = Q рівно, хоч би як диск ліг на комірки.

    `volumeMode specific` — вираз дає силу на кубометр. У нестисливому
    рівняння імпульсу кінематичне (поділене на ρ), тому і сила ділиться
    на ρ∞. У стисливому — ньютони на кубометр як є.
    """
    disks = cfg.disks_on
    if not disks:
        return ""
    rho = 1.0 if cfg.flow.compressible else float(cfg.flow.rho)
    out = []
    for d in disks:
        a = _unit(d.axis)
        h = 0.5 * cfg.disk_thickness(d)
        R, rh = float(d.radius), float(d.hub)
        span = max(R - rh, 1e-12)
        T = d.thrust_now / rho
        Q = d.torque_now / rho * (1 if d.spin >= 0 else -1)
        # ОДИН ВИРАЗ, БЕЗ `variables`. Це не стиль, а обхід: SemiImplicitSource
        # вимикає перевірку розмірностей лише на час розбору самого виразу,
        # а змінні рахує раніше (`clearVariables`), коли перевірка ще
        # ввімкнена. `pos() − vector(...)` — це метри мінус безрозмірне, і
        # rhoSimpleFoam / simpleFoam падали на першій ітерації з «Different
        # dimensions for '(a - b)'». У setExprFields, де перевіряли вираз
        # спершу, перевірку вимкнено глобально, тож там вада не видна.
        av = "vector(" + ", ".join(f"{x:.8g}" for x in a) + ")"
        dv = "(pos() - vector(" + ", ".join(f"{x:.8g}" for x in d.centre) + "))"
        xa = f"({dv} & {av})"
        rv = f"({dv} - {xa}*{av})"
        r = f"mag({rv})"
        s = f"(({r} - {rh:.8g})/{span:.8g})"
        prof = (f"{s}*{s}*sqrt(max(1 - {s}, 0))" if d.profile == "realistic"
                else "1")
        w = (f"((mag({xa}) <= {h:.8g} && {r} >= {rh:.8g} && {r} <= {R:.8g})"
             f" ? {prof} : 0)")
        expr = f"({T:.8g}*{w}/max(sum({w}*vol()), 1e-30))*{av}"
        if abs(Q) > 0:
            t = f"(({av} ^ {rv})/max({r}, 1e-12))"
            expr += (f" + ({Q:.8g}*{w}/max(sum({w}*{r}*vol()), 1e-30))*{t}")
        out.append(f"""{disk_zone(d.name)}
{{
    // гвинт-диск {d.name}: T = {d.thrust_now:.6g} Н, Q = {d.torque_now:.6g} Н·м
    type            vectorSemiImplicitSource;
    active          yes;
    selectionMode   cellZone;
    cellZone        {disk_zone(d.name)};
    volumeMode      specific;
    sources
    {{
        U
        {{
            explicit
            {{
                type        exprField;
                expression  "{expr}";
            }}
            implicit    none;
        }}
    }}
}}
""")
    return "\n".join(out)


def portal_slab_file(patch: str) -> str:
    return f"{patch}_slab.stl"


def portal_ramp(cfg: CaseCfg) -> float:
    """За скільки джерела порталів наростають від нуля до повної сили.

    Нестаціонарний — секунди: десять проходів струменя крізь свій діаметр.
    Стаціонарний — «час» це номер ітерації: п'ята частина прогону, але не
    менше 50 і не більше 300 ітерацій.

    ЧОМУ. Повну силу з першої миті шар порталу не витримує: на прогоні
    користувача (rhoPimpleFoam, турбіна 973 К, ṁ = 0.225 кг/с) сопло
    щокроку вливало в шар ~9 % його маси разом із 155 кВт тепла, вхід стільки
    ж забирав — і температура вперлася в обидві межі вже на першому кроці,
    а на тринадцятому розвʼязувач упав. Відтворено: на грубішій сітці за
    перший крок у шар уливалось 160 % його маси, обрізано 45 % комірок.
    Потоку треба дати розігнатися, поки джерела ростуть.
    """
    s = cfg.solver
    if s.steady:
        return float(min(300, max(50, int(s.n_iterations) // 5)))
    if cfg.warm_start:
        # Теплий старт уже розігнав струмінь до повної сили. Наростати
        # знову з нуля означало б на початку нестаціонару вимкнути двигун
        # у готовому полі — та сама ударна хвиля, лише навпаки.
        return 0.0
    flow = cfg.flow
    ramp = 0.0
    for e in cfg.engines:
        pt = engine_point(e, flow)
        speed = max(pt.v_exit, flow.u_inf, 1.0)
        size = max(e.nozzle_dh, 1e-6)
        ramp = max(ramp, PORTAL_RAMP_PASSES * size / speed)
    # Наростання не має з'їсти прогін: не більше чверті кінцевого часу.
    return float(min(ramp, 0.25 * s.end_time)) if ramp > 0 else 0.0


def portal_dt(cfg: CaseCfg) -> float:
    """Найбільший крок часу, який витримує шар порталу, с. 0 — обмеження немає.

    Струмінь за крок не має перелетіти більше за PORTAL_JET_CO комірки
    шару. Розвʼязувач сам цього не знає: `maxCo` рахується з ПОТОЧНОЇ
    течії, а на старті в полі швидкості струменя ще немає. На відтвореному
    збої перший крок вийшов 1 мс при набігаючих 25 м/с — і сопло за один
    крок влило в шар більше маси, ніж у ньому було: падіння на першому ж
    кроці навіть із наростанням джерел.
    """
    if not cfg.portal_mode or cfg.solver.steady:
        return 0.0
    flow = cfg.flow
    cap = 0.0
    for e in cfg.engines:
        if not (e.nozzle and e.intakes):
            continue
        pt = engine_point(e, flow)
        speed = max(pt.v_exit, max(pt.v_intakes or (0.0,)), flow.u_inf, 1.0)
        for name in (e.nozzle, *e.intakes):
            b = next((x for x in cfg.port_patches if x.name == name), None)
            if b is None:
                continue
            dt = PORTAL_JET_CO * cfg.portal_cell(b) / speed
            cap = dt if cap <= 0 else min(cap, dt)
    return cap


def _ramped(value: str, zero: str, ramp: float) -> str:
    """Function1 «від нуля до value за ramp», далі стала (clamp)."""
    if ramp <= 0:
        return value
    return f"table ((0 {zero}) ({ramp:.6g} {value}))"


def _src(field: str, explicit: str, implicit: str, ramp: float,
         zero: str = "0") -> str:
    """Один запис у `sources` semiImplicitSource, з наростанням.

    Порожнє `explicit`/`implicit` — запис пропускається: OpenFOAM вважає
    відсутнє нулем. Неявна частина завжди скалярна, тож її нуль — «0».
    """
    lines = [f"        {field}\n        {{\n"]
    if explicit:
        lines.append(f"            explicit    {_ramped(explicit, zero, ramp)};\n")
    if implicit:
        lines.append(f"            implicit    {_ramped(implicit, '0', ramp)};\n")
    lines.append("        }\n")
    return "".join(lines)


def _portal_sources(cfg: CaseCfg) -> str:
    """Джерела порталів: маса, імпульс і енергія в шарі з активного боку.

    Сопло ДОДАЄ повітря: масу ṁ, імпульс ṁ·Vₑ уздовж своєї нормалі й
    енергію, яку несе струмінь, — ентальпію й кінетичну:
    ṁ·(Cp·(Tₑ − Tref) + Vₑ²/2). Саме ентальпію, а не внутрішню енергію:
    струмінь, входячи в обʼєм, ще й проштовхує повітря перед собою.

    Вхід ЗАБИРАЄ повітря, і забирає те, що тут є, а не те, що ми задали:
    імпульс і енергія знімаються неявно, Sp = −ṁ, тобто −ṁ·U і −ṁ·e
    місцеві. До `e` ще бракує p/ρ і кінетичної частини ентальпії; їх
    знімаємо явно за набігаючим потоком, ṁ·(R·T∞ + V∞²/2). Вхід стоїть у
    майже незбуреному повітрі, і помилка тут мала, а без цього доданка
    вхід мовчки гріло б на десятки градусів.

    `volumeMode absolute`: числа — СУМИ на весь шар, OpenFOAM ділить їх
    на обʼєм зони сам. Тому скільки б комірок не потрапило в шар, маса й
    сила ті самі.

    Перевірено на rhoSimpleFoam:
    лише сопло на +0.02 кг/с — з домену виходить рівно на 0.0200 кг/с
    більше; сопло й вхід — баланс нуль.
    """
    if not cfg.portal_mode:
        return ""
    flow = cfg.flow
    r_gas = flow.p_inf / max(flow.rho * flow.t_inf, 1e-12)
    ramp = portal_ramp(cfg)
    out = []
    for e in cfg.engines:
        if not (e.nozzle and e.intakes):
            continue
        pt = engine_point(e, flow)
        n = _unit(e.nozzle_normal)
        mom = tuple(e.eta * pt.mdot_nozzle * pt.v_exit * x for x in n)
        h_jet = phys.AIR_CP * (pt.t_exit - FOAM_T_REF) + 0.5 * pt.v_exit ** 2
        zone = portal_zone(e.nozzle)
        out.append(f"""{zone}_mass
{{
    // сопло {e.nozzle}: +{pt.mdot_nozzle:.6g} кг/с, {pt.t_exit:.6g} К
    type            scalarSemiImplicitSource;
    active          yes;
    selectionMode   cellZone;
    cellZone        {zone};
    volumeMode      absolute;
    sources
    {{
{_src('rho', f'{pt.mdot_nozzle:.8g}', '', ramp)}{_src('e', f'{pt.mdot_nozzle * h_jet:.8g}', '', ramp)}
    }}
}}

{zone}_momentum
{{
    // η·ṁ·Vₑ = {e.eta * pt.mdot_nozzle * pt.v_exit:.6g} Н уздовж {_vec(n)}
    type            vectorSemiImplicitSource;
    active          yes;
    selectionMode   cellZone;
    cellZone        {zone};
    volumeMode      absolute;
    sources
    {{
{_src('U', _vec(mom), '', ramp, zero='(0 0 0)')}
    }}
}}
""")
        h_in = r_gas * flow.t_inf + 0.5 * flow.u_inf ** 2
        for name, m in zip(e.intakes, pt.mdot_intakes):
            zone = portal_zone(name)
            out.append(f"""{zone}_mass
{{
    // вхід {name}: −{m:.6g} кг/с
    type            scalarSemiImplicitSource;
    active          yes;
    selectionMode   cellZone;
    cellZone        {zone};
    volumeMode      absolute;
    sources
    {{
{_src('rho', f'{-m:.8g}', '', ramp)}{_src('e', f'{-m * h_in:.8g}', f'{-m:.8g}', ramp)}
    }}
}}

{zone}_momentum
{{
    type            vectorSemiImplicitSource;
    active          yes;
    selectionMode   cellZone;
    cellZone        {zone};
    volumeMode      absolute;
    sources
    {{
{_src('U', '', f'{-m:.8g}', ramp, zero='(0 0 0)')}
    }}
}}
""")
    return "\n".join(out)


def fv_options(cfg: CaseCfg) -> str:
    """Стеля на модуль швидкості.

    Найдешевший запобіжник від раннього вибуху: одна погана комірка дає
    локальний сплеск швидкості, той через конвекцію псує сусідів, і за кілька
    десятків ітерацій розв'язок іде в нескінченність. limitVelocity просто
    зрізає модуль, не даючи сплеску розростися, а на збіжному розв'язку не
    робить нічого — швидкості там і так нижчі за стелю.
    """
    # Стеля — від НАЙШВИДШОГО, що є в задачі: струмінь двигуна буває
    # удвічі швидший за набігаючий потік, і стеля від u∞ мовчки зрізала б
    # його у стійкому етапі.
    fastest = cfg.flow.u_inf
    for e in cfg.engines:
        try:
            fastest = max(fastest, engine_point(e, cfg.flow).v_exit)
        except (ValueError, ZeroDivisionError):
            pass
    cap = fastest * cfg.solver.velocity_cap_factor
    active = "yes" if cfg.solver.robust_start else "no"
    # Пористі джерела дописуються ПІСЛЯ стелі швидкості й лише коли зони є:
    # без них текст лишається тим самим, що й був.
    porous = (_porous_sources(cfg) + _portal_sources(cfg)
              + _disk_sources(cfg))
    return (
        _head("dictionary", "fvOptions", "constant")
        + f"""limitU
{{
    type            limitVelocity;
    active          {active};
    selectionMode   all;
    max             {cap:.6g};
}}
"""
        + _limit_temperature(cfg)
        + (("\n" + porous) if porous else "")
        + FOOTER
    )


def _limit_temperature(cfg: CaseCfg) -> str:
    """Рамка на температуру — але не всім розвʼязувачам, а тим, хто її чує.

    Історія тут у двох діях, і друга виправляє першу.

    ДІЯ ПЕРША. Надзвукові прогони гинули з арифметичною помилкою всередині
    термодинаміки: явна схема на ударній хвилі час від часу дає в одній
    комірці внутрішню енергію, якої не буває, температура з неї виходить
    відʼємна, і формула вʼязкості Сазерленда бере з неї корінь. Я додав
    `limitTemperature`. Прогін упав так само, а в журналі стояло:

        Source limitT defined for field e but never used

    rhoCentralFoam fvOptions НЕ ЗАСТОСОВУЄ. Це розвʼязувач за густиною: він
    не збирає матрицю для енергії, у яку джерело можна було б вставити, —
    рівняння інтегруються явно. Запис лишається лежати, а людина, яка його
    туди поклала, вважає себе захищеною. Тож я прибрав рамку зовсім.

    ДІЯ ДРУГА, І ТУТ Я ПОМИЛИВСЯ. Висновок був правильний РІВНО ПРО ОДИН
    розвʼязувач, а вимкнув я рамку ДЛЯ ВСІХ. rhoSimpleFoam і rhoPimpleFoam —
    тиск-орієнтовані й сегреговані, вони матрицю енергії саме що збирають.
    У джерелі OpenFOAM v2512, rhoSimpleFoam/EEqn.H, чорним по білому:

        == fvOptions(rho, he)   ...   fvOptions.constrain(EEqn)
        EEqn.solve();                 fvOptions.correct(he);

    Ціну цієї надмірної обачності заплатив справжній апарат користувача:
    чотиримоторний Body68 на 111 м/с падав з sigFpe рівно там, де рамки не
    стало, — одразу по розвʼязанні `e`, у корені Сазерленда.

    Тому: для тиск-орієнтованих — рамка, для rhoCentralFoam — порожньо.
    Межі свідомо широчезні (для 288 К це 72...1177 К): вони не можуть
    зачепити збіжний зовнішній обтік, а відʼємну температуру спиняють
    намертво. І коли рамка спрацьовує, OpenFOAM про це друкує — тиха
    правка тут була б гіршою за падіння.
    """
    if solver_name(cfg) not in ("rhoSimpleFoam", "rhoPimpleFoam"):
        return ""
    t = cfg.flow.t_inf
    # Температура гальмування: (γ-1)/2 = 0.2 для двоатомного газу. Стеля
    # береться від неї, бо саме до неї гріється потік у застійній точці.
    t_stag = t * (1.0 + 0.2 * cfg.flow.mach ** 2)
    lo = max(20.0, 0.25 * t)
    hi = max(4.0 * t_stag, t + 500.0)
    return f"""
limitT
{{
    type            limitTemperature;
    active          yes;
    selectionMode   all;
    min             {lo:.6g};
    max             {hi:.6g};
}}
"""


def fv_solution(cfg: CaseCfg) -> str:
    s = cfg.solver
    names = list(turbulence_field_names(cfg.flow.turbulence))
    if not names:
        # Алгебраїчна модель не розвʼязує жодного турбулентного рівняння.
        # Імена нижче склеюються в регулярку виду "(U|k|omega)", і порожній
        # список дав би "(U|)" та "()" — словник, який OpenFOAM прочитає, але
        # не так, як задумано. Підставляємо nut: його ніхто не розвʼязує, тож
        # зайвий запис у solvers і relaxationFactors просто нікуди не піде.
        names = ["nut"]
    if cfg.flow.compressible:
        # Рівняння енергії ділить із турбулентними і розвʼязувач, і
        # релаксацію, і поріг збіжності — тож просто стає в той самий список,
        # звідки все це збирається.
        names.append("e")
    turb = "|".join(names)

    if not s.steady:
        # Нестаціонар — окрема гілка цілком, а не домішки до стаціонарної.
        # SIMPLE і PIMPLE різняться не парою рядків: там інші розвʼязувачі
        # (окремі для останнього проходу), інший блок керування й інша
        # релаксація. Змішувати їх в одному шаблоні означало б ризикувати
        # стаціонарним шляхом, який працює.
        return (
            _head("dictionary", "fvSolution", "system")
            + _fv_solution_pimple(turb, s, cfg.flow.compressible,
                                  moving=cfg.motion.on,
                                  dynamic=cfg.adapt.native)
            + FOOTER
        )

    if cfg.flow.compressible:
        # Стисливий SIMPLE тримається на іншому, ніж нестисливий: густину
        # релаксують ДУЖЕ сильно (0.01), інакше збурення тиску живить саме
        # себе через рівняння стану й розрахунок іде врознос за десятки
        # ітерацій. pMinFactor/pMaxFactor — запобіжник від відʼємного тиску:
        # у нестисливому такого поняття немає, а тут воно валить рівняння
        # стану. Числа з туторіалу rhoSimpleFoam/aerofoilNACA0012 — це
        # зовнішня аеродинаміка, тобто рівно наш випадок.
        relax = (
            "relaxationFactors\n{\n    fields\n    {\n"
            "        p               0.7;\n"
            "        rho             0.01;\n    }\n"
            "    equations\n    {\n"
            f"        U               {0.2 if s.robust_start else 0.3};\n"
            f'        "({turb})"      0.7;\n'
            "    }\n}\n"
        )
        simple_extra = ("    pMinFactor          0.1;\n"
                        "    pMaxFactor          2;\n")
        return (
            _head("dictionary", "fvSolution", "system")
            + _fv_solution_body(turb, s, simple_extra, relax,
                                non_ortho=1 if s.robust_start else 0)
            + FOOTER
        )

    if s.robust_start:
        # Класичний SIMPLE з низькою релаксацією. SIMPLEC на 0.9 швидший, але
        # на брудній сітці саме він і зриває розрахунок.
        relax = (
            "relaxationFactors\n{\n    fields\n    {\n"
            "        p               0.3;\n    }\n"
            "    equations\n    {\n"
            "        U               0.7;\n"
            f'        "({turb})"      0.7;\n'
            "    }\n}\n"
        )
        simple_extra = "    consistent          no;\n"
        return (
            _head("dictionary", "fvSolution", "system")
            + _fv_solution_body(turb, s, simple_extra, relax, non_ortho=1)
            + FOOTER
        )

    if s.consistent:
        relax = (
            "relaxationFactors\n{\n    equations\n    {\n"
            f"        U               {s.relaxation_u};\n"
            f'        ".*"            {s.relaxation_u};\n'
            "    }\n}\n"
        )
        simple_extra = "    consistent          yes;\n"
    else:
        relax = (
            "relaxationFactors\n{\n    fields\n    {\n"
            f"        p               {s.relaxation_p};\n    }}\n"
            "    equations\n    {\n"
            f"        U               {s.relaxation_u};\n"
            f'        "({turb})"      {s.relaxation_u};\n'
            "    }\n}\n"
        )
        simple_extra = "    consistent          no;\n"

    return (
        _head("dictionary", "fvSolution", "system")
        + _fv_solution_body(turb, s, simple_extra, relax, non_ortho=0)
        + FOOTER
    )


# Розвʼязувач для зміщення вузлів. Потрібен, коли сітка рухається:
# displacementLaplacian розносить зміщення границі по домену, і для цього
# розвʼязує власне рівняння — а розвʼязувача для нього в fvSolution немає,
# бо в нерухомому кейсі його ніхто не питає. Без цього запису pimpleFoam
# падає на першій же ітерації: «Entry 'cellDisplacement' not found in
# dictionary system/fvSolution/solvers». Помилка приходить зі словника, і
# знайти її можна лише в журналі кроку.
#
# GAMG тут доречний: рівняння Лапласа на всю сітку — рівно та задача, на
# якій багатосіткові методи виграють найбільше.
#
# Плюс `pcorr` і `pcorrFinal`. Коли вузли зрушили, потік крізь грані вже не
# відповідає новому положенню сітки, і PIMPLE розвʼязує окрему корекцію,
# щоб закон збереження обʼєму виконувався точно. Без цих двох записів
# падіння таке саме, тільки на рядок далі: «Entry 'pcorrFinal' not found».
# Точність тут навмисно груба (1e-2): це технічна поправка на рух сітки, а
# не саме поле тиску, і доводити її до 1e-7 — витрачати час на ніщо.
#
# Записані повністю, без посилань `$p`: цей блок стоїть у словнику ВИЩЕ за
# `p`, а `$p` розкривається під час читання й на те, що нижче, не дивиться.
# Корекція потоку. Потрібна БУДЬ-ЯКІЙ динамічній сітці, а не лише рухомій:
# коли вузли зрушили АБО комірки поділилися, потоки крізь грані вже не
# відповідають новій сітці, і PIMPLE розвʼязує окрему корекцію.
#
# Я вже наступив на це двічі. Першого разу з рухомою сіткою: «Entry
# 'pcorrFinal' not found in dictionary». Другого разу — коли додав рідну
# адаптацію й лишив ці записи прив'язаними до руху. Помилка слово в слово
# та сама, і знайшлася аж на прогоні.
PCORR_SOLVER = """    pcorr
    {
        solver          GAMG;
        tolerance       1e-02;
        relTol          0;
        smoother        GaussSeidel;
    }

    pcorrFinal
    {
        solver          GAMG;
        tolerance       1e-02;
        relTol          0;
        smoother        GaussSeidel;
    }

"""

# А ось це — лише для РУХУ: розвʼязувач зміщення вузлів. Комірки, що
# діляться, вузлів не рухають, і цей запис їм ні до чого.
MOTION_SOLVER = """    cellDisplacement
    {
        solver          GAMG;
        tolerance       1e-06;
        relTol          0;
        smoother        GaussSeidel;
        nCellsInCoarsestLevel 20;
    }

""" + PCORR_SOLVER


def _fv_solution_pimple(turb: str, s: SolverCfg, compressible: bool,
                        moving: bool = False,
                        dynamic: bool = False) -> str:
    """fvSolution для PIMPLE.

    Головна відмінність від SIMPLE — окремі розвʼязувачі з суфіксом `Final`:
    на останньому зовнішньому проході кроку систему треба дорозвʼязати до
    кінця (relTol 0), інакше похибка накопичується з кроку в крок. На всіх
    інших проходах повне розвʼязання — марна витрата.

    Релаксація тут теж працює інакше. Записів `pFinal`/`UFinal` у
    relaxationFactors НЕМАЄ навмисно: коли OpenFOAM не знаходить запису для
    останнього проходу, він не релаксує зовсім — а саме цього й треба.
    """
    if s.n_outer_correctors <= 1:
        # Один зовнішній прохід — це PISO. Релаксувати нема чого й нема між
        # чим: поле в межах кроку розвʼязується один раз.
        relax = ('relaxationFactors\n{\n    equations\n    {\n'
                 '        ".*"            1;\n    }\n}\n')
    else:
        relax = ('relaxationFactors\n{\n    fields\n    {\n'
                 '        p               0.7;\n    }\n'
                 '    equations\n    {\n'
                 '        U               0.9;\n'
                 f'        "({turb})"      0.9;\n'
                 '    }\n}\n')
    # rhoPimpleFoam на відміну від pimpleFoam розвʼязує ще й рівняння для
    # густини й ВИМАГАЄ запису для неї — без нього падає на першій же
    # ітерації з «Entry 'rho' not found». Розвʼязувач тут diagonal, бо
    # рівняння явне: густина береться з рівняння стану, а не з системи.
    rho_solver = ('    "rho.*"\n'
                  '    {\n'
                  '        solver          diagonal;\n'
                  '    }\n\n') if compressible else ""
    # transonic no — явно: на дозвукових швидкостях це і є типове значення,
    # але мовчазний типовий вибір у словнику, де все інше виписано, читається
    # як недогляд. Трансзвук ця версія програми однаково не підтримує.
    transonic = "    transonic           no;\n" if compressible else ""
    # Рух дає повний блок (зміщення вузлів плюс корекція потоку), а сама
    # адаптація — лише корекцію: комірки діляться, але вузли не їдуть.
    motion = MOTION_SOLVER if moving else (PCORR_SOLVER if dynamic else "")
    return f"""solvers
{{
{motion}{rho_solver}    p
    {{
        solver          GAMG;
        tolerance       1e-7;
        relTol          0.01;
        smoother        GaussSeidel;
    }}

    pFinal
    {{
        $p;
        relTol          0;
    }}

    Phi
    {{
        $p;
        relTol          0.01;
    }}

    "(U|{turb})"
    {{
        solver          smoothSolver;
        smoother        symGaussSeidel;
        tolerance       1e-8;
        relTol          0.1;
        nSweeps         1;
    }}

    "(U|{turb})Final"
    {{
        $U;
        relTol          0;
    }}
}}

PIMPLE
{{
{transonic}    nOuterCorrectors    {max(1, s.n_outer_correctors)};
    nCorrectors         {max(1, s.n_correctors)};
    nNonOrthogonalCorrectors 0;
}}

potentialFlow
{{
    nNonOrthogonalCorrectors 10;
}}

{relax}"""


def _fv_solution_body(turb: str, s: SolverCfg, simple_extra: str,
                      relax: str, non_ortho: int) -> str:
    return f"""solvers
{{
    p
    {{
        solver          GAMG;
        tolerance       1e-7;
        relTol          0.01;
        smoother        GaussSeidel;
    }}

    Phi
    {{
        $p;
        relTol          0.01;
    }}

    U
    {{
        solver          smoothSolver;
        smoother        GaussSeidel;
        tolerance       1e-8;
        relTol          0.1;
        nSweeps         1;
    }}

    "({turb})"
    {{
        $U;
        tolerance       1e-8;
        relTol          0.1;
    }}
}}

SIMPLE
{{
    nNonOrthogonalCorrectors {non_ortho};
{simple_extra}
    residualControl
    {{
        p               {s.residual_tol:.1e};
        U               {s.residual_tol:.1e};
        "({turb})"      {s.residual_tol:.1e};
    }}
}}

potentialFlow
{{
    nNonOrthogonalCorrectors 10;
}}

{relax}"""


def decompose_par_dict(cfg: CaseCfg) -> str:
    n = max(1, cfg.solver.n_procs)
    method = cfg.solver.decompose_method
    extra = ""
    weights = [int(w) for w in (cfg.solver.proc_weights or [])]
    if len(weights) == n and n > 1 and all(w > 0 for w in weights):
        # Розподіл між машинами: шматок кожного процесу — за його швидкістю
        # (бенчмарк foamdist). Цілі числа: scotch читає їх як label. Ваги
        # бачить і snappyHexMesh, коли вирівнює сітку наприкінці.
        return (
            _head("dictionary", "decomposeParDict", "system")
            + f"numberOfSubdomains {n};\n\nmethod          scotch;\n\n"
            "scotchCoeffs\n{\n    processorWeights ("
            + " ".join(str(w) for w in weights) + ");\n}\n"
            + FOOTER
        )
    if method == "hierarchical":
        nx, ny, nz = _factor3(n)
        extra = (
            "\ncoeffs\n{\n"
            f"    n           ({nx} {ny} {nz});\n"
            "    delta       0.001;\n    order       xyz;\n}\n"
        )
    return (
        _head("dictionary", "decomposeParDict", "system")
        + f"numberOfSubdomains {n};\n\nmethod          {method};\n{extra}"
        + FOOTER
    )


def _factor3(n: int) -> tuple[int, int, int]:
    """Розкладає n на три множники якомога ближчі один до одного."""
    best = (n, 1, 1)
    best_spread = float("inf")
    for a in range(1, n + 1):
        if n % a:
            continue
        rest = n // a
        for b in range(1, rest + 1):
            if rest % b:
                continue
            c = rest // b
            spread = max(a, b, c) - min(a, b, c)
            if spread < best_spread:
                best_spread, best = spread, (a, b, c)
    return best


# ==========================================================================
# Скрипти
# ==========================================================================

def allrun(cfg: CaseCfg) -> str:
    s = cfg.solver
    par = s.n_procs > 1
    lines = [
        "#!/bin/sh",
        'cd "${0%/*}" || exit',
        ". ${WM_PROJECT_DIR:?}/bin/tools/RunFunctions",
        "",
        "restore0Dir",
        "runApplication surfaceFeatureExtract",
        "runApplication blockMesh",
    ]
    if par:
        lines += [
            "runApplication decomposePar -copyZero",
            "runParallel snappyHexMesh -overwrite",
        ]
        if s.run_check_mesh:
            lines.append("runParallel checkMesh -writeFields '(nonOrthoAngle)' -constant")
        lines += [
            "",
            "# snappyHexMesh -overwrite лишає поля 0/ неконсистентними з новою",
            "# сіткою, тому розкладаємо їх наново з еталона.",
            "restore0Dir -processor",
            "",
        ]
        if cfg.needs_topo_set:
            lines.append("runParallel topoSet")
        if cfg.use_potential_init:
            lines.append("runParallel potentialFoam -writephi")
        lines += _allrun_warm(cfg, "runParallel", "processor*/")
        lines += [
            "runParallel $(getApplication)",
            "runApplication reconstructParMesh -constant",
            "runApplication reconstructPar -latestTime",
        ]
    else:
        lines += ["runApplication snappyHexMesh -overwrite"]
        if s.run_check_mesh:
            lines.append("runApplication checkMesh -constant")
        lines += ["restore0Dir"]
        if cfg.needs_topo_set:
            lines.append("runApplication topoSet")
        if cfg.use_potential_init:
            lines.append("runApplication potentialFoam -writephi")
        lines += _allrun_warm(cfg, "runApplication", ".")
        lines.append("runApplication $(getApplication)")
    lines.append("")
    lines.append("#" + "-" * 76 + "#")
    return "\n".join(lines) + "\n"


def _allrun_warm(cfg: CaseCfg, run: str, where: str) -> list[str]:
    """Рядки Allrun для теплого старту (CaseCfg.warm_start), або нічого."""
    if not cfg.warm_start:
        return []
    files = ("system/fvSchemes", "system/fvSolution", "system/controlDict",
             "constant/fvOptions")
    warm = SOLVER_OF[(cfg.flow.regime, True)]
    use = lambda tag: [f"cp {f}.{tag} {f}" for f in files]   # noqa: E731
    return (["", "# Теплий старт: стаціонарне поле стає моментом 0."]
            + use("stage1")
            + [f"{run} -s warm {warm}",
               f"for d in {where}; do",
               '    last=$(cd "$d" && ls -d [0-9]* | sort -g | tail -1)',
               '    if [ "$last" != 0 ]; then',
               '        rm -rf "$d/0" && mv "$d/$last" "$d/0" '
               '&& rm -rf "$d/0/uniform"',
               "    fi",
               "done"]
            + use("stage2") + [""])


def allclean(cfg: CaseCfg) -> str:
    return """#!/bin/sh
cd "${0%/*}" || exit
. ${WM_PROJECT_DIR:?}/bin/tools/CleanFunctions

cleanCase0
rm -rf constant/extendedFeatureEdgeMesh
rm -f  constant/triSurface/*.eMesh
rm -rf postProcessing

#----------------------------------------------------------------------------#
"""


# ==========================================================================
# Збірка
# ==========================================================================

def build_case(cfg: CaseCfg) -> dict[str, str]:
    """Повертає {відносний шлях: вміст} для всіх текстових файлів кейсу."""
    files: dict[str, str] = {
        "system/controlDict": control_dict(cfg),
        "system/fvSchemes": (fv_schemes_central(cfg) if cfg.flow.supersonic else fv_schemes(cfg)),
        "system/fvSolution": (fv_solution_central(cfg) if cfg.flow.supersonic else fv_solution(cfg)),
        "system/blockMeshDict": block_mesh_dict(cfg),
        "system/snappyHexMeshDict": snappy_dict(cfg),
        "system/meshQualityDict": mesh_quality_dict(cfg),
        "system/surfaceFeatureExtractDict": surface_features_dict(cfg),
        "system/decomposeParDict": decompose_par_dict(cfg),
        "constant/turbulenceProperties": turbulence_properties(cfg),
        "constant/fvOptions": fv_options(cfg),
        "0.orig/U": field_U(cfg),
        "0.orig/p": field_p(cfg),
        "Allrun": allrun(cfg),
        "Allclean": allclean(cfg),
    }
    # Два режими — дві різні моделі середовища, і плутати їх не можна:
    # rhoSimpleFoam читає thermophysicalProperties і не знає, що таке
    # transportProperties, а simpleFoam — навпаки. Класти обидва «про всяк
    # випадок» гірше за жодного: кейс тоді виглядає придатним для обох, і
    # звідки взялася густина, стає питанням віри.
    if cfg.needs_topo_set:
        files["system/topoSetDict"] = topo_set_dict(cfg)
    if cfg.rotating:
        files["constant/MRFProperties"] = mrf_properties(cfg)
    if cfg.motion.on:
        # Два файли, і обидва лише коли рух увімкнено. Порожній
        # dynamicMeshDict у нерухомому кейсі змусив би pimpleFoam шукати
        # рухому сітку там, де її немає.
        files["constant/dynamicMeshDict"] = dynamic_mesh_dict(cfg)
        files["0.orig/pointDisplacement"] = field_point_displacement(cfg)
    elif cfg.adapt.native:
        # Рідна адаптація — теж dynamicFvMesh, і в кейсі він може бути
        # лише один. Тому або рух, або адаптація; guard_adapt це й
        # забороняє, а тут ми просто не пишемо другий.
        files["constant/dynamicMeshDict"] = dynamic_mesh_dict_refine(cfg)
    if cfg.flow.compressible:
        files["constant/thermophysicalProperties"] = thermophysical_properties(cfg)
        files["0.orig/T"] = field_T(cfg)
        files["0.orig/alphat"] = field_alphat(cfg)
    else:
        files["constant/transportProperties"] = transport_properties(cfg)

    for name, text in turbulence_fields(cfg).items():
        files[f"0.orig/{name}"] = text

    if cfg.solver.two_stage or cfg.warm_start:
        files.update(_stage_files(cfg))
    return files


def mesh_dicts(cfg: CaseCfg) -> dict[str, str]:
    """Лише словники, з яких складається ключ збереженої сітки
    (`fs_backend.MESH_INPUTS`), — без решти кейсу. Ті самі тексти, що й у
    build_case(): з ними порівнюють записаний кейс, щоб знати, чи підійде
    збережена сітка до ТЕПЕРІШНІХ налаштувань."""
    return {
        "system/blockMeshDict": block_mesh_dict(cfg),
        "system/snappyHexMeshDict": snappy_dict(cfg),
        "system/surfaceFeatureExtractDict": surface_features_dict(cfg),
        "system/meshQualityDict": mesh_quality_dict(cfg),
    }


# Шляхи, які build_case() пише ТІЛЬКИ ЗА УМОВИ — тобто саме ті, що можуть
# лишитися на диску від попереднього збирання того самого кейса, коли умова
# перестала виконуватися.
#
# Це не дрібниця, а справжня причина падінь, які виглядають як зіпсована
# геометрія. Хтось скинув ролі деталей назад на «тіло» (кнопка «Скинути
# налаштування» так і робить), обертових зон не лишилося — і build_case()
# більше не повертає ні `constant/MRFProperties`, ні `system/topoSetDict`.
# Але писальний цикл у create_case() лише ЗАПИСУЄ те, що дав build_case(), і
# НІКОЛИ не видаляє те, чого він більше не дав. Стара MRFProperties із
# чотирма зонами лишається лежати, топосет, що мав їх створити в сітці,
# більше не запускається (needs_topo_set теж став False) — і розвʼязувач
# падає з «cannot find MRF cellZone», хоча сама постановка виглядає чистою.
#
# Список тримається тут, поруч із build_case(), а не десь у вікні: це єдине
# місце, де видно, які саме ключі умовні, і звідси він не може розійтися з
# реальною логікою вище.
CONDITIONAL_PATHS: tuple[str, ...] = (
    "system/topoSetDict",
    "constant/MRFProperties",
    "constant/dynamicMeshDict",
    "0.orig/pointDisplacement",
    "constant/thermophysicalProperties",
    "0.orig/T",
    "0.orig/alphat",
    "constant/transportProperties",
    "system/fvSchemes.stage1", "system/fvSchemes.stage2",
    "system/fvSolution.stage1", "system/fvSolution.stage2",
    "system/controlDict.stage1", "system/controlDict.stage2",
    "constant/fvOptions.stage1", "constant/fvOptions.stage2",
)


def prune_stale_files(case_dir, files: dict[str, str]) -> list[str]:
    """Прибрати з `case_dir` умовні файли, яких у цьому збиранні вже нема.

    Викликати ПІСЛЯ build_case(), ПЕРЕД записом `files`. Повертає список
    прибраних шляхів — не для контролю, а щоб було що показати в журналі:
    мовчазне прибирання тут таке саме небезпечне, як мовчазне залишення.
    """
    from pathlib import Path as _Path
    removed = []
    base = _Path(case_dir)
    for rel in CONDITIONAL_PATHS:
        if rel in files:
            continue
        p = base / rel
        if p.exists():
            p.unlink()
            removed.append(rel)
    return removed


def _stage_files(cfg: CaseCfg) -> dict[str, str]:
    """Два комплекти словників для двоетапного прогону.

    Перший етап рахує стійко (upwind, низька релаксація, стеля швидкості) і
    доводить поле до придатного стану. Другий продовжує з нього звичайними
    схемами другого порядку — у controlDict стоїть startFrom latestTime, тому
    жодного перенесення полів не потрібно, поле просто читається з останнього
    часу. Ключове: між етапами НЕ можна відновлювати 0.orig, інакше результат
    першого етапу буде затерто.
    """
    import copy

    if cfg.warm_start:
        return _warm_stage_files(cfg)

    stage1 = copy.deepcopy(cfg)
    stage1.solver.robust_start = True
    stage1.solver.n_iterations = cfg.solver.robust_iterations
    # КРИТИЧНО: перший етап мусить ЗАПИСАТИ своє поле, інакше другий етап із
    # startFrom latestTime не знайде нічого, крім нуля, і почне з початку —
    # усі стійкі ітерації будуть викинуті. Мовчки: у журналі просто видно, що
    # другий етап рахує з Time = 1.
    #
    # OpenFOAM не пише фінальний крок автоматично: при writeControl timeStep
    # запис відбувається лише коли номер ітерації кратний writeInterval. Тому
    # робимо інтервал рівним довжині етапу — тоді останній крок точно на місці.
    stage1.solver.write_interval = max(1, cfg.solver.robust_iterations)
    # Зупинка по резидуалах на першому етапі не потрібна: треба просто дійти
    # до розумного поля, а не зійтися першим порядком.
    stage1.solver.residual_tol = 1e-12

    stage2 = copy.deepcopy(cfg)
    stage2.solver.robust_start = False

    out = {}
    for tag, c in (("stage1", stage1), ("stage2", stage2)):
        out[f"system/fvSchemes.{tag}"] = fv_schemes(c)
        out[f"system/fvSolution.{tag}"] = fv_solution(c)
        out[f"system/controlDict.{tag}"] = control_dict(c)
        out[f"constant/fvOptions.{tag}"] = fv_options(c)
    return out


def _warm_stage_files(cfg: CaseCfg) -> dict[str, str]:
    """Теплий старт (CaseCfg.warm_start): етап 1 — стаціонарний, етап 2 — свій.

    Етап 1 — це той самий кейс, але стаціонарний і стійкий (upwind,
    низька релаксація, стеля швидкості): йому треба не зійтися, а
    прибрати гідроудар. Його останнє поле крок «поле → момент 0» робить
    нульовим моментом, і етап 2 (нестаціонар) стартує з нього.
    """
    import copy

    n = max(1, int(cfg.solver.warm_iterations))
    stage1 = copy.deepcopy(cfg)
    s1 = stage1.solver
    s1.steady = True
    s1.robust_start = True
    s1.n_iterations = n
    s1.write_interval = n          # записати рівно останню ітерацію
    s1.residual_tol = 1e-12        # не зупинятися раніше: див. вище
    # Адаптація — справа нестаціонару (dynamicRefineFvMesh); стаціонарний
    # етап сітку не міняє, і показник йому рахувати ні до чого.
    stage1.adapt.mode = "none"
    stage2 = copy.deepcopy(cfg)

    out = {}
    for tag, c in (("stage1", stage1), ("stage2", stage2)):
        out[f"system/fvSchemes.{tag}"] = fv_schemes(c)
        out[f"system/fvSolution.{tag}"] = fv_solution(c)
        out[f"system/controlDict.{tag}"] = control_dict(c)
        out[f"constant/fvOptions.{tag}"] = fv_options(c)
    return out


def summary(cfg: CaseCfg) -> dict[str, str]:
    """Короткий звіт для рядка стану інтерфейсу."""
    est = cfg.estimate()
    f = cfg.flow
    re = phys.reynolds(f.u_inf, f.l_ref, f.nu)
    stack = cfg.layer_stack
    yp = stack.yplus(f.u_inf, f.l_ref, f.nu) if stack.n_layers else 0.0
    sx, sy, sz = cfg.domain_size
    return {
        "Re": f"{re:.3g}",
        "Mach": f"{f.mach:.3f}",
        "Domain": tr("{sx:.4g} x {sy:.4g} x {sz:.4g} m", sx=sx, sy=sy, sz=sz),
        "Background cell": phys.fmt_len(cfg.base_cell),
        "Surface cell": phys.fmt_len(cfg.surface_cell),
        "Cells (estimate)": phys.fmt_cells(est.total),
        "snappy memory": tr("{snappy_peak_gb:.1f} GB", snappy_peak_gb=est.snappy_peak_gb),
        "solver memory": tr("{solver_gb:.1f} GB", solver_gb=est.solver_gb),
        "y+ (estimate)": f"{yp:.1f}" if yp else "no layers",
        "First layer": phys.fmt_len(stack.first_abs) if stack.n_layers else "—",
        "Layer stack / boundary layer": (
            f"{stack.covers_bl(f.u_inf, f.l_ref, f.nu):.2f}" if stack.n_layers else "—"
        ),
    }
