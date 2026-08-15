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

from pathlib import Path

import pytest
from gffbase import Feature, FeatureNotFoundError, create_db
from gffbase import merge_criteria as mc

DATA = Path(__file__).parent / "data"


@pytest.fixture
def hier_db():
    return create_db(str(DATA / "hierarchy.gff3"), ":memory:")


# ---------------------------------------------------------------------------
# interfeatures
# ---------------------------------------------------------------------------


def test_interfeatures_yields_gaps(hier_db):
    exons = sorted(hier_db.children("t1", featuretype="exon"), key=lambda f: f.start)
    inters = list(hier_db.interfeatures(exons, new_featuretype="intron"))
    assert len(inters) == len(exons) - 1
    for inter in inters:
        assert inter.featuretype == "intron"
    # First gap should sit between exon 1 (100..200) and exon 2 (500..600).
    assert inters[0].start == 201
    assert inters[0].end == 499


def test_interfeatures_skips_zero_length_gaps(hier_db):
    f1 = Feature(seqid="chr1", start=1, end=10, dialect={"fmt": "gff3"}, attributes={"ID": "a"})
    f2 = Feature(seqid="chr1", start=11, end=20, dialect={"fmt": "gff3"}, attributes={"ID": "b"})
    inters = list(hier_db.interfeatures([f1, f2]))
    assert inters == []


def test_interfeatures_merge_attributes_dedupe(hier_db):
    a = Feature(
        seqid="chr1", start=1, end=10, attributes={"k": ["v1", "v2"]}, dialect={"fmt": "gff3"}
    )
    b = Feature(
        seqid="chr1", start=20, end=30, attributes={"k": ["v2", "v3"]}, dialect={"fmt": "gff3"}
    )
    inters = list(hier_db.interfeatures([a, b]))
    assert inters[0].attributes["k"] == ["v1", "v2", "v3"]


def test_interfeatures_attribute_func_called(hier_db):
    """`attribute_func` is UNARY and runs on each flank's attributes.

    It used to take `(prev, cur, attrs)` and run once on the merged result,
    which meant any callback written against gffutils raised `TypeError`. It
    now matches the oracle: one argument, one return, applied to each
    neighbour *before* the merge -- so a callback can rewrite a flank's
    attributes and see the rewrite carried into the gap.
    """
    a = Feature(seqid="chr1", start=1, end=10, attributes={"k": "a"}, dialect={"fmt": "gff3"})
    b = Feature(seqid="chr1", start=20, end=30, attributes={"k": "b"}, dialect={"fmt": "gff3"})
    seen = []

    def cb(attrs):
        seen.append(dict(attrs))
        return {**attrs, "custom": ["yes"]}

    out = list(hier_db.interfeatures([a, b], attribute_func=cb))
    # Once per flank, not once per gap.
    assert len(seen) == 2
    assert seen == [{"k": ["a"]}, {"k": ["b"]}]
    assert out[0].attributes["custom"] == ["yes"]
    # Both flanks' values survive the merge, deduplicated and sorted.
    assert out[0].attributes["k"] == ["a", "b"]


def test_interfeatures_update_attributes(hier_db):
    a = Feature(seqid="chr1", start=1, end=10, attributes={"a": "1"}, dialect={"fmt": "gff3"})
    b = Feature(seqid="chr1", start=20, end=30, attributes={"b": "2"}, dialect={"fmt": "gff3"})
    out = list(hier_db.interfeatures([a, b], update_attributes={"forced": ["yes"]}))
    assert out[0].attributes["forced"] == ["yes"]


def test_interfeatures_empty_input(hier_db):
    assert list(hier_db.interfeatures([])) == []


# ---------------------------------------------------------------------------
# merge / merge_all
# ---------------------------------------------------------------------------


