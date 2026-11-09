//! foamrs — fast OpenFOAM field reading for OpenFOAM Studio.
//!
//! The Python side (fs_fast.py) works without this module: it falls back to
//! VTK. This module only makes reading many time steps fast: files are
//! parsed in parallel on all cores, outside the Python GIL.

use numpy::{IntoPyArray, PyArrayMethods};
use pyo3::exceptions::PyValueError;
use pyo3::prelude::*;
use pyo3::types::PyDict;
use rayon::prelude::*;

pub mod field;
pub mod surface;

fn to_py<'py>(py: Python<'py>, v: field::Values) -> PyResult<Bound<'py, PyAny>> {
    let n = v.len();
    let ncomp = v.ncomp;
    let arr = v.data.into_pyarray(py);
    if ncomp == 1 {
        Ok(arr.into_any())
    } else {
        Ok(arr.reshape([n, ncomp])?.into_any())
    }
}

/// One file: internal values (optional) and the `value` of named patches.
struct Frame {
    internal: Option<field::Values>,
    patches: Vec<(String, Option<field::Values>)>,
}

fn read_one(
    path: &str,
    n_cells: usize,
    patches: &[(String, usize)],
    internal: bool,
) -> Result<Frame, String> {
    let buf = std::fs::read(path).map_err(|e| format!("{path}: {e}"))?;
    let inner = if internal {
        Some(field::internal_field(&buf, n_cells).map_err(|e| format!("{path}: {e}"))?)
    } else {
        None
    };
    let mut out = Vec::with_capacity(patches.len());
    for (name, n_faces) in patches {
        // A patch the file does not mention (or without `value`) is None:
        // the caller decides what to show there.
        let v = field::patch_value(&buf, name, *n_faces).unwrap_or(None);
        out.push((name.clone(), v));
    }
    Ok(Frame { internal: inner, patches: out })
}

/// Internal field of one file as a numpy array: (N,) or (N, ncomp).
#[pyfunction]
#[pyo3(signature = (path, n_cells=0))]
fn read_internal<'py>(py: Python<'py>, path: &str, n_cells: usize) -> PyResult<Bound<'py, PyAny>> {
    let p = path.to_string();
    let vals = py
        .detach(move || {
            let buf = std::fs::read(&p).map_err(|e| format!("{p}: {e}"))?;
            field::internal_field(&buf, n_cells).map_err(|e| format!("{p}: {e}"))
        })
        .map_err(PyValueError::new_err)?;
    to_py(py, vals)
}

/// Many files in parallel. Returns a list, one item per path:
/// (internal array or None, {patch: array or None}).
///
/// `patches` — [(name, n_faces)]; n_faces expands uniform values.
#[pyfunction]
#[pyo3(signature = (paths, n_cells=0, patches=Vec::new(), internal=true))]
fn read_fields<'py>(
    py: Python<'py>,
    paths: Vec<String>,
    n_cells: usize,
    patches: Vec<(String, usize)>,
    internal: bool,
) -> PyResult<Vec<(Option<Bound<'py, PyAny>>, Bound<'py, PyDict>)>> {
    let frames: Vec<Result<Frame, String>> = py.detach(|| {
        paths
            .par_iter()
            .map(|p| read_one(p, n_cells, &patches, internal))
            .collect()
    });
    let mut out = Vec::with_capacity(frames.len());
    for f in frames {
        let f = f.map_err(PyValueError::new_err)?;
        let inner = match f.internal {
            Some(v) => Some(to_py(py, v)?),
            None => None,
        };
        let d = PyDict::new(py);
        for (name, v) in f.patches {
            match v {
                Some(v) => d.set_item(name, to_py(py, v)?)?,
                None => d.set_item(name, py.None())?,
            }
        }
        out.push((inner, d));
    }
    Ok(out)
}

fn rows3(a: &numpy::PyReadonlyArray2<'_, f64>, what: &str) -> PyResult<Vec<[f64; 3]>> {
    let v = a.as_array();
    if v.ncols() != 3 {
        return Err(PyValueError::new_err(format!("{what}: need (N, 3)")));
    }
    Ok(v.rows().into_iter().map(|r| [r[0], r[1], r[2]]).collect())
}

