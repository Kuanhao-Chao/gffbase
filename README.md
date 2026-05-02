# GFFBase

[![PyPI](https://img.shields.io/pypi/v/gffbase.svg)](https://pypi.org/project/gffbase/)
[![Python](https://img.shields.io/pypi/pyversions/gffbase.svg)](https://pypi.org/project/gffbase/)
[![License](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)
[![Tests](https://img.shields.io/badge/tests-386%20passing-brightgreen.svg)](#testing)
[![Coverage](https://img.shields.io/badge/coverage-95.46%25-brightgreen.svg)](#testing)
[![Battle-tested](https://img.shields.io/badge/battle--tested-GENCODE%20%7C%20RefSeq%20%7C%20MANE%20%7C%20CHESS%203-blue.svg)](#-the-big-four--battle-tested-across-every-canonical-human-annotation)

---

## What is GFFBase?

**GFFBase is a Rust + DuckDB drop-in successor to
[`gffutils`](https://github.com/daler/gffutils).** It ingests human-genome
annotation files (GTF/GFF3) up to **17.83× faster** than the legacy
library, runs spatial overlap queries at **1,000–1,800 qps** across every
dialect we tested, and pulls millions of features into a zero-copy
PyArrow buffer in a single call — **36.68× faster** than legacy on the
bulk ML extraction workloads that actually dominate modern genomics
pipelines.

A SIMD Rust+PyO3 parser feeds DuckDB's columnar storage through
record-batch Arrow handoffs. A smart query router auto-picks an
R-tree or B-tree spatial index per query, and a closure-cache /
recursive-CTE relational dispatcher selects the right strategy based
on the corpus's actual hierarchy depth. The full `FeatureDB` /
`Feature` / `create_db` / `DataIterator` / `GFFWriter` /
`merge_criteria` legacy API is preserved verbatim — most users
migrate by changing one import line.

---

## ⚡ The Big Four — battle-tested across every canonical human annotation

Phase 17 ran the full ingest + spatial + batched mega-bench against
all four canonical human-genome annotation sources, head-to-head with
legacy `gffutils`. Phase 19 then closed the lone remaining performance
gap (raw GFF3 ingest), so gffbase now wins **every ingest matchup
end-to-end**:

| Corpus                   | Format | Lines      | gffbase ingest | legacy ingest | **speedup**   | spatial qps | batched (5 k anchors) |
| ------------------------ | :----: | ---------: | -------------: | ------------: | ------------: | ----------: | --------------------: |
| **GENCODE v45** (basic)  |  GTF   |  2,001,750 |   **3 min 22 s** | ~60 min[^1]   | **17.83×**    |   **1,204** | 172 ms / 596 k desc   |
| **RefSeq GRCh38.p14**    |  GFF3  |  4,932,571 | **4 min 12 s**[^2] |   6 min 5 s   | **1.45×**     |   **1,011** | 263 ms / 999 k desc   |
| **MANE v1.5** (Ensembl)  |  GFF3  |    524,834 |    **21.6 s**  |    45.1 s     | **2.09×**     |   **1,766** |  78 ms / 156 k desc   |
| **CHESS 3.1.3**          |  GFF3  |  2,761,061 |    **53.6 s**  |  2 min 13.1 s | **2.48×**     |   **1,175** |  91 ms / 161 k desc   |

[^1]: Phase 11 measured the canonical legacy GENCODE ingest at **3,582 s (59 min 42 s)** with `disable_infer_genes=False`. Phase 17's safety-valve killed the legacy run at the 15-minute cap; the headline speedup uses the Phase-11 measurement.
[^2]: Phase-19 result — the same RefSeq used to take 7 min 49 s before the GFF3 ingest pipeline got autonomously re-architected.

**Robustness:** every corpus ingests cleanly with **zero strict-mode
warnings** from the Phase-16 NCBI-spec-hardened parser, including
RefSeq's notorious duplicate-`ID=cds-NP_xxx` convention (split CDS
segments). gffbase mirrors `gffutils.merge_strategy="create_unique"`
automatically and records the remap in the `duplicates` table — no
config knobs to flip.

📊 Full reproducible numbers + per-corpus root-cause analysis:
[`PERFORMANCE_COMPARISON.md`](PERFORMANCE_COMPARISON.md). Re-run via
`python benchmarks/06_mega.py --legacy-timeout 900`.

---

## 🚀 The Killer Feature — zero-copy PyArrow for ML pipelines

Modern ML genomics pipelines have one shape: **pull every exon for
50 000 transcripts, push the column-oriented table into a tensor,
train.** Legacy `gffutils` forces a per-feature Python loop —
constructing 1.6 M throwaway `Feature` objects per pull, which crushes
both wall time and memory. gffbase bypasses Python entirely with a
single batched call that returns DuckDB's internal Arrow buffers
directly:

```python
# 50 000 transcript IDs → every exon, in one query.
# Returns a zero-copy pyarrow.Table — no Python `Feature` object
# is constructed at any layer.
exons = db.children_batched(
    transcript_ids,
    featuretype="exon",
    format="arrow",        # or "df" / "polars"
)

# Hand off directly to PyTorch / Hugging Face datasets / JAX / Lance.
import torch
starts = torch.from_numpy(exons.column("start").to_numpy())
ends   = torch.from_numpy(exons.column("end").to_numpy())
# The "anchor" column carries the input id for each row, so you can
# reconstruct per-transcript groups without re-issuing N queries.
```

**Numbers for that one call** (50 000 transcripts, GENCODE v45,
returning 1.6 M exon rows):

| Path                                      |        Wall | vs legacy        |
| ----------------------------------------- | ----------: | ---------------- |
| gffbase `children_batched(format='arrow')`|   **1.16 s**| **36.68× faster**|
| legacy `gffutils` row-by-row loop         |     42.55 s | 1.0× (baseline)  |
| gffbase row-by-row loop                   |     ≥ 642 s | 0.07× *(slower!)*|

This is **the** reason GFFBase exists. Iterating
`for x in ids: db.children(x)` with DuckDB pays vectorization startup
per call and is *slower* than legacy's SQLite row-by-row path — but
the batched API obliterates both row-by-row paths because it issues
one set-based SQL query and avoids constructing any Python `Feature`
objects whatsoever.

`region_batched(...)` and `parents_batched(...)` have the same
zero-copy contract for spatial and parent workloads.

---

## 📦 Installation

```bash
pip install gffbase
```

Universal `abi3-py39` wheels — single binary per arch covers CPython
3.9 → 3.13. No Rust toolchain required at install time.

For source/dev installs (Rust ≥ 1.69 + maturin):

```bash
pip install -e .[dev]
maturin develop --release
```

---

## 🏃 Quick start — row-by-row (drop-in for `gffutils`)

```python
from gffbase import create_db

# 1. Ingest a GTF/GFF3 in seconds (auto-detects format, gzipped OK).
db = create_db("gencode.v45.basic.annotation.gtf.gz",
               "gencode.duckdb", force=True)

# 2. Walk a single gene's hierarchy.
for tx in db.children("ENSG00000139618", level=1, featuretype="transcript"):
    print(tx.id, tx.start, tx.end)

# 3. Spatial overlap query — uses the per-seqid R-tree under the hood.
for f in db.region("chr17:43044295-43125483", featuretype="exon"):
    print(f)
```

If you're migrating from `gffutils`, change one line:

```python
import gffbase as gffutils    # one-line alias migration
db = gffutils.create_db(...)  # everything else identical
```

(But please read the [Migration Guide](MIGRATION.md) first — it has
**one** important note about ML loops.)

---

## 🤖 Quick start — vectorized for ML

```python
from gffbase import FeatureDB

db = FeatureDB("gencode.duckdb")

# Pull every exon for 50 000 transcripts — one set-based SQL query.
exons = db.children_batched(
    transcript_ids,                # iterable of 50 000 IDs
    featuretype="exon",
    format="arrow",                # "df" / "polars" also supported
)
# exons is a pyarrow.Table sharing memory with DuckDB. No copies.

# Spatial: "for each ATAC-seq peak, find every overlapping CDS."
peaks = [("chr1", 100_000, 110_000), ("chr1", 200_000, 210_000), ...]
overlaps = db.region_batched(peaks, featuretype="CDS", format="arrow")
```

See the [Machine Learning Workflows
Cookbook](docs/cookbooks/machine_learning_workflows.md) for end-to-end
pipelines with PyTorch and Hugging Face `datasets`.

---

## ✨ What's inside

- **Rust + PyO3 parser** — SIMD line/tab splitting, lazy URL-decoding,
  GTF semicolon-in-quotes safe, gzipped input transparent. Phase-16
  hardened against the NCBI GFF3 spec (line-numbered
  `GFFFormatError`, strict / non-strict modes, 9 enforced rules).
- **DuckDB columnar storage** — 7-table schema, set-based GTF
  gene/transcript synthesis, recursive-CTE transitive closure,
  per-seqid-banded R-tree spatial index built inline during ingest
  (Phase-19 optimization).
- **Smart routing** — `region()` auto-picks R-tree vs B-tree;
  `children()` auto-picks closure cache vs dynamic CTE based on
  measured corpus depth.
- **Vectorized batched API** — `children_batched`, `parents_batched`,
  `region_batched` return `pyarrow.Table` / `pandas.DataFrame` /
  `polars.DataFrame` directly out of DuckDB's buffer pool.
- **Drop-in legacy API** — `FeatureDB`, `Feature`, `create_db`,
  `DataIterator`, `GFFWriter`, `merge_criteria`, `interfeatures`,
  `bed12`, `execute()` SQL escape hatch, `export_sqlite()`.
- **abi3 wheels** — single binary per arch covers CPython 3.9–3.13.

---

## 📚 Documentation

Full site (rendered with MkDocs Material) — build it locally:

```bash
pip install -e .[docs]
mkdocs serve            # http://localhost:8000
```

| Page                                                                | What's there                                                              |
| ------------------------------------------------------------------- | ------------------------------------------------------------------------- |
| [Performance comparison](PERFORMANCE_COMPARISON.md)                 | Big-Four head-to-head numbers + per-corpus root-cause analysis            |
| [Migration guide for `gffutils` users](MIGRATION.md)                | Drop-in compat checklist + the one OLAP/OLTP gotcha you must understand   |
| [Cookbooks](docs/cookbooks/)                                        | GENCODE/Ensembl, RefSeq, MANE, ML workflows                               |
| [API reference](docs/api/)                                          | Every public method, full signatures + docstrings                         |

---

## 🧪 Testing

```bash
pip install -e .[test]
pytest                  # 386 passed, 2 skipped, 95.46% coverage
```

CI runs the full matrix on Linux + macOS + Windows, both R-tree and
B-tree fallback paths, on Python 3.9 / 3.11 / 3.13.

---

## 🪪 License

MIT. See [`LICENSE`](LICENSE).

---

**Citation:** if GFFBase helps your research, please cite the project at
the [Releases page](https://github.com/your-org/gffbase/releases).
