"""Dialect sampling: which records decide whether a file is GTF or GFF3.

Two defects, both in 0.2.0:

* The Rust engine stopped sampling at the first record it could not parse,
  where the Python engine skipped it and kept going. A GTF whose first line
  was malformed therefore left Rust with no samples, the dialect defaulted to
  GFF3, and in compat mode -- which keeps loading past the bad line -- the
  gene/transcript hierarchy was silently never built.
* The engines disagreed on how many records `checklines` means, and neither
  matched gffutils, whose `peek(n)` samples `n + 1`. `checklines=0` sampled
  nothing in Rust and one record in Python.
"""

from __future__ import annotations

import pytest
from gffbase import GFFFormatError, native_available
from gffbase.ingest import from_file
from gffbase.parser import parse_gff

ENGINES = ["python"] + (["rust"] if native_available() else [])

# Two attributes each, so gffutils -- which weights a sample by its attribute
# count -- votes exactly as an unweighted majority does.
GTF = 'chr1\tt\texon\t{s}\t{e}\t.\t+\t.\tgene_id "G{i}"; transcript_id "T{i}";\n'
GFF3 = "chr1\tt\texon\t{s}\t{e}\t.\t+\t.\tID=x{i};Name=n{i}\n"


def _line(template: str, i: int) -> str:
    return template.format(s=1 + 100 * i, e=50 + 100 * i, i=i)


def _write(tmp_path, name: str, text: str) -> str:
    path = tmp_path / name
    path.write_bytes(text.encode("utf-8") if isinstance(text, str) else text)
    return str(path)


def _fmt(path: str, engine: str, **kw) -> str:
    return parse_gff(path, engine=engine, strict=False, validation="gffutils", **kw).dialect()[
        "fmt"
    ]


CLEAN_GTF = "".join(_line(GTF, i) for i in range(4))

BAD_FIRST_LINES = {
    "non_integer_start": 'chr1\tt\texon\tX\t50\t.\t+\t.\tgene_id "G0";\n',
    "non_integer_end": 'chr1\tt\texon\t1\tY\t.\t+\t.\tgene_id "G0";\n',
    "invalid_utf8": b'chr1\tt\texon\t1\t50\t.\t+\t.\tgene_id "\xff\xfe";\n',
}


@pytest.mark.parametrize("engine", ENGINES)
@pytest.mark.parametrize("bad", sorted(BAD_FIRST_LINES))
def test_a_bad_first_line_does_not_end_sampling(tmp_path, engine, bad):
    head = BAD_FIRST_LINES[bad]
    head = head if isinstance(head, bytes) else head.encode()
    path = _write(tmp_path, "x.gtf", head + CLEAN_GTF.encode())
    assert _fmt(path, engine) == "gtf"


@pytest.mark.parametrize("engine", ENGINES)
@pytest.mark.parametrize("bad", sorted(BAD_FIRST_LINES))
def test_a_bad_first_line_still_gets_the_gtf_hierarchy(tmp_path, engine, bad):
    head = BAD_FIRST_LINES[bad]
    head = head if isinstance(head, bytes) else head.encode()
    path = _write(tmp_path, "x.gtf", head + CLEAN_GTF.encode())
    con, _ = from_file(path, ":memory:", engine=engine)
    counts = dict(con.execute("SELECT featuretype, count(*) FROM features GROUP BY 1").fetchall())
    assert counts == {"exon": 4, "transcript": 4, "gene": 4}


@pytest.mark.parametrize("engine", ENGINES)
def test_checklines_zero_still_samples_one_record(tmp_path, engine):
    path = _write(tmp_path, "x.gtf", CLEAN_GTF)
    assert _fmt(path, engine, checklines=0) == "gtf"


# GTF, GFF3, GFF3, GTF, GTF, GTF. A sample of the first k records votes:
#   k=1 gtf | k=3 gff3 (1:2) | k=4 tie -> gff3 | k=5 gtf (3:2)
MIXED = "".join(_line(t, i) for i, t in enumerate([GTF, GFF3, GFF3, GTF, GTF, GTF]))
EXPECTED_FMT = {0: "gtf", 2: "gff3", 4: "gtf"}


@pytest.mark.parametrize("engine", ENGINES)
@pytest.mark.parametrize("checklines", sorted(EXPECTED_FMT))
def test_checklines_samples_one_more_record_than_it_names(tmp_path, engine, checklines):
    """`checklines=4` samples five records, so the fifth breaks the 2:2 tie."""
    path = _write(tmp_path, "mixed.gtf", MIXED)
    assert _fmt(path, engine, checklines=checklines) == EXPECTED_FMT[checklines]


@pytest.mark.skipif(len(ENGINES) < 2, reason="needs both engines")
@pytest.mark.parametrize("checklines", range(8))
def test_the_engines_agree_for_every_checklines(tmp_path, checklines):
    path = _write(tmp_path, "mixed.gtf", MIXED)
    assert _fmt(path, "rust", checklines=checklines) == _fmt(path, "python", checklines=checklines)


@pytest.mark.parametrize("engine", ENGINES)
def test_strict_mode_still_raises_at_the_bad_line(tmp_path, engine):
    """Sampling past a bad record must not swallow it: iteration re-reads it."""
    path = _write(tmp_path, "x.gtf", BAD_FIRST_LINES["non_integer_start"] + CLEAN_GTF)
    with pytest.raises(GFFFormatError) as info:
        list(parse_gff(path, engine=engine, strict=True, validation="ncbi"))
    assert "line 1" in str(info.value)


@pytest.mark.parity
@pytest.mark.parametrize("checklines", sorted(EXPECTED_FMT))
def test_the_sample_size_matches_gffutils(tmp_path, checklines):
    gffutils = pytest.importorskip("gffutils")
    path = _write(tmp_path, "mixed.gtf", MIXED)
    oracle = gffutils.DataIterator(path, checklines=checklines).dialect["fmt"]
    assert oracle == EXPECTED_FMT[checklines]
