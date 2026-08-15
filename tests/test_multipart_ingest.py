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
"""Fusing several input lines sharing one `ID` into one discontinuous feature.

Two rules govern everything here.

**Only strict mode fuses.** gffutils' `merge_strategy="merge"` requires all
eight non-attribute columns to match, so it never merges a genuine split
feature -- it routes them to `create_unique`. Fusing is therefore NEW
behaviour, not gffutils behaviour, and compat mode must not do it. A first
draft ran the resolve pass unconditionally, which fused the very rows
`create_unique` had just been asked to keep apart, because `dup`, `dup_1` and
`dup_2` all still share `raw_id = 'dup'`. The compat tests below are the ones
that caught it.

**The predicate is what makes fusing safe.** GFF3 requires the segments of a
discontinuous feature to share seqid, source, featuretype and strand.
`ncbi_gff3.txt` is the negative case and a real one: it shares an id between a
CDS, a start_codon and a stop_codon, and another between genes on opposite
strands. Fusing those would invent features that do not exist.
"""

from __future__ import annotations

import os
import re
from pathlib import Path

import pytest
from gffbase import MultipartConstraintError, create_db
from gffbase.ingest import _SURROGATE_SEP

UPSTREAM = Path(__file__).parent / "data" / "upstream"

# An NCBI-style split CDS: two lines, one ID, phase continuing across the join.
SPLIT_CDS = """##gff-version 3
chr1\trs\tgene\t100\t900\t.\t+\t.\tID=g1
chr1\trs\tmRNA\t100\t900\t.\t+\t.\tID=t1;Parent=g1
chr1\trs\tCDS\t100\t200\t.\t+\t0\tID=cds1;Parent=t1
chr1\trs\tCDS\t800\t900\t.\t+\t2\tID=cds1;Parent=t1
"""


def _write(tmp_path, text, name="in.gff3"):
    p = tmp_path / name
    p.write_text(text)
    return str(p)


@pytest.fixture
def fused(tmp_path):
    return create_db(_write(tmp_path, SPLIT_CDS), ":memory:", mode="strict")


def _rows(db, sql, params=None):
    return db.conn.execute(sql, params or []).fetchall()


# ---------------------------------------------------------------------------
# Compat mode must be untouched
# ---------------------------------------------------------------------------


def test_compat_still_rejects_a_duplicate_id(tmp_path):
    """`merge_strategy="error"` is the default and must stay an error: a
    discontinuous feature is not something gffutils can represent."""
    with pytest.raises(Exception, match="Duplicate ID"):
        create_db(_write(tmp_path, SPLIT_CDS), ":memory:")


def test_compat_create_unique_keeps_the_lines_apart(tmp_path):
    """The regression that motivated the mode guard. All three rows still share
    a `raw_id`, which is exactly what the resolve pass groups on -- so running
    it in compat mode silently undid `create_unique`."""
    db = create_db(_write(tmp_path, SPLIT_CDS), ":memory:", merge_strategy="create_unique")
    ids = [r[0] for r in _rows(db, "SELECT id FROM features ORDER BY file_order")]
    assert ids == ["g1", "t1", "cds1", "cds1_1"]
    assert _rows(db, "SELECT COUNT(*) FROM segments")[0][0] == 0
    assert all(r[0] == 1 for r in _rows(db, "SELECT n_segments FROM features"))


def test_compat_records_no_multipart_features(tmp_path):
    db = create_db(_write(tmp_path, SPLIT_CDS), ":memory:", merge_strategy="create_unique")
    meta = dict(_rows(db, "SELECT key, value FROM meta"))
    assert meta["n_multipart"] == "0"


# ---------------------------------------------------------------------------
# Strict mode fuses
# ---------------------------------------------------------------------------


def test_the_two_lines_become_one_feature(fused):
    ids = [r[0] for r in _rows(fused, "SELECT id FROM features ORDER BY file_order")]
    assert ids == ["g1", "t1", "cds1"]


def test_the_survivor_carries_the_envelope(fused):
    """`features.start/end` widen to MIN/MAX over the segments, which is what
    keeps the existing R-tree and every v1 query shape correct."""
    assert _rows(fused, "SELECT start, \"end\", n_segments FROM features WHERE id = 'cds1'") == [
        (100, 900, 2)
    ]


