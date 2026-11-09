//! The repair steps. Each step changes the mesh and returns a count for the
//! report. The order and the tolerances are in lib.rs (PIPELINE). The
//! comment of each step tells what it does.

use crate::math::*;
use crate::mesh::*;
use std::cmp::{Ordering, Reverse};
use std::collections::{BinaryHeap, HashSet, VecDeque};

// ---- Stitch ------------------------------------------------------------

#[derive(Clone, Copy)]
struct Cand {
    d: f64,
    a: u32,
    b: u32,
}
impl PartialEq for Cand {
    fn eq(&self, o: &Self) -> bool {
        self.cmp(o) == Ordering::Equal
    }
}
impl Eq for Cand {}
impl PartialOrd for Cand {
    fn partial_cmp(&self, o: &Self) -> Option<Ordering> {
        Some(self.cmp(o))
    }
}
impl Ord for Cand {
    fn cmp(&self, o: &Self) -> Ordering {
        self.d.total_cmp(&o.d).then(self.a.cmp(&o.a)).then(self.b.cmp(&o.b))
    }
}

fn find(p: &mut [u32], mut x: u32) -> u32 {
    while p[x as usize] != x {
        let g = p[p[x as usize] as usize];
        p[x as usize] = g;
        x = g;
    }
    x
}

/// Merge boundary vertices in pairs, the nearest pair first, while the
/// distance is not more than `tol`. The vertex with the lower index stays.
pub fn merge_boundary_vertices(mesh: &mut Mesh, tol: f64) -> usize {
    let ef = mesh.edge_faces();
    let nv = mesh.vertices.len();
    let mut is_b = vec![false; nv];
    for (k, f) in &ef {
        if f.len() == 1 {
            is_b[k.0 as usize] = true;
            is_b[k.1 as usize] = true;
        }
    }
    let mut grid = Grid::new(tol);
    for v in 0..nv {
        if is_b[v] {
            grid.insert(v as u32, mesh.vertices[v]);
        }
    }
    let (mut heap, mut near) = (BinaryHeap::new(), Vec::new());
    for v in 0..nv {
        if !is_b[v] {
            continue;
        }
        near.clear();
        grid.near(mesh.vertices[v], &mut near);
        for &w in &near {
            if (w as usize) <= v {
                continue;
            }
            let d = dist(mesh.vertices[v], mesh.vertices[w as usize]);
            if d <= tol {
                heap.push(Reverse(Cand { d, a: v as u32, b: w }));
            }
        }
    }
    let mut parent: Vec<u32> = (0..nv as u32).collect();
    let mut merges = 0;
    while let Some(Reverse(c)) = heap.pop() {
        let (ra, rb) = (find(&mut parent, c.a), find(&mut parent, c.b));
        if ra == rb {
            continue;
        }
        let (ra, rb) = (ra.min(rb), ra.max(rb));
        if (ra, rb) != (c.a.min(c.b), c.a.max(c.b)) {
            // One end is merged already: measure again from the survivors.
            let d = dist(mesh.vertices[ra as usize], mesh.vertices[rb as usize]);
            if d <= tol {
                heap.push(Reverse(Cand { d, a: ra, b: rb }));
            }
            continue;
        }
        parent[rb as usize] = ra;
        merges += 1;
    }
    if merges > 0 {
        let remap: Vec<u32> = (0..nv as u32).map(|v| find(&mut parent, v)).collect();
        mesh.apply_remap(&remap);
    }
    merges
}

/// Stitch = exact weld, boundary-vertex merge, T-junction split, exact weld.
pub fn stitch(mesh: &mut Mesh, tol: f64) -> usize {
    mesh.weld_exact()
        + merge_boundary_vertices(mesh, tol)
        + mesh.split_boundary_tjunctions(tol)
        + mesh.weld_exact()
}

// ---- CloseTrivialHoles -------------------------------------------------

/// Close each hole of 3 edges with one triangle. A hole of 2 edges cannot
/// occur in an indexed mesh with one edge per vertex pair.
pub fn close_trivial_holes(mesh: &mut Mesh) -> usize {
    let mut n = 0;
    for l in mesh.boundary_loops() {
        if l.len() == 3 {
            mesh.triangles.push([l[0], l[2], l[1]]);
            n += 1;
        }
    }
    n
}

