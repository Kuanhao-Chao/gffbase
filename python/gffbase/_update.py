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
"""`FeatureDB.update`: add features to an existing database.

gffutils runs its ingest creator over the new data against the existing
database, so an update resolves ids, infers GTF parents and records `Parent`
relations exactly as `create_db` would have. 0.2.0 appended rows directly
instead, and lost all three: a feature's `ID` attribute was ignored (every new
row was named `<featuretype>_<n>`), no edges were written (so `children()`
never saw the new rows), and a duplicate id was stored rather than refused.

This does what gffutils does, in two steps. The new data is ingested on its
own into a scratch in-memory database by the same pipeline `create_db` uses --
id_spec, transform, merge_strategy among the new rows, GTF inference -- with
the target's dialect forced and its autoincrement counters as the starting
point. The staged rows are then reconciled with the target's and copied in,
and edges are derived for them against the target, where their parents live.
"""

from __future__ import annotations

import logging
import os
import tempfile
from collections.abc import Callable

import duckdb

from gffbase.exceptions import DuplicateIDError, EmptyInputError
from gffbase.feature import Feature, ParsedFeature

_log = logging.getLogger("gffbase")

#: `create_db` keywords that mean something for an update. gffutils forwards
#: its keywords to the ingest creator; anything outside this set is refused
#: rather than silently ignored.
UPDATE_KWARGS = frozenset(
    {
        "id_spec",
        "merge_strategy",
        "transform",
        "force_merge_fields",
        "checklines",
        "force_dialect_check",
        "from_string",
        "disable_infer_genes",
        "disable_infer_transcripts",
        "infer_gene_extent",
        "gtf_transcript_key",
        "gtf_gene_key",
        "gtf_subfeature",
        "verbose",
    }
)

_FEATURE_COLS = (
    "id",
    "seqid",
    "source",
    "featuretype",
    "start",
    '"end"',
    "score",
    "strand",
    "frame",
    "attributes_blob",
    "extra_blob",
    "file_order",
    "is_synthetic",
    "raw_id",
    "occ",
    "n_segments",
    "id_origin",
)
_SEGMENT_COLS = (
    "feature_id",
    "seg_idx",
    "start",
    '"end"',
    "score",
    "frame",
    "attributes_blob",
    "extra_blob",
    "file_order",
    "attrs_same_as_seg0",
)
_ATTRIBUTE_COLS = ("feature_id", "key", "value", "idx", "seg_idx", "ord")


# ---------------------------------------------------------------------------
# Source
# ---------------------------------------------------------------------------


def _parsed_line(pf: ParsedFeature) -> str:
    cols = [
        pf.seqid,
        pf.source,
        pf.featuretype,
        "." if pf.start is None else str(pf.start),
        "." if pf.end is None else str(pf.end),
        pf.score,
        pf.strand,
        pf.frame,
        pf.attributes_blob.decode("utf-8"),
        *pf.extra,
    ]
    return "\t".join(cols)


def _render(features) -> str:
    lines = []
    for feat in features:
        if isinstance(feat, Feature):
            line = str(feat)
        elif isinstance(feat, ParsedFeature):
            line = _parsed_line(feat)
        else:
            raise TypeError(f"update() does not accept {type(feat)!r}")
        lines.append(line if line.endswith("\n") else line + "\n")
    return "".join(lines)


def source_path(data, from_string: bool) -> tuple[str | None, Callable[[], None]]:
    """A file holding `data`, and how to clean it up.

    `data` may be a path, the text itself (`from_string=True`), a `FeatureDB`,
    or an iterable of `Feature` / `ParsedFeature` -- the forms gffutils
    accepts. Returns `(None, ...)` when there is nothing to add.
    """
    from gffbase.interface import FeatureDB

    def nothing() -> None:
        return None

    if not from_string and isinstance(data, str | os.PathLike):
        return os.fspath(data), nothing
    if from_string:
        text = data.decode("utf-8") if isinstance(data, bytes) else str(data)
    else:
        if isinstance(data, FeatureDB):
            data = data.all_features()
        text = _render(data)
    if not text.strip():
        return None, nothing

    tmp = tempfile.NamedTemporaryFile(
        mode="w", suffix=".gffbase-update", delete=False, encoding="utf-8"
    )
    try:
        tmp.write(text)
    finally:
        tmp.close()

    def remove() -> None:
        try:
            os.unlink(tmp.name)
        except OSError:  # pragma: no cover - already gone
            pass

    return tmp.name, remove


