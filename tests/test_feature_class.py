# ---------------------------------------------------------------------------
# Author: Kuan-Hao Chao <kuanhao.chao@gmail.com>
# ---------------------------------------------------------------------------
"""Exhaustive tests for the Feature class — every dunder, every alias,
every formatting branch."""

from __future__ import annotations

import json

import pytest

from gffbase import Feature
from gffbase.feature import _LazyAttributes, _coord_to_int, _revcomp, feature_from_row


# ---------------------------------------------------------------------------
# _coord_to_int / _revcomp
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("raw,expected", [
    (None, None),
    ("", None),
    (".", None),
    ("100", 100),
    (100, 100),
])
def test_coord_to_int(raw, expected):
    assert _coord_to_int(raw) == expected


def test_revcomp_basic():
    assert _revcomp("ATGC") == "GCAT"
    assert _revcomp("aagctt") == "aagctt"  # palindrome
    assert _revcomp("AAANNN") == "NNNTTT"


# ---------------------------------------------------------------------------
# _LazyAttributes
# ---------------------------------------------------------------------------


def test_lazy_attributes_from_blob_lazy():
    """The blob is parsed only on first access, not at construction."""
    attrs = _LazyAttributes(blob=b"ID=g1;Name=alpha", dialect_fmt="gff3")
    assert attrs._parsed is False
    assert attrs["ID"] == ["g1"]
    assert attrs._parsed is True
    # Subsequent access doesn't reparse.
    assert attrs["Name"] == ["alpha"]


def test_lazy_attributes_from_dict():
    attrs = _LazyAttributes(initial={"foo": "bar", "baz": ["a", "b"]})
    assert attrs["foo"] == ["bar"]
    assert attrs["baz"] == ["a", "b"]


def test_lazy_attributes_from_pairs_list():
    attrs = _LazyAttributes(initial=[("k", "v1", 0), ("k", "v2", 1), ("other", "x", 0)])
    assert attrs["k"] == ["v1", "v2"]
    assert attrs["other"] == ["x"]


def test_lazy_attributes_empty_blob():
    attrs = _LazyAttributes(blob=b"", dialect_fmt="gff3")
    # No-op materialize — no items.
    assert len(attrs) == 0
    assert list(attrs) == []


def test_lazy_attributes_setitem_normalizes_lists():
    attrs = _LazyAttributes(initial={})
    attrs["k"] = "single"
    assert attrs["k"] == ["single"]
    attrs["k"] = ["a", "b"]
    assert attrs["k"] == ["a", "b"]


def test_lazy_attributes_delitem_contains_iter():
    attrs = _LazyAttributes(initial={"a": "1", "b": "2"})
    assert "a" in attrs
    del attrs["a"]
    assert "a" not in attrs
    assert sorted(iter(attrs)) == ["b"]


def test_lazy_attributes_repr():
    attrs = _LazyAttributes(initial={"k": "v"})
    assert "Attributes(" in repr(attrs)


def test_lazy_attributes_keys_values_items():
    attrs = _LazyAttributes(initial={"a": "1", "b": "2"})
    assert sorted(attrs.keys()) == ["a", "b"]
    assert [v for v in sorted(attrs.values(), key=str)] == [["1"], ["2"]]
    assert sorted(attrs.items()) == [("a", ["1"]), ("b", ["2"])]


def test_lazy_attributes_init_from_other_lazy():
    src = _LazyAttributes(initial={"k": "v"})
    dst = _LazyAttributes(initial=src)
    assert dst["k"] == ["v"]


def test_lazy_attributes_unsupported_init_type():
    with pytest.raises(TypeError):
        _LazyAttributes(initial=42)


# ---------------------------------------------------------------------------
# Feature constructor / aliases / dunders
# ---------------------------------------------------------------------------


def _basic_feat(**overrides):
    defaults = dict(
        seqid="chr1", source="src", featuretype="exon",
        start=100, end=200, score=".", strand="+", frame=".",
        id="x1", dialect={"fmt": "gff3"},
    )
    defaults.update(overrides)
    return Feature(**defaults)


def test_constructor_normalizes_dot_coords():
    f = Feature(start=".", end=".")
    assert f.start is None
    assert f.end is None
    # __len__ on missing coords
    assert len(f) == 0


def test_constructor_normalizes_none_score_strand_frame():
    f = Feature(score=None, strand=None, frame=None)
    assert f.score == "."
    assert f.strand == "."
    assert f.frame == "."


def test_chrom_alias_setter_getter():
    f = _basic_feat()
    assert f.chrom == "chr1"
    f.chrom = "chrZ"
    assert f.seqid == "chrZ"


