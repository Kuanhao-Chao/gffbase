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
"""A destination either does not exist, or is a complete database.

A failed ingest used to leave a file behind: valid DuckDB, the full gffbase
schema, no data and no `meta` rows. That could mislead someone in two
directions. Retrying refused with "already exists. Pass force=True", inviting a
force-overwrite of a file they had not meant to touch. And opening the leftover
produced an *empty database that reported itself as current* -- because a
missing `schema_version` looked like a v1 database, which was then dutifully
"migrated" to v2.

The ingest now builds beside the target and renames on success, so the target
is never touched until its replacement is finished. What is left is the
possibility of being handed such a file from elsewhere -- an older gffbase, a
truncated copy -- and that is refused at open rather than silently accepted.
"""

from __future__ import annotations

import duckdb
import pytest
from gffbase import FeatureDB, SchemaVersionError, create_db
from gffbase.ingest import _TMP_SUFFIX
from gffbase.schema import DDL

GOOD = "##gff-version 3\nchr1\trs\tgene\t1\t9\t.\t+\t.\tID=g1\n"
#: Two rows with one id, which the default `merge_strategy="error"` refuses --
#: a failure late enough to have already written features.
DUPLICATE = GOOD + "chr1\trs\tgene\t1\t9\t.\t+\t.\tID=g1\n"


def _write(tmp_path, text, name="in.gff3"):
    p = tmp_path / name
    p.write_text(text)
    return str(p)


def _stray(tmp_path) -> list[str]:
    """Anything the ingest left lying around, scratch files included."""
    return sorted(
        p.name
        for p in tmp_path.iterdir()
        if p.suffix in (".duckdb", ".wal") or _TMP_SUFFIX in p.name
    )


# ---------------------------------------------------------------------------
# Failure leaves nothing
# ---------------------------------------------------------------------------


def test_a_failed_ingest_creates_no_file(tmp_path):
    out = tmp_path / "db.duckdb"
    with pytest.raises(Exception, match="Duplicate ID"):
        create_db(_write(tmp_path, DUPLICATE), str(out))
    assert not out.exists()
    assert _stray(tmp_path) == []


def test_retrying_after_a_failure_needs_no_force(tmp_path):
    """The behaviour that could push someone into force-overwriting: the first
    attempt never succeeded, so the second should not have to claim it is
    replacing anything."""
    out = tmp_path / "db.duckdb"
    with pytest.raises(Exception, match="Duplicate ID"):
        create_db(_write(tmp_path, DUPLICATE, "bad.gff3"), str(out))
    db = create_db(_write(tmp_path, GOOD, "good.gff3"), str(out))
    assert [f.id for f in db.all_features()] == ["g1"]


def test_a_failed_overwrite_leaves_the_original_intact(tmp_path):
    """`force=True` used to unlink the target before reading a single line, so
    a source file that turned out to be unloadable destroyed a good database."""
    out = tmp_path / "db.duckdb"
    create_db(_write(tmp_path, GOOD, "good.gff3"), str(out)).conn.close()

    with pytest.raises(Exception, match="Duplicate ID"):
        create_db(_write(tmp_path, DUPLICATE, "bad.gff3"), str(out), force=True)

    assert out.exists()
    assert [f.id for f in FeatureDB(str(out)).all_features()] == ["g1"]
    assert _stray(tmp_path) == ["db.duckdb"]


def test_an_interrupt_mid_ingest_leaves_nothing(tmp_path, monkeypatch):
    """Caught as BaseException, not Exception: a KeyboardInterrupt during a
    long ingest is exactly when a half-written file would be left behind."""
    import gffbase.ingest as ingest_mod

    def interrupt(*_args, **_kwargs):
        raise KeyboardInterrupt

    monkeypatch.setattr(ingest_mod, "resolve_multipart", interrupt)
    out = tmp_path / "db.duckdb"
    with pytest.raises(KeyboardInterrupt):
        create_db(_write(tmp_path, GOOD), str(out))
    assert not out.exists()
    assert _stray(tmp_path) == []


def test_an_unreadable_source_leaves_nothing(tmp_path):
    out = tmp_path / "db.duckdb"
    with pytest.raises(OSError):
        create_db(str(tmp_path / "does-not-exist.gff3"), str(out))
    assert not out.exists()
    assert _stray(tmp_path) == []


