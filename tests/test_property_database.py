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
"""Generated database oracles for hierarchy and spatial query routing.

These examples are intentionally small. Hypothesis varies topology,
coordinates, and query boundaries while a plain-Python oracle supplies the
expected answer independently of DuckDB's closure and index implementations.
"""

from __future__ import annotations

from collections import defaultdict, deque
from pathlib import Path
from tempfile import TemporaryDirectory

import pytest
from gffbase import Feature, create_db
from hypothesis import example, given, settings
from hypothesis import strategies as st
from hypothesis.stateful import RuleBasedStateMachine, invariant, rule

pytestmark = pytest.mark.property


@st.composite
def _dag_cases(draw):
    """A rooted acyclic hierarchy plus inclusive region queries."""

    count = draw(st.integers(min_value=1, max_value=10))
    parents: list[tuple[int, ...]] = [()]
    for child in range(1, count):
        # Every parent precedes its child, which makes cycles impossible
        # without sharing production's graph implementation. A set (rather
        # than one scalar parent) generates diamonds, alternate paths, and
        # genuine GFF3 multi-parent features.
        selected = draw(
            st.sets(
                st.integers(min_value=0, max_value=child - 1),
                min_size=0,
                max_size=min(3, child),
            )
        )
        parents.append(tuple(sorted(selected)))

    intervals = []
    for _ in range(count):
        start = draw(st.integers(min_value=1, max_value=250))
        width = draw(st.integers(min_value=0, max_value=80))
        intervals.append((start, start + width))

    queries = draw(
        st.lists(
            st.tuples(
                st.integers(min_value=1, max_value=300),
                st.integers(min_value=1, max_value=300),
            ).map(lambda pair: (min(pair), max(pair))),
            min_size=1,
            max_size=8,
        )
    )
    return parents, intervals, queries


def _shortest_paths(direct: dict[int, set[int]], anchor: int) -> dict[int, int]:
    """Independent breadth-first minimum depths from one anchor."""
    found: dict[int, int] = {}
    frontier = deque((child, 1) for child in sorted(direct.get(anchor, set())))
    while frontier:
        descendant, depth = frontier.popleft()
        if descendant in found:
            continue
        found[descendant] = depth
        frontier.extend((child, depth + 1) for child in sorted(direct.get(descendant, set())))
    return found


def _descendants(parents: list[tuple[int, ...]], anchor: int) -> dict[int, int]:
    direct: dict[int, set[int]] = defaultdict(set)
    for child, child_parents in enumerate(parents):
        for parent in child_parents:
            direct[parent].add(child)
    return _shortest_paths(direct, anchor)


def _ancestors(parents: list[tuple[int, ...]], anchor: int) -> dict[int, int]:
    reverse: dict[int, set[int]] = defaultdict(set)
    for child, child_parents in enumerate(parents):
        reverse[child].update(child_parents)
    return _shortest_paths(reverse, anchor)


