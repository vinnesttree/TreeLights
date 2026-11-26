//! Following LED blobs from frame to frame (walk/tracking.py: track).
//!
//! A faithful port of the Python `track` loop without the experimental local motion
//! field: the same prediction, global shift vote, one-to-one matching, coasting, lost
//! and re-acquired tracks, in the same order, so it gives the same tracks. The
//! homographies from the gyro are computed in Python (exactly as there) and passed in.
//!
//! Neighbour searches use a uniform grid instead of a k-d tree. The results are the
//! same sets of pairs; the only difference is the order in which the shift vote sums
//! the displacements near its peak, which can change the mean in the last bits.

use numpy::ndarray::Array2;
use numpy::{PyArray2, PyReadonlyArray1, PyReadonlyArray2, PyReadonlyArray3};
use pyo3::prelude::*;

type P2 = [f64; 2];

/// Tracking parameters, read from walk/tracking.py's module constants at each call.
#[derive(Clone, Copy)]
struct Params {
    gate_px: f64,
    vote_gate_gyro: f64,
    vote_gate_nogyro: f64,
    max_coast_s: f64,
    max_deviation_px: f64,
    lock_frames: i64,
    shift_alpha: f64,
    max_lost_s: f64,
    reacquire_px_per_s: f64,
    reacquire_max_px: f64,
    vel_alpha: f64,
}

/// Points binned in square cells, for "all points within r of q" queries.
struct Grid {
    minx: f64,
    miny: f64,
    cell: f64,
    nx: i64,
    ny: i64,
    start: Vec<u32>, // CSR: points of cell c are idx[start[c]..start[c+1]]
    idx: Vec<u32>,
}

impl Grid {
    fn new(pts: &[P2], r: f64) -> Grid {
        let (mut minx, mut miny, mut maxx, mut maxy) = (
            f64::INFINITY,
            f64::INFINITY,
            f64::NEG_INFINITY,
            f64::NEG_INFINITY,
        );
        for p in pts {
            minx = minx.min(p[0]);
            miny = miny.min(p[1]);
            maxx = maxx.max(p[0]);
            maxy = maxy.max(p[1]);
        }
        let w = (maxx - minx).max(0.0);
        let h = (maxy - miny).max(0.0);
        // Cells at least r wide, and not many more cells than points.
        let n = pts.len().max(1) as f64;
        let mut cell = r.max(1e-6).max(((w * h) / (2.0 * n)).sqrt());
        if !cell.is_finite() || cell <= 0.0 {
            cell = 1.0;
        }
        let nx = ((w / cell).floor() as i64 + 1).max(1);
        let ny = ((h / cell).floor() as i64 + 1).max(1);
        let ncell = (nx * ny) as usize;
        let mut count = vec![0u32; ncell + 1];
        let cells: Vec<usize> = pts
            .iter()
            .map(|p| {
                let cx = (((p[0] - minx) / cell).floor() as i64).clamp(0, nx - 1);
                let cy = (((p[1] - miny) / cell).floor() as i64).clamp(0, ny - 1);
                (cy * nx + cx) as usize
            })
            .collect();
        for &c in &cells {
            count[c + 1] += 1;
        }
        for c in 0..ncell {
            count[c + 1] += count[c];
        }
        let start = count.clone();
        let mut fill = count;
        let mut idx = vec![0u32; pts.len()];
        for (k, &c) in cells.iter().enumerate() {
            idx[fill[c] as usize] = k as u32;
            fill[c] += 1;
        }
        Grid {
            minx,
            miny,
            cell,
            nx,
            ny,
            start,
            idx,
        }
    }

