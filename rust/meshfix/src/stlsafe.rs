//! StlSafe: the last step of the repair. It makes the result keep its
//! counts after a write to STL and a read.
//!
//! STL keeps float32 coordinates and no topology. A reader (VTK, OpenFOAM)
//! joins the points that are equal after the write. Two problems follow
//! (found in T30 on delta: 0/0/0/0 in memory, 28 non-manifold and 17
//! degenerate after the round trip):
//!
//! 1. Vertices nearer than the float32 step become one point. The copies of
//!    a pinched edge or vertex (one copy for each sheet, `split_nonmanifold`)
//!    have the same position, thus they always join again: the edge then has
//!    four faces. Needles (an edge shorter than the step) lose their face.
//! 2. A face with a very small height can become a face of zero area after
//!    the rounding (the program's limit: area < mean area · 1e-10).
//!
//! The step works on the final coordinates (after the scale back). Let M be
//! the largest |coordinate| and u = M · 2^-24. For each scale factor k of
//! the file (mm, m, inch...), float32 moves each coordinate by at most
//! ulp(k·x)/2 <= u·k. Thus two vertices whose largest coordinate difference
//! (Chebyshev distance) is more than 2u stay two points for each k. The step:
//!
//! - finds the vertex pairs at Chebyshev distance <= 2u. A pair on an edge
//!   is collapsed (link condition). Another pair is moved apart: one vertex
//!   moves by a few u (away from the other vertex, or along its normal, or
//!   to its ring centroid);
//! - finds the faces whose area after the rounding is below 10 times the
//!   program's limit, for the scale factors in `GRIDS`. Such a face gets a
//!   flip of its longest edge, or its apex moves away from the long edge;
//! - refuses each change that turns a face over, makes a face at risk, puts
//!   a vertex near another vertex, or makes a face cross another face;
//! - gives the mesh back unchanged when the result has more open,
//!   non-manifold or flipped edges than before, or is no longer clean.
//!
//! The moves are some u: about 1e-3 mm on a model of 3 m in mm.

use crate::math::*;
use crate::mesh::{directed, Grid, Mesh};
use crate::stages::{undo_crossing_ops, zero_area_limit, VertexFaces};
use std::collections::HashMap;

/// Scale factors from the repair coordinates to the file coordinates that
/// the face check tries. fs_meshfix gives mm: a file in mm (1), in m (1e-3)
/// and in inches (1/25.4).
pub const GRIDS: [f64; 3] = [1.0, 1e-3, 1.0 / 25.4];

/// A face is at risk when its area after the rounding is below this factor
/// times the program's zero-area limit.
const RISK: f64 = 10.0;

#[derive(Clone, Debug, Default)]
pub struct StlSafeReport {
    /// u = M · 2^-24, in the units of the mesh.
    pub unit: f64,
    /// Near vertex pairs and faces at risk before the step.
    pub pairs: usize,
    pub faces: usize,
    /// Changes kept.
    pub moved: usize,
    pub collapsed: usize,
    pub flipped: usize,
    /// Near pairs and faces at risk after the step.
    pub left_pairs: usize,
    pub left_faces: usize,
    /// Largest vertex move, in the units of the mesh.
    pub max_move: f64,
    /// Vertices that join at a float32 round trip (sum over GRIDS), before
    /// and after.
    pub joined_before: usize,
    pub joined_after: usize,
    /// The guard gave the mesh back unchanged.
    pub undone: bool,
    /// Rounds of the loop, and seconds of the scans and of the crossing
    /// checks (for diagnosis).
    pub rounds: usize,
    pub t_scan: f64,
    pub t_cross: f64,
}

impl StlSafeReport {
    pub fn changes(&self) -> usize {
        if self.undone {
            0
        } else {
            self.moved + self.collapsed + self.flipped
        }
    }
}

/// The position after a float32 round trip at the scale factor k.
#[inline]
fn rounded(p: Vec3, k: f64) -> Vec3 {
    [((p[0] * k) as f32) as f64 / k, ((p[1] * k) as f32) as f64 / k, ((p[2] * k) as f32) as f64 / k]
}

#[inline]
fn cheb(a: Vec3, b: Vec3) -> f64 {
    (a[0] - b[0]).abs().max((a[1] - b[1]).abs()).max((a[2] - b[2]).abs())
}

