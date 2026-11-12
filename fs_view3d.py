"""Вкладка «3D» на Rust: foamview (wgpu) малює, тут — камера й шари.

ЧОМУ КАМЕРА ТУТ, А НЕ В RUST. Матриці рахує Python, а Rust лише малює.
Тоді проєкція, яку перевіряють тести (`renderer.WorldToDisplay`), і кадр на
екрані — ті самі числа, а не дві реалізації, що можуть розійтися.

СУМІСНІСТЬ ІЗ VTK. Вікно й тести звертаються до старого `Viewport` через
`plotter.camera`, `plotter.camera_position`, `plotter.renderer`,
`actors[...]` з `GetBounds()` / `GetMapper().GetInput()`. Тут ті самі імена
поверх нового рушія, тож вікно не помічає підміни, а наявні тести
перевіряють новий перегляд без переписування.

Миша — як у VTK (trackball camera): ліва — обертати, середня або Shift+ліва
— зсувати, права й коліщатко — наближати, Ctrl+ліва — крутити навколо осі
погляду.
"""

from __future__ import annotations

import math
import os
from pathlib import Path

import numpy as np
from fs_i18n import tr

try:                                       # pragma: no cover - залежить від збірки
    import foamview as _fv
except ImportError:                        # pragma: no cover
    _fv = None

FONT_CANDIDATES = (r"C:\Windows\Fonts\segoeui.ttf", r"C:\Windows\Fonts\arial.ttf",
                   "/System/Library/Fonts/Supplemental/Arial.ttf",
                   "/Library/Fonts/Arial.ttf",
                   "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
                   "/usr/share/fonts/dejavu/DejaVuSans.ttf",
                   "/usr/share/fonts/TTF/DejaVuSans.ttf")

# Помилка в Rust приходить як pyo3 PanicException — це BaseException, а не
# Exception, тож `except Exception` її не ловить (перевірено). Ловимо все,
# крім Ctrl+C і виходу.
GPU_ERRORS = (Exception,) if _fv is None else (Exception, BaseException)


def gpu_failed(exc: BaseException) -> bool:
    return not isinstance(exc, (KeyboardInterrupt, SystemExit))


def available() -> bool:
    return _fv is not None


def window_kind() -> str | None:
    """Що таке winId() на цій ОС для foamview; None — лише без вікна.

    Linux (X11 / Wayland) поки без нативного вікна: foamview малює в
    текстуру, і кадр показує QPainter — повільніше, але працює.
    """
    import sys
    if sys.platform.startswith("win"):
        return "win32"
    if sys.platform == "darwin":
        return "appkit"
    return None


# --------------------------------------------------------------------------
# Хто малює вкладку «3D»: ланцюжок запасних шляхів
# --------------------------------------------------------------------------
#
# Вимога: вкладка «3D» ЗАВЖДИ показує картинку; повільніше — можна.
# Рівні по черзі:
#   gpu  — foamview на апаратному GPU (DX12 / Vulkan / Metal);
#   soft — foamview на програмному адаптері (WARP, lavapipe, llvmpipe);
#   vtk  — старий шлях (потребує OpenGL);
#   cpu  — foamview.CpuRenderer: без GPU і без OpenGL, повільно.
# FS_3D=gpu|soft|vtk|cpu — примусово один рівень (без перебору).
#
# ПРОБА В ОКРЕМОМУ ПРОЦЕСІ. Драйвер може впасти чи зависнути на пробі, а GL
# у тому ж процесі ламає VTK. Тому кожен кандидат пробується дочірнім
# Python з тайм-аутом, а вибір кешується на машину (ключ — список адаптерів
# і драйверів + версія foamview): наступні старти не чекають.
# GL у перебір не входить: де не працює VTK (OpenGL), там не запрацює і GL
# wgpu, а там, де VTK працює, GL wgpu його ламає. Примусово — WGPU_BACKEND=gl.

LEVELS = ("gpu", "soft", "vtk", "cpu")
LEVEL_TEXT = {
    "gpu": "3D: GPU",
    "soft": "3D: software GPU — slower",
    "vtk": "3D: VTK (OpenGL)",
    "cpu": "3D: CPU without GPU — slow",
}
PROBE_VERSION = 2          # змінити, коли міняється сама проба: кеш стане старим
PROBE_TIMEOUT = 25.0       # с на одного кандидата
_HW_RANK = {"DiscreteGpu": 0, "IntegratedGpu": 1, "VirtualGpu": 2, "Other": 3}


class Level:
    """Вибраний рівень: хто малює і на якому адаптері."""

    def __init__(self, level: str, backend: str | None = None,
                 adapter: str | None = None, note: str = ""):
        self.level = level
        self.backend = backend
        self.adapter = adapter
        self.note = note

    @property
    def foamview(self) -> bool:
        return self.level in ("gpu", "soft", "cpu")

    def text(self) -> str:
        t = tr(LEVEL_TEXT.get(self.level, self.level))
        if self.level in ("gpu", "soft") and self.adapter:
            t += f" ({self.adapter}, {self.backend})" if self.backend else f" ({self.adapter})"
        return t

    def as_dict(self) -> dict:
        return {"level": self.level, "backend": self.backend, "adapter": self.adapter,
                "note": self.note}

    def note_text(self) -> str:
        """`note` for the person, in the interface language (see note_text)."""
        return note_text(self.note)

    def __repr__(self):
        return f"Level({self.as_dict()})"


def make_renderer(level: Level, hwnd, w: int, h: int, font, kind: str = "win32"):
    """foamview.Renderer на вибраному адаптері або CpuRenderer."""
    if level.level == "cpu":
        return _fv.CpuRenderer(None, w, h, font)
    return _fv.Renderer(hwnd, w, h, font, kind=kind,
                        backend=level.backend, adapter=level.adapter)


_EYE = [1.0, 0, 0, 0, 0, 1.0, 0, 0, 0, 0, 1.0, 0, 0, 0, 0, 1.0]
_BG = (204, 51, 25)
_FG = (26, 179, 77)
_FG2 = (40, 90, 200)


