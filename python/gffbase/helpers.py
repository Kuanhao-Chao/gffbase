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
"""Compatibility helpers."""

from __future__ import annotations

import copy
from pathlib import Path


def merge_attributes(attr1, attr2, numeric_sort: bool = False) -> dict:
    """Union two attribute mappings, key by key.

    Values are deduplicated and **sorted**, not kept in insertion order: the
    result is a set union with a deterministic rendering, which is what makes
    two features merge to the same attributes regardless of which was seen
    first. `numeric_sort` sorts numerically where every value of a key parses
    as a number, so `["2", "10"]` does not come back as `["10", "2"]`.

    Used by every derived-feature method; ported from `gffutils.helpers` so
    that a caller comparing derived output against the oracle sees the same
    ordering.
    """
    merged = {
        k: list(v) if isinstance(v, list) else [v] for k, v in copy.deepcopy(dict(attr2)).items()
    }
    for key, values in copy.deepcopy(dict(attr1)).items():
        values = list(values) if isinstance(values, list) else [values]
        if key in merged:
            merged[key].extend(values)
        else:
            merged[key] = values

    if not numeric_sort:
        return {k: sorted(set(v)) for k, v in merged.items()}

    out = {}
    for key, values in merged.items():
        try:
            out[key] = [text for _, text in sorted((float(v), v) for v in set(values))]
        except ValueError:
            # Not every value is numeric; fall back to lexicographic rather
            # than raising, since a mixed key is normal in real annotations.
            out[key] = sorted(set(values))
    return out


def example_filename(fn: str) -> str:
    """Return the absolute path to a packaged example file under ``tests/data``.

    Mirrors the legacy ``gffutils.example_filename`` API. The legacy package
    shipped fixtures inside the package itself; we point at the test fixtures
    directory bundled with the source distribution.
    """
    here = Path(__file__).resolve().parent
    candidates = [
        here.parent.parent / "tests" / "data" / fn,
        here / "data" / fn,
    ]
    for c in candidates:
        if c.is_file():
            return str(c)
    raise FileNotFoundError(f"example file not found: {fn}")