# ---------------------------------------------------------------------------
# Stage
# ---------------------------------------------------------------------------


def stage(path: str, options, autoinc: dict) -> duckdb.DuckDBPyConnection | None:
    """Ingest `path` into a scratch database. None if it holds no features."""
    from gffbase.ingest import _build_database

    try:
        con, _stats = _build_database(
            path,
            ":memory:",
            options=options,
            build_rtree=False,
            autoinc_seed=autoinc,
            validate=False,
        )
    except EmptyInputError:
        # gffutils returns early when the new data is empty; there is
        # nothing to add, which is not an error for an update.
        return None
    return con


# ---------------------------------------------------------------------------
# Merge
# ---------------------------------------------------------------------------


def _executemany(conn, sql: str, rows: list) -> None:
    # DuckDB refuses an empty parameter list rather than doing nothing.
    if rows:
        conn.executemany(sql, rows)


def _target_ids(conn, ids: list[str]) -> set[str]:
    rows = conn.execute(
        "SELECT id FROM features WHERE id IN (SELECT UNNEST(?::VARCHAR[]))", [ids]
    ).fetchall()
    return {r[0] for r in rows}


def _rename(stage_con, old: str, new: str) -> None:
    stage_con.execute("UPDATE features SET id = ? WHERE id = ?", [new, old])
    stage_con.execute("UPDATE attributes SET feature_id = ? WHERE feature_id = ?", [new, old])
    stage_con.execute("UPDATE segments SET feature_id = ? WHERE feature_id = ?", [new, old])


def _drop(con, ids: list[str], *, edges: bool = False) -> None:
    if not ids:
        return
    con.execute("DELETE FROM features WHERE id IN (SELECT UNNEST(?::VARCHAR[]))", [ids])
    con.execute("DELETE FROM attributes WHERE feature_id IN (SELECT UNNEST(?::VARCHAR[]))", [ids])
    con.execute("DELETE FROM segments WHERE feature_id IN (SELECT UNNEST(?::VARCHAR[]))", [ids])
    if edges:
        # Only edges *from* the feature's own attributes: they are re-derived
        # from the replacement. Edges naming it as a parent belong to its
        # children and stay.
        con.execute("DELETE FROM edges WHERE child IN (SELECT UNNEST(?::VARCHAR[]))", [ids])


def _parsed_from_stage(stage_con, fid: str) -> ParsedFeature:
    row = stage_con.execute(
        'SELECT seqid, source, featuretype, start, "end", score, strand, frame, '
        "attributes_blob, extra_blob FROM features WHERE id = ?",
        [fid],
    ).fetchone()
    pairs = stage_con.execute(
        "SELECT key, value, idx FROM attributes WHERE feature_id = ? AND seg_idx = 0 "
        "ORDER BY ord, rowid",
        [fid],
    ).fetchall()
    extra = bytes(row[9]).decode("utf-8").split("\t") if row[9] else []
    return ParsedFeature(
        seqid=row[0],
        source=row[1],
        featuretype=row[2],
        start=row[3],
        end=row[4],
        score=row[5],
        strand=row[6],
        frame=row[7],
        attributes_blob=bytes(row[8]) if row[8] is not None else b"",
        attributes_pairs=[tuple(p) for p in pairs],
        extra=extra,
    )


def _extend_seqid_map(conn, stage_con) -> dict[str, int]:
    """Give every new seqid the next R-tree band, and persist it."""
    from gffbase.ingest import SEQID_Y_BAND

    known = dict(conn.execute("SELECT seqid, seqid_y FROM seqid_map").fetchall())
    new = [
        r[0]
        for r in stage_con.execute(
            "SELECT seqid FROM features GROUP BY seqid ORDER BY MIN(file_order)"
        ).fetchall()
        if r[0] not in known
    ]
    # The builder's rule, so later ingest-path inserts agree: bands are dense
    # in encounter order, and the next free one is `len * band`.
    for seqid in new:
        known[seqid] = len(known) * SEQID_Y_BAND
        conn.execute("INSERT INTO seqid_map (seqid, seqid_y) VALUES (?, ?)", [seqid, known[seqid]])
    return known


