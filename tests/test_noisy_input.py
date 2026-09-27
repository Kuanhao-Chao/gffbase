"""Noisy and non-standard input: what real files do, and what gffbase makes of it.

Each case here was found on a real corpus or by the compat fuzzer, and each
used to load wrongly -- usually silently. Every database built here must also
pass full validation.
"""

from __future__ import annotations

import pytest
from gffbase import create_db, native_available
from gffbase.ingest import from_file
from gffbase.parser import parse_bytes

ENGINES = ["python"] + (["rust"] if native_available() else [])

GENE = "chr1\tt\tgene\t1\t100\t.\t+\t.\tID=g1\n"
MRNA = "chr1\tt\tmRNA\t1\t100\t.\t+\t.\tID=t1;Parent=g1\n"


def _db(text: str, **kw):
    db = create_db(text, ":memory:", from_string=True, **kw)
    report = db.validate(level="full", sample=None)
    assert report.ok, report.errors
    return db


def _ids(db) -> list[str]:
    return [f.id for f in db.all_features()]


# ---------------------------------------------------------------------------
# Lines
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("engine", ENGINES)
@pytest.mark.parametrize("ending", ["\n", "\r\n", "\r"], ids=["lf", "crlf", "lone_cr"])
def test_every_line_ending_splits_lines(engine, ending):
    """A lone CR (classic Mac) used to be no line break at all: the first
    feature swallowed the rest of the file into its ID."""
    data = (GENE + MRNA).replace("\n", ending).encode()
    records = list(parse_bytes(data, engine=engine, validation="gffutils"))
    assert [dict(r.attributes_dict())["ID"] for r in records] == [["g1"], ["t1"]]


def test_a_cr_only_file_keeps_its_hierarchy():
    db = _db((GENE + MRNA).replace("\n", "\r"))
    assert [f.id for f in db.children("g1")] == ["t1"]


@pytest.mark.parametrize("engine", ENGINES)
def test_a_byte_order_mark_is_not_part_of_the_seqid(engine):
    data = b"\xef\xbb\xbf" + GENE.encode()
    (record,) = parse_bytes(data, engine=engine, validation="gffutils")
    assert record.seqid == "chr1"


@pytest.mark.parametrize("engine", ENGINES)
@pytest.mark.parametrize("blank", ["   \n", "\t\t\n", " \t \n"])
def test_whitespace_only_lines_are_blank(engine, blank):
    """One used to become a feature named after its spaces."""
    records = list(parse_bytes((blank + GENE).encode(), engine=engine, validation="gffutils"))
    assert [r.seqid for r in records] == ["chr1"]


@pytest.mark.parametrize("engine", ENGINES)
def test_a_line_holding_a_nul_byte_is_binary_and_reported(engine):
    """Once a lone CR ends a line, a binary file splits into runs of control
    bytes that are valid UTF-8; compat mode kept each as a feature."""
    it = parse_bytes(b"\x00\x01\x02\r" + GENE.encode(), engine=engine, validation="gffutils")
    assert [r.seqid for r in it] == ["chr1"]
    (warning,) = it.warnings
    assert warning["line_no"] == 1 and "NUL byte" in warning["message"]


def _tar(members: dict[str, bytes], fmt=None, gz: bool = False) -> bytes:
    import gzip
    import io
    import tarfile

    out = io.BytesIO()
    with tarfile.open(fileobj=out, mode="w", format=fmt or tarfile.USTAR_FORMAT) as tar:
        for name, data in members.items():
            info = tarfile.TarInfo(name)
            info.size = len(data)
            if fmt == tarfile.PAX_FORMAT:
                info.pax_headers = {"comment": "x" * 50}
            tar.addfile(info, io.BytesIO(data))
    raw = out.getvalue()
    return gzip.compress(raw) if gz else raw


def _tar_cases():
    import tarfile

    body = (GENE + MRNA).encode()
    return {
        "ustar": _tar({"a.gff3": body}),
        "ustar_gz": _tar({"a.gff3": body}, gz=True),
        "pax": _tar({"a.gff3": body}, fmt=tarfile.PAX_FORMAT),
        "gnu_long_name": _tar({"d/" * 60 + "a.gff3": body}, fmt=tarfile.GNU_FORMAT),
    }


