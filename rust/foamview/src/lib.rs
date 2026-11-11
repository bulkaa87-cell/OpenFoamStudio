//! foamview — the 3-D view of OpenFOAM Studio, drawn on the GPU (wgpu).
//!
//! It draws straight into a native Qt window (Win32 HWND) or, without a
//! window, into an off-screen texture (tests, screenshots). The camera is
//! NOT here: Python computes the matrices, so the projection the tests check
//! (fs_view3d) and the picture on screen are the same numbers.
//!
//! Layers, drawn in this order:
//!   background gradient → opaque surfaces → overlay surfaces (problem marks
//!   that lie ON the body: depth bias, so they do not flicker) → lines
//!   (screen-space quads: any width, unlike 1-px GPU lines) → transparent
//!   surfaces → text (world- or pixel-anchored) → axes triad in a corner.
//!
//! Text uses a Windows TrueType font (fontdue), so Cyrillic labels work —
//! VTK had no Cyrillic font and showed boxes.

use std::collections::HashMap;
use std::num::NonZeroIsize;

use numpy::{IntoPyArray, PyArrayMethods, PyReadonlyArray1, PyReadonlyArray2};
use pyo3::exceptions::{PyRuntimeError, PyValueError};
use pyo3::prelude::*;
use wgpu::util::DeviceExt;

mod cpu;

const DEPTH: wgpu::TextureFormat = wgpu::TextureFormat::Depth32Float;
const ATLAS: u32 = 1024;
const TRIAD_PX: u32 = 110;
// Colours in the colour table of a layer (shaders.wgsl: LUT_N, Layer.lut).
const LUT_N: usize = 256;
// A value that is not a number goes to the GPU as this: the shader shows
// the NaN colour for |s| > 1e30. (A NaN test in the shader is not safe:
// some compilers remove `s != s`.)
const NAN_MARK: f32 = 3.0e38;
// Suffix of the layer that holds the shadow of a text layer.
const SHADOW: &str = "\u{0}shadow";

#[repr(C)]
#[derive(Clone, Copy, bytemuck::Pod, bytemuck::Zeroable)]
struct Globals {
    view_proj: [[f32; 4]; 4],
    view: [[f32; 4]; 4],
    viewport: [f32; 4],
    bg_top: [f32; 4],
    bg_bottom: [f32; 4],
}

#[repr(C)]
#[derive(Clone, Copy, bytemuck::Pod, bytemuck::Zeroable)]
struct LayerU {
    color: [f32; 4],
    params: [f32; 4],
    // Scalar layers: low end, 1 / (high - low), colours in lut, 1 = viewport
    // fractions (colour bar).
    scal: [f32; 4],
    nan: [f32; 4],
    lut: [[f32; 4]; LUT_N],
}

/// The uniform of a layer without a field (one colour).
fn lu(color: [f32; 4], params: [f32; 4]) -> LayerU {
    LayerU { color, params, scal: [0.0, 1.0, 1.0, 0.0], nan: [0.0; 4], lut: [[0.0; 4]; LUT_N] }
}

#[repr(C)]
#[derive(Clone, Copy, bytemuck::Pod, bytemuck::Zeroable)]
struct Glyph {
    anchor: [f32; 4],
    rect: [f32; 4],
    uv: [f32; 4],
}

#[derive(Clone, Copy, PartialEq, Eq, Debug)]
enum Pass {
    Opaque,
    Overlay,
    Transparent,
    Line,
    LineOverlay,
    Text,
}

enum Geom {
    Tri { vbuf: wgpu::Buffer, ibuf: wgpu::Buffer, count: u32 },
    Seg { buf: wgpu::Buffer, count: u32 },
    Text { buf: wgpu::Buffer, count: u32 },
    // Triangles coloured by a field (see STri).
    STri(STri),
    // Segments coloured by a field: one scalar at each end (sbuf: 2 floats
    // per segment).
    SSeg { buf: wgpu::Buffer, sbuf: wgpu::Buffer, count: u32 },
}

/// Triangles with a scalar per vertex.
///  * Indexed — point data: shared vertices, one scalar per point. The index
///    buffer is split into parts that fit the buffer limit.
///  * Flat — cell data: three own vertices per triangle with the scalar of
///    the triangle, in parts of whole triangles that fit the buffer limit.
enum STri {
    Indexed { vbuf: wgpu::Buffer, sbuf: wgpu::Buffer, nbuf: Option<wgpu::Buffer>,
              parts: Vec<(wgpu::Buffer, u32)>, n_points: usize },
    Flat { parts: Vec<FlatPart>, n_tris: usize },
}

struct FlatPart {
    vbuf: wgpu::Buffer,
    sbuf: wgpu::Buffer,
    first: usize,         // first triangle of this part
    tris: usize,
}

/// Values for the GPU: NaN and infinity become NAN_MARK.
fn marked(v: impl Iterator<Item = f32>) -> Vec<f32> {
    v.map(|x| if x.is_finite() { x } else { NAN_MARK }).collect()
}

/// The uniform of a layer with a field. lut: (K, 3|4) floats, K <= LUT_N.
fn scalar_uniform(lut: &numpy::ndarray::ArrayView2<'_, f32>, clim: (f32, f32), opacity: f32,
                  nan: (f32, f32, f32, f32), params: [f32; 4], screen: bool)
                  -> PyResult<LayerU> {
    let k = lut.nrows();
    if k == 0 || k > LUT_N || !(lut.ncols() == 3 || lut.ncols() == 4) {
        return Err(PyValueError::new_err(format!("lut (K,3|4), 1 <= K <= {LUT_N}")));
    }
    let mut table = [[0.0f32; 4]; LUT_N];
    for (i, row) in lut.rows().into_iter().enumerate() {
        table[i] = [row[0], row[1], row[2], if row.len() > 3 { row[3] } else { 1.0 }];
    }
    let span = clim.1 - clim.0;
    let inv = if span.abs() > f32::MIN_POSITIVE && span.is_finite() { 1.0 / span } else { 0.0 };
    let mut u = lu([1.0, 1.0, 1.0, opacity], params);
    u.scal = [clim.0, inv, k as f32, if screen { 1.0 } else { 0.0 }];
    u.nan = [nan.0, nan.1, nan.2, nan.3];
    u.lut = table;
    Ok(u)
}

struct Layer {
    geom: Geom,
    pass: Pass,
    // The uniform as it is on the GPU: set_colormap changes a part of it.
    u: Box<LayerU>,
    ubuf: wgpu::Buffer,
    bind: wgpu::BindGroup,
    visible: bool,
    order: u64,
}

#[derive(Clone, Copy)]
struct GlyphInfo {
    uv: [f32; 4],
    w: f32,
    h: f32,
    xmin: f32,
    ymin: f32,
    advance: f32,
}

struct Atlas {
    tex: wgpu::Texture,
    bind: wgpu::BindGroup,
    x: u32,
    y: u32,
    row_h: u32,
    glyphs: HashMap<(char, u32), GlyphInfo>,
}

struct Pipelines {
    bg: wgpu::RenderPipeline,
    opaque: wgpu::RenderPipeline,
    overlay: wgpu::RenderPipeline,
    transparent: wgpu::RenderPipeline,
    line: wgpu::RenderPipeline,
    line_overlay: wgpu::RenderPipeline,
    text: wgpu::RenderPipeline,
    s_opaque: wgpu::RenderPipeline,
    s_overlay: wgpu::RenderPipeline,
    s_transparent: wgpu::RenderPipeline,
    s_line: wgpu::RenderPipeline,
    s_line_overlay: wgpu::RenderPipeline,
}

struct Targets {
    w: u32,
    h: u32,
    msaa: Option<wgpu::TextureView>,
    depth: wgpu::TextureView,
    // Resolve target for screenshots / the off-screen mode.
    off: wgpu::Texture,
    off_view: wgpu::TextureView,
}

type Raw = (raw_window_handle::RawDisplayHandle, raw_window_handle::RawWindowHandle);

#[cfg(windows)]
#[link(name = "user32")]
unsafe extern "system" {
    fn GetWindowLongPtrW(hwnd: isize, index: i32) -> isize;
}

#[cfg(windows)]
#[link(name = "kernel32")]
unsafe extern "system" {
    fn GetModuleHandleW(name: *const u16) -> isize;
}

