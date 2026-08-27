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
"""Phase 16 — NCBI GFF3 spec compliance torture tests.

Each test feeds a hand-crafted malformed input to ``parse_bytes`` and
asserts the strict-vs-non-strict semantics:

* Strict path: ``GFFFormatError`` raised, with ``.line_no`` + ``.kind``.
* Non-strict path: errors demoted to warnings, valid records still flow.

These tests run against both the Rust extension (when built) and the
pure-Python fallback via the ``engine`` parametrize fixture inherited
from ``conftest.py``.
"""

from __future__ import annotations

import pytest
from gffbase import GFFFormatError, parse_bytes

# A baseline well-formed GFF3 line we can splice into multi-line fixtures.
GOOD = b"chr1\tsrc\texon\t100\t200\t.\t+\t.\tID=g1\n"


def _consume(text: bytes, *, strict: bool, engine: str = "rust"):
    """Drive an iterator to completion. Returns (records, iterator)."""
    it = parse_bytes(text, strict=strict, engine=engine)
    records = list(it)
    return records, it


# ---------------------------------------------------------------------------
# 1–2. Wrong number of columns.
# ---------------------------------------------------------------------------


def test_too_few_fields_raises_gff_format_error_strict(engine):
    bad = b"chr1\tsrc\texon\t1\t10\t.\t+\n"  # 7 columns
    with pytest.raises(GFFFormatError) as excinfo:
        list(parse_bytes(bad, engine=engine))
    err = excinfo.value
    assert err.line_no == 1
    assert err.kind == "TooFewFields"


def test_eight_columns_also_too_few(engine):
    bad = b"chr1\tsrc\texon\t1\t10\t.\t+\t.\n"  # 8 columns, no col 9
    with pytest.raises(GFFFormatError) as excinfo:
        list(parse_bytes(bad, engine=engine))
    assert excinfo.value.kind == "TooFewFields"


def test_too_few_fields_non_strict(engine):
    bad = b"chr1\tsrc\texon\t1\t10\t.\t+\n"
    records, it = _consume(bad, strict=False, engine=engine)
    assert records == []
    assert any(w["kind"] == "TooFewFields" and w["line_no"] == 1 for w in it.warnings)


# ---------------------------------------------------------------------------
# 3–5. Coordinate validation.
# ---------------------------------------------------------------------------


def test_non_integer_start_strict(engine):
    bad = b"chr1\tsrc\texon\tabc\t10\t.\t+\t.\tID=x\n"
    with pytest.raises(GFFFormatError) as excinfo:
        list(parse_bytes(bad, engine=engine))
    assert excinfo.value.kind == "InvalidCoordinate"
    assert excinfo.value.line_no == 1


def test_non_integer_end_strict(engine):
    bad = b"chr1\tsrc\texon\t1\txyz\t.\t+\t.\tID=x\n"
    with pytest.raises(GFFFormatError):
        list(parse_bytes(bad, engine=engine))


def test_negative_start_strict(engine):
    bad = b"chr1\tsrc\texon\t-5\t10\t.\t+\t.\tID=x\n"
    with pytest.raises(GFFFormatError) as excinfo:
        list(parse_bytes(bad, engine=engine))
    assert excinfo.value.kind == "InvalidCoordinate"


def test_zero_start_strict(engine):
    bad = b"chr1\tsrc\texon\t0\t10\t.\t+\t.\tID=x\n"
    with pytest.raises(GFFFormatError) as excinfo:
        list(parse_bytes(bad, engine=engine))
    assert excinfo.value.kind == "InvalidCoordinate"


def test_end_less_than_start_strict(engine):
    bad = b"chr1\tsrc\texon\t100\t10\t.\t+\t.\tID=x\n"
    with pytest.raises(GFFFormatError) as excinfo:
        list(parse_bytes(bad, engine=engine))
    assert excinfo.value.kind == "InvalidCoordinate"


