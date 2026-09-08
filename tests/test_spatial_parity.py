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
"""Spatial routing parity tests.

GFFBase has two spatial paths:
  * R-tree (DuckDB spatial extension) — primary path
  * Multi-column B-tree on `(seqid, start, end)` — fallback when the
    spatial extension is unavailable, or when
    ``GFFBASE_TEST_DISABLE_RTREE=1`` is set.

These tests construct features whose coordinates land **exactly** on
the edge of a queried region (start == query_end, end == query_start,
zero-length overlap, etc.) and prove the two paths return the
**identical** id sets. A divergence here would be a silent
correctness bug for users on the B-tree fallback.
"""

from __future__ import annotations

import pytest
from gffbase import create_db


@pytest.fixture
def edge_db(tmp_path):
    """Boundary-case fixture: every feature's coordinates have been
    chosen to live on or near a query edge.

    Layout (1-based, inclusive — the GFF3 convention):

      e_left   :  90 .. 100   (ends exactly at query_start)
      e_inside : 100 .. 200   (starts at the boundary)
      e_overlap: 150 .. 250   (straddles right edge)
      e_right  : 200 .. 210   (starts at query_end)
      e_far    : 300 .. 400   (no overlap with query 100..200)
    """
    src = tmp_path / "edges.gff3"
    src.write_text(
        "##gff-version 3\n"
        "chr1\trs\texon\t90\t100\t.\t+\t.\tID=e_left\n"
        "chr1\trs\texon\t100\t200\t.\t+\t.\tID=e_inside\n"
        "chr1\trs\texon\t150\t250\t.\t+\t.\tID=e_overlap\n"
        "chr1\trs\texon\t200\t210\t.\t+\t.\tID=e_right\n"
        "chr1\trs\texon\t300\t400\t.\t+\t.\tID=e_far\n"
    )
    return create_db(str(src), str(tmp_path / "edges.duckdb"), force=True)


@pytest.fixture
def edge_db_btree(tmp_path, monkeypatch):
    """Same fixture, but built with the R-tree path *disabled* via the
    library-wide kill switch. The resulting DB exposes only the
    multi-column B-tree fallback."""
    monkeypatch.setenv("GFFBASE_TEST_DISABLE_RTREE", "1")
    src = tmp_path / "edges_btree.gff3"
    src.write_text(
        "##gff-version 3\n"
        "chr1\trs\texon\t90\t100\t.\t+\t.\tID=e_left\n"
        "chr1\trs\texon\t100\t200\t.\t+\t.\tID=e_inside\n"
        "chr1\trs\texon\t150\t250\t.\t+\t.\tID=e_overlap\n"
        "chr1\trs\texon\t200\t210\t.\t+\t.\tID=e_right\n"
        "chr1\trs\texon\t300\t400\t.\t+\t.\tID=e_far\n"
    )
    return create_db(str(src), str(tmp_path / "edges_btree.duckdb"), force=True)


def _ids(db, **region_kwargs):
    """Run db.region(**region_kwargs) and return the sorted id set."""
    return sorted(f.id for f in db.region(**region_kwargs))


# ---------------------------------------------------------------------------
# 1. Off-by-one boundary cases
# ---------------------------------------------------------------------------

# We use the same set of region queries against both paths and assert
# byte-identical id sets out of each. Each parameter is
# (region_kwargs, expected_id_set).

PARAMS = [
    # Inclusive overlap — anything touching [100, 200].
    pytest.param(
        dict(seqid="chr1", start=100, end=200),
        {"e_left", "e_inside", "e_overlap", "e_right"},
        id="inclusive_endpoints",
    ),
    # 1-bp shift inward: [101, 199] — drops e_left (ends at 100) and
    # e_right (starts at 200).
    pytest.param(
        dict(seqid="chr1", start=101, end=199),
        {"e_inside", "e_overlap"},
        id="one_bp_inward_shift",
    ),
    # Zero-length point query — single base at position 100. Touches
    # everything containing position 100: e_left and e_inside.
    pytest.param(
        dict(seqid="chr1", start=100, end=100),
        {"e_left", "e_inside"},
        id="zero_length_at_left_boundary",
    ),
    # Single-base at right boundary.
    pytest.param(
        dict(seqid="chr1", start=200, end=200),
        {"e_inside", "e_overlap", "e_right"},
        id="zero_length_at_right_boundary",
    ),
    # Past everything — empty result.
    pytest.param(
        dict(seqid="chr1", start=10_000, end=20_000),
        set(),
        id="far_past_features",
    ),
    # Spans every feature.
    pytest.param(
        dict(seqid="chr1", start=1, end=10_000),
        {"e_left", "e_inside", "e_overlap", "e_right", "e_far"},
        id="span_all",
    ),
]


