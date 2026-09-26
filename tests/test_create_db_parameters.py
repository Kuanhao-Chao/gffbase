"""`create_db` parameters that 0.2.0 accepted and silently ignored.

`gtf_transcript_key` / `gtf_gene_key` were hard-coded to `transcript_id` /
`gene_id` in every ingest statement; `dialect=` was stored nowhere and
changed nothing; `verbose=True` printed nothing (so `gffbase create`, which
asks for progress unless `--quiet`, was silent); `text_factory` and
`default_encoding` configure sqlite3 and cannot mean anything here, but said
so nowhere; and `add_relation(level=2)` stored a direct edge, so
`children(level=1)` returned a grandchild.
"""

from __future__ import annotations

import logging
import os
import subprocess
import sys
import warnings
from pathlib import Path

import gffbase
import pytest
from gffbase import FeatureDB, create_db

# Wherever this process imported gffbase from -- the source tree or an
# installed wheel -- so the child runs the same code.
PYTHON_SRC = Path(gffbase.__file__).resolve().parents[1]

GTF = (
    'chr1\tt\texon\t1\t100\t.\t+\t.\tgene_id "G1"; gene_name "BRCA1"; transcript_id "T1"; tx "A";\n'
    'chr1\tt\texon\t200\t300\t.\t+\t.\tgene_id "G1"; gene_name "BRCA1"; transcript_id "T1"; tx "A";\n'
    'chr1\tt\texon\t400\t500\t.\t+\t.\tgene_id "G2"; gene_name "BRCA1"; transcript_id "T2"; tx "B";\n'
)


def parents_of(db) -> list[tuple[str, str]]:
    return sorted((p.id, c.id) for c in db.all_features() for p in db.parents(c, level=1))


def non_exons(db) -> list[tuple]:
    return sorted(
        (f.featuretype, f.id, f.start, f.end) for f in db.all_features() if f.featuretype != "exon"
    )


# ---------------------------------------------------------------------------
# gtf_transcript_key / gtf_gene_key
# ---------------------------------------------------------------------------


def test_the_default_keys():
    db = create_db(GTF, ":memory:", from_string=True)
    assert non_exons(db) == [
        ("gene", "G1", 1, 300),
        ("gene", "G2", 400, 500),
        ("transcript", "T1", 1, 300),
        ("transcript", "T2", 400, 500),
    ]


def test_gtf_gene_key_groups_transcripts_into_genes():
    db = create_db(GTF, ":memory:", from_string=True, gtf_gene_key="gene_name")
    # One gene now: both transcripts share gene_name BRCA1. The default
    # id_spec still names it by gene_id, the most common among its children.
    assert non_exons(db) == [
        ("gene", "G1", 1, 500),
        ("transcript", "T1", 1, 300),
        ("transcript", "T2", 400, 500),
    ]
    assert ("G1", "T2") in parents_of(db)


def test_gtf_gene_key_with_a_matching_id_spec():
    db = create_db(
        GTF,
        ":memory:",
        from_string=True,
        gtf_gene_key="gene_name",
        id_spec={"gene": "gene_name", "transcript": "transcript_id"},
    )
    assert [f.id for f in db.features_of_type("gene")] == ["BRCA1"]
    assert sorted(f.id for f in db.children("BRCA1", level=1)) == ["T1", "T2"]


def test_gtf_transcript_key_groups_exons_into_transcripts():
    gtf = GTF.replace('transcript_id "T2"', 'transcript_id "T1"')  # tx still splits A / B
    db = create_db(gtf, ":memory:", from_string=True, gtf_transcript_key="tx")
    transcripts = sorted((f.start, f.end) for f in db.features_of_type("transcript"))
    assert transcripts == [(1, 300), (400, 500)]


@pytest.mark.parity
def test_matches_gffutils_when_the_id_spec_names_the_keys():
    gffutils = pytest.importorskip("gffutils")
    kw = {
        "gtf_gene_key": "gene_name",
        "id_spec": {"gene": "gene_name", "transcript": "transcript_id"},
    }
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        oracle = gffutils.create_db(GTF, ":memory:", from_string=True, **kw)
    ours = create_db(GTF, ":memory:", from_string=True, **kw)
    assert non_exons(ours) == non_exons(oracle)
    assert parents_of(ours) == parents_of(oracle)


# ---------------------------------------------------------------------------
# dialect=
# ---------------------------------------------------------------------------


