//! Alpha wrap with an offset (the idea of CGAL Alpha_wrap_3, Portaneri et
//! al., SIGGRAPH 2022), then a shrink of the wrap onto the input.
//!
//! The wrap does not repair the input. It makes a new surface around it:
//! 1. A 3D Delaunay triangulation starts with the 8 corners of a box around
//!    the input. All cells in the box are "inside".
//! 2. A ball of radius `alpha` goes in from the outside. A face between an
//!    outside and an inside cell is a gate when the smallest empty sphere
//!    through its 3 vertices has a radius >= alpha.
//! 3. At a gate, the inside cell becomes outside, except when it is too
//!    near the input. Then a Steiner point on the offset surface (distance
//!    `offset` from the input) goes into the triangulation.
//! 4. When no gate is left, the faces between outside and inside cells are
//!    the wrap. Outside cells never touch the input, so the wrap encloses
//!    it. Vertices where the wrap is not a 2-manifold get more inside cells.
//!
//! The result is closed, 2-manifold, without self-intersections, and each
//! shell is outward. It removes the inner parts of an assembly, and fills
//! gaps narrower than about 2·alpha.
//!
//! The shrink then moves each wrap vertex to `gap` from its closest input
//! point. A move that turns a face too much is made smaller. Faces that
//! cross after the moves get their vertices back towards the wrap until no
//! face crosses. Then faces that stay far from the input are split, and the
//! new vertices move the same way (refine passes): the surface goes into
//! the concave places that the ball bridged.
//!
//! Limits: a cavity whose entry is narrower than about 2·alpha is filled
//! (the Windows repair keeps such cavities). A few faces of the raw wrap
//! can form very thin spikes (two faces on one edge at ~180 degrees); they
//! do not cross other faces.

use crate::aabb::TriTree;
use crate::delaunay::*;
use crate::math::*;
use crate::Mesh;
use rayon::prelude::*;
use robust::{orient3d, Coord3D};
use std::cmp::Ordering;
use std::collections::{BinaryHeap, HashMap};
use std::time::Instant;

#[derive(Clone, Debug)]
pub struct WrapOptions {
    /// Radius of the ball that goes in (model units).
    pub alpha: f64,
    /// Distance of the Steiner points from the input (model units).
    pub offset: f64,
    /// Stop when the triangulation has more points (the wrap then fails).
    pub max_points: usize,
    /// Shrink the wrap onto the input.
    pub shrink: bool,
    /// The distance from the input where the shrink stops (model units).
    pub gap: f64,
    /// Passes of "split the faces that stay far from the input, move the
    /// new vertices" after the shrink.
    pub refine_passes: usize,
    /// A face is refined when its centroid is farther than gap + refine_tol
    /// from the input.
    pub refine_tol: f64,
}

#[derive(Clone, Debug, Default)]
pub struct WrapReport {
    pub alpha: f64,
    pub offset: f64,
    pub points: usize,
    pub cells: usize,
    pub gates: usize,
    pub carved: usize,
    pub steiner: usize,
    pub manifold_flips: usize,
    /// Closed outside pockets made inside.
    pub pockets: usize,
    pub faces: usize,
    /// The point limit stopped the wrap.
    pub stopped: bool,
    pub shrink_moved: usize,
    pub shrink_kept: usize,
    /// Vertices added by the refine passes of the shrink.
    pub refined: usize,
    pub crossing_faces: usize,
    pub t_wrap: f64,
    /// Parts of t_wrap: Steiner point search (parallel), Delaunay insertion.
    pub t_steiner: f64,
    pub t_insert: f64,
    pub t_manifold: f64,
    pub t_shrink: f64,
    /// Check of the extracted wrap: open, non-manifold, flipped edges,
    /// shells with a negative volume.
    pub bad_open: usize,
    pub bad_nm: usize,
    pub bad_flipped: usize,
    pub bad_shells: usize,
}

#[derive(Clone, Copy)]
struct Gate {
    r2: f64,
    cell: u32,
    face: u8,
    stamp: u32,
}

impl PartialEq for Gate {
    fn eq(&self, o: &Self) -> bool {
        self.r2 == o.r2
    }
}
impl Eq for Gate {}
impl PartialOrd for Gate {
    fn partial_cmp(&self, o: &Self) -> Option<Ordering> {
        Some(self.cmp(o))
    }
}
impl Ord for Gate {
    fn cmp(&self, o: &Self) -> Ordering {
        // Largest sphere first; ties by cell id for a fixed order.
        self.r2
            .partial_cmp(&o.r2)
            .unwrap_or(Ordering::Equal)
            .then_with(|| o.cell.cmp(&self.cell))
            .then_with(|| o.face.cmp(&self.face))
    }
}

struct Wrapper<'a> {
    tree: &'a TriTree,
    del: Delaunay,
    /// Circumcenter and squared radius of each cell slot.
    cc: Vec<(Vec3, f64)>,
    heap: BinaryHeap<Gate>,
    alpha2: f64,
    offset: f64,
    rep: WrapReport,
}