@pytest.mark.parametrize(("region_kwargs", "expected"), PARAMS)
def test_rtree_path_returns_expected_ids(edge_db, region_kwargs, expected):
    assert set(_ids(edge_db, **region_kwargs)) == expected


@pytest.mark.parametrize(("region_kwargs", "expected"), PARAMS)
def test_btree_fallback_returns_same_ids(edge_db_btree, region_kwargs, expected):
    assert set(_ids(edge_db_btree, **region_kwargs)) == expected


# ---------------------------------------------------------------------------
# 2. Direct R-tree vs B-tree differential
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(("region_kwargs", "_expected"), PARAMS)
def test_rtree_btree_byte_identical(edge_db, edge_db_btree, region_kwargs, _expected):
    """Differential check: for every boundary-case region, the R-tree
    and B-tree paths must return the exact same id set. A divergence
    here would silently mislead users running on a host where the
    spatial extension fails to load."""
    rtree_ids = set(_ids(edge_db, **region_kwargs))
    btree_ids = set(_ids(edge_db_btree, **region_kwargs))
    assert rtree_ids == btree_ids


# ---------------------------------------------------------------------------
# 3. completely_within=True boundary tests
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("kwargs", "expected"),
    [
        # completely_within=True over [100, 250]: e_inside (100..200) +
        # e_overlap (150..250) are fully contained. e_left (90..100) is
        # NOT (90 < 100). e_right (200..210) IS (200..210 ⊂ 100..250).
        pytest.param(
            dict(seqid="chr1", start=100, end=250, completely_within=True),
            {"e_inside", "e_overlap", "e_right"},
            id="fully_contained_subset",
        ),
        # completely_within=False over the same region pulls in e_left
        # (overlaps at one base).
        pytest.param(
            dict(seqid="chr1", start=100, end=250, completely_within=False),
            {"e_left", "e_inside", "e_overlap", "e_right"},
            id="overlap_includes_edge_features",
        ),
    ],
)
def test_completely_within_boundary(edge_db, kwargs, expected):
    assert set(_ids(edge_db, **kwargs)) == expected


@pytest.mark.parametrize(
    ("kwargs", "expected"),
    [
        pytest.param(
            dict(seqid="chr1", start=100, end=250, completely_within=True),
            {"e_inside", "e_overlap", "e_right"},
            id="fully_contained_subset",
        ),
        pytest.param(
            dict(seqid="chr1", start=100, end=250, completely_within=False),
            {"e_left", "e_inside", "e_overlap", "e_right"},
            id="overlap_includes_edge_features",
        ),
    ],
)
def test_completely_within_boundary_btree(edge_db_btree, kwargs, expected):
    assert set(_ids(edge_db_btree, **kwargs)) == expected


# ---------------------------------------------------------------------------
# 4. Multi-seqid: the R-tree y-band must not leak features across chromosomes
# ---------------------------------------------------------------------------


@pytest.fixture
def multi_seqid_db(tmp_path):
    """Two features with **identical** [start, end] but on different
    seqids. The y-band trick must keep them in disjoint R-tree leaves
    so a query on chr1 cannot return the chrX feature."""
    src = tmp_path / "multi.gff3"
    src.write_text(
        "##gff-version 3\n"
        "chr1\trs\texon\t100\t200\t.\t+\t.\tID=on_chr1\n"
        "chrX\trs\texon\t100\t200\t.\t+\t.\tID=on_chrX\n"
        "chr1\trs\texon\t150\t250\t.\t+\t.\tID=overlap1\n"
        "chrX\trs\texon\t150\t250\t.\t+\t.\tID=overlapX\n"
    )
    return create_db(str(src), str(tmp_path / "multi.duckdb"), force=True)


def test_query_on_chr1_doesnt_leak_chrX(multi_seqid_db):
    rows = sorted(
        f.id
        for f in multi_seqid_db.region(
            seqid="chr1",
            start=100,
            end=200,
        )
    )
    # Only chr1 ids — the y-band keeps the chrX features in a
    # different R-tree subtree.
    assert all(r.endswith("chr1") or r == "overlap1" for r in rows)
    assert "on_chrX" not in rows
    assert "overlapX" not in rows