def check_frame(r) -> tuple[bool, str]:
    """Пробний кадр 64×48: відоме тло + квадрат у центрі кольором ШАРУ.

    Квадрат ловить і помилки uniform-буферів: на WARP колись усі шари
    читали буфер останнього створеного шару — тло було чорним без жодної
    помилки wgpu.
    """
    r.set_background((0.8, 0.2, 0.1), (0.8, 0.2, 0.1))
    pts = np.array([[-.5, -.5, .5], [.5, -.5, .5], [.5, .5, .5], [-.5, .5, .5]], np.float32)
    r.set_triangles("probe", pts, np.array([[0, 1, 2], [0, 2, 3]], np.uint32),
                    (0.1, 0.7, 0.3, 1.0), lighting=False)
    img = np.asarray(r.screenshot(_EYE, _EYE, False, None))
    r.remove("probe")
    h, w = img.shape[:2]
    bg = img[2, 2, :3].astype(int)
    fg = img[h // 2, w // 2, :3].astype(int)
    ok = (np.abs(bg - _BG).max() < 12) and (np.abs(fg - _FG).max() < 12)
    if not ok:
        return False, tr("wrong frame: background {tolist}, center {tolist2}", tolist=bg.tolist(),
                         tolist2=fg.tolist())
    if hasattr(r, "set_scalar_triangles"):
        # Шар із полем («Результати»): інший шейдер і таблиця кольорів у
        # буфері шару — перевіряємо й його. Значення 0.75 на шкалі 0..1 з
        # двох кольорів — другий колір.
        lut = np.array([[c / 255 for c in _BG] + [1.0], [c / 255 for c in _FG2] + [1.0]],
                       np.float32)
        r.set_scalar_triangles("probe", pts, np.array([[0, 1, 2], [0, 2, 3]], np.uint32),
                               np.full(4, 0.75, np.float32), lut, (0.0, 1.0), lighting=False)
        img = np.asarray(r.screenshot(_EYE, _EYE, False, None))
        r.remove("probe")
        fg = img[h // 2, w // 2, :3].astype(int)
        if np.abs(fg - _FG2).max() >= 12:
            return False, tr("wrong frame of a layer with a field: center {tolist}", tolist=fg.tolist())
    return True, ""


def _probe_foamview(level: Level) -> tuple[bool, str]:
    """У ЦЬОМУ процесі: адаптер є, пристрій є, кадр правильний?"""
    try:
        r = make_renderer(level, None, 64, 48, None)
        ok, why = check_frame(r)
        return ok, r.adapter if ok else f"{r.adapter}: {why}"
    except BaseException as exc:                          # noqa: BLE001
        if not gpu_failed(exc):
            raise
        return False, f"{type(exc).__name__}: {exc}".splitlines()[0][:300]


def _probe_vtk() -> tuple[bool, str]:
    """У ЦЬОМУ процесі: VTK малює (OpenGL є і дає правильний кадр)?"""
    try:
        import pyvista as pv
        pl = pv.Plotter(off_screen=True, window_size=(64, 48))
        pl.set_background([c / 255 for c in _BG])
        pl.add_mesh(pv.Plane(i_size=4, j_size=4), color=[c / 255 for c in _FG],
                    lighting=False)
        pl.view_xy()
        img = np.asarray(pl.screenshot(return_img=True))
        pl.close()
        h, w = img.shape[:2]
        fg = img[h // 2, w // 2, :3].astype(int)
        ok = np.abs(fg - _FG).max() < 20
        return bool(ok), "OpenGL" if ok else tr("wrong frame: center {tolist}", tolist=fg.tolist())
    except Exception as exc:                              # noqa: BLE001
        return False, f"{type(exc).__name__}: {exc}".splitlines()[0][:300]


def _cache_path() -> Path:
    if os.environ.get("FS_3D_CACHE"):                     # тести, gpucheck
        return Path(os.environ["FS_3D_CACHE"])
    base = os.environ.get("LOCALAPPDATA") or os.path.join(os.path.expanduser("~"), ".cache")
    return Path(base) / "OpenFOAM Studio" / "view3d.json"


def _adapters() -> list[dict]:
    if _fv is None or not hasattr(_fv, "adapters"):
        return []
    try:
        return list(_fv.adapters())
    except BaseException as exc:                          # noqa: BLE001
        if not gpu_failed(exc):
            raise
        return []


def candidates(adapters: list[dict]) -> list[Level]:
    """Апаратні адаптери (дискретний → вбудований → …), далі програмні."""
    hw = [a for a in adapters if a.get("device_type") in _HW_RANK]
    hw.sort(key=lambda a: _HW_RANK[a["device_type"]])         # стабільне: порядок wgpu
    sw = [a for a in adapters if a.get("device_type") == "Cpu"]
    return ([Level("gpu", a["backend"], a["name"]) for a in hw]
            + [Level("soft", a["backend"], a["name"]) for a in sw])


def _cache_key(adapters: list[dict]) -> str:
    import json
    import sys
    ver = getattr(_fv, "__version__", "?") if _fv is not None else "-"
    rows = sorted((a.get("name", ""), a.get("backend", ""), a.get("device_type", ""),
                   a.get("driver", ""), a.get("driver_info", "")) for a in adapters)
    return json.dumps([PROBE_VERSION, ver, sys.platform, rows], ensure_ascii=False)


# "ok" of the probe; older versions wrote its translation ("ок").
_LEGACY_NOTE_OK = ("ok", "ок", "OK")


def note_text(note: str) -> str:
    """A probe note ("<adapter> (<backend>): ok; VTK: ok") for the person.

    The cache file (view3d.json) keeps the note of the probe time. The
    probe writes "ok" (versions before 2026-11-11 wrote the translation of
    that time, for example "ок"), so each part is translated here, in the
    interface language of NOW.
    """
    parts = []
    for part in (note or "").split("; "):
        head, sep, tail = part.rpartition(": ")
        if not sep:
            parts.append(tr(part))
        elif tail in _LEGACY_NOTE_OK:
            parts.append(f"{head}: {tr('OK', ctx="status")}")
        else:
            parts.append(f"{head}: {tr(tail)}")
    return "; ".join(p for p in parts if p)


def _run_probe(args: list[str]) -> tuple[bool, str]:
    """Проба в дочірньому процесі: падіння чи зависання драйвера не валить
    програму, а GL / OpenGL не чіпає контекст VTK цього процесу."""
    import json
    import subprocess
    import sys
    exe = sys.executable
    if exe.lower().endswith("pythonw.exe"):
        alt = exe[:-len("pythonw.exe")] + "python.exe"
        if os.path.isfile(alt):
            exe = alt
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    # Той самий foamview, що й у цьому процесі (gpucheck тестує збірку з
    # іншої теки), а не перший-ліпший поруч зі скриптом.
    env = dict(os.environ)
    if _fv is not None and getattr(_fv, "__file__", None):
        env["PYTHONPATH"] = os.pathsep.join(
            [os.path.dirname(os.path.abspath(_fv.__file__))]
            + [p for p in [env.get("PYTHONPATH")] if p])
    try:
        cp = subprocess.run([exe, "-X", "utf8", os.path.abspath(__file__), "--probe", *args],
                            capture_output=True, text=True, encoding="utf-8",
                            errors="replace", timeout=PROBE_TIMEOUT, creationflags=flags,
                            env=env)
    except subprocess.TimeoutExpired:
        return False, tr("probe hung (> {PROBE_TIMEOUT:.0f} s)",
                         PROBE_TIMEOUT=PROBE_TIMEOUT)
    except OSError as exc:
        return False, tr("probe did not start: {exc}", exc=exc)
    lines = [s for s in cp.stdout.splitlines() if s.startswith("{")]
    if not lines:
        tail = (cp.stderr.strip().splitlines() or ["?"])[-1][:200]
        return False, tr("probe crashed (code {returncode}): {tail}", returncode=cp.returncode,
                         tail=tail)
    res = json.loads(lines[-1])
    return bool(res.get("ok")), str(res.get("note", ""))


def _probe_main(argv: list[str]) -> int:
    """`python fs_view3d.py --probe foamview <level> <backend> <adapter>`
    або `--probe vtk`: один JSON-рядок з результатом."""
    import json
    if argv[:1] == ["vtk"]:
        ok, note = _probe_vtk()
    else:
        _, lv, be, ad = (argv + ["", "", "", ""])[:4]
        if _fv is None:
            ok, note = False, "foamview is not built"
        else:
            ok, note = _probe_foamview(Level(lv, be or None, ad or None))
    print(json.dumps({"ok": ok, "note": note}, ensure_ascii=False), flush=True)
    return 0


_LEVEL: Level | None = None


def select_level(refresh: bool = False, log=None) -> Level:
    """Хто малює вкладку «3D» на цій машині (див. коментар над LEVELS).

    Перший старт на машині (чи після зміни GPU / драйвера) — перебір
    кандидатів дочірніми процесами, ~1 с на кандидата; далі — з кешу.
    """
    import json
    global _LEVEL
    if _LEVEL is not None and not refresh:
        return _LEVEL
    say = log or (lambda s: None)
    forced, source = forced_level()
    if forced in LEVELS:
        if forced in ("gpu", "soft") and _fv is not None:
            # Примусово — без перебору: адаптер вибирає wgpu (або
            # WGPU_BACKEND / WGPU_ADAPTER_NAME).
            pick = [c for c in candidates(_adapters()) if c.level == forced]
            lv = pick[0] if (forced == "soft" and pick) else Level(forced)
        else:
            lv = Level(forced if (forced == "vtk" or _fv is not None) else "vtk")
        lv.note = (f"FS_3D={forced}" if source == "FS_3D"
                   else f"settings: {forced}")
        _LEVEL = lv
        return lv
    if _fv is None:
        _LEVEL = Level("vtk", note="foamview is not built")
        return _LEVEL
    adapters = _adapters()
    key = _cache_key(adapters)
    path = _cache_path()
    if not refresh:
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            if data.get("key") == key and data.get("level") in LEVELS:
                _LEVEL = Level(data["level"], data.get("backend"), data.get("adapter"),
                               data.get("note", ""))
                return _LEVEL
        except (OSError, ValueError):
            pass
    chosen = _probe_chain(adapters, key, path, say)
    _LEVEL = chosen
    return chosen


def check_graphics(log=None) -> Level:
    """«Перевірити графіку» (Файл → Налаштування → 3D): probe all candidates
    again, as the automatic choice does, and write the cache.

    It does not change the level of this process (the open views keep
    their renderer); the next start uses the new cache.
    """
    say = log or (lambda s: None)
    if _fv is None:
        return Level("vtk", note="foamview is not built")
    adapters = _adapters()
    return _probe_chain(adapters, _cache_key(adapters), _cache_path(), say)


def forced_level() -> tuple[str, str]:
    """(level, source) that the user forces, or ("", ""): the environment
    variable FS_3D first, then the program setting "view3d_backend"."""
    env = os.environ.get("FS_3D", "").strip().lower()
    if env:
        return env, "FS_3D"
    try:
        import fs_settings
        return fs_settings.view3d_forced()
    except Exception:                                     # noqa: BLE001
        return "", ""


def _probe_chain(adapters: list[dict], key: str, path: Path, say) -> Level:
    """Candidates one by one in child processes; the first good one wins.
    The result goes to the cache file."""
    import json
    import time
    notes = []
    chosen = None
    for c in candidates(adapters):
        say(tr("3D: trying {adapter} ({backend})…", adapter=c.adapter, backend=c.backend))
        ok, note = _run_probe(["foamview", c.level, c.backend or "", c.adapter or ""])
        # "ok", not tr('ок'): the cache keeps this note, and note_text()
        # translates it when it is shown (the language can change).
        notes.append(f"{c.adapter} ({c.backend}): {'ok' if ok else note}")
        if ok:
            chosen = Level(c.level, c.backend, c.adapter)
            break
    if chosen is None:
        say(tr("3D: trying VTK…"))
        ok, note = _run_probe(["vtk"])
        notes.append(f"VTK: {'ok' if ok else note}")
        chosen = Level("vtk") if ok else Level("cpu")
    chosen.note = "; ".join(notes)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"key": key, "time": time.strftime("%Y-%m-%d %H:%M"),
                                    **chosen.as_dict()}, ensure_ascii=False, indent=1),
                        encoding="utf-8")
    except OSError:
        pass
    return chosen


def forget_level():
    """Вибір не підтвердився в цьому процесі: стерти кеш, наступний старт
    перебере кандидатів заново."""
    global _LEVEL
    _LEVEL = None
    try:
        _cache_path().unlink()
    except OSError:
        pass


_PROBE: dict = {}


def probe(level: Level | None = None) -> tuple[bool, str]:
    """Пробний кадр У ЦЬОМУ процесі для рівня foamview (типово — GPU, як
    вибере wgpu). Памʼятається на весь запуск."""
    lv = level or Level("gpu")
    k = (lv.level, lv.backend, lv.adapter)
    if k not in _PROBE:
        _PROBE[k] = ((False, "foamview is not built") if _fv is None
                     else _probe_foamview(lv))
    return _PROBE[k]


def usable() -> bool:
    return probe()[0]


def font_path() -> str | None:
    for p in FONT_CANDIDATES:
        if os.path.isfile(p):
            return p
    return None


def hex_rgb(c) -> tuple[float, float, float]:
    """'#b9c4cf' / (r, g, b) у 0..1 / імена, що трапляються в коді."""
    if isinstance(c, str):
        named = {"white": "#ffffff", "black": "#000000", "red": "#ff0000",
                 "gray": "#808080", "grey": "#808080"}
        s = named.get(c.lower(), c).lstrip("#")
        if len(s) == 3:
            s = "".join(ch * 2 for ch in s)
        return tuple(int(s[i:i + 2], 16) / 255.0 for i in (0, 2, 4))
    r, g, b = (float(x) for x in c[:3])
    if max(r, g, b) > 1.0:
        r, g, b = r / 255.0, g / 255.0, b / 255.0
    return (r, g, b)


# --------------------------------------------------------------------------
# Камера (VTK-сумісна)
# --------------------------------------------------------------------------

