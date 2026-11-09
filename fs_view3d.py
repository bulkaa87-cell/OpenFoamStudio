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


_PROBE: tuple[bool, str] | None = None


def probe() -> tuple[bool, str]:
    """Чи справді малює GPU: один кадр 32×24 без вікна, тло відоме.

    Модуль може бути, а адаптера чи пристрою — ні; а WARP (програмний DX12)
    віддає чорний кадр без жодної помилки. Тоді вкладка «3D» бере VTK, а не
    показує «3D недоступне». Результат памʼятається на весь запуск.
    """
    global _PROBE
    if _PROBE is not None:
        return _PROBE
    if _fv is None:
        _PROBE = (False, "foamview не зібрано")
        return _PROBE
    try:
        r = _fv.Renderer(None, 32, 24, None)
        r.set_background((0.8, 0.2, 0.1), (0.8, 0.2, 0.1))
        eye = [1.0, 0, 0, 0, 0, 1.0, 0, 0, 0, 0, 1.0, 0, 0, 0, 0, 1.0]
        img = r.screenshot(eye, eye, True, None)
        px = np.asarray(img)[12, 16, :3].astype(int)
        ok = abs(px[0] - 204) < 12 and abs(px[1] - 51) < 12 and abs(px[2] - 25) < 12
        _PROBE = (bool(ok), f"{r.adapter}" if ok else
                  f"{r.adapter}: кадр не той ({px.tolist()} замість [204, 51, 25])")
    except BaseException as exc:                          # noqa: BLE001
        if not gpu_failed(exc):
            raise
        _PROBE = (False, f"GPU: {exc}")
    return _PROBE


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
    """GetInput() і зсув глибини: Viewport вмикає його для підсвітки, що
    лежить РІВНО на поверхні тіла. Тут це — шар-накладка в foamview."""

    def __init__(self, actor):
        self._a = actor

    def GetInput(self):
        return self._a.mesh

    def SetResolveCoincidentTopologyToPolygonOffset(self):
        self._a.set_overlay(True)

    def SetRelativeCoincidentTopologyPolygonOffsetParameters(self, *_a):
        self._a.set_overlay(True)

    def SetRelativeCoincidentTopologyLineOffsetParameters(self, *_a):
        self._a.set_overlay(True)


class Actor:
    """Шар у сцені — з тим, що в акторі VTK питають вікно й тести."""

    def __init__(self, view, name: str, mesh, kind: str, style: dict | None = None):
        self._view = view
        self.name = name
        self.mesh = mesh
        self.kind = kind
        self.style = dict(style or {})
        self.parts: list[str] = [name]      # імена шарів у foamview
        self._visible = True

    def set_overlay(self, on: bool):
        if bool(self.style.get("overlay")) != bool(on):
            self.style["overlay"] = bool(on)
            self._view._upload(self)

    def GetBounds(self):
        return tuple(self.mesh.bounds) if self.mesh is not None else (0,) * 6

    def GetMapper(self):
        return _Mapper(self)

    def SetVisibility(self, on):
        self._visible = bool(on)
        self._view._set_visible(self.name, self._visible)

    def GetVisibility(self):
        return self._visible

    VisibilityOn = lambda self: self.SetVisibility(True)      # noqa: E731
    VisibilityOff = lambda self: self.SetVisibility(False)    # noqa: E731


# --------------------------------------------------------------------------
# Геометрія pyvista → масиви для foamview
# --------------------------------------------------------------------------

def _has_faces(m) -> bool:
    """Чи є в сітці грані (полігони) — без застарілого n_faces_strict."""
    faces = getattr(m, "faces", None)
    return faces is not None and len(faces) > 0


def _triangles(mesh):
    """(points float32 (N,3), tris uint32 (M,3)) поверхні, або None."""
    import pyvista as pv
    if mesh is None or not mesh.n_points:
        return None
    m = mesh if isinstance(mesh, pv.PolyData) else mesh.extract_surface(
        algorithm="dataset_surface")
    if not _has_faces(m):
        return None
    if not m.is_all_triangles:
        m = m.triangulate()
    faces = np.asarray(m.faces).reshape(-1, 4)[:, 1:]
    return (np.ascontiguousarray(m.points, np.float32),
            np.ascontiguousarray(faces, np.uint32))


