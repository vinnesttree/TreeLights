//! Per-LED hypotheses and point refinement (reconstruction/hypotheses.py: build_hypotheses with
//! geometry.refine_point, and fill.refine_points).
//!
//! Same algorithms and orderings as the Python; the nonlinear least squares are a
//! Levenberg-Marquardt with analytic Jacobians instead of scipy's finite differences, so
//! results agree with the Python to the solvers' tolerances, not bit for bit.

use nalgebra::{Matrix3, SymmetricEigen, Vector3};
use numpy::ndarray::Array2;
use numpy::{
    IntoPyArray, PyArray1, PyArray2, PyReadonlyArray1, PyReadonlyArray2, PyReadonlyArray3,
};
use pyo3::exceptions::PyValueError;
use pyo3::prelude::*;
use rayon::prelude::*;

const INLIER_PX: f64 = 3.0;
const MAX_HYPS: usize = 5;
const MAX_PAIRS: usize = 40;

/// A camera: x_cam = R X + t, pinhole with radial distortion k1, k2.
#[derive(Clone, Copy)]
struct Cam {
    r: Matrix3<f64>,
    t: Vector3<f64>,
    f: f64,
    cx: f64,
    cy: f64,
    k1: f64,
    k2: f64,
    c: Vector3<f64>, // center -R^T t
}

impl Cam {
    /// cv2.undistortPoints with dist [k1, k2, 0, 0] and the default criteria (5 fixed-point
    /// iterations), checked against OpenCV 4.10 numerically.
    fn undistort(&self, u: f64, v: f64) -> (f64, f64) {
        let ifx = 1.0 / self.f;
        let x0 = (u - self.cx) * ifx;
        let y0 = (v - self.cy) * ifx;
        let (mut x, mut y) = (x0, y0);
        for _ in 0..5 {
            let r2 = x * x + y * y;
            let icdist = 1.0 / (1.0 + (self.k2 * r2 + self.k1) * r2);
            if icdist < 0.0 {
                return (x0, y0);
            }
            x = x0 * icdist;
            y = y0 * icdist;
        }
        (x, y)
    }

    /// World ray direction (unit) through a pixel.
    fn ray(&self, u: f64, v: f64) -> Vector3<f64> {
        let (x, y) = self.undistort(u, v);
        let d = self.r.transpose() * Vector3::new(x, y, 1.0);
        d / d.norm()
    }

    /// hypotheses._project: None when the point isn't in front (z <= 1e-9).
    fn project_front(&self, p: &Vector3<f64>) -> Option<(f64, f64)> {
        let xc = self.r * p + self.t;
        if xc[2] <= 1e-9 {
            return None;
        }
        let x = xc[0] / xc[2];
        let y = xc[1] / xc[2];
        let r2 = x * x + y * y;
        let d = 1.0 + self.k1 * r2 + self.k2 * r2 * r2;
        Some((x * d * self.f + self.cx, y * d * self.f + self.cy))
    }

    /// Projection with the |z| < 1e-9 clamp of geometry.refine_point / fill.View.project, and
    /// its Jacobian with respect to the point (rows u, v).
    #[allow(clippy::needless_range_loop)]
    fn project_jac(&self, p: &Vector3<f64>) -> ((f64, f64), [[f64; 3]; 2]) {
        let xc = self.r * p + self.t;
        let clamped = xc[2].abs() < 1e-9;
        let z = if clamped { 1e-9 } else { xc[2] };
        let x = xc[0] / z;
        let y = xc[1] / z;
        let r2 = x * x + y * y;
        let d = 1.0 + self.k1 * r2 + self.k2 * r2 * r2;
        let uv = (self.f * x * d + self.cx, self.f * y * d + self.cy);
        // d d / d r2, then chain through x, y.
        let dd = self.k1 + 2.0 * self.k2 * r2;
        let dudx = self.f * (d + x * dd * 2.0 * x);
        let dudy = self.f * x * dd * 2.0 * y;
        let dvdx = self.f * y * dd * 2.0 * x;
        let dvdy = self.f * (d + y * dd * 2.0 * y);
        // d(x, y) / d xc
        let iz = 1.0 / z;
        let dz = if clamped { 0.0 } else { 1.0 };
        let dx = [iz, 0.0, -x * iz * dz];
        let dy = [0.0, iz, -y * iz * dz];
        let mut j = [[0.0; 3]; 2];
        for c in 0..3 {
            // d xc / d p = R
            let mut gx = 0.0;
            let mut gy = 0.0;
            for k in 0..3 {
                gx += dx[k] * self.r[(k, c)];
                gy += dy[k] * self.r[(k, c)];
            }
            j[0][c] = dudx * gx + dudy * gy;
            j[1][c] = dvdx * gx + dvdy * gy;
        }
        (uv, j)
    }
}

