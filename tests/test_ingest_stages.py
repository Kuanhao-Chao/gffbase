"""`IngestStats.stages`: where ingest time goes, per stage."""

from __future__ import annotations

import time

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
