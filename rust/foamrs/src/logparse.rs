//! Fast parse of an OpenFOAM run log (studio/run.log).
//!
//! This is the same parser as `RunState.feed` in fs_backend.py, but for a
//! block of many lines in one call. When the program connects to a long run
//! on the server, it reads the full log again (500 000 lines and more). The
//! Python loop needs about 1 s for this, the Rust parser about 0.05 s.
//!
//! The two parsers must give the same result. Each rule below is a copy of a
//! regular expression in fs_backend.py; the comment above each rule gives
//! the expression. Python `re` uses Unicode rules for `\d`, `\w` and `\s`.
//! This parser uses ASCII rules. Where a non-ASCII character can change the
//! result, the parser stops with `Bail`, and Python parses the block itself.
//!
//! Speed: a line can change the state only if it contains one of a few
//! literal strings (`Solving for `, `Time = ` at the line start, ...). We
//! find all of these strings in the full text first (memchr, in parallel)
//! and examine only those lines. The exception is the text of a fatal
//! error: while it is open, each line counts.

use memchr::memmem;
use memchr::{memchr, memrchr};
use pyo3::prelude::*;
use pyo3::types::{PyDict, PyList, PyString};
use rayon::prelude::*;

/// Lines of a fatal error text (FATAL_LINES in fs_backend.py).
const FATAL_LINES: usize = 6;

/// A line that only Python can parse exactly (Unicode digits and similar).
#[derive(Debug, PartialEq)]
pub struct Bail;

/// The state of `RunState` in fs_backend.py.
#[derive(Debug, Clone, Default, PartialEq)]
pub struct State {
    pub moment: f64,
    pub steps: i64,
    pub cells: i64,
    pub converged: bool,
    pub fatal: String,
    pub phase: String,
    pub mesh_ok: Option<bool>,
    pub mesh_failed_checks: i64,
    pub max_skewness: f64,
    pub n_skew_faces: i64,
    pub max_nonortho: f64,
    pub negative_volumes: bool,
    pub layer_coverage: f64,
    pub fatal_open: bool,
    pub fatal_parts: Vec<String>,
    /// Residual series in the order of their first point. A series that
    /// existed before this block starts with its old last point (that point
    /// can change: the worst of Ux/Uy/Uz at the same time).
    pub series: Vec<Series>,
}

#[derive(Debug, Clone, Default, PartialEq)]
pub struct Series {
    pub key: String,
    pub points: Vec<(f64, f64)>,
    /// True when this block added or changed a point.
    pub touched: bool,
}

// --- the literal strings -------------------------------------------------

const L_FATAL: usize = 0;
const L_TIME: usize = 1;
const L_RESID: usize = 2;
const L_CELLS: usize = 3;
const L_SNAPPY: usize = 4;
const L_FAILED: usize = 5;
const L_MESH_OK: usize = 6;
const L_SKEW: usize = 7;
const L_NONORTHO: usize = 8;
const L_STARS: usize = 9;
const L_ADDED: usize = 10;
const L_CONVERGED: usize = 11;

/// RE_SNAPPY_PHASE alternatives, in the order of the expression.
const SNAPPY: [&str; 6] = [
    "Refinement phase",
    "Morph phase",
    "Shrinking phase",
    "Layer addition phase",
    "Doing final balancing",
    "Finished meshing",
];

/// (literal, bit, only at the start of a line). A line without any of these
/// strings cannot change the state (except in an open fatal error).
const LITERALS: [(&str, usize, bool); 17] = [
    ("FOAM FATAL ", L_FATAL, false),
    ("Time = ", L_TIME, true),
    ("Solving for ", L_RESID, false),
    ("cells:", L_CELLS, false),
    (SNAPPY[0], L_SNAPPY, true),
    (SNAPPY[1], L_SNAPPY, true),
    (SNAPPY[2], L_SNAPPY, true),
    (SNAPPY[3], L_SNAPPY, true),
    (SNAPPY[4], L_SNAPPY, true),
    (SNAPPY[5], L_SNAPPY, true),
    ("Failed ", L_FAILED, false),
    ("Mesh OK", L_MESH_OK, true),
    ("Max skewness = ", L_SKEW, false),
    ("non-orthogonality Max: ", L_NONORTHO, false),
    ("***", L_STARS, false),
    ("Added ", L_ADDED, false),
    ("solution converged", L_CONVERGED, false),
];

