<p align="center">
  <img src="https://raw.githubusercontent.com/Kuanhao-Chao/gffbase/main/docs/source/_static/logo.svg#gh-light-mode-only" alt="gffbase" width="62%">
  <img src="https://raw.githubusercontent.com/Kuanhao-Chao/gffbase/main/docs/source/_static/logo-white.svg#gh-dark-mode-only" alt="gffbase" width="62%">
</p>


[![PyPI version](https://img.shields.io/pypi/v/gffbase.svg)](https://pypi.org/project/gffbase/)
[![PyPI downloads](https://img.shields.io/pypi/dm/gffbase.svg)](https://pypi.org/project/gffbase/)
[![Python versions](https://img.shields.io/pypi/pyversions/gffbase.svg)](https://pypi.org/project/gffbase/)
[![License](https://img.shields.io/badge/license-Apache%202.0-blue.svg)](https://github.com/Kuanhao-Chao/gffbase/blob/main/LICENSE)
[![Docs](https://img.shields.io/badge/docs-khchao.com%2Fgffbase-blue.svg)](https://khchao.com/gffbase/)
[![CI](https://github.com/Kuanhao-Chao/gffbase/actions/workflows/ci.yml/badge.svg?branch=main)](https://github.com/Kuanhao-Chao/gffbase/actions/workflows/ci.yml)

## What is GFFBase?

**A high-performance genomic-annotation engine — a SIMD Rust parser, a DuckDB
columnar backend and a zero-copy PyArrow interface — built for whole-genome
ingest and bulk machine-learning feature extraction, and a drop-in successor to
[`gffutils`](https://github.com/daler/gffutils).**

The legacy `FeatureDB` / `Feature` / `create_db` / `DataIterator` / `GFFWriter` /
`merge_criteria` API is preserved verbatim, so most scripts migrate by changing
one import line.

## Install

```bash
pip install gffbase
```

Universal `abi3` wheels: one binary per platform covers CPython 3.10 through
3.14, and no Rust toolchain is needed. Source and development builds are in the
[installation guide](https://khchao.com/gffbase/content/installation.html).

> **0.2.0 is the current release.** It fixes two SQL injection vulnerabilities
> in 0.1.0 — see the [security advisory](https://github.com/Kuanhao-Chao/gffbase/security/advisories/GHSA-5f5g-g3v5-prrg) —
> and it contains breaking changes, listed in the
> [changelog](https://github.com/Kuanhao-Chao/gffbase/blob/main/CHANGELOG.md).

## Quick start

<!-- docs-test: skip reason="needs the GENCODE v49 corpus" -->
```python
from gffbase import create_db

# Ingest a GTF or GFF3 — format auto-detected, gzip transparent.
with create_db("gencode.v49.basic.annotation.gtf.gz", "gencode.duckdb") as db:

    # Walk one gene's hierarchy.
    for tx in db.children("ENSG00000139618", level=1, featuretype="transcript"):
        print(tx.id, tx.start, tx.end)

    # Overlap query, routed to the per-seqid R-tree.
    for exon in db.region("chr17:43044295-43125483", featuretype="exon"):
        print(exon)
```

Coming from `gffutils`? `import gffbase as gffutils` is usually the only change
you need — but read the [migration guide](https://khchao.com/gffbase/content/migration.html) first,
for the one loop pattern you should not port unchanged.

## Bulk extraction without Python objects

The workload GFFBase exists for: pull every exon for tens of thousands of
transcripts, hand the columns to a tensor, train. One set-based query returns
DuckDB's own Arrow buffers, and **no** `Feature` **object is constructed at any
layer** — where a per-feature Python loop allocates millions of throwaway
objects and that allocation dominates everything else.

<!-- docs-test: skip reason="illustrative: an id list the reader supplies" -->
```python
exons = db.children_batched(
    transcript_ids,              # an iterable of 50 000 IDs
    featuretype="exon",
    format="arrow",              # "df" and "polars" also supported
)
# A pyarrow.Table sharing memory with DuckDB. Its "anchor" column carries the
# input id for each row, so per-transcript groups survive without N queries.

import torch
starts = torch.from_numpy(exons.column("start").to_numpy())
```

`region_batched()` and `parents_batched()` offer the same zero-copy contract for
spatial and parent workloads. One trade to know: iterating
`for i in ids: db.children(i)` is **slower** here than legacy's row-by-row
SQLite path, because DuckDB pays vectorization startup on every call — which is
why the batched API exists.

End-to-end PyTorch and Hugging Face pipelines are in the
[ML workflows cookbook](https://khchao.com/gffbase/content/cookbook_ml_workflows.html),
and every method has a snippet in the
[usage gallery](https://khchao.com/gffbase/content/usage_gallery.html).

## Measured against `gffutils`

Head-to-head across five canonical human-genome annotations — one run, one
machine, one commit. **Read the ingest column as a draw rather than a win:**
gffbase spans 1.21× to 0.69×, ahead where per-feature overhead dominates and
behind on the attribute-dense whole-genome files, because both engines are
attribute-bound and effectively serial at a comparable rate. The durable
advantages — batched extraction, spatial indexing, SQL over the whole corpus —
are untouched by that result.

<!-- BEGIN GENERATED: corpus-table -->
| Corpus | Format | Lines | gffbase ingest | legacy ingest | speedup | peak RSS (ingest + full validation) | spatial qps | batched (5 k anchors) |
| --- | :--: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| **GENCODE v49** (basic) | GTF | 6,068,892 | **10 min 14 s** | 7 min 1 s | **0.69×** | 53.57 GB | **707** ±0% (n=5) | 963 ms / 1.93 M desc |
| **GENCODE v49** (basic) | GFF3 | 6,066,054 | **11 min 8 s** | 9 min 59 s | **0.90×** | 62.00 GB | **705** ±0% (n=5) | 1096 ms / 1.93 M desc |
| **RefSeq GRCh38.p14** | GFF3 | 4,932,571 | **7 min 2 s** | 6 min 34 s | **0.93×** | 26.80 GB | **540** ±0% (n=5) | 588 ms / 999 k desc |
| **CHESS 3.1.3** | GFF3 | 2,761,061 | **1 min 53 s** | 2 min 17 s | **1.21×** | 3.38 GB | **702** ±0% (n=5) | 202 ms / 161 k desc |
| **MANE v1.5** (Ensembl) | GFF3 | 524,834 | **40.0 s** | 45.7 s | **1.14×** | 3.98 GB | **840** ±0% (n=5) | 206 ms / 156 k desc |
<!-- END GENERATED: corpus-table -->

<!-- BEGIN GENERATED: benchmark-provenance -->
**Measured on** AMD EPYC 7702 64-Core Processor · 128 cores · 1007.22 GB RAM · Linux-5.14.0-503.15.1.el9_5.x86_64-x86_64-with-glibc2.34  
**Versions:** Python 3.11.16 · gffbase 0.2.0rc1 · duckdb 1.5.5 · pyarrow 25.0.1 · gffutils 0.14  
**Commit:** `42bb900e328c` · **Run:** 2026-09-10T19:56:52Z  
*Generated from `benchmarks/results/06_mega.linux-x86_64.json` by `tools/gen_benchmark_tables.py`. Do not edit by hand.*
<!-- END GENERATED: benchmark-provenance -->

`peak RSS` is ingest **plus exhaustive validation**, not the cost of ingest:
`validate_db` defaults to `sample=200` and the CLI never overrides it. No
speedup is published unless both engines' correctness signatures match first,
and every number is generated from a committed measurement file — a test fails
if a published table stops matching it. The per-corpus analysis, the fairness
constraints and the measured run-to-run spread are on the
[performance](https://khchao.com/gffbase/content/performance.html) and
[methodology](https://khchao.com/gffbase/content/methodology.html) pages.

## What's inside

- **Rust + PyO3 parser** — SIMD line and tab splitting, percent-decoding in and
  encoding back out, GTF semicolon-in-quotes safe, gzip transparent, failures
  raised as line-numbered `GFFFormatError`.
- **DuckDB columnar storage** — an 11-table schema plus 3 compatibility views,
  set-based GTF gene/transcript synthesis, recursive-CTE transitive closure, and
  a per-seqid-banded R-tree built inline during ingest.
- **Smart routing** — `region()` picks R-tree or B-tree; `children()` picks the
  closure cache or a dynamic CTE from the corpus's measured hierarchy depth.
- **Vectorized batched API** — `children_batched`, `parents_batched` and
  `region_batched` return `pyarrow.Table`, `pandas.DataFrame` or
  `polars.DataFrame` straight out of DuckDB's buffer pool.
- **Drop-in legacy API** — plus `bed12`, `interfeatures`, an `execute()` SQL
  escape hatch and `export_sqlite()`, with every remaining difference from
  `gffutils` 0.14 declared and tested.
- **Discontinuous features** — several lines sharing one `ID` are one logical
  feature, with per-segment phase, `covered_length` and `explode_segments=`.
- **A command line and validation** — `gffbase create|fetch|children|parents|region|search|rmdups|sanitize|validate|migrate`,
  structural validation at two depths, and in-place migration of a v1 database.

## Documentation

Full site: **[khchao.com/gffbase](https://khchao.com/gffbase/)**

| Page | What's there |
| --- | --- |
| [Quickstart](https://khchao.com/gffbase/content/quickstart.html) | A ten-minute walkthrough on a demo annotation |
| [Migration guide](https://khchao.com/gffbase/content/migration.html) | Drop-in checklist, and the OLAP/OLTP gotcha |
| [Usage gallery](https://khchao.com/gffbase/content/usage_gallery.html) | Every public method, copy-pasteable |
| [Cookbooks](https://khchao.com/gffbase/content/cookbooks.html) | GENCODE/Ensembl, RefSeq, MANE, ML workflows |
| [Performance](https://khchao.com/gffbase/content/performance.html) | The numbers, and what they do not claim |
| [Command line](https://khchao.com/gffbase/content/cli.html) | Every `gffbase` subcommand |
| [API reference](https://khchao.com/gffbase/content/api.html) | Full signatures and docstrings |
| [FAQ](https://khchao.com/gffbase/content/faq.html) and [troubleshooting](https://khchao.com/gffbase/content/troubleshooting.html) | Short answers; errors organized by message |

## Contributing

Pull requests, bug reports and feature suggestions are welcome.
[`CONTRIBUTING.md`](https://github.com/Kuanhao-Chao/gffbase/blob/main/CONTRIBUTING.md) covers the Rust and Python
development setup, the test suite and its coverage gates, branch naming and the
PR checklist. The repository ships
[issue](https://github.com/Kuanhao-Chao/gffbase/tree/main/.github/ISSUE_TEMPLATE) and
[pull-request](https://github.com/Kuanhao-Chao/gffbase/blob/main/.github/PULL_REQUEST_TEMPLATE.md) templates.

## License

Apache License 2.0 — see [`LICENSE`](https://github.com/Kuanhao-Chao/gffbase/blob/main/LICENSE).

## Citation

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

The repository also ships a [`CITATION.cff`](https://github.com/Kuanhao-Chao/gffbase/blob/main/CITATION.cff), so
GitHub's "Cite this repository" button produces an up-to-date reference.
Per-version DOIs are tracked on the [releases page](https://github.com/Kuanhao-Chao/gffbase/releases).