impl<'a> Wrapper<'a> {
    fn update_cc(&mut self, c: u32) {
        if self.cc.len() < self.del.cells.len() {
            self.cc.resize(self.del.cells.len(), ([0.0; 3], 0.0));
        }
        let p = self.del.cell_pts(c);
        self.cc[c as usize] = circumsphere(p[0], p[1], p[2], p[3]);
    }

    /// Squared radius of the smallest empty sphere through face i of c.
    fn gate_r2(&self, c: u32, i: usize) -> f64 {
        let cell = &self.del.cells[c as usize];
        let n = cell.n[i];
        let f: Vec<Vec3> = (0..4).filter(|&k| k != i).map(|k| self.del.pts[cell.v[k] as usize]).collect();
        let (fc, fr2) = circumcircle(f[0], f[1], f[2]);
        let oc = self.del.pts[cell.v[i] as usize];
        let nc = &self.del.cells[n as usize];
        let j = nc.index_of_neighbor(c);
        let on = self.del.pts[nc.v[j] as usize];
        if dist2(oc, fc) > fr2 && dist2(on, fc) > fr2 {
            fr2
        } else {
            let rc = if cell.infinite() { f64::MAX } else { self.cc[c as usize].1 };
            let rn = if nc.infinite() { f64::MAX } else { self.cc[n as usize].1 };
            rc.min(rn)
        }
    }

    /// Queue face i of the outside cell c when the ball goes through it.
    fn push_gate(&mut self, c: u32, i: usize) {
        let r2 = self.gate_r2(c, i);
        if r2 >= self.alpha2 {
            let stamp = self.del.cells[c as usize].stamp;
            self.heap.push(Gate { r2, cell: c, face: i as u8, stamp });
            self.rep.gates += 1;
        }
    }

    /// Steiner point for the gate from outside cell c to inside cell n.
    /// It reads only the cells c and n (and the input): a result stays
    /// good while these two cells do not change.
    fn steiner(&self, c: u32, n: u32) -> Option<Vec3> {
        let ncc = self.cc[n as usize].0;
        if self.tree.within(ncc, self.offset) {
            // Rule 1: the Voronoi edge of the gate goes into the offset
            // region. For an infinite cell c the edge is a ray; the
            // circumcenter of c is a far point on it.
            let ccc = self.cc[c as usize].0;
            if let Some(p) = self.tree.first_hit(ccc, ncc, self.offset) {
                return Some(p);
            }
        }
        // Rule 2: the cell touches the input.
        let tet = self.del.cell_pts(n);
        // A ball around the circumcenter that holds the 4 vertices holds the
        // cell (also when the f64 circumcenter of a sliver is not exact).
        let cc = self.cc[n as usize].0;
        let r2 = tet.iter().map(|&p| dist2(cc, p)).fold(0.0, f64::max);
        let ball = if r2.is_finite() { Some((cc, r2 * (1.0 + 1e-9) + 1e-24)) } else { None };
        if self.tree.hits_tet(&tet, ball) {
            let (cp, d2) = self.tree.closest(ncc);
            let mut dir = sub(ncc, cp);
            if d2 < 1e-30 {
                // The circumcenter is on the input: go to the side of the
                // outside cell.
                let cen = scale(add(add(tet[0], tet[1]), add(tet[2], tet[3])), 0.25);
                dir = sub(self.cc[c as usize].0, cen);
            }
            let l = norm(dir);
            if l < 1e-300 {
                return None;
            }
            return Some(add(cp, scale(dir, self.offset / l)));
        }
        None
    }

    /// The gate (c, face) still leads from an outside to an inside cell:
    /// Some((n, stamp of n)).
    fn live_gate(&self, g: &Gate) -> Option<(u32, u32)> {
        let cell = &self.del.cells[g.cell as usize];
        if !cell.alive || cell.stamp != g.stamp || !cell.outside {
            return None;
        }
        let n = cell.n[g.face as usize];
        if n == NONE || self.del.cells[n as usize].outside {
            return None;
        }
        Some((n, self.del.cells[n as usize].stamp))
    }

    /// The gates go out of the queue in batches. The Steiner points of a
    /// batch are found in parallel (read only). Then the batch is applied
    /// in order; a gate whose two cells changed in the meantime goes back
    /// into the queue.
    fn run(&mut self, max_points: usize) {
        const BATCH: usize = 256;
        let mut batch: Vec<(Gate, u32, u32)> = Vec::with_capacity(BATCH);
        let mut taken: Vec<u32> = Vec::with_capacity(BATCH);
        let mut later: Vec<Gate> = Vec::new();
        loop {
            batch.clear();
            taken.clear();
            while batch.len() < BATCH {
                let Some(g) = self.heap.pop() else { break };
                let Some((n, ns)) = self.live_gate(&g) else { continue };
                if taken.contains(&n) {
                    // One inside cell once per batch.
                    later.push(g);
                    continue;
                }
                taken.push(n);
                batch.push((g, n, ns));
            }
            self.heap.extend(later.drain(..));
            if batch.is_empty() {
                break;
            }
            let ts = Instant::now();
            let points: Vec<Option<Vec3>> = {
                let me = &*self;
                batch.par_iter().map(|&(g, n, _)| me.steiner(g.cell, n)).collect()
            };
            self.rep.t_steiner += ts.elapsed().as_secs_f64();
            for (k, &(g, n, ns)) in batch.iter().enumerate() {
                match self.live_gate(&g) {
                    Some((n2, ns2)) if n2 == n && ns2 == ns => {}
                    Some(_) => {
                        // Cell n changed: the gate goes back.
                        self.push_gate(g.cell, g.face as usize);
                        continue;
                    }
                    None => continue,
                }
                if self.step(n, points[k], max_points) {
                    return;
                }
            }
        }
    }