def _segments(mesh, wireframe: bool = False):
    """Відрізки (M,6) float32: лінії сітки, а для wireframe — ребра граней."""
    import pyvista as pv
    if mesh is None or not mesh.n_points:
        return None
    m = mesh if isinstance(mesh, pv.PolyData) else mesh.extract_surface(
        algorithm="dataset_surface")
    if wireframe and _has_faces(m):
        m = m.extract_all_edges()
    pts = np.asarray(m.points, np.float32)
    conn = np.asarray(m.lines)
    segs, i = [], 0
    while i < len(conn):
        n = int(conn[i])
        ids = conn[i + 1:i + 1 + n]
        if n >= 2:
            segs.append(np.column_stack([pts[ids[:-1]], pts[ids[1:]]]))
        i += n + 1
    if not segs:
        return None
    return np.ascontiguousarray(np.vstack(segs), np.float32)


def _points(mesh):
    """Точки як відрізки нульової довжини (foamview малює їх квадратиками)."""
    if mesh is None or not mesh.n_points:
        return None
    p = np.asarray(mesh.points, np.float32)
    return np.ascontiguousarray(np.column_stack([p, p]), np.float32)


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

    def __init__(self, parent=None):
        from fs_view3d_qt import GpuWidget
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
        st = a.style
        rgb = hex_rgb(st.get("color", "#b9c4cf"))
        alpha = float(st.get("opacity", 1.0))
        col = (*rgb, alpha)
        if a.kind == "surface":
            tri = _triangles(a.mesh)
            if tri is not None:
                mode = ("overlay" if st.get("overlay") else
                        "transparent" if alpha < 0.999 else "opaque")
                g.set_triangles(a.name, tri[0], tri[1], col, mode=mode,
                                lighting=bool(st.get("lighting", True)),
                                specular=float(st.get("specular", 0.0)))
        elif a.kind == "lines":
            seg = _segments(a.mesh, wireframe=bool(st.get("wireframe")))
            if seg is not None:
                g.set_lines(a.name, seg, col, width=float(st.get("line_width", 1.0)),
                            overlay=bool(st.get("overlay")))
        elif a.kind == "points":
            seg = _points(a.mesh)
            if seg is not None:
                g.set_lines(a.name, seg, col, width=float(st.get("point_size", 5.0)),
                            overlay=True)
        elif a.kind == "text":
            g.set_labels(a.name, st["anchors"], st["texts"], col,
                         size=int(st.get("size", 14)), screen=bool(st.get("screen")),
                         center=bool(st.get("center")),
                         offset=tuple(st.get("offset", (0.0, 0.0))))
        for p in a.parts:
            g.set_visible(p, a._visible)

    def upload_all(self):
        for a in self.actors.values():
            if a.kind == "group":
                for sub in a.children:
                    self._upload(sub)
            else:
                self._upload(a)

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
                 show_scalar_bar=True, scalar_bar_args=None, render=True, **_ignored):
        import pyvista as pv
        name = name or f"actor{len(self.actors)}"
        m = mesh if isinstance(mesh, pv.DataSet) else pv.wrap(mesh)
        if not isinstance(m, pv.PolyData):
            # extract_cells / підсвітка граней дає UnstructuredGrid: малюємо
            # його поверхню (VTK робить так само).
            m = m.extract_surface(algorithm="dataset_surface")
        st = dict(color=color or "#b9c4cf", opacity=opacity, specular=specular,
                  lighting=lighting)
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
        return self._add(Actor(self, name, m, kind, st), reset_camera=reset_camera)

    def add_point_labels(self, points, labels, text_color="white", font_size=14,
                         name=None, shape=None, always_visible=True, **_ignored):
        import pyvista as pv
        pts = np.asarray(points, float).reshape(-1, 3)
        name = name or f"labels{len(self.actors)}"
        st = dict(color=text_color, anchors=[tuple(map(float, p)) for p in pts],
                  texts=[str(t) for t in labels], size=int(font_size * 1.1),
                  offset=(8.0, -8.0))
        return self._add(Actor(self, name, pv.PolyData(pts), "text", st))

    def add_text(self, text, position="upper_left", font_size=12, color="white",
                 name=None, **_ignored):
        name = name or f"text{len(self.actors)}"
        st = dict(color=color, anchors=[(8.0, 8.0, 0.0)], texts=[str(text)],
                  size=int(font_size * 1.3), screen=True)
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
        if self.gpu is not None:
            self.gpu.clear()
        self._dirty()

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
