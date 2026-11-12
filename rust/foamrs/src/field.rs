//! Parsing of OpenFOAM field files (volScalarField, volVectorField).
//!
//! Supported:
//! - format ascii and binary (arch "LSB;label=32;scalar=64");
//! - internalField uniform / nonuniform;
//! - boundaryField: the `value` entry of one patch, uniform / nonuniform.
//!
//! Not supported (returns an error, the caller falls back to VTK):
//! - compressed files (.gz), scalar=32, big-endian, tensor fields.

use std::fmt;

#[derive(Debug)]
pub struct FieldError(pub String);

impl fmt::Display for FieldError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        write!(f, "{}", self.0)
    }
}

fn err<T>(msg: impl Into<String>) -> Result<T, FieldError> {
    Err(FieldError(msg.into()))
}

/// Values of one field entry: `n` items of `ncomp` doubles each.
#[derive(Debug, Clone, PartialEq)]
pub struct Values {
    pub ncomp: usize,
    pub data: Vec<f64>,
}

impl Values {
    pub fn len(&self) -> usize {
        if self.ncomp == 0 {
            0
        } else {
            self.data.len() / self.ncomp
        }
    }
}

#[derive(Debug, Clone, Copy, PartialEq)]
enum Format {
    Ascii,
    Binary,
}

fn find(hay: &[u8], needle: &[u8], from: usize) -> Option<usize> {
    if needle.is_empty() || from >= hay.len() {
        return None;
    }
    hay[from..]
        .windows(needle.len())
        .position(|w| w == needle)
        .map(|p| p + from)
}

fn is_space(b: u8) -> bool {
    b == b' ' || b == b'\t' || b == b'\n' || b == b'\r'
}

fn skip_space(buf: &[u8], mut i: usize) -> usize {
    while i < buf.len() && is_space(buf[i]) {
        i += 1;
    }
    i
}

/// Read one whitespace- or delimiter-separated token at `i`.
fn token(buf: &[u8], i: usize) -> (&[u8], usize) {
    let s = skip_space(buf, i);
    let mut e = s;
    while e < buf.len() && !is_space(buf[e]) && !b"();{}".contains(&buf[e]) {
        e += 1;
    }
    (&buf[s..e], e)
}

fn header_format(buf: &[u8]) -> Result<Format, FieldError> {
    let head_end = find(buf, b"}", 0).unwrap_or(buf.len().min(4096));
    let head = &buf[..head_end];
    if find(head, b"binary", 0).is_some() {
        if let Some(a) = find(head, b"arch", 0) {
            let line_end = find(head, b";", a).unwrap_or(head.len());
            let arch = &head[a..line_end];
            if find(arch, b"MSB", 0).is_some() {
                return err("big-endian binary field");
            }
            if find(arch, b"scalar=32", 0).is_some() {
                return err("single-precision binary field");
            }
        }
        Ok(Format::Binary)
    } else {
        Ok(Format::Ascii)
    }
}

fn ncomp_of(kind: &[u8]) -> Result<usize, FieldError> {
    match kind {
        b"scalar" => Ok(1),
        b"vector" => Ok(3),
        b"symmTensor" => Ok(6),
        b"tensor" => Ok(9),
        _ => err(format!("unknown list type {}", String::from_utf8_lossy(kind))),
    }
}

fn parse_f64(t: &[u8]) -> Result<f64, FieldError> {
    let s = std::str::from_utf8(t).map_err(|_| FieldError("bad number".into()))?;
    s.parse::<f64>()
        .map_err(|_| FieldError(format!("bad number '{}'", s)))
}

/// Parse a uniform value at `i` (after the word `uniform`): `3.5` or `(1 2 3)`.
fn parse_uniform(buf: &[u8], i: usize) -> Result<(Vec<f64>, usize), FieldError> {
    let s = skip_space(buf, i);
    if s < buf.len() && buf[s] == b'(' {
        let close = find(buf, b")", s).ok_or_else(|| FieldError("no ')'".into()))?;
        let inner = &buf[s + 1..close];
        let mut out = Vec::new();
        for t in inner.split(|b| is_space(*b)).filter(|t| !t.is_empty()) {
            out.push(parse_f64(t)?);
        }
        Ok((out, close + 1))
    } else {
        let (t, e) = token(buf, s);
        Ok((vec![parse_f64(t)?], e))
    }
}

