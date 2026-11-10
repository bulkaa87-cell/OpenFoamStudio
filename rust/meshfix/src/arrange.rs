//! Union by a mesh arrangement and winding-number labels.
//!
//! This is the "winding" mode of the Wrap step (the default; the variable
//! MESHFIX_WRAP=union selects union.rs). It treats all faces as one soup,
//! also faces of open parts. Before it, planar.rs removes the glued caps in
//! axis planes.
//!
//! 1. Weld equal positions. Move each vertex by a very small vector (1e-6
//!    units, direction from a hash of the position), as union.rs does. This
//!    removes most exact coincidences.
//! 2. Find the face pairs that cross (exact orient3d signs) and their cut
//!    segments. Almost coplanar pairs are cut too: a pair that is not cut
//!    leaves a curve end, and the two sides of the curve meet there. An end
//!    point of a segment is "edge E crosses face T". It has one id for each
//!    (E, T), so all faces on E use the same point.
//! 3. Triple points: three faces that cross in one point. The decision is
//!    made one time for each triple, so the three faces agree.
//! 4. Cut each face again in its own plane (constrained Delaunay, spade),
//!    with all cut segments as constraints. The result is the arrangement:
//!    faces meet only at shared edges.
//! 5. Patches: pieces joined through edges with two pieces that are not on
//!    a cut curve. Each patch side gets the generalized winding number of the
//!    whole input (Jacobson et al. 2013, fast tree sum) at a few pieces.
//! 6. Around each edge, the pieces are sorted by angle. Two neighbor sides in
//!    this order look into the same space (a wedge). A minimum cut gives each
//!    patch side a label: inside or outside. The data cost is the side area
//!    times the distance of its winding number from 0.5; the cost of two
//!    labels in one wedge is MESHFIX_LAMBDA times the edge length. Thus a
//!    gap in the arrangement (an open edge, a missed cut) can separate two
//!    cells, but a correct cell keeps one label.
//! 7. A piece between an inside side and an outside side stays, turned so
//!    that the inside is behind it. All other pieces go.
//! 8. At an edge with more than two kept pieces, the pieces are paired by the
//!    angle order. Each manifold sheet gets its own vertex copies.
//!
//! Where the labels of one wedge differ, the result has an open edge. The
//! steps after the union in stages::wrap_winding close these.
use crate::math::*;
use crate::mesh::*;
use crate::union::{candidate_pairs, dbg_time, jitter, test_pair_x, Fwn, Hit, PairResult};
use rayon::prelude::*;
use robust::{orient3d, Coord3D};
use spade::{ConstrainedDelaunayTriangulation, Point2, Triangulation};
use std::collections::{HashMap, VecDeque};

#[derive(Clone, Debug, Default, PartialEq)]
pub struct ArrangeReport {
    /// Cut segments (face pairs that cross).
    pub pairs: usize,
    /// Pairs not cut: degenerate touch.
    pub degenerate: usize,
    /// Pairs not cut: almost coplanar contact.
    pub contact: usize,
    /// Points where three faces cross.
    pub triples: usize,
    /// Faces cut and triangulated again.
    pub cut_faces: usize,
    /// Faces where a constraint crossed another constraint (the CDT added
    /// a point that the other faces do not have).
    pub steiner_faces: usize,
    /// Faces that the CDT could not do (kept uncut).
    pub failed_faces: usize,
    /// Faces of the arrangement.
    pub pieces: usize,
    /// End points of cut curves inside a face (odd degree).
    pub dangling: usize,
    /// Patches (nodes of the minimum cut, two sides each).
    pub cells: usize,
    /// Patches removed (all their faces).
    pub removed_patches: usize,
    /// Faces kept / removed.
    pub kept: usize,
    pub removed: usize,
    /// Kept faces that got no partner at an edge.
    pub unpaired: usize,
    /// The stage stopped with a panic. The mesh is as before.
    pub panicked: bool,
    /// Original vertices that keep the small move (their faces crossed
    /// another face at the original position).
    pub unmoved: usize,
}

#[inline]
fn c3(p: Vec3) -> Coord3D<f64> {
    Coord3D { x: p[0], y: p[1], z: p[2] }
}

#[inline]
fn o3(a: Vec3, b: Vec3, c: Vec3, d: Vec3) -> f64 {
    orient3d(c3(a), c3(b), c3(c), c3(d))
}

fn find(p: &mut [u32], mut x: u32) -> u32 {
    while p[x as usize] != x {
        let g = p[p[x as usize] as usize];
        p[x as usize] = g;
        x = g;
    }
    x
}

fn join(p: &mut [u32], a: u32, b: u32) {
    let (ra, rb) = (find(p, a), find(p, b));
    if ra != rb {
        p[ra.max(rb) as usize] = ra.min(rb);
    }
}

/// A cut segment between faces a < b, from point p to point q, with the
/// triple points on it.
struct Seg {
    a: u32,
    b: u32,
    p: u32,
    q: u32,
    split: Vec<u32>,
}

#[derive(Default)]
struct Cuts {
    point: HashMap<(EdgeKey, u32), u32>,
    on_edge: HashMap<EdgeKey, Vec<(f64, u32)>>,
    inside: HashMap<u32, Vec<u32>>,
    segs: Vec<Seg>,
    seg_id: HashMap<(u32, u32), usize>,
    of_face: HashMap<u32, Vec<usize>>,
}

impl Cuts {
    fn point_of(&mut self, v: &mut Vec<Vec3>, h: Hit) -> u32 {
        let k = ekey(h.u, h.v);
        if let Some(&id) = self.point.get(&(k, h.tri)) {
            return id;
        }
        let (o0, o1) = if h.u == k.0 { (h.ou, h.ov) } else { (h.ov, h.ou) };
        let t = (o0 / (o0 - o1)).clamp(0.0, 1.0);
        let (p0, p1) = (v[k.0 as usize], v[k.1 as usize]);
        let id = v.len() as u32;
        v.push(add(p0, scale(sub(p1, p0), t)));
        self.point.insert((k, h.tri), id);
        self.on_edge.entry(k).or_default().push((t, id));
        self.inside.entry(h.tri).or_default().push(id);
        id
    }

