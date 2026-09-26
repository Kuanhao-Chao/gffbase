"""`gffbase create` flags that no test exercised: --disable-infer-genes,
--disable-infer-transcripts, --merge, --force and --mode."""

from __future__ import annotations

import pytest
from gffbase import FeatureDB
from gffbase.cli import main

GTF = (
    'chr1\tt\texon\t1\t50\t.\t+\t.\tgene_id "G1"; transcript_id "T1";\n'
    'chr1\tt\texon\t80\t100\t.\t+\t.\tgene_id "G1"; transcript_id "T1";\n'
)
SPLIT_CDS = (
    "##gff-version 3\n"
    "chr1\tt\tmRNA\t1\t100\t.\t+\t.\tID=t1\n"
    "chr1\tt\tCDS\t1\t30\t.\t+\t0\tID=c1;Parent=t1\n"
    "chr1\tt\tCDS\t60\t100\t.\t+\t0\tID=c1;Parent=t1\n"
)


def _create(tmp_path, text: str, *flags: str, name: str = "in.gff") -> FeatureDB:
    src = tmp_path / name
    src.write_text(text)
    out = tmp_path / "out.duckdb"
    assert main(["create", str(src), "--output", str(out), "--quiet", *flags]) == 0
    return FeatureDB(str(out), read_only=True)


def _types(db) -> dict[str, int]:
    return {ft: db.count_features_of_type(ft) for ft in db.featuretypes()}


def test_default_infers_genes_and_transcripts(tmp_path):
    assert _types(_create(tmp_path, GTF, name="in.gtf")) == {"exon": 2, "gene": 1, "transcript": 1}


def test_disable_infer_genes(tmp_path):
    db = _create(tmp_path, GTF, "--disable-infer-genes", name="in.gtf")
    assert _types(db) == {"exon": 2, "transcript": 1}


def test_disable_infer_transcripts(tmp_path):
    db = _create(tmp_path, GTF, "--disable-infer-transcripts", name="in.gtf")
    assert "transcript" not in _types(db)


@pytest.mark.parametrize(
    ("strategy", "cds_ids"),
    [("create_unique", ["c1", "c1_1"]), ("warning", ["c1"])],
)
def test_merge_strategy(tmp_path, strategy, cds_ids):
    db = _create(tmp_path, SPLIT_CDS, "--merge", strategy)
    assert sorted(f.id for f in db.features_of_type("CDS")) == cds_ids


def test_merge_error_is_reported_not_traced(tmp_path, capsys):
    src = tmp_path / "in.gff"
    src.write_text(SPLIT_CDS)
    rc = main(["create", str(src), "--output", str(tmp_path / "o.duckdb"), "--merge", "error"])
    assert rc == 1
    assert "DuplicateIDError" in capsys.readouterr().err


def test_mode_strict_fuses_the_split_cds(tmp_path):
    db = _create(tmp_path, SPLIT_CDS, "--mode", "strict", "--merge", "error")
    (cds,) = db.features_of_type("CDS")
    assert (cds.id, cds.start, cds.end, len(cds.segments)) == ("c1", 1, 100, 2)


def test_force_overwrites_and_its_absence_refuses(tmp_path, capsys):
    src = tmp_path / "in.gtf"
    src.write_text(GTF)
    out = str(tmp_path / "out.duckdb")
    assert main(["create", str(src), "--output", out, "--quiet"]) == 0
    assert main(["create", str(src), "--output", out, "--quiet"]) == 1
    assert "already exists" in capsys.readouterr().err
    assert main(["create", str(src), "--output", out, "--quiet", "--force"]) == 0