/// Parse the value after an entry keyword (`internalField` or `value`).
/// `n_uniform`: how many items a uniform value expands to.
fn parse_entry(
    buf: &[u8],
    at: usize,
    fmt: Format,
    n_uniform: usize,
) -> Result<Values, FieldError> {
    let (kind, i) = token(buf, at);
    match kind {
        b"uniform" => {
            let (v, _) = parse_uniform(buf, i)?;
            let ncomp = v.len();
            let mut data = Vec::with_capacity(ncomp * n_uniform);
            for _ in 0..n_uniform {
                data.extend_from_slice(&v);
            }
            Ok(Values { ncomp, data })
        }
        b"nonuniform" => {
            // List<vector> 1234 ( ... )
            let (list, i) = token(buf, i);
            let ty = list
                .strip_prefix(b"List<")
                .and_then(|r| r.strip_suffix(b">"))
                .ok_or_else(|| FieldError("expected List<...>".into()))?;
            let ncomp = ncomp_of(ty)?;
            let (count, i) = token(buf, i);
            let n: usize = std::str::from_utf8(count)
                .ok()
                .and_then(|s| s.parse().ok())
                .ok_or_else(|| FieldError("bad list size".into()))?;
            let open = skip_space(buf, i);
            if open >= buf.len() || buf[open] != b'(' {
                return err("expected '(' after list size");
            }
            let body = open + 1;
            match fmt {
                Format::Binary => {
                    let bytes = n * ncomp * 8;
                    if body + bytes > buf.len() {
                        return err("binary list runs past end of file");
                    }
                    let raw = &buf[body..body + bytes];
                    let mut data = vec![0f64; n * ncomp];
                    for (k, chunk) in raw.chunks_exact(8).enumerate() {
                        data[k] = f64::from_le_bytes(chunk.try_into().unwrap());
                    }
                    if body + bytes >= buf.len() || buf[body + bytes] != b')' {
                        return err("binary list not closed by ')'");
                    }
                    Ok(Values { ncomp, data })
                }
                Format::Ascii => {
                    let mut data = Vec::with_capacity(n * ncomp);
                    let mut j = body;
                    let mut depth = 0usize;
                    while data.len() < n * ncomp {
                        j = skip_space(buf, j);
                        if j >= buf.len() {
                            return err("ascii list runs past end of file");
                        }
                        match buf[j] {
                            b'(' => {
                                depth += 1;
                                j += 1;
                            }
                            b')' => {
                                if depth == 0 {
                                    return err("ascii list shorter than its size");
                                }
                                depth -= 1;
                                j += 1;
                            }
                            _ => {
                                let (t, e) = token(buf, j);
                                data.push(parse_f64(t)?);
                                j = e;
                            }
                        }
                    }
                    Ok(Values { ncomp, data })
                }
            }
        }
        _ => err(format!(
            "expected uniform/nonuniform, got '{}'",
            String::from_utf8_lossy(kind)
        )),
    }
}

/// `parse_entry` for polymesh.rs: the value at `at` (after its keyword).
pub fn parse_entry_pub(buf: &[u8], at: usize, binary: bool, n_uniform: usize) -> Result<Values, FieldError> {
    parse_entry(buf, at, if binary { Format::Binary } else { Format::Ascii }, n_uniform)
}

/// Internal field values. `n_cells` is needed only for `uniform`.
pub fn internal_field(buf: &[u8], n_cells: usize) -> Result<Values, FieldError> {
    let fmt = header_format(buf)?;
    let at = find(buf, b"internalField", 0).ok_or_else(|| FieldError("no internalField".into()))?;
    parse_entry(buf, at + b"internalField".len(), fmt, n_cells)
}

