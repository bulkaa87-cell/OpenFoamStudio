"""Protection of case folders: no delete of a folder that a run uses (T43).

Two parts, both without Qt:

* The guard. Before the program deletes or replaces a case folder on a
  server (``sync_to_ssh``) or in WSL (``sync_to_wsl``), the same shell
  command first runs ``ofs_guard``. The guard refuses when:

  - the path is not one folder directly in ``~/openfoam-studio`` (empty
    name, ``/``, ``~``, a symbolic link, a path outside the root);
  - a run uses the folder: the head script of the session
    (``studio/pid``, ``studio/run.sh``) is alive, a process has its working
    folder there, or a docker/podman container of the program or of
    foamdist has the folder as its working folder or as a mount;
  - the folder has results that the program did not download
    (``studio/fetched`` is missing or older than ``studio/status``).

  Before 2026-11-12 the upload did ``rm -rf`` without a check. The case name
  comes from the STL name, so a second run of the same model deleted the
  folder of a run that was in progress (reproduced on a server with a dbg
  copy: studio/ and the time folders were gone, the head process continued
  in a deleted folder).

* The name of a new run. A run of a case that already ran goes into a new
  folder ``<name>_r2``, ``<name>_r3``, … (local, server and WSL). Thus the
  guard is the last line of defence, not the normal path.

The pre-run (T39) and its main run use ONE folder: the main run calls the
upload with ``own=True``. Then the guard still refuses a live run, but it
lets the program replace the results of its own pre-run.
"""

from __future__ import annotations

import os
import re
from pathlib import Path

import fs_paths
from fs_i18n import tr

ROOT = fs_paths.REMOTE_ROOT
# The marker that the results of this session are on the laptop.
FETCHED = "studio/fetched"
GUARD_MARK = "@@FS guard="
# The exit code of an upload that the guard refused.
GUARD_EXIT = 42

_NAME_OK = re.compile(r"^[\w][\w.-]*$", re.UNICODE)
_RUN_SUFFIX = re.compile(r"^(.*?)_r(\d+)$")


def check_name(name: str) -> str:
    """The case name if it is one safe folder name. Else ValueError."""
    text = str(name or "")
    if (not text or text in (".", "..") or not _NAME_OK.match(text)
            or text.endswith(".")):
        raise ValueError(tr("“{name}” is not a valid case folder name. "
                            "Nothing was deleted.", name=text))
    return text


# The guard, as a bash function. "{{" in the docker template is literal: the
# text is not formatted. Paths in the variables are always in double quotes.
_GUARD_FN = r'''ofs_guard() {
  local D="$1" own="$2" RR R S p st q c e id x n res i
  [ -n "$D" ] || { echo "bad empty"; return; }
  [ -L "$D" ] && { echo "bad link"; return; }
  [ -e "$D" ] || { echo "free new"; return; }
  [ -d "$D" ] || { echo "bad notdir"; return; }
  RR=$(readlink -f -- "$HOME/openfoam-studio") || { echo "bad root"; return; }
  R=$(readlink -f -- "$D") || { echo "bad path"; return; }
  case "$RR" in ""|/) echo "bad root"; return;; esac
  case "$R" in ""|/|"$HOME"|"$RR") echo "bad path"; return;; esac
  [ "$(dirname -- "$R")" = "$RR" ] || { echo "bad outside"; return; }
  S="$R/studio"
  p=$(head -c 20 "$S/pid" 2>/dev/null | tr -cd 0-9)
  st=$(head -c 20 "$S/status" 2>/dev/null)
  if [ -n "$p" ] && kill -0 "$p" 2>/dev/null \
      && tr '\0' ' ' < "/proc/$p/cmdline" 2>/dev/null | grep -q "studio/run.sh"; then
    case "$st" in running*) echo "busy run $p"; return;; esac
    for i in 1 2 3 4 5 6 7 8 9 10 11 12 13 14 15; do
      kill -0 "$p" 2>/dev/null || break; sleep 1; done
    kill -0 "$p" 2>/dev/null && { echo "busy run $p"; return; }
  fi
  for q in /proc/[0-9]*; do
    c=$(readlink "$q/cwd" 2>/dev/null) || continue
    c=${c% (deleted)}
    case "$c" in "$R"|"$R"/*|"$D"|"$D"/*)
      for i in 1 2 3 4 5 6 7 8 9 10; do [ -d "$q" ] || break; sleep 1; done
      [ -d "$q" ] && { echo "busy proc ${q#/proc/}"; return; };;
    esac
  done
  for e in docker podman; do
    command -v "$e" >/dev/null 2>&1 || continue
    for id in $("$e" ps -q --filter label=foamdist=1 2>/dev/null) \
              $("$e" ps -q --filter label=openfoam-studio=1 2>/dev/null); do
      for x in $("$e" inspect -f '{{.Config.WorkingDir}}{{range .Mounts}} {{.Source}}{{end}}' "$id" 2>/dev/null); do
        case "$x" in "$R"|"$R"/*|"$D"|"$D"/*) echo "busy container $id"; return;; esac
      done
    done
  done
  [ "$own" = 1 ] && { echo "free own"; return; }
  res=""
  [ -d "$S" ] && res=1
  for x in "$R"/processor* "$R"/postProcessing "$R"/*/; do
    [ -e "$x" ] || continue
    n=$(basename -- "$x")
    case "$n" in processor*|postProcessing|studio) res=1;;
      0|*[!0-9.e+-]*) ;;
      *) res=1;;
    esac
  done
  [ -z "$res" ] && { echo "free empty"; return; }
  if [ -f "$S/fetched" ] && ! [ "$S/status" -nt "$S/fetched" ]; then
    echo "free fetched"; return; fi
  echo "results"
}
'''


