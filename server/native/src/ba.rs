//! Bundle adjustment (reconstruction/sfm.py: bundle_adjust).
//!
//! The same problem as the Python version: the parameter vector is
//! `[log f0, k1, k2] [log focal deviation per segment] [rvec, t per free segment] [points]`,
//! with reprojection residuals per observation, a prior on each focal deviation, a penalty
//! keeping k2 small and, optionally, a residual fixing the scale (the distance between the
//! fixed camera and a reference camera).
//!
//! Minimised with Levenberg–Marquardt on the robust (Cauchy) cost, with an analytic
//! Jacobian. The normal equations are solved with the Schur complement on the points
//! (3x3 blocks), leaving a dense system over the camera parameters (Cholesky). The robust
//! loss is handled by iteratively reweighted least squares: the gradient is exact, the
//! Hessian is approximated by J^T diag(rho') J, so the minimum is the one scipy's
//! least_squares(loss="cauchy") finds.

use nalgebra::{DMatrix, DVector, Matrix3, Vector3};
use numpy::{PyArray1, PyReadonlyArray1, PyReadonlyArray2};
use pyo3::exceptions::PyValueError;
use pyo3::prelude::*;

const FTOL: f64 = 1e-8;
const XTOL: f64 = 1e-8;
const LAMBDA0: f64 = 1e-3;
const LAMBDA_MAX: f64 = 1e16;

fn skew(v: &Vector3<f64>) -> Matrix3<f64> {
    Matrix3::new(0.0, -v[2], v[1], v[2], 0.0, -v[0], -v[1], v[0], 0.0)
}

/// Rotation matrix of a rotation vector (same as geometry.rodrigues_batch).
fn rodrigues(w: &Vector3<f64>) -> Matrix3<f64> {
    let th = w.norm();
    if th <= 1e-12 {
        return Matrix3::identity();
    }
    let k = skew(&(w / th));
    Matrix3::identity() + k * th.sin() + (k * k) * (1.0 - th.cos())
}

/// dR/dw_i for i = 0..3 (Gallego & Yezzi, "A compact formula for the derivative of a
/// 3-D rotation in exponential coordinates").
fn rodrigues_deriv(w: &Vector3<f64>, r: &Matrix3<f64>) -> [Matrix3<f64>; 3] {
    let th2 = w.norm_squared();
    let mut out = [Matrix3::zeros(); 3];
    if th2 < 1e-16 {
        for (i, o) in out.iter_mut().enumerate() {
            let mut e = Vector3::zeros();
            e[i] = 1.0;
            *o = skew(&e);
        }
        return out;
    }
    let wx = skew(w);
    let i_r = Matrix3::identity() - r;
    for (i, o) in out.iter_mut().enumerate() {
        let mut e = Vector3::zeros();
        e[i] = 1.0;
        let v = w.cross(&(i_r * e));
        *o = ((wx * w[i] + skew(&v)) / th2) * r;
    }
    out
}

struct Problem<'a> {
    obs_s: Vec<usize>,
    obs_p: Vec<usize>,
    obs_uv: &'a [f64],
    size: &'a [f64],
    cx: &'a [f64],
    cy: &'a [f64],
    free_idx: Vec<i64>,
    r_fix: Matrix3<f64>,
    t_fix: Vector3<f64>,
    nseg: usize,
    npts: usize,
    fix_f0: bool,
    logf0: f64,
    focal_prior: f64,
    scale_ref: Option<usize>,
    scale0: f64,
    f_scale: f64,
    o_fd: usize,
    o_pose: usize,
    o_pts: usize,
    by_point: Vec<Vec<usize>>,
}

/// Per-segment rotation, translation and (for free segments) dR/dw.
struct Poses {
    r: Vec<Matrix3<f64>>,
    t: Vec<Vector3<f64>>,
    dr: Vec<Option<[Matrix3<f64>; 3]>>,
}

impl<'a> Problem<'a> {
    fn n_res(&self) -> usize {
        2 * self.obs_s.len() + self.nseg + 1 + usize::from(self.scale_ref.is_some())
    }

