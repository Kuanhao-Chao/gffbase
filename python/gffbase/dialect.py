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
"""Dialect template. Mirrors `gffutils.constants.dialect` so this API
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


#: The dialect keys decided by vote. `order` is not one of them: it is the
#: union of every sample's keys in first-appearance order.
_VOTED_KEYS = (
    "fmt",
    "field separator",
    "keyval separator",
    "multival separator",
    "leading semicolon",
    "trailing semicolon",
    "quoted GFF2 values",
    "repeated keys",
    "semicolon in quotes",
)


def merge_dialects(samples: list[dict]) -> dict:
    """Reconcile per-line dialect observations into one, as gffutils'
    `helpers._choose_dialect` does.

    Every key is decided by its own vote; a line weighs as much as it has
    distinct attributes; a tie goes to the value seen first. This used to be a
    plain majority for the format and an OR over the flags -- so one quoted
    line re-quoted a whole file on output, and an attribute-less line (a
    five-column row) could outvote a real one.

    The tally is an insertion-ordered dict rather than a `set`: iteration order
    of a set of strings depends on PYTHONHASHSEED, so a tie resolved through
    one picked a different winner on different runs, and the separator chosen
    here is the one a feature is re-serialized with. (The Rust engine had the
    same defect via HashMap iteration order; both are fixed the same way.)
    """
    if not samples:
        return default_dialect()

    defaults = default_dialect()
    out = default_dialect()
    for key in _VOTED_KEYS:
        tally: dict = {}
        for sample in samples:
            value = sample.get(key, defaults[key])
            tally[value] = tally.get(value, 0) + len(sample.get("order", ()))
        # `max` returns the first maximum it meets, and dicts keep insertion
        # order, so a tie goes to the first-seen value.
        out[key] = max(tally, key=tally.__getitem__)

    seen = set()
    order: list[str] = []
    for s in samples:
        for key in s.get("order", []):
            if key not in seen:
                seen.add(key)
                order.append(key)
    out["order"] = order
    return out


def normalize_dialect(dialect: dict) -> dict:
    """A complete dialect from a possibly partial one.

    `create_db(dialect={"fmt": "gtf"})` is a reasonable thing to write, and
    everything downstream (rendering, the stored `meta` row) expects every
    key. Missing keys take the default; a GTF gets GTF's key/value separator.
    """
    if not isinstance(dialect, dict):
        raise TypeError(f"dialect must be a dict; got {type(dialect).__name__}")
    fmt = dialect.get("fmt", "gff3")
    if fmt not in ("gff3", "gtf"):
        raise ValueError(f"dialect['fmt'] must be 'gff3' or 'gtf'; got {fmt!r}")
    out = default_dialect()
    if fmt == "gtf":
        out.update(
            {
                "keyval separator": " ",
                "field separator": "; ",
                "quoted GFF2 values": True,
                "trailing semicolon": True,
            }
        )
    out.update(dialect)
    return out