def _arrow(cur):
    # `to_arrow_table` on newer DuckDB, `fetch_arrow_table` on the 1.4 floor.
    return cur.to_arrow_table() if hasattr(cur, "to_arrow_table") else cur.fetch_arrow_table()


def _copy(conn, stage_con, offset: int, has_bbox: bool) -> None:
    """Copy every staged row into the target, re-banded and re-ordered."""
    cols = ", ".join(_FEATURE_COLS)
    feats = _arrow(stage_con.execute(f"SELECT {cols} FROM features"))
    attrs = _arrow(stage_con.execute(f"SELECT {', '.join(_ATTRIBUTE_COLS)} FROM attributes"))
    segs = _arrow(stage_con.execute(f"SELECT {', '.join(_SEGMENT_COLS)} FROM segments"))

    select = ", ".join(
        f"s.file_order + {int(offset)}" if c == "file_order" else f"s.{c}" for c in _FEATURE_COLS
    )
    bbox_col, bbox_val = "", ""
    if has_bbox:
        bbox_col = ", bbox"
        bbox_val = (
            ', CASE WHEN s.start IS NULL OR s."end" IS NULL THEN NULL '
            'ELSE ST_MakeEnvelope(s.start, m.seqid_y, s."end", m.seqid_y + 1) END'
        )
    conn.register("__upd_features", feats)
    conn.register("__upd_attributes", attrs)
    conn.register("__upd_segments", segs)
    try:
        conn.execute(
            f"INSERT INTO features ({cols}, seqid_y{bbox_col}) "
            f"SELECT {select}, m.seqid_y{bbox_val} "
            "FROM __upd_features s JOIN seqid_map m ON m.seqid = s.seqid"
        )
        acols = ", ".join(_ATTRIBUTE_COLS)
        conn.execute(f"INSERT INTO attributes ({acols}) SELECT {acols} FROM __upd_attributes")
        scols = ", ".join(_SEGMENT_COLS)
        sselect = ", ".join(
            f"s.file_order + {int(offset)}" if c == "file_order" else f"s.{c}"
            for c in _SEGMENT_COLS
        )
        conn.execute(
            f"INSERT INTO segments ({scols}, seqid_y) "
            f"SELECT {sselect}, m.seqid_y FROM __upd_segments s "
            "JOIN features f ON f.id = s.feature_id "
            "JOIN seqid_map m ON m.seqid = f.seqid"
        )
    finally:
        conn.unregister("__upd_features")
        conn.unregister("__upd_attributes")
        conn.unregister("__upd_segments")

    for original, new in stage_con.execute("SELECT original_id, new_id FROM duplicates").fetchall():
        conn.execute(
            "INSERT OR IGNORE INTO duplicates (original_id, new_id) VALUES (?, ?)", [original, new]
        )
    conflicts = stage_con.execute(
        "SELECT raw_id, resolved_id, kind, file_order, detail FROM id_conflicts"
    ).fetchall()
    _executemany(
        conn,
        "INSERT INTO id_conflicts (raw_id, resolved_id, kind, file_order, detail) "
        "VALUES (?, ?, ?, ?, ?)",
        [(r, s, k, None if o is None else o + offset, d) for r, s, k, o, d in conflicts],
    )


