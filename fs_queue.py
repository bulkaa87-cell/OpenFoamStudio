"""The run queue on a server, the side of the program (T46). No Qt.

The queue itself is `fs_queue_runner.py`: it lives on the server in
~/openfoam-studio/.queue and starts the jobs there and on the lab nodes, also
when the laptop sleeps or is off. This module:

* puts the runner on the server and starts its daemon (`Client.setup`);
* talks to it over ssh (`Client.add`, `list`, `cancel`, ...): each call is
  one `python3 fs_queue_runner.py <command>` with a JSON answer;
* makes a job of a case folder: the run script comes from the same
  generator as «Run on Server» (`fs_backend.detached_script`, the steps of
  `run_steps`), and it is put in studio/q_<token>.sh (`script_job`);
* says in words what the states and the reasons of the queue mean
  (`state_text`, `job_text`), and finds what changed between two reads
  (`changes`) for the notifications of the program.

The window («Queue», fs_queuewin) and the command line `ofs` (fs_cli) use it.
"""

from __future__ import annotations

import json
import re
import secrets
import shlex
import subprocess
import time
from pathlib import Path

import fs_backend as be
import fs_caseguard as guard
from fs_i18n import tr

RUNNER_LOCAL = Path(__file__).resolve().with_name("fs_queue_runner.py")
QUEUE_DIR = be.REMOTE_ROOT + "/.queue"
RUNNER_NAME = "fs_queue_runner.py"

STATES = ("queued", "held", "running", "done", "failed", "cancelled")
ACTIVE = ("queued", "held", "running")
FINAL = ("done", "failed", "cancelled")

# Groups of run steps for «--steps» (the order of a run).
STEP_GROUPS = ("mesh", "solve", "reconstruct")
_MESH_STEPS = {be.STEP_CLEAN, be.STEP_MESH_CHECK, be.STEP_MESH_LOAD,
               be.STEP_MESH_STORE, "surfaceFeatureExtract", "blockMesh",
               "decomposePar", "snappyHexMesh", "checkMesh"}
_RECON_STEPS = {"reconstructParMesh", "reconstructPar"}


class QueueError(RuntimeError):
    """The queue did not do what was asked. The text is for the person."""


def runner_version() -> int:
    """VERSION of the runner file next to this module."""
    m = re.search(r"^VERSION = (\d+)", RUNNER_LOCAL.read_text(encoding="utf-8"),
                  re.M)
    return int(m.group(1)) if m else 0


# --------------------------------------------------------------------------
# The client
# --------------------------------------------------------------------------

def _local_run(argv, stdin: "bytes | None", timeout: float):
    p = subprocess.run(argv, input=stdin, stdout=subprocess.PIPE,
                       stderr=subprocess.PIPE, timeout=timeout,
                       creationflags=be.CREATE_NO_WINDOW)
    return p.returncode, p.stdout.decode("utf-8", "replace"), \
        p.stderr.decode("utf-8", "replace")


