//! Machine speed for weighted decomposition.
//!
//! The pieces of the mesh used to be equal, so the whole run went at the
//! speed of the slowest process. Now each process gets a piece in proportion
//! to its speed: a fast machine takes more cells, a busy one fewer.
//!
//! Speed = cells per second per process, measured on THAT machine with THAT
//! many processes at once (so memory bandwidth shared by the cores is in the
//! number), in the same OpenFOAM container as the real run. The case is a
//! built-in 3-D lid-driven cavity (icoFoam, 60³ = 216 000 cells): no
//! tutorials are needed — the run image has none.
//!
//! The result is cached on the head machine per (host, processes, CPU
//! model); it is measured again when the CPU changed, after `MAX_AGE_DAYS`,
//! when the old number was taken under load, or on request.
//!
//! At run time the cached speed is scaled by how free the machine is NOW:
//! factor = (physical cores − load) / processes, kept in [0.15, 1].

use std::collections::BTreeMap;
use std::fs;
use std::path::{Path, PathBuf};
use std::process::{Command, Stdio};
use std::thread;
use std::time::{SystemTime, UNIX_EPOCH};

use crate::{home, json_str, ssh_cmd, Host, HWLOC, IMAGE, LABEL};

pub const CELLS: u64 = 60 * 60 * 60;
pub const STEPS: usize = 15;
pub const MAX_AGE_DAYS: f64 = 30.0;

#[derive(Clone, Debug, Default)]
pub struct Entry {
    pub host: String,
    pub slots: usize,
    pub cpu: String,
    pub cores: usize,
    pub s_per_step: f64,
    pub load: f64, // load average just before the benchmark
    pub time: u64,
}

impl Entry {
    /// Cells per second per process.
    pub fn speed(&self) -> f64 {
        if self.s_per_step <= 0.0 || self.slots == 0 {
            return 0.0;
        }
        CELLS as f64 / self.slots as f64 / self.s_per_step
    }
    fn line(&self) -> String {
        format!("{}\t{}\t{}\t{}\t{:.6}\t{:.2}\t{}", self.host, self.slots,
                self.cpu.replace('\t', " "), self.cores, self.s_per_step, self.load,
                self.time)
    }
    fn parse(line: &str) -> Option<Entry> {
        let f: Vec<&str> = line.split('\t').collect();
        if f.len() != 7 {
            return None;
        }
        Some(Entry {
            host: f[0].into(),
            slots: f[1].parse().ok()?,
            cpu: f[2].into(),
            cores: f[3].parse().ok()?,
            s_per_step: f[4].parse().ok()?,
            load: f[5].parse().ok()?,
            time: f[6].parse().ok()?,
        })
    }
    /// Was the machine busy while it was measured (a number to redo)?
    pub fn under_load(&self) -> bool {
        self.load > 0.5 + 0.25 * self.cores as f64
    }
}

pub fn cache_path() -> PathBuf {
    home().join(".cache/foamdist/bench.tsv")
}

pub fn load_cache(path: &Path) -> BTreeMap<(String, usize), Entry> {
    let mut out = BTreeMap::new();
    if let Ok(text) = fs::read_to_string(path) {
        for line in text.lines() {
            if let Some(e) = Entry::parse(line) {
                out.insert((e.host.clone(), e.slots), e);
            }
        }
    }
    out
}

pub fn save_cache(path: &Path, cache: &BTreeMap<(String, usize), Entry>) {
    if let Some(d) = path.parent() {
        let _ = fs::create_dir_all(d);
    }
    let text: String = cache.values().map(|e| e.line() + "\n").collect();
    let tmp = path.with_extension("tsv.part");
    if fs::write(&tmp, text).is_ok() {
        let _ = fs::rename(&tmp, path);
    }
}

fn now() -> u64 {
    SystemTime::now().duration_since(UNIX_EPOCH).map(|d| d.as_secs()).unwrap_or(0)
}

