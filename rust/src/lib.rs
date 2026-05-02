//! gffbase native core — PyO3 entry point.
//!
//! Exposes a single `parse_file(path, force_dialect_check=False, checklines=10)`
//! callable plus a `parse_bytes(data, ...)` callable. Both yield Python tuples
//! with the canonical 11-tuple shape that `gffbase.parser` consumes:
//!
//!     (seqid, source, featuretype, start, end, score, strand, frame,
//!      attributes_blob, attributes_pairs, extra)
//!
//! `attributes_pairs` is a list[(key, value, idx)]; `idx` preserves multi-value
//! ordering. `start`/`end` are int or None (`.` becomes None). `attributes_blob`
//! is the raw col-9 bytes for byte-faithful round-trip.

use pyo3::prelude::*;
use pyo3::types::{PyBytes, PyList, PyTuple};
use pyo3::exceptions::{PyIOError, PyValueError};

mod dialect;
mod attributes;
mod escape;
mod parser;

use parser::{ParseOptions, RecordIter, FileSource};

/// Parse a path (plain text or .gz). Yields one tuple per feature.
#[pyfunction]
#[pyo3(signature = (path, checklines=10, force_dialect_check=false, force_gff=false))]
fn parse_file(
    py: Python<'_>,
    path: &str,
    checklines: usize,
    force_dialect_check: bool,
    force_gff: bool,
) -> PyResult<PyObject> {
    let opts = ParseOptions {
        checklines,
        force_dialect_check,
        force_gff,
    };
    let source = FileSource::open(path)
        .map_err(|e| PyIOError::new_err(format!("could not open {}: {}", path, e)))?;
    let iter = RecordIter::new(source, opts)
        .map_err(|e| PyValueError::new_err(format!("parser error: {}", e)))?;
    let py_iter = PyRecordIterator { inner: Some(iter) };
    Ok(Py::new(py, py_iter)?.into_any())
}

/// Parse an in-memory byte buffer. Yields one tuple per feature.
#[pyfunction]
#[pyo3(signature = (data, checklines=10, force_dialect_check=false, force_gff=false))]
fn parse_bytes(
    py: Python<'_>,
    data: &[u8],
    checklines: usize,
    force_dialect_check: bool,
    force_gff: bool,
) -> PyResult<PyObject> {
    let opts = ParseOptions {
        checklines,
        force_dialect_check,
        force_gff,
    };
    let source = FileSource::from_bytes(data.to_vec());
    let iter = RecordIter::new(source, opts)
        .map_err(|e| PyValueError::new_err(format!("parser error: {}", e)))?;
    let py_iter = PyRecordIterator { inner: Some(iter) };
    Ok(Py::new(py, py_iter)?.into_any())
}

/// Detect the dialect of an input by reading the first `checklines` features
/// and return it as a Python dict. Useful for tests and for the API layer.
#[pyfunction]
#[pyo3(signature = (path, checklines=10))]
fn detect_dialect(py: Python<'_>, path: &str, checklines: usize) -> PyResult<PyObject> {
    let source = FileSource::open(path)
        .map_err(|e| PyIOError::new_err(format!("could not open {}: {}", path, e)))?;
    let opts = ParseOptions {
        checklines,
        force_dialect_check: false,
        force_gff: false,
    };
    let iter = RecordIter::new(source, opts)
        .map_err(|e| PyValueError::new_err(format!("parser error: {}", e)))?;
    Ok(dialect_to_pydict(py, iter.dialect())?)
}

#[pyclass]
struct PyRecordIterator {
    inner: Option<RecordIter>,
}

