//! Volumetric remesh: the last fallback when the repair steps do not give a
//! closed mesh.
//!
//! 1. A regular grid covers the model. Each triangle (dilated by `delta`)
//!    marks the cells it touches.
//! 2. The edges of the Kuhn tetrahedra of each touched cell are tested
//!    against the triangles: an edge that a triangle cuts is "blocked".
//! 3. A flood fill from the grid corner over the open edges finds the
//!    outside. Each grid point that the fill does not reach is inside.
//!    This test does not use the face orientation. Thus inverted parts,
//!    inner walls and overlapping parts give no problem. Gaps smaller than
//!    2·delta do not leak.
//! 4. Marching tetrahedra on the inside/outside labels give a closed,
//!    manifold, outward surface. Each output vertex is the first hit of the
//!    input surface, seen from the outside grid point. Thus the vertices are
//!    on the input surface.
//! 5. A quadric decimation (planes of the input triangles) removes most
//!    vertices on flat and smooth areas and moves vertices onto the sharp
//!    edges and corners of the input.
//!
//! All work is in grid units (one cell = 1).

use crate::math::*;
use crate::mesh::Mesh;
use rayon::prelude::*;
use std::time::Instant;

/// Block edge in cells (the flood fill skips empty blocks).
const B: usize = 8;
const BB: usize = B * B * B;

/// Kuhn edge directions. Each cube splits into 6 tetrahedra along the
/// main diagonal; all tetrahedron edges have one of these directions.
const DIRS: [[i64; 3]; 7] =
    [[1, 0, 0], [0, 1, 0], [0, 0, 1], [1, 1, 0], [1, 0, 1], [0, 1, 1], [1, 1, 1]];

fn dir_index(d: [i64; 3]) -> usize {
    DIRS.iter().position(|x| *x == d).expect("not a Kuhn direction")
}

#[derive(Clone, Debug)]
pub struct VolumeOptions {
    /// Cells along the largest extent of the model.
    pub cells: usize,
    /// Dilation of the triangles in cells: gaps below 2·delta are closed.
    pub delta: f64,
    /// Decimation: largest root of the sum of squared distances from a new
    /// vertex to the input planes that it carries, in cells.
    pub decimate_tol: f64,
    /// Switch off the decimation (for diagnosis).
    pub decimate: bool,
    /// Edge of the parallel decimation pieces, in cells.
    pub chunk: f64,
    /// Parts below this volume (cells³) are bubbles of the grid: removed.
    pub min_shell: f64,
    /// Budget of surface cells (the grid gets coarser above it).
    pub max_cells: f64,
}

impl Default for VolumeOptions {
    fn default() -> Self {
        Self { cells: 2048, delta: 0.05, decimate_tol: 0.2, decimate: true, chunk: 48.0, min_shell: 8.0, max_cells: 1.6e7 }
    }
}

impl VolumeOptions {
    /// Default values, changed by MESHFIX_VOL_CELLS, MESHFIX_VOL_DELTA,
    /// MESHFIX_VOL_TOL, MESHFIX_VOL_NODEC (diagnosis switches).
    pub fn from_env() -> Self {
        let mut o = Self::default();
        let get = |k: &str| std::env::var(k).ok().and_then(|v| v.parse::<f64>().ok());
        if let Some(v) = get("MESHFIX_VOL_CELLS") {
            o.cells = v as usize;
        }
        if let Some(v) = get("MESHFIX_VOL_DELTA") {
            o.delta = v;
        }
        if let Some(v) = get("MESHFIX_VOL_TOL") {
            o.decimate_tol = v;
        }
        if let Some(v) = get("MESHFIX_VOL_CHUNK") {
            o.chunk = v;
        }
        if std::env::var_os("MESHFIX_VOL_NODEC").is_some() {
            o.decimate = false;
        }
        o
    }
}