fn unit_of(m: &Mesh) -> f64 {
    let big = m.vertices.iter().flat_map(|p| p.iter().map(|x| x.abs())).fold(0.0f64, f64::max);
    big.max(1e-300) * 2f64.powi(-24)
}

/// True when the triangle keeps an area of at least `thr` at each grid.
fn safe_tri(p: [Vec3; 3], thr: f64, grids: &[f64]) -> bool {
    if tri_area(p[0], p[1], p[2]) < thr {
        return false;
    }
    grids.iter().all(|&k| {
        let q = [rounded(p[0], k), rounded(p[1], k), rounded(p[2], k)];
        tri_area(q[0], q[1], q[2]) >= thr
    })
}

fn used_vertices(m: &Mesh) -> Vec<bool> {
    let mut used = vec![false; m.vertices.len()];
    for t in &m.triangles {
        for &v in t {
            used[v as usize] = true;
        }
    }
    used
}

/// Pairs of used vertices at Chebyshev distance <= lim.
fn near_pairs(m: &Mesh, used: &[bool], lim: f64) -> (Vec<(u32, u32)>, Grid) {
    let mut grid = Grid::new(lim);
    for (i, &p) in m.vertices.iter().enumerate() {
        if used[i] {
            grid.insert(i as u32, p);
        }
    }
    let mut out = Vec::new();
    let mut near = Vec::new();
    for (i, &p) in m.vertices.iter().enumerate() {
        if !used[i] {
            continue;
        }
        near.clear();
        grid.near(p, &mut near);
        for &j in &near {
            if j as usize > i && cheb(p, m.vertices[j as usize]) <= lim {
                out.push((i as u32, j));
            }
        }
    }
    out.sort_unstable();
    (out, grid)
}

fn risky_faces(m: &Mesh, thr: f64, grids: &[f64]) -> Vec<usize> {
    use rayon::prelude::*;
    (0..m.triangles.len()).into_par_iter().filter(|&f| !safe_tri(m.tri_pos(f), thr, grids)).collect()
}

/// Vertices that join at a float32 round trip at the scale factor k.
pub fn joined_at(m: &Mesh, k: f64) -> usize {
    let used = used_vertices(m);
    let mut seen: HashMap<[u32; 3], ()> = HashMap::with_capacity(m.vertices.len());
    let mut n = 0;
    for (i, p) in m.vertices.iter().enumerate() {
        if !used[i] {
            continue;
        }
        let key = [((p[0] * k) as f32).to_bits(), ((p[1] * k) as f32).to_bits(), ((p[2] * k) as f32).to_bits()];
        if seen.insert(key, ()).is_some() {
            n += 1;
        }
    }
    n
}

/// The mesh as a reader gets it from an STL at the scale factor k: float32
/// positions (in the units of `m`), equal points joined. Faces stay (a face
/// with a repeated vertex stays, as in VTK).
pub fn round_trip(m: &Mesh, k: f64) -> Mesh {
    let mut ids: HashMap<[u32; 3], u32> = HashMap::with_capacity(m.vertices.len());
    let mut verts: Vec<Vec3> = Vec::with_capacity(m.vertices.len());
    let mut map = vec![u32::MAX; m.vertices.len()];
    let mut tris = Vec::with_capacity(m.triangles.len());
    for t in &m.triangles {
        let mut out = [0u32; 3];
        for (c, &v) in t.iter().enumerate() {
            if map[v as usize] == u32::MAX {
                let p = m.vertices[v as usize];
                let q = [(p[0] * k) as f32, (p[1] * k) as f32, (p[2] * k) as f32];
                let key = [q[0].to_bits(), q[1].to_bits(), q[2].to_bits()];
                let id = *ids.entry(key).or_insert_with(|| {
                    verts.push([q[0] as f64 / k, q[1] as f64 / k, q[2] as f64 / k]);
                    (verts.len() - 1) as u32
                });
                map[v as usize] = id;
            }
            out[c] = map[v as usize];
        }
        tris.push(out);
    }
    Mesh::new(verts, tris)
}

