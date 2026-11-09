//! Coded entries (coded function objects, codedFixedValue, #codeStream, #calc)
//! in a distributed run.
//!
//! OpenFOAM compiles them with wmake on first use. The run image has no
//! compiler: the solver logs «exec(wmake) failed», and the other ranks wait
//! forever for a library that never appears — on the other machines it would
//! not appear anyway, dynamicCode is not shared (2026-11-09: a 20-rank run of
//! the user hung like this on adaptErr, the mesh-adaptation indicator that
//! OpenFOAM Studio writes as a coded function object). A library built
//! natively on the head does not help either: Ubuntu 26.04 / glibc 2.43 there,
//! 2.39 in the image.
//!
//! So, before the pieces go out, a COMPILE PASS on the head: the solver runs
//! in the dev image (same OpenFOAM build as the run image, with a compiler)
//! on ALL pieces, for one step, in a shadow case:
//!
//!   .foamdist-compile/
//!     system/        a copy; controlDict: one step, no writes, only the
//!                    coded function objects
//!     constant, processor*, dynamicCode  → links to the real ones
//!
//! The libraries land in the real dynamicCode and go out with the pieces.
//! The real run then finds them (their names carry the SHA1 of the code)
//! and compiles nothing.
//!
//! Why not `-dry-run`: it swaps the mesh for a simplified column mesh and
//! fails on any point zone («Zone named frozenPoints not found. Available
//! zone names: 1(frozenPoints)», simplifiedMeshes::columnFvMeshInfo).

use std::fs;
use std::path::Path;

/// Does the case use anything OpenFOAM compiles at run time?
///
/// Only the dictionaries the solver reads count. Stored copies are not read:
/// a warm start keeps both stages side by side (controlDict.stage1,
/// controlDict.stage2) and copies one of them to controlDict before each
/// stage. 2026-11-09: the coded adaptErr in controlDict.stage2 started a
/// compile pass for stage 1, which has no coded entry, and the run stopped
/// with «compile pass built no library».
pub fn has_coded(case: &Path) -> bool {
    let mut files: Vec<std::path::PathBuf> = Vec::new();
    for dir in ["system", "constant"] {
        if let Ok(rd) = fs::read_dir(case.join(dir)) {
            files.extend(rd.flatten().map(|e| e.path())
                .filter(|p| p.is_file() && !is_stored_copy(p)));
        }
    }
    // Boundary conditions: the fields of the time the run starts from.
    let p0 = case.join("processor0");
    if let Some(t) = latest_time(&p0) {
        if let Ok(rd) = fs::read_dir(p0.join(&t)) {
            files.extend(rd.flatten().map(|e| e.path()).filter(|p| p.is_file()));
        }
    }
    files.iter().any(|p| {
        let Ok(bytes) = fs::read(p) else { return false };
        // Binary fields: only the ASCII header and dictionaries matter.
        let text = String::from_utf8_lossy(&bytes[..bytes.len().min(2_000_000)]);
        text_has_coded(&text)
    })
}

pub fn text_has_coded(text: &str) -> bool {
    text.lines().any(|l| {
        let t = l.trim_start();
        if t.starts_with("//") {
            return false;
        }
        t.contains("#codeStream") || t.contains("#calc")
            || (t.starts_with("type") && t.split_whitespace().nth(1)
                .is_some_and(|w| w.trim_end_matches(';').starts_with("coded")))
    })
}

/// A copy of a dictionary that OpenFOAM does not read: `<name>.stage<N>`,
/// `.orig`, `.bak…`, `.old`, `…~`.
pub fn is_stored_copy(p: &Path) -> bool {
    let name = p.file_name().map(|n| n.to_string_lossy().to_string()).unwrap_or_default();
    if name.ends_with('~') {
        return true;
    }
    let Some((_, ext)) = name.rsplit_once('.') else { return false };
    let stage = ext.strip_prefix("stage")
        .is_some_and(|n| !n.is_empty() && n.chars().all(|c| c.is_ascii_digit()));
    stage || ext == "orig" || ext == "old" || ext.starts_with("bak")
}