class Client:
    """The queue on one server.

    `host`: the ssh name of the server, or "local" when this program runs on
    the server itself (n8n, a script there). `qdir`: the queue folder on the
    server (bash text; the tests use a dbg folder). `run(argv, stdin,
    timeout) -> (rc, out, err)` replaces subprocess in the tests.
    """

    def __init__(self, host: str, qdir: str = QUEUE_DIR, run=None):
        self.host = (host or "").strip()
        if not self.host:
            raise QueueError(tr("No queue server is set. Enter the server "
                                "in the «Runs» window."))
        self.qdir = qdir
        self._run = run or _local_run

    @property
    def local(self) -> bool:
        return self.host == "local"

    @property
    def runner(self) -> str:
        return f"{self.qdir}/{RUNNER_NAME}"

    def _bash(self, script: str) -> list:
        if self.local:
            return ["bash", "-c", script]
        return be.ssh_argv(self.host, script)

    def _env(self) -> str:
        return ("" if self.qdir == QUEUE_DIR
                else f'OFS_QUEUE_DIR="{self.qdir}" ')

    def call(self, *args: str, stdin: "str | None" = None,
             timeout: float = 90) -> dict:
        """Run one runner command. Returns its JSON; QueueError if not ok."""
        for a in args:
            if not re.fullmatch(r"[\w.@:=+-]*", a):
                raise QueueError(tr("Bad argument for the queue: {a}", a=a))
        cmd = (f'{self._env()}python3 "{self.runner}" '
               + " ".join(shlex.quote(a) for a in args))
        try:
            rc, out, err = self._run(self._bash(cmd),
                                     stdin.encode("utf-8") if stdin else None,
                                     timeout)
        except (OSError, subprocess.SubprocessError) as exc:
            raise QueueError(tr("The queue server {host} did not reply: "
                                "{why}", host=self.host, why=exc)) from exc
        got = None
        for line in reversed(out.strip().splitlines()):
            line = line.strip()
            if line.startswith("{"):
                try:
                    got = json.loads(line)
                    break
                except ValueError:
                    pass
        if got is None:
            text = (err or out).strip()
            if "No such file" in text or "can't open file" in text:
                raise QueueError(tr("The queue is not set up on {host}. "
                                    "Click «Set Up» in the «Runs» window.",
                                    host=self.host))
            if "python3" in text and ("not found" in text
                                      or "No such" in text):
                raise QueueError(tr("python3 is not on {host}: the queue "
                                    "needs it.", host=self.host))
            raise QueueError(tr("The queue server {host} did not reply: "
                                "{why}", host=self.host,
                                why=text[-300:] or tr("code {rc}", rc=rc)))
        if not got.get("ok", True):
            raise QueueError(tr("The queue refused: {why}",
                                why=got.get("error", "?")))
        return got

    # --- set up ------------------------------------------------------------

    def version(self) -> int:
        """The runner version on the server; 0: not there."""
        try:
            return int(self.call("version").get("version", 0))
        except QueueError:
            return 0

    def upload_runner(self) -> None:
        data = RUNNER_LOCAL.read_bytes()
        q, name = self.qdir, RUNNER_NAME
        cmd = (f'mkdir -p "{q}" && cat > "{q}/{name}.tmp" && '
               f'mv -f "{q}/{name}.tmp" "{q}/{name}" && wc -c < "{q}/{name}"')
        rc, out, err = self._run(self._bash(cmd), data, 120)
        got = next((int(t) for t in out.split() if t.isdigit()), -1)
        if rc != 0 or got != len(data):
            raise QueueError(tr("Cannot put the queue on {host}: {why}",
                                host=self.host,
                                why=(err or out).strip()[-200:]))

    def ready(self) -> dict:
        """The runner of this version is on the server and its daemon runs.
        A newer runner file makes the running daemon start again by itself."""
        if self.version() != runner_version():
            self.upload_runner()
        return self.call("ensure")

    def setup(self, systemd: bool = True, unit: str = "") -> dict:
        """Put the runner on the server and start it: a systemd user service
        when the server allows it (lingering on), else nohup. The answer
        says which ({"how", "linger", "systemd", "why"})."""
        if self.version() != runner_version():
            self.upload_runner()
        args = ["--unit", unit] if unit else []
        return self.call("install" if systemd else "ensure", *args)

    def uninstall(self, unit: str = "") -> dict:
        return self.call("uninstall", *(["--unit", unit] if unit else []))

    # --- jobs ----------------------------------------------------------------

    def add(self, jobs: list) -> list:
        return list(self.call("add", stdin=json.dumps(jobs)).get("ids", []))

    def list(self, full: bool = False) -> dict:
        return self.call("list", *(["--full"] if full else []))

    def cancel(self, ids) -> dict:
        return self.call("cancel", *ids).get("states", {})

    def hold(self, ids) -> dict:
        return self.call("hold", *ids).get("states", {})

    def release(self, ids) -> dict:
        return self.call("release", *ids).get("states", {})

    def remove(self, ids=(), finished: bool = False) -> list:
        args = list(ids) + (["--finished"] if finished else [])
        return self.call("remove", *args).get("removed", [])

    def move(self, jid: str, where: str = "", to: "int | None" = None) -> int:
        args = [jid] + ([f"--{where}"] if where else []) + (
            ["--to", str(int(to))] if to is not None else [])
        return int(self.call("move", *args).get("index", 0))

    def pause(self) -> None:
        self.call("pause")

    def resume(self) -> None:
        self.call("resume")

    def machines(self, hosts=()) -> dict:
        return self.call("machines", *hosts).get("machines", {})

    def log(self, jid: str, lines: int = 40) -> str:
        return self.call("log", jid, "-n", str(int(lines))).get("log", "")