// ---- RemoveDoubleTriangles ---------------------------------------------

/// Weld with `weld_tol`, then remove each triangle that uses the same three
/// vertices as another triangle (either orientation).
pub fn remove_double_triangles(mesh: &mut Mesh, weld_tol: f64) -> usize {
    mesh.weld(weld_tol);
    let mut seen = HashSet::with_capacity(mesh.triangles.len());
    let mut keep = vec![true; mesh.triangles.len()];
    let mut removed = 0;
    // Go from the end: the later copy stays, the earlier copy goes.
    for (f, t) in mesh.triangles.iter().enumerate().rev() {
        let mut k = *t;
        k.sort_unstable();
        if !seen.insert(k) {
            keep[f] = false;
            removed += 1;
        }
    }
    if removed > 0 {
        mesh.retain_faces(&keep);
    }
    mesh.weld_exact();
    removed
}

// ---- FixFlippedTriangles -----------------------------------------------

/// Make the orientation the same in each shell. Faces are put in two
/// classes through the manifold edges. The class with the smaller area
/// turns over. A shell that cannot be oriented (Moebius) stays as it is.
pub fn fix_flipped(mesh: &mut Mesh) -> usize {
    let ef = mesh.edge_faces();
    let nf = mesh.triangles.len();
    let mut class = vec![0u8; nf];
    let (mut queue, mut members, mut flipped) = (VecDeque::new(), Vec::new(), 0);
    for seed in 0..nf {
        if class[seed] != 0 {
            continue;
        }
        members.clear();
        let mut area = [0f64; 3];
        let mut conflict = false;
        class[seed] = 1;
        queue.push_back(seed);
        while let Some(f) = queue.pop_front() {
            members.push(f);
            let [p0, p1, p2] = mesh.tri_pos(f);
            area[class[f] as usize] += tri_area(p0, p1, p2);
            let t = mesh.triangles[f];
            for i in 0..3 {
                let (a, b) = (t[i], t[(i + 1) % 3]);
                let adj = &ef[&ekey(a, b)];
                if adj.len() != 2 {
                    continue;
                }
                let g = (if adj[0] as usize == f { adj[1] } else { adj[0] }) as usize;
                if g == f {
                    continue;
                }
                let want = if directed(&mesh.triangles[g], b, a) { class[f] } else { 3 - class[f] };
                if class[g] == 0 {
                    class[g] = want;
                    queue.push_back(g);
                } else if class[g] != want {
                    conflict = true;
                }
            }
        }
        if conflict {
            continue;
        }
        let flip = if area[1] >= area[2] { 2 } else { 1 };
        for &f in &members {
            if class[f] == flip {
                mesh.triangles[f].swap(1, 2);
                flipped += 1;
            }
        }
    }
    flipped
}

// ---- CloseAllHoles -----------------------------------------------------

/// Holes up to this size get the minimum-area triangulation (good for a
/// curved hole). Larger holes get a CDT in the plane of the hole.
pub const MAX_DP_HOLE: usize = 200;
/// When the CDT fails (the hole does not project to a simple polygon), the
/// minimum-area triangulation is used up to this size, then a fan.
pub const MAX_DP_FALLBACK: usize = 800;

/// Fan from the centroid: the last fallback.
fn fan(v: &mut Vec<Vec3>, poly: &[u32]) -> Vec<[u32; 3]> {
    let mut c = [0.0; 3];
    for &i in poly {
        c = add(c, v[i as usize]);
    }
    let ci = v.len() as u32;
    v.push(scale(c, 1.0 / poly.len() as f64));
    (0..poly.len()).map(|k| [poly[k], poly[(k + 1) % poly.len()], ci]).collect()
}

