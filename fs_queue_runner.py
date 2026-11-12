#!/usr/bin/env python3
"""OpenFOAM Studio: the run queue on a server (T46).

This file runs ON THE SERVER of the queue. It has only the Python standard
library: the server has no other module of the program. The program and the
command line `ofs` (fs_queue, fs_cli) put it in ~/openfoam-studio/.queue and
call it over ssh. n8n or a script on the server can call it directly:

    python3 ~/openfoam-studio/.queue/fs_queue_runner.py list
    python3 ~/openfoam-studio/.queue/fs_queue_runner.py cancel j7

Each command prints one JSON object.

The queue
---------
`queue.json` holds the jobs in their order. A job is one case folder on one
machine and one run script in it (studio/q_<token>.sh). The program makes the
script with the same generator as a detached run (fs_backend.detached_script).
To start a job, the runner copies that script to studio/run.sh and starts it
with the same command as «Run on Server» (fs_backend.detached_launch_command).
Thus a queued run is a normal detached run: «Connect to Run» shows it, and
the T43 guard (fs_caseguard) sees it.

States: queued -> running -> done | failed | cancelled; held (a queued job
that must wait). `after`: the job starts only when each of these jobs is done.
If one of them failed or was cancelled, the job is cancelled. `not_before`: the
job does not start before this time (epoch seconds).

Machines
--------
`host` is "" (this server) or an ssh name that this server knows (node4).
`nodes` are other machines that the job also uses (a foamdist run). No limits
of hours, cores or load (the user, 2026-11-12). A job starts when its turn and
its chain allow, unless:

* another job of the queue runs on one of its machines (one queue job per
  machine is the default order; `share` = «parallel on this machine»);
* one of its machines does not reply.

Another OpenFOAM run on the machine (one that the queue did not start) or a
high load (>= LOAD_FRAC of the cores) does not hold a job back: it goes into
the `note` of the job, for the person. The queue never stops or touches a run
that it did not start, and the T43 guard refuses a folder that a run uses.

The daemon
----------
`daemon` loops: it reads the states of the running jobs (studio/status,
studio/pid), starts the next jobs and writes `queue.json`. One daemon only
(lock file). It survives the end of the ssh session (setsid nohup) or runs as a
systemd user service (`install`), which also starts after a reboot when
lingering is on for the user. This file does not enable lingering.
A job that was running when the server stopped becomes failed ("aborted").

Only the states and the logs are written here. Notifications are made by the
program, when it reads the queue.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import signal
import subprocess
import sys
import time
import traceback

VERSION = 2
QUEUE_ENV = "OFS_QUEUE_DIR"
DEFAULT_DIR = "~/openfoam-studio/.queue"
UNIT = "ofs-queue"
FORMAT = 1

STATES = ("queued", "held", "running", "done", "failed", "cancelled")
ACTIVE = ("queued", "held", "running")
FINAL = ("done", "failed", "cancelled")

POLL = 20            # s: a full tick (states of running jobs, starts)
WAKE = 2             # s: how often the daemon looks for a change of the queue
LOAD_FRAC = 0.5      # a note «busy» when load1 >= LOAD_FRAC * cores
UNREACH_RETRY = 120  # s: do not ask an unreachable machine again before this
LOG_MAX = 1 << 20    # bytes of runner.log before it is rotated

# The process names of OpenFOAM that make a machine busy. The same names as
# fs_backend.FOAM_PROCS (tests/test_queue.py compares them).
FOAM_NAMES = ("simpleFoam", "rhoSimpleFoam", "pimpleFoam", "rhoPimpleFoam",
              "snappyHexMesh", "potentialFoam", "blockMesh", "checkMesh",
              "topoSet", "refineHexMesh", "foamdist")

_DIR_RE = re.compile(r"^(\$HOME|/[\w./-]+)/openfoam-studio/[\w][\w.-]*$")
_HOST_RE = re.compile(r"^([\w.-]+@)?[\w.-]*$")
_KEY_RE = re.compile(r"^[~\w./-]*$")
_SCRIPT_RE = re.compile(r"^q_[\w-]+\.sh$")
_ID_RE = re.compile(r"^j\d+$")


class QueueError(Exception):
    """A request that the queue refuses (bad job, unknown id)."""


# --------------------------------------------------------------------------
# Files
# --------------------------------------------------------------------------

def queue_dir() -> str:
    return os.path.expanduser(os.environ.get(QUEUE_ENV) or DEFAULT_DIR)


def _path(qdir: str, name: str) -> str:
    return os.path.join(qdir, name)


class Lock:
    """An exclusive lock on a file (flock; msvcrt for the tests on Windows)."""

    def __init__(self, path: str, timeout: float = 120.0,
                 blocking: bool = True):
        self.path, self.timeout, self.blocking = path, timeout, blocking
        self.fh = None

    def acquire(self) -> bool:
        os.makedirs(os.path.dirname(self.path) or ".", exist_ok=True)
        self.fh = open(self.path, "a+")
        end = time.monotonic() + self.timeout
        while True:
            if self._try():
                return True
            if not self.blocking or time.monotonic() > end:
                self.fh.close()
                self.fh = None
                if self.blocking:
                    raise QueueError("lock timeout: " + self.path)
                return False
            time.sleep(0.1)

    def _try(self) -> bool:
        try:
            import fcntl
            try:
                fcntl.flock(self.fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                return True
            except OSError:
                return False
        except ImportError:                         # Windows (tests only)
            import msvcrt
            try:
                self.fh.seek(0)
                msvcrt.locking(self.fh.fileno(), msvcrt.LK_NBLCK, 1)
                return True
            except OSError:
                return False

    def release(self) -> None:
        if self.fh is None:
            return
        try:
            try:
                import fcntl
                fcntl.flock(self.fh.fileno(), fcntl.LOCK_UN)
            except ImportError:
                import msvcrt
                try:
                    self.fh.seek(0)
                    msvcrt.locking(self.fh.fileno(), msvcrt.LK_UNLCK, 1)
                except OSError:
                    pass
        finally:
            self.fh.close()
            self.fh = None

    def __enter__(self):
        self.acquire()
        return self

    def __exit__(self, *exc):
        self.release()


def empty_queue() -> dict:
    return {"format": FORMAT, "paused": False, "next": 1, "jobs": []}


def load(qdir: str) -> dict:
    try:
        with open(_path(qdir, "queue.json"), encoding="utf-8") as fh:
            data = json.load(fh)
    except FileNotFoundError:
        return empty_queue()
    except ValueError:
        # A broken file is kept for a person to look at; the queue starts
        # empty instead of guessing.
        bad = _path(qdir, "queue.json.bad-%d" % int(time.time()))
        try:
            os.replace(_path(qdir, "queue.json"), bad)
        except OSError:
            pass
        return empty_queue()
    if not isinstance(data, dict) or not isinstance(data.get("jobs"), list):
        return empty_queue()
    data.setdefault("paused", False)
    data.setdefault("next", 1)
    return data


def save(qdir: str, data: dict) -> None:
    os.makedirs(qdir, exist_ok=True)
    tmp = _path(qdir, "queue.json.tmp")
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(data, fh, ensure_ascii=False, indent=1)
        fh.write("\n")
    os.replace(tmp, _path(qdir, "queue.json"))


def log(qdir: str, text: str) -> None:
    """One line in runner.log (ASCII ids and numbers, no secrets)."""
    path = _path(qdir, "runner.log")
    try:
        if os.path.getsize(path) > LOG_MAX:
            os.replace(path, path + ".1")
    except OSError:
        pass
    try:
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(time.strftime("%Y-%m-%d %H:%M:%S ") + text + "\n")
    except OSError:
        pass


def job_by_id(data: dict, jid: str) -> dict:
    for j in data["jobs"]:
        if j.get("id") == jid:
            return j
    raise QueueError("unknown job: " + str(jid))


# --------------------------------------------------------------------------
# Shell: this machine or ssh
# --------------------------------------------------------------------------

SSH_OPTS = ["-o", "BatchMode=yes", "-o", "ConnectTimeout=10",
            "-o", "ServerAliveInterval=15"]


class Shell:
    """Runs a bash script here ("" host) or on an ssh host."""

    def __init__(self):
        self._canon = {}

    def run(self, host: str, script: str, key: str = "",
            timeout: float = 60) -> "tuple[int, str]":
        if not host:
            argv = ["bash", "-c", script]
        else:
            argv = ["ssh"] + SSH_OPTS
            if key:
                argv += ["-i", os.path.expanduser(key)]
            argv += [host, script]
        try:
            p = subprocess.run(argv, stdout=subprocess.PIPE,
                               stderr=subprocess.PIPE, timeout=timeout,
                               stdin=subprocess.DEVNULL)
        except subprocess.TimeoutExpired:
            return 124, "timeout"
        except OSError as exc:
            return 127, str(exc)
        out = p.stdout.decode("utf-8", "replace")
        if p.returncode != 0:
            out += p.stderr.decode("utf-8", "replace")
        return p.returncode, out

    def canon(self, host: str) -> str:
        """One name per machine: "" for this server, else user@hostname.

        A foamdist node is "user1@10.0.0.21" and the same machine as a
        job is "node1": both must count as one machine.
        """
        if not host:
            return ""
        if host in self._canon:
            return self._canon[host]
        name = host
        try:
            p = subprocess.run(["ssh", "-G", host], stdout=subprocess.PIPE,
                               stderr=subprocess.DEVNULL, timeout=10)
            got = {}
            for line in p.stdout.decode("utf-8", "replace").splitlines():
                k, _, v = line.partition(" ")
                if k in ("user", "hostname"):
                    got[k] = v.strip()
            if got.get("hostname"):
                if got["hostname"] in self._local_addrs():
                    name = ""
                else:
                    name = "%s@%s" % (got.get("user", ""), got["hostname"])
        except (OSError, subprocess.SubprocessError):
            pass
        self._canon[host] = name
        return name

    def _local_addrs(self) -> set:
        if "_local" not in self._canon:
            addrs = {"localhost", "127.0.0.1", "::1"}
            try:
                p = subprocess.run(["hostname", "-I"], stdout=subprocess.PIPE,
                                   stderr=subprocess.DEVNULL, timeout=10)
                addrs |= set(p.stdout.decode().split())
                p = subprocess.run(["hostname"], stdout=subprocess.PIPE,
                                   stderr=subprocess.DEVNULL, timeout=10)
                addrs |= set(p.stdout.decode().split())
            except (OSError, subprocess.SubprocessError):
                pass
            self._canon["_local"] = addrs
        return self._canon["_local"]


# --------------------------------------------------------------------------
# Scripts (bash) that the runner sends
# --------------------------------------------------------------------------

def launch_command(d: str, script: str, jid: str = "") -> str:
    """Start the job script as a detached run (no guard).

    The last part is the same command as fs_backend.detached_launch_command
    (tests/test_queue.py compares them). The program sends its own launch
    command with the T43 guard in front; this one is for a job without it.
    """
    s = d + "/studio"
    return (f'cd "{d}" && cp -f "{s}/{script}" "{s}/run.sh" && '
            f'rm -f "{s}/status" "{s}/pid" && '
            f'printf "%s\\n" "{jid}" > "{s}/queue_job" && '
            + detached_launch(d))


def detached_launch(d: str) -> str:
    """The detached start of studio/run.sh (fs_backend.launch_detached)."""
    return (f'cd "{d}" && {{ setsid nohup bash "{d}/studio/run.sh" '
            f'> "{d}/studio/boot.log" 2>&1 < /dev/null & echo $!; }}')


def stop_command(d: str) -> str:
    """Stop a running job (without the dist cleanup of the program)."""
    return (f'p=$(cat "{d}/studio/pid" 2>/dev/null); '
            '[ -n "$p" ] && kill -TERM -"$p" 2>/dev/null; '
            f'printf "stop\\tqueue\\n" > "{d}/studio/status" 2>/dev/null; '
            'echo 0')


def head_alive(pid: str = "$p") -> str:
    """bash test: the head script of a run lives as process `pid`.

    The same text as fs_caseguard.head_alive (tests/test_queue.py checks it;
    this module cannot import it: it lives alone on the server). `kill -0`
    alone is not enough: after a reboot of the server, the number in
    studio/pid can belong to another process. The command line must have
    studio/run.sh. Without /proc (not Linux), `kill -0` only.
    """
    return (f'{{ [ -n "{pid}" ] && kill -0 "{pid}" 2>/dev/null && '
            f'{{ ! [ -r "/proc/{pid}/cmdline" ] || tr "\\0" " " '
            f'< "/proc/{pid}/cmdline" 2>/dev/null '
            f'| grep -q "studio/run.sh"; }}; }}')


def probe_job_script(d: str, pid: int) -> str:
    s = f'"{d}/studio'
    return (
        f's=$(head -c 400 {s}/status" 2>/dev/null | tr "\\n" " "); '
        f'p=$(head -c 20 {s}/pid" 2>/dev/null | tr -cd 0-9); '
        'a=0; ' + head_alive("$p") + ' && a=1; '
        # No launch pid: "" (`kill -0 0` would test the own process group).
        'l=0; ' + head_alive(str(int(pid)) if pid else "") + ' && l=1; '
        f'st=$(head -c 200 {s}/step" 2>/dev/null | tr -d "\\n"); '
        'printf "@@Q %s\\037%s\\037%s\\037%s\\037%s\\n" '
        '"$s" "$p" "$a" "$l" "$st"')


def probe_machine_script() -> str:
    names = "|".join(FOAM_NAMES)
    return (
        'echo "@@M $(nproc) $(cut -d" " -f1 /proc/loadavg) $HOME"; '
        f"for p in $(ps -eo pid=,comm= | awk '$2 ~ /^({names})$/ "
        "{print $1}'); do "
        'echo "@@P $p $(readlink /proc/$p/cwd 2>/dev/null)"; done')


def parse_job_probe(out: str) -> "dict | None":
    for line in out.splitlines():
        if line.startswith("@@Q "):
            parts = line[4:].split("\x1f")
            if len(parts) < 5:
                return None
            status, pid, alive, lalive, step = parts[:5]
            word, _, rest = status.strip().partition("\t")
            return {"status": word.strip(), "rest": rest.strip(),
                    "pid": int(pid) if pid.isdigit() else 0,
                    "alive": alive == "1", "launch_alive": lalive == "1",
                    "step": step.strip()}
    return None


def parse_machine(out: str) -> "dict | None":
    m = None
    procs = []
    for line in out.splitlines():
        if line.startswith("@@M "):
            bits = line[4:].split(None, 2)
            if len(bits) < 2:
                return None
            try:
                m = {"ncpu": int(bits[0]), "load": float(bits[1]),
                     "home": bits[2].strip() if len(bits) > 2 else ""}
            except ValueError:
                return None
        elif line.startswith("@@P "):
            bits = line[4:].split(None, 1)
            procs.append((bits[0], bits[1].strip() if len(bits) > 1 else ""))
    if m is None:
        return None
    m["procs"] = procs
    return m


def expand(d: str, home: str) -> str:
    return d.replace("$HOME", home) if home else d


def foreign_note(m: "dict | None", own_dirs, own_running: int,
                 frac: float = LOAD_FRAC) -> str:
    """What else runs on the machine, as information (never a wait):
    "foam:<pids>" (OpenFOAM that the queue did not start), "load:<l>/<n>"
    or ""."""
    if m is None:
        return ""
    foreign = []
    for pid, cwd in m["procs"]:
        mine = cwd and any(cwd == d or cwd.startswith(d.rstrip("/") + "/")
                           for d in own_dirs)
        if not mine:
            foreign.append(pid)
    if foreign:
        return "foam:" + ",".join(foreign[:5])
    if not own_running and m["load"] >= frac * max(m["ncpu"], 1):
        return "load:%.2f/%d" % (m["load"], m["ncpu"])
    return ""


def machine_free(m: "dict | None", own_dirs, own_running: int,
                 share: bool, frac: float = LOAD_FRAC) -> "tuple[bool, str]":
    """(free, why) of one machine for one job.

    No limits of hours, cores or load (the user, 2026-11-12: «без лімітів»):
    another run on the machine does not hold a job back; it only goes into
    the message (`foreign_note`). A job waits only when the machine does not
    reply, or when another job of the queue runs there and the job has no
    `share` («parallel on this machine»). The queue never stops or touches a
    run that it did not start; the T43 guard still protects the folder.
    """
    if m is None:
        return False, "unreachable"
    if own_running and not share:
        return False, "queue"
    return True, ""


# --------------------------------------------------------------------------
# The logic (no shell inside: the tests use a fake Shell)
# --------------------------------------------------------------------------

def deps_state(data: dict, job: dict) -> "tuple[str, str]":
    """("ready" | "wait" | "broken", the id that decides)."""
    states = {j.get("id"): j.get("state") for j in data["jobs"]}
    for dep in job.get("after") or []:
        st = states.get(dep)
        if st is None or st == "done":
            continue
        if st in ("failed", "cancelled"):
            return "broken", dep
        return "wait", dep
    return "ready", ""


def machines_of(job: dict) -> "list[tuple[str, str]]":
    """[(host, key)] of each machine that the job uses."""
    out = [(job.get("host", ""), job.get("key", ""))]
    for n in job.get("nodes") or []:
        out.append((n.get("host", ""), n.get("key", "")))
    return out


def candidates(data: dict, now: float) -> list:
    """Queued jobs that may start now if their machines are free."""
    if data.get("paused"):
        return []
    out = []
    for j in data["jobs"]:
        if j.get("state") != "queued":
            continue
        if float(j.get("not_before") or 0) > now:
            continue
        if deps_state(data, j)[0] != "ready":
            continue
        out.append(j)
    return out


def apply_probe(job: dict, got: "dict | None", now: float) -> "str | None":
    """Update a running job from its probe. Returns the new state or None."""
    if got is None:
        job.setdefault("unreachable", now)
        return None
    job.pop("unreachable", None)
    if got.get("step"):
        job["step"] = got["step"]
    st = got["status"]
    if st == "running":
        if got["alive"] or got["launch_alive"]:
            return None
        return _end(job, "failed", "aborted", "", now)
    if st == "ok":
        return _end(job, "done", "", got.get("rest", ""), now)
    if st == "fail":
        return _end(job, "failed", "fail", got.get("rest", ""), now)
    if st == "stop":
        return _end(job, "cancelled", "stopped", got.get("rest", ""), now)
    # No status yet: the script did not write it. Wait while the head lives.
    if got["launch_alive"] or got["alive"]:
        return None
    if now - float(job.get("started") or now) < 30:
        return None
    return _end(job, "failed", "nostart", "", now)


def _end(job: dict, state: str, msg: str, detail: str, now: float) -> str:
    job["state"] = state
    job["msg"] = msg
    if detail:
        job["detail"] = detail[:400]
    job["ended"] = now
    return state


def tick(qdir: str, shell, now: "float | None" = None,
         frac: "float | None" = None) -> list:
    """One pass: read running jobs, end them, start new ones.

    Returns events [(job id, new state)]. The lock is held only while the
    file is read and written; the slow ssh probes run between.
    """
    now = time.time() if now is None else now
    with Lock(_path(qdir, "lock")):
        data = load(qdir)
    frac = float(data.get("load_frac") or LOAD_FRAC) if frac is None else frac
    running = [dict(j) for j in data["jobs"] if j.get("state") == "running"]
    probes = {}
    for j in running:
        rc, out = shell.run(j.get("host", ""),
                            probe_job_script(j["dir"], j.get("pid", 0)),
                            j.get("key", ""), timeout=40)
        probes[j["id"]] = parse_job_probe(out) if rc == 0 else None

    # Machines: those of the candidates and those of the running jobs.
    want = {}
    for j in candidates(data, now) + running:
        for host, key in machines_of(j):
            want.setdefault(host, key)
    cache = data.get("unreach") or {}
    states = {}
    for host, key in want.items():
        last = cache.get(host)
        if last and now - float(last) < UNREACH_RETRY:
            states[host] = None
            continue
        rc, out = shell.run(host, probe_machine_script(), key, timeout=40)
        states[host] = parse_machine(out) if rc == 0 else None

    events = []
    with Lock(_path(qdir, "lock")):
        data = load(qdir)
        for j in data["jobs"]:
            if j.get("state") == "running" and j["id"] in probes:
                new = apply_probe(j, probes[j["id"]], now)
                if new:
                    events.append((j["id"], new))
                    log(qdir, "%s %s %s %s" % (j["id"], new, j.get("msg", ""),
                                               j.get("detail", "")[:120]))
        for j in data["jobs"]:
            if j.get("state") in ("queued", "held"):
                ok, dep = deps_state(data, j)
                if ok == "broken":
                    _end(j, "cancelled", "dep:" + dep, "", now)
                    events.append((j["id"], "cancelled"))
                    log(qdir, "%s cancelled dep %s" % (j["id"], dep))
        events += start_ready(qdir, data, shell, states, now, frac)
        data["unreach"] = {h: (cache.get(h) or now) for h, m in states.items()
                           if m is None}
        data["checked"] = now
        save(qdir, data)
    return events


def _canon(shell, host: str) -> str:
    fn = getattr(shell, "canon", None)
    return fn(host) if fn else host


def start_ready(qdir: str, data: dict, shell, states: dict, now: float,
                frac: float) -> list:
    """Start each candidate whose machines are free. Mutates `data`."""
    events = []
    # The queue's own running jobs per machine, and their folders.
    own = {}
    for j in data["jobs"]:
        if j.get("state") != "running":
            continue
        for host, _key in machines_of(j):
            c = _canon(shell, host)
            own.setdefault(c, []).append(j)
    def note_of(j, mine_of) -> str:
        """Other runs on the machines of `j`: information only."""
        notes = []
        for host, _key in machines_of(j):
            m = states.get(host)
            mine = mine_of(_canon(shell, host))
            home = m["home"] if m else ""
            n = foreign_note(m, [expand(r["dir"], home) for r in mine],
                             len(mine), frac)
            if n and n not in notes:
                notes.append(n)
        return ";".join(notes)

    for j in data["jobs"]:
        if j.get("state") == "running":
            note = note_of(j, lambda c: own.get(c, []))
            if note:
                j["note"] = note
            else:
                j.pop("note", None)
    for j in candidates(data, now):
        mach = machines_of(j)
        ok, why = True, ""
        for host, _key in mach:
            m = states.get(host)
            c = _canon(shell, host)
            mine = own.get(c, [])
            home = m["home"] if m else ""
            dirs = [expand(r["dir"], home) for r in mine]
            free, why = machine_free(m, dirs, len(mine),
                                     bool(j.get("share")), frac)
            if host not in states:
                free, why = False, "unprobed"
            if not free:
                ok = False
                break
        note = note_of(j, lambda c: own.get(c, []))
        if note:
            j["note"] = note
        else:
            j.pop("note", None)
        if not ok:
            if j.get("wait") != why:
                j["wait"] = why
            continue
        j.pop("wait", None)
        cmd = j.get("launch") or launch_command(j["dir"], j["script"], j["id"])
        rc, out = shell.run(j.get("host", ""), cmd, j.get("key", ""),
                            timeout=90)
        if rc != 0:
            msg = "guard" if rc == 42 else "launch"
            _end(j, "failed", msg, _tail(out), now)
            events.append((j["id"], "failed"))
            log(qdir, "%s failed %s rc=%d" % (j["id"], msg, rc))
            continue
        pid = next((int(t) for t in reversed(out.split()) if t.isdigit()), 0)
        j["state"] = "running"
        j["pid"] = pid
        j["started"] = now
        j["msg"] = ""
        j.pop("detail", None)
        events.append((j["id"], "running"))
        log(qdir, "%s start host=%s pid=%d" % (j["id"], j.get("host") or "-",
                                                pid))
        for host, _key in mach:
            own.setdefault(_canon(shell, host), []).append(j)
    return events


def _tail(text: str, n: int = 300) -> str:
    text = (text or "").strip()
    return text[-n:]


# --------------------------------------------------------------------------
# Requests
# --------------------------------------------------------------------------

def _clean_job(raw: dict, index: int, ids: list, known: set) -> dict:
    if not isinstance(raw, dict):
        raise QueueError("job %d: not an object" % index)
    d = str(raw.get("dir", ""))
    if not _DIR_RE.match(d):
        raise QueueError("job %d: bad dir" % index)
    script = str(raw.get("script", ""))
    if not _SCRIPT_RE.match(script):
        raise QueueError("job %d: bad script" % index)
    host = str(raw.get("host", "") or "")
    key = str(raw.get("key", "") or "")
    if not _HOST_RE.match(host) or not _KEY_RE.match(key):
        raise QueueError("job %d: bad host" % index)
    nodes = []
    for n in raw.get("nodes") or []:
        h, k = str(n.get("host", "")), str(n.get("key", "") or "")
        if not h or not _HOST_RE.match(h) or not _KEY_RE.match(k):
            raise QueueError("job %d: bad node" % index)
        nodes.append({"host": h, "key": k})
    after = []
    for a in raw.get("after") or []:
        a = str(a)
        if a.startswith("@"):
            try:
                a = ids[int(a[1:])]
            except (ValueError, IndexError):
                raise QueueError("job %d: bad after %s" % (index, a))
        elif a not in known:
            raise QueueError("job %d: unknown after %s" % (index, a))
        after.append(a)
    try:
        not_before = float(raw.get("not_before") or 0)
    except (TypeError, ValueError):
        raise QueueError("job %d: bad not_before" % index)
    job = {
        "name": str(raw.get("name", "") or os.path.basename(d))[:200],
        "series": str(raw.get("series", "") or "")[:200],
        "host": host, "key": key, "nodes": nodes,
        "dir": d, "script": script,
        "after": after, "not_before": not_before,
        "share": bool(raw.get("share")),
        "state": "held" if raw.get("hold") else "queued",
        "case_dir": str(raw.get("case_dir", "") or "")[:500],
        "via": str(raw.get("via", "") or "")[:200],
        "steps": [str(s)[:80] for s in (raw.get("steps") or [])][:80],
        "meta": raw.get("meta") if isinstance(raw.get("meta"), dict) else {},
        "origin": str(raw.get("origin", "") or "")[:40],
    }
    for k in ("launch", "stop"):
        if raw.get(k):
            job[k] = str(raw[k])
    return job


def add_jobs(data: dict, raws: list, now: float) -> list:
    known = {j.get("id") for j in data["jobs"]}
    ids = []
    for i in range(len(raws)):
        ids.append("j%d" % (int(data.get("next", 1)) + i))
    jobs = [_clean_job(r, i, ids, known | set(ids[:i])) for i, r in
            enumerate(raws)]
    for jid, job in zip(ids, jobs):
        job["id"] = jid
        job["created"] = now
        data["jobs"].append(job)
    data["next"] = int(data.get("next", 1)) + len(raws)
    return ids


def cancel_job(data: dict, jid: str, shell, now: float) -> str:
    j = job_by_id(data, jid)
    st = j.get("state")
    if st in ("queued", "held"):
        _end(j, "cancelled", "cancelled", "", now)
        return "cancelled"
    if st == "running":
        cmd = j.get("stop") or stop_command(j["dir"])
        rc, out = shell.run(j.get("host", ""), cmd, j.get("key", ""),
                            timeout=60)
        if rc != 0:
            raise QueueError("cannot stop %s: %s" % (jid, _tail(out, 200)))
        _end(j, "cancelled", "cancelled", "", now)
        return "cancelled"
    return st


def move_job(data: dict, jid: str, to: int) -> int:
    j = job_by_id(data, jid)
    jobs = data["jobs"]
    jobs.remove(j)
    to = max(0, min(int(to), len(jobs)))
    jobs.insert(to, j)
    return to


def public(job: dict, full: bool = False) -> dict:
    if full:
        return dict(job)
    return {k: v for k, v in job.items() if k not in ("launch", "stop")}


# --------------------------------------------------------------------------
# The daemon and the service
# --------------------------------------------------------------------------

def daemon_alive(qdir: str) -> bool:
    lk = Lock(_path(qdir, "daemon.lock"), blocking=False)
    if lk.acquire():
        lk.release()
        return False
    return True


def daemon_info(qdir: str, unit: str = UNIT) -> dict:
    alive = daemon_alive(qdir)
    info = {"alive": alive}
    try:
        info["heartbeat_age"] = round(
            time.time() - os.path.getmtime(_path(qdir, "heartbeat")), 1)
    except OSError:
        info["heartbeat_age"] = -1
    try:
        with open(_path(qdir, "daemon.pid")) as fh:
            info["pid"] = int(fh.read().strip() or 0)
    except (OSError, ValueError):
        info["pid"] = 0
    info["systemd"] = os.path.isfile(unit_path(unit))
    return info


def run_daemon(qdir: str, shell, poll: float = POLL, once: bool = False) -> int:
    lk = Lock(_path(qdir, "daemon.lock"), blocking=False)
    if not lk.acquire():
        print(json.dumps({"ok": True, "already": True}))
        return 0
    stop = []
    signal.signal(signal.SIGTERM, lambda *_: stop.append(1))
    with open(_path(qdir, "daemon.pid"), "w") as fh:
        fh.write(str(os.getpid()))
    me = os.path.abspath(__file__)
    try:
        my_mtime = os.path.getmtime(me)
    except OSError:
        my_mtime = 0
    log(qdir, "daemon start pid=%d version=%d" % (os.getpid(), VERSION))
    last, last_q = 0.0, None
    while not stop:
        try:
            if os.path.getmtime(me) != my_mtime:
                log(qdir, "daemon new version, restart")
                lk.release()
                os.execv(sys.executable, [sys.executable, me, "daemon"])
        except OSError:
            pass
        try:
            q_mtime = os.path.getmtime(_path(qdir, "queue.json"))
        except OSError:
            q_mtime = None
        now = time.time()
        if now - last >= poll or q_mtime != last_q:
            try:
                tick(qdir, shell, now)
            except Exception:                            # noqa: BLE001
                log(qdir, "tick error: " + traceback.format_exc(limit=3)
                    .replace("\n", " | "))
            last = time.time()
            try:
                last_q = os.path.getmtime(_path(qdir, "queue.json"))
            except OSError:
                last_q = None
            try:
                with open(_path(qdir, "heartbeat"), "w") as fh:
                    fh.write(str(int(last)))
            except OSError:
                pass
        if once:
            break
        time.sleep(WAKE)
    log(qdir, "daemon stop pid=%d" % os.getpid())
    lk.release()
    return 0


def unit_path(unit: str) -> str:
    return os.path.expanduser("~/.config/systemd/user/%s.service" % unit)


def linger_on() -> bool:
    user = os.environ.get("USER") or ""
    try:
        p = subprocess.run(["loginctl", "show-user", user, "-p", "Linger"],
                           stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                           timeout=15)
    except (OSError, subprocess.SubprocessError):
        return False
    return p.stdout.decode().strip().lower() == "linger=yes"


def _systemctl(*args) -> "tuple[int, str]":
    try:
        p = subprocess.run(["systemctl", "--user"] + list(args),
                           stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                           timeout=60)
    except (OSError, subprocess.SubprocessError) as exc:
        return 127, str(exc)
    return p.returncode, p.stdout.decode("utf-8", "replace")


def start_nohup(qdir: str) -> None:
    me = os.path.abspath(__file__)
    env = dict(os.environ)
    env[QUEUE_ENV] = qdir
    with open(_path(qdir, "daemon.out"), "ab") as out:
        subprocess.Popen(["setsid", "nohup", "nice", "-n", "10",
                          sys.executable, me, "daemon"],
                         stdout=out, stderr=subprocess.STDOUT,
                         stdin=subprocess.DEVNULL, env=env,
                         start_new_session=True, close_fds=True)


def ensure(qdir: str, unit: str = UNIT) -> dict:
    info = {"linger": linger_on(), "started": False}
    if daemon_alive(qdir):
        info.update(daemon_info(qdir, unit))
        info["how"] = "systemd" if info.get("systemd") else "nohup"
        return info
    if os.path.isfile(unit_path(unit)):
        rc, out = _systemctl("start", unit)
        info["how"] = "systemd"
        if rc != 0:
            info["systemd_error"] = _tail(out, 200)
            start_nohup(qdir)
            info["how"] = "nohup"
    else:
        start_nohup(qdir)
        info["how"] = "nohup"
    for _ in range(50):
        if daemon_alive(qdir):
            break
        time.sleep(0.1)
    info["started"] = True
    info.update(daemon_info(qdir, unit))
    return info


def stop_daemon(qdir: str, unit: str = UNIT) -> dict:
    if os.path.isfile(unit_path(unit)):
        _systemctl("stop", unit)
    info = daemon_info(qdir, unit)
    pid = info.get("pid") or 0
    if pid and daemon_alive(qdir):
        try:
            os.kill(pid, signal.SIGTERM)
        except OSError:
            pass
        for _ in range(60):
            if not daemon_alive(qdir):
                break
            time.sleep(0.1)
    return {"alive": daemon_alive(qdir)}


def install(qdir: str, unit: str = UNIT) -> dict:
    """A systemd user service, if lingering is on (it is never enabled
    here). Without lingering, the daemon runs with nohup and does not start
    after a reboot of the server."""
    info = {"linger": linger_on(), "systemd": False}
    rc, out = _systemctl("is-system-running")
    word = out.strip().splitlines()[-1] if out.strip() else ""
    if word not in ("running", "degraded") or not info["linger"]:
        info["why"] = "linger_off" if not info["linger"] else "no_systemd"
        info.update(ensure(qdir, unit))
        return info
    me = os.path.abspath(__file__)
    text = "\n".join([
        "[Unit]",
        "Description=OpenFOAM Studio run queue (T46)",
        "",
        "[Service]",
        "Type=simple",
        "Environment=%s=%s" % (QUEUE_ENV, qdir),
        "ExecStart=%s %s daemon" % (sys.executable, me),
        "Restart=on-failure",
        "RestartSec=30",
        # The runs that the daemon started must live on when the daemon
        # stops or gets a new version.
        "KillMode=process",
        "Nice=10",
        "",
        "[Install]",
        "WantedBy=default.target",
        "",
    ])
    path = unit_path(unit)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as fh:
        fh.write(text)
    stop_daemon(qdir, unit="-none-")         # a nohup daemon gives way
    _systemctl("daemon-reload")
    rc, out = _systemctl("enable", "--now", unit)
    info["systemd"] = rc == 0
    if rc != 0:
        info["why"] = "systemctl: " + _tail(out, 200)
        info.update(ensure(qdir, unit))
        return info
    for _ in range(50):
        if daemon_alive(qdir):
            break
        time.sleep(0.1)
    info.update(daemon_info(qdir, unit))
    info["how"] = "systemd"
    return info


def uninstall(qdir: str, unit: str = UNIT) -> dict:
    path = unit_path(unit)
    if os.path.isfile(path):
        _systemctl("disable", "--now", unit)
        os.remove(path)
        _systemctl("daemon-reload")
    return stop_daemon(qdir, unit="-none-")


# --------------------------------------------------------------------------
# Command line
# --------------------------------------------------------------------------

def _parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="fs_queue_runner",
                                description="OpenFOAM Studio run queue")
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("version")
    a = sub.add_parser("add", help="jobs as JSON on stdin")
    a.add_argument("--file", default="-")
    ls = sub.add_parser("list")
    ls.add_argument("--full", action="store_true")
    for name in ("cancel", "hold", "release", "remove"):
        c = sub.add_parser(name)
        c.add_argument("ids", nargs="*")
        if name == "remove":
            c.add_argument("--finished", action="store_true")
    mv = sub.add_parser("move")
    mv.add_argument("id")
    mv.add_argument("--to", type=int)
    mv.add_argument("--up", action="store_true")
    mv.add_argument("--down", action="store_true")
    mv.add_argument("--top", action="store_true")
    sub.add_parser("pause")
    sub.add_parser("resume")
    m = sub.add_parser("machines")
    m.add_argument("hosts", nargs="*")
    lg = sub.add_parser("log")
    lg.add_argument("id")
    lg.add_argument("-n", type=int, default=40)
    for name in ("ensure", "install", "uninstall", "stop-daemon"):
        s = sub.add_parser(name)
        s.add_argument("--unit", default=UNIT)
    d = sub.add_parser("daemon")
    d.add_argument("--once", action="store_true")
    d.add_argument("--poll", type=float, default=POLL)
    t = sub.add_parser("tick")
    t.add_argument("--now", type=float)
    return p


def main(argv=None, shell=None, out=None) -> int:
    out = out or sys.stdout
    args = _parser().parse_args(argv)
    qdir = queue_dir()
    os.makedirs(qdir, exist_ok=True)
    shell = shell or Shell()
    now = time.time()

    def say(obj) -> int:
        out.write(json.dumps(obj, ensure_ascii=False) + "\n")
        return 0 if obj.get("ok", True) else 1

    try:
        if args.cmd == "version":
            return say({"ok": True, "version": VERSION})
        if args.cmd == "daemon":
            return run_daemon(qdir, shell, args.poll, args.once)
        if args.cmd == "tick":
            ev = tick(qdir, shell, args.now)
            return say({"ok": True, "events": ev})
        if args.cmd == "ensure":
            return say(dict(ok=True, **ensure(qdir, args.unit)))
        if args.cmd == "install":
            return say(dict(ok=True, **install(qdir, args.unit)))
        if args.cmd == "uninstall":
            return say(dict(ok=True, **uninstall(qdir, args.unit)))
        if args.cmd == "stop-daemon":
            return say(dict(ok=True, **stop_daemon(qdir, args.unit)))
        if args.cmd == "machines":
            hosts = args.hosts or [""]
            res = {}
            for h in hosts:
                if not _HOST_RE.match(h):
                    raise QueueError("bad host")
                rc, text = shell.run(h, probe_machine_script(), timeout=40)
                m = parse_machine(text) if rc == 0 else None
                free, why = machine_free(m, [], 0, False)
                res[h or "."] = {"reachable": m is not None, "free": free,
                                 "why": why, "note": foreign_note(m, [], 0),
                                 "ncpu": m["ncpu"] if m else 0,
                                 "load": m["load"] if m else 0.0,
                                 "foam": len(m["procs"]) if m else 0}
            return say({"ok": True, "machines": res})
        if args.cmd == "log":
            with Lock(_path(qdir, "lock")):
                j = job_by_id(load(qdir), args.id)
            n = max(1, min(int(args.n), 5000))
            rc, text = shell.run(j.get("host", ""),
                                 'tail -n %d "%s/studio/run.log" 2>&1' %
                                 (n, j["dir"]), j.get("key", ""), timeout=60)
            return say({"ok": rc == 0, "id": j["id"], "log": text})
        with Lock(_path(qdir, "lock")):
            data = load(qdir)
            res = {"ok": True}
            if args.cmd == "list":
                res.update(paused=bool(data.get("paused")), version=VERSION,
                           now=now, checked=data.get("checked", 0),
                           jobs=[public(j, args.full) for j in data["jobs"]],
                           daemon=daemon_info(qdir))
                return say(res)
            if args.cmd == "add":
                text = (sys.stdin.read() if args.file == "-"
                        else open(args.file, encoding="utf-8").read())
                try:
                    raw = json.loads(text)
                except ValueError:
                    raise QueueError("add: the input is not JSON")
                raws = raw if isinstance(raw, list) else [raw]
                if not raws:
                    raise QueueError("add: no job")
                res["ids"] = add_jobs(data, raws, now)
                log(qdir, "add " + ",".join(res["ids"]))
            elif args.cmd == "cancel":
                res["states"] = {i: cancel_job(data, i, shell, now)
                                 for i in args.ids}
                log(qdir, "cancel " + ",".join(args.ids))
            elif args.cmd in ("hold", "release"):
                res["states"] = {}
                for i in args.ids:
                    j = job_by_id(data, i)
                    if args.cmd == "hold" and j["state"] == "queued":
                        j["state"] = "held"
                        j.pop("wait", None)
                    elif args.cmd == "release" and j["state"] == "held":
                        j["state"] = "queued"
                    res["states"][i] = j["state"]
            elif args.cmd == "remove":
                drop = set(args.ids)
                if args.finished:
                    drop |= {j["id"] for j in data["jobs"]
                             if j.get("state") in FINAL}
                for i in args.ids:
                    if job_by_id(data, i).get("state") not in FINAL:
                        raise QueueError("remove: %s is not finished" % i)
                data["jobs"] = [j for j in data["jobs"]
                                if j.get("id") not in drop]
                res["removed"] = sorted(drop)
            elif args.cmd == "move":
                ids = [j["id"] for j in data["jobs"]]
                at = ids.index(job_by_id(data, args.id)["id"])
                if args.top:
                    to = 0
                elif args.up:
                    to = at - 1
                elif args.down:
                    to = at + 1
                elif args.to is not None:
                    to = args.to
                else:
                    raise QueueError("move: --to, --up, --down or --top")
                res["index"] = move_job(data, args.id, to)
            elif args.cmd in ("pause", "resume"):
                data["paused"] = args.cmd == "pause"
                res["paused"] = data["paused"]
                log(qdir, args.cmd)
            save(qdir, data)
            return say(res)
    except QueueError as exc:
        return say({"ok": False, "error": str(exc)})


if __name__ == "__main__":
    sys.exit(main())
