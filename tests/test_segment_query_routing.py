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
"""Region queries against multipart features.

A multipart feature's stored `start`/`end` are the ENVELOPE over its segments,
so an envelope-based overlap test is a superset of the true answer: a feature
whose two segments straddle a query region matches on its envelope while
overlapping nothing. That is the price of keeping `features` one row per
logical feature -- and it is paid back by a recheck conjunct that is emitted
only when the database actually holds a multipart feature.

Two things therefore have to hold, and they pull in opposite directions:

* with nothing multipart, the emitted SQL must be **byte-identical to v1** --
  the whole cost argument rests on that;
* with something multipart, the gap case must be **excluded**, on both the
  R-tree and the B-tree path, which reach the answer by different routes.

The multipart rows are built here at the SQL level. The ingest path that
produces them is Stage C; the query behaviour is testable now, and testing it
now is what keeps Stage C from having to debug two things at once.
"""

from __future__ import annotations

import pytest
from gffbase import create_db

# g_gap's two segments are 100..200 and 800..900, so its envelope is 100..900.
# A query over 400..500 falls in the GAP: the envelope overlaps, no segment
# does. That is the only case the recheck exists for.
SRC = """##gff-version 3
chr1\trs\tgene\t100\t900\t.\t+\t.\tID=g_gap
chr1\trs\tgene\t100\t900\t.\t+\t.\tID=g_solid
chr1\trs\tgene\t2000\t2100\t.\t+\t.\tID=g_far
"""


def _make_multipart(db, fid: str, spans) -> None:
    """Turn an ordinary feature into a multipart one, at the SQL level.

    Sets the envelope on `features` and writes one `segments` row per span --
    exactly the state Stage C's resolve pass will produce.
    """
    con = db.conn
    starts = [s for s, _ in spans]
    ends = [e for _, e in spans]
    con.execute(
        'UPDATE features SET n_segments = ?, start = ?, "end" = ? WHERE id = ?',
        [len(spans), min(starts), max(ends), fid],
    )
    for i, (s, e) in enumerate(spans):
        con.execute(
            """
            INSERT INTO segments
                (feature_id, seg_idx, start, "end", score, frame,
                 attributes_blob, extra_blob, file_order, attrs_same_as_seg0, seqid_y)
            VALUES (?, ?, ?, ?, '.', '.', ?, NULL, ?, TRUE, 0)
            """,
            [fid, i, s, e, f"ID={fid}".encode(), i],
        )
    con.execute(
        "INSERT OR REPLACE INTO meta(key, value) SELECT 'n_multipart', "
        "CAST(COUNT(*) AS VARCHAR) FROM features WHERE n_segments > 1"
    )
    db._n_multipart = int(
        con.execute("SELECT COUNT(*) FROM features WHERE n_segments > 1").fetchone()[0]
    )


@pytest.fixture
def db(tmp_path):
    src = tmp_path / "seg.gff3"
    src.write_text(SRC)
    return create_db(str(src), ":memory:")


@pytest.fixture
def gapped(db):
    _make_multipart(db, "g_gap", [(100, 200), (800, 900)])
    return db


# ---------------------------------------------------------------------------
# Nothing multipart: the SQL must not change at all
# ---------------------------------------------------------------------------


def test_no_conjunct_is_emitted_when_nothing_is_multipart(db):
    assert db._n_multipart == 0
    for sql, _params in (
        db._region_sql_rtree("chr1", 400, 500, None, None, False),
        db._region_sql_btree("chr1", 400, 500, None, None, False),
    ):
        assert "segments" not in sql
        assert "EXISTS" not in sql


def test_the_recheck_helper_returns_nothing_when_not_needed(db):
    assert db._segment_overlap(400, 500) == ([], [])


def test_scan_and_limit_paths_are_also_unchanged(db):
    sql, _ = db._build_scan_sql(
        base_where=[],
        base_params=[],
        limit=("chr1", 400, 500),
        strand=None,
        featuretype=None,
        order_by=None,
        reverse=False,
        completely_within=False,
    )
    assert "EXISTS" not in sql
    where, _ = db._limit_filter(("chr1", 400, 500), False)
    assert not any("EXISTS" in w for w in where)


# ---------------------------------------------------------------------------
# Something multipart: the gap must be excluded
# ---------------------------------------------------------------------------


