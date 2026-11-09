//! meshfix — mesh repair for OpenFOAM Studio.
//!
//! It does the steps of the Windows 3D-print repair (Printing3DModel.
//! RepairAsync in Windows.Graphics.Printing.3D.dll) in the same order and
//! with the same tolerances. The comments of stages.rs describe the steps.
//!
//! The Python side is fs_meshfix.py. The program works without this module:
//! the «meshfix» button is then disabled.

pub mod aabb;
pub mod alphawrap;
pub mod arrange;
pub mod decimate;
pub mod delaunay;
pub mod math;
pub mod mesh;
pub mod planar;
#[cfg(feature = "python")]
mod py;
pub mod rims;
pub mod seal;
pub mod stages;
// Diagnosis on a mesh file: RUSTFLAGS="--cfg meshfix_debug" (file not in git).
#[cfg(all(test, meshfix_debug))]
mod debug_tests;
#[cfg(test)]
mod tests;
pub mod union;
pub mod volume;

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
    /// Fallback 1 after the 12 steps (not a Windows step): alpha wrap of
    /// the input, shrunk onto the input (alphawrap.rs).
    AlphaWrap,
    /// Fallback 2, the last one: volumetric remesh (volume.rs).
    VolumeRemesh,
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
            Stage::AlphaWrap => "AlphaWrap",
            Stage::VolumeRemesh => "VolumeRemesh",
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
    /// Fallback 1: when the 12 steps do not give a clean mesh, make an
    /// alpha wrap of the input and shrink it onto the input (alphawrap.rs).
    pub alpha_wrap: bool,
    /// Alpha wrap also on a mesh that the 12 steps made clean (for tests).
    pub alpha_wrap_force: bool,
    /// Ball radius and offset of the alpha wrap, in the units of the input
    /// (mm from fs_meshfix). None = from the model (`wrap_params`).
    pub wrap_alpha: Option<f64>,
    pub wrap_offset: Option<f64>,
    /// Fallback 2: volumetric remesh when the mesh is still not clean.
    pub volume_fallback: bool,
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
            alpha_wrap: true,
            alpha_wrap_force: false,
            wrap_alpha: None,
            wrap_offset: None,
            volume_fallback: true,
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
    /// Steps undone by the guard (they gave more open or non-manifold edges).
    /// A fallback that ran but was not used is in this list too.
    pub undone: Vec<Stage>,
    /// The fallback whose result replaced the result of the 12 steps:
    /// Stage::AlphaWrap or Stage::VolumeRemesh. None: no fallback used.
    pub fallback: Option<Stage>,
    /// The alpha wrap, when it ran.
    pub alpha_wrap: Option<alphawrap::WrapReport>,
    /// The volumetric remesh, when it ran.
    pub volume: Option<volume::VolumeReport>,
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
        // The fallbacks are not single steps: see `repair`.
        Stage::AlphaWrap | Stage::VolumeRemesh => 0,
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
    let input_norm = mesh.clone();
    let (mut out, union_rep, mut undone, pre_wrap) = pipeline(mesh, opts, &mut progress);
    let mut fallback = None;
    let mut wrap_rep = None;
    let mut vol_rep = None;
    // Fallbacks, in this order: alpha wrap, then volumetric remesh. Each
    // runs only when the mesh is not clean yet, and its result is used only
    // when it is clean (closed, oriented, each part outward) and not worse
    // than the input by the counts of the program's check.
    let run_fallbacks = opts.stop_after.is_none() && std::env::var("MESHFIX_FALLBACK").as_deref() != Ok("off");
    // An empty result (a flat plate: Windows gives no triangles too) gets
    // no fallback.
    let run_fallbacks = run_fallbacks && !mesh.triangles.is_empty();
    if run_fallbacks && opts.alpha_wrap && (opts.alpha_wrap_force || !is_clean(mesh)) {
        let t0 = Instant::now();
        let wo = wrap_params(&input_norm, opts, s);
        let res = std::panic::catch_unwind(std::panic::AssertUnwindSafe(|| alphawrap::wrap(&input_norm, &wo))).ok();
        let mut used = false;
        let mut faces = 0;
        if let Some((w, r)) = res {
            if let Some(w) = w {
                faces = w.triangles.len();
                if fallback_ok(&w, &before) {
                    *mesh = w;
                    used = true;
                    fallback = Some(Stage::AlphaWrap);
                }
            }
            wrap_rep = Some(r);
        }
        if !used {
            undone.push(Stage::AlphaWrap);
        }
        out.push(StageResult { stage: Stage::AlphaWrap, count: faces, time: t0.elapsed() });
        progress(Stage::AlphaWrap, faces);
    }
    let vol_mode = std::env::var("MESHFIX_VOL").unwrap_or_default();
    let want_vol = match vol_mode.as_str() {
        "0" | "off" => false,
        "force" => true,
        _ => run_fallbacks && opts.volume_fallback && !is_clean(mesh),
    };
    if want_vol && !mesh.triangles.is_empty() {
        let t0 = Instant::now();
        // Source of the remesh: the mesh before the union (after
        // CloseAllHoles). The raw input leaks: open thin parts fill with
        // "outside". MESHFIX_VOL_SRC=input|result for diagnosis.
        let src_mode = std::env::var("MESHFIX_VOL_SRC").unwrap_or_default();
        let src = match (src_mode.as_str(), &pre_wrap) {
            ("input", _) => &input_norm,
            ("result", _) => &*mesh,
            (_, Some(p)) => p,
            _ => &*mesh,
        };
        let vo = volume::VolumeOptions::from_env();
        let res = std::panic::catch_unwind(std::panic::AssertUnwindSafe(|| volume::remesh(src, &vo))).ok().flatten();
        let mut used = false;
        let mut faces = 0;
        if let Some((mut vm, mut rep)) = res {
            stages::remove_zero_area_with(&mut vm, true);
            volume::fix_zero_area(&mut vm);
            rep.times.push(("zero_area", t0.elapsed().as_secs_f64()));
            rep.clean = is_clean(&vm);
            // A large hole lets the flood fill into a body, and the body is
            // lost. The result is then much smaller than the source.
            let (v_new, v_src) = (signed_volume(&vm), signed_volume(src).abs());
            rep.volume_ratio = if v_src > 0.0 { v_new / v_src } else { 1.0 };
            faces = vm.triangles.len();
            if fallback_ok(&vm, &before) && rep.volume_ratio > 0.3 {
                *mesh = vm;
                used = true;
                fallback = Some(Stage::VolumeRemesh);
            }
            vol_rep = Some(rep);
        }
        if !used {
            undone.push(Stage::VolumeRemesh);
        }
        out.push(StageResult { stage: Stage::VolumeRemesh, count: faces, time: t0.elapsed() });
        progress(Stage::VolumeRemesh, faces);
    }
    let after = mesh_info(mesh);
    mesh.affine(1.0 / s, lo);
    Ok(RepairReport {
        before,
        after,
        scale: s,
        stages: out,
        union: union_rep,
        undone,
        fallback,
        alpha_wrap: wrap_rep,
        volume: vol_rep,
    })
}