#[derive(Clone, Debug, Default)]
pub struct VolumeReport {
    pub h: f64,
    pub dims: [usize; 3],
    pub surface_cells: usize,
    pub surface_blocks: usize,
    pub mt_faces: usize,
    pub faces: usize,
    pub missing_hits: usize,
    pub clean: bool,
    pub dropped_shells: usize,
    /// Volume of the remesh / |volume| of its source.
    pub volume_ratio: f64,
    pub times: Vec<(&'static str, f64)>,
}

struct Grid {
    o: Vec3,
    h: f64,
    /// Points per axis (a multiple of B). Cells: 0..n-1.
    n: [usize; 3],
    nb: [usize; 3],
}

impl Grid {
    #[inline]
    fn key(&self, i: [usize; 3]) -> u64 {
        ((i[0] * self.n[1] + i[1]) * self.n[2] + i[2]) as u64
    }
    #[inline]
    fn unkey(&self, k: u64) -> [usize; 3] {
        let k = k as usize;
        let z = k % self.n[2];
        let r = k / self.n[2];
        [r / self.n[1], r % self.n[1], z]
    }
    #[inline]
    fn block(&self, i: [usize; 3]) -> usize {
        ((i[0] / B) * self.nb[1] + i[1] / B) * self.nb[2] + i[2] / B
    }
    #[inline]
    fn local(i: [usize; 3]) -> usize {
        ((i[0] % B) * B + i[1] % B) * B + i[2] % B
    }
    #[inline]
    fn in_range(&self, i: [i64; 3]) -> Option<[usize; 3]> {
        for k in 0..3 {
            if i[k] < 0 || i[k] >= self.n[k] as i64 {
                return None;
            }
        }
        Some([i[0] as usize, i[1] as usize, i[2] as usize])
    }
}

// ---- 1. Triangles to cells ------------------------------------------------

/// Cells that the triangle, dilated by `e`, can touch (conservative).
/// Scan lines in the plane of the two axes with the smallest normal part.
fn bin_triangle(g: &Grid, t: &[Vec3; 3], e: f64, out: &mut Vec<u64>) {
    let n = cross(sub(t[1], t[0]), sub(t[2], t[0]));
    let nn = norm(n);
    if !(nn > 1e-12) {
        return;
    }
    let w = (0..3).max_by(|&a, &b| n[a].abs().total_cmp(&n[b].abs())).unwrap();
    let (u, v) = ((w + 1) % 3, (w + 2) % 3);
    let lo = |ax: usize| t.iter().map(|p| p[ax]).fold(f64::INFINITY, f64::min);
    let hi = |ax: usize| t.iter().map(|p| p[ax]).fold(f64::NEG_INFINITY, f64::max);
    let (wlo, whi) = (lo(w) - e, hi(w) + e);
    // Thickness of the dilated plane along w.
    let pad = e * nn / n[w].abs();
    let lim = |ax: usize, x: f64| -> i64 { (x.floor() as i64).clamp(0, g.n[ax] as i64 - 2) };
    let j0 = lim(v, lo(v) - e);
    let j1 = lim(v, hi(v) + e);
    for j in j0..=j1 {
        let (y0, y1) = (j as f64 - e, j as f64 + 1.0 + e);
        let mut umin = f64::INFINITY;
        let mut umax = f64::NEG_INFINITY;
        for k in 0..3 {
            let (p, q) = (t[k], t[(k + 1) % 3]);
            if p[v] >= y0 && p[v] <= y1 {
                umin = umin.min(p[u]);
                umax = umax.max(p[u]);
            }
            for y in [y0, y1] {
                let (a, b) = (p[v].min(q[v]), p[v].max(q[v]));
                if a <= y && y <= b && b > a {
                    let s = (y - p[v]) / (q[v] - p[v]);
                    let x = p[u] + s * (q[u] - p[u]);
                    umin = umin.min(x);
                    umax = umax.max(x);
                }
            }
        }
        if umin > umax {
            continue;
        }
        let i0 = lim(u, umin - e);
        let i1 = lim(u, umax + e);
        for i in i0..=i1 {
            let (x0, x1) = (i as f64 - e, i as f64 + 1.0 + e);
            let mut zmin = f64::INFINITY;
            let mut zmax = f64::NEG_INFINITY;
            for x in [x0, x1] {
                for y in [y0, y1] {
                    let z = t[0][w] - (n[u] * (x - t[0][u]) + n[v] * (y - t[0][v])) / n[w];
                    zmin = zmin.min(z);
                    zmax = zmax.max(z);
                }
            }
            let zmin = (zmin - pad).max(wlo);
            let zmax = (zmax + pad).min(whi);
            if zmin > zmax {
                continue;
            }
            for k in lim(w, zmin)..=lim(w, zmax) {
                let mut c = [0usize; 3];
                c[u] = i as usize;
                c[v] = j as usize;
                c[w] = k as usize;
                out.push(g.key(c));
            }
        }
    }
}

// ---- 2. Segment against a dilated triangle --------------------------------

/// Closest distance² from p (in the plane of the triangle) to the triangle.
fn dist2_in_plane(p: Vec3, t: &[Vec3; 3], n: Vec3) -> f64 {
    let mut inside = true;
    for k in 0..3 {
        let (a, b) = (t[k], t[(k + 1) % 3]);
        if dot(cross(sub(b, a), sub(p, a)), n) < 0.0 {
            inside = false;
        }
    }
    if inside {
        return 0.0;
    }
    let mut best = f64::INFINITY;
    for k in 0..3 {
        let (a, b) = (t[k], t[(k + 1) % 3]);
        let ab = sub(b, a);
        let l2 = dot(ab, ab);
        let s = if l2 > 0.0 { (dot(sub(p, a), ab) / l2).clamp(0.0, 1.0) } else { 0.0 };
        best = best.min(dist2(p, add(a, scale(ab, s))));
    }
    best
}

/// Parameter t in [0, 1] where the segment a + t·d crosses the plane of the
/// triangle at a point less than `e` from the triangle.
#[inline]
fn seg_hit(a: Vec3, d: Vec3, t: &[Vec3; 3], e: f64) -> Option<f64> {
    let n = cross(sub(t[1], t[0]), sub(t[2], t[0]));
    let nn = norm(n);
    if !(nn > 1e-12) {
        return None;
    }
    let den = dot(n, d);
    if den.abs() <= 1e-12 * nn * norm(d) {
        return None;
    }
    let s = dot(n, sub(t[0], a)) / den;
    if !(-1e-12..=1.0 + 1e-12).contains(&s) {
        return None;
    }
    let s = s.clamp(0.0, 1.0);
    let p = add(a, scale(d, s));
    if dist2_in_plane(p, t, n) <= e * e {
        Some(s)
    } else {
        None
    }
}

// ---- The volume grid ------------------------------------------------------

struct Vol {
    g: Grid,
    /// Triangles in grid units.
    tg: Vec<[Vec3; 3]>,
    e: f64,
    /// Sorted surface cells, and their triangles: tri[start[i]..start[i+1]].
    cells: Vec<u64>,
    start: Vec<u32>,
    tri: Vec<u32>,
    /// Block -> slot of a surface block, or u32::MAX for an empty block.
    slot: Vec<u32>,
    masks: Vec<[u8; BB]>,
    out_bits: Vec<[u64; BB / 64]>,
    out_empty: Vec<bool>,
}

impl Vol {
    fn cell_tris(&self, c: u64) -> &[u32] {
        match self.cells.binary_search(&c) {
            Ok(i) => &self.tri[self.start[i] as usize..self.start[i + 1] as usize],
            Err(_) => &[],
        }
    }