def queue_host() -> str:
    """The queue server of the settings ("" = the server of «Run on
    Server»)."""
    import fs_settings as fset
    return (fset.get("queue_host") or fset.get("ssh_host") or "").strip()


def same_host(a: str, b: str) -> bool:
    return (a or "").strip().lower() == (b or "").strip().lower()


# --------------------------------------------------------------------------
# Jobs
# --------------------------------------------------------------------------

def run_steps(cfg, backend, dist: str = "", mesh_key: str = "",
              remesh: bool = False) -> list:
    """The steps of a run of `cfg` — «Run» of the window, the queue and
    `ofs` take them from here."""
    import fs_case as fc
    return be.build_steps(
        cfg.solver.n_procs, backend,
        # Not solver.potential_init: in the compressible mode potentialFoam
        # does not fit, whatever the check box says.
        cfg.use_potential_init, cfg.solver.run_check_mesh,
        two_stage=cfg.solver.two_stage,
        solver=fc.solver_name(cfg),
        topo_set=cfg.needs_topo_set,
        warm=(fc.SOLVER_OF[(cfg.flow.regime, True)] if cfg.warm_start else ""),
        dist=dist, mesh_key=mesh_key, remesh=remesh)


def step_groups(steps) -> list:
    """The group (STEP_GROUPS) of each step."""
    out, mesh_done = [], False
    for s in steps:
        sid = be.step_id(s.name)
        if sid in _RECON_STEPS:
            out.append("reconstruct")
        elif sid in _MESH_STEPS or (sid == "restore0" and not mesh_done):
            out.append("mesh")
        else:
            out.append("solve")
        if sid == "restore0" and out[-1] == "mesh":
            mesh_done = True
        elif sid in ("snappyHexMesh", be.STEP_MESH_LOAD, "checkMesh"):
            mesh_done = True
    return out


def parse_groups(text: str) -> list:
    """«mesh,solve» -> ["mesh", "solve"]. ValueError on an unknown name."""
    names = [t.strip().lower() for t in re.split(r"[,\s]+", text or "") if t.strip()]
    bad = [n for n in names if n not in STEP_GROUPS]
    if bad or not names:
        raise ValueError(tr("Unknown steps: {bad}. Use {groups}.",
                            bad=", ".join(bad) or "-",
                            groups=", ".join(STEP_GROUPS)))
    return names


def select_steps(steps, groups) -> list:
    if not groups or set(groups) >= set(STEP_GROUPS):
        return list(steps)
    want = set(groups)
    return [s for s, g in zip(steps, step_groups(steps)) if g in want]


def dist_nodes(machines, key: str, user: str) -> list:
    """The other machines of a foamdist run, as the queue server reaches them
    (user@address, the key on the server). The runner itself finds which
    address is the queue server."""
    out = []
    for m in machines:
        u = m.user or user
        out.append({"host": f"{u + '@' if u else ''}{m.addr}",
                    "key": m.key or key})
    return out


def new_token() -> str:
    return secrets.token_hex(4)


