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
"""UCSC genomic binning, the public surface.

`gffbase._bins` has the arithmetic and is what the SQLite export uses; this
module is the compatibility face of it, under the names `gffutils.bins`
exports. The two must not diverge, so everything here delegates rather than
reimplements — `tests/test_compat_surface.py` pins that.

The one thing this adds over `_bins` is `one=False`: the *set* of bins a range
overlaps, rather than the single smallest bin containing it. That is what a
query needs (`WHERE bin IN (...)`) as opposed to what a write needs, and it is
required by `helpers.make_query`.
"""

from __future__ import annotations

from gffbase._bins import (
    _BINFIRSTSHIFT,
    _BINNEXTSHIFT,
    _BINOFFSETS,
    MAX_CHROM_SIZE,
)

#: Shift per level: each level's bins are 8x the width of the one below.
NEXT_SHIFT = _BINNEXTSHIFT

#: The finest bin is 2**17 wide.
FIRST_SHIFT = _BINFIRSTSHIFT

#: Bin number at the start of each level, finest first.
OFFSETS = list(_BINOFFSETS)

#: How much to subtract from `start` to reach 0-based coordinates. GFF is
#: 1-based and BED is 0-based, and getting this wrong shifts every bin at a
#: level boundary.
COORD_OFFSETS = {"bed": 0, "gff": 1}

__all__ = [
    "COORD_OFFSETS",
    "FIRST_SHIFT",
    "MAX_CHROM_SIZE",
    "NEXT_SHIFT",
    "OFFSETS",
    "bins",
    "print_bin_sizes",
]


def bins(start: int, stop: int, fmt: str = "gff", one: bool = True):
    """The UCSC bin(s) for the range `[start, stop]`.

    Parameters
    ----------
    start, stop
        Range endpoints, inclusive.
    fmt : {"gff", "bed"}
        Coordinate convention of `start`; see `COORD_OFFSETS`.
    one : bool
        True (default) returns the single smallest bin that fully contains the
        range -- what you store on a row. False returns the `set` of every bin
        the range overlaps at any level -- what you query with, since a feature
        in a coarser bin can still overlap your range.

    Ranges at or beyond `MAX_CHROM_SIZE`, and negative ones, collapse to bin 1
    ("somewhere on this chromosome"), which is how the scheme degrades rather
    than raising on a coordinate it cannot represent.
    """
    # The guards test the ORIGINAL coordinates, before the format offset is
    # applied. That ordering is load-bearing: a GFF `start=0` -- which is not a
    # legal GFF coordinate, but occurs -- passes the negative check and only
    # then becomes -1, which is how it reaches the fall-through below.
    if start >= MAX_CHROM_SIZE or stop >= MAX_CHROM_SIZE:
        return 1 if one else {1}
    if start < 0 or stop < 0:
        return 1 if one else {1}

    start = (start - COORD_OFFSETS[fmt]) >> FIRST_SHIFT
    stop = stop >> FIRST_SHIFT

    # Everything fits within the chromosome, which is bin 1.
    found = {1}
    for offset in OFFSETS:
        if one and start == stop:
            # After the shifting, `start` counts bins at this level, so the
            # bin id is the level's offset plus it.
            return offset + start
        found.update(range(offset + start, offset + stop + 1))
        start >>= NEXT_SHIFT
        stop >>= NEXT_SHIFT

    # Reached only when `one=True` never found a level containing both ends --
    # i.e. the shifted start went negative. The oracle returns the accumulated
    # SET here despite `one=True`, so a caller can get either type back from
    # one call. Reproduced deliberately rather than "corrected", because the
    # legacy `bin` column has to match whatever gffutils wrote.
    return found


def print_bin_sizes() -> None:
    """Report each level's bin count and width. A debugging aid, kept because
    the oracle exports it and upstream examples call it."""
    for i, offset in enumerate(OFFSETS):
        binstart = offset
        try:
            binstop = OFFSETS[i + 1]
        except IndexError:
            binstop = binstart
        actual_size: float = 2 ** (FIRST_SHIFT + i * NEXT_SHIFT)
        suffix = "bp"
        for candidate in ("bp", "Kb", "Mb", "Gb"):
            suffix = candidate
            if actual_size < 1024:
                break
            actual_size /= 1024.0
        print(f"level: {i}, bins {binstart}-{binstop}, bin size {actual_size:.1f} {suffix}")