    #[inline]
    fn is_out(&self, v: [usize; 3]) -> bool {
        let b = self.g.block(v);
        let s = self.slot[b];
        if s == u32::MAX {
            self.out_empty[b]
        } else {
            let l = Grid::local(v);
            (self.out_bits[s as usize][l / 64] >> (l % 64)) & 1 == 1
        }
    }

    /// All hits on the edge from point c in direction dir: (t, triangle).
    fn hits(&self, c: [usize; 3], dir: usize) -> Vec<(f64, u32)> {
        let a = [c[0] as f64, c[1] as f64, c[2] as f64];
        let d = DIRS[dir];
        let dv = [d[0] as f64, d[1] as f64, d[2] as f64];
        self.cell_tris(self.g.key(c))
            .iter()
            .filter_map(|&f| seg_hit(a, dv, &self.tg[f as usize], self.e).map(|s| (s, f)))
            .collect()
    }
}

const BLOCK_TAG: u64 = 1 << 63;

fn flood(vol: &mut Vol) {
    let g = &vol.g;
    let nblocks = g.nb[0] * g.nb[1] * g.nb[2];
    let mut out_empty = vec![false; nblocks];
    let mut out_bits = vec![[0u64; BB / 64]; vol.masks.len()];
    let mut stack: Vec<u64> = Vec::new();
    // Block 0 is empty (the grid has one block of space around the model).
    debug_assert_eq!(vol.slot[0], u32::MAX);
    out_empty[0] = true;
    stack.push(BLOCK_TAG);
    let slot = &vol.slot;
    let masks = &vol.masks;
    let blocked = |c: [usize; 3], dir: usize| -> bool {
        let s = slot[g.block(c)];
        s != u32::MAX && (masks[s as usize][Grid::local(c)] >> dir) & 1 == 1
    };
    let bdims = [g.nb[0] as i64, g.nb[1] as i64, g.nb[2] as i64];
    let bkey = |b: [i64; 3]| -> usize { ((b[0] * bdims[1] + b[1]) * bdims[2] + b[2]) as usize };
    let bunkey = |k: usize| -> [i64; 3] {
        let z = k % g.nb[2];
        let r = k / g.nb[2];
        [(r / g.nb[1]) as i64, (r % g.nb[1]) as i64, z as i64]
    };
    // Visit point w: mark it outside.
    macro_rules! visit {
        ($w:expr) => {{
            let w: [usize; 3] = $w;
            let b = g.block(w);
            let s = slot[b];
            if s == u32::MAX {
                if !out_empty[b] {
                    out_empty[b] = true;
                    stack.push(BLOCK_TAG | b as u64);
                }
            } else {
                let l = Grid::local(w);
                let bits = &mut out_bits[s as usize][l / 64];
                if (*bits >> (l % 64)) & 1 == 0 {
                    *bits |= 1 << (l % 64);
                    stack.push(g.key(w));
                }
            }
        }};
    }
    while let Some(item) = stack.pop() {
        if item & BLOCK_TAG != 0 {
            let bk = (item & !BLOCK_TAG) as usize;
            let b = bunkey(bk);
            for ox in -1i64..=1 {
                for oy in -1i64..=1 {
                    for oz in -1i64..=1 {
                        let o = [ox, oy, oz];
                        if o == [0, 0, 0] {
                            continue;
                        }
                        // An edge joins two blocks only when all non-zero
                        // offsets have the same sign.
                        let pos = o.iter().any(|&x| x > 0);
                        let neg = o.iter().any(|&x| x < 0);
                        if pos && neg {
                            continue;
                        }
                        let nbk = [b[0] + o[0], b[1] + o[1], b[2] + o[2]];
                        if (0..3).any(|k| nbk[k] < 0 || nbk[k] >= bdims[k]) {
                            continue;
                        }
                        let nk = bkey(nbk);
                        if slot[nk] == u32::MAX {
                            if !out_empty[nk] {
                                out_empty[nk] = true;
                                stack.push(BLOCK_TAG | nk as u64);
                            }
                            continue;
                        }
                        // Surface block: the point pairs across the shared
                        // face, edge or corner.
                        let sg: i64 = if pos { 1 } else { -1 };
                        let range = |k: usize| -> (usize, usize) {
                            match o[k] {
                                1 => (B - 1, B),
                                -1 => (0, 1),
                                _ => (0, B),
                            }
                        };
                        let (rx, ry, rz) = (range(0), range(1), range(2));
                        for (di, d) in DIRS.iter().enumerate() {
                            if (0..3).any(|k| o[k] != 0 && d[k] == 0) {
                                continue;
                            }
                            for lx in rx.0..rx.1 {
                                for ly in ry.0..ry.1 {
                                    for lz in rz.0..rz.1 {
                                        let v = [
                                            b[0] * B as i64 + lx as i64,
                                            b[1] * B as i64 + ly as i64,
                                            b[2] * B as i64 + lz as i64,
                                        ];
                                        let w = [v[0] + sg * d[0], v[1] + sg * d[1], v[2] + sg * d[2]];
                                        let Some(wu) = g.in_range(w) else { continue };
                                        if g.block(wu) != nk {
                                            continue;
                                        }
                                        if sg < 0 && blocked(wu, di) {
                                            continue;
                                        }
                                        visit!(wu);
                                    }
                                }
                            }
                        }
                    }
                }
            }
        } else {
            let v = g.unkey(item);
            let vi = [v[0] as i64, v[1] as i64, v[2] as i64];
            for (di, d) in DIRS.iter().enumerate() {
                if let Some(w) = g.in_range([vi[0] + d[0], vi[1] + d[1], vi[2] + d[2]]) {
                    if !blocked(v, di) {
                        visit!(w);
                    }
                }
                if let Some(w) = g.in_range([vi[0] - d[0], vi[1] - d[1], vi[2] - d[2]]) {
                    if !blocked(w, di) {
                        visit!(w);
                    }
                }
            }
        }
    }
    vol.out_empty = out_empty;
    vol.out_bits = out_bits;
}

/// The 6 Kuhn tetrahedra of a cube: axis order of the path from the
/// lowest corner to the highest corner.
const PERMS: [[usize; 3]; 6] = [[0, 1, 2], [0, 2, 1], [1, 0, 2], [1, 2, 0], [2, 0, 1], [2, 1, 0]];

/// Marching tetrahedra in one cell: faces as triples of edge ids
/// (point key · 8 + dir).
fn mt_cell(vol: &Vol, ck: u64, out: &mut Vec<[u64; 3]>) {
    let g = &vol.g;
    {
        {
            let c = g.unkey(ck);
            if (0..3).any(|k| c[k] + 1 >= g.n[k]) {
                return;
            }
            let mut lab = [false; 8];
            for (i, l) in lab.iter_mut().enumerate() {
                *l = vol.is_out([c[0] + (i >> 2 & 1), c[1] + (i >> 1 & 1), c[2] + (i & 1)]);
            }
            if lab.iter().all(|&x| x) || lab.iter().all(|&x| !x) {
                return;
            }
            for p in PERMS {
                let mut vs = [[0usize; 3]; 4];
                vs[0] = c;
                for s in 0..3 {
                    vs[s + 1] = vs[s];
                    vs[s + 1][p[s]] += 1;
                }
                let o: [bool; 4] = std::array::from_fn(|i| vol.is_out(vs[i]));
                let nin = o.iter().filter(|&&x| !x).count();
                if nin == 0 || nin == 4 {
                    continue;
                }
                let eid = |i: usize, j: usize| -> u64 {
                    let (i, j) = (i.min(j), i.max(j));
                    let d = [
                        vs[j][0] as i64 - vs[i][0] as i64,
                        vs[j][1] as i64 - vs[i][1] as i64,
                        vs[j][2] as i64 - vs[i][2] as i64,
                    ];
                    g.key(vs[i]) * 8 + dir_index(d) as u64
                };
                let mid = |i: usize, j: usize| -> Vec3 {
                    let f = |k: usize| (vs[i][k] + vs[j][k]) as f64 * 0.5;
                    [f(0), f(1), f(2)]
                };
                let pt = |i: usize| -> Vec3 { [vs[i][0] as f64, vs[i][1] as f64, vs[i][2] as f64] };
                // Direction from the inside points to the outside points.
                let mut r = [0.0; 3];
                for i in 0..4 {
                    let s = if o[i] { 1.0 / (4 - nin) as f64 } else { -1.0 / nin as f64 };
                    r = add(r, scale(pt(i), s));
                }
                let mut emit = |e: [(usize, usize); 3]| {
                    let m: Vec<Vec3> = e.iter().map(|&(i, j)| mid(i, j)).collect();
                    let n = cross(sub(m[1], m[0]), sub(m[2], m[0]));
                    let ids = [eid(e[0].0, e[0].1), eid(e[1].0, e[1].1), eid(e[2].0, e[2].1)];
                    if dot(n, r) >= 0.0 {
                        out.push(ids);
                    } else {
                        out.push([ids[0], ids[2], ids[1]]);
                    }
                };
                if nin == 1 || nin == 3 {
                    let odd = (0..4).find(|&i| o[i] == (nin == 3)).unwrap();
                    let others: Vec<usize> = (0..4).filter(|&i| i != odd).collect();
                    emit([(odd, others[0]), (odd, others[1]), (odd, others[2])]);
                } else {
                    let ins: Vec<usize> = (0..4).filter(|&i| !o[i]).collect();
                    let outs: Vec<usize> = (0..4).filter(|&i| o[i]).collect();
                    let (a, b, c2, d) = (ins[0], ins[1], outs[0], outs[1]);
                    // Quad ac, ad, bd, bc.
                    emit([(a, c2), (a, d), (b, d)]);
                    emit([(a, c2), (b, d), (b, c2)]);
                }
            }
        }
    }
}

/// Volumetric remesh of `m` (normalized units). Returns None when the
/// model is empty or the remesh gives no faces.
pub fn remesh(m: &Mesh, opts: &VolumeOptions) -> Option<(Mesh, VolumeReport)> {
    let mut rep = VolumeReport::default();
    let t0 = Instant::now();
    let lap = |rep: &mut VolumeReport, name: &'static str| {
        rep.times.push((name, t0.elapsed().as_secs_f64()));
    };
    let (lo, hi) = m.bbox()?;
    let ext = (0..3).map(|k| hi[k] - lo[k]).fold(0.0f64, f64::max);
    if !(ext > 0.0) || m.triangles.is_empty() {
        return None;
    }
    // Cell size: `cells` on the largest extent, but not more than about
    // `max_cells` surface cells (memory and time on a model with much area).
    let area: f64 = (0..m.triangles.len())
        .map(|f| {
            let [a, b, c] = m.tri_pos(f);
            tri_area(a, b, c)
        })
        .sum();
    let h = (ext / opts.cells.max(8) as f64).max((area / opts.max_cells).sqrt());
    // An odd shift, so that grid points are almost never on a model face.
    let shift = [0.3719, 0.2753, 0.1931];
    let o: Vec3 = std::array::from_fn(|k| lo[k] - (B as f64 + shift[k]) * h);
    let n: [usize; 3] = std::array::from_fn(|k| {
        let cells = ((hi[k] - o[k]) / h).ceil() as usize + B + 1;
        cells.div_ceil(B) * B
    });
    let g = Grid { o, h, n, nb: [n[0] / B, n[1] / B, n[2] / B] };
    rep.h = h;
    rep.dims = n;
    let e = opts.delta;
    let tg: Vec<[Vec3; 3]> = (0..m.triangles.len())
        .map(|f| {
            let p = m.tri_pos(f);
            std::array::from_fn(|i| std::array::from_fn(|k| (p[i][k] - o[k]) / h))
        })
        .collect();

