"""Чи влізе задача в памʼять — одна перевірка на трьох споживачів.

Рядок стану під вікном, помічник (який знижує рівень, доки задача не
влізе) і питання перед запуском рахували це кожен по-своєму: стан брав
85 % памʼяті, помічник і запуск — 60 %; усі троє — лише пік snappyHexMesh
і лише памʼять «якогось» бекенда, а коли її ще не спитали, мовчки 8 ГБ.
Звідси «⚠ сітка не влізе в 8 ГБ» для задачі, яка йде на сервері з 46 ГБ,
ще й зі збереженою сіткою, тобто взагалі без snappyHexMesh.

Тепер усе тут:
  * snappyHexMesh рахується лише тоді, коли сітку справді будуватимуть, і
    на ТІЙ машині, де її будують (сервер — навіть у розподіленому прогоні);
  * розвʼязувач — на кожній машині своєю часткою комірок проти ЇЇ памʼяті;
  * невідома памʼять — це «невідомо», а не 8 ГБ: про неї кажемо, але
    запуску не заважаємо.
"""

from __future__ import annotations

import gzip
import os
import re
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Callable

import fs_physics as phys
from fs_i18n import tr

# Яку частку памʼяті дозволяємо зайняти задачі.
#
# 0.6 там, де поруч живе Windows (WSL, Docker Desktop, BlueCFD): памʼять WSL
# — не вся памʼять машини, а віртуальна машина WSL ще й роздувається під
# тиском. Прогін, у якому сітка передбачала 0.6 ГБ, система зняла саме через
# брак памʼяті на боці Windows, а не всередині WSL.
# 0.85 для Linux-машини (сервер, вузли розподілу): там поруч лише сама ОС.
RAM_HEADROOM_SHARED = 0.6
RAM_HEADROOM_SERVER = 0.85

SHARED_KINDS = ("wsl", "docker", "bluecfd")


def headroom_for(kind: str) -> float:
    return RAM_HEADROOM_SHARED if kind in SHARED_KINDS else RAM_HEADROOM_SERVER


def fmt_gb(gb: float) -> str:
    """7.6, а не «8»: округлення до цілого ховало саме ту різницю, через
    яку задача не влазить. Від 10 ГБ і більше десяті вже не важать."""
    return f"{gb:.1f}" if gb < 10 else f"{gb:.0f}"


@dataclass
class MemHost:
    """Машина, на якій щось рахується. `ram_gb` 0 — памʼять ще невідома."""
    name: str
    ram_gb: float = 0.0
    headroom: float = RAM_HEADROOM_SERVER
    ranks: int = 1
    share: float = 1.0          # частка комірок (вага) на розвʼязувачі

    @property
    def known(self) -> bool:
        return self.ram_gb > 0

    @property
    def budget_gb(self) -> float:
        return self.ram_gb * self.headroom


@dataclass
class MemRow:
    host: MemHost
    stage: str                  # "snappy" | "solver"
    cells: int
    need_gb: float

    @property
    def fits(self) -> "bool | None":
        """None — памʼять машини невідома."""
        if not self.host.known:
            return None
        return self.need_gb <= self.host.budget_gb

    def line(self) -> str:
        h = self.host
        what = ("snappyHexMesh" if self.stage == "snappy"
                else tr("розвʼязувач × {ranks}", ranks=h.ranks))
        head = (tr("{what} на {name}: {cells} комірок, "
                "~{need_gb:.1f} ГБ", what=what, name=h.name, cells=phys.fmt_cells(self.cells), need_gb=self.need_gb))
        if not h.known:
            return tr("{head} — памʼять машини ще невідома", head=head)
        verdict = "влізе" if self.fits else "НЕ ВЛІЗЕ"
        return (tr("{head} з {ram_gb} ГБ (під задачу "
                "{budget_gb}) — {verdict}", head=head, ram_gb=fmt_gb(h.ram_gb), budget_gb=fmt_gb(h.budget_gb), verdict=verdict))


