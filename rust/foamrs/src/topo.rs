//! Geometry check of a triangle surface (fs_geometry.diagnose), in one pass.
//!
//! The same numbers as the VTK/numpy path, only faster (opening a project
//! with a 250 000-triangle model checked the scene several times, ~1.4 s
//! each):
//!   - open edges (one face), non-manifold edges (more than two faces),
//!     bad winding (two faces walk the shared edge in the SAME direction) —
//!     by sorting undirected edge keys, like the numpy code;
//!   - connected parts: faces that share a VERTEX are connected (as
//!     vtkPolyDataConnectivityFilter does), numbered in the order of their
//!     first face (also as VTK);
//!   - areas, degenerate faces, signed volume per part and in total;
//!   - edge statistics; hole sizes (bounding-box diagonal of each loop of
//!     open edges, joined by shared points);
//!   - the silhouette raster for the frontal areas: the voxels the surface
//!     passes through, by the rule of vtkVoxelModeller — a grid point is
//!     marked when its distance to a triangle is at most `max_dist`. Only
//!     the three projections are kept (the areas need nothing else).

use rayon::prelude::*;
use std::sync::atomic::{AtomicU8, Ordering};

pub struct Report {
    pub open_edges: usize,
    pub non_manifold_edges: usize,
    pub bad_winding: usize,
    pub n_regions: usize,
    pub regions: Vec<i64>,
    pub area: f64,
    pub degenerate: usize,
    pub part_volumes: Vec<f64>,
    pub signed_volume: f64,
    pub edge_stats: [f64; 4],
    pub hole_sizes: Vec<f64>,
}

fn sub(a: [f64; 3], b: [f64; 3]) -> [f64; 3] {
    [a[0] - b[0], a[1] - b[1], a[2] - b[2]]
}
fn dot(a: [f64; 3], b: [f64; 3]) -> f64 {
    a[0] * b[0] + a[1] * b[1] + a[2] * b[2]
}
fn cross(a: [f64; 3], b: [f64; 3]) -> [f64; 3] {
    [a[1] * b[2] - a[2] * b[1], a[2] * b[0] - a[0] * b[2], a[0] * b[1] - a[1] * b[0]]
}
fn norm(a: [f64; 3]) -> f64 {
    dot(a, a).sqrt()
}

struct Dsu {
    p: Vec<u32>,
}

impl Dsu {
    fn new(n: usize) -> Dsu {
        Dsu { p: (0..n as u32).collect() }
    }
    fn find(&mut self, mut x: u32) -> u32 {
        while self.p[x as usize] != x {
            let g = self.p[self.p[x as usize] as usize];
            self.p[x as usize] = g;
            x = g;
        }
        x
    }
    fn union(&mut self, a: u32, b: u32) {
        let (ra, rb) = (self.find(a), self.find(b));
        if ra != rb {
            self.p[ra.max(rb) as usize] = ra.min(rb);
        }
    }
}

/// numpy-style median (mean of the two middle values for an even count).
fn median(v: &mut [f64]) -> f64 {
    if v.is_empty() {
        return 0.0;
    }
    let n = v.len();
    let mid = n / 2;
    let (_, m, _) = v.select_nth_unstable_by(mid, |a, b| a.total_cmp(b));
    let hi = *m;
    if n % 2 == 1 {
        return hi;
    }
    // Even count: the other middle value is the largest of the left part.
    let left_max = v[..mid].iter().copied().fold(f64::NEG_INFINITY, f64::max);
    0.5 * (left_max + hi)
}

