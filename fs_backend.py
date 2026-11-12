"""Пошук OpenFOAM і запуск кейсу.

Підтримуються три способи запуску на Windows:

  WSL       — wsl.exe -d <distro> -e bash -c "source .../etc/bashrc && ..."
  blueCFD   — нативний порт, запуск через його setvars.bat
  Docker    — контейнер з примонтованим каталогом кейсу

Кейс завжди лежить на диску Windows, але у WSL його можна скопіювати в
рідну файлову систему: робота через /mnt/x іде по 9p і на файлових операціях
(decomposePar, reconstructPar, запис часових зрізів) втрачає в рази.
"""

from __future__ import annotations

import codecs
import hashlib
import json
import math
import os
import queue
import re
import shutil
import tarfile
import tempfile
import subprocess
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path, PureWindowsPath
from fs_i18n import tr
import fs_caseguard as guard
import fs_coeffs as coeffs
import fs_paths

CREATE_NO_WINDOW = 0x08000000 if os.name == "nt" else 0


def wsl_argv(distro: str, script: str) -> list[str]:
    """argv для виконання bash-скрипта всередині WSL.

    Обовʼязково `-e`, а не `--`. При `wsl.exe -d D -- bash -c "..."` рядок
    проходить ще через один шар оболонки, який розкриває змінні ДО того, як
    їх побачить bash. Наслідок непомітний і підступний: $HOME і $$ виживають
    (їх розкриває той самий зовнішній шар), а власні змінні скрипта й змінні
    циклу перетворюються на порожні рядки:

        wsl -d Ubuntu -- bash -c 'X=hello; echo "[$X]"'   ->  []
        wsl -d Ubuntu -e bash -c 'X=hello; echo "[$X]"'   ->  [hello]

    `-e` виконує програму напряму, без проміжної оболонки.
    """
    return ["wsl.exe", "-d", distro, "-e", "bash", "-c", script]


def _run(args: list[str], timeout: int = 25) -> tuple[int, str]:
    try:
        p = subprocess.run(args, capture_output=True, timeout=timeout,
                           creationflags=CREATE_NO_WINDOW)
    except (OSError, subprocess.SubprocessError):
        return (1, "")
    out = p.stdout.decode("utf-8", "replace") + p.stderr.decode("utf-8", "replace")
    # wsl.exe любить віддавати UTF-16LE
    if out.count("\x00") > len(out) // 4:
        out = p.stdout.decode("utf-16-le", "replace")
    return (p.returncode, out)


def win_to_wsl(path: str) -> str:
    """D:\\work\\case -> /mnt/d/work/case"""
    p = PureWindowsPath(os.path.abspath(path))
    drive = p.drive.rstrip(":").lower()
    rest = "/".join(p.parts[1:])
    return f"/mnt/{drive}/{rest}" if drive else str(p).replace("\\", "/")


def wsl_to_unc(distro: str, path: str) -> str:
    """/home/user/case -> \\\\wsl.localhost\\Ubuntu\\home\\user\\case

    Потрібно, щоб читати файли всередині WSL прямо з Windows — наприклад,
    щоб малювати коефіцієнти під час прогону, коли кейс лежить у рідній ФС.
    """
    return "\\\\wsl.localhost\\" + distro + path.replace("/", "\\")


# --------------------------------------------------------------------------
# Опис бекенду
# --------------------------------------------------------------------------

# Імена процесів OpenFOAM, які варто ловити: розвʼязувачі й побудова сітки.
# pgrep -f бере РОЗШИРЕНИЙ регулярний вираз, тому альтернатива через |.
FOAM_PROCS = ("simpleFoam|rhoSimpleFoam|pimpleFoam|rhoPimpleFoam"
              "|snappyHexMesh|potentialFoam|blockMesh|checkMesh|topoSet"
              "|refineHexMesh|foamdist")


# BatchMode вимикає будь-які запити пароля: краще чесний збій за секунду,
# ніж прогін, що мовчки завис на невидимому запиті. ControlMaster тримає
# одне зʼєднання на всі кроки — інакше кожен крок платив би за новий
# рукостискання й перевірку ключа.
SSH_OPTS = ("-o", "BatchMode=yes", "-o", "ConnectTimeout=15",
            "-o", "ServerAliveInterval=30")


def ssh_argv(host: str, command: str) -> list[str]:
    return ["ssh", *SSH_OPTS, host, command]


# --------------------------------------------------------------------------
# OpenFOAM in a container on a server (docker or podman)
# --------------------------------------------------------------------------
#
# The same way as the distributed run (rust/foamdist, Engine::run): each
# OpenFOAM command runs in `<engine> run --rm` of the image, with the
# OpenFOAM bashrc of the image. Differences from foamdist:
# - The folder $HOME/openfoam-studio is mounted at the SAME path, and the
#   container starts in the current folder. Thus the paths in the commands
#   (the case, the mesh store) are the same inside and outside, and
#   `signal_script` finds the processes by their working folder (the
#   container processes are processes of this user on the server).
# - HOME stays the home folder of the user (foamdist uses /tmp).

CONTAINER_ENGINES = ("docker", "podman")
# The folder of the program on a server and in WSL (fs_paths.REMOTE_ROOT).
REMOTE_ROOT = fs_paths.REMOTE_ROOT
CONTAINER_ROOT = REMOTE_ROOT
CONTAINER_BASHRC = "/usr/lib/openfoam/openfoam*/etc/bashrc"
CONTAINER_LABEL = "openfoam-studio=1"
# hwloc in the image looks for GPUs through X and hangs without a display
# (the foamdist trap, rust/foamdist/src/main.rs HWLOC).
CONTAINER_HWLOC = "HWLOC_COMPONENTS=-gl,-opencl,-cuda,-nvml,-rsmi,-levelzero"


def _shq(text: str) -> str:
    """Single quotes for bash."""
    return "'" + text.replace("'", "'\\''") + "'"


def container_run(engine: str, image: str, script: str) -> str:
    """bash: run `script` (bash text) in `image`, in the current folder.

    The bashrc of the image comes first. The current folder must be inside
    CONTAINER_ROOT (the cases of the program are there).
    """
    if engine not in CONTAINER_ENGINES:
        raise ValueError(engine)
    if engine == "podman":
        user = "--userns=keep-id"
    else:
        # docker: the files stay the files of the user; ssh and MPI need a
        # passwd entry for the uid (podman keep-id adds one, docker not).
        user = ('--user "$(id -u):$(id -g)" -v /etc/passwd:/etc/passwd:ro '
                '-v /etc/group:/etc/group:ro')
    inner = f"source {CONTAINER_BASHRC} >/dev/null 2>&1; {script}"
    return (f'{engine} run --rm --network=host --ipc=host '
            f'--label {CONTAINER_LABEL} {user} -e HOME="$HOME" '
            f'-e {CONTAINER_HWLOC} '
            f'-v "{CONTAINER_ROOT}:{CONTAINER_ROOT}:z" -w "$PWD" '
            f'--entrypoint bash {_shq(image)} -c {_shq(inner)}')


def parse_container(text: str) -> tuple[str, str]:
    """"docker|image" -> ("docker", "image"). ("", "") if not valid."""
    engine, _, image = (text or "").partition("|")
    engine, image = engine.strip(), image.strip()
    if engine in CONTAINER_ENGINES and re.match(r"^[\w./:@-]+$", image):
        return engine, image
    return "", ""


def containers_setting(text: str) -> dict:
    """{host: "engine|image"} from the setting «ssh_containers» (JSON)."""
    try:
        data = json.loads(text or "{}")
    except ValueError:
        return {}
    if not isinstance(data, dict):
        return {}
    return {str(k): str(v) for k, v in data.items()
            if parse_container(str(v))[0]}


def container_name(case_dir: str) -> str:
    """Стале імʼя контейнера для цього кейсу.

    Хеш, а не сам шлях: у docker імʼя обмежене набором символів, а шлях
    містить двокрапку, зворотні скісні й будь-що ще.
    """
    digest = hashlib.sha1(os.path.abspath(case_dir).encode("utf-8")).hexdigest()
    return f"openfoam-studio-{digest[:12]}"


def signal_script(target: str, sig: str) -> str:
    """bash, який шле сигнал СВОЇМ процесам OpenFOAM у каталозі `target`.

    Розрізняємо за робочим каталогом процесу, а не за іменем: pkill за
    іменем зачепив би й сусідній прогін, якщо користувач рахує два кейси
    паралельно або має щось своє в тому ж дистрибутиві.

    Сигнал шлеться ГРУПІ процесів (-PID), бо mpirun розгалужується: зупинити
    саму лише голову означає лишити гілки рахувати далі.
    """
    # Розподілений прогін (foamdist): його процеси в контейнерах і на інших
    # машинах, за текою їх не знайти. Поки він іде, у кейсі лежить скрипт,
    # що прибирає їх усі (його пише foamdist і сам видаляє наприкінці).
    dist = (f'[ -x "{target}/foamdist-clean.sh" ] && '
            f'"{target}/foamdist-clean.sh" >/dev/null 2>&1; '
            if sig == "TERM" else "")
    return (
        dist +
        f'n=0; for p in $(pgrep -f "{FOAM_PROCS}"); do '
        'cwd=$(readlink "/proc/$p/cwd" 2>/dev/null); '
        f'case "$cwd" in "{target}"*) '
        f'kill -{sig} "$p" 2>/dev/null && n=$((n+1));; esac; '
        'done; echo "$n"'
    )


# --------------------------------------------------------------------------
# Мʼяка зупинка: спершу зріз, потім кінець
# --------------------------------------------------------------------------
#
# «Стоп» шле SIGTERM, а OpenFOAM на нього НІЧОГО не пише: сигнали запису
# (writeNowSignal, stopAtWriteNowSignal в etc/controlDict) типово вимкнені,
# -1. Тож після «Стоп» лишається лише останній ЗАПЛАНОВАНИЙ зріз — а на
# нестаціонарі з writeInterval 0.001 це може бути година рахунку тому. І той
# зріз лежить у processor*/, незібраний: сценарій гине раніше, ніж дійде до
# reconstructPar (2026-11-09, case_jet: записано 0.001, пораховано 0.00137).
#
# Тому мʼяка зупинка йде не сигналом, а тим самим шляхом, що й будь-яка
# правка на льоту: `stopAt writeNow` у system/controlDict. runTimeModifiable
# перечитує словник на початку кожного кроку, розвʼязувач дописує ПОТОЧНИЙ
# момент і виходить з кодом 0. У паралельному прогоні словник читає лише
# голова (timeStampMaster) і розсилає решті — тому це має працювати й для
# foamdist, де інші машини мають свої копії system/.
#
# Позначка studio/softstop каже сценарію: далі не рахувати (другий етап
# теплого старту інакше взяв би controlDict.stage2 і рахував далі), а лише
# зібрати зріз і перевірити його. У позначці лежить старе значення stopAt,
# щоб повернути його: інакше наступний прогін без етапів зупинився б на
# першому ж кроці.

SOFT_FLAG = "softstop"
# Хто справді РАХУЄ. Сітку (snappyHexMesh) і reconstructPar мʼякою зупинкою
# не спинити: їм ні до чого stopAt, і зрізу, який варто зберегти, ще немає.
# Велика літера теж: rhoPimpleFoam — це «PimpleFoam», і pgrep без неї його
# не бачив (тест на підставних розвʼязувачах це й зловив).
SOLVER_PROCS = "[sSpP]impleFoam|potentialFoam|foamdist"

_STOPAT_RE = r"^([[:space:]]*stopAt[[:space:]]+)[A-Za-z]+;"


def soft_stop_script(target: str) -> str:
    """bash: попросити розвʼязувач у теці `target` записати зріз і вийти.

    Друкує один рядок-відповідь: `soft <старе stopAt>` — прийнято;
    `nosolver` — у цій теці зараз ніхто не рахує; `nodict` — немає
    system/controlDict або в ньому немає рядка stopAt.

    Пастка, на якій тут уже різались: `pgrep -f` із шаблоном, що стоїть і в
    рядку САМОГО цього bash. Тому — без `cd` у теку (інакше наш bash мав би
    той самий робочий каталог і знайшов би сам себе) і з явним викиданням
    `$$`.
    """
    c = f'"{target}/system/controlDict"'
    flag = f'"{target}/{STUDIO}/{SOFT_FLAG}"'
    return (
        'me=$$; n=0; '
        f'for p in $(pgrep -f "{SOLVER_PROCS}"); do '
        '[ "$p" = "$me" ] && continue; '
        'cwd=$(readlink "/proc/$p/cwd" 2>/dev/null); '
        f'case "$cwd" in "{target}"*) n=$((n+1));; esac; '
        'done; '
        '[ "$n" -gt 0 ] || { echo nosolver; exit 0; }; '
        f'[ -f {c} ] || {{ echo nodict; exit 0; }}; '
        "old=$(sed -nE "
        "'s/^[[:space:]]*stopAt[[:space:]]+([A-Za-z]+);.*/\\1/p' "
        f"{c} | head -1); "
        '[ -n "$old" ] || { echo nodict; exit 0; }; '
        f'mkdir -p "{target}/{STUDIO}"; '
        # Друге натискання не має затерти справжнє старе значення словом
        # writeNow — тоді повертати не було б чого.
        f'{{ [ "$old" = writeNow ] && [ -s {flag} ]; }} || '
        f'printf "%s\\n" "$old" > {flag}; '
        f"sed -i -E 's/{_STOPAT_RE}/\\1writeNow;/' {c}; "
        f"grep -qE '^[[:space:]]*stopAt[[:space:]]+writeNow;' {c} "
        '&& echo "soft $old" || echo nodict'
    )


def soft_restore_command() -> str:
    """bash (у теці кейсу): повернути stopAt, яке стояло до мʼякої зупинки."""
    flag = f'"{STUDIO}/{SOFT_FLAG}"'
    return (
        f'old=$(head -1 {flag} 2>/dev/null); '
        '[ -n "$old" ] && [ "$old" != writeNow ] && '
        f"sed -i -E 's/{_STOPAT_RE}/\\1'\"$old\"';/' system/controlDict; true"
    )


def soft_check_command() -> str:
    """bash (у теці кейсу): чи справді є зріз, який можна відкрити.

    Перевіряємо там, де його читатиме програма: у КОРЕНІ кейсу (після
    reconstructPar). Зріз без поля швидкости — не зріз.
    """
    return (
        'shopt -s nullglob; t=""; '
        'for d in [0-9]*/; do n=${d%/}; '
        'case "$n" in ""|*[!0-9.]*) continue;; esac; '
        '[ "$n" = 0 ] && continue; '
        'if [ -z "$t" ] || awk -v a="$n" -v b="$t" "BEGIN{exit !(a+0>b+0)}"; '
        'then t=$n; fi; done; '
        f'[ -n "$t" ] || {{ echo "{MARK} snap=none"; exit 1; }}; '
        '{ [ -s "$t/U" ] || [ -s "$t/U.gz" ]; } || '
        f'{{ echo "{MARK} snap=noU time=$t"; exit 1; }}; '
        f'echo "{MARK} snap=ok time=$t fields=$(ls "$t" | wc -l) '
        'size=$(du -sh "$t" | cut -f1)"'
    )


def soft_stop_file(case_dir: str) -> str:
    """Те саме, що `soft_stop_script`, але Python-ом по файлу на цьому
    компʼютері — для Docker і BlueCFD, де кейс лежить тут же. Процесів тут
    не перевірити, тож відповідь лише `soft …` або `nodict`."""
    c = Path(case_dir) / "system" / "controlDict"
    try:
        text = c.read_text(encoding="utf-8")
    except OSError:
        return "nodict"
    m = re.search(r"^(\s*stopAt\s+)([A-Za-z]+);", text, re.M)
    if not m:
        return "nodict"
    old = m.group(2)
    flag = Path(case_dir) / STUDIO / SOFT_FLAG
    flag.parent.mkdir(parents=True, exist_ok=True)
    if not (old == "writeNow" and flag.exists()
            and flag.read_text(encoding="utf-8").strip()):
        flag.write_text(old + "\n", encoding="utf-8")
    c.write_text(text[:m.start(2)] + "writeNow" + text[m.end(2):],
                 encoding="utf-8")
    return f"soft {old}"


def soft_check_local(case_dir: str) -> tuple[bool, str]:
    """`soft_check_command` Python-ом — для BlueCFD, де немає bash."""
    root = Path(case_dir)
    import fs_timeline as ftl                 # the rule of a time folder (T51)
    times = ftl.time_folders(root)
    if not times:
        return (False, tr("no time directory in the case root"))
    d = root / times[-1][1]
    if not any((d / f).is_file() and (d / f).stat().st_size
               for f in ("U", "U.gz")):
        return (False, tr("time directory {name} has no U field", name=d.name))
    return (True, tr("time directory {name}: {n_list} fields", name=d.name, n_list=len(list(d.iterdir()))))


def soft_restore_file(case_dir: str) -> None:
    flag = Path(case_dir) / STUDIO / SOFT_FLAG
    c = Path(case_dir) / "system" / "controlDict"
    try:
        old = flag.read_text(encoding="utf-8").strip()
        text = c.read_text(encoding="utf-8")
    except OSError:
        return
    if old and old != "writeNow":
        c.write_text(re.sub(r"^(\s*stopAt\s+)[A-Za-z]+;", rf"\g<1>{old};",
                            text, count=1, flags=re.M), encoding="utf-8")


@dataclass
class Backend:
    kind: str                 # wsl | bluecfd | docker | native | ssh
    label: str
    version: str = ""
    distro: str = ""
    bashrc: str = ""
    setvars: str = ""
    image: str = ""
    host: str = ""            # для ssh: імʼя з ~/.ssh/config або user@host
    # ssh only: OpenFOAM in a container on the server ("" = native install).
    engine: str = ""          # docker | podman
    ready: bool = True
    detail: str = ""

    @property
    def id(self) -> str:
        """Stable name of this backend that does not depend on the language.

        The session and the settings keep this, not `label`: the label is
        for the person and changes with the interface language (and with
        the OpenFOAM version on a server).
        """
        if self.kind == "wsl":
            return f"wsl:{self.distro}"
        if self.kind == "ssh":
            return f"ssh:{self.host}"
        if self.kind == "docker":
            return f"docker:{self.image}"
        if self.kind == "bluecfd":
            return f"bluecfd:{self.version}"
        return self.kind

    # --- побудова команди ------------------------------------------------

    def work_dir(self, case_dir: str, native_dir: str | None = None) -> str:
        """The case folder as the shell of this backend sees it.

        WSL: `native_dir` (the copy in the native file system) or the
        Windows folder through /mnt. ssh: `native_dir` (the folder on the
        server); without it the folder of the same name in REMOTE_ROOT, so
        that the probes can make a command without a case. Other kinds: the
        case folder itself.
        """
        if self.kind == "wsl":
            return native_dir or win_to_wsl(case_dir)
        if self.kind == "ssh":
            return native_dir or remote_dir_for(Path(case_dir).name)
        return os.path.abspath(case_dir)

    def shell(self, case_dir: str, command: str, native_dir: str | None = None) -> list[str]:
        """Повертає argv для subprocess, що виконає `command` у каталозі кейсу."""
        if self.kind == "wsl":
            wd = self.work_dir(case_dir, native_dir)
            inner = f'source "{self.bashrc}" && cd "{wd}" && {command}'
            return wsl_argv(self.distro, inner)

        if self.kind == "ssh":
            # Каталог на СЕРВЕРІ. Шлях Windows там не існує, тому native_dir
            # тут не зручність, а обовʼязок; запасний варіант потрібен лише
            # щоб розпізнавання й перевірки могли скласти команду без кейсу.
            wd = self.work_dir(case_dir, native_dir)
            if self.engine:
                inner = (f'cd "{wd}" && '
                         + container_run(self.engine, self.image, command))
            else:
                inner = f'source "{self.bashrc}" && cd "{wd}" && {command}'
            return ssh_argv(self.host, inner)

        if self.kind == "bluecfd":
            inner = f'call "{self.setvars}" && cd /d "{case_dir}" && {command}'
            return ["cmd.exe", "/c", inner]

        if self.kind == "docker":
            wd = "/case"
            return [
                "docker", "run", "--rm",
                # Імʼя потрібне, щоб контейнер можна було поставити на паузу:
                # docker pause адресується саме за ним. Виводиться з каталогу
                # кейсу, тому два різні кейси не б'ються за одне імʼя.
                "--name", container_name(case_dir),
                "-v", f"{os.path.abspath(case_dir)}:{wd}",
                "-w", wd, self.image, "bash", "-c",
                f'source /usr/lib/openfoam/openfoam*/etc/bashrc 2>/dev/null; {command}',
            ]

        return ["bash", "-c", f'cd "{case_dir}" && {command}']

    def pause_argv(self, case_dir: str, native_dir: str | None = None,
                   resume: bool = False) -> list[str] | None:
        """argv, який завмирає (або відживляє) прогін цього кейсу.

        None означає «цей бекенд так не вміє» — тоді інтерфейс просто не
        показує кнопку, замість того щоб показати й нічого не зробити.
        """
        sig = "CONT" if resume else "STOP"
        if self.kind == "wsl":
            return wsl_argv(self.distro, signal_script(
                self.work_dir(case_dir, native_dir), sig))
        if self.kind == "docker":
            verb = "unpause" if resume else "pause"
            return ["docker", verb, container_name(case_dir)]
        if self.kind == "ssh":
            return ssh_argv(self.host, signal_script(
                self.work_dir(case_dir, native_dir), sig))
        if self.kind == "native":
            return ["bash", "-c", signal_script(self.work_dir(case_dir), sig)]
        # BlueCFD — це Windows-збірка, і сигналів STOP/CONT там немає.
        return None

    def can_pause(self) -> bool:
        return self.kind in ("wsl", "docker", "native", "ssh")

    def soft_stop_argv(self, case_dir: str,
                       native_dir: str | None = None) -> list[str] | None:
        """argv мʼякої зупинки (див. `soft_stop_script`). None — кейс лежить
        тут же, і правити словник треба Python-ом (`soft_stop_file`)."""
        if self.kind == "wsl":
            return wsl_argv(self.distro, soft_stop_script(
                self.work_dir(case_dir, native_dir)))
        if self.kind == "ssh":
            return ssh_argv(self.host, soft_stop_script(
                self.work_dir(case_dir, native_dir)))
        if self.kind == "native":
            return ["bash", "-c", soft_stop_script(self.work_dir(case_dir))]
        return None

    def mpi(self, n: int, app: str, args: str = "") -> str:
        if n <= 1:
            return f"{app} {args}".strip()
        tail = f"{args} -parallel".strip()
        if self.kind == "bluecfd":
            return f"mpiexec -n {n} {app} {tail}"
        return f"mpirun -np {n} --oversubscribe {app} {tail}"