/// What the machine is: CPU model, physical cores, load now.
pub fn machine_script() -> String {
    "echo cpu=$(lscpu | sed -n 's/^Model name: *//p' | head -1); \
     echo cores=$(lscpu -p=core,socket 2>/dev/null | grep -v '^#' | sort -u | wc -l); \
     echo load=$(cut -d' ' -f1 /proc/loadavg)".to_string()
}

pub fn parse_kv(text: &str) -> BTreeMap<String, String> {
    text.lines()
        .filter_map(|l| l.split_once('='))
        .map(|(k, v)| (k.trim().to_string(), v.trim().to_string()))
        .collect()
}

/// The benchmark as one bash script (sent over ssh, or run here).
pub fn bench_script(slots: usize) -> String {
    let n = 60;
    let par = if slots > 1 {
        format!("decomposePar > log.decomposePar 2>&1 && \
                 mpirun -np {slots} --bind-to core icoFoam -parallel")
    } else {
        "icoFoam".to_string()
    };
    format!(r#"set -e
D=$HOME/foamdist/_bench_$$
rm -rf "$D"; mkdir -p "$D/system" "$D/constant" "$D/0"
trap 'rm -rf "$D"' EXIT
H='FoamFile {{ version 2.0; format ascii; class dictionary; object x; }}'
cat > "$D/system/blockMeshDict" <<EOF
$H
convertToMeters 1;
vertices ((0 0 0) (1 0 0) (1 1 0) (0 1 0) (0 0 1) (1 0 1) (1 1 1) (0 1 1));
blocks (hex (0 1 2 3 4 5 6 7) ({n} {n} {n}) simpleGrading (1 1 1));
boundary
(
    lid {{ type wall; faces ((4 5 6 7)); }}
    walls {{ type wall; faces ((0 3 2 1) (0 4 7 3) (1 2 6 5) (0 1 5 4) (3 7 6 2)); }}
);
EOF
cat > "$D/system/controlDict" <<EOF
$H
application icoFoam; startFrom startTime; startTime 0; stopAt endTime;
endTime {end}; deltaT 0.001; writeControl timeStep; writeInterval 100000;
purgeWrite 0; writeFormat binary; writePrecision 6; runTimeModifiable false;
EOF
cat > "$D/system/fvSchemes" <<EOF
$H
ddtSchemes {{ default Euler; }}
gradSchemes {{ default Gauss linear; }}
divSchemes {{ default none; div(phi,U) Gauss linear; }}
laplacianSchemes {{ default Gauss linear corrected; }}
interpolationSchemes {{ default linear; }}
snGradSchemes {{ default corrected; }}
EOF
cat > "$D/system/fvSolution" <<EOF
$H
solvers
{{
    p {{ solver PCG; preconditioner DIC; tolerance 1e-06; relTol 0.05; }}
    pFinal {{ \$p; relTol 0; }}
    U {{ solver smoothSolver; smoother symGaussSeidel; tolerance 1e-05; relTol 0; }}
}}
PISO {{ nCorrectors 2; nNonOrthogonalCorrectors 0; pRefCell 0; pRefValue 0; }}
EOF
cat > "$D/system/decomposeParDict" <<EOF
$H
numberOfSubdomains {slots}; method scotch;
EOF
cat > "$D/constant/transportProperties" <<EOF
$H
nu 0.01;
EOF
cat > "$D/0/U" <<EOF
FoamFile {{ version 2.0; format ascii; class volVectorField; object U; }}
dimensions [0 1 -1 0 0 0 0]; internalField uniform (0 0 0);
boundaryField {{ lid {{ type fixedValue; value uniform (1 0 0); }} walls {{ type noSlip; }} }}
EOF
cat > "$D/0/p" <<EOF
FoamFile {{ version 2.0; format ascii; class volScalarField; object p; }}
dimensions [0 2 -2 0 0 0 0]; internalField uniform 0;
boundaryField {{ lid {{ type zeroGradient; }} walls {{ type zeroGradient; }} }}
EOF
echo load=$(cut -d' ' -f1 /proc/loadavg)
if command -v podman >/dev/null 2>&1; then
    RUN="podman run --rm --userns=keep-id"
else
    RUN="docker run --rm --user $(id -u):$(id -g) -v /etc/passwd:/etc/passwd:ro -v /etc/group:/etc/group:ro"
fi
$RUN --network=host --ipc=host -e HOME=/tmp -e {hwloc} --label {label} \
    -v "$D:/case:Z" --entrypoint bash {image} -c \
    "source /usr/lib/openfoam/openfoam2512/etc/bashrc >/dev/null 2>&1; cd /case; \
     blockMesh > log.blockMesh 2>&1 && {par} > log.icoFoam 2>&1"
grep '^ExecutionTime' "$D/log.icoFoam" | awk '{{print "t=" $3}}'
"#, n = n, end = format!("{:.3}", STEPS as f64 * 0.001), slots = slots, par = par,
        hwloc = HWLOC, label = LABEL, image = IMAGE)
}

