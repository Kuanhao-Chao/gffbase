"""`create_db(pragmas=...)` reaches the ingest, and a failed ingest cleans up.

`pragmas` used to be applied only to the handle returned after ingest had
finished, so the documented way to bound ingest memory --
`pragmas={"memory_limit": "2GB"}` -- could not work. And DuckDB's spill
directory `{db}.tmp` was `unlink`ed like a file, which fails on a directory,
silently; a failed ingest that had spilled left its spill files behind.
"""

from __future__ import annotations

import os

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


def test_a_failed_ingest_removes_the_spill_directory(tmp_path, monkeypatch):
    real = ingest._build_database

    def spill_then_fail(path, dbfn=":memory:", **kwargs):
        con, _ = real(path, dbfn, **kwargs)
        con.close()
        spill = f"{dbfn}.tmp"
        os.makedirs(spill, exist_ok=True)
        with open(os.path.join(spill, "duckdb_temp_block-0.block"), "wb") as fh:
            fh.write(b"\0" * 4096)
        raise RuntimeError("disk full")

    monkeypatch.setattr(ingest, "_build_database", spill_then_fail)
    src = tmp_path / "a.gff3"
    src.write_text(GFF)
    with pytest.raises(RuntimeError, match="disk full"):
        create_db(str(src), str(tmp_path / "a.duckdb"))
    assert sorted(os.listdir(tmp_path)) == ["a.gff3"]


def test_remove_quietly_does_not_follow_a_symlinked_spill_dir(tmp_path):
    keep = tmp_path / "elsewhere"
    keep.mkdir()
    (keep / "precious").write_text("x")
    target = tmp_path / "db.duckdb"
    os.symlink(keep, f"{target}.tmp")
    ingest._remove_quietly(str(target))
    assert (keep / "precious").exists()
    assert not os.path.lexists(f"{target}.tmp")
