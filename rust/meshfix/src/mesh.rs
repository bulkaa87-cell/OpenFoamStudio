//! Indexed triangle mesh and the topology queries that the steps use.

use crate::math::*;
use std::collections::{HashMap, HashSet};

pub type EdgeKey = (u32, u32);
/// Undirected edge → faces that use it.
pub type EdgeFaces = HashMap<EdgeKey, Vec<u32>>;

#[inline]
pub fn ekey(a: u32, b: u32) -> EdgeKey {
    if a < b {
        (a, b)
    } else {
        (b, a)
    }
}

/// True when the triangle has the directed edge a→b.
#[inline]
pub fn directed(t: &[u32; 3], a: u32, b: u32) -> bool {
    (0..3).any(|i| t[i] == a && t[(i + 1) % 3] == b)
}

/// Turn the triangle (orientation stays) so that t[0] == a.
#[inline]
pub fn rotate_to(t: [u32; 3], a: u32) -> [u32; 3] {
    if t[0] == a {
        t
    } else if t[1] == a {
        [t[1], t[2], t[0]]
    } else {
        [t[2], t[0], t[1]]
    }
}

/// Bits of a coordinate, with -0.0 equal to 0.0.
#[inline]
fn fbits(x: f64) -> u64 {
    if x == 0.0 {
        0
    } else {
        x.to_bits()
    }
}

/// Uniform hash grid for "points near p".
pub struct Grid {
    inv: f64,
    cells: HashMap<(i64, i64, i64), Vec<u32>>,
}

impl Grid {
    pub fn new(cell: f64) -> Self {
        Self { inv: 1.0 / cell.max(1e-12), cells: HashMap::new() }
    }
    #[inline]
    fn key(&self, p: Vec3) -> (i64, i64, i64) {
        (
            (p[0] * self.inv).floor() as i64,
            (p[1] * self.inv).floor() as i64,
            (p[2] * self.inv).floor() as i64,
        )
    }
    pub fn insert(&mut self, id: u32, p: Vec3) {
        let k = self.key(p);
        self.cells.entry(k).or_default().push(id);
    }
    /// Remove id from the cell of p (p: the position it was inserted with).
    pub fn remove(&mut self, id: u32, p: Vec3) {
        let k = self.key(p);
        if let Some(v) = self.cells.get_mut(&k) {
            v.retain(|&x| x != id);
        }
    }
    /// Ids in the 27 cells around p. The caller measures the distance.
    pub fn near(&self, p: Vec3, out: &mut Vec<u32>) {
        let (x, y, z) = self.key(p);
        for dx in -1..=1 {
            for dy in -1..=1 {
                for dz in -1..=1 {
                    if let Some(v) = self.cells.get(&(x + dx, y + dy, z + dz)) {
                        out.extend_from_slice(v);
                    }
                }
            }
        }
    }
}

#[derive(Clone, Debug, Default, PartialEq)]
pub struct Mesh {
    pub vertices: Vec<Vec3>,
    pub triangles: Vec<[u32; 3]>,
}

impl Mesh {
    pub fn new(vertices: Vec<Vec3>, triangles: Vec<[u32; 3]>) -> Self {
        Self { vertices, triangles }
    }

    /// Write the mesh as an OBJ file (for diagnosis).
    pub fn write_obj(&self, path: &std::path::Path) -> std::io::Result<()> {
        use std::io::Write;
        let mut w = std::io::BufWriter::new(std::fs::File::create(path)?);
        for p in &self.vertices {
            writeln!(w, "v {:.17e} {:.17e} {:.17e}", p[0], p[1], p[2])?;
        }
        for t in &self.triangles {
            writeln!(w, "f {} {} {}", t[0] + 1, t[1] + 1, t[2] + 1)?;
        }
        Ok(())
    }

    pub fn bbox(&self) -> Option<(Vec3, Vec3)> {
        let first = *self.vertices.first()?;
        let (mut lo, mut hi) = (first, first);
        for p in &self.vertices {
            for k in 0..3 {
                lo[k] = lo[k].min(p[k]);
                hi[k] = hi[k].max(p[k]);
            }
        }
        Some((lo, hi))
    }

    /// p ← p·s + t
    pub fn affine(&mut self, s: f64, t: Vec3) {
        for p in &mut self.vertices {
            *p = add(scale(*p, s), t);
        }
    }

