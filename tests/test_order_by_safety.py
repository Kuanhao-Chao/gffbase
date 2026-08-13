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
"""`order_by` is interpolated into SQL, so it has to be a whitelist.

This was a live SQL injection, not a theoretical one. gffbase interpolated any
unrecognized `order_by` verbatim "for power users", and DuckDB executes
trailing statements -- so

    db.all_features(order_by="start ASC; DROP TABLE attributes; SELECT ...")

dropped the table and returned rows as if nothing had happened.

gffutils has the identical interpolation and is not exploitable, because
SQLite's `execute()` refuses more than one statement. gffbase inherited the API
shape and lost the accidental protection when it changed backend. The tests
below run the payload that worked.

The whitelist also RESTORES the documented contract rather than narrowing it.
gffutils specifies that `order_by` items "must be in: 'seqid', 'source',
'featuretype', 'start', 'end', 'score', 'strand', 'frame', 'attributes',
'extra'" -- and of those, `attributes` and `extra` used to raise a
BinderException here, a tuple silently sorted by nothing, and a list raised
`TypeError: unhashable type`.
"""

from __future__ import annotations

import pytest
from gffbase import create_db
from gffbase.interface import _ORDER_BY_COLUMNS, FeatureDB

SRC = """##gff-version 3
chr2\trs\tgene\t50\t99\t.\t+\t.\tID=g2
chr1\trs\tgene\t10\t900\t.\t+\t.\tID=g1;Parent=g2
chr1\trs\tgene\t5\t9\t.\t+\t.\tID=g3;Parent=g2
"""


@pytest.fixture
def db(tmp_path):
    src = tmp_path / "in.gff3"
    src.write_text(SRC)
    return create_db(str(src), ":memory:")


def _tables(db) -> set[str]:
    return {r[0] for r in db.conn.execute("SELECT table_name FROM duckdb_tables()").fetchall()}


# ---------------------------------------------------------------------------
# The injection
# ---------------------------------------------------------------------------

#: The payload that worked. The trailing SELECT re-supplies the projection the
#: caller's generator expects, so the query "succeeds" and the damage is
#: invisible from the call site.
DROP_TABLE = (
    'start ASC; DROP TABLE attributes; SELECT id, seqid, source, featuretype, start, "end", '
    "score, strand, frame, attributes_blob, extra_blob, file_order FROM features ORDER BY start"
)


@pytest.mark.parametrize(
    "call",
    [
        pytest.param(lambda db, ob: list(db.all_features(order_by=ob)), id="all_features"),
        pytest.param(
            lambda db, ob: list(db.features_of_type("gene", order_by=ob)), id="features_of_type"
        ),
        pytest.param(lambda db, ob: list(db.children("g2", order_by=ob)), id="children"),
        pytest.param(lambda db, ob: list(db.parents("g1", order_by=ob)), id="parents"),
    ],
)
def test_no_entry_point_lets_a_second_statement_through(db, call):
    """Every path that takes `order_by` shares one resolver, so none of them
    can be the way around it -- the joined ones used to have their own copy of
    the pass-through."""
    before = _tables(db)
    with pytest.raises(ValueError, match="cannot order by"):
        call(db, DROP_TABLE)
    assert _tables(db) == before
    assert "attributes" in _tables(db)


@pytest.mark.parametrize(
    "payload",
    [
        "start ASC; DROP TABLE attributes",
        "start) ; DROP TABLE attributes; SELECT 1 FROM features ORDER BY (start",
        "(SELECT COUNT(*) FROM attributes)",
        "start --",
        "1; ATTACH ':memory:' AS evil",
        "f.seqid",
        "start DESC",
    ],
)
def test_every_shape_of_smuggled_sql_is_refused(db, payload):
    """Including the ones that merely READ -- a correlated subquery in an
    ORDER BY leaks data without changing anything, and `start DESC` shows that
    even a benign-looking fragment is not a column name."""
    with pytest.raises(ValueError, match="cannot order by"):
        list(db.all_features(order_by=payload))


