//! foamdist — EXPERIMENTAL: one OpenFOAM run split across several machines.
//!
//! OpenFOAM already splits the mesh into pieces (decomposePar) and runs one
//! MPI process per piece. foamdist puts those processes on several machines:
//!
//!   probe  — what each machine has: cores, load, the container image;
//!   run    — decompose, send each machine ONLY its pieces, run the solver
//!            with mpirun, bring the pieces back, clean up;
//!   clean  — remove the containers and the case copies on the machines.
//!
//! It runs on the HEAD machine, where the case is. Every MPI process runs in
//! the same OpenFOAM container image (one MPI version everywhere — mixing a
//! native OpenMPI 5 with the image's 4.1 does not work). mpirun starts in a
//! container on the head; it starts the daemons on the other machines
//! through an rsh agent that does `ssh host podman run … orted …`.
//!
//! Measured (2026-11-09, 1.37 M cells, rhoPimpleFoam): 4 processes on one
//! i7-7700 — 6.06 s/step; 12 on three of them over 1 GbE — 2.01 s/step.
//!
//! Traps this code avoids (each one cost a hang):
//! - podman refuses `--ipc=host` with `--shm-size`;
//! - with `plm_rsh_no_tree_spawn` OpenMPI adds `--daemonize`: the daemon
//!   forks away, the container ends, mpirun waits forever → the agent drops
//!   the flag (tree spawn stays off: only the head has the ssh key);
//! - the remote daemon checks that the agent path exists → agent everywhere;
//! - with `--network=host` hwloc reaches the desktop X server through the
//!   shared abstract socket and hangs on a machine with a desktop session →
//!   HWLOC_COMPONENTS without gl/opencl/…;
//! - MPI data ports start at 1024, Fedora opens 1025–65535 → ports 10000+;
//! - a killed run leaves containers behind → all carry a label and are
//!   removed by it.

use std::collections::BTreeMap;
use std::env;
use std::fs;
use std::io::Write;
use std::path::{Path, PathBuf};
use std::process::{Command, ExitCode, Stdio};
use std::thread;
use std::time::{Instant, SystemTime, UNIX_EPOCH};

mod bench;

const IMAGE: &str = "docker.io/opencfd/openfoam-run:2512";
const BASHRC: &str = "/usr/lib/openfoam/openfoam2512/etc/bashrc";
const LABEL: &str = "foamdist=1";
const HWLOC: &str = "HWLOC_COMPONENTS=-gl,-opencl,-cuda,-nvml,-rsmi,-levelzero";
const REMOTE_ROOT: &str = "foamdist"; // under the remote user's home

/// The rsh agent. %IMAGE% and %USER% are filled in when it is written.
/// Each machine can have its own user and key: /keys/users has the lines
/// "address user", /keys/id-<address> is that machine's key. A machine with
/// neither uses /keys/id and %USER%.
const AGENT: &str = r#"#!/bin/bash
# Written by foamdist. OpenMPI rsh agent: start the remote daemon INSIDE the
# OpenFOAM container. $1 = host, the rest = the daemon command line.
# --daemonize is dropped: a forked daemon ends the container (see foamdist).
host=$1
shift
args=()
for a in "$@"; do
    [ "$a" = "--daemonize" ] || args+=("$a")
done
inner="source %BASHRC% >/dev/null 2>&1; exec ${args[*]}"
k="/keys/id-$host"
[ -f "$k" ] || k=/keys/id
u=$(awk -v h="$host" '$1 == h { print $2; exit }' /keys/users 2>/dev/null)
[ -n "$u" ] || u=%USER%
exec ssh -i "$k" -o UserKnownHostsFile=/keys/known_hosts \
    -o StrictHostKeyChecking=accept-new -o BatchMode=yes -o LogLevel=ERROR \
    "$u@$host" \
    podman run --rm --network=host --userns=keep-id --ipc=host \
    -e HOME=/tmp -e %HWLOC% --label %LABEL% \
    -v "\$HOME/%ROOT%/$FOAMDIST_CASE:/case:Z" \
    --entrypoint bash "%IMAGE%" -c "$(printf %q "$inner")"
"#;

// ---------------------------------------------------------------------------
// Small helpers
// ---------------------------------------------------------------------------

fn now() -> u64 {
    SystemTime::now().duration_since(UNIX_EPOCH).map(|d| d.as_secs()).unwrap_or(0)
}

fn json_str(s: &str) -> String {
    let mut out = String::from("\"");
    for c in s.chars() {
        match c {
            '"' => out.push_str("\\\""),
            '\\' => out.push_str("\\\\"),
            '\n' => out.push_str("\\n"),
            '\r' => {}
            '\t' => out.push_str("\\t"),
            c if (c as u32) < 0x20 => out.push_str(&format!("\\u{:04x}", c as u32)),
            c => out.push(c),
        }
    }
    out.push('"');
    out
}

/// Quote for a POSIX shell (single quotes).
fn sh_quote(s: &str) -> String {
    format!("'{}'", s.replace('\'', "'\\''"))
}

fn home() -> PathBuf {
    PathBuf::from(env::var("HOME").unwrap_or_else(|_| "/tmp".into()))
}

