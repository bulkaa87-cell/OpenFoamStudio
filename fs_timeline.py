"""Timeline of the «Results» tab: the times of a case and the mini player.

What is here (no Qt, so the tests run without a window):

* `time_value(name)`, `time_folders(case_dir)` — THE rule of a time folder
  name for the whole program (T51): a decimal number, also with an exponent
  (`5e-05`), never `nan` or `inf`. fs_post, fs_rsmesh, fs_anim, fs_live,
  fs_servertimes and fs_backend use it.
* `case_times(case_dir)` — each time of a case folder, and what the folder
  has for it: the full fields in the case root, only the processor*/ parts,
  or only the surfaces that the run writes (postProcessing/studioLive*).
* `surface_results(case_dir, cfg)` — the surface times as a results object
  (fs_live.LiveResults), for a case that has more surface times than full
  times (the «surfaces only» download).
* `Timeline` — the state of the player: the times, the current frame, play,
  step, loop, speed, and the order in which the frames load in the
  background (the frame that the person looks at first, then the frames that
  the player shows next).
* `FrameCache` — the loaded frames, with a memory limit (LRU). The values of
  a frame for one field stay when the person selects another field, so a
  return to the first field is immediate.
"""

from __future__ import annotations

import math
import os
import re
import threading
from collections import OrderedDict
from dataclasses import dataclass
from pathlib import Path

import numpy as np

# Folders of the surfaces that the run writes (fs_live).
SURF_DIRS = ("studioLive", "studioLiveSlice", "studioLiveFlow")

# Frames per second of the player: the choices and the default.
FPS_CHOICES = (1, 2, 4, 6, 10, 15, 24, 30)
FPS_DEFAULT = 6

# Memory for the loaded frames of one case (body and slice values).
CACHE_BYTES = 1_500_000_000


# A time folder name, as OpenFOAM writes it: 0.0015, 12, 1.5e-03, 5e-05.
# `float()` alone also takes "nan", "inf", "1_000" and spaces.
TIME_NAME = re.compile(r"^[-+]?(\d+\.?\d*|\.\d+)([eE][-+]?\d+)?$")

# The fields that make a time folder a result (not only polyMesh/ or uniform/).
RESULT_FIELDS = ("U", "p", "p_rgh", "T")


def time_value(name: str) -> float | None:
    """The time of a time folder name, or None when `name` is not a time."""
    if not TIME_NAME.match(name or ""):
        return None
    t = float(name)
    return t if math.isfinite(t) else None


def _has_fields(folder, fields=RESULT_FIELDS) -> bool:
    try:
        with os.scandir(folder) as it:
            return any(e.name in fields and e.is_file() for e in it)
    except OSError:
        return False


def time_folders(case_dir, *, positive: bool = True,
                 fields: "tuple[str, ...] | None" = None
                 ) -> list[tuple[float, str]]:
    """(time, folder name as on the disk) of each time folder, sorted.

    `positive`: only times > 0 (time 0 is the initial state). `fields`: only
    folders that have one of these field files. A folder that cannot be read
    gives an empty list, not an error.
    """
    out = []
    try:
        with os.scandir(case_dir) as it:
            entries = [(e.name, e.path) for e in it if e.is_dir()]
    except OSError:
        return []
    for name, path in entries:
        t = time_value(name)
        if t is None or (positive and t <= 0):
            continue
        if fields is not None and not _has_fields(path, fields):
            continue
        out.append((t, name))
    return sorted(out)


@dataclass
class TimeEntry:
    """One time of a case and where its data is."""
    name: str                 # the folder name, as on the disk
    time: float
    full: bool = False        # full fields in the case root
    proc: bool = False        # only in processor*/ (not reconstructed)
    surf: bool = False        # surfaces of the run (studioLive)

    @property
    def kind(self) -> str:
        """full | proc | surf: the best data that is here."""
        if self.full:
            return "full"
        if self.proc:
            return "proc"
        return "surf" if self.surf else ""


def case_times(case_dir: str) -> list[TimeEntry]:
    """Each time > 0 of the case, sorted. Time 0 is the initial state."""
    root = Path(case_dir)
    by: dict[str, TimeEntry] = {}

    def entry(name: str) -> TimeEntry | None:
        t = time_value(name)
        if t is None or t <= 0:
            return None
        # The same time can have two names (0.0015 and 1.5e-03): one entry.
        key = f"{t:.12g}"
        e = by.get(key)
        if e is None:
            e = by[key] = TimeEntry(name, t)
        return e

    if not root.is_dir():
        return []
    for _t, name in time_folders(root, fields=RESULT_FIELDS):
        e = entry(name)
        if e is not None:
            e.full, e.name = True, name
    proc = root / "processor0"
    for _t, name in time_folders(proc, fields=RESULT_FIELDS):
        e = entry(name)
        if e is not None and not e.full:
            e.proc = True
    wall = root / "postProcessing" / SURF_DIRS[0]
    if wall.is_dir():
        for d in wall.iterdir():
            if d.is_dir() and any(d.glob("*.vtp")):
                e = entry(d.name)
                if e is not None:
                    e.surf = True
    return sorted(by.values(), key=lambda e: e.time)


def surface_folders(case_dir: str) -> dict[float, str]:
    """{time: folder} of the run surfaces in the case (studioLive)."""
    return {e.time: str(Path(case_dir) / "postProcessing" / SURF_DIRS[0]
                        / e.name)
            for e in case_times(case_dir) if e.surf}


def surface_results(case_dir: str, cfg=None):
    """The surface times as a results object (same calls as fs_post.Results)."""
    import fs_live
    return fs_live.LiveResults(surface_folders(case_dir), cfg=cfg)