/// Seconds per step from the ExecutionTime lines (steps 4..end).
pub fn s_per_step(text: &str) -> Option<f64> {
    let t: Vec<f64> = text.lines()
        .filter_map(|l| l.strip_prefix("t="))
        .filter_map(|v| v.trim().parse().ok())
        .collect();
    if t.len() < 6 {
        return None;
    }
    let a = t[3];
    let b = *t.last()?;
    Some((b - a) / (t.len() - 4) as f64)
}

fn run_script(host: &Host, script: &str) -> Result<String, String> {
    let mut c = if host.local {
        let mut c = Command::new("bash");
        c.arg("-s");
        c
    } else {
        ssh_cmd(&host.key, &host.user, &host.addr, "bash -s")
    };
    use std::io::Write;
    let mut child = c.stdin(Stdio::piped()).stdout(Stdio::piped()).stderr(Stdio::piped())
        .spawn().map_err(|e| format!("{e}"))?;
    child.stdin.take().map(|mut s| s.write_all(script.as_bytes()));
    let out = child.wait_with_output().map_err(|e| format!("{e}"))?;
    let text = String::from_utf8_lossy(&out.stdout).to_string();
    if out.status.success() {
        Ok(text)
    } else {
        Err(format!("{}{}", text.trim(), String::from_utf8_lossy(&out.stderr).trim()))
    }
}

/// One machine: what it is now, its (cached or new) benchmark.
pub struct Plan {
    pub host: Host,
    pub cpu: String,
    pub cores: usize,
    pub load: f64,
    pub entry: Option<Entry>,
    pub source: String, // cached | measured | failed: …
    pub factor: f64,
}