fn local_ips() -> Vec<String> {
    let out = Command::new("hostname").arg("-I").output();
    match out {
        Ok(o) => String::from_utf8_lossy(&o.stdout)
            .split_whitespace()
            .map(String::from)
            .collect(),
        Err(_) => Vec::new(),
    }
}

#[derive(Clone, Debug)]
struct Host {
    addr: String,
    slots: usize,
    local: bool,
    first_rank: usize,
    /// Who and with which key (a path on the head) to log in as. Every
    /// machine can have its own: machines of different owners or sites do
    /// not share one account.
    user: String,
    key: PathBuf,
}

/// "user@address" or "address" → (user, address); no user → `default`.
fn split_user(s: &str, default: &str) -> (String, String) {
    match s.split_once('@') {
        Some((u, a)) if !u.is_empty() => (u.to_string(), a.to_string()),
        _ => (default.to_string(), s.trim_start_matches('@').to_string()),
    }
}

/// --keys "address=path,address=path": a key for each machine that needs
/// its own. The others use --key.
fn parse_keys(spec: &str) -> Result<BTreeMap<String, PathBuf>, String> {
    let mut m = BTreeMap::new();
    for part in spec.split(',').map(str::trim).filter(|s| !s.is_empty()) {
        let Some((a, k)) = part.split_once('=') else {
            return Err(format!("{part}: need address=key"));
        };
        m.insert(a.trim().to_string(), PathBuf::from(k.trim()));
    }
    Ok(m)
}

fn parse_hosts(spec: &str, user: &str, key: &Path,
               keys: &BTreeMap<String, PathBuf>) -> Result<Vec<Host>, String> {
    let ips = local_ips();
    let mut hosts = Vec::new();
    for part in spec.split(',').map(str::trim).filter(|s| !s.is_empty()) {
        let (huser, part_addr) = split_user(part, user);
        let (addr, slots) = match part_addr.rsplit_once(':') {
            Some((a, s)) => (a.to_string(), s.parse::<usize>().map_err(|_| {
                format!("{part}: slots must be a number (host:slots)")
            })?),
            None => return Err(format!("{part}: need host:slots")),
        };
        if slots == 0 {
            return Err(format!("{part}: zero slots"));
        }
        let local = ips.iter().any(|ip| ip == &addr) || addr == "localhost";
        let hkey = keys.get(&addr).cloned().unwrap_or_else(|| key.to_path_buf());
        hosts.push(Host { addr, slots, local, first_rank: 0, user: huser, key: hkey });
    }
    if hosts.is_empty() {
        return Err("no hosts".into());
    }
    // The head goes first: rank 0 (the master, which writes postProcessing
    // and the log) must run where the case is.
    hosts.sort_by_key(|h| !h.local);
    if !hosts[0].local {
        return Err("the head machine (this one) must be in --hosts: rank 0 \
                    runs where the case is".into());
    }
    let mut r = 0;
    for h in hosts.iter_mut() {
        h.first_rank = r;
        r += h.slots;
    }
    Ok(hosts)
}

/// Networks for MPI traffic: the /24 of every machine's IPv4 address.
///
/// It was one fixed /24 network written into the code, so a machine on any other
/// network was refused by MPI without a word. Loopback is left out: the head
/// can be "localhost", and MPI must not try to reach the others through it.
fn mpi_nets(hosts: &[Host]) -> String {
    use std::net::ToSocketAddrs;
    let mut nets: Vec<String> = Vec::new();
    for h in hosts {
        let addrs: Vec<std::net::IpAddr> = if h.local {
            local_ips().iter().filter_map(|s| s.parse().ok()).collect()
        } else {
            (h.addr.as_str(), 22).to_socket_addrs().map(|it| it.map(|a| a.ip()).collect())
                .unwrap_or_default()
        };
        let remote_v4: Vec<std::net::Ipv4Addr> = addrs.iter().filter_map(|ip| match ip {
            std::net::IpAddr::V4(v) if !v.is_loopback() => Some(*v),
            _ => None,
        }).collect();
        // The head has many addresses (LAN, docker, tailscale): take only
        // those that share a /24 with some other machine.
        for v in remote_v4 {
            let o = v.octets();
            let net = format!("{}.{}.{}.0/24", o[0], o[1], o[2]);
            if h.local && !hosts.iter().any(|x| !x.local && x.addr.starts_with(
                &format!("{}.{}.{}.", o[0], o[1], o[2]))) {
                continue;
            }
            if !nets.contains(&net) {
                nets.push(net);
            }
        }
    }
    nets.join(",")
}

fn ssh_cmd(key: &Path, user: &str, host: &str, remote: &str) -> Command {
    let mut c = Command::new("ssh");
    c.arg("-i").arg(key)
        .args(["-o", "BatchMode=yes", "-o", "LogLevel=ERROR",
               "-o", "StrictHostKeyChecking=accept-new", "-o", "ConnectTimeout=10"])
        .arg(format!("{user}@{host}"))
        .arg(remote);
    c
}

