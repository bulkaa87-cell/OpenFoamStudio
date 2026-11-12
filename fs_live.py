"""Проміжні результати прогону, що ще йде: дані, без малювання.

ЩО САМЕ ВОЗИМО. Повний зріз великого кейсу (2.4 млн комірок, 7 полів) —
це 100–300 МБ, і через Wi-Fi його не тягнуть заради одного погляду. Тому
прогін САМ пише малий продукт у кожен момент запису: функція `surfaces`
(бібліотека sampling) кладе поверхню тіла з p, тертям і y+ та одну
середню площину з p, U, nut у postProcessing/studioLive*/<момент>/ як
.vtp. Це кілька мегабайтів на момент, а рахувати потім нічого не треба.

У ПАРАЛЕЛЬНОМУ прогоні поверхню збирає головний процес: файли лежать у
корені кейсу, а не в processor*/. Так і в розподіленому (foamdist), де
processor*/ розкидані по різних машинах.

Прогону, пущеному без цієї функції, її можна ДОДАТИ на льоту: словник
system/controlDict перечитується (runTimeModifiable), і нова функція
працює з наступного запису. Перевірено 2026-11-10 на v2512 (simpleFoam,
6 процесів): додана посеред прогону функція записала поверхні з
наступного ж запису; навмисно зіпсована (чужий патч, неповний словник,
невідомий тип) дала лише «FOAM Warning», розвʼязувач дорахував до кінця
з кодом 0.

Для моментів, записаних ДО цього, є запасний шлях: foamToVTK по кожній
processor*/ окремо (лише патчі тіла, з nice), без reconstructPar і без
читання всієї сітки разом. Для розподіленого прогону він не годиться:
частини лежать на інших машинах.

Модуль без Qt: смуга у вікні (fs_livebar) кличе його з окремого потоку.
"""

from __future__ import annotations

import hashlib
import os
import re
import shutil
import subprocess
import tarfile
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np

import fs_backend as be
from fs_i18n import tr

# Імена функцій у controlDict і тек у postProcessing.
FO_WALL = "studioLive"
FO_SLICE = "studioLiveSlice"
SLICE_NAME = "studio_slice"
# Що пишемо. Швидкість на стінці — нуль (прилипання), тож на тілі вона не
# потрібна; тертя й y+ — поля функцій wallShearStress і yPlus, які стоять
# у словнику вище й рахуються раніше.
WALL_FIELDS = ("p", "wallShearStress", "yPlus")
SLICE_FIELDS = ("p", "U", "nut")
# Груба 3D-вибірка швидкості для ліній потоку (функція `sets`, тип array).
FO_FLOW = "studioLiveFlow"
FLOW_NAME = "studio_flow"
FLOW_FILE = f"{FLOW_NAME}_U.xy.gz"
FLOW_POINTS = 120_000
# Момент, файли якого змінювалися зовсім недавно, ще може дописуватися.
FRESH_S = 20
# Скільки завантажених моментів тримаємо на один прогін.
KEEP = 8
# Тека для запасного шляху (foamToVTK), усередині кожної processor*/.
EXTRACT = ".studio-live"


def _fo_text(cfg) -> str:
    """Дві функції для controlDict: поверхня тіла й середня площина."""
    patches = [b.name for b in cfg.body_patches]
    out = []
    if patches:
        surf = "".join(f"""
            {name}
            {{
                type        patch;
                patches     ({name});
                interpolate false;
                triangulate false;
            }}""" for name in patches)
        out.append(_surfaces_fo(FO_WALL, WALL_FIELDS, surf))
    # Площина вздовж потоку через середину тіла: нормаль по розмаху (Y),
    # та сама, що й типова вісь смуги «Зріз».
    cx, cy, cz = cfg.centre
    surf = f"""
            {SLICE_NAME}
            {{
                type        cuttingPlane;
                planeType   pointAndNormal;
                pointAndNormalDict
                {{
                    point   ({cx:.6g} {cy:.6g} {cz:.6g});
                    normal  (0 1 0);
                }}
                interpolate false;
                triangulate true;
            }}"""
    out.append(_surfaces_fo(FO_SLICE, SLICE_FIELDS, surf))
    out.append(_flow_fo(cfg))
    return "".join(out)


def flow_box(cfg, points: int = FLOW_POINTS):
    """Коробка грубої вибірки швидкості: (початок, розмір, точок по осях).

    Габарит тіла з запасом 0.3 L з кожного боку і ще 1.0 L за тілом (у бік
    потоку): там слід, заради якого лінії й дивляться. Крок однаковий по
    всіх осях, щоб лінії не тягнулися вздовж однієї з них.
    """
    lo = np.asarray(cfg.bbox_min, dtype=float)
    hi = np.asarray(cfg.bbox_max, dtype=float)
    length = max(float(np.max(hi - lo)), 1e-6)
    d = np.asarray(getattr(cfg.flow, "direction", (1.0, 0.0, 0.0)), float)
    d = d / max(float(np.linalg.norm(d)), 1e-12)
    lo = lo - 0.3 * length - 1.0 * length * np.maximum(-d, 0.0)
    hi = hi + 0.3 * length + 1.0 * length * np.maximum(d, 0.0)
    span = hi - lo
    step = float(np.prod(span) / max(int(points), 1)) ** (1.0 / 3.0)
    # arraySet ставить n точок у ВНУТРІШНІ вузли: крок = розмір / (n + 1).
    n = np.maximum(np.round(span / step).astype(int) - 1, 1)
    return tuple(lo), tuple(span), tuple(int(k) for k in n)


