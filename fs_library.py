"""Бібліотека збережених симуляцій: маніфест кейсу і читання його з диска.

Готовий кейс OpenFOAM не зберігає налаштувань програми. У `system/` лежать
уже розгорнуті словники, з яких вихідні числа не відновити: рівні
рефайнменту ще так-сяк читаються, а кут атаки, Aref, одиниці STL чи
двоетапність — ні, вони розчинені в готових полях, схемах і `fvOptions`.
Тому поряд із кейсом пишеться `studio.json` — рівно те, що було в `CaseCfg`
на момент запису, плюс що з цього вийшло після прогону.

Читання навмисно терпляче. Маніфест — звичайний текстовий файл у каталозі
кейсу; його правлять руками й переносять між машинами. Тому кожне значення
перевіряється, а негідні відкидаються поіменно (`apply_dict` повертає їх
список), а не валять відкриття кейсу винятком. Окремо перевіряються поля з
фіксованим набором значень: якби в маніфест потрапила модель турбулентності,
якої немає в списку, комбобокс мовчки лишився б на старій — інтерфейс
показував би одне, а кейс рахувався б з іншим.

Записуємо через тимчасовий файл і `os.replace`: маніфест перезаписується на
кожному створенні кейсу й після кожного прогону, і обрив на півслові лишив
би по собі каліку, яку вже нічим не прочитати.
"""

from __future__ import annotations

import json
import math
import os
import re
import shutil
import time
import zipfile
from dataclasses import asdict, dataclass, field, fields, is_dataclass
from pathlib import Path

import fs_backend as be
import fs_case as fc
import fs_post as post

MANIFEST = "studio.json"
FORMAT = 1
RUNS_KEPT = 20

# Поля з фіксованим набором значень: усе інше інтерфейс не покаже.
ENUMS: dict[str, tuple[str, ...]] = {
    "turbulence": tuple(fc.TURBULENCE_MODELS),
    # Без цього рядка з маніфесту пройшов би БУДЬ-ЯКИЙ рядок: _coerce для
    # звичайного str перевіряє лише тип. Далі solver_name() падає з
    # KeyError уже при створенні кейсу, і з тексту помилки не видно, що
    # винен зіпсований маніфест.
    "regime": tuple(fc.REGIMES),
    "farfield_mode": ("freestream", "tunnel"),
    "decompose_method": ("scotch", "hierarchical", "simple"),
    "write_format": ("binary", "ascii"),
}

STATE_RESULTS = "результати"
STATE_MESH = "сітка"
STATE_SETTINGS = "налаштування"


def _now() -> str:
    return time.strftime("%Y-%m-%d %H:%M")


# ==========================================================================
# CaseCfg <-> dict
# ==========================================================================

def cfg_to_dict(cfg: fc.CaseCfg) -> dict:
    """Конфіг у звичайний dict. Кортежі стануть списками вже в JSON."""
    return asdict(cfg)


def apply_dict(cfg: fc.CaseCfg, data: dict | None) -> list[str]:
    """Переносить збережені значення в *існуючий* конфіг.

    Саме в існуючий, а не поверненням нового: панелі інтерфейсу тримають
    посилання і на сам `cfg`, і на його вкладені `domain`/`mesh`/`flow`/
    `solver` — кожен сетер замкнений на конкретний обʼєкт ще при створенні
    віджета. Підміна конфігу новим обʼєктом лишила б половину панелей
    писати в покинутий: видимі значення й те, що піде в кейс, розʼїхалися б
    без жодної помилки.

    Повертає список відкинутих ключів у вигляді `mesh.n_layers`.
    """
    dropped: list[str] = []
    if isinstance(data, dict):
        _apply(cfg, data, dropped, "")
    return dropped


def cfg_from_dict(data: dict | None) -> fc.CaseCfg:
    """Новий конфіг зі збереженого. Для тих, хто не тримає посилань."""
    cfg = fc.CaseCfg()
    apply_dict(cfg, data)
    return cfg


def _apply(obj, data: dict, dropped: list[str], prefix: str) -> None:
    for f in fields(obj):
        if f.name not in data:
            continue
        value = data[f.name]
        current = getattr(obj, f.name)
        path = prefix + f.name

        if f.name == "boxes":
            setattr(obj, f.name, _boxes(value, dropped, path))
            continue
        if f.name == "bodies":
            setattr(obj, f.name, _bodies(value, dropped, path))
            continue
        if f.name == "refine_surfaces":
            setattr(obj, f.name, _refine_surfaces(value, dropped, path))
            continue
        if f.name == "porous":
            setattr(obj, f.name, _porous(value, dropped, path))
            continue
        if f.name == "rotating":
            setattr(obj, f.name, _rotating(value, dropped, path))
            continue
        if f.name == "engines":
            setattr(obj, f.name, _engines(value, dropped, path))
            continue
        if f.name == "disks":
            setattr(obj, f.name, _disks(value, dropped, path))
            continue
        if is_dataclass(current):
            if isinstance(value, dict):
                _apply(current, value, dropped, path + ".")
            else:
                dropped.append(path)
            continue

        ok, coerced = _coerce(f.name, current, value)
        if ok:
            setattr(obj, f.name, coerced)
        else:
            dropped.append(path)


def _refine_surfaces(value, dropped: list[str], path: str):
    """Поверхні згущення зі збереженого. Сходинки приїжджають списками
    списків, бо JSON кортежів не знає."""
    out = []
    if not isinstance(value, list):
        dropped.append(path)
        return out
    for i, item in enumerate(value):
        if not isinstance(item, dict) or "name" not in item:
            dropped.append(f"{path}[{i}]")
            continue
        levels = []
        for pair in item.get("levels") or []:
            try:
                levels.append((float(pair[0]), int(pair[1])))
            except (TypeError, ValueError, IndexError):
                dropped.append(f"{path}[{i}].levels")
        out.append(fc.RefineSurface(
            name=str(item["name"]),
            stl_file=str(item.get("stl_file") or ""),
            levels=levels))
    return out


def _boxes(value, dropped: list[str], path: str) -> list[fc.RefinementBox]:
    out = []
    for item in value or []:
        try:
            out.append(fc.RefinementBox(
                name=str(item["name"]),
                min=tuple(float(x) for x in item["min"]),
                max=tuple(float(x) for x in item["max"]),
                level=int(item["level"]),
            ))
        except (KeyError, TypeError, ValueError, AttributeError):
            dropped.append(path)
    return out


def _bodies(value, dropped: list[str], path: str) -> list[fc.BodyPatch]:
    """Роздільні патчі з маніфесту.

    Імена тут не просто рядки: OpenFOAM приймає за імʼя границі лише
    «слово», і правлений руками маніфест легко дає таке, на якому падає вже
    snappyHexMesh. Тому непридатне імʼя відкидається разом із патчем — краще
    повернутися до одного тіла, ніж зібрати кейс, що не порахується.
    """
    out = []
    for item in value or []:
        try:
            name = str(item["name"])
            stl = str(item["stl_file"])
        except (KeyError, TypeError, ValueError):
            dropped.append(path)
            continue
        if not name or name != fc.patch_name_from(name) or not stl:
            dropped.append(path)
            continue
        try:
            span = float(item.get("span", 0.0) or 0.0)
        except (TypeError, ValueError):
            span = 0.0
        try:
            area = float(item.get("area", 0.0) or 0.0)
        except (TypeError, ValueError):
            area = 0.0
        # ВЛАСНИЙ рівень згущення. Разом із площею він потрібен оцінці
        # сітки: без них вона порахує лопаті за спільним рівнем і покаже
        # мільйон там, де їх чотири. Кривий запис — не привід відкидати
        # патч цілком: без рівня деталь просто згущується як усі.
        lv = item.get("level")
        try:
            level = (tuple(int(v) for v in lv[:2])
                     if isinstance(lv, (list, tuple)) and len(lv) >= 2
                     else None)
        except (TypeError, ValueError):
            level = None
        # Роль відновлюємо ЛИШЕ для кришок двигуна. Решта ролей і досі
        # живе в самих деталях і перезбирається з них, а кришка без своєї
        # ролі стала б стінкою — і кейс, відкритий назад, мовчки втратив би
        # двигун.
        role = str(item.get("role", "body") or "body")
        # Диск теж: без своєї ролі він став би стінкою посеред потоку.
        role = (role if role in fc.PORT_ROLES or role == fc.DISK_ROLE
                else "body")
        engine = (str(item.get("engine", "") or "")
                  if role in fc.PORT_ROLES else "")
        out.append(fc.BodyPatch(name=name, stl_file=stl, span=span,
                                area=max(area, 0.0), level=level,
                                role=role, engine=engine))
    return out


