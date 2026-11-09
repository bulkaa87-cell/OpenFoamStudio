//! Union of closed shells that intersect (the "Wrap" behavior of the
//! Windows repair: two cubes that cut into each other become one body).
//!
//! Steps:
//! 1. Move each vertex by a very small vector (1e-6 units, direction from
//!    a hash of the position). This removes the exact coincidences (an
//!    edge through an edge, a vertex on a face) that are usual in CAD
//!    output. The moved original vertices go back at the end.
//! 2. Find the triangle pairs that cross (hash grid, then exact orient3d
//!    signs from the `robust` crate). A pair that is still degenerate after
//!    step 1 is skipped and counted.
//! 3. Each crossing point is "edge E crosses triangle T". It gets one id for
//!    each (E, T), so the faces on the two sides of E use the same point.
//! 4. Each cut triangle is triangulated again in its own plane with a
//!    constrained Delaunay triangulation (`spade`), with the cut segments
//!    as constraints.
//! 5. The cut curves are edges with 4 faces. They divide the surface into
//!    patches. A patch stays when the winding number of all other faces at
//!    a point on it is 1/2: outside on the front, inside on the back.

use crate::math::*;
use crate::mesh::*;
use robust::{orient3d, Coord3D};
use spade::{ConstrainedDelaunayTriangulation, Point2, Triangulation};
use std::collections::{HashMap, VecDeque};

#[derive(Clone, Debug, Default, PartialEq, Eq)]
pub struct UnionReport {
    /// Triangle pairs that cross.
    pub pairs: usize,
    /// Pairs skipped because they touch in a degenerate way.
    pub degenerate: usize,
    /// Triangles cut and triangulated again.
    pub cut_faces: usize,
    /// Patches removed (inside another shell).
    pub removed_patches: usize,
    /// Triangles that the CDT could not do (kept as they were).
    pub failed_faces: usize,
    /// The union stopped with a panic. The mesh is as before the union.
    pub panicked: bool,
    /// Face pairs that touch almost coplanar (not cut).
    pub contact: usize,
    /// The union gave new open or non-manifold edges, so it was not kept:
    /// the mesh is as before the union.
    pub rolled_back: bool,
    /// New open and non-manifold edges that caused the roll-back.
    pub bad_edges: usize,
    /// Cut clusters left uncut because their cut gave open or
    /// non-manifold edges.
    pub skipped_clusters: usize,
    /// Faces kept although outside or inside, because their removal opened
    /// an edge.
    pub kept_patches: usize,
}

#[inline]
fn c3(p: Vec3) -> Coord3D<f64> {
    Coord3D { x: p[0], y: p[1], z: p[2] }
}

/// Exact sign (and an approximate value) of the orientation of d against
/// the plane a, b, c.
#[inline]
fn o3(a: Vec3, b: Vec3, c: Vec3, d: Vec3) -> f64 {
    orient3d(c3(a), c3(b), c3(c), c3(d))
}

#[inline]
fn sgn(x: f64) -> i8 {
    if x > 0.0 {
        1
    } else if x < 0.0 {
        -1
    } else {
        0
    }
}

fn face_box(m: &Mesh, f: usize) -> (Vec3, Vec3) {
    let [a, b, c] = m.tri_pos(f);
    let mut lo = a;
    let mut hi = a;
    for p in [b, c] {
        for k in 0..3 {
            lo[k] = lo[k].min(p[k]);
            hi[k] = hi[k].max(p[k]);
        }
    }
    (lo, hi)
}

/// Pairs of faces whose boxes overlap. Each pair comes one time: from the
/// grid cell that holds the low corner of the box overlap.
pub(crate) fn candidate_pairs(m: &Mesh, faces: &[u32]) -> Vec<(u32, u32)> {
    if faces.len() < 2 {
        return Vec::new();
    }
    let boxes: Vec<(Vec3, Vec3)> = faces.iter().map(|&f| face_box(m, f as usize)).collect();
    let mut glo = [f64::INFINITY; 3];
    let mut ghi = [f64::NEG_INFINITY; 3];
    let mut mean = 0.0;
    for (lo, hi) in &boxes {
        for k in 0..3 {
            glo[k] = glo[k].min(lo[k]);
            ghi[k] = ghi[k].max(hi[k]);
        }
        mean += (0..3).map(|k| hi[k] - lo[k]).fold(0.0, f64::max);
    }
    mean /= boxes.len() as f64;
    let ext = (0..3).map(|k| ghi[k] - glo[k]).fold(0.0, f64::max);
    let cell = mean.max(ext / 1024.0).max(1e-12);
    let inv = 1.0 / cell;
    let key = |p: Vec3| -> [i64; 3] {
        [(p[0] * inv).floor() as i64, (p[1] * inv).floor() as i64, (p[2] * inv).floor() as i64]
    };
    let mut grid: HashMap<[i64; 3], Vec<u32>> = HashMap::new();
    for (i, (lo, hi)) in boxes.iter().enumerate() {
        let (a, b) = (key(*lo), key(*hi));
        for x in a[0]..=b[0] {
            for y in a[1]..=b[1] {
                for z in a[2]..=b[2] {
                    grid.entry([x, y, z]).or_default().push(i as u32);
                }
            }
        }
    }
    use rayon::prelude::*;
    let cells: Vec<(&[i64; 3], &Vec<u32>)> = grid.iter().filter(|(_, l)| l.len() > 1).collect();
    let mut out: Vec<(u32, u32)> = cells
        .par_iter()
        .flat_map_iter(|&(k, list)| {
            let mut local = Vec::new();
            for a in 0..list.len() {
                let (i, bi) = (list[a] as usize, boxes[list[a] as usize]);
                for &j in &list[a + 1..] {
                    let bj = boxes[j as usize];
                    if (0..3).any(|d| bi.0[d] > bj.1[d] || bj.0[d] > bi.1[d]) {
                        continue;
                    }
                    let corner = [bi.0[0].max(bj.0[0]), bi.0[1].max(bj.0[1]), bi.0[2].max(bj.0[2])];
                    if key(corner) != *k {
                        continue;
                    }
                    local.push((faces[i], faces[j as usize]));
                }
            }
            local
        })
        .collect();
    out.sort_unstable();
    out
}

/// One end of a cut segment: mesh edge (u, v) crosses triangle `tri`.
#[derive(Clone, Copy, Debug)]
pub(crate) struct Hit {
    pub(crate) u: u32,
    pub(crate) v: u32,
    pub(crate) tri: u32,
    pub(crate) ou: f64,
    pub(crate) ov: f64,
}

#[derive(Clone, Copy, Debug)]
pub(crate) enum PairResult {
    None,
    Degenerate,
    /// Almost coplanar faces (contact, not a cut): not cut.
    Contact,
    Segment([Hit; 2]),
    /// The faces share vertex `va` (in a) = `vb` (in b, the same point),
    /// and cross from there to the hit.
    FromVertex { va: u32, vb: u32, hit: Hit },
}

