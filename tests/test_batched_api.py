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
"""Phase 12 — vectorized batched API tests.

Covers `children_batched`, `parents_batched`, and `region_batched` in all
three return formats (`arrow`, `df`, `polars`) plus the edge cases that
matter to ML consumers.
"""

from __future__ import annotations

import os
from pathlib import Path

import pyarrow as pa
import pytest
from gffbase import create_db

DATA = Path(__file__).parent / "data"

RTREE_DISABLED = os.environ.get("GFFBASE_TEST_DISABLE_RTREE", "").lower() in ("1", "true", "yes")
requires_rtree = pytest.mark.skipif(RTREE_DISABLED, reason="GFFBASE_TEST_DISABLE_RTREE active")


@pytest.fixture
def hier_db():
    return create_db(str(DATA / "hierarchy.gff3"), ":memory:")


@pytest.fixture
def gtf_db():
    return create_db(str(DATA / "synthesize.gtf"), ":memory:")


# ---------------------------------------------------------------------------
# children_batched
# ---------------------------------------------------------------------------


def test_children_batched_returns_arrow_table(hier_db):
    out = hier_db.children_batched(["g1"])
    assert isinstance(out, pa.Table)
    # 7 descendants of g1 (t1, t2 + 4 exons + 2 CDSs).
    assert out.num_rows == 7
    # Schema enforced.
    assert "anchor" in out.column_names
    assert "descendant_id" in out.column_names
    assert "depth" in out.column_names


def test_children_batched_single_query_for_many_anchors(hier_db, monkeypatch):
    """Passing N anchors must run ONE SQL execute, not N. We wrap the
    connection rather than monkey-patching its read-only execute attr."""
    real_execute = type(hier_db.conn).execute

    calls = {"n": 0}

    def counting(self, sql, *a, **kw):
        calls["n"] += 1
        return real_execute(self, sql, *a, **kw)

    monkeypatch.setattr(type(hier_db.conn), "execute", counting)
    out = hier_db.children_batched(["g1", "t1", "t2"], level=1)
    # Exactly one execute for the bulk SELECT.
    assert calls["n"] == 1
    # Anchors are preserved in the output so callers can group by them.
    assert set(out.column("anchor").to_pylist()) == {"g1", "t1", "t2"}


def test_children_batched_level_filter(hier_db):
    out = hier_db.children_batched(["g1"], level=1)
    # g1 → t1, t2 only.
    assert sorted(out.column("descendant_id").to_pylist()) == ["t1", "t2"]


def test_children_batched_featuretype_filter(hier_db):
    out = hier_db.children_batched(["g1"], featuretype="exon")
    assert set(out.column("featuretype").to_pylist()) == {"exon"}


def test_children_batched_featuretype_list(hier_db):
    out = hier_db.children_batched(["g1"], featuretype=["exon", "CDS"])
    assert set(out.column("featuretype").to_pylist()) <= {"exon", "CDS"}


def test_children_batched_accepts_feature_objects(hier_db):
    g1 = hier_db["g1"]
    out = hier_db.children_batched([g1])
    assert out.num_rows == 7


def test_children_batched_rejects_bad_input(hier_db):
    with pytest.raises(TypeError):
        hier_db.children_batched([42])


def test_children_batched_empty_input_returns_empty_table(hier_db):
    out = hier_db.children_batched([])
    assert isinstance(out, pa.Table)
    assert out.num_rows == 0
    # Schema still populated so downstream code sees the same columns.
    assert "descendant_id" in out.column_names


def test_children_batched_dynamic_cte_when_level_exceeds_max_depth(hier_db):
    # Force dynamic CTE path.
    hier_db._max_depth = 0
    out = hier_db.children_batched(["g1"], level=2)
    # All children at depth 2.
    types = set(out.column("featuretype").to_pylist())
    assert types <= {"exon", "CDS"}


def test_children_batched_df_format(hier_db):
    pd = pytest.importorskip("pandas")
    out = hier_db.children_batched(["g1"], format="df")
    assert isinstance(out, pd.DataFrame)
    assert "descendant_id" in out.columns


def test_children_batched_polars_format(hier_db):
    pl = pytest.importorskip("polars")
    out = hier_db.children_batched(["g1"], format="polars")
    assert isinstance(out, pl.DataFrame)
    assert "descendant_id" in out.columns


def test_children_batched_unknown_format_raises(hier_db):
    with pytest.raises(ValueError):
        hier_db.children_batched(["g1"], format="unknown")


def test_children_batched_empty_with_unknown_format_raises(hier_db):
    with pytest.raises(ValueError):
        hier_db.children_batched([], format="unknown")


# ---------------------------------------------------------------------------
# parents_batched
# ---------------------------------------------------------------------------


