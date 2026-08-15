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
"""Connection lifecycle: `close()`, the context manager, and `read_only`.

DuckDB holds an EXCLUSIVE lock on the database file for the life of a writable
connection. `FeatureDB` had no way to release it -- no `close`, no
`__enter__`/`__exit__`, no `__del__` -- and no way to open one read-only. Two
consequences, both of which these tests pin:

* the file could not be replaced or deleted while any handle existed, which is
  fatal on Windows and merely confusing elsewhere;
* N worker processes could not read one annotation database concurrently,
  which is the shape of every PyTorch `DataLoader` job the project markets
  itself for.

The subtle part is OWNERSHIP. gffbase must never close a connection the caller
opened and still holds, and must always close one it opened itself -- including
the one `create_db` opens on the caller's behalf.
"""

from __future__ import annotations

import gc
from pathlib import Path

import duckdb
import pytest
from gffbase import ClosedDatabaseError, FeatureDB, ReadOnlyError, create_db

SRC = """##gff-version 3
chr1\trs\tgene\t1\t1000\t.\t+\t.\tID=g
chr1\trs\tmRNA\t1\t1000\t.\t+\t.\tID=t;Parent=g
chr1\trs\texon\t1\t100\t.\t+\t.\tID=e;Parent=t
"""


@pytest.fixture
def dbpath(tmp_path) -> Path:
    src = tmp_path / "a.gff3"
    src.write_text(SRC)
    out = tmp_path / "a.duckdb"
    create_db(str(src), str(out), force=True).close()
    return out


# ---------------------------------------------------------------------------
# close()
# ---------------------------------------------------------------------------


def test_close_is_idempotent(dbpath):
    db = FeatureDB(dbpath)
    db.close()
    db.close()  # must not raise
    assert db.closed


def _try_open_in_a_subprocess(path: Path) -> tuple[int, str]:
    """Open `path` for writing from another process. Returns (rc, stderr).

    A subprocess, not another `duckdb.connect` in this one: DuckDB shares a
    single database instance per process, so a second in-process connection
    succeeds regardless of the lock. The lock is between PROCESSES, which is
    what makes it matter -- and what makes `close()` matter.
    """
    import subprocess
    import sys
    import textwrap

    code = textwrap.dedent(f"""
        import duckdb
        duckdb.connect({str(path)!r}, read_only=False).close()
    """)
    proc = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, timeout=120)
    return proc.returncode, proc.stderr


def test_close_releases_the_cross_process_file_lock(dbpath):
    """The point of the whole feature: another process can take the file."""
    first = FeatureDB(dbpath)
    rc, err = _try_open_in_a_subprocess(dbpath)
    assert rc != 0, "a second process took a write lock while a handle was open"
    assert "lock" in err.lower() or "conflict" in err.lower(), err

    first.close()

    rc, err = _try_open_in_a_subprocess(dbpath)
    assert rc == 0, f"close() did not release the lock: {err}"


@pytest.mark.parametrize(
    ("name", "call"),
    [
        ("__getitem__", lambda db: db["g"]),
        ("__contains__", lambda db: "g" in db),
        ("children", lambda db: list(db.children("g"))),
        ("parents", lambda db: list(db.parents("e"))),
        ("region", lambda db: list(db.region("chr1:1-100"))),
        ("all_features", lambda db: list(db.all_features())),
        ("features_of_type", lambda db: list(db.features_of_type("exon"))),
        ("count_features_of_type", lambda db: db.count_features_of_type()),
        ("featuretypes", lambda db: list(db.featuretypes())),
        ("seqids", lambda db: list(db.seqids())),
        ("schema", lambda db: db.schema()),
        ("execute", lambda db: db.execute("SELECT 1")),
        ("children_batched", lambda db: db.children_batched(["g"])),
        ("parents_batched", lambda db: db.parents_batched(["e"])),
        ("region_batched", lambda db: db.region_batched([("chr1", 1, 100)])),
    ],
)
def test_every_read_path_reports_a_closed_database(dbpath, name, call):
    """Not one raw `ConnectionException` from DuckDB.

    Several of these reach the connection *before* the row-yielding helper
    that does the obvious guarding -- `children()` runs a dispatcher query to
    choose its execution path, and the batched APIs build their own SQL. Each
    needs its own guard, so each gets its own case here.
    """
    db = FeatureDB(dbpath)
    db.close()
    with pytest.raises(ClosedDatabaseError, match=r"closed FeatureDB"):
        call(db)


def test_the_closed_error_says_what_to_do_instead(dbpath):
    db = FeatureDB(dbpath)
    db.close()
    with pytest.raises(ClosedDatabaseError) as excinfo:
        db["g"]
    assert "with FeatureDB(path) as db" in str(excinfo.value)


# ---------------------------------------------------------------------------
# Context manager
# ---------------------------------------------------------------------------


def test_with_block_closes_on_exit(dbpath):
    with FeatureDB(dbpath) as db:
        assert db["g"].id == "g"
        assert not db.closed
    assert db.closed


def test_with_block_closes_even_when_the_body_raises(dbpath):
    db = None
    with pytest.raises(RuntimeError, match="boom"):
        with FeatureDB(dbpath) as db:
            raise RuntimeError("boom")
    assert db is not None and db.closed


