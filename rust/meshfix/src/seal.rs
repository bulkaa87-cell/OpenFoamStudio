//! Seal: a topological last step for assemblies that the Windows steps
//! leave open (from the stitch approach, wip/t9-stitch). In this branch the
//! winding Wrap calls it when its own post steps leave open or non-manifold
//! edges.
//!
//! The program's check counts open edges, non-manifold edges, parts with a
//! negative volume and zero-area faces. It does not look for faces that
//! cross. Thus a set of closed, outward shells that cut into each other is a
//! correct result. This step makes each shell closed. It does not cut faces
//! and does not compute a volume union. It keeps the original faces where it
//! can.
//!
//! Each round:
//! 1. Remove the inner walls at non-manifold seams. A patch is a group of
//!    faces joined by edges with two faces. When the other faces enclose a
//!    patch (winding number 1), the patch is an inner wall: it goes.
//! 2. Split the non-manifold edges. Faces pair by their turn order around
//!    the edge. A face without a partner gets its own edge (a new hole).
//! 3. Fill each hole. Two holes that face each other at a small distance
//!    (the open end of a thin wall) get a strip between them. Other holes
//!    get the minimum-area triangulation, or a fan from a new centre vertex
//!    when no triangulation is possible without a used edge.
//! 4. Orient each shell, so that its signed volume is positive.

use crate::math::*;
use crate::mesh::*;
use crate::stages::*;
use crate::rims::{bridge_rims, simple_cycles, weld_copy};
use std::collections::HashMap;

#[derive(Clone, Debug, Default, PartialEq, Eq)]
pub struct SealReport {
    pub rounds: usize,
    pub dropped_patches: usize,
    pub split_vertices: usize,
    pub removed_faces: usize,
    pub filled: usize,
    pub welded: usize,
    pub bridged: usize,
    pub fans: usize,
    pub turned_shells: usize,
}

fn dbg(msg: impl FnOnce() -> String) {
    if std::env::var_os("MESHFIX_DEBUG").is_some() {
        eprintln!("meshfix seal: {}", msg());
    }
}


/// Count of bad edges for the seal: open, non-manifold, and edges whose
/// two faces use them in the same direction.
pub fn seal_defects(mesh: &Mesh) -> (usize, usize, usize) {
    let ef = mesh.edge_faces();
    let (mut open, mut nm, mut bad) = (0, 0, 0);
    for (&(a, b), fs) in &ef {
        match fs.len() {
            1 => open += 1,
            2 => {
                let (f, g) = (&mesh.triangles[fs[0] as usize], &mesh.triangles[fs[1] as usize]);
                if directed(f, a, b) == directed(g, a, b) {
                    bad += 1;
                }
            }
            _ => nm += 1,
        }
    }
    (open, nm, bad)
}

/// Distance (normalized units) for a T-junction split before the fill.
const TJUNCTION_TOL: f64 = 1e-6;

/// Run the seal rounds. Returns the report.
pub fn seal(mesh: &mut Mesh) -> SealReport {
    let mut rep = SealReport::default();
    for round in 0..8 {
        let (open, nm, bad) = seal_defects(mesh);
        let neg = negative_shells(mesh);
        dbg(|| format!("round {round}: open {open} nm {nm} bad {bad} negative shells {neg}"));
        if open == 0 && nm == 0 && bad == 0 && neg == 0 {
            break;
        }
        rep.rounds += 1;
        rep.split_vertices += split_partial(mesh);
        if nm > 0 {
            rep.removed_faces += remove_stubborn_nm(mesh);
        }
        // Faces on an orientation conflict go. Then each hole rim has one
        // direction, and the fill agrees with the faces around it.
        let conflict = orient_bfs(mesh);
        if conflict > 0 {
            rep.removed_faces += conflict;
            rep.split_vertices += split_partial(mesh);
        }
        // A rim vertex on another rim edge (a T-junction, often a hole of
        // three points on a line): split that edge. A fill there has zero area.
        mesh.split_boundary_tjunctions(TJUNCTION_TOL);
        rep.bridged += bridge_rims(mesh);
        let (f, w, fan) = fill_loops(mesh);
        rep.filled += f;
        rep.welded += w;
        rep.fans += fan;
        rep.turned_shells += orient(mesh);
    }
    dbg(|| format!("{rep:?}"));
    rep
}

/// Edge-connected shells with a negative signed volume.
fn negative_shells(mesh: &Mesh) -> usize {
    let ef = mesh.edge_faces();
    components(mesh, &ef).iter().filter(|s| shell_volume(mesh, s) < 0.0).count()
}

