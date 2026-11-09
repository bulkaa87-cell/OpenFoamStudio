//! Rim tools from the stitch approach (wip/t9-stitch, seal.rs): the bridge
//! of the two rims of an open wall end, and the weld of rim vertex copies.
//! CloseAllHoles (stages.rs) uses both before it fills a hole.
use crate::math::*;
use crate::mesh::*;
use std::collections::HashMap;

fn dbg2(msg: impl FnOnce() -> String) {
    if std::env::var_os("MESHFIX_DEBUG_BRIDGE").is_some() {
        eprintln!("meshfix bridge: {}", msg());
    }
}

// ---- 3a. Bridge of two rims ------------------------------------------------
//
// The open end of a thin wall (a tube, a lip, a flange) has two rims: one on
// each side of the wall, at the wall thickness from each other. A fill of
// each rim gives two disks across the opening (for example across an engine
// duct). The correct closure is a strip between the two rims.

/// Largest gap of a bridge, normalized units (the largest extent of the
/// model is 5..400 units).
pub const BRIDGE_GAP: f64 = 2.0;
/// The gap must also be below this part of the smaller rim size.
const BRIDGE_GAP_REL: f64 = 0.2;

struct Rim {
    lo: Vec3,
    hi: Vec3,
    size: f64,
}

/// Distance from p to segment a-b, and the closest point.
fn seg_closest(p: Vec3, a: Vec3, b: Vec3) -> (f64, Vec3) {
    let d = sub(b, a);
    let l2 = dot(d, d);
    let t = if l2 > 0.0 { (dot(sub(p, a), d) / l2).clamp(0.0, 1.0) } else { 0.0 };
    let q = add(a, scale(d, t));
    (dist(p, q), q)
}

/// Closest point on the closed polyline `l` to p: (distance, point, segment).
fn loop_closest(v: &[Vec3], l: &[u32], p: Vec3) -> (f64, Vec3, usize) {
    let n = l.len();
    let mut best = (f64::INFINITY, p, 0);
    for i in 0..n {
        let (d, q) = seg_closest(p, v[l[i] as usize], v[l[(i + 1) % n] as usize]);
        if d < best.0 {
            best = (d, q, i);
        }
    }
    best
}

/// Score of a bridge between rims a and b (rim order), or None when they
/// are not the two rims of one wall end. Lower is better (mean gap).
fn bridge_score(mesh: &Mesh, ef: &EdgeFaces, a: &[u32], b: &[u32], gap: f64) -> Option<f64> {
    let v = &mesh.vertices;
    let (mut sum, mut cnt, mut band, mut anti) = (0.0, 0usize, 0usize, 0usize);
    for (x, y) in [(a, b), (b, a)] {
        let n = x.len();
        for i in 0..n {
            let (p0, p1) = (v[x[i] as usize], v[x[(i + 1) % n] as usize]);
            let mid = scale(add(p0, p1), 0.5);
            let (d, q, k) = loop_closest(v, y, mid);
            if d > gap {
                dbg2(|| format!("pair {}x{}: gap {d:.3} > {gap:.3} at {:?}", a.len(), b.len(), mid));
                return None;
            }
            sum += d;
            cnt += 1;
            // The two rims of a wall end run in opposite directions (as the
            // outer and the inner circle of an annulus).
            let ty = sub(v[y[(k + 1) % y.len()] as usize], v[y[k] as usize]);
            if dot(sub(p1, p0), ty) < 0.0 {
                anti += 1;
            }
            // The face on this rim edge must not point to the other rim:
            // then the surface between the rims exists already (a band).
            let Some(fs) = ef.get(&ekey(x[i], x[(i + 1) % n])) else { continue };
            let t = mesh.triangles[fs[0] as usize];
            let Some(c) = t.iter().copied().find(|&z| z != x[i] && z != x[(i + 1) % n]) else { continue };
            let e = sub(p1, p0);
            let le2 = dot(e, e);
            let w = sub(v[c as usize], p0);
            let w = if le2 > 0.0 { sub(w, scale(e, dot(w, e) / le2)) } else { w };
            let dq = sub(q, mid);
            let (lw, lq) = (norm(w), norm(dq));
            if lw > 0.0 && lq > 1e-9 && dot(w, dq) > 0.7 * lw * lq {
                band += 1;
            }
        }
    }
    dbg2(|| format!("pair {}x{}: anti {anti}/{cnt} band {band} mean {:.3} at {:?}", a.len(), b.len(), sum / cnt.max(1) as f64, v[a[0] as usize]));
    if cnt == 0 || (anti as f64) < 0.8 * cnt as f64 || (band as f64) > 0.3 * cnt as f64 {
        return None;
    }
    Some(sum / cnt as f64)
}

