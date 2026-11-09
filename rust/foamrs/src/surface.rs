//! Lines along a vector field ON a triangle surface ("oil flow").
//!
//! Why not vtkStreamTracer in surface mode: it finds the next cell by the
//! nearest point in space. On a thin wing the nearest cell is often on the
//! other side, so a line jumps from the lower skin to the upper one; at an
//! opening it flies straight through the air. Here a line moves only into
//! the face that shares the edge it crosses, so it cannot leave the surface.
//!
//! The field is linear inside each face (from the vertex vectors), projected
//! onto the face plane; one step is a midpoint (RK2) step that ends at the
//! face edge at the latest. Linear, not constant per face: with a constant
//! field, lines that met at an edge rode the same edges together and the
//! picture became dark bundles.
//!
//! Even spacing (Jobard–Lefer): with `spacing` > 0 the lines are traced one
//! by one, and a line stops when it comes closer than half the spacing to a
//! line already drawn; a seed closer than the spacing is skipped. This is
//! the look of oil flow: evenly spread lines that gather only where the flow
//! really converges.
//!
//! A line stops at:
//! - a boundary edge or an edge with more than two faces;
//! - an edge where both faces push the flow into it (a convergence line);
//! - a face where the field is (near) zero;
//! - the length or step limit, or another line (even spacing).

use std::collections::HashMap;

pub struct Surface {
    pts: Vec<[f64; 3]>,
    tris: Vec<[usize; 3]>,
    /// Face across edge k (edge k is opposite vertex k), or NONE.
    nbr: Vec<[usize; 3]>,
    /// Unit normal of each face (zero for a degenerate face).
    fnorm: Vec<[f64; 3]>,
    /// Inverse Gram matrix of (e1, e2) of each face, for barycentric steps.
    ginv: Vec<[f64; 3]>,
    /// Longest step inside a face: a third of its longest edge.
    hmax: Vec<f64>,
    vec: Vec<[f64; 3]>,
    /// Vertex normals for the display offset.
    vnorm: Vec<[f64; 3]>,
    floor: f64,
}

const NONE: usize = usize::MAX;
const EPS: f64 = 1e-12;

fn sub(a: [f64; 3], b: [f64; 3]) -> [f64; 3] {
    [a[0] - b[0], a[1] - b[1], a[2] - b[2]]
}
fn dot(a: [f64; 3], b: [f64; 3]) -> f64 {
    a[0] * b[0] + a[1] * b[1] + a[2] * b[2]
}
fn cross(a: [f64; 3], b: [f64; 3]) -> [f64; 3] {
    [
        a[1] * b[2] - a[2] * b[1],
        a[2] * b[0] - a[0] * b[2],
        a[0] * b[1] - a[1] * b[0],
    ]
}
fn norm(a: [f64; 3]) -> f64 {
    dot(a, a).sqrt()
}
fn scale(a: [f64; 3], s: f64) -> [f64; 3] {
    [a[0] * s, a[1] * s, a[2] * s]
}

/// Uniform grid of points of the lines already drawn (even spacing).
pub struct Grid {
    cell: f64,
    map: HashMap<(i64, i64, i64), Vec<[f64; 3]>>,
}

impl Grid {
    pub fn new(cell: f64) -> Grid {
        Grid { cell: cell.max(1e-12), map: HashMap::new() }
    }
    fn key(&self, p: [f64; 3]) -> (i64, i64, i64) {
        (
            (p[0] / self.cell).floor() as i64,
            (p[1] / self.cell).floor() as i64,
            (p[2] / self.cell).floor() as i64,
        )
    }
    /// Is any stored point closer than `r` (r ≤ cell)?
    pub fn near(&self, p: [f64; 3], r: f64) -> bool {
        let (i, j, k) = self.key(p);
        let r2 = r * r;
        for di in -1..=1 {
            for dj in -1..=1 {
                for dk in -1..=1 {
                    if let Some(v) = self.map.get(&(i + di, j + dj, k + dk)) {
                        if v.iter().any(|q| {
                            let d = sub(*q, p);
                            dot(d, d) < r2
                        }) {
                            return true;
                        }
                    }
                }
            }
        }
        false
    }
    /// Store a line, with extra points so that no gap is longer than r/2.
    pub fn add_line(&mut self, line: &[[f64; 3]], r: f64) {
        let step = (0.5 * r).max(1e-12);
        for w in line.windows(2) {
            let d = sub(w[1], w[0]);
            let n = (norm(d) / step).ceil().max(1.0) as usize;
            for s in 0..n {
                let p = [
                    w[0][0] + d[0] * s as f64 / n as f64,
                    w[0][1] + d[1] * s as f64 / n as f64,
                    w[0][2] + d[2] * s as f64 / n as f64,
                ];
                let k = self.key(p);
                self.map.entry(k).or_default().push(p);
            }
        }
        if let Some(&p) = line.last() {
            let k = self.key(p);
            self.map.entry(k).or_default().push(p);
        }
    }
}

