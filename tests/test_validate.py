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
"""Post-ingest invariants.

A validator is only worth what it catches, so almost every test here breaks the
database on purpose and asserts the specific invariant fires. A check that
passes on healthy data and also passes on corrupted data is worse than no check
at all -- it converts an unknown into a false assurance.

INV-5 is the one that matters most. A fused feature whose envelope is narrower
than its segments simply stops being returned by `region()`: nothing raises, no
count looks wrong, and the feature is gone. The test for it therefore corrupts
an envelope and asserts both that INV-5 fires AND that the query really does
lose the feature -- otherwise the invariant is guarding nothing.

Calibration matters as much as coverage. Running this across the upstream
corpus is what set the severities: `edges_resolve` is a warning because
`mouse_extra_comma.gff3` has `Parent=a,` and gffutils writes that dangling
relation too, so erroring on it would be a divergence rather than a repair.
"""

from __future__ import annotations

import logging
from pathlib import Path

import pytest
from gffbase import create_db
from gffbase.validate import ERROR, WARNING, ValidationError, validate_db

UPSTREAM = Path(__file__).parent / "data" / "upstream"

SPLIT_CDS = """##gff-version 3
chr1\trs\tgene\t100\t900\t.\t+\t.\tID=g1
chr1\trs\tmRNA\t100\t900\t.\t+\t.\tID=t1;Parent=g1
chr1\trs\tCDS\t100\t200\t.\t+\t0\tID=cds1;Parent=t1
chr1\trs\tCDS\t800\t900\t.\t+\t2\tID=cds1;Parent=t1
"""


@pytest.fixture
def db(tmp_path):
    src = tmp_path / "in.gff3"
    src.write_text(SPLIT_CDS)
    return create_db(str(src), ":memory:", mode="strict")


def _names(report) -> set[str]:
    return {v.name for v in report.violations}


def _set_envelope(db, fid: str, start: int, end: int) -> None:
    """Write a wrong envelope the way a buggy fuse would: to `bbox` too.

    Changing only `start`/`end` leaves the R-tree answering from the old
    envelope, so the corruption shows up as the two query paths disagreeing --
    which is INV-8's job, not INV-5's. To exercise INV-5 the envelope has to be
    consistently wrong, which is exactly what a miscomputed MIN/MAX produces.
    """
    db.conn.execute('UPDATE features SET start = ?, "end" = ? WHERE id = ?', [start, end, fid])
    if db._rtree_built:
        db.conn.execute(
            'UPDATE features SET bbox = ST_MakeEnvelope(start, seqid_y, "end", seqid_y + 1) '
            "WHERE id = ?",
            [fid],
        )


# ---------------------------------------------------------------------------
# Healthy databases
# ---------------------------------------------------------------------------


def test_a_freshly_built_database_validates(db):
    report = validate_db(db, level="full")
    assert report.ok
    assert bool(report) is True
    assert report.violations == []
    assert len(report.checked) >= 13


def test_every_invariant_actually_ran(db):
    """A green report must not mean "nothing was looked at".

    INV-8 compares `bbox` against the coordinates it was built from, so it
    only exists to run when an R-tree was built. Under
    `GFFBASE_TEST_DISABLE_RTREE=1` there is no `bbox` column and `validate_db`
    records it as skipped -- which this test used to read as a failure, making
    the whole B-tree CI job red. Asserting it unconditionally was the bug; the
    skip is the designed behaviour, so assert *that* instead, and keep
    requiring it whenever the column is there.
    """
    report = validate_db(db, level="full")
    numbers = {c.split(" ", 1)[0] for c in report.checked}
    always = {f"INV-{i}" for i in (1, 2, 3, 4, 5, 6, 7, 9, 10, 11, 13, 14)}
    assert numbers >= always
    assert "INV-12" in numbers

    if db._rtree_built:
        assert "INV-8" in numbers
        assert report.skipped == []
    else:
        assert "INV-8" not in numbers
        assert [s.split(" ", 1)[0] for s in report.skipped] == ["INV-8"]
        assert "no R-tree" in report.skipped[0]


