"""`create_db(pragmas=...)` reaches the ingest.

`pragmas` used to be applied only to the handle returned after ingest had
finished, so the documented way to bound ingest memory --
`pragmas={"memory_limit": "2GB"}` -- could not work.
"""

from __future__ import annotations

import duckdb
import pytest
from gffbase import create_db, ingest
from gffbase.constants import default_pragmas

GFF = "##gff-version 3\nchr1\tt\tgene\t1\t100\t.\t+\t.\tID=g1\n"


def _setting(con, name: str) -> str:
    return con.execute("SELECT current_setting(?)", [name]).fetchone()[0]


def test_pragmas_reach_the_ingest_connection(monkeypatch):
    seen = {}
    real = ingest._build_database

    def spy(path, dbfn=":memory:", **kwargs):
        con, stats = real(path, dbfn, **kwargs)
        # The connection ingest built on: `:memory:` hands back that one.
        seen["memory_limit"] = _setting(con, "memory_limit")
        seen["threads"] = _setting(con, "threads")
        return con, stats

    monkeypatch.setattr(ingest, "_build_database", spy)
    create_db(GFF, ":memory:", from_string=True, pragmas={"memory_limit": "321MB", "threads": 2})
    assert seen["memory_limit"].startswith("30")  # 321 MB is reported as ~306 MiB
    assert int(seen["threads"]) == 2


def test_an_explicit_threads_pragma_beats_the_environment(monkeypatch):
    monkeypatch.setenv("GFFBASE_THREADS", "3")
    db = create_db(GFF, ":memory:", from_string=True, pragmas={"threads": 1})
    assert int(_setting(db.conn, "threads")) == 1


def test_gffutils_sqlite_pragmas_are_skipped_at_ingest():
    db = create_db(GFF, ":memory:", from_string=True, pragmas=default_pragmas)
    assert db["g1"].start == 1


def test_a_pragma_value_cannot_smuggle_a_statement():
    with pytest.raises(duckdb.Error):
        create_db(
            GFF, ":memory:", from_string=True, pragmas={"threads": "1; DROP TABLE attributes"}
        )
