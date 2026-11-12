"""The window finds its server runs again by itself, and tells about their
end (T44).

Before T44: after each sleep of the laptop the person clicked «Connect to
Run», waited for the server check, selected the run in a table and clicked
«Connect» (4-5 actions). Nothing told that a run had ended.

`RunGuard` (one per main window):

- Notifications (fs_notify): the end, a failure, a stop of a run, the
  download of the results. A tray message (a Windows toast) when the person
  does not look at the window, and a line in the status bar. A click opens
  the case or connects to the run.
- Watch: the server runs of this computer that the window does not follow.
  One ssh call per server per minute (fs_runwatch.Backoff; slower when the
  server does not reply).
- Self-recovery: at start, after a sleep (a jump of the wall clock between
  two ticks, fs_runwatch.ClockJump) and after a new network
  (QNetworkInformation), the guard asks the servers (quickly first:
  Tailscale can need some seconds) and connects to the run of this computer
  by the path of «Connect to Run» (MainWindow.start_attach), without a
  dialog. A run that the window follows connects again by itself
  (fs_backend.Runner.attach); after a sleep the guard makes it do that at
  once (Runner.reconnect_now). Unsaved changes in the scene: the guard does
  not replace the scene, it only tells.
- No message boxes: a server that does not reply is a normal state.

The servers: the «Server» setting, the server of the case in the window
(studio.json, fs_backend.run_server_dir, T43), the servers of the watched
runs and the server of the followed run.
"""

from __future__ import annotations

import os
import threading
import time
from pathlib import Path

from PySide6 import QtCore

import fs_backend as be
import fs_i18n
import fs_notify
import fs_runwatch as rw
import fs_settings as fset
from fs_i18n import tr

fs_i18n.install()

# Settings keys (fs_settings.DEFAULTS).
AUTO = "auto_reconnect"