    /// Calls f(j) for every point j that may lie within r of q (a superset; the
    /// caller checks the distance). Points come cell by cell.
    fn around(&self, q: P2, r: f64, mut f: impl FnMut(usize)) {
        let m = r * 1.0001 + 1e-9;
        let x0 = ((q[0] - m - self.minx) / self.cell).floor();
        let x1 = ((q[0] + m - self.minx) / self.cell).floor();
        let y0 = ((q[1] - m - self.miny) / self.cell).floor();
        let y1 = ((q[1] + m - self.miny) / self.cell).floor();
        if !(x1 >= 0.0 && y1 >= 0.0 && x0 < self.nx as f64 && y0 < self.ny as f64) {
            return;
        }
        let x0 = (x0.max(0.0) as i64).min(self.nx - 1);
        let x1 = (x1 as i64).min(self.nx - 1);
        let y0 = (y0.max(0.0) as i64).min(self.ny - 1);
        let y1 = (y1 as i64).min(self.ny - 1);
        for cy in y0..=y1 {
            for cx in x0..=x1 {
                let c = (cy * self.nx + cx) as usize;
                for k in self.start[c]..self.start[c + 1] {
                    f(self.idx[k as usize] as usize);
                }
            }
        }
    }
}

#[inline]
fn d2(a: P2, b: P2) -> f64 {
    let dx = a[0] - b[0];
    let dy = a[1] - b[1];
    dx * dx + dy * dy
}

/// Python `_vote_shift`: the most common displacement between predictions and blobs.
fn vote_shift(
    pred: &[P2],
    blobs: &[P2],
    grid: Option<&Grid>,
    gate: f64,
    scratch: &mut Vec<usize>,
) -> (P2, i64) {
    if pred.is_empty() || blobs.is_empty() {
        return ([0.0, 0.0], 0);
    }
    let grid = grid.expect("grid for nonempty blobs");
    let g2 = gate * gate;
    let mut d: Vec<P2> = Vec::new();
    for &p in pred {
        scratch.clear();
        grid.around(p, gate, |j| scratch.push(j));
        for &j in scratch.iter() {
            if d2(p, blobs[j]) <= g2 {
                d.push([blobs[j][0] - p[0], blobs[j][1] - p[1]]);
            }
        }
    }
    if d.is_empty() {
        return ([0.0, 0.0], 0);
    }
    let nb = (gate / 2.0).ceil() as i64;
    let edges: Vec<f64> = (-nb..=nb).map(|k| k as f64 * 2.0 - 1.0).collect();
    let nbins = edges.len() - 1;
    let last = edges[nbins];
    // np.histogramdd: searchsorted(edges, x, 'right') - 1, with the last edge in the last bin.
    let ne = edges.len() as i64;
    let bin = |x: f64| -> Option<usize> {
        // cnt = number of edges <= x (searchsorted 'right'), from a guess fixed up exactly.
        let mut cnt = (((x - edges[0]) / 2.0).floor() + 1.0).clamp(0.0, ne as f64) as i64;
        while cnt < ne && edges[cnt as usize] <= x {
            cnt += 1;
        }
        while cnt > 0 && edges[cnt as usize - 1] > x {
            cnt -= 1;
        }
        let cnt = cnt as usize;
        let cnt = if x == last { cnt - 1 } else { cnt };
        if cnt >= 1 && cnt <= nbins {
            Some(cnt - 1)
        } else {
            None
        }
    };
    let mut h = vec![0.0f64; nbins * nbins];
    for v in &d {
        if let (Some(ix), Some(iy)) = (bin(v[0]), bin(v[1])) {
            h[ix * nbins + iy] += 1.0;
        }
    }
    let at = |a: &Vec<f64>, i: usize, j: usize| a[i * nbins + j];
    let mut hs = h.clone();
    for i in 0..nbins {
        for j in 0..nbins {
            let mut s = hs[i * nbins + j];
            if i >= 1 {
                s += at(&h, i - 1, j);
            }
            if i + 1 < nbins {
                s += at(&h, i + 1, j);
            }
            hs[i * nbins + j] = s;
        }
    }
    let mut hs2 = hs.clone();
    for i in 0..nbins {
        for j in 0..nbins {
            let mut s = hs2[i * nbins + j];
            if j >= 1 {
                s += at(&hs, i, j - 1);
            }
            if j + 1 < nbins {
                s += at(&hs, i, j + 1);
            }
            hs2[i * nbins + j] = s;
        }
    }
    let mx = hs2.iter().cloned().fold(f64::NEG_INFINITY, f64::max);
    let thr = 0.7 * mx;
    let center = |k: usize| edges[k] + 1.0;
    let mut best: Option<(f64, usize, usize)> = None;
    for i in 0..nbins {
        for j in 0..nbins {
            if hs2[i * nbins + j] >= thr {
                let mag = center(i).hypot(center(j));
                if best.is_none_or(|b| mag < b.0) {
                    best = Some((mag, i, j));
                }
            }
        }
    }
    let (_, ix, iy) = best.expect("a strong peak");
    let c = [center(ix), center(iy)];
    let (mut sx, mut sy, mut n) = (0.0, 0.0, 0i64);
    for v in &d {
        if (v[0] - c[0]).abs().max((v[1] - c[1]).abs()) <= 3.0 {
            sx += v[0];
            sy += v[1];
            n += 1;
        }
    }
    if n == 0 {
        return ([0.0, 0.0], 0);
    }
    ([sx / n as f64, sy / n as f64], n)
}

