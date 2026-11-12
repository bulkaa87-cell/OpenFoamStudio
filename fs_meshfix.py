"""meshfix repair: the steps of the Windows repair, in Rust.

WHAT IT IS. The method «Windows» of the «Полагодити» menu (fs_winfix)
calls the Windows service
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
- the Wrap step cuts all faces against each other and keeps the faces
  between inside and outside by the winding number of the whole input
  (also of open parts). Steps after it do not make faces cross.
- the last step StlSafe (rust/meshfix/src/stlsafe.rs) makes the result
  keep its counts after a write to STL (float32) and a read that joins
  equal points: the copies of a pinched edge or vertex move apart by some
  float32 steps (about 1e-3 mm on a model of 3 m), and faces that the
  rounding makes degenerate get a flip or a small vertex move. Report key
  "stl_safe".
- fallbacks, when the 12 steps do not give a closed mesh: first an alpha
  wrap of the input (shrunk onto the input), then a volumetric remesh.
  Each result is used only when it is closed and no count of the
  program's check is larger than in the input. Report key "fallback".

FORCED METHODS. repair(..., method=...) with "alphawrap" or "volume" runs
only that fallback, also on a mesh that the 12 steps would make clean. The
«Полагодити» menu of the window gives these methods (METHODS below).

UNITS. The step tolerances are in mm, as in the service. Thus the mesh goes
from m to mm for the repair and back, as in fs_winfix.
"""

from __future__ import annotations

import numpy as np
import pyvista as pv
import fs_i18n
from fs_i18n import tr

try:                                       # pragma: no cover - depends on the build
    import meshfix as _rs
except ImportError:                        # pragma: no cover
    _rs = None

M_TO_MM = 1000.0

# Step names for the log. The keys are Stage::name in rust/meshfix.
STEP_NAMES = {
    "Stitch1": "stitched", "Stitch2": "stitched (2)",
    "CloseTrivialHoles1": "3-edge holes", "CloseTrivialHoles2": "3-edge holes (2)",
    "RemoveDoubleTriangles": "duplicates removed",
    "FixFlipped1": "triangles flipped", "FixFlipped2": "flipped (2)",
    "CloseAllHoles": "holes closed",
    "Wrap": "wrap: parts removed",
    "RemoveDegeneratedFaces": "degenerate faces fixed",
    "RemoveTinyShells": "tiny shells removed",
    "PrepareForExport": "T-junctions cut",
    "AlphaWrap": "alpha wrap, triangles",
    "VolumeRemesh": "volume rebuild, triangles",
    "StlSafe": "points moved for STL",
}

# Text for the status line when a fallback replaced the result.
FALLBACK_TEXT = {
    "AlphaWrap": "12 steps did not give a closed mesh — result: model "
                 "wrap (alpha wrap), shrunk onto the surface",
    "VolumeRemesh": "12 steps did not give a closed mesh — result: "
                    "volume rebuild (parts thinner than ~0.7 mm can "
                    "disappear)",
}

# Methods of this module: key → (name in the status text, report key of
# the step that gives the result; None = the 12 steps).
METHODS = {
    "meshfix": ("meshfix", None),
    "alphawrap": ("alpha wrap", "AlphaWrap"),
    "volume": ("volume rebuild", "VolumeRemesh"),
}

# Status text of a forced method.
FORCED_TEXT = {
    "AlphaWrap": "model wrap (alpha wrap), shrunk onto the surface",
    "VolumeRemesh": "volume rebuild (parts thinner than ~0.7 mm may have "
                    "disappeared)",
}

# Report of the last repair. The window uses it for the status line.
last_report: dict | None = None


def available() -> bool:
    return _rs is not None and hasattr(_rs, "repair")


def why_not() -> str:
    if available():
        return ""
    return (tr("The meshfix module (Rust) is not built. To build it:\n\n   "
               " powershell -ExecutionPolicy Bypass -File "
               "tools\\build_rust.ps1\n\nRepair with the Windows service "
               "(the “Windows” method in the “Repair” menu) also works "
               "without it."))


def _triangles(mesh: pv.PolyData) -> np.ndarray:
    faces = np.asarray(mesh.faces)
    if faces.size == 0 or faces.size % 4 or not np.all(faces[::4] == 3):
        mesh = mesh.triangulate()
        faces = np.asarray(mesh.faces)
    return faces.reshape(-1, 4)[:, 1:]


def repair_arrays(points_mm, tris, *, wrap: bool = True,
                  method: str = "meshfix"):
    """(points mm, triangles) → (points mm, triangles, report). No pyvista."""
    if not available():
        raise RuntimeError(why_not())
    if method not in METHODS:
        raise ValueError(f"no meshfix method {method!r}")
    args = (np.ascontiguousarray(points_mm, dtype=np.float64),
            np.ascontiguousarray(tris, dtype=np.int64), True, bool(wrap))
    if method == "meshfix":
        return _rs.repair(*args)
    return _rs.repair(*args, method=method)


# The end of a repair step text that is a verdict (what the repair did):
# the window keeps such a line in the log. A character, not a word of the
# text: the text is translated (T24). Invisible (U+2063).
VERDICT = "\u2063"