def test_each_line_becomes_a_segment_with_its_own_phase(fused):
    """Per-segment CDS phase is the reason the table exists: there is nowhere
    in `features` to put more than one phase."""
    assert _rows(
        fused,
        'SELECT seg_idx, start, "end", frame FROM segments '
        "WHERE feature_id = 'cds1' ORDER BY seg_idx",
    ) == [(0, 100, 200, "0"), (1, 800, 900, "2")]


def test_segment_zero_is_stored_even_though_features_carries_its_blob(fused):
    """Its own coordinates need somewhere to live: the feature row now holds
    the envelope, not segment 0's span."""
    assert _rows(fused, "SELECT COUNT(*) FROM segments WHERE feature_id='cds1' AND seg_idx=0") == [
        (1,)
    ]


def test_the_ingest_reports_how_many_it_fused(tmp_path):
    from gffbase import ingest
    from gffbase._options import IngestOptions

    _con, stats = ingest.from_file(
        _write(tmp_path, SPLIT_CDS), options=IngestOptions(mode="strict")
    )
    assert stats.n_multipart == 1


def test_meta_records_the_count_so_queries_can_gate_on_it(fused):
    assert dict(_rows(fused, "SELECT key, value FROM meta"))["n_multipart"] == "1"


# ---------------------------------------------------------------------------
# Hierarchy: one edge, not one per line
# ---------------------------------------------------------------------------


def test_a_discontinuous_feature_has_one_parent_edge_not_two(fused):
    """Both CDS lines carry `Parent=t1`. Without DISTINCT this produced two
    identical edges and therefore duplicate closure rows -- which was already a
    latent defect for `Parent=a,a` on a single line."""
    assert _rows(fused, "SELECT parent, child FROM edges ORDER BY 1, 2") == [
        ("g1", "t1"),
        ("t1", "cds1"),
    ]


def test_the_closure_has_no_duplicate_rows(fused):
    total, distinct = _rows(
        fused,
        "SELECT COUNT(*), COUNT(DISTINCT (ancestor, descendant, depth)) FROM closure",
    )[0]
    assert total == distinct


def test_children_returns_the_fused_feature_once(fused):
    assert [f.id for f in fused.children("t1")] == ["cds1"]


# ---------------------------------------------------------------------------
# Attributes
# ---------------------------------------------------------------------------


def test_redundant_per_segment_attribute_rows_are_dropped(fused):
    """NCBI repeats identical attributes on every CDS line. Storing them per
    segment would grow the table for no information, so rows are kept only
    where a segment's column 9 differs from segment 0's."""
    # Two lines, identical column 9 -- so exactly one set of rows survives,
    # all of them owned by segment 0.
    assert _rows(
        fused, "SELECT key, value, seg_idx FROM attributes WHERE feature_id='cds1' ORDER BY key"
    ) == [("ID", "cds1", 0), ("Parent", "t1", 0)]
    assert _rows(
        fused,
        "SELECT seg_idx, attrs_same_as_seg0 FROM segments WHERE feature_id='cds1' ORDER BY seg_idx",
    ) == [(0, True), (1, True)]


def test_a_segment_with_different_attributes_keeps_its_own_rows(tmp_path):
    """Where segments disagree, the logical feature takes the UNION -- legal
    GFF3, and forcing equality would fragment real files over vendor typos."""
    src = SPLIT_CDS.replace(
        "chr1\trs\tCDS\t800\t900\t.\t+\t2\tID=cds1;Parent=t1",
        "chr1\trs\tCDS\t800\t900\t.\t+\t2\tID=cds1;Parent=t1;Note=second_half",
    )
    db = create_db(_write(tmp_path, src), ":memory:", mode="strict")
    rows = _rows(
        db,
        "SELECT key, value, seg_idx FROM attributes WHERE feature_id='cds1' ORDER BY seg_idx, key",
    )
    assert ("Note", "second_half", 1) in rows
    assert _rows(
        db, "SELECT seg_idx, attrs_same_as_seg0 FROM segments WHERE feature_id='cds1'"
    ) == [
        (0, True),
        (1, False),
    ]