/// After a compile pass that built no library: did the solver end normally?
/// Then nothing coded was active (a coded entry that fails to build stops
/// the solver), and the run can go on. `out` is the output of the compile
/// script, `log` the solver log of the pass.
pub fn ended_clean(out: &str, log: &str) -> bool {
    out.lines().any(|l| l.trim() == "compile pass rc=0")
        && log.lines().any(|l| l.trim() == "End")
}

/// Largest numeric time directory in `dir` (as written), if any.
pub fn latest_time(dir: &Path) -> Option<String> {
    let mut best: Option<(f64, String)> = None;
    for e in fs::read_dir(dir).ok()?.flatten() {
        if !e.path().is_dir() {
            continue;
        }
        let name = e.file_name().to_string_lossy().to_string();
        if let Ok(v) = name.parse::<f64>() {
            if best.as_ref().map_or(true, |(b, _)| v > *b) {
                best = Some((v, name));
            }
        }
    }
    best.map(|(_, n)| n)
}

/// Build the shadow case. Returns its name (relative to the case).
pub fn make_shadow(case: &Path, ranks: usize) -> Result<String, String> {
    let name = ".foamdist-compile";
    let sh = case.join(name);
    if sh.exists() {
        fs::remove_dir_all(&sh).map_err(|e| format!("{}: {e}", sh.display()))?;
    }
    fs::create_dir_all(sh.join("system")).map_err(|e| format!("{e}"))?;
    fs::create_dir_all(case.join("dynamicCode")).map_err(|e| format!("{e}"))?;
    for e in fs::read_dir(case.join("system")).map_err(|e| format!("system: {e}"))?.flatten() {
        if e.path().is_file() {
            fs::copy(e.path(), sh.join("system").join(e.file_name()))
                .map_err(|x| format!("{}: {x}", e.path().display()))?;
        }
    }
    #[cfg(unix)]
    {
        use std::os::unix::fs::symlink;
        let mut links = vec!["constant".to_string(), "dynamicCode".to_string()];
        links.extend((0..ranks).map(|r| format!("processor{r}")));
        for l in links {
            symlink(format!("../{l}"), sh.join(&l)).map_err(|e| format!("{l}: {e}"))?;
        }
    }
    let _ = ranks;
    Ok(name.to_string())
}

/// Shell (inside the dev image, in the shadow case): one step, no writes,
/// only coded function objects; then the solver on all pieces.
pub fn compile_script(shadow: &str, solver: &str, ranks: usize, start: &str) -> String {
    format!(
        r#"cd /case/{shadow} || exit 1
c=system/controlDict
foamDictionary -entry startFrom -set startTime $c >/dev/null
foamDictionary -entry startTime -set {start} $c >/dev/null
foamDictionary -entry adjustTimeStep -set no $c >/dev/null
dt=$(foamDictionary -entry deltaT -value $c)
foamDictionary -entry endTime -set $(awk "BEGIN{{printf \"%.12g\", {start} + $dt}}") $c >/dev/null
foamDictionary -entry writeControl -set timeStep $c >/dev/null
foamDictionary -entry writeInterval -set 1000000000 $c >/dev/null
for f in $(foamDictionary -entry functions -keywords $c 2>/dev/null); do
    t=$(foamDictionary -entry functions/$f/type -value $c 2>/dev/null)
    case "$t" in coded*) ;; *) foamDictionary -entry functions/$f -remove $c >/dev/null 2>&1;; esac
done
mpirun -np {ranks} --oversubscribe --bind-to none {solver} -parallel > log.compile 2>&1
echo "compile pass rc=$?"
"#)
}

