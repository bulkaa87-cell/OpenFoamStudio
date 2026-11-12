"""The «Runs» window (T49b) and the watch of the queue (T46).

`QueueWatch` reads the queue on the server now and then (a thread, never in
the window thread) while the queue is set up. It gives each new read to the
window and tells the person when a job or a series ended. The end of a job is
told by fs_recover.RunGuard.tell_end (T44: the same texts, the click connects
to the run, and its memory of told ends keeps its own watch from telling it
again); the end of a series by fs_notify.notify.

`RunsWindow` (before T49b two windows: «Queue» and «Runs on the Server»)
shows the plan of the queue (the jobs that did not start: add the case or
the angle series, up, down, hold, release, cancel, pause, resume, «start
not before», set up) and the runs on the servers (fs_runs: each run folder
of each known server and the started queue jobs, each run once: connect,
download the results, add again to the queue, cancel a queue job, remove on
the server through the guard of fs_caseguard, remove the ended jobs).
"""

from __future__ import annotations

import os
import threading
import time

from PySide6 import QtCore, QtGui, QtWidgets

import fs_backend as be
import fs_caseguard as guard
import fs_queue as fq
import fs_runs
import fs_settings as fset
from fs_i18n import tr

WATCH_MS = 60_000          # the main window reads the queue this often
WINDOW_MS = 15_000         # and this often while the window is open
SCAN_MS = 60_000           # the run folders of the servers, window open


def notify(kind: str, title: str, text: str, case: str = "",
           action=None) -> None:
    """A notification of the program: fs_notify (T44), nothing else."""
    import fs_notify
    fs_notify.notify(kind, title, text, case=case, action=action)


def job_session(job: dict, queue: str, now: "float | None" = None):
    """The run of an ended job as fs_runwatch sees it (for fs_recover: the
    same texts, the same «one note per end» memory, the click connects)."""
    now = time.time() if now is None else now
    st, msg = job.get("state"), job.get("msg", "")
    status = {"done": "ok", "failed": "fail", "cancelled": "stop"}.get(st, st)
    if st == "failed" and msg == "aborted":
        status = "running"                      # rw.state: «aborted»
    ended = float(job.get("ended") or 0)
    return be.RemoteSession(
        name=job.get("name", ""), remote_dir=job.get("dir", ""),
        status=status, step=job.get("step", ""),
        message=fq.job_text(job) if st == "failed" else "",
        case_dir=job.get("case_dir", ""),
        host=job.get("via") or job.get("host") or queue,
        alive=False, age=int(now - ended) if ended else -1)


def default_start_at(now: "QtCore.QDateTime | None" = None,
                     lead_s: int = 15 * 60) -> QtCore.QDateTime:
    """The default time of «Start not before»: the next full hour that is
    at least `lead_s` after `now` (14:20 -> 15:00, 14:50 -> 16:00)."""
    now = now or QtCore.QDateTime.currentDateTime()
    t = now.addSecs(lead_s)
    tm = t.time()
    hour = QtCore.QDateTime(t.date(), QtCore.QTime(tm.hour(), 0))
    return hour if hour == t else hour.addSecs(3600)


class _Bridge(QtCore.QObject):
    done = QtCore.Signal(object, object, object)     # callback, result, error


class Worker:
    """Runs `fn` in a thread; `then(result, error)` runs in the window thread."""

    def __init__(self):
        self._bridge = _Bridge()
        self._bridge.done.connect(self._finish)
        self.busy = 0

    def start(self, fn, then) -> None:
        self.busy += 1

        def run():
            try:
                got, err = fn(), None
            except Exception as exc:                     # noqa: BLE001
                got, err = None, exc
            try:
                self._bridge.done.emit(then, got, err)
            except RuntimeError:                         # window is gone
                pass
        threading.Thread(target=run, name="queue", daemon=True).start()

    def _finish(self, then, got, err) -> None:
        self.busy -= 1
        then(got, err)