def test_a_parent_union_still_yields_one_edge_per_distinct_parent(tmp_path):
    src = SPLIT_CDS.replace(
        "chr1\trs\tCDS\t800\t900\t.\t+\t2\tID=cds1;Parent=t1",
        "chr1\trs\tCDS\t800\t900\t.\t+\t2\tID=cds1;Parent=t1,g1",
    )
    db = create_db(_write(tmp_path, src), ":memory:", mode="strict")
    assert sorted(_rows(db, "SELECT parent, child FROM edges WHERE child='cds1'")) == [
        ("g1", "cds1"),
        ("t1", "cds1"),
    ]


# ---------------------------------------------------------------------------
# The predicate rejects what it must
# ---------------------------------------------------------------------------


def test_differing_featuretype_is_refused(tmp_path):
    src = (
        "##gff-version 3\n"
        "chr1\trs\tCDS\t100\t200\t.\t+\t0\tID=x\n"
        "chr1\trs\tstart_codon\t800\t900\t.\t+\t0\tID=x\n"
    )
    with pytest.raises(MultipartConstraintError, match="featuretype differs"):
        create_db(_write(tmp_path, src), ":memory:", mode="strict")


def test_differing_strand_is_refused(tmp_path):
    src = (
        "##gff-version 3\n"
        "chr1\trs\tgene\t100\t200\t.\t+\t.\tID=x\n"
        "chr1\trs\tgene\t800\t900\t.\t-\t.\tID=x\n"
    )
    with pytest.raises(MultipartConstraintError, match="strand differs"):
        create_db(_write(tmp_path, src), ":memory:", mode="strict")


def test_differing_seqid_is_refused(tmp_path):
    src = (
        "##gff-version 3\n"
        "chr1\trs\tgene\t100\t200\t.\t+\t.\tID=x\n"
        "chr2\trs\tgene\t800\t900\t.\t+\t.\tID=x\n"
    )
    with pytest.raises(MultipartConstraintError, match="seqid differs"):
        create_db(_write(tmp_path, src), ":memory:", mode="strict")


def test_the_error_names_the_column_and_both_line_numbers(tmp_path):
    """A `MultipartConstraintError` is something the user has to act on -- fix
    the file, or pass `split` -- so "these lines conflict" is not enough."""
    src = (
        "##gff-version 3\n"
        "chr1\trs\tgene\t1\t9\t.\t+\t.\tID=other\n"
        "chr1\trs\tCDS\t100\t200\t.\t+\t0\tID=x\n"
        "chr1\trs\tstop_codon\t800\t900\t.\t+\t0\tID=x\n"
    )
    with pytest.raises(MultipartConstraintError) as exc:
        create_db(_write(tmp_path, src), ":memory:", mode="strict")
    msg = str(exc.value)
    assert "'x'" in msg
    assert "'CDS' on line 2" in msg
    assert "'stop_codon' on line 3" in msg
    assert 'on_multipart_conflict="split"' in msg


def test_coordinates_score_and_phase_may_all_differ(tmp_path):
    """The whole point. Only seqid/source/featuretype/strand are constrained."""
    src = (
        "##gff-version 3\n"
        "chr1\trs\tCDS\t100\t200\t10.5\t+\t0\tID=x\n"
        "chr1\trs\tCDS\t800\t900\t99.0\t+\t2\tID=x\n"
    )
    db = create_db(_write(tmp_path, src), ":memory:", mode="strict")
    assert _rows(db, "SELECT score, frame FROM segments WHERE feature_id='x' ORDER BY seg_idx") == [
        ("10.5", "0"),
        ("99.0", "2"),
    ]


# ---------------------------------------------------------------------------
# on_multipart_conflict="split"
# ---------------------------------------------------------------------------


def test_split_partitions_by_the_constraint_key(tmp_path):
    src = (
        "##gff-version 3\n"
        "chr1\trs\tCDS\t100\t200\t.\t+\t0\tID=x\n"
        "chr1\trs\tstart_codon\t800\t900\t.\t+\t0\tID=x\n"
    )
    db = create_db(_write(tmp_path, src), ":memory:", mode="strict", on_multipart_conflict="split")
    assert _rows(db, "SELECT id, featuretype FROM features ORDER BY file_order") == [
        ("x", "CDS"),
        ("x_1", "start_codon"),
    ]