// ---- 2. Non-manifold split with partial pairs ----------------------------

fn find(p: &mut [u32], mut x: u32) -> u32 {
    while p[x as usize] != x {
        let g = p[p[x as usize] as usize];
        p[x as usize] = g;
        x = g;
    }
    x
}

/// Give each manifold sheet at a non-manifold edge or vertex its own vertex
/// copies. At an edge with more than two faces, the faces pair by their turn
/// order (see stages::pair_around_edge). A face without a partner gets an
/// open edge. Returns the number of new vertices.
pub fn split_partial(mesh: &mut Mesh) -> usize {
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
            n if n > 2 => pair_around_edge(mesh, a, b, fs),
            _ => Vec::new(),
        };
        for (f, g) in pairs {
            join(&mut parent, corner(mesh, f, a), corner(mesh, g, a));
            join(&mut parent, corner(mesh, f, b), corner(mesh, g, b));
        }
    }
    let nv = mesh.vertices.len();
    let mut taken = vec![false; nv];
    let mut root_id: HashMap<u32, u32> = HashMap::new();
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

/// An edge that stays non-manifold after the split: the faces on it are
/// joined around its end vertices too (a sheet that folds onto itself, or
/// faces that are lines). Keep one pair of faces with opposite directions
/// (the pair of the turn order when there is one) and remove the others.
/// Their holes get a fill in the next step. Returns the faces removed.
pub fn remove_stubborn_nm(mesh: &mut Mesh) -> usize {
    let ef = mesh.edge_faces();
    let mut keep = vec![true; mesh.triangles.len()];
    let mut keys: Vec<EdgeKey> = ef.iter().filter(|(_, fs)| fs.len() > 2).map(|(k, _)| *k).collect();
    keys.sort_unstable();
    for (a, b) in keys {
        let fs: Vec<u32> = ef[&(a, b)].iter().copied().filter(|&f| keep[f as usize]).collect();
        if fs.len() <= 2 {
            continue;
        }
        let pair = pair_around_edge(mesh, a, b, &fs).first().copied().or_else(|| {
            let f0 = fs[0];
            let d0 = directed(&mesh.triangles[f0 as usize], a, b);
            fs.iter().copied().find(|&g| directed(&mesh.triangles[g as usize], a, b) != d0).map(|g| (f0, g))
        });
        for &f in &fs {
            let kept = match pair {
                Some((x, y)) => f == x || f == y,
                None => f == fs[0],
            };
            if !kept {
                keep[f as usize] = false;
            }
        }
    }
    let n = keep.iter().filter(|k| !**k).count();
    if n > 0 {
        mesh.retain_faces(&keep);
    }
    n
}

// ---- 3. Hole fill ----------------------------------------------------------

/// Largest hole for the O(n^3) minimum-area triangulation.
const MAX_DP: usize = 400;

/// Fill each boundary loop. Returns (filled loops, welded copies, fans).
///
/// A rim can pass two times through one position: once on each of two
/// vertex copies (made by the split). A fill face on both copies has zero
/// area. Thus the copies are welded first, when the weld gives no edge with
/// more than two faces. A slit (a→x→a') then closes, and a pinched rim
/// becomes two simple rims.
pub fn fill_loops(mesh: &mut Mesh) -> (usize, usize, usize) {
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
    let (mut filled, mut welded, mut fans) = (0, 0, 0);
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
            if cyc.len() < 3 {
                continue; // a closed slit: the weld joined its two edges
            }
            fans += fill_one(mesh, &mut ef, &cyc) as usize;
            filled += 1;
        }
    }
    (filled, welded, fans)
}

