"""`schema.build_closure` builds the same closure the recursive CTE did.

The CTE is kept here as the oracle. It was replaced because its `UNION` held
the whole walk in one hash table DuckDB cannot spill, so a `memory_limit`
turned a large ingest into an OutOfMemoryException.
"""

from __future__ import annotations

import duckdb
import pytest
from gffbase.schema import build_closure
from hypothesis import given, settings
from hypothesis import strategies as st

ORACLE = """
INSERT INTO closure (ancestor, descendant, depth)
WITH RECURSIVE walk(ancestor, descendant, depth) AS (
    SELECT DISTINCT parent, child, 1 AS depth FROM edges
    UNION
    SELECT w.ancestor, e.child, w.depth + 1
    FROM walk w
    JOIN edges e ON e.parent = w.descendant
    WHERE w.depth < ? AND w.ancestor <> e.child
)
SELECT ancestor, descendant, MIN(depth) AS depth
FROM walk
GROUP BY ancestor, descendant
"""


def _closure(edges, max_depth, build):
    con = duckdb.connect()
    con.execute("CREATE TABLE edges (parent VARCHAR NOT NULL, child VARCHAR NOT NULL)")
    con.execute(
        "CREATE TABLE closure (ancestor VARCHAR NOT NULL, descendant VARCHAR NOT NULL, "
        "depth SMALLINT NOT NULL)"
    )
    if edges:
        con.executemany("INSERT INTO edges VALUES (?, ?)", edges)
    build(con, max_depth)
    rows = con.execute("SELECT ancestor, descendant, depth FROM closure").fetchall()
    assert len(rows) == len(set(rows)), "a pair appears twice"
    return sorted(rows)


def _oracle(con, max_depth):
    con.execute(ORACLE, [max_depth])


NODES = st.sampled_from(list("abcdefgh"))


@pytest.mark.property
@settings(max_examples=300, deadline=None)
@given(edges=st.lists(st.tuples(NODES, NODES), max_size=25), max_depth=st.integers(1, 6))
def test_levelwise_matches_the_recursive_cte(edges, max_depth):
    """Random graphs, cycles and self-edges included."""
    assert _closure(edges, max_depth, build_closure) == _closure(edges, max_depth, _oracle)


@pytest.mark.parametrize(
    ("edges", "max_depth"),
    [
        ([], 8),
        ([("g", "t"), ("t", "e1"), ("t", "e2")], 8),
        ([("g", "t"), ("t", "e")], 1),  # the cut-off
        ([("a", "b"), ("b", "c"), ("c", "a")], 8),  # a cycle
        ([("a", "a")], 8),  # a self-edge
        ([("a", "b"), ("a", "c"), ("b", "d"), ("c", "d"), ("d", "e")], 8),  # a diamond
    ],
)
def test_levelwise_matches_on_known_shapes(edges, max_depth):
    assert _closure(edges, max_depth, build_closure) == _closure(edges, max_depth, _oracle)


def test_the_staging_table_is_gone_afterwards():
    con = duckdb.connect()
    con.execute("CREATE TABLE edges (parent VARCHAR NOT NULL, child VARCHAR NOT NULL)")
    con.execute(
        "CREATE TABLE closure (ancestor VARCHAR NOT NULL, descendant VARCHAR NOT NULL, "
        "depth SMALLINT NOT NULL)"
    )
    con.execute("INSERT INTO edges VALUES ('a', 'b')")
    build_closure(con, 8)
    tables = {r[0] for r in con.execute("SELECT table_name FROM duckdb_tables()").fetchall()}
    assert "__closure_walk" not in tables
