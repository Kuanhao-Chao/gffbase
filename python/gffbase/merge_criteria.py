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
    """Same sequence. Almost always wanted -- omitting it merges features on
    different chromosomes into one."""
    return acc.seqid == cur.seqid


def strand(acc, cur, components):
    """Same orientation. Leave this out to merge regardless of strand."""
    return acc.strand == cur.strand


def feature_type(acc, cur, components):
    """Same featuretype, so exons do not merge with CDSs."""
    return acc.featuretype == cur.featuretype


def exact_coordinates_only(acc, cur, components):
    """Identical span. Merges duplicates, nothing else."""
    return acc.start == cur.start and acc.end == cur.end


def overlap_end_inclusive(acc, cur, components):
    """`cur` starts within `acc`, or immediately after it.

    "Immediately after" is the `+ 1`: two features that abut with no gap are
    adjacent, not overlapping, and merging them is normally what a caller
    wants when collapsing exon runs.
    """
    return acc.start <= cur.start <= acc.end + 1


def overlap_start_inclusive(acc, cur, components):
    """`cur` ends within `acc`, or immediately before it."""
    return acc.start <= cur.end + 1 <= acc.end + 1


def overlap_any_inclusive(acc, cur, components):
    """Either end qualifies."""
    return overlap_end_inclusive(acc, cur, components) or overlap_start_inclusive(
        acc, cur, components
    )


def overlap_end_threshold(threshold: int):
    """`cur` starts within the accumulator, allowing a gap of `threshold`.

    **Changed in 0.2.0.** This and the two factories below are RANGE tests,
    not distance tests. They used to compute
    `abs(acc.end - cur.start) <= threshold`, which reads naturally but answers
    a different question: it asks how far apart two boundaries are, and so
    *rejects a feature lying entirely inside the accumulator* -- the most
    unambiguous overlap there is.

    Concretely, with `acc = (1, 100)`, `cur = (50, 200)`, `threshold = 5`, the
    old form gave `abs(100 - 50) = 50 <= 5` -> False, and these two plainly
    overlapping features did not merge. This form gives `1 <= 50 <= 105` ->
    True.

    If you call `merge` or `merge_all` with one of these, the set of features
    that merge has changed. Nothing else in the merge machinery did.
    """

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
