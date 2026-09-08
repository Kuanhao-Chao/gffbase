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
"""The legacy SQLite export, opened by real gffutils.

This is the strongest parity claim available anywhere in the suite: not "our
implementation agrees with our model of gffutils", but *gffutils itself reads
the file gffbase wrote and answers correctly*. Everything else is a proxy for
that.

Two things make the export non-trivial. gffutils has no way to represent a
discontinuous feature, so one has to be flattened back into the N features
gffutils itself would have made -- and the grouping recorded in `duplicates`
so a re-import can find it again. And `region(completely_within=True)` filters
on the UCSC `bin` column, so a bin computed with the wrong offsets makes those
queries return nothing at all, with no error anywhere.
"""

from __future__ import annotations

import sqlite3
import warnings
from pathlib import Path

import pytest
from gffbase import create_db, export_sqlite

pytestmark = pytest.mark.parity


@pytest.fixture(scope="module", autouse=True)
def _need_oracle():
    """Skip the whole module when the oracle is not installed.

    `pytestmark = pytest.mark.parity` labels these tests; it does not deselect
    them from a plain `pytest` run. CI's `test` job installs `.[test,all]`,
    and `gffutils` lives in the `bench` extra -- so without this guard every
    test below raised `ModuleNotFoundError` in all fourteen matrix cells. It
    passed locally only because a gffutils checkout happens to be importable
    here, which is exactly the kind of difference a CI run exists to find.
    """
    from tests.parity import differential as D

    D.requires_gffutils()


UPSTREAM = Path(__file__).parent.parent / "data" / "upstream"

SPLIT_CDS = """##gff-version 3
chr1\trs\tgene\t100\t900\t.\t+\t.\tID=g1
chr1\trs\tmRNA\t100\t900\t.\t+\t.\tID=t1;Parent=g1
chr1\trs\tCDS\t100\t200\t.\t+\t0\tID=cds1;Parent=t1
chr1\trs\tCDS\t800\t900\t.\t+\t2\tID=cds1;Parent=t1
"""


@pytest.fixture
def exported(tmp_path):
    """A gffbase database with a discontinuous feature, and its export."""
    src = tmp_path / "in.gff3"
    src.write_text(SPLIT_CDS)
    db = create_db(str(src), ":memory:", mode="strict")
    out = tmp_path / "legacy.db"
    export_sqlite(db.conn, str(out))
    return db, out


def _oracle(path):
    import gffutils

    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        return gffutils.FeatureDB(str(path))


# ---------------------------------------------------------------------------
# The claim
# ---------------------------------------------------------------------------


def test_real_gffutils_opens_a_gffbase_export(exported):
    _db, out = exported
    legacy = _oracle(out)
    assert legacy.dialect["fmt"] == "gff3"
    assert sorted(legacy.featuretypes()) == ["CDS", "gene", "mRNA"]


def test_export_is_analyzed_before_a_legacy_reader_opens_it(exported):
    """The exported database must not make gffutils warn on first open."""
    _db, out = exported
    con = sqlite3.connect(out)
    try:
        assert con.execute(
            "SELECT COUNT(*) FROM sqlite_master WHERE type='table' AND name='sqlite_stat1'"
        ).fetchone() == (1,)
    finally:
        con.close()

    import gffutils

    with warnings.catch_warnings():
        warnings.simplefilter("error", UserWarning)
        legacy = gffutils.FeatureDB(str(out))
    legacy.conn.close()


def test_real_gffutils_answers_every_query_shape(exported):
    _db, out = exported
    legacy = _oracle(out)
    assert sorted(f.id for f in legacy.all_features()) == ["cds1", "cds1_1", "g1", "t1"]
    assert sorted(f.id for f in legacy.children("t1")) == ["cds1", "cds1_1"]
    assert sorted(f.id for f in legacy.parents("cds1_1")) == ["g1", "t1"]
    assert legacy.count_features_of_type("CDS") == 2
    assert str(legacy["cds1"]) == "chr1\trs\tCDS\t100\t200\t.\t+\t0\tID=cds1;Parent=t1"