def script_job(backend, remote_dir: str, steps, *, name: str, case_dir: str,
               queue: str, stop_on_bad_mesh: bool = True, series: str = "",
               after=(), not_before: float = 0.0, nodes=(), share: bool = False,
               hold: bool = False, meta: "dict | None" = None,
               origin: str = "window", put=None) -> dict:
    """Make the run script of a queued job, put it on the machine of the run,
    and return the job (for Client.add).

    The script is the one of «Run on Server» (fs_backend.detached_script);
    only its file name is studio/q_<token>.sh, so that two jobs of one folder
    (a chain) do not overwrite each other. `put(backend, dir, text, name)`
    replaces fs_backend.put_script in the tests.
    """
    token = new_token()
    sname = f"q_{token}.sh"
    smeta = {"name": name, "case_dir": str(case_dir),
             "started": be.START_LATER,
             "steps": [be.step_id(s.name) for s in steps], "queue": token}
    script = be.detached_script(steps, remote_dir, backend.bashrc, smeta,
                                stop_on_bad_mesh=stop_on_bad_mesh,
                                engine=backend.engine, image=backend.image)
    (put or be.put_script)(backend, remote_dir, script, sname)
    host = "" if same_host(backend.host, queue) else backend.host
    return {
        "name": name, "series": series, "host": host, "via": backend.host,
        "dir": remote_dir, "script": sname,
        "launch": be.queue_launch_command(remote_dir, sname, token),
        "stop": be.stop_command(remote_dir),
        "after": list(after), "not_before": float(not_before or 0),
        "nodes": list(nodes), "share": bool(share), "hold": bool(hold),
        "case_dir": str(case_dir), "steps": smeta["steps"],
        "meta": dict(meta or {}), "origin": origin,
    }


# --------------------------------------------------------------------------
# Words for the person
# --------------------------------------------------------------------------

def state_text(state: str) -> str:
    """A state of the queue for the person (the state stays an ASCII id)."""
    if state == "queued":
        return tr("queued", ctx="queue")
    if state == "held":
        return tr("held", ctx="queue")
    if state == "running":
        return tr("running", ctx="queue")
    if state == "done":
        return tr("done", ctx="queue")
    if state == "failed":
        return tr("failed", ctx="queue")
    if state == "cancelled":
        return tr("cancelled", ctx="queue")
    return tr("unknown", ctx="queue")


def wait_text(why: str) -> str:
    """Why a queued job did not start yet (the «wait» of the runner)."""
    if not why:
        return ""
    head, _, rest = why.partition(":")
    if head == "queue":
        return tr("waits: another job of the queue runs on the machine")
    if head == "unreachable":
        return tr("waits: the machine does not reply")
    if head == "unprobed":
        return tr("waits for the next check")
    return why


def note_text(note: str) -> str:
    """What else runs on the machines of a job (the «note» of the runner).
    Information only: the queue does not wait for it (no limits)."""
    out = []
    for part in (note or "").split(";"):
        head, _, rest = part.partition(":")
        if head == "foam":
            out.append(tr("another run is also on the machine (process {p})",
                          p=rest.replace(",", ", ")))
        elif head == "load":
            out.append(tr("the machine is also busy (load {v})", v=rest))
    from fs_i18n import join
    return join("; ", out) if out else ""


def job_text(job: dict, now: "float | None" = None) -> str:
    """The message of a job for the person: why it failed, why it waits,
    and what else runs on its machine."""
    text = _job_text(job, now)
    if job.get("state") in ("queued", "running") and job.get("note"):
        note = note_text(job["note"])
        if note:
            text = text + " · " + note if text else note
    return text


def _job_text(job: dict, now: "float | None" = None) -> str:
    msg = str(job.get("msg") or "")
    detail = str(job.get("detail") or "")
    state = job.get("state")
    if state == "held":
        return tr("waits for «Release»")
    if state == "queued":
        nb = float(job.get("not_before") or 0)
        now = time.time() if now is None else now
        if nb > now:
            return tr("starts not before {t}", t=fmt_time(nb))
        if job.get("after"):
            return wait_text(job.get("wait", "")) or tr(
                "after {ids}", ids=", ".join(job["after"]))
        return wait_text(job.get("wait", ""))
    if msg == "fail":
        bits = detail.split("\t")
        return be.session_message("fail", bits) or tr("the run failed")
    if msg == "aborted":
        return tr("the run broke off: no process on the machine (a reboot or "
                  "lack of memory)")
    if msg == "nostart":
        return tr("the run did not start (see studio/boot.log)")
    if msg == "guard":
        got = guard.parse_guard(detail)
        if got:
            return guard.guard_text(got[0], got[1], job.get("dir", ""),
                                    job.get("host") or tr("the queue server"))
        return tr("the folder is busy")
    if msg == "launch":
        return tr("cannot start: {why}", why=detail[-160:])
    if msg.startswith("dep:"):
        return tr("not started: job {id} did not finish", id=msg[4:])
    if msg == "stopped":
        return tr("stopped")
    if msg == "cancelled":
        return tr("cancelled by the person")
    if state == "done" and detail:
        return be.session_message("ok", detail.split("\t"))
    if state == "running":
        return be.step_title(job.get("step", "")) if job.get("step") else ""
    return msg


