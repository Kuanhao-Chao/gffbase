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
"""Schema v2 storage structure.

v2 is *additive*: it introduces the tables and columns a discontinuous
(multipart) GFF3 feature needs, without changing what any query returns while
no feature actually has more than one segment. These tests hold that line --
they check the new structure exists and is correctly populated, and separately
that the parts of the database a caller can observe are unchanged.

The population tests matter more than they look. `raw_id`, `occ` and
`id_origin` are written from values the ingest loop already had, at points
where a mistake produces a plausible-looking wrong answer rather than an error:
`raw_id` recording the *post*-rename id would make the multipart resolve pass
group nothing, and it would still pass every test that only reads `id`.
"""

from __future__ import annotations

import duckdb
import pytest
from gffbase import create_db
from gffbase.schema import SCHEMA_VERSION

# One gene, one mRNA, two exons -- nothing multipart, nothing renamed.
PLAIN = """##gff-version 3
chr1\trs\tgene\t1\t900\t.\t+\t.\tID=g1;Name=alpha
chr1\trs\tmRNA\t1\t900\t.\t+\t.\tID=t1;Parent=g1
chr1\trs\texon\t1\t200\t.\t+\t.\tID=e1;Parent=t1
chr1\trs\texon\t700\t900\t.\t+\t.\tID=e2;Parent=t1
"""


@pytest.fixture
def plain_db(tmp_path):
    src = tmp_path / "plain.gff3"
    src.write_text(PLAIN)
    return create_db(str(src), ":memory:")


def _columns(con, table: str) -> dict[str, str]:
    rows = con.execute(
        "SELECT column_name, data_type FROM duckdb_columns() WHERE table_name = ?", [table]
    ).fetchall()
    return {name: dtype for name, dtype in rows}


# ---------------------------------------------------------------------------
# The version stamp itself
# ---------------------------------------------------------------------------


def test_schema_version_is_two():
    """Pinned deliberately. Changing this value is a migration-visible event,
    so it should require editing a test that says so, not slip through with a
    DDL edit."""
    assert SCHEMA_VERSION == "2"


def test_a_new_database_is_stamped_v2(plain_db):
    meta = dict(plain_db.conn.execute("SELECT key, value FROM meta").fetchall())
    assert meta["schema_version"] == "2"


def test_n_multipart_is_recorded_and_zero_for_an_ordinary_file(plain_db):
    """The query builders read this to decide whether to emit the segment
    conjunct at all, so it has to be present even when it is zero -- an absent
    key and a zero are different bugs."""
    meta = dict(plain_db.conn.execute("SELECT key, value FROM meta").fetchall())
    assert meta["n_multipart"] == "0"


# ---------------------------------------------------------------------------
# Structure
# ---------------------------------------------------------------------------


def test_features_gains_the_v2_columns(plain_db):
    cols = _columns(plain_db.conn, "features")
    for name in ("raw_id", "occ", "n_segments", "id_origin"):
        assert name in cols, f"features.{name} missing"
    # v1's columns must all still be there: v2 is additive, and a dropped
    # column would break the projection rather than the schema.
    for name in ("id", "seqid", "source", "featuretype", "start", "end", "score"):
        assert name in cols


def test_segments_table_exists_and_is_empty_without_multipart_features(plain_db):
    cols = _columns(plain_db.conn, "segments")
    assert {"feature_id", "seg_idx", "start", "end", "frame", "attrs_same_as_seg0"} <= set(cols)
    n = plain_db.conn.execute("SELECT COUNT(*) FROM segments").fetchone()[0]
    assert n == 0, "segments is sparse -- it holds rows only for n_segments > 1"


def test_attributes_gains_seg_idx_and_ord(plain_db):
    cols = _columns(plain_db.conn, "attributes")
    assert "seg_idx" in cols
    assert "ord" in cols


def test_id_conflicts_table_exists(plain_db):
    cols = _columns(plain_db.conn, "id_conflicts")
    assert {"raw_id", "resolved_id", "kind"} <= set(cols)


def test_segments_index_is_built(plain_db):
    idx = [
        r[0]
        for r in plain_db.conn.execute(
            "SELECT index_name FROM duckdb_indexes() WHERE table_name = 'segments'"
        ).fetchall()
    ]
    assert "segments_fid" in idx