def _flow_fo(cfg) -> str:
    """Функція `sets` (тип array): U у рівній 3D-сітці точок навколо тіла.

    Площина вище дає лише одну площину, а лінії потоку з неї виходять одразу
    ж. Ця вибірка — грубий 3D-обʼєм для ліній на проміжному результаті:
    ~120 тис. точок, 2–4 МБ (gzip) на запис. Точки в тілі й поза доменом
    OpenFOAM не пише.
    """
    (ox, oy, oz), (sx, sy, sz), (nx, ny, nz) = flow_box(cfg)
    return f"""
    {FO_FLOW}
    {{
        type            sets;
        libs            (sampling);
        writeControl    writeTime;
        errors          warn;
        interpolationScheme cellPoint;
        setFormat       raw;
        formatOptions
        {{
            raw
            {{
                compression true;
                precision   6;
            }}
        }}
        fields          (U);
        sets
        {{
            {FLOW_NAME}
            {{
                type            array;
                axis            xyz;
                origin          ({ox:.6g} {oy:.6g} {oz:.6g});
                rotation
                {{
                    type    axes;
                    e1      (1 0 0);
                    e2      (0 1 0);
                }}
                pointsDensity   ({nx} {ny} {nz});
                spanBox         ({sx:.6g} {sy:.6g} {sz:.6g});
            }}
        }}
    }}
"""


def _surfaces_fo(name: str, fields, surfaces: str) -> str:
    return f"""
    {name}
    {{
        type            surfaces;
        libs            (sampling);
        writeControl    writeTime;
        errors          warn;
        surfaceFormat   vtk;
        formatOptions
        {{
            vtk
            {{
                format      binary;
                precision   7;
            }}
        }}
        fields          ({' '.join(fields)});
        surfaces
        {{{surfaces}
        }}
    }}
"""


def functions(cfg) -> str:
    """Блок для `functions { }` нового прогону (див. fs_case.control_dict)."""
    return _fo_text(cfg)


# --------------------------------------------------------------------------
# Сценарії bash для машини, де йде прогін
# --------------------------------------------------------------------------

def _q(path: str) -> str:
    """Шлях у лапках bash; `$HOME` лишається розгортатися."""
    return '"' + path.replace('"', '\\"') + '"'


def probe_script(target: str) -> str:
    """bash: що є в теці прогону. Рядки:

    now <сек>                 — годинник тієї машини (для «свіжих» файлів)
    fo <0|1>                  — функція studioLive уже в system/controlDict
    dist <0|1>                — розподілений прогін (foamdist)
    live <момент> <mtime> <байт>  — готова поверхня
    raw <момент>              — повний зріз (processor0/ або корінь)
    idx <момент> <крок>       — номер кроку цього моменту (uniform/time)
    """
    t = _q(target)
    return (
        f'cd {t} 2>/dev/null || {{ echo nodir; exit 0; }}; shopt -s nullglob; '
        'echo "now $(date +%s)"; '
        f'grep -q "^[[:space:]]*{FO_WALL}[[:space:]]*$" system/controlDict '
        '2>/dev/null && echo "fo 1" || echo "fo 0"; '
        '[ -f foamdist.json ] && echo "dist 1" || echo "dist 0"; '
        f'for d in postProcessing/{FO_WALL}/*/; do n=${{d%/}}; n=${{n##*/}}; '
        'case "$n" in ""|*[!0-9.e+-]*) continue;; esac; '
        'm=0; s=0; for f in "$d"*.vtp; do '
        'fm=$(stat -c %Y "$f"); fs=$(stat -c %s "$f"); '
        '[ "$fm" -gt "$m" ] && m=$fm; s=$((s+fs)); done; '
        f'for f in postProcessing/{FO_SLICE}/"$n"/*.vtp '
        f'postProcessing/{FO_FLOW}/"$n"/*; do '
        's=$((s+$(stat -c %s "$f"))); done; '
        'echo "live $n $m $s"; done; '
        'src=.; [ -d processor0 ] && src=processor0; '
        'for d in "$src"/[0-9]*/; do n=${d%/}; n=${n##*/}; '
        'case "$n" in ""|0|*[!0-9.e+-]*) continue;; esac; '
        'echo "raw $n"; '
        'i=$(sed -nE "s/^[[:space:]]*index[[:space:]]+([0-9]+);.*/\\1/p" '
        '"$d/uniform/time" 2>/dev/null | head -1); '
        '[ -n "$i" ] && echo "idx $n $i"; done; true'
    )