@pytest.mark.parametrize(
    "name",
    [
        "synthetic.gff3",
        "random-chr.gff",
        "ncbi_gff3.txt",
        "FBgn0031208.gff",
        "gencode-v19.gtf",
        "ensembl_gtf.txt",
        "c_elegans_WS199_ann_gff.txt",
        "unsanitized.gff",
        "mouse_extra_comma.gff3",
        "glimmer_nokeyval.gff3",
        "keep-order-test.gtf",
    ],
)
def test_the_upstream_corpus_validates_without_errors(name):
    """Calibration, not just coverage: an invariant that fires on a file
    gffutils reads is describing gffbase's expectations rather than the data's
    correctness, and has to be reconsidered or downgraded."""
    db = create_db(str(UPSTREAM / name), ":memory:", merge_strategy="create_unique")
    report = validate_db(db, level="full")
    assert report.errors == [], f"{name}: {[str(v) for v in report.errors]}"


def test_a_warning_does_not_make_the_report_fail():
    """`mouse_extra_comma.gff3` has `Parent=a,`, which yields an empty parent
    id. gffutils writes that relation too, so it is reported and tolerated."""
    db = create_db(str(UPSTREAM / "mouse_extra_comma.gff3"), ":memory:")
    report = validate_db(db)
    assert "edges_resolve" in _names(report)
    assert all(v.severity == WARNING for v in report.violations)
    assert report.ok is True
    report.raise_for_status()  # must not raise


# ---------------------------------------------------------------------------
# INV-5 -- the one that matters
# ---------------------------------------------------------------------------


def test_a_narrowed_envelope_is_caught(db):
    """And, crucially, that the corruption really does lose the feature -- an
    invariant guarding a harmless difference is not worth running."""
    assert "cds1" in {f.id for f in db.region(("chr1", 850, 860))}

    _set_envelope(db, "cds1", 100, 200)

    assert "cds1" not in {f.id for f in db.region(("chr1", 850, 860))}, (
        "the corruption must actually break a query, or INV-5 guards nothing"
    )
    report = validate_db(db)
    assert "envelope_exact" in _names(report)
    assert not report.ok


def test_a_widened_envelope_is_caught(db):
    _set_envelope(db, "cds1", 1, 900)
    assert "envelope_exact" in _names(validate_db(db))


def test_the_violation_names_the_feature_and_both_coordinate_pairs(db):
    """A report a user cannot act on is a report they will ignore."""
    _set_envelope(db, "cds1", 100, 200)
    (violation,) = [v for v in validate_db(db).violations if v.name == "envelope_exact"]
    assert violation.invariant == "INV-5"
    assert violation.severity == ERROR
    assert violation.count == 1
    assert violation.examples[0][0] == "cds1"
    assert "cds1" in str(violation)


# ---------------------------------------------------------------------------
# The remaining invariants, each broken on purpose
# ---------------------------------------------------------------------------


def test_inv2_orphan_segments(db):
    db.conn.execute("DELETE FROM features WHERE id = 'cds1'")
    assert "no_orphan_segments" in _names(validate_db(db))


def test_inv3_n_segments_disagreeing(db):
    db.conn.execute("UPDATE features SET n_segments = 5 WHERE id = 'cds1'")
    assert "n_segments_matches" in _names(validate_db(db))


def test_inv4_seg_idx_not_dense(db):
    db.conn.execute("UPDATE segments SET seg_idx = 7 WHERE seg_idx = 1")
    assert "seg_idx_dense" in _names(validate_db(db))


def test_inv6_dangling_edge(db):
    db.conn.execute("INSERT INTO edges VALUES ('ghost', 'cds1')")
    report = validate_db(db)
    assert "edges_resolve" in _names(report)
    assert report.ok is True, "a dangling parent is normal GFF3; it must not be an error"


