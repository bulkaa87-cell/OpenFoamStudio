//! Tests for each step, on the defects of tests/meshfix_cases.py.
//! Run: cargo test --no-default-features

use crate::mesh::{components, shell_volume};
use crate::stages::*;
use crate::*;

const CUBE_T: [[u32; 3]; 12] = [
    [0, 2, 1], [0, 3, 2], [4, 5, 6], [4, 6, 7], [0, 1, 5], [0, 5, 4],
    [3, 7, 6], [3, 6, 2], [0, 4, 7], [0, 7, 3], [1, 2, 6], [1, 6, 5],
];

fn cube(s: f64, o: Vec3) -> Mesh {
    let v = [[0., 0., 0.], [1., 0., 0.], [1., 1., 0.], [0., 1., 0.],
             [0., 0., 1.], [1., 0., 1.], [1., 1., 1.], [0., 1., 1.]];
    Mesh::new(v.iter().map(|p| add(scale(*p, s), o)).collect(), CUBE_T.to_vec())
}

fn join(a: &Mesh, b: &Mesh) -> Mesh {
    let off = a.vertices.len() as u32;
    let mut m = a.clone();
    m.vertices.extend_from_slice(&b.vertices);
    m.triangles.extend(b.triangles.iter().map(|t| [t[0] + off, t[1] + off, t[2] + off]));
    m
}

fn soup(m: &Mesh) -> Mesh {
    let mut o = Mesh::default();
    for f in 0..m.triangles.len() {
        let b = o.vertices.len() as u32;
        o.vertices.extend_from_slice(&m.tri_pos(f));
        o.triangles.push([b, b + 1, b + 2]);
    }
    o
}

/// Top edge 4-5 split: the top face uses copies of 4 and 5 moved up by gap.
fn seam(gap: f64, s: f64) -> Mesh {
    let mut m = cube(s, [0.0; 3]);
    let (a, b) = (m.vertices[4], m.vertices[5]);
    m.vertices.push(add(a, [0.0, 0.0, gap]));
    m.vertices.push(add(b, [0.0, 0.0, gap]));
    m.triangles[2] = [8, 9, 6];
    m.triangles[3] = [8, 6, 7];
    m
}

fn volume(m: &Mesh) -> f64 {
    let all: Vec<u32> = (0..m.triangles.len() as u32).collect();
    if all.is_empty() {
        0.0
    } else {
        shell_volume(m, &all)
    }
}

fn fix(m: &mut Mesh) -> RepairReport {
    repair(m, &RepairOptions::default(), |_, _| {}).unwrap()
}

fn assert_good_cube(m: &Mesh, r: &RepairReport, vol: f64) {
    assert!(r.after.is_closed(), "{:?}", r.after);
    assert!(r.after.is_oriented(), "{:?}", r.after);
    assert_eq!(r.after.shells, 1);
    assert!((volume(m) - vol).abs() < 1e-6 * vol.max(1.0), "volume {}", volume(m));
}

// ---- one step at a time -----------------------------------------------

#[test]
fn stitch_merges_soup() {
    let mut m = soup(&cube(10.0, [0.0; 3]));
    assert_eq!(m.vertices.len(), 36);
    stitch(&mut m, 0.01);
    assert_eq!(m.vertices.len(), 8);
    assert!(mesh_info(&m).is_closed());
}

#[test]
fn stitch_obeys_tolerance() {
    let mut m = seam(0.009, 10.0);
    stitch(&mut m, 0.01);
    assert_eq!(m.vertices.len(), 8);
    let mut m = seam(0.011, 10.0);
    stitch(&mut m, 0.01);
    assert_eq!(m.vertices.len(), 10);
}

#[test]
fn stitch_splits_t_junction() {
    // The top face uses a midpoint on edge 4-5. The front face does not.
    let mut m = cube(10.0, [0.0; 3]);
    m.vertices.push([5.0, 0.0, 10.0]);
    m.triangles[2] = [4, 8, 6];
    m.triangles.push([8, 5, 6]);
    assert_eq!(mesh_info(&m).boundary_edges, 3);
    let n = stitch(&mut m, 0.01);
    assert!(n >= 1);
    assert!(mesh_info(&m).is_closed());
}

#[test]
fn trivial_hole_gets_one_triangle() {
    let mut m = cube(10.0, [0.0; 3]);
    m.triangles.remove(2);
    assert_eq!(close_trivial_holes(&mut m), 1);
    let i = mesh_info(&m);
    assert!(i.is_closed() && i.is_oriented());
    assert!((volume(&m) - 1000.0).abs() < 1e-9);
}