def test_stop_alias_setter_getter():
    f = _basic_feat()
    assert f.stop == 200
    f.stop = "."
    assert f.end is None


def test_len_inclusive():
    f = _basic_feat(start=100, end=200)
    assert len(f) == 101


def test_repr_format():
    f = _basic_feat()
    r = repr(f)
    assert "<Feature exon" in r
    assert "chr1:100-200" in r
    assert "[+]" in r


def test_str_byte_faithful_blob_round_trip():
    f = Feature(
        seqid="chr1", source="src", featuretype="exon",
        start=100, end=200, score=".", strand="+", frame=".",
        attributes=b"ID=g1;Name=alpha;Note=hello%20world",
        dialect={"fmt": "gff3"},
    )
    line = str(f)
    parts = line.split("\t")
    # Byte-faithful col-9 round-trip
    assert parts[8] == "ID=g1;Name=alpha;Note=hello%20world"


def test_str_reformats_after_attribute_mutation():
    f = Feature(
        seqid="chr1", source="src", featuretype="exon",
        start=100, end=200, attributes=b"ID=g1;Name=alpha",
        dialect={"fmt": "gff3"},
    )
    f.attributes["Note"] = "added"
    line = str(f)
    parts = line.split("\t")
    # blob re-render path activated; check key=val present
    assert "Note=added" in parts[8]
    assert "ID=g1" in parts[8]


def test_str_emits_dot_coords_when_none():
    f = Feature(seqid="chr1", source=".", featuretype=".",
                start=None, end=None, dialect={"fmt": "gff3"})
    line = str(f)
    assert line.split("\t")[3] == "."
    assert line.split("\t")[4] == "."


def test_str_gtf_emits_quoted_values():
    f = Feature(
        seqid="chr1", source="src", featuretype="exon",
        start=1, end=10, attributes={"gene_id": "ENSG1"},
        dialect={"fmt": "gtf", "keyval separator": " ",
                 "field separator": "; ", "quoted GFF2 values": True,
                 "trailing semicolon": True},
    )
    line = str(f)
    assert 'gene_id "ENSG1"' in line
    # Trailing semicolon honored.
    assert line.endswith(";")


def test_str_gff3_with_field_sep_and_trailing_semi():
    f = Feature(
        seqid="chr1", source="src", featuretype="exon",
        start=1, end=10, attributes={"k": "v"},
        dialect={"fmt": "gff3", "field separator": "; ",
                 "keyval separator": "=", "trailing semicolon": True,
                 "leading semicolon": True},
    )
    line = str(f)
    col9 = line.split("\t")[8]
    assert col9.startswith(";")
    assert col9.endswith(";")


def test_str_sort_attribute_values():
    f = Feature(
        seqid="chr1", source=".", featuretype=".",
        start=1, end=10, attributes={"Parent": ["c", "a", "b"]},
        dialect={"fmt": "gff3"}, sort_attribute_values=True,
    )
    col9 = str(f).split("\t")[8]
    assert "Parent=a,b,c" in col9


def test_extra_string_input_split_on_tabs():
    f = Feature(start=1, end=2, extra="x\ty\tz", dialect={"fmt": "gff3"})
    assert f.extra == ["x", "y", "z"]


def test_extra_bytes_input_decoded():
    f = Feature(start=1, end=2, extra=b"a\tb", dialect={"fmt": "gff3"})
    assert f.extra == ["a", "b"]


def test_extra_iterable_input():
    f = Feature(start=1, end=2, extra=("a", "b"), dialect={"fmt": "gff3"})
    assert f.extra == ["a", "b"]


def test_extra_none_yields_empty():
    f = Feature(start=1, end=2, extra=None, dialect={"fmt": "gff3"})
    assert f.extra == []


def test_extra_empty_string_yields_empty_list():
    f = Feature(start=1, end=2, extra="", dialect={"fmt": "gff3"})
    assert f.extra == []


def test_str_includes_extra_columns():
    f = Feature(seqid="chr1", source="src", featuretype="exon",
                start=1, end=10, attributes={"ID": "x"},
                extra=["extra1", "extra2"], dialect={"fmt": "gff3"})
    parts = str(f).split("\t")
    assert parts[-2] == "extra1"
    assert parts[-1] == "extra2"


def test_int_index_field_access():
    f = _basic_feat()
    assert f[0] == "chr1"
    assert f[1] == "src"
    assert f[2] == "exon"
    assert f[3] == 100
    assert f[4] == 200
    assert f[5] == "."
    assert f[6] == "+"
    assert f[7] == "."


