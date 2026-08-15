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
"""UCSC genomic binning. Used only for the legacy SQLite export path; the
runtime query layer uses DuckDB's R-tree (or the seqstart B-tree fallback)
and never touches `bin`. Port of the relevant logic from `gffutils/bins.py`.
"""

from __future__ import annotations

#: Bin-number offsets per level, coarsest-resolution level LAST.
#:
#: These must match `gffutils.bins.OFFSETS` exactly, and for a long time they
#: did not: the top level (4681) was missing and the fallback was 0 instead of
#: 1, so every bin gffbase computed was one level off. On ten of eleven
#: representative coordinate ranges the answer differed from the oracle's.
#:
#: It is not cosmetic. `gffutils.FeatureDB.region(completely_within=True)`
#: filters with `bin = ?` over the set of bins overlapping the query, so an
#: exported database with the wrong bins answers those queries with nothing at
#: all -- no error, just an empty result.
_BINOFFSETS = (
    4096 + 512 + 64 + 8 + 1,  # bins 4681-585
    512 + 64 + 8 + 1,  # bins 585-73
    64 + 8 + 1,  # bins 73-9
    8 + 1,  # bins 9-1
    1,  # bin  0
)
_BINFIRSTSHIFT = 17
_BINNEXTSHIFT = 3

#: Past this, UCSC binning stops being meaningful and everything lands in
#: bin 1 -- "somewhere on the chromosome".
MAX_CHROM_SIZE = 2**29


def bin_from_coords(start: int | None, end: int | None) -> int | None:
    """The smallest UCSC bin containing [start, end], 1-based and inclusive.

    A faithful port of ``gffutils.bins.bins(start, stop, fmt="gff", one=True)``,
    including its guards: coordinates at or past `MAX_CHROM_SIZE`, and negative
    coordinates (which real GFF files do contain), both collapse to bin 1.

    Returns None for a missing coordinate, which a GFF row may legally have.
    Used only by the SQLite export; the runtime query layer uses the R-tree or
    the seqstart B-tree and never touches `bin`.
    """
    if start is None or end is None:
        return None
    if start >= MAX_CHROM_SIZE or end >= MAX_CHROM_SIZE:
        return 1
    if start < 0 or end < 0:
        return 1

    # `fmt="gff"` means 1-based starts, so the start -- and only the start --
    # is shifted down by one before bucketing.
    start_bin = (start - 1) >> _BINFIRSTSHIFT
    end_bin = end >> _BINFIRSTSHIFT
    for offset in _BINOFFSETS:
        if start_bin == end_bin:
            return offset + start_bin
        start_bin >>= _BINNEXTSHIFT
        end_bin >>= _BINNEXTSHIFT
    # Unreachable for coordinates below MAX_CHROM_SIZE: after five levels the
    # shift totals 29 bits, so both operands are 0 and the last level matches.
    return 1  # pragma: no cover
