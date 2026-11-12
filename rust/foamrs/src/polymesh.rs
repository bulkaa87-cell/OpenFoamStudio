//! Reader of a reconstructed OpenFOAM case: polyMesh and vol fields, with the
//! same output as vtkOpenFOAMReader of VTK 9.7 (T47).
//!
//! The Python side (fs_rsmesh.py) makes a vtkUnstructuredGrid and vtkPolyData
//! from these arrays. Each rule below copies the rule of
//! vtkOpenFOAMReader.cxx (VTK 9.7.0), so that the numbers are the same:
//!
//! - points and values are float32 (the reader keeps vtkFloatArray);
//! - the cell type and the point order of hex, wedge, pyramid and tet cells
//!   come from `InsertCellsToGrid`; other cells are VTK_POLYHEDRON with the
//!   same face streams;
//! - point values: the mean of the cells around a point (double sum in the
//!   order of the cell ids). A point on a "patch", "wall" or "mappedWall"
//!   patch gets the mean of the boundary face values around it instead;
//! - a patch without `value` takes the values of its owner cells
//!   (`noSlip`: zero).
//!
//! Supported: ascii and binary, label=32 and label=64, scalar=64 and
//! scalar=32, files compressed with gzip (`name.gz`), faceCompactList and
//! faceList. An error goes back to Python, and Python then reads the case
//! through VTK. Decomposed cases (processor*) are not read here: a later
//! reader can join the processor meshes and use the same `Mesh::volume`.

use std::fmt;
use std::io::Read;
use std::path::{Path, PathBuf};

use rayon::prelude::*;

use crate::field;

#[derive(Debug)]
pub struct MeshError(pub String);

impl fmt::Display for MeshError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        write!(f, "{}", self.0)
    }
}

type R<T> = Result<T, MeshError>;

fn bad<T>(msg: impl Into<String>) -> R<T> {
    Err(MeshError(msg.into()))
}

// VTK cell types.
pub const VTK_TETRA: u8 = 10;
pub const VTK_HEXAHEDRON: u8 = 12;
pub const VTK_WEDGE: u8 = 13;
pub const VTK_PYRAMID: u8 = 14;
pub const VTK_POLYHEDRON: u8 = 42;

/// FOAMRS_TIMING=1: print the time of each step to stderr.
fn stamp(what: &str, t0: std::time::Instant) {
    if std::env::var_os("FOAMRS_TIMING").is_some() {
        eprintln!("foamrs: {what} {:.3} s", t0.elapsed().as_secs_f64());
    }
}

// --------------------------------------------------------------------------
// Files and headers.
// --------------------------------------------------------------------------

/// Read `path`, or `path.gz` if `path` is not there. Gzip data is unpacked.
pub fn read_file(path: &Path) -> R<Vec<u8>> {
    let (p, raw) = match std::fs::read(path) {
        Ok(b) => (path.to_path_buf(), b),
        Err(_) => {
            let mut gz = path.as_os_str().to_owned();
            gz.push(".gz");
            let gz = PathBuf::from(gz);
            match std::fs::read(&gz) {
                Ok(b) => (gz, b),
                Err(e) => return bad(format!("{}: {e}", path.display())),
            }
        }
    };
    if raw.len() >= 2 && raw[0] == 0x1f && raw[1] == 0x8b {
        let mut out = Vec::with_capacity(raw.len() * 4);
        flate2::read::MultiGzDecoder::new(&raw[..])
            .read_to_end(&mut out)
            .map_err(|e| MeshError(format!("{}: gzip: {e}", p.display())))?;
        Ok(out)
    } else {
        Ok(raw)
    }
}

/// True if `path` or `path.gz` is a file.
pub fn is_file(path: &Path) -> bool {
    if path.is_file() {
        return true;
    }
    let mut gz = path.as_os_str().to_owned();
    gz.push(".gz");
    Path::new(&gz).is_file()
}

#[derive(Debug, Clone)]
pub struct Header {
    pub binary: bool,
    pub label64: bool,
    pub scalar32: bool,
    pub class: String,
    /// Index just after the closing `}` of the FoamFile block.
    pub end: usize,
}

fn is_space(b: u8) -> bool {
    b == b' ' || b == b'\t' || b == b'\n' || b == b'\r' || b == 0x0c
}

/// Skip white space and C/C++ comments.
fn skip(buf: &[u8], mut i: usize) -> usize {
    loop {
        while i < buf.len() && is_space(buf[i]) {
            i += 1;
        }
        if i + 1 < buf.len() && buf[i] == b'/' && buf[i + 1] == b'/' {
            while i < buf.len() && buf[i] != b'\n' {
                i += 1;
            }
        } else if i + 1 < buf.len() && buf[i] == b'/' && buf[i + 1] == b'*' {
            i += 2;
            while i + 1 < buf.len() && !(buf[i] == b'*' && buf[i + 1] == b'/') {
                i += 1;
            }
            i = (i + 2).min(buf.len());
        } else {
            return i;
        }
    }
}

/// One token: a word, a quoted string (with the quotes) or one delimiter.
fn token(buf: &[u8], i: usize) -> (&[u8], usize) {
    let s = skip(buf, i);
    if s >= buf.len() {
        return (&buf[s..s], s);
    }
    if b"(){};[]".contains(&buf[s]) {
        return (&buf[s..s + 1], s + 1);
    }
    if buf[s] == b'"' {
        let mut e = s + 1;
        while e < buf.len() && buf[e] != b'"' {
            if buf[e] == b'\\' {
                e += 1;
            }
            e += 1;
        }
        let e = (e + 1).min(buf.len());
        return (&buf[s..e], e);
    }
    let mut e = s;
    while e < buf.len() && !is_space(buf[e]) && !b"(){};[]\"".contains(&buf[e]) {
        e += 1;
    }
    (&buf[s..e], e)
}

fn unquote(t: &[u8]) -> &[u8] {
    if t.len() >= 2 && t[0] == b'"' && t[t.len() - 1] == b'"' {
        &t[1..t.len() - 1]
    } else {
        t
    }
}

pub fn header(buf: &[u8], what: &str) -> R<Header> {
    let (t, mut i) = token(buf, 0);
    if t != b"FoamFile" {
        return bad(format!("{what}: no FoamFile header"));
    }
    let (t, j) = token(buf, i);
    if t != b"{" {
        return bad(format!("{what}: bad FoamFile header"));
    }
    i = j;
    let mut h = Header { binary: false, label64: false, scalar32: false, class: String::new(), end: 0 };
    loop {
        let (key, j) = token(buf, i);
        if key.is_empty() {
            return bad(format!("{what}: FoamFile header not closed"));
        }
        if key == b"}" {
            h.end = j;
            return Ok(h);
        }
        // Value tokens up to `;`.
        let mut vals: Vec<&[u8]> = Vec::new();
        let mut k = j;
        loop {
            let (v, e) = token(buf, k);
            k = e;
            if v.is_empty() || v == b";" || v == b"}" {
                break;
            }
            vals.push(v);
        }
        i = k;
        let first = vals.first().map(|v| unquote(v)).unwrap_or(b"");
        match key {
            b"format" => h.binary = first == b"binary",
            b"class" => h.class = String::from_utf8_lossy(first).into_owned(),
            b"arch" => {
                let a = String::from_utf8_lossy(first);
                if a.contains("MSB") {
                    return bad(format!("{what}: big-endian binary file"));
                }
                h.label64 = a.contains("label=64");
                h.scalar32 = a.contains("scalar=32");
            }
            _ => {}
        }
    }
}

fn parse_usize(t: &[u8]) -> Option<usize> {
    if t.is_empty() || !t.iter().all(|b| b.is_ascii_digit()) {
        return None;
    }
    std::str::from_utf8(t).ok()?.parse().ok()
}

fn parse_f64(t: &[u8]) -> R<f64> {
    std::str::from_utf8(t)
        .ok()
        .and_then(|s| s.parse::<f64>().ok())
        .ok_or_else(|| MeshError(format!("bad number '{}'", String::from_utf8_lossy(t))))
}

