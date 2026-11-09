//! AABB tree over the triangles of a mesh. The alpha wrap asks it three
//! questions: the closest point on the input, "is the input nearer than r",
//! and "does a tetrahedron touch the input".

use crate::math::*;

const LEAF: usize = 4;

#[derive(Clone, Copy)]
struct Node {
    lo: Vec3,
    hi: Vec3,
    /// Leaf: first triangle in `order`. Inner: index of the left child
    /// (the right child is `left + 1`).
    first: u32,
    /// Leaf: number of triangles. Inner: 0.
    count: u32,
}

pub struct TriTree {
    tris: Vec<[Vec3; 3]>,
    nodes: Vec<Node>,
}

#[inline]
fn box_dist2(p: Vec3, lo: Vec3, hi: Vec3) -> f64 {
    let mut d = 0.0;
    for k in 0..3 {
        let v = if p[k] < lo[k] {
            lo[k] - p[k]
        } else if p[k] > hi[k] {
            p[k] - hi[k]
        } else {
            0.0
        };
        d += v * v;
    }
    d
}

/// Closest point on triangle abc to p (Ericson, Real-Time Collision
/// Detection, 5.1.5).
pub fn closest_on_tri(p: Vec3, a: Vec3, b: Vec3, c: Vec3) -> Vec3 {
    let ab = sub(b, a);
    let ac = sub(c, a);
    let ap = sub(p, a);
    let d1 = dot(ab, ap);
    let d2 = dot(ac, ap);
    if d1 <= 0.0 && d2 <= 0.0 {
        return a;
    }
    let bp = sub(p, b);
    let d3 = dot(ab, bp);
    let d4 = dot(ac, bp);
    if d3 >= 0.0 && d4 <= d3 {
        return b;
    }
    let vc = d1 * d4 - d3 * d2;
    if vc <= 0.0 && d1 >= 0.0 && d3 <= 0.0 {
        let v = d1 / (d1 - d3);
        return add(a, scale(ab, v));
    }
    let cp = sub(p, c);
    let d5 = dot(ab, cp);
    let d6 = dot(ac, cp);
    if d6 >= 0.0 && d5 <= d6 {
        return c;
    }
    let vb = d5 * d2 - d1 * d6;
    if vb <= 0.0 && d2 >= 0.0 && d6 <= 0.0 {
        let w = d2 / (d2 - d6);
        return add(a, scale(ac, w));
    }
    let va = d3 * d6 - d5 * d4;
    if va <= 0.0 && (d4 - d3) >= 0.0 && (d5 - d6) >= 0.0 {
        let w = (d4 - d3) / ((d4 - d3) + (d5 - d6));
        return add(b, scale(sub(c, b), w));
    }
    let denom = 1.0 / (va + vb + vc);
    let v = vb * denom;
    let w = vc * denom;
    add(a, add(scale(ab, v), scale(ac, w)))
}

/// Separating-axis test: do the solid tetrahedron and the triangle share a
/// point? Touching counts as a hit (conservative for the wrap).
pub fn tet_tri_overlap(t: &[Vec3; 4], f: &[Vec3; 3]) -> bool {
    let te = [
        sub(t[1], t[0]),
        sub(t[2], t[0]),
        sub(t[3], t[0]),
        sub(t[2], t[1]),
        sub(t[3], t[1]),
        sub(t[3], t[2]),
    ];
    let fe = [sub(f[1], f[0]), sub(f[2], f[1]), sub(f[0], f[2])];
    let separated = |ax: Vec3| -> bool {
        let n2 = dot(ax, ax);
        if n2 < 1e-300 {
            return false;
        }
        let (mut a0, mut a1) = (f64::MAX, f64::MIN);
        for p in t {
            let s = dot(*p, ax);
            a0 = a0.min(s);
            a1 = a1.max(s);
        }
        let (mut b0, mut b1) = (f64::MAX, f64::MIN);
        for p in f {
            let s = dot(*p, ax);
            b0 = b0.min(s);
            b1 = b1.max(s);
        }
        a1 < b0 || b1 < a0
    };
    // Face normals of the tetrahedron.
    let faces = [[1usize, 2, 3], [0, 2, 3], [0, 1, 3], [0, 1, 2]];
    for fc in faces {
        let n = cross(sub(t[fc[1]], t[fc[0]]), sub(t[fc[2]], t[fc[0]]));
        if separated(n) {
            return false;
        }
    }
    if separated(cross(fe[0], fe[1])) {
        return false;
    }
    for a in te {
        for b in fe {
            if separated(cross(a, b)) {
                return false;
            }
        }
    }
    true
}