# --------------------------------------------------------------------------
# Детект
# --------------------------------------------------------------------------

FOAM_BASHRC_GLOBS = (
    "/usr/lib/openfoam/openfoam*/etc/bashrc",
    "/opt/openfoam*/etc/bashrc",
    "/opt/OpenFOAM-*/etc/bashrc",
    "$HOME/OpenFOAM/OpenFOAM-*/etc/bashrc",
)

# nullglob прибирає шаблони, що ні з чим не збіглися. Без нього bash лишає
# шаблон як літерал, і будь-яка спроба обійти це через `ls`/`eval` дає
# порожній токен на кожен незбіжний глоб.
_PROBE = (
    "shopt -s nullglob; for f in " + " ".join(FOAM_BASHRC_GLOBS)
    + "; do echo \"FOUND:$f\"; done"
)


def detect_wsl() -> list[Backend]:
    rc, out = _run(["wsl.exe", "-l", "-q"])
    if rc != 0:
        return []
    distros = [d.strip() for d in out.replace("\x00", "").splitlines() if d.strip()]
    found: list[Backend] = []
    for d in distros:
        rc, out = _run(wsl_argv(d, _PROBE), timeout=60)
        paths = [ln[6:].strip() for ln in out.splitlines() if ln.startswith("FOUND:")]
        paths = [p for p in paths if p.startswith("/")]
        if not paths:
            found.append(Backend(kind="wsl", label=f"WSL / {d}", distro=d,
                                 ready=False,
                                 detail="distribution found, OpenFOAM not found"))
            continue
        bashrc = sorted(paths)[-1]
        rc, ver = _run(
            wsl_argv(d, f'source "{bashrc}" >/dev/null 2>&1 '
                        f'&& echo "V:$WM_PROJECT_VERSION"'),
            timeout=60)
        version = "?"
        for ln in ver.splitlines():
            if ln.startswith("V:") and ln[2:].strip():
                version = ln[2:].strip()
        found.append(Backend(kind="wsl", label=f"WSL / {d}", distro=d,
                             bashrc=bashrc, version=version,
                             ready=version != "?", detail=bashrc))
    return found


def detect_bluecfd() -> list[Backend]:
    out: list[Backend] = []
    for root in (r"C:\Program Files", r"C:\Program Files (x86)", "C:\\"):
        try:
            entries = list(Path(root).glob("blueCFD*"))
        except OSError:
            continue
        for e in entries:
            for sv in list(e.glob("setvars.bat")) + list(e.glob("*/setvars.bat")):
                out.append(Backend(kind="bluecfd", label=f"blueCFD-Core ({e.name})",
                                   setvars=str(sv), version=e.name, detail=str(sv)))
                break
    return out


def detect_docker() -> list[Backend]:
    rc, out = _run(["docker", "--version"])
    if rc != 0:
        return []
    rc, imgs = _run(["docker", "images", "--format", "{{.Repository}}:{{.Tag}}"])
    cands = [ln.strip() for ln in imgs.splitlines()
             if "openfoam" in ln.lower() or "foam" in ln.lower()]
    if not cands:
        return [Backend(kind="docker", label="Docker (no OpenFOAM image)",
                        ready=False, detail=out.strip().splitlines()[0]
                        if out.strip() else "docker found")]
    return [Backend(kind="docker", label=f"Docker / {c}", image=c, detail=c)
            for c in cands]


# Server side: the OpenFOAM images of docker and podman, "engine|image".
_IMAGES_PROBE = (
    "for e in podman docker; do command -v $e >/dev/null 2>&1 || continue; "
    "timeout 20 $e images --format '{{.Repository}}:{{.Tag}}' 2>/dev/null "
    "| grep -i openfoam | sed \"s/^/$e|/\"; done")


def pick_container(lines: "list[str]") -> str:
    """The best "engine|image" of a list: the run image first (smaller,
    the one foamdist uses), then default, then dev. "" if none."""
    cands = [ln.strip() for ln in lines if parse_container(ln.strip())[0]]

    def rank(c: str) -> tuple:
        img = c.split("|", 1)[1]
        kind = (0 if "openfoam-run" in img else 1 if "openfoam-default" in img
                else 2 if "openfoam-dev" in img else 3)
        return (kind, c.split("|", 1)[0] != "podman", img)
    return sorted(cands, key=rank)[0] if cands else ""


def image_version(image: str) -> str:
    """"opencfd/openfoam-run:2512" -> "v2512" (the tag), else the tag."""
    tag = image.rsplit(":", 1)[-1] if ":" in image.rsplit("/", 1)[-1] else ""
    return ("v" + tag) if re.match(r"^\d{4}$", tag) else (tag or "?")


def _container_backend(host: str, container: str) -> Backend:
    """A server with OpenFOAM in a container: the image must be there."""
    engine, image = parse_container(container)
    check = (f"if {engine} image inspect {_shq(image)} >/dev/null 2>&1; "
             "then echo IMG=yes; else echo IMG=no; fi; nproc")
    rc, text = _run(ssh_argv(host, check), timeout=25)
    lines = [ln.strip() for ln in text.strip().splitlines() if ln.strip()]
    if rc != 0 or "IMG=yes" not in lines:
        why = ("no connection" if rc != 0 and "IMG=no" not in lines
               else tr("the image {image} is not on the server", image=image))
        return Backend(kind="ssh", label=tr("Server {host}", host=host),
                       host=host, engine=engine, image=image, ready=False,
                       detail=(lines[-1][:120] if rc != 0 and lines else why))
    version = image_version(image)
    cores = lines[-1] if lines[-1].isdigit() else "?"
    return Backend(
        kind="ssh", host=host, engine=engine, image=image, version=version,
        bashrc=CONTAINER_BASHRC,
        label=tr("Server {host} / {version} ({engine})", host=host,
                 version=version, engine=engine),
        detail=tr("{engine} image {image}, cores {cores}", engine=engine,
                  image=image, cores=cores))


def detect_ssh(hosts: "list[str] | tuple[str, ...]" = (),
               containers: "dict | None" = None) -> list[Backend]:
    """Сервери зі списку, на яких знайшовся OpenFOAM.

    Недоступний сервер повертається теж, але з ready=False і поясненням:
    мовчки зникнути зі списку — найгірше, що може зробити налаштований
    користувачем пункт, бо шукати причину тоді ніде.

    `containers` ({host: "engine|image"}, the setting «ssh_containers» that
    the server wizard writes): that server runs OpenFOAM in that image. A
    server without such an entry and without a native OpenFOAM: the program
    takes an OpenFOAM image of docker or podman, if the server has one.
    """
    containers = containers or {}
    out: list[Backend] = []
    for host in hosts:
        host = (host or "").strip()
        if not host:
            continue
        if host in containers:
            out.append(_container_backend(host, containers[host]))
            continue
        probe = ("for g in " + " ".join(FOAM_BASHRC_GLOBS) + "; do "
                 '[ -f "$g" ] && echo "$g" && break; done')
        rc, text = _run(ssh_argv(host, f"shopt -s nullglob; {probe}"),
                        timeout=25)
        if rc != 0:
            out.append(Backend(kind="ssh", label=tr("Server {host}", host=host), host=host,
                               ready=False,
                               detail=(text.strip().splitlines() or
                                       ["no connection"])[-1][:120]))
            continue
        bashrc = (text.strip().splitlines() or [""])[-1].strip()
        if not bashrc:
            rc, imgs = _run(ssh_argv(host, _IMAGES_PROBE), timeout=45)
            best = pick_container(imgs.splitlines()) if rc == 0 else ""
            if best:
                out.append(_container_backend(host, best))
                continue
            out.append(Backend(kind="ssh", label=tr("Server {host}", host=host), host=host,
                               ready=False,
                               detail="connection OK, but OpenFOAM not found"))
            continue
        rc, ver = _run(ssh_argv(
            host, f'source "{bashrc}" >/dev/null 2>&1; '
                  'echo "$WM_PROJECT_VERSION"; nproc'), timeout=25)
        lines = [l.strip() for l in ver.strip().splitlines() if l.strip()]
        version = lines[0] if lines else ""
        cores = lines[1] if len(lines) > 1 else "?"
        out.append(Backend(kind="ssh", label=tr("Server {host} / {version}", host=host, version=version),
                           host=host, bashrc=bashrc, version=version,
                           detail=tr("{bashrc}, cores {cores}", bashrc=bashrc, cores=cores)))
    return out


def detect_all(ssh_hosts: "list[str] | tuple[str, ...]" = (),
               progress=None, containers: "dict | None" = None) -> list[Backend]:
    """Усі бекенди: WSL, blueCFD, Docker і сервери зі списку.

    `progress(name, i, n)` (за бажання) кличеться ПЕРЕД кожною перевіркою:
    вікно показує, яку машину зараз питаємо. Перевірки довгі (SSH до 25 с
    на сервер), і без цього вікно виглядало б завислим.
    """
    hosts = [h.strip() for h in ssh_hosts if (h or "").strip()]
    steps = [("WSL", detect_wsl), ("blueCFD", detect_bluecfd),
             ("Docker", detect_docker)]
    steps += [(h, lambda h=h: detect_ssh([h], containers)) for h in hosts]
    found: list[Backend] = []
    for i, (name, check) in enumerate(steps, 1):
        if progress is not None:
            progress(name, i, len(steps))
        found += check()
    found.sort(key=lambda b: (not b.ready, b.kind != "wsl"))
    return found


BACKEND_KINDS = ("wsl", "ssh", "docker", "bluecfd", "native")
# Server labels of older versions, in each interface language
# ("Сервер {host}", "Server {host}"). The host is the first word after it.
_LEGACY_SERVER = re.compile(r"^(?:Сервер|Server)\s+(\S+)")


def is_backend_id(text: str) -> bool:
    kind = (text or "").split(":", 1)[0]
    return kind in BACKEND_KINDS and (":" in text or kind == "native")


def backend_id_from_label(label: str) -> str:
    """The `Backend.id` for a label that an older version stored.

    Older versions kept the label (for a server: "Сервер myserver / v2512"
    or, in English, "Server myserver / v2512"). The id comes from the part of
    the label that does not change with the language. "" if unknown.
    """
    t = (label or "").strip()
    for mark in _LEGACY_NOT_READY:
        if t.endswith(mark):
            t = t[:-len(mark)].strip()
    if is_backend_id(t):
        return t
    if t.startswith("WSL / "):
        return "wsl:" + t[len("WSL / "):].strip()
    if t.startswith("Docker / "):
        return "docker:" + t[len("Docker / "):].strip()
    if t.startswith("Docker ("):
        return "docker:"
    if t.startswith("blueCFD-Core (") and t.endswith(")"):
        return "bluecfd:" + t[len("blueCFD-Core ("):-1]
    m = _LEGACY_SERVER.match(t)
    if m:
        return "ssh:" + m.group(1)
    return ""


def backend_matches(backend: "Backend | None", want: str) -> bool:
    """True if `want` (an id, or a label of an older version) names `backend`."""
    if backend is None or not want:
        return False
    return backend.id == backend_id_from_label(want)


# Версію не зашиваємо: репозиторій ESI поповнюється двічі на рік. Але просто
# взяти найновішу не можна — там регулярно лежить release candidate
# (напр. 2606.0~rc2), тому кандидати з ~rc / ~beta відкидаємо.
INSTALL_SCRIPT = (
    "curl -fsSL https://dl.openfoam.com/add-debian-repo.sh | sudo bash && "
    "sudo apt-get update && "
    "PKG=$(apt-cache search openfoam | grep -oE '^openfoam[0-9]+-default' "
    "| sort -Vr | while read p; do "
    "v=$(apt-cache policy \"$p\" | awk '/Candidate:/{print $2}'); "
    "case \"$v\" in *~rc*|*~beta*|*~alpha*) continue;; esac; "
    "echo \"$p\"; break; done) && "
    'echo "Ставлю $PKG" && '
    'sudo apt-get install -y "$PKG"'
)


def install_command(distro: str = "Ubuntu") -> tuple[list[str], str]:
    """argv, що відкриє консольне вікно WSL і поставить OpenFOAM.

    Виконується інтерактивно, бо apt попросить пароль sudo — підставити його
    з програми неможливо й не треба.
    """
    argv = ["cmd.exe", "/c", "start", "", "wsl.exe", "-d", distro, "-e",
            "bash", "-lic", f"{INSTALL_SCRIPT}; echo; echo ГОТОВО; exec bash"]
    return argv, INSTALL_SCRIPT


# --------------------------------------------------------------------------
# Кроки прогону
# --------------------------------------------------------------------------

@dataclass
class Step:
    name: str
    command: str
    weight: float = 1.0        # приблизна частка часу, для індикатора прогресу
    optional: bool = False
    # Чи це САМ розвʼязувач. Раніше це визначалося збігом назви кроку з
    # «simpleFoam», і поява rhoSimpleFoam мовчки зламала б скидання рядів
    # резидуалів: назва інша, умова не спрацьовує, графік показує суміш.
    solver: bool = False
    # Перший етап двоетапного прогону: ряди перед ним треба скинути, а перед
    # другим — ні, бо він продовжує ту саму історію.
    first_stage: bool = False
    # Крок, що лишається й після мʼякої зупинки: збирання зрізу. Решту
    # (наступні етапи розвʼязувача) після неї пропускаємо.
    keep_on_soft: bool = False
    # Збережена сітка (`mesh_cache_steps`): "miss" — крок лише тоді, коли
    # збереженої сітки немає (будування сітки); "hit" — лише коли є
    # (узяти її). Порожньо — завжди. Ознака — файл studio/mesh_hit у кейсі,
    # його ставить крок перевірки: так умова однаково працює і в сценарії на
    # сервері, і у виконавці у вікні.
    cond: str = ""


MESH_HIT_FLAG = "mesh_hit"


# --------------------------------------------------------------------------
# Step names and log markers that do not depend on the interface language
# --------------------------------------------------------------------------
#
# The program reads back what the run script and the step commands write
# (run.log, studio/status, studio/step). Thus these texts are a PROTOCOL, not
# texts for the person: they are ASCII and they never change with the
# interface language. The person sees `step_title()` and `render_line()`,
# which translate them with fs_i18n.tr().
#
# A step name is an id: an OpenFOAM tool name ("snappyHexMesh"), one of
# STEP_TITLES, or "<solver>-warm" / "<solver>-stable". The log file of a
# step is logs/<id>.log.
#
# A marker is one line "@@FS key=value key=value ..." (values without
# spaces):
#     @@FS step=<id>                         a step starts (header)
#     @@FS done                              the script is at its end
#     @@FS optional step=<id> rc=<n>         an optional step failed
#     @@FS mesh=skip|miss key=<k>            stored mesh: not used / none
#     @@FS mesh=hit key=<k> cells=<n> date=<YYYY-MM-DDTHH:MM>
#     @@FS mesh=stored key=<k> size=<du -sh>
#     @@FS snap=none | snap=noU time=<t> | snap=ok time=<t> fields=<n> size=<s>
#     @@FS fail=rc rc=<n> | fail=negvol | fail=skew value=<v>
#          | fail=nonortho value=<v>       (reason in studio/status)
#
# Versions before 2026-11-11 wrote Ukrainian texts here («===== очищення
# =====», «є збережена сітка …»). A run that such a version started on a
# server can still be attached: `step_heads` reads both header forms and
# `step_id` maps the old Ukrainian step names to the ids.

MARK = "@@FS"
STEP_DONE = "done"
STEP_CLEAN = "clean"
STEP_MESH_CHECK = "meshCacheCheck"
STEP_MESH_LOAD = "meshCacheLoad"
STEP_MESH_STORE = "meshCacheStore"
STEP_PROMOTE = "promoteToZero"
STEP_SOFT_CHECK = "softCheck"
WARM_SUFFIX = "-warm"
STABLE_SUFFIX = "-stable"

# id -> the title that the person sees (step_title translates it).
STEP_TITLES = {
    STEP_CLEAN: "cleanup",
    STEP_MESH_CHECK: "saved mesh?",
    STEP_MESH_LOAD: "mesh from saved",
    STEP_MESH_STORE: "save mesh",
    STEP_PROMOTE: "field → time 0",
    STEP_SOFT_CHECK: "time step check",
    STEP_DONE: "done",
}
# Old Ukrainian step names (in logs, studio/step and studio/status of runs
# that an older version started) -> id. They are data of older versions, not
# texts of this one: they stay Ukrainian (a _LEGACY name: the i18n tools
# leave them as they are).
_LEGACY_STEPS = {
    "очищення": STEP_CLEAN,
    "збережена сітка?": STEP_MESH_CHECK,
    "сітка зі збереженої": STEP_MESH_LOAD,
    "зберегти сітку": STEP_MESH_STORE,
    "поле → момент 0": STEP_PROMOTE,
    "перевірка зрізу": STEP_SOFT_CHECK,
    "готово": STEP_DONE,
}
_LEGACY_SOFT = "зупинено зі зрізом"     # the status text of older versions
_LEGACY_NOT_READY = ("  (не готовий)", "  (not ready)")
_LEGACY_RC = re.compile(r"^код (\d+)$")
_LEGACY_WARM = "-теплий старт"
_LEGACY_STABLE = "-стійкий"


def step_id(name: str) -> str:
    """The id of a step name; an old Ukrainian name gives its id."""
    name = (name or "").strip()
    if name in _LEGACY_STEPS:
        return _LEGACY_STEPS[name]
    if name.endswith(_LEGACY_WARM):
        return name[:-len(_LEGACY_WARM)] + WARM_SUFFIX
    if name.endswith(_LEGACY_STABLE):
        return name[:-len(_LEGACY_STABLE)] + STABLE_SUFFIX
    return name


def step_title(name: str) -> str:
    """What the person sees for a step (id or old name), translated."""
    sid = step_id(name)
    if sid in STEP_TITLES:
        return tr(STEP_TITLES[sid])
    if sid.endswith(WARM_SUFFIX):
        return tr("{solver}-warm start", solver=sid[:-len(WARM_SUFFIX)])
    if sid.endswith(STABLE_SUFFIX):
        return tr("{solver}-stable", solver=sid[:-len(STABLE_SUFFIX)])
    return sid


def step_banner(name: str) -> str:
    """The header line of a step in the log on the screen."""
    return f"===== {step_title(name)} ====="


def parse_marker(line: str) -> "dict[str, str] | None":
    """{key: value} of a marker line, or None if `line` is not a marker."""
    if not line.startswith(MARK):
        return None
    rest = line[len(MARK):]
    if rest and not rest[0].isspace():
        return None
    out: dict[str, str] = {}
    for tok in rest.split():
        k, _, v = tok.partition("=")
        out[k] = v
    return out


def _snap_text(m: dict) -> str:
    snap = m.get("snap", "")
    if snap == "none":
        return tr("no time directory in the case root")
    if snap == "noU":
        return tr("time directory {name} has no U field", name=m.get("time", "?"))
    return tr("time directory {name}: {n_list} fields, {size}", name=m.get("time", "?"),
              n_list=m.get("fields", "?"), size=m.get("size", "?"))


def _fail_text(m: dict) -> str:
    why = m.get("fail", "")
    if why == "rc":
        return tr("code {rc}", rc=m.get("rc", "?"))
    if why == "negvol":
        return tr("the mesh has cells with zero or negative volume")
    if why == "skew":
        return tr("skewness {v}: the solver will almost certainly fail "
                  "on this mesh", v=m.get("value", "?"))
    if why == "nonortho":
        return tr("non-orthogonality {v}", v=m.get("value", "?"))
    return why


def render_marker(m: dict, raw: str = "") -> str:
    """The text for the person of a parsed marker (translated)."""
    if "step" in m and len(m) == 1:
        return step_banner(m["step"])
    if STEP_DONE in m:
        return step_banner(STEP_DONE)
    if "optional" in m:
        return tr("[{name}] optional step, continuing (code {rc})",
                  name=step_title(m.get("step", "")), rc=m.get("rc", "?"))
    mesh = m.get("mesh")
    if mesh == "skip":
        return tr("the saved mesh is not used: a new mesh was requested")
    if mesh == "miss":
        return tr("no saved mesh {key}: building the mesh", key=m.get("key", ""))
    if mesh == "hit":
        return tr("saved mesh {key}: {cells} cells, from {date}; no need "
                  "to build", key=m.get("key", ""),
                  cells=m.get("cells", "?"),
                  date=m.get("date", "?").replace("T", " "))
    if mesh == "stored":
        return tr("mesh saved: {key}, {size}", key=m.get("key", ""),
                  size=m.get("size", "?"))
    if "snap" in m:
        return _snap_text(m)
    if "fail" in m:
        return _fail_text(m)
    return raw


def render_line(line: str) -> str:
    """`line` for the screen: a marker becomes a translated text."""
    if not line.startswith(MARK):
        return line
    m = parse_marker(line)
    return line if m is None else render_marker(m, line)


def render_text(text: str) -> str:
    """`render_line` for each line of `text` (lines joined with "\\n")."""
    if MARK not in text:
        return text
    return "\n".join(render_line(ln) for ln in text.split("\n"))


