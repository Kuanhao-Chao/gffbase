"""`FeatureDB.update` adds features the way `create_db` would have.

0.2.0 appended rows directly, and lost what ingest derives from them:

* the `ID` attribute was ignored, so every new feature was renamed
  `<featuretype>_<n>` (the introns of `db.update(db.create_introns())`, the
  docstring's own example, became `intron_6` rather than `e1-e2`);
* no edges were written, so `children()` and `parents()` never saw a new row;
* a duplicate id was stored under a made-up name instead of being refused;
* GTF genes and transcripts were not inferred for new exons.

gffutils runs its ingest creator over the new data; these tests hold gffbase
to the same results, and to a database that still passes full validation.
"""

from __future__ import annotations

import pytest
from gffbase import create_db
from gffbase.exceptions import DuplicateIDError
from gffbase.feature import ParsedFeature, feature_from_line
from gffbase.validate import validate_db

GFF3 = (
    "##gff-version 3\n"
    "chr1\tt\tgene\t1\t1000\t.\t+\t.\tID=g1\n"
    "chr1\tt\tmRNA\t1\t1000\t.\t+\t.\tID=t1;Parent=g1\n"
    "chr1\tt\texon\t1\t100\t.\t+\t.\tID=e1;Parent=t1\n"
    "chr1\tt\texon\t300\t400\t.\t+\t.\tID=e2;Parent=t1\n"
    "chr1\tt\tCDS\t1\t100\t.\t+\t0\tParent=t1\n"
)
GTF = (
    'chr1\tt\texon\t1\t100\t.\t+\t.\tgene_id "G1"; transcript_id "T1";\n'
    'chr1\tt\texon\t300\t400\t.\t+\t.\tgene_id "G1"; transcript_id "T1";\n'
)


def line(text: str):
    return feature_from_line(text)


@pytest.fixture
def gff3_db():
    return create_db(GFF3, ":memory:", from_string=True)


@pytest.fixture
def gtf_db():
    return create_db(GTF, ":memory:", from_string=True)


def ids(db) -> list[str]:
    return sorted(f.id for f in db.all_features())


def kids(db, fid, level=1) -> list[str]:
    return sorted(f.id for f in db.children(fid, level=level))


def assert_valid(db):
    report = validate_db(db, level="full", sample=None)
    assert report.ok, report.errors


# ---------------------------------------------------------------------------
# GFF3
# ---------------------------------------------------------------------------


def test_new_rows_keep_their_id_and_join_the_hierarchy(gff3_db):
    gff3_db.update(
        [
            line("chr1\tt\tmRNA\t1\t900\t.\t+\t.\tID=t2;Parent=g1"),
            line("chr1\tt\texon\t1\t100\t.\t+\t.\tID=e3;Parent=t2"),
        ]
    )
    assert kids(gff3_db, "g1") == ["t1", "t2"]
    assert kids(gff3_db, "t2") == ["e3"]
    assert kids(gff3_db, "g1", level=None) == ["CDS_1", "e1", "e2", "e3", "t1", "t2"]
    assert sorted(p.id for p in gff3_db.parents("e3", level=None)) == ["g1", "t2"]
    assert_valid(gff3_db)


def test_an_id_less_row_continues_the_autoincrement(gff3_db):
    gff3_db.update([line("chr1\tt\tCDS\t300\t400\t.\t+\t0\tParent=t1")])
    assert "CDS_2" in gff3_db
    assert kids(gff3_db, "t1") == ["CDS_1", "CDS_2", "e1", "e2"]


def test_a_generated_name_skips_ids_the_file_already_used():
    db = create_db(
        GFF3 + "chr1\tt\tCDS\t500\t600\t.\t+\t0\tID=CDS_2\n", ":memory:", from_string=True
    )
    db.update([line("chr1\tt\tCDS\t300\t400\t.\t+\t0\tParent=t1")])
    new = [f.id for f in db.children("t1", featuretype="CDS") if f.start == 300]
    assert new and new[0] not in ("CDS_1", "CDS_2")


