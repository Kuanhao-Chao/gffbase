# ---------------------------------------------------------------------------
# Author: Kuan-Hao Chao <kuanhao.chao@gmail.com>
# ---------------------------------------------------------------------------
"""Phase 5 — invariants from Phase 1 §3.9 (1-based inclusive coords,
list-wrapped attrs, generator return types, dialect round-trip)."""

from __future__ import annotations

from pathlib import Path
import types

import pytest

from gffbase import create_db, Feature

DATA = Path(__file__).parent / "data"


@pytest.fixture
def hier_db():
    return create_db(str(DATA / "hierarchy.gff3"), ":memory:")


def test_one_based_inclusive_length(hier_db):
    f = hier_db["e1"]  # 100..200 inclusive
    assert f.start == 100
    assert f.end == 200
    assert len(f) == 101  # 200 - 100 + 1


def test_attributes_are_lists(hier_db):
    f = hier_db["e1"]
    assert isinstance(f.attributes["Parent"], list)
    assert f.attributes["Parent"] == ["t1"]
    f2 = hier_db["g1"]
    assert isinstance(f2.attributes["ID"], list)
    assert f2.attributes["ID"] == ["g1"]


def test_generator_return_types(hier_db):
    assert isinstance(hier_db.children("g1"), types.GeneratorType)
    assert isinstance(hier_db.parents("e1"), types.GeneratorType)
    assert isinstance(hier_db.region(seqid="chr1"), types.GeneratorType)
    assert isinstance(hier_db.all_features(), types.GeneratorType)
    assert isinstance(hier_db.features_of_type("exon"), types.GeneratorType)
    assert isinstance(hier_db.featuretypes(), types.GeneratorType)
    assert isinstance(hier_db.seqids(), types.GeneratorType)


def test_str_round_trip(hier_db):
    """Authored features that haven't had their attributes mutated should
    serialize back to a line where the col-9 bytes match the original."""
    f = hier_db["e1"]
    line = str(f)
    parts = line.split("\t")
    assert parts[0] == "chr1"
    assert parts[2] == "exon"
    assert parts[3] == "100"
    assert parts[4] == "200"
    # col-9 should be the byte-faithful original (we stored the blob).
    assert parts[8] == "ID=e1;Parent=t1"


def test_chrom_alias(hier_db):
    f = hier_db["g1"]
    assert f.chrom == f.seqid
    f.chrom = "chrZ"
    assert f.seqid == "chrZ"


def test_stop_alias(hier_db):
    f = hier_db["g1"]
    assert f.stop == f.end


def test_feature_int_index(hier_db):
    f = hier_db["g1"]
    assert f[0] == f.seqid
    assert f[3] == f.start
    assert f[4] == f.end


def test_feature_str_index(hier_db):
    f = hier_db["g1"]
    assert f["ID"] == ["g1"]