def test_split_keeps_the_lowest_file_order_on_the_bare_id(tmp_path):
    src = (
        "##gff-version 3\n"
        "chr1\trs\tstop_codon\t800\t900\t.\t+\t0\tID=x\n"
        "chr1\trs\tCDS\t100\t200\t.\t+\t0\tID=x\n"
    )
    db = create_db(_write(tmp_path, src), ":memory:", mode="strict", on_multipart_conflict="split")
    assert _rows(db, "SELECT id FROM features ORDER BY file_order") == [("x",), ("x_1",)]
    assert _rows(db, "SELECT featuretype FROM features WHERE id='x'") == [("stop_codon",)]


def test_split_still_fuses_the_rows_that_do_share_a_key(tmp_path):
    """A genuine split CDS plus one stray line: the CDS must still fuse."""
    src = (
        "##gff-version 3\n"
        "chr1\trs\tCDS\t100\t200\t.\t+\t0\tID=x\n"
        "chr1\trs\tCDS\t800\t900\t.\t+\t2\tID=x\n"
        "chr1\trs\tstart_codon\t100\t102\t.\t+\t0\tID=x\n"
    )
    db = create_db(_write(tmp_path, src), ":memory:", mode="strict", on_multipart_conflict="split")
    assert _rows(db, "SELECT id, featuretype, n_segments FROM features ORDER BY file_order") == [
        ("x", "CDS", 2),
        ("x_1", "start_codon", 1),
    ]


def test_split_is_recorded_with_its_reason(tmp_path):
    src = (
        "##gff-version 3\n"
        "chr1\trs\tCDS\t100\t200\t.\t+\t0\tID=x\n"
        "chr1\trs\tstart_codon\t800\t900\t.\t+\t0\tID=x\n"
    )
    db = create_db(_write(tmp_path, src), ":memory:", mode="strict", on_multipart_conflict="split")
    rows = _rows(db, "SELECT raw_id, kind, detail FROM id_conflicts")
    assert ("x", "strict_split") == rows[0][:2]
    assert "featuretype differs" in rows[0][2]


def test_a_split_rename_does_not_collide_with_a_literal_id(tmp_path):
    """`_autoincrement` only guarantees uniqueness against its own counters,
    and this runs after the bulk load -- so a file that literally contains
    `x_1` would otherwise get a second row claiming it."""
    src = (
        "##gff-version 3\n"
        "chr1\trs\tCDS\t100\t200\t.\t+\t0\tID=x\n"
        "chr1\trs\tstart_codon\t800\t900\t.\t+\t0\tID=x\n"
        "chr1\trs\tgene\t1\t9\t.\t+\t.\tID=x_1\n"
    )
    db = create_db(_write(tmp_path, src), ":memory:", mode="strict", on_multipart_conflict="split")
    ids = [r[0] for r in _rows(db, "SELECT id FROM features")]
    assert len(ids) == len(set(ids)) == 3
    assert "x_1" in ids


def test_an_invalid_conflict_action_is_rejected(tmp_path):
    with pytest.raises(ValueError, match="on_multipart_conflict"):
        create_db(_write(tmp_path, SPLIT_CDS), ":memory:", on_multipart_conflict="nonsense")


# ---------------------------------------------------------------------------
# Surrogate ids are strictly internal
# ---------------------------------------------------------------------------


#: Both resolution paths, each with a source that actually reaches it: the
#: first fuses cleanly, the second has to split before it can fuse.
_LEAK_CASES = {
    "fuse": (
        {},
        "##gff-version 3\n"
        "chr1\trs\tmRNA\t100\t900\t.\t+\t.\tID=t\n"
        "chr1\trs\tCDS\t100\t200\t.\t+\t0\tID=x;Parent=t\n"
        "chr1\trs\tCDS\t800\t900\t.\t+\t2\tID=x;Parent=t\n",
    ),
    "split-then-fuse": (
        {"on_multipart_conflict": "split"},
        "##gff-version 3\n"
        "chr1\trs\tmRNA\t100\t900\t.\t+\t.\tID=t\n"
        "chr1\trs\tCDS\t100\t200\t.\t+\t0\tID=x;Parent=t\n"
        "chr1\trs\tCDS\t800\t900\t.\t+\t2\tID=x;Parent=t\n"
        "chr1\trs\tstart_codon\t100\t102\t.\t+\t0\tID=x;Parent=t\n",
    ),
}