def _unit(v):
    v = np.asarray(v, float)
    n = float(np.linalg.norm(v))
    return v / n if n > 1e-300 else v


class Camera:
    """Те, що вікно й тести беруть у vtkCamera / pyvista Camera."""

    def __init__(self):
        self._pos = np.array([1.0, -1.0, 1.0])
        self._foc = np.zeros(3)
        self._up = np.array([0.0, 0.0, 1.0])
        self.view_angle = 30.0
        self.parallel_projection = False
        self.parallel_scale = 1.0
        self.clipping_range = (0.01, 1000.0)
        self.changed = None                  # викликається після зміни

    def _touch(self):
        if self.changed is not None:
            self.changed()

    # --- pyvista-стиль ---------------------------------------------------
    @property
    def position(self):
        return tuple(float(x) for x in self._pos)

    @position.setter
    def position(self, v):
        self._pos = np.asarray(v, float).copy()
        self._orthogonalize()
        self._touch()

    @property
    def focal_point(self):
        return tuple(float(x) for x in self._foc)

    @focal_point.setter
    def focal_point(self, v):
        self._foc = np.asarray(v, float).copy()
        self._orthogonalize()
        self._touch()

    @property
    def up(self):
        return tuple(float(x) for x in self._up)

    @up.setter
    def up(self, v):
        self._up = _unit(v)
        self._orthogonalize()
        self._touch()

    view_up = up

    def _orthogonalize(self):
        d = self._foc - self._pos
        if float(np.linalg.norm(d)) < 1e-300:
            return
        d = _unit(d)
        u = self._up - d * float(np.dot(self._up, d))
        if float(np.linalg.norm(u)) < 1e-9:
            # Погляд уздовж «угору»: беремо будь-який перпендикуляр.
            u = np.cross(d, [1.0, 0.0, 0.0])
            if float(np.linalg.norm(u)) < 1e-9:
                u = np.cross(d, [0.0, 1.0, 0.0])
        self._up = _unit(u)

    # --- VTK-стиль -------------------------------------------------------
    def GetPosition(self):
        return self.position

    def SetPosition(self, *v):
        self.position = v[0] if len(v) == 1 else v

    def GetFocalPoint(self):
        return self.focal_point

    def SetFocalPoint(self, *v):
        self.focal_point = v[0] if len(v) == 1 else v

    def GetViewUp(self):
        return self.up

    def SetViewUp(self, *v):
        self.up = v[0] if len(v) == 1 else v

    def GetParallelScale(self):
        return float(self.parallel_scale)

    def SetParallelScale(self, s):
        self.parallel_scale = float(s)
        self._touch()

    def GetParallelProjection(self):
        return bool(self.parallel_projection)

    def SetParallelProjection(self, on):
        self.parallel_projection = bool(on)
        self._touch()

    def GetViewAngle(self):
        return float(self.view_angle)

    def SetViewAngle(self, a):
        self.view_angle = float(a)
        self._touch()

    def GetDistance(self):
        return float(np.linalg.norm(self._pos - self._foc))

    def GetDirectionOfProjection(self):
        return tuple(_unit(self._foc - self._pos))

    def GetClippingRange(self):
        return tuple(self.clipping_range)

    # --- рухи ------------------------------------------------------------
    def azimuth(self, deg: float):
        self._rotate_about(self._up, deg)

    def elevation(self, deg: float):
        # Як vtkCamera::Elevation: вісь — cross(−напрямок погляду, угору),
        # «угору» лишається (його потім вирівнює _orthogonalize).
        axis = _unit(np.cross(self._pos - self._foc, self._up))
        self._rotate_about(axis, deg, keep_up=True)

    Azimuth = azimuth
    Elevation = elevation

    def _rotate_about(self, axis, deg, keep_up=True):
        a = math.radians(deg)
        k = _unit(axis)
        v = self._pos - self._foc

        def rot(x):
            return (x * math.cos(a) + np.cross(k, x) * math.sin(a)
                    + k * float(np.dot(k, x)) * (1 - math.cos(a)))
        self._pos = self._foc + rot(v)
        if not keep_up:
            self._up = _unit(rot(self._up))
        self._orthogonalize()
        self._touch()

    def roll(self, deg: float):
        d = _unit(self._foc - self._pos)
        a = math.radians(deg)
        u = self._up
        self._up = _unit(u * math.cos(a) + np.cross(d, u) * math.sin(a))
        self._touch()

    def dolly(self, factor: float):
        """factor > 1 — ближче (як vtkCamera.Dolly)."""
        if self.parallel_projection:
            self.parallel_scale /= max(factor, 1e-9)
        else:
            self._pos = self._foc + (self._pos - self._foc) / max(factor, 1e-9)
        self._touch()

    def zoom(self, factor: float):
        if self.parallel_projection:
            self.parallel_scale /= max(factor, 1e-9)
        else:
            self.view_angle = max(1e-3, min(179.0, self.view_angle / factor))
        self._touch()

    Zoom = zoom
    Dolly = dolly

    # --- матриці ---------------------------------------------------------
    def view_matrix(self) -> np.ndarray:
        f = _unit(self._foc - self._pos)
        s = _unit(np.cross(f, self._up))
        u = np.cross(s, f)
        m = np.eye(4)
        m[0, :3], m[1, :3], m[2, :3] = s, u, -f
        m[:3, 3] = -m[:3, :3] @ self._pos
        return m

    def projection_matrix(self, aspect: float) -> np.ndarray:
        """Проєкція з глибиною 0..1 (як у wgpu/D3D)."""
        n, fa = (float(x) for x in self.clipping_range)
        if not self.parallel_projection:
            n = max(n, 1e-9)
        fa = max(fa, n + max(abs(n), 1e-9) * 1e-4)
        m = np.zeros((4, 4))
        if self.parallel_projection:
            h = max(self.parallel_scale, 1e-12)
            w = h * aspect
            m[0, 0] = 1.0 / w
            m[1, 1] = 1.0 / h
            m[2, 2] = -1.0 / (fa - n)
            m[2, 3] = -n / (fa - n)
            m[3, 3] = 1.0
        else:
            t = 1.0 / math.tan(math.radians(self.view_angle) / 2.0)
            m[0, 0] = t / aspect
            m[1, 1] = t
            m[2, 2] = fa / (n - fa)
            m[2, 3] = n * fa / (n - fa)
            m[3, 2] = -1.0
        return m

    def reset_clipping(self, bounds):
        """Ближня й дальня площини навколо сцени (як ResetCameraClippingRange)."""
        if bounds is None:
            return
        b = np.asarray(bounds, float).reshape(3, 2)
        corners = np.array([[x, y, z] for x in b[0] for y in b[1] for z in b[2]])
        d = _unit(self._foc - self._pos)
        depth = (corners - self._pos) @ d
        near, far = float(depth.min()), float(depth.max())
        span = max(far - near, 1e-6)
        far = far + 0.05 * span
        near = near - 0.05 * span
        if self.parallel_projection:
            # У паралельній ближня площина може стояти й позаду камери:
            # глибина там лінійна, від'ємна ближня їй не шкодить.
            self.clipping_range = (near, far)
        elif far <= 1e-9:
            self.clipping_range = (1e-4, 1.0)      # сцена позаду камери
        else:
            self.clipping_range = (max(near, far * 1e-4, 1e-9), far)


class _Renderer:
    """Те, що тести беруть у vtkRenderer: WorldToDisplay і камера."""

    def __init__(self, plotter):
        self._pl = plotter
        self._world = (0.0, 0.0, 0.0, 1.0)
        self._display = (0.0, 0.0, 0.0)
        self.camera_set = False

    def GetActiveCamera(self):
        return self._pl.camera

    def SetWorldPoint(self, x, y, z, w=1.0):
        self._world = (x, y, z, w)

    def WorldToDisplay(self):
        w, h = self._pl.window_size
        vp = self._pl.view_proj()
        c = vp @ np.array(self._world, float)
        cw = c[3] if abs(c[3]) > 1e-300 else 1e-300
        ndc = c[:3] / cw
        self._display = ((ndc[0] + 1) * 0.5 * w, (ndc[1] + 1) * 0.5 * h, ndc[2])

    def GetDisplayPoint(self):
        return self._display

    def reset_camera_clipping_range(self):
        self._pl.camera.reset_clipping(self._pl.scene_bounds())

    ResetCameraClippingRange = reset_camera_clipping_range


class _RenderWindow:
    def __init__(self, plotter):
        self._pl = plotter

    def GetSize(self):
        return tuple(self._pl.window_size)

    def Render(self):
        self._pl.render()


class _Mapper:
    """Те, що код бере в мапері VTK/pyvista: вхідні дані (`dataset`,
    `SetInputData`), де лежить поле (`SetScalarModeTo…`, `SelectColorArray`)
    і зсув глибини (Viewport вмикає його для підсвітки, що лежить РІВНО на
    поверхні тіла; тут це — шар-накладка в foamview).

    Нові дані НЕ малюють кадр самі (як і в pyvista): кадр — `render()`."""

    def __init__(self, actor):
        self._a = actor

    def GetInput(self):
        return self._a.mesh

    GetInputAsDataSet = GetInput

    def Update(self):
        """Мапер VTK тут нічого не рахує: дані вже лежать у шарі."""

    @property
    def dataset(self):
        return self._a.mesh

    @dataset.setter
    def dataset(self, ds):
        self._a.set_mesh(ds)

    def SetInputData(self, ds):
        self._a.set_mesh(ds)

    def SetScalarModeToUseCellData(self):
        self._a.restyle(preference="cell")

    def SetScalarModeToUsePointData(self):
        self._a.restyle(preference="point")

    def SelectColorArray(self, name):
        self._a.restyle(scalars=name)

    @property
    def scalar_range(self):
        return self._a.style.get("clim")

    def SetResolveCoincidentTopologyToPolygonOffset(self):
        self._a.set_overlay(True)

    def SetRelativeCoincidentTopologyPolygonOffsetParameters(self, *_a):
        self._a.set_overlay(True)

    def SetRelativeCoincidentTopologyLineOffsetParameters(self, *_a):
        self._a.set_overlay(True)


