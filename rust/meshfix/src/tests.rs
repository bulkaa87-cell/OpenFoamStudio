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
fn winding_union_of_glued_and_crossing_cubes() {
    // Two cubes glued on the plane x = 10 (caps with other triangles), and
    // a third cube that crosses both. The union is one closed shell.
    let a = cube(10.0, [0.0; 3]);
    let mut b = cube(10.0, [10.0, 0.0, 0.0]);
    // Other triangulation of the glued cap of b (x = 10: vertices 0, 3, 7, 4).
    for t in &mut b.triangles {
        if *t == [0, 4, 7] {
            *t = [0, 4, 3];
        } else if *t == [0, 7, 3] {
            *t = [4, 7, 3];
        }
    }
    let c = cube(6.0, [7.0, 2.0, 7.0]);
    let mut m = join(&join(&a, &b), &c);
    let before = volume(&m);
    let (_, rep) = wrap_winding(&mut m);
    assert!(!rep.panicked, "{rep:?}");
    let i = mesh_info(&m);
    assert!(i.is_closed() && i.is_oriented(), "{i:?} {rep:?}");
    assert_eq!(i.shells, 1, "{i:?}");
    // 2000 for the two cubes plus the part of c above z = 10 (6 x 6 x 3).
    let v = volume(&m);
    assert!((v - 2108.0).abs() < 1e-3, "volume {v} (input {before})");
}