def test_the_error_says_what_is_allowed(db):
    """A caller who hits this needs to know the accepted set, not just that
    theirs was rejected."""
    with pytest.raises(ValueError) as exc:
        list(db.all_features(order_by="nonsense"))
    message = str(exc.value)
    assert "'nonsense'" in message
    for name in ("seqid", "start", "featuretype", "length"):
        assert name in message


def test_a_non_string_order_by_is_a_type_error(db):
    with pytest.raises(TypeError, match="order_by must be"):
        list(db.all_features(order_by=42))


# ---------------------------------------------------------------------------
# The documented contract, which now actually works
# ---------------------------------------------------------------------------


def test_every_name_the_oracle_documents_is_accepted(db):
    """gffutils' docstring is the contract. `attributes` and `extra` used to
    raise a BinderException, because gffbase's columns are named
    `attributes_blob` / `extra_blob` and the name was passed through raw."""
    for name in (
        "seqid",
        "source",
        "featuretype",
        "start",
        "end",
        "score",
        "strand",
        "frame",
        "attributes",
        "extra",
    ):
        assert len(list(db.all_features(order_by=name))) == 3, name


def test_a_tuple_actually_sorts_now(db):
    """It used to be interpolated as a Python repr, which DuckDB parses as a
    constant struct -- so the query sorted by nothing and quietly returned file
    order. Sorting by (seqid, start) must put chr1:5 before chr1:10."""
    assert [f.id for f in db.all_features(order_by=("seqid", "start"))] == ["g3", "g1", "g2"]
    assert [f.id for f in db.all_features()] == ["g2", "g1", "g3"], "file order, for contrast"


def test_a_list_no_longer_raises_unhashable_type(db):
    assert [f.id for f in db.all_features(order_by=["seqid", "start"])] == ["g3", "g1", "g2"]


def test_a_comma_separated_string_is_accepted(db):
    assert [f.id for f in db.all_features(order_by="seqid, start")] == ["g3", "g1", "g2"]


def test_reverse_applies_to_every_key(db):
    """gffutils appends the direction once, which in SQL reverses only the last
    key. Multi-key sorting did not work here at all before, so there is no
    behaviour to preserve and copying that would be copying a defect."""
    assert FeatureDB._order_clause(("seqid", "start"), reverse=True) == ("seqid DESC, start DESC")
    assert [f.id for f in db.all_features(order_by=("seqid", "start"), reverse=True)] == [
        "g2",
        "g1",
        "g3",
    ]


def test_the_default_is_still_file_order(db):
    assert FeatureDB._order_clause(None, reverse=False) == "file_order ASC"
    assert [f.id for f in db.all_features()] == ["g2", "g1", "g3"]


def test_end_is_quoted_and_length_is_an_expression():
    """`end` is a SQL reserved word, and `length` is gffbase's own sort key
    rather than a column at all."""
    assert FeatureDB._order_clause("end", reverse=False) == '"end" ASC'
    assert FeatureDB._order_clause("length", reverse=False) == '("end" - start) ASC'
    assert FeatureDB._order_clause_qualified("length", False, "f") == '(f."end" - f.start) ASC'


def test_the_qualified_form_qualifies_every_key():
    assert (
        FeatureDB._order_clause_qualified(("seqid", "end"), False, "f")
        == 'f.seqid ASC, f."end" ASC'
    )


def test_id_is_accepted_although_the_oracle_omits_it(db):
    assert [f.id for f in db.all_features(order_by="id")] == ["g1", "g2", "g3"]


def test_every_whitelisted_name_produces_runnable_sql(db):
    """The whitelist maps names to SQL fragments; a typo in one of them would
    only surface when someone sorted by that particular column."""
    for name in _ORDER_BY_COLUMNS:
        assert len(list(db.all_features(order_by=name))) == 3, name
        assert len(list(db.children("g2", order_by=name))) == 2, name