class _Property:
    """vtkProperty актора: те, що код міняє після add_mesh."""

    def __init__(self, actor):
        self._a = actor

    def SetEdgeVisibility(self, on):
        self._a.restyle(show_edges=bool(on))

    def GetEdgeVisibility(self):
        return bool(self._a.style.get("show_edges"))

    def SetOpacity(self, a):
        self._a.restyle(opacity=float(a))

    def GetOpacity(self):
        return float(self._a.style.get("opacity", 1.0))

    def SetColor(self, *c):
        self._a.restyle(color=tuple(c[0]) if len(c) == 1 else tuple(c))

    def SetLineWidth(self, w):
        self._a.restyle(line_width=float(w))

    def SetPointSize(self, s):
        self._a.restyle(point_size=float(s))

    def __getattr__(self, name):            # решта властивостей — без дії
        if name.startswith("Set") or name.endswith("On") or name.endswith("Off"):
            return lambda *_a, **_k: None
        raise AttributeError(name)


class _Inert:
    """Властивість тексту шкали: шрифт тут — шрифт усього Plotter (файл
    TrueType з кирилицею), тож Set… нічого не міняють, а GetFontFile каже,
    яким файлом шкалу підписано."""

    def __init__(self, font=None):
        self._font = font

    def GetFontFile(self):
        return self._font

    def __getattr__(self, name):
        return lambda *_a, **_k: None


class Actor:
    """Шар у сцені — з тим, що в акторі VTK питають вікно й тести."""

    def __init__(self, view, name: str, mesh, kind: str, style: dict | None = None):
        self._view = view
        self.name = name
        self.mesh = mesh
        self.kind = kind
        self.style = dict(style or {})
        # Імена шарів у foamview: сам шар і ребра граней (show_edges).
        self.parts: list[str] = [name, name + EDGES]
        self._visible = True
        self._mapper = _Mapper(self)
        # Як значення поля лягли на шар (з _upload): де лежить поле і яка
        # комірка сітки в кожного трикутника. Для швидкого кадру анімації.
        self._field: dict | None = None
        self._values = None                 # значення з set_scalars

    def set_overlay(self, on: bool):
        if bool(self.style.get("overlay")) != bool(on):
            self.style["overlay"] = bool(on)
            self._view._upload(self)

    def set_mesh(self, ds):
        """Нові дані того самого шару (зріз пересунули, кадр анімації)."""
        self.mesh = _as_polydata(ds)
        self._values = None
        self._view._upload(self)

    def restyle(self, **kw):
        changed = {k: v for k, v in kw.items() if self.style.get(k, None) != v}
        if changed:
            self.style.update(changed)
            self._view._upload(self)

    def set_scalars(self, values) -> None:
        """Лише нові значення поля — геометрія лишається на GPU (кадр
        анімації). `values` — по точках або по комірках `mesh`, як поле
        лежало, коли шар клали. Кадр не малює: це `render()`."""
        v = np.ascontiguousarray(values, np.float32).ravel()
        self._values = v
        f = self._field
        g = self._view.gpu
        if f is not None and g is not None and hasattr(g, "update_scalars"):
            try:
                g.update_scalars(self.name, v if f["cid"] is None else v[f["cid"]])
                return
            except GPU_ERRORS as exc:                     # noqa: BLE001
                if not gpu_failed(exc):
                    raise
        self._view._upload(self)

    def SetText(self, _position, text):
        """vtkCornerAnnotation.SetText: новий текст підпису."""
        self.style["texts"] = [str(text)]
        self._view._upload(self)

    def GetBounds(self):
        return tuple(self.mesh.bounds) if self.mesh is not None else (0,) * 6

    @property
    def mapper(self):
        return self._mapper

    def GetMapper(self):
        return self._mapper

    def GetProperty(self):
        return _Property(self)

    prop = property(GetProperty)

    def SetVisibility(self, on):
        self._visible = bool(on)
        self._view._set_visible(self.name, self._visible)

    def GetVisibility(self):
        return self._visible

    visibility = property(GetVisibility, SetVisibility)

    VisibilityOn = lambda self: self.SetVisibility(True)      # noqa: E731
    VisibilityOff = lambda self: self.SetVisibility(False)    # noqa: E731


# Суфікси імен шарів у foamview: ребра граней актора, частини шкали.
EDGES = "\u0001edges"
BAR = "\u0002bar\u0000"


class ScalarBar:
    """Шкала кольорів (як vtkScalarBarActor у pyvista): смуга з таблицею
    кольорів актора, позначки й заголовок. Місце — частки вікна від лівого
    нижнього кута, тож шкала йде за зміною розміру вікна сама."""

    def __init__(self, view, title: str, actor: Actor, *, vertical=False, position_x=None,
                 position_y=None, width=None, height=None, n_labels=5, fmt=None,
                 color=None, title_font_size=None, label_font_size=None):
        self._view = view
        self.title = title
        self.actor = actor
        self.vertical = bool(vertical)
        d = (0.9, 0.02, 0.08, 0.45) if self.vertical else (0.35, 0.05, 0.6, 0.08)
        self.box = tuple(float(v) if v is not None else dv for v, dv in
                         zip((position_x, position_y, width, height), d))
        self.n_labels = int(n_labels if n_labels is not None else 5)
        self.fmt = fmt or "%.3g"
        self.color = hex_rgb(color) if color is not None else (1.0, 1.0, 1.0)
        self.title_size = int(title_font_size or 14)
        self.label_size = int(label_font_size or 12)
        self._visible = True
        key = BAR + title
        self.parts = [key + "\u0000strip", key + "\u0000labels", key + "\u0000title"]

    def SetVisibility(self, on):
        self._visible = bool(on)
        self._view._upload_bar(self)
        self._view._dirty()

    def GetVisibility(self):
        return self._visible

    VisibilityOn = lambda self: self.SetVisibility(True)      # noqa: E731
    VisibilityOff = lambda self: self.SetVisibility(False)    # noqa: E731

    def GetTitleTextProperty(self):
        return _Inert(getattr(self._view, "font", None) or font_path())

    GetLabelTextProperty = GetTitleTextProperty
    GetAnnotationTextProperty = GetTitleTextProperty

    def ticks(self) -> list[str]:
        lo, hi = self.actor.style["clim"]
        n = max(self.n_labels, 2)
        out = []
        for v in np.linspace(lo, hi, n):
            try:
                out.append(self.fmt % v)
            except (TypeError, ValueError):
                out.append("%.3g" % v)
        return out


# --------------------------------------------------------------------------
# Геометрія pyvista → масиви для foamview
# --------------------------------------------------------------------------

NAN_COLOR = (0.663, 0.663, 0.663, 1.0)     # як у pyvista (dark_gray)
# Світло на грані, що стоїть ребром до погляду, для поверхні з полем. Більше,
# ніж у простої поверхні (0.18): колір поля лишається близьким до кольору на
# шкалі, як під «light kit» VTK у pyvista, а форму ще видно.
FIELD_AMBIENT = 0.55
CID = "__fs_cid"                           # номер комірки до розбиття на трикутники


def _as_polydata(ds):
    import pyvista as pv
    if ds is None:
        return None
    m = ds if isinstance(ds, pv.DataSet) else pv.wrap(ds)
    if not isinstance(m, pv.PolyData):
        # extract_cells / підсвітка граней дає UnstructuredGrid: малюємо
        # його поверхню (VTK робить так само).
        m = m.extract_surface(algorithm="dataset_surface")
    return m


def lut_colors(cmap=None, n_colors=256) -> np.ndarray:
    """(K, 4) float32: таблиця кольорів, як pyvista будує її для VTK —
    K рівних відрізків шкали, колір у кожному — з мапи `cmap`."""
    from matplotlib import colormaps
    from matplotlib.colors import Colormap, ListedColormap
    if cmap is None:
        cm = colormaps["viridis"]
    elif isinstance(cmap, Colormap):
        cm = cmap
    elif isinstance(cmap, str):
        cm = colormaps[cmap]
    else:
        cm = ListedColormap([hex_rgb(c) for c in cmap])
    n = int(max(1, min(int(n_colors or 256), 256)))
    return np.ascontiguousarray(cm(np.linspace(0.0, 1.0, n)), np.float32)


def _field(mesh, scalars, preference="point"):
    """(значення float32 (n,), "point" | "cell") для `scalars` з add_mesh
    (імʼя масиву або сам масив), або None. Вектор — його довжина (як pyvista)."""
    if scalars is None or mesh is None:
        return None
    if isinstance(scalars, str):
        order = ("cell", "point") if preference == "cell" else ("point", "cell")
        for where in order:
            data = mesh.cell_data if where == "cell" else mesh.point_data
            if scalars in data.keys():
                v = np.asarray(data[scalars])
                break
        else:
            return None
    else:
        v = np.asarray(scalars)
        n = v.shape[0] if v.ndim else 0
        if n == mesh.n_points and not (preference == "cell" and n == mesh.n_cells):
            where = "point"
        elif n == mesh.n_cells:
            where = "cell"
        else:
            return None
    if v.ndim == 2:
        v = np.linalg.norm(v, axis=1)
    return np.ascontiguousarray(v, np.float32).ravel(), where


def _clim(values) -> tuple[float, float]:
    """Межі шкали за даними (pyvista: від найменшого до найбільшого)."""
    v = np.asarray(values, float)
    v = v[np.isfinite(v)]
    if not v.size:
        return (0.0, 1.0)
    lo, hi = float(v.min()), float(v.max())
    if hi <= lo:
        hi = lo + max(abs(lo) * 1e-6, 1e-12)
    return (lo, hi)


def _has_faces(m) -> bool:
    """Чи є в сітці грані (полігони) — без застарілого n_faces_strict."""
    faces = getattr(m, "faces", None)
    return faces is not None and len(faces) > 0


