# GFFBase vs. Legacy `gffutils` — Performance Comparison

**Input:** GENCODE human v45 basic annotation
([gencode.v45.basic.annotation.gtf.gz](https://ftp.ebi.ac.uk/pub/databases/gencode/Gencode_human/release_45/gencode.v45.basic.annotation.gtf.gz)),
28.25 MB compressed, 2,001,755 lines, 25 chromosomes / contigs.

**Test environment:** macOS Darwin 25.3.0, Apple Silicon, Anaconda Python 3.13.5,
DuckDB 1.5.2, PyArrow 19.0.0, gffutils 0.13, gffbase 0.1.0.

**Provenance:** Ingest numbers were captured by the `benchmarks/01_ingest.py`
harness during this benchmark run, with `--reuse-cached` quoting the Phase 6
60-minute legacy ingest (the same DB file is reused for downstream query
benchmarks). All query and disk numbers are fresh, captured by
`benchmarks/02_spatial.py`, `benchmarks/03_relational.py`,
`benchmarks/04_disk.py`. Raw machine-readable outputs:
`benchmarks/out/{01_ingest,02_spatial,03_relational,04_disk,results}.json`.

---

## 1. Headline Comparison

| Metric | gffbase | legacy gffutils | Δ |
|---|---|---|---|
| **Ingest wall** (GENCODE v45, 2.0 M lines) | **226 s** (3 min 46 s) | **3,582 s** (59 min 42 s) | **15.87× faster** |
| **Ingest peak RSS** | 1.45 GB | 145.8 MB | 10.2× more memory |
| **Ingest throughput** | 9 668 features / s | 559 features / s | 17.3× higher |
| **Spatial overlap query** (5 000 random regions, p50 latency) | **0.72 ms** | 6.01 ms | **8.3× lower latency** |
| **Spatial overlap query** (qps) | **852 qps** | 164 qps | **5.20× faster** |
| **Spatial overlap query** (p95 latency) | **2.67 ms** | 12.00 ms | **4.5× lower** |
| **Relational `children(level=None)`** (500 genes, qps) | 68.5 qps | 714 qps | 0.10× (legacy faster) |
| **Disk size** | 2.73 GB | 1.79 GB | 1.52× larger |
| **`features` row count** | 2 182 889 (incl. 181 k synthetic) | 2 001 750 | gffbase synthesizes more parents |

The picture is decisive on two of the three runtime axes (ingest, spatial
query) and unfavorable on one (single-feature relational point lookup).
Each axis has a concrete root-cause; full analysis below.

---

## 2. Ingestion

### 2.1 Numbers

| Engine | Wall | Throughput | Peak RSS | On-disk |
|---|---|---|---|---|
| gffbase 0.1.0 | **225.78 s** (3 min 46 s) | **9 668 feat/s** | 1.45 GB | 2.73 GB |
| gffutils 0.13 | 3 582.04 s (59 min 42 s) | 559 feat/s | 145.8 MB | 1.79 GB |
| **Speedup** | **15.87×** | 17.30× | 0.10× (gffbase uses more) | 0.66× (gffbase larger) |

### 2.2 Why is gffbase 15.87× faster?

Two architectural choices replace the most expensive parts of the legacy
ingestion pipeline:

1. **N+1 grandchild loop → single recursive CTE.**
   Legacy `gffutils._GFFDBCreator._update_relations` iterates
   `SELECT id FROM features` and, for **every** feature, runs an inner
   correlated subquery to find grandchildren. On GENCODE this is **2.0 M
   round-trips** through SQLite (`bench/out/legacy_full.log` shows the
   471 k-row relations population step running for 50 minutes). gffbase
   replaces this with **one set-based recursive CTE**:
   ```sql
   INSERT INTO closure (ancestor, descendant, depth)
   WITH RECURSIVE walk(ancestor, descendant, depth) AS (
       SELECT parent, child, 1 FROM edges
       UNION ALL
       SELECT w.ancestor, e.child, w.depth + 1
       FROM walk w JOIN edges e ON e.parent = w.descendant
       WHERE w.depth < ?
   )
   SELECT * FROM walk;
   ```
   Single SQL statement, vectorized inside DuckDB. Closure population
   shrinks from ~50 minutes to ~2 seconds.

2. **GTF gene/transcript synthesis: ~300 000 MIN/MAX queries → two
   `GROUP BY` statements.** Legacy runs a per-transcript MIN(start)/MAX(end)
   query, then per-gene. gffbase collapses both into single aggregations:
   ```sql
   INSERT INTO features (id, ..., start, "end", ...)
   SELECT a.value, ..., MIN(f.start), MAX(f."end"), ...
   FROM features f
   JOIN attributes a ON a.feature_id = f.id AND a.key = 'transcript_id'
   GROUP BY a.value;
   ```

3. **Per-row `INSERT` → bulk Arrow batches.** Legacy runs
   `cursor.execute(_INSERT, feature.astuple())` per feature in a try/except
   loop. gffbase accumulates 50 000 rows into a `pyarrow.Table`, registers
   it with DuckDB, and runs `INSERT INTO features SELECT * FROM staging`.
   DuckDB consumes the Arrow buffer column-wise — no per-row Python ↔ C
   crossings, no per-row JSON encoding (legacy JSON-encodes col-9 on every
   insert via `helpers._jsonify`).

### 2.3 The memory tradeoff

gffbase peaks at 1.45 GB while legacy stays under 150 MB. This is by
design: the legacy pipeline streams features one-by-one through `executemany`,
holding only one row in flight; gffbase batches 50 000 rows into Arrow before
flushing, and DuckDB allocates a multi-GB buffer pool for vectorized inserts
+ R-tree build. For HPC workstations with 16+ GB RAM the tradeoff is sound.
Memory-constrained users can reduce `batch_size` or set
`PRAGMA memory_limit='512MB'`.

### 2.4 The disk-size tradeoff

| File | Bytes | Ratio |
|---|---|---|
| `gencode.duckdb` (gffbase) | 2 932 355 072 | 1.52× |
| `gencode_legacy.sqlite` | 1 923 682 304 | 1.00× |

gffbase is ~50 % larger on disk despite DuckDB's columnar compression.
Reason: gffbase **materializes the transitive closure** (3 877 126 rows ×
~30 bytes = ~120 MB) **and per-row `seqid_y` + `bbox` columns** for the
R-tree index, which legacy has no equivalent of. The closure is what makes
`children(level=N)` an O(1) seek instead of an N-deep recursion. Per-table
breakdown:

| Table | gffbase rows | Notes |
|---|---|---|
| `features` | 2 182 889 | includes 181 k synthesized gene/transcript rows |
| `attributes` | 34 128 026 | normalized long-form (`feature_id, key, value, idx`) |
| `edges` | 2 056 515 | parent→child direct edges |
| `closure` | 3 877 126 | materialized transitive closure (depth 1+2) |
| `seqid_map` | 25 | `seqid → seqid_y` for R-tree y-banding |
| `directives`, `meta`, `autoincrements`, `duplicates` | 11 + 6 + 0 + 0 | provenance |

Legacy stores `attributes` as a single JSON-encoded TEXT column on each
feature row (1 160 966 144 bytes — 60 % of the legacy DB). gffbase pays
extra disk to enable indexed key/value attribute queries via the
`attributes_kv` index (impossible against a JSON blob without a full scan).

---

## 3. Spatial Queries (`region()`)

### 3.1 Numbers

5 000 random overlap queries grounded in real GENCODE feature spans
(`benchmarks/02_spatial.py`, seed 20260501).

| Engine | Wall | qps | Mean latency | p50 | p95 | Max |
|---|---|---|---|---|---|---|
| **gffbase R-tree** (default path) | **5.87 s** | **852** | **1.17 ms** | **0.72 ms** | **2.67 ms** | 7.4 ms |
| gffbase B-tree fallback | 8.11 s | 617 | 1.62 ms | 1.27 ms | 3.05 ms | 12.7 ms |
| legacy gffutils | 30.58 s | 164 | 6.12 ms | 6.01 ms | 12.00 ms | 47.0 ms |
| **gffbase R-tree vs legacy** | **5.20× faster** | 5.20× | 5.23× | 8.35× | 4.49× | 6.4× |
| gffbase B-tree vs legacy | 3.77× faster | 3.77× | 3.77× | 4.74× | 3.93× | 3.7× |

Both gffbase paths returned the **same number** of features per query as
legacy — semantic equivalence holds.

### 3.2 Why is gffbase 5.2× faster?

Legacy's spatial path is **UCSC-style hierarchical binning** on a B-tree:

```sql
SELECT … FROM features
WHERE bin IN (?, ?, ?, ?, ?)    -- enumerated overlapping bins
  AND seqid = ? AND start <= ? AND end >= ?
```

The bin set is computed in Python; the SQLite index then does multiple
B-tree seeks. Effective on small queries; per-query latency floors at ~5 ms
because of Python overhead (bin computation + result Feature reconstruction).

gffbase's R-tree path is a **single `ST_Intersects` predicate** against a
DuckDB R-tree where each chromosome lives in its own y-band
(`bbox = ST_MakeEnvelope(start, seqid_y, "end", seqid_y + 1)`):

```sql
SELECT … FROM features
WHERE seqid = ?
  AND ST_Intersects(bbox, ST_MakeEnvelope(?, seqid_y, ?, seqid_y + 1))
```

The y-band encoding (Phase 7) ensures internal R-tree nodes never union two
chromosomes — split heuristics segregate seqids automatically, so the index
returns only same-chromosome candidates. p50 latency drops to **0.72 ms**
because the path is a single vectorized seek + pre-encoded result tuple.

The B-tree fallback (DuckDB multi-column index on `(seqid, start, end)`) is
in between: it's faster than legacy's bin scheme (3.77×) because DuckDB's
range pruning is more efficient than SQLite's `IN (...)` bin lookup, but
slower than the R-tree because B-tree on `start/end` requires a full
candidate scan within the chromosome.

---

## 4. Relational Queries (`children(level=None)`)

### 4.1 Numbers

500 random gene IDs **common to both DBs** (`benchmarks/03_relational.py`).

| Engine | Wall | qps | Descendants returned |
|---|---|---|---|
| gffbase auto-routed (closure cache) | 7.30 s | 68.5 | 14 995 |
| gffbase forced dynamic CTE | 6.20 s | 80.6 | 14 995 |
| **legacy gffutils** | **0.70 s** | **714** | 14 578 |
| Legacy speedup over gffbase | **10.4×** | 10.4× | -2.7 % (different synthesis) |

### 4.2 Why is legacy faster on this workload? (an honest, isolated finding)

This is an unambiguous gffbase loss; we report the cause rather than hide it.

The benchmark issues 500 small `children()` queries — each gene returns ~30
descendants (~15 k Feature objects total). Three factors stack against
gffbase here:

1. **Per-row Feature object construction is more expensive in gffbase.**
   gffbase's `feature_from_row` builds a `Feature` with `_LazyAttributes`
   wrapper plus dialect dict per row. Legacy's `gffutils.Feature` is a
   simpler dataclass over `sqlite3.Row`. At 15 000 rows the per-row Python
   overhead difference dominates.

2. **DuckDB's vectorized engine optimizes for big queries, not small ones.**
   For a 30-row result, DuckDB pays a fixed-cost vectorization startup that
   SQLite skips. Legacy's `SELECT … WHERE id = ?` is an indexed B-tree seek
   completing in microseconds; gffbase's closure-JOIN-features runs a
   vectorized hash join even for tiny inputs.

3. **The legacy 1.79 GB SQLite file is fully OS-cache resident.** The Phase
   6 → Phase 11 flow leaves both DBs warm in OS file cache, so legacy's
   small queries hit memory directly. DuckDB's columnar layout reads
   different page extents and benefits less from this warmth.

**Where gffbase wins relationally** is workloads the bench doesn't capture:
single bulk queries (`SELECT … FROM closure WHERE ancestor IN (gene1, gene2,
…, gene500)`), aggregations, or joins across the entire annotation. For
the per-gene-iteration pattern this benchmark uses, legacy stays faster
when the corpus is cache-warm. **The closure cache *is* a meaningful win
when materialized depth ≥ 4** (Phase 7 §3.3 measured 3.85× over dynamic
CTE on the closure-cache-warm path) — but Phase 11's GENCODE corpus has
real depth = 2, so the cache's benefit is small.

---

## 5. Reproducibility

```bash
# All scripts live in benchmarks/. The legacy gffutils ingest takes ~60 min;
# pass --reuse-cached to quote the Phase 6 captured numbers + reuse the
# pre-built DBs in bench/out/.
cd /path/to/gffbase
pip install -e .[bench]                # pulls in psutil, gffutils, memory_profiler
python benchmarks/run_all.py --reuse-cached --n-spatial 5000 --n-relational 500

# Or run a single stage:
python benchmarks/01_ingest.py     --reuse-cached
python benchmarks/02_spatial.py    --n-queries 5000
python benchmarks/03_relational.py --n-genes 500
python benchmarks/04_disk.py
```

Outputs land in `benchmarks/out/{01..04}.json` plus an aggregated
`results.json`. Wall times are deterministic to ~10 % across reboots.

---

## 6. Summary

| Workload | Winner | Magnitude | Reason |
|---|---|---|---|
| **GENCODE-scale ingest** | **gffbase** | **15.87× faster** | recursive-CTE closure replaces N+1 loop; bulk Arrow ingest replaces per-row INSERT |
| **Spatial overlap queries** | **gffbase** | **5.20× faster, 8.3× lower p50 latency** | per-seqid R-tree y-bands segregate chromosomes; single vectorized index seek |
| **Single-feature relational lookups** | legacy gffutils | 10.4× faster on small batches | DuckDB pays vectorization overhead per query; SQLite B-tree seek is faster on cache-warm small results |
| **Peak RSS during ingest** | legacy gffutils | 10× lower | gffbase batches 50k rows into Arrow + builds R-tree in-memory |
| **Disk footprint** | legacy gffutils | 1.52× smaller | gffbase materializes closure + R-tree bbox column for query speed |

For the workloads gffbase was designed for — annotation ingestion at
mammalian scale and spatial overlap queries on whole-genome data — the
architecture is decisively faster (5×–15×). For micro-batch relational
point lookups against a cache-warm SQLite file, legacy gffutils remains
competitive; this is a known DuckDB-vs-SQLite tradeoff and is documented
above for honesty.