    #[inline]
    pub fn tri_pos(&self, f: usize) -> [Vec3; 3] {
        let t = self.triangles[f];
        [self.vertices[t[0] as usize], self.vertices[t[1] as usize], self.vertices[t[2] as usize]]
    }

    pub fn edge_faces(&self) -> EdgeFaces {
        let mut m: EdgeFaces = HashMap::with_capacity(self.triangles.len() * 3 / 2 + 1);
        for (f, t) in self.triangles.iter().enumerate() {
            for i in 0..3 {
                m.entry(ekey(t[i], t[(i + 1) % 3])).or_default().push(f as u32);
            }
        }
        m
    }

    /// Remove the triangles that use one vertex two times.
    pub fn drop_collapsed(&mut self) -> usize {
        let n = self.triangles.len();
        self.triangles.retain(|t| t[0] != t[1] && t[1] != t[2] && t[2] != t[0]);
        n - self.triangles.len()
    }

    /// Remove the vertices that no triangle uses, and renumber.
    pub fn compact(&mut self) {
        let mut map = vec![u32::MAX; self.vertices.len()];
        let mut out = Vec::with_capacity(self.vertices.len());
        for t in &mut self.triangles {
            for v in t.iter_mut() {
                let i = *v as usize;
                if map[i] == u32::MAX {
                    map[i] = out.len() as u32;
                    out.push(self.vertices[i]);
                }
                *v = map[i];
            }
        }
        self.vertices = out;
    }

    pub fn retain_faces(&mut self, keep: &[bool]) {
        let mut i = 0;
        self.triangles.retain(|_| {
            let k = keep[i];
            i += 1;
            k
        });
        self.compact();
    }

    pub fn apply_remap(&mut self, remap: &[u32]) {
        for t in &mut self.triangles {
            for v in t.iter_mut() {
                *v = remap[*v as usize];
            }
        }
        self.drop_collapsed();
        self.compact();
    }

    /// Merge the vertices that have bit-identical positions.
    pub fn weld_exact(&mut self) -> usize {
        self.weld_exact_where(None)
    }

    /// Merge bit-identical positions, only among the vertices with
    /// `only[v] == true` (all vertices when None).
    pub fn weld_exact_where(&mut self, only: Option<&[bool]>) -> usize {
        let mut seen: HashMap<[u64; 3], u32> = HashMap::with_capacity(self.vertices.len());
        let mut remap = Vec::with_capacity(self.vertices.len());
        let mut merged = 0;
        for (i, p) in self.vertices.iter().enumerate() {
            if only.is_some_and(|m| !m[i]) {
                remap.push(i as u32);
                continue;
            }
            let r = *seen.entry([fbits(p[0]), fbits(p[1]), fbits(p[2])]).or_insert(i as u32);
            merged += (r != i as u32) as usize;
            remap.push(r);
        }
        if merged > 0 {
            self.apply_remap(&remap);
        }
        merged
    }

    /// True for each vertex on an edge with one face (a hole rim). An
    /// unwelded triangle soup has all vertices on a rim.
    pub fn rim_vertices(&self) -> Vec<bool> {
        let mut rim = vec![false; self.vertices.len()];
        for (&(a, b), fs) in &self.edge_faces() {
            if fs.len() == 1 {
                rim[a as usize] = true;
                rim[b as usize] = true;
            }
        }
        rim
    }

    /// Open edges plus non-manifold edges, by sorting (fast).
    pub fn defect_edges(&self) -> usize {
        let mut keys: Vec<u64> = Vec::with_capacity(self.triangles.len() * 3);
        for t in &self.triangles {
            for i in 0..3 {
                let (a, b) = ekey(t[i], t[(i + 1) % 3]);
                keys.push(((a as u64) << 32) | b as u64);
            }
        }
        keys.sort_unstable();
        let mut n = 0;
        let mut i = 0;
        while i < keys.len() {
            let mut j = i + 1;
            while j < keys.len() && keys[j] == keys[i] {
                j += 1;
            }
            if j - i != 2 {
                n += 1;
            }
            i = j;
        }
        n
    }