/// Python `_greedy_match`: one-to-one nearest matching within a per-prediction gate,
/// over each prediction's two nearest points (within the largest gate).
fn greedy_match(
    pred: &[P2],
    pts: &[P2],
    gate: &[f64],
    scratch: &mut Vec<usize>,
) -> (Vec<i64>, Vec<i64>) {
    let mut mt = vec![-1i64; pred.len()];
    let mut mb = vec![-1i64; pts.len()];
    if pred.is_empty() || pts.is_empty() {
        return (mt, mb);
    }
    let gmax = gate.iter().cloned().fold(f64::NEG_INFINITY, f64::max);
    let grid = Grid::new(pts, gmax);
    let ub2 = gmax * gmax;
    let mut cand: Vec<(f64, usize, usize)> = Vec::new();
    for (i, &p) in pred.iter().enumerate() {
        scratch.clear();
        grid.around(p, gmax, |j| scratch.push(j));
        // The two nearest (ties: lower index first).
        let mut b1: Option<(f64, usize)> = None;
        let mut b2: Option<(f64, usize)> = None;
        for &j in scratch.iter() {
            let dd = d2(p, pts[j]);
            if dd > ub2 {
                continue;
            }
            let key = (dd, j);
            let less = |a: (f64, usize), b: Option<(f64, usize)>| {
                b.is_none_or(|b| a.0 < b.0 || (a.0 == b.0 && a.1 < b.1))
            };
            if less(key, b1) {
                b2 = b1;
                b1 = Some(key);
            } else if less(key, b2) {
                b2 = Some(key);
            }
        }
        for b in [b1, b2].into_iter().flatten() {
            let dist = b.0.sqrt();
            if dist <= gate[i] {
                cand.push((dist, i, b.1));
            }
        }
    }
    cand.sort_by(|a, b| {
        a.0.partial_cmp(&b.0)
            .unwrap()
            .then(a.1.cmp(&b.1))
            .then(a.2.cmp(&b.2))
    });
    for (_d, i, j) in cand {
        if mt[i] < 0 && mb[j] < 0 {
            mt[i] = j as i64;
            mb[j] = i as i64;
        }
    }
    (mt, mb)
}

fn apply_h(h: &[f64], p: &[P2]) -> Vec<P2> {
    p.iter()
        .map(|q| {
            let x = q[0] * h[0] + q[1] * h[1] + h[2];
            let y = q[0] * h[3] + q[1] * h[4] + h[5];
            let w = q[0] * h[6] + q[1] * h[7] + h[8];
            [x / w, y / w]
        })
        .collect()
}

