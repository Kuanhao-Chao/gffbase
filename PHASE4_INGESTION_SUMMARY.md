# Phase 4 — DuckDB Ingestion Engine Summary

**Scope.** Plumb the Phase 3 Rust+PyO3 parser into a DuckDB database via a fast Arrow-based handoff, materialize the seven-table schema, synthesize missing GTF gene/transcript rows in pure SQL, build the transitive closure with a recursive CTE, and add a real R-tree spatial index. **No row-by-row Python `INSERT` statements.**

**Status.** Phase 4 ingestion engine complete. **32 tests pass** (10 parser × 2 engines + 12 ingestion). The Rust→Arrow→DuckDB handoff is the only data path on the hot loop; every normalization step is a single SQL statement.

---

## 1. Repository deltas (Phase 3 → Phase 4)

```
gffutils2/
├── python/gffutils2/
│   ├── schema.py          # NEW — DDL, post-load index, set-based SQL
│   ├── ingest.py          # NEW — Rust→Arrow→DuckDB pipeline
│   └── __init__.py        # exports `ingest`
└── tests/
    ├── data/
    │   ├── hierarchy.gff3 # NEW — multi-level GFF3 fixture
    │   └── synthesize.gtf # NEW — GTF without authored gene/transcript rows
    └── test_ingest_basic.py  # NEW — 12 ingestion assertions
```

No Rust changes were required. The parser's existing 11-tuple output (`seqid, source, featuretype, start, end, score, strand, frame, attributes_blob, attributes_pairs, extra`) is exactly what the Arrow batch builder expects.

---

## 2. The Rust → DuckDB Handoff

### 2.1 Pathway

```
Rust parser  ──►  Python ParsedFeature  ──►  Arrow column lists
                                                     │
                                                     ▼
                                            PyArrow Table (schema-explicit)
                                                     │
                                                     ▼
                                       con.register("__staging_features", tbl)
                                                     │
                                                     ▼
                            INSERT INTO features SELECT * FROM __staging_features
                                                     │
                                                     ▼
                                           DuckDB columnar storage
```

The Rust extension yields 11-tuples to Python (Phase 3). The ingestion engine drains them into per-column Python lists inside an `_ArrowBatchBuilder`. When the batch hits `DEFAULT_BATCH_SIZE` (50 000 rows), the builder produces two `pa.Table` objects (`features` and `attributes`) using **explicit schemas** — no `from_pylist`-style inference — and registers them with DuckDB. DuckDB then runs `INSERT … SELECT` on the registered Arrow tables, which is its zero-copy fast path.

### 2.2 Why this avoids row-by-row INSERT

DuckDB's `con.register(name, arrow_table)` exposes an Arrow buffer to the engine as a virtual table. The subsequent `INSERT … SELECT` reads columnar Arrow chunks directly into DuckDB's columnar storage; there is **no per-row Python ↔ C boundary crossing**. Compared to `con.executemany("INSERT … VALUES (?, ?, …)", rows)`, this is dramatically faster on real workloads because:

1. The Python interpreter never holds the GIL on each row.
2. DuckDB consumes whole Arrow chunks at once.
3. Column types are declared upfront — no type-inference pass.

The only Python-side overhead is the column-list `.append()`s in the builder. Phase 5 can reduce that further by emitting Arrow batches directly from the Rust crate (via the `arrow` crate); for Phase 4 the Python builder is already much faster than the legacy `cursor.execute()`-per-feature path.

### 2.3 Schemas registered

```python
FEATURES_SCHEMA = pa.schema([
    ("id",              pa.string()),
    ("seqid",           pa.string()),
    ("source",          pa.string()),
    ("featuretype",     pa.string()),
    ("start",           pa.int64()),
    ("end",             pa.int64()),
    ("score",           pa.string()),
    ("strand",          pa.string()),
    ("frame",           pa.string()),
    ("attributes_blob", pa.binary()),
    ("extra_blob",      pa.binary()),
    ("file_order",      pa.int64()),
    ("is_synthetic",    pa.bool_()),
])

ATTRIBUTES_SCHEMA = pa.schema([
    ("feature_id", pa.string()),
    ("key",        pa.string()),
    ("value",      pa.string()),
    ("idx",        pa.int16()),
])
```

`directives` are similarly bulk-loaded from a single Arrow table; `edges` and `closure` are derived later by SQL only.

---

## 3. The Seven-Table Schema (DDL Executed)