impl Surface {
    /// `vectors` and `normals` are per vertex. `floor` — where the field
    /// magnitude is below it there is no direction (the line stops).
    pub fn new(
        pts: Vec<[f64; 3]>,
        tris: Vec<[usize; 3]>,
        vectors: Vec<[f64; 3]>,
        normals: Vec<[f64; 3]>,
        floor: f64,
    ) -> Surface {
        let nf = tris.len();
        let mut edges: HashMap<(usize, usize), Vec<(usize, usize)>> =
            HashMap::with_capacity(nf * 2);
        for (f, t) in tris.iter().enumerate() {
            for k in 0..3 {
                let a = t[(k + 1) % 3];
                let b = t[(k + 2) % 3];
                let key = if a < b { (a, b) } else { (b, a) };
                edges.entry(key).or_default().push((f, k));
            }
        }
        let mut nbr = vec![[NONE; 3]; nf];
        for faces in edges.values() {
            if faces.len() == 2 {
                let (f, k) = faces[0];
                let (g, j) = faces[1];
                nbr[f][k] = g;
                nbr[g][j] = f;
            }
        }
        let mut fnorm = vec![[0.0; 3]; nf];
        let mut ginv = vec![[0.0; 3]; nf];
        let mut hmax = vec![0.0; nf];
        for (f, t) in tris.iter().enumerate() {
            let (a, b, c) = (pts[t[0]], pts[t[1]], pts[t[2]]);
            let e1 = sub(b, a);
            let e2 = sub(c, a);
            let n = cross(e1, e2);
            let area2 = norm(n);
            let (g11, g12, g22) = (dot(e1, e1), dot(e1, e2), dot(e2, e2));
            let det = g11 * g22 - g12 * g12;
            if area2 <= 0.0 || det <= 1e-300 {
                continue; // degenerate face: no direction, line stops
            }
            ginv[f] = [g22 / det, -g12 / det, g11 / det];
            fnorm[f] = scale(n, 1.0 / area2);
            let longest = norm(e1).max(norm(e2)).max(norm(sub(c, b)));
            hmax[f] = longest / 3.0;
        }
        Surface { pts, tris, nbr, fnorm, ginv, hmax, vec: vectors, vnorm: normals, floor }
    }

    pub fn n_faces(&self) -> usize {
        self.tris.len()
    }

    /// Unit direction of the field at barycentric `l` in face f, or None.
    fn dir_at(&self, f: usize, l: [f64; 3]) -> Option<[f64; 3]> {
        let nu = self.fnorm[f];
        if nu == [0.0; 3] {
            return None;
        }
        let t = self.tris[f];
        let mut v = [0.0; 3];
        for k in 0..3 {
            for d in 0..3 {
                v[d] += l[k] * self.vec[t[k]][d];
            }
        }
        let vn = dot(v, nu);
        let vt = sub(v, scale(nu, vn));
        let m = norm(vt);
        if m > self.floor && m > 0.0 {
            Some(scale(vt, 1.0 / m))
        } else {
            None
        }
    }

    /// Change of barycentric coordinates per unit length along `d` in face f.
    fn dlambda(&self, f: usize, d: [f64; 3]) -> [f64; 3] {
        let t = self.tris[f];
        let a = self.pts[t[0]];
        let e1 = sub(self.pts[t[1]], a);
        let e2 = sub(self.pts[t[2]], a);
        let (r1, r2) = (dot(e1, d), dot(e2, d));
        let g = self.ginv[f];
        let du = g[0] * r1 + g[1] * r2;
        let dv = g[1] * r1 + g[2] * r2;
        [-du - dv, du, dv]
    }