    // 1. Cells of each triangle.
    let mut pairs: Vec<(u64, u32)> = tg
        .par_iter()
        .enumerate()
        .flat_map_iter(|(f, t)| {
            let mut keys = Vec::new();
            bin_triangle(&g, t, e + 1e-6, &mut keys);
            keys.sort_unstable();
            keys.dedup();
            keys.into_iter().map(move |k| (k, f as u32))
        })
        .collect();
    pairs.par_sort_unstable();
    let mut cells: Vec<u64> = Vec::new();
    let mut start: Vec<u32> = Vec::new();
    let mut tri: Vec<u32> = Vec::with_capacity(pairs.len());
    for (i, &(k, f)) in pairs.iter().enumerate() {
        if i == 0 || pairs[i - 1].0 != k {
            cells.push(k);
            start.push(i as u32);
        }
        tri.push(f);
    }
    start.push(pairs.len() as u32);
    drop(pairs);
    rep.surface_cells = cells.len();
    lap(&mut rep, "bin");

    // 2. Blocked edges of each surface cell.
    let mut vol = Vol {
        g,
        tg,
        e,
        cells,
        start,
        tri,
        slot: Vec::new(),
        masks: Vec::new(),
        out_bits: Vec::new(),
        out_empty: Vec::new(),
    };
    let cell_masks: Vec<u8> = (0..vol.cells.len())
        .into_par_iter()
        .map(|i| {
            let c = vol.g.unkey(vol.cells[i]);
            let a = [c[0] as f64, c[1] as f64, c[2] as f64];
            let ts = &vol.tri[vol.start[i] as usize..vol.start[i + 1] as usize];
            let mut mask = 0u8;
            for (di, d) in DIRS.iter().enumerate() {
                let dv = [d[0] as f64, d[1] as f64, d[2] as f64];
                if ts.iter().any(|&f| seg_hit(a, dv, &vol.tg[f as usize], e).is_some()) {
                    mask |= 1 << di;
                }
            }
            mask
        })
        .collect();
    let nblocks = vol.g.nb[0] * vol.g.nb[1] * vol.g.nb[2];
    vol.slot = vec![u32::MAX; nblocks];
    for (i, &ck) in vol.cells.iter().enumerate() {
        if cell_masks[i] == 0 {
            continue;
        }
        let c = vol.g.unkey(ck);
        let b = vol.g.block(c);
        if vol.slot[b] == u32::MAX {
            vol.slot[b] = vol.masks.len() as u32;
            vol.masks.push([0u8; BB]);
        }
        vol.masks[vol.slot[b] as usize][Grid::local(c)] = cell_masks[i];
    }
    rep.surface_blocks = vol.masks.len();
    lap(&mut rep, "edges");