def fetch_script(target: str, t: str) -> str:
    """bash: поверхні одного моменту — tar.gz у stdout, з nice."""
    return (
        f'cd {_q(target)} || exit 1; '
        f'set --; for d in postProcessing/{FO_WALL}/{t} '
        f'postProcessing/{FO_SLICE}/{t} postProcessing/{FO_FLOW}/{t}; '
        'do [ -d "$d" ] && set -- "$@" "$d"; '
        'done; [ $# -gt 0 ] || exit 3; '
        'nice -n 19 tar czf - "$@"'
    )


def extract_script(target: str, bashrc: str, t: str,
                   patches: "list[str]", engine: str = "",
                   image: str = "") -> str:
    """bash: поверхні тіла з повного зрізу (запасний шлях), tar.gz у stdout.

    foamToVTK по кожній processor*/ окремо: кожна читає лише свій шматок
    сітки (на 2.4 млн комірок і 28 частинах — десятки МБ памʼяті), з
    найнижчим пріоритетом процесора й диска. Виходи — у тимчасову теку
    всередині частини, вони одразу ж видаляються. Для розподіленого прогону
    (foamdist) — код 4: частини лежать на інших машинах.
    """
    sel = (" -patches '(" + " ".join(patches) + ")'") if patches else ""
    conv = (f'nice -n 19 ionice -c3 foamToVTK -time {t} -no-internal '
            f"-no-point-data -fields '({' '.join(WALL_FIELDS)})'{sel} "
            f'-name {EXTRACT}')
    body = (
        '[ -f foamdist.json ] && exit 4; shopt -s nullglob; '
        f'rm -rf processor*/{EXTRACT} {EXTRACT}; n=0; '
        'for p in processor*; do '
        f'[ -d "$p/{t}" ] || continue; '
        f'{conv} -case "$p" >/dev/null 2>&1 && n=$((n+1)); done; '
        f'if [ "$n" = 0 ] && [ -d "{t}" ]; then {conv} >/dev/null 2>&1 '
        '&& n=1; fi; '
        '[ "$n" -gt 0 ] || exit 3; '
        f'set -- processor*/{EXTRACT}; [ -d {EXTRACT} ] && set -- "$@" {EXTRACT}; '
        f'nice -n 19 tar czf - "$@"; rc=$?; rm -rf processor*/{EXTRACT} '
        f'{EXTRACT}; exit $rc'
    )
    if engine:
        # OpenFOAM in a container on the server (fs_backend.container_run).
        return (f'cd {_q(target)} || exit 1; '
                + be.container_run(engine, image, body))
    return f'source {_q(bashrc)} >/dev/null 2>&1; cd {_q(target)} || exit 1; ' + body


ENABLE_MARK = "studio/live-added"


def enable_script(target: str, block: str) -> str:
    """bash: дописати функції в controlDict прогону, що йде.

    Вставка — одразу після `functions` і його `{`, як у словниках, що їх
    пише програма. Новий файл кладеться поруч і підміняє старий одним `mv`,
    щоб розвʼязувач ніколи не прочитав половину. Копія старого лишається в
    studio/. Відповідь: `added`, `present` або `nodict`.
    """
    c = f'{_q(target)}/system/controlDict'
    body = block.replace("'", "")
    return (
        f'[ -f {c} ] || {{ echo nodict; exit 0; }}; '
        f'grep -q "^[[:space:]]*{FO_WALL}[[:space:]]*$" {c} '
        '&& { echo present; exit 0; }; '
        f'grep -q "^functions" {c} || {{ echo nodict; exit 0; }}; '
        f'mkdir -p {_q(target)}/studio; '
        f'cp {c} {_q(target)}/studio/controlDict.before-live; '
        f"blk='{body}'; "
        'awk -v blk="$blk" '
        "'done!=1 && seen==1 && /^\\{/ {print; print blk; done=1; next} "
        "/^functions/ {seen=1} {print}' "
        f'{c} > {c}.studio-live && mv {c}.studio-live {c} && '
        f'grep -q "^[[:space:]]*{FO_WALL}[[:space:]]*$" {c} && '
        f'{{ date +%s > {_q(target)}/{ENABLE_MARK}; echo added; }} '
        '|| echo nodict'
    )


# --------------------------------------------------------------------------
# Звідки брати: прогін і машина
# --------------------------------------------------------------------------

@dataclass
class LiveTime:
    time: float
    name: str                 # імʼя каталогу, як на диску
    surface: bool = False     # є готова поверхня (studioLive)
    raw: bool = False         # є повний зріз
    index: int = -1           # номер кроку
    mtime: int = 0
    size: int = 0
    fresh: bool = False       # ще може дописуватися


@dataclass
class Probe:
    ok: bool = True
    why: str = ""
    has_fo: bool = False
    distributed: bool = False
    times: "list[LiveTime]" = field(default_factory=list)

    def usable(self) -> "list[LiveTime]":
        """Моменти, які можна завантажити (без тих, що ще пишуться)."""
        return [t for t in self.times
                if not t.fresh and (t.surface or (t.raw and self.can_extract))]

    @property
    def can_extract(self) -> bool:
        return not self.distributed