def test_inv7_overlapping_segments_warn(db):
    db.conn.execute("UPDATE segments SET start = 150 WHERE seg_idx = 1")
    report = validate_db(db)
    assert "segments_do_not_overlap" in _names(report)
    assert [v.severity for v in report.violations if v.name == "segments_do_not_overlap"] == [
        WARNING
    ]


def test_inv8_stale_bbox(db):
    if not db._rtree_built:
        pytest.skip("spatial extension unavailable")
    db.conn.execute("UPDATE features SET start = 42 WHERE id = 'g1'")
    assert "bbox_matches" in _names(validate_db(db))


def test_inv8_accepts_a_reversed_coordinate_row(tmp_path):
    """`ST_MakeEnvelope` normalizes its corners, so a row with `end < start` --
    which compat mode accepts, and which `sanitize_gff_file` exists to repair --
    has a correctly-ordered bbox. Comparing against the raw columns flagged
    `unsanitized.gff` as corrupt when its bbox was right."""
    src = tmp_path / "rev.gff3"
    src.write_text("##gff-version 3\nchr1\trs\tgene\t1000\t500\t.\t+\t.\tID=backwards\n")
    db = create_db(str(src), ":memory:")
    assert "bbox_matches" not in _names(validate_db(db))


def test_inv9_missing_seqid_map_row(db):
    db.conn.execute("DELETE FROM seqid_map")
    if not db._rtree_built:
        pytest.skip("no seqid_map is written without the spatial extension")
    assert "seqid_map_complete" in _names(validate_db(db))


def test_inv10_attribute_dedup_inconsistent(db):
    """A segment marked as repeating segment 0's column 9 must own no attribute
    rows. Get it backwards and attributes silently vanish for the segments that
    really did carry different ones."""
    db.conn.execute("UPDATE segments SET attrs_same_as_seg0 = FALSE WHERE seg_idx = 1")
    assert "attribute_dedup_consistent" in _names(validate_db(db))


def test_inv11_closure_cycle(db):
    db.conn.execute("INSERT INTO closure VALUES ('g1', 'g1', 1)")
    assert "closure_sound" in _names(validate_db(db))


def test_inv11_depth_one_row_with_no_edge(db):
    db.conn.execute("INSERT INTO closure VALUES ('g1', 'cds1', 1)")
    assert "closure_sound" in _names(validate_db(db))


def test_inv13_conflict_pointing_at_a_missing_feature(db):
    db.conn.execute("INSERT INTO id_conflicts VALUES ('x', 'ghost', 'multipart', NULL, NULL)")
    assert "conflicts_resolve" in _names(validate_db(db))


def test_inv13_unknown_id_origin(db):
    db.conn.execute("UPDATE features SET id_origin = 'invented' WHERE id = 'g1'")
    assert "conflicts_resolve" in _names(validate_db(db))


def test_inv14_file_order_not_the_first_segments(db):
    db.conn.execute("UPDATE features SET file_order = 99 WHERE id = 'cds1'")
    assert "file_order_is_first_segment" in _names(validate_db(db))


def test_inv12_catches_attributes_that_no_longer_match_their_blob(db):
    """The check that would catch an escaping bug: a parser that mangled a
    value writes attribute rows the stored bytes no longer produce, and every
    other invariant here would still hold."""
    db.conn.execute("UPDATE attributes SET value = 'tampered' WHERE feature_id = 'g1'")
    assert "attributes_reparse" not in _names(validate_db(db, level="fast"))
    assert "attributes_reparse" in _names(validate_db(db, level="full"))


