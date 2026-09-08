---
hide:
  - navigation
title: GFFBase
---

# GFFBase

[![PyPI](https://img.shields.io/pypi/v/gffbase.svg)](https://pypi.org/project/gffbase/)
[![Python](https://img.shields.io/pypi/pyversions/gffbase.svg)](https://pypi.org/project/gffbase/)
[![License](https://img.shields.io/badge/license-Apache%202.0-blue.svg)](https://github.com/Kuanhao-Chao/gffbase/blob/main/LICENSE)
[![CI](https://github.com/Kuanhao-Chao/gffbase/actions/workflows/ci.yml/badge.svg?branch=main)](https://github.com/Kuanhao-Chao/gffbase/actions/workflows/ci.yml)

**A genomic-annotation engine built for whole-genome scale: a Rust GFF3/GTF
parser, DuckDB columnar storage, and a zero-copy PyArrow interface — with the
`gffutils` API preserved, so most code migrates by changing one import.**

```bash
pip install gffbase
```

!!! warning "Release-candidate documentation"
    PyPI currently provides 0.1.0. This site describes the unreleased 0.2.0rc1
    candidate; verify the installed version and use an exact reviewed commit
    for candidate testing.

<!-- docs-test: skip reason="needs the GENCODE v49 corpus" -->
```python
from gffbase import create_db

with create_db("gencode.v49.annotation.gtf.gz", "gencode.duckdb") as db:
    for tx in db.children("ENSG00000139618", featuretype="transcript"):
        print(tx.id, tx.start, tx.end)

    for feature in db.region("chr17:43044295-43125483", featuretype="exon"):
        print(feature)
```

<div class="grid cards" markdown>

-   :material-download: **[Install](getting-started/installation.md)**

    Wheels for Linux, macOS and Windows. No Rust toolchain needed.

-   :material-rocket-launch: **[Quickstart](getting-started/quickstart.md)**

    Ten minutes, one file, and the four things you will actually do.

-   :material-swap-horizontal: **[Migrating from gffutils](migration.md)**

    What is identical, what differs, and the one gotcha that matters.

-   :material-book-open-variant: **[API reference](api/index.md)**

    Every public method, with signatures and docstrings.

</div>

---

## What it is for

Three workloads, in the order people hit them:

**Ingesting a whole-genome annotation.** A SIMD Rust parser feeds DuckDB
through Arrow record batches. When a GTF genuinely omits gene or transcript
rows, their parents are synthesized with set-based SQL. Modern GENCODE already
contains those rows, so inference can be disabled for that workload. Gzipped
input is accepted directly, although native parsing currently materializes the
decompressed input before returning records.

**Querying it.** `region()` picks an R-tree or a B-tree per query;
`children()` picks a materialized transitive closure or a recursive CTE based
on the corpus's measured hierarchy depth. You do not choose, and both paths are
tested to return identical results.

**Feeding a model.** `children_batched(format="arrow")` answers "every exon for
these fifty thousand transcripts" with a single query returning a
`pyarrow.Table` that shares memory with DuckDB — constructing no Python
`Feature` objects at any layer. `parents_batched` and `region_batched` have the
same contract, and `format="df"` / `"polars"` are there too.

---

## Measured against gffutils

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

This is the retained historical Mac result, generated from a committed
measurement file and verified by the test suite. It contains four corpora and
must not be combined with Linux ratios. See [Performance](performance.md) and
[Methodology](performance/methodology.md). “Censored at” marks a legacy run
killed at the safety valve without finishing, so no comparator wall or ratio
is reported.

!!! note "One honest caveat"
    A **row-by-row loop** over many IDs (`for i in ids: db.children(i)`) is
    *slower* in GFFBase than in `gffutils` — DuckDB pays vectorization startup
    per call. That trade is why the batched API exists, and the
    [Migration guide](migration.md) shows the rewrite.

---

## Compatibility

The `FeatureDB` / `Feature` / `create_db` / `DataIterator` / `GFFWriter` /
`merge_criteria` surface is preserved, and the remaining differences are
declared in a register that CI checks against a pinned `gffutils` build — it
fails both on an undeclared gap and on a declaration that has gone stale.

<!-- docs-test: skip reason="illustrative: contains an elided fragment" -->
```python
import gffbase as gffutils        # one-line alias migration
db = gffutils.create_db(...)
```

Real annotation files break the GFF3 specification routinely. GFFBase's default
`mode="compat"` reads what `gffutils` reads; `mode="strict"` applies the full
NCBI rule set and fuses split-CDS records into genuine discontinuous features.
See [Compatibility & strict modes](guides/modes.md).

---

## Also included

- A **command line** — `gffbase create|fetch|children|parents|region|search|rmdups|sanitize|validate|migrate`
- **Post-ingest validation** (`gffbase.validate`) checking 14 structural invariants
- **In-place schema migration** (`gffbase.migrate`) for databases written by 0.1.x
- **A SQLite exporter** producing a database real `gffutils` can open
- **A pure-Python fallback parser**, so a wheel-less install still works