/// Edges of triangle `ta` (face fa) that cross triangle `tb` (face fb).
fn edge_hits(m: &Mesh, ta: [u32; 3], tb: [u32; 3], fb: u32, out: &mut Vec<Hit>) -> bool {
    let p = |i: u32| m.vertices[i as usize];
    let (b0, b1, b2) = (p(tb[0]), p(tb[1]), p(tb[2]));
    let o: [f64; 3] = [o3(b0, b1, b2, p(ta[0])), o3(b0, b1, b2, p(ta[1])), o3(b0, b1, b2, p(ta[2]))];
    for i in 0..3 {
        let (u, v) = (ta[i], ta[(i + 1) % 3]);
        let (su, sv) = (sgn(o[i]), sgn(o[(i + 1) % 3]));
        if su == sv || su == 0 || sv == 0 {
            continue;
        }
        let (pu, pv) = (p(u), p(v));
        let s = [sgn(o3(pu, pv, b0, b1)), sgn(o3(pu, pv, b1, b2)), sgn(o3(pu, pv, b2, b0))];
        let nz: Vec<i8> = s.iter().copied().filter(|&x| x != 0).collect();
        let agree = nz.iter().all(|&x| x == nz[0]);
        if !agree {
            continue; // the line misses the triangle
        }
        if nz.len() < 3 {
            return false; // through an edge or a vertex of tb
        }
        out.push(Hit { u, v, tri: fb, ou: o[i], ov: o[(i + 1) % 3] });
    }
    true
}

/// Two faces that share one vertex (by position): the face parts that cross
/// form a segment from that vertex to one edge hit.
fn test_shared_vertex(m: &Mesh, fa: u32, fb: u32, ia: usize, ib: usize) -> PairResult {
    let ta = m.triangles[fa as usize];
    let tb = m.triangles[fb as usize];
    let p = |i: u32| m.vertices[i as usize];
    let mut hits = Vec::with_capacity(2);
    for (t1, t2, f2, i1) in [(ta, tb, fb, ia), (tb, ta, fa, ib)] {
        // Only the edge opposite the shared vertex can cross the other face.
        let (u, v) = (t1[(i1 + 1) % 3], t1[(i1 + 2) % 3]);
        let (b0, b1, b2) = (p(t2[0]), p(t2[1]), p(t2[2]));
        let (ou, ov) = (o3(b0, b1, b2, p(u)), o3(b0, b1, b2, p(v)));
        let (su, sv) = (sgn(ou), sgn(ov));
        if su == 0 || sv == 0 {
            return PairResult::Degenerate;
        }
        if su == sv {
            continue;
        }
        let (pu, pv) = (p(u), p(v));
        let s = [sgn(o3(pu, pv, b0, b1)), sgn(o3(pu, pv, b1, b2)), sgn(o3(pu, pv, b2, b0))];
        if s.contains(&0) {
            return PairResult::Degenerate;
        }
        if s[0] == s[1] && s[1] == s[2] {
            hits.push(Hit { u, v, tri: f2, ou, ov });
        }
    }
    match hits.len() {
        0 => PairResult::None,
        1 => PairResult::FromVertex { va: ta[ia], vb: tb[ib], hit: hits[0] },
        _ => PairResult::Degenerate,
    }
}

/// Faces closer than this angle to parallel are a contact, not a cut.
pub const CONTACT_DEG: f64 = 0.5;
const CONTACT_COS: f64 = 0.999_961_923_064_171_7; // cos(0.5 deg)

pub(crate) fn test_pair(m: &Mesh, fa: u32, fb: u32) -> PairResult {
    test_pair_x(m, fa, fb, true)
}

/// test_pair; with `contact` false, almost coplanar faces are cut too.
pub(crate) fn test_pair_x(m: &Mesh, fa: u32, fb: u32, contact: bool) -> PairResult {
    let ta = m.triangles[fa as usize];
    let tb = m.triangles[fb as usize];
    let p = |i: u32| m.vertices[i as usize];
    // Shared vertices, by position: the copies made by the non-manifold
    // split have other ids but the same position.
    let mut shared = Vec::with_capacity(3);
    for i in 0..3 {
        for j in 0..3 {
            if ta[i] == tb[j] || p(ta[i]) == p(tb[j]) {
                shared.push((i, j));
            }
        }
    }
    match shared.len() {
        0 => {}
        1 => return test_shared_vertex(m, fa, fb, shared[0].0, shared[0].1),
        _ => return PairResult::None, // a shared edge: no crossing in general position
    }
    // Faces at less than CONTACT_DEG to each other: two bodies that touch
    // face to face (a spinner on a nose). A cut there gives only noise at
    // the size of the small move of step 1.
    let (na, nb) = (
        cross(sub(p(ta[1]), p(ta[0])), sub(p(ta[2]), p(ta[0]))),
        cross(sub(p(tb[1]), p(tb[0])), sub(p(tb[2]), p(tb[0]))),
    );
    let (la, lb) = (norm(na), norm(nb));
    // Only faces that face each other (opposite normals): two bodies that
    // touch. Faces with the same direction are cut: a cut curve that
    // becomes tangent must not stop there.
    if contact && la > 0.0 && lb > 0.0 && -dot(na, nb) > CONTACT_COS * la * lb {
        let (lo_a, hi_a) = face_box(m, fa as usize);
        let (lo_b, hi_b) = face_box(m, fb as usize);
        if (0..3).all(|k| lo_a[k] <= hi_b[k] && lo_b[k] <= hi_a[k]) {
            return PairResult::Contact;
        }
    }
    let (a0, a1, a2) = (p(ta[0]), p(ta[1]), p(ta[2]));
    let (b0, b1, b2) = (p(tb[0]), p(tb[1]), p(tb[2]));
    let sa = [sgn(o3(b0, b1, b2, a0)), sgn(o3(b0, b1, b2, a1)), sgn(o3(b0, b1, b2, a2))];
    if sa[0] == sa[1] && sa[1] == sa[2] && sa[0] != 0 {
        return PairResult::None;
    }
    let sb = [sgn(o3(a0, a1, a2, b0)), sgn(o3(a0, a1, a2, b1)), sgn(o3(a0, a1, a2, b2))];
    if sb[0] == sb[1] && sb[1] == sb[2] && sb[0] != 0 {
        return PairResult::None;
    }
    if sa.contains(&0) || sb.contains(&0) {
        return PairResult::Degenerate;
    }
    let mut hits = Vec::with_capacity(4);
    if !edge_hits(m, ta, tb, fb, &mut hits) || !edge_hits(m, tb, ta, fa, &mut hits) {
        return PairResult::Degenerate;
    }
    match hits.len() {
        0 => PairResult::None,
        2 => PairResult::Segment([hits[0], hits[1]]),
        _ => PairResult::Degenerate,
    }
}

