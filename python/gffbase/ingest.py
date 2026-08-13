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
"""Phase 4 ingestion engine.

Streams the Rust parser's output through PyArrow record batches into DuckDB,
then runs a fixed sequence of set-based SQL passes for normalization, GTF
synthesis, transitive closure, and indexing. No per-feature Python loops in
the hot path — everything that scales with feature count goes through Arrow
or pure SQL.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass

import duckdb
import pyarrow as pa

from gffbase import parser as _parser
from gffbase._dbutil import scalar
from gffbase._options import IdSpecResolver, IngestOptions, _FeatureAdapter
from gffbase.exceptions import DuplicateIDError, MultipartConstraintError
from gffbase.feature import ParsedFeature
from gffbase.schema import (
    CLOSURE_RECURSIVE_CTE,
    COMPAT_VIEWS_SQL,
    DDL,
    EDGES_FROM_GTF,
    EDGES_FROM_PARENT,
    GTF_PROPAGATE_GENE_ID,
    GTF_SYNTHESIZE_GENES,
    GTF_SYNTHESIZE_TRANSCRIPT_ATTRS,
    GTF_SYNTHESIZE_TRANSCRIPTS,
    POST_LOAD_INDEXES,
    SCHEMA_VERSION,
    SEGMENTS_ALL_VIEW,
)

_log = logging.getLogger("gffbase.ingest")

DEFAULT_BATCH_SIZE = 50_000
DEFAULT_MAX_DEPTH = 8


@dataclass
class IngestStats:
    """Reported back to the caller for benchmarking and tests."""

    n_features_raw: int = 0
    n_features_synthetic_transcripts: int = 0
    n_features_synthetic_genes: int = 0
    n_attributes: int = 0
    n_edges: int = 0
    n_closure_rows: int = 0
    rtree_built: bool = False
    fmt: str = "gff3"
    dialect: dict = None  # type: ignore[assignment]
    directives: list[str] = None  # type: ignore[assignment]
    #: Specification violations the parser tolerated. Non-empty only under
    #: `mode="compat"`, where a violating record is kept and annotated rather
    #: than rejected -- so the caller gets gffutils' data plus a diagnostic
    #: gffutils never offered.
    warnings: list[dict] = None  # type: ignore[assignment]
    #: Records dropped by a `transform` callback or by `merge_strategy`.
    n_skipped: int = 0
    #: Features assembled from more than one input line. Non-zero only under
    #: `mode="strict"`, the only mode that fuses.
    n_multipart: int = 0


# ---------------------------------------------------------------------------
# Arrow batch builder. Accumulates ParsedFeature output until a fixed row
# count, then yields one PyArrow Table per category (features, attributes).
# Edges and directives are derived later by SQL.
# ---------------------------------------------------------------------------


class _ArrowBatchBuilder:
    """Accumulates parsed features into column-oriented Python lists, then
    produces PyArrow tables on flush. We deliberately keep the schema explicit
    so DuckDB sees the right column types (no INFER passes).

    Phase 19: the builder also stamps each row's ``seqid_y`` value during
    ``append()`` using a shared ``seqid_to_y`` dict (lazy band assignment in
    encounter order). This eliminates two full-table ``UPDATE`` passes that
    used to dominate ingest wall time on real GFF3 corpora.
    """

    FEATURES_SCHEMA = pa.schema(
        [
            ("id", pa.string()),
            ("seqid", pa.string()),
            ("source", pa.string()),
            ("featuretype", pa.string()),
            ("start", pa.int64()),
            ("end", pa.int64()),
            ("score", pa.string()),
            ("strand", pa.string()),
            ("frame", pa.string()),
            ("attributes_blob", pa.binary()),
            ("extra_blob", pa.binary()),
            ("file_order", pa.int64()),
            ("is_synthetic", pa.bool_()),
            ("raw_id", pa.string()),
            ("occ", pa.int32()),
            ("id_origin", pa.string()),
            ("seqid_y", pa.int64()),
        ]
    )

    ATTRIBUTES_SCHEMA = pa.schema(
        [
            ("feature_id", pa.string()),
            ("key", pa.string()),
            ("value", pa.string()),
            ("idx", pa.int16()),
        ]
    )

    def __init__(self, seqid_to_y: dict, has_spatial: bool = False):
        # Shared across all batches so seqid_y assignment is stable for the
        # whole file. Caller owns the dict; we mutate it in place.
        self._seqid_to_y = seqid_to_y
        self._has_spatial = has_spatial
        self._reset()

    def _reset(self):
        # Feature columns
        self.f_id: list = []
        self.f_seqid: list = []
        self.f_source: list = []
        self.f_type: list = []
        self.f_start: list = []
        self.f_end: list = []
        self.f_score: list = []
        self.f_strand: list = []
        self.f_frame: list = []
        self.f_blob: list = []
        self.f_extra: list = []
        self.f_order: list = []
        self.f_synth: list = []
        self.f_raw_id: list = []
        self.f_occ: list = []
        self.f_id_origin: list = []
        self.f_seqid_y: list = []
        # Attribute columns
        self.a_fid: list = []
        self.a_key: list = []
        self.a_val: list = []
        self.a_idx: list = []

    def append(
        self,
        feat_id: str,
        feat: ParsedFeature,
        file_order: int,
        raw_id: str | None = None,
        occ: int = 0,
        id_origin: str = "attribute",
    ):
        """Stage one feature.

        `raw_id` is the id as derived from column 9 *before* duplicate
        resolution renamed it; it defaults to `feat_id`, which is correct
        whenever no rename happened. `(raw_id, occ)` is the surrogate identity
        the multipart resolve pass groups on.
        """
        seqid = feat.seqid
        # Lazy y-band assignment: each new seqid gets the next slot.
        # `dict.get` + assignment is faster than `setdefault` here because
        # we hit the cache path on >99 % of rows in real data.
        y = self._seqid_to_y.get(seqid)
        if y is None:
            y = len(self._seqid_to_y) * SEQID_Y_BAND
            self._seqid_to_y[seqid] = y
        self.f_id.append(feat_id)
        self.f_seqid.append(seqid)
        self.f_source.append(feat.source)
        self.f_type.append(feat.featuretype)
        # `None` is carried through, not coerced. A `.` coordinate is legal GFF
        # and the distinction is only losable once.
        self.f_start.append(feat.start)
        self.f_end.append(feat.end)
        self.f_score.append(feat.score)
        self.f_strand.append(feat.strand)
        self.f_frame.append(feat.frame)
        self.f_blob.append(feat.attributes_blob)
        self.f_extra.append(("\t".join(feat.extra)).encode("utf-8") if feat.extra else b"")
        self.f_order.append(file_order)
        self.f_synth.append(False)
        self.f_raw_id.append(feat_id if raw_id is None else raw_id)
        self.f_occ.append(occ)
        self.f_id_origin.append(id_origin)
        self.f_seqid_y.append(y)
        for k, v, idx in feat.attributes_pairs:
            self.a_fid.append(feat_id)
            self.a_key.append(k)
            self.a_val.append(v)
            self.a_idx.append(idx)

    def __len__(self) -> int:
        return len(self.f_id)

    def features_table(self) -> pa.Table:
        return pa.table(
            {
                "id": self.f_id,
                "seqid": self.f_seqid,
                "source": self.f_source,
                "featuretype": self.f_type,
                "start": self.f_start,
                "end": self.f_end,
                "score": self.f_score,
                "strand": self.f_strand,
                "frame": self.f_frame,
                "attributes_blob": self.f_blob,
                "extra_blob": self.f_extra,
                "file_order": self.f_order,
                "is_synthetic": self.f_synth,
                "raw_id": self.f_raw_id,
                "occ": self.f_occ,
                "id_origin": self.f_id_origin,
                "seqid_y": self.f_seqid_y,
            },
            schema=self.FEATURES_SCHEMA,
        )

    def attributes_table(self) -> pa.Table:
        return pa.table(
            {
                "feature_id": self.a_fid,
                "key": self.a_key,
                "value": self.a_val,
                "idx": self.a_idx,
            },
            schema=self.ATTRIBUTES_SCHEMA,
        )

    @classmethod
    def _staging_columns(cls, schema: pa.Schema) -> str:
        """The staged column list, quoted for SQL, in Arrow field order.

        Derived from the Arrow schema rather than written out again, so adding
        a column in one place cannot silently misalign the INSERT. The
        `SELECT *` this replaced was purely positional: `raw_id`, `occ` and
        `id_origin` landing in the wrong order would have written an id into
        `seqid_y` without any error.
        """
        return ", ".join(_quote(name) for name in schema.names)

    def flush_into(self, con: duckdb.DuckDBPyConnection):
        if not self.f_id:
            return
        feats = self.features_table()
        attrs = self.attributes_table()
        # Register and INSERT ... SELECT — DuckDB's fastest Arrow path. When
        # the spatial extension is loaded we ALSO compute `bbox` inline so
        # the R-tree build at the end of ingest is a single CREATE INDEX
        # (no UPDATE pass over the table).
        con.register("__staging_features", feats)
        con.register("__staging_attributes", attrs)
        fcols = self._staging_columns(self.FEATURES_SCHEMA)
        if self._has_spatial:
            con.execute(
                f"INSERT INTO features ({fcols}, bbox) "
                f"SELECT {fcols}, "
                # A null coordinate yields a null envelope rather than
                # failing the insert; such rows are then absent from R-tree
                # results, which matches gffutils (where `NULL <= ?` is
                # unknown) and the B-tree path.
                'CASE WHEN start IS NULL OR "end" IS NULL THEN NULL '
                'ELSE ST_MakeEnvelope(start, seqid_y, "end", seqid_y + 1) END '
                "FROM __staging_features"
            )
        else:
            con.execute(f"INSERT INTO features ({fcols}) SELECT {fcols} FROM __staging_features")
        acols = self._staging_columns(self.ATTRIBUTES_SCHEMA)
        con.execute(f"INSERT INTO attributes ({acols}) SELECT {acols} FROM __staging_attributes")
        con.unregister("__staging_features")
        con.unregister("__staging_attributes")
        self._reset()


# ---------------------------------------------------------------------------
# ID resolution. Pulled out so the bulk loop has zero branches that hit Python
# attribute parsing twice.
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Duplicate-ID resolution.
# ---------------------------------------------------------------------------

#: The eight non-attribute GFF columns `merge_strategy="merge"` compares.
#: Column 9 is excluded by definition -- merging attributes is the point.
_MERGE_COMPARE_FIELDS = (
    "seqid",
    "source",
    "featuretype",
    "start",
    "end",
    "score",
    "strand",
    "frame",
)


def _unwrap_transformed(result, original):
    """Return the ParsedFeature a transform callback produced.

    gffutils transforms mutate and return the Feature they were handed. Ours
    are handed a `_FeatureAdapter` view, so unwrap it back to the underlying
    ParsedFeature; anything else is returned as-is so a transform may also
    build a feature from scratch.
    """
    inner = getattr(result, "_parsed", None)
    return inner if inner is not None else (result if result is not original else original)


# ---------------------------------------------------------------------------
# Multipart resolution.
#
# GFF3 lets one logical feature span several lines sharing an `ID` -- how NCBI
# represents a split CDS. Under `mode="strict"` those lines are loaded with a
# surrogate id each and then FUSED here into one logical feature carrying a
# `segments` row per line.
#
# This runs before `EDGES_FROM_PARENT` and before GTF synthesis, so edges and
# the closure are built once against final logical ids and never need
# rewriting, and synthesized rows are never duplicate candidates.
# ---------------------------------------------------------------------------

#: Separator in the surrogate id a strict-mode duplicate is loaded under. 0x1F
#: (ASCII unit separator) cannot appear in a GFF attribute value, so a
#: surrogate can never collide with a real id -- and any that survives the
#: resolve pass is a bug loud enough to assert on.
_SURROGATE_SEP = "\x1f"


def _surrogate_id(raw_id: str, occ: int) -> str:
    return f"{raw_id}{_SURROGATE_SEP}{occ}"


#: The columns GFF3 requires the segments of a discontinuous feature to share.
#: Coordinates, score and phase deliberately may differ -- per-segment phase is
#: the whole reason the `segments` table exists -- and attributes are unioned.
_MULTIPART_KEY = ("seqid", "source", "featuretype", "strand")


def _multipart_runs(con) -> list[tuple]:
    """Groups of loaded rows sharing one `raw_id`, with the predicate evaluated.

    One aggregate query rather than a query per candidate: the FlyBase 50k
    fixture alone has 345 runs, and a real annotation file has thousands.
    """
    distinct = ", ".join(f"COUNT(DISTINCT {col}) AS n_{col}" for col in _MULTIPART_KEY)
    return con.execute(
        f"""
        SELECT raw_id, COUNT(*) AS n, MIN(file_order) AS first_line, {distinct}
        FROM features
        WHERE raw_id IS NOT NULL AND is_synthetic = FALSE
        GROUP BY raw_id
        HAVING COUNT(*) > 1
        ORDER BY MIN(file_order)
        """
    ).fetchall()


def _describe_conflict(con, raw_id: str) -> str:
    """Name the diverging column and both line numbers, for the exception.

    A `MultipartConstraintError` is something the user has to act on -- fix the
    file, or pass `on_multipart_conflict="split"` -- so "these lines conflict"
    is not enough; it has to say which column and where.
    """
    rows = con.execute(
        f"SELECT file_order, {', '.join(_MULTIPART_KEY)} FROM features "
        "WHERE raw_id = ? ORDER BY file_order",
        [raw_id],
    ).fetchall()
    first = rows[0]
    for other in rows[1:]:
        for i, col in enumerate(_MULTIPART_KEY, start=1):
            if first[i] != other[i]:
                return (
                    f"{col} differs ({first[i]!r} on line {first[0]} vs "
                    f"{other[i]!r} on line {other[0]})"
                )
    return "columns differ"  # pragma: no cover - only reachable if the predicate lied


def _free_autoincrement(con, base: str, autoinc: dict) -> str:
    """An autoincremented name that no loaded feature already claims.

    `_autoincrement` only guarantees uniqueness against its own counters, and
    this runs after the bulk load -- so a file that literally contains `x_1`
    would otherwise get a second row claiming it and violate the primary key.
    """
    while True:
        candidate = IdSpecResolver._autoincrement(base, autoinc)
        taken = scalar(
            con,
            "SELECT COUNT(*) FROM features WHERE id = ? OR raw_id = ?",
            [candidate, candidate],
        )
        if not taken:
            return candidate


def _split_conflicting_run(con, raw_id: str, autoinc: dict) -> int:
    """Partition a conflicting run by its constraint key.

    The lowest `file_order` keeps the bare id; every other distinct key gets an
    autoincremented one. Rows that DO share a key stay together, so a file with
    a genuine split CDS plus one stray line still fuses the CDS.
    """
    keys = con.execute(
        f"SELECT {', '.join(_MULTIPART_KEY)}, MIN(file_order) AS first_line "
        "FROM features WHERE raw_id = ? "
        f"GROUP BY {', '.join(_MULTIPART_KEY)} ORDER BY MIN(file_order)",
        [raw_id],
    ).fetchall()
    renamed = 0
    for pos, row in enumerate(keys):
        if pos == 0:
            continue  # lowest file_order keeps the bare id
        new_raw = _free_autoincrement(con, raw_id, autoinc)
        predicate = " AND ".join(f"{col} = ?" for col in _MULTIPART_KEY)
        con.execute(
            f"UPDATE features SET raw_id = ? WHERE raw_id = ? AND {predicate}",
            [new_raw, raw_id, *row[: len(_MULTIPART_KEY)]],
        )
        renamed += 1
    return renamed


def _canonicalize_surrogates(con) -> None:
    """Give the first row of every `raw_id` group its bare id back.

    Duplicate rows are loaded under a surrogate id so they can coexist under
    the primary key until the resolve pass can see the whole run. Fusion then
    deletes all but the first row of each group -- but only the ORIGINAL first
    row already held the bare id. After a split, a group's new first row is
    still wearing a surrogate, and fusing would make that surrogate the logical
    id of a real feature.

    So this runs before fusion. Anything still surrogate afterwards is a row
    fusion is about to delete.
    """
    renames = con.execute(
        """
        SELECT id, raw_id FROM (
            SELECT id, raw_id,
                   ROW_NUMBER() OVER (PARTITION BY raw_id ORDER BY file_order, id) AS rn
            FROM features
        )
        WHERE rn = 1 AND id <> raw_id
        """
    ).fetchall()
    for surrogate, bare in renames:
        con.execute("UPDATE features SET id = ? WHERE id = ?", [bare, surrogate])
        con.execute("UPDATE attributes SET feature_id = ? WHERE feature_id = ?", [bare, surrogate])


def _fuse_multipart(con, raw_ids: list[str], has_spatial: bool) -> int:
    """Collapse each run into one logical feature plus its `segments` rows.

    Set-based over every run at once. The surviving row is the one with the
    lowest `file_order`; it keeps the bare id, and its coordinates widen to the
    envelope so the existing R-tree and every v1 query shape stay correct.
    """
    con.execute("DROP TABLE IF EXISTS __mp_rows")
    con.execute(
        """
        CREATE TEMP TABLE __mp_rows AS
        SELECT
            f.id, f.raw_id, f.start, f."end", f.score, f.frame,
            f.attributes_blob, f.extra_blob, f.file_order, f.seqid_y,
            ROW_NUMBER() OVER w - 1                    AS seg_idx,
            FIRST_VALUE(f.id)              OVER w      AS logical_id,
            FIRST_VALUE(f.attributes_blob) OVER w      AS seg0_blob
        FROM features f
        JOIN (SELECT UNNEST(?::VARCHAR[]) AS raw_id) k ON k.raw_id = f.raw_id
        WINDOW w AS (PARTITION BY f.raw_id ORDER BY f.file_order, f.id)
        """,
        [raw_ids],
    )

    # Segment 0 is stored too, even though `features` carries its blob: the
    # feature row's coordinates become the envelope, so segment 0's own
    # coordinates need somewhere to live.
    con.execute(
        """
        INSERT INTO segments (feature_id, seg_idx, start, "end", score, frame,
                              attributes_blob, extra_blob, file_order,
                              attrs_same_as_seg0, seqid_y)
        SELECT logical_id, seg_idx, start, "end", score, frame,
               COALESCE(attributes_blob, ''::BLOB), extra_blob, file_order,
               attributes_blob IS NOT DISTINCT FROM seg0_blob, seqid_y
        FROM __mp_rows
        """
    )

    # Widen the survivor to the envelope.
    con.execute(
        """
        UPDATE features SET
            start      = agg.min_start,
            "end"      = agg.max_end,
            n_segments = agg.n
        FROM (
            SELECT logical_id, MIN(start) AS min_start, MAX("end") AS max_end,
                   COUNT(*) AS n
            FROM __mp_rows GROUP BY logical_id
        ) agg
        WHERE features.id = agg.logical_id
        """
    )
    if has_spatial:
        con.execute(
            """
            UPDATE features SET bbox = CASE
                WHEN start IS NULL OR "end" IS NULL THEN NULL
                ELSE ST_MakeEnvelope(start, seqid_y, "end", seqid_y + 1) END
            WHERE id IN (SELECT DISTINCT logical_id FROM __mp_rows)
            """
        )

    # Attribute rows. Where a segment repeats segment 0's column 9 byte for
    # byte -- which NCBI does on every CDS line -- its rows are redundant and
    # are dropped, so the table stays the size it was in v1. Where it differs,
    # they are re-pointed at the logical id and tagged with the owning segment.
    con.execute(
        """
        DELETE FROM attributes WHERE feature_id IN (
            SELECT id FROM __mp_rows
            WHERE seg_idx > 0 AND attributes_blob IS NOT DISTINCT FROM seg0_blob
        )
        """
    )
    con.execute(
        """
        UPDATE attributes SET feature_id = r.logical_id, seg_idx = r.seg_idx
        FROM __mp_rows r
        WHERE attributes.feature_id = r.id AND r.seg_idx > 0
        """
    )

    con.execute("DELETE FROM features WHERE id IN (SELECT id FROM __mp_rows WHERE seg_idx > 0)")
    n = scalar(con, "SELECT COUNT(DISTINCT logical_id) FROM __mp_rows")
    con.execute("DROP TABLE __mp_rows")
    return int(n)


def _record_id_conflicts(con, rows: list[tuple]) -> None:
    if not rows:
        return
    con.executemany(
        "INSERT INTO id_conflicts (raw_id, resolved_id, kind, file_order, detail) "
        "VALUES (?, ?, ?, ?, ?)",
        rows,
    )


def resolve_multipart(con, options, autoinc: dict, has_spatial: bool) -> int:
    """Fuse duplicate-id runs into discontinuous features. Returns the count.

    Skipped entirely when no `raw_id` collided -- the case for every GTF corpus
    and for GENCODE GFF3 -- so the cost on a file without discontinuous
    features is one aggregate query that returns no rows.
    """
    # Compat mode must not fuse anything, ever. `merge_strategy` has already
    # had the final say there, and gffutils' own `merge` requires all eight
    # non-attribute columns to match -- so it never merges a genuine split
    # feature, and neither may we while claiming to be a drop-in.
    #
    # Without this guard, `merge_strategy="create_unique"` fused the very rows
    # it had just been asked to keep separate: `dup`, `dup_1` and `dup_2` all
    # still share `raw_id = 'dup'`, which is exactly what the resolve pass
    # groups on.
    if not options.fuses_multipart:
        return 0

    runs = _multipart_runs(con)
    if not runs:
        return 0

    fuse: list[str] = []
    conflicts: list[str] = []
    for raw_id, _n, _first_line, *distinct_counts in runs:
        if all(c == 1 for c in distinct_counts):
            fuse.append(raw_id)
        else:
            conflicts.append(raw_id)

    if conflicts and options.on_multipart_conflict == "error":
        raw_id = conflicts[0]
        raise MultipartConstraintError(
            f"lines sharing ID {raw_id!r} cannot form one discontinuous feature: "
            f"{_describe_conflict(con, raw_id)}. GFF3 requires the segments of a "
            "discontinuous feature to share seqid, source, featuretype and strand. "
            'Pass on_multipart_conflict="split" to keep them as separate features.'
        )

    conflict_rows: list[tuple] = []
    for raw_id in conflicts:
        detail = _describe_conflict(con, raw_id)
        n_new = _split_conflicting_run(con, raw_id, autoinc)
        conflict_rows.append((raw_id, raw_id, "strict_split", None, detail))
        _log.info("split %d conflicting id group(s) for %r: %s", n_new, raw_id, detail)

    # Splitting reshuffles the groups: some become singletons, and the ones
    # that remain runs may now satisfy the predicate. Re-derive both.
    if conflicts:
        _canonicalize_surrogates(con)
        fuse = [
            raw_id
            for raw_id, _n, _fl, *counts in _multipart_runs(con)
            if all(c == 1 for c in counts)
        ]

    n_fused = _fuse_multipart(con, fuse, has_spatial) if fuse else 0
    conflict_rows.extend((raw_id, raw_id, "multipart", None, None) for raw_id in fuse)
    _record_id_conflicts(con, conflict_rows)

    # A surrogate id is an internal artifact of loading duplicates under a
    # primary key. Every one of them must have been either renamed back or
    # deleted by now; one that escaped would reach users as a feature id with a
    # control character in it.
    leaked = scalar(con, "SELECT COUNT(*) FROM features WHERE id LIKE '%' || chr(31) || '%'")
    if leaked:  # pragma: no cover - defensive; a leak is a bug in this pass
        raise AssertionError(
            f"{leaked} surrogate id(s) survived multipart resolution; this is a gffbase bug"
        )
    return n_fused


def _resolve_deferred_duplicates(con, deferred, options, autoinc, builder, seqid_to_y, fmt):
    """Apply `merge` / `replace` to rows held back during the bulk load.

    Both strategies need to see the row already in the database, so they
    cannot be decided while streaming. Duplicates are a small fraction of any
    real corpus, so resolving them row-by-row here is cheap; the bulk path
    stays set-based.

    Returns the (original_id, new_id) pairs to record in `duplicates`.
    """
    new_duplicates: list[tuple[str, str]] = []
    strategy = options.merge_strategy
    compare = [f for f in _MERGE_COMPARE_FIELDS if f not in options.force_merge_fields]

    for fid, feat, file_order in deferred:
        if strategy == "replace":
            # Last one wins: drop the incumbent and insert this row under the
            # same id.
            con.execute("DELETE FROM attributes WHERE feature_id = ?", [fid])
            con.execute("DELETE FROM features WHERE id = ?", [fid])
            _insert_single(con, builder, seqid_to_y, fid, feat, file_order)
            continue

        # strategy == "merge"
        row = con.execute(
            f"SELECT {', '.join(_quote(f) for f in _MERGE_COMPARE_FIELDS)} "
            "FROM features WHERE id = ?",
            [fid],
        ).fetchone()
        existing = dict(zip(_MERGE_COMPARE_FIELDS, row, strict=True)) if row else {}

        same = bool(existing) and all(
            _as_text(existing[f]) == _as_text(getattr(feat, f)) for f in compare
        )
        if not same:
            # gffutils falls back to create_unique when the other columns
            # differ, and records the rename in `duplicates`.
            new_id = IdSpecResolver._autoincrement(fid, autoinc)
            _insert_single(con, builder, seqid_to_y, new_id, feat, file_order)
            new_duplicates.append((fid, new_id))
            continue

        # Same everywhere else: fold this row's attributes into the incumbent.
        for key, value, idx in feat.attributes_pairs:
            already = scalar(
                con,
                "SELECT COUNT(*) FROM attributes WHERE feature_id = ? AND key = ? AND value = ?",
                [fid, key, value],
            )
            if not already:
                con.execute(
                    "INSERT INTO attributes (feature_id, key, value, idx) VALUES (?, ?, ?, ?)",
                    [fid, key, value, idx],
                )
        # `force_merge_fields` collapse to a sorted comma-joined string.
        for fld in options.force_merge_fields:
            merged = sorted({_as_text(existing[fld]), _as_text(getattr(feat, fld))})
            con.execute(
                f"UPDATE features SET {_quote(fld)} = ? WHERE id = ?",
                [",".join(merged), fid],
            )
        # The normalized rows are now the truth for this feature; bring the
        # raw blob back in line with them.
        _regenerate_attributes_blob(con, fid, fmt)
    return new_duplicates


def _regenerate_attributes_blob(con, feature_id: str, fmt: str) -> None:
    """Rebuild `features.attributes_blob` from the normalized attribute rows.

    A merged feature is the one case where the raw column-9 bytes stop being
    the truth: `merge_strategy="merge"` folds the incoming row's attributes
    into the existing feature's `attributes` rows, but the blob still holds
    only what the first line said. Since `Feature.attributes` reads the blob
    (that is the whole point of the byte-faithful fast path), the merge was
    invisible to every caller -- the table had both values and the feature
    reported one.
    """
    rows = con.execute(
        "SELECT key, value FROM attributes WHERE feature_id = ? ORDER BY rowid",
        [feature_id],
    ).fetchall()
    if not rows:
        return

    # Preserve first-seen key order and collapse repeats into one multi-valued
    # entry, which is how both engines' parsers present them.
    grouped: dict[str, list[str]] = {}
    for key, value in rows:
        grouped.setdefault(key, []).append(value)

    if fmt == "gtf":
        parts = [f'{k} "{v}"' for k, values in grouped.items() for v in values]
        blob = "; ".join(parts)
    else:
        blob = ";".join(f"{k}={','.join(values)}" for k, values in grouped.items())
    con.execute(
        "UPDATE features SET attributes_blob = ? WHERE id = ?",
        [blob.encode("utf-8"), feature_id],
    )


def _quote(field: str) -> str:
    """`end` is reserved in SQL, so it always needs quoting."""
    return '"end"' if field == "end" else field


def _as_text(value) -> str:
    return "" if value is None else str(value)


def _insert_single(con, builder, seqid_to_y, fid, feat, file_order):
    """Insert one feature through the same Arrow path as the bulk load."""
    builder.append(fid, feat, file_order)
    builder.flush_into(con)


# ---------------------------------------------------------------------------
# Public entry point.
# ---------------------------------------------------------------------------


def from_file(
    path: str,
    dbfn: str = ":memory:",
    *,
    options: IngestOptions | None = None,
    force: bool = False,
    batch_size: int = DEFAULT_BATCH_SIZE,
    max_depth: int = DEFAULT_MAX_DEPTH,
    disable_infer_genes: bool = False,
    disable_infer_transcripts: bool = False,
    gtf_subfeature: str = "exon",
    engine: str | None = "auto",
    build_rtree: bool = True,
) -> tuple[duckdb.DuckDBPyConnection, IngestStats]:
    """Ingest a GFF3 or GTF file into a DuckDB database.

    Returns the open connection plus an `IngestStats` summary. The connection
    is the canonical handle `FeatureDB` wraps.

    `options` carries the full `create_db` policy (id_spec, merge_strategy,
    transform, ...). The individual keyword arguments are the older, narrower
    interface and are folded into `options` when it is not supplied.
    """
    if options is None:
        options = IngestOptions(
            force=force,
            disable_infer_genes=disable_infer_genes,
            disable_infer_transcripts=disable_infer_transcripts,
            gtf_subfeature=gtf_subfeature,
        )
    force = options.force
    disable_infer_genes = options.disable_infer_genes
    disable_infer_transcripts = options.disable_infer_transcripts
    gtf_subfeature = options.gtf_subfeature

    if dbfn != ":memory:":
        if os.path.exists(dbfn) and not force:
            raise ValueError(f"{dbfn} already exists. Pass force=True to overwrite.")
        if os.path.exists(dbfn) and force:
            os.unlink(dbfn)

    con = duckdb.connect(dbfn)
    _apply_pragmas(con)
    con.execute(DDL)

    # Phase 19: load the spatial extension UPFRONT (was: lazy after bulk
    # load). This lets us widen the `features` schema to include `bbox`
    # and stamp the R-tree envelope inline during the Arrow batch INSERT,
    # eliminating two full-table UPDATE passes that used to dominate
    # ingest wall time.
    has_spatial = build_rtree and not _rtree_disabled_by_env() and _try_load_spatial(con)
    if has_spatial:
        con.execute("ALTER TABLE features ADD COLUMN IF NOT EXISTS bbox GEOMETRY")

    # Drive the parser.
    it = _parser.parse_gff(
        path,
        engine=engine,
        checklines=options.checklines,
        force_dialect_check=options.force_dialect_check,
        force_gff=options.force_gff,
        validation=options.resolved_mode.validation,
        strict=options.resolved_mode.raises,
    )
    seqid_to_y: dict = {}
    builder = _ArrowBatchBuilder(seqid_to_y, has_spatial=has_spatial)
    # Autoincrement counters, keyed by base. gffutils keeps the same table:
    # `<featuretype>_<n>` for a feature whose id_spec yields nothing, and
    # `<id>_<n>` for a `create_unique` collision.
    autoinc: dict = {}
    seen_ids: dict = {}
    duplicate_pairs: list = []  # (original_id, new_id) for the duplicates table
    # Rows held back for a post-load pass. Only `merge` and `replace` need
    # this -- the other three strategies decide at the point of collision --
    # so the memory cost is opt-in.
    deferred: list = []
    file_order = 0
    n_raw = 0
    n_skipped = 0
    # Resolve the dialect format ONCE — calling `it.dialect()` per record
    # is a Rust↔Python boundary cost we shouldn't pay 5 M times. The
    # parser commits to a dialect during the peek phase, so the value is
    # stable from the first yielded record onward.
    _fmt_cache: str | None = None
    resolver = None
    strategy = options.merge_strategy
    transform = options.transform
    fuse_multipart = options.fuses_multipart

    for feat in it:
        file_order += 1
        n_raw += 1
        if resolver is None:
            # The dialect is only knowable once the parser has committed to
            # one, which the Python engine does on first yield -- so this is
            # resolved on the first record and then reused, rather than paying
            # a Rust/Python boundary crossing per row.
            _fmt_cache = _dialect_fmt_safe(it)
            resolver = options.resolver_for(_fmt_cache)

        if transform is not None:
            # gffutils semantics: the transform may return a replacement
            # feature, or anything falsy to drop the record entirely.
            result = transform(_FeatureAdapter(feat))
            if not result:
                n_skipped += 1
                continue
            if result is not True:
                feat = _unwrap_transformed(result, feat)

        fid, origin = resolver.resolve(feat, autoinc)
        # `(raw_id, occ)` is the schema-v2 surrogate identity: the id column 9
        # yielded, plus how many lines before this one yielded the same. It is
        # what the multipart resolve pass groups on, and it is free here --
        # `seen_ids` already had to know, it just recorded a bare `True`.
        raw_id = fid
        occ = seen_ids.get(fid, 0)

        if occ:
            if fuse_multipart:
                # Strict mode: a duplicate id is a CANDIDATE discontinuous
                # feature, not yet an error. Load it under a surrogate id and
                # let the resolve pass decide, once it can see the whole run.
                # The surrogate deliberately does not go through
                # `_autoincrement`: these rows are either deleted (fused) or
                # renamed (split), so polluting the `autoincrements` table with
                # counters that get undone would misinform a later `update()`.
                fid = _surrogate_id(raw_id, occ)
            elif strategy == "error":
                raise DuplicateIDError(f"Duplicate ID {fid}")
            elif strategy == "warning":
                _log.warning("Duplicate lines in file for id '%s'; ignoring all but the first", fid)
                n_skipped += 1
                continue
            elif strategy == "create_unique":
                # Note: NO `duplicates` row here. gffutils only records a
                # rename when `merge` falls back to create_unique, because
                # that table exists to let a later merge find the sibling
                # rows -- and under plain create_unique there is nothing to
                # merge. Recording it anyway made `duplicates` disagree with
                # the oracle on every deduplicated file.
                fid = IdSpecResolver._autoincrement(fid, autoinc)
            else:
                # merge / replace: resolved after the bulk load, against the
                # row that is already in the database.
                deferred.append((fid, feat, file_order))
                continue
        # Count occurrences of the *raw* id, and separately mark any renamed id
        # as taken. Both entries matter: the count drives `occ`, and without the
        # second one a later line literally named `g1_1` would not be seen as
        # colliding with the rename that produced `g1_1`, which is a primary-key
        # violation rather than a resolution.
        seen_ids[raw_id] = occ + 1
        if fid != raw_id:
            seen_ids[fid] = 1
        builder.append(fid, feat, file_order, raw_id, occ, origin)
        if len(builder) >= batch_size:
            builder.flush_into(con)
    builder.flush_into(con)

    if deferred:
        duplicate_pairs.extend(
            _resolve_deferred_duplicates(
                con, deferred, options, autoinc, builder, seqid_to_y, _fmt_cache or "gff3"
            )
        )

    if duplicate_pairs:
        dup_tbl = pa.table(
            {
                "original_id": [b for b, _ in duplicate_pairs],
                "new_id": [n for _, n in duplicate_pairs],
            }
        )
        con.register("__staging_dups", dup_tbl)
        con.execute(
            "INSERT INTO duplicates (original_id, new_id) "
            "SELECT original_id, new_id FROM __staging_dups"
        )
        con.unregister("__staging_dups")

    # Multipart resolution, BEFORE edges, GTF synthesis and the
    # `autoincrements` write: edges and the closure are then built once against
    # final logical ids and never need rewriting, synthesized rows are never
    # duplicate candidates, and a `split` resolution's renames are reflected in
    # the counters that get persisted below.
    n_multipart = resolve_multipart(con, options, autoinc, has_spatial)

    # `autoincrements` records the counters so a later `update()` does not
    # reissue an id this build already handed out.
    if autoinc:
        ai_tbl = pa.table({"base": list(autoinc.keys()), "n": [int(v) for v in autoinc.values()]})
        con.register("__staging_autoinc", ai_tbl)
        con.execute("INSERT INTO autoincrements (base, n) SELECT base, n FROM __staging_autoinc")
        con.unregister("__staging_autoinc")

    dialect = it.dialect()
    directives = list(it.directives())
    fmt = (dialect or {}).get("fmt", "gff3")

    # Persist directives in one shot (set-based).
    if directives:
        dir_table = pa.table({"directive": directives})
        con.register("__staging_directives", dir_table)
        con.execute("INSERT INTO directives (directive) SELECT directive FROM __staging_directives")
        con.unregister("__staging_directives")

    # Set-based normalization passes.
    n_synth_t = 0
    n_synth_g = 0

    if fmt == "gtf":
        if not disable_infer_transcripts:
            n_synth_t = _synthesize_transcripts(con, gtf_subfeature)
        if not disable_infer_genes:
            n_synth_g = _synthesize_genes(con, gtf_subfeature)
        con.execute(EDGES_FROM_GTF)
        # GTF synthesis inserts new rows without seqid_y / bbox set. Patch
        # them up in a single targeted UPDATE (touches only synthesized
        # rows; ~4-9 % of features at GENCODE scale).
        if has_spatial:
            con.execute(
                "UPDATE features "
                "SET seqid_y = m.seqid_y, "
                "    bbox = ST_MakeEnvelope("
                "        features.start, m.seqid_y, "
                '        features."end", m.seqid_y + 1) '
                "FROM seqid_map m "
                "WHERE features.seqid = m.seqid AND features.seqid_y IS NULL"
            )
        else:
            con.execute(
                "UPDATE features SET seqid_y = m.seqid_y "
                "FROM seqid_map m "
                "WHERE features.seqid = m.seqid AND features.seqid_y IS NULL"
            )
    else:
        con.execute(EDGES_FROM_PARENT)

    # Closure via recursive CTE.
    con.execute(CLOSURE_RECURSIVE_CTE, [max_depth])

    # Indexes — only after all data is materialized.
    con.execute(POST_LOAD_INDEXES)

    # Optional R-tree. Phase 19: when spatial is loaded, this is now a
    # single CREATE INDEX over the bbox column we already populated
    # inline during the Arrow batch INSERTs (no UPDATE pass).
    rtree_built = False
    if has_spatial:
        rtree_built = _finalize_rtree(con, seqid_to_y)

    # SQLite-compat views (must run after closure has been populated).
    # `segments_all` first -- `features_compat` is defined on top of it.
    con.execute(SEGMENTS_ALL_VIEW)
    con.execute(COMPAT_VIEWS_SQL)

    # Stats.
    n_attributes = scalar(con, "SELECT COUNT(*) FROM attributes")
    n_edges = scalar(con, "SELECT COUNT(*) FROM edges")
    n_closure = scalar(con, "SELECT COUNT(*) FROM closure")

    # Meta — record dialect, fmt, and the rtree availability so a re-opened
    # DB can route queries correctly without probing.
    _write_meta(con, dialect, fmt, rtree_built=rtree_built, max_depth=max_depth)

    return con, IngestStats(
        n_features_raw=n_raw,
        n_features_synthetic_transcripts=n_synth_t,
        n_features_synthetic_genes=n_synth_g,
        n_attributes=n_attributes,
        n_edges=n_edges,
        n_closure_rows=n_closure,
        rtree_built=rtree_built,
        fmt=fmt,
        dialect=dialect,
        directives=directives,
        warnings=list(getattr(it, "warnings", []) or []),
        n_skipped=n_skipped,
        n_multipart=n_multipart,
    )


# ---------------------------------------------------------------------------
# Helpers.
# ---------------------------------------------------------------------------


def _dialect_fmt_safe(it) -> str:
    """The Rust iterator commits to a dialect after the peek phase. The
    Python fallback only sets it after the first record yields. Both are
    populated by the time we land in this function on the first feature."""
    try:
        d = it.dialect()
        if d:
            return d.get("fmt", "gff3")
    except Exception:
        pass
    return "gff3"


def _apply_pragmas(con: duckdb.DuckDBPyConnection):
    # DuckDB's defaults are excellent; we only nudge memory & threads. Anything
    # missing here is left to the caller via DUCKDB_THREADS env var.
    threads = os.environ.get("GFFUTILS2_THREADS")
    if threads:
        con.execute(f"PRAGMA threads = {int(threads)}")
    # Suppress the interactive progress bar — it floods stderr in batch and
    # subprocess scenarios and offers no value for benchmarking or scripting.
    try:
        con.execute("PRAGMA disable_progress_bar")
    except duckdb.Error:
        pass


def _synthesize_transcripts(con, subfeature: str) -> int:
    """Run the GROUP BY transcript synthesis. Returns rows inserted."""
    before = con.execute(
        "SELECT COUNT(*) FROM features WHERE featuretype = 'transcript'"
    ).fetchone()[0]
    con.execute(GTF_SYNTHESIZE_TRANSCRIPTS, [subfeature])
    after = con.execute(
        "SELECT COUNT(*) FROM features WHERE featuretype = 'transcript'"
    ).fetchone()[0]
    n = after - before
    # Make the synthesized transcripts visible to subsequent passes by giving
    # them a self-referential transcript_id attribute, then propagate the
    # gene_id from the children. The propagation joins attributes directly,
    # so no temporary edge inserts are needed (duplicate-edge-free).
    con.execute(GTF_SYNTHESIZE_TRANSCRIPT_ATTRS)
    con.execute(GTF_PROPAGATE_GENE_ID)
    return n


def _synthesize_genes(con, subfeature: str) -> int:
    before = con.execute("SELECT COUNT(*) FROM features WHERE featuretype = 'gene'").fetchone()[0]
    con.execute(GTF_SYNTHESIZE_GENES, [subfeature])
    after = con.execute("SELECT COUNT(*) FROM features WHERE featuretype = 'gene'").fetchone()[0]
    n = after - before
    # Mirror the synthesized gene_id into attributes so downstream queries
    # treat synthesized genes the same as authored ones.
    con.execute(
        """
        INSERT INTO attributes (feature_id, key, value, idx)
        SELECT f.id, 'gene_id', f.id, 0
        FROM features f
        WHERE f.featuretype = 'gene' AND f.is_synthetic = TRUE
        """
    )
    return n


SEQID_Y_BAND = 1_000_000  # gap between adjacent seqids' y-bands.


def _rtree_disabled_by_env() -> bool:
    """``GFFBASE_TEST_DISABLE_RTREE=1`` forces the B-tree fallback path
    library-wide so the CI matrix can exercise it without test-code
    changes."""
    return os.environ.get("GFFBASE_TEST_DISABLE_RTREE", "").lower() in ("1", "true", "yes")


def _try_load_spatial(con: duckdb.DuckDBPyConnection) -> bool:
    """Attempt to install + load the DuckDB spatial extension. Returns
    True iff it's now usable on this connection."""
    try:
        con.execute("INSTALL spatial")
        con.execute("LOAD spatial")
        return True
    except duckdb.Error:
        return False