@pytest.mark.parametrize("case", list(_LEAK_CASES), ids=list(_LEAK_CASES))
def test_no_surrogate_id_ever_reaches_a_table(tmp_path, case):
    """Duplicates load under a surrogate id so they can coexist under the
    primary key until the run can be judged as a whole. Every one must be
    renamed back or deleted; one escaping would reach users as a feature id
    with a control character in it.

    The split path is the one that got this wrong first: it renamed `raw_id`
    and left the surrogate sitting in `id`.
    """
    kwargs, src = _LEAK_CASES[case]
    db = create_db(_write(tmp_path, src), ":memory:", mode="strict", **kwargs)
    for table, column in (
        ("features", "id"),
        ("features", "raw_id"),
        ("attributes", "feature_id"),
        ("segments", "feature_id"),
        ("edges", "parent"),
        ("edges", "child"),
        ("closure", "ancestor"),
        ("closure", "descendant"),
    ):
        leaked = _rows(
            db, f"SELECT COUNT(*) FROM {table} WHERE {column} LIKE ?", ["%" + _SURROGATE_SEP + "%"]
        )
        assert leaked == [(0,)], f"surrogate id leaked into {table}.{column}"


def test_fusing_does_not_pollute_the_autoincrement_counters(fused):
    """The surrogate deliberately bypasses `_autoincrement`: these rows are
    deleted, so counters recording them would misinform a later `update()`
    into skipping ids nobody ever used."""
    assert _rows(fused, "SELECT COUNT(*) FROM autoincrements") == [(0,)]


# ---------------------------------------------------------------------------
# Corpus cases
# ---------------------------------------------------------------------------


def _count_runs(path: Path) -> tuple[int, int]:
    """(runs satisfying the predicate, runs conflicting), read straight from
    the text. Deriving the expectation from the file rather than hard-coding it
    keeps the test honest if the fixture is ever refreshed."""
    groups: dict[str, list] = {}
    for line in path.read_text(errors="replace").splitlines():
        if not line or line.startswith(("#", ">")):
            continue
        cols = line.split("\t")
        if len(cols) < 9:
            continue
        m = re.search(r"(?:^|;)\s*ID=([^;]*)", cols[8])
        if not m or not m.group(1):
            continue
        groups.setdefault(m.group(1), []).append((cols[0], cols[1], cols[2], cols[6]))
    runs = [v for v in groups.values() if len(v) > 1]
    ok = sum(1 for v in runs if len(set(v)) == 1)
    return ok, len(runs) - ok


@pytest.mark.parametrize("name", ["synthetic.gff3", "random-chr.gff"])
def test_small_upstream_corpora_fuse_exactly_their_runs(name):
    path = UPSTREAM / name
    expected_ok, expected_conflicts = _count_runs(path)
    assert expected_conflicts == 0, "fixture changed; this one should have no conflicts"
    db = create_db(str(path), ":memory:", mode="strict")
    assert _rows(db, "SELECT COUNT(*) FROM features WHERE n_segments > 1") == [(expected_ok,)]


def test_ncbi_gff3_is_the_negative_case():
    """Real, malformed GFF3: one id shared by a CDS, a start_codon and a
    stop_codon, and another shared by genes on opposite strands. Fusing those
    would invent features that do not exist in the file."""
    ok, conflicts = _count_runs(UPSTREAM / "ncbi_gff3.txt")
    assert conflicts > 0 and ok == 0, "fixture changed; it should conflict on every run"
    with pytest.raises(MultipartConstraintError):
        create_db(str(UPSTREAM / "ncbi_gff3.txt"), ":memory:", mode="strict")