def reason_text(step: str, why: str) -> str:
    """The reason of a failed run (studio/status) for the person.

    New scripts write a marker (`fail=…`, or the `snap=…` line of the slice
    check); older scripts wrote a Ukrainian text, which `tr` looks up.
    """
    why = (why or "").strip()
    m = parse_marker(why)
    if m is None:
        old = _LEGACY_RC.match(why)          # «код 137» of an older script
        return tr("code {rc}", rc=old.group(1)) if old else tr(why)
    text = render_marker(m, why)
    if step_id(step) == STEP_SOFT_CHECK and "snap" in m:
        return tr("soft stop: the time step was not written ({v})", v=text)
    return text


def soft_slice_text(detail: str) -> str:
    """The slice of a soft stop for the person, from the last line of the
    slice check (a marker), or from the status text of an older version
    («зупинено зі зрізом — зріз 0.01: 7 полів, 12M»)."""
    detail = (detail or "").strip()
    if detail.startswith(MARK):
        return render_line(detail)
    if detail.startswith(_LEGACY_SOFT) and "—" in detail:
        return detail.split("—", 1)[1].strip()
    return tr(detail)


def soft_done_text(slice_text: str) -> str:
    """The final message of a soft stop that wrote a slice."""
    return tr("stopped with time step written — {last}", last=slice_text)


def cond_test(cond: str) -> str:
    """bash-умова (у теці кейсу) для Step.cond."""
    f = f'"{STUDIO}/{MESH_HIT_FLAG}"'
    return {"hit": f"[ -e {f} ]", "miss": f"[ ! -e {f} ]"}.get(cond, "true")


def _clean_command(backend: Backend) -> str:
    """Прибирає результати попереднього прогону.

    Без цього postProcessing накопичує файли від усіх запусків: OpenFOAM при
    рестарті не перезаписує coefficient.dat, а відкладає старий у
    coefficient_0.dat. Читач бере обидва, і показані Cd/Cl виходять сумішшю
    різних розрахунків — помітити це майже неможливо, бо числа виглядають
    правдоподібно.
    """
    if backend.kind == "bluecfd":
        return ('if exist postProcessing rmdir /s /q postProcessing & '
                'if exist logs rmdir /s /q logs & echo cleaned')
    # Числові каталоги часу, крім 0 і 0.orig; сітку в constant не чіпаємо.
    # Перевірка на суто числове імʼя обовʼязкова: шаблон [0-9]* сам по собі
    # збігся б і з якимось «3d_model» у каталозі кейсу, і ми б його стерли.
    # Саме [0-9]*, а не [1-9]*: моменти нестаціонару — це «0.001»,
    # «0.00163459», і шаблон з одиниці їх оминав, тож старі зрізи лишалися
    # поруч із новими.
    return (
        "rm -rf postProcessing && shopt -s nullglob && "
        "for d in [0-9]*/ processor*/[0-9]*/; do "
        'n=${d%/}; n=${n##*/}; '
        'case "$n" in ""|0|*[!0-9.]*) continue;; esac; '
        'rm -rf "$d"; done; echo cleaned'
    )


def _restore_command(backend: Backend, parallel: bool) -> str:
    """Повертає 0.orig на місце 0 — синтаксис залежить від оболонки бекенду."""
    if backend.kind == "bluecfd":
        # blueCFD виконує команди в cmd.exe, а не в bash
        base = 'if exist 0 rmdir /s /q 0 & xcopy /E /I /Q /Y 0.orig 0'
        if parallel:
            base += (' & for /d %d in (processor*) do ('
                     'if exist %d\\0 rmdir /s /q %d\\0 & '
                     'xcopy /E /I /Q /Y 0.orig %d\\0 )')
        return base
    base = "rm -rf 0 && cp -r 0.orig 0"
    if parallel:
        # nullglob обовʼязковий: якщо каталогів processor* ще (або вже) немає,
        # bash лишає шаблон літералом, цикл виконується один раз зі шляхом
        # "processor*/0", і крок падає на cp.
        base += (" && shopt -s nullglob"
                 " && for d in processor*/; do"
                 ' rm -rf "$d/0" && cp -r 0.orig "$d/0"; done')
    return base


STAGE_FILES = (
    ("system/fvSchemes", "system/fvSchemes"),
    ("system/fvSolution", "system/fvSolution"),
    ("system/controlDict", "system/controlDict"),
    ("constant/fvOptions", "constant/fvOptions"),
)


def _use_stage(backend: Backend, tag: str) -> str:
    """Команда, що ставить на місце комплект словників потрібного етапу."""
    if backend.kind == "bluecfd":
        parts = [f'copy /Y "{d}.{tag}" "{d}"'.replace("/", "\\")
                 for _, d in STAGE_FILES]
        return " & ".join(parts)
    return " && ".join(f'cp "{d}.{tag}" "{d}"' for _, d in STAGE_FILES)


# The window knows that a run on the server lives without the laptop from
# Runner.on_event("detached", …), not from a log line (that text is
# translated). See Runner.on_event.


def promote_latest_command(backend: Backend, parallel: bool) -> str:
    """Останній записаний момент стає моментом 0 (теплий старт).

    `uniform/` викидається: там лежить час, з яким момент записано
    (номер ітерації стаціонарного етапу), і нестаціонар інакше почав би з
    t = 300 с замість нуля. phi лишається: це масовий потік стаціонарного
    етапу, якраз той, що чекає rhoPimpleFoam.
    """
    one = ('last=$(cd "$d" && ls -d [0-9]* 2>/dev/null | sort -g | tail -1); '
           'if [ -n "$last" ] && [ "$last" != 0 ]; then '
           'rm -rf "$d/0" && mv "$d/$last" "$d/0" && rm -rf "$d/0/uniform"; fi')
    if backend.kind == "bluecfd":
        # Windows-збірка: той самий сенс, але без bash. Теплий старт тут
        # рідкість, тож простий варіант — через PowerShell.
        ps = ("$dirs = @('.') + (Get-ChildItem -Directory processor* | "
              "% FullName); foreach ($d in $dirs) { $t = Get-ChildItem "
              "-Directory $d | ? { $_.Name -match '^[0-9.eE+-]+$' } | "
              "Sort-Object { [double]$_.Name } | Select -Last 1; if ($t -and "
              "$t.Name -ne '0') { Remove-Item -Recurse -Force (Join-Path $d "
              "'0') -ErrorAction SilentlyContinue; Rename-Item $t.FullName '0'; "
              "Remove-Item -Recurse -Force (Join-Path $d '0\\uniform') "
              "-ErrorAction SilentlyContinue } }")
        return f'powershell -NoProfile -Command "{ps}"'
    where = "processor*/" if parallel else "."
    return f"shopt -s nullglob; for d in {where}; do {one}; done"


# --------------------------------------------------------------------------
# ЕКСПЕРИМЕНТАЛЬНО: один прогін на кількох машинах (rust/foamdist).
#
# Сітку, як завжди, будує сервер — одразу на стільки шматків, скільки разом
# процесів на всіх машинах. Лише крок розвʼязувача замість mpirun запускає
# foamdist: той роздає кожній машині її processor*, запускає mpirun у
# контейнері OpenFOAM на сервері (і по контейнеру на кожній машині) і
# збирає шматки назад. Далі reconstructPar іде звичайно.
# --------------------------------------------------------------------------

FOAMDIST_REMOTE = fs_paths.REMOTE_ROOT + "/bin/foamdist"
FOAMDIST_LOCAL = (Path(__file__).resolve().parent / "rust" / "foamdist" / "target"
                  / "x86_64-unknown-linux-musl" / "release" / "foamdist")


@dataclass
class DistMachine:
    """Одна машина розподіленого прогону.

    Користувач і ключ — СВОЇ для кожної: машини різних людей чи місць не
    мають спільного облікового запису. Порожньо — типові з панелі.
    `key` — шлях НА СЕРВЕРІ (з нього foamdist заходить на машину).
    """
    addr: str = ""
    procs: int = 4
    name: str = ""
    user: str = ""
    key: str = ""
    on: bool = True
    # Памʼять машини, ГБ: з `foamdist probe` («Перевірити машини», «Знайти
    # машини»). 0 — ще не питали; перевірка памʼяті тоді каже «невідома».
    ram_gb: float = 0.0

    def label(self) -> str:
        return self.name or self.addr

    def to_dict(self) -> dict:
        return {"name": self.name, "addr": self.addr, "procs": self.procs,
                "user": self.user, "key": self.key, "on": self.on,
                "ram_gb": self.ram_gb}

    @staticmethod
    def from_dict(d: dict) -> "DistMachine":
        try:
            procs = max(1, int(d.get("procs", 4)))
        except (TypeError, ValueError):
            procs = 4
        try:
            ram = max(0.0, float(d.get("ram_gb", 0.0) or 0.0))
        except (TypeError, ValueError):
            ram = 0.0
        return DistMachine(addr=str(d.get("addr", "")).strip(), procs=procs,
                           name=str(d.get("name", "")).strip(),
                           user=str(d.get("user", "")).strip(),
                           key=str(d.get("key", "")).strip(),
                           on=bool(d.get("on", True)), ram_gb=ram)


def probe_ram_gb(g: dict) -> float:
    """Памʼять машини з відповіді `foamdist probe`, ГБ; 0 — невідома.

    `mem_total_gb` (MemTotal) — у нових foamdist; старі дають лише
    `mem_gb` (MemAvailable у мить перевірки) — беремо її: вона менша,
    тобто обережніша."""
    for k in ("mem_total_gb", "mem_gb"):
        try:
            v = float((g or {}).get(k) or 0)
        except (TypeError, ValueError):
            v = 0.0
        if v > 0:
            return v
    return 0.0


_ADDR_RE = re.compile(r"[\w.\-]+")


def machines_to_json(ms: "list[DistMachine]") -> str:
    return json.dumps([m.to_dict() for m in ms], ensure_ascii=False)


def machines_from_json(text: str) -> "list[DistMachine]":
    try:
        raw = json.loads(text or "[]")
    except ValueError:
        return []
    return [DistMachine.from_dict(d) for d in raw if isinstance(d, dict)]


def machines_from_legacy(hosts_text: str) -> "list[DistMachine]":
    """Старий рядок «адреса:процеси, …» → записи машин (без назв)."""
    out = []
    for part in re.split(r"[,\s;]+", hosts_text or ""):
        m = re.fullmatch(r"([\w.\-]+):(\d+)", part.strip())
        if m and int(m.group(2)) > 0:
            out.append(DistMachine(addr=m.group(1), procs=int(m.group(2))))
    return out


def check_machines(ms: "list[DistMachine]") -> "tuple[list[DistMachine], str]":
    """Увімкнені машини і що з ними не так ("" — усе гаразд)."""
    on = [m for m in ms if m.on]
    seen = set()
    for m in on:
        if not m.addr or not _ADDR_RE.fullmatch(m.addr):
            return on, tr("“{v}”: address “{addr}” is not an address", v=m.label() or '?', addr=m.addr)
        if m.addr in seen:
            return on, tr("address {addr} twice", addr=m.addr)
        seen.add(m.addr)
        if m.procs < 1:
            return on, tr("“{label}”: zero processes", label=m.label())
        if m.user and not re.fullmatch(r"[\w.\-]+", m.user):
            return on, tr("“{label}”: user “{user}”", label=m.label(), user=m.user)
        if any(c in m.key for c in " ,='\"$`"):
            return on, f"«{m.label()}»: у шляху ключа не можна пробілів і ,='\"$`"
    if len(on) < 2:
        return on, tr("at least two enabled machines are necessary — otherwise this is a normal run")
    return on, ""


def _as_machines(hosts) -> "list[DistMachine]":
    """Старий вигляд [(адреса, процеси)] теж приймаємо."""
    return [h if isinstance(h, DistMachine) else DistMachine(addr=h[0], procs=h[1])
            for h in hosts]


def _dist_args(hosts, key: str, user: str, net: str = "") -> str:
    """--hosts [user@]адреса:процеси,… --key … --user … [--keys …] [--net …]"""
    ms = _as_machines(hosts)
    spec = ",".join(f"{m.user + '@' if m.user else ''}{m.addr}:{m.procs}"
                    for m in ms)
    own = ",".join(f"{m.addr}={m.key}" for m in ms if m.key and m.key != key)
    return (f"--hosts {spec} --key {key}" + (f" --user {user}" if user else "")
            + (f" --keys {own}" if own else "")
            + (f" --net {net}" if net else ""))


def parse_dist_hosts(text: str) -> tuple[list[tuple[str, int]], str]:
    """«10.0.0.20:8, 10.0.0.21:4» → ([(адреса, процеси)], помилка)."""
    out = []
    for part in re.split(r"[,\s;]+", text or ""):
        part = part.strip()
        if not part:
            continue
        m = re.fullmatch(r"([\w.\-]+):(\d+)", part)
        if not m:
            return [], (tr("“{part}”: address:processes is necessary, "
                           "for example 10.0.0.21:4", part=part))
        n = int(m.group(2))
        if n < 1:
            return [], tr("“{part}”: zero processes", part=part)
        out.append((m.group(1), n))
    if len(out) < 2:
        return out, tr("at least two machines are necessary — otherwise this is a normal run")
    return out, ""


def dist_command(hosts, key: str, user: str, net: str = "") -> str:
    """Початок команди foamdist для кроку розвʼязувача; імʼя розвʼязувача —
    останнім словом, його додає build_steps. `hosts` — DistMachine або
    [(адреса, процеси)]."""
    return (f'"{FOAMDIST_REMOTE}" run --case . --no-decompose --stream '
            + _dist_args(hosts, key, user, net) + " --solver")


def ensure_foamdist(backend: Backend, local: Path = FOAMDIST_LOCAL) -> str:
    """Покласти foamdist на сервер, якщо його там немає чи він інший.

    Повертає порожній рядок або пояснення, чому не вийшло.
    """
    if backend.kind != "ssh":
        return tr("distribution across machines — only for a run on a server")
    if not Path(local).is_file():
        return (tr("{local} is missing — build it: tools\\build_rust.ps1 "
                   "(it also builds foamdist for Linux)", local=local))
    data = Path(local).read_bytes()
    want = hashlib.sha256(data).hexdigest()
    rc, out = _run(ssh_argv(backend.host,
                            f'sha256sum "{FOAMDIST_REMOTE}" 2>/dev/null'), 30)
    if want in out:
        return ""
    cmd = (f'mkdir -p "$(dirname "{FOAMDIST_REMOTE}")" && '
           f'cat > "{FOAMDIST_REMOTE}.new" && chmod +x "{FOAMDIST_REMOTE}.new" '
           f'&& mv "{FOAMDIST_REMOTE}.new" "{FOAMDIST_REMOTE}"')
    try:
        p = subprocess.run(ssh_argv(backend.host, cmd), input=data,
                           capture_output=True, timeout=300,
                           creationflags=CREATE_NO_WINDOW)
    except (OSError, subprocess.SubprocessError) as exc:
        return tr("cannot put foamdist on the server: {exc}", exc=exc)
    if p.returncode != 0:
        return (tr("cannot put foamdist on the server: {decode}", decode=p.stderr.decode("utf-8", "replace")[-300:]))
    return ""


def has_foamdist(backend: Backend) -> bool:
    rc, _ = _run(ssh_argv(backend.host, f'test -x "{FOAMDIST_REMOTE}"'), 30)
    return rc == 0


def probe_dist(backend: Backend, hosts, key: str, user: str) -> list[dict]:
    """`foamdist probe` з сервера: по словнику на машину (з `hostname`)."""
    ms = _as_machines(hosts)
    addrs = " ".join(f"{m.user + '@' if m.user else ''}{m.addr}" for m in ms)
    own = ",".join(f"{m.addr}={m.key}" for m in ms if m.key and m.key != key)
    rc, out = _run(ssh_argv(backend.host,
                            f'"{FOAMDIST_REMOTE}" probe {addrs} --key {key} '
                            + (f"--user {user}" if user else "")
                            + (f" --keys {own}" if own else "")),
                   120)
    got = []
    for line in out.splitlines():
        line = line.strip()
        if line.startswith("{"):
            try:
                got.append(json.loads(line))
            except ValueError:
                pass
    return got


# Пошук машин: що бачить СЕРВЕР (машини розподілу ходять саме від нього).
# Кандидати — його локальні мережі (/24 навколо кожної адреси; мережі
# docker/podman/tailscale пропускаємо: там не машини, а контейнери чи весь
# світ) і хости з його ~/.ssh/config (там і назви, і користувачі). Порт 22
# перевіряємо всіх разом, по секунді на кожен — уся мережа за ~2 с.
_SCAN_SCRIPT = r'''
nets=$(ip -4 -o addr show scope global 2>/dev/null | awk '{print $2, $4}' \
  | grep -vE '^(docker|br-|veth|virbr|podman|tailscale|cni|flannel|lxc|zt)' \
  | awk '{print $2}')
cands=""
for n in $nets; do
  ip=${n%/*}; base=${ip%.*}; echo "SELF $ip $(hostname -s)"
  for i in $(seq 1 254); do cands="$cands $base.$i"; done
done
if [ -f ~/.ssh/config ]; then
  for a in $(awk 'tolower($1)=="host" { for (i=2;i<=NF;i++) print $i }' \
             ~/.ssh/config | grep -v '[*?!]' | sort -u); do
    g=$(ssh -G "$a" 2>/dev/null)
    h=$(printf '%s\n' "$g" | awk '$1=="hostname"{print $2}')
    u=$(printf '%s\n' "$g" | awk '$1=="user"{print $2}')
    echo "ALIAS $a $h $u"
    case " $cands " in *" $h "*) ;; *) cands="$cands $h";; esac
  done
fi
for ip in $cands; do
  ( timeout 1 bash -c ": </dev/tcp/$ip/22" 2>/dev/null && echo "OPEN $ip" ) &
done
wait
'''


def parse_scan(text: str) -> dict:
    """Вивід `_SCAN_SCRIPT` → {"self": [...], "open": [...], "alias": {адреса:
    (назва, користувач)}}."""
    out = {"self": [], "open": [], "alias": {}}
    for line in text.splitlines():
        bits = line.split()
        if len(bits) >= 2 and bits[0] == "SELF":
            out["self"].append(bits[1])
            # Сервер себе у своєму ~/.ssh/config не має: назву каже сам.
            if len(bits) >= 3:
                out["alias"].setdefault(bits[1], (bits[2], ""))
        elif len(bits) >= 2 and bits[0] == "OPEN":
            if bits[1] not in out["open"]:
                out["open"].append(bits[1])
        elif len(bits) >= 3 and bits[0] == "ALIAS":
            # Перша назва виграє: у config та сама адреса може мати кілька.
            out["alias"].setdefault(bits[2], (bits[1], bits[3] if len(bits) > 3
                                               else ""))
    return out


def scan_dist(backend: Backend, key: str, user: str) -> list[dict]:
    """Знайти машини, на які сервер може піти: мережа + ~/.ssh/config, потім
    `foamdist probe` усіх, у кого відкритий ssh. По словнику на машину:
    addr, name, user (з config), self (це сам сервер), і поля probe."""
    rc, out = _run(ssh_argv(backend.host, _SCAN_SCRIPT), 90)
    if rc != 0 and "OPEN" not in out:
        raise RuntimeError(out.strip()[-300:] or tr("the server did not reply"))
    sc = parse_scan(out)
    ms = []
    for addr in sc["open"]:
        name, u = sc["alias"].get(addr, ("", ""))
        ms.append(DistMachine(addr=addr, name=name,
                              user=u if u and u != user else ""))
    got = {g.get("host"): g for g in probe_dist(backend, ms, key, user)} if ms else {}
    found = []
    for m in ms:
        g = dict(got.get(m.addr) or {"ok": False, "error": "did not answer"})
        g.update(addr=m.addr, name=m.name or str(g.get("hostname") or ""),
                 user=m.user, self=m.addr in sc["self"] or g.get("head") == "yes")
        found.append(g)
    return found


