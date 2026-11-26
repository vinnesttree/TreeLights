//! Placing the LEDs of a gap between measured ones (reconstruction/fill.py: solve_gap).
//!
//! Residuals, as in the Python: a hinge on every link of the chain [a, p0..pm-1, (b)] longer
//! than its bound, the distance of each single-view LED from its ray, and a weak pull of every
//! point towards the path around the tree. Each residual involves at most two consecutive
//! points, so the normal equations are block tridiagonal and each Levenberg-Marquardt step is
//! solved exactly. The problem is convex (squared hinges of distances, linear ray and path
//! terms, the path term strictly convex), so this reaches the same minimum as scipy's dense
//! finite-difference solve, to the solvers' tolerances.

use nalgebra::{Matrix3, Vector3};
use numpy::ndarray::Array2;
use numpy::{IntoPyArray, PyArray2, PyReadonlyArray1, PyReadonlyArray2};
use pyo3::exceptions::PyValueError;
use pyo3::prelude::*;

use crate::hyp::block_thomas;

const MAX_ITERS: usize = 5000;

struct Gap {
    a: Vector3<f64>,
    b: Option<Vector3<f64>>,
    links: Vec<f64>, // links[k] bounds chain[k] -> chain[k + 1]
    path: Vec<Vector3<f64>>,
    rays: Vec<(usize, Vector3<f64>, Matrix3<f64>)>, // point, ray origin, I - d d^T
    s_link: f64,
    s_ray: f64,
    s_path: f64,
}

/// Cost, diagonal blocks, blocks between k and k+1, gradient (of half the cost's Gauss-Newton form).
type System = (f64, Vec<Matrix3<f64>>, Vec<Matrix3<f64>>, Vec<Vector3<f64>>);

impl Gap {
    /// The link from chain point k to k+1, as (index of the first free point or None,
    /// index of the second or None, their positions).
    fn link_ends(
        &self,
        p: &[Vector3<f64>],
        k: usize,
    ) -> (Option<usize>, Option<usize>, Vector3<f64>, Vector3<f64>) {
        let m = p.len();
        let at = |c: usize| -> (Option<usize>, Vector3<f64>) {
            if c == 0 {
                (None, self.a)
            } else if c <= m {
                (Some(c - 1), p[c - 1])
            } else {
                (None, self.b.expect("link past the last point needs b"))
            }
        };
        let (i, pi) = at(k);
        let (j, pj) = at(k + 1);
        (i, j, pi, pj)
    }

    fn cost(&self, p: &[Vector3<f64>]) -> f64 {
        let mut c = 0.0;
        for (k, &l) in self.links.iter().enumerate() {
            let (_, _, pi, pj) = self.link_ends(p, k);
            let h = ((pj - pi).norm() - l).max(0.0) * self.s_link;
            c += h * h;
        }
        for (k, c0, proj) in &self.rays {
            let r = proj * (p[*k] - c0) * self.s_ray;
            c += r.norm_squared();
        }
        for (q, t) in p.iter().zip(&self.path) {
            c += ((q - t) * self.s_path).norm_squared();
        }
        c
    }

    fn system(&self, p: &[Vector3<f64>]) -> System {
        let m = p.len();
        let mut d = vec![Matrix3::zeros(); m];
        let mut o = vec![Matrix3::zeros(); m.saturating_sub(1)];
        let mut g = vec![Vector3::zeros(); m];
        let mut cost = 0.0;
        for (k, &l) in self.links.iter().enumerate() {
            let (i, j, pi, pj) = self.link_ends(p, k);
            let diff = pj - pi;
            let len = diff.norm();
            if len - l > 0.0 && len > 0.0 {
                let h = (len - l) * self.s_link;
                let e = diff * (self.s_link / len); // dh/dpj; dh/dpi = -e
                let ee = e * e.transpose();
                cost += h * h;
                if let Some(i) = i {
                    d[i] += ee;
                    g[i] -= e * h;
                }
                if let Some(j) = j {
                    d[j] += ee;
                    g[j] += e * h;
                }
                if let (Some(i), Some(_)) = (i, j) {
                    o[i] -= ee; // j == i + 1
                }
            }
        }
        for (k, c0, proj) in &self.rays {
            let jm = proj * self.s_ray; // symmetric
            let r = jm * (p[*k] - c0);
            cost += r.norm_squared();
            d[*k] += jm.transpose() * jm;
            g[*k] += jm.transpose() * r;
        }
        let sp2 = self.s_path * self.s_path;
        for (k, (q, t)) in p.iter().zip(&self.path).enumerate() {
            let r = (q - t) * self.s_path;
            cost += r.norm_squared();
            d[k] += Matrix3::identity() * sp2;
            g[k] += r * self.s_path;
        }
        (cost, d, o, g)
    }

