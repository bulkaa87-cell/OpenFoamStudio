//! Incremental 3D Delaunay triangulation (Bowyer-Watson) for the alpha
//! wrap. Exact orient3d and insphere signs come from the `robust` crate.
//!
//! A large tetrahedron of 4 "super" vertices holds all points. Cells with
//! a super vertex are the "infinite" cells. The alpha wrap inserts the 8
//! corners of a box around the input first, so the infinite cells are the
//! cells outside that box.
//!
//! Each cell is positive: orient3d(v0, v1, v2, v3) > 0 (robust sign).
//! Face i is the face opposite to vertex i; n[i] is the cell across it.

use crate::math::*;
use robust::{insphere, orient3d, Coord3D};

pub const NONE: u32 = u32::MAX;
/// Vertex ids 0..4 are the super vertices.
pub const N_SUPER: u32 = 4;

/// Outward vertex order of face i of a positive cell: the normal (right
/// hand) points away from vertex i.
pub const FACE: [[usize; 3]; 4] = [[1, 3, 2], [0, 2, 3], [0, 3, 1], [0, 1, 2]];

#[derive(Clone, Copy, Debug)]
pub struct Cell {
    pub v: [u32; 4],
    pub n: [u32; 4],
    /// Changes each time the slot is used again (for stale queue entries).
    pub stamp: u32,
    pub alive: bool,
    pub outside: bool,
}

impl Cell {
    #[inline]
    pub fn infinite(&self) -> bool {
        self.v.iter().any(|&v| v < N_SUPER)
    }
    #[inline]
    pub fn index_of_neighbor(&self, c: u32) -> usize {
        self.n.iter().position(|&x| x == c).expect("neighbor link")
    }
    #[inline]
    pub fn index_of_vertex(&self, v: u32) -> Option<usize> {
        self.v.iter().position(|&x| x == v)
    }
}

#[inline]
fn c3(p: Vec3) -> Coord3D<f64> {
    Coord3D { x: p[0], y: p[1], z: p[2] }
}

#[inline]
pub fn orient(a: Vec3, b: Vec3, c: Vec3, d: Vec3) -> f64 {
    orient3d(c3(a), c3(b), c3(c), c3(d))
}

/// Circumcenter and squared radius of a tetrahedron (f64, not exact).
pub fn circumsphere(a: Vec3, b: Vec3, c: Vec3, d: Vec3) -> (Vec3, f64) {
    let (b1, c1, d1) = (sub(b, a), sub(c, a), sub(d, a));
    let den = 2.0 * det3(b1, c1, d1);
    if den.abs() < 1e-300 {
        let m = scale(add(add(a, b), add(c, d)), 0.25);
        return (m, f64::MAX);
    }
    let num = add(
        add(scale(cross(c1, d1), dot(b1, b1)), scale(cross(d1, b1), dot(c1, c1))),
        scale(cross(b1, c1), dot(d1, d1)),
    );
    let r = scale(num, 1.0 / den);
    (add(a, r), dot(r, r))
}

/// Circumcenter and squared radius of a triangle in 3D.
pub fn circumcircle(a: Vec3, b: Vec3, c: Vec3) -> (Vec3, f64) {
    let (b1, c1) = (sub(b, a), sub(c, a));
    let n = cross(b1, c1);
    let nn = dot(n, n);
    if nn < 1e-300 {
        let m = scale(add(add(a, b), c), 1.0 / 3.0);
        return (m, f64::MAX);
    }
    let w = sub(scale(c1, dot(b1, b1)), scale(b1, dot(c1, c1)));
    let r = scale(cross(w, n), 0.5 / nn);
    (add(a, r), dot(r, r))
}

pub struct Delaunay {
    pub pts: Vec<Vec3>,
    pub cells: Vec<Cell>,
    free: Vec<u32>,
    rng: u64,
    /// The last cell made; start of the next walk.
    pub last: u32,
    // Work buffers of `insert`.
    mark: Vec<u32>,
    mark_id: u32,
    cavity: Vec<u32>,
    bnd: Vec<(u32, usize, u32, usize)>,
    pub new_cells: Vec<u32>,
    pub killed: Vec<u32>,
}

