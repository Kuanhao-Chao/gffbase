"""`IngestStats.stages`: where ingest time goes, per stage."""

from __future__ import annotations

import time

import pytest
from gffbase._options import IngestOptions
from gffbase.ingest import from_file

GFF3 = (
    "chr1\tt\tgene\t1\t100\t.\t+\t.\tID=g1\n"
    "chr1\tt\tmRNA\t1\t100\t.\t+\t.\tID=t1;Parent=g1\n"
    "chr1\tt\texon\t1\t50\t.\t+\t.\tID=e1;Parent=t1\n"
)
GTF = 'chr1\tt\texon\t1\t50\t.\t+\t.\tgene_id "G"; transcript_id "T";\n'


def _stats(tmp_path, text, name, **kw):
    path = tmp_path / name
    path.write_text(text)
    t0 = time.perf_counter()
    _, stats = from_file(str(path), ":memory:", **kw)
    return stats, time.perf_counter() - t0


def test_every_stage_is_timed_in_order(tmp_path):
    stats, wall = _stats(tmp_path, GFF3, "a.gff3")
    assert list(stats.stages)[:3] == ["setup", "parse", "append"]
    assert "edges" in stats.stages and "gtf_inference" not in stats.stages
    assert {"closure", "indexes", "stats"} <= set(stats.stages)
    assert all(v >= 0 for v in stats.stages.values())
    # The stages partition the build: nothing large goes uncharged.
    assert sum(stats.stages.values()) <= wall


def test_a_gtf_reports_its_inference_stage(tmp_path):
    stats, _ = _stats(tmp_path, GTF, "a.gtf")
    assert "gtf_inference" in stats.stages and "edges" not in stats.stages


def test_strict_mode_reports_validation(tmp_path):
    stats, _ = _stats(tmp_path, GFF3, "a.gff3", options=IngestOptions(mode="strict"))
    assert "validate" in stats.stages


def test_the_trace_prints_each_stage(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("GFFBASE_INGEST_TRACE", "1")
    stats, _ = _stats(tmp_path, GFF3, "a.gff3")
    err = capsys.readouterr().err
    for stage in stats.stages:
        assert f"gffbase ingest: {stage} " in err


def test_no_trace_by_default(tmp_path, monkeypatch, capsys):
    monkeypatch.delenv("GFFBASE_INGEST_TRACE", raising=False)
    _stats(tmp_path, GFF3, "a.gff3")
    assert "gffbase ingest:" not in capsys.readouterr().err


def _threads(tmp_path, **kw):
    path = tmp_path / "t.gff3"
    path.write_text(GFF3)
    con, _ = from_file(str(path), ":memory:", **kw)
    return int(con.execute("SELECT current_setting('threads')").fetchone()[0])


def test_ingest_uses_at_most_eight_threads_by_default(tmp_path, monkeypatch):
    """Threads past a handful bought MANE no time and cost 1.2 GiB of RSS."""
    import os

    from gffbase._dbutil import DEFAULT_THREADS

    monkeypatch.delenv("GFFBASE_THREADS", raising=False)
    monkeypatch.delenv("GFFUTILS2_THREADS", raising=False)
    assert _threads(tmp_path) == min(DEFAULT_THREADS, os.cpu_count() or 1)


def test_the_environment_and_pragmas_override_the_thread_default(tmp_path, monkeypatch):
    monkeypatch.setenv("GFFBASE_THREADS", "3")
    assert _threads(tmp_path) == 3
    options = IngestOptions(pragmas={"threads": 2})
    assert _threads(tmp_path, options=options) == 2


# ---------------------------------------------------------------------------
# The memory budget
# ---------------------------------------------------------------------------


def _limit(con) -> int:
    from gffbase.ingest import _setting_bytes

    return _setting_bytes(con.execute("SELECT current_setting('memory_limit')").fetchone()[0])


def test_a_file_ingest_runs_under_the_budget_and_hands_back_duckdbs_limit(tmp_path, monkeypatch):
    import duckdb
    from gffbase import ingest

    default = _limit(duckdb.connect())
    seen = []
    real = ingest._add_features_primary_key
    monkeypatch.setattr(
        ingest, "_add_features_primary_key", lambda con: (seen.append(_limit(con)), real(con))
    )
    path = tmp_path / "a.gff3"
    path.write_text(GFF3)
    con, _ = from_file(str(path), str(tmp_path / "a.duckdb"))
    assert seen == [pytest.approx(ingest.INGEST_MEMORY_LIMIT, rel=0.01)]
    assert _limit(con) == pytest.approx(default, rel=0.01)


@pytest.mark.parametrize("case", ["in_memory", "caller_chose"])
def test_the_budget_stays_out_of_the_way(case, tmp_path, monkeypatch):
    import duckdb
    from gffbase import ingest

    seen = []
    real = ingest._add_features_primary_key
    monkeypatch.setattr(
        ingest, "_add_features_primary_key", lambda con: (seen.append(_limit(con)), real(con))
    )
    path = tmp_path / "a.gff3"
    path.write_text(GFF3)
    if case == "in_memory":
        from_file(str(path), ":memory:")
        assert seen == [pytest.approx(_limit(duckdb.connect()), rel=0.01)]
    else:
        from_file(
            str(path),
            str(tmp_path / "a.duckdb"),
            options=IngestOptions(pragmas={"memory_limit": "3GB"}),
        )
        # DuckDB reports sizes to 0.1 GiB ("2.7 GiB" for 3 GB).
        assert seen == [pytest.approx(3 * 10**9, rel=0.05)]


def test_a_step_that_runs_out_is_retried_with_twice_the_limit(tmp_path, caplog):
    import logging

    import duckdb
    from gffbase.ingest import INGEST_MEMORY_LIMIT, _MemoryBudget

    con = duckdb.connect(str(tmp_path / "b.duckdb"))
    budget = _MemoryBudget(con, str(tmp_path / "b.duckdb"), None)
    attempts = []

    def step():
        attempts.append(_limit(con))
        if len(attempts) < 3:
            raise duckdb.OutOfMemoryException("out of memory")
        return "done"

    with caplog.at_level(logging.INFO, logger="gffbase.ingest"):
        assert budget.run(step) == "done"
    assert attempts == [pytest.approx(INGEST_MEMORY_LIMIT * k, rel=0.01) for k in (1, 2, 4)]
    assert "raising DuckDB's memory limit" in caplog.text


def test_running_out_at_the_ceiling_is_an_error(tmp_path):
    import duckdb
    from gffbase.ingest import _MemoryBudget

    con = duckdb.connect(str(tmp_path / "c.duckdb"))
    budget = _MemoryBudget(con, str(tmp_path / "c.duckdb"), None)

    def step():
        raise duckdb.OutOfMemoryException("out of memory")

    with pytest.raises(duckdb.OutOfMemoryException):
        budget.run(step)
    assert budget.limit == budget.ceiling


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("805.7 GiB", int(805.7 * 2**30)),
        ("976.5 MiB", int(976.5 * 2**20)),
        ("512 KB", 512_000),
        ("3GB", 3 * 10**9),
        ("unlimited", None),
    ],
)
def test_duckdb_size_settings_are_read(text, expected):
    from gffbase.ingest import _setting_bytes

    assert _setting_bytes(text) == expected