def scan_to_machines(found: list[dict], existing: "list[DistMachine]") -> list:
    """Знахідки → [(DistMachine, що про неї сказати, готова, уже в списку)].

    Готова = ssh пускає, це Linux із ядрами і є образ OpenFOAM. Процесів
    пропонуємо стільки, скільки ФІЗИЧНИХ ядер: більше на розвʼязувачі не
    дає нічого, крім тісноти.
    """
    have = {m.addr for m in existing}
    rows = []
    for g in sorted(found, key=lambda g: (not g.get("self"), g.get("name") or "~",
                                          g.get("addr", ""))):
        cores = int(float(g.get("cores", 0) or 0))
        threads = int(float(g.get("threads", 0) or 0))
        m = DistMachine(addr=g["addr"], name=g.get("name", ""),
                        user=g.get("user", ""),
                        procs=cores or max(1, threads // 2) or 4,
                        ram_gb=probe_ram_gb(g) if g.get("ok") else 0.0)
        if not g.get("ok"):
            err = " ".join(str(g.get("error", "")).split())
            if "Permission denied" in err:
                why = tr("ssh does not accept the server key (different user or key)")
            elif "Connection refused" in err or "timed out" in err:
                why = tr("ssh does not reply")
            else:
                why = tr("failed: {err}", err=err[:120])
            ready = False
        elif cores <= 0:
            why, ready = "ssh accepts, but this does not look like a Linux machine", False
        elif g.get("image") != "yes":
            why = (tr("{cores} cores, but no OpenFOAM image (podman pull "
                      "docker.io/opencfd/openfoam-run:2512)", cores=cores))
            ready = False
        else:
            busy = tr(", load {get}", get=g.get('load')) if g.get("load") is not None else ""
            why = (tr("{v}{cores} cores / {threads} threads, {get} GB "
                      "free{busy}", v='server, ' if g.get('self') else '', cores=cores, threads=threads, get=g.get('mem_gb', '?'), busy=busy))
            ready = True
        rows.append((m, why, ready, m.addr in have))
    return rows


def dist_weights(backend: Backend, hosts, key: str,
                 user: str, rebench: bool = False) -> dict:
    """`foamdist weights` з сервера: швидкість кожної машини й ваги процесів.

    Бенчмарк кожної машини foamdist тримає в кеші на сервері
    (~/.cache/foamdist/bench.tsv) і повторює лише коли треба, тож зазвичай
    це секунди; перший раз або з `rebench` — до хвилини.
    {"hosts": [...], "weights": [...]} або {"error": "..."}.
    """
    rc, out = _run(ssh_argv(backend.host,
                            f'"{FOAMDIST_REMOTE}" weights '
                            + _dist_args(hosts, key, user)
                            + (" --rebench" if rebench else "")), 600)
    for line in reversed(out.splitlines()):
        line = line.strip()
        if line.startswith("{") and '"weights"' in line:
            try:
                return json.loads(line)
            except ValueError:
                break
    return {"error": out.strip()[-400:] or tr("foamdist weights: code {rc}", rc=rc)}


# --------------------------------------------------------------------------
# Збережена сітка
# --------------------------------------------------------------------------
#
# Сітка — найдовша частина підготовки: на case_jet snappyHexMesh іде ~10 хв, і
# доти кожен прогін будував її наново, навіть коли змінилася лише кількість
# процесів (20 → 24, бо додали машину) чи швидкість потоку. Тепер готова
# сітка лежить на тій машині, де рахують (`MESH_STORE`), під ключем із того,
# що її визначає: геометрія і словники сітки. Той самий ключ — сітка
# береться готова і лише ріжеться на потрібну кількість шматків.
#
# Зберігаємо ЗІБРАНУ сітку, а не шматки processor*: так її можна взяти на
# будь-яку кількість процесів. Пастка (перевірено на v2512):
# `reconstructParMesh -constant` губить рівні згущення (cellLevel,
# pointLevel, level0Edge) — і dynamicRefineFvMesh на взятій сітці вважав би
# усі комірки нульового рівня й згущував би заново. Повертає їх
# `reconstructPar -constant -no-fields`; decomposePar потім розносить їх
# по шматках сам.

MESH_STORE = fs_paths.REMOTE_ROOT + "/meshes"
MESH_KEEP = 12              # стільки останніх сіток лишаємо, решту прибираємо
MESH_INPUTS = ("system/blockMeshDict", "system/snappyHexMeshDict",
               "system/surfaceFeatureExtractDict", "system/meshQualityDict")


def mesh_key(case_dir: str) -> str:
    """Ключ сітки: геометрія + словники сітки. "" — нема з чого рахувати.

    *.eMesh у triSurface пропускаємо: їх пише surfaceFeatureExtract, і при
    прогоні на місці (WSL без копії) ключ мінявся б після першого ж запуску.
    """
    root = Path(case_dir)
    h = hashlib.sha256()
    got = False
    for rel in MESH_INPUTS:
        f = root / rel
        if f.is_file():
            h.update(rel.encode() + b"\0" + f.read_bytes() + b"\0")
            got = True
    tri = root / "constant" / "triSurface"
    if tri.is_dir():
        for f in sorted(tri.rglob("*")):
            if f.is_file() and f.suffix.lower() != ".emesh":
                h.update(f.relative_to(root).as_posix().encode() + b"\0")
                with open(f, "rb") as fh:
                    for chunk in iter(lambda: fh.read(1 << 20), b""):
                        h.update(chunk)
                got = True
    return h.hexdigest()[:16] if got else ""


def mesh_cache_supported(backend: Backend) -> bool:
    # Docker: $HOME контейнера не живе між прогонами. BlueCFD: cmd.exe.
    return backend.kind in ("ssh", "wsl", "native")


def _mesh_check_command(key: str, remesh: bool) -> str:
    c = f'"{MESH_STORE}/{key}"'
    flag = f'"{STUDIO}/{MESH_HIT_FLAG}"'
    if remesh:
        return (f'mkdir -p "{STUDIO}"; rm -f {flag}; '
                f'echo "{MARK} mesh=skip key={key}"')
    return (
        f'mkdir -p "{STUDIO}"; rm -f {flag}; '
        f'if [ -s {c}/polyMesh/faces ] && [ -s {c}/polyMesh/owner ]; then '
        f'touch {flag} {c}; '
        f'echo "{MARK} mesh=hit key={key} cells=$(grep -ao "nCells:[0-9]*" '
        f'{c}/polyMesh/owner | head -1 | cut -d: -f2) '
        f'date=$(date -r {c}/polyMesh/faces "+%Y-%m-%dT%H:%M")"; '
        f'else echo "{MARK} mesh=miss key={key}"; fi'
    )


def _mesh_load_command(key: str, par: bool) -> str:
    c = f'"{MESH_STORE}/{key}"'
    cmd = f'rm -rf constant/polyMesh && cp -r {c}/polyMesh constant/polyMesh'
    if par:
        # -copyZero, як і звичайний decomposePar: 0/ уже розкладено з 0.orig.
        cmd += " && decomposePar -copyZero -force"
    return cmd


def _mesh_store_command(key: str, par: bool) -> str:
    c = f'"{MESH_STORE}/{key}"'
    cmd = ""
    if par:
        cmd = ("reconstructParMesh -constant && "
               "reconstructPar -constant -no-fields && ")
    # Через тимчасову теку й mv: обірваний на півдорозі запис не має
    # виглядати готовою сіткою.
    cmd += (
        f'mkdir -p "{MESH_STORE}" && rm -rf {c}.part && mkdir -p {c}.part && '
        f'cp -r constant/polyMesh {c}.part/ && rm -rf {c}.part/polyMesh/sets && '
        f'rm -rf {c} && mv {c}.part {c} && '
        f'echo "{MARK} mesh=stored key={key} size=$(du -sh {c} | cut -f1)"; '
        f'cd "{MESH_STORE}" && ls -1dt */ 2>/dev/null | tail -n +{MESH_KEEP + 1} '
        '| while read -r d; do rm -rf "$d"; done; true'
    )
    return cmd


# Копія сховища на ЦЬОМУ компʼютері: звідси сітка йде у файл проєкту і з
# проєкту — назад на сервер. Сервер не вічний (теку кейсу перезаписує кожен
# прогін, сховище тримає лише MESH_KEEP останніх), а проєкт людина носить із
# собою.
# Next to the program in a source copy, in Documents in an installed one.
LOCAL_MESH_STORE = fs_paths.data_dir() / "meshes"


def _mesh_complete(d: Path) -> bool:
    pm = Path(d) / "polyMesh"
    return all(any((pm / f"{n}{z}").is_file() for z in ("", ".gz"))
               for n in ("points", "faces", "owner", "neighbour", "boundary"))


def local_mesh_dir(key: str) -> "Path | None":
    """Тека сітки `key` на цьому компʼютері (у ній polyMesh/) або None."""
    if not key:
        return None
    d = LOCAL_MESH_STORE / key
    return d if _mesh_complete(d) else None


def _store_argv(backend: Backend, script: str) -> "list[str] | None":
    """argv, що виконує bash там, де лежить сховище сіток цього бекенда."""
    if backend.kind == "ssh":
        return ssh_argv(backend.host, script)
    if backend.kind == "wsl":
        return wsl_argv(backend.distro, script)
    if backend.kind == "native":
        return ["bash", "-c", script]
    return None


def remote_mesh_exists(backend: Backend, key: str) -> bool:
    argv = _store_argv(backend, f'test -s "{MESH_STORE}/{key}/polyMesh/faces"')
    return bool(argv) and _run(argv, 30)[0] == 0


def fetch_mesh(backend: Backend, key: str) -> str:
    """Сховище бекенда → LOCAL_MESH_STORE/key. "" — вийшло, інакше чому ні.

    gzip на тому боці: сітка бінарна, але стискається приблизно вдвічі, а
    канал до сервера буває ~2 МБ/с — це різниця між хвилиною і двома.
    """
    argv = _store_argv(backend, f'cd "{MESH_STORE}" && test -s "{key}/polyMesh/faces" '
                                f'&& tar czf - "{key}"')
    if argv is None:
        return tr("this backend has no mesh store")
    LOCAL_MESH_STORE.mkdir(parents=True, exist_ok=True)
    tmp = LOCAL_MESH_STORE / f".in-{key}.tgz"
    part = LOCAL_MESH_STORE / f".in-{key}"
    rc, err = _pipe(argv, stdout_path=tmp, timeout=3600)
    try:
        if rc != 0:
            return tr("mesh not downloaded: {v}", v=err.strip()[-200:] or tr("code {rc}", rc=rc))
        shutil.rmtree(part, ignore_errors=True)
        part.mkdir()
        with tarfile.open(tmp, "r:gz") as tar:
            for m in tar.getmembers():
                # Лише звичайні файли й теки під <ключ>/ — архів приходить із
                # сервера, але розпаковувати «../» не годиться ніколи.
                if (not (m.isfile() or m.isdir()) or m.name.startswith("/")
                        or ".." in Path(m.name).parts
                        or Path(m.name).parts[0] != key):
                    return tr("strange entry in the mesh archive: {name}", name=m.name)
            tar.extractall(part)
        if not _mesh_complete(part / key):
            return tr("something is missing in the mesh archive")
        dst = LOCAL_MESH_STORE / key
        shutil.rmtree(dst, ignore_errors=True)
        os.replace(part / key, dst)
        return ""
    finally:
        tmp.unlink(missing_ok=True)
        shutil.rmtree(part, ignore_errors=True)


def push_mesh(backend: Backend, key: str) -> str:
    """LOCAL_MESH_STORE/key → сховище бекенда. "" — вийшло."""
    src = local_mesh_dir(key)
    if src is None:
        return tr("this computer does not have this mesh")
    argv = _store_argv(backend, (
        f'S="{MESH_STORE}"; mkdir -p "$S" && d="$S/.in-$$" && mkdir -p "$d" && '
        f'tar xzf - -C "$d" && test -s "$d/{key}/polyMesh/faces" && '
        f'rm -rf "$S/{key}" && mv "$d/{key}" "$S/{key}"; rc=$?; rm -rf "$d"; '
        'exit $rc'))
    if argv is None:
        return tr("this backend has no mesh store")
    tmp = LOCAL_MESH_STORE / f".out-{key}.tgz"
    try:
        with tarfile.open(tmp, "w:gz", compresslevel=6) as tar:
            tar.add(src, arcname=key)
        rc, err = _pipe(argv, stdin_path=tmp, timeout=3600)
        if rc != 0:
            return tr("mesh not uploaded: {v}", v=err.strip()[-200:] or tr("code {rc}", rc=rc))
        return ""
    finally:
        tmp.unlink(missing_ok=True)


def ensure_remote_mesh(backend: Backend, key: str) -> "str | None":
    """Put the saved mesh `key` of this computer into the mesh store of
    `backend`, if the store does not have it yet. One rule for «Run», the
    queue and `ofs` (T51): uploading a mesh takes minutes, building it again
    takes tens of minutes.

    None: nothing to do (this computer has no such mesh, or the store has it
    already). "": uploaded now. Other text: why the upload failed (the run
    then builds the mesh). An error of ssh comes to the caller as an
    exception.
    """
    if not key or local_mesh_dir(key) is None:
        return None
    if remote_mesh_exists(backend, key):
        return None
    return push_mesh(backend, key)


def build_steps(n_procs: int, backend: Backend, potential_init: bool,
                check_mesh: bool, reconstruct: bool = True,
                two_stage: bool = False,
                solver: str = "simpleFoam",
                topo_set: bool = False,
                warm: str = "", dist: str = "",
                mesh_key: str = "", remesh: bool = False) -> list[Step]:
    """Кроки прогону. `warm` — розвʼязувач теплого старту (CaseCfg.warm_start).
    `dist` — команда foamdist (dist_command): тоді кроки розвʼязувача йдуть
    на кількох машинах. `mesh_key` — ключ збереженої сітки (`mesh_key()`):
    якщо така сітка є, будування пропускається; `remesh` — будувати однаково
    (і зберегти нову)."""
    par = n_procs > 1
    cache = bool(mesh_key) and mesh_cache_supported(backend)
    build = "miss" if cache else ""

    def solve(app: str) -> str:
        return f"{dist} {app}" if dist else backend.mpi(n_procs, app)
    steps = [
        Step(STEP_CLEAN, _clean_command(backend), 0.01),
        # 0/ розкладається з еталона ще до decomposePar: -copyZero копіює саме
        # каталог 0/, а після попереднього прогону там може лишитися сміття.
        Step("restore0", _restore_command(backend, False), 0.01),
    ]
    if cache:
        steps.append(Step(STEP_MESH_CHECK,
                          _mesh_check_command(mesh_key, remesh), 0.005))
        steps.append(Step(STEP_MESH_LOAD,
                          _mesh_load_command(mesh_key, par), 0.03, cond="hit"))
    steps.append(Step("surfaceFeatureExtract", "surfaceFeatureExtract", 0.02,
                      cond=build))
    steps.append(Step("blockMesh", "blockMesh", 0.02, cond=build))
    if par:
        steps.append(Step("decomposePar", "decomposePar -copyZero -force", 0.03,
                          cond=build))
        steps.append(Step("snappyHexMesh", backend.mpi(n_procs, "snappyHexMesh",
                                                       "-overwrite"), 0.30,
                          cond=build))
    else:
        steps.append(Step("snappyHexMesh", "snappyHexMesh -overwrite", 0.30,
                          cond=build))

    if check_mesh:
        # Збережену сітку не перевіряємо вдруге: у сховище потрапляє лише
        # та, що пройшла перевірку.
        steps.append(Step("checkMesh",
                          backend.mpi(n_procs, "checkMesh", "-constant"),
                          0.03, optional=True, cond=build))
    if cache:
        # Необовʼязковий: не вдалося зберегти — рахуємо далі, просто
        # наступного разу сітку збудує знову.
        steps.append(Step(STEP_MESH_STORE, _mesh_store_command(mesh_key, par),
                          0.03, optional=True, cond="miss"))

    # snappyHexMesh -overwrite лишає поля 0/ прив'язаними до старої сітки,
    # тому розкладаємо еталон 0.orig наново. Поля задані рівномірно, тож
    # жодного перенесення не треба — OpenFOAM сам натягне їх на нову сітку.
    steps.append(Step("restore0", _restore_command(backend, par), 0.01))

    if topo_set:
        # Зони комірок нарізаються по ГОТОВІЙ сітці: до snappyHexMesh їх
        # різати нема по чому, а після — коробка вже знає, які саме комірки
        # в неї потрапили.
        steps.append(Step("topoSet", backend.mpi(n_procs, "topoSet"), 0.02))

    if potential_init:
        steps.append(Step("potentialFoam",
                          backend.mpi(n_procs, "potentialFoam", "-writephi"),
                          0.04, optional=True))

    if warm:
        # Теплий старт: стаціонарний етап, його поле — у момент 0, далі
        # нестаціонар зі своїми словниками. 0.orig між ними НЕ відновлюємо.
        steps.append(Step(
            warm + WARM_SUFFIX,
            _use_stage(backend, "stage1") + " && "
            + solve(warm), 0.15,
            solver=True, first_stage=True))
        steps.append(Step(STEP_PROMOTE,
                          promote_latest_command(backend, par), 0.01))
        steps.append(Step(
            solver,
            _use_stage(backend, "stage2") + " && "
            + solve(solver), 0.35, solver=True,
            first_stage=True))
    elif two_stage:
        # Між етапами 0.orig НЕ відновлюємо: другий етап має продовжити з поля,
        # яке дав перший (у controlDict стоїть startFrom latestTime).
        steps.append(Step(
            solver + STABLE_SUFFIX,
            _use_stage(backend, "stage1") + " && "
            + solve(solver), 0.25,
            solver=True, first_stage=True))
        steps.append(Step(
            solver,
            _use_stage(backend, "stage2") + " && "
            + solve(solver), 0.25, solver=True))
    else:
        steps.append(Step(solver, solve(solver), 0.50,
                          solver=True))

    if par and reconstruct:
        rpm = "reconstructParMesh -constant"
        if backend.kind != "bluecfd":
            # СІТКА, ЩО ЗМІНИЛАСЯ ПІД ЧАС РАХУНКУ (dynamicRefineFvMesh), лежить
            # у processor*/<час>/polyMesh, а не в constant. Тоді зібрати лише
            # constant замало: reconstructPar накладе поля нової сітки на
            # стару й упаде на розмірах. Тому, якщо в останньому моменті є
            # своя сітка, збираємо й її.
            rpm += (' && { t=$(cd processor0 && ls -d [0-9]* 2>/dev/null '
                    '| sort -g | tail -1); '
                    'if [ -n "$t" ] && ls "processor0/$t/polyMesh/faces"* '
                    '>/dev/null 2>&1; then reconstructParMesh -time "$t"; '
                    'fi; }')
        steps.append(Step("reconstructParMesh", rpm, 0.02, keep_on_soft=True))
        steps.append(Step("reconstructPar", "reconstructPar -latestTime", 0.03,
                          keep_on_soft=True))
    return steps


# --------------------------------------------------------------------------
# Розбір логу
# --------------------------------------------------------------------------

RE_TIME = re.compile(r"^Time = (\S+)")
RE_RESID = re.compile(
    r"Solving for (\w+), Initial residual = ([0-9.eE+-]+), "
    r"Final residual = ([0-9.eE+-]+), No Iterations (\d+)"
)
RE_CONTINUITY = re.compile(r"time step continuity errors.*sum local = ([0-9.eE+-]+)")
RE_CELLS = re.compile(r"^\s*cells:\s+(\d+)", re.M)
RE_SNAPPY_PHASE = re.compile(
    r"^(Refinement phase|Morph phase|Shrinking phase|Layer addition phase|"
    r"Doing final balancing|Finished meshing)"
)
RE_MESH_FAILED = re.compile(r"Failed (\d+) mesh check")
RE_MESH_OK = re.compile(r"^Mesh OK")
RE_SKEW = re.compile(r"Max skewness = ([0-9.eE+-]+),\s*(\d+) highly skew")
RE_NONORTHO = re.compile(r"non-orthogonality Max: ([0-9.eE+-]+)")
RE_NEGVOL = re.compile(r"(negative volume|Zero or negative cell volume|"
                       r"zero or negative face area)", re.I)

# Пороги, за якими вирішуємо «попередити чи зупинити».
# checkMesh лається на перекіс уже з 4 — це поріг ЯКОСТІ, а не працездатності:
# кілька граней трохи вище за нього мають майже всі реальні сітки. Небезпечно
# стає ближче до 8-10, і тоді типові налаштування розвʼязувача зриваються.
SKEW_FATAL = 8.0
NONORTHO_FATAL = 75.0
RE_FAILED = re.compile(r"FOAM FATAL (ERROR|IO ERROR)")
RE_LAYERS = re.compile(r"Added (\d+) out of (\d+) cells")
# "Extruding 167600 out of 244274 faces (68.6%)": the faces that got at least
# one layer. "Added N out of M cells" is NOT this number: M is faces x
# requested layers, so it is the share of the planned layer CELLS (T38:
# 32 layers, 69% of the faces extruded, but only 9.8% of the cells).
RE_EXTRUDE = re.compile(r"Extruding (\d+) out of (\d+) faces")

# Банер фатальної помилки не несе ніякої інформації: сам текст іде
# НАСТУПНИМИ рядками, а назва словника — у рядку «file: ... at line N».
RE_FATAL_END = re.compile(
    r"^\s*(From |in file |FOAM exiting|Finalising|MPI_ABORT|mpirun|"
    r"-{10,}|\*{5,})")
RE_RANK = re.compile(r"^\[\d+\]\s*")
RE_ARROW = re.compile(r"^-+>\s*")
FATAL_LINES = 6        # більше в рядок стану однаково не влазить


def _is_float(text: str) -> bool:
    try:
        float(text)
    except ValueError:
        return False
    return True


_fast_mod = None


def _fast():
    """fs_fast, завантажений при першій потребі (тягне numpy)."""
    global _fast_mod
    if _fast_mod is None:
        try:
            import fs_fast as mod
        except Exception:                  # pragma: no cover - збірка без numpy
            mod = False
        _fast_mod = mod
    return _fast_mod or None


@dataclass
class RunState:
    # Час розвʼязувача. У стаціонарі це номер ітерації (1, 2, 3…), у
    # нестаціонарі — секунди з дробом. Тому float, а не int: int(float("0.001"))
    # дорівнює нулю, і всі кроки нестаціонару злиплися б в один.
    moment: float = 0.0
    steps: int = 0                 # скільки разів бачили рядок «Time =»
    residuals: dict[str, list[tuple[float, float]]] = field(default_factory=dict)
    cells: int = 0
    converged: bool = False
    fatal: str = ""
    phase: str = ""
    mesh_ok: bool | None = None
    mesh_failed_checks: int = 0
    max_skewness: float = 0.0
    n_skew_faces: int = 0
    max_nonortho: float = 0.0
    negative_volumes: bool = False
    layer_coverage: float = -1.0
    # Share of the wall faces with at least one layer (the last
    # "Extruding N out of M faces" line), -1 when not seen. Not in _PARSED:
    # foamrs does not read it, feed_text reads it with one regex search.
    layer_faces: float = -1.0
    # Збирання тексту фатальної помилки, див. _feed_fatal.
    _fatal_open: bool = False
    _fatal_parts: list[str] = field(default_factory=list)

    def mesh_severity(self) -> tuple[str, str]:
        """(рівень, пояснення); рівень — ok / warn / fatal.

        Сам факт «Failed N mesh checks» нічого не вирішує: одна грань із
        перекосом 4.4 при порозі 4.0 — це шум, а шістнадцять із 6.4 валять
        розвʼязувач на 39-й ітерації. Тому дивимося на величини.
        """
        if self.negative_volumes:
            return ("fatal", tr("the mesh has cells with zero or negative volume"))
        if self.max_skewness >= SKEW_FATAL:
            return ("fatal", (
                tr("skewness {max_skewness:.2f} on {n_skew_faces} faces "
                   "(danger threshold {SKEW_FATAL:.0f})", max_skewness=self.max_skewness, n_skew_faces=self.n_skew_faces, SKEW_FATAL=SKEW_FATAL)))
        if self.max_nonortho >= NONORTHO_FATAL:
            return ("fatal", tr("non-orthogonality {max_nonortho:.1f}", max_nonortho=self.max_nonortho))
        if self.mesh_ok is False:
            parts = []
            if self.max_skewness:
                parts.append(tr("skewness {max_skewness:.2f} on "
                                "{n_skew_faces} faces", max_skewness=self.max_skewness, n_skew_faces=self.n_skew_faces))
            if self.max_nonortho:
                parts.append(tr("non-orthogonality {max_nonortho:.1f}", max_nonortho=self.max_nonortho))
            # fs_i18n.join keeps the keys: the status line follows a live
            # change of the language (T38).
            from fs_i18n import join
            return ("warn", join(", ", parts) if parts else tr("has remarks"))
        return ("ok", "")

    @property
    def iteration(self) -> int:
        """Номер ітерації — лише для стаціонару, де час цілий.

        Лишено властивістю, а не полем, щоб решта програми, яка знає тільки
        про ітерації, працювала як раніше.
        """
        return int(self.moment)

    def reset_residuals(self) -> None:
        """Перед новим розвʼязувачем: potentialFoam теж рахує з Time = 1,
        і без скидання його ітерації наклалися б на simpleFoam."""
        self.residuals.clear()
        self.moment = 0.0
        self.steps = 0
        self.converged = False

    def reset_step(self) -> None:
        """Перед кожним кроком.

        Фатальна помилка належить кроку, а не всьому прогону: без скидання
        помилка з необовʼязкового кроку (той самий checkMesh) лишалася в
        стані, і кожен наступний крок «завершувався» нею, хоч насправді
        проходив.
        """
        self.fatal = ""
        self._fatal_open = False
        self._fatal_parts = []

    def _feed_fatal(self, line: str) -> bool:
        """Збирає справжній текст фатальної помилки OpenFOAM.

        Формат такий:

            --> FOAM FATAL IO ERROR: (openfoam-2512)
            Cannot find patchField entry for body

            file: /шлях/0/U.boundaryField at line 30 to 46.

                From ... in file ... at line 200.
            FOAM exiting

        У рядок стану раніше потрапляв лише перший рядок — «FOAM FATAL IO
        ERROR: (openfoam-2512)», з якого не видно ні що сталося, ні в якому
        файлі. Тому беремо повідомлення й рядок «file:», а провенанс у
        сирцях C++ відкидаємо: він людині ні про що не говорить.
        """
        text = RE_ARROW.sub("", RE_RANK.sub("", line)).strip()
        if not text:
            # Порожні рядки НЕ закривають блок: у стандартному форматі рядок
            # «file: ... at line N» — а це найкорисніше, що там є — стоїть
            # саме після порожнього. Блок закриває або провенанс у сирцях,
            # або шум mpirun, або ліміт рядків.
            return False
        if RE_FATAL_END.match(text):
            self._fatal_open = False
            return False
        if text not in self._fatal_parts:
            # У паралельному прогоні ту саму помилку друкує кожен ранг.
            self._fatal_parts.append(text.rstrip("."))
        if len(self._fatal_parts) >= FATAL_LINES:
            self._fatal_open = False
        self.fatal = ". ".join(self._fatal_parts)
        return True

    def feed(self, line: str) -> bool:
        """Повертає True, якщо стан змінився настільки, що варто оновити графіки."""
        changed = False
        if self._fatal_open:
            return self._feed_fatal(line)
        if RE_FAILED.search(line):
            # Банер лишаємо як запасний варіант, якщо текст не прийде.
            self.fatal = RE_ARROW.sub("", RE_RANK.sub("", line)).strip()
            self._fatal_open = True
            self._fatal_parts = []
            return True

        m = RE_TIME.match(line)
        if m:
            try:
                self.moment = float(m.group(1))
                self.steps += 1
            except ValueError:
                pass
            return True

        m = RE_RESID.search(line)
        if m:
            fld = m.group(1)
            # [0-9.eE+-]+ пропускає й «1.2.3»: такий рядок просто
            # пропускаємо, а не валимо читання журналу (так само й foamrs).
            try:
                init = float(m.group(2))
            except ValueError:
                return False
            # Ux/Uy/Uz зводимо в один ряд U, беручи найгірший
            key = "U" if fld in ("Ux", "Uy", "Uz") else fld
            series = self.residuals.setdefault(key, [])
            if series and series[-1][0] == self.moment:
                series[-1] = (self.moment, max(series[-1][1], init))
            else:
                series.append((self.moment, init))
            return True

        m = RE_CELLS.match(line)
        if m:
            self.cells = int(m.group(1))
            changed = True

        m = RE_SNAPPY_PHASE.match(line)
        if m:
            self.phase = m.group(1)
            changed = True

        m = RE_MESH_FAILED.search(line)
        if m:
            self.mesh_failed_checks = int(m.group(1))
            self.mesh_ok = False
            changed = True
        elif RE_MESH_OK.match(line):
            self.mesh_ok = True
            changed = True

        m = RE_SKEW.search(line)
        if m and _is_float(m.group(1)):
            self.max_skewness = float(m.group(1))
            self.n_skew_faces = int(m.group(2))
            changed = True

        m = RE_NONORTHO.search(line)
        if m and _is_float(m.group(1)):
            self.max_nonortho = max(self.max_nonortho, float(m.group(1)))
            changed = True

        if RE_NEGVOL.search(line) and "***" in line:
            self.negative_volumes = True
            changed = True

        m = RE_LAYERS.search(line)
        if m:
            added, total = int(m.group(1)), int(m.group(2))
            # Частка граней, що реально отримали шари. Нижче ~0.7 пограншар
            # розвʼязано погано, і Cd буде занижений.
            self.layer_coverage = added / total if total else 0.0
            changed = True

        if self._feed_extrude(line):
            changed = True

        if "SIMPLE solution converged" in line or "solution converged in" in line:
            self.converged = True
            changed = True

        return changed

    # Поля, які розбирач змінює. Їх же бере й віддає foamrs.parse_log.
    _PARSED = ("moment", "steps", "cells", "converged", "fatal", "phase",
               "mesh_ok", "mesh_failed_checks", "max_skewness",
               "n_skew_faces", "max_nonortho", "negative_volumes",
               "layer_coverage")

    def feed_text(self, text: str) -> bool:
        """Те саме, що `feed` для кожного рядка `text.split("\\n")`.

        Навіщо. Підключення до прогону, що йде вже добу, перечитує весь
        журнал: пів мільйона рядків. `feed` по рядку — це ≈ 1 с самого лише
        розбору, foamrs (Rust) робить те саме за соті частки секунди. Без
        foamrs, або коли блок має щось, що точно розбирає лише Python
        (юнікодні цифри тощо), працює цикл по `feed`. Результат однаковий.
        """
        fast = _fast()
        if fast is not None:
            out = fast.parse_log(text, self._parse_state())
            if out is not None:
                self._take_parse(out)
                faces = self._feed_extrude(text)
                return bool(out["changed"]) or faces
        changed = False
        for line in text.split("\n"):
            if self.feed(line):
                changed = True
        return changed

    def _feed_extrude(self, text: str) -> bool:
        """The last "Extruding N out of M faces" in `text` -> layer_faces."""
        if "Extruding " not in text:
            return False
        last = None
        for last in RE_EXTRUDE.finditer(text):
            pass
        if last is None:
            return False
        done, total = int(last.group(1)), int(last.group(2))
        self.layer_faces = done / total if total else 0.0
        return True

    def _parse_state(self) -> dict:
        st = {k: getattr(self, k) for k in self._PARSED}
        st["fatal_open"] = self._fatal_open
        st["fatal_parts"] = list(self._fatal_parts)
        st["last"] = [(k, s[-1]) for k, s in self.residuals.items() if s]
        return st

    def _take_parse(self, out: dict) -> None:
        for k in self._PARSED:
            setattr(self, k, out[k])
        self._fatal_open = out["fatal_open"]
        self._fatal_parts = list(out["fatal_parts"])
        for key, pts in out["series"]:
            series = self.residuals.get(key)
            if series:
                # Перша точка foamrs — це оновлена остання точка ряду.
                series[-1:] = pts
            else:
                self.residuals[key] = pts


def body_coeff_names(case_dir: str) -> list[str]:
    """Імена частин, для яких є окремі сили.

    Беремо з диска, а не з конфігу: кейс могли зробити іншою версією
    програми або поправити руками, і показувати треба те, що справді
    порахувалося.
    """
    root = Path(case_dir) / "postProcessing"
    if not root.is_dir():
        return []
    return sorted(d.name[len("forceCoeffs_"):] for d in root.iterdir()
                  if d.is_dir() and d.name.startswith("forceCoeffs_"))


def read_body_coeffs(case_dir: str) -> dict[str, dict[str, list]]:
    """{частина: {Cd/Cl/...: ряд}} для всіх частин, які є на диску."""
    return {n: read_force_coeffs(case_dir, f"forceCoeffs_{n}")
            for n in body_coeff_names(case_dir)}


def read_force_coeffs(case_dir: str,
                      name: str = "forceCoeffs") -> dict[str, list[tuple[float, float]]]:
    """Читає postProcessing/<name>/*/ і повертає ряди Cd, Cl, CmPitch.

    Імена файлів різняться між версіями (coefficient.dat / forceCoeffs.dat),
    тому беремо будь-який .dat і орієнтуємось на заголовок з іменами колонок.

    `name` — імʼя функції з controlDict. Типове читає СУМАРНІ сили; з
    роздільними патчами поряд лежать `forceCoeffs_<патч>` на кожну частину.
    """
    # One reader for the program: fs_coeffs (T51; fs_polars uses it too).
    return coeffs.read_series(case_dir, name)


# T45 rule, now in fs_coeffs (fs_polars and the tests take it from here).
warm_start_files = coeffs.warm_start_files


def read_series(case_dir: str, name: str,
                pattern: str = "*.dat") -> list[tuple[float, tuple]]:
    """Ряд однієї функції postProcessing: [(час, (числа…)), …].

    Для surfaceFieldValue: скаляр дає одне число, вектор — три. OpenFOAM
    пише вектор у дужках, `(1 2 3)`; дужки тут просто прибираються.
    Повтори того самого часу (рестарт) — лишається найсвіжіший, як і в
    `read_force_coeffs`.
    """
    root = Path(case_dir) / "postProcessing" / name
    if not root.is_dir():
        return []
    files = sorted(root.glob(f"*/{pattern}"), key=lambda p: p.stat().st_mtime)
    by_time: dict[float, tuple] = {}
    for f in files:
        try:
            text = f.read_text(errors="replace")
        except OSError:
            continue
        for line in text.splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            parts = line.replace("(", " ").replace(")", " ").split()
            try:
                vals = [float(x) for x in parts]
            except ValueError:
                continue
            if len(vals) < 2:
                continue
            by_time[vals[0]] = tuple(vals[1:])
    return sorted(by_time.items())


def read_port_probes(case_dir: str, patches) -> dict[str, dict[str, list]]:
    """{кришка: {mdot/momentum/pressure: ряд}} — з того, що є на диску."""
    import fs_case as fc
    out: dict[str, dict[str, list]] = {}
    for patch in patches:
        got = {}
        for probe in [p for p, *_ in fc.ENGINE_PROBES] + [fc.PORTAL_PROBE]:
            # `forces` пише поруч force.dat і moment.dat з тими самими
            # часами; змішати їх означало б підмінити силу моментом.
            pattern = "force.dat" if probe == "pressure" else "*.dat"
            s = read_series(case_dir, fc.engine_probe_name(patch, probe),
                            pattern)
            if s:
                got[probe] = s
        if got:
            out[patch] = got
    return out


def center_of_pressure(stats: dict[str, tuple[float, float]],
                        l_ref: float, aoa_deg: float,
                        cofr: tuple[float, float, float],
                        ) -> tuple[float, float, float] | None:
    """Точка на хорді, де сумарна сила дає той самий момент, що й CmPitch.

    Рахується з тих самих Cd/Cl/CmPitch, які forceCoeffs уже видає навколо
    CofR (pitchAxis у нас завжди Y — момент саме поздовжній). По розмаху й
    висоті forceCoeffs нічого не каже, тому там просто беремо CofR.

    Знак звірено на живому прогоні (2026-10-01): кейс gui_wing, NACA 0012, 5°,
    хорда 0.2 м, передня кромка в x = 0 — точка лягає на 28.7% хорди від
    передньої кромки (для тонкого профілю очікується близько 25%, для
    скінченного крила трохи далі). Зворотний знак дав би −28.7%, тобто
    точку перед крилом.

    ЦЕ НЕ ОПОРА ДЛЯ ЦЕНТРУВАННЯ. Точка живе на одному куті й іде в
    нескінченність, коли нормальна сила прямує до нуля: на тілі без несучої
    поверхні (filled_holes1, 5°) вона лежить за 0.35 м ПЕРЕД носом, а той самий
    літак на стаціонарному розвʼязувачі дає зовсім інше місце (−0.31 проти
    −0.61 м). Про стійкість по тангажу каже не вона, а нейтральна точка —
    див. `neutral_point`, вона потребує кількох кутів атаки.
    """
    if "Cl" not in stats or "CmPitch" not in stats:
        return None
    cl = stats["Cl"][0]
    cd = stats.get("Cd", (0.0, 0.0))[0]
    cm = stats["CmPitch"][0]
    a = math.radians(aoa_deg)
    c_n = cl * math.cos(a) + cd * math.sin(a)
    if abs(c_n) < 1e-6:
        return None
    x = cofr[0] - cm * l_ref / c_n
    return (x, cofr[1], cofr[2])


@dataclass
class NeutralPoint:
    """Нейтральна точка по тангажу, знайдена за кількома кутами атаки."""
    x: float                        # м, у системі кейсу (потік іде вздовж +X)
    cn_alpha: float                 # dCn/dα, на градус
    cm_alpha: float                 # dCm/dα відносно центру моментів, на градус
    angles: tuple                   # кути, за якими пораховано, градуси
    pairs: tuple                    # нейтральна точка за кожною парою сусідніх
                                    # кутів: за розкидом видно, чи лінійно


MIN_ANGLE_SPAN = 1.0                # менший розмах кутів — похідна тоне в шумі
MIN_CN_ALPHA = 1e-4                 # на градус; менше — точка тікає в нескінченність


def normal_coeff(cd: float, cl: float, aoa_deg: float) -> float:
    """Коефіцієнт нормальної сили в осях тіла: Cn = Cl·cosα + Cd·sinα.

    У forceCoeffs підйомна сила й опір рахуються відносно напрямку потоку,
    а плече моменту по X множиться на силу, нормальну до вісі тіла. Тому
    для нейтральної точки потрібен саме Cn, а не Cl.
    """
    a = math.radians(aoa_deg)
    return cl * math.cos(a) + cd * math.sin(a)


def _slope(xs: list[float], ys: list[float]) -> float:
    """Нахил прямої найменших квадратів."""
    n = len(xs)
    mx, my = sum(xs) / n, sum(ys) / n
    den = sum((x - mx) ** 2 for x in xs)
    return sum((x - mx) * (y - my) for x, y in zip(xs, ys)) / den


def neutral_point(points: list[tuple[float, float, float, float]],
                  l_ref: float, x_cofr: float) -> NeutralPoint | None:
    """Нейтральна точка: де момент по тангажу не залежить від кута атаки.

    `points` — [(кут, Cd, Cl, CmPitch)], по одному на прогін, усі з ОДНАКОВИМИ
    Lref, Aref і центром моментів CofR (інакше коефіцієнти нормовані по-
    різному й порівнювати їх не можна). Переніс моменту в іншу точку x:

        Cm(x) = Cm(CofR) + (x − CofR)·Cn / Lref

    Нейтральна — така x, де dCm/dα = 0:

        x_np = CofR − (dCm/dα) / (dCn/dα) · Lref

    Центр ваги ставлять ПЕРЕД нею (менший x, бо потік іде вздовж +X) на 5–15%
    хорди: різниця (x_np − x_цв)/Lref — це запас стійкості. З одного кута вона
    не рахується; центр тиску (див. `center_of_pressure`) збігається з нею
    лише в симетричної конфігурації, де при α = 0 нема ні сили, ні моменту.

    None — якщо кутів замало (розмах менший за MIN_ANGLE_SPAN) або нормальна
    сила не росте з кутом.
    """
    pts = sorted((float(a), float(cd), float(cl), float(cm))
                 for a, cd, cl, cm in points)
    if len(pts) < 2 or pts[-1][0] - pts[0][0] < MIN_ANGLE_SPAN or l_ref <= 0:
        return None
    angles = [p[0] for p in pts]
    cn = [normal_coeff(p[1], p[2], p[0]) for p in pts]
    cm = [p[3] for p in pts]
    cn_a, cm_a = _slope(angles, cn), _slope(angles, cm)
    if cn_a < MIN_CN_ALPHA:
        return None
    pairs = []
    for i in range(len(pts) - 1):
        da = angles[i + 1] - angles[i]
        if da < 1e-9:
            continue
        dn = (cn[i + 1] - cn[i]) / da
        dm = (cm[i + 1] - cm[i]) / da
        if dn >= MIN_CN_ALPHA:
            pairs.append(x_cofr - dm / dn * l_ref)
    return NeutralPoint(x=x_cofr - cm_a / cn_a * l_ref, cn_alpha=cn_a,
                        cm_alpha=cm_a, angles=tuple(angles),
                        pairs=tuple(pairs))


# --------------------------------------------------------------------------
# Сесія на сервері: прогін, що живе окремо від вікна
# --------------------------------------------------------------------------
#
# ЧОМУ ЦЕ ВЗАГАЛІ ПОТРІБНО. Доти кожен крок пускався як `ssh host "команда"`,
# а вікно тримало трубу й читало з неї рядки. Поки вікно живе — все гаразд.
# Але щойно програма закривається (чи падає, чи ноут іде спати), ssh рветься,
# віддалені процеси отримують SIGHUP і гинуть. Годинний прогін на сервері
# вмирав від того, що на ноуті закрили вікно.
#
# ДЕ ТРИМАТИ СТАН. Спокуса — запамʼятати прогін у себе, в теці налаштувань.
# Так простіше, і так неправильно: тоді прогін «існує» лише для тієї
# програми, що його пустила. Переставив систему, сів за інший ноут, почистив
# налаштування — і прогін іде, а знайти його нічим.
#
# Тому стан живе НА СЕРВЕРІ, поруч із кейсом, у теці `studio/`:
#
#     studio/run.sh    сам сценарій, яким прогін пущено
#     studio/run.log   увесь журнал одним файлом — його й читає вікно
#     studio/status    running | ok | fail<TAB>крок<TAB>що сталося
#     studio/step      назва кроку, який іде просто зараз
#     studio/pid       номер процесу-голови
#     studio/meta.json звідки прогін пущено: імʼя кейсу, тека на ноуті, час
#
# Через це «підключитися» не потребує нічого, крім адреси сервера: програма
# просто дивиться, що там лежить.

# Рядок-заголовок кроку в журналі. Той самий візерунок пише і локальний
# виконавець, і сценарій на сервері — саме по ньому журнал сесії
# розкладається назад по кроках.
_STEP_LINE = re.compile(r"^=====\s+(.+?)\s+=====$")


_HEAD_STEP = f"{MARK} step="
_HEAD_DONE = f"{MARK} {STEP_DONE}"


def _head_title(line: str) -> "str | None":
    """The step id of a header line, or None. Both forms: the marker
    "@@FS step=<id>" / "@@FS done", and the older «===== <name> =====»."""
    if line.startswith(MARK):
        if line.startswith(_HEAD_STEP):
            name = line[len(_HEAD_STEP):].strip()
            return name or None
        if line.rstrip() == _HEAD_DONE:
            return STEP_DONE
        return None
    head = _STEP_LINE.match(line)
    return step_id(head.group(1)) if head else None


def _heads_of(block: str, lead: str, heads: list) -> None:
    if block.startswith(lead):
        at = 0
    else:
        at = block.find("\n" + lead) + 1          # 0: не знайдено
        if at == 0:
            return
    while True:
        end = block.find("\n", at)
        if end < 0:
            end = len(block)
        line = block[at:end]
        title = _head_title(line)
        if title is not None:
            heads.append((at, end, line, title))
        at = block.find("\n" + lead, end) + 1
        if at == 0:
            return


def step_heads(block: str) -> list:
    """Заголовки кроків у блоці рядків: [(початок, кінець, рядок, id)].

    `id` — step_id (для старого журналу — уже переведений зі старої
    української назви). Кандидатів шукає str.find (рядок, що починається з
    «@@FS» або «=====»): регулярний вираз з re.M на 40 МБ журналу йде майже
    секунду.
    """
    heads: list = []
    _heads_of(block, MARK, heads)
    _heads_of(block, "=====", heads)
    heads.sort(key=lambda h: h[0])
    return heads


def read_blocks(stream, chunk: int = 1 << 20):
    """Текст з труби блоками цілих рядків (через "\n", без останнього "\n").

    Рядки ті самі, що дав би `readline` у текстовому режимі (utf-8 із
    заміною, "\r\n" і "\r" стають "\n"), але без виклику на кожен рядок.
    Окремий потік читає трубу, а тут береться ВСЕ, що вже прийшло: поки
    сервер віддає старий журнал, блоки великі; потім — по кілька рядків,
    без затримки.
    """
    q: "queue.Queue[bytes | None]" = queue.Queue()

    def pump():
        try:
            while True:
                data = stream.read(chunk)
                if not data:
                    break
                q.put(data)
        except (OSError, ValueError):
            pass
        finally:
            q.put(None)

    threading.Thread(target=pump, daemon=True, name="log-pump").start()
    # Не io.IncrementalNewlineDecoder: він у 4-5 разів повільніший за сам
    # utf-8, хоча в журналі з сервера "\r" майже не буває.
    dec = codecs.getincrementaldecoder("utf-8")(errors="replace")
    rest = ""
    cr = False             # блок скінчився на "\r": може бути пів "\r\n"
    while True:
        parts = [q.get()]
        while True:
            try:
                parts.append(q.get_nowait())
            except queue.Empty:
                break
        done = parts[-1] is None
        if done:
            parts.pop()
        text = dec.decode(b"".join(parts), final=done)
        if cr:
            text = "\r" + text
        cr = not done and text.endswith("\r")
        if cr:
            text = text[:-1]
        if "\r" in text:
            text = text.replace("\r\n", "\n").replace("\r", "\n")
        text = rest + text
        if done:
            if text:
                yield text[:-1] if text.endswith("\n") else text
            return
        cut = text.rfind("\n")
        if cut < 0:
            rest = text
            continue
        rest = text[cut + 1:]
        yield text[:cut]

STUDIO = "studio"
SESSION_ROOT = fs_paths.REMOTE_ROOT
# Скільки секунд чекати між опитуваннями стану, поки читаємо журнал.
ATTACH_POLL = 2


@dataclass
class RemoteSession:
    """Один прогін на сервері — такий, яким його видно ззовні."""
    name: str
    remote_dir: str
    status: str = "running"      # running | ok | fail | unknown
    step: str = ""
    message: str = ""
    started: str = ""
    case_dir: str = ""           # тека на ноуті, звідки прогін пустили
    host: str = ""
    pid: int = 0
    alive: bool = False          # процес-голова справді живий
    log_lines: int = 0
    # Скільки секунд минуло, відколи стан востаннє змінювався. Для того,
    # що скінчилося, це і є «як давно». -1 — невідомо.
    age: int = -1
    # "ok" after a soft stop: the slice that was written, for the person.
    # "" for any other end.
    soft: str = ""
    # T44: the results of this session are on the laptop (studio/fetched,
    # T43). False also for a session from an older probe.
    fetched: bool = False

    @property
    def step_text(self) -> str:
        """The current step for the person (translated)."""
        return step_title(self.step) if self.step else ""

    @property
    def running(self) -> bool:
        return self.status == "running"

    @property
    def finished(self) -> bool:
        return self.status in ("ok", "fail", "stop")

    def worth_greeting(self, window_s: int = 12 * 3600) -> bool:
        """Чи варто сказати про цю сесію одразу при запуску програми.

        Дві підстави, і обидві про людину, яка щойно відкрила вікно:
        прогін ІДЕ зараз — про це треба знати негайно; або прогін
        СКІНЧИВСЯ, поки її не було, і вона по це й прийшла.

        Давнє не згадуємо: сесії на сервері лежать доти, доки їх не
        затруть, і за місяць їх набереться стільки, що вітання
        перетвориться на шум, у якому загубиться потрібне.
        """
        if self.running and self.alive:
            return True
        if self.stale:
            return 0 <= self.age <= window_s
        return self.finished and 0 <= self.age <= window_s

    @property
    def stale(self) -> bool:
        """Написано «іде», а процесу немає.

        Так виглядає прогін, убитий перезавантаженням сервера чи OOM-killer:
        файл стану лишився з останнім словом «running», і повірити йому
        означало б чекати на те, чого вже немає.
        """
        return self.status == "running" and not self.alive

    def human(self) -> str:
        if self.stale:
            return tr("aborted")
        return tr({"running": "running", "ok": "done", "fail": "failed",
                   "stop": "stopped"}.get(self.status, "unknown"))


def _sh_name(name: str) -> str:
    """Назва кроку, придатна для імені файлу."""
    return re.sub(r"[^\w.-]+", "_", name, flags=re.UNICODE).strip("_") or "step"


# meta["started"] of a script that the queue starts later (T46).
START_LATER = "@@start@@"


def detached_script(steps: "list[Step]", remote_dir: str, bashrc: str,
                    meta: dict, stop_on_bad_mesh: bool = True,
                    engine: str = "", image: str = "") -> str:
    """bash, який проганяє всі кроки САМ, без жодного звʼязку з вікном.

    Журнал пишеться рівно тим самим візерунком, що й локальний виконавець
    (порожній рядок, «===== крок =====», вивід), бо його читає той самий
    розбирач станів. Розійдися вони — і графіки при підключенні були б
    порожні, а причину шукали б у розвʼязувачі.

    Перевірка сітки теж тут, а не у вікні. Вікна може не бути: сенс усієї
    цієї теки в тому, що прогін живе без нього. Лишити перевірку на боці
    вікна означало б, що відʼєднаний прогін спокійно рахує добу на сітці,
    яку відʼєднаним його ж і не пустили б.
    """
    s = f'"{remote_dir}/{STUDIO}"'
    lines = [
        "#!/bin/bash",
        "# Складено OpenFOAM Studio. Правити не треба: його перезаписує",
        "# кожен новий запуск цього кейсу.",
        *([f'source "{bashrc}"'] if not engine else [
            # OpenFOAM in a container (container_run): each step command
            # runs in the image; the tests and the log stay on the server.
            "ofs_in() { " + container_run(engine, image, 'eval "$OFS_CMD"')
            .replace('-e HOME="$HOME"', '-e HOME="$HOME" -e OFS_CMD')
            + "; }"]),
        f'D="{remote_dir}"',
        f'S="$D/{STUDIO}"',
        'mkdir -p "$S" "$D/logs"',
        # meta пишеться ПІСЛЯ mkdir, а не десь у голові сценарію: тека
        # сесії до того ще може не існувати, і запис мовчки пропадав би.
        f"cat > \"$S/meta.json\" <<'OFSMETA'\n"
        f"{json.dumps(meta, ensure_ascii=False)}\nOFSMETA",
        # A queued script (T46) starts later than it was made: the real
        # start time goes in place of START_LATER.
        *([f'sed -i "s/{START_LATER}/$(date "+%Y-%m-%d %H:%M:%S")/" '
           '"$S/meta.json"'] if meta.get("started") == START_LATER else []),
        'echo $$ > "$S/pid"',
        ': > "$S/run.log"',
        'echo running > "$S/status"',
        'say() { printf "%s\\n" "$*" >> "$S/run.log"; }',
        # Причину пишемо В ОДИН рядок із табуляціями: так її читає
        # `read_session`, і так вона не розсиплеться на багаторядковому
        # тексті помилки OpenFOAM.
        'die() { printf "fail\\t%s\\t%s\\n" "$1" "$2" > "$S/status"; exit 1; }',
        "",
        "run_step() {",
        '  local title="$1" file="$2" cmd="$3" how="${4:-eval}"',
        '  printf "%s\\n" "$title" > "$S/step"',
        '  say ""',
        f'  say "{MARK} step=$title"',
        # tee: той самий текст іде і в спільний журнал, і в окремий файл
        # кроку — саме його показує вікно в повідомленні «дивіться
        # logs/checkMesh.log». PIPESTATUS, бо код повернення потрібен від
        # команди, а не від tee.
        # how=in: the command runs in the container (ofs_in reads OFS_CMD).
        '  if [ "$how" = in ]; then ( cd "$D" && OFS_CMD="$cmd" ofs_in ) '
        '2>&1 | tee -a "$S/run.log" > "$D/logs/$file.log"',
        '  else ( cd "$D" && eval "$cmd" ) 2>&1 | tee -a "$S/run.log" '
        '> "$D/logs/$file.log"; fi',
        '  return ${PIPESTATUS[0]}',
        "}",
        "",
    ]
    # Позначка мʼякої зупинки від МИНУЛОГО прогону не має спинити цей.
    lines.append(f'rm -f "$S/{SOFT_FLAG}"; SOFT=""')
    for step in steps:
        # The name is an ASCII id (step_id); the old Ukrainian names are
        # only read, never written.
        title = step_id(step.name).replace('"', "'")
        file = _sh_name(title)
        # Мʼяку зупинку перевіряємо ПЕРЕД кожним кроком, а не лише після
        # розвʼязувача: перший етап міг скінчитися сам саме тоді, коли
        # людина натиснула кнопку, і без цього другий етап узяв би свій
        # controlDict.stage2 і рахував би далі.
        lines.append(f'[ -f "$S/{SOFT_FLAG}" ] && SOFT=1')
        wrap = not step.keep_on_soft
        if wrap:
            lines.append('if [ -z "$SOFT" ]; then')
        if step.cond:
            lines.append(f'if ( cd "$D" && {cond_test(step.cond)} ); then')
        # Команда йде в ОДИНАРНИХ лапках. У подвійних `$` розгортався б
        # двічі: спершу при складанні аргументу для run_step, потім у
        # `eval`. На `echo "крок $i"` це давало порожнечу замість номера, а
        # на справжніх командах OpenFOAM — тихо не те, бо змінні там є
        # ($FOAM_*, $(nproc)) і потрібні вони саме всередині eval.
        cmd = step.command.replace("'", "'\\''")
        # The distributed run (foamdist) starts its own containers: it
        # runs on the server, not in the image.
        how = " in" if engine and FOAMDIST_REMOTE not in step.command else ""
        lines.append(f"run_step \"{title}\" \"{file}\" '{cmd}'{how}")
        if step.optional:
            lines.append('rc=$?; [ $rc -eq 0 ] || say '
                         f'"{MARK} optional step={title} rc=$rc"')
        else:
            lines.append(f'rc=$?; [ $rc -eq 0 ] || die "{title}" '
                         f'"{MARK} fail=rc rc=$rc"')
        if step.name == "checkMesh" and stop_on_bad_mesh:
            lines.extend(_mesh_gate_lines(file))
        if step.cond:
            lines.append("fi")
        if wrap:
            lines.append("fi")
    lines.append(f'[ -f "$S/{SOFT_FLAG}" ] && SOFT=1')
    # Той самий спосіб лапок, що й для команд кроків вище.
    check = soft_check_command().replace("'", "'\\''")
    restore = soft_restore_command().replace("'", "'\\''")
    lines.extend([
        # Мʼяка зупинка: повернути stopAt і ПЕРЕВІРИТИ, що зріз справді
        # лежить у корені, — інакше «готово» було б неправдою.
        'if [ -n "$SOFT" ]; then',
        f"  ( cd \"$D\" && eval '{restore}' )",
        f"  run_step \"{STEP_SOFT_CHECK}\" \"{STEP_SOFT_CHECK}\" '{check}'",
        # The last line of the check is a marker "@@FS snap=…": the reason
        # of "fail" (reason_text) or the slice of "ok" (soft_done_text).
        '  rc=$?',
        f'  M=$(tail -1 "$D/logs/{STEP_SOFT_CHECK}.log")',
        f'  [ $rc -eq 0 ] || die "{STEP_SOFT_CHECK}" "$M"',
        f'  rm -f "$S/{SOFT_FLAG}"',
        '  printf "ok\\t%s\\n" "$M" > "$S/status"',
        "else",
    ])
    lines.append('echo ok > "$S/status"')
    lines.append("fi")
    lines.append('say ""')
    lines.append(f'say "{MARK} {STEP_DONE}"')
    return "\n".join(lines) + "\n"


def _mesh_gate_lines(file: str) -> list[str]:
    """Заборона рахувати на непридатній сітці — тими самими порогами.

    checkMesh завершується з нулем навіть тоді, коли перевірки провалено,
    тож дивитися треба на ВЕЛИЧИНИ. Пороги тут ті самі, що й у
    `RunState.mesh_severity`, і це не збіг: числа беруться з тих самих
    сталих при складанні сценарію, щоб два місця не розійшлися мовчки.
    """
    log = f'"$D/logs/{file}.log"'
    # ЧИСЛО ВИРІЗАЄТЬСЯ ПІСЛЯ РОЗДІЛЬНИКА, а не фільтром за символами.
    #
    # Перша версія брала `tr -dc "0-9.eE+-"` — і вигрібала літери `e` зі
    # слова «skewness». Виходило «ee0.634». Далі awk порівнював це з
    # порогом, а оскільки такий рядок числом не виглядає, порівняння йшло
    # ЯК З РЯДКАМИ: "ee0.634" > "8" — правда, бо 'e' більше за '8'. Добра
    # сітка з перекосом 0.63 бракувалася як непридатна, і прогін гинув із
    # поясненням «перекіс ee0.63436078».
    #
    # Тому: число беремо через sed після «=» чи «:», а в awk примусово
    # переводимо в число через `v+0`. Тоді сміття дає нуль, тобто мовчання,
    # а не хибну тривогу — у цей бік помилятися дешевше.
    return [
        f'SKEW=$(grep -ao "Max skewness = [0-9.eE+-]*" {log} '
        '| tail -1 | sed "s/.*=[[:space:]]*//")',
        f'NORT=$(grep -ao "non-orthogonality Max: [0-9.eE+-]*" {log} '
        '| tail -1 | sed "s/.*:[[:space:]]*//")',
        f'NEG=$(grep -aciE "negative volume|zero or negative cell volume|'
        f'zero or negative face area" {log} || true)',
        '[ "${NEG:-0}" -gt 0 ] && die "checkMesh" '
        f'"{MARK} fail=negvol"',
        f"awk -v v=\"${{SKEW:-0}}\" 'BEGIN{{exit !(v+0>={SKEW_FATAL})}}' "
        f'&& die "checkMesh" "{MARK} fail=skew value=$SKEW"',
        f"awk -v v=\"${{NORT:-0}}\" 'BEGIN{{exit !(v+0>={NONORTHO_FATAL})}}' "
        f'&& die "checkMesh" "{MARK} fail=nonortho value=$NORT"',
    ]


def launch_detached(backend: Backend, remote_dir: str, script: str) -> int:
    """Покласти сценарій на сервер і пустити його ВІДЧЕПЛЕНИМ. Повертає pid.

    `setsid` — не про акуратність, а про суть справи: він виводить процес із
    сеансу ssh, і тоді розрив звʼязку йому вже нічого не робить. Саме звідси
    й береться те, заради чого все це: прогін переживає закриття програми.
    """
    # ВИВАНТАЖЕННЯ Й ЗАПУСК — ОКРЕМИМИ ЗАХОДАМИ, і це не зайва обережність.
    #
    # Перша версія робила все одним рядком: `mkdir && cat > run.sh && ... &
    # echo $!`. У bash кінцевий `&` бере ВЕСЬ ланцюжок `&&`, а не останню
    # команду, тож у фон пішло й саме читання сценарію — ssh закрив вхід
    # раніше, ніж `cat` устиг щось прочитати. На сервері лишався run.sh
    # нульового розміру, `echo $!` слухняно віддавав номер процесу, і
    # запуск виглядав успішним. Порожня тека сесії зʼявлялася аж потім.
    put_script(backend, remote_dir, script)
    rc, out = _run(ssh_argv(backend.host, detached_launch_command(remote_dir)),
                   timeout=60)
    if rc != 0:
        raise RuntimeError(tr("cannot start the run: {strip}", strip=out.strip()[:200]))
    for token in reversed(out.split()):
        if token.isdigit():
            return int(token)
    return 0


def detached_launch_command(remote_dir: str) -> str:
    """bash: start studio/run.sh detached (setsid nohup), print its pid.

    The one start command of a server run, for «Run on Server» and for the
    queue (T46). fs_queue_runner.detached_launch is the same text (the
    runner has no other module of the program); tests/test_queue.py
    compares them.
    """
    return (f'cd "{remote_dir}" && {{ setsid nohup bash '
            f'"{remote_dir}/{STUDIO}/run.sh" '
            f'> "{remote_dir}/{STUDIO}/boot.log" 2>&1 < /dev/null & echo $!; }}')


def queue_launch_command(remote_dir: str, script: str, job: str = "") -> str:
    """bash for the queue (T46): first the T43 guard (no live run uses the
    folder), then studio/<script> becomes studio/run.sh and starts detached.
    `job` (the queue id) goes to studio/queue_job."""
    s = f"{remote_dir}/{STUDIO}"
    return guard.guarded(remote_dir, (
        f'cd "{remote_dir}" && cp -f "{s}/{script}" "{s}/run.sh" && '
        f'rm -f "{s}/status" "{s}/pid" && '
        f'printf "%s\\n" "{job}" > "{s}/queue_job" && '
        + detached_launch_command(remote_dir)), own=True)


def stop_command(target: str) -> str:
    """bash: stop the detached run in `target`: the head script with its
    process group, and the OpenFOAM processes of the folder. «Stop» and the
    queue (T46) use it."""
    return (f'p=$(cat "{target}/{STUDIO}/pid" 2>/dev/null); '
            '[ -n "$p" ] && kill -TERM -"$p" 2>/dev/null; '
            f'printf "stop\\tзупинено з програми\\n" '
            f'> "{target}/{STUDIO}/status" 2>/dev/null; '
            + signal_script(target, "TERM"))


def put_script(backend: Backend, remote_dir: str, script: str,
               name: str = "run.sh") -> None:
    """Put a run script in <remote_dir>/studio/<name> and check its size.

    «Run on Server» puts studio/run.sh and starts it at once. The queue
    (T46) puts studio/q_<token>.sh; the queue copies it to run.sh when the
    job starts.
    """
    if not re.fullmatch(r"[\w.-]+\.sh", name):
        raise ValueError(name)
    up = subprocess.run(
        ssh_argv(backend.host,
                 f'mkdir -p "{remote_dir}/{STUDIO}" "{remote_dir}/logs" && '
                 f'cat > "{remote_dir}/{STUDIO}/{name}" && '
                 f'chmod +x "{remote_dir}/{STUDIO}/{name}" && '
                 f'wc -c < "{remote_dir}/{STUDIO}/{name}"'),
        input=script.encode("utf-8"), stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT, timeout=120,
        creationflags=CREATE_NO_WINDOW)
    out = up.stdout.decode("utf-8", "replace").strip()
    if up.returncode != 0:
        raise RuntimeError(tr("cannot put the script on the server: {out}", out=out[:200]))
    # Звіряємо РОЗМІР, а не код повернення: саме мовчазний нульовий файл і
    # був тією вадою, тож перевіряти тут треба те, що вона ламала.
    got = next((int(t) for t in out.split() if t.isdigit()), 0)
    want = len(script.encode("utf-8"))
    if got != want:
        raise RuntimeError(
            tr("the script arrived incomplete: {got} bytes instead of {want}", got=got, want=want))


# Роздільник полів у `studio/status` і у відповіді `read_session`.
_FS = "\x1f"


def _session_probe(dirs: str) -> str:
    """bash, що описує сесії в заданих теках — по одному рядку на сесію.

    Одним заходом, а не запитом на поле: кожне окреме ssh коштує рукостискання,
    а полів тут шість. На списку з десяти прогонів різниця — секунди проти
    хвилини.
    """
    return (
        f'for d in {dirs}; do '
        '[ -d "$d/' + STUDIO + '" ] || continue; '
        's=$(head -c 400 "$d/' + STUDIO + '/status" 2>/dev/null); '
        'p=$(cat "$d/' + STUDIO + '/pid" 2>/dev/null); '
        'st=$(head -c 200 "$d/' + STUDIO + '/step" 2>/dev/null); '
        'm=$(tr -d "\\n" < "$d/' + STUDIO + '/meta.json" 2>/dev/null); '
        'n=$(wc -l < "$d/' + STUDIO + '/run.log" 2>/dev/null || echo 0); '
        # The head lives: the rule of fs_caseguard (kill -0 and the command
        # line studio/run.sh). Only kill -0 took a reused pid after a reboot
        # of the server for a live run (T44).
        'a=0; ' + guard.head_alive("$p") + ' && a=1; '
        # T44: the results are on the laptop (studio/fetched of T43 is not
        # older than studio/status).
        'f=0; [ -f "$d/' + STUDIO + '/fetched" ] && ! [ "$d/' + STUDIO
        + '/status" -nt "$d/' + STUDIO + '/fetched" ] && f=1; '
        # Коли стан востаннє змінювався — це і є час, коли прогін
        # скінчився. Окремої позначки про це не заводимо: mtime файлу
        # стану каже те саме й не може розійтися з ним.
        'g=$(( $(date +%s) - $(stat -c %Y "$d/' + STUDIO + '/status" '
        '2>/dev/null || date +%s) )); '
        f'printf "%s{_FS}%s{_FS}%s{_FS}%s{_FS}%s{_FS}%s{_FS}%s{_FS}%s\\n" '
        '"$d" "$(printf "%s" "$s" | tr "\\n" " ")" "$p$a" "$st" "$n" "$g" '
        '"$m" "$f"; '
        'done'
    )


def session_message(status: str, rest: "list[str]") -> str:
    """The message of studio/status (the fields after the status word) for
    the person. New scripts write markers, older ones Ukrainian texts."""
    rest = [r.strip() for r in rest]
    if status == "fail" and len(rest) >= 2:
        why = reason_text(rest[0], "\t".join(rest[1:]))
        return f"{step_title(rest[0])}: {why}" if rest[0] else why
    text = "\t".join(rest).strip()
    if status == "ok" and text:
        return soft_done_text(soft_slice_text(text))
    return reason_text("", text) if text else ""


def _parse_session(line: str, host: str) -> "RemoteSession | None":
    parts = line.split(_FS)
    if len(parts) < 6:
        return None
    # Сьоме поле — вік стану. Його могло й не бути (сесія з давнішої
    # версії), і тоді це не привід відкинути весь рядок: без віку сесія
    # просто не потрапить у вітання при запуску.
    # The eighth field (T44): "1" when the results are on the laptop.
    fetched = len(parts) >= 8 and parts[7].strip() == "1"
    if len(parts) >= 7:
        path, status_raw, pid_alive, step, nlines, age_raw, meta_raw = parts[:7]
    else:
        path, status_raw, pid_alive, step, nlines, meta_raw = parts[:6]
        age_raw = "-1"
    bits = status_raw.split("\t")
    status = (bits[0] or "unknown").strip()
    if status not in ("running", "ok", "fail", "stop"):
        status = "unknown"
    # pid і ознака життя склеєні в одне поле, щоб не плодити роздільників:
    # останній символ — 0 або 1, решта — номер процесу.
    alive = pid_alive.endswith("1")
    try:
        pid = int(pid_alive[:-1] or 0)
    except ValueError:
        pid = 0
    meta = {}
    if meta_raw.strip():
        try:
            meta = json.loads(meta_raw)
        except ValueError:
            meta = {}
    try:
        lines = int(nlines.strip() or 0)
    except ValueError:
        lines = 0
    try:
        age = int(age_raw.strip() or -1)
    except ValueError:
        age = -1
    return RemoteSession(
        name=str(meta.get("name") or Path(path).name),
        remote_dir=path.strip(), status=status,
        step=step_id(step),
        message=session_message(status, bits[1:]),
        soft=(soft_slice_text("\t".join(bits[1:]))
              if status == "ok" and "".join(bits[1:]).strip() else ""),
        started=str(meta.get("started", "")),
        case_dir=str(meta.get("case_dir", "")),
        host=host, pid=pid, alive=alive, log_lines=lines, age=age,
        fetched=fetched)


def list_sessions(backend: Backend) -> list[RemoteSession]:
    """Які прогони є на сервері. Найсвіжіші — першими.

    Порожній список означає рівно «нічого не знайдено», а не «сервер
    недоступний»: недоступність приходить винятком, і плутати їх не можна.
    """
    if backend.kind != "ssh":
        return []
    rc, out = _run(ssh_argv(backend.host,
                            _session_probe(f'"{SESSION_ROOT}"/*')), timeout=45)
    if rc != 0:
        raise RuntimeError(out.strip()[:300] or tr("the server did not reply"))
    found = [s for s in (_parse_session(ln, backend.host)
                         for ln in out.splitlines() if _FS in ln) if s]
    found.sort(key=lambda s: (s.started or "", s.name), reverse=True)
    return found


def read_session(backend: Backend, remote_dir: str,
                 strict: bool = False) -> "RemoteSession | None":
    """Стан однієї сесії просто зараз.

    None: no session in this folder, or (strict=False) no answer from the
    server. With strict=True, no answer from the server raises
    ConnectionError: the attach must not take a lost network for the end
    of the run (T44).
    """
    if backend.kind != "ssh":
        return None
    rc, out = _run(ssh_argv(backend.host, _session_probe(f'"{remote_dir}"')),
                   timeout=30)
    if rc != 0:
        if strict:
            raise ConnectionError(out.strip()[:300]
                                  or tr("the server did not reply"))
        return None
    for ln in out.splitlines():
        if _FS in ln:
            return _parse_session(ln, backend.host)
    return None


def attach_command(remote_dir: str, from_line: int = 1) -> str:
    """bash, що віддає журнал і САМ завершується, коли прогін скінчився.

    `tail -F` один нікуди не годиться: він не завершується ніколи, і вікно
    чекало б на прогін, який давно готовий. Тому поруч крутиться сторож, що
    дивиться у файл стану, а після його зміни дає хвостові ще мить дочитати
    останні рядки — інакше кінець журналу обривався б на півслові.
    """
    return (
        f'cd "{remote_dir}" 2>/dev/null || exit 3; '
        f'tail -n +{max(int(from_line), 1)} -F "{STUDIO}/run.log" '
        '2>/dev/null & TP=$!; '
        'while :; do '
        f's=$(head -c 20 "{STUDIO}/status" 2>/dev/null); '
        'case "$s" in running*) ;; *) sleep 2; break;; esac; '
        f'sleep {ATTACH_POLL}; '
        'done; '
        'kill $TP 2>/dev/null; wait $TP 2>/dev/null; exit 0'
    )


# --------------------------------------------------------------------------
# Виконавець
# --------------------------------------------------------------------------

class Runner:
    """Виконує список кроків, віддаючи рядки логу через callback.

    Не залежить від Qt — інтерфейс просто крутить його в окремому потоці.
    """

    def __init__(self, backend: Backend, case_dir: str,
                 on_line=None, on_step=None, on_done=None,
                 native_dir: str | None = None,
                 stop_on_bad_mesh: bool = True, on_block=None,
                 on_event=None):
        self.backend = backend
        self.case_dir = case_dir
        self.native_dir = native_dir
        self.on_line = on_line or (lambda s: None)
        # Блок рядків журналу одним текстом (рядки через "\n"). Без нього
        # кожен рядок іде в on_line окремо.
        self.on_block = on_block
        self.on_step = on_step or (lambda name, idx, total: None)
        self.on_done = on_done or (lambda ok, msg: None)
        # Events for the window, without a text that it must compare:
        # on_event(name, value). "detached" (value: the folder on the server).
        self.on_event = on_event or (lambda name, value: None)
        # After a soft stop with a slice: the slice for the person ("" if the
        # run did not end so). The window reads this, not the message text.
        self.soft_result = ""
        self.stop_on_bad_mesh = stop_on_bad_mesh
        self.state = RunState()
        self._proc: subprocess.Popen | None = None
        self._stop = threading.Event()
        # Ends the pause between two tries to connect again (stop, detach,
        # reconnect_now). T44.
        self._wake = threading.Event()
        # How the run ended, as an ASCII id, for the window and the
        # notifications: "ok", "fail", "stop", "aborted", "gone" (no
        # session), "" (not known or not ended).
        self.final_status = ""
        # Мʼяка зупинка: розвʼязувач пише зріз і виходить, далі — лише збирання.
        self._soft = threading.Event()
        self.current_step = ""
        self.paused = False
        # Тека сесії на сервері, коли прогін відчеплений. Порожньо
        # означає звичайний прогін, привʼязаний до цього вікна.
        self.remote_dir = ""
        # True after detach(): the window let the run go (for example at
        # close). The run continues on the server, so nothing must be
        # copied back or deleted on this side.
        self.left = False
        self.log_dir = Path(case_dir) / "logs"

    def pause(self, resume: bool = False) -> tuple[bool, str]:
        """Завмирає прогін. Повертає (вдалося, що сказати людині)."""
        argv = self.backend.pause_argv(self.case_dir, self.native_dir,
                                       resume=resume)
        if argv is None:
            return (False, tr("this backend cannot pause a run"))
        rc, out = _run(argv, timeout=25)
        if rc != 0:
            return (False, out.strip()[:200] or tr("failed", ctx="action"))
        self.paused = not resume
        if self.backend.kind == "docker":
            return (True, tr("container resumed") if resume
                    else tr("container paused"))
        # Скрипт друкує, СКІЛЬКИ процесів зачепив. Нуль означає, що рахувати
        # вже нема чого, і мовчазне «готово» тут було б брехнею.
        n = 0
        for line in reversed(out.strip().splitlines()):
            if line.strip().isdigit():
                n = int(line.strip())
                break
        if n == 0:
            self.paused = False
            return (False, tr("no process of this case found — maybe the "
                           "run has already ended"))
        return (True, tr("{v} processes: {n}", v='resumed' if resume else 'paused', n=n))

    def detach(self) -> None:
        """Перестати ЧИТАТИ журнал, не чіпаючи самого прогону.

        Це не те саме, що `stop`, і плутати їх не можна — плутанина коштує
        годин рахунку. Закриття вікна мусить робити САМЕ ЦЕ: людина йде
        додому, а сервер рахує далі. Кнопка «Зупинити» — навпаки, має
        дістатися процесів на сервері й погасити їх.

        Перша версія цього не розрізняла: вікно при закритті кликало
        `stop`, і відчеплений прогін гинув від закритої кришки ноута —
        тобто рівно від того, заради чого його й відчіплювали.
        """
        self.left = True
        self._stop.set()
        self._wake.set()
        self._kill_local()

    def _kill_local(self) -> None:
        """Прибити СВІЙ процес — той, що читає журнал чи веде прогін."""
        p = self._proc
        if p and p.poll() is None:
            try:
                if os.name == "nt":
                    subprocess.run(["taskkill", "/F", "/T", "/PID", str(p.pid)],
                                   capture_output=True, creationflags=CREATE_NO_WINDOW)
                else:
                    p.terminate()
            except OSError:
                pass

    def soft_stop(self) -> tuple[bool, str]:
        """Записати зріз і зупинитися. Повертає (прийнято, що сказати людині).

        На відміну від `stop` нічого не вбиває: просить розвʼязувач
        (`stopAt writeNow`) дописати поточний момент і вийти, а сценарій
        потім збирає зріз (reconstructPar) і перевіряє, що він є.
        """
        if self.backend.kind == "ssh":
            target = self.remote_dir or self.native_dir
            argv = self.backend.soft_stop_argv(self.case_dir, target)
        else:
            argv = self.backend.soft_stop_argv(self.case_dir, self.native_dir)
        if argv is None:
            out = soft_stop_file(self.case_dir)
        else:
            rc, out = _run(argv, timeout=30)
            if rc != 0:
                return (False, tr("cannot reach the case: {v}", v=out.strip()[:200] or tr("code {rc}", rc=rc)))
        word = next((ln.strip() for ln in reversed(out.strip().splitlines())
                     if ln.strip()), "")
        if word == "nosolver":
            return (False, tr("the solver is not computing now (meshing or "
                           "reconstruction is in progress) — there is no "
                           "time step worth saving yet. Wait or click "
                           "“Stop”."))
        if not word.startswith("soft"):
            return (False, tr("system/controlDict has no stopAt line — a "
                           "soft stop is not possible"))
        self._soft.set()
        return (True, tr("the solver will finish the current time and stop "
                      "(up to one step), then the time step will be "
                      "collected and checked"))

    def stop(self) -> None:
        self._stop.set()
        self._wake.set()
        self._kill_local()
        # ВІДЧЕПЛЕНИЙ ПРОГІН УБИТИ МІСЦЕВИМИ ЗАСОБАМИ НЕМОЖЛИВО, і це не
        # вада, а те, заради чого він відчеплений. Обірвати трубу ssh
        # означає лише перестати ЧИТАТИ журнал; на сервері все рахує далі.
        #
        # Тому «Зупинити» має дістатися самих процесів — і саме СВОЇХ,
        # тих, що працюють у теці цього кейсу. Заразом гасимо сценарій-голову,
        # інакше він слухняно візьметься за наступний крок.
        if self.backend.kind == "ssh":
            target = self.remote_dir or self.native_dir
            if target:
                _run(ssh_argv(self.backend.host, stop_command(target)),
                     timeout=30)
        # У WSL процеси не належать дереву wsl.exe, тому глушимо їх окремо.
        # Але саме СВОЇ: pkill за іменем прибив би й сусідній прогін, якщо
        # користувач рахує два кейси паралельно або має щось своє в тому ж
        # дистрибутиві. Розрізняємо за робочим каталогом процесу.
        if self.backend.kind == "wsl":
            target = self.native_dir or win_to_wsl(self.case_dir)
            # pgrep -f бере РОЗШИРЕНИЙ регулярний вираз: альтернатива пишеться
            # просто через |, без зворотних скісних. З BRE-екрануванням "\|"
            # шаблон не збігається ні з чим, і зупинка мовчки нічого не робить.
            # Список процесів спільний із паузою: раніше він був свій, і в
            # ньому бракувало pimpleFoam — нестаціонарний прогін кнопка
            # «Зупинити» не зупиняла зовсім.
            _run(wsl_argv(self.backend.distro,
                          signal_script(target, "TERM")), timeout=25)

    # --- відчеплений прогін на сервері --------------------------------

    def run_detached(self, steps: list[Step], name: str = "",
                     remote_dir: str = "") -> None:
        """Пустити прогін на сервері так, щоб він пережив закриття вікна.

        Відмінність від `run` одна, але вона змінює все: кроки виконує не
        ця програма, а сценарій на сервері. Звідси програма лише читає
        журнал — і може перестати це робити будь-коли, хоч закрившись.
        """
        target = remote_dir or self.native_dir or remote_dir_for(name)
        meta = {
            "name": name or Path(self.case_dir).name,
            "case_dir": str(self.case_dir),
            "started": time.strftime("%Y-%m-%d %H:%M:%S"),
            "steps": [step_id(s.name) for s in steps],
        }
        script = detached_script(steps, target, self.backend.bashrc, meta,
                                 stop_on_bad_mesh=self.stop_on_bad_mesh,
                                 engine=self.backend.engine,
                                 image=self.backend.image)
        self.on_line(tr("Starting on {host} detached — the run will not "
                        "depend on this window.", host=self.backend.host))
        pid = launch_detached(self.backend, target, script)
        self.remote_dir = target
        self.on_line(tr("Server session: {target} (process {pid}).",
                        target=target, pid=pid))
        self.on_event("detached", target)
        self.on_line(tr("You can close the program: the run will not stop, "
                     "and you can connect to it again with the “Connect” "
                     "button."))
        self.attach(target, steps=steps)

    # Pauses (s) between the tries to connect again after the connection to
    # the server is lost (sleep of the laptop, a new network, Tailscale that
    # is not ready yet). The last value repeats (T44).
    RECONNECT_DELAYS = (2, 5, 10, 20, 40, 60)

    def reconnect_now(self) -> None:
        """Connect to the session again now (after the laptop wakes up).

        After a sleep the ssh of the log can be dead without an error: ssh
        sees the lost connection only after 3 x ServerAliveInterval. Close
        it; attach() then reads the state of the run and connects again
        from the next log line. The run on the server does not change.
        """
        if not self.remote_dir or self._stop.is_set():
            return
        self._wake.set()
        self._kill_local()

    def attach(self, remote_dir: str, from_line: int = 1,
               steps: "list[Step] | None" = None) -> None:
        """Читати журнал сесії з сервера, поки вона не скінчиться.

        Поновлює й стан: кожен рядок іде крізь той самий розбирач, що й при
        звичайному прогоні, тож підключення до прогону, який іде вже годину,
        відновлює і графіки, і кількість комірок, і фазу.

        A lost connection is not the end of the run (T44). When the ssh of
        the log stops and the server does not reply, or replies that the run
        continues, attach() waits (RECONNECT_DELAYS) and connects again from
        the next log line. on_event("link", "lost") and ("link", "back")
        tell the window. Only stop() or detach() end the wait.
        """
        self.log_dir.mkdir(parents=True, exist_ok=True)
        self.remote_dir = remote_dir
        self.final_status = ""
        names = [step_id(s.name) for s in steps] if steps else []
        total = len(names)
        got = 0            # lines of run.log that came in
        tries = 0          # tries in a row without a reply
        lost = False
        final = None

        # Журнал розкладаємо по файлах кроків на льоту: вікно посилає людину
        # саме в logs/<крок>.log, і при підключенні ці файли мають бути.
        #
        # ЧИТАЄМО БЛОКАМИ, А НЕ ПО РЯДКУ. На початку підключення приходить
        # увесь журнал прогону — пів мільйона рядків і більше. По рядку це
        # секунди (readline, розбір, запис і сигнал вікну на кожен рядок);
        # блоком — соті частки: один розбір (foamrs), один запис, один
        # сигнал. Далі блоки дрібні, і рядки йдуть так само одразу.
        self._attach_fh = None
        try:
            while True:
                argv = ssh_argv(self.backend.host,
                                attach_command(remote_dir, from_line + got))
                # stderr separately: the messages of ssh ("Broken pipe",
                # "Network is unreachable") are not lines of run.log. In the
                # log they would also move the line count of a reconnect.
                try:
                    self._proc = subprocess.Popen(
                        argv, stdout=subprocess.PIPE,
                        stderr=subprocess.DEVNULL, bufsize=0,
                        creationflags=CREATE_NO_WINDOW,
                    )
                except OSError as exc:
                    self.on_done(False, str(exc))
                    return
                assert self._proc.stdout is not None
                for block in read_blocks(self._proc.stdout):
                    got += block.count("\n") + 1
                    if lost:
                        lost, tries = False, 0
                        self.on_event("link", "back")
                    self._attach_block(block, names, total)
                    if self._stop.is_set():
                        break
                self._proc.wait()
                if self._stop.is_set():
                    break
                # Вирок беремо з файлу стану, а не з коду виходу ssh: ssh
                # каже лише про звʼязок, а не про те, чим скінчився прогін.
                try:
                    final = read_session(self.backend, remote_dir,
                                         strict=True)
                    reached = True
                except ConnectionError:
                    final, reached = None, False
                if reached and not (final is not None and final.running
                                    and final.alive):
                    break
                if reached and lost:
                    # The server replies again: connect at once.
                    lost, tries = False, 0
                    self.on_event("link", "back")
                elif not reached and not lost:
                    lost = True
                    self.on_event("link", "lost")
                delay = self.RECONNECT_DELAYS[
                    min(tries, len(self.RECONNECT_DELAYS) - 1)]
                tries += 1
                self._wake.clear()
                if not self._stop.is_set():
                    self._wake.wait(delay)
                if self._stop.is_set():
                    break
        finally:
            if self._attach_fh is not None:
                self._attach_fh.close()
                self._attach_fh = None

        if self._stop.is_set():
            self.on_done(False, tr("log reading stopped"))
            return
        if final is None:
            self.final_status = "gone"
            self.on_done(False, tr("the server did not tell how the run ended"))
        elif final.status == "ok":
            self.final_status = "ok"
            if final.soft:
                self._soft.set()
                self.soft_result = final.soft
            self.on_done(True, final.message or tr("done"))
        elif final.stale:
            self.final_status = "aborted"
            self.on_done(False, (
                tr("the run broke off: the state says “running”, but there "
                "is no process on the server. This is how a run killed "
                "by a reboot or by lack of memory looks.")))
        else:
            self.final_status = (final.status if final.status in
                                 ("fail", "stop") else "fail")
            what = final.message or tr("no explanation")
            self.on_done(False, f"{final.step_text or tr('run')}: {what}")

    def _attach_block(self, block: str, names: list[str], total: int) -> None:
        """Один блок журналу сесії: рядки кроків — у файли й розбирач,
        заголовки кроків («===== крок =====») — як і раніше, по одному."""
        pos = 0
        for start, end, line, title in step_heads(block):
            if start > pos:
                self._attach_lines(block[pos:start - 1])
            if self._attach_fh is not None:
                self._attach_fh.close()
                self._attach_fh = None
            if title != STEP_DONE:
                self.current_step = title
                self.state.reset_step()
                if names and title in names:
                    self.on_step(title, names.index(title), total)
                else:
                    self.on_step(title, 0, 0)
                try:
                    # Двійковий файл: ті самі байти, що дав би текстовий
                    # режим (див. _attach_lines), але без його повільного
                    # перекладу кінців рядків.
                    self._attach_fh = open(
                        self.log_dir / f"{_sh_name(title)}.log", "wb")
                except OSError:
                    self._attach_fh = None
            self.state.feed(line)
            self.on_line(step_banner(title))
            pos = end + 1
        if pos <= len(block):
            self._attach_lines(block[pos:])

    def _attach_lines(self, text: str) -> None:
        """Рядки без заголовків кроків (через "\\n", без останнього "\\n")."""
        if self._attach_fh is not None:
            data = (text + "\n").encode("utf-8")
            if os.linesep != "\n":
                data = data.replace(b"\n", os.linesep.encode())
            self._attach_fh.write(data)
            self._attach_fh.flush()
        self.state.feed_text(text)
        text = render_text(text)
        if self.on_block is not None:
            self.on_block(text)
        else:
            for line in text.split("\n"):
                self.on_line(line)

    def run(self, steps: list[Step]) -> None:
        self.log_dir.mkdir(parents=True, exist_ok=True)
        total = len(steps)
        for i, step in enumerate(steps):
            if self._stop.is_set():
                self.on_done(False, tr("stopped by the user"))
                return
            # Після мʼякої зупинки — лише збирання зрізу, без нових етапів.
            if self._soft.is_set() and not step.keep_on_soft:
                continue
            if step.cond and not self._cond_ok(step.cond):
                continue
            self.current_step = step.name
            self.on_step(step.name, i, total)
            ok, msg = self._run_step(step)
            if not ok:
                if step.optional:
                    self.on_line(tr("[{name}] optional step, continuing: {msg}", name=step_title(step.name), msg=msg))
                    continue
                self.on_done(False, f"{step_title(step.name)}: {msg}")
                return

            # checkMesh завершується з кодом 0 навіть коли перевірки не
            # пройдено, тому дивимося на розібраний текст — і на ВЕЛИЧИНИ,
            # а не на сам факт провалу: інакше одна гранична грань зупиняла б
            # цілком придатну сітку.
            if step.name == "checkMesh":
                level, why = self.state.mesh_severity()
                if level == "fatal" and self.stop_on_bad_mesh:
                    self.on_done(False, (
                        tr("the mesh is not usable: {why}. The solver "
                           "will almost certainly diverge on it. See "
                           "logs/checkMesh.log.", why=why)))
                    return
                if level == "warn":
                    self.on_line("")
                    tip = ""
                    if self.state.max_skewness > 5.0:
                        tip = (tr(" Skewness is clearly above the "
                               "threshold: if the solver diverges in the "
                               "first few hundred iterations, turn on "
                               "“Stable start”."))
                    self.on_line(tr("[checkMesh] remarks: {why}. For a "
                                    "real mesh this is within normal "
                                    "limits — continuing.{tip}", why=why, tip=tip))
        if self._soft.is_set():
            self._finish_soft()
            return
        self.on_done(True, tr("done"))

    def _cond_ok(self, cond: str) -> bool:
        """Чи виконувати крок з умовою (Step.cond) — питаємо саму теку кейсу."""
        rc, _ = _run(self.backend.shell(self.case_dir, cond_test(cond),
                                        self.native_dir), timeout=30)
        return rc == 0

    def _finish_soft(self) -> None:
        """Після мʼякої зупинки: повернути stopAt і ПЕРЕВІРИТИ зріз."""
        if self.backend.soft_stop_argv(self.case_dir, self.native_dir) is None:
            soft_restore_file(self.case_dir)
            (Path(self.case_dir) / STUDIO / SOFT_FLAG).unlink(missing_ok=True)
        else:
            _run(self.backend.shell(self.case_dir, soft_restore_command()
                                    + f'; rm -f "{STUDIO}/{SOFT_FLAG}"',
                                    self.native_dir), timeout=30)
        if self.backend.kind == "bluecfd":
            # cmd.exe, а не bash: перевіряємо Python-ом по тій самій мірці.
            ok, last = soft_check_local(self.case_dir)
            self.on_line(last)
            if ok:
                self.soft_result = last
                self.on_done(True, soft_done_text(last))
            else:
                self.on_done(False, tr("soft stop: the time step was not written ({last})", last=last))
            return
        ok, msg = self._run_step(Step(STEP_SOFT_CHECK, soft_check_command()))
        log = self.log_dir / f"{STEP_SOFT_CHECK}.log"
        last = next((ln for ln in reversed(
            log.read_text(encoding="utf-8", errors="replace").splitlines())
            if ln.strip()), "") if log.exists() else ""
        if not ok:
            self.on_done(False, tr("soft stop: the time step was not written ({v})",
                                   v=render_line(last) if last else msg))
            return
        self.soft_result = soft_slice_text(last)
        self.on_done(True, soft_done_text(self.soft_result))

    def _run_step(self, step: Step) -> tuple[bool, str]:
        # Скидаємо ряди перед ПЕРШИМ розвʼязувачем. potentialFoam теж рахує
        # з Time = 1, а другий етап двоетапного прогону продовжує нумерацію
        # першого — його історію затирати не можна.
        if step.solver and (step.first_stage or not self.state.residuals):
            self.state.reset_residuals()
        self.state.reset_step()
        argv = self.backend.shell(self.case_dir, step.command, self.native_dir)
        self.on_line("")
        self.on_line(step_banner(step.name))
        log_path = self.log_dir / f"{_sh_name(step_id(step.name))}.log"

        try:
            self._proc = subprocess.Popen(
                argv, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                bufsize=1, universal_newlines=True, encoding="utf-8",
                errors="replace", creationflags=CREATE_NO_WINDOW,
            )
        except OSError as exc:
            return (False, str(exc))

        with open(log_path, "w", encoding="utf-8") as fh:
            assert self._proc.stdout is not None
            # ЧИТАЄМО ЧЕРЕЗ readline, А НЕ `for line in stdout`.
            # Ітерація файлового обʼєкта тягне внутрішній буфер випереджального
            # читання: Python не віддає ЖОДНОГО рядка, доки той буфер не
            # наповниться. На трубі довгого розрахунку це означає, що програма
            # глухне — процес рахує далі, а вікно висить із нульовим CPU і
            # порожнім журналом. readline віддає рядок одразу, як він прийшов.
            for line in iter(self._proc.stdout.readline, ""):
                line = line.rstrip("\n")
                fh.write(line + "\n")
                fh.flush()      # інакше лог порожній, поки крок не завершиться
                self.state.feed(line)
                self.on_line(render_line(line))
                if self._stop.is_set():
                    break

        rc = self._proc.wait()
        if self.state.fatal:
            return (False, self.state.fatal)
        if rc != 0:
            return (False, tr("exit code {rc}, see {log}", rc=rc, log=f"logs/{log_path.name}"))
        return (True, "")


# --------------------------------------------------------------------------
# Копіювання кейсу в рідну ФС WSL
# --------------------------------------------------------------------------

# Що саме потрібно OpenFOAM, щоб порахувати кейс із нуля. Решта — сітка,
# часові зрізи, processor*, postProcessing, логи — це результат, і копіювати
# його в WSL немає сенсу: перший же крок прогону його прибирає. На кейсі
# plane5 це різниця між 283 МБ і 8 МБ через 9p.
SYNC_INPUTS = ("system", "constant", "0.orig", "Allrun", "Allclean")


def _pipe(argv: list[str], stdin_path=None, stdout_path=None,
          timeout: int = 1800) -> tuple[int, str]:
    """subprocess із файлом на вході або виході — для тару через ssh."""
    fin = open(stdin_path, "rb") if stdin_path else None
    fout = open(stdout_path, "wb") if stdout_path else None
    try:
        p = subprocess.run(argv, stdin=fin,
                           stdout=fout or subprocess.PIPE,
                           stderr=subprocess.PIPE, timeout=timeout,
                           creationflags=CREATE_NO_WINDOW)
    except (OSError, subprocess.SubprocessError) as exc:
        return (1, str(exc))
    finally:
        if fin:
            fin.close()
        if fout:
            fout.close()
    err = (p.stderr or b"").decode("utf-8", "replace")
    return (p.returncode, err)


def remote_dir_for(name: str) -> str:
    """The folder of the run `name` on a server or in WSL (bash text)."""
    return f"{fs_paths.REMOTE_ROOT}/{name}"


def remote_shown(name: str) -> str:
    """The folder of the run `name` in a text for the person."""
    return f"{fs_paths.REMOTE_SHOWN}/{name}"


def server_case_names(backend: Backend) -> set[str]:
    """Names in ~/openfoam-studio on the server (ssh) or in WSL (T43).

    A new run must not take one of them (fs_caseguard.new_run_name).
    Exception: the server did not answer.
    """
    cmd = f'ls -1A "{SESSION_ROOT}" 2>/dev/null; echo "@@FS names-end"'
    if backend.kind == "ssh":
        rc, out = _run(ssh_argv(backend.host, cmd), timeout=45)
    elif backend.kind == "wsl":
        rc, out = _run(wsl_argv(backend.distro, cmd), timeout=45)
    else:
        return set()
    if rc != 0 or "@@FS names-end" not in out:
        raise RuntimeError(out.strip()[:200] or tr("the server did not reply"))
    return {ln.strip() for ln in out.splitlines()
            if ln.strip() and not ln.startswith("@@FS")}


def _guard_refused(rc: int, err: str, folder: str, where: str) -> None:
    """Raise fs_caseguard.CaseBusy if the guard refused the upload."""
    got = guard.parse_guard(err) if rc == guard.GUARD_EXIT else None
    if got:
        raise guard.CaseBusy(got[0], got[1], folder, where)


def sync_to_ssh(backend: Backend, case_dir: str, name: str,
                own: bool = False) -> str:
    """Кладе вхідні дані кейсу на сервер і повертає тамтешній шлях.

    Тар в один потік, а не scp пофайлово: у кейсі тисячі дрібних файлів, і
    окрема передача на кожен коштувала б більше, ніж самі дані.

    Їде лише вхідне. Сітку сервер збудує сам — гнати півмільйона комірок по
    мережі, щоб потім там-таки їх перерахувати, безглуздо.

    The old folder is deleted only when fs_caseguard lets it (T43): no live
    run in it, and no results that were not downloaded. `own`: the main
    run after the pre-run of the program (T39) replaces the pre-run results.
    """
    guard.check_name(name)
    src = Path(case_dir)
    items = [i for i in SYNC_INPUTS if (src / i).exists()]
    if not items:
        raise RuntimeError(tr("the case has none of {SYNC_INPUTS}", SYNC_INPUTS=SYNC_INPUTS))
    remote = remote_dir_for(name)
    tmp = Path(tempfile.gettempdir()) / f"ofs-{name}.tar"
    try:
        with tarfile.open(tmp, "w") as tar:
            for item in items:
                # Сітку з попереднього прогону не везмо: сервер збудує свою.
                tar.add(src / item, arcname=item, filter=lambda ti: (
                    None if ti.name.startswith("constant/polyMesh") else ti))
        rc, err = _pipe(ssh_argv(
            backend.host,
            guard.guarded(remote, (
                f'rm -rf -- "{remote}" && mkdir -p "{remote}" && '
                f'tar xf - -C "{remote}"'), own=own)), stdin_path=tmp)
    finally:
        tmp.unlink(missing_ok=True)
    _guard_refused(rc, err, remote_shown(name), backend.host)
    if rc != 0:
        raise RuntimeError(tr("cannot put the case on the server: {strip}", strip=err.strip()[:200]))
    return remote


# Що забирати з сервера. Вхідні дані назад не їдуть: вони й так у нас, а
# перезапис їх копією з сервера лише плодив би різницю на рівному місці.
SYNC_OUTPUTS = ("postProcessing", "log", "logs", "constant/polyMesh")


def sync_from_ssh(backend: Backend, remote_dir: str, case_dir: str) -> None:
    """Забирає результати з сервера.

    Спершу прибирає СТАРІ результати на нашому боці, і лише потім
    розпаковує — з тієї самої причини, що й у WSL-версії: інакше кейс стає
    сумішшю поколінь, де сітка з одного прогону, а поля з іншого, і ParaView
    каже «Number of cells/points in mesh and field do not match».

    Прибираємо саме перелічене, а не все підряд: у каталозі кейсу люди
    тримають і свої файли.
    """
    dst = Path(case_dir)
    for d in dst.iterdir() if dst.is_dir() else []:
        name = d.name
        if d.is_dir() and name not in ("0.orig",) and name.replace(".", "").isdigit():
            shutil.rmtree(d, ignore_errors=True)
    for rel in SYNC_OUTPUTS:
        shutil.rmtree(dst / rel, ignore_errors=True)
    tmp = Path(tempfile.gettempdir()) / f"ofs-back-{Path(case_dir).name}.tar"
    # Часові каталоги перелічує сам сервер: імена наперед невідомі, а
    # шаблон [0-9]* збігся б і з чимось на кшталт «3d_model».
    # Only time 0 and the LAST time: the run reconstructs only the last one,
    # and the other times that the «Times from Server» dialog reconstructed
    # on the server (T40) are 0.5 GB each — the person downloads them there,
    # on request.
    listing = (
        f'cd "{remote_dir}" || exit 1; shopt -s nullglob; out=""; ts=""; '
        'for d in [0-9]*/; do n=${d%/}; '
        'case "$n" in ""|*[!0-9.]*) continue;; esac; '
        'if [ "$n" = 0 ]; then out="$out 0"; else ts="$ts $n"; fi; done; '
        'last=$(printf "%s\\n" $ts | sort -g | tail -1); '
        '[ -n "$last" ] && out="$out $last"; '
        + "".join(f'[ -e "{r}" ] && out="$out {r}"; ' for r in SYNC_OUTPUTS)
        # Проміжні поверхні (fs_live) лишаються на сервері: це десятки МБ на
        # кожен запис, а після прогону є повні зрізи.
        + '[ -z "$out" ] && exit 3; '
        "tar cf - --exclude='postProcessing/studioLive*' $out"
    )
    rc, err = _pipe(ssh_argv(backend.host, listing), stdout_path=tmp)
    try:
        if rc == 3:
            raise RuntimeError(tr("no results on the server: the run did "
                               "not get to a write"))
        if rc != 0:
            raise RuntimeError(tr("cannot get the results from the server: {v}", v=err.strip()[:200] or tr("code {rc}", rc=rc)))
        with tarfile.open(tmp, "r") as tar:
            tar.extractall(dst)
    finally:
        tmp.unlink(missing_ok=True)
    mark_fetched(backend, remote_dir)


def mark_fetched(backend: Backend, remote_dir: str) -> None:
    """Note on the server: the results of this folder are on the laptop.

    fs_caseguard lets a later upload replace such a folder; a folder with
    results that nobody downloaded stays. A failure here only makes the
    guard more careful, so it is not an error.
    """
    cmd = (f'[ -d "{remote_dir}/{STUDIO}" ] || mkdir -p "{remote_dir}/{STUDIO}"; '
           f'touch "{remote_dir}/{guard.FETCHED}"')
    try:
        if backend.kind == "ssh":
            _run(ssh_argv(backend.host, cmd), timeout=30)
        elif backend.kind == "wsl":
            _run(wsl_argv(backend.distro, cmd), timeout=30)
    except Exception:                                     # noqa: BLE001
        pass


def run_server_dir(case_dir: str, run: "dict | None" = None) -> tuple[str, str]:
    """(host, folder) on the server of a run of this local case (T43).

    `run`: one entry of the «runs» list of the manifest (studio.json); None:
    the last upload of the case. The folder is the one the program used,
    also for «<name>_r2». A case from before T43 has no record: then
    ("", remote_dir_for(<folder name>)), the old rule.
    """
    got = run.get("server") if isinstance(run, dict) else None
    if not isinstance(got, dict):
        import fs_library as lib               # lib imports this module
        data = lib.read_manifest(case_dir) or {}
        got = data.get("server") if run is None else None
    if isinstance(got, dict) and got.get("dir"):
        return (str(got.get("host", "")), str(got["dir"]))
    return ("", remote_dir_for(Path(case_dir).name))


def sync_to_wsl(backend: Backend, case_dir: str, name: str,
                own: bool = False) -> str:
    """Копіює вхідні дані кейсу в ~/openfoam-studio/<name> і повертає шлях.

    Робота через /mnt/x іде по 9p і на файлових операціях втрачає в рази,
    тому для реальних прогонів кейс варто тримати в рідній ФС.
    """
    guard.check_name(name)
    src = win_to_wsl(case_dir)
    dst = remote_dir_for(name)
    items = " ".join(f'"{src}/{item}"' for item in SYNC_INPUTS)
    # T43: the old folder goes only when fs_caseguard lets it.
    cmd = f'mkdir -p "{fs_paths.REMOTE_ROOT}"; ' + guard.guarded(dst, (
        f'rm -rf "{dst}" && '
        f'mkdir -p "{dst}" && '
        # Наявні елементи копіюємо, відсутніх не вимагаємо: Allclean чи
        # 0.orig у чужому кейсі може й не бути.
        f'for item in {items}; do [ -e "$item" ] && cp -r "$item" "{dst}/"; '
        f'done; '
        # Реконструйована сітка з попереднього прогону в constant/ теж
        # непотрібна — blockMesh і snappy зроблять свою.
        f'rm -rf "{dst}/constant/polyMesh" && '
        f'echo "{dst}"'), own=own)
    rc, out = _run(wsl_argv(backend.distro, cmd), timeout=600)
    _guard_refused(rc, out, remote_shown(name), f"WSL {backend.distro}")
    if rc != 0:
        raise RuntimeError(tr("cannot copy the case to WSL: {strip}", strip=out.strip()))
    return out.strip().splitlines()[-1].strip()


def sync_from_wsl(backend: Backend, native_dir: str, case_dir: str) -> None:
    """Повертає результати з рідної ФС WSL назад на диск Windows.

    Спершу прибирає на боці Windows СТАРІ результати, і лише потім копіює.
    Проста копія лишала часові каталоги всіх попередніх прогонів, а
    `constant/polyMesh` приходив новий — і кейс виявлявся сумішшю поколінь.
    ParaView на старому зрізі видавав «Number of cells/points in mesh and
    field do not match», бо сітка й поля були з різних прогонів; на plane5
    так зібралося три покоління: 253 тис., 430 343 і 430 573 комірки.

    Прибираємо саме перелічене, а не «все, чого немає в WSL»: у каталозі
    кейсу люди тримають і свої файли (той самий `3d_model/` із вихідною
    моделлю), і дзеркалення з `--delete` знесло б їх разом зі сміттям. Логи
    пише сама програма прямо в каталог на Windows, тому вони тут теж
    недоторканні.
    """
    dst = win_to_wsl(case_dir)
    cmd = (
        f'cd "{dst}" || exit 1; shopt -s nullglob; '
        # Числові каталоги часу, окрім 0.orig. Перевірка на суто числове
        # імʼя обовʼязкова: шаблон сам по собі збігся б і з «3d_model».
        f'for d in [0-9]*/; do n=${{d%/}}; '
        f'case "$n" in ""|*[!0-9.]*) continue;; esac; rm -rf "$d"; done; '
        f'rm -rf processor* postProcessing constant/polyMesh; '
        f'cp -r "{native_dir}/." "{dst}/" && '
        # T43: the results are here now (mark_fetched, in one command).
        f'mkdir -p "{native_dir}/{STUDIO}" && touch "{native_dir}/{guard.FETCHED}"')
    rc, out = _run(wsl_argv(backend.distro, cmd), timeout=900)
    if rc != 0:
        # Раніше помилки тут глушилися через 2>/dev/null, і провалена копія
        # виглядала як успішна: людина дивилася на результати попереднього
        # прогону й не мала жодного способу про це дізнатися.
        raise RuntimeError(
            tr("cannot get the results back from WSL: {v}", v=out.strip().splitlines()[-1] if out.strip() else tr("code {rc}", rc=rc)))


def find_paraview() -> str:
    """Шлях до paraview.exe або порожній рядок.

    Шукаємо нативний Windows-білд навмисно. paraFoam усередині WSL вимагає
    X-сервера: WSLg є лише у Windows 11, а на Windows 10 довелося б ставити
    VcXsrv і возити графіку через мережевий сокет.
    """
    exe = shutil.which("paraview")
    if exe:
        return exe
    roots = [Path(r"C:\Program Files"), Path(r"C:\Program Files (x86)")]
    found: list[Path] = []
    for root in roots:
        try:
            for d in root.glob("ParaView*"):
                found.extend(d.glob("bin/paraview.exe"))
        except OSError:
            continue
    if not found:
        return ""
    # найсвіжіша версія — імена виду ParaView-5.13.1-Windows-...
    found.sort(key=lambda p: p.parent.parent.name)
    return str(found[-1])


PARAVIEW_URL = "https://www.paraview.org/download/"


# Скільки ЯДЕР, а не потоків. `nproc` рахує логічні процесори, тобто з
# гіпертредингом, і на звичайному ноуті завищує вдвічі: шість ядер
# i5-11400H він подає як дванадцять.
#
# Для CFD це не дрібниця. Два потоки одного ядра ділять ті самі
# виконавчі блоки й той самий кеш, а розвʼязувач упирається не в
# арифметику, а в пропускну здатність памʼяті — тож удвічі більше
# процесів дає те саме або повільніше, зате вдвічі більше обміну між
# підобластями. Людина, якій написали «12 ядер», ставить дванадцять
# процесів і дивується, чому стало гірше.
_CORE_PROBE = ('lscpu -b -p=Core,Socket 2>/dev/null | grep -v "^#" '
               '| sort -u | wc -l')
_RES_PROBE = f"nproc; {_CORE_PROBE}; free -m | awk '/^Mem:/{{print $2}}'"


def _parse_resources(out: str) -> "tuple[int, int, float]":
    """(фізичних ядер, потоків, ГБ) з виводу `_RES_PROBE`."""
    lines = [ln.strip() for ln in out.splitlines() if ln.strip()]
    threads = int(lines[0])
    # lscpu є не всюди (є образи без util-linux). Нуль звідти означає
    # «не знаю», і тоді чесніше взяти потоки, ніж вигадати.
    cores = int(lines[1]) or threads
    ram = int(lines[2]) / 1024.0
    return (max(1, min(cores, threads)), max(1, threads), ram)


def wsl_resources(backend: Backend) -> tuple[int, int, float]:
    """(фізичних ядер, потоків, ГБ памʼяті) всередині WSL.

    Не відповів — памʼять 0.0, тобто «невідома». Раніше тут стояло 8.0, і
    перевірка памʼяті впевнено казала «не влізе в 8 ГБ» про число, якого
    ніхто не міряв."""
    rc, out = _run(wsl_argv(backend.distro, _RES_PROBE), timeout=60)
    try:
        return _parse_resources(out)
    except (ValueError, IndexError):
        n = os.cpu_count() or 4
        return (max(1, n // 2), n, 0.0)


def local_ram_gb() -> float:
    """Уся памʼять цього компʼютера, ГБ (для Docker, BlueCFD, native).
    0.0 — не вдалося дізнатися."""
    try:
        if os.name == "nt":
            import ctypes

            class _MemStatus(ctypes.Structure):
                _fields_ = [("dwLength", ctypes.c_ulong),
                            ("dwMemoryLoad", ctypes.c_ulong),
                            ("ullTotalPhys", ctypes.c_ulonglong),
                            ("ullAvailPhys", ctypes.c_ulonglong),
                            ("ullTotalPageFile", ctypes.c_ulonglong),
                            ("ullAvailPageFile", ctypes.c_ulonglong),
                            ("ullTotalVirtual", ctypes.c_ulonglong),
                            ("ullAvailVirtual", ctypes.c_ulonglong),
                            ("ullAvailExtendedVirtual", ctypes.c_ulonglong)]
            ms = _MemStatus()
            ms.dwLength = ctypes.sizeof(_MemStatus)
            if ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(ms)):
                return ms.ullTotalPhys / 2 ** 30
            return 0.0
        return os.sysconf("SC_PAGE_SIZE") * os.sysconf("SC_PHYS_PAGES") / 2 ** 30
    except (OSError, ValueError, AttributeError):
        return 0.0


def ssh_resources(backend: Backend) -> tuple[int, int, float]:
    """Те саме для сервера. Саме там воно й важить найбільше.

    Чекаємо коротко. Питання тут — `nproc` і `free`, тобто мілісекунди
    роботи; усе, що довше за кілька секунд, це вже не рахунок, а мережа.
    А оскільки питає нитка ВІКНА, довге очікування виглядає як підвисла
    програма, і краще лишитися без числа, ніж без вікна.
    """
    rc, out = _run(ssh_argv(backend.host, _RES_PROBE), timeout=20)
    try:
        return _parse_resources(out)
    except (ValueError, IndexError):
        return (0, 0, 0.0)          # нуль = не питали або не відповіли
