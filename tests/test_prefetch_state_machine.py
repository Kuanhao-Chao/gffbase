"""Automatic prefetch never changes an answer.

A relation call (or `db[id]`) on a feature a live stream holds is answered,
with its neighbours', by one query -- see `FeatureDB._from_window`. That is
only an optimisation if the answer is exactly the per-call path's, whatever
streams are open, however far they have advanced, and whatever was written
in between. This state machine interleaves all of it and checks every answer
against the per-call path.
"""

from __future__ import annotations

import pytest
from gffbase import create_db
from gffbase.interface import FeatureDB
from hypothesis import settings
from hypothesis import strategies as st
from hypothesis.stateful import RuleBasedStateMachine, invariant, precondition, rule

pytestmark = pytest.mark.property


def _gff(n_genes: int) -> str:
    lines = ["##gff-version 3"]
    pos = 1
    for g in range(n_genes):
        lines.append(f"chr1\tt\tgene\t{pos}\t{pos + 900}\t.\t+\t.\tID=g{g}")
        for t in range(2):
            tid = f"t{g}_{t}"
            lines.append(f"chr1\tt\tmRNA\t{pos}\t{pos + 900}\t.\t+\t.\tID={tid};Parent=g{g}")
            for e in range(3):
                s = pos + e * 300
                lines.append(f"chr1\tt\texon\t{s}\t{s + 99}\t.\t+\t.\tID=e{g}_{t}_{e};Parent={tid}")
                lines.append(f"chr1\tt\tCDS\t{s}\t{s + 99}\t.\t+\t0\tID=c{g}_{t}_{e};Parent={tid}")
        pos += 1000
    return "\n".join(lines) + "\n"


SHAPES = [
    ("children", 1, None),
    ("children", None, None),
    ("children", None, "exon"),
    ("children", 2, None),
    ("children", None, ["exon", "CDS"]),
    ("parents", 1, None),
    ("parents", None, "gene"),
    ("parents", None, None),
    ("getitem", None, None),
]


class Prefetch(RuleBasedStateMachine):
    def __init__(self):
        super().__init__()
        self.db = create_db(_gff(12), ":memory:", from_string=True)
        self.streams: list = []
        self.seen: list[str] = []
        self.serial = 0

    def _answer(self, shape, fid):
        method, level, featuretype = shape
        if method == "getitem":
            try:
                f = self.db[fid]
            except Exception as exc:  # noqa: BLE001 - the exception is the answer
                return type(exc).__name__
            return (f.id, f.seqid, f.start, f.end, dict(f.attributes))
        call = getattr(self.db, method)
        return [(f.id, f.start, f.end) for f in call(fid, level=level, featuretype=featuretype)]

    @rule(kind=st.sampled_from(["gene", "mRNA", "exon", "all", "children"]))
    def open_stream(self, kind):
        if kind == "all":
            it = self.db.all_features()
        elif kind == "children":
            it = self.db.children(self.seen[-1] if self.seen else "g0")
        else:
            it = self.db.features_of_type(kind)
        self.streams.append(it)

    @precondition(lambda self: self.streams)
    @rule(data=st.data(), k=st.integers(1, 40))
    def advance(self, data, k):
        it = data.draw(st.sampled_from(self.streams))
        for _ in range(k):
            try:
                self.seen.append(next(it).id)
            except StopIteration:
                self.streams.remove(it)
                return

    @precondition(lambda self: self.streams)
    @rule(data=st.data())
    def close_stream(self, data):
        it = data.draw(st.sampled_from(self.streams))
        it.close()
        self.streams.remove(it)

    @precondition(lambda self: self.seen)
    @rule(data=st.data(), shape=st.sampled_from(SHAPES))
    def query(self, data, shape):
        fid = data.draw(st.sampled_from(self.seen[-60:]))
        got = self._answer(shape, fid)
        FeatureDB._PREFETCH = False
        try:
            expected = self._answer(shape, fid)
        finally:
            FeatureDB._PREFETCH = True
        assert got == expected, (shape, fid)

    @rule(g=st.integers(0, 11))
    def add_transcript(self, g):
        self.serial += 1
        tid = f"new{self.serial}"
        self.db.update(
            f"chr1\tt\tmRNA\t{g * 1000 + 1}\t{g * 1000 + 50}\t.\t+\t.\tID={tid};Parent=g{g}\n"
            f"chr1\tt\texon\t{g * 1000 + 1}\t{g * 1000 + 20}\t.\t+\t.\tID={tid}e;Parent={tid}\n",
            from_string=True,
        )

    @precondition(lambda self: self.seen)
    @rule(data=st.data())
    def delete_one(self, data):
        fid = data.draw(st.sampled_from(self.seen))
        if fid in self.db:
            self.db.delete(fid)

    @rule(g=st.integers(0, 11), t=st.integers(0, 11))
    def relate(self, g, t):
        child = f"t{t}_0"
        if f"g{g}" in self.db and child in self.db:
            self.db.add_relation(f"g{g}", child, level=1)

    @invariant()
    def nothing_live_without_streams(self):
        if not self.streams:
            assert not self.db._live

    def teardown(self):
        for it in self.streams:
            it.close()
        assert not self.db._live
        self.db.close()


Prefetch.TestCase.settings = settings(
    max_examples=60, stateful_step_count=40, deadline=None, report_multiple_bugs=False
)
TestPrefetch = Prefetch.TestCase