#[test]
fn double_triangles_go() {
    let mut m = cube(10.0, [0.0; 3]);
    let t0 = m.triangles[0];
    m.triangles.push(t0);
    m.triangles.push([t0[0], t0[2], t0[1]]);
    assert_eq!(remove_double_triangles(&mut m, 1e-4), 2);
    assert_eq!(m.triangles.len(), 12);
    assert!(mesh_info(&m).is_closed());
}

#[test]
fn flipped_face_turns_back() {
    let mut m = cube(10.0, [0.0; 3]);
    m.triangles[6].swap(1, 2);
    assert_eq!(mesh_info(&m).flipped_edges, 3);
    assert_eq!(fix_flipped(&mut m), 1);
    assert!(mesh_info(&m).is_oriented());
    assert!((volume(&m) - 1000.0).abs() < 1e-9);
}

#[test]
fn all_holes_close_flat_and_curved() {
    // Square hole of 4 edges.
    let mut m = cube(10.0, [0.0; 3]);
    m.triangles.drain(2..4);
    assert_eq!(close_all_holes(&mut m), 1);
    assert!(mesh_info(&m).is_closed() && mesh_info(&m).is_oriented());
    assert!((volume(&m) - 1000.0).abs() < 1e-9);
    // Open cylinder with 2 holes of 24 edges.
    let n = 24;
    let mut v = Vec::new();
    for z in [0.0, 10.0] {
        for i in 0..n {
            let a = i as f64 * std::f64::consts::TAU / n as f64;
            v.push([5.0 * a.cos(), 5.0 * a.sin(), z]);
        }
    }
    let mut t = Vec::new();
    for i in 0..n as u32 {
        let j = (i + 1) % n as u32;
        t.push([i, j, n as u32 + j]);
        t.push([i, n as u32 + j, n as u32 + i]);
    }
    let mut c = Mesh::new(v, t);
    assert_eq!(close_all_holes(&mut c), 2);
    assert!(mesh_info(&c).is_closed() && mesh_info(&c).is_oriented());
    let ngon = 0.5 * n as f64 * 25.0 * (std::f64::consts::TAU / n as f64).sin();
    assert!((volume(&c) - ngon * 10.0).abs() < 1e-6);
}

#[test]
fn concave_flat_hole_has_no_overlap() {
    // An L-shaped flat cap. Its rim is one concave hole: the fill must have
    // the area of the L (3), with no triangle outside the L.
    let v = vec![[0., 0., 0.], [2., 0., 0.], [2., 1., 0.], [1., 1., 0.], [1., 2., 0.], [0., 2., 0.]];
    let mut cap = Mesh::new(v, vec![[0, 1, 2], [0, 2, 3], [0, 3, 4], [0, 4, 5]]);
    assert_eq!(close_all_holes(&mut cap), 1);
    let area: f64 = cap.triangles[4..]
        .iter()
        .map(|t| tri_area(cap.vertices[t[0] as usize], cap.vertices[t[1] as usize], cap.vertices[t[2] as usize]))
        .sum();
    assert!((area - 3.0).abs() < 1e-9, "fill area {area}");
    // The fill must not use a diagonal of the cap (that edge would get 4 faces).
    let i = mesh_info(&cap);
    assert!(i.is_closed() && i.is_oriented(), "{i:?}");
}

#[test]
fn wrap_turns_inverted_and_drops_nested() {
    let mut m = cube(10.0, [0.0; 3]);
    for t in &mut m.triangles {
        t.swap(1, 2);
    }
    let mut m = join(&m, &cube(4.0, [3.0, 3.0, 3.0]));
    assert_eq!(wrap(&mut m), 1);
    assert_eq!(m.triangles.len(), 12);
    assert!((volume(&m) - 1000.0).abs() < 1e-9);
}

#[test]
fn degenerate_face_goes() {
    let mut m = cube(10.0, [0.0; 3]);
    m.vertices.push([5.0, 0.0, 10.0]);
    m.triangles[2] = [4, 8, 6];
    m.triangles.push([8, 5, 6]);
    m.triangles.push([8, 4, 5]);
    assert!(mesh_info(&m).is_closed());
    assert!(remove_degenerated_faces(&mut m, 0.002) >= 1);
    let i = mesh_info(&m);
    assert!(i.is_closed() && i.is_oriented(), "{i:?}");
    for f in 0..m.triangles.len() {
        let [a, b, c] = m.tri_pos(f);
        assert!(tri_area(a, b, c) > 1e-9);
    }
}