/// Libraries built so far (count of *.so under dynamicCode/platforms).
pub fn libs(case: &Path) -> usize {
    let mut n = 0;
    if let Ok(rd) = fs::read_dir(case.join("dynamicCode/platforms")) {
        for plat in rd.flatten() {
            if let Ok(libs) = fs::read_dir(plat.path().join("lib")) {
                n += libs.flatten().filter(|e| {
                    e.file_name().to_string_lossy().ends_with(".so")
                }).count();
            }
        }
    }
    n
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn finds_coded_entries() {
        assert!(text_has_coded("adaptErr\n{\n    type            coded;\n"));
        assert!(text_has_coded("    type codedFixedValue;"));
        assert!(text_has_coded("x #calc \"$a*2\";"));
        assert!(text_has_coded("code #codeStream { };"));
        assert!(!text_has_coded("    type            forces;"));
        assert!(!text_has_coded("// type coded;"));
        assert!(!text_has_coded("    type            fixedValue;"));
    }

    #[test]
    fn stage_copies_are_not_read() {
        for n in ["controlDict.stage1", "controlDict.stage2", "fvOptions.stage2",
                  "controlDict.orig", "controlDict.bak-1533", "controlDict.old",
                  "controlDict~"] {
            assert!(is_stored_copy(Path::new(n)), "{n}");
        }
        for n in ["controlDict", "fvOptions", "thermophysicalProperties.water",
                  "controlDict.stage", "decomposeParDict"] {
            assert!(!is_stored_copy(Path::new(n)), "{n}");
        }
    }

    #[test]
    fn warm_start_stage1_has_nothing_to_compile() {
        // case_jet_09.11_11.15: coded adaptErr only in controlDict.stage2.
        let d = std::env::temp_dir().join(format!("fd_hc_{}", std::process::id()));
        let _ = fs::remove_dir_all(&d);
        fs::create_dir_all(d.join("system")).unwrap();
        fs::create_dir_all(d.join("processor0/0")).unwrap();
        let plain = "functions\n{\n    yPlus\n    {\n        type yPlus;\n    }\n}\n";
        let coded = "functions\n{\n    adaptErr\n    {\n        type            coded;\n    }\n}\n";
        fs::write(d.join("system/controlDict"), plain).unwrap();
        fs::write(d.join("system/controlDict.stage1"), plain).unwrap();
        fs::write(d.join("system/controlDict.stage2"), coded).unwrap();
        fs::write(d.join("processor0/0/U"), "type fixedValue;").unwrap();
        assert!(!has_coded(&d), "stage 1: only the stored stage-2 copy is coded");
        fs::write(d.join("system/controlDict"), coded).unwrap();
        assert!(has_coded(&d), "stage 2: the active controlDict is coded");
        let _ = fs::remove_dir_all(&d);
    }

    #[test]
    fn clean_end_without_library_is_not_an_error() {
        let log = "ExecutionTime = 4.42 s  ClockTime = 9 s\n\nEnd\n\nFinalising parallel run\n";
        assert!(ended_clean("compile pass rc=0\nstartFrom startTime;\n", log));
        assert!(!ended_clean("compile pass rc=1\n", log));
        assert!(!ended_clean("compile pass rc=0\n", "exec(wmake) failed\n"));
    }

    #[test]
    fn script_sets_one_step_and_keeps_coded_only() {
        let s = compile_script(".foamdist-compile", "rhoPimpleFoam", 20, "0");
        assert!(s.contains("adjustTimeStep -set no"));
        assert!(s.contains("writeInterval -set 1000000000"));
        assert!(s.contains("coded*) ;;"));
        assert!(s.contains("mpirun -np 20 --oversubscribe --bind-to none rhoPimpleFoam -parallel"));
        assert!(s.contains("printf \\\"%.12g\\\", 0 + $dt"));
    }

    #[test]
    fn latest_time_is_numeric_max() {
        let d = std::env::temp_dir().join(format!("fd_lt_{}", std::process::id()));
        let _ = fs::remove_dir_all(&d);
        for t in ["0", "0.002", "0.0015", "constant"] {
            fs::create_dir_all(d.join(t)).unwrap();
        }
        assert_eq!(latest_time(&d).as_deref(), Some("0.002"));
        let _ = fs::remove_dir_all(&d);
    }
}
