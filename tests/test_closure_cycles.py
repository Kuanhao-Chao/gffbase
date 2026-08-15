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
"""A cyclic `Parent` graph must terminate, not lap.

GFF3 does not forbid writing one, and files in the wild do. The hierarchy walks
used to follow the cycle until they ran out of depth budget, emitting the same
handful of features once per lap: a two-feature cycle made `children()` return
64 rows, and the deeper of the two dynamic walks runs to depth 64 by default,
so a longer cycle was worse.

The walks now carry the path they took and refuse to visit a node twice on it.
That is free on well-formed data -- in a DAG no node can repeat on a path, so
the filter never fires, and the FlyBase 50k corpus produces byte-identical
closure rows either way.

Where the file's assertion is literal and bounded, it is kept rather than
tidied: `ID=a;Parent=a` gives `children('a') == ['a']`, which is what gffutils
returns too. The validator reports it as a warning instead of pretending it did
not happen.
"""

from __future__ import annotations

import logging

import pytest
from gffbase import create_db

HEAD = "##gff-version 3\n"


def _feature(fid: str, parent: str | None = None) -> str:
    attrs = f"ID={fid}" + (f";Parent={parent}" if parent else "")
    return f"chr1\trs\tgene\t1\t9\t.\t+\t.\t{attrs}\n"


def _build(tmp_path, text, name="in.gff3", **kwargs):
    src = tmp_path / name
    src.write_text(text)
    return create_db(str(src), ":memory:", **kwargs)


SELF_LOOP = HEAD + _feature("a", "a")
TWO_CYCLE = HEAD + _feature("a", "b") + _feature("b", "a")
THREE_CYCLE = HEAD + _feature("a", "c") + _feature("b", "a") + _feature("c", "b")
#: A cycle hanging off an acyclic spine -- the shape that matters, because the
#: healthy part must keep answering correctly.
TAIL = HEAD + _feature("root") + _feature("a", "root") + _feature("b", "a") + _feature("a2", "b")


# ---------------------------------------------------------------------------
# Termination
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "text,expected",
    [
        (TWO_CYCLE, ["b"]),
        (THREE_CYCLE, ["b", "c"]),
    ],
)
def test_a_cycle_yields_each_feature_once(tmp_path, text, expected):
    """It used to yield 64 rows -- the cycle's members over and over, one copy
    per lap, until the depth budget ran out."""
    db = _build(tmp_path, text)
    assert sorted(f.id for f in db.children("a")) == expected


@pytest.mark.parametrize("text", [SELF_LOOP, TWO_CYCLE, THREE_CYCLE])
def test_the_closure_stays_small(tmp_path, text):
    """Bounded by the number of features, not by the depth budget."""
    db = _build(tmp_path, text)
    n_features = db.conn.execute("SELECT COUNT(*) FROM features").fetchone()[0]
    n_closure = db.conn.execute("SELECT COUNT(*) FROM closure").fetchone()[0]
    assert n_closure <= n_features * n_features


def test_the_dynamic_walk_is_cycle_safe_too(tmp_path):
    """`children(level=...)` past the materialized depth falls back to a
    recursive CTE that runs to depth 64 by default -- the deeper of the two
    walks, and so the more expensive one to hit a cycle in."""
    db = _build(tmp_path, THREE_CYCLE)
    assert [f.id for f in db.children("a", level=9)] == []
    assert sorted(f.id for f in db.children("a", level=1)) == ["b"]


def test_the_batched_walk_is_cycle_safe_too(tmp_path):
    db = _build(tmp_path, THREE_CYCLE)
    table = db.children_batched(["a"], level=None)
    ids = table["descendant_id"].to_pylist()
    assert sorted(ids) == ["b", "c"]


def test_parents_is_cycle_safe(tmp_path):
    db = _build(tmp_path, THREE_CYCLE)
    assert sorted(f.id for f in db.parents("a")) == ["b", "c"]


def test_an_acyclic_branch_still_answers_correctly(tmp_path):
    """A cycle must not poison the rest of the hierarchy."""
    text = TAIL + _feature("loop1", "loop2") + _feature("loop2", "loop1")
    db = _build(tmp_path, text)
    assert sorted(f.id for f in db.children("root")) == ["a", "a2", "b"]
    assert sorted(f.id for f in db.parents("a2")) == ["a", "b", "root"]