fn run_ok(mut c: Command) -> Result<String, String> {
    let out = c.stdin(Stdio::null()).output().map_err(|e| format!("{e}"))?;
    let text = String::from_utf8_lossy(&out.stdout).to_string()
        + &String::from_utf8_lossy(&out.stderr);
    if out.status.success() {
        Ok(text)
    } else {
        Err(text.trim().to_string())
    }
}

// ---------------------------------------------------------------------------
// Status file
// ---------------------------------------------------------------------------

struct Status {
    path: PathBuf,
    fields: BTreeMap<String, String>, // key -> JSON value
}

impl Status {
    fn new(case: &Path) -> Status {
        Status { path: case.join("foamdist.json"), fields: BTreeMap::new() }
    }
    fn set(&mut self, k: &str, v: &str) {
        self.fields.insert(k.into(), json_str(v));
        self.write();
    }
    fn set_raw(&mut self, k: &str, v: String) {
        self.fields.insert(k.into(), v);
        self.write();
    }
    fn write(&self) {
        let body: Vec<String> = self.fields.iter().map(|(k, v)| format!("  {}: {}", json_str(k), v)).collect();
        let text = format!("{{\n{}\n}}\n", body.join(",\n"));
        let tmp = self.path.with_extension("json.part");
        if fs::write(&tmp, text).is_ok() {
            let _ = fs::rename(&tmp, &self.path);
        }
    }
}

fn say(log: &mut fs::File, text: &str) {
    println!("{text}");
    let _ = writeln!(log, "{text}");
}

// ---------------------------------------------------------------------------
// Container commands
// ---------------------------------------------------------------------------

struct Engine {
    exe: String, // podman | docker
}

impl Engine {
    fn detect(want: &str) -> Engine {
        if !want.is_empty() {
            return Engine { exe: want.into() };
        }
        for e in ["podman", "docker"] {
            if Command::new(e).arg("--version").stdout(Stdio::null())
                .stderr(Stdio::null()).status().map(|s| s.success()).unwrap_or(false) {
                return Engine { exe: e.into() };
            }
        }
        Engine { exe: "podman".into() }
    }

    /// `engine run` of `script` in the image with the case at /case.
    fn run(&self, image: &str, case: &Path, keys: Option<&Path>, env: &[(&str, &str)],
           script: &str) -> Command {
        let mut c = Command::new(&self.exe);
        c.args(["run", "--rm", "--network=host", "--ipc=host", "-e", "HOME=/tmp",
                "-e", HWLOC, "--label", LABEL]);
        if self.exe == "podman" {
            c.arg("--userns=keep-id");
        } else {
            // docker: run as this user, so the files stay ours.
            let uid = run_ok({ let mut u = Command::new("id"); u.arg("-u"); u })
                .unwrap_or_default();
            let gid = run_ok({ let mut g = Command::new("id"); g.arg("-g"); g })
                .unwrap_or_default();
            c.arg("--user").arg(format!("{}:{}", uid.trim(), gid.trim()));
            // ssh refuses to run for a uid without a passwd entry ("No
            // user exists for uid"); podman's keep-id adds one, docker not.
            c.args(["-v", "/etc/passwd:/etc/passwd:ro", "-v", "/etc/group:/etc/group:ro"]);
        }
        for (k, v) in env {
            c.arg("-e").arg(format!("{k}={v}"));
        }
        c.arg("-v").arg(format!("{}:/case:Z", case.display()));
        if let Some(k) = keys {
            c.arg("-v").arg(format!("{}:/keys:Z", k.display()));
        }
        c.args(["--entrypoint", "bash", image, "-c"]);
        c.arg(format!("source {BASHRC} >/dev/null 2>&1; cd /case; {script}"));
        c
    }
}

// ---------------------------------------------------------------------------
// probe
// ---------------------------------------------------------------------------

fn probe(hosts: &[String], user: &str, key: &Path, keys: &BTreeMap<String, PathBuf>,
         image: &str) -> ExitCode {
    let script = format!(
        "echo hostname=$(hostname); echo cores=$(lscpu -p=core,socket 2>/dev/null | grep -v '^#' | sort -u | wc -l); \
         echo threads=$(nproc); echo load=$(cut -d' ' -f1 /proc/loadavg); \
         echo mem_gb=$(awk '/MemAvailable/{{printf \"%.1f\", $2/1048576}}' /proc/meminfo); \
         if podman image exists {image} 2>/dev/null; then echo image=yes; \
         elif docker image inspect {image} >/dev/null 2>&1; then echo image=yes; \
         else echo image=no; fi; \
         echo busy=$( (podman ps -q --filter label={LABEL} 2>/dev/null; \
                       docker ps -q --filter label={LABEL} 2>/dev/null) | wc -l)");
    let ips = local_ips();
    let handles: Vec<_> = hosts.iter().map(|spec| {
        let (u, h) = split_user(spec, user);
        let k = keys.get(&h).cloned().unwrap_or_else(|| key.to_path_buf());
        let s = script.clone();
        // The head is probed here, not over ssh: its user is not the nodes'
        // user, and the key does not open it.
        let local = h == "localhost" || ips.iter().any(|ip| ip == &h);
        thread::spawn(move || {
            let t0 = Instant::now();
            let got = if local {
                let mut c = Command::new("bash");
                c.arg("-c").arg(&s);
                run_ok(c).map(|t| t + "head=yes\n")
            } else {
                run_ok(ssh_cmd(&k, &u, &h, &s))
            };
            (h, got, t0.elapsed().as_millis())
        })
    }).collect();
    let mut all_ok = true;
    for hd in handles {
        let (h, got, ms) = hd.join().unwrap_or(("?".into(), Err("thread".into()), 0));
        match got {
            Ok(text) => {
                let mut kv = vec![format!("\"host\": {}", json_str(&h)),
                                  "\"ok\": true".to_string(),
                                  format!("\"ssh_ms\": {ms}")];
                for line in text.lines() {
                    if let Some((k, v)) = line.split_once('=') {
                        let v = v.trim();
                        let val = if v.parse::<f64>().is_ok() { v.to_string() } else { json_str(v) };
                        kv.push(format!("{}: {}", json_str(k.trim()), val));
                    }
                }
                println!("{{{}}}", kv.join(", "));
            }
            Err(e) => {
                all_ok = false;
                println!("{{\"host\": {}, \"ok\": false, \"error\": {}}}", json_str(&h), json_str(&e));
            }
        }
    }
    if all_ok { ExitCode::SUCCESS } else { ExitCode::from(1) }
}