@pytest.mark.parametrize(
    "start,end",
    [(".", "10"), ("1", "."), (".", "-1"), ("1", "0"), ("1", "-1")],
)
def test_coordinates_must_be_present_and_positive_strict(engine, start, end):
    bad = f"chr1\tsrc\texon\t{start}\t{end}\t.\t+\t.\tID=x\n".encode()
    with pytest.raises(GFFFormatError) as excinfo:
        list(parse_bytes(bad, engine=engine))
    assert excinfo.value.kind == "InvalidCoordinate"


def test_missing_coordinates_are_retained_with_a_compat_warning(engine):
    text = b"chr1\tsrc\tregion\t.\t.\t.\t.\t.\tID=r\n"
    it = parse_bytes(text, engine=engine, validation="gffutils")
    (record,) = list(it)
    assert (record.start, record.end) == (None, None)
    assert [warning["kind"] for warning in it.warnings] == [
        "InvalidCoordinate",
        "InvalidCoordinate",
    ]
    assert "start coordinate" in it.warnings[0]["message"]
    assert "end coordinate" in it.warnings[1]["message"]


# ---------------------------------------------------------------------------
# 6–7. Strand validation.
# ---------------------------------------------------------------------------


def test_invalid_strand_strict(engine):
    bad = b"chr1\tsrc\texon\t1\t10\t.\t@\t.\tID=x\n"
    with pytest.raises(GFFFormatError) as excinfo:
        list(parse_bytes(bad, engine=engine))
    assert excinfo.value.kind == "InvalidStrand"


def test_multichar_strand_strict(engine):
    bad = b"chr1\tsrc\texon\t1\t10\t.\t+-\t.\tID=x\n"
    with pytest.raises(GFFFormatError) as excinfo:
        list(parse_bytes(bad, engine=engine))
    assert excinfo.value.kind == "InvalidStrand"


def test_strand_question_mark_accepted(engine):
    ok = b"chr1\tsrc\texon\t1\t10\t.\t?\t.\tID=x\n"
    records, _ = _consume(ok, strict=True, engine=engine)
    assert len(records) == 1


# ---------------------------------------------------------------------------
# 8–9. Phase / frame validation.
# ---------------------------------------------------------------------------


def test_invalid_phase_strict(engine):
    bad = b"chr1\tsrc\texon\t1\t10\t.\t+\t5\tID=x\n"
    with pytest.raises(GFFFormatError) as excinfo:
        list(parse_bytes(bad, engine=engine))
    assert excinfo.value.kind == "InvalidPhase"


def test_cds_phase_dot_strict(engine):
    bad = b"chr1\tsrc\tCDS\t1\t10\t.\t+\t.\tID=c1\n"
    with pytest.raises(GFFFormatError) as excinfo:
        list(parse_bytes(bad, engine=engine))
    assert excinfo.value.kind == "InvalidPhase"


def test_cds_phase_concrete_accepted(engine):
    ok = b"chr1\tsrc\tCDS\t1\t10\t.\t+\t0\tID=c1\n"
    records, _ = _consume(ok, strict=True, engine=engine)
    assert len(records) == 1


def test_phase_dot_on_non_cds_accepted(engine):
    ok = b"chr1\tsrc\texon\t1\t10\t.\t+\t.\tID=e1\n"
    records, _ = _consume(ok, strict=True, engine=engine)
    assert len(records) == 1


# ---------------------------------------------------------------------------
# 10–12. Seqid + featuretype validation.
# ---------------------------------------------------------------------------


def test_empty_seqid_strict(engine):
    bad = b"\tsrc\texon\t1\t10\t.\t+\t.\tID=x\n"
    with pytest.raises(GFFFormatError) as excinfo:
        list(parse_bytes(bad, engine=engine))
    assert excinfo.value.kind == "EmptySeqid"


def test_empty_featuretype_strict(engine):
    bad = b"chr1\tsrc\t\t1\t10\t.\t+\t.\tID=x\n"
    with pytest.raises(GFFFormatError) as excinfo:
        list(parse_bytes(bad, engine=engine))
    assert excinfo.value.kind == "EmptyFeaturetype"


def test_featuretype_with_whitespace_strict(engine):
    bad = b"chr1\tsrc\texon foo\t1\t10\t.\t+\t.\tID=x\n"
    with pytest.raises(GFFFormatError) as excinfo:
        list(parse_bytes(bad, engine=engine))
    assert excinfo.value.kind == "InvalidFeaturetype"


