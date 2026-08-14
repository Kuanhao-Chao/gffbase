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
"""Upgrading a schema v1 database.

Everything here runs against `tests/data/v1/schema_v1.duckdb.gz` -- a REAL
database written by the gffbase code that predates v2, not a v2 database
stripped back to a v1 shape. That distinction matters: a reconstruction tests
the migration against my model of v1, and this tests it against v1.

The property the migration has to have is narrow and absolute: **no query may
answer differently afterwards**. It is run automatically when a v1 database is
opened, so a migration that quietly changed a result would change it under
callers who never asked for anything. Re-fusing v1's `x_1` rows into
discontinuous features is a result change, which is why it lives in a separate,
explicitly-invoked `coalesce_multipart`.
"""

from __future__ import annotations

import gzip
import shutil
from pathlib import Path

import duckdb
import pytest
from gffbase import FeatureDB, SchemaVersionError, create_db
from gffbase.migrate import coalesce_multipart, migrate_v1_to_v2

V1_DIR = Path(__file__).parent / "data" / "v1"
V1_SOURCE = V1_DIR / "schema_v1_source.gff3"


@pytest.fixture
def v1_db(tmp_path) -> Path:
    """A writable copy of the real v1 database."""
    path = tmp_path / "v1.duckdb"
    with gzip.open(V1_DIR / "schema_v1.duckdb.gz", "rb") as src, open(path, "wb") as dst:
        shutil.copyfileobj(src, dst)
    return path


def _snapshot(db) -> dict:
    """Everything a caller can observe, for a before/after comparison."""
    ids = sorted(f.id for f in db.all_features())
    return {
        "lines": [str(f) for f in db.all_features()],
        "ids": ids,
        "featuretypes": sorted(db.featuretypes()),
        "counts": {t: db.count_features_of_type(t) for t in sorted(db.featuretypes())},
        "children": {i: sorted(f.id for f in db.children(i)) for i in ids},
        "parents": {i: sorted(f.id for f in db.parents(i)) for i in ids},
        "region": sorted(f.id for f in db.region(("chr1", 1, 1000))),
        "attributes": {i: dict(db[i].attributes) for i in ids},
    }


# ---------------------------------------------------------------------------
# The fixture really is v1
# ---------------------------------------------------------------------------


def test_the_fixture_is_a_genuine_v1_database(v1_db):
    """If this ever stops being true, every test below is testing nothing."""
    con = duckdb.connect(str(v1_db), read_only=True)
    try:
        meta = dict(con.execute("SELECT key, value FROM meta").fetchall())
        assert meta["schema_version"] == "1"
        columns = {
            r[0]
            for r in con.execute(
                "SELECT column_name FROM duckdb_columns() WHERE table_name = 'features'"
            ).fetchall()
        }
        assert not (columns & {"raw_id", "occ", "n_segments", "id_origin"})
        tables = {r[0] for r in con.execute("SELECT table_name FROM duckdb_tables()").fetchall()}
        assert "segments" not in tables
    finally:
        con.close()


def test_the_fixture_shows_what_v1_did_with_a_discontinuous_feature(v1_db):
    """Two lines shared `ID=cds1`, so v1 could only make two features. That is
    the state `coalesce_multipart` has to recognise."""
    con = duckdb.connect(str(v1_db), read_only=True)
    try:
        ids = [r[0] for r in con.execute("SELECT id FROM features ORDER BY file_order").fetchall()]
    finally:
        con.close()
    assert ids == ["g1", "t1", "cds1", "cds1_1", "e1"]


# ---------------------------------------------------------------------------
# The migration
# ---------------------------------------------------------------------------


def test_migrating_reports_what_it_did(v1_db):
    result = migrate_v1_to_v2(v1_db)
    assert result.from_version == "1"
    assert result.to_version == "2"
    assert result.changed is True
    assert bool(result) is True
    assert "features.n_segments" in result.applied
    assert "segments" in result.applied
    assert "segments_all" in result.applied


