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
"""Boundary tests for the vectorized API surface.

* Empty input lists ⇒ empty PyArrow table with the documented schema.
* Massively large input lists (10 000+ ids) ⇒ same schema, no
  per-row Python `Feature` objects materialized.
* Invalid / unknown feature IDs ⇒ no crash; absent from output.
* Mixed valid + invalid in one call ⇒ valid IDs return their
  descendants, invalid IDs simply don't appear in `anchor`.
* `format` round-trip: arrow ↔ df ↔ polars all preserve the schema
  shape, even on empty inputs.

These exercise the `children_batched`, `parents_batched`, and
`region_batched` paths.
"""

from __future__ import annotations

import pyarrow as pa
import pytest
from gffbase import create_db

CHILDREN_SCHEMA = [
    "anchor",
    "descendant_id",
    "seqid",
    "source",
    "featuretype",
    "start",
    "end",
    "score",
    "strand",
    "frame",
    "file_order",
    "depth",
]

REGION_SCHEMA = [
    "query_idx",
    "query_seqid",
    "query_start",
    "query_end",
    "id",
    "seqid",
    "source",
    "featuretype",
    "start",
    "end",
    "score",
    "strand",
    "frame",
    "file_order",
]


@pytest.fixture
def db(tmp_path):
    """A small but real hierarchy: 1 gene → 2 transcripts → 3 exons each."""
    src = tmp_path / "tiny.gff3"
    lines = ["##gff-version 3\n"]
    lines.append("chr1\trs\tgene\t1\t10000\t.\t+\t.\tID=gA\n")
    for tx in ("tA1", "tA2"):
        lines.append(f"chr1\trs\tmRNA\t1\t10000\t.\t+\t.\tID={tx};Parent=gA\n")
        for i, (s, e) in enumerate([(100, 200), (300, 400), (500, 600)]):
            lines.append(f"chr1\trs\texon\t{s}\t{e}\t.\t+\t.\tID={tx}_e{i};Parent={tx}\n")
    src.write_text("".join(lines))
    return create_db(str(src), str(tmp_path / "tiny.duckdb"), force=True)


# ---------------------------------------------------------------------------
# Empty-input invariants
# ---------------------------------------------------------------------------


def test_children_batched_empty_list_returns_typed_empty_table(db):
    """`children_batched([])` must return an empty `pyarrow.Table`
    with the documented schema — *not* `None`, *not* a runtime error.
    Downstream `concat` / `groupby` code stays correct."""
    result = db.children_batched([], format="arrow")
    assert isinstance(result, pa.Table)
    assert result.num_rows == 0
    assert result.column_names == CHILDREN_SCHEMA


def test_parents_batched_empty_list_returns_typed_empty_table(db):
    result = db.parents_batched([], format="arrow")
    assert isinstance(result, pa.Table)
    assert result.num_rows == 0
    assert result.column_names == CHILDREN_SCHEMA


def test_region_batched_empty_list_returns_typed_empty_table(db):
    result = db.region_batched([], format="arrow")
    assert isinstance(result, pa.Table)
    assert result.num_rows == 0
    assert result.column_names == REGION_SCHEMA


def test_empty_inputs_round_trip_through_df_format(db):
    """Format='df' on an empty input still has the right column
    layout (so `df.empty` works without a `KeyError`)."""
    pytest.importorskip("pandas")
    df = db.children_batched([], format="df")
    # pandas DataFrame
    assert df.empty
    assert list(df.columns) == CHILDREN_SCHEMA


def test_empty_inputs_keep_schema_for_region(db):
    pytest.importorskip("pandas")
    df = db.region_batched([], format="df")
    assert df.empty
    assert list(df.columns) == REGION_SCHEMA


# ---------------------------------------------------------------------------
# Invalid / unknown IDs
# ---------------------------------------------------------------------------


def test_children_batched_unknown_id_yields_no_rows(db):
    """Unknown anchor ID — no descendants in the closure — must
    silently produce zero rows for that anchor (no crash, no
    KeyError)."""
    result = db.children_batched(["does_not_exist"], format="arrow")
    assert result.num_rows == 0
    assert result.column_names == CHILDREN_SCHEMA