/// Locate the dictionary of `patch` inside boundaryField; returns body range.
fn patch_block(buf: &[u8], patch: &str) -> Option<(usize, usize)> {
    let bf = find(buf, b"boundaryField", 0)?;
    let open = find(buf, b"{", bf)?;
    let name = patch.as_bytes();
    let mut i = open + 1;
    loop {
        let (t, e) = token(buf, i);
        if t.is_empty() {
            // `}` closes boundaryField, or a stray delimiter.
            let s = skip_space(buf, e);
            if s >= buf.len() || buf[s] == b'}' {
                return None;
            }
            i = s + 1;
            continue;
        }
        let s = skip_space(buf, e);
        if s >= buf.len() {
            return None;
        }
        if buf[s] == b'{' {
            // find the matching closing brace, skipping binary blobs is not
            // needed: binary lists never contain a lone '}' before their ')'
            // in practice is NOT guaranteed, so walk lists by their size.
            let close = block_end(buf, s)?;
            if t == name {
                return Some((s + 1, close));
            }
            i = close + 1;
        } else {
            // `#includeEtc "..."` or `key value;`
            let semi = find(buf, b"\n", s).unwrap_or(buf.len());
            i = semi;
        }
    }
}

/// If a binary `nonuniform List<type> N (...)` starts at `i`, the index
/// just past its closing `)`.
fn skip_list(buf: &[u8], i: usize, fmt: Format) -> Option<usize> {
    if fmt != Format::Binary {
        return None;
    }
    let (kind, j) = token(buf, i);
    if kind != b"nonuniform" {
        return None;
    }
    let (list, j) = token(buf, j);
    let ty = list.strip_prefix(b"List<")?.strip_suffix(b">")?;
    let nc = ncomp_of(ty).ok()?;
    let (count, j) = token(buf, j);
    let n: usize = std::str::from_utf8(count).ok()?.parse().ok()?;
    let p = skip_space(buf, j);
    if p < buf.len() && buf[p] == b'(' {
        Some(p + 1 + n * nc * 8 + 1)
    } else {
        None
    }
}

/// End of the `{ ... }` block that opens at `open`, stepping over binary
/// list payloads by their declared size.
fn block_end(buf: &[u8], open: usize) -> Option<usize> {
    let fmt = header_format(buf).ok()?;
    let mut depth = 0i32;
    let mut i = open;
    while i < buf.len() {
        match buf[i] {
            b'{' => depth += 1,
            b'}' => {
                depth -= 1;
                if depth == 0 {
                    return Some(i);
                }
            }
            b'L' if fmt == Format::Binary && buf[i..].starts_with(b"List<") => {
                // List<type> N ( <raw> )
                let (list, j) = token(buf, i);
                let ty = list.strip_prefix(b"List<").and_then(|r| r.strip_suffix(b">"));
                if let Some(ty) = ty {
                    if let Ok(nc) = ncomp_of(ty) {
                        let (count, j) = token(buf, j);
                        if let Some(n) = std::str::from_utf8(count).ok().and_then(|s| s.parse::<usize>().ok()) {
                            let p = skip_space(buf, j);
                            if p < buf.len() && buf[p] == b'(' {
                                i = p + 1 + n * nc * 8 + 1;
                                continue;
                            }
                        }
                    }
                }
            }
            _ => {}
        }
        i += 1;
    }
    None
}

