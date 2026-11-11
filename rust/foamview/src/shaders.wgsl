// foamview shaders. Colours are sRGB values written straight into a
// non-sRGB target — the same look as VTK, which also lights in sRGB space.

struct Globals {
    view_proj: mat4x4<f32>,
    view: mat4x4<f32>,
    // x, y: viewport size in px; z: 1 = perspective, 0 = parallel
    viewport: vec4<f32>,
    bg_top: vec4<f32>,
    bg_bottom: vec4<f32>,
};

// Number of colours in the colour table of a layer (lut). Keep it equal to
// LUT_N in lib.rs.
const LUT_N: i32 = 256;

struct Layer {
    color: vec4<f32>,
    // x: lighting (1/0), y: specular, z: line width px (lines) or the
    // ambient part (layers with a field, 0 = 0.18), w: 1 = the vertices have
    // normals (smooth shading)
    params: vec4<f32>,
    // Scalar layers: x: low end of the scale, y: 1 / (high - low),
    // z: number of colours in lut, w: 1 = positions are fractions of the
    // viewport from the bottom-left corner (colour bar), not world points.
    scal: vec4<f32>,
    // The colour of a value that is not a number (lib.rs puts 3e38 there).
    nan: vec4<f32>,
    lut: array<vec4<f32>, 256>,
};

@group(0) @binding(0) var<uniform> g: Globals;

// Colours are sRGB numbers. On an sRGB-encoded target (viewport.w = 1) the
// hardware encodes on write, so decode first — or everything looks washed out.
fn finish(c: vec4<f32>) -> vec4<f32> {
    let lin = pow(max(c.rgb, vec3<f32>(0.0)), vec3<f32>(2.2));
    return vec4<f32>(select(c.rgb, lin, g.viewport.w > 0.5), c.a);
}
@group(1) @binding(0) var<uniform> layer: Layer;

// The colour of scalar s: the same rule as a VTK lookup table with
// interpolation before the mapping — s comes interpolated per pixel, then
// one entry of the table is taken. Values out of the scale are clamped.
fn lut_color(s: f32) -> vec4<f32> {
    let n = clamp(layer.scal.z, 1.0, f32(LUT_N));
    let t = (s - layer.scal.x) * layer.scal.y;
    let i = i32(clamp(floor(t * n), 0.0, n - 1.0));
    return select(layer.lut[i], layer.nan, abs(s) > 1e30);
}

// Headlight, two-sided: a face seen from behind is lit like its front.
// amb: the light on a face seen edge-on (0.18 for plain surfaces; layers with
// a field take more, so that a colour stays near its colour-bar colour, as
// with the VTK light kit).
fn light(base: vec3<f32>, n0: vec3<f32>, vpos: vec3<f32>, amb: f32) -> vec3<f32> {
    var n = n0;
    var v = normalize(-vpos);
    if (g.viewport.z < 0.5) {
        v = vec3<f32>(0.0, 0.0, 1.0);
    }
    let d = abs(dot(n, v));
    if (dot(n, v) < 0.0) {
        n = -n;
    }
    let r = reflect(-v, n);
    let s = layer.params.y * pow(max(dot(r, v), 0.0), 30.0);
    return base * (amb + (1.0 - amb) * d) + vec3<f32>(s);
}

// ---------------------------------------------------------------- surfaces

struct SurfOut {
    @builtin(position) pos: vec4<f32>,
    @location(0) vpos: vec3<f32>,
};

@vertex
fn vs_surface(@location(0) p: vec3<f32>) -> SurfOut {
    var o: SurfOut;
    o.pos = g.view_proj * vec4<f32>(p, 1.0);
    o.vpos = (g.view * vec4<f32>(p, 1.0)).xyz;
    return o;
}

@fragment
fn fs_surface(i: SurfOut) -> @location(0) vec4<f32> {
    let base = layer.color.rgb;
    if (layer.params.x < 0.5) {
        return finish(vec4<f32>(base, layer.color.a));
    }
    // Flat shading: the face normal from screen-space derivatives — no
    // normals to upload, and every triangle is lit as a plane (as VTK
    // does with smooth_shading=False).
    let n = normalize(cross(dpdx(i.vpos), dpdy(i.vpos)));
    return finish(vec4<f32>(light(base, n, i.vpos, 0.18), layer.color.a));
}