#[derive(Default)]
struct Cuts {
    /// (edge, triangle) → vertex id of the crossing point.
    point: HashMap<(EdgeKey, u32), u32>,
    /// Points on a mesh edge, with t measured from key.0 to key.1.
    on_edge: HashMap<EdgeKey, Vec<(f64, u32)>>,
    /// Points inside a triangle.
    inside: HashMap<u32, Vec<u32>>,
    /// Cut segments of a triangle.
    segs: HashMap<u32, Vec<(u32, u32)>>,
}

impl Cuts {
    fn point_of(&mut self, m: &mut Mesh, h: Hit) -> u32 {
        let k = ekey(h.u, h.v);
        if let Some(&id) = self.point.get(&(k, h.tri)) {
            return id;
        }
        // t from k.0: the plane value goes from o(k.0) to o(k.1) through 0.
        let (o0, o1) = if h.u == k.0 { (h.ou, h.ov) } else { (h.ov, h.ou) };
        let t = (o0 / (o0 - o1)).clamp(0.0, 1.0);
        let (p0, p1) = (m.vertices[k.0 as usize], m.vertices[k.1 as usize]);
        let id = m.vertices.len() as u32;
        m.vertices.push(add(p0, scale(sub(p1, p0), t)));
        self.point.insert((k, h.tri), id);
        self.on_edge.entry(k).or_default().push((t, id));
        self.inside.entry(h.tri).or_default().push(id);
        id
    }
}

/// Triangulate face `t` again with its cut points and segments.
/// Returns the new faces, or None when the CDT fails.
fn retriangulate(m: &mut Mesh, t: [u32; 3], f: u32, cuts: &Cuts, aliases: &mut Vec<(u32, u32)>) -> Option<Vec<[u32; 3]>> {
    let c = [m.vertices[t[0] as usize], m.vertices[t[1] as usize], m.vertices[t[2] as usize]];
    let n = cross(sub(c[1], c[0]), sub(c[2], c[0]));
    let k = (0..3).max_by(|&x, &y| n[x].abs().total_cmp(&n[y].abs())).unwrap();
    if n[k] == 0.0 {
        return None;
    }
    let (ax, ay) = if n[k] > 0.0 { ((k + 1) % 3, (k + 2) % 3) } else { ((k + 2) % 3, (k + 1) % 3) };
    let proj = |p: Vec3| Point2::new(p[ax], p[ay]);

    let mut cdt: ConstrainedDelaunayTriangulation<Point2<f64>> = ConstrainedDelaunayTriangulation::new();
    let mut h2id: Vec<u32> = Vec::new();
    let mut handle = |cdt: &mut ConstrainedDelaunayTriangulation<Point2<f64>>, id: u32, h2id: &mut Vec<u32>, m: &Mesh| {
        let h = cdt.insert(proj(m.vertices[id as usize])).ok()?;
        let i = h.index();
        if i >= h2id.len() {
            h2id.resize(i + 1, u32::MAX);
        }
        if h2id[i] == u32::MAX {
            h2id[i] = id;
        } else if h2id[i] != id {
            aliases.push((id, h2id[i]));
        }
        Some(h)
    };

    // Boundary chains.
    let mut chains: Vec<Vec<spade::handles::FixedVertexHandle>> = Vec::new();
    for i in 0..3 {
        let (a, b) = (t[i], t[(i + 1) % 3]);
        let key = ekey(a, b);
        let mut pts: Vec<(f64, u32)> = cuts.on_edge.get(&key).cloned().unwrap_or_default();
        pts.sort_by(|x, y| x.0.total_cmp(&y.0));
        if a != key.0 {
            pts.reverse();
        }
        let mut chain = vec![handle(&mut cdt, a, &mut h2id, m)?];
        for (_, id) in pts {
            chain.push(handle(&mut cdt, id, &mut h2id, m)?);
        }
        chain.push(handle(&mut cdt, b, &mut h2id, m)?);
        chains.push(chain);
    }
    if let Some(ins) = cuts.inside.get(&f) {
        for &id in ins {
            handle(&mut cdt, id, &mut h2id, m)?;
        }
    }
    let mut seg_h = Vec::new();
    if let Some(segs) = cuts.segs.get(&f) {
        for &(p, q) in segs {
            let hp = handle(&mut cdt, p, &mut h2id, m)?;
            let hq = handle(&mut cdt, q, &mut h2id, m)?;
            seg_h.push((hp, hq));
        }
    }
    for chain in &chains {
        for w in chain.windows(2) {
            if w[0] != w[1] {
                cdt.add_constraint_and_split(w[0], w[1], |p| p);
            }
        }
    }
    for (hp, hq) in seg_h {
        if hp != hq {
            cdt.add_constraint_and_split(hp, hq, |p| p);
        }
    }

    // Vertices that the constraint split made: back to 3D in the plane of t.
    let (q0, q1, q2) = (proj(c[0]), proj(c[1]), proj(c[2]));
    let area = (q1.x - q0.x) * (q2.y - q0.y) - (q2.x - q0.x) * (q1.y - q0.y);
    for v in cdt.vertices() {
        let i = v.fix().index();
        if i < h2id.len() && h2id[i] != u32::MAX {
            continue;
        }
        let q = v.position();
        let l1 = ((q.x - q0.x) * (q2.y - q0.y) - (q2.x - q0.x) * (q.y - q0.y)) / area;
        let l2 = ((q1.x - q0.x) * (q.y - q0.y) - (q.x - q0.x) * (q1.y - q0.y)) / area;
        let l0 = 1.0 - l1 - l2;
        let p3 = add(add(scale(c[0], l0), scale(c[1], l1)), scale(c[2], l2));
        if i >= h2id.len() {
            h2id.resize(i + 1, u32::MAX);
        }
        h2id[i] = m.vertices.len() as u32;
        m.vertices.push(p3);
    }

    // Faces outside the boundary chains: reachable from the convex hull
    // without a crossing of a constraint edge.
    let faces: Vec<_> = cdt.inner_faces().collect();
    let mut pos: HashMap<usize, usize> = HashMap::with_capacity(faces.len());
    for (i, fh) in faces.iter().enumerate() {
        pos.insert(fh.fix().index(), i);
    }
    let mut outside = vec![false; faces.len()];
    let mut queue = VecDeque::new();
    for (i, fh) in faces.iter().enumerate() {
        for e in fh.adjacent_edges() {
            if e.rev().face().is_outer() && !e.is_constraint_edge() && !outside[i] {
                outside[i] = true;
                queue.push_back(i);
            }
        }
    }
    while let Some(i) = queue.pop_front() {
        for e in faces[i].adjacent_edges() {
            if e.is_constraint_edge() {
                continue;
            }
            if let Some(g) = e.rev().face().as_inner() {
                let j = pos[&g.fix().index()];
                if !outside[j] {
                    outside[j] = true;
                    queue.push_back(j);
                }
            }
        }
    }
    let mut out = Vec::with_capacity(faces.len());
    for (i, fh) in faces.iter().enumerate() {
        if outside[i] {
            continue;
        }
        let vs = fh.vertices();
        out.push([h2id[vs[0].fix().index()], h2id[vs[1].fix().index()], h2id[vs[2].fix().index()]]);
    }
    if out.is_empty() {
        return None;
    }
    Some(out)
}

