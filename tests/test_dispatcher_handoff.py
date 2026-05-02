"""Relational dispatcher handoff tests.

GFFBase has two relational lookup paths:
  * **Closure cache** — materialized `(ancestor, descendant, depth)`
    rows up to ``meta.max_depth`` (default 8). Constant-time lookup.
  * **Dynamic CTE** — a recursive `WITH RECURSIVE walk(...)` that
    walks the `edges` table at query time. Used as the correctness
    fallback for traversals deeper than ``max_depth``.

The dispatcher (`FeatureDB._should_use_dynamic`) is supposed to pick
one based on the requested ``level``. These tests pin the contract
that **both paths return identical descendant sets** at the boundary,
so users walking deep hierarchies see no jump in semantics when the
fallback kicks in.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from gffbase import FeatureDB, create_db
from gffbase import ingest


def _deep_chain_lines(depth: int) -> str:
    """Linear chain: g → t1 → t2 → ... → tN → exon. Hierarchy depth
    = ``depth`` (the gene-to-exon distance)."""
    lines = ["##gff-version 3\n"]
    lines.append("chr1\trs\tgene\t1\t1000\t.\t+\t.\tID=g\n")
    parent = "g"
    for i in range(1, depth):
        nid = f"t{i}"
        lines.append(
            f"chr1\trs\tmRNA\t1\t1000\t.\t+\t.\tID={nid};Parent={parent}\n"
        )
        parent = nid
    # Leaf
    lines.append(
        f"chr1\trs\texon\t100\t200\t.\t+\t.\tID=leaf;Parent={parent}\n"
    )
    return "".join(lines)


@pytest.fixture
def deep_db_max2(tmp_path):
    """A 6-deep linear hierarchy ingested with ``max_depth=2``. Levels
    1 and 2 are in the closure cache; levels 3-6 must fall through
    to the dynamic CTE."""
    src = tmp_path / "deep.gff3"
    src.write_text(_deep_chain_lines(depth=6))
    out = tmp_path / "deep.duckdb"
    con, _stats = ingest.from_file(
        str(src), dbfn=str(out), force=True, max_depth=2,
    )
    con.close()
    db = FeatureDB(str(out))
    # Pre-condition: the materialized closure tops out at 2.
    closure_top = db.execute("SELECT MAX(depth) FROM closure").fetchone()[0]
    assert closure_top == 2
    return db


# ---------------------------------------------------------------------------
# 1. Per-level dispatcher decision
# ---------------------------------------------------------------------------


def test_dispatcher_picks_cache_within_max_depth(deep_db_max2):
    """`level <= max_depth` MUST go through the materialized closure
    cache (the constant-time path)."""
    assert deep_db_max2._dispatch_relation(
        level=1, target_id="g", direction="children"
    ) is False
    assert deep_db_max2._dispatch_relation(
        level=2, target_id="g", direction="children"
    ) is False


def test_dispatcher_picks_dynamic_past_max_depth(deep_db_max2):
    """`level > max_depth` MUST go through the dynamic CTE (the
    deeper-than-cache fallback)."""
    for lvl in (3, 5, 6):
        assert deep_db_max2._dispatch_relation(
            level=lvl, target_id="g", direction="children"
        ) is True


def test_dispatcher_level_none_dynamic_when_overflow(deep_db_max2):
    """`level=None` (full descendant set) goes dynamic when the
    actual hierarchy depth exceeds the materialized cache."""
    # The chain runs 6 deep; the cache only knows 2 levels; the
    # dispatcher must detect the overflow and fall through.
    assert deep_db_max2._dispatch_relation(
        level=None, target_id="g", direction="children"
    ) is True


# ---------------------------------------------------------------------------
# 2. Identical-result handoff: both paths return the same ids
# ---------------------------------------------------------------------------


def test_cache_and_dynamic_return_same_descendants_at_level_2(deep_db_max2):
    """Level 2 fits in the closure cache. Force the dynamic path by
    flipping ``_max_depth`` to 0 (a stand-in for "the cache wasn't
    populated for this depth"). The two paths must return identical
    id sets — *that's the seamless-handoff guarantee*. Because the
    chain is g → t1 → t2 → … → leaf, level=2 returns the single
    feature at depth 2 below g, which is `t2`."""
    cache_ids = sorted(f.id for f in deep_db_max2.children("g", level=2))
    # Flip the dispatcher to dynamic for this same query.
    deep_db_max2._closure_max_depth = 0   # forces use_dynamic for level=None
    deep_db_max2._max_depth = 0           # forces use_dynamic for level >= 1
    dynamic_ids = sorted(f.id for f in deep_db_max2.children("g", level=2))
    assert cache_ids == dynamic_ids
    assert cache_ids == ["t2"]


def test_cache_and_dynamic_return_same_full_descendants(tmp_path):
    """Same hierarchy ingested twice — once with `max_depth=8`
    (everything fits in the cache), once with `max_depth=2` (levels
    3-5 force the dynamic fallback). Full descendant set must match.
    """
    chain = _deep_chain_lines(depth=5)
    src_a = tmp_path / "deep_a.gff3"
    src_b = tmp_path / "deep_b.gff3"
    src_a.write_text(chain)
    src_b.write_text(chain)

    con_a, _ = ingest.from_file(
        str(src_a), dbfn=str(tmp_path / "a.duckdb"), force=True, max_depth=8,
    )
    con_a.close()
    con_b, _ = ingest.from_file(
        str(src_b), dbfn=str(tmp_path / "b.duckdb"), force=True, max_depth=2,
    )
    con_b.close()

    db_full_cache = FeatureDB(str(tmp_path / "a.duckdb"))
    db_partial_cache = FeatureDB(str(tmp_path / "b.duckdb"))

    full = sorted(f.id for f in db_full_cache.children("g", level=None))
    partial = sorted(f.id for f in db_partial_cache.children("g", level=None))

    assert full == partial
    # Sanity: the chain depth is 5, so 5 descendants total.
    assert full == ["leaf", "t1", "t2", "t3", "t4"]


# ---------------------------------------------------------------------------
# 3. Direct boundary-level handoff: level == max_depth ↔ level == max_depth+1
# ---------------------------------------------------------------------------


def test_boundary_level_n_vs_n_plus_one(deep_db_max2):
    """Right on the cache/dynamic boundary: level=max_depth uses
    cache, level=max_depth+1 uses dynamic. Both must return the
    correct (single-feature) result for the next link in the chain.

    The chain is g → t1 → t2 → t3 → t4 → t5 → leaf. children(g,
    level=N) is whichever single node sits at depth N below g."""
    assert sorted(f.id for f in deep_db_max2.children("g", level=1)) == ["t1"]
    assert sorted(f.id for f in deep_db_max2.children("g", level=2)) == ["t2"]
    # Cross the max_depth=2 boundary into the dynamic CTE path:
    assert sorted(f.id for f in deep_db_max2.children("g", level=3)) == ["t3"]
    assert sorted(f.id for f in deep_db_max2.children("g", level=4)) == ["t4"]
    assert sorted(f.id for f in deep_db_max2.children("g", level=5)) == ["t5"]
    assert sorted(f.id for f in deep_db_max2.children("g", level=6)) == ["leaf"]


# ---------------------------------------------------------------------------
# 4. Batched API also handles the boundary correctly
# ---------------------------------------------------------------------------


def test_children_batched_explicit_level_crosses_boundary(deep_db_max2):
    """`children_batched(level=N)` flips to dynamic when N >
    max_depth. With max_depth=2 and the chain g → … → leaf:

    * level=2 (cache path) returns the single node at depth 2 (`t2`).
    * level=4 (dynamic path) returns `t4`.

    Both paths must return the correct single-feature result.
    """
    table_cache = deep_db_max2.children_batched(["g"], level=2, format="arrow")
    assert set(table_cache.column("descendant_id").to_pylist()) == {"t2"}
    table_dyn = deep_db_max2.children_batched(["g"], level=4, format="arrow")
    assert set(table_dyn.column("descendant_id").to_pylist()) == {"t4"}


def test_children_batched_level_within_cache(deep_db_max2):
    """Batched children at `level=1` is served by the closure cache;
    just the immediate child should be returned."""
    table = deep_db_max2.children_batched(["g"], level=1, format="arrow")
    descendants = set(table.column("descendant_id").to_pylist())
    assert descendants == {"t1"}


# ---------------------------------------------------------------------------
# 5. Parents direction also honors the dispatcher
# ---------------------------------------------------------------------------


def test_parents_full_walk_through_dynamic(deep_db_max2):
    """Walk *up* from `leaf` — the chain length 6 forces the
    dynamic CTE, and the result must include every ancestor."""
    ancestors = sorted(f.id for f in deep_db_max2.parents("leaf", level=None))
    assert ancestors == ["g", "t1", "t2", "t3", "t4", "t5"]
