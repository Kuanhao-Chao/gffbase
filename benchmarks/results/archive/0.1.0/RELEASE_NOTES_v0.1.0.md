# GFFBase v0.1.0 — First Public Release

**The fastest way to ingest, query, and stream genome-annotation
files into modern ML pipelines — purpose-built for whole-genome
scale and a drop-in successor to [`gffutils`](https://github.com/daler/gffutils).**

GFFBase is a high-performance GFF3 / GTF engine combining a SIMD
Rust + PyO3 parser, a DuckDB columnar storage backend, and a
zero-copy PyArrow interface. It preserves the entire `FeatureDB` /
`Feature` / `create_db` / `DataIterator` / `GFFWriter` /
`merge_criteria` legacy API verbatim, while adding a vectorized
batched API designed for tensor-shaped pulls into PyTorch /
Hugging Face / JAX / Lance.

This is the first public release. The engine has been validated
against every canonical human-genome annotation corpus, with
**zero strict-mode warnings** from the NCBI-spec-hardened parser.

---

## ⚡ Major Human Reference Annotations — head-to-head against legacy gffutils

Full reproducible numbers, hardware spec, and per-corpus root-cause
analysis: see [`PERFORMANCE_COMPARISON.md`](PERFORMANCE_COMPARISON.md).

| Corpus                   | Format | Lines      | gffbase ingest | legacy ingest | speedup       | spatial qps | batched (5 k anchors) |
| ------------------------ | :----: | ---------: | -------------: | ------------: | ------------: | ----------: | --------------------: |
| **GENCODE v49** (basic)  |  GTF   |  6,068,892 |   4 min 37 s   | ≥ 2 hr 30 min | **🚀 ≥ 32×**  |   **1,204** | 172 ms / 596 k desc   |
| **GENCODE v49** (basic)  |  GFF3  |  6,066,054 |   6 min 7 s    |  11 min 23 s  | **1.86×**     |   **1,292** | 422 ms / 1.93 M desc  |
| **RefSeq GRCh38.p14**    |  GFF3  |  4,932,571 |   4 min 12 s   |   6 min 5 s   | **1.45×**     |   **1,011** | 263 ms / 999 k desc   |
| **MANE v1.5** (Ensembl)  |  GFF3  |    524,834 |    21.6 s      |    45.1 s     | **2.09×**     |   **1,766** |  78 ms / 156 k desc   |
| **CHESS 3.1.3**          |  GFF3  |  2,761,061 |    53.6 s      |  2 min 13.1 s | **2.48×**     |   **1,175** |  91 ms / 161 k desc   |

The headline ≥ 32× GENCODE-GTF speedup comes from replacing
legacy's millions of Python ↔ SQLite round-trips with set-based
DuckDB `GROUP BY` synthesis + a recursive-CTE closure pass —
gene/transcript inference is now a single SQL query, not a
per-feature loop. RefSeq's split-CDS duplicate-`ID=cds-NP_xxx`
convention is handled transparently via the `duplicates` table.

---

## 🚀 The killer feature — zero-copy PyArrow for ML pipelines

Modern ML genomics has one shape: pull every exon for tens of
thousands of transcripts, push the column-oriented table into a
tensor, train. Legacy `gffutils` forces a per-feature Python loop —
constructing 1.6 M throwaway `Feature` objects per pull, which
crushes both wall time and memory.

GFFBase bypasses Python entirely:

```python
from gffbase import FeatureDB

db = FeatureDB("gencode.duckdb")

exons = db.children_batched(
    transcript_ids,  # 50 000 IDs
    featuretype="exon",
    format="arrow",  # zero-copy pyarrow.Table
)

import torch

starts = torch.from_numpy(exons.column("start").to_numpy())
ends = torch.from_numpy(exons.column("end").to_numpy())
```

| Path                                        | Wall on 50 k transcripts | vs legacy        |
| ------------------------------------------- | -----------------------: | ---------------- |
| `db.children_batched(format='arrow')`       |              **1.16 s**  | **36.68× faster**|
| legacy `gffutils` row-by-row loop           |                  42.55 s | 1.0×             |
| gffbase row-by-row loop                     |                   ≥ 642 s| 0.07× *(slower!)*|

Output formats: `'arrow'` (zero-copy `pyarrow.Table`),
`'pandas'`, `'polars'`. Hand off directly to PyTorch /
Hugging Face / JAX / Lance — no Python `Feature` objects, ever.

See the [Machine Learning Workflows Cookbook](https://gffbase.khchao.com/cookbooks/machine_learning_workflows/)
for end-to-end pipelines.

---

## 📦 Installation

```bash
pip install gffbase
```

Universal **abi3-py39** wheels — one binary per arch covers
CPython 3.9 → 3.13. Manylinux x86_64 + aarch64, macOS Intel +
Apple Silicon, Windows x86_64.

```python
from gffbase import create_db

db = create_db("gencode.v49.basic.annotation.gtf.gz", "gencode.duckdb", force=True)
for tx in db.children("ENSG00000139618", level=1, featuretype="transcript"):
    print(tx.id, tx.start, tx.end)
```

---

## 🛠️ Migration from gffutils — drop-in compatibility, one OLAP gotcha

The full legacy API is preserved verbatim: `FeatureDB`, `Feature`,
`create_db`, `DataIterator`, `GFFWriter`, `merge_criteria`,
`bed12()`, `execute()`, plus a one-call `db.export_sqlite()` for
downstream tools that still expect the legacy SQLite schema.

**The one breaking-ish change:** under the hood, the storage engine
is now DuckDB (an OLAP columnar database), not SQLite. In practice
this is invisible — every public method works identically — but
two corner cases differ:

- **Concurrent write transactions across processes are not
  supported.** DuckDB is a single-writer database (multiple
  readers OK). Pipelines that opened multiple `gffutils.FeatureDB`
  instances with overlapping write sessions will need to serialize.
- **Raw SQL passed to `db.execute()` must be DuckDB-compatible
  SQL, not SQLite SQL.** The two dialects overlap heavily, but
  e.g. `INSERT OR REPLACE` becomes `INSERT … ON CONFLICT DO UPDATE`,
  and SQLite-specific `pragma` statements are silently ignored.
  Most users never call `db.execute()` directly.

For users with an existing legacy `.sqlite` database who want to
hand it off to a downstream tool while still upgrading their own
pipeline: open the new `.duckdb` and call `db.export_sqlite("out.sqlite")`.
Full migration guide:
[`MIGRATION.md`](https://github.com/Kuanhao-Chao/gffbase/blob/main/MIGRATION.md)
or [docs](https://gffbase.khchao.com/migration/).

---

## 🧪 Quality gates

- **523 tests passing, 7 skipped** (polars-only paths).
- **99.19 % branch coverage**, gated at ≥ 99 % in CI.
- **`mkdocs build --strict`** clean.
- **`ruff` clean** on the entire Python source.
- **Cross-platform CI matrix:** Ubuntu / macOS / Windows × Python
  3.9 / 3.11 / 3.13 × R-tree-on / R-tree-off (B-tree fallback).
- **NCBI strict-mode parser** — every GENCODE / RefSeq / MANE /
  CHESS 3 corpus ingests with zero strict-mode warnings.

---

## 📚 Documentation

- **Site:** https://gffbase.khchao.com/
- **Usage gallery:** copy-pasteable snippets for every public method.
- **Cookbooks:** GENCODE/Ensembl, NCBI RefSeq, MANE, ML workflows.
- **API reference:** every public method, full signatures + docstrings.
- **Performance:** the v0.1.0 ingest-optimization story, end to end.

---

## 🤝 Contributing

External contributions welcome — see
[`CONTRIBUTING.md`](https://github.com/Kuanhao-Chao/gffbase/blob/main/CONTRIBUTING.md)
for development setup (Rust ≥ 1.69, Python 3.9–3.13,
`maturin develop --release`), the test-and-coverage gates, and the
full PR checklist. Bug reports and feature requests use issue
templates under
[`.github/ISSUE_TEMPLATE/`](https://github.com/Kuanhao-Chao/gffbase/tree/main/.github/ISSUE_TEMPLATE).

---

## 🪪 License

[Apache License 2.0](https://github.com/Kuanhao-Chao/gffbase/blob/main/LICENSE).

---

## 🙏 Acknowledgments

GFFBase stands on the shoulders of `gffutils` (Ryan Dale et al.) —
the design of the `FeatureDB` / `Feature` / `create_db` API is
preserved verbatim out of respect for the years of pipelines built
on top of it. The performance work focuses on the storage and
parsing layers underneath, not on changing what the API looks like.

---

**Full changelog will be added inline when this draft is published.**
For now, this is the inaugural cut from `main`.