class QueueWatch(QtCore.QObject):
    """Reads the queue and tells the person about ended jobs."""

    updated = QtCore.Signal(object)          # the answer of «list»
    failed = QtCore.Signal(str)

    def __init__(self, parent, client_factory=None):
        super().__init__(parent)
        self.win = parent
        self._client_factory = client_factory or (
            lambda: fq.Client(fq.queue_host()))
        self.worker = Worker()
        self.jobs: "list | None" = None      # None: not read yet
        self.last: dict = {}
        self.timer = QtCore.QTimer(self)
        self.timer.timeout.connect(self.poll)
        self.timer.start(WATCH_MS)

    def client(self) -> fq.Client:
        return self._client_factory()

    def poll(self) -> None:
        if not fset.get("queue_on") or self.worker.busy:
            return
        try:
            c = self.client()
        except fq.QueueError:
            return
        self.worker.start(c.list, self._got)

    def _got(self, res, err) -> None:
        if err is not None:
            self.failed.emit(str(err))
            return
        jobs = res.get("jobs", [])
        if self.jobs is not None:
            for ev in fq.changes(self.jobs, jobs):
                self._tell(ev)
        self.jobs = jobs
        self.last = res
        self.updated.emit(res)

    def _tell(self, ev) -> None:
        if ev[0] == "job":
            # The end of one run: the note of fs_recover (T44), which also
            # remembers it, so that its own watch does not tell it again.
            s = job_session(ev[1], fq.queue_host())
            guard = getattr(self.win, "run_guard", None)
            if guard is not None:
                if not guard.seen.told(s):
                    guard.tell_end(s)
            elif ev[1].get("state") == "done":
                notify("done", tr("Queue: {name} done",
                                  name=ev[1].get("name", "")),
                       fq.job_text(ev[1]), case=s.case_dir)
            else:
                notify("fail", tr("Queue: {name} — {state}",
                                  name=ev[1].get("name", ""),
                                  state=fq.state_text(ev[1].get("state", ""))),
                       fq.job_text(ev[1]), case=s.case_dir)
        elif ev[0] == "series":
            jobs = ev[2]
            n_ok = sum(1 for j in jobs if j.get("state") == "done")
            notify("done" if n_ok == len(jobs) else "fail",
                   tr("Queue: series {s} ended", s=ev[1]),
                   tr("{ok} of {n} jobs done. «Runs» → «Download "
                      "Results» gets them.", ok=n_ok, n=len(jobs)),
                   case=next((j.get("case_dir", "") for j in jobs), ""))
            hook = getattr(self.win, "queue_series_ended", None)
            if hook is not None:
                hook(ev[1], jobs)


STATE_COLORS = {"running": "#1f6feb", "ok": "#1a7f37", "done": "#1a7f37",
                "fail": "#cf222e", "failed": "#cf222e", "aborted": "#cf222e",
                "stop": "#6e7781", "cancelled": "#6e7781", "held": "#9a6700"}


def _item(text, job_id: str = "", raw: bool = False,
          tip: str = "") -> QtWidgets.QTableWidgetItem:
    """A table cell. `raw`: a value (a case name, a machine), not a text of
    the program: it goes in through setData, so that fs_i18n does not take
    a case called «sphere» for a word to translate."""
    if raw:
        item = QtWidgets.QTableWidgetItem()
        item.setData(QtCore.Qt.DisplayRole, str(text))
    else:
        item = QtWidgets.QTableWidgetItem(text)
    item.setData(QtCore.Qt.UserRole, job_id)
    if tip:
        item.setToolTip(tip)
    return item


def _table(cols, stretch: int) -> QtWidgets.QTableWidget:
    t = QtWidgets.QTableWidget(0, len(cols))
    t.setHorizontalHeaderLabels(list(cols))
    t.setSelectionBehavior(QtWidgets.QAbstractItemView.SelectRows)
    t.setEditTriggers(QtWidgets.QAbstractItemView.NoEditTriggers)
    t.verticalHeader().setVisible(False)
    # T49: the short columns take the width of their texts, the message
    # takes the rest. resizeColumnsToContents() with only
    # setStretchLastSection() packed all the columns to the left.
    hdr = t.horizontalHeader()
    for c in range(len(cols)):
        hdr.setSectionResizeMode(
            c, QtWidgets.QHeaderView.Stretch if c == stretch
            else QtWidgets.QHeaderView.ResizeToContents)
    return t