fn cams_from(
    r: &PyReadonlyArray3<f64>,
    t: &PyReadonlyArray2<f64>,
    k: &PyReadonlyArray2<f64>,
) -> PyResult<Vec<Cam>> {
    let (r, t, k) = (r.as_array(), t.as_array(), k.as_array());
    let n = r.shape()[0];
    if r.shape()[1..] != [3, 3] || t.shape() != [n, 3] || k.shape() != [n, 5] {
        return Err(PyValueError::new_err(
            "cameras: R (S,3,3), t (S,3), K (S,5: f, cx, cy, k1, k2)",
        ));
    }
    Ok((0..n)
        .map(|s| {
            let rm = Matrix3::from_fn(|i, j| r[[s, i, j]]);
            let tv = Vector3::new(t[[s, 0]], t[[s, 1]], t[[s, 2]]);
            Cam {
                r: rm,
                t: tv,
                f: k[[s, 0]],
                cx: k[[s, 1]],
                cy: k[[s, 2]],
                k1: k[[s, 3]],
                k2: k[[s, 4]],
                c: -(rm.transpose() * tv),
            }
        })
        .collect())
}

/// One observation of a point for refinement: camera, pixel, weight (1 / sigma).
#[derive(Clone, Copy)]
struct Ob {
    cam: usize,
    u: f64,
    v: f64,
    w: f64,
}

fn weighted_sys(x: &Vector3<f64>, obs: &[Ob], cams: &[Cam]) -> (f64, Matrix3<f64>, Vector3<f64>) {
    let mut cost = 0.0;
    let mut h = Matrix3::zeros();
    let mut g = Vector3::zeros();
    for o in obs {
        let ((pu, pv), j) = cams[o.cam].project_jac(x);
        let r = [(pu - o.u) * o.w, (pv - o.v) * o.w];
        for (row, rr) in j.iter().zip(r) {
            let jr = Vector3::new(row[0] * o.w, row[1] * o.w, row[2] * o.w);
            h += jr * jr.transpose();
            g += jr * rr;
            cost += rr * rr;
        }
    }
    (cost, h, g)
}

fn weighted_cost(x: &Vector3<f64>, obs: &[Ob], cams: &[Cam]) -> f64 {
    obs.iter()
        .map(|o| {
            let ((pu, pv), _) = cams[o.cam].project_jac(x);
            let (a, b) = ((pu - o.u) * o.w, (pv - o.v) * o.w);
            a * a + b * b
        })
        .sum()
}

/// geometry.refine_point: nonlinear triangulation with fixed cameras (linear loss).
/// Returns (X, per-observation reprojection error px, covariance inv(J^T J) or None if singular).
fn refine_point(
    x0: Vector3<f64>,
    obs: &[Ob],
    cams: &[Cam],
) -> (Vector3<f64>, Vec<f64>, Option<Matrix3<f64>>) {
    let mut x = x0;
    let (mut cost, mut h, mut g) = weighted_sys(&x, obs, cams);
    let mut lambda = 1e-3;
    for _ in 0..100 {
        if g.amax() <= 1e-14 * (1.0 + cost) {
            break;
        }
        let mut a = h;
        for k in 0..3 {
            a[(k, k)] += lambda * h[(k, k)].max(1e-30);
        }
        let Some(step) = a.cholesky().map(|c| c.solve(&(-g))) else {
            lambda *= 10.0;
            if lambda > 1e16 {
                break;
            }
            continue;
        };
        let xn = x + step;
        let cn = weighted_cost(&xn, obs, cams);
        if cn.is_finite() && cn <= cost {
            let small = step.norm() <= 1e-13 * (x.norm() + 1e-13) || cost - cn <= 1e-16 * cost;
            x = xn;
            (cost, h, g) = weighted_sys(&x, obs, cams);
            lambda = (lambda * 0.3).max(1e-12);
            if small {
                break;
            }
        } else {
            lambda *= 10.0;
            if lambda > 1e16 {
                break;
            }
        }
    }
    let errs = obs
        .iter()
        .map(|o| {
            let ((pu, pv), _) = cams[o.cam].project_jac(&x);
            (pu - o.u).hypot(pv - o.v)
        })
        .collect();
    (x, errs, h.try_inverse())
}