@example(
    (
        [(), (0,), (0,), (0, 1, 2)],
        [(1, 10), (11, 20), (21, 30), (31, 40)],
        [(1, 40), (15, 35)],
    )
)
@example(
    (
        [(), (0,), (0,), (1, 2)],
        [(1, 100), (10, 20), (30, 40), (15, 35)],
        [(1, 1), (20, 30)],
    )
)
@given(_dag_cases())
def test_generated_dag_matches_relationship_and_region_oracles(case):
    parents, intervals, queries = case
    lines = ["##gff-version 3\n"]
    for idx, ((start, end), child_parents) in enumerate(zip(intervals, parents, strict=True)):
        attrs = f"ID=f{idx}"
        if child_parents:
            attrs += ";Parent=" + ",".join(f"f{parent}" for parent in child_parents)
        featuretype = "gene" if not child_parents else "exon"
        lines.append(f"chr1\tproperty\t{featuretype}\t{start}\t{end}\t.\t+\t.\t{attrs}\n")

    with TemporaryDirectory(prefix="gffbase-property-") as scratch:
        source = Path(scratch) / "generated.gff3"
        source.write_text("".join(lines), encoding="utf-8")
        with create_db(str(source), ":memory:") as db:
            expected_children = {
                f"f{idx}": {
                    f"f{child}": depth for child, depth in _descendants(parents, idx).items()
                }
                for idx in range(len(parents))
            }
            expected_parents = {
                f"f{idx}": {
                    f"f{parent}": depth for parent, depth in _ancestors(parents, idx).items()
                }
                for idx in range(len(parents))
            }

            for idx in range(len(parents)):
                anchor = f"f{idx}"
                scalar_children = {child.id for child in db.children(anchor, level=None)}
                scalar_parents = {parent.id for parent in db.parents(anchor, level=None)}
                assert scalar_children == set(expected_children[anchor])
                assert scalar_parents == set(expected_parents[anchor])
                for depth in range(1, len(parents)):
                    assert {child.id for child in db.children(anchor, level=depth)} == {
                        child
                        for child, expected_depth in expected_children[anchor].items()
                        if expected_depth == depth
                    }
                    assert {parent.id for parent in db.parents(anchor, level=depth)} == {
                        parent
                        for parent, expected_depth in expected_parents[anchor].items()
                        if expected_depth == depth
                    }

            anchors = list(expected_children)
            child_rows = db.children_batched(anchors, level=None).to_pylist()
            parent_rows = db.parents_batched(anchors, level=None).to_pylist()
            actual_children: dict[str, dict[str, int]] = defaultdict(dict)
            actual_parents: dict[str, dict[str, int]] = defaultdict(dict)
            for row in child_rows:
                actual_children[row["anchor"]][row["descendant_id"]] = row["depth"]
            for row in parent_rows:
                actual_parents[row["anchor"]][row["descendant_id"]] = row["depth"]
            assert {anchor: actual_children[anchor] for anchor in anchors} == expected_children
            assert {anchor: actual_parents[anchor] for anchor in anchors} == expected_parents

            expected_regions = {
                query_idx: {
                    f"f{idx}"
                    for idx, (feature_start, feature_end) in enumerate(intervals)
                    if feature_start <= query_end and feature_end >= query_start
                }
                for query_idx, (query_start, query_end) in enumerate(queries)
            }
            region_args = [("chr1", start, end) for start, end in queries]
            indexed_rows = db.region_batched(region_args).to_pylist()
            indexed: dict[int, set[str]] = defaultdict(set)
            for row in indexed_rows:
                indexed[row["query_idx"]].add(row["id"])
            assert {idx: indexed[idx] for idx in expected_regions} == expected_regions

            original_rtree = db._rtree_built
            db._rtree_built = False
            try:
                fallback_rows = db.region_batched(region_args).to_pylist()
            finally:
                db._rtree_built = original_rtree
            fallback: dict[int, set[str]] = defaultdict(set)
            for row in fallback_rows:
                fallback[row["query_idx"]].add(row["id"])
            assert {idx: fallback[idx] for idx in expected_regions} == expected_regions

            assert db.validate(level="full").ok


def _expected_closure(edges: set[tuple[str, str]]) -> set[tuple[str, str, int]]:
    direct: dict[str, set[str]] = defaultdict(set)
    for parent, child in edges:
        direct[parent].add(child)

    closure: set[tuple[str, str, int]] = set()
    for ancestor in direct:
        found: dict[str, int] = {}
        frontier = deque((child, 1) for child in sorted(direct[ancestor]))
        while frontier:
            descendant, depth = frontier.popleft()
            if descendant in found:
                continue
            found[descendant] = depth
            frontier.extend((child, depth + 1) for child in sorted(direct.get(descendant, set())))
        closure.update((ancestor, descendant, depth) for descendant, depth in found.items())
    return closure


def test_closure_oracle_uses_only_the_shortest_alternate_path():
    """A direct edge wins over a longer route to the same descendant."""
    edges = {("a", "b"), ("a", "c"), ("c", "b")}
    assert _expected_closure(edges) == {
        ("a", "b", 1),
        ("a", "c", 1),
        ("c", "b", 1),
    }


def test_forced_multipart_and_duplicate_id_shapes(tmp_path: Path):
    """Pin the graph-shaping ingest cases random DAG rows cannot express."""
    multipart = tmp_path / "multipart.gff3"
    multipart.write_text(
        "##gff-version 3\n"
        "chr1\tp\tgene\t1\t100\t.\t+\t.\tID=g\n"
        "chr1\tp\tmRNA\t1\t100\t.\t+\t.\tID=t;Parent=g\n"
        "chr1\tp\tCDS\t10\t20\t.\t+\t0\tID=cds;Parent=t\n"
        "chr1\tp\tCDS\t30\t40\t.\t+\t2\tID=cds;Parent=t\n",
        encoding="utf-8",
    )
    with create_db(str(multipart), ":memory:", mode="strict") as db:
        assert db.conn.execute("SELECT n_segments FROM features WHERE id = 'cds'").fetchone() == (
            2,
        )
        assert db.conn.execute(
            'SELECT start, "end", frame FROM segments WHERE feature_id = ? ORDER BY seg_idx',
            ["cds"],
        ).fetchall() == [(10, 20, "0"), (30, 40, "2")]
        assert db.conn.execute(
            "SELECT ancestor, descendant, depth FROM closure ORDER BY 1, 2"
        ).fetchall() == [("g", "cds", 2), ("g", "t", 1), ("t", "cds", 1)]
        assert db.validate(level="full").ok

    duplicate = tmp_path / "duplicate.gff3"
    duplicate.write_text(
        "##gff-version 3\n"
        "chr1\tp\tgene\t1\t10\t.\t+\t.\tID=dup\n"
        "chr1\tp\tgene\t20\t30\t.\t+\t.\tID=dup\n",
        encoding="utf-8",
    )
    with create_db(str(duplicate), ":memory:", merge_strategy="create_unique") as db:
        assert db.conn.execute(
            "SELECT id, raw_id, occ FROM features ORDER BY file_order"
        ).fetchall() == [
            ("dup", "dup", 0),
            ("dup_1", "dup", 1),
        ]
        # This table is reserved for merge->create_unique fallback. Explicit
        # create_unique provenance lives on (raw_id, occ), matching gffutils.
        assert db.conn.execute("SELECT COUNT(*) FROM duplicates").fetchone() == (0,)
        assert db.validate(level="full").ok