def summary(report: dict) -> str:
    """One line "what was done" from the steps with a count above zero."""
    parts = []
    fallback = report.get("fallback")
    for name, count, _sec in report.get("stages", ()):
        if name in FALLBACK_TEXT and name != fallback:
            continue                       # ran, but its result was not used
        if count:
            parts.append(tr(STEP_NAMES.get(name, name)) + f" {count}")
    u = report.get("union") or {}
    if u.get("pairs"):
        parts.append(tr("body intersections cut {u}", u=u['pairs']))
    if u.get("skipped_clusters"):
        parts.append(tr("intersection spots left as they are {u}", u=u['skipped_clusters']))
    if u.get("rolled_back") or u.get("panicked"):
        parts.append(tr("merging the bodies failed — the bodies stay separate"))
    if report.get("guard"):
        return guard_text(report["guard"])
    if report.get("method", "meshfix") != "meshfix" and fallback in FORCED_TEXT:
        return tr(FORCED_TEXT[fallback])
    if fallback in FALLBACK_TEXT:
        return tr(FALLBACK_TEXT[fallback])
    after = report.get("after") or {}
    if after.get("boundary_edges") or after.get("non_manifold_edges"):
        # meshfix only cuts and joins closed bodies. An assembly with open
        # and non-manifold parts needs the full wrap of the Windows service.
        parts.append(tr("open edges left {get}, non-manifold {get2} — "
                        "for such an assembly, use “Windows” or “volume "
                        "rebuild” in the “Repair” menu", get=after.get('boundary_edges', 0), get2=after.get('non_manifold_edges', 0)))
    return (fs_i18n.join(", ", parts) if parts
            else tr("the steps changed nothing"))


def repair(mesh: pv.PolyData, say=None,
           method: str = "meshfix") -> pv.PolyData:
    """A repaired copy of `mesh`. The input and the output are in m.

    The same contract as fs_winfix.repair: call it from a thread
    (RepairWorker). Rust releases the GIL, thus the window stays live.
    method: a key of METHODS. A forced method that gives no result raises
    RuntimeError (the window then puts the model back).
    """
    global last_report
    say = say or (lambda _text: None)
    if mesh is None or mesh.n_cells == 0:
        raise ValueError(tr("Nothing to repair: the model has no triangles."))
    if not available():
        raise RuntimeError(why_not())
    tri = _triangles(mesh)
    pts = np.asarray(mesh.points, dtype=np.float64) * M_TO_MM
    label, stage = METHODS[method]
    say(tr("{label}: {n_tri} triangles{v}", label=label, n_tri=len(tri), v=", 12 steps" if stage is None else ""))
    try:
        out_p, out_t, report = repair_arrays(pts, tri, method=method)
    except ValueError as exc:
        raise RuntimeError(tr("{label} failed: {exc}.", label=label, exc=exc)) from exc
    last_report = report
    if stage is not None and report.get("fallback") != stage:
        raise RuntimeError(forced_failure(method, report))
    say(tr(label) + ": " + summary(report) + VERDICT)
    if len(out_t) == 0:
        raise RuntimeError(tr("No triangles are left after the repair: the "
                           "model was flat or made only of tiny shells."))
    out_t = np.asarray(out_t, dtype=np.int64)
    out = pv.PolyData(
        np.asarray(out_p) / M_TO_MM,
        np.hstack([np.full((len(out_t), 1), 3, np.int64), out_t]).ravel())
    # Guard for the whole repair, with the counts of the program's own
    # check (the same numbers as «Дірки / Вивернуті / Non-manifold»). When
    # one of them is larger after the repair, the input comes back.
    worse = worse_than(mesh, out)
    report["guard"] = worse
    if worse:
        say(tr(label) + ": " + guard_text(worse) + VERDICT)
        return mesh.copy()
    return out


def forced_failure(method: str, report: dict) -> str:
    """Text for the dialog when a forced method gave no result."""
    if method == "volume":
        v = report.get("volume") or {}
        if v and 0 < v.get("volume_ratio", 1.0) <= 0.3:
            return (tr("The volume rebuild lost the body: {v:.0f}% of "
                       "the volume is left. Most often this is a large "
                       "hole through which the “outside” leaked inside. "
                       "Try meshfix or alpha wrap.", v=v['volume_ratio'] * 100))
        return (tr("The volume rebuild gave no surface: the model probably "
                "consists of parts thinner than ~0.7 mm."))
    return (tr("Alpha wrap did not give a closed shell. Try meshfix or the "
            "volume rebuild."))


# Program counts that the guard compares: (key, label for the status line).
GUARD_COUNTS = (("open_edges", "holes"), ("flipped_faces", "flipped"),
                ("non_manifold_edges", "non-manifold"))


def worse_than(before: pv.PolyData, after: pv.PolyData) -> list:
    """[(label, before, after)] for each program count that got larger."""
    import fs_geometry as geo
    d0, d1 = geo.diagnose(before), geo.diagnose(after)
    return [(label, getattr(d0, key), getattr(d1, key))
            for key, label in GUARD_COUNTS
            if getattr(d1, key) > getattr(d0, key)]


def guard_text(worse: list) -> str:
    return (tr("the result is worse than the input model ({join}) — the "
               "model stays unchanged", join=", ".join(f"{label} {a} → {b}" for label, a, b in worse)))