/// geometry.triangulate_rays for two or more rays (np.linalg.lstsq on the 3x3 normal system).
fn triangulate(cs: &[Vector3<f64>], ds: &[Vector3<f64>]) -> Vector3<f64> {
    let mut a = Matrix3::zeros();
    let mut b = Vector3::zeros();
    for (c, d) in cs.iter().zip(ds) {
        let d = d / d.norm();
        let p = Matrix3::identity() - d * d.transpose();
        a += p;
        b += p * c;
    }
    let svd = a.svd(true, true);
    let smax = svd.singular_values.max();
    svd.solve(&b, f64::EPSILON * 3.0 * smax)
        .unwrap_or_else(|_| Vector3::zeros())
}

#[derive(Clone, Copy)]
struct Entry {
    idx: usize, // index into the caller's entry arrays
    seg: usize,
    u: f64,
    v: f64,
    score: f64,
    sg: f64,
    diffuse: bool,
}

struct Hyp {
    x: Vector3<f64>,
    inl: Vec<Entry>,
    err_px: f64,
    angle: f64,
    sigma: f64,
    score: f64,
    nerr: f64,
    diffuse: f64,
}

/// hypotheses._inliers: per segment (in first-appearance order), the candidate fitting X best
/// relative to its own uncertainty (the first such on ties), if within INLIER_PX sigmas.
fn inliers(x: &Vector3<f64>, by_seg: &[(usize, Vec<Entry>)], cams: &[Cam]) -> Vec<Entry> {
    let mut out = Vec::new();
    for (s, cands) in by_seg {
        let Some((pu, pv)) = cams[*s].project_front(x) else {
            continue;
        };
        let mut best = cands[0];
        let mut best_key = (best.u - pu).hypot(best.v - pv) / best.sg;
        for c in &cands[1..] {
            let key = (c.u - pu).hypot(c.v - pv) / c.sg;
            if key < best_key {
                best = *c;
                best_key = key;
            }
        }
        if (best.u - pu).hypot(best.v - pv) < INLIER_PX * best.sg {
            out.push(best);
        }
    }
    out
}

fn refine_inliers(
    x: Vector3<f64>,
    inl: &[Entry],
    cams: &[Cam],
) -> (Vector3<f64>, Vec<f64>, Option<Matrix3<f64>>) {
    let obs: Vec<Ob> = inl
        .iter()
        .map(|e| Ob {
            cam: e.seg,
            u: e.u,
            v: e.v,
            w: 1.0 / e.sg.max(1.0),
        })
        .collect();
    refine_point(x, &obs, cams)
}

fn sorted_segs(inl: &[Entry]) -> Vec<usize> {
    let mut s: Vec<usize> = inl.iter().map(|e| e.seg).collect();
    s.sort_unstable();
    s
}

fn eff_angle(a: &Vector3<f64>, b: &Vector3<f64>) -> f64 {
    let d = a.dot(b).clamp(-1.0, 1.0).acos().to_degrees();
    d.min(180.0 - d)
}

fn mean(v: impl Iterator<Item = f64>) -> f64 {
    let (s, n) = v.fold((0.0, 0usize), |(s, n), x| (s + x, n + 1));
    s / n as f64
}