#[inline]
fn add(a: &mut [P2], s: P2) {
    for p in a.iter_mut() {
        p[0] += s[0];
        p[1] += s[1];
    }
}

struct Out {
    rows: Vec<[f64; 7]>,
    next_id: i64,
    shifts: Vec<P2>,
}

#[allow(clippy::too_many_arguments)]
fn run(
    py: Python<'_>,
    blobs: &numpy::ndarray::ArrayView2<f64>,
    offsets: &[i64],
    t: &[f64],
    hs: &numpy::ndarray::ArrayView3<f64>,
    has_h: &[bool],
    prm: Params,
    progress: Option<&Bound<'_, PyAny>>,
) -> PyResult<Out> {
    let nf = t.len();
    let (mut pos, mut vel, mut last, mut ids): (Vec<P2>, Vec<P2>, Vec<f64>, Vec<i64>) =
        (vec![], vec![], vec![], vec![]);
    let (mut lpos, mut lvel, mut llast, mut lids): (Vec<P2>, Vec<P2>, Vec<f64>, Vec<i64>) =
        (vec![], vec![], vec![], vec![]);
    let mut next_id: i64 = 0;
    let mut rows: Vec<[f64; 7]> = Vec::new();
    let mut shifts = vec![[0.0, 0.0]; nf];
    let mut shift_rate = [0.0f64, 0.0];
    let mut lock: i64 = 0;
    let every = (nf / 100).max(1);
    let mut scratch = Vec::new();
    let row = |id: i64, fi: usize, b: usize| -> [f64; 7] {
        [
            id as f64,
            fi as f64,
            blobs[[b, 0]],
            blobs[[b, 1]],
            blobs[[b, 2]],
            blobs[[b, 4]],
            blobs[[b, 5]],
        ]
    };
    for fi in 0..nf {
        if let Some(cb) = progress {
            if fi % every == 0 {
                cb.call1((fi as f64 / nf as f64,))?;
            }
        }
        if fi % 64 == 0 {
            py.check_signals()?;
        }
        let o0 = offsets[fi] as usize;
        let o1 = offsets[fi + 1] as usize;
        let bxy: Vec<P2> = (o0..o1).map(|k| [blobs[[k, 0]], blobs[[k, 1]]]).collect();
        let tf = t[fi];
        let gyro = has_h[fi];
        let dt = if fi > 0 { tf - t[fi - 1] } else { 0.0 };
        let hm: Vec<f64> = if gyro {
            (0..9).map(|k| hs[[fi, k / 3, k % 3]]).collect()
        } else {
            vec![]
        };
        let mv = |p: &[P2], v: &[P2]| -> (Vec<P2>, Vec<P2>) {
            let base = if gyro && !p.is_empty() {
                apply_h(&hm, p)
            } else {
                p.to_vec()
            };
            let pred = base
                .iter()
                .zip(v)
                .map(|(b, v)| [b[0] + v[0] * dt, b[1] + v[1] * dt])
                .collect();
            (base, pred)
        };
        let (mut base, mut pred) = mv(&pos, &vel);
        let (mut lbase, mut lpred) = mv(&lpos, &lvel);
        let grid = if bxy.is_empty() {
            None
        } else {
            Some(Grid::new(
                &bxy,
                0.5 * prm.vote_gate_nogyro.min(prm.vote_gate_gyro),
            ))
        };
        if fi > 0 && !pred.is_empty() {
            let expected = [shift_rate[0] * dt, shift_rate[1] * dt];
            add(&mut pred, expected);
            add(&mut base, expected);
            add(&mut lpred, expected);
            add(&mut lbase, expected);
            let locked = gyro && lock >= prm.lock_frames;
            let gate = if locked {
                prm.vote_gate_gyro
            } else {
                prm.vote_gate_nogyro
            };
            let (s, mut votes) = vote_shift(&pred, &bxy, grid.as_ref(), gate, &mut scratch);
            if locked
                && s[0].hypot(s[1]) > prm.max_deviation_px + 0.3 * expected[0].hypot(expected[1])
            {
                votes = 0;
            }
            if votes >= 3 {
                add(&mut pred, s);
                add(&mut base, s);
                add(&mut lpred, s);
                add(&mut lbase, s);
                if dt > 0.0 {
                    let rate = [(expected[0] + s[0]) / dt, (expected[1] + s[1]) / dt];
                    shift_rate = if lock == 0 {
                        rate
                    } else {
                        [
                            (1.0 - prm.shift_alpha) * shift_rate[0] + prm.shift_alpha * rate[0],
                            (1.0 - prm.shift_alpha) * shift_rate[1] + prm.shift_alpha * rate[1],
                        ]
                    };
                }
                lock += 1;
                shifts[fi] = [expected[0] + s[0], expected[1] + s[1]];
            } else {
                lock = 0;
                shifts[fi] = [expected[0] + 0.0, expected[1] + 0.0];
            }
        }
        let gates = vec![prm.gate_px; pred.len()];
        let (mt, mb) = greedy_match(&pred, &bxy, &gates, &mut scratch);
        let va = prm.vel_alpha;
        for i in 0..pred.len() {
            if mt[i] >= 0 {
                let j = mt[i] as usize;
                let new = bxy[j];
                if dt > 0.0 {
                    for a in 0..2 {
                        vel[i][a] = (1.0 - va) * vel[i][a] + va * (new[a] - base[i][a]) / dt;
                    }
                }
                pos[i] = new;
                last[i] = tf;
                rows.push(row(ids[i], fi, o0 + j));
            } else {
                pos[i] = pred[i];
            }
        }
        lpos = lpred;
        // Unmatched blobs: re-acquire lost tracks before starting new ones.
        let mut free: Vec<usize> = (0..bxy.len()).filter(|&j| mb[j] < 0).collect();
        if !free.is_empty() && !lpos.is_empty() {
            let gate: Vec<f64> = llast
                .iter()
                .map(|&l| {
                    (prm.gate_px + prm.reacquire_px_per_s * (tf - l)).min(prm.reacquire_max_px)
                })
                .collect();
            let fxy: Vec<P2> = free.iter().map(|&j| bxy[j]).collect();
            let (lt, _lb) = greedy_match(&lpos, &fxy, &gate, &mut scratch);
            let back: Vec<usize> = (0..lpos.len()).filter(|&i| lt[i] >= 0).collect();
            if !back.is_empty() {
                let mut taken = vec![false; bxy.len()];
                for &i in &back {
                    let j = free[lt[i] as usize];
                    pos.push(bxy[j]);
                    vel.push(lvel[i]);
                    last.push(tf);
                    ids.push(lids[i]);
                    taken[j] = true;
                }
                for &i in &back {
                    rows.push(row(lids[i], fi, o0 + free[lt[i] as usize]));
                }
                let keep: Vec<bool> = lt.iter().map(|&v| v < 0).collect();
                retain4(&keep, &mut lpos, &mut lvel, &mut llast, &mut lids);
                free.retain(|&f| !taken[f]);
            }
        }
        // Live tracks unseen too long become lost; lost tracks expire.
        let stale: Vec<bool> = last.iter().map(|&l| (tf - l) > prm.max_coast_s).collect();
        if stale.iter().any(|&s| s) {
            for k in 0..pos.len() {
                if stale[k] {
                    lpos.push(pos[k]);
                    lvel.push(vel[k]);
                    llast.push(last[k]);
                    lids.push(ids[k]);
                }
            }
            let keep: Vec<bool> = stale.iter().map(|&s| !s).collect();
            retain4(&keep, &mut pos, &mut vel, &mut last, &mut ids);
        }
        let alive: Vec<bool> = llast.iter().map(|&l| (tf - l) <= prm.max_lost_s).collect();
        retain4(&alive, &mut lpos, &mut lvel, &mut llast, &mut lids);
        for &j in &free {
            pos.push(bxy[j]);
            vel.push([0.0, 0.0]);
            last.push(tf);
            ids.push(next_id);
            rows.push(row(next_id, fi, o0 + j));
            next_id += 1;
        }
    }
    Ok(Out {
        rows,
        next_id,
        shifts,
    })
}

