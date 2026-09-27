"""Differential comparison with NON-default `create_db` options.

`test_differential.py` compares every shared fixture under the defaults. An
option gffbase accepted but ignored -- six of them in 0.2.0 -- is invisible
there. This builds each fixture under each option with both libraries and
requires that the option introduce no difference beyond the ones the fixture
already shows under the defaults (those are pinned, with reasons, in
`test_differential.py`).

When both libraries raise, the exceptions must agree in kind: gffbase's may be
a subclass of the oracle's (`DuplicateIDError` and `EmptyInputError` subclass
`ValueError`, which is what the oracle raises), so `except` clauses written
against gffutils keep working.
"""

from __future__ import annotations

import logging
import warnings

import pytest

from . import differential as D
from .test_differential import SHARED_GFF3, SHARED_GTF

pytestmark = pytest.mark.parity


def _rename(f):
    f.attributes["Note"] = ["x"]
    return f


def _drop_exons(f):
    return None if f.featuretype == "exon" else f


def _spec_callable(f):
    value = f.attributes.get("ID") or f.attributes.get("gene_id")
    return ("cb_" + value[0]) if value else None


GFF3_OPTIONS = {
    "id_spec=Name": {"id_spec": "Name"},
    "id_spec=[Name,ID]": {"id_spec": ["Name", "ID"]},
    "id_spec=dict": {"id_spec": {"gene": "Name"}},
    "id_spec=callable": {"id_spec": _spec_callable},
    "merge=create_unique": {"merge_strategy": "create_unique"},
    "merge=merge": {"merge_strategy": "merge"},
    "merge=replace": {"merge_strategy": "replace"},
    "merge=warning": {"merge_strategy": "warning"},
    "transform=rename": {"transform": _rename},
    "transform=drop_exons": {"transform": _drop_exons},
    "checklines=0": {"checklines": 0},
    "checklines=100": {"checklines": 100},
    "force_dialect_check": {"force_dialect_check": True},
    "keep_order": {"keep_order": True},
    "sort_attribute_values": {"sort_attribute_values": True},
}

GTF_OPTIONS = {
    "disable_infer_genes": {"disable_infer_genes": True},
    "disable_infer_transcripts": {"disable_infer_transcripts": True},
    "disable_both": {"disable_infer_genes": True, "disable_infer_transcripts": True},
    "merge=create_unique": {"merge_strategy": "create_unique"},
    "id_spec=callable": {"id_spec": _spec_callable},
    "transform=drop_exons": {"transform": _drop_exons},
    "force_gff": {"force_gff": True},
    "checklines=0": {"checklines": 0},
}

_ORACLE_CRASHES_ON_NULL_COORDINATES = (
    "INTENTIONAL: with a transform, the oracle's iterator tests `if feature:`, which "
    "calls `Feature.__len__`, which subtracts None coordinates -- TypeError on "
    "c_elegans_WS199_ann_gff.txt, 'len() should return >= 0' on unsanitized.gff. "
    "gffbase's adapter is always truthy, so the file loads."
)
_ORACLE_FORCE_DIALECT_CHECK = (
    "INTENTIONAL: the oracle's force_dialect_check leaves the iterator dialect None "
    "and create_db indexes it, raising TypeError on every file. gffbase checks every "
    "line and loads."
)
_KEY_WHITESPACE_UNDER_ID_SPEC = (
    "The fixture's baseline key-whitespace deviation (_ATTR_KEY_WHITESPACE in "
    "test_differential.py), seen again under renamed ids so the baseline "
    "subtraction cannot cancel it."
)
_GTF_NO_TRANSCRIPTS_NO_LEVEL2 = (
    "DEVIATION: with transcript inference disabled and no authored transcripts, the "
    "oracle still records gene -> child relations at level 2 from `gene_id`. gffbase "
    "stores direct relations only and links a child to a gene through its "
    "transcript, so without one the child is not linked to the gene."
)
_CDS_ONLY_INFERENCE = (
    "DEVIATION: with every exon dropped the file is CDS-only. The oracle infers "
    "parents from exons alone, so it infers none and every CDS is an orphan; "
    "gffbase infers the transcripts (and genes) from the CDS rows instead."
)
_CALLABLE_COLLIDES_WITH_INFERRED = (
    "DEVIATION: the callable names the inferred transcript after its own exon. The "
    "oracle merges the inferred feature away; gffbase keeps it under a generated id "
    "(`transcript_1`), so the transcript level survives."
)