# ---------------------------------------------------------------------------
# `segments_all` -- exactly one row per physical input line
# ---------------------------------------------------------------------------


def test_segments_all_has_one_row_per_input_line(plain_db):
    n = plain_db.conn.execute("SELECT COUNT(*) FROM segments_all").fetchone()[0]
    assert n == 4


def test_segments_all_stays_disjoint_once_a_feature_is_multipart(plain_db):
    """The `n_segments = 1` filter is what makes the view's two branches
    disjoint: without it, segment 0 of a multipart feature is emitted twice,
    once from `features` and once from `segments`.

    That cannot be observed while `segments` is empty -- deleting the filter is
    a no-op on every database the ingest path can currently build -- so the
    multipart row is constructed directly here. The view's contract is B2's
    deliverable and is testable now; the ingest path that produces such rows is
    Stage C's.
    """
    con = plain_db.conn
    con.execute("UPDATE features SET n_segments = 2 WHERE id = 'e1'")
    con.execute(
        """
        INSERT INTO segments
            (feature_id, seg_idx, start, "end", score, frame,
             attributes_blob, extra_blob, file_order, attrs_same_as_seg0, seqid_y)
        VALUES ('e1', 0, 1, 200, '.', '.', 'ID=e1;Parent=t1'::BLOB, NULL, 3, TRUE, 0),
               ('e1', 1, 400, 500, '.', '.', 'ID=e1;Parent=t1'::BLOB, NULL, 9, TRUE, 0)
        """
    )

    rows = con.execute(
        "SELECT seg_idx, start, \"end\" FROM segments_all WHERE feature_id = 'e1' ORDER BY seg_idx"
    ).fetchall()
    assert rows == [(0, 1, 200), (1, 400, 500)], (
        "segment 0 must come from `segments`, not from both branches"
    )

    # And the whole view still holds exactly one row per physical line:
    # 3 singleton features + 2 segments of e1.
    assert con.execute("SELECT COUNT(*) FROM segments_all").fetchone()[0] == 5


def test_segments_all_equals_features_while_nothing_is_multipart(plain_db):
    """The degenerate case: with nothing multipart the view is `features`."""
    rows = plain_db.conn.execute(
        """
        SELECT feature_id, seg_idx, seqid, start, "end" FROM segments_all
        ORDER BY feature_id
        """
    ).fetchall()
    direct = plain_db.conn.execute(
        'SELECT id, 0, seqid, start, "end" FROM features ORDER BY id'
    ).fetchall()
    assert rows == direct


def test_features_compat_is_row_identical_to_the_v1_definition(plain_db):
    """`features_compat` moved onto `segments_all` in v2. While no feature is
    multipart that must be a no-op, row for row and column for column."""
    got = plain_db.conn.execute("SELECT * FROM features_compat ORDER BY id").fetchall()
    v1_shape = plain_db.conn.execute(
        """
        SELECT id, seqid, source, featuretype, start, "end", score, strand, frame,
               CAST(attributes_blob AS VARCHAR), CAST(extra_blob AS VARCHAR), 0
        FROM features ORDER BY id
        """
    ).fetchall()
    assert got == v1_shape


# ---------------------------------------------------------------------------
# Population of the new columns
# ---------------------------------------------------------------------------


def test_raw_id_and_occ_are_the_identity_of_an_unrenamed_feature(plain_db):
    rows = plain_db.conn.execute("SELECT id, raw_id, occ, n_segments FROM features").fetchall()
    assert rows, "fixture produced no features"
    for fid, raw_id, occ, n_segments in rows:
        assert raw_id == fid
        assert occ == 0
        assert n_segments == 1


def test_id_origin_records_where_the_id_came_from(tmp_path):
    """An `ID=` attribute gives 'attribute'; a row with no id at all is
    autoincremented and must say so. `IdSpecResolver.resolve` already returned
    this and v1 threw it away."""
    src = tmp_path / "origins.gff3"
    src.write_text(
        "##gff-version 3\n"
        "chr1\trs\tgene\t1\t100\t.\t+\t.\tID=named\n"
        "chr1\trs\tgene\t200\t300\t.\t+\t.\tName=no_id_here\n"
    )
    db = create_db(str(src), ":memory:")
    origins = dict(db.conn.execute("SELECT id, id_origin FROM features").fetchall())
    assert origins["named"] == "attribute"
    assert origins["gene_1"] == "autoincrement"


