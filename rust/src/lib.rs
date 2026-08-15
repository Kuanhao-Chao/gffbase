// ---------------------------------------------------------------------------
// Author: Kuan-Hao Chao <kuanhao.chao@gmail.com>
// Copyright 2026 Kuan-Hao Chao
//
// Licensed under the Apache License, Version 2.0 (the "License");
// you may not use this file except in compliance with the License.
// You may obtain a copy of the License at
//
//     http://www.apache.org/licenses/LICENSE-2.0
//
// Unless required by applicable law or agreed to in writing, software
// distributed under the License is distributed on an "AS IS" BASIS,
// WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
// See the License for the specific language governing permissions and
// limitations under the License.
// ---------------------------------------------------------------------------
//! gffbase native core — PyO3 entry point.
//!
//! Exposes a single `parse_file(path, force_dialect_check=False, checklines=10)`
//! callable plus a `parse_bytes(data, ...)` callable. Both yield Python tuples
//! with the canonical 11-tuple shape that `gffbase.parser` consumes:
//!
//! ```text
//! (seqid, source, featuretype, start, end, score, strand, frame,
//!  attributes_blob, attributes_pairs, extra)
//! ```
//!
//! `attributes_pairs` is a list[(key, value, idx)]; `idx` preserves multi-value
//! ordering. `start`/`end` are int or None (`.` becomes None). `attributes_blob`
//! is the raw col-9 bytes for byte-faithful round-trip.

use pyo3::create_exception;
use pyo3::exceptions::{PyIOError, PyValueError};
use pyo3::prelude::*;
use pyo3::types::{PyBytes, PyDict, PyList, PyTuple};

mod attributes;
mod dialect;
mod escape;
mod parser;
mod validate;

use parser::{FileSource, ParseOptions, RecordIter};
use validate::{GffError, ValidationProfile};

// Phase 16: descriptive parser errors. Subclassing `PyValueError` keeps
// legacy `pytest.raises(ValueError)` calls working while letting users
// catch the more specific `GFFFormatError` for rich line-numbered context.
create_exception!(_native, GFFFormatError, PyValueError);

/// Convert a structured `GffError` into a Python `GFFFormatError` with
/// `.line_no`, `.kind`, and `.message` attributes attached on the
/// exception instance.
fn gff_error_to_py(py: Python<'_>, e: GffError) -> PyErr {
    let msg = format!("line {}: {}", e.line_no, e.message);
    let err = GFFFormatError::new_err(msg);
    // Attach structured fields onto the exception instance so Python callers
    // can branch on `.kind` and report `.line_no` without re-parsing `str(e)`.
    let inst = err.value(py);
    let _ = inst.setattr("line_no", e.line_no);
    let _ = inst.setattr("kind", e.kind.as_str());
    let _ = inst.setattr("message", e.message.clone());
    err
}

/// Map the Python-facing profile name onto the enum.
///
/// `"gffutils"` is the compatibility rule set used by `create_db()`;
/// `"ncbi"` is the full specification. Anything else is a caller error.
fn parse_profile(name: &str) -> PyResult<ValidationProfile> {
    match name {
        "gffutils" | "compat" => Ok(ValidationProfile::Gffutils),
        "ncbi" | "strict" => Ok(ValidationProfile::Ncbi),
        other => Err(PyValueError::new_err(format!(
            "validation must be 'gffutils' or 'ncbi'; got {:?}",
            other
        ))),
    }
}

