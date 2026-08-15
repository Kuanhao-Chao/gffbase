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

    The result is streamed into a temporary file rather than left wrapping the
    generator, because a generator-backed `BedTool` is a one-shot stream that
    reports its emptiness inconsistently:

        bt = pybedtools.BedTool(gen())
        len(list(bt))   # 0
        len(bt)         # 3

    Verified against pybedtools 0.12.0 with no gffbase code involved. gffutils
    returns the generator-backed object, so `list(to_bedtool(...))` there is
    silently empty -- a wrong answer that looks like an empty database.

    `saveas()` costs a temp file, not memory, so this still works on a database
    larger than RAM; and nearly every `BedTool` operation materializes to a
    file anyway.
    """
    _pybedtools()
    from pybedtools import BedTool

    from gffbase.helpers import asinterval

    def gen():
        for feature in iterator:
            yield asinterval(feature)

    return BedTool(gen()).saveas()


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
    the minus. Getting that backwards is the classic error here and it is
    silent -- the output is still a valid, plausible-looking BED file, just
    describing the wrong end of every reverse-strand gene.

    Parameters
    ----------
    db : FeatureDB
    merge_overlapping : bool
        Merge TSSes that coincide. Forces BED6 output, since `bedtools merge`
        has no notion of GFF attributes.
    attrs : str or list of str
        Attribute(s) to use for the BED name column. Implies BED6.
    attrs_sep : str
        Joins several `attrs` into one name.
    merge_kwargs : dict
        Passed to `BedTool.merge`; defaults to `dict(o="distinct", c=4, s=True)`.
    as_bed6 : bool
        Convert to BED6 even without merging.
    bedtools_227_or_later : bool
        Accepted for signature compatibility with gffutils and not consulted:
        the column layout it selects between was a workaround for a bedtools
        release from 2017, and `merge_kwargs` expresses the same thing
        explicitly for anyone who needs it.

    Returns
    -------
    pybedtools.BedTool, coordinate-sorted.
    """
    _pybedtools()
    from pybedtools import featurefuncs

    from gffbase.feature import Feature

    del bedtools_227_or_later  # see the docstring

    #: Where a multi-attribute name is staged. `gff2bed`'s `name_field` takes
    #: ONE attribute key, so joining several has to happen before conversion.
    name_key = "_gffbase_tss_name"
    names = [attrs] if isinstance(attrs, str) else list(attrs or [])

    def gen():
        for transcript in db.features_of_type("transcript"):
            if transcript.start is None or transcript.end is None:
                # No position, so no transcription start site. Dropping it is
                # the only honest option; inventing 0 would place it at the
                # start of the chromosome.
                continue
            point = transcript.end if transcript.strand == "-" else transcript.start
            attributes = dict(transcript.attributes)
            if names:
                attributes[name_key] = [
                    attrs_sep.join(attributes.get(key, ["."])[0] for key in names)
                ]
            yield Feature(
                seqid=transcript.seqid,
                source=db.derived_source,
                featuretype=f"{transcript.featuretype}_TSS",
                start=point,
                end=point,
                score=transcript.score,
                strand=transcript.strand,
                attributes=attributes,
                dialect=db.dialect,
            )

    # `to_bedtool` already materializes, so no second `saveas()` here.
    result = to_bedtool(gen())

    # `bedtools merge` has no notion of GFF attributes, so merging implies BED6.
    if names or as_bed6 or merge_overlapping:
        field = name_key if names else "ID"
        result = result.each(featurefuncs.gff2bed, name_field=field).saveas()

    if merge_overlapping:
        kwargs = {"o": "distinct", "c": 4, "s": True}
        kwargs.update(merge_kwargs or {})
        result = result.sort().merge(**kwargs).saveas()

    return result.sort().saveas()