def test_parents_batched_returns_all_ancestors(hier_db):
    out = hier_db.parents_batched(["e1"])
    ids = sorted(out.column("descendant_id").to_pylist())
    # Depending on direction column shape, parents are returned as the
    # 'descendant_id' column. e1's ancestors: t1 (depth 1), g1 (depth 2).
    assert "t1" in ids and "g1" in ids


def test_parents_batched_level_1_only_direct(hier_db):
    out = hier_db.parents_batched(["e1"], level=1)
    assert out.column("descendant_id").to_pylist() == ["t1"]


def test_parents_batched_anchor_column_carries_input_id(hier_db):
    out = hier_db.parents_batched(["e1", "e2"], level=1)
    # Both anchors should appear; t1 is parent of both.
    anchors = set(out.column("anchor").to_pylist())
    assert anchors == {"e1", "e2"}


def test_parents_batched_empty_returns_empty_arrow(hier_db):
    out = hier_db.parents_batched([])
    assert out.num_rows == 0


# ---------------------------------------------------------------------------
# region_batched
# ---------------------------------------------------------------------------


@requires_rtree
def test_region_batched_rtree_path(gtf_db):
    out = gtf_db.region_batched(
        [("chr1", 100, 600), ("chr2", 1500, 2200)],
        featuretype="exon",
    )
    assert isinstance(out, pa.Table)
    # query_idx column tells us which query each result row belongs to.
    qidx = out.column("query_idx").to_pylist()
    assert set(qidx) == {0, 1}
    # All result rows must match their query's seqid.
    for q, sid in zip(qidx, out.column("seqid").to_pylist(), strict=True):
        assert sid == ("chr1" if q == 0 else "chr2")


def test_region_batched_btree_fallback(gtf_db):
    saved = gtf_db._rtree_built
    gtf_db._rtree_built = False
    try:
        out = gtf_db.region_batched(
            [("chr1", 100, 600)],
            featuretype="exon",
        )
        ids = out.column("id").to_pylist()
        assert "exon_1" in ids and "exon_2" in ids
    finally:
        gtf_db._rtree_built = saved


def test_region_batched_returns_same_features_on_both_paths(gtf_db):
    saved = gtf_db._rtree_built
    rt = gtf_db.region_batched([("chr1", 100, 600)], featuretype="exon")
    gtf_db._rtree_built = False
    try:
        bt = gtf_db.region_batched([("chr1", 100, 600)], featuretype="exon")
    finally:
        gtf_db._rtree_built = saved
    assert sorted(rt.column("id").to_pylist()) == sorted(bt.column("id").to_pylist())


def test_region_batched_completely_within(gtf_db):
    # exon_1 = chr1:100-200, NOT fully within 150..400.
    out = gtf_db.region_batched(
        [("chr1", 150, 400)],
        featuretype="exon",
        completely_within=True,
    )
    assert "exon_1" not in out.column("id").to_pylist()


def test_region_batched_empty_input(gtf_db):
    out = gtf_db.region_batched([])
    assert isinstance(out, pa.Table)
    assert out.num_rows == 0


def test_region_batched_skips_invalid_regions(gtf_db):
    # Mix valid + invalid (seqid-only string, no coords) — invalid ones drop out.
    out = gtf_db.region_batched(
        [
            ("chr1", 100, 600),
            "chr2",  # seqid-only → no coords → filtered
        ],
        featuretype="exon",
    )
    # Only chr1 query produces results.
    assert set(out.column("query_seqid").to_pylist()) == {"chr1"}


def test_region_batched_df_format(gtf_db):
    pd = pytest.importorskip("pandas")
    out = gtf_db.region_batched([("chr1", 100, 600)], format="df")
    assert isinstance(out, pd.DataFrame)
    assert "query_idx" in out.columns


def test_region_batched_polars_format(gtf_db):
    pl = pytest.importorskip("polars")
    out = gtf_db.region_batched([("chr1", 100, 600)], format="polars")
    assert isinstance(out, pl.DataFrame)


def test_region_batched_unknown_format_raises(gtf_db):
    with pytest.raises(ValueError):
        gtf_db.region_batched([("chr1", 100, 600)], format="unknown")


def test_region_batched_empty_unknown_format_raises(gtf_db):
    with pytest.raises(ValueError):
        gtf_db.region_batched([], format="unknown")


# ---------------------------------------------------------------------------
# Numerical equivalence: row-by-row API == batched API for the same inputs
# ---------------------------------------------------------------------------


def test_children_row_by_row_matches_batched(hier_db):
    row_by_row = sorted(c.id for c in hier_db.children("g1", level=None))
    batched = sorted(hier_db.children_batched(["g1"]).column("descendant_id").to_pylist())
    assert row_by_row == batched


def test_region_row_by_row_matches_batched(gtf_db):
    rbr = sorted(f.id for f in gtf_db.region(seqid="chr1", start=100, end=600, featuretype="exon"))
    batched = sorted(
        gtf_db.region_batched([("chr1", 100, 600)], featuretype="exon").column("id").to_pylist()
    )
    assert rbr == batched