/// Solid angle of triangle a, b, c seen from p (Van Oosterom-Strackee).
/// Zero when p is in the plane of the triangle.
pub(crate) fn solid_angle(p: Vec3, a: Vec3, b: Vec3, c: Vec3) -> f64 {
    let (a, b, c) = (sub(a, p), sub(b, p), sub(c, p));
    let (la, lb, lc) = (norm(a), norm(b), norm(c));
    let det = det3(a, b, c);
    if det.abs() <= 1e-12 * la * lb * lc {
        return 0.0;
    }
    let den = la * lb * lc + dot(a, b) * lc + dot(a, c) * lb + dot(b, c) * la;
    2.0 * det.atan2(den)
}

/// A fixed pseudo-random vector in [-1, 1]^3 from the position (splitmix64).
/// Copies of one vertex (made at a shared edge) get the same vector, so
/// they stay together and the two bodies keep touching, not crossing.
pub(crate) fn jitter(p: Vec3) -> Vec3 {
    let mut x = p[0].to_bits() ^ p[1].to_bits().rotate_left(21) ^ p[2].to_bits().rotate_left(42);
    let mut out = [0.0; 3];
    for o in &mut out {
        x = x.wrapping_add(0x9E37_79B9_7F4A_7C15);
        let mut z = x;
        z = (z ^ (z >> 30)).wrapping_mul(0xBF58_476D_1CE4_E5B9);
        z = (z ^ (z >> 27)).wrapping_mul(0x94D0_49BB_1331_11EB);
        z ^= z >> 31;
        *o = (z >> 11) as f64 / (1u64 << 52) as f64 - 1.0;
    }
    out
}

fn find(p: &mut [u32], mut x: u32) -> u32 {
    while p[x as usize] != x {
        let g = p[p[x as usize] as usize];
        p[x as usize] = g;
        x = g;
    }
    x
}

/// Pair tests do not change between rounds: they are done one time.
struct Prepared {
    shell_of: Vec<u32>,
    rank: u32,
    nv0: usize,
    orig: Vec<Vec3>,
    moved: Vec<Vec3>,
    /// (face a, face b, result) for the pairs that cross.
    crossing: Vec<(u32, u32, PairResult)>,
    degenerate: usize,
    contact: usize,
    /// Outside test by a flood fill (None when the box is empty).
    vox: Option<VoxelOutside>,
}

/// Voxel budget of the outside test: 400 voxels for each face, at most 1e8
/// (about 1.6 mm on a 1.5 m aircraft), at least 1e6.
pub fn voxel_budget(faces: usize) -> usize {
    (faces * 400).clamp(1_000_000, 100_000_000)
}

fn prepare(mesh: &Mesh) -> Option<Prepared> {
    let ef = mesh.edge_faces();
    let shells = components(mesh, &ef);
    let nf = mesh.triangles.len();
    // All shells take part, also open ones: the winding number of a
    // shell with a small gap is still near 0 outside and near 1 inside.
    let mut shell_of = vec![u32::MAX; nf];
    let mut rank = 0u32;
    for s in &shells {
        for &f in s {
            shell_of[f as usize] = rank;
        }
        rank += 1;
    }
    if rank == 0 {
        return None;
    }
    // 1. Small move of each vertex of a closed shell: 1e-6 units in a
    // direction from a hash of the position. This removes the exact
    // coincidences (a vertex on the plane of a face, an edge through an
    // edge) that are usual in CAD output.
    let nv0 = mesh.vertices.len();
    let orig = mesh.vertices.clone();
    let mut moved = orig.clone();
    let mut done = vec![false; nv0];
    for f in 0..nf {
        if shell_of[f] == u32::MAX {
            continue;
        }
        for &v in &mesh.triangles[f] {
            if !done[v as usize] {
                done[v as usize] = true;
                moved[v as usize] = add(orig[v as usize], scale(jitter(orig[v as usize]), 1e-6));
            }
        }
    }
    let jm = Mesh::new(moved.clone(), mesh.triangles.clone());
    // 2. Crossing pairs.
    let faces: Vec<u32> = (0..nf as u32).filter(|&f| shell_of[f as usize] != u32::MAX).collect();
    let t = std::time::Instant::now();
    let vox = VoxelOutside::new(&jm, &faces, voxel_budget(faces.len()));
    dbg_time("voxels", t);
    let mut p = Prepared { shell_of, rank, nv0, orig, moved, crossing: Vec::new(), degenerate: 0, contact: 0, vox };
    let results: Vec<(u32, u32, PairResult)> = {
        use rayon::prelude::*;
        candidate_pairs(&jm, &faces)
            .into_par_iter()
            .filter_map(|(fa, fb)| match test_pair(&jm, fa, fb) {
                PairResult::None => None,
                r => Some((fa, fb, r)),
            })
            .collect()
    };
    for (fa, fb, r) in results {
        match r {
            PairResult::None => {}
            PairResult::Degenerate => p.degenerate += 1,
            PairResult::Contact => p.contact += 1,
            r => p.crossing.push((fa, fb, r)),
        }
    }
    Some(p)
}

/// Union of the closed shells. The shells must point out (see stages::wrap).
///
/// A cut curve with a gap (for example where it runs into a face contact)
/// lets a patch leak to the other side, and the result gets open or
/// non-manifold edges. Such a curve group (cluster) is then left uncut, and
/// the union runs again, up to MAX_ROUNDS times.
pub fn union_shells(mesh: &mut Mesh) -> UnionReport {
    let t0 = std::time::Instant::now();
    let Some(prep) = prepare(mesh) else { return UnionReport::default() };
    dbg_time("prepare", t0);
    let nf = mesh.triangles.len();
    let mut excluded = vec![false; nf];
    let mut protected = vec![false; nf];
    let mut skipped = 0;
    let mut kept_patches = 0;
    for _ in 0..MAX_ROUNDS {
        let mut m = mesh.clone();
        let mut bad = Vec::new();
        let mut protect = Vec::new();
        let t1 = std::time::Instant::now();
        let mut rep = union_round(&mut m, &prep, &excluded, &protected, &mut bad, &mut protect);
        dbg_time("round", t1);
        rep.degenerate = prep.degenerate;
        rep.contact = prep.contact;
        if bad.is_empty() && protect.is_empty() {
            rep.skipped_clusters = skipped;
            rep.kept_patches = kept_patches;
            *mesh = m;
            return rep;
        }
        skipped += bad.len();
        for cluster in bad {
            for f in cluster {
                excluded[f as usize] = true;
            }
        }
        kept_patches += protect.len();
        for f in protect {
            protected[f as usize] = true;
        }
    }
    UnionReport { rolled_back: true, skipped_clusters: skipped, degenerate: prep.degenerate, contact: prep.contact, ..Default::default() }
}

