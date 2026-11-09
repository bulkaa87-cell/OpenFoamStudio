//! Small vector helpers. All values are f64: the meshes come in mm, and
//! f32 has not enough digits for a 1e-4 weld at 400 units.

pub type Vec3 = [f64; 3];

#[inline]
pub fn add(a: Vec3, b: Vec3) -> Vec3 {
    [a[0] + b[0], a[1] + b[1], a[2] + b[2]]
}
#[inline]
pub fn sub(a: Vec3, b: Vec3) -> Vec3 {
    [a[0] - b[0], a[1] - b[1], a[2] - b[2]]
}
#[inline]
pub fn scale(a: Vec3, s: f64) -> Vec3 {
    [a[0] * s, a[1] * s, a[2] * s]
}
#[inline]
pub fn dot(a: Vec3, b: Vec3) -> f64 {
    a[0] * b[0] + a[1] * b[1] + a[2] * b[2]
}
#[inline]
pub fn cross(a: Vec3, b: Vec3) -> Vec3 {
    [a[1] * b[2] - a[2] * b[1], a[2] * b[0] - a[0] * b[2], a[0] * b[1] - a[1] * b[0]]
}
#[inline]
pub fn norm(a: Vec3) -> f64 {
    dot(a, a).sqrt()
}
#[inline]
pub fn dist(a: Vec3, b: Vec3) -> f64 {
    norm(sub(a, b))
}
#[inline]
pub fn dist2(a: Vec3, b: Vec3) -> f64 {
    let d = sub(a, b);
    dot(d, d)
}
#[inline]
pub fn tri_area(a: Vec3, b: Vec3, c: Vec3) -> f64 {
    0.5 * norm(cross(sub(b, a), sub(c, a)))
}
/// det(a, b, c) = a · (b × c)
#[inline]
pub fn det3(a: Vec3, b: Vec3, c: Vec3) -> f64 {
    dot(a, cross(b, c))
}