// --- Python character rules ---------------------------------------------

/// `str.isspace()` and `\s` of Python. Rust `is_whitespace` does not include
/// U+001C..U+001F; Python does.
fn py_space(c: char) -> bool {
    c.is_whitespace() || ('\u{1c}'..='\u{1f}').contains(&c)
}

fn py_strip(s: &str) -> &str {
    s.trim_matches(py_space)
}

fn is_float_class(b: u8) -> bool {
    // [0-9.eE+-]
    b.is_ascii_digit() || matches!(b, b'.' | b'e' | b'E' | b'+' | b'-')
}

/// `float(s)` of Python. Ok(None) is ValueError.
fn py_float(s: &str) -> Result<Option<f64>, Bail> {
    if !s.is_ascii() || s.contains('_') {
        // Unicode digits and "1_000" are valid for Python: let it decide.
        return Err(Bail);
    }
    Ok(s.parse::<f64>().ok())
}

/// `int(s)` of Python for a run of ASCII digits.
fn py_int(s: &str) -> Result<i64, Bail> {
    s.parse::<i64>().map_err(|_| Bail)
}

/// Python `max(a, b)`: b only if b > a (NaN stays where it is).
fn py_max(a: f64, b: f64) -> f64 {
    if b > a {
        b
    } else {
        a
    }
}

/// A run of `\d+` at `at`: Ok(Some(end)) or Ok(None) when no digit is there.
/// A non-ASCII character in the run can be a Unicode digit: Bail.
fn digits(b: &[u8], at: usize) -> Result<Option<usize>, Bail> {
    let mut i = at;
    while i < b.len() && b[i].is_ascii_digit() {
        i += 1;
    }
    if i < b.len() && b[i] >= 0x80 {
        return Err(Bail);
    }
    Ok(if i > at { Some(i) } else { None })
}

/// End of `[0-9.eE+-]+` at `at`, or None.
fn float_class(b: &[u8], at: usize) -> Option<usize> {
    let mut i = at;
    while i < b.len() && is_float_class(b[i]) {
        i += 1;
    }
    if i > at {
        Some(i)
    } else {
        None
    }
}

/// End of `\s*` at `at` (Python rules).
fn spaces(s: &str, at: usize) -> usize {
    match s[at..].char_indices().find(|&(_, c)| !py_space(c)) {
        Some((k, _)) => at + k,
        None => s.len(),
    }
}

fn starts_at(b: &[u8], at: usize, lit: &str) -> bool {
    b.len() >= at + lit.len() && &b[at..at + lit.len()] == lit.as_bytes()
}

/// All positions of `lit` (ASCII) in the line `s`. A line is short: `str::find`
/// is faster here than a memchr searcher that is made for each call.
fn find_all<'a>(s: &'a str, lit: &'a str) -> impl Iterator<Item = usize> + 'a {
    let mut from = 0;
    std::iter::from_fn(move || {
        // `from` is one byte after an ASCII literal start: a char boundary.
        let k = s[from..].find(lit)?;
        let at = from + k;
        from = at + 1;
        Some(at)
    })
}

// --- the rules ------------------------------------------------------------

/// RE_RANK `^\[\d+\]\s*` then RE_ARROW `^-+>\s*` (both re.sub, once at the
/// start), then `.strip()`.
fn banner_text(line: &str) -> Result<&str, Bail> {
    let b = line.as_bytes();
    let mut s = line;
    if b.first() == Some(&b'[') {
        if let Some(end) = digits(b, 1)? {
            if b.get(end) == Some(&b']') {
                s = &line[spaces(line, end + 1)..];
            }
        }
    }
    let b = s.as_bytes();
    let mut i = 0;
    while i < b.len() && b[i] == b'-' {
        i += 1;
    }
    if i > 0 && b.get(i) == Some(&b'>') {
        s = &s[spaces(s, i + 1)..];
    }
    Ok(py_strip(s))
}