/// The 12 steps with the guard. Returns (step results, union report,
/// steps undone, the mesh after CloseAllHoles).
fn pipeline(
    mesh: &mut Mesh,
    opts: &RepairOptions,
    progress: &mut impl FnMut(Stage, usize),
) -> (Vec<StageResult>, union::UnionReport, Vec<Stage>, Option<Mesh>) {
    let mut out = Vec::with_capacity(PIPELINE.len());
    let mut union_rep = union::UnionReport::default();
    let mut defects = mesh.defect_edges();
    let mut undone: Vec<Stage> = Vec::new();
    let mut pre_wrap = None;
    for stage in PIPELINE {
        let t0 = Instant::now();
        // Guard: a step that gives more open + non-manifold edges than
        // before is undone. The Windows steps reduce these numbers; a step
        // that does not is wrong for this mesh.
        let snapshot = mesh.clone();
        let mut count = if stage == Stage::Wrap && o_wrap(opts) {
            let (n, r) = stages::wrap_report(mesh);
            union_rep = r;
            n
        } else {
            run_stage(stage, mesh, opts)
        };
        let now = mesh.defect_edges();
        if now > defects && std::env::var_os("MESHFIX_NO_UNION_GUARD").is_none() {
            *mesh = snapshot;
            undone.push(stage);
            count = 0;
        } else {
            defects = now;
        }
        out.push(StageResult { stage, count, time: t0.elapsed() });
        if stage == Stage::CloseAllHoles && opts.volume_fallback {
            pre_wrap = Some(mesh.clone());
        }
        progress(stage, count);
        if opts.stop_after == Some(stage) {
            break;
        }
    }
    (out, union_rep, undone, pre_wrap)
}

/// A fallback result is used only when it is clean and no count of the
/// program's check (open edges, non-manifold edges, flipped edges) is
/// larger than in the input.
fn fallback_ok(m: &Mesh, before: &MeshInfo) -> bool {
    if !is_clean(m) {
        return false;
    }
    let i = mesh_info(m);
    i.boundary_edges <= before.boundary_edges
        && i.non_manifold_edges <= before.non_manifold_edges
        && i.flipped_edges <= before.flipped_edges
}

