//! CpuRenderer — the last fallback of the «3D» tab: no GPU, no driver, no
//! OpenGL. It has the Python API of `Renderer` and the look of
//! shaders.wgsl (the same lighting, line quads, text and axes triad), done on
//! the CPU. It is slow, but it always gives a picture.
//!
//! Anti-aliasing: with samples > 1 the frame is drawn at 2 × 2 the size and
//! averaged. The GPU path uses MSAA 4×, so gpucheck compares the two frames
//! with the same reference.
//!
//! Threads: the rows are split into bands. Each rayon worker draws ALL
//! layers, in the GPU order, into its own band. Thus the order of the writes
//! to one pixel (depth test, blending) is the same as on the GPU.
//!
//! Triangles are snapped to 1/256 px and the edge tests are integer, with the
//! top-left rule: a pixel on an edge that two triangles share is drawn once.
//! Without this, the diagonal of a transparent quad shows as a line.

use std::collections::HashMap;

use numpy::{IntoPyArray, PyArrayMethods, PyReadonlyArray1, PyReadonlyArray2};
use pyo3::exceptions::PyValueError;
use pyo3::prelude::*;
use rayon::prelude::*;

use crate::{SHADOW, TRIAD_PX};

const SUB: f64 = 256.0; // fixed point: 1/256 px
const GUARD: f32 = 4.0; // x, y are clipped to ±GUARD·w (a guard band)

type M4 = [[f32; 4]; 4];

/// Row-major 4×4 from Python.
fn rows(m: &[f32]) -> PyResult<M4> {
    if m.len() != 16 {
        return Err(PyValueError::new_err("a 4×4 matrix needs 16 numbers"));
    }
    let mut o = [[0.0f32; 4]; 4];
    for r in 0..4 {
        for c in 0..4 {
            o[r][c] = m[r * 4 + c];
        }
    }
    Ok(o)
}

fn mul(m: &M4, p: [f32; 3]) -> [f32; 4] {
    let mut o = [0.0f32; 4];
    for (r, v) in o.iter_mut().enumerate() {
        *v = m[r][0] * p[0] + m[r][1] * p[1] + m[r][2] * p[2] + m[r][3];
    }
    o
}

fn sub3(a: [f32; 3], b: [f32; 3]) -> [f32; 3] {
    [a[0] - b[0], a[1] - b[1], a[2] - b[2]]
}

fn dot3(a: [f32; 3], b: [f32; 3]) -> f32 {
    a[0] * b[0] + a[1] * b[1] + a[2] * b[2]
}

fn cross3(a: [f32; 3], b: [f32; 3]) -> [f32; 3] {
    [a[1] * b[2] - a[2] * b[1], a[2] * b[0] - a[0] * b[2], a[0] * b[1] - a[1] * b[0]]
}

fn norm3(a: [f32; 3]) -> [f32; 3] {
    let l = dot3(a, a).sqrt();
    if l > 0.0 { [a[0] / l, a[1] / l, a[2] / l] } else { a }
}

/// One unit in the last place of a float depth near `z`: the "r" of the
/// D3D / Vulkan depth-bias formula for a 32-bit float depth buffer.
fn depth_ulp(z: f32) -> f32 {
    let z = z.abs().max(f32::MIN_POSITIVE);
    2f32.powi(z.log2().floor() as i32 - 23)
}

#[derive(Clone, Copy, PartialEq, Eq, Debug)]
enum Pass {
    Opaque,
    Overlay,
    Line,
    LineOverlay,
    Transparent,
    Text,
}

#[derive(Clone, Copy)]
struct Metrics {
    w: usize,
    h: usize,
    xmin: f32,
    ymin: f32,
    advance: f32,
}

struct Bitmap {
    m: Metrics,
    data: Vec<u8>,
}

impl Bitmap {
    /// Bilinear sample at texel coordinates (texel centres at +0.5); zero
    /// outside — the gap around each glyph in the GPU atlas.
    fn sample(&self, u: f32, v: f32) -> f32 {
        let (w, h) = (self.m.w as i32, self.m.h as i32);
        let x = u - 0.5;
        let y = v - 0.5;
        let x0 = x.floor();
        let y0 = y.floor();
        let fx = x - x0;
        let fy = y - y0;
        let at = |xi: i32, yi: i32| -> f32 {
            if xi < 0 || yi < 0 || xi >= w || yi >= h {
                0.0
            } else {
                self.data[(yi * w + xi) as usize] as f32 / 255.0
            }
        };
        let (xi, yi) = (x0 as i32, y0 as i32);
        let top = at(xi, yi) * (1.0 - fx) + at(xi + 1, yi) * fx;
        let bot = at(xi, yi + 1) * (1.0 - fx) + at(xi + 1, yi + 1) * fx;
        top * (1.0 - fy) + bot * fy
    }
}

#[derive(Clone, Copy)]
struct CGlyph {
    anchor: [f32; 4],
    rect: [f32; 4],
    key: (char, u32),
}

enum Geom {
    Tri { pts: Vec<[f32; 3]>, idx: Vec<[u32; 3]> },
    Seg(Vec<[f32; 6]>),
    Text(Vec<CGlyph>),
    // Coloured by a field (Renderer.set_scalar_triangles / set_scalar_lines).
    // vals: one per point, or one per triangle (cell); NaN already marked.
    FieldTri { pts: Vec<[f32; 3]>, idx: Vec<[u32; 3]>, vals: Vec<f32>, cell: bool,
               screen: bool },
    FieldSeg { segs: Vec<[f32; 6]>, vals: Vec<[f32; 2]> },
}

/// The colour table of a layer coloured by a field (as Layer.lut on the GPU).
struct Lut {
    table: Vec<[f32; 4]>,
    lo: f32,
    inv: f32,
    nan: [f32; 4],
}

impl Lut {
    fn new(lut: &numpy::ndarray::ArrayView2<'_, f32>, clim: (f32, f32),
           nan: (f32, f32, f32, f32)) -> PyResult<Lut> {
        let k = lut.nrows();
        if k == 0 || k > 256 || !(lut.ncols() == 3 || lut.ncols() == 4) {
            return Err(PyValueError::new_err("lut (K,3|4), 1 <= K <= 256"));
        }
        let table = lut.rows().into_iter()
            .map(|r| [r[0], r[1], r[2], if r.len() > 3 { r[3] } else { 1.0 }]).collect();
        let span = clim.1 - clim.0;
        let inv = if span.abs() > f32::MIN_POSITIVE && span.is_finite() { 1.0 / span } else { 0.0 };
        Ok(Lut { table, lo: clim.0, inv, nan: [nan.0, nan.1, nan.2, nan.3] })
    }

    /// The same rule as lut_color in shaders.wgsl.
    fn color(&self, s: f32) -> [f32; 4] {
        if !(s.abs() <= 1e30) {
            return self.nan;
        }
        let n = self.table.len() as f32;
        let i = (((s - self.lo) * self.inv * n).floor()).clamp(0.0, n - 1.0) as usize;
        self.table[i]
    }
}

/// NaN and infinity become NAN_MARK, as on the GPU.
fn mark(x: f32) -> f32 {
    if x.is_finite() { x } else { NAN_MARK }
}

const NAN_MARK: f32 = 3.0e38;

struct Layer {
    geom: Geom,
    pass: Pass,
    lut: Option<Lut>,
    color: [f32; 4],
    lighting: bool,
    specular: f32,
    // Line width in device pixels (logical width × scale), as on the GPU.
    width: f32,
    // Layers with a field: the light on a face seen edge-on (0 = 0.18).
    ambient: f32,
    visible: bool,
    order: u64,
}

