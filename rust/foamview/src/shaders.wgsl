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

struct Layer {
    color: vec4<f32>,
    // x: lighting (1/0), y: specular, z: line width px, w: unused
    params: vec4<f32>,
};

@group(0) @binding(0) var<uniform> g: Globals;

// Colours are sRGB numbers. On an sRGB-encoded target (viewport.w = 1) the
// hardware encodes on write, so decode first — or everything looks washed out.
fn finish(c: vec4<f32>) -> vec4<f32> {
    let lin = pow(max(c.rgb, vec3<f32>(0.0)), vec3<f32>(2.2));
    return vec4<f32>(select(c.rgb, lin, g.viewport.w > 0.5), c.a);
}
@group(1) @binding(0) var<uniform> layer: Layer;

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
    var n = normalize(cross(dpdx(i.vpos), dpdy(i.vpos)));
    var v = normalize(-i.vpos);
    if (g.viewport.z < 0.5) {
        v = vec3<f32>(0.0, 0.0, 1.0);
    }
    // Headlight, two-sided: a face seen from behind is lit like its front.
    let d = abs(dot(n, v));
    if (dot(n, v) < 0.0) {
        n = -n;
    }
    let r = reflect(-v, n);
    let s = layer.params.y * pow(max(dot(r, v), 0.0), 30.0);
    let c = base * (0.18 + 0.82 * d) + vec3<f32>(s);
    return finish(vec4<f32>(c, layer.color.a));
}

// ---------------------------------------------------------- thick lines
// One instance per segment; six vertices make a screen-space quad.

struct LineOut {
    @builtin(position) pos: vec4<f32>,
};

@vertex
fn vs_line(@builtin(vertex_index) vi: u32,
           @location(0) a: vec3<f32>,
           @location(1) b: vec3<f32>) -> LineOut {
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
    var ends = array<f32, 6>(0.0, 1.0, 0.0, 0.0, 1.0, 1.0);
    var sides = array<f32, 6>(-1.0, -1.0, 1.0, 1.0, -1.0, 1.0);
    let e = ends[vi];
    let sd = sides[vi];
    let half = layer.params.z * 0.5;
    var c = ca;
    if (e > 0.5) {
        c = cb;
    }
    let px = nrm * sd * half + dir * (e * 2.0 - 1.0) * half * select(0.0, 1.0, point);
    let offset = px / (g.viewport.xy * 0.5) * c.w;
    var o: LineOut;
    o.pos = vec4<f32>(c.xy + offset, c.z, c.w);
    return o;
}

@fragment
fn fs_line(i: LineOut) -> @location(0) vec4<f32> {
    return finish(layer.color);
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
// One instance per glyph. The anchor is a world point (kind 0) or a pixel
// position from the top-left corner (kind 1); the glyph quad is in pixels.

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
    var base: vec2<f32>;
    var z = 0.0;
    if (anchor.w < 0.5) {
        let c = g.view_proj * vec4<f32>(anchor.xyz, 1.0);
        let w = max(c.w, 1e-6);
        base = (c.xy / w * 0.5 + vec2<f32>(0.5, 0.5)) * g.viewport.xy;
        base.y = g.viewport.y - base.y;    // to pixels from the top
        z = 0.0;
        if (c.w <= 0.0) {
            z = 2.0;                        // behind the camera: clipped
        }
    } else {
        base = anchor.xy;
    }
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
