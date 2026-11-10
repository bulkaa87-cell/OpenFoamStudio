//! Overlay of faces that lie exactly in one axis plane.
//!
//! CAD assemblies often have parts that touch on a plane: a section cut of
//! the fuselage, the symmetry plane, a panel on a flat skin. The two caps on
//! that plane are coplanar and overlap. The small vertex move of the
//! arrangement (arrange.rs) would make them cross in many thin slivers.
//!
//! This step does the overlay of all faces in one axis plane (all three
//! vertices have the same bit-exact coordinate) with a 2D constrained
//! Delaunay triangulation. Each new triangle gets the sum of the
//! orientations (+1 / -1 against the plane axis) of the input faces that
//! cover it:
//! - sum 0 (two caps that face each other, or no face): the triangle goes;
//! - sum not 0: one triangle stays, turned by the sign of the sum.
//!
//! New points on an edge that a face outside the plane also uses split that
//! face too, so that the mesh has no T-junction there.

use crate::math::*;
use crate::mesh::*;
use robust::{orient2d, Coord};
use spade::{ConstrainedDelaunayTriangulation, Point2, Triangulation};
use std::collections::HashMap;

#[derive(Clone, Debug, Default, PartialEq, Eq)]
pub struct PlanarReport {
    /// Planes where the overlay changed the faces.
    pub planes: usize,
    /// Input faces replaced.
    pub replaced: usize,
    /// Faces after the overlay.
    pub added: usize,
    /// Planes skipped because the CDT failed.
    pub failed: usize,
    /// Coordinates moved onto an axis plane.
    pub snapped: usize,
}

#[inline]
fn o2(a: [f64; 2], b: [f64; 2], c: [f64; 2]) -> f64 {
    orient2d(Coord { x: a[0], y: a[1] }, Coord { x: b[0], y: b[1] }, Coord { x: c[0], y: c[1] })
}

/// Do the overlay on each axis plane with overlapping faces.
pub fn overlay_axis_planes(mesh: &mut Mesh) -> PlanarReport {
    let mut rep = PlanarReport::default();
    let tol = std::env::var("MESHFIX_PLANE_TOL").ok().and_then(|s| s.parse::<f64>().ok()).unwrap_or(PLANE_TOL);
    if tol > 0.0 {
        rep.snapped = snap_axis_values(mesh, tol);
    }
    mesh.weld_exact();
    // Faces by plane: (axis, coordinate bits).
    let mut groups: HashMap<(usize, u64), Vec<u32>> = HashMap::new();
    for (f, t) in mesh.triangles.iter().enumerate() {
        let p = [mesh.vertices[t[0] as usize], mesh.vertices[t[1] as usize], mesh.vertices[t[2] as usize]];
        for k in 0..3 {
            if p[0][k] == p[1][k] && p[1][k] == p[2][k] {
                let v = if p[0][k] == 0.0 { 0.0f64 } else { p[0][k] };
                groups.entry((k, v.to_bits())).or_default().push(f as u32);
                break;
            }
        }
    }
    let mut keys: Vec<(usize, u64)> = groups.iter().filter(|(_, l)| l.len() >= 2).map(|(k, _)| *k).collect();
    keys.sort_unstable();
    let mut remove = vec![false; mesh.triangles.len()];
    let mut add_tris: Vec<[u32; 3]> = Vec::new();
    // Splits of edges that faces outside the planes use: edge -> points.
    let mut edge_points: HashMap<EdgeKey, Vec<u32>> = HashMap::new();
    for key in keys {
        let faces = &groups[&key];
        let r = std::panic::catch_unwind(std::panic::AssertUnwindSafe(|| overlay_one(mesh, key, faces)));
        match r {
            Ok(Some(ov)) => {
                rep.planes += 1;
                rep.replaced += faces.len();
                rep.added += ov.tris.len();
                for &f in faces {
                    remove[f as usize] = true;
                }
                let base = mesh.vertices.len() as u32;
                mesh.vertices.extend_from_slice(&ov.new_pos);
                let g = |id: u32| if id >= NEW { base + (id - NEW) } else { id };
                for t in ov.tris {
                    add_tris.push([g(t[0]), g(t[1]), g(t[2])]);
                }
                for (e, pts) in ov.on_edge {
                    edge_points.entry(e).or_default().extend(pts.into_iter().map(g));
                }
            }
            Ok(None) => {}
            Err(_) => rep.failed += 1,
        }
    }
    if rep.planes == 0 {
        return rep;
    }
    // Split the faces outside the planes at the new points on their edges.
    // A face with points on more than one edge goes through the queue
    // again with each piece.
    let n0 = mesh.triangles.len();
    let mut out: Vec<[u32; 3]> = Vec::with_capacity(n0 + add_tris.len());
    let mut queue: Vec<[u32; 3]> = (0..n0).filter(|&f| !remove[f]).map(|f| mesh.triangles[f]).collect();
    while let Some(t) = queue.pop() {
        let mut split = false;
        for i in 0..3 {
            let (a, b, c) = (t[i], t[(i + 1) % 3], t[(i + 2) % 3]);
            let Some(pts) = edge_points.get(&ekey(a, b)) else { continue };
            let (pa, pb) = (mesh.vertices[a as usize], mesh.vertices[b as usize]);
            let d = sub(pb, pa);
            let mut sp: Vec<(f64, u32)> = pts.iter().map(|&x| (dot(sub(mesh.vertices[x as usize], pa), d), x)).collect();
            sp.sort_by(|x, y| x.0.total_cmp(&y.0));
            sp.dedup_by_key(|x| x.1);
            let mut chain = vec![a];
            chain.extend(sp.into_iter().map(|x| x.1));
            chain.push(b);
            for w in chain.windows(2) {
                queue.push([w[0], w[1], c]);
            }
            split = true;
            break;
        }
        if !split {
            out.push(t);
        }
    }
    out.extend(add_tris);
    mesh.triangles = out;
    mesh.drop_collapsed();
    mesh.compact();
    rep
}

const NEW: u32 = 0x8000_0000;

/// Coordinates closer than this (normalized units) to the coordinate of an
/// axis plane are moved onto the plane.
pub const PLANE_TOL: f64 = 1e-5;

/// Move coordinate values that are almost equal to the value of an axis
/// plane onto that value. An axis plane is a value that the three vertices
/// of at least one face have exactly. Returns the number of moved values.
fn snap_axis_values(mesh: &mut Mesh, tol: f64) -> usize {
    let mut moved = 0;
    for k in 0..3 {
        let mut planes: Vec<f64> = Vec::new();
        for t in &mesh.triangles {
            let z = mesh.vertices[t[0] as usize][k];
            if mesh.vertices[t[1] as usize][k] == z && mesh.vertices[t[2] as usize][k] == z {
                planes.push(z);
            }
        }
        if planes.is_empty() {
            continue;
        }
        planes.sort_by(|a, b| a.total_cmp(b));
        planes.dedup();
        for p in &mut mesh.vertices {
            let x = p[k];
            let i = planes.partition_point(|&v| v < x);
            let mut best: Option<f64> = None;
            for j in [i.wrapping_sub(1), i] {
                if let Some(&v) = planes.get(j) {
                    if (v - x).abs() <= tol && best.map_or(true, |b: f64| (v - x).abs() < (b - x).abs()) {
                        best = Some(v);
                    }
                }
            }
            if let Some(v) = best {
                if v != x {
                    p[k] = v;
                    moved += 1;
                }
            }
        }
    }
    moved
}

struct Overlay {
    tris: Vec<[u32; 3]>,
    new_pos: Vec<Vec3>,
    on_edge: HashMap<EdgeKey, Vec<u32>>,
}