/// Signed volume of all faces.
pub fn signed_volume(m: &Mesh) -> f64 {
    (0..m.triangles.len())
        .map(|f| {
            let [a, b, c] = m.tri_pos(f);
            det3(a, b, c)
        })
        .sum::<f64>()
        / 6.0
}

/// Closed, manifold, consistently oriented, and each part (faces that
/// share a vertex, as the program counts them) has a positive volume.
pub fn is_clean(m: &Mesh) -> bool {
    use rayon::prelude::*;
    if m.triangles.is_empty() {
        return false;
    }
    // Each directed edge once, and its reverse once: then each undirected
    // edge has exactly two faces in opposite directions.
    let mut d: Vec<(u32, u32)> = m
        .triangles
        .par_iter()
        .flat_map_iter(|t| [(t[0], t[1]), (t[1], t[2]), (t[2], t[0])])
        .collect();
    if d.iter().any(|e| e.0 == e.1) {
        return false;
    }
    d.par_sort_unstable();
    if d.windows(2).any(|w| w[0] == w[1]) {
        return false;
    }
    if !d.par_iter().all(|&(a, b)| d.binary_search(&(b, a)).is_ok()) {
        return false;
    }
    let n = m.vertices.len();
    let mut p: Vec<u32> = (0..n as u32).collect();
    fn find(p: &mut [u32], mut x: u32) -> u32 {
        while p[x as usize] != x {
            p[x as usize] = p[p[x as usize] as usize];
            x = p[x as usize];
        }
        x
    }
    for t in &m.triangles {
        for k in 1..3 {
            let (a, b) = (find(&mut p, t[0]), find(&mut p, t[k]));
            if a != b {
                p[a.max(b) as usize] = a.min(b);
            }
        }
    }
    let mut vol = std::collections::HashMap::<u32, f64>::new();
    for t in &m.triangles {
        let r = find(&mut p, t[0]);
        let [a, b, c] = [m.vertices[t[0] as usize], m.vertices[t[1] as usize], m.vertices[t[2] as usize]];
        *vol.entry(r).or_default() += det3(a, b, c) / 6.0;
    }
    vol.values().all(|&v| v > 0.0)
}

fn env_f64(name: &str) -> Option<f64> {
    std::env::var(name).ok().and_then(|s| s.parse().ok())
}

/// Alpha wrap parameters from the model (normalized units).
///
/// The ball radius alpha sets the size of the wrap faces: about
/// 1.6·area/alpha² faces. The target is two wrap faces for each input
/// face, 50 000 .. 600 000. The offset is alpha/30, and the shrink stops
/// at offset/10 from the input. The refine passes of the shrink split the
/// faces that stay more than 2·offset farther from the input.
/// `s` is the normalization scale (normalized = input units · s).
pub fn wrap_params(src: &Mesh, o: &RepairOptions, s: f64) -> alphawrap::WrapOptions {
    let area: f64 = (0..src.triangles.len())
        .map(|f| {
            let [a, b, c] = src.tri_pos(f);
            tri_area(a, b, c)
        })
        .sum();
    let (lo, hi) = src.bbox().unwrap_or(([0.0; 3], [1.0; 3]));
    let ext = (0..3).map(|k| hi[k] - lo[k]).fold(0.0f64, f64::max).max(1e-9);
    let target = (2 * src.triangles.len()).clamp(50_000, 600_000) as f64;
    let mut alpha = (1.6 * area.max(1e-12) / target).sqrt().clamp(ext / 2000.0, ext / 20.0);
    if let Some(a) = o.wrap_alpha.filter(|&a| a > 0.0).map(|a| a * s).or_else(|| env_f64("MESHFIX_ALPHA")) {
        alpha = a;
    }
    let mut offset = alpha / 30.0;
    if let Some(v) = o.wrap_offset.filter(|&v| v > 0.0).map(|v| v * s).or_else(|| env_f64("MESHFIX_OFFSET")) {
        offset = v;
    }
    let gap = env_f64("MESHFIX_GAP").unwrap_or(offset / 10.0);
    let est = 1.6 * area / (alpha * alpha);
    alphawrap::WrapOptions {
        alpha,
        offset,
        max_points: ((est * 20.0) as usize).clamp(200_000, 30_000_000),
        shrink: std::env::var_os("MESHFIX_NO_SHRINK").is_none(),
        gap,
        refine_passes: env_f64("MESHFIX_REFINE").map(|x| x as usize).unwrap_or(3),
        refine_tol: offset * env_f64("MESHFIX_REFINE_TOL").unwrap_or(2.0),
    }
}

fn o_wrap(o: &RepairOptions) -> bool {
    o.enable_wrap
}
