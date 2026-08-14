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
"""pybedtools interop. Requires the `pybedtools` extra.

Guarded import, for the same reason as `biopython_integration`: the module has
to import so the surface exists, and the failure has to be actionable at the
point of use. gffutils imports pybedtools unguarded at module scope, so
`import gffutils.pybedtools_integration` raises outright without it.
"""

from __future__ import annotations

import os

__all__ = ["to_bedtool", "tsses"]


def _pybedtools():
    try:
        import pybedtools
    except ImportError as exc:  # pragma: no cover - depends on the environment
        raise ImportError(
            "pybedtools is required for gffbase.pybedtools_integration; "
            "install it with `pip install gffbase[pybedtools]` or `pip install pybedtools`"
        ) from exc
    return pybedtools


def to_bedtool(iterator):
    """Any feature iterator -> `pybedtools.BedTool`.

    Lazily: the generator is not drained here, so a BedTool can be built over a
    database larger than memory.
    """
    pybedtools = _pybedtools()
    from gffbase.helpers import asinterval

    def gen():
        for feature in iterator:
            yield asinterval(feature)

    return pybedtools.BedTool(gen())


def tsses(
    db,
    merge_overlapping=False,
    attrs=None,
    attrs_sep=":",
    merge_kwargs=None,
    as_bed6=False,
    bedtools_227_or_later=True,
):
    """One 1 bp feature per transcript, at its TSS.

    The TSS is the 5' end, so it is `start` on the plus strand and `end` on
    the minus -- getting that backwards is the classic error here, and it is
    silent because the output still looks like a valid BED file.
    """
    pybedtools = _pybedtools()
    from gffbase.feature import Feature

    _ = bedtools_227_or_later  # accepted for signature parity; not consulted

    def gen():
        for transcript in db.features_of_type("transcript"):
            if transcript.start is None or transcript.end is None:
                continue
            if transcript.strand == "-":
                start = end = transcript.end
            else:
                start = end = transcript.start
            attributes = dict(transcript.attributes)
            yield Feature(
                seqid=transcript.seqid,
                source=db.derived_source,
                featuretype=f"{transcript.featuretype}_TSS",
                start=start,
                end=end,
                score=transcript.score,
                strand=transcript.strand,
                attributes=attributes,
                dialect=db.dialect,
            )

    x = to_bedtool(gen()).saveas()
    if as_bed6 or merge_overlapping or attrs:
        x = x.each(pybedtools.featurefuncs.gff2bed, name_field=attrs or "ID").saveas()
    if merge_overlapping:
        kwargs = dict(o="distinct", c=4, s=True)
        kwargs.update(merge_kwargs or {})
        x = x.sort().merge(**kwargs).saveas()
    if attrs and attrs_sep:
        pass
    return x.sort().saveas() if os.environ.get("GFFBASE_TSSES_SORT", "1") == "1" else x
