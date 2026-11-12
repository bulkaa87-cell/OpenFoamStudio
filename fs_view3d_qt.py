"""Qt-віджет, у який малює foamview, і миша як у VTK (trackball camera).

Два режими:
  * НАТИВНИЙ — віджет має власне вікно (HWND), і wgpu малює просто в
    нього: Qt сам нічого тут не фарбує (WA_PaintOnScreen, paintEngine None);
  * БЕЗ ВІКНА — платформа Qt offscreen (тести) або GPU не дав поверхню:
    foamview малює в текстуру, кадр читається назад і малюється QPainter'ом.
    Повільніше, але чесно той самий рушій.
"""

from __future__ import annotations

import math

import numpy as np
from PySide6 import QtCore, QtGui, QtWidgets

import fs_view3d as v3
from fs_i18n import tr
import fs_i18n

# The Qt text setters translate and keep the key of each text, also in a
# widget that is made before the main window (T28).
fs_i18n.install()

Qt = QtCore.Qt


class GpuWidget(QtWidgets.QWidget):
    def __init__(self, plotter, parent=None):
        super().__init__(parent)
        self.pl = plotter
        self.gpu = None
        self.error = ""
        # Нативне вікно — лише там, де foamview уміє поверхню (Windows,
        # macOS); на Linux, в offscreen-платформі тестів і для CPU-рівня —
        # малюємо в памʼять і показуємо кадр QPainter'ом.
        self._kind = v3.window_kind()
        self._native = (self._kind is not None and QtGui.QGuiApplication.platformName()
                        not in ("offscreen", "minimal")
                        and plotter.level.level != "cpu")
        self._retried = False
        if self._native:
            self.setAttribute(Qt.WA_NativeWindow, True)
            self.setAttribute(Qt.WA_PaintOnScreen, True)
            self.setAttribute(Qt.WA_NoSystemBackground, True)
            self.setAttribute(Qt.WA_OpaquePaintEvent, True)
        self.setFocusPolicy(Qt.StrongFocus)
        self.setMinimumSize(80, 60)
        self._override = None
        self._drag = None
        self._last = None

    # --- розмір ------------------------------------------------------------
    def pixel_size(self) -> tuple[int, int]:
        if self._override is not None:
            return self._override
        # Фізичні пікселі і без нативного вікна: кадр у памʼяті (CPU, Linux)
        # малюється 1:1, а не в логічних пікселях з розтягом — інакше на
        # екрані 125 % лінії й краї стають сходинками.
        dpr = self.devicePixelRatioF()
        return (max(1, round(self.width() * dpr)), max(1, round(self.height() * dpr)))

    def set_pixel_size(self, w: int, h: int):
        self._override = (max(1, w), max(1, h))
        if not self._native:
            self.resize(w, h)
        if self.gpu is not None:
            self.gpu.resize(*self._override)
        self.update()

    def sizeHint(self):
        return QtCore.QSize(640, 480)

    # --- GPU ---------------------------------------------------------------
    def _go_offscreen(self):
        self._native = False
        self.setAttribute(Qt.WA_PaintOnScreen, False)

    def _to_cpu(self, why: str):
        """Останній рівень: CPU-растеризатор. Повільно, але картинка є."""
        self.pl.level = v3.Level("cpu", note=why)
        self._go_offscreen()
        if self.pl.on_level is not None:
            self.pl.on_level(self.pl.level)

    def _make(self, w, h, font):
        lv = self.pl.level
        hwnd = int(self.winId()) if self._native else None
        try:
            return v3.make_renderer(lv, hwnd, w, h, font, kind=self._kind or "win32")
        except BaseException as exc:                      # noqa: BLE001
            if not v3.gpu_failed(exc):
                raise
            self.error = str(exc)
        if self._native and lv.level != "cpu" and (lv.backend or lv.adapter):
            # Проба йде БЕЗ вікна, тож поверхню вікна вона не перевіряє.
            # Вибраний бекенд не дав поверхню — у тому ж вікні вибір wgpu
            # (усі бекенди): так було до ланцюжка, і це гладкий кадр, а не
            # off-screen, розтягнутий QPainter'ом.
            try:
                r = v3.make_renderer(v3.Level(lv.level, note=lv.note), hwnd, w, h, font,
                                     kind=self._kind or "win32")
                self.setToolTip(tr("3D: {backend} did not give a window "
                                   "surface ({error}); drawing with "
                                   "{adapter}", backend=lv.backend,
                                   error=self.error, adapter=r.adapter))
                return r
            except BaseException as exc:                  # noqa: BLE001
                if not v3.gpu_failed(exc):
                    raise
                self.error = f"{self.error}; {exc}"
        if self._native:
            # Без поверхні — той самий адаптер, кадр у памʼять.
            self._go_offscreen()
            try:
                return v3.make_renderer(lv, None, w, h, font)
            except BaseException as exc:                  # noqa: BLE001
                if not v3.gpu_failed(exc):
                    raise
                self.error = f"{self.error}; {exc}"
        if lv.level != "cpu":
            self._to_cpu(tr("{text} was not created: {error}", text=lv.text(), error=self.error))
            return v3.make_renderer(self.pl.level, None, w, h, font)
        return None

    def _ensure(self):
        if self.gpu is not None or v3._fv is None:
            return self.gpu
        w, h = self.pixel_size()
        self.gpu = self._make(w, h, getattr(self.pl, "font", None) or v3.font_path())
        if self.gpu is None:
            return None
        if hasattr(self.gpu, "set_scale"):
            self.gpu.set_scale(self.devicePixelRatioF())
        self.gpu.set_background(*self.pl._bg)
        self.pl.upload_all()
        return self.gpu

    def paintEngine(self):
        return None if self._native else super().paintEngine()

    def showEvent(self, e):
        super().showEvent(e)
        self._ensure()
        self.update()

    def resizeEvent(self, e):
        super().resizeEvent(e)
        if self._override is None and self.gpu is not None:
            self.gpu.resize(*self.pixel_size())
        self.update()

    def paintEvent(self, e):
        g = self._ensure()
        if g is None:
            if not self._native:
                p = QtGui.QPainter(self)
                p.fillRect(self.rect(), QtGui.QColor("#22262b"))
                p.setPen(QtGui.QColor("#e8e8e8"))
                p.drawText(self.rect(), Qt.AlignCenter,
                           tr("3D is not available: {v}", v=self.error or tr("no foamview")))
            return
        if self._native:
            self._draw()
        else:
            img = self.grab_image()
            if img is None:
                return
            h, w = img.shape[:2]
            q = QtGui.QImage(img.data, w, h, 4 * w, QtGui.QImage.Format_RGBA8888)
            # Кадр у фізичних пікселях: позначити його dpr, щоб Qt поклав
            # його 1:1, без масштабу.
            q.setDevicePixelRatio(max(w / max(self.width(), 1), 1e-3))
            p = QtGui.QPainter(self)
            p.drawImage(QtCore.QPointF(0, 0), q)
            p.end()

    def _draw(self):
        if self.gpu is None:
            return
        vp, view, persp, triad = self.pl.matrices()
        try:
            self.gpu.render(vp, view, persp, triad)
        except BaseException as exc:                      # noqa: BLE001
            if not v3.gpu_failed(exc):
                raise
            self._gpu_lost(exc)

    def _gpu_lost(self, exc):
        """Помилка GPU / скидання драйвера (TDR): пристрій уже не той.
        Раз — новий рендерер на тому ж адаптері; знову — CPU. Шари сцени
        живуть у Plotter, тож upload_all() відновлює кадр повністю."""
        self.error = str(exc)
        self.setToolTip("3D: " + self.error)
        self.gpu = None
        if self._retried and self.pl.level.level != "cpu":
            self._to_cpu(tr("GPU failed twice: {error}", error=self.error))
        self._retried = True
        self.update()

    def render_now(self):
        if self._native and self.gpu is not None:
            self._draw()
        else:
            self.update()

    def grab_image(self):
        g = self._ensure()
        if g is None:
            return None
        vp, view, persp, triad = self.pl.matrices()
        for _ in range(3):                 # той самий GPU, ще раз, тоді CPU
            try:
                return g.screenshot(vp, view, persp, triad)
            except BaseException as exc:                  # noqa: BLE001
                if not v3.gpu_failed(exc):
                    raise
                self._gpu_lost(exc)
                g = self._ensure()
                if g is None:
                    return None
        return None

    def release(self):
        self.gpu = None

    # --- миша (як vtkInteractorStyleTrackballCamera) ------------------------
    # Ручки сцени (площина зрізу, насіння ліній) беруть мишу першими: їх
    # тягнуть, а не крутять камеру. Координати ручкам — у пікселях кадру.
    def _px(self, pos) -> np.ndarray:
        k = self.pl._dpr()
        return np.array([pos.x() * k, pos.y() * k])

    def mousePressEvent(self, e):
        self.setFocus()
        btn = {Qt.LeftButton: "left", Qt.RightButton: "right"}.get(e.button())
        if (btn and not (e.modifiers() & (Qt.ShiftModifier | Qt.ControlModifier))
                and self.pl.widget_press(self._px(e.position()), btn)):
            self._drag = ("widget", None)
            self._last = e.position()
            return
        self._drag = (e.button(), e.modifiers())
        self._last = e.position()

    def mouseReleaseEvent(self, e):
        if self._drag is not None and self._drag[0] == "widget":
            self.pl.widget_release()
            self.update()
        self._drag = None

    def mouseMoveEvent(self, e):
        if self._drag is None or self._last is None:
            return
        if self._drag[0] == "widget":
            pos = e.position()
            d = self._px(pos) - self._px(self._last)
            self._last = pos
            self.pl.widget_move(d, self._px(pos))
            return
        btn, mods = self._drag
        pos = e.position()
        dx = pos.x() - self._last.x()
        dy = pos.y() - self._last.y()          # униз — додатне (Qt)
        self._last = pos
        w, h = max(self.width(), 1), max(self.height(), 1)
        cam = self.pl.camera
        if btn == Qt.LeftButton and mods & Qt.ControlModifier:
            # Обертання навколо осі погляду: кут між векторами від центру.
            cx, cy = w / 2, h / 2
            a0 = math.atan2(cy - (pos.y() - dy), (pos.x() - dx) - cx)
            a1 = math.atan2(cy - pos.y(), pos.x() - cx)
            cam.roll(math.degrees(a1 - a0))
        elif btn == Qt.MiddleButton or (btn == Qt.LeftButton
                                        and mods & Qt.ShiftModifier):
            self._pan(dx, dy, h)
        elif btn == Qt.LeftButton:
            cam.azimuth(-200.0 * dx / w)
            cam.elevation(200.0 * dy / h)
        elif btn == Qt.RightButton:
            f = 1.1 ** (10.0 * (-dy) / (h / 2))
            cam.dolly(f)
        self.update()

    def _pan(self, dx, dy, h):
        cam = self.pl.camera
        d = cam._foc - cam._pos
        dist = float(np.linalg.norm(d)) or 1.0
        if cam.parallel_projection:
            per = 2.0 * cam.parallel_scale / h
        else:
            per = 2.0 * dist * math.tan(math.radians(cam.view_angle) / 2.0) / h
        right = v3._unit(np.cross(d, cam._up))
        move = (-dx * right + dy * cam._up) * per
        cam._pos = cam._pos + move
        cam._foc = cam._foc + move
        cam._touch()

    def wheelEvent(self, e):
        steps = e.angleDelta().y() / 120.0
        if steps:
            self.pl.camera.dolly(1.1 ** (2.0 * steps))
            self.update()