/// Overlay of the faces of one plane. None when nothing overlaps.
fn overlay_one(mesh: &Mesh, key: (usize, u64), faces: &[u32]) -> Option<Overlay> {
    let (k, bits) = key;
    let value = f64::from_bits(bits);
    let (ax, ay) = ((k + 1) % 3, (k + 2) % 3);
    let p2 = |id: u32| {
        let p = mesh.vertices[id as usize];
        [p[ax], p[ay]]
    };
    // Orientation of each face against the plane axis.
    let sign: Vec<i32> = faces
        .iter()
        .map(|&f| {
            let t = mesh.triangles[f as usize];
            let o = o2(p2(t[0]), p2(t[1]), p2(t[2]));
            if o > 0.0 {
                1
            } else if o < 0.0 {
                -1
            } else {
                0
            }
        })
        .collect();
    // Quick test: any two faces that overlap in their interiors? Use a grid
    // of boxes and a coverage count at the CDT triangles below; first build
    // the CDT.
    let mut cdt: ConstrainedDelaunayTriangulation<Point2<f64>> = ConstrainedDelaunayTriangulation::new();
    let mut h2id: Vec<u32> = Vec::new();
    let mut id2h: HashMap<u32, spade::handles::FixedVertexHandle> = HashMap::new();
    for &f in faces {
        for &id in &mesh.triangles[f as usize] {
            if id2h.contains_key(&id) {
                continue;
            }
            let q = p2(id);
            let h = cdt.insert(Point2::new(q[0], q[1])).ok()?;
            let i = h.index();
            if i >= h2id.len() {
                h2id.resize(i + 1, u32::MAX);
            }
            if h2id[i] == u32::MAX {
                h2id[i] = id;
            }
            id2h.insert(id, h);
        }
    }
    // Two input vertices at the same 2D point: one CDT vertex. The faces
    // then use the first id; the other ids keep their other faces.
    let mut edges: Vec<(u32, u32)> = Vec::new();
    for &f in faces {
        let t = mesh.triangles[f as usize];
        for i in 0..3 {
            edges.push(ekey(t[i], t[(i + 1) % 3]));
        }
    }
    edges.sort_unstable();
    edges.dedup();
    for &(a, b) in &edges {
        let (ha, hb) = (id2h[&a], id2h[&b]);
        if ha != hb {
            cdt.add_constraint_and_split(ha, hb, |p| p);
        }
    }
    // New points (crossings of edges).
    let mut new_pos: Vec<Vec3> = Vec::new();
    for vh in cdt.vertices() {
        let i = vh.fix().index();
        if i < h2id.len() && h2id[i] != u32::MAX {
            continue;
        }
        let q = vh.position();
        let mut p = [0.0; 3];
        p[k] = value;
        p[ax] = q.x;
        p[ay] = q.y;
        if i >= h2id.len() {
            h2id.resize(i + 1, u32::MAX);
        }
        h2id[i] = NEW + new_pos.len() as u32;
        new_pos.push(p);
    }
    // Coverage of each CDT triangle: sum of the signs of the faces that
    // contain its centroid. A grid over the face boxes finds the faces.
    let mut lo = [f64::INFINITY; 2];
    let mut hi = [f64::NEG_INFINITY; 2];
    let boxes: Vec<([f64; 2], [f64; 2])> = faces
        .iter()
        .map(|&f| {
            let t = mesh.triangles[f as usize];
            let mut l = [f64::INFINITY; 2];
            let mut h = [f64::NEG_INFINITY; 2];
            for &id in &t {
                let q = p2(id);
                for d in 0..2 {
                    l[d] = l[d].min(q[d]);
                    h[d] = h[d].max(q[d]);
                }
            }
            for d in 0..2 {
                lo[d] = lo[d].min(l[d]);
                hi[d] = hi[d].max(h[d]);
            }
            (l, h)
        })
        .collect();
    let n = (faces.len() as f64).sqrt().ceil().max(1.0) as usize;
    let cw = [((hi[0] - lo[0]) / n as f64).max(1e-12), ((hi[1] - lo[1]) / n as f64).max(1e-12)];
    let cell = |q: [f64; 2]| {
        [
            (((q[0] - lo[0]) / cw[0]).floor() as i64).clamp(0, n as i64 - 1) as usize,
            (((q[1] - lo[1]) / cw[1]).floor() as i64).clamp(0, n as i64 - 1) as usize,
        ]
    };
    let mut grid: Vec<Vec<u32>> = vec![Vec::new(); n * n];
    for (i, (l, h)) in boxes.iter().enumerate() {
        let (a, b) = (cell(*l), cell(*h));
        for x in a[0]..=b[0] {
            for y in a[1]..=b[1] {
                grid[y * n + x].push(i as u32);
            }
        }
    }
    let mut tris = Vec::new();
    let mut changed = false;
    for fh in cdt.inner_faces() {
        let vs = fh.vertices();
        let q: Vec<[f64; 2]> = vs.iter().map(|v| [v.position().x, v.position().y]).collect();
        let c = [(q[0][0] + q[1][0] + q[2][0]) / 3.0, (q[0][1] + q[1][1] + q[2][1]) / 3.0];
        let g = cell(c);
        let mut count = 0;
        let mut sum = 0;
        for &i in &grid[g[1] * n + g[0]] {
            let (l, h) = boxes[i as usize];
            if c[0] < l[0] || c[0] > h[0] || c[1] < l[1] || c[1] > h[1] {
                continue;
            }
            let t = mesh.triangles[faces[i as usize] as usize];
            let (a, b, d) = (p2(t[0]), p2(t[1]), p2(t[2]));
            let s = [o2(a, b, c), o2(b, d, c), o2(d, a, c)];
            if s.iter().all(|&x| x > 0.0) || s.iter().all(|&x| x < 0.0) {
                count += 1;
                sum += sign[i as usize];
            }
        }
        if count == 0 {
            continue;
        }
        if count != 1 {
            changed = true;
        }
        if sum == 0 {
            continue;
        }
        let ids = [h2id[vs[0].fix().index()], h2id[vs[1].fix().index()], h2id[vs[2].fix().index()]];
        // CDT triangles are counterclockwise in (ax, ay): sign +1.
        tris.push(if sum > 0 { ids } else { [ids[0], ids[2], ids[1]] });
    }
    if !changed {
        return None;
    }
    // Points on the input edges (for the faces outside the plane).
    let mut on_edge: HashMap<EdgeKey, Vec<u32>> = HashMap::new();
    for &(a, b) in &edges {
        // Walk from a to b on constraint edges: each step goes to the
        // constraint neighbor that is nearest to the line and goes forward.
        let (qa, qb) = (p2(a), p2(b));
        let (ha, hb) = (id2h[&a], id2h[&b]);
        let dir = [qb[0] - qa[0], qb[1] - qa[1]];
        let l2 = dir[0] * dir[0] + dir[1] * dir[1];
        let mut pts = Vec::new();
        let mut cur = ha;
        let mut steps = 0;
        while cur != hb && steps < 10_000 {
            steps += 1;
            let pc = cdt.vertex(cur).position();
            let tc = ((pc.x - qa[0]) * dir[0] + (pc.y - qa[1]) * dir[1]) / l2;
            let mut best: Option<(f64, spade::handles::FixedVertexHandle)> = None;
            for e in cdt.vertex(cur).out_edges() {
                if !cdt.is_constraint_edge(e.as_undirected().fix()) {
                    continue;
                }
                let to = e.to();
                let q = to.position();
                let t = ((q.x - qa[0]) * dir[0] + (q.y - qa[1]) * dir[1]) / l2;
                if t <= tc || t > 1.0 + 1e-9 {
                    continue;
                }
                let off = ((q.x - qa[0]) * dir[1] - (q.y - qa[1]) * dir[0]).abs() / l2.sqrt();
                if best.map_or(true, |b| off < b.0) {
                    best = Some((off, to.fix()));
                }
            }
            let Some((off, next)) = best else { break };
            if off > 1e-6 * l2.sqrt().max(1e-9) {
                break;
            }
            if next != hb {
                pts.push(h2id[next.index()]);
            }
            cur = next;
        }
        if cur != hb {
            continue;
        }
        if !pts.is_empty() {
            on_edge.insert((a, b), pts);
        }
    }
    Some(Overlay { tris, new_pos, on_edge })
}

