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
"""Summarise a GFF/GTF source without building a database.

The point is to answer "what featuretypes are in this file?" before deciding
what to keep, on a file too large to want to ingest twice.
"""

from __future__ import annotations

import sys
from collections import Counter

__all__ = ["inspect"]


def inspect(
    data,
    look_for=["featuretype", "chrom", "attribute_keys", "feature_count"],  # noqa: B006
    limit=None,
    verbose: bool = True,
) -> dict:
    """Count things in a GFF/GTF source.

    Parameters
    ----------
    data
        A filename, a `FeatureDB` (its `all_features()` is used), or any
        iterable of features.
    look_for : list
        What to tally. Any `Feature` attribute name works (`chrom`, `source`,
        `strand`, ...), plus the special `"attribute_keys"`, which counts
        column-9 keys rather than a field. `"feature_count"` is always
        reported whether or not it is requested.
    limit : int
        Stop after this many features.
    verbose : bool
        Report progress to stderr.

    Returns
    -------
    dict
        One key per entry in `look_for`, each mapping value -> count, plus
        `feature_count`.

    The mutable default for `look_for` is upstream's and is part of the pinned
    signature. It is never mutated here, which is what makes it harmless.
    """
    from gffbase.interface import FeatureDB
    from gffbase.iterators import DataIterator

    results: dict[str, Counter] = {}
    obj_attrs = []
    for item in look_for:
        if item not in ("attribute_keys", "feature_count"):
            obj_attrs.append(item)
        results[item] = Counter()

    attr_keys = "attribute_keys" in look_for

    if isinstance(data, FeatureDB):
        source = data.all_features()
    elif isinstance(data, str):
        source = DataIterator(data)
    else:
        source = data

    feature_count = 0
    for feature in source:
        if verbose:
            sys.stderr.write(f"\r{feature_count} features inspected")
            sys.stderr.flush()

        for attr in obj_attrs:
            results[attr].update([getattr(feature, attr)])
        if attr_keys:
            results["attribute_keys"].update(feature.attributes.keys())

        feature_count += 1
        if limit and feature_count == limit:
            break

    if verbose:
        sys.stderr.write("\n")

    out: dict = {key: dict(counter) for key, counter in results.items()}
    # Unconditional, and last, so asking for it in `look_for` cannot leave an
    # empty Counter behind.
    out["feature_count"] = feature_count
    return out