    /// Distance to the face edge along dλ, and that edge (NONE: no exit).
    fn exit(l: [f64; 3], dl: [f64; 3]) -> (f64, usize) {
        let mut best = f64::INFINITY;
        let mut k_exit = NONE;
        for k in 0..3 {
            if dl[k] < -EPS {
                let t = l[k].max(0.0) / -dl[k];
                if t < best {
                    best = t;
                    k_exit = k;
                }
            }
        }
        (best, k_exit)
    }

    fn point(&self, f: usize, l: [f64; 3], offset: f64) -> [f64; 3] {
        let t = self.tris[f];
        let mut p = [0.0; 3];
        let mut n = [0.0; 3];
        for k in 0..3 {
            for d in 0..3 {
                p[d] += l[k] * self.pts[t[k]][d];
                n[d] += l[k] * self.vnorm[t[k]][d];
            }
        }
        let m = norm(n);
        if offset != 0.0 && m > 0.0 {
            for d in 0..3 {
                p[d] += offset * n[d] / m;
            }
        }
        p
    }

    /// Centre of face f, as drawn (with the offset).
    pub fn centre(&self, f: usize, offset: f64) -> [f64; 3] {
        self.point(f, [1.0 / 3.0; 3], offset)
    }

    /// One direction from the centre of face `seed`. `sign` = +1 or −1.
    /// `stop(p)` ends the line before point p (even spacing).
    fn trace<F: Fn([f64; 3]) -> bool>(&self, seed: usize, sign: f64, max_len: f64,
                                      max_steps: usize, offset: f64,
                                      stop: &F) -> Vec<[f64; 3]> {
        let mut out = Vec::new();
        let mut f = seed;
        let mut l = [1.0 / 3.0; 3];
        let mut entry: usize = NONE; // local edge we came in through
        let mut len = 0.0;
        let mut stalls = 0;
        out.push(self.point(f, l, offset));
        for _ in 0..max_steps {
            let d1 = match self.dir_at(f, l) {
                Some(d) => scale(d, sign),
                None => break,
            };
            let dl1 = self.dlambda(f, d1);
            let (t1, k1) = Self::exit(l, dl1);
            if k1 == NONE {
                break;
            }
            if t1 <= EPS && k1 == entry {
                break; // both faces push into this edge: convergence line
            }
            // Midpoint step, never past the edge.
            let h = t1.min(self.hmax[f]).min(max_len - len);
            let mut lm = [0.0; 3];
            for k in 0..3 {
                lm[k] = (l[k] + 0.5 * h * dl1[k]).max(0.0);
            }
            let s = lm[0] + lm[1] + lm[2];
            for x in lm.iter_mut() {
                *x /= s;
            }
            let d2 = self.dir_at(f, lm).map(|d| scale(d, sign)).unwrap_or(d1);
            let dl2 = self.dlambda(f, d2);
            let (t2, k2) = Self::exit(l, dl2);
            let (dl, t_exit, k_exit) = if k2 == NONE || (t2 <= EPS && k2 == entry) {
                (dl1, t1, k1)
            } else {
                (dl2, t2, k2)
            };
            let step = t_exit.min(self.hmax[f]).min(max_len - len);
            for k in 0..3 {
                l[k] += step * dl[k];
            }
            len += step;
            let p = self.point(f, l, offset);
            if stop(p) {
                break;
            }
            out.push(p);
            if len >= max_len {
                break;
            }
            if step <= EPS {
                stalls += 1;
                if stalls > 8 {
                    break; // circling a vertex
                }
            } else {
                stalls = 0;
            }
            if step < t_exit - EPS {
                entry = NONE; // still inside this face
                continue;
            }
            // Cross into the neighbour face across edge k_exit.
            let g = self.nbr[f][k_exit];
            if g == NONE {
                break;
            }
            let t = self.tris[f];
            let (va, vb) = (t[(k_exit + 1) % 3], t[(k_exit + 2) % 3]);
            let (wa, wb) = (l[(k_exit + 1) % 3].max(0.0), l[(k_exit + 2) % 3].max(0.0));
            let s = wa + wb;
            let (wa, wb) = if s > 0.0 { (wa / s, wb / s) } else { (0.5, 0.5) };
            let tg = self.tris[g];
            let mut lg = [0.0; 3];
            let mut opp = NONE;
            for k in 0..3 {
                if tg[k] == va {
                    lg[k] = wa;
                } else if tg[k] == vb {
                    lg[k] = wb;
                } else {
                    opp = k;
                }
            }
            if opp == NONE {
                break;
            }
            // Would the line leave g straight back through the same edge?
            if let Some(dg) = self.dir_at(g, lg) {
                let back = self.dlambda(g, scale(dg, sign));
                if back[opp] < -EPS {
                    break; // convergence line: stop here, like oil does
                }
            }
            f = g;
            l = lg;
            entry = opp;
        }
        out
    }

