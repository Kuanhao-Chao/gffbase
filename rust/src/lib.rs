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
use pyo3::exceptions::{PyIOError, PyOSError, PyValueError};
use pyo3::prelude::*;
use pyo3::types::{PyBytes, PyDict, PyList, PyTuple};

mod attributes;
mod dialect;
mod escape;
mod parser;
mod producer;
mod validate;

use parser::{FileSource, OpenError, ParseOptions, RecordIter};
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
/// The Python exception for a failed open, matching what `open()` raises in
/// the pure-Python engine. Python's `OSError(errno, message)` constructor
/// picks the subclass itself -- `FileNotFoundError`, `PermissionError`,
/// `IsADirectoryError` -- so the OS error code is passed through rather than
/// mapped here. These all used to arrive as a bare `OSError`, so the two
/// engines raised different types for the same mistake.
fn open_error(py: Python<'_>, path: &str, e: OpenError) -> PyErr {
    let e = match e {
        OpenError::Io(e) => e,
        // What the file holds, not the file: a tar archive of several files.
        OpenError::Format(message) => {
            return PyValueError::new_err(format!("parser error: {}", message))
        }
        // The content, before a single line: a corrupt or truncated gzip.
        OpenError::Read(message) => {
            return gff_error_to_py(
                py,
                GffError::new(0, validate::ErrorKind::ReadError, message),
            )
        }
    };
    let message = format!("could not open {}: {}", path, e);
    match e.raw_os_error() {
        Some(code) => PyOSError::new_err((code, message)),
        None => PyIOError::new_err(message),
    }
}

#[pyfunction]
#[pyo3(signature = (path, checklines=10, force_dialect_check=false, force_gff=false, strict=true, validation="ncbi", ignore_url_escape_characters=false))]
// Keep the established Python-callable arguments flat; grouping them would
// change PyO3's public API solely to satisfy an internal lint.
#[allow(clippy::too_many_arguments)]
fn parse_file(
    py: Python<'_>,
    path: &str,
    checklines: usize,
    force_dialect_check: bool,
    force_gff: bool,
    strict: bool,
    validation: &str,
    ignore_url_escape_characters: bool,
) -> PyResult<Py<PyAny>> {
    let opts = ParseOptions {
        checklines,
        force_dialect_check,
        force_gff,
        strict,
        profile: parse_profile(validation)?,
        decode_url_escapes: !ignore_url_escape_characters,
    };
    let source = FileSource::open(path).map_err(|e| open_error(py, path, e))?;
    let iter = RecordIter::new(source, opts)
        .map_err(|e| PyValueError::new_err(format!("parser error: {}", e)))?;
    let py_iter = PyRecordIterator { inner: Some(iter) };
    Ok(Py::new(py, py_iter)?.into_any())
}

