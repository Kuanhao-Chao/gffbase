"""`DataIterator` reads what `create_db` and gffutils read.

In 0.2.0 it parsed with the strict NCBI profile, so it raised
`GFFFormatError` on files that `create_db` (compat, the default) loads and
`gffutils.DataIterator` reads -- a quoted value, a non-numeric score. Its
`transform` also kept a feature when the callable returned `None`, against
gffutils' documented contract ("a value that evaluates to False ... will be
skipped") and against `create_db(transform=...)`, so a filter ported from
gffutils filtered nothing. A long run of dropped features recursed once per
feature.
"""

from __future__ import annotations

import pytest
from gffbase import DataIterator, Feature, create_db

NOISY = (
    "##gff-version 3\n"
    'chr1\tt\tgene\t1\t100\t.\t+\t.\tID="g1"\n'
    "chr1\tt\tgene\t200\t300\tnot_a_number\t+\t.\tID=g2\n"
    "chr1\tt\tgene\tX\t300\t.\t+\t.\tID=unparseable\n"
    "chr1\tt\tgene\t400\t500\t.\t+\t.\tID=g3\n"
)


def test_it_reads_what_create_db_reads():
    it = DataIterator(NOISY, from_string=True)
    ids = [f.attributes["ID"][0] for f in it]
    db = create_db(NOISY, ":memory:", from_string=True)
    assert ids == [f.id for f in db.all_features()] == ["g1", "g2", "g3"]


def test_problems_are_reported_not_raised():
    it = DataIterator(NOISY, from_string=True)
    list(it)
    lines = sorted(w["line_no"] for w in it.warnings)
    assert 4 in lines  # the unparseable start
    assert all({"line_no", "kind", "message"} <= set(w) for w in it.warnings)


def test_a_file_path_behaves_the_same(tmp_path):
    path = tmp_path / "noisy.gff3"
    path.write_text(NOISY)
    assert [f.attributes["ID"][0] for f in DataIterator(str(path))] == ["g1", "g2", "g3"]


@pytest.mark.parametrize(
    ("transform", "expected"),
    [
        (lambda f: None, []),
        (lambda f: False, []),
        (lambda f: True, ["g1", "g2", "g3"]),
        (lambda f: f if f.attributes["ID"] != ["g2"] else None, ["g1", "g3"]),
    ],
    ids=["none_drops", "false_drops", "true_keeps", "filter"],
)
def test_transform_follows_the_gffutils_contract(transform, expected):
    it = DataIterator(NOISY, from_string=True, transform=transform)
    assert [f.attributes["ID"][0] for f in it] == expected


def test_in_memory_features_follow_the_same_contract():
    feats = list(DataIterator(NOISY, from_string=True))
    kept = DataIterator(feats, transform=lambda f: f if f.attributes["ID"] != ["g2"] else None)
    assert [f.attributes["ID"][0] for f in kept] == ["g1", "g3"]


def test_a_feature_without_coordinates_is_not_mistaken_for_a_drop():
    """A Feature with no coordinates has length 0, so is falsy."""
    nocoord = Feature(
        seqid="chr1", featuretype="gene", start=".", end=".", attributes={"ID": ["n"]}
    )
    assert not nocoord
    it = DataIterator([nocoord], transform=lambda f: f)
    assert list(it) == [nocoord]


def test_dropping_many_features_in_a_row_does_not_recurse():
    text = "".join(f"chr1\tt\texon\t{i}\t{i + 1}\t.\t+\t.\tID=e{i}\n" for i in range(1, 5000))
    it = DataIterator(
        text + "chr1\tt\tgene\t1\t9\t.\t+\t.\tID=last\n",
        from_string=True,
        transform=lambda f: f if f.featuretype == "gene" else None,
    )
    assert [f.attributes["ID"][0] for f in it] == ["last"]


@pytest.mark.parity
def test_matches_gffutils_on_quotes_and_transform():
    gffutils = pytest.importorskip("gffutils")
    text = 'chr1\tt\tgene\t1\t100\t.\t+\t.\tID="g1"\nchr1\tt\tgene\t200\t300\t.\t+\t.\tID=g2\n'

    def keep_g1(f):
        return f if f.attributes["ID"] == ["g1"] else None

    oracle = [
        dict(f.attributes) for f in gffutils.DataIterator(text, from_string=True, transform=keep_g1)
    ]
    ours = [dict(f.attributes) for f in DataIterator(text, from_string=True, transform=keep_g1)]
    assert ours == oracle == [{"ID": ["g1"]}]