/// RE_FATAL_END on stripped text: `^\s*(From |in file |FOAM exiting|
/// Finalising|MPI_ABORT|mpirun|-{10,}|\*{5,})`.
fn fatal_end(text: &str) -> bool {
    const HEADS: [&str; 6] = [
        "From ",
        "in file ",
        "FOAM exiting",
        "Finalising",
        "MPI_ABORT",
        "mpirun",
    ];
    let t = &text[spaces(text, 0)..];
    if HEADS.iter().any(|h| t.starts_with(h)) {
        return true;
    }
    let b = t.as_bytes();
    let run = |c: u8| b.iter().take_while(|&&x| x == c).count();
    run(b'-') >= 10 || run(b'*') >= 5
}

/// `RunState._feed_fatal`.
fn feed_fatal(st: &mut State, line: &str) -> Result<bool, Bail> {
    let text = banner_text(line)?;
    if text.is_empty() {
        return Ok(false);
    }
    if fatal_end(text) {
        st.fatal_open = false;
        return Ok(false);
    }
    // Python: `if text not in parts: parts.append(text.rstrip("."))`.
    if !st.fatal_parts.iter().any(|p| p == text) {
        st.fatal_parts.push(text.trim_end_matches('.').to_string());
    }
    if st.fatal_parts.len() >= FATAL_LINES {
        st.fatal_open = false;
    }
    st.fatal = st.fatal_parts.join(". ");
    Ok(true)
}

/// RE_RESID: `Solving for (\w+), Initial residual = ([0-9.eE+-]+), Final
/// residual = ([0-9.eE+-]+), No Iterations (\d+)`, search.
/// Returns (field, initial residual text).
fn match_resid(line: &str) -> Result<Option<(&str, &str)>, Bail> {
    let b = line.as_bytes();
    for at in find_all(line, "Solving for ") {
        let f0 = at + "Solving for ".len();
        let mut i = f0;
        while i < b.len() && (b[i].is_ascii_alphanumeric() || b[i] == b'_') {
            i += 1;
        }
        if i < b.len() && b[i] >= 0x80 {
            return Err(Bail); // \w is Unicode in Python
        }
        if i == f0 || !starts_at(b, i, ", Initial residual = ") {
            continue;
        }
        let v0 = i + ", Initial residual = ".len();
        let Some(v1) = float_class(b, v0) else { continue };
        if !starts_at(b, v1, ", Final residual = ") {
            continue;
        }
        let w0 = v1 + ", Final residual = ".len();
        let Some(w1) = float_class(b, w0) else { continue };
        if !starts_at(b, w1, ", No Iterations ") {
            continue;
        }
        if digits(b, w1 + ", No Iterations ".len())?.is_none() {
            continue;
        }
        return Ok(Some((&line[f0..i], &line[v0..v1])));
    }
    Ok(None)
}

/// `Failed (\d+) mesh check`, search.
fn match_failed(line: &str) -> Result<Option<i64>, Bail> {
    let b = line.as_bytes();
    for at in find_all(line, "Failed ") {
        let d0 = at + "Failed ".len();
        if let Some(d1) = digits(b, d0)? {
            if starts_at(b, d1, " mesh check") {
                return Ok(Some(py_int(&line[d0..d1])?));
            }
        }
    }
    Ok(None)
}

/// `Max skewness = ([0-9.eE+-]+),\s*(\d+) highly skew`, search.
fn match_skew(line: &str) -> Result<Option<(&str, i64)>, Bail> {
    let b = line.as_bytes();
    for at in find_all(line, "Max skewness = ") {
        let v0 = at + "Max skewness = ".len();
        let Some(v1) = float_class(b, v0) else { continue };
        if b.get(v1) != Some(&b',') {
            continue;
        }
        let d0 = spaces(line, v1 + 1);
        if let Some(d1) = digits(b, d0)? {
            if starts_at(b, d1, " highly skew") {
                return Ok(Some((&line[v0..v1], py_int(&line[d0..d1])?)));
            }
        }
    }
    Ok(None)
}

/// `non-orthogonality Max: ([0-9.eE+-]+)`, search.
fn match_nonortho(line: &str) -> Option<&str> {
    let b = line.as_bytes();
    for at in find_all(line, "non-orthogonality Max: ") {
        let v0 = at + "non-orthogonality Max: ".len();
        if let Some(v1) = float_class(b, v0) {
            return Some(&line[v0..v1]);
        }
    }
    None
}