    /// Apply the result of the gate to the inside cell n: insert the
    /// Steiner point, or make n outside. True when the point limit stops
    /// the wrap.
    fn step(&mut self, n: u32, sp: Option<Vec3>, max_points: usize) -> bool {
        match sp {
            Some(p) => {
                if self.del.pts.len() >= max_points {
                    self.rep.stopped = true;
                    return true;
                }
                let ti = Instant::now();
                let ins = self.del.insert(p, n);
                self.rep.t_insert += ti.elapsed().as_secs_f64();
                if ins.is_none() {
                    return false;
                }
                self.rep.steiner += 1;
                let news = std::mem::take(&mut self.del.new_cells);
                for &nc in &news {
                    self.update_cc(nc);
                }
                for &nc in &news {
                    for i in 0..4 {
                        let nb = self.del.cells[nc as usize].n[i];
                        if nb == NONE {
                            continue;
                        }
                        let (a, b) = (self.del.cells[nc as usize].outside, self.del.cells[nb as usize].outside);
                        if a && !b {
                            self.push_gate(nc, i);
                        } else if !a && b {
                            let j = self.del.cells[nb as usize].index_of_neighbor(nc);
                            self.push_gate(nb, j);
                        }
                    }
                }
                self.del.new_cells = news;
            }
            None => {
                self.del.cells[n as usize].outside = true;
                self.rep.carved += 1;
                for j in 0..4 {
                    let m = self.del.cells[n as usize].n[j];
                    if m != NONE && !self.del.cells[m as usize].outside {
                        self.push_gate(n, j);
                    }
                }
            }
        }
        false
    }
}

/// Cells around each vertex (CSR: start offsets, cell ids).
fn incident_cells(del: &Delaunay) -> (Vec<u32>, Vec<u32>) {
    let nv = del.pts.len();
    let mut cnt = vec![0u32; nv + 1];
    for c in del.cells.iter().filter(|c| c.alive) {
        for &v in &c.v {
            cnt[v as usize + 1] += 1;
        }
    }
    for i in 0..nv {
        cnt[i + 1] += cnt[i];
    }
    let mut fill = cnt.clone();
    let mut ids = vec![0u32; cnt[nv] as usize];
    for (ci, c) in del.cells.iter().enumerate() {
        if !c.alive {
            continue;
        }
        for &v in &c.v {
            ids[fill[v as usize] as usize] = ci as u32;
            fill[v as usize] += 1;
        }
    }
    (cnt, ids)
}

/// Is the wrap not a 2-manifold at v? The inside cells around v and the
/// outside cells around v must each be one group, joined through faces
/// that hold v.
fn non_manifold_at(del: &Delaunay, inc: &[u32], v: u32, seen: &mut Vec<u32>) -> bool {
    let mut has_in = None;
    let mut has_out = None;
    for &c in inc {
        if del.cells[c as usize].outside {
            has_out = Some(c);
        } else if has_in.is_none() {
            has_in = Some(c);
        }
    }
    let (Some(si), Some(so)) = (has_in, has_out) else { return false };
    seen.clear();
    for start in [si, so] {
        let side = del.cells[start as usize].outside;
        let mut stack = vec![start];
        seen.push(start);
        while let Some(c) = stack.pop() {
            let cell = &del.cells[c as usize];
            let iv = cell.index_of_vertex(v).unwrap();
            for i in 0..4 {
                if i == iv {
                    continue;
                }
                let nb = cell.n[i];
                if nb == NONE || del.cells[nb as usize].outside != side || seen.contains(&nb) {
                    continue;
                }
                seen.push(nb);
                stack.push(nb);
            }
        }
    }
    seen.len() < inc.len()
}