fn parse_i64(t: &[u8]) -> R<i64> {
    std::str::from_utf8(t)
        .ok()
        .and_then(|s| s.parse::<i64>().ok())
        .ok_or_else(|| MeshError(format!("bad label '{}'", String::from_utf8_lossy(t))))
}

/// The size `N` and the index just after `(` of the list that starts at `i`.
fn list_start(buf: &[u8], i: usize, what: &str) -> R<(usize, usize)> {
    let (t, j) = token(buf, i);
    let n = parse_usize(t).ok_or_else(|| MeshError(format!("{what}: expected a list size")))?;
    let (o, j) = token(buf, j);
    if o != b"(" {
        return bad(format!("{what}: expected '(' after the list size (uniform lists are not supported)"));
    }
    Ok((n, j))
}

/// A labelList at `i`: (values, index after the closing `)`).
fn label_list(buf: &[u8], i: usize, h: &Header, what: &str) -> R<(Vec<i64>, usize)> {
    let (n, body) = list_start(buf, i, what)?;
    if h.binary {
        let w = if h.label64 { 8 } else { 4 };
        let end = body + n * w;
        if end >= buf.len() || buf[end] != b')' {
            return bad(format!("{what}: binary list not closed"));
        }
        let raw = &buf[body..end];
        let v: Vec<i64> = if h.label64 {
            raw.chunks_exact(8).map(|c| i64::from_le_bytes(c.try_into().unwrap())).collect()
        } else {
            raw.chunks_exact(4).map(|c| i32::from_le_bytes(c.try_into().unwrap()) as i64).collect()
        };
        Ok((v, end + 1))
    } else {
        let mut v = Vec::with_capacity(n);
        let mut k = body;
        for _ in 0..n {
            let (t, e) = token(buf, k);
            v.push(parse_i64(t)?);
            k = e;
        }
        let (c, e) = token(buf, k);
        if c != b")" {
            return bad(format!("{what}: list longer than its size"));
        }
        Ok((v, e))
    }
}

/// label_list straight into u32 (ids of points, faces and cells).
fn label_list_u32(buf: &[u8], i: usize, h: &Header, what: &str) -> R<(Vec<u32>, usize)> {
    if !h.binary || h.label64 {
        let (v, e) = label_list(buf, i, h, what)?;
        return Ok((labels_u32(v, what)?, e));
    }
    let (n, body) = list_start(buf, i, what)?;
    let end = body + n * 4;
    if end >= buf.len() || buf[end] != b')' {
        return bad(format!("{what}: binary list not closed"));
    }
    let v: Vec<u32> = buf[body..end]
        .par_chunks_exact(4 * 65536)
        .flat_map_iter(|c| c.chunks_exact(4).map(|c| i32::from_le_bytes(c.try_into().unwrap()) as u32))
        .chain(buf[body + (n * 4 / 262144) * 262144..end].par_chunks_exact(4)
            .map(|c| i32::from_le_bytes(c.try_into().unwrap()) as u32))
        .collect();
    if v.par_iter().any(|&x| x > i32::MAX as u32) {
        return bad(format!("{what}: negative label"));
    }
    Ok((v, end + 1))
}

fn labels_u32(v: Vec<i64>, what: &str) -> R<Vec<u32>> {
    let mut out = Vec::with_capacity(v.len());
    for x in v {
        if x < 0 || x > u32::MAX as i64 - 1 {
            return bad(format!("{what}: label {x} out of range"));
        }
        out.push(x as u32);
    }
    Ok(out)
}

// --------------------------------------------------------------------------
// polyMesh files.
// --------------------------------------------------------------------------

/// points: (x, y, z) per point.
pub fn read_points(dir: &Path) -> R<Vec<[f64; 3]>> {
    let t0 = std::time::Instant::now();
    let buf = read_file(&dir.join("points"))?;
    stamp("  points: file", t0);
    let h = header(&buf, "points")?;
    let (n, body) = list_start(&buf, h.end, "points")?;
    let mut out = Vec::with_capacity(n);
    if h.binary {
        let w = if h.scalar32 { 4 } else { 8 };
        let end = body + n * 3 * w;
        if end >= buf.len() || buf[end] != b')' {
            return bad("points: binary list not closed");
        }
        let raw = &buf[body..end];
        if h.scalar32 {
            for c in raw.chunks_exact(12) {
                let f = |k: usize| f32::from_le_bytes(c[k..k + 4].try_into().unwrap()) as f64;
                out.push([f(0), f(4), f(8)]);
            }
        } else {
            for c in raw.chunks_exact(24) {
                let f = |k: usize| f64::from_le_bytes(c[k..k + 8].try_into().unwrap());
                out.push([f(0), f(8), f(16)]);
            }
        }
    } else {
        let mut k = body;
        for _ in 0..n {
            let (o, e) = token(&buf, k);
            if o != b"(" {
                return bad("points: expected '('");
            }
            let mut p = [0.0; 3];
            let mut e = e;
            for c in p.iter_mut() {
                let (t, e2) = token(&buf, e);
                *c = parse_f64(t)?;
                e = e2;
            }
            let (c, e2) = token(&buf, e);
            if c != b")" {
                return bad("points: expected ')'");
            }
            k = e2;
            out.push(p);
        }
    }
    Ok(out)
}

/// Faces as a compact list: face f is `labels[offsets[f]..offsets[f+1]]`.
pub struct Faces {
    pub offsets: Vec<u32>,
    pub labels: Vec<u32>,
}

impl Faces {
    #[inline]
    pub fn len(&self) -> usize {
        self.offsets.len().saturating_sub(1)
    }
    #[inline]
    pub fn is_empty(&self) -> bool {
        self.len() == 0
    }
    #[inline]
    pub fn get(&self, f: usize) -> &[u32] {
        &self.labels[self.offsets[f] as usize..self.offsets[f + 1] as usize]
    }
    #[inline]
    pub fn size(&self, f: usize) -> usize {
        (self.offsets[f + 1] - self.offsets[f]) as usize
    }
}

pub fn read_faces(dir: &Path) -> R<Faces> {
    let t0 = std::time::Instant::now();
    let buf = read_file(&dir.join("faces"))?;
    stamp("  faces: file", t0);
    let h = header(&buf, "faces")?;
    if h.class == "faceCompactList" {
        let (offsets, j) = label_list_u32(&buf, h.end, &h, "faces (offsets)")?;
        let (labels, _) = label_list_u32(&buf, j, &h, "faces (labels)")?;
        stamp("  faces: lists", t0);

        if offsets.is_empty() || *offsets.last().unwrap() as usize != labels.len()
            || offsets.windows(2).any(|w| w[1] < w[0])
        {
            return bad("faces: bad compact list");
        }
        return Ok(Faces { offsets, labels });
    }
    if h.binary {
        return bad("faces: binary faceList (not compact) is not supported");
    }
    // ascii faceList: N ( n(a b c) ... )
    let (n, mut k) = list_start(&buf, h.end, "faces")?;
    let mut offsets = Vec::with_capacity(n + 1);
    let mut labels = Vec::with_capacity(n * 4);
    offsets.push(0u32);
    for _ in 0..n {
        let (m, body) = list_start(&buf, k, "faces")?;
        let mut e = body;
        for _ in 0..m {
            let (t, e2) = token(&buf, e);
            let x = parse_i64(t)?;
            if x < 0 || x >= u32::MAX as i64 {
                return bad("faces: label out of range");
            }
            labels.push(x as u32);
            e = e2;
        }
        let (c, e2) = token(&buf, e);
        if c != b")" {
            return bad("faces: face longer than its size");
        }
        k = e2;
        offsets.push(labels.len() as u32);
    }
    Ok(Faces { offsets, labels })
}