// ---------------------------------------------------------------------------
// run
// ---------------------------------------------------------------------------

struct RunOpts {
    case: PathBuf,
    hosts: Vec<Host>,
    solver: String,
    image: String,
    user: String,
    key: PathBuf,
    engine: Engine,
    keep: bool,
    decompose: bool,
    reconstruct: bool,
    stream: bool,
    /// Networks for MPI (btl/oob_tcp_if_include). Empty = from the addresses.
    net: String,
}

fn write_agent(dir: &Path, image: &str, user: &str) -> std::io::Result<()> {
    let text = AGENT.replace("%IMAGE%", image).replace("%USER%", user)
        .replace("%BASHRC%", BASHRC).replace("%HWLOC%", HWLOC)
        .replace("%LABEL%", LABEL).replace("%ROOT%", REMOTE_ROOT);
    let p = dir.join("foamdist-agent.sh");
    fs::write(&p, text)?;
    #[cfg(unix)]
    {
        use std::os::unix::fs::PermissionsExt;
        fs::set_permissions(&p, fs::Permissions::from_mode(0o755))?;
    }
    Ok(())
}

fn set_subdomains(case: &Path, n: usize) -> Result<(), String> {
    let p = case.join("system/decomposeParDict");
    let text = fs::read_to_string(&p).map_err(|e| format!("{}: {e}", p.display()))?;
    let mut out = String::new();
    let mut done = false;
    for line in text.lines() {
        if line.trim_start().starts_with("numberOfSubdomains") {
            out.push_str(&format!("numberOfSubdomains {n};\n"));
            done = true;
        } else {
            out.push_str(line);
            out.push('\n');
        }
    }
    if !done {
        return Err("decomposeParDict has no numberOfSubdomains".into());
    }
    fs::write(&p, out).map_err(|e| format!("{e}"))
}

fn processor_dirs(case: &Path) -> usize {
    (0..).take_while(|i| case.join(format!("processor{i}")).is_dir()).count()
}

/// Keys for the head container: a copy, so ~/.ssh is never relabelled.
/// `id` is the common key; `id-<address>` the key of a machine that has its
/// own; `users` the user of every machine (read by the agent).
fn prepare_keys(key: &Path, hosts: &[Host]) -> Result<PathBuf, String> {
    let dir = home().join(REMOTE_ROOT).join("keys");
    fs::create_dir_all(&dir).map_err(|e| format!("{e}"))?;
    // Keys of machines from an earlier run must not answer for this one.
    if let Ok(rd) = fs::read_dir(&dir) {
        for e in rd.flatten() {
            if e.file_name().to_string_lossy().starts_with("id-") {
                let _ = fs::remove_file(e.path());
            }
        }
    }
    let mut copies = vec![(key.to_path_buf(), dir.join("id"))];
    let mut users = String::new();
    for h in hosts.iter().filter(|h| !h.local) {
        if h.key != key {
            copies.push((h.key.clone(), dir.join(format!("id-{}", h.addr))));
        }
        users.push_str(&format!("{} {}\n", h.addr, h.user));
    }
    for (src, dst) in &copies {
        fs::copy(src, dst).map_err(|e| format!("{}: {e}", src.display()))?;
        #[cfg(unix)]
        {
            use std::os::unix::fs::PermissionsExt;
            let _ = fs::set_permissions(dst, fs::Permissions::from_mode(0o600));
        }
    }
    fs::write(dir.join("users"), users).map_err(|e| format!("users: {e}"))?;
    let kh = home().join(".ssh/known_hosts");
    if kh.is_file() {
        let _ = fs::copy(&kh, dir.join("known_hosts"));
    }
    Ok(dir)
}