/// Make the wrap a 2-manifold: around each bad vertex, outside cells
/// become inside until the vertex is good (as CGAL make_manifold).
fn make_manifold(del: &mut Delaunay) -> usize {
    let (start, ids) = incident_cells(del);
    let inc = |v: u32| &ids[start[v as usize] as usize..start[v as usize + 1] as usize];
    let mut seen = Vec::new();
    let mut stack: Vec<u32> = (N_SUPER..del.pts.len() as u32)
        .filter(|&v| non_manifold_at(del, inc(v), v, &mut seen))
        .collect();
    let mut flips = 0;
    let box_vertex = |v: u32| v < N_SUPER + 8;
    let mut guard = 0usize;
    while let Some(v) = stack.pop() {
        guard += 1;
        if guard > 50_000_000 {
            break;
        }
        if !non_manifold_at(del, inc(v), v, &mut seen) {
            continue;
        }
        let mut cand: Vec<u32> = inc(v)
            .iter()
            .copied()
            .filter(|&c| del.cells[c as usize].outside && !del.cells[c as usize].infinite())
            .collect();
        let key = |del: &Delaunay, c: u32| {
            let cell = &del.cells[c as usize];
            let artificial = cell.v.iter().any(|&x| box_vertex(x));
            let iv = cell.index_of_vertex(v).unwrap();
            let mut bf = 0;
            for i in 0..4 {
                if i != iv && !del.cells[cell.n[i] as usize].outside {
                    bf += 1;
                }
            }
            let p = del.cell_pts(c);
            let mut le = 0.0f64;
            for a in 0..4 {
                for b in a + 1..4 {
                    le = le.max(dist2(p[a], p[b]));
                }
            }
            (artificial, -bf, le)
        };
        cand.sort_by(|&a, &b| {
            let (ka, kb) = (key(del, a), key(del, b));
            ka.0.cmp(&kb.0).then(ka.1.cmp(&kb.1)).then(ka.2.partial_cmp(&kb.2).unwrap_or(Ordering::Equal))
        });
        let mut changed = Vec::new();
        for c in cand {
            del.cells[c as usize].outside = false;
            flips += 1;
            changed.push(c);
            if !non_manifold_at(del, inc(v), v, &mut seen) {
                break;
            }
        }
        for c in changed {
            for &w in &del.cells[c as usize].v {
                if w >= N_SUPER && w != v {
                    stack.push(w);
                }
            }
        }
    }
    flips
}

/// Outside cells that the outside region of the infinite cells cannot
/// reach become inside. The manifold step can close such a pocket; its
/// surface would be an inward shell. Returns the number of cells.
fn fill_pockets(del: &mut Delaunay) -> usize {
    let n = del.cells.len();
    let mut seen = vec![false; n];
    let mut stack: Vec<u32> = Vec::new();
    for (ci, c) in del.cells.iter().enumerate() {
        if c.alive && c.outside && c.infinite() {
            seen[ci] = true;
            stack.push(ci as u32);
        }
    }
    while let Some(c) = stack.pop() {
        for &nb in &del.cells[c as usize].n {
            if nb != NONE && !seen[nb as usize] && del.cells[nb as usize].outside {
                seen[nb as usize] = true;
                stack.push(nb);
            }
        }
    }
    let mut filled = 0;
    for ci in 0..n {
        let c = &mut del.cells[ci];
        if c.alive && c.outside && !seen[ci] {
            c.outside = false;
            filled += 1;
        }
    }
    filled
}

/// The faces between inside and outside cells, outward.
fn extract(del: &Delaunay) -> Mesh {
    let mut map: HashMap<u32, u32> = HashMap::new();
    let mut verts = Vec::new();
    let mut tris = Vec::new();
    for c in &del.cells {
        if !c.alive || c.outside {
            continue;
        }
        for i in 0..4 {
            let nb = c.n[i];
            if nb != NONE && !del.cells[nb as usize].outside {
                continue;
            }
            let mut t = [0u32; 3];
            for (k, &fi) in FACE[i].iter().enumerate() {
                let v = c.v[fi];
                let id = *map.entry(v).or_insert_with(|| {
                    verts.push(del.pts[v as usize]);
                    (verts.len() - 1) as u32
                });
                t[k] = id;
            }
            tris.push(t);
        }
    }
    Mesh::new(verts, tris)
}