pub fn check(pts: &[[f64; 3]], tris: &[[u32; 3]], hole_limit: usize) -> Report {
    let n = pts.len();
    let m = tris.len();

    // --- edges: sort undirected keys, keep the direction and the face ----
    // Open / non-manifold follow vtkFeatureEdges: for each use of an edge,
    // count the OTHER faces that have it. None → an open edge (each such use
    // counts: the repeated edge of a degenerate face is two open edges, as
    // in VTK); two or more → non-manifold, counted once per edge. Bad
    // winding follows the numpy code: exactly two uses, same direction.
    let mut keys: Vec<(u64, bool, u32)> = Vec::with_capacity(m * 3);
    for (f, t) in tris.iter().enumerate() {
        for (a, b) in [(t[0], t[1]), (t[1], t[2]), (t[2], t[0])] {
            let (lo, hi) = if a < b { (a, b) } else { (b, a) };
            keys.push(((lo as u64) * (n as u64) + hi as u64, a < b, f as u32));
        }
    }
    keys.par_sort_unstable_by_key(|k| (k.0, k.2));
    let (mut open, mut nm, mut bad) = (0usize, 0usize, 0usize);
    let mut boundary: Vec<u64> = Vec::new();
    let mut i = 0;
    while i < keys.len() {
        let mut j = i + 1;
        while j < keys.len() && keys[j].0 == keys[i].0 {
            j += 1;
        }
        let group = &keys[i..j];
        // faces are sorted inside the group: count the distinct ones
        let distinct = 1 + group.windows(2).filter(|w| w[0].2 != w[1].2).count();
        if distinct == 1 {
            open += group.len();
            boundary.extend(std::iter::repeat(keys[i].0).take(group.len()));
        } else if distinct >= 3 {
            // once per edge: by the uses of the lowest face (as VTK does)
            nm += group.iter().filter(|e| e.2 == group[0].2).count();
        }
        if group.len() == 2 && group[0].1 == group[1].1 {
            bad += 1;
        }
        i = j;
    }

    // --- parts: faces sharing a vertex ---------------------------------
    let mut dsu = Dsu::new(n);
    for t in tris {
        dsu.union(t[0], t[1]);
        dsu.union(t[0], t[2]);
    }
    let mut label = vec![u32::MAX; n];
    let mut regions = Vec::with_capacity(m);
    let mut n_regions = 0u32;
    for t in tris {
        let r = dsu.find(t[0]) as usize;
        if label[r] == u32::MAX {
            label[r] = n_regions;
            n_regions += 1;
        }
        regions.push(label[r] as i64);
    }

    // --- areas, volumes, edge lengths ----------------------------------
    let per: Vec<(f64, f64, [f64; 3])> = tris.par_iter().map(|t| {
        let (a, b, c) = (pts[t[0] as usize], pts[t[1] as usize], pts[t[2] as usize]);
        let area = 0.5 * norm(cross(sub(b, a), sub(c, a)));
        let vol = dot(a, cross(b, c)) / 6.0;
        (area, vol, [norm(sub(b, a)), norm(sub(c, b)), norm(sub(a, c))])
    }).collect();
    let area: f64 = per.iter().map(|p| p.0).sum();
    let mean = if m > 0 { area / m as f64 } else { 0.0 };
    let thr = (mean * 1e-10).max(1e-24);
    let degenerate = per.iter().filter(|p| p.0 < thr).count();
    let mut part_volumes = vec![0.0; n_regions as usize];
    let mut signed = 0.0;
    for (k, p) in per.iter().enumerate() {
        part_volumes[regions[k] as usize] += p.1;
        signed += p.1;
    }
    let mut lens: Vec<f64> = per.iter().flat_map(|p| p.2).filter(|&l| l > 0.0).collect();
    let edge_stats = if lens.is_empty() {
        [0.0; 4]
    } else {
        let mn = lens.iter().copied().fold(f64::INFINITY, f64::min);
        let mx = lens.iter().copied().fold(f64::NEG_INFINITY, f64::max);
        let md = median(&mut lens);
        let mut shortest: Vec<f64> = per.iter()
            .map(|p| p.2[0].min(p.2[1]).min(p.2[2])).filter(|&l| l > 0.0).collect();
        let short = if shortest.is_empty() { mn } else { median(&mut shortest) };
        [mn, mx, md, short]
    };

    // --- holes: loops of open edges, joined by shared points ------------
    let mut hole_sizes = Vec::new();
    if !boundary.is_empty() {
        // vtkFeatureEdges merges coincident points in its output, so loops
        // are joined by POSITION, not by point number (a seam with doubled
        // points is one hole, not two). A loop of a single point is skipped.
        let mut by_pos: std::collections::HashMap<[u64; 3], u32> =
            std::collections::HashMap::new();
        let mut canon = |i: u32| -> u32 {
            let p = pts[i as usize];
            let k = [p[0].to_bits(), p[1].to_bits(), p[2].to_bits()];
            *by_pos.entry(k).or_insert(i)
        };
        let ends: Vec<(u32, u32)> = boundary.iter()
            .map(|&k| (canon((k / n as u64) as u32), canon((k % n as u64) as u32))).collect();
        let mut d = Dsu::new(n);
        for &(a, b) in &ends {
            d.union(a, b);
        }
        let mut boxes: std::collections::HashMap<u32, ([f64; 3], [f64; 3],
                                                       std::collections::HashSet<u32>)> =
            std::collections::HashMap::new();
        for &(a, b) in &ends {
            let r = d.find(a);
            let e = boxes.entry(r).or_insert(([f64::INFINITY; 3], [f64::NEG_INFINITY; 3],
                                              std::collections::HashSet::new()));
            for q in [a, b] {
                e.2.insert(q);
                let p = pts[q as usize];
                for ax in 0..3 {
                    e.0[ax] = e.0[ax].min(p[ax]);
                    e.1[ax] = e.1[ax].max(p[ax]);
                }
            }
        }
        hole_sizes = boxes.values().filter(|b| b.2.len() >= 2)
            .map(|(lo, hi, _)| norm(sub(*hi, *lo))).collect();
        hole_sizes.sort_by(|a, b| b.total_cmp(a));
        hole_sizes.truncate(hole_limit);
    }

    Report {
        open_edges: open, non_manifold_edges: nm, bad_winding: bad,
        n_regions: n_regions as usize, regions, area, degenerate, part_volumes,
        signed_volume: signed, edge_stats, hole_sizes,
    }
}