```sql
CREATE TABLE features (
    id              VARCHAR PRIMARY KEY,
    seqid           VARCHAR NOT NULL,
    source          VARCHAR,
    featuretype     VARCHAR NOT NULL,
    start           BIGINT  NOT NULL,
    "end"           BIGINT  NOT NULL,
    score           VARCHAR,
    strand          VARCHAR,
    frame           VARCHAR,
    attributes_blob BLOB,
    extra_blob      BLOB,
    file_order      BIGINT,
    is_synthetic    BOOLEAN DEFAULT FALSE
);

CREATE TABLE attributes (
    feature_id VARCHAR NOT NULL,
    key        VARCHAR NOT NULL,
    value      VARCHAR NOT NULL,
    idx        SMALLINT NOT NULL DEFAULT 0
);

CREATE TABLE edges (
    parent VARCHAR NOT NULL,
    child  VARCHAR NOT NULL
);

CREATE TABLE closure (
    ancestor   VARCHAR NOT NULL,
    descendant VARCHAR NOT NULL,
    depth      SMALLINT NOT NULL
);

CREATE TABLE meta (
    key   VARCHAR PRIMARY KEY,
    value VARCHAR
);

CREATE SEQUENCE directive_seq START 1;
CREATE TABLE directives (
    seq       BIGINT PRIMARY KEY DEFAULT nextval('directive_seq'),
    directive VARCHAR NOT NULL
);

CREATE TABLE autoincrements (
    base VARCHAR PRIMARY KEY,
    n    BIGINT
);

CREATE TABLE duplicates (
    original_id VARCHAR NOT NULL,
    new_id      VARCHAR PRIMARY KEY
);
```

`autoincrements` and `duplicates` are reserved for the Phase 5 `merge_strategy` and `id_spec` compat layer; they are created during Phase 4 but not yet populated.

Indexes are deferred until **after** all data (including synthesized rows and the closure) has been loaded — see §6.

---

## 4. Set-Based GTF Gene/Transcript Synthesis

The legacy gffutils ran one `SELECT MIN(start), MAX(end) WHERE transcript_id = ?` per transcript and one per gene — ~300 000 round-trips on GENCODE human (Phase 1 §2.2). gffutils2 collapses this into **two `GROUP BY` aggregations**.

### 4.1 Transcript synthesis (one statement, one scan)

```sql
INSERT INTO features (id, seqid, source, featuretype, start, "end",
                      score, strand, frame,
                      attributes_blob, extra_blob, file_order, is_synthetic)
SELECT
    a.value                AS id,
    ANY_VALUE(f.seqid)     AS seqid,
    'gffutils2_derived'    AS source,
    'transcript'           AS featuretype,
    MIN(f.start)           AS start,
    MAX(f."end")           AS "end",
    '.'                    AS score,
    ANY_VALUE(f.strand)    AS strand,
    '.'                    AS frame,
    NULL, NULL, NULL,
    TRUE                   AS is_synthetic
FROM features f
JOIN attributes a ON a.feature_id = f.id AND a.key = 'transcript_id'
WHERE f.featuretype = ?                    -- bound parameter, e.g. 'exon'
  AND a.value NOT IN (SELECT id FROM features)
GROUP BY a.value;
```

Two follow-ups (also single statements):

1. Self-mark each synthetic transcript with its own `transcript_id` attribute so subsequent passes treat it the same as authored ones.
2. **Gene_id propagation** — without ever touching the edges table — by joining `attributes` on shared `feature_id`:

```sql
INSERT INTO attributes (feature_id, key, value, idx)
WITH pairs AS (
    SELECT tid.value AS transcript_id,
           gid.value AS gene_id,
           COUNT(*) AS cnt
    FROM attributes tid
    JOIN attributes gid ON gid.feature_id = tid.feature_id
    WHERE tid.key = 'transcript_id' AND gid.key = 'gene_id'
    GROUP BY tid.value, gid.value
),
ranked AS (
    SELECT transcript_id, gene_id, cnt,
           ROW_NUMBER() OVER (PARTITION BY transcript_id
                              ORDER BY cnt DESC, gene_id) AS rn
    FROM pairs
)
SELECT t.id, 'gene_id', r.gene_id, 0
FROM features t
JOIN ranked r ON r.transcript_id = t.id AND r.rn = 1
WHERE t.featuretype = 'transcript' AND t.is_synthetic = TRUE;
```

The `ROW_NUMBER()` window picks the most common `gene_id` per transcript — handles inconsistent GTFs without stalling. Crucially, this propagation **does not depend on the edges table**, so we can defer all edge population to a single late pass and avoid duplicate edges.

### 4.2 Gene synthesis (mirrors transcript)

```sql
INSERT INTO features (...)
SELECT a.value, ANY_VALUE(f.seqid), 'gffutils2_derived', 'gene',
       MIN(f.start), MAX(f."end"),
       '.', ANY_VALUE(f.strand), '.',
       NULL, NULL, NULL, TRUE
FROM features f
JOIN attributes a ON a.feature_id = f.id AND a.key = 'gene_id'
WHERE f.featuretype IN ('transcript', ?)   -- transcript + subfeature
  AND a.value NOT IN (SELECT id FROM features)
GROUP BY a.value;
```

