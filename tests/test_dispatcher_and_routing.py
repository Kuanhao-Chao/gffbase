"""Explicit branch coverage for the smart dispatcher (cache vs dynamic CTE)
and the spatial router (R-tree vs B-tree fallback)."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from gffbase import FeatureDB, create_db
from gffbase.ingest import from_file

DATA = Path(__file__).parent / "data"

RTREE_DISABLED = os.environ.get(
    "GFFBASE_TEST_DISABLE_RTREE", ""
).lower() in ("1", "true", "yes")
requires_rtree = pytest.mark.skipif(
    RTREE_DISABLED, reason="GFFBASE_TEST_DISABLE_RTREE active"
)


@pytest.fixture
def hier_db():
    return create_db(str(DATA / "hierarchy.gff3"), ":memory:")


# ---------------------------------------------------------------------------
# _dispatch_relation decision matrix
# ---------------------------------------------------------------------------


def test_dispatcher_level_int_within_max_depth_picks_cache(hier_db):
    # level=1, well within max_depth=8 → cache
    assert hier_db._dispatch_relation(level=1, target_id="g1", direction="children") is False


def test_dispatcher_level_int_exceeding_max_depth_picks_dynamic(hier_db):
    # Force level past max_depth → dynamic
    assert hier_db._dispatch_relation(level=99, target_id="g1", direction="children") is True


def test_dispatcher_level_none_with_closure_picks_cache(hier_db):
    # Hierarchy has closure_max_depth >= 1; no overflow → cache
    assert hier_db._dispatch_relation(level=None, target_id="g1", direction="children") is False


def test_dispatcher_level_none_with_empty_closure_picks_dynamic(hier_db):
    saved = hier_db._closure_max_depth
    hier_db._closure_max_depth = 0
    try:
        assert hier_db._dispatch_relation(level=None, target_id="g1", direction="children") is True
    finally:
        hier_db._closure_max_depth = saved


def test_dispatcher_overflow_forces_dynamic(hier_db):
    # Force overflow check to return True
    saved = hier_db._has_overflow
    hier_db._has_overflow = lambda *_: True
    try:
        assert hier_db._dispatch_relation(level=None, target_id="g1", direction="children") is True
    finally:
        hier_db._has_overflow = saved


def test_parents_uses_dispatcher(hier_db):
    parents = list(hier_db.parents("e1", level=None))
    ids = sorted(p.id for p in parents)
    assert "g1" in ids and "t1" in ids


def test_parents_level_1_only_direct(hier_db):
    parents = list(hier_db.parents("e1", level=1))
    assert [p.id for p in parents] == ["t1"]


def test_parents_dynamic_path_when_level_exceeds_cache(hier_db):
    # Reduce max_depth below the requested level; dispatcher → dynamic CTE
    saved = hier_db._max_depth
    hier_db._max_depth = 0
    try:
        # The hierarchy fixture has g1 → t1 → e1 (depth 2 from g1's perspective);
        # when max_depth=0 the cache covers nothing and any level traversal
        # goes through the dynamic walker.
        out = list(hier_db.parents("e1", level=2))
        assert any(p.id == "g1" for p in out)
    finally:
        hier_db._max_depth = saved


# ---------------------------------------------------------------------------
# Spatial routing
# ---------------------------------------------------------------------------


@requires_rtree
def test_rtree_path_returns_features(hier_db):
    # When R-tree is on, _region_sql_rtree builds an ST_Intersects query.
    feats = list(hier_db.region(seqid="chr1", start=100, end=300, featuretype="exon"))
    assert any(f.id == "e1" for f in feats)


def test_btree_fallback_when_rtree_off(hier_db):
    saved = hier_db._rtree_built
    hier_db._rtree_built = False
    try:
        feats = list(hier_db.region(seqid="chr1", start=100, end=300, featuretype="exon"))
        assert any(f.id == "e1" for f in feats)
    finally:
        hier_db._rtree_built = saved


def test_btree_fallback_partial_region_seqid_only(hier_db):
    saved = hier_db._rtree_built
    hier_db._rtree_built = False
    try:
        feats = list(hier_db.region(seqid="chr1"))
        assert len(feats) > 0
    finally:
        hier_db._rtree_built = saved


def test_btree_fallback_only_start():
    """Phase 7 region path: only start passed."""
    db = create_db(str(DATA / "hierarchy.gff3"), ":memory:")
    db._rtree_built = False
    feats = list(db.region(seqid="chr1", start=100, featuretype="exon"))
    assert all(f.start >= 100 for f in feats)


def test_btree_fallback_only_end():
    db = create_db(str(DATA / "hierarchy.gff3"), ":memory:")
    db._rtree_built = False
    feats = list(db.region(seqid="chr1", end=500, featuretype="exon"))
    assert all(f.end <= 500 for f in feats)


def test_region_no_args_returns_everything():
    db = create_db(str(DATA / "hierarchy.gff3"), ":memory:")
    feats = list(db.region())
    # No filters → full table scan.
    assert len(feats) >= 5


def test_region_unknown_seqid_returns_empty():
    db = create_db(str(DATA / "hierarchy.gff3"), ":memory:")
    feats = list(db.region(seqid="chrZZZ", start=1, end=10))
    assert feats == []


def test_region_completely_within_excludes_overhanging(hier_db):
    # exon e1 is 100..200; querying 150..400 with completely_within=True
    # should NOT return it (overhangs left).
    feats = list(hier_db.region(seqid="chr1", start=150, end=400,
                                 featuretype="exon",
                                 completely_within=True))
    assert "e1" not in [f.id for f in feats]


def test_region_string_form_with_strand_suffix():
    db = create_db(str(DATA / "hierarchy.gff3"), ":memory:")
    feats = list(db.region("chr1:100-300:+"))
    assert all(f.seqid == "chr1" for f in feats)


def test_region_string_form_seqid_only():
    db = create_db(str(DATA / "hierarchy.gff3"), ":memory:")
    feats = list(db.region("chr1"))
    assert len(feats) > 0


def test_region_tuple_two_element():
    db = create_db(str(DATA / "hierarchy.gff3"), ":memory:")
    feats = list(db.region(region=("chr1", "ignored")))
    assert len(feats) > 0


def test_region_tuple_invalid_length_raises():
    db = create_db(str(DATA / "hierarchy.gff3"), ":memory:")
    with pytest.raises(ValueError):
        list(db.region(region=(1, 2, 3, 4)))


def test_region_unsupported_type_raises():
    db = create_db(str(DATA / "hierarchy.gff3"), ":memory:")
    with pytest.raises(TypeError):
        list(db.region(region=42))


def test_region_filter_by_strand_and_featuretype(hier_db):
    feats = list(hier_db.region(seqid="chr1", featuretype="exon", strand="+"))
    assert all(f.strand == "+" and f.featuretype == "exon" for f in feats)


def test_region_filter_featuretype_list(hier_db):
    feats = list(hier_db.region(seqid="chr1", featuretype=["exon", "CDS"]))
    types = {f.featuretype for f in feats}
    assert types <= {"exon", "CDS"}


# ---------------------------------------------------------------------------
# Cosmetic / metadata
# ---------------------------------------------------------------------------


def test_schema_property_returns_string(hier_db):
    s = hier_db.schema
    assert isinstance(s, str)
    assert "features" in s.lower() or "table" in s.lower() or s == ""


def test_analyze_marks_db_analyzed(hier_db):
    assert hier_db._analyzed is False
    hier_db.analyze()
    assert hier_db._analyzed is True


def test_set_pragmas_silently_skips_unsupported(hier_db):
    # journal_mode is SQLite-specific; DuckDB rejects it. Should not raise.
    hier_db.set_pragmas({"journal_mode": "MEMORY"})


def test_featuredb_init_with_existing_path(tmp_path):
    p = tmp_path / "saved.duckdb"
    create_db(str(DATA / "hierarchy.gff3"), str(p))
    # Reopen
    db = FeatureDB(str(p))
    assert "g1" in db


def test_featuredb_init_invalid_dbfn_type_raises():
    with pytest.raises(TypeError):
        FeatureDB(42)


# ---------------------------------------------------------------------------
# all_features ordering branches
# ---------------------------------------------------------------------------


def test_all_features_order_by_length(hier_db):
    feats = list(hier_db.all_features(order_by="length"))
    lengths = [f.end - f.start for f in feats]
    assert lengths == sorted(lengths)


def test_all_features_order_by_seqid_reverse(hier_db):
    feats = list(hier_db.all_features(order_by="seqid", reverse=True))
    assert feats == sorted(feats, key=lambda f: f.seqid, reverse=True)


def test_all_features_limit_string_region(hier_db):
    feats = list(hier_db.all_features(limit="chr1:100-300"))
    assert all(f.seqid == "chr1" for f in feats)
