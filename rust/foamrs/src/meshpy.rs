//! Python side of polymesh.rs: `foamrs.read_mesh(faces_dir, points_dir)`
//! gives a `PolyMesh`; its `volume` and `boundary` give numpy arrays for a
//! vtkUnstructuredGrid and for vtkPolyData of each patch (fs_rsmesh.py).

use std::path::{Path, PathBuf};

use numpy::{IntoPyArray, PyArrayMethods};
use pyo3::exceptions::PyValueError;
use pyo3::prelude::*;
use pyo3::types::{PyDict, PyList};

use crate::polymesh::{self, Mesh, F32};

fn err(e: polymesh::MeshError) -> PyErr {
    PyValueError::new_err(e.0)
}

/// Run ; a panic becomes a MeshError. PyO3 would make a panic a
/// PanicException, which `except Exception` in Python does not catch, and
/// the caller must be able to fall back to VTK.
fn guarded<T>(f: impl FnOnce() -> Result<T, polymesh::MeshError>) -> Result<T, polymesh::MeshError> {
    match std::panic::catch_unwind(std::panic::AssertUnwindSafe(f)) {
        Ok(r) => r,
        Err(p) => {
            let msg = p
                .downcast_ref::<&str>()
                .map(|s| s.to_string())
                .or_else(|| p.downcast_ref::<String>().cloned())
                .unwrap_or_else(|| "unknown".into());
            Err(polymesh::MeshError(format!("internal error: {msg}")))
        }
    }
}

fn arr<'py>(py: Python<'py>, v: F32) -> PyResult<Bound<'py, PyAny>> {
    let nc = v.ncomp.max(1);
    let n = v.data.len() / nc;
    let a = v.data.into_pyarray(py);
    if v.ncomp == 1 {
        Ok(a.into_any())
    } else {
        Ok(a.reshape([n, nc])?.into_any())
    }
}

fn points3<'py>(py: Python<'py>, p: Vec<f32>) -> PyResult<Bound<'py, PyAny>> {
    let n = p.len() / 3;
    Ok(p.into_pyarray(py).reshape([n, 3])?.into_any())
}

/// A polyMesh read once; volume and boundary of any time on it.
#[pyclass(frozen, module = "foamrs")]
pub struct PolyMesh {
    mesh: Mesh,
}

fn select(time_dir: &Path, names: &Option<Vec<String>>) -> Result<Vec<polymesh::FieldFile>, polymesh::MeshError> {
    let all = polymesh::list_fields(time_dir)?;
    Ok(match names {
        None => all,
        Some(want) => all.into_iter().filter(|f| want.iter().any(|w| *w == f.name)).collect(),
    })
}

#[pymethods]
impl PolyMesh {
    #[getter]
    fn n_cells(&self) -> usize {
        self.mesh.n_cells
    }

    #[getter]
    fn n_points(&self) -> usize {
        self.mesh.points.len()
    }

    #[getter]
    fn n_faces(&self) -> usize {
        self.mesh.n_faces()
    }

    /// [(name, type, startFace, nFaces)] in file order.
    fn patches(&self) -> Vec<(String, String, usize, usize)> {
        self.mesh
            .patches
            .iter()
            .map(|p| (p.name.clone(), p.kind_name.clone(), p.start, p.n_faces))
            .collect()
    }

    /// The field names of a time folder, as the VTK reader lists them.
    fn field_names(&self, time_dir: &str) -> PyResult<Vec<String>> {
        let d = PathBuf::from(time_dir);
        Ok(polymesh::list_fields(&d).map_err(err)?.into_iter().map(|f| f.name).collect())
    }

    /// The volume at `time_dir` with the fields `fields` (None: all).
    /// A dict: points (N,3) float32, types uint8, offsets/conn int64 (cell
    /// points), floc_offsets int64 (faces of each polyhedron),
    /// pface_offsets/pface_conn int64 (faces of the polyhedra), names, cell
    /// {name: array}, point {name: array}.
    #[pyo3(signature = (time_dir, fields=None))]
    fn volume<'py>(&self, py: Python<'py>, time_dir: &str, fields: Option<Vec<String>>) -> PyResult<Bound<'py, PyDict>> {
        let d = PathBuf::from(time_dir);
        let mesh = &self.mesh;
        let (cells, data, pts) = py
            .detach(move || guarded(|| {
                let ff = select(&d, &fields)?;
                let (cells, data) = mesh.volume(&ff)?;
                Ok((cells, data, mesh.points_f32()))
            }))
            .map_err(err)?;
        let out = PyDict::new(py);
        out.set_item("points", points3(py, pts)?)?;
        out.set_item("types", cells.types.into_pyarray(py))?;
        out.set_item("offsets", cells.offsets.into_pyarray(py))?;
        out.set_item("conn", cells.conn.into_pyarray(py))?;
        out.set_item("floc_offsets", cells.floc_offsets.into_pyarray(py))?;
        out.set_item("pface_offsets", cells.pface_offsets.into_pyarray(py))?;
        out.set_item("pface_conn", cells.pface_conn.into_pyarray(py))?;
        let names = PyList::empty(py);
        let cd = PyDict::new(py);
        let pd = PyDict::new(py);
        for (name, c, p) in data {
            names.append(&name)?;
            cd.set_item(&name, arr(py, c)?)?;
            pd.set_item(&name, arr(py, p)?)?;
        }
        out.set_item("names", names)?;
        out.set_item("cell", cd)?;
        out.set_item("point", pd)?;
        Ok(out)
    }

    /// The patches at `time_dir` with the fields `fields` (None: all), in
    /// file order: [dict(name, type, points (n,3) float32, face_offsets,
    /// face_conn int64, names, cell {name: array}, point {name: array})].
    #[pyo3(signature = (time_dir, fields=None))]
    fn boundary<'py>(&self, py: Python<'py>, time_dir: &str, fields: Option<Vec<String>>) -> PyResult<Bound<'py, PyList>> {
        let d = PathBuf::from(time_dir);
        let mesh = &self.mesh;
        let outs = py
            .detach(move || guarded(|| {
                let ff = select(&d, &fields)?;
                mesh.boundary(&ff)
            }))
            .map_err(err)?;
        let list = PyList::empty(py);
        for p in outs {
            let d = PyDict::new(py);
            d.set_item("name", &p.name)?;
            d.set_item("type", &p.kind)?;
            d.set_item("points", points3(py, p.points)?)?;
            d.set_item("face_offsets", p.face_offsets.into_pyarray(py))?;
            d.set_item("face_conn", p.face_conn.into_pyarray(py))?;
            let names = PyList::empty(py);
            let cd = PyDict::new(py);
            let pd = PyDict::new(py);
            for (name, c, pv) in p.data {
                names.append(&name)?;
                cd.set_item(&name, arr(py, c)?)?;
                pd.set_item(&name, arr(py, pv)?)?;
            }
            d.set_item("names", names)?;
            d.set_item("cell", cd)?;
            d.set_item("point", pd)?;
            list.append(d)?;
        }
        Ok(list)
    }
}

/// Read polyMesh: `faces_dir` holds faces, owner, neighbour, boundary;
/// `points_dir` holds points (often the same folder).
#[pyfunction]
pub fn read_mesh(py: Python<'_>, faces_dir: &str, points_dir: &str) -> PyResult<PolyMesh> {
    let (a, b) = (PathBuf::from(faces_dir), PathBuf::from(points_dir));
    let mesh = py.detach(move || guarded(|| Mesh::read(&a, &b))).map_err(err)?;
    Ok(PolyMesh { mesh })
}
