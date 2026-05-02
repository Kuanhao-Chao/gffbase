# Phase 3 — Parser Scaffolding Summary

**Scope.** Project workspace, Rust+PyO3 parser, pure-Python fallback parser, and a differential test harness. **No DuckDB ingestion.** The parser yields fully-populated in-memory records; storage arrives in Phase 4.

**Status.** Parser scaffolding complete. 20 pytest assertions pass on both engines (Rust + Python fallback) on the included GFF3 and GTF fixtures. 8 native Rust unit tests pass.

---

## 1. Repository Structure

```
gff/
├── gffutils/                          # untouched original library (read-only reference)
└── gffutils2/                         # new successor package (Phase 3 product)
    ├── pyproject.toml                 # maturin build backend
    ├── README.md
    ├── .gitignore
    ├── PHASE3_PARSER_SUMMARY.md       # this document
    ├── rust/                          # Rust crate, compiled to gffutils2._native
    │   ├── Cargo.toml
    │   ├── Cargo.lock                 # pinned for rustc 1.69 compat (see §5)
    │   └── src/
    │       ├── lib.rs                 # PyO3 module entrypoint
    │       ├── dialect.rs             # Dialect struct + plurality vote
    │       ├── attributes.rs          # Column-9 state-machine parser
    │       ├── escape.rs              # GFF3 percent-decoding (zero-copy fast path)
    │       └── parser.rs              # Streaming line/tab parser, dialect peek
    ├── python/
    │   └── gffutils2/
    │       ├── __init__.py            # Public surface
    │       ├── feature.py             # ParsedFeature dataclass (Phase 3 in-memory shape)
    │       ├── dialect.py             # Pure-Python dialect template
    │       ├── parser.py              # Engine dispatcher (auto / rust / python)
    │       └── _pyfallback/           # Pure-Python correctness oracle
    │           ├── __init__.py
    │           ├── parser.py
    │           └── attributes.py
    ├── tests/
    │   ├── conftest.py                # parametrizes every test on engine ∈ {python, rust}
    │   ├── data/
    │   │   ├── simple.gff3            # 5-feature GFF3 with %-escapes & multi-value attrs
    │   │   └── simple.gtf             # 3-feature GTF with quoted vals, semi-in-quotes
    │   ├── test_parser_basics.py      # 10 assertions × 2 engines = 20 cases
    │   └── test_engine_equivalence.py # Differential: rust output == python output
    ├── bench/                         # populated in Phase 4+
    └── docs/
```

The Rust crate compiles to a single shared library that Python imports as `gffutils2._native`. The Python-only fallback under `gffutils2._pyfallback` is identical in surface (yields `ParsedFeature`, exposes `.dialect()` and `.directives()`).

---

## 2. Build Instructions

### One-time toolchain prep

```bash
# Install Rust if missing
curl --proto '=https' --tlsv1.2 -sSf https://sh.rustup.rs | sh

# Install maturin (PyO3 build helper)
pip install maturin
```

PyO3 0.22's transitive dependencies require **rustc ≥ 1.71**. The crate currently builds on **rustc 1.69** thanks to pinned versions in `Cargo.lock` (see §5). For a fresh CI environment, run `rustup update stable` first.

### Build & install in development mode

```bash
cd /Users/chaokuan-hao/Documents/Projects/gff/gffutils2
maturin develop --release
```

`maturin develop` builds the `.so`, installs the `gffutils2` package in editable mode, and links the compiled extension at `gffutils2/_native.*.so` inside the active interpreter's site-packages. Subsequent edits to Python files take effect immediately; Rust edits require re-running `maturin develop --release`.

### Verifying the install

```bash
python -c "from gffutils2 import native_available; print(native_available())"
# -> True  (False if the extension didn't build)
```

### Running the test suite

```bash
pip install pytest
cd /Users/chaokuan-hao/Documents/Projects/gff/gffutils2
pytest
```

Every test runs once against the Rust engine and once against the Python fallback. The fallback is always present; Rust tests are skipped automatically if the extension isn't built.

```bash
# Rust unit tests (independent of Python)
cd rust && cargo test
```

---

## 3. Architecture of the Parser

### 3.1 Rust crate (`gffutils2-core`)

The crate is structured around four small modules:

| Module | Responsibility |
|---|---|
| `parser.rs` | File I/O, line splitting (memchr-driven), tab splitting, dialect peek, `Iterator<Item=Result<Record, String>>` |
| `attributes.rs` | Column-9 state-machine parser. Splits on top-level `;`, handles quoted values, multi-value GFF3 commas, repeated keys, leading/trailing semicolons |
| `escape.rs` | GFF3 percent-decoding. Returns `Cow::Borrowed` when no `%` byte is present (zero-copy fast path) |
| `dialect.rs` | `Dialect` struct + `choose()` reconciliation across peeked samples (plurality vote on separators, OR for boolean flags, first-appearance order for keys) |
| `lib.rs` | PyO3 module entrypoint: `parse_file`, `parse_bytes`, `detect_dialect`, plus `PyRecordIterator` exposing `__iter__`/`__next__`/`dialect()`/`directives()` |

