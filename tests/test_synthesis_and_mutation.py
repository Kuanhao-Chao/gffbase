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
"""Synthesis (interfeatures, merge, create_introns, create_splice_sites,
bed12, children_bp, iter_by_parent_childs) and mutation
(update / delete / add_relation) coverage."""

from __future__ import annotations

import types
from pathlib import Path

import pytest

from gffbase import Feature, FeatureNotFoundError, create_db, merge_criteria as mc

DATA = Path(__file__).parent / "data"


@pytest.fixture
def hier_db():
    return create_db(str(DATA / "hierarchy.gff3"), ":memory:")


# ---------------------------------------------------------------------------
# interfeatures
# ---------------------------------------------------------------------------


def test_interfeatures_yields_gaps(hier_db):
    exons = sorted(hier_db.children("t1", featuretype="exon"),
                   key=lambda f: f.start)
    inters = list(hier_db.interfeatures(exons, new_featuretype="intron"))
    assert len(inters) == len(exons) - 1
    for inter in inters:
        assert inter.featuretype == "intron"
    # First gap should sit between exon 1 (100..200) and exon 2 (500..600).
    assert inters[0].start == 201
    assert inters[0].end == 499


def test_interfeatures_skips_zero_length_gaps(hier_db):
    f1 = Feature(seqid="chr1", start=1, end=10, dialect={"fmt": "gff3"},
                 attributes={"ID": "a"})
    f2 = Feature(seqid="chr1", start=11, end=20, dialect={"fmt": "gff3"},
                 attributes={"ID": "b"})
    inters = list(hier_db.interfeatures([f1, f2]))
    assert inters == []


def test_interfeatures_merge_attributes_dedupe(hier_db):
    a = Feature(seqid="chr1", start=1, end=10,
                attributes={"k": ["v1", "v2"]}, dialect={"fmt": "gff3"})
    b = Feature(seqid="chr1", start=20, end=30,
                attributes={"k": ["v2", "v3"]}, dialect={"fmt": "gff3"})
    inters = list(hier_db.interfeatures([a, b]))
    assert inters[0].attributes["k"] == ["v1", "v2", "v3"]


def test_interfeatures_attribute_func_called(hier_db):
    a = Feature(seqid="chr1", start=1, end=10,
                attributes={"k": "v"}, dialect={"fmt": "gff3"})
    b = Feature(seqid="chr1", start=20, end=30,
                attributes={"k": "v"}, dialect={"fmt": "gff3"})
    seen = []
    def cb(prev, cur, attrs):
        seen.append(attrs)
        attrs["custom"] = ["yes"]
        return attrs
    out = list(hier_db.interfeatures([a, b], attribute_func=cb))
    assert seen
    assert out[0].attributes["custom"] == ["yes"]


def test_interfeatures_update_attributes(hier_db):
    a = Feature(seqid="chr1", start=1, end=10, attributes={"a": "1"},
                dialect={"fmt": "gff3"})
    b = Feature(seqid="chr1", start=20, end=30, attributes={"b": "2"},
                dialect={"fmt": "gff3"})
    out = list(hier_db.interfeatures([a, b], update_attributes={"forced": ["yes"]}))
    assert out[0].attributes["forced"] == ["yes"]


def test_interfeatures_empty_input(hier_db):
    assert list(hier_db.interfeatures([])) == []


# ---------------------------------------------------------------------------
# merge / merge_all
# ---------------------------------------------------------------------------


def test_merge_overlapping_features(hier_db):
    feats = [
        Feature(seqid="chr1", featuretype="exon", strand="+",
                start=1, end=100, dialect={"fmt": "gff3"}),
        Feature(seqid="chr1", featuretype="exon", strand="+",
                start=50, end=150, dialect={"fmt": "gff3"}),
        Feature(seqid="chr1", featuretype="exon", strand="+",
                start=300, end=400, dialect={"fmt": "gff3"}),
    ]
    merged = list(hier_db.merge(feats))
    assert len(merged) == 2
    assert merged[0].start == 1 and merged[0].end == 150
    assert len(merged[0].children) == 2
    assert merged[1].start == 300


def test_merge_empty_input(hier_db):
    assert list(hier_db.merge([])) == []


def test_merge_with_custom_criteria(hier_db):
    feats = [
        Feature(seqid="chr1", featuretype="exon", strand="+",
                start=1, end=10, dialect={"fmt": "gff3"}),
        Feature(seqid="chr1", featuretype="CDS", strand="+",
                start=5, end=15, dialect={"fmt": "gff3"}),
    ]
    # Default criteria require same featuretype → no merge.
    out_default = list(hier_db.merge(feats))
    assert len(out_default) == 2
    # Drop featuretype criterion → both merge.
    out_relaxed = list(hier_db.merge(feats, merge_criteria=(mc.seqid, mc.overlap_end_inclusive, mc.strand)))
    assert len(out_relaxed) == 1


def test_merge_all_returns_list(hier_db):
    out = hier_db.merge_all(featuretypes_groups=("exon",))
    assert isinstance(out, list)
    # Three exons in hierarchy.gff3 (e1, e2, e3); e1 / e2 don't overlap; e3 distinct.
    assert len(out) >= 1


# ---------------------------------------------------------------------------
# create_introns / create_splice_sites
# ---------------------------------------------------------------------------


def test_create_introns_grandparent(hier_db):
    introns = list(hier_db.create_introns(grandparent_featuretype="gene"))
    # Expect at least one intron from t1's two exons.
    assert any(f.featuretype == "intron" for f in introns)