/// Alpha wrap of `input`. None when the wrap could not finish.
pub fn wrap(input: &Mesh, o: &WrapOptions) -> (Option<Mesh>, WrapReport) {
    let rep = WrapReport { alpha: o.alpha, offset: o.offset, ..Default::default() };
    let Some((lo, hi)) = input.bbox() else { return (None, rep) };
    if o.alpha <= 0.0 || o.offset <= 0.0 {
        return (None, rep);
    }
    let t0 = Instant::now();
    let tree = TriTree::from_mesh(input);
    let m = 2.0 * (o.alpha + o.offset);
    let (blo, bhi) = (sub(lo, [m; 3]), add(hi, [m; 3]));
    let del = Delaunay::new(blo, bhi);
    let mut w = Wrapper {
        tree: &tree,
        del,
        cc: Vec::new(),
        heap: BinaryHeap::new(),
        alpha2: o.alpha * o.alpha,
        offset: o.offset,
        rep,
    };
    // Box corners, moved by a small fixed amount: 8 corners of a box are on
    // one sphere, and the exact signs then give ties.
    let ext = norm(sub(bhi, blo));
    for k in 0..8u32 {
        let jit = [
            ((k * 7 + 3) % 11) as f64 * 1e-7 * ext,
            ((k * 5 + 1) % 13) as f64 * 1e-7 * ext,
            ((k * 3 + 2) % 17) as f64 * 1e-7 * ext,
        ];
        let p = [
            if k & 1 == 0 { blo[0] } else { bhi[0] } + jit[0],
            if k & 2 == 0 { blo[1] } else { bhi[1] } + jit[1],
            if k & 4 == 0 { blo[2] } else { bhi[2] } + jit[2],
        ];
        w.del.insert(p, NONE);
    }
    for c in 0..w.del.cells.len() as u32 {
        if w.del.cells[c as usize].alive {
            w.update_cc(c);
        }
    }
    for c in 0..w.del.cells.len() as u32 {
        let cell = w.del.cells[c as usize];
        if !cell.alive || !cell.outside {
            continue;
        }
        for i in 0..4 {
            let nb = cell.n[i];
            if nb != NONE && !w.del.cells[nb as usize].outside {
                w.push_gate(c, i);
            }
        }
    }
    w.run(o.max_points);
    let mut rep = std::mem::take(&mut w.rep);
    let mut del = w.del;
    rep.points = del.pts.len();
    rep.cells = del.alive_cells();
    rep.t_wrap = t0.elapsed().as_secs_f64();
    if rep.stopped {
        return (None, rep);
    }
    let t1 = Instant::now();
    // A filled pocket can leave a bad vertex again: repeat both.
    for _ in 0..10 {
        rep.manifold_flips += make_manifold(&mut del);
        let filled = fill_pockets(&mut del);
        rep.pockets += filled;
        if filled == 0 {
            break;
        }
    }
    let mut mesh = extract(&del);
    rep.t_manifold = t1.elapsed().as_secs_f64();
    rep.faces = mesh.triangles.len();
    {
        let i = crate::mesh_info(&mesh);
        rep.bad_open = i.boundary_edges;
        rep.bad_nm = i.non_manifold_edges;
        rep.bad_flipped = i.flipped_edges;
        let ef = mesh.edge_faces();
        rep.bad_shells =
            crate::mesh::components(&mesh, &ef).iter().filter(|s| crate::mesh::shell_volume(&mesh, s) <= 0.0).count();
    }
    if mesh.triangles.is_empty() {
        return (None, rep);
    }
    if o.shrink {
        let t2 = Instant::now();
        shrink(&mut mesh, &tree, o.gap, o.refine_tol, o.refine_passes, &mut rep);
        rep.t_shrink = t2.elapsed().as_secs_f64();
    }
    (Some(mesh), rep)
}

// ---------------------------------------------------------------------------
// Shrink

#[inline]
fn c3(p: Vec3) -> Coord3D<f64> {
    Coord3D { x: p[0], y: p[1], z: p[2] }
}

#[inline]
fn o3(a: Vec3, b: Vec3, c: Vec3, d: Vec3) -> f64 {
    orient3d(c3(a), c3(b), c3(c), c3(d))
}

/// Does segment pq cross the inside of triangle abc? The signs are exact.
/// A crossing nearer than `tol` to the plane or to an edge of abc is a
/// touch, not a crossing: f64 rounding (for example of an edge midpoint)
/// gives such contacts of 1e-14 relative size.
fn seg_cross(p: Vec3, q: Vec3, a: Vec3, b: Vec3, c: Vec3, tol: f64) -> bool {
    let s1 = o3(a, b, c, p);
    let s2 = o3(a, b, c, q);
    if !((s1 > 0.0 && s2 < 0.0) || (s1 < 0.0 && s2 > 0.0)) {
        return false;
    }
    let e1 = o3(p, q, a, b);
    let e2 = o3(p, q, b, c);
    let e3 = o3(p, q, c, a);
    if !((e1 > 0.0 && e2 > 0.0 && e3 > 0.0) || (e1 < 0.0 && e2 < 0.0 && e3 < 0.0)) {
        return false;
    }
    let ln = norm(cross(sub(b, a), sub(c, a)));
    let (d1, d2) = (s1.abs() / ln, s2.abs() / ln);
    if d1.min(d2) <= tol {
        return false;
    }
    let x = add(p, scale(sub(q, p), d1 / (d1 + d2)));
    for (u, v) in [(a, b), (b, c), (c, a)] {
        let e = sub(v, u);
        if norm(cross(sub(x, u), e)) <= tol * norm(e) {
            return false;
        }
    }
    true
}

/// Do two faces cross? Faces with a common edge are not tested.
fn faces_cross(m: &Mesh, f: u32, g: u32, tol: f64) -> bool {
    let (tf, tg) = (m.triangles[f as usize], m.triangles[g as usize]);
    let shared: Vec<u32> = tf.iter().copied().filter(|v| tg.contains(v)).collect();
    if shared.len() >= 2 {
        return false;
    }
    let pf = m.tri_pos(f as usize);
    let pg = m.tri_pos(g as usize);
    let edges = [(0usize, 1usize), (1, 2), (2, 0)];
    for (a, b) in edges {
        if shared.len() == 1 && (tf[a] == shared[0] || tf[b] == shared[0]) {
            continue;
        }
        if seg_cross(pf[a], pf[b], pg[0], pg[1], pg[2], tol) {
            return true;
        }
    }
    for (a, b) in edges {
        if shared.len() == 1 && (tg[a] == shared[0] || tg[b] == shared[0]) {
            continue;
        }
        if seg_cross(pg[a], pg[b], pf[0], pf[1], pf[2], tol) {
            return true;
        }
    }
    false
}