@pytest.mark.parametrize("engine", ENGINES)
@pytest.mark.parametrize("case", sorted(_tar_cases()))
def test_a_one_file_tar_archive_is_read_as_that_file(engine, case, tmp_path):
    """FlyBase publishes `dmel-all-r6.69.gff.gz` as a gzipped tar: the 512-byte
    header used to become part of line 1 and the zero padding a last line."""
    path = tmp_path / "a.gff.gz"
    path.write_bytes(_tar_cases()[case])
    con, stats = from_file(str(path), ":memory:", engine=engine)
    assert con.execute("SELECT id FROM features ORDER BY file_order").fetchall() == [
        ("g1",),
        ("t1",),
    ]
    assert not stats.warnings


@pytest.mark.parametrize("engine", ENGINES)
def test_a_tar_archive_of_several_files_is_refused(engine):
    data = _tar({"a.gff3": GENE.encode(), "b.gff3": MRNA.encode()})
    with pytest.raises(ValueError, match="more than one file"):
        list(parse_bytes(data, engine=engine, validation="gffutils"))


@pytest.mark.parametrize("engine", ENGINES)
def test_ustar_at_byte_257_of_a_gff_is_still_text(engine):
    """Without a valid header checksum it is not an archive."""
    pad = "chr1\tt\tgene\t1\t100\t.\t+\t.\tID=g0;Note="
    line = pad + "x" * (257 - len(pad)) + "ustar\n"
    records = list(parse_bytes((line + GENE).encode(), engine=engine, validation="gffutils"))
    assert [dict(r.attributes_dict())["ID"] for r in records] == [["g0"], ["g1"]]


# ---------------------------------------------------------------------------
# Column 9
# ---------------------------------------------------------------------------


def test_a_dot_column_9_means_no_attributes():
    """`.` used to parse as an attribute KEY named `.` (gffutils writes it back
    as `. ""`). It now reads and writes like an empty column 9."""
    db = _db("chr1\tt\tgene\t1\t100\t.\t+\t.\t.\n")
    (gene,) = db.all_features()
    assert dict(gene.attributes) == {}
    assert str(gene) == "chr1\tt\tgene\t1\t100\t.\t+\t.\t"


def test_a_trailing_comma_in_parent_makes_no_empty_edge():
    db = _db(GENE + "chr1\tt\tmRNA\t1\t100\t.\t+\t.\tID=t1;Parent=g1,\n")
    assert db.execute("SELECT parent, child FROM edges").fetchall() == [("g1", "t1")]
    assert db["t1"].attributes["Parent"] == ["g1", ""]  # as gffutils reads it


def test_spaces_around_the_separator_are_not_part_of_the_value():
    db = _db("chr1\tt\tgene\t1\t100\t.\t+\t.\tID = g1 ; Name = x\n")
    assert _ids(db) == ["g1"]
    assert db["g1"].attributes["Name"] == ["x"]


def test_a_value_that_is_only_a_space_is_still_data():
    db = _db("chr1\tt\tgene\t1\t100\t.\t+\t.\tID=g1;Note= \n")
    assert db["g1"].attributes["Note"] == [" "]


# ---------------------------------------------------------------------------
# GTF
# ---------------------------------------------------------------------------


def test_a_cds_only_gtf_gets_its_hierarchy():
    """Gene predictors write CDS rows and no exons. Transcripts used to be
    inferred from exons only, so none were, and every CDS was an orphan."""
    db = _db(
        'chr1\tt\tCDS\t1\t100\t.\t+\t0\tgene_id "G1"; transcript_id "T1";\n'
        'chr1\tt\tCDS\t200\t300\t.\t+\t0\tgene_id "G1"; transcript_id "T1";\n'
    )
    assert (db["T1"].start, db["T1"].end) == (1, 300)
    assert [f.id for f in db.children("G1", level=1)] == ["T1"]
    assert len(list(db.children("T1", level=1))) == 2


def test_an_explicit_subfeature_is_honoured_even_if_absent():
    db = _db(
        'chr1\tt\tCDS\t1\t100\t.\t+\t0\tgene_id "G1"; transcript_id "T1";\n',
        gtf_subfeature="exon",
    )
    # The default is the one that falls back; asking for exon explicitly is
    # indistinguishable from the default here, so this documents the rule
    # rather than a difference.
    assert "T1" in db


def test_a_row_without_a_transcript_id_hangs_from_its_gene_and_is_reported():
    db = _db(
        'chr1\tt\texon\t1\t100\t.\t+\t.\tgene_id "G1";\n'
        'chr1\tt\texon\t200\t300\t.\t+\t.\tgene_id "G1"; transcript_id "T1";\n'
    )
    assert sorted(f.id for f in db.children("G1", level=1)) == ["T1", "exon_1"]
    (warning,) = [w for w in db.warnings if w["kind"] == "GtfMissingTranscriptId"]
    assert "exon_1" in warning["message"]