def test_children_batched_mixed_valid_and_invalid_ids(db):
    """Mix of real and bogus ids: only the real one contributes
    rows; the bogus one is filtered out by the closure JOIN."""
    result = db.children_batched(["gA", "ghost", "another_phantom"], format="arrow")
    anchors = set(result.column("anchor").to_pylist())
    assert anchors == {"gA"}
    # gA has 2 transcripts × 3 exons + 2 transcripts = 8 descendants.
    assert result.num_rows == 8


def test_parents_batched_unknown_id_yields_no_rows(db):
    result = db.parents_batched(["does_not_exist"], format="arrow")
    assert result.num_rows == 0


def test_region_batched_seqid_not_in_db_yields_no_rows(db):
    """Querying a chromosome that doesn't exist in the DB returns
    an empty table with the right schema."""
    result = db.region_batched([("chrZZ", 100, 200)], format="arrow")
    assert result.num_rows == 0
    assert result.column_names == REGION_SCHEMA


# ---------------------------------------------------------------------------
# Massively large input lists
# ---------------------------------------------------------------------------


def test_children_batched_large_id_list_still_one_query(db):
    """10 000 anchor IDs (most of which are invalid) in a single
    call. The `IN (?, ?, …, ?)` clause has to handle it without
    triggering a per-id Python loop."""
    ids = ["gA"] + [f"phantom_{i}" for i in range(10_000)]
    result = db.children_batched(ids, format="arrow")
    # The 8 real descendants of gA show up; the 10 000 phantoms
    # contribute zero.
    assert result.num_rows == 8
    assert set(result.column("anchor").to_pylist()) == {"gA"}


def test_region_batched_large_region_list(db):
    """10 000 random regions (all on chrZZ which doesn't exist) +
    one real region. The single overlapping region's row is
    returned; the other 10 000 contribute zero."""
    regions = [("chrZZ", i * 100, i * 100 + 50) for i in range(10_000)]
    regions.append(("chr1", 1, 600))  # captures everything
    result = db.region_batched(regions, format="arrow")
    # Last query (idx == 10000) is the real one; it should hit the
    # gene + 2 mRNA + 6 exons = 9 features.
    assert result.num_rows >= 1
    real_query_idx = 10_000
    real_rows = result.filter(pa.compute.equal(result.column("query_idx"), real_query_idx))
    assert real_rows.num_rows == 9


# ---------------------------------------------------------------------------
# Format round-trip on non-empty inputs
# ---------------------------------------------------------------------------


def test_children_batched_arrow_df_polars_consistent(db):
    """All three return shapes carry the same row count + same column
    names for the same query."""
    pytest.importorskip("pandas")
    arrow_t = db.children_batched(["gA"], format="arrow")
    df = db.children_batched(["gA"], format="df")
    assert arrow_t.num_rows == len(df)
    assert list(df.columns) == CHILDREN_SCHEMA == arrow_t.column_names

    pytest.importorskip("polars")
    plframe = db.children_batched(["gA"], format="polars")
    assert plframe.shape[0] == arrow_t.num_rows
    assert list(plframe.columns) == CHILDREN_SCHEMA


def test_invalid_format_raises_value_error(db):
    """Anything other than arrow/df/polars is a programmer error."""
    with pytest.raises(ValueError):
        db.children_batched(["gA"], format="parquet")


# ---------------------------------------------------------------------------
# Anchor-column groupby contract
# ---------------------------------------------------------------------------


def test_anchor_column_lets_caller_groupby_without_reissuing(db):
    """The whole point of the batched API is to issue ONE query and
    reconstruct per-id results in user code via the `anchor` column.
    This test pins the contract that anchor values match the input
    ids exactly."""
    ids = ["gA", "tA1", "tA2"]
    table = db.children_batched(ids, format="arrow")
    seen_anchors = set(table.column("anchor").to_pylist())
    assert seen_anchors == {"gA", "tA1", "tA2"}
    # Counts per anchor.
    counts = {
        a: int(table.filter(pa.compute.equal(table.column("anchor"), a)).num_rows) for a in ids
    }
    # gA: 2 mRNA + 6 exons = 8
    # tA1: 3 exons
    # tA2: 3 exons
    assert counts["gA"] == 8
    assert counts["tA1"] == 3
    assert counts["tA2"] == 3