/// Probe every machine, benchmark where needed (machines in parallel).
pub fn plan(hosts: &[Host], rebench: bool) -> Vec<Plan> {
    let path = cache_path();
    let cache = load_cache(&path);
    let jobs: Vec<_> = hosts.iter().cloned().map(|h| {
        let old = cache.get(&(h.addr.clone(), h.slots)).cloned();
        thread::spawn(move || {
            let info = run_script(&h, &machine_script())
                .map(|t| parse_kv(&t)).unwrap_or_default();
            let cpu = info.get("cpu").cloned().unwrap_or_default();
            let cores: usize = info.get("cores").and_then(|v| v.parse().ok()).unwrap_or(0);
            let load: f64 = info.get("load").and_then(|v| v.parse().ok()).unwrap_or(0.0);
            // A number taken under load is redone only once the machine is
            // free: re-measuring a machine that is still busy gives the same
            // low number and costs it another 20 s of work.
            let busy_now = load > 0.5 + 0.25 * cores as f64;
            let fresh = old.as_ref().is_some_and(|e| {
                !rebench && e.cpu == cpu && (!e.under_load() || busy_now)
                    && (now().saturating_sub(e.time) as f64) < MAX_AGE_DAYS * 86400.0
            });
            let (entry, source) = if fresh {
                (old, "cached".to_string())
            } else {
                match run_script(&h, &bench_script(h.slots)) {
                    Ok(text) => {
                        let kv = parse_kv(&text);
                        match s_per_step(&text) {
                            Some(s) if s > 0.0 => (Some(Entry {
                                host: h.addr.clone(), slots: h.slots, cpu: cpu.clone(),
                                cores, s_per_step: s,
                                load: kv.get("load").and_then(|v| v.parse().ok()).unwrap_or(load),
                                time: now(),
                            }), "measured".to_string()),
                            _ => (old, "failed: no timing in the benchmark".to_string()),
                        }
                    }
                    Err(e) => (old, format!("failed: {}", e.chars().take(200).collect::<String>())),
                }
            };
            // Free cores now (own foamdist runs are not running during a plan).
            let factor = if cores > 0 {
                ((cores as f64 - load) / h.slots as f64).clamp(0.15, 1.0)
            } else {
                1.0
            };
            Plan { host: h, cpu, cores, load, entry, source, factor }
        })
    }).collect();
    let plans: Vec<Plan> = jobs.into_iter().filter_map(|j| j.join().ok()).collect();
    let mut cache = load_cache(&path);
    for p in &plans {
        if p.source == "measured" {
            if let Some(e) = &p.entry {
                cache.insert((e.host.clone(), e.slots), e.clone());
            }
        }
    }
    save_cache(&path, &cache);
    plans
}

/// Integer weight per rank (scotch reads processorWeights as labels), in
/// rank order. A machine without a number gets the mean speed.
pub fn weights(plans: &[Plan]) -> Vec<u64> {
    let speeds: Vec<f64> = plans.iter()
        .filter_map(|p| p.entry.as_ref().map(|e| e.speed()))
        .filter(|s| *s > 0.0).collect();
    let mean = if speeds.is_empty() { 1.0 } else { speeds.iter().sum::<f64>() / speeds.len() as f64 };
    let per: Vec<f64> = plans.iter().map(|p| {
        let s = p.entry.as_ref().map(|e| e.speed()).filter(|s| *s > 0.0).unwrap_or(mean);
        s * p.factor
    }).collect();
    let max = per.iter().cloned().fold(0.0_f64, f64::max).max(1e-12);
    let mut out = Vec::new();
    for (p, w) in plans.iter().zip(&per) {
        let iw = ((w / max) * 1000.0).round().max(1.0) as u64;
        for _ in 0..p.host.slots {
            out.push(iw);
        }
    }
    out
}

pub fn plan_json(plans: &[Plan], w: &[u64]) -> String {
    let total: u64 = w.iter().sum::<u64>().max(1);
    let mut rows = Vec::new();
    let mut r = 0;
    for p in plans {
        let mine: u64 = w[r..r + p.host.slots].iter().sum();
        r += p.host.slots;
        let (speed, sps, age) = match &p.entry {
            Some(e) => (e.speed(), e.s_per_step,
                        (now().saturating_sub(e.time) as f64) / 86400.0),
            None => (0.0, 0.0, -1.0),
        };
        rows.push(format!(
            "{{\"host\": {}, \"slots\": {}, \"head\": {}, \"cpu\": {}, \"cores\": {}, \
             \"load\": {:.2}, \"factor\": {:.3}, \"speed\": {:.0}, \"s_per_step\": {:.4}, \
             \"bench\": {}, \"age_days\": {:.1}, \"share\": {:.4}}}",
            json_str(&p.host.addr), p.host.slots, p.host.local, json_str(&p.cpu), p.cores,
            p.load, p.factor, speed, sps, json_str(&p.source), age,
            mine as f64 / total as f64));
    }
    format!("{{\"hosts\": [{}], \"weights\": [{}]}}", rows.join(", "),
            w.iter().map(|x| x.to_string()).collect::<Vec<_>>().join(", "))
}

