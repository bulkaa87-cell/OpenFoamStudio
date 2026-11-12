"""All written times of a run on a server: list, reconstruct there, download.

After a parallel run the run script reconstructs only the last time
(reconstructPar -latestTime), and the download takes only the case root. So
the «Results» tab showed one time of a transient run that wrote 27 (T40).
Here:

* `list_script` / `parse_list` — each time of the run folder on the server:
  reconstructed in the root, only in processor*/, or only the surfaces that
  the run writes (postProcessing/studioLive*), with the sizes.
* `reconstruct_argv` — reconstructPar for the selected times that are only in
  processor*/, ON THE SERVER, with `nice -n 19`, in the same OpenFOAM as the
  run (the backend shell: a native install or the container). This is a light
  post-processing step, not a calculation. `-newTimes` skips each time that
  is reconstructed already. processor*/ is only read.
* `fetch_script` / `extract` — the selected times as a tar stream, into the
  local case: all fields, the fields for viewing, or the surfaces only.

No Qt: the dialog (fs_timesdlg) calls `get_times` from a worker thread.
"""

from __future__ import annotations

import shutil
import subprocess
import tarfile
import tempfile
import time as _time
from dataclasses import dataclass, field
from pathlib import Path

import fs_backend as be
import fs_timeline as ftl
from fs_i18n import tr

# What a download takes.
MODES = ("view", "all", "surf")
# Fields for the «Results» tab: body (p, wall shear stress, y+), slice
# (U, p, nut), temperature for a compressible run. The rest (phi, the *_0
# fields of the old time level, k, omega, transition fields) is two thirds of
# a time folder and the tab does not show it.
VIEW_FIELDS = ("U", "p", "p_rgh", "T", "nut", "yPlus", "wallShearStress")
SURF_DIRS = ("studioLive", "studioLiveSlice", "studioLiveFlow")
# Times per reconstructPar call: each call reads the mesh of each processor
# again, one call per time costs more than the fields; one call for all gives
# no progress.
GROUP = 4


def _q(path: str) -> str:
    """A path in bash double quotes; `$HOME` still expands."""
    return '"' + path.replace('"', '\\"') + '"'


def _num_case(var: str = "n") -> str:
    """bash: skip a name that is not a time (and time 0)."""
    return (f'case "${var}" in ""|0|*[!0-9.e+-]*) continue;; esac; ')


def list_script(remote: str) -> str:
    """bash: the times of the run folder. Lines:

    status <word>           — studio/status (running, done, fail…)
    dist <0|1>              — distributed run (foamdist)
    cells <n>               — cells of constant/polyMesh (0: none)
    nproc <n>               — processor* folders
    pcells <n>              — cells of all processor*/constant/polyMesh
    root <time> <bytes>     — reconstructed time with fields
    proc <time> <bytes> <parts> — time in processor*/ (bytes of all parts)
    surf <time> <bytes>     — surfaces of the run for this time
    file <name> <bytes>     — the files of the last root time (for the estimate)
    """
    s = ",".join(SURF_DIRS)
    return (
        f'cd {_q(remote)} 2>/dev/null || {{ echo nodir; exit 0; }}; '
        'shopt -s nullglob; '
        'echo "status $(head -c 40 studio/status 2>/dev/null | head -1 | cut -f1)"; '
        '[ -f foamdist.json ] && echo "dist 1" || echo "dist 0"; '
        'c=$(head -c 4096 constant/polyMesh/owner 2>/dev/null '
        '| grep -ao "nCells:[0-9]*" | head -1 | cut -d: -f2); '
        'echo "cells ${c:-0}"; '
        'np=0; pc=0; for p in processor[0-9]*/; do np=$((np+1)); '
        'k=$(head -c 4096 "$p"constant/polyMesh/owner 2>/dev/null '
        '| grep -ao "nCells:[0-9]*" | head -1 | cut -d: -f2); '
        'pc=$((pc+${k:-0})); done; '
        'echo "nproc $np"; echo "pcells $pc"; last=""; '
        'for d in [0-9]*/; do n=${d%/}; ' + _num_case() +
        '[ -e "$n/U" ] || [ -e "$n/p" ] || continue; '
        'echo "root $n $(du -sb "$n" | cut -f1)"; last="$n"; done; '
        'if [ "$np" -gt 0 ]; then for d in processor0/[0-9]*/; do '
        'n=${d%/}; n=${n##*/}; ' + _num_case() +
        'set -- processor[0-9]*/"$n"; '
        'echo "proc $n $(du -scb "$@" | tail -1 | cut -f1) $#"; done; fi; '
        f'for d in postProcessing/{SURF_DIRS[0]}/*/; do n=${{d%/}}; n=${{n##*/}}; '
        + _num_case() +
        f'set -- postProcessing/{{{s}}}/"$n"; '
        'echo "surf $n $(du -scb "$@" 2>/dev/null | tail -1 | cut -f1)"; done; '
        'if [ -n "$last" ]; then for f in "$last"/*; do [ -f "$f" ] && '
        'echo "file ${f##*/} $(stat -c %s "$f")"; done; fi; true'
    )