def parse_probe(text: str) -> Probe:
    lines = [ln.split() for ln in text.splitlines() if ln.strip()]
    if any(ln[0] == "nodir" for ln in lines):
        return Probe(ok=False, why=tr("the run folder does not exist"))
    now = 0
    pr = Probe()
    by: dict[str, LiveTime] = {}

    def get(name: str) -> "LiveTime | None":
        try:
            t = float(name)
        except ValueError:
            return None
        return by.setdefault(name, LiveTime(t, name))

    for ln in lines:
        key, rest = ln[0], ln[1:]
        if key == "now" and rest:
            now = int(rest[0])
        elif key == "fo" and rest:
            pr.has_fo = rest[0] == "1"
        elif key == "dist" and rest:
            pr.distributed = rest[0] == "1"
        elif key == "live" and len(rest) >= 3:
            lt = get(rest[0])
            if lt is not None and int(rest[2]) > 0:
                lt.surface, lt.mtime, lt.size = True, int(rest[1]), int(rest[2])
        elif key == "raw" and rest:
            lt = get(rest[0])
            if lt is not None:
                lt.raw = True
        elif key == "idx" and len(rest) >= 2:
            lt = get(rest[0])
            if lt is not None:
                lt.index = int(rest[1])
    for lt in by.values():
        lt.fresh = bool(lt.surface and now and now - lt.mtime < FRESH_S)
    # Повний зріз останнього моменту теж може ще писатися: його mtime тут
    # невідомий, тож найпізніший «лише повний» момент без поверхні беремо
    # лише тоді, коли після нього вже є інший.
    pr.times = sorted(by.values(), key=lambda x: x.time)
    raws = [t for t in pr.times if t.raw and not t.surface]
    if raws and raws[-1] is pr.times[-1]:
        raws[-1].fresh = True
    return pr


@dataclass
class Source:
    """Прогін, з якого беремо: машина, тека там, тека кейсу тут."""
    backend: "be.Backend"
    case_dir: str
    native_dir: str = ""

    @property
    def target(self) -> str:
        """Тека прогону на ТІЙ машині (для bash)."""
        if self.native_dir:
            return self.native_dir
        if self.backend.kind == "wsl":
            return be.win_to_wsl(self.case_dir)
        return self.case_dir

    @property
    def has_shell(self) -> bool:
        return self.backend.kind in ("ssh", "wsl", "native") and (
            self.backend.kind != "ssh" or bool(self.native_dir))

    @property
    def local_root(self) -> str:
        """Тека, яку можна читати прямо звідси, або "" (тоді — через tar)."""
        if self.backend.kind in ("docker", "bluecfd", "native"):
            return self.case_dir
        if self.backend.kind == "wsl" and not self.native_dir:
            return self.case_dir
        return ""

    def argv(self, script: str) -> "list[str]":
        k = self.backend.kind
        if k == "ssh":
            return be.ssh_argv(self.backend.host, script)
        if k == "wsl":
            return be.wsl_argv(self.backend.distro, script)
        return ["bash", "-c", script]

    def key(self) -> str:
        who = f"{self.backend.kind}|{self.backend.host}|{self.target}"
        return hashlib.sha1(who.encode("utf-8")).hexdigest()[:12]

    # --- запити ---------------------------------------------------------

    def probe(self, timeout: int = 40) -> Probe:
        if not self.has_shell:
            return _probe_local(self.case_dir)
        try:
            p = subprocess.run(self.argv(probe_script(self.target)),
                               capture_output=True, timeout=timeout,
                               creationflags=be.CREATE_NO_WINDOW)
        except (OSError, subprocess.SubprocessError) as exc:
            return Probe(ok=False, why=str(exc)[:200])
        out = (p.stdout or b"").decode("utf-8", "replace")
        if p.returncode != 0 and "now " not in out:
            err = (p.stderr or b"").decode("utf-8", "replace").strip()
            return Probe(ok=False, why=err[-200:] or tr("code {rc}",
                                                        rc=p.returncode))
        return parse_probe(out)

    def enable(self, cfg) -> str:
        """Дописати функції в прогін, що йде. Відповідь сценарію."""
        block = functions(cfg)
        if not self.has_shell:
            return _enable_local(self.case_dir, block)
        p = subprocess.run(self.argv(enable_script(self.target, block)),
                           capture_output=True, timeout=60,
                           creationflags=be.CREATE_NO_WINDOW)
        out = (p.stdout or b"").decode("utf-8", "replace").strip()
        return out.splitlines()[-1] if out else "nodict"

    def fetch(self, lt: LiveTime, patches: "list[str]",
              timeout: int = 900) -> "tuple[str, dict]":
        """Покласти поверхні моменту в теку тут. (тека, заміри)."""
        t0 = time.monotonic()
        if self.local_root and lt.surface:
            folder = _local_folder(self.local_root, lt.name)
            return folder, {"bytes": 0, "seconds": time.monotonic() - t0,
                            "how": "local"}
        dest = cache_dir(self) / f"{lt.name}@{lt.mtime}"
        if lt.surface and _complete(dest):
            return str(dest), {"bytes": 0, "seconds": 0.0, "how": "cache"}
        if not self.has_shell:
            raise RuntimeError(tr("intermediate data is not available on this backend"))
        if lt.surface:
            script, how = fetch_script(self.target, lt.name), "surface"
        else:
            script = extract_script(self.target, self.backend.bashrc,
                                    lt.name, patches,
                                    engine=getattr(self.backend, "engine", ""),
                                    image=getattr(self.backend, "image", ""))
            how = "extract"
        tmp = Path(tempfile.gettempdir()) / f"ofs-live-{self.key()}.tgz"
        rc, err = be._pipe(self.argv(script), stdout_path=tmp,
                           timeout=timeout)
        try:
            if rc == 4:
                raise RuntimeError(tr(
                    "the run is distributed over machines: the full time "
                    "steps are not here, only the surfaces that the run "
                    "writes"))
            if rc == 3:
                raise RuntimeError(tr("there is no data for time {t}",
                                      t=lt.name))
            if rc != 0:
                raise RuntimeError(tr("cannot get the data: {err}",
                                      err=err.strip()[-200:] or rc))
            size = tmp.stat().st_size
            part = dest.with_name(dest.name + ".part")
            shutil.rmtree(part, ignore_errors=True)
            part.mkdir(parents=True)
            with tarfile.open(tmp, "r:gz") as tar:
                _safe_extract(tar, part)
            _normalise(part, how)
            shutil.rmtree(dest, ignore_errors=True)
            part.rename(dest)
        finally:
            tmp.unlink(missing_ok=True)
        _trim(dest.parent)
        return str(dest), {"bytes": size, "seconds": time.monotonic() - t0,
                           "how": how}


