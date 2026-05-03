# ---------------------------------------------------------------------------
# Author: Kuan-Hao Chao <kuanhao.chao@gmail.com>
# ---------------------------------------------------------------------------
"""The Rust parser boundary — malformed and edge-case inputs."""

from __future__ import annotations

import pytest

from gffbase import parse_bytes, parse_gff


def test_too_few_fields_raises_value_error():
    bad = b"chr1\tsrc\texon\t1\t10\t.\t+\n"   # 7 fields, missing col 9 + frame
    with pytest.raises(ValueError):
        list(parse_bytes(bad))


def test_blank_lines_ignored():
    text = b"\n\nchr1\tsrc\texon\t1\t10\t.\t+\t.\tID=x\n\n"
    feats = list(parse_bytes(text))
    assert len(feats) == 1


def test_comment_only_input_yields_nothing():
    text = b"# hello\n## ##gff-version 3\n# more\n"
    feats = list(parse_bytes(text))
    assert feats == []


def test_directive_collected():
    text = b"##gff-version 3\nchr1\tsrc\texon\t1\t10\t.\t+\t.\tID=x\n"
    it = parse_bytes(text)
    list(it)
    assert any(d.startswith("##gff-version") for d in it.directives())


def test_fasta_terminator_halts_iteration():
    text = (
        b"chr1\tsrc\texon\t1\t10\t.\t+\t.\tID=x\n"
        b"##FASTA\n"
        b">chr1\nACGT\n"
        b"chr2\tsrc\texon\t1\t10\t.\t+\t.\tID=y\n"
    )
    feats = list(parse_bytes(text))
    assert len(feats) == 1


def test_dot_coordinates_become_none():
    text = b"chr1\tsrc\texon\t.\t.\t.\t+\t.\tID=x\n"
    feats = list(parse_bytes(text))
    assert feats[0].start is None
    assert feats[0].end is None


def test_carriage_return_line_endings_handled():
    # Rust parser strips \r before tab-splitting; pure-Python parser
    # similarly normalizes line endings via _iter_lines.
    text = b"chr1\tsrc\texon\t1\t10\t.\t+\t.\tID=x\r\n"
    feats = list(parse_bytes(text, engine="python"))
    assert len(feats) == 1
    assert feats[0].seqid == "chr1"
    # The trailing \r must NOT leak into the col-9 attribute value.
    attrs = feats[0].attributes_dict()
    assert attrs["ID"] == ["x"]


def test_gtf_quoted_value_with_semicolon():
    text = b'chr1\tsrc\texon\t1\t10\t.\t+\t.\tgene_id "ENSG"; note "with;semi";\n'
    feats = list(parse_bytes(text))
    attrs = feats[0].attributes_dict()
    assert attrs["note"] == ["with;semi"]


def test_percent_escape_decoded():
    text = b"chr1\tsrc\texon\t1\t10\t.\t+\t.\tNote=hello%20world%2Cyou\n"
    feats = list(parse_bytes(text))
    assert feats[0].attributes_dict()["Note"] == ["hello world,you"]


def test_invalid_percent_escape_passes_through():
    text = b"chr1\tsrc\texon\t1\t10\t.\t+\t.\tNote=100%%\n"
    feats = list(parse_bytes(text))
    # Malformed %X — value preserved as literal.
    assert "100%" in feats[0].attributes_dict()["Note"][0]


def test_gff3_multivalue_split_on_comma():
    text = b"chr1\tsrc\texon\t1\t10\t.\t+\t.\tParent=a,b,c\n"
    feats = list(parse_bytes(text))
    assert feats[0].attributes_dict()["Parent"] == ["a", "b", "c"]


def test_extra_columns_past_nine_preserved():
    text = b"chr1\tsrc\texon\t1\t10\t.\t+\t.\tID=x\textra1\textra2\n"
    feats = list(parse_bytes(text))
    assert feats[0].extra == ["extra1", "extra2"]


def test_force_dialect_check_full_pass():
    """With force_dialect_check=True every feature is sampled."""
    text = b"\n".join(
        f"chr1\tsrc\texon\t{i}\t{i+10}\t.\t+\t.\tID=x{i}".encode()
        for i in range(1, 25)
    ) + b"\n"
    it = parse_bytes(text, force_dialect_check=True)
    list(it)
    assert it.dialect()["fmt"] == "gff3"


def test_force_gff_overrides_detection():
    text = b'chr1\tsrc\texon\t1\t10\t.\t+\t.\tgene_id "ENSG"; transcript_id "ENST";\n'
    it = parse_bytes(text, force_gff=True)
    list(it)
    # The dialect is forced to GFF3 even though the input looks like GTF.
    assert it.dialect()["fmt"] == "gff3"


def test_engine_python_explicit():
    text = b"chr1\tsrc\texon\t1\t10\t.\t+\t.\tID=x\n"
    feats = list(parse_bytes(text, engine="python"))
    assert len(feats) == 1


def test_engine_unknown_raises():
    with pytest.raises(ValueError):
        parse_bytes(b"", engine="bogus")


def test_engine_rust_when_unavailable_raises(monkeypatch):
    from gffbase import parser as _p
    monkeypatch.setattr(_p, "_NATIVE", False)
    with pytest.raises(RuntimeError):
        parse_bytes(b"", engine="rust")


def test_parse_gff_explicit_python(tmp_path):
    p = tmp_path / "x.gff3"
    p.write_text("chr1\tsrc\texon\t1\t10\t.\t+\t.\tID=x\n")
    feats = list(parse_gff(str(p), engine="python"))
    assert len(feats) == 1


def test_detect_dialect_smoke(tmp_path):
    from gffbase import detect_dialect
    p = tmp_path / "x.gff3"
    p.write_text("chr1\tsrc\texon\t1\t10\t.\t+\t.\tID=x\n")
    d = detect_dialect(str(p))
    assert d["fmt"] == "gff3"


def test_detect_dialect_python_engine(tmp_path):
    from gffbase import detect_dialect
    p = tmp_path / "x.gff3"
    p.write_text("chr1\tsrc\texon\t1\t10\t.\t+\t.\tID=x\n")
    d = detect_dialect(str(p), engine="python")
    assert d["fmt"] == "gff3"


def test_native_available_returns_bool():
    from gffbase import native_available
    assert isinstance(native_available(), bool)
