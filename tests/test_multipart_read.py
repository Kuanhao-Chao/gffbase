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
"""Reading discontinuous features back out.

Three things have to hold at once:

* the row-by-row APIs keep yielding `Feature` objects -- a `MultipartFeature`
  IS one, so legacy consumers are unaffected, but a bare `FeatureSegment` must
  never leak into `region()` / `children()` / `all_features()`;
* segments are fetched once per chunk, not once per feature, or iterating a
  corpus with 345 discontinuous features becomes 345 extra round trips;
* a file round-trips byte for byte, which is the point of storing the physical
  lines at all.

The prefetch is where this got interesting. A DuckDB connection holds ONE
result set, so running the segment query on `self.conn` mid-iteration discarded
the rows `_yield_features` was still streaming: the FlyBase 50k corpus came
back as 10 069 lines instead of 49 981, and nothing raised. `_CHUNK` is a class
attribute so that failure is reachable from a four-line fixture.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest
from gffbase import Feature, FeatureSegment, MultipartFeature, create_db
from gffbase.interface import FeatureDB

UPSTREAM = Path(__file__).parent / "data" / "upstream"

SPLIT_CDS = """##gff-version 3
chr1\trs\tgene\t100\t900\t.\t+\t.\tID=g1
chr1\trs\tmRNA\t100\t900\t.\t+\t.\tID=t1;Parent=g1
chr1\trs\tCDS\t100\t200\t.\t+\t0\tID=cds1;Parent=t1
chr1\trs\tCDS\t800\t900\t.\t+\t2\tID=cds1;Parent=t1
"""


@pytest.fixture
def db(tmp_path):
    src = tmp_path / "in.gff3"
    src.write_text(SPLIT_CDS)
    return create_db(str(src), ":memory:", mode="strict")


def _cols(table, *names):
    return list(zip(*(table[n].to_pylist() for n in names), strict=True))


# ---------------------------------------------------------------------------
# The row-by-row APIs
# ---------------------------------------------------------------------------


def test_a_fused_feature_comes_back_as_a_multipart_feature(db):
    f = db["cds1"]
    assert isinstance(f, MultipartFeature)
    assert f.is_multipart is True
    assert f.n_segments == 2


def test_an_ordinary_feature_is_still_a_plain_feature(db):
    """`type(f) is Feature` stops being true only for the rows that really are
    discontinuous. Upgrading everything would be a silent behaviour change for
    every caller doing an exact type check."""
    assert type(db["g1"]) is Feature
    assert type(db["t1"]) is Feature


@pytest.mark.parametrize(
    "call",
    [
        lambda db: db.all_features(),
        lambda db: db.region(("chr1", 1, 1000)),
        lambda db: db.children("t1"),
        lambda db: db.parents("cds1"),
        lambda db: db.features_of_type("CDS"),
    ],
)
def test_no_bare_segment_leaks_into_a_row_by_row_api(db, call):
    """A `FeatureSegment` reaching these would corrupt legacy consumers: its
    coordinates are one line's, not the feature's."""
    for f in call(db):
        assert isinstance(f, Feature)
        assert not isinstance(f, FeatureSegment), f"{f.id} came back as a bare segment"


def test_every_iteration_path_upgrades_the_fused_feature(db):
    for call in (db.all_features, lambda: db.region(("chr1", 1, 1000)), lambda: db.children("t1")):
        got = {f.id: f for f in call()}
        assert isinstance(got["cds1"], MultipartFeature), "not upgraded on this path"


def test_the_feature_reports_the_envelope_and_its_segments(db):
    f = db["cds1"]
    assert (f.start, f.end) == (100, 900)
    assert len(f) == 801
    assert f.covered_length == 202
    assert [(s.seg_idx, s.start, s.end, s.frame) for s in f.segments] == [
        (0, 100, 200, "0"),
        (1, 800, 900, "2"),
    ]


def test_each_segment_carries_its_own_phase_and_the_shared_columns(db):
    """seqid/source/featuretype/strand come from the logical row -- the ingest
    predicate guarantees they are invariant -- and only the per-line columns
    come from `segments`."""
    for seg in db["cds1"].segments:
        assert (seg.seqid, seg.source, seg.featuretype, seg.strand) == ("chr1", "rs", "CDS", "+")
        assert seg.id == "cds1"
    assert [s.frame for s in db["cds1"].segments] == ["0", "2"]


# ---------------------------------------------------------------------------
# Round-trip
# ---------------------------------------------------------------------------


def _reassemble(db) -> list[str]:
    """Every physical line the database holds, back in file order."""
    pairs = [
        (seg.file_order, line)
        for f in db.all_features()
        for seg, line in zip(f.segments, f.to_lines(), strict=True)
    ]
    return [line for _order, line in sorted(pairs)]


def _feature_lines(path: Path) -> list[str]:
    out = []
    for line in path.read_text(errors="replace").splitlines():
        if not line or line.startswith("#"):
            continue
        if line.startswith(">"):
            break
        if len(line.split("\t")) >= 9:
            out.append(line)
    return out


def test_a_split_cds_round_trips_byte_for_byte(tmp_path):
    src = tmp_path / "in.gff3"
    src.write_text(SPLIT_CDS)
    db = create_db(str(src), ":memory:", mode="strict")
    assert _reassemble(db) == _feature_lines(src)


@pytest.mark.parametrize("name", ["synthetic.gff3", "random-chr.gff", "ncbi_gff3.txt"])
def test_upstream_corpora_round_trip_byte_for_byte(name):
    """The strongest available claim that fusing lost nothing: every input line
    comes back, unchanged, in its original position."""
    path = UPSTREAM / name
    db = create_db(str(path), ":memory:", mode="strict", on_multipart_conflict="split")
    assert _reassemble(db) == _feature_lines(path)


@pytest.mark.skipif(
    not os.environ.get("GFFBASE_GFFUTILS_DATA"),
    reason="set GFFBASE_GFFUTILS_DATA to a gffutils test/data directory",
)
def test_flybase_50k_round_trips_byte_for_byte():
    """49 981 lines, 345 of them belonging to discontinuous features. This is
    the case that exposed the truncating prefetch -- at 10 000 rows per chunk,
    a four-line fixture never reaches a chunk boundary."""
    path = Path(os.environ["GFFBASE_GFFUTILS_DATA"]) / "dmel-all-no-analysis-r5.49_50k_lines.gff"
    if not path.is_file():
        pytest.skip(f"{path.name} not present")
    db = create_db(str(path), ":memory:", mode="strict", on_multipart_conflict="split")
    assert _reassemble(db) == _feature_lines(path)


# ---------------------------------------------------------------------------
# The prefetch
# ---------------------------------------------------------------------------


def test_iteration_survives_a_chunk_boundary(db, monkeypatch):
    """The regression. Prefetching on the shared connection discarded the rows
    the outer cursor was still streaming, so iteration stopped at the first
    chunk -- silently, with a short result rather than an error."""
    monkeypatch.setattr(FeatureDB, "_CHUNK", 1)
    assert [f.id for f in db.all_features()] == ["g1", "t1", "cds1"]
    assert isinstance([f for f in db.all_features() if f.id == "cds1"][0], MultipartFeature)


@pytest.mark.parametrize("chunk", [1, 2, 3, 4, 10])
def test_the_result_is_the_same_at_every_chunk_size(db, monkeypatch, chunk):
    monkeypatch.setattr(FeatureDB, "_CHUNK", chunk)
    assert [f.id for f in db.all_features()] == ["g1", "t1", "cds1"]
    assert [f.n_segments for f in db.all_features()] == [1, 1, 2]


def test_segments_are_fetched_once_per_chunk_not_once_per_feature(db, monkeypatch):
    """Otherwise iterating a corpus with 345 discontinuous features is 345
    extra round trips."""
    calls = []
    original = FeatureDB._prefetch_segments

    def counting(self, ids):
        calls.append(list(ids))
        return original(self, ids)

    monkeypatch.setattr(FeatureDB, "_prefetch_segments", counting)
    list(db.all_features())
    assert len(calls) == 1, f"expected one prefetch for one chunk, got {len(calls)}"
    assert calls[0] == ["g1", "t1", "cds1"]


def test_no_prefetch_happens_when_nothing_is_multipart(tmp_path, monkeypatch):
    """`_n_multipart` gates it, so an ordinary corpus pays one attribute test
    per 10 000 rows and no query at all."""
    src = tmp_path / "plain.gff3"
    src.write_text("##gff-version 3\nchr1\trs\tgene\t1\t9\t.\t+\t.\tID=g1\n")
    plain = create_db(str(src), ":memory:", mode="strict")

    calls = []
    monkeypatch.setattr(FeatureDB, "_prefetch_segments", lambda self, ids: calls.append(ids) or {})
    assert [f.id for f in plain.all_features()] == ["g1"]
    assert calls == []


def test_a_single_lookup_of_an_ordinary_feature_costs_no_segment_query(db, monkeypatch):
    """`db[id]` projects `n_segments` and only reaches for segments when there
    are any -- otherwise every point lookup in a multipart database would pay
    for a table the feature has no rows in."""
    calls = []
    original = FeatureDB._prefetch_segments
    monkeypatch.setattr(
        FeatureDB, "_prefetch_segments", lambda self, ids: calls.append(ids) or original(self, ids)
    )
    db["g1"]
    assert calls == []
    db["cds1"]
    assert calls == [["cds1"]]


# ---------------------------------------------------------------------------
# explode_segments -- tabular APIs only
# ---------------------------------------------------------------------------


def test_region_batched_is_logical_by_default(db):
    # Ordered by (start, id): all three envelopes begin at 100.
    assert _cols(db.region_batched([("chr1", 100, 900)]), "id", "start", "end") == [
        ("cds1", 100, 900),
        ("g1", 100, 900),
        ("t1", 100, 900),
    ]
    assert "seg_idx" not in db.region_batched([("chr1", 100, 900)]).column_names


def test_region_batched_explodes_into_physical_lines(db):
    """The fused CDS becomes two rows carrying their own coordinates, and they
    sort by their own start -- so the second segment lands after the features
    that begin at 100, not beside its own first segment."""
    got = _cols(
        db.region_batched([("chr1", 100, 900)], explode_segments=True),
        "id",
        "seg_idx",
        "start",
        "end",
    )
    assert got == [
        ("cds1", 0, 100, 200),
        ("g1", 0, 100, 900),
        ("t1", 0, 100, 900),
        ("cds1", 1, 800, 900),
    ]


def test_region_batched_ordering_is_total(db):
    """`(query_idx, start)` alone is not: two features sharing a start came
    back in whatever order the join produced, which differed between the
    logical and exploded forms of the same query."""
    for _ in range(5):
        assert _cols(db.region_batched([("chr1", 100, 900)]), "id") == [
            ("cds1",),
            ("g1",),
            ("t1",),
        ]


def test_exploding_only_returns_segments_that_overlap(db):
    """A query in the gap must return no segment of the fused feature -- the
    exploded form filters on the segment's own coordinates, not the
    envelope's."""
    got = _cols(db.region_batched([("chr1", 400, 500)], explode_segments=True), "id", "seg_idx")
    assert [row for row in got if row[0] == "cds1"] == []
    got = _cols(db.region_batched([("chr1", 850, 860)], explode_segments=True), "id", "seg_idx")
    assert ("cds1", 1) in got and ("cds1", 0) not in got


