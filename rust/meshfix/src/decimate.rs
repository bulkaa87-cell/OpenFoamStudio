//! Quadric edge-collapse decimation of a closed, manifold mesh.
//!
//! The quadric of each vertex comes from the plane of the INPUT triangle
//! that the vertex is on (the volume remesh gives it). Thus a collapse of
//! two vertices on two different input planes goes to the line where the
//! planes meet: the sharp edges and corners of the input come back.
//!
//! A collapse is done only when it keeps the mesh closed and manifold (link
//! condition), turns no face over, and makes no face of zero area.

use crate::math::*;
use std::cmp::Ordering;
use std::collections::BinaryHeap;

/// Symmetric quadric: x·A·x + 2·b·x + c, with the total weight w.
#[derive(Clone, Copy, Default)]
pub(crate) struct Quad {
    a: [f64; 6], // xx xy xz yy yz zz
    b: Vec3,
    c: f64,
    w: f64,
}

impl Quad {
    fn plane(n: Vec3, d: f64, w: f64) -> Self {
        Quad {
            a: [
                w * n[0] * n[0],
                w * n[0] * n[1],
                w * n[0] * n[2],
                w * n[1] * n[1],
                w * n[1] * n[2],
                w * n[2] * n[2],
            ],
            b: scale(n, w * d),
            c: w * d * d,
            w,
        }
    }
    pub(crate) fn add(&self, o: &Quad) -> Quad {
        Quad {
            a: std::array::from_fn(|i| self.a[i] + o.a[i]),
            b: add(self.b, o.b),
            c: self.c + o.c,
            w: self.w + o.w,
        }
    }
    fn eval(&self, x: Vec3) -> f64 {
        let a = &self.a;
        let ax = [
            a[0] * x[0] + a[1] * x[1] + a[2] * x[2],
            a[1] * x[0] + a[3] * x[1] + a[4] * x[2],
            a[2] * x[0] + a[4] * x[1] + a[5] * x[2],
        ];
        dot(x, ax) + 2.0 * dot(self.b, x) + self.c
    }
    /// The minimum, pulled to `m` where A is singular: (A + λI)x = λm - b.
    fn optimum(&self, m: Vec3) -> Option<Vec3> {
        let a = &self.a;
        let tr = a[0] + a[3] + a[5];
        if !(tr > 0.0) {
            return Some(m);
        }
        let l = 1e-7 * tr;
        let r = [l * m[0] - self.b[0], l * m[1] - self.b[1], l * m[2] - self.b[2]];
        let c0 = [a[0] + l, a[1], a[2]];
        let c1 = [a[1], a[3] + l, a[4]];
        let c2 = [a[2], a[4], a[5] + l];
        let det = det3(c0, c1, c2);
        if !(det.abs() > 1e-300) {
            return None;
        }
        Some([det3(r, c1, c2) / det, det3(c0, r, c2) / det, det3(c0, c1, r) / det])
    }
}

#[derive(Clone, Copy)]
struct Cand {
    cost: f64,
    a: u32,
    b: u32,
    va: u32,
    vb: u32,
}
impl PartialEq for Cand {
    fn eq(&self, o: &Self) -> bool {
        self.cost == o.cost
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
        o.cost.total_cmp(&self.cost) // min-heap
    }
}


/// One decimation run on a mesh piece. Locked vertices do not move and are
/// not removed (they are on faces of other pieces).
struct Dec<'a> {
    v: &'a mut Vec<Vec3>,
    f: &'a mut Vec<[u32; 3]>,
    q: &'a mut Vec<Quad>,
    locked: &'a [bool],
    alive: Vec<bool>,
    vf: Vec<Vec<u32>>,
    ver: Vec<u32>,
    removed: Vec<bool>,
    tol2: f64,
    max_move: f64,
}

