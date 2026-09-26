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

import logging
from typing import Any

import duckdb

_log = logging.getLogger("gffbase")


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


def _sql_literal(value) -> str:
    """Render a Python scalar as a DuckDB literal.

    `SET`/`PRAGMA` take no bind parameters, so a setting's value has to be
    written into the statement text. This is the one place that is allowed to
    happen, and it happens by construction rather than by interpolation:
    booleans and numbers have no syntax to escape, and a string is
    single-quoted with its own quotes doubled, which is the only escape SQL
    string literals have.
    """
    if isinstance(value, bool):
        # Before the int branch: bool is a subclass of int, and DuckDB spells
        # its booleans `true`/`false`, not `1`/`0`.
        return "true" if value else "false"
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float):
        if value != value or value in (float("inf"), float("-inf")):
            raise ValueError(f"cannot use {value!r} as a setting value")
        return repr(value)
    text = str(value).replace("'", "''")
    return f"'{text}'"


def apply_settings(con: duckdb.DuckDBPyConnection, settings: dict) -> None:
    """Apply DuckDB settings, skipping names DuckDB does not have.

    Legacy callers pass `constants.default_pragmas` -- `synchronous`,
    `journal_mode`, `main.page_size`, `main.cache_size` -- none of which
    DuckDB has. Those are skipped, which is what makes a gffutils script run
    here unchanged.

    Names are matched against DuckDB's own settings catalog and values
    rendered by `_sql_literal`, so nothing a caller supplies reaches the
    parser as syntax (see docs/source/content/advisory_sql_injection.rst).
    Matching the live catalog rather than a hardcoded list tracks whatever
    DuckDB build is installed.
    """
    known = {row[0] for row in con.execute("SELECT name FROM duckdb_settings()").fetchall()}
    for key, value in settings.items():
        name = str(key)
        if name not in known:
            _log.debug("skipping setting %r: not a DuckDB setting", name)
            continue
        # `name` is echoed from the catalog, so it cannot carry syntax.
        con.execute(f"SET {name} = {_sql_literal(value)}")


def in_transaction(con: duckdb.DuckDBPyConnection) -> bool:
    """True if `con` is inside an explicit transaction.

    DuckDB exposes no such flag, and probing with `BEGIN` is not an option: a
    failed statement inside a transaction aborts it, so the probe would
    destroy the caller's transaction it was meant to respect. Instead: in
    autocommit mode every statement runs in a transaction of its own, so two
    consecutive `txid_current()` calls differ; inside an explicit transaction
    they return the same id.
    """
    first = con.execute("SELECT txid_current()").fetchone()
    second = con.execute("SELECT txid_current()").fetchone()
    return first is not None and second is not None and first[0] == second[0]
