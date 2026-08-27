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
"""`FeatureSegment` and `MultipartFeature` -- the objects, without a database.

Both subclass `Feature`, and the compatibility argument rests entirely on what
they DON'T override: `__str__`, `__len__`, `__hash__`, `__eq__`, `__getitem__`,
`astuple`, `_format_line`, `_format_attributes`. A subclass that redefined any
of them would break legacy consumers in ways no individual assertion here would
notice, so one test checks that directly, by identity of the function objects.

The rest covers the two quantities that are easy to conflate -- `len()` is the
envelope span and `covered_length` excludes the gaps -- and per-segment phase,
which is the reason this storage exists at all.
"""

from __future__ import annotations

import pytest
from gffbase import Feature, FeatureSegment, MultipartFeature


def _seg(start, end, frame=".", seg_idx=0, blob=b"ID=cds1;Parent=t1"):
    return FeatureSegment(
        seqid="chr1",
        source="rs",
        featuretype="CDS",
        start=start,
        end=end,
        score=".",
        strand="+",
        frame=frame,
        attributes=blob,
        id="cds1",
        seg_idx=seg_idx,
    )


@pytest.fixture
def split_cds():
    """An NCBI-style split CDS: two exons 599 bp apart, with the phase
    continuing across the join. Envelope 100..900, covered 202."""
    return MultipartFeature(
        seqid="chr1",
        source="rs",
        featuretype="CDS",
        start=100,
        end=900,
        score=".",
        strand="+",
        frame="0",
        attributes=b"ID=cds1;Parent=t1",
        id="cds1",
        n_segments=2,
        segments=[_seg(100, 200, "0", 0), _seg(800, 900, "2", 1)],
    )


# ---------------------------------------------------------------------------
# What must NOT be overridden
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("subclass", [FeatureSegment, MultipartFeature])
@pytest.mark.parametrize(
    "method",
    [
        "__str__",
        "__len__",
        "__hash__",
        "__eq__",
        "__ne__",
        "__getitem__",
        "__setitem__",
        "astuple",
        "calc_bin",
        "sequence",
        "_format_line",
        "_format_attributes",
    ],
)
def test_compat_surface_is_inherited_not_reimplemented(subclass, method):
    """Preserved by inaction. Overriding any of these would change behaviour
    for every legacy consumer, and no single assertion elsewhere would catch
    it -- so compare the function objects directly."""
    assert getattr(subclass, method) is getattr(Feature, method), (
        f"{subclass.__name__} overrides {method}; the compat surface must be inherited"
    )


def test_both_subclasses_are_features(split_cds):
    """`isinstance(x, Feature)` is load-bearing at nine call sites inside
    gffbase, and `Feature.__eq__` returns NotImplemented for a non-Feature, so
    a sibling class would compare unequal to an identical Feature."""
    assert isinstance(split_cds, Feature)
    assert isinstance(_seg(1, 10), Feature)


def test_neither_subclass_grows_a_dict(split_cds):
    """`Feature` is slotted because it is instantiated per row. A subclass that
    forgot its own `__slots__` would silently re-add `__dict__` to every
    instance."""
    for obj in (split_cds, _seg(1, 10)):
        with pytest.raises(AttributeError):
            _ = obj.__dict__


# ---------------------------------------------------------------------------
# The singleton case
# ---------------------------------------------------------------------------


def test_an_ordinary_feature_is_its_own_sole_segment():
    """So callers can write `for seg in f.segments` without first asking
    whether the feature is discontinuous."""
    f = Feature(
        seqid="chr1",
        source="rs",
        featuretype="exon",
        start=5,
        end=25,
        attributes=b"ID=e1",
        id="e1",
    )
    (only,) = f.segments
    assert isinstance(only, FeatureSegment)
    assert only.seg_idx == 0
    assert str(only) == str(f)
    assert only == f


def test_singleton_class_attributes_cost_nothing():
    f = Feature(seqid="chr1", featuretype="exon", start=1, end=2)
    assert f.is_multipart is False
    assert f.n_segments == 1
    # ClassVars, not per-instance state.
    assert "is_multipart" not in Feature.__slots__
    assert "n_segments" not in Feature.__slots__


def test_a_plain_feature_reports_one_line():
    f = Feature(seqid="chr1", featuretype="exon", start=1, end=2, attributes=b"ID=e1")
    assert f.to_lines() == [str(f)]


# ---------------------------------------------------------------------------
# The multipart case
# ---------------------------------------------------------------------------


def test_multipart_flags(split_cds):
    assert split_cds.is_multipart is True
    assert split_cds.n_segments == 2


def test_len_is_the_envelope_and_covered_length_excludes_the_gaps(split_cds):
    """The two quantities are easy to conflate and differ by the gap. `len`
    keeps the inherited `Feature` meaning; `covered_length` is the new one."""
    assert len(split_cds) == 801
    assert split_cds.covered_length == 202