/// Fill one simple rim (in rim order). Returns true when a centre fan was
/// necessary.
fn fill_one(mesh: &mut Mesh, ef: &mut EdgeFaces, rim: &[u32]) -> bool {
    if rim.len() == 3 && split_flat_rim(mesh, ef, rim) {
        return false;
    }
    // The fill faces go opposite to the rim.
    let poly: Vec<u32> = rim.iter().rev().copied().collect();
    // Candidates in this order: minimum area, CDT, centre fan. The first
    // valid one without a zero-area face wins; else the first valid one.
    let flat = |v: &[Vec3], tris: &[[u32; 3]]| {
        tris.iter().any(|t| {
            let (a, b, c) = (v[t[0] as usize], v[t[1] as usize], v[t[2] as usize]);
            let l = dist2(a, b).max(dist2(b, c)).max(dist2(c, a));
            tri_area(a, b, c) <= 1e-9 * l
        })
    };
    let mut best: Option<Vec<[u32; 3]>> = None;
    let mut fan = false;
    for k in 0..3 {
        let tris = match k {
            0 if poly.len() <= MAX_DP => triangulate_dp_banned(&mesh.vertices, &poly, ef),
            1 => triangulate_cdt(&mesh.vertices, &poly).unwrap_or_default(),
            2 => {
                // The centre vertex is added only when the fan is used.
                let mut v = mesh.vertices.clone();
                let t = centre_fan(&mut v, &poly);
                let ok = !flat(&v, &t) || best.is_none();
                if ok {
                    mesh.vertices.push(*v.last().unwrap());
                    best = Some(t);
                    fan = true;
                }
                break;
            }
            _ => Vec::new(),
        };
        if tris.is_empty() || !valid_fill(&tris, &poly, ef) {
            continue;
        }
        let f = flat(&mesh.vertices, &tris);
        if best.is_none() {
            best = Some(tris);
        } else if !f {
            best = Some(tris);
        }
        if !f {
            break;
        }
    }
    let tris = best.unwrap_or_default();
    for t in &tris {
        let f = mesh.triangles.len() as u32;
        for i in 0..3 {
            ef.entry(ekey(t[i], t[(i + 1) % 3])).or_default().push(f);
        }
        mesh.triangles.push(*t);
    }
    fan
}
/// A rim of three points on a line: u→w is the long edge, m is between.
/// The face on u→w is split at m. Then the rim closes with no new face
/// (one fill face here would have zero area). Returns true when done.
fn split_flat_rim(mesh: &mut Mesh, ef: &mut EdgeFaces, rim: &[u32]) -> bool {
    let p = |i: u32| mesh.vertices[i as usize];
    let [r0, r1, r2] = [rim[0], rim[1], rim[2]];
    let lmax = dist2(p(r0), p(r1)).max(dist2(p(r1), p(r2))).max(dist2(p(r2), p(r0)));
    if tri_area(p(r0), p(r1), p(r2)) > 1e-9 * lmax {
        return false;
    }
    // The long edge in rim order.
    let k = (0..3)
        .max_by(|&x, &y| dist2(p(rim[x]), p(rim[(x + 1) % 3])).total_cmp(&dist2(p(rim[y]), p(rim[(y + 1) % 3]))))
        .unwrap();
    let (u, w, m) = (rim[k], rim[(k + 1) % 3], rim[(k + 2) % 3]);
    let Some(fs) = ef.get(&ekey(u, w)) else { return false };
    if fs.len() != 1 {
        return false;
    }
    let g = fs[0];
    let t = mesh.triangles[g as usize];
    if !directed(&t, u, w) {
        return false;
    }
    let c = rotate_to(t, u)[2];
    if c == m || ef.contains_key(&ekey(m, c)) {
        return false;
    }
    let g2 = mesh.triangles.len() as u32;
    mesh.triangles[g as usize] = [u, m, c];
    mesh.triangles.push([m, w, c]);
    ef.remove(&ekey(u, w));
    ef.entry(ekey(u, m)).or_default().push(g);
    ef.entry(ekey(m, w)).or_default().push(g2);
    if let Some(l) = ef.get_mut(&ekey(w, c)) {
        for x in l.iter_mut() {
            if *x == g {
                *x = g2;
            }
        }
    }
    ef.insert(ekey(m, c), vec![g, g2]);
    true
}

/// True when the fill uses each rim edge one time, each new edge two
/// times, and no new edge is an edge of the mesh already.
fn valid_fill(tris: &[[u32; 3]], poly: &[u32], ef: &EdgeFaces) -> bool {
    let n = poly.len();
    let rim: std::collections::HashSet<EdgeKey> = (0..n).map(|i| ekey(poly[i], poly[(i + 1) % n])).collect();
    let mut count: HashMap<EdgeKey, u32> = HashMap::new();
    for t in tris {
        if t[0] == t[1] || t[1] == t[2] || t[2] == t[0] {
            return false;
        }
        for i in 0..3 {
            *count.entry(ekey(t[i], t[(i + 1) % 3])).or_default() += 1;
        }
    }
    rim.iter().all(|k| count.get(k) == Some(&1))
        && count.iter().all(|(k, &c)| rim.contains(k) || (c == 2 && !ef.contains_key(k)))
}