/// Closest distance² from point p to triangle (a, b, c) (Ericson).
#[cfg(test)]
fn dist2_tri(p: [f64; 3], a: [f64; 3], b: [f64; 3], c: [f64; 3]) -> f64 {
    let q = closest_on_tri(p, a, b, c);
    let d = sub(p, q);
    dot(d, d)
}

/// The point of triangle (a, b, c) closest to p (Ericson, Real-Time
/// Collision Detection 5.1.5).
fn closest_on_tri(p: [f64; 3], a: [f64; 3], b: [f64; 3], c: [f64; 3]) -> [f64; 3] {
    let ab = sub(b, a);
    let ac = sub(c, a);
    let ap = sub(p, a);
    let d1 = dot(ab, ap);
    let d2 = dot(ac, ap);
    let q = |x: [f64; 3]| x;
    if d1 <= 0.0 && d2 <= 0.0 {
        return q(a);
    }
    let bp = sub(p, b);
    let d3 = dot(ab, bp);
    let d4 = dot(ac, bp);
    if d3 >= 0.0 && d4 <= d3 {
        return q(b);
    }
    let vc = d1 * d4 - d3 * d2;
    if vc <= 0.0 && d1 >= 0.0 && d3 <= 0.0 {
        let v = d1 / (d1 - d3);
        return q([a[0] + ab[0] * v, a[1] + ab[1] * v, a[2] + ab[2] * v]);
    }
    let cp = sub(p, c);
    let d5 = dot(ab, cp);
    let d6 = dot(ac, cp);
    if d6 >= 0.0 && d5 <= d6 {
        return q(c);
    }
    let vb = d5 * d2 - d1 * d6;
    if vb <= 0.0 && d2 >= 0.0 && d6 <= 0.0 {
        let w = d2 / (d2 - d6);
        return q([a[0] + ac[0] * w, a[1] + ac[1] * w, a[2] + ac[2] * w]);
    }
    let va = d3 * d6 - d5 * d4;
    if va <= 0.0 && (d4 - d3) >= 0.0 && (d5 - d6) >= 0.0 {
        let w = (d4 - d3) / ((d4 - d3) + (d5 - d6));
        let bc = sub(c, b);
        return q([b[0] + bc[0] * w, b[1] + bc[1] * w, b[2] + bc[2] * w]);
    }
    let denom = 1.0 / (va + vb + vc);
    let v = vb * denom;
    let w = vc * denom;
    q([a[0] + ab[0] * v + ac[0] * w, a[1] + ab[1] * v + ac[1] * w,
       a[2] + ab[2] * v + ac[2] * w])
}

