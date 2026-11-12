"""Watch the server runs and find them again after a sleep (T44). No Qt.

The window (fs_recover) and later a run queue or a command line tool use
these parts:

- `probe(host, dirs)`: the state of some sessions, one ssh call. None when
  the server does not reply (sleep, no network, Tailscale not ready).
- `discover(host)`: all sessions on the server (`list_sessions`), one ssh
  call. None when the server does not reply.
- `state(session)`: "running", "ok", "fail", "stop", "aborted".
- `ours(session)`: the run was started from this computer (its case folder
  is here).
- `Backoff`: when to ask a server next: each minute, slower without an
  answer, quickly after a wake-up (`hurry`).
- `ClockJump`: finds a sleep of the computer from a jump of the wall clock
  between two timer ticks (portable: Windows Modern Standby, macOS, Linux).
- `Seen`: the run ends that the program told about already (a small JSON
  file next to the settings), so a notification comes one time only.
- `pick(sessions, shown_case)`: the run that the window connects to by
  itself.

The state on the server is studio/status + studio/pid (the head lives by the
rule of fs_caseguard.head_alive) + studio/fetched (T43), read by
fs_backend._session_probe.
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path

import fs_backend as be

# How often to ask a server about the runs that we watch (s).
PERIOD = 60
# After a wake-up or a new network: Tailscale can need some seconds.
FAST = (5, 10, 20, 40, 60)
# The server does not reply (the laptop is away from the network).
SLOW = (60, 120, 240, 480, 600)
# A tick of the window timer (s) and the jump that means a sleep.
TICK = 15
JUMP = 45
# Notes about run ends: kept so long (s).
SEEN_DAYS = 7


def key(host: str, remote_dir: str) -> str:
    """One id for a session: the host and the folder name. The folder can
    be written with $HOME or as the full path; the name is the same."""
    return f"{host}|{Path(str(remote_dir).rstrip('/')).name}"


def session_key(s: "be.RemoteSession") -> str:
    return key(s.host, s.remote_dir)


def state(s: "be.RemoteSession") -> str:
    """The state of a session as an ASCII id."""
    if s.stale:
        return "aborted"
    if s.running:
        return "running"
    return s.status if s.status in ("ok", "fail", "stop") else "fail"


def ours(s: "be.RemoteSession", exists=os.path.isdir) -> bool:
    """The run was started from this computer: its case folder is here."""
    return bool(s.case_dir) and bool(exists(s.case_dir))


def _quote(d: str) -> str:
    # $HOME stays a variable for the shell; the rest is a plain word.
    return '"' + str(d).replace('"', "") + '"'


def _ask(host: str, words: str, run=None) -> "list[be.RemoteSession] | None":
    run = run or be._run
    rc, out = run(be.ssh_argv(host, be._session_probe(words)), timeout=30)
    if rc != 0:
        return None
    return [s for s in (be._parse_session(ln, host)
                        for ln in out.splitlines() if be._FS in ln) if s]


def probe(host: str, dirs, run=None) -> "list[be.RemoteSession] | None":
    """The sessions in these folders now. None: the server did not reply."""
    dirs = [d for d in dirs if d]
    if not dirs:
        return []
    return _ask(host, " ".join(_quote(d) for d in dirs), run=run)


def discover(host: str, run=None) -> "list[be.RemoteSession] | None":
    """All sessions on the server. None: the server did not reply."""
    return _ask(host, f'"{be.SESSION_ROOT}"/*', run=run)


class Backoff:
    """When to ask each server next. All times are `now` values (s)."""

    def __init__(self, period: float = PERIOD, fast=FAST, slow=SLOW):
        self.period = period
        self.fast = tuple(fast)
        self.slow = tuple(slow)
        self._next: dict[str, float] = {}
        self._fails: dict[str, int] = {}
        self._hurry: dict[str, bool] = {}

    def hosts(self) -> list[str]:
        return list(self._next)

    def add(self, host: str, now: float, delay: float = 0.0) -> None:
        if host not in self._next:
            self._next[host] = now + delay
            self._fails[host] = 0
            self._hurry[host] = False

    def drop(self, host: str) -> None:
        for d in (self._next, self._fails, self._hurry):
            d.pop(host, None)

    def due(self, now: float) -> list[str]:
        return [h for h, t in self._next.items() if t <= now]

    def ok(self, host: str, now: float) -> None:
        if host in self._next:
            self._fails[host] = 0
            self._hurry[host] = False
            self._next[host] = now + self.period

    def fail(self, host: str, now: float) -> float:
        """No reply: the next try later. Returns the pause."""
        if host not in self._next:
            return 0.0
        n = self._fails[host]
        if self._hurry[host] and n < len(self.fast):
            pause = self.fast[n]
        else:
            if self._hurry[host]:
                n -= len(self.fast)
            pause = self.slow[min(max(n, 0), len(self.slow) - 1)]
        self._fails[host] += 1
        self._next[host] = now + pause
        return pause

    def hurry(self, host: str, now: float) -> None:
        """Ask now, then quickly (after a wake-up or a new network)."""
        self.add(host, now)
        self._fails[host] = 0
        self._hurry[host] = True
        self._next[host] = now

    def failures(self, host: str) -> int:
        return self._fails.get(host, 0)


class ClockJump:
    """A sleep of the computer, seen from a timer.

    A timer does not run while the computer sleeps. The tick after the
    wake-up sees that much more wall-clock time went by than the period of
    the timer. This works on each system, also with Windows Modern Standby
    (no power broadcast for a program window there).
    """

    def __init__(self, period: float = TICK, slack: float = JUMP,
                 clock=time.time):
        self.period = period
        self.slack = slack
        self.clock = clock
        self.last = clock()

    def tick(self) -> float:
        """Seconds of sleep since the last tick (0: no sleep)."""
        now = self.clock()
        gap = now - self.last
        self.last = now
        if gap > self.period + self.slack or gap < -self.slack:
            return max(gap - self.period, 0.0) or self.slack
        return 0.0


class Seen:
    """The run ends that the program told about (one notification each)."""

    FILE = "runs-seen.json"

    def __init__(self, path: "str | os.PathLike | None" = None,
                 clock=time.time):
        if path is None:
            import fs_settings
            path = fs_settings.path().parent / self.FILE
        self.path = Path(path)
        self.clock = clock
        self._data: dict[str, dict] = {}
        self._load()

    def _load(self) -> None:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            self._data = data if isinstance(data, dict) else {}
        except (OSError, ValueError):
            self._data = {}

    def _save(self) -> None:
        limit = self.clock() - SEEN_DAYS * 86400
        self._data = {k: v for k, v in self._data.items()
                      if isinstance(v, dict) and v.get("at", 0) >= limit}
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(".tmp")
            tmp.write_text(json.dumps(self._data, ensure_ascii=False,
                                      indent=1), encoding="utf-8")
            os.replace(tmp, self.path)
        except OSError:
            pass

    @staticmethod
    def _end(s: "be.RemoteSession", now: float) -> float:
        return now - s.age if s.age >= 0 else 0.0

    def told(self, s: "be.RemoteSession") -> bool:
        """The program told about this end already."""
        got = self._data.get(session_key(s))
        if not got or got.get("state") != state(s):
            return False
        # The same folder can end again later (a new run in it).
        end = self._end(s, self.clock())
        return not end or not got.get("end") or abs(end - got["end"]) < 900

    def tell(self, s: "be.RemoteSession") -> None:
        now = self.clock()
        self._data[session_key(s)] = {"state": state(s),
                                      "end": self._end(s, now), "at": now}
        self._save()


def pick(sessions, shown_case: str = "", exists=os.path.isdir):
    """The session that the window connects to by itself, or None.

    Only runs of this computer. First a run of the case that the window
    shows; then the newest run that is running; then the newest finished
    run whose results are not downloaded yet.
    """
    def same(a: str, b: str) -> bool:
        return bool(a and b) and (os.path.normcase(os.path.abspath(a))
                                  == os.path.normcase(os.path.abspath(b)))

    mine = [s for s in sessions if ours(s, exists)]
    live = [s for s in mine if state(s) == "running"]
    # Not old ones: a run that ended days ago is not a run of this sleep.
    fetch = [s for s in mine if state(s) == "ok" and not s.fetched
             and s.worth_greeting()]
    for group in (live, fetch):
        for s in group:
            if same(s.case_dir, shown_case):
                return s
    for group in (live, fetch):
        if group:
            return max(group, key=lambda s: s.started or "")
    return None
