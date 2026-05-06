---
hide:
  - navigation
  - toc
title: GFFBase
---

# GFFBase

[![PyPI](https://img.shields.io/pypi/v/gffbase.svg)](https://pypi.org/project/gffbase/)
[![Python](https://img.shields.io/pypi/pyversions/gffbase.svg)](https://pypi.org/project/gffbase/)
[![License](https://img.shields.io/badge/license-Apache%202.0-blue.svg)](https://github.com/your-org/gffbase/blob/main/LICENSE)
[![Tests](https://img.shields.io/badge/tests-507%20passing-brightgreen.svg)](#testing)
[![Coverage](https://img.shields.io/badge/coverage-98.01%25-brightgreen.svg)](#testing)
[![Validated](https://img.shields.io/badge/validated-GENCODE%20%7C%20RefSeq%20%7C%20MANE%20%7C%20CHESS%203-blue.svg)](#comprehensive-human-genome-annotations-validated-across-every-canonical-corpus)

---

## What is GFFBase?

**GFFBase is a high-performance genomic-annotation engine combining a
SIMD Rust parser, a DuckDB columnar backend, and a zero-copy PyArrow
interface — purpose-built for whole-genome-scale ingest and bulk
machine-learning feature extraction, while remaining a drop-in
successor to [`gffutils`](https://github.com/daler/gffutils).**

A SIMD Rust+PyO3 parser feeds DuckDB's columnar storage through
record-batch Arrow handoffs. A smart query router auto-picks an
R-tree or B-tree spatial index per query, and a closure-cache /
recursive-CTE relational dispatcher selects the right strategy based
on the corpus's actual hierarchy depth. The full `FeatureDB` /
`Feature` / `create_db` / `DataIterator` / `GFFWriter` /
`merge_criteria` legacy API is preserved verbatim.

### Three reasons it matters

1. **🚀 ≥ 32× faster GENCODE GTF ingest** (v49, 6.07 M lines) —
   set-based DuckDB `GROUP BY` synthesis + recursive-CTE closure
   replaces legacy's millions of Python ↔ SQLite round-trips
   spent inventing the missing gene/transcript rows.
2. **⚡ 36.68× faster bulk ML extraction** — `children_batched(format='arrow')`
   returns 50 000 transcripts → 1.6 M exons as a zero-copy PyArrow
   table in **1.16 s**. No Python `Feature` objects, ever.
3. **🛡️  Validated NCBI compliance** — all four canonical human-genome
   annotations (GENCODE / RefSeq / MANE / CHESS 3) ingest cleanly with
   **zero strict-mode warnings**. RefSeq's split-CDS duplicate-ID
   convention is handled automatically.

---

## ⚡ Comprehensive Human Genome Annotations — validated across every canonical corpus

Head-to-head benchmark against legacy gffutils on the four canonical
human-genome annotation sources, with the v0.1.0 GFF3 ingest pipeline
optimizations applied:

| Corpus                   | Format | Lines      | gffbase ingest | legacy ingest | speedup       | spatial qps | batched (5 k anchors) |
| ------------------------ | :----: | ---------: | -------------: | ------------: | ------------: | ----------: | --------------------: |
| **GENCODE v49** (basic)  |  GTF   |  6,068,892 |   4 min 37 s   | ≥ 2 hr 30 min | **🚀 ≥ 32×**  |   **1,204** | 172 ms / 596 k desc   |
| **GENCODE v49** (basic)  |  GFF3  |  6,066,054 |   6 min 7 s    |  11 min 23 s  | **1.86×**     |   **1,292** | 422 ms / 1.93 M desc  |
| **RefSeq GRCh38.p14**    |  GFF3  |  4,932,571 |   4 min 12 s   |   6 min 5 s   | **1.45×**     |   **1,011** | 263 ms / 999 k desc   |
| **MANE v1.5** (Ensembl)  |  GFF3  |    524,834 |    21.6 s      |    45.1 s     | **2.09×**     |   **1,766** |  78 ms / 156 k desc   |
| **CHESS 3.1.3**          |  GFF3  |  2,761,061 |    53.6 s      |  2 min 13.1 s | **2.48×**     |   **1,175** |  91 ms / 161 k desc   |

Every corpus ingests with **zero strict-mode warnings** from the
NCBI-spec-hardened Rust parser. RefSeq's duplicate-`ID=cds-NP_xxx`
convention (split CDS segments) is handled transparently via the
`duplicates` table. Full reproducible numbers + per-corpus root-cause
analysis: see [Performance Comparison](performance.md).

---

## 🚀 The Killer Feature — zero-copy PyArrow for ML pipelines

Modern ML genomics pipelines have one shape: pull every exon for
50 000 transcripts, push the column-oriented table into a tensor,
train. Legacy `gffutils` forces a per-feature Python loop —
constructing 1.6 M throwaway `Feature` objects per pull, which
crushes both wall time and memory. gffbase bypasses Python entirely:

```python
exons = db.children_batched(
    transcript_ids,                # 50 000 IDs
    featuretype="exon",
    format="arrow",                # zero-copy pyarrow.Table
)

import torch
starts = torch.from_numpy(exons.column("start").to_numpy())
ends   = torch.from_numpy(exons.column("end").to_numpy())
```

| Path                                        | Wall on 50 k transcripts | vs legacy        |
| ------------------------------------------- | -----------------------: | ---------------- |
| `db.children_batched(format='arrow')`       |              **1.16 s**  | **36.68× faster**|
| legacy `gffutils` row-by-row loop           |                  42.55 s | 1.0×             |
| gffbase row-by-row loop                     |                   ≥ 642 s| 0.07× *(slower!)*|

This is **the** reason GFFBase exists. See the [Machine Learning
Workflows Cookbook](cookbooks/machine_learning_workflows.md) for
end-to-end pipelines.

---

## 📦 Installation

```bash
pip install gffbase
```

Universal `abi3-py39` wheels — one binary per arch covers CPython
3.9 → 3.13.

---

## 🏃 Quick start (row-by-row)

```python
from gffbase import create_db

db = create_db("gencode.v49.chr_patch_hapl_scaff.basic.annotation.gtf.gz",
               "gencode.duckdb", force=True)

for tx in db.children("ENSG00000139618", level=1, featuretype="transcript"):
    print(tx.id, tx.start, tx.end)

for f in db.region("chr17:43044295-43125483", featuretype="exon"):
    print(f)
```

## 🤖 Quick start (vectorized for ML)

```python
from gffbase import FeatureDB

db = FeatureDB("gencode.duckdb")
exons = db.children_batched(transcript_ids, featuretype="exon", format="arrow")

# Hand off to PyTorch / Hugging Face / JAX / Lance — no Python copies.
```

---

## ✨ What's inside

- **Rust + PyO3 parser** — SIMD splitting, lazy URL-decoding,
  GTF semicolon-in-quotes safe, gzipped input transparent. Hardened
  against the NCBI GFF3 spec.
- **DuckDB columnar storage** — set-based GTF synthesis,
  recursive-CTE closure, per-seqid-banded R-tree built inline during
  ingest.
- **Smart routing** — R-tree / B-tree spatial; closure-cache /
  dynamic-CTE relational.
- **Vectorized batched API** — `pyarrow.Table` / `pandas.DataFrame` /
  `polars.DataFrame`, directly out of DuckDB's buffer pool.
- **Drop-in legacy API** — `FeatureDB`, `Feature`, `create_db`,
  `DataIterator`, `GFFWriter`, `merge_criteria`, `bed12`,
  `execute()`, `export_sqlite()`.
- **abi3 wheels** — single binary per arch covers CPython 3.9–3.13.

---

## 📚 Where to next

| Page | What's there |
|---|---|
| [Usage Gallery](usage_gallery.md) | Copy-pasteable snippets for every public API method |
| [Performance](performance.md) | Head-to-head numbers across every canonical human-genome annotation + the v0.1.0 ingest optimization story |
| [Migration from gffutils](migration.md) | Drop-in compatibility + the one OLAP gotcha |
| [Cookbooks](cookbooks/index.md) | GENCODE/Ensembl, RefSeq, MANE, ML workflows |
| [API Reference](api/index.md) | Every public method, full signatures + docstrings |

---

## 🧪 Testing

```bash
pip install -e .[test]
pytest                  # 507 passed, 4 skipped, 98.01% coverage
```

CI runs the full matrix on Linux + macOS + Windows, both R-tree and
B-tree fallback paths, on Python 3.9 / 3.11 / 3.13.

## 🪪 License

Apache License 2.0.