def test_real_gffutils_region_queries_work(exported):
    """`region(completely_within=True)` filters on the UCSC `bin` column. With
    the offsets gffbase used to compute, every such query returned nothing --
    silently, because a bin mismatch is just an empty result."""
    _db, out = exported
    legacy = _oracle(out)
    assert "cds1_1" in {f.id for f in legacy.region(("chr1", 850, 860))}
    assert "cds1" not in {f.id for f in legacy.region(("chr1", 850, 860))}
    within = {f.id for f in legacy.region(("chr1", 1, 1000), completely_within=True)}
    assert within == {"cds1", "cds1_1", "g1", "t1"}


@pytest.mark.parametrize(
    "name",
    [
        "synthetic.gff3",
        "random-chr.gff",
        "c_elegans_WS199_ann_gff.txt",
        "gff_example1.gff3",
        "ncbi_gff3.txt",
        "intro_docs_example.gff",
        "hybrid1.gff3",
    ],
)
def test_gffutils_reads_the_export_exactly_as_it_reads_its_own_database(name, tmp_path):
    """The equivalence claim: for the same source file, a gffbase export and a
    gffutils-built database are indistinguishable through gffutils' own API.

    `FBgn0031208.gff` is deliberately absent. It differs, in gffbase's favour:
    that file writes `; Parent=`, gffutils keeps `' Parent'` with the leading
    space as a literal key and so loses the edge entirely, while gffbase strips
    it and finds the parents. Recorded as an intentional deviation and covered
    by `test_differential.py`.
    """
    import gffutils

    src = UPSTREAM / name
    ours = create_db(str(src), ":memory:", merge_strategy="create_unique")
    out = tmp_path / "e.db"
    export_sqlite(ours.conn, str(out))

    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        theirs = gffutils.create_db(
            str(src), str(tmp_path / "o.db"), merge_strategy="create_unique", force=True
        )
    mine = _oracle(out)

    def snapshot(db):
        ids = sorted(f.id for f in db.all_features())
        return (
            ids,
            sorted(str(f) for f in db.all_features()),
            sorted(db.featuretypes()),
            {i: sorted(f.id for f in db.children(i)) for i in ids},
            {i: sorted(f.id for f in db.parents(i)) for i in ids},
        )

    assert snapshot(mine) == snapshot(theirs)


# ---------------------------------------------------------------------------
# The flattening
# ---------------------------------------------------------------------------


def test_one_legacy_row_per_input_line(exported):
    db, out = exported
    con = sqlite3.connect(out)
    try:
        assert con.execute("SELECT COUNT(*) FROM features").fetchone()[0] == 4
    finally:
        con.close()
    assert db.conn.execute("SELECT COUNT(*) FROM features").fetchone()[0] == 3


def test_segment_zero_carries_its_own_coordinates_not_the_envelope(exported):
    """The feature row holds 100..900 after fusing. Exporting that would
    describe a span the source file never contained."""
    _db, out = exported
    con = sqlite3.connect(out)
    try:
        assert con.execute("SELECT start, end FROM features WHERE id = 'cds1'").fetchone() == (
            100,
            200,
        )
        assert con.execute("SELECT start, end FROM features WHERE id = 'cds1_1'").fetchone() == (
            800,
            900,
        )
    finally:
        con.close()


def test_each_segment_keeps_its_own_phase(exported):
    _db, out = exported
    con = sqlite3.connect(out)
    try:
        assert con.execute(
            "SELECT id, frame FROM features WHERE featuretype='CDS' ORDER BY id"
        ).fetchall() == [
            ("cds1", "0"),
            ("cds1_1", "2"),
        ]
    finally:
        con.close()


def test_relations_fan_out_over_both_endpoints(exported):
    """A relation naming only the logical id would dangle: the exported
    features table no longer has that row under that name for every segment."""
    _db, out = exported
    con = sqlite3.connect(out)
    try:
        rels = sorted(con.execute("SELECT parent, child, level FROM relations").fetchall())
    finally:
        con.close()
    assert rels == [
        ("g1", "cds1", 2),
        ("g1", "cds1_1", 2),
        ("g1", "t1", 1),
        ("t1", "cds1", 1),
        ("t1", "cds1_1", 1),
    ]