    fn solve(&self, mut p: Vec<Vector3<f64>>) -> Vec<Vector3<f64>> {
        if p.is_empty() {
            return p;
        }
        let (mut cost, mut d, mut o, mut g) = self.system(&p);
        let mut lambda = 1e-3;
        // Many cheap iterations (each is O(m)): links switching between slack and taut make
        // convergence slow near the minimum, and scipy's 200 evaluations often stop short of it.
        for _ in 0..MAX_ITERS {
            let mean_diag = d.iter().map(|mm| mm.trace()).sum::<f64>() / (3 * d.len()) as f64;
            let gmax = g.iter().map(|v| v.amax()).fold(0.0, f64::max);
            if gmax <= 1e-10 * (1.0 + cost) {
                break;
            }
            // Levenberg damping, the same for every point: Marquardt's per-point scaling leaves
            // points whose links are slack (curvature only from the weak path term) almost
            // undamped, and the hinge then makes steps overshoot over and over.
            let da: Vec<Matrix3<f64>> = d
                .iter()
                .map(|mm| mm + Matrix3::identity() * (lambda * mean_diag))
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
            let cn = self.cost(&pn);
            if cn.is_finite() && cn <= cost {
                // A tiny step only means convergence when it isn't the damping that made it tiny.
                let snorm = step.iter().map(|s| s.norm_squared()).sum::<f64>().sqrt();
                let pnorm = p.iter().map(|s| s.norm_squared()).sum::<f64>().sqrt();
                let small = lambda < 1e-6
                    && (snorm <= 1e-13 * (pnorm + 1e-13) || cost - cn <= 1e-16 * cost);
                p = pn;
                (cost, d, o, g) = self.system(&p);
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
}

fn vec3(a: &numpy::ndarray::ArrayView1<f64>) -> PyResult<Vector3<f64>> {
    if a.len() != 3 {
        return Err(PyValueError::new_err("expected a 3-vector"));
    }
    Ok(Vector3::new(a[0], a[1], a[2]))
}

fn rows3(a: &numpy::ndarray::ArrayView2<f64>, what: &str) -> PyResult<Vec<Vector3<f64>>> {
    if a.shape()[1] != 3 {
        return Err(PyValueError::new_err(format!("{what} must be (k,3)")));
    }
    Ok((0..a.shape()[0])
        .map(|k| Vector3::new(a[[k, 0]], a[[k, 1]], a[[k, 2]]))
        .collect())
}

/// fill.solve_gap: a (3,), b (3,) or None, links (m+1 with b, m without), path (m,3), the
/// start x0 (m,3), single-view rays (point index, origin (r,3), unit direction (r,3)), and the
/// scale. Returns the placed points (m,3).
#[pyfunction]
#[pyo3(signature = (a, b, links, path, x0, ray_k, ray_c, ray_d, scale_ref))]
#[allow(clippy::too_many_arguments)]
fn solve_gap<'py>(
    py: Python<'py>,
    a: PyReadonlyArray1<f64>,
    b: Option<PyReadonlyArray1<f64>>,
    links: PyReadonlyArray1<f64>,
    path: PyReadonlyArray2<f64>,
    x0: PyReadonlyArray2<f64>,
    ray_k: PyReadonlyArray1<i64>,
    ray_c: PyReadonlyArray2<f64>,
    ray_d: PyReadonlyArray2<f64>,
    scale_ref: f64,
) -> PyResult<Bound<'py, PyArray2<f64>>> {
    let path = rows3(&path.as_array(), "path")?;
    let x0 = rows3(&x0.as_array(), "x0")?;
    let m = path.len();
    if x0.len() != m {
        return Err(PyValueError::new_err(
            "x0 and path must have the same length",
        ));
    }
    let b = match b {
        Some(v) => Some(vec3(&v.as_array())?),
        None => None,
    };
    let links: Vec<f64> = links.as_array().to_vec();
    if links.len() != m + usize::from(b.is_some()) {
        return Err(PyValueError::new_err(
            "links must have one entry per link of the chain",
        ));
    }
    let (rk, rc, rd) = (
        ray_k.as_array(),
        rows3(&ray_c.as_array(), "ray_c")?,
        rows3(&ray_d.as_array(), "ray_d")?,
    );
    if rc.len() != rk.len() || rd.len() != rk.len() {
        return Err(PyValueError::new_err(
            "ray arrays must have the same length",
        ));
    }
    let mut rays = Vec::with_capacity(rk.len());
    for ((&k, c), dv) in rk.iter().zip(rc).zip(rd) {
        if k < 0 || k as usize >= m {
            return Err(PyValueError::new_err("ray index out of range"));
        }
        rays.push((k as usize, c, Matrix3::identity() - dv * dv.transpose()));
    }
    let gap = Gap {
        a: vec3(&a.as_array())?,
        b,
        links,
        path,
        rays,
        s_link: 1.0 / (0.01 * scale_ref),
        s_ray: 1.0 / (0.005 * scale_ref),
        s_path: 1.0 / (0.3 * scale_ref),
    };
    let out = py.detach(|| gap.solve(x0));
    let flat: Vec<f64> = out.iter().flat_map(|v| [v[0], v[1], v[2]]).collect();
    Ok(Array2::from_shape_vec((m, 3), flat)
        .unwrap()
        .into_pyarray(py))
}

pub fn register(m: &Bound<'_, PyModule>) -> PyResult<()> {
    m.add_function(wrap_pyfunction!(solve_gap, m)?)?;
    Ok(())
}