/// Silhouette projections of the surface voxels (vtkVoxelModeller rule):
/// grid point (i, j, k) at origin + (i, j, k)·voxel is on the shell when
/// some triangle is within `max_dist` of it. Returns the projections along
/// X (ny×nz), Y (nx×nz), Z (nx×ny), row-major, 1 = covered.
pub fn shell_projections(pts: &[[f64; 3]], tris: &[[u32; 3]], origin: [f64; 3],
                         dims: [usize; 3], voxel: f64, max_dist: f64)
                         -> (Vec<u8>, Vec<u8>, Vec<u8>) {
    let [nx, ny, nz] = dims;
    let px: Vec<AtomicU8> = (0..ny * nz).map(|_| AtomicU8::new(0)).collect();
    let py: Vec<AtomicU8> = (0..nx * nz).map(|_| AtomicU8::new(0)).collect();
    let pz: Vec<AtomicU8> = (0..nx * ny).map(|_| AtomicU8::new(0)).collect();
    let half = 0.5 * voxel;
    tris.par_iter().for_each(|t| {
        let (a, b, c) = (pts[t[0] as usize], pts[t[1] as usize], pts[t[2] as usize]);
        // VTK's EvaluatePosition gives up on a zero-area triangle.
        if norm(cross(sub(b, a), sub(c, a))) <= 0.0 {
            return;
        }
        let mut lo = [0usize; 3];
        let mut hi = [0usize; 3];
        for ax in 0..3 {
            let mn = a[ax].min(b[ax]).min(c[ax]) - max_dist;
            let mx = a[ax].max(b[ax]).max(c[ax]) + max_dist;
            let l = ((mn - origin[ax]) / voxel) as i64;     // truncation, as in VTK
            let h = ((mx - origin[ax]) / voxel) as i64;
            let lim = [nx, ny, nz][ax] as i64 - 1;
            lo[ax] = l.clamp(0, lim) as usize;
            hi[ax] = h.clamp(0, lim) as usize;
        }
        for k in lo[2]..=hi[2] {
            let z = origin[2] + k as f64 * voxel;
            for j in lo[1]..=hi[1] {
                let y = origin[1] + j as f64 * voxel;
                for i in lo[0]..=hi[0] {
                    let x = origin[0] + i as f64 * voxel;
                    // vtkVoxelModeller: the closest point of the triangle lies
                    // inside this voxel (within half a voxel on every axis).
                    let cp = closest_on_tri([x, y, z], a, b, c);
                    if (cp[0] - x).abs() <= half && (cp[1] - y).abs() <= half
                        && (cp[2] - z).abs() <= half {
                        px[j * nz + k].store(1, Ordering::Relaxed);
                        py[i * nz + k].store(1, Ordering::Relaxed);
                        pz[i * ny + j].store(1, Ordering::Relaxed);
                    }
                }
            }
        }
    });
    let take = |v: Vec<AtomicU8>| v.into_iter().map(|a| a.into_inner()).collect();
    (take(px), take(py), take(pz))
}

#[cfg(test)]
mod tests {
    use super::*;

    fn cube() -> (Vec<[f64; 3]>, Vec<[u32; 3]>) {
        let p = vec![[0.0, 0.0, 0.0], [1.0, 0.0, 0.0], [1.0, 1.0, 0.0], [0.0, 1.0, 0.0],
                     [0.0, 0.0, 1.0], [1.0, 0.0, 1.0], [1.0, 1.0, 1.0], [0.0, 1.0, 1.0]];
        // outward winding
        let t = vec![[0, 2, 1], [0, 3, 2], [4, 5, 6], [4, 6, 7], [0, 1, 5], [0, 5, 4],
                     [1, 2, 6], [1, 6, 5], [2, 3, 7], [2, 7, 6], [3, 0, 4], [3, 4, 7]];
        (p, t)
    }

    #[test]
    fn closed_cube() {
        let (p, t) = cube();
        let r = check(&p, &t, 400);
        assert_eq!((r.open_edges, r.non_manifold_edges, r.bad_winding), (0, 0, 0));
        assert_eq!(r.n_regions, 1);
        assert!((r.area - 6.0).abs() < 1e-12);
        assert!((r.signed_volume - 1.0).abs() < 1e-12, "{}", r.signed_volume);
        assert!(r.hole_sizes.is_empty());
    }

    #[test]
    fn open_flipped_and_second_part() {
        let (mut p, mut t) = cube();
        t.pop(); // hole: one triangle missing → 3 open edges
        t[0] = [0, 1, 2]; // flipped face → bad winding on its edges
        let off = p.len() as u32;
        p.extend([[5.0, 0.0, 0.0], [6.0, 0.0, 0.0], [5.0, 1.0, 0.0]]);
        t.push([off, off + 1, off + 2]);
        let r = check(&p, &t, 400);
        assert_eq!(r.n_regions, 2);
        assert_eq!(r.regions[0], 0);
        assert_eq!(*r.regions.last().unwrap(), 1);
        assert!(r.open_edges >= 3 + 3, "{}", r.open_edges);
        assert!(r.bad_winding >= 1);
        assert_eq!(r.hole_sizes.len(), 2);
    }

    #[test]
    fn medians_like_numpy() {
        assert_eq!(median(&mut [3.0, 1.0, 2.0]), 2.0);
        assert_eq!(median(&mut [4.0, 1.0, 3.0, 2.0]), 2.5);
    }

    #[test]
    fn distance_to_triangle() {
        let (a, b, c) = ([0.0, 0.0, 0.0], [1.0, 0.0, 0.0], [0.0, 1.0, 0.0]);
        assert!((dist2_tri([0.2, 0.2, 0.5], a, b, c) - 0.25).abs() < 1e-12);
        assert!((dist2_tri([2.0, 0.0, 0.0], a, b, c) - 1.0).abs() < 1e-12);
        assert!((dist2_tri([-1.0, -1.0, 0.0], a, b, c) - 2.0).abs() < 1e-12);
    }
}