def test_inv12_tolerates_a_key_with_no_values(tmp_path):
    """`pseudo` / `ID=` yield a key with an EMPTY value list, which cannot be
    represented as a `(key, value)` row. That difference is the schema's, not a
    parser bug -- four upstream fixtures reported a violation for behaviour
    that is exactly right."""
    src = tmp_path / "empty.gff3"
    src.write_text("##gff-version 3\nchr1\trs\tgene\t1\t9\t.\t+\t.\tID=g1;pseudo=\n")
    db = create_db(str(src), ":memory:")
    assert db["g1"].attributes["pseudo"] == []
    assert db.conn.execute("SELECT COUNT(*) FROM attributes WHERE key = 'pseudo'").fetchone() == (
        0,
    )
    assert validate_db(db, level="full").ok


# ---------------------------------------------------------------------------
# Reporting and plumbing
# ---------------------------------------------------------------------------


def test_raise_for_status_raises_only_on_errors(db):
    validate_db(db).raise_for_status()
    _set_envelope(db, "cds1", 100, 200)
    with pytest.raises(ValidationError, match="INV-5"):
        validate_db(db).raise_for_status()


def test_raise_on_error_raises_inline(db):
    _set_envelope(db, "cds1", 100, 200)
    with pytest.raises(ValidationError):
        validate_db(db, raise_on_error=True)


def test_a_strict_ingest_validates_what_it_built(tmp_path, monkeypatch):
    """The point of running it automatically: a corrupt build must not be
    handed back to the caller as if it were fine."""
    import gffbase.ingest as ingest_mod

    original = ingest_mod.resolve_multipart

    def corrupting(con, options, autoinc, has_spatial):
        n = original(con, options, autoinc, has_spatial)
        con.execute('UPDATE features SET "end" = start WHERE n_segments > 1')
        return n

    monkeypatch.setattr(ingest_mod, "resolve_multipart", corrupting)
    src = tmp_path / "in.gff3"
    src.write_text(SPLIT_CDS)
    with pytest.raises(ValidationError, match="INV-5"):
        create_db(str(src), ":memory:", mode="strict")


def test_compat_mode_does_not_validate(tmp_path):
    """Compat exists to load whatever gffutils loads, and several invariants
    describe structure a tolerated file will not have."""
    db = create_db(str(UPSTREAM / "mouse_extra_comma.gff3"), ":memory:")
    assert db is not None  # loaded despite the dangling edges


def test_the_convenience_method_reaches_the_same_report(db):
    assert db.validate().ok is validate_db(db).ok
    assert db.validate(level="full").level == "full"


def test_an_unknown_level_is_rejected(db):
    with pytest.raises(ValueError, match="level must be"):
        validate_db(db, level="thorough")


def test_checks_needing_segments_are_skipped_not_silently_passed(tmp_path):
    """A v1 database has no `segments`. Reporting those checks as passing would
    turn "not looked at" into "verified"."""
    import duckdb

    src = tmp_path / "s.gff3"
    src.write_text("##gff-version 3\nchr1\trs\tgene\t1\t9\t.\t+\t.\tID=g1\n")
    path = tmp_path / "db.duckdb"
    create_db(str(src), str(path)).conn.close()

    con = duckdb.connect(str(path))
    con.execute("LOAD spatial")
    con.execute("DROP VIEW IF EXISTS features_compat")
    con.execute("DROP VIEW IF EXISTS segments_all")
    con.execute("DROP TABLE segments")
    report = validate_db(con)
    con.close()

    assert any("INV-5" in s for s in report.skipped)
    assert not any("INV-5" in c for c in report.checked)
    assert report.ok


def test_violations_are_logged_at_their_severity(db, caplog):
    _set_envelope(db, "cds1", 100, 200)
    with caplog.at_level(logging.WARNING, logger="gffbase.validate"):
        validate_db(db)
    assert any(r.levelname == "ERROR" and "INV-5" in r.getMessage() for r in caplog.records)


def test_the_report_reads_usefully(db):
    assert "invariants held" in str(validate_db(db))
    _set_envelope(db, "cds1", 100, 200)
    text = str(validate_db(db))
    assert "INV-5" in text and "envelope_exact" in text