/// The separating-axis test of `tet_tri_overlap` with the planes of the
/// tetrahedron made once, for many triangles.
struct TetTest {
    t: [Vec3; 4],
    /// Outward face normals and their offsets: dot(n, p) > d is outside.
    n: [Vec3; 4],
    d: [f64; 4],
    edges: [Vec3; 6],
}

impl TetTest {
    fn new(t: &[Vec3; 4]) -> Self {
        let faces = [[1usize, 2, 3], [0, 2, 3], [0, 1, 3], [0, 1, 2]];
        let mut n = [[0.0; 3]; 4];
        let mut d = [0.0; 4];
        for (i, f) in faces.iter().enumerate() {
            let mut nn = cross(sub(t[f[1]], t[f[0]]), sub(t[f[2]], t[f[0]]));
            if dot(nn, sub(t[i], t[f[0]])) > 0.0 {
                nn = scale(nn, -1.0);
            }
            n[i] = nn;
            d[i] = dot(nn, t[f[0]]);
        }
        let edges = [
            sub(t[1], t[0]),
            sub(t[2], t[0]),
            sub(t[3], t[0]),
            sub(t[2], t[1]),
            sub(t[3], t[1]),
            sub(t[3], t[2]),
        ];
        Self { t: *t, n, d, edges }
    }

    fn overlaps(&self, f: &[Vec3; 3]) -> bool {
        // Tetrahedron faces: all 3 triangle points outside one face.
        for i in 0..4 {
            let n = self.n[i];
            if dot(n, f[0]) > self.d[i] && dot(n, f[1]) > self.d[i] && dot(n, f[2]) > self.d[i] {
                return false;
            }
        }
        let fe = [sub(f[1], f[0]), sub(f[2], f[1]), sub(f[0], f[2])];
        let sep = |ax: Vec3| -> bool {
            if dot(ax, ax) < 1e-300 {
                return false;
            }
            let (mut a0, mut a1) = (f64::MAX, f64::MIN);
            for p in &self.t {
                let s = dot(*p, ax);
                a0 = a0.min(s);
                a1 = a1.max(s);
            }
            let (mut b0, mut b1) = (f64::MAX, f64::MIN);
            for p in f {
                let s = dot(*p, ax);
                b0 = b0.min(s);
                b1 = b1.max(s);
            }
            a1 < b0 || b1 < a0
        };
        if sep(cross(fe[0], fe[1])) {
            return false;
        }
        for a in self.edges {
            for b in fe {
                if sep(cross(a, b)) {
                    return false;
                }
            }
        }
        true
    }
}

impl TriTree {
    pub fn new(tris: Vec<[Vec3; 3]>) -> Self {
        let n = tris.len();
        let mut order: Vec<u32> = (0..n as u32).collect();
        let cen: Vec<Vec3> = tris
            .iter()
            .map(|t| scale(add(add(t[0], t[1]), t[2]), 1.0 / 3.0))
            .collect();
        let mut nodes = Vec::with_capacity(2 * n / LEAF + 2);
        nodes.push(Node { lo: [0.0; 3], hi: [0.0; 3], first: 0, count: 0 });
        // Iterative build: (node, start, end).
        let mut stack = vec![(0usize, 0usize, n)];
        while let Some((ni, s, e)) = stack.pop() {
            let mut lo = [f64::MAX; 3];
            let mut hi = [f64::MIN; 3];
            for &i in &order[s..e] {
                for p in &tris[i as usize] {
                    for k in 0..3 {
                        lo[k] = lo[k].min(p[k]);
                        hi[k] = hi[k].max(p[k]);
                    }
                }
            }
            nodes[ni].lo = lo;
            nodes[ni].hi = hi;
            if e - s <= LEAF {
                nodes[ni].first = s as u32;
                nodes[ni].count = (e - s) as u32;
                continue;
            }
            let mut clo = [f64::MAX; 3];
            let mut chi = [f64::MIN; 3];
            for &i in &order[s..e] {
                let c = cen[i as usize];
                for k in 0..3 {
                    clo[k] = clo[k].min(c[k]);
                    chi[k] = chi[k].max(c[k]);
                }
            }
            let ax = (0..3)
                .max_by(|&a, &b| (chi[a] - clo[a]).partial_cmp(&(chi[b] - clo[b])).unwrap())
                .unwrap();
            let mid = (s + e) / 2;
            order[s..e].select_nth_unstable_by(mid - s, |&a, &b| {
                cen[a as usize][ax].partial_cmp(&cen[b as usize][ax]).unwrap()
            });
            let left = nodes.len();
            nodes.push(Node { lo: [0.0; 3], hi: [0.0; 3], first: 0, count: 0 });
            nodes.push(Node { lo: [0.0; 3], hi: [0.0; 3], first: 0, count: 0 });
            nodes[ni].first = left as u32;
            nodes[ni].count = 0;
            stack.push((left, s, mid));
            stack.push((left + 1, mid, e));
        }
        let tris = order.iter().map(|&i| tris[i as usize]).collect();
        Self { tris, nodes }
    }