GTF_TWO = (
    'chr1\tt\texon\t1\t50\t.\t+\t.\tgene_id "G"; transcript_id "T1";\n'
    'chr1\tt\texon\t80\t100\t.\t+\t.\tgene_id "G"; transcript_id "T2";\n'
    'chr1\tt\texon\t200\t300\t.\t+\t.\tgene_id "H"; transcript_id "T3";\n'
)


def _dump(con):
    """Every row of the tables a stage writes, as sets -- `bbox` aside, which
    exists only when the R-tree was built."""
    has_bbox = con.execute(
        "SELECT count(*) FROM duckdb_columns() WHERE table_name = 'features' "
        "AND column_name = 'bbox'"
    ).fetchone()[0]
    out = {}
    for table in ("features", "attributes", "edges", "closure", "autoincrements"):
        select = "* EXCLUDE (bbox)" if table == "features" and has_bbox else "*"
        out[table] = sorted(map(repr, con.execute(f"SELECT {select} FROM {table}").fetchall()))
    return out


def test_gtf_inference_that_runs_out_is_rolled_back_and_rerun(tmp_path, monkeypatch, caplog):
    """A multi-statement step cannot just be called again: it is rolled back,
    its counters restored, and rerun with more memory -- to the same result."""
    import logging

    import duckdb
    from gffbase import ingest

    path = tmp_path / "a.gtf"
    path.write_text(GTF_TWO)
    clean, _ = from_file(str(path), str(tmp_path / "clean.duckdb"))
    expected = _dump(clean)

    real = ingest._synthesize_genes
    calls = []

    def once_out_of_memory(*args, **kwargs):
        calls.append(1)
        n = real(*args, **kwargs)
        if len(calls) == 1:  # after its writes, so the rollback has work to do
            raise duckdb.OutOfMemoryException("out of memory")
        return n

    monkeypatch.setattr(ingest, "_synthesize_genes", once_out_of_memory)
    with caplog.at_level(logging.INFO, logger="gffbase.ingest"):
        con, _ = from_file(str(path), str(tmp_path / "retried.duckdb"))
    assert len(calls) == 2
    assert "raising DuckDB's memory limit" in caplog.text
    assert _dump(con) == expected


def test_no_automatic_checkpoint_while_the_budget_holds(tmp_path, monkeypatch):
    """A checkpoint that runs out of memory is a FatalException, which no
    retry can recover from; the one checkpoint happens at close."""
    from gffbase import ingest

    seen = []
    real = ingest._add_features_primary_key

    def spy(con):
        seen.append(con.execute("SELECT current_setting('checkpoint_threshold')").fetchone()[0])
        return real(con)

    monkeypatch.setattr(ingest, "_add_features_primary_key", spy)
    path = tmp_path / "a.gff3"
    path.write_text(GFF3)
    con, _ = from_file(str(path), str(tmp_path / "a.duckdb"))
    assert "GiB" in seen[0] and float(seen[0].split()[0]) > 900
    assert con.execute("SELECT current_setting('checkpoint_threshold')").fetchone()[0] == "16.0 MiB"


def test_a_handle_opened_on_a_file_uses_the_same_default(tmp_path, monkeypatch):
    """DuckDB's own default is every core, which a per-call query pays for."""
    import os

    from gffbase import FeatureDB
    from gffbase._dbutil import DEFAULT_THREADS

    path = tmp_path / "a.gff3"
    path.write_text(GFF3)
    con, _ = from_file(str(path), str(tmp_path / "a.duckdb"))
    con.close()

    def opened():
        db = FeatureDB(str(tmp_path / "a.duckdb"), read_only=True)
        try:
            return int(db.conn.execute("SELECT current_setting('threads')").fetchone()[0])
        finally:
            db.close()

    monkeypatch.delenv("GFFBASE_THREADS", raising=False)
    monkeypatch.delenv("GFFUTILS2_THREADS", raising=False)
    assert opened() == min(DEFAULT_THREADS, os.cpu_count() or 1)
    monkeypatch.setenv("GFFBASE_THREADS", "2")
    assert opened() == 2