def test_children_batched_explodes(db):
    got = _cols(
        db.children_batched(["t1"], explode_segments=True), "descendant_id", "seg_idx", "start"
    )
    assert sorted(got) == [("cds1", 0, 100), ("cds1", 1, 800)]


def test_parents_batched_explodes(db):
    got = _cols(
        db.parents_batched(["cds1"], explode_segments=True), "descendant_id", "seg_idx", "start"
    )
    assert sorted(got) == [("g1", 0, 100), ("t1", 0, 100)]


def test_exploding_an_ordinary_database_just_adds_a_zero_column(tmp_path):
    """With nothing multipart, `segments_all` is `features`, so exploding is a
    no-op apart from the new column."""
    src = tmp_path / "plain.gff3"
    src.write_text(
        "##gff-version 3\n"
        "chr1\trs\tgene\t1\t99\t.\t+\t.\tID=g1\n"
        "chr1\trs\texon\t1\t50\t.\t+\t.\tID=e1;Parent=g1\n"
    )
    plain = create_db(str(src), ":memory:", mode="strict")
    logical = _cols(plain.region_batched([("chr1", 1, 99)]), "id", "start", "end")
    # Row for row, not merely as a set: the ordering must agree too.
    exploded = _cols(
        plain.region_batched([("chr1", 1, 99)], explode_segments=True),
        "id",
        "start",
        "end",
    )
    assert logical == exploded
    assert set(
        plain.region_batched([("chr1", 1, 99)], explode_segments=True)["seg_idx"].to_pylist()
    ) == {0}