struct TriadAxis {
    seg: [f32; 6],
    color: [f32; 4],
    width: f32,
    glyphs: Vec<CGlyph>,
}

/// Where a draw goes: the matrices, and the viewport in frame pixels
/// (the frame is the 2×2 one when anti-aliasing is on).
#[derive(Clone, Copy)]
struct View {
    vp: M4,
    view: M4,
    ox: f32,
    oy: f32,
    vw: f32,
    vh: f32,
    // pixel scale of the frame to the 1× frame (2 with anti-aliasing)
    ss: f32,
}

impl View {
    fn to_screen(&self, c: [f32; 4]) -> (f32, f32, f32) {
        let iw = 1.0 / c[3];
        (self.ox + (c[0] * iw * 0.5 + 0.5) * self.vw,
         self.oy + (0.5 - c[1] * iw * 0.5) * self.vh,
         c[2] * iw)
    }

    fn rect(&self) -> (i32, i32, i32, i32) {
        (self.ox.floor() as i32, self.oy.floor() as i32,
         (self.ox + self.vw).ceil() as i32, (self.oy + self.vh).ceil() as i32)
    }
}

/// A triangle on the screen, ready to fill.
struct STri {
    x: [i64; 3],
    y: [i64; 3],
    area: i64,
    tl: [bool; 3],
    z: [f32; 3],
    iw: [f32; 3],
    vpw: [[f32; 3]; 3], // view-space position × 1/w (perspective-correct)
    sw: [f32; 3],       // scalar × 1/w (perspective-correct)
    n: [f32; 3],        // face normal, view space
    bias: f32,
    bx: (i32, i32, i32, i32), // pixel bounds [x0, x1) × [y0, y1)
}

struct SSeg {
    a: (f32, f32),
    dir: (f32, f32),
    len: f32,
    half: f32,
    point: bool,
    za: f32,
    zb: f32,
    sa: f32,
    sb: f32,
    // 1/w at the two ends: the GPU interpolates the scalar perspective-correct.
    iwa: f32,
    iwb: f32,
    bias: f32,
    bx: (i32, i32, i32, i32),
}

struct SQuad<'a> {
    x0: f32,
    y0: f32,
    w: f32,
    h: f32,
    bmp: &'a Bitmap,
}