def test_query_on_chrX_doesnt_leak_chr1(multi_seqid_db):
    rows = sorted(
        f.id
        for f in multi_seqid_db.region(
            seqid="chrX",
            start=100,
            end=200,
        )
    )
    assert "on_chr1" not in rows
    assert "overlap1" not in rows


# ---------------------------------------------------------------------------
# 5. Non-existent seqid
# ---------------------------------------------------------------------------


def test_unknown_seqid_returns_empty(edge_db):
    """Querying a seqid that doesn't exist in the DB returns an empty
    result — no crash, no full-table scan, no `KeyError`."""
    rows = list(edge_db.region(seqid="chrZZ", start=1, end=1000))
    assert rows == []


def test_unknown_seqid_returns_empty_btree(edge_db_btree):
    rows = list(edge_db_btree.region(seqid="chrZZ", start=1, end=1000))
    assert rows == []


# ---------------------------------------------------------------------------
# Reversed spans (end < start)
# ---------------------------------------------------------------------------
#
# Compat mode accepts `end < start` -- it is a warning, not a rejection,
# because real annotation files contain them and refusing the whole file helps
# nobody. `sanitize_gff_file` exists to repair one. Having accepted such a row,
# the two index paths have to agree about it.
#
# They did not. `ST_MakeEnvelope` orders its own corners, so the R-tree treated
# `1000..500` as `500..1000`; the B-tree compared the raw columns and could
# only match when the query was wide enough to satisfy `"end" >= ?`. Same
# database, same query, two answers -- selected by whether the DuckDB spatial
# extension happened to load, which depends on network egress at ingest time.
#
# Normalized is the right reading: someone who wrote `1000 500` meant
# `500..1000`, and INV-8 has asserted the normalizing envelope all along. The
# B-tree now normalizes too.
#
# The existing fixture could not catch this: every feature in it is well formed.


def _reversed_src(path):
    path.write_text(
        "##gff-version 3\n"
        "chr1\trs\texon\t100\t50\t.\t+\t.\tID=reversed\n"
        "chr1\trs\texon\t10\t20\t.\t+\t.\tID=normal\n"
    )
    return str(path)


@pytest.fixture
def reversed_db(tmp_path):
    return create_db(_reversed_src(tmp_path / "rev.gff3"), str(tmp_path / "rev.duckdb"), force=True)


@pytest.fixture
def reversed_db_btree(tmp_path, monkeypatch):
    monkeypatch.setenv("GFFBASE_TEST_DISABLE_RTREE", "1")
    return create_db(
        _reversed_src(tmp_path / "rev_b.gff3"), str(tmp_path / "rev_b.duckdb"), force=True
    )


REVERSED_QUERIES = [
    # (query, ids expected once 100..50 is read as 50..100)
    ({"seqid": "chr1", "start": 60, "end": 200}, {"reversed"}),
    ({"seqid": "chr1", "start": 1, "end": 1000}, {"reversed", "normal"}),
    ({"seqid": "chr1", "start": 40, "end": 60}, {"reversed"}),
    ({"seqid": "chr1", "start": 200, "end": 300}, set()),
    ({"seqid": "chr1", "start": 15, "end": 18}, {"normal"}),
]


@pytest.mark.parametrize(("kwargs", "expected"), REVERSED_QUERIES)
def test_a_reversed_span_is_read_as_its_normalized_range(reversed_db, kwargs, expected):
    assert set(_ids(reversed_db, **kwargs)) == expected


@pytest.mark.parametrize(("kwargs", "expected"), REVERSED_QUERIES)
def test_the_btree_reads_a_reversed_span_the_same_way(reversed_db_btree, kwargs, expected):
    assert set(_ids(reversed_db_btree, **kwargs)) == expected


@pytest.mark.parametrize(("kwargs", "_expected"), REVERSED_QUERIES)
def test_the_two_index_paths_agree_about_a_reversed_span(
    reversed_db, reversed_db_btree, kwargs, _expected
):
    """The property that actually matters: same database, same answer,
    whichever index the environment gave us."""
    assert set(_ids(reversed_db, **kwargs)) == set(_ids(reversed_db_btree, **kwargs))


def test_a_reversed_span_still_round_trips_its_original_bytes(reversed_db):
    """Normalizing is a QUERY-time reading. The stored row keeps the bytes the
    file had, or gffbase would stop being byte-faithful on output."""
    feature = reversed_db["reversed"]
    assert (feature.start, feature.end) == (100, 50)