def test_an_explicit_dialect_decides_the_format():
    # One GFF3-looking line in a GTF: inference would call it GFF3.
    text = "chr1\tt\texon\t1\t100\t.\t+\t.\tgene_id=G1;transcript_id=T1\n"
    assert create_db(text, ":memory:", from_string=True).dialect["fmt"] == "gff3"
    db = create_db(text, ":memory:", from_string=True, dialect={"fmt": "gtf"})
    assert db.dialect["fmt"] == "gtf"
    assert db.dialect["keyval separator"] == " "  # completed from GTF defaults
    assert db.fmt == "gtf"


def test_a_bad_dialect_is_refused_before_any_work():
    with pytest.raises(ValueError, match="fmt"):
        create_db(GTF, ":memory:", from_string=True, dialect={"fmt": "bed"})
    with pytest.raises(ValueError, match="force_dialect_check"):
        create_db(
            GTF, ":memory:", from_string=True, dialect={"fmt": "gtf"}, force_dialect_check=True
        )


# ---------------------------------------------------------------------------
# verbose
# ---------------------------------------------------------------------------


def _run(code: str, tmp_path) -> subprocess.CompletedProcess:
    """A fresh interpreter with no logging configured -- a user's script."""
    env = {**os.environ, "PYTHONPATH": str(PYTHON_SRC)}
    return subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True,
        text=True,
        cwd=tmp_path,
        env=env,
        timeout=120,
        check=True,
    )


def test_verbose_reports_progress_on_stderr(tmp_path):
    (tmp_path / "x.gtf").write_text(GTF)
    proc = _run("import gffbase; gffbase.create_db('x.gtf', ':memory:', verbose=True)", tmp_path)
    assert "gffbase: parsing x.gtf" in proc.stderr
    assert "inferring GTF transcripts and genes" in proc.stderr
    assert "done: 7 features" in proc.stderr
    quiet = _run("import gffbase; gffbase.create_db('x.gtf', ':memory:')", tmp_path)
    assert quiet.stderr == ""


def test_quiet_by_default(capsys):
    create_db(GTF, ":memory:", from_string=True)
    assert capsys.readouterr().err == ""


def test_verbose_leaves_the_logger_as_it_found_it():
    logger = logging.getLogger("gffbase.ingest")
    level, handlers = logger.level, list(logger.handlers)
    create_db(GTF, ":memory:", from_string=True, verbose="debug")
    assert (logger.level, logger.handlers) == (level, handlers)


def test_an_application_logging_setup_receives_the_messages(caplog):
    with caplog.at_level(logging.INFO, logger="gffbase.ingest"):
        create_db(GTF, ":memory:", from_string=True, verbose=True)
    assert any("done:" in r.getMessage() for r in caplog.records)


def test_the_cli_create_reports_progress_unless_quiet(tmp_path):
    (tmp_path / "x.gtf").write_text(GTF)
    cli = "from gffbase.cli import main; main({})"
    loud = _run(cli.format(["create", "x.gtf", "--output", "a.duckdb"]), tmp_path)
    assert "done:" in loud.stderr
    quiet = _run(cli.format(["create", "x.gtf", "--output", "b.duckdb", "--quiet"]), tmp_path)
    assert quiet.stderr == ""


# ---------------------------------------------------------------------------
# sqlite3-only options
# ---------------------------------------------------------------------------


def test_sqlite_only_options_warn_when_set(monkeypatch):
    from gffbase import interface

    monkeypatch.setattr(interface, "_SQLITE_ONLY_WARNED", set())
    with pytest.warns(UserWarning, match="text_factory=.*no effect"):
        create_db(GTF, ":memory:", from_string=True, text_factory=bytes)
    db = create_db(GTF, ":memory:", from_string=True)
    with pytest.warns(UserWarning, match="default_encoding=.*no effect"):
        FeatureDB(db.conn, default_encoding="latin-1")


def test_the_defaults_do_not_warn(recwarn):
    create_db(GTF, ":memory:", from_string=True, text_factory=str)
    assert not [w for w in recwarn if "no effect" in str(w.message)]


# ---------------------------------------------------------------------------
# add_relation(level=)
# ---------------------------------------------------------------------------


def test_add_relation_refuses_a_level_it_cannot_store():
    db = create_db(
        "chr1\tt\tgene\t1\t100\t.\t+\t.\tID=g\nchr1\tt\texon\t1\t50\t.\t+\t.\tID=e\n",
        ":memory:",
        from_string=True,
    )
    with pytest.raises(ValueError, match="level=2"):
        db.add_relation("g", "e", level=2)
    assert list(db.children("g", level=1)) == []
    db.add_relation("g", "e", level=1)
    assert [f.id for f in db.children("g", level=1)] == ["e"]
