//! Python module `meshfix`: repair(points, tris) for fs_meshfix.py.

use crate::{mesh_info, repair as do_repair, Mesh, MeshInfo, RepairOptions};
use numpy::{IntoPyArray, PyArrayMethods};
use pyo3::exceptions::PyValueError;
use pyo3::prelude::*;
use pyo3::types::{PyDict, PyList};

fn info_dict<'py>(py: Python<'py>, i: &MeshInfo) -> PyResult<Bound<'py, PyDict>> {
    let d = PyDict::new(py);
    d.set_item("vertices", i.vertices)?;
    d.set_item("edges", i.edges)?;
    d.set_item("faces", i.faces)?;
    d.set_item("boundary_edges", i.boundary_edges)?;
    d.set_item("non_manifold_edges", i.non_manifold_edges)?;
    d.set_item("flipped_edges", i.flipped_edges)?;
    d.set_item("shells", i.shells)?;
    Ok(d)
}

/// Repair a triangle mesh. points (N,3) float, tris (M,3) int.
/// Returns (points (K,3), tris (L,3), report dict). Units go in and come
/// back the same. The tolerances apply to mm (see stages.rs).
#[pyfunction]
#[pyo3(signature = (points, tris, normalize=true, wrap=true, stop_after=None))]
fn repair<'py>(
    py: Python<'py>,
    points: numpy::PyReadonlyArray2<'py, f64>,
    tris: numpy::PyReadonlyArray2<'py, i64>,
    normalize: bool,
    wrap: bool,
    stop_after: Option<&str>,
) -> PyResult<(Bound<'py, PyAny>, Bound<'py, PyAny>, Bound<'py, PyDict>)> {
    let stop = match stop_after {
        None => None,
        Some(name) => Some(
            crate::PIPELINE
                .iter()
                .copied()
                .find(|s| s.name() == name)
                .ok_or_else(|| PyValueError::new_err(format!("stop_after: no step {name}")))?,
        ),
    };
    let p = points.as_array();
    if p.ncols() != 3 {
        return Err(PyValueError::new_err("points: need (N, 3)"));
    }
    let t = tris.as_array();
    if t.ncols() != 3 {
        return Err(PyValueError::new_err("tris: need (M, 3)"));
    }
    let n = p.nrows() as i64;
    let mut tri = Vec::with_capacity(t.nrows());
    for r in t.rows() {
        if r.iter().any(|&i| i < 0 || i >= n) {
            return Err(PyValueError::new_err("tris: index out of range"));
        }
        tri.push([r[0] as u32, r[1] as u32, r[2] as u32]);
    }
    let verts: Vec<[f64; 3]> = p.rows().into_iter().map(|r| [r[0], r[1], r[2]]).collect();
    let opts = RepairOptions { normalize_scale: normalize, enable_wrap: wrap, stop_after: stop, ..Default::default() };
    let (mesh, report) = py.detach(move || {
        let mut m = Mesh::new(verts, tri);
        let r = do_repair(&mut m, &opts, |_, _| {});
        (m, r)
    });
    let report = report.map_err(|e| PyValueError::new_err(e.to_string()))?;

    let nv = mesh.vertices.len();
    let nf = mesh.triangles.len();
    let flat_p: Vec<f64> = mesh.vertices.iter().flat_map(|v| v.iter().copied()).collect();
    let flat_t: Vec<i64> = mesh.triangles.iter().flat_map(|t| t.iter().map(|&i| i as i64)).collect();
    let pts = flat_p.into_pyarray(py).reshape([nv, 3])?.into_any();
    let tr = flat_t.into_pyarray(py).reshape([nf, 3])?.into_any();

    let d = PyDict::new(py);
    d.set_item("before", info_dict(py, &report.before)?)?;
    d.set_item("after", info_dict(py, &report.after)?)?;
    d.set_item("scale", report.scale)?;
    let steps = PyList::empty(py);
    for s in &report.stages {
        steps.append((s.stage.name(), s.count, s.time.as_secs_f64()))?;
    }
    d.set_item("stages", steps)?;
    let u = PyDict::new(py);
    u.set_item("pairs", report.union.pairs)?;
    u.set_item("degenerate", report.union.degenerate)?;
    u.set_item("cut_faces", report.union.cut_faces)?;
    u.set_item("removed_patches", report.union.removed_patches)?;
    u.set_item("failed_faces", report.union.failed_faces)?;
    u.set_item("panicked", report.union.panicked)?;
    u.set_item("contact", report.union.contact)?;
    u.set_item("rolled_back", report.union.rolled_back)?;
    u.set_item("bad_edges", report.union.bad_edges)?;
    u.set_item("skipped_clusters", report.union.skipped_clusters)?;
    d.set_item("union", u)?;
    Ok((pts, tr, d))
}

/// Counts of a mesh without a repair: the same dict as report["after"].
#[pyfunction]
fn info<'py>(
    py: Python<'py>,
    points: numpy::PyReadonlyArray2<'py, f64>,
    tris: numpy::PyReadonlyArray2<'py, i64>,
) -> PyResult<Bound<'py, PyDict>> {
    let p = points.as_array();
    let t = tris.as_array();
    if p.ncols() != 3 || t.ncols() != 3 {
        return Err(PyValueError::new_err("need (N, 3) points and (M, 3) tris"));
    }
    let n = p.nrows() as i64;
    if t.iter().any(|&i| i < 0 || i >= n) {
        return Err(PyValueError::new_err("tris: index out of range"));
    }
    let m = Mesh::new(
        p.rows().into_iter().map(|r| [r[0], r[1], r[2]]).collect(),
        t.rows().into_iter().map(|r| [r[0] as u32, r[1] as u32, r[2] as u32]).collect(),
    );
    info_dict(py, &mesh_info(&m))
}

#[pymodule]
fn meshfix(m: &Bound<'_, PyModule>) -> PyResult<()> {
    m.add_function(wrap_pyfunction!(repair, m)?)?;
    m.add_function(wrap_pyfunction!(info, m)?)?;
    m.add("__version__", env!("CARGO_PKG_VERSION"))?;
    Ok(())
}