def test_conjunct_appears_once_a_feature_is_multipart(gapped):
    assert gapped._n_multipart == 1
    sql, params = gapped._region_sql_btree("chr1", 400, 500, None, None, False)
    assert "EXISTS" in sql
    assert "FROM segments s" in sql
    # The recheck binds the region bounds a second time, in (end, start) order.
    assert params[-2:] == [500, 400]


def test_a_query_in_the_gap_does_not_return_the_feature(gapped):
    """The defect this exists to prevent: `g_gap` spans 100..200 and 800..900,
    so a query over 400..500 overlaps neither segment -- but does overlap the
    100..900 envelope."""
    found = {f.id for f in gapped.region(("chr1", 400, 500))}
    assert "g_gap" not in found
    # ...while the plain feature covering the same span is still returned, so
    # this is not the conjunct simply excluding everything.
    assert "g_solid" in found


@pytest.mark.parametrize(
    "region,expected",
    [
        ((150, 160), True),  # inside segment 0
        ((850, 860), True),  # inside segment 1
        ((400, 500), False),  # the gap
        ((190, 810), True),  # spans the gap, touches both segments
        ((201, 799), False),  # strictly inside the gap
        ((90, 110), True),  # straddles the start of segment 0
        ((3000, 3100), False),  # nowhere near
    ],
)
def test_overlap_is_decided_by_segments_not_by_the_envelope(gapped, region, expected):
    found = {f.id for f in gapped.region(("chr1", region[0], region[1]))}
    assert ("g_gap" in found) is expected


def test_both_query_paths_agree(gapped):
    """The R-tree and B-tree paths reach the answer by opposite routes -- a
    spatial index scan versus a filtered table scan -- so agreeing is evidence
    the conjunct is correct rather than that one path is simply ignored."""
    for lo, hi in ((150, 160), (400, 500), (190, 810), (850, 860)):
        rtree = {f.id for f in gapped.region(("chr1", lo, hi))}
        btree_sql, btree_params = gapped._region_sql_btree("chr1", lo, hi, None, None, False)
        btree = {r[0] for r in gapped.conn.execute(btree_sql, btree_params).fetchall()}
        assert rtree == btree, f"paths disagree on {lo}..{hi}"


def test_completely_within_needs_no_recheck(gapped):
    """An envelope lies inside a region exactly when all of its segments do, so
    the envelope test is already exact -- emitting the conjunct here would be
    wasted work, and getting it wrong would be a bug."""
    sql, _ = gapped._region_sql_btree("chr1", 50, 1000, None, None, True)
    assert "EXISTS" not in sql
    assert "g_gap" in {f.id for f in gapped.region(("chr1", 50, 1000), completely_within=True)}
    assert "g_gap" not in {f.id for f in gapped.region(("chr1", 50, 500), completely_within=True)}


def test_batched_region_excludes_the_gap_too(gapped):
    """`region_batched` correlates against staged query columns rather than
    bound parameters, so it needs its own form of the same recheck."""
    result = gapped.region_batched([("chr1", 400, 500), ("chr1", 150, 160)])
    by_query: dict[int, set] = {}
    for qi, fid in zip(result["query_idx"].to_pylist(), result["id"].to_pylist(), strict=True):
        by_query.setdefault(qi, set()).add(fid)
    assert "g_gap" not in by_query.get(0, set())
    assert "g_gap" in by_query.get(1, set())


def test_a_multipart_feature_is_returned_exactly_once(gapped):
    """`features` physically holds one row per logical feature, so logical
    dedup is structural -- no DISTINCT anywhere. A query overlapping BOTH
    segments must still yield one feature, not two."""
    ids = [f.id for f in gapped.region(("chr1", 100, 900))]
    assert ids.count("g_gap") == 1


# ---------------------------------------------------------------------------
# The planner must still use the R-tree
# ---------------------------------------------------------------------------


def test_the_rtree_index_survives_the_added_conjunct(gapped):
    """The conjunct is deliberately a separate top-level `AND`, so
    `ST_Intersects` stays a top-level conjunct and remains index-eligible.
    Folding it into an `OR` at the top level would silently turn every region
    query into a full scan."""
    if not gapped._rtree_built:
        pytest.skip("spatial extension unavailable")
    sql, params = gapped._region_sql_rtree("chr1", 400, 500, None, None, False)
    assert "EXISTS" in sql, "fixture is not exercising the multipart path"
    plan = "\n".join(
        str(x) for row in gapped.conn.execute("EXPLAIN " + sql, params).fetchall() for x in row
    )
    assert "RTREE_INDEX_SCAN" in plan, f"R-tree scan lost from plan:\n{plan}"
