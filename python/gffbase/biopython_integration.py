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
"""BioPython interop. Requires the `biopython` extra.

The import is guarded so that `import gffbase.biopython_integration` succeeds
without BioPython -- the parity surface has to exist whether or not an optional
dependency is installed -- but calling either function then raises with an
actionable message rather than a bare `NameError`.
"""

from __future__ import annotations

try:
    from Bio.SeqFeature import FeatureLocation, SeqFeature

    _HAVE_BIOPYTHON = True
except ImportError:  # pragma: no cover - depends on the environment
    _HAVE_BIOPYTHON = False

__all__ = ["from_seqfeature", "to_seqfeature"]

_biopython_strand = {"+": 1, "-": -1, ".": None, "?": 0}
_feature_strand = {v: k for k, v in _biopython_strand.items()}


def _require():
    if not _HAVE_BIOPYTHON:
        raise ImportError(
            "biopython is required for gffbase.biopython_integration; "
            "install it with `pip install gffbase[biopython]` or `pip install biopython`"
        )


def to_seqfeature(feature):
    """`Feature` (or a GFF line) -> `Bio.SeqFeature.SeqFeature`.

    The GFF columns that have no SeqFeature equivalent -- source, score, seqid
    and frame -- are carried in `qualifiers` alongside the column-9
    attributes, so `from_seqfeature` can rebuild the feature exactly.
    """
    _require()
    from gffbase.feature import Feature, feature_from_line

    if isinstance(feature, str):
        feature = feature_from_line(feature)
    elif not isinstance(feature, Feature):
        raise TypeError(f"expected a Feature or a GFF line, got {type(feature)!r}")

    qualifiers = {
        "source": [feature.source],
        "score": [feature.score],
        "seqid": [feature.seqid],
        "frame": [feature.frame],
    }
    qualifiers.update({k: list(v) for k, v in feature.attributes.items()})
    # BioPython locations are 0-based half-open; GFF is 1-based closed.
    #
    # `strand` belongs to the LOCATION, not the SeqFeature. BioPython removed
    # the `SeqFeature(strand=...)` argument, so passing it -- as gffutils does
    # -- raises `TypeError` on any current install.
    return SeqFeature(
        FeatureLocation(feature.start - 1, feature.stop, strand=_biopython_strand[feature.strand]),
        id=feature.id,
        type=feature.featuretype,
        qualifiers=qualifiers,
    )


def from_seqfeature(s, **kwargs):
    """`Bio.SeqFeature.SeqFeature` -> `Feature`."""
    _require()
    from gffbase.feature import Feature

    source = s.qualifiers.get("source", ["."])[0]
    score = s.qualifiers.get("score", ["."])[0]
    seqid = s.qualifiers.get("seqid", ["."])[0]
    frame = s.qualifiers.get("frame", ["."])[0]
    strand = _feature_strand[s.location.strand]
    start = int(s.location.start) + 1
    stop = int(s.location.end)
    featuretype = s.type
    id = s.id
    attributes = {
        k: v for k, v in s.qualifiers.items() if k not in ("source", "score", "seqid", "frame")
    }
    return Feature(
        seqid=seqid,
        source=source,
        featuretype=featuretype,
        start=start,
        end=stop,
        score=score,
        strand=strand,
        frame=frame,
        attributes=attributes,
        id=id,
        **kwargs,
    )