/// Smallest face quality that a collapse may make (MESHFIX_VOL_QMIN).
fn qmin() -> f64 {
    static Q: std::sync::OnceLock<f64> = std::sync::OnceLock::new();
    *Q.get_or_init(|| std::env::var("MESHFIX_VOL_QMIN").ok().and_then(|v| v.parse().ok()).unwrap_or(0.0))
}

fn quality(p: [Vec3; 3]) -> (Vec3, f64) {
    let n = cross(sub(p[1], p[0]), sub(p[2], p[0]));
    let l = dist2(p[0], p[1]) + dist2(p[1], p[2]) + dist2(p[2], p[0]);
    let q = if l > 0.0 { 2.0 * 3f64.sqrt() * norm(n) / l } else { 0.0 };
    (n, q)
}

impl Dec<'_> {
    fn ring_into(&self, x: u32, r: &mut Vec<u32>) {
        r.clear();
        for &f in &self.vf[x as usize] {
            for &w in &self.f[f as usize] {
                if w != x {
                    r.push(w);
                }
            }
        }
        r.sort_unstable();
        r.dedup();
    }

    /// Position and cost of the collapse a-b, or None.
    fn plan(&self, a: u32, b: u32) -> Option<(Vec3, f64)> {
        if self.locked[a as usize] || self.locked[b as usize] {
            return None;
        }
        let q = self.q[a as usize].add(&self.q[b as usize]);
        let (pa, pb) = (self.v[a as usize], self.v[b as usize]);
        let m = scale(add(pa, pb), 0.5);
        // Sum of the squared distances to all planes of the merged vertices:
        // each input plane counts, so no plane can move more than tol.
        let cost = |x: Vec3| q.eval(x).max(0.0);
        let best = match q.optimum(m) {
            Some(x) if dist(x, m) <= self.max_move => (x, cost(x)),
            _ => [m, pa, pb]
                .into_iter()
                .map(|x| (x, cost(x)))
                .min_by(|a, b| a.1.total_cmp(&b.1))
                .unwrap(),
        };
        Some(best).filter(|b| b.1 <= self.tol2)
    }

    /// Check the collapse a-b to x: link condition and face checks.
    fn valid(&self, a: u32, b: u32, x: Vec3, ra: &mut Vec<u32>, rb: &mut Vec<u32>) -> bool {
        self.ring_into(a, ra);
        self.ring_into(b, rb);
        if ra.len() < 3 || rb.len() < 3 || ra.len() + rb.len() < 8 {
            return false;
        }
        let common = ra.iter().filter(|w| rb.binary_search(w).is_ok()).count();
        if common != 2 {
            return false;
        }
        // The two faces on a-b must be the only shared faces.
        let shared = self.vf[a as usize]
            .iter()
            .filter(|&&f| self.f[f as usize].contains(&b))
            .count();
        if shared != 2 {
            return false;
        }
        for &s in &[a, b] {
            for &f in &self.vf[s as usize] {
                let t = self.f[f as usize];
                if t.contains(&a) && t.contains(&b) {
                    continue;
                }
                let old: [Vec3; 3] = std::array::from_fn(|i| self.v[t[i] as usize]);
                let new: [Vec3; 3] =
                    std::array::from_fn(|i| if t[i] == a || t[i] == b { x } else { old[i] });
                let (n0, q0) = quality(old);
                let (n1, q1) = quality(new);
                let (l0, l1) = (norm(n0), norm(n1));
                if !(l1 > 0.0) || dot(n0, n1) <= 0.2 * l0 * l1 {
                    return false;
                }
                if q1 < qmin() && q1 < q0 {
                    return false;
                }
            }
        }
        true
    }

    fn collapse(&mut self, a: u32, b: u32, x: Vec3) {
        self.v[a as usize] = x;
        let qb = self.q[b as usize];
        self.q[a as usize] = self.q[a as usize].add(&qb);
        let fb = std::mem::take(&mut self.vf[b as usize]);
        for &f in &fb {
            let t = &mut self.f[f as usize];
            if t.contains(&a) {
                self.alive[f as usize] = false;
                for &w in t.iter() {
                    if w != a && w != b {
                        self.vf[w as usize].retain(|&g| g != f);
                    }
                }
            } else {
                for w in t.iter_mut() {
                    if *w == b {
                        *w = a;
                    }
                }
            }
        }
        let alive = &self.alive;
        self.vf[a as usize].retain(|&g| alive[g as usize]);
        for &f in &fb {
            if self.alive[f as usize] {
                self.vf[a as usize].push(f);
            }
        }
        self.removed[b as usize] = true;
        self.ver[a as usize] += 1;
        self.ver[b as usize] += 1;
    }

    fn run(&mut self) -> usize {
        let nf = self.f.len();
        let mut heap: BinaryHeap<Cand> = BinaryHeap::with_capacity(nf * 3 / 2);
        for t in self.f.iter() {
            for i in 0..3 {
                let (a, b) = (t[i], t[(i + 1) % 3]);
                if a < b {
                    if let Some((_, c)) = self.plan(a, b) {
                        heap.push(Cand { cost: c, a, b, va: 0, vb: 0 });
                    }
                }
            }
        }
        let mut done = 0;
        let (mut ra, mut rb) = (Vec::new(), Vec::new());
        while let Some(c) = heap.pop() {
            let (a, b) = (c.a, c.b);
            if self.removed[a as usize]
                || self.removed[b as usize]
                || self.ver[a as usize] != c.va
                || self.ver[b as usize] != c.vb
            {
                continue;
            }
            let Some((x, _)) = self.plan(a, b) else { continue };
            if !self.valid(a, b, x, &mut ra, &mut rb) {
                continue;
            }
            self.collapse(a, b, x);
            done += 1;
            self.ring_into(a, &mut ra);
            for &w in ra.iter() {
                if let Some((_, cost)) = self.plan(a, w) {
                    let (p, r) = if a < w { (a, w) } else { (w, a) };
                    heap.push(Cand {
                        cost,
                        a: p,
                        b: r,
                        va: self.ver[p as usize],
                        vb: self.ver[r as usize],
                    });
                }
            }
        }
        done
    }
}