def _derive_edges(conn, new_ids: list[str], fmt: str, options) -> None:
    """Edges for the new rows, derived against the whole target.

    The same rules ingest applies, restricted to rows this update touched. For
    a GTF that means either end of the edge: a new exon under an existing
    transcript, and an existing exon under a transcript this update added.
    """
    if not new_ids:
        return
    conn.execute("CREATE TEMP TABLE __upd_new AS SELECT UNNEST(?::VARCHAR[]) AS id", [new_ids])
    try:
        not_already = (
            "AND NOT EXISTS (SELECT 1 FROM edges e "
            "                WHERE e.parent = a.value AND e.child = a.feature_id)"
        )
        if fmt == "gtf":
            conn.execute(
                "INSERT INTO edges (parent, child) "
                "SELECT DISTINCT a.value, a.feature_id FROM attributes a "
                "JOIN features f ON f.id = a.feature_id "
                "WHERE a.key = ? AND a.seg_idx = 0 "
                "  AND f.featuretype NOT IN ('gene', 'transcript') AND a.value <> f.id "
                "  AND EXISTS (SELECT 1 FROM features p "
                "              WHERE p.id = a.value AND p.featuretype = 'transcript') "
                "  AND (a.feature_id IN (SELECT id FROM __upd_new) "
                "       OR a.value IN (SELECT id FROM __upd_new)) " + not_already,
                [options.gtf_transcript_key],
            )
            conn.execute(
                "INSERT INTO edges (parent, child) "
                "SELECT DISTINCT a.value, a.feature_id FROM attributes a "
                "JOIN features f ON f.id = a.feature_id "
                "WHERE a.key = ? AND a.seg_idx = 0 "
                "  AND f.featuretype = 'transcript' AND a.value <> f.id "
                "  AND EXISTS (SELECT 1 FROM features p "
                "              WHERE p.id = a.value AND p.featuretype = 'gene') "
                "  AND (a.feature_id IN (SELECT id FROM __upd_new) "
                "       OR a.value IN (SELECT id FROM __upd_new)) " + not_already,
                [options.gtf_gene_key],
            )
        else:
            # GFF3 keeps an edge to a parent that is not (yet) in the database,
            # as ingest and gffutils both do, so nothing needs the parent side.
            conn.execute(
                "INSERT INTO edges (parent, child) "
                "SELECT DISTINCT a.value, a.feature_id FROM attributes a "
                "WHERE a.key = 'Parent' AND a.value <> '' "
                "  AND a.feature_id IN (SELECT id FROM __upd_new) " + not_already
            )
    finally:
        conn.execute("DROP TABLE __upd_new")


def _widen_inferred_parents(conn, new_ids: list[str], has_bbox: bool) -> None:
    """Stretch an inferred GTF parent over the children this update gave it.

    An inferred transcript or gene *is* the envelope of its children, so a new
    exon outside it has to move its edge. gffutils leaves the old extent in
    place, which makes the parent stop covering its own child: `region()`
    over the new exon misses its gene, and INV-16 fails. Transcripts first,
    because a gene's children are transcripts that may just have grown.
    """
    if not new_ids:
        return
    bbox = (
        ", bbox = ST_MakeEnvelope(x.s, features.seqid_y, x.e, features.seqid_y + 1)"
        if has_bbox
        else ""
    )
    grown = list(new_ids)
    for parent_type in ("transcript", "gene"):
        conn.execute(
            'UPDATE features SET start = x.s, "end" = x.e' + bbox + " "
            "FROM (SELECT p.id, LEAST(p.start, MIN(c.start)) AS s, "
            '             GREATEST(p."end", MAX(c."end")) AS e '
            "      FROM features p JOIN edges ed ON ed.parent = p.id "
            "      JOIN features c ON c.id = ed.child "
            "      WHERE p.is_synthetic AND p.featuretype = ? "
            "        AND p.start IS NOT NULL AND c.start IS NOT NULL "
            "        AND p.id IN (SELECT e2.parent FROM edges e2 "
            "                     WHERE e2.child IN (SELECT UNNEST(?::VARCHAR[])) "
            "                        OR e2.parent IN (SELECT UNNEST(?::VARCHAR[]))) "
            '      GROUP BY p.id, p.start, p."end") x '
            'WHERE features.id = x.id AND (features.start <> x.s OR features."end" <> x.e)',
            [parent_type, grown, grown],
        )
        if parent_type == "transcript":
            # A transcript with a new child may just have grown, so to its
            # gene it counts as new.
            grown += [
                r[0]
                for r in conn.execute(
                    "SELECT DISTINCT e.parent FROM edges e JOIN features p ON p.id = e.parent "
                    "WHERE p.is_synthetic AND p.featuretype = 'transcript' "
                    "  AND e.child IN (SELECT UNNEST(?::VARCHAR[]))",
                    [grown],
                ).fetchall()
            ]