enum Item<'a> {
    Tris { tris: Vec<STri>, pass: Pass, color: [f32; 4], lighting: bool, spec: f32,
           amb: f32, persp: bool, lut: Option<&'a Lut> },
    Segs { segs: Vec<SSeg>, color: [f32; 4], lut: Option<&'a Lut> },
    Glyphs { quads: Vec<SQuad<'a>>, color: [f32; 4], clip: (i32, i32, i32, i32) },
}

/// Edge function in fixed point: > 0 when p is on the inner side of a→b.
#[inline]
fn edge(ax: i64, ay: i64, bx: i64, by: i64, px: i64, py: i64) -> i64 {
    (bx - ax) * (py - ay) - (by - ay) * (px - ax)
}

#[derive(Clone, Copy)]
struct CV {
    c: [f32; 4],
    v: [f32; 3],
    s: f32,
}

fn lerp_cv(a: CV, b: CV, t: f32) -> CV {
    let mut c = [0.0; 4];
    for i in 0..4 {
        c[i] = a.c[i] + (b.c[i] - a.c[i]) * t;
    }
    let mut v = [0.0; 3];
    for i in 0..3 {
        v[i] = a.v[i] + (b.v[i] - a.v[i]) * t;
    }
    CV { c, v, s: a.s + (b.s - a.s) * t }
}

/// Sutherland–Hodgman against one clip-space plane: keep dist >= 0.
fn clip_poly(poly: &[CV], dist: impl Fn(&[f32; 4]) -> f32) -> Vec<CV> {
    let mut out = Vec::with_capacity(poly.len() + 2);
    for i in 0..poly.len() {
        let a = poly[i];
        let b = poly[(i + 1) % poly.len()];
        let (da, db) = (dist(&a.c), dist(&b.c));
        if da >= 0.0 {
            out.push(a);
        }
        if (da >= 0.0) != (db >= 0.0) {
            out.push(lerp_cv(a, b, da / (da - db)));
        }
    }
    out
}

/// `s`: the scalar at each vertex (0 without a field). `screen`: x, y are
/// fractions of the viewport (a colour bar), at depth 0.
#[allow(clippy::too_many_arguments)]
fn setup_tri(view: &View, p: [[f32; 3]; 3], s: [f32; 3], screen: bool, overlay: bool,
             clip: (i32, i32, i32, i32), out: &mut Vec<STri>) {
    let mut poly: Vec<CV> = p.iter().zip(s)
        .map(|(q, sv)| {
            if screen {
                return CV { c: [q[0] * 2.0 - 1.0, q[1] * 2.0 - 1.0, 0.0, 1.0],
                            v: [q[0], q[1], 0.0], s: sv };
            }
            let v = mul(&view.view, *q);
            CV { c: mul(&view.vp, *q), v: [v[0], v[1], v[2]], s: sv }
        })
        .collect();
    let n = cross3(sub3(poly[1].v, poly[0].v), sub3(poly[2].v, poly[0].v));
    if !(dot3(n, n) > 0.0) {
        return;
    }
    let n = norm3(n);
    // The GPU clips to 0 <= z <= w (wgpu depth range) and to the viewport.
    let planes: [fn(&[f32; 4]) -> f32; 6] = [
        |c| c[2],
        |c| c[3] - c[2],
        |c| GUARD * c[3] - c[0],
        |c| GUARD * c[3] + c[0],
        |c| GUARD * c[3] - c[1],
        |c| GUARD * c[3] + c[1],
    ];
    let inside = poly.iter().all(|q| planes.iter().all(|f| f(&q.c) >= 0.0));
    if !inside {
        for f in planes {
            poly = clip_poly(&poly, f);
            if poly.len() < 3 {
                return;
            }
        }
    }
    let sv: Vec<(f32, f32, f32, f32, [f32; 3], f32)> = poly.iter()
        .filter(|q| q.c[3] > 1e-20)
        .map(|q| {
            let (sx, sy, z) = view.to_screen(q.c);
            let iw = 1.0 / q.c[3];
            (sx, sy, z, iw, [q.v[0] * iw, q.v[1] * iw, q.v[2] * iw], q.s * iw)
        })
        .collect();
    if sv.len() < 3 {
        return;
    }
    for k in 1..sv.len() - 1 {
        let mut v = [sv[0], sv[k], sv[k + 1]];
        let fx = |s: f32| (s as f64 * SUB).round() as i64;
        let mut x = [fx(v[0].0), fx(v[1].0), fx(v[2].0)];
        let mut y = [fx(v[0].1), fx(v[1].1), fx(v[2].1)];
        let mut area = edge(x[0], y[0], x[1], y[1], x[2], y[2]);
        if area == 0 {
            continue;
        }
        if area < 0 {
            // Two-sided: the same triangle, the other winding.
            v.swap(1, 2);
            x.swap(1, 2);
            y.swap(1, 2);
            area = -area;
        }
        // Edge i is the one across from vertex i: (i+1) → (i+2).
        let mut tl = [false; 3];
        for (i, t) in tl.iter_mut().enumerate() {
            let (a, b) = ((i + 1) % 3, (i + 2) % 3);
            let (dx, dy) = (x[b] - x[a], y[b] - y[a]);
            *t = (dy == 0 && dx > 0) || dy < 0;
        }
        let xmin = v.iter().map(|q| q.0).fold(f32::INFINITY, f32::min);
        let xmax = v.iter().map(|q| q.0).fold(f32::NEG_INFINITY, f32::max);
        let ymin = v.iter().map(|q| q.1).fold(f32::INFINITY, f32::min);
        let ymax = v.iter().map(|q| q.1).fold(f32::NEG_INFINITY, f32::max);
        let bx = ((xmin.floor() as i32).max(clip.0), (xmax.ceil() as i32 + 1).min(clip.2),
                  (ymin.floor() as i32).max(clip.1), (ymax.ceil() as i32 + 1).min(clip.3));
        if bx.0 >= bx.1 || bx.2 >= bx.3 {
            continue;
        }
        let z = [v[0].2, v[1].2, v[2].2];
        let mut bias = 0.0;
        if overlay {
            // D3D12 / Vulkan: constant · r + slope · max|dz/dx|, |dz/dy|.
            // foamview: constant -8, slope -2 (lib.rs, `pipeline`).
            let (x0, y0) = (v[0].0 as f64, v[0].1 as f64);
            let (x1, y1) = (v[1].0 as f64 - x0, v[1].1 as f64 - y0);
            let (x2, y2) = (v[2].0 as f64 - x0, v[2].1 as f64 - y0);
            let (z1, z2) = ((z[1] - z[0]) as f64, (z[2] - z[0]) as f64);
            let det = x1 * y2 - x2 * y1;
            let m = if det.abs() > 1e-12 {
                let dzdx = (z1 * y2 - z2 * y1) / det;
                let dzdy = (x1 * z2 - x2 * z1) / det;
                dzdx.abs().max(dzdy.abs()) as f32
            } else {
                0.0
            };
            let zmax = z.iter().copied().fold(0.0f32, |a, b| a.max(b.abs()));
            bias = -8.0 * depth_ulp(zmax) - 2.0 * m;
        }
        out.push(STri {
            x, y, area, tl, z,
            iw: [v[0].3, v[1].3, v[2].3],
            vpw: [v[0].4, v[1].4, v[2].4],
            sw: [v[0].5, v[1].5, v[2].5],
            n, bias, bx,
        });
    }
}

fn setup_seg(view: &View, s: &[f32; 6], sv: [f32; 2], width: f32, overlay: bool,
             clip: (i32, i32, i32, i32)) -> Option<SSeg> {
    let (mut sa, mut sb) = (sv[0], sv[1]);
    let mut ca = mul(&view.vp, [s[0], s[1], s[2]]);
    let mut cb = mul(&view.vp, [s[3], s[4], s[5]]);
    // Near plane (z >= 0): the GPU clips the quad there.
    if ca[2] < 0.0 && cb[2] < 0.0 {
        return None;
    }
    let lerp = |a: [f32; 4], b: [f32; 4], t: f32| {
        [a[0] + (b[0] - a[0]) * t, a[1] + (b[1] - a[1]) * t,
         a[2] + (b[2] - a[2]) * t, a[3] + (b[3] - a[3]) * t]
    };
    if ca[2] < 0.0 {
        let t = ca[2] / (ca[2] - cb[2]);
        ca = lerp(ca, cb, t);
        sa += (sb - sa) * t;
    } else if cb[2] < 0.0 {
        let t = cb[2] / (cb[2] - ca[2]);
        cb = lerp(cb, ca, t);
        sb += (sa - sb) * t;
    }
    let wa = ca[3].max(1e-6);
    let wb = cb[3].max(1e-6);
    let a = (view.ox + view.vw * 0.5 + ca[0] / wa * view.vw * 0.5,
             view.oy + view.vh * 0.5 - ca[1] / wa * view.vh * 0.5);
    let b = (view.ox + view.vw * 0.5 + cb[0] / wb * view.vw * 0.5,
             view.oy + view.vh * 0.5 - cb[1] / wb * view.vh * 0.5);
    let (dx, dy) = (b.0 - a.0, b.1 - a.1);
    let len = (dx * dx + dy * dy).sqrt();
    let point = len < 1e-4;
    let dir = if point { (1.0, 0.0) } else { (dx / len, dy / len) };
    let half = width * 0.5;
    let (za, zb) = (ca[2] / wa, cb[2] / wb);
    let mut bias = 0.0;
    if overlay {
        let m = if point { 0.0 } else { (zb - za).abs() / len };
        bias = -8.0 * depth_ulp(za.abs().max(zb.abs())) - 2.0 * m;
    }
    let (x0, x1) = (a.0.min(b.0) - half, a.0.max(b.0) + half);
    let (y0, y1) = (a.1.min(b.1) - half, a.1.max(b.1) + half);
    if !(x0.is_finite() && x1.is_finite() && y0.is_finite() && y1.is_finite()) {
        return None;
    }
    let bx = ((x0.floor() as i32).max(clip.0), (x1.ceil() as i32 + 1).min(clip.2),
              (y0.floor() as i32).max(clip.1), (y1.ceil() as i32 + 1).min(clip.3));
    if bx.0 >= bx.1 || bx.2 >= bx.3 {
        return None;
    }
    Some(SSeg { a, dir, len, half, point, za, zb, sa, sb, iwa: 1.0 / wa, iwb: 1.0 / wb, bias, bx })
}

#[allow(clippy::too_many_arguments)]
fn shade(base: [f32; 4], lighting: bool, spec: f32, amb: f32, n: [f32; 3], vpos: [f32; 3],
         persp: bool) -> [f32; 3] {
    let b = [base[0], base[1], base[2]];
    if !lighting {
        return b;
    }
    // The same as fs_surface: headlight, two-sided, flat.
    let v = if persp { norm3([-vpos[0], -vpos[1], -vpos[2]]) } else { [0.0, 0.0, 1.0] };
    let nv = dot3(n, v);
    let d = nv.abs();
    let n = if nv < 0.0 { [-n[0], -n[1], -n[2]] } else { n };
    let k = 2.0 * dot3(n, v);
    let r = [-v[0] + k * n[0], -v[1] + k * n[1], -v[2] + k * n[2]];
    let s = spec * dot3(r, v).max(0.0).powf(30.0);
    let amb = if amb > 0.0 { amb } else { 0.18 };
    let k = amb + (1.0 - amb) * d;
    [b[0] * k + s, b[1] * k + s, b[2] * k + s]
}

#[inline]
fn put(dst: &mut [f32; 3], c: [f32; 3], a: f32, blend: bool) {
    let c = [c[0].clamp(0.0, 1.0), c[1].clamp(0.0, 1.0), c[2].clamp(0.0, 1.0)];
    if blend {
        let a = a.clamp(0.0, 1.0);
        for i in 0..3 {
            dst[i] = c[i] * a + dst[i] * (1.0 - a);
        }
    } else {
        *dst = c;
    }
}

/// Draw one item into the band of rows [y0, y1) (full-width rows).
/// ss: samples per pixel side (2 with anti-aliasing). A field colour is
/// looked up once per pixel, at its centre, as MSAA on the GPU shades once
/// per pixel: the colour bands then have the same edges.
#[allow(clippy::too_many_arguments)]
fn draw_item(item: &Item, col: &mut [[f32; 3]], dep: &mut [f32], fw: usize, y0: i32, y1: i32,
             ss: i32) {
    // The centre of the pixel that frame sample k (x or y) belongs to.
    let centre = |k: i32| ((k / ss) * ss) as f32 + 0.5 * ss as f32;
    match item {
        Item::Tris { tris, pass, color, lighting, spec, amb, persp, lut } => {
            let blend = *pass != Pass::Opaque;
            let write = *pass != Pass::Transparent;
            for t in tris {
                let (ya, yb) = (t.bx.2.max(y0), t.bx.3.min(y1));
                if ya >= yb {
                    continue;
                }
                let af = t.area as f64;
                for py in ya..yb {
                    let syp = py as i64 * SUB as i64 + SUB as i64 / 2;
                    let row = (py - y0) as usize * fw;
                    for px in t.bx.0..t.bx.1 {
                        let sxp = px as i64 * SUB as i64 + SUB as i64 / 2;
                        let e0 = edge(t.x[1], t.y[1], t.x[2], t.y[2], sxp, syp);
                        let e1 = edge(t.x[2], t.y[2], t.x[0], t.y[0], sxp, syp);
                        let e2 = edge(t.x[0], t.y[0], t.x[1], t.y[1], sxp, syp);
                        let ok = |e: i64, tl: bool| e > 0 || (e == 0 && tl);
                        if !(ok(e0, t.tl[0]) && ok(e1, t.tl[1]) && ok(e2, t.tl[2])) {
                            continue;
                        }
                        let b = [(e0 as f64 / af) as f32, (e1 as f64 / af) as f32,
                                 (e2 as f64 / af) as f32];
                        let z = b[0] * t.z[0] + b[1] * t.z[1] + b[2] * t.z[2] + t.bias;
                        let i = row + px as usize;
                        let pass_depth = match pass {
                            Pass::Overlay => z <= dep[i],
                            _ => z < dep[i],
                        };
                        if !pass_depth {
                            continue;
                        }
                        let q = b[0] * t.iw[0] + b[1] * t.iw[1] + b[2] * t.iw[2];
                        let mut vp = [0.0f32; 3];
                        for (k, o) in vp.iter_mut().enumerate() {
                            *o = (b[0] * t.vpw[0][k] + b[1] * t.vpw[1][k] + b[2] * t.vpw[2][k]) / q;
                        }
                        let base = match lut {
                            Some(l) => {
                                let (cxp, cyp) = ((centre(px) as f64 * SUB) as i64,
                                                  (centre(py) as f64 * SUB) as i64);
                                let c = [
                                    (edge(t.x[1], t.y[1], t.x[2], t.y[2], cxp, cyp) as f64 / af) as f32,
                                    (edge(t.x[2], t.y[2], t.x[0], t.y[0], cxp, cyp) as f64 / af) as f32,
                                    (edge(t.x[0], t.y[0], t.x[1], t.y[1], cxp, cyp) as f64 / af) as f32,
                                ];
                                let qc = c[0] * t.iw[0] + c[1] * t.iw[1] + c[2] * t.iw[2];
                                let sv = (c[0] * t.sw[0] + c[1] * t.sw[1] + c[2] * t.sw[2]) / qc;
                                let c = l.color(sv);
                                [c[0], c[1], c[2], color[3]]
                            }
                            None => *color,
                        };
                        let c = shade(base, *lighting, *spec, *amb, t.n, vp, *persp);
                        put(&mut col[i], c, color[3], blend);
                        if write {
                            dep[i] = z;
                        }
                    }
                }
            }
        }
        Item::Segs { segs, color, lut } => {
            let c0 = [color[0], color[1], color[2]];
            for s in segs {
                let (ya, yb) = (s.bx.2.max(y0), s.bx.3.min(y1));
                for py in ya..yb {
                    let cy = py as f32 + 0.5;
                    let row = (py - y0) as usize * fw;
                    for px in s.bx.0..s.bx.1 {
                        let cx = px as f32 + 0.5;
                        let (rx, ry) = (cx - s.a.0, cy - s.a.1);
                        let t = if s.point {
                            if rx.abs() > s.half || ry.abs() > s.half {
                                continue;
                            }
                            0.0
                        } else {
                            let along = rx * s.dir.0 + ry * s.dir.1;
                            let across = -rx * s.dir.1 + ry * s.dir.0;
                            if along < 0.0 || along > s.len || across.abs() > s.half {
                                continue;
                            }
                            along / s.len
                        };
                        let z = s.za + (s.zb - s.za) * t + s.bias;
                        if !(0.0..=1.0).contains(&z) {
                            continue;
                        }
                        let i = row + px as usize;
                        if z <= dep[i] {
                            let c = match lut {
                                Some(l) => {
                                    let tc = if s.point { 0.0 } else {
                                        (((centre(px) - s.a.0) * s.dir.0
                                          + (centre(py) - s.a.1) * s.dir.1) / s.len)
                                            .clamp(0.0, 1.0)
                                    };
                                    let (qa, qb) = ((1.0 - tc) * s.iwa, tc * s.iwb);
                                    let k = l.color((qa * s.sa + qb * s.sb) / (qa + qb));
                                    [k[0], k[1], k[2]]
                                }
                                None => c0,
                            };
                            put(&mut col[i], c, color[3], true);
                            dep[i] = z;
                        }
                    }
                }
            }
        }
        Item::Glyphs { quads, color, clip } => {
            let c = [color[0], color[1], color[2]];
            for q in quads {
                let ya = (q.y0.floor() as i32).max(y0).max(clip.1);
                let yb = ((q.y0 + q.h).ceil() as i32).min(y1).min(clip.3);
                let xa = (q.x0.floor() as i32).max(clip.0);
                let xb = ((q.x0 + q.w).ceil() as i32).min(clip.2).min(fw as i32);
                for py in ya..yb {
                    let fy = (py as f32 + 0.5 - q.y0) / q.h;
                    if !(0.0..1.0).contains(&fy) {
                        continue;
                    }
                    let row = (py - y0) as usize * fw;
                    for px in xa.max(0)..xb {
                        let fx = (px as f32 + 0.5 - q.x0) / q.w;
                        if !(0.0..1.0).contains(&fx) {
                            continue;
                        }
                        let cov = q.bmp.sample(fx * q.bmp.m.w as f32, fy * q.bmp.m.h as f32);
                        if cov > 0.0 {
                            put(&mut col[row + px as usize], c, color[3] * cov, true);
                        }
                    }
                }
            }
        }
    }
}

#[pyclass(unsendable)]
pub struct CpuRenderer {
    w: u32,
    h: u32,
    scale: f32,
    samples: u32,
    layers: HashMap<String, Layer>,
    font: Option<fontdue::Font>,
    glyphs: HashMap<(char, u32), Bitmap>,
    triad: Vec<TriadAxis>,
    bg: ([f32; 4], [f32; 4]),
    counter: u64,
}

impl CpuRenderer {
    fn glyph(&mut self, c: char, px: u32) -> Option<Metrics> {
        if let Some(b) = self.glyphs.get(&(c, px)) {
            return Some(b.m);
        }
        let font = self.font.as_ref()?;
        let (m, data) = font.rasterize(c, px as f32);
        let met = Metrics { w: m.width, h: m.height, xmin: m.xmin as f32, ymin: m.ymin as f32,
                            advance: m.advance_width };
        self.glyphs.insert((c, px), Bitmap { m: met, data });
        Some(met)
    }

    /// The same layout as `Renderer::layout_text` (lib.rs).
    /// align: 0 = text starts at the anchor, 0.5 = centred, 1 = ends there.
    fn layout_text(&mut self, anchor: [f32; 4], text: &str, px: u32, align: f32,
                   offset: (f32, f32)) -> Vec<CGlyph> {
        let ascent = self.font.as_ref()
            .and_then(|f| f.horizontal_line_metrics(px as f32))
            .map(|m| m.ascent).unwrap_or(px as f32 * 0.8);
        let mut out = Vec::new();
        let line_h = px as f32 * 1.25;
        for (li, line) in text.split('\n').enumerate() {
            let glyphs: Vec<(char, Option<Metrics>)> =
                line.chars().map(|c| (c, self.glyph(c, px))).collect();
            let width: f32 = glyphs.iter().map(|(_, g)| g.map_or(px as f32 * 0.5, |g| g.advance)).sum();
            let mut pen = -width * align + offset.0;
            let base = ascent + li as f32 * line_h + offset.1;
            for (c, g) in glyphs {
                if let Some(g) = g {
                    if g.w > 0 && g.h > 0 {
                        out.push(CGlyph {
                            anchor,
                            rect: [pen + g.xmin, base - (g.h as f32 + g.ymin), g.w as f32, g.h as f32],
                            key: (c, px),
                        });
                    }
                    pen += g.advance;
                } else {
                    pen += px as f32 * 0.5;
                }
            }
        }
        out
    }

    fn triad_px(&self) -> u32 {
        (TRIAD_PX as f32 * self.scale).round().max(40.0) as u32
    }

    /// Scalars of segments as pairs: 2M values, or M values twice.
    fn pairs(sv: Vec<f32>, m: usize) -> PyResult<Vec<[f32; 2]>> {
        if sv.len() == 2 * m {
            Ok(sv.chunks_exact(2).map(|c| [mark(c[0]), mark(c[1])]).collect())
        } else if sv.len() == m {
            Ok(sv.into_iter().map(|x| [mark(x), mark(x)]).collect())
        } else {
            Err(PyValueError::new_err(format!(
                "scalars: {} (two per segment) or {m} values, not {}", 2 * m, sv.len())))
        }
    }

    fn build_triad(&mut self) {
        let axes = [([1.0f32, 0.0, 0.0], [0.86, 0.24, 0.22], "X"),
                    ([0.0, 1.0, 0.0], [0.93, 0.83, 0.25], "Y"),
                    ([0.0, 0.0, 1.0], [0.30, 0.55, 0.95], "Z")];
        let mut out = Vec::new();
        for (d, c, name) in axes {
            let px = (15.0 * self.scale).round() as u32;
            let glyphs = self.layout_text([d[0] * 0.95, d[1] * 0.95, d[2] * 0.95, 0.0], name, px,
                                          0.5, (0.0, -8.0 * self.scale));
            out.push(TriadAxis {
                seg: [0.0, 0.0, 0.0, d[0] * 0.8, d[1] * 0.8, d[2] * 0.8],
                color: [c[0], c[1], c[2], 1.0],
                width: 3.0 * self.scale,
                glyphs,
            });
        }
        self.triad = out;
    }

    #[allow(clippy::too_many_arguments)]
    fn put_layer(&mut self, name: &str, geom: Geom, pass: Pass, color: [f32; 4], lighting: bool,
                 specular: f32, width: f32) {
        let visible = self.layers.get(name).map_or(true, |l| l.visible);
        self.counter += 1;
        self.layers.insert(name.to_string(), Layer { geom, pass, lut: None, color, lighting,
                                                     specular, width, ambient: 0.0, visible,
                                                     order: self.counter });
    }

    fn quads<'a>(&'a self, view: &View, glyphs: &[CGlyph]) -> Vec<SQuad<'a>> {
        let ss = view.ss;
        glyphs.iter().filter_map(|g| {
            let bmp = self.glyphs.get(&g.key)?;
            let (bx, by) = if g.anchor[3] < 0.5 {
                let c = mul(&view.vp, [g.anchor[0], g.anchor[1], g.anchor[2]]);
                if c[3] <= 0.0 {
                    return None; // behind the camera: the GPU clips it (z = 2)
                }
                let w = c[3].max(1e-6);
                ((c[0] / w * 0.5 + 0.5) * view.vw, view.vh - (c[1] / w * 0.5 + 0.5) * view.vh)
            } else if g.anchor[3] < 1.5 {
                (g.anchor[0] * ss, g.anchor[1] * ss)
            } else {
                // Fractions of the viewport from the bottom-left corner.
                (g.anchor[0] * view.vw, (1.0 - g.anchor[1]) * view.vh)
            };
            Some(SQuad { x0: view.ox + bx + g.rect[0] * ss, y0: view.oy + by + g.rect[1] * ss,
                         w: g.rect[2] * ss, h: g.rect[3] * ss, bmp })
        }).collect()
    }

    fn frame(&self, view_proj: &[f32], view: &[f32], perspective: bool,
             triad: Option<&[f32]>) -> PyResult<Vec<u8>> {
        let ss: u32 = if self.samples > 1 { 2 } else { 1 };
        let (fw, fh) = ((self.w * ss) as usize, (self.h * ss) as usize);
        let main = View { vp: rows(view_proj)?, view: rows(view)?,
                          ox: 0.0, oy: 0.0, vw: fw as f32, vh: fh as f32, ss: ss as f32 };
        let full = main.rect();
        let mut order: Vec<&Layer> = self.layers.values().filter(|l| l.visible).collect();
        order.sort_by_key(|l| l.order);
        let mut items: Vec<Item> = Vec::new();
        for pass in [Pass::Opaque, Pass::Overlay, Pass::Line, Pass::LineOverlay,
                     Pass::Transparent, Pass::Text] {
            for l in order.iter().filter(|l| l.pass == pass) {
                match &l.geom {
                    Geom::Tri { pts, idx } => {
                        let overlay = pass == Pass::Overlay;
                        let tris: Vec<STri> = idx.par_iter()
                            .fold(Vec::new, |mut acc, t| {
                                let p = [pts[t[0] as usize], pts[t[1] as usize], pts[t[2] as usize]];
                                setup_tri(&main, p, [0.0; 3], false, overlay, full, &mut acc);
                                acc
                            })
                            .reduce(Vec::new, |mut a, mut b| { a.append(&mut b); a });
                        items.push(Item::Tris { tris, pass, color: l.color, lighting: l.lighting,
                                                spec: l.specular, amb: 0.0, persp: perspective,
                                                lut: None });
                    }
                    Geom::FieldTri { pts, idx, vals, cell, screen } => {
                        let overlay = pass == Pass::Overlay;
                        let tris: Vec<STri> = idx.par_iter().enumerate()
                            .fold(Vec::new, |mut acc, (k, t)| {
                                let p = [pts[t[0] as usize], pts[t[1] as usize], pts[t[2] as usize]];
                                let s = if *cell { [vals[k]; 3] } else {
                                    [vals[t[0] as usize], vals[t[1] as usize], vals[t[2] as usize]]
                                };
                                setup_tri(&main, p, s, *screen, overlay, full, &mut acc);
                                acc
                            })
                            .reduce(Vec::new, |mut a, mut b| { a.append(&mut b); a });
                        items.push(Item::Tris { tris, pass, color: l.color, lighting: l.lighting,
                                                spec: l.specular, amb: l.ambient,
                                                persp: perspective, lut: l.lut.as_ref() });
                    }
                    Geom::Seg(segs) => {
                        let w = l.width * ss as f32;
                        let ov = pass == Pass::LineOverlay;
                        let s: Vec<SSeg> = segs.par_iter()
                            .filter_map(|s| setup_seg(&main, s, [0.0; 2], w, ov, full)).collect();
                        items.push(Item::Segs { segs: s, color: l.color, lut: None });
                    }
                    Geom::FieldSeg { segs, vals } => {
                        let w = l.width * ss as f32;
                        let ov = pass == Pass::LineOverlay;
                        let s: Vec<SSeg> = segs.par_iter().zip(vals.par_iter())
                            .filter_map(|(s, v)| setup_seg(&main, s, *v, w, ov, full)).collect();
                        items.push(Item::Segs { segs: s, color: l.color, lut: l.lut.as_ref() });
                    }
                    Geom::Text(g) => {
                        items.push(Item::Glyphs { quads: self.quads(&main, g), color: l.color,
                                                  clip: full });
                    }
                }
            }
        }
        let tpx = self.triad_px();
        if let Some(tm) = triad {
            if self.w > tpx + 6 && self.h > tpx + 6 {
                let m = rows(tm)?;
                let s = ss as f32;
                let tv = View { vp: m, view: m, ox: 6.0 * s,
                                oy: (self.h - tpx - 6) as f32 * s,
                                vw: tpx as f32 * s, vh: tpx as f32 * s, ss: s };
                let clip = tv.rect();
                for a in &self.triad {
                    let segs: Vec<SSeg> = setup_seg(&tv, &a.seg, [0.0; 2], a.width * s, false,
                                                    clip).into_iter().collect();
                    items.push(Item::Segs { segs, color: a.color, lut: None });
                    items.push(Item::Glyphs { quads: self.quads(&tv, &a.glyphs), color: a.color,
                                              clip });
                }
            }
        }
        let mut col = vec![[0.0f32; 3]; fw * fh];
        let mut dep = vec![1.0f32; fw * fh];
        let (top, bot) = self.bg;
        let threads = rayon::current_num_threads().max(1);
        let band = fh.div_ceil(threads * 4).max(8);
        col.par_chunks_mut(fw * band).zip(dep.par_chunks_mut(fw * band)).enumerate()
            .for_each(|(bi, (c, d))| {
                let y0 = (bi * band) as i32;
                let y1 = y0 + (c.len() / fw) as i32;
                for py in y0..y1 {
                    // vs_bg: t = 0 at the bottom edge, 1 at the top edge.
                    let t = 1.0 - (py as f32 + 0.5) / fh as f32;
                    let px = [bot[0] + (top[0] - bot[0]) * t, bot[1] + (top[1] - bot[1]) * t,
                              bot[2] + (top[2] - bot[2]) * t];
                    let row = (py - y0) as usize * fw;
                    c[row..row + fw].fill(px);
                }
                for it in &items {
                    draw_item(it, c, d, fw, y0, y1, ss as i32);
                }
            });
        // Resolve: the average of each ss × ss block (as an MSAA resolve).
        let (w, h) = (self.w as usize, self.h as usize);
        let s = ss as usize;
        let k = 1.0 / (s * s) as f32;
        let mut out = vec![0u8; w * h * 4];
        out.par_chunks_mut(w * 4).enumerate().for_each(|(y, row)| {
            for x in 0..w {
                let mut acc = [0.0f32; 3];
                for dy in 0..s {
                    for dx in 0..s {
                        let p = col[(y * s + dy) * fw + x * s + dx];
                        for i in 0..3 {
                            acc[i] += p[i].clamp(0.0, 1.0);
                        }
                    }
                }
                for i in 0..3 {
                    row[x * 4 + i] = (acc[i] * k * 255.0).round() as u8;
                }
                row[x * 4 + 3] = 255;
            }
        });
        Ok(out)
    }
}

