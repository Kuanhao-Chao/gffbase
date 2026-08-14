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
"""Exhaustive tests for merge_criteria predicates."""

from __future__ import annotations

import pytest
from gffbase import Feature
from gffbase import merge_criteria as mc


def F(start, end, **kw):
    defaults = dict(
        seqid="chr1",
        source=".",
        featuretype="exon",
        score=".",
        strand="+",
        frame=".",
        dialect={"fmt": "gff3"},
    )
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


@pytest.mark.parametrize(
    "acc_end,cur_start,expected",
    [
        (200, 200, True),  # touching
        (200, 201, True),  # adjacent (end+1)
        (200, 202, False),  # gap
        (200, 100, True),  # cur starts inside acc
    ],
)
def test_overlap_end_inclusive(acc_end, cur_start, expected):
    acc = F(1, acc_end)
    cur = F(cur_start, cur_start + 10)
    assert mc.overlap_end_inclusive(acc, cur, [acc]) is expected


def test_overlap_start_inclusive():
    acc = F(100, 200)
    # cur ends adjacent to acc.start (within or just before)
    cur1 = F(50, 99)  # cur.end+1 = 100, falls in [100, 201]
    assert mc.overlap_start_inclusive(acc, cur1, [acc]) is True
    cur2 = F(50, 50)  # cur.end+1 = 51, NOT in [100, 201]
    assert mc.overlap_start_inclusive(acc, cur2, [acc]) is False


def test_overlap_any_inclusive():
    acc = F(100, 200)
    assert mc.overlap_any_inclusive(acc, F(150, 250), [acc]) is True
    assert mc.overlap_any_inclusive(acc, F(50, 99), [acc]) is True
    assert mc.overlap_any_inclusive(acc, F(300, 400), [acc]) is False


def test_overlap_end_threshold_factory():
    pred = mc.overlap_end_threshold(5)
    acc = F(1, 100)
    assert pred(acc, F(103, 200), [acc]) is True  # starts 3 past the end
    assert pred(acc, F(120, 200), [acc]) is False  # starts 20 past the end


def test_overlap_start_threshold_factory():
    pred = mc.overlap_start_threshold(5)
    acc = F(100, 200)
    assert pred(acc, F(50, 96), [acc]) is True  # ends 4 before the start
    assert pred(acc, F(50, 80), [acc]) is False  # ends 20 before the start


def test_overlap_any_threshold_factory():
    pred = mc.overlap_any_threshold(5)
    acc = F(100, 200)
    assert pred(acc, F(202, 300), [acc]) is True  # end-side
    assert pred(acc, F(50, 96), [acc]) is True  # start-side
    assert pred(acc, F(300, 400), [acc]) is False  # too far on either


# ---------------------------------------------------------------------------
# The case that separates a RANGE test from a DISTANCE test.
# ---------------------------------------------------------------------------
#
# The three tests above pass under both formulas, so they never pinned the
# semantics they were believed to pin. These do.
#
# The predicates used to compute `abs(acc.end - cur.start) <= threshold`, a
# distance between two boundaries. That rejects a feature lying entirely
# inside the accumulator -- the most unambiguous overlap there is -- because
# its start can be arbitrarily far from the accumulator's end.


@pytest.mark.parametrize(
    "cur",
    [
        pytest.param(F(50, 200), id="overlapping-tail"),
        pytest.param(F(50, 60), id="fully-contained"),
        pytest.param(F(1, 100), id="identical"),
    ],
)
def test_end_threshold_admits_features_that_plainly_overlap(cur):
    """`acc = (1, 100)`. Each of these overlaps it and must merge, at a
    threshold small enough that a distance test would refuse them."""
    acc = F(1, 100)
    assert mc.overlap_end_threshold(5)(acc, cur, [acc]) is True


def test_end_threshold_is_not_a_distance_test():
    """The regression, stated directly: distance 50, but contained."""
    acc = F(1, 100)
    contained = F(50, 200)
    assert abs(acc.end - contained.start) == 50  # far, by the old measure
    assert mc.overlap_end_threshold(5)(acc, contained, [acc]) is True


def test_start_threshold_admits_a_contained_feature():
    acc = F(100, 200)
    assert mc.overlap_start_threshold(5)(acc, F(120, 180), [acc]) is True


def test_thresholds_still_refuse_a_genuinely_distant_feature():
    """The widening must not make the predicates vacuous."""
    acc = F(100, 200)
    assert mc.overlap_end_threshold(5)(acc, F(400, 500), [acc]) is False
    assert mc.overlap_start_threshold(5)(acc, F(1, 50), [acc]) is False
    assert mc.overlap_any_threshold(5)(acc, F(400, 500), [acc]) is False


@pytest.mark.parametrize(
    "cur", [F(150, 300), F(200, 300), F(201, 300), F(202, 300), F(120, 130), F(1, 50)]
)
def test_the_threshold_families_meet_the_inclusive_ones(cur):
    """Where each threshold predicate degrades to its `_inclusive` sibling.

    The two do NOT meet at the same threshold, and the asymmetry is real
    rather than a porting slip:

        overlap_end_threshold(1)   == overlap_end_inclusive
        overlap_start_threshold(0) == overlap_start_inclusive

    because the end-side formula compares against `acc.end + threshold` while
    the start-side one already carries the `+ 1` on the other operand
    (`cur.end + 1 <= acc.end + 1`). Pinned so that "tidying" one of them into
    symmetry with the other has to be a deliberate, visible change.
    """
    acc = F(100, 200)
    assert mc.overlap_end_threshold(1)(acc, cur, [acc]) is mc.overlap_end_inclusive(acc, cur, [acc])
    assert mc.overlap_start_threshold(0)(acc, cur, [acc]) is mc.overlap_start_inclusive(
        acc, cur, [acc]
    )
