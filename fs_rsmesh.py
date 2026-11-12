"""Fast reading of a reconstructed OpenFOAM case for «Results» (T47).

foamrs (Rust, rust/foamrs/src/polymesh.rs) reads polyMesh and the vol
fields on all cores and gives numpy arrays. This module makes from them the
same VTK objects that pv.OpenFOAMReader gives: a vtkUnstructuredGrid (the
volume, with VTK_POLYHEDRON cells through SetPolyhedralCells) and one
vtkPolyData per patch. The numbers are the same as the numbers of the VTK
reader (tests/test_rsmesh.py compares them field by field).

fs_post.Results calls this module first. On ANY error it reads through VTK,
as before, and writes one log line. Not supported here (they go to VTK):
decomposed cases (processor*), point fields, regex patch names in
boundaryField, binary faceList. `FS_RSMESH=0` (environment) turns this
reader off.

Measured 2026-11-12 (laptop, warm disk cache): see devlog
wip-t47-rust-reader.md.
"""

from __future__ import annotations

import logging
import os

import numpy as np

try:                                       # pragma: no cover - depends on the build
    import foamrs as _rs
except ImportError:                        # pragma: no cover
    _rs = None

log = logging.getLogger(__name__)



def available() -> bool:
    """True if foamrs has the mesh reader and it is not turned off."""
    if os.environ.get("FS_RSMESH", "1").strip() in ("0", "off", "no"):
        return False
    return _rs is not None and hasattr(_rs, "read_mesh")


def time_names(case_dir: str) -> list[tuple[float, str]]:
    """All time folders of the case (with 0), sorted by time."""
    # One rule for the program: fs_timeline.time_folders (T51).
    import fs_timeline as ftl
    return ftl.time_folders(case_dir, positive=False)


def nearest_time(case_dir: str, time: float) -> str | None:
    """The folder name of the time nearest to `time` (as fs_post._read_at)."""
    names = time_names(case_dir)
    if not names:
        return None
    return min(names, key=lambda tn: abs(tn[0] - time))[1]


def _is_file(path: str) -> bool:
    return os.path.isfile(path) or os.path.isfile(path + ".gz")


def instances(case_dir: str, time_name: str) -> tuple[str, str]:
    """(polyMesh folder with faces, polyMesh folder with points) of a time.

    As vtkOpenFOAMReader (PopulateMeshTimeIndices): the latest time folder
    up to this time that has polyMesh/faces (points: polyMesh/points), else
    constant/polyMesh.
    """
    const = os.path.join(case_dir, "constant", "polyMesh")
    faces_dir = points_dir = const
    t_here = float(time_name)
    for t, n in time_names(case_dir):
        if t > t_here:
            break
        pm = os.path.join(case_dir, n, "polyMesh")
        if os.path.isdir(pm):
            if _is_file(os.path.join(pm, "faces")):
                faces_dir = pm
            if _is_file(os.path.join(pm, "points")):
                points_dir = pm
    return faces_dir, points_dir


def _stamp(folder: str) -> tuple:
    out = []
    for f in ("faces", "owner", "neighbour", "boundary", "points"):
        for p in (os.path.join(folder, f), os.path.join(folder, f + ".gz")):
            try:
                st = os.stat(p)
                out.append((f, st.st_mtime_ns, st.st_size))
                break
            except OSError:
                continue
    return tuple(out)


class Reader:
    """One case: keeps the last polyMesh, makes the volume and the patches."""

    def __init__(self, case_dir: str):
        self.case_dir = str(case_dir)
        self._mesh = None              # (key, foamrs.PolyMesh)

    def mesh(self, time_name: str):
        fd, pd = instances(self.case_dir, time_name)
        key = (fd, pd, _stamp(fd), _stamp(pd))
        if self._mesh is not None and self._mesh[0] == key:
            return self._mesh[1]
        self._mesh = None              # free the old mesh before the new read
        m = _rs.read_mesh(fd, pd)
        self._mesh = (key, m)
        return m

    def volume(self, time_name: str, fields=None):
        """pv.UnstructuredGrid of the time with `fields` (None: all)."""
        m = self.mesh(time_name)
        d = m.volume(os.path.join(self.case_dir, time_name),
                     None if fields is None else list(fields))
        return make_grid(d)

    def boundary(self, time_name: str, fields=None) -> list:
        """[(patch name, pv.PolyData)] in file order, with all vol fields."""
        m = self.mesh(time_name)
        got = m.boundary(os.path.join(self.case_dir, time_name),
                         None if fields is None else list(fields))
        return [(p["name"], make_patch(p)) for p in got]


def _id_array(a: np.ndarray):
    from vtkmodules.util.numpy_support import numpy_to_vtkIdTypeArray
    return numpy_to_vtkIdTypeArray(np.ascontiguousarray(a, dtype=np.int64),
                                   deep=False)


def _cell_array(offsets: np.ndarray, conn: np.ndarray):
    from vtkmodules.vtkCommonDataModel import vtkCellArray
    ca = vtkCellArray()
    ca.SetData(_id_array(offsets), _id_array(conn))
    return ca


def _add(store, name: str, arr: np.ndarray) -> None:
    store[name] = arr


def make_grid(d: dict):
    """vtkUnstructuredGrid (as pyvista) from foamrs PolyMesh.volume."""
    import pyvista as pv
    from vtkmodules.util.numpy_support import numpy_to_vtk
    from vtkmodules.vtkCommonCore import vtkPoints
    from vtkmodules.vtkCommonDataModel import vtkUnstructuredGrid

    grid = vtkUnstructuredGrid()
    pts = vtkPoints()
    pts.SetData(numpy_to_vtk(d["points"], deep=False))
    grid.SetPoints(pts)
    types = numpy_to_vtk(d["types"], deep=False,
                         array_type=3)             # VTK_UNSIGNED_CHAR
    cells = _cell_array(d["offsets"], d["conn"])
    if len(d["pface_conn"]):
        floc = _cell_array(d["floc_offsets"],
                           np.arange(len(d["pface_offsets"]) - 1,
                                     dtype=np.int64))
        faces = _cell_array(d["pface_offsets"], d["pface_conn"])
        grid.SetPolyhedralCells(types, cells, floc, faces)
    else:
        grid.SetCells(types, cells)
    out = pv.wrap(grid)
    for name in d["names"]:
        _add(out.cell_data, name, d["cell"][name])
        _add(out.point_data, name, d["point"][name])
    return out


def make_patch(p: dict):
    """vtkPolyData (as pyvista) of one patch from foamrs PolyMesh.boundary."""
    import pyvista as pv
    from vtkmodules.util.numpy_support import numpy_to_vtk
    from vtkmodules.vtkCommonCore import vtkPoints
    from vtkmodules.vtkCommonDataModel import vtkPolyData

    poly = vtkPolyData()
    pts = vtkPoints()
    pts.SetData(numpy_to_vtk(p["points"], deep=False))
    poly.SetPoints(pts)
    poly.SetPolys(_cell_array(p["face_offsets"], p["face_conn"]))
    out = pv.wrap(poly)
    for name in p["names"]:
        _add(out.cell_data, name, p["cell"][name])
        _add(out.point_data, name, p["point"][name])
    return out
