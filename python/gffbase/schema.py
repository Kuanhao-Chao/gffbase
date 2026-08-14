# ---------------------------------------------------------------------------
# Author: Kuan-Hao Chao <kuanhao.chao@gmail.com>
# Copyright 2026 Kuan-Hao Chao
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
# ---------------------------------------------------------------------------
"""DDL for the gffbase DuckDB schema (Phase 2 §4.1) and post-load index/synthesis SQL.

Centralized so the ingestion engine and tests share one source of truth.
"""

from __future__ import annotations

# Schema version, recorded in `meta` and checked by `FeatureDB.__init__`.
#
# v2 adds the storage a *discontinuous* (multipart) GFF3 feature needs: several
# input lines sharing one `ID`, which is how NCBI represents a split CDS. In v1
# the second such line collided against the `features` primary key and every
# merge strategy lost information.
#
# The shape that keeps the change small: `features` stays ONE ROW PER LOGICAL
# FEATURE, with `start`/`end`/`bbox` widened to the *envelope* over its
# segments; `segments` is a SPARSE side table carrying physical lines only for
# features with `n_segments > 1`. So logical dedup stays structural (no
# DISTINCT anywhere), the R-tree stays the primary access path, storage grows
# only with duplicate lines rather than with corpus size, and a v1 -> v2
# migration touches zero feature rows.
SCHEMA_VERSION = "2"