def _disks(value, dropped: list[str], path: str) -> list[fc.ActuatorDisk]:
    """Гвинти-диски з маніфесту. Кривий запис — геть, а не «майже диск»."""
    out = []
    for i, item in enumerate(value or []):
        try:
            d = fc.ActuatorDisk(
                name=str(item["name"]),
                centre=tuple(float(x) for x in item["centre"]),
                axis=tuple(float(x) for x in item["axis"]),
                radius=float(item["radius"]),
                hub=float(item.get("hub", 0.0)),
                thickness=float(item.get("thickness", 0.0)),
                thrust=float(item.get("thrust", 0.0)),
                torque=float(item.get("torque", 0.0)),
                spin=1 if int(item.get("spin", 1)) >= 0 else -1,
                profile=str(item.get("profile", "realistic")),
                from_throttle=bool(item.get("from_throttle", True)),
                refine=bool(item.get("refine", True)),
                throttle=float(item.get("throttle", 1.0)),
            )
        except (KeyError, TypeError, ValueError, AttributeError):
            dropped.append(f"{path}[{i}]")
            continue
        nums = (d.radius, d.hub, d.thickness, d.thrust, d.torque,
                d.throttle) + d.centre + d.axis
        if (d.name != fc.patch_name_from(d.name) or len(d.centre) != 3
                or len(d.axis) != 3 or d.profile not in fc.DISK_PROFILE_KEYS
                or not all(math.isfinite(x) for x in nums)
                or min(d.radius, d.hub, d.thickness, d.thrust, d.torque) < 0):
            dropped.append(f"{path}[{i}]")
            continue
        out.append(d)
    return out


def _engines(value, dropped: list[str], path: str) -> list[fc.Engine]:
    """Двигуни з маніфесту.

    Імена кришок перевіряємо так само, як імена патчів: вони йдуть в
    OpenFOAM як імена границь. Відʼємна тяга чи площа — не «дуже слабкий
    двигун», а запис, з якого нічого не порахувати.
    """
    out = []
    for i, item in enumerate(value or []):
        try:
            e = fc.Engine(
                name=str(item["name"]),
                kind=str(item.get("kind", "edf")),
                # Маніфест без цього ключа — зроблений до Q31, коли тяга
                # означала «у польоті». Так його й читаємо.
                thrust_mode=str(item.get("thrust_mode", "flight")),
                thrust=float(item.get("thrust", 20.0)),
                v_jet=float(item.get("v_jet", 0.0)),
                t_jet=float(item.get("t_jet", 900.0)),
                fuel_frac=float(item.get("fuel_frac", 0.0)),
                direct=bool(item.get("direct", False)),
                mdot=float(item.get("mdot", 0.5)),
                v_exit=float(item.get("v_exit", 50.0)),
                from_throttle=bool(item.get("from_throttle", True)),
                refine=bool(item.get("refine", True)),
                nozzle=str(item.get("nozzle", "") or ""),
                intakes=tuple(str(x) for x in item.get("intakes", ())),
                nozzle_area=float(item.get("nozzle_area", 0.0)),
                nozzle_dh=float(item.get("nozzle_dh", 0.0)),
                intake_areas=tuple(float(x)
                                   for x in item.get("intake_areas", ())),
                throttle=float(item.get("throttle", 1.0)),
                nozzle_normal=tuple(float(x) for x in item.get(
                    "nozzle_normal", (1.0, 0.0, 0.0))),
                nozzle_proj=float(item.get("nozzle_proj", 0.0)),
            )
            if len(e.nozzle_normal) != 3:
                raise ValueError("nozzle_normal")
        except (KeyError, TypeError, ValueError, AttributeError):
            dropped.append(f"{path}[{i}]")
            continue
        nums = (e.thrust, e.v_jet, e.t_jet, e.fuel_frac, e.mdot, e.v_exit,
                e.nozzle_area, e.nozzle_dh, e.throttle) + e.intake_areas
        names = (e.name, *e.intakes) + ((e.nozzle,) if e.nozzle else ())
        if (e.kind not in fc.ENGINE_KIND_KEYS
                or e.thrust_mode not in fc.THRUST_MODE_KEYS
                or any(n != fc.patch_name_from(n) for n in names)
                or not all(math.isfinite(x) and x >= 0 for x in nums)):
            dropped.append(f"{path}[{i}]")
            continue
        out.append(e)
    return out


def _porous(value, dropped: list[str], path: str) -> list[fc.PorousZone]:
    """Пористі зони з маніфесту.

    Перевіряємо не лише типи: імʼя зони йде в OpenFOAM як імʼя cellZone, а
    недодатний перепад тиску чи нульовий розмір дали б зону, яка нічого не
    робить, — і мовчки, бо помилкою це ніде не вважається.
    """
    out = []
    for item in value or []:
        try:
            name = str(item["name"])
            centre = tuple(float(x) for x in item["centre"])
            size = tuple(float(x) for x in item["size"])
            axis = tuple(float(x) for x in item.get("axis", (1.0, 0.0, 0.0)))
            dp = float(item["dp"])
            u_ref = float(item["u_ref"])
            mode = str(item.get("mode", "inertial"))
            cross = float(item.get("cross_factor", 10.0))
        except (KeyError, TypeError, ValueError):
            dropped.append(path)
            continue
        if (name != fc.patch_name_from(name) or mode not in fc.POROUS_MODES
                or len(centre) != 3 or len(size) != 3 or len(axis) != 3
                or min(size) <= 0 or dp <= 0 or u_ref <= 0
                or not all(math.isfinite(x) for x in centre + size + axis)):
            dropped.append(path)
            continue
        out.append(fc.PorousZone(name=name, centre=centre, size=size,
                                 axis=axis, dp=dp, u_ref=u_ref, mode=mode,
                                 cross_factor=cross))
    return out


def _rotating(value, dropped: list[str], path: str) -> list[fc.RotatingZone]:
    """Обертові зони з маніфесту.

    Нульовий радіус чи довжина дали б зону без жодної комірки — тобто MRF,
    який мовчки нічого не робить. Краще відкинути її тут.
    """
    out = []
    for item in value or []:
        try:
            name = str(item["name"])
            centre = tuple(float(x) for x in item["centre"])
            axis = tuple(float(x) for x in item["axis"])
            radius = float(item["radius"])
            length = float(item["length"])
            rpm = float(item["rpm"])
            still = tuple(str(x) for x in item.get("non_rotating", ()))
        except (KeyError, TypeError, ValueError):
            dropped.append(path)
            continue
        if (name != fc.patch_name_from(name) or len(centre) != 3
                or len(axis) != 3 or radius <= 0 or length <= 0
                or not math.isfinite(rpm)
                or not all(math.isfinite(x) for x in centre + axis)
                or not any(abs(x) > 0 for x in axis)):
            dropped.append(path)
            continue
        out.append(fc.RotatingZone(name=name, centre=centre, axis=axis,
                                   radius=radius, length=length, rpm=rpm,
                                   non_rotating=still))
    return out


