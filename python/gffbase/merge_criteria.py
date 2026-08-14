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
"""Merge predicates — pure functions consumed by ``FeatureDB.merge``.

Signature: ``(acc: Feature, cur: Feature, components: list[Feature]) -> bool``.
``acc`` is the running accumulator; ``cur`` is the candidate to fold in;
``components`` is the list of already-folded features. All callables return
True if the candidate should be merged into the accumulator.

Mirrors the legacy `gffutils.merge_criteria` module.
"""

from __future__ import annotations


def seqid(acc, cur, components):
    return acc.seqid == cur.seqid


def strand(acc, cur, components):
    return acc.strand == cur.strand


def feature_type(acc, cur, components):
    return acc.featuretype == cur.featuretype


def exact_coordinates_only(acc, cur, components):
    return acc.start == cur.start and acc.end == cur.end


def overlap_end_inclusive(acc, cur, components):
    """True if ``cur.start`` falls within or immediately after ``acc``."""
    return acc.start <= cur.start <= acc.end + 1


def overlap_start_inclusive(acc, cur, components):
    return acc.start <= cur.end + 1 <= acc.end + 1


def overlap_any_inclusive(acc, cur, components):
    return overlap_end_inclusive(acc, cur, components) or overlap_start_inclusive(
        acc, cur, components
    )


# The three threshold factories below are RANGE tests, not distance tests.
#
# They used to compute `abs(acc.end - cur.start) <= threshold`, which reads
# naturally but answers a different question: it asks how far apart two
# boundaries are, and therefore *rejects a feature that lies entirely inside
# the accumulator* -- distance zero is not what a contained feature produces.
# The oracle asks whether `cur` starts anywhere within the accumulator extended
# by the threshold, which admits containment and is what "overlap within
# `threshold`" means to a caller.
#
# Concretely, with `acc = (1, 100)`, `cur = (50, 200)`, `threshold = 5`:
# the old form gave `abs(100 - 50) = 50 <= 5` -> False, and these two plainly
# overlapping features did not merge. The oracle gives `1 <= 50 <= 105` -> True.


def overlap_end_threshold(threshold: int):
    """`cur` starts within the accumulator, allowing a gap of `threshold`."""

    def predicate(acc, cur, components):
        return acc.start <= cur.start <= acc.end + threshold

    return predicate


def overlap_start_threshold(threshold: int):
    """`cur` ends within the accumulator, allowing a gap of `threshold`."""

    def predicate(acc, cur, components):
        return acc.start - threshold <= cur.end + 1 <= acc.end + 1

    return predicate


def overlap_any_threshold(threshold: int):
    """Either end qualifies."""
    end_thr = overlap_end_threshold(threshold)
    start_thr = overlap_start_threshold(threshold)

    def predicate(acc, cur, components):
        return end_thr(acc, cur, components) or start_thr(acc, cur, components)

    return predicate
