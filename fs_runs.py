"""The data of the «Runs» window (T49b). No Qt.

One window shows two parts:

- the plan: the jobs of the queue that did not start yet (queued, held, and
  the ones that ended without a start, for example a cancelled chain);
- the runs that are on the servers: each session folder in
  ~/openfoam-studio of each known server (fs_runwatch.discover, one ssh call
  per server), together with the queue jobs that started.

A queue job that started has a session folder (studio/ of its run). The job
and the session are ONE row (`Row`): the key is the machine and the folder
name (fs_runwatch.key). A started job whose machine did not reply (or that
is not on a known server) is a row from the job only.

`remove_command` deletes a run folder on a server only through the guard of
fs_caseguard (T43): never a live run, never a path outside
~/openfoam-studio.
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from pathlib import Path

import fs_backend as be
import fs_caseguard as guard
import fs_queue as fq
import fs_runwatch as rw
from fs_i18n import tr


@dataclass
class Row:
    """One run on a machine: a session, a queue job, or both."""
    host: str
    session: "be.RemoteSession | None" = None
    job: "dict | None" = None

    @property
    def key(self) -> str:
        return rw.key(self.host, self.remote_dir)

    @property
    def name(self) -> str:
        if self.session is not None:
            return self.session.name
        return str((self.job or {}).get("name", ""))

    @property
    def remote_dir(self) -> str:
        if self.session is not None:
            return self.session.remote_dir
        return str((self.job or {}).get("dir", ""))

    @property
    def case_dir(self) -> str:
        if self.session is not None and self.session.case_dir:
            return self.session.case_dir
        return str((self.job or {}).get("case_dir", ""))

    @property
    def state(self) -> str:
        """An ASCII id: running, ok, fail, stop, aborted, or a queue state
        (when the machine of the job did not reply)."""
        if self.session is not None:
            return rw.state(self.session)
        st = str((self.job or {}).get("state", ""))
        return {"done": "ok", "failed": "fail",
                "cancelled": "stop"}.get(st, st)

    @property
    def running(self) -> bool:
        return self.state == "running"

    @property
    def ended(self) -> bool:
        return self.state in ("ok", "fail", "stop", "aborted")

    @property
    def fetched(self) -> "bool | None":
        """True / False for an ended session; None when not known."""
        if self.session is None or not self.ended:
            return None
        return bool(self.session.fetched)

    @property
    def job_id(self) -> str:
        return str((self.job or {}).get("id", ""))

    def started_text(self) -> str:
        if self.session is not None and self.session.started:
            return self.session.started[:16]
        return fq.fmt_time((self.job or {}).get("started") or 0)

    def ended_at(self, now: float) -> float:
        if self.session is not None:
            if self.ended and self.session.age >= 0:
                return now - self.session.age
            return 0.0
        return float((self.job or {}).get("ended") or 0)

    def step_text(self) -> str:
        if self.session is not None and self.session.step:
            return self.session.step_text
        step = (self.job or {}).get("step", "")
        return be.step_title(step) if step else ""

    def lines(self) -> str:
        if self.session is not None:
            return str(self.session.log_lines)
        return ""

    def as_job(self, queue: str) -> dict:
        """The row as a job for fs_queuewin.fetch_jobs (the download)."""
        j = dict(self.job or {})
        j.setdefault("name", self.name)
        j["dir"] = self.remote_dir
        j["case_dir"] = self.case_dir
        j["via"] = self.host
        j.setdefault("host", "" if self.host == queue else self.host)
        return j


def state_text(state: str) -> str:
    """The state of a row for the person."""
    texts = {
        "running": tr("running", ctx="queue"),
        "ok": tr("done", ctx="queue"),
        "fail": tr("failed", ctx="queue"),
        "stop": tr("stopped"),
        "aborted": tr("aborted"),
    }
    if state in texts:
        return texts[state]
    return fq.state_text(state)


def results_text(row: Row) -> str:
    """Are the results of the row on this computer?"""
    f = row.fetched
    if f is None:
        return ""
    return tr("downloaded") if f else tr("not downloaded")


def message_text(row: Row) -> str:
    parts = []
    if row.session is not None and row.session.message:
        parts.append(row.session.message)
    elif row.job is not None:
        text = fq.job_text(row.job)
        if text:
            parts.append(text)
    if row.session is not None and row.session.stale:
        parts.append(tr("no process on the server (a reboot or no memory)"))
    if row.job_id:
        parts.append(tr("queue job {id}", id=row.job_id))
    return " · ".join(str(p) for p in parts)


def job_host(job: dict, queue: str) -> str:
    """The ssh name of the machine of a job, as this computer reaches it."""
    return str(job.get("via") or job.get("host") or queue)


def planned(jobs: list) -> list:
    """The jobs of the plan: not started yet."""
    return [j for j in jobs if not j.get("started")]


def merge(jobs: list, found: "dict[str, list | None]",
          queue: str) -> "list[Row]":
    """The rows of the server part: the sessions of each server and the
    queue jobs that started, each run once. Newest first."""
    rows: dict[str, Row] = {}
    for host, sessions in found.items():
        for s in sessions or []:
            r = Row(host=host, session=s)
            rows[r.key] = r
    for j in jobs:
        if not j.get("started"):
            continue
        host = job_host(j, queue)
        k = rw.key(host, j.get("dir", ""))
        if k in rows:
            if rows[k].job is None or (rows[k].job.get("started") or 0) \
                    <= (j.get("started") or 0):
                rows[k].job = j
        else:
            rows[k] = Row(host=host, job=j)

    def order(r: Row):
        return (r.started_text(), r.name)
    return sorted(rows.values(), key=order, reverse=True)


def hosts_of(jobs: list, queue: str, more=()) -> list:
    """The servers to ask: the given ones, the queue server and the machines
    of the started queue jobs. Each name once, the order kept."""
    out: list[str] = []

    def add(h):
        h = str(h or "").strip()
        if h and h not in out:
            out.append(h)
    for h in more:
        add(h)
    add(queue)
    for j in jobs:
        if j.get("started"):
            add(job_host(j, queue))
    return out


def scan(hosts, discover=None, timeout: float = 60.0) -> "dict[str, list | None]":
    """All sessions of each server, one ssh call per server, the servers in
    parallel. {host: [RemoteSession] or None (no reply)}."""
    discover = discover or rw.discover
    got: dict[str, "list | None"] = {}
    lock = threading.Lock()

    def one(h):
        try:
            res = discover(h)
        except Exception:                                  # noqa: BLE001
            res = None
        with lock:
            got[h] = res
    threads = [threading.Thread(target=one, args=(h,), daemon=True,
                                name="runs-scan") for h in hosts]
    for t in threads:
        t.start()
    end = time.monotonic() + timeout
    for t in threads:
        t.join(max(0.0, end - time.monotonic()))
    with lock:
        return {h: got.get(h) for h in hosts}


def remove_command(remote_dir: str, own: bool = False) -> str:
    """bash: delete one run folder through the guard of fs_caseguard.

    The guard refuses a live run, a path that is not one folder in
    ~/openfoam-studio, and (`own` False) results that were not downloaded.
    """
    name = guard.check_name(Path(str(remote_dir).rstrip("/")).name)
    folder = be.remote_dir_for(name)
    return guard.guarded(folder, f'rm -rf -- "{folder}" && echo removed',
                         own=own)


def remove_run(host: str, remote_dir: str, own: bool = False,
               run=None) -> None:
    """Delete the run folder on `host`. Raises fs_caseguard.CaseBusy when
    the guard refuses (nothing is deleted), RuntimeError on other errors."""
    run = run or be._run
    rc, out = run(be.ssh_argv(host, remove_command(remote_dir, own)),
                  timeout=120)
    name = Path(str(remote_dir).rstrip("/")).name
    got = guard.parse_guard(out) if rc == guard.GUARD_EXIT else None
    if got:
        raise guard.CaseBusy(got[0], got[1], be.remote_shown(name),
                             host)
    if rc != 0 or "removed" not in out:
        raise RuntimeError(tr("cannot delete {name} on {host}: {why}",
                              name=name, host=host,
                              why=out.strip()[-200:] or tr("code {rc}", rc=rc)))


@dataclass
class Snapshot:
    """The last read of the servers, for the window and the run watch."""
    found: dict = field(default_factory=dict)       # host -> list | None
    at: float = 0.0

    def update(self, found: dict, now: "float | None" = None) -> None:
        self.found.update(found)
        self.at = time.time() if now is None else now
