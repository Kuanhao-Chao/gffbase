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
"""Pure-Python column-9 attribute parser. Shape mirrors the Rust implementation
exactly: returns `(pairs, dialect_observation)` where `pairs` is a list of
`(key, value, multivalue_index)`.
"""

from __future__ import annotations

import urllib.parse

from gffbase.dialect import default_dialect


def parse_attributes(blob: str) -> tuple[list[tuple[str, str, int]], dict]:
    obs = default_dialect()
    if not blob:
        return [], obs

    obs["leading semicolon"] = blob.lstrip().startswith(";")
    obs["trailing semicolon"] = blob.rstrip().endswith(";")
    if "; " in blob:
        obs["field separator"] = "; "
    elif " ; " in blob:
        obs["field separator"] = " ; "
    else:
        obs["field separator"] = ";"

    segments = _split_top_level_semicolons(blob, obs)

    pairs: list[tuple[str, str, int]] = []
    keys_seen: dict[str, int] = {}
    order: list[str] = []
    detected_fmt = None

    for seg in segments:
        seg = seg.strip()
        if not seg:
            continue
        key, raw_val, kv_sep = _split_keyval(seg)
        if not key:
            continue
        local_fmt = "gff3" if kv_sep == "=" else "gtf"
        if detected_fmt is None:
            detected_fmt = local_fmt

        clean_val, was_quoted = _strip_quotes(raw_val)
        if was_quoted:
            obs["quoted GFF2 values"] = True

        if local_fmt == "gff3":
            multi_values = _split_unquoted_commas(clean_val)
        else:
            multi_values = [clean_val]

        if key not in order:
            order.append(key)
        counter = keys_seen.get(key, 0)
        if counter > 0:
            obs["repeated keys"] = True

        for v in multi_values:
            decoded = urllib.parse.unquote(v) if local_fmt == "gff3" else v
            pairs.append((key, decoded, counter))
            counter += 1
        keys_seen[key] = counter

    obs["fmt"] = detected_fmt or "gff3"
    obs["keyval separator"] = " " if obs["fmt"] == "gtf" else "="
    obs["order"] = order
    return pairs, obs


def _split_top_level_semicolons(blob: str, obs: dict) -> list[str]:
    out: list[str] = []
    start = 0
    in_quotes = False
    for i, ch in enumerate(blob):
        if ch == '"':
            in_quotes = not in_quotes
        elif ch == ";":
            if in_quotes:
                obs["semicolon in quotes"] = True
            else:
                out.append(blob[start:i])
                start = i + 1
    out.append(blob[start:])
    return out


def _split_keyval(seg: str) -> tuple[str, str, str]:
    eq = seg.find("=")
    if eq != -1:
        return seg[:eq].strip(), seg[eq + 1 :].strip(), "="
    # GTF-style: split on first whitespace.
    for i, ch in enumerate(seg):
        if ch in (" ", "\t"):
            return seg[:i].strip(), seg[i:].lstrip().strip(), " "
    return seg.strip(), "", "="


def _strip_quotes(s: str) -> tuple[str, bool]:
    s = s.strip()
    if len(s) >= 2 and s.startswith('"') and s.endswith('"'):
        return s[1:-1], True
    return s, False


def _split_unquoted_commas(s: str) -> list[str]:
    out: list[str] = []
    start = 0
    in_quotes = False
    for i, ch in enumerate(s):
        if ch == '"':
            in_quotes = not in_quotes
        elif ch == "," and not in_quotes:
            out.append(s[start:i])
            start = i + 1
    out.append(s[start:])
    return out