/// `Added (\d+) out of (\d+) cells`, search.
fn match_layers(line: &str) -> Result<Option<(i64, i64)>, Bail> {
    let b = line.as_bytes();
    for at in find_all(line, "Added ") {
        let a0 = at + "Added ".len();
        let Some(a1) = digits(b, a0)? else { continue };
        if !starts_at(b, a1, " out of ") {
            continue;
        }
        let t0 = a1 + " out of ".len();
        let Some(t1) = digits(b, t0)? else { continue };
        if starts_at(b, t1, " cells") {
            return Ok(Some((py_int(&line[a0..a1])?, py_int(&line[t0..t1])?)));
        }
    }
    Ok(None)
}

/// RE_CELLS `^\s*cells:\s+(\d+)`, match.
fn match_cells(line: &str) -> Result<Option<i64>, Bail> {
    let b = line.as_bytes();
    let c0 = spaces(line, 0);
    if !starts_at(b, c0, "cells:") {
        return Ok(None);
    }
    let s0 = c0 + "cells:".len();
    let d0 = spaces(line, s0);
    if d0 == s0 {
        return Ok(None);
    }
    match digits(b, d0)? {
        Some(d1) => Ok(Some(py_int(&line[d0..d1])?)),
        None => Ok(None),
    }
}

/// RE_NEGVOL with re.I: `(negative volume|Zero or negative cell volume|zero
/// or negative face area)`, search. Python re.I also folds some non-ASCII
/// letters to ASCII (KELVIN SIGN to k), so a non-ASCII line goes to Python.
fn match_negvol(line: &str) -> Result<bool, Bail> {
    if !line.is_ascii() {
        return Err(Bail);
    }
    let low = line.to_ascii_lowercase();
    Ok(["negative volume", "zero or negative cell volume", "zero or negative face area"]
        .iter()
        .any(|p| low.contains(p)))
}

fn add_residual(st: &mut State, field: &str, init: f64) {
    let key = if matches!(field, "Ux" | "Uy" | "Uz") { "U" } else { field };
    let moment = st.moment;
    let idx = match st.series.iter().position(|s| s.key == key) {
        Some(i) => i,
        None => {
            st.series.push(Series { key: key.to_string(), points: Vec::new(), touched: false });
            st.series.len() - 1
        }
    };
    let s = &mut st.series[idx];
    s.touched = true;
    match s.points.last_mut() {
        Some(last) if last.0 == moment => last.1 = py_max(last.1, init),
        _ => s.points.push((moment, init)),
    }
}

/// `RunState.feed` for a line that holds the literals in `mask`.
fn feed_line(st: &mut State, line: &str, mask: u32) -> Result<bool, Bail> {
    let has = |bit: usize| mask & (1 << bit) != 0;
    if st.fatal_open {
        return feed_fatal(st, line);
    }
    // RE_FAILED `FOAM FATAL (ERROR|IO ERROR)`, search.
    if has(L_FATAL) && (line.contains("FOAM FATAL ERROR") || line.contains("FOAM FATAL IO ERROR")) {
        st.fatal = banner_text(line)?.to_string();
        st.fatal_open = true;
        st.fatal_parts.clear();
        return Ok(true);
    }
    // RE_TIME `^Time = (\S+)`.
    if has(L_TIME) && line.starts_with("Time = ") {
        let v0 = "Time = ".len();
        let v1 = match line[v0..].char_indices().find(|&(_, c)| py_space(c)) {
            Some((k, _)) => v0 + k,
            None => line.len(),
        };
        if v1 > v0 {
            if let Some(t) = py_float(&line[v0..v1])? {
                st.moment = t;
                st.steps += 1;
            }
            return Ok(true);
        }
    }
    if has(L_RESID) {
        if let Some((field, init)) = match_resid(line)? {
            // A residual that is not a number: Python skips the line too.
            let Some(init) = py_float(init)? else { return Ok(false) };
            add_residual(st, field, init);
            return Ok(true);
        }
    }

    let mut changed = false;
    if has(L_CELLS) {
        if let Some(n) = match_cells(line)? {
            st.cells = n;
            changed = true;
        }
    }
    if has(L_SNAPPY) {
        if let Some(p) = SNAPPY.iter().find(|p| line.starts_with(*p)) {
            st.phase = p.to_string();
            changed = true;
        }
    }
    let failed = if has(L_FAILED) { match_failed(line)? } else { None };
    if let Some(n) = failed {
        st.mesh_failed_checks = n;
        st.mesh_ok = Some(false);
        changed = true;
    } else if has(L_MESH_OK) && line.starts_with("Mesh OK") {
        st.mesh_ok = Some(true);
        changed = true;
    }
    if has(L_SKEW) {
        if let Some((v, n)) = match_skew(line)? {
            if let Some(v) = py_float(v)? {
                st.max_skewness = v;
                st.n_skew_faces = n;
                changed = true;
            }
        }
    }
    if has(L_NONORTHO) {
        if let Some(v) = match_nonortho(line) {
            if let Some(v) = py_float(v)? {
                st.max_nonortho = py_max(st.max_nonortho, v);
                changed = true;
            }
        }
    }
    if has(L_STARS) && match_negvol(line)? {
        st.negative_volumes = true;
        changed = true;
    }
    if has(L_ADDED) {
        if let Some((added, total)) = match_layers(line)? {
            if added.unsigned_abs() > (1 << 53) || total.unsigned_abs() > (1 << 53) {
                return Err(Bail); // Python divides big ints exactly
            }
            st.layer_coverage = if total != 0 { added as f64 / total as f64 } else { 0.0 };
            changed = true;
        }
    }
    if has(L_CONVERGED)
        && (line.contains("SIMPLE solution converged") || line.contains("solution converged in"))
    {
        st.converged = true;
        changed = true;
    }
    Ok(changed)
}