/// Phase times to stderr when the variable MESHFIX_TIMING is set.
pub(crate) fn dbg_time(what: &str, t: std::time::Instant) {
    if std::env::var_os("MESHFIX_TIMING").is_some() {
        eprintln!("meshfix time {what}: {:.3} s", t.elapsed().as_secs_f64());
    }
}

#[derive(Clone, Copy, PartialEq, Eq, Debug)]
enum Side {
    Keep,
    Flip,
    Drop,
}

/// Union rounds before the union gives up.
pub const MAX_ROUNDS: usize = 6;

fn union_round(
    mesh: &mut Mesh,
    prep: &Prepared,
    excluded: &[bool],
    protected: &[bool],
    bad: &mut Vec<Vec<u32>>,
    protect: &mut Vec<u32>,
) -> UnionReport {
    let t_round = std::time::Instant::now();
    let mut rep = UnionReport::default();
    let nf = mesh.triangles.len();
    let nv0 = prep.nv0;
    let shell_of = &prep.shell_of;
    let rank = prep.rank;
    mesh.vertices.copy_from_slice(&prep.moved);

    // Clusters: faces joined by a crossing pair.
    let mut fparent: Vec<u32> = (0..nf as u32).collect();
    // Vertex → a face of its cluster.
    let mut vcluster: HashMap<u32, u32> = HashMap::new();
    let mut cuts = Cuts::default();
    let mut vertex_alias: Vec<(u32, u32)> = Vec::new();
    for &(fa, fb, r) in &prep.crossing {
        if excluded[fa as usize] || excluded[fb as usize] {
            continue;
        }
        let (ra, rb) = (find(&mut fparent, fa), find(&mut fparent, fb));
        if ra != rb {
            fparent[ra.max(rb) as usize] = ra.min(rb);
        }
        match r {
            PairResult::Segment([h0, h1]) => {
                let p0 = cuts.point_of(mesh, h0);
                let p1 = cuts.point_of(mesh, h1);
                vcluster.insert(p0, fa);
                vcluster.insert(p1, fa);
                if p0 != p1 {
                    cuts.segs.entry(fa).or_default().push((p0, p1));
                    cuts.segs.entry(fb).or_default().push((p0, p1));
                    rep.pairs += 1;
                }
            }
            PairResult::FromVertex { va, vb, hit } => {
                let p1 = cuts.point_of(mesh, hit);
                vcluster.insert(p1, fa);
                vcluster.insert(va, fa);
                vcluster.insert(vb, fa);
                // Copies of one point must become one vertex for the cut
                // curve. The final non-manifold split separates them again.
                if va != vb {
                    vertex_alias.push((va, vb));
                }
                cuts.segs.entry(fa).or_default().push((va, p1));
                cuts.segs.entry(fb).or_default().push((vb, p1));
                rep.pairs += 1;
            }
            _ => {}
        }
    }
    #[cfg(test)]
    if std::env::var("MESHFIX_DEBUG_BIN").is_ok() {
        let mut segset: std::collections::HashSet<(u32, u32)> = std::collections::HashSet::new();
        for l in cuts.segs.values() {
            for &(a, b) in l {
                segset.insert((a.min(b), a.max(b)));
            }
        }
        let mut deg: HashMap<u32, u32> = HashMap::new();
        for &(a, b) in &segset {
            *deg.entry(a).or_default() += 1;
            *deg.entry(b).or_default() += 1;
        }
        let odd = deg.values().filter(|&&d| d % 2 == 1).count();
        let odd_orig = deg.iter().filter(|(&v, &d)| d % 2 == 1 && (v as usize) < nv0).count();
        eprintln!("debug: segments {} curve points {} odd-degree points {} (original vertices {})", segset.len(), deg.len(), odd, odd_orig);
    }
    // 3-4. Cut faces: faces with segments, inner points or points on an edge.
    let ef = mesh.edge_faces();
    let mut cut = vec![false; nf];
    for &f in cuts.segs.keys().chain(cuts.inside.keys()) {
        cut[f as usize] = true;
    }
    for k in cuts.on_edge.keys() {
        if let Some(fs) = ef.get(k) {
            for &f in fs {
                cut[f as usize] = true;
            }
        }
    }
    let mut hit_shell = vec![false; rank as usize];
    let mut tris: Vec<[u32; 3]> = Vec::with_capacity(nf + 4 * cuts.segs.len());
    let mut tshell: Vec<u32> = Vec::with_capacity(tris.capacity());
    let mut tsrc: Vec<u32> = Vec::with_capacity(tris.capacity());
    let mut aliases = vertex_alias;
    for f in 0..nf {
        let t = mesh.triangles[f];
        if !cut[f] {
            tris.push(t);
            tshell.push(shell_of[f]);
            tsrc.push(f as u32);
            continue;
        }
        if shell_of[f] != u32::MAX {
            hit_shell[shell_of[f] as usize] = true;
        }
        let before = mesh.vertices.len() as u32;
        // spade can assert on an almost degenerate constraint set. That
        // face then stays uncut, and the cluster check of this round
        // excludes its cut cluster in the next round.
        let tri_new = std::panic::catch_unwind(std::panic::AssertUnwindSafe(|| {
            retriangulate(mesh, t, f as u32, &cuts, &mut aliases)
        }))
        .unwrap_or(None);
        match tri_new {
            Some(new) => {
                rep.cut_faces += 1;
                if cuts.segs.contains_key(&(f as u32)) {
                    for v in before..mesh.vertices.len() as u32 {
                        vcluster.insert(v, f as u32);
                    }
                }
                for nt in new {
                    tris.push(nt);
                    tshell.push(shell_of[f]);
                    tsrc.push(f as u32);
                }
            }
            None => {
                rep.failed_faces += 1;
                tris.push(t);
                tshell.push(shell_of[f]);
                tsrc.push(f as u32);
            }
        }
    }

    dbg_time("  cut+cdt", t_round);
    // Weld: the same point can come from two crossings (for example where
    // three faces meet). The tolerance is far below each repair tolerance.
    let nv = mesh.vertices.len();
    let mut parent: Vec<u32> = (0..nv as u32).collect();
    for (a, b) in aliases {
        let (ra, rb) = (find(&mut parent, a), find(&mut parent, b));
        if ra != rb {
            parent[ra.max(rb) as usize] = ra.min(rb);
        }
    }
    let tol = 1e-9;
    let mut grid = Grid::new(tol);
    let mut near = Vec::new();
    for i in nv0..nv {
        let p = mesh.vertices[i];
        near.clear();
        grid.near(p, &mut near);
        if let Some(&j) = near.iter().find(|&&j| dist2(p, mesh.vertices[j as usize]) <= tol * tol) {
            let (ra, rb) = (find(&mut parent, i as u32), find(&mut parent, j));
            if ra != rb {
                parent[ra.max(rb) as usize] = ra.min(rb);
            }
        } else {
            grid.insert(i as u32, p);
        }
    }
    let mut keep_t = Vec::with_capacity(tris.len());
    let mut keep_s = Vec::with_capacity(tris.len());
    let mut keep_src = Vec::with_capacity(tris.len());
    for ((t, s), src) in tris.into_iter().zip(tshell).zip(tsrc) {
        let t = [find(&mut parent, t[0]), find(&mut parent, t[1]), find(&mut parent, t[2])];
        if t[0] != t[1] && t[1] != t[2] && t[2] != t[0] {
            keep_t.push(t);
            keep_s.push(s);
            keep_src.push(src);
        }
    }
    mesh.triangles = keep_t;
    let tshell = keep_s;
    let tsrc = keep_src;
    // Cluster of each welded vertex.
    // One welded vertex can be in more than one cluster: keep all of them,
    // so that the result does not depend on the map order.
    let mut wcluster: HashMap<u32, Vec<u32>> = HashMap::with_capacity(vcluster.len());
    for (&v, &f) in &vcluster {
        let r = find(&mut parent, v);
        let c = find(&mut fparent, f);
        let e = wcluster.entry(r).or_default();
        if !e.contains(&c) {
            e.push(c);
        }
    }

    // 5. Patches through edges with exactly two faces.
    let ef = mesh.edge_faces();
    let nf = mesh.triangles.len();
    let mut patch = vec![u32::MAX; nf];
    let mut patches: Vec<Vec<u32>> = Vec::new();
    for s in 0..nf {
        if patch[s] != u32::MAX {
            continue;
        }
        let id = patches.len() as u32;
        let mut list = vec![s as u32];
        patch[s] = id;
        let mut i = 0;
        while i < list.len() {
            let f = list[i] as usize;
            i += 1;
            let t = mesh.triangles[f];
            for j in 0..3 {
                let fs = &ef[&ekey(t[j], t[(j + 1) % 3])];
                if fs.len() != 2 {
                    continue;
                }
                for &g in fs {
                    if patch[g as usize] == u32::MAX {
                        patch[g as usize] = id;
                        list.push(g);
                    }
                }
            }
        }
        patches.push(list);
    }
    // Winding numbers with a fast (tree) sum over all closed faces.
    let closed_faces: Vec<u32> = (0..nf as u32).filter(|&f| tshell[f as usize] != u32::MAX).collect();
    dbg_time("  weld+patches", t_round);
    let fwn = Fwn::new(mesh, &closed_faces);
    let mut drop_face = vec![false; nf];
    let _ = &hit_shell;
    // MESHFIX_PER_FACE=1: classify each face on its own (no patch leak
    // through a gap in a cut curve). Diagnosis switch for now.
    let singles: Vec<Vec<u32>>;
    let source: &Vec<Vec<u32>> = if std::env::var_os("MESHFIX_PER_FACE").is_some() {
        singles = (0..nf as u32).map(|f| vec![f]).collect();
        &singles
    } else {
        &patches
    };
    let todo: Vec<&Vec<u32>> = source
        .iter()
        .filter(|list| {
            tshell[list[0] as usize] != u32::MAX && !list.iter().any(|&g| protected[tsrc[g as usize] as usize])
        })
        .collect();
    let vox = prep.vox.as_ref();
    let ws: Vec<Side> = {
        use rayon::prelude::*;
        let m: &Mesh = mesh;
        todo.par_iter()
            .map(|list| {
                let f = *list
                    .iter()
                    .max_by(|&&x, &&y| {
                        let [a, b, c] = m.tri_pos(x as usize);
                        let [d, e, g] = m.tri_pos(y as usize);
                        tri_area(a, b, c).total_cmp(&tri_area(d, e, g))
                    })
                    .unwrap();
                let t = m.tri_pos(f as usize);
                let p = scale(add(add(t[0], t[1]), t[2]), 1.0 / 3.0);
                let n = cross(sub(t[1], t[0]), sub(t[2], t[0]));
                let ln = norm(n);
                if ln == 0.0 {
                    return Side::Drop;
                }
                let u = scale(n, 1.0 / ln);
                // 1. Voxel flood fill: is the space in front of / behind the
                // face connected to the outside? Two and three voxels away,
                // so that the own surface voxels do not count.
                if let Some(v) = vox {
                    let h = v.voxel();
                    let front = v.outside(add(p, scale(u, 2.0 * h))) || v.outside(add(p, scale(u, 3.0 * h)));
                    let back = v.outside(sub(p, scale(u, 2.0 * h))) && v.outside(sub(p, scale(u, 3.0 * h)));
                    match (front, back) {
                        (true, false) => return Side::Keep,
                        (false, true) => return Side::Flip,
                        (false, false) => return Side::Drop,
                        (true, true) => {} // thinner than the voxels: see 2.
                    }
                }
                // 2. Winding number a small step in front of and behind the
                // face. A thin solid gives about 0 and 1, a loose sheet
                // about -0.5 and 0.5. The step is more than the move of
                // step 1 (1e-6), so a face of another body in contact counts.
                let eps = (0.05 * (0.5 * ln).sqrt()).clamp(2e-5, 1e-3);
                let e = scale(u, eps);
                let (wf, wb) = (fwn.eval(add(p, e), None), fwn.eval(sub(p, e), None));
                if wf < 0.25 && wb > 0.75 {
                    Side::Keep
                } else if wb < 0.25 && wf > 0.75 {
                    Side::Flip
                } else {
                    Side::Drop
                }
            })
            .collect()
    };
    #[cfg(test)]
    let side_of: HashMap<u32, Side> = todo.iter().zip(ws.iter()).flat_map(|(l, &sd)| l.iter().map(move |&g| (g, sd))).collect();
    let mut flip_face = vec![false; nf];
    for (list, side) in todo.into_iter().zip(ws) {
        if side == Side::Keep {
            continue; // outside in front, inside behind: outer surface
        }
        if side == Side::Flip {
            // Outer surface, but turned in: turn it out.
            for &g in list {
                flip_face[g as usize] = true;
            }
            continue;
        }
        // Inside on both sides (inner wall, overlap, contact) or outside on
        // both sides (loose sheet).
        rep.removed_patches += 1;
        for &g in list {
            drop_face[g as usize] = true;
        }
    }
    for f in 0..nf {
        if flip_face[f] {
            mesh.triangles[f].swap(1, 2);
        }
    }

    dbg_time("  classify", t_round);
    // Check: an edge of the kept faces with other than 2 faces, at a cut
    // vertex, marks its cluster as bad.
    let mut count: HashMap<EdgeKey, u32> = HashMap::new();
    for f in 0..nf {
        if drop_face[f] {
            continue;
        }
        let t = mesh.triangles[f];
        for j in 0..3 {
            *count.entry(ekey(t[j], t[(j + 1) % 3])).or_default() += 1;
        }
    }
    #[cfg(test)]
    if std::env::var("MESHFIX_DEBUG_BIN").is_ok() {
        let mut shown = 0;
        let mut kinds: HashMap<String, usize> = HashMap::new();
        for (&(a, b), &n) in &count {
            if n != 4 {
                continue;
            }
            let fs: Vec<u32> = ef[&(a, b)].iter().copied().filter(|&g| !drop_face[g as usize]).collect();
            let mut key: Vec<String> = fs.iter().map(|&g| format!("s{}:{:?}:p{}", tshell[g as usize], side_of.get(&g), patches[patch[g as usize] as usize].len())).collect();
            key.sort();
            let shells: Vec<u32> = { let mut v: Vec<u32> = fs.iter().map(|&g| tshell[g as usize]).collect(); v.sort(); v.dedup(); v };
            *kinds.entry(format!("shells {} sides {:?}", shells.len(), fs.iter().map(|g| side_of.get(g).copied()).collect::<Vec<_>>())).or_default() += 1;
            if shown < 6 {
                shown += 1;
                eprintln!("  4-face edge {a}-{b} at {:?}: {:?}", mesh.vertices[a as usize], key);
            }
        }
        let mut k: Vec<_> = kinds.into_iter().collect();
        k.sort_by(|x, y| y.1.cmp(&x.1));
        for (s, c) in k.iter().take(8) {
            eprintln!("  kind {c}: {s}");
        }
    }
    let mut bad_roots: Vec<u32> = Vec::new();
    for (&(a, b), &n) in &count {
        if n == 2 {
            continue;
        }
        let all = &ef[&(a, b)];
        // A defect that was there before the removal is not ours.
        if n as usize == all.len() {
            continue;
        }
        let mut at_cut = false;
        for v in [a, b] {
            for &c in wcluster.get(&v).map(|x| x.as_slice()).unwrap_or(&[]) {
                at_cut = true;
                if !bad_roots.contains(&c) {
                    bad_roots.push(c);
                }
            }
        }
        if !at_cut {
            // The removal of a patch opened this edge: keep that patch.
            for &g in all {
                if drop_face[g as usize] {
                    protect.push(tsrc[g as usize]);
                }
            }
        }
    }
    protect.sort_unstable();
    protect.dedup();
    if !bad_roots.is_empty() || !protect.is_empty() {
        let mut groups: HashMap<u32, Vec<u32>> = HashMap::new();
        for f in 0..fparent.len() as u32 {
            let r = find(&mut fparent, f);
            if bad_roots.contains(&r) {
                groups.entry(r).or_default().push(f);
            }
        }
        let mut gs: Vec<Vec<u32>> = groups.into_values().collect();
        gs.sort_unstable();
        bad.extend(gs);
        return rep;
    }

    // Original vertices go back to their place.
    mesh.vertices[..nv0].copy_from_slice(&prep.orig);
    let keep: Vec<bool> = drop_face.iter().map(|d| !d).collect();
    mesh.retain_faces(&keep);
    rep
}