@dataclass
class MemFit:
    cells: int
    saved: bool                 # сітка збережена: snappyHexMesh не буде
    rows: list = field(default_factory=list)

    @property
    def over(self) -> list:
        return [r for r in self.rows if r.fits is False]

    @property
    def unknown(self) -> list:
        return [r for r in self.rows if r.fits is None]

    @property
    def verdict(self) -> str:
        """over — не влізе; unknown — є машини з невідомою памʼяттю; ok."""
        if self.over:
            return "over"
        return "unknown" if self.unknown else "ok"

    @property
    def worst(self) -> "MemRow | None":
        """Рядок, що не влазить найбільше (за відношенням до бюджету)."""
        rows = self.over
        if not rows:
            return None
        return max(rows, key=lambda r: r.need_gb / max(r.host.budget_gb, 1e-9))

    def solver_rows(self) -> list:
        return [r for r in self.rows if r.stage == "solver"]

    def warning(self) -> str:
        """Текст для рядка стану; "" — усе влазить."""
        bits = []
        w = self.worst
        if w is not None:
            h = w.host
            if w.stage == "snappy":
                bits.append(
                    tr("⚠ сітка не влізе на {name}: snappyHexMesh "
                    "~{need_gb:.1f} ГБ, памʼяті {ram_gb} ГБ "
                    "(під задачу {budget_gb})", name=h.name, need_gb=w.need_gb, ram_gb=fmt_gb(h.ram_gb), budget_gb=fmt_gb(h.budget_gb)))
            else:
                src = "збереженої сітки " if self.saved else ""
                bits.append(
                    tr("⚠ розвʼязувач не влізе на {name}: "
                    "{cells} комірок {src}~{need_gb:.1f} ГБ, "
                    "памʼяті {ram_gb} ГБ "
                    "(під задачу {budget_gb})", name=h.name, cells=phys.fmt_cells(w.cells), src=src, need_gb=w.need_gb, ram_gb=fmt_gb(h.ram_gb), budget_gb=fmt_gb(h.budget_gb)))
        names = []
        for r in self.unknown:
            if r.host.name not in names:
                names.append(r.host.name)
        if names:
            dist = len(self.solver_rows()) > 1 and any(
                r.stage == "solver" for r in self.unknown)
            bits.append(tr("ℹ памʼять {names} ще невідома — там памʼять "
                           "не перевіряю{v}", names=", ".join(names), v=" (кнопка «Перевірити машини»)" if dist else ""))
        return "   ".join(bits)

    def details(self) -> str:
        """Усе по машинах — для підказки й діалогу перед запуском."""
        lines = [tr("Памʼять (оцінка):")]
        if self.saved:
            lines.append(tr("  сітка збережена ({cells} "
                         "комірок) — snappyHexMesh не запускається", cells=phys.fmt_cells(self.cells)))
        for r in self.rows:
            lines.append("  " + r.line())
        sol = self.solver_rows()
        if len(sol) > 1:
            need = sum(r.need_gb for r in sol)
            known = [r.host for r in sol if r.host.known]
            ram = sum(h.ram_gb for h in known)
            if len(known) == len(sol):
                tail = tr(" з {ram} ГБ", ram=fmt_gb(ram))
            elif known:
                tail = (tr(" (памʼять відома лише для {n_known} з них: "
                        "{ram} ГБ)", n_known=len(known), ram=fmt_gb(ram)))
            else:
                tail = ""
            lines.append(tr("  разом розвʼязувач: ~{need:.1f} ГБ на "
                         "{n_sol} машинах{tail}", need=need, n_sol=len(sol), tail=tail))
        return "\n".join(lines)


def memory_fit(est: "phys.MeshEstimate", mesher: MemHost,
               solvers: "list[MemHost] | None" = None,
               saved_cells: int = 0) -> MemFit:
    """Чи влізе: snappyHexMesh (якщо сітку будують) на `mesher`, і
    розвʼязувач на кожній з `solvers` (без них — на `mesher` цілим).

    `saved_cells` > 0 — сітка вже є (збережена) з такою кількістю комірок:
    тоді snappyHexMesh не буде, а розвʼязувач рахуємо від справжнього числа
    комірок, а не від оцінки.

    Розкладання збереженої сітки (decomposePar на сервері) окремо не
    рахуємо: коефіцієнта для нього не міряли, а цілий розвʼязувач на тій
    самій сітці бере більше.
    """
    saved = saved_cells > 0
    cells = int(saved_cells) if saved else int(est.total)
    rows = []
    if not saved:
        rows.append(MemRow(mesher, "snappy", int(est.total),
                           est.snappy_peak_gb))
    hosts = solvers or [replace(mesher, share=1.0)]
    total_share = sum(max(h.share, 0.0) for h in hosts) or 1.0
    for h in hosts:
        n = int(round(cells * max(h.share, 0.0) / total_share))
        rows.append(MemRow(h, "solver", n,
                           n / 1.0e6 * phys.SOLVER_GB_PER_MCELL))
    return MemFit(cells=cells, saved=saved, rows=rows)


# --------------------------------------------------------------------------
# Розподіл: машини → MemHost
# --------------------------------------------------------------------------

def dist_hosts(machines, weights=(), head_ram_gb: float = 0.0) -> "list[MemHost]":
    """Машини розподілу (`DistMachine`) → MemHost з часткою комірок.

    Частка — сума ваг процесів цієї машини (`weights`, по одній на процес,
    у порядку машин — так їх кладе foamdist); без ваг — за кількістю
    процесів. Памʼять — з `foamdist probe` (`ram_gb` машини). Першою в
    списку стоїть сам сервер: якщо його памʼять ще не питали, беремо
    памʼять бекенда (`head_ram_gb`).
    """
    total = sum(m.procs for m in machines)
    w = [float(x) for x in weights] if len(weights) == total else [1.0] * total
    out, at = [], 0
    for i, m in enumerate(machines):
        share = sum(w[at:at + m.procs])
        at += m.procs
        ram = float(getattr(m, "ram_gb", 0.0) or 0.0)
        if ram <= 0 and i == 0:
            ram = head_ram_gb
        out.append(MemHost(name=m.label(), ram_gb=ram,
                           headroom=RAM_HEADROOM_SERVER, ranks=m.procs,
                           share=share))
    return out


