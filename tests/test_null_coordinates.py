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
"""A `.` in GFF column 4 or 5 must survive, and must not break anything.

Coordinates are legally absent in GFF -- WormBase and NCBI both emit such rows.
gffbase used to declare `features.start`/`"end"` `NOT NULL`, so the Arrow batch
builder coerced a missing coordinate to `0`: the feature reopened as `0..0` and
serialized zeros where the source said `.`. Making the columns nullable fixes
the round trip but makes a whole class of `TypeError` reachable, since every
coordinate-space operation sorts or does arithmetic on those fields.

These tests cover both halves: the round trip, and every operation that has to
cope with a feature that has no position.
"""

from __future__ import annotations

import gffbase
import pytest
from gffbase import Feature

# One row with no coordinates, one with them, under a shared parent -- enough to
# exercise "skip the unplaced feature but still compute over the rest".
MIXED = (
    "##gff-version 3\n"
    "chr1\tsrc\tmRNA\t1\t1000\t.\t+\t.\tID=t1\n"
    "chr1\tsrc\texon\t.\t.\t.\t+\t.\tID=e_null;Parent=t1\n"
    "chr1\tsrc\texon\t100\t200\t.\t+\t.\tID=e1;Parent=t1\n"
    "chr1\tsrc\texon\t400\t500\t.\t+\t.\tID=e2;Parent=t1\n"
    "chr1\tsrc\tgene\t.\t.\t.\t+\t.\tID=g_null\n"
)


@pytest.fixture
def db(tmp_path):
    src = tmp_path / "mixed.gff3"
    src.write_text(MIXED)
    return gffbase.create_db(str(src), ":memory:")


@pytest.fixture
def disk_db(tmp_path):
    """Same data, but persisted and reopened -- the round trip that failed."""
    src = tmp_path / "mixed.gff3"
    src.write_text(MIXED)
    path = str(tmp_path / "mixed.duckdb")
    gffbase.create_db(str(src), path, force=True)
    return gffbase.FeatureDB(path)


# ---------------------------------------------------------------------------
# Round trip.
# ---------------------------------------------------------------------------


def test_null_coordinates_are_preserved_not_coerced(db):
    feature = db["g_null"]
    assert feature.start is None
    assert feature.end is None


def test_null_coordinates_survive_reopen(disk_db):
    """The original defect: the value had to survive the storage layer."""
    feature = disk_db["g_null"]
    assert (feature.start, feature.end) == (None, None)


def test_null_coordinates_serialize_back_to_dots(db):
    """`0` here would silently corrupt an exported annotation file."""
    assert str(db["g_null"]).split("\t")[3:5] == [".", "."]


def test_length_of_an_unplaced_feature_is_zero(db):
    assert len(db["g_null"]) == 0


def test_placed_features_are_unaffected(db):
    feature = db["e1"]
    assert (feature.start, feature.end) == (100, 200)
    assert len(feature) == 101


# ---------------------------------------------------------------------------
# Query paths. The R-tree and B-tree paths must agree, which is the property
# most at risk: a NULL bbox is invisible to `ST_Intersects`, and a NULL
# comparison is unknown to the B-tree predicate -- for opposite reasons.
# ---------------------------------------------------------------------------


def test_unplaced_features_are_returned_by_a_full_scan(db):
    assert "g_null" in {f.id for f in db.all_features()}


def test_unplaced_features_are_absent_from_region_queries(db):
    """Matches gffutils, where `NULL <= ?` is unknown and the row drops out."""
    assert "g_null" not in {f.id for f in db.region(seqid="chr1", start=1, end=10**6)}


def test_unplaced_features_are_absent_from_batched_region_queries(db):
    hits = db.region_batched([("chr1", 1, 10**6)], format="arrow").column("id").to_pylist()
    assert "g_null" not in hits


def test_region_agrees_between_the_rtree_and_btree_paths(tmp_path, monkeypatch):
    """The two index paths must not disagree about which rows exist.

    They reach the same answer by different routes -- a null envelope never
    intersects, and a null comparison is never true -- so agreement here is
    not automatic.
    """
    src = tmp_path / "mixed.gff3"
    src.write_text(MIXED)

    monkeypatch.delenv("GFFBASE_TEST_DISABLE_RTREE", raising=False)
    rtree_db = gffbase.create_db(str(src), ":memory:")

    monkeypatch.setenv("GFFBASE_TEST_DISABLE_RTREE", "1")
    btree_db = gffbase.create_db(str(src), ":memory:")
    assert not btree_db._rtree_built

    def hits(database):
        return sorted(f.id for f in database.region(seqid="chr1", start=1, end=10**6))

    assert hits(rtree_db) == hits(btree_db)