def test_inherited_behaviour_operates_on_the_envelope(split_cds):
    """A caller that knows nothing about discontinuous features must see
    exactly the gffutils behaviour for a feature spanning the envelope."""
    assert split_cds.start == 100
    assert split_cds.end == 900
    assert str(split_cds).split("\t")[3:5] == ["100", "900"]


def test_to_lines_reproduces_every_input_line_with_its_own_phase(split_cds):
    """Per-segment CDS phase is the main reason `segments` exists: there is
    nowhere in `features` to put more than one phase."""
    lines = split_cds.to_lines()
    assert len(lines) == 2
    assert lines[0] == "chr1\trs\tCDS\t100\t200\t.\t+\t0\tID=cds1;Parent=t1"
    assert lines[1] == "chr1\trs\tCDS\t800\t900\t.\t+\t2\tID=cds1;Parent=t1"


def test_segments_are_in_file_order_not_coordinate_order():
    """GFF3 does not require segments to be sorted, and `to_lines()` has to
    reproduce the input."""
    mp = MultipartFeature(
        seqid="chr1",
        source="rs",
        featuretype="CDS",
        start=100,
        end=900,
        strand="+",
        attributes=b"ID=cds1",
        id="cds1",
        n_segments=2,
        segments=[_seg(800, 900, "2", 0), _seg(100, 200, "0", 1)],
    )
    assert [s.start for s in mp.segments] == [800, 100]
    assert [int(line.split("\t")[3]) for line in mp.to_lines()] == [800, 100]


def test_a_segment_carries_the_logical_id(split_cds):
    """So `db[seg.id]` finds the whole feature, while the segment's own `ID=`
    survives byte-for-byte in the blob."""
    for seg in split_cds.segments:
        assert seg.id == "cds1"
        assert "ID=cds1" in str(seg)


def test_segments_are_loaded_lazily_when_not_prefetched():
    """`_yield_features` prefetches per chunk; the loader is the fallback for a
    feature obtained some other way. Without it, iterating a multipart corpus
    would be an N+1."""
    calls = []

    def loader():
        calls.append(1)
        return [_seg(100, 200, "0", 0), _seg(800, 900, "2", 1)]

    mp = MultipartFeature(
        seqid="chr1",
        featuretype="CDS",
        start=100,
        end=900,
        id="cds1",
        n_segments=2,
        segment_loader=loader,
    )
    assert calls == []
    assert len(mp.segments) == 2
    assert len(mp.segments) == 2
    assert calls == [1], "segments must be loaded once and cached"


def test_unloadable_segments_report_why():
    mp = MultipartFeature(
        seqid="chr1", featuretype="CDS", start=1, end=9, id="orphan", n_segments=2
    )
    with pytest.raises(RuntimeError, match="neither prefetched nor loadable"):
        _ = mp.segments


def test_repr_says_how_many_segments(split_cds):
    assert "x2" in repr(split_cds)
    assert "CDS[1]" in repr(split_cds.segments[1])


# ---------------------------------------------------------------------------
# to_line(normalized=)
# ---------------------------------------------------------------------------


def test_default_rendering_is_byte_faithful():
    """A file that round-trips through gffbase comes back unchanged, including
    whatever spacing the source used."""
    odd = b"ID=x ;  Name=y"
    f = Feature(seqid="chr1", featuretype="gene", start=1, end=9, attributes=odd, id="x")
    assert str(f).endswith("ID=x ;  Name=y")
    assert f.to_line() == str(f)


def test_normalized_rendering_re_emits_from_the_parsed_mapping():
    """gffutils always re-renders, so this is the form to compare against the
    oracle -- at the cost of the source's exact spacing."""
    odd = b"ID=x ;  Name=y"
    f = Feature(seqid="chr1", featuretype="gene", start=1, end=9, attributes=odd, id="x")
    normalized = f.to_line(normalized=True)
    assert normalized != f.to_line()
    # The space before the delimiter is part of the preceding GFF3 value;
    # normalized rendering must preserve parsed value bytes, not erase them.
    assert normalized.endswith("ID=x ;Name=y")


def test_normalizing_does_not_disturb_the_byte_faithful_path():
    """`to_line(normalized=True)` must not materialize the attribute mapping in
    a way that permanently switches `str()` onto the re-rendered path."""
    f = Feature(
        seqid="chr1", featuretype="gene", start=1, end=9, attributes=b"ID=x ;  Name=y", id="x"
    )
    before = str(f)
    f.to_line(normalized=True)
    assert str(f) == before


def test_normalized_applies_to_every_segment(split_cds):
    lines = split_cds.to_lines(normalized=True)
    assert len(lines) == 2
    assert all(line.endswith("ID=cds1;Parent=t1") for line in lines)
