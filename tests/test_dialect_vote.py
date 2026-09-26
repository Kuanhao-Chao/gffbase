"""The inferred dialect is gffutils' weighted, per-key vote, in both engines.

Through 0.2.1 gffbase took a plain majority for the format and OR-ed the
boolean flags. The OR meant one quoted line re-quoted every value of a file on
output; the unweighted count let an attribute-less line (a five-column row)
outvote a real one. gffutils decides each key separately, weights a line by its
number of distinct attributes, and breaks ties by first appearance
(`helpers._choose_dialect`).
"""

from __future__ import annotations

import pytest
from gffbase import native_available
from gffbase.dialect import merge_dialects
from gffbase.parser import parse_gff

ENGINES = ["python"] + (["rust"] if native_available() else [])

KEYS = (
    "fmt",
    "field separator",
    "keyval separator",
    "leading semicolon",
    "trailing semicolon",
    "quoted GFF2 values",
    "repeated keys",
)

CASES = {
    # One quoted value among plain GFF3 lines: the vote says unquoted.
    "one_quoted_line": (
        "chr1\tt\tgene\t1\t10\t.\t+\t.\tID=a;Name=x\n"
        "chr1\tt\tgene\t20\t30\t.\t+\t.\tID=b;Name=y\n"
        'chr1\tt\tgene\t40\t50\t.\t+\t.\tID="c"\n'
    ),
    # A rich `; `-separated line outweighs two thin `;` lines.
    "weighted_separator": (
        "chr1\tt\tgene\t1\t10\t.\t+\t.\tID=g1;\n"
        "chr1\tt\tgene\t20\t30\t.\t+\t.\tID=g2;\n"
        "chr1\tt\tCDS\t1\t10\t.\t+\t0\tID=c1; Parent=g1; a=1; b=2; c=3; d=4;\n"
    ),
    # Attribute-less rows weigh nothing, so the GTF line decides.
    "attributeless_rows": (
        "chr1\tt\texon\t1\t10\n"
        "chr1\tt\texon\t20\t30\n"
        'chr1\tt\texon\t40\t50\t.\t+\t.\tgene_id "G"; transcript_id "T";\n'
    ),
    # An exact tie goes to the value seen first.
    "tie_goes_to_first": (
        'chr1\tt\texon\t1\t10\t.\t+\t.\tgene_id "G"; transcript_id "T";\n'
        "chr1\tt\texon\t20\t30\t.\t+\t.\tID=x;Name=y\n"
    ),
}

EXPECTED = {
    "one_quoted_line": {"fmt": "gff3", "quoted GFF2 values": False},
    "weighted_separator": {"field separator": "; "},
    "attributeless_rows": {"fmt": "gtf"},
    "tie_goes_to_first": {"fmt": "gtf"},
}


def _dialect(tmp_path, name: str, engine: str) -> dict:
    path = tmp_path / f"{name}.gff"
    path.write_text(CASES[name])
    return parse_gff(str(path), engine=engine, strict=False, validation="gffutils").dialect()


@pytest.mark.parametrize("engine", ENGINES)
@pytest.mark.parametrize("name", sorted(CASES))
def test_the_vote(tmp_path, engine, name):
    dialect = _dialect(tmp_path, name, engine)
    for key, value in EXPECTED[name].items():
        assert dialect[key] == value, (key, dialect)


@pytest.mark.skipif(len(ENGINES) < 2, reason="needs both engines")
@pytest.mark.parametrize("name", sorted(CASES))
def test_the_engines_agree(tmp_path, name):
    python, rust = (_dialect(tmp_path, name, e) for e in ("python", "rust"))
    assert {k: python[k] for k in KEYS} == {k: rust[k] for k in KEYS}


def test_no_samples_is_the_default():
    assert merge_dialects([])["fmt"] == "gff3"


@pytest.mark.parity
@pytest.mark.parametrize("name", sorted(CASES))
def test_matches_gffutils(tmp_path, name):
    gffutils = pytest.importorskip("gffutils")
    path = tmp_path / f"{name}.gff"
    path.write_text(CASES[name])
    oracle = gffutils.DataIterator(str(path)).dialect
    ours = parse_gff(str(path), strict=False, validation="gffutils").dialect()
    for key in EXPECTED[name]:
        assert ours[key] == oracle[key], key
