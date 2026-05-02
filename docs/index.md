---
hide:
  - navigation
  - toc
title: GFFBase
---

# GFFBase

**A Rust + DuckDB GFF3/GTF engine that's 15× faster to ingest GENCODE
than legacy `gffutils` and 36× faster at bulk ML queries.**

[![PyPI](https://img.shields.io/pypi/v/gffbase.svg)](https://pypi.org/project/gffbase/)
[![Python](https://img.shields.io/pypi/pyversions/gffbase.svg)](https://pypi.org/project/gffbase/)
[![License](https://img.shields.io/badge/license-MIT-blue.svg)](https://github.com/your-org/gffbase/blob/main/LICENSE)
[![Tests](https://img.shields.io/badge/tests-317%20passing-brightgreen.svg)](#testing)
[![Coverage](https://img.shields.io/badge/coverage-95.4%25-brightgreen.svg)](#testing)

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
| GENCODE v45 full ingest (2.0 M lines) | **226 s** | 3 582 s (60 min) | **15.87×** |
| Spatial overlap (5 000 random regions) | **852 qps** | 164 qps | **5.20×** |
| Spatial query p50 latency | **0.72 ms** | 6.01 ms | **8.35×** lower |
| **Bulk ML `children_batched` (50 000 transcripts)** | **1.16 s** | 42.55 s | 🚀 **36.68×** |
| 5 000-id batched lookup | 0.479 s | 5.81 s | 12.12× |

Numbers reproduced in [Performance Comparison](performance.md).

The 36.68× win at bulk ML scale is the core reason this library exists
— see the [Machine Learning Workflows
Cookbook](cookbooks/machine_learning_workflows.md).

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

## 📚 Where to next

| Page | What's there |
|---|---|
| [Performance](performance.md) | Head-to-head benchmark numbers + root-cause analysis for every metric |
| [Migration from gffutils](migration.md) | Drop-in compatibility checklist + the one OLAP gotcha you must understand |
| [Cookbooks](cookbooks/index.md) | GENCODE/Ensembl, RefSeq, MANE, ML workflows |
| [API Reference](api/index.md) | Every public method with full signatures and docstrings |

## 🧪 Testing

```bash
pip install -e .[test]
pytest                  # 317 passed, ~95.4% line + branch coverage
```

CI runs the full matrix on Linux + macOS + Windows, both R-tree and
B-tree fallback paths, on Python 3.9 / 3.11 / 3.13.

## 🪪 License

MIT.