// ---- Contacts in any plane ------------------------------------------------
//
// Two bodies that touch on a plane that is not an axis plane (a pylon on a
// tilted wing skin, a wall between two halves of a nacelle) have two caps
// that lie almost on each other. The overlay above does not see them: their
// vertices do not have one equal coordinate. Here such faces are found by a
// plane test with a tolerance relative to the model size:
//
// 1. Face pairs that are almost parallel, where the vertices of the smaller
//    face are within the tolerance of the plane of the larger face, and
//    whose interiors overlap in that plane.
// 2. Groups: faces joined by such pairs. A group that is not in one plane
//    (a curved contact) is split into clusters, each in one plane.
// 3. For each cluster: projection to its plane, vertex merge and vertex on
//    edge snap (tolerance), 2D constrained Delaunay overlay. Each new
//    triangle gets the count of the faces that cover it, for each side of
//    the plane. One side only: one triangle stays, turned to that side. Both
//    sides (two caps that face each other) or no face: the triangle goes.
// 4. The vertices of the cluster go onto the plane (when they are in one
//    cluster only); new points are on the plane.
// 5. Faces outside the clusters with new points on their edges are split.

/// Default contact tolerance, relative to the diagonal of the model box.
pub const CONTACT_TOL: f64 = 5e-6;
/// Faces of contacts within this many tolerances are marked for the
/// winding union (CONTACT_LEFT).
const LEFT_TOL: f64 = 10.0;
/// Origin mark of a face of a contact pair that stays (no used cluster).
const LEFT: u32 = u32::MAX - 1;

thread_local! {
    /// Faces (by position) of the contacts that the overlay did not replace.
    /// The winding union (arrange.rs) takes this set.
    pub static CONTACT_LEFT: std::cell::RefCell<std::collections::HashSet<[u64; 9]>> = Default::default();
}

/// Key of a face by its corner positions (sorted).
pub fn face_key(m: &Mesh, f: usize) -> [u64; 9] {
    let mut k: Vec<[u64; 3]> = m.tri_pos(f).iter().map(|p| [p[0].to_bits(), p[1].to_bits(), p[2].to_bits()]).collect();
    k.sort_unstable();
    [k[0][0], k[0][1], k[0][2], k[1][0], k[1][1], k[1][2], k[2][0], k[2][1], k[2][2]]
}

/// Cluster vertices nearer than this part of the tolerance are welded.
const MICRO_WELD: f64 = 0.02;
/// Two faces are almost parallel when |cos| of their normals is above this.
const CONTACT_COS: f64 = 0.9995;

#[derive(Clone, Debug, Default, PartialEq)]
pub struct ContactReport {
    /// Face pairs in contact.
    pub pairs: usize,
    /// Groups of faces joined by the pairs.
    pub groups: usize,
    /// Plane clusters (two faces or more).
    pub clusters: usize,
    /// Clusters where the overlay changed the faces.
    pub changed: usize,
    /// Input faces replaced, faces after the overlay.
    pub replaced: usize,
    pub added: usize,
    /// Vertices merged into a vertex of another face.
    pub merged: usize,
    /// Vertices put on an edge of another face.
    pub snapped: usize,
    /// Vertices moved onto their plane.
    pub moved: usize,
    /// Tolerance used (normalized units).
    pub tol: f64,
    /// Clusters not used: their result gave new open edges.
    pub undone: usize,
}

/// Unit vectors u, v with (u, v, n) right-handed.
fn plane_basis(n: Vec3) -> (Vec3, Vec3) {
    let k = (0..3).min_by(|&a, &b| n[a].abs().total_cmp(&n[b].abs())).unwrap();
    let mut e = [0.0; 3];
    e[k] = 1.0;
    let u = cross(n, e);
    let u = scale(u, 1.0 / norm(u));
    let v = cross(n, u);
    (u, v)
}

/// True when the interiors of two 2D triangles overlap (exact signs).
fn tri_overlap_2d(a: [[f64; 2]; 3], b: [[f64; 2]; 3]) -> bool {
    let ccw = |t: [[f64; 2]; 3]| -> Option<[[f64; 2]; 3]> {
        let o = o2(t[0], t[1], t[2]);
        if o > 0.0 {
            Some(t)
        } else if o < 0.0 {
            Some([t[0], t[2], t[1]])
        } else {
            None
        }
    };
    let (Some(a), Some(b)) = (ccw(a), ccw(b)) else { return false };
    for (s, t) in [(a, b), (b, a)] {
        for i in 0..3 {
            let (p, q) = (s[i], s[(i + 1) % 3]);
            if t.iter().all(|&x| o2(p, q, x) <= 0.0) {
                return false;
            }
        }
    }
    true
}

/// Point in polygon (winding count, exact signs). The point must not be on
/// the boundary.
fn in_polygon(poly: &[[f64; 2]], c: [f64; 2]) -> bool {
    let mut w = 0i32;
    for i in 0..poly.len() {
        let (a, b) = (poly[i], poly[(i + 1) % poly.len()]);
        if a[1] <= c[1] {
            if b[1] > c[1] && o2(a, b, c) > 0.0 {
                w += 1;
            }
        } else if b[1] <= c[1] && o2(a, b, c) < 0.0 {
            w -= 1;
        }
    }
    w != 0
}

struct Plane {
    c: Vec3,
    n: Vec3,
}

impl Plane {
    #[inline]
    fn dist(&self, p: Vec3) -> f64 {
        dot(sub(p, self.c), self.n)
    }
}

/// Area-weighted plane of the faces (normals turned to the first face) and
/// the largest vertex distance from it.
fn fit_plane(m: &Mesh, faces: &[u32], nrm: &[Vec3], area: &[f64]) -> (Plane, f64) {
    let r = nrm[faces[0] as usize];
    let (mut s, mut c, mut aw) = ([0.0; 3], [0.0; 3], 0.0);
    for &f in faces {
        let a = area[f as usize];
        let n = nrm[f as usize];
        s = add(s, scale(n, if dot(n, r) >= 0.0 { a } else { -a }));
        let [p, q, w] = m.tri_pos(f as usize);
        c = add(c, scale(add(add(p, q), w), a / 3.0));
        aw += a;
    }
    let l = norm(s);
    let n = if l > 0.0 { scale(s, 1.0 / l) } else { r };
    let c = if aw > 0.0 { scale(c, 1.0 / aw) } else { m.tri_pos(faces[0] as usize)[0] };
    let pl = Plane { c, n };
    let mut dev: f64 = 0.0;
    for &f in faces {
        for p in m.tri_pos(f as usize) {
            dev = dev.max(pl.dist(p).abs());
        }
    }
    (pl, dev)
}

/// Result of the overlay of one cluster. Ids at NEW and above are new
/// points (index into new_pos).
struct ClusterOut {
    faces: Vec<u32>,
    tris: Vec<[u32; 3]>,
    new_pos: Vec<Vec3>,
    merges: Vec<(u32, u32)>,
    snapped: usize,
    on_edge: Vec<(EdgeKey, Vec<u32>)>,
    moves: Vec<(u32, Vec3)>,
}