@dataclass
class ServerTime:
    name: str
    time: float
    root: int = 0             # bytes of the reconstructed folder (0 = none)
    proc: int = 0             # bytes of the processor*/ parts
    parts: int = 0            # processor*/ folders that have this time
    surf: int = 0             # bytes of the surfaces

    @property
    def reconstructed(self) -> bool:
        return self.root > 0


@dataclass
class Listing:
    ok: bool = True
    why: str = ""
    status: str = ""
    distributed: bool = False
    cells: int = 0            # cells of the mesh of the fields
    root_cells: int = 0       # cells of constant/polyMesh of the case root
    nproc: int = 0
    times: list = field(default_factory=list)       # [ServerTime]
    files: dict = field(default_factory=dict)       # last root time: name → bytes

    @property
    def running(self) -> bool:
        return self.status == "running"

    def by_name(self) -> dict:
        return {t.name: t for t in self.times}

    def can_reconstruct(self, t: ServerTime) -> bool:
        """All processor*/ parts are on the server (a foamdist run keeps
        them on the other machines until the gather)."""
        return t.proc > 0 and self.nproc > 0 and t.parts == self.nproc

    def needs_reconstruct(self, t: ServerTime) -> bool:
        return not t.reconstructed and self.can_reconstruct(t)

    def full_bytes(self, t: ServerTime) -> int:
        """Bytes of the reconstructed folder (an estimate if not yet)."""
        if t.root:
            return t.root
        ref = next((r for r in reversed(self.times) if r.root and r.proc),
                   None)
        if ref is not None and t.proc:
            return int(ref.root * t.proc / ref.proc)
        return int(t.proc * 0.7)

    def view_share(self) -> float:
        """Part of a time folder that the fields for viewing take."""
        total = sum(self.files.values())
        if not total:
            return 0.35
        view = sum(b for n, b in self.files.items() if n in VIEW_FIELDS)
        return max(min(view / total, 1.0), 0.01)

    def size(self, t: ServerTime, mode: str) -> int:
        """Bytes that a download of `t` in `mode` takes."""
        if mode == "surf":
            return t.surf
        full = self.full_bytes(t)
        return int(full * self.view_share()) if mode == "view" else full

    def usable(self, t: ServerTime, mode: str) -> bool:
        """Can a download in `mode` give this time?"""
        if mode == "surf":
            return t.surf > 0
        if t.reconstructed:
            return True
        if not self.can_reconstruct(t):
            return False
        # The newest time of a run that goes on can still be written.
        if self.running and t is self._last_proc():
            return False
        return True

    def _last_proc(self):
        procs = [t for t in self.times if t.proc]
        return procs[-1] if procs else None