KNOWN = {
    ("transform=rename", "c_elegans_WS199_ann_gff.txt"): _ORACLE_CRASHES_ON_NULL_COORDINATES,
    ("transform=rename", "unsanitized.gff"): _ORACLE_CRASHES_ON_NULL_COORDINATES,
    ("transform=drop_exons", "c_elegans_WS199_ann_gff.txt"): _ORACLE_CRASHES_ON_NULL_COORDINATES,
    ("transform=drop_exons", "unsanitized.gff"): _ORACLE_CRASHES_ON_NULL_COORDINATES,
    **{("force_dialect_check", name): _ORACLE_FORCE_DIALECT_CHECK for name in SHARED_GFF3},
    ("id_spec=dict", "FBgn0031208.gff"): _KEY_WHITESPACE_UNDER_ID_SPEC,
    ("id_spec=callable", "FBgn0031208.gff"): _KEY_WHITESPACE_UNDER_ID_SPEC,
    ("checklines=0", "gms2_example.gff3"): (
        "INTENTIONAL: sampling one record picks `;` as the separator, so the oracle "
        "reads the CDS keys as ' Parent' and loses the edges; gffbase strips key "
        "whitespace (_ATTR_KEY_WHITESPACE) and keeps them."
    ),
    **{
        ("disable_infer_transcripts", name): _GTF_NO_TRANSCRIPTS_NO_LEVEL2
        for name in (
            "FBgn0031208.gtf",
            "ensembl_gtf.txt",
            "issue174.gtf",
            "keep-order-test.gtf",
            "sharr.gtf",
        )
    },
    ("id_spec=callable", "keep-order-test.gtf"): _CALLABLE_COLLIDES_WITH_INFERRED,
    **{
        ("transform=drop_exons", name): _CDS_ONLY_INFERENCE
        for name in ("FBgn0031208.gtf", "ensembl_gtf.txt", "sharr.gtf")
    },
}


def _build(name: str, kwargs: dict):
    import gffbase
    import gffutils

    out = []
    for lib in (gffutils, gffbase):
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            logging.disable(logging.WARNING)
            try:
                out.append((lib.create_db(D.fixture(name), ":memory:", **kwargs), None))
            except Exception as exc:  # noqa: BLE001 - compared below
                out.append((None, exc))
            finally:
                logging.disable(logging.NOTSET)
    return out


def _differences(oracle, ours) -> set:
    found: set = set()
    theirs, mine = D.features_by_id(oracle), D.features_by_id(ours)
    for fid in set(theirs) | set(mine):
        if fid not in theirs or fid not in mine:
            found.add((fid, "missing" if fid in theirs else "extra"))
            continue
        for field, value in theirs[fid].items():
            if mine[fid].get(field) != value:
                found.add((fid, field))
    for rel in D.relations(oracle) ^ D.relations(ours):
        found.add((rel, "relation"))
    return found


_BASELINE: dict[str, set] = {}


def _baseline(name: str) -> set:
    if name not in _BASELINE:
        (oracle, oexc), (ours, gexc) = _build(name, {})
        _BASELINE[name] = set() if (oexc or gexc) else _differences(oracle, ours)
    return _BASELINE[name]


def _cases():
    for options, names in ((GFF3_OPTIONS, SHARED_GFF3), (GTF_OPTIONS, SHARED_GTF)):
        for option in options:
            for name in names:
                reason = KNOWN.get((option, name))
                marks = [pytest.mark.xfail(strict=True, reason=reason)] if reason else []
                yield pytest.param(options[option], name, id=f"{option}-{name}", marks=marks)


@pytest.mark.parametrize(("kwargs", "name"), list(_cases()))
def test_an_option_adds_no_difference(kwargs, name):
    D.requires_gffutils()
    (oracle, oexc), (ours, gexc) = _build(name, kwargs)
    if oexc or gexc:
        assert oexc is not None and gexc is not None, (
            f"only {'the oracle' if oexc else 'gffbase'} raised: {oexc or gexc!r}"
        )
        assert isinstance(gexc, type(oexc)), f"{type(gexc).__name__} vs {type(oexc).__name__}"
        return
    new = _differences(oracle, ours) - _baseline(name)
    assert not new, sorted(map(str, new))[:10]


def test_every_known_case_names_a_real_combination():
    real = {
        (o, n)
        for opts, names in ((GFF3_OPTIONS, SHARED_GFF3), (GTF_OPTIONS, SHARED_GTF))
        for o in opts
        for n in names
    }
    assert set(KNOWN) <= real, sorted(set(KNOWN) - real)