def cache_dir(src: Source) -> Path:
    return Path(tempfile.gettempdir()) / "ofs-live" / src.key()


def _complete(dest: Path) -> bool:
    return dest.is_dir() and any(dest.glob("*.vtp"))


def _trim(folder: Path, keep: int = KEEP) -> None:
    """Лишити `keep` найсвіжіших моментів кешу одного прогону."""
    try:
        items = sorted((p for p in folder.iterdir() if p.is_dir()),
                       key=lambda p: p.stat().st_mtime)
    except OSError:
        return
    for p in items[:-keep]:
        shutil.rmtree(p, ignore_errors=True)


def _safe_extract(tar: tarfile.TarFile, dest: Path) -> None:
    """Лише звичайні файли й теки, і лише всередину `dest`."""
    root = dest.resolve()
    for m in tar.getmembers():
        if not (m.isfile() or m.isdir()):
            continue
        out = (dest / m.name).resolve()
        if root != out and root not in out.parents:
            continue
        if m.isdir():
            out.mkdir(parents=True, exist_ok=True)
            continue
        out.parent.mkdir(parents=True, exist_ok=True)
        src = tar.extractfile(m)
        if src is not None:
            with src, open(out, "wb") as fh:
                shutil.copyfileobj(src, fh)


def _normalise(part: Path, how: str) -> None:
    """Звести розпаковане до плоского вигляду: <патч>.vtp, studio_slice.vtp.

    Поверхні прогону лежать як postProcessing/<функція>/<момент>/*.vtp.
    Запасний шлях дає шматок кожного патча з кожної processor*/ — їх
    зшиваємо в один файл на патч.
    """
    files = list(part.rglob("*.vtp"))
    if how != "extract":
        for f in files + list(part.rglob(FLOW_FILE)):
            f.replace(part / f.name)
    else:
        import pyvista as pv
        groups: dict[str, list] = {}
        for f in files:
            if f.parent.name != "boundary":
                continue
            groups.setdefault(f.stem, []).append(f)
        for name, items in groups.items():
            meshes = [pv.read(str(f)) for f in sorted(items)]
            meshes = [m for m in meshes if m.n_cells]
            if not meshes:
                continue
            merged = meshes[0] if len(meshes) == 1 else meshes[0].merge(
                meshes[1:], merge_points=False)
            if not isinstance(merged, pv.PolyData):
                merged = merged.extract_surface()
            merged.save(str(part / f"{name}.vtp"))
    for d in [p for p in part.iterdir() if p.is_dir()]:
        shutil.rmtree(d, ignore_errors=True)


def _local_folder(root: str, name: str) -> str:
    """Поверхні моменту в кейсі, який видно звідси: зводимо в одну теку
    кешу посиланнями не можна (Windows), тож повертаємо теку стінки, а
    площину LiveResults шукає поруч сам."""
    return str(Path(root) / "postProcessing" / FO_WALL / name)


def _probe_local(case_dir: str) -> Probe:
    """`probe_script` Python-ом — для кейсу, що лежить тут (Docker, BlueCFD)."""
    lines = [f"now {int(time.time())}"]
    c = Path(case_dir) / "system" / "controlDict"
    try:
        has = re.search(rf"^\s*{FO_WALL}\s*$", c.read_text("utf-8"), re.M)
    except OSError:
        has = None
    lines.append(f"fo {1 if has else 0}")
    lines.append("dist 0")
    base = Path(case_dir) / "postProcessing" / FO_WALL
    for d in sorted(base.iterdir()) if base.is_dir() else []:
        vtp = list(d.glob("*.vtp"))
        if not vtp:
            continue
        m = int(max(f.stat().st_mtime for f in vtp))
        s = sum(f.stat().st_size for f in vtp)
        lines.append(f"live {d.name} {m} {s}")
    pr = parse_probe("\n".join(lines))
    pr.distributed = True        # запасного шляху тут немає
    return pr