// ---- Fast winding number (Barnes-Hut, dipole order) ---------------------

struct FwnNode {
    lo: Vec3,
    hi: Vec3,
    /// Area-weighted centroid and the sum of the area vectors.
    c: Vec3,
    n: Vec3,
    r: f64,
    /// Children (left, right), or u32::MAX for a leaf.
    kids: [u32; 2],
    /// Face range in `order` for a leaf.
    start: u32,
    end: u32,
}

/// Winding number of a set of closed, outward faces at a point. Far boxes
/// use the dipole approximation, near boxes the exact solid angles.
pub(crate) struct Fwn {
    nodes: Vec<FwnNode>,
    order: Vec<u32>,
    tri: Vec<[Vec3; 3]>,
}

const FWN_LEAF: usize = 8;
const FWN_BETA: f64 = 3.0;

impl Fwn {
    pub(crate) fn new(mesh: &Mesh, faces: &[u32]) -> Self {
        let tri: Vec<[Vec3; 3]> = faces.iter().map(|&f| mesh.tri_pos(f as usize)).collect();
        let mut s = Fwn { nodes: Vec::new(), order: (0..faces.len() as u32).collect(), tri };
        if !faces.is_empty() {
            let n = s.order.len();
            s.build(0, n);
        }
        s
    }

