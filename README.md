# GFFBase

A modernized, high-performance successor to [`gffutils`](https://github.com/daler/gffutils). Rust-accelerated GFF3/GTF parser with a DuckDB-backed storage engine and a drop-in Python API.

> Status: **Phase 7 — rebrand + routing remediation.** All 77 tests pass on the new namespace. See `PHASE7_REMEDIATION_REPORT.md` for performance changes.

## Highlights

- **Rust+PyO3 parser** with SIMD line/tab splitting, lazy URL-unescaping, and zero-copy hand-off to DuckDB via PyArrow.
- **DuckDB columnar storage** with a 7-table schema, set-based GTF gene/transcript synthesis (one `GROUP BY` per pass), recursive-CTE transitive closure, and per-seqid-banded R-tree spatial index.
- **Smart query routing**: `region()` picks R-tree vs. multi-column B-tree based on what's available; `children()` picks the materialized closure cache vs. a dynamic recursive CTE based on the corpus's true hierarchy depth.
- **Drop-in legacy API**: `FeatureDB`, `Feature`, `create_db`, `DataIterator`, `GFFWriter`, the full `merge_criteria` + `interfeatures` + `bed12` toolchain, plus a SQLite-compat `execute()` escape hatch and a `export_sqlite()` writer.
- **GENCODE-scale**: ingests the full human v45 basic GTF (~2.0 M lines) in 74 s in-memory / 227 s disk-backed, using ~1.1 GB peak RSS — **15.8× to 48× faster than legacy gffutils** (3582 s).

## Layout

```
gffbase/
├── pyproject.toml                # maturin build config
├── rust/                         # Rust extension (PyO3) — gffbase._native
│   ├── Cargo.toml
│   └── src/
│       ├── lib.rs                # PyO3 entrypoint
│       ├── parser.rs             # streaming GFF3/GTF parser
│       ├── attributes.rs         # column-9 state machine
│       ├── dialect.rs            # dialect detection
│       └── escape.rs             # GFF3 percent-encoding
├── python/
│   └── gffbase/
│       ├── __init__.py           # public surface
│       ├── feature.py            # Feature + ParsedFeature
│       ├── interface.py          # FeatureDB
│       ├── ingest.py             # Rust→Arrow→DuckDB pipeline
│       ├── schema.py             # DDL + set-based SQL constants
│       ├── parser.py             # native/python dispatcher
│       ├── create_db.py          # legacy create_db() shim
│       ├── iterators.py          # DataIterator
│       ├── gffwriter.py          # GFFWriter
│       ├── merge_criteria.py
│       ├── helpers.py
│       ├── sqlite_export.py      # legacy SQLite export
│       ├── exceptions.py
│       └── _pyfallback/          # pure-Python correctness oracle parser
├── tests/                        # pytest suite (77 tests)
├── bench/                        # GENCODE benchmark harness
└── docs/
```

## Building

```bash
pip install maturin
cd /path/to/gffbase
maturin develop --release        # installs gffbase._native into the active venv
```

## Running tests

```bash
pip install -e .[test]
pytest                           # 77 / 77
```

## Quick start

```python
from gffbase import create_db

db = create_db("annotations.gff3", ":memory:")
for f in db.children("ENSG00000123456", level=None, featuretype="exon"):
    print(f)
```

See `PHASE5_API_SUMMARY.md` for the full method-by-method legacy compatibility checklist.

## Benchmarking

```bash
cd /path/to/gffbase
python bench/benchmark.py --skip-legacy-full --reuse-db \
    --n-region 10000 --n-children 2000 --diff-lines 50000
```

Numbers from Phase 6 + Phase 7 are summarized in `PHASE6_BENCHMARK_REPORT.md` and `PHASE7_REMEDIATION_REPORT.md`.