    pub fn from_mesh(m: &crate::Mesh) -> Self {
        Self::new((0..m.triangles.len()).map(|f| m.tri_pos(f)).collect())
    }

    pub fn is_empty(&self) -> bool {
        self.tris.is_empty()
    }

    /// Closest point on the input and the squared distance. Only points
    /// nearer than sqrt(`limit2`) are looked for; else (p, f64::MAX).
    pub fn closest_within(&self, p: Vec3, limit2: f64) -> (Vec3, f64) {
        let mut best = (p, f64::MAX);
        let mut best2 = limit2;
        if self.tris.is_empty() {
            return best;
        }
        let mut stack: Vec<u32> = Vec::with_capacity(64);
        stack.push(0);
        while let Some(ni) = stack.pop() {
            let nd = &self.nodes[ni as usize];
            if box_dist2(p, nd.lo, nd.hi) >= best2 {
                continue;
            }
            if nd.count > 0 {
                for t in &self.tris[nd.first as usize..(nd.first + nd.count) as usize] {
                    let q = closest_on_tri(p, t[0], t[1], t[2]);
                    let d2 = dist2(p, q);
                    if d2 < best2 {
                        best2 = d2;
                        best = (q, d2);
                    }
                }
            } else {
                let l = nd.first;
                let (a, b) = (&self.nodes[l as usize], &self.nodes[l as usize + 1]);
                let (da, db) = (box_dist2(p, a.lo, a.hi), box_dist2(p, b.lo, b.hi));
                // Nearer child last, so that it is popped first.
                if da < db {
                    stack.push(l + 1);
                    stack.push(l);
                } else {
                    stack.push(l);
                    stack.push(l + 1);
                }
            }
        }
        best
    }

    pub fn closest(&self, p: Vec3) -> (Vec3, f64) {
        self.closest_within(p, f64::MAX)
    }

    /// Is a point of the input at distance <= r from p?
    pub fn within(&self, p: Vec3, r: f64) -> bool {
        let r2 = r * r;
        let mut stack: Vec<u32> = Vec::with_capacity(64);
        if self.tris.is_empty() {
            return false;
        }
        stack.push(0);
        while let Some(ni) = stack.pop() {
            let nd = &self.nodes[ni as usize];
            if box_dist2(p, nd.lo, nd.hi) > r2 {
                continue;
            }
            if nd.count > 0 {
                for t in &self.tris[nd.first as usize..(nd.first + nd.count) as usize] {
                    if dist2(p, closest_on_tri(p, t[0], t[1], t[2])) <= r2 {
                        return true;
                    }
                }
            } else {
                stack.push(nd.first);
                stack.push(nd.first + 1);
            }
        }
        false
    }

    /// Does the solid tetrahedron touch a triangle of the input?
    /// `ball` = (center, squared radius) of a ball that holds the
    /// tetrahedron (for example its circumsphere): it removes most
    /// candidates before the exact test.
    pub fn hits_tet(&self, t: &[Vec3; 4], ball: Option<(Vec3, f64)>) -> bool {
        if self.tris.is_empty() {
            return false;
        }
        // Quick yes: the inscribed ball touches the input.
        let fa = |a: usize, b: usize, c: usize| norm(cross(sub(t[b], t[a]), sub(t[c], t[a])));
        let (a0, a1, a2, a3) = (fa(1, 2, 3), fa(0, 2, 3), fa(0, 1, 3), fa(0, 1, 2));
        let sum = a0 + a1 + a2 + a3;
        if sum > 0.0 {
            let vol6 = det3(sub(t[1], t[0]), sub(t[2], t[0]), sub(t[3], t[0])).abs();
            let r_in = vol6 / sum;
            let inc = scale(
                add(add(scale(t[0], a0), scale(t[1], a1)), add(scale(t[2], a2), scale(t[3], a3))),
                1.0 / sum,
            );
            if r_in > 0.0 && self.within(inc, r_in) {
                return true;
            }
        }
        let tt = TetTest::new(t);
        let (bc, br2) = ball.unwrap_or(([0.0; 3], f64::MAX));
        let mut lo = t[0];
        let mut hi = t[0];
        for p in &t[1..] {
            for k in 0..3 {
                lo[k] = lo[k].min(p[k]);
                hi[k] = hi[k].max(p[k]);
            }
        }
        let mut stack: Vec<u32> = Vec::with_capacity(64);
        stack.push(0);
        while let Some(ni) = stack.pop() {
            let nd = &self.nodes[ni as usize];
            if (0..3).any(|k| nd.hi[k] < lo[k] || nd.lo[k] > hi[k]) {
                continue;
            }
            if br2 != f64::MAX && box_dist2(bc, nd.lo, nd.hi) > br2 {
                continue;
            }
            if nd.count > 0 {
                for f in &self.tris[nd.first as usize..(nd.first + nd.count) as usize] {
                    if (0..3).any(|k| {
                        f[0][k].max(f[1][k]).max(f[2][k]) < lo[k] || f[0][k].min(f[1][k]).min(f[2][k]) > hi[k]
                    }) {
                        continue;
                    }
                    if br2 != f64::MAX && dist2(bc, closest_on_tri(bc, f[0], f[1], f[2])) > br2 {
                        continue;
                    }
                    if tt.overlaps(f) {
                        return true;
                    }
                }
            } else {
                stack.push(nd.first);
                stack.push(nd.first + 1);
            }
        }
        false
    }