def test_merge_overlapping_features(hier_db):
    feats = [
        Feature(
            seqid="chr1", featuretype="exon", strand="+", start=1, end=100, dialect={"fmt": "gff3"}
        ),
        Feature(
            seqid="chr1", featuretype="exon", strand="+", start=50, end=150, dialect={"fmt": "gff3"}
        ),
        Feature(
            seqid="chr1",
            featuretype="exon",
            strand="+",
            start=300,
            end=400,
            dialect={"fmt": "gff3"},
        ),
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
        Feature(
            seqid="chr1", featuretype="exon", strand="+", start=1, end=10, dialect={"fmt": "gff3"}
        ),
        Feature(
            seqid="chr1", featuretype="CDS", strand="+", start=5, end=15, dialect={"fmt": "gff3"}
        ),
    ]
    # Default criteria require same featuretype → no merge.
    out_default = list(hier_db.merge(feats))
    assert len(out_default) == 2
    # Drop featuretype criterion → both merge.
    out_relaxed = list(
        hier_db.merge(feats, merge_criteria=(mc.seqid, mc.overlap_end_inclusive, mc.strand))
    )
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
    introns = list(hier_db.create_introns(grandparent_featuretype=None, parent_featuretype="mRNA"))
    assert any(f.featuretype == "intron" for f in introns)


def test_create_introns_requires_one_anchor(hier_db):
    with pytest.raises(ValueError):
        list(hier_db.create_introns(grandparent_featuretype="gene", parent_featuretype="mRNA"))
    with pytest.raises(ValueError):
        list(hier_db.create_introns(grandparent_featuretype=None, parent_featuretype=None))