AUGUSTUS = (
    "chr1\tAUGUSTUS\tgene\t100\t900\t0.9\t+\t.\tg1\n"
    "chr1\tAUGUSTUS\ttranscript\t100\t900\t0.9\t+\t.\tg1.t1\n"
    'chr1\tAUGUSTUS\tstart_codon\t100\t102\t.\t+\t0\ttranscript_id "g1.t1"; gene_id "g1";\n'
    'chr1\tAUGUSTUS\tCDS\t100\t300\t0.8\t+\t0\ttranscript_id "g1.t1"; gene_id "g1";\n'
    'chr1\tAUGUSTUS\tCDS\t500\t900\t0.9\t+\t2\ttranscript_id "g1.t1"; gene_id "g1";\n'
    'chr1\tAUGUSTUS\tstop_codon\t898\t900\t.\t+\t0\ttranscript_id "g1.t1"; gene_id "g1";\n'
)


def test_augustus_bare_ids_name_their_rows():
    """AUGUSTUS writes gene and transcript rows as a bare id in column 9."""
    db = _db(AUGUSTUS)
    assert [f.id for f in db.children("g1", level=1)] == ["g1.t1"]
    assert len(list(db.children("g1.t1", level=1))) == 4
    assert db["g1"].attributes["gene_id"] == ["g1"]


def test_inferred_parents_carry_their_attributes():
    """Synthesis left column 9 empty, so an inferred gene read back with no
    attributes and was written out without its gene_id."""
    db = _db('chr1\tt\texon\t1\t100\t.\t+\t.\tgene_id "G1"; transcript_id "T1";\n')
    assert dict(db["G1"].attributes) == {"gene_id": ["G1"]}
    assert dict(db["T1"].attributes) == {"transcript_id": ["T1"], "gene_id": ["G1"]}
    assert str(db["T1"]).endswith('transcript_id "T1"; gene_id "G1";')


# ---------------------------------------------------------------------------
# Identifiers
# ---------------------------------------------------------------------------


def test_create_unique_skips_suffixes_the_file_already_uses():
    """Liftoff names extra copies `<id>_1`. When `X_1` came first, a later
    duplicate of `X` was renamed `X_1` too, and ingest died on a DuckDB
    primary-key violation."""
    text = (
        "chr1\tt\tgene\t1\t9\t.\t+\t.\tID=X_1\n"
        "chr1\tt\tgene\t20\t29\t.\t+\t.\tID=X\n"
        "chr1\tt\tgene\t40\t49\t.\t+\t.\tID=X\n"
    )
    for strategy in ("create_unique", "merge"):
        assert sorted(_ids(_db(text, merge_strategy=strategy))) == ["X", "X_1", "X_2"]


def test_a_generated_id_skips_one_the_file_used_literally():
    db = _db("chr1\tt\texon\t1\t9\t.\t+\t.\tID=exon_1\nchr1\tt\texon\t20\t29\t.\t+\t.\tName=noid\n")
    assert sorted(_ids(db)) == ["exon_1", "exon_2"]


# ---------------------------------------------------------------------------
# Coordinates
# ---------------------------------------------------------------------------


def test_a_feature_spanning_a_circular_origin_loads_and_is_found(tmp_path):
    """NCBI writes a feature across the origin of a circular replicon with an
    end past the sequence length."""
    text = (
        "##gff-version 3\n"
        "NC_1\tRefSeq\tregion\t1\t1000\t.\t+\t.\tID=r;Is_circular=true\n"
        "NC_1\tRefSeq\tgene\t950\t1050\t.\t+\t.\tID=spans_origin\n"
    )
    path = tmp_path / "circular.gff3"
    path.write_text(text)
    for mode in ("compat", "strict"):
        db = create_db(str(path), ":memory:", mode=mode)
        assert "spans_origin" in [f.id for f in db.region("NC_1:990-1000")]


@pytest.mark.parametrize("engine", ENGINES)
def test_both_engines_build_the_same_noisy_database(tmp_path, engine):
    """The whole noisy set through `from_file`, per engine."""
    path = tmp_path / "noisy.gtf"
    path.write_text(AUGUSTUS)
    con, _ = from_file(str(path), ":memory:", engine=engine)
    assert con.execute("SELECT featuretype, id FROM features ORDER BY file_order, id").fetchall()[
        :2
    ] == [("gene", "g1"), ("transcript", "g1.t1")]
