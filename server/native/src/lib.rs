//! Native (Rust) kernels for the heaviest parts of walk processing and reconstruction.
//!
//! Python (`treelights/native.py`) imports this module if it is installed and falls back
//! to the pure-Python implementations otherwise. Each module registers its own functions.

use pyo3::prelude::*;

mod ba;
mod fill;
mod hyp;
mod track;

#[pyfunction]
fn version() -> &'static str {
    env!("CARGO_PKG_VERSION")
}

#[pymodule]
fn treelights_native(m: &Bound<'_, PyModule>) -> PyResult<()> {
    m.add_function(wrap_pyfunction!(version, m)?)?;
    ba::register(m)?;
    fill::register(m)?;
    hyp::register(m)?;
    track::register(m)?;
    Ok(())
}