    fn poses(&self, x: &[f64], with_deriv: bool) -> Poses {
        let mut r = Vec::with_capacity(self.nseg);
        let mut t = Vec::with_capacity(self.nseg);
        let mut dr = Vec::with_capacity(self.nseg);
        for s in 0..self.nseg {
            let fi = self.free_idx[s];
            if fi < 0 {
                r.push(self.r_fix);
                t.push(self.t_fix);
                dr.push(None);
            } else {
                let o = self.o_pose + 6 * fi as usize;
                let w = Vector3::new(x[o], x[o + 1], x[o + 2]);
                let rm = rodrigues(&w);
                dr.push(if with_deriv {
                    Some(rodrigues_deriv(&w, &rm))
                } else {
                    None
                });
                r.push(rm);
                t.push(Vector3::new(x[o + 3], x[o + 4], x[o + 5]));
            }
        }
        Poses { r, t, dr }
    }

    fn focal(&self, x: &[f64], s: usize) -> f64 {
        let l0 = if self.fix_f0 { self.logf0 } else { x[0] };
        (l0 + x[self.o_fd + s]).exp() * self.size[s]
    }

    fn point(&self, x: &[f64], p: usize) -> Vector3<f64> {
        let o = self.o_pts + 3 * p;
        Vector3::new(x[o], x[o + 1], x[o + 2])
    }

    /// Distance between the scale reference camera's centre and the fixed camera's.
    fn scale_residual(&self, ps: &Poses) -> Option<(f64, Vector3<f64>, Vector3<f64>)> {
        let s = self.scale_ref?;
        let c = -(ps.r[s].transpose() * ps.t[s]);
        let cf = -(self.r_fix.transpose() * self.t_fix);
        let d = c - cf;
        let n = d.norm();
        Some((
            (n - self.scale0) / self.scale0 * 100.0,
            c,
            d / n.max(1e-300),
        ))
    }

    /// All residuals, in the order of sfm.residuals().
    fn residuals(&self, x: &[f64]) -> Vec<f64> {
        let ps = self.poses(x, false);
        let mut out = Vec::with_capacity(self.n_res());
        let (k1, k2) = (x[1], x[2]);
        for (i, (&s, &p)) in self.obs_s.iter().zip(&self.obs_p).enumerate() {
            let xc = ps.r[s] * self.point(x, p) + ps.t[s];
            let zs = if xc[2].abs() < 1e-9 { 1e-9 } else { xc[2] };
            let (u, v) = (xc[0] / zs, xc[1] / zs);
            let r2 = u * u + v * v;
            let d = 1.0 + k1 * r2 + k2 * r2 * r2;
            let f = self.focal(x, s);
            out.push(f * u * d + self.cx[s] - self.obs_uv[2 * i]);
            out.push(f * v * d + self.cy[s] - self.obs_uv[2 * i + 1]);
        }
        for s in 0..self.nseg {
            out.push(x[self.o_fd + s] / self.focal_prior);
        }
        out.push(x[2] * 10.0);
        if let Some((g, _, _)) = self.scale_residual(&ps) {
            out.push(g);
        }
        out
    }

    fn cost(&self, r: &[f64]) -> f64 {
        let fs2 = self.f_scale * self.f_scale;
        0.5 * fs2 * r.iter().map(|&v| (v * v / fs2).ln_1p()).sum::<f64>()
    }

    /// rho'(z) of the Cauchy loss for residual v.
    fn weight(&self, v: f64) -> f64 {
        1.0 / (1.0 + v * v / (self.f_scale * self.f_scale))
    }
}

/// The linearised problem at one point: everything needed to solve the damped normal
/// equations for any damping.
struct Linear {
    hcc: DMatrix<f64>,
    gc: DVector<f64>,
    v: Vec<Matrix3<f64>>,
    gp: Vec<Vector3<f64>>,
    /// Per point: the camera-parameter rows of W = J_c^T W J_p that are not zero.
    w_rows: Vec<Vec<(usize, Vector3<f64>)>>,
    active: Vec<bool>,
}