    fn build(&mut self, start: usize, end: usize) -> u32 {
        let mut lo = [f64::INFINITY; 3];
        let mut hi = [f64::NEG_INFINITY; 3];
        let mut c = [0.0; 3];
        let mut n = [0.0; 3];
        let mut area = 0.0;
        for &i in &self.order[start..end] {
            let [a, b, d] = self.tri[i as usize];
            for p in [a, b, d] {
                for k in 0..3 {
                    lo[k] = lo[k].min(p[k]);
                    hi[k] = hi[k].max(p[k]);
                }
            }
            let av = scale(cross(sub(b, a), sub(d, a)), 0.5);
            let ar = norm(av);
            n = add(n, av);
            c = add(c, scale(add(add(a, b), d), ar / 3.0));
            area += ar;
        }
        let c = if area > 0.0 { scale(c, 1.0 / area) } else { scale(add(lo, hi), 0.5) };
        let mut r: f64 = 0.0;
        for &x in &[lo[0], hi[0]] {
            for &y in &[lo[1], hi[1]] {
                for &z in &[lo[2], hi[2]] {
                    r = r.max(dist([x, y, z], c));
                }
            }
        }
        let id = self.nodes.len() as u32;
        self.nodes.push(FwnNode { lo, hi, c, n, r, kids: [u32::MAX; 2], start: start as u32, end: end as u32 });
        if end - start > FWN_LEAF {
            let k = (0..3).max_by(|&x, &y| (hi[x] - lo[x]).total_cmp(&(hi[y] - lo[y]))).unwrap();
            let tri = &self.tri;
            let key = |i: &u32| {
                let [a, b, d] = tri[*i as usize];
                a[k] + b[k] + d[k]
            };
            let mid = (start + end) / 2;
            self.order[start..end].select_nth_unstable_by(mid - start, |x, y| key(x).total_cmp(&key(y)));
            let l = self.build(start, mid);
            let rr = self.build(mid, end);
            self.nodes[id as usize].kids = [l, rr];
        }
        id
    }