impl Delaunay {
    /// A triangulation with the super tetrahedron around the box lo..hi.
    pub fn new(lo: Vec3, hi: Vec3) -> Self {
        let c = scale(add(lo, hi), 0.5);
        let r = norm(sub(hi, lo)).max(1e-6) * 1e3;
        let mut s = [
            add(c, [r, r, r]),
            add(c, [r, -r, -r]),
            add(c, [-r, r, -r]),
            add(c, [-r, -r, r]),
        ];
        if orient(s[0], s[1], s[2], s[3]) < 0.0 {
            s.swap(2, 3);
        }
        let mut d = Self {
            pts: s.to_vec(),
            cells: Vec::new(),
            free: Vec::new(),
            rng: 0x9E37_79B9_7F4A_7C15,
            last: 0,
            mark: Vec::new(),
            mark_id: 0,
            cavity: Vec::new(),
            bnd: Vec::new(),
            new_cells: Vec::new(),
            killed: Vec::new(),
        };
        d.cells.push(Cell { v: [0, 1, 2, 3], n: [NONE; 4], stamp: 0, alive: true, outside: true });
        d.mark.push(0);
        d
    }

    #[inline]
    fn rand(&mut self) -> u64 {
        self.rng ^= self.rng << 13;
        self.rng ^= self.rng >> 7;
        self.rng ^= self.rng << 17;
        self.rng
    }

    #[inline]
    pub fn cell_pts(&self, c: u32) -> [Vec3; 4] {
        let v = self.cells[c as usize].v;
        [self.pts[v[0] as usize], self.pts[v[1] as usize], self.pts[v[2] as usize], self.pts[v[3] as usize]]
    }

    pub fn alive_cells(&self) -> usize {
        self.cells.len() - self.free.len()
    }

