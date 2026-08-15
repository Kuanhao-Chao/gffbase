---
title: Benchmark methodology
---

# Benchmark methodology

Every number published on the [Performance](../performance.md) page comes from
one run of one harness, and the file it comes from is committed to the
repository. This page describes how those numbers are produced and what they
do and do not claim.

---

## Reproducing a run

```bash
pip install -e ".[bench,all]"

python benchmarks/download_corpora.py                # ~257 MB, five corpora
python benchmarks/06_mega.py --legacy-timeout 5400 --keep-db gencode-gff3

# then regenerate the published tables from the measurements
cp benchmarks/out/06_mega.json benchmarks/results/06_mega.json
python tools/gen_benchmark_tables.py --write
```

`tools/gen_benchmark_tables.py --check` verifies that every published table
still matches the committed measurements, and is asserted by
`tests/test_release_hygiene.py` — so a table cannot drift from the numbers
behind it without a test failing.

---

## What is measured

| Metric | How |
| --- | --- |
| **Ingest wall** | `create_db()` in a fresh subprocess, wall clock around the call |
| **Peak RSS** | parent polls the child's RSS (plus its children) every 50 ms |
| **On-disk size** | recursive size of the finished database |
| **Spatial qps** | 5 000 regions sampled from the corpus's own per-seqid spans |
| **Batched extraction** | `children_batched(..., format="arrow")` over 5 000 anchors |

Region sampling uses a fixed seed (`20260501`), recorded in the results file,
so two runs sample the same regions.

## Corpora

The five canonical human-genome annotations, downloaded from their primary
sources by `benchmarks/download_corpora.py`: GENCODE v49 basic (**both** the
GTF and the GFF3 release of the same biology), RefSeq GRCh38.p14, MANE v1.5
(Ensembl IDs) and CHESS 3.1.3.

## Fairness

- Both engines get `merge_strategy="create_unique"`. This is not cosmetic:
  under the default `"error"` both refuse RefSeq and MANE outright, and timing
  gffbase under one duplicate-ID policy against gffutils under another would
  compare two different workloads on exactly the axis that decides whether the
  run completes.
- Legacy `gffutils` runs with parent inference **enabled**. Disabling it would
  skip the very work that makes GTF ingest slow, which is the phenomenon under
  study.
- Each engine runs in its own subprocess, so peak RSS is attributable and
  neither inherits the other's warm caches.
- Both write to the same filesystem.

## What is *not* comparable

- **Spatial and batched columns are gffbase-only.** `gffutils` has no spatial
  index and no batched API, so there is nothing to put in the other column.
  The `region()` throughput comparison in the narrative sections is
  like-for-like; the per-corpus qps figures are not a head-to-head.
- **Query benchmarks run against a warm page cache**, immediately after the
  ingest that built the database. They measure steady-state query throughput,
  not cold-start.

---

## Capped runs, and why there are no extrapolated numbers

Legacy `gffutils` ingest of GENCODE v49 **GTF** does not finish in any
reasonable time — GTF has no explicit parents, so every gene and transcript row
has to be invented, one Python↔SQLite round trip at a time. The harness caps it
with `--legacy-timeout` (default 90 minutes).

**A capped run yields a lower bound, never an estimate.** In the results file
`wall_seconds` is `null`, `wall_seconds_lower_bound` carries the cap, and the
speedup appears as `ingest_speedup_lower_bound`. The rendered table prints
`> 90 min` and `> N×`.

This replaced a hardcoded `wall_seconds = timeout × 2.0`. That factor had no
measurement behind it, and it was the sole source of the previously published
"≥ 2 hr 30 min" legacy wall and "≥ 32×" headline. A number produced by
multiplying a timeout is not a result, and printing one beside real
measurements invites a reader to distrust all of them.

The table generator refuses to render a row whose legacy run timed out but
still carries a wall time, so the defect cannot come back quietly.

---

## Uncertainty

The headline sweep is **n = 1 per cell**: legacy ingest of the large corpora
takes hours, and repeating the whole sweep three times is not a good use of a
day. Run-to-run spread is instead measured on the two cheap corpora, which
share the same code paths:

```bash
python benchmarks/06_mega.py --repeats 5 --only mane --only chess
```

Treat that spread as the measurement uncertainty for every cell. Where a
result file reports `n = 1` it carries a `value` key and deliberately **no**
`median`, so a renderer cannot present a single sample as a central tendency.

---

## Provenance

Every results file embeds the environment it was produced in — CPU model, core
count, RAM, OS, Python, DuckDB, PyArrow, `gffutils` and `gffbase` versions,
`rustc`, the git commit and whether the working tree was dirty:

<!-- docs-test: skip reason="reads a results file produced by a benchmark run" -->
```python
import json
env = json.load(open("benchmarks/results/06_mega.json"))["environment"]
```

None of this was recorded before 0.2.0. The published numbers carried their
hardware and versions only as hand-typed prose, which said "gffbase 0.1.0"
throughout the 0.2.0 development cycle — so nothing in the repository could
have detected a regression.

---

## Disk

A full sweep is disk-bound before it is CPU-bound: the five corpus pairs total
roughly 38 GiB. Each pair is purged as soon as its numbers are recorded, which
holds the peak to about 16 GiB. `--keep-db KEY` retains one for later stages;
`--no-purge` keeps everything and needs the full 38 GiB. Set
`GFFBASE_BENCH_OUT` to run against another volume. The harness checks free
space before each corpus and refuses to start one it cannot finish.