def test_migrating_twice_is_a_no_op(v1_db):
    assert migrate_v1_to_v2(v1_db).changed is True
    again = migrate_v1_to_v2(v1_db)
    assert again.changed is False
    assert again.applied == []
    assert bool(again) is False


def test_the_migrated_database_has_the_v2_shape(v1_db):
    migrate_v1_to_v2(v1_db)
    con = duckdb.connect(str(v1_db), read_only=True)
    try:
        columns = {
            r[0]
            for r in con.execute(
                "SELECT column_name FROM duckdb_columns() WHERE table_name = 'features'"
            ).fetchall()
        }
        assert {"raw_id", "occ", "n_segments", "id_origin"} <= columns
        tables = {r[0] for r in con.execute("SELECT table_name FROM duckdb_tables()").fetchall()}
        assert {"segments", "id_conflicts"} <= tables
        views = {
            r[0]
            for r in con.execute(
                "SELECT view_name FROM duckdb_views() WHERE NOT internal"
            ).fetchall()
        }
        assert "segments_all" in views
        assert dict(con.execute("SELECT key, value FROM meta").fetchall())["schema_version"] == "2"
    finally:
        con.close()


def test_no_query_answers_differently_after_migrating(v1_db, tmp_path):
    """The property that makes automatic migration at open acceptable."""
    reference = tmp_path / "reference.duckdb"
    shutil.copy(v1_db, reference)

    shim = FeatureDB(str(reference), upgrade="never")
    assert shim._v1_shim is True, "the reference must be read WITHOUT migrating"
    before = _snapshot(shim)
    shim.conn.close()

    migrate_v1_to_v2(v1_db)
    after_db = FeatureDB(str(v1_db))
    assert after_db._v1_shim is False
    after = _snapshot(after_db)

    for key in before:
        assert before[key] == after[key], f"{key} changed across the migration"


def test_the_migration_leaves_raw_id_null_rather_than_guessing(v1_db):
    """NULL means "the id column 9 originally carried is unknown", which is the
    truth. `raw_id = id` would be a lie for exactly the rows that matter: the
    ones `create_unique` renamed, where the original id was something else."""
    migrate_v1_to_v2(v1_db)
    con = duckdb.connect(str(v1_db), read_only=True)
    try:
        assert con.execute("SELECT COUNT(*) FROM features WHERE raw_id IS NOT NULL").fetchone() == (
            0,
        )
        meta = dict(con.execute("SELECT key, value FROM meta").fetchall())
        assert meta["raw_id_valid"] == "false"
        assert meta["attributes_ord_valid"] == "false"
    finally:
        con.close()


def test_a_migrated_database_is_inert_for_fusion_until_asked(v1_db):
    """The resolve pass skips NULL `raw_id`, so opening a migrated v1 database
    cannot spontaneously merge anything."""
    migrate_v1_to_v2(v1_db)
    db = FeatureDB(str(v1_db))
    assert db._n_multipart == 0
    assert db.conn.execute("SELECT COUNT(*) FROM segments").fetchone() == (0,)
    assert sorted(f.id for f in db.all_features()) == ["cds1", "cds1_1", "e1", "g1", "t1"]


def test_migrating_a_v2_database_is_a_no_op(tmp_path):
    src = tmp_path / "s.gff3"
    src.write_text("##gff-version 3\nchr1\trs\tgene\t1\t9\t.\t+\t.\tID=g1\n")
    path = tmp_path / "v2.duckdb"
    create_db(str(src), str(path)).conn.close()
    result = migrate_v1_to_v2(path)
    assert result.changed is False
    assert result.from_version == "2"


def test_migrating_a_newer_database_is_refused(tmp_path):
    src = tmp_path / "s.gff3"
    src.write_text("##gff-version 3\nchr1\trs\tgene\t1\t9\t.\t+\t.\tID=g1\n")
    path = tmp_path / "v3.duckdb"
    create_db(str(src), str(path)).conn.close()
    con = duckdb.connect(str(path))
    con.execute("UPDATE meta SET value = '3' WHERE key = 'schema_version'")
    con.close()
    with pytest.raises(SchemaVersionError, match="upgrades"):
        migrate_v1_to_v2(path)