# ---------------------------------------------------------------------------
# Well-formed data is unaffected
# ---------------------------------------------------------------------------


def test_an_acyclic_hierarchy_is_unchanged(tmp_path):
    """The path filter cannot fire in a DAG, so nothing about a normal corpus
    changes -- which is the property that makes carrying the path acceptable."""
    text = HEAD + _feature("g") + _feature("t", "g") + _feature("e1", "t") + _feature("e2", "t")
    db = _build(tmp_path, text)
    assert sorted(f.id for f in db.children("g")) == ["e1", "e2", "t"]
    assert [
        (r[0], r[1], r[2])
        for r in db.conn.execute(
            "SELECT ancestor, descendant, depth FROM closure ORDER BY depth, ancestor, descendant"
        ).fetchall()
    ] == [
        ("g", "t", 1),
        ("t", "e1", 1),
        ("t", "e2", 1),
        ("g", "e1", 2),
        ("g", "e2", 2),
    ]


def test_a_diamond_still_reports_its_descendant_once(tmp_path):
    """A DAG is not a cycle: `d` is reachable from `a` by two paths of equal
    length and must appear once, which is what the DISTINCT on the closure
    projection is for."""
    text = HEAD + _feature("a") + _feature("b", "a") + _feature("c", "a") + _feature("d", "b")
    text += "chr1\trs\tgene\t1\t9\t.\t+\t.\tID=d2;Parent=b,c\n"
    db = _build(tmp_path, text)
    assert sorted(f.id for f in db.children("a")) == ["b", "c", "d", "d2"]


# ---------------------------------------------------------------------------
# Reporting
# ---------------------------------------------------------------------------


def test_a_cycle_is_logged(tmp_path, caplog):
    """Staying silent would leave the user with a hierarchy that quietly is not
    the one their file describes."""
    with caplog.at_level(logging.WARNING, logger="gffbase.ingest"):
        _build(tmp_path, TWO_CYCLE)
    messages = [r.getMessage() for r in caplog.records]
    assert any("form a cycle and were not followed" in m for m in messages)


def test_a_cycle_is_recorded_in_the_database(tmp_path):
    db = _build(tmp_path, THREE_CYCLE)
    rows = db.conn.execute(
        "SELECT raw_id, resolved_id FROM id_conflicts WHERE kind = 'parent_cycle'"
    ).fetchall()
    assert len(rows) == 3


def test_no_cycle_is_reported_for_a_healthy_file(tmp_path):
    db = _build(tmp_path, TAIL)
    assert db.conn.execute(
        "SELECT COUNT(*) FROM id_conflicts WHERE kind = 'parent_cycle'"
    ).fetchone() == (0,)


# ---------------------------------------------------------------------------
# The self-edge, which the file asserts literally
# ---------------------------------------------------------------------------


def test_a_self_loop_matches_the_oracle(tmp_path):
    """gffutils gives `children('a') == ['a']` for `ID=a;Parent=a`, and so do
    we: the file says it, and it is bounded."""
    db = _build(tmp_path, SELF_LOOP)
    assert [f.id for f in db.children("a")] == ["a"]


def test_self_ancestry_is_a_warning_not_an_error(tmp_path):
    """Reachable from ordinary input and matching the oracle, so it cannot be
    an error -- but `children(x)` naming `x` is worth knowing about."""
    db = _build(tmp_path, SELF_LOOP)
    report = db.validate()
    assert report.ok is True
    assert [(v.name, v.severity) for v in report.violations] == [
        ("closure_self_ancestry", "warning")
    ]


def test_a_strict_ingest_still_loads_a_self_loop(tmp_path):
    """Validation runs at the end of a strict ingest and raises on errors, so
    the severity choice decides whether such a file loads at all."""
    db = _build(tmp_path, SELF_LOOP, mode="strict")
    assert len(list(db.all_features())) == 1


def test_a_healthy_file_reports_no_self_ancestry(tmp_path):
    db = _build(tmp_path, TAIL)
    assert db.validate().violations == []