def test_create_db_result_is_context_manageable(tmp_path):
    """`create_db` opens the connection, so the handle it returns owns it.

    Without the ownership hand-off, `with create_db(...) as db:` would exit
    without releasing the connection it had just created -- the exact leak the
    context manager exists to close.
    """
    src = tmp_path / "b.gff3"
    src.write_text(SRC)
    out = tmp_path / "b.duckdb"
    with create_db(str(src), str(out), force=True) as db:
        assert db.count_features_of_type() == 3
    assert db.closed
    # The lock is gone, so the file can be reopened for writing.
    duckdb.connect(str(out), read_only=False).close()


# ---------------------------------------------------------------------------
# Ownership
# ---------------------------------------------------------------------------


def test_a_caller_supplied_connection_is_never_closed(dbpath):
    """gffbase did not open it, so gffbase does not get to close it."""
    con = duckdb.connect(str(dbpath))
    db = FeatureDB(con)
    db.close()
    assert db.closed
    # Still usable by its owner.
    assert con.execute("SELECT COUNT(*) FROM features").fetchone()[0] == 3
    con.close()


def test_the_segment_cursor_is_closed_regardless_of_ownership(dbpath):
    """gffbase creates the segment cursor with `conn.cursor()`, so gffbase
    owns *it* even when it does not own the connection."""
    con = duckdb.connect(str(dbpath))
    db = FeatureDB(con)
    db._segment_cursor()  # force creation
    assert db._seg_cursor is not None
    db.close()
    assert db._seg_cursor is None
    con.close()


def test_del_on_a_half_constructed_object_does_not_raise():
    """`__init__` can raise before the lifecycle attributes exist, and
    `__del__` still runs. Reading an unset attribute there would raise a
    second exception during collection and mask the first."""
    with pytest.raises(TypeError):
        FeatureDB(42)
    gc.collect()  # must be quiet


# ---------------------------------------------------------------------------
# read_only
# ---------------------------------------------------------------------------


def test_read_only_reads_work(dbpath):
    db = FeatureDB(dbpath, read_only=True)
    try:
        assert db.read_only
        assert db["g"].id == "g"
        assert [f.id for f in db.children("g", level=None)] == ["t", "e"]
        assert db.region_batched([("chr1", 1, 100)]).num_rows > 0
    finally:
        db.close()


@pytest.mark.parametrize(
    ("name", "call"),
    [
        ("delete", lambda db: db.delete(["e"])),
        ("update", lambda db: db.update([])),
        ("analyze", lambda db: db.analyze()),
        ("add_relations", lambda db: db.add_relations([])),
    ],
)
def test_read_only_refuses_every_mutator(dbpath, name, call):
    db = FeatureDB(dbpath, read_only=True)
    try:
        with pytest.raises(ReadOnlyError, match=r"read_only=True"):
            call(db)
    finally:
        db.close()


def test_two_read_only_handles_can_share_one_file(dbpath):
    """The reason `read_only` exists: N worker processes, one database.

    A writable connection takes an exclusive lock, so this is impossible
    without it -- and a `DataLoader` with `num_workers>1` is exactly this
    pattern.
    """
    a = FeatureDB(dbpath, read_only=True)
    b = FeatureDB(dbpath, read_only=True)
    try:
        assert a["g"].id == b["g"].id == "g"
    finally:
        a.close()
        b.close()


def test_read_only_does_not_migrate_a_v1_database(tmp_path):
    """`upgrade="auto"` is coerced to `"never"` under `read_only`.

    Migration is DDL. Relying on DuckDB to refuse it is not enough: the
    migration helper catches `duckdb.Error` and falls through to v1
    compatibility mode, so the attempt would only add a confusing log line on
    the way to the same result.
    """
    import gzip
    import shutil

    fixture = Path(__file__).parent / "data" / "v1" / "schema_v1.duckdb.gz"
    if not fixture.is_file():
        pytest.skip("v1 fixture not vendored")
    v1 = tmp_path / "v1.duckdb"
    with gzip.open(fixture, "rb") as fh, open(v1, "wb") as out:
        shutil.copyfileobj(fh, out)
    before = v1.stat().st_mtime_ns

    db = FeatureDB(v1, read_only=True)
    try:
        assert db._upgrade == "never"
        assert db._v1_shim is True
        assert db._schema_version == 1
    finally:
        db.close()
    assert v1.stat().st_mtime_ns == before, "a read-only open modified the file"


def test_read_only_is_advisory_on_a_supplied_connection(dbpath):
    """A caller can ask for a read-only VIEW over a connection they own.

    DuckDB does not enforce it -- the connection is what it is -- but the
    mutator guards still fire, which is the useful half.
    """
    con = duckdb.connect(str(dbpath))
    db = FeatureDB(con, read_only=True)
    try:
        assert db.read_only
        assert db["g"].id == "g"
        with pytest.raises(ReadOnlyError):
            db.delete(["e"])
    finally:
        db.close()
        con.close()


def test_set_pragmas_is_allowed_read_only(dbpath):
    """`SET` is session state, not a write to the database file.

    Blocking it would be a false promise of safety and would break the
    `pragmas=` constructor argument, which legacy callers pass routinely.
    """
    db = FeatureDB(dbpath, read_only=True, pragmas={"threads": 2})
    try:
        db.set_pragmas({"threads": 1})
    finally:
        db.close()