def merge(db, stage_con, options) -> None:
    """Reconcile the staged rows with the target's and write them in."""
    from gffbase.ingest import (
        IdSpecResolver,
        _ArrowBatchBuilder,
        _resolve_deferred_duplicates,
    )

    conn = db.conn
    staged = stage_con.execute(
        "SELECT id, is_synthetic, id_origin, featuretype FROM features ORDER BY file_order"
    ).fetchall()
    if not staged:
        return
    staged_ids = [r[0] for r in staged]
    existing = _target_ids(conn, staged_ids)

    autoinc = dict(conn.execute("SELECT base, n FROM autoincrements").fetchall())
    for base, n in stage_con.execute("SELECT base, n FROM autoincrements").fetchall():
        autoinc[base] = max(int(n), int(autoinc.get(base, 0)))
    claimed = set(staged_ids)

    def free_id(base: str) -> str:
        while True:
            candidate = IdSpecResolver._autoincrement(base, autoinc)
            if candidate not in claimed and not _target_ids(conn, [candidate]):
                claimed.add(candidate)
                return candidate

    strategy = options.merge_strategy
    renames: dict[str, str] = {}
    dropped: list[str] = []
    replaced: list[str] = []
    merged: list[str] = []
    for fid, synthetic, origin, featuretype in staged:
        if fid not in existing:
            continue
        if synthetic:
            # An inferred gene or transcript the target already has. gffutils
            # folds it into the incumbent, which in practice leaves the
            # incumbent as it was; so does this.
            dropped.append(fid)
        elif origin == "autoincrement":
            # A generated name that happens to be taken, not the caller's id:
            # draw the next free one rather than calling it a duplicate.
            renames[fid] = free_id(featuretype)
        elif strategy == "error":
            raise DuplicateIDError(f"Duplicate ID {fid}")
        elif strategy == "warning":
            _log.warning("Duplicate lines in file for id '%s'; ignoring all but the first", fid)
            dropped.append(fid)
        elif strategy == "create_unique":
            renames[fid] = free_id(fid)
        elif strategy == "replace":
            replaced.append(fid)
        else:  # merge
            merged.append(fid)

    # Nothing has been written to the target yet, so a DuplicateIDError above
    # leaves it untouched.
    deferred = [_parsed_from_stage(stage_con, fid) for fid in merged]
    order_of = dict(stage_con.execute("SELECT id, file_order FROM features").fetchall())
    offset = int(conn.execute("SELECT COALESCE(MAX(file_order), 0) FROM features").fetchone()[0])
    merged_orders = [order_of[fid] + offset for fid in merged]

    _drop(stage_con, dropped + merged)
    for old, new in renames.items():
        _rename(stage_con, old, new)
    _drop(conn, replaced, edges=True)

    seqid_map = _extend_seqid_map(conn, stage_con)
    has_bbox = bool(db._rtree_built)
    new_ids = [r[0] for r in stage_con.execute("SELECT id FROM features").fetchall()]
    _copy(conn, stage_con, offset, has_bbox)

    if deferred:
        builder = _ArrowBatchBuilder(seqid_map, has_spatial=has_bbox)
        pairs = _resolve_deferred_duplicates(
            conn,
            list(zip(merged, deferred, merged_orders, strict=True)),
            options,
            autoinc,
            builder,
            seqid_map,
            db.fmt,
        )
        _executemany(
            conn, "INSERT OR IGNORE INTO duplicates (original_id, new_id) VALUES (?, ?)", pairs
        )
        new_ids.extend(merged)
        new_ids.extend(new for _, new in pairs)

    _derive_edges(conn, new_ids, db.fmt, options)
    if db.fmt == "gtf":
        _widen_inferred_parents(conn, new_ids, has_bbox)

    _executemany(
        conn,
        "INSERT OR REPLACE INTO autoincrements (base, n) VALUES (?, ?)",
        [(base, int(n)) for base, n in autoinc.items()],
    )
    if db._rtree_built:
        db._seqid_y_map = dict(seqid_map)
