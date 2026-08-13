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
"""The SELECT projection and the positional unpack must never drift apart.

`feature_from_row` unpacks its row *positionally*. The column list used to be
written out three times independently -- once as a SQL string, once as a Python
list inside a second helper, and once as the tuple that drives the unpack. A
mismatch in any one of them does not raise: it shifts every field by one, so a
feature comes back with its `source` in `featuretype` and its `score` in
`strand`. Nothing in the suite would have caught that, because every field is a
string and every assertion downstream compares strings.

These tests pin the single-source-of-truth property directly, and then prove it
end-to-end by checking that a real query lands every column in the right
attribute -- including through the aliased (joined) form, which is the one that
was written out separately.
"""

from __future__ import annotations

import pytest
from gffbase import create_db
from gffbase.feature import _DB_ROW_FIELDS, db_row_projection, feature_from_row
from gffbase.interface import _SELECT_FEATURE, FeatureDB

# A row where every single column carries a *distinguishable* value. If the
# projection shifts by one, some field lands somewhere it does not belong and
# the assertion that names it fails.
_DISTINCT = "ctgA\tvendor\tmRNA\t11\t22\t3.5\t-\t2\tID=distinct;Parent=p1\n"


@pytest.fixture
def distinct_db(tmp_path):
    src = tmp_path / "distinct.gff3"
    src.write_text("##gff-version 3\nctgA\tvendor\tgene\t1\t99\t.\t-\t.\tID=p1\n" + _DISTINCT)
    return create_db(str(src), ":memory:")


def test_both_sql_forms_are_derived_from_the_canonical_field_list():
    """Not "kept in sync" -- actually generated from the one tuple."""
    assert _SELECT_FEATURE == db_row_projection()
    assert FeatureDB._select_feature_aliased("f") == db_row_projection("f")


def test_projection_column_count_matches_the_positional_unpack():
    """`feature_from_row` destructures a fixed-width tuple. If the projection
    and that tuple disagree on width, DuckDB happily returns the row and Python
    raises a bare ValueError from deep inside a generator.

    Checked behaviourally rather than by reading the source: a row of exactly
    `len(_DB_ROW_FIELDS)` must unpack, and one column narrower must not.
    """
    assert len(_SELECT_FEATURE.split(", ")) == len(_DB_ROW_FIELDS)

    row = ("f1", "chr1", "src", "gene", 1, 10, ".", "+", ".", b"ID=f1", None, 0)
    assert len(row) == len(_DB_ROW_FIELDS)
    assert feature_from_row(row).id == "f1"

    with pytest.raises(ValueError):
        feature_from_row(row[:-1])
    with pytest.raises(ValueError):
        feature_from_row((*row, "surplus"))


def test_reserved_words_are_quoted_in_both_forms():
    """`end` is a SQL reserved word. Unquoted, DuckDB rejects the query; the
    aliased form additionally has to place the alias *outside* the quotes."""
    assert '"end"' in db_row_projection()
    assert 'f."end"' in db_row_projection("f")
    assert '"f.end"' not in db_row_projection("f")


def test_aliased_form_qualifies_every_column():
    """A joined query with an unqualified column is an ambiguity error, and it
    only shows up on the join paths -- `children`, `parents`, `region_batched`."""
    aliased = db_row_projection("x").split(", ")
    assert len(aliased) == len(_DB_ROW_FIELDS)
    assert all(col.startswith("x.") for col in aliased)


def test_every_column_lands_in_the_right_attribute(distinct_db):
    """End-to-end proof for the unaliased projection."""
    f = distinct_db["distinct"]
    assert f.id == "distinct"
    assert f.seqid == "ctgA"
    assert f.source == "vendor"
    assert f.featuretype == "mRNA"
    assert f.start == 11
    assert f.end == 22
    assert f.score == "3.5"
    assert f.strand == "-"
    assert f.frame == "2"
    assert f.attributes["Parent"] == ["p1"]


def test_the_aliased_join_path_agrees_field_for_field(distinct_db):
    """`children()` reaches the same row through the *other* column list. The
    two lists were independent, so this is the comparison that would have caught
    a drift between them."""
    direct = distinct_db["distinct"]
    (via_join,) = list(distinct_db.children("p1"))
    for name in _DB_ROW_FIELDS:
        attr = "end" if name == "end" else name
        if name in ("attributes_blob", "extra_blob", "file_order"):
            continue
        assert getattr(via_join, attr) == getattr(direct, attr), f"{name} differs across paths"
    assert str(via_join) == str(direct)