def fmt_time(t: float) -> str:
    if not t:
        return ""
    return time.strftime("%Y-%m-%d %H:%M", time.localtime(float(t)))


def machine_text(job: dict, queue: str) -> str:
    host = job.get("host") or queue
    if job.get("nodes"):
        return tr("{host} + {n} machines", host=host, n=len(job["nodes"]))
    return host


def changes(old: list, new: list) -> list:
    """What to tell the person after a new read of the queue.

    [("job", job) for a job that ended (done/failed/cancelled) since the
    last read; ("series", name, jobs) when the last job of a series
    ended]. A job that was already final at the first read gives nothing.
    """
    before = {j.get("id"): j.get("state") for j in old or []}
    out = []
    ended_series = set()
    for j in new or []:
        st, was = j.get("state"), before.get(j.get("id"))
        if st in FINAL and was is not None and was not in FINAL:
            if st != "cancelled" or j.get("msg") != "cancelled":
                out.append(("job", j))
            if j.get("series"):
                ended_series.add(j["series"])
    for s in sorted(ended_series):
        jobs = [j for j in new if j.get("series") == s]
        if jobs and all(j.get("state") in FINAL for j in jobs):
            out.append(("series", s, jobs))
    return out


def parse_at(text: str, now: "float | None" = None) -> float:
    """«23:30» (the next such time), «2026-11-13 02:00» or epoch seconds ->
    epoch seconds. ValueError on a text that is none of these."""
    text = (text or "").strip()
    now = time.time() if now is None else now
    if re.fullmatch(r"\d{9,}(\.\d+)?", text):
        return float(text)
    m = re.fullmatch(r"(\d{1,2}):(\d{2})", text)
    if m:
        lt = time.localtime(now)
        t = time.mktime((lt.tm_year, lt.tm_mon, lt.tm_mday, int(m.group(1)),
                         int(m.group(2)), 0, 0, 0, -1))
        if t <= now:
            t = time.mktime((lt.tm_year, lt.tm_mon, lt.tm_mday + 1,
                             int(m.group(1)), int(m.group(2)), 0, 0, 0, -1))
        return t
    for fmt in ("%Y-%m-%d %H:%M", "%Y-%m-%dT%H:%M", "%Y-%m-%d %H:%M:%S"):
        try:
            return time.mktime(time.strptime(text, fmt))
        except ValueError:
            pass
    raise ValueError(tr("“{text}” is not a time: use HH:MM or "
                        "YYYY-MM-DD HH:MM", text=text))


# --------------------------------------------------------------------------
# A saved case folder -> a queued job (the command line)
# --------------------------------------------------------------------------

def _copy_inputs(src: Path, dst: Path) -> None:
    import shutil
    import fs_library as lib
    dst.mkdir(parents=True, exist_ok=False)
    for item in be.SYNC_INPUTS:
        p = src / item
        if p.is_dir():
            shutil.copytree(p, dst / item, ignore=shutil.ignore_patterns(
                "polyMesh") if item == "constant" else None)
        elif p.is_file():
            shutil.copy2(p, dst / item)
    man = lib.manifest_path(src)
    if man.is_file():
        data = json.loads(man.read_text(encoding="utf-8"))
        data["runs"] = []
        data.pop("server", None)
        (dst / man.name).write_text(json.dumps(data, ensure_ascii=False,
                                               indent=1), encoding="utf-8")