    // 3. Outside by flood fill.
    flood(&mut vol);
    lap(&mut rep, "flood");

    // 4. Marching tetrahedra and a first decimation, chunk by chunk (in
    // parallel, little memory). Vertices on a chunk border are locked.
    let cs = (opts.chunk.round() as usize).max(8);
    let ck_of = |c: [usize; 3]| -> u64 {
        (((c[0] / cs) as u64) << 42) | (((c[1] / cs) as u64) << 21) | (c[2] / cs) as u64
    };
    let mut order: Vec<(u64, u32)> = vol
        .cells
        .par_iter()
        .enumerate()
        .map(|(i, &k)| (ck_of(vol.g.unkey(k)), i as u32))
        .collect();
    order.par_sort_unstable();
    let mut groups: Vec<(usize, usize)> = Vec::new();
    let mut s0 = 0;
    for i in 1..=order.len() {
        if i == order.len() || order[i].0 != order[s0].0 {
            groups.push((s0, i));
            s0 = i;
        }
    }
    let tol2 = (opts.decimate_tol * h).powi(2);
    let max_move = 3.0 * h + opts.decimate_tol * h;
    let do_dec = opts.decimate;
    struct Piece {
        v: Vec<Vec3>,
        f: Vec<[u32; 3]>,
        q: Vec<crate::decimate::Quad>,
        /// Edge id of a locked vertex, u64::MAX for a free one.
        lock: Vec<u64>,
        mt: usize,
        missing: usize,
    }
    let volr = &vol;
    let pieces: Vec<Piece> = groups
        .par_iter()
        .map(|&(a, b)| {
            let mut faces: Vec<[u64; 3]> = Vec::new();
            for &(_, i) in &order[a..b] {
                mt_cell(volr, volr.cells[i as usize], &mut faces);
            }
            let mut ids: Vec<u64> = faces.iter().flat_map(|f| f.iter().copied()).collect();
            ids.sort_unstable();
            ids.dedup();
            let mut missing = 0;
            let mut v: Vec<Vec3> = Vec::with_capacity(ids.len());
            let mut planes: Vec<Option<Vec3>> = Vec::with_capacity(ids.len());
            let mut locked: Vec<bool> = Vec::with_capacity(ids.len());
            for &id in &ids {
                let c = volr.g.unkey(id / 8);
                let di = (id % 8) as usize;
                let d = DIRS[di];
                let hs = volr.hits(c, di);
                let pick = if volr.is_out(c) {
                    hs.iter().copied().min_by(|x, y| x.0.total_cmp(&y.0))
                } else {
                    hs.iter().copied().max_by(|x, y| x.0.total_cmp(&y.0))
                };
                let (s, plane) = match pick {
                    Some((s, f)) => {
                        let t = &volr.tg[f as usize];
                        (s, Some(cross(sub(t[1], t[0]), sub(t[2], t[0]))))
                    }
                    None => {
                        missing += 1;
                        (0.5, None)
                    }
                };
                v.push(std::array::from_fn(|k| {
                    volr.g.o[k] + (c[k] as f64 + s * d[k] as f64) * volr.g.h
                }));
                planes.push(plane);
                locked.push((0..3).any(|k| d[k] == 0 && c[k] % cs == 0));
            }
            let mut f: Vec<[u32; 3]> = faces
                .iter()
                .map(|t| std::array::from_fn(|i| ids.binary_search(&t[i]).unwrap() as u32))
                .collect();
            let mt = f.len();
            let mut q = crate::decimate::plane_quads(&v, &f, &planes);
            let old: Vec<u32> = if do_dec {
                crate::decimate::decimate_piece(&mut v, &mut f, &mut q, &locked, tol2, max_move).0
            } else {
                (0..v.len() as u32).collect()
            };
            let lock = old.iter().map(|&o| if locked[o as usize] { ids[o as usize] } else { u64::MAX }).collect();
            Piece { v, f, q, lock, mt, missing }
        })
        .collect();
    drop(order);
    rep.mt_faces = pieces.iter().map(|p| p.mt).sum();
    rep.missing_hits = pieces.iter().map(|p| p.missing).sum();
    lap(&mut rep, "extract");
    // Join the pieces: a locked vertex is one vertex for all its pieces.
    let mut verts: Vec<Vec3> = Vec::new();
    let mut quads: Vec<crate::decimate::Quad> = Vec::new();
    let mut tris: Vec<[u32; 3]> = Vec::new();
    let mut shared: std::collections::HashMap<u64, u32> = std::collections::HashMap::new();
    for p in pieces {
        let map: Vec<u32> = (0..p.v.len())
            .map(|i| {
                if p.lock[i] != u64::MAX {
                    if let Some(&g) = shared.get(&p.lock[i]) {
                        quads[g as usize] = quads[g as usize].add(&p.q[i]);
                        return g;
                    }
                    shared.insert(p.lock[i], verts.len() as u32);
                }
                verts.push(p.v[i]);
                quads.push(p.q[i]);
                verts.len() as u32 - 1
            })
            .collect();
        tris.extend(p.f.iter().map(|t| [map[t[0] as usize], map[t[1] as usize], map[t[2] as usize]]));
    }
    if tris.is_empty() {
        return None;
    }
    lap(&mut rep, "join");
    // 5. More decimation passes: shifted chunks, then all.
    if opts.decimate {
        crate::decimate::finish(&mut verts, &mut tris, &mut quads, opts.chunk * h, tol2, max_move);
        lap(&mut rep, "decimate");
    }
    let mut out = Mesh::new(verts, tris);
    rep.dropped_shells = drop_small_shells(&mut out, opts.min_shell * h * h * h);
    rep.faces = out.triangles.len();
    Some((out, rep))
}

