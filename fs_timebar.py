"""The timeline of the «Results» tab: a mini player (T40).

One control for the time of the view and for the animation (before T40 they
were a «Time» list and a separate «Animation» group):

    ⇤ ◂ ▶ ▸ ⇥ ───●──────── t = 0.0375 s  25/26   6 fps  ⟲  [▮▮▮ ]  ⚙

* the slider has a tick for each time of the case;
* play / pause, a step back and forward, to the first and the last time;
* the speed (frames per second) and loop;
* a small progress bar while frames load in the background;
* «Full View» goes back from the fast frames to the full view of this time;
* ⚙: lines in the frames (slow), a moving slice, «Save GIF…»;
* «Data» selects full fields or the run surfaces, when the case has both.

No logic here: the bar shows the state of fs_timeline.Timeline and sends
signals. The window loads and draws.
"""

from __future__ import annotations

from PySide6 import QtCore, QtWidgets

import fs_i18n
import fs_timeline as ftl
from fs_i18n import tr

fs_i18n.install()

# Plain text signs: ⏮ ⏭ ⏸ are colour emoji in the Windows fonts.
PLAY, PAUSE = "▶", "❚❚"


class TimeBar(QtWidgets.QWidget):
    seek = QtCore.Signal(int)              # go to frame i
    play_toggled = QtCore.Signal(bool)
    fps_changed = QtCore.Signal(int)
    loop_toggled = QtCore.Signal(bool)
    full_view = QtCore.Signal()            # back to the full view
    source_changed = QtCore.Signal(str)    # "full" | "surf"
    options_changed = QtCore.Signal()      # lines in frames, moving slice
    export_requested = QtCore.Signal()
    reconstruct_requested = QtCore.Signal()
    server_requested = QtCore.Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.timeline = ftl.Timeline()
        self._n_missing = 0
        row = QtWidgets.QHBoxLayout(self)
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(4)

        def tool(text, tip, slot, check=False):
            b = QtWidgets.QToolButton()
            b.setText(text)
            b.setToolTip(tip)
            b.setAutoRaise(True)
            b.setCheckable(check)
            b.setMinimumWidth(26)
            (b.toggled if check else b.clicked).connect(slot)
            row.addWidget(b)
            return b

        self.lbl_title = QtWidgets.QLabel("Time")
        row.addWidget(self.lbl_title)
        self.btn_first = tool("⇤", "First time", lambda: self._go(0))
        self.btn_back = tool("◂", "One time back (←)", lambda: self._step(-1))
        self.btn_play = tool("▶", "Play / pause (Space). The frames load in "
                             "the background; the player waits for a frame "
                             "that is not here yet.", self._play, check=True)
        self.btn_fwd = tool("▸", "One time forward (→)", lambda: self._step(1))
        self.btn_last = tool("⇥", "Last time",
                             lambda: self._go(len(self.timeline) - 1))

        self.slider = QtWidgets.QSlider(QtCore.Qt.Horizontal)
        self.slider.setMinimum(0)
        self.slider.setMaximum(0)
        self.slider.setTickPosition(QtWidgets.QSlider.TicksBelow)
        self.slider.setTickInterval(1)
        self.slider.setPageStep(1)
        self.slider.setMinimumWidth(160)
        self.slider.setToolTip("Drag to scrub through the times. Each tick "
                               "is one written time.")
        self.slider.valueChanged.connect(self._slider_moved)
        row.addWidget(self.slider, 1)

        self.lbl_time = QtWidgets.QLabel("")
        self.lbl_time.setMinimumWidth(self.lbl_time.fontMetrics().horizontalAdvance(
            "t = 0.0000000 s   999/999") + 8)
        row.addWidget(self.lbl_time)

        self.cmb_fps = QtWidgets.QComboBox()
        for f in ftl.FPS_CHOICES:
            self.cmb_fps.addItem(tr("{n} fps", n=f), f)
        self.cmb_fps.setCurrentIndex(ftl.FPS_CHOICES.index(ftl.FPS_DEFAULT))
        self.cmb_fps.setToolTip("Playback speed: frames per second.")
        self.cmb_fps.currentIndexChanged.connect(self._fps)
        row.addWidget(self.cmb_fps)
        self.btn_loop = tool("⟲", "Loop: after the last time, start again.",
                             self._loop, check=True)
        self.btn_loop.setChecked(True)

        self.progress = QtWidgets.QProgressBar()
        self.progress.setMaximumWidth(110)
        self.progress.setMaximumHeight(14)
        self.progress.setTextVisible(True)
        self.progress.setFormat("%v/%m")
        self.progress.setToolTip("Frames loaded in the background.")
        self.progress.hide()
        row.addWidget(self.progress)

        self.cmb_source = QtWidgets.QComboBox()
        self.cmb_source.setToolTip(
            "Which data the timeline shows: the full fields of the case "
            "(body, slice, lines), or the surfaces that the run writes at "
            "each time (body and one mid plane, light).")
        self.cmb_source.currentIndexChanged.connect(
            lambda _: self.source_changed.emit(self.cmb_source.currentData() or "full"))
        self.cmb_source.hide()
        row.addWidget(self.cmb_source)

        self.btn_full = QtWidgets.QPushButton("Full View")
        self.btn_full.setToolTip(
            "The player shows fast frames (only the field values change). "
            "Back to the full view of this time: all layers, the slice "
            "handle, the coefficients.")
        self.btn_full.clicked.connect(self.full_view.emit)
        self.btn_full.hide()
        row.addWidget(self.btn_full)

        self.btn_reconstruct = QtWidgets.QPushButton("")
        self.btn_reconstruct.setToolTip(
            "These times are only in the processor*/ folders of this case. "
            "reconstructPar puts them into the case root.")
        self.btn_reconstruct.clicked.connect(self.reconstruct_requested.emit)
        self.btn_reconstruct.hide()
        row.addWidget(self.btn_reconstruct)

        self.btn_server = QtWidgets.QPushButton("Times from Server…")
        self.btn_server.setToolTip(
            "Show each time that the run wrote on the server, with the "
            "sizes, and download the selected ones (the server reconstructs "
            "them first if necessary).")
        self.btn_server.clicked.connect(self.server_requested.emit)
        row.addWidget(self.btn_server)

        self.btn_menu = QtWidgets.QToolButton()
        self.btn_menu.setText("⚙")
        self.btn_menu.setToolTip("Frame options")
        self.btn_menu.setPopupMode(QtWidgets.QToolButton.InstantPopup)
        menu = QtWidgets.QMenu(self.btn_menu)
        self.act_streams = menu.addAction("Streamlines in the frames (slow)")
        self.act_streams.setCheckable(True)
        self.act_streams.setToolTip(
            "The lines are traced again for each frame — this is the most "
            "expensive part: seconds per frame on a large mesh.")
        self.act_moving = menu.addAction("Moving slice (more memory)")
        self.act_moving.setCheckable(True)
        self.act_moving.setToolTip(
            "By default the slice of the frames stays where it is. With "
            "this option the fields of the full volume are kept in memory "
            "for each frame, and you can move the plane during playback.")
        for a in (self.act_streams, self.act_moving):
            a.toggled.connect(lambda _: self.options_changed.emit())
        menu.addSeparator()
        self.act_gif = menu.addAction("Save GIF…")
        self.act_gif.triggered.connect(self.export_requested.emit)
        menu.setToolTipsVisible(True)
        self.btn_menu.setMenu(menu)
        row.addWidget(self.btn_menu)

        self.set_times([])

    # --- state -> widgets -----------------------------------------------------

    def set_times(self, times, keep: float | None = None) -> None:
        """New times. The current frame stays on `keep` (or on the same
        time), else goes to the last time. No `seek` signal."""
        tl = self.timeline
        tl.set_times(times, keep)
        self.slider.blockSignals(True)
        self.slider.setMaximum(max(len(tl) - 1, 0))
        # A tick per time; with many times a tick per few, else the ticks
        # are one grey bar.
        self.slider.setTickInterval(max(1, len(tl) // 60))
        self.slider.setValue(max(tl.index, 0))
        self.slider.blockSignals(False)
        many = len(tl) >= 2
        for w in (self.btn_first, self.btn_back, self.btn_play, self.btn_fwd,
                  self.btn_last, self.slider, self.cmb_fps, self.btn_loop):
            w.setEnabled(many)
        if not many and self.btn_play.isChecked():
            self.set_playing(False)
        self._label()

    def set_index(self, i: int) -> None:
        """Show frame i (the window went there). No `seek` signal."""
        self.timeline.go(i)
        self.slider.blockSignals(True)
        self.slider.setValue(max(self.timeline.index, 0))
        self.slider.blockSignals(False)
        self._label()

    def set_playing(self, on: bool) -> None:
        self.timeline.playing = bool(on)
        self.btn_play.blockSignals(True)
        self.btn_play.setChecked(bool(on))
        self.btn_play.blockSignals(False)
        self.btn_play.setText(PAUSE if on else PLAY)

    def set_progress(self, have: int, total: int) -> None:
        """Frames loaded; hidden when all are here (or nothing loads)."""
        if total <= 0 or have >= total:
            self.progress.hide()
            return
        self.progress.setMaximum(total)
        self.progress.setValue(have)
        self.progress.show()

    def set_frame_mode(self, on: bool) -> None:
        self.btn_full.setVisible(bool(on))

    def set_sources(self, full: int, surf: int, current: str) -> None:
        """Show «Data» only when the case has both kinds of times."""
        c = self.cmb_source
        c.blockSignals(True)
        c.clear()
        if full and surf:
            c.addItem(tr("full fields ({n})", n=full), "full")
            c.addItem(tr("surfaces ({n})", n=surf), "surf")
            c.setCurrentIndex(1 if current == "surf" else 0)
        c.blockSignals(False)
        c.setVisible(bool(full and surf))

    def set_missing(self, n: int) -> None:
        self._n_missing = int(n)
        self.btn_reconstruct.setVisible(n > 0)
        self.btn_reconstruct.setText(
            fs_i18n.plural(n, "Reconstruct {n} more time (reconstructPar)",
                           "Reconstruct {n} more times (reconstructPar)"))

    def _label(self) -> None:
        t, k, n = self.timeline.label()
        self.lbl_time.setText(tr("t = {t:.6g} s   {k}/{n}", t=t, k=k, n=n)
                              if n else tr("no times"))

    def retranslate_ui(self) -> None:
        self._label()
        self.set_missing(self._n_missing)

    # --- widgets -> signals -------------------------------------------------

    def _slider_moved(self, v: int) -> None:
        self.timeline.go(v)
        self._label()
        self.seek.emit(self.timeline.index)

    def _go(self, i: int) -> None:
        if len(self.timeline):
            self.set_index(i)
            self.seek.emit(self.timeline.index)

    def _step(self, d: int) -> None:
        if len(self.timeline):
            self._go(self.timeline.step(d))

    def _play(self, on: bool) -> None:
        self.set_playing(on)
        self.play_toggled.emit(bool(on))

    def _fps(self, _i: int) -> None:
        self.timeline.fps = int(self.cmb_fps.currentData() or ftl.FPS_DEFAULT)
        self.fps_changed.emit(self.timeline.fps)

    def _loop(self, on: bool) -> None:
        self.timeline.loop = bool(on)
        self.loop_toggled.emit(bool(on))

    def keyPressEvent(self, ev) -> None:                  # noqa: N802
        k = ev.key()
        if k == QtCore.Qt.Key_Left:
            self._step(-1)
        elif k == QtCore.Qt.Key_Right:
            self._step(1)
        elif k == QtCore.Qt.Key_Space:
            self.btn_play.toggle()
        else:
            super().keyPressEvent(ev)