/// Area-weighted vertex normal (not unit), and the sum of the face areas·2.
fn vertex_normal(m: &Mesh, vf: &VertexFaces, v: u32) -> (Vec3, f64) {
    let mut n = [0.0; 3];
    let mut s = 0.0;
    for &f in vf.of(v) {
        let [a, b, c] = m.tri_pos(f as usize);
        let x = cross(sub(b, a), sub(c, a));
        n = add(n, x);
        s += norm(x);
    }
    (n, s)
}

fn unit(v: Vec3) -> Option<Vec3> {
    let l = norm(v);
    if l > 0.0 && l.is_finite() {
        Some(scale(v, 1.0 / l))
    } else {
        None
    }
}

struct Ctx<'a> {
    grids: &'a [f64],
    thr: f64,
    lim: f64,
}

impl Ctx<'_> {
    /// True when v can move to `to`: no face of v turns over or gets at
    /// risk, and no other used vertex is within `lim` of `to`.
    fn move_ok(&self, m: &Mesh, vf: &VertexFaces, grid: &Grid, v: u32, to: Vec3, near: &mut Vec<u32>) -> bool {
        for &f in vf.of(v) {
            let t = m.triangles[f as usize];
            let old = m.tri_pos(f as usize);
            let mut new = old;
            for k in 0..3 {
                if t[k] == v {
                    new[k] = to;
                }
            }
            let n0 = cross(sub(old[1], old[0]), sub(old[2], old[0]));
            let n1 = cross(sub(new[1], new[0]), sub(new[2], new[0]));
            if dot(n0, n1) <= 0.0 || !safe_tri(new, self.thr, self.grids) {
                return false;
            }
        }
        near.clear();
        grid.near(to, near);
        // The grid has the positions at the start of the round. The moved
        // vertices of this round are locked with their neighbours, so the
        // positions that the test reads are the true ones.
        near.iter().all(|&w| w == v || cheb(to, m.vertices[w as usize]) > self.lim)
    }

    /// True when the collapse of edge (a, b) to `to` keeps a manifold mesh
    /// and safe faces.
    fn collapse_ok(&self, m: &Mesh, vf: &VertexFaces, a: u32, b: u32, to: Vec3) -> bool {
        let faces = vf.edge(m, a, b);
        if faces.len() != 2 {
            return false;
        }
        let (ra, rb) = (vf.ring(m, a), vf.ring(m, b));
        let common = ra.iter().filter(|x| rb.binary_search(x).is_ok()).count();
        if common != 2 || (vf.on_rim(m, a) && vf.on_rim(m, b)) {
            return false;
        }
        for &v in &[a, b] {
            for &f in vf.of(v) {
                let t = m.triangles[f as usize];
                if t.contains(&a) && t.contains(&b) {
                    continue;
                }
                let old = m.tri_pos(f as usize);
                let mut new = old;
                for k in 0..3 {
                    if t[k] == a || t[k] == b {
                        new[k] = to;
                    }
                }
                let n0 = cross(sub(old[1], old[0]), sub(old[2], old[0]));
                let n1 = cross(sub(new[1], new[0]), sub(new[2], new[0]));
                if dot(n0, n1) <= 0.0 || !safe_tri(new, self.thr, self.grids) {
                    return false;
                }
            }
        }
        true
    }
}