    fn add_seg(&mut self, fa: u32, fb: u32, p: u32, q: u32) {
        if p == q {
            return;
        }
        let (a, b) = (fa.min(fb), fa.max(fb));
        let id = self.segs.len();
        self.segs.push(Seg { a, b, p, q, split: Vec::new() });
        self.seg_id.insert((a, b), id);
        self.of_face.entry(a).or_default().push(id);
        self.of_face.entry(b).or_default().push(id);
    }
}

/// Result of the CDT of one face.
struct FaceCut {
    tris: Vec<[u32; 3]>,
    /// New points that the CDT made (Steiner points), with positions.
    extra: Vec<Vec3>,
    /// (id, id) pairs at the same 2D position.
    aliases: Vec<(u32, u32)>,
    steiner: bool,
}

/// Triangulate face `f` again with all its points and constraint chains.
/// New Steiner points get ids from `next_id` on (local numbering; the
/// caller gives them their global ids).
fn cut_face(v: &[Vec3], t: [u32; 3], f: u32, cuts: &Cuts, chains_extra: &[Vec<u32>], next_id: u32) -> Option<FaceCut> {
    let c = [v[t[0] as usize], v[t[1] as usize], v[t[2] as usize]];
    let n = cross(sub(c[1], c[0]), sub(c[2], c[0]));
    let k = (0..3).max_by(|&x, &y| n[x].abs().total_cmp(&n[y].abs())).unwrap();
    if n[k] == 0.0 {
        return None;
    }
    let (ax, ay) = if n[k] > 0.0 { ((k + 1) % 3, (k + 2) % 3) } else { ((k + 2) % 3, (k + 1) % 3) };
    let proj = |p: Vec3| Point2::new(p[ax], p[ay]);
    let mut cdt: ConstrainedDelaunayTriangulation<Point2<f64>> = ConstrainedDelaunayTriangulation::new();
    let mut h2id: Vec<u32> = Vec::new();
    let mut aliases = Vec::new();
    let mut handle = |cdt: &mut ConstrainedDelaunayTriangulation<Point2<f64>>, id: u32, h2id: &mut Vec<u32>| {
        let h = cdt.insert(proj(v[id as usize])).ok()?;
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
    // Boundary chains: corners and the points on the three edges.
    let mut chains: Vec<Vec<spade::handles::FixedVertexHandle>> = Vec::new();
    for i in 0..3 {
        let (a, b) = (t[i], t[(i + 1) % 3]);
        let key = ekey(a, b);
        let mut pts: Vec<(f64, u32)> = cuts.on_edge.get(&key).cloned().unwrap_or_default();
        pts.sort_by(|x, y| x.0.total_cmp(&y.0));
        if a != key.0 {
            pts.reverse();
        }
        let mut chain = vec![handle(&mut cdt, a, &mut h2id)?];
        for (_, id) in pts {
            chain.push(handle(&mut cdt, id, &mut h2id)?);
        }
        chain.push(handle(&mut cdt, b, &mut h2id)?);
        chains.push(chain);
    }
    if let Some(ins) = cuts.inside.get(&f) {
        for &id in ins {
            handle(&mut cdt, id, &mut h2id)?;
        }
    }
    let mut inner: Vec<Vec<spade::handles::FixedVertexHandle>> = Vec::new();
    for ch in chains_extra {
        let mut hs = Vec::with_capacity(ch.len());
        for &id in ch {
            hs.push(handle(&mut cdt, id, &mut h2id)?);
        }
        inner.push(hs);
    }
    let mut steiner = false;
    let mut add_c = |cdt: &mut ConstrainedDelaunayTriangulation<Point2<f64>>, a, b| {
        if a == b || cdt.exists_constraint(a, b) {
            return;
        }
        let r = cdt.try_add_constraint(a, b);
        if r.is_empty() {
            steiner = true;
            cdt.add_constraint_and_split(a, b, |p| p);
        }
    };
    for chain in &chains {
        for w in chain.windows(2) {
            add_c(&mut cdt, w[0], w[1]);
        }
    }
    for ch in &inner {
        for w in ch.windows(2) {
            add_c(&mut cdt, w[0], w[1]);
        }
    }
    // Steiner points: back to 3D in the plane of the face.
    let (q0, q1, q2) = (proj(c[0]), proj(c[1]), proj(c[2]));
    let area = (q1.x - q0.x) * (q2.y - q0.y) - (q2.x - q0.x) * (q1.y - q0.y);
    let mut extra = Vec::new();
    for vh in cdt.vertices() {
        let i = vh.fix().index();
        if i < h2id.len() && h2id[i] != u32::MAX {
            continue;
        }
        let q = vh.position();
        let l1 = ((q.x - q0.x) * (q2.y - q0.y) - (q2.x - q0.x) * (q.y - q0.y)) / area;
        let l2 = ((q1.x - q0.x) * (q.y - q0.y) - (q.x - q0.x) * (q1.y - q0.y)) / area;
        let l0 = 1.0 - l1 - l2;
        let p3 = add(add(scale(c[0], l0), scale(c[1], l1)), scale(c[2], l2));
        if i >= h2id.len() {
            h2id.resize(i + 1, u32::MAX);
        }
        h2id[i] = next_id + extra.len() as u32;
        extra.push(p3);
    }
    // Faces outside the boundary chains: reachable from the hull without a
    // crossing of a constraint edge.
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
    let mut tris = Vec::with_capacity(faces.len());
    for (i, fh) in faces.iter().enumerate() {
        if outside[i] {
            continue;
        }
        let vs = fh.vertices();
        tris.push([h2id[vs[0].fix().index()], h2id[vs[1].fix().index()], h2id[vs[2].fix().index()]]);
    }
    if tris.is_empty() {
        return None;
    }
    Some(FaceCut { tris, extra, aliases, steiner })
}

/// Union of all faces by the arrangement and winding-number labels.
/// On a panic the mesh stays as it was.
pub fn winding_union(mesh: &mut Mesh) -> ArrangeReport {
    let backup = mesh.clone();
    match std::panic::catch_unwind(std::panic::AssertUnwindSafe(|| arrange(mesh))) {
        Ok(r) => r,
        Err(_) => {
            *mesh = backup;
            ArrangeReport { panicked: true, ..Default::default() }
        }
    }
}

fn arrange(mesh: &mut Mesh) -> ArrangeReport {
    let t0 = std::time::Instant::now();
    let mut rep = ArrangeReport::default();
    mesh.weld_exact();
    mesh.drop_collapsed();
    mesh.compact();
    let nf = mesh.triangles.len();
    if nf == 0 {
        return rep;
    }
    let nv0 = mesh.vertices.len();
    let orig = mesh.vertices.clone();
    // Faces of contacts that the overlay left (planar.rs): their pieces are
    // not joined into one patch with the pieces of other faces.
    let left = crate::planar::CONTACT_LEFT.with(|c| std::mem::take(&mut *c.borrow_mut()));
    let contact_src: Vec<bool> = if left.is_empty() || std::env::var_os("MESHFIX_NO_LEFT").is_some() {
        vec![false; nf]
    } else {
        (0..nf).map(|f| left.contains(&crate::planar::face_key(mesh, f))).collect()
    };
    // Axis planes: a coordinate value that many vertices share (a symmetry
    // plane, a section cut between two parts). The small move keeps such a
    // coordinate, so that faces in that plane stay exactly coplanar: two
    // glued caps then do not cross each other in random slivers.
    let keep_planes = std::env::var_os("MESHFIX_KEEP_PLANES").is_some();
    let mut plane_vals: [std::collections::HashSet<u64>; 3] = Default::default();
    if keep_planes {
        for k in 0..3 {
            let mut cnt: HashMap<u64, u32> = HashMap::new();
            for p in &orig {
                *cnt.entry(p[k].to_bits()).or_default() += 1;
            }
            plane_vals[k] = cnt.into_iter().filter(|&(_, c)| c >= PLANE_MIN_VERTICES).map(|(b, _)| b).collect();
        }
    }
    let moved: Vec<Vec3> = orig
        .iter()
        .map(|&p| {
            let mut j = scale(jitter(p), 1e-6);
            for k in 0..3 {
                if plane_vals[k].contains(&p[k].to_bits()) {
                    j[k] = 0.0;
                }
            }
            add(p, j)
        })
        .collect();
    let jm = Mesh::new(moved, mesh.triangles.clone());
    let all: Vec<u32> = (0..nf as u32).collect();
    let skip_contact = std::env::var_os("MESHFIX_SKIP_CONTACT").is_some();

    // 2. Crossing pairs and cut segments.
    let results: Vec<(u32, u32, PairResult)> = candidate_pairs(&jm, &all)
        .into_par_iter()
        .filter_map(|(fa, fb)| match test_pair_x(&jm, fa, fb, skip_contact) {
            PairResult::None => None,
            r => Some((fa, fb, r)),
        })
        .collect();
    dbg_time("arrange pairs", t0);
    let mut v = jm.vertices.clone();
    let mut cuts = Cuts::default();
    for (fa, fb, r) in results {
        match r {
            PairResult::Degenerate => rep.degenerate += 1,
            PairResult::Contact => rep.contact += 1,
            PairResult::Segment([h0, h1]) => {
                let p0 = cuts.point_of(&mut v, h0);
                let p1 = cuts.point_of(&mut v, h1);
                cuts.add_seg(fa, fb, p0, p1);
            }
            PairResult::FromVertex { va, vb, hit } => {
                // Welded above: the shared vertex has one id.
                let p1 = cuts.point_of(&mut v, hit);
                let _ = vb;
                cuts.add_seg(fa, fb, va, p1);
            }
            PairResult::None => {}
        }
    }
    rep.pairs = cuts.segs.len();

    // 3. Triple points: for faces a < b < c that cross pairwise, the
    // segment of (a, b) crosses face c. One decision for each triple.
    let mut triples: Vec<(usize, usize, usize, Vec3)> = Vec::new();
    {
        let tp = |f: u32| jm.tri_pos(f as usize);
        for (&a, list) in &cuts.of_face {
            // partners of a with a larger id
            let mut part: Vec<(u32, usize)> = list
                .iter()
                .map(|&s| {
                    let sg = &cuts.segs[s];
                    (if sg.a == a { sg.b } else { sg.a }, s)
                })
                .filter(|&(g, _)| g > a)
                .collect();
            part.sort_unstable();
            for i in 0..part.len() {
                for j in i + 1..part.len() {
                    let (b, sab) = part[i];
                    let (c, sac) = part[j];
                    let Some(&sbc) = cuts.seg_id.get(&(b.min(c), b.max(c))) else { continue };
                    let sg = &cuts.segs[sab];
                    let (pp, pq) = (v[sg.p as usize], v[sg.q as usize]);
                    let [c0, c1, c2] = tp(c);
                    let (op, oq) = (o3(c0, c1, c2, pp), o3(c0, c1, c2, pq));
                    if op == 0.0 || oq == 0.0 || (op > 0.0) == (oq > 0.0) {
                        continue;
                    }
                    let s = [o3(pp, pq, c0, c1), o3(pp, pq, c1, c2), o3(pp, pq, c2, c0)];
                    if !(s.iter().all(|&x| x > 0.0) || s.iter().all(|&x| x < 0.0)) {
                        continue;
                    }
                    let x = add(pp, scale(sub(pq, pp), op / (op - oq)));
                    triples.push((sab, sac, sbc, x));
                }
            }
        }
    }
    triples.sort_by(|x, y| (x.0, x.1, x.2).cmp(&(y.0, y.1, y.2)));
    rep.triples = triples.len();
    for (sab, sac, sbc, x) in triples {
        let id = v.len() as u32;
        v.push(x);
        let mut fs: Vec<u32> = Vec::with_capacity(6);
        for s in [sab, sac, sbc] {
            cuts.segs[s].split.push(id);
            fs.push(cuts.segs[s].a);
            fs.push(cuts.segs[s].b);
        }
        fs.sort_unstable();
        fs.dedup();
        for f in fs {
            cuts.inside.entry(f).or_default().push(id);
        }
    }
    // Each segment as a chain p, splits (by position on the segment), q.
    let chains: Vec<Vec<u32>> = cuts
        .segs
        .iter()
        .map(|s| {
            let (pp, pq) = (v[s.p as usize], v[s.q as usize]);
            let d = sub(pq, pp);
            let mut sp: Vec<(f64, u32)> = s.split.iter().map(|&x| (dot(sub(v[x as usize], pp), d), x)).collect();
            sp.sort_by(|x, y| x.0.total_cmp(&y.0));
            let mut ch = vec![s.p];
            ch.extend(sp.into_iter().map(|x| x.1));
            ch.push(s.q);
            ch
        })
        .collect();
    dbg_time("arrange segments", t0);

    // 4. Cut faces. Steiner points get local ids from STEINER on.
    let mut cut = vec![false; nf];
    for &f in cuts.of_face.keys().chain(cuts.inside.keys()) {
        cut[f as usize] = true;
    }
    {
        let ef = jm.edge_faces();
        for k in cuts.on_edge.keys() {
            if let Some(fs) = ef.get(k) {
                for &f in fs {
                    cut[f as usize] = true;
                }
            }
        }
    }
    let todo: Vec<u32> = (0..nf as u32).filter(|&f| cut[f as usize]).collect();
    let results: Vec<(u32, Option<FaceCut>)> = {
        let v = &v;
        let cuts = &cuts;
        let chains = &chains;
        let jm = &jm;
        todo.par_iter()
            .map(|&f| {
                let ch: Vec<Vec<u32>> = cuts
                    .of_face
                    .get(&f)
                    .map(|l| l.iter().map(|&s| chains[s].clone()).collect())
                    .unwrap_or_default();
                let t = jm.triangles[f as usize];
                let r = std::panic::catch_unwind(std::panic::AssertUnwindSafe(|| cut_face(v, t, f, cuts, &ch, STEINER)))
                    .unwrap_or(None);
                (f, r)
            })
            .collect()
    };
    dbg_time("arrange cdt", t0);
    let mut res_of: HashMap<u32, FaceCut> = results.into_iter().filter_map(|(f, r)| r.map(|r| (f, r))).collect();
    let mut tris: Vec<[u32; 3]> = Vec::with_capacity(nf * 2);
    let mut src: Vec<u32> = Vec::with_capacity(nf * 2);
    let mut aliases: Vec<(u32, u32)> = Vec::new();
    let mut steiner_face = vec![false; nf];
    for f in 0..nf as u32 {
        if !cut[f as usize] {
            tris.push(jm.triangles[f as usize]);
            src.push(f);
            continue;
        }
        match res_of.remove(&f) {
            Some(fc) => {
                rep.cut_faces += 1;
                rep.steiner_faces += fc.steiner as usize;
                steiner_face[f as usize] = fc.steiner;
                let base = v.len() as u32;
                v.extend_from_slice(&fc.extra);
                let g = |id: u32| if id >= STEINER { base + (id - STEINER) } else { id };
                for t in fc.tris {
                    tris.push([g(t[0]), g(t[1]), g(t[2])]);
                    src.push(f);
                }
                aliases.extend(fc.aliases.into_iter().map(|(x, y)| (g(x), g(y))));
            }
            None => {
                rep.failed_faces += 1;
                tris.push(jm.triangles[f as usize]);
                src.push(f);
            }
        }
    }

    // Weld: the same point can come from two cuts. The tolerance is far
    // below each repair tolerance.
    let nv = v.len();
    let mut parent: Vec<u32> = (0..nv as u32).collect();
    for (a, b) in aliases {
        join(&mut parent, a, b);
    }
    {
        let tol = 1e-9;
        let mut grid = Grid::new(tol);
        let mut near = Vec::new();
        for i in nv0..nv {
            let p = v[i];
            near.clear();
            grid.near(p, &mut near);
            if let Some(&j) = near.iter().find(|&&j| dist2(p, v[j as usize]) <= tol * tol) {
                join(&mut parent, i as u32, j);
            } else {
                grid.insert(i as u32, p);
            }
        }
    }
    let mut keep_t = Vec::with_capacity(tris.len());
    let mut keep_s = Vec::with_capacity(tris.len());
    for (t, s) in tris.into_iter().zip(src) {
        let t = [find(&mut parent, t[0]), find(&mut parent, t[1]), find(&mut parent, t[2])];
        if t[0] != t[1] && t[1] != t[2] && t[2] != t[0] {
            keep_t.push(t);
            keep_s.push(s);
        }
    }
    let tris = keep_t;
    let src = keep_s;
    let np = tris.len();
    rep.pieces = np;
    dbg_time("arrange weld", t0);

    // 5. Patches: pieces joined through edges with exactly two pieces in
    // opposite directions that are not on a cut curve. The arrangement
    // gives no reason for a different label inside a patch. An edge on a cut
    // curve, an edge of a face with a Steiner point and an open edge stop a
    // patch.
    let mut hard: std::collections::HashSet<EdgeKey> = std::collections::HashSet::new();
    for ch in &chains {
        for w in ch.windows(2) {
            let (a, b) = (find(&mut parent, w[0]), find(&mut parent, w[1]));
            if a != b {
                hard.insert(ekey(a, b));
            }
        }
    }
    // A cut curve that ends inside a face (a skipped pair, a failed
    // triple) lets the two sides of the curve meet around its end. The
    // faces at such an end are not joined into patches.
    let mut leaky = steiner_face.clone();
    {
        let mut deg: HashMap<u32, u32> = HashMap::new();
        for &(a, b) in &hard {
            *deg.entry(a).or_default() += 1;
            *deg.entry(b).or_default() += 1;
        }
        let odd: std::collections::HashSet<u32> = deg.iter().filter(|(_, &d)| d % 2 == 1).map(|(&x, _)| x).collect();
        rep.dangling = odd.len();
        for (f, t) in tris.iter().enumerate() {
            if t.iter().any(|x| odd.contains(x)) {
                leaky[src[f] as usize] = true;
            }
        }
    }
    let mut ef: HashMap<EdgeKey, Vec<u32>> = HashMap::with_capacity(np * 3 / 2 + 1);
    for (f, t) in tris.iter().enumerate() {
        for i in 0..3 {
            ef.entry(ekey(t[i], t[(i + 1) % 3])).or_default().push(f as u32);
        }
    }
    let mut edges: Vec<(EdgeKey, Vec<u32>)> = ef.into_iter().collect();
    edges.sort_unstable_by_key(|e| e.0);
    edges.par_iter_mut().for_each(|(k, fs)| {
        if fs.len() >= 3 {
            *fs = radial_order(&v, k.0, k.1, fs, &tris);
        }
    });
    let next_side = |f: u32, a: u32, b: u32| if directed(&tris[f as usize], a, b) { 2 * f } else { 2 * f + 1 };
    let mut pp: Vec<u32> = (0..np as u32).collect();
    for ((a, b), ord) in &edges {
        if ord.len() != 2 || hard.contains(&(*a, *b)) {
            continue;
        }
        let (f, g) = (ord[0], ord[1]);
        if leaky[src[f as usize] as usize] || leaky[src[g as usize] as usize] {
            continue;
        }
        if contact_src[src[f as usize] as usize] != contact_src[src[g as usize] as usize] {
            continue;
        }
        if directed(&tris[f as usize], *a, *b) == directed(&tris[g as usize], *a, *b) {
            continue;
        }
        join(&mut pp, f, g);
    }
    let mut patch_of = vec![0u32; np];
    let mut patches: Vec<Vec<u32>> = Vec::new();
    {
        let mut id: HashMap<u32, u32> = HashMap::new();
        for f in 0..np as u32 {
            let r = find(&mut pp, f);
            let p = *id.entry(r).or_insert_with(|| {
                patches.push(Vec::new());
                (patches.len() - 1) as u32
            });
            patch_of[f as usize] = p;
            patches[p as usize].push(f);
        }
    }
    let area: Vec<f64> = tris
        .iter()
        .map(|t| tri_area(v[t[0] as usize], v[t[1] as usize], v[t[2] as usize]))
        .collect();
    rep.cells = patches.len();
    dbg_time("arrange patches", t0);

    // 6. Winding number of the whole input on each side of a few pieces of
    // each patch. First at the piece centroid without the own source face:
    // -1/2 in front, +1/2 behind. A patch that is not clearly in or out then
    // gets side samples at the distance SIDE_EPS from the centroid, along
    // the normal of the source face. Thus a crack between two bodies that is
    // thinner than SIDE_EPS (two caps that nearly touch) gets the winding
    // number of the bodies, and its walls go. MESHFIX_SIDE_EPS=0: no side
    // samples.
    let side_eps = std::env::var("MESHFIX_SIDE_EPS").ok().and_then(|s| s.parse::<f64>().ok()).unwrap_or(SIDE_EPS);
    // beta 2 (as in Barill et al. 2018): about half the time of beta 3 on
    // delta, with the same labels in the bench.
    let beta = std::env::var("MESHFIX_FWN_BETA").ok().and_then(|x| x.parse::<f64>().ok()).unwrap_or(ARRANGE_FWN_BETA);
    let fwn = Fwn::with_beta(&jm, &all, beta);
    let src_unit: Vec<Option<Vec3>> = (0..nf)
        .map(|f| {
            let [a, b, c] = jm.tri_pos(f);
            let n = cross(sub(b, a), sub(c, a));
            let l = norm(n);
            (l > 0.0).then(|| scale(n, 1.0 / l))
        })
        .collect();
    let wside: Vec<(f64, f64)> = patches
        .par_iter()
        .map(|list| {
            let mut pick: Vec<u32> = if list.len() <= SAMPLES {
                list.clone()
            } else {
                let mut by_area = list.clone();
                by_area.sort_by(|&x, &y| area[y as usize].total_cmp(&area[x as usize]).then(x.cmp(&y)));
                let mut p: Vec<u32> = by_area[..SAMPLES / 2].to_vec();
                let step = list.len() / (SAMPLES / 2);
                p.extend(list.iter().step_by(step.max(1)).take(SAMPLES / 2));
                p
            };
            pick.sort_unstable();
            pick.dedup();
            let centroid = |f: u32| {
                let t = tris[f as usize];
                scale(add(add(v[t[0] as usize], v[t[1] as usize]), v[t[2] as usize]), 1.0 / 3.0)
            };
            let mean = |vals: &[(f64, f64, f64)]| {
                let (mut sf, mut sb, mut sa) = (0.0, 0.0, 0.0);
                for &(wf, wb, a) in vals {
                    sf += wf * a;
                    sb += wb * a;
                    sa += a;
                }
                if sa > 0.0 {
                    (sf / sa, sb / sa)
                } else {
                    let k = vals.len().max(1) as f64;
                    (vals.iter().map(|x| x.0).sum::<f64>() / k, vals.iter().map(|x| x.1).sum::<f64>() / k)
                }
            };
            // At the centroid, without the own source face.
            let at_c: Vec<(f64, f64, f64)> = pick
                .iter()
                .map(|&f| {
                    let w = fwn.eval(centroid(f), Some(jm.tri_pos(src[f as usize] as usize)));
                    (w - 0.5, w + 0.5, area[f as usize])
                })
                .collect();
            let (wf, wb) = mean(&at_c);
            // A patch that both sides put clearly in or clearly out goes in
            // any case. The others get the side samples (fewer, the largest
            // pieces first: these evaluations near the surface are slow).
            let clear = (wf > 0.75 && wb > 0.75) || (wf < 0.25 && wb < 0.25);
            if side_eps <= 0.0 || clear {
                return (wf, wb);
            }
            let mut by_area = pick.clone();
            by_area.sort_by(|&x, &y| area[y as usize].total_cmp(&area[x as usize]).then(x.cmp(&y)));
            let at_eps: Vec<(f64, f64, f64)> = by_area
                .iter()
                .take(EPS_SAMPLES)
                .filter_map(|&f| {
                    let n = src_unit[src[f as usize] as usize]?;
                    let c = centroid(f);
                    Some((
                        fwn.eval(add(c, scale(n, side_eps)), None),
                        fwn.eval(sub(c, scale(n, side_eps)), None),
                        area[f as usize],
                    ))
                })
                .collect();
            if at_eps.is_empty() {
                (wf, wb)
            } else {
                mean(&at_eps)
            }
        })
        .collect();
    let wpatch: Vec<f64> = wside.iter().map(|&(f, b)| 0.5 * (f + b)).collect();
    dbg_time("arrange winding", t0);

    // 7. Labels by a minimum cut. Node 2p = front of patch p, 2p+1 = back.
    // Data: the area of a side times its distance from 0.5 (winding number,
    // capped at 1) for the wrong label. Smoothness: LAMBDA times the edge
    // length for two sides in one wedge with different labels (that gives
    // an open seam). A gap in the arrangement (an open edge, a missed cut)
    // joins two cells by only a short edge, so the cut can separate them.
    let lambda = std::env::var("MESHFIX_LAMBDA").ok().and_then(|s| s.parse::<f64>().ok()).unwrap_or(LAMBDA);
    let npatch = patches.len();
    let (s_node, t_node) = (2 * npatch as u32, 2 * npatch as u32 + 1);
    let mut flow = Flow::new(2 * npatch + 2);
    for p in 0..npatch {
        let a: f64 = patches[p].iter().map(|&f| area[f as usize]).sum();
        for (k, w) in [(0u32, wside[p].0), (1u32, wside[p].1)] {
            let node = 2 * p as u32 + k;
            let c_in = a * (0.5 - w).clamp(0.0, 1.0);
            let c_out = a * (w - 0.5).clamp(0.0, 1.0);
            // source = inside: s -> node is cut when the node is outside.
            if c_out > 0.0 {
                flow.add(s_node, node, c_out, 0.0);
            }
            if c_in > 0.0 {
                flow.add(node, t_node, c_in, 0.0);
            }
        }
    }
    let mut smooth: HashMap<(u32, u32), f64> = HashMap::new();
    for ((a, b), ord) in &edges {
        let k = ord.len();
        let len = dist(v[*a as usize], v[*b as usize]);
        for i in 0..k {
            let (f, g) = (ord[i], ord[(i + 1) % k]);
            let s1 = next_side(f, *a, *b);
            let s2 = next_side(g, *a, *b) ^ 1;
            let n1 = 2 * patch_of[(s1 / 2) as usize] + (s1 & 1);
            let n2 = 2 * patch_of[(s2 / 2) as usize] + (s2 & 1);
            if n1 != n2 {
                *smooth.entry((n1.min(n2), n1.max(n2))).or_default() += lambda * len;
            }
        }
    }
    let mut sm: Vec<((u32, u32), f64)> = smooth.into_iter().collect();
    sm.sort_unstable_by_key(|x| x.0);
    for ((x, y), c) in sm {
        flow.add(x, y, c, c);
    }
    let cut_value = flow.max_flow(s_node, t_node);
    let src_side = flow.source_side(s_node);
    dbg_time("arrange min cut", t0);
    let inside: Vec<bool> = (0..2 * np)
        .map(|s| src_side[(2 * patch_of[s / 2] + (s as u32 & 1)) as usize])
        .collect();
    if std::env::var_os("MESHFIX_TIMING").is_some() {
        let in_area: f64 = (0..npatch).filter(|&p| src_side[2 * p + 1] && !src_side[2 * p]).map(|p| patches[p].iter().map(|&f| area[f as usize]).sum::<f64>()).sum();
        eprintln!("arrange patches {npatch} cut value {cut_value:.4e} kept-patch area {in_area:.4e}");
        // Wedges with two labels: by the number of pieces at the edge and
        // by the rounded winding numbers of the two patches.
        let mut by_k: std::collections::BTreeMap<usize, usize> = Default::default();
        let mut by_w: HashMap<(i64, i64, bool), usize> = HashMap::new();
        let mut wh: std::collections::BTreeMap<i64, f64> = Default::default();
        for p in 0..npatch {
            let a: f64 = patches[p].iter().map(|&f| area[f as usize]).sum();
            *wh.entry((wpatch[p] * 4.0).round() as i64).or_default() += a;
        }
        eprintln!("patch area by winding (x4): {wh:?}");
        for ((a, b), ord) in &edges {
            let k = ord.len();
            for i in 0..k {
                let (f, g) = (ord[i], ord[(i + 1) % k]);
                let s1 = next_side(f, *a, *b);
                let s2 = next_side(g, *a, *b) ^ 1;
                if inside[s1 as usize] != inside[s2 as usize] {
                    *by_k.entry(k).or_default() += 1;
                    let (p1, p2) = (patch_of[f as usize] as usize, patch_of[g as usize] as usize);
                    let w1 = (wpatch[p1] * 4.0).round() as i64;
                    let w2 = (wpatch[p2] * 4.0).round() as i64;
                    let h = hard.contains(&(*a, *b));
                    *by_w.entry((w1.min(w2), w1.max(w2), h)).or_default() += 1;
                }
            }
        }
        let mut shown = 0;
        for ((a, b), ord) in &edges {
            if shown >= 6 || ord.len() != 4 {
                continue;
            }
            let k = ord.len();
            let seam = (0..k).any(|i| {
                let (f, g) = (ord[i], ord[(i + 1) % k]);
                inside[next_side(f, *a, *b) as usize] != inside[(next_side(g, *a, *b) ^ 1) as usize]
            });
            if !seam {
                continue;
            }
            shown += 1;
            eprintln!("seam edge {a}-{b} at {:?} len {:.3e}", v[*a as usize], dist(v[*a as usize], v[*b as usize]));
            let (pa, pb) = (v[*a as usize], v[*b as usize]);
            let d = sub(pb, pa);
            for &f in ord {
                let t = tris[f as usize];
                let c = scale(add(add(v[t[0] as usize], v[t[1] as usize]), v[t[2] as usize]), 1.0 / 3.0);
                let w = fwn.eval(c, Some(jm.tri_pos(src[f as usize] as usize)));
                let o = t.iter().copied().find(|&x| x != *a && x != *b).unwrap();
                let pw = sub(v[o as usize], pa);
                let perp = sub(pw, scale(d, dot(pw, d) / dot(d, d)));
                eprintln!(
                    "  piece {f} src {} patch {} (n {}) wp {:.3} w {:.3} dir {} area {:.2e} perp {:?} in front {} back {}",
                    src[f as usize], patch_of[f as usize], patches[patch_of[f as usize] as usize].len(),
                    wpatch[patch_of[f as usize] as usize], w, directed(&t, *a, *b), area[f as usize],
                    perp, inside[2 * f as usize], inside[2 * f as usize + 1]
                );
            }
        }
        let mut bw: Vec<_> = by_w.into_iter().collect();
        bw.sort_by(|x, y| y.1.cmp(&x.1));
        eprintln!("seam wedges by pieces at edge: {by_k:?}");
        eprintln!("seam wedges by (w1*4, w2*4, cut curve): {:?}", &bw[..bw.len().min(20)]);
    }

    // Kept faces: inside behind, outside in front (else turned).
    let keep: Vec<bool> = (0..np).map(|f| inside[2 * f] != inside[2 * f + 1]).collect();
    let flip: Vec<bool> = (0..np).map(|f| inside[2 * f]).collect();
    rep.kept = keep.iter().filter(|&&k| k).count();
    rep.removed = np - rep.kept;
    rep.removed_patches = patches.iter().filter(|l| l.iter().all(|&f| !keep[f as usize])).count();
    if std::env::var_os("MESHFIX_FOLD_DEBUG").is_some() {
        // Kept pieces that fold onto a kept neighbor: own winding numbers
        // against the label of their patch.
        let unit = |f: usize| {
            let t = tris[f];
            let n = cross(sub(v[t[1] as usize], v[t[0] as usize]), sub(v[t[2] as usize], v[t[0] as usize]));
            let l = norm(n);
            if l > 0.0 { scale(n, 1.0 / l) } else { [0.0; 3] }
        };
        let mut folded: Vec<u32> = Vec::new();
        for ((_a, _b), ord) in &edges {
            let ks: Vec<u32> = ord.iter().copied().filter(|&f| keep[f as usize]).collect();
            for i in 0..ks.len() {
                for j in i + 1..ks.len() {
                    let (nf, ng) = (unit(ks[i] as usize), unit(ks[j] as usize));
                    let sf = if flip[ks[i] as usize] { -1.0 } else { 1.0 };
                    let sg = if flip[ks[j] as usize] { -1.0 } else { 1.0 };
                    if sf * sg * dot(nf, ng) < -0.985 {
                        folded.push(ks[i]);
                        folded.push(ks[j]);
                    }
                }
            }
        }
        folded.sort_unstable();
        folded.dedup();
        let mut cat: std::collections::BTreeMap<(String, usize), usize> = Default::default();
        for &f in &folded {
            let t = tris[f as usize];
            let c = scale(add(add(v[t[0] as usize], v[t[1] as usize]), v[t[2] as usize]), 1.0 / 3.0);
            let n = src_unit[src[f as usize] as usize].unwrap_or([0.0; 3]);
            let wf = fwn.eval(add(c, scale(n, side_eps)), None);
            let wb = fwn.eval(sub(c, scale(n, side_eps)), None);
            let own = if (wf > 0.5) == (wb > 0.5) { "own:remove" } else { "own:keep" };
            let p = patch_of[f as usize] as usize;
            let sz = patches[p].len();
            let b = if sz == 1 { 1 } else if sz <= 8 { 8 } else if sz <= 100 { 100 } else if sz <= 10000 { 10000 } else { 1000000 };
            *cat.entry((own.to_string(), b)).or_default() += 1;
        }
        eprintln!("fold debug: {} folded kept pieces; (own verdict, patch size bin) -> count: {:?}", folded.len(), cat);
    }

    // 8. Pairs at each edge by the angle order, then vertex copies for each
    // manifold sheet.
    let mut cp: Vec<u32> = (0..3 * np as u32).collect();
    let corner = |f: u32, x: u32| 3 * f + tris[f as usize].iter().position(|&y| y == x).unwrap() as u32;
    for ((a, b), ord) in &edges {
        let outs: Vec<u32> = ord.iter().copied().filter(|&f| keep[f as usize]).collect();
        let m = outs.len();
        if m == 0 {
            continue;
        }
        let mut has = vec![false; m];
        if m >= 2 {
            for i in 0..m {
                let j = (i + 1) % m;
                let (f, g) = (outs[i], outs[j]);
                if inside[next_side(f, *a, *b) as usize] && inside[(next_side(g, *a, *b) ^ 1) as usize] {
                    has[i] = true;
                    has[j] = true;
                    join(&mut cp, corner(f, *a), corner(g, *a));
                    join(&mut cp, corner(f, *b), corner(g, *b));
                }
            }
        }
        rep.unpaired += has.iter().filter(|&&h| !h).count();
    }
    let keep_jitter = std::env::var_os("MESHFIX_KEEP_JITTER").is_some();
    let mut out_v: Vec<Vec3> = Vec::new();
    let mut jit_v: Vec<Vec3> = Vec::new();
    let mut is_orig: Vec<bool> = Vec::new();
    let mut id_of: HashMap<u32, u32> = HashMap::new();
    let mut out_t: Vec<[u32; 3]> = Vec::with_capacity(rep.kept);
    for f in 0..np {
        if !keep[f] {
            continue;
        }
        let t = tris[f];
        let mut nt = [0u32; 3];
        for i in 0..3 {
            let r = find(&mut cp, (3 * f + i) as u32);
            nt[i] = *id_of.entry(r).or_insert_with(|| {
                let x = t[i] as usize;
                out_v.push(if x < nv0 && !keep_jitter { orig[x] } else { v[x] });
                jit_v.push(v[x]);
                is_orig.push(x < nv0);
                (out_v.len() - 1) as u32
            });
        }
        if flip[f] {
            nt.swap(1, 2);
        }
        out_t.push(nt);
    }
    mesh.vertices = out_v;
    mesh.triangles = out_t;
    // The original vertices go back to their positions without the small
    // move. The cut points stay where the cut found them (on the moved
    // faces). Where this makes two faces cross, the vertices of these faces
    // keep the moved position (1e-6 units from the original).
    if !keep_jitter {
        rep.unmoved = restore_crossings(mesh, &jit_v, &is_orig);
    }
    dbg_time("arrange done", t0);
    rep
}

/// After the original positions came back: the vertices of faces that now
/// cross another face go to their moved position again. Only faces with a
/// new vertex (a cut point) are tested at first. Returns the number of
/// vertices that keep the moved position.
fn restore_crossings(mesh: &mut Mesh, jit: &[Vec3], is_orig: &[bool]) -> usize {
    let nf = mesh.triangles.len();
    let mut active: Vec<bool> = mesh.triangles.iter().map(|t| t.iter().any(|&x| !is_orig[x as usize])).collect();
    let mut moved = 0;
    for _ in 0..6 {
        let pairs = crate::alphawrap::crossing_pairs_opt(mesh, Some(&active), false);
        if pairs.is_empty() {
            break;
        }
        let mut changed = vec![false; mesh.vertices.len()];
        let mut any = false;
        for (f, g) in pairs {
            for h in [f, g] {
                for &x in &mesh.triangles[h as usize] {
                    let x = x as usize;
                    if is_orig[x] && mesh.vertices[x] != jit[x] {
                        mesh.vertices[x] = jit[x];
                        changed[x] = true;
                        any = true;
                        moved += 1;
                    }
                }
            }
        }
        if !any {
            break;
        }
        active = (0..nf).map(|f| mesh.triangles[f].iter().any(|&x| changed[x as usize])).collect();
    }
    moved
}

/// Vertices that must share a coordinate value to make it an axis plane.
const PLANE_MIN_VERTICES: u32 = 8;

/// Local id base of the Steiner points of one face cut.
const STEINER: u32 = 0x8000_0000;

/// Faces on edge (a, b), sorted by the angle around the axis a -> b.
fn radial_order(v: &[Vec3], a: u32, b: u32, fs: &[u32], tris: &[[u32; 3]]) -> Vec<u32> {
    let (pa, pb) = (v[a as usize], v[b as usize]);
    let d = sub(pb, pa);
    let dd = dot(d, d);
    let perp = |f: u32| {
        let t = tris[f as usize];
        let c = t.iter().copied().find(|&x| x != a && x != b).unwrap();
        let w = sub(v[c as usize], pa);
        sub(w, scale(d, dot(w, d) / dd))
    };
    let mut u = [0.0; 3];
    for &f in fs {
        let p = perp(f);
        let l = norm(p);
        if l > 0.0 {
            u = scale(p, 1.0 / l);
            break;
        }
    }
    let w = cross(scale(d, 1.0 / dd.sqrt()), u);
    let mut ang: Vec<(f64, u32)> = fs
        .iter()
        .map(|&f| {
            let p = perp(f);
            (dot(p, w).atan2(dot(p, u)), f)
        })
        .collect();
    ang.sort_by(|x, y| x.0.total_cmp(&y.0).then(x.1.cmp(&y.1)));
    ang.into_iter().map(|x| x.1).collect()
}

/// Samples of the winding number for each patch.
const SAMPLES: usize = 8;
/// Distance of a side sample from its piece (normalized units, the largest
/// extent is 5..400). A crack thinner than this between two bodies closes.
/// 0.002 units = 0.007 mm on a model of 1450 mm. The weld tolerance is
/// 1e-4 units, the stitch tolerance 0.01 units.
const SIDE_EPS: f64 = 2e-3;
/// Side samples of a patch that is not clearly in or out.
const EPS_SAMPLES: usize = 4;
/// Accuracy of the winding number tree in the labels (see Fwn::with_beta).
const ARRANGE_FWN_BETA: f64 = 2.0;
/// Default weight of an open seam: cost per unit of edge length, in units
/// of area (normalized model units).
const LAMBDA: f64 = 1.0;

/// Maximum flow (Dinic) with f64 capacities, for the labels.
struct Flow {
    head: Vec<u32>,
    to: Vec<u32>,
    next: Vec<u32>,
    cap: Vec<f64>,
}

const NIL: u32 = u32::MAX;
const FLOW_EPS: f64 = 1e-12;

impl Flow {
    fn new(n: usize) -> Self {
        Flow { head: vec![NIL; n], to: Vec::new(), next: Vec::new(), cap: Vec::new() }
    }

    fn add(&mut self, u: u32, v: u32, c_uv: f64, c_vu: f64) {
        for (a, b, c) in [(u, v, c_uv), (v, u, c_vu)] {
            self.to.push(b);
            self.cap.push(c);
            self.next.push(self.head[a as usize]);
            self.head[a as usize] = (self.to.len() - 1) as u32;
        }
    }

    fn levels(&self, s: u32, t: u32) -> Option<Vec<i32>> {
        let mut level = vec![-1i32; self.head.len()];
        level[s as usize] = 0;
        let mut q = VecDeque::new();
        q.push_back(s);
        while let Some(u) = q.pop_front() {
            let mut e = self.head[u as usize];
            while e != NIL {
                let v = self.to[e as usize];
                if self.cap[e as usize] > FLOW_EPS && level[v as usize] < 0 {
                    level[v as usize] = level[u as usize] + 1;
                    q.push_back(v);
                }
                e = self.next[e as usize];
            }
        }
        (level[t as usize] >= 0).then_some(level)
    }

    fn max_flow(&mut self, s: u32, t: u32) -> f64 {
        let mut total = 0.0;
        while let Some(mut level) = self.levels(s, t) {
            let mut it = self.head.clone();
            let mut path: Vec<u32> = Vec::new();
            let mut u = s;
            loop {
                if u == t {
                    let f = path.iter().map(|&e| self.cap[e as usize]).fold(f64::INFINITY, f64::min);
                    for &e in &path {
                        self.cap[e as usize] -= f;
                        self.cap[(e ^ 1) as usize] += f;
                    }
                    total += f;
                    let k = path.iter().position(|&e| self.cap[e as usize] <= FLOW_EPS).unwrap_or(0);
                    path.truncate(k);
                    u = if k == 0 { s } else { self.to[path[k - 1] as usize] };
                    continue;
                }
                let mut moved = false;
                while it[u as usize] != NIL {
                    let e = it[u as usize];
                    let v = self.to[e as usize];
                    if self.cap[e as usize] > FLOW_EPS && level[v as usize] == level[u as usize] + 1 {
                        path.push(e);
                        u = v;
                        moved = true;
                        break;
                    }
                    it[u as usize] = self.next[e as usize];
                }
                if !moved {
                    if u == s {
                        break;
                    }
                    level[u as usize] = -1;
                    let e = path.pop().unwrap();
                    u = self.to[(e ^ 1) as usize];
                    it[u as usize] = self.next[it[u as usize] as usize];
                }
            }
        }
        total
    }

    /// Nodes reachable from s in the residual graph.
    fn source_side(&self, s: u32) -> Vec<bool> {
        let mut seen = vec![false; self.head.len()];
        seen[s as usize] = true;
        let mut q = vec![s];
        while let Some(u) = q.pop() {
            let mut e = self.head[u as usize];
            while e != NIL {
                let v = self.to[e as usize];
                if self.cap[e as usize] > FLOW_EPS && !seen[v as usize] {
                    seen[v as usize] = true;
                    q.push(v);
                }
                e = self.next[e as usize];
            }
        }
        seen
    }
}