/// The HINSTANCE of a Win32 window (GWLP_HINSTANCE); a window made with
/// hInstance = NULL has none — then the module of this process.
#[cfg(windows)]
fn win32_hinstance(hwnd: isize) -> Option<NonZeroIsize> {
    const GWLP_HINSTANCE: i32 = -6;
    NonZeroIsize::new(unsafe { GetWindowLongPtrW(hwnd, GWLP_HINSTANCE) })
        .or_else(|| NonZeroIsize::new(unsafe { GetModuleHandleW(std::ptr::null()) }))
}

#[cfg(not(windows))]
fn win32_hinstance(_hwnd: isize) -> Option<NonZeroIsize> {
    None
}

#[pyclass(unsendable)]
pub struct Renderer {
    instance: wgpu::Instance,
    adapter: wgpu::Adapter,
    device: wgpu::Device,
    queue: wgpu::Queue,
    surface: Option<wgpu::Surface<'static>>,
    // The window, to make the surface again when the system loses it.
    raw: Option<Raw>,
    // Set by wgpu's error / device-lost callbacks. A GPU error (or a TDR —
    // this laptop already had BSOD 0x116) becomes a Python RuntimeError
    // from the next call instead of a panic Python cannot catch.
    error: std::sync::Arc<std::sync::Mutex<Option<String>>>,
    reconfigure: bool,
    // The target is sRGB-encoded: colours are decoded in the shader first.
    srgb: bool,
    // Device pixels per logical pixel (HiDPI): line widths, text, triad.
    scale: f32,
    kind: String,
    format: wgpu::TextureFormat,
    samples: u32,
    targets: Targets,
    pipes: Pipelines,
    layer_layout: wgpu::BindGroupLayout,
    globals_buf: wgpu::Buffer,
    globals_bind: wgpu::BindGroup,
    triad_buf: wgpu::Buffer,
    triad_bind: wgpu::BindGroup,
    layers: HashMap<String, Layer>,
    triad: Vec<Layer>,
    // Bound at group 1 for the background (its pipeline layout has the slot).
    default_bind: Option<(wgpu::Buffer, wgpu::BindGroup)>,
    atlas: Atlas,
    font: Option<fontdue::Font>,
    bg: ([f32; 4], [f32; 4]),
    counter: u64,
    info: String,
}

fn rgba(c: (f32, f32, f32, f32)) -> [f32; 4] {
    [c.0, c.1, c.2, c.3]
}

/// Row-major 4×4 from Python → column-major for WGSL.
fn mat(m: &[f32]) -> PyResult<[[f32; 4]; 4]> {
    if m.len() != 16 {
        return Err(PyValueError::new_err("a 4×4 matrix needs 16 numbers"));
    }
    let mut out = [[0.0f32; 4]; 4];
    for r in 0..4 {
        for c in 0..4 {
            out[c][r] = m[r * 4 + c];
        }
    }
    Ok(out)
}

fn make_targets(device: &wgpu::Device, format: wgpu::TextureFormat, samples: u32,
                w: u32, h: u32) -> Targets {
    let w = w.max(1);
    let h = h.max(1);
    let size = wgpu::Extent3d { width: w, height: h, depth_or_array_layers: 1 };
    let msaa = (samples > 1).then(|| {
        device.create_texture(&wgpu::TextureDescriptor {
            label: Some("msaa"), size, mip_level_count: 1, sample_count: samples,
            dimension: wgpu::TextureDimension::D2, format,
            usage: wgpu::TextureUsages::RENDER_ATTACHMENT, view_formats: &[],
        }).create_view(&Default::default())
    });
    let depth = device.create_texture(&wgpu::TextureDescriptor {
        label: Some("depth"), size, mip_level_count: 1, sample_count: samples,
        dimension: wgpu::TextureDimension::D2, format: DEPTH,
        usage: wgpu::TextureUsages::RENDER_ATTACHMENT, view_formats: &[],
    }).create_view(&Default::default());
    let off = device.create_texture(&wgpu::TextureDescriptor {
        label: Some("offscreen"), size, mip_level_count: 1, sample_count: 1,
        dimension: wgpu::TextureDimension::D2, format,
        usage: wgpu::TextureUsages::RENDER_ATTACHMENT | wgpu::TextureUsages::COPY_SRC,
        view_formats: &[],
    });
    let off_view = off.create_view(&Default::default());
    Targets { w, h, msaa, depth, off, off_view }
}

fn pipeline(device: &wgpu::Device, layout: &wgpu::PipelineLayout, module: &wgpu::ShaderModule,
            format: wgpu::TextureFormat, samples: u32, vs: &str, fs: &str,
            buffers: &[Option<wgpu::VertexBufferLayout>], blend: bool, depth_write: bool,
            compare: wgpu::CompareFunction, bias: bool) -> wgpu::RenderPipeline {
    device.create_render_pipeline(&wgpu::RenderPipelineDescriptor {
        label: Some(vs),
        layout: Some(layout),
        vertex: wgpu::VertexState {
            module, entry_point: Some(vs), compilation_options: Default::default(), buffers,
        },
        primitive: wgpu::PrimitiveState {
            topology: wgpu::PrimitiveTopology::TriangleList,
            cull_mode: None,
            ..Default::default()
        },
        depth_stencil: Some(wgpu::DepthStencilState {
            format: DEPTH,
            depth_write_enabled: Some(depth_write),
            depth_compare: Some(compare),
            stencil: Default::default(),
            bias: if bias {
                wgpu::DepthBiasState { constant: -8, slope_scale: -2.0, clamp: 0.0 }
            } else {
                Default::default()
            },
        }),
        multisample: wgpu::MultisampleState { count: samples, mask: !0, alpha_to_coverage_enabled: false },
        fragment: Some(wgpu::FragmentState {
            module, entry_point: Some(fs), compilation_options: Default::default(),
            targets: &[Some(wgpu::ColorTargetState {
                format,
                blend: blend.then_some(wgpu::BlendState::ALPHA_BLENDING),
                write_mask: wgpu::ColorWrites::ALL,
            })],
        }),
        multiview_mask: None,
        cache: None,
    })
}

impl Renderer {
    fn layer_bind(&self, u: LayerU) -> (wgpu::Buffer, wgpu::BindGroup) {
        let ubuf = self.device.create_buffer_init(&wgpu::util::BufferInitDescriptor {
            label: Some("layer"), contents: bytemuck::bytes_of(&u),
            usage: wgpu::BufferUsages::UNIFORM | wgpu::BufferUsages::COPY_DST,
        });
        let bind = self.device.create_bind_group(&wgpu::BindGroupDescriptor {
            label: Some("layer"), layout: &self.layer_layout,
            entries: &[wgpu::BindGroupEntry { binding: 0, resource: ubuf.as_entire_binding() }],
        });
        (ubuf, bind)
    }

    fn put(&mut self, name: &str, geom: Geom, pass: Pass, u: LayerU) {
        let visible = self.layers.get(name).map_or(true, |l| l.visible);
        let (ubuf, bind) = self.layer_bind(u);
        self.counter += 1;
        self.layers.insert(name.to_string(),
                           Layer { geom, pass, u: Box::new(u), ubuf, bind, visible,
                                   order: self.counter });
    }

    fn glyph(&mut self, c: char, px: u32) -> Option<GlyphInfo> {
        if let Some(g) = self.atlas.glyphs.get(&(c, px)) {
            return Some(*g);
        }
        let font = self.font.as_ref()?;
        let (m, bitmap) = font.rasterize(c, px as f32);
        let (w, h) = (m.width as u32, m.height as u32);
        if self.atlas.x + w + 1 > ATLAS {
            self.atlas.x = 0;
            self.atlas.y += self.atlas.row_h + 1;
            self.atlas.row_h = 0;
        }
        if self.atlas.y + h + 1 > ATLAS {
            // Full: start over (labels are re-laid out on every change).
            self.atlas.glyphs.clear();
            self.atlas.x = 0;
            self.atlas.y = 0;
            self.atlas.row_h = 0;
        }
        let (x, y) = (self.atlas.x, self.atlas.y);
        if w > 0 && h > 0 {
            self.queue.write_texture(
                wgpu::TexelCopyTextureInfo {
                    texture: &self.atlas.tex, mip_level: 0,
                    origin: wgpu::Origin3d { x, y, z: 0 }, aspect: wgpu::TextureAspect::All,
                },
                &bitmap,
                wgpu::TexelCopyBufferLayout { offset: 0, bytes_per_row: Some(w), rows_per_image: Some(h) },
                wgpu::Extent3d { width: w, height: h, depth_or_array_layers: 1 },
            );
        }
        self.atlas.x += w + 1;
        self.atlas.row_h = self.atlas.row_h.max(h);
        let f = ATLAS as f32;
        let g = GlyphInfo {
            uv: [x as f32 / f, y as f32 / f, (x + w) as f32 / f, (y + h) as f32 / f],
            w: w as f32, h: h as f32, xmin: m.xmin as f32, ymin: m.ymin as f32,
            advance: m.advance_width,
        };
        self.atlas.glyphs.insert((c, px), g);
        Some(g)
    }