def prepare_case(case_dir: str, *, on: str, queue: str, groups=None,
                 series: str = "", after=(), not_before: float = 0.0,
                 share: bool = False, hold: bool = False, say=None,
                 origin: str = "cli") -> dict:
    """A case folder that «Create Case» made -> a job, ready for Client.add.

    Same rules as «Run on Server»: a used folder gives a new run folder
    (<name>_r2, T43, here and on the server), the upload goes through the
    T43 guard, the steps come from `run_steps`. `on`: the ssh name of the
    machine, or "dist" (foamdist over the machines of the settings; the case
    must have been made with «Distribute» on).
    """
    import fs_case as fc
    import fs_library as lib
    import fs_settings as fset
    say = say or (lambda _t: None)
    src = Path(case_dir).resolve()
    data = lib.read_manifest(src) or {}
    if not isinstance(data.get("cfg"), dict) or not (src / "system").is_dir():
        raise QueueError(tr("{d} is not a case made by the program (no "
                            "studio.json or system/).", d=src))
    cfg = lib.cfg_from_dict(data["cfg"])
    dist = ""
    nodes = []
    host = on
    if on == "dist":
        host = queue
        ms, why = be.check_machines(be.machines_from_json(
            fset.get("dist_machines")))
        if why:
            raise QueueError(tr("Distribution: {why}", why=why))
        total = sum(m.procs for m in ms)
        if cfg.solver.n_procs != total:
            raise QueueError(tr(
                "The case has {n} processes, the machines {total}. Make the "
                "case with «Distribute» on in the program first.",
                n=cfg.solver.n_procs, total=total))
        key, user = fset.get("dist_key"), fset.get("dist_user")
        dist = be.dist_command(ms, key, user, fset.get("dist_net"))
        nodes = dist_nodes(ms, key, user)
    containers = be.containers_setting(fset.get("ssh_containers"))
    say(tr("Checking {host}…", host=host))
    found = be.detect_ssh([host], containers)
    backend = found[0] if found else None
    if backend is None or not backend.ready:
        raise QueueError(tr("{host}: {why}", host=host,
                            why=backend.detail if backend else "?"))
    if dist:
        err = be.ensure_foamdist(backend)
        if err:
            raise QueueError(err)
    name = fc.safe_case_name(src.name)
    used, taken = guard.local_names(src.parent)
    remote = be.server_case_names(backend)
    if not guard.local_used(src):
        used.discard(name.lower())
    new = guard.new_run_name(name, used | remote, taken | remote)
    run_dir = src
    if new != name:
        run_dir = src.parent / new
        say(tr("Case “{name}” already has a run: this run goes to the new "
               "folder “{new}”. The old results stay.", name=name, new=new))
        _copy_inputs(src, run_dir)
        cfg.name = new
        lib.write_manifest(run_dir, cfg, stl_source=data.get("stl_source", ""))
    say(tr("Uploading the case to {host}…", host=host))
    remote_dir = be.sync_to_ssh(backend, str(run_dir), run_dir.name)
    lib.set_server(run_dir, backend.host, remote_dir)
    mkey = be.mesh_key(str(run_dir)) if be.mesh_cache_supported(backend) else ""
    if mkey:
        # The same rule as «Run» of the window (T51). A failed upload does
        # not stop the job (it builds the mesh), but the person must see why
        # the mesh is built again.
        try:
            err = be.ensure_remote_mesh(backend, mkey)
        except Exception as exc:                          # noqa: BLE001
            err = str(exc).strip() or type(exc).__name__
        if err:
            say(tr("Mesh not uploaded, building it: {exc}", exc=err))
        elif err == "":
            say(tr("Mesh {mkey} uploaded — no need to build it.", mkey=mkey))
    steps = select_steps(run_steps(cfg, backend, dist=dist, mesh_key=mkey),
                         groups)
    return script_job(backend, remote_dir, steps, name=run_dir.name,
                      case_dir=str(run_dir), queue=queue,
                      stop_on_bad_mesh=cfg.solver.stop_on_bad_mesh,
                      series=series, after=after, not_before=not_before,
                      nodes=nodes, share=share, hold=hold,
                      meta={"aoa": cfg.flow.aoa_deg}, origin=origin)
