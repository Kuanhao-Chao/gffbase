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
"""`meta.schema_version` is read at open, not just written at create.

From the first release the version was recorded and never consulted. A
mismatch therefore surfaced as whatever the first query happened to hit: a
missing-column error from somewhere deep in a query builder, or -- worse -- a
perfectly plausible wrong answer from a query that never touched the new
columns. The gate turns that into one decision made once, at open.

The v1 databases here are built by stripping a real v2 database back to the v1
shape, rather than by hand-writing DDL. A hand-written fixture would drift from
what v1 actually produced and would stop testing anything.
"""

from __future__ import annotations

import logging

import duckdb
import pytest
from gffbase import FeatureDB, SchemaVersionError, create_db

SRC = """##gff-version 3
chr1\trs\tgene\t1\t900\t.\t+\t.\tID=g1
chr1\trs\tmRNA\t1\t900\t.\t+\t.\tID=t1;Parent=g1
chr1\trs\texon\t1\t200\t.\t+\t.\tID=e1;Parent=t1
chr1\trs\texon\t700\t900\t.\t+\t.\tID=e2;Parent=t1
"""

#: Everything v2 added. Removing all of it is what makes a database v1-shaped.
_V2_ADDITIONS = ("raw_id", "occ", "n_segments", "id_origin")


@pytest.fixture
def v2_path(tmp_path):
    src = tmp_path / "src.gff3"
    src.write_text(SRC)
    path = tmp_path / "db.duckdb"
    create_db(str(src), str(path)).conn.close()
    return path


def _connect(path) -> duckdb.DuckDBPyConnection:
    """A raw connection that can modify `features`.

    DuckDB refuses to bind a table carrying an index type it cannot resolve, so
    an R-tree-indexed `features` is unwritable -- and unreadable -- until the
    spatial extension is loaded. `FeatureDB.__init__` does this for its own
    connection; a test poking the database directly has to do it too.
    """
    con = duckdb.connect(str(path))
    try:
        con.execute("LOAD spatial")
    except duckdb.Error:
        pass  # no spatial extension: no R-tree, nothing to unblock
    return con


def _downgrade_to_v1(path) -> None:
    """Strip a v2 database back to the v1 shape, in place.

    Deliberately removes the *structure*, not just the version stamp: a
    database that still had the v2 columns would let the shim pass by accident.
    """
    con = _connect(path)
    has_spatial = bool(
        con.execute(
            "SELECT COUNT(*) FROM duckdb_indexes() WHERE index_name = 'features_rtree'"
        ).fetchone()[0]
    )
    try:
        # Order matters, and DuckDB enforces it: a column cannot be dropped
        # while anything depends on its table. Take every dependent off first,
        # then put the v1 ones back at the end -- including the R-tree, because
        # real v1 databases had one and losing it here would quietly move every
        # shim test onto the B-tree path.
        con.execute("DROP VIEW IF EXISTS features_compat")
        con.execute("DROP VIEW IF EXISTS segments_all")
        con.execute("DROP TABLE IF EXISTS segments")
        con.execute("DROP TABLE IF EXISTS id_conflicts")
        for index in (
            "features_type",
            "features_seqstart",
            "features_rtree",
            "attributes_kv",
            "attributes_fid",
        ):
            con.execute(f"DROP INDEX IF EXISTS {index}")

        for col in _V2_ADDITIONS:
            con.execute(f"ALTER TABLE features DROP COLUMN {col}")
        con.execute("ALTER TABLE attributes DROP COLUMN seg_idx")
        con.execute("ALTER TABLE attributes DROP COLUMN ord")
        con.execute("CREATE INDEX attributes_kv ON attributes(key, value)")
        con.execute("CREATE INDEX attributes_fid ON attributes(feature_id)")
        con.execute("UPDATE meta SET value = '1' WHERE key = 'schema_version'")
        con.execute("DELETE FROM meta WHERE key = 'n_multipart'")

        con.execute(
            """
            CREATE OR REPLACE VIEW features_compat AS
                SELECT id, seqid, source, featuretype, start, "end",
                       score, strand, frame,
                       CAST(attributes_blob AS VARCHAR) AS attributes,
                       CAST(extra_blob      AS VARCHAR) AS extra,
                       0 AS bin
                FROM features
            """
        )
        con.execute("CREATE INDEX features_type ON features(featuretype)")
        con.execute('CREATE INDEX features_seqstart ON features(seqid, start, "end")')
        if has_spatial:
            con.execute("CREATE INDEX features_rtree ON features USING RTREE (bbox)")
    finally:
        con.close()