def _finalize_rtree(con: duckdb.DuckDBPyConnection, seqid_to_y: dict) -> bool:
    """Persist the seqid_to_y dict into the `seqid_map` side table and
    create the R-tree index over the (already-populated) `bbox` column.

    Phase 19: this is the entire R-tree build — no UPDATE passes. The
    `bbox` column was filled in inline by ``_ArrowBatchBuilder.flush_into``
    using the per-row seqid_y stamped by the builder.
    """
    try:
        if seqid_to_y:
            seqid_rows = list(seqid_to_y.items())
            # Stable ordering (encounter order in the file) — preserves the
            # invariant that the first seqid sees seqid_y == 0.
            con.execute("DELETE FROM seqid_map")
            con.executemany(
                "INSERT INTO seqid_map(seqid, seqid_y) VALUES (?, ?)",
                seqid_rows,
            )
        con.execute("CREATE INDEX IF NOT EXISTS features_rtree ON features USING RTREE (bbox)")
        return True
    except duckdb.Error:
        return False


def _write_meta(
    con: duckdb.DuckDBPyConnection,
    dialect: dict,
    fmt: str,
    *,
    rtree_built: bool = False,
    max_depth: int = DEFAULT_MAX_DEPTH,
):
    import json

    # Closure max depth: used by FeatureDB's relational dispatcher (Phase 7) to
    # pick the cache vs. dynamic CTE without a per-call query.
    row = con.execute("SELECT MAX(depth) FROM closure").fetchone()
    closure_max_depth = int(row[0]) if row and row[0] is not None else 0
    # How many features were built from more than one input line. Stored rather
    # than computed per query because it gates whether the query builders emit
    # the segment-overlap conjunct at all: at zero -- the case for every GTF
    # corpus, all of GENCODE, and every database migrated from v1 -- they emit
    # SQL byte-identical to v1 and the segment machinery costs nothing.
    n_multipart = scalar(con, "SELECT COUNT(*) FROM features WHERE n_segments > 1")
    rows = [
        ("schema_version", SCHEMA_VERSION),
        ("dialect", json.dumps(dialect or {})),
        ("fmt", fmt),
        ("rtree_built", "true" if rtree_built else "false"),
        ("max_depth", str(int(max_depth))),
        ("closure_max_depth", str(closure_max_depth)),
        ("n_multipart", str(int(n_multipart))),
    ]
    con.executemany("INSERT OR REPLACE INTO meta(key, value) VALUES (?, ?)", rows)