/// Overlay of the faces of one cluster in its plane. None when no faces
/// overlap.
fn overlay_cluster(m: &Mesh, faces: &[u32], pl: &Plane, tol: f64) -> Option<ClusterOut> {
    let (u, v) = plane_basis(pl.n);
    let q2 = |p: Vec3| {
        let d = sub(p, pl.c);
        [dot(d, u), dot(d, v)]
    };
    // Vertices of the cluster and their 2D positions.
    let mut ids: Vec<u32> = faces.iter().flat_map(|&f| m.triangles[f as usize]).collect();
    ids.sort_unstable();
    ids.dedup();
    let local: HashMap<u32, usize> = ids.iter().enumerate().map(|(i, &id)| (id, i)).collect();
    let pos: Vec<[f64; 2]> = ids.iter().map(|&id| q2(m.vertices[id as usize])).collect();
    // Edges of the cluster faces (local ids).
    let mut fedges: std::collections::HashSet<(usize, usize)> = std::collections::HashSet::new();
    for &f in faces {
        let t = m.triangles[f as usize];
        for i in 0..3 {
            let (a, b) = (local[&t[i]], local[&t[(i + 1) % 3]]);
            fedges.insert((a.min(b), a.max(b)));
        }
    }
    // 1. Merge vertices nearer than tol that are not on one edge.
    let cellw = tol.max(1e-12);
    let gkey = |p: [f64; 2]| ((p[0] / cellw).floor() as i64, (p[1] / cellw).floor() as i64);
    let mut grid: HashMap<(i64, i64), Vec<usize>> = HashMap::new();
    let mut rep: Vec<usize> = (0..ids.len()).collect();
    let mut merges = Vec::new();
    for i in 0..ids.len() {
        let k = gkey(pos[i]);
        let mut best: Option<(f64, usize)> = None;
        for dx in -1..=1 {
            for dy in -1..=1 {
                if let Some(l) = grid.get(&(k.0 + dx, k.1 + dy)) {
                    for &j in l {
                        if fedges.contains(&(j.min(i), j.max(i))) {
                            continue;
                        }
                        let d = ((pos[i][0] - pos[j][0]).powi(2) + (pos[i][1] - pos[j][1]).powi(2)).sqrt();
                        if d <= tol && best.map_or(true, |b| d < b.0) {
                            best = Some((d, j));
                        }
                    }
                }
            }
        }
        let best = if std::env::var_os("MESHFIX_CONTACT_NOMERGE").is_some() { None } else { best };
        match best {
            Some((_, j)) => {
                rep[i] = j;
                merges.push((ids[i], ids[j]));
            }
            None => grid.entry(k).or_default().push(i),
        }
    }
    // Faces in representative ids; collapsed faces go.
    let tri_l: Vec<Option<[usize; 3]>> = faces
        .iter()
        .map(|&f| {
            let t = m.triangles[f as usize];
            let r = [rep[local[&t[0]]], rep[local[&t[1]]], rep[local[&t[2]]]];
            if r[0] == r[1] || r[1] == r[2] || r[2] == r[0] || o2(pos[r[0]], pos[r[1]], pos[r[2]]) == 0.0 {
                None
            } else {
                Some(r)
            }
        })
        .collect();
    let mut edges: Vec<(usize, usize)> = Vec::new();
    for t in tri_l.iter().flatten() {
        for i in 0..3 {
            let (a, b) = (t[i], t[(i + 1) % 3]);
            edges.push((a.min(b), a.max(b)));
        }
    }
    edges.sort_unstable();
    edges.dedup();
    if edges.is_empty() {
        return None;
    }
    // 2. Vertices within tol of the inner part of an edge go on that edge.
    let mut lsum = 0.0;
    for &(a, b) in &edges {
        lsum += ((pos[a][0] - pos[b][0]).powi(2) + (pos[a][1] - pos[b][1]).powi(2)).sqrt();
    }
    let ecell = (lsum / edges.len() as f64).max(tol).max(1e-9);
    let ekey2 = |p: [f64; 2]| ((p[0] / ecell).floor() as i64, (p[1] / ecell).floor() as i64);
    let mut vgrid: HashMap<(i64, i64), Vec<usize>> = HashMap::new();
    let mut is_rep = vec![false; ids.len()];
    for t in tri_l.iter().flatten() {
        for &x in t {
            is_rep[x] = true;
        }
    }
    for i in 0..ids.len() {
        if is_rep[i] {
            vgrid.entry(ekey2(pos[i])).or_default().push(i);
        }
    }
    let mut on_e: HashMap<(usize, usize), Vec<(f64, usize)>> = HashMap::new();
    let mut snapped = 0;
    for &(a, b) in &edges {
        let (pa, pb) = (pos[a], pos[b]);
        let d = [pb[0] - pa[0], pb[1] - pa[1]];
        let l2 = d[0] * d[0] + d[1] * d[1];
        if l2 <= 0.0 {
            continue;
        }
        let k0 = ekey2([pa[0].min(pb[0]) - tol, pa[1].min(pb[1]) - tol]);
        let k1 = ekey2([pa[0].max(pb[0]) + tol, pa[1].max(pb[1]) + tol]);
        if (k1.0 - k0.0 + 1) * (k1.1 - k0.1 + 1) > 4096 {
            continue;
        }
        let mut hits = Vec::new();
        for x in k0.0..=k1.0 {
            for y in k0.1..=k1.1 {
                let Some(l) = vgrid.get(&(x, y)) else { continue };
                for &w in l {
                    // A vertex on an edge to a or b is a sliver face, not
                    // a contact.
                    if w == a
                        || w == b
                        || fedges.contains(&(w.min(a), w.max(a)))
                        || fedges.contains(&(w.min(b), w.max(b)))
                    {
                        continue;
                    }
                    let pw = pos[w];
                    let t = ((pw[0] - pa[0]) * d[0] + (pw[1] - pa[1]) * d[1]) / l2;
                    if t <= 0.0 || t >= 1.0 {
                        continue;
                    }
                    let off = ((pw[0] - pa[0]) * d[1] - (pw[1] - pa[1]) * d[0]).abs() / l2.sqrt();
                    if off <= tol {
                        hits.push((t, w));
                    }
                }
            }
        }
        if std::env::var_os("MESHFIX_CONTACT_NOSNAP").is_some() {
            hits.clear();
        }
        if !hits.is_empty() {
            hits.sort_by(|x, y| x.0.total_cmp(&y.0));
            snapped += hits.len();
            on_e.insert((a, b), hits);
        }
    }
    // Chain of an edge from a to b (local ids), with the snapped vertices.
    let chain = |a: usize, b: usize| -> Vec<usize> {
        let k = (a.min(b), a.max(b));
        let mut c = vec![k.0];
        if let Some(h) = on_e.get(&k) {
            c.extend(h.iter().map(|x| x.1));
        }
        c.push(k.1);
        if a != k.0 {
            c.reverse();
        }
        c
    };
    // 3. CDT with all chains as constraints.
    let mut cdt: ConstrainedDelaunayTriangulation<Point2<f64>> = ConstrainedDelaunayTriangulation::new();
    let mut h_of: Vec<Option<spade::handles::FixedVertexHandle>> = vec![None; ids.len()];
    let mut h2l: Vec<usize> = Vec::new();
    for i in 0..ids.len() {
        if !is_rep[i] {
            continue;
        }
        let h = cdt.insert(Point2::new(pos[i][0], pos[i][1])).ok()?;
        let hi = h.index();
        if hi >= h2l.len() {
            h2l.resize(hi + 1, usize::MAX);
        }
        if h2l[hi] == usize::MAX {
            h2l[hi] = i;
        }
        h_of[i] = Some(h);
    }
    for &(a, b) in &edges {
        let c = chain(a, b);
        for w in c.windows(2) {
            let (ha, hb) = (h_of[w[0]]?, h_of[w[1]]?);
            if ha != hb {
                cdt.add_constraint_and_split(ha, hb, |p| p);
            }
        }
    }
    // Global ids of the CDT vertices; new points go onto the plane.
    let mut gid: Vec<u32> = vec![u32::MAX; cdt.num_vertices()];
    let mut new_pos: Vec<Vec3> = Vec::new();
    for vh in cdt.vertices() {
        let i = vh.fix().index();
        if i < h2l.len() && h2l[i] != usize::MAX {
            gid[i] = ids[h2l[i]];
            continue;
        }
        let q = vh.position();
        gid[i] = NEW + new_pos.len() as u32;
        new_pos.push(add(pl.c, add(scale(u, q.x), scale(v, q.y))));
    }
    // 4. Path of each edge in the CDT: the chain with the new points that
    // the crossings added. Each part w0-w1 of the chain is a walk on
    // constraint edges through new points, nearest to the line.
    let walk = |w0: usize, w1: usize| -> Option<Vec<usize>> {
        let (h0, h1) = (h_of[w0]?, h_of[w1]?);
        let (q0, q1) = (pos[w0], pos[w1]);
        let d = [q1[0] - q0[0], q1[1] - q0[1]];
        let s2 = d[0] * d[0] + d[1] * d[1];
        if s2 <= 0.0 {
            return None;
        }
        let sl = s2.sqrt();
        let mut out = vec![h0.index()];
        let mut cur = h0;
        let mut tc = 0.0;
        for _ in 0..100_000 {
            if cur == h1 {
                return Some(out);
            }
            let mut best: Option<(f64, f64, spade::handles::FixedVertexHandle)> = None;
            for e in cdt.vertex(cur).out_edges() {
                if !cdt.is_constraint_edge(e.as_undirected().fix()) {
                    continue;
                }
                let to = e.to();
                if to.fix() != h1 && gid[to.fix().index()] < NEW {
                    continue;
                }
                let q = to.position();
                let t = ((q.x - q0[0]) * d[0] + (q.y - q0[1]) * d[1]) / s2;
                if t <= tc || t > 1.0 + 1e-9 {
                    continue;
                }
                let off = ((q.x - q0[0]) * d[1] - (q.y - q0[1]) * d[0]).abs() / sl;
                if best.map_or(true, |b| off < b.0) {
                    best = Some((off, t, to.fix()));
                }
            }
            let (off, t, next) = best?;
            if off > 1e-6 * sl + 1e-12 {
                return None;
            }
            out.push(next.index());
            cur = next;
            tc = t;
        }
        None
    };
    let mut path: HashMap<(usize, usize), Vec<usize>> = HashMap::with_capacity(edges.len());
    for &(a, b) in &edges {
        let c = chain(a, b);
        let mut p = Vec::new();
        for w in c.windows(2) {
            match walk(w[0], w[1]) {
                Some(seg) => {
                    if !p.is_empty() {
                        p.pop();
                    }
                    p.extend(seg);
                }
                None => {
                    if p.is_empty() {
                        p.push(h_of[w[0]]?.index());
                    }
                    p.push(h_of[w[1]]?.index());
                }
            }
        }
        path.insert((a, b), p);
    }
    let cpos = |hi: usize| {
        let q = cdt.vertex(spade::handles::FixedVertexHandle::from_index(hi)).position();
        [q.x, q.y]
    };
    // Coverage of each CDT triangle: the faces whose polygon (along the
    // paths) holds it. Seeds: the triangles on the inner side of the
    // polygon edges. A flood fill from them does not cross a polygon edge.
    // A polygon whose edges are not all in the CDT, or whose fill gets to
    // the outer face, uses the centroid test.
    let nall = cdt.num_all_faces();
    let mut cover_f = vec![0u16; nall];
    let mut cover_b = vec![0u16; nall];
    let mut stamp = vec![u32::MAX; nall];
    let mut fixed_of = vec![None; nall];
    for fh in cdt.inner_faces() {
        fixed_of[fh.fix().index()] = Some(fh.fix());
    }
    let mut fallback: Vec<(Vec<[f64; 2]>, bool)> = Vec::new();
    let mut inside: Vec<usize> = Vec::new();
    let mut stack: Vec<usize> = Vec::new();
    let mut bnd: std::collections::HashSet<(usize, usize)> = std::collections::HashSet::new();
    for (fi, t) in tri_l.iter().flatten().enumerate() {
        let front = o2(pos[t[0]], pos[t[1]], pos[t[2]]) > 0.0;
        // Polygon vertices (CDT indices), in the order of the face.
        let mut ring: Vec<usize> = Vec::new();
        for i in 0..3 {
            let (a, b) = (t[i], t[(i + 1) % 3]);
            let p = &path[&(a.min(b), a.max(b))];
            if a < b {
                ring.extend_from_slice(&p[..p.len() - 1]);
            } else {
                ring.extend(p[1..].iter().rev());
            }
        }
        bnd.clear();
        inside.clear();
        stack.clear();
        let mut ok = true;
        for k in 0..ring.len() {
            let (x, y) = (ring[k], ring[(k + 1) % ring.len()]);
            bnd.insert((x.min(y), x.max(y)));
            // Inner side: left of x -> y for a front face, right for a back one.
            let (from, to) = if front { (x, y) } else { (y, x) };
            let e = cdt.get_edge_from_neighbors(
                spade::handles::FixedVertexHandle::from_index(from),
                spade::handles::FixedVertexHandle::from_index(to),
            );
            match e.and_then(|e| e.face().as_inner().map(|f| f.fix().index())) {
                Some(g) => {
                    if stamp[g] != fi as u32 {
                        stamp[g] = fi as u32;
                        stack.push(g);
                    }
                }
                None => ok = false,
            }
        }
        while ok {
            let Some(g) = stack.pop() else { break };
            inside.push(g);
            let fh = cdt.face(fixed_of[g].unwrap());
            for e in fh.adjacent_edges() {
                let (x, y) = (e.from().fix().index(), e.to().fix().index());
                if bnd.contains(&(x.min(y), x.max(y))) {
                    continue;
                }
                match e.rev().face().as_inner() {
                    Some(h) => {
                        let h = h.fix().index();
                        if stamp[h] != fi as u32 {
                            stamp[h] = fi as u32;
                            stack.push(h);
                        }
                    }
                    None => ok = false,
                }
            }
        }
        if ok {
            for &g in &inside {
                if front {
                    cover_f[g] += 1;
                } else {
                    cover_b[g] += 1;
                }
            }
        } else {
            fallback.push((ring.iter().map(|&h| cpos(h)).collect(), front));
        }
    }
    let trace = std::env::var("MESHFIX_CONTACT_SHOW").ok().and_then(|x| x.parse::<u32>().ok()) == Some(faces[0]);
    if trace {
        eprintln!("cluster of face {}: {} faces, plane c {:?} n {:?}, fallback polygons {}", faces[0], faces.len(), pl.c, pl.n, fallback.len());
        for (&f, t) in faces.iter().zip(&tri_l) {
            let tt = m.triangles[f as usize];
            let d: Vec<f64> = tt.iter().map(|&x| pl.dist(m.vertices[x as usize])).collect();
            eprintln!("  face {f} {tt:?} rep {t:?} 2d {:?} dist {d:?}", tt.map(|x| pos[local[&x]]));
        }
        let mut hist: HashMap<(u16, u16), usize> = HashMap::new();
        for fh in cdt.inner_faces() {
            let g = fh.fix().index();
            *hist.entry((cover_f[g], cover_b[g])).or_default() += 1;
        }
        eprintln!("  coverage (front, back) -> CDT triangles: {hist:?}");
    }
    let mut tris = Vec::new();
    let mut overlap = false;
    for fh in cdt.inner_faces() {
        let g = fh.fix().index();
        let vs = fh.vertices();
        let (mut nf, mut nb) = (cover_f[g] as u32, cover_b[g] as u32);
        if !fallback.is_empty() {
            let q: Vec<[f64; 2]> = vs.iter().map(|v| [v.position().x, v.position().y]).collect();
            let c = [(q[0][0] + q[1][0] + q[2][0]) / 3.0, (q[0][1] + q[1][1] + q[2][1]) / 3.0];
            for (poly, front) in &fallback {
                if in_polygon(poly, c) {
                    if *front {
                        nf += 1;
                    } else {
                        nb += 1;
                    }
                }
            }
        }
        if nf + nb == 0 {
            continue;
        }
        if nf + nb > 1 {
            overlap = true;
        }
        // The sum of the sides, as in the axis-plane overlay: equal counts
        // (caps that face each other) go; else one triangle stays, turned
        // to the larger side. Thus the winding number field of the input
        // stays the same where the count difference is 0 or 1.
        if nf == nb {
            continue;
        }
        let ids3 = [gid[vs[0].fix().index()], gid[vs[1].fix().index()], gid[vs[2].fix().index()]];
        // CDT triangles are counterclockwise in (u, v): normal +n.
        tris.push(if nf > nb { ids3 } else { [ids3[0], ids3[2], ids3[1]] });
    }
    if !overlap {
        return None;
    }
    // 5. Points on the cluster edges, for the faces outside the cluster.
    let mut on_edge: Vec<(EdgeKey, Vec<u32>)> = Vec::new();
    for (&(a, b), p) in &path {
        if p.len() > 2 {
            on_edge.push((ekey(ids[a], ids[b]), p[1..p.len() - 1].iter().map(|&h| gid[h]).collect()));
        }
    }
    let moves = (0..ids.len())
        .filter(|&i| is_rep[i])
        .map(|i| {
            let p = m.vertices[ids[i] as usize];
            (ids[i], sub(p, scale(pl.n, pl.dist(p))))
        })
        .collect();
    Some(ClusterOut { faces: faces.to_vec(), tris, new_pos, merges, snapped, on_edge, moves })
}