pub fn read_labels(path: &Path, what: &str) -> R<Vec<u32>> {
    let buf = read_file(path)?;
    let h = header(&buf, what)?;
    Ok(label_list_u32(&buf, h.end, &h, what)?.0)
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum PatchKind {
    /// symmetryPlane, wedge, cyclic, empty …: no boundary point values.
    Geometric,
    /// patch, wall, mappedWall.
    Physical,
    /// processor, processorCyclic.
    Processor,
}

#[derive(Debug, Clone)]
pub struct Patch {
    pub name: String,
    pub kind_name: String,
    pub kind: PatchKind,
    pub start: usize,
    pub n_faces: usize,
}

/// polyMesh/boundary: the patches in file order.
pub fn read_boundary(dir: &Path) -> R<Vec<Patch>> {
    let buf = read_file(&dir.join("boundary"))?;
    let h = header(&buf, "boundary")?;
    let (n, mut k) = list_start(&buf, h.end, "boundary")?;
    let mut out = Vec::with_capacity(n);
    for _ in 0..n {
        let (name, e) = token(&buf, k);
        let (o, e) = token(&buf, e);
        if name.is_empty() || o != b"{" {
            return bad("boundary: expected 'name {'");
        }
        let mut kind_name = String::new();
        let (mut start, mut nf) = (None, None);
        let mut i = e;
        loop {
            let (key, e) = token(&buf, i);
            if key.is_empty() {
                return bad("boundary: patch not closed");
            }
            if key == b"}" {
                i = e;
                break;
            }
            let mut vals: Vec<&[u8]> = Vec::new();
            let mut j = e;
            let mut depth = 0i32;
            loop {
                let (v, e2) = token(&buf, j);
                j = e2;
                if v.is_empty() {
                    break;
                }
                if v == b"(" {
                    depth += 1;
                } else if v == b")" {
                    depth -= 1;
                } else if v == b";" && depth <= 0 {
                    break;
                }
                vals.push(v);
            }
            i = j;
            let first = vals.first().copied().unwrap_or(b"");
            match key {
                b"type" => kind_name = String::from_utf8_lossy(unquote(first)).into_owned(),
                b"startFace" => start = parse_usize(first),
                b"nFaces" => nf = parse_usize(first),
                _ => {}
            }
        }
        k = i;
        let kind = match kind_name.as_str() {
            "patch" | "wall" | "mappedWall" => PatchKind::Physical,
            "processor" | "processorCyclic" => PatchKind::Processor,
            _ => PatchKind::Geometric,
        };
        let (Some(start), Some(n_faces)) = (start, nf) else {
            return bad("boundary: patch without startFace or nFaces");
        };
        out.push(Patch {
            name: String::from_utf8_lossy(unquote(name)).into_owned(),
            kind_name,
            kind,
            start,
            n_faces,
        });
    }
    // The same checks as vtkFoamBoundaries::update.
    for w in out.windows(2) {
        if w[0].start + w[0].n_faces != w[1].start {
            return bad("boundary: patches are not contiguous");
        }
    }
    Ok(out)
}

// --------------------------------------------------------------------------
// The mesh.
// --------------------------------------------------------------------------

pub struct Mesh {
    pub points: Vec<[f64; 3]>,
    pub faces: Faces,
    pub owner: Vec<u32>,
    pub neighbour: Vec<u32>,
    pub patches: Vec<Patch>,
    pub n_cells: usize,
}

/// The cells as VTK arrays (vtkUnstructuredGrid::SetPolyhedralCells).
pub struct Cells {
    pub types: Vec<u8>,
    /// Cell point lists: offsets (n_cells + 1) and point ids.
    pub offsets: Vec<i64>,
    pub conn: Vec<i64>,
    /// Faces of each polyhedron (ids into poly_faces): offsets (n_cells + 1).
    pub floc_offsets: Vec<i64>,
    /// Faces of all polyhedra: offsets and point ids.
    pub pface_offsets: Vec<i64>,
    pub pface_conn: Vec<i64>,
}

/// Values of one field, float32 like vtkFloatArray.
#[derive(Debug, Clone)]
pub struct F32 {
    pub ncomp: usize,
    pub data: Vec<f32>,
}

impl F32 {
    fn from_values(v: field::Values) -> F32 {
        F32 { ncomp: v.ncomp, data: v.data.into_iter().map(|x| x as f32).collect() }
    }
}

/// One field: internal values and the values of each patch (file order).
pub struct FieldData {
    pub name: String,
    pub internal: F32,
    pub patches: Vec<F32>,
}

impl Mesh {
    /// Read polyMesh. `faces_dir` has faces, owner, neighbour and boundary;
    /// `points_dir` has points (for a moving mesh they can be different).
    pub fn read(faces_dir: &Path, points_dir: &Path) -> R<Mesh> {
        let t0 = std::time::Instant::now();
        let (a, b) = rayon::join(
            || rayon::join(|| read_faces(faces_dir), || read_points(points_dir)),
            || {
                rayon::join(
                    || read_labels(&faces_dir.join("owner"), "owner"),
                    || {
                        rayon::join(
                            || read_labels(&faces_dir.join("neighbour"), "neighbour"),
                            || read_boundary(faces_dir),
                        )
                    },
                )
            },
        );
        stamp("read files", t0);
        let (faces, points) = (a.0?, a.1?);
        let owner = b.0?;
        let (mut neighbour, patches) = (b.1 .0?, b.1 .1?);
        // Very old meshes: neighbour as long as owner, padded with -1. Here
        // -1 became an error above, so this case goes to VTK.
        if owner.len() > faces.len() {
            return bad("owner is longer than faces");
        }
        if neighbour.len() > owner.len() {
            return bad("neighbour is longer than owner");
        }
        neighbour.shrink_to_fit();
        let np = points.len() as u32;
        if faces.labels.iter().any(|&p| p >= np) {
            return bad("faces: point id out of range");
        }
        if (0..owner.len()).any(|f| faces.size(f) < 3) {
            return bad("faces: a face with less than 3 points");
        }
        let n_cells = owner.iter().chain(neighbour.iter()).map(|&c| c as usize + 1).max().unwrap_or(0);
        if let Some(last) = patches.last() {
            if last.start + last.n_faces > owner.len() {
                return bad("boundary: patches past the last face");
            }
        }
        Ok(Mesh { points, faces, owner, neighbour, patches, n_cells })
    }

    pub fn n_faces(&self) -> usize {
        self.owner.len()
    }

    pub fn points_f32(&self) -> Vec<f32> {
        let mut out = Vec::with_capacity(self.points.len() * 3);
        for p in &self.points {
            out.extend_from_slice(&[p[0] as f32, p[1] as f32, p[2] as f32]);
        }
        out
    }

    /// Faces of each cell in increasing face order (CreateCellFaces).
    fn cell_faces(&self) -> (Vec<u32>, Vec<u32>) {
        let nc = self.n_cells;
        let mut off = vec![0u32; nc + 1];
        for &c in &self.owner {
            off[c as usize + 1] += 1;
        }
        for &c in &self.neighbour {
            off[c as usize + 1] += 1;
        }
        for i in 0..nc {
            off[i + 1] += off[i];
        }
        let mut next = off.clone();
        let mut list = vec![0u32; off[nc] as usize];
        let ni = self.neighbour.len();
        for f in 0..ni {
            let o = self.owner[f] as usize;
            list[next[o] as usize] = f as u32;
            next[o] += 1;
            let n = self.neighbour[f] as usize;
            list[next[n] as usize] = f as u32;
            next[n] += 1;
        }
        for f in ni..self.owner.len() {
            let o = self.owner[f] as usize;
            list[next[o] as usize] = f as u32;
            next[o] += 1;
        }
        (off, list)
    }

    /// Cell types and point lists as vtkOpenFOAMReader makes them.
    pub fn cells(&self) -> R<Cells> {
        let t0 = std::time::Instant::now();
        let (coff, clist) = self.cell_faces();
        stamp("  cells: cell faces", t0);
        let nc = self.n_cells;
        const CHUNK: usize = 16384;
        let parts: Vec<R<Part>> = (0..nc.div_ceil(CHUNK))
            .into_par_iter()
            .map(|k| {
                let mut part = Part::default();
                let a = k * CHUNK;
                let b = ((k + 1) * CHUNK).min(nc);
                for c in a..b {
                    let cf = &clist[coff[c] as usize..coff[c + 1] as usize];
                    self.one_cell(c, cf, &mut part)?;
                }
                Ok(part)
            })
            .collect();
        stamp("  cells: parts", t0);
        let parts: Vec<Part> = parts.into_iter().collect::<R<_>>()?;
        // Join the parts: prefix sums (cheap, in order), the large lists
        // in parallel.
        let mut types = Vec::with_capacity(nc);
        let mut offsets = Vec::with_capacity(nc + 1);
        let mut floc_offsets = Vec::with_capacity(nc + 1);
        let mut pface_offsets = vec![0i64];
        let (mut s, mut fs) = (0i64, 0i64);
        offsets.push(0);
        floc_offsets.push(0);
        for p in &parts {
            types.extend_from_slice(&p.types);
            for (&n, &k) in p.sizes.iter().zip(&p.nfaces) {
                s += n as i64;
                offsets.push(s);
                fs += k as i64;
                floc_offsets.push(fs);
            }
            let mut b = *pface_offsets.last().unwrap();
            for &n in &p.pface_sizes {
                b += n as i64;
                pface_offsets.push(b);
            }
        }
        let conn = par_concat(parts.iter().map(|p| p.conn.as_slice()).collect());
        let pface_conn = par_concat(parts.iter().map(|p| p.pface_conn.as_slice()).collect());
        stamp("  cells: joined", t0);
        let cells = Cells { types, offsets, conn, floc_offsets, pface_offsets, pface_conn };
        Ok(cells)
    }

    #[inline]
    fn is_owner(&self, c: usize, f: u32) -> bool {
        self.owner[f as usize] as usize == c
    }

    fn one_cell(&self, c: usize, cf: &[u32], out: &mut Part) -> R<()> {
        let fs = &self.faces;
        let size = |f: u32| fs.size(f as usize);
        let nf = cf.len();
        let mut ty = VTK_POLYHEDRON;
        if nf == 6 {
            if cf.iter().all(|&f| size(f) == 4) {
                ty = VTK_HEXAHEDRON;
            }
        } else if nf == 5 {
            let (mut nt, mut nq) = (0, 0);
            for &f in cf {
                match size(f) {
                    3 => nt += 1,
                    4 => nq += 1,
                    _ => break,
                }
            }
            if nt == 2 && nq == 3 {
                ty = VTK_WEDGE;
            } else if nt == 4 && nq == 1 {
                ty = VTK_PYRAMID;
            }
        } else if nf == 4 && cf.iter().all(|&f| size(f) == 3) {
            ty = VTK_TETRA;
        }

        let mut cp: [u32; 8] = [0; 8];
        let mut n: usize;
        if ty == VTK_HEXAHEDRON {
            {
                let f0 = cf[0];
                let p = fs.get(f0 as usize);
                cp[0] = p[0];
                if self.is_owner(c, f0) {
                    cp[1] = p[3];
                    cp[2] = p[2];
                    cp[3] = p[1];
                } else {
                    cp[1] = p[1];
                    cp[2] = p[2];
                    cp[3] = p[3];
                }
                n = 4;
            }
            let (b0, b2) = (cp[0], cp[2]);
            let mut opp: i64 = -1;
            let mut pivot: i64 = -1;
            let mut dup: i32 = -1;
            for &f in cf.iter().take(5).skip(1) {
                let own = self.is_owner(c, f);
                let p = fs.get(f as usize);
                let mut found: i32 = -1;
                let mut pi = 0usize;
                while pi < 4 {
                    if b0 == p[pi] {
                        found = 0;
                        break;
                    } else if b2 == p[pi] {
                        found = 2;
                        break;
                    }
                    pi += 1;
                }
                if found == -1 {
                    opp = f as i64;
                    if pivot >= 0 {
                        break;
                    }
                } else if pivot == -1 {
                    dup = found;
                    let next = p[(pi + 1) % 4];
                    let prev = p[(3 + pi) % 4];
                    let cmp = if own { cp[1 + found as usize] } else { cp[3 - found as usize] };
                    pivot = if next == cmp { prev as i64 } else { next as i64 };
                    if opp >= 0 {
                        break;
                    }
                }
            }
            if opp == -1 {
                opp = cf[5] as i64;
            }
            let p = fs.get(opp as usize);
            let mut pp = 0usize;
            while pp < 4 && p[pp] as i64 != pivot {
                pp += 1;
            }
            if pp == 4 {
                return bad(format!("cell {c}: hexahedron without a pivot point"));
            }
            if dup == 2 {
                pp = (pp + 2) % 4;
            }
            if self.is_owner(c, opp as u32) {
                for &x in p.iter().skip(pp) {
                    cp[n] = x;
                    n += 1;
                }
                for &x in p.iter().take(pp) {
                    cp[n] = x;
                    n += 1;
                }
            } else {
                for k in (0..=pp).rev() {
                    cp[n] = p[k];
                    n += 1;
                }
                for k in ((pp + 1)..4).rev() {
                    cp[n] = p[k];
                    n += 1;
                }
            }
            out.push_cell(VTK_HEXAHEDRON, &cp[..8]);
            return Ok(());
        }
        if ty == VTK_WEDGE {
            let base = (0..5).find(|&i| size(cf[i]) == 3).unwrap_or(0);
            {
                let f = cf[base];
                let p = fs.get(f as usize);
                cp[0] = p[0];
                if self.is_owner(c, f) {
                    cp[1] = p[1];
                    cp[2] = p[2];
                } else {
                    cp[1] = p[2];
                    cp[2] = p[1];
                }
                n = 3;
            }
            let (b0, b2) = (cp[0], cp[2]);
            let mut opp: i64 = -1;
            let mut pivot: i64 = -1;
            let mut dup2 = false;
            for (fi, &f) in cf.iter().enumerate().take(5) {
                if fi == base {
                    continue;
                }
                if size(f) == 3 {
                    opp = f as i64;
                } else if pivot == -1 {
                    let own = self.is_owner(c, f);
                    let p = fs.get(f as usize);
                    let mut found0 = false;
                    let mut pi = 0usize;
                    while pi < 4 {
                        if b0 == p[pi] {
                            found0 = true;
                            break;
                        } else if b2 == p[pi] {
                            break;
                        }
                        pi += 1;
                    }
                    let (prev_b, next_b) = if found0 {
                        (cp[2], cp[1])
                    } else {
                        dup2 = true;
                        (cp[1], cp[0])
                    };
                    let next = p[(pi + 1) % 4];
                    let prev = p[(3 + pi) % 4];
                    let cmp = if own { prev_b } else { next_b };
                    pivot = if next == cmp { prev as i64 } else { next as i64 };
                }
                if opp >= 0 && pivot >= 0 {
                    break;
                }
            }
            if opp >= 0 {
                let p = fs.get(opp as usize);
                if let Some(mut pp) = (0..3).find(|&k| p[k] as i64 == pivot) {
                    if self.is_owner(c, opp as u32) {
                        if dup2 {
                            pp = (pp + 2) % 3;
                        }
                        for k in (0..=pp).rev() {
                            cp[n] = p[k];
                            n += 1;
                        }
                        for k in ((pp + 1)..3).rev() {
                            cp[n] = p[k];
                            n += 1;
                        }
                    } else {
                        if dup2 {
                            pp = (1 + pp) % 3;
                        }
                        for &x in p.iter().skip(pp) {
                            cp[n] = x;
                            n += 1;
                        }
                        for &x in p.iter().take(pp) {
                            cp[n] = x;
                            n += 1;
                        }
                    }
                    out.push_cell(VTK_WEDGE, &cp[..6]);
                    return Ok(());
                }
            }
            ty = VTK_POLYHEDRON; // no pivot: VTK makes a polyhedron
        }
        if ty == VTK_PYRAMID || ty == VTK_TETRA {
            let base = if ty == VTK_PYRAMID {
                (0..nf).find(|&i| size(cf[i]) == 4).unwrap_or(0)
            } else {
                0
            };
            let f = cf[base];
            let p = fs.get(f as usize);
            cp[0] = p[0];
            n = 1;
            if self.is_owner(c, f) {
                for k in (1..p.len()).rev() {
                    cp[n] = p[k];
                    n += 1;
                }
            } else {
                for &x in p.iter().skip(1) {
                    cp[n] = x;
                    n += 1;
                }
            }
            let other = fs.get(cf[if base != 0 { 0 } else { 1 }] as usize);
            let mut apex = 0u32;
            for &o in other {
                apex = o;
                if !p.contains(&o) {
                    break;
                }
            }
            cp[n] = apex;
            n += 1;
            out.push_cell(ty, &cp[..n]);
            return Ok(());
        }
        // VTK_POLYHEDRON
        let mut cell_pts: Vec<u32> = Vec::with_capacity(32);
        let mut work: Vec<i64> = Vec::with_capacity(16);
        let start_conn = out.pface_conn.len();
        for &f in cf {
            let own = self.is_owner(c, f);
            let p = fs.get(f as usize);
            let m = p.len();
            work.clear();
            work.extend(p.iter().map(|&x| x as i64));
            let n_before = cell_pts.len();
            // pass 1: face points (point 0, then the others in the cell
            // direction), mark points that earlier faces already gave
            out.pface_conn.push(p[0]);
            if cell_pts[..n_before].contains(&p[0]) {
                work[0] = -1;
            }
            let mut idx: i64 = if own { 1 } else { m as i64 - 1 };
            let dir: i64 = if own { 1 } else { -1 };
            for _ in 1..m {
                let x = p[idx as usize];
                out.pface_conn.push(x);
                if cell_pts[..n_before].contains(&x) {
                    work[idx as usize] = -1;
                }
                idx += dir;
            }
            out.pface_sizes.push(m as u32);
            // pass 2: new points in the file order of the face
            for &x in &work {
                if x != -1 {
                    cell_pts.push(x as u32);
                }
            }
        }
        let _ = start_conn;
        out.types.push(VTK_POLYHEDRON);
        out.sizes.push(cell_pts.len() as u32);
        out.nfaces.push(nf as u32);
        out.conn.extend_from_slice(&cell_pts);
        Ok(())
    }
}

/// Join u32 lists into one i64 list, the copies in parallel.
fn par_concat(parts: Vec<&[u32]>) -> Vec<i64> {
    let total: usize = parts.iter().map(|p| p.len()).sum();
    let mut out = vec![0i64; total];
    let mut jobs = Vec::with_capacity(parts.len());
    let mut rest: &mut [i64] = &mut out;
    for p in parts {
        let (a, b) = rest.split_at_mut(p.len());
        jobs.push((a, p));
        rest = b;
    }
    jobs.into_par_iter().for_each(|(dst, src)| {
        for (d, &x) in dst.iter_mut().zip(src) {
            *d = x as i64;
        }
    });
    out
}

/// Lists of "items around each point": `idx` for each item, in item order
/// (so a sum over a list has the same order as in VTK). CSR: (offsets, ids).
fn point_lists(n_points: usize, items: impl Fn(&mut dyn FnMut(usize, u32))) -> (Vec<u32>, Vec<u32>) {
    let mut off = vec![0u32; n_points + 1];
    items(&mut |p, _| off[p + 1] += 1);
    for i in 0..n_points {
        off[i + 1] += off[i];
    }
    let mut next = off.clone();
    let mut ids = vec![0u32; off[n_points] as usize];
    items(&mut |p, item| {
        ids[next[p] as usize] = item;
        next[p] += 1;
    });
    (off, ids)
}

#[derive(Default)]
struct Part {
    types: Vec<u8>,
    sizes: Vec<u32>,
    nfaces: Vec<u32>,
    conn: Vec<u32>,
    pface_sizes: Vec<u32>,
    pface_conn: Vec<u32>,
}

impl Part {
    fn push_cell(&mut self, ty: u8, pts: &[u32]) {
        self.types.push(ty);
        self.sizes.push(pts.len() as u32);
        self.nfaces.push(0);
        self.conn.extend_from_slice(pts);
    }
}

// --------------------------------------------------------------------------
// Fields.
// --------------------------------------------------------------------------

/// What a time folder holds: the vol fields that VTK would list.
#[derive(Debug, Clone)]
pub struct FieldFile {
    pub name: String,
    pub path: PathBuf,
    pub class: String,
}

/// The field files of a time folder, sorted by name (GetFieldNames +
/// SortFieldFiles, IgnoreRestartFiles on). A point field is an error: the
/// VTK reader would add point data that this reader does not make.
pub fn list_fields(time_dir: &Path) -> R<Vec<FieldFile>> {
    let mut out = Vec::new();
    let rd = std::fs::read_dir(time_dir).map_err(|e| MeshError(format!("{}: {e}", time_dir.display())))?;
    for e in rd.flatten() {
        let Ok(ft) = e.file_type() else { continue };
        if ft.is_dir() {
            continue;
        }
        let file = e.file_name().to_string_lossy().into_owned();
        if file.is_empty() || file.ends_with('~') || (file.len() > 2 && file.ends_with("_0")) {
            continue;
        }
        if let Some(dot) = file.rfind('.') {
            let ext = &file[dot + 1..];
            if matches!(ext, "bak" | "BAK" | "old" | "save") {
                continue;
            }
        }
        let path = e.path();
        let Some(class) = header_class(&path) else { continue };
        let Some(len) = class.find("Field") else { continue };
        let internal_only = &class[len..] == "Field::Internal";
        if !(len + 5 == class.len() || internal_only) {
            continue;
        }
        let kind = if class.starts_with("vol") {
            &class[3..len]
        } else if class.starts_with("point") {
            if matches!(&class[5..len], "Scalar" | "Vector" | "SymmTensor" | "Tensor" | "SphericalTensor") {
                return bad(format!("{}: point fields are not supported", path.display()));
            }
            continue;
        } else {
            continue;
        };
        if !matches!(kind, "Scalar" | "Vector" | "SymmTensor" | "Tensor" | "SphericalTensor") {
            continue;
        }
        let name = file.strip_suffix(".gz").unwrap_or(&file).to_string();
        out.push(FieldFile { name, path, class });
    }
    out.sort_by(|a, b| a.name.cmp(&b.name));
    out.dedup_by(|a, b| a.name == b.name);
    Ok(out)
}

/// The `class` of a FoamFile header (first 2 KB, like VTK), unpacked if gzip.
fn header_class(path: &Path) -> Option<String> {
    use std::io::Read as _;
    let mut f = std::fs::File::open(path).ok()?;
    let mut head = vec![0u8; 2048];
    let n = f.read(&mut head).ok()?;
    head.truncate(n);
    if head.len() >= 2 && head[0] == 0x1f && head[1] == 0x8b {
        let mut out = vec![0u8; 2048];
        let mut d = flate2::read::MultiGzDecoder::new(std::fs::File::open(path).ok()?);
        let mut got = 0;
        while got < out.len() {
            match d.read(&mut out[got..]) {
                Ok(0) => break,
                Ok(k) => got += k,
                Err(_) => break,
            }
        }
        out.truncate(got);
        head = out;
    }
    let h = header(&head, "field").ok()?;
    Some(h.class)
}

fn ncomp_of_class(class: &str) -> usize {
    if class.contains("SymmTensor") {
        6
    } else if class.contains("SphericalTensor") {
        1
    } else if class.contains("Tensor") {
        9
    } else if class.contains("Vector") {
        3
    } else {
        1
    }
}

/// One entry of boundaryField: its key, `type` and the place of `value`.
struct BEntry {
    key: String,
    kind: String,
    value_at: Option<usize>,
}

/// Step over a binary `nonuniform List<type> N (raw)` at `i`, if one is there.
fn skip_binary_list(buf: &[u8], i: usize) -> Option<usize> {
    let (t, j) = token(buf, i);
    let (list, j) = if t == b"nonuniform" { token(buf, j) } else { (t, j) };
    let ty = list.strip_prefix(b"List<")?.strip_suffix(b">")?;
    let nc = match ty {
        b"scalar" => 1,
        b"vector" => 3,
        b"symmTensor" => 6,
        b"tensor" => 9,
        b"sphericalTensor" => 1,
        b"label" => return None,
        _ => return None,
    };
    let (count, j) = token(buf, j);
    let n = parse_usize(count)?;
    let (o, j) = token(buf, j);
    if o != b"(" {
        return None;
    }
    Some(j + n * nc * 8 + 1)
}

/// The entries of boundaryField, in file order.
fn boundary_entries(buf: &[u8], binary: bool, from: usize) -> R<Vec<BEntry>> {
    // Find the keyword boundaryField at the top level.
    let mut i = from;
    loop {
        let (t, e) = token(buf, i);
        if t.is_empty() {
            return bad("no boundaryField");
        }
        if t == b"boundaryField" {
            i = e;
            break;
        }
        // Skip a top-level entry (internalField with a binary list included).
        if binary {
            if let Some(k) = skip_binary_list(buf, e) {
                i = k;
                continue;
            }
        }
        if t == b"{" {
            i = block_close(buf, i, binary)? + 1;
        } else {
            i = e;
        }
    }
    let (o, mut i) = token(buf, i);
    if o != b"{" {
        return bad("boundaryField: expected '{'");
    }
    let mut out = Vec::new();
    loop {
        let (key, e) = token(buf, i);
        if key.is_empty() {
            return bad("boundaryField not closed");
        }
        if key == b"}" {
            return Ok(out);
        }
        if key.starts_with(b"#") {
            // #includeEtc and other directives: the VTK reader cannot read
            // such a file either; VTK then gives its own result.
            return bad(format!("directive {} in boundaryField", String::from_utf8_lossy(key)));
        }
        let (o, e) = token(buf, e);
        if o != b"{" {
            // key value; at the boundaryField level: skip to ';'
            let mut j = e;
            loop {
                let (t, e2) = token(buf, j);
                j = e2;
                if t.is_empty() || t == b";" {
                    break;
                }
            }
            i = j;
            continue;
        }
        // Inside a patch block: key value ...;
        let mut j = e;
        let mut kind = String::new();
        let mut value_at = None;
        loop {
            let (k, e2) = token(buf, j);
            if k.is_empty() {
                return bad("boundaryField: patch block not closed");
            }
            if k == b"}" {
                j = e2;
                break;
            }
            if k == b"value" {
                value_at = Some(e2);
            }
            if k == b"type" {
                let (v, _) = token(buf, e2);
                kind = String::from_utf8_lossy(unquote(v)).into_owned();
            }
            // Skip to the ';' of this entry; binary lists by their size.
            let mut m = e2;
            if binary {
                if let Some(x) = skip_binary_list(buf, m) {
                    m = x;
                }
            }
            let mut depth = 0i32;
            loop {
                let (t, e3) = token(buf, m);
                if t.is_empty() {
                    return bad("boundaryField: entry not closed");
                }
                m = e3;
                if t == b"(" {
                    depth += 1;
                } else if t == b")" {
                    depth -= 1;
                } else if t == b"{" {
                    m = block_close(buf, m - 1, binary)? + 1;
                    if depth <= 0 {
                        break; // `key { ... }` has no `;`
                    }
                } else if t == b";" && depth <= 0 {
                    break;
                } else if t == b"}" && depth <= 0 {
                    // `}` without `;` closes the patch: step back to it
                    m -= 1;
                    break;
                }
            }
            j = m;
        }
        out.push(BEntry {
            key: String::from_utf8_lossy(unquote(key)).into_owned(),
            kind,
            value_at,
        });
        i = j;
    }
}

/// Index of the `}` that closes the `{` at `open`.
fn block_close(buf: &[u8], open: usize, binary: bool) -> R<usize> {
    let mut depth = 0i32;
    let mut i = open;
    loop {
        let (t, e) = token(buf, i);
        if t.is_empty() {
            return bad("block not closed");
        }
        if t == b"{" {
            depth += 1;
        } else if t == b"}" {
            depth -= 1;
            if depth == 0 {
                return Ok(e - 1);
            }
        } else if binary {
            if let Some(k) = skip_binary_list(buf, i) {
                i = k;
                continue;
            }
        }
        i = e;
    }
}

impl Mesh {
    /// Read one field file: internal values and the value of each patch.
    /// Ok(None): the internal field does not fit the mesh — VTK skips such a
    /// field.
    ///
    /// `need_internal` false (patches only): a binary internal field is not
    /// converted; only the owner cells of patches without `value` are read.
    pub fn read_field(&self, ff: &FieldFile, need_internal: bool) -> R<Option<FieldData>> {
        let buf = read_file(&ff.path)?;
        let h = header(&buf, &ff.name)?;
        if h.binary && h.scalar32 {
            return bad(format!("{}: scalar=32 fields are not supported", ff.name));
        }
        let ncomp = ncomp_of_class(&ff.class);
        let dim_field = ff.class.ends_with("::Internal");
        let ikey: &[u8] = if dim_field { b"value" } else { b"internalField" };
        let at = top_entry(&buf, h.end, ikey, h.binary)
            .ok_or_else(|| MeshError(format!("{}: no {}", ff.name, String::from_utf8_lossy(ikey))))?;
        let mut raw = None;
        if !need_internal && !dim_field && h.binary {
            if let Some((nc, n, body)) = binary_list_at(&buf, at) {
                if nc != ncomp {
                    return bad(format!("{}: {} components, the class says {}", ff.name, nc, ncomp));
                }
                if n != self.n_cells {
                    return Ok(None);
                }
                if body + n * nc * 8 > buf.len() {
                    return bad(format!("{}: binary list runs past the end", ff.name));
                }
                raw = Some(body);
            }
        }
        let internal = if raw.is_some() {
            F32 { ncomp, data: Vec::new() }
        } else {
            let v = field::parse_entry_pub(&buf, at, h.binary, self.n_cells)
                .map_err(|e| MeshError(format!("{}: {e}", ff.name)))?;
            if v.ncomp != ncomp {
                return bad(format!("{}: {} components, the class says {}", ff.name, v.ncomp, ncomp));
            }
            if v.len() != self.n_cells {
                return Ok(None);
            }
            F32::from_values(v)
        };
        let mut patches = Vec::with_capacity(self.patches.len());
        if dim_field {
            for p in &self.patches {
                patches.push(self.owner_values(&internal, p));
            }
            return Ok(Some(FieldData { name: ff.name.clone(), internal, patches }));
        }
        let entries = boundary_entries(&buf, h.binary, h.end)
            .map_err(|e| MeshError(format!("{}: {e}", ff.name)))?;
        for p in &self.patches {
            let Some(be) = entries.iter().find(|e| e.key == p.name) else {
                return bad(format!("{}: no boundaryField entry for patch {} (patterns are not supported)", ff.name, p.name));
            };
            let vals = match be.value_at {
                Some(at) => {
                    let v = field::parse_entry_pub(&buf, at, h.binary, p.n_faces)
                        .map_err(|e| MeshError(format!("{} {}: {e}", ff.name, p.name)))?;
                    if p.n_faces == 0 {
                        F32 { ncomp, data: Vec::new() }
                    } else {
                        if v.ncomp != ncomp || v.len() != p.n_faces {
                            return bad(format!("{} {}: value does not fit the patch", ff.name, p.name));
                        }
                        F32::from_values(v)
                    }
                }
                None if be.kind == "noSlip" => F32 { ncomp, data: vec![0.0; p.n_faces * ncomp] },
                None => match raw {
                    Some(body) => self.owner_values_raw(&buf[body..], ncomp, p),
                    None => self.owner_values(&internal, p),
                },
            };
            patches.push(vals);
        }
        Ok(Some(FieldData { name: ff.name.clone(), internal, patches }))
    }

    /// owner_values from the raw little-endian doubles of a binary list.
    fn owner_values_raw(&self, raw: &[u8], nc: usize, p: &Patch) -> F32 {
        let mut data = Vec::with_capacity(p.n_faces * nc);
        for f in p.start..p.start + p.n_faces {
            let c = self.owner[f] as usize;
            for k in 0..nc {
                let at = (c * nc + k) * 8;
                data.push(f64::from_le_bytes(raw[at..at + 8].try_into().unwrap()) as f32);
            }
        }
        F32 { ncomp: nc, data }
    }

    fn owner_values(&self, internal: &F32, p: &Patch) -> F32 {
        let nc = internal.ncomp;
        let mut data = Vec::with_capacity(p.n_faces * nc);
        for f in p.start..p.start + p.n_faces {
            let c = self.owner[f] as usize;
            data.extend_from_slice(&internal.data[c * nc..(c + 1) * nc]);
        }
        F32 { ncomp: nc, data }
    }

    /// The point ids of all boundary faces of "physical" and processor
    /// patches, marked: is_boundary[p].
    fn boundary_points(&self) -> Vec<bool> {
        let mut mark = vec![false; self.points.len()];
        for p in &self.patches {
            if p.kind == PatchKind::Geometric {
                continue;
            }
            for f in p.start..p.start + p.n_faces {
                for &q in self.faces.get(f) {
                    mark[q as usize] = true;
                }
            }
        }
        mark
    }

    /// Point values of the volume (InterpolateCellToPoint + the boundary
    /// override of GetVolFieldAtTimeStep). `around`: the cells around each
    /// inner point; `bface`: the boundary faces (of physical and processor
    /// patches, in patch order) around each boundary point.
    fn point_values(&self, fd: &FieldData, is_bnd: &[bool], around: &(Vec<u32>, Vec<u32>),
                    bface: &(Vec<u32>, Vec<u32>)) -> F32 {
        let nc = fd.internal.ncomp;
        let np = self.points.len();
        let iv = &fd.internal.data;
        let mut bv: Vec<f32> = Vec::new();
        for (pi, p) in self.patches.iter().enumerate() {
            if p.kind != PatchKind::Geometric {
                bv.extend_from_slice(&fd.patches[pi].data);
            }
        }
        let mut out = vec![0f32; np * nc];
        out.par_chunks_mut(nc * 4096).enumerate().for_each(|(k, chunk)| {
            let mut s = [0f64; 9];
            for (j, o) in chunk.chunks_mut(nc).enumerate() {
                let p = k * 4096 + j;
                let ((off, ids), vals) = if is_bnd[p] { (bface, &bv) } else { (around, iv) };
                let list = &ids[off[p] as usize..off[p + 1] as usize];
                s[..nc].iter_mut().for_each(|x| *x = 0.0);
                for &c in list {
                    let v = &vals[c as usize * nc..(c as usize + 1) * nc];
                    for q in 0..nc {
                        s[q] += v[q] as f64;
                    }
                }
                mean_into(o, &s[..nc], list.len() as f64);
            }
        });
        F32 { ncomp: nc, data: out }
    }

    /// Volume: cells and the cell and point values of `fields`.
    pub fn volume(&self, fields: &[FieldFile]) -> R<(Cells, Vec<(String, F32, F32)>)> {
        let t0 = std::time::Instant::now();
        let (cells, read) = rayon::join(
            || { let c = self.cells(); stamp("cells", t0); c },
            || fields.par_iter().map(|ff| self.read_field(ff, true)).collect::<Vec<_>>(),
        );
        let cells = cells?;
        let mut got = Vec::new();
        for r in read {
            if let Some(fd) = r? {
                got.push(fd);
            }
        }
        stamp("cells+fields", t0);
        let np = self.points.len();
        let is_bnd = self.boundary_points();
        let (around, bface) = rayon::join(
            || point_lists(np, |f: &mut dyn FnMut(usize, u32)| {
                for c in 0..self.n_cells {
                    let (a, b) = (cells.offsets[c] as usize, cells.offsets[c + 1] as usize);
                    for &p in &cells.conn[a..b] {
                        if !is_bnd[p as usize] {
                            f(p as usize, c as u32);
                        }
                    }
                }
            }),
            || point_lists(np, |f: &mut dyn FnMut(usize, u32)| {
                let mut k = 0u32;
                for p in self.patches.iter().filter(|p| p.kind != PatchKind::Geometric) {
                    for face in p.start..p.start + p.n_faces {
                        for &q in self.faces.get(face) {
                            f(q as usize, k);
                        }
                        k += 1;
                    }
                }
            }),
        );
        stamp("point lists", t0);
        let pts: Vec<F32> = got.iter().map(|fd| self.point_values(fd, &is_bnd, &around, &bface)).collect();
        stamp("point values", t0);
        let out = got
            .into_iter()
            .zip(pts)
            .map(|(fd, pv)| (fd.name, fd.internal, pv))
            .collect();
        Ok((cells, out))
    }
    /// Patches with their own points (in the order the faces visit them),
    /// faces in local ids, and the face and point values of `fields`.
    pub fn boundary(&self, fields: &[FieldFile]) -> R<Vec<PatchOut>> {
        let read: Vec<R<Option<FieldData>>> = fields.par_iter().map(|ff| self.read_field(ff, false)).collect();
        let mut got = Vec::new();
        for r in read {
            if let Some(fd) = r? {
                got.push(fd);
            }
        }
        let outs: Vec<PatchOut> = self
            .patches
            .par_iter()
            .enumerate()
            .map(|(pi, p)| {
                let mut local = std::collections::HashMap::<u32, u32>::new();
                let mut gl: Vec<u32> = Vec::new();
                let mut offs = Vec::with_capacity(p.n_faces + 1);
                let mut conn = Vec::new();
                offs.push(0i64);
                for f in p.start..p.start + p.n_faces {
                    for &q in self.faces.get(f) {
                        let id = *local.entry(q).or_insert_with(|| {
                            gl.push(q);
                            (gl.len() - 1) as u32
                        });
                        conn.push(id as i64);
                    }
                    offs.push(conn.len() as i64);
                }
                let mut pts = Vec::with_capacity(gl.len() * 3);
                for &q in &gl {
                    let x = self.points[q as usize];
                    pts.extend_from_slice(&[x[0] as f32, x[1] as f32, x[2] as f32]);
                }
                let mut data = Vec::new();
                for fd in &got {
                    let cv = fd.patches[pi].clone();
                    let nc = fd.internal.ncomp;
                    let cv = if cv.ncomp == 0 { F32 { ncomp: nc, data: Vec::new() } } else { cv };
                    let n = gl.len();
                    let mut sum = vec![0f64; n * nc];
                    let mut cnt = vec![0u32; n];
                    for j in 0..p.n_faces {
                        let val = &cv.data[j * nc..(j + 1) * nc];
                        for &q in &conn[offs[j] as usize..offs[j + 1] as usize] {
                            let q = q as usize;
                            cnt[q] += 1;
                            for k in 0..nc {
                                sum[q * nc + k] += val[k] as f64;
                            }
                        }
                    }
                    let mut pv = vec![0f32; n * nc];
                    for q in 0..n {
                        mean_into(&mut pv[q * nc..(q + 1) * nc], &sum[q * nc..(q + 1) * nc], cnt[q] as f64);
                    }
                    data.push((fd.name.clone(), cv, F32 { ncomp: nc, data: pv }));
                }
                PatchOut {
                    name: p.name.clone(),
                    kind: p.kind_name.clone(),
                    points: pts,
                    face_offsets: offs,
                    face_conn: conn,
                    data,
                }
            })
            .collect();
        Ok(outs)
    }
}

pub struct PatchOut {
    pub name: String,
    pub kind: String,
    pub points: Vec<f32>,
    pub face_offsets: Vec<i64>,
    pub face_conn: Vec<i64>,
    /// (field, face values, point values)
    pub data: Vec<(String, F32, F32)>,
}

/// The mean as InterpolateCellToPoint makes it: scalars sum / w, other
/// sizes sum * (1 / w); NaN without cells.
#[inline]
fn mean_into(out: &mut [f32], sum: &[f64], w: f64) {
    if out.len() == 1 {
        out[0] = if w != 0.0 { (sum[0] / w) as f32 } else { f32::NAN };
    } else {
        let inv = if w != 0.0 { 1.0 / w } else { f64::NAN };
        for (o, s) in out.iter_mut().zip(sum) {
            *o = (inv * s) as f32;
        }
    }
}

/// `nonuniform List<type> N (` at `i`: (components, N, index of the data).
fn binary_list_at(buf: &[u8], i: usize) -> Option<(usize, usize, usize)> {
    let (t, j) = token(buf, i);
    if t != b"nonuniform" {
        return None;
    }
    let (list, j) = token(buf, j);
    let nc = match list.strip_prefix(b"List<")?.strip_suffix(b">")? {
        b"scalar" | b"sphericalTensor" => 1,
        b"vector" => 3,
        b"symmTensor" => 6,
        b"tensor" => 9,
        _ => return None,
    };
    let (count, j) = token(buf, j);
    let n = parse_usize(count)?;
    let (o, j) = token(buf, j);
    if o != b"(" {
        return None;
    }
    Some((nc, n, j))
}

/// The value of a top-level keyword (`internalField`, `value`): the index
/// after the keyword. Binary lists of earlier entries are stepped over.
fn top_entry(buf: &[u8], from: usize, key: &[u8], binary: bool) -> Option<usize> {
    let mut i = from;
    loop {
        let (t, e) = token(buf, i);
        if t.is_empty() {
            return None;
        }
        if t == key {
            return Some(e);
        }
        if t == b"{" {
            i = block_close(buf, i, binary).ok()? + 1;
            continue;
        }
        if binary {
            if let Some(k) = skip_binary_list(buf, e) {
                i = k;
                continue;
            }
        }
        i = e;
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    /// Two hex cells side by side (x 0..2, y 0..1, z 0..1), written as an
    /// OpenFOAM case in ascii.
    fn two_hex(dir: &Path) {
        std::fs::create_dir_all(dir.join("constant/polyMesh")).unwrap();
        std::fs::create_dir_all(dir.join("1")).unwrap();
        let pm = dir.join("constant/polyMesh");
        let hdr = |class: &str, obj: &str| {
            format!("FoamFile\n{{\n    version 2.0;\n    format ascii;\n    class {class};\n    object {obj};\n}}\n// * * //\n\n")
        };
        let mut pts = String::new();
        for z in 0..2 {
            for y in 0..2 {
                for x in 0..3 {
                    pts += &format!("({x} {y} {z})\n");
                }
            }
        }
        let id = |x: u32, y: u32, z: u32| x + 3 * y + 6 * z;
        std::fs::write(pm.join("points"), hdr("vectorField", "points") + &format!("12\n(\n{pts})\n")).unwrap();
        // face 0: internal x=1 (owner 0, neighbour 1); then boundary faces.
        let faces: Vec<Vec<u32>> = vec![
            vec![id(1, 0, 0), id(1, 1, 0), id(1, 1, 1), id(1, 0, 1)],
            // left x=0 (cell 0)
            vec![id(0, 0, 0), id(0, 0, 1), id(0, 1, 1), id(0, 1, 0)],
            // right x=2 (cell 1)
            vec![id(2, 0, 0), id(2, 1, 0), id(2, 1, 1), id(2, 0, 1)],
            // bottom z=0, cells 0, 1
            vec![id(0, 0, 0), id(0, 1, 0), id(1, 1, 0), id(1, 0, 0)],
            vec![id(1, 0, 0), id(1, 1, 0), id(2, 1, 0), id(2, 0, 0)],
            // top z=1
            vec![id(0, 0, 1), id(1, 0, 1), id(1, 1, 1), id(0, 1, 1)],
            vec![id(1, 0, 1), id(2, 0, 1), id(2, 1, 1), id(1, 1, 1)],
            // front y=0
            vec![id(0, 0, 0), id(1, 0, 0), id(1, 0, 1), id(0, 0, 1)],
            vec![id(1, 0, 0), id(2, 0, 0), id(2, 0, 1), id(1, 0, 1)],
            // back y=1
            vec![id(0, 1, 0), id(0, 1, 1), id(1, 1, 1), id(1, 1, 0)],
            vec![id(1, 1, 0), id(1, 1, 1), id(2, 1, 1), id(2, 1, 0)],
        ];
        let mut fs = format!("{}\n(\n", faces.len());
        for f in &faces {
            fs += &format!("4({} {} {} {})\n", f[0], f[1], f[2], f[3]);
        }
        fs += ")\n";
        std::fs::write(pm.join("faces"), hdr("faceList", "faces") + &fs).unwrap();
        std::fs::write(pm.join("owner"), hdr("labelList", "owner") + "11\n(\n0 0 1 0 1 0 1 0 1 0 1\n)\n").unwrap();
        std::fs::write(pm.join("neighbour"), hdr("labelList", "neighbour") + "1\n(\n1\n)\n").unwrap();
        std::fs::write(
            pm.join("boundary"),
            hdr("polyBoundaryMesh", "boundary")
                + "3\n(\n    inlet\n    {\n        type patch;\n        nFaces 1;\n        startFace 1;\n    }\n    outlet\n    {\n        type patch;\n        nFaces 1;\n        startFace 2;\n    }\n    walls\n    {\n        type wall;\n        inGroups List<word> 1(wall);\n        nFaces 8;\n        startFace 3;\n    }\n)\n",
        )
        .unwrap();
        std::fs::write(
            dir.join("1/p"),
            hdr("volScalarField", "p")
                + "dimensions [0 2 -2 0 0 0 0];\ninternalField nonuniform List<scalar> 2(1 3);\nboundaryField\n{\n    inlet { type fixedValue; value uniform 10; }\n    outlet { type zeroGradient; }\n    walls { type zeroGradient; }\n}\n",
        )
        .unwrap();
        std::fs::write(
            dir.join("1/U"),
            hdr("volVectorField", "U")
                + "dimensions [0 1 -1 0 0 0 0];\ninternalField uniform (1 0 0);\nboundaryField\n{\n    inlet { type fixedValue; value uniform (2 0 0); }\n    outlet { type inletOutlet; inletValue uniform (0 0 0); value nonuniform List<vector> 1((5 0 0)); }\n    walls { type noSlip; }\n}\n",
        )
        .unwrap();
    }

    #[test]
    fn two_hex_volume_and_boundary() {
        let dir = std::env::temp_dir().join(format!("foamrs-t47-{}", std::process::id()));
        let _ = std::fs::remove_dir_all(&dir);
        two_hex(&dir);
        let pm = dir.join("constant/polyMesh");
        let m = Mesh::read(&pm, &pm).unwrap();
        assert_eq!(m.n_cells, 2);
        let ff = list_fields(&dir.join("1")).unwrap();
        assert_eq!(ff.iter().map(|f| f.name.as_str()).collect::<Vec<_>>(), vec!["U", "p"]);
        let (cells, data) = m.volume(&ff).unwrap();
        assert_eq!(cells.types, vec![VTK_HEXAHEDRON, VTK_HEXAHEDRON]);
        assert_eq!(cells.offsets, vec![0, 8, 16]);
        let p = &data.iter().find(|d| d.0 == "p").unwrap();
        assert_eq!(p.1.data, vec![1.0, 3.0]);
        // Point (0,0,0) is on the inlet (10) and walls (zeroGradient -> 1):
        // faces around it: inlet, bottom0, front0 -> (10 + 1 + 1) / 3.
        assert!((p.2.data[0] - 4.0).abs() < 1e-6);
        let b = m.boundary(&ff).unwrap();
        assert_eq!(b.len(), 3);
        assert_eq!(b[0].name, "inlet");
        assert_eq!(b[0].points.len(), 12);
        let u = b[2].data.iter().find(|d| d.0 == "U").unwrap();
        assert!(u.1.data.iter().all(|&x| x == 0.0)); // noSlip
        let uo = b[1].data.iter().find(|d| d.0 == "U").unwrap();
        assert_eq!(uo.1.data, vec![5.0, 0.0, 0.0]);
        std::fs::remove_dir_all(&dir).unwrap();
    }

    #[test]
    fn gzip_file() {
        use std::io::Write;
        let dir = std::env::temp_dir().join(format!("foamrs-t47gz-{}", std::process::id()));
        std::fs::create_dir_all(&dir).unwrap();
        let mut e = flate2::write::GzEncoder::new(Vec::new(), flate2::Compression::default());
        e.write_all(b"FoamFile\n{\n format ascii;\n class labelList;\n}\n3\n(\n4 5 6\n)\n").unwrap();
        std::fs::write(dir.join("owner.gz"), e.finish().unwrap()).unwrap();
        assert_eq!(read_labels(&dir.join("owner"), "owner").unwrap(), vec![4, 5, 6]);
        std::fs::remove_dir_all(&dir).unwrap();
    }
}