Followed by a single `INSERT … SELECT` to give each synthetic gene its own `gene_id` attribute. Total cost: **two scans of `features`** for the entire annotation, regardless of how many genes/transcripts the file contains.

### 4.3 Edge population (single-pass, duplicate-free)

After all synthesis is complete, edges are populated exactly once:

```sql
-- For GFF3
INSERT INTO edges (parent, child)
SELECT a.value AS parent, a.feature_id AS child
FROM attributes a
WHERE a.key = 'Parent';

-- For GTF
INSERT INTO edges (parent, child)
SELECT a.value, a.feature_id
FROM attributes a
JOIN features f ON f.id = a.feature_id
WHERE a.key = 'transcript_id'
  AND f.featuretype <> 'transcript'
  AND a.value <> f.id
  AND EXISTS (SELECT 1 FROM features p WHERE p.id = a.value)
UNION ALL
SELECT a.value, a.feature_id
FROM attributes a
JOIN features f ON f.id = a.feature_id
WHERE a.key = 'gene_id'
  AND f.featuretype = 'transcript'
  AND a.value <> f.id
  AND EXISTS (SELECT 1 FROM features p WHERE p.id = a.value);
```

A regression test (`test_no_duplicate_edges_or_closure`) asserts both `edges` and `closure` contain no duplicate rows after ingestion.

---

## 5. Recursive CTE Closure (N+1 Annihilation)

The legacy gffutils ran one correlated subquery **per feature** to find grandchildren, with a tempfile detour to dodge SQLite read/write conflicts (Phase 1 §2.1). gffutils2 replaces this with **one recursive CTE**, executed by DuckDB's vectorized engine, materializing the full transitive closure up to a configurable depth:

```sql
INSERT INTO closure (ancestor, descendant, depth)
WITH RECURSIVE walk(ancestor, descendant, depth) AS (
    SELECT parent, child, 1 AS depth FROM edges
    UNION ALL
    SELECT w.ancestor, e.child, w.depth + 1
    FROM walk w
    JOIN edges e ON e.parent = w.descendant
    WHERE w.depth < ?              -- max_depth, default 8
)
SELECT ancestor, descendant, depth FROM walk;
```

One statement. No tempfile. No Python loop. Output observed on the hierarchy fixture (gene→2 mRNAs→exons/CDS):

```
('g1', 't1', 1)   ('g1', 't2', 1)
('t1', 'c1', 1)   ('t1', 'c2', 1)   ('t1', 'e1', 1)   ('t1', 'e2', 1)
('t2', 'e3', 1)
('g1', 'c1', 2)   ('g1', 'c2', 2)   ('g1', 'e1', 2)   ('g1', 'e2', 2)   ('g1', 'e3', 2)
```

depth-1 entries ≈ direct edges; depth-2 entries enable `db.children('g1', level=None)` to return every descendant exon/CDS in one indexed SQL query (Phase 5).

`max_depth=8` covers every real annotation we know of (GENCODE max is 4); deeper hierarchies fall back to runtime recursive CTE in the API layer (Phase 5 work).

---

## 6. Post-Load Indexes + R-Tree

All B-tree indexes are created **after** synthesis and closure population:

```sql
CREATE INDEX features_type     ON features(featuretype);
CREATE INDEX features_seqid    ON features(seqid);
CREATE INDEX features_seqstart ON features(seqid, start, "end");
CREATE INDEX attributes_kv     ON attributes(key, value);
CREATE INDEX attributes_fid    ON attributes(feature_id);
CREATE INDEX edges_parent      ON edges(parent);
CREATE INDEX edges_child       ON edges(child);
CREATE INDEX closure_ancestor  ON closure(ancestor, depth);
CREATE INDEX closure_descend   ON closure(descendant, depth);
```

Then the spatial extension is loaded and a real **R-tree index** is built on a generated `bbox GEOMETRY` column:

```sql
INSTALL spatial;
LOAD   spatial;
ALTER  TABLE features ADD COLUMN IF NOT EXISTS bbox GEOMETRY;
UPDATE features SET bbox = ST_MakeEnvelope(start, 0, "end", 1) WHERE bbox IS NULL;
CREATE INDEX features_rtree ON features USING RTREE (bbox);
```

`ST_MakeEnvelope(start, 0, end, 1)` packs each feature's 1-D extent into a 2-D box with a unit-height y-axis so DuckDB's R-tree machinery (which is 2-D) handles it natively. The Phase 5 `region()` query rewrites:

```sql
SELECT * FROM features
WHERE seqid = ?
  AND ST_Intersects(bbox, ST_MakeEnvelope(:rstart, 0, :rend, 1));
```

Because the R-tree depends on the spatial extension being downloadable, `_build_rtree()` falls back to the multi-column B-tree on `(seqid, start, end)` if `INSTALL spatial` fails (e.g., offline HPC environments). The returned `IngestStats.rtree_built` flag tells the caller which path was taken; on the test machine it returns `True`. A regression test (`test_rtree_query_returns_overlaps`) confirms `ST_Intersects`-backed queries return the expected exons.

---

## 7. The end-to-end pipeline (one function call)

```python
from gffutils2 import ingest

con, stats = ingest.from_file(
    "GENCODE_human_v45.gff3",
    dbfn="gencode.duckdb",
    force=True,
    batch_size=50_000,
    max_depth=8,
    disable_infer_genes=False,
    disable_infer_transcripts=False,
    gtf_subfeature="exon",
    build_rtree=True,
)

print(stats)
# IngestStats(n_features_raw=..., n_features_synthetic_transcripts=...,
#             n_features_synthetic_genes=..., n_attributes=...,
#             n_edges=..., n_closure_rows=..., rtree_built=True,
#             fmt='gff3'|'gtf', dialect={...}, directives=[...])
```

The function executes, in order:

1. Apply PRAGMAs (`threads`, configurable via `GFFUTILS2_THREADS`).
2. Run the schema DDL.
3. Drive the parser; flush Arrow batches into `features` + `attributes` every 50 000 rows.
4. Bulk-insert collected directives.
5. (GTF only) `_synthesize_transcripts` → `_synthesize_genes`, in pure SQL.
6. Single-pass `EDGES_FROM_PARENT` (GFF3) or `EDGES_FROM_GTF` (GTF).
7. Recursive CTE closure into `closure`.
8. All B-tree indexes.
9. R-tree (or fallback).
10. Persist `meta`.

---

## 8. Verification Performed

```
$ pytest
................................                                         [100%]
32 passed in 1.85s
```

| Test | Asserts |
|---|---|
| `test_gff3_basic_ingest` | All 8 authored rows ingested; no synthesis on GFF3 |
| `test_gff3_attributes_table` | Attributes long-form table is queryable by `(key, value)` |
| `test_gff3_edges` | `Parent=` ⇒ correct rows in `edges` |
| `test_gff3_closure_depths` | Recursive CTE produces depth-1 + depth-2 rows; no spurious depth-3 |
| `test_gtf_synthesis_counts` | 3 transcripts + 2 genes synthesized from 5 exons |
| `test_gtf_synthesis_extents` | `MIN/MAX` aggregation gives correct extents (T1 spans both exons) |
| `test_gtf_closure_after_synthesis` | gene → exon depth-2 closure rows materialize |
| `test_indexes_built` | Post-load indexes (`features_seqstart`, `attributes_kv`, `closure_ancestor`) present |
| `test_meta_recorded` | `schema_version` and `fmt` written to `meta` |
| `test_no_duplicate_edges_or_closure` | Regression: edges/closure contain no duplicates |
| `test_rtree_query_returns_overlaps` | `ST_Intersects` against the R-tree returns the expected exons |
| `test_force_overwrites` | `force=True` semantics on existing `.duckdb` files |

Plus 20 parser tests (10 × 2 engines) carry forward unchanged.

---

## 9. What's Intentionally NOT in Phase 4

- **The `FeatureDB` / `Feature` compat surface.** Phase 5 will wrap the connection in the legacy public API.
- **`merge_strategy`, `id_spec` callable, `transform`.** Reserved for Phase 5; the schema is ready (`autoincrements`, `duplicates`).
- **`update`, `delete`, `add_relation` mutation methods.** Phase 5.
- **Multi-file / parallel-by-seqid ingest.** Reserved per Phase 2 §1.5.
- **Performance benchmarks against GENCODE.** Phase 5+ once the API is in place to drive realistic workloads.

---

## 10. Phase 5 Hand-off

When approved, Phase 5 will:

1. Build the `FeatureDB` Python class wrapping the DuckDB connection produced by `ingest.from_file`.
2. Add `Feature` (the user-facing class with `__str__`/`__getitem__`/`sequence()`/etc.).
3. Implement every method from Phase 1 §3 against the new schema, including `region()` using the R-tree and `children`/`parents` using the closure.
4. Wire `merge_strategy`, `id_spec`, `transform`.
5. Add the `gffutils` namespace shim for drop-in compatibility.
6. Run the legacy gffutils pytest suite as the regression gate.

**Stopping here. Awaiting your review of the ingestion engine before Phase 5 begins.**