fn uf_find(p: &mut [u32], mut x: u32) -> u32 {
    while p[x as usize] != x {
        let g = p[p[x as usize] as usize];
        p[x as usize] = g;
        x = g;
    }
    x
}

/// Overlay of almost coplanar faces in any plane (see above).
pub fn overlay_contacts(mesh: &mut Mesh) -> ContactReport {
    use rayon::prelude::*;
    let t0 = std::time::Instant::now();
    let mut rep = ContactReport::default();
    CONTACT_LEFT.with(|c| c.borrow_mut().clear());
    mesh.weld_exact();
    let nf = mesh.triangles.len();
    let Some((lo, hi)) = mesh.bbox() else { return rep };
    let rel = std::env::var("MESHFIX_CONTACT_TOL").ok().and_then(|s| s.parse::<f64>().ok()).unwrap_or(CONTACT_TOL);
    let tol = rel * dist(lo, hi);
    rep.tol = tol;
    if nf < 2 || tol <= 0.0 {
        return rep;
    }
    let nrm_area: Vec<(Vec3, f64)> = (0..nf)
        .into_par_iter()
        .map(|f| {
            let [a, b, c] = mesh.tri_pos(f);
            let n = cross(sub(b, a), sub(c, a));
            let l = norm(n);
            if l > 0.0 {
                (scale(n, 1.0 / l), 0.5 * l)
            } else {
                ([0.0; 3], 0.0)
            }
        })
        .collect();
    let nrm: Vec<Vec3> = nrm_area.iter().map(|x| x.0).collect();
    let area: Vec<f64> = nrm_area.iter().map(|x| x.1).collect();
    // Faces with an open edge (a loose flap, the rim of an open part) are
    // not used: the sides of their coverage have no meaning.
    let ef = mesh.edge_faces();
    let open_face: Vec<bool> = mesh
        .triangles
        .iter()
        .map(|t| (0..3).any(|i| ef.get(&ekey(t[i], t[(i + 1) % 3])).map_or(true, |l| l.len() == 1)))
        .collect();
    // 1. Pairs.
    let all: Vec<u32> = (0..nf as u32).collect();
    let m = &*mesh;
    let pairs: Vec<(u32, u32)> = crate::union::candidate_pairs_pad(m, &all, tol)
        .into_par_iter()
        .filter(|&(fa, fb)| {
            let (a, b) = (fa as usize, fb as usize);
            if open_face[a] || open_face[b] || area[a] == 0.0 || area[b] == 0.0 || dot(nrm[a], nrm[b]).abs() < CONTACT_COS {
                return false;
            }
            let (big, small) = if area[a] >= area[b] { (a, b) } else { (b, a) };
            let pb = m.tri_pos(big);
            let ps = m.tri_pos(small);
            let pl = Plane { c: pb[0], n: nrm[big] };
            if ps.iter().any(|&p| pl.dist(p).abs() > tol) {
                return false;
            }
            let (u, v) = plane_basis(nrm[big]);
            let q = |p: Vec3| [dot(p, u), dot(p, v)];
            tri_overlap_2d(pb.map(q), ps.map(q))
        })
        .collect();
    rep.pairs = pairs.len();
    crate::union::dbg_time("contacts pairs", t0);
    if pairs.is_empty() {
        mark_left(mesh, tol);
        return rep;
    }
    // 2. Groups and plane clusters.
    let mut parent: Vec<u32> = (0..nf as u32).collect();
    let mut nbr: HashMap<u32, Vec<u32>> = HashMap::new();
    for &(a, b) in &pairs {
        let (ra, rb) = (uf_find(&mut parent, a), uf_find(&mut parent, b));
        if ra != rb {
            parent[ra.max(rb) as usize] = ra.min(rb);
        }
        nbr.entry(a).or_default().push(b);
        nbr.entry(b).or_default().push(a);
    }
    let mut members: Vec<u32> = nbr.keys().copied().collect();
    let mut member = vec![false; nf];
    for &f in &members {
        member[f as usize] = true;
    }
    members.sort_unstable();
    let mut gmap: HashMap<u32, Vec<u32>> = HashMap::new();
    for f in members {
        let r = uf_find(&mut parent, f);
        gmap.entry(r).or_default().push(f);
    }
    let mut groups: Vec<Vec<u32>> = gmap.into_values().collect();
    groups.sort_by_key(|g| g[0]);
    rep.groups = groups.len();
    let mut in_group = vec![u32::MAX; nf];
    for (gi, g) in groups.iter().enumerate() {
        for &f in g {
            in_group[f as usize] = gi as u32;
        }
    }
    let fits = |f: u32, pl: &Plane| {
        dot(nrm[f as usize], pl.n).abs() >= CONTACT_COS && mesh.tri_pos(f as usize).iter().all(|&p| pl.dist(p).abs() <= tol)
    };
    let mut clusters: Vec<(Vec<u32>, Plane)> = Vec::new();
    let mut grown: Vec<bool> = Vec::new();
    let mut assigned = vec![false; nf];
    for (gi, g) in groups.iter().enumerate() {
        let (pl, dev) = fit_plane(mesh, g, &nrm, &area);
        if dev <= tol && g.iter().all(|&f| dot(nrm[f as usize], pl.n).abs() >= CONTACT_COS) {
            clusters.push((g.clone(), pl));
            grown.push(false);
            continue;
        }
        if std::env::var_os("MESHFIX_CONTACT_NOGROW").is_some() {
            continue;
        }
        // Curved contact: grow clusters from the largest faces.
        let mut order = g.clone();
        order.sort_by(|&a, &b| area[b as usize].total_cmp(&area[a as usize]).then(a.cmp(&b)));
        for &s in &order {
            if assigned[s as usize] {
                continue;
            }
            assigned[s as usize] = true;
            let [p0, _, _] = mesh.tri_pos(s as usize);
            let mut pl = Plane { c: p0, n: nrm[s as usize] };
            let mut cl = vec![s];
            let mut i = 0;
            let mut refit = 8;
            while i < cl.len() {
                let f = cl[i];
                i += 1;
                let t = mesh.triangles[f as usize];
                let mut next: Vec<u32> = nbr.get(&f).cloned().unwrap_or_default();
                for k in 0..3 {
                    if let Some(l) = ef.get(&ekey(t[k], t[(k + 1) % 3])) {
                        next.extend(l.iter().copied().filter(|&h| in_group[h as usize] == gi as u32));
                    }
                }
                for h in next {
                    if !assigned[h as usize] && fits(h, &pl) {
                        assigned[h as usize] = true;
                        cl.push(h);
                    }
                }
                // Fit the plane again when the cluster has doubled.
                if cl.len() >= refit {
                    refit *= 2;
                    let (p2, d2) = fit_plane(mesh, &cl, &nrm, &area);
                    if d2 <= tol {
                        pl = p2;
                    }
                }
            }
            if cl.len() >= 2 {
                let (p2, d2) = fit_plane(mesh, &cl, &nrm, &area);
                clusters.push((cl, if d2 <= tol { p2 } else { pl }));
                grown.push(true);
            }
        }
    }
    rep.clusters = clusters.len();
    crate::union::dbg_time("contacts clusters", t0);
    if std::env::var_os("MESHFIX_TIMING").is_some() {
        let big = clusters.iter().map(|c| c.0.len()).max().unwrap_or(0);
        eprintln!("contacts: {} pairs, {} groups, {} clusters, largest {big}", rep.pairs, rep.groups, rep.clusters);
    }
    // 3. Overlay of each cluster.
    let outs: Vec<Option<ClusterOut>> = clusters
        .par_iter()
        .map(|(faces, pl)| {
            std::panic::catch_unwind(std::panic::AssertUnwindSafe(|| overlay_cluster(m, faces, pl, tol))).unwrap_or(None)
        })
        .collect();
    let mut outs = outs;
    if let Ok(r) = std::env::var("MESHFIX_CONTACT_RANGE") {
        let v: Vec<usize> = r.split(':').filter_map(|x| x.parse().ok()).collect();
        if v.len() == 2 {
            for (i, o) in outs.iter_mut().enumerate() {
                if i < v[0] || i >= v[1] {
                    *o = None;
                }
            }
        }
    }
    if let Some(only) = std::env::var("MESHFIX_CONTACT_ONLY").ok().and_then(|x| x.parse::<u32>().ok()) {
        for (o, (faces, _)) in outs.iter_mut().zip(&clusters) {
            if faces[0] != only {
                *o = None;
            }
        }
    }
    if let Some(p) = std::env::var_os("MESHFIX_CONTACT_DUMP") {
        use std::io::Write;
        if let Ok(mut w) = std::fs::File::create(p) {
            for (i, ((faces, pl), o)) in clusters.iter().zip(&outs).enumerate() {
                let (mut lo, mut hi) = ([f64::INFINITY; 3], [f64::NEG_INFINITY; 3]);
                for &f in faces {
                    for q in mesh.tri_pos(f as usize) {
                        for k in 0..3 {
                            lo[k] = lo[k].min(q[k]);
                            hi[k] = hi[k].max(q[k]);
                        }
                    }
                }
                let (nt, nm, ns) = o.as_ref().map_or((0, 0, 0), |o| (o.tris.len(), o.merges.len(), o.snapped));
                let _ = writeln!(
                    w,
                    "{i} {} {} {} {nt} {nm} {ns} {} {} {} {} {} {} {} {} {} {}",
                    faces.len(),
                    grown[i] as u8,
                    o.is_some() as u8,
                    lo[0], lo[1], lo[2], hi[0], hi[1], hi[2], pl.n[0], pl.n[1], pl.n[2], faces[0]
                );
            }
        }
    }
    let outs: Vec<ClusterOut> = outs.into_iter().flatten().collect();
    if outs.is_empty() {
        mark_left(mesh, tol);
        return rep;
    }

    crate::union::dbg_time("contacts overlay", t0);
    // 4. Assemble. A cluster whose result gives new open edges (a gap to a
    // neighbor cluster, a T-junction that the split misses) is not used;
    // then the rest is assembled again.
    let src = mesh.clone();
    let open0: std::collections::HashSet<EdgeKey> = open_edges(&src).into_iter().map(|x| x.0).collect();
    let mut on = vec![true; outs.len()];
    let guard = std::env::var_os("MESHFIX_CONTACT_NOGUARD").is_none();
    for round in 0..6 {
        let mut r = rep.clone();
        let (m2, owner, origin) = assemble(&src, &outs, &on, &mut r, &member);
        let mut bad = 0;
        if guard && round < 5 {
            // The cluster of the face at the new open edge; for a face
            // outside the clusters, the clusters that have both ends (else
            // either end).
            let empty: Vec<u32> = Vec::new();
            for ((a, b), f) in open_edges(&m2) {
                if open0.contains(&ekey(a, b)) {
                    continue;
                }
                let mut cs: Vec<u32> = Vec::new();
                if origin[f] < LEFT {
                    cs.push(origin[f]);
                } else {
                    let (oa, ob) = (owner.get(&a).unwrap_or(&empty), owner.get(&b).unwrap_or(&empty));
                    cs.extend(oa.iter().filter(|c| ob.contains(c)));
                    if cs.is_empty() {
                        cs.extend(oa.iter().chain(ob.iter()));
                    }
                }
                for c in cs {
                    if on[c as usize] {
                        on[c as usize] = false;
                        bad += 1;
                    }
                }
            }
        }
        if bad == 0 {
            *mesh = m2;
            rep = r;
            rep.undone = on.iter().filter(|&&x| !x).count();
            if let Some(p) = std::env::var_os("MESHFIX_CONTACT_UNDONE") {
                use std::io::Write;
                if let Ok(mut w) = std::fs::File::create(p) {
                    for (o, &x) in outs.iter().zip(&on) {
                        if !x {
                            let [p, q, r] = src.tri_pos(o.faces[0] as usize);
                            let c = scale(add(add(p, q), r), 1.0 / 3.0);
                            let _ = writeln!(w, "{} {} {} {} {} {} {} {}", o.faces[0], o.faces.len(), o.tris.len(), o.merges.len(), o.snapped, c[0], c[1], c[2]);
                        }
                    }
                }
            }
            break;
        }
    }
    mesh.drop_collapsed();
    mesh.compact();
    mark_left(mesh, tol);
    rep
}

