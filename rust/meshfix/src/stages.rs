//! The repair steps. Each step changes the mesh and returns a count for the
//! report. The order and the tolerances are in lib.rs (PIPELINE). The
//! comment of each step tells what it does.

use crate::math::*;
use crate::mesh::*;
use std::cmp::{Ordering, Reverse};
use std::collections::{BinaryHeap, HashMap, HashSet, VecDeque};

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
/// The exact welds use rim vertices only: a weld of inner vertices joins
/// bodies that touch at a vertex into one non-manifold vertex (measured on
/// a clean, closed aircraft model: 57 new non-manifold edges).
pub fn stitch(mesh: &mut Mesh, tol: f64) -> usize {
    mesh.weld_rim(0.0)
        + merge_boundary_vertices(mesh, tol)
        + mesh.split_boundary_tjunctions(tol)
        + mesh.weld_rim(0.0)
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
    mesh.weld_rim(weld_tol);
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
///
/// First (from the stitch approach, rims.rs): two rims that are the two
/// sides of one open wall end get a strip between them, not two disks
/// across the opening. Then the vertex copies at one position on a rim are
/// welded (a slit closes, a pinched rim becomes simple rims). Then each
/// simple rim gets a fill.
pub fn close_all_holes(mesh: &mut Mesh) -> usize {
    let bridged = crate::rims::bridge_rims(mesh);
    let (loops, _welded) = crate::rims::weld_rim_copies(mesh);
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
    bridged + loops.len()
}

/// CloseAllHoles for the winding Wrap: as `close_all_holes`, but a fill
/// that crosses another face is made again with the next method (minimum
/// area, then CDT, then a fan from the centroid). Returns the number of
/// holes closed.
pub fn close_all_holes_checked(mesh: &mut Mesh) -> usize {
    let bridged = crate::rims::bridge_rims(mesh);
    let (loops, _welded) = crate::rims::weld_rim_copies(mesh);
    if loops.is_empty() {
        return bridged;
    }
    let ef = mesh.edge_faces();
    let base_nf = mesh.triangles.len();
    let base_nv = mesh.vertices.len();
    let mut choice = vec![0usize; loops.len()];
    for _ in 0..4 {
        mesh.triangles.truncate(base_nf);
        mesh.vertices.truncate(base_nv);
        let mut owner: Vec<usize> = Vec::new();
        for (i, l) in loops.iter().enumerate() {
            let poly: Vec<u32> = l.iter().rev().copied().collect();
            let tris = match choice[i] {
                0 if poly.len() <= MAX_DP_HOLE => triangulate_min_area(&mesh.vertices, &poly, &ef),
                0 | 1 => triangulate_cdt(&mesh.vertices, &poly)
                    .or_else(|| (poly.len() <= MAX_DP_FALLBACK).then(|| triangulate_min_area(&mesh.vertices, &poly, &ef)))
                    .unwrap_or_else(|| fan(&mut mesh.vertices, &poly)),
                _ => fan(&mut mesh.vertices, &poly),
            };
            for t in tris {
                mesh.triangles.push(t);
                owner.push(i);
            }
        }
        if std::env::var_os("MESHFIX_NO_CROSS_CHECK").is_some() {
            break;
        }
        let active: Vec<bool> = (0..mesh.triangles.len()).map(|f| f >= base_nf).collect();
        let pairs = crate::alphawrap::crossing_pairs_opt(mesh, Some(&active), false);
        let mut changed = false;
        let mut bad = vec![false; loops.len()];
        for (f, g) in pairs {
            for h in [f as usize, g as usize] {
                if h >= base_nf {
                    bad[owner[h - base_nf]] = true;
                }
            }
        }
        for i in 0..loops.len() {
            if bad[i] && choice[i] < 2 {
                choice[i] += 1;
                changed = true;
            }
        }
        if !changed {
            // A fill that crosses with each triangulation is not used: the
            // hole stays for the steps after this one.
            if bad.iter().any(|&b| b) && std::env::var_os("MESHFIX_KEEP_BAD_FILL").is_none() {
                let mut i = 0;
                mesh.triangles.retain(|_| {
                    let k = i < base_nf || !bad[owner[i - base_nf]];
                    i += 1;
                    k
                });
            }
            break;
        }
    }
    bridged + loops.len()
}

// ---- Wrap --------------------------------------------------------------
//
// Split non-manifold edges and vertices, turn each
// closed shell so that the normals point out, make the union of the shells
// that intersect (union.rs), and remove each closed shell that is fully
// inside another closed shell. The mesh is not refined.

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

/// The Wrap mode: "winding" (arrange.rs, default) or "union" (union.rs).
/// The variable MESHFIX_WRAP selects it.
pub fn wrap_mode_winding() -> bool {
    !matches!(std::env::var("MESHFIX_WRAP").as_deref(), Ok("union"))
}

/// Wrap by the arrangement and winding-number labels (arrange.rs).
pub fn wrap_winding(mesh: &mut Mesh) -> (usize, crate::arrange::ArrangeReport) {
    let t = std::time::Instant::now();
    let before_split = mesh.clone();
    let defects = mesh.defect_edges();
    split_nonmanifold(mesh);
    if mesh.defect_edges() > defects {
        *mesh = before_split;
    }
    orient_shells(mesh);
    crate::union::dbg_time("winding orient", t);
    if std::env::var_os("MESHFIX_NO_PLANAR").is_none() {
        if let Some(p) = std::env::var_os("MESHFIX_DUMP_PREPLANAR") {
            let _ = mesh.write_obj(std::path::Path::new(&p));
        }
        let i0 = crate::mesh_info(mesh);
        let pr = crate::planar::overlay_axis_planes(mesh);
        if std::env::var_os("MESHFIX_TIMING").is_some() {
            eprintln!("winding planar {pr:?}\n  before {i0:?}\n  after {:?}", crate::mesh_info(mesh));
        }
        // Contacts in other planes (any orientation).
        if std::env::var_os("MESHFIX_NO_CONTACT").is_none() {
            let cr = crate::planar::overlay_contacts(mesh);
            if std::env::var_os("MESHFIX_TIMING").is_some() {
                eprintln!("winding contacts {cr:?}\n  after {:?}", crate::mesh_info(mesh));
            }
        }
    }
    crate::union::dbg_time("winding planar", t);
    if let Some(p) = std::env::var_os("MESHFIX_DUMP_PLANAR") {
        let _ = mesh.write_obj(std::path::Path::new(&p));
    }
    let rep = crate::arrange::winding_union(mesh);
    crate::union::dbg_time("winding union", t);
    if let Some(p) = std::env::var_os("MESHFIX_DUMP_UNION") {
        let _ = mesh.write_obj(std::path::Path::new(&p));
    }
    winding_post(mesh);
    if let Some(p) = std::env::var_os("MESHFIX_DUMP_WRAP") {
        let _ = mesh.write_obj(std::path::Path::new(&p));
    }
    if std::env::var_os("MESHFIX_TIMING").is_some() {
        eprintln!("winding report {rep:?}");
        eprintln!("winding after {:?}", crate::mesh_info(mesh));
    }
    (rep.removed_patches, rep)
}

#[cfg(test)]
pub(crate) fn excise_seams_for_test(mesh: &mut Mesh) {
    excise_seams(mesh)
}

/// Cut out and fill the seams (open and non-manifold edges), with 1 to 4
/// vertex rings around them. Each pass is kept only when it gives fewer
/// open + non-manifold edges.
fn excise_seams(mesh: &mut Mesh) {
    for rings in 1..=4 {
        let d0 = mesh.defect_edges();
        if d0 == 0 {
            break;
        }
        let seed: Vec<Vec3> = defect_groups(mesh).concat();
        let before = mesh.clone();
        excise_and_fill(mesh, rings, &seed);
        let d1 = mesh.defect_edges();
        if std::env::var_os("MESHFIX_TIMING").is_some() {
            eprintln!("winding excise {} vertices, {rings} rings: defects {d0} -> {d1}", seed.len());
        }
        if d1 >= d0 {
            *mesh = before;
        }
    }
}

/// Groups of the vertices on open and non-manifold edges (joined by such
/// edges). Positions, so that the groups stay valid when ids change.
pub fn defect_groups(mesh: &Mesh) -> Vec<Vec<Vec3>> {
    let ef = mesh.edge_faces();
    let mut par: HashMap<u32, u32> = HashMap::new();
    fn root(par: &mut HashMap<u32, u32>, mut x: u32) -> u32 {
        while let Some(&p) = par.get(&x) {
            if p == x {
                break;
            }
            x = p;
        }
        x
    }
    let mut edges: Vec<EdgeKey> = ef.iter().filter(|(_, fs)| fs.len() != 2).map(|(&e, _)| e).collect();
    edges.sort_unstable();
    for &(a, b) in &edges {
        par.entry(a).or_insert(a);
        par.entry(b).or_insert(b);
        let (ra, rb) = (root(&mut par, a), root(&mut par, b));
        if ra != rb {
            par.insert(ra.max(rb), ra.min(rb));
        }
    }
    let mut groups: HashMap<u32, Vec<Vec3>> = HashMap::new();
    let mut keys: Vec<u32> = par.keys().copied().collect();
    keys.sort_unstable();
    for v in keys {
        let r = root(&mut par, v);
        groups.entry(r).or_default().push(mesh.vertices[v as usize]);
    }
    let mut out: Vec<(u32, Vec<Vec3>)> = groups.into_iter().collect();
    out.sort_by_key(|x| x.0);
    out.into_iter().map(|x| x.1).collect()
}

/// Remove the faces within `rings` vertex rings of the vertices at the
/// positions `seed`, then split, remove flaps and fill the holes.
pub fn excise_and_fill(mesh: &mut Mesh, rings: usize, seed: &[Vec3]) {
    let want: HashSet<[u64; 3]> = seed.iter().map(|p| [p[0].to_bits(), p[1].to_bits(), p[2].to_bits()]).collect();
    let mut mark: Vec<bool> = mesh.vertices.iter().map(|p| want.contains(&[p[0].to_bits(), p[1].to_bits(), p[2].to_bits()])).collect();
    let mut drop = vec![false; mesh.triangles.len()];
    for _ in 0..rings {
        for (f, t) in mesh.triangles.iter().enumerate() {
            if t.iter().any(|&x| mark[x as usize]) {
                drop[f] = true;
            }
        }
        for (f, t) in mesh.triangles.iter().enumerate() {
            if drop[f] {
                for &x in t {
                    mark[x as usize] = true;
                }
            }
        }
    }
    let keep: Vec<bool> = drop.iter().map(|&d| !d).collect();
    mesh.retain_faces(&keep);
    split_nonmanifold(mesh);
    remove_flaps(mesh);
    close_all_holes_checked(mesh);
    split_nonmanifold(mesh);
}

/// Remove flaps: faces with two open edges whose third edge has more than
/// two faces (a piece of a sheet that hangs on a seam). Repeats while it
/// finds some. Returns the number of faces removed.
pub fn remove_flaps(mesh: &mut Mesh) -> usize {
    let mut total = 0;
    for _ in 0..8 {
        let ef = mesh.edge_faces();
        let keep: Vec<bool> = mesh
            .triangles
            .iter()
            .map(|t| {
                let n: Vec<usize> = (0..3).map(|i| ef[&ekey(t[i], t[(i + 1) % 3])].len()).collect();
                !(n.iter().filter(|&&c| c == 1).count() == 2 && n.iter().any(|&c| c > 2))
            })
            .collect();
        let n = keep.iter().filter(|&&k| !k).count();
        if n == 0 {
            break;
        }
        mesh.retain_faces(&keep);
        total += n;
    }
    total
}

/// The steps after the winding union: voids, seams (split, fill, seal,
/// cut out and fill), inverted parts, nested shells.
pub fn winding_post(mesh: &mut Mesh) {
    let t0 = std::time::Instant::now();
    // A closed shell with a negative volume is a void inside the body. The
    // Wrap fills such voids.
    let voids = remove_voids(mesh);
    if std::env::var_os("MESHFIX_TIMING").is_some() {
        eprintln!("winding voids removed {voids}");
        eprintln!("winding after union {:?}", crate::mesh_info(mesh));
    }
    // Seams of the labels (open input regions): stitch, close the holes,
    // split what still touches. Each is kept only when it does not give
    // more open or non-manifold edges.
    if std::env::var_os("MESHFIX_NO_POST").is_none() {
        for k in 0..5 {
            let before = mesh.clone();
            let d0 = mesh.defect_edges();
            match k {
                0 => {
                    split_nonmanifold(mesh);
                }
                4 => {
                    split_pinched_edges(mesh);
                }
                1 => {
                    // The stitch moves rim vertices by up to 0.01 units, and
                    // then faces cross. The fill below welds the rim copies.
                    if std::env::var_os("MESHFIX_POST_STITCH").is_some() {
                        stitch(mesh, 0.01);
                    }
                }
                2 => {
                    remove_flaps(mesh);
                    close_all_holes_checked(mesh);
                }
                _ => {
                    split_nonmanifold(mesh);
                }
            }
            // A step that makes faces cross (a weld moves vertices, a fill
            // can go through a near surface) goes back too.
            if mesh.defect_edges() > d0 || k == 1 && new_crossings(&before, mesh) > 0 {
                *mesh = before;
            }
            if std::env::var_os("MESHFIX_TIMING").is_some() {
                eprintln!("winding post {k}: {:?}", crate::mesh_info(mesh));
            }
            crate::union::dbg_time("winding post step", t0);
        }
        // Seams that the steps above cannot close. First: cut out the faces
        // near all of them (1 to 4 rings) and fill again; the hole fill does
        // not keep a fill that crosses a face. Kept only with fewer open +
        // non-manifold edges. Then, if seams are left, the seal of the
        // stitch approach (split by turn order, orient, bridge, weld, fill)
        // and the cut out again.
        excise_seams(mesh);
        crate::union::dbg_time("winding excise", t0);
        if mesh.defect_edges() > 0 && std::env::var_os("MESHFIX_NO_SEAL").is_none() {
            let before = mesh.clone();
            let d0 = mesh.defect_edges();
            let r = crate::seal::seal(mesh);
            if mesh.defect_edges() >= d0 || new_crossings(&before, mesh) > 0 {
                *mesh = before;
            }
            if std::env::var_os("MESHFIX_TIMING").is_some() {
                eprintln!("winding seal {r:?}: {:?}", crate::mesh_info(mesh));
            }
            excise_seams(mesh);
        }

        remove_voids(mesh);
        remove_inverted_parts(mesh, 1e-3);
        // A closed shell inside another one (a pocket between bodies that
        // the overlay of contacts closed) goes.
        remove_nested_with(mesh, true);
    }
    crate::union::dbg_time("winding post done", t0);
}

/// Crossing face pairs in `after` with at least one face that is not in
/// `before` (a face is the same when its three corner positions are).
pub fn new_crossings(before: &Mesh, after: &Mesh) -> usize {
    if std::env::var_os("MESHFIX_NO_CROSS_CHECK").is_some() {
        return 0;
    }
    let key = |m: &Mesh, f: usize| {
        let mut k: Vec<[u64; 3]> = m.tri_pos(f).iter().map(|p| [p[0].to_bits(), p[1].to_bits(), p[2].to_bits()]).collect();
        k.sort_unstable();
        [k[0], k[1], k[2]]
    };
    let old: HashSet<[[u64; 3]; 3]> = (0..before.triangles.len()).map(|f| key(before, f)).collect();
    let active: Vec<bool> = (0..after.triangles.len()).map(|f| !old.contains(&key(after, f))).collect();
    if !active.iter().any(|&a| a) {
        return 0;
    }
    crate::alphawrap::crossing_pairs_opt(after, Some(&active), false).len()
}

/// Remove each closed shell with a negative volume (a void). Returns the
/// number of shells removed.
pub fn remove_voids(mesh: &mut Mesh) -> usize {
    let ef = mesh.edge_faces();
    let shells = components(mesh, &ef);
    let mut keep = vec![true; mesh.triangles.len()];
    let mut n = 0;
    for s in &shells {
        if shell_closed(mesh, &ef, s) && shell_volume(mesh, s) < 0.0 {
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

/// Wrap with the union report: (count for the step, union report).
pub fn wrap_report(mesh: &mut Mesh) -> (usize, crate::union::UnionReport) {
    if wrap_mode_winding() {
        let (n, r) = wrap_winding(mesh);
        let u = crate::union::UnionReport {
            pairs: r.pairs,
            degenerate: r.degenerate,
            cut_faces: r.cut_faces,
            removed_patches: r.removed_patches,
            failed_faces: r.failed_faces,
            panicked: r.panicked,
            contact: r.contact,
            ..Default::default()
        };
        return (n, u);
    }
    // Two bodies on one edge are one edge-connected shell that is not
    // closed. After the split each body is a closed shell of its own.
    let t = std::time::Instant::now();
    // The split must not make more open or non-manifold edges.
    let before_split = mesh.clone();
    let defects = mesh.defect_edges();
    split_nonmanifold(mesh);
    if mesh.defect_edges() > defects {
        *mesh = before_split;
    }
    crate::union::dbg_time("wrap split", t);
    orient_shells(mesh);
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
    if rep.pairs > 0 && !rep.panicked && std::env::var_os("MESHFIX_NO_UNION_GUARD").is_none() {
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
    // The union removes inner walls and loose sheets of open parts. Their
    // gaps are now holes of the outer surface: stitch and close them, then
    // split what still touches. Each of these is kept only when it does not
    // give more open or non-manifold edges.
    for k in 0..3 {
        let before = mesh.clone();
        let d0 = mesh.defect_edges();
        match k {
            0 => {
                stitch(mesh, 0.01);
            }
            1 => {
                close_all_holes(mesh);
            }
            _ => {
                split_nonmanifold(mesh);
            }
        }
        if mesh.defect_edges() > d0 {
            *mesh = before;
        }
    }
    orient_shells(mesh);
    crate::union::dbg_time("wrap close", t);
    (n + rep.removed_patches, rep)
}

/// Turn each shell so that its signed volume is positive. For an open shell
/// with a small gap the sign still tells the side.
fn orient_shells(mesh: &mut Mesh) {
    let ef = mesh.edge_faces();
    let shells = components(mesh, &ef);
    for s in &shells {
        if shell_volume(mesh, s) < 0.0 {
            for &f in s {
                mesh.triangles[f as usize].swap(1, 2);
            }
        }
    }
}

/// Remove each closed shell that is fully inside another closed shell.
fn remove_nested(mesh: &mut Mesh) -> usize {
    remove_nested_with(mesh, false)
}

/// remove_nested. With `mostly`, a small closed shell (volume below 1e-4 of
/// the other shell) with 3/4 of its samples inside also goes: a small body
/// that was not joined and sticks out of the surface by a sliver.
fn remove_nested_with(mesh: &mut Mesh, mostly: bool) -> usize {
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
            let inside = pts.iter().filter(|&&p| fwn.eval(p, None) > 0.5).count();
            let small = || shell_volume(mesh, &shells[i]).abs() < 1e-4 * shell_volume(mesh, &shells[j]).abs();
            if inside == pts.len() || (mostly && inside * 4 >= pts.len() * 3 && small()) {
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
pub(crate) struct VertexFaces {
    start: Vec<u32>,
    list: Vec<u32>,
}

impl VertexFaces {
    pub(crate) fn new(mesh: &Mesh) -> Self {
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
    pub(crate) fn of(&self, v: u32) -> &[u32] {
        &self.list[self.start[v as usize] as usize..self.start[v as usize + 1] as usize]
    }
    /// Faces that use edge (a, b).
    pub(crate) fn edge(&self, mesh: &Mesh, a: u32, b: u32) -> Vec<u32> {
        self.of(a).iter().copied().filter(|&f| mesh.triangles[f as usize].contains(&b)).collect()
    }
    /// Sorted neighbour vertices of v.
    pub(crate) fn ring(&self, mesh: &Mesh, v: u32) -> Vec<u32> {
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
    pub(crate) fn on_rim(&self, mesh: &Mesh, v: u32) -> bool {
        self.ring(mesh, v).into_iter().any(|w| self.edge(mesh, v, w).len() != 2)
    }
}

/// Collapse each edge shorter than `tol` (needle). Then, for each face with
/// a height below `tol` (cap), flip its longest edge, or remove the face
/// when the edge is on the boundary. Returns the number of changes.
pub fn remove_degenerated_faces(mesh: &mut Mesh, tol: f64) -> usize {
    let mut total = 0;
    // Vertices whose collapse made a face cross: not tried again.
    let mut refused: Vec<bool> = Vec::new();
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
            let mut plan: Vec<(u32, u32, Vec3)> = Vec::new();
            if refused.len() < nv {
                refused.resize(nv, false);
            }
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
                    if !collapse_keeps_normals(mesh, &vf, a, b, scale(add(pa, pb), 0.5)) {
                        continue;
                    }
                    if refused[a as usize] || refused[b as usize] {
                        continue;
                    }
                    plan.push((a, b, scale(add(pa, pb), 0.5)));
                    // Lock both one-rings: the rings above stay true for
                    // each later collapse in this pass.
                    for &x in ra.iter().chain(rb.iter()) {
                        locked[x as usize] = true;
                    }
                    locked[a as usize] = true;
                    locked[b as usize] = true;
                }
            }
            // A collapse that makes a face cross another face is refused
            // (the union result has surfaces very near each other).
            let plan = keep_without_crossings(mesh, plan, &mut refused);
            for &(a, b, to) in &plan {
                mesh.vertices[a as usize] = to;
                remap[b as usize] = a;
                collapsed += 1;
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
        let mut flips: Vec<(usize, usize, [u32; 3], [u32; 3])> = Vec::new();
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
            flips.push((f, g, mesh.triangles[f], mesh.triangles[g]));
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
        // A flip that makes a face cross another face goes back.
        changed -= undo_crossing_flips(mesh, &flips);
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

/// The collapses of `plan` (a, b, new position) without the ones that make
/// a face cross another face. The vertices of a refused collapse are marked
/// in `refused`.
fn keep_without_crossings(mesh: &Mesh, plan: Vec<(u32, u32, Vec3)>, refused: &mut [bool]) -> Vec<(u32, u32, Vec3)> {
    if plan.is_empty() || std::env::var_os("MESHFIX_NO_CROSS_CHECK").is_some() {
        return plan;
    }
    let nv = mesh.vertices.len();
    let mut trial = mesh.clone();
    let mut remap: Vec<u32> = (0..nv as u32).collect();
    let mut owner = vec![u32::MAX; nv];
    for (i, &(a, b, to)) in plan.iter().enumerate() {
        trial.vertices[a as usize] = to;
        remap[b as usize] = a;
        owner[a as usize] = i as u32;
    }
    // No compaction: `owner` is indexed by the vertex ids of the mesh.
    for t in &mut trial.triangles {
        for x in t.iter_mut() {
            *x = remap[*x as usize];
        }
    }
    trial.drop_collapsed();
    let active: Vec<bool> = trial.triangles.iter().map(|t| t.iter().any(|&x| owner[x as usize] != u32::MAX)).collect();
    let pairs = crate::alphawrap::crossing_pairs_opt(&trial, Some(&active), false);
    if pairs.is_empty() {
        return plan;
    }
    let mut bad = vec![false; plan.len()];
    for (f, g) in pairs {
        for h in [f, g] {
            for &x in &trial.triangles[h as usize] {
                let o = owner[x as usize];
                if o != u32::MAX {
                    bad[o as usize] = true;
                }
            }
        }
    }
    plan.into_iter()
        .zip(bad)
        .filter_map(|(p, b)| {
            if b {
                refused[p.0 as usize] = true;
                refused[p.1 as usize] = true;
                None
            } else {
                Some(p)
            }
        })
        .collect()
}

/// Undo each flip of `flips` (f, g, old f, old g) whose new faces cross
/// another face. Returns the number of flips undone.
fn undo_crossing_flips(mesh: &mut Mesh, flips: &[(usize, usize, [u32; 3], [u32; 3])]) -> usize {
    if flips.is_empty() || std::env::var_os("MESHFIX_NO_CROSS_CHECK").is_some() {
        return 0;
    }
    let mut active = vec![false; mesh.triangles.len()];
    let mut of = vec![u32::MAX; mesh.triangles.len()];
    for (i, &(f, g, _, _)) in flips.iter().enumerate() {
        active[f] = true;
        active[g] = true;
        of[f] = i as u32;
        of[g] = i as u32;
    }
    let pairs = crate::alphawrap::crossing_pairs_opt(mesh, Some(&active), false);
    let mut undo = vec![false; flips.len()];
    for (f, g) in pairs {
        for h in [f, g] {
            if of[h as usize] != u32::MAX {
                undo[of[h as usize] as usize] = true;
            }
        }
    }
    let mut n = 0;
    for (i, &(f, g, tf, tg)) in flips.iter().enumerate() {
        if undo[i] {
            mesh.triangles[f] = tf;
            mesh.triangles[g] = tg;
            n += 1;
        }
    }
    n
}

/// Undo the operations of one pass that make a face cross another face:
/// collapses (a, b, old position of a; `remap[b] = a`), flips (f, g, old
/// f, old g) and vertex moves (v, old position). The operations of one
/// pass touch separate one-rings, so each one goes back alone. Returns
/// (collapses undone, flips undone); for moves the first number counts
/// the moves undone.
pub(crate) fn undo_crossing_ops(
    mesh: &mut Mesh,
    remap: &mut [u32],
    cols: &[(u32, u32, Vec3)],
    flips: &[(usize, usize, [u32; 3], [u32; 3])],
    moves: &[(u32, Vec3)],
) -> (usize, usize) {
    if (cols.is_empty() && flips.is_empty() && moves.is_empty()) || std::env::var_os("MESHFIX_NO_CROSS_CHECK").is_some() {
        return (0, 0);
    }
    let nv = mesh.vertices.len();
    let mut hot = vec![false; nv];
    for &(a, _, _) in cols {
        hot[a as usize] = true;
    }
    for &(v, _) in moves {
        hot[v as usize] = true;
    }
    let mut flip_face = vec![false; mesh.triangles.len()];
    for &(f, g, _, _) in flips {
        flip_face[f] = true;
        flip_face[g] = true;
    }
    // The trial mesh: remap applied, collapsed faces left out, no
    // compaction (vertex ids stay).
    let mut trial = Mesh::new(mesh.vertices.clone(), Vec::with_capacity(mesh.triangles.len()));
    let mut old_of: Vec<usize> = Vec::with_capacity(mesh.triangles.len());
    for (f, t) in mesh.triangles.iter().enumerate() {
        let t = [remap[t[0] as usize], remap[t[1] as usize], remap[t[2] as usize]];
        if t[0] != t[1] && t[1] != t[2] && t[2] != t[0] {
            trial.triangles.push(t);
            old_of.push(f);
        }
    }
    let active: Vec<bool> = trial
        .triangles
        .iter()
        .zip(&old_of)
        .map(|(t, &f)| flip_face[f] || t.iter().any(|&x| hot[x as usize]))
        .collect();
    if !active.iter().any(|&a| a) {
        return (0, 0);
    }
    let pairs = crate::alphawrap::crossing_pairs_opt(&trial, Some(&active), false);
    if pairs.is_empty() {
        return (0, 0);
    }
    let mut bad_v = vec![false; nv];
    let mut bad_f = vec![false; mesh.triangles.len()];
    for (f, g) in pairs {
        for h in [f, g] {
            bad_f[old_of[h as usize]] = true;
            for &x in &trial.triangles[h as usize] {
                bad_v[x as usize] = true;
            }
        }
    }
    let (mut nc, mut nf) = (0, 0);
    for &(a, b, old) in cols {
        if bad_v[a as usize] {
            mesh.vertices[a as usize] = old;
            remap[b as usize] = b;
            nc += 1;
        }
    }
    for &(v, old) in moves {
        if bad_v[v as usize] {
            mesh.vertices[v as usize] = old;
            nc += 1;
        }
    }
    for &(f, g, tf, tg) in flips {
        if bad_f[f] || bad_f[g] {
            mesh.triangles[f] = tf;
            mesh.triangles[g] = tg;
            nf += 1;
        }
    }
    (nc, nf)
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
    let mut n = n + split_nonmanifold(mesh);
    if wrap_mode_winding() {
        n += split_pinched_edges(mesh);
    }
    // Winding mode: the thorough cleanup of the library approach (each edge,
    // to the midpoint or to an end), then the last cleanup below.
    n += remove_zero_area_with(mesh, wrap_mode_winding());
    if wrap_mode_winding() {
        n += remove_zero_area_final(mesh) + remove_micro_clusters(mesh) + remove_inverted_parts(mesh, 1e-3);
    }
    n
}

/// True when moving a and b to `to` turns no face around them over and
/// makes no face of zero area (the faces on edge ab disappear).
fn collapse_keeps_normals(mesh: &Mesh, vf: &VertexFaces, a: u32, b: u32, to: Vec3) -> bool {
    for &v in &[a, b] {
        for &f in vf.of(v) {
            let t = mesh.triangles[f as usize];
            if t.contains(&a) && t.contains(&b) {
                continue;
            }
            let old = mesh.tri_pos(f as usize);
            let mut new = old;
            for k in 0..3 {
                if t[k] == a || t[k] == b {
                    new[k] = to;
                }
            }
            let n0 = cross(sub(old[1], old[0]), sub(old[2], old[0]));
            let n1 = cross(sub(new[1], new[0]), sub(new[2], new[0]));
            if dot(n0, n1) <= 0.0 || norm(n1) <= 1e-12 * norm(n0) {
                return false;
            }
        }
    }
    true
}

/// Last cleanup of the winding mode, after remove_zero_area: the faces that
/// still have a zero area (the program's limit). A collapse of the
/// shortest possible edge needs only the link condition here (the faces
/// are in a region of the size of the small vertex move, where the face
/// normals have no meaning). When no collapse is possible, the vertices of
/// the face move a little to the centroid of their neighbors (two copies
/// of one vertex then separate). No face is deleted, so a closed mesh stays
/// closed. Returns the number of changes.
pub fn remove_zero_area_final(mesh: &mut Mesh) -> usize {
    let mut total = 0;
    for round in 0..32 {
        let limit = zero_area_limit(mesh);
        let cand: Vec<usize> = (0..mesh.triangles.len())
            .filter(|&f| {
                let [a, b, c] = mesh.tri_pos(f);
                tri_area(a, b, c) < limit
            })
            .collect();
        if cand.is_empty() {
            break;
        }
        let vf = VertexFaces::new(mesh);
        let nv = mesh.vertices.len();
        let mut locked = vec![false; nv];
        let mut remap: Vec<u32> = (0..nv as u32).collect();
        let mut collapsed = 0;
        let mut stuck: Vec<usize> = Vec::new();
        let mut col_log: Vec<(u32, u32, Vec3)> = Vec::new();
        for &f in &cand {
            let t = mesh.triangles[f];
            if t.iter().any(|&v| locked[v as usize]) {
                continue;
            }
            let p = mesh.tri_pos(f);
            let lens = [dist(p[0], p[1]), dist(p[1], p[2]), dist(p[2], p[0])];
            let mut order = [0usize, 1, 2];
            order.sort_by(|&x, &y| lens[x].total_cmp(&lens[y]));
            let mut done = false;
            for &i in &order {
                let (a, b) = (t[i], t[(i + 1) % 3]);
                if vf.edge(mesh, a, b).len() != 2 {
                    continue;
                }
                let (ra, rb) = (vf.ring(mesh, a), vf.ring(mesh, b));
                let common = ra.iter().filter(|x| rb.binary_search(x).is_ok()).count();
                if common != 2 || (vf.on_rim(mesh, a) && vf.on_rim(mesh, b)) {
                    continue;
                }
                let to = scale(add(mesh.vertices[a as usize], mesh.vertices[b as usize]), 0.5);
                col_log.push((a, b, mesh.vertices[a as usize]));
                mesh.vertices[a as usize] = to;
                remap[b as usize] = a;
                for &x in ra.iter().chain(rb.iter()) {
                    locked[x as usize] = true;
                }
                locked[a as usize] = true;
                locked[b as usize] = true;
                collapsed += 1;
                done = true;
                break;
            }
            if !done {
                stuck.push(f);
            }
        }
        let mut nudged = 0;
        if collapsed == 0 {
            let w = (0.01 * (1u64 << round.min(6)) as f64).min(0.5);
            let mut moves: Vec<(u32, Vec3)> = Vec::new();
            for &f in &stuck {
                for &v in &mesh.triangles[f] {
                    let ring = vf.ring(mesh, v);
                    if ring.is_empty() {
                        continue;
                    }
                    let mut c = [0.0; 3];
                    for &x in &ring {
                        c = add(c, mesh.vertices[x as usize]);
                    }
                    c = scale(c, 1.0 / ring.len() as f64);
                    let p = mesh.vertices[v as usize];
                    moves.push((v, add(p, scale(sub(c, p), w))));
                }
            }
            moves.sort_by_key(|m| m.0);
            moves.dedup_by_key(|m| m.0);
            let mut nudge_log: Vec<(u32, Vec3)> = Vec::new();
            for (v, p) in moves {
                nudge_log.push((v, mesh.vertices[v as usize]));
                mesh.vertices[v as usize] = p;
                nudged += 1;
            }
            let mut ident: Vec<u32> = (0..nv as u32).collect();
            nudged -= undo_crossing_ops(mesh, &mut ident, &[], &[], &nudge_log).0;
        } else {
            collapsed -= undo_crossing_ops(mesh, &mut remap, &col_log, &[], &[]).0;
            mesh.apply_remap(&remap);
        }
        if collapsed + nudged == 0 {
            break;
        }
        total += collapsed + nudged;
    }
    total
}

/// As `collapse_keeps_normals`, but a face with an area below `limit`
/// before the collapse is not checked when it stays below 1e4 · limit:
/// its normal has no direction.
fn collapse_keeps_normals_lim(mesh: &Mesh, vf: &VertexFaces, a: u32, b: u32, to: Vec3, limit: f64) -> bool {
    for &v in &[a, b] {
        for &f in vf.of(v) {
            let t = mesh.triangles[f as usize];
            if t.contains(&a) && t.contains(&b) {
                continue;
            }
            let old = mesh.tri_pos(f as usize);
            let mut new = old;
            for k in 0..3 {
                if t[k] == a || t[k] == b {
                    new[k] = to;
                }
            }
            let n0 = cross(sub(old[1], old[0]), sub(old[2], old[0]));
            let n1 = cross(sub(new[1], new[0]), sub(new[2], new[0]));
            if 0.5 * norm(n0) < limit && 0.5 * norm(n1) < 1e4 * limit {
                continue;
            }
            if dot(n0, n1) <= 0.0 || norm(n1) <= 1e-12 * norm(n0) {
                return false;
            }
        }
    }
    true
}

/// Zero-area limit of the program's «Вироджені» counter (fs_geometry.diagnose):
/// area < max(mean area * 1e-10, 1e-24).
/// Faces of zero area that the collapses above cannot remove: a small
/// cluster of them at a corner where several surfaces meet. All vertices of
/// such a cluster (joined by zero-area faces) go to the vertex with the
/// most faces. Kept only when it gives no more open + non-manifold edges
/// and no new crossing: first all clusters at once, else each alone.
/// Returns the number of clusters removed.
/// Move the vertices `g` to the vertex `r` (faces of zero area go), and keep
/// it only when the edges around do not get more open or non-manifold edges
/// and no moved face crosses a face. Local checks only.
fn try_collapse(mesh: &mut Mesh, g: &[u32], r: u32) -> bool {
    let gs: HashSet<u32> = g.iter().copied().collect();
    let fs: Vec<usize> = (0..mesh.triangles.len()).filter(|&f| mesh.triangles[f].iter().any(|x| gs.contains(x))).collect();
    let old: Vec<[u32; 3]> = fs.iter().map(|&f| mesh.triangles[f]).collect();
    let bad_edges = |m: &Mesh, keep: &dyn Fn(u32) -> bool| {
        let mut cnt: HashMap<EdgeKey, u32> = HashMap::new();
        for &f in &fs {
            let t = m.triangles[f];
            if t[0] == t[1] || t[1] == t[2] || t[2] == t[0] {
                continue;
            }
            for i in 0..3 {
                let e = ekey(t[i], t[(i + 1) % 3]);
                if keep(e.0) || keep(e.1) {
                    *cnt.entry(e).or_default() += 1;
                }
            }
        }
        cnt.values().filter(|&&c| c != 2).count()
    };
    let d0 = bad_edges(mesh, &|x| gs.contains(&x));
    for &f in &fs {
        for x in mesh.triangles[f].iter_mut() {
            if gs.contains(x) {
                *x = r;
            }
        }
    }
    let d1 = bad_edges(mesh, &|x| x == r);
    let mut good = d1 <= d0;
    if good {
        // Crossings of the moved faces with the faces near them.
        let act: Vec<usize> = fs
            .iter()
            .zip(&old)
            .filter(|&(&f, o)| {
                let t = mesh.triangles[f];
                t != *o && t[0] != t[1] && t[1] != t[2] && t[2] != t[0]
            })
            .map(|(&f, _)| f)
            .collect();
        let mut lo = [f64::INFINITY; 3];
        let mut hi = [f64::NEG_INFINITY; 3];
        for &f in &act {
            for p in mesh.tri_pos(f) {
                for k in 0..3 {
                    lo[k] = lo[k].min(p[k]);
                    hi[k] = hi[k].max(p[k]);
                }
            }
        }
        let is_act: HashSet<usize> = act.iter().copied().collect();
        let mut sub_f: Vec<usize> = act.clone();
        for f in 0..mesh.triangles.len() {
            if is_act.contains(&f) || fs.binary_search(&f).is_ok() {
                continue;
            }
            let p = mesh.tri_pos(f);
            let out = (0..3).any(|k| p.iter().all(|q| q[k] < lo[k]) || p.iter().all(|q| q[k] > hi[k]));
            if !out {
                sub_f.push(f);
            }
        }
        let mut vid: HashMap<u32, u32> = HashMap::new();
        let mut sv: Vec<Vec3> = Vec::new();
        let mut st: Vec<[u32; 3]> = Vec::new();
        for &f in &sub_f {
            let t = mesh.triangles[f].map(|x| {
                *vid.entry(x).or_insert_with(|| {
                    sv.push(mesh.vertices[x as usize]);
                    (sv.len() - 1) as u32
                })
            });
            st.push(t);
        }
        let sub = Mesh::new(sv, st);
        let mask: Vec<bool> = (0..sub_f.len()).map(|i| i < act.len()).collect();
        good = crate::alphawrap::crossing_pairs_opt(&sub, Some(&mask), false).is_empty();
    }
    if std::env::var_os("MESHFIX_TIMING").is_some() {
        eprintln!("micro cluster {} vertices, rep {r}: bad edges {d0} -> {d1}, kept {good}", g.len());
    }
    if good {
        mesh.drop_collapsed();
    } else {
        for (&f, t) in fs.iter().zip(old) {
            mesh.triangles[f] = t;
        }
    }
    good
}

pub fn remove_micro_clusters(mesh: &mut Mesh) -> usize {
    let limit = zero_area_limit(mesh);
    let bad: Vec<usize> = (0..mesh.triangles.len())
        .filter(|&f| {
            let [a, b, c] = mesh.tri_pos(f);
            tri_area(a, b, c) < limit
        })
        .collect();
    if bad.is_empty() {
        return 0;
    }
    let nv = mesh.vertices.len();
    let mut par: Vec<u32> = (0..nv as u32).collect();
    fn root(p: &mut [u32], mut x: u32) -> u32 {
        while p[x as usize] != x {
            p[x as usize] = p[p[x as usize] as usize];
            x = p[x as usize];
        }
        x
    }
    let mut inb = vec![false; nv];
    for &f in &bad {
        let t = mesh.triangles[f];
        for &x in &t {
            inb[x as usize] = true;
        }
        for k in 1..3 {
            let (a, b) = (root(&mut par, t[0]), root(&mut par, t[k]));
            if a != b {
                par[a.max(b) as usize] = a.min(b);
            }
        }
    }
    let mut valence = vec![0u32; nv];
    for t in &mesh.triangles {
        for &x in t {
            valence[x as usize] += 1;
        }
    }
    let mut groups: HashMap<u32, Vec<u32>> = HashMap::new();
    for v in 0..nv as u32 {
        if inb[v as usize] {
            let r = root(&mut par, v);
            groups.entry(r).or_default().push(v);
        }
    }
    let mut groups: Vec<Vec<u32>> = groups.into_values().collect();
    groups.sort_by_key(|g| g[0]);
    let best = |g: &Vec<u32>| *g.iter().max_by_key(|&&v| (valence[v as usize], std::cmp::Reverse(v))).unwrap();
    let apply = |mesh: &mut Mesh, gs: &[(Vec<u32>, u32)]| {
        let mut remap: Vec<u32> = (0..mesh.vertices.len() as u32).collect();
        for (g, r) in gs {
            let r = *r;
            for &v in g {
                remap[v as usize] = r;
            }
        }
        for t in &mut mesh.triangles {
            for x in t.iter_mut() {
                *x = remap[*x as usize];
            }
        }
        mesh.drop_collapsed();
    };
    let ok = |before: &Mesh, after: &Mesh| after.defect_edges() <= before.defect_edges() && new_crossings(before, after) == 0;
    let before = mesh.clone();
    let all: Vec<(Vec<u32>, u32)> = groups.iter().map(|g| (g.clone(), best(g))).collect();
    apply(mesh, &all);
    let mut n = groups.len();
    if !ok(&before, mesh) {
        *mesh = before;
        n = 0;
        // Each cluster alone; the vertex with the most faces first, then
        // the other vertices of the cluster.
        for g in &groups {
            let b0 = best(g);
            let mut tries: Vec<(Vec<u32>, u32)> = vec![(g.clone(), b0)];
            tries.extend(g.iter().copied().filter(|&v| v != b0).take(7).map(|r| (g.clone(), r)));
            // Then one edge of the group at a time, to each end.
            for i in 0..g.len().min(8) {
                for j in i + 1..g.len().min(8) {
                    tries.push((vec![g[i], g[j]], g[i]));
                    tries.push((vec![g[i], g[j]], g[j]));
                }
            }
            for (g, r) in tries {
                if try_collapse(mesh, &g, r) {
                    n += 1;
                    break;
                }
            }
        }
    }
    // Clusters that no collapse can remove: cut out 1 to 3 rings around
    // them and fill the hole again.
    let tiny_pos: Vec<Vec<Vec3>> = groups
        .iter()
        .filter(|g| g.iter().all(|&v| (v as usize) < mesh.vertices.len()))
        .map(|g| g.iter().map(|&v| mesh.vertices[v as usize]).collect())
        .collect();
    let count_tiny = |m: &Mesh| {
        let l = zero_area_limit(m);
        (0..m.triangles.len())
            .filter(|&f| {
                let [a, b, c] = m.tri_pos(f);
                tri_area(a, b, c) < l
            })
            .count()
    };
    for pos in tiny_pos {
        let t0 = count_tiny(mesh);
        if t0 == 0 {
            break;
        }
        let want: HashSet<[u64; 3]> = pos.iter().map(|p| [p[0].to_bits(), p[1].to_bits(), p[2].to_bits()]).collect();
        // Still a vertex of a tiny face?
        let l = zero_area_limit(mesh);
        let still = (0..mesh.triangles.len()).any(|f| {
            let [a, b, c] = mesh.tri_pos(f);
            tri_area(a, b, c) < l && [a, b, c].iter().any(|p| want.contains(&[p[0].to_bits(), p[1].to_bits(), p[2].to_bits()]))
        });
        if !still {
            continue;
        }
        for rings in 1..=3 {
            let b = mesh.clone();
            excise_and_fill(mesh, rings, &pos);
            let good = ok(&b, mesh) && count_tiny(mesh) < t0;
            if std::env::var_os("MESHFIX_TIMING").is_some() {
                eprintln!("micro cluster at {:?}: cut out {rings} rings: {}", pos[0], if good { "kept" } else { "undone" });
            }
            if good {
                n += 1;
                break;
            }
            *mesh = b;
        }
    }
    if n > 0 {
        mesh.compact();
    }
    n
}

pub fn zero_area_limit(mesh: &Mesh) -> f64 {
    let n = mesh.triangles.len();
    if n == 0 {
        return 0.0;
    }
    let total: f64 = (0..n)
        .map(|f| {
            let [a, b, c] = mesh.tri_pos(f);
            tri_area(a, b, c)
        })
        .sum();
    (total / n as f64 * 1e-10).max(1e-24)
}

/// Last cleanup: each face with an area below the limit of the program's
/// check goes away by an edge collapse (link condition, no face turns over)
/// or by a flip of its longest edge. No face is deleted, so a closed mesh
/// stays closed. Returns the number of changes.
pub fn remove_zero_area(mesh: &mut Mesh) -> usize {
    remove_zero_area_with(mesh, false)
}

/// As `remove_zero_area`. With `thorough`, a face that the first try
/// cannot remove also tries the collapse of each of its edges, to the
/// midpoint and to each end, and more rounds are done (library stage).
pub fn remove_zero_area_with(mesh: &mut Mesh, thorough: bool) -> usize {
    let mut total = 0;
    let rounds = if thorough { 64 } else { 16 };
    for _ in 0..rounds {
        let limit = zero_area_limit(mesh);
        let cand: Vec<usize> = (0..mesh.triangles.len())
            .filter(|&f| {
                let [a, b, c] = mesh.tri_pos(f);
                tri_area(a, b, c) < limit
            })
            .collect();
        if cand.is_empty() {
            break;
        }
        let vf = VertexFaces::new(mesh);
        let nv = mesh.vertices.len();
        let mut locked = vec![false; nv];
        let mut remap: Vec<u32> = (0..nv as u32).collect();
        let mut collapsed = 0;
        let mut flipped = 0;
        let mut new_edges: HashSet<EdgeKey> = HashSet::new();
        let mut col_log: Vec<(u32, u32, Vec3)> = Vec::new();
        let mut flip_log: Vec<(usize, usize, [u32; 3], [u32; 3])> = Vec::new();
        for &f in &cand {
            let t = mesh.triangles[f];
            if t.iter().any(|&v| locked[v as usize]) {
                continue;
            }
            let p = mesh.tri_pos(f);
            let lens = [dist(p[0], p[1]), dist(p[1], p[2]), dist(p[2], p[0])];
            // 1. Collapse the shortest edge (a needle).
            let mut order = [0usize, 1, 2];
            order.sort_by(|&x, &y| lens[x].total_cmp(&lens[y]));
            let tries: &[usize] = if thorough { &order } else { &order[..1] };
            let mut done = false;
            'edges: for &i in tries {
                let (a, b) = (t[i], t[(i + 1) % 3]);
                let faces = vf.edge(mesh, a, b);
                if faces.len() != 2 {
                    continue;
                }
                let (ra, rb) = (vf.ring(mesh, a), vf.ring(mesh, b));
                let common = ra.iter().filter(|x| rb.binary_search(x).is_ok()).count();
                if common != 2 || (vf.on_rim(mesh, a) && vf.on_rim(mesh, b)) {
                    continue;
                }
                let (pa, pb) = (mesh.vertices[a as usize], mesh.vertices[b as usize]);
                let mid = scale(add(pa, pb), 0.5);
                let dests: &[Vec3] = if thorough { &[mid, pa, pb] } else { &[mid] };
                for &to in dests {
                    let ok = if thorough {
                        collapse_keeps_normals_lim(mesh, &vf, a, b, to, limit)
                    } else {
                        collapse_keeps_normals(mesh, &vf, a, b, to)
                    };
                    if ok {
                        col_log.push((a, b, mesh.vertices[a as usize]));
                        mesh.vertices[a as usize] = to;
                        remap[b as usize] = a;
                        for &x in ra.iter().chain(rb.iter()) {
                            locked[x as usize] = true;
                        }
                        locked[a as usize] = true;
                        locked[b as usize] = true;
                        collapsed += 1;
                        done = true;
                        break 'edges;
                    }
                }
            }
            if done {
                continue;
            }
            // 2. Flip the longest edge (a cap).
            let i = (0..3).max_by(|&x, &y| lens[x].total_cmp(&lens[y])).unwrap();
            let (a, b, c) = (t[i], t[(i + 1) % 3], t[(i + 2) % 3]);
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
            let cd = ekey(c, d);
            if d == c || !vf.edge(mesh, c, d).is_empty() || new_edges.contains(&cd) {
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
            let ng = cross(sub(pa, pb), sub(pd, pb));
            if dot(n1, n2) <= 0.0 || dot(n1, ng) <= 0.0 || 0.5 * norm(n1) < limit || 0.5 * norm(n2) < limit {
                continue;
            }
            flip_log.push((f, g, mesh.triangles[f], mesh.triangles[g]));
            mesh.triangles[f] = [a, d, c];
            mesh.triangles[g] = [d, b, c];
            new_edges.insert(cd);
            for v in [a, b, c, d] {
                locked[v as usize] = true;
            }
            flipped += 1;
        }
        // Collapses and flips that make a face cross another face go back.
        let (rc, rf) = undo_crossing_ops(mesh, &mut remap, &col_log, &flip_log, &[]);
        collapsed -= rc;
        flipped -= rf;
        if collapsed > 0 {
            mesh.apply_remap(&remap);
        }
        if collapsed + flipped == 0 {
            break;
        }
        total += collapsed + flipped;
    }
    total
}

// ---- Non-manifold split ------------------------------------------------

/// Pairs of faces on an edge with more than two faces. Each body is on the
/// back side of its faces, so a face that runs a to b pairs with the face
/// before it in the turn order around the edge (and that face runs b to a).
pub(crate) fn pair_around_edge(mesh: &Mesh, a: u32, b: u32, fs: &[u32]) -> Vec<(u32, u32)> {
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

/// An edge with 2k faces (k > 1) that vertex copies cannot split: the
/// sheets touch along the edge and meet again around both end points (a
/// pinch). Each face pair after the first gets its own copy of the edge:
/// a new middle vertex splits the two faces of the pair. The edge then has
/// two faces on each copy. Returns the number of edges split.
pub fn split_pinched_edges(mesh: &mut Mesh) -> usize {
    let ef = mesh.edge_faces();
    let mut todo: Vec<(u32, u32, Vec<(u32, u32)>)> = Vec::new();
    let mut used = vec![false; mesh.triangles.len()];
    let mut keys: Vec<(&EdgeKey, &Vec<u32>)> = ef.iter().filter(|(_, fs)| fs.len() >= 4 && fs.len() % 2 == 0).collect();
    keys.sort_unstable_by_key(|x| *x.0);
    for (&(a, b), fs) in keys {
        let mut pairs = pair_around_edge(mesh, a, b, fs);
        if 2 * pairs.len() != fs.len() {
            // The turn order does not alternate (the faces cross at the
            // edge). Pair the directions in face order: each pair still
            // gives an edge with two opposite faces.
            let plus: Vec<u32> = fs.iter().copied().filter(|&f| directed(&mesh.triangles[f as usize], a, b)).collect();
            let minus: Vec<u32> = fs.iter().copied().filter(|&f| directed(&mesh.triangles[f as usize], b, a)).collect();
            if plus.len() != minus.len() || 2 * plus.len() != fs.len() {
                continue;
            }
            pairs = plus.into_iter().zip(minus).collect();
        }
        // A face can be split only one time in this pass.
        if fs.iter().any(|&f| used[f as usize]) {
            continue;
        }
        for &f in fs {
            used[f as usize] = true;
        }
        todo.push((a, b, pairs));
    }
    let n = todo.len();
    let mut add: Vec<[u32; 3]> = Vec::new();
    for (a, b, pairs) in todo {
        let mid = scale(add3(mesh.vertices[a as usize], mesh.vertices[b as usize]), 0.5);
        for &(f, g) in pairs.iter().skip(1) {
            let m = mesh.vertices.len() as u32;
            mesh.vertices.push(mid);
            for h in [f, g] {
                let t = mesh.triangles[h as usize];
                // Turn the face so that it starts with the edge.
                let t = if directed(&t, a, b) { rotate_to(t, a) } else { rotate_to(t, b) };
                let (u, v, c) = (t[0], t[1], t[2]);
                mesh.triangles[h as usize] = [u, m, c];
                add.push([m, v, c]);
            }
        }
    }
    mesh.triangles.extend(add);
    n
}

/// Parts below this |volume| (normalized units cubed) are removed by
/// remove_inverted_parts: the limit of RemoveTinyShells.
pub const TINY_PART_VOLUME: f64 = 0.001;

#[inline]
fn add3(a: Vec3, b: Vec3) -> Vec3 {
    [a[0] + b[0], a[1] + b[1], a[2] + b[2]]
}

/// Remove each part (faces joined by vertices, as the program counts parts)
/// with a negative volume that is small: below `rel` times the volume of
/// all parts. Such a part is a thin void left by the union. Returns the
/// number of parts removed. A part with |volume| below TINY_PART_VOLUME
/// goes too.
pub fn remove_inverted_parts(mesh: &mut Mesh, rel: f64) -> usize {
    if mesh.triangles.is_empty() {
        return 0;
    }
    let nv = mesh.vertices.len();
    let mut parent: Vec<u32> = (0..nv as u32).collect();
    for t in &mesh.triangles {
        for i in 1..3 {
            let (ra, rb) = (find(&mut parent, t[0]), find(&mut parent, t[i]));
            if ra != rb {
                parent[ra.max(rb) as usize] = ra.min(rb);
            }
        }
    }
    let mut vol: std::collections::HashMap<u32, f64> = std::collections::HashMap::new();
    let mut total = 0.0;
    for f in 0..mesh.triangles.len() {
        let [a, b, c] = mesh.tri_pos(f);
        let v = det3(a, b, c) / 6.0;
        *vol.entry(find(&mut parent, mesh.triangles[f][0])).or_default() += v;
        total += v;
    }
    let limit = rel * total.abs();
    // A part with almost no volume: its sign is noise (the program sums in
    // absolute coordinates), so it goes too.
    let bad: std::collections::HashSet<u32> =
        vol.iter().filter(|(_, &v)| (v < 0.0 && -v < limit) || v.abs() < TINY_PART_VOLUME).map(|(&r, _)| r).collect();
    if bad.is_empty() {
        return 0;
    }
    let keep: Vec<bool> = (0..mesh.triangles.len()).map(|f| !bad.contains(&find(&mut parent, mesh.triangles[f][0]))).collect();
    mesh.retain_faces(&keep);
    bad.len()
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
            n if n > 2 => {
                // Split only when each face gets a partner. A face without
                // a partner would get an open edge: then the edge stays as
                // it is (all faces stay joined).
                let p = pair_around_edge(mesh, a, b, fs);
                if 2 * p.len() == n {
                    p
                } else {
                    (1..n).map(|i| (fs[0], fs[i])).collect()
                }
            }
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