/// Decimate a piece in place: the faces that are left, renumbered, with the
/// vertex list cut to the used vertices. Returns (old index of each kept
/// vertex, number of collapses).
pub(crate) fn decimate_piece(
    v: &mut Vec<Vec3>,
    f: &mut Vec<[u32; 3]>,
    q: &mut Vec<Quad>,
    locked: &[bool],
    tol2: f64,
    max_move: f64,
) -> (Vec<u32>, usize) {
    let nv = v.len();
    let mut vf: Vec<Vec<u32>> = vec![Vec::new(); nv];
    for (i, t) in f.iter().enumerate() {
        for &w in t {
            vf[w as usize].push(i as u32);
        }
    }
    let nf = f.len();
    let mut dec = Dec {
        v,
        f,
        q,
        locked,
        alive: vec![true; nf],
        vf,
        ver: vec![0; nv],
        removed: vec![false; nv],
        tol2,
        max_move,
    };
    let done = dec.run();
    let alive = std::mem::take(&mut dec.alive);
    drop(dec);
    let mut k = 0;
    f.retain(|_| {
        k += 1;
        alive[k - 1]
    });
    let mut map = vec![u32::MAX; nv];
    let mut old: Vec<u32> = Vec::new();
    for t in f.iter_mut() {
        for w in t.iter_mut() {
            let i = *w as usize;
            if map[i] == u32::MAX {
                map[i] = old.len() as u32;
                old.push(i as u32);
            }
            *w = map[i];
        }
    }
    *v = old.iter().map(|&i| v[i as usize]).collect();
    *q = old.iter().map(|&i| q[i as usize]).collect();
    (old, done)
}