/// Faces of contacts that are left (clusters not used, or a gap up to
/// LEFT_TOL x the tolerance) go to CONTACT_LEFT: the winding union does not
/// join them into one patch with other faces.
fn mark_left(mesh: &Mesh, tol: f64) {
    let lt = std::env::var("MESHFIX_LEFT_TOL").ok().and_then(|x| x.parse::<f64>().ok()).unwrap_or(LEFT_TOL) * tol;
    if lt > 0.0 {
        let keys = contact_faces(mesh, lt).into_iter().map(|f| face_key(mesh, f)).collect();
        CONTACT_LEFT.with(|c| *c.borrow_mut() = keys);
    }
}

/// Faces of the almost parallel pairs that overlap within `tol` (the pair
/// test of overlay_contacts, without the open-edge rule).
fn contact_faces(mesh: &Mesh, tol: f64) -> Vec<usize> {
    use rayon::prelude::*;
    let nf = mesh.triangles.len();
    let na: Vec<(Vec3, f64)> = (0..nf)
        .into_par_iter()
        .map(|f| {
            let [a, b, c] = mesh.tri_pos(f);
            let n = cross(sub(b, a), sub(c, a));
            let l = norm(n);
            if l > 0.0 {
                (scale(n, 1.0 / l), 0.5 * l)
            } else {
                ([0.0; 3], 0.0)
            }
        })
        .collect();
    let all: Vec<u32> = (0..nf as u32).collect();
    let pairs: Vec<(u32, u32)> = crate::union::candidate_pairs_pad(mesh, &all, tol)
        .into_par_iter()
        .filter(|&(fa, fb)| {
            let (a, b) = (fa as usize, fb as usize);
            if na[a].1 == 0.0 || na[b].1 == 0.0 || dot(na[a].0, na[b].0).abs() < CONTACT_COS {
                return false;
            }
            let (big, small) = if na[a].1 >= na[b].1 { (a, b) } else { (b, a) };
            let pb = mesh.tri_pos(big);
            let ps = mesh.tri_pos(small);
            let pl = Plane { c: pb[0], n: na[big].0 };
            if ps.iter().any(|&p| pl.dist(p).abs() > tol) {
                return false;
            }
            let (u, v) = plane_basis(na[big].0);
            let q = |p: Vec3| [dot(p, u), dot(p, v)];
            tri_overlap_2d(pb.map(q), ps.map(q))
        })
        .collect();
    let mut out: Vec<usize> = pairs.iter().flat_map(|&(a, b)| [a as usize, b as usize]).collect();
    out.sort_unstable();
    out.dedup();
    out
}