def test_migrating_accepts_an_open_connection(v1_db):
    con = duckdb.connect(str(v1_db))
    try:
        assert migrate_v1_to_v2(con).changed is True
        assert dict(con.execute("SELECT key, value FROM meta").fetchall())["schema_version"] == "2"
    finally:
        con.close()


# ---------------------------------------------------------------------------
# Automatic upgrade at open
# ---------------------------------------------------------------------------


def test_opening_a_v1_database_upgrades_it_by_default(v1_db):
    db = FeatureDB(str(v1_db))
    assert db._v1_shim is False
    assert db._schema_version == 2
    db.conn.close()
    con = duckdb.connect(str(v1_db), read_only=True)
    try:
        assert dict(con.execute("SELECT key, value FROM meta").fetchall())["schema_version"] == "2"
    finally:
        con.close()


def test_upgrade_never_leaves_the_file_alone(v1_db):
    db = FeatureDB(str(v1_db), upgrade="never")
    assert db._v1_shim is True
    db.conn.close()
    con = duckdb.connect(str(v1_db), read_only=True)
    try:
        assert dict(con.execute("SELECT key, value FROM meta").fetchall())["schema_version"] == "1"
    finally:
        con.close()


def test_upgrade_error_refuses_to_open(v1_db):
    with pytest.raises(SchemaVersionError, match="upgrade='error'"):
        FeatureDB(str(v1_db), upgrade="error")


def test_an_invalid_upgrade_mode_is_rejected(v1_db):
    with pytest.raises(ValueError, match="upgrade must be"):
        FeatureDB(str(v1_db), upgrade="sometimes")


def test_a_read_only_connection_degrades_to_shim_mode_rather_than_crashing(v1_db):
    """DDL is impossible there, and refusing to open would be worse than
    reading it: the shim answers every v1 query correctly."""
    con = duckdb.connect(str(v1_db), read_only=True)
    try:
        db = FeatureDB(con)
        assert db._v1_shim is True
        assert db._n_multipart == 0
        assert sorted(f.id for f in db.all_features()) == ["cds1", "cds1_1", "e1", "g1", "t1"]
    finally:
        con.close()


# ---------------------------------------------------------------------------
# coalesce_multipart -- the opt-in result change
# ---------------------------------------------------------------------------


def test_coalescing_refuses_nothing_and_re_fuses_the_split_cds(v1_db):
    migrate_v1_to_v2(v1_db)
    db = FeatureDB(str(v1_db))
    assert coalesce_multipart(db) == 1
    assert sorted(f.id for f in db.all_features()) == ["cds1", "e1", "g1", "t1"]
    assert db.conn.execute(
        'SELECT seg_idx, start, "end", frame FROM segments ORDER BY seg_idx'
    ).fetchall() == [(0, 100, 200, "0"), (1, 800, 900, "2")]


def test_coalescing_reaches_the_same_state_as_a_native_strict_ingest(v1_db):
    """The strongest claim available for the migration path: a v1 database
    upgraded and coalesced is indistinguishable from one built by today's code
    from the same source file."""
    migrate_v1_to_v2(v1_db)
    migrated = FeatureDB(str(v1_db))
    coalesce_multipart(migrated)

    native = create_db(str(V1_SOURCE), ":memory:", mode="strict")

    assert sorted(str(f) for f in migrated.all_features()) == sorted(
        str(f) for f in native.all_features()
    )
    assert (
        migrated.conn.execute(
            'SELECT feature_id, seg_idx, start, "end", frame FROM segments ORDER BY feature_id, seg_idx'
        ).fetchall()
        == native.conn.execute(
            'SELECT feature_id, seg_idx, start, "end", frame FROM segments ORDER BY feature_id, seg_idx'
        ).fetchall()
    )


