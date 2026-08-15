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
"""Disk-footprint benchmark — gffbase .duckdb vs legacy .sqlite, decomposed
by table where the engine exposes a stat view.
"""

from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "python"))

import duckdb

from benchmarks.common import (
    GFFBASE_DB,
    LEGACY_DB,
    du,
    pretty_bytes,
    write_results,
)


def gffbase_table_breakdown():
    con = duckdb.connect(str(GFFBASE_DB), read_only=True)
    # `pragma database_size` returns engine-level totals; per-table size is
    # not exposed in DuckDB's public catalog. We approximate via row counts +
    # average row width via `summary`.
    tables = [
        r[0]
        for r in con.execute(
            "SELECT table_name FROM duckdb_tables() WHERE database_name = current_database()"
        ).fetchall()
    ]
    breakdown = {}
    for t in tables:
        try:
            n = con.execute(f'SELECT COUNT(*) FROM "{t}"').fetchone()[0]
            breakdown[t] = {"row_count": n}
        except duckdb.Error:
            continue
    con.close()
    return breakdown


def legacy_table_breakdown():
    sq = sqlite3.connect(str(LEGACY_DB))
    breakdown = {}
    # SQLite's dbstat virtual table needs the SQLITE_ENABLE_DBSTAT_VTAB compile flag,
    # which the system Python build may lack. Fall back to row counts.
    try:
        rows = sq.execute("SELECT name, SUM(pgsize) FROM dbstat GROUP BY name").fetchall()
        for name, size in rows:
            breakdown[name] = {"bytes": size}
    except sqlite3.OperationalError:
        for (name,) in sq.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall():
            try:
                n = sq.execute(f"SELECT COUNT(*) FROM [{name}]").fetchone()[0]
                breakdown[name] = {"row_count": n}
            except sqlite3.Error:
                pass
    sq.close()
    return breakdown


def main():
    gffbase_total = du(GFFBASE_DB)
    legacy_total = du(LEGACY_DB)
    print(f"[disk] gffbase .duckdb = {pretty_bytes(gffbase_total)}", flush=True)
    print(f"[disk] legacy .sqlite  = {pretty_bytes(legacy_total)}", flush=True)
    if legacy_total:
        ratio = gffbase_total / legacy_total
        print(f"[disk] gffbase / legacy = {ratio:.2f}× ", flush=True)

    payload = {
        "gffbase": {
            "path": str(GFFBASE_DB),
            "total_bytes": gffbase_total,
            "tables": gffbase_table_breakdown(),
        },
        "legacy": {
            "path": str(LEGACY_DB),
            "total_bytes": legacy_total,
            "tables": legacy_table_breakdown(),
        },
        "comparison": {
            "gffbase_over_legacy": (gffbase_total / legacy_total) if legacy_total else None,
            "delta_bytes": gffbase_total - legacy_total,
        },
    }
    p = write_results("04_disk", payload)
    print(f"\nResults → {p}", flush=True)


if __name__ == "__main__":
    main()