def guard_function() -> str:
    """The bash text that defines ``ofs_guard <folder> <own 0|1>``.

    It prints one line: ``free …``, ``busy run|proc|container <id>``,
    ``results`` or ``bad <why>``.
    """
    return _GUARD_FN


def head_alive(pid: str = "$p") -> str:
    """bash test: the head script of a session lives as process `pid`.

    The rule of ``ofs_guard``: ``kill -0`` and the command line has
    ``studio/run.sh``. After a reboot of the server, the number in
    ``studio/pid`` can belong to another process. Without ``/proc`` (not
    Linux), ``kill -0`` only. The session probe of fs_backend uses it (T44).
    """
    return (f'{{ [ -n "{pid}" ] && kill -0 "{pid}" 2>/dev/null && '
            f'{{ ! [ -r "/proc/{pid}/cmdline" ] || tr "\\0" " " '
            f'< "/proc/{pid}/cmdline" 2>/dev/null '
            f'| grep -q "studio/run.sh"; }}; }}')


def guarded(folder: str, then: str, own: bool = False) -> str:
    """bash: run the guard for `folder`; only on «free» run `then`.

    On a refusal it writes ``@@FS guard=<verdict>`` to stderr and ends with
    GUARD_EXIT. `folder` is a bash word in double quotes (``$HOME`` is
    expanded by the shell).
    """
    return (guard_function()
            + f'v=$(ofs_guard "{folder}" {1 if own else 0}); '
            + 'case "$v" in free*) ;; *) '
            + f'echo "{GUARD_MARK}$v" >&2; exit {GUARD_EXIT};; esac; '
            + then)


class CaseBusy(RuntimeError):
    """The guard refused to delete a case folder. Nothing was deleted."""

    def __init__(self, kind: str, detail: str, folder: str, where: str):
        self.kind, self.detail, self.folder, self.where = kind, detail, folder, where
        super().__init__(guard_text(kind, detail, folder, where))


def parse_guard(text: str) -> "tuple[str, str] | None":
    """(kind, detail) from the output of a refused upload, else None."""
    for line in (text or "").splitlines():
        at = line.find(GUARD_MARK)
        if at >= 0:
            words = line[at + len(GUARD_MARK):].split(None, 1)
            if words:
                return (words[0], words[1].strip() if len(words) > 1 else "")
    return None


def guard_text(kind: str, detail: str, folder: str, where: str) -> str:
    """The message of a refusal for the person (translated)."""
    if kind == "busy":
        return tr("The folder {folder} on {where} is used by a run now "
                  "({detail}). Nothing was deleted. Wait until the run "
                  "ends or stop it, or give the case another name.",
                  folder=folder, where=where, detail=detail)
    if kind == "results":
        return tr("The folder {folder} on {where} has results that were "
                  "not downloaded. Nothing was deleted. Connect to that "
                  "run and get its results, or give the case another "
                  "name.", folder=folder, where=where)
    return tr("Refused to delete {folder} on {where}: it is not a case "
              "folder in ~/openfoam-studio ({detail}). Nothing was deleted.",
              folder=folder, where=where, detail=detail or kind)


# --------------------------------------------------------------------------
# The folder of a new run
# --------------------------------------------------------------------------

def run_base(name: str) -> str:
    """«plane_r3» -> «plane»; «plane» -> «plane»."""
    m = _RUN_SUFFIX.match(name or "")
    return m.group(1) if m and m.group(1) else (name or "")


def local_used(folder: "str | os.PathLike") -> bool:
    """True if a run used this local case folder (it has run output).

    A folder that only «Create Case» made (system, constant, 0.orig, the
    manifest) is not used: the first run goes there.
    """
    root = Path(folder)
    try:
        if not root.is_dir():
            return False
        for entry in root.iterdir():
            n = entry.name
            if not entry.is_dir():
                continue
            if n in ("logs", "studio", "postProcessing") or n.startswith("processor"):
                return True
            if n != "0" and re.fullmatch(r"[0-9][0-9.e+-]*", n):
                return True
    except OSError:
        return True            # cannot read: do not write there
    return False


def local_names(base_dir: "str | os.PathLike") -> "tuple[set[str], set[str]]":
    """(used, all) folder names in the local cases folder (lower case)."""
    used, names = set(), set()
    try:
        entries = list(os.scandir(base_dir))
    except OSError:
        return used, names
    for e in entries:
        try:
            if not e.is_dir():
                continue
        except OSError:
            continue
        names.add(e.name.lower())
        if local_used(e.path):
            used.add(e.name.lower())
    return used, names


def new_run_name(name: str, used: "set[str]", taken: "set[str]") -> str:
    """The folder name for a new run of `name`.

    `used`: names whose folder has a run (here or on the server): never
    write there. `taken`: all names that exist (also unused ones). `name`
    itself is kept when it is not used. Else ``<base>_r<n>`` with n one
    more than the highest number of that base, minimum 2. All comparisons
    ignore the case of letters (Windows).
    """
    used = {u.lower() for u in used}
    taken = {t.lower() for t in taken} | used
    if name.lower() not in used:
        return name
    base = run_base(name)
    top = 1
    for t in taken:
        m = _RUN_SUFFIX.match(t)
        if m and m.group(1) == base.lower():
            top = max(top, int(m.group(2)))
    n = top + 1
    while f"{base}_r{n}".lower() in taken:
        n += 1
    return f"{base}_r{n}"