@pytest.mark.parametrize("control", ["\x1c", "\x1d", "\x1e", "\x1f", "\x7f", "\x85"])
def test_featuretype_with_unicode_control_is_rejected_consistently(engine, control):
    bad = f"chr1\tsrc\texon{control}part\t1\t10\t.\t+\t.\tID=x\n".encode()

    with pytest.raises(GFFFormatError) as excinfo:
        list(parse_bytes(bad, engine=engine))

    assert excinfo.value.kind == "InvalidFeaturetype"


def test_control_character_keeps_full_compat_warning_order(engine):
    bad = b"\tsrc\tbad\x1ctype\t.\t0\t 1 \tx\t9\tbroken\textra\n"
    iterator = parse_bytes(bad, engine=engine, validation="gffutils")

    list(iterator)

    assert [warning["kind"] for warning in iterator.warnings] == [
        "TooManyFields",
        "EmptySeqid",
        "InvalidFeaturetype",
        "InvalidCoordinate",
        "InvalidCoordinate",
        "InvalidStrand",
        "InvalidPhase",
        "InvalidScore",
        "InvalidAttribute",
    ]


# ---------------------------------------------------------------------------
# 13. Score validation.
# ---------------------------------------------------------------------------


def test_invalid_score_strict(engine):
    bad = b"chr1\tsrc\texon\t1\t10\tabc\t+\t.\tID=x\n"
    with pytest.raises(GFFFormatError) as excinfo:
        list(parse_bytes(bad, engine=engine))
    assert excinfo.value.kind == "InvalidScore"


def test_float_score_accepted(engine):
    ok = b"chr1\tsrc\texon\t1\t10\t0.95\t+\t.\tID=x\n"
    records, _ = _consume(ok, strict=True, engine=engine)
    assert len(records) == 1


@pytest.mark.parametrize("score", ["nan", "NaN", "inf", "+inf", "-inf", "Infinity"])
def test_non_finite_scores_are_rejected_strict(engine, score):
    bad = f"chr1\tsrc\texon\t1\t10\t{score}\t+\t.\tID=x\n".encode()
    with pytest.raises(GFFFormatError) as excinfo:
        list(parse_bytes(bad, engine=engine))
    assert excinfo.value.kind == "InvalidScore"


@pytest.mark.parametrize("score", [" 1", "1 "])
def test_score_surrounding_whitespace_is_rejected_strict(engine, score):
    bad = f"chr1\tsrc\texon\t1\t10\t{score}\t+\t.\tID=x\n".encode()
    with pytest.raises(GFFFormatError) as excinfo:
        list(parse_bytes(bad, engine=engine))
    assert excinfo.value.kind == "InvalidScore"


def test_compat_emits_every_applicable_diagnostic_in_strict_order(engine):
    bad = b"\tsrc\tbad type\t.\t0\t 1 \tx\t9\tbroken\textra\n"
    iterator = parse_bytes(bad, engine=engine, validation="gffutils")

    (record,) = list(iterator)

    assert record.score == " 1 "
    assert [warning["kind"] for warning in iterator.warnings] == [
        "TooManyFields",
        "EmptySeqid",
        "InvalidFeaturetype",
        "InvalidCoordinate",
        "InvalidCoordinate",
        "InvalidStrand",
        "InvalidPhase",
        "InvalidScore",
        "InvalidAttribute",
    ]


# ---------------------------------------------------------------------------
# 14. Mixed: line_no points at the failing line in a multi-line input.
# ---------------------------------------------------------------------------


def test_mixed_input_line_number_correct(engine):
    text = (
        b"chr1\tsrc\texon\t100\t200\t.\t+\t.\tID=a\n"  # line 1, OK
        b"chr1\tsrc\texon\tabc\t200\t.\t+\t.\tID=b\n"  # line 2, BAD
        b"chr1\tsrc\texon\t300\t400\t.\t+\t.\tID=c\n"  # line 3, OK
    )
    with pytest.raises(GFFFormatError) as excinfo:
        list(parse_bytes(text, engine=engine))
    assert excinfo.value.line_no == 2