/// Faces that cross another face (hash grid, then exact signs).
pub fn crossing_faces(m: &Mesh) -> Vec<u32> {
    faces_of(&crossing_pairs(m, None), m.triangles.len())
}

/// The face pairs (f < g, sorted) that cross or fold, only pairs with at
/// least one `active` face.
fn crossing_pairs(m: &Mesh, active: Option<&[bool]>) -> Vec<(u32, u32)> {
    crossing_pairs_opt(m, active, true)
}

/// As `crossing_pairs`; `folds` = false leaves out the folded pairs (the
/// faces that cross only).
pub(crate) fn crossing_pairs_opt(m: &Mesh, active: Option<&[bool]>, folds: bool) -> Vec<(u32, u32)> {
    let nf = m.triangles.len();
    if nf == 0 {
        return Vec::new();
    }
    let is_active = |f: u32| active.is_none_or(|a| a[f as usize]);
    // Contacts nearer than 1e-9 of the model size are touches.
    let tol = m.bbox().map(|(lo, hi)| dist(lo, hi)).unwrap_or(0.0) * 1e-9;
    let boxes: Vec<(Vec3, Vec3)> = (0..nf)
        .map(|f| {
            let p = m.tri_pos(f);
            let mut lo = p[0];
            let mut hi = p[0];
            for q in &p[1..] {
                for k in 0..3 {
                    lo[k] = lo[k].min(q[k]);
                    hi[k] = hi[k].max(q[k]);
                }
            }
            (lo, hi)
        })
        .collect();
    let mut sizes: Vec<f64> = boxes.iter().map(|(lo, hi)| (0..3).map(|k| hi[k] - lo[k]).fold(0.0, f64::max)).collect();
    let mid = sizes.len() / 2;
    sizes.select_nth_unstable_by(mid, |a, b| a.partial_cmp(b).unwrap());
    let cell = (sizes[mid] * 2.0).max(1e-9);
    let key = |p: Vec3| -> (i64, i64, i64) {
        ((p[0] / cell).floor() as i64, (p[1] / cell).floor() as i64, (p[2] / cell).floor() as i64)
    };
    // Grid of the active faces; each other face is then tested against the
    // active faces in its grid cells.
    let mut grid: HashMap<(i64, i64, i64), Vec<u32>> = HashMap::new();
    for (f, (lo, hi)) in boxes.iter().enumerate() {
        if !is_active(f as u32) {
            continue;
        }
        let (a, b) = (key(*lo), key(*hi));
        for x in a.0..=b.0 {
            for y in a.1..=b.1 {
                for z in a.2..=b.2 {
                    grid.entry((x, y, z)).or_default().push(f as u32);
                }
            }
        }
    }
    let overlap = |f: u32, g: u32| {
        let (bf, bg) = (&boxes[f as usize], &boxes[g as usize]);
        !(0..3).any(|k| bf.1[k] < bg.0[k] || bg.1[k] < bf.0[k])
    };
    let lists: Vec<&Vec<u32>> = grid.values().filter(|v| v.len() > 1).collect();
    let mut hits: Vec<(u32, u32)> = lists
        .par_iter()
        .flat_map_iter(|list| {
            let mut out = Vec::new();
            for i in 0..list.len() {
                for j in i + 1..list.len() {
                    let (f, g) = (list[i], list[j]);
                    if overlap(f, g) && faces_cross(m, f, g, tol) {
                        out.push((f, g));
                    }
                }
            }
            out
        })
        .collect();
    if active.is_some() {
        let rest: Vec<(u32, u32)> = (0..nf as u32)
            .into_par_iter()
            .filter(|&g| !is_active(g))
            .flat_map_iter(|g| {
                let (lo, hi) = boxes[g as usize];
                let (a, b) = (key(lo), key(hi));
                let mut out = Vec::new();
                for x in a.0..=b.0 {
                    for y in a.1..=b.1 {
                        for z in a.2..=b.2 {
                            if let Some(list) = grid.get(&(x, y, z)) {
                                for &f in list {
                                    if overlap(f, g) && faces_cross(m, f, g, tol) {
                                        out.push((f, g));
                                    }
                                }
                            }
                        }
                    }
                }
                out
            })
            .collect();
        hits.extend(rest);
    }
    // Faces on one edge that fold onto each other: the crossing test skips
    // faces with a common edge, so test their angle here.
    if folds {
        for (f, g) in folded_pairs(m) {
            if is_active(f) || is_active(g) {
                hits.push((f, g));
            }
        }
    }
    for h in hits.iter_mut() {
        if h.0 > h.1 {
            *h = (h.1, h.0);
        }
    }
    hits.sort_unstable();
    hits.dedup();
    hits
}

fn faces_of(pairs: &[(u32, u32)], nf: usize) -> Vec<u32> {
    let mut bad = vec![false; nf];
    for &(f, g) in pairs {
        bad[f as usize] = true;
        bad[g as usize] = true;
    }
    (0..nf as u32).filter(|&f| bad[f as usize]).collect()
}

