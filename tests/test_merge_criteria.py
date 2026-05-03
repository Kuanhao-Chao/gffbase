# ---------------------------------------------------------------------------
# Author: Kuan-Hao Chao <kuanhao.chao@gmail.com>
# ---------------------------------------------------------------------------
"""Exhaustive tests for merge_criteria predicates."""

from __future__ import annotations

import pytest

from gffbase import Feature, merge_criteria as mc


def F(start, end, **kw):
    defaults = dict(seqid="chr1", source=".", featuretype="exon",
                    score=".", strand="+", frame=".", dialect={"fmt": "gff3"})
    defaults.update(kw)
    return Feature(start=start, end=end, **defaults)


def test_seqid_predicate():
    a, b = F(1, 10), F(20, 30)
    assert mc.seqid(a, b, [a]) is True
    c = F(1, 10, seqid="chr2")
    assert mc.seqid(a, c, [a]) is False


def test_strand_predicate():
    a, b = F(1, 10, strand="+"), F(1, 10, strand="+")
    assert mc.strand(a, b, [a]) is True
    c = F(1, 10, strand="-")
    assert mc.strand(a, c, [a]) is False


def test_feature_type_predicate():
    a, b = F(1, 10, featuretype="exon"), F(20, 30, featuretype="exon")
    assert mc.feature_type(a, b, [a]) is True
    c = F(20, 30, featuretype="CDS")
    assert mc.feature_type(a, c, [a]) is False


def test_exact_coordinates_only():
    a, b = F(100, 200), F(100, 200)
    assert mc.exact_coordinates_only(a, b, [a]) is True
    c = F(100, 201)
    assert mc.exact_coordinates_only(a, c, [a]) is False


@pytest.mark.parametrize("acc_end,cur_start,expected", [
    (200, 200, True),    # touching
    (200, 201, True),    # adjacent (end+1)
    (200, 202, False),   # gap
    (200, 100, True),    # cur starts inside acc
])
def test_overlap_end_inclusive(acc_end, cur_start, expected):
    acc = F(1, acc_end)
    cur = F(cur_start, cur_start + 10)
    assert mc.overlap_end_inclusive(acc, cur, [acc]) is expected


def test_overlap_start_inclusive():
    acc = F(100, 200)
    # cur ends adjacent to acc.start (within or just before)
    cur1 = F(50, 99)   # cur.end+1 = 100, falls in [100, 201]
    assert mc.overlap_start_inclusive(acc, cur1, [acc]) is True
    cur2 = F(50, 50)   # cur.end+1 = 51, NOT in [100, 201]
    assert mc.overlap_start_inclusive(acc, cur2, [acc]) is False


def test_overlap_any_inclusive():
    acc = F(100, 200)
    assert mc.overlap_any_inclusive(acc, F(150, 250), [acc]) is True
    assert mc.overlap_any_inclusive(acc, F(50, 99), [acc]) is True
    assert mc.overlap_any_inclusive(acc, F(300, 400), [acc]) is False


def test_overlap_end_threshold_factory():
    pred = mc.overlap_end_threshold(5)
    acc = F(1, 100)
    assert pred(acc, F(103, 200), [acc]) is True   # |100-103| = 3
    assert pred(acc, F(120, 200), [acc]) is False  # |100-120| = 20


def test_overlap_start_threshold_factory():
    pred = mc.overlap_start_threshold(5)
    acc = F(100, 200)
    assert pred(acc, F(50, 96), [acc]) is True     # |100 - 96| = 4
    assert pred(acc, F(50, 80), [acc]) is False    # |100 - 80| = 20


def test_overlap_any_threshold_factory():
    pred = mc.overlap_any_threshold(5)
    acc = F(100, 200)
    assert pred(acc, F(202, 300), [acc]) is True       # end-side
    assert pred(acc, F(50, 96), [acc]) is True         # start-side
    assert pred(acc, F(300, 400), [acc]) is False      # too far on either
