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
"""``export_sqlite`` — write a legacy gffutils-format SQLite database from a
gffbase DuckDB connection.

Documented break: the ``attributes`` column in the exported file is the raw
col-9 bytes (UTF-8 string), not legacy-style JSON. Most downstream code reads
attributes via ``Feature.attributes`` and continues to work; raw-SQL queries
that depend on JSON-decoded attributes need migration.
"""

from __future__ import annotations

import json
import os
import sqlite3

import duckdb

from gffbase._bins import bin_from_coords

_LEGACY_SCHEMA = """
CREATE TABLE features (
    id text,
    seqid text,
    source text,
    featuretype text,
    start int,
    end int,
    score text,
    strand text,
    frame text,
    attributes text,
    extra text,
    bin int,
    primary key (id)
);
CREATE TABLE relations (
    parent text,
    child text,
    level int,
    primary key (parent, child, level)
);
CREATE TABLE meta (
    dialect text,
    version text
);
CREATE TABLE directives (
    directive text
);
CREATE TABLE autoincrements (
    base text,
    n int,
    primary key (base)
);
CREATE TABLE duplicates (
    idspecid text,
    newid text,
    primary key (newid)
);
CREATE INDEX featuretype  ON features (featuretype);
CREATE INDEX seqidstartend ON features (seqid, start, end);
CREATE INDEX relationsparent ON relations (parent);
CREATE INDEX relationschild  ON relations (child);
CREATE INDEX binindex ON features (bin);
"""


def _legacy_ids(con) -> dict[str, str]:
    """Map every physical line to the id gffutils would have given it.

    gffutils cannot represent a discontinuous feature at all, so a database
    holding one has to be flattened before it can be exported. The flattening
    reproduces what `gffutils.create_db(file, merge_strategy="create_unique")`
    would have produced from the same input: segment 0 keeps the logical id and
    segment k gets `<id>_k`.

    Collision-hardened, because a file can legitimately contain both `cds1` and
    `cds1_1`: a generated name that some other feature already claims is
    stepped past rather than written, which would otherwise violate the legacy
    table's primary key.

    Returns `{f"{feature_id}\\x00{seg_idx}": legacy_id}`. Empty when nothing is
    multipart, which is the overwhelmingly common case and means the export
    does no extra work at all.
    """
    rows = con.execute(
        "SELECT feature_id, seg_idx FROM segments WHERE seg_idx > 0 ORDER BY feature_id, seg_idx"
    ).fetchall()
    if not rows:
        return {}

    taken = {r[0] for r in con.execute("SELECT id FROM features").fetchall()}
    mapping: dict[str, str] = {}
    for feature_id, seg_idx in rows:
        n = seg_idx
        candidate = f"{feature_id}_{n}"
        while candidate in taken:
            n += 1
            candidate = f"{feature_id}_{n}"
        taken.add(candidate)
        mapping[f"{feature_id}\x00{seg_idx}"] = candidate
    return mapping


def export_sqlite(con: duckdb.DuckDBPyConnection, path: str, force: bool = False) -> str:
    """Write a legacy SQLite ``.db`` from the given DuckDB connection.

    The result is openable by real gffutils. Because gffutils has no way to
    represent a discontinuous feature, one is flattened back into the N
    features gffutils itself would have made -- see `_legacy_ids`. The grouping
    is not lost: `duplicates` records `(logical_id, legacy_id)` for every
    segment past the first, which is both what that table means and how a
    re-import can rediscover it.

    Returns the absolute path on success.
    """
    if os.path.exists(path):
        if not force:
            raise ValueError(f"{path} already exists; pass force=True to overwrite")
        os.unlink(path)

    sqlite_con = sqlite3.connect(path)
    try:
        sqlite_con.executescript(_LEGACY_SCHEMA)
        legacy = _legacy_ids(con)

        # `segments_all`, not `features`: one row per physical input LINE. A
        # segment-0 row must carry its own coordinates here, not the envelope,
        # or the exported file describes spans the source never contained.
        rows = con.execute(
            """
            SELECT feature_id, seg_idx, seqid, source, featuretype, start, "end",
                   score, strand, frame,
                   CAST(attributes_blob AS VARCHAR) AS attributes,
                   CAST(extra_blob      AS VARCHAR) AS extra
            FROM segments_all
            ORDER BY file_order NULLS LAST, feature_id, seg_idx
            """
        ).fetchall()

        # UCSC bin is computed in Python: DuckDB has no equivalent, and
        # `gffutils.FeatureDB.region(completely_within=True)` filters on it, so
        # getting it wrong makes those queries return nothing at all.
        export_rows = []
        for (
            feature_id,
            seg_idx,
            seqid,
            source,
            featuretype,
            start,
            end,
            score,
            strand,
            frame,
            attributes,
            extra,
        ) in rows:
            export_rows.append(
                (
                    legacy.get(f"{feature_id}\x00{seg_idx}", feature_id),
                    seqid,
                    source,
                    featuretype,
                    start,
                    end,
                    score,
                    strand,
                    frame,
                    attributes or "",
                    extra or "",
                    bin_from_coords(start, end),
                )
            )
        sqlite_con.executemany(
            "INSERT INTO features VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            export_rows,
        )

        # Closure -> relations(parent, child, level=depth), fanned out over
        # both endpoints' legacy ids. A relation naming only the logical id
        # would dangle against a features table that no longer has that row
        # under that name for every segment.
        by_logical: dict[str, list[str]] = {}
        for key, legacy_id in legacy.items():
            by_logical.setdefault(key.split("\x00", 1)[0], []).append(legacy_id)

        rels = []
        for ancestor, descendant, depth in con.execute(
            "SELECT ancestor, descendant, depth FROM closure"
        ).fetchall():
            for parent in [ancestor, *by_logical.get(ancestor, ())]:
                for child in [descendant, *by_logical.get(descendant, ())]:
                    rels.append((parent, child, depth))
        sqlite_con.executemany("INSERT INTO relations VALUES (?,?,?)", rels)

        # `duplicates` is how a re-import rediscovers the grouping, and it is
        # also exactly what the legacy table means: a row that was renamed to
        # avoid a primary-key collision.
        if legacy:
            sqlite_con.executemany(
                "INSERT INTO duplicates VALUES (?, ?)",
                [(key.split("\x00", 1)[0], legacy_id) for key, legacy_id in legacy.items()],
            )

        # Meta — write the dialect (JSON) + version.
        meta = dict(con.execute("SELECT key, value FROM meta").fetchall())
        sqlite_con.execute(
            "INSERT INTO meta VALUES (?, ?)",
            (meta.get("dialect", json.dumps({"fmt": "gff3"})), "gffbase-export"),
        )

        # Directives.
        dirs = con.execute("SELECT directive FROM directives ORDER BY seq").fetchall()
        sqlite_con.executemany("INSERT INTO directives VALUES (?)", dirs)

        # Autoincrements (typically empty in Phase 5).
        try:
            ai = con.execute("SELECT base, n FROM autoincrements").fetchall()
            if ai:
                sqlite_con.executemany("INSERT INTO autoincrements VALUES (?, ?)", ai)
        except duckdb.Error:
            pass

        sqlite_con.commit()
    finally:
        sqlite_con.close()
    return os.path.abspath(path)