/// Fan from a new vertex at the centroid. Each new edge is new, so the
/// fill can never give an edge with more than two faces.
fn centre_fan(v: &mut Vec<Vec3>, poly: &[u32]) -> Vec<[u32; 3]> {
    let mut c = [0.0; 3];
    for &i in poly {
        c = add(c, v[i as usize]);
    }
    let ci = v.len() as u32;
    v.push(scale(c, 1.0 / poly.len() as f64));
    (0..poly.len()).map(|k| [poly[k], poly[(k + 1) % poly.len()], ci]).collect()
}

/// Minimum-area triangulation where no diagonal is an existing mesh edge.
/// Empty when that is not possible.
fn triangulate_dp_banned(v: &[Vec3], poly: &[u32], ef: &EdgeFaces) -> Vec<[u32; 3]> {
    let n = poly.len();
    if n < 3 {
        return Vec::new();
    }
    if n == 3 {
        return vec![[poly[0], poly[1], poly[2]]];
    }
    let used = |i: usize, j: usize| ef.contains_key(&ekey(poly[i], poly[j]));
    let p = |i: usize| v[poly[i] as usize];
    let mut w = vec![f64::INFINITY; n * n];
    let mut o = vec![0usize; n * n];
    for i in 0..n - 1 {
        w[i * n + i + 1] = 0.0;
    }
    for len in 2..n {
        for i in 0..n - len {
            let j = i + len;
            if !(i == 0 && j == n - 1) && used(i, j) {
                continue;
            }
            for m in i + 1..j {
                let (a, b) = (w[i * n + m], w[m * n + j]);
                if !a.is_finite() || !b.is_finite() {
                    continue;
                }
                let c = a + b + fill_weight(p(i), p(m), p(j));
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
    let mut out = Vec::with_capacity(n - 2);
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

/// Weight of one fill triangle (the same as in stages.rs): area plus a
/// small part of the longest edge squared, against slivers.
fn fill_weight(a: Vec3, b: Vec3, c: Vec3) -> f64 {
    let l = dist2(a, b).max(dist2(b, c)).max(dist2(c, a));
    let ar = tri_area(a, b, c);
    // A face of zero area is a defect of the program's check: avoid it.
    let pen = if ar <= 1e-9 * l { 10.0 * l + 1e-6 } else { 0.0 };
    ar + 1e-3 * l + pen
}

// ---- 4. Orientation ----------------------------------------------------------

/// Orient the faces of each shell the same way (breadth-first through the
/// edges with two faces; the class with less area turns over). A face pair
/// on an edge where the orientation conflicts is removed. Returns the
/// number of faces removed.
pub fn orient_bfs(mesh: &mut Mesh) -> usize {
    let ef = mesh.edge_faces();
    let nf = mesh.triangles.len();
    let mut class = vec![0u8; nf];
    let mut drop = vec![false; nf];
    let mut queue = std::collections::VecDeque::new();
    let mut members = Vec::new();
    // Seeds in the order of area, largest first: the conflicts then go to
    // the small faces at the far end.
    let mut seeds: Vec<usize> = (0..nf).collect();
    let area: Vec<f64> = (0..nf)
        .map(|f| {
            let [a, b, c] = mesh.tri_pos(f);
            tri_area(a, b, c)
        })
        .collect();
    seeds.sort_by(|&x, &y| area[y].total_cmp(&area[x]));
    for &seed in &seeds {
        if class[seed] != 0 {
            continue;
        }
        members.clear();
        let mut sum = [0f64; 3];
        class[seed] = 1;
        queue.push_back(seed);
        while let Some(f) = queue.pop_front() {
            members.push(f);
            sum[class[f] as usize] += area[f];
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
                    drop[f] = true;
                    drop[g] = true;
                }
            }
        }
        let flip = if sum[1] >= sum[2] { 2 } else { 1 };
        for &f in &members {
            if class[f] == flip {
                mesh.triangles[f].swap(1, 2);
            }
        }
    }
    let n = drop.iter().filter(|d| **d).count();
    if n > 0 {
        let keep: Vec<bool> = drop.iter().map(|d| !d).collect();
        mesh.retain_faces(&keep);
    }
    n
}

/// Make the faces of each shell agree, then turn each shell with a negative
/// signed volume. Returns the number of shells turned.
fn orient(mesh: &mut Mesh) -> usize {
    fix_flipped(mesh);
    let ef = mesh.edge_faces();
    let shells = components(mesh, &ef);
    let mut n = 0;
    for s in &shells {
        if shell_volume(mesh, s) < 0.0 {
            for &f in s {
                mesh.triangles[f as usize].swap(1, 2);
            }
            n += 1;
        }
    }
    n
}