/// The body of build_hypotheses' loop for one LED; entries in the caller's order.
fn led_hypotheses(mut entries: Vec<Entry>, cams: &[Cam], merge: f64) -> Vec<Hyp> {
    let mut seen: Vec<usize> = entries.iter().map(|e| e.seg).collect();
    seen.sort_unstable();
    seen.dedup();
    if seen.len() < 2 {
        return Vec::new();
    }
    // sorted(key=-score) is stable: equal scores keep their order.
    entries.sort_by(|a, b| {
        b.score
            .partial_cmp(&a.score)
            .unwrap_or(std::cmp::Ordering::Equal)
    });
    let mut by_seg: Vec<(usize, Vec<Entry>)> = Vec::new();
    for e in &entries {
        match by_seg.iter_mut().find(|(s, _)| *s == e.seg) {
            Some((_, v)) => v.push(*e),
            None => by_seg.push((e.seg, vec![*e])),
        }
    }
    let mut pairs = Vec::new();
    'outer: for (ai, a) in entries.iter().enumerate() {
        for b in &entries[ai + 1..] {
            if a.seg != b.seg {
                pairs.push((*a, *b));
                if pairs.len() == MAX_PAIRS {
                    break 'outer;
                }
            }
        }
    }
    let mut hyps: Vec<Hyp> = Vec::new();
    for (a, b) in pairs {
        let (ca, cb) = (&cams[a.seg], &cams[b.seg]);
        let (da, db) = (ca.ray(a.u, a.v), cb.ray(b.u, b.v));
        let cosang = da.dot(&db).clamp(-1.0, 1.0);
        if cosang.acos().to_degrees() < 1.0 {
            continue;
        }
        let x = triangulate(&[ca.c, cb.c], &[da, db]);
        let inl = inliers(&x, &by_seg, cams);
        if inl.len() < 2 {
            continue;
        }
        let (x, _errs, _cov) = refine_inliers(x, &inl, cams);
        let inl = inliers(&x, &by_seg, cams);
        if inl.len() < 2 {
            continue;
        }
        let segs = sorted_segs(&inl);
        if hyps
            .iter()
            .any(|h| (h.x - x).norm() < merge && sorted_segs(&h.inl) == segs)
        {
            continue;
        }
        let (x, errs, cov) = refine_inliers(x, &inl, cams);
        let dirs: Vec<Vector3<f64>> = inl
            .iter()
            .map(|e| {
                let d = cams[e.seg].c - x;
                d / d.norm()
            })
            .collect();
        let mut angle = f64::NEG_INFINITY;
        for i in 0..dirs.len() {
            for j in i + 1..dirs.len() {
                angle = angle.max(eff_angle(&dirs[i], &dirs[j]));
            }
        }
        let cov = cov.unwrap_or_else(|| Matrix3::identity() * 1e6);
        let emax = SymmetricEigen::new(cov).eigenvalues.max();
        hyps.push(Hyp {
            x,
            err_px: mean(errs.iter().copied()),
            angle,
            sigma: emax.max(0.0).sqrt(),
            score: mean(inl.iter().map(|e| e.score)),
            nerr: mean(errs.iter().zip(&inl).map(|(e, o)| e / o.sg)),
            diffuse: mean(inl.iter().map(|e| if e.diffuse { 1.0 } else { 0.0 })),
            inl,
        });
    }
    // Stable sort by (-n, diffuse, nerr), then collapse near-duplicates.
    hyps.sort_by(|a, b| {
        b.inl
            .len()
            .cmp(&a.inl.len())
            .then(
                a.diffuse
                    .partial_cmp(&b.diffuse)
                    .unwrap_or(std::cmp::Ordering::Equal),
            )
            .then(
                a.nerr
                    .partial_cmp(&b.nerr)
                    .unwrap_or(std::cmp::Ordering::Equal),
            )
    });
    let mut uniq: Vec<Hyp> = Vec::new();
    for h in hyps {
        if !uniq.iter().any(|u| (h.x - u.x).norm() < merge) {
            uniq.push(h);
        }
    }
    uniq.truncate(MAX_HYPS);
    uniq
}

type HypArrays<'py> = (
    Bound<'py, PyArray1<i64>>,
    Bound<'py, PyArray2<f64>>,
    Bound<'py, PyArray2<f64>>,
    Bound<'py, PyArray1<i64>>,
    Bound<'py, PyArray1<i64>>,
);

