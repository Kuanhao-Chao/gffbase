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
"""Dialect template. Mirrors `gffutils.constants.dialect` so Phase 4's API
layer can pass these dicts straight to backwards-compat consumers.
"""

from __future__ import annotations


def default_dialect() -> dict:
    return {
        "fmt": "gff3",
        "field separator": ";",
        "keyval separator": "=",
        "multival separator": ",",
        "leading semicolon": False,
        "trailing semicolon": False,
        "quoted GFF2 values": False,
        "repeated keys": False,
        "semicolon in quotes": False,
        "order": [],
    }


def merge_dialects(samples: list[dict]) -> dict:
    """Reconcile per-line dialect observations into one. OR for booleans,
    plurality vote for separators, first-appearance order for keys."""
    if not samples:
        return default_dialect()

    n_gtf = sum(1 for s in samples if s.get("fmt") == "gtf")
    fmt = "gtf" if n_gtf > len(samples) - n_gtf else "gff3"
    keyval = " " if fmt == "gtf" else "="

    # Plurality vote, ties broken by first appearance.
    #
    # `max(set(...), key=...)` looks equivalent but is not: iteration order of
    # a `set` of strings depends on PYTHONHASHSEED, which is randomized per
    # process, so a tie resolved this way picks a different winner on
    # different runs -- and the separator chosen here is the one used when a
    # feature is re-serialized. The same file could round-trip to different
    # text run to run. Counting over the ordered list keeps the result a
    # function of the input alone. (The Rust engine had the same defect via
    # HashMap iteration order; both are fixed the same way.)
    field_seps = [s.get("field separator", ";") for s in samples]
    field_sep = max(dict.fromkeys(field_seps), key=field_seps.count)

    out = default_dialect()
    out["fmt"] = fmt
    out["keyval separator"] = keyval
    out["field separator"] = field_sep
    for k in (
        "leading semicolon",
        "trailing semicolon",
        "quoted GFF2 values",
        "repeated keys",
        "semicolon in quotes",
    ):
        out[k] = any(s.get(k, False) for s in samples)

    seen = set()
    order: list[str] = []
    for s in samples:
        for key in s.get("order", []):
            if key not in seen:
                seen.add(key)
                order.append(key)
    out["order"] = order
    return out
