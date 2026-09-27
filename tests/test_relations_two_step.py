"""`children()` / `parents()` answer exactly what the closure join answered.

The join (`closure c JOIN features f ON f.id = c.descendant WHERE
c.ancestor = ? AND c.depth = ?`) scanned the whole closure on every call:
DuckDB never scans a multi-column index, and drops a single-column one as
soon as another predicate joins the filter. The replacement looks the ids up
in one table (`edges` for level 1, the closure otherwise), then fetches those
features by primary key. The join is kept here as the oracle.
"""

from __future__ import annotations

import os

import pytest
from gffbase import create_db

DATA = os.environ.get("GFFBASE_GFFUTILS_DATA", "/ccb/salz3/kh.chao/gffutils/gffutils/test/data")
FIXTURES = sorted(
    f
    for f in (os.listdir(DATA) if os.path.isdir(DATA) else [])
    if f.endswith((".gff", ".gff3", ".gtf", ".txt")) and "fasta" not in f
)


def _oracle(db, anchor, level, featuretype, order_by, reverse, limit, within, direction):
    join_col, anchor_col = (
        ("c.descendant", "c.ancestor")
        if direction == "children"
        else ("c.ancestor", "c.descendant")
    )
    where, params = [f"{anchor_col} = ?"], [anchor]
    if level is not None:
        where.append("c.depth = ?")
        params.append(level)
    w, p = db._featuretype_filter(featuretype)
    where += w
    params += p
    w, p = db._limit_filter(limit, within)
    where += w
    params += p
    sql = (
        f"SELECT {db._select_feature_aliased('f')} FROM closure c "
        f"JOIN features f ON f.id = {join_col} WHERE {' AND '.join(where)} "
        f"ORDER BY {db._order_clause_qualified(order_by, reverse, 'f')}"
    )
    return [row[0] for row in db.conn.execute(sql, params).fetchall()]


def _anchors(db):
    feats = list(db.all_features())
    parents = [f.id for f in feats if f.featuretype in ("gene", "mRNA", "transcript")]
    leaves = [f.id for f in feats if f.featuretype in ("exon", "CDS")]
    return (parents[:4] + leaves[:4]) or [f.id for f in feats[:4]]


def _db(fixture):
    try:
        return create_db(os.path.join(DATA, fixture), ":memory:", merge_strategy="create_unique")
    except Exception as exc:  # noqa: BLE001 - a fixture gffbase refuses is not this test
        pytest.skip(f"{fixture}: {type(exc).__name__}")


ARGS = [
    {},
    {"level": 1},
    {"level": 2},
    {"featuretype": "exon"},
    {"featuretype": ["exon", "CDS"], "level": 1},
    {"order_by": "start", "reverse": True},
    {"order_by": ["featuretype", "end"]},
]


@pytest.mark.parametrize("fixture", FIXTURES)
def test_relations_match_the_closure_join(fixture):
    db = _db(fixture)
    if db._closure_max_depth == 0:
        pytest.skip("no hierarchy")
    for anchor in _anchors(db):
        seqid = db[anchor].seqid
        for direction, method in (("children", db.children), ("parents", db.parents)):
            for kwargs in ARGS + [
                {"limit": (seqid, 1, 10**9)},
                {"limit": (seqid, db[anchor].start or 1, db[anchor].start or 1)},
                {"limit": (seqid, 1, 10**9), "completely_within": True},
            ]:
                got = [f.id for f in method(anchor, **kwargs)]
                expected = _oracle(
                    db,
                    anchor,
                    kwargs.get("level"),
                    kwargs.get("featuretype"),
                    kwargs.get("order_by"),
                    kwargs.get("reverse", False),
                    kwargs.get("limit"),
                    kwargs.get("completely_within", False),
                    direction,
                )
                assert got == expected, f"{fixture} {direction}({anchor!r}, {kwargs})"


def test_a_long_id_list_is_still_one_answer(monkeypatch):
    """Past `_ID_GROUP` ids a prefetch reads in groups; the answer is whole
    and in order."""
    from gffbase.interface import FeatureDB

    text = "chr1\tt\tgene\t1\t10000\t.\t+\t.\tID=g\n" + "".join(
        f"chr1\tt\t{'exon' if i % 2 else 'CDS'}\t{i + 1}\t{i + 1}\t.\t+\t.\tID=e{i};Parent=g\n"
        for i in range(60)
    )
    db = create_db(text, ":memory:", from_string=True)
    expected = [row[0] for row in db._rows_by_ids([f"e{i}" for i in range(60)])]
    monkeypatch.setattr(FeatureDB, "_ID_GROUP", 7)
    assert [row[0] for row in db._rows_by_ids([f"e{i}" for i in range(60)])] == expected
    exons = [row[0] for row in db._rows_by_ids([f"e{i}" for i in range(60)], "exon")]
    assert exons == [f"e{i}" for i in range(1, 60, 2)]