DDL = """
CREATE TABLE IF NOT EXISTS features (
    id              VARCHAR PRIMARY KEY,   -- LOGICAL key, post-resolution
    seqid           VARCHAR NOT NULL,      -- logical: invariant across segments
    source          VARCHAR,               -- logical
    featuretype     VARCHAR NOT NULL,      -- logical
    -- NULLABLE on purpose. A GFF row may legally carry `.` in columns 4 and 5,
    -- and gffutils preserves that as None. Declaring these NOT NULL forced the
    -- Arrow builder to coerce a missing coordinate to 0, so the feature
    -- reopened as 0..0 and serialized zeros where the source said `.`.
    --
    -- For a multipart feature these are the ENVELOPE: MIN(start), MAX(end)
    -- over its segments. For the singleton case -- every feature until a file
    -- actually carries a split one -- the envelope IS the line, which is why
    -- `features_rtree` stays valid across the v1 -> v2 migration.
    start           BIGINT,
    "end"           BIGINT,
    -- Representative values, taken from segment 0. Per-segment score and
    -- phase live in `segments`; carrying segment 0's here keeps every v1
    -- query shape working unchanged.
    score           VARCHAR,
    strand          VARCHAR,               -- logical: one feature, one orientation
    frame           VARCHAR,
    attributes_blob BLOB,
    extra_blob      BLOB,
    file_order      BIGINT,
    is_synthetic    BOOLEAN DEFAULT FALSE,
    -- v2. `id` as derived from column 9 BEFORE duplicate resolution renamed
    -- it. Together with `occ` it forms the load-time surrogate identity
    -- `(raw_id, occ)` that the multipart resolve pass groups on.
    raw_id          VARCHAR,
    -- v2. 0-based occurrence of `raw_id` at load time. Non-zero only where a
    -- duplicate id was resolved by renaming.
    occ             INTEGER NOT NULL DEFAULT 0,
    -- v2. Number of physical input lines this feature was built from. 1 for
    -- everything except a genuine discontinuous feature; the query builders
    -- test `meta.n_multipart` so they can skip the segment machinery whenever
    -- no feature in the database has more than one.
    n_segments      INTEGER NOT NULL DEFAULT 1,
    -- v2. Where `id` came from: 'attribute' when the data supplied it,
    -- 'autoincrement' when it was generated. `IdSpecResolver.resolve` already
    -- computed this; v1 discarded it.
    id_origin       VARCHAR NOT NULL DEFAULT 'attribute',
    -- y-band populated post-load: each distinct seqid gets a unique band so
    -- the R-tree split heuristics segregate chromosomes (Phase 7 fix).
    seqid_y         BIGINT
);

-- v2. Physical input lines for multipart features ONLY.
--
-- Invariant: a `feature_id` appears here iff its `features.n_segments > 1`,
-- and when it appears, ALL of its segments 0 .. n-1 are present. Segment 0 is
-- stored even though `features` carries its blob, because `features.start/end`
-- is the envelope and segment 0's own coordinates need somewhere to live.
--
-- `frame` is the main reason this table exists: a split CDS carries a
-- different phase on each line, and there is nowhere in `features` to put
-- more than one of them.
CREATE TABLE IF NOT EXISTS segments (
    feature_id      VARCHAR NOT NULL,
    -- 0-based, ordered by FILE APPEARANCE rather than coordinate: GFF3 does
    -- not require segments to be sorted and `to_lines()` has to reproduce the
    -- input order. Coordinate order is recovered with `ORDER BY start`.
    seg_idx         INTEGER NOT NULL,
    start           BIGINT,
    "end"           BIGINT,
    score           VARCHAR,
    frame           VARCHAR,
    attributes_blob BLOB NOT NULL,
    extra_blob      BLOB,
    file_order      BIGINT NOT NULL,
    -- False when this segment's raw column 9 differs byte-for-byte from
    -- segment 0's. NCBI repeats identical attributes on every CDS line, so
    -- this is true almost always, and `attributes` rows are stored per-segment
    -- only where it is false.
    attrs_same_as_seg0 BOOLEAN NOT NULL DEFAULT TRUE,
    seqid_y         BIGINT
);

-- One row per distinct seqid mapping to its R-tree y-band. Populated during
-- the R-tree build step; consulted at query time to translate `region()`
-- bounds into the same band space used by the index.
CREATE TABLE IF NOT EXISTS seqid_map (
    seqid  VARCHAR PRIMARY KEY,
    seqid_y BIGINT NOT NULL
);

CREATE TABLE IF NOT EXISTS attributes (
    feature_id      VARCHAR NOT NULL,   -- LOGICAL id
    key             VARCHAR NOT NULL,
    value           VARCHAR NOT NULL,
    idx             SMALLINT NOT NULL DEFAULT 0,  -- multivalue index (v1 meaning)
    -- v2. Owning physical line. Rows with seg_idx > 0 exist only where that
    -- segment's blob differs from segment 0's.
    seg_idx         INTEGER NOT NULL DEFAULT 0,
    -- v2. Pair position within its line. v1 relied on physical insertion order
    -- to reproduce key order, which no SQL engine guarantees.
    ord             INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS edges (
    parent          VARCHAR NOT NULL,
    child           VARCHAR NOT NULL
);

CREATE TABLE IF NOT EXISTS closure (
    ancestor        VARCHAR NOT NULL,
    descendant      VARCHAR NOT NULL,
    depth           SMALLINT NOT NULL
);

CREATE TABLE IF NOT EXISTS meta (
    key             VARCHAR PRIMARY KEY,
    value           VARCHAR
);

CREATE SEQUENCE IF NOT EXISTS directive_seq START 1;

CREATE TABLE IF NOT EXISTS directives (
    seq             BIGINT PRIMARY KEY DEFAULT nextval('directive_seq'),
    directive       VARCHAR NOT NULL
);

CREATE TABLE IF NOT EXISTS autoincrements (
    base            VARCHAR PRIMARY KEY,
    n               BIGINT
);

-- Must be byte-for-byte what gffutils writes: `export_sqlite` copies it
-- verbatim and the oracle's `_candidate_merges` reads it. gffutils populates
-- it ONLY from the merge -> create_unique fallback, so plain `create_unique`
-- deliberately records nothing here.
CREATE TABLE IF NOT EXISTS duplicates (
    original_id     VARCHAR NOT NULL,
    new_id          VARCHAR PRIMARY KEY
);

-- v2, gffbase-native. The full record `duplicates` cannot hold without
-- breaking oracle compatibility: every id resolution, with its reason.
-- `kind` is one of multipart / create_unique / merge / merge_fallback /
-- warning_dropped / replaced / strict_split.
CREATE TABLE IF NOT EXISTS id_conflicts (
    raw_id          VARCHAR NOT NULL,
    resolved_id     VARCHAR NOT NULL,
    kind            VARCHAR NOT NULL,
    file_order      BIGINT,
    detail          VARCHAR
);
"""