def test_a_duplicate_id_is_refused_and_nothing_is_written(gff3_db):
    before = ids(gff3_db)
    with pytest.raises(DuplicateIDError, match="Duplicate ID t1"):
        gff3_db.update(
            [
                line("chr1\tt\texon\t500\t600\t.\t+\t.\tID=e9;Parent=t1"),
                line("chr1\tt\tmRNA\t1\t900\t.\t+\t.\tID=t1;Parent=g1"),
            ]
        )
    assert ids(gff3_db) == before


def test_merge_strategy_warning_keeps_the_incumbent(gff3_db):
    gff3_db.update(
        [line("chr1\tt\tmRNA\t5\t900\t.\t+\t.\tID=t1;Parent=g1")], merge_strategy="warning"
    )
    assert gff3_db["t1"].start == 1


def test_merge_strategy_create_unique_renames_the_newcomer(gff3_db):
    gff3_db.update(
        [line("chr1\tt\tmRNA\t5\t900\t.\t+\t.\tID=t1;Parent=g1")], merge_strategy="create_unique"
    )
    assert gff3_db["t1"].start == 1
    assert gff3_db["t1_1"].start == 5
    assert kids(gff3_db, "g1") == ["t1", "t1_1"]


def test_merge_strategy_replace_swaps_the_row_and_its_parent_edge(gff3_db):
    gff3_db.update([line("chr1\tt\tgene\t1\t2000\t.\t+\t.\tID=g2")])
    gff3_db.update(
        [line("chr1\tt\tmRNA\t5\t900\t.\t+\t.\tID=t1;Parent=g2")], merge_strategy="replace"
    )
    assert gff3_db["t1"].start == 5
    assert kids(gff3_db, "g2") == ["t1"]
    assert kids(gff3_db, "g1") == []
    # The exons still hang off t1: edges naming it as a parent are its children's.
    assert kids(gff3_db, "t1", level=None) == ["CDS_1", "e1", "e2"]
    assert_valid(gff3_db)


def test_merge_strategy_merge_folds_attributes_into_the_incumbent(gff3_db):
    gff3_db.update(
        [line("chr1\tt\tmRNA\t1\t1000\t.\t+\t.\tID=t1;Parent=g1;Name=tx")], merge_strategy="merge"
    )
    assert gff3_db["t1"].attributes["Name"] == ["tx"]
    assert ids(gff3_db).count("t1") == 1
    assert_valid(gff3_db)


def test_the_docstring_example_update_with_create_introns(gff3_db):
    gff3_db.update(list(gff3_db.create_introns()))
    introns = list(gff3_db.features_of_type("intron"))
    assert [(i.id, i.start, i.end) for i in introns] == [("e1-e2", 101, 299)]
    assert [p.id for p in gff3_db.parents("e1-e2", level=1)] == ["t1"]
    assert_valid(gff3_db)


# ---------------------------------------------------------------------------
# GTF
# ---------------------------------------------------------------------------


def test_gtf_exons_join_existing_and_inferred_transcripts(gtf_db):
    gtf_db.update(
        [
            line('chr1\tt\texon\t500\t600\t.\t+\t.\tgene_id "G1"; transcript_id "T1";'),
            line('chr1\tt\texon\t700\t800\t.\t+\t.\tgene_id "G1"; transcript_id "T2";'),
        ]
    )
    assert kids(gtf_db, "G1") == ["T1", "T2"]
    assert len(kids(gtf_db, "T1")) == 3
    assert len(kids(gtf_db, "T2")) == 1
    # An inferred parent is the envelope of its children, so it grows to
    # cover the new ones. (gffutils leaves the old extent: a declared
    # deviation.)
    assert (gtf_db["T1"].start, gtf_db["T1"].end) == (1, 600)
    assert (gtf_db["T2"].start, gtf_db["T2"].end) == (700, 800)
    assert (gtf_db["G1"].start, gtf_db["G1"].end) == (1, 800)
    assert "G1" in [f.id for f in gtf_db.region("chr1:750-760")]
    assert_valid(gtf_db)