def test_coalescing_makes_the_multipart_feature_readable_as_one(v1_db):
    migrate_v1_to_v2(v1_db)
    db = FeatureDB(str(v1_db))
    coalesce_multipart(db)
    f = db["cds1"]
    assert f.is_multipart is True
    assert f.n_segments == 2
    assert (f.start, f.end) == (100, 900)
    assert f.covered_length == 202
    assert f.to_lines() == [
        "chr1\trs\tCDS\t100\t200\t.\t+\t0\tID=cds1;Parent=t1",
        "chr1\trs\tCDS\t800\t900\t.\t+\t2\tID=cds1;Parent=t1",
    ]


def test_coalescing_updates_the_recorded_counts(v1_db):
    migrate_v1_to_v2(v1_db)
    db = FeatureDB(str(v1_db))
    coalesce_multipart(db)
    meta = dict(db.conn.execute("SELECT key, value FROM meta").fetchall())
    assert meta["n_multipart"] == "1"
    assert meta["raw_id_valid"] == "true"
    # The live object has to see it too, or its query builders keep emitting
    # the v1 SQL that ignores segments.
    assert db._n_multipart == 1


def test_coalescing_is_idempotent(v1_db):
    migrate_v1_to_v2(v1_db)
    db = FeatureDB(str(v1_db))
    assert coalesce_multipart(db) == 1
    before = sorted(str(f) for f in db.all_features())
    assert coalesce_multipart(db) == 0
    assert sorted(str(f) for f in db.all_features()) == before
    assert db.conn.execute("SELECT COUNT(*) FROM segments").fetchone() == (2,)


def test_coalescing_only_strips_a_suffix_when_the_base_id_exists(tmp_path):
    """`_autoincrement` never issues `x_1` unless `x` was taken, so a suffixed
    id with no bare counterpart is a real id and must be left alone.

    Two of them, deliberately: with one, dropping the guard is harmless because
    a group of one is not a run. With two, dropping it silently fuses two
    unrelated features into one -- which is data loss, not a heuristic miss.
    """
    src = tmp_path / "s.gff3"
    src.write_text(
        "##gff-version 3\n"
        "chr1\trs\tgene\t1\t9\t.\t+\t.\tID=sample_1\n"
        "chr1\trs\tgene\t20\t29\t.\t+\t.\tID=sample_2\n"
    )
    path = tmp_path / "db.duckdb"
    create_db(str(src), str(path)).conn.close()
    db = FeatureDB(str(path))
    assert coalesce_multipart(db) == 0
    assert sorted(f.id for f in db.all_features()) == ["sample_1", "sample_2"]
    assert db.conn.execute("SELECT COUNT(*) FROM segments").fetchone() == (0,)


def test_coalescing_does_strip_the_suffix_when_the_base_id_is_present(tmp_path):
    """The other half of the rule: `x` alongside `x_1` IS the create_unique
    signature, and those must be recognised."""
    src = tmp_path / "s.gff3"
    src.write_text(
        "##gff-version 3\n"
        "chr1\trs\tCDS\t100\t200\t.\t+\t0\tID=x\n"
        "chr1\trs\tCDS\t800\t900\t.\t+\t2\tID=x\n"
    )
    path = tmp_path / "db.duckdb"
    create_db(str(src), str(path), merge_strategy="create_unique").conn.close()
    db = FeatureDB(str(path))
    assert sorted(f.id for f in db.all_features()) == ["x", "x_1"]
    assert coalesce_multipart(db) == 1
    assert [f.id for f in db.all_features()] == ["x"]


def test_coalescing_respects_the_multipart_predicate(tmp_path):
    """It reuses the ingest resolve pass, so a run whose rows disagree on a
    constrained column is reported rather than fused."""
    from gffbase import MultipartConstraintError

    src = tmp_path / "s.gff3"
    src.write_text(
        "##gff-version 3\n"
        "chr1\trs\tCDS\t100\t200\t.\t+\t0\tID=x\n"
        "chr1\trs\tstart_codon\t800\t900\t.\t+\t0\tID=x\n"
    )
    path = tmp_path / "db.duckdb"
    create_db(str(src), str(path), merge_strategy="create_unique").conn.close()
    db = FeatureDB(str(path))
    with pytest.raises(MultipartConstraintError, match="featuretype differs"):
        coalesce_multipart(db)