fn linearise(pb: &Problem, x: &[f64], res: &[f64]) -> Linear {
    let nc = pb.o_pts;
    let ps = pb.poses(x, true);
    let (k1, k2) = (x[1], x[2]);
    let mut hcc = DMatrix::<f64>::zeros(nc, nc);
    let mut gc = DVector::<f64>::zeros(nc);
    let mut v = vec![Matrix3::<f64>::zeros(); pb.npts];
    let mut gp = vec![Vector3::<f64>::zeros(); pb.npts];
    // Per observation: camera columns and their 2 Jacobian rows, point Jacobian (2x3).
    let nobs = pb.obs_s.len();
    let mut cols: Vec<[usize; 10]> = Vec::with_capacity(nobs);
    let mut ncols: Vec<usize> = Vec::with_capacity(nobs);
    let mut jc: Vec<[[f64; 10]; 2]> = Vec::with_capacity(nobs);
    let mut jp: Vec<[[f64; 3]; 2]> = Vec::with_capacity(nobs);
    let mut wts: Vec<[f64; 2]> = Vec::with_capacity(nobs);
    for i in 0..nobs {
        let s = pb.obs_s[i];
        let p = pb.obs_p[i];
        let xw = pb.point(x, p);
        let xc = ps.r[s] * xw + ps.t[s];
        let clamped = xc[2].abs() < 1e-9;
        let zs = if clamped { 1e-9 } else { xc[2] };
        let (u, vv) = (xc[0] / zs, xc[1] / zs);
        let r2 = u * u + vv * vv;
        let dd = k1 + 2.0 * k2 * r2; // d(distortion)/d(r2)
        let d = 1.0 + k1 * r2 + k2 * r2 * r2;
        let f = pb.focal(x, s);
        // d(pixel)/d(u, v)
        let a00 = f * (d + 2.0 * u * u * dd);
        let a01 = f * (2.0 * u * vv * dd);
        let a10 = a01;
        let a11 = f * (d + 2.0 * vv * vv * dd);
        // d(u, v)/d(Xc)
        let inv = 1.0 / zs;
        let (bu2, bv2) = if clamped {
            (0.0, 0.0)
        } else {
            (-xc[0] * inv * inv, -xc[1] * inv * inv)
        };
        // d(pixel)/d(Xc): 2x3
        let dxc = [
            [a00 * inv, a01 * inv, a00 * bu2 + a01 * bv2],
            [a10 * inv, a11 * inv, a10 * bu2 + a11 * bv2],
        ];
        let mut c = [0usize; 10];
        let mut jrow = [[0.0f64; 10]; 2];
        let mut n = 0;
        let du_df = [u * d, vv * d];
        if !pb.fix_f0 {
            c[n] = 0;
            jrow[0][n] = f * du_df[0];
            jrow[1][n] = f * du_df[1];
            n += 1;
        }
        c[n] = 1;
        jrow[0][n] = f * u * r2;
        jrow[1][n] = f * vv * r2;
        n += 1;
        c[n] = 2;
        jrow[0][n] = f * u * r2 * r2;
        jrow[1][n] = f * vv * r2 * r2;
        n += 1;
        c[n] = pb.o_fd + s;
        jrow[0][n] = f * du_df[0];
        jrow[1][n] = f * du_df[1];
        n += 1;
        if let Some(drs) = &ps.dr[s] {
            let o = pb.o_pose + 6 * pb.free_idx[s] as usize;
            for k in 0..3 {
                let dx = drs[k] * xw;
                c[n] = o + k;
                for row in 0..2 {
                    jrow[row][n] = dxc[row][0] * dx[0] + dxc[row][1] * dx[1] + dxc[row][2] * dx[2];
                }
                n += 1;
            }
            for k in 0..3 {
                c[n] = o + 3 + k;
                for row in 0..2 {
                    jrow[row][n] = dxc[row][k];
                }
                n += 1;
            }
        }
        let rm = &ps.r[s];
        let mut jpp = [[0.0f64; 3]; 2];
        for row in 0..2 {
            for k in 0..3 {
                jpp[row][k] =
                    dxc[row][0] * rm[(0, k)] + dxc[row][1] * rm[(1, k)] + dxc[row][2] * rm[(2, k)];
            }
        }
        let w = [pb.weight(res[2 * i]), pb.weight(res[2 * i + 1])];
        // Camera block and gradient.
        for row in 0..2 {
            let wr = w[row] * res[2 * i + row];
            for a in 0..n {
                gc[c[a]] += jrow[row][a] * wr;
                let wa = w[row] * jrow[row][a];
                for b in 0..n {
                    hcc[(c[a], c[b])] += wa * jrow[row][b];
                }
            }
            for a in 0..3 {
                gp[p][a] += jpp[row][a] * wr;
                for b in 0..3 {
                    v[p][(a, b)] += w[row] * jpp[row][a] * jpp[row][b];
                }
            }
        }
        cols.push(c);
        ncols.push(n);
        jc.push(jrow);
        jp.push(jpp);
        wts.push(w);
    }
    // Priors on the focal deviations, k2.
    let m = 2 * nobs;
    for s in 0..pb.nseg {
        let col = pb.o_fd + s;
        let j = 1.0 / pb.focal_prior;
        let w = pb.weight(res[m + s]);
        gc[col] += j * w * res[m + s];
        hcc[(col, col)] += w * j * j;
    }
    {
        let w = pb.weight(res[m + pb.nseg]);
        gc[2] += 10.0 * w * res[m + pb.nseg];
        hcc[(2, 2)] += w * 100.0;
    }
    // Scale: residual on the reference camera's centre C = -R^T t.
    if let (Some(s), Some((g, _c, dir))) = (pb.scale_ref, pb.scale_residual(&ps)) {
        if let Some(drs) = &ps.dr[s] {
            let o = pb.o_pose + 6 * pb.free_idx[s] as usize;
            let k = 100.0 / pb.scale0;
            let mut j = [0.0f64; 6];
            for a in 0..3 {
                let dc = -(drs[a].transpose() * ps.t[s]);
                j[a] = k * dir.dot(&dc);
            }
            let dct = -(ps.r[s].transpose()); // dC/dt
            for a in 0..3 {
                j[3 + a] = k * (dir[0] * dct[(0, a)] + dir[1] * dct[(1, a)] + dir[2] * dct[(2, a)]);
            }
            let w = pb.weight(g);
            for a in 0..6 {
                gc[o + a] += j[a] * w * g;
                for b in 0..6 {
                    hcc[(o + a, o + b)] += w * j[a] * j[b];
                }
            }
        }
    }
    // W rows per point.
    let mut w_rows: Vec<Vec<(usize, Vector3<f64>)>> = Vec::with_capacity(pb.npts);
    let mut acc = vec![Vector3::<f64>::zeros(); nc];
    let mut touched = vec![false; nc];
    for p in 0..pb.npts {
        let mut list: Vec<usize> = Vec::new();
        for &i in &pb.by_point[p] {
            for a in 0..ncols[i] {
                let col = cols[i][a];
                if !touched[col] {
                    touched[col] = true;
                    list.push(col);
                }
                for row in 0..2 {
                    let ww = wts[i][row] * jc[i][row][a];
                    for b in 0..3 {
                        acc[col][b] += ww * jp[i][row][b];
                    }
                }
            }
        }
        list.sort_unstable();
        let rows = list
            .iter()
            .map(|&col| {
                let val = acc[col];
                acc[col] = Vector3::zeros();
                touched[col] = false;
                (col, val)
            })
            .collect();
        w_rows.push(rows);
    }
    let mut active = vec![true; nc];
    if pb.fix_f0 {
        active[0] = false;
    }
    Linear {
        hcc,
        gc,
        v,
        gp,
        w_rows,
        active,
    }
}