def test_create_unique_records_the_pre_rename_id_in_raw_id(tmp_path):
    """The whole point of `raw_id`: three lines share `ID=dup`, so ids become
    dup / dup_1 / dup_2, but all three must still be groupable back to `dup`.
    Recording the post-rename id here would look completely normal and quietly
    make the multipart resolve pass group nothing."""
    src = tmp_path / "dups.gff3"
    src.write_text(
        "##gff-version 3\n"
        "chr1\trs\tCDS\t1\t100\t.\t+\t0\tID=dup\n"
        "chr1\trs\tCDS\t200\t300\t.\t+\t1\tID=dup\n"
        "chr1\trs\tCDS\t400\t500\t.\t+\t2\tID=dup\n"
    )
    db = create_db(str(src), ":memory:", merge_strategy="create_unique")
    rows = db.conn.execute("SELECT id, raw_id, occ FROM features ORDER BY occ").fetchall()
    assert rows == [("dup", "dup", 0), ("dup_1", "dup", 1), ("dup_2", "dup", 2)]


def test_a_literal_collision_with_a_generated_name_is_still_detected(tmp_path):
    """Occurrence counting is keyed on the raw id, but a rename also has to
    reserve the name it produced. `ID=dup` twice yields `dup_1`; a later
    literal `ID=dup_1` must be renamed rather than colliding on the primary
    key."""
    src = tmp_path / "collide.gff3"
    src.write_text(
        "##gff-version 3\n"
        "chr1\trs\tgene\t1\t100\t.\t+\t.\tID=dup\n"
        "chr1\trs\tgene\t200\t300\t.\t+\t.\tID=dup\n"
        "chr1\trs\tgene\t400\t500\t.\t+\t.\tID=dup_1\n"
    )
    db = create_db(str(src), ":memory:", merge_strategy="create_unique")
    ids = [r[0] for r in db.conn.execute("SELECT id FROM features ORDER BY file_order").fetchall()]
    assert len(ids) == len(set(ids)) == 3
    assert ids[:2] == ["dup", "dup_1"]


def test_synthesized_gtf_features_carry_a_raw_id(tmp_path):
    """Synthesized rows are inserted by SQL that does not go through the Arrow
    builder. A NULL `raw_id` there would make every synthetic feature group
    together under the resolve pass."""
    src = tmp_path / "synth.gtf"
    src.write_text(
        'chr1\trs\texon\t100\t200\t.\t+\t.\tgene_id "G1"; transcript_id "T1";\n'
        'chr1\trs\texon\t300\t400\t.\t+\t.\tgene_id "G1"; transcript_id "T1";\n'
    )
    db = create_db(str(src), ":memory:")
    rows = db.conn.execute(
        "SELECT id, raw_id FROM features WHERE is_synthetic ORDER BY id"
    ).fetchall()
    assert rows, "expected synthesized transcript and gene rows"
    for fid, raw_id in rows:
        assert raw_id == fid


# ---------------------------------------------------------------------------
# The insert path is name-bound, not position-bound
# ---------------------------------------------------------------------------


def test_staging_insert_names_its_columns_in_arrow_order():
    """`flush_into` used `SELECT *`. Adding `raw_id`/`occ`/`id_origin` in a
    different order than the table declares them would have written an id
    string into `seqid_y` -- DuckDB would cast what it could and fail somewhere
    unrelated."""
    from gffbase.ingest import _ArrowBatchBuilder as B

    cols = B._staging_columns(B.FEATURES_SCHEMA)
    assert cols.split(", ") == ['"end"' if n == "end" else n for n in B.FEATURES_SCHEMA.names]
    assert '"end"' in cols


def test_v2_columns_survive_a_file_round_trip(tmp_path):
    """An on-disk database must reopen with the new columns intact -- the DDL
    runs at create time only."""
    src = tmp_path / "rt.gff3"
    src.write_text(PLAIN)
    path = tmp_path / "rt.duckdb"
    create_db(str(src), str(path)).conn.close()

    con = duckdb.connect(str(path), read_only=True)
    try:
        assert _columns(con, "features")["n_segments"]
        assert con.execute("SELECT COUNT(*) FROM segments_all").fetchone()[0] == 4
        meta = dict(con.execute("SELECT key, value FROM meta").fetchall())
        assert meta["schema_version"] == "2"
    finally:
        con.close()