/// build_hypotheses for all LEDs.
///
/// Cameras: R (S,3,3), t (S,3), K (S,5: f, cx, cy, k1, k2). Candidate entries, in the order
/// Python builds them (by segment in camera order, then candidate order): LED, segment index,
/// pixel (E,2), score, sigma_px (already >= 1), diffuse (0/1). Entries of LEDs outside
/// 0..n_leds are ignored.
///
/// Returns, per hypothesis (grouped by LED in increasing order, best first): LED, X (H,3),
/// stats (H,6: err_px, angle_deg, sigma, score, nerr, diffuse), and its inlier entries as
/// offsets (H+1) into a flat array of entry indices (segments in inlier order).
#[pyfunction]
#[pyo3(signature = (cam_r, cam_t, cam_k, e_led, e_seg, e_xy, e_score, e_sigma, e_diffuse, n_leds, merge))]
#[allow(clippy::too_many_arguments)]
fn build_hypotheses<'py>(
    py: Python<'py>,
    cam_r: PyReadonlyArray3<f64>,
    cam_t: PyReadonlyArray2<f64>,
    cam_k: PyReadonlyArray2<f64>,
    e_led: PyReadonlyArray1<i64>,
    e_seg: PyReadonlyArray1<i64>,
    e_xy: PyReadonlyArray2<f64>,
    e_score: PyReadonlyArray1<f64>,
    e_sigma: PyReadonlyArray1<f64>,
    e_diffuse: PyReadonlyArray1<u8>,
    n_leds: i64,
    merge: f64,
) -> PyResult<HypArrays<'py>> {
    let cams = cams_from(&cam_r, &cam_t, &cam_k)?;
    let (led, seg, xy) = (e_led.as_array(), e_seg.as_array(), e_xy.as_array());
    let (score, sigma, diffuse) = (e_score.as_array(), e_sigma.as_array(), e_diffuse.as_array());
    let m = led.len();
    if seg.len() != m
        || xy.shape() != [m, 2]
        || score.len() != m
        || sigma.len() != m
        || diffuse.len() != m
    {
        return Err(PyValueError::new_err(
            "entry arrays must have the same length",
        ));
    }
    let n = n_leds.max(0) as usize;
    let mut per_led: Vec<Vec<Entry>> = vec![Vec::new(); n];
    for k in 0..m {
        let (l, s) = (led[k], seg[k]);
        if s < 0 || s as usize >= cams.len() {
            return Err(PyValueError::new_err("segment index out of range"));
        }
        if l >= 0 && (l as usize) < n {
            per_led[l as usize].push(Entry {
                idx: k,
                seg: s as usize,
                u: xy[[k, 0]],
                v: xy[[k, 1]],
                score: score[k],
                sg: sigma[k],
                diffuse: diffuse[k] != 0,
            });
        }
    }
    let results: Vec<Vec<Hyp>> = py.detach(|| {
        per_led
            .into_par_iter()
            .map(|e| led_hypotheses(e, &cams, merge))
            .collect()
    });
    let total: usize = results.iter().map(|r| r.len()).sum();
    let mut h_led = Vec::with_capacity(total);
    let mut h_x = Vec::with_capacity(3 * total);
    let mut h_stats = Vec::with_capacity(6 * total);
    let mut off = vec![0i64];
    let mut idx = Vec::new();
    for (l, hs) in results.iter().enumerate() {
        for h in hs {
            h_led.push(l as i64);
            h_x.extend_from_slice(&[h.x[0], h.x[1], h.x[2]]);
            h_stats.extend_from_slice(&[h.err_px, h.angle, h.sigma, h.score, h.nerr, h.diffuse]);
            idx.extend(h.inl.iter().map(|e| e.idx as i64));
            off.push(idx.len() as i64);
        }
    }
    Ok((
        h_led.into_pyarray(py),
        Array2::from_shape_vec((total, 3), h_x)
            .unwrap()
            .into_pyarray(py),
        Array2::from_shape_vec((total, 6), h_stats)
            .unwrap()
            .into_pyarray(py),
        off.into_pyarray(py),
        idx.into_pyarray(py),
    ))
}

// ---- fill.refine_points ------------------------------------------------------------------

/// Cost, diagonal 3x3 blocks, blocks between points k and k+1, gradient.
type BlockSystem = (f64, Vec<Matrix3<f64>>, Vec<Matrix3<f64>>, Vec<Vector3<f64>>);

struct PointsProblem<'a> {
    cams: &'a [Cam],
    obs_k: &'a [usize],
    obs: &'a [Ob],       // w unused (1)
    link_k: &'a [usize], // link joins point k and k + 1
    allowed: &'a [f64],
    scale: f64,
}

