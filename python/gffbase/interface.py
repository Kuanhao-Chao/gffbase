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
"""``FeatureDB`` — drop-in successor to ``gffutils.FeatureDB``.

Wraps a DuckDB connection produced by ``gffbase.ingest.from_file`` (or opened
from an on-disk ``.duckdb`` file) and exposes the legacy public surface.

Two routing decisions are made dynamically:

* ``region()`` dispatches to an R-tree-backed query when ``meta.rtree_built``
  is true, otherwise to the multi-column B-tree path. Both queries are
  semantically identical; only the planner choice differs.
* ``children()`` / ``parents()`` read from the materialized closure table
  when the requested ``level`` lies within ``meta.max_depth``, and fall back
  to a dynamic recursive CTE when a deeper traversal is requested.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Iterator

import duckdb

from gffbase._dbutil import scalar, scalar_or
from gffbase.exceptions import FeatureNotFoundError, SchemaVersionError
from gffbase.feature import Feature, db_row_projection, feature_from_row
from gffbase.modes import (
    MODE_COMPAT,
    MODE_STRICT,
    ON_ERROR_RAISE,
    VALIDATION_GFFUTILS,
)
from gffbase.schema import SCHEMA_VERSION

#: `source` stamped on features gffbase derives rather than reads.
#:
#: Mode-dependent on purpose. A script ported from gffutils filters on
#: `source == "gffutils_derived"`, and compat mode exists so that script keeps
#: working; strict mode reports honest provenance instead. The GTF-synthesis
#: path in `schema.py` writes the strict spelling.
DERIVED_SOURCE = {
    MODE_COMPAT: "gffutils_derived",
    MODE_STRICT: "gffbase_derived",
}

_log = logging.getLogger("gffbase.interface")

# Selection clause for FeatureDB -> Feature reconstruction. Derived from
# `feature._DB_ROW_FIELDS` rather than restated, so the projection and the
# positional unpacking in `feature_from_row` cannot drift apart.
_SELECT_FEATURE = db_row_projection()


def _with_coordinates(features):
    """Drop features that have no coordinates.

    A GFF row may legally carry `.` in columns 4 and 5, so `start`/`end` are
    nullable. Every caller of this helper performs a *coordinate-space*
    operation -- sorting by position, computing a gap, merging overlaps -- and
    a feature outside coordinate space is simply not in the input domain of
    those operations. Filtering here keeps the whole database usable when a
    single row lacks coordinates; the alternative, raising, would make
    `merge_all()` unusable on any file containing one (WormBase emits them).

    Without this, each of these paths raised
    `TypeError: '<' not supported between instances of 'int' and 'NoneType'`
    from deep inside a sort key.
    """
    return [f for f in features if f.start is not None and f.end is not None]


#: Sort keys `order_by` accepts, mapped to the SQL that implements each.
#:
#: This is a WHITELIST, and it restores the documented contract rather than
#: narrowing it: gffutils specifies that "the string or tuple items must be in:
#: 'seqid', 'source', 'featuretype', 'start', 'end', 'score', 'strand',
#: 'frame', 'attributes', 'extra'". gffbase used to fall through to
#: interpolating anything else straight into the SQL "for power users", which
#: on DuckDB is a live injection: DuckDB executes trailing statements, so
#: `order_by="start ASC; DROP TABLE attributes; SELECT ..."` dropped the table
#: and still returned rows. gffutils has the same interpolation and is saved
#: only by SQLite refusing more than one statement per execute.
#:
#: `{q}` is the table qualifier, empty for an unjoined query.
_ORDER_BY_COLUMNS = {
    # Not in the oracle's documented list, but a real column that callers do
    # sort by, and safe: an omission there rather than a rule.
    "id": "{q}id",
    "seqid": "{q}seqid",
    "source": "{q}source",
    "featuretype": "{q}featuretype",
    "start": "{q}start",
    "end": '{q}"end"',
    "score": "{q}score",
    "strand": "{q}strand",
    "frame": "{q}frame",
    # The oracle's names for the two blob columns.
    "attributes": "{q}attributes_blob",
    "extra": "{q}extra_blob",
    # gffbase extensions: `file_order` is the default sort, and `length` sorts
    # by span rather than by position.
    "file_order": "{q}file_order",
    "length": '({q}"end" - {q}start)',
}


def _order_clause(order_by, reverse: bool, qualifier: str = "") -> str:
    """Build an ORDER BY clause from a whitelisted sort key, or several.

    Accepts a single name, a tuple or list of names, or a comma-separated
    string. All three were nominally supported and only the first worked: a
    tuple was interpolated as a Python repr, which DuckDB parses as a constant
    struct and so silently sorted by nothing, and a list raised
    `TypeError: unhashable type: 'list'` from a set membership test.

    `reverse` applies to EVERY key. gffutils appends the direction once, which
    in SQL reverses only the last key -- almost certainly not what a caller
    asking for descending order means. Since multi-key sorting did not work at
    all here before, there is no behaviour to preserve, and reproducing that in
    new code would be copying a defect.
    """
    q = f"{qualifier}." if qualifier else ""
    if order_by is None:
        names = ["file_order"]
    elif isinstance(order_by, str):
        names = [part.strip() for part in order_by.split(",") if part.strip()]
    elif isinstance(order_by, (tuple, list)):
        names = [str(part).strip() for part in order_by]
    else:
        raise TypeError(
            f"order_by must be a string, tuple or list of column names; got {type(order_by)!r}"
        )
    if not names:
        names = ["file_order"]

    direction = "DESC" if reverse else "ASC"
    parts = []
    for name in names:
        try:
            template = _ORDER_BY_COLUMNS[name]
        except KeyError:
            raise ValueError(
                f"cannot order by {name!r}; order_by accepts {', '.join(sorted(_ORDER_BY_COLUMNS))}"
            ) from None
        parts.append(f"{template.format(q=q)} {direction}")
    return ", ".join(parts)


def _sql_literal(value) -> str:
    """Render a Python scalar as a DuckDB literal.

    `SET`/`PRAGMA` take no bind parameters, so a setting's value has to be
    written into the statement text. This is the one place that is allowed to
    happen, and it happens by construction rather than by interpolation:
    booleans and numbers have no syntax to escape, and a string is
    single-quoted with its own quotes doubled, which is the only escape SQL
    string literals have.
    """
    if isinstance(value, bool):
        # Before the int branch: bool is a subclass of int, and DuckDB spells
        # its booleans `true`/`false`, not `1`/`0`.
        return "true" if value else "false"
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float):
        if value != value or value in (float("inf"), float("-inf")):
            raise ValueError(f"cannot use {value!r} as a setting value")
        return repr(value)
    text = str(value).replace("'", "''")
    return f"'{text}'"


#: Sentinel for "this feature merged with nothing".
#:
#: Empty tuple rather than `None` or `[]` so that `if merged.children:` is the
#: discriminator, exactly as upstream. `merge()` used to set `children`
#: unconditionally, which made that test useless -- every feature looked
#: merged, so `merge_all` could not tell a real merge from a pass-through.
no_children: tuple = ()


def assign_child(parent, child):
    """Default `child_func` for `add_relation`: point the child at the parent.

    Returns the child, because `add_relation` writes back what the callback
    returns rather than relying on mutation.
    """
    child.attributes["Parent"] = parent["ID"]
    return child


def _finalize_merge(feature, feature_children):
    """Stamp a merged feature with its provenance, or mark it a pass-through.

    A genuine merge takes the comma-joined set of its components' sources, so
    the result says where it came from. A run of one is not a merge and gets
    `no_children`.
    """
    if len(feature_children) > 1:
        feature.source = ",".join(sorted({child.source for child in feature_children}))
        feature.children = list(feature_children)
    else:
        feature.children = no_children
    return feature


def _require_feature_id(obj) -> str:
    """Coerce a `Feature`-or-id argument to a primary-key string.

    A `Feature` built by hand (rather than loaded from a database) has
    `id is None`. Passing one to a query used to reach SQL as a NULL bind and
    silently match nothing; this reports the mistake instead.
    """
    fid = obj.id if isinstance(obj, Feature) else obj
    if fid is None:
        raise ValueError(
            "feature has no database id -- pass an id string, or a Feature "
            "obtained from this database"
        )
    if not isinstance(fid, str):
        raise TypeError(f"expected a feature id string or Feature; got {type(obj)!r}")
    return fid


class FeatureDB:
    """Drop-in successor to ``gffutils.FeatureDB``."""

    # ------------------------------------------------------------------
    # Construction
    # ------------------------------------------------------------------

    def __init__(
        self,
        dbfn,
        default_encoding: str = "utf-8",
        keep_order: bool = False,
        pragmas: dict | None = None,
        sort_attribute_values: bool = False,
        text_factory=str,
        upgrade: str = "auto",
    ):
        if upgrade not in ("auto", "never", "error"):
            raise ValueError(f"upgrade must be 'auto', 'never' or 'error'; got {upgrade!r}")
        self._upgrade = upgrade
        self.default_encoding = default_encoding
        #: Specification violations tolerated while building this database.
        #: Populated under `mode="compat"`; empty for a reopened database,
        #: which has no record of how it was built.
        self.warnings: list[dict] = []
        self.keep_order = keep_order
        self.sort_attribute_values = sort_attribute_values
        self.text_factory = text_factory
        self._analyzed_flag = False
        # Created on demand; see `_segment_cursor`.
        self._seg_cursor: duckdb.DuckDBPyConnection | None = None

        # Resolve dbfn → connection.
        if isinstance(dbfn, duckdb.DuckDBPyConnection):
            self.conn = dbfn
            self.dbfn = ":existing-connection:"
        elif (
            isinstance(dbfn, tuple)
            and len(dbfn) == 2
            and isinstance(dbfn[0], duckdb.DuckDBPyConnection)
        ):
            # (con, IngestStats) — used internally by `create_db`.
            self.conn = dbfn[0]
            self.dbfn = ":existing-connection:"
            self.warnings = list(getattr(dbfn[1], "warnings", []) or [])
        elif isinstance(dbfn, str):
            self.dbfn = dbfn
            self.conn = duckdb.connect(dbfn, read_only=False)
        else:
            raise TypeError(
                f"dbfn must be a path, DuckDB connection, or (con, stats) tuple; got {type(dbfn)!r}"
            )

        if pragmas:
            self.set_pragmas(pragmas)

        # Recover provenance from `meta`.
        meta = self._read_meta()
        self._apply_schema_version(meta)
        self.dialect = self._parse_dialect(meta.get("dialect"))
        self.fmt = meta.get("fmt", "gff3")
        # How this database was built. Absent from anything written before the
        # key existed, and absent from a v1 database; both were compat, which
        # is what the default says.
        self.mode = meta.get("mode", MODE_COMPAT)
        self.validation = meta.get("validation", VALIDATION_GFFUTILS)
        self.on_error = meta.get("on_error", ON_ERROR_RAISE)
        self._rtree_built = meta.get("rtree_built", "false").lower() == "true"
        self._max_depth = int(meta.get("max_depth", "8"))
        # Defensive: confirm the R-tree index actually exists in this DB.
        if self._rtree_built:
            self._rtree_built = self._has_rtree_index()
        # If the R-tree was built at ingest time, the spatial extension's
        # functions (ST_Intersects, ST_MakeEnvelope, …) must be loaded into
        # the current connection — they are NOT auto-loaded by DuckDB just
        # because the index exists. If the load fails (offline HPC node, etc),
        # gracefully fall back to the multi-column B-tree path.
        if self._rtree_built:
            try:
                self.conn.execute("LOAD spatial")
            except duckdb.Error:
                try:
                    self.conn.execute("INSTALL spatial")
                    self.conn.execute("LOAD spatial")
                except duckdb.Error:
                    self._rtree_built = False

        # Phase 7 — closure-cache vs dynamic-CTE dispatcher. Read the corpus's
        # true hierarchy depth once at open; fall back to a live MAX(depth)
        # query if older DBs don't carry the meta row.
        cmd_meta = meta.get("closure_max_depth")
        if cmd_meta is not None:
            self._closure_max_depth = int(cmd_meta)
        else:
            try:
                row = self.conn.execute("SELECT MAX(depth) FROM closure").fetchone()
                self._closure_max_depth = int(row[0]) if row and row[0] is not None else 0
            except duckdb.Error:
                self._closure_max_depth = 0

        # Phase 7 — load the seqid → y-band map so `_region_sql_rtree` can
        # produce a tightly-bounded ST_MakeEnvelope at query time. Empty when
        # no R-tree was built (we fall back to the B-tree path anyway).
        self._seqid_y_map: dict = {}
        if self._rtree_built:
            try:
                rows = self.conn.execute("SELECT seqid, seqid_y FROM seqid_map").fetchall()
                self._seqid_y_map = {s: int(y) for s, y in rows}
            except duckdb.Error:
                # Older DBs without the map — fall back to B-tree to be safe.
                self._rtree_built = False

        # Directives
        self.directives = [
            row[0]
            for row in self.conn.execute("SELECT directive FROM directives ORDER BY seq").fetchall()
        ]

    @staticmethod
    def _parse_dialect(raw):
        if not raw:
            return {"fmt": "gff3"}
        try:
            return json.loads(raw)
        except (TypeError, ValueError):
            return {"fmt": "gff3"}

    def _read_meta(self) -> dict:
        try:
            rows = self.conn.execute("SELECT key, value FROM meta").fetchall()
            return {k: v for k, v in rows}
        except duckdb.Error:
            return {}

    def _apply_schema_version(self, meta: dict) -> None:
        """Classify the database's schema version and configure for it.

        The version was written from the first release and never read, so until
        now a mismatch surfaced as whatever the first query happened to hit --
        a missing-column error, or a plausible wrong answer from a query that
        did not touch the new columns. This is the gate.

        * ``"2"`` -- current. Proceed.
        * missing / ``"1"`` -- degrade to *v1 shim mode*. Every v2 addition is
          additive, so a v1 database answers every v1 query correctly; the only
          thing it cannot do is represent a multipart feature. Setting
          ``_n_multipart = 0`` makes each query builder take exactly the branch
          that emits v1 SQL, so the shim costs nothing and needs no special
          cases downstream. `gffbase.migrate` (Stage D) upgrades in place.
        * anything else -- ``SchemaVersionError``. A newer database may have
          moved data this build would silently misread, and an unparseable
          version cannot be reasoned about at all.
        """
        raw = meta.get("schema_version")
        self._v1_shim = False

        if raw is None and self._looks_unfinished():
            raise SchemaVersionError(
                f"{self.dbfn} has gffbase tables but no metadata at all, so it is an "
                "incomplete database -- an ingest that failed part-way, or a file "
                "truncated afterwards. Rebuild it with create_db(); opening it would "
                "silently hand back an empty database that reports itself as current."
            )

        if raw is None or raw == "1":
            # `None` covers both a genuine v1 database and one built before the
            # version was recorded at all; neither has the v2 tables.
            if self._upgrade == "error":
                raise SchemaVersionError(
                    f"{self.dbfn} uses schema v1 and upgrade='error' was requested. "
                    "Pass upgrade='auto' to migrate it in place, or 'never' to open "
                    "it read-compatible."
                )
            if self._upgrade == "auto" and self._try_migrate():
                self._schema_version = int(SCHEMA_VERSION)
                self._n_multipart = self._read_n_multipart(self._read_meta())
                return
            self._v1_shim = True
            self._schema_version = 1
            self._n_multipart = 0
            _log.info(
                "database %s uses schema v1; opening in compatibility mode. "
                "Run gffbase.migrate.migrate_v1_to_v2() to upgrade in place.",
                self.dbfn,
            )
            return

        if raw != SCHEMA_VERSION:
            raise SchemaVersionError(
                f"{self.dbfn} has schema_version {raw!r}, but this gffbase "
                f"reads version {SCHEMA_VERSION!r}. A database written by a "
                "newer gffbase cannot be read safely; upgrade gffbase."
            )

        self._schema_version = int(SCHEMA_VERSION)
        self._n_multipart = self._read_n_multipart(meta)

    def _looks_unfinished(self) -> bool:
        """True for a database whose tables exist but whose `meta` is empty.

        That is what a failed ingest used to leave on disk: valid DuckDB, the
        full gffbase schema, no data and no metadata. Every real database has
        at least a `schema_version` row, because `_write_meta` is the last step
        of a successful build -- so an empty `meta` beside a real `features`
        table cannot be anything but an interrupted one.

        Distinguished from "no `meta` table at all", which is not a gffbase
        database and is left to fail on its own terms.

        Only applied to a database opened from a PATH. A caller who hands
        `FeatureDB` a connection they built and populated themselves is doing
        something deliberate, and the advice this refusal gives -- rebuild it
        with `create_db()` -- would not even apply to them.
        """
        if self.dbfn == ":existing-connection:":
            return False
        try:
            n_meta = scalar(self.conn, "SELECT COUNT(*) FROM meta")
            n_features = scalar(self.conn, "SELECT COUNT(*) FROM features")
        except duckdb.Error:
            return False
        return n_meta == 0 and n_features == 0

    def _try_migrate(self) -> bool:
        """Upgrade a v1 database in place. False if the connection cannot.

        The migration is purely additive and changes no query result, which is
        what makes doing it automatically at open acceptable. A read-only
        connection -- or any other reason DDL is refused -- is not an error
        here: the shim reads a v1 database perfectly well, so degrading is
        strictly better than refusing to open.
        """
        from gffbase.migrate import migrate_v1_to_v2

        try:
            result = migrate_v1_to_v2(self.dbfn, con=self.conn)
        except duckdb.Error as exc:
            _log.info(
                "could not upgrade %s in place (%s); opening in v1 compatibility mode",
                self.dbfn,
                exc,
            )
            return False
        return bool(result)

    def _read_n_multipart(self, meta: dict) -> int:
        """How many features span more than one input line.

        Gates the segment-overlap conjunct in every query builder, so it is
        read once at open rather than per query. Falls back to counting when
        the meta row is absent -- a database built by an early v2 build, or one
        a caller mutated directly -- because guessing zero there would silently
        drop segments from `region()`.
        """
        recorded = meta.get("n_multipart")
        if recorded is not None:
            try:
                return int(recorded)
            except ValueError:
                pass
        try:
            return int(scalar(self.conn, "SELECT COUNT(*) FROM features WHERE n_segments > 1"))
        except duckdb.Error:
            return 0

    def _has_rtree_index(self) -> bool:
        try:
            rows = self.conn.execute(
                "SELECT index_name FROM duckdb_indexes() "
                "WHERE table_name = 'features' AND index_name = 'features_rtree'"
            ).fetchall()
            return bool(rows)
        except duckdb.Error:
            return False

    # ------------------------------------------------------------------
    # Properties
    # ------------------------------------------------------------------

    def schema(self) -> str:
        """The database schema as SQL text.

        A METHOD, not a property: gffutils documents `db.schema()` and callers
        write it that way. Exposing it as a property meant the documented call
        raised `TypeError: 'str' object is not callable`.
        """
        rows = self.conn.execute("""
            SELECT sql FROM duckdb_tables() WHERE database_name = current_database()
            UNION ALL
            SELECT sql FROM duckdb_views()  WHERE database_name = current_database()
        """).fetchall()
        return "\n".join(r[0] for r in rows if r[0])

    @property
    def _analyzed(self) -> bool:
        return self._analyzed_flag

    # ------------------------------------------------------------------
    # Dunders
    # ------------------------------------------------------------------

    def validate(self, level: str = "fast", **kwargs):
        """Check this database's structural invariants.

        See :func:`gffbase.validate.validate_db`. Run automatically at the end
        of a strict-mode ingest; worth running by hand after `update()`,
        `delete()` or `coalesce_multipart()`, which are the operations that can
        leave the two halves of a discontinuous feature disagreeing.
        """
        from gffbase.validate import validate_db

        return validate_db(self, level=level, **kwargs)

    def _build_feature(self, row, segments=None) -> Feature:
        """The one place a database row becomes a `Feature`.

        Both construction sites route through here so the database-wide
        rendering settings cannot reach one and miss the other -- they used to
        be stored on `FeatureDB` and never passed on at all, which made
        `keep_order` and `sort_attribute_values` silently inert.
        """
        return feature_from_row(
            row,
            dialect=self.dialect,
            keep_order=self.keep_order,
            sort_attribute_values=self.sort_attribute_values,
            segments=segments,
        )

    def __getitem__(self, key) -> Feature:
        target_id = _require_feature_id(key)
        # Project `n_segments` only where a multipart feature can exist, so a
        # v1 shim database -- which has no such column -- still answers, and so
        # the overwhelmingly common single-segment lookup costs no extra query.
        extra = ", n_segments" if self._n_multipart else ""
        row = self.conn.execute(
            f"SELECT {_SELECT_FEATURE}{extra} FROM features WHERE id = ?", [target_id]
        ).fetchone()
        if row is None:
            raise FeatureNotFoundError(target_id)
        if not extra:
            return self._build_feature(row)

        row, n_segments = row[:-1], row[-1]
        if n_segments <= 1:
            return self._build_feature(row)
        # A lazy loader rather than an eager fetch: `db[id]` is often used to
        # read a coordinate or an attribute, and those need no segments at all.
        segments = self._prefetch_segments([target_id]).get(target_id, [])
        return self._build_feature(row, segments)

    def __contains__(self, key) -> bool:
        target_id = key.id if isinstance(key, Feature) else key
        row = self.conn.execute(
            "SELECT 1 FROM features WHERE id = ? LIMIT 1", [target_id]
        ).fetchone()
        return row is not None

    # ------------------------------------------------------------------
    # Counts and distinct-value iterators
    # ------------------------------------------------------------------

    def count_features_of_type(self, featuretype: str | None = None) -> int:
        if featuretype is None:
            return scalar(self.conn, "SELECT COUNT(*) FROM features")
        return scalar(
            self.conn, "SELECT COUNT(*) FROM features WHERE featuretype = ?", [featuretype]
        )

    def featuretypes(self) -> Iterator[str]:
        for (ft,) in self.conn.execute(
            "SELECT DISTINCT featuretype FROM features ORDER BY featuretype"
        ).fetchall():
            yield ft

    def seqids(self) -> Iterator[str]:
        for (s,) in self.conn.execute(
            "SELECT DISTINCT seqid FROM features ORDER BY seqid"
        ).fetchall():
            yield s

    # ------------------------------------------------------------------
    # Scans (all_features / features_of_type)
    # ------------------------------------------------------------------

    def all_features(
        self,
        limit=None,
        strand: str | None = None,
        featuretype: str | list[str] | None = None,
        order_by=None,
        reverse: bool = False,
        completely_within: bool = False,
    ) -> Iterator[Feature]:
        sql, params = self._build_scan_sql(
            base_where=[],
            base_params=[],
            limit=limit,
            strand=strand,
            featuretype=featuretype,
            order_by=order_by,
            reverse=reverse,
            completely_within=completely_within,
        )
        yield from self._yield_features(sql, params)

    def features_of_type(
        self,
        featuretype: str | list[str],
        limit=None,
        strand: str | None = None,
        order_by=None,
        reverse: bool = False,
        completely_within: bool = False,
    ) -> Iterator[Feature]:
        yield from self.all_features(
            limit=limit,
            strand=strand,
            featuretype=featuretype,
            order_by=order_by,
            reverse=reverse,
            completely_within=completely_within,
        )

    def _segment_overlap(self, start, end, *, qualifier: str = ""):
        """Recheck conjunct for a region *overlap* query. ``([], [])`` when not
        needed, which is the overwhelmingly common case.

        A multipart feature's stored coordinates are the envelope over its
        segments, so an envelope-based overlap test is a *superset* of the true
        answer: a feature whose two segments straddle the query region matches
        on its envelope while overlapping nothing. GFF3 semantics are that a
        discontinuous feature overlaps a region when any of its segments does,
        so those have to be filtered back out.

        Three properties make this nearly free:

        * It is omitted entirely when no feature in the database is multipart
          -- every GTF corpus, all of GENCODE, every database migrated from v1
          -- and the emitted SQL is then byte-identical to v1's.
        * `n_segments = 1` short-circuits the `EXISTS`, so even in a database
          that does have multipart features the subquery is evaluated only for
          the handful of rows that need it.
        * It is a separate top-level conjunct, so `ST_Intersects` stays a
          top-level conjunct too and the planner still picks the R-tree.

        Not needed for ``completely_within``: an envelope lies inside the
        region exactly when all of its segments do, so the envelope test is
        already exact there.
        """
        if not self._n_multipart or start is None or end is None:
            return [], []
        q = f"{qualifier}." if qualifier else ""
        return (
            [
                f"({q}n_segments = 1 OR EXISTS ("
                f"SELECT 1 FROM segments s WHERE s.feature_id = {q}id "
                'AND s.start <= ? AND s."end" >= ?))'
            ],
            [end, start],
        )

    def _build_scan_sql(
        self,
        *,
        base_where,
        base_params,
        limit,
        strand,
        featuretype,
        order_by,
        reverse,
        completely_within,
    ):
        where = list(base_where)
        params = list(base_params)
        # `limit` (legacy) accepts a region triple. Reuse `region()`'s parser.
        if limit is not None:
            rseqid, rstart, rend = self._normalize_region_args(limit, None, None, None)
            if rseqid is not None:
                where.append("seqid = ?")
                params.append(rseqid)
            if rstart is not None and rend is not None:
                if completely_within:
                    where.append('start >= ? AND "end" <= ?')
                    params.extend([rstart, rend])
                else:
                    where.append('start <= ? AND "end" >= ?')
                    params.extend([rend, rstart])
                    seg_where, seg_params = self._segment_overlap(rstart, rend)
                    where.extend(seg_where)
                    params.extend(seg_params)
        if strand is not None:
            where.append("strand = ?")
            params.append(strand)
        if featuretype is not None:
            if isinstance(featuretype, (list, tuple, set)):
                placeholders = ",".join("?" * len(featuretype))
                where.append(f"featuretype IN ({placeholders})")
                params.extend(featuretype)
            else:
                where.append("featuretype = ?")
                params.append(featuretype)

        sql = f"SELECT {_SELECT_FEATURE} FROM features"
        if where:
            sql += " WHERE " + " AND ".join(where)
        sql += " ORDER BY " + self._order_clause(order_by, reverse)
        return sql, params

    @staticmethod
    def _order_clause(order_by, reverse: bool) -> str:
        return _order_clause(order_by, reverse)

    # ------------------------------------------------------------------
    # region() — smart R-tree vs B-tree dispatch
    # ------------------------------------------------------------------

    def region(
        self,
        region=None,
        seqid: str | None = None,
        start: int | None = None,
        end: int | None = None,
        strand: str | None = None,
        featuretype: str | list[str] | None = None,
        completely_within: bool = False,
    ) -> Iterator[Feature]:
        rseqid, rstart, rend = self._normalize_region_args(region, seqid, start, end)
        # Decide path.
        use_rtree = (
            self._rtree_built and rseqid is not None and rstart is not None and rend is not None
        )
        if use_rtree:
            sql, params = self._region_sql_rtree(
                rseqid, rstart, rend, strand, featuretype, completely_within
            )
        else:
            sql, params = self._region_sql_btree(
                rseqid, rstart, rend, strand, featuretype, completely_within
            )
        yield from self._yield_features(sql, params)

    def _region_sql_rtree(self, seqid, start, end, strand, featuretype, completely_within):
        # Phase 7: each seqid lives in its own y-band, so the R-tree query
        # envelope is tight on both axes — no cross-chromosome candidates.
        seqid_y = self._seqid_y_map.get(seqid)
        if seqid_y is None:
            # Unknown seqid (e.g., not in this DB). Fall through to a query
            # that returns no rows but is still valid.
            return self._region_sql_btree(seqid, start, end, strand, featuretype, completely_within)
        where = ["seqid = ?", "ST_Intersects(bbox, ST_MakeEnvelope(?, ?, ?, ?))"]
        params = [seqid, start, seqid_y, end, seqid_y + 1]
        if completely_within:
            where.append('start >= ? AND "end" <= ?')
            params.extend([start, end])
        else:
            seg_where, seg_params = self._segment_overlap(start, end)
            where.extend(seg_where)
            params.extend(seg_params)
        if strand is not None:
            where.append("strand = ?")
            params.append(strand)
        if featuretype is not None:
            if isinstance(featuretype, (list, tuple, set)):
                ph = ",".join("?" * len(featuretype))
                where.append(f"featuretype IN ({ph})")
                params.extend(featuretype)
            else:
                where.append("featuretype = ?")
                params.append(featuretype)
        sql = f"SELECT {_SELECT_FEATURE} FROM features WHERE {' AND '.join(where)} ORDER BY start"
        return sql, params

    def _region_sql_btree(self, seqid, start, end, strand, featuretype, completely_within):
        where = []
        params = []
        if seqid is not None:
            where.append("seqid = ?")
            params.append(seqid)
        if start is not None and end is not None:
            if completely_within:
                where.append('start >= ? AND "end" <= ?')
                params.extend([start, end])
            else:
                # Standard overlap: feature.start <= region.end AND feature.end >= region.start
                where.append('start <= ? AND "end" >= ?')
                params.extend([end, start])
                seg_where, seg_params = self._segment_overlap(start, end)
                where.extend(seg_where)
                params.extend(seg_params)
        elif start is not None:
            where.append("start >= ?")
            params.append(start)
        elif end is not None:
            where.append('"end" <= ?')
            params.append(end)
        if strand is not None:
            where.append("strand = ?")
            params.append(strand)
        if featuretype is not None:
            if isinstance(featuretype, (list, tuple, set)):
                ph = ",".join("?" * len(featuretype))
                where.append(f"featuretype IN ({ph})")
                params.extend(featuretype)
            else:
                where.append("featuretype = ?")
                params.append(featuretype)
        sql = f"SELECT {_SELECT_FEATURE} FROM features"
        if where:
            sql += " WHERE " + " AND ".join(where)
        sql += " ORDER BY start"
        return sql, params

    @staticmethod
    def _normalize_region_args(region, seqid, start, end):
        """Return ``(seqid, start, end)`` triple from any of the four legacy
        input shapes."""
        if region is not None and any(v is not None for v in (seqid, start, end)):
            raise ValueError("pass either `region` or seqid/start/end, not both")
        if region is None:
            return seqid, start, end
        if isinstance(region, str):
            # "chr:start-end" or "chr:start-end:strand"
            chrom_rest = region.split(":", 1)
            if len(chrom_rest) == 1:
                return chrom_rest[0], None, None
            chrom, rest = chrom_rest
            if "-" in rest:
                s, e = rest.split("-", 1)
                # strip optional ":strand" suffix.
                if ":" in e:
                    e = e.split(":", 1)[0]
                return chrom, int(s), int(e)
            return chrom, None, None
        if isinstance(region, tuple):
            if len(region) == 3:
                return region[0], int(region[1]), int(region[2])
            if len(region) == 2:
                return region[0], None, None
            raise ValueError(f"region tuple must be (seqid, start, end); got {region!r}")
        if isinstance(region, Feature):
            return region.seqid, region.start, region.end
        raise TypeError(f"unsupported region type: {type(region)!r}")

    # ------------------------------------------------------------------
    # Phase 12 — Vectorized batched spatial API.
    # ------------------------------------------------------------------

    def region_batched(
        self,
        regions,
        featuretype: str | list[str] | None = None,
        completely_within: bool = False,
        format: str = "arrow",
        explode_segments: bool = False,
    ):
        """Bulk overlap query. Performs a SINGLE spatial JOIN between every
        input region and the features table, returning a column-oriented
        result that maps each query (`query_idx`) back to its overlapping
        features.

        Parameters
        ----------
        regions : iterable of (seqid, start, end) | str | Feature
            Each item is normalized through `_normalize_region_args`; the
            same four input shapes accepted by `region()` are accepted here.
        featuretype : str | list[str] | None
            Optional `features.featuretype` filter applied to all regions.
        completely_within : bool
            If True, only features fully contained in the region are returned
            (default False — overlap is sufficient).
        format : "arrow" | "df" | "polars"
            Return shape (default `"arrow"`).

        explode_segments : bool
            Emit one row per physical INPUT LINE rather than one per logical
            feature, adding a `seg_idx` column. A discontinuous feature then
            contributes a row per segment, each with its own coordinates,
            score and phase -- which is what a caller writing a coverage track
            or exporting to a line-oriented format actually needs.

            Offered on the tabular APIs only, never on `region()` /
            `children()` / `all_features()`: those must keep yielding
            `Feature` objects, and letting `FeatureSegment` rows leak into
            them would corrupt legacy consumers.

        Result columns are query_idx, query_seqid, query_start,
        query_end, id, seqid, source, featuretype, start, end, score,
        strand, frame, file_order -- plus seg_idx when `explode_segments`.
        """
        rows = []
        for r in regions:
            if isinstance(r, tuple) and len(r) == 3 and all(v is not None for v in r):
                seqid, rs, re_ = r[0], int(r[1]), int(r[2])
            else:
                seqid, rs, re_ = self._normalize_region_args(r, None, None, None)
            if seqid is None or rs is None or re_ is None:
                continue
            rows.append((seqid, int(rs), int(re_)))
        if not rows:
            return self._empty_region_batched(format, explode_segments)

        import pyarrow as pa

        regions_table = pa.table(
            {
                "query_idx": list(range(len(rows))),
                "query_seqid": [r[0] for r in rows],
                "query_start": [r[1] for r in rows],
                "query_end": [r[2] for r in rows],
            }
        )
        self.conn.register("__staging_regions", regions_table)
        try:
            # The R-tree path uses the seqid_y-encoded envelope so that
            # DuckDB's spatial index segregates chromosomes (Phase 7).
            # B-tree fallback is identical SQL minus the ST_Intersects
            # predicate.
            ft_where, ft_params = self._featuretype_filter(featuretype, qualifier="f")
            ft_clause = (" AND " + " AND ".join(ft_where)) if ft_where else ""
            within_clause = (
                ' AND f.start >= q.query_start AND f."end" <= q.query_end'
                if completely_within
                else ""
            )
            # Same envelope-superset recheck as `_segment_overlap`, but
            # correlated against the staged query columns rather than bound
            # parameters. Empty -- and so byte-identical to v1 -- unless this
            # database actually holds a multipart feature. `sg`, not `s`: the
            # R-tree arm already binds `s` to the seqid lookup.
            # Exploding replaces the logical projection with `segments_all`,
            # which yields exactly one row per physical input line. The join to
            # `features` stays -- it is what carries the R-tree predicate -- so
            # the spatial index is still doing the selection and the extra join
            # only expands the rows it found.
            src = "sa" if explode_segments else "f"
            seg_cols = ", sa.seg_idx" if explode_segments else ""
            # `(query_idx, start)` alone is not a total order: two features
            # sharing a start came back in whatever order the join happened to
            # produce, which differed between the logical and exploded forms of
            # the same query. `id` makes it deterministic, and `seg_idx` keeps a
            # feature's own lines in file order.
            seg_order = ", sa.seg_idx" if explode_segments else ""
            explode_join = ""
            if explode_segments:
                bounds = (
                    'sa.start >= q.query_start AND sa."end" <= q.query_end'
                    if completely_within
                    else 'sa.start <= q.query_end AND sa."end" >= q.query_start'
                )
                explode_join = " JOIN segments_all sa ON sa.feature_id = f.id AND " + bounds

            seg_clause = ""
            if self._n_multipart and not completely_within and not explode_segments:
                seg_clause = (
                    " AND (f.n_segments = 1 OR EXISTS ("
                    "SELECT 1 FROM segments sg WHERE sg.feature_id = f.id "
                    'AND sg.start <= q.query_end AND sg."end" >= q.query_start))'
                )

            if self._rtree_built and self._seqid_y_map:
                # Inline the seqid → seqid_y map as a small VALUES table so
                # the JOIN can prune by chromosome inside the R-tree.
                values_pairs = ",".join(
                    f"(?, {y})" for y in (self._seqid_y_map[s] for s in self._seqid_y_map)
                )
                seqid_y_params = list(self._seqid_y_map.keys())
                sql = f"""
                    WITH seqid_lookup(seqid, seqid_y) AS (
                        VALUES {values_pairs}
                    )
                    SELECT
                        q.query_idx, q.query_seqid, q.query_start, q.query_end,
                        f.id, {src}.seqid, {src}.source, {src}.featuretype,
                        {src}.start, {src}."end" AS "end",
                        {src}.score, {src}.strand, {src}.frame, {src}.file_order{seg_cols}
                    FROM __staging_regions q
                    JOIN seqid_lookup s ON s.seqid = q.query_seqid
                    JOIN features f
                      ON f.seqid = q.query_seqid
                      AND ST_Intersects(
                          f.bbox,
                          ST_MakeEnvelope(q.query_start, s.seqid_y,
                                          q.query_end,   s.seqid_y + 1)){explode_join}
                    WHERE 1=1{within_clause}{seg_clause}{ft_clause}
                    ORDER BY q.query_idx, {src}.start, f.id{seg_order}
                """
                params = list(seqid_y_params) + ft_params
            else:
                sql = f"""
                    SELECT
                        q.query_idx, q.query_seqid, q.query_start, q.query_end,
                        f.id, {src}.seqid, {src}.source, {src}.featuretype,
                        {src}.start, {src}."end" AS "end",
                        {src}.score, {src}.strand, {src}.frame, {src}.file_order{seg_cols}
                    FROM __staging_regions q
                    JOIN features f
                      ON f.seqid = q.query_seqid
                      AND f.start <= q.query_end
                      AND f."end"  >= q.query_start{explode_join}
                    WHERE 1=1{within_clause}{seg_clause}{ft_clause}
                    ORDER BY q.query_idx, {src}.start, f.id{seg_order}
                """
                params = list(ft_params)

            return self._materialize_batched(sql, params, format=format)
        finally:
            try:
                self.conn.unregister("__staging_regions")
            except Exception:
                pass

    def _empty_region_batched(self, format: str, explode_segments: bool = False):
        import pyarrow as pa

        schema = pa.schema(
            [
                ("query_idx", pa.int64()),
                ("query_seqid", pa.string()),
                ("query_start", pa.int64()),
                ("query_end", pa.int64()),
                ("id", pa.string()),
                ("seqid", pa.string()),
                ("source", pa.string()),
                ("featuretype", pa.string()),
                ("start", pa.int64()),
                ("end", pa.int64()),
                ("score", pa.string()),
                ("strand", pa.string()),
                ("frame", pa.string()),
                ("file_order", pa.int64()),
            ]
            # An empty result must have the SAME columns as a populated one,
            # or a caller reading `seg_idx` breaks exactly when nothing matched.
            + ([("seg_idx", pa.int32())] if explode_segments else [])
        )
        empty = pa.table({name: [] for name in schema.names}, schema=schema)
        fmt = format.lower()
        if fmt == "arrow":
            return empty
        if fmt in ("df", "pandas"):
            try:
                return empty.to_pandas()
            except ModuleNotFoundError as e:  # pragma: no cover
                raise ImportError(
                    "format='df' requires the optional pandas package "
                    "(pip install 'gffbase[pandas]')"
                ) from e
        if fmt == "polars":
            try:
                import polars as pl
            except ImportError as e:  # pragma: no cover
                raise ImportError(
                    "format='polars' requires the optional polars package "
                    "(pip install 'gffbase[polars]')"
                ) from e
            return pl.from_arrow(empty)
        raise ValueError(f"format must be one of 'arrow' | 'df' | 'polars'; got {format!r}")

    # ------------------------------------------------------------------
    # children() / parents() — closure cache + dynamic CTE fallback
    # ------------------------------------------------------------------

    def children(
        self,
        id,
        level: int | None = None,
        featuretype: str | list[str] | None = None,
        order_by=None,
        reverse: bool = False,
        limit=None,
        completely_within: bool = False,
    ) -> Iterator[Feature]:
        target_id = _require_feature_id(id)
        yield from self._relation_query(
            target_id,
            level,
            featuretype,
            order_by,
            reverse,
            limit,
            completely_within,
            direction="children",
        )

    def parents(
        self,
        id,
        level: int | None = None,
        featuretype: str | list[str] | None = None,
        order_by=None,
        reverse: bool = False,
        completely_within: bool = False,
        limit=None,
    ) -> Iterator[Feature]:
        target_id = _require_feature_id(id)
        yield from self._relation_query(
            target_id,
            level,
            featuretype,
            order_by,
            reverse,
            limit,
            completely_within,
            direction="parents",
        )

    # ------------------------------------------------------------------
    # Phase 12 — Vectorized batched API.
    #
    # The row-by-row `children()` / `parents()` / `region()` generators
    # carry per-row Python overhead that dominates wall time on small
    # GENCODE-scale queries (Phase 11 §4.2). The methods below replace the
    # per-id loop with a single bulk SQL query and return the result as a
    # zero-copy PyArrow `Table` (or pandas / polars DataFrame), letting
    # downstream ML pipelines consume the data column-wise without ever
    # materializing a Python `Feature` object.
    # ------------------------------------------------------------------

    def children_batched(
        self,
        feature_ids,
        level: int | None = None,
        featuretype: str | list[str] | None = None,
        format: str = "arrow",
        explode_segments: bool = False,
    ):
        """Bulk children lookup. Returns the descendants of ALL `feature_ids`
        in a single vectorized DuckDB query.

        Parameters
        ----------
        feature_ids : iterable of str | Feature
            Anchors. May contain `Feature` objects or raw ID strings.
        level : int | None
            None → all descendants (closure cache when materialized,
            otherwise dynamic CTE). Integer → exact-depth point lookup.
        featuretype : str | list[str] | None
            Optional filter on `features.featuretype`.
        format : "arrow" | "df" | "polars"
            Return shape (default `"arrow"` — `pyarrow.Table`).

        explode_segments : bool
            Emit one row per physical INPUT LINE rather than one per logical
            feature, adding a `seg_idx` column. Tabular APIs only -- see
            `region_batched`.

        Result columns are anchor (the parent ID supplied),
        descendant_id, seqid, source, featuretype, start, end, score,
        strand, frame, file_order, depth -- plus seg_idx when
        `explode_segments`.
        """
        return self._batched_relation(
            feature_ids,
            level=level,
            featuretype=featuretype,
            direction="children",
            format=format,
            explode_segments=explode_segments,
        )

    def parents_batched(
        self,
        feature_ids,
        level: int | None = None,
        featuretype: str | list[str] | None = None,
        format: str = "arrow",
        explode_segments: bool = False,
    ):
        """Bulk parents lookup. Mirrors `children_batched` but walks the
        closure / edges in the reverse direction."""
        return self._batched_relation(
            feature_ids,
            level=level,
            featuretype=featuretype,
            direction="parents",
            format=format,
            explode_segments=explode_segments,
        )

    def _batched_relation(
        self,
        feature_ids,
        *,
        level: int | None,
        featuretype,
        direction: str,
        format: str,
        explode_segments: bool = False,
    ):
        ids = self._coerce_id_list(feature_ids)
        if not ids:
            return self._empty_batched_result(
                format,
                "children" if direction == "children" else "parents",
                explode_segments,
            )

        # Decide cache vs dynamic the same way the row-by-row dispatcher
        # does — it's a one-time decision per call here, not per-row.
        use_dynamic = (level is not None and level > self._max_depth) or (
            level is None and self._closure_max_depth == 0
        )

        ph = ",".join("?" * len(ids))
        ft_where, ft_params = self._featuretype_filter(featuretype, qualifier="f")
        ft_clause = (" AND " + " AND ".join(ft_where)) if ft_where else ""

        # `segments_all` yields one row per physical input line and, for a
        # singleton feature, exactly that feature's own row -- so swapping the
        # joined relation is the whole of "explode". `id` becomes `feature_id`
        # there, since a row is now a line rather than a feature.
        rel = "segments_all" if explode_segments else "features"
        id_col = "feature_id" if explode_segments else "id"
        seg_cols = ", f.seg_idx" if explode_segments else ""

        if direction == "children":
            anchor_alias, descendant_alias = "ancestor", "descendant"
            edge_anchor_col, edge_descendant_col = "parent", "child"
        else:
            anchor_alias, descendant_alias = "descendant", "ancestor"
            edge_anchor_col, edge_descendant_col = "child", "parent"

        if use_dynamic:
            # Recursive CTE seeded by every anchor in the batch.
            max_walk = level if level is not None else max(64, self._max_depth * 4)
            depth_filter = " AND w.depth = ?" if level is not None else ""
            cte = f"""
                WITH RECURSIVE walk(anchor, id, depth, seen) AS (
                    SELECT {edge_anchor_col}, {edge_descendant_col}, 1,
                           [{edge_anchor_col}, {edge_descendant_col}]
                    FROM edges WHERE {edge_anchor_col} IN ({ph})
                    UNION ALL
                    SELECT w.anchor, e.{edge_descendant_col}, w.depth + 1,
                           list_append(w.seen, e.{edge_descendant_col})
                    FROM walk w
                    JOIN edges e ON e.{edge_anchor_col} = w.id
                    -- See CLOSURE_RECURSIVE_CTE: no node twice on one path.
                    WHERE w.depth < ? AND NOT list_contains(w.seen, e.{edge_descendant_col})
                )
                SELECT
                    w.anchor       AS anchor,
                    f.{id_col}     AS descendant_id,
                    f.seqid, f.source, f.featuretype,
                    f.start, f."end" AS "end",
                    f.score, f.strand, f.frame, f.file_order, w.depth{seg_cols}
                FROM walk w
                JOIN {rel} f ON f.{id_col} = w.id
                WHERE 1=1{depth_filter}{ft_clause}
            """
            params: list = list(ids) + [max_walk]
            if level is not None:
                params.append(level)
            params.extend(ft_params)
        else:
            # Closure cache hit — single set-based JOIN.
            depth_filter = " AND c.depth = ?" if level is not None else ""
            cte = f"""
                SELECT
                    c.{anchor_alias}     AS anchor,
                    f.{id_col}            AS descendant_id,
                    f.seqid, f.source, f.featuretype,
                    f.start, f."end" AS "end",
                    f.score, f.strand, f.frame, f.file_order, c.depth{seg_cols}
                FROM closure c
                JOIN {rel} f ON f.{id_col} = c.{descendant_alias}
                WHERE c.{anchor_alias} IN ({ph}){depth_filter}{ft_clause}
            """
            params = list(ids)
            if level is not None:
                params.append(level)
            params.extend(ft_params)

        return self._materialize_batched(cte, params, format=format)

    @staticmethod
    def _coerce_id_list(feature_ids) -> list[str]:
        """Normalize a heterogeneous iterable of (id-string | Feature) into a
        list of strings."""
        out: list[str] = []
        for item in feature_ids:
            if isinstance(item, Feature):
                out.append(_require_feature_id(item))
            elif isinstance(item, str):
                out.append(item)
            else:
                raise TypeError(f"feature_ids may contain only str or Feature; got {type(item)!r}")
        return out

    def _materialize_batched(self, sql: str, params: list, *, format: str):
        """Execute `sql` and return the result in the requested shape.

        DuckDB's `fetch_arrow_table()` is a zero-copy hand-off — the
        returned `pyarrow.Table` shares the same Arrow buffers DuckDB uses
        internally, with no per-row Python boundary crossings.
        """
        cur = self.conn.execute(sql, params)
        fmt = format.lower()
        if fmt == "arrow":
            # DuckDB ≥ 1.0 prefers `to_arrow_table()`; fall back to the older
            # `fetch_arrow_table()` for environments pinning ≤ 0.10.
            return (
                getattr(cur, "to_arrow_table", cur.fetch_arrow_table)()
                if hasattr(cur, "to_arrow_table")
                else cur.fetch_arrow_table()
            )
        if fmt in ("df", "pandas"):
            try:
                return cur.df()
            except ModuleNotFoundError as e:  # pragma: no cover
                raise ImportError(
                    "format='df' requires the optional pandas package "
                    "(pip install 'gffbase[pandas]')"
                ) from e
        if fmt == "polars":
            try:
                return cur.pl()  # DuckDB >=1.0 returns a polars.DataFrame
            except ModuleNotFoundError as e:  # pragma: no cover
                raise ImportError(
                    "format='polars' requires the optional polars package "
                    "(pip install 'gffbase[polars]')"
                ) from e
        raise ValueError(f"format must be one of 'arrow' | 'df' | 'polars'; got {format!r}")

    def _empty_batched_result(self, format: str, direction: str, explode_segments: bool = False):
        """Return a properly-typed empty result when the input id list is
        empty. Avoids issuing a SQL query at all."""
        import pyarrow as pa

        schema = pa.schema(
            [
                ("anchor", pa.string()),
                ("descendant_id", pa.string()),
                ("seqid", pa.string()),
                ("source", pa.string()),
                ("featuretype", pa.string()),
                ("start", pa.int64()),
                ("end", pa.int64()),
                ("score", pa.string()),
                ("strand", pa.string()),
                ("frame", pa.string()),
                ("file_order", pa.int64()),
                ("depth", pa.int16()),
            ]
            + ([("seg_idx", pa.int32())] if explode_segments else [])
        )
        empty = pa.table({name: [] for name in schema.names}, schema=schema)
        fmt = format.lower()
        if fmt == "arrow":
            return empty
        if fmt in ("df", "pandas"):
            try:
                return empty.to_pandas()
            except ModuleNotFoundError as e:  # pragma: no cover
                raise ImportError(
                    "format='df' requires the optional pandas package "
                    "(pip install 'gffbase[pandas]')"
                ) from e
        if fmt == "polars":
            try:
                import polars as pl
            except ImportError as e:  # pragma: no cover
                raise ImportError(
                    "format='polars' requires the optional polars package "
                    "(pip install 'gffbase[polars]')"
                ) from e
            return pl.from_arrow(empty)
        raise ValueError(f"format must be one of 'arrow' | 'df' | 'polars'; got {format!r}")

    def _relation_query(
        self,
        target_id: str,
        level: int | None,
        featuretype,
        order_by,
        reverse: bool,
        limit,
        completely_within: bool,
        direction: str,
    ):
        # Phase 7 — smart cache-vs-dynamic dispatcher.
        use_dynamic = self._dispatch_relation(level, target_id, direction)
        if use_dynamic:
            sql, params = self._relation_sql_dynamic(
                target_id,
                level,
                featuretype,
                order_by,
                reverse,
                limit,
                completely_within,
                direction,
            )
        else:
            sql, params = self._relation_sql_cached(
                target_id,
                level,
                featuretype,
                order_by,
                reverse,
                limit,
                completely_within,
                direction,
            )
        yield from self._yield_features(sql, params)

    def _dispatch_relation(self, level: int | None, target_id: str, direction: str) -> bool:
        """Return True iff the caller should be served by the dynamic CTE.

        Decision matrix (Phase 7, revised):

        +-------+------------------+-------------------+--------------+
        | level | closure_max_depth| has_overflow?     | path         |
        +=======+==================+===================+==============+
        | None  | == 0 (no edges)  | n/a               | dynamic CTE  |
        | None  | >= 1             | no                | closure cache|
        | None  | >= 1             | yes               | dynamic CTE  |
        | int   | <= max_depth     | n/a               | closure cache|
        | int   | >  max_depth     | n/a               | dynamic CTE  |
        +-------+------------------+-------------------+--------------+

        Phase 7 measurement on GENCODE v45 (depth 2, 2000 genes / 274k descs):
        forced cache  = 18.35 s
        forced dynamic = 66.82 s
        Cache wins by 3.85× when the corpus's hierarchy fits in the cache.
        Phase 6's earlier finding (cache marginally slower) was an OS-cache
        artifact that disappeared on a clean run with the new R-tree encoding.

        Cache is preferred whenever it can serve the request; dynamic is the
        correctness fallback for traversals that extend past `max_depth`.
        """
        if level is not None:
            return level > self._max_depth
        # level is None.
        if self._closure_max_depth == 0:
            return True  # closure is empty; dynamic walks edges
        # Cache covers most of the tree; check for overflow past the boundary.
        return self._has_overflow(target_id, direction)

    def _has_overflow(self, target_id: str, direction: str) -> bool:
        """Are there descendants/ancestors past max_depth?"""
        if direction == "children":
            sql = """
                SELECT EXISTS (
                    SELECT 1 FROM edges e
                    JOIN closure c ON c.descendant = e.parent
                    WHERE c.ancestor = ? AND c.depth = ?
                )
            """
        else:
            sql = """
                SELECT EXISTS (
                    SELECT 1 FROM edges e
                    JOIN closure c ON c.ancestor = e.child
                    WHERE c.descendant = ? AND c.depth = ?
                )
            """
        return bool(scalar_or(self.conn, sql, False, [target_id, self._max_depth]))

    def _relation_sql_cached(
        self,
        target_id,
        level,
        featuretype,
        order_by,
        reverse,
        limit,
        completely_within,
        direction,
    ):
        if direction == "children":
            join_col, anchor_col = "c.descendant", "c.ancestor"
        else:
            join_col, anchor_col = "c.ancestor", "c.descendant"
        where = [f"{anchor_col} = ?"]
        params = [target_id]
        if level is not None:
            where.append("c.depth = ?")
            params.append(level)
        feat_where, feat_params = self._featuretype_filter(featuretype)
        where.extend(feat_where)
        params.extend(feat_params)
        lim_where, lim_params = self._limit_filter(limit, completely_within)
        where.extend(lim_where)
        params.extend(lim_params)

        sql = (
            f"SELECT {self._select_feature_aliased('f')} "
            f"FROM closure c JOIN features f ON f.id = {join_col} "
            f"WHERE {' AND '.join(where)} "
            f"ORDER BY {self._order_clause_qualified(order_by, reverse, 'f')}"
        )
        return sql, params

    def _relation_sql_dynamic(
        self,
        target_id,
        level,
        featuretype,
        order_by,
        reverse,
        limit,
        completely_within,
        direction,
    ):
        # Dynamic recursive CTE walks the edges table directly.
        if direction == "children":
            base_where = "parent = ?"
            recurse_join = "JOIN edges e ON e.parent = w.id"
            select_col = "child"
        else:
            base_where = "child = ?"
            recurse_join = "JOIN edges e ON e.child = w.id"
            select_col = "parent"

        # Walk depth bound: requested `level` if given, else a generous cap.
        max_walk = level if level is not None else max(64, self._max_depth * 4)

        cte = f"""
            WITH RECURSIVE walk(id, depth, seen) AS (
                SELECT {select_col}, 1, [{select_col}] FROM edges WHERE {base_where}
                UNION ALL
                SELECT e.{select_col}, w.depth + 1, list_append(w.seen, e.{select_col})
                FROM walk w
                {recurse_join}
                -- See CLOSURE_RECURSIVE_CTE: no node twice on one path. This
                -- is the deeper of the two walks -- it runs to depth 64 when
                -- no level is given -- so a cycle here was the most expensive
                -- one to hit.
                WHERE w.depth < ? AND NOT list_contains(w.seen, e.{select_col})
            )
        """
        where = ["1=1"]
        params: list = [target_id, max_walk]
        if level is not None:
            where.append("w.depth = ?")
            params.append(level)
        feat_where, feat_params = self._featuretype_filter(featuretype, qualifier="f")
        where.extend(feat_where)
        params.extend(feat_params)
        lim_where, lim_params = self._limit_filter(limit, completely_within, qualifier="f")
        where.extend(lim_where)
        params.extend(lim_params)

        sql = (
            f"{cte} "
            f"SELECT {self._select_feature_aliased('f')} "
            f"FROM walk w JOIN features f ON f.id = w.id "
            f"WHERE {' AND '.join(where)} "
            f"ORDER BY {self._order_clause_qualified(order_by, reverse, 'f')}"
        )
        return sql, params

    @staticmethod
    def _select_feature_aliased(alias: str) -> str:
        """The same projection as `_SELECT_FEATURE`, qualified by `alias`."""
        return db_row_projection(alias)

    @staticmethod
    def _featuretype_filter(featuretype, *, qualifier: str = "f"):
        if featuretype is None:
            return [], []
        if isinstance(featuretype, (list, tuple, set)):
            ph = ",".join("?" * len(featuretype))
            return [f"{qualifier}.featuretype IN ({ph})"], list(featuretype)
        return [f"{qualifier}.featuretype = ?"], [featuretype]

    def _limit_filter(self, limit, completely_within: bool, *, qualifier: str = "f"):
        if limit is None:
            return [], []
        rseqid, rstart, rend = self._normalize_region_args(limit, None, None, None)
        where = []
        params = []
        if rseqid is not None:
            where.append(f"{qualifier}.seqid = ?")
            params.append(rseqid)
        if rstart is not None and rend is not None:
            if completely_within:
                where.append(f'{qualifier}.start >= ? AND {qualifier}."end" <= ?')
                params.extend([rstart, rend])
            else:
                where.append(f'{qualifier}.start <= ? AND {qualifier}."end" >= ?')
                params.extend([rend, rstart])
                seg_where, seg_params = self._segment_overlap(rstart, rend, qualifier=qualifier)
                where.extend(seg_where)
                params.extend(seg_params)
        return where, params

    @staticmethod
    def _order_clause_qualified(order_by, reverse, qualifier):
        return _order_clause(order_by, reverse, qualifier)

    # ------------------------------------------------------------------
    # Mutation
    # ------------------------------------------------------------------

    def delete(self, features, make_backup: bool = True, **kwargs) -> FeatureDB:
        ids = self._coerce_ids(features)
        if not ids:
            return self
        placeholders = ",".join("?" * len(ids))
        self.conn.execute(f"DELETE FROM features WHERE id IN ({placeholders})", ids)
        self.conn.execute(f"DELETE FROM attributes WHERE feature_id IN ({placeholders})", ids)
        # `segments` too. Without this the segment rows outlive their feature,
        # and `segments_all` joins them straight back -- so every physical-level
        # consumer (`export_sqlite`, the validator, `explode_segments`) would
        # keep reporting lines of a feature the caller deleted.
        self.conn.execute(f"DELETE FROM segments WHERE feature_id IN ({placeholders})", ids)
        self.conn.execute(
            f"DELETE FROM edges WHERE parent IN ({placeholders}) OR child IN ({placeholders})",
            ids + ids,
        )
        self.conn.execute(
            f"DELETE FROM closure WHERE ancestor IN ({placeholders}) OR descendant IN ({placeholders})",
            ids + ids,
        )
        return self

    def update(self, data, make_backup: bool = True, **kwargs) -> FeatureDB:
        # Phase 5 minimal update: accept iterable of Feature objects and
        # append them to features + attributes + edges, then refresh closure.
        from gffbase.feature import ParsedFeature
        from gffbase.ingest import _ArrowBatchBuilder

        # Phase 19: the builder needs the seqid_to_y dict so it can stamp
        # seqid_y (and bbox, when the R-tree is live) inline. Reuse the map
        # the FeatureDB already loaded from `seqid_map`.
        builder = _ArrowBatchBuilder(
            self._seqid_y_map,
            has_spatial=bool(self._rtree_built),
        )
        order = scalar_or(self.conn, "SELECT COALESCE(MAX(file_order), 0) FROM features", 0)

        if isinstance(data, FeatureDB):
            data = list(data.all_features())
        for feat in data:
            order += 1
            if isinstance(feat, Feature):
                blob = (
                    feat._attributes_blob
                    if feat._attributes_blob is not None
                    else feat._format_attributes().encode("utf-8")
                )
                pairs = [(k, v, i) for k, vs in feat.attributes.items() for i, v in enumerate(vs)]
                pf = ParsedFeature(
                    seqid=feat.seqid,
                    source=feat.source,
                    featuretype=feat.featuretype,
                    start=feat.start,
                    end=feat.end,
                    score=feat.score,
                    strand=feat.strand,
                    frame=feat.frame,
                    attributes_blob=blob,
                    attributes_pairs=pairs,
                    extra=list(feat.extra),
                )
                fid = feat.id or f"{feat.featuretype}_{order}"
            elif isinstance(feat, ParsedFeature):
                pf = feat
                fid = next(
                    (v for k, v, _ in pf.attributes_pairs if k == "ID"),
                    f"{pf.featuretype}_{order}",
                )
            else:
                raise TypeError(f"update() does not accept {type(feat)!r}")
            builder.append(fid, pf, order)
        builder.flush_into(self.conn)
        # Refresh closure: rebuild from edges (cheap on small updates).
        self.conn.execute("DELETE FROM closure")
        from gffbase.schema import CLOSURE_RECURSIVE_CTE

        self.conn.execute(CLOSURE_RECURSIVE_CTE, [self._max_depth])
        return self

    def add_relation(
        self, parent, child, level: int = 1, parent_func=None, child_func=None
    ) -> FeatureDB:
        """Link one parent to one child.

        `parent_func` / `child_func` receive `(parent, child)`, and whatever
        they RETURN is written back to the database. That is upstream's
        contract and the reason `assign_child` returns the child: the callback
        exists to edit an attribute (`Parent=`) that then has to be persisted.
        Previously the return value was discarded and nothing was written, so
        `child_func=assign_child` set an attribute on a throwaway object.

        A string id is resolved to a `Feature` first, so callbacks fire whether
        the caller passed objects or ids -- they used to be skipped silently
        for ids.
        """
        return self.add_relations(
            [(parent, child)], level=level, parent_func=parent_func, child_func=child_func
        )

    def add_relations(self, pairs, level: int = 1, parent_func=None, child_func=None) -> FeatureDB:
        """`add_relation` for many pairs, with ONE closure rebuild.

        Deriving the closure costs a recursive CTE over every edge, so doing it
        per pair makes a bulk operation quadratic. `merge_all` links every
        component of every merged feature and is the caller that made this
        necessary.
        """
        pairs = list(pairs)
        if not pairs:
            return self

        edges: list[list[str]] = []
        touched: dict[str, Feature] = {}
        for parent, child in pairs:
            parent = self[parent] if isinstance(parent, str) else parent
            child = self[child] if isinstance(child, str) else child
            edges.append([_require_feature_id(parent), _require_feature_id(child)])
            if parent_func is not None:
                updated = parent_func(parent, child)
                if updated is not None:
                    touched[_require_feature_id(updated)] = updated
            if child_func is not None:
                updated = child_func(parent, child)
                if updated is not None:
                    touched[_require_feature_id(updated)] = updated

        self.conn.executemany("INSERT INTO edges(parent, child) VALUES (?, ?)", edges)
        for feature in touched.values():
            self._write_back(feature)

        from gffbase.schema import CLOSURE_RECURSIVE_CTE

        self.conn.execute("DELETE FROM closure")
        self.conn.execute(CLOSURE_RECURSIVE_CTE, [self._max_depth])
        return self

    def _insert(self, feature: Feature) -> FeatureDB:
        """Write one NEW feature into the database.

        The oracle's private seam, reproduced because `merge_all` needs it and
        because a successor is expected to have an equivalent. Delegates to
        `update`, which owns the Arrow batch machinery.
        """
        return self.update([feature])

    def _write_back(self, feature: Feature) -> None:
        """Persist edits to a feature that is ALREADY in the database.

        `update()` appends, and `features.id` is a primary key, so it cannot
        stand in for this: re-inserting an existing feature is a constraint
        violation, not an update. Rewrites the scalar columns, replaces the
        long-form attribute rows, and refreshes the derived bbox so the R-tree
        cannot go stale behind the B-tree (INV-8).
        """
        fid = _require_feature_id(feature)
        blob = feature._format_attributes().encode("utf-8")
        self.conn.execute(
            'UPDATE features SET seqid = ?, source = ?, featuretype = ?, start = ?, "end" = ?, '
            "score = ?, strand = ?, frame = ?, attributes_blob = ? WHERE id = ?",
            [
                feature.seqid,
                feature.source,
                feature.featuretype,
                feature.start,
                feature.end,
                feature.score,
                feature.strand,
                feature.frame,
                blob,
                fid,
            ],
        )
        self.conn.execute("DELETE FROM attributes WHERE feature_id = ?", [fid])
        rows = [
            [fid, key, value, idx]
            for key, values in feature.attributes.items()
            for idx, value in enumerate(values)
        ]
        if rows:
            self.conn.executemany(
                "INSERT INTO attributes(feature_id, key, value, idx) VALUES (?, ?, ?, ?)", rows
            )
        if self._rtree_built:
            self.conn.execute(
                'UPDATE features SET bbox = CASE WHEN start IS NULL OR "end" IS NULL THEN NULL '
                'ELSE ST_MakeEnvelope(start, seqid_y, "end", seqid_y + 1) END WHERE id = ?',
                [fid],
            )

    @staticmethod
    def _coerce_ids(features) -> list[str]:
        if isinstance(features, str):
            return [features]
        if isinstance(features, Feature):
            return [_require_feature_id(features)]
        if isinstance(features, FeatureDB):
            return [_require_feature_id(f) for f in features.all_features()]
        out: list[str] = []
        for f in features:
            if isinstance(f, str):
                out.append(f)
            elif isinstance(f, Feature):
                out.append(_require_feature_id(f))
        return out

    # ------------------------------------------------------------------
    # Synthesis / convenience
    # ------------------------------------------------------------------

    def interfeatures(
        self,
        features,
        new_featuretype=None,
        merge_attributes: bool = True,
        numeric_sort: bool = False,
        dialect=None,
        attribute_func=None,
        update_attributes=None,
    ):
        """The gaps between consecutive features.

        `attribute_func` takes ONE argument -- an attribute mapping -- and
        returns one, matching the oracle. It is applied to each neighbour's
        attributes *before* they are merged, not to the merged result. The
        previous three-argument `(prev, cur, attrs)` form meant any gffutils
        caller passing a callback got a `TypeError`.
        """
        from gffbase.helpers import merge_attributes as _merge_attrs

        if attribute_func is None:

            def attribute_func(a):
                return a

        feats = _with_coordinates(features)
        last = None
        for cur in feats:
            if last is None:
                last = cur
                continue
            if cur.seqid != last.seqid:
                # A gap between two different sequences is not a gap. Without
                # this the pair produced a feature spanning nothing, stamped
                # with the previous sequence's name.
                last = cur
                continue

            new_start = last.end + 1
            new_end = cur.start - 1
            if new_end < new_start:
                last = cur
                continue

            if merge_attributes:
                attrs = _merge_attrs(
                    attribute_func(dict(last.attributes)),
                    attribute_func(dict(cur.attributes)),
                    numeric_sort=numeric_sort,
                )
            else:
                attrs = {}
            if update_attributes:
                attrs.update(update_attributes)

            # A feature may not carry several IDs, so a merged pair's two IDs
            # become one hyphenated id rather than a multi-valued attribute.
            if len(attrs.get("ID", [])) > 1:
                attrs["ID"] = ["-".join(attrs["ID"])]

            yield Feature(
                seqid=last.seqid,
                source=self.derived_source,
                featuretype=(
                    new_featuretype
                    if new_featuretype is not None
                    else f"inter_{last.featuretype}_{cur.featuretype}"
                ),
                start=new_start,
                end=new_end,
                score=".",
                # Where the flanks disagree the gap has no orientation. This
                # used to inherit the left flank's strand unconditionally.
                strand=cur.strand if last.strand == cur.strand else ".",
                attributes=attrs,
                dialect=dialect or self.dialect,
            )
            last = cur

    @property
    def derived_source(self) -> str:
        """`source` for features this database derives rather than reads.

        `gffutils_derived` under compat so ported scripts that filter on it
        keep working; `gffbase_derived` under strict, which reports honest
        provenance.
        """
        return DERIVED_SOURCE.get(self.mode, DERIVED_SOURCE[MODE_COMPAT])

    def merge(self, features, merge_criteria=None, multiline: bool = False):
        """Collapse runs of features that satisfy every criterion.

        Consumes `features` **in the order given**. That is the oracle's
        contract and it matters: `merge_all` supplies a specific
        `merge_order`, and re-sorting here (which this used to do) silently
        discarded it.

        A feature that merged with nothing is yielded unchanged with
        `children` set to `no_children`, so a caller can tell a real merge from
        a pass-through by truthiness. `merge_all` depends on exactly that.
        """
        from gffbase import merge_criteria as mc

        if merge_criteria is None:
            merge_criteria = (mc.seqid, mc.overlap_end_inclusive, mc.strand, mc.feature_type)
        elif not isinstance(merge_criteria, (list, tuple)):
            merge_criteria = [merge_criteria]

        accum: Feature | None = None
        components: list[Feature] = []
        last_id: str | None = None

        for f in _with_coordinates(features):
            if accum is None:
                # A feature that fails its own criteria can never merge with
                # anything, so pass it straight through rather than opening a
                # run with it. Without this pre-pass such a feature was
                # silently accumulated into the next run.
                if all(pred(f, f, components) for pred in merge_criteria):
                    accum, components, last_id = f, [f], None
                else:
                    yield _finalize_merge(f, no_children)
                continue

            if not components:
                # `accum` came from a previous run's tail and has not been
                # checked against its own criteria yet.
                if all(pred(accum, accum, components) for pred in merge_criteria):
                    components.append(accum)
                else:
                    yield _finalize_merge(accum, no_children)
                    accum, last_id = f, None
                    continue

            if not all(pred(accum, f, components) for pred in merge_criteria):
                yield _finalize_merge(accum, components)
                accum, components, last_id = f, [], None
                continue

            if len(components) == 1:
                # About to merge for real, so stop mutating the caller's
                # feature and take a copy with an identity of its own.
                accum = self._clone_for_merge(accum)
                if not last_id:
                    last_id = self._next_autoincrement_id(accum.featuretype)
                accum.id = last_id
                accum.attributes["ID"] = last_id
            components.append(f)

            # Ambiguity flags: where the components disagree, say so rather
            # than silently keeping the first one's value.
            if f.seqid not in accum.seqid.split(","):
                accum.seqid += "," + f.seqid
            if f.strand != accum.strand:
                accum.strand = "."
            if f.frame != accum.frame:
                accum.frame = "."
            if f.featuretype != accum.featuretype:
                accum.featuretype = "sequence_feature"
            # Both ends, not just the far one: with a caller-chosen
            # `merge_order` the run is not necessarily start-sorted, and only
            # extending `end` quietly truncated the merged feature.
            if f.start < accum.start:
                accum.start = f.start
            if f.end > accum.end:
                accum.end = f.end

        if accum is not None:
            yield _finalize_merge(accum, components)

    def _next_autoincrement_id(self, featuretype: str) -> str:
        """`<featuretype>_<n>`, the oracle's scheme for a synthesized id.

        Counters live in the `autoincrements` table so that ids stay unique
        across sessions -- an in-memory counter would restart at 1 on reopen
        and collide with what a previous run wrote.
        """
        n = scalar_or(self.conn, "SELECT n FROM autoincrements WHERE base = ?", 0, [featuretype])
        while True:
            n += 1
            candidate = f"{featuretype}_{n}"
            if not self.__contains__(candidate):
                break
        self.conn.execute(
            "INSERT OR REPLACE INTO autoincrements(base, n) VALUES (?, ?)", [featuretype, n]
        )
        return candidate

    @staticmethod
    def _clone_for_merge(f: Feature) -> Feature:
        return Feature(
            seqid=f.seqid,
            source=f.source,
            featuretype=f.featuretype,
            start=f.start,
            end=f.end,
            score=f.score,
            strand=f.strand,
            frame=f.frame,
            attributes={k: list(v) for k, v in f.attributes.items()},
            dialect=f.dialect,
        )

    def merge_all(
        self,
        merge_order=("seqid", "featuretype", "strand", "start"),
        merge_criteria=None,
        featuretypes_groups=(None,),
        exclude_components: bool = False,
    ) -> list[Feature]:
        """Merge everything in the database and **write the results back**.

        Three things were wrong here and all three were silent. The method
        returned every input feature including ones that merged with nothing,
        so a caller could not tell what had actually been merged; it persisted
        nothing, despite documenting that "the resulting records are added to
        the database"; and it accepted `exclude_components` and ignored it, so
        asking for the components to be removed did nothing at all.
        """
        if not len(featuretypes_groups):
            # An empty tuple used to mean "merge nothing" and return []. The
            # oracle reads it as "no featuretype filter".
            featuretypes_groups = (None,)

        result: list[Feature] = []
        for group in featuretypes_groups:
            merged_in_group = [
                merged
                for merged in self.merge(
                    self.all_features(featuretype=group, order_by=merge_order),
                    merge_criteria=merge_criteria,
                )
                if merged.children
            ]
            for merged in merged_in_group:
                self._insert(merged)
                result.append(merged)

            if exclude_components:
                self.delete(
                    [c for merged in merged_in_group for c in merged.children],
                    make_backup=False,
                )
            else:
                # One batched call, not one per child: `add_relation` re-derives
                # the closure each time, so the oracle's per-child loop would
                # make this O(children x full rebuild).
                self.add_relations(
                    [(merged, child) for merged in merged_in_group for child in merged.children],
                    level=1,
                    child_func=assign_child,
                )
        return result

    def create_introns(
        self,
        exon_featuretype: str = "exon",
        grandparent_featuretype: str | None = "gene",
        parent_featuretype: str | None = None,
        new_featuretype: str = "intron",
        merge_attributes: bool = True,
        numeric_sort: bool = False,
    ) -> Iterator[Feature]:
        """Introns, computed **per transcript**.

        `grandparent_featuretype="gene"` descends one level first and computes
        the gaps within each transcript separately. Treating the gene as the
        direct anchor -- which this used to do -- pools the exons of every
        isoform into one sorted list, so the "introns" of a multi-isoform gene
        were computed across transcript boundaries and were not introns of
        anything.
        """
        for anchor in self._exon_anchors(grandparent_featuretype, parent_featuretype):
            exons = self.children(anchor, level=1, featuretype=exon_featuretype, order_by="start")
            yield from self.interfeatures(
                exons,
                new_featuretype=new_featuretype,
                merge_attributes=merge_attributes,
                numeric_sort=numeric_sort,
                dialect=self.dialect,
            )

    def _exon_anchors(self, grandparent_featuretype, parent_featuretype) -> Iterator[Feature]:
        """The features whose direct exon children form one transcript.

        Shared by `create_introns` and `create_splice_sites` so the two cannot
        drift apart on the grouping question.
        """
        if bool(grandparent_featuretype) == bool(parent_featuretype):
            raise ValueError(
                "exactly one of `grandparent_featuretype` or `parent_featuretype` "
                "should be provided"
            )
        if grandparent_featuretype:
            for gene in self.features_of_type(grandparent_featuretype):
                yield from self.children(gene, level=1)
        else:
            yield from self.features_of_type(parent_featuretype)

    def create_splice_sites(
        self,
        exon_featuretype: str = "exon",
        grandparent_featuretype: str | None = "gene",
        parent_featuretype: str | None = None,
        merge_attributes: bool = True,
        numeric_sort: bool = False,
    ) -> Iterator[Feature]:
        """The two-base splice sites flanking each intron.

        A splice site is a dinucleotide -- GT at the donor, AG at the acceptor
        -- so these are 2 bp features, not the 1 bp ones this used to emit.
        They are typed by their position in the transcript rather than in the
        genome, so the left site of a minus-strand transcript is its 3' site.

        Emission order is every left site, then every right site, matching the
        oracle; the intron's merged attributes are carried through with the ID
        prefixed by the featuretype so the two sites of one intron differ.
        """
        for side in ("left", "right"):
            for anchor in self._exon_anchors(grandparent_featuretype, parent_featuretype):
                exons = self.children(
                    anchor, level=1, featuretype=exon_featuretype, order_by="start"
                )
                if anchor.strand == "+":
                    featuretype = (
                        "five_prime_cis_splice_site"
                        if side == "left"
                        else "three_prime_cis_splice_site"
                    )
                elif anchor.strand == "-":
                    featuretype = (
                        "three_prime_cis_splice_site"
                        if side == "left"
                        else "five_prime_cis_splice_site"
                    )
                else:
                    # No orientation, so neither end is 5' or 3'.
                    featuretype = "splice_site"

                for site in self.interfeatures(
                    exons,
                    new_featuretype=featuretype,
                    merge_attributes=merge_attributes,
                    numeric_sort=numeric_sort,
                    dialect=self.dialect,
                ):
                    if side == "left":
                        site.end = site.start + 1
                    else:
                        site.start = site.end - 1
                    if site.attributes.get("ID"):
                        site.attributes["ID"] = [f"{featuretype}_{site.attributes['ID'][0]}"]
                    yield site

    def children_bp(
        self,
        feature,
        child_featuretype: str = "exon",
        merge: bool = False,
        merge_criteria=None,
        **kwargs,
    ) -> int:
        if kwargs:
            # Accepting and ignoring these was worse than refusing them:
            # `ignore_strand` was removed upstream precisely because it gave
            # the wrong answer, and silently dropping it returns a number that
            # looks right.
            if "ignore_strand" in kwargs:
                raise ValueError(
                    "'ignore_strand' has been deprecated; please use merge_criteria to "
                    "control how features should be merged. E.g., leave out the mc.strand "
                    "criteria to ignore strand."
                )
            raise TypeError(f"children_bp() got unexpected keyword arguments {list(kwargs)}")

        kids = self.children(feature, featuretype=child_featuretype, order_by="start")
        if merge:
            kids = self.merge(kids, merge_criteria=merge_criteria)
        total = 0
        for k in kids:
            if k.start is not None and k.end is not None:
                total += k.end - k.start + 1
        return total

    def bed12(
        self,
        feature,
        block_featuretype=("exon",),
        thick_featuretype=("CDS",),
        thin_featuretype=None,
        name_field: str = "ID",
        color=None,
    ) -> str:
        if thick_featuretype and thin_featuretype:
            raise ValueError("Can only specify one of `thick_featuretype` or `thin_featuretype`")
        if isinstance(feature, str):
            feature = self[feature]
        blocks = sorted(
            _with_coordinates(self.children(feature, featuretype=list(block_featuretype))),
            key=lambda f: (f.start, f.end),
        )
        if feature.start is None or feature.end is None:
            raise ValueError(
                f"cannot build a BED12 record for {feature.id!r}: "
                "feature has no start/end coordinates"
            )
        chrom_start = feature.start - 1
        chrom_end = feature.end

        if thin_featuretype:
            # The complement of `thick`: the caller names the UNtranslated
            # parts, and the thick span is what lies between them. Accepted and
            # silently ignored before, so `thin_featuretype=["UTR"]` returned a
            # record with the thick span covering the whole feature.
            thin = sorted(
                _with_coordinates(self.children(feature, featuretype=list(thin_featuretype))),
                key=lambda f: (f.start, f.end),
            )
            if thin:
                thick_start, thick_end = thin[0].end, thin[-1].start - 1
            else:
                thick_start, thick_end = feature.start, feature.end
        else:
            thick = sorted(
                _with_coordinates(self.children(feature, featuretype=list(thick_featuretype))),
                key=lambda f: (f.start, f.end),
            )
            if thick:
                thick_start, thick_end = thick[0].start - 1, thick[-1].end
            else:
                # No CDS: the oracle marks the whole feature thick, using its
                # 1-based start. Collapsing both to `chrom_start` -- which this
                # did -- renders an entirely thin feature, the opposite claim.
                thick_start, thick_end = feature.start, feature.end

        try:
            name_value = feature.attributes[name_field][0]
        except (KeyError, IndexError):
            name_value = "."
        score = feature.score if feature.score not in (".", "") else "0"
        # `.` is a legal BED strand and means "unstranded". Rewriting it to `+`
        # asserts an orientation the source did not have.
        strand = feature.strand if feature.strand in ("+", "-") else "."
        rgb = (color or "0,0,0").replace(" ", "").strip()
        # BED12 requires blockCount to equal the number of entries in
        # blockSizes and blockStarts, so a block with missing coordinates has
        # to drop out of all three together, not just the two lists.
        sized = [(b.start, b.end) for b in blocks if b.start is not None and b.end is not None]
        if not sized:
            # A feature with no block children is one block: itself.
            sized = [(feature.start, feature.end)]
        block_count = len(sized)
        # No trailing comma. UCSC tolerates one, but the oracle emits none and
        # a differential comparison sees every line as different.
        block_sizes = ",".join(str(end - start + 1) for start, end in sized)
        block_starts = ",".join(str((start - 1) - chrom_start) for start, _end in sized)
        return "\t".join(
            str(x)
            for x in (
                feature.seqid,
                chrom_start,
                chrom_end,
                name_value,
                score,
                strand,
                thick_start,
                thick_end,
                rgb,
                block_count,
                block_sizes,
                block_starts,
            )
        )

    def iter_by_parent_childs(
        self,
        featuretype: str = "gene",
        level: int | None = None,
        order_by=None,
        reverse: bool = False,
        completely_within: bool = False,
    ) -> Iterator[list[Feature]]:
        for parent in self.features_of_type(featuretype, order_by=order_by, reverse=reverse):
            kids = list(
                self.children(
                    parent,
                    level=level,
                    order_by=order_by,
                    reverse=reverse,
                    completely_within=completely_within,
                )
            )
            yield [parent, *kids]

    # ------------------------------------------------------------------
    # Escape hatch + maintenance
    # ------------------------------------------------------------------

    def execute(self, query: str):
        """Execute arbitrary SQL. Returns DuckDB's relation cursor.
        SQLite-style queries against ``features_compat`` and ``relations_compat``
        views are supported; see ``compat_views.sql``."""
        return self.conn.execute(query.rstrip(";"))

    def analyze(self) -> None:
        self.conn.execute("ANALYZE")
        self._analyzed_flag = True

    def set_pragmas(self, pragmas: dict) -> None:
        """Apply DuckDB settings, ignoring pragmas that only SQLite has.

        Legacy callers pass `constants.default_pragmas` -- `synchronous`,
        `journal_mode`, `main.page_size`, `main.cache_size` -- none of which
        DuckDB has. Those are skipped, which is what makes a gffutils script
        run here unchanged.

        Both the name and the value used to be interpolated straight into the
        statement, and the whole loop body sat inside `except duckdb.Error:
        continue`. DuckDB executes trailing statements, so

            db.set_pragmas({"threads": "1; DROP TABLE attributes"})

        dropped the table -- and because the exception was swallowed, a
        payload that *failed* was silent too. Names are now matched against
        DuckDB's own settings catalog and values rendered as SQL literals, so
        nothing a caller supplies reaches the parser as syntax.

        Matching the live catalog rather than a hardcoded list means the check
        tracks whatever DuckDB build is installed, instead of going stale and
        rejecting settings a newer version added.
        """
        known = {
            row[0] for row in self.conn.execute("SELECT name FROM duckdb_settings()").fetchall()
        }
        for k, v in pragmas.items():
            name = str(k)
            if name not in known:
                # Not a DuckDB setting. Previously this was indistinguishable
                # from "DuckDB rejected the value"; now it is a decision.
                _log.debug("set_pragmas: skipping %r, not a DuckDB setting", name)
                continue
            # `name` is echoed from the catalog, so it cannot carry syntax.
            self.conn.execute(f"SET {name} = {_sql_literal(v)}")

    # ------------------------------------------------------------------
    # Internal: row → Feature streaming
    # ------------------------------------------------------------------

    #: Rows pulled from DuckDB per round trip, and the boundary at which
    #: segments are prefetched.
    _CHUNK = 10_000

    def _segment_cursor(self):
        """A cursor of our own for segment prefetches.

        A DuckDB connection holds ONE result set: running a query on it
        discards whatever the previous `execute` was still streaming. So
        prefetching segments on `self.conn` in the middle of `_yield_features`
        silently truncated iteration at the first chunk boundary -- the
        FlyBase 50k corpus came back as 10 069 lines instead of 49 981, and
        nothing raised. `cursor()` gives an independent connection to the same
        database, so the two queries no longer interfere.
        """
        if self._seg_cursor is None:
            self._seg_cursor = self.conn.cursor()
        return self._seg_cursor

    def _prefetch_segments(self, ids: list[str]) -> dict[str, list]:
        """Segment rows for a whole chunk of features, keyed by feature id.

        One query per chunk rather than one per feature: `MultipartFeature`
        would otherwise lazily load its own segments and iterating a corpus
        with 345 discontinuous features would be 345 extra round trips.

        Only multipart features have rows in `segments`, so the result is
        empty for every ordinary feature and the returned map doubles as the
        test for which rows need upgrading.
        """
        rows = (
            self._segment_cursor()
            .execute(
                'SELECT feature_id, seg_idx, start, "end", score, frame, '
                "attributes_blob, extra_blob, file_order "
                "FROM segments WHERE feature_id IN (SELECT UNNEST(?::VARCHAR[])) "
                "ORDER BY feature_id, seg_idx",
                [ids],
            )
            .fetchall()
        )
        out: dict[str, list] = {}
        for row in rows:
            out.setdefault(row[0], []).append(row)
        return out

    def _yield_features(self, sql: str, params: list) -> Iterator[Feature]:
        cur = self.conn.execute(sql, params)
        # `_n_multipart` is read once at open. At zero -- every GTF corpus, all
        # of GENCODE, every database migrated from v1 -- this whole path costs
        # one attribute test per chunk of 10 000 rows.
        multipart = bool(self._n_multipart)
        while True:
            rows = cur.fetchmany(self._CHUNK)
            if not rows:
                return
            segments = self._prefetch_segments([r[0] for r in rows]) if multipart else {}
            for row in rows:
                yield self._build_feature(row, segments.get(row[0]))
