---
title: Performance
---

# Performance

Head-to-head against legacy [`gffutils`](https://github.com/daler/gffutils) on
the five canonical human-genome annotation releases.

Every number below is **generated from a committed measurement file**
(`benchmarks/results/06_mega.json`) by `tools/gen_benchmark_tables.py`. A test
in the release-hygiene suite fails if a published table stops matching the data
behind it, so these cannot drift from what was actually measured. How the
measurements are taken — and what they do and do not claim — is on the
[Methodology](performance/methodology.md) page.

<!-- BEGIN GENERATED: benchmark-provenance -->
**Measured on** Apple M1 Pro · 10 cores · 16.00 GB RAM · macOS-26.3-arm64-arm-64bit-Mach-O  
**Versions:** Python 3.13.5 · gffbase 0.2.0 · duckdb 1.5.2 · pyarrow 19.0.0 · gffutils 0.13  
**Commit:** `1d52bf6738e0` · **Run:** 2026-08-15T22:56:50Z  
*Generated from `benchmarks/results/06_mega.json` by `tools/gen_benchmark_tables.py`. Do not edit by hand.*
<!-- END GENERATED: benchmark-provenance -->

---

## Across every canonical corpus

<!-- BEGIN GENERATED: corpus-table -->
| Corpus | Format | Lines | gffbase ingest | legacy ingest | speedup | peak RSS | spatial qps | batched (5 k anchors) |
| --- | :--: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| **GENCODE v49** (basic) | GTF | 6,068,892 | **4 min 5 s** | > 1 hr 30 min | **> 22.0×** | 5.62 GB | **1,457** | 522 ms / 1.93 M desc |
| **RefSeq GRCh38.p14** | GFF3 | 4,932,571 | **3 min 1 s** | 3 min 37 s | **1.20×** | 4.73 GB | **1,188** | 352 ms / 999 k desc |
| **CHESS 3.1.3** | GFF3 | 2,761,061 | **48.4 s** | 1 min 9 s | **1.43×** | 2.43 GB | **1,893** | 96 ms / 161 k desc |
| **MANE v1.5** (Ensembl) | GFF3 | 524,834 | **19.8 s** | 26.5 s | **1.34×** | 1.61 GB | **2,086** | 80 ms / 156 k desc |
<!-- END GENERATED: corpus-table -->

A `>` in the legacy column means that run was **killed at the safety valve
without finishing**, so both its wall time and the speedup are floors. No value
in this table is extrapolated; see
[Capped runs](performance/methodology.md#capped-runs-and-why-there-are-no-extrapolated-numbers).

!!! note "GENCODE v49 GFF3 is missing from this run"
    The sweep measures four of the five [corpora](datasets.md). GENCODE's
    GFF3 edition needs ~14 GiB free to hold both databases at once, and the
    run machine had 13.2 GiB, so the harness **refused to start it** rather
    than fail partway through. It is a gap in coverage, not a result: nothing
    here is inferred from the missing row, and the previous measurement for it
    was discarded rather than carried forward, because it came from a
    contaminated run on a different commit.

    Reproduce it on a machine with the headroom:

    ```bash
    python benchmarks/06_mega.py --only gencode-gff3 --publish
    python tools/gen_benchmark_tables.py --write
    ```

---

## The GTF synthesis gap

GENCODE v49 ships in **both** GTF and GFF3 — the same genes, the same
transcripts, the same exons, differing only in surface format. That pairing is
the cleanest available measurement of where the ingest cost actually lives,
because everything except the format is held constant.

**GFF3 states parentage; GTF implies it.** A GFF3 file carries explicit `gene`
and `mRNA` rows and a `Parent=` attribute on every child, so building the
hierarchy is a matter of reading edges that are already written down. A GTF
file has neither: it contains only the leaf features, each tagged with
`gene_id` and `transcript_id`, and the gene and transcript rows have to be
**invented** — their coordinates derived from the span of their children.

The two engines invent them very differently.

=== "legacy `gffutils`"

    A Python loop over every feature, and for each missing parent a correlated
    SQL subquery to find the extent of its children — millions of
    Python ↔ SQLite round trips, each one a separate query plan, on a database
    that is being written to at the same time.

=== "gffbase"

    Two set-based `GROUP BY` aggregations and one recursive CTE, evaluated
    inside DuckDB. The same three statements run whether the parents were read
    from `Parent=` columns or aggregated from `gene_id` strings, which is why
    the gffbase column barely moves between the two GENCODE rows while the
    legacy column changes by more than an order of magnitude.

This is also why the speedups on the GFF3-only corpora are modest. Where
parentage is explicit, legacy ingest is close to a streaming `INSERT` and there
is little synthesis work to win back — the gain there comes from the parser and
the columnar write path, not from the algorithm.

---

## Bulk extraction for ML

The workload GFFBase exists for: pull every exon for a large set of
transcripts, hand the columns to a tensor, train.

<!-- docs-test: skip reason="illustrative: an id list the reader supplies" -->
```python
exons = db.children_batched(transcript_ids, featuretype="exon", format="arrow")
```

One set-based SQL query returning a `pyarrow.Table` that shares memory with
DuckDB's buffers. **No Python `Feature` object is constructed at any layer** —
which is the whole difference, because constructing millions of them is what
dominates the row-by-row path in both libraries.

The per-corpus batched column in the table above shows this at 5 000 anchors.

!!! warning "The row-by-row loop is the wrong tool here"
    `for i in ids: db.children(i)` is **slower in GFFBase than in `gffutils`**.
    DuckDB pays vectorization startup on every call; SQLite, an OLTP engine,
    does not. This is a real and inherent trade, not a defect — and it is why
    the batched API exists. The [Migration guide](migration.md) covers it in
    the one place a ported script is likely to hit it.

---

## The trade-offs

Being fast at whole-corpus work costs something at the other end, and it is
worth being explicit about what:

The two costs that can be measured are, so they are generated from the same
run as the speed numbers rather than retyped:

<!-- BEGIN GENERATED: tradeoffs-table -->
| | gffbase | legacy `gffutils` | ratio |
| --- | ---: | ---: | ---: |
| **Peak ingest RSS** | 1.61 GB – 5.62 GB | 174.50 MB – 495.06 MB | 9.44–24.24× |
| **On-disk database** | 610.51 MB – 6.14 GB | 472.90 MB – 4.68 GB | 1.29–1.36× |

*Measured across 4 corpora; ratios are gffbase ÷ legacy.*
<!-- END GENERATED: tradeoffs-table -->

The rest of the ledger is qualitative, and stays that way:

| | gffbase | legacy `gffutils` |
| --- | --- | --- |
| **Ingest wall** | faster on every corpus measured | — |
| **Single-feature point query** | comparable | comparable |
| **Row-by-row loop over many IDs** | **slower** | faster |
| **Bulk batched extraction** | one query, zero `Feature` objects | not available |
| **Spatial index** | R-tree, or B-tree fallback | none |

The memory and disk costs buy the speed: an Arrow batch builder that stages
columns before writing, a materialized transitive closure so hierarchy walks
are indexed lookups rather than recursion, and a long-form attributes table so
attribute search does not scan.

---

## Reproducing this

```bash
pip install -e ".[bench,all]"
python benchmarks/download_corpora.py
python benchmarks/06_mega.py --legacy-timeout 5400 --keep-db gencode-gff3
```

Full detail, including the fairness constraints and the repeat policy, on the
[Methodology](performance/methodology.md) page.