/// Parse an in-memory byte buffer. Yields one tuple per feature.
#[pyfunction]
#[pyo3(signature = (data, checklines=10, force_dialect_check=false, force_gff=false, strict=true, validation="ncbi", ignore_url_escape_characters=false))]
// See `parse_file`: this is a public PyO3 signature, not an internal API.
#[allow(clippy::too_many_arguments)]
fn parse_bytes(
    py: Python<'_>,
    data: &[u8],
    checklines: usize,
    force_dialect_check: bool,
    force_gff: bool,
    strict: bool,
    validation: &str,
    ignore_url_escape_characters: bool,
) -> PyResult<Py<PyAny>> {
    let opts = ParseOptions {
        checklines,
        force_dialect_check,
        force_gff,
        strict,
        profile: parse_profile(validation)?,
        decode_url_escapes: !ignore_url_escape_characters,
    };
    let source = FileSource::from_bytes(data.to_vec())
        .map_err(|e| PyValueError::new_err(format!("parser error: {}", e)))?;
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
    let source = FileSource::open(path).map_err(|e| open_error(py, path, e))?;
    let opts = ParseOptions {
        checklines,
        force_dialect_check: false,
        force_gff: false,
        // Dialect detection is non-strict by design: malformed lines in
        // the first `checklines` get skipped without poisoning detection.
        strict: false,
        profile: ValidationProfile::Gffutils,
        decode_url_escapes: true,
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
        // `idx` stays an int: the Python side stores it in an INTEGER column
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

fn public_version() -> String {
    let cargo_version = env!("CARGO_PKG_VERSION");
    match cargo_version.split_once("-rc.") {
        Some((release, candidate)) => format!("{}rc{}", release, candidate),
        None => cargo_version.to_string(),
    }
}

/// Decode the id_spec `_options.native_id_spec` built: `("keys", keys)` or
/// `("by_type", {featuretype: keys})`, each key `("attr" | "col", name)`.
fn id_spec_from_py(spec: &Bound<'_, PyAny>) -> PyResult<producer::IdSpec> {
    fn keys(list: &Bound<'_, PyAny>) -> PyResult<Vec<producer::Key>> {
        let mut out = Vec::new();
        for item in list.try_iter()? {
            let (kind, name): (String, String) = item?.extract()?;
            out.push(match kind.as_str() {
                "attr" => producer::Key::Attribute(name),
                "col" => producer::Key::Column(name),
                other => {
                    return Err(PyValueError::new_err(format!(
                        "unknown id_spec key kind {other:?}"
                    )))
                }
            });
        }
        Ok(out)
    }
    let (form, body): (String, Bound<'_, PyAny>) = spec.extract()?;
    match form.as_str() {
        "keys" => Ok(producer::IdSpec::Keys(keys(&body)?)),
        "by_type" => {
            let mut map = std::collections::HashMap::new();
            for (k, v) in body.cast::<PyDict>()?.iter() {
                map.insert(k.extract::<String>()?, keys(&v)?);
            }
            Ok(producer::IdSpec::ByType(map))
        }
        other => Err(PyValueError::new_err(format!(
            "unknown id_spec form {other:?}"
        ))),
    }
}

fn stop_to_py(py: Python<'_>, stop: producer::Stop) -> PyErr {
    match stop {
        producer::Stop::Parse(e) => gff_error_to_py(py, e),
        producer::Stop::Duplicate(id, line, first) => {
            let message = format!("Duplicate ID {id} (line {line}, first seen on line {first})");
            match py
                .import("gffbase.exceptions")
                .and_then(|m| m.getattr("DuplicateIDError"))
                .and_then(|cls| cls.call1((message,)))
            {
                Ok(err) => PyErr::from_value(err),
                Err(e) => e,
            }
        }
        producer::Stop::MultiValuedId(key) => PyValueError::new_err(format!(
            "The ID field {key} has more than one value but a single value is required for a \
             primary key in the database. Consider using a custom id_spec to convert these \
             multiple values into a single value"
        )),
    }
}

/// Memory the producer filled, handed to pyarrow without a copy:
/// `pa.foreign_buffer(buf.address, buf.size, base=buf)` keeps this object --
/// and so the memory -- alive for as long as any Arrow array refers to it.
/// Copying each batch into `bytes` first kept two copies of it alive at the
/// peak. Moving a `Vec` in here does not move its heap buffer, so `address`
/// stays valid; `frozen` means nothing can change it afterwards.
#[pyclass(frozen, name = "OwnedBuffer")]
struct OwnedBuffer {
    address: usize,
    size: usize,
    _data: Owned,
}

enum Owned {
    U8(Vec<u8>),
    I32(Vec<i32>),
    I64(Vec<i64>),
}

#[pymethods]
impl OwnedBuffer {
    #[getter]
    fn address(&self) -> usize {
        self.address
    }

    #[getter]
    fn size(&self) -> usize {
        self.size
    }
}

fn owned(py: Python<'_>, data: Owned) -> PyResult<Bound<'_, PyAny>> {
    let (address, size) = match &data {
        Owned::U8(v) => (v.as_ptr() as usize, v.len()),
        Owned::I32(v) => (v.as_ptr() as usize, std::mem::size_of_val(v.as_slice())),
        Owned::I64(v) => (v.as_ptr() as usize, std::mem::size_of_val(v.as_slice())),
    };
    Ok(Bound::new(
        py,
        OwnedBuffer {
            address,
            size,
            _data: data,
        },
    )?
    .into_any())
}

/// A string/binary column as `(kind, offsets, data)`: 32-bit offsets
/// (`str`, `bin`) whenever the data fits; 64-bit (`large_str`, `large_bin`)
/// only when it does not.
fn bytes_col<'py>(
    py: Python<'py>,
    kind: &str,
    b: producer::Bytes,
) -> PyResult<Bound<'py, PyTuple>> {
    let producer::Bytes { offsets, data } = b;
    let (kind, offsets) = if data.len() <= i32::MAX as usize {
        let narrow: Vec<i32> = offsets.iter().map(|&o| o as i32).collect();
        (kind.to_string(), Owned::I32(narrow))
    } else {
        (format!("large_{kind}"), Owned::I64(offsets))
    };
    PyTuple::new(
        py,
        [
            kind.into_pyobject(py)?.into_any(),
            owned(py, offsets)?,
            owned(py, Owned::U8(data))?,
        ],
    )
}

fn batch_to_py(py: Python<'_>, mut b: producer::Batch) -> PyResult<Bound<'_, PyDict>> {
    use std::mem::take;
    let d = PyDict::new(py);
    d.set_item("n", b.n)?;
    d.set_item("n_attributes", b.a_n)?;
    for (name, col) in [
        ("id", take(&mut b.id)),
        ("seqid", take(&mut b.seqid)),
        ("source", take(&mut b.source)),
        ("featuretype", take(&mut b.featuretype)),
        ("score", take(&mut b.score)),
        ("strand", take(&mut b.strand)),
        ("frame", take(&mut b.frame)),
        ("raw_id", take(&mut b.raw_id)),
        ("id_origin", take(&mut b.id_origin)),
        ("a_keys", take(&mut b.a_keys)),
        ("a_value", take(&mut b.a_value)),
    ] {
        d.set_item(name, bytes_col(py, "str", col)?)?;
    }
    for (name, col) in [
        ("attributes_blob", take(&mut b.attributes_blob)),
        ("extra_blob", take(&mut b.extra_blob)),
    ] {
        d.set_item(name, bytes_col(py, "bin", col)?)?;
    }
    for (name, col) in [("start", take(&mut b.start)), ("end", take(&mut b.end))] {
        let validity = if col.nulls > 0 {
            owned(py, Owned::U8(col.validity))?
        } else {
            py.None().into_bound(py)
        };
        d.set_item(
            name,
            (
                "i64",
                owned(py, Owned::I64(col.values))?,
                validity,
                col.nulls,
            ),
        )?;
    }
    for (name, col) in [
        ("file_order", take(&mut b.file_order)),
        ("seqid_y", take(&mut b.seqid_y)),
    ] {
        d.set_item(name, ("i64", owned(py, Owned::I64(col))?, py.None(), 0))?;
    }
    for (name, col) in [
        ("occ", take(&mut b.occ)),
        ("a_idx", take(&mut b.a_idx)),
        ("a_feature_row", take(&mut b.a_feature_row)),
        ("a_key", take(&mut b.a_key)),
    ] {
        d.set_item(name, ("i32", owned(py, Owned::I32(col))?))?;
    }
    let deferred = PyList::empty(py);
    for (fid, order, rec) in &b.deferred {
        deferred.append((fid.as_str(), *order, record_to_pytuple(py, rec)?))?;
    }
    d.set_item("deferred", deferred)?;
    d.set_item(
        "dropped",
        PyList::new(py, b.dropped.iter().map(String::as_str))?,
    )?;
    Ok(d)
}

/// Ingest rows as column buffers; see `producer.rs`.
#[pyclass(name = "IngestProducer")]
struct PyIngestProducer {
    inner: producer::Producer,
}

#[pymethods]
impl PyIngestProducer {
    #[new]
    #[pyo3(signature = (records, id_spec, strategy, gtf_keys, surrogate_sep, seqid_band, batch_size, autoinc_seed))]
    #[allow(clippy::too_many_arguments)]
    fn new(
        records: &Bound<'_, PyRecordIterator>,
        id_spec: &Bound<'_, PyAny>,
        strategy: &str,
        gtf_keys: Option<(String, String)>,
        surrogate_sep: String,
        seqid_band: i64,
        batch_size: usize,
        autoinc_seed: Vec<(String, i64)>,
    ) -> PyResult<Self> {
        let strategy = match strategy {
            "error" => producer::Strategy::Error,
            "warning" => producer::Strategy::Warning,
            "create_unique" => producer::Strategy::CreateUnique,
            "merge" | "replace" => producer::Strategy::Defer,
            "fuse" => producer::Strategy::Fuse,
            other => return Err(PyValueError::new_err(format!("unknown strategy {other:?}"))),
        };
        let iter = records
            .borrow_mut()
            .inner
            .take()
            .ok_or_else(|| PyValueError::new_err("the record iterator was already consumed"))?;
        let cfg = producer::Config {
            id_spec: id_spec_from_py(id_spec)?,
            strategy,
            gtf_keys,
            surrogate_sep,
            seqid_band,
            batch_size: batch_size.max(1),
        };
        Ok(PyIngestProducer {
            inner: producer::Producer::new(iter, cfg, autoinc_seed),
        })
    }

    /// The next batch as a dict of column buffers, or None at the end.
    fn next_batch<'py>(&mut self, py: Python<'py>) -> PyResult<Option<Bound<'py, PyDict>>> {
        match self.inner.next_batch() {
            Ok(Some(batch)) => Ok(Some(batch_to_py(py, batch)?)),
            Ok(None) => Ok(None),
            Err(stop) => Err(stop_to_py(py, stop)),
        }
    }

    /// (records read, records dropped, last file_order).
    fn counts(&self) -> (u64, u64, i64) {
        (
            self.inner.n_raw,
            self.inner.n_skipped,
            self.inner.file_order,
        )
    }

    /// Autoincrement counters, in the order their bases were first used.
    fn autoincrements(&self) -> Vec<(String, i64)> {
        self.inner
            .autoinc_order
            .iter()
            .map(|b| (b.clone(), self.inner.autoinc[b]))
            .collect()
    }

    /// seqid -> y band, in encounter order.
    fn seqid_map(&self) -> Vec<(String, i64)> {
        self.inner
            .seqid_order
            .iter()
            .map(|s| (s.clone(), self.inner.seqid_y[s]))
            .collect()
    }

    fn dialect(&self, py: Python<'_>) -> PyResult<Py<PyAny>> {
        dialect_to_pydict(py, self.inner.iter.dialect())
    }

    fn directives(&self, py: Python<'_>) -> PyResult<Py<PyAny>> {
        let list = PyList::empty(py);
        for d in self.inner.iter.directives() {
            list.append(d.as_str())?;
        }
        Ok(list.into_any().unbind())
    }

    fn warnings(&self, py: Python<'_>) -> PyResult<Py<PyAny>> {
        let list = PyList::empty(py);
        for w in self.inner.iter.warnings() {
            let d = PyDict::new(py);
            d.set_item("line_no", w.line_no)?;
            d.set_item("kind", w.kind.as_str())?;
            d.set_item("message", w.message.as_str())?;
            list.append(d)?;
        }
        Ok(list.into_any().unbind())
    }
}

#[pymodule]
fn _native(py: Python<'_>, m: &Bound<'_, PyModule>) -> PyResult<()> {
    m.add_function(wrap_pyfunction!(parse_file, m)?)?;
    m.add_function(wrap_pyfunction!(parse_bytes, m)?)?;
    m.add_function(wrap_pyfunction!(detect_dialect, m)?)?;
    m.add_class::<PyIngestProducer>()?;
    m.add_class::<OwnedBuffer>()?;
    m.add("__version__", public_version())?;
    // Phase 16 — expose the descriptive Python exception type.
    m.add("GFFFormatError", py.get_type::<GFFFormatError>())?;
    Ok(())
}