fn run(o: RunOpts) -> ExitCode {
    let case = &o.case;
    let name = case.file_name().map(|s| s.to_string_lossy().to_string())
        .unwrap_or_else(|| "case".into());
    let mut log = match fs::OpenOptions::new().create(true).append(true)
        .open(case.join("log.foamdist")) {
        Ok(f) => f,
        Err(e) => {
            eprintln!("{}: {e}", case.display());
            return ExitCode::from(2);
        }
    };
    let mut st = Status::new(case);
    let total: usize = o.hosts.iter().map(|h| h.slots).sum();
    st.set("phase", "start");
    st.set_raw("started", now().to_string());
    st.set_raw("ranks", total.to_string());
    st.set("solver", &o.solver);
    st.set_raw("hosts", format!("[{}]", o.hosts.iter().map(|h| format!(
        "{{\"host\": {}, \"slots\": {}, \"first_rank\": {}, \"head\": {}}}",
        json_str(&h.addr), h.slots, h.first_rank, h.local)).collect::<Vec<_>>().join(", ")));
    say(&mut log, &format!("foamdist: {} ranks on {} machines, solver {}", total,
                           o.hosts.len(), o.solver));

    let fail = |st: &mut Status, log: &mut fs::File, what: &str| -> ExitCode {
        say(log, &format!("FAILED: {what}"));
        st.set("error", what);
        st.set("phase", "failed");
        st.set_raw("finished", now().to_string());
        ExitCode::from(1)
    };

    // 1. hostfile, agent, keys
    let hostfile: String = o.hosts.iter().map(|h| format!("{} slots={}\n", h.addr, h.slots)).collect();
    if let Err(e) = fs::write(case.join("hosts.foamdist"), &hostfile) {
        return fail(&mut st, &mut log, &format!("hostfile: {e}"));
    }
    if let Err(e) = write_agent(case, &o.image, &o.user) {
        return fail(&mut st, &mut log, &format!("agent: {e}"));
    }
    let keys = match prepare_keys(&o.key, &o.hosts) {
        Ok(k) => k,
        Err(e) => return fail(&mut st, &mut log, &format!("keys: {e}")),
    };
    // «Стоп» у програмі не бачить процесів у контейнерах (там інша тека,
    // а частина — взагалі на інших машинах). Поки йде прогін, у кейсі лежить
    // скрипт, що прибирає їх усі; програма викликає його на зупинці.
    let clean_sh = case.join("foamdist-clean.sh");
    {
        let exe = env::current_exe().map(|p| p.display().to_string())
            .unwrap_or_else(|_| "foamdist".into());
        let spec: Vec<String> = o.hosts.iter()
            .map(|h| format!("{}@{}:{}", h.user, h.addr, h.slots)).collect();
        let own: Vec<String> = o.hosts.iter().filter(|h| h.key != o.key)
            .map(|h| format!("{}={}", h.addr, h.key.display())).collect();
        let keys_arg = if own.is_empty() { String::new() }
                       else { format!(" --keys {}", sh_quote(&own.join(","))) };
        let text = format!(
            "#!/bin/bash\n# Written by foamdist for the time of a run: stops it everywhere.\n\
             exec {} clean --case {} --hosts {} --key {} --user {}{} --engine {}\n",
            sh_quote(&exe), sh_quote(&name), sh_quote(&spec.join(",")),
            sh_quote(&o.key.display().to_string()), sh_quote(&o.user), keys_arg,
            o.engine.exe);
        if fs::write(&clean_sh, text).is_ok() {
            #[cfg(unix)]
            {
                use std::os::unix::fs::PermissionsExt;
                let _ = fs::set_permissions(&clean_sh, fs::Permissions::from_mode(0o755));
            }
        }
    }
    struct Remove(PathBuf);
    impl Drop for Remove {
        fn drop(&mut self) {
            let _ = fs::remove_file(&self.0);
        }
    }
    let _remove_clean = Remove(clean_sh);

    // 2. decompose — pieces in proportion to each process's speed
    if o.decompose {
        st.set("phase", "decompose");
        let plans = bench::plan(&o.hosts, false);
        let w = bench::weights(&plans);
        for p in &plans {
            say(&mut log, &format!("  {} × {}: {:.0} cells/s per process ({}), free {:.0}%",
                                   p.host.addr, p.host.slots,
                                   p.entry.as_ref().map(|e| e.speed()).unwrap_or(0.0),
                                   p.source, p.factor * 100.0));
        }
        st.set_raw("plan", bench::plan_json(&plans, &w));
        if let Err(e) = fs::write(case.join("system/decomposeParDict"), bench::decompose_dict(&w)) {
            return fail(&mut st, &mut log, &format!("decomposeParDict: {e}"));
        }
        if let Err(e) = set_subdomains(case, total) {
            return fail(&mut st, &mut log, &e);
        }
        let t0 = Instant::now();
        let c = o.engine.run(&o.image, case, None, &[],
                             "rm -rf processor*; decomposePar -force > log.decomposePar 2>&1");
        if let Err(e) = run_ok(c) {
            return fail(&mut st, &mut log, &format!("decomposePar: {e} (log.decomposePar)"));
        }
        say(&mut log, &format!("decomposePar: {:.1} s", t0.elapsed().as_secs_f64()));
    }
    let have = processor_dirs(case);
    if have != total {
        return fail(&mut st, &mut log, &format!(
            "{have} processor dirs, but {total} ranks: decompose for {total}"));
    }

    // 3. distribute: each remote machine gets the case skeleton and ONLY its
    //    pieces (OpenMPI fills the hosts in hostfile order: --map-by slot).
    st.set("phase", "distribute");
    let t0 = Instant::now();
    let jobs: Vec<_> = o.hosts.iter().filter(|h| !h.local).cloned().map(|h| {
        let (key, user, case, name) = (h.key.clone(), h.user.clone(), case.clone(), name.clone());
        thread::spawn(move || -> Result<(), String> {
            let remote = format!("{REMOTE_ROOT}/{name}");
            run_ok(ssh_cmd(&key, &user, &h.addr, &format!(
                "mkdir -p {r} && rm -rf {r}/processor*", r = sh_quote(&remote))))?;
            let mut src: Vec<String> = vec!["system".into(), "constant".into(),
                                            "foamdist-agent.sh".into()];
            for r in h.first_rank..h.first_rank + h.slots {
                src.push(format!("processor{r}"));
            }
            let mut c = Command::new("rsync");
            c.current_dir(&case).arg("-a")
                .arg("-e").arg(format!("ssh -i {} -o BatchMode=yes -o LogLevel=ERROR",
                                       key.display()))
                .args(&src)
                .arg(format!("{user}@{}:{remote}/", h.addr));
            run_ok(c).map(|_| ())
        })
    }).collect();
    for j in jobs {
        match j.join() {
            Ok(Ok(())) => {}
            Ok(Err(e)) => return fail(&mut st, &mut log, &format!("distribute: {e}")),
            Err(_) => return fail(&mut st, &mut log, "distribute: thread panicked"),
        }
    }
    say(&mut log, &format!("distribute: {:.1} s", t0.elapsed().as_secs_f64()));

    // 4. solve
    st.set("phase", "solve");
    let net = if o.net.is_empty() { mpi_nets(&o.hosts) } else { o.net.clone() };
    if net.is_empty() {
        return fail(&mut st, &mut log, "no IPv4 network for MPI: give --net CIDR");
    }
    say(&mut log, &format!("MPI network: {net}"));
    st.set("net", &net);
    let t0 = Instant::now();
    let mpirun = format!(
        "mpirun --hostfile hosts.foamdist -np {total} -x FOAMDIST_CASE \
         --map-by slot --bind-to core \
         --mca plm_rsh_agent /case/foamdist-agent.sh \
         --mca routed direct --mca plm_rsh_no_tree_spawn 1 \
         --mca btl tcp,self,vader \
         --mca btl_tcp_if_include {net} \
         --mca btl_tcp_port_min_v4 10000 --mca btl_tcp_port_range_v4 2000 \
         --mca oob_tcp_if_include {net} \
         {solver} -parallel -case /case{redirect}",
        solver = o.solver,
        // --stream: the solver output goes to OUR stdout. OpenFOAM Studio
        // reads the residuals from there, exactly as from a normal run.
        redirect = if o.stream { " 2>&1".to_string() } else { format!(" > log.{} 2>&1", o.solver) });
    let mut c = o.engine.run(&o.image, case, Some(&keys), &[("FOAMDIST_CASE", &name)], &mpirun);
    let solved = if o.stream {
        let _ = std::io::stdout().flush();
        match c.stdin(Stdio::null()).status() {
            Ok(s) if s.success() => Ok(String::new()),
            Ok(s) => Err(format!("exit {}", s.code().unwrap_or(-1))),
            Err(e) => Err(format!("{e}")),
        }
    } else {
        run_ok(c)
    };
    let secs = t0.elapsed().as_secs_f64();
    st.set_raw("solve_s", format!("{secs:.1}"));
    say(&mut log, &format!("solve: {secs:.1} s, {}", if solved.is_ok() { "ok" } else { "FAILED" }));

    // 5. gather the pieces back (also after a failure: the log of each rank
    //    and what was written are needed to see why).
    st.set("phase", "gather");
    let t0 = Instant::now();
    let jobs: Vec<_> = o.hosts.iter().filter(|h| !h.local).cloned().map(|h| {
        let (key, user, case, name) = (h.key.clone(), h.user.clone(), case.clone(), name.clone());
        thread::spawn(move || -> Result<(), String> {
            let remote = format!("{REMOTE_ROOT}/{name}");
            let mut c = Command::new("rsync");
            c.current_dir(&case).arg("-a")
                .arg("-e").arg(format!("ssh -i {} -o BatchMode=yes -o LogLevel=ERROR",
                                       key.display()));
            for r in h.first_rank..h.first_rank + h.slots {
                c.arg(format!("{user}@{}:{remote}/processor{r}", h.addr));
            }
            c.arg("./");
            run_ok(c).map(|_| ())
        })
    }).collect();
    let mut gather_err = None;
    for j in jobs {
        if let Ok(Err(e)) = j.join() {
            gather_err = Some(e);
        }
    }
    say(&mut log, &format!("gather: {:.1} s", t0.elapsed().as_secs_f64()));

    // 6. clean up containers everywhere; remote copies unless --keep.
    st.set("phase", "cleanup");
    cleanup(&o.hosts, &o.engine, &name, !o.keep);

    if let Err(e) = solved {
        return fail(&mut st, &mut log, &format!("solver: {e} (see log.{})", o.solver));
    }
    if let Some(e) = gather_err {
        return fail(&mut st, &mut log, &format!("gather: {e}"));
    }
    if o.reconstruct {
        st.set("phase", "reconstruct");
        let c = o.engine.run(&o.image, case, None, &[],
                             "reconstructPar -latestTime > log.reconstructPar 2>&1");
        if let Err(e) = run_ok(c) {
            return fail(&mut st, &mut log, &format!("reconstructPar: {e}"));
        }
    }
    st.set("phase", "done");
    st.set_raw("finished", now().to_string());
    say(&mut log, "foamdist: done");
    ExitCode::SUCCESS
}