# ---------------------------------------------------------------------------
# `segments_all` -- the uniform physical view: exactly one row per physical
# input line, whether or not the owning feature is multipart.
#
# The `n_segments = 1` filter on the first branch is what makes the two
# branches disjoint; without it, segment 0 of a multipart feature would be
# counted twice. Every physical-level consumer reads this one name --
# `explode_segments`, `export_sqlite`, `to_lines()`, the validator -- rather
# than re-deriving the UNION.
#
# Created after the bulk load alongside the other views, so it is defined once
# the conditional `bbox` column exists.
# ---------------------------------------------------------------------------
SEGMENTS_ALL_VIEW = """
CREATE OR REPLACE VIEW segments_all AS
    SELECT id AS feature_id, 0 AS seg_idx, seqid, source, featuretype, strand,
           start, "end", score, frame, attributes_blob, extra_blob,
           file_order, is_synthetic, seqid_y
    FROM features WHERE n_segments = 1
UNION ALL
    SELECT s.feature_id, s.seg_idx, f.seqid, f.source, f.featuretype, f.strand,
           s.start, s."end", s.score, s.frame, s.attributes_blob, s.extra_blob,
           s.file_order, f.is_synthetic, s.seqid_y
    FROM segments s JOIN features f ON f.id = s.feature_id;
"""


# Post-load index DDL. Built only after bulk insert + closure materialization,
# per the Phase 2 plan. Note: B-tree on `(seqid, start, end)` is the universal
# fallback; the R-tree (if the spatial extension loads) is added separately in
# `ingest.py::_build_rtree`.
POST_LOAD_INDEXES = """
CREATE INDEX IF NOT EXISTS features_type     ON features(featuretype);
CREATE INDEX IF NOT EXISTS features_seqstart ON features(seqid, start, "end");
CREATE INDEX IF NOT EXISTS attributes_kv     ON attributes(key, value);
CREATE INDEX IF NOT EXISTS attributes_fid    ON attributes(feature_id);
CREATE INDEX IF NOT EXISTS edges_parent      ON edges(parent);
CREATE INDEX IF NOT EXISTS edges_child       ON edges(child);
CREATE INDEX IF NOT EXISTS closure_ancestor  ON closure(ancestor, depth);
CREATE INDEX IF NOT EXISTS closure_descend   ON closure(descendant, depth);
CREATE INDEX IF NOT EXISTS segments_fid      ON segments(feature_id, seg_idx);
"""
# Phase 19: dropped redundant `features_seqid` — every (seqid)
# predicate is satisfied by the leading prefix of `features_seqstart`.


# ---------------------------------------------------------------------------
# Set-based normalization SQL (Phase 2 §3).
# ---------------------------------------------------------------------------

# 1. Edges from GFF3 `Parent=` attributes.
EDGES_FROM_PARENT = """
INSERT INTO edges (parent, child)
SELECT DISTINCT a.value AS parent, a.feature_id AS child
FROM attributes a
WHERE a.key = 'Parent';
"""
# DISTINCT because a fused discontinuous feature takes the UNION of its
# segments' `Parent` values, and NCBI repeats `Parent=` on every segment line.
# It also fixes a latent v1 defect: `Parent=a,a` on a single line produced two
# identical edges and therefore duplicate closure rows.

# 2. Edges from GTF gene_id / transcript_id (after gene/transcript rows have
#    been synthesized). The transcript->child edge is for any feature with a
#    transcript_id attribute. The gene->transcript edge is for any feature with
#    featuretype='transcript'.
EDGES_FROM_GTF = """
INSERT INTO edges (parent, child)
SELECT a.value AS parent, a.feature_id AS child
FROM attributes a
JOIN features f ON f.id = a.feature_id
WHERE a.key = 'transcript_id'
  AND f.featuretype <> 'transcript'
  AND a.value <> f.id        -- guard: don't create self-loops
  AND EXISTS (SELECT 1 FROM features p WHERE p.id = a.value)
UNION ALL
SELECT a.value AS parent, a.feature_id AS child
FROM attributes a
JOIN features f ON f.id = a.feature_id
WHERE a.key = 'gene_id'
  AND f.featuretype = 'transcript'
  AND a.value <> f.id
  AND EXISTS (SELECT 1 FROM features p WHERE p.id = a.value);
"""