    /// align: 0 = text starts at the anchor, 0.5 = centred, 1 = ends there.
    fn layout_text(&mut self, anchor: [f32; 4], text: &str, px: u32, align: f32,
                   offset: (f32, f32)) -> Vec<Glyph> {
        let ascent = self.font.as_ref()
            .and_then(|f| f.horizontal_line_metrics(px as f32))
            .map(|m| m.ascent).unwrap_or(px as f32 * 0.8);
        let mut out = Vec::new();
        let mut lines: Vec<&str> = text.split('\n').collect();
        if lines.is_empty() {
            lines.push("");
        }
        let line_h = px as f32 * 1.25;
        for (li, line) in lines.iter().enumerate() {
            let glyphs: Vec<(char, Option<GlyphInfo>)> =
                line.chars().map(|c| (c, self.glyph(c, px))).collect();
            let width: f32 = glyphs.iter().map(|(_, g)| g.map_or(px as f32 * 0.5, |g| g.advance)).sum();
            let mut pen = -width * align + offset.0;
            let base = ascent + li as f32 * line_h + offset.1;
            for (_, g) in glyphs {
                if let Some(g) = g {
                    if g.w > 0.0 {
                        out.push(Glyph {
                            anchor,
                            rect: [pen + g.xmin, base - (g.h + g.ymin), g.w, g.h],
                            uv: g.uv,
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

    /// Scalars of segments as pairs (both ends): 2M values as they are, M
    /// values (one per segment) twice.
    fn pairs(sv: Vec<f32>, m: usize) -> PyResult<Vec<f32>> {
        if sv.len() == 2 * m {
            Ok(marked(sv.into_iter()))
        } else if sv.len() == m {
            Ok(marked(sv.into_iter().flat_map(|x| [x, x])))
        } else {
            Err(PyValueError::new_err(format!(
                "scalars: {} (two per segment) or {m} values, not {}", 2 * m, sv.len())))
        }
    }

    /// The largest buffer to make: the device limit, but not more than
    /// 256 MiB (a big mesh goes in parts).
    fn max_buffer(&self) -> u64 {
        self.device.limits().max_buffer_size.clamp(1 << 16, 1 << 28)
    }

    fn vbuf(&self, label: &str, data: &[f32], dst: bool) -> wgpu::Buffer {
        let mut usage = wgpu::BufferUsages::VERTEX;
        if dst {
            usage |= wgpu::BufferUsages::COPY_DST;
        }
        self.device.create_buffer_init(&wgpu::util::BufferInitDescriptor {
            label: Some(label), contents: bytemuck::cast_slice(data), usage,
        })
    }

    fn check(&self) -> PyResult<()> {
        if let Some(e) = self.error.lock().ok().and_then(|g| g.clone()) {
            return Err(PyRuntimeError::new_err(format!("GPU: {e}")));
        }
        Ok(())
    }

    fn triad_px(&self) -> u32 {
        (TRIAD_PX as f32 * self.scale).round().max(40.0) as u32
    }

    /// The surface again, after the system lost it (driver reset, display).
    fn recreate_surface(&mut self) {
        let Some((d, w)) = self.raw else { return };
        let target = wgpu::SurfaceTargetUnsafe::RawHandle { raw_display_handle: Some(d),
                                                            raw_window_handle: w };
        if let Ok(s) = unsafe { self.instance.create_surface_unsafe(target) } {
            self.surface = Some(s);
            self.configure();
        }
    }

    fn draw(&mut self, view_proj: &[f32], view: &[f32], perspective: bool,
            triad: Option<&[f32]>, to_surface: bool) -> PyResult<()> {
        self.check()?;
        let t = &self.targets;
        let srgb = if self.srgb { 1.0 } else { 0.0 };
        let gl = Globals {
            view_proj: mat(view_proj)?, view: mat(view)?,
            viewport: [t.w as f32, t.h as f32, if perspective { 1.0 } else { 0.0 }, srgb],
            bg_top: self.bg.0, bg_bottom: self.bg.1,
        };
        self.queue.write_buffer(&self.globals_buf, 0, bytemuck::bytes_of(&gl));
        let tpx = self.triad_px();
        if let Some(tm) = triad {
            let tg = Globals {
                view_proj: mat(tm)?, view: mat(tm)?,
                viewport: [tpx as f32, tpx as f32, 0.0, srgb],
                bg_top: self.bg.0, bg_bottom: self.bg.1,
            };
            self.queue.write_buffer(&self.triad_buf, 0, bytemuck::bytes_of(&tg));
        }
        if to_surface && self.reconfigure {
            self.reconfigure = false;
            self.configure();
        }
        let frame = if to_surface {
            let got = match self.surface.as_ref() {
                Some(s) => s.get_current_texture(),
                None => return Ok(()),
            };
            match got {
                wgpu::CurrentSurfaceTexture::Success(f) => Some(f),
                wgpu::CurrentSurfaceTexture::Suboptimal(f) => {
                    self.reconfigure = true;          // after this frame
                    Some(f)
                }
                wgpu::CurrentSurfaceTexture::Outdated => {
                    self.configure();
                    return Ok(());
                }
                wgpu::CurrentSurfaceTexture::Lost => {
                    self.recreate_surface();
                    return Ok(());
                }
                // Timeout, minimised window: no frame now, the next paint retries.
                _ => return Ok(()),
            }
        } else {
            None
        };
        let frame_view = frame.as_ref().map(|f| f.texture.create_view(&Default::default()));
        let resolve = frame_view.as_ref().unwrap_or(&self.targets.off_view);
        let mut enc = self.device.create_command_encoder(&Default::default());
        {
            let (view, resolve_target) = match &self.targets.msaa {
                Some(m) => (m, Some(resolve)),
                None => (resolve, None),
            };
            let mut rp = enc.begin_render_pass(&wgpu::RenderPassDescriptor {
                label: Some("frame"),
                color_attachments: &[Some(wgpu::RenderPassColorAttachment {
                    view, depth_slice: None, resolve_target,
                    ops: wgpu::Operations { load: wgpu::LoadOp::Clear(wgpu::Color::BLACK),
                                            store: wgpu::StoreOp::Store },
                })],
                depth_stencil_attachment: Some(wgpu::RenderPassDepthStencilAttachment {
                    view: &self.targets.depth,
                    depth_ops: Some(wgpu::Operations { load: wgpu::LoadOp::Clear(1.0),
                                                       store: wgpu::StoreOp::Store }),
                    stencil_ops: None,
                }),
                timestamp_writes: None, occlusion_query_set: None, multiview_mask: None,
            });
            rp.set_bind_group(0, &self.globals_bind, &[0]);
            rp.set_bind_group(2, &self.atlas.bind, &[]);
            if let Some((_, b)) = &self.default_bind {
                rp.set_bind_group(1, b, &[0]);
            }
            rp.set_pipeline(&self.pipes.bg);
            rp.draw(0..3, 0..1);
            let mut order: Vec<&Layer> = self.layers.values().filter(|l| l.visible).collect();
            order.sort_by_key(|l| l.order);
            for pass in [Pass::Opaque, Pass::Overlay, Pass::Line, Pass::LineOverlay,
                         Pass::Transparent, Pass::Text] {
                for l in order.iter().filter(|l| l.pass == pass) {
                    draw_layer(&mut rp, &self.pipes, l);
                }
            }
            if triad.is_some() && self.targets.w > tpx + 6 && self.targets.h > tpx + 6 {
                rp.set_viewport(6.0, (self.targets.h - tpx - 6) as f32,
                                tpx as f32, tpx as f32, 0.0, 1.0);
                rp.set_bind_group(0, &self.triad_bind, &[0]);
                for l in &self.triad {
                    draw_layer(&mut rp, &self.pipes, l);
                }
            }
        }
        self.queue.submit(Some(enc.finish()));
        if let Some(f) = frame {
            self.queue.present(f);
        }
        self.check()
    }

    fn configure(&mut self) {
        if let Some(s) = &self.surface {
            s.configure(&self.device, &wgpu::SurfaceConfiguration {
                usage: wgpu::TextureUsages::RENDER_ATTACHMENT,
                format: self.format,
                width: self.targets.w, height: self.targets.h,
                present_mode: wgpu::PresentMode::AutoVsync,
                desired_maximum_frame_latency: 2,
                alpha_mode: wgpu::CompositeAlphaMode::Auto,
                view_formats: vec![],
                color_space: wgpu::SurfaceColorSpace::Auto,
            });
        }
    }

    fn build_triad(&mut self) {
        let axes = [([1.0, 0.0, 0.0], [0.86, 0.24, 0.22], "X"),
                    ([0.0, 1.0, 0.0], [0.93, 0.83, 0.25], "Y"),
                    ([0.0, 0.0, 1.0], [0.30, 0.55, 0.95], "Z")];
        let mut out = Vec::new();
        for (d, c, name) in axes {
            let seg: [f32; 6] = [0.0, 0.0, 0.0, d[0] * 0.8, d[1] * 0.8, d[2] * 0.8];
            let buf = self.device.create_buffer_init(&wgpu::util::BufferInitDescriptor {
                label: Some("triad"), contents: bytemuck::cast_slice(&seg),
                usage: wgpu::BufferUsages::VERTEX,
            });
            let (ubuf, bind) = self.layer_bind(lu([c[0], c[1], c[2], 1.0],
                                                  [0.0, 0.0, 3.0 * self.scale, 0.0]));
            let u = Box::new(lu([c[0], c[1], c[2], 1.0], [0.0, 0.0, 3.0 * self.scale, 0.0]));
            out.push(Layer { geom: Geom::Seg { buf, count: 1 }, pass: Pass::Line, u, ubuf, bind,
                             visible: true, order: 0 });
            let px = (15.0 * self.scale).round() as u32;
            let glyphs = self.layout_text([d[0] * 0.95, d[1] * 0.95, d[2] * 0.95, 0.0], name, px,
                                          0.5, (0.0, -8.0 * self.scale));
            if !glyphs.is_empty() {
                let buf = self.device.create_buffer_init(&wgpu::util::BufferInitDescriptor {
                    label: Some("triad text"), contents: bytemuck::cast_slice(&glyphs),
                    usage: wgpu::BufferUsages::VERTEX,
                });
                let (ubuf, bind) = self.layer_bind(lu([c[0], c[1], c[2], 1.0], [0.0; 4]));
                let u = Box::new(lu([c[0], c[1], c[2], 1.0], [0.0; 4]));
                out.push(Layer { geom: Geom::Text { buf, count: glyphs.len() as u32 },
                                 pass: Pass::Text, u, ubuf, bind, visible: true, order: 0 });
            }
        }
        self.triad = out;
    }
}

fn draw_layer<'a>(rp: &mut wgpu::RenderPass<'a>, p: &'a Pipelines, l: &'a Layer) {
    rp.set_bind_group(1, &l.bind, &[0]);
    match &l.geom {
        Geom::Tri { vbuf, ibuf, count } => {
            rp.set_pipeline(match l.pass {
                Pass::Overlay => &p.overlay,
                Pass::Transparent => &p.transparent,
                _ => &p.opaque,
            });
            rp.set_vertex_buffer(0, vbuf.slice(..));
            rp.set_index_buffer(ibuf.slice(..), wgpu::IndexFormat::Uint32);
            rp.draw_indexed(0..*count, 0, 0..1);
        }
        Geom::Seg { buf, count } => {
            rp.set_pipeline(if l.pass == Pass::LineOverlay { &p.line_overlay } else { &p.line });
            rp.set_vertex_buffer(0, buf.slice(..));
            rp.draw(0..6, 0..*count);
        }
        Geom::Text { buf, count } => {
            rp.set_pipeline(&p.text);
            rp.set_vertex_buffer(0, buf.slice(..));
            rp.draw(0..6, 0..*count);
        }
        Geom::STri(st) => {
            rp.set_pipeline(match l.pass {
                Pass::Overlay => &p.s_overlay,
                Pass::Transparent => &p.s_transparent,
                _ => &p.s_opaque,
            });
            match st {
                STri::Indexed { vbuf, sbuf, nbuf, parts, .. } => {
                    rp.set_vertex_buffer(0, vbuf.slice(..));
                    rp.set_vertex_buffer(1, sbuf.slice(..));
                    // Without normals the slot gets the positions again
                    // (the shader then uses the face normal).
                    rp.set_vertex_buffer(2, nbuf.as_ref().unwrap_or(vbuf).slice(..));
                    for (ibuf, count) in parts {
                        rp.set_index_buffer(ibuf.slice(..), wgpu::IndexFormat::Uint32);
                        rp.draw_indexed(0..*count, 0, 0..1);
                    }
                }
                STri::Flat { parts, .. } => {
                    for part in parts {
                        rp.set_vertex_buffer(0, part.vbuf.slice(..));
                        rp.set_vertex_buffer(1, part.sbuf.slice(..));
                        rp.set_vertex_buffer(2, part.vbuf.slice(..));
                        rp.draw(0..(part.tris * 3) as u32, 0..1);
                    }
                }
            }
        }
        Geom::SSeg { buf, sbuf, count } => {
            rp.set_pipeline(if l.pass == Pass::LineOverlay { &p.s_line_overlay } else { &p.s_line });
            rp.set_vertex_buffer(0, buf.slice(..));
            rp.set_vertex_buffer(1, sbuf.slice(..));
            rp.draw(0..6, 0..*count);
        }
    }
}

#[pymethods]
impl Renderer {
    /// `hwnd` — a native window (Qt `winId()`), or None for off-screen.
    /// `kind` — what the handle is: "win32" (HWND) or "appkit" (NSView*).
    /// On Linux (X11 / Wayland) there is no window mode yet: pass None.
    /// `backend` ("vulkan", "dx12", "metal", "gl") and `adapter` (a part of
    /// the adapter name) select one adapter — the choice of
    /// fs_view3d.select_level(). Without them: WGPU_BACKEND /
    /// WGPU_ADAPTER_NAME, else the high-performance adapter of DX12 / Vulkan /
    /// Metal.
    #[new]
    #[pyo3(signature = (hwnd, width, height, font_path=None, samples=4, kind="win32",
                        backend=None, adapter=None))]
    #[allow(clippy::too_many_arguments)]
    fn new(hwnd: Option<isize>, width: u32, height: u32, font_path: Option<String>,
           samples: u32, kind: &str, backend: Option<String>, adapter: Option<String>)
           -> PyResult<Self> {
        let mut desc = wgpu::InstanceDescriptor::new_without_display_handle();
        // NOT GL by default: wgpu's GL backend makes its own WGL context
        // current while it looks for adapters, and VTK (the results view in
        // the same window) then crashes with an access violation in its
        // OpenGL calls. DX12 / Vulkan / Metal; WGPU_BACKEND overrides (tests).
        let backends = backend.as_deref().map(wgpu::Backends::from_comma_list)
            .filter(|b| !b.is_empty())
            .or_else(wgpu::Backends::from_env)
            .unwrap_or(wgpu::Backends::PRIMARY);
        desc.backends = backends;
        let instance = wgpu::Instance::new(desc);
        let raw: Option<Raw> = match hwnd {
            Some(h) => {
                let nz = NonZeroIsize::new(h).ok_or_else(|| PyValueError::new_err("hwnd = 0"))?;
                Some(match kind {
                    "appkit" => {
                        let view = std::ptr::NonNull::new(nz.get() as *mut std::ffi::c_void)
                            .ok_or_else(|| PyValueError::new_err("NSView = 0"))?;
                        (raw_window_handle::RawDisplayHandle::AppKit(
                             raw_window_handle::AppKitDisplayHandle::new()),
                         raw_window_handle::RawWindowHandle::AppKit(
                             raw_window_handle::AppKitWindowHandle::new(view)))
                    }
                    "win32" => {
                        // Vulkan on Win32 needs the HINSTANCE of the window
                        // (DX12 does not). Without it the surface failed with
                        // "Vulkan requires ... hinstance", the widget fell back
                        // to the off-screen path and the view was stretched
                        // and jagged (2026-11-09, after the adapter choice
                        // started to pass backend="vulkan").
                        let mut wh = raw_window_handle::Win32WindowHandle::new(nz);
                        wh.hinstance = win32_hinstance(nz.get());
                        (raw_window_handle::RawDisplayHandle::Windows(
                             raw_window_handle::WindowsDisplayHandle::new()),
                         raw_window_handle::RawWindowHandle::Win32(wh))
                    }
                    other => return Err(PyValueError::new_err(format!(
                        "window kind {other}: only win32 / appkit (use off-screen)"))),
                })
            }
            None => None,
        };
        let surface = match raw {
            Some((d, w)) => {
                let target = wgpu::SurfaceTargetUnsafe::RawHandle { raw_display_handle: Some(d),
                                                                    raw_window_handle: w };
                Some(unsafe { instance.create_surface_unsafe(target) }
                     .map_err(|e| PyRuntimeError::new_err(format!("surface: {e}")))?)
            }
            None => None,
        };
        // WGPU_ADAPTER_NAME (a part of the adapter name, any case) selects
        // one adapter, as WGPU_BACKEND selects the backend (gpucheck: Intel,
        // WARP). Not wgpu::util::initialize_adapter_from_env: it panics when
        // no name matches, and a panic is a PanicException in Python.
        let wanted = adapter.or_else(|| std::env::var("WGPU_ADAPTER_NAME").ok())
            .map(|s| s.trim().to_lowercase()).filter(|s| !s.is_empty());
        let adapter = match wanted {
            Some(name) => pollster::block_on(instance.enumerate_adapters(backends))
                .into_iter()
                .filter(|a| a.get_info().name.to_lowercase().contains(&name))
                .find(|a| surface.as_ref().map_or(true, |s| a.is_surface_supported(s)))
                .ok_or_else(|| PyRuntimeError::new_err(format!(
                    "no GPU adapter: WGPU_ADAPTER_NAME={name:?} matches no adapter")))?,
            None => pollster::block_on(instance.request_adapter(&wgpu::RequestAdapterOptions {
                power_preference: wgpu::PowerPreference::HighPerformance,
                force_fallback_adapter: false,
                compatible_surface: surface.as_ref(),
                ..Default::default()
            })).map_err(|e| PyRuntimeError::new_err(format!("no GPU adapter: {e}")))?,
        };
        let ai = adapter.get_info();
        let info = format!("{} ({:?})", ai.name, ai.backend);
        let kind_s = format!("{:?}", ai.device_type);
        // The adapter's own limits, not the defaults (256 MiB buffers, 8192 px
        // textures): a big mesh or a 4K-wide window must not panic.
        let (device, queue) = pollster::block_on(adapter.request_device(&wgpu::DeviceDescriptor {
            label: Some("foamview"),
            required_limits: adapter.limits(),
            ..Default::default()
        })).map_err(|e| PyRuntimeError::new_err(format!("device: {e}")))?;
        let error = std::sync::Arc::new(std::sync::Mutex::new(None::<String>));
        {
            let e1 = error.clone();
            device.on_uncaptured_error(std::sync::Arc::new(move |err: wgpu::Error| {
                if let Ok(mut g) = e1.lock() {
                    g.get_or_insert_with(|| err.to_string());
                }
            }));
            let e2 = error.clone();
            device.set_device_lost_callback(move |reason, msg| {
                if let Ok(mut g) = e2.lock() {
                    g.get_or_insert_with(|| format!("device lost ({reason:?}): {msg}"));
                }
            });
        }
        // 8-bit RGBA/BGRA only (the screenshot reads 4 bytes a pixel). A
        // plain (non-sRGB) target first: colours are sRGB numbers and go in
        // as is; an sRGB-only surface gets them decoded in the shader.
        let pick = [wgpu::TextureFormat::Bgra8Unorm, wgpu::TextureFormat::Rgba8Unorm,
                    wgpu::TextureFormat::Bgra8UnormSrgb, wgpu::TextureFormat::Rgba8UnormSrgb];
        let format = match &surface {
            Some(s) => {
                let caps = s.get_capabilities(&adapter);
                pick.iter().copied().find(|f| caps.formats.contains(f))
                    .ok_or_else(|| PyRuntimeError::new_err(format!(
                        "no 8-bit RGBA surface format: {:?}", caps.formats)))?
            }
            None => wgpu::TextureFormat::Rgba8Unorm,
        };
        let srgb = format.is_srgb();
        // 1 and 4 are the only sample counts WebGPU guarantees (when the
        // format says so); anything else asked for is treated as 4.
        let flags = adapter.get_texture_format_features(format).flags;
        let dflags = adapter.get_texture_format_features(DEPTH).flags;
        let samples = if samples > 1 && flags.sample_count_supported(4)
            && dflags.sample_count_supported(4) { 4 } else { 1 };

        let module = device.create_shader_module(wgpu::ShaderModuleDescriptor {
            label: Some("foamview"),
            source: wgpu::ShaderSource::Wgsl(include_str!("shaders.wgsl").into()),
        });
        // Dynamic offset (always 0), on purpose: DX12 then binds each uniform
        // buffer as a root CBV, not through a descriptor table. On WARP
        // (Microsoft Basic Render Driver) every table of one layout read the
        // buffer of the LAST bind group made with that layout: globals read
        // the empty triad buffer and the frame was black (measured 2026-11-09).
        // Limit: 2 of the 8 dynamic uniform buffers that wgpu guarantees.
        let uniform_entry = |vis| wgpu::BindGroupLayoutEntry {
            binding: 0, visibility: vis,
            ty: wgpu::BindingType::Buffer { ty: wgpu::BufferBindingType::Uniform,
                                            has_dynamic_offset: true, min_binding_size: None },
            count: None,
        };
        let globals_layout = device.create_bind_group_layout(&wgpu::BindGroupLayoutDescriptor {
            label: Some("globals"), entries: &[uniform_entry(wgpu::ShaderStages::VERTEX_FRAGMENT)],
        });
        let layer_layout = device.create_bind_group_layout(&wgpu::BindGroupLayoutDescriptor {
            label: Some("layer"), entries: &[uniform_entry(wgpu::ShaderStages::VERTEX_FRAGMENT)],
        });
        let atlas_layout = device.create_bind_group_layout(&wgpu::BindGroupLayoutDescriptor {
            label: Some("atlas"),
            entries: &[
                wgpu::BindGroupLayoutEntry {
                    binding: 0, visibility: wgpu::ShaderStages::FRAGMENT,
                    ty: wgpu::BindingType::Texture {
                        sample_type: wgpu::TextureSampleType::Float { filterable: true },
                        view_dimension: wgpu::TextureViewDimension::D2, multisampled: false,
                    },
                    count: None,
                },
                wgpu::BindGroupLayoutEntry {
                    binding: 1, visibility: wgpu::ShaderStages::FRAGMENT,
                    ty: wgpu::BindingType::Sampler(wgpu::SamplerBindingType::Filtering),
                    count: None,
                },
            ],
        });
        let layout = device.create_pipeline_layout(&wgpu::PipelineLayoutDescriptor {
            label: Some("foamview"),
            bind_group_layouts: &[Some(&globals_layout), Some(&layer_layout), Some(&atlas_layout)],
            immediate_size: 0,
        });
        let pos = [Some(wgpu::VertexBufferLayout {
            array_stride: 12, step_mode: wgpu::VertexStepMode::Vertex,
            attributes: &wgpu::vertex_attr_array![0 => Float32x3],
        })];
        let seg = [Some(wgpu::VertexBufferLayout {
            array_stride: 24, step_mode: wgpu::VertexStepMode::Instance,
            attributes: &wgpu::vertex_attr_array![0 => Float32x3, 1 => Float32x3],
        })];
        let glyph = [Some(wgpu::VertexBufferLayout {
            array_stride: 48, step_mode: wgpu::VertexStepMode::Instance,
            attributes: &wgpu::vertex_attr_array![0 => Float32x4, 1 => Float32x4, 2 => Float32x4],
        })];
        let spos = [
            Some(wgpu::VertexBufferLayout {
                array_stride: 12, step_mode: wgpu::VertexStepMode::Vertex,
                attributes: &wgpu::vertex_attr_array![0 => Float32x3],
            }),
            Some(wgpu::VertexBufferLayout {
                array_stride: 4, step_mode: wgpu::VertexStepMode::Vertex,
                attributes: &wgpu::vertex_attr_array![1 => Float32],
            }),
            Some(wgpu::VertexBufferLayout {
                array_stride: 12, step_mode: wgpu::VertexStepMode::Vertex,
                attributes: &wgpu::vertex_attr_array![2 => Float32x3],
            }),
        ];
        let sseg = [
            Some(wgpu::VertexBufferLayout {
                array_stride: 24, step_mode: wgpu::VertexStepMode::Instance,
                attributes: &wgpu::vertex_attr_array![0 => Float32x3, 1 => Float32x3],
            }),
            Some(wgpu::VertexBufferLayout {
                array_stride: 8, step_mode: wgpu::VertexStepMode::Instance,
                attributes: &wgpu::vertex_attr_array![2 => Float32x2],
            }),
        ];
        use wgpu::CompareFunction as C;
        let pipes = Pipelines {
            s_opaque: pipeline(&device, &layout, &module, format, samples, "vs_scalar", "fs_scalar",
                               &spos, false, true, C::Less, false),
            s_overlay: pipeline(&device, &layout, &module, format, samples, "vs_scalar",
                                "fs_scalar", &spos, true, true, C::LessEqual, true),
            s_transparent: pipeline(&device, &layout, &module, format, samples, "vs_scalar",
                                    "fs_scalar", &spos, true, false, C::Less, false),
            s_line: pipeline(&device, &layout, &module, format, samples, "vs_sline", "fs_sline",
                             &sseg, true, true, C::LessEqual, false),
            s_line_overlay: pipeline(&device, &layout, &module, format, samples, "vs_sline",
                                     "fs_sline", &sseg, true, true, C::LessEqual, true),
            bg: pipeline(&device, &layout, &module, format, samples, "vs_bg", "fs_bg", &[],
                         false, false, C::Always, false),
            opaque: pipeline(&device, &layout, &module, format, samples, "vs_surface", "fs_surface",
                             &pos, false, true, C::Less, false),
            overlay: pipeline(&device, &layout, &module, format, samples, "vs_surface", "fs_surface",
                              &pos, true, true, C::LessEqual, true),
            transparent: pipeline(&device, &layout, &module, format, samples, "vs_surface",
                                  "fs_surface", &pos, true, false, C::Less, false),
            line: pipeline(&device, &layout, &module, format, samples, "vs_line", "fs_line", &seg,
                           true, true, C::LessEqual, false),
            line_overlay: pipeline(&device, &layout, &module, format, samples, "vs_line", "fs_line",
                                   &seg, true, true, C::LessEqual, true),
            text: pipeline(&device, &layout, &module, format, samples, "vs_text", "fs_text", &glyph,
                           true, false, C::Always, false),
        };
        let zero = Globals { view_proj: [[0.0; 4]; 4], view: [[0.0; 4]; 4], viewport: [1.0; 4],
                             bg_top: [0.0; 4], bg_bottom: [0.0; 4] };
        let mk_globals = |label| {
            let buf = device.create_buffer_init(&wgpu::util::BufferInitDescriptor {
                label: Some(label), contents: bytemuck::bytes_of(&zero),
                usage: wgpu::BufferUsages::UNIFORM | wgpu::BufferUsages::COPY_DST,
            });
            let bind = device.create_bind_group(&wgpu::BindGroupDescriptor {
                label: Some(label), layout: &globals_layout,
                entries: &[wgpu::BindGroupEntry { binding: 0, resource: buf.as_entire_binding() }],
            });
            (buf, bind)
        };
        let (globals_buf, globals_bind) = mk_globals("globals");
        let (triad_buf, triad_bind) = mk_globals("triad");
        let atlas_tex = device.create_texture(&wgpu::TextureDescriptor {
            label: Some("atlas"),
            size: wgpu::Extent3d { width: ATLAS, height: ATLAS, depth_or_array_layers: 1 },
            mip_level_count: 1, sample_count: 1, dimension: wgpu::TextureDimension::D2,
            format: wgpu::TextureFormat::R8Unorm,
            usage: wgpu::TextureUsages::TEXTURE_BINDING | wgpu::TextureUsages::COPY_DST,
            view_formats: &[],
        });
        let sampler = device.create_sampler(&wgpu::SamplerDescriptor {
            mag_filter: wgpu::FilterMode::Linear, min_filter: wgpu::FilterMode::Linear,
            ..Default::default()
        });
        let atlas_bind = device.create_bind_group(&wgpu::BindGroupDescriptor {
            label: Some("atlas"), layout: &atlas_layout,
            entries: &[
                wgpu::BindGroupEntry { binding: 0, resource: wgpu::BindingResource::TextureView(
                    &atlas_tex.create_view(&Default::default())) },
                wgpu::BindGroupEntry { binding: 1, resource: wgpu::BindingResource::Sampler(&sampler) },
            ],
        });
        let font = font_path.and_then(|p| std::fs::read(p).ok())
            .and_then(|b| fontdue::Font::from_bytes(b, fontdue::FontSettings::default()).ok());
        let targets = make_targets(&device, format, samples, width, height);
        let mut r = Renderer {
            instance, adapter, device, queue, surface, raw, error, reconfigure: false, srgb,
            scale: 1.0, kind: kind_s, format, samples, targets, pipes, layer_layout,
            globals_buf, globals_bind, triad_buf, triad_bind, layers: HashMap::new(),
            triad: Vec::new(),
            default_bind: None,
            atlas: Atlas { tex: atlas_tex, bind: atlas_bind, x: 0, y: 0, row_h: 0,
                           glyphs: HashMap::new() },
            font, bg: ([0.23, 0.25, 0.29, 1.0], [0.13, 0.15, 0.17, 1.0]), counter: 0, info,
        };
        r.configure();
        r.default_bind = Some(r.layer_bind(lu([0.0; 4], [0.0; 4])));
        r.build_triad();
        Ok(r)
    }

    /// GPU and graphics API in use, e.g. "NVIDIA GeForce RTX 3050 (Dx12)".
    #[getter]
    fn adapter(&self) -> String {
        self.info.clone()
    }

    /// Adapter type: DiscreteGpu, IntegratedGpu, Cpu (software), ...
    #[getter]
    fn adapter_kind(&self) -> String {
        self.kind.clone()
    }

    /// Device pixels per logical pixel (Qt devicePixelRatio): line widths,
    /// text and the axes triad are given in logical pixels.
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
        let (w, h) = (width.max(1), height.max(1));
        if (w, h) == (self.targets.w, self.targets.h) {
            return;
        }
        self.targets = make_targets(&self.device, self.format, self.samples, w, h);
        self.configure();
    }

    fn size(&self) -> (u32, u32) {
        (self.targets.w, self.targets.h)
    }

    fn set_background(&mut self, top: (f32, f32, f32), bottom: (f32, f32, f32)) {
        self.bg = ([top.0, top.1, top.2, 1.0], [bottom.0, bottom.1, bottom.2, 1.0]);
    }

    /// Triangles: points (N,3) float32, tris (M,3) uint32.
    /// mode: "opaque" | "overlay" (on a surface, depth bias) | "transparent".
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
        let pv: Vec<f32> = p.iter().copied().collect();
        let tv: Vec<u32> = t.iter().copied().collect();
        if tv.iter().any(|&i| i >= n) {
            return Err(PyValueError::new_err("triangle index out of range"));
        }
        if tv.is_empty() || pv.is_empty() {
            self.remove(name);
            return Ok(());
        }
        let vbuf = self.device.create_buffer_init(&wgpu::util::BufferInitDescriptor {
            label: Some(name), contents: bytemuck::cast_slice(&pv), usage: wgpu::BufferUsages::VERTEX,
        });
        let ibuf = self.device.create_buffer_init(&wgpu::util::BufferInitDescriptor {
            label: Some(name), contents: bytemuck::cast_slice(&tv), usage: wgpu::BufferUsages::INDEX,
        });
        let pass = match mode {
            "overlay" => Pass::Overlay,
            "transparent" => Pass::Transparent,
            _ => Pass::Opaque,
        };
        self.put(name, Geom::Tri { vbuf, ibuf, count: tv.len() as u32 }, pass,
                 lu(rgba(color), [if lighting { 1.0 } else { 0.0 }, specular, 0.0, 0.0]));
        Ok(())
    }

    /// Line segments: (M,6) float32 — x0 y0 z0 x1 y1 z1 per row.
    #[pyo3(signature = (name, segments, color, width=1.0, overlay=false))]
    fn set_lines(&mut self, name: &str, segments: PyReadonlyArray2<'_, f32>,
                 color: (f32, f32, f32, f32), width: f32, overlay: bool) -> PyResult<()> {
        let s = segments.as_array();
        if s.ncols() != 6 {
            return Err(PyValueError::new_err("segments (M,6)"));
        }
        let v: Vec<f32> = s.iter().copied().collect();
        if v.is_empty() {
            self.remove(name);
            return Ok(());
        }
        let buf = self.device.create_buffer_init(&wgpu::util::BufferInitDescriptor {
            label: Some(name), contents: bytemuck::cast_slice(&v), usage: wgpu::BufferUsages::VERTEX,
        });
        self.put(name, Geom::Seg { buf, count: s.nrows() as u32 },
                 if overlay { Pass::LineOverlay } else { Pass::Line },
                 lu(rgba(color), [0.0, 0.0, (width * self.scale).max(1.0), 0.0]));
        Ok(())
    }

    /// Text. anchors: world points, or pixels from the top-left (screen=True).
    /// `viewport`: anchors are fractions of the viewport from the
    /// bottom-left corner (as VTK viewport coordinates): they follow a resize.
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
        // A dark shadow 1 px down-right, drawn first: a coloured label on
        // the light body (or a dark one on the background) stays readable.
        let d = self.scale.max(1.0);
        let shadow: Vec<Glyph> = glyphs.iter().map(|g| Glyph {
            rect: [g.rect[0] + d, g.rect[1] + d, g.rect[2], g.rect[3]], ..*g }).collect();
        let sbuf = self.device.create_buffer_init(&wgpu::util::BufferInitDescriptor {
            label: Some(name), contents: bytemuck::cast_slice(&shadow),
            usage: wgpu::BufferUsages::VERTEX,
        });
        let sname = format!("{name}{SHADOW}");
        self.put(&sname, Geom::Text { buf: sbuf, count: shadow.len() as u32 }, Pass::Text,
                 lu([0.0, 0.0, 0.0, 0.8 * color.3], [0.0; 4]));
        let buf = self.device.create_buffer_init(&wgpu::util::BufferInitDescriptor {
            label: Some(name), contents: bytemuck::cast_slice(&glyphs),
            usage: wgpu::BufferUsages::VERTEX,
        });
        self.put(name, Geom::Text { buf, count: glyphs.len() as u32 }, Pass::Text,
                 lu(rgba(color), [0.0; 4]));
        Ok(())
    }