/// Positions of all literals, sorted: (position << 5) | literal bit.
fn literal_hits(text: &str) -> Vec<u64> {
    let b = text.as_bytes();
    let scan = |&(lit, bit, at_start): &(&str, usize, bool)| -> Vec<u64> {
        memmem::find_iter(b, lit.as_bytes())
            .filter(|&p| !at_start || p == 0 || b[p - 1] == b'\n')
            .map(|p| ((p as u64) << 5) | bit as u64)
            .collect()
    };
    let parts: Vec<Vec<u64>> = if b.len() > (1 << 20) {
        LITERALS.par_iter().map(scan).collect()
    } else {
        LITERALS.iter().map(scan).collect()
    };
    let mut hits: Vec<u64> = Vec::with_capacity(parts.iter().map(|p| p.len()).sum());
    for p in parts {
        hits.extend(p);
    }
    hits.sort_unstable();
    hits
}

/// Feed all lines of `text` (lines joined by "\n", as `text.split("\n")`).
/// Returns True if a line changed the state (as `feed` per line). On `Bail`
/// the state is partly changed: the caller must throw it away.
pub fn feed_text(st: &mut State, text: &str) -> Result<bool, Bail> {
    let b = text.as_bytes();
    let len = b.len();
    let hits = literal_hits(text);
    let mut changed = false;
    let mut h = 0; // next hit
    let mut pos = 0; // start of the next line not yet examined
    loop {
        if st.fatal_open {
            // Each line counts until the fatal text closes.
            let end = memchr(b'\n', &b[pos..]).map_or(len, |k| pos + k);
            changed |= feed_fatal(st, &text[pos..end])?;
            if end == len {
                break;
            }
            pos = end + 1;
            continue;
        }
        while h < hits.len() && ((hits[h] >> 5) as usize) < pos {
            h += 1;
        }
        if h == hits.len() {
            break;
        }
        let at = (hits[h] >> 5) as usize;
        let start = memrchr(b'\n', &b[pos..at]).map_or(pos, |k| pos + k + 1);
        let end = memchr(b'\n', &b[at..]).map_or(len, |k| at + k);
        let mut mask = 0u32;
        // A literal has no "\n", so each hit before `end` is on this line.
        while h < hits.len() && ((hits[h] >> 5) as usize) < end {
            mask |= 1 << (hits[h] & 31);
            h += 1;
        }
        changed |= feed_line(st, &text[start..end], mask)?;
        if end == len {
            break;
        }
        pos = end + 1;
    }
    Ok(changed)
}

/// Reference: `feed` for each line, without the literal search. The unit
/// tests compare it with `feed_text`.
#[cfg(test)]
fn feed_lines_slow(st: &mut State, text: &str) -> Result<bool, Bail> {
    let mut changed = false;
    for line in text.split('\n') {
        changed |= feed_line(st, line, u32::MAX)?;
    }
    Ok(changed)
}

