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

from collections import defaultdict
from pathlib import Path
from tempfile import TemporaryDirectory

import pytest
from gffbase import Feature, create_db
from hypothesis import given, settings
from hypothesis import strategies as st
from hypothesis.stateful import RuleBasedStateMachine, invariant, rule

pytestmark = pytest.mark.property


@st.composite
def _dag_cases(draw):
    """A rooted acyclic hierarchy plus inclusive region queries."""

    count = draw(st.integers(min_value=1, max_value=10))
    parents = [None]
    for child in range(1, count):
        # A parent always precedes its child, which makes cycles impossible
        # without sharing production's graph implementation.
        parents.append(draw(st.integers(min_value=0, max_value=child - 1)))

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


def _descendants(parents: list[int | None], anchor: int) -> dict[int, int]:
    direct: dict[int, list[int]] = defaultdict(list)
    for child, parent in enumerate(parents):
        if parent is not None:
            direct[parent].append(child)

    found: dict[int, int] = {}
    frontier = [(child, 1) for child in direct[anchor]]
    while frontier:
        child, depth = frontier.pop()
        previous = found.get(child)
        if previous is not None and previous <= depth:
            continue
        found[child] = depth
        frontier.extend((grandchild, depth + 1) for grandchild in direct[child])
    return found


def _ancestors(parents: list[int | None], anchor: int) -> dict[int, int]:
    found: dict[int, int] = {}
    parent = parents[anchor]
    depth = 1
    while parent is not None:
        found[parent] = depth
        parent = parents[parent]
        depth += 1
    return found


@given(_dag_cases())
def test_generated_dag_matches_relationship_and_region_oracles(case):
    parents, intervals, queries = case
    lines = ["##gff-version 3\n"]
    for idx, ((start, end), parent) in enumerate(zip(intervals, parents, strict=True)):
        attrs = f"ID=f{idx}"
        if parent is not None:
            attrs += f";Parent=f{parent}"
        featuretype = "gene" if parent is None else "exon"
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
        frontier = [(child, 1, {ancestor, child}) for child in direct[ancestor]]
        while frontier:
            descendant, depth, seen = frontier.pop()
            closure.add((ancestor, descendant, depth))
            frontier.extend(
                (child, depth + 1, seen | {child})
                for child in direct.get(descendant, set())
                if child not in seen
            )
    return closure


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