/// Make the mesh safe for an STL round trip at the scale factors `grids`
/// (see the module text). The mesh stays as it is when it gets worse.
pub fn make_stl_safe(mesh: &mut Mesh, grids: &[f64]) -> StlSafeReport {
    let mut rep = StlSafeReport::default();
    if mesh.triangles.is_empty() {
        return rep;
    }
    let before = mesh.clone();
    let info0 = crate::mesh_info(mesh);
    let clean0 = crate::is_clean(mesh);
    let u = unit_of(mesh);
    rep.unit = u;
    let ctx = Ctx { grids, thr: zero_area_limit(mesh) * RISK, lim: 2.0 * u };
    rep.joined_before = grids.iter().map(|&k| joined_at(mesh, k)).sum();
    // Tries per pair / face (keys: vertex ids; no compaction in the loop).
    let mut tries: HashMap<[u32; 3], usize> = HashMap::new();
    const PAIR_TRIES: usize = 1 + 8 * 4;
    const FACE_DIRS: usize = 9;
    const FACE_TRIES: usize = 1 + FACE_DIRS * 4;
    let steps = [6.0, 12.0, 24.0, 48.0];
    let mut first = true;
    // A full scan in the first round and after a change of the topology;
    // else only the pairs and faces of the last round and the ones around
    // the vertices that moved.
    let mut full = true;
    let mut used: Vec<bool> = Vec::new();
    let mut grid = Grid::new(ctx.lim);
    let mut pairs: Vec<(u32, u32)> = Vec::new();
    let mut risky: Vec<usize> = Vec::new();
    for _round in 0..48 {
        rep.rounds += 1;
        let t0 = std::time::Instant::now();
        if full {
            used = used_vertices(mesh);
            (pairs, grid) = near_pairs(mesh, &used, ctx.lim);
            risky = risky_faces(mesh, ctx.thr, grids);
            full = false;
        }
        if first {
            rep.pairs = pairs.len();
            rep.faces = risky.len();
            first = false;
        }
        rep.t_scan += t0.elapsed().as_secs_f64();
        if pairs.is_empty() && risky.is_empty() {
            break;
        }
        let vf = VertexFaces::new(mesh);
        let nv = mesh.vertices.len();
        let mut locked = vec![false; nv];
        let mut near: Vec<u32> = Vec::new();
        let mut cols: Vec<(u32, u32, Vec3)> = Vec::new();
        let mut flips: Vec<(usize, usize, [u32; 3], [u32; 3])> = Vec::new();
        let mut moves: Vec<(u32, Vec3)> = Vec::new();
        let mut remap: Vec<u32> = (0..nv as u32).collect();
        let mut tried_any = false;

        let lock_around = |locked: &mut Vec<bool>, mesh: &Mesh, v: u32, near: &mut Vec<u32>| {
            locked[v as usize] = true;
            for &f in vf.of(v) {
                for &w in &mesh.triangles[f as usize] {
                    locked[w as usize] = true;
                }
            }
            near.clear();
            grid.near(mesh.vertices[v as usize], near);
            for &w in near.iter() {
                locked[w as usize] = true;
            }
        };

        for &(i, j) in &pairs {
            if locked[i as usize] || locked[j as usize] {
                continue;
            }
            let key = [i, j, u32::MAX];
            let start = *tries.get(&key).unwrap_or(&0);
            if start >= PAIR_TRIES {
                continue;
            }
            tried_any = true;
            let (pi, pj) = (mesh.vertices[i as usize], mesh.vertices[j as usize]);
            let mut done = PAIR_TRIES;
            for a in start..PAIR_TRIES {
                if a == 0 {
                    let mid = scale(add(pi, pj), 0.5);
                    if !vf.edge(mesh, i, j).is_empty() && ctx.collapse_ok(mesh, &vf, i, j, mid) {
                        cols.push((i, j, pi));
                        mesh.vertices[i as usize] = mid;
                        remap[j as usize] = i;
                        lock_around(&mut locked, mesh, i, &mut near);
                        lock_around(&mut locked, mesh, j, &mut near);
                        done = a + 1;
                        break;
                    }
                    continue;
                }
                let step = steps[(a - 1) / 8] * u;
                let (v, w) = if (a - 1) % 2 == 0 { (i, j) } else { (j, i) };
                let (pv, pw) = (mesh.vertices[v as usize], mesh.vertices[w as usize]);
                let (nrm, s) = vertex_normal(mesh, &vf, v);
                let dir = match ((a - 1) % 8) / 2 {
                    0 => unit(sub(pv, pw)),
                    1 => unit(nrm).filter(|_| norm(nrm) > 1e-3 * s).map(|n| scale(n, -1.0)),
                    2 => unit(nrm).filter(|_| norm(nrm) > 1e-3 * s),
                    _ => {
                        let ring = vf.ring(mesh, v);
                        if ring.is_empty() {
                            None
                        } else {
                            let mut c = [0.0; 3];
                            for &x in &ring {
                                c = add(c, mesh.vertices[x as usize]);
                            }
                            unit(sub(scale(c, 1.0 / ring.len() as f64), pv))
                        }
                    }
                };
                let Some(dir) = dir else { continue };
                let to = add(pv, scale(dir, step));
                if cheb(to, pw) <= 2.0 * ctx.lim {
                    continue;
                }
                if ctx.move_ok(mesh, &vf, &grid, v, to, &mut near) {
                    moves.push((v, pv));
                    mesh.vertices[v as usize] = to;
                    lock_around(&mut locked, mesh, v, &mut near);
                    lock_around(&mut locked, mesh, w, &mut near);
                    done = a + 1;
                    break;
                }
            }
            tries.insert(key, done);
        }

        for &f in &risky {
            let t = mesh.triangles[f];
            if t.iter().any(|&v| locked[v as usize]) {
                continue;
            }
            let mut key = t;
            key.sort_unstable();
            let start = *tries.get(&key).unwrap_or(&0);
            if start >= FACE_TRIES {
                continue;
            }
            tried_any = true;
            let p = mesh.tri_pos(f);
            let lens = [dist(p[0], p[1]), dist(p[1], p[2]), dist(p[2], p[0])];
            let e = (0..3).max_by(|&x, &y| lens[x].total_cmp(&lens[y])).unwrap();
            let (a, b, c) = (t[e], t[(e + 1) % 3], t[(e + 2) % 3]);
            let mut done = FACE_TRIES;
            for at in start..FACE_TRIES {
                if at == 0 {
                    // Flip the longest edge.
                    let adj = vf.edge(mesh, a, b);
                    if adj.len() != 2 {
                        continue;
                    }
                    let g = if adj[0] as usize == f { adj[1] } else { adj[0] } as usize;
                    let tg = mesh.triangles[g];
                    if !directed(&tg, b, a) || tg.iter().any(|&v| locked[v as usize]) {
                        continue;
                    }
                    let Some(d) = tg.iter().copied().find(|&x| x != a && x != b) else { continue };
                    if d == c || !vf.edge(mesh, c, d).is_empty() {
                        continue;
                    }
                    let (pa, pb, pc, pd) = (
                        mesh.vertices[a as usize],
                        mesh.vertices[b as usize],
                        mesh.vertices[c as usize],
                        mesh.vertices[d as usize],
                    );
                    let n1 = cross(sub(pd, pa), sub(pc, pa));
                    let n2 = cross(sub(pb, pd), sub(pc, pd));
                    let nf = cross(sub(pb, pa), sub(pc, pa));
                    let ng = cross(sub(pa, pb), sub(pd, pb));
                    let nsum = add(nf, ng);
                    if dot(n1, n2) <= 0.0 || dot(n1, nsum) <= 0.0 || dot(n2, nsum) <= 0.0 {
                        continue;
                    }
                    if !safe_tri([pa, pd, pc], ctx.thr, grids) || !safe_tri([pd, pb, pc], ctx.thr, grids) {
                        continue;
                    }
                    flips.push((f, g, mesh.triangles[f], mesh.triangles[g]));
                    mesh.triangles[f] = [a, d, c];
                    mesh.triangles[g] = [d, b, c];
                    for v in [a, b, c, d] {
                        lock_around(&mut locked, mesh, v, &mut near);
                    }
                    done = at + 1;
                    break;
                }
                let step = steps[(at - 1) / FACE_DIRS] * u;
                let (pa, pb, pc) = (mesh.vertices[a as usize], mesh.vertices[b as usize], mesh.vertices[c as usize]);
                // A move out of the plane of the face gives it a height, and
                // does not turn over the faces that lie almost in that plane
                // (also a face folded back onto it).
                let nf = unit(cross(sub(pb, pa), sub(pc, pa)));
                let (v, dir) = match (at - 1) % FACE_DIRS {
                    0 => {
                        // The apex, away from the line ab, in the plane.
                        let ab = sub(pb, pa);
                        let l2 = dot(ab, ab);
                        let d = if l2 <= 0.0 {
                            None
                        } else {
                            let foot = add(pa, scale(ab, dot(sub(pc, pa), ab) / l2));
                            unit(sub(pc, foot))
                        };
                        (c, d)
                    }
                    1 => (c, nf),
                    2 => (c, nf.map(|n| scale(n, -1.0))),
                    k @ (3 | 4) => {
                        let (nrm, s) = vertex_normal(mesh, &vf, c);
                        let sign = if k == 3 { -1.0 } else { 1.0 };
                        (c, unit(nrm).filter(|_| norm(nrm) > 1e-3 * s).map(|n| scale(n, sign)))
                    }
                    5 => (a, nf),
                    6 => (a, nf.map(|n| scale(n, -1.0))),
                    7 => (b, nf),
                    _ => (b, nf.map(|n| scale(n, -1.0))),
                };
                let Some(dir) = dir else { continue };
                let pv = mesh.vertices[v as usize];
                let to = add(pv, scale(dir, step));
                if ctx.move_ok(mesh, &vf, &grid, v, to, &mut near) {
                    moves.push((v, pv));
                    mesh.vertices[v as usize] = to;
                    lock_around(&mut locked, mesh, v, &mut near);
                    done = at + 1;
                    break;
                }
            }
            tries.insert(key, done);
        }

        if !tried_any {
            break;
        }
        // Changes that make a face cross another face go back.
        let t1 = std::time::Instant::now();
        undo_crossing_ops(mesh, &mut remap, &cols, &flips, &moves);
        rep.t_cross += t1.elapsed().as_secs_f64();
        let kept_cols = cols.iter().filter(|c| remap[c.1 as usize] == c.0).count();
        let kept_moves = moves.iter().filter(|&&(v, old)| mesh.vertices[v as usize] != old).count();
        let kept_flips = flips.iter().filter(|&&(f, _, tf, _)| mesh.triangles[f] != tf).count();
        rep.collapsed += kept_cols;
        rep.moved += kept_moves;
        rep.flipped += kept_flips;
        if kept_cols > 0 || kept_flips > 0 {
            for t in &mut mesh.triangles {
                for x in t.iter_mut() {
                    *x = remap[*x as usize];
                }
            }
            mesh.drop_collapsed();
            full = true;
            continue;
        }
        // Only moves: update the grid, then the lists for the next round.
        let t2 = std::time::Instant::now();
        let mut moved: Vec<u32> = Vec::new();
        for &(v, old) in &moves {
            let now = mesh.vertices[v as usize];
            if now != old {
                grid.remove(v, old);
                grid.insert(v, now);
                moved.push(v);
            }
        }
        let mut next: Vec<(u32, u32)> =
            pairs.iter().copied().filter(|&(i, j)| cheb(mesh.vertices[i as usize], mesh.vertices[j as usize]) <= ctx.lim).collect();
        let mut faces: Vec<usize> = risky.clone();
        for &v in &moved {
            near.clear();
            grid.near(mesh.vertices[v as usize], &mut near);
            for &w in &near {
                if w != v && used[w as usize] && cheb(mesh.vertices[v as usize], mesh.vertices[w as usize]) <= ctx.lim {
                    next.push((v.min(w), v.max(w)));
                }
            }
            faces.extend(vf.of(v).iter().map(|&f| f as usize));
        }
        next.sort_unstable();
        next.dedup();
        faces.sort_unstable();
        faces.dedup();
        faces.retain(|&f| !safe_tri(mesh.tri_pos(f), ctx.thr, grids));
        pairs = next;
        risky = faces;
        rep.t_scan += t2.elapsed().as_secs_f64();
    }
    let used = used_vertices(mesh);
    rep.max_move = (0..mesh.vertices.len())
        .filter(|&v| used[v])
        .map(|v| dist(mesh.vertices[v], before.vertices[v]))
        .fold(0.0, f64::max);
    rep.left_pairs = near_pairs(mesh, &used, ctx.lim).0.len();
    rep.left_faces = risky_faces(mesh, ctx.thr, grids).len();
    if rep.collapsed > 0 {
        mesh.compact();
    }
    rep.joined_after = grids.iter().map(|&k| joined_at(mesh, k)).sum();
    // Guard: the topology must not get worse.
    let info1 = crate::mesh_info(mesh);
    let worse = info1.boundary_edges > info0.boundary_edges
        || info1.non_manifold_edges > info0.non_manifold_edges
        || info1.flipped_edges > info0.flipped_edges
        || (clean0 && !crate::is_clean(mesh))
        || rep.joined_after > rep.joined_before;
    if worse {
        *mesh = before;
        rep.undone = true;
        rep.left_pairs = rep.pairs;
        rep.left_faces = rep.faces;
        rep.joined_after = rep.joined_before;
        rep.max_move = 0.0;
    }
    rep
}