#[pymethods]
impl PyRecordIterator {
    fn __iter__(slf: PyRef<'_, Self>) -> PyRef<'_, Self> {
        slf
    }

    fn __next__(mut slf: PyRefMut<'_, Self>, py: Python<'_>) -> PyResult<Option<PyObject>> {
        let iter = match slf.inner.as_mut() {
            Some(i) => i,
            None => return Ok(None),
        };
        match iter.next() {
            Some(Ok(rec)) => Ok(Some(record_to_pytuple(py, &rec)?)),
            Some(Err(e)) => Err(PyValueError::new_err(format!("parser error: {}", e))),
            // Do NOT null out `inner` here: callers expect to be able to read
            // `.dialect()` and `.directives()` after the iterator is exhausted.
            None => Ok(None),
        }
    }

    /// Return the inferred dialect dict. Available after iteration begins or
    /// after the peek phase completes.
    fn dialect(&self, py: Python<'_>) -> PyResult<PyObject> {
        match &self.inner {
            Some(it) => dialect_to_pydict(py, it.dialect()),
            None => Ok(py.None()),
        }
    }

    /// Return the list of `##` directives encountered so far.
    fn directives(&self, py: Python<'_>) -> PyResult<PyObject> {
        let list = PyList::empty_bound(py);
        if let Some(it) = &self.inner {
            for d in it.directives() {
                list.append(d.as_str())?;
            }
        }
        Ok(list.into_any().unbind())
    }
}

fn record_to_pytuple(py: Python<'_>, rec: &parser::Record) -> PyResult<PyObject> {
    let start_obj: PyObject = match rec.start {
        Some(v) => v.into_py(py),
        None => py.None(),
    };
    let end_obj: PyObject = match rec.end {
        Some(v) => v.into_py(py),
        None => py.None(),
    };
    let attrs_blob = PyBytes::new_bound(py, &rec.attributes_blob);
    let pairs = PyList::empty_bound(py);
    for (k, v, idx) in &rec.attributes_pairs {
        let t = PyTuple::new_bound(py, &[k.into_py(py), v.into_py(py), (*idx as i64).into_py(py)]);
        pairs.append(t)?;
    }
    let extra_list = PyList::empty_bound(py);
    for e in &rec.extra {
        extra_list.append(e.as_str())?;
    }
    let tup = PyTuple::new_bound(
        py,
        &[
            rec.seqid.clone().into_py(py),
            rec.source.clone().into_py(py),
            rec.featuretype.clone().into_py(py),
            start_obj,
            end_obj,
            rec.score.clone().into_py(py),
            rec.strand.clone().into_py(py),
            rec.frame.clone().into_py(py),
            attrs_blob.into_any().unbind(),
            pairs.into_any().unbind(),
            extra_list.into_any().unbind(),
        ],
    );
    Ok(tup.into_any().unbind())
}

fn dialect_to_pydict(py: Python<'_>, d: &dialect::Dialect) -> PyResult<PyObject> {
    let dict = pyo3::types::PyDict::new_bound(py);
    dict.set_item("fmt", d.fmt_str())?;
    dict.set_item("field separator", d.field_separator.as_str())?;
    dict.set_item("keyval separator", d.keyval_separator.to_string())?;
    dict.set_item("multival separator", d.multival_separator.to_string())?;
    dict.set_item("leading semicolon", d.leading_semicolon)?;
    dict.set_item("trailing semicolon", d.trailing_semicolon)?;
    dict.set_item("quoted GFF2 values", d.quoted_gff2_values)?;
    dict.set_item("repeated keys", d.repeated_keys)?;
    dict.set_item("semicolon in quotes", d.semicolon_in_quotes)?;
    let order = PyList::empty_bound(py);
    for k in &d.order {
        order.append(k.as_str())?;
    }
    dict.set_item("order", order)?;
    Ok(dict.into_any().unbind())
}

#[pymodule]
fn _native(_py: Python<'_>, m: &Bound<'_, PyModule>) -> PyResult<()> {
    m.add_function(wrap_pyfunction!(parse_file, m)?)?;
    m.add_function(wrap_pyfunction!(parse_bytes, m)?)?;
    m.add_function(wrap_pyfunction!(detect_dialect, m)?)?;
    m.add("__version__", env!("CARGO_PKG_VERSION"))?;
    Ok(())
}
