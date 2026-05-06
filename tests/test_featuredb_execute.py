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
"""Phase 5 — `execute()` escape hatch and SQLite-compat views."""

from __future__ import annotations

from pathlib import Path

import pytest

from gffbase import create_db

DATA = Path(__file__).parent / "data"


@pytest.fixture
def hier_db():
    return create_db(str(DATA / "hierarchy.gff3"), ":memory:")


def test_features_compat_view(hier_db):
    rows = hier_db.execute("SELECT id FROM features_compat WHERE seqid='chr1'").fetchall()
    ids = sorted(r[0] for r in rows)
    assert ids == ["c1", "c2", "e1", "e2", "e3", "g1", "t1", "t2"]


def test_features_compat_attributes_are_raw_bytes(hier_db):
    row = hier_db.execute(
        "SELECT attributes FROM features_compat WHERE id='g1'"
    ).fetchone()
    # Documented break: this is the col-9 raw text, not JSON.
    assert row[0] == "ID=g1;Name=geneA"


def test_relations_compat_view(hier_db):
    rows = hier_db.execute(
        "SELECT parent, child FROM relations_compat WHERE level=1 AND parent='g1'"
    ).fetchall()
    pairs = sorted(rows)
    assert pairs == [("g1", "t1"), ("g1", "t2")]


def test_relations_compat_level_2(hier_db):
    rows = hier_db.execute(
        "SELECT child FROM relations_compat WHERE level=2 AND parent='g1' ORDER BY child"
    ).fetchall()
    children = [r[0] for r in rows]
    assert children == ["c1", "c2", "e1", "e2", "e3"]


def test_execute_strips_trailing_semicolon(hier_db):
    rows = hier_db.execute("SELECT COUNT(*) FROM features;").fetchone()
    assert rows[0] == 8