def _enable_local(case_dir: str, block: str) -> str:
    c = Path(case_dir) / "system" / "controlDict"
    try:
        text = c.read_text("utf-8")
    except OSError:
        return "nodict"
    if re.search(rf"^\s*{FO_WALL}\s*$", text, re.M):
        return "present"
    m = re.search(r"^functions\s*\n\{\s*\n", text, re.M)
    if not m:
        return "nodict"
    new = text[:m.end()] + block.lstrip("\n") + text[m.end():]
    tmp = c.with_name(c.name + ".studio-live")
    tmp.write_text(new, "utf-8")
    os.replace(tmp, c)
    return "added"


# --------------------------------------------------------------------------
# Читання: те саме, що Results, але з кількох .vtp
# --------------------------------------------------------------------------

class LiveResults:
    """Проміжний результат у вигляді, який розуміє вкладка «Результати».

    Повторює ту частину `fs_post.Results`, якою користується вкладка:
    `times`, `patch`, `patch_names`, `internal`, `fields`. Тому малювання
    (VTK зараз, foamview потім) не знає, звідки прийшли дані.

    Обʼєму тут НЕМАЄ — є лише середня площина. Щоб зріз на вкладці показав
    її, `internal` повертає тонкий шар призм, видавлених із площини на
    крихту в обидва боки: площина зрізу по Y крізь нього дає саму площину.
    """

    live = True

    def __init__(self, folders: "dict[float, str]",
                 info: "dict[float, LiveTime] | None" = None, cfg=None):
        self.folders = dict(folders)
        self.info = dict(info or {})
        # Налаштування САМОГО прогону (патчі тіла, течія для Cp). Вкладка
        # малює проміжний результат з ними, а не з тим, що відкрито у вікні:
        # після «Підключитися» у вікні може стояти зовсім інший проєкт або
        # порожній, і тоді вона шукала патч «body», якого в прогоні немає
        # (2026-11-11, case_jet: «У результатах немає патчів: body»).
        self.cfg = cfg
        self._cache: dict[tuple[float, str], object] = {}
        self._slab: tuple[float, object] | None = None
        self._flow: tuple[float, tuple] | None = None

    @property
    def times(self) -> "list[float]":
        return sorted(self.folders)

    def _near(self, time_: float) -> float:
        have = self.times
        if not have:
            return time_
        return min(have, key=lambda x: abs(x - time_))

    def _file(self, t: float, name: str) -> "Path | None":
        folder = Path(self.folders[t])
        f = folder / f"{name}.vtp"
        if f.is_file():
            return f
        # Тека поверхонь прогону, видима звідси напряму: площина лежить у
        # сусідній функції.
        if folder.parent.name == FO_WALL:
            g = folder.parent.parent / FO_SLICE / folder.name / f"{name}.vtp"
            if g.is_file():
                return g
        return None

    def _flow_file(self, t: float) -> "Path | None":
        folder = Path(self.folders[t])
        for f in (folder / FLOW_FILE,
                  folder.parent.parent / FO_FLOW / folder.name / FLOW_FILE):
            if f.is_file():
                return f
        return None

    def stream_volume(self, time_: float):
        """Обʼєм для ліній потоку. → (сітка, крок вибірки в м) або (None, 0).

        Є 3D-вибірка (`studioLiveFlow`) — рівна сітка з неї; крок > 0. Нема —
        лише середня площина: шар із неї, але швидкість у ньому ПЛОСКА (без
        складової по нормалі), і в field_data[PLANE_MARK] — (вісь, позиція).
        Інакше лінія за кілька сантиметрів виходить із шару й обривається
        (2026-11-11, case_jet: 6 ліній зі 1000 насінин).
        """
        if not self.folders:
            return None, 0.0
        t = self._near(time_)
        if self._flow is not None and self._flow[0] == t:
            return self._flow[1]
        self._flow = None
        got: tuple = (None, 0.0)
        f = self._flow_file(t)
        if f is not None:
            try:
                got = read_flow(f)
            except Exception:                               # noqa: BLE001
                got = (None, 0.0)
        if got[0] is None:
            plane = self._read(t, SLICE_NAME)
            if plane is not None and "U" in plane.cell_data:
                got = (in_plane(slab(close_pinholes(plane))), 0.0)
        self._flow = (t, got)
        return got

    def patch_names(self, time_: float) -> "list[str]":
        t = self._near(time_)
        try:
            return sorted(f.stem for f in Path(self.folders[t]).glob("*.vtp")
                          if f.stem != SLICE_NAME)
        except (OSError, KeyError):
            return []

    def patch(self, time_: float, name: str):
        if not self.folders:
            return None
        t = self._near(time_)
        key = (t, name)
        if key not in self._cache:
            self._cache[key] = self._read(t, name)
        return self._cache[key]

    def _read(self, t: float, name: str):
        import pyvista as pv
        f = self._file(t, name)
        if f is None:
            return None
        try:
            mesh = pv.read(str(f))
        except Exception:
            return None
        if not isinstance(mesh, pv.PolyData) or not mesh.n_cells:
            return None
        # Читач OpenFOAM дає і точкові, і комірчасті масиви; вкладка
        # згладжує тіло по точках, а розбір по гранях бере комірчасті.
        try:
            mesh = mesh.cell_data_to_point_data(pass_cell_data=True)
        except Exception:
            pass
        for k in list(mesh.field_data.keys()):
            del mesh.field_data[k]
        return mesh

    def internal(self, time_: float):
        if not self.folders:
            return None
        t = self._near(time_)
        if self._slab is not None and self._slab[0] == t:
            return self._slab[1]
        self._slab = None
        plane = self._read(t, SLICE_NAME)
        grid = slab(plane) if plane is not None else None
        self._slab = (t, grid)
        return grid

    @staticmethod
    def fields(mesh) -> "list[str]":
        import fs_post
        return fs_post.Results.fields(mesh)