// --------------------------------------------------- surfaces with a field
// One scalar per vertex (point data) or the same scalar on the three
// vertices of a triangle (cell data). Normals are optional: without them
// (params.w = 0) the slot holds the positions again and the face normal
// comes from the derivatives, as in fs_surface.

struct ScalOut {
    @builtin(position) pos: vec4<f32>,
    @location(0) vpos: vec3<f32>,
    @location(1) nrm: vec3<f32>,
    @location(2) s: f32,
};

@vertex
fn vs_scalar(@location(0) p: vec3<f32>, @location(1) s: f32,
             @location(2) n: vec3<f32>) -> ScalOut {
    var o: ScalOut;
    let world = g.view_proj * vec4<f32>(p, 1.0);
    // A colour bar: x, y are fractions of the viewport; depth 0 puts it in
    // front of the scene. `select`, not `if` (Intel GL, see vs_line).
    let flat2d = vec4<f32>(p.x * 2.0 - 1.0, p.y * 2.0 - 1.0, 0.0, 1.0);
    o.pos = select(world, flat2d, layer.scal.w > 0.5);
    o.vpos = (g.view * vec4<f32>(p, 1.0)).xyz;
    o.nrm = (g.view * vec4<f32>(n, 0.0)).xyz;
    o.s = s;
    return o;
}

@fragment
fn fs_scalar(i: ScalOut) -> @location(0) vec4<f32> {
    let base = lut_color(i.s).rgb;
    if (layer.params.x < 0.5) {
        return finish(vec4<f32>(base, layer.color.a));
    }
    let face = normalize(cross(dpdx(i.vpos), dpdy(i.vpos)));
    let ln = length(i.nrm);
    let n = select(face, i.nrm / max(ln, 1e-20), layer.params.w > 0.5 && ln > 1e-20);
    // params.z: the ambient part (0 = as plain surfaces).
    let amb = select(0.18, layer.params.z, layer.params.z > 0.0);
    return finish(vec4<f32>(light(base, n, i.vpos, amb), layer.color.a));
}

// ---------------------------------------------------------- thick lines
// One instance per segment; six vertices make a screen-space quad.

struct LineOut {
    @builtin(position) pos: vec4<f32>,
};

fn line_end(vi: u32) -> f32 {
    var ends = array<f32, 6>(0.0, 1.0, 0.0, 0.0, 1.0, 1.0);
    return ends[vi];
}

fn line_clip(vi: u32, a: vec3<f32>, b: vec3<f32>) -> vec4<f32> {
    let ca = g.view_proj * vec4<f32>(a, 1.0);
    let cb = g.view_proj * vec4<f32>(b, 1.0);
    let wa = max(ca.w, 1e-6);
    let wb = max(cb.w, 1e-6);
    let sa = ca.xy / wa * g.viewport.xy * 0.5;
    let sb = cb.xy / wb * g.viewport.xy * 0.5;
    var dir = sb - sa;
    let len = length(dir);
    // A segment of zero length is a POINT: it becomes a square of the
    // line width (problem points, e.g. degenerate triangles).
    // `select`, not `if` on this computed bool: the branch broke all lines
    // on Intel GL (checked on four backends).
    let point = len < 1e-4;
    dir = select(dir / max(len, 1e-4), vec2<f32>(1.0, 0.0), point);
    let nrm = vec2<f32>(-dir.y, dir.x);
    // corner: (end, side) for the 6 vertices of two triangles
    var sides = array<f32, 6>(-1.0, -1.0, 1.0, 1.0, -1.0, 1.0);
    let e = line_end(vi);
    let sd = sides[vi];
    let half = layer.params.z * 0.5;
    let c = select(ca, cb, e > 0.5);
    let px = nrm * sd * half + dir * (e * 2.0 - 1.0) * half * select(0.0, 1.0, point);
    let offset = px / (g.viewport.xy * 0.5) * c.w;
    return vec4<f32>(c.xy + offset, c.z, c.w);
}

@vertex
fn vs_line(@builtin(vertex_index) vi: u32,
           @location(0) a: vec3<f32>,
           @location(1) b: vec3<f32>) -> LineOut {
    var o: LineOut;
    o.pos = line_clip(vi, a, b);
    return o;
}

