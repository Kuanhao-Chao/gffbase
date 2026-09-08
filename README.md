<p align="center">
  <img src="docs/assets/logo.svg#gh-light-mode-only" alt="gffbase" width="62%">
  <img src="docs/assets/logo-white.svg#gh-dark-mode-only" alt="gffbase" width="62%">
</p>


[![PyPI version](https://img.shields.io/pypi/v/gffbase.svg)](https://pypi.org/project/gffbase/)
[![PyPI downloads](https://img.shields.io/pypi/dm/gffbase.svg)](https://pypi.org/project/gffbase/)
[![Python versions](https://img.shields.io/pypi/pyversions/gffbase.svg)](https://pypi.org/project/gffbase/)
[![License](https://img.shields.io/badge/license-Apache%202.0-blue.svg)](LICENSE)
[![Docs](https://img.shields.io/badge/docs-khchao.com%2Fgffbase-blue.svg)](https://khchao.com/gffbase/)
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

1. **Whole-genome ingest, measured.** The historical Mac sweep records the
   complete commands and environment for four large annotations. A controlled
   Linux campaign now separates modern GTF with existing parents, inference
   disabled, and a reproducibly parent-stripped synthesis workload before
   making a causal performance claim. *([The numbers](#-measured-against-legacy-gffutils))*
2. **Bulk extraction without Python objects.**
   `children_batched(format='arrow')` answers "every exon for these tens of
   thousands of transcripts" with a single set-based query returning a
   `pyarrow.Table` that shares memory with DuckDB. No `Feature` object is
   constructed at any layer. *([How](#-the-killer-feature--zero-copy-pyarrow-for-ml-pipelines))*
3. **Validated, not just counted.** Structural checks cover feature identity,
   normalized attributes, multipart envelopes, direct edges, transitive
   closure, indexes, and synthetic hierarchy coherence. Real-corpus runs are a
   separate release gate from the quick unit suite.

---

## 📦 Installation

```bash
pip install gffbase
```

> **Release status:** PyPI currently provides 0.1.0. This branch documents the
> unreleased 0.2.0rc1 candidate; verify `gffbase.__version__` and use an exact
> reviewed commit for candidate testing. The release checklist will add the
> final date and publication instructions only when 0.2.0 is authorized.

Universal `abi3-py310` wheels — single binary per arch covers CPython
3.10 → 3.14. No Rust toolchain required at install time.

For source/dev installs (Rust >= 1.83 + maturin):

```bash
pip install -e .[dev]
maturin develop --release
```

---

## 🏃 Quick start — row-by-row (drop-in for `gffutils`)

<!-- docs-test: skip reason="needs the GENCODE v49 corpus" -->
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

<!-- docs-test: skip reason="illustrative: contains an elided fragment" -->
```python
import gffbase as gffutils    # one-line alias migration
db = gffutils.create_db(...)  # everything else identical
```

(But please read the [Migration Guide](https://khchao.com/gffbase/content/migration.html) first — it has
**one** important note about ML loops.)

---

## 🤖 Quick start — vectorized for ML

<!-- docs-test: skip reason="illustrative: names gencode.duckdb, which the reader supplies" -->
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
Cookbook](https://khchao.com/gffbase/content/cookbook_ml_workflows.html) for end-to-end
pipelines with PyTorch and Hugging Face `datasets`.

---

## ⚡ Measured against legacy `gffutils`

The table below is the retained historical Mac run. It contains four of the
five canonical inputs and used default legacy GTF inference. It is useful
platform-specific evidence, but it does not isolate the cost of synthesis.

<!-- BEGIN GENERATED: corpus-table -->
| Corpus | Format | Lines | gffbase ingest | legacy ingest | speedup | peak RSS | spatial qps | batched (5 k anchors) |
| --- | :--: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| **GENCODE v49** (basic) | GTF | 6,068,892 | **4 min 5 s** | censored at 1 hr 30 min | — | 5.62 GB | **1,457** | 522 ms / 1.93 M desc |
| **RefSeq GRCh38.p14** | GFF3 | 4,932,571 | **3 min 1 s** | 3 min 37 s | **1.20×** | 4.73 GB | **1,188** | 352 ms / 999 k desc |
| **CHESS 3.1.3** | GFF3 | 2,761,061 | **48.4 s** | 1 min 9 s | **1.43×** | 2.43 GB | **1,893** | 96 ms / 161 k desc |
| **MANE v1.5** (Ensembl) | GFF3 | 524,834 | **19.8 s** | 26.5 s | **1.34×** | 1.61 GB | **2,086** | 80 ms / 156 k desc |
<!-- END GENERATED: corpus-table -->

<!-- BEGIN GENERATED: benchmark-provenance -->
**Measured on** Apple M1 Pro · 10 cores · 16.00 GB RAM · macOS-26.3-arm64-arm-64bit-Mach-O  
**Versions:** Python 3.13.5 · gffbase 0.2.0 · duckdb 1.5.2 · pyarrow 19.0.0 · gffutils 0.13  
**Commit:** `1d52bf6738e0` · **Run:** 2026-08-15T22:56:50Z  
*Generated from `benchmarks/results/06_mega.json` by `tools/gen_benchmark_tables.py`. Do not edit by hand.*
<!-- END GENERATED: benchmark-provenance -->

“Censored at” means the comparator was killed at its safety valve without
finishing. It is cap evidence only: no comparator wall or speedup is claimed.

**Do not interpret the GTF row as a synthesis-only comparison.** GENCODE v49's
GTF already contains gene and transcript rows. Leaving legacy inference enabled
can repeat work that its own modern-GENCODE guidance recommends disabling. The
cluster campaign therefore reports default behavior, inference-disabled real
data, and parent-stripped synthesis as three independent arms.

**Robustness.** Publication now requires matching deterministic feature and
relationship signatures plus a full structural-validation pass. Parser
failures remain line-numbered `GFFFormatError` instances.

**Three of the five use the split-CDS convention** — one CDS spread over
several lines that share an `ID` — namely RefSeq, MANE, and GENCODE's GFF3
edition. That is not an edge case, so gffbase does not pick a reading for you:
`merge_strategy` defaults to `"error"`, exactly as in `gffutils`, and you say
which you want. `merge_strategy="create_unique"` renames the rows as `gffutils`
would, so a ported script sees what it expects; `mode="strict"` fuses them into
one discontinuous feature backed by the `segments` table, which is what the
GFF3 specification actually describes.

📊 Method, fairness constraints and re-run instructions:
**[Methodology](https://khchao.com/gffbase/content/methodology.html)**. Every
number is generated from a committed measurement file by
`tools/gen_benchmark_tables.py`, and a test fails if a published table stops
matching it.

---

## 🚀 The Killer Feature — zero-copy PyArrow for ML pipelines

Modern ML genomics pipelines have one shape: **pull every exon for
50 000 transcripts, push the column-oriented table into a tensor,
train.** Legacy `gffutils` forces a per-feature Python loop —
constructing 1.6 M throwaway `Feature` objects per pull, which crushes
both wall time and memory. gffbase bypasses Python entirely with a
single batched call that returns DuckDB's internal Arrow buffers
directly:

<!-- docs-test: skip reason="illustrative: an id list the reader supplies" -->
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

**Why it is faster, and by how much.** The batched call issues one set-based
SQL query and hands back DuckDB's own Arrow buffers. The row-by-row
alternative constructs one Python `Feature` per result row, and at 1.6 M exons
that allocation dominates everything else — in *both* libraries. Per-corpus
batched throughput is in the
[table above](#-measured-against-legacy-gffutils); the
[Performance page](https://khchao.com/gffbase/content/performance.html) breaks it down.

Note the trade honestly: iterating `for x in ids: db.children(x)` in gffbase is
**slower** than legacy's SQLite row-by-row path, because DuckDB pays
vectorization startup per call. That is why the batched API exists, and why the
[Migration guide](https://khchao.com/gffbase/content/migration.html) puts it front and centre.

`region_batched(...)` and `parents_batched(...)` have the same
zero-copy contract for spatial and parent workloads.

---

## ✨ What's inside

- **Rust + PyO3 parser** — SIMD line/tab splitting, lazy URL-decoding
  *and* percent-encoding on the way back out, GTF semicolon-in-quotes safe,
  gzipped input transparent. The formal profile enforces the nine-column GFF3
  contract with line-numbered `GFFFormatError`; the compatibility profile
  preserves accepted legacy inputs with diagnostics.
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
- **Post-ingest validation** — `gffbase.validate` checks fast and full
  structural invariant sets, and
  `gffbase.migrate` upgrades a v1 database in place.
- **abi3 wheels** — single binary per arch covers CPython 3.10-3.14.

---

## 📚 Documentation

Full site, built with Sphinx and the furo theme:
**[https://khchao.com/gffbase/](https://khchao.com/gffbase/)**

| Page                                                                                       | What's there                                                              |
| ------------------------------------------------------------------------------------------ | ------------------------------------------------------------------------- |
| [Usage Gallery](https://khchao.com/gffbase/content/usage_gallery.html)                     | Copy-pasteable snippets for every public API method                       |
| [Performance comparison](https://khchao.com/gffbase/content/performance.html)              | Head-to-head numbers across every canonical human-genome annotation + per-corpus root-cause analysis |
| [Migration guide for `gffutils` users](https://khchao.com/gffbase/content/migration.html)  | Drop-in compat checklist + the one OLAP/OLTP gotcha you must understand   |
| [Cookbooks](https://khchao.com/gffbase/content/cookbooks.html)                             | GENCODE/Ensembl, RefSeq, MANE, ML workflows                               |
| [API reference](https://khchao.com/gffbase/content/api.html)                               | Every public method, full signatures + docstrings                         |

To build the docs locally:

```bash
pip install -e .[docs]       # needs Python >= 3.12; Sphinx 9 requires it
make -C docs html            # -> docs/build/html/index.html
```

The build runs with `-W --keep-going`, so a broken cross-reference or a page
missing from the toctree is an error, not a quietly degraded page. That is the
same command CI runs.

---

## 🧪 Testing

```bash
pip install -e '.[test,all]'
pytest
pytest --cov=gffbase --cov-report=term     # coverage report
```

The suite passes in full with the compiled extension built and the DuckDB
spatial extension available. Without them, the Rust-engine and R-tree cells
skip rather than fail — CI runs dedicated jobs where a missing capability is
an error, so those paths cannot silently go unexercised.

Three things are checked that most suites do not:

- **Differential parity** against a git-pinned `gffutils` build, with every
  deliberate difference declared in a register that fails both on an
  undeclared gap and on a declaration that has gone stale.
- **The documentation's own code.** Every runnable snippet in `docs/`,
  `README.md` and `MIGRATION.md` is executed against the vendored fixtures, so
  a documented example cannot rot unnoticed.
- **Release invariants** — version agreement across every file that states one,
  packaging manifests resolving to real files, workflow YAML that GitHub will
  actually load, and every published benchmark table matching its committed
  measurements.

Add `-m corpus`, after `python benchmarks/download_corpora.py`, to run the
same operations against the real human-genome annotations.

CI runs the full matrix on Linux + macOS + Windows, both R-tree and
B-tree fallback paths, on Python 3.10 / 3.12 / 3.14.

---

## 🤝 Contributing

GFFBase welcomes pull requests, bug reports, and feature suggestions.
Start with [`CONTRIBUTING.md`](CONTRIBUTING.md) for the full guide:

- Rust + Python development setup (`maturin develop --release`)
- Running the test suite + the coverage gate (95 % R-tree / 94 % B-tree)
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
  version = {0.2.0rc1},
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
