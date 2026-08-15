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
"""Phase 5 — hierarchy queries: closure cache + dynamic CTE fallback."""

from __future__ import annotations

from pathlib import Path

import pytest
from gffbase import FeatureDB, create_db
from gffbase.ingest import from_file

DATA = Path(__file__).parent / "data"


@pytest.fixture
def hier_db():
    return create_db(str(DATA / "hierarchy.gff3"), ":memory:")


def test_children_level_1(hier_db):
    kids = sorted(c.id for c in hier_db.children("g1", level=1))
    # Direct children of g1 are the two mRNAs.
    assert kids == ["t1", "t2"]


def test_children_level_2(hier_db):
    kids = sorted(c.id for c in hier_db.children("g1", level=2))
    # Grandchildren: 4 exons + 2 CDSs
    assert kids == ["c1", "c2", "e1", "e2", "e3"]


def test_children_level_none_returns_all(hier_db):
    kids = sorted(c.id for c in hier_db.children("g1", level=None))
    assert kids == ["c1", "c2", "e1", "e2", "e3", "t1", "t2"]


def test_parents_level_1(hier_db):
    pars = sorted(p.id for p in hier_db.parents("e1", level=1))
    assert pars == ["t1"]


def test_parents_level_none(hier_db):
    pars = sorted(p.id for p in hier_db.parents("e1", level=None))
    assert pars == ["g1", "t1"]


def test_children_filter_by_featuretype(hier_db):
    kids = sorted(c.id for c in hier_db.children("g1", level=None, featuretype="exon"))
    assert kids == ["e1", "e2", "e3"]


def test_children_returns_generator(hier_db):
    import types

    assert isinstance(hier_db.children("g1"), types.GeneratorType)
    assert isinstance(hier_db.parents("e1"), types.GeneratorType)


# ---------------------------------------------------------------------------
# Dynamic CTE fallback
# ---------------------------------------------------------------------------


def test_dynamic_cte_when_level_exceeds_max_depth():
    """Build a 5-level chain via add_relation against a DB ingested with
    max_depth=2, then assert children(root, level=4) walks via the dynamic
    CTE (not the closure cache, which only reaches depth 2)."""
    con, stats = from_file(str(DATA / "hierarchy.gff3"), max_depth=2)
    db = FeatureDB((con, stats))
    assert db._max_depth == 2

    # Confirm closure only goes to depth 2.
    max_d = con.execute("SELECT MAX(depth) FROM closure").fetchone()[0]
    assert max_d == 2

    # Add a deep chain: g1 -> t1 already exists; we add t1 -> n1 -> n2 -> n3,
    # so g1 -> n3 lives at depth 4.
    for nid in ("n1", "n2", "n3"):
        con.execute(
            'INSERT INTO features (id, seqid, source, featuretype, start, "end", '
            "score, strand, frame, attributes_blob, extra_blob, file_order, is_synthetic) "
            "VALUES (?, 'chr1', 'test', 'leaf', 100, 200, '.', '+', '.', NULL, NULL, NULL, FALSE)",
            [nid],
        )
    db.add_relation("t1", "n1")
    db.add_relation("n1", "n2")
    db.add_relation("n2", "n3")

    # Cached closure won't have depth 4 entries — but the dynamic CTE should.
    via_cache_depth = con.execute(
        "SELECT MAX(depth) FROM closure WHERE ancestor = 'g1'"
    ).fetchone()[0]
    assert via_cache_depth <= 2

    # Now ask for level=4 — must trigger the dynamic CTE.
    kids = list(db.children("g1", level=4))
    assert [k.id for k in kids] == ["n3"]


def test_dynamic_cte_when_level_none_with_overflow():
    """level=None traversal should detect overflow descendants past
    max_depth and switch to the dynamic CTE so we still see the deep node."""
    con, stats = from_file(str(DATA / "hierarchy.gff3"), max_depth=2)
    db = FeatureDB((con, stats))
    for nid in ("n1", "n2", "n3"):
        con.execute(
            'INSERT INTO features (id, seqid, source, featuretype, start, "end", '
            "score, strand, frame, attributes_blob, extra_blob, file_order, is_synthetic) "
            "VALUES (?, 'chr1', 'test', 'leaf', 100, 200, '.', '+', '.', NULL, NULL, NULL, FALSE)",
            [nid],
        )
    db.add_relation("t1", "n1")
    db.add_relation("n1", "n2")
    db.add_relation("n2", "n3")

    all_descendants = sorted(c.id for c in db.children("g1", level=None))
    assert "n3" in all_descendants
    assert "n2" in all_descendants
    assert "n1" in all_descendants