def test_gtf_without_inference_still_links_both_directions():
    db = create_db(GTF, ":memory:", from_string=True, disable_infer_transcripts=True)
    assert "T1" not in db
    db.update(
        [line('chr1\tt\ttranscript\t1\t400\t.\t+\t.\tgene_id "G1"; transcript_id "T1";')],
        disable_infer_transcripts=True,
    )
    # The new transcript adopts the exons that were already there.
    assert len(kids(db, "T1")) == 2
    assert kids(db, "G1") == ["T1"]


def test_gtf_introns(gtf_db):
    gtf_db.update(list(gtf_db.create_introns()))
    introns = list(gtf_db.features_of_type("intron"))
    assert [(i.id, i.start, i.end) for i in introns] == [("intron_1", 101, 299)]
    assert [p.id for p in gtf_db.parents("intron_1", level=1)] == ["T1"]


# ---------------------------------------------------------------------------
# Inputs
# ---------------------------------------------------------------------------


def test_a_path(gff3_db, tmp_path):
    path = tmp_path / "more.gff3"
    path.write_text("chr1\tt\texon\t500\t600\t.\t+\t.\tID=e5;Parent=t1\n")
    gff3_db.update(str(path))
    assert "e5" in kids(gff3_db, "t1")


def test_text_with_from_string(gff3_db):
    gff3_db.update("chr1\tt\texon\t500\t600\t.\t+\t.\tID=e5;Parent=t1\n", from_string=True)
    assert "e5" in kids(gff3_db, "t1")


def test_another_featuredb(gff3_db):
    other = create_db("chr2\tt\tgene\t1\t50\t.\t+\t.\tID=g7\n", ":memory:", from_string=True)
    gff3_db.update(other)
    assert "g7" in gff3_db


def test_a_parsed_feature(gff3_db):
    pf = ParsedFeature(
        seqid="chr1",
        source="t",
        featuretype="exon",
        start=500,
        end=600,
        score=".",
        strand="+",
        frame=".",
        attributes_blob=b"ID=e6;Parent=t1",
        attributes_pairs=[("ID", "e6", 0), ("Parent", "t1", 0)],
        extra=[],
    )
    gff3_db.update([pf])
    assert "e6" in kids(gff3_db, "t1")


def test_nothing_to_add_is_a_no_op(gff3_db):
    before = ids(gff3_db)
    assert gff3_db.update([]) is gff3_db
    assert gff3_db.update("", from_string=True) is gff3_db
    assert ids(gff3_db) == before


def test_an_unknown_keyword_is_refused(gff3_db):
    with pytest.raises(TypeError, match="no_such_option"):
        gff3_db.update([], no_such_option=True)


def test_a_transform_applies_to_new_rows(gff3_db):
    def tag(f):
        f.source = "tagged"
        return f

    gff3_db.update([line("chr1\tt\texon\t500\t600\t.\t+\t.\tID=e5;Parent=t1")], transform=tag)
    assert gff3_db["e5"].source == "tagged"


def test_a_new_seqid_is_found_by_region(gff3_db):
    gff3_db.update([line("chrNEW\tt\tgene\t10\t20\t.\t+\t.\tID=gn")])
    assert [f.id for f in gff3_db.region("chrNEW:1-100")] == ["gn"]
    # ...without disturbing the chromosome that was there first.
    assert "g1" in [f.id for f in gff3_db.region("chr1:1-50")]
    assert_valid(gff3_db)


def test_an_on_disk_update_survives_reopening(tmp_path):
    from gffbase import FeatureDB

    dbfn = str(tmp_path / "u.duckdb")
    create_db(GFF3, dbfn, from_string=True).close()
    with FeatureDB(dbfn) as db:
        db.update([line("chr1\tt\texon\t500\t600\t.\t+\t.\tID=e5;Parent=t1")])
    with FeatureDB(dbfn) as db:
        assert "e5" in kids(db, "t1")
        assert "e5" in kids(db, "g1", level=None)