/// Lines along a vertex vector field ON a triangle surface (oil flow).
///
/// points (N,3), tris (M,3) int, vectors (N,3), normals (N,3) — per vertex;
/// seeds — face indices. Returns (points (K,3), counts (L,)): line i is the
/// next counts[i] points. Lines with fewer than two points are dropped.
///
/// spacing > 0 — evenly spaced lines (traced one by one, in seed order);
/// 0 — every seed gives a line, traced in parallel. min_length drops short
/// lines (even spacing only).
#[pyfunction]
#[pyo3(signature = (points, tris, vectors, normals, seeds, max_length,
                    max_faces=20000, offset=0.0, floor=0.0, spacing=0.0,
                    min_length=0.0))]
#[allow(clippy::too_many_arguments)]
fn surface_lines<'py>(
    py: Python<'py>,
    points: numpy::PyReadonlyArray2<'py, f64>,
    tris: numpy::PyReadonlyArray2<'py, i64>,
    vectors: numpy::PyReadonlyArray2<'py, f64>,
    normals: numpy::PyReadonlyArray2<'py, f64>,
    seeds: numpy::PyReadonlyArray1<'py, i64>,
    max_length: f64,
    max_faces: usize,
    offset: f64,
    floor: f64,
    spacing: f64,
    min_length: f64,
) -> PyResult<(Bound<'py, PyAny>, Bound<'py, PyAny>)> {
    let pts = rows3(&points, "points")?;
    let vec = rows3(&vectors, "vectors")?;
    let nrm = rows3(&normals, "normals")?;
    if vec.len() != pts.len() || nrm.len() != pts.len() {
        return Err(PyValueError::new_err("vectors/normals: one row per point"));
    }
    let t = tris.as_array();
    if t.ncols() != 3 {
        return Err(PyValueError::new_err("tris: need (M, 3)"));
    }
    let n = pts.len() as i64;
    let mut tri = Vec::with_capacity(t.nrows());
    for r in t.rows() {
        if r.iter().any(|&i| i < 0 || i >= n) {
            return Err(PyValueError::new_err("tris: index out of range"));
        }
        tri.push([r[0] as usize, r[1] as usize, r[2] as usize]);
    }
    let nf = tri.len() as i64;
    let seeds: Vec<usize> = seeds
        .as_array()
        .iter()
        .filter(|&&s| s >= 0 && s < nf)
        .map(|&s| s as usize)
        .collect();
    let (flat, counts) = py.detach(move || {
        let s = surface::Surface::new(pts, tri, vec, nrm, floor);
        let lines: Vec<Vec<[f64; 3]>> = if spacing > 0.0 {
            s.even_lines(&seeds, spacing, min_length, max_length, max_faces, offset)
        } else {
            let never = |_: [f64; 3]| false;
            seeds
                .par_iter()
                .map(|&f| s.line(f, max_length, max_faces, offset, &never))
                .filter(|l| l.len() >= 2)
                .collect()
        };
        let total: usize = lines.iter().map(|l| l.len()).sum();
        let mut flat = Vec::with_capacity(total * 3);
        let mut counts = Vec::with_capacity(lines.len());
        for l in &lines {
            counts.push(l.len() as i64);
            for p in l {
                flat.extend_from_slice(p);
            }
        }
        (flat, counts)
    });
    let k = flat.len() / 3;
    let p = flat.into_pyarray(py).reshape([k, 3])?.into_any();
    let c = counts.into_pyarray(py).into_any();
    Ok((p, c))
}

/// Number of worker threads rayon uses.
#[pyfunction]
fn threads() -> usize {
    rayon::current_num_threads()
}

#[pymodule]
fn foamrs(m: &Bound<'_, PyModule>) -> PyResult<()> {
    m.add_function(wrap_pyfunction!(read_internal, m)?)?;
    m.add_function(wrap_pyfunction!(read_fields, m)?)?;
    m.add_function(wrap_pyfunction!(surface_lines, m)?)?;
    m.add_function(wrap_pyfunction!(threads, m)?)?;
    m.add("__version__", env!("CARGO_PKG_VERSION"))?;
    Ok(())
}