/// One pass over cubic chunks of edge `chunk`, shifted by `shift`, in
/// parallel. The vertices on faces of two chunks are locked.
fn chunk_pass(
    v: &mut Vec<Vec3>,
    f: &mut Vec<[u32; 3]>,
    q: &mut Vec<Quad>,
    lo: Vec3,
    chunk: f64,
    shift: f64,
    tol2: f64,
    max_move: f64,
) -> usize {
    use rayon::prelude::*;
    let key_of = |t: &[u32; 3]| -> u64 {
        let c: Vec3 = std::array::from_fn(|k| {
            (v[t[0] as usize][k] + v[t[1] as usize][k] + v[t[2] as usize][k]) / 3.0
        });
        let i: [u64; 3] =
            std::array::from_fn(|k| (((c[k] - lo[k] + shift) / chunk).floor().max(0.0)) as u64);
        (i[0] << 42) | (i[1] << 21) | i[2]
    };
    let mut order: Vec<(u64, u32)> =
        f.par_iter().enumerate().map(|(i, t)| (key_of(t), i as u32)).collect();
    order.par_sort_unstable();
    // Owner chunk of each vertex; u64::MAX = on two chunks (locked).
    let nv = v.len();
    let mut owner = vec![u64::MAX - 1; nv];
    for &(k, i) in &order {
        for &w in &f[i as usize] {
            let o = &mut owner[w as usize];
            if *o == u64::MAX - 1 {
                *o = k;
            } else if *o != k {
                *o = u64::MAX;
            }
        }
    }
    let mut groups: Vec<(usize, usize)> = Vec::new();
    let mut s = 0;
    for i in 1..=order.len() {
        if i == order.len() || order[i].0 != order[s].0 {
            groups.push((s, i));
            s = i;
        }
    }
    let (vr, fr, qr, or) = (&*v, &*f, &*q, &owner);
    let pieces: Vec<(Vec<Vec3>, Vec<[u32; 3]>, Vec<Quad>, Vec<u32>, usize)> = groups
        .par_iter()
        .map(|&(a, b)| {
            let mut gid: Vec<u32> = Vec::new();
            let mut lf: Vec<[u32; 3]> = Vec::with_capacity(b - a);
            let mut local = std::collections::HashMap::<u32, u32>::new();
            for &(_, i) in &order[a..b] {
                let t = fr[i as usize];
                lf.push(std::array::from_fn(|k| {
                    *local.entry(t[k]).or_insert_with(|| {
                        gid.push(t[k]);
                        gid.len() as u32 - 1
                    })
                }));
            }
            let mut lv: Vec<Vec3> = gid.iter().map(|&g| vr[g as usize]).collect();
            let mut lq: Vec<Quad> = gid.iter().map(|&g| qr[g as usize]).collect();
            let locked: Vec<bool> = gid.iter().map(|&g| or[g as usize] == u64::MAX).collect();
            let (old, done) = decimate_piece(&mut lv, &mut lf, &mut lq, &locked, tol2, max_move);
            // Global index of a locked vertex, u32::MAX for a new one.
            let back: Vec<u32> =
                old.iter().map(|&o| if locked[o as usize] { gid[o as usize] } else { u32::MAX }).collect();
            (lv, lf, lq, back, done)
        })
        .collect();
    // Join: locked vertices keep their index; the others are appended.
    let mut nv2 = v.len();
    let mut total = 0;
    let mut faces: Vec<[u32; 3]> = Vec::with_capacity(f.len());
    let mut new_v: Vec<Vec3> = Vec::new();
    let mut new_q: Vec<Quad> = Vec::new();
    for (lv, lf, lq, back, done) in pieces {
        total += done;
        let map: Vec<u32> = back
            .iter()
            .enumerate()
            .map(|(i, &g)| {
                if g != u32::MAX {
                    g
                } else {
                    new_v.push(lv[i]);
                    new_q.push(lq[i]);
                    nv2 += 1;
                    nv2 as u32 - 1
                }
            })
            .collect();
        for t in lf {
            faces.push([map[t[0] as usize], map[t[1] as usize], map[t[2] as usize]]);
        }
    }
    v.extend(new_v);
    q.extend(new_q);
    *f = faces;
    // Drop the vertices that no face uses.
    let mut map = vec![u32::MAX; v.len()];
    let mut keep: Vec<u32> = Vec::new();
    for t in f.iter_mut() {
        for w in t.iter_mut() {
            let i = *w as usize;
            if map[i] == u32::MAX {
                map[i] = keep.len() as u32;
                keep.push(i as u32);
            }
            *w = map[i];
        }
    }
    *v = keep.iter().map(|&i| v[i as usize]).collect();
    *q = keep.iter().map(|&i| q[i as usize]).collect();
    total
}