/// Open edges (one face) and their face, with the vertices welded by exact
/// position.
fn open_edges(m: &Mesh) -> Vec<(EdgeKey, usize)> {
    let w = weld_map(m);
    let mut cnt: HashMap<EdgeKey, (u32, usize)> = HashMap::with_capacity(m.triangles.len() * 3 / 2);
    for (f, t) in m.triangles.iter().enumerate() {
        let t = [w[t[0] as usize], w[t[1] as usize], w[t[2] as usize]];
        if t[0] == t[1] || t[1] == t[2] || t[2] == t[0] {
            continue;
        }
        for i in 0..3 {
            let e = cnt.entry(ekey(t[i], t[(i + 1) % 3])).or_insert((0, f));
            e.0 += 1;
        }
    }
    cnt.into_iter().filter(|&(_, (c, _))| c == 1).map(|(e, (_, f))| (e, f)).collect()
}

/// Each vertex to the first vertex with the same position.
fn weld_map(m: &Mesh) -> Vec<u32> {
    let mut seen: HashMap<[u64; 3], u32> = HashMap::with_capacity(m.vertices.len());
    m.vertices
        .iter()
        .enumerate()
        .map(|(i, p)| {
            let k = [p[0], p[1], p[2]].map(|x| if x == 0.0 { 0 } else { x.to_bits() });
            *seen.entry(k).or_insert(i as u32)
        })
        .collect()
}

