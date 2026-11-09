"""meshfix repair: the steps of the Windows repair, in Rust.

WHAT IT IS. «Полагодити як слайсер» (fs_winfix) calls the Windows service
(Windows.Graphics.Printing.3D.dll, Printing3DModel.RepairAsync). meshfix
(rust/meshfix, module meshfix.pyd) does the same 12 steps in the same order
and with the same tolerances: stitch, close holes of 3 edges, remove
doubles, orient the normals, close all holes, "wrap", remove degenerate
faces and tiny shells. The comments of rust/meshfix/src/stages.rs
describe the steps.

DIFFERENCES FROM THE SERVICE (measured on tests/meshfix_cases.py):
- it works without WinRT and outside Windows, with no 3MF exchange;
- it does NOT refine the mesh: the service cuts each side of a 10 mm cube
  into 16 triangles (12 → 96), meshfix keeps 12;
- it does NOT move vertices: the service moves the faces at the bbox
  maximum in by 0.002 normalized units (2 um on a 10 mm cube);
- bodies that intersect become one body (a union), as in the service.
  Faces that touch almost coplanar (less than 0.5 deg) are a contact, not
  a cut. A cut group that would give open edges is left uncut;
- an edge or a vertex that two bodies share gets one copy for each body.
  The service keeps the two bodies joined there (one shell).

UNITS. The step tolerances are in mm, as in the service. Thus the mesh goes
from m to mm for the repair and back, as in fs_winfix.
"""

from __future__ import annotations

import numpy as np
import pyvista as pv

try:                                       # pragma: no cover - depends on the build
    import meshfix as _rs
except ImportError:                        # pragma: no cover
    _rs = None

M_TO_MM = 1000.0

# Step names for the log. The keys are Stage::name in rust/meshfix.
STEP_NAMES = {
    "Stitch1": "зшито", "Stitch2": "зшито (2)",
    "CloseTrivialHoles1": "дірок на 3 ребра", "CloseTrivialHoles2": "дірок на 3 ребра (2)",
    "RemoveDoubleTriangles": "дублів прибрано",
    "FixFlipped1": "розвернуто трикутників", "FixFlipped2": "розвернуто (2)",
    "CloseAllHoles": "дірок закрито",
    "Wrap": "обгортка: прибрано частин",
    "RemoveDegeneratedFaces": "вироджених виправлено",
    "RemoveTinyShells": "крихітних оболонок прибрано",
    "PrepareForExport": "T-зʼєднань розрізано",
}

# Report of the last repair. The window uses it for the status line.
last_report: dict | None = None


def available() -> bool:
    return _rs is not None and hasattr(_rs, "repair")


def why_not() -> str:
    if available():
        return ""
    return ("Модуль meshfix (Rust) не зібрано. Зібрати:" + "\n\n"
            "    powershell -ExecutionPolicy Bypass -File tools\\build_rust.ps1"
            + "\n\n" + "Ремонт службою Windows («Полагодити як слайсер») "
            "працює й без нього.")


def _triangles(mesh: pv.PolyData) -> np.ndarray:
    faces = np.asarray(mesh.faces)
    if faces.size == 0 or faces.size % 4 or not np.all(faces[::4] == 3):
        mesh = mesh.triangulate()
        faces = np.asarray(mesh.faces)
    return faces.reshape(-1, 4)[:, 1:]


def repair_arrays(points_mm, tris, *, wrap: bool = True):
    """(points mm, triangles) → (points mm, triangles, report). No pyvista."""
    if not available():
        raise RuntimeError(why_not())
    return _rs.repair(np.ascontiguousarray(points_mm, dtype=np.float64),
                      np.ascontiguousarray(tris, dtype=np.int64),
                      True, bool(wrap))


def summary(report: dict) -> str:
    """One line "what was done" from the steps with a count above zero."""
    parts = []
    for name, count, _sec in report.get("stages", ()):
        if count:
            parts.append(f"{STEP_NAMES.get(name, name)} {count}")
    u = report.get("union") or {}
    if u.get("pairs"):
        parts.append(f"перетинів тіл розрізано {u['pairs']}")
    if u.get("skipped_clusters"):
        parts.append(f"місць перетину лишено як є {u['skipped_clusters']}")
    if u.get("rolled_back") or u.get("panicked"):
        parts.append("обʼєднання тіл не вдалося — тіла лишилися окремими")
    return ", ".join(parts) if parts else "кроки нічого не змінили"


def repair(mesh: pv.PolyData, say=None) -> pv.PolyData:
    """A repaired copy of `mesh`. The input and the output are in m.

    The same contract as fs_winfix.repair: call it from a thread
    (RepairWorker). Rust releases the GIL, thus the window stays live.
    """
    global last_report
    say = say or (lambda _text: None)
    if mesh is None or mesh.n_cells == 0:
        raise ValueError("Нема чого лагодити: у моделі жодного трикутника.")
    if not available():
        raise RuntimeError(why_not())
    tri = _triangles(mesh)
    pts = np.asarray(mesh.points, dtype=np.float64) * M_TO_MM
    say(f"meshfix: {len(tri)} трикутників, 12 кроків")
    try:
        out_p, out_t, report = repair_arrays(pts, tri)
    except ValueError as exc:
        raise RuntimeError(f"meshfix відмовив: {exc}.") from exc
    last_report = report
    say("meshfix: " + summary(report))
    if len(out_t) == 0:
        raise RuntimeError("Після ремонту не лишилося жодного трикутника: "
                           "модель була плоскою або з самих крихітних "
                           "оболонок.")
    out_t = np.asarray(out_t, dtype=np.int64)
    return pv.PolyData(
        np.asarray(out_p) / M_TO_MM,
        np.hstack([np.full((len(out_t), 1), 3, np.int64), out_t]).ravel())
