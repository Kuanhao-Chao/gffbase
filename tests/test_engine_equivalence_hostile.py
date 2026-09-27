"""The Rust and Python engines agree on every upstream fixture and on hostile
input, under both rule sets: the same features (all columns, raw column 9
and parsed pairs), the same warnings, dialect and directives -- or the same
error at the same line.

`test_engine_equivalence.py` compared two clean files only. The Python engine
is the one a wheel-less install runs, so any disagreement is a result that
depends on how gffbase was installed. This sweep found `int()` accepting
`1_000` and Arabic-Indic digits as coordinates, which Rust rejects.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from gffbase import native_available
from gffbase.parser import parse_gff

pytestmark = pytest.mark.skipif(not native_available(), reason="needs both engines")

UPSTREAM = sorted(p for p in (Path(__file__).parent / "data" / "upstream").iterdir() if p.is_file())

HOSTILE = {
    "underscore_int": "chr1\tt\tgene\t1_000\t2000\t.\t+\t.\tID=a\n",
    "space_int": "chr1\tt\tgene\t 5 \t20\t.\t+\t.\tID=a\n",
    "unicode_digits": "chr1\tt\tgene\t١٢\t20\t.\t+\t.\tID=a\n",
    "plus_sign": "chr1\tt\tgene\t+5\t20\t.\t+\t.\tID=a\n",
    "negative_start": "chr1\tt\tgene\t-5\t20\t.\t+\t.\tID=a\n",
    "float_coordinate": "chr1\tt\tgene\t5.0\t20\t.\t+\t.\tID=a\n",
    "huge_coordinate": "chr1\tt\tgene\t99999999999999999999\t20\t.\t+\t.\tID=a\n",
    "score_nan": "chr1\tt\tgene\t5\t20\tnan\t+\t.\tID=a\n",
    "score_inf": "chr1\tt\tgene\t5\t20\tinf\t+\t.\tID=a\n",
    "score_padded": "chr1\tt\tgene\t5\t20\t 1.5 \t+\t.\tID=a\n",
    "dot_padded": "chr1\tt\tgene\t5\t20\t . \t+\t.\tID=a\n",
    "bom": "﻿chr1\tt\tgene\t5\t20\t.\t+\t.\tID=a\n",
    "lone_cr": "chr1\tt\tgene\t5\t20\t.\t+\t.\tID=a\rchr1\tt\tgene\t30\t40\t.\t+\t.\tID=b\r",
    "crlf": "chr1\tt\tgene\t5\t20\t.\t+\t.\tID=a\r\n",
    "whitespace_line": "   \nchr1\tt\tgene\t5\t20\t.\t+\t.\tID=a\n",
    "tabs_line": "\t\t\nchr1\tt\tgene\t5\t20\t.\t+\t.\tID=a\n",
    "dot_column9": "chr1\tt\tgene\t5\t20\t.\t+\t.\t.\n",
    "trailing_tab": "chr1\tt\tgene\t5\t20\t.\t+\t.\tID=a\t\n",
    "parent_trailing_comma": "chr1\tt\tgene\t5\t20\t.\t+\t.\tID=a;Parent=x,\n",
    "spaces_around_equals": "chr1\tt\tgene\t5\t20\t.\t+\t.\tID = a ; Name = b\n",
    "empty_value": "chr1\tt\tgene\t5\t20\t.\t+\t.\tID=a;Note=\n",
    "strand_question": "chr1\tt\tgene\t5\t20\t.\t?\t.\tID=a\n",
    "strand_bad": "chr1\tt\tgene\t5\t20\t.\tx\t.\tID=a\n",
    "phase_bad": "chr1\tt\tCDS\t5\t20\t.\t+\t7\tID=a\n",
    "latin1": b"chr1\tt\tgene\t5\t20\t.\t+\t.\tID=caf\xe9\n",
    "nul_byte": "chr1\tt\tgene\t5\t20\t.\t+\t.\tID=a\x00b\n",
    "percent_bad": "chr1\tt\tgene\t5\t20\t.\t+\t.\tID=a%ZZ\n",
    "percent_ok": "chr1\tt\tgene\t5\t20\t.\t+\t.\tID=a%3Bb\n",
    "gtf_unquoted": "chr1\tt\texon\t5\t20\t.\t+\t.\tgene_id G1; transcript_id T1;\n",
    "gtf_semicolon_in_quotes": 'chr1\tt\texon\t5\t20\t.\t+\t.\tgene_id "G;1"; transcript_id "T1";\n',
    "fasta_section": (
        "##gff-version 3\n##sequence-region chr1 1 100\n"
        "chr1\tt\tgene\t5\t20\t.\t+\t.\tID=a\n##FASTA\n>chr1\nACGT\n"
    ),
    "start_after_end": "chr1\tt\tgene\t50\t20\t.\t+\t.\tID=a\n",
    "zero_start": "chr1\tt\tgene\t0\t20\t.\t+\t.\tID=a\n",
}


def _run(path: str, engine: str, validation: str):
    try:
        it = parse_gff(path, engine=engine, strict=validation == "ncbi", validation=validation)
        features = [
            (
                f.seqid,
                f.source,
                f.featuretype,
                f.start,
                f.end,
                f.score,
                f.strand,
                f.frame,
                f.attributes_blob,
                [tuple(p) for p in f.attributes_pairs],
                list(f.extra),
            )
            for f in it
        ]
        warnings = sorted((w.get("line_no"), w.get("kind")) for w in (it.warnings or []))
        dialect = {k: v for k, v in it.dialect().items() if k != "order"}
        return features, warnings, dialect, list(it.directives())
    except Exception as exc:  # noqa: BLE001 - the error is the result
        return type(exc).__name__, getattr(exc, "line_no", None), getattr(exc, "kind", None)


def _cases():
    for path in UPSTREAM:
        yield pytest.param(str(path), id=path.name)
    for name in sorted(HOSTILE):
        yield pytest.param(name, id=name)


@pytest.mark.parametrize("validation", ["gffutils", "ncbi"])
@pytest.mark.parametrize("source", list(_cases()))
def test_the_engines_agree(tmp_path, source, validation):
    if source in HOSTILE:
        data = HOSTILE[source]
        path = tmp_path / source
        path.write_bytes(data if isinstance(data, bytes) else data.encode("utf-8"))
        source = str(path)
    assert _run(source, "python", validation) == _run(source, "rust", validation)


@pytest.mark.parametrize("engine", ["python", "rust"])
@pytest.mark.parametrize("value", ["1_000", "١٢"])
def test_a_non_ascii_integer_is_not_a_coordinate(tmp_path, engine, value):
    path = tmp_path / "x.gff3"
    path.write_text(f"chr1\tt\tgene\t{value}\t2000\t.\t+\t.\tID=a\n", encoding="utf-8")
    it = parse_gff(str(path), engine=engine, strict=False, validation="gffutils")
    assert list(it) == []
    assert [w["kind"] for w in it.warnings] == ["InvalidCoordinate"]