    /// A whole line through the centre of `seed`: backward + forward.
    pub fn line<F: Fn([f64; 3]) -> bool>(&self, seed: usize, max_len: f64,
                                         max_steps: usize, offset: f64,
                                         stop: &F) -> Vec<[f64; 3]> {
        let mut back = self.trace(seed, -1.0, max_len, max_steps, offset, stop);
        let fwd = self.trace(seed, 1.0, max_len, max_steps, offset, stop);
        back.reverse();
        back.pop(); // the seed point is the first point of `fwd`
        back.extend(fwd);
        back
    }

    /// Evenly spaced lines (sequential): seeds closer than `spacing` to a
    /// drawn line are skipped; a line stops at half the spacing from others.
    /// Lines shorter than `min_len` are dropped.
    pub fn even_lines(&self, seeds: &[usize], spacing: f64, min_len: f64,
                      max_len: f64, max_steps: usize,
                      offset: f64) -> Vec<Vec<[f64; 3]>> {
        let mut grid = Grid::new(spacing);
        let mut out = Vec::new();
        for &s in seeds {
            if self.fnorm[s] == [0.0; 3] || grid.near(self.centre(s, offset), spacing) {
                continue;
            }
            let test = |p: [f64; 3]| grid.near(p, 0.5 * spacing);
            let line = self.line(s, max_len, max_steps, offset, &test);
            let mut len = 0.0;
            for w in line.windows(2) {
                len += norm(sub(w[1], w[0]));
            }
            if line.len() >= 2 && len >= min_len {
                grid.add_line(&line, spacing);
                out.push(line);
            }
        }
        out
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn never(_: [f64; 3]) -> bool {
        false
    }

    /// A strip of 2·n triangles along x (width w), field (1, vy, 0).
    fn strip(n: usize, w: f64, vy: f64) -> Surface {
        let mut pts = Vec::new();
        for i in 0..=n {
            pts.push([i as f64, 0.0, 0.0]);
            pts.push([i as f64, w, 0.0]);
        }
        let mut tris = Vec::new();
        for i in 0..n {
            let (a, b, c, d) = (2 * i, 2 * i + 2, 2 * i + 3, 2 * i + 1);
            tris.push([a, b, c]);
            tris.push([a, c, d]);
        }
        let v = vec![[1.0, vy, 0.0]; pts.len()];
        let nrm = vec![[0.0, 0.0, 1.0]; pts.len()];
        Surface::new(pts, tris, v, nrm, 1e-12)
    }

    fn length(line: &[[f64; 3]]) -> f64 {
        line.windows(2).map(|w| norm(sub(w[1], w[0]))).sum()
    }

    #[test]
    fn straight_line_crosses_all_faces() {
        let s = strip(10, 1.0, 0.0);
        let line = s.line(9, 100.0, 10000, 0.0, &never);
        let first = line.first().unwrap();
        let last = line.last().unwrap();
        assert!(first[0] < 1e-9, "starts at the left end: {first:?}");
        assert!((last[0] - 10.0).abs() < 1e-9, "ends at the right end: {last:?}");
        for p in &line {
            assert!(p[2].abs() < 1e-12, "stays on the surface");
            assert!((p[1] - line[0][1]).abs() < 1e-9, "stays straight");
        }
    }

    #[test]
    fn length_limit() {
        let s = strip(10, 1.0, 0.0);
        let line = s.trace(0, 1.0, 2.5, 10000, 0.0, &never);
        assert!((length(&line) - 2.5).abs() < 1e-9, "{}", length(&line));
    }

    #[test]
    fn stops_at_boundary_edge() {
        let s = strip(4, 1.0, 5.0);
        let line = s.trace(3, 1.0, 100.0, 10000, 0.0, &never);
        let last = line.last().unwrap();
        assert!((last[1] - 1.0).abs() < 1e-9, "ends on y = 1: {last:?}");
    }

    #[test]
    fn convergence_line_stops() {
        // Two faces whose fields point into their shared edge.
        let pts = vec![[0.0, 0.0, 0.0], [1.0, 0.0, 0.0], [0.0, 1.0, 0.0], [1.0, 1.0, 0.0]];
        let tris = vec![[0, 1, 2], [1, 3, 2]];
        let v = vec![[1.0, 1.0, 0.0], [1.0, 1.0, 0.0], [-1.0, -1.0, 0.0], [-1.0, -1.0, 0.0]];
        let n = vec![[0.0, 0.0, 1.0]; 4];
        // Vertex 1 and 2 sit on the shared edge; make them push inward too.
        let mut v = v;
        v[1] = [0.0, 0.0, 0.0];
        v[2] = [0.0, 0.0, 0.0];
        let s = Surface::new(pts, tris, v, n, 1e-12);
        let line = s.trace(0, 1.0, 100.0, 10000, 0.0, &never);
        let last = line.last().unwrap();
        assert!(last[0] + last[1] <= 1.0 + 1e-9, "does not cross the edge: {last:?}");
        assert!(line.len() < 100, "stops, does not circle");
    }

    #[test]
    fn offset_moves_along_normal() {
        let s = strip(2, 1.0, 0.0);
        let line = s.line(0, 100.0, 10000, 0.01, &never);
        for p in &line {
            assert!((p[2] - 0.01).abs() < 1e-12);
        }
    }

    #[test]
    fn linear_field_curves_smoothly() {
        // Rotating field v = (−y, x): lines are circles around the origin.
        let n = 40;
        let mut pts = Vec::new();
        for j in 0..=n {
            for i in 0..=n {
                pts.push([-1.0 + 2.0 * i as f64 / n as f64, -1.0 + 2.0 * j as f64 / n as f64, 0.0]);
            }
        }
        let mut tris = Vec::new();
        for j in 0..n {
            for i in 0..n {
                let a = j * (n + 1) + i;
                tris.push([a, a + 1, a + n + 2]);
                tris.push([a, a + n + 2, a + n + 1]);
            }
        }
        let v: Vec<[f64; 3]> = pts.iter().map(|p| [-p[1], p[0], 0.0]).collect();
        let nrm = vec![[0.0, 0.0, 1.0]; pts.len()];
        let s = Surface::new(pts, tris, v, nrm, 1e-12);
        // Seed a face near (0.5, 0); one quarter of the circle.
        let f = (0..s.n_faces())
            .min_by(|&a, &b| {
                let ca = s.centre(a, 0.0);
                let cb = s.centre(b, 0.0);
                let da = (ca[0] - 0.5).powi(2) + ca[1].powi(2);
                let db = (cb[0] - 0.5).powi(2) + cb[1].powi(2);
                da.partial_cmp(&db).unwrap()
            })
            .unwrap();
        let c = s.centre(f, 0.0);
        let r0 = (c[0] * c[0] + c[1] * c[1]).sqrt();
        let line = s.trace(f, 1.0, 0.5 * std::f64::consts::PI * r0, 100000, 0.0, &never);
        for p in &line {
            let r = (p[0] * p[0] + p[1] * p[1]).sqrt();
            assert!((r - r0).abs() < 0.01, "radius drifts: {r} vs {r0}");
        }
    }

    #[test]
    fn even_spacing_keeps_lines_apart() {
        // 10 × 4 plate of 0.25 squares, field along +x.
        let (nx, ny, h) = (40, 16, 0.25);
        let mut pts = Vec::new();
        for j in 0..=ny {
            for i in 0..=nx {
                pts.push([i as f64 * h, j as f64 * h, 0.0]);
            }
        }
        let mut tris = Vec::new();
        for j in 0..ny {
            for i in 0..nx {
                let a = j * (nx + 1) + i;
                tris.push([a, a + 1, a + nx + 2]);
                tris.push([a, a + nx + 2, a + nx + 1]);
            }
        }
        let v = vec![[1.0, 0.0, 0.0]; pts.len()];
        let nrm = vec![[0.0, 0.0, 1.0]; pts.len()];
        let s = Surface::new(pts, tris, v, nrm, 1e-12);
        let seeds: Vec<usize> = (0..s.n_faces()).collect();
        let lines = s.even_lines(&seeds, 0.5, 0.0, 100.0, 100000, 0.0);
        assert!(lines.len() >= 4, "several lines: {}", lines.len());
        let ys: Vec<f64> = lines.iter().map(|l| l[0][1]).collect();
        for i in 0..ys.len() {
            for j in 0..i {
                assert!((ys[i] - ys[j]).abs() >= 0.25 - 1e-9, "too close: {ys:?}");
            }
        }
    }
}