impl PointsProblem<'_> {
    fn cost(&self, p: &[Vector3<f64>]) -> f64 {
        let mut c = 0.0;
        for (o, &k) in self.obs.iter().zip(self.obs_k) {
            let ((pu, pv), _) = self.cams[o.cam].project_jac(&p[k]);
            c += (pu - o.u).powi(2) + (pv - o.v).powi(2);
        }
        for (&k, &al) in self.link_k.iter().zip(self.allowed) {
            let h = ((p[k + 1] - p[k]).norm() - al).max(0.0) * self.scale;
            c += h * h;
        }
        c
    }

    /// Gauss-Newton system: diagonal blocks, blocks between k and k+1, gradient.
    fn system(&self, p: &[Vector3<f64>]) -> BlockSystem {
        let n = p.len();
        let mut d = vec![Matrix3::zeros(); n];
        let mut o = vec![Matrix3::zeros(); n.saturating_sub(1)];
        let mut g = vec![Vector3::zeros(); n];
        let mut cost = 0.0;
        for (ob, &k) in self.obs.iter().zip(self.obs_k) {
            let ((pu, pv), j) = self.cams[ob.cam].project_jac(&p[k]);
            for (row, r) in j.iter().zip([pu - ob.u, pv - ob.v]) {
                let jr = Vector3::new(row[0], row[1], row[2]);
                d[k] += jr * jr.transpose();
                g[k] += jr * r;
                cost += r * r;
            }
        }
        for (&k, &al) in self.link_k.iter().zip(self.allowed) {
            let diff = p[k + 1] - p[k];
            let len = diff.norm();
            if len - al > 0.0 && len > 0.0 {
                let h = (len - al) * self.scale;
                let e = diff * (self.scale / len); // d h / d p[k+1]; d h / d p[k] = -e
                let ee = e * e.transpose();
                d[k] += ee;
                d[k + 1] += ee;
                o[k] -= ee;
                g[k] -= e * h;
                g[k + 1] += e * h;
                cost += h * h;
            }
        }
        (cost, d, o, g)
    }
}

/// Solve the block tridiagonal system (diag d, off-diagonal o between k and k+1) for rhs.
pub(crate) fn block_thomas(
    d: &[Matrix3<f64>],
    o: &[Matrix3<f64>],
    rhs: &[Vector3<f64>],
) -> Option<Vec<Vector3<f64>>> {
    let n = d.len();
    let mut cp: Vec<Matrix3<f64>> = Vec::with_capacity(n);
    let mut dp: Vec<Vector3<f64>> = Vec::with_capacity(n);
    for k in 0..n {
        let (m, r) = if k == 0 {
            (d[0], rhs[0])
        } else {
            let lo = o[k - 1].transpose();
            (d[k] - lo * cp[k - 1], rhs[k] - lo * dp[k - 1])
        };
        let mi = m.try_inverse()?;
        cp.push(if k + 1 < n {
            mi * o[k]
        } else {
            Matrix3::zeros()
        });
        dp.push(mi * r);
    }
    let mut x = vec![Vector3::zeros(); n];
    for k in (0..n).rev() {
        x[k] = if k + 1 < n {
            dp[k] - cp[k] * x[k + 1]
        } else {
            dp[k]
        };
    }
    Some(x)
}

fn refine_points_lm(pr: &PointsProblem, mut p: Vec<Vector3<f64>>) -> Vec<Vector3<f64>> {
    let n = p.len();
    if n == 0 {
        return p;
    }
    let (mut cost, mut d, mut o, mut g) = pr.system(&p);
    let mut lambda = 1e-3;
    for _ in 0..100 {
        let gmax = g.iter().map(|v| v.amax()).fold(0.0, f64::max);
        if gmax <= 1e-14 * (1.0 + cost) {
            break;
        }
        let da: Vec<Matrix3<f64>> = d
            .iter()
            .map(|m| {
                let mut a = *m;
                for k in 0..3 {
                    a[(k, k)] += lambda * m[(k, k)].max(1e-12);
                }
                a
            })
            .collect();
        let rhs: Vec<Vector3<f64>> = g.iter().map(|v| -v).collect();
        let Some(step) = block_thomas(&da, &o, &rhs) else {
            lambda *= 10.0;
            if lambda > 1e16 {
                break;
            }
            continue;
        };
        let pn: Vec<Vector3<f64>> = p.iter().zip(&step).map(|(a, s)| a + s).collect();
        let cn = pr.cost(&pn);
        if cn.is_finite() && cn <= cost {
            let snorm = step.iter().map(|s| s.norm_squared()).sum::<f64>().sqrt();
            let pnorm = p.iter().map(|s| s.norm_squared()).sum::<f64>().sqrt();
            let small = snorm <= 1e-13 * (pnorm + 1e-13) || cost - cn <= 1e-16 * cost;
            p = pn;
            (cost, d, o, g) = pr.system(&p);
            lambda = (lambda * 0.3).max(1e-12);
            if small {
                break;
            }
        } else {
            lambda *= 10.0;
            if lambda > 1e16 {
                break;
            }
        }
    }
    p
}