def test_create_splice_sites(hier_db):
    """Splice sites are 2 bp and typed by their position in the transcript.

    They used to be 1 bp and always typed `splice_site`. A splice site is a
    dinucleotide (GT donor, AG acceptor), so a 1 bp feature names half of one;
    and the type carries the biology -- the left site of a minus-strand
    transcript is its 3' site, not its 5'.
    """
    sites = list(hier_db.create_splice_sites(grandparent_featuretype="gene"))
    assert len(sites) >= 2
    assert all(len(f) == 2 for f in sites), [len(f) for f in sites]
    assert all(
        f.featuretype
        in {"five_prime_cis_splice_site", "three_prime_cis_splice_site", "splice_site"}
        for f in sites
    )
    # A stranded transcript gets both a donor and an acceptor, never two of
    # the same kind.
    stranded = [f for f in sites if f.strand in "+-"]
    if stranded:
        assert {f.featuretype for f in stranded} == {
            "five_prime_cis_splice_site",
            "three_prime_cis_splice_site",
        }
    # All left sites precede all right sites.
    assert len({f.featuretype for f in sites[: len(sites) // 2]}) <= 1


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


def test_bed12_no_cds_marks_the_whole_feature_thick(hier_db):
    """With no CDS children, thickStart/thickEnd span the feature.

    This used to collapse both to `chromStart`, which renders the feature
    entirely THIN -- the opposite claim, and one a genome browser draws
    differently. The oracle uses the feature's own (1-based) start and its
    stop, and that asymmetry with `chromStart` is deliberate upstream.
    """
    feat = hier_db["t2"]
    cols = hier_db.bed12("t2").split("\t")
    assert int(cols[6]) == feat.start
    assert int(cols[7]) == feat.end
    assert int(cols[6]) != int(cols[7]), "a thick span of zero is not 'no CDS'"


def test_bed12_has_no_trailing_comma(hier_db):
    """UCSC tolerates one; the oracle emits none, so every line differed."""
    cols = hier_db.bed12("t1").split("\t")
    assert not cols[10].endswith(",")
    assert not cols[11].endswith(",")
    assert len(cols[10].split(",")) == int(cols[9])
    assert len(cols[11].split(",")) == int(cols[9])


def test_bed12_refuses_both_thick_and_thin(hier_db):
    with pytest.raises(ValueError, match="only specify one"):
        hier_db.bed12("t1", thick_featuretype=["CDS"], thin_featuretype=["UTR"])


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


def test_delete_of_a_middle_node_leaves_no_orphaned_closure_rows(hier_db):
    """Deleting a transcript must take its exons out of the gene's descendants.

    `delete()` used to remove only the closure rows that NAMED the deleted id
    as ancestor or descendant. A depth-2 row like `g1 -> e1` names neither --
    it merely routed *through* the transcript -- so it survived, and
    `children(g1, level=None)` kept returning exons of a transcript that no
    longer existed. The closure is rebuilt from `edges` now, which is what
    `update()` already did.
    """
    # t1's own children, which reach g1 only THROUGH t1. (t2's exon e3 is a
    # sibling branch and must survive -- deleting t1 says nothing about it.)
    orphaned_by_the_delete = {f.id for f in hier_db.children("t1", level=None)}
    assert orphaned_by_the_delete == {"e1", "e2", "c1", "c2"}, "precondition"

    before = {f.id for f in hier_db.children("g1", level=None)}
    assert {"t1"} | orphaned_by_the_delete <= before, "precondition: g1 reaches them all"

    hier_db.delete("t1")

    after = {f.id for f in hier_db.children("g1", level=None)}
    assert "t1" not in after
    still_reachable = after & orphaned_by_the_delete
    assert not still_reachable, (
        f"{sorted(still_reachable)} reached g1 only through the deleted t1, but are "
        "still reachable -- the depth-2 closure rows that routed through t1 survived"
    )
    assert "e3" in after, "t2's exon is a sibling branch and must be untouched"

    # And the stored closure must match a closure derived from scratch.
    stale = hier_db.execute(
        "SELECT COUNT(*) FROM closure c "
        "WHERE NOT EXISTS (SELECT 1 FROM features f WHERE f.id = c.ancestor) "
        "   OR NOT EXISTS (SELECT 1 FROM features f WHERE f.id = c.descendant)"
    ).fetchone()[0]
    assert stale == 0, f"{stale} closure rows reference a deleted feature"


def test_mutation_refreshes_the_depth_the_dispatcher_routes_on(hier_db):
    """`_closure_max_depth` is read once at open and the dispatcher trusts it.

    A mutation can change the corpus's real depth, so every mutator has to
    re-read it -- and persist it, so a handle opened later agrees. Neither
    `update()` nor `delete()` did.
    """
    hier_db.delete("t1")
    live = hier_db.execute("SELECT COALESCE(MAX(depth), 0) FROM closure").fetchone()[0]
    assert hier_db._closure_max_depth == live
    persisted = hier_db.execute("SELECT value FROM meta WHERE key = 'closure_max_depth'").fetchone()
    assert persisted is not None and int(persisted[0]) == live


def test_add_relation_creates_edge_and_closure(hier_db):
    # Insert a synthetic feature first.
    hier_db.conn.execute(
        'INSERT INTO features (id, seqid, source, featuretype, start, "end", '
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
        'INSERT INTO features (id, seqid, source, featuretype, start, "end", '
        "score, strand, frame, attributes_blob, extra_blob, file_order, is_synthetic) "
        "VALUES ('cb', 'chr1', 'test', 'leaf', 100, 200, '.', '+', '.', NULL, NULL, NULL, FALSE)"
    )
    parent = hier_db["g1"]
    child = hier_db["cb"]
    hier_db.add_relation(parent, child, parent_func=parent_cb, child_func=child_cb)
    assert seen.get("parent_called") and seen.get("child_called")


def test_update_appends_features(hier_db):
    new_feat = Feature(
        seqid="chr1",
        source="test",
        featuretype="exon",
        start=10000,
        end=10500,
        score=".",
        strand="+",
        frame=".",
        attributes={"ID": "newx"},
        dialect={"fmt": "gff3"},
        id="newx",
    )
    hier_db.update([new_feat])
    assert "newx" in hier_db
    assert hier_db["newx"].start == 10000
