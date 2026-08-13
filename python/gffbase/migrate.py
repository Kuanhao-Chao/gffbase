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
"""Upgrading a schema v1 database in place.

Two separate operations, deliberately not combined:

:func:`migrate_v1_to_v2`
    Structural only -- `ADD COLUMN`, `CREATE TABLE`, `CREATE VIEW`. It must
    never change what any query returns, so it does not attempt to recognise
    v1's `x_1` / `x_2` rows as segments of one discontinuous feature. An
    in-place upgrade that silently merged 18 features into 17 would be a data
    change wearing a migration's clothes.

:func:`coalesce_multipart`
    The opt-in second step that does exactly that, reusing the ingest resolve
    pass. It changes results, which is why the caller has to ask for it.

What the migration deliberately does NOT do is backfill `raw_id`. NULL there
means "this database predates the column, and the id column 9 originally
carried is unknown" -- which is the truth. Setting `raw_id = id` would be a
lie precisely for the rows that matter: the ones `create_unique` renamed.
`meta.raw_id_valid` records it, and the resolve pass skips NULL `raw_id`, so a
migrated database is simply inert for fusion until `coalesce_multipart` runs.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field

import duckdb

from gffbase._dbutil import scalar_or
from gffbase.exceptions import SchemaVersionError
from gffbase.schema import SCHEMA_VERSION, SEGMENTS_ALL_VIEW

_log = logging.getLogger("gffbase.migrate")

#: Columns v2 adds to `features`, with the type and default each needs so an
#: existing row remains valid without being rewritten.
_FEATURE_COLUMNS = (
    ("raw_id", "VARCHAR", None),
    ("occ", "INTEGER", "0"),
    ("n_segments", "INTEGER", "1"),
    ("id_origin", "VARCHAR", "'attribute'"),
)

_ATTRIBUTE_COLUMNS = (
    ("seg_idx", "INTEGER", "0"),
    ("ord", "INTEGER", "0"),
)

#: Tables v2 adds. Written out here rather than sliced out of `schema.DDL`
#: because a migration has to keep working against the shape it was written
#: for, even if the create-time DDL later gains a column.
_NEW_TABLES = """
CREATE TABLE IF NOT EXISTS segments (
    feature_id      VARCHAR NOT NULL,
    seg_idx         INTEGER NOT NULL,
    start           BIGINT,
    "end"           BIGINT,
    score           VARCHAR,
    frame           VARCHAR,
    attributes_blob BLOB NOT NULL,
    extra_blob      BLOB,
    file_order      BIGINT NOT NULL,
    attrs_same_as_seg0 BOOLEAN NOT NULL DEFAULT TRUE,
    seqid_y         BIGINT
);

CREATE TABLE IF NOT EXISTS id_conflicts (
    raw_id          VARCHAR NOT NULL,
    resolved_id     VARCHAR NOT NULL,
    kind            VARCHAR NOT NULL,
    file_order      BIGINT,
    detail          VARCHAR
);
"""

#: `features_compat` is redefined onto `segments_all`, matching a fresh v2
#: database. With nothing multipart the two definitions are identical row for
#: row, so this changes no result.
_COMPAT_VIEW = """
CREATE OR REPLACE VIEW features_compat AS
    SELECT feature_id AS id, seqid, source, featuretype, start, "end",
           score, strand, frame,
           CAST(attributes_blob AS VARCHAR) AS attributes,
           CAST(extra_blob      AS VARCHAR) AS extra,
           0 AS bin
    FROM segments_all;
