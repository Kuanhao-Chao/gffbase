# ---------------------------------------------------------------------------
# Author: Kuan-Hao Chao <kuanhao.chao@gmail.com>
# ---------------------------------------------------------------------------
"""Phase 5 — basic FeatureDB surface tests."""

from __future__ import annotations

from pathlib import Path

import pytest

from gffbase import FeatureDB, Feature, FeatureNotFoundError, create_db

DATA = Path(__file__).parent / "data"


@pytest.fixture
def hier_db():
    return create_db(str(DATA / "hierarchy.gff3"), ":memory:")


@pytest.fixture
def gtf_db():
    return create_db(str(DATA / "synthesize.gtf"), ":memory:")


def test_getitem_returns_feature(hier_db):
    f = hier_db["g1"]
    assert isinstance(f, Feature)
    assert f.id == "g1"
    assert f.featuretype == "gene"
    assert f.start == 100
    assert f.end == 1000


def test_getitem_missing_raises(hier_db):
    with pytest.raises(FeatureNotFoundError):
        _ = hier_db["does-not-exist"]


def test_contains(hier_db):
    assert "g1" in hier_db
    assert "nope" not in hier_db


def test_count_features_of_type(hier_db):
    assert hier_db.count_features_of_type("gene") == 1
    assert hier_db.count_features_of_type("mRNA") == 2
    assert hier_db.count_features_of_type("exon") == 3
    assert hier_db.count_features_of_type() == 8


def test_featuretypes(hier_db):
    types = sorted(hier_db.featuretypes())
    assert types == ["CDS", "exon", "gene", "mRNA"]


def test_seqids(gtf_db):
    seqs = sorted(gtf_db.seqids())
    assert seqs == ["chr1", "chr2"]


def test_features_of_type_returns_generator(hier_db):
    import types
    gen = hier_db.features_of_type("exon")
    assert isinstance(gen, types.GeneratorType)
    feats = list(gen)
    assert len(feats) == 3
    assert all(f.featuretype == "exon" for f in feats)


def test_all_features(hier_db):
    feats = list(hier_db.all_features())
    assert len(feats) == 8
    # Default order_by is file_order, ascending.
    assert [f.id for f in feats] == ["g1", "t1", "e1", "e2", "c1", "c2", "t2", "e3"]


def test_all_features_filter_strand(gtf_db):
    pos = list(gtf_db.all_features(strand="+", featuretype="exon"))
    neg = list(gtf_db.all_features(strand="-", featuretype="exon"))
    assert len(pos) == 3
    assert len(neg) == 2