    /// Merge each rim vertex into the first earlier rim vertex within
    /// `tol`. Inner vertices stay: two bodies that touch at a vertex must
    /// not become one non-manifold vertex.
    pub fn weld_rim(&mut self, tol: f64) -> usize {
        let rim = self.rim_vertices();
        let tol2 = tol * tol;
        let mut grid = Grid::new(tol.max(1e-12));
        let mut remap = Vec::with_capacity(self.vertices.len());
        let mut near = Vec::new();
        let mut merged = 0;
        for (i, &p) in self.vertices.iter().enumerate() {
            if !rim[i] {
                remap.push(i as u32);
                continue;
            }
            near.clear();
            grid.near(p, &mut near);
            match near.iter().copied().find(|&j| dist2(p, self.vertices[j as usize]) <= tol2) {
                Some(j) => {
                    remap.push(j);
                    merged += 1;
                }
                None => {
                    grid.insert(i as u32, p);
                    remap.push(i as u32);
                }
            }
        }
        if merged > 0 {
            self.apply_remap(&remap);
        }
        merged
    }

    /// Merge each vertex into the first earlier vertex within `tol`.
    pub fn weld(&mut self, tol: f64) -> usize {
        if tol <= 0.0 {
            return self.weld_exact();
        }
        let tol2 = tol * tol;
        let mut grid = Grid::new(tol);
        let mut remap = Vec::with_capacity(self.vertices.len());
        let mut near = Vec::new();
        let mut merged = 0;
        for (i, &p) in self.vertices.iter().enumerate() {
            near.clear();
            grid.near(p, &mut near);
            match near.iter().copied().find(|&j| dist2(p, self.vertices[j as usize]) <= tol2) {
                Some(j) => {
                    remap.push(j);
                    merged += 1;
                }
                None => {
                    grid.insert(i as u32, p);
                    remap.push(i as u32);
                }
            }
        }
        if merged > 0 {
            self.apply_remap(&remap);
        }
        merged
    }

    /// Boundary loops. The vertex order follows the half-edges of the faces
    /// next to the hole, so a fill face must use the reverse order.
    pub fn boundary_loops(&self) -> Vec<Vec<u32>> {
        let ef = self.edge_faces();
        let mut out: HashMap<u32, Vec<u32>> = HashMap::new();
        let mut count = 0usize;
        for (&(u, v), fs) in &ef {
            if fs.len() != 1 {
                continue;
            }
            let (a, b) = if directed(&self.triangles[fs[0] as usize], u, v) { (u, v) } else { (v, u) };
            out.entry(a).or_default().push(b);
            count += 1;
        }
        // HashMap order is random: sort, so that the result is the same each run.
        for l in out.values_mut() {
            l.sort_unstable_by(|x, y| y.cmp(x));
        }
        let mut starts: Vec<u32> = out.keys().copied().collect();
        starts.sort_unstable();
        let mut loops = Vec::new();
        for s in starts {
            while let Some(first) = out.get_mut(&s).and_then(|l| l.pop()) {
                let (mut lp, mut cur, mut ok) = (vec![s], first, true);
                while cur != s {
                    if lp.len() > count {
                        ok = false;
                        break;
                    }
                    lp.push(cur);
                    match out.get_mut(&cur).and_then(|l| l.pop()) {
                        Some(n) => cur = n,
                        None => {
                            ok = false;
                            break;
                        }
                    }
                }
                if ok && lp.len() >= 3 {
                    loops.push(lp);
                }
            }
        }
        loops
    }