/// Remove each part (faces that share a vertex) with a volume below
/// `min_vol` or below zero. Such parts are small bubbles of the grid (a
/// grid point closed in between near faces), not parts of the model.
pub fn drop_small_shells(m: &mut Mesh, min_vol: f64) -> usize {
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
    let roots: Vec<u32> = m.triangles.iter().map(|t| find(&mut p, t[0])).collect();
    for (t, r) in m.triangles.iter().zip(&roots) {
        let [a, b, c] = [m.vertices[t[0] as usize], m.vertices[t[1] as usize], m.vertices[t[2] as usize]];
        *vol.entry(*r).or_default() += det3(a, b, c) / 6.0;
    }
    let bad: std::collections::HashSet<u32> =
        vol.iter().filter(|(_, &v)| !(v >= min_vol)).map(|(&r, _)| r).collect();
    if bad.is_empty() {
        return 0;
    }
    let keep: Vec<bool> = roots.iter().map(|r| !bad.contains(r)).collect();
    m.retain_faces(&keep);
    bad.len()
}

/// Last cleanup of the remesh: each face with an area below the limit of
/// the program's check goes away by a half-edge collapse (link condition,
/// no other face turns over or gets a zero area). The mesh stays closed.
/// Returns the number of collapses.
pub fn fix_zero_area(m: &mut Mesh) -> usize {
    let mut total = 0;
    for _ in 0..8 {
        let limit = crate::stages::zero_area_limit(m);
        let area = |m: &Mesh, t: [u32; 3]| -> f64 {
            tri_area(m.vertices[t[0] as usize], m.vertices[t[1] as usize], m.vertices[t[2] as usize])
        };
        let bad: Vec<usize> =
            (0..m.triangles.len()).filter(|&f| area(m, m.triangles[f]) < limit).collect();
        if bad.is_empty() {
            break;
        }
        let nv = m.vertices.len();
        let mut vf: Vec<Vec<u32>> = vec![Vec::new(); nv];
        for (f, t) in m.triangles.iter().enumerate() {
            for &w in t {
                vf[w as usize].push(f as u32);
            }
        }
        let ring = |vf: &Vec<Vec<u32>>, m: &Mesh, x: u32| -> Vec<u32> {
            let mut r: Vec<u32> = vf[x as usize]
                .iter()
                .flat_map(|&f| m.triangles[f as usize])
                .filter(|&w| w != x)
                .collect();
            r.sort_unstable();
            r.dedup();
            r
        };
        let mut locked = vec![false; nv];
        let mut remap: Vec<u32> = (0..nv as u32).collect();
        let mut done = 0;
        'face: for &f in &bad {
            let t = m.triangles[f];
            if t.iter().any(|&w| locked[w as usize]) {
                continue;
            }
            for i in 0..3 {
                for (a, b) in [(t[i], t[(i + 1) % 3]), (t[(i + 1) % 3], t[i])] {
                    // Collapse b into a (a keeps its place).
                    let (ra, rb) = (ring(&vf, m, a), ring(&vf, m, b));
                    let common = ra.iter().filter(|w| rb.binary_search(w).is_ok()).count();
                    let shared = vf[a as usize]
                        .iter()
                        .filter(|&&g| m.triangles[g as usize].contains(&b))
                        .count();
                    if common != 2 || shared != 2 || ra.len() + rb.len() < 8 {
                        continue;
                    }
                    let pa = m.vertices[a as usize];
                    let ok = vf[b as usize].iter().all(|&g| {
                        let u = m.triangles[g as usize];
                        if u.contains(&a) {
                            return true;
                        }
                        let old: [Vec3; 3] = std::array::from_fn(|k| m.vertices[u[k] as usize]);
                        let new: [Vec3; 3] = std::array::from_fn(|k| if u[k] == b { pa } else { old[k] });
                        let n0 = cross(sub(old[1], old[0]), sub(old[2], old[0]));
                        let n1 = cross(sub(new[1], new[0]), sub(new[2], new[0]));
                        0.5 * norm(n1) >= limit && (0.5 * norm(n0) < limit || dot(n0, n1) > 0.0)
                    });
                    if !ok {
                        continue;
                    }
                    remap[b as usize] = a;
                    for w in ra.iter().chain(rb.iter()).chain([a, b].iter()) {
                        locked[*w as usize] = true;
                    }
                    done += 1;
                    continue 'face;
                }
            }
        }
        if done == 0 {
            break;
        }
        m.apply_remap(&remap);
        total += done;
    }
    total
}