"""


@dataclass
class MigrationResult:
    """What a migration did, for logging and for tests to assert on."""

    #: Version found before the migration ran.
    from_version: str
    #: Version now recorded.
    to_version: str
    #: False when the database was already current -- the idempotent re-run.
    changed: bool = False
    #: Structures actually created, in the order they were applied.
    applied: list[str] = field(default_factory=list)

    def __bool__(self) -> bool:
        return self.changed


def _read_version(con: duckdb.DuckDBPyConnection) -> str | None:
    try:
        return scalar_or(con, "SELECT value FROM meta WHERE key = 'schema_version'", None)
    except duckdb.Error:
        return None


def _existing_columns(con: duckdb.DuckDBPyConnection, table: str) -> set[str]:
    rows = con.execute(
        "SELECT column_name FROM duckdb_columns() WHERE table_name = ?", [table]
    ).fetchall()
    return {r[0] for r in rows}


def _add_columns(con, table: str, columns, applied: list[str]) -> None:
    """`ADD COLUMN` for anything missing.

    Checked against the catalogue rather than relying on `IF NOT EXISTS`,
    because that clause is what makes the operation idempotent and it is worth
    being able to report exactly which columns were added.
    """
    present = _existing_columns(con, table)
    for name, sql_type, default in columns:
        if name in present:
            continue
        clause = f" DEFAULT {default}" if default is not None else ""
        con.execute(f"ALTER TABLE {table} ADD COLUMN {name} {sql_type}{clause}")
        applied.append(f"{table}.{name}")


def migrate_v1_to_v2(target, *, con: duckdb.DuckDBPyConnection | None = None) -> MigrationResult:
    """Upgrade a schema v1 database to v2, in place.

    `target` may be a path or an open connection. The whole upgrade runs in one
    transaction, so a database is never left half-migrated; and it is
    idempotent, so running it against a database that is already v2 is a no-op
    that reports ``changed=False`` rather than an error.

    Every statement is additive. No feature row's data is altered, and a
    caller's queries return exactly what they returned before -- which is the
    property that makes this safe to run automatically at open.
    """
    owned = False
    if con is None:
        if isinstance(target, duckdb.DuckDBPyConnection):
            con = target
        else:
            con = duckdb.connect(str(target))
            owned = True

    try:
        found = _read_version(con)
        current = found if found is not None else "1"

        if current == SCHEMA_VERSION:
            return MigrationResult(from_version=current, to_version=current, changed=False)
        if current != "1":
            raise SchemaVersionError(
                f"cannot migrate schema version {current!r}: this gffbase upgrades "
                f"v1 to v{SCHEMA_VERSION} only"
            )

        applied: list[str] = []
        # One transaction: a database is never left half-upgraded, so a failure
        # mid-way leaves a still-valid v1 database rather than something no
        # version of gffbase can read.
        con.execute("BEGIN TRANSACTION")
        try:
            _add_columns(con, "features", _FEATURE_COLUMNS, applied)
            _add_columns(con, "attributes", _ATTRIBUTE_COLUMNS, applied)
            con.execute(_NEW_TABLES)
            applied.extend(["segments", "id_conflicts"])
            con.execute("CREATE INDEX IF NOT EXISTS segments_fid ON segments(feature_id, seg_idx)")
            con.execute(SEGMENTS_ALL_VIEW)
            con.execute(_COMPAT_VIEW)
            applied.extend(["segments_all", "features_compat"])
            con.executemany(
                "INSERT OR REPLACE INTO meta(key, value) VALUES (?, ?)",
                [
                    ("schema_version", SCHEMA_VERSION),
                    ("n_multipart", "0"),
                    # `raw_id` is left NULL rather than backfilled to `id`; see
                    # the module docstring. This records that, so
                    # `coalesce_multipart` and the validator can tell a
                    # migrated database from a natively-built one.
                    ("raw_id_valid", "false"),
                    # v1 relied on physical insertion order to reproduce
                    # attribute key order, which no SQL engine guarantees, so
                    # `ord` cannot be recovered. The raw blob is intact, and
                    # that is where key order should be read from.
                    ("attributes_ord_valid", "false"),
                ],
            )
            con.execute("COMMIT")
        except Exception:
            con.execute("ROLLBACK")
            raise

        _log.info("migrated %s from schema v1 to v%s", target, SCHEMA_VERSION)
        return MigrationResult(
            from_version="1", to_version=SCHEMA_VERSION, changed=True, applied=applied
        )
    finally:
        if owned:
            con.close()


# ---------------------------------------------------------------------------
# The opt-in second step
# ---------------------------------------------------------------------------

#: `IdSpecResolver._autoincrement` renders `f"{base}_{n}"`, so this is its
#: inverse -- the only trace a v1 database keeps of a `create_unique` rename.
_AUTOINCREMENT_SUFFIX = re.compile(r"^(?P<base>.+)_(?P<n>[1-9]\d*)$")


def _reconstruct_raw_ids(con) -> int:
    """Recover `raw_id` for rows a v1 `create_unique` renamed. Returns the count.

    Two sources, in order of trust:

    1. `duplicates`, which gffutils and gffbase both populate on the
       merge-to-create_unique fallback. It records the rename directly.
    2. The `_N` suffix, which is all `create_unique` leaves behind otherwise.
       Applied only when a feature with the bare base id also exists, since
       that is the signature of the rename -- `_autoincrement` never issues
       `x_1` unless `x` was already taken.

    Rule 2 can still misfire on a file that genuinely names a feature `x_1`
    alongside one named `x`. That is exactly why coalescing is an explicit,
    separately-invoked call and not part of the upgrade.
    """
    # Start from the identity, then override only the rows a rename touched.
    con.execute("UPDATE features SET raw_id = id WHERE raw_id IS NULL")

    ids = {r[0] for r in con.execute("SELECT id FROM features").fetchall()}
    renames: dict[str, str] = {}

    # Source 2 first, so the authoritative source can overwrite it.
    for fid in ids:
        m = _AUTOINCREMENT_SUFFIX.match(fid)
        if m and m.group("base") in ids:
            renames[fid] = m.group("base")

    try:
        for original_id, new_id in con.execute(
            "SELECT original_id, new_id FROM duplicates"
        ).fetchall():
            if new_id in ids:
                renames[new_id] = original_id
    except duckdb.Error:
        pass  # a database old enough to lack the table has nothing to recover

    if renames:
        con.executemany(
            "UPDATE features SET raw_id = ? WHERE id = ?",
            [(base, fid) for fid, base in renames.items()],
        )
    recovered = len(renames)

    # `occ` follows from `raw_id`: file order within the group.
    con.execute(
        """
        UPDATE features SET occ = r.rn
        FROM (
            SELECT id, ROW_NUMBER() OVER (PARTITION BY raw_id ORDER BY file_order, id) - 1 AS rn
            FROM features
        ) r
        WHERE features.id = r.id
        """
    )
    return recovered


def coalesce_multipart(db, *, on_multipart_conflict: str = "error") -> int:
    """Re-fuse rows a v1 `create_unique` split apart. Returns the count fused.

    Deliberately NOT part of :func:`migrate_v1_to_v2`: it changes query
    results, merging what were N features into one, and an in-place upgrade
    must never do that behind a caller's back.

    Reuses the ingest resolve pass, so the GFF3 predicate applies unchanged --
    segments must share seqid, source, featuretype and strand, and a run that
    does not is reported rather than fused.
    """
    from gffbase._options import IngestOptions
    from gffbase.ingest import resolve_multipart

    con = db.conn if hasattr(db, "conn") else db
    _reconstruct_raw_ids(con)

    options = IngestOptions(mode="strict", on_multipart_conflict=on_multipart_conflict)
    n = resolve_multipart(con, options, {}, has_spatial=_has_bbox(con))
    con.execute(
        "INSERT OR REPLACE INTO meta(key, value) SELECT 'n_multipart', "
        "CAST(COUNT(*) AS VARCHAR) FROM features WHERE n_segments > 1"
    )
    con.execute("INSERT OR REPLACE INTO meta(key, value) VALUES ('raw_id_valid', 'true')")
    if hasattr(db, "_n_multipart"):
        db._n_multipart = int(
            scalar_or(con, "SELECT COUNT(*) FROM features WHERE n_segments > 1", 0)
        )
    return n


def _has_bbox(con) -> bool:
    return "bbox" in _existing_columns(con, "features")