/// Parse a path (plain text or .gz). Yields one tuple per feature.
#[pyfunction]
#[pyo3(signature = (path, checklines=10, force_dialect_check=false, force_gff=false, strict=true, validation="ncbi"))]
fn parse_file(
    py: Python<'_>,
    path: &str,
    checklines: usize,
    force_dialect_check: bool,
    force_gff: bool,
    strict: bool,
    validation: &str,
) -> PyResult<Py<PyAny>> {
    let opts = ParseOptions {
        checklines,
        force_dialect_check,
        force_gff,
        strict,
        profile: parse_profile(validation)?,
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
#[pyo3(signature = (data, checklines=10, force_dialect_check=false, force_gff=false, strict=true, validation="ncbi"))]
fn parse_bytes(
    py: Python<'_>,
    data: &[u8],
    checklines: usize,
    force_dialect_check: bool,
    force_gff: bool,
    strict: bool,
    validation: &str,
) -> PyResult<Py<PyAny>> {
    let opts = ParseOptions {
        checklines,
        force_dialect_check,
        force_gff,
        strict,
        profile: parse_profile(validation)?,
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
fn detect_dialect(py: Python<'_>, path: &str, checklines: usize) -> PyResult<Py<PyAny>> {
    let source = FileSource::open(path)
        .map_err(|e| PyIOError::new_err(format!("could not open {}: {}", path, e)))?;
    let opts = ParseOptions {
        checklines,
        force_dialect_check: false,
        force_gff: false,
        // Dialect detection is non-strict by design: malformed lines in
        // the first `checklines` get skipped without poisoning detection.
        strict: false,
        profile: ValidationProfile::Gffutils,
    };
    let iter = RecordIter::new(source, opts)
        .map_err(|e| PyValueError::new_err(format!("parser error: {}", e)))?;
    dialect_to_pydict(py, iter.dialect())
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

    fn __next__(mut slf: PyRefMut<'_, Self>, py: Python<'_>) -> PyResult<Option<Py<PyAny>>> {
        let iter = match slf.inner.as_mut() {
            Some(i) => i,
            None => return Ok(None),
        };
        match iter.next() {
            Some(Ok(rec)) => Ok(Some(record_to_pytuple(py, &rec)?)),
            // Structured GffError → Python GFFFormatError with .line_no /
            // .kind / .message attributes.
            Some(Err(e)) => Err(gff_error_to_py(py, e)),
            // Do NOT null out `inner` here: callers expect to be able to read
            // `.dialect()` and `.directives()` after the iterator is exhausted.
            None => Ok(None),
        }
    }

    /// List of `GffError`s collected during a non-strict run. Each item
    /// is a dict with keys `line_no`, `kind`, and `message`. Empty when
    /// the iterator was created with `strict=True` (the default), since
    /// errors propagate via `__next__` instead in that mode.
    fn warnings(&self, py: Python<'_>) -> PyResult<Py<PyAny>> {
        let list = PyList::empty(py);
        if let Some(it) = &self.inner {
            for w in it.warnings() {
                let d = PyDict::new(py);
                d.set_item("line_no", w.line_no)?;
                d.set_item("kind", w.kind.as_str())?;
                d.set_item("message", w.message.as_str())?;
                list.append(d)?;
            }
        }
        Ok(list.into_any().unbind())
    }

    /// Return the inferred dialect dict. Available after iteration begins or
    /// after the peek phase completes.
    fn dialect(&self, py: Python<'_>) -> PyResult<Py<PyAny>> {
        match &self.inner {
            Some(it) => dialect_to_pydict(py, it.dialect()),
            None => Ok(py.None()),
        }
    }

    /// Return the list of `##` directives encountered so far.
    fn directives(&self, py: Python<'_>) -> PyResult<Py<PyAny>> {
        let list = PyList::empty(py);
        if let Some(it) = &self.inner {
            for d in it.directives() {
                list.append(d.as_str())?;
            }
        }
        Ok(list.into_any().unbind())
    }
}

fn record_to_pytuple(py: Python<'_>, rec: &parser::Record) -> PyResult<Py<PyAny>> {
    // `Option<i64>` converts to an int or to `None`, which is what preserves a
    // `.` coordinate as Python `None` rather than coercing it to 0.
    let start_obj = rec.start.into_pyobject(py)?;
    let end_obj = rec.end.into_pyobject(py)?;

    let attrs_blob = PyBytes::new(py, &rec.attributes_blob);

    let pairs = PyList::empty(py);
    for (k, v, idx) in &rec.attributes_pairs {
        // `idx` stays an int: the Python side stores it in a SMALLINT column
        // and orders multi-valued attributes by it.
        pairs.append(PyTuple::new(
            py,
            [
                k.as_str().into_pyobject(py)?.into_any(),
                v.as_str().into_pyobject(py)?.into_any(),
                (*idx as i64).into_pyobject(py)?.into_any(),
            ],
        )?)?;
    }

    let extra_list = PyList::empty(py);
    for e in &rec.extra {
        extra_list.append(e.as_str())?;
    }

    let tup = PyTuple::new(
        py,
        [
            rec.seqid.as_str().into_pyobject(py)?.into_any(),
            rec.source.as_str().into_pyobject(py)?.into_any(),
            rec.featuretype.as_str().into_pyobject(py)?.into_any(),
            start_obj.into_any(),
            end_obj.into_any(),
            rec.score.as_str().into_pyobject(py)?.into_any(),
            rec.strand.as_str().into_pyobject(py)?.into_any(),
            rec.frame.as_str().into_pyobject(py)?.into_any(),
            attrs_blob.into_any(),
            pairs.into_any(),
            extra_list.into_any(),
        ],
    )?;
    Ok(tup.into_any().unbind())
}

fn dialect_to_pydict(py: Python<'_>, d: &dialect::Dialect) -> PyResult<Py<PyAny>> {
    let dict = pyo3::types::PyDict::new(py);
    dict.set_item("fmt", d.fmt_str())?;
    dict.set_item("field separator", d.field_separator.as_str())?;
    dict.set_item("keyval separator", d.keyval_separator.to_string())?;
    dict.set_item("multival separator", d.multival_separator.to_string())?;
    dict.set_item("leading semicolon", d.leading_semicolon)?;
    dict.set_item("trailing semicolon", d.trailing_semicolon)?;
    dict.set_item("quoted GFF2 values", d.quoted_gff2_values)?;
    dict.set_item("repeated keys", d.repeated_keys)?;
    dict.set_item("semicolon in quotes", d.semicolon_in_quotes)?;
    let order = PyList::empty(py);
    for k in &d.order {
        order.append(k.as_str())?;
    }
    dict.set_item("order", order)?;
    Ok(dict.into_any().unbind())
}

#[pymodule]
fn _native(py: Python<'_>, m: &Bound<'_, PyModule>) -> PyResult<()> {
    m.add_function(wrap_pyfunction!(parse_file, m)?)?;
    m.add_function(wrap_pyfunction!(parse_bytes, m)?)?;
    m.add_function(wrap_pyfunction!(detect_dialect, m)?)?;
    m.add("__version__", env!("CARGO_PKG_VERSION"))?;
    // Phase 16 — expose the descriptive Python exception type.
    m.add("GFFFormatError", py.get_type::<GFFFormatError>())?;
    Ok(())
}