def slab(plane, normal: int = 1):
    """Тонкий шар призм із трикутників площини (нормаль — вісь `normal`).

    Товщина — тисячна частка габариту площини: зріз по цій самій осі
    крізь середину шару дає ту саму площину з тими самими значеннями.
    """
    import pyvista as pv
    tri = plane.triangulate()
    faces = np.asarray(tri.faces).reshape(-1, 4)[:, 1:]
    pts = np.asarray(tri.points, dtype=float)
    b = tri.bounds
    span = max(b[1] - b[0], b[3] - b[2], b[5] - b[4], 1e-9)
    h = 1e-3 * span
    off = np.zeros(3)
    off[normal] = h
    n = len(pts)
    allp = np.vstack([pts - off, pts + off])
    cells = np.hstack([np.full((len(faces), 1), 6), faces, faces + n])
    kinds = np.full(len(faces), pv.CellType.WEDGE, dtype=np.uint8)
    grid = pv.UnstructuredGrid(cells.ravel(), kinds, allp)
    for k in tri.cell_data.keys():
        grid.cell_data[k] = np.asarray(tri.cell_data[k])
    try:
        grid = grid.cell_data_to_point_data(pass_cell_data=True)
    except Exception:
        pass
    return grid


# Мітка в field_data: швидкість є лише в одній площині — (вісь, позиція).
PLANE_MARK = "fs_stream_plane"


def in_plane(grid, normal: int = 1):
    """Копія шару, де швидкість лежить у площині (складова по нормалі = 0).

    Лінія в такому шарі не виходить із нього, тож доходить до кінця. Це
    лінії ПЛОСКОЇ проєкції швидкості, а не справжні 3D-лінії: вікно так і
    каже.
    """
    out = grid.copy()
    for store in (out.point_data, out.cell_data):
        if "U" in store:
            u = np.array(store["U"], dtype=float)
            u[:, normal] = 0.0
            store["U"] = u
    b = grid.bounds
    out.field_data[PLANE_MARK] = np.array(
        [float(normal), 0.5 * (b[2 * normal] + b[2 * normal + 1])])
    return out


def close_pinholes(plane, max_points: int = 8):
    """Закрити дрібні дірки в площині — пропущені трикутники розрізу.

    cuttingPlane (interpolate false) губить окремі трикутники: на площині
    case_jet (22 тис. трикутників) таких дірок 20, по 3–5 вершин. Лінія
    потоку, що влучає в таку дірку, обривається «поза доменом». Дірки з
    кількома вершинами закриваємо віялом трикутників зі значеннями —
    середніми по вершинах. Великі петлі (переріз тіла) не чіпаємо.
    """
    import pyvista as pv
    tri = plane.triangulate().clean()
    faces = np.asarray(tri.faces).reshape(-1, 4)[:, 1:]
    if not len(faces):
        return tri
    e = np.sort(np.vstack([faces[:, [0, 1]], faces[:, [1, 2]],
                           faces[:, [2, 0]]]), axis=1)
    uniq, count = np.unique(e, axis=0, return_counts=True)
    nb: dict[int, list] = {}
    for a, b in uniq[count == 1]:
        nb.setdefault(int(a), []).append(int(b))
        nb.setdefault(int(b), []).append(int(a))
    new, seen = [], set()
    for start in list(nb):
        if start in seen:
            continue
        loop, prev, cur, ok = [start], -1, start, True
        seen.add(start)
        while True:
            nxt = [p for p in nb[cur] if p != prev]
            if len(nb[cur]) != 2 or not nxt:
                ok = False
                break
            prev, cur = cur, nxt[0]
            if cur == start:
                break
            if cur in seen or len(loop) > max_points:
                ok = False
                break
            seen.add(cur)
            loop.append(cur)
        if ok and 3 <= len(loop) <= max_points:
            new += [(loop[0], loop[i], loop[i + 1])
                    for i in range(1, len(loop) - 1)]
    if not new:
        return tri
    add = np.asarray(new, dtype=np.int64)
    allf = np.vstack([faces, add])
    out = pv.PolyData(np.asarray(tri.points),
                      np.hstack([np.full((len(allf), 1), 3), allf]).ravel())
    for k in tri.cell_data.keys():
        old = np.asarray(tri.cell_data[k])
        if k in tri.point_data:
            extra = np.asarray(tri.point_data[k])[add].mean(axis=1)
        else:
            extra = np.repeat(old[:1] * 0, len(add), axis=0)
        out.cell_data[k] = np.concatenate([old, extra.astype(old.dtype)])
    return out