    /// Winding number at p. `skip` is a triangle to leave out (the sample
    /// face itself), given by its three corners.
    pub(crate) fn eval(&self, p: Vec3, skip: Option<[Vec3; 3]>) -> f64 {
        if self.nodes.is_empty() {
            return 0.0;
        }
        let mut w = 0.0;
        let mut stack = vec![0u32];
        while let Some(i) = stack.pop() {
            let nd = &self.nodes[i as usize];
            let d = sub(nd.c, p);
            let l = norm(d);
            let inside_box = (0..3).all(|k| p[k] >= nd.lo[k] && p[k] <= nd.hi[k]);
            if !inside_box && l > FWN_BETA * nd.r {
                w += dot(nd.n, d) / (l * l * l);
                continue;
            }
            if nd.kids[0] == u32::MAX {
                for &f in &self.order[nd.start as usize..nd.end as usize] {
                    let t = self.tri[f as usize];
                    if Some(t) == skip {
                        continue;
                    }
                    w += solid_angle(p, t[0], t[1], t[2]);
                }
            } else {
                stack.push(nd.kids[0]);
                stack.push(nd.kids[1]);
            }
        }
        w / (4.0 * std::f64::consts::PI)
    }
}

// ---- Outside test by a voxel flood fill ---------------------------------
//
// The winding number does not give a clear inside when an input body has
// inner walls or gaps (each wall adds a jump of 1). The flood fill does: a
// point is outside when empty voxels connect it to the bounding box border.
// Gaps smaller than a voxel are closed. This is how a wrap sees the model.

pub(crate) struct VoxelOutside {
    lo: Vec3,
    h: f64,
    n: [usize; 3],
    /// 1 bit per voxel: on a surface.
    wall: Vec<u64>,
    /// 1 bit per voxel: connected to the border by empty voxels.
    out: Vec<u64>,
}

#[inline]
fn bit(v: &[u64], i: usize) -> bool {
    v[i >> 6] >> (i & 63) & 1 == 1
}
#[inline]
fn set(v: &mut [u64], i: usize) {
    v[i >> 6] |= 1 << (i & 63);
}

impl VoxelOutside {
    /// `budget` = largest number of voxels in the box.
    pub(crate) fn new(mesh: &Mesh, faces: &[u32], budget: usize) -> Option<Self> {
        let mut lo = [f64::INFINITY; 3];
        let mut hi = [f64::NEG_INFINITY; 3];
        for &f in faces {
            for p in mesh.tri_pos(f as usize) {
                for k in 0..3 {
                    lo[k] = lo[k].min(p[k]);
                    hi[k] = hi[k].max(p[k]);
                }
            }
        }
        let ext = (0..3).map(|k| hi[k] - lo[k]).fold(0.0, f64::max);
        if !(ext > 0.0) {
            return None;
        }
        let size = [hi[0] - lo[0], hi[1] - lo[1], hi[2] - lo[2]];
        let h = ((size[0] + ext * 0.01) * (size[1] + ext * 0.01) * (size[2] + ext * 0.01) / budget as f64).cbrt();
        // Two empty voxel layers on each side: the border is outside.
        let lo = [lo[0] - 2.0 * h, lo[1] - 2.0 * h, lo[2] - 2.0 * h];
        let n = [
            ((hi[0] - lo[0]) / h).ceil() as usize + 3,
            ((hi[1] - lo[1]) / h).ceil() as usize + 3,
            ((hi[2] - lo[2]) / h).ceil() as usize + 3,
        ];
        let total = n[0] * n[1] * n[2];
        let words = total.div_ceil(64);
        let mut s = VoxelOutside { lo, h, n, wall: vec![0; words], out: vec![0; words] };
        // Mark the surface: points on each face at a third of a voxel.
        for &f in faces {
            let [a, b, c] = mesh.tri_pos(f as usize);
            let l = dist(a, b).max(dist(b, c)).max(dist(c, a));
            let k = ((l / (h / 3.0)).ceil() as usize).clamp(1, 4096);
            for i in 0..=k {
                for j in 0..=(k - i) {
                    let (u, v) = (i as f64 / k as f64, j as f64 / k as f64);
                    let p = add(a, add(scale(sub(b, a), u), scale(sub(c, a), v)));
                    if let Some(id) = s.index(p) {
                        set(&mut s.wall, id);
                    }
                }
            }
        }
        // Flood fill from the border through empty voxels (6 neighbours).
        let mut queue: VecDeque<u32> = VecDeque::new();
        let idx = |x: usize, y: usize, z: usize| (z * n[1] + y) * n[0] + x;
        for z in 0..n[2] {
            for y in 0..n[1] {
                for x in 0..n[0] {
                    if x == 0 || y == 0 || z == 0 || x == n[0] - 1 || y == n[1] - 1 || z == n[2] - 1 {
                        let i = idx(x, y, z);
                        if !bit(&s.wall, i) && !bit(&s.out, i) {
                            set(&mut s.out, i);
                            queue.push_back(i as u32);
                        }
                    }
                }
            }
        }
        let (sx, sy) = (1usize, n[0]);
        let sz = n[0] * n[1];
        while let Some(i) = queue.pop_front() {
            let i = i as usize;
            let x = i % n[0];
            let y = (i / n[0]) % n[1];
            let z = i / sz;
            let mut try_push = |j: usize, s: &mut VoxelOutside| {
                if !bit(&s.wall, j) && !bit(&s.out, j) {
                    set(&mut s.out, j);
                    queue.push_back(j as u32);
                }
            };
            if x > 0 { try_push(i - sx, &mut s); }
            if x + 1 < n[0] { try_push(i + sx, &mut s); }
            if y > 0 { try_push(i - sy, &mut s); }
            if y + 1 < n[1] { try_push(i + sy, &mut s); }
            if z > 0 { try_push(i - sz, &mut s); }
            if z + 1 < n[2] { try_push(i + sz, &mut s); }
        }
        Some(s)
    }

    fn index(&self, p: Vec3) -> Option<usize> {
        let mut c = [0usize; 3];
        for k in 0..3 {
            let v = ((p[k] - self.lo[k]) / self.h).floor();
            if v < 0.0 || v >= self.n[k] as f64 {
                return None;
            }
            c[k] = v as usize;
        }
        Some((c[2] * self.n[1] + c[1]) * self.n[0] + c[0])
    }

    /// True when p is in a voxel that the flood fill reached.
    pub(crate) fn outside(&self, p: Vec3) -> bool {
        match self.index(p) {
            Some(i) => bit(&self.out, i),
            None => true,
        }
    }

    pub(crate) fn voxel(&self) -> f64 {
        self.h
    }
}