# ---------------------------------------------------------------------------
# Against the oracle
# ---------------------------------------------------------------------------


SCENARIOS = {
    "gff3_children": (
        GFF3,
        [
            "chr1\tt\tmRNA\t1\t900\t.\t+\t.\tID=t2;Parent=g1",
            "chr1\tt\texon\t1\t100\t.\t+\t.\tID=e3;Parent=t2",
            "chr1\tt\tCDS\t1\t100\t.\t+\t0\tParent=t2",
        ],
    ),
    "gtf_exons": (
        GTF,
        [
            'chr1\tt\texon\t500\t600\t.\t+\t.\tgene_id "G1"; transcript_id "T1";',
            'chr1\tt\texon\t700\t800\t.\t+\t.\tgene_id "G1"; transcript_id "T2";',
        ],
    ),
}


def _snapshot(db):
    # An inferred parent's extent is a declared deviation (it grows to cover
    # new children; the oracle's does not), so inferred rows are compared by
    # identity and every authored row by its coordinates too.
    rows = sorted(
        (f.id, f.featuretype)
        + ((None, None) if f.source.endswith("_derived") else (f.start, f.end))
        for f in db.all_features()
    )
    rels = sorted((p.id, c.id) for c in db.all_features() for p in db.parents(c, level=1))
    return rows, rels


@pytest.mark.parity
@pytest.mark.parametrize("name", sorted(SCENARIOS))
def test_update_matches_gffutils(name):
    gffutils = pytest.importorskip("gffutils")
    import warnings

    from gffutils.feature import feature_from_line as oracle_line

    source, new = SCENARIOS[name]
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        oracle = gffutils.create_db(source, ":memory:", from_string=True)
        oracle.update([oracle_line(x) for x in new])
    ours = create_db(source, ":memory:", from_string=True)
    ours.update([line(x) for x in new])
    assert _snapshot(ours) == _snapshot(oracle)


# ---------------------------------------------------------------------------
# Strict databases and atomicity
# ---------------------------------------------------------------------------


def test_a_strict_database_accepts_children_of_existing_parents(caplog):
    db = create_db(GFF3, ":memory:", from_string=True, mode="strict")
    with caplog.at_level("WARNING", logger="gffbase"):
        db.update([line("chr1\tt\texon\t500\t600\t.\t+\t.\tID=e5;Parent=t1")])
    assert "e5" in kids(db, "t1")
    # The staged fragment is not validated on its own: its parent lives in
    # the target, which is where validation runs.
    assert "INV-6" not in caplog.text
    assert_valid(db)


def test_a_strict_database_fuses_a_split_feature_in_an_update():
    db = create_db(GFF3, ":memory:", from_string=True, mode="strict")
    db.update(
        [
            line("chr1\tt\tCDS\t300\t350\t.\t+\t0\tID=c9;Parent=t1"),
            line("chr1\tt\tCDS\t380\t400\t.\t+\t2\tID=c9;Parent=t1"),
        ]
    )
    assert (db["c9"].start, db["c9"].end) == (300, 400)
    assert_valid(db)


def test_a_failed_update_leaves_the_database_as_it_was(gff3_db, monkeypatch):
    from gffbase import _update

    before = (ids(gff3_db), kids(gff3_db, "g1", level=None))

    def fail(*args, **kwargs):
        raise RuntimeError("disk full")

    monkeypatch.setattr(_update, "_derive_edges", fail)
    with pytest.raises(RuntimeError, match="disk full"):
        gff3_db.update([line("chr1\tt\tmRNA\t1\t900\t.\t+\t.\tID=t2;Parent=g1")])
    assert (ids(gff3_db), kids(gff3_db, "g1", level=None)) == before
    assert_valid(gff3_db)
