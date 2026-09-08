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
"""Row order must be total, and `score` must sort as a number.

Two defects, one theme: a query that returns the right SET of features in an
order the caller cannot rely on, or in an order that is confidently wrong.

**Order was not total.** `region()` and the scan path ended in `ORDER BY start`
and `ORDER BY file_order` respectively. Neither is unique -- features share a
start constantly, and GTF synthesis stamps a synthesized parent with
`MIN(file_order)` of its children, which is a value a real child already holds.
DuckDB's sort is parallel and does not preserve ties, so identical queries over
identical data returned different orders across runs. For a pipeline that
writes its first N results to a file, that is a reproducibility bug.

`region_batched` already solved this and says why at its ORDER BY: `(query_idx,
start)` is not a total order, so `id` is appended. The row-by-row twins never
got the same treatment.

**`score` sorted as text.** The column is VARCHAR, because GFF3 permits `.`
and the oracle stores it as text. So `order_by="score"` compared strings:
`10 < 100 < 1e3 < 2.5 < 9`. A caller asking for the highest-scoring features
got a plausible-looking, wrong answer with nothing raised.
"""

from __future__ import annotations

import pytest
from gffbase import create_db

# Every feature shares start=100, so ONLY a tiebreak can make the order total.
TIED_STARTS = "##gff-version 3\n" + "".join(
    f"chr1\trs\texon\t100\t200\t.\t+\t.\tID=e{i:03d}\n" for i in range(60)
)

SCORED = "##gff-version 3\n" + "".join(
    f"chr1\trs\tgene\t{10 * i + 1}\t{10 * i + 9}\t{s}\t+\t.\tID=g{i}\n"
    for i, s in enumerate(["9", "10", "2.5", "100", ".", "1e3", "-4"])
)


@pytest.fixture
def tied_db(tmp_path):
    src = tmp_path / "tied.gff3"
    src.write_text(TIED_STARTS)
    return create_db(str(src), str(tmp_path / "tied.duckdb"), force=True)


@pytest.fixture
def scored_db(tmp_path):
    src = tmp_path / "scored.gff3"
    src.write_text(SCORED)
    return create_db(str(src), str(tmp_path / "scored.duckdb"), force=True)


def test_region_returns_a_stable_order_across_repeated_calls(tied_db):
    """The same query, run repeatedly, must not shuffle."""
    runs = [[f.id for f in tied_db.region(seqid="chr1", start=1, end=1000)] for _ in range(8)]
    assert len({tuple(r) for r in runs}) == 1, "region() row order is not deterministic"


def test_all_features_returns_a_stable_order_across_repeated_calls(tied_db):
    runs = [[f.id for f in tied_db.all_features()] for _ in range(8)]
    assert len({tuple(r) for r in runs}) == 1, "all_features() row order is not deterministic"


def test_the_two_index_paths_agree_on_order_not_just_membership(tied_db):
    """A caller who loses the R-tree must not get a different ORDER either."""
    rtree = [f.id for f in tied_db.region(seqid="chr1", start=1, end=1000)]
    tied_db._rtree_built = False
    try:
        btree = [f.id for f in tied_db.region(seqid="chr1", start=1, end=1000)]
    finally:
        tied_db._rtree_built = True
    assert rtree == btree


def test_score_sorts_numerically_not_lexicographically(scored_db):
    """`10` must not sort before `9`."""
    ordered = [f.score for f in scored_db.features_of_type("gene", order_by="score")]
    numeric = [float(s) for s in ordered if s != "."]
    assert numeric == sorted(numeric), f"score sorted as text: {ordered}"


def test_an_unscored_feature_sorts_last(scored_db):
    """`.` means "no score", so it belongs at the end, not wherever `.` falls
    in the collation."""
    ordered = [f.score for f in scored_db.features_of_type("gene", order_by="score")]
    assert ordered[-1] == ".", ordered


def test_score_descending_puts_the_highest_first(scored_db):
    ordered = [f.score for f in scored_db.features_of_type("gene", order_by="score", reverse=True)]
    assert ordered[0] == "1e3", ordered


# ---------------------------------------------------------------------------
# The order is pinned at the SQL level, not only behaviourally
# ---------------------------------------------------------------------------
#
# The behavioural tests above pass even on the broken code: DuckDB only
# reorders ties once the sort goes parallel, which needs far more rows than a
# unit-test fixture should carry. A test that cannot fail is not a test, so the
# property itself -- "every ORDER BY this module emits ends in a unique
# column" -- is asserted directly against the generated SQL.


def test_the_default_order_clause_ends_in_a_unique_column():
    from gffbase.interface import _order_clause

    assert _order_clause(None, False).endswith("id ASC")


@pytest.mark.parametrize(
    "order_by", ["start", "score", "featuretype", "file_order", "length", ("seqid", "start")]
)
def test_every_order_clause_ends_in_a_unique_column(order_by):
    from gffbase.interface import _order_clause

    assert _order_clause(order_by, False).endswith("id ASC"), order_by
    assert _order_clause(order_by, True).endswith("id ASC"), "reverse must not flip the tiebreak"


def test_sorting_by_id_does_not_append_a_redundant_tiebreak():
    from gffbase.interface import _order_clause

    assert _order_clause("id", False).count("id") == 1


@pytest.mark.parametrize("completely_within", [False, True])
def test_both_region_sql_paths_end_in_a_unique_column(tied_db, completely_within):
    """Covers the hand-written ORDER BY literals, which `_order_clause` never
    sees -- they are the ones that were actually wrong."""
    for builder in (tied_db._region_sql_rtree, tied_db._region_sql_btree):
        sql, _ = builder("chr1", 1, 1000, None, None, completely_within)
        order_by = sql[sql.rindex("ORDER BY") :]
        assert order_by.rstrip().endswith("id"), (builder.__name__, order_by)
