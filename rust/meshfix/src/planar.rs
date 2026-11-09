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
