"""`ofs`: the command line of OpenFOAM Studio (T46). No window, no Qt.

For Claude agents, n8n and scripts: put runs in the queue on the server,
read their states, download results and make a report.

    python -m fs_cli queue add cases\\plane --on server1 --at 23:00
    python -m fs_cli queue list --json
    python -m fs_cli queue cancel j7
    python -m fs_cli status cases\\plane
    python -m fs_cli fetch cases\\plane --latest
    python -m fs_cli report plane --out reports\\plane

`ofs.cmd` in the program folder does the same (`ofs queue list`). `--json`
gives one JSON object on stdout (exit code 0: ok, 1: error, 2: bad usage).
Nothing here takes or prints a password or a key: ssh uses the keys of the
user, as the program does.

On the server itself, without this program, the queue answers directly:
`python3 ~/openfoam-studio/.queue/fs_queue_runner.py list`.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path

if __name__ == "__main__" and __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parent))

import fs_compat  # noqa: F401,E402  (first of our modules)
import fs_backend as be                                       # noqa: E402
import fs_i18n                                                # noqa: E402
import fs_queue as fq                                         # noqa: E402
import fs_settings as fset                                    # noqa: E402
from fs_i18n import tr                                        # noqa: E402


class Usage(Exception):
    """A bad argument: exit code 2."""


def cases_dir() -> Path:
    got = (fset.get("cases_dir") or "").strip()
    if got:
        return Path(got)
    import fs_paths
    return fs_paths.data_dir() / "cases"


def find_case(text: str) -> Path:
    """A case folder from a path or from a name in the cases folder."""
    p = Path(text)
    if p.is_dir():
        return p.resolve()
    q = cases_dir() / text
    if q.is_dir():
        return q.resolve()
    raise Usage(tr("No case folder “{c}” (also not in {d}).", c=text,
                   d=cases_dir()))


def client(args) -> fq.Client:
    host = (args.server or fq.queue_host()).strip()
    if not host:
        raise Usage(tr("No queue server: give --server or set the server in "
                       "the program."))
    return fq.Client(host, qdir=args.queue_dir or fq.QUEUE_DIR)


# --------------------------------------------------------------------------
# Output
# --------------------------------------------------------------------------

def _row(job: dict, queue: str) -> str:
    when = fq.fmt_time(job.get("started") or job.get("created") or 0)
    text = fq.job_text(job)
    return (f"{job.get('id', '?'):>5}  {fq.state_text(job.get('state', '')):<12}"
            f" {fq.machine_text(job, queue):<18} {job.get('name', ''):<28} "
            f"{when:<16}  {text}")


def show_list(res: dict, queue: str, out, show_all: bool) -> None:
    jobs = res.get("jobs", [])
    if not show_all:
        jobs = [j for j in jobs if j.get("state") in fq.ACTIVE
                or time.time() - float(j.get("ended") or 0) < 24 * 3600]
    d = res.get("daemon", {})
    head = tr("Queue on {host}: {state}", host=queue,
              state=(tr("paused") if res.get("paused") else tr("on")))
    head += "; " + (tr("the runner works (systemd)") if d.get("alive") and
                    d.get("systemd") else tr("the runner works") if
                    d.get("alive") else tr("THE RUNNER IS OFF: run "
                                           "«ofs queue setup»"))
    print(head, file=out)
    if not jobs:
        print(tr("No jobs."), file=out)
    for j in jobs:
        print(_row(j, queue), file=out)


# --------------------------------------------------------------------------
# Commands
# --------------------------------------------------------------------------

def cmd_queue(args, out) -> dict:
    c = client(args)
    what = args.what
    if what == "setup":
        info = c.setup(systemd=not args.no_systemd)
        if not args.json:
            print(setup_text(info), file=out)
        return info
    if what == "list":
        res = c.list()
        if not args.json:
            show_list(res, c.host, out, args.all)
        return res
    if what == "add":
        if args.chain and args.after:
            raise Usage(tr("Use --chain or --after, not both."))
        groups = fq.parse_groups(args.steps) if args.steps else None
        try:
            at = fq.parse_at(args.at) if args.at else 0.0
        except ValueError as exc:
            raise Usage(str(exc))
        on = args.on or c.host
        if c.local and on == c.host:
            raise Usage(tr("Give --on: the machine of the run."))
        say = (lambda t: None) if args.json else (lambda t: print(t, file=out))
        jobs = []
        for i, name in enumerate(args.cases):
            after = list(args.after or [])
            if args.chain and i > 0:
                after = [f"@{i - 1}"]
            jobs.append(fq.prepare_case(
                str(find_case(name)), on=on, queue=c.host, groups=groups,
                series=args.series or "", after=after, not_before=at,
                share=args.share, hold=args.hold, say=say))
        c.ready()
        ids = c.add(jobs)
        res = {"ok": True, "ids": ids,
               "jobs": [{"id": i, "name": j["name"], "dir": j["dir"],
                         "host": j.get("via") or j.get("host") or c.host}
                        for i, j in zip(ids, jobs)]}
        if not args.json:
            for r in res["jobs"]:
                print(tr("{id}: {name} on {host} ({dir})", **r), file=out)
        return res
    if what in ("cancel", "hold", "release"):
        states = getattr(c, what)(args.ids)
        if not args.json:
            for i, st in states.items():
                print(f"{i}: {fq.state_text(st)}", file=out)
        return {"ok": True, "states": states}
    if what == "remove":
        removed = c.remove(args.ids, finished=args.finished)
        if not args.json:
            print(tr("Removed: {ids}", ids=", ".join(removed) or "-"), file=out)
        return {"ok": True, "removed": removed}
    if what == "move":
        where = ("up" if args.up else "down" if args.down
                 else "top" if args.top else "")
        if not where and args.to is None:
            raise Usage(tr("Give --up, --down, --top or --to N."))
        idx = c.move(args.id, where, args.to)
        if not args.json:
            print(tr("{id} is now number {n}.", id=args.id, n=idx + 1),
                  file=out)
        return {"ok": True, "index": idx}
    if what in ("pause", "resume"):
        getattr(c, what)()
        if not args.json:
            print(tr("The queue is paused: running jobs go on, new ones "
                     "wait.") if what == "pause" else
                  tr("The queue goes on."), file=out)
        return {"ok": True, "paused": what == "pause"}
    if what == "log":
        text = c.log(args.id, args.n)
        if not args.json:
            print(be.render_text(text), file=out)
        return {"ok": True, "id": args.id, "log": text}
    if what == "machines":
        res = c.machines(args.hosts)
        if not args.json:
            for h, m in res.items():
                line = (tr("free") if m.get("free") else
                        fq.wait_text(m.get("why", "")))
                note = fq.note_text(m.get("note", ""))
                print(f"{h}: " + (line + " · " + note if note else line),
                      file=out)
        return {"ok": True, "machines": res}
    raise Usage(what)


def setup_text(info: dict) -> str:
    """What «setup» did, for the person."""
    if info.get("how") == "systemd" and info.get("systemd", True):
        return tr("The queue works on the server as a systemd user service: "
                  "it goes on without the laptop and starts again after a "
                  "reboot of the server.")
    text = tr("The queue works on the server (nohup): it goes on without "
              "the laptop, but it does not start again after a reboot of "
              "the server. The program or «ofs» starts it again at the next "
              "use.")
    if info.get("why") == "linger_off":
        text += " " + tr("Lingering is off for your user on the server, so "
                         "the systemd service is not used. Lingering was "
                         "not changed.")
    return text


def _backend_for(host: str) -> "be.Backend":
    return be.Backend(kind="ssh", label=host, host=host)


def cmd_status(args, out) -> dict:
    case = find_case(args.case)
    host, remote = be.run_server_dir(str(case))
    host = host or fset.get("ssh_host") or ""
    if not host:
        raise Usage(tr("The case has no server record and no server is set."))
    s = be.read_session(_backend_for(host), remote)
    res = {"ok": True, "case": str(case), "host": host, "dir": remote}
    if s is None:
        res["status"] = "none"
    else:
        res.update(status=("aborted" if s.stale else s.status), step=s.step,
                   message=s.message, alive=s.alive, started=s.started,
                   age=s.age, log_lines=s.log_lines)
    queue = (args.server or fq.queue_host()).strip()
    if queue:
        try:
            jobs = fq.Client(queue, qdir=args.queue_dir or fq.QUEUE_DIR) \
                .list().get("jobs", [])
            mine = [j for j in jobs if j.get("dir") == remote
                    and fq.same_host(j.get("via") or queue, host)]
            if mine:
                res["queue"] = mine[-1]
        except fq.QueueError:
            pass
    if not args.json:
        if s is None:
            print(tr("{host}:{dir}: no run there.", host=host, dir=remote),
                  file=out)
        else:
            print(tr("{name} on {host}: {state}", name=case.name, host=host,
                     state=s.human()), file=out)
            if s.step:
                print(tr("step: {s}", s=s.step_text), file=out)
            if s.message:
                print(s.message, file=out)
        if res.get("queue"):
            j = res["queue"]
            print(tr("queue: {id} {state} {text}", id=j["id"],
                     state=fq.state_text(j["state"]), text=fq.job_text(j)),
                  file=out)
    return res


def cmd_fetch(args, out) -> dict:
    """The results of the last run of a case: the same download as the
    program (fs_backend.sync_from_ssh, then fs_servertimes for the times)."""
    import fs_servertimes as fst
    case = find_case(args.case)
    host, _ = be.run_server_dir(str(case))
    host = host or fset.get("ssh_host") or ""
    if not host:
        raise Usage(tr("The case has no server record and no server is set."))
    backend = _backend_for(host)
    remote = fst.run_folder(str(case))
    say = (lambda t, f=0.0: None) if args.json else (
        lambda t, f=0.0: print(t, file=out))
    res = {"ok": True, "case": str(case), "host": host, "dir": remote}
    if not fst.local_cells(str(case)) or not (args.from_t is not None
                                              or args.to_t is not None):
        say(tr("Downloading the results of {dir} from {host}…", dir=remote,
               host=host))
        be.sync_from_ssh(backend, remote, str(case))
        res["base"] = True
    if args.from_t is not None or args.to_t is not None or args.all:
        listing = fst.list_times(backend, remote)
        if not listing.ok:
            raise fq.QueueError(listing.why)
        if args.all:
            names = fst.missing_names(listing, args.mode,
                                      fst.local_have(str(case), args.mode))
        else:
            a = args.from_t if args.from_t is not None else -1e300
            b = args.to_t if args.to_t is not None else 1e300
            names = fst.range_names(listing, args.mode, a, b)
        if names:
            got = fst.get_times(backend, str(case), remote, listing, names,
                                args.mode, say=say)
            res.update(times=got.get("came", []),
                       reconstructed=got.get("reconstructed", []),
                       bytes=got.get("bytes", 0))
        else:
            res["times"] = []
            say(tr("No more times to download."))
    if not args.json:
        print(tr("Done: {case}", case=case), file=out)
    return res


def cmd_report(args, out) -> dict:
    """The report of an angle series: the series from fs_polars.find_series
    (the newest <name>_r<n> of each angle), the report from the command line
    of fs_report (T45) — the same folder as the «Polars» window makes."""
    import contextlib
    import io
    import fs_library as lib
    import fs_paths
    import fs_polars as pol
    import fs_report
    import fs_case as fc
    root = Path(args.cases) if args.cases else cases_dir()
    paths = []
    p = Path(args.series)
    if p.is_dir() and (p / "system").is_dir():
        paths = [str(p)]
    else:
        infos = lib.scan(root, with_size=False)
        for s in pol.find_series(infos):
            if s.base.lower() == args.series.lower():
                paths = [c.path for c in s.members]
        if not paths:
            raise Usage(tr("No angle series “{s}” in {d}.", s=args.series,
                           d=root))
    folder = Path(args.out) if args.out else (
        fs_paths.data_dir() / "reports" / fc.safe_case_name(
            f"{p.name}_{time.strftime('%Y%m%d-%H%M')}"))
    argv = paths + ["--out", str(folder)]
    if args.no_images:
        argv.append("--no-images")
    if args.csv_only:
        argv.append("--csv-only")
    text = io.StringIO()
    with contextlib.redirect_stdout(text):
        code = fs_report.main(argv)
    lines = [ln for ln in text.getvalue().splitlines() if ln.strip()]
    if not args.json:
        print(text.getvalue().rstrip(), file=out)
    if code:
        raise fq.QueueError(lines[-1] if lines else tr("code {rc}", rc=code))
    return {"ok": True, "index": lines[-1] if lines else "",
            "folder": str(folder), "csv": str(folder / "polar.csv"),
            "cases": paths,
            "notes": [ln[2:] for ln in lines if ln.startswith("! ")]}


# --------------------------------------------------------------------------

def parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="ofs", description=tr("OpenFOAM Studio without the window: the run "
        "queue on the server, states, downloads, reports."))
    p.add_argument("--json", action="store_true",
                   help=tr("one JSON object on stdout"))
    p.add_argument("--server", default="",
                   help=tr("the queue server (default: the setting of the "
                   "program; «local» on the server itself)"))
    p.add_argument("--queue-dir", default="", help=argparse.SUPPRESS)
    p.add_argument("--lang", choices=sorted(fs_i18n.LANGUAGES), default=None)
    sub = p.add_subparsers(dest="cmd", required=True)

    q = sub.add_parser("queue", help=tr("the run queue"))
    qs = q.add_subparsers(dest="what", required=True)
    a = qs.add_parser("add", help=tr("put case folders in the queue"))
    a.add_argument("cases", nargs="+")
    a.add_argument("--steps", default="",
                   help=tr("mesh,solve,reconstruct (default: all)"))
    a.add_argument("--on", default="",
                   help=tr("the machine: an ssh name (server1, node4) or «dist»"))
    a.add_argument("--after", action="append", help=tr("start after this job id"))
    a.add_argument("--chain", action="store_true",
                   help=tr("each case starts after the one before it"))
    a.add_argument("--at", default="", help=tr("not before: HH:MM or "
                   "YYYY-MM-DD HH:MM"))
    a.add_argument("--series", default="", help=tr("a name for the group"))
    a.add_argument("--share", action="store_true",
                   help=tr("may run beside another job on the same machine"))
    a.add_argument("--hold", action="store_true", help=tr("add it held"))
    ls = qs.add_parser("list")
    ls.add_argument("--all", action="store_true",
                    help=tr("also jobs that ended more than a day ago"))
    for name in ("cancel", "hold", "release"):
        c = qs.add_parser(name)
        c.add_argument("ids", nargs="+")
    r = qs.add_parser("remove")
    r.add_argument("ids", nargs="*")
    r.add_argument("--finished", action="store_true")
    m = qs.add_parser("move")
    m.add_argument("id")
    m.add_argument("--to", type=int)
    m.add_argument("--up", action="store_true")
    m.add_argument("--down", action="store_true")
    m.add_argument("--top", action="store_true")
    qs.add_parser("pause")
    qs.add_parser("resume")
    lg = qs.add_parser("log")
    lg.add_argument("id")
    lg.add_argument("-n", type=int, default=40)
    su = qs.add_parser("setup", help=tr("put the queue on the server"))
    su.add_argument("--no-systemd", action="store_true")
    mc = qs.add_parser("machines")
    mc.add_argument("hosts", nargs="*")

    s = sub.add_parser("status", help=tr("the state of the last run of a case"))
    s.add_argument("case")
    f = sub.add_parser("fetch", help=tr("download the results of a case"))
    f.add_argument("case")
    g = f.add_mutually_exclusive_group()
    g.add_argument("--latest", action="store_true",
                   help=tr("the last time (the normal download)"))
    g.add_argument("--all", action="store_true", help=tr("each written time"))
    f.add_argument("--from", dest="from_t", type=float)
    f.add_argument("--to", dest="to_t", type=float)
    f.add_argument("--mode", choices=("view", "all", "surf"), default="view",
                   help=tr("fields for viewing, all fields, or surfaces only"))
    rp = sub.add_parser("report", help=tr("the report of an angle series"))
    rp.add_argument("series", help=tr("the base name (plane) or a case folder"))
    rp.add_argument("--out", default="")
    rp.add_argument("--cases", default="", help=tr("the cases folder"))
    rp.add_argument("--no-images", action="store_true")
    rp.add_argument("--csv-only", action="store_true",
                    help=tr("only the table polar.csv"))
    return p


def main(argv=None, out=None) -> int:
    out = out or sys.stdout
    argv = list(sys.argv[1:] if argv is None else argv)
    lang = next((argv[i + 1] for i, a in enumerate(argv[:-1])
                 if a == "--lang"), None)
    fs_i18n.set_language(lang if lang in fs_i18n.LANGUAGES else (
        os.environ.get("FS_LANG") or fset.get("ui_language")))
    try:
        args = parser().parse_args(argv)
    except SystemExit as exc:
        return int(exc.code or 0) if exc.code not in (None, 0) else 0
    fn ={"queue": cmd_queue, "status": cmd_status, "fetch": cmd_fetch,
          "report": cmd_report}[args.cmd]
    try:
        res = fn(args, out)
        code = 0
    except Usage as exc:
        res, code = {"ok": False, "error": str(exc)}, 2
    except (fq.QueueError, guard_errors(), RuntimeError, OSError,
            ValueError) as exc:
        res, code = {"ok": False, "error": str(exc)}, 1
    if args.json:
        out.write(json.dumps(res, ensure_ascii=False, default=str) + "\n")
    elif code:
        print(tr("Error: {e}", e=res["error"]), file=sys.stderr)
    return code


def guard_errors():
    import fs_caseguard as guard
    return guard.CaseBusy


if __name__ == "__main__":
    for _s in (sys.stdout, sys.stderr):
        try:
            _s.reconfigure(encoding="utf-8")
        except AttributeError:
            pass
    sys.exit(main())