# 3. GTF transcript synthesis. One scan with GROUP BY transcript_id over the
#    subfeature rows (typically 'exon'). Replaces 250k+ MIN/MAX queries from
#    the legacy gffutils with a single vectorized aggregation.
GTF_SYNTHESIZE_TRANSCRIPTS = """
INSERT INTO features (id, seqid, source, featuretype, start, "end",
                      score, strand, frame,
                      attributes_blob, extra_blob, file_order, is_synthetic,
                      raw_id)
SELECT
    a.value                    AS id,
    ANY_VALUE(f.seqid)         AS seqid,
    'gffbase_derived'        AS source,
    'transcript'               AS featuretype,
    MIN(f.start)               AS start,
    MAX(f."end")               AS "end",
    '.'                        AS score,
    ANY_VALUE(f.strand)        AS strand,
    '.'                        AS frame,
    NULL                       AS attributes_blob,
    NULL                       AS extra_blob,
    NULL                       AS file_order,
    TRUE                       AS is_synthetic,
    -- A synthesized row's id came from a `transcript_id`/`gene_id`
    -- attribute, so raw_id is that same value; leaving it NULL would
    -- make the multipart resolve pass group every synthetic feature
    -- together.
    a.value                    AS raw_id
FROM features f
JOIN attributes a ON a.feature_id = f.id AND a.key = 'transcript_id'
WHERE f.featuretype = ?                          -- subfeature, e.g. 'exon'
  AND a.value NOT IN (SELECT id FROM features)
GROUP BY a.value;
"""

# 3b. Mirror the synthesized transcript_id back into the attributes table so
#     subsequent gene synthesis sees them.
GTF_SYNTHESIZE_TRANSCRIPT_ATTRS = """
INSERT INTO attributes (feature_id, key, value, idx)
SELECT f.id, 'transcript_id', f.id, 0
FROM features f
WHERE f.featuretype = 'transcript' AND f.is_synthetic = TRUE;
"""

# 3c. Carry gene_id forward onto each synthesized transcript by joining
#     attributes directly: each subfeature row carries (transcript_id, gene_id)
#     pairs in the attributes table; we group by transcript and pick the most
#     common gene_id (handles inconsistent GTFs gracefully). Crucially this
#     does NOT depend on the edges table, so we can defer edge population to
#     a single pass after all synthesis completes — eliminating duplicate rows.
GTF_PROPAGATE_GENE_ID = """
INSERT INTO attributes (feature_id, key, value, idx)
WITH pairs AS (
    SELECT
        tid.value AS transcript_id,
        gid.value AS gene_id,
        COUNT(*) AS cnt
    FROM attributes tid
    JOIN attributes gid ON gid.feature_id = tid.feature_id
    WHERE tid.key = 'transcript_id'
      AND gid.key = 'gene_id'
    GROUP BY tid.value, gid.value
),
ranked AS (
    SELECT
        transcript_id, gene_id, cnt,
        ROW_NUMBER() OVER (PARTITION BY transcript_id ORDER BY cnt DESC, gene_id) AS rn
    FROM pairs
)
SELECT t.id, 'gene_id', r.gene_id, 0
FROM features t
JOIN ranked r ON r.transcript_id = t.id AND r.rn = 1
WHERE t.featuretype = 'transcript' AND t.is_synthetic = TRUE;
"""

# 4. GTF gene synthesis. Same shape as transcript synthesis, but groups over
#    all rows whose featuretype IN ('transcript', subfeature) carrying a
#    gene_id attribute.
GTF_SYNTHESIZE_GENES = """
INSERT INTO features (id, seqid, source, featuretype, start, "end",
                      score, strand, frame,
                      attributes_blob, extra_blob, file_order, is_synthetic,
                      raw_id)
SELECT
    a.value                    AS id,
    ANY_VALUE(f.seqid)         AS seqid,
    'gffbase_derived'        AS source,
    'gene'                     AS featuretype,
    MIN(f.start)               AS start,
    MAX(f."end")               AS "end",
    '.'                        AS score,
    ANY_VALUE(f.strand)        AS strand,
    '.'                        AS frame,
    NULL                       AS attributes_blob,
    NULL                       AS extra_blob,
    NULL                       AS file_order,
    TRUE                       AS is_synthetic,
    -- A synthesized row's id came from a `transcript_id`/`gene_id`
    -- attribute, so raw_id is that same value; leaving it NULL would
    -- make the multipart resolve pass group every synthetic feature
    -- together.
    a.value                    AS raw_id
FROM features f
JOIN attributes a ON a.feature_id = f.id AND a.key = 'gene_id'
WHERE f.featuretype IN ('transcript', ?)        -- transcript + subfeature
  AND a.value NOT IN (SELECT id FROM features)
GROUP BY a.value;
"""