def parse_list(text: str) -> Listing:
    out = Listing()
    by: dict[str, ServerTime] = {}
    pcells = 0

    def get(name: str) -> ServerTime | None:
        t = ftl.time_value(name)          # the rule of a time folder (T51)
        if t is None:
            return None
        return by.setdefault(name, ServerTime(name, t))

    for line in text.splitlines():
        p = line.split()
        if not p:
            continue
        key, rest = p[0], p[1:]
        try:
            if key == "nodir":
                return Listing(ok=False, why=tr("the run folder does not exist"))
            if key == "status":
                out.status = rest[0] if rest else ""
            elif key == "dist" and rest:
                out.distributed = rest[0] == "1"
            elif key == "cells" and rest:
                out.root_cells = int(rest[0])
            elif key == "pcells" and rest:
                pcells = int(rest[0])
            elif key == "nproc" and rest:
                out.nproc = int(rest[0])
            elif key == "root" and len(rest) >= 2:
                t = get(rest[0])
                if t is not None:
                    t.root = int(rest[1])
            elif key == "proc" and len(rest) >= 3:
                t = get(rest[0])
                if t is not None:
                    t.proc, t.parts = int(rest[1]), int(rest[2])
            elif key == "surf" and len(rest) >= 2:
                t = get(rest[0])
                if t is not None:
                    t.surf = int(rest[1])
            elif key == "file" and len(rest) >= 2:
                out.files[rest[0]] = int(rest[1])
        except ValueError:
            continue
    out.times = sorted(by.values(), key=lambda t: t.time)
    # The fields of processor*/ are on the mesh of the parts: their sum is
    # the cell count (a decomposed-only case has no root mesh, or only the
    # background mesh of blockMesh there).
    out.cells = pcells if pcells > 0 else out.root_cells
    return out


def reconstruct_command(names) -> str:
    """The OpenFOAM command for the backend shell (on the server)."""
    times = ",".join(names)
    return f"nice -n 19 reconstructPar -newTimes -time '{times}'"


def reconstruct_argv(backend, case_dir: str, remote: str, names) -> list[str]:
    return backend.shell(case_dir, reconstruct_command(names),
                         native_dir=remote)


def fetch_script(remote: str, names, mode: str) -> str:
    """bash: the selected times as a tar stream (stdout), with nice.

    view — the VIEW_FIELDS files and uniform/ of each time;
    all  — each time folder;
    surf — postProcessing/studioLive*/<time>.
    Exit code 3: nothing to send.
    """
    if mode not in MODES:
        raise ValueError(mode)
    items = " ".join(f"'{n}'" for n in names)
    if mode == "surf":
        pick = (f'for n in {items}; do for s in {" ".join(SURF_DIRS)}; do '
                'd="postProcessing/$s/$n"; [ -d "$d" ] && set -- "$@" "$d"; '
                'done; done; ')
    elif mode == "all":
        pick = (f'for n in {items}; do [ -d "$n" ] && set -- "$@" "$n"; '
                'done; ')
    else:
        fields = " ".join(VIEW_FIELDS + ("uniform",))
        pick = (f'for n in {items}; do for f in {fields}; do '
                '[ -e "$n/$f" ] && set -- "$@" "$n/$f"; done; done; ')
    return (f'cd {_q(remote)} || exit 1; set --; ' + pick +
            '[ $# -gt 0 ] || exit 3; nice -n 19 tar cf - "$@"')


# --------------------------------------------------------------------------
# The local side
# --------------------------------------------------------------------------

def _safe_members(tar: tarfile.TarFile, dest: Path):
    root = dest.resolve()
    for m in tar.getmembers():
        if not (m.isfile() or m.isdir()):
            continue
        out = (dest / m.name).resolve()
        if root != out and root not in out.parents:
            continue
        yield m, out