def test_ncbi_gff3_loads_under_split():
    db = create_db(
        str(UPSTREAM / "ncbi_gff3.txt"), ":memory:", mode="strict", on_multipart_conflict="split"
    )
    ids = [r[0] for r in _rows(db, "SELECT id FROM features")]
    assert len(ids) == len(set(ids)), "split must leave every id unique"
    assert _rows(db, "SELECT COUNT(*) FROM id_conflicts WHERE kind='strict_split'")[0][0] > 0


@pytest.mark.skipif(
    not os.environ.get("GFFBASE_GFFUTILS_DATA"),
    reason="set GFFBASE_GFFUTILS_DATA to a gffutils test/data directory",
)
def test_flybase_50k_fuses_every_run():
    """The scale case: 345 genuine segment runs over 690 lines."""
    path = Path(os.environ["GFFBASE_GFFUTILS_DATA"]) / "dmel-all-no-analysis-r5.49_50k_lines.gff"
    if not path.is_file():
        pytest.skip(f"{path.name} not present")
    expected_ok, _ = _count_runs(path)
    db = create_db(str(path), ":memory:", mode="strict", on_multipart_conflict="split")
    assert _rows(db, "SELECT COUNT(*) FROM features WHERE n_segments > 1") == [(expected_ok,)]
    # Every logical feature accounts for exactly its own lines.
    assert _rows(db, "SELECT COUNT(*) FROM segments") == [(expected_ok * 2,)]


# ---------------------------------------------------------------------------
# Envelope exactness -- the invariant a wrong fusion breaks silently
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("name", ["synthetic.gff3", "random-chr.gff"])
def test_the_envelope_is_exactly_min_max_over_the_segments(name):
    """The single most important post-fusion invariant: a too-narrow envelope
    silently drops the feature from `region()`, and a too-wide one is only
    caught by the segment recheck."""
    db = create_db(str(UPSTREAM / name), ":memory:", mode="strict")
    assert _rows(
        db,
        """
        SELECT COUNT(*) FROM features f
        JOIN (SELECT feature_id, MIN(start) mn, MAX("end") mx
              FROM segments GROUP BY feature_id) s ON s.feature_id = f.id
        WHERE f.start IS DISTINCT FROM s.mn OR f."end" IS DISTINCT FROM s.mx
        """,
    ) == [(0,)]


@pytest.mark.parametrize("name", ["synthetic.gff3", "random-chr.gff"])
def test_segments_all_yields_exactly_one_row_per_input_line(name):
    """`n_features - n_multipart + n_segments` -- the arithmetic only works if
    the view's two branches are disjoint and `segments` holds every segment."""
    db = create_db(str(UPSTREAM / name), ":memory:", mode="strict")
    (n_feat,) = _rows(db, "SELECT COUNT(*) FROM features")[0]
    (n_mp,) = _rows(db, "SELECT COUNT(*) FROM features WHERE n_segments > 1")[0]
    (n_seg,) = _rows(db, "SELECT COUNT(*) FROM segments")[0]
    (n_all,) = _rows(db, "SELECT COUNT(*) FROM segments_all")[0]
    assert n_all == n_feat - n_mp + n_seg


@pytest.mark.parametrize("name", ["synthetic.gff3", "random-chr.gff"])
def test_n_segments_agrees_with_the_stored_segment_rows(name):
    db = create_db(str(UPSTREAM / name), ":memory:", mode="strict")
    assert _rows(
        db,
        """
        SELECT COUNT(*) FROM features f
        LEFT JOIN (SELECT feature_id, COUNT(*) n FROM segments GROUP BY feature_id) s
               ON s.feature_id = f.id
        WHERE f.n_segments <> COALESCE(s.n, 1)
        """,
    ) == [(0,)]


@pytest.mark.parametrize("name", ["synthetic.gff3", "random-chr.gff"])
def test_seg_idx_is_dense_and_starts_at_zero(name):
    db = create_db(str(UPSTREAM / name), ":memory:", mode="strict")
    assert _rows(
        db,
        """
        SELECT COUNT(*) FROM (
            SELECT feature_id, MIN(seg_idx) lo, MAX(seg_idx) hi,
                   COUNT(*) n, COUNT(DISTINCT seg_idx) d
            FROM segments GROUP BY feature_id
        ) WHERE lo <> 0 OR hi <> n - 1 OR d <> n
        """,
    ) == [(0,)]