    /// The first point on the segment a→b at distance <= `off` from the
    /// input (sphere tracing). Some(a) when a itself is that near.
    pub fn first_hit(&self, a: Vec3, b: Vec3, off: f64) -> Option<Vec3> {
        let dir = sub(b, a);
        let len = norm(dir);
        let at = |t: f64| add(a, scale(dir, t / len.max(1e-300)));
        let tol = off * 1e-3;
        // A step is at least off/4: the offset region is 2·off thick, thus
        // such a step cannot jump over it. When a step goes in, a bisection
        // finds the boundary.
        let min_step = 0.25 * off;
        let mut t = 0.0;
        let mut prev: Option<f64> = None;
        for _ in 0..100_000 {
            let x = at(t);
            // Only the distance up to the segment end matters.
            let lim = len - t + off + tol;
            let (_, d2) = self.closest_within(x, lim * lim);
            if d2 == f64::MAX {
                return None;
            }
            let d = d2.sqrt();
            if d <= off + tol {
                let Some(mut lo) = prev else { return Some(x) };
                let mut hi = t;
                for _ in 0..30 {
                    let mid = 0.5 * (lo + hi);
                    let (_, dm2) = self.closest_within(at(mid), (off + tol) * (off + tol));
                    if dm2 <= (off + tol) * (off + tol) {
                        hi = mid;
                    } else {
                        lo = mid;
                    }
                    if hi - lo < tol {
                        break;
                    }
                }
                return Some(at(hi));
            }
            prev = Some(t);
            t += (d - off).max(min_step);
            if t > len {
                return None;
            }
        }
        None
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    /// The fast test gives the same answer as the plain SAT test.
    #[test]
    fn tet_test_same_as_sat() {
        let mut s: u64 = 99;
        let mut rnd = || {
            s ^= s << 13;
            s ^= s >> 7;
            s ^= s << 17;
            (s % 10_000) as f64 / 1000.0
        };
        let mut hits = 0;
        for _ in 0..20_000 {
            let t = [[rnd(), rnd(), rnd()], [rnd(), rnd(), rnd()], [rnd(), rnd(), rnd()], [rnd(), rnd(), rnd()]];
            let f = [[rnd(), rnd(), rnd()], [rnd(), rnd(), rnd()], [rnd(), rnd(), rnd()]];
            let a = tet_tri_overlap(&t, &f);
            assert_eq!(a, TetTest::new(&t).overlaps(&f));
            hits += a as usize;
        }
        assert!(hits > 1000 && hits < 19_000, "{hits}");
    }

    /// Closest point and first hit on one triangle.
    #[test]
    fn closest_and_first_hit() {
        let tree = TriTree::new(vec![[[0.0, 0.0, 0.0], [10.0, 0.0, 0.0], [0.0, 10.0, 0.0]]]);
        let (p, d2) = tree.closest([2.0, 2.0, 5.0]);
        assert!(dist2(p, [2.0, 2.0, 0.0]) < 1e-20 && (d2 - 25.0).abs() < 1e-9);
        let h = tree.first_hit([2.0, 2.0, 5.0], [2.0, 2.0, -5.0], 0.5).unwrap();
        assert!((h[2] - 0.5).abs() < 1e-3, "{h:?}");
        assert!(tree.first_hit([2.0, 2.0, 5.0], [20.0, 20.0, 5.0], 0.5).is_none());
        assert!(tree.within([1.0, 1.0, 0.4], 0.5) && !tree.within([1.0, 1.0, 0.6], 0.5));
    }
}