class RunsWindow(QtWidgets.QDialog):
    """«Runs» (T49b): the plan of the queue and the runs on the servers.

    Top: the queue jobs that did not start yet, with the queue actions.
    Bottom: each run folder on each known server (running, done, failed)
    and the queue jobs that started, each run once (fs_runs.merge), with
    «Connect», «Download Results», «Add Again to the Queue», «Cancel Job»
    and «Remove on Server».

    The main window gives the actions that need it (callbacks): add_case,
    add_series, fetch (rows as jobs), connect (a RemoteSession), hosts (the
    known servers) and snapshot (fs_runs.Snapshot, shared with the run
    watch of T44).
    """

    COLS = ("ID", "Case", "Machine", "State", "Message")
    COL_MSG = 4
    SCOLS = ("Case", "Machine", "State", "Step", "Started", "Ended", "Lines",
             "Results", "Message")
    SCOL_MSG = 8

    def __init__(self, parent, watch: QueueWatch, add_case=None,
                 add_series=None, fetch=None, connect=None, hosts=None,
                 snapshot: "fs_runs.Snapshot | None" = None, discover=None,
                 remove=None, prepare=None):
        super().__init__(parent)
        self.watch = watch
        self.add_case, self.add_series, self.fetch = add_case, add_series, fetch
        self.connect_run = connect
        self.hosts_fn = hosts or (lambda: [])
        self.snapshot = snapshot if snapshot is not None else fs_runs.Snapshot()
        self.discover = discover            # tests: a fake discover(host)
        self.remove_fn = remove or fs_runs.remove_run
        self.prepare_fn = prepare or fq.prepare_case
        self.worker = Worker()              # the queue actions
        self.scanner = Worker()             # the read of the servers
        self.res: dict = {}
        self.rows: list = []                # fs_runs.Row of the bottom part
        self.setWindowTitle("Runs")
        self.resize(1100, 680)
        lay = QtWidgets.QVBoxLayout(self)

        top = QtWidgets.QHBoxLayout()
        top.addWidget(QtWidgets.QLabel("Queue server"))
        self.ed_host = QtWidgets.QLineEdit(fq.queue_host())
        self.ed_host.setPlaceholderText("server1")
        self.ed_host.setToolTip(
            "The server that keeps the queue and starts the jobs: a name "
            "from ~/.ssh/config. The jobs can run on it, on the machines it "
            "reaches (node1, node2…) or distributed over them.")
        self.ed_host.editingFinished.connect(self._host_changed)
        top.addWidget(self.ed_host, 1)
        self.btn_setup = QtWidgets.QPushButton("Set Up")
        self.btn_setup.setToolTip(
            "Put the queue on the server and start it. With lingering on "
            "for your user there, it is a systemd user service and starts "
            "again after a reboot of the server. The program does not "
            "change lingering.")
        self.btn_setup.clicked.connect(self.setup)
        top.addWidget(self.btn_setup)
        self.btn_refresh = QtWidgets.QPushButton("Refresh")
        self.btn_refresh.setToolTip(
            "Read the queue and the run folders of each server again (one "
            "ssh call per server).")
        self.btn_refresh.clicked.connect(self.refresh_all)
        top.addWidget(self.btn_refresh)
        lay.addLayout(top)
        self.lbl_runner = QtWidgets.QLabel("")
        self.lbl_runner.setWordWrap(True)
        lay.addWidget(self.lbl_runner)

        split = QtWidgets.QSplitter(QtCore.Qt.Vertical)
        self.split = split
        lay.addWidget(split, 1)

        # --- the plan ----------------------------------------------------
        self.box_plan = QtWidgets.QGroupBox("Planned: the queue")
        pv = QtWidgets.QVBoxLayout(self.box_plan)
        self.table = _table(self.COLS, self.COL_MSG)
        self.table.itemSelectionChanged.connect(self._update_buttons)
        pv.addWidget(self.table, 1)

        add = QtWidgets.QHBoxLayout()
        self.btn_add = QtWidgets.QPushButton("Add the Case")
        self.btn_add.setToolTip(
            "The case of the window as one job, on the server of «Run on "
            "Server». With the series check box: one job per angle.")
        self.btn_add.clicked.connect(lambda: self._add(self.add_case))
        self.btn_series = QtWidgets.QPushButton("Add the Angle Series")
        self.btn_series.setToolTip(
            "Each angle of the «Angles» field is one job. The server runs "
            "them one after another; the laptop can sleep.")
        self.btn_series.clicked.connect(lambda: self._add(self.add_series))
        self.chk_at = QtWidgets.QCheckBox("Start not before")
        self.chk_at.setToolTip(
            "The new jobs wait until this time, for example the night.")
        self.chk_at.setChecked(False)
        # T49: the default time is made when the window opens and when the
        # box is ticked, not once at the first open (the window stays: a
        # time of yesterday was shown).
        self.ed_at = QtWidgets.QDateTimeEdit(default_start_at())
        self.ed_at.setDisplayFormat("yyyy-MM-dd HH:mm")
        self.ed_at.setCalendarPopup(True)
        self.ed_at.setEnabled(False)
        self.chk_at.toggled.connect(self._at_toggled)
        for w in (self.btn_add, self.btn_series, self.chk_at, self.ed_at):
            add.addWidget(w)
        add.addStretch(1)
        pv.addLayout(add)

        row = QtWidgets.QHBoxLayout()
        self.btn_up = QtWidgets.QPushButton("Up")
        self.btn_down = QtWidgets.QPushButton("Down")
        self.btn_hold = QtWidgets.QPushButton("Hold")
        self.btn_hold.setToolTip("The selected jobs wait until «Release».")
        self.btn_release = QtWidgets.QPushButton("Release")
        self.btn_cancel = QtWidgets.QPushButton("Cancel Job")
        self.btn_cancel.setToolTip("The selected waiting jobs do not start.")
        self.btn_pause = QtWidgets.QPushButton("Pause Queue")
        self.btn_pause.setToolTip(
            "The running jobs go on; no new job starts until «Resume».")
        self.btn_up.clicked.connect(lambda: self._move("up"))
        self.btn_down.clicked.connect(lambda: self._move("down"))
        self.btn_hold.clicked.connect(lambda: self._act("hold"))
        self.btn_release.clicked.connect(lambda: self._act("release"))
        self.btn_cancel.clicked.connect(lambda: self._cancel(self._plan_ids()))
        self.btn_pause.clicked.connect(self._pause)
        for w in (self.btn_up, self.btn_down, self.btn_hold, self.btn_release,
                  self.btn_cancel):
            row.addWidget(w)
        row.addStretch(1)
        row.addWidget(self.btn_pause)
        pv.addLayout(row)
        split.addWidget(self.box_plan)

        # --- the runs on the servers ---------------------------------------
        self.box_runs = QtWidgets.QGroupBox("On the servers")
        sv = QtWidgets.QVBoxLayout(self.box_runs)
        self.lbl_hosts = QtWidgets.QLabel("")
        self.lbl_hosts.setWordWrap(True)
        self.lbl_hosts.setStyleSheet("color:#6e7781;")
        sv.addWidget(self.lbl_hosts)
        self.runs = _table(self.SCOLS, self.SCOL_MSG)
        # «Step» and «Lines» have a second meaning in the catalogue (the
        # step of a move, the lines of a slice): the run context.
        self.runs.setHorizontalHeaderItem(
            3, QtWidgets.QTableWidgetItem(tr("Step", ctx="run")))
        self.runs.setHorizontalHeaderItem(
            6, QtWidgets.QTableWidgetItem(tr("Lines", ctx="log")))
        self.runs.setSelectionMode(
            QtWidgets.QAbstractItemView.ExtendedSelection)
        self.runs.itemSelectionChanged.connect(self._update_buttons)
        self.runs.doubleClicked.connect(lambda _i: self._connect())
        sv.addWidget(self.runs, 1)
        srow = QtWidgets.QHBoxLayout()
        self.btn_connect = QtWidgets.QPushButton("Connect")
        self.btn_connect.setToolTip(
            "Connect the window to the selected run: a run in progress "
            "shows live; an ended run opens with its log and results.")
        self.btn_connect.clicked.connect(self._connect)
        self.btn_fetch = QtWidgets.QPushButton("Download Results")
        self.btn_fetch.setToolTip(
            "Download the results of the selected ended runs into their "
            "case folders (the last time; the «Results» tab then offers the "
            "other times). For an angle series of the queue, the comparison "
            "with the neutral point opens.")
        self.btn_fetch.clicked.connect(self._fetch)
        self.btn_again = QtWidgets.QPushButton("Add Again to the Queue")
        self.btn_again.setToolTip(
            "The case of the selected run as a new queue job on the same "
            "machine. The new run goes into a new folder (<name>_r2); the "
            "old results stay.")
        self.btn_again.clicked.connect(self._again)
        self.btn_stop = QtWidgets.QPushButton("Cancel Job")
        self.btn_stop.setToolTip(
            "A running queue job is stopped (like «Stop»: without a time "
            "step).")
        self.btn_stop.clicked.connect(lambda: self._cancel(self._run_ids()))
        self.btn_remove = QtWidgets.QPushButton("Remove on Server")
        self.btn_remove.setToolTip(
            "Delete the folder of the selected ended run on its server. "
            "Never a running run: the guard of the program checks it on "
            "the server first.")
        self.btn_remove.clicked.connect(self._remove)
        self.btn_clear = QtWidgets.QPushButton("Remove Ended")
        self.btn_clear.setToolTip(
            "Remove the ended jobs from the list of the queue. The case "
            "folders and the results on the server stay.")
        self.btn_clear.clicked.connect(self._clear)
        for w in (self.btn_connect, self.btn_fetch, self.btn_again,
                  self.btn_stop, self.btn_remove):
            srow.addWidget(w)
        srow.addStretch(1)
        srow.addWidget(self.btn_clear)
        sv.addLayout(srow)
        split.addWidget(self.box_runs)
        split.setSizes([260, 360])

        self.lbl_status = QtWidgets.QLabel("")
        self.lbl_status.setWordWrap(True)
        lay.addWidget(self.lbl_status)

        self.timer = QtCore.QTimer(self)
        self.timer.timeout.connect(self.refresh)
        self.scan_timer = QtCore.QTimer(self)
        self.scan_timer.timeout.connect(self.scan)
        watch.updated.connect(self.show_result)
        watch.failed.connect(self._failed)
        if watch.last:
            self.show_result(watch.last)
        else:
            self._fill_runs()
        self._update_buttons()

    # --- reading --------------------------------------------------------------

    def showEvent(self, ev) -> None:                     # noqa: N802
        super().showEvent(ev)
        self._fresh_at()
        self.timer.start(WINDOW_MS)
        self.scan_timer.start(SCAN_MS)
        self.refresh_all()

    def hideEvent(self, ev) -> None:                     # noqa: N802
        self.timer.stop()
        self.scan_timer.stop()
        super().hideEvent(ev)

    def focus_server(self) -> None:
        """Open on the runs on the servers («Connect to Run»)."""
        self.runs.setFocus()
        if self.runs.rowCount() and not self.runs.selectedItems():
            self.runs.selectRow(0)

    def _fresh_at(self) -> None:
        """At each open: a time that is not old. An unticked box gets the
        default time again; a ticked box with a time that is already past
        becomes unticked (that time does not mean anything now)."""
        now = QtCore.QDateTime.currentDateTime()
        if self.chk_at.isChecked() and self.ed_at.dateTime() > now:
            return
        self.chk_at.setChecked(False)
        self.ed_at.setDateTime(default_start_at(now))

    def _at_toggled(self, on: bool) -> None:
        if on and (self.ed_at.dateTime()
                   <= QtCore.QDateTime.currentDateTime().addSecs(60)):
            self.ed_at.setDateTime(default_start_at())
        self.ed_at.setEnabled(on)

    def refresh_all(self) -> None:
        self.refresh()
        self.scan()

    def refresh(self) -> None:
        """Read the queue (fs_queuewin.QueueWatch, a thread)."""
        if not self.ed_host.text().strip():
            self.lbl_runner.setText(tr("Enter the queue server, then click "
                                       "«Set Up»."))
            return
        if not fset.get("queue_on"):
            self.lbl_runner.setText(tr("The queue is not set up on {host} "
                                       "yet: click «Set Up».",
                                       host=self.ed_host.text().strip()))
            return
        self.watch.poll()

    def hosts(self) -> list:
        queue = fq.queue_host() if fset.get("queue_on") else ""
        try:
            more = list(self.hosts_fn() or [])
        except Exception:                                  # noqa: BLE001
            more = []
        return fs_runs.hosts_of(self.res.get("jobs", []), queue, more)

    def scan(self) -> None:
        """Read the run folders of each server: one ssh call per server, in
        a thread; the window does not wait."""
        if self.scanner.busy:
            return
        hosts = self.hosts()
        if not hosts:
            self.lbl_hosts.setText(tr("No server is known yet: enter one in "
                                      "«Server» on the «Run» tab."))
            return
        self.lbl_hosts.setText(tr("Reading {hosts}…", hosts=", ".join(hosts)))
        disc = self.discover
        self.scanner.start(lambda: fs_runs.scan(hosts, discover=disc),
                           self._scanned)

    def _scanned(self, found, err) -> None:
        if err is not None or found is None:
            self.lbl_hosts.setText(tr("The servers did not reply: {why}",
                                      why=err))
            return
        self.snapshot.update(found)
        self._fill_runs()

    def _failed(self, text: str) -> None:
        self.lbl_status.setText(tr("The queue did not reply: {why}", why=text))

    def show_result(self, res: dict) -> None:
        self.res = res or {}
        d = self.res.get("daemon", {})
        if d.get("alive"):
            how = (tr("a systemd service: it also starts after a reboot of "
                      "the server") if d.get("systemd") else
                   tr("nohup: after a reboot of the server it starts at the "
                      "next use"))
            text = tr("The queue on {host} works ({how}).",
                      host=fq.queue_host(), how=how)
        else:
            text = tr("The queue on {host} is off: click «Set Up».",
                      host=fq.queue_host())
        if self.res.get("paused"):
            text += " " + tr("Paused: no new job starts.")
        self.lbl_runner.setText(text)
        self.btn_pause.setText(tr("Resume Queue") if self.res.get("paused")
                               else tr("Pause Queue"))
        self._fill(self.res.get("jobs", []))
        self._fill_runs()
        self.lbl_status.setText(tr("Read at {t}.", t=time.strftime("%H:%M:%S")))

    def _fill(self, jobs: list) -> None:
        keep = set(self._plan_ids())
        jobs = fs_runs.planned(jobs)
        self.table.setRowCount(len(jobs))
        queue = fq.queue_host()
        for r, j in enumerate(jobs):
            jid = str(j.get("id", ""))
            msg = fq.job_text(j)
            cells = (_item(jid, jid, raw=True),
                     _item(j.get("name", ""), jid, raw=True),
                     _item(fq.machine_text(j, queue), jid, raw=True),
                     _item(fq.state_text(j.get("state", "")), jid),
                     _item(msg, jid, tip=msg))
            for c, item in enumerate(cells):
                if c == 3 and j.get("state") in STATE_COLORS:
                    item.setForeground(QtGui.QColor(STATE_COLORS[j["state"]]))
                self.table.setItem(r, c, item)
            if jid in keep:
                self.table.selectRow(r)
        self._update_buttons()

    def _fill_runs(self) -> None:
        keep = {r.key for r in self.selected_rows()}
        queue = fq.queue_host() if fset.get("queue_on") else ""
        found = dict(self.snapshot.found)
        self.rows = fs_runs.merge(self.res.get("jobs", []), found, queue)
        now = time.time()
        self.runs.setRowCount(len(self.rows))
        for r, row in enumerate(self.rows):
            st = row.state
            msg = fs_runs.message_text(row)
            res = fs_runs.results_text(row)
            cells = (_item(row.name, row.job_id, raw=True,
                           tip=row.remote_dir),
                     _item(row.host, row.job_id, raw=True),
                     _item(fs_runs.state_text(st), row.job_id),
                     _item(row.step_text() or "", row.job_id, raw=True),
                     _item(row.started_text(), row.job_id, raw=True),
                     _item(fq.fmt_time(row.ended_at(now)), row.job_id,
                           raw=True),
                     _item(row.lines(), row.job_id, raw=True),
                     _item(res, row.job_id),
                     _item(msg, row.job_id, tip=msg))
            for c, item in enumerate(cells):
                if c == 2 and st in STATE_COLORS:
                    item.setForeground(QtGui.QColor(STATE_COLORS[st]))
                if c == 7 and row.fetched is False:
                    item.setForeground(QtGui.QColor("#9a6700"))
                self.runs.setItem(r, c, item)
            if row.key in keep:
                self.runs.selectRow(r)
        quiet = [h for h, s in found.items() if s is None]
        hosts = [h for h in found if found[h] is not None]
        text = tr("Servers: {hosts}.", hosts=", ".join(hosts) or "—")
        if quiet:
            text += " " + tr("No reply from {hosts}.", hosts=", ".join(quiet))
        if self.snapshot.at:
            text += " " + tr("Read at {t}.", t=time.strftime(
                "%H:%M:%S", time.localtime(self.snapshot.at)))
        if found:                    # else keep the note of scan()
            self.lbl_hosts.setText(text)
        self._update_buttons()

    def retranslate_ui(self) -> None:
        # fs_i18n does not redo the own texts of a widget with this hook.
        self.setWindowTitle("Runs")
        if self.res:
            self.show_result(self.res)
        else:
            self._fill_runs()

    # --- selection ------------------------------------------------------------

    def _plan_ids(self) -> list:
        rows = sorted({i.row() for i in self.table.selectedItems()})
        out = []
        for r in rows:
            it = self.table.item(r, 0)
            if it is not None:
                out.append(it.data(QtCore.Qt.UserRole))
        return out

    def selected_rows(self) -> list:
        rows = sorted({i.row() for i in self.runs.selectedItems()})
        return [self.rows[r] for r in rows if 0 <= r < len(self.rows)]

    def _run_ids(self) -> list:
        return [r.job_id for r in self.selected_rows() if r.job_id]

    def _jobs(self, ids=None) -> list:
        jobs = self.res.get("jobs", [])
        return [j for j in jobs if ids is None or j.get("id") in ids]

    def _update_buttons(self) -> None:
        sel = self._jobs(self._plan_ids())
        states = {j.get("state") for j in sel}
        busy = bool(self.worker.busy)
        on = bool(fset.get("queue_on"))
        self.btn_up.setEnabled(len(sel) == 1 and not busy)
        self.btn_down.setEnabled(len(sel) == 1 and not busy)
        self.btn_hold.setEnabled("queued" in states and not busy)
        self.btn_release.setEnabled("held" in states and not busy)
        self.btn_cancel.setEnabled(bool(states & set(fq.ACTIVE)) and not busy)
        for b in (self.btn_add, self.btn_series, self.btn_clear,
                  self.btn_pause):
            b.setEnabled(on and not busy)
        rows = self.selected_rows()
        one = rows[0] if len(rows) == 1 else None
        self.btn_connect.setEnabled(
            one is not None and self.connect_run is not None
            and (one.session is not None or one.running))
        self.btn_fetch.setEnabled(
            not busy and self.fetch is not None
            and any(r.ended and r.state != "aborted" for r in rows))
        self.btn_again.setEnabled(
            not busy and on and one is not None and not one.running
            and bool(one.case_dir))
        run_jobs = self._jobs(self._run_ids())
        self.btn_stop.setEnabled(not busy and any(
            j.get("state") in fq.ACTIVE for j in run_jobs))
        self.btn_remove.setEnabled(
            not busy and one is not None and one.session is not None
            and one.ended)

    # --- actions of the queue ------------------------------------------------

    def _host_changed(self) -> None:
        host = self.ed_host.text().strip()
        if host != fq.queue_host():
            fset.update({"queue_host": host, "queue_on": False})
            self.res = {}
            self._fill([])
            self._fill_runs()
            self.refresh()

    def client(self) -> fq.Client:
        return self.watch.client()

    def _run(self, text: str, fn, then=None, client: bool = True) -> None:
        self.lbl_status.setText(text)

        def done(got, err):
            self._update_buttons()
            if err is not None:
                self._error(err, queue=client)
                return
            if then is not None:
                then(got)
            self.watch.poll()
        c = None
        if client:
            try:
                c = self.client()
            except fq.QueueError as exc:
                self.lbl_status.setText(str(exc))
                return
        self.worker.start(lambda: fn(c), done)
        self._update_buttons()

    def _error(self, err, queue: bool = True) -> None:
        if queue and not isinstance(err, guard.CaseBusy):
            self.lbl_status.setText(tr("The queue: {why}", why=err))
        else:
            self.lbl_status.setText(str(err))

    def setup(self) -> None:
        self._host_changed()
        host = self.ed_host.text().strip()
        if not host:
            self.lbl_runner.setText(tr("Enter the queue server first."))
            return

        def then(info):
            fset.update({"queue_host": host, "queue_on": True})
            import fs_cli
            self.lbl_runner.setText(fs_cli.setup_text(info))
        self._run(tr("Setting up the queue on {host}…", host=host),
                  lambda c: c.setup(systemd=True), then)

    def not_before(self) -> float:
        if not self.chk_at.isChecked():
            return 0.0
        return float(self.ed_at.dateTime().toSecsSinceEpoch())

    def _add(self, fn) -> None:
        if fn is None:
            return
        fn(self.not_before())
        self.watch.poll()

    def _move(self, where: str) -> None:
        ids = self._plan_ids()
        if len(ids) == 1:
            self._run(tr("Moving {id}…", id=ids[0]),
                      lambda c: c.move(ids[0], where))

    def _act(self, what: str) -> None:
        ids = self._plan_ids()
        if ids:
            self._run(tr("Sending to the queue…"),
                      lambda c: getattr(c, what)(ids))

    def _cancel(self, ids=None) -> None:
        ids = [j["id"] for j in self._jobs(ids or [])
               if j.get("state") in fq.ACTIVE]
        if not ids:
            return
        running = [j for j in self._jobs(ids) if j.get("state") == "running"]
        if running:
            box = QtWidgets.QMessageBox.question(
                self, tr("Cancel Job"),
                tr("{n} of the selected jobs are running. Stop them now? "
                   "What was computed after the last written time is lost.",
                   n=len(running)))
            if box != QtWidgets.QMessageBox.Yes:
                return
        self._run(tr("Cancelling {ids}…", ids=", ".join(ids)),
                  lambda c: c.cancel(ids))

    def _clear(self) -> None:
        self._run(tr("Removing the ended jobs…"),
                  lambda c: c.remove(finished=True))

    def _pause(self) -> None:
        paused = bool(self.res.get("paused"))
        self._run(tr("Sending to the queue…"),
                  (lambda c: c.resume()) if paused else (lambda c: c.pause()))

    # --- actions on the runs on the servers ----------------------------------

    def _connect(self) -> None:
        rows = self.selected_rows()
        if len(rows) != 1 or self.connect_run is None:
            return
        row = rows[0]
        s = row.session or be.RemoteSession(
            name=row.name, remote_dir=row.remote_dir, host=row.host,
            case_dir=row.case_dir, status="running")
        self.lbl_status.setText(tr("Connecting to {name} on {host}…",
                                   name=row.name, host=row.host))
        self.connect_run(s)

    def _fetch(self) -> None:
        if self.fetch is None:
            return
        queue = fq.queue_host()
        jobs = [r.as_job(queue) for r in self.selected_rows()
                if r.ended and r.state != "aborted"]
        if jobs:
            self.fetch(jobs)
            self.scan()

    def _again(self) -> None:
        rows = self.selected_rows()
        if len(rows) != 1:
            return
        row = rows[0]
        if not os.path.isdir(row.case_dir):
            self.lbl_status.setText(tr(
                "The case folder {d} is not on this computer: the run "
                "cannot go to the queue again.", d=row.case_dir or "?"))
            return
        queue = fq.queue_host()
        on = "dist" if (row.job or {}).get("nodes") else row.host
        at = self.not_before()
        prep = self.prepare_fn

        def job(c):
            j = prep(row.case_dir, on=on, queue=queue, not_before=at,
                     origin="window")
            c.ready()
            return c.add([j]), j

        def then(got):
            ids, j = got
            self.lbl_status.setText(tr("{name} is in the queue again as "
                                       "{ids} ({dir}).", name=j.get("name", ""),
                                       ids=", ".join(ids), dir=j.get("dir", "")))
        self._run(tr("Uploading {name} to {host} for the queue…",
                     name=row.name, host=row.host), job, then)

    def _remove(self) -> None:
        rows = self.selected_rows()
        if len(rows) != 1:
            return
        row = rows[0]
        if row.running or row.session is None:
            return
        text = tr("Delete the folder {dir} on {host}? The case folder on "
                  "this computer stays.", dir=row.remote_dir, host=row.host)
        box = QtWidgets.QMessageBox.question(self, tr("Remove on Server"),
                                             text)
        if box != QtWidgets.QMessageBox.Yes:
            return
        own = False
        if row.fetched is False:
            # A second, separate warning (the user, T49c): the results are
            # only on the server. «Cancel» is the default button. Only after
            # «Yes» the guard may skip its «results not downloaded» check
            # (own=1); a live run it refuses always.
            Box = QtWidgets.QMessageBox
            box = Box.warning(
                self, tr("Remove on Server"),
                tr("The results of this run are not downloaded to the "
                   "laptop yet. Delete them on the server for ever?"),
                Box.Yes | Box.Cancel, Box.Cancel)
            if box != Box.Yes:
                self.lbl_status.setText(tr("Nothing was deleted."))
                return
            own = True
        self._remove_run(row, own=own)

    def _remove_run(self, row, own: bool) -> None:
        remove = self.remove_fn
        jid = row.job_id if (row.job or {}).get("state") in fq.FINAL else ""
        queue_on = bool(fset.get("queue_on"))

        def job(_c):
            remove(row.host, row.remote_dir, own=own)
            if jid and queue_on:
                try:
                    self.client().remove([jid])
                except Exception:                          # noqa: BLE001
                    pass
            return row

        def then(_got):
            self.lbl_status.setText(tr("{dir} on {host} is deleted.",
                                       dir=row.remote_dir, host=row.host))
            found = dict(self.snapshot.found)
            got = found.get(row.host)
            if got:
                self.snapshot.update({row.host: [
                    s for s in got if s.remote_dir != row.remote_dir]})
            self._fill_runs()
            self.scan()
        self._run(tr("Deleting {dir} on {host}…", dir=row.remote_dir,
                     host=row.host), job, then, client=False)


# The name before T49b.
QueueWindow = RunsWindow


def fetch_jobs(jobs: list, queue: str, say=None) -> "tuple[list, list]":
    """Download the results of ended jobs into their case folders (no Qt;
    the main window runs it in a thread). Same download as after «Run on
    Server» (fs_backend.sync_from_ssh). → (fetched case folders, [(job,
    why)])."""
    import os
    import fs_library as lib
    got, bad = [], []
    for j in jobs:
        d = j.get("case_dir", "")
        if not d or not os.path.isdir(d):
            bad.append((j, tr("the case folder {d} is not on this computer",
                              d=d or "?")))
            continue
        host = j.get("via") or j.get("host") or queue
        if say:
            say(tr("Downloading {name} from {host}…", name=j.get("name", ""),
                   host=host))
        try:
            be.sync_from_ssh(be.Backend(kind="ssh", label=host, host=host),
                             j["dir"], d)
            lib.record_run(d, ok=True, note="queue " + str(j.get("id", "")),
                           server={"host": host, "dir": j["dir"]})
            got.append(d)
        except Exception as exc:                          # noqa: BLE001
            bad.append((j, str(exc)))
    return got, bad