def test_int_index_setter():
    f = _basic_feat()
    f[0] = "chrZ"
    assert f.seqid == "chrZ"


def test_str_index_attribute_access_set():
    f = Feature(start=1, end=2, attributes={"foo": "bar"}, dialect={"fmt": "gff3"})
    assert f["foo"] == ["bar"]
    f["new"] = "val"
    assert f["new"] == ["val"]


def test_eq_ne_same_str_representation():
    f1 = _basic_feat()
    f2 = _basic_feat()
    assert f1 == f2
    assert not (f1 != f2)
    assert hash(f1) == hash(f2)


def test_eq_ne_returns_notimplemented_for_other_types():
    f = _basic_feat()
    assert (f == "string") is False
    assert (f != "string") is True


def test_astuple_legacy_shape():
    f = Feature(
        seqid="chr1", source="src", featuretype="exon",
        start=100, end=200, score=".", strand="+", frame=".",
        attributes={"ID": "x"}, extra=["alt"], id="x", bin=None,
        dialect={"fmt": "gff3"},
    )
    tup = f.astuple()
    assert len(tup) == 12
    assert tup[0] == "x"
    assert tup[3] == "exon"
    assert tup[4] == 100
    assert tup[5] == 200
    # attributes column is JSON
    parsed = json.loads(tup[9])
    assert parsed == {"ID": ["x"]}
    # extra is JSON
    assert json.loads(tup[10]) == ["alt"]
    # bin is computed lazily
    assert isinstance(tup[11], int)


def test_astuple_empty_extra_emits_default():
    f = Feature(start=1, end=10, attributes={"k": "v"}, dialect={"fmt": "gff3"})
    tup = f.astuple()
    assert tup[10] == "[]"


def test_calc_bin_returns_int_for_real_coords():
    f = _basic_feat(start=100, end=200)
    b = f.calc_bin()
    assert isinstance(b, int)
    assert f.bin == b


def test_calc_bin_returns_none_for_missing_coords():
    f = Feature(start=".", end=".", dialect={"fmt": "gff3"})
    assert f.calc_bin() is None


def test_calc_bin_with_explicit_argument_overrides():
    f = _basic_feat()
    assert f.calc_bin(_bin=42) == 42
    assert f.bin == 42


# ---------------------------------------------------------------------------
# Feature.sequence — uses a dummy mapping object instead of pyfaidx.
# ---------------------------------------------------------------------------


class _DummyContig:
    def __init__(self, seq: str): self._s = seq
    def __getitem__(self, sl): return self._s[sl]
    def __str__(self): return self._s


class _DummyFasta:
    def __init__(self, mapping): self._m = mapping
    def __getitem__(self, k): return _DummyContig(self._m[k])


def test_sequence_plus_strand_no_revcomp():
    fa = _DummyFasta({"chr1": "ACGTACGTACGT"})  # 1-based: 'A'CGTACGTACGT
    f = Feature(seqid="chr1", start=1, end=4, strand="+", dialect={"fmt": "gff3"})
    assert f.sequence(fa) == "ACGT"


def test_sequence_minus_strand_revcomp():
    fa = _DummyFasta({"chr1": "AAAACGT"})
    f = Feature(seqid="chr1", start=5, end=7, strand="-", dialect={"fmt": "gff3"})
    # raw slice = "CGT", revcomp = "ACG"
    assert f.sequence(fa) == "ACG"


def test_sequence_use_strand_false_keeps_raw():
    fa = _DummyFasta({"chr1": "AAAACGT"})
    f = Feature(seqid="chr1", start=5, end=7, strand="-", dialect={"fmt": "gff3"})
    assert f.sequence(fa, use_strand=False) == "CGT"


# ---------------------------------------------------------------------------
# feature_from_row
# ---------------------------------------------------------------------------


def test_feature_from_row_round_trip():
    row = ("g1", "chr1", "src", "gene", 100, 500,
           ".", "+", ".", b"ID=g1;Name=alpha", b"", 7)
    f = feature_from_row(row, dialect={"fmt": "gff3"})
    assert f.id == "g1"
    assert f.seqid == "chr1"
    assert f.start == 100
    assert f.end == 500
    assert f.attributes["ID"] == ["g1"]
    assert f.file_order == 7
    # Empty extra blob → empty list.
    assert f.extra == []


def test_feature_from_row_with_none_score_falls_back_to_dot():
    row = ("g1", "chr1", "src", "gene", 1, 10,
           None, None, None, b"", b"", 0)
    f = feature_from_row(row)
    assert f.score == "."
    assert f.strand == "."
    assert f.frame == "."
