# GFFBase

[![PyPI version](https://img.shields.io/pypi/v/gffbase.svg)](https://pypi.org/project/gffbase/)
[![PyPI downloads](https://img.shields.io/pypi/dm/gffbase.svg)](https://pypi.org/project/gffbase/)
[![Python versions](https://img.shields.io/pypi/pyversions/gffbase.svg)](https://pypi.org/project/gffbase/)
[![License](https://img.shields.io/badge/license-Apache%202.0-blue.svg)](LICENSE)
[![Docs](https://img.shields.io/badge/docs-gffbase.khchao.com-blue.svg)](https://gffbase.khchao.com/)
[![CI](https://github.com/Kuanhao-Chao/gffbase/actions/workflows/ci.yml/badge.svg?branch=main)](https://github.com/Kuanhao-Chao/gffbase/actions/workflows/ci.yml)

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
`merge_criteria` legacy API is preserved verbatim — most users
migrate by changing one import line.

### Three reasons it matters

1. **🚀 ≥ 32× faster GENCODE GTF ingest** (v49, 6.07 M lines) — and
   **mathematically more efficient**: legacy needs a Python loop +
   ~5 million correlated SQLite subqueries to *invent* the missing
   gene/transcript rows, while gffbase does the same work in two
   set-based DuckDB `GROUP BY` aggregations + one recursive CTE.
   *([Proven by a same-release GTF/GFF3 head-to-head](#-comprehensive-human-genome-annotations--validated-across-every-canonical-corpus))*
2. **⚡ 36.68× faster bulk ML extraction** — `children_batched(format='arrow')`
   returns 50 000 transcripts → 1.6 M exons as a zero-copy PyArrow
   table in **1.16 s**. No Python `Feature` objects, ever. *([How?](#-the-killer-feature--zero-copy-pyarrow-for-ml-pipelines))*
3. **🛡️  Validated NCBI compliance** — all four canonical human-genome
   annotations (GENCODE / RefSeq / MANE / CHESS 3) ingest cleanly. RefSeq's
   split-CDS duplicate-ID convention is handled automatically, and under
   `mode="strict"` those lines become one discontinuous feature rather than
   several renamed ones.

---

## 📦 Installation

```bash
pip install gffbase
```

Universal `abi3-py310` wheels — single binary per arch covers CPython
3.10 → 3.14. No Rust toolchain required at install time.

For source/dev installs (Rust >= 1.83 + maturin):

```bash
pip install -e .[dev]
maturin develop --release
```

---

## 🏃 Quick start — row-by-row (drop-in for `gffutils`)

```python
from gffbase import create_db

# 1. Ingest a GTF/GFF3 in seconds (auto-detects format, gzipped OK).
db = create_db("gencode.v49.chr_patch_hapl_scaff.basic.annotation.gtf.gz",
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

(But please read the [Migration Guide](https://gffbase.khchao.com/migration/) first — it has
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
Cookbook](https://gffbase.khchao.com/cookbooks/machine_learning_workflows/) for end-to-end
pipelines with PyTorch and Hugging Face `datasets`.

---

## ⚡ Comprehensive Human Genome Annotations — validated across every canonical corpus

Validated head-to-head against legacy `gffutils` on the four canonical
human-genome annotation sources, including the **GENCODE v49 GTF and
GFF3 versions of the same release** — a same-biology, same-features,
different-format pairing that exposes the GTF Synthesis Advantage in
its purest form:

| Corpus                   | Format | Lines      | gffbase ingest | legacy ingest | **speedup**   | spatial qps | batched (5 k anchors) |
| ------------------------ | :----: | ---------: | -------------: | ------------: | ------------: | ----------: | --------------------: |
| **GENCODE v49** (basic)  |  GTF   |  6,068,892 |   **4 min 37 s** | ≥ 2 hr 30 min[^1]    | **🚀 ≥ 32×**  |   **1,204** | 172 ms / 596 k desc   |
| **GENCODE v49** (basic)  |  GFF3  |  6,066,054 |   **6 min 7 s** | 11 min 23 s    | **1.86×**     |   **1,292** | 422 ms / 1.93 M desc  |
| **RefSeq GRCh38.p14**    |  GFF3  |  4,932,571 |   **4 min 12 s**[^2] |   6 min 5 s   | **1.45×**     |   **1,011** | 263 ms / 999 k desc   |
| **MANE v1.5** (Ensembl)  |  GFF3  |    524,834 |    **21.6 s**  |    45.1 s     | **2.09×**     |   **1,766** |  78 ms / 156 k desc   |
| **CHESS 3.1.3**          |  GFF3  |  2,761,061 |    **53.6 s**  |  2 min 13.1 s | **2.48×**     |   **1,175** |  91 ms / 161 k desc   |

[^1]: Legacy `gffutils.create_db()` on GENCODE v49 GTF (6.07 M lines) hits the bench's safety-valve cap (75 min). The reported wall is a conservative 2× extrapolation — the canonical GENCODE v45 GTF (2.0 M lines, 3× smaller) ran uncapped at **3,582 s (59 min 42 s)** on the same hardware, so the v49 wall is well past 2 hours. See [Performance Comparison §"GTF Synthesis Advantage"](https://gffbase.khchao.com/performance/#the-gtf-synthesis-advantage-proven-by-a-same-release-head-to-head) for the formal cost model.
[^2]: Result of the v0.1.0 ingest-pipeline optimization — the same RefSeq corpus used to take 7 min 49 s before the GFF3 path was re-architected to stamp `seqid_y` and `bbox` inline during the Arrow batch INSERT.

**The same biological release, ingested in two different formats, by
two different engines** — that's the load-bearing comparison. Legacy
GFF3 ingest finishes in 11 min because every parent edge is explicit;
legacy GTF ingest takes hours because the parent rows have to be
*invented* from the data (one Python ↔ SQLite round-trip per missing
row). gffbase replaces those millions of round-trips with two
set-based DuckDB `GROUP BY` aggregations + one recursive CTE — the
**same code path** runs for GTF and GFF3, which is why the gffbase
column barely shifts (4 min 37 s → 6 min 7 s) between the two rows
while the legacy column balloons by 13×–20×.

**Robustness:** every corpus ingests cleanly from the NCBI-spec-hardened
Rust parser (9 enforced rules, line-numbered `GFFFormatError`). RefSeq's
notorious duplicate-`ID=cds-NP_xxx` convention — a CDS split across several
lines — is handled transparently, and how depends on the mode you asked for.
Under the default `mode="compat"` the rows are renamed as
`gffutils.merge_strategy="create_unique"` would, so a ported script sees what
it expects. Under `mode="strict"` they are **fused into one discontinuous
feature** backed by the `segments` table, which is what the GFF3 specification
actually describes. See [Schema v2](design/schema-v2.md).

📊 Full reproducible numbers + per-corpus root-cause analysis:
[Performance Comparison](https://gffbase.khchao.com/performance/). Re-run via
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

**Numbers for that one call** (50 000 transcripts, GENCODE basic
annotation, returning 1.6 M exon rows):

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

## ✨ What's inside

- **Rust + PyO3 parser** — SIMD line/tab splitting, lazy URL-decoding
  *and* percent-encoding on the way back out, GTF semicolon-in-quotes safe,
  gzipped input transparent. Hardened against the NCBI GFF3 spec
  (line-numbered `GFFFormatError`, 9 enforced rules, `compat`/`strict`
  profiles).
- **DuckDB columnar storage** — 11-table schema (plus 3 compatibility
  views), set-based GTF
  gene/transcript synthesis, recursive-CTE transitive closure,
  per-seqid-banded R-tree spatial index built inline during ingest.
- **Smart routing** — `region()` auto-picks R-tree vs B-tree;
  `children()` auto-picks closure cache vs dynamic CTE based on
  measured corpus depth.
- **Vectorized batched API** — `children_batched`, `parents_batched`,
  `region_batched` return `pyarrow.Table` / `pandas.DataFrame` /
  `polars.DataFrame` directly out of DuckDB's buffer pool.
- **Drop-in legacy API** — `FeatureDB`, `Feature`, `create_db`,
  `DataIterator`, `GFFWriter`, `merge_criteria`, `interfeatures`,
  `bed12`, `execute()` SQL escape hatch, `export_sqlite()`. 97% of the
  gffutils 0.14 symbol surface, with every remaining difference declared and
  tested.
- **Discontinuous features** — `MultipartFeature` / `FeatureSegment`, per
  segment phase, `covered_length`, and `explode_segments=` on the batched
  APIs. Several lines sharing one `ID` are one logical feature.
- **A command line** — `gffbase create|fetch|children|parents|region|search|
  rmdups|sanitize|validate|migrate`. See [the CLI reference](cli.md).
- **Post-ingest validation** — `gffbase.validate` checks 15 invariants, and
  `gffbase.migrate` upgrades a v1 database in place.
- **abi3 wheels** — single binary per arch covers CPython 3.10-3.14.

---

## 📚 Documentation

Full site rendered with MkDocs Material:
**[https://gffbase.khchao.com/](https://gffbase.khchao.com/)**

| Page                                                                                       | What's there                                                              |
| ------------------------------------------------------------------------------------------ | ------------------------------------------------------------------------- |
| [Usage Gallery](https://gffbase.khchao.com/usage_gallery/)                     | Copy-pasteable snippets for every public API method                       |
| [Performance comparison](https://gffbase.khchao.com/performance/)              | Head-to-head numbers across every canonical human-genome annotation + per-corpus root-cause analysis |
| [Migration guide for `gffutils` users](https://gffbase.khchao.com/migration/)  | Drop-in compat checklist + the one OLAP/OLTP gotcha you must understand   |
| [Cookbooks](https://gffbase.khchao.com/cookbooks/)                             | GENCODE/Ensembl, RefSeq, MANE, ML workflows                               |
| [API reference](https://gffbase.khchao.com/api/)                               | Every public method, full signatures + docstrings                         |

To build the docs locally:

```bash
pip install -e .[docs]
mkdocs serve            # http://localhost:8000
```

---

## 🧪 Testing

```bash
pip install -e '.[test,all]'
pytest                                     # 1680 passed
pytest --cov=gffbase --cov-report=term     # coverage report
```

All 1680 tests pass with the compiled extension built and the DuckDB spatial
extension available. Without them, the `native` and `rtree` cells skip rather
than fail — CI runs dedicated jobs where a missing capability is an error, so
those paths cannot silently go unexercised.

CI runs the full matrix on Linux + macOS + Windows, both R-tree and
B-tree fallback paths, on Python 3.10 / 3.12 / 3.14.

---

## 🤝 Contributing

GFFBase welcomes pull requests, bug reports, and feature suggestions.
Start with [`CONTRIBUTING.md`](CONTRIBUTING.md) for the full guide:

- Rust + Python development setup (`maturin develop --release`)
- Running the test suite + the 99 % coverage gate
- Branch naming, Conventional Commits, the PR checklist

The repo ships standard
[issue templates](.github/ISSUE_TEMPLATE/) and a
[PR template](.github/PULL_REQUEST_TEMPLATE.md) so new
contributions land with the context maintainers need to triage them
quickly.

---

## 🪪 License

Apache License 2.0. See [`LICENSE`](LICENSE).

---

## 📖 Citation

If GFFBase contributes to your research, please cite it:

```bibtex
@software{chao_gffbase_2026,
  author  = {Chao, Kuan-Hao},
  title   = {{GFFBase}: Rust-accelerated GFF3/GTF parser with a
             DuckDB-backed storage engine and zero-copy PyArrow interface},
  year    = 2026,
  version = {0.2.0},
  url     = {https://github.com/Kuanhao-Chao/gffbase},
}
```

The repository also ships a [`CITATION.cff`](CITATION.cff), so GitHub's
"Cite this repository" button produces an up-to-date reference. Per-version
DOIs are tracked on the
[Releases page](https://github.com/Kuanhao-Chao/gffbase/releases).

---

If GFFBase saves you a benchmark cycle or a pipeline rewrite, a ⭐ on
[GitHub](https://github.com/Kuanhao-Chao/gffbase) is the easiest way
to say so — and helps other genomics teams find the project.