# ---------------------------------------------------------------------------
# Success is unchanged
# ---------------------------------------------------------------------------


def test_a_successful_ingest_leaves_only_the_target(tmp_path):
    out = tmp_path / "db.duckdb"
    db = create_db(_write(tmp_path, GOOD), str(out))
    assert out.exists()
    assert _stray(tmp_path) == ["db.duckdb"], "the scratch file must be gone"
    assert [f.id for f in db.all_features()] == ["g1"]


def test_the_returned_connection_points_at_the_target(tmp_path):
    """The build happens on a scratch path, so the handle handed back has to be
    reopened on the real one -- otherwise later writes would go to a file that
    no longer exists."""
    out = tmp_path / "db.duckdb"
    db = create_db(_write(tmp_path, GOOD), str(out))
    db.conn.execute("INSERT INTO meta VALUES ('probe', 'written')")
    db.conn.close()
    reopened = FeatureDB(str(out))
    assert reopened.conn.execute("SELECT value FROM meta WHERE key='probe'").fetchone() == (
        "written",
    )


def test_overwriting_with_force_still_works(tmp_path):
    out = tmp_path / "db.duckdb"
    create_db(_write(tmp_path, GOOD, "a.gff3"), str(out)).conn.close()
    other = "##gff-version 3\nchr2\trs\tgene\t1\t9\t.\t+\t.\tID=other\n"
    db = create_db(_write(tmp_path, other, "b.gff3"), str(out), force=True)
    assert [f.id for f in db.all_features()] == ["other"]


def test_an_existing_target_without_force_still_refuses(tmp_path):
    out = tmp_path / "db.duckdb"
    create_db(_write(tmp_path, GOOD), str(out)).conn.close()
    with pytest.raises(ValueError, match="already exists"):
        create_db(_write(tmp_path, GOOD), str(out))


def test_in_memory_is_unaffected(tmp_path):
    db = create_db(_write(tmp_path, GOOD), ":memory:")
    assert [f.id for f in db.all_features()] == ["g1"]
    assert _stray(tmp_path) == []


# ---------------------------------------------------------------------------
# Being handed an incomplete file from elsewhere
# ---------------------------------------------------------------------------


def test_an_incomplete_database_is_refused_at_open(tmp_path):
    """Exactly what a pre-fix failed ingest left on disk: the whole schema,
    nothing in it. Opening it used to yield an empty database claiming to be
    schema v2, because a missing version looked like v1 and was migrated."""
    path = tmp_path / "partial.duckdb"
    con = duckdb.connect(str(path))
    con.execute(DDL)
    con.close()
    with pytest.raises(SchemaVersionError, match="incomplete database"):
        FeatureDB(str(path))


def test_the_refusal_says_what_to_do_about_it(tmp_path):
    path = tmp_path / "partial.duckdb"
    con = duckdb.connect(str(path))
    con.execute(DDL)
    con.close()
    with pytest.raises(SchemaVersionError) as exc:
        FeatureDB(str(path))
    message = str(exc.value)
    assert str(path) in message
    assert "create_db" in message


def test_a_genuinely_empty_database_still_opens(tmp_path):
    """A source file with no features is not an error, and its database has
    metadata -- which is precisely what distinguishes it from an interrupted
    build."""
    src = _write(tmp_path, "##gff-version 3\n", "empty.gff3")
    out = tmp_path / "empty.duckdb"
    create_db(src, str(out)).conn.close()
    db = FeatureDB(str(out))
    assert list(db.all_features()) == []
    assert db._schema_version == 2


def test_a_v1_database_is_still_recognised_not_called_incomplete(tmp_path):
    """The check must not swallow the version gate: a v1 database has meta
    rows, so it is upgraded rather than refused."""
    import gzip
    import shutil
    from pathlib import Path

    fixture = Path(__file__).parent / "data" / "v1" / "schema_v1.duckdb.gz"
    path = tmp_path / "v1.duckdb"
    with gzip.open(fixture, "rb") as src, open(path, "wb") as dst:
        shutil.copyfileobj(src, dst)
    db = FeatureDB(str(path))
    assert db._schema_version == 2
    assert len(list(db.all_features())) == 5
