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

_BINOFFSETS = (512 + 64 + 8 + 1, 64 + 8 + 1, 8 + 1, 1, 0)
_BINFIRSTSHIFT = 17
_BINNEXTSHIFT = 3


def bin_from_coords(start: int, end: int) -> int:
    """Return the smallest UCSC bin that contains [start, end] (1-based, inclusive).
    `start`/`end` here are converted to 0-based half-open as the original code
    does. Used only by the SQLite export to populate the legacy `bin` column.
    """
    start0 = start - 1
    end0 = end
    start_bin = start0 >> _BINFIRSTSHIFT
    end_bin = (end0 - 1) >> _BINFIRSTSHIFT
    for offset in _BINOFFSETS:
        if start_bin == end_bin:
            return offset + start_bin
        start_bin >>= _BINNEXTSHIFT
        end_bin >>= _BINNEXTSHIFT
    return 0
