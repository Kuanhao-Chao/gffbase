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
"""Gene models drawn as matplotlib collections. Requires pybedtools + matplotlib.

Guarded import so the module resolves without either; `Gene` raises at
construction with an actionable message rather than failing at import.
"""

from __future__ import annotations

try:
    from pybedtools.contrib.plotting import Track

    _HAVE_TRACK = True
except ImportError:  # pragma: no cover - depends on the environment
    Track = None
    _HAVE_TRACK = False

__all__ = ["Gene", "Track"]


class Gene:
    """One gene as a stack of `Track`s: transcripts thin, UTRs thicker, CDS thickest.

    The featuretype names are parameters because the same biology is spelled
    differently by different sources -- GTF says `3'UTR` where FlyBase GFF says
    `three_prime_UTR` -- and a hardcoded list silently draws nothing for half
    the corpora in use.
    """

    def __init__(
        self,
        db,
        gene_id,
        transcripts=("mRNA",),
        utrs=("3'UTR", "5'UTR"),
        cds=("CDS",),
        ybase=0,
        **kwargs,
    ):
        if not _HAVE_TRACK:
            raise ImportError(
                "pybedtools (with matplotlib) is required for gffbase.contrib.plotting; "
                "install it with `pip install gffbase[pybedtools] matplotlib`"
            )
        from gffbase.helpers import asinterval

        self.db = db
        self.gene_id = gene_id
        self.ybase = ybase
        self.heights = {"transcript": 0.1, "utr": 0.3, "cds": 0.5}
        self.heights["full"] = self.heights["cds"] + 0.2

        groups = {
            "transcript": list(transcripts),
            "utr": list(utrs),
            "cds": list(cds),
        }
        self.tracks = []
        for kind, featuretypes in groups.items():
            children = [
                asinterval(f) for f in db.children(gene_id) if f.featuretype in featuretypes
            ]
            if not children:
                continue
            height = self.heights[kind]
            # Each track is centred on the same baseline so the thicker
            # features sit over the thinner ones rather than beside them.
            offset = ybase + (self.heights["full"] - height) / 2.0
            self.tracks.append(Track(children, ybase=offset, yheight=height, **kwargs))

        self.max_y = ybase + self.heights["full"]

    def add_to_ax(self, ax):
        """Draw onto a matplotlib axes."""
        for track in self.tracks:
            ax.add_collection(track)