# --------------------------------------------------------------------------
# Збережена сітка: чи підійде вона до ТЕПЕРІШНІХ налаштувань
# --------------------------------------------------------------------------

_NCELLS_RE = re.compile(rb"nCells:\s*(\d+)")


def mesh_cells(mesh_dir) -> int:
    """Кількість комірок сітки з заголовка polyMesh/owner ("note"). 0 — не
    вдалося. Читаємо лише перші кілобайти: сам файл — десятки мегабайтів."""
    pm = Path(mesh_dir) / "polyMesh"
    for name, opener in (("owner", open), ("owner.gz", gzip.open)):
        f = pm / name
        if not f.is_file():
            continue
        try:
            with opener(f, "rb") as fh:
                head = fh.read(4096)
        except (OSError, EOFError):
            return 0
        m = _NCELLS_RE.search(head)
        return int(m.group(1)) if m else 0
    return 0


class SavedMesh:
    """Чи візьме прогін збережену сітку замість snappyHexMesh.

    Ключ сітки рахується з файлів кейсу (`fs_backend.mesh_key`): словники
    сітки + геометрія в triSurface. Для рядка стану кейс ще не записано,
    тому порівнюємо словники, які вийшли б ЗАРАЗ (`mesh_dicts(cfg)`), з
    тими, що вже лежать:
      * у теці кейсу (записаний останнім «Створити кейс» чи прогоном);
      * у проєкті з вбудованою сіткою (ключ у проєкті).
    Збіглися — ключ того джерела; сітка з ним є на цьому компʼютері — її
    комірки. Хеш геометрії рахуємо раз на стан файлів, не на кожен рух.
    """

    def __init__(self, mesh_dicts: Callable, mesh_key: Callable,
                 local_mesh_dir: Callable, inputs,
                 project_source: "Callable | None" = None):
        self._dicts = mesh_dicts            # cfg -> {шлях: текст}
        self._key = mesh_key                # case_dir -> ключ
        self._local = local_mesh_dir        # ключ -> тека | None
        self._inputs = tuple(inputs)
        self._project_source = project_source   # path -> (cfg, ключ) | None
        self._case_cache: dict = {}
        self._proj_cache: dict = {}
        self._cells_cache: dict = {}

    @staticmethod
    def _stamp(paths) -> tuple:
        out = []
        for p in paths:
            try:
                st = os.stat(p)
                out.append((str(p), st.st_mtime_ns, st.st_size))
            except OSError:
                out.append((str(p), 0, -1))
        return tuple(out)

    def _case_source(self, case_dir: str):
        root = Path(case_dir)
        if not case_dir or not root.is_dir():
            return None
        files = [root / rel for rel in self._inputs]
        tri = root / "constant" / "triSurface"
        if tri.is_dir():
            files += sorted(f for f in tri.rglob("*") if f.is_file())
        stamp = self._stamp(files)
        hit = self._case_cache.get(case_dir)
        if hit is not None and hit[0] == stamp:
            return hit[1]
        dicts = {}
        for rel in self._inputs:
            try:
                dicts[rel] = (root / rel).read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError):
                dicts[rel] = None
        src = (dicts, self._key(case_dir))
        self._case_cache = {case_dir: (stamp, src)}
        return src

    def _proj_src(self, path: str):
        if not path or self._project_source is None:
            return None
        stamp = self._stamp([path])
        hit = self._proj_cache.get(path)
        if hit is not None and hit[0] == stamp:
            return hit[1]
        src = None
        got = self._project_source(path)
        if got:
            cfg, key = got
            if key:
                src = (self._dicts(cfg), key)
        self._proj_cache = {path: (stamp, src)}
        return src

    def cells(self, mesh_dir) -> int:
        key = str(mesh_dir)
        if key not in self._cells_cache:
            self._cells_cache[key] = mesh_cells(mesh_dir)
        return self._cells_cache[key]

    def find(self, cfg, case_dir: str = "", project: str = "") -> "tuple[str, int]":
        """(ключ, комірок) збереженої сітки для `cfg` або ("", 0)."""
        want = self._dicts(cfg)
        for src in (self._case_source(case_dir), self._proj_src(project)):
            if not src:
                continue
            dicts, key = src
            if not key or dicts != want:
                continue
            d = self._local(key)
            if d is None:
                continue
            n = self.cells(d)
            if n > 0:
                return key, n
        return "", 0