**Streaming model.** The parser materializes the input into a single `Vec<u8>` (gzip-decompressed if `.gz`) and walks forward via a moving `pos` cursor. `memchr::memchr(b'\n', ...)` finds newlines at SIMD speed. Tab fields are split with a hand-written byte-loop. The dialect peek phase parses up to `checklines=10` features, snapshots their `pos`, then *resets* the cursor so iteration starts from the top.

**Yielding to Python.** Each `Record` becomes an 11-element `PyTuple`:

```
(seqid, source, featuretype, start, end, score, strand, frame,
 attributes_blob: bytes, attributes_pairs: list[(key, value, idx)], extra: list[str])
```

`attributes_blob` preserves the **original col-9 bytes** — required for the byte-faithful round-trip invariant from Phase 1 §3.9. `attributes_pairs` is the long-form decomposition that Phase 4 will bulk-load into the DuckDB `attributes` table.

### 3.2 Python fallback (`gffutils2._pyfallback`)

Mirrors the Rust crate's surface and behavior 1:1 — same iterator API, same dialect dict, same `(key, value, idx)` triple shape. Uses `urllib.parse.unquote` for percent decoding and a hand-written semicolon-aware splitter that matches the Rust state machine. Acts as both:

1. **Correctness oracle.** `tests/test_engine_equivalence.py` asserts the two engines produce byte-identical output on every fixture.
2. **Fallback runtime.** Pure-Python users who can't compile the extension still get a working parser, slower but identical in behavior.

### 3.3 Public dispatcher (`gffutils2.parser`)

```python
from gffutils2 import parse_gff

it = parse_gff("annotations.gff3")            # auto: Rust if available, else Python
for f in it: ...

parse_gff("annotations.gff3", engine="python")  # force fallback
parse_gff("annotations.gff3", engine="rust")    # force native (raises if not built)
```

Both engines return `_Iterator` which yields `ParsedFeature` (slotted dataclass) and exposes `.dialect()` and `.directives()`.

---

## 4. Quirks and Edge Cases Handled

The two engines agree on:

| Edge case | Handling |
|---|---|
| GFF3 percent-encoding (`%20`, `%2C`, …) | Decoded; unknown `%XX` falls through unchanged |
| Multi-value GFF3 (`Parent=a,b,c`) | Three rows with `idx ∈ {0,1,2}` |
| GTF quoted values (`gene_id "ENSG"`) | Quotes stripped, `quoted GFF2 values=True` flagged |
| Semicolons inside quotes (`note "a;b";ID=x`) | Not treated as separators; flagged on dialect |
| Leading/trailing semicolons | Detected, recorded on dialect for round-trip |
| `; ` vs `;` field separator | Plurality vote across peeked samples |
| Windows line endings (`\r\n`) | `\r` trimmed before tab split |
| `.` in start/end | Becomes `None` |
| Extra columns past col 9 | Captured as `extra: list[str]` |
| Comments (`#`, but not `##`) | Skipped silently |
| Directives (`##gff-version 3`, `##sequence-region`) | Collected on the iterator |
| `##FASTA` sentinel | Halts feature emission |
| Gzipped input (`.gff3.gz`) | Auto-detected by extension; decompressed via `flate2::MultiGzDecoder` |

Behaviors deliberately deferred to Phase 4:
- `merge_strategy` (handled by the ingestion layer, not the parser).
- `id_spec` callable resolution.
- Synthetic gene/transcript inference for GTF.
- `transform` callback support.

---

## 5. Technical Hurdles at the Rust↔Python Boundary

### 5.1 Module-name mismatch (resolved)

`pyproject.toml` declares `module-name = "gffutils2._native"`. PyO3 derives the expected init symbol from the module name's last component, so the `#[pymodule] fn ...` and the Cargo `[lib].name` both have to be `_native`. First build emitted:

```
⚠️ Couldn't find the symbol `PyInit__native` in the native library.
```

Fix: `[lib].name = "_native"` in `Cargo.toml` and `#[pymodule] fn _native(...)` in `lib.rs`.

### 5.2 Borrow checker conflict in the read loop (resolved)

The read loop borrowed `self.buf` immutably to slice out the next line, then tried to format an error message that read `self.line_no`. The borrow checker rejected both the increment and the read while the slice was alive (E0503, E0502). Two fixes:

1. Move `line_no += 1` *into* `read_line()` so the increment happens before the borrow is returned.
2. Materialize the line into an owned `Vec<u8>` (`slice.to_vec()`) so we drop the borrow on `self.buf` before touching other `&self` fields.