fn retain4(keep: &[bool], a: &mut Vec<P2>, b: &mut Vec<P2>, c: &mut Vec<f64>, d: &mut Vec<i64>) {
    let mut k = 0;
    a.retain(|_| {
        k += 1;
        keep[k - 1]
    });
    k = 0;
    b.retain(|_| {
        k += 1;
        keep[k - 1]
    });
    k = 0;
    c.retain(|_| {
        k += 1;
        keep[k - 1]
    });
    k = 0;
    d.retain(|_| {
        k += 1;
        keep[k - 1]
    });
}

/// track(blobs (N,>=6) f64 of all frames, offsets (F+1,) i64, t (F,) f64, H (F,3,3) f64,
///       has_h (F,) bool, params (11 floats, see walk/tracking.py), progress callable or None)
///   -> (rows (M,7) f64, n_tracks, shifts (F,2) f64)
#[pyfunction]
#[pyo3(signature = (blobs, offsets, t, h, has_h, params, progress=None))]
#[allow(clippy::too_many_arguments, clippy::type_complexity)]
fn track<'py>(
    py: Python<'py>,
    blobs: PyReadonlyArray2<'py, f64>,
    offsets: PyReadonlyArray1<'py, i64>,
    t: PyReadonlyArray1<'py, f64>,
    h: PyReadonlyArray3<'py, f64>,
    has_h: PyReadonlyArray1<'py, bool>,
    params: Vec<f64>,
    progress: Option<Bound<'py, PyAny>>,
) -> PyResult<(Bound<'py, PyArray2<f64>>, i64, Bound<'py, PyArray2<f64>>)> {
    if params.len() != 11 {
        return Err(pyo3::exceptions::PyValueError::new_err(
            "params: 11 values expected",
        ));
    }
    let prm = Params {
        gate_px: params[0],
        vote_gate_gyro: params[1],
        vote_gate_nogyro: params[2],
        max_coast_s: params[3],
        max_deviation_px: params[4],
        lock_frames: params[5] as i64,
        shift_alpha: params[6],
        max_lost_s: params[7],
        reacquire_px_per_s: params[8],
        reacquire_max_px: params[9],
        vel_alpha: params[10],
    };
    let b = blobs.as_array();
    let off = offsets.as_slice()?;
    let tt = t.as_slice()?;
    let hh = h.as_array();
    let hf = has_h.as_slice()?;
    let nf = tt.len();
    if off.len() != nf + 1
        || hf.len() != nf
        || hh.shape() != [nf, 3, 3]
        || (b.nrows() > 0 && b.ncols() < 6)
    {
        return Err(pyo3::exceptions::PyValueError::new_err(
            "inconsistent array shapes",
        ));
    }
    if off[0] != 0 || off.windows(2).any(|w| w[1] < w[0]) || off[nf] as usize != b.nrows() {
        return Err(pyo3::exceptions::PyValueError::new_err("bad offsets"));
    }
    let out = run(py, &b, off, tt, &hh, hf, prm, progress.as_ref())?;
    let m = out.rows.len();
    let rows =
        Array2::from_shape_vec((m, 7), out.rows.into_iter().flatten().collect()).expect("shape");
    let sh =
        Array2::from_shape_vec((nf, 2), out.shifts.into_iter().flatten().collect()).expect("shape");
    Ok((
        PyArray2::from_owned_array(py, rows),
        out.next_id,
        PyArray2::from_owned_array(py, sh),
    ))
}

pub fn register(m: &Bound<'_, PyModule>) -> PyResult<()> {
    m.add_function(wrap_pyfunction!(track, m)?)?;
    Ok(())
}
