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
"""Phase 5 — `export_sqlite` round-trip."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest
from gffbase import create_db, export_sqlite

DATA = Path(__file__).parent / "data"


def test_export_sqlite_writes_legacy_tables(tmp_path):
    db = create_db(str(DATA / "hierarchy.gff3"), ":memory:")
    out = tmp_path / "exported.db"
    export_sqlite(db.conn, str(out))
    assert out.exists()

    sq = sqlite3.connect(str(out))
    try:
        # Legacy schema must have features, relations, meta, directives.
        tables = sorted(
            r[0] for r in sq.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
        )
        assert "features" in tables
        assert "relations" in tables
        assert "meta" in tables
        assert "directives" in tables

        n_features = sq.execute("SELECT COUNT(*) FROM features").fetchone()[0]
        assert n_features == 8

        # Relations match closure.
        n_rel = sq.execute("SELECT COUNT(*) FROM relations").fetchone()[0]
        assert n_rel > 0

        # Bin column was populated.
        bins = sq.execute("SELECT bin FROM features WHERE id='e1'").fetchone()
        assert bins[0] is not None
    finally:
        sq.close()


def test_export_refuses_overwrite(tmp_path):
    db = create_db(str(DATA / "hierarchy.gff3"), ":memory:")
    out = tmp_path / "exported.db"
    export_sqlite(db.conn, str(out))
    with pytest.raises(ValueError):
        export_sqlite(db.conn, str(out))
    # force=True overwrites
    export_sqlite(db.conn, str(out), force=True)