# ---------------------------------------------------------------------------
# SQLite-compat views (Phase 2 §6). Make `FeatureDB.execute()` accept queries
# written against the legacy gffutils schema names. The `attributes` column
# is the raw col-9 bytes (NOT JSON) — documented break.
# ---------------------------------------------------------------------------
COMPAT_VIEWS_SQL = """
CREATE OR REPLACE VIEW features_compat AS
    -- Built on `segments_all`, not on `features`: gffutils' `features` table
    -- holds one row per physical input line, because gffutils cannot represent
    -- a discontinuous feature at all. While no feature is multipart the two
    -- definitions are identical row-for-row (a test asserts it), and once one
    -- is, the physical shape is the one a query written against gffutils
    -- expects.
    SELECT feature_id AS id, seqid, source, featuretype, start, "end",
           score, strand, frame,
           CAST(attributes_blob AS VARCHAR) AS attributes,
           CAST(extra_blob      AS VARCHAR) AS extra,
           0 AS bin
    FROM segments_all;

CREATE OR REPLACE VIEW relations_compat AS
    SELECT ancestor AS parent, descendant AS child, depth AS level
    FROM closure;
"""


# ---------------------------------------------------------------------------
# Recursive CTE closure (Phase 2 §3.1) — replaces the N+1 grandchild loop.
# Single SQL statement; vectorized executor.
# ---------------------------------------------------------------------------
CLOSURE_RECURSIVE_CTE = """
INSERT INTO closure (ancestor, descendant, depth)
WITH RECURSIVE walk(ancestor, descendant, depth, seen) AS (
    SELECT parent, child, 1 AS depth, [parent, child] AS seen FROM edges
    UNION ALL
    SELECT w.ancestor, e.child, w.depth + 1, list_append(w.seen, e.child)
    FROM walk w
    JOIN edges e ON e.parent = w.descendant
    -- Cycle-safe: a node never appears twice on one path, so a cyclic
    -- `Parent` graph terminates at the cycle instead of re-entering it once
    -- per level. GFF3 does not forbid writing one, and without this a
    -- two-feature cycle made `children()` return 64 rows -- the same handful
    -- of features over and over, one copy per lap.
    --
    -- Free on well-formed data: in a DAG no node can repeat on a path, so the
    -- filter never fires. Verified identical on the FlyBase 50k corpus (31 230
    -- closure rows either way) for 17 ms more over 19 746 edges.
    WHERE w.depth < ? AND NOT list_contains(w.seen, e.child)
)
SELECT DISTINCT ancestor, descendant, depth FROM walk;
"""
# DISTINCT on the final projection, not on the recursive step, which must stay
# UNION ALL to terminate. GFF3 permits a DAG -- a feature may name several
# `Parent`s -- so the same descendant can be reachable by two paths of equal
# length, and `UNION ALL` emitted one closure row per path. `children(g, level=2)`
# then returned five features where only three were distinct.
#
# gffutils never had this: its `relations` table declares
# PRIMARY KEY (parent, child, level), so it deduplicates by construction.


# ---------------------------------------------------------------------------
# Cycle detection.
#
# Run after the closure so it can be answered by a join rather than another
# traversal: an edge closes a cycle exactly when its child already reaches its
# parent. Self-edges are checked directly, since the closure walk now refuses
# to emit them.
# ---------------------------------------------------------------------------
FIND_PARENT_CYCLES = """
SELECT DISTINCT e.parent, e.child FROM edges e
WHERE e.parent = e.child
   OR EXISTS (SELECT 1 FROM closure c
              WHERE c.ancestor = e.child AND c.descendant = e.parent)
"""
