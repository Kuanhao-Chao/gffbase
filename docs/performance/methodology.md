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

**A capped run is censored, not measured.** In a current result its state is
`timed_out`, `cap_seconds` records the safety valve, and `wall_seconds` is
`null`. It produces neither a speedup nor a speedup floor. Tables render only
`censored at 90 min` in the comparator column.

The preserved 2026-08-15 Mac artifact predates this contract and uses schema
v2 names such as `wall_seconds_lower_bound`. Its bytes remain historical
evidence, but the renderer treats those fields only as censoring metadata and
never repeats the old `> N×` claim.

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

`--repeats` applies to the two cheap in-process measurements — the spatial
sweep and the batched extraction — and **not** to the ingest walls, where a
single legacy GENCODE run already costs over an hour. When it is greater than
1, the first pass is discarded as a warm-up: a smoke test measured a 6.7×
max/min ratio that was entirely cold page cache, and a spread that is really a
cold-start artifact is worse than no spread, because it gets published as
measurement uncertainty. After the warm-up the same measurement spreads under
5%.

Treat that spread as the measurement uncertainty for every cell. Where a
result file reports `n = 1` it carries a `value` key and deliberately **no**
`median`, so a renderer cannot present a single sample as a central tendency.

### Equal work, or no ratio

A speedup is recorded only when both completed databases have the same strict
`database-signature-v3`. The signature covers every logical segment,
canonicalized per-segment attributes (including empty flags and value order),
direct relationships, minimum-depth closure, feature counts, and the
feature-type histogram. Feature counts remain a useful diagnostic but are not
accepted as a correctness proof by themselves.

Attribute **key** order is deliberately canonicalized lexically because GFF/GTF
key order is non-semantic and engines may expose inferred attributes
differently. Value order is retained within each key, so reordering values
changes the signature while reordering keys does not.

---

## Publishing a run

A sweep writes to `benchmarks/out/` (gitignored). The committed file the
published tables are generated from is `benchmarks/results/06_mega.json`, and
copying between them used to be an undocumented manual step — so a fresh run
could sit on disk while the docs kept rendering the previous measurement.

```bash
python benchmarks/06_mega.py --legacy-timeout 5400 --publish
python tools/gen_benchmark_tables.py --write
```

`--publish` copies the finished run into place; the generator then rewrites
every table from it. `tests/test_release_hygiene.py` fails if a table stops
matching the numbers behind it.

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