/// Solve (H + lambda * diag(H)) delta = -g. None if the system isn't positive definite.
fn solve_step(pb: &Problem, lin: &Linear, lambda: f64) -> Option<Vec<f64>> {
    let nc = pb.o_pts;
    let mut s = lin.hcc.clone();
    let mut rhs = -lin.gc.clone();
    for c in 0..nc {
        if !lin.active[c] {
            for k in 0..nc {
                s[(c, k)] = 0.0;
                s[(k, c)] = 0.0;
            }
            s[(c, c)] = 1.0;
            rhs[c] = 0.0;
            continue;
        }
        let dgn = lin.hcc[(c, c)].max(1e-12);
        s[(c, c)] += lambda * dgn;
    }
    let mut vinv = Vec::with_capacity(pb.npts);
    for p in 0..pb.npts {
        let mut vd = lin.v[p];
        for a in 0..3 {
            vd[(a, a)] += lambda * vd[(a, a)].max(1e-12);
        }
        let vi = vd.try_inverse()?;
        // Y = W V^-1 (rows), S -= Y W^T, rhs -= Y (-g_p)
        let rows = &lin.w_rows[p];
        let y: Vec<Vector3<f64>> = rows.iter().map(|(_, w)| vi * w).collect(); // V is symmetric
        let gp = -lin.gp[p];
        // Rows are sorted by column, so rows[..=a] are the entries on or below the diagonal:
        // only the lower triangle is accumulated, and mirrored below.
        for (a, (ca, _)) in rows.iter().enumerate() {
            if !lin.active[*ca] {
                continue;
            }
            rhs[*ca] -= y[a].dot(&gp);
            let ya = y[a];
            for (cb, wb) in rows[..=a].iter() {
                if lin.active[*cb] {
                    s[(*ca, *cb)] -= ya.dot(wb);
                }
            }
        }
        vinv.push(vi);
    }
    for j in 0..nc {
        for i in (j + 1)..nc {
            s[(j, i)] = s[(i, j)];
        }
    }
    let chol = s.cholesky()?;
    let dc = chol.solve(&rhs);
    let mut delta = vec![0.0; pb.o_pts + 3 * pb.npts];
    for c in 0..nc {
        delta[c] = if lin.active[c] { dc[c] } else { 0.0 };
    }
    for p in 0..pb.npts {
        // V dp = -g_p - W^T dc
        let mut b = -lin.gp[p];
        for (c, w) in &lin.w_rows[p] {
            b -= w * delta[*c];
        }
        let dp = vinv[p] * b;
        let o = pb.o_pts + 3 * p;
        delta[o] = dp[0];
        delta[o + 1] = dp[1];
        delta[o + 2] = dp[2];
    }
    if delta.iter().all(|v| v.is_finite()) {
        Some(delta)
    } else {
        None
    }
}