def _triangles(mesh, cells: bool = False):
    """(points float32 (N,3), tris uint32 (M,3), cid) поверхні, або None.

    cid — номер комірки `mesh` для кожного трикутника (cells=True), щоб
    покласти на трикутники значення по комірках; None — номери збігаються.
    Порядок комірок VTK: точки, лінії, а вже потім полігони."""
    m = _as_polydata(mesh)
    if m is None or not m.n_points or not _has_faces(m):
        return None
    cid = None
    if m.is_all_triangles:
        t = m
        first = m.n_verts + m.n_lines
        if cells and first:
            cid = np.arange(first, first + len(m.faces) // 4)
    else:
        if cells:
            t = m.copy(deep=False)
            t.cell_data[CID] = np.arange(m.n_cells, dtype=np.int64)
            t = t.triangulate()
            cid = np.asarray(t.cell_data[CID])[t.n_verts + t.n_lines:]
        else:
            t = m.triangulate()
    faces = np.asarray(t.faces).reshape(-1, 4)[:, 1:]
    return (np.ascontiguousarray(t.points, np.float32),
            np.ascontiguousarray(faces, np.uint32), cid)


def _line_index(m):
    """(i0, i1, line) для кожного відрізка ліній PolyData `m`: номери точок
    кінців і номер лінії (від першої лінії, без точок-вершин)."""
    from vtkmodules.util.numpy_support import vtk_to_numpy
    ca = m.GetLines()
    if ca is None or ca.GetNumberOfCells() == 0:
        return None
    off = vtk_to_numpy(ca.GetOffsetsArray()).astype(np.int64)
    conn = vtk_to_numpy(ca.GetConnectivityArray()).astype(np.int64)
    if len(conn) < 2:
        return None
    k = np.arange(len(conn) - 1)
    start = np.zeros(len(conn) + 1, bool)
    start[off[1:-1]] = True                 # тут починається наступна лінія
    keep = ~start[k + 1]
    k = k[keep]
    if not k.size:
        return None
    line = np.searchsorted(off, k, side="right") - 1
    return conn[k], conn[k + 1], line


def _segments(mesh, wireframe: bool = False, field=None):
    """Відрізки (M,6) float32: лінії сітки, а для wireframe — ребра граней.
    field — (значення, "point"|"cell") тієї ж сітки: тоді ще й значення на
    кінцях (2M,). Повертає segs або (segs, pairs); None — ліній немає."""
    m = _as_polydata(mesh)
    if m is None or not m.n_points:
        return None
    if wireframe and _has_faces(m):
        m = m.extract_all_edges()
        field = None
    got = _line_index(m)
    if got is None:
        return None
    i0, i1, line = got
    pts = np.asarray(m.points, np.float32)
    segs = np.ascontiguousarray(np.column_stack([pts[i0], pts[i1]]), np.float32)
    if field is None:
        return segs
    v, where = field
    if where == "point":
        pairs = np.column_stack([v[i0], v[i1]])
    else:
        c = v[m.n_verts + line]
        pairs = np.column_stack([c, c])
    return segs, np.ascontiguousarray(pairs, np.float32).ravel()


def _points(mesh):
    """Точки як відрізки нульової довжини (foamview малює їх квадратиками)."""
    if mesh is None or not mesh.n_points:
        return None
    p = np.asarray(mesh.points, np.float32)
    return np.ascontiguousarray(np.column_stack([p, p]), np.float32)


def _point_normals(pts: np.ndarray, tris: np.ndarray) -> np.ndarray:
    """Нормалі в точках (сума нормалей граней, зважена площею) — гладке
    освітлення (smooth_shading). Бік не важливий: світло двостороннє."""
    a, b, c = pts[tris[:, 0]], pts[tris[:, 1]], pts[tris[:, 2]]
    fn = np.cross(b - a, c - a).astype(np.float64)
    n = np.zeros((len(pts), 3))
    for k in range(3):                  # bincount: у рази швидше за np.add.at
        for j in range(3):
            n[:, j] += np.bincount(tris[:, k], weights=fn[:, j], minlength=len(pts))
    ln = np.linalg.norm(n, axis=1, keepdims=True)
    return np.ascontiguousarray(n / np.where(ln > 0, ln, 1.0), np.float32)


# --------------------------------------------------------------------------
# Ручки, які тягнуть мишею (замість віджетів VTK)
# --------------------------------------------------------------------------
#
# fs_views ставить у вʼюпорт три ручки VTK: площину зрізу
# (vtkImplicitPlaneWidget2), відрізок і кулю насіння ліній потоку
# (vtkLineWidget, vtkSphereWidget2). Їм потрібен інтерактор VTK, а тут його
# нема. DragWidget — те саме для foamview: малюється шарами, мишу бере
# GpuWidget, і в нього ті самі методи VTK, які кличе fs_views (GetOrigin,
# SetPoint1, GetCenter, AddObserver, InvokeEvent, Off…), тож код сцен і
# тести працюють з обома.

class DragWidget:
    """kind: "plane" — площина ⟂ осі `axis`, ручка — переріз рамки `bounds`,
    тягнеться лише вздовж осі й не виходить за рамку; "line" — відрізок з
    двома кінцями-ручками (тягнути кінець або весь); "sphere" — куля (ліва
    кнопка — перенести, права — радіус)."""

    GRAB_PX = 10.0                          # на скільки пікселів біля ручки беремо

    def __init__(self, plotter, kind: str, *, color=(0.9, 0.92, 0.95), bounds=None,
                 axis: int = 0, origin=(0.0, 0.0, 0.0), point1=None, point2=None,
                 centre=None, radius: float = 1.0, line_width: float = 3.0):
        self._pl = plotter
        self.kind = kind
        self.color = tuple(color)
        self.bounds = tuple(float(b) for b in bounds) if bounds is not None else None
        self.axis = int(axis)
        self._origin = np.asarray(origin, float).copy()
        self._p1 = np.asarray(point1 if point1 is not None else (0, 0, 0), float).copy()
        self._p2 = np.asarray(point2 if point2 is not None else (1, 0, 0), float).copy()
        self._centre = np.asarray(centre if centre is not None else (0, 0, 0), float).copy()
        self._radius = float(radius)
        self.line_width = float(line_width)
        self._obs: dict[str, list] = {}
        self._on = False
        self._grab = None                   # що тягнуть: "plane", "p1", "p2", "line", "move", "radius"
        self._selected = False
        self.name = f"\u0003widget{id(self)}"
        self.parts = [self.name + s for s in ("\u0000fill", "\u0000line", "\u0000dots")]

    # --- VTK-подібне ---------------------------------------------------------
    def AddObserver(self, event, cb):
        self._obs.setdefault(event, []).append(cb)
        return len(self._obs[event])

    def RemoveAllObservers(self):
        self._obs.clear()

    def InvokeEvent(self, event):
        for cb in list(self._obs.get(event, ())):
            cb(self, event)

    def On(self):
        if not self._on:
            self._on = True
            self._pl._add_widget(self)

    def Off(self):
        if self._on:
            self._on = False
            self._pl._remove_widget(self)

    def SetInteractor(self, iren):
        if iren is None:
            self.Off()

    def GetEnabled(self):
        return self._on

    def GetOrigin(self):
        return tuple(float(x) for x in self._origin)

    def SetOrigin(self, *o):
        o = np.asarray(o[0] if len(o) == 1 else o, float)
        self._origin = self._clamp_origin(o)
        self._redraw()

    def GetNormal(self):
        n = [0.0, 0.0, 0.0]
        n[self.axis] = 1.0
        return tuple(n)

    def GetPoint1(self):
        return tuple(float(x) for x in self._p1)

    def GetPoint2(self):
        return tuple(float(x) for x in self._p2)

    def SetPoint1(self, *p):
        self._p1 = np.asarray(p[0] if len(p) == 1 else p, float).copy()
        self._redraw()

    def SetPoint2(self, *p):
        self._p2 = np.asarray(p[0] if len(p) == 1 else p, float).copy()
        self._redraw()

    def GetCenter(self):
        return tuple(float(x) for x in self._centre)

    def SetCenter(self, *c):
        self._centre = np.asarray(c[0] if len(c) == 1 else c, float).copy()
        self._redraw()

    def GetRadius(self):
        return float(self._radius)

    def SetRadius(self, r):
        self._radius = max(float(r), 1e-12)
        self._redraw()

    # --- геометрія -----------------------------------------------------------
    def _clamp_origin(self, o):
        o = np.asarray(o, float).copy()
        if self.bounds is not None:
            lo, hi = self.bounds[2 * self.axis], self.bounds[2 * self.axis + 1]
            o[self.axis] = min(max(o[self.axis], lo), hi)
        return o

    def _plane_quad(self) -> np.ndarray:
        """Чотири кути перерізу рамки площиною (ручка площини)."""
        u, v = [a for a in range(3) if a != self.axis]
        b = self.bounds or (-1, 1, -1, 1, -1, 1)
        out = []
        for cu, cv in ((0, 0), (1, 0), (1, 1), (0, 1)):
            p = np.zeros(3)
            p[self.axis] = self._origin[self.axis]
            p[u] = b[2 * u + cu]
            p[v] = b[2 * v + cv]
            out.append(p)
        return np.asarray(out)

    def _circles(self, n: int = 48) -> np.ndarray:
        """Три великі кола кулі: відрізки (3n, 6)."""
        t = np.linspace(0, 2 * np.pi, n + 1)
        segs = []
        for a, b in ((0, 1), (1, 2), (0, 2)):
            ring = np.zeros((n + 1, 3))
            ring[:, a] = np.cos(t)
            ring[:, b] = np.sin(t)
            ring = self._centre + self._radius * ring
            segs.append(np.column_stack([ring[:-1], ring[1:]]))
        return np.vstack(segs)

    def _redraw(self):
        if self._on:
            self._pl._upload_widget(self)
            self._pl._dirty()

    def draw(self, g):
        """Покласти шари віджета в foamview `g`."""
        for p in self.parts:
            g.remove(p)
        if not self._on:
            return
        k = 1.25 if self._selected else 1.0
        col = tuple(min(1.0, c * k) for c in self.color)
        if self.kind == "plane":
            q = self._plane_quad().astype(np.float32)
            g.set_triangles(self.parts[0], q, np.array([[0, 1, 2], [0, 2, 3]], np.uint32),
                            (*col, 0.16 if self._selected else 0.08), mode="transparent",
                            lighting=False)
            edges = np.array([np.concatenate([q[i], q[(i + 1) % 4]]) for i in range(4)],
                             np.float32)
            g.set_lines(self.parts[1], edges, (*col, 1.0), width=2.0, overlay=True)
        elif self.kind == "line":
            seg = np.concatenate([self._p1, self._p2]).astype(np.float32)[None, :]
            g.set_lines(self.parts[1], seg, (*col, 1.0), width=self.line_width, overlay=True)
            dots = np.array([np.concatenate([self._p1, self._p1]),
                             np.concatenate([self._p2, self._p2])], np.float32)
            g.set_lines(self.parts[2], dots, (*col, 1.0), width=10.0, overlay=True)
        elif self.kind == "sphere":
            g.set_lines(self.parts[1], self._circles().astype(np.float32), (*col, 0.6),
                        width=1.5, overlay=True)

    # --- миша ------------------------------------------------------------------
    def _screen(self, p) -> np.ndarray:
        return self._pl._to_screen(p)

    def pick(self, xy, button) -> str | None:
        """Що під курсором `xy` (пікселі вікна, y униз): імʼя ручки або None."""
        xy = np.asarray(xy, float)
        r = self.GRAB_PX * self._pl._dpr()
        if self.kind == "plane":
            if button != "left":
                return None
            poly = np.array([self._screen(p) for p in self._plane_quad()])
            return "plane" if _inside(xy, poly) else None
        if self.kind == "line":
            if button != "left":
                return None
            a, b = self._screen(self._p1), self._screen(self._p2)
            if np.linalg.norm(xy - a) <= r:
                return "p1"
            if np.linalg.norm(xy - b) <= r:
                return "p2"
            return "line" if _seg_dist(xy, a, b) <= r * 0.7 else None
        if self.kind == "sphere":
            c = self._screen(self._centre)
            edge = self._screen(self._centre + self._radius * self._pl._screen_right())
            rad = np.linalg.norm(edge - c)
            if np.linalg.norm(xy - c) <= rad + r:
                return "move" if button == "left" else "radius" if button == "right" else None
        return None

    def press(self, grab: str):
        self._grab = grab
        self._selected = True
        self.InvokeEvent("StartInteractionEvent")
        self._redraw()

    def move(self, d_xy, xy):
        """Курсор зрушив на d_xy пікселів (y униз)."""
        g = self._grab
        if g is None:
            return
        if g == "plane":
            o = self._origin
            a = self._screen(o)
            ax = np.zeros(3)
            ax[self.axis] = 1.0
            span = (self.bounds[2 * self.axis + 1] - self.bounds[2 * self.axis]
                    if self.bounds else 1.0) or 1.0
            b = self._screen(o + ax * span)
            d = b - a
            dd = float(d @ d)
            if dd > 1e-9:
                o = o.copy()
                o[self.axis] += float(np.asarray(d_xy) @ d) / dd * span
                self._origin = self._clamp_origin(o)
        elif g in ("p1", "p2", "line", "move"):
            at = {"p1": self._p1, "p2": self._p2,
                  "line": 0.5 * (self._p1 + self._p2), "move": self._centre}[g]
            w = self._pl._screen_to_world_delta(at, d_xy)
            if g in ("p1", "line"):
                self._p1 = self._p1 + w
            if g in ("p2", "line"):
                self._p2 = self._p2 + w
            if g == "move":
                self._centre = self._centre + w
        elif g == "radius":
            # Угору — більше, униз — менше (як права кнопка у VTK).
            self._radius = max(self._radius * (1.0 + 0.01 * -float(d_xy[1])), 1e-9)
        self._redraw()
        self.InvokeEvent("InteractionEvent")

    def release(self):
        if self._grab is None:
            return
        self._grab = None
        self._selected = False
        self._redraw()
        self.InvokeEvent("EndInteractionEvent")


def _inside(p, poly) -> bool:
    """Точка в опуклому чи простому многокутнику (парність перетинів)."""
    x, y = p
    n, ins = len(poly), False
    for i in range(n):
        x1, y1 = poly[i]
        x2, y2 = poly[(i + 1) % n]
        if (y1 > y) != (y2 > y):
            xc = x1 + (y - y1) * (x2 - x1) / (y2 - y1)
            if xc > x:
                ins = not ins
    return ins


def _seg_dist(p, a, b) -> float:
    d = b - a
    t = float(np.clip((p - a) @ d / max(float(d @ d), 1e-12), 0.0, 1.0))
    return float(np.linalg.norm(p - (a + t * d)))


# --------------------------------------------------------------------------
# Plotter — те, що Viewport кличе в pyvista, але на foamview
# --------------------------------------------------------------------------

_VIEWS = {
    # напрямок «від фокуса до камери», «угору» — як у pyvista
    "xy": ((0.0, 0.0, 1.0), (0.0, 1.0, 0.0)),
    "xz": ((0.0, -1.0, 0.0), (0.0, 0.0, 1.0)),
    "yz": ((1.0, 0.0, 0.0), (0.0, 0.0, 1.0)),
    "iso": ((1.0, 1.0, 1.0), (0.0, 0.0, 1.0)),
}


class Plotter:
    """Сцена й камера поверх foamview з іменами з pyvista/VTK."""

    def __init__(self, parent=None, level: Level | None = None, on_level=None,
                 font: str | None = None):
        """level — хто малює (select_level()); типово GPU, як вибере wgpu.
        on_level(Level) — коли віджет сам перейшов на інший рівень (GPU
        відмовив під час роботи → CPU). font — файл шрифту TrueType
        (типово — font_path())."""
        from fs_view3d_qt import GpuWidget
        self.level = level or Level("gpu")
        self.font = font if font and os.path.isfile(font) else None
        self.on_level = on_level
        self.camera = Camera()
        self.camera.changed = self._dirty
        self.renderer = _Renderer(self)
        self.render_window = _RenderWindow(self)
        self.actors: dict[str, Actor] = {}
        self._axes = False
        self._bg = ((0.227, 0.255, 0.286), (0.133, 0.149, 0.169))
        self._size_override = None
        self.interactor = GpuWidget(self, parent)
        self.app_window = self.interactor
        self._closed = False

    # --- GPU ---------------------------------------------------------------
    @property
    def gpu(self):
        return self.interactor.gpu

    def _dirty(self):
        if not self._closed:
            self.interactor.update()

    @property
    def window_size(self):
        if self._size_override is not None:
            return list(self._size_override)
        w, h = self.interactor.pixel_size()
        return [w, h]

    @window_size.setter
    def window_size(self, wh):
        w, h = int(wh[0]), int(wh[1])
        self._size_override = (w, h)
        self.interactor.set_pixel_size(w, h)

    def view_proj(self) -> np.ndarray:
        w, h = self.window_size
        self.camera.reset_clipping(self.scene_bounds())
        return self.camera.projection_matrix(w / max(h, 1)) @ self.camera.view_matrix()

    def matrices(self):
        """(view_proj, view, perspective, triad) для foamview, рядками."""
        vp = self.view_proj()
        view = self.camera.view_matrix()
        triad = None
        if self._axes:
            rot = np.eye(4)
            rot[:3, :3] = view[:3, :3]
            ortho = np.diag([1.0, 1.0, -0.5, 1.0])
            ortho[2, 3] = 0.5
            triad = list((ortho @ rot).astype(np.float32).ravel())
        return (list(vp.astype(np.float32).ravel()),
                list(view.astype(np.float32).ravel()),
                not self.camera.parallel_projection, triad)

    def scene_bounds(self):
        bs = [a.GetBounds() for a in self.actors.values()
              if a.mesh is not None and a._visible and a.kind != "text"]
        if not bs:
            return None
        b = np.asarray(bs, float)
        return (b[:, 0].min(), b[:, 1].max(), b[:, 2].min(), b[:, 3].max(),
                b[:, 4].min(), b[:, 5].max())

    # --- шари --------------------------------------------------------------
    def _set_visible(self, name, on):
        a = self.actors.get(name)
        if a is not None and self.gpu is not None:
            for p in a.parts:
                self.gpu.set_visible(p, on)
        self._dirty()

    def _upload(self, a: Actor):
        g = self.gpu
        if g is None:
            return                          # покладемо, щойно зʼявиться GPU
        for p in a.parts:
            g.remove(p)
        a._field = None
        st = a.style
        rgb = hex_rgb(st.get("color", "#b9c4cf"))
        alpha = float(st.get("opacity", 1.0))
        col = (*rgb, alpha)
        # Поле — лише там, де рушій уміє (старий foamview.pyd — одним кольором).
        fld = None
        if st.get("scalars") is not None and hasattr(g, "set_scalar_triangles"):
            fld = _field(a.mesh, st["scalars"], st.get("preference", "point"))
            if fld is not None and a._values is not None and len(a._values) == len(fld[0]):
                fld = (a._values, fld[1])
        if a.kind == "surface":
            cells = fld is not None and fld[1] == "cell"
            tri = _triangles(a.mesh, cells=cells)
            if tri is not None:
                mode = ("overlay" if st.get("overlay") else
                        "transparent" if alpha < 0.999 else "opaque")
                lighting = bool(st.get("lighting", True))
                if fld is not None:
                    pts, tris, cid = tri
                    v = fld[0] if cid is None else fld[0][cid]
                    normals = None
                    if not cells and lighting and st.get("smooth"):
                        normals = _point_normals(pts, tris)
                    g.set_scalar_triangles(
                        a.name, pts, tris, np.ascontiguousarray(v, np.float32), st["lut"],
                        tuple(map(float, st["clim"])), cell=cells, normals=normals,
                        opacity=alpha, nan_color=st.get("nan_color", NAN_COLOR), mode=mode,
                        lighting=lighting, specular=float(st.get("specular", 0.0)),
                        ambient=FIELD_AMBIENT)
                    a._field = {"where": fld[1], "cid": cid}
                else:
                    g.set_triangles(a.name, tri[0], tri[1], col, mode=mode, lighting=lighting,
                                    specular=float(st.get("specular", 0.0)))
                if st.get("show_edges"):
                    seg = _segments(a.mesh, wireframe=True)
                    if seg is not None:
                        g.set_lines(a.name + EDGES, seg,
                                    (*hex_rgb(st.get("edge_color", "#000000")), 1.0),
                                    width=1.0, overlay=True)
        elif a.kind == "lines":
            got = _segments(a.mesh, wireframe=bool(st.get("wireframe")), field=fld)
            if isinstance(got, tuple):
                g.set_scalar_lines(a.name, got[0], got[1], st["lut"],
                                   tuple(map(float, st["clim"])),
                                   width=float(st.get("line_width", 1.0)),
                                   overlay=bool(st.get("overlay")), opacity=alpha,
                                   nan_color=st.get("nan_color", NAN_COLOR))
                a._field = {"where": "segments", "cid": None}
            elif got is not None:
                g.set_lines(a.name, got, col, width=float(st.get("line_width", 1.0)),
                            overlay=bool(st.get("overlay")))
        elif a.kind == "points":
            seg = _points(a.mesh)
            if seg is not None:
                g.set_lines(a.name, seg, col, width=float(st.get("point_size", 5.0)),
                            overlay=True)
        elif a.kind == "text":
            # Нові ключі — лише коли потрібні: старий foamview.pyd їх не знає.
            extra = {k: True for k in ("viewport", "right") if st.get(k)}
            g.set_labels(a.name, st["anchors"], [str(tr(t)) for t in st["texts"]],
                         col,
                         size=int(st.get("size", 14)), screen=bool(st.get("screen")),
                         center=bool(st.get("center")),
                         offset=tuple(st.get("offset", (0.0, 0.0))), **extra)
        for p in a.parts:
            g.set_visible(p, a._visible)

    def _upload_bar(self, bar: "ScalarBar"):
        """Шкала: смуга з таблицею кольорів, позначки, заголовок."""
        g = self.gpu
        if g is None:
            return
        for p in bar.parts:
            g.remove(p)
        st = bar.actor.style
        if not bar._visible or "lut" not in st or not hasattr(g, "set_scalar_triangles"):
            return
        x, y, w, h = bar.box
        lo, hi = (float(c) for c in st["clim"])
        col = (*bar.color, 1.0)
        quad = np.array([[0, 1, 2], [0, 2, 3]], np.uint32)
        ticks = bar.ticks()
        n = len(ticks)
        if bar.vertical:
            # Смуга — ліва частина рамки; позначки праворуч, заголовок над
            # рамкою з правим краєм на її правому краї (рамка стоїть біля
            # правого краю вікна, і текст по центру вилазив би за нього).
            x1 = x + 0.4 * w
            pts = np.array([[x, y, 0], [x1, y, 0], [x1, y + h, 0], [x, y + h, 0]], np.float32)
            vals = np.array([lo, lo, hi, hi], np.float32)
            anchors = [(x1, y + h * i / (n - 1), 0.0) for i in range(n)]
            g.set_labels(bar.parts[1], anchors, ticks, col, size=bar.label_size,
                         viewport=True, offset=(4.0, -0.62 * bar.label_size))
            g.set_labels(bar.parts[2], [(x + w, y + h, 0.0)], [str(tr(bar.title)).rstrip()], col,
                         size=bar.title_size, viewport=True, right=True,
                         offset=(0.0, -1.25 * bar.title_size - 6.0))
        else:
            # Смуга — верхня частина рамки; позначки під нею, заголовок над.
            y0 = y + 0.35 * h
            pts = np.array([[x, y0, 0], [x + w, y0, 0], [x + w, y + h, 0], [x, y + h, 0]],
                           np.float32)
            vals = np.array([lo, hi, hi, lo], np.float32)
            anchors = [(x + w * i / (n - 1), y0, 0.0) for i in range(n)]
            g.set_labels(bar.parts[1], anchors, ticks, col, size=bar.label_size,
                         viewport=True, center=True, offset=(0.0, 3.0))
            g.set_labels(bar.parts[2], [(x + 0.5 * w, y + h, 0.0)], [str(tr(bar.title)).rstrip()], col,
                         size=bar.title_size, viewport=True, center=True,
                         offset=(0.0, -1.25 * bar.title_size - 4.0))
        g.set_scalar_triangles(bar.parts[0], pts, quad, vals, st["lut"], (lo, hi),
                               lighting=False, screen=True)

    def upload_all(self):
        for a in self.actors.values():
            if a.kind == "group":
                for sub in a.children:
                    self._upload(sub)
            else:
                self._upload(a)
        for bar in self.scalar_bars.values():
            self._upload_bar(bar)
        for wd in self.widgets:
            self._upload_widget(wd)

    def _add(self, a: Actor, reset_camera=False) -> Actor:
        old = self.actors.get(a.name)
        if old is not None:
            self.remove_actor(old, render=False)
        self.actors[a.name] = a
        self._upload(a)
        if reset_camera:
            self.reset_camera()
        self._dirty()
        return a

    def add_mesh(self, mesh, color=None, opacity=1.0, style=None, line_width=None,
                 name=None, reset_camera=False, smooth_shading=False, specular=0.0,
                 point_size=None, render_points_as_spheres=False, show_edges=False,
                 render_lines_as_tubes=False, lighting=True, scalars=None, cmap=None,
                 show_scalar_bar=True, scalar_bar_args=None, render=True, clim=None,
                 n_colors=256, preference="point", nan_color=None, edge_color=None,
                 flip_scalars=False, **_ignored):
        name = name or f"actor{len(self.actors)}"
        m = _as_polydata(mesh)
        st = dict(color=color or "#b9c4cf", opacity=opacity, specular=specular,
                  lighting=lighting, smooth=bool(smooth_shading),
                  show_edges=bool(show_edges), edge_color=edge_color or "#000000")
        if point_size is not None and not _has_faces(m) and not m.n_lines:
            kind = "points"
            st["point_size"] = point_size
        elif style == "wireframe":
            kind = "lines"
            st.update(wireframe=True, line_width=line_width or 1.0)
        elif m.n_lines and not _has_faces(m):
            kind = "lines"
            st["line_width"] = line_width or 1.0
        elif point_size is not None and m.n_verts and not _has_faces(m):
            kind = "points"
            st["point_size"] = point_size
        else:
            kind = "surface"
        fld = _field(m, scalars, preference) if scalars is not None else None
        if fld is not None:
            # Межі й таблиця — раз, при додаванні (як у pyvista: нові дані
            # мапера шкалу не міняють).
            lut = lut_colors(cmap, n_colors)
            if flip_scalars:
                lut = np.ascontiguousarray(lut[::-1])
            st.update(scalars=scalars if isinstance(scalars, str) else fld[0],
                      preference=fld[1], lut=lut,
                      clim=tuple(map(float, clim)) if clim is not None else _clim(fld[0]))
            if nan_color is not None:
                st["nan_color"] = (*hex_rgb(nan_color), 1.0)
        a = self._add(Actor(self, name, m, kind, st), reset_camera=reset_camera)
        if fld is not None and show_scalar_bar:
            args = dict(scalar_bar_args or {})
            title = args.pop("title", None) or (scalars if isinstance(scalars, str) else "")
            self.add_scalar_bar(title=title, mapper=a.mapper, render=False, **args)
        return a

    # --- шкали -------------------------------------------------------------
    @property
    def scalar_bars(self) -> dict:
        if not hasattr(self, "_bars"):
            self._bars: dict[str, ScalarBar] = {}
        return self._bars

    def add_scalar_bar(self, title="", mapper=None, n_labels=5, fmt=None, vertical=None,
                       position_x=None, position_y=None, width=None, height=None,
                       color=None, title_font_size=None, label_font_size=None,
                       render=True, **_ignored):
        """Шкала кольорів актора `mapper` (або останнього актора з полем)."""
        actor = getattr(mapper, "_a", None)
        if actor is None:
            actor = next((a for a in reversed(list(self.actors.values()))
                          if "lut" in a.style), None)
        if actor is None:
            return None
        title = str(title)
        old = self.scalar_bars.pop(title, None)
        if old is not None and self.gpu is not None:
            for p in old.parts:
                self.gpu.remove(p)
        bar = ScalarBar(self, title, actor, vertical=bool(vertical), position_x=position_x,
                        position_y=position_y, width=width, height=height,
                        n_labels=n_labels, fmt=fmt, color=color,
                        title_font_size=title_font_size, label_font_size=label_font_size)
        self.scalar_bars[title] = bar
        self._upload_bar(bar)
        self._dirty()
        return bar

    def remove_scalar_bar(self, title=None, render=True):
        if title is None:
            titles = list(self.scalar_bars)
        else:
            titles = [title]
        for t in titles:
            bar = self.scalar_bars.pop(t, None)
            if bar is not None and self.gpu is not None:
                for p in bar.parts:
                    self.gpu.remove(p)
        if render:
            self._dirty()

    def add_point_labels(self, points, labels, text_color="white", font_size=14,
                         name=None, shape=None, always_visible=True, **_ignored):
        import pyvista as pv
        pts = np.asarray(points, float).reshape(-1, 3)
        name = name or f"labels{len(self.actors)}"
        st = dict(color=text_color, anchors=[tuple(map(float, p)) for p in pts],
                  texts=[t if isinstance(t, str) else str(t) for t in labels],
                  size=int(font_size * 1.1),
                  offset=(8.0, -8.0))
        return self._add(Actor(self, name, pv.PolyData(pts), "text", st))

    # Куточки й краї вікна для add_text: (x, y) частки вікна від лівого
    # нижнього кута; вирівнювання; верх тексту біля y чи низ.
    _CORNERS = {
        "lower_left": ((0.01, 0.01), "left", "bottom"),
        "lower_right": ((0.99, 0.01), "right", "bottom"),
        "upper_right": ((0.99, 0.99), "right", "top"),
        "upper_edge": ((0.5, 0.99), "center", "top"),
        "lower_edge": ((0.5, 0.01), "center", "bottom"),
    }

    def add_text(self, text, position="upper_left", font_size=12, color="white",
                 name=None, viewport=False, **_ignored):
        """Напис у вікні. position — куточок ("upper_left", …) або (x, y):
        частки вікна (viewport=True) чи пікселі від лівого нижнього кута;
        (x, y) — лівий нижній кут тексту, як у VTK."""
        name = name or f"text{len(self.actors)}"
        size = int(font_size * 1.3)
        lines = max(1, str(text).count("\n") + 1)
        # A TrStr stays a TrStr: the draw translates it into the language of
        # the moment (a live change of the language, T28).
        st = dict(color=color, texts=[text if isinstance(text, str)
                                      else str(text)], size=size)
        if isinstance(position, str) and position in self._CORNERS:
            (x, y), align, edge = self._CORNERS[position]
            st.update(anchors=[(x, y, 0.0)], viewport=True, center=align == "center",
                      right=align == "right",
                      offset=(0.0, -1.25 * size * lines if edge == "bottom" else 0.0))
        elif isinstance(position, (tuple, list)) and len(position) >= 2:
            x, y = float(position[0]), float(position[1])
            if not viewport:
                w, h = self.window_size
                x, y = x / max(w, 1), y / max(h, 1)
            st.update(anchors=[(x, y, 0.0)], viewport=True,
                      offset=(0.0, -1.25 * size * lines))
        else:                                   # "upper_left" та невідоме
            st.update(anchors=[(8.0, 8.0, 0.0)], screen=True)
        return self._add(Actor(self, name, None, "text", st))

    def add_ruler(self, pointa, pointb, title="", font_size_factor=0.5,
                  label_format="%.3g", number_labels=5, **_ignored):
        """Лінійка: лінія, засічки й підписи (як pyvista add_ruler)."""
        import pyvista as pv
        a = np.asarray(pointa, float)
        b = np.asarray(pointb, float)
        name = f"ruler_{title or len(self.actors)}"
        length = float(np.linalg.norm(b - a))
        line = pv.Line(a, b)
        group = Actor(self, name, line, "group", {})
        ticks, texts = [], []
        n = max(int(number_labels), 2)
        for i in range(n):
            t = i / (n - 1)
            ticks.append(tuple(a + (b - a) * t))
            texts.append(label_format % (length * t))
        if title:
            ticks.append(tuple(b + (b - a) * 0.06))
            texts.append(title)
        line_actor = Actor(self, name + "_line", line, "lines",
                           dict(color="#e8e8e8", line_width=2.0))
        text_actor = Actor(self, name + "_text", pv.PolyData(np.asarray(ticks)), "text",
                           dict(color="#e8e8e8", anchors=ticks, texts=texts, size=12,
                                center=True, offset=(0.0, 4.0)))
        group.children = [line_actor, text_actor]
        group.parts = [line_actor.name, text_actor.name]
        old = self.actors.get(name)
        if old is not None:
            self.remove_actor(old, render=False)
        self.actors[name] = group
        for c in group.children:
            self._upload(c)
        self._dirty()
        return group

    def add_axes(self, **_ignored):
        self._axes = True
        self._dirty()

    def set_background(self, color, top=None):
        bottom = hex_rgb(color)
        self._bg = (hex_rgb(top) if top else bottom, bottom)
        if self.gpu is not None:
            self.gpu.set_background(*self._bg)
        self._dirty()

    def remove_actor(self, actor, render=True, reset_camera=False):
        items = actor if isinstance(actor, (list, tuple)) else [actor]
        for it in items:
            name = it if isinstance(it, str) else getattr(it, "name", None)
            a = self.actors.pop(name, None)
            if a is not None and self.gpu is not None:
                for p in a.parts:
                    self.gpu.remove(p)
        if render:
            self._dirty()
        return True

    def clear(self):
        self.actors.clear()
        self.scalar_bars.clear()
        if self.gpu is not None:
            self.gpu.clear()
            for wd in self.widgets:            # ручки живуть далі (як у VTK)
                self._upload_widget(wd)
        self._dirty()

    @property
    def background_color(self):
        import pyvista as pv
        return pv.Color(self._bg[1])

    # --- ручки (DragWidget) ------------------------------------------------
    @property
    def widgets(self) -> list:
        if not hasattr(self, "_widgets"):
            self._widgets: list[DragWidget] = []
        return self._widgets

    def add_drag_widget(self, kind: str, **kw) -> DragWidget:
        """Ручка, яку тягнуть мишею (див. DragWidget). Вмикає її On()."""
        return DragWidget(self, kind, **kw)

    def _add_widget(self, wd):
        if wd not in self.widgets:
            self.widgets.append(wd)
        self._upload_widget(wd)
        self._dirty()

    def _remove_widget(self, wd):
        if wd in self.widgets:
            self.widgets.remove(wd)
        if self.gpu is not None:
            for p in wd.parts:
                self.gpu.remove(p)
        self._dirty()

    def _upload_widget(self, wd):
        if self.gpu is not None:
            wd.draw(self.gpu)

    def _dpr(self) -> float:
        """Фізичних пікселів кадру на логічний піксель віджета."""
        return self.window_size[0] / max(self.interactor.width(), 1)

    def _to_screen(self, p) -> np.ndarray:
        """Світова точка → пікселі кадру від лівого ВЕРХНЬОГО кута."""
        w, h = self.window_size
        c = self.view_proj() @ np.array([p[0], p[1], p[2], 1.0])
        cw = c[3] if abs(c[3]) > 1e-300 else 1e-300
        return np.array([(c[0] / cw + 1) * 0.5 * w, (1 - c[1] / cw) * 0.5 * h])

    def _screen_right(self) -> np.ndarray:
        c = self.camera
        return _unit(np.cross(c._foc - c._pos, c._up))

    def _screen_to_world_delta(self, at, d_xy) -> np.ndarray:
        """Зсув курсора на d_xy пікселів кадру (y униз) → зсув у світі в
        площині екрана, що проходить через точку `at`."""
        c = self.camera
        h = max(self.window_size[1], 1)
        f = _unit(c._foc - c._pos)
        if c.parallel_projection:
            per = 2.0 * c.parallel_scale / h
        else:
            depth = max(float((np.asarray(at, float) - c._pos) @ f), 1e-9)
            per = 2.0 * depth * math.tan(math.radians(c.view_angle) / 2.0) / h
        return (self._screen_right() * float(d_xy[0]) - c._up * float(d_xy[1])) * per

    def widget_press(self, xy, button: str) -> bool:
        """Миша натиснута в `xy` (пікселі кадру): чи взяла її ручка."""
        for wd in reversed(self.widgets):
            grab = wd.pick(xy, button)
            if grab:
                self._grabbed = wd
                wd.press(grab)
                return True
        return False

    def widget_move(self, d_xy, xy) -> bool:
        wd = getattr(self, "_grabbed", None)
        if wd is None:
            return False
        wd.move(d_xy, xy)
        return True

    def widget_release(self) -> bool:
        wd = getattr(self, "_grabbed", None)
        self._grabbed = None
        if wd is None:
            return False
        wd.release()
        return True

    def reset_camera_clipping_range(self):
        self.camera.reset_clipping(self.scene_bounds())

    # Інтерактора VTK тут немає: віджети VTK (площина зрізу, насіння ліній)
    # дивляться на `iren` і без нього не вмикаються.
    iren = None

    # --- камера ------------------------------------------------------------
    @property
    def camera_position(self):
        c = self.camera
        return [c.position, c.focal_point, c.up]

    @camera_position.setter
    def camera_position(self, cp):
        pos, foc, up = cp
        c = self.camera
        c._pos = np.asarray(pos, float).copy()
        c._foc = np.asarray(foc, float).copy()
        c._up = _unit(up)
        c._orthogonalize()
        self._dirty()

    @property
    def camera_set(self):
        return self.renderer.camera_set

    @camera_set.setter
    def camera_set(self, v):
        self.renderer.camera_set = bool(v)

    def reset_camera(self, render=True, bounds=None):
        """Як vtkRenderer.ResetCamera: бік погляду той самий, сцена вписана."""
        b = bounds if bounds is not None else self.scene_bounds()
        if b is None:
            return
        b = np.asarray(b, float)
        lo, hi = b[0::2], b[1::2]
        centre = (lo + hi) / 2
        radius = 0.5 * float(np.linalg.norm(hi - lo)) or 1.0
        c = self.camera
        d = c._pos - c._foc
        if float(np.linalg.norm(d)) < 1e-300:
            d = np.array([0.0, 0.0, 1.0])
        d = _unit(d)
        dist = radius / math.sin(math.radians(c.view_angle) / 2.0)
        c._foc = centre
        c._pos = centre + d * dist
        c.parallel_scale = radius
        c._orthogonalize()
        self.renderer.camera_set = True
        self._dirty()

    def _view(self, which, negative=False):
        vec, up = _VIEWS[which]
        v = -np.asarray(vec, float) if negative else np.asarray(vec, float)
        c = self.camera
        dist = c.GetDistance() or 1.0
        c._pos = c._foc + _unit(v) * dist
        c._up = np.asarray(up, float)
        c._orthogonalize()
        self.reset_camera()

    def view_isometric(self, negative=False, render=True, bounds=None):
        self._view("iso", negative)

    def view_xy(self, negative=False, render=True, bounds=None):
        self._view("xy", negative)

    def view_xz(self, negative=False, render=True, bounds=None):
        self._view("xz", negative)

    def view_yz(self, negative=False, render=True, bounds=None):
        self._view("yz", negative)

    # --- кадр --------------------------------------------------------------
    def render(self):
        self.interactor.render_now()

    def screenshot(self, filename=None, transparent_background=False,
                   return_img=True, window_size=None):
        img = self.interactor.grab_image()
        if img is None:
            return None
        rgb = img[..., :3].copy()
        if filename:
            from PIL import Image
            Image.fromarray(rgb).save(filename)
        return rgb if return_img else None

    def close(self):
        self._closed = True
        self.interactor.release()


if __name__ == "__main__":                 # проба рівня в дочірньому процесі
    import sys
    if sys.argv[1:2] == ["--probe"]:
        sys.exit(_probe_main(sys.argv[2:]))