// --- Python interface -------------------------------------------------

fn get<'py>(d: &Bound<'py, PyDict>, key: &str) -> PyResult<Bound<'py, PyAny>> {
    d.get_item(key)?
        .ok_or_else(|| pyo3::exceptions::PyKeyError::new_err(key.to_string()))
}

fn state_from_py(d: &Bound<'_, PyDict>) -> PyResult<State> {
    let mut st = State {
        moment: get(d, "moment")?.extract()?,
        steps: get(d, "steps")?.extract()?,
        cells: get(d, "cells")?.extract()?,
        converged: get(d, "converged")?.extract()?,
        fatal: get(d, "fatal")?.extract()?,
        phase: get(d, "phase")?.extract()?,
        mesh_ok: get(d, "mesh_ok")?.extract()?,
        mesh_failed_checks: get(d, "mesh_failed_checks")?.extract()?,
        max_skewness: get(d, "max_skewness")?.extract()?,
        n_skew_faces: get(d, "n_skew_faces")?.extract()?,
        max_nonortho: get(d, "max_nonortho")?.extract()?,
        negative_volumes: get(d, "negative_volumes")?.extract()?,
        layer_coverage: get(d, "layer_coverage")?.extract()?,
        fatal_open: get(d, "fatal_open")?.extract()?,
        fatal_parts: get(d, "fatal_parts")?.extract()?,
        series: Vec::new(),
    };
    let last: Vec<(String, (f64, f64))> = get(d, "last")?.extract()?;
    for (key, p) in last {
        st.series.push(Series { key, points: vec![p], touched: false });
    }
    Ok(st)
}

fn state_to_py<'py>(py: Python<'py>, st: &State, changed: bool) -> PyResult<Bound<'py, PyDict>> {
    let d = PyDict::new(py);
    d.set_item("moment", st.moment)?;
    d.set_item("steps", st.steps)?;
    d.set_item("cells", st.cells)?;
    d.set_item("converged", st.converged)?;
    d.set_item("fatal", &st.fatal)?;
    d.set_item("phase", &st.phase)?;
    d.set_item("mesh_ok", st.mesh_ok)?;
    d.set_item("mesh_failed_checks", st.mesh_failed_checks)?;
    d.set_item("max_skewness", st.max_skewness)?;
    d.set_item("n_skew_faces", st.n_skew_faces)?;
    d.set_item("max_nonortho", st.max_nonortho)?;
    d.set_item("negative_volumes", st.negative_volumes)?;
    d.set_item("layer_coverage", st.layer_coverage)?;
    d.set_item("fatal_open", st.fatal_open)?;
    d.set_item("fatal_parts", st.fatal_parts.clone())?;
    let series = PyList::empty(py);
    for s in st.series.iter().filter(|s| s.touched) {
        series.append((&s.key, PyList::new(py, s.points.iter().copied())?))?;
    }
    d.set_item("series", series)?;
    d.set_item("changed", changed)?;
    Ok(d)
}