fn minimise(pb: &Problem, x0: Vec<f64>, max_nfev: usize) -> Vec<f64> {
    let mut x = x0;
    let mut res = pb.residuals(&x);
    let mut cost = pb.cost(&res);
    let mut nfev = 1;
    let mut lambda = LAMBDA0;
    'outer: while nfev < max_nfev {
        let lin = linearise(pb, &x, &res);
        loop {
            if nfev >= max_nfev || lambda > LAMBDA_MAX {
                break 'outer;
            }
            let Some(delta) = solve_step(pb, &lin, lambda) else {
                lambda *= 10.0;
                continue;
            };
            let xn: Vec<f64> = x.iter().zip(&delta).map(|(a, b)| a + b).collect();
            let rn = pb.residuals(&xn);
            nfev += 1;
            let cn = pb.cost(&rn);
            if cn.is_finite() && cn < cost {
                let dnorm = delta.iter().map(|v| v * v).sum::<f64>().sqrt();
                let xnorm = xn.iter().map(|v| v * v).sum::<f64>().sqrt();
                let df = cost - cn;
                x = xn;
                res = rn;
                let converged = df < FTOL * cost || dnorm < XTOL * (XTOL + xnorm);
                cost = cn;
                lambda = (lambda / 10.0).max(1e-12);
                if converged {
                    break 'outer;
                }
                continue 'outer;
            }
            lambda *= 10.0;
        }
    }
    x
}