def _set_version(path, value: str | None) -> None:
    con = _connect(path)
    try:
        if value is None:
            con.execute("DELETE FROM meta WHERE key = 'schema_version'")
        else:
            con.execute("UPDATE meta SET value = ? WHERE key = 'schema_version'", [value])
    finally:
        con.close()


# ---------------------------------------------------------------------------
# Current version
# ---------------------------------------------------------------------------


def test_a_v2_database_opens_normally(v2_path):
    db = FeatureDB(str(v2_path))
    assert db._schema_version == 2
    assert db._v1_shim is False
    assert db._n_multipart == 0
    assert len(list(db.all_features())) == 4


def test_n_multipart_is_recomputed_when_the_meta_row_is_missing(v2_path):
    """Guessing zero here would silently drop segments from `region()`, so an
    absent meta row must fall back to counting rather than to a default."""
    con = _connect(v2_path)
    con.execute("DELETE FROM meta WHERE key = 'n_multipart'")
    con.execute("UPDATE features SET n_segments = 2 WHERE id = 'e1'")
    con.close()

    db = FeatureDB(str(v2_path))
    assert db._n_multipart == 1


def test_a_corrupt_n_multipart_value_falls_back_to_counting(v2_path):
    con = _connect(v2_path)
    con.execute("UPDATE meta SET value = 'not-a-number' WHERE key = 'n_multipart'")
    con.close()

    db = FeatureDB(str(v2_path))
    assert db._n_multipart == 0


# ---------------------------------------------------------------------------
# Older version -- degrade, do not fail
# ---------------------------------------------------------------------------


def test_a_v1_database_opens_in_shim_mode(v2_path):
    _downgrade_to_v1(v2_path)
    db = FeatureDB(str(v2_path))
    assert db._v1_shim is True
    assert db._schema_version == 1
    # Zero is what makes every query builder emit v1 SQL, so the shim needs no
    # special cases anywhere downstream.
    assert db._n_multipart == 0


def test_a_v1_database_still_answers_every_v1_query(v2_path):
    """The point of an additive schema: v1 data is not degraded data. If the
    shim could not answer these, the gate would be a regression rather than a
    safeguard."""
    _downgrade_to_v1(v2_path)
    db = FeatureDB(str(v2_path))

    assert len(list(db.all_features())) == 4
    assert db["g1"].id == "g1"
    assert {f.id for f in db.children("t1")} == {"e1", "e2"}
    assert {f.id for f in db.parents("e1")} == {"t1", "g1"}
    assert {f.id for f in db.region(("chr1", 1, 300))} >= {"e1"}
    assert db.count_features_of_type("exon") == 2
    assert str(db["e1"]).startswith("chr1\trs\texon\t1\t200")


def test_a_database_with_no_version_recorded_is_treated_as_v1(v2_path):
    """Databases predating the version stamp exist and have the v1 shape;
    treating a missing key as current would read columns that are not there."""
    _downgrade_to_v1(v2_path)
    _set_version(v2_path, None)
    db = FeatureDB(str(v2_path))
    assert db._v1_shim is True
    assert len(list(db.all_features())) == 4


def test_opening_a_v1_database_says_so(v2_path, caplog):
    _downgrade_to_v1(v2_path)
    with caplog.at_level(logging.INFO, logger="gffbase.interface"):
        FeatureDB(str(v2_path))
    assert any("schema v1" in r.getMessage() for r in caplog.records)
    assert any("migrate_v1_to_v2" in r.getMessage() for r in caplog.records)


# ---------------------------------------------------------------------------
# Newer or unintelligible version -- refuse
# ---------------------------------------------------------------------------


def test_a_newer_database_is_refused(v2_path):
    """The failure this replaces: a v3 database that moved `attributes_blob`
    elsewhere would open, and every feature would silently report no
    attributes."""
    _set_version(v2_path, "3")
    with pytest.raises(SchemaVersionError, match="newer gffbase"):
        FeatureDB(str(v2_path))


def test_an_unintelligible_version_is_refused(v2_path):
    _set_version(v2_path, "banana")
    with pytest.raises(SchemaVersionError):
        FeatureDB(str(v2_path))


def test_the_error_names_the_database_and_both_versions(v2_path):
    """A version error is something the user has to act on -- upgrade, or
    rebuild -- so it has to say which file and which versions."""
    _set_version(v2_path, "99")
    with pytest.raises(SchemaVersionError) as exc:
        FeatureDB(str(v2_path))
    msg = str(exc.value)
    assert str(v2_path) in msg
    assert "'99'" in msg
    assert "'2'" in msg


def test_schema_version_error_is_a_value_error(v2_path):
    """Callers written against the pre-gate API catch `ValueError`; the new
    exception must not slip past them."""
    assert issubclass(SchemaVersionError, ValueError)