#[test]
fn tiny_shell_threshold() {
    for (s, gone) in [(0.05, true), (0.08, true), (0.12, false), (0.3, false)] {
        let mut m = join(&cube(10.0, [0.0; 3]), &cube(s, [20.0, 0.0, 0.0]));
        assert_eq!(remove_tiny_shells(&mut m, 0.001) == 1, gone, "size {s}");
    }
}

#[test]
fn normal_scale_range() {
    assert_eq!(normal_scale(1.0), 5.0);
    assert_eq!(normal_scale(10.0), 1.0);
    assert_eq!(normal_scale(1000.0), 0.4);
}

// ---- the full repair --------------------------------------------------

#[test]
fn full_repair_cases() {
    let c = cube(10.0, [0.0; 3]);
    let mut list: Vec<(&str, Mesh, f64)> = vec![("cube", c.clone(), 1000.0), ("soup", soup(&c), 1000.0)];
    let mut m = c.clone();
    m.triangles.remove(2);
    list.push(("hole_tri", m, 1000.0));
    let mut m = c.clone();
    m.triangles.drain(2..4);
    list.push(("hole_quad", m, 1000.0));
    let mut m = c.clone();
    m.triangles[6].swap(1, 2);
    list.push(("flip_one", m, 1000.0));
    let mut m = c.clone();
    for t in &mut m.triangles {
        t.swap(1, 2);
    }
    list.push(("inverted", m, 1000.0));
    let mut m = c.clone();
    m.triangles.push(c.triangles[0]);
    list.push(("dup_tri", m, 1000.0));
    list.push(("nested", join(&c, &cube(4.0, [3.0, 3.0, 3.0])), 1000.0));
    list.push(("tiny", join(&c, &cube(0.05, [20.0, 0.0, 0.0])), 1000.0));
    for (name, mut m, vol) in list {
        let r = fix(&mut m);
        assert_good_cube(&m, &r, vol);
        assert_eq!(m.vertices.len(), 8, "{name}");
        assert_eq!(m.triangles.len(), 12, "{name}");
    }
}

#[test]
fn full_repair_seams_follow_scale() {
    // Normalized tolerance 0.01: 10 mm cube → 0.01 mm, 1000 mm → 0.025 mm,
    // 1 mm → 0.002 mm. Below it: stitch (8 vertices). Above it: fill (10).
    for (gap, size, stitched) in [
        (0.009, 10.0, true), (0.011, 10.0, false),
        (0.02, 1000.0, true), (0.03, 1000.0, false),
        (0.0015, 1.0, true), (0.003, 1.0, false),
    ] {
        let mut m = seam(gap, size);
        let r = fix(&mut m);
        assert!(r.after.is_closed() && r.after.is_oriented(), "{gap} {size}");
        assert_eq!(m.vertices.len() == 8, stitched, "gap {gap} size {size}: {}", m.vertices.len());
    }
}

#[test]
fn flat_plate_becomes_nothing() {
    // Windows gives an empty model for a flat plate: the fill makes a closed
    // shell with no volume, and RemoveTinyShells removes it.
    let mut m = Mesh::new(vec![[0., 0., 0.], [10., 0., 0.], [10., 10., 0.], [0., 10., 0.]], vec![[0, 1, 2], [0, 2, 3]]);
    fix(&mut m);
    assert!(m.triangles.is_empty());
}

#[test]
fn shells_and_empty() {
    let mut m = Mesh::new(vec![[0.0; 3]; 3], vec![]);
    assert!(repair(&mut m, &RepairOptions::default(), |_, _| {}).is_err());
    let mut m = join(&cube(10.0, [0.0; 3]), &cube(10.0, [30.0, 0.0, 0.0]));
    let r = fix(&mut m);
    assert_eq!(r.after.shells, 2);
    let ef = m.edge_faces();
    assert_eq!(components(&m, &ef).len(), 2);
}

// ---- union, non-manifold split, large concave hole ----------------------

/// Cube turned by az about z and ax about x around its centre, then moved.
fn turned(s: f64, shift: Vec3, az: f64, ax: f64) -> Mesh {
    let mut m = cube(s, [0.0; 3]);
    let (a, b) = (az.to_radians(), ax.to_radians());
    let c = [s / 2.0; 3];
    for p in &mut m.vertices {
        let q = sub(*p, c);
        let q = [q[0] * a.cos() - q[1] * a.sin(), q[0] * a.sin() + q[1] * a.cos(), q[2]];
        let q = [q[0], q[1] * b.cos() - q[2] * b.sin(), q[1] * b.sin() + q[2] * b.cos()];
        *p = add(add(q, c), shift);
    }
    m
}