/// Minimise the bundle-adjustment cost of sfm.bundle_adjust from x0; returns the solution.
///
/// obs_s/obs_p: segment and point of each observation; obs_uv (m, 2): observed pixels;
/// size/cx/cy per segment; free_idx per segment (-1 for the fixed one); r_fix/t_fix: the
/// fixed segment's pose; scale_ref: segment whose distance to the fixed one is held at
/// scale0 (-1: none).
#[pyfunction]
#[allow(clippy::too_many_arguments)]
#[pyo3(signature = (x0, obs_s, obs_p, obs_uv, size, cx, cy, free_idx, r_fix, t_fix, npts, fix_f0, logf0, focal_prior, scale_ref, scale0, max_nfev, f_scale))]
fn ba_minimize<'py>(
    py: Python<'py>,
    x0: PyReadonlyArray1<'py, f64>,
    obs_s: PyReadonlyArray1<'py, i64>,
    obs_p: PyReadonlyArray1<'py, i64>,
    obs_uv: PyReadonlyArray2<'py, f64>,
    size: PyReadonlyArray1<'py, f64>,
    cx: PyReadonlyArray1<'py, f64>,
    cy: PyReadonlyArray1<'py, f64>,
    free_idx: PyReadonlyArray1<'py, i64>,
    r_fix: PyReadonlyArray2<'py, f64>,
    t_fix: PyReadonlyArray1<'py, f64>,
    npts: usize,
    fix_f0: bool,
    logf0: f64,
    focal_prior: f64,
    scale_ref: i64,
    scale0: f64,
    max_nfev: usize,
    f_scale: f64,
) -> PyResult<Bound<'py, PyArray1<f64>>> {
    let x0v = x0.as_slice()?.to_vec();
    let obs_s: Vec<usize> = obs_s.as_slice()?.iter().map(|&v| v as usize).collect();
    let obs_p: Vec<usize> = obs_p.as_slice()?.iter().map(|&v| v as usize).collect();
    let uv = obs_uv.as_slice()?;
    let free_idx = free_idx.as_slice()?.to_vec();
    let nseg = free_idx.len();
    let nfree = free_idx.iter().filter(|&&v| v >= 0).count();
    let rf = r_fix.as_array();
    let tf = t_fix.as_slice()?;
    let size = size.as_slice()?;
    let cx = cx.as_slice()?;
    let cy = cy.as_slice()?;
    let o_fd = 3;
    let o_pose = o_fd + nseg;
    let o_pts = o_pose + 6 * nfree;
    if x0v.len() != o_pts + 3 * npts || uv.len() != 2 * obs_s.len() || obs_p.len() != obs_s.len() {
        return Err(PyValueError::new_err(
            "inconsistent bundle-adjustment inputs",
        ));
    }
    if size.len() != nseg
        || cx.len() != nseg
        || cy.len() != nseg
        || rf.shape() != [3, 3]
        || tf.len() != 3
    {
        return Err(PyValueError::new_err("inconsistent per-segment inputs"));
    }
    if obs_s.iter().any(|&s| s >= nseg) || obs_p.iter().any(|&p| p >= npts) {
        return Err(PyValueError::new_err("observation index out of range"));
    }
    let mut by_point = vec![Vec::new(); npts];
    for (i, &p) in obs_p.iter().enumerate() {
        by_point[p].push(i);
    }
    let scale_ref = if scale_ref >= 0 {
        Some(scale_ref as usize)
    } else {
        None
    };
    if scale_ref.is_some_and(|s| s >= nseg) {
        return Err(PyValueError::new_err("scale_ref out of range"));
    }
    let pb = Problem {
        obs_s,
        obs_p,
        obs_uv: uv,
        size,
        cx,
        cy,
        free_idx,
        r_fix: Matrix3::from_fn(|i, j| rf[[i, j]]),
        t_fix: Vector3::new(tf[0], tf[1], tf[2]),
        nseg,
        npts,
        fix_f0,
        logf0,
        focal_prior,
        scale_ref,
        scale0,
        f_scale,
        o_fd,
        o_pose,
        o_pts,
        by_point,
    };
    let x = py.detach(|| minimise(&pb, x0v, max_nfev));
    Ok(PyArray1::from_vec(py, x))
}