def extract(tar_path: str, case_dir: str, mode: str) -> list[str]:
    """Put the downloaded times into the case. → the names that came.

    The files go first into a hidden folder in the case and then move into
    place, so a broken download leaves no half time folder. A time folder that
    the case has already stays (its files are not overwritten), only missing
    files are added: the case keeps what it had.
    """
    case = Path(case_dir)
    part = case / ".t40-download.part"
    if part.exists():
        shutil.rmtree(part)
    part.mkdir()
    came: set[str] = set()
    try:
        with tarfile.open(tar_path, "r") as tar:
            for m, out in _safe_members(tar, part):
                if m.isdir():
                    out.mkdir(parents=True, exist_ok=True)
                    continue
                out.parent.mkdir(parents=True, exist_ok=True)
                src = tar.extractfile(m)
                if src is not None:
                    with src, open(out, "wb") as fh:
                        shutil.copyfileobj(src, fh)
        if mode == "surf":
            for s in SURF_DIRS:
                base = part / "postProcessing" / s
                for d in base.iterdir() if base.is_dir() else []:
                    _merge(d, case / "postProcessing" / s / d.name)
                    came.add(d.name)
        else:
            for d in part.iterdir():
                if d.is_dir():
                    _merge(d, case / d.name)
                    came.add(d.name)
    finally:
        shutil.rmtree(part, ignore_errors=True)
    return sorted(came, key=float)


def _merge(src: Path, dst: Path) -> None:
    """Move `src` to `dst`; into an existing `dst` only the missing files."""
    if not dst.exists():
        dst.parent.mkdir(parents=True, exist_ok=True)
        src.replace(dst)
        return
    for f in src.rglob("*"):
        rel = f.relative_to(src)
        target = dst / rel
        if f.is_dir():
            target.mkdir(parents=True, exist_ok=True)
        elif not target.exists():
            target.parent.mkdir(parents=True, exist_ok=True)
            f.replace(target)


def local_cells(case_dir: str) -> int:
    import fs_fast
    try:
        return int(fs_fast.n_cells(case_dir) or 0)
    except OSError:
        return 0


def run_folder(case_dir: str, known: str = "") -> str:
    """The run folder of this case on the server (for bash).

    `known` — the folder of the run that the window ran or connected to
    (Worker.native_dir), if there is one. Else the folder that the manifest
    of the case records for its last upload (fs_backend.run_server_dir, T43:
    also «<name>_r2»); a case without a record gets the old rule
    ~/openfoam-studio/<case folder name>.

    Nothing here or in the download deletes on the server: reconstructPar
    only adds time folders (-newTimes), and the download only reads (tar).
    """
    return known or be.run_server_dir(case_dir)[1]


def _key(t: float) -> str:
    return f"{float(t):.12g}"


def local_have(case_dir: str, mode: str) -> set:
    """Times (keys) that the local case has already for `mode`."""
    return {_key(e.time) for e in ftl.case_times(case_dir)
            if (e.surf if mode == "surf" else e.full)}


def missing_names(listing: Listing, mode: str, have) -> list[str]:
    """Each time that a download in `mode` can give and that is not here."""
    have = set(have)
    return [t.name for t in listing.times
            if listing.usable(t, mode) and _key(t.time) not in have]


def range_names(listing: Listing, mode: str, a: float, b: float) -> list[str]:
    """Each time from `a` to `b` (both included, any order) for `mode`."""
    lo, hi = sorted((float(a), float(b)))
    eps = 1e-12 * max(abs(hi), 1.0)
    return [t.name for t in listing.times
            if listing.usable(t, mode) and lo - eps <= t.time <= hi + eps]


def size_of(listing: Listing, names, mode: str) -> int:
    by = listing.by_name()
    return sum(listing.size(by[n], mode) for n in names if n in by)


def mesh_problem(listing: Listing, case_dir: str, mode: str) -> str:
    """Why the fields of the server do not fit the local case, or ""."""
    if mode == "surf":
        return ""
    here = local_cells(case_dir)
    if not here:
        return tr("The local case has no mesh (constant/polyMesh). Download "
                  "the results of the run first, or select “surfaces only”.")
    if listing.cells and here != listing.cells:
        return tr("The mesh of the local case ({here:,} cells) is not the "
                  "mesh of the run on the server ({there:,} cells): the "
                  "fields would not fit. Download the results of the run "
                  "again first.", here=here, there=listing.cells)
    return ""