    /// Triangles coloured by a field (the «Результати» tab, animation).
    /// points (N,3) f32, tris (M,3) u32; scalars f32: N values (point data)
    /// or M values with cell=True (one per triangle). lut (K,3|4) colours,
    /// K <= 256, spread evenly over clim (lo, hi) as in a VTK lookup table.
    /// normals (N,3): smooth shading (point data only). screen: x, y of the
    /// points are fractions of the viewport from the bottom-left corner,
    /// drawn in front of the scene (a colour bar).
    /// ambient: the light on a face seen edge-on, 0..1 (0 = 0.18, as plain
    /// surfaces).
    #[pyo3(signature = (name, points, tris, scalars, lut, clim, cell=false, normals=None,
                        opacity=1.0, nan_color=(0.66, 0.66, 0.66, 1.0), mode="opaque",
                        lighting=true, specular=0.0, screen=false, ambient=0.0))]
    #[allow(clippy::too_many_arguments)]
    fn set_scalar_triangles(&mut self, name: &str, points: PyReadonlyArray2<'_, f32>,
                            tris: PyReadonlyArray2<'_, u32>, scalars: PyReadonlyArray1<'_, f32>,
                            lut: PyReadonlyArray2<'_, f32>, clim: (f32, f32), cell: bool,
                            normals: Option<PyReadonlyArray2<'_, f32>>, opacity: f32,
                            nan_color: (f32, f32, f32, f32), mode: &str, lighting: bool,
                            specular: f32, screen: bool, ambient: f32) -> PyResult<()> {
        self.check()?;
        let p = points.as_array();
        let t = tris.as_array();
        if p.ncols() != 3 || t.ncols() != 3 {
            return Err(PyValueError::new_err("points (N,3), tris (M,3)"));
        }
        let (n, m) = (p.nrows(), t.nrows());
        let sv: Vec<f32> = scalars.as_array().iter().copied().collect();
        let want = if cell { m } else { n };
        if sv.len() != want {
            return Err(PyValueError::new_err(format!(
                "scalars: {want} values ({}), not {}", if cell { "one per triangle" }
                else { "one per point" }, sv.len())));
        }
        if t.iter().any(|&i| i as usize >= n) {
            return Err(PyValueError::new_err("triangle index out of range"));
        }
        if n == 0 || m == 0 {
            self.remove(name);
            return Ok(());
        }
        let nv: Option<Vec<f32>> = match &normals {
            Some(a) if !cell => {
                let a = a.as_array();
                if a.nrows() != n || a.ncols() != 3 {
                    return Err(PyValueError::new_err("normals (N,3), one per point"));
                }
                Some(a.iter().copied().collect())
            }
            _ => None,
        };
        let params = [if lighting { 1.0 } else { 0.0 }, specular, ambient.clamp(0.0, 1.0),
                      if nv.is_some() { 1.0 } else { 0.0 }];
        let u = scalar_uniform(&lut.as_array(), clim, opacity, nan_color, params, screen)?;
        let maxb = self.max_buffer() as usize;
        let geom = if !cell {
            if n * 12 > maxb {
                return Err(PyValueError::new_err(format!(
                    "{n} points do not fit one GPU buffer ({maxb} bytes)")));
            }
            let pv: Vec<f32> = p.iter().copied().collect();
            let vbuf = self.vbuf(name, &pv, false);
            let sbuf = self.vbuf(name, &marked(sv.into_iter()), true);
            let nbuf = nv.map(|v| self.vbuf(name, &v, false));
            let tv: Vec<u32> = t.iter().copied().collect();
            let parts = tv.chunks((maxb / 12).max(1) * 3).map(|c| {
                (self.device.create_buffer_init(&wgpu::util::BufferInitDescriptor {
                    label: Some(name), contents: bytemuck::cast_slice(c),
                    usage: wgpu::BufferUsages::INDEX,
                }), c.len() as u32)
            }).collect();
            STri::Indexed { vbuf, sbuf, nbuf, parts, n_points: n }
        } else {
            // Three own vertices per triangle: 36 bytes of positions each.
            let per_part = (maxb / 36).max(1);
            let mut parts = Vec::new();
            let mut first = 0;
            while first < m {
                let k = per_part.min(m - first);
                let mut pos = Vec::with_capacity(k * 9);
                for tri in first..first + k {
                    for c in 0..3 {
                        let i = t[[tri, c]] as usize;
                        pos.extend_from_slice(&[p[[i, 0]], p[[i, 1]], p[[i, 2]]]);
                    }
                }
                let s3 = marked(sv[first..first + k].iter().flat_map(|&x| [x, x, x]));
                parts.push(FlatPart { vbuf: self.vbuf(name, &pos, false),
                                      sbuf: self.vbuf(name, &s3, true), first, tris: k });
                first += k;
            }
            STri::Flat { parts, n_tris: m }
        };
        let pass = match mode {
            "overlay" => Pass::Overlay,
            "transparent" => Pass::Transparent,
            _ => Pass::Opaque,
        };
        self.put(name, Geom::STri(geom), pass, u);
        Ok(())
    }

    /// Segments coloured by a field: (M,6) float32 as in set_lines; scalars
    /// 2M values (the two ends of each segment) or M (one per segment).
    #[pyo3(signature = (name, segments, scalars, lut, clim, width=1.0, overlay=false,
                        opacity=1.0, nan_color=(0.66, 0.66, 0.66, 1.0)))]
    #[allow(clippy::too_many_arguments)]
    fn set_scalar_lines(&mut self, name: &str, segments: PyReadonlyArray2<'_, f32>,
                        scalars: PyReadonlyArray1<'_, f32>, lut: PyReadonlyArray2<'_, f32>,
                        clim: (f32, f32), width: f32, overlay: bool, opacity: f32,
                        nan_color: (f32, f32, f32, f32)) -> PyResult<()> {
        self.check()?;
        let seg = segments.as_array();
        if seg.ncols() != 6 {
            return Err(PyValueError::new_err("segments (M,6)"));
        }
        let m = seg.nrows();
        if m == 0 {
            self.remove(name);
            return Ok(());
        }
        let sv: Vec<f32> = scalars.as_array().iter().copied().collect();
        let pairs = Self::pairs(sv, m)?;
        if m * 24 > self.max_buffer() as usize {
            return Err(PyValueError::new_err(format!(
                "{m} segments do not fit one GPU buffer")));
        }
        let v: Vec<f32> = seg.iter().copied().collect();
        let buf = self.vbuf(name, &v, false);
        let sbuf = self.vbuf(name, &pairs, true);
        let u = scalar_uniform(&lut.as_array(), clim, opacity, nan_color,
                               [0.0, 0.0, (width * self.scale).max(1.0), 0.0], false)?;
        self.put(name, Geom::SSeg { buf, sbuf, count: m as u32 },
                 if overlay { Pass::LineOverlay } else { Pass::Line }, u);
        Ok(())
    }

    /// New values for a layer coloured by a field. The geometry stays on the
    /// GPU (animation: only the values change). The count is the same as
    /// when the layer was made.
    fn update_scalars(&mut self, name: &str, scalars: PyReadonlyArray1<'_, f32>) -> PyResult<()> {
        self.check()?;
        let sv: Vec<f32> = scalars.as_array().iter().copied().collect();
        let l = self.layers.get(name)
            .ok_or_else(|| PyValueError::new_err(format!("no layer {name:?}")))?;
        let count = |want: usize, got: usize| if want == got { Ok(()) } else {
            Err(PyValueError::new_err(format!("{name}: {want} values, not {got}")))
        };
        match &l.geom {
            Geom::STri(STri::Indexed { sbuf, n_points, .. }) => {
                count(*n_points, sv.len())?;
                self.queue.write_buffer(sbuf, 0, bytemuck::cast_slice(&marked(sv.into_iter())));
            }
            Geom::STri(STri::Flat { parts, n_tris }) => {
                count(*n_tris, sv.len())?;
                for part in parts {
                    let s3 = marked(sv[part.first..part.first + part.tris].iter()
                        .flat_map(|&x| [x, x, x]));
                    self.queue.write_buffer(&part.sbuf, 0, bytemuck::cast_slice(&s3));
                }
            }
            Geom::SSeg { sbuf, count: m, .. } => {
                let pairs = Self::pairs(sv, *m as usize)?;
                self.queue.write_buffer(sbuf, 0, bytemuck::cast_slice(&pairs));
            }
            _ => return Err(PyValueError::new_err(format!("{name}: a layer without a field"))),
        }
        Ok(())
    }

    /// Another colour table or scale for a layer coloured by a field.
    #[pyo3(signature = (name, lut, clim, nan_color=None, opacity=None))]
    fn set_colormap(&mut self, name: &str, lut: PyReadonlyArray2<'_, f32>, clim: (f32, f32),
                    nan_color: Option<(f32, f32, f32, f32)>, opacity: Option<f32>)
                    -> PyResult<()> {
        self.check()?;
        let Some(l) = self.layers.get_mut(name) else {
            return Err(PyValueError::new_err(format!("no layer {name:?}")));
        };
        let old = &l.u;
        let nan = nan_color.unwrap_or((old.nan[0], old.nan[1], old.nan[2], old.nan[3]));
        let mut u = scalar_uniform(&lut.as_array(), clim, opacity.unwrap_or(old.color[3]), nan,
                                   old.params, old.scal[3] > 0.5)?;
        u.color[..3].copy_from_slice(&old.color[..3]);
        self.queue.write_buffer(&l.ubuf, 0, bytemuck::bytes_of(&u));
        l.u = Box::new(u);
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

    /// Draw a frame into the window. Matrices are row-major 4×4 (16 floats);
    /// `triad` — the view-projection of the corner axes, or None.
    #[pyo3(signature = (view_proj, view, perspective=true, triad=None))]
    fn render(&mut self, view_proj: Vec<f32>, view: Vec<f32>, perspective: bool,
              triad: Option<Vec<f32>>) -> PyResult<()> {
        let to_surface = self.surface.is_some();
        self.draw(&view_proj, &view, perspective, triad.as_deref(), to_surface)
    }

    /// The same frame, read back: (h, w, 4) uint8 RGBA.
    #[pyo3(signature = (view_proj, view, perspective=true, triad=None))]
    fn screenshot<'py>(&mut self, py: Python<'py>, view_proj: Vec<f32>, view: Vec<f32>,
                       perspective: bool, triad: Option<Vec<f32>>)
                       -> PyResult<Bound<'py, PyAny>> {
        self.draw(&view_proj, &view, perspective, triad.as_deref(), false)?;
        let (w, h) = (self.targets.w, self.targets.h);
        let row = (w * 4).div_ceil(256) * 256;
        let buf = self.device.create_buffer(&wgpu::BufferDescriptor {
            label: Some("readback"), size: (row * h) as u64,
            usage: wgpu::BufferUsages::COPY_DST | wgpu::BufferUsages::MAP_READ,
            mapped_at_creation: false,
        });
        let mut enc = self.device.create_command_encoder(&Default::default());
        enc.copy_texture_to_buffer(
            wgpu::TexelCopyTextureInfo { texture: &self.targets.off, mip_level: 0,
                                         origin: wgpu::Origin3d::ZERO,
                                         aspect: wgpu::TextureAspect::All },
            wgpu::TexelCopyBufferInfo { buffer: &buf, layout: wgpu::TexelCopyBufferLayout {
                offset: 0, bytes_per_row: Some(row), rows_per_image: Some(h) } },
            wgpu::Extent3d { width: w, height: h, depth_or_array_layers: 1 },
        );
        self.queue.submit(Some(enc.finish()));
        let slice = buf.slice(..);
        slice.map_async(wgpu::MapMode::Read, |_| {});
        self.device.poll(wgpu::PollType::wait_indefinitely())
            .map_err(|e| PyRuntimeError::new_err(format!("poll: {e}")))?;
        let data = slice.get_mapped_range()
            .map_err(|e| PyRuntimeError::new_err(format!("map: {e:?}")))?;
        let bgra = matches!(self.format, wgpu::TextureFormat::Bgra8Unorm
                            | wgpu::TextureFormat::Bgra8UnormSrgb);
        let mut out = vec![0u8; (w * h * 4) as usize];
        for y in 0..h as usize {
            let src = &data[y * row as usize..y * row as usize + (w * 4) as usize];
            let dst = &mut out[y * (w * 4) as usize..(y + 1) * (w * 4) as usize];
            dst.copy_from_slice(src);
            if bgra {
                for px in dst.chunks_exact_mut(4) {
                    px.swap(0, 2);
                }
            }
            for px in dst.chunks_exact_mut(4) {
                px[3] = 255;
            }
        }
        drop(data);
        buf.unmap();
        let arr = out.into_pyarray(py).reshape([h as usize, w as usize, 4])?;
        Ok(arr.into_any())
    }
}