/// Residuals and the analytic Jacobian (dense) at x, for testing the derivatives.
#[pyfunction]
#[allow(clippy::too_many_arguments)]
#[pyo3(signature = (x, obs_s, obs_p, obs_uv, size, cx, cy, free_idx, r_fix, t_fix, npts, fix_f0, logf0, focal_prior, scale_ref, scale0))]
fn ba_residuals<'py>(
    py: Python<'py>,
    x: PyReadonlyArray1<'py, f64>,
    obs_s: PyReadonlyArray1<'py, i64>,
    obs_p: PyReadonlyArray1<'py, i64>,
    obs_uv: PyReadonlyArray2<'py, f64>,
    size: PyReadonlyArray1<'py, f64>,
    cx: PyReadonlyArray1<'py, f64>,
    cy: PyReadonlyArray1<'py, f64>,
    free_idx: PyReadonlyArray1<'py, i64>,
    r_fix: PyReadonlyArray2<'py, f64>,
    t_fix: PyReadonlyArray1<'py, f64>,
    npts: usize,
    fix_f0: bool,
    logf0: f64,
    focal_prior: f64,
    scale_ref: i64,
    scale0: f64,
) -> PyResult<(Bound<'py, PyArray1<f64>>, Bound<'py, PyArray1<f64>>)> {
    // Gradient of the robust cost (exact) and residuals: enough to check the Jacobian
    // against finite differences of the cost in tests.
    let xv = x.as_slice()?.to_vec();
    let obs_s: Vec<usize> = obs_s.as_slice()?.iter().map(|&v| v as usize).collect();
    let obs_p: Vec<usize> = obs_p.as_slice()?.iter().map(|&v| v as usize).collect();
    let free_idx = free_idx.as_slice()?.to_vec();
    let nseg = free_idx.len();
    let nfree = free_idx.iter().filter(|&&v| v >= 0).count();
    let rf = r_fix.as_array();
    let tf = t_fix.as_slice()?;
    let mut by_point = vec![Vec::new(); npts];
    for (i, &p) in obs_p.iter().enumerate() {
        by_point[p].push(i);
    }
    let pb = Problem {
        obs_s,
        obs_p,
        obs_uv: obs_uv.as_slice()?,
        size: size.as_slice()?,
        cx: cx.as_slice()?,
        cy: cy.as_slice()?,
        free_idx,
        r_fix: Matrix3::from_fn(|i, j| rf[[i, j]]),
        t_fix: Vector3::new(tf[0], tf[1], tf[2]),
        nseg,
        npts,
        fix_f0,
        logf0,
        focal_prior,
        scale_ref: if scale_ref >= 0 {
            Some(scale_ref as usize)
        } else {
            None
        },
        scale0,
        f_scale: 2.0,
        o_fd: 3,
        o_pose: 3 + nseg,
        o_pts: 3 + nseg + 6 * nfree,
        by_point,
    };
    let res = pb.residuals(&xv);
    let lin = linearise(&pb, &xv, &res);
    let mut g = vec![0.0; xv.len()];
    for c in 0..pb.o_pts {
        g[c] = lin.gc[c];
    }
    for p in 0..npts {
        for a in 0..3 {
            g[pb.o_pts + 3 * p + a] = lin.gp[p][a];
        }
    }
    Ok((PyArray1::from_vec(py, res), PyArray1::from_vec(py, g)))
}

pub fn register(m: &Bound<'_, PyModule>) -> PyResult<()> {
    m.add_function(wrap_pyfunction!(ba_minimize, m)?)?;
    m.add_function(wrap_pyfunction!(ba_residuals, m)?)?;
    Ok(())
}