def _coerce(name: str, current, value):
    """(чи придатне, приведене значення) — за типом того, що вже в конфігу."""
    if name in ENUMS:
        return (isinstance(value, str) and value in ENUMS[name]), value
    if current is None:                       # inside_point
        if value is None:
            return True, None
        try:
            point = tuple(float(x) for x in value)
        except (TypeError, ValueError):
            return False, None
        return len(point) == 3, point
    if isinstance(current, bool):
        return isinstance(value, (bool, int)), bool(value)
    if isinstance(current, tuple):
        try:
            got = tuple(float(x) for x in value)
        except (TypeError, ValueError):
            return False, None
        return (len(got) == len(current)
                and all(math.isfinite(x) for x in got)), got
    if isinstance(current, int):
        try:
            got = float(value)
        except (TypeError, ValueError):
            return False, None
        return math.isfinite(got), int(round(got))
    if isinstance(current, float):
        try:
            got = float(value)
        except (TypeError, ValueError):
            return False, None
        return math.isfinite(got), got
    if isinstance(current, str):
        return isinstance(value, str), value
    if isinstance(current, list):
        # Список чисел (ваги процесів розподіленого прогону).
        try:
            got = [int(round(float(x))) for x in (value or [])]
        except (TypeError, ValueError):
            return False, None
        return all(x > 0 for x in got), got
    return False, None


# ==========================================================================
# Маніфест
# ==========================================================================

def manifest_path(case_dir: str | os.PathLike) -> Path:
    return Path(case_dir) / MANIFEST


def read_manifest(case_dir: str | os.PathLike) -> dict | None:
    """Маніфест або None, якщо його немає чи він неповносправний."""
    return _read_json(manifest_path(case_dir))


