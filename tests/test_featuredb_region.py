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
"""Phase 5 — `region()` smart routing tests.

Both R-tree path and B-tree fallback must return identical results. The SQL
is inspected to confirm dispatch.
"""

from __future__ import annotations

import inspect
import os
from pathlib import Path

import pytest

from gffbase import FeatureDB, Feature, create_db
from gffbase.ingest import from_file

DATA = Path(__file__).parent / "data"

# Phase 8: when CI runs the B-tree matrix cell it sets
# GFFBASE_TEST_DISABLE_RTREE=1 — every test that asserts the R-tree path
# must skip in that mode. The other tests run in both modes and validate
# that results are identical regardless of which path is taken.
RTREE_DISABLED = os.environ.get(
    "GFFBASE_TEST_DISABLE_RTREE", ""
).lower() in ("1", "true", "yes")
requires_rtree = pytest.mark.skipif(
    RTREE_DISABLED, reason="GFFBASE_TEST_DISABLE_RTREE set; R-tree path off"
)


@pytest.fixture
def gtf_db():
    return create_db(str(DATA / "synthesize.gtf"), ":memory:")


@pytest.fixture
def gtf_db_no_rtree():
    con, stats = from_file(str(DATA / "synthesize.gtf"), build_rtree=False)
    return FeatureDB((con, stats))


@requires_rtree
def test_rtree_built_default(gtf_db):
    # The DuckDB spatial extension is available in CI/test envs.
    assert gtf_db._rtree_built is True


def test_btree_fallback_when_no_rtree(gtf_db_no_rtree):
    assert gtf_db_no_rtree._rtree_built is False


def test_region_string_form(gtf_db):
    feats = list(gtf_db.region("chr1:150-400"))
    ids = sorted(f.id for f in feats if f.featuretype == "exon")
    # exon 100..200 (overlaps 150..200) and exon 300..500 (overlaps 300..400)
    assert ids == ["exon_1", "exon_2"]


def test_region_tuple_form(gtf_db):
    feats = list(gtf_db.region(region=("chr1", 150, 400), featuretype="exon"))
    ids = sorted(f.id for f in feats)
    assert ids == ["exon_1", "exon_2"]


def test_region_kwargs_form(gtf_db):
    feats = list(gtf_db.region(seqid="chr1", start=150, end=400, featuretype="exon"))
    ids = sorted(f.id for f in feats)
    assert ids == ["exon_1", "exon_2"]


def test_region_feature_form(gtf_db):
    anchor = Feature(seqid="chr1", start=150, end=400)
    feats = list(gtf_db.region(region=anchor, featuretype="exon"))
    ids = sorted(f.id for f in feats)
    assert ids == ["exon_1", "exon_2"]


def test_region_completely_within_excludes_overhanging(gtf_db):
    # exon_1 is 100..200, NOT fully contained in 150..400.
    feats = list(gtf_db.region("chr1:150-400", featuretype="exon", completely_within=True))
    assert feats == []
    # But fully containing it works:
    feats = list(gtf_db.region("chr1:50-250", featuretype="exon", completely_within=True))
    ids = [f.id for f in feats]
    assert ids == ["exon_1"]


def test_region_results_match_between_rtree_and_btree(gtf_db, gtf_db_no_rtree):
    rt = sorted(f.id for f in gtf_db.region("chr1:150-800", featuretype="exon"))
    bt = sorted(f.id for f in gtf_db_no_rtree.region("chr1:150-800", featuretype="exon"))
    assert rt == bt


@requires_rtree
def test_rtree_sql_uses_st_intersects(gtf_db):
    sql, params = gtf_db._region_sql_rtree("chr1", 100, 500, None, None, False)
    assert "ST_Intersects" in sql
    assert "ST_MakeEnvelope" in sql


def test_btree_sql_uses_range(gtf_db_no_rtree):
    sql, params = gtf_db_no_rtree._region_sql_btree("chr1", 100, 500, None, None, False)
    assert "ST_Intersects" not in sql
    assert "start <= ?" in sql
    assert '"end" >= ?' in sql


def test_region_seqid_only_returns_all_on_seqid(gtf_db):
    feats = list(gtf_db.region(seqid="chr2", featuretype="exon"))
    ids = sorted(f.id for f in feats)
    assert ids == ["exon_4", "exon_5"]


def test_region_mutually_exclusive_args_raise(gtf_db):
    with pytest.raises(ValueError):
        list(gtf_db.region(region="chr1:100-200", seqid="chr2"))