class DatabaseMutationMachine(RuleBasedStateMachine):
    """A bounded model for public updates, deletes, relations, and rollback."""

    def __init__(self):
        super().__init__()
        self.db = create_db(
            "##gff-version 3\nchr1\tproperty\tgene\t1\t10\t.\t+\t.\tID=f0\n",
            ":memory:",
            from_string=True,
        )
        self.active = {"f0"}
        self.edges: set[tuple[str, str]] = set()
        self.next_id = 1
        self.rollback_id = 10_000

    @rule(start=st.integers(min_value=1, max_value=500))
    def append_feature(self, start):
        if len(self.active) >= 8:
            return
        feature_id = f"f{self.next_id}"
        self.next_id += 1
        self.db.update(
            [
                Feature(
                    seqid="chr1",
                    source="property",
                    featuretype="gene",
                    start=start,
                    end=start + 9,
                    strand="+",
                    id=feature_id,
                    attributes={"ID": [feature_id]},
                )
            ]
        )
        self.active.add(feature_id)

    @rule(choice=st.integers(min_value=0, max_value=100))
    def delete_feature(self, choice):
        candidates = sorted(self.active - {"f0"})
        if not candidates:
            return
        feature_id = candidates[choice % len(candidates)]
        self.db.delete(feature_id)
        self.active.remove(feature_id)
        self.edges = {edge for edge in self.edges if feature_id not in edge}

    @rule(
        left=st.integers(min_value=0, max_value=100),
        right=st.integers(min_value=0, max_value=100),
    )
    def add_acyclic_relation(self, left, right):
        ordered = sorted(self.active, key=lambda value: int(value[1:]))
        if len(ordered) < 2:
            return
        first, second = ordered[left % len(ordered)], ordered[right % len(ordered)]
        if first == second:
            return
        parent, child = sorted((first, second), key=lambda value: int(value[1:]))
        edge = (parent, child)
        if edge in self.edges:
            return
        self.db.add_relation(parent, child)
        self.edges.add(edge)

    @rule(start=st.integers(min_value=1, max_value=500))
    def rolled_back_update_is_invisible(self, start):
        feature_id = f"rollback_{self.rollback_id}"
        self.rollback_id += 1
        self.db.conn.execute("BEGIN TRANSACTION")
        try:
            self.db.update(
                [
                    Feature(
                        seqid="chr1",
                        source="property",
                        featuretype="gene",
                        start=start,
                        end=start + 1,
                        strand="+",
                        id=feature_id,
                        attributes={"ID": [feature_id]},
                    )
                ]
            )
        finally:
            self.db.conn.execute("ROLLBACK")
        assert (
            self.db.conn.execute(
                "SELECT COUNT(*) FROM features WHERE id = ?", [feature_id]
            ).fetchone()[0]
            == 0
        )

    @invariant()
    def database_matches_the_independent_model(self):
        actual_ids = {row[0] for row in self.db.conn.execute("SELECT id FROM features").fetchall()}
        actual_edges = {
            tuple(row) for row in self.db.conn.execute("SELECT parent, child FROM edges").fetchall()
        }
        actual_closure = {
            tuple(row)
            for row in self.db.conn.execute(
                "SELECT ancestor, descendant, depth FROM closure"
            ).fetchall()
        }
        assert actual_ids == self.active
        assert actual_edges == self.edges
        assert actual_closure == _expected_closure(self.edges)
        assert self.db.validate(level="full").ok

    def teardown(self):
        self.db.close()


TestDatabaseMutations = DatabaseMutationMachine.TestCase
TestDatabaseMutations.settings = settings(settings.default, stateful_step_count=12)