/// Vertex quadrics: the plane of the input triangle of each vertex (None:
/// the mean normal of its faces), weight = 1/3 of the area around it plus a
/// small part, so that a vertex with no area has a quadric too.
pub(crate) fn plane_quads(v: &[Vec3], f: &[[u32; 3]], planes: &[Option<Vec3>]) -> Vec<Quad> {
    let nv = v.len();
    let mut area = vec![0.0f64; nv];
    let mut nsum = vec![[0.0f64; 3]; nv];
    for t in f {
        let p: [Vec3; 3] = std::array::from_fn(|i| v[t[i] as usize]);
        let n = cross(sub(p[1], p[0]), sub(p[2], p[0]));
        let ar = 0.5 * norm(n);
        for &w in t {
            area[w as usize] += ar / 3.0;
            nsum[w as usize] = add(nsum[w as usize], n);
        }
    }
    let _ = area;
    // Unit plane of each vertex.
    let pl: Vec<Option<(Vec3, f64)>> = (0..nv)
        .map(|i| {
            let n = planes[i].unwrap_or(nsum[i]);
            let l = norm(n);
            if !(l > 0.0) {
                return None;
            }
            let n = scale(n, 1.0 / l);
            Some((n, -dot(n, v[i])))
        })
        .collect();
    // Each vertex carries its own plane and the planes of its neighbours.
    // Near a sharp edge a vertex thus has both planes: it can only move
    // onto the edge, not along one plane away from it (the triangles that
    // cut the edge do not grow).
    let mut nb: Vec<Vec<u32>> = vec![Vec::new(); nv];
    for t in f {
        for k in 0..3 {
            nb[t[k] as usize].push(t[(k + 1) % 3]);
            nb[t[k] as usize].push(t[(k + 2) % 3]);
        }
    }
    (0..nv)
        .map(|i| {
            let mut ring = std::mem::take(&mut nb[i]);
            ring.sort_unstable();
            ring.dedup();
            ring.push(i as u32);
            let mut q = Quad::default();
            for &j in &ring {
                if let Some((n, d)) = pl[j as usize] {
                    q = q.add(&Quad::plane(n, d, 1.0));
                }
            }
            if q.w == 0.0 {
                q.w = 1.0;
            }
            q
        })
        .collect()
}

/// The decimation passes after the first one: chunks shifted by half a
/// chunk (in parallel), then the whole mesh (the seams).
pub(crate) fn finish(
    v: &mut Vec<Vec3>,
    f: &mut Vec<[u32; 3]>,
    q: &mut Vec<Quad>,
    chunk: f64,
    tol2: f64,
    max_move: f64,
) -> usize {
    let mut lo = [f64::INFINITY; 3];
    for p in v.iter() {
        for k in 0..3 {
            lo[k] = lo[k].min(p[k]);
        }
    }
    let mut total = chunk_pass(v, f, q, lo, chunk, 0.5 * chunk, tol2, max_move);
    let locked = vec![false; v.len()];
    total += decimate_piece(v, f, q, &locked, tol2, max_move).1;
    total
}