@fragment
fn fs_line(i: LineOut) -> @location(0) vec4<f32> {
    return finish(layer.color);
}

// Lines coloured by a field: a scalar at each end of the segment.
struct SLineOut {
    @builtin(position) pos: vec4<f32>,
    @location(0) s: f32,
};

@vertex
fn vs_sline(@builtin(vertex_index) vi: u32,
            @location(0) a: vec3<f32>,
            @location(1) b: vec3<f32>,
            @location(2) s: vec2<f32>) -> SLineOut {
    var o: SLineOut;
    o.pos = line_clip(vi, a, b);
    o.s = select(s.x, s.y, line_end(vi) > 0.5);
    return o;
}

@fragment
fn fs_sline(i: SLineOut) -> @location(0) vec4<f32> {
    return finish(vec4<f32>(lut_color(i.s).rgb, layer.color.a));
}

// ------------------------------------------------------------- background

struct BgOut {
    @builtin(position) pos: vec4<f32>,
    @location(0) t: f32,
};

@vertex
fn vs_bg(@builtin(vertex_index) vi: u32) -> BgOut {
    var xy = array<vec2<f32>, 3>(vec2<f32>(-1.0, -1.0), vec2<f32>(3.0, -1.0),
                                 vec2<f32>(-1.0, 3.0));
    var o: BgOut;
    o.pos = vec4<f32>(xy[vi], 0.0, 1.0);
    o.t = xy[vi].y * 0.5 + 0.5;
    return o;
}

@fragment
fn fs_bg(i: BgOut) -> @location(0) vec4<f32> {
    return finish(mix(g.bg_bottom, g.bg_top, clamp(i.t, 0.0, 1.0)));
}

// ------------------------------------------------------------------ text
// One instance per glyph. The anchor is a world point (kind 0), a pixel
// position from the top-left corner (kind 1) or a fraction of the viewport
// from the bottom-left corner (kind 2, as VTK viewport coordinates); the
// glyph quad is in pixels.

struct TextOut {
    @builtin(position) pos: vec4<f32>,
    @location(0) uv: vec2<f32>,
};

@group(2) @binding(0) var atlas: texture_2d<f32>;
@group(2) @binding(1) var atlas_s: sampler;

@vertex
fn vs_text(@builtin(vertex_index) vi: u32,
           @location(0) anchor: vec4<f32>,      // xyz, w = kind
           @location(1) rect: vec4<f32>,        // offset px (x, y down), size px
           @location(2) uv: vec4<f32>) -> TextOut {
    var cx = array<f32, 6>(0.0, 1.0, 0.0, 0.0, 1.0, 1.0);
    var cy = array<f32, 6>(0.0, 0.0, 1.0, 1.0, 0.0, 1.0);
    let fx = cx[vi];
    let fy = cy[vi];
    let c = g.view_proj * vec4<f32>(anchor.xyz, 1.0);
    let w = max(c.w, 1e-6);
    var wbase = (c.xy / w * 0.5 + vec2<f32>(0.5, 0.5)) * g.viewport.xy;
    wbase.y = g.viewport.y - wbase.y;       // to pixels from the top
    let fbase = vec2<f32>(anchor.x * g.viewport.x, (1.0 - anchor.y) * g.viewport.y);
    // `select`, not `if` (Intel GL, see vs_line).
    let world = anchor.w < 0.5;
    let base = select(select(fbase, anchor.xy, anchor.w < 1.5), wbase, world);
    // A world anchor behind the camera: depth 2 is clipped.
    let z = select(0.0, 2.0, world && c.w <= 0.0);
    let px = base + rect.xy + vec2<f32>(fx * rect.z, fy * rect.w);
    let ndc = vec2<f32>(px.x / g.viewport.x * 2.0 - 1.0,
                        1.0 - px.y / g.viewport.y * 2.0);
    var o: TextOut;
    o.pos = vec4<f32>(ndc, z, 1.0);
    o.uv = vec2<f32>(mix(uv.x, uv.z, fx), mix(uv.y, uv.w, fy));
    return o;
}

@fragment
fn fs_text(i: TextOut) -> @location(0) vec4<f32> {
    let a = textureSample(atlas, atlas_s, i.uv).r;
    return finish(vec4<f32>(layer.color.rgb, layer.color.a * a));
}