/// decomposeParDict with scotch and these weights (rank order).
pub fn decompose_dict(w: &[u64]) -> String {
    format!(
        "FoamFile\n{{\n    version     2.0;\n    format      ascii;\n    class       dictionary;\n    \
         location    \"system\";\n    object      decomposeParDict;\n}}\n\n\
         // Written by foamdist: pieces in proportion to each process's speed.\n\
         numberOfSubdomains {};\n\nmethod          scotch;\n\n\
         scotchCoeffs\n{{\n    processorWeights ({});\n}}\n",
        w.len(), w.iter().map(|x| x.to_string()).collect::<Vec<_>>().join(" "))
}

#[cfg(test)]
mod tests {
    use super::*;

    fn host(addr: &str, slots: usize) -> Host {
        Host { addr: addr.into(), slots, local: false, first_rank: 0,
               user: "u".into(), key: PathBuf::from("/k") }
    }

    fn plan_of(addr: &str, slots: usize, sps: f64, factor: f64) -> Plan {
        Plan {
            host: host(addr, slots), cpu: "x".into(), cores: 8, load: 0.0,
            entry: Some(Entry { host: addr.into(), slots, cpu: "x".into(), cores: 8,
                                s_per_step: sps, load: 0.0, time: now() }),
            source: "cached".into(), factor,
        }
    }

    #[test]
    fn cache_line_round_trip() {
        let e = Entry { host: "10.0.0.1".into(), slots: 4, cpu: "Intel i7".into(),
                        cores: 4, s_per_step: 0.25, load: 0.1, time: 123 };
        let back = Entry::parse(&e.line()).unwrap();
        assert_eq!((back.host, back.slots, back.cpu, back.cores, back.time),
                   (e.host, e.slots, e.cpu, e.cores, e.time));
        assert!((back.s_per_step - 0.25).abs() < 1e-9);
    }

    #[test]
    fn timing_from_execution_lines() {
        let text = "load=0.1\nt=1\nt=2\nt=3\nt=4\nt=6\nt=8\nt=10\n";
        // steps 4..7: (10 - 4) / 3
        assert!((s_per_step(text).unwrap() - 2.0).abs() < 1e-12);
        assert!(s_per_step("t=1\nt=2").is_none());
    }

    #[test]
    fn faster_process_gets_bigger_weight() {
        // Same processes, one machine twice as fast per step.
        let p = vec![plan_of("a", 2, 1.0, 1.0), plan_of("b", 2, 0.5, 1.0)];
        let w = weights(&p);
        assert_eq!(w.len(), 4);
        assert_eq!(w[2], 1000);
        assert_eq!(w[0], 500);
    }

    #[test]
    fn busy_machine_gets_less() {
        let p = vec![plan_of("a", 4, 1.0, 1.0), plan_of("b", 4, 1.0, 0.25)];
        let w = weights(&p);
        assert_eq!((w[0], w[4]), (1000, 250));
    }

    #[test]
    fn speed_is_per_process() {
        // 4 processes taking 1 s per step on 216 000 cells → 54 000 cells/s each.
        let e = Entry { slots: 4, s_per_step: 1.0, ..Default::default() };
        assert!((e.speed() - 54000.0).abs() < 1e-6);
    }

    #[test]
    fn dict_lists_every_rank() {
        let d = decompose_dict(&[1000, 1000, 500]);
        assert!(d.contains("numberOfSubdomains 3;"));
        assert!(d.contains("processorWeights (1000 1000 500);"));
    }

    #[test]
    fn script_has_no_stray_braces() {
        let s = bench_script(4);
        assert!(s.contains("(60 60 60)"));
        assert!(s.contains("mpirun -np 4"));
        assert!(s.contains("endTime 0.015"));
        assert!(!s.contains("{n}") && !s.contains("{slots}"));
    }
}
