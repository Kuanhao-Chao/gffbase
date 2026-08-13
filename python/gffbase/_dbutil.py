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
"""Small shared helpers for talking to DuckDB.

``fetchone()`` returns ``None`` when a query yields no rows, so the very common
``con.execute(...).fetchone()[0]`` idiom raises ``TypeError: 'NoneType' object
is not subscriptable`` instead of anything a caller can act on. These helpers
make the no-row case explicit.
"""

from __future__ import annotations

from typing import Any

import duckdb


def scalar(con: duckdb.DuckDBPyConnection, sql: str, params: list | None = None) -> Any:
    """Return the first column of the first row.

    Raises ``duckdb.Error`` if the query returned no rows at all, which is a
    programming error for the aggregate queries this is used with (``COUNT(*)``
    and friends always produce exactly one row).
    """
    row = con.execute(sql, params) if params is not None else con.execute(sql)
    result = row.fetchone()
    if result is None:
        raise duckdb.Error(f"query returned no rows where exactly one was expected: {sql!r}")
    return result[0]


def scalar_or(
    con: duckdb.DuckDBPyConnection,
    sql: str,
    default: Any,
    params: list | None = None,
) -> Any:
    """Like :func:`scalar`, but return ``default`` when there is no row.

    Also returns ``default`` when the row exists but the value is SQL NULL, so
    callers do not have to distinguish "no rows" from "one NULL row" -- for
    ``MAX(...)`` over an empty table those mean the same thing.
    """
    row = con.execute(sql, params) if params is not None else con.execute(sql)
    result = row.fetchone()
    if result is None or result[0] is None:
        return default
    return result[0]
