# GFFBase

**A Rust + DuckDB GFF3/GTF engine that's 17.83× faster to ingest GENCODE
than legacy `gffutils` and 36× faster at bulk ML queries.**

[![PyPI](https://img.shields.io/pypi/v/gffbase.svg)](https://pypi.org/project/gffbase/)
[![Python](https://img.shields.io/pypi/pyversions/gffbase.svg)](https://pypi.org/project/gffbase/)
[![License](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)
[![Tests](https://img.shields.io/badge/tests-385%20passing-brightgreen.svg)](#testing)
[![Coverage](https://img.shields.io/badge/coverage-95.3%25-brightgreen.svg)](#testing)

> **Battle-tested against GENCODE, RefSeq, MANE, and CHESS 3.** All
> four canonical human-genome annotations ingest cleanly through the
> Phase-16-hardened Rust parser with zero strict-mode warnings, including
> RefSeq's notorious duplicate-ID convention (multiple GFF3 rows sharing
> `ID=cds-NP_xxx`). gffbase mirrors `gffutils.merge_strategy="create_unique"`,
> suffixing repeats with `__N` and recording every remap in the
> `duplicates` table — robustness that just works on the messiest real
> NCBI files.

GFFBase is a drop-in successor to
[`gffutils`](https://github.com/daler/gffutils): a Rust+PyO3 parser
feeds a DuckDB columnar storage engine through a zero-copy PyArrow
hand-off, with a smart query router that picks an R-tree / B-tree
spatial index and a closure-cache / dynamic-CTE relational dispatcher
based on the corpus's true hierarchy depth. The full legacy
`FeatureDB` / `Feature` / `create_db` / `DataIterator` / `GFFWriter`
API is preserved.

---

## ⚡ Killer features

| Workload | gffbase | legacy gffutils | **Speedup** |
|---|---|---|---|
| GENCODE v45 full ingest (2.0 M lines) | **201 s** | 3 582 s (60 min) | **17.83×** |
| Spatial overlap (5 000 random regions) | **1 204 qps** | 164 qps | **7.34×** |
| Spatial query p50 latency | **0.72 ms** | 6.01 ms | **8.35×** lower |
| **Bulk ML `children_batched` (50 000 transcripts)** | **1.16 s** | 42.55 s | **🚀 36.68×** |
| 5 000-id batched lookup | 0.479 s | 5.81 s | 12.12× |
| Differential correctness | ✅ identical | — | (50 k features) |

### The Big Four — every canonical human annotation, head-to-head

| Corpus | Lines | gffbase ingest | legacy ingest | Spatial qps | 5 k-anchor batched |
|---|---:|---:|---:|---:|---:|
| **GENCODE v45** (GTF) | 2,001,750 | **3 min 22 s** | ≥ 30 min (15-min cap) | **1 204** | 172 ms / 596 k desc |
| **RefSeq GRCh38.p14** | 4,932,571 | 7 min 49 s | 6 min 5 s | **1 011** | 263 ms / 999 k desc |
| **MANE v1.5** | 524,834 | 44.9 s | 38.9 s | **1 766** | 73 ms / 156 k desc |
| **CHESS 3.1.3** | 2,761,061 | 3 min 17 s | 1 min 31 s | **1 175** | 79 ms / 161 k desc |

Spatial throughput stays in the **1,011 – 1,766 qps** band across every
dialect. Bulk batched extraction returns **5 k anchors → up to 1 M
descendants in under 270 ms** consistently. Numbers reproduced in
[`PERFORMANCE_COMPARISON.md`](PERFORMANCE_COMPARISON.md) — re-runnable
via `python benchmarks/06_mega.py --legacy-timeout 900`.

The 36.68× win at bulk ML scale is the core reason this library exists
— see [Machine Learning Workflows
Cookbook](docs/cookbooks/machine_learning_workflows.md).

## 🚀 Quick start

```bash
pip install gffbase
```

```python
from gffbase import create_db

# 1. Ingest a GTF/GFF3 in seconds (auto-detects format).
db = create_db("gencode.v45.basic.annotation.gtf.gz",
               "gencode.duckdb", force=True)

# 2. Walk a single gene's hierarchy.
for tx in db.children("ENSG00000139618", level=1, featuretype="transcript"):
    print(tx.id, tx.start, tx.end)

# 3. Spatial overlap query — uses per-seqid R-tree under the hood.
for f in db.region("chr17:43044295-43125483", featuretype="exon"):
    print(f)
```

### The killer ML pattern — vectorized PyArrow extraction

```python
# Pull every exon for 50 000 transcripts in a single call.
# Returns a zero-copy pyarrow.Table — no Python `Feature` objects ever
# constructed. 1.16 s wall on GENCODE v45 (vs 42.55 s for legacy).
exons = db.children_batched(
    transcript_ids,                # 50 000 IDs
    featuretype="exon",
    format="arrow",
)

# Hand off directly to Hugging Face datasets, PyTorch, polars, …
import torch
starts = torch.from_numpy(exons.column("start").to_numpy())
```

## ✨ What's inside

- **Rust + PyO3 parser** — SIMD line/tab splitting, lazy URL-decoding,
  GTF semicolon-in-quotes safe, gzipped input transparent.
- **DuckDB columnar storage** — 7-table schema, set-based GTF
  gene/transcript synthesis, recursive-CTE transitive closure,
  per-seqid-banded R-tree spatial index.
- **Smart routing** — `region()` auto-picks R-tree vs B-tree;
  `children()` auto-picks closure cache vs dynamic CTE.
- **Vectorized batched API** — `children_batched`, `parents_batched`,
  `region_batched` return `pyarrow.Table` / `pandas.DataFrame` /
  `polars.DataFrame` for direct ML pipeline hand-off.
- **Drop-in legacy API** — `FeatureDB`, `Feature`, `create_db`,
  `DataIterator`, `GFFWriter`, `merge_criteria`, `interfeatures`,
  `bed12`, `execute()` SQL escape hatch, `export_sqlite()`.
- **abi3 wheels** — single binary per arch covers CPython 3.9–3.13.

## 📚 Documentation

Full site (rendered with MkDocs Material): see
[`docs/`](docs/) or build it locally:

```bash
pip install -e .[docs]
mkdocs serve            # http://localhost:8000
```

Quick links:

- [Performance comparison](PERFORMANCE_COMPARISON.md) — head-to-head numbers + root-cause analysis
- [Migration guide for `gffutils` users](MIGRATION.md) — drop-in compat + the one OLAP gotcha
- [Cookbooks](docs/cookbooks/) — GENCODE/Ensembl, RefSeq, MANE, ML workflows
- [API reference](docs/api/) — every public method with signature + docstring

## 🧪 Testing

```bash
pip install -e .[test]
pytest                  # 385 passed, 2 skipped, ~95.3% line + branch coverage
```

CI runs the full matrix on Linux + macOS + Windows, both R-tree and
B-tree fallback paths, on Python 3.9 / 3.11 / 3.13.

## 📦 Installation from source

```bash
# Build the Rust extension into the active venv:
pip install maturin
maturin develop --release
```

Requires Rust ≥ 1.71.

## 🪪 License

MIT. See [`LICENSE`](LICENSE).

---

**Citation:** if GFFBase helps your research, please cite the project at
the [Releases page](https://github.com/your-org/gffbase/releases).