/// Inside test for a convex closed mesh with outward faces.
fn in_convex(m: &Mesh, p: Vec3) -> bool {
    (0..m.triangles.len()).all(|f| {
        let [a, b, c] = m.tri_pos(f);
        dot(cross(sub(b, a), sub(c, a)), sub(p, a)) <= 0.0
    })
}

/// Volume of the union of convex meshes, Monte Carlo with a fixed seed.
fn union_volume_mc(parts: &[Mesh], lo: Vec3, hi: Vec3, n: usize) -> f64 {
    let mut hit = 0usize;
    let mut seed = 12345u64;
    let mut rnd = || {
        seed = seed.wrapping_mul(6364136223846793005).wrapping_add(1442695040888963407);
        (seed >> 11) as f64 / (1u64 << 53) as f64
    };
    for _ in 0..n {
        let p = [lo[0] + rnd() * (hi[0] - lo[0]), lo[1] + rnd() * (hi[1] - lo[1]), lo[2] + rnd() * (hi[2] - lo[2])];
        if parts.iter().any(|m| in_convex(m, p)) {
            hit += 1;
        }
    }
    hit as f64 / n as f64 * (hi[0] - lo[0]) * (hi[1] - lo[1]) * (hi[2] - lo[2])
}

fn assert_one_closed_body(m: &Mesh, r: &RepairReport) {
    assert!(r.after.is_closed(), "{:?}", r.after);
    assert!(r.after.is_oriented(), "{:?}", r.after);
    assert_eq!(r.after.shells, 1, "{:?}", r.after);
    assert!(volume(m) > 0.0);
}

#[test]
fn union_axis_aligned_cubes() {
    // Many exact coincidences: an edge of one cube goes through the face
    // diagonal of the other. The small move of step 1 removes them.
    let mut m = join(&cube(10.0, [0.0; 3]), &cube(10.0, [5.0, 5.0, 5.0]));
    let r = fix(&mut m);
    assert_one_closed_body(&m, &r);
    assert!((volume(&m) - 1875.0).abs() < 1e-3, "volume {}", volume(&m));
}

#[test]
fn union_turned_cube() {
    let a = cube(10.0, [0.0; 3]);
    let b = turned(10.0, [6.0, 4.0, 3.0], 30.0, 20.0);
    let want = union_volume_mc(&[a.clone(), b.clone()], [-5.0; 3], [25.0; 3], 400_000);
    let mut m = join(&a, &b);
    let r = fix(&mut m);
    assert_one_closed_body(&m, &r);
    assert!((volume(&m) - want).abs() < 0.01 * want, "volume {} want {want}", volume(&m));
}

#[test]
fn union_three_bodies() {
    let a = cube(10.0, [0.0; 3]);
    let b = cube(10.0, [5.0, 5.0, 5.0]);
    let c = turned(8.0, [2.0, -4.0, 4.0], 25.0, 35.0);
    let want = union_volume_mc(&[a.clone(), b.clone(), c.clone()], [-6.0; 3], [20.0; 3], 400_000);
    let mut m = join(&join(&a, &b), &c);
    let r = fix(&mut m);
    assert_one_closed_body(&m, &r);
    assert!((volume(&m) - want).abs() < 0.01 * want, "volume {} want {want}", volume(&m));
}

#[test]
fn union_report_counts() {
    let mut m = join(&cube(10.0, [0.0; 3]), &turned(10.0, [6.0, 4.0, 3.0], 30.0, 20.0));
    let rep = crate::union::union_shells(&mut m);
    assert!(rep.pairs > 0 && rep.cut_faces > 0 && rep.removed_patches > 0, "{rep:?}");
    assert_eq!(rep.failed_faces, 0, "{rep:?}");
}

#[test]
fn shared_edge_is_split() {
    // Two cubes on the edge x = y = 10, welded: the edge has 4 faces.
    // Cube B vertices 0 and 4 are (10, 10, 0) and (10, 10, 10).
    let mut m = join(&cube(10.0, [0.0; 3]), &cube(10.0, [10.0, 10.0, 0.0]));
    for t in &mut m.triangles {
        for v in t.iter_mut() {
            if *v == 8 {
                *v = 2;
            } else if *v == 12 {
                *v = 6;
            }
        }
    }
    m.compact();
    assert_eq!(mesh_info(&m).non_manifold_edges, 1);
    let r = fix(&mut m);
    assert!(r.after.is_closed() && r.after.is_oriented(), "{:?}", r.after);
    assert_eq!(r.after.non_manifold_edges, 0);
    assert_eq!(r.after.shells, 2);
    assert!((volume(&m) - 2000.0).abs() < 1e-9);
}