The cost (one allocation per line) is dwarfed by parsing work; for hot paths in Phase 4 we can revisit by restructuring the iterator to never re-borrow `self`.

### 5.3 Iterator state after exhaustion (resolved)

Initial design nulled out `self.inner` on `StopIteration` to free memory. This broke `it.directives()` and `it.dialect()` calls *after* the iterator was drained — a common pattern (`list(it); print(it.directives())`). Fix: keep `inner` alive; rely on Python GC to free it when the wrapper is dropped.

### 5.4 PyO3 0.22 + rustc 1.69 (resolved with version pins)

PyO3 0.22.6's transitive dependencies (`quote 1.0.45`, `unicode-ident 1.0.24`) require rustc 1.71+. The local dev environment has rustc 1.69. Pinned `Cargo.lock` to compatible versions:

```
quote          = 1.0.39
proc-macro2    = 1.0.94
syn            = 2.0.96
unicode-ident  = 1.0.13
libc           = 0.2.155
```

For users on a current toolchain (1.71+), `cargo update` will bump these to latest; CI should run `rustup update stable` before building.

### 5.5 PyO3 0.22 API changes (no migration needed yet)

The crate uses the new `Bound<'_, ...>` API uniformly (`PyList::empty_bound`, `PyTuple::new_bound`, `PyDict::new_bound`). Phase 4's API layer will need the same treatment when constructing user-facing Python objects.

---

## 6. Verification Performed

### 6.1 Rust unit tests

```
$ cd rust && cargo test --lib
running 8 tests
test attributes::tests::gff3_simple ... ok
test attributes::tests::gff3_multivalue ... ok
test attributes::tests::gtf_quoted ... ok
test attributes::tests::semicolon_in_quotes ... ok
test attributes::tests::percent_escapes ... ok
test escape::tests::passthrough_when_no_percent ... ok
test escape::tests::decodes_percent_escapes ... ok
test escape::tests::malformed_escapes_pass_through ... ok
test result: ok. 8 passed; 0 failed
```

### 6.2 Python integration tests (both engines)

```
$ pytest
....................                                                     [100%]
20 passed in 0.42s
```

10 test functions × 2 engines = 20 cases. The differential equivalence tests (`test_engine_equivalence.py`) confirm every parsed `(key, value, idx)` triple and every `attributes_blob` is byte-identical between engines on the GFF3 and GTF fixtures.

### 6.3 Manual smoke test on the GTF fixture

```
exon 100  200  {'gene_id': ['ENSG1'], 'transcript_id': ['ENST1'], 'exon_number': ['1']}
exon 300  500  {'gene_id': ['ENSG1'], 'transcript_id': ['ENST1'], 'exon_number': ['2']}
exon 1000 1500 {'gene_id': ['ENSG2'], 'transcript_id': ['ENST2'], 'note': ['weird; with semi']}

dialect: {'fmt': 'gtf', 'field separator': '; ', 'keyval separator': ' ',
          'quoted GFF2 values': True, 'semicolon in quotes': True, ...}
```

Note the literal `;` inside the `note` value survived attribute splitting, and the dialect correctly flagged `semicolon in quotes`.

---

## 7. What's Intentionally NOT in Phase 3

Per the directive, the following are deferred:

- **DuckDB integration.** No backend wiring. The parser yields `ParsedFeature` objects; nothing persists.
- **Backwards-compat `Feature` / `FeatureDB` classes.** `ParsedFeature` is a Phase-3-only shape. Phase 4 introduces the full Phase-1-§3 API surface.
- **Synthetic gene/transcript inference.** Belongs to the GTF ingestion stage, not the parser.
- **Performance benchmarks against GENCODE-scale input.** `bench/` exists but is empty.
- **CI configuration.** Local builds work; GitHub Actions wiring is a Phase 5 concern.

---

## 8. Phase 4 Hand-off

When approved, Phase 4 begins by:

1. Vendoring DuckDB into the Python package (or pinning `duckdb>=1.0` as a runtime dep).
2. Implementing the schema from `PHASE2_ARCHITECTURE_PROPOSAL.md` §4.1 (`features`, `attributes`, `edges`, `closure`, etc.).
3. Building `gffutils2.ingest.from_file(path) -> FeatureDB` that streams the Rust parser's output into DuckDB via Arrow `RecordBatch` (or `executemany` as a first step).
4. Materializing the closure with the recursive CTE (proposal §3.1).
5. Synthesizing GTF gene/transcript rows via two `GROUP BY` queries (proposal §3.2).
6. Wiring up `FeatureDB`, `Feature`, `DataIterator`, `GFFWriter`, and the compat shim.

**Stopping here. Awaiting your review of the parser scaffolding before Phase 4 implementation begins.**