/// The adapters that wgpu sees: a list of dicts (name, backend, device_type,
/// driver, driver_info, vendor, device). `backends`: "vulkan,dx12", ...;
/// default DX12 / Vulkan / Metal. GL only when asked for: its adapter search
/// makes a WGL context current, and VTK in the same process then crashes.
#[pyfunction]
#[pyo3(signature = (backends=None))]
fn adapters(backends: Option<&str>) -> Vec<HashMap<String, String>> {
    let b = backends.map(wgpu::Backends::from_comma_list)
        .unwrap_or(wgpu::Backends::PRIMARY);
    let mut desc = wgpu::InstanceDescriptor::new_without_display_handle();
    desc.backends = b;
    let instance = wgpu::Instance::new(desc);
    pollster::block_on(instance.enumerate_adapters(b)).into_iter().map(|a| {
        let i = a.get_info();
        HashMap::from([
            ("name".to_string(), i.name.clone()),
            ("backend".to_string(), format!("{:?}", i.backend).to_lowercase()),
            ("device_type".to_string(), format!("{:?}", i.device_type)),
            ("driver".to_string(), i.driver.clone()),
            ("driver_info".to_string(), i.driver_info.clone()),
            ("vendor".to_string(), format!("{:#06x}", i.vendor)),
            ("device".to_string(), format!("{:#06x}", i.device)),
        ])
    }).collect()
}

#[pymodule]
fn foamview(m: &Bound<'_, PyModule>) -> PyResult<()> {
    m.add_class::<Renderer>()?;
    m.add_class::<cpu::CpuRenderer>()?;
    m.add_function(wrap_pyfunction!(adapters, m)?)?;
    m.add("__version__", env!("CARGO_PKG_VERSION"))?;
    Ok(())
}
