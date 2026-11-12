"""Background loader of the timeline player (T40).

One worker thread per set of frames (`fs_anim.Loader`): it makes the loader
(the geometry: the body, the slice), sends it to the window, and then loads
the frames one by one, in the order that the window gives (`want`). The window
gives a new order each time the person goes to another frame, so the frame on
the screen always comes next. The window shows each frame as soon as the
signal `loaded` arrives.

The worker never touches the scene: it only fills the arrays of the frames.
"""

from __future__ import annotations

import threading
import traceback

from PySide6 import QtCore


class _Worker(QtCore.QObject):
    ready = QtCore.Signal(object)          # fs_anim.Loader
    loaded = QtCore.Signal(int)
    failed = QtCore.Signal(str)
    idle = QtCore.Signal()                 # all wanted frames are here

    def __init__(self, make):
        super().__init__()
        self._make = make
        self._cv = threading.Condition()
        self._order: list[int] = []
        self._stop = False
        self.loader = None

    def want(self, order) -> None:
        with self._cv:
            self._order = [int(i) for i in order]
            self._cv.notify()

    def stop(self) -> None:
        with self._cv:
            self._stop = True
            self._cv.notify()

    def _next(self):
        with self._cv:
            while not self._stop:
                ld = self.loader
                while self._order:
                    i = self._order.pop(0)
                    if ld is not None and i not in ld.done:
                        return i
                self.idle.emit()
                self._cv.wait()
            return None

    @QtCore.Slot()
    def run(self) -> None:
        try:
            ld = self._make()
        except Exception as exc:                          # noqa: BLE001
            self.failed.emit(str(exc) or traceback.format_exc(limit=2))
            return
        self.loader = ld
        self.ready.emit(ld)
        while True:
            i = self._next()
            if i is None:
                return
            try:
                ld.load([i])
            except Exception as exc:                      # noqa: BLE001
                self.failed.emit(str(exc) or traceback.format_exc(limit=2))
                return
            self.loaded.emit(i)


class FrameLoader(QtCore.QObject):
    """Owns the worker thread. The window connects to the signals."""

    ready = QtCore.Signal(object)
    loaded = QtCore.Signal(int)
    failed = QtCore.Signal(str)
    idle = QtCore.Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self._thread: QtCore.QThread | None = None
        self._worker: _Worker | None = None
        self._retired: list = []          # stopped threads that still end
        self._gen = 0

    @property
    def busy(self) -> bool:
        return self._thread is not None

    def threads(self) -> list:
        """Each QThread of this loader that can still run (for the tests)."""
        out = [t for t, _ in self._retired]
        return out + ([self._thread] if self._thread is not None else [])

    def start(self, make) -> None:
        """A new set of frames: stop the old worker, start a new one.
        `make()` runs in the worker thread and returns a fs_anim.Loader."""
        self.stop()
        self._gen += 1
        gen = self._gen
        w = _Worker(make)
        t = QtCore.QThread(self)
        t.setObjectName("timeline-frames")
        w.moveToThread(t)
        t.started.connect(w.run)
        # Signals of an old worker that arrive after a restart are dropped.
        w.ready.connect(lambda ld, g=gen: g == self._gen and self.ready.emit(ld))
        w.loaded.connect(lambda i, g=gen: g == self._gen and self.loaded.emit(i))
        w.failed.connect(lambda m, g=gen: g == self._gen and self.failed.emit(m))
        w.idle.connect(lambda g=gen: g == self._gen and self.idle.emit())
        self._worker, self._thread = w, t
        t.start()

    def want(self, order) -> None:
        if self._worker is not None:
            self._worker.want(order)

    def stop(self) -> None:
        """Stop the worker and do not wait: a frame that loads now finishes
        in its thread, and its signals are dropped. `close` waits."""
        w, t = self._worker, self._thread
        self._worker = self._thread = None
        self._gen += 1
        if w is not None:
            w.stop()
        if t is not None:
            t.quit()
            self._retired.append((t, w))
            t.finished.connect(self._reap)

    def _reap(self) -> None:
        self._retired = [(t, w) for t, w in self._retired if t.isRunning()]

    def close(self, wait_ms: int = 20000) -> None:
        """At the end of the window: stop and wait for each thread."""
        self.stop()
        for t, _w in self._retired:
            t.wait(wait_ms)
        self._retired = []