fn cleanup(hosts: &[Host], engine: &Engine, name: &str, remove: bool) {
    let rm = format!("podman ps -aq --filter label={LABEL} | xargs -r podman rm -f -t 0 >/dev/null 2>&1; true");
    let _ = Command::new("bash").arg("-c")
        .arg(format!("{} ps -aq --filter label={LABEL} | xargs -r {} rm -f >/dev/null 2>&1; true",
                     engine.exe, engine.exe))
        .status();
    let jobs: Vec<_> = hosts.iter().filter(|h| !h.local).cloned().map(|h| {
        let (key, user) = (h.key.clone(), h.user.clone());
        let mut cmd = rm.clone();
        if remove {
            cmd.push_str(&format!("; rm -rf {}", sh_quote(&format!("{REMOTE_ROOT}/{name}"))));
        }
        thread::spawn(move || { let _ = run_ok(ssh_cmd(&key, &user, &h.addr, &cmd)); })
    }).collect();
    for j in jobs {
        let _ = j.join();
    }
}

// ---------------------------------------------------------------------------
// main
// ---------------------------------------------------------------------------

const USAGE: &str = "foamdist — EXPERIMENTAL: one OpenFOAM run on several machines

  foamdist probe [USER@]HOST... [--user U] [--key K] [--keys HOST=KEY,...]
  foamdist run --case DIR --hosts [USER@]IP:SLOTS,... --solver NAME
               [--user USER] [--key ~/.ssh/foamdist_ed25519]
               [--keys IP=KEY,...] [--net CIDR,...]
               [--image IMAGE] [--engine podman|docker]
               [--no-decompose] [--reconstruct] [--keep] [--stream]
  foamdist weights --hosts [USER@]IP:SLOTS,... [--keys ...] [--rebench]
               [--write-dict FILE]
               speed of each machine (benchmark, cached in
               ~/.cache/foamdist/bench.tsv) and the processorWeights
  foamdist clean --case NAME --hosts [USER@]IP:SLOTS,... [--user U] [--key K]
               [--keys IP=KEY,...]