/// Largest number of face fans around one vertex (1 = manifold vertex).
fn max_fans(m: &Mesh) -> usize {
    let ef = m.edge_faces();
    let mut best = 0;
    for v in 0..m.vertices.len() as u32 {
        let fs: Vec<u32> = (0..m.triangles.len() as u32).filter(|&f| m.triangles[f as usize].contains(&v)).collect();
        let mut seen = vec![false; fs.len()];
        let mut fans = 0;
        for s in 0..fs.len() {
            if seen[s] {
                continue;
            }
            fans += 1;
            let mut st = vec![s];
            seen[s] = true;
            while let Some(i) = st.pop() {
                let t = m.triangles[fs[i] as usize];
                for &w in t.iter().filter(|&&w| w != v) {
                    for &g in &ef[&crate::mesh::ekey(v, w)] {
                        if let Some(j) = fs.iter().position(|&x| x == g) {
                            if !seen[j] {
                                seen[j] = true;
                                st.push(j);
                            }
                        }
                    }
                }
            }
        }
        best = best.max(fans);
    }
    best
}

#[test]
fn shared_corner_is_split() {
    // Cube B vertex 0 (10, 10, 10) = cube A vertex 6.
    let mut m = join(&cube(10.0, [0.0; 3]), &cube(10.0, [10.0, 10.0, 10.0]));
    for t in &mut m.triangles {
        for v in t.iter_mut() {
            if *v == 8 {
                *v = 6;
            }
        }
    }
    m.compact();
    assert_eq!(max_fans(&m), 2);
    let r = fix(&mut m);
    assert!(r.after.is_closed() && r.after.is_oriented(), "{:?}", r.after);
    assert_eq!(r.after.shells, 2);
    assert_eq!(max_fans(&m), 1);
}

#[test]
fn large_concave_hole_has_no_overlap() {
    // C-shaped prism, open at both ends: two holes of 320 vertices. The
    // centroid of the C is outside the C, so a fan would overlap itself.
    let n = 160;
    let mut ring = Vec::new();
    for i in 0..n {
        let a = (30.0 + 300.0 * i as f64 / (n - 1) as f64).to_radians();
        ring.push([5.0 * a.cos(), 5.0 * a.sin()]);
    }
    for i in (0..n).rev() {
        let a = (30.0 + 300.0 * i as f64 / (n - 1) as f64).to_radians();
        ring.push([3.0 * a.cos(), 3.0 * a.sin()]);
    }
    let k = ring.len() as u32;
    let mut v: Vec<Vec3> = ring.iter().map(|p| [p[0], p[1], 0.0]).collect();
    v.extend(ring.iter().map(|p| [p[0], p[1], 10.0]));
    let mut t = Vec::new();
    for i in 0..k {
        let j = (i + 1) % k;
        t.push([i, j, k + j]);
        t.push([i, k + j, k + i]);
    }
    let mut m = Mesh::new(v, t);
    let side = m.triangles.len();
    let r = fix(&mut m);
    assert!(r.after.is_closed() && r.after.is_oriented(), "{:?}", r.after);
    // Each cap face must face out: -z at the bottom, +z at the top.
    let mut caps = 0;
    for f in 0..m.triangles.len() {
        let [a, b, c] = m.tri_pos(f);
        if (a[2] - b[2]).abs() > 1e-9 || (a[2] - c[2]).abs() > 1e-9 {
            continue;
        }
        caps += 1;
        let nz = cross(sub(b, a), sub(c, a))[2];
        assert!(if a[2] < 5.0 { nz < 0.0 } else { nz > 0.0 }, "face {f} folds over");
    }
    assert_eq!(caps, m.triangles.len() - side);
    let ngon: f64 = (0..ring.len())
        .map(|i| {
            let (p, q) = (ring[i], ring[(i + 1) % ring.len()]);
            p[0] * q[1] - q[0] * p[1]
        })
        .sum::<f64>()
        / 2.0;
    assert!((volume(&m) - ngon * 10.0).abs() < 1e-6 * ngon * 10.0);
}

#[test]
fn cdt_rejects_self_crossing_rim() {
    // A bow-tie rim cannot be a simple polygon in any plane.
    let v = vec![[0., 0., 0.], [1., 1., 0.], [1., 0., 0.], [0., 1., 0.]];
    assert!(triangulate_cdt(&v, &[0, 1, 2, 3]).is_none());
}
