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
"""Exhaustive corruption suite — feeds the engine every flavor of
malformed input we can dream up and verifies graceful failure.

Coverage targets:

* Circular GFF3 parent-child dependencies (gene → transcript → gene).
* Orphan features (Parent= referencing a nonexistent ID).
* Out-of-bounds coordinates (negative, start > end, huge values).
* Mixed line endings (CRLF and LF in the same file).
* UTF-8 BOM, trailing whitespace in IDs / attribute values.
* Strict mode: ``GFFFormatError`` with the exact ``line_no`` + ``kind``.
* Non-strict mode: malformed lines drop, valid records still flow,
  ``iterator.warnings`` carries the structured error info.

The Rust + Python parser engines are exercised in parallel via the
``engine`` fixture inherited from ``conftest.py``.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from gffbase import GFFFormatError, create_db, parse_bytes


# ---------------------------------------------------------------------------
# 1. Circular dependencies
# ---------------------------------------------------------------------------


def test_gff3_circular_parent_chain_terminates(tmp_path):
    """A ↔ B mutual-Parent loop must not crash the closure CTE.

    The recursive CTE is bounded by ``max_depth`` (default 8), so the
    closure population terminates instead of blowing the call stack.
    Callers reading via ``children(level=None)`` get a finite (albeit
    duplicated) descendant set rather than a hang.
    """
    src = tmp_path / "circular.gff3"
    src.write_text(
        "##gff-version 3\n"
        # Two mutually-parented features.
        "chr1\trs\tgene\t1\t1000\t.\t+\t.\tID=A;Parent=B\n"
        "chr1\trs\tgene\t1\t1000\t.\t+\t.\tID=B;Parent=A\n"
        # A bystander so the file isn't pathologically empty.
        "chr1\trs\texon\t10\t20\t.\t+\t.\tID=e1;Parent=A\n"
    )
    db = create_db(str(src), str(tmp_path / "out.duckdb"), force=True)
    # Edges materialized cleanly — both directions present.
    edges = sorted(
        (r[0], r[1]) for r in db.execute(
            "SELECT parent, child FROM edges"
        ).fetchall()
    )
    assert ("A", "B") in edges
    assert ("B", "A") in edges
    assert ("A", "e1") in edges
    # Closure terminated (didn't hang) and contains both A and B as
    # ancestors of e1.
    rows = db.execute(
        "SELECT DISTINCT ancestor FROM closure WHERE descendant = 'e1' "
        "ORDER BY ancestor"
    ).fetchall()
    ancestors = {r[0] for r in rows}
    assert "A" in ancestors and "B" in ancestors
    # No row exceeds the configured depth.
    max_depth = db.execute("SELECT MAX(depth) FROM closure").fetchone()[0]
    assert 1 <= max_depth <= 8


def test_gff3_self_parent_doesnt_explode(tmp_path):
    """Feature listing itself as its own Parent. The closure CTE's
    depth bound prevents an infinite walk."""
    src = tmp_path / "self_parent.gff3"
    src.write_text(
        "##gff-version 3\n"
        "chr1\trs\tgene\t1\t100\t.\t+\t.\tID=loop;Parent=loop\n"
    )
    db = create_db(str(src), str(tmp_path / "self.duckdb"), force=True)
    n_closure = db.execute("SELECT COUNT(*) FROM closure").fetchone()[0]
    # Bounded — finite number of (loop, loop, k) rows up to max_depth.
    assert n_closure <= 8


# ---------------------------------------------------------------------------
# 2. Orphan features (Parent= references a missing ID)
# ---------------------------------------------------------------------------


def test_orphan_parent_reference_does_not_crash(tmp_path):
    """A ``Parent=ghost`` pointer where ``ghost`` is never defined is
    accepted; the resulting edge points to a nonexistent feature, and
    queries that walk through that edge simply yield nothing extra."""
    src = tmp_path / "orphan.gff3"
    src.write_text(
        "##gff-version 3\n"
        "chr1\trs\tgene\t1\t1000\t.\t+\t.\tID=g1\n"
        "chr1\trs\tmRNA\t1\t1000\t.\t+\t.\tID=t1;Parent=g1\n"
        # Orphan: parent ID 'phantom_gene' is not in the file.
        "chr1\trs\texon\t1\t100\t.\t+\t.\tID=e1;Parent=phantom_gene\n"
    )
    db = create_db(str(src), str(tmp_path / "orphan.duckdb"), force=True)
    # Edge to phantom is recorded.
    edges = {(r[0], r[1]) for r in db.execute(
        "SELECT parent, child FROM edges"
    ).fetchall()}
    assert ("phantom_gene", "e1") in edges
    # children() of the phantom walks the edge we recorded — so it
    # returns the orphan exon. That's correct: the user can use the
    # `duplicates` / orphan information to detect the dangling ref
    # if they care.
    phantom_children = [f.id for f in db.children("phantom_gene")]
    assert "e1" in phantom_children
    # But the phantom itself is NOT a queryable feature (no row).
    from gffbase.exceptions import FeatureNotFoundError
    with pytest.raises(FeatureNotFoundError):
        db["phantom_gene"]
    # The orphan exon itself is still present and queryable directly.
    assert db["e1"].id == "e1"


def test_orphan_doesnt_break_real_hierarchy(tmp_path):
    """Even with one orphan in the file, the well-formed gene → mRNA →
    exon hierarchy must walk correctly."""
    src = tmp_path / "mixed_orphan.gff3"
    src.write_text(
        "##gff-version 3\n"
        "chr1\trs\tgene\t1\t1000\t.\t+\t.\tID=g1\n"
        "chr1\trs\tmRNA\t1\t1000\t.\t+\t.\tID=t1;Parent=g1\n"
        "chr1\trs\texon\t1\t100\t.\t+\t.\tID=e1;Parent=t1\n"
        "chr1\trs\texon\t200\t300\t.\t+\t.\tID=e2;Parent=t1\n"
        # The orphan: mentions a Parent that doesn't exist.
        "chr1\trs\texon\t400\t500\t.\t+\t.\tID=orph;Parent=ghost\n"
    )
    db = create_db(str(src), str(tmp_path / "mh.duckdb"), force=True)
    # The g1 hierarchy is intact.
    descendants = {f.id for f in db.children("g1", level=None)}
    assert {"t1", "e1", "e2"} <= descendants
    assert "orph" not in descendants


# ---------------------------------------------------------------------------
# 3. Out-of-bounds coordinates
# ---------------------------------------------------------------------------


def test_negative_start_strict_with_line_no(engine):
    bad = (
        b"chr1\trs\texon\t100\t200\t.\t+\t.\tID=ok\n"
        b"chr1\trs\texon\t-5\t10\t.\t+\t.\tID=neg\n"
    )
    with pytest.raises(GFFFormatError) as excinfo:
        list(parse_bytes(bad, engine=engine))
    assert excinfo.value.line_no == 2
    assert excinfo.value.kind == "InvalidCoordinate"


def test_zero_start_strict(engine):
    bad = b"chr1\trs\texon\t0\t10\t.\t+\t.\tID=zero\n"
    with pytest.raises(GFFFormatError) as excinfo:
        list(parse_bytes(bad, engine=engine))
    assert excinfo.value.kind == "InvalidCoordinate"
    assert excinfo.value.line_no == 1


def test_end_less_than_start_strict(engine):
    bad = b"chr1\trs\texon\t1000\t100\t.\t+\t.\tID=rev\n"
    with pytest.raises(GFFFormatError) as excinfo:
        list(parse_bytes(bad, engine=engine))
    assert excinfo.value.kind == "InvalidCoordinate"


def test_huge_coordinates_accepted(engine):
    """Coordinates larger than any real chromosome (10^15 bp) are
    structurally valid GFF3 — the spec doesn't impose an upper bound,
    so neither do we. The validator only enforces ``start ≥ 1`` and
    ``end ≥ start``."""
    huge = b"chr1\trs\texon\t1\t999999999999999\t.\t+\t.\tID=huge\n"
    records = list(parse_bytes(huge, engine=engine))
    assert len(records) == 1
    assert records[0].end == 999_999_999_999_999


def test_oob_coords_non_strict_drops_line(engine):
    bad = (
        b"chr1\trs\texon\t100\t200\t.\t+\t.\tID=ok\n"
        b"chr1\trs\texon\t-5\t10\t.\t+\t.\tID=bad\n"
        b"chr1\trs\texon\t300\t400\t.\t+\t.\tID=ok2\n"
    )
    it = parse_bytes(bad, strict=False, engine=engine)
    records = list(it)
    ids = [r.attributes_pairs[0][1] for r in records]
    assert ids == ["ok", "ok2"]
    assert any(
        w["line_no"] == 2 and w["kind"] == "InvalidCoordinate"
        for w in it.warnings
    )


# ---------------------------------------------------------------------------
# 4. Mixed line endings (CRLF + LF in the same file)
# ---------------------------------------------------------------------------


def test_mixed_crlf_lf_line_endings(engine):
    """A real-world hazard: files saved on Windows then concatenated
    with Unix-emitted lines. Both record terminators must work in the
    same input."""
    mixed = (
        b"##gff-version 3\r\n"
        b"chr1\trs\texon\t1\t100\t.\t+\t.\tID=a\r\n"
        b"chr1\trs\texon\t200\t300\t.\t+\t.\tID=b\n"     # LF
        b"chr1\trs\texon\t400\t500\t.\t+\t.\tID=c\r\n"   # back to CRLF
    )
    records = list(parse_bytes(mixed, engine=engine))
    ids = [
        next(v for k, v, _ in r.attributes_pairs if k == "ID")
        for r in records
    ]
    assert ids == ["a", "b", "c"]


def test_pure_crlf_file(engine):
    """All-CRLF file. The trailing ``\\r`` on the attribute string
    must not be carried into the parsed `ID` value."""
    crlf = (
        b"chr1\trs\texon\t1\t100\t.\t+\t.\tID=just_a\r\n"
        b"chr1\trs\texon\t200\t300\t.\t+\t.\tID=just_b\r\n"
    )
    records = list(parse_bytes(crlf, engine=engine))
    ids = [
        next(v for k, v, _ in r.attributes_pairs if k == "ID")
        for r in records
    ]
    # If the parser leaked the \r into the attribute, this would be
    # 'just_a\r' — assert the bare value.
    assert ids == ["just_a", "just_b"]


# ---------------------------------------------------------------------------
# 5. Encoding hazards
# ---------------------------------------------------------------------------


def test_utf8_bom_first_line(engine):
    """A UTF-8 BOM (``\\xef\\xbb\\xbf``) at the start of a file. Some
    editors prepend it; the parser must either skip it or treat the
    leading-BOM line as a comment-equivalent. Either way, a single
    valid record on the next line should still come through."""
    bom = b"\xef\xbb\xbf##gff-version 3\nchr1\trs\texon\t1\t10\t.\t+\t.\tID=x\n"
    # We accept either: the BOM is silently skipped, OR the very first
    # line is treated as a parser warning + the second line still
    # parses.  What we *don't* accept is a hard crash.
    records = list(parse_bytes(bom, engine=engine, strict=False))
    assert any(
        any(k == "ID" and v == "x" for k, v, _ in r.attributes_pairs)
        for r in records
    )


def test_latin1_garbage_in_attribute_value_doesnt_crash(engine):
    """Some real-world annotations smuggle Latin-1 bytes into ``Note=``
    or ``description=`` fields. The parser must not crash; it should
    yield the record (lossy or strict-decoded depending on engine) or
    surface a warning. The exact handling is engine-dependent — we
    only assert *no crash*."""
    latin1 = (
        b"chr1\trs\texon\t1\t10\t.\t+\t.\tID=x;Note=caf\xe9_signal\n"
    )
    try:
        records = list(parse_bytes(latin1, engine=engine, strict=False))
    except Exception as e:  # pragma: no cover - defense-in-depth
        pytest.fail(f"Latin-1 byte should not crash the parser: {e!r}")
    assert len(records) >= 0  # parser made it through


# ---------------------------------------------------------------------------
# 6. Whitespace / trailing-junk in IDs
# ---------------------------------------------------------------------------


def test_trailing_whitespace_in_attribute_value(engine):
    """``ID=foo `` with a trailing space. Real-world files do this; we
    accept the value verbatim (whitespace included). We just need
    consistent behavior (no crash, no silent truncation we can't detect)."""
    src = b"chr1\trs\texon\t1\t10\t.\t+\t.\tID=foo \n"
    records = list(parse_bytes(src, engine=engine))
    assert len(records) == 1
    val = next(v for k, v, _ in records[0].attributes_pairs if k == "ID")
    # Whether the parser preserves or trims the trailing space is
    # acceptable — it just must yield a stable, non-empty string.
    assert val.strip() == "foo"


def test_id_with_internal_whitespace(engine):
    """``ID=foo bar`` with a literal space inside the value. The GFF3
    spec technically requires percent-encoding, but real files break
    it. We accept it permissively and round-trip the bytes verbatim."""
    src = b"chr1\trs\texon\t1\t10\t.\t+\t.\tID=foo bar\n"
    records = list(parse_bytes(src, engine=engine))
    assert len(records) == 1


# ---------------------------------------------------------------------------
# 7. Combined corruption — line numbers stay accurate across a multi-
#    issue file in non-strict mode.
# ---------------------------------------------------------------------------


def test_multi_corruption_line_numbers_preserved(engine):
    """Three different errors on three different lines; the warnings
    list must report each one with the correct ``line_no`` so users
    can grep their original file."""
    src = (
        b"chr1\trs\texon\t1\t10\t.\t+\t.\tID=ok1\n"        # line 1, valid
        b"chr1\trs\texon\t-5\t10\t.\t+\t.\tID=neg\n"       # line 2, bad coord
        b"chr1\trs\texon\t1\t10\t.\t@\t.\tID=str\n"        # line 3, bad strand
        b"chr1\trs\texon\t100\t200\t.\t+\t.\tID=ok2\n"     # line 4, valid
        b"chr1\trs\t\t1\t10\t.\t+\t.\tID=noft\n"           # line 5, empty ft
    )
    it = parse_bytes(src, strict=False, engine=engine)
    records = list(it)
    valid_ids = [
        next(v for k, v, _ in r.attributes_pairs if k == "ID")
        for r in records
    ]
    assert valid_ids == ["ok1", "ok2"]
    warning_lines = sorted(w["line_no"] for w in it.warnings)
    assert warning_lines == [2, 3, 5]
    kinds_by_line = {w["line_no"]: w["kind"] for w in it.warnings}
    assert kinds_by_line[2] == "InvalidCoordinate"
    assert kinds_by_line[3] == "InvalidStrand"
    assert kinds_by_line[5] == "EmptyFeaturetype"


# ---------------------------------------------------------------------------
# 8. Truncated / partial-last-line files
# ---------------------------------------------------------------------------


def test_truncated_partial_last_line_strict(engine):
    """File cut off mid-record (no trailing newline, fewer than 9 cols
    on the last line). Strict mode raises with the exact line number."""
    bad = (
        b"chr1\trs\texon\t1\t10\t.\t+\t.\tID=ok\n"
        b"chr1\trs\texon\t100\t200"   # truncated; only 5 columns
    )
    with pytest.raises(GFFFormatError) as excinfo:
        list(parse_bytes(bad, engine=engine))
    assert excinfo.value.line_no == 2
    assert excinfo.value.kind == "TooFewFields"


def test_truncated_partial_last_line_non_strict(engine):
    bad = (
        b"chr1\trs\texon\t1\t10\t.\t+\t.\tID=ok\n"
        b"chr1\trs\texon\t100\t200"
    )
    it = parse_bytes(bad, strict=False, engine=engine)
    records = list(it)
    assert len(records) == 1
    assert any(w["line_no"] == 2 for w in it.warnings)


# ---------------------------------------------------------------------------
# 9. GFFFormatError class invariants
# ---------------------------------------------------------------------------


def test_gff_format_error_is_value_error_subclass():
    """Backwards-compat: any catch-all that previously trapped
    ``ValueError`` from legacy ``gffutils`` continues to trap our
    structured error."""
    assert issubclass(GFFFormatError, ValueError)


def test_gff_format_error_carries_repr_friendly_attrs(engine):
    bad = b"chr1\trs\texon\t-5\t10\t.\t+\t.\tID=neg\n"
    with pytest.raises(GFFFormatError) as excinfo:
        list(parse_bytes(bad, engine=engine))
    err = excinfo.value
    # Required structured fields.
    assert isinstance(err.line_no, int) and err.line_no >= 1
    assert isinstance(err.kind, str) and err.kind
    assert isinstance(err.message, str) and err.message
    # The string form mentions the line number for grep-ability.
    assert str(err.line_no) in str(err)


# ---------------------------------------------------------------------------
# 10. Full create_db on a corrupted GFF3 — strict default raises,
#     opt-out path tolerates.
# ---------------------------------------------------------------------------


def test_create_db_strict_default_raises_on_bad_line(tmp_path):
    """``create_db`` defaults to strict parsing. A single bad row
    sinks the whole ingest with a ``GFFFormatError`` carrying the
    exact line number."""
    src = tmp_path / "bad.gff3"
    src.write_text(
        "chr1\trs\texon\t1\t10\t.\t+\t.\tID=ok\n"
        "chr1\trs\texon\t-5\t10\t.\t+\t.\tID=neg\n"
    )
    with pytest.raises(GFFFormatError) as excinfo:
        create_db(str(src), str(tmp_path / "out.duckdb"), force=True)
    assert excinfo.value.line_no == 2