def _write_json(path: Path, data: dict) -> str:
    """Запис через тимчасовий файл: обрив не лишає каліки замість файлу."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=1),
                   encoding="utf-8")
    os.replace(tmp, path)
    return str(path)


def _read_json(path: Path) -> dict | None:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, UnicodeDecodeError):
        return None
    return data if isinstance(data, dict) else None


def _write(case_dir: str | os.PathLike, data: dict) -> str:
    return _write_json(manifest_path(case_dir), data)


def _base(case_dir: str | os.PathLike) -> dict:
    """Скелет маніфесту з уже збереженим уцілілим вмістом."""
    data = read_manifest(case_dir) or {}
    notes = data.get("notes")
    return {
        "format": FORMAT,
        "created": data.get("created") or _now(),
        "saved": _now(),
        "name": data.get("name", ""),
        "stl_source": data.get("stl_source", ""),
        "notes": notes if isinstance(notes, str) else "",
        "runs": [r for r in (data.get("runs") or []) if isinstance(r, dict)],
        "cfg": data.get("cfg"),
    }


def write_manifest(case_dir: str | os.PathLike, cfg: fc.CaseCfg,
                   stl_source: str = "") -> str:
    """Пише (або переписує) маніфест, зберігаючи примітки й історію прогонів."""
    data = _base(case_dir)
    data["name"] = cfg.name
    data["cfg"] = cfg_to_dict(cfg)
    if stl_source:
        data["stl_source"] = str(stl_source)
    return _write(case_dir, data)


def record_run(case_dir: str | os.PathLike, *, ok: bool, seconds: float = 0.0,
               iterations: int = 0, cells: int = 0, converged: bool = False,
               coeffs: dict | None = None, backend: str = "",
               note: str = "", layers: float = -1.0,
               end_time: float = 0.0) -> str:
    """Дописує результат прогону в історію кейсу.

    `iterations` — скільки кроків зробив розвʼязувач; у стаціонарі це й є
    ітерації. `end_time` має значення лише для нестаціонару: там «ітерацій»
    недосить, треба знати, скільки секунд пораховано.
    """
    data = _base(case_dir)
    entry = {
        "finished": _now(),
        "ok": bool(ok),
        "seconds": round(float(seconds), 1),
        "iterations": int(iterations),
        "end_time": round(float(end_time), 6),
        "cells": int(cells),
        "converged": bool(converged),
        "backend": backend,
        "note": note,
        # Частка граней, які отримали призматичні шари. Зі словників кейсу
        # її не дістати — це результат роботи snappy, а не налаштування.
        "layers": round(float(layers), 3),
        "coeffs": {k: [float("%.6g" % v[0]), float("%.3g" % v[1])]
                   for k, v in (coeffs or {}).items()},
    }
    data["runs"] = (data["runs"] + [entry])[-RUNS_KEPT:]
    return _write(case_dir, data)


def set_notes(case_dir: str | os.PathLike, text: str) -> str:
    data = _base(case_dir)
    data["notes"] = text
    return _write(case_dir, data)


# ==========================================================================
# Остання сесія і збереження налаштувань окремим файлом
# ==========================================================================

SESSION = "session.json"
SESSION_DIR = "session"
SESSION_STL = "body.stl"

# Понад стільки трикутників геометрію в сесію не кладемо: запис 150-мегабайтного
# STL на кожне закриття вікна — надто дорога послуга. Тоді в сесії лишається
# сам шлях до вихідного файлу, а розвороти й ремонт доведеться повторити.
MESH_CACHE_LIMIT = 1_500_000


def session_path(app_dir: str | os.PathLike) -> Path:
    return Path(app_dir) / SESSION


def session_stl(app_dir: str | os.PathLike) -> Path:
    """Куди кладемо геометрію останньої сесії — уже в метрах і вже
    розвернуту, тобто рівно те, що було у вʼюпорті."""
    return Path(app_dir) / SESSION_DIR / SESSION_STL


def read_session(app_dir: str | os.PathLike) -> dict | None:
    return _read_json(session_path(app_dir))


def session_project(app_dir: str | os.PathLike) -> Path:
    """Сцена останньої сесії ПО ДЕТАЛЯХ — у форматі проєкту.

    Злитий `body.stl` губив поділ на деталі: після перезапуску три моделі
    з ролями ставали однією без ролей. Цей файл тримає кожну окремо.
    """
    return Path(app_dir) / SESSION_DIR / f"last{PROJECT_EXT}"


def _rel(root: Path, path: str) -> str:
    if not path:
        return ""
    try:
        return str(Path(path).relative_to(root))
    except ValueError:
        return str(path)


def write_session(app_dir: str | os.PathLike, cfg: fc.CaseCfg,
                  stl_source: str = "", geometry: str = "",
                  case_dir: str = "", cases_dir: str = "",
                  ui: dict | None = None, project: str = "") -> str:
    """Запамʼятовує сесію: налаштування, геометрію і де що лежало.

    `geometry` — шлях до кешованого STL; у файлі він зберігається відносно
    каталогу програми, щоб її можна було перенести разом із сесією.
    `project` — сцена по деталях (`session_project`); старі версії програми
    цього ключа не знають і читають `geometry`, як читали.
    """
    root = Path(app_dir)
    rel = _rel(root, geometry)
    data = {
        "format": FORMAT,
        "saved": _now(),
        "cfg": cfg_to_dict(cfg),
        "stl_source": str(stl_source or ""),
        "geometry": rel,
        "case_dir": str(case_dir or ""),
        "cases_dir": str(cases_dir or ""),
        "ui": ui or {},
        "project": _rel(root, project),
    }
    return _write_json(session_path(root), data)


def _session_file(app_dir, data: dict, key: str) -> str:
    rel = data.get(key) or ""
    if not rel:
        return ""
    path = Path(rel)
    if not path.is_absolute():
        path = Path(app_dir) / path
    return str(path) if path.is_file() else ""


def session_geometry(app_dir: str | os.PathLike, data: dict) -> str:
    """Шлях до геометрії сесії, якщо файл ще на місці."""
    return _session_file(app_dir, data, "geometry")


def session_scene(app_dir: str | os.PathLike, data: dict) -> str:
    """Шлях до сцени сесії по деталях, якщо вона є (сесії з 2026-11-08)."""
    return _session_file(app_dir, data, "project")


def export_settings(path: str | os.PathLike, cfg: fc.CaseCfg) -> str:
    """Налаштування окремим файлом — щоб перенести на іншу машину."""
    return _write_json(Path(path), {
        "format": FORMAT,
        "saved": _now(),
        "name": cfg.name,
        "cfg": cfg_to_dict(cfg),
    })


def import_settings(path: str | os.PathLike) -> dict | None:
    """Читає налаштування з файлу й повертає словник конфігу.

    Приймає і власний файл налаштувань, і `studio.json` із будь-якого кейсу,
    і навіть голий конфіг без обгортки: усі три варіанти — те саме дерево,
    і відмовлятися від нього через відсутній заголовок було б капризом.
    """
    data = _read_json(Path(path))
    if data is None:
        return None
    inner = data.get("cfg")
    if isinstance(inner, dict):
        return inner
    if any(key in data for key in ("domain", "mesh", "flow", "solver")):
        return data
    return None


# ==========================================================================
# Розбір логів
# ==========================================================================

RE_CHECK_CELLS = re.compile(r"^\s*cells:\s+(\d+)", re.M)
RE_SNAPPY_CELLS = re.compile(r"cells:(\d+)")
RE_FAILED_CHECKS = re.compile(r"Failed (\d+) mesh checks")
RE_MESH_OK = re.compile(r"^Mesh OK", re.M)
# Час буває і цілим (номер ітерації стаціонару), і дробовим (секунди
# нестаціонару). Старий взірець брав лише цілі, тому нестаціонарний
# прогін виглядав би як «нуль ітерацій».
RE_ITER = re.compile(r"^Time = ([0-9]+(?:\.[0-9]*)?(?:[eE][+-]?[0-9]+)?)\s*$", re.M)

# Логи, які не є логами розвʼязувача, хоч і звуться *Foam.
SKIP_LOGS = {"potentialFoam"}
RE_EXEC = re.compile(r"ExecutionTime = ([0-9.eE+-]+) s")

TAIL = 256 * 1024      # логи солвера бувають на десятки мегабайт


def tail_text(path: str | os.PathLike, limit: int = TAIL) -> str:
    """Хвіст файлу. Усе, що нас цікавить, лежить у кінці логу, а читати його
    цілком означало б перемолоти десятки мегабайт на кожне оновлення
    вкладки."""
    try:
        size = os.path.getsize(path)
        with open(path, "rb") as fh:
            if size > limit:
                fh.seek(size - limit)
            raw = fh.read()
    except OSError:
        return ""
    return raw.decode("utf-8", errors="replace")


@dataclass
class LogFacts:
    cells: int = 0
    mesh_failed: int = -1          # -1 — checkMesh не проганяли
    iterations: int = 0
    seconds: float = 0.0
    finished: bool = False         # солвер дійшов до «End»
    # Нестаціонар лічить не ітерації, а час. Тримаємо обидва: `iterations`
    # лишається тим, чим було (номер останньої ітерації стаціонару), а для
    # нестаціонару є `end_time` у секундах і `steps` — скільки кроків зроблено.
    end_time: float = 0.0
    steps: int = 0


def parse_logs(case_dir: str | os.PathLike) -> LogFacts:
    """Що можна дізнатися про кейс із логів попереднього прогону."""
    logs = Path(case_dir) / "logs"
    out = LogFacts()
    if not logs.is_dir():
        return out

    check = tail_text(logs / "checkMesh.log")
    if check:
        cells = RE_CHECK_CELLS.findall(check)
        if cells:
            out.cells = int(cells[-1])
        m = RE_FAILED_CHECKS.search(check)
        if m:
            out.mesh_failed = int(m.group(1))
        elif RE_MESH_OK.search(check):
            out.mesh_failed = 0
    if not out.cells:
        # checkMesh могли не проганяти — тоді підсумок є в логу snappy.
        found = RE_SNAPPY_CELLS.findall(tail_text(logs / "snappyHexMesh.log"))
        if found:
            out.cells = int(found[-1])

    # Логи звуться так само, як кроки, а крок розвʼязувача зветься так само,
    # як сам розвʼязувач. Перелічувати їх поіменно не можна: із появою
    # rhoSimpleFoam список «simpleFoam.log» мовчки перестав щось знаходити,
    # і стисливі кейси показувалися в бібліотеці без жодної статистики.
    main = sorted(p for p in logs.glob("*Foam.log")
                  if p.stem not in SKIP_LOGS)
    staged = sorted(logs.glob("*Foam-стійкий.log"))

    # Двоетапний прогін пише два логи, а нумерація ітерацій у другому
    # продовжується з першого — тому по ітераціях беремо максимум, а не суму.
    # Час, навпаки, складаємо: це два послідовні розрахунки.
    for path in staged + main:
        text = tail_text(path)
        iters = RE_ITER.findall(text)
        if iters:
            out.steps += len(iters)
            last = float(iters[-1])
            out.end_time = max(out.end_time, last)
            # У стаціонарі Time — це номер ітерації, у нестаціонарі — секунди
            # з дробом. Називати другі «ітераціями» означало б показати в
            # бібліотеці «0 ітерацій» для прогону на 0.2 секунди.
            if last.is_integer():
                out.iterations = max(out.iterations, int(last))
        times = RE_EXEC.findall(text)
        if times:
            out.seconds += float(times[-1])
        if path in main:
            # Після «End» паралельний прогін додає «Finalising parallel run»,
            # тому дивимося не на останній рядок, а на кілька останніх.
            out.finished = "End" in text.strip().splitlines()[-5:]
    return out


# ==========================================================================
# Стан кейсу на диску
# ==========================================================================

def case_stl(case_dir: str | os.PathLike) -> str:
    """STL, з якого рахувався кейс. Він уже в метрах і вже розвернутий."""
    tri = Path(case_dir) / "constant" / "triSurface"
    if not tri.is_dir():
        return ""
    files = sorted(tri.glob("*.stl"), key=lambda p: p.stat().st_size,
                   reverse=True)
    return str(files[0]) if files else ""


def has_mesh(case_dir: str | os.PathLike) -> bool:
    root = Path(case_dir)
    if (root / "constant" / "polyMesh" / "owner").is_file():
        return True
    return (root / "processor0" / "constant" / "polyMesh" / "owner").is_file()


def is_case(path: str | os.PathLike) -> bool:
    root = Path(path)
    if not root.is_dir():
        return False
    return (manifest_path(root).is_file()
            or (root / "system" / "controlDict").is_file()
            or (root / "system" / "snappyHexMeshDict").is_file())


def dir_size(path: str | os.PathLike) -> int:
    """Розмір каталогу разом із вкладеними. Помилки доступу пропускаємо:
    підрахунок розміру не та причина, щоб не показати кейс у списку."""
    total = 0
    stack = [str(path)]
    while stack:
        try:
            with os.scandir(stack.pop()) as it:
                for entry in it:
                    try:
                        if entry.is_dir(follow_symlinks=False):
                            stack.append(entry.path)
                        else:
                            total += entry.stat(follow_symlinks=False).st_size
                    except OSError:
                        continue
        except OSError:
            continue
    return total


def reclaimable(case_dir: str | os.PathLike) -> list[tuple[str, int]]:
    """Що в кейсі можна видалити, не втративши результату: [(назва, байтів)].

    Це каталоги `processor*` — після `reconstructPar` їх вміст продубльовано
    у звичайних часових каталогах — і проміжні часові зрізи, окрім
    останнього. Останній зріз, `0.orig`, `constant`, `system`,
    `postProcessing`, логи й маніфест не чіпаємо ніколи.
    """
    root = Path(case_dir)
    out: list[tuple[str, int]] = []
    if not root.is_dir():
        return out
    for entry in sorted(root.glob("processor*")):
        if entry.is_dir():
            out.append((entry.name, dir_size(entry)))
    times = post.time_dirs(str(root))
    for t in times[:-1]:
        name = "%g" % t
        path = root / name
        if path.is_dir():
            out.append((name, dir_size(path)))
    return out


def reclaim(case_dir: str | os.PathLike, names: list[str]) -> int:
    """Видаляє названі каталоги кейсу. Повертає, скільки байтів звільнено.

    Приймає лише те, що назвав `reclaimable`: інакше достатньо було б
    порожнього рядка в списку, щоб знести кейс цілком.
    """
    allowed = {name for name, _ in reclaimable(case_dir)}
    freed = 0
    for name in names:
        if name not in allowed:
            continue
        path = Path(case_dir) / name
        size = dir_size(path)
        shutil.rmtree(path, ignore_errors=True)
        if not path.exists():
            freed += size
    return freed


def delete_case(case_dir: str | os.PathLike) -> bool:
    """Видаляє кейс цілком — лише якщо це справді кейс, а не чужий каталог."""
    if not is_case(case_dir):
        return False
    shutil.rmtree(case_dir, ignore_errors=True)
    return not Path(case_dir).exists()


# ==========================================================================
# Опис одного кейсу
# ==========================================================================

@dataclass
class CaseInfo:
    path: str
    name: str
    modified: float = 0.0
    size: int = 0
    state: str = STATE_SETTINGS
    cfg: dict | None = None
    notes: str = ""
    created: str = ""
    stl_source: str = ""
    stl: str = ""
    runs: list[dict] = field(default_factory=list)
    logs: LogFacts = field(default_factory=LogFacts)
    coeffs: dict[str, tuple[float, float]] = field(default_factory=dict)
    times: list[float] = field(default_factory=list)
    # Зрізи, поля в яких не відповідають сітці кейсу — тобто лишилися від
    # попередніх прогонів. Читати їх не можна, див. fs_post.matching_times.
    stale_times: list[float] = field(default_factory=list)

    # ---- зручні витяги з конфігу; None, якщо маніфесту немає ----

    def get(self, *path, default=None):
        node = self.cfg
        for key in path:
            if not isinstance(node, dict) or key not in node:
                return default
            node = node[key]
        return node

    @property
    def has_cfg(self) -> bool:
        return isinstance(self.cfg, dict) and bool(self.cfg)

    @property
    def u_inf(self):
        return self.get("flow", "u_inf")

    @property
    def aoa(self):
        return self.get("flow", "aoa_deg")

    @property
    def aos(self):
        return self.get("flow", "aos_deg")

    @property
    def turbulence(self):
        return self.get("flow", "turbulence")

    @property
    def cells(self) -> int:
        return self.logs.cells or int(self.last_run.get("cells", 0) or 0)

    @property
    def iterations(self) -> int:
        return self.logs.iterations or int(self.last_run.get("iterations", 0) or 0)

    @property
    def seconds(self) -> float:
        return self.logs.seconds or float(self.last_run.get("seconds", 0.0) or 0.0)

    @property
    def steady(self) -> bool:
        """Стаціонарний був прогін чи ні. Старі кейси — стаціонарні: поля
        `steady` в їхніх маніфестах немає, а нестаціонару тоді не існувало."""
        value = self.get("solver", "steady")
        return True if value is None else bool(value)

    @property
    def end_time(self) -> float:
        """Скільки секунд пораховано. Має сенс лише для нестаціонару."""
        return (self.logs.end_time
                or float(self.last_run.get("end_time", 0.0) or 0.0))

    @property
    def progress_label(self) -> tuple[str, str]:
        """(підпис, значення) — «ітерацій» чи «часу», залежно від постановки.

        У нестаціонарі номер кроку нічого не каже: важливо, скільки секунд
        пораховано з потрібних.
        """
        if self.steady:
            return ("ітерацій", str(self.iterations) if self.iterations else "—")
        if not self.end_time:
            return ("часу", "—")
        return ("часу", f"{self.end_time:g} с за {self.iterations} кроків"
                if self.iterations else f"{self.end_time:g} с")

    @property
    def last_run(self) -> dict:
        return self.runs[-1] if self.runs else {}

    @property
    def layers(self) -> float:
        """Частка граней із шарами в останньому прогоні, де вона відома."""
        for run in reversed(self.runs):
            value = run.get("layers")
            if isinstance(value, (int, float)) and value >= 0:
                return float(value)
        return -1.0

    @property
    def run_status(self) -> str:
        """Чим скінчився останній прогін — одним словом для таблиці."""
        run = self.last_run
        if run:
            if not run.get("ok"):
                return "обірвано"
            return "збіглося" if run.get("converged") else "пройдено"
        if self.logs.finished:
            return "пройдено"
        if self.logs.iterations:
            return "обірвано"
        return ""

    def coeff(self, name: str):
        """(середнє, розкид) або None.

        Живі дані з `postProcessing` мають пріоритет над записаними в
        маніфесті: кейс могли дорахувати вручну через `Allrun`, і тоді
        маніфест відстав.

        Далі йдемо історією прогонів назад, а не беремо лише останній:
        обірваний прогін коефіцієнтів не пише, але результат попереднього
        від того не зникає — а порожня клітинка сказала б, що його ніколи й
        не було. Чим саме скінчився останній прогін, показує окремий
        стовпець.
        """
        if name in self.coeffs:
            return self.coeffs[name]
        for run in reversed(self.runs):
            pair = (run.get("coeffs") or {}).get(name)
            if isinstance(pair, (list, tuple)) and len(pair) == 2:
                try:
                    return float(pair[0]), float(pair[1])
                except (TypeError, ValueError):
                    continue
        return None


def read_case(path: str | os.PathLike, with_size: bool = True,
              with_coeffs: bool = True) -> CaseInfo:
    root = Path(path)
    data = read_manifest(root) or {}
    info = CaseInfo(path=str(root), name=root.name)
    info.cfg = data.get("cfg") if isinstance(data.get("cfg"), dict) else None
    notes = data.get("notes")
    info.notes = notes if isinstance(notes, str) else ""
    info.created = str(data.get("created", ""))
    info.stl_source = str(data.get("stl_source", ""))
    info.runs = [r for r in (data.get("runs") or []) if isinstance(r, dict)]
    info.stl = case_stl(root)
    info.logs = parse_logs(root)
    info.times, info.stale_times = post.matching_times(str(root))

    if info.times:
        info.state = STATE_RESULTS
    elif has_mesh(root):
        info.state = STATE_MESH
    else:
        info.state = STATE_SETTINGS

    stamps = [0.0]
    for probe in (root, manifest_path(root), root / "logs"):
        try:
            stamps.append(os.path.getmtime(probe))
        except OSError:
            pass
    info.modified = max(stamps)
    if with_size:
        info.size = dir_size(root)
    if with_coeffs:
        try:
            info.coeffs = post.summarise_coeffs(be.read_force_coeffs(str(root)))
        except (OSError, ValueError):
            info.coeffs = {}
    return info


def scan(root: str | os.PathLike, with_size: bool = True) -> list[CaseInfo]:
    """Усі кейси в каталозі, найсвіжіші першими."""
    out: list[CaseInfo] = []
    try:
        entries = sorted(os.scandir(root), key=lambda e: e.name.lower())
    except OSError:
        return out
    for entry in entries:
        try:
            if not entry.is_dir() or not is_case(entry.path):
                continue
        except OSError:
            continue
        out.append(read_case(entry.path, with_size=with_size))
    out.sort(key=lambda c: c.modified, reverse=True)
    return out


# ==========================================================================
# Стійкість по тангажу: нейтральна точка з кількох кейсів
# ==========================================================================
#
# Навіщо. Центр ваги ставлять не за центром тиску (він живе на одному куті й
# тікає в нескінченність, коли нормальна сила мала), а за НЕЙТРАЛЬНОЮ точкою:
# вона одна на літак і каже, де момент перестає залежати від кута. Знайти її
# можна лише з кількох прогонів на різних кутах — тому тут вони збираються з
# виділених у «Збережених» кейсів, а математика лежить у fs_backend.
#
# Перевірено на справжній серії cases/_polar (0°, 5°, 10°): нейтральна точка
# x = −0.132 м, момент відносно неї на трьох кутах 0.042 / 0.048 / 0.042, а
# відносно центру моментів він росте від 0.173 до 0.357; за парами кутів точка
# ходить у межах ±0.008 м (±4% Lref).

# Запас стійкості, який зазвичай беруть: центр ваги перед нейтральною точкою на
# 5–15% хорди. Менше — ледь стійкий, більше — важкий на носі.
MARGIN_LOW = 0.05
MARGIN_HIGH = 0.15


@dataclass
class Stability:
    """Що можна сказати про нейтральну точку за виділеними кейсами."""
    point: be.NeutralPoint | None = None
    problem: str = ""                   # чому порахувати не вдалося
    notes: list[str] = field(default_factory=list)   # на що зважити в числі
    names: list[str] = field(default_factory=list)   # кейси, що пішли в розрахунок
    l_ref: float = 0.0
    x_cofr: float = 0.0
    nose_x: float | None = None         # x найпередньої точки тіла
    length: float | None = None         # довжина тіла вздовж X

    def margin(self, x_cg: float) -> float | None:
        """Запас стійкості в частках Lref. Додатний — центр ваги ПЕРЕД
        нейтральною точкою (менший x, бо потік іде вздовж +X)."""
        if self.point is None or self.l_ref <= 0:
            return None
        return (self.point.x - x_cg) / self.l_ref

    def cg_range(self) -> tuple[float, float] | None:
        """(найпередніший x, найзадніший x) центру ваги із запасом 5–15%."""
        if self.point is None or self.l_ref <= 0:
            return None
        return (self.point.x - MARGIN_HIGH * self.l_ref,
                self.point.x - MARGIN_LOW * self.l_ref)


def stability(cases: list[CaseInfo]) -> Stability:
    """Нейтральна точка за виділеними кейсами одного літака на різних кутах."""
    out = Stability()
    usable = []                     # (info, cfg, (Cd, Cl, Cm), розкиди)
    for info in cases:
        if not info.has_cfg:
            out.notes.append(f"«{info.name}»: маніфесту немає, налаштування "
                             f"невідомі — кейс пропущено")
            continue
        cfg = cfg_from_dict(info.cfg)
        got = [info.coeff(n) for n in ("Cd", "Cl", "CmPitch")]
        if any(p is None for p in got):
            out.notes.append(f"«{info.name}»: коефіцієнтів немає — кейс пропущено")
            continue
        means = [p[0] for p in got]
        if not all(math.isfinite(v) and abs(v) < 1e3 for v in means):
            out.notes.append(f"«{info.name}»: розрахунок розійшовся — кейс пропущено")
            continue
        if abs(cfg.flow.aos_deg) > 1e-6:
            out.notes.append(f"«{info.name}»: кут ковзання {cfg.flow.aos_deg:g}° — "
                             f"поздовжню стійкість так не знайти, кейс пропущено")
            continue
        usable.append((info, cfg, means, [p[1] for p in got]))
    if len(usable) < 2:
        out.problem = ("потрібно щонайменше два кейси ЦЬОГО Ж літака на різних "
                       "кутах атаки, з коефіцієнтами")
        return out

    ref = usable[0][1].flow
    for info, cfg, _, _ in usable[1:]:
        f = cfg.flow
        same = (math.isclose(f.l_ref, ref.l_ref, rel_tol=1e-6)
                and math.isclose(f.a_ref, ref.a_ref, rel_tol=1e-6)
                and all(math.isclose(a, b, rel_tol=1e-6, abs_tol=1e-9)
                        for a, b in zip(f.cofr, ref.cofr)))
        if not same:
            out.problem = (
                f"у «{info.name}» інші Lref, Aref чи центр моментів, ніж у "
                f"«{usable[0][0].name}»: коефіцієнти нормовані по-різному, "
                f"порівнювати їх не можна")
            return out
    out.l_ref, out.x_cofr = ref.l_ref, ref.cofr[0]
    out.names = [u[0].name for u in usable]

    angles = [u[1].flow.aoa_deg for u in usable]
    if max(angles) - min(angles) < be.MIN_ANGLE_SPAN:
        out.problem = (f"кути атаки однакові або різняться менш ніж на "
                       f"{be.MIN_ANGLE_SPAN:g}°: за одним кутом похідної нема")
        return out

    # Габарит тіла: щоб показати точку «від носа», а не лише в системі кейсу.
    boxes = [(u[1].bbox_min[0], u[1].bbox_max[0]) for u in usable]
    if all(math.isclose(b[0], boxes[0][0], abs_tol=1e-4)
           and math.isclose(b[1], boxes[0][1], abs_tol=1e-4) for b in boxes):
        out.nose_x = boxes[0][0]
        out.length = boxes[0][1] - boxes[0][0]
    else:
        out.notes.append("габарити тіла в кейсах різні — «від носа» не показую")

    out.point = be.neutral_point(
        [(u[1].flow.aoa_deg, *u[2]) for u in usable], out.l_ref, out.x_cofr)
    if out.point is None:
        out.problem = ("нормальна сила майже не росте з кутом (dCn/dα менше "
                       f"{be.MIN_CN_ALPHA:g} на градус): нейтральна точка тікає "
                       "в нескінченність")
        return out

    # На що зважити в числі.
    p = out.point
    if len(p.angles) == 2:
        out.notes.append("лише два кути: лінійність не перевірена, бажано "
                         "третій між ними чи поряд")
    elif p.pairs and max(p.pairs) - min(p.pairs) > 0.10 * out.l_ref:
        out.notes.append(
            f"за різними парами кутів точка ходить від {min(p.pairs):.3f} до "
            f"{max(p.pairs):.3f} м: залежність від кута нелінійна (зрив?), "
            f"беріть кути ближче до робочого")
    speeds = [u[1].flow.u_inf for u in usable]
    if max(speeds) > 1.01 * min(speeds):
        out.notes.append("швидкість у кейсів різна — числа Рейнольдса не "
                         "збігаються")
    if len({u[1].flow.turbulence for u in usable}) > 1:
        out.notes.append("моделі турбулентності різні")
    if len({u[0].steady for u in usable}) > 1:
        out.notes.append("серед кейсів є і стаціонарні, і нестаціонарні "
                         "прогони: беріть однакові")
    sources = {os.path.basename(u[0].stl_source) for u in usable if u[0].stl_source}
    if len(sources) > 1:
        out.notes.append("кейси зроблено з різних файлів геометрії: "
                         + ", ".join(sorted(sources)))
    shaky = [u[0].name for u in usable
             if any(sp > 0.05 * max(abs(m), 0.01)
                    for sp, m in zip(u[3][1:], u[2][1:]))]
    if shaky:
        out.notes.append("коефіцієнти ще коливаються в кейсах: "
                         + ", ".join(shaky) + " — точка наближена")
    if out.nose_x is not None and out.length:
        lo, hi = out.nose_x - 0.25 * out.length, out.nose_x + 1.25 * out.length
        if not lo <= p.x <= hi:
            out.notes.append("нейтральна точка далеко за межами тіла: нормальна "
                             "сила майже не росте з кутом, число ненадійне")
    return out


# --------------------------------------------------------------------------
# Серія кутів: прогони один за одним, а наприкінці — нейтральна точка
# --------------------------------------------------------------------------
#
# Вище точка рахується з кейсів, які людина вже має. Але щоб їх мати, треба
# було тричі змінити кут, тричі перейменувати кейс і тричі натиснути
# «Запустити», щоразу дочекавшись попереднього. Серія робить це сама; тут
# лише те, що не потребує вікна: які кути, як назвати кейси і чи годиться
# постановка для серії взагалі.

DEFAULT_SERIES = "0, 3, 6"
# Вище за це стаціонарний розвʼязувач на відриві не сходиться (розкид Cl
# 70% на filled_holes1), а нейтральна точка — це похідна в ЛІНІЙНІЙ ділянці.
SERIES_STALL_DEG = 12.0


def parse_angles(text: str) -> tuple[list[float], str]:
    """Кути атаки з рядка: (кути за зростанням, чому не так — або "").

    Роздільник — кома, пробіл або крапка з комою. Якщо в рядку є крапка з
    комою, кома всередині числа читається як десяткова: «0; 2,5; 5» —
    це три кути, а не чотири.
    """
    raw = (text or "").strip()
    if not raw:
        return [], "кути не задано"
    if ";" in raw:
        tokens = [t.strip().replace(",", ".") for t in raw.split(";")]
    else:
        tokens = raw.replace(",", " ").split()
    angles = []
    for t in tokens:
        if not t:
            continue
        t = t.replace("°", "").replace("−", "-")
        try:
            a = float(t)
        except ValueError:
            return [], f"«{t}» — не число"
        if not math.isfinite(a) or abs(a) > 90.0:
            return [], f"кут {t}° поза межами −90…90°"
        angles.append(a)
    unique = sorted({round(a, 6) for a in angles})
    if len(unique) < 2:
        return unique, "потрібно щонайменше два різні кути"
    if unique[-1] - unique[0] < be.MIN_ANGLE_SPAN:
        return unique, (f"кути різняться менш ніж на {be.MIN_ANGLE_SPAN:g}°: "
                        f"похідної з них не взяти")
    return unique, ""


def runs_word(n: int) -> str:
    """«2 прогони», «5 прогонів», «21 прогін» — українська форма числівника."""
    if n % 10 == 1 and n % 100 != 11:
        return f"{n} прогін"
    if 2 <= n % 10 <= 4 and not 12 <= n % 100 <= 14:
        return f"{n} прогони"
    return f"{n} прогонів"


def series_case_name(base: str, angle: float) -> str:
    """Імʼя кейсу для одного кута серії: «plane_aoa5», «plane_aoa-2.5».

    Кут у назві — не прикраса: у «Збережених» серію видно одразу, і кейси
    різних кутів не перезаписують один одного.
    """
    base = (base or "case").strip() or "case"
    return f"{base}_aoa{angle:g}"


def series_problem(cfg) -> str:
    """Чому серію на цій постановці запускати не можна — або "".

    Лише те, що робить результат БЕЗГЛУЗДИМ, а не просто гіршим: кут
    ковзання (поздовжню стійкість так не знайти, і `stability` такі кейси
    пропускає) і режим «аеротруба», де slip-стінки зрізають кут атаки.
    """
    f = cfg.flow
    if abs(f.aos_deg) > 1e-6:
        return (f"стоїть кут ковзання {f.aos_deg:g}°: нейтральна точка по "
                f"тангажу шукається при нульовому ковзанні, і такі кейси в "
                f"розрахунок не підуть. Поставте ковзання 0 на вкладці «Потік».")
    if f.farfield_mode == "tunnel":
        return ("зовнішні межі в режимі «аеротруба»: slip-стінки зрізають кут "
                "атаки, і всі прогони серії порахували б той самий потік. "
                "Перемкніть «Дальнє поле» на freestream на вкладці «Потік».")
    return ""


def series_notes(angles: list[float]) -> list[str]:
    """На що зважити в самому наборі кутів — до запуску, а не після."""
    notes = []
    if len(angles) == 2:
        notes.append("лише два кути: лінійність не перевірити, краще три")
    if max(abs(a) for a in angles) > SERIES_STALL_DEG:
        notes.append(f"кути понад {SERIES_STALL_DEG:g}° близькі до зриву: там "
                     f"стаціонарний розвʼязувач не сходиться, а момент "
                     f"нелінійний — точка поїде")
    return notes


# ==========================================================================
# Форматування для таблиці
# ==========================================================================

def fmt_size(n: int) -> str:
    if n <= 0:
        return "—"
    for unit, div in (("ГБ", 1 << 30), ("МБ", 1 << 20), ("КБ", 1 << 10)):
        if n >= div:
            return f"{n / div:.1f} {unit}"
    return f"{n} Б"


def fmt_time(stamp: float) -> str:
    if not stamp:
        return "—"
    return time.strftime("%Y-%m-%d %H:%M", time.localtime(stamp))


def fmt_seconds(s: float) -> str:
    if not s:
        return "—"
    if s < 90:
        return f"{s:.0f} с"
    if s < 3600:
        return f"{s / 60:.1f} хв"
    return f"{s / 3600:.1f} год"


# ==========================================================================
# ПРОЄКТ: УСЕ В ОДНОМУ ФАЙЛІ
# ==========================================================================
#
# Сесія і файл налаштувань кожен рятують СВОЮ половину, і обидві —
# неповні. Сесія кладе зліплену геометрію одним STL, без поділу на
# деталі: ролі, оберти, осі й завіси в ній не переживають нічого.
# Файл налаштувань, навпаки, тримає числа, але не тримає геометрії.
#
# Проєкт — це коробка, в якій лежить УСЕ й одразу:
#
#     project.json     налаштування, перелік деталей і що кожна з них є
#     parts/NNN_*.stl  геометрія кожної деталі ОКРЕМО, вже в метрах,
#                      уже повернута — тобто рівно те, що у вʼюпорті
#
# Чому окремо, а не одним зліпком: зліпок не розібрати назад. Роль
# «гвинт» належить деталі, а не сцені, і зберегти її, не зберігши межі
# між деталями, неможливо.
#
# Чому ZIP: це один файл, який можна переслати, і водночас його вміст
# видно будь-яким архіватором — коли щось піде не так, людина зможе
# дістати свої STL без нашої програми.

PROJECT_EXT = ".foamproj"
PROJECT_MANIFEST = "project.json"
PROJECT_PARTS = "parts"
PROJECT_VERSION = 1
# Сітка проєкту: зібрана, з рівнями згущення (як у сховищі сіток бекенда).
# Лежить під mesh/polyMesh/…, а ключ (fs_backend.mesh_key) — у маніфесті:
# без ключа не сказати, чи ця сітка ще пасує до геометрії й налаштувань.
PROJECT_MESH = "mesh"


def _safe_stem(name: str, fallback: str) -> str:
    """Імʼя для файлу всередині архіву: без шляхів і без несподіванок."""
    stem = os.path.splitext(os.path.basename(str(name or "")))[0]
    stem = re.sub(r"[^0-9A-Za-zА-Яа-яЇїІіЄєҐґ._-]+", "_", stem).strip("._")
    return stem or fallback


def project_mesh_key(path: str | os.PathLike) -> str:
    """Ключ сітки, що лежить у проєкті, або "" (сітки немає)."""
    try:
        with zipfile.ZipFile(path, "r") as z:
            m = json.loads(z.read(PROJECT_MANIFEST).decode("utf-8"))
            key = str((m.get("mesh") or {}).get("key") or "")
            if key and f"{PROJECT_MESH}/polyMesh/faces" not in z.namelist() \
                    and f"{PROJECT_MESH}/polyMesh/faces.gz" not in z.namelist():
                return ""
            return key
    except (OSError, KeyError, ValueError, zipfile.BadZipFile):
        return ""


def extract_project_mesh(path: str | os.PathLike, store: str | os.PathLike) -> str:
    """Сітку з проєкту — у `store/<ключ>/polyMesh`. Повертає ключ або "".

    Уже розпакована (та сама тека є) — не чіпаємо: сітка під ключем одна.
    """
    key = project_mesh_key(path)
    if not key or not re.fullmatch(r"[0-9a-f]{8,64}", key):
        return ""
    dst = Path(store) / key
    if be._mesh_complete(dst):
        return key
    part = Path(store) / f".prj-{key}"
    shutil.rmtree(part, ignore_errors=True)
    try:
        with zipfile.ZipFile(path, "r") as z:
            for info in z.infolist():
                name = info.filename
                if not name.startswith(PROJECT_MESH + "/polyMesh/") or info.is_dir():
                    continue
                rel = Path(name).relative_to(PROJECT_MESH)
                if ".." in rel.parts:
                    continue
                out = part / rel
                out.parent.mkdir(parents=True, exist_ok=True)
                with z.open(info) as src, open(out, "wb") as fh:
                    shutil.copyfileobj(src, fh, 1 << 20)
        if not be._mesh_complete(part):
            return ""
        shutil.rmtree(dst, ignore_errors=True)
        os.replace(part, dst)
        return key
    except (OSError, zipfile.BadZipFile):
        return ""
    finally:
        shutil.rmtree(part, ignore_errors=True)


def write_project(path: str | os.PathLike, cfg: fc.CaseCfg, parts,
                  extra: dict | None = None, mesh_key: str = "",
                  mesh_dir: str | os.PathLike | None = None) -> str:
    """Записати проєкт. `parts` — список словників із ключем `stl` (bytes).

    Решта ключів словника йде в маніфест як є: імʼя, роль, оберти, вісь,
    завіса, галочка. Що саме там лежить, вирішує той, хто викликає —
    бібліотека не має знати про будову `Part` у вікні.

    Сітка: `mesh_dir` (тека з polyMesh/) кладеться у проєкт під ключем
    `mesh_key`. Немає теки, але старий файл на тому самому місці вже має
    сітку З ТИМ САМИМ ключем — її переносимо: інакше автозбереження перед
    прогоном (коли нової сітки ще немає) мовчки викидало б сітку з проєкту.
    Ключ інший — стара сітка вже не пасує, і її не переносимо.
    """
    target = Path(path)
    if target.suffix.lower() != PROJECT_EXT:
        target = target.with_suffix(PROJECT_EXT)
    target.parent.mkdir(parents=True, exist_ok=True)
    mesh_src = Path(mesh_dir) / "polyMesh" if mesh_dir else None
    if mesh_src is not None and not mesh_src.is_dir():
        mesh_src = None
    # Ключ невідомий (кейс ще не створено) — переносимо як є: сітка з
    # чужим ключем у проєкті нешкідлива, бо перед прогоном її беруть лише
    # тоді, коли ключ збігся.
    old_key = project_mesh_key(target) if (mesh_src is None and target.is_file()) else ""
    carry = bool(old_key) and (not mesh_key or old_key == mesh_key)
    if carry:
        mesh_key = old_key

    meta = []
    blobs = []
    for i, part in enumerate(parts):
        data = part.get("stl")
        row = {k: v for k, v in part.items() if k != "stl"}
        if data:
            fname = f"{PROJECT_PARTS}/{i:03d}_{_safe_stem(part.get('name'), f'part{i}')}.stl"
            row["file"] = fname
            blobs.append((fname, data))
        meta.append(row)

    manifest = {
        "kind": "openfoam-studio-project",
        "version": PROJECT_VERSION,
        "saved": _now(),
        "cfg": cfg_to_dict(cfg),
        "parts": meta,
        "extra": dict(extra or {}),
    }
    if mesh_key and (mesh_src is not None or carry):
        manifest["mesh"] = {"key": mesh_key}
    # Пишемо ЧЕРЕЗ ТИМЧАСОВИЙ ФАЙЛ і лише тоді підміняємо. Проєкт
    # зберігають поверх старого, і обрив на середині запису коштував би
    # і нового файлу, і старого.
    tmp = target.with_suffix(target.suffix + ".part")
    with zipfile.ZipFile(tmp, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr(PROJECT_MANIFEST,
                   json.dumps(manifest, ensure_ascii=False, indent=1))
        for fname, data in blobs:
            # STL уже стиснений погано, але геть без стиснення архів
            # виходить утричі більший — лишаємо DEFLATE.
            z.writestr(fname, data)
        if mesh_src is not None:
            for f in sorted(mesh_src.rglob("*")):
                if f.is_file():
                    z.write(f, f"{PROJECT_MESH}/polyMesh/"
                               + f.relative_to(mesh_src).as_posix())
        elif carry:
            # Потоком, а не в памʼять: сітка — сотні мегабайт.
            with zipfile.ZipFile(target, "r") as old:
                for info in old.infolist():
                    if info.filename.startswith(PROJECT_MESH + "/"):
                        with old.open(info) as src, \
                                z.open(info.filename, "w", force_zip64=True) as dst:
                            shutil.copyfileobj(src, dst, 1 << 20)
    os.replace(tmp, target)
    return str(target)


def read_project(path: str | os.PathLike) -> dict | None:
    """Прочитати проєкт: {'cfg', 'parts', 'extra'} або None.

    Кожна деталь у 'parts' дістає ключ `stl` із байтами свого STL —
    або None, якщо файл у архіві відсутній. Мовчки пропускати таку
    деталь не можна: зникла геометрія це не дрібниця, і той, хто
    викликає, має сказати про це людині.
    """
    src = Path(path)
    if not src.exists():
        return None
    try:
        with zipfile.ZipFile(src, "r") as z:
            names = set(z.namelist())
            if PROJECT_MANIFEST not in names:
                return None
            manifest = json.loads(z.read(PROJECT_MANIFEST).decode("utf-8"))
            if manifest.get("kind") != "openfoam-studio-project":
                return None
            parts = []
            for row in manifest.get("parts") or []:
                item = dict(row)
                fname = item.pop("file", "")
                item["stl"] = z.read(fname) if fname in names else None
                parts.append(item)
    except (zipfile.BadZipFile, KeyError, ValueError, OSError):
        return None
    return {
        "cfg": cfg_from_dict(manifest.get("cfg")),
        "parts": parts,
        "extra": manifest.get("extra") or {},
        "version": manifest.get("version"),
        "saved": manifest.get("saved", ""),
    }