# --------------------------------------------------------------------------
# All steps
# --------------------------------------------------------------------------

def _run(argv, timeout: int) -> tuple[int, str]:
    try:
        p = subprocess.run(argv, capture_output=True, timeout=timeout,
                           creationflags=be.CREATE_NO_WINDOW)
    except (OSError, subprocess.SubprocessError) as exc:
        return 1, str(exc)
    out = (p.stdout or b"").decode("utf-8", "replace")
    err = (p.stderr or b"").decode("utf-8", "replace")
    return p.returncode, out if p.returncode == 0 else (err or out)


def list_times(backend, remote: str, timeout: int = 120) -> Listing:
    rc, out = _run(be.ssh_argv(backend.host, list_script(remote)), timeout)
    if rc != 0:
        return Listing(ok=False, why=out.strip()[-300:] or tr("code {rc}", rc=rc))
    return parse_list(out)


class Cancelled(Exception):
    pass


def get_times(backend, case_dir: str, remote: str, listing: Listing,
              names, mode: str, say=None, cancelled=None) -> dict:
    """Reconstruct (on the server, only what is missing) and download.

    `say(text, fraction)` gives the progress; `cancelled()` → True stops
    between two steps. → {"reconstructed": [...], "came": [...], "bytes": n,
    "seconds": s}.
    """
    t0 = _time.monotonic()
    say = say or (lambda *_: None)
    cancelled = cancelled or (lambda: False)
    by = listing.by_name()
    names = [n for n in names if n in by and listing.usable(by[n], mode)]
    if not names:
        raise RuntimeError(tr("No time can be downloaded in this mode."))
    why = mesh_problem(listing, case_dir, mode)
    if why:
        raise RuntimeError(why)
    todo = ([n for n in names if listing.needs_reconstruct(by[n])]
            if mode != "surf" else [])
    total = len(todo) + len(names)
    done_rec = []
    for a in range(0, len(todo), GROUP):
        if cancelled():
            raise Cancelled()
        group = todo[a:a + GROUP]
        say(tr("reconstructPar on the server: times {a}–{b} of {n}",
               a=a + 1, b=a + len(group), n=len(todo)), a / max(total, 1))
        rc, out = _run(reconstruct_argv(backend, case_dir, remote, group),
                       timeout=3 * 3600)
        if rc != 0:
            raise RuntimeError(tr("reconstructPar on the server failed "
                                  "(code {rc}): {tail}", rc=rc,
                                  tail=out.strip()[-300:]))
        done_rec += group
    if cancelled():
        raise Cancelled()
    want = sum(listing.size(by[n], mode) for n in names)
    say(tr("downloading {n} times (about {mb:,.0f} MB)…", n=len(names),
           mb=want / 1e6), len(todo) / max(total, 1))
    tmp = Path(tempfile.gettempdir()) / f"ofs-times-{Path(case_dir).name}.tar"
    try:
        rc, err = be._pipe(be.ssh_argv(backend.host,
                                       fetch_script(remote, names, mode)),
                           stdout_path=tmp, timeout=6 * 3600)
        if rc == 3:
            raise RuntimeError(tr("there is no data for these times on the "
                                  "server"))
        if rc != 0:
            raise RuntimeError(tr("cannot get the data: {err}",
                                  err=err.strip()[-200:] or rc))
        size = tmp.stat().st_size
        say(tr("unpacking…"), 0.97)
        came = extract(str(tmp), case_dir, mode)
    finally:
        tmp.unlink(missing_ok=True)
    say(tr("done"), 1.0)
    return {"reconstructed": done_rec, "came": came, "bytes": size,
            "seconds": _time.monotonic() - t0}
