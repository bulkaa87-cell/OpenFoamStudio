"""Кейс з диска → те, що потрібно кадрам: обʼєм, тіла, потік, гвинти.

Кадри роблять і з кейсів, збережених цією програмою (є studio.json з усіма
налаштуваннями), і з тих, що рахувалися скриптом чи вручну. У других нічого
не загублено: усе потрібне лежить у самому кейсі — швидкість і тіла в
controlDict (блок forceCoeffs), гвинти в MRFProperties, в'язкість у
transportProperties. Звідти воно й береться, а що не знайшлося — те просять
у командному рядку.

Модуль не залежить від Qt: ним користується і `tools/render_views.py`, і
тести.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np

import fs_library as lib
import fs_post as post
import fs_views as fv
from fs_i18n import tr

# Границі, які тілом не є: їх у кадр не кладемо, навіть якщо про тіла нічого
# не відомо. Назви — ті, що дає сама програма й blockMesh за замовчуванням.
NOT_BODIES = {"inlet", "outlet", "farfield", "top", "bottom", "sides", "side",
              "ground", "front", "back", "left", "right", "symmetry",
              "frontAndBack", "internalMesh"}

RE_NUM = r"([-+]?(?:\d+\.?\d*|\.\d+)(?:[eE][-+]?\d+)?)"


def _read(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""


def _strip_comments(text: str) -> str:
    text = re.sub(r"/\*.*?\*/", "", text, flags=re.S)
    return re.sub(r"//[^\n]*", "", text)


def _vector(text: str, key: str):
    m = re.search(key + r"\s*\(\s*" + RE_NUM + r"\s+" + RE_NUM + r"\s+"
                  + RE_NUM + r"\s*\)", text)
    return tuple(float(x) for x in m.groups()) if m else None


def control_facts(case_dir: str | Path) -> dict:
    """Що можна знати про потік і тіла з system/controlDict.

    Ключі (лише ті, що знайшлися): u_inf, rho, direction, a_ref, patches. Тіла —
    найдовший список `patches (...)` серед блоків forceCoeffs: перший блок
    зазвичай рахує сили на всьому тілі, а наступні — по частинах.
    """
    text = _strip_comments(_read(Path(case_dir) / "system" / "controlDict"))
    out: dict = {}
    m = re.search(r"magUInf\s+" + RE_NUM, text)
    if m:
        out["u_inf"] = float(m.group(1))
    m = re.search(r"rhoInf\s+" + RE_NUM, text)
    if m:
        out["rho"] = float(m.group(1))
    m = re.search(r"\bAref\s+" + RE_NUM, text)
    if m:
        out["a_ref"] = float(m.group(1))
    d = _vector(text, "dragDir")
    if d and np.linalg.norm(d) > 0:
        out["direction"] = tuple(np.asarray(d) / np.linalg.norm(d))
    m = re.search(r"application\s+(\w+)", text)
    if m:
        out["application"] = m.group(1)
    lists = []
    for block in re.finditer(r"type\s+forceCoeffs\s*;(.*?)\n\s*\}", text,
                             flags=re.S):
        for m in re.finditer(r"patches\s*\(([^)]*)\)", block.group(1)):
            # Імена можуть стояти в лапках: "plane". Лапки — не частина імені.
            lists.append([w.strip("\"'") for w in m.group(1).split()])
    if lists:
        out["patches"] = max(lists, key=len)
    return out


def kinematic_viscosity(case_dir: str | Path) -> float | None:
    text = _strip_comments(_read(Path(case_dir) / "constant"
                                 / "transportProperties"))
    m = re.search(r"\bnu\s+(?:\[[^\]]*\]\s+)?" + RE_NUM, text)
    return float(m.group(1)) if m else None


def mrf_rotors(case_dir: str | Path, bodies: dict) -> list:
    """Гвинти з constant/MRFProperties: місце й вісь — звідти, радіус — з
    самого гвинта (найдальша точка його поверхні від осі), бо в цьому
    словнику радіуса немає. Гвинт — це патч, що називається так само, як
    обертова зона."""
    text = _strip_comments(_read(Path(case_dir) / "constant" / "MRFProperties"))
    out = []
    for m in re.finditer(r"(\w+)\s*\{([^{}]*)\}", text):
        body = m.group(2)
        zone = re.search(r"cellZone\s+(\w+)", body)
        origin, axis = _vector(body, "origin"), _vector(body, "axis")
        if not (zone and origin and axis):
            continue
        name = zone.group(1)
        a = fv._unit(axis)
        radius = 0.1
        mesh = bodies.get(name)
        if mesh is not None and mesh.n_points:
            rel = np.asarray(mesh.points) - np.asarray(origin)
            radial = rel - (rel @ a)[:, None] * a
            radius = float(np.linalg.norm(radial, axis=1).max())
        out.append(fv.Rotor(name, tuple(origin), tuple(a), radius, 0.0))
    return out


@dataclass
class LoadedCase:
    view: fv.CaseView
    rotors: list = field(default_factory=list)
    name: str = ""
    time: float = 0.0
    source: str = ""            # звідки взяті налаштування потоку
    notes: list = field(default_factory=list)   # що довелося припустити


def load_case(case_dir: str | Path, *, time: float | None = None,
              label: str = "", u_inf: float | None = None) -> LoadedCase:
    """Читає кейс для кадрів. `u_inf` — явна швидкість, якщо кейс її не каже."""
    case_dir = Path(case_dir)
    name = case_dir.name
    res = post.Results(str(case_dir), name)
    # Лише ті моменти, що відповідають сітці, — як і на вкладці «Результати».
    # Кейс, що накопичив зрізи від інших сіток, інакше дав би пустий обʼєм
    # під виглядом нормального (VTK лише пише «cells do not match» у stderr).
    good, stale = post.matching_times(str(case_dir))
    times = good or list(res.times)
    if not times:
        raise ValueError(tr("Case {name} has no time directories with results.", name=name))
    t = float(time) if time is not None else float(times[-1])
    if t not in times:
        why = (tr(" (time directories {join} are from another mesh)", join=', '.join('%g' % x for x in stale))
               if stale else "")
        raise ValueError(tr("Time {t:g} is not in case {name}; available: {join}{why}.", t=t, name=name, join=", ".join(f"{x:g}" for x in times[-5:]), why=why))
    volume = res.internal(t)
    if volume is None:
        raise ValueError(tr("Cannot read the mesh volume in case {name}.", name=name))

    notes: list = []
    if stale:
        notes.append(tr("time directories {join} are from another mesh — skipped", join=", ".join("%g" % x for x in stale)))
    manifest = lib.read_manifest(case_dir)
    cfg = lib.cfg_from_dict(manifest.get("cfg")) if manifest else None
    facts = control_facts(case_dir)
    if cfg is not None and manifest and manifest.get("cfg"):
        fs = fv.Freestream.from_flow(cfg.flow)
        wanted = [b.name for b in cfg.body_patches]
        source = "studio.json"
    else:
        speed = u_inf if u_inf is not None else facts.get("u_inf")
        if speed is None:
            raise ValueError(
                tr("Case {name} does not give the flow speed (no "
                   "studio.json, no magUInf in controlDict). Set it: "
                   "--u-inf.", name=name))
        nu = kinematic_viscosity(case_dir) or 1.47e-5
        # Густинні розвʼязувачі (rhoSimpleFoam, rhoPimpleFoam, rhoCentralFoam)
        # тримають АБСОЛЮТНИЙ тиск у паскалях, а не кінематичний. Без цього
        # p·ρ дає 1.2e5 Па замість 30, і шкала виходить у 1e5 разів не та.
        # Відлік тиску в словниках не записано — беремо стандартний.
        comp = str(facts.get("application", "")).startswith("rho")
        if comp:
            notes.append(tr("compressible solver: p∞ = 101325 Pa (assumed)"))
        fs = fv.Freestream(u_inf=float(speed),
                           direction=facts.get("direction", (1.0, 0.0, 0.0)),
                           rho=facts.get("rho", 1.225), nu=nu,
                           p_inf=101325.0 if comp else 0.0, compressible=comp)
        wanted = facts.get("patches") or []
        source = "controlDict"
    if u_inf is not None:
        fs = fv.Freestream(u_inf=float(u_inf), direction=fs.direction,
                           rho=fs.rho, p_inf=fs.p_inf, nu=fs.nu,
                           compressible=fs.compressible)
    # Назви з controlDict бувають шаблонами чи групами, яких у сітці нема, —
    # лишаємо лише ті, що справді є. Якщо не лишилось нічого (або про тіла
    # нічого не знаємо) — беремо всі границі, що тілом не є за назвою: краще
    # показати зайве, ніж мовчки намалювати порожнє.
    present = res.patch_names(t)
    unknown = [n for n in wanted if n not in present]
    wanted = [n for n in wanted if n in present]
    if unknown:
        notes.append(tr("no such boundary: {unknown}", unknown=", ".join(unknown)))
    if not wanted:
        wanted = [n for n in present if n not in NOT_BODIES]
    bodies = {n: res.patch(t, n) for n in wanted}
    bodies = {n: m for n, m in bodies.items() if m is not None}

    if cfg is not None and manifest and manifest.get("cfg") and cfg.rotating:
        rotors = [fv.Rotor.from_zone(z) for z in cfg.rotating]
    else:
        rotors = mrf_rotors(case_dir, bodies)
    return LoadedCase(fv.CaseView(volume, bodies, fs, label or name), rotors,
                      name, t, source, notes)


def load_history(case_dir: str | Path, label: str = "") -> fv.HistoryCase:
    """Ряди коефіцієнтів кейса: сумарні й по частинах. Стаціонарність — з
    маніфесту, а без нього з розвʼязувача в controlDict: для нестаціонарного
    коливання — не біда, а фізика, і вирок про нього інший."""
    import fs_backend as be

    case_dir = Path(case_dir)
    series = be.read_force_coeffs(str(case_dir))
    parts = be.read_body_coeffs(str(case_dir))
    if not series and not parts:
        raise ValueError(tr("Case {name} has no postProcessing with "
                            "coefficients: it was not run, or they were "
                            "removed.", name=case_dir.name))
    manifest = lib.read_manifest(case_dir)
    if manifest and manifest.get("cfg"):
        steady = bool(lib.cfg_from_dict(manifest["cfg"]).solver.steady)
    else:
        text = _strip_comments(_read(case_dir / "system" / "controlDict"))
        app = re.search(r"application\s+(\w+)", text)
        steady = not (app and "pimple" in app.group(1).lower())
    return fv.HistoryCase(label or case_dir.name, series, parts, steady)