def test_order_by_length_tolerates_unplaced_features(db):
    """`("end" - start)` is NULL for these; they must not break the sort."""
    ids = [f.id for f in db.features_of_type("exon", order_by="length")]
    assert set(ids) == {"e_null", "e1", "e2"}


# ---------------------------------------------------------------------------
# Coordinate-space operations. Each of these raised
# `TypeError: '<' not supported between instances of 'int' and 'NoneType'`
# from inside a sort key once coordinates became nullable.
# ---------------------------------------------------------------------------


def test_merge_skips_unplaced_features(db):
    merged = [(f.start, f.end) for f in db.merge(list(db.children("t1")))]
    assert merged == [(100, 200), (400, 500)]


def test_merge_all_skips_unplaced_features(db):
    """`merge_all` guarded only `start`, and only in its own sort key -- the
    `end` comparison inside `merge()` still raised.

    The assertion used to be `len(...) > 0`, which stood in for "it did not
    raise". That stopped being a meaningful check once `merge_all` began
    returning only features that genuinely merged: nothing in this fixture
    overlaps, so the correct answer is now `[]` and the old assertion was
    testing the wrong thing. Assert the actual contract instead -- it runs,
    and it does not invent a merge out of the two disjoint exons.
    """
    before = db.count_features_of_type()
    assert db.merge_all() == []
    # And it must not have written anything either.
    assert db.count_features_of_type() == before


def test_merge_all_merges_and_persists_when_features_do_overlap(db):
    """The positive case, on the same null-carrying fixture: an overlap that
    spans the unplaced rows still merges, and the result is written back."""
    db.update(
        [
            Feature(
                seqid="chr1",
                source="src",
                featuretype="exon",
                start=150,
                end=450,
                strand="+",
                id="e_bridge",
                attributes={"ID": "e_bridge", "Parent": "t1"},
                dialect={"fmt": "gff3"},
            )
        ]
    )
    merged = db.merge_all(featuretypes_groups=("exon",))
    assert len(merged) == 1
    m = merged[0]
    assert (m.start, m.end) == (100, 500)
    # Persisted, and reachable by its generated id.
    assert m.id in db
    assert db[m.id].start == 100


def test_interfeatures_skips_unplaced_features(db):
    gaps = [(f.start, f.end) for f in db.interfeatures(list(db.children("t1")))]
    assert gaps == [(201, 399)]


def test_create_introns_skips_unplaced_exons(db):
    introns = [
        (f.start, f.end)
        for f in db.create_introns(parent_featuretype="mRNA", grandparent_featuretype=None)
    ]
    assert introns == [(201, 399)]


def test_create_splice_sites_skips_unplaced_exons(db):
    sites = list(db.create_splice_sites(parent_featuretype="mRNA", grandparent_featuretype=None))
    assert len(sites) == 2


def test_children_bp_ignores_unplaced_children(db):
    assert db.children_bp("t1") == 202  # 101 + 101, the unplaced exon contributes nothing


# ---------------------------------------------------------------------------
# bed12: the anchor and its children need different treatment.
# ---------------------------------------------------------------------------


def test_bed12_skips_unplaced_block_children(db):
    """The null guard used to sit *after* the sort, so an unplaced child raised
    `TypeError` instead of ever reaching it."""
    fields = db.bed12("t1").split("\t")
    block_count, block_sizes, block_starts = int(fields[9]), fields[10], fields[11]
    assert block_count == 2
    assert len(block_sizes.rstrip(",").split(",")) == block_count
    assert len(block_starts.rstrip(",").split(",")) == block_count


def test_bed12_on_an_unplaced_anchor_raises_actionably(db):
    """A BED record for a feature with no position is a caller error, not
    something to silently skip."""
    with pytest.raises(ValueError, match="no start/end coordinates"):
        db.bed12("g_null")


def test_sequence_on_an_unplaced_feature_raises_actionably(db):
    with pytest.raises(ValueError, match="no start/end coordinates"):
        db["g_null"].sequence({})


# ---------------------------------------------------------------------------
# Parser level, both engines.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("engine", ["rust", "python"])
def test_both_engines_yield_none_for_a_dot_coordinate(tmp_path, engine):
    if engine == "rust" and not gffbase.native_available():
        pytest.skip("native extension not built")
    src = tmp_path / "n.gff3"
    src.write_text("##gff-version 3\nchr1\tsrc\tgene\t.\t.\t.\t+\t.\tID=g\n")
    (feature,) = list(gffbase.parse_gff(str(src), engine=engine, validation="gffutils"))
    assert (feature.start, feature.end) == (None, None)