/// Face pairs on one edge with normals more than ~170 degrees apart (the
/// two faces lie nearly on each other).
fn folded_pairs(m: &Mesh) -> Vec<(u32, u32)> {
    let mut keys: Vec<(u64, u32)> = Vec::with_capacity(m.triangles.len() * 3);
    for (f, t) in m.triangles.iter().enumerate() {
        for k in 0..3 {
            let (a, b) = (t[k], t[(k + 1) % 3]);
            let key = if a < b { ((a as u64) << 32) | b as u64 } else { ((b as u64) << 32) | a as u64 };
            keys.push((key, f as u32));
        }
    }
    keys.par_sort_unstable();
    let mut out = Vec::new();
    let mut i = 0;
    while i < keys.len() {
        let mut j = i + 1;
        while j < keys.len() && keys[j].0 == keys[i].0 {
            j += 1;
        }
        for x in i..j {
            for y in x + 1..j {
                let (f, g) = (keys[x].1, keys[y].1);
                let (nf, ng) = (face_normal(m, f as usize), face_normal(m, g as usize));
                if dot(nf, ng) < -0.985 * norm(nf) * norm(ng) {
                    out.push((f, g));
                }
            }
        }
        i = j;
    }
    out
}

fn face_normal(m: &Mesh, f: usize) -> Vec3 {
    let p = m.tri_pos(f);
    cross(sub(p[1], p[0]), sub(p[2], p[0]))
}

/// Move the vertices with `movable` (None = all) to `gap` from the input.
/// A move that turns a face more than about 80 degrees from its normal at
/// the start, or makes it nearly zero area, is made smaller. Then the faces
/// that cross get their vertices back towards the start until no face
/// crosses. Returns (moved, crossing faces at first, vertices kept).
fn settle(m: &mut Mesh, tree: &TriTree, gap: f64, movable: Option<&[bool]>) -> (usize, usize, usize) {
    let nv = m.vertices.len();
    let orig = m.vertices.clone();
    let base = crossing_pairs(m, None);
    let n0: Vec<Vec3> = (0..m.triangles.len()).map(|f| face_normal(m, f)).collect();
    let mut vf: Vec<Vec<u32>> = vec![Vec::new(); nv];
    for (f, t) in m.triangles.iter().enumerate() {
        for &v in t {
            vf[v as usize].push(f as u32);
        }
    }
    let targets: Vec<Option<Vec3>> = (0..nv)
        .into_par_iter()
        .map(|v| {
            if movable.is_some_and(|mv| !mv[v]) {
                return None;
            }
            let p = orig[v];
            let (cp, d2) = tree.closest(p);
            let d = d2.sqrt();
            if d <= gap * 1.0001 || d < 1e-300 {
                None
            } else {
                Some(add(cp, scale(sub(p, cp), gap / d)))
            }
        })
        .collect();
    // A move of v to x is good when each face around v keeps its side.
    let good = |m: &Mesh, v: usize, x: Vec3| -> bool {
        for &f in &vf[v] {
            let t = m.triangles[f as usize];
            let mut p = [m.vertices[t[0] as usize], m.vertices[t[1] as usize], m.vertices[t[2] as usize]];
            for k in 0..3 {
                if t[k] as usize == v {
                    p[k] = x;
                }
            }
            let n = cross(sub(p[1], p[0]), sub(p[2], p[0]));
            let a = &n0[f as usize];
            let (ln, la) = (norm(n), norm(*a));
            if ln < 1e-6 * la || dot(n, *a) < 0.17 * ln * la {
                return false;
            }
        }
        true
    };
    // Gauss-Seidel passes: a vertex that cannot go all the way now can go
    // later, when its neighbours moved.
    let mut done: Vec<bool> = targets.iter().map(|t| t.is_none()).collect();
    for _pass in 0..4 {
        let mut any = false;
        for v in 0..nv {
            if done[v] {
                continue;
            }
            let t = targets[v].unwrap();
            let cur = m.vertices[v];
            for s in [1.0, 0.5, 0.25] {
                let x = add(cur, scale(sub(t, cur), s));
                if good(m, v, x) {
                    m.vertices[v] = x;
                    any = true;
                    if s == 1.0 {
                        done[v] = true;
                    }
                    break;
                }
            }
        }
        if !any {
            break;
        }
    }
    let moved = (0..nv).filter(|&v| m.vertices[v] != orig[v]).count();
    // Faces that cross: their vertices go back towards the start. The
    // start has no crossing faces, thus this ends.
    let mut first = None;
    for round in 0..40 {
        // Only faces with a moved vertex can cross a face now.
        let active: Vec<bool> = m
            .triangles
            .iter()
            .map(|t| t.iter().any(|&v| m.vertices[v as usize] != orig[v as usize]))
            .collect();
        let mut pairs = crossing_pairs(m, Some(&active));
        // Pairs bad already at the start (for example the two faces of a
        // very thin spike of the wrap) cannot be made good by going back.
        pairs.retain(|p| base.binary_search(p).is_err());
        let bad = faces_of(&pairs, m.triangles.len());
        first.get_or_insert(bad.len());
        if bad.is_empty() {
            break;
        }
        if round == 39 {
            m.vertices = orig.clone();
            break;
        }
        let back = if round < 3 { 0.5 } else { 1.0 };
        let mut hit = vec![false; nv];
        for f in bad {
            for &v in &m.triangles[f as usize] {
                hit[v as usize] = true;
            }
        }
        for v in 0..nv {
            if hit[v] {
                m.vertices[v] = add(orig[v], scale(sub(m.vertices[v], orig[v]), 1.0 - back));
            }
        }
    }
    let kept = (0..nv).filter(|&v| movable.is_none_or(|mv| mv[v]) && m.vertices[v] == orig[v]).count();
    (moved, first.unwrap_or(0), kept)
}