class RunGuard(QtCore.QObject):
    probed = QtCore.Signal(str, object, str)      # host, sessions|None, mode
    found = QtCore.Signal(object, object, bool)   # backend|None, session, user

    def __init__(self, win, clock=time.time):
        super().__init__(win)
        self.win = win
        self.clock = clock
        self.backoff = rw.Backoff()
        self.jump = rw.ClockJump(clock=clock)
        self.seen = rw.Seen(clock=clock)
        # Runs of this computer that run on a server, and that the window
        # does not follow: {key: RemoteSession}.
        self.watch: "dict[str, be.RemoteSession]" = {}
        self._recover: set[str] = set()     # hosts to ask in full
        self._asking: set[str] = set()      # hosts with a probe in a thread
        self._user_stop = False
        self._started = False
        self._waits = 0
        self.notifier = fs_notify.install(win, self.open_note)
        self.probed.connect(self._on_probed)
        self.found.connect(self._on_found)
        self.timer = QtCore.QTimer(self)
        self.timer.setInterval(int(rw.TICK * 1000))
        self.timer.timeout.connect(self.tick)
        self._net = None
        self._hook_network()

    # --- start, tick, wake-up --------------------------------------------

    def start(self) -> None:
        """At program start: after the session restore, find the runs."""
        if self._started:
            return
        if not self._window_ready() and self._waits < 120:
            self._waits += 1
            QtCore.QTimer.singleShot(500, self.start)
            return
        self._started = True
        self.jump.last = self.clock()
        self.timer.start()
        self.recover("start")

    def stop(self) -> None:
        self.timer.stop()
        tray = getattr(self.notifier, "tray", None)
        if tray is not None:
            tray.hide()

    def tick(self) -> None:
        if self.jump.tick():
            self.recover("resume")
        self._poll()

    def _hook_network(self) -> None:
        """A new network (QNetworkInformation): find the runs again."""
        try:
            from PySide6.QtNetwork import QNetworkInformation as NI
            if NI.instance() is None and not NI.loadDefaultBackend():
                return
            net = NI.instance()
            if net is None:
                return
            net.reachabilityChanged.connect(self._reachability)
            self._net = net
        except Exception:                                  # noqa: BLE001
            self._net = None

    def _reachability(self, value) -> None:
        try:
            from PySide6.QtNetwork import QNetworkInformation as NI
            online = value == NI.Reachability.Online
        except Exception:                                  # noqa: BLE001
            online = True
        if online and self._started:
            # Tailscale needs some seconds after the network.
            QtCore.QTimer.singleShot(3000, lambda: self.recover("network"))

    def recover(self, why: str = "start") -> None:
        """Ask the servers now (and quickly again); connect to our run."""
        now = self.clock()
        if why in ("resume", "network"):
            self._kick_followed()
        hosts = self._hosts()
        for host in hosts:
            self.backoff.hurry(host, now)
        self._recover |= set(hosts)
        self._poll()

    def _kick_followed(self) -> None:
        w = self.win
        runner = getattr(w.worker, "runner", None) if w.worker else None
        if (runner is not None and getattr(runner, "remote_dir", "")
                and self._run_busy()):
            try:
                runner.reconnect_now()
            except Exception:                              # noqa: BLE001
                pass

    # --- which servers -----------------------------------------------------

    def _hosts(self) -> list[str]:
        hosts: list[str] = []

        def add(h):
            h = str(h or "").strip()
            if h and h not in hosts:
                hosts.append(h)
        add(fset.get("ssh_host"))
        try:
            add(self.win.panel_run.ed_server.text())
        except (AttributeError, RuntimeError):
            pass
        case = getattr(self.win, "case_dir", "") or ""
        if case and os.path.isdir(case):
            try:
                add(be.run_server_dir(case)[0])
            except Exception:                              # noqa: BLE001
                pass
        for s in self.watch.values():
            add(s.host)
        return hosts

    # --- asking --------------------------------------------------------------

    def _poll(self) -> None:
        now = self.clock()
        for host in self.backoff.due(now):
            if host in self._asking:
                continue
            full = host in self._recover
            dirs = [s.remote_dir for s in self.watch.values()
                    if s.host == host]
            if not full and not dirs:
                self.backoff.drop(host)
                continue
            self._asking.add(host)
            threading.Thread(target=self._ask, args=(host, dirs, full),
                             daemon=True, name="run-watch").start()

    def _ask(self, host: str, dirs: list, full: bool) -> None:
        try:
            got = rw.discover(host) if full else rw.probe(host, dirs)
        except Exception:                                  # noqa: BLE001
            got = None
        try:
            self.probed.emit(host, got, "recover" if full else "watch")
        except RuntimeError:                               # window is gone
            pass

    def _followed(self) -> str:
        """The key of the server run that the window follows now, or ""."""
        w = self.win
        if not self._run_busy() or w.worker is None:
            return ""
        b = getattr(w.worker, "backend", None)
        if getattr(b, "kind", "") != "ssh":
            return ""
        runner = getattr(w.worker, "runner", None)
        d = (getattr(w.worker, "attach_to", "")
             or (runner.remote_dir if runner is not None else "")
             or getattr(w.worker, "native_dir", "") or "")
        return rw.key(b.host, d) if d else ""

    @QtCore.Slot(str, object, str)
    def _on_probed(self, host: str, got, mode: str) -> None:
        self._asking.discard(host)
        if getattr(self.win, "_closing", False):
            return
        now = self.clock()
        if got is None:
            self.backoff.fail(host, now)
            return
        self.backoff.ok(host, now)
        followed = self._followed()
        seen_keys = set()
        target = None
        if mode == "recover":
            self._recover.discard(host)
            target = self._target(got, followed)
        elif fset.get(AUTO, True) and not followed and self._idle():
            # A watched run of the case in the window ended: get its
            # results into that case (the scene stays).
            target = next((s for s in got if rw.state(s) == "ok"
                           and not s.fetched and self._shows(s.case_dir)
                           and rw.session_key(s) in self.watch), None)
        for s in got:
            k = rw.session_key(s)
            seen_keys.add(k)
            if k == followed:
                self.watch.pop(k, None)
                continue
            st = rw.state(s)
            if st == "running":
                if k in self.watch or (mode == "recover" and rw.ours(s)):
                    self.watch[k] = s
                continue
            was = self.watch.pop(k, None) is not None
            fresh = mode == "recover" and rw.ours(s) and s.worth_greeting()
            if (was or fresh) and not self.seen.told(s) and s is not target:
                self.tell_end(s)
        if mode == "watch":
            for k in [k for k, s in self.watch.items()
                      if s.host == host and k not in seen_keys]:
                self.watch.pop(k)          # the folder is gone
        if target is not None:
            self._auto_connect(target)
        if not any(s.host == host for s in self.watch.values()) \
                and host not in self._recover:
            self.backoff.drop(host)

    # --- connect by itself ---------------------------------------------------

    def _target(self, got, followed: str):
        """The run that the window connects to after this reply, or None."""
        if not fset.get(AUTO, True) or followed or not self._idle():
            return None
        shown = self.win.case_dir if self._shows(self.win.case_dir) else ""
        return rw.pick([s for s in got if rw.session_key(s) != followed],
                       shown_case=shown or "")

    def _auto_connect(self, s: "be.RemoteSession") -> None:
        w = self.win
        if w.project_is_dirty() and not self._shows(s.case_dir):
            if rw.state(s) == "running":
                text = tr("{name} runs on {host}. The scene has unsaved "
                          "changes, so the program did not connect. Click "
                          "to connect.", name=s.name, host=s.host)
            else:
                text = tr("{name} on {host} is done, the results are not "
                          "downloaded. The scene has unsaved changes. Click "
                          "to get the results.", name=s.name, host=s.host)
            if rw.state(s) != "running":
                self.seen.tell(s)
            fs_notify.notify("info", tr("A run of this computer"), text,
                             case=s.case_dir,
                             action=("attach", s.host, s.remote_dir))
            return
        self.connect_run(s, user=False)

    def connect_run(self, s: "be.RemoteSession", user: bool) -> None:
        """Connect to the run `s` like «Connect to Run», without a table."""
        b = self._known_backend(s.host)
        if b is not None:
            self._on_found(b, s, user)
            return

        def job():
            try:
                b2 = self.find_backend(s.host)
            except Exception:                              # noqa: BLE001
                b2 = None
            try:
                self.found.emit(b2, s, user)
            except RuntimeError:
                pass
        threading.Thread(target=job, daemon=True, name="run-find").start()

    def find_backend(self, host: str):
        """A ready ssh backend for `host` (a thread: ssh takes seconds)."""
        found = [b for b in be.detect_ssh(
            [host], be.containers_setting(fset.get("ssh_containers")))
            if b.ready]
        return found[0] if found else None

    def _known_backend(self, host: str):
        try:
            cmb = self.win.panel_run.cmb_backend
        except AttributeError:
            return None
        for i in range(cmb.count()):
            b = cmb.itemData(i)
            if (b is not None and getattr(b, "kind", "") == "ssh"
                    and b.ready and b.host == host):
                return b
        return None

    @QtCore.Slot(object, object, bool)
    def _on_found(self, backend, s, user: bool) -> None:
        w = self.win
        if getattr(w, "_closing", False):
            return
        if backend is None:
            if user:
                w.statusBar().showMessage(
                    tr("{host} does not reply now. The program asks again "
                       "by itself.", host=s.host), 10000)
            # Ask again later (quickly): the network can come back.
            self.backoff.hurry(s.host, self.clock() + rw.FAST[0])
            self._recover.add(s.host)
            return
        if not user and not self._idle():
            return
        self.watch.pop(rw.session_key(s), None)
        w.start_attach(backend, s)
        if not user and self._run_busy():
            # After start_attach: it clears the log.
            w.log.push(tr("The program connected to this run by itself "
                          "(start, a sleep of the computer or a new "
                          "network)."))

    # --- the state of the window ---------------------------------------------

    def _run_busy(self) -> bool:
        th = getattr(self.win, "thread", None)
        try:
            return th is not None and th.isRunning()
        except RuntimeError:
            return False

    def _window_ready(self) -> bool:
        w = self.win
        return (not getattr(w, "_detecting", False)
                and getattr(w, "_load_thread", None) is None
                and not getattr(w, "_restore_queue", None)
                and getattr(w, "_pending_restore", None) is None)

    def _idle(self) -> bool:
        w = self.win
        return (not self._run_busy() and self._window_ready()
                and getattr(w, "_series", None) is None
                and not getattr(w, "_prerun_phase", "")
                and not getattr(w, "_closing", False))

    def _shows(self, case: str) -> bool:
        try:
            return bool(case) and self.win._shows_case(case)
        except Exception:                                  # noqa: BLE001
            return False

    # --- notifications ---------------------------------------------------------

    def tell_end(self, s: "be.RemoteSession") -> None:
        """A run that the window did not follow ended."""
        st = rw.state(s)
        self.seen.tell(s)
        action = ("attach", s.host, s.remote_dir)
        if st == "ok":
            if s.fetched:
                return
            fs_notify.notify("done", tr("Run finished"),
                             tr("{name} on {host}: done. Click to get the "
                                "results.", name=s.name, host=s.host),
                             case=s.case_dir, action=action)
        elif st == "stop":
            fs_notify.notify("stop", tr("Run stopped"),
                             tr("{name} on {host}: stopped.", name=s.name,
                                host=s.host), case=s.case_dir, action=action)
        elif st == "aborted":
            fs_notify.notify("fail", tr("Run broke off"),
                             tr("{name} on {host}: no process on the server "
                                "(a reboot or no memory).", name=s.name,
                                host=s.host), case=s.case_dir, action=action)
        else:
            fs_notify.notify("fail", tr("Run failed"),
                             tr("{name} on {host}: {why}", name=s.name,
                                host=s.host, why=s.message or s.human()),
                             case=s.case_dir, action=action)

    def user_stopped(self) -> None:
        """«Stop» in this window: no notification about this end."""
        self._user_stop = True

    def run_event(self, name: str, value: str) -> None:
        """RunWorker.event of the followed run."""
        w = self.win
        host = getattr(getattr(w.worker, "backend", None), "host", "") \
            if w.worker else ""
        if name == "fetch" and value == "ok":
            fs_notify.notify("done", tr("Run finished"),
                             tr("{name}: done. The program downloads the "
                                "results.", name=self._run_name()),
                             case=getattr(w.worker, "case_dir", ""))
        elif name == "link" and value == "lost":
            text = tr("The connection to {host} is lost. The run continues "
                      "on the server; the program connects again by "
                      "itself.", host=host)
            w.log.push(text)
            w.statusBar().showMessage(text, 30000)
        elif name == "link" and value == "back":
            text = tr("Connected to {host} again.", host=host)
            w.log.push(text)
            w.statusBar().showMessage(text, 8000)

    def _run_name(self) -> str:
        w = self.win.worker
        if w is None:
            return ""
        return (getattr(w, "name", "") or
                Path(str(getattr(w, "case_dir", "") or "")).name)

    def run_done(self, ok: bool, msg: str) -> None:
        """The end of the followed run (MainWindow.on_done, at the end)."""
        w = self.win
        user_stop, self._user_stop = self._user_stop, False
        worker = w.worker
        if worker is None or getattr(w, "_series", None) is not None:
            return
        runner = getattr(worker, "runner", None)
        final = getattr(runner, "final_status", "") if runner else ""
        final = final or ("ok" if ok else "fail")
        b = getattr(worker, "backend", None)
        remote = getattr(b, "kind", "") == "ssh" and bool(
            getattr(worker, "native_dir", "")
            or getattr(worker, "attach_to", ""))
        name = self._run_name()
        case = getattr(worker, "case_dir", "") or ""
        host = getattr(b, "host", "")
        if remote:
            d = (runner.remote_dir if runner is not None else "") \
                or getattr(worker, "native_dir", "")
            # The watch and a later start do not tell this end again.
            self.seen.tell(be.RemoteSession(
                name=name, remote_dir=d, host=host, age=0,
                status=final if final in ("ok", "fail", "stop") else "fail",
                alive=final != "aborted"))
            self.watch.pop(rw.key(host, d), None)
        if user_stop or final == "gone":
            return
        fetched = getattr(worker, "fetched", None)
        if ok and remote and fetched is True:
            fs_notify.notify("fetched", tr("Results downloaded"),
                             tr("{name}: the results from {host} are on "
                                "this computer.", name=name, host=host),
                             case=case)
        elif ok and remote and fetched is False:
            fs_notify.notify("fail", tr("Download failed"),
                             tr("{name}: done on {host}, but the results did "
                                "not come. See the log.", name=name,
                                host=host), case=case)
        elif ok:
            fs_notify.notify("done", tr("Run finished"),
                             tr("{name}: done.", name=name), case=case)
        elif final == "stop":
            fs_notify.notify("stop", tr("Run stopped"),
                             tr("{name}: stopped.", name=name), case=case)
        else:
            last = (msg or "").strip().splitlines()
            fs_notify.notify("fail", tr("Run failed"),
                             tr("{name}: {why}", name=name,
                                why=last[-1][:200] if last else ""),
                             case=case)

    # --- a click on a notification ---------------------------------------------

    def open_note(self, note) -> None:
        w = self.win
        action = note.action or ()
        if action and action[0] == "attach":
            host, rdir = action[1], action[2]
            if self._followed() == rw.key(host, rdir):
                w.tabs_right.setCurrentWidget(w.log)
                return

            def job():
                got = rw.probe(host, [rdir])
                s = got[0] if got else None
                b = self.find_backend(host) if s is not None else None
                try:
                    if s is None:
                        self.found.emit(None, be.RemoteSession(
                            name=Path(rdir).name, remote_dir=rdir,
                            host=host), True)
                    else:
                        self.found.emit(b, s, True)
                except RuntimeError:
                    pass
            w.statusBar().showMessage(
                tr("Asking {host} what is running there…", host=host), 10000)
            threading.Thread(target=job, daemon=True,
                             name="run-open").start()
            return
        case = note.case
        if not case or not os.path.isdir(case):
            return
        if self._shows(case):
            w.tabs_right.setCurrentWidget(w.results_view)
        elif not self._run_busy():
            w._open_attached(case)
