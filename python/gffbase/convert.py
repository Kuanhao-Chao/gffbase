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
"""Conversions that operate on a `FeatureDB`."""

from __future__ import annotations


def to_bed12(f, db, child_type: str = "exon", name_field: str = "ID") -> str:
    """Build one BED12 line for a top-level feature.

    Superseded by `FeatureDB.bed12`, which this delegates to so the two cannot
    disagree, but still importable because gffutils exports it.

    Two differences from `FeatureDB.bed12` are upstream's, not ours, and are
    preserved: the line ends with a newline, and the thick span always covers
    the whole feature (this function predates thick/thin handling and never
    looks at CDS children).
    """
    if isinstance(f, str):
        f = db[f]
    fields = db.bed12(f, block_featuretype=[child_type], name_field=name_field).split("\t")
    # Columns 7 and 8: whole-feature thick span, in this function's own terms.
    fields[6], fields[7] = str(f.start), str(f.stop)
    return "\t".join(fields) + "\n"


__all__ = ["to_bed12"]