/// `value` of one patch, expanded to `n_faces` for uniform values.
/// Ok(None) — the patch exists but has no `value` (zeroGradient etc.).
pub fn patch_value(buf: &[u8], patch: &str, n_faces: usize) -> Result<Option<Values>, FieldError> {
    let fmt = header_format(buf)?;
    let (s, e) = patch_block(buf, patch).ok_or_else(|| FieldError(format!("no patch {}", patch)))?;
    let body = &buf[..e];
    let mut i = s;
    loop {
        let (t, j) = token(body, i);
        if t.is_empty() {
            return Ok(None);
        }
        if t == b"value" {
            return parse_entry(body, j, fmt, n_faces).map(Some);
        }
        // Skip this entry up to its `;`. A binary list (`gradient`,
        // `refValue` of mixed conditions) is stepped over by its size:
        // its raw bytes may contain anything, `;` and `)` included.
        let mut k = skip_list(body, j, fmt).unwrap_or(j);
        let mut depth = 0i32;
        while k < body.len() {
            match body[k] {
                b'(' => depth += 1,
                b')' => depth -= 1,
                b';' if depth <= 0 => break,
                _ => {}
            }
            k += 1;
        }
        i = k + 1;
        if i >= body.len() {
            return Ok(None);
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    const ASCII_VEC: &str = "FoamFile\n{\n    format ascii;\n    class volVectorField;\n}\n\
dimensions [0 1 -1 0 0 0 0];\n\
internalField nonuniform List<vector> \n3\n(\n(1 2 3)\n(4 5 6)\n(7 8 -9.5e-1)\n)\n;\n\
boundaryField\n{\n    #includeEtc \"caseDicts/setConstraintTypes\"\n\
    inlet\n    {\n        type fixedValue;\n        value uniform (10 0 0);\n    }\n\
    body\n    {\n        type noSlip;\n    }\n\
    noz\n    {\n        type flowRateInletVelocity;\n        massFlowRate 0.2;\n        value nonuniform List<vector> 2((1 1 1)(2 2 2));\n    }\n}\n";

    #[test]
    fn ascii_vector_internal() {
        let v = internal_field(ASCII_VEC.as_bytes(), 0).unwrap();
        assert_eq!(v.ncomp, 3);
        assert_eq!(v.data, vec![1., 2., 3., 4., 5., 6., 7., 8., -0.95]);
    }

    #[test]
    fn ascii_patches() {
        let b = ASCII_VEC.as_bytes();
        let inlet = patch_value(b, "inlet", 2).unwrap().unwrap();
        assert_eq!(inlet.data, vec![10., 0., 0., 10., 0., 0.]);
        assert!(patch_value(b, "body", 5).unwrap().is_none());
        let noz = patch_value(b, "noz", 2).unwrap().unwrap();
        assert_eq!(noz.data, vec![1., 1., 1., 2., 2., 2.]);
        assert!(patch_value(b, "missing", 1).is_err());
    }

    #[test]
    fn uniform_scalar() {
        let s = "FoamFile\n{\n format ascii;\n}\ninternalField uniform 101325;\nboundaryField\n{\n}\n";
        let v = internal_field(s.as_bytes(), 4).unwrap();
        assert_eq!(v.ncomp, 1);
        assert_eq!(v.data, vec![101325.; 4]);
    }

    #[test]
    fn binary_scalar_and_patch() {
        let mut b: Vec<u8> = b"FoamFile\n{\n    format binary;\n    arch \"LSB;label=32;scalar=64\";\n}\ninternalField nonuniform List<scalar> 3(".to_vec();
        for x in [1.5f64, -2.0, 3.25] {
            b.extend_from_slice(&x.to_le_bytes());
        }
        b.extend_from_slice(b");\nboundaryField\n{\n    wall\n    {\n        type fixedValue;\n        value nonuniform List<scalar> 2(");
        for x in [7.0f64, 8.0] {
            b.extend_from_slice(&x.to_le_bytes());
        }
        b.extend_from_slice(b");\n    }\n}\n");
        let v = internal_field(&b, 0).unwrap();
        assert_eq!(v.data, vec![1.5, -2.0, 3.25]);
        let w = patch_value(&b, "wall", 2).unwrap().unwrap();
        assert_eq!(w.data, vec![7.0, 8.0]);
    }

    #[test]
    fn binary_payload_with_brace_bytes() {
        // A double whose bytes contain '}' (0x7D) and '{' must not break
        // the patch search.
        let tricky = f64::from_le_bytes([0x7D, 0x7B, 0x29, 0x28, 0x3B, 0, 0, 0x40]);
        let mut b: Vec<u8> = b"FoamFile\n{\n format binary;\n arch \"LSB;label=32;scalar=64\";\n}\ninternalField uniform 0;\nboundaryField\n{\n    a\n    {\n        type fixedValue;\n        value nonuniform List<scalar> 1(".to_vec();
        b.extend_from_slice(&tricky.to_le_bytes());
        b.extend_from_slice(b");\n    }\n    b\n    {\n        type fixedValue;\n        value uniform 5;\n    }\n}\n");
        let a = patch_value(&b, "a", 1).unwrap().unwrap();
        assert_eq!(a.data[0].to_bits(), tricky.to_bits());
        let bb = patch_value(&b, "b", 3).unwrap().unwrap();
        assert_eq!(bb.data, vec![5.0, 5.0, 5.0]);
    }
}