    /// Split each boundary edge at the boundary vertices that lie within
    /// `tol` of its inner part (T-junctions). The next stitch can then
    /// merge the vertex pairs.
    pub fn split_boundary_tjunctions(&mut self, tol: f64) -> usize {
        let mut total = 0;
        for _ in 0..8 {
            let ef = self.edge_faces();
            let mut bedges = Vec::new();
            let mut is_b = vec![false; self.vertices.len()];
            let mut len_sum = 0.0;
            for (&(u, v), fs) in &ef {
                if fs.len() != 1 {
                    continue;
                }
                let f = fs[0];
                let (a, b) = if directed(&self.triangles[f as usize], u, v) { (u, v) } else { (v, u) };
                bedges.push((a, b, f));
                is_b[u as usize] = true;
                is_b[v as usize] = true;
                len_sum += dist(self.vertices[u as usize], self.vertices[v as usize]);
            }
            if bedges.is_empty() {
                break;
            }
            bedges.sort_unstable();
            let cell = (len_sum / bedges.len() as f64).max(tol).max(1e-6);
            let mut grid = Grid::new(cell);
            for (i, &b) in is_b.iter().enumerate() {
                if b {
                    grid.insert(i as u32, self.vertices[i]);
                }
            }

            let nf = self.triangles.len();
            let (mut used, mut keep) = (vec![false; nf], vec![true; nf]);
            let (mut added, mut cand, mut seen) = (Vec::new(), Vec::new(), HashSet::new());
            let mut splits = 0;
            for &(a, b, f) in &bedges {
                let fi = f as usize;
                if used[fi] {
                    continue;
                }
                let tri = rotate_to(self.triangles[fi], a);
                let c = tri[2];
                let (pa, pb) = (self.vertices[a as usize], self.vertices[b as usize]);
                let d = sub(pb, pa);
                let l2 = dot(d, d);
                if l2 <= 0.0 {
                    continue;
                }
                let steps = ((l2.sqrt() / cell).ceil() as usize).clamp(1, 1 << 16);
                cand.clear();
                seen.clear();
                for s in 0..=steps {
                    grid.near(add(pa, scale(d, s as f64 / steps as f64)), &mut cand);
                }
                let mut hits: Vec<(f64, u32)> = Vec::new();
                for &v in &cand {
                    if v == a || v == b || v == c || !seen.insert(v) {
                        continue;
                    }
                    let pv = self.vertices[v as usize];
                    let t = dot(sub(pv, pa), d) / l2;
                    if t <= 1e-4 || t >= 1.0 - 1e-4 {
                        continue;
                    }
                    if dist(pv, add(pa, scale(d, t))) < tol {
                        hits.push((t, v));
                    }
                }
                if hits.is_empty() {
                    continue;
                }
                hits.sort_by(|x, y| x.0.total_cmp(&y.0));
                let mut prev = a;
                for &(_, v) in &hits {
                    added.push([prev, v, c]);
                    prev = v;
                }
                added.push([prev, b, c]);
                keep[fi] = false;
                used[fi] = true;
                splits += hits.len();
            }
            if splits == 0 {
                break;
            }
            let mut i = 0;
            self.triangles.retain(|_| {
                let k = keep[i];
                i += 1;
                k
            });
            self.triangles.extend(added);
            total += splits;
        }
        total
    }
}

/// Edge-connected face groups (shells).
pub fn components(mesh: &Mesh, ef: &EdgeFaces) -> Vec<Vec<u32>> {
    let nf = mesh.triangles.len();
    let (mut seen, mut comps, mut stack) = (vec![false; nf], Vec::new(), Vec::new());
    for s in 0..nf {
        if seen[s] {
            continue;
        }
        seen[s] = true;
        stack.push(s as u32);
        let mut comp = Vec::new();
        while let Some(f) = stack.pop() {
            comp.push(f);
            let t = mesh.triangles[f as usize];
            for i in 0..3 {
                for &g in &ef[&ekey(t[i], t[(i + 1) % 3])] {
                    if !seen[g as usize] {
                        seen[g as usize] = true;
                        stack.push(g);
                    }
                }
            }
        }
        comps.push(comp);
    }
    comps
}

/// True when each edge of the shell has exactly two faces.
pub fn shell_closed(mesh: &Mesh, ef: &EdgeFaces, shell: &[u32]) -> bool {
    shell.iter().all(|&f| {
        let t = mesh.triangles[f as usize];
        (0..3).all(|i| ef[&ekey(t[i], t[(i + 1) % 3])].len() == 2)
    })
}

/// Signed volume: Σ det(A-p0, B-p0, C-p0) / 6, p0 = first vertex of the
/// first face. Positive when the normals point out.
pub fn shell_volume(mesh: &Mesh, shell: &[u32]) -> f64 {
    let Some(&f0) = shell.first() else { return 0.0 };
    let p0 = mesh.vertices[mesh.triangles[f0 as usize][0] as usize];
    shell
        .iter()
        .map(|&f| {
            let [a, b, c] = mesh.tri_pos(f as usize);
            det3(sub(a, p0), sub(b, p0), sub(c, p0))
        })
        .sum::<f64>()
        / 6.0
}