/// Split the edges of the faces whose centroid is more than `tol` farther
/// from the input than `gap` (edges shorter than `min_len` stay). Returns
/// the new vertices as a mask.
fn refine(m: &mut Mesh, tree: &TriTree, gap: f64, tol: f64, min_len: f64) -> Vec<bool> {
    let nf = m.triangles.len();
    let lim = gap + tol;
    let far: Vec<bool> = (0..nf)
        .into_par_iter()
        .map(|f| {
            let p = m.tri_pos(f);
            let c = scale(add(add(p[0], p[1]), p[2]), 1.0 / 3.0);
            let (_, d2) = tree.closest_within(c, lim * lim);
            d2 == f64::MAX
        })
        .collect();
    let mut mid: HashMap<(u32, u32), u32> = HashMap::new();
    for f in 0..nf {
        if !far[f] {
            continue;
        }
        let t = m.triangles[f];
        for k in 0..3 {
            let (a, b) = (t[k], t[(k + 1) % 3]);
            let key = if a < b { (a, b) } else { (b, a) };
            if mid.contains_key(&key) || dist(m.vertices[a as usize], m.vertices[b as usize]) < min_len {
                continue;
            }
            let p = scale(add(m.vertices[a as usize], m.vertices[b as usize]), 0.5);
            m.vertices.push(p);
            mid.insert(key, (m.vertices.len() - 1) as u32);
        }
    }
    let mut new_mask = vec![false; m.vertices.len()];
    for &v in mid.values() {
        new_mask[v as usize] = true;
    }
    if mid.is_empty() {
        return new_mask;
    }
    let get = |a: u32, b: u32| mid.get(&if a < b { (a, b) } else { (b, a) }).copied();
    let mut out = Vec::with_capacity(nf + 3 * mid.len());
    for &t in &m.triangles {
        let e = [get(t[0], t[1]), get(t[1], t[2]), get(t[2], t[0])];
        let n = e.iter().filter(|x| x.is_some()).count();
        match n {
            0 => out.push(t),
            3 => {
                let (ab, bc, ca) = (e[0].unwrap(), e[1].unwrap(), e[2].unwrap());
                out.push([t[0], ab, ca]);
                out.push([ab, t[1], bc]);
                out.push([ca, bc, t[2]]);
                out.push([ab, bc, ca]);
            }
            1 => {
                // Rotate so that the split edge is t0-t1.
                let k = e.iter().position(|x| x.is_some()).unwrap();
                let (a, b, c) = (t[k], t[(k + 1) % 3], t[(k + 2) % 3]);
                let x = e[k].unwrap();
                out.push([a, x, c]);
                out.push([x, b, c]);
            }
            _ => {
                // Rotate so that the edge t2-t0 is the one not split.
                let k = e.iter().position(|x| x.is_none()).unwrap();
                let (a, b, c) = (t[(k + 1) % 3], t[(k + 2) % 3], t[k]);
                let (x, y) = (e[(k + 1) % 3].unwrap(), e[(k + 2) % 3].unwrap());
                // a-x-b-y-c: triangle x-b-y and the quad a-x-y-c.
                out.push([x, b, y]);
                let (pa, px, py, pc) = (
                    m.vertices[a as usize],
                    m.vertices[x as usize],
                    m.vertices[y as usize],
                    m.vertices[c as usize],
                );
                if dist2(pa, py) < dist2(px, pc) {
                    out.push([a, x, y]);
                    out.push([a, y, c]);
                } else {
                    out.push([a, x, c]);
                    out.push([x, y, c]);
                }
            }
        }
    }
    m.triangles = out;
    new_mask
}

/// Move the wrap vertices to `gap` from the input (see `settle`). Then
/// split faces that stay far from the input and move the new vertices, a
/// few times: the surface goes into the concave places that the ball
/// bridged.
pub fn shrink(m: &mut Mesh, tree: &TriTree, gap: f64, tol: f64, passes: usize, rep: &mut WrapReport) {
    let (moved, crossing, kept) = settle(m, tree, gap, None);
    rep.shrink_moved = moved;
    rep.crossing_faces = crossing;
    rep.shrink_kept = kept;
    for _ in 0..passes {
        let mask = refine(m, tree, gap, tol, 2.0 * tol);
        let added = mask.iter().filter(|&&b| b).count();
        if added == 0 {
            break;
        }
        rep.refined += added;
        let (_, crossing, kept) = settle(m, tree, gap, Some(&mask));
        rep.crossing_faces += crossing;
        rep.shrink_kept += kept;
    }
}
