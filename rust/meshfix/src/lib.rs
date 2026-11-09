//! meshfix — mesh repair for OpenFOAM Studio.
//!
//! It does the steps of the Windows 3D-print repair (Printing3DModel.
//! RepairAsync in Windows.Graphics.Printing.3D.dll) in the same order and
//! with the same tolerances. The comments of stages.rs describe the steps.
//!
//! The Python side is fs_meshfix.py. The program works without this module:
//! the «meshfix» button is then disabled.

pub mod math;
pub mod mesh;
#[cfg(feature = "python")]
mod py;
pub mod stages;
// Diagnosis on a mesh file: RUSTFLAGS="--cfg meshfix_debug" (file not in git).
#[cfg(all(test, meshfix_debug))]
mod debug_tests;
#[cfg(test)]
mod tests;
pub mod union;

pub use mesh::Mesh;
use math::*;
use std::time::{Duration, Instant};

#[derive(Clone, Copy, Debug, PartialEq, Eq, Hash)]
pub enum Stage {
    Stitch1,
    CloseTrivialHoles1,
    RemoveDoubleTriangles,
    FixFlipped1,
    CloseTrivialHoles2,
    Stitch2,
    FixFlipped2,
    CloseAllHoles,
    Wrap,
    RemoveDegeneratedFaces,
    RemoveTinyShells,
    PrepareForExport,
}

/// The step order of the Windows repair ("repair_extended").
pub const PIPELINE: [Stage; 12] = [
    Stage::Stitch1,
    Stage::CloseTrivialHoles1,
    Stage::RemoveDoubleTriangles,
    Stage::FixFlipped1,
    Stage::CloseTrivialHoles2,
    Stage::Stitch2,
    Stage::FixFlipped2,
    Stage::CloseAllHoles,
    Stage::Wrap,
    Stage::RemoveDegeneratedFaces,
    Stage::RemoveTinyShells,
    Stage::PrepareForExport,
];

impl Stage {
    pub fn name(self) -> &'static str {
        match self {
            Stage::Stitch1 => "Stitch1",
            Stage::CloseTrivialHoles1 => "CloseTrivialHoles1",
            Stage::RemoveDoubleTriangles => "RemoveDoubleTriangles",
            Stage::FixFlipped1 => "FixFlipped1",
            Stage::CloseTrivialHoles2 => "CloseTrivialHoles2",
            Stage::Stitch2 => "Stitch2",
            Stage::FixFlipped2 => "FixFlipped2",
            Stage::CloseAllHoles => "CloseAllHoles",
            Stage::Wrap => "Wrap",
            Stage::RemoveDegeneratedFaces => "RemoveDegeneratedFaces",
            Stage::RemoveTinyShells => "RemoveTinyShells",
            Stage::PrepareForExport => "PrepareForExport",
        }
    }
}

/// Tolerances in normalized units (the largest extent is 5..400 units,
/// see `repair`). Black-box tests confirmed stitch and tiny-shell values.
#[derive(Clone, Debug)]
pub struct RepairOptions {
    pub normalize_scale: bool,
    pub stitch_tolerance: f64,
    pub weld_tolerance: f64,
    pub degenerate_tolerance: f64,
    pub tiny_shell_volume: f64,
    pub export_tolerance: f64,
    pub enable_wrap: bool,
    /// Stop after this step (for tests and diagnosis). None = all steps.
    pub stop_after: Option<Stage>,
}

impl Default for RepairOptions {
    fn default() -> Self {
        Self {
            normalize_scale: true,
            stitch_tolerance: 0.01,
            weld_tolerance: 1e-4,
            degenerate_tolerance: 0.002,
            tiny_shell_volume: 0.001,
            export_tolerance: 0.001,
            enable_wrap: true,
            stop_after: None,
        }
    }
}

#[derive(Clone, Debug, Default, PartialEq, Eq)]
pub struct MeshInfo {
    pub vertices: usize,
    pub edges: usize,
    pub faces: usize,
    pub boundary_edges: usize,
    pub manifold_edges: usize,
    pub non_manifold_edges: usize,
    pub flipped_edges: usize,
    pub shells: usize,
}

impl MeshInfo {
    pub fn is_closed(&self) -> bool {
        self.boundary_edges == 0 && self.non_manifold_edges == 0
    }
    pub fn is_oriented(&self) -> bool {
        self.flipped_edges == 0
    }
}

pub fn mesh_info(m: &Mesh) -> MeshInfo {
    let ef = m.edge_faces();
    let mut i = MeshInfo {
        vertices: m.vertices.len(),
        edges: ef.len(),
        faces: m.triangles.len(),
        shells: mesh::components(m, &ef).len(),
        ..Default::default()
    };
    for (&(a, b), fs) in &ef {
        match fs.len() {
            1 => i.boundary_edges += 1,
            2 => {
                i.manifold_edges += 1;
                let (f, g) = (&m.triangles[fs[0] as usize], &m.triangles[fs[1] as usize]);
                if mesh::directed(f, a, b) == mesh::directed(g, a, b) {
                    i.flipped_edges += 1;
                }
            }
            _ => i.non_manifold_edges += 1,
        }
    }
    i
}