def test_duplicates_records_the_grouping(exported):
    """Both what the legacy table means -- a row renamed to dodge a primary-key
    collision -- and how a re-import rediscovers which lines belonged
    together."""
    _db, out = exported
    con = sqlite3.connect(out)
    try:
        assert con.execute("SELECT idspecid, newid FROM duplicates").fetchall() == [
            ("cds1", "cds1_1")
        ]
    finally:
        con.close()


def test_a_generated_name_never_collides_with_a_real_one(tmp_path):
    """A file can legitimately contain both `cds1` and `cds1_1`, and the legacy
    features table has `id` as its primary key."""
    src = tmp_path / "collide.gff3"
    src.write_text(
        "##gff-version 3\n"
        "chr1\trs\tCDS\t100\t200\t.\t+\t0\tID=cds1\n"
        "chr1\trs\tCDS\t800\t900\t.\t+\t2\tID=cds1\n"
        "chr1\trs\tgene\t5000\t5100\t.\t+\t.\tID=cds1_1\n"
    )
    db = create_db(str(src), ":memory:", mode="strict")
    out = tmp_path / "e.db"
    export_sqlite(db.conn, str(out))

    con = sqlite3.connect(out)
    try:
        ids = [r[0] for r in con.execute("SELECT id FROM features").fetchall()]
    finally:
        con.close()
    assert len(ids) == len(set(ids)) == 3
    assert "cds1_1" in ids  # the real one keeps its name
    assert "cds1_2" in ids  # the generated one steps past it


def test_an_ordinary_database_exports_unchanged(tmp_path):
    """Nothing multipart means nothing to flatten, and the export should not
    invent a `duplicates` row or rename anything."""
    src = tmp_path / "plain.gff3"
    src.write_text(
        "##gff-version 3\n"
        "chr1\trs\tgene\t1\t99\t.\t+\t.\tID=g1\n"
        "chr1\trs\texon\t1\t50\t.\t+\t.\tID=e1;Parent=g1\n"
    )
    db = create_db(str(src), ":memory:")
    out = tmp_path / "e.db"
    export_sqlite(db.conn, str(out))
    con = sqlite3.connect(out)
    try:
        assert sorted(r[0] for r in con.execute("SELECT id FROM features")) == ["e1", "g1"]
        assert con.execute("SELECT COUNT(*) FROM duplicates").fetchone() == (0,)
    finally:
        con.close()


# ---------------------------------------------------------------------------
# UCSC bins
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "start,end",
    [
        (1, 100),
        (1, 900),
        (100, 200),
        (800, 900),
        (1, 1),
        (131072, 131073),
        (1, 300000),
        (12345, 67890),
        (1, 2**20),
        (2**28, 2**28 + 5),
        (1, 2**29 - 1),
        (2**29, 2**29 + 10),
        (-5, 10),
    ],
)
def test_bins_match_the_oracle_exactly(start, end):
    """Not approximately: `region(completely_within=True)` compares the stored
    bin for equality against the set of bins overlapping the query, so being
    one level off means matching nothing."""
    from gffbase._bins import bin_from_coords
    from gffutils.bins import bins as oracle_bins

    assert bin_from_coords(start, end) == oracle_bins(start, end, fmt="gff", one=True)


def test_a_null_coordinate_has_no_bin():
    from gffbase._bins import bin_from_coords

    assert bin_from_coords(None, 10) is None
    assert bin_from_coords(10, None) is None


def test_exported_bins_match_what_gffutils_computed(tmp_path):
    """End to end: the same source through both paths must agree row for row."""
    import gffutils

    src = UPSTREAM / "c_elegans_WS199_ann_gff.txt"
    ours = create_db(str(src), ":memory:", merge_strategy="create_unique")
    out = tmp_path / "e.db"
    export_sqlite(ours.conn, str(out))
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        gffutils.create_db(
            str(src), str(tmp_path / "o.db"), merge_strategy="create_unique", force=True
        )

    mine = sqlite3.connect(out)
    theirs = sqlite3.connect(tmp_path / "o.db")
    try:
        a = mine.execute("SELECT id, bin FROM features ORDER BY id").fetchall()
        b = theirs.execute("SELECT id, bin FROM features ORDER BY id").fetchall()
    finally:
        mine.close()
        theirs.close()
    assert a == b