/// Apply the cluster results with `on` set: merges, moves, new points, the
/// new faces, and the split of the other faces. Returns the mesh (vertex
/// ids: the input ids, then the new points; not compacted) and, for each
/// vertex (welded by position), the clusters that use it, and for each
/// face its cluster (u32::MAX: a face outside the clusters).
fn assemble(
    src: &Mesh,
    outs: &[ClusterOut],
    on: &[bool],
    rep: &mut ContactReport,
    member: &[bool],
) -> (Mesh, HashMap<u32, Vec<u32>>, Vec<u32>) {
    let mut mesh = src.clone();
    let nf = mesh.triangles.len();
    let nv = mesh.vertices.len();
    let mut vp: Vec<u32> = (0..nv as u32).collect();
    let mut remove = vec![false; nf];
    let mut add_tris: Vec<[u32; 3]> = Vec::new();
    let mut add_owner: Vec<u32> = Vec::new();
    let mut edge_points: HashMap<EdgeKey, Vec<u32>> = HashMap::new();
    let mut move_cnt: HashMap<u32, (u32, Vec3)> = HashMap::new();
    let mut new_pos: Vec<Vec3> = Vec::new();
    let mut in_owner: Vec<(u32, u32)> = Vec::new();
    for (ci, o) in outs.iter().enumerate() {
        if !on[ci] {
            continue;
        }
        for &(a, b) in &o.merges {
            let (ra, rb) = (uf_find(&mut vp, a), uf_find(&mut vp, b));
            if ra != rb {
                vp[ra.max(rb) as usize] = ra.min(rb);
                rep.merged += 1;
            }
        }
    }
    for (ci, o) in outs.iter().enumerate() {
        if !on[ci] {
            continue;
        }
        rep.changed += 1;
        rep.replaced += o.faces.len();
        rep.added += o.tris.len();
        rep.snapped += o.snapped;
        for &f in &o.faces {
            remove[f as usize] = true;
            for &x in &src.triangles[f as usize] {
                in_owner.push((x, ci as u32));
            }
        }
        let base = (nv + new_pos.len()) as u32;
        new_pos.extend_from_slice(&o.new_pos);
        let g = |id: u32| if id >= NEW { base + (id - NEW) } else { id };
        for t in &o.tris {
            add_tris.push([g(t[0]), g(t[1]), g(t[2])]);
            add_owner.push(ci as u32);
        }
        for (e, pts) in &o.on_edge {
            edge_points.entry(*e).or_default().extend(pts.iter().map(|&x| g(x)));
        }
        for &(id, p) in &o.moves {
            move_cnt.entry(id).or_insert((0, p)).0 += 1;
        }
    }
    // Vertices of one cluster only go onto its plane.
    if std::env::var_os("MESHFIX_CONTACT_NOMOVE").is_none() {
        for (id, (cnt, p)) in move_cnt {
            if cnt == 1 && uf_find(&mut vp, id) == id && mesh.vertices[id as usize] != p {
                mesh.vertices[id as usize] = p;
                rep.moved += 1;
            }
        }
    }
    mesh.vertices.extend_from_slice(&new_pos);
    vp.extend((nv as u32)..(nv + new_pos.len()) as u32);
    let nvall = mesh.vertices.len();
    let remap: Vec<u32> = (0..nvall as u32).map(|i| uf_find(&mut vp, i)).collect();
    let mut ep2: HashMap<EdgeKey, Vec<u32>> = HashMap::new();
    for ((a, b), pts) in edge_points {
        let k = ekey(remap[a as usize], remap[b as usize]);
        if k.0 != k.1 {
            ep2.entry(k).or_default().extend(pts.into_iter().map(|x| remap[x as usize]));
        }
    }
    // Split the faces outside the clusters at the new points. The new faces
    // of a cluster go through the split too: a neighbor cluster can have
    // points on their outer edges. Points within the tolerance of two edges
    // can make a cycle of splits: a face is split 16 times at most.
    let mut out: Vec<[u32; 3]> = Vec::with_capacity(nf + add_tris.len());
    let mut origin: Vec<u32> = Vec::with_capacity(nf + add_tris.len());
    let mut queue: Vec<([u32; 3], u8, u32)> = (0..nf)
        .filter(|&f| !remove[f])
        .map(|f| (mesh.triangles[f], if member[f] { LEFT } else { u32::MAX }))
        .chain(add_tris.iter().copied().zip(add_owner.iter().copied()))
        .map(|(t, o)| ([remap[t[0] as usize], remap[t[1] as usize], remap[t[2] as usize]], 0u8, o))
        .collect();
    while let Some((t, depth, org)) = queue.pop() {
        if t[0] == t[1] || t[1] == t[2] || t[2] == t[0] {
            continue;
        }
        let mut split = false;
        for i in 0..3 {
            if depth >= 16 {
                break;
            }
            let (a, b, c) = (t[i], t[(i + 1) % 3], t[(i + 2) % 3]);
            let Some(pts) = ep2.get(&ekey(a, b)) else { continue };
            let (pa, pb) = (mesh.vertices[a as usize], mesh.vertices[b as usize]);
            let d = sub(pb, pa);
            let l2 = dot(d, d);
            let mut sp: Vec<(f64, u32)> = pts
                .iter()
                .filter(|&&x| x != a && x != b && x != c)
                .map(|&x| (dot(sub(mesh.vertices[x as usize], pa), d), x))
                .filter(|&(t, _)| t > 0.0 && t < l2)
                .collect();
            if sp.is_empty() {
                continue;
            }
            sp.sort_by(|x, y| x.0.total_cmp(&y.0));
            sp.dedup_by_key(|x| x.1);
            let mut ch = vec![a];
            ch.extend(sp.into_iter().map(|x| x.1));
            ch.push(b);
            for w in ch.windows(2) {
                queue.push(([w[0], w[1], c], depth + 1, org));
            }
            split = true;
            break;
        }
        if !split {
            out.push(t);
            origin.push(org);
        }
    }
    // Weld the vertices of the clusters that are nearer than 1/50 of the
    // tolerance: points of two clusters near a common corner, crossing
    // points next to a vertex. Else they make faces of zero area.
    let wr = MICRO_WELD * rep.tol;
    if wr > 0.0 {
        let mut touched = vec![false; mesh.vertices.len()];
        for &(x, _) in &in_owner {
            touched[remap[x as usize] as usize] = true;
        }
        for t in &add_tris {
            for &x in t {
                touched[remap[x as usize] as usize] = true;
            }
        }
        let mut grid = Grid::new(wr);
        let mut wm: Vec<u32> = (0..mesh.vertices.len() as u32).collect();
        let mut near = Vec::new();
        for i in 0..mesh.vertices.len() {
            if !touched[i] {
                continue;
            }
            let p = mesh.vertices[i];
            near.clear();
            grid.near(p, &mut near);
            match near.iter().copied().find(|&j| dist2(p, mesh.vertices[j as usize]) <= wr * wr) {
                Some(j) => wm[i] = j,
                None => grid.insert(i as u32, p),
            }
        }
        let mut o2 = Vec::with_capacity(out.len());
        let mut g2 = Vec::with_capacity(out.len());
        for (t, g) in out.into_iter().zip(origin) {
            let t = [wm[t[0] as usize], wm[t[1] as usize], wm[t[2] as usize]];
            if t[0] != t[1] && t[1] != t[2] && t[2] != t[0] {
                o2.push(t);
                g2.push(g);
            }
        }
        out = o2;
        origin = g2;
    }
    mesh.triangles = out;
    // Owners, by welded vertex.
    let w = weld_map(&mesh);
    let mut owner: HashMap<u32, Vec<u32>> = HashMap::new();
    let mut add = |x: u32, c: u32| {
        let l = owner.entry(w[remap[x as usize] as usize]).or_default();
        if !l.contains(&c) {
            l.push(c);
        }
    };
    for (x, c) in in_owner {
        add(x, c);
    }
    for (t, &c) in add_tris.iter().zip(&add_owner) {
        for &x in t {
            add(x, c);
        }
    }
    (mesh, owner, origin)
}