def read_flow(path) -> tuple:
    """3D-вибірка `studioLiveFlow` (raw: x y z Ux Uy Uz, gzip) → (сітка, крок).

    Точки лежать у вузлах рівної ґратки, але тих, що в тілі чи поза
    доменом, у файлі немає. Ґратку відновлюємо з координат; лишаємо лише
    комірки, усі 8 вершин яких є, — тож на місці тіла в сітці дірка, і лінія
    на ній зупиняється, а не проходить крізь тіло.
    """
    import gzip
    import pyvista as pv
    with gzip.open(path, "rt", encoding="ascii", errors="replace") as fh:
        text = "".join(ln for ln in fh if not ln.lstrip().startswith("#"))
    data = np.array(text.split(), dtype=float)
    if data.size < 6 * 8:
        return None, 0.0
    data = data[: data.size // 6 * 6].reshape(-1, 6)
    xyz, u = data[:, :3], data[:, 3:]
    lo = xyz.min(axis=0)
    step = np.zeros(3)
    for a in range(3):
        v = np.unique(np.round(xyz[:, a], 7))
        d = np.diff(v)
        d = d[d > 1e-9]
        step[a] = float(np.median(d)) if d.size else 1.0
    idx = np.rint((xyz - lo) / step).astype(int)
    dims = idx.max(axis=0) + 1
    if np.any(dims < 2):
        return None, 0.0
    have = np.zeros(dims[::-1], dtype=bool)          # [k, j, i]: x — найшвидше
    vel = np.zeros(tuple(dims[::-1]) + (3,))
    have[idx[:, 2], idx[:, 1], idx[:, 0]] = True
    vel[idx[:, 2], idx[:, 1], idx[:, 0]] = u
    img = pv.ImageData(dimensions=tuple(int(n) for n in dims),
                       spacing=tuple(step), origin=tuple(lo))
    img.point_data["U"] = vel.reshape(-1, 3)
    ok = np.ones(tuple(dims[::-1] - 1), dtype=bool)
    for dk in (0, 1):
        for dj in (0, 1):
            for di in (0, 1):
                ok &= have[dk:dk + dims[2] - 1, dj:dj + dims[1] - 1,
                           di:di + dims[0] - 1]
    cells = np.flatnonzero(ok.ravel())
    if not cells.size:
        return None, 0.0
    grid = img.extract_cells(cells)
    return grid, float(step.max())


def run_cfg(case_dir: str, fallback=None, names: "list[str] | None" = None):
    """Налаштування прогону для показу його проміжного результату.

    Перше — маніфест кейсу (studio.json), з якого прогін пустили. Якщо його
    немає, береться копія `fallback` (налаштування вікна), а коли її патчі
    тіла не збігаються з поверхнями прогону (`names`), тілом стають самі
    поверхні: показати чесно намальоване тіло краще, ніж порожній екран.
    """
    import copy
    import fs_case as fc
    import fs_library as lib
    try:
        data = (lib.read_manifest(case_dir) or {}).get("cfg") if case_dir \
            else None
        if data:
            return lib.cfg_from_dict(data)
    except Exception:                                       # noqa: BLE001
        pass
    if fallback is None:
        return None
    cfg = copy.deepcopy(fallback)
    have = {b.name for b in cfg.body_patches}
    if names and not have & set(names):
        cfg.bodies = [fc.BodyPatch(n, f"{n}.stl") for n in names]
        cfg.split_patches = len(names) > 1
    return cfg


def _whole(name: str) -> bool:
    try:
        return float(name).is_integer()
    except ValueError:
        return False


def label(lt: LiveTime, steady: bool) -> str:
    """Підпис моменту для списку.

    «Ітерація» — лише коли й справді стаціонар (імена тек — цілі числа):
    0.0195 — це секунди, а не номер ітерації.
    """
    if steady and _whole(lt.name):
        head = tr("iteration {t}", t=lt.name)
    elif lt.index >= 0:
        head = tr("t = {t} s (step {i})", t=lt.name, i=lt.index)
    else:
        head = tr("t = {t} s", t=lt.name)
    if lt.surface:
        size = tr("{mb:.1f} MB", mb=lt.size / 1e6)
        return tr("{head} · surfaces, {size}", head=head, size=size)
    return tr("{head} · from the full time step (slower)", head=head)
