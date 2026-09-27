"""Compat mode on noisy bytes: never crashes, never silently empty, engines agree.

The other property tests generate well-formed records. Real files are not:
this builds lines from the pieces that break parsers -- the wrong number of
tabs, bare and doubled separators, stray quotes, percent signs, CR and CRLF,
comments, directives, and bytes that are not UTF-8 -- and checks that
gffbase's compatibility mode handles every one of them the same way in both
engines, and that `create_db` either builds a database holding what the parser
read or raises one of gffbase's own errors.
"""

from __future__ import annotations

import pytest
from gffbase import create_db, native_available
from gffbase.exceptions import (
    DuplicateIDError,
    EmptyInputError,
    GFFFormatError,
    MultipartConstraintError,
)
from gffbase.parser import parse_bytes
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

pytestmark = pytest.mark.property

ENGINES = ["python"] + (["rust"] if native_available() else [])

_ATOMS = st.sampled_from(
    [
        "chr1",
        "gene",
        "exon",
        "mRNA",
        "CDS",
        "1",
        "100",
        "0",
        "-5",
        ".",
        "+",
        "-",
        "?",
        "ID=a",
        "ID=b",
        "Parent=a",
        "Parent=a,",
        "Name=x;y",
        'gene_id "G"',
        'transcript_id "T"',
        "Note=%3B",
        "Note=%ZZ",
        '"',
        ";",
        "=",
        " ",
        "a b",
        "é",
        "",
    ]
)
_FIELD = st.lists(_ATOMS, min_size=1, max_size=3).map("".join)
_LINE = st.one_of(
    st.lists(_FIELD, min_size=0, max_size=11).map("\t".join),
    st.sampled_from(["", "   ", "#comment", "##gff-version 3", "##sequence-region chr1 1 9"]),
)
_BAD_BYTES = st.sampled_from([b"", b"", b"", b"\xff", b"\xe9", b"\x00"])


@st.composite
def noisy_gff(draw) -> bytes:
    lines = draw(st.lists(_LINE, min_size=0, max_size=8))
    ending = draw(st.sampled_from(["\n", "\r\n", "\r"]))
    body = ending.join(lines).encode("utf-8")
    return body + draw(_BAD_BYTES)


def _parse(data: bytes, engine: str):
    try:
        it = parse_bytes(data, engine=engine, strict=False, validation="gffutils")
        features = [
            (
                f.seqid,
                f.featuretype,
                f.start,
                f.end,
                f.attributes_blob,
                [tuple(p) for p in f.attributes_pairs],
            )
            for f in it
        ]
        return "ok", features, sorted((w["line_no"], w["kind"]) for w in it.warnings)
    except Exception as exc:  # noqa: BLE001 - the error is the result
        return "raised", type(exc).__name__


_SETTINGS = settings(suppress_health_check=[HealthCheck.too_slow], deadline=None)


@_SETTINGS
@given(data=noisy_gff())
def test_the_engines_agree_on_noise(data):
    results = [_parse(data, e) for e in ENGINES]
    assert all(r == results[0] for r in results[1:])


#: What `create_db` may raise on arbitrary input: gffbase's own errors, each of
#: which names the problem. Anything else -- KeyError, a DuckDB error, an
#: IndexError -- is a crash.
_DOCUMENTED = (EmptyInputError, DuplicateIDError, GFFFormatError, MultipartConstraintError)


@_SETTINGS
@given(data=noisy_gff())
def test_create_db_never_crashes_and_never_loses_everything(data):
    parsed = _parse(data, ENGINES[-1])
    try:
        db = create_db(
            data.decode("utf-8", errors="replace"),
            ":memory:",
            from_string=True,
            merge_strategy="create_unique",
        )
    except _DOCUMENTED:
        return
    stored = db.count_features_of_type()
    if parsed[0] == "ok" and parsed[1]:
        assert stored >= len(parsed[1])
    assert db.validate(level="fast").ok
