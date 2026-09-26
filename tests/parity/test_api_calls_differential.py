"""Differential comparison of FeatureDB *methods* under non-default arguments.

`test_differential.py` compares databases; this compares what the shared
methods return when called with the arguments people actually pass --
strand, featuretype lists, ordering, `limit` / `completely_within`, levels,
`merge_criteria`, `bed12`, `children_bp` -- on GFF3 and GTF fixtures.
"""

from __future__ import annotations

import logging
import warnings

import pytest

from . import differential as D

pytestmark = pytest.mark.parity

FIXTURES = [
    "intro_docs_example.gff",
    "gff_example1.gff3",
    "hybrid1.gff3",
    "FBgn0031208.gtf",
    "gencode-v19.gtf",
]


def _ids(features):
    return [f.id for f in features]


def _calls(db, lib) -> dict:
    """Name -> thunk. `lib` supplies the library's own merge_criteria module."""
    fts = sorted(db.featuretypes())
    seqs = sorted(db.seqids())
    feats = list(db.all_features())
    genes = [f for f in feats if f.featuretype == "gene"] or feats[:1]
    g = genes[0]
    leaf = ([f for f in feats if f.featuretype in ("exon", "CDS")] or feats[-1:])[0]
    s = min(f.start for f in feats if f.start)
    e = max(f.end for f in feats if f.end)
    mid = (s + e) // 2
    half = (g.seqid, g.start, (g.start + g.end) // 2)
    mc = lib.merge_criteria
    tx = "mRNA" if "mRNA" in fts else "transcript"
    return {
        "featuretypes": lambda: sorted(db.featuretypes()),
        "seqids": lambda: sorted(db.seqids()),
        "count_all": lambda: db.count_features_of_type(),
        "count_each": lambda: {ft: db.count_features_of_type(ft) for ft in fts},
        "all_strand_plus": lambda: sorted(_ids(db.all_features(strand="+"))),
        "all_order_start": lambda: [f.start for f in db.all_features(order_by="start")],
        "all_order_start_reverse": lambda: [
            f.start for f in db.all_features(order_by="start", reverse=True)
        ],
        "all_limit": lambda: sorted(_ids(db.all_features(limit=(seqs[0], s, mid)))),
        "all_limit_within": lambda: sorted(
            _ids(db.all_features(limit=(seqs[0], s, mid), completely_within=True))
        ),
        "features_of_type_list": lambda: sorted(_ids(db.features_of_type(fts[:2]))),
        "features_of_type_order_end": lambda: [
            f.end for f in db.features_of_type(fts[0], order_by="end")
        ],
        "children": lambda: sorted(_ids(db.children(g))),
        "children_level1": lambda: sorted(_ids(db.children(g, level=1))),
        "children_level2": lambda: sorted(_ids(db.children(g, level=2))),
        "children_exon": lambda: sorted(_ids(db.children(g, featuretype="exon"))),
        "children_order_start": lambda: [f.start for f in db.children(g, order_by="start")],
        "children_limit": lambda: sorted(_ids(db.children(g, limit=half))),
        "children_limit_within": lambda: sorted(
            _ids(db.children(g, limit=half, completely_within=True))
        ),
        "parents": lambda: sorted(_ids(db.parents(leaf))),
        "parents_level1": lambda: sorted(_ids(db.parents(leaf, level=1))),
        "parents_gene": lambda: sorted(_ids(db.parents(leaf, featuretype="gene"))),
        "region_string": lambda: sorted(_ids(db.region(f"{seqs[0]}:{s}-{mid}"))),
        "region_within": lambda: sorted(
            _ids(db.region(seqid=seqs[0], start=s, end=mid, completely_within=True))
        ),
        "region_minus": lambda: sorted(_ids(db.region(seqid=seqs[0], start=s, end=e, strand="-"))),
        "region_featuretypes": lambda: sorted(
            _ids(db.region(seqid=seqs[0], start=s, end=e, featuretype=["exon", "CDS"]))
        ),
        "region_of_feature": lambda: sorted(_ids(db.region(leaf))),
        "children_bp": lambda: db.children_bp(g),
        "children_bp_merge": lambda: db.children_bp(g, merge=True),
        "bed12": lambda: db.bed12(g),
        "iter_by_parent_childs": lambda: [
            sorted(f.id for f in group) for group in db.iter_by_parent_childs(featuretype="gene")
        ],
        "merge_exons": lambda: sorted(
            (m.seqid, m.start, m.end, m.strand) for m in db.merge(db.features_of_type("exon"))
        ),
        "merge_ignoring_strand": lambda: sorted(
            (m.seqid, m.start, m.end)
            for m in db.merge(
                db.features_of_type("exon"),
                merge_criteria=(mc.seqid, mc.overlap_end_inclusive),
            )
        ),
        "introns_per_parent": lambda: sorted(
            (i.start, i.end)
            for i in db.create_introns(parent_featuretype=tx, grandparent_featuretype=None)
        ),
        "interfeatures_of_exons": lambda: sorted(
            (i.start, i.end)
            for i in db.interfeatures(db.features_of_type("exon", order_by="start"))
        ),
    }


_ORACLE_SELF_ANCESTRY = (
    "Known: the oracle lists a GTF gene with an authored row among its own "
    "descendants (the self-ancestry quirk `test_database_signature.py` normalizes)."
)

KNOWN = {
    **{
        ("gencode-v19.gtf", call): _ORACLE_SELF_ANCESTRY
        for call in (
            "children",
            "children_level1",
            "children_level2",
            "children_order_start",
            "children_limit",
            "iter_by_parent_childs",
        )
    },
}


def _both(name):
    import gffbase
    import gffutils

    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        logging.disable(logging.WARNING)
        try:
            oracle = gffutils.create_db(D.fixture(name), ":memory:", merge_strategy="create_unique")
        finally:
            logging.disable(logging.NOTSET)
    ours = gffbase.create_db(D.fixture(name), ":memory:", merge_strategy="create_unique")
    return (oracle, gffutils), (ours, gffbase)


def _call_names() -> list[str]:
    import gffbase

    db = gffbase.create_db(D.fixture(FIXTURES[0]), ":memory:", merge_strategy="create_unique")
    return sorted(_calls(db, gffbase))


def _cases():
    for name in FIXTURES:
        for call in _call_names():
            reason = KNOWN.get((name, call))
            marks = [pytest.mark.xfail(strict=True, reason=reason)] if reason else []
            yield pytest.param(name, call, id=f"{name}-{call}", marks=marks)


def _run(thunk):
    try:
        return thunk()
    except Exception as exc:  # noqa: BLE001 - the exception type is the result
        return f"raised {type(exc).__name__}"


@pytest.mark.parametrize(("name", "call"), list(_cases()))
def test_a_method_call_agrees_with_the_oracle(name, call):
    D.requires_gffutils()
    (oracle, olib), (ours, glib) = _both(name)
    assert _run(_calls(ours, glib)[call]) == _run(_calls(oracle, olib)[call])


def test_iter_by_parent_childs_honours_level():
    """DEVIATION: the oracle's `iter_by_parent_childs` accepts `level` and never
    passes it on, so `level=1` returns every descendant. gffbase honours it."""
    import gffbase

    db = gffbase.create_db(D.fixture("intro_docs_example.gff"), ":memory:")
    (group,) = [g for g in db.iter_by_parent_childs(featuretype="gene", level=1)]
    assert {f.featuretype for f in group[1:]} == {"mRNA"}


def test_create_splice_sites_works_where_the_oracle_raises():
    """DEVIATION: the oracle's `create_splice_sites` raises KeyError on these
    fixtures; gffbase returns donor and acceptor sites."""
    import gffbase

    db = gffbase.create_db(D.fixture("intro_docs_example.gff"), ":memory:")
    sites = list(db.create_splice_sites())
    assert sites and all(s.end - s.start == 1 for s in sites)