#[pymethods]
impl CpuRenderer {
    /// The arguments of `Renderer`. `hwnd` and `kind` are ignored: the CPU
    /// draws only into memory, and the widget shows the frame (QPainter).
    #[new]
    #[pyo3(signature = (hwnd=None, width=640, height=480, font_path=None, samples=4,
                        kind="win32", backend=None, adapter=None))]
    #[allow(clippy::too_many_arguments, unused_variables)]
    fn new(hwnd: Option<isize>, width: u32, height: u32, font_path: Option<String>,
           samples: u32, kind: &str, backend: Option<String>, adapter: Option<String>)
           -> PyResult<Self> {
        let font = font_path.and_then(|p| std::fs::read(p).ok())
            .and_then(|b| fontdue::Font::from_bytes(b, fontdue::FontSettings::default()).ok());
        let mut r = CpuRenderer {
            w: width.max(1), h: height.max(1), scale: 1.0,
            samples: if samples > 1 { 4 } else { 1 },
            layers: HashMap::new(), font, glyphs: HashMap::new(), triad: Vec::new(),
            bg: ([0.23, 0.25, 0.29, 1.0], [0.13, 0.15, 0.17, 1.0]), counter: 0,
        };
        r.build_triad();
        Ok(r)
    }

    #[getter]
    fn adapter(&self) -> String {
        format!("CPU, {} threads (foamview)", rayon::current_num_threads())
    }

    #[getter]
    fn adapter_kind(&self) -> String {
        "Cpu".to_string()
    }

    fn set_scale(&mut self, scale: f32) {
        let s = scale.clamp(0.5, 8.0);
        if (s - self.scale).abs() > 1e-3 {
            self.scale = s;
            self.build_triad();
        }
    }

    #[getter]
    fn samples(&self) -> u32 {
        self.samples
    }

    #[getter]
    fn has_font(&self) -> bool {
        self.font.is_some()
    }

    fn resize(&mut self, width: u32, height: u32) {
        self.w = width.max(1);
        self.h = height.max(1);
    }

    fn size(&self) -> (u32, u32) {
        (self.w, self.h)
    }

    fn set_background(&mut self, top: (f32, f32, f32), bottom: (f32, f32, f32)) {
        self.bg = ([top.0, top.1, top.2, 1.0], [bottom.0, bottom.1, bottom.2, 1.0]);
    }

    #[pyo3(signature = (name, points, tris, color, mode="opaque", lighting=true, specular=0.3))]
    #[allow(clippy::too_many_arguments)]
    fn set_triangles(&mut self, name: &str, points: PyReadonlyArray2<'_, f32>,
                     tris: PyReadonlyArray2<'_, u32>, color: (f32, f32, f32, f32), mode: &str,
                     lighting: bool, specular: f32) -> PyResult<()> {
        let p = points.as_array();
        let t = tris.as_array();
        if p.ncols() != 3 || t.ncols() != 3 {
            return Err(PyValueError::new_err("points (N,3), tris (M,3)"));
        }
        let n = p.nrows() as u32;
        let pts: Vec<[f32; 3]> = p.rows().into_iter().map(|r| [r[0], r[1], r[2]]).collect();
        let idx: Vec<[u32; 3]> = t.rows().into_iter().map(|r| [r[0], r[1], r[2]]).collect();
        if idx.iter().flatten().any(|&i| i >= n) {
            return Err(PyValueError::new_err("triangle index out of range"));
        }
        if idx.is_empty() || pts.is_empty() {
            self.remove(name);
            return Ok(());
        }
        let pass = match mode {
            "overlay" => Pass::Overlay,
            "transparent" => Pass::Transparent,
            _ => Pass::Opaque,
        };
        self.put_layer(name, Geom::Tri { pts, idx }, pass,
                       [color.0, color.1, color.2, color.3], lighting, specular, 0.0);
        Ok(())
    }

    #[pyo3(signature = (name, segments, color, width=1.0, overlay=false))]
    fn set_lines(&mut self, name: &str, segments: PyReadonlyArray2<'_, f32>,
                 color: (f32, f32, f32, f32), width: f32, overlay: bool) -> PyResult<()> {
        let s = segments.as_array();
        if s.ncols() != 6 {
            return Err(PyValueError::new_err("segments (M,6)"));
        }
        let v: Vec<[f32; 6]> = s.rows().into_iter()
            .map(|r| [r[0], r[1], r[2], r[3], r[4], r[5]]).collect();
        if v.is_empty() {
            self.remove(name);
            return Ok(());
        }
        self.put_layer(name, Geom::Seg(v), if overlay { Pass::LineOverlay } else { Pass::Line },
                       [color.0, color.1, color.2, color.3], false, 0.0,
                       (width * self.scale).max(1.0));
        Ok(())
    }

    #[pyo3(signature = (name, anchors, texts, color, size=14, screen=false, center=false,
                        offset=(0.0, 0.0), viewport=false, right=false))]
    #[allow(clippy::too_many_arguments)]
    fn set_labels(&mut self, name: &str, anchors: Vec<(f32, f32, f32)>, texts: Vec<String>,
                  color: (f32, f32, f32, f32), size: u32, screen: bool, center: bool,
                  offset: (f32, f32), viewport: bool, right: bool) -> PyResult<()> {
        if anchors.len() != texts.len() {
            return Err(PyValueError::new_err("one text per anchor"));
        }
        let mut glyphs = Vec::new();
        for (a, t) in anchors.iter().zip(&texts) {
            let kind = if viewport { 2.0 } else if screen { 1.0 } else { 0.0 };
            let anchor = [a.0, a.1, a.2, kind];
            let px = ((size as f32) * self.scale).round().max(6.0) as u32;
            let off = (offset.0 * self.scale, offset.1 * self.scale);
            let align = if center { 0.5 } else if right { 1.0 } else { 0.0 };
            glyphs.extend(self.layout_text(anchor, t, px, align, off));
        }
        if glyphs.is_empty() {
            self.remove(name);
            return Ok(());
        }
        let d = self.scale.max(1.0);
        let shadow: Vec<CGlyph> = glyphs.iter().map(|g| CGlyph {
            rect: [g.rect[0] + d, g.rect[1] + d, g.rect[2], g.rect[3]], ..*g }).collect();
        let sname = format!("{name}{SHADOW}");
        self.put_layer(&sname, Geom::Text(shadow), Pass::Text, [0.0, 0.0, 0.0, 0.8 * color.3],
                       false, 0.0, 0.0);
        self.put_layer(name, Geom::Text(glyphs), Pass::Text,
                       [color.0, color.1, color.2, color.3], false, 0.0, 0.0);
        Ok(())
    }

    /// The same as Renderer.set_scalar_triangles. `normals` are not used:
    /// the CPU always shades flat.
    #[pyo3(signature = (name, points, tris, scalars, lut, clim, cell=false, normals=None,
                        opacity=1.0, nan_color=(0.66, 0.66, 0.66, 1.0), mode="opaque",
                        lighting=true, specular=0.0, screen=false, ambient=0.0))]
    #[allow(clippy::too_many_arguments, unused_variables)]
    fn set_scalar_triangles(&mut self, name: &str, points: PyReadonlyArray2<'_, f32>,
                            tris: PyReadonlyArray2<'_, u32>, scalars: PyReadonlyArray1<'_, f32>,
                            lut: PyReadonlyArray2<'_, f32>, clim: (f32, f32), cell: bool,
                            normals: Option<PyReadonlyArray2<'_, f32>>, opacity: f32,
                            nan_color: (f32, f32, f32, f32), mode: &str, lighting: bool,
                            specular: f32, screen: bool, ambient: f32) -> PyResult<()> {
        let p = points.as_array();
        let t = tris.as_array();
        if p.ncols() != 3 || t.ncols() != 3 {
            return Err(PyValueError::new_err("points (N,3), tris (M,3)"));
        }
        let pts: Vec<[f32; 3]> = p.rows().into_iter().map(|r| [r[0], r[1], r[2]]).collect();
        let idx: Vec<[u32; 3]> = t.rows().into_iter().map(|r| [r[0], r[1], r[2]]).collect();
        let vals: Vec<f32> = scalars.as_array().iter().map(|&x| mark(x)).collect();
        let want = if cell { idx.len() } else { pts.len() };
        if vals.len() != want {
            return Err(PyValueError::new_err(format!("scalars: {want} values, not {}",
                                                     vals.len())));
        }
        if idx.iter().flatten().any(|&i| i as usize >= pts.len()) {
            return Err(PyValueError::new_err("triangle index out of range"));
        }
        if idx.is_empty() || pts.is_empty() {
            self.remove(name);
            return Ok(());
        }
        let l = Lut::new(&lut.as_array(), clim, nan_color)?;
        let pass = match mode {
            "overlay" => Pass::Overlay,
            "transparent" => Pass::Transparent,
            _ => Pass::Opaque,
        };
        self.put_layer(name, Geom::FieldTri { pts, idx, vals, cell, screen }, pass,
                       [1.0, 1.0, 1.0, opacity], lighting, specular, 0.0);
        if let Some(layer) = self.layers.get_mut(name) {
            layer.lut = Some(l);
            layer.ambient = ambient.clamp(0.0, 1.0);
        }
        Ok(())
    }

    /// The same as Renderer.set_scalar_lines.
    #[pyo3(signature = (name, segments, scalars, lut, clim, width=1.0, overlay=false,
                        opacity=1.0, nan_color=(0.66, 0.66, 0.66, 1.0)))]
    #[allow(clippy::too_many_arguments)]
    fn set_scalar_lines(&mut self, name: &str, segments: PyReadonlyArray2<'_, f32>,
                        scalars: PyReadonlyArray1<'_, f32>, lut: PyReadonlyArray2<'_, f32>,
                        clim: (f32, f32), width: f32, overlay: bool, opacity: f32,
                        nan_color: (f32, f32, f32, f32)) -> PyResult<()> {
        let s = segments.as_array();
        if s.ncols() != 6 {
            return Err(PyValueError::new_err("segments (M,6)"));
        }
        let segs: Vec<[f32; 6]> = s.rows().into_iter()
            .map(|r| [r[0], r[1], r[2], r[3], r[4], r[5]]).collect();
        if segs.is_empty() {
            self.remove(name);
            return Ok(());
        }
        let vals = Self::pairs(scalars.as_array().iter().copied().collect(), segs.len())?;
        let l = Lut::new(&lut.as_array(), clim, nan_color)?;
        self.put_layer(name, Geom::FieldSeg { segs, vals },
                       if overlay { Pass::LineOverlay } else { Pass::Line },
                       [1.0, 1.0, 1.0, opacity], false, 0.0, (width * self.scale).max(1.0));
        if let Some(layer) = self.layers.get_mut(name) {
            layer.lut = Some(l);
        }
        Ok(())
    }

    /// The same as Renderer.update_scalars.
    fn update_scalars(&mut self, name: &str, scalars: PyReadonlyArray1<'_, f32>) -> PyResult<()> {
        let sv: Vec<f32> = scalars.as_array().iter().map(|&x| mark(x)).collect();
        let Some(l) = self.layers.get_mut(name) else {
            return Err(PyValueError::new_err(format!("no layer {name:?}")));
        };
        match &mut l.geom {
            Geom::FieldTri { vals, .. } => {
                if vals.len() != sv.len() {
                    return Err(PyValueError::new_err(format!(
                        "{name}: {} values, not {}", vals.len(), sv.len())));
                }
                *vals = sv;
            }
            Geom::FieldSeg { segs, vals } => {
                *vals = Self::pairs(sv, segs.len())?;
            }
            _ => return Err(PyValueError::new_err(format!("{name}: a layer without a field"))),
        }
        Ok(())
    }

    /// The same as Renderer.set_colormap.
    #[pyo3(signature = (name, lut, clim, nan_color=None, opacity=None))]
    fn set_colormap(&mut self, name: &str, lut: PyReadonlyArray2<'_, f32>, clim: (f32, f32),
                    nan_color: Option<(f32, f32, f32, f32)>, opacity: Option<f32>)
                    -> PyResult<()> {
        let Some(l) = self.layers.get_mut(name) else {
            return Err(PyValueError::new_err(format!("no layer {name:?}")));
        };
        let old = l.lut.as_ref().map_or([0.66, 0.66, 0.66, 1.0], |x| x.nan);
        let nan = nan_color.unwrap_or((old[0], old[1], old[2], old[3]));
        l.lut = Some(Lut::new(&lut.as_array(), clim, nan)?);
        if let Some(a) = opacity {
            l.color[3] = a;
        }
        Ok(())
    }

    fn set_visible(&mut self, name: &str, visible: bool) {
        for n in [name.to_string(), format!("{name}{SHADOW}")] {
            if let Some(l) = self.layers.get_mut(&n) {
                l.visible = visible;
            }
        }
    }

    fn remove(&mut self, name: &str) {
        self.layers.remove(name);
        self.layers.remove(&format!("{name}{SHADOW}"));
    }

    fn clear(&mut self) {
        self.layers.clear();
    }

    fn names(&self) -> Vec<String> {
        self.layers.keys().filter(|k| !k.ends_with(SHADOW)).cloned().collect()
    }

    /// No window: nothing to present. The widget calls `screenshot`.
    #[pyo3(signature = (view_proj, view, perspective=true, triad=None))]
    #[allow(unused_variables)]
    fn render(&mut self, view_proj: Vec<f32>, view: Vec<f32>, perspective: bool,
              triad: Option<Vec<f32>>) -> PyResult<()> {
        Ok(())
    }

    /// The frame: (h, w, 4) uint8 RGBA. The GIL is free while it draws.
    #[pyo3(signature = (view_proj, view, perspective=true, triad=None))]
    fn screenshot<'py>(&mut self, py: Python<'py>, view_proj: Vec<f32>, view: Vec<f32>,
                       perspective: bool, triad: Option<Vec<f32>>)
                       -> PyResult<Bound<'py, PyAny>> {
        let me: &CpuRenderer = self;
        let out = py.detach(|| me.frame(&view_proj, &view, perspective, triad.as_deref()))?;
        let arr = out.into_pyarray(py).reshape([self.h as usize, self.w as usize, 4])?;
        Ok(arr.into_any())
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn eye() -> Vec<f32> {
        vec![1.0, 0.0, 0.0, 0.0, 0.0, 1.0, 0.0, 0.0, 0.0, 0.0, 1.0, 0.0, 0.0, 0.0, 0.0, 1.0]
    }

    fn renderer(w: u32, h: u32, samples: u32) -> CpuRenderer {
        CpuRenderer::new(None, w, h, None, samples, "win32", None, None).unwrap()
    }

    fn px(img: &[u8], w: u32, x: u32, y: u32) -> [u8; 3] {
        let i = ((y * w + x) * 4) as usize;
        [img[i], img[i + 1], img[i + 2]]
    }

    #[test]
    fn background_gradient() {
        let mut r = renderer(8, 100, 1);
        r.set_background((1.0, 0.0, 0.0), (0.0, 0.0, 1.0));
        let img = r.frame(&eye(), &eye(), false, None).unwrap();
        assert!(px(&img, 8, 4, 0)[0] > 250);
        assert!(px(&img, 8, 4, 99)[2] > 250);
    }

    fn add_quad(r: &mut CpuRenderer, name: &str, z: f32, color: [f32; 4], mode: Pass) {
        let pts = vec![[-0.5, -0.5, z], [0.5, -0.5, z], [0.5, 0.5, z], [-0.5, 0.5, z]];
        let idx = vec![[0, 1, 2], [0, 2, 3]];
        r.put_layer(name, Geom::Tri { pts, idx }, mode, color, false, 0.0, 0.0);
    }

    #[test]
    fn transparent_quad_has_no_seam() {
        // The shared diagonal must be blended once (top-left rule).
        let mut r = renderer(64, 64, 1);
        r.set_background((0.0, 0.0, 0.0), (0.0, 0.0, 0.0));
        add_quad(&mut r, "q", 0.5, [1.0, 1.0, 1.0, 0.5], Pass::Transparent);
        let img = r.frame(&eye(), &eye(), false, None).unwrap();
        for y in 20..44 {
            for x in 20..44 {
                assert_eq!(px(&img, 64, x, y), [128, 128, 128], "at {x},{y}");
            }
        }
    }

    #[test]
    fn depth_and_overlay() {
        let mut r = renderer(32, 32, 1);
        add_quad(&mut r, "far", 0.8, [0.0, 0.0, 1.0, 1.0], Pass::Opaque);
        add_quad(&mut r, "near", 0.2, [1.0, 0.0, 0.0, 1.0], Pass::Opaque);
        add_quad(&mut r, "mark", 0.2, [0.0, 1.0, 0.0, 1.0], Pass::Overlay);
        let img = r.frame(&eye(), &eye(), false, None).unwrap();
        assert_eq!(px(&img, 32, 16, 16), [0, 255, 0]);
    }

    #[test]
    fn near_plane_clip() {
        // A triangle that crosses z = 0 must not wrap around or panic.
        let mut r = renderer(32, 32, 4);
        let pts = vec![[-0.5, -0.5, -1.0], [0.5, -0.5, 0.5], [0.0, 0.5, 0.5]];
        r.put_layer("t", Geom::Tri { pts, idx: vec![[0, 1, 2]] }, Pass::Opaque,
                    [1.0, 1.0, 1.0, 1.0], true, 0.3, 0.0);
        r.frame(&eye(), &eye(), true, None).unwrap();
    }

    #[test]
    fn thick_line_and_point() {
        let mut r = renderer(40, 40, 1);
        r.set_background((0.0, 0.0, 0.0), (0.0, 0.0, 0.0));
        r.put_layer("l", Geom::Seg(vec![[-1.0, 0.0, 0.5, 1.0, 0.0, 0.5]]), Pass::Line,
                    [1.0, 1.0, 1.0, 1.0], false, 0.0, 4.0);
        r.put_layer("p", Geom::Seg(vec![[0.5, 0.5, 0.5, 0.5, 0.5, 0.5]]), Pass::Line,
                    [1.0, 0.0, 0.0, 1.0], false, 0.0, 6.0);
        let img = r.frame(&eye(), &eye(), false, None).unwrap();
        assert_eq!(px(&img, 40, 5, 19), [255, 255, 255]);
        assert_eq!(px(&img, 40, 5, 20), [255, 255, 255]);
        assert_eq!(px(&img, 40, 5, 12), [0, 0, 0]);
        assert_eq!(px(&img, 40, 30, 10), [255, 0, 0]); // the point at (0.5, 0.5)
    }
}