def test_mixed_input_non_strict_yields_valid(engine):
    text = (
        b"chr1\tsrc\texon\t100\t200\t.\t+\t.\tID=a\n"
        b"chr1\tsrc\texon\tabc\t200\t.\t+\t.\tID=b\n"
        b"chr1\tsrc\texon\t300\t400\t.\t+\t.\tID=c\n"
    )
    records, it = _consume(text, strict=False, engine=engine)
    ids = [next((v for k, v, _ in r.attributes_pairs if k == "ID"), None) for r in records]
    assert ids == ["a", "c"]
    assert len(it.warnings) == 1
    assert it.warnings[0]["line_no"] == 2
    assert it.warnings[0]["kind"] == "InvalidCoordinate"


# ---------------------------------------------------------------------------
# 15. Truncated final line.
# ---------------------------------------------------------------------------


def test_truncated_final_line_strict(engine):
    # No trailing newline; only 4 fields.
    bad = b"chr1\tsrc\texon\t1"
    with pytest.raises(GFFFormatError) as excinfo:
        list(parse_bytes(bad, engine=engine))
    assert excinfo.value.kind == "TooFewFields"


# ---------------------------------------------------------------------------
# 16. Directives / comments only.
# ---------------------------------------------------------------------------


def test_directives_and_comments_only_no_error(engine):
    text = b"##gff-version 3\n# comment\n## another directive\n"
    records, it = _consume(text, strict=True, engine=engine)
    assert records == []
    assert any("gff-version" in d for d in it.directives())


# ---------------------------------------------------------------------------
# 17. All-`.` coordinates are a compatibility extension, not strict GFF3.
# ---------------------------------------------------------------------------


def test_all_dots_row_rejected_strict(engine):
    bad = b"chr1\tsrc\tregion\t.\t.\t.\t.\t.\tID=r\n"
    with pytest.raises(GFFFormatError) as excinfo:
        list(parse_bytes(bad, engine=engine))
    assert excinfo.value.kind == "InvalidCoordinate"


# ---------------------------------------------------------------------------
# 18. Dbxref multi-value.
# ---------------------------------------------------------------------------


def test_dbxref_multivalue_parses_to_multiple_pairs(engine):
    ok = b"chr1\tsrc\tgene\t1\t10\t.\t+\t.\tID=g;Dbxref=GeneID:1,HGNC:HGNC:1\n"
    records, _ = _consume(ok, strict=True, engine=engine)
    pairs = records[0].attributes_pairs
    dbxrefs = [v for k, v, _ in pairs if k == "Dbxref"]
    assert dbxrefs == ["GeneID:1", "HGNC:HGNC:1"]


# ---------------------------------------------------------------------------
# 21–22. Attribute-string structure.
# ---------------------------------------------------------------------------


def test_garbage_attribute_string_strict(engine):
    bad = b"chr1\tsrc\texon\t1\t10\t.\t+\t.\tjustsomegarbage\n"
    with pytest.raises(GFFFormatError) as excinfo:
        list(parse_bytes(bad, engine=engine))
    assert excinfo.value.kind == "InvalidAttribute"


@pytest.mark.parametrize(
    "blob",
    [
        "ID=x;broken",
        "ID=x;=empty-key",
        "=empty-key",
        'ID=x;Name="unterminated',
        "ID=bad%",
        "ID=bad%2",
        "ID=bad%XZ",
    ],
)
def test_malformed_attribute_grammar_is_rejected_strict(engine, blob):
    bad = f"chr1\tsrc\texon\t1\t10\t.\t+\t.\t{blob}\n".encode()
    with pytest.raises(GFFFormatError) as excinfo:
        list(parse_bytes(bad, engine=engine))
    assert excinfo.value.kind == "InvalidAttribute"


def test_too_many_fields_rejected_strict_but_preserved_in_compat(engine):
    text = b"chr1\tsrc\texon\t1\t10\t.\t+\t.\tID=x\textra1\textra2\n"
    with pytest.raises(GFFFormatError) as excinfo:
        list(parse_bytes(text, engine=engine))
    assert excinfo.value.kind == "TooManyFields"

    it = parse_bytes(text, engine=engine, validation="gffutils")
    (record,) = list(it)
    assert record.extra == ["extra1", "extra2"]
    assert [warning["kind"] for warning in it.warnings] == ["TooManyFields"]