#[derive(Debug)]
pub enum RepairError {
    EmptyMesh,
    ModelTooLarge(f64),
    BadIndex,
}

impl std::fmt::Display for RepairError {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        match self {
            RepairError::EmptyMesh => write!(f, "the mesh has no triangles"),
            RepairError::ModelTooLarge(e) => write!(f, "the model extent {e} is more than 1e7"),
            RepairError::BadIndex => write!(f, "a triangle index is out of range"),
        }
    }
}

impl std::error::Error for RepairError {}

#[derive(Clone, Debug)]
pub struct StageResult {
    pub stage: Stage,
    pub count: usize,
    pub time: Duration,
}

#[derive(Clone, Debug)]
pub struct RepairReport {
    pub before: MeshInfo,
    pub after: MeshInfo,
    /// Normalized = original · scale (after the shift to the bbox minimum).
    pub scale: f64,
    pub stages: Vec<StageResult>,
    /// What the union in the Wrap step did.
    pub union: union::UnionReport,
}

pub fn run_stage(stage: Stage, m: &mut Mesh, o: &RepairOptions) -> usize {
    match stage {
        Stage::Stitch1 | Stage::Stitch2 => stages::stitch(m, o.stitch_tolerance),
        Stage::CloseTrivialHoles1 | Stage::CloseTrivialHoles2 => stages::close_trivial_holes(m),
        Stage::RemoveDoubleTriangles => stages::remove_double_triangles(m, o.weld_tolerance),
        Stage::FixFlipped1 | Stage::FixFlipped2 => stages::fix_flipped(m),
        Stage::CloseAllHoles => stages::close_all_holes(m),
        Stage::Wrap => {
            if o.enable_wrap {
                stages::wrap(m)
            } else {
                0
            }
        }
        Stage::RemoveDegeneratedFaces => stages::remove_degenerated_faces(m, o.degenerate_tolerance),
        Stage::RemoveTinyShells => stages::remove_tiny_shells(m, o.tiny_shell_volume),
        Stage::PrepareForExport => stages::prepare_for_export(m, o.export_tolerance),
    }
}

/// Scale factor that puts the largest extent in [5, 400], as Windows does.
/// Measured: cubes of 0.5..5 mm and of 400..5000 mm show the same
/// relative result, cubes of 5..400 mm show the same absolute result.
pub fn normal_scale(ext: f64) -> f64 {
    if ext > 1e-6 && ext < 5.0 {
        5.0 / ext
    } else if ext > 400.0 {
        400.0 / ext
    } else {
        1.0
    }
}

/// Repair the mesh in place: normalize, do the 12 steps, scale back.
pub fn repair(
    mesh: &mut Mesh,
    opts: &RepairOptions,
    mut progress: impl FnMut(Stage, usize),
) -> Result<RepairReport, RepairError> {
    if mesh.triangles.is_empty() {
        return Err(RepairError::EmptyMesh);
    }
    let nv = mesh.vertices.len() as u32;
    if mesh.triangles.iter().any(|t| t.iter().any(|&i| i >= nv)) {
        return Err(RepairError::BadIndex);
    }
    let (lo, hi) = mesh.bbox().ok_or(RepairError::EmptyMesh)?;
    let ext = (0..3).map(|k| hi[k] - lo[k]).fold(0.0f64, f64::max);
    if ext > 1e7 {
        return Err(RepairError::ModelTooLarge(ext));
    }
    let s = if opts.normalize_scale { normal_scale(ext) } else { 1.0 };
    mesh.affine(1.0, scale(lo, -1.0));
    mesh.affine(s, [0.0; 3]);
    mesh.drop_collapsed();
    mesh.compact();
    let before = mesh_info(mesh);
    let mut out = Vec::with_capacity(PIPELINE.len());
    let mut union_rep = union::UnionReport::default();
    for stage in PIPELINE {
        let t0 = Instant::now();
        let count = if stage == Stage::Wrap && o_wrap(opts) {
            let (n, r) = stages::wrap_report(mesh);
            union_rep = r;
            n
        } else {
            run_stage(stage, mesh, opts)
        };
        out.push(StageResult { stage, count, time: t0.elapsed() });
        progress(stage, count);
        if opts.stop_after == Some(stage) {
            break;
        }
    }
    let after = mesh_info(mesh);
    mesh.affine(1.0 / s, lo);
    Ok(RepairReport { before, after, scale: s, stages: out, union: union_rep })
}

fn o_wrap(o: &RepairOptions) -> bool {
    o.enable_wrap
}