/// Strip between rims a and b (both in rim order). Each rim edge is used
/// one time against its direction. None when the strip is not valid.
fn zipper(mesh: &Mesh, ef: &EdgeFaces, a: &[u32], b: &[u32]) -> Option<Vec<[u32; 3]>> {
    let v = &mesh.vertices;
    // P runs against rim a, R = rim b: P and R run the same way.
    let p: Vec<u32> = a.iter().rev().copied().collect();
    let (n, m) = (p.len(), b.len());
    let j0 = (0..m)
        .min_by(|&x, &y| dist2(v[p[0] as usize], v[b[x] as usize]).total_cmp(&dist2(v[p[0] as usize], v[b[y] as usize])))?;
    let r = |j: usize| b[(j0 + j) % m];
    let pp = |i: usize| p[i % n];
    let (mut i, mut j) = (0usize, 0usize);
    let mut out = Vec::with_capacity(n + m);
    while i < n || j < m {
        // Advance on P: face (p_i, p_i+1, r_j), new diagonal (p_i+1, r_j).
        // Advance on R: face (r_j+1, r_j, p_i), new diagonal (p_i, r_j+1).
        let adv_p = if i >= n {
            false
        } else if j >= m {
            true
        } else {
            dist2(v[pp(i + 1) as usize], v[r(j) as usize]) <= dist2(v[pp(i) as usize], v[r(j + 1) as usize])
        };
        if adv_p {
            out.push([pp(i), pp(i + 1), r(j)]);
            i += 1;
        } else {
            out.push([r(j + 1), r(j), pp(i)]);
            j += 1;
        }
    }
    // Validity: rim edges one time, new edges two times and not in the mesh,
    // no face with zero area.
    let rim: std::collections::HashSet<EdgeKey> = (0..n)
        .map(|i| ekey(p[i], p[(i + 1) % n]))
        .chain((0..m).map(|j| ekey(b[j], b[(j + 1) % m])))
        .collect();
    let mut count: HashMap<EdgeKey, u32> = HashMap::new();
    for t in &out {
        if t[0] == t[1] || t[1] == t[2] || t[2] == t[0] {
            return None;
        }
        let (x, y, z) = (v[t[0] as usize], v[t[1] as usize], v[t[2] as usize]);
        let l = dist2(x, y).max(dist2(y, z)).max(dist2(z, x));
        if tri_area(x, y, z) <= 1e-9 * l {
            return None;
        }
        for k in 0..3 {
            *count.entry(ekey(t[k], t[(k + 1) % 3])).or_default() += 1;
        }
    }
    let ok = rim.iter().all(|k| count.get(k) == Some(&1))
        && count.iter().all(|(k, &c)| rim.contains(k) || (c == 2 && !ef.contains_key(k)));
    ok.then_some(out)
}

/// Bridge each pair of rims that are the two sides of one open wall end.
/// Returns the number of strips made.
pub fn bridge_rims(mesh: &mut Mesh) -> usize {
    let loops = mesh.boundary_loops();
    if loops.len() < 2 {
        return 0;
    }
    let ef = mesh.edge_faces();
    let rims: Vec<Rim> = loops
        .iter()
        .map(|l| {
            let mut lo = [f64::INFINITY; 3];
            let mut hi = [f64::NEG_INFINITY; 3];
            for &x in l {
                let p = mesh.vertices[x as usize];
                for k in 0..3 {
                    lo[k] = lo[k].min(p[k]);
                    hi[k] = hi[k].max(p[k]);
                }
            }
            Rim { lo, hi, size: dist(lo, hi) }
        })
        .collect();
    // Candidate pairs: boxes near each other, sizes alike.
    let mut cand: Vec<(f64, usize, usize)> = Vec::new();
    for i in 0..loops.len() {
        for j in i + 1..loops.len() {
            let (ri, rj) = (&rims[i], &rims[j]);
            let small = ri.size.min(rj.size);
            let gap = BRIDGE_GAP.min(BRIDGE_GAP_REL * small);
            if small <= 0.0 || ri.size.max(rj.size) > 1.5 * small {
                continue;
            }
            if (0..3).any(|k| ri.lo[k] > rj.hi[k] + gap || rj.lo[k] > ri.hi[k] + gap) {
                continue;
            }
            if (loops[i].len() as f64) * (loops[j].len() as f64) > 4e7 {
                continue;
            }
            if let Some(s) = bridge_score(mesh, &ef, &loops[i], &loops[j], gap) {
                cand.push((s, i, j));
            }
        }
    }
    cand.sort_by(|x, y| x.0.total_cmp(&y.0));
    let mut used = vec![false; loops.len()];
    let mut made = 0;
    let mut ef = ef;
    for (_, i, j) in cand {
        if used[i] || used[j] {
            continue;
        }
        let Some(tris) = zipper(mesh, &ef, &loops[i], &loops[j]) else { continue };
        used[i] = true;
        used[j] = true;
        for t in tris {
            let f = mesh.triangles.len() as u32;
            for k in 0..3 {
                ef.entry(ekey(t[k], t[(k + 1) % 3])).or_default().push(f);
            }
            mesh.triangles.push(t);
        }
        made += 1;
    }
    made
}