def test_create_introns_parent(hier_db):
    introns = list(hier_db.create_introns(
        grandparent_featuretype=None, parent_featuretype="mRNA"))
    assert any(f.featuretype == "intron" for f in introns)


def test_create_introns_requires_one_anchor(hier_db):
    with pytest.raises(ValueError):
        list(hier_db.create_introns(
            grandparent_featuretype="gene", parent_featuretype="mRNA"))
    with pytest.raises(ValueError):
        list(hier_db.create_introns(
            grandparent_featuretype=None, parent_featuretype=None))


def test_create_splice_sites(hier_db):
    sites = list(hier_db.create_splice_sites(grandparent_featuretype="gene"))
    # Each intron yields 2 splice-site rows (left + right).
    assert all(f.featuretype == "splice_site" for f in sites)
    assert len(sites) >= 2


# ---------------------------------------------------------------------------
# bed12 / children_bp / iter_by_parent_childs
# ---------------------------------------------------------------------------


def test_bed12_basic(hier_db):
    line = hier_db.bed12("t1")
    cols = line.split("\t")
    assert len(cols) == 12
    assert cols[0] == "chr1"
    assert cols[3] in {"t1", "."}
    # block_count = 2 exons
    assert cols[9] == "2"


def test_bed12_with_feature_object(hier_db):
    feat = hier_db["t1"]
    line = hier_db.bed12(feat)
    assert line.split("\t")[0] == "chr1"


def test_bed12_no_cds_uses_thin_thick(hier_db):
    # Use a transcript without CDSs — t2 has only one exon.
    line = hier_db.bed12("t2")
    cols = line.split("\t")
    # thick_start should equal chrom_start when no CDS
    assert int(cols[6]) == int(cols[1])
    assert int(cols[7]) == int(cols[1])


def test_children_bp_sums_exon_lengths(hier_db):
    # t1: exon e1 = 100..200 (101 bp), e2 = 500..600 (101 bp)
    bp = hier_db.children_bp("t1", child_featuretype="exon")
    assert bp == 202


def test_children_bp_with_merge_dedupe(hier_db):
    bp_no_merge = hier_db.children_bp("t1", child_featuretype="exon")
    bp_merge = hier_db.children_bp("t1", child_featuretype="exon", merge=True)
    # Non-overlapping exons: merge doesn't shrink total bp.
    assert bp_merge == bp_no_merge


def test_iter_by_parent_childs(hier_db):
    groups = list(hier_db.iter_by_parent_childs(featuretype="gene"))
    assert len(groups) == 1
    parent, *kids = groups[0]
    assert parent.id == "g1"
    assert len(kids) >= 5


# ---------------------------------------------------------------------------
# delete / add_relation / update
# ---------------------------------------------------------------------------


def test_delete_by_id(hier_db):
    hier_db.delete("e1")
    with pytest.raises(FeatureNotFoundError):
        _ = hier_db["e1"]


def test_delete_by_feature_object(hier_db):
    f = hier_db["e1"]
    hier_db.delete(f)
    with pytest.raises(FeatureNotFoundError):
        _ = hier_db["e1"]


def test_delete_iterable_of_ids(hier_db):
    hier_db.delete(["e1", "e2"])
    with pytest.raises(FeatureNotFoundError):
        _ = hier_db["e1"]
    with pytest.raises(FeatureNotFoundError):
        _ = hier_db["e2"]


def test_delete_iterable_of_features(hier_db):
    feats = [hier_db["e1"], hier_db["e2"]]
    hier_db.delete(feats)
    assert "e1" not in hier_db
    assert "e2" not in hier_db


def test_delete_empty_iterable_is_noop(hier_db):
    hier_db.delete([])
    assert "g1" in hier_db


def test_add_relation_creates_edge_and_closure(hier_db):
    # Insert a synthetic feature first.
    hier_db.conn.execute(
        "INSERT INTO features (id, seqid, source, featuretype, start, \"end\", "
        "score, strand, frame, attributes_blob, extra_blob, file_order, is_synthetic) "
        "VALUES ('orphan', 'chr1', 'test', 'leaf', 100, 200, '.', '+', '.', NULL, NULL, NULL, FALSE)"
    )
    hier_db.add_relation("g1", "orphan")
    # closure includes the new edge at depth 1
    rows = hier_db.conn.execute(
        "SELECT depth FROM closure WHERE ancestor='g1' AND descendant='orphan'"
    ).fetchall()
    assert (1,) in rows


def test_add_relation_with_callbacks(hier_db):
    seen = {}
    def parent_cb(p, c):
        seen["parent_called"] = True
    def child_cb(p, c):
        seen["child_called"] = True
    hier_db.conn.execute(
        "INSERT INTO features (id, seqid, source, featuretype, start, \"end\", "
        "score, strand, frame, attributes_blob, extra_blob, file_order, is_synthetic) "
        "VALUES ('cb', 'chr1', 'test', 'leaf', 100, 200, '.', '+', '.', NULL, NULL, NULL, FALSE)"
    )
    parent = hier_db["g1"]
    child = hier_db["cb"]
    hier_db.add_relation(parent, child, parent_func=parent_cb, child_func=child_cb)
    assert seen.get("parent_called") and seen.get("child_called")


def test_update_appends_features(hier_db):
    new_feat = Feature(
        seqid="chr1", source="test", featuretype="exon",
        start=10000, end=10500, score=".", strand="+", frame=".",
        attributes={"ID": "newx"}, dialect={"fmt": "gff3"}, id="newx",
    )
    hier_db.update([new_feat])
    assert "newx" in hier_db
    assert hier_db["newx"].start == 10000