def test_dot_attribute_string_accepted(engine):
    ok = b"chr1\tsrc\texon\t1\t10\t.\t+\t.\t.\n"
    records, _ = _consume(ok, strict=True, engine=engine)
    assert len(records) == 1


def test_python_parse_bytes_invalid_utf8_is_line_aware():
    text = GOOD + b"chr1\tsrc\texon\t1\t10\t.\t+\t.\tID=bad\xff\n"
    with pytest.raises(GFFFormatError) as excinfo:
        list(parse_bytes(text, engine="python"))
    assert excinfo.value.line_no == 2
    assert excinfo.value.kind == "InvalidAttribute"


def test_validation_aliases_and_unknown_profile(engine):
    assert len(list(parse_bytes(GOOD, engine=engine, validation="strict"))) == 1
    assert len(list(parse_bytes(GOOD, engine=engine, validation="compat"))) == 1
    with pytest.raises(ValueError, match="validation must be"):
        parse_bytes(GOOD, engine=engine, validation="unknown")


# ---------------------------------------------------------------------------
# 24–25. Iterator.warnings shape + GFFFormatError ⊂ ValueError.
# ---------------------------------------------------------------------------


def test_warning_dict_keys(engine):
    text = (
        b"chr1\tsrc\texon\t100\t200\t.\t+\t.\tID=a\n"
        b"\tsrc\texon\t1\t10\t.\t+\t.\tID=b\n"  # empty seqid
    )
    _, it = _consume(text, strict=False, engine=engine)
    assert len(it.warnings) == 1
    w = it.warnings[0]
    assert set(w.keys()) >= {"line_no", "kind", "message"}
    assert w["line_no"] == 2
    assert w["kind"] == "EmptySeqid"


def test_gff_format_error_is_value_error_subclass():
    """Legacy callers do `pytest.raises(ValueError)` — must keep working."""
    assert issubclass(GFFFormatError, ValueError)


def test_legacy_value_error_catch_still_works(engine):
    bad = b"chr1\tsrc\texon\t1\t10\t.\t+\n"
    with pytest.raises(ValueError):  # NOT GFFFormatError specifically
        list(parse_bytes(bad, engine=engine))


# ---------------------------------------------------------------------------
# Additional sanity: the existing well-formed corpus parses without
# warnings in non-strict mode either.
# ---------------------------------------------------------------------------


def test_clean_input_has_zero_warnings(engine):
    text = GOOD * 5
    records, it = _consume(text, strict=False, engine=engine)
    assert len(records) == 5
    assert it.warnings == []


# ---------------------------------------------------------------------------
# Direct-construction coverage of the Python-side GFFFormatError class.
# ---------------------------------------------------------------------------


def test_python_gff_format_error_constructor_carries_fields():
    from gffbase.exceptions import GFFFormatError as PyGFFFormatError

    err = PyGFFFormatError("oops", line_no=42, kind="InvalidStrand")
    assert err.line_no == 42
    assert err.kind == "InvalidStrand"
    assert err.message == "oops"
    assert isinstance(err, ValueError)


def test_iterator_warnings_via_callable_or_attribute(engine):
    """The dispatcher's ``warnings`` adapter accepts both the callable
    form (Rust extension) and the attribute form (pure-Python fallback)."""
    text = b"chr1\tsrc\texon\t100\t200\t.\t+\t.\tID=a\n"
    _, it = _consume(text, strict=False, engine=engine)
    assert isinstance(it.warnings, list)


def test_warnings_empty_when_inner_lacks_attribute():
    """The adapter returns [] gracefully when the underlying iterator
    doesn't expose `warnings`."""
    from gffbase.parser import _Iterator

    class _Stub:
        def __iter__(self):
            return self

        def __next__(self):
            raise StopIteration

        def dialect(self):
            return {"fmt": "gff3"}

        def directives(self):
            return []

        # deliberately no `warnings`

    it = _Iterator(_Stub(), native=False)
    assert it.warnings == []