def pick_source(entries: list[TimeEntry]) -> str:
    """Which data the timeline shows first: "full" or "surf".

    The surfaces only when they give more times: after a «surfaces only»
    download the case has one full time (the last one) and many surface
    times, and the person wants to scrub through the surfaces.
    """
    full = sum(1 for e in entries if e.full)
    surf = sum(1 for e in entries if e.surf)
    return "surf" if surf >= 2 and surf > full else "full"


# --------------------------------------------------------------------------
# The player
# --------------------------------------------------------------------------

class Timeline:
    """Times, the current frame, and what the player does next."""

    def __init__(self, times=(), fps: int = FPS_DEFAULT, loop: bool = True):
        self.times: list[float] = []
        self.index = -1
        self.fps = int(fps)
        self.loop = bool(loop)
        self.playing = False
        self.set_times(times)

    def __len__(self) -> int:
        return len(self.times)

    @property
    def current(self) -> float | None:
        return self.times[self.index] if 0 <= self.index < len(self.times) \
            else None

    def set_times(self, times, keep: float | None = None) -> None:
        """New times. The current frame goes to `keep` (or stays on the
        same time), else to the last time."""
        old = self.current if keep is None else keep
        self.times = sorted({float(t) for t in times})
        if not self.times:
            self.index = -1
            self.playing = False
            return
        self.index = (self.nearest(old) if old is not None
                      else len(self.times) - 1)

    def nearest(self, t: float) -> int:
        if not self.times:
            return -1
        arr = np.asarray(self.times)
        return int(np.argmin(np.abs(arr - float(t))))

    def go(self, i: int) -> int:
        if self.times:
            self.index = max(0, min(int(i), len(self.times) - 1))
        return self.index

    def step(self, d: int) -> int:
        """One frame forward (d=1) or back (d=-1). Past the end: with loop
        to the other end, without loop it stays."""
        n = len(self.times)
        if not n:
            return -1
        i = self.index + int(d)
        if self.loop:
            i %= n
        return self.go(i)

    def tick(self) -> int | None:
        """Next frame of the playback, or None at the end without loop
        (the player then stops)."""
        n = len(self.times)
        if not n:
            self.playing = False
            return None
        if self.index + 1 >= n and not self.loop:
            self.playing = False
            return None
        return self.step(1)

    def interval_ms(self) -> int:
        return max(int(1000 / max(self.fps, 1)), 15)

    def load_order(self, have=()) -> list[int]:
        """Indices to load in the background, the most needed first.

        The current frame first, then the frames that the playback shows
        next (forward, then around the end when loop is on), then the frames
        behind. `have` — indices that are loaded already.
        """
        n = len(self.times)
        if not n:
            return []
        have = set(have)
        cur = max(self.index, 0)
        ahead = list(range(cur, n))
        behind = list(range(cur - 1, -1, -1))
        if self.loop:
            order = ahead + list(range(0, cur))
        else:
            order = ahead + behind
        return [i for i in order if i not in have]

    def label(self, i: int | None = None) -> tuple[float, int, int]:
        """(time, frame number from 1, frame count) for the time label."""
        i = self.index if i is None else i
        if not (0 <= i < len(self.times)):
            return (0.0, 0, len(self.times))
        return (self.times[i], i + 1, len(self.times))


# --------------------------------------------------------------------------
# Cache of loaded frames
# --------------------------------------------------------------------------

def _nbytes(value) -> int:
    if value is None:
        return 0
    if isinstance(value, np.ndarray):
        return int(value.nbytes)
    if isinstance(value, (list, tuple)):
        return sum(_nbytes(v) for v in value)
    if isinstance(value, dict):
        return sum(_nbytes(v) for v in value.values())
    return int(getattr(value, "nbytes", 0) or 0)


class FrameCache:
    """Loaded frames, the oldest out first when the memory limit is full.

    The key is any hashable value (for example (case, time, "body", "cp")).
    A value is an array, a list or a dict of arrays, or None ("this frame has
    no such field": that is an answer too, and a new read cannot change it).
    The background loader writes, the window reads: each call takes a lock.
    """

    def __init__(self, limit: int = CACHE_BYTES):
        self._lock = threading.RLock()
        self.limit = int(limit)
        self._data: OrderedDict = OrderedDict()
        self._size: dict = {}
        self.nbytes = 0

    def __contains__(self, key) -> bool:
        with self._lock:
            return key in self._data

    def __len__(self) -> int:
        with self._lock:
            return len(self._data)

    def get(self, key, default=None):
        with self._lock:
            if key not in self._data:
                return default
            self._data.move_to_end(key)
            return self._data[key]

    def put(self, key, value) -> None:
        size = _nbytes(value)
        with self._lock:
            if key in self._data:
                self.nbytes -= self._size.pop(key)
                del self._data[key]
            self._data[key] = value
            self._size[key] = size
            self.nbytes += size
            # The newest frame stays even if it alone is larger than the limit.
            while self.nbytes > self.limit and len(self._data) > 1:
                old, _ = self._data.popitem(last=False)
                self.nbytes -= self._size.pop(old)

    def drop(self, match) -> int:
        """Remove each key for which `match(key)` is true. → count."""
        with self._lock:
            gone = [k for k in self._data if match(k)]
            for k in gone:
                del self._data[k]
                self.nbytes -= self._size.pop(k)
            return len(gone)

    def clear(self) -> None:
        with self._lock:
            self._data.clear()
            self._size.clear()
            self.nbytes = 0