The head machine (this one) must be in --hosts; it gets rank 0.
USER@ and --keys give a machine its own user and key; the others use
--user and --key. --net: the networks for MPI traffic; by default the /24
of the machines' addresses.";

fn main() -> ExitCode {
    let args: Vec<String> = env::args().skip(1).collect();
    if args.is_empty() || args[0] == "-h" || args[0] == "--help" {
        println!("{USAGE}");
        return ExitCode::SUCCESS;
    }
    let mut opt: BTreeMap<String, String> = BTreeMap::new();
    let mut flags: Vec<String> = Vec::new();
    let mut free: Vec<String> = Vec::new();
    let mut i = 1;
    while i < args.len() {
        let a = &args[i];
        if let Some(k) = a.strip_prefix("--") {
            if ["no-decompose", "reconstruct", "keep", "stream", "rebench"].contains(&k) {
                flags.push(k.into());
            } else if i + 1 < args.len() {
                opt.insert(k.into(), args[i + 1].clone());
                i += 1;
            } else {
                eprintln!("{a}: needs a value");
                return ExitCode::from(2);
            }
        } else {
            free.push(a.clone());
        }
        i += 1;
    }
    let user = opt.get("user").cloned().unwrap_or_else(|| "".into());
    let key = PathBuf::from(opt.get("key").cloned().unwrap_or_else(|| {
        home().join(".ssh/foamdist_ed25519").display().to_string()
    }));
    let image = opt.get("image").cloned().unwrap_or_else(|| IMAGE.into());
    let keys = match parse_keys(opt.get("keys").map(String::as_str).unwrap_or("")) {
        Ok(k) => k,
        Err(e) => {
            eprintln!("--keys: {e}");
            return ExitCode::from(2);
        }
    };
    match args[0].as_str() {
        "probe" => probe(&free, &user, &key, &keys, &image),
        "weights" => {
            let hosts = match parse_hosts(opt.get("hosts").map(String::as_str).unwrap_or(""),
                                          &user, &key, &keys) {
                Ok(h) => h,
                Err(e) => {
                    eprintln!("--hosts: {e}");
                    return ExitCode::from(2);
                }
            };
            let plans = bench::plan(&hosts, flags.iter().any(|f| f == "rebench"));
            let w = bench::weights(&plans);
            if let Some(p) = opt.get("write-dict") {
                if let Err(e) = fs::write(p, bench::decompose_dict(&w)) {
                    eprintln!("{p}: {e}");
                    return ExitCode::from(1);
                }
            }
            println!("{}", bench::plan_json(&plans, &w));
            ExitCode::SUCCESS
        }
        "run" | "clean" => {
            let hosts = match parse_hosts(opt.get("hosts").map(String::as_str).unwrap_or(""),
                                          &user, &key, &keys) {
                Ok(h) => h,
                Err(e) => {
                    eprintln!("--hosts: {e}");
                    return ExitCode::from(2);
                }
            };
            let engine = Engine::detect(opt.get("engine").map(String::as_str).unwrap_or(""));
            if args[0] == "clean" {
                let name = opt.get("case").cloned().unwrap_or_default();
                let name = Path::new(&name).file_name()
                    .map(|s| s.to_string_lossy().to_string()).unwrap_or_default();
                if name.is_empty() {
                    eprintln!("--case: needed");
                    return ExitCode::from(2);
                }
                cleanup(&hosts, &engine, &name, true);
                return ExitCode::SUCCESS;
            }
            let case = match opt.get("case").map(|c| fs::canonicalize(c)) {
                Some(Ok(p)) => p,
                _ => {
                    eprintln!("--case: no such directory");
                    return ExitCode::from(2);
                }
            };
            let Some(solver) = opt.get("solver").cloned() else {
                eprintln!("--solver: needed");
                return ExitCode::from(2);
            };
            run(RunOpts {
                case, hosts, solver, image, user, key, engine,
                keep: flags.iter().any(|f| f == "keep"),
                decompose: !flags.iter().any(|f| f == "no-decompose"),
                reconstruct: flags.iter().any(|f| f == "reconstruct"),
                stream: flags.iter().any(|f| f == "stream"),
                net: opt.get("net").cloned().unwrap_or_default(),
            })
        }
        other => {
            eprintln!("unknown command {other}\n\n{USAGE}");
            ExitCode::from(2)
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn quoting() {
        assert_eq!(sh_quote("a b"), "'a b'");
        assert_eq!(sh_quote("it's"), "'it'\\''s'");
        assert_eq!(json_str("a\"b\\c\nd"), "\"a\\\"b\\\\c\\nd\"");
    }

    #[test]
    fn agent_has_no_placeholders() {
        let t = AGENT.replace("%IMAGE%", IMAGE).replace("%USER%", "user1")
            .replace("%BASHRC%", BASHRC).replace("%HWLOC%", HWLOC)
            .replace("%LABEL%", LABEL).replace("%ROOT%", REMOTE_ROOT);
        assert!(!t.contains('%') || t.contains("printf %q"), "{t}");
        assert!(t.contains("--daemonize"));
        assert!(!t.contains("--shm-size"));
    }

    fn ph(spec: &str) -> Result<Vec<Host>, String> {
        parse_hosts(spec, "u", Path::new("/k"), &BTreeMap::new())
    }

    #[test]
    fn hosts_need_slots() {
        assert!(ph("10.0.0.1").is_err());
        assert!(ph("10.0.0.1:x").is_err());
        assert!(ph("10.0.0.1:0").is_err());
    }

    #[test]
    fn head_goes_first_and_ranks_follow() {
        let h = ph("10.9.9.9:4,localhost:2,10.9.9.8:3").unwrap();
        assert_eq!(h[0].addr, "localhost");
        assert_eq!((h[0].first_rank, h[1].first_rank, h[2].first_rank), (0, 2, 6));
    }

    #[test]
    fn head_must_be_listed() {
        assert!(ph("10.9.9.9:4").is_err());
    }

    #[test]
    fn each_machine_has_its_own_user_and_key() {
        let keys = parse_keys("10.9.9.8=/keys/other").unwrap();
        let h = parse_hosts("localhost:2,anna@10.9.9.9:4,10.9.9.8:3", "u",
                            Path::new("/k"), &keys).unwrap();
        let by = |a: &str| h.iter().find(|x| x.addr == a).unwrap().clone();
        assert_eq!((by("10.9.9.9").user.as_str(), by("10.9.9.9").key.as_path()),
                   ("anna", Path::new("/k")));
        assert_eq!((by("10.9.9.8").user.as_str(), by("10.9.9.8").key.as_path()),
                   ("u", Path::new("/keys/other")));
        assert!(parse_keys("nokey").is_err());
    }

    #[test]
    fn mpi_network_comes_from_the_addresses() {
        let h = ph("localhost:2,10.9.9.9:4,10.9.9.8:3,10.7.5.4:2").unwrap();
        let n = mpi_nets(&h);
        assert!(n.contains("10.9.9.0/24") && n.contains("10.7.5.0/24"), "{n}");
        assert!(!n.contains("127."), "{n}");
    }

    #[test]
    fn agent_picks_key_and_user_per_machine() {
        assert!(AGENT.contains("/keys/id-$host") && AGENT.contains("/keys/users"));
        assert!(AGENT.contains("\"$u@$host\""));
    }
}
