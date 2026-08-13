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
from gffbase.exceptions import DuplicateIDError
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
        self.f_seqid_y: list = []
        # Attribute columns
        self.a_fid: list = []
        self.a_key: list = []
        self.a_val: list = []
        self.a_idx: list = []

    def append(self, feat_id: str, feat: ParsedFeature, file_order: int):
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
        if self._has_spatial:
            con.execute(
                "INSERT INTO features ("
                'id, seqid, source, featuretype, start, "end", '
                "score, strand, frame, attributes_blob, extra_blob, "
                "file_order, is_synthetic, seqid_y, bbox"
                ') SELECT id, seqid, source, featuretype, start, "end", '
                "score, strand, frame, attributes_blob, extra_blob, "
                "file_order, is_synthetic, seqid_y, "
                # A null coordinate yields a null envelope rather than
                # failing the insert; such rows are then absent from R-tree
                # results, which matches gffutils (where `NULL <= ?` is
                # unknown) and the B-tree path.
                'CASE WHEN start IS NULL OR "end" IS NULL THEN NULL '
                'ELSE ST_MakeEnvelope(start, seqid_y, "end", seqid_y + 1) END '
                "FROM __staging_features"
            )
        else:
            con.execute(
                "INSERT INTO features ("
                'id, seqid, source, featuretype, start, "end", '
                "score, strand, frame, attributes_blob, extra_blob, "
                "file_order, is_synthetic, seqid_y"
                ") SELECT * FROM __staging_features"
            )
        con.execute(
            "INSERT INTO attributes (feature_id, key, value, idx) "
            "SELECT feature_id, key, value, idx FROM __staging_attributes"
        )
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

        fid, _origin = resolver.resolve(feat, autoinc)

        if fid in seen_ids:
            if strategy == "error":
                raise DuplicateIDError(f"Duplicate ID {fid}")
            if strategy == "warning":
                _log.warning("Duplicate lines in file for id '%s'; ignoring all but the first", fid)
                n_skipped += 1
                continue
            if strategy == "create_unique":
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
        seen_ids[fid] = True
        builder.append(fid, feat, file_order)
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
    rows = [
        ("schema_version", SCHEMA_VERSION),
        ("dialect", json.dumps(dialect or {})),
        ("fmt", fmt),
        ("rtree_built", "true" if rtree_built else "false"),
        ("max_depth", str(int(max_depth))),
        ("closure_max_depth", str(closure_max_depth)),
    ]
    con.executemany("INSERT OR REPLACE INTO meta(key, value) VALUES (?, ?)", rows)