    /// A cell that holds p (inside or on its boundary). Visibility walk.
    pub fn locate(&mut self, p: Vec3, hint: u32) -> u32 {
        let mut t = if hint != NONE && (hint as usize) < self.cells.len() && self.cells[hint as usize].alive {
            hint
        } else {
            self.last
        };
        if !self.cells[t as usize].alive {
            t = (0..self.cells.len() as u32).find(|&c| self.cells[c as usize].alive).unwrap();
        }
        let mut steps = 0usize;
        'walk: loop {
            steps += 1;
            if steps > 10_000_000 {
                return t;
            }
            let start = (self.rand() & 3) as usize;
            let cell = self.cells[t as usize];
            for k in 0..4 {
                let i = (start + k) & 3;
                let mut q = [
                    self.pts[cell.v[0] as usize],
                    self.pts[cell.v[1] as usize],
                    self.pts[cell.v[2] as usize],
                    self.pts[cell.v[3] as usize],
                ];
                q[i] = p;
                if orient(q[0], q[1], q[2], q[3]) < 0.0 {
                    let nb = cell.n[i];
                    if nb == NONE {
                        return t;
                    }
                    t = nb;
                    continue 'walk;
                }
            }
            return t;
        }
    }

    #[inline]
    fn in_sphere(&self, c: u32, p: Vec3) -> bool {
        let q = self.cell_pts(c);
        insphere(c3(q[0]), c3(q[1]), c3(q[2]), c3(q[3]), c3(p)) > 0.0
    }

    fn alloc(&mut self, cell: Cell) -> u32 {
        if let Some(i) = self.free.pop() {
            let stamp = self.cells[i as usize].stamp.wrapping_add(1);
            self.cells[i as usize] = Cell { stamp, ..cell };
            i
        } else {
            self.cells.push(cell);
            self.mark.push(0);
            (self.cells.len() - 1) as u32
        }
    }

    /// Insert p. None when p is equal to a vertex. Else the new vertex id;
    /// `new_cells` holds the new cells, `killed` the removed ones.
    /// New cells get `outside = infinite()`.
    pub fn insert(&mut self, p: Vec3, hint: u32) -> Option<u32> {
        let t0 = self.locate(p, hint);
        for &v in &self.cells[t0 as usize].v {
            if self.pts[v as usize] == p {
                return None;
            }
        }
        self.mark_id = self.mark_id.wrapping_add(1);
        if self.mark_id == 0 {
            self.mark.iter_mut().for_each(|m| *m = 0);
            self.mark_id = 1;
        }
        let mid = self.mark_id;
        self.cavity.clear();
        self.bnd.clear();
        self.cavity.push(t0);
        self.mark[t0 as usize] = mid;
        let mut k = 0;
        while k < self.cavity.len() {
            let c = self.cavity[k];
            k += 1;
            for i in 0..4 {
                let nb = self.cells[c as usize].n[i];
                if nb != NONE && self.mark[nb as usize] == mid {
                    continue;
                }
                if nb != NONE && self.in_sphere(nb, p) {
                    self.mark[nb as usize] = mid;
                    self.cavity.push(nb);
                } else {
                    let j = if nb == NONE { 0 } else { self.cells[nb as usize].index_of_neighbor(c) };
                    self.bnd.push((c, i, nb, j));
                }
            }
        }
        // A boundary face of the cavity that p does not see strictly would
        // give a flat or negative cell. With exact signs this does not
        // occur; the check stays as a guard.
        loop {
            let mut grow = None;
            for (bi, &(c, i, nb, _)) in self.bnd.iter().enumerate() {
                let mut q = self.cell_pts(c);
                q[i] = p;
                if orient(q[0], q[1], q[2], q[3]) <= 0.0 && nb != NONE {
                    grow = Some(bi);
                    break;
                }
            }
            let Some(bi) = grow else { break };
            let (_, _, nb, _) = self.bnd[bi];
            if self.mark[nb as usize] == mid {
                // Already in the cavity through another face.
                self.bnd.remove(bi);
                continue;
            }
            self.mark[nb as usize] = mid;
            self.cavity.push(nb);
            // Rebuild the boundary from the cavity.
            self.bnd.clear();
            for &c in &self.cavity {
                for i in 0..4 {
                    let nb2 = self.cells[c as usize].n[i];
                    if nb2 != NONE && self.mark[nb2 as usize] == mid {
                        continue;
                    }
                    let j = if nb2 == NONE { 0 } else { self.cells[nb2 as usize].index_of_neighbor(c) };
                    self.bnd.push((c, i, nb2, j));
                }
            }
        }
        let pv = self.pts.len() as u32;
        self.pts.push(p);
        // Save the cavity faces before the slots are used again.
        let faces: Vec<([u32; 4], usize, u32, usize)> =
            self.bnd.iter().map(|&(c, i, nb, j)| (self.cells[c as usize].v, i, nb, j)).collect();
        self.killed.clear();
        for &c in &self.cavity {
            self.cells[c as usize].alive = false;
            self.free.push(c);
            self.killed.push(c);
        }
        self.new_cells.clear();
        // Edge (sorted vertex pair) → (cell, face index) for the links
        // between new cells.
        let mut open: Vec<((u32, u32), u32, usize)> = Vec::with_capacity(faces.len() * 3 / 2);
        for (v, i, nb, j) in faces {
            let mut nv = v;
            nv[i] = pv;
            let mut cell = Cell { v: nv, n: [NONE; 4], stamp: 0, alive: true, outside: false };
            cell.n[i] = nb;
            cell.outside = cell.infinite();
            let id = self.alloc(cell);
            if nb != NONE {
                self.cells[nb as usize].n[j] = id;
            }
            for f in 0..4 {
                if f == i {
                    continue;
                }
                // Face f holds p and the two vertices other than v[f], v[i].
                let mut e = [0u32; 2];
                let mut m = 0;
                for g in 0..4 {
                    if g != f && g != i {
                        e[m] = nv[g];
                        m += 1;
                    }
                }
                let key = if e[0] < e[1] { (e[0], e[1]) } else { (e[1], e[0]) };
                if let Some(pos) = open.iter().position(|x| x.0 == key) {
                    let (_, oc, of) = open.swap_remove(pos);
                    self.cells[id as usize].n[f] = oc;
                    self.cells[oc as usize].n[of] = id;
                } else {
                    open.push((key, id, f));
                }
            }
            self.new_cells.push(id);
        }
        debug_assert!(open.is_empty());
        self.last = self.new_cells[0];
        Some(pv)
    }
}
