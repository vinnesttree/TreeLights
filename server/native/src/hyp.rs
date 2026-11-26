//! Per-LED hypotheses and point refinement (reconstruction/hypotheses.py, geometry.refine_point,
//! fill.refine_points).
use pyo3::prelude::*;

pub fn register(_m: &Bound<'_, PyModule>) -> PyResult<()> {
    Ok(())
}