/// Parse a block of log lines (joined by "\n") from the state `state`
/// (the fields of RunState, plus `last`: [(key, (time, value))], the last
/// point of each residual series).
///
/// Returns the new state as a dict: the same fields, `series`:
/// [(key, points)] for each changed series (the first point replaces the old
/// last point when the series had one), and `changed`. Returns None when
/// only Python can parse the block exactly: then Python must parse it.
#[pyfunction]
pub fn parse_log<'py>(
    py: Python<'py>,
    text: &Bound<'py, PyString>,
    state: &Bound<'py, PyDict>,
) -> PyResult<Option<Bound<'py, PyDict>>> {
    let mut st = state_from_py(state)?;
    let text = text.to_cow()?;
    let r = py.detach(|| feed_text(&mut st, &text));
    match r {
        Ok(changed) => Ok(Some(state_to_py(py, &st, changed)?)),
        Err(Bail) => Ok(None),
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    const SAMPLE: &str = "\
Time = 1\n\
smoothSolver:  Solving for Ux, Initial residual = 0.5, Final residual = 1e-3, No Iterations 2\n\
smoothSolver:  Solving for Uy, Initial residual = 0.7, Final residual = 1e-3, No Iterations 2\n\
GAMG:  Solving for p, Initial residual = 1, Final residual = 0.01, No Iterations 5\n\
ExecutionTime = 3.71 s  ClockTime = 36 s\n\
Time = 2\n\
smoothSolver:  Solving for Ux, Initial residual = 0.25, Final residual = 1e-3, No Iterations 2\n\
    cells:            381097\n\
Refinement phase\n\
 ***Max skewness = 4.284898, 2 highly skew faces detected\n\
    Mesh non-orthogonality Max: 67.135483 average: 10.382677\n\
Failed 1 mesh checks.\n\
Added 900 out of 1000 cells (90%).\n\
 ***Zero or negative cell volume detected.\n\
SIMPLE solution converged in 3 iterations\n\
[1] --> FOAM FATAL IO ERROR: (openfoam-2512)\n\
[1] Cannot find patchField entry for body.\n\
[0] Cannot find patchField entry for body\n\
\n\
file: /case/0/U.boundaryField at line 30 to 46.\n\
    From void Foam::x() in file y.C at line 200.\n\
Solving for k, Initial residual = 0.1, Final residual = 1e-3, No Iterations 1";

    #[test]
    fn sample() {
        let mut st = State::default();
        assert_eq!(feed_text(&mut st, SAMPLE), Ok(true));
        assert_eq!(st.moment, 2.0);
        assert_eq!(st.steps, 2);
        let u = st.series.iter().find(|s| s.key == "U").unwrap();
        assert_eq!(u.points, vec![(1.0, 0.7), (2.0, 0.25)]);
        assert_eq!(st.cells, 381097);
        assert_eq!(st.phase, "Refinement phase");
        assert_eq!(st.max_skewness, 4.284898);
        assert_eq!(st.n_skew_faces, 2);
        assert_eq!(st.max_nonortho, 67.135483);
        assert_eq!(st.mesh_ok, Some(false));
        assert_eq!(st.mesh_failed_checks, 1);
        assert_eq!(st.layer_coverage, 0.9);
        assert!(st.negative_volumes);
        assert!(st.converged);
        assert_eq!(
            st.fatal,
            "Cannot find patchField entry for body. \
             file: /case/0/U.boundaryField at line 30 to 46"
        );
        assert!(!st.fatal_open);
        assert_eq!(st.series.iter().find(|s| s.key == "k").unwrap().points, vec![(2.0, 0.1)]);
    }

    #[test]
    fn same_as_each_line() {
        let mut a = State::default();
        let mut b = State::default();
        assert_eq!(feed_text(&mut a, SAMPLE), feed_lines_slow(&mut b, SAMPLE));
        assert_eq!(a, b);
    }

    #[test]
    fn unicode_goes_to_python() {
        let mut st = State::default();
        assert_eq!(feed_text(&mut st, "    cells: \u{0661}\u{0662}"), Err(Bail));
        assert_eq!(feed_text(&mut State::default(), "Time = 1_0"), Err(Bail));
        // Cyrillic without a literal is not a problem.
        assert_eq!(feed_text(&mut State::default(), "adaptErr: за порогом 1"), Ok(false));
    }

    #[test]
    fn open_fatal_crosses_blocks() {
        let mut st = State::default();
        feed_text(&mut st, "--> FOAM FATAL ERROR: (openfoam-2512)").unwrap();
        assert!(st.fatal_open);
        feed_text(&mut st, "Cannot find file \"points\"\nFOAM exiting\nTime = 5").unwrap();
        assert_eq!(st.fatal, "Cannot find file \"points\"");
        assert!(!st.fatal_open);
        assert_eq!(st.moment, 5.0);
    }
}

#[cfg(test)]
mod bench {
    use super::*;

    /// cargo test --release logparse::bench -- --ignored --nocapture
    /// with T20_LOG=<path of a long log>.
    #[test]
    #[ignore]
    fn long_log() {
        let Ok(p) = std::env::var("T20_LOG") else { return };
        let text = std::fs::read_to_string(p).unwrap();
        for _ in 0..3 {
            let t = std::time::Instant::now();
            let hits = literal_hits(&text);
            let t1 = t.elapsed();
            let mut st = State::default();
            feed_text(&mut st, &text).unwrap();
            println!("hits {} in {:?}, all {:?}", hits.len(), t1, t.elapsed() - t1);
        }
    }
}