// ---- Weld of rim vertex copies ----------------------------------------------

/// A rim can pass two times through one position: once on each of two
/// vertex copies (made by a split). A fill face on both copies has zero
/// area. Thus the copies are welded first, when the weld gives no edge with
/// more than two faces. A slit (a→x→a') then closes, and a pinched rim
/// becomes two simple rims.
///
/// Returns the simple rims (in rim order) that are left to fill, and the
/// number of welded copies.
pub fn weld_rim_copies(mesh: &mut Mesh) -> (Vec<Vec<u32>>, usize) {
    let loops = mesh.boundary_loops();
    let mut ef = mesh.edge_faces();
    let nv = mesh.vertices.len();
    let mut on_rim = vec![false; nv];
    for l in &loops {
        for &v in l {
            on_rim[v as usize] = true;
        }
    }
    let mut nbr: HashMap<u32, Vec<u32>> = HashMap::new();
    for &(a, b) in ef.keys() {
        if on_rim[a as usize] {
            nbr.entry(a).or_default().push(b);
        }
        if on_rim[b as usize] {
            nbr.entry(b).or_default().push(a);
        }
    }
    let mut welded = 0;
    let mut out = Vec::new();
    for l in &loops {
        let mut first: HashMap<[u64; 3], u32> = HashMap::new();
        let mut seq = Vec::with_capacity(l.len());
        for &v in l {
            let p = mesh.vertices[v as usize];
            let key = [p[0].to_bits(), p[1].to_bits(), p[2].to_bits()];
            match first.get(&key) {
                Some(&a) if a != v && weld_copy(mesh, &mut ef, &mut nbr, a, v) => {
                    welded += 1;
                    seq.push(a);
                }
                Some(_) => seq.push(v),
                None => {
                    first.insert(key, v);
                    seq.push(v);
                }
            }
        }
        for cyc in simple_cycles(&seq) {
            // A closed slit: the weld joined its two edges.
            if cyc.len() >= 3 {
                out.push(cyc);
            }
        }
    }
    (out, welded)
}

/// Weld vertex w into a (same position). Refused when an edge would get
/// more than two faces, or two faces in the same direction.
pub(crate) fn weld_copy(mesh: &mut Mesh, ef: &mut EdgeFaces, nbr: &mut HashMap<u32, Vec<u32>>, a: u32, w: u32) -> bool {
    let nw = nbr.get(&w).cloned().unwrap_or_default();
    let na = nbr.get(&a).cloned().unwrap_or_default();
    if nw.contains(&a) || na.contains(&w) {
        return false;
    }
    for &u in &nw {
        let fw = &ef[&ekey(w, u)];
        if let Some(fa) = ef.get(&ekey(a, u)) {
            if fa.len() + fw.len() > 2 {
                return false;
            }
            let da = directed(&mesh.triangles[fa[0] as usize], a, u);
            let dw = directed(&mesh.triangles[fw[0] as usize], w, u);
            if da == dw {
                return false;
            }
        }
    }
    let mut faces: Vec<u32> = Vec::new();
    for &u in &nw {
        let fs = ef.remove(&ekey(w, u)).unwrap_or_default();
        faces.extend_from_slice(&fs);
        ef.entry(ekey(a, u)).or_default().extend(fs);
        if let Some(l) = nbr.get_mut(&u) {
            for x in l.iter_mut() {
                if *x == w {
                    *x = a;
                }
            }
            l.sort_unstable();
            l.dedup();
        }
        let la = nbr.entry(a).or_default();
        if !la.contains(&u) {
            la.push(u);
        }
    }
    faces.sort_unstable();
    faces.dedup();
    for f in faces {
        for x in mesh.triangles[f as usize].iter_mut() {
            if *x == w {
                *x = a;
            }
        }
    }
    nbr.remove(&w);
    true
}

/// Split a closed vertex sequence into simple cycles at repeated vertices.
pub(crate) fn simple_cycles(seq: &[u32]) -> Vec<Vec<u32>> {
    let mut out = Vec::new();
    let mut stack: Vec<u32> = Vec::with_capacity(seq.len());
    let mut at: HashMap<u32, usize> = HashMap::new();
    for &v in seq {
        if let Some(&k) = at.get(&v) {
            out.push(stack[k..].to_vec());
            for x in &stack[k + 1..] {
                at.remove(x);
            }
            stack.truncate(k + 1);
        } else {
            at.insert(v, stack.len());
            stack.push(v);
        }
    }
    out.push(stack);
    out
}