@pytest.mark.parametrize("explode", [False, True])
def test_an_empty_result_has_the_same_columns_as_a_populated_one(db, explode):
    """Otherwise a caller reading `seg_idx` breaks precisely when the query
    matched nothing."""
    assert (
        db.region_batched([], explode_segments=explode).column_names
        == db.region_batched([("chr1", 100, 900)], explode_segments=explode).column_names
    )
    assert (
        db.children_batched([], explode_segments=explode).column_names
        == db.children_batched(["t1"], explode_segments=explode).column_names
    )


# ---------------------------------------------------------------------------
# delete() must reach `segments`
# ---------------------------------------------------------------------------


def test_deleting_a_feature_removes_its_segments(db):
    """`segments_all` joins orphaned segment rows straight back, so leaving
    them behind means every physical-level consumer keeps reporting lines of a
    feature the caller deleted."""
    assert db.conn.execute("SELECT COUNT(*) FROM segments").fetchone() == (2,)
    db.delete(["cds1"])
    assert db.conn.execute("SELECT COUNT(*) FROM segments").fetchone() == (0,)
    assert db.conn.execute(
        "SELECT COUNT(*) FROM segments_all WHERE feature_id = 'cds1'"
    ).fetchone() == (0,)


def test_a_deleted_feature_does_not_reappear_through_the_physical_view(db):
    db.delete(["cds1"])
    assert [f.id for f in db.all_features()] == ["g1", "t1"]
    assert _cols(db.region_batched([("chr1", 100, 900)], explode_segments=True), "id") == [
        ("g1",),
        ("t1",),
    ]


def test_deleting_an_ordinary_feature_still_works(db):
    db.delete(["g1"])
    assert "g1" not in [f.id for f in db.all_features()]
    assert db.conn.execute("SELECT COUNT(*) FROM segments").fetchone() == (2,)