/// fill.refine_points: points (L,3) in increasing LED order; observations (point index,
/// camera index, pixel (M,2)); links from point k to k+1 with their allowed length; hinge
/// residual max(0, length - allowed) * scale. Cameras fixed. Returns the refined points.
#[pyfunction]
#[pyo3(signature = (cam_r, cam_t, cam_k, p0, obs_k, obs_cam, obs_uv, link_k, allowed, scale))]
#[allow(clippy::too_many_arguments)]
fn refine_points<'py>(
    py: Python<'py>,
    cam_r: PyReadonlyArray3<f64>,
    cam_t: PyReadonlyArray2<f64>,
    cam_k: PyReadonlyArray2<f64>,
    p0: PyReadonlyArray2<f64>,
    obs_k: PyReadonlyArray1<i64>,
    obs_cam: PyReadonlyArray1<i64>,
    obs_uv: PyReadonlyArray2<f64>,
    link_k: PyReadonlyArray1<i64>,
    allowed: PyReadonlyArray1<f64>,
    scale: f64,
) -> PyResult<Bound<'py, PyArray2<f64>>> {
    let cams = cams_from(&cam_r, &cam_t, &cam_k)?;
    let p0 = p0.as_array();
    let n = p0.shape()[0];
    if p0.shape() != [n, 3] {
        return Err(PyValueError::new_err("p0 must be (L,3)"));
    }
    let (ok, oc, ouv) = (obs_k.as_array(), obs_cam.as_array(), obs_uv.as_array());
    let m = ok.len();
    if oc.len() != m || ouv.shape() != [m, 2] {
        return Err(PyValueError::new_err(
            "observation arrays must have the same length",
        ));
    }
    let mut ks = Vec::with_capacity(m);
    let mut obs = Vec::with_capacity(m);
    for i in 0..m {
        if ok[i] < 0 || ok[i] as usize >= n || oc[i] < 0 || oc[i] as usize >= cams.len() {
            return Err(PyValueError::new_err("observation index out of range"));
        }
        ks.push(ok[i] as usize);
        obs.push(Ob {
            cam: oc[i] as usize,
            u: ouv[[i, 0]],
            v: ouv[[i, 1]],
            w: 1.0,
        });
    }
    let (lk, al) = (link_k.as_array(), allowed.as_array());
    if al.len() != lk.len() {
        return Err(PyValueError::new_err(
            "link arrays must have the same length",
        ));
    }
    let mut links = Vec::with_capacity(lk.len());
    for &k in lk.iter() {
        if k < 0 || k as usize + 1 >= n {
            return Err(PyValueError::new_err("link index out of range"));
        }
        links.push(k as usize);
    }
    let allowed: Vec<f64> = al.to_vec();
    let start: Vec<Vector3<f64>> = (0..n)
        .map(|k| Vector3::new(p0[[k, 0]], p0[[k, 1]], p0[[k, 2]]))
        .collect();
    let out = py.detach(|| {
        let pr = PointsProblem {
            cams: &cams,
            obs_k: &ks,
            obs: &obs,
            link_k: &links,
            allowed: &allowed,
            scale,
        };
        refine_points_lm(&pr, start)
    });
    let flat: Vec<f64> = out.iter().flat_map(|v| [v[0], v[1], v[2]]).collect();
    Ok(Array2::from_shape_vec((n, 3), flat)
        .unwrap()
        .into_pyarray(py))
}

pub fn register(m: &Bound<'_, PyModule>) -> PyResult<()> {
    m.add_function(wrap_pyfunction!(build_hypotheses, m)?)?;
    m.add_function(wrap_pyfunction!(refine_points, m)?)?;
    Ok(())
}