#[test]
fn winding_open_part_inside_closed_part() {
    // An open box (no top) fully inside a closed cube: the union is the cube.
    let mut inner = cube(4.0, [3.0, 3.0, 3.0]);
    inner.triangles.retain(|t| *t != [4, 5, 6] && *t != [4, 6, 7]);
    let mut m = join(&cube(10.0, [0.0; 3]), &inner);
    wrap_winding(&mut m);
    let i = mesh_info(&m);
    assert!(i.is_closed() && i.is_oriented(), "{i:?}");
    assert!((volume(&m) - 1000.0).abs() < 1e-6);
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
    // StlSafe moves the two copies of the edge apart by some float32 steps.
    assert!((volume(&m) - 2000.0).abs() < 1e-3, "volume {}", volume(&m));
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

// ---- regressions from a real aircraft model (251k faces) ----

/// Two cubes on the edge x = y = 10, each with its own vertices at the same
/// positions (a clean, closed input). An exact weld of inner vertices joined
/// them into a non-manifold edge, and the split then opened holes.
#[test]
fn touching_copies_stay_clean() {
    let mut m = join(&cube(10.0, [0.0; 3]), &cube(10.0, [10.0, 10.0, 0.0]));
    assert_eq!(m.defect_edges(), 0);
    let r = fix(&mut m);
    assert_eq!(m.defect_edges(), 0, "{:?}", r.after);
    assert_eq!(r.after.shells, 2);
    // StlSafe moves the two copies of the edge apart by some float32 steps.
    assert!((volume(&m) - 2000.0).abs() < 1e-3, "volume {}", volume(&m));
}

/// A cube with a flap: one more face on a cube edge (3 faces on the edge).
/// The split cannot pair the flap, so the edge must stay as it is, and the
/// repair must not give more open or non-manifold edges than the input.
#[test]
fn fin_edge_does_not_open() {
    let mut m = cube(10.0, [0.0; 3]);
    m.vertices.push([10.0, -5.0, 5.0]);
    m.triangles.push([5, 1, 8]);
    let d0 = m.defect_edges();
    assert!(d0 > 0);
    fix(&mut m);
    assert!(m.defect_edges() <= d0, "{} > {d0}", m.defect_edges());
}

/// Slivers next to a closed edge: a zero-area cap on the top front edge and
/// a needle at a corner. After the repair: no face below the zero-area limit
/// of the program's check, and the cube stays closed and outward.
#[test]
fn slivers_go_and_cube_stays_closed() {
    let mut m = cube(10.0, [0.0; 3]);
    // cap: vertex 8 on edge 4-5, faces [4,8,6], [8,5,6] and the cap [8,4,5]
    m.vertices.push([5.0, 0.0, 10.0]);
    m.triangles[2] = [4, 8, 6];
    m.triangles.push([8, 5, 6]);
    m.triangles.push([8, 4, 5]);
    // needle: vertex 9 at 1e-9 from corner 6 inside face [4,6,7]
    m.vertices.push([10.0 - 1e-9, 10.0, 10.0]);
    let i = m.triangles.iter().position(|t| *t == [4, 6, 7]).unwrap();
    m.triangles[i] = [4, 9, 7];
    m.triangles.push([4, 6, 9]);
    m.triangles.push([9, 6, 7]);
    assert!(mesh_info(&m).is_closed());
    let r = fix(&mut m);
    assert!(r.after.is_closed() && r.after.is_oriented(), "{:?}", r.after);
    let limit = zero_area_limit(&m);
    for f in 0..m.triangles.len() {
        let [a, b, c] = m.tri_pos(f);
        assert!(tri_area(a, b, c) >= limit, "face {f} area {}", tri_area(a, b, c));
    }
    assert!((volume(&m) - 1000.0).abs() < 1e-6, "volume {} {:?} {:?}", volume(&m), m.vertices, r.stages);
}

// ---------------------------------------------------------------------------
// Alpha wrap (alphawrap.rs, delaunay.rs, aabb.rs)

/// Random points: each cell is positive, the links agree, and no point is
/// inside the circumsphere of a cell (exact signs).
#[test]
fn delaunay_cells_positive_and_empty() {
    use crate::delaunay::*;
    let mut d = Delaunay::new([0.0; 3], [10.0; 3]);
    let mut s: u64 = 12345;
    let mut rnd = || {
        s ^= s << 13;
        s ^= s >> 7;
        s ^= s << 17;
        (s % 1_000_000) as f64 / 1e5
    };
    for _ in 0..400 {
        let p = [rnd(), rnd(), rnd()];
        d.insert(p, NONE);
    }
    // Points on a grid: many on one plane and on one sphere.
    for x in 0..4 {
        for y in 0..4 {
            d.insert([x as f64 * 2.0, y as f64 * 2.0, 5.0], NONE);
        }
    }
    let n = d.pts.len();
    let c3 = |p: Vec3| robust::Coord3D { x: p[0], y: p[1], z: p[2] };
    for (ci, c) in d.cells.iter().enumerate() {
        if !c.alive {
            continue;
        }
        let q = d.cell_pts(ci as u32);
        assert!(orient(q[0], q[1], q[2], q[3]) > 0.0, "cell {ci} not positive");
        for i in 0..4 {
            let nb = c.n[i];
            if nb != NONE {
                assert!(d.cells[nb as usize].alive);
                assert!(d.cells[nb as usize].n.contains(&(ci as u32)));
            }
        }
        if c.infinite() {
            continue;
        }
        for v in N_SUPER as usize..n {
            let r = robust::insphere(c3(q[0]), c3(q[1]), c3(q[2]), c3(q[3]), c3(d.pts[v]));
            assert!(r <= 0.0, "point {v} inside the sphere of cell {ci}");
        }
    }
}

fn wrap_opts(alpha: f64) -> crate::alphawrap::WrapOptions {
    crate::alphawrap::WrapOptions {
        alpha,
        offset: alpha / 30.0,
        max_points: 2_000_000,
        shrink: true,
        gap: alpha / 300.0,
        refine_passes: 3,
        refine_tol: alpha / 30.0,
    }
}

fn assert_wrap_clean(m: &Mesh) {
    let i = mesh_info(m);
    assert!(i.is_closed() && i.is_oriented(), "{i:?}");
    assert!(is_clean(m));
}

/// A cube soup: the wrap is closed and outward, and after the shrink its
/// volume is near the cube volume.
#[test]
fn alpha_wrap_cube_soup() {
    let src = soup(&cube(10.0, [0.0; 3]));
    let (w, r) = crate::alphawrap::wrap(&src, &wrap_opts(1.0));
    let w = w.expect("wrap");
    assert_wrap_clean(&w);
    assert!(!r.stopped);
    let v = volume(&w);
    assert!((v - 1000.0).abs() < 10.0, "volume {v}");
    // The wrap encloses the input: no wrap vertex is inside the cube.
    for p in &w.vertices {
        let inside = (0..3).map(|k| p[k].min(10.0 - p[k])).fold(f64::MAX, f64::min);
        assert!(inside < 1e-9, "vertex {p:?} inside");
    }
}

/// A flap on a cube, and a cube with an open side: the wrap gives one
/// closed, outward body each time.
#[test]
fn alpha_wrap_open_and_nonmanifold_inputs() {
    let mut fin = cube(10.0, [0.0; 3]);
    fin.vertices.push([10.0, -5.0, 5.0]);
    fin.triangles.push([5, 1, 8]);
    let (w, _) = crate::alphawrap::wrap(&fin, &wrap_opts(0.5));
    let w = w.expect("wrap");
    assert_wrap_clean(&w);
    assert_eq!(mesh_info(&w).shells, 1);
    let v = volume(&w);
    assert!(v > 1000.0 && v < 1010.0, "volume {v}");

    let mut open = cube(10.0, [0.0; 3]);
    open.triangles.drain(2..4);
    let (w, _) = crate::alphawrap::wrap(&open, &wrap_opts(0.5));
    assert_wrap_clean(&w.expect("wrap"));
}

/// Two crossing cubes and a cube in a cube: one closed body each, the
/// inner faces are gone.
#[test]
fn alpha_wrap_union_and_nested() {
    let m = join(&cube(10.0, [0.0; 3]), &cube(10.0, [5.0, 5.0, 5.0]));
    let (w, _) = crate::alphawrap::wrap(&m, &wrap_opts(1.0));
    let w = w.expect("wrap");
    assert_wrap_clean(&w);
    assert_eq!(mesh_info(&w).shells, 1);
    let v = volume(&w);
    assert!((v - 1875.0).abs() < 25.0, "volume {v}");
    let m = join(&cube(10.0, [0.0; 3]), &cube(4.0, [3.0, 3.0, 3.0]));
    let (w, _) = crate::alphawrap::wrap(&m, &wrap_opts(1.0));
    let w = w.expect("wrap");
    assert_wrap_clean(&w);
    assert_eq!(mesh_info(&w).shells, 1);
}

/// The full repair uses the wrap only when the 12 steps leave defects: a
/// clean cube stays as it is, an open sheet through a cube comes back
/// closed.
#[test]
fn repair_uses_alpha_wrap_only_for_defects() {
    let mut m = cube(10.0, [0.0; 3]);
    let r = fix(&mut m);
    assert!(r.alpha_wrap.is_none() && r.fallback.is_none());
    assert_eq!(m.triangles.len(), 12);
    let mut m = cube(10.0, [0.0; 3]);
    let b = m.vertices.len() as u32;
    m.vertices.extend_from_slice(&[[-5.0, 5.0, -5.0], [15.0, 5.0, -5.0], [15.0, 5.0, 15.0], [-5.0, 5.0, 15.0]]);
    m.triangles.push([b, b + 1, b + 2]);
    m.triangles.push([b, b + 2, b + 3]);
    let r = fix(&mut m);
    assert!(r.after.is_closed() && r.after.is_oriented(), "{:?}", r.after);
    assert!(is_clean(&m));
}

/// The forced methods of the «Полагодити» menu: each runs on a clean cube
/// too (the pipeline would not run a fallback there), and each gives a
/// clean, closed result with about the volume of the cube.
#[test]
fn forced_alpha_wrap_on_a_clean_cube() {
    forced_method_on_a_clean_cube(Method::AlphaWrap);
}

#[test]
fn forced_volume_on_a_clean_cube() {
    forced_method_on_a_clean_cube(Method::Volume);
}

#[test]
fn method_names() {
    assert_eq!(Method::from_name("meshfix"), Some(Method::Pipeline));
    assert_eq!(Method::from_name("volume").map(Method::name), Some("volume"));
    assert_eq!(Method::from_name("x"), None);
}

fn forced_method_on_a_clean_cube(method: Method) {
    {
        let mut m = cube(10.0, [0.0; 3]);
        let o = RepairOptions { method, volume_cells: Some(96), ..Default::default() };
        let r = repair(&mut m, &o, |_, _| {}).unwrap();
        let want = if method == Method::AlphaWrap { Stage::AlphaWrap } else { Stage::VolumeRemesh };
        assert_eq!(r.fallback, Some(want), "{method:?}");
        assert!(is_clean(&m), "{method:?}");
        // The QEM decimation of the remesh gives the 12 cube faces again.
        assert!(m.triangles.len() >= 12, "{method:?}");
        let v = volume(&m);
        assert!((v - 1000.0).abs() < 60.0, "{method:?} volume {v}");
        // The forced wrap does not run the 12 steps; the forced remesh
        // runs the steps up to CloseAllHoles.
        let steps: Vec<Stage> = r.stages.iter().map(|s| s.stage).filter(|&s| s != Stage::StlSafe).collect();
        if method == Method::AlphaWrap {
            assert_eq!(steps, vec![Stage::AlphaWrap]);
        } else {
            assert!(steps.contains(&Stage::CloseAllHoles) && !steps.contains(&Stage::Wrap), "{steps:?}");
        }
    }
}

// ---- volumetric remesh (volume.rs) ------------------------------------

fn turned_box(s: f64, shift: Vec3, az: f64) -> Mesh {
    let mut m = cube(s, [-s / 2.0; 3]);
    let (c, d) = (az.to_radians().cos(), az.to_radians().sin());
    for p in &mut m.vertices {
        *p = add([c * p[0] - d * p[1], d * p[0] + c * p[1], p[2]], shift);
    }
    m
}

#[test]
fn volume_remesh_gives_closed_union() {
    // Two overlapping boxes, one inverted, and a small open slit in the
    // other: the remesh ignores the orientation, and a gap below 2·delta
    // does not leak.
    let mut a = turned_box(10.0, [0.0; 3], 20.0);
    let mut b = turned_box(10.0, [5.0, 3.0, 4.0], -15.0);
    for t in &mut b.triangles {
        t.swap(1, 2);
    }
    // Slit: vertex 6 of box a gets a copy that face 4-6-7 uses, moved by
    // 1e-3 (h = 22/256 here, delta = 0.05 h = 4e-3).
    let p6 = a.vertices[6];
    a.vertices.push(add(p6, [1e-3, 0.0, 0.0]));
    let i = a.triangles.iter().position(|t| *t == [4, 6, 7]).unwrap();
    a.triangles[i] = [4, 8, 7];
    let m = join(&a, &b);
    let o = volume::VolumeOptions { cells: 256, ..Default::default() };
    let (mut r, rep) = volume::remesh(&m, &o).expect("remesh");
    volume::fix_zero_area(&mut r);
    assert!(is_clean(&r), "{rep:?}");
    assert_eq!(mesh_info(&r).shells, 1);
    // Union volume by sampling: the remesh is close to it.
    let inside = |p: Vec3, sh: Vec3, az: f64| {
        let (c, d) = (az.to_radians().cos(), az.to_radians().sin());
        let q = sub(p, sh);
        let (x, y) = (c * q[0] + d * q[1], -d * q[0] + c * q[1]);
        x.abs() <= 5.0 && y.abs() <= 5.0 && q[2].abs() <= 5.0
    };
    let n = 60;
    let mut hit = 0;
    for i in 0..n {
        for j in 0..n {
            for k in 0..n {
                let f = |x: usize| -9.0 + 22.0 * (x as f64 + 0.5) / n as f64;
                let p = [f(i), f(j), f(k)];
                if inside(p, [0.0; 3], 20.0) || inside(p, [5.0, 3.0, 4.0], -15.0) {
                    hit += 1;
                }
            }
        }
    }
    let want = hit as f64 / (n * n * n) as f64 * 22f64.powi(3);
    let got = volume(&r);
    assert!((got - want).abs() < 0.03 * want, "volume {got} want {want}");
    // Flat faces stay flat: the decimation removes most grid vertices.
    assert!(r.triangles.len() < 2000, "decimation: {} faces", r.triangles.len());
}

#[test]
fn volume_remesh_keeps_sharp_box() {
    // The decimation moves the grid vertices onto the edges and corners of
    // the input: a box comes back with its volume and few faces.
    for az in [0.0, 20.0] {
        let one = turned_box(10.0, [0.0; 3], az);
        let o = volume::VolumeOptions { cells: 128, ..Default::default() };
        let (r, _) = volume::remesh(&one, &o).unwrap();
        assert!(is_clean(&r));
        assert!((volume(&r) - 1000.0).abs() < 0.05, "az {az}: volume {}", volume(&r));
        assert!(r.triangles.len() < 200, "az {az}: {} faces", r.triangles.len());
    }
}

#[test]
fn volume_remesh_drops_sheets() {
    // A cube with a flap face on an edge, and a loose triangle through a
    // wall: the remesh gives the closed cube only (a sheet has no inside).
    let mut m = cube(10.0, [0.0; 3]);
    m.vertices.push([10.0, -5.0, 5.0]);
    m.triangles.push([5, 1, 8]);
    m.vertices.extend_from_slice(&[[3.0, 3.0, 5.0], [7.0, 4.0, 15.0], [6.0, 7.0, 5.0]]);
    m.triangles.push([9, 10, 11]);
    let o = volume::VolumeOptions { cells: 128, ..Default::default() };
    let (mut r, _) = volume::remesh(&m, &o).unwrap();
    volume::fix_zero_area(&mut r);
    assert!(is_clean(&r));
    assert_eq!(mesh_info(&r).shells, 1);
    assert!((volume(&r) - 1000.0).abs() < 0.05, "volume {}", volume(&r));
    // The full repair of the flap cube ends closed too.
    let mut f = cube(10.0, [0.0; 3]);
    f.vertices.push([10.0, -5.0, 5.0]);
    f.triangles.push([5, 1, 8]);
    fix(&mut f);
    assert!(is_clean(&f));
    assert!((volume(&f) - 1000.0).abs() < 1.0, "volume {}", volume(&f));
}

// ---- From the stitch approach (seal.rs, rims.rs)
/// Seal step: the flap gets its own edge, its fill makes a shell of no
/// volume, and RemoveTinyShells removes that shell. The cube stays closed.
#[test]
fn fin_is_sealed() {
    let mut m = cube(10.0, [0.0; 3]);
    m.vertices.push([10.0, -5.0, 5.0]);
    m.triangles.push([5, 1, 8]);
    let r = fix(&mut m);
    assert_eq!(m.defect_edges(), 0, "{:?}", r.after);
    assert_good_cube(&m, &r, 1000.0);
}

/// Seal step: a sheet glued to a closed cube along a closed seam (3 faces on
/// each seam edge), like a fairing on a fuselage. The result must be closed
/// and outward, with no face of zero area.
#[test]
fn glued_sheet_is_sealed() {
    // Cube 0..10, and a roof over the top face: a pyramid of 4 faces from
    // the top edges to an apex. The top face of the cube stays.
    let mut m = cube(10.0, [0.0; 3]);
    m.vertices.push([5.0, 5.0, 14.0]);
    for (a, b) in [(4u32, 5u32), (5, 6), (6, 7), (7, 4)] {
        m.triangles.push([a, b, 8]);
    }
    assert!(m.defect_edges() > 0);
    let r = fix(&mut m);
    assert_eq!(m.defect_edges(), 0, "{:?}", r.after);
    assert!(r.after.is_oriented(), "{:?}", r.after);
    let ef = m.edge_faces();
    for s in components(&m, &ef) {
        assert!(shell_volume(&m, &s) > 0.0);
    }
    let limit = zero_area_limit(&m);
    for f in 0..m.triangles.len() {
        let [a, b, c] = m.tri_pos(f);
        assert!(tri_area(a, b, c) >= limit, "face {f}");
    }
}

/// A square tube with a wall of 1 mm, closed at z = 0, open at z = 10. The
/// two rims of the open end get a strip (bridge_rims). Two disks would also
/// give a closed mesh, but they block the opening of the tube.
#[test]
fn open_wall_end_gets_a_strip() {
    let sq = |s0: f64, s1: f64, z: f64| vec![[s0, s0, z], [s1, s0, z], [s1, s1, z], [s0, s1, z]];
    let mut v = sq(0.0, 10.0, 0.0);
    v.extend(sq(0.0, 10.0, 10.0));
    v.extend(sq(1.0, 9.0, 0.0));
    v.extend(sq(1.0, 9.0, 10.0));
    let (o, oo, i, ii) = (0u32, 4u32, 8u32, 12u32);
    let mut t = Vec::new();
    let mut quad = |a: u32, b: u32, c: u32, d: u32| {
        t.push([a, b, c]);
        t.push([a, c, d]);
    };
    for k in 0..4u32 {
        let k1 = (k + 1) % 4;
        quad(o + k, o + k1, oo + k1, oo + k); // outer wall, normal out
        quad(i + k, ii + k, ii + k1, i + k1); // inner wall, normal to the axis
        quad(o + k, i + k, i + k1, o + k1); // closed end, normal -z
    }
    let mut m = Mesh::new(v, t);
    let r = fix(&mut m);
    assert_eq!(m.defect_edges(), 0, "{:?}", r.after);
    assert!(r.after.is_oriented());
    assert!((volume(&m) - (1000.0 - 640.0)).abs() < 1e-6, "volume {}", volume(&m));
    for f in 0..m.triangles.len() {
        let [a, b, c] = m.tri_pos(f);
        let g = scale(add(add(a, b), c), 1.0 / 3.0);
        let inside = g[0] > 1.0 + 1e-9 && g[0] < 9.0 - 1e-9 && g[1] > 1.0 + 1e-9 && g[1] < 9.0 - 1e-9;
        assert!(!(inside && (g[2] - 10.0).abs() < 1e-9), "a face across the opening: {g:?}");
    }
}


// ---- contacts in any plane (planar::overlay_contacts) --------------------

fn cuboid(lo: Vec3, hi: Vec3) -> Mesh {
    let mut m = cube(1.0, [0.0; 3]);
    for p in &mut m.vertices {
        for k in 0..3 {
            p[k] = lo[k] + p[k] * (hi[k] - lo[k]);
        }
    }
    m
}

/// Turn all vertices by az about z, then by ax about x (degrees).
fn rotate(m: &mut Mesh, az: f64, ax: f64) {
    let (a, b) = (az.to_radians(), ax.to_radians());
    for p in &mut m.vertices {
        let q = [p[0] * a.cos() - p[1] * a.sin(), p[0] * a.sin() + p[1] * a.cos(), p[2]];
        *p = [q[0], q[1] * b.cos() - q[2] * b.sin(), q[1] * b.sin() + q[2] * b.cos()];
    }
}

#[test]
fn contact_overlay_tilted_glued_boxes() {
    // Box b sits on box a with a gap of 2e-5 and hangs over one side. All
    // turned, so that the contact is not in an axis plane.
    let a = cuboid([0.0; 3], [10.0; 3]);
    let b = cuboid([2.0, 3.0, 10.0 + 2e-5], [8.0, 12.0, 15.0]);
    let mut m = join(&a, &b);
    rotate(&mut m, 30.0, 20.0);
    let r = planar::overlay_contacts(&mut m);
    assert!(r.changed >= 1, "{r:?}");
    let i = mesh_info(&m);
    assert_eq!(m.defect_edges(), 0, "{i:?} {r:?}");
    assert!(i.is_oriented(), "{i:?}");
    assert_eq!(i.shells, 1, "{i:?}");
    let v = volume(&m);
    assert!((v - 1270.0).abs() < 1e-2, "volume {v}");
}

/// Closed box with an n x n grid on the top and the bottom, walls in strips.
/// The grid diagonals go one way when `flip` is false, the other way else.
fn grid_box(lo: Vec3, hi: Vec3, n: usize, flip: bool) -> Mesh {
    let mut v = Vec::new();
    for z in [lo[2], hi[2]] {
        for j in 0..=n {
            for i in 0..=n {
                v.push([lo[0] + (hi[0] - lo[0]) * i as f64 / n as f64, lo[1] + (hi[1] - lo[1]) * j as f64 / n as f64, z]);
            }
        }
    }
    let id = |l: usize, i: usize, j: usize| (l * (n + 1) * (n + 1) + j * (n + 1) + i) as u32;
    let mut t = Vec::new();
    for j in 0..n {
        for i in 0..n {
            let q = [id(1, i, j), id(1, i + 1, j), id(1, i + 1, j + 1), id(1, i, j + 1)];
            let tr = if flip { [[q[0], q[1], q[3]], [q[1], q[2], q[3]]] } else { [[q[0], q[1], q[2]], [q[0], q[2], q[3]]] };
            for f in tr {
                t.push(f); // top: normal +z
                t.push([f[0] - id(1, 0, 0), f[2] - id(1, 0, 0), f[1] - id(1, 0, 0)]); // bottom: -z
            }
        }
    }
    // Ring of the grid boundary, counterclockwise seen from +z.
    let mut ring = Vec::new();
    for i in 0..n {
        ring.push((i, 0));
    }
    for j in 0..n {
        ring.push((n, j));
    }
    for i in (1..=n).rev() {
        ring.push((i, n));
    }
    for j in (1..=n).rev() {
        ring.push((0, j));
    }
    for k in 0..ring.len() {
        let (a, b) = (ring[k], ring[(k + 1) % ring.len()]);
        let (b0, b1, t0, t1) = (id(0, a.0, a.1), id(0, b.0, b.1), id(1, a.0, a.1), id(1, b.0, b.1));
        t.push([b0, b1, t1]);
        t.push([b0, t1, t0]);
    }
    Mesh::new(v, t)
}

#[test]
fn contact_overlay_fine_caps_with_noise() {
    // Two grid boxes glued on caps with other grids, b turned by 0.3 deg in
    // the plane, its bottom vertices moved by up to 3e-5 off the plane, and
    // all turned out of the axis planes. After the overlay and the winding
    // Wrap the result is one closed body with the volume of both.
    let a = grid_box([0.0; 3], [10.0; 3], 7, false);
    let mut b = grid_box([2.0, 3.0, 10.0], [8.0, 12.0, 15.0], 5, true);
    let (s, c) = (0.3f64.to_radians().sin(), 0.3f64.to_radians().cos());
    let mut h = 7u64;
    for p in &mut b.vertices {
        let q = [p[0] - 5.0, p[1] - 7.5];
        *p = [5.0 + q[0] * c - q[1] * s, 7.5 + q[0] * s + q[1] * c, p[2]];
        if p[2] == 10.0 {
            h = h.wrapping_mul(6364136223846793005).wrapping_add(1442695040888963407);
            p[2] += 3e-5 * ((h >> 11) as f64 / (1u64 << 53) as f64);
        }
    }
    let mut m = join(&a, &b);
    rotate(&mut m, 25.0, 35.0);
    let mut o = m.clone();
    let r = planar::overlay_contacts(&mut o);
    assert!(r.changed >= 1, "{r:?}");
    if o.defect_edges() > 0 {
        for (&(x, y), fs) in &o.edge_faces() {
            if fs.len() != 2 {
                println!("edge {x}-{y} {:?} {:?}", o.vertices[x as usize], o.vertices[y as usize]);
                for &f in fs {
                    println!("   face {f} {:?} {:?}", o.triangles[f as usize], o.tri_pos(f as usize));
                }
            }
        }
    }
    assert_eq!(o.defect_edges(), 0, "{:?} {r:?}", mesh_info(&o));
    let (_, rep) = wrap_winding(&mut m);
    assert!(!rep.panicked);
    let i = mesh_info(&m);
    assert!(i.is_closed() && i.is_oriented(), "{i:?}");
    assert_eq!(i.shells, 1, "{i:?}");
    let v = volume(&m);
    assert!((v - 1270.0).abs() < 0.05, "volume {v}");
}

#[test]
fn flap_on_an_edge_goes() {
    // A closed cube with one more face on its edge 0-1 (a flap): the flap
    // goes, the cube stays closed.
    let mut m = cube(10.0, [0.0; 3]);
    m.vertices.push([5.0, -5.0, -5.0]);
    m.triangles.push([0, 1, 8]);
    assert!(m.defect_edges() > 0);
    assert_eq!(remove_flaps(&mut m), 1);
    assert_eq!(m.defect_edges(), 0);
    assert_eq!(m.triangles.len(), 12);
}

#[test]
fn seams_are_cut_out_and_filled() {
    // A box with a grid of 6 x 6 on the top: two top faces in the middle
    // go (a hole), and a flap hangs on one rim edge. The cut out and fill
    // closes it with the same volume.
    let mut m = grid_box([0.0; 3], [10.0; 3], 6, false);
    let mid: Vec<usize> = (0..m.triangles.len())
        .filter(|&f| {
            let [a, b, c] = m.tri_pos(f);
            let g = scale(add(add(a, b), c), 1.0 / 3.0);
            g[2] == 10.0 && g[0] > 5.0 - 1e-9 && g[0] < 5.0 + 10.0 / 6.0 && g[1] > 5.0 - 1e-9 && g[1] < 5.0 + 10.0 / 6.0
        })
        .collect();
    assert_eq!(mid.len(), 2);
    let keep: Vec<bool> = (0..m.triangles.len()).map(|f| !mid.contains(&f)).collect();
    m.retain_faces(&keep);
    let (a, b) = {
        let p = |x: Vec3| m.vertices.iter().position(|v| *v == x).unwrap() as u32;
        (p([5.0, 5.0, 10.0]), p([5.0 + 10.0 / 6.0, 5.0, 10.0]))
    };
    m.vertices.push([6.0, 4.0, 14.0]);
    let c = (m.vertices.len() - 1) as u32;
    m.triangles.push([a, b, c]);
    assert!(m.defect_edges() > 0);
    excise_seams_for_test(&mut m);
    let i = mesh_info(&m);
    assert!(i.is_closed() && i.is_oriented(), "{i:?}");
    assert!((volume(&m) - 1000.0).abs() < 1e-6, "volume {}", volume(&m));
}

// ---- StlSafe (the last step) -------------------------------------------

/// (clean, degenerate faces) of the mesh after a float32 STL round trip
/// at the scale factor k.
fn rt_clean(m: &Mesh, k: f64) -> (bool, usize) {
    let rt = crate::stlsafe::round_trip(m, k);
    let lim = zero_area_limit(&rt);
    let deg = (0..rt.triangles.len())
        .filter(|&f| {
            let [a, b, c] = rt.tri_pos(f);
            tri_area(a, b, c) < lim
        })
        .count();
    (is_clean(&rt), deg)
}

#[test]
fn stl_round_trip_of_shared_edge() {
    // Two cubes on one edge, far from the origin (a coarse float32 step).
    // The repair gives each cube its own copy of the edge; an STL reader
    // joins equal points, and the edge then has four faces.
    let mut m = join(&cube(10.0, [1000.0, 1000.0, 0.0]), &cube(10.0, [1010.0, 1010.0, 0.0]));
    for t in &mut m.triangles {
        for x in t.iter_mut() {
            if *x == 8 {
                *x = 2;
            } else if *x == 12 {
                *x = 6;
            }
        }
    }
    let mut no = m.clone();
    let o = RepairOptions { stl_safe: false, ..Default::default() };
    repair(&mut no, &o, |_, _| {}).unwrap();
    assert!(is_clean(&no));
    assert!(!rt_clean(&no, 1.0).0, "without StlSafe the copies join");
    let r = fix(&mut m);
    assert!(is_clean(&m));
    let s = r.stl_safe.expect("StlSafe ran");
    assert!(!s.undone && s.joined_after == 0 && s.left_pairs == 0, "{s:?}");
    assert!(s.max_move < 1e-3, "{s:?}");
    for k in crate::stlsafe::GRIDS {
        assert_eq!(rt_clean(&m, k), (true, 0), "k {k}");
    }
    assert!((volume(&m) - 2000.0).abs() < 0.2, "volume {}", volume(&m));
}

#[test]
fn stl_safe_fixes_a_cap() {
    // A sliver on the top edge 4-5 of a cube: its apex is 1e-7 off the
    // edge. float32 puts the apex on the edge: a face of zero area.
    let mut m = cube(10.0, [1000.0, 1000.0, 1000.0]);
    let (p4, p5) = (m.vertices[4], m.vertices[5]);
    let c = m.vertices.len() as u32;
    m.vertices.push(add(scale(add(p4, p5), 0.5), [0.0, 1e-7, 0.0]));
    m.triangles[2] = [4, c, 6];
    m.triangles.push([c, 5, 6]);
    m.triangles.push([4, 5, c]);
    assert!(is_clean(&m));
    assert!(rt_clean(&m, 1.0).1 > 0, "the sliver is degenerate after the round trip");
    let r = crate::stlsafe::make_stl_safe(&mut m, &crate::stlsafe::GRIDS);
    assert!(!r.undone && r.left_faces == 0 && r.left_pairs == 0, "{r:?}");
    assert!(is_clean(&m));
    for k in crate::stlsafe::GRIDS {
        assert_eq!(rt_clean(&m, k), (true, 0), "k {k}");
    }
    assert!((volume(&m) - 1000.0).abs() < 1e-3, "volume {}", volume(&m));
}

#[test]
fn stl_safe_keeps_a_clean_cube() {
    let mut m = cube(10.0, [0.0; 3]);
    let before = m.clone();
    let r = crate::stlsafe::make_stl_safe(&mut m, &crate::stlsafe::GRIDS);
    assert_eq!((r.pairs, r.faces, r.changes()), (0, 0, 0), "{r:?}");
    assert_eq!(m, before);
}