/// Constrained Delaunay triangulation of a hole in its best-fit plane
/// (Newell normal). None when the projected polygon is not simple.
pub fn triangulate_cdt(v: &[Vec3], poly: &[u32]) -> Option<Vec<[u32; 3]>> {
    use spade::{ConstrainedDelaunayTriangulation, Point2, Triangulation};
    let n = poly.len();
    let mut nn = [0.0; 3];
    for i in 0..n {
        let (a, b) = (v[poly[i] as usize], v[poly[(i + 1) % n] as usize]);
        nn[0] += (a[1] - b[1]) * (a[2] + b[2]);
        nn[1] += (a[2] - b[2]) * (a[0] + b[0]);
        nn[2] += (a[0] - b[0]) * (a[1] + b[1]);
    }
    let l = norm(nn);
    if l == 0.0 {
        return None;
    }
    let nz = scale(nn, 1.0 / l);
    let helper = if nz[0].abs() < 0.9 { [1.0, 0.0, 0.0] } else { [0.0, 1.0, 0.0] };
    let e1 = {
        let u = cross(nz, helper);
        scale(u, 1.0 / norm(u))
    };
    let e2 = cross(nz, e1);
    let mut cdt: ConstrainedDelaunayTriangulation<Point2<f64>> = ConstrainedDelaunayTriangulation::new();
    let mut hs = Vec::with_capacity(n);
    let mut h2i = vec![usize::MAX; n];
    for (i, &id) in poly.iter().enumerate() {
        let p = v[id as usize];
        let h = cdt.insert(Point2::new(dot(p, e1), dot(p, e2))).ok()?;
        if h.index() >= n || h2i[h.index()] != usize::MAX {
            return None; // two rim vertices at one projected point
        }
        h2i[h.index()] = i;
        hs.push(h);
    }
    for i in 0..n {
        let (a, b) = (hs[i], hs[(i + 1) % n]);
        if !cdt.can_add_constraint(a, b) {
            return None;
        }
        cdt.add_constraint(a, b);
        if !cdt.exists_constraint(a, b) {
            return None; // a rim vertex lies on this rim edge
        }
    }
    // Remove the faces outside the rim (reachable from the hull).
    let faces: Vec<_> = cdt.inner_faces().collect();
    let pos: std::collections::HashMap<usize, usize> =
        faces.iter().enumerate().map(|(i, f)| (f.fix().index(), i)).collect();
    let mut outside = vec![false; faces.len()];
    let mut queue = VecDeque::new();
    for (i, f) in faces.iter().enumerate() {
        if f.adjacent_edges().iter().any(|e| e.rev().face().is_outer() && !e.is_constraint_edge()) {
            outside[i] = true;
            queue.push_back(i);
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
    // The poly runs counter-clockwise around the Newell normal, thus the CCW
    // CDT faces have the orientation of the poly.
    let out: Vec<[u32; 3]> = faces
        .iter()
        .enumerate()
        .filter(|(i, _)| !outside[*i])
        .map(|(_, f)| {
            let vs = f.vertices();
            [poly[h2i[vs[0].fix().index()]], poly[h2i[vs[1].fix().index()]], poly[h2i[vs[2].fix().index()]]]
        })
        .collect();
    (out.len() == n - 2).then_some(out)
}

/// Weight of one fill triangle: the area, plus a small part of the longest
/// edge squared. All triangulations of a flat convex hole have the same
/// area, so the second term selects the one without slivers. A triangle
/// outside a flat concave hole adds much area, so the area term rejects it.
fn fill_weight(a: Vec3, b: Vec3, c: Vec3) -> f64 {
    let l = dist2(a, b).max(dist2(b, c)).max(dist2(c, a));
    tri_area(a, b, c) + 1e-3 * l
}

/// Triangulation of a closed polygon with the minimum total weight
/// (dynamic programming, O(n^3)). A diagonal that is already an edge of the
/// mesh gets an infinite weight: it would give an edge with 4 faces. When
/// no triangulation is possible without such a diagonal, the weights apply
/// without this rule.
fn triangulate_min_area(v: &[Vec3], poly: &[u32], ef: &EdgeFaces) -> Vec<[u32; 3]> {
    let n = poly.len();
    let used = |i: usize, j: usize| ef.contains_key(&ekey(poly[i], poly[j]));
    let mut out = triangulate_dp(v, poly, &|i, m, j| {
        (m > i + 1 && used(i, m)) || (j > m + 1 && used(m, j)) || (!(i == 0 && j == n - 1) && used(i, j))
    });
    if out.is_empty() {
        out = triangulate_dp(v, poly, &|_, _, _| false);
    }
    out
}

fn triangulate_dp(v: &[Vec3], poly: &[u32], banned: &dyn Fn(usize, usize, usize) -> bool) -> Vec<[u32; 3]> {
    let n = poly.len();
    let p = |i: usize| v[poly[i] as usize];
    let mut w = vec![f64::INFINITY; n * n];
    let mut o = vec![0usize; n * n];
    for i in 0..n - 1 {
        w[i * n + i + 1] = 0.0;
    }
    for len in 2..n {
        for i in 0..n - len {
            let j = i + len;
            for m in i + 1..j {
                if banned(i, m, j) {
                    continue;
                }
                let c = w[i * n + m] + w[m * n + j] + fill_weight(p(i), p(m), p(j));
                if c < w[i * n + j] {
                    w[i * n + j] = c;
                    o[i * n + j] = m;
                }
            }
        }
    }
    if !w[n - 1].is_finite() {
        return Vec::new();
    }
    let mut out = Vec::with_capacity(n.saturating_sub(2));
    let mut st = vec![(0usize, n - 1)];
    while let Some((i, j)) = st.pop() {
        if j < i + 2 {
            continue;
        }
        let m = o[i * n + j];
        out.push([poly[i], poly[m], poly[j]]);
        st.push((i, m));
        st.push((m, j));
    }
    out
}

/// Close each boundary loop. Returns the number of holes closed.
pub fn close_all_holes(mesh: &mut Mesh) -> usize {
    let loops = mesh.boundary_loops();
    let mut ef = mesh.edge_faces();
    for l in &loops {
        // The fill faces go opposite to the rim.
        let poly: Vec<u32> = l.iter().rev().copied().collect();
        if poly.len() <= MAX_DP_HOLE {
            let tris = triangulate_min_area(&mesh.vertices, &poly, &ef);
            for t in &tris {
                let f = mesh.triangles.len() as u32;
                for i in 0..3 {
                    ef.entry(ekey(t[i], t[(i + 1) % 3])).or_default().push(f);
                }
                mesh.triangles.push(*t);
            }
        } else {
            // Large hole: CDT in the plane of the hole. It cannot overlap
            // itself when the hole is a simple polygon in that plane.
            let tris = triangulate_cdt(&mesh.vertices, &poly)
                .or_else(|| (poly.len() <= MAX_DP_FALLBACK).then(|| triangulate_min_area(&mesh.vertices, &poly, &ef)))
                .unwrap_or_else(|| fan(&mut mesh.vertices, &poly));
            for t in &tris {
                let f = mesh.triangles.len() as u32;
                for i in 0..3 {
                    ef.entry(ekey(t[i], t[(i + 1) % 3])).or_default().push(f);
                }
                mesh.triangles.push(*t);
            }
        }
    }
    loops.len()
}

// ---- Wrap --------------------------------------------------------------
//
// Like the Windows Wrap: split non-manifold edges and vertices, turn each
// closed shell so that the normals point out, make the union of the shells
// that intersect (union.rs), and remove each closed shell that is fully
// inside another closed shell. The Windows Wrap also refines the mesh. This
// one does not.

fn shell_bbox(mesh: &Mesh, shell: &[u32]) -> (Vec3, Vec3) {
    let mut lo = [f64::INFINITY; 3];
    let mut hi = [f64::NEG_INFINITY; 3];
    for &f in shell {
        for p in mesh.tri_pos(f as usize) {
            for k in 0..3 {
                lo[k] = lo[k].min(p[k]);
                hi[k] = hi[k].max(p[k]);
            }
        }
    }
    (lo, hi)
}

/// Returns the number of inner shells removed.
pub fn wrap(mesh: &mut Mesh) -> usize {
    wrap_report(mesh).0
}

/// Wrap with the union report: (count for the step, union report).
pub fn wrap_report(mesh: &mut Mesh) -> (usize, crate::union::UnionReport) {
    // Two bodies on one edge are one edge-connected shell that is not
    // closed. After the split each body is a closed shell of its own.
    let t = std::time::Instant::now();
    split_nonmanifold(mesh);
    crate::union::dbg_time("wrap split", t);
    orient_closed_shells(mesh);
    crate::union::dbg_time("wrap orient", t);
    // The union uses a third-party CDT. A panic in it must not stop the
    // repair: the mesh then stays as it was before the union.
    let backup = mesh.clone();
    let info0 = crate::mesh_info(mesh);
    let mut rep = match std::panic::catch_unwind(std::panic::AssertUnwindSafe(|| crate::union::union_shells(mesh))) {
        Ok(r) => r,
        Err(_) => {
            *mesh = backup.clone();
            crate::union::UnionReport { panicked: true, ..Default::default() }
        }
    };
    // The union must not make the mesh worse. If it gives new open or
    // non-manifold edges (for example where a cut curve runs into a face
    // contact), the mesh goes back to the state before the union.
    if rep.pairs > 0 && !rep.panicked {
        let info1 = crate::mesh_info(mesh);
        let bad = (info1.boundary_edges + info1.non_manifold_edges)
            .saturating_sub(info0.boundary_edges + info0.non_manifold_edges);
        if bad > 0 {
            *mesh = backup;
            rep.rolled_back = true;
            rep.bad_edges = bad;
            rep.removed_patches = 0;
        }
    }
    crate::union::dbg_time("wrap union", t);
    let n = remove_nested(mesh);
    crate::union::dbg_time("wrap nested", t);
    (n + rep.removed_patches, rep)
}

fn orient_closed_shells(mesh: &mut Mesh) {
    let ef = mesh.edge_faces();
    let shells = components(mesh, &ef);
    for s in &shells {
        if shell_closed(mesh, &ef, s) && shell_volume(mesh, s) < 0.0 {
            for &f in s {
                mesh.triangles[f as usize].swap(1, 2);
            }
        }
    }
}

/// Remove each closed shell that is fully inside another closed shell.
fn remove_nested(mesh: &mut Mesh) -> usize {
    let ef = mesh.edge_faces();
    let shells = components(mesh, &ef);
    let closed: Vec<bool> = shells.iter().map(|s| shell_closed(mesh, &ef, s)).collect();
    let boxes: Vec<(Vec3, Vec3)> = shells.iter().map(|s| shell_bbox(mesh, s)).collect();
    let inside_box = |i: usize, j: usize| {
        (0..3).all(|k| boxes[i].0[k] >= boxes[j].0[k] && boxes[i].1[k] <= boxes[j].1[k])
    };
    let mut drop = vec![false; shells.len()];
    let mut trees: std::collections::HashMap<usize, crate::union::Fwn> = std::collections::HashMap::new();
    for i in 0..shells.len() {
        if !closed[i] {
            continue;
        }
        let step = (shells[i].len() / 64).max(1);
        let pts: Vec<Vec3> = shells[i]
            .iter()
            .step_by(step)
            .map(|&f| {
                let [a, b, c] = mesh.tri_pos(f as usize);
                scale(add(add(a, b), c), 1.0 / 3.0)
            })
            .collect();
        for j in 0..shells.len() {
            if i == j || !closed[j] || drop[j] || !inside_box(i, j) {
                continue;
            }
            // Winding number of shell j (fast tree sum): 1 inside, 0 outside.
            let fwn = trees.entry(j).or_insert_with(|| crate::union::Fwn::new(mesh, &shells[j]));
            if pts.iter().all(|&p| fwn.eval(p, None) > 0.5) {
                drop[i] = true;
                break;
            }
        }
    }
    let mut keep = vec![true; mesh.triangles.len()];
    let mut n = 0;
    for (s, &d) in shells.iter().zip(&drop) {
        if d {
            n += 1;
            for &f in s {
                keep[f as usize] = false;
            }
        }
    }
    if n > 0 {
        mesh.retain_faces(&keep);
    }
    n
}

// ---- RemoveDegeneratedFaces --------------------------------------------

/// Height of a triangle over its longest edge.
fn height(a: Vec3, b: Vec3, c: Vec3) -> f64 {
    let l = dist(a, b).max(dist(b, c)).max(dist(c, a));
    if l <= 0.0 {
        0.0
    } else {
        2.0 * tri_area(a, b, c) / l
    }
}

/// Faces of each vertex (compressed rows): faces of v = list[start[v]..start[v+1]].
struct VertexFaces {
    start: Vec<u32>,
    list: Vec<u32>,
}

impl VertexFaces {
    fn new(mesh: &Mesh) -> Self {
        let nv = mesh.vertices.len();
        let mut start = vec![0u32; nv + 1];
        for t in &mesh.triangles {
            for &v in t {
                start[v as usize + 1] += 1;
            }
        }
        for i in 0..nv {
            start[i + 1] += start[i];
        }
        let mut fill = start.clone();
        let mut list = vec![0u32; start[nv] as usize];
        for (f, t) in mesh.triangles.iter().enumerate() {
            for &v in t {
                list[fill[v as usize] as usize] = f as u32;
                fill[v as usize] += 1;
            }
        }
        Self { start, list }
    }
    fn of(&self, v: u32) -> &[u32] {
        &self.list[self.start[v as usize] as usize..self.start[v as usize + 1] as usize]
    }
    /// Faces that use edge (a, b).
    fn edge(&self, mesh: &Mesh, a: u32, b: u32) -> Vec<u32> {
        self.of(a).iter().copied().filter(|&f| mesh.triangles[f as usize].contains(&b)).collect()
    }
    /// Sorted neighbour vertices of v.
    fn ring(&self, mesh: &Mesh, v: u32) -> Vec<u32> {
        let mut r: Vec<u32> = Vec::with_capacity(8);
        for &f in self.of(v) {
            for &w in &mesh.triangles[f as usize] {
                if w != v {
                    r.push(w);
                }
            }
        }
        r.sort_unstable();
        r.dedup();
        r
    }
    /// True when an edge at v has other than two faces.
    fn on_rim(&self, mesh: &Mesh, v: u32) -> bool {
        self.ring(mesh, v).into_iter().any(|w| self.edge(mesh, v, w).len() != 2)
    }
}

/// Collapse each edge shorter than `tol` (needle). Then, for each face with
/// a height below `tol` (cap), flip its longest edge, or remove the face
/// when the edge is on the boundary. Returns the number of changes.
pub fn remove_degenerated_faces(mesh: &mut Mesh, tol: f64) -> usize {
    let mut total = 0;
    for _ in 0..64 {
        // A fast scan first: most rounds end here.
        let mut short = false;
        let mut caps = false;
        for f in 0..mesh.triangles.len() {
            let [p0, p1, p2] = mesh.tri_pos(f);
            let lens = [dist(p0, p1), dist(p1, p2), dist(p2, p0)];
            let lmax = lens[0].max(lens[1]).max(lens[2]);
            if lens[0].min(lens[1]).min(lens[2]) < tol {
                short = true;
            }
            if lmax > 0.0 && 2.0 * tri_area(p0, p1, p2) / lmax < tol {
                caps = true;
            }
        }
        if !short && !caps {
            break;
        }
        let vf = VertexFaces::new(mesh);
        let nv = mesh.vertices.len();

        if short {
            let mut remap: Vec<u32> = (0..nv as u32).collect();
            let mut locked = vec![false; nv];
            let mut collapsed = 0;
            for fi in 0..mesh.triangles.len() {
                let t = mesh.triangles[fi];
                for i in 0..3 {
                    let (a, b) = (t[i], t[(i + 1) % 3]);
                    if locked[a as usize] || locked[b as usize] {
                        continue;
                    }
                    let (pa, pb) = (mesh.vertices[a as usize], mesh.vertices[b as usize]);
                    if dist(pa, pb) >= tol {
                        continue;
                    }
                    // Link condition: the common neighbours of a and b must
                    // be the third vertices of the faces on edge ab.
                    // Otherwise the collapse makes an edge with more than
                    // two faces.
                    let faces = vf.edge(mesh, a, b);
                    if faces.len() > 2 {
                        continue;
                    }
                    let (ra, rb) = (vf.ring(mesh, a), vf.ring(mesh, b));
                    let common = ra.iter().filter(|x| rb.binary_search(x).is_ok()).count();
                    if common != faces.len() {
                        continue;
                    }
                    // Two rim vertices joined by an inner edge: a collapse
                    // would pinch the surface.
                    if faces.len() == 2 && vf.on_rim(mesh, a) && vf.on_rim(mesh, b) {
                        continue;
                    }
                    mesh.vertices[a as usize] = scale(add(pa, pb), 0.5);
                    remap[b as usize] = a;
                    // Lock both one-rings: the rings above stay true for
                    // each later collapse in this pass.
                    for &x in ra.iter().chain(rb.iter()) {
                        locked[x as usize] = true;
                    }
                    locked[a as usize] = true;
                    locked[b as usize] = true;
                    collapsed += 1;
                }
            }
            if collapsed > 0 {
                mesh.apply_remap(&remap);
                total += collapsed;
                continue;
            }
        }

        let nf = mesh.triangles.len();
        let (mut touched, mut keep) = (vec![false; nf], vec![true; nf]);
        // Vertices of faces changed in this pass: the face lists in `vf` are
        // old there, so the pass does not use them again.
        let mut dirty = vec![false; nv];
        let mut new_edges: HashSet<EdgeKey> = HashSet::new();
        let mut changed = 0;
        for f in 0..nf {
            if touched[f] {
                continue;
            }
            let t = mesh.triangles[f];
            if t.iter().any(|&v| dirty[v as usize]) {
                continue;
            }
            let [p0, p1, p2] = mesh.tri_pos(f);
            let lens = [dist(p0, p1), dist(p1, p2), dist(p2, p0)];
            let i = (0..3).max_by(|&x, &y| lens[x].total_cmp(&lens[y])).unwrap();
            if lens[i] <= 0.0 || 2.0 * tri_area(p0, p1, p2) / lens[i] >= tol {
                continue;
            }
            let (a, b, c) = (t[i], t[(i + 1) % 3], t[(i + 2) % 3]);
            let adj = vf.edge(mesh, a, b);
            if adj.len() == 1 {
                keep[f] = false;
                touched[f] = true;
                changed += 1;
                continue;
            }
            if adj.len() != 2 {
                continue;
            }
            let g = (if adj[0] as usize == f { adj[1] } else { adj[0] }) as usize;
            let tg = mesh.triangles[g];
            if touched[g] || !directed(&tg, b, a) {
                continue;
            }
            let Some(d) = tg.iter().copied().find(|&x| x != a && x != b) else { continue };
            let cd = ekey(c, d);
            if d == c || dirty[d as usize] || !vf.edge(mesh, c, d).is_empty() || new_edges.contains(&cd) {
                continue;
            }
            // Flip only when it helps: both new faces must be higher than
            // the cap, and must not fold over each other. Without this
            // check, caps on a curved surface flip forth and back.
            let (pa, pb, pc, pd) = (
                mesh.vertices[a as usize],
                mesh.vertices[b as usize],
                mesh.vertices[c as usize],
                mesh.vertices[d as usize],
            );
            let old_h = height(p0, p1, p2);
            let (h1, h2) = (height(pa, pd, pc), height(pd, pb, pc));
            let n1 = cross(sub(pd, pa), sub(pc, pa));
            let n2 = cross(sub(pb, pd), sub(pc, pd));
            if h1.min(h2) <= old_h || dot(n1, n2) <= 0.0 {
                continue;
            }
            mesh.triangles[f] = [a, d, c];
            mesh.triangles[g] = [d, b, c];
            new_edges.insert(cd);
            touched[f] = true;
            touched[g] = true;
            for v in [a, b, c, d] {
                dirty[v as usize] = true;
            }
            changed += 1;
        }
        if changed == 0 {
            break;
        }
        if keep.iter().any(|k| !k) {
            mesh.retain_faces(&keep);
        }
        total += changed;
    }
    total
}

// ---- RemoveTinyShells --------------------------------------------------

/// Remove each closed shell with |volume| below `min_volume`.
pub fn remove_tiny_shells(mesh: &mut Mesh, min_volume: f64) -> usize {
    let ef = mesh.edge_faces();
    let shells = components(mesh, &ef);
    let mut keep = vec![true; mesh.triangles.len()];
    let mut removed = 0;
    for s in &shells {
        if shell_closed(mesh, &ef, s) && shell_volume(mesh, s).abs() < min_volume {
            for &f in s {
                keep[f as usize] = false;
            }
            removed += 1;
        }
    }
    if removed > 0 {
        mesh.retain_faces(&keep);
    }
    removed
}

// ---- PrepareForExport --------------------------------------------------

/// Split the T-junctions that are left, weld exactly, remove collapsed faces.
pub fn prepare_for_export(mesh: &mut Mesh, tol: f64) -> usize {
    let n = mesh.split_boundary_tjunctions(tol);
    // Exact weld of the boundary vertices only. A weld of inner vertices
    // would join again the copies that Wrap made at shared edges and
    // vertices (two bodies that touch).
    let mut rim = vec![false; mesh.vertices.len()];
    for (&(a, b), fs) in &mesh.edge_faces() {
        if fs.len() == 1 {
            rim[a as usize] = true;
            rim[b as usize] = true;
        }
    }
    mesh.weld_exact_where(Some(&rim));
    mesh.drop_collapsed();
    n + split_nonmanifold(mesh)
}

// ---- Non-manifold split ------------------------------------------------

/// Pairs of faces on an edge with more than two faces. Each body is on the
/// back side of its faces, so a face that runs a to b pairs with the face
/// before it in the turn order around the edge (and that face runs b to a).
fn pair_around_edge(mesh: &Mesh, a: u32, b: u32, fs: &[u32]) -> Vec<(u32, u32)> {
    let (pa, pb) = (mesh.vertices[a as usize], mesh.vertices[b as usize]);
    let d = sub(pb, pa);
    let ld = norm(d);
    if ld == 0.0 {
        return Vec::new();
    }
    let d = scale(d, 1.0 / ld);
    let mut items: Vec<(f64, i8, u32)> = Vec::with_capacity(fs.len());
    let mut u0: Option<Vec3> = None;
    for &f in fs {
        let t = mesh.triangles[f as usize];
        let Some(c) = t.iter().copied().find(|&x| x != a && x != b) else { return Vec::new() };
        let w = sub(mesh.vertices[c as usize], pa);
        let u = sub(w, scale(d, dot(w, d)));
        if norm(u) == 0.0 {
            return Vec::new();
        }
        let r = *u0.get_or_insert(u);
        let th = dot(cross(r, u), d).atan2(dot(r, u));
        let s = if directed(&t, a, b) { 1 } else { -1 };
        items.push((th, s, f));
    }
    items.sort_by(|x, y| x.0.total_cmp(&y.0));
    let n = items.len();
    let mut used = vec![false; n];
    let mut out = Vec::new();
    for i in 0..n {
        if items[i].1 != 1 || used[i] {
            continue;
        }
        let j = (i + n - 1) % n;
        if items[j].1 == -1 && !used[j] {
            used[i] = true;
            used[j] = true;
            out.push((items[i].2, items[j].2));
        }
    }
    out
}

/// Give each manifold sheet at a non-manifold edge or vertex its own
/// vertex copies. Returns the number of new vertices.
pub fn split_nonmanifold(mesh: &mut Mesh) -> usize {
    let nf = mesh.triangles.len();
    if nf == 0 {
        return 0;
    }
    let ef = mesh.edge_faces();
    let mut parent: Vec<u32> = (0..3 * nf as u32).collect();
    let corner = |m: &Mesh, f: u32, v: u32| -> u32 {
        let t = m.triangles[f as usize];
        f * 3 + t.iter().position(|&x| x == v).unwrap() as u32
    };
    let join = |parent: &mut Vec<u32>, x: u32, y: u32| {
        let (rx, ry) = (find(parent, x), find(parent, y));
        if rx != ry {
            parent[rx.max(ry) as usize] = rx.min(ry);
        }
    };
    for (&(a, b), fs) in &ef {
        let pairs = match fs.len() {
            2 => vec![(fs[0], fs[1])],
            n if n > 2 => pair_around_edge(mesh, a, b, fs),
            _ => Vec::new(),
        };
        for (f, g) in pairs {
            join(&mut parent, corner(mesh, f, a), corner(mesh, g, a));
            join(&mut parent, corner(mesh, f, b), corner(mesh, g, b));
        }
    }
    let nv = mesh.vertices.len();
    let mut taken = vec![false; nv];
    let mut root_id: std::collections::HashMap<u32, u32> = std::collections::HashMap::new();
    let mut added = 0;
    for f in 0..nf {
        for i in 0..3 {
            let v = mesh.triangles[f][i];
            let r = find(&mut parent, (f * 3 + i) as u32);
            let id = match root_id.get(&r) {
                Some(&id) => id,
                None => {
                    let id = if !taken[v as usize] {
                        taken[v as usize] = true;
                        v
                    } else {
                        mesh.vertices.push(mesh.vertices[v as usize]);
                        added += 1;
                        (mesh.vertices.len() - 1) as u32
                    };
                    root_id.insert(r, id);
                    id
                }
            };
            mesh.triangles[f][i] = id;
        }
    }
    added
}
