"""The force coefficient files of a case: one reader for the whole program.

postProcessing/<name>/<start time>/coefficient.dat (forceCoeffs.dat in older
OpenFOAM versions). A run that was started again writes a new file next to
the old one (coefficient_0.dat). This module has no Qt and imports no other
module of the program: fs_backend (the Library, the live plots, the
history) and fs_polars (polars, report) read through it (T51; before, each
had its own parser).

* `parse_dat(path)` — one file: the coefficient columns and the reference
  values of the header (magUInf, lRef, Aref, CofR …).
* `read_segments(case_dir, name)` — each file of a function, the oldest
  first (a file that did not change is not read again).
* `merge(segments)` — one series per coefficient: the newest record of a
  time wins, and the steady warm start of a transient run is dropped
  (`warm_start_files`).
* `read_series(case_dir, name)` — `merge(read_segments(...))[0]`.
"""

from __future__ import annotations

import re
import threading
from dataclasses import dataclass, field
from pathlib import Path

# Coefficients that are read (the names of the OpenFOAM columns).
COEFFS = ("Cl", "Cd", "CmPitch", "Cs")

RE_HEADER_NUM = re.compile(r"^#\s*(\w+)\s*:\s*([-+0-9.eE]+)\s*$")
RE_HEADER_VEC = re.compile(
    r"^#\s*(\w+)\s*:\s*\(\s*([-+0-9.eE]+)\s+([-+0-9.eE]+)\s+([-+0-9.eE]+)\s*\)")


@dataclass
class Segment:
    """One .dat file of postProcessing/forceCoeffs: one part of a run."""
    path: str
    mtime: float
    series: dict = field(default_factory=dict)     # {"Cd": [(t, v), ...]}
    meta: dict = field(default_factory=dict)       # magUInf, lRef, Aref, CofR...

    @property
    def times(self) -> list[float]:
        for pts in self.series.values():
            return [t for t, _ in pts]
        return []

    @property
    def whole_times(self) -> bool:
        """True if each time is a whole number (steady iterations)."""
        ts = self.times
        return bool(ts) and all(float(t).is_integer() for t in ts)


def parse_dat(path) -> Segment | None:
    """One coefficient file, or None when it cannot be read."""
    path = Path(path)
    try:
        text = path.read_text(errors="replace")
        mtime = path.stat().st_mtime
    except OSError:
        return None
    seg = Segment(str(path), mtime)
    header: list[str] = []
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        if line.startswith("#"):
            m = RE_HEADER_VEC.match(line)
            if m:
                seg.meta[m.group(1)] = tuple(float(x) for x in m.groups()[1:])
                continue
            m = RE_HEADER_NUM.match(line)
            if m:
                try:
                    seg.meta[m.group(1)] = float(m.group(2))
                except ValueError:
                    pass
                continue
            cols = line.lstrip("#").split()
            if cols and cols[0].lower() in ("time", "time(s)"):
                header = cols
            continue
        if not header:
            continue
        parts = line.split()
        if len(parts) != len(header):
            continue
        try:
            vals = [float(x) for x in parts]
        except ValueError:
            continue
        for name, v in zip(header[1:], vals[1:]):
            if name in COEFFS:
                seg.series.setdefault(name, []).append((vals[0], v))
    return seg


# {path: ((mtime, size), Segment)}. One entry per file: during a run the
# live plots read the growing file again and again, and an entry per
# version would keep each old version in memory.
_CACHE: dict = {}
_CACHE_MAX = 512
_LOCK = threading.Lock()


def read_segments(case_dir, name: str = "forceCoeffs") -> list[Segment]:
    """Each coefficient file of the case, the oldest first.

    The order: modification time, then the path. A file that did not change
    (the same time and size) is not read again.
    """
    root = Path(case_dir) / "postProcessing" / name
    if not root.is_dir():
        return []
    files = []
    for f in root.glob("*/*.dat"):
        try:
            st = f.stat()
        except OSError:
            continue
        files.append((st.st_mtime, str(f), st.st_size))
    files.sort()
    out = []
    for mtime, f, size in files:
        stamp = (mtime, size)
        with _LOCK:
            got = _CACHE.get(f)
        seg = got[1] if got is not None and got[0] == stamp else None
        if seg is None:
            seg = parse_dat(f)
            with _LOCK:
                if len(_CACHE) >= _CACHE_MAX:
                    _CACHE.clear()
                _CACHE[f] = (stamp, seg)
        if seg is not None and seg.series:
            out.append(seg)
    return out


def warm_start_files(times: list[list[float]]) -> set[int]:
    """Indexes of the coefficient files that are a steady warm start.

    T45. The warm start of a transient run is a steady solver: its times are
    iteration numbers (1, 2, 3 …). It writes into the same
    postProcessing/forceCoeffs/0 folder as the transient run (times
    0.00001 …). Merged and sorted by time, the iterations come AFTER the
    seconds, and «the last 50 records» are the warm start (case_jet: Cl
    0.107 of the warm start instead of 0.126 of the transient run).

    Rule: when a file has fractional times, each file that has only
    whole-number times is a warm start. A steady case (all files whole) and
    a transient case without a warm start (all files fractional) keep all
    files. While the warm start runs and the transient file is not there,
    the warm start stays (the live plots show it).
    """
    whole = [bool(ts) and all(float(t).is_integer() for t in ts) for ts in times]
    if not any(ts and not w for ts, w in zip(times, whole)):
        return set()
    return {i for i, w in enumerate(whole) if w}


def merge(segments: list[Segment]) -> tuple[dict, dict, bool]:
    """(series, meta, warm_dropped): the series of the run itself.

    The newest record of a time wins: a restart moves the old file to
    coefficient_0.dat and writes the same time again. The steady warm start
    of a transient run is dropped (`warm_start_files`).
    """
    warm = warm_start_files([s.times for s in segments])
    by_name: dict[str, dict[float, float]] = {}
    meta: dict = {}
    for i, seg in enumerate(segments):       # oldest first: newer overwrite
        if i in warm:
            continue
        meta.update(seg.meta)
        for name, pts in seg.series.items():
            slot = by_name.setdefault(name, {})
            for t, v in pts:
                slot[t] = v
    series = {n: sorted(d.items()) for n, d in by_name.items()}
    return series, meta, bool(warm)


def read_series(case_dir, name: str = "forceCoeffs"
                ) -> dict[str, list[tuple[float, float]]]:
    """{coefficient: [(time, value), …]} of the function `name`."""
    return merge(read_segments(case_dir, name))[0]
