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
"""Phase 4 integration: ingest GFF3 / GTF into DuckDB and verify the schema,
the closure, and GTF synthesis. Runs against the auto-detected engine
(prefers Rust if built; otherwise the pure-Python fallback).
"""

from __future__ import annotations

from pathlib import Path

import pytest
from gffbase import ingest
from gffbase._options import IngestOptions

DATA = Path(__file__).parent / "data"


@pytest.fixture
def hier_path():
    return str(DATA / "hierarchy.gff3")


@pytest.fixture
def synth_path():
    return str(DATA / "synthesize.gtf")


def test_gff3_basic_ingest(hier_path):
    con, stats = ingest.from_file(hier_path)
    assert stats.fmt == "gff3"
    # All authored rows present.
    n_features = con.execute("SELECT COUNT(*) FROM features").fetchone()[0]
    assert n_features == 8
    assert stats.n_features_synthetic_genes == 0
    assert stats.n_features_synthetic_transcripts == 0


def test_gff3_attributes_table(hier_path):
    con, _ = ingest.from_file(hier_path)
    rows = con.execute(
        "SELECT key, value FROM attributes WHERE feature_id = 'g1' ORDER BY key, idx"
    ).fetchall()
    keys = {k for k, _ in rows}
    assert "ID" in keys
    assert "Name" in keys
    # Look up a CDS by attribute key/value (this is the new fast-path).
    rows = con.execute(
        "SELECT feature_id FROM attributes WHERE key='Parent' AND value='t1' ORDER BY feature_id"
    ).fetchall()
    assert ("e1",) in rows
    assert ("c1",) in rows


def test_gff3_edges(hier_path):
    con, _ = ingest.from_file(hier_path)
    rows = set(con.execute("SELECT parent, child FROM edges").fetchall())
    assert ("g1", "t1") in rows
    assert ("g1", "t2") in rows
    assert ("t1", "e1") in rows
    assert ("t1", "c2") in rows


def test_gff3_closure_depths(hier_path):
    con, stats = ingest.from_file(hier_path)
    # depth 1: gene -> transcript, transcript -> exon/CDS
    assert ("g1", "t1", 1) in set(
        con.execute("SELECT ancestor, descendant, depth FROM closure WHERE depth=1").fetchall()
    )
    # depth 2: gene -> exon
    rows_d2 = set(con.execute("SELECT ancestor, descendant FROM closure WHERE depth=2").fetchall())
    assert ("g1", "e1") in rows_d2
    assert ("g1", "c2") in rows_d2
    # No depth=3 in this file (max actual depth is 2).
    n3 = con.execute("SELECT COUNT(*) FROM closure WHERE depth>=3").fetchone()[0]
    assert n3 == 0
    assert stats.n_closure_rows > 0


def test_gtf_synthesis_counts(synth_path):
    con, stats = ingest.from_file(synth_path)
    assert stats.fmt == "gtf"
    # 5 exons + 3 transcripts (T1, T2, T3) + 2 genes (G1, G2)
    assert stats.n_features_synthetic_transcripts == 3
    assert stats.n_features_synthetic_genes == 2
    n_total = con.execute("SELECT COUNT(*) FROM features").fetchone()[0]
    assert n_total == 5 + 3 + 2


def test_gtf_synthesis_extents(synth_path):
    con, _ = ingest.from_file(synth_path)
    # T1 spans exons 100..200 + 300..500
    row = con.execute("SELECT seqid, start, \"end\", strand FROM features WHERE id='T1'").fetchone()
    assert row == ("chr1", 100, 500, "+")
    # T2 = single exon 700..900
    row = con.execute("SELECT start, \"end\" FROM features WHERE id='T2'").fetchone()
    assert row == (700, 900)
    # G1 covers T1 and T2: 100..900
    row = con.execute("SELECT seqid, start, \"end\", strand FROM features WHERE id='G1'").fetchone()
    assert row == ("chr1", 100, 900, "+")
    # G2 covers T3 only: 1000..2500 on chr2
    row = con.execute("SELECT seqid, start, \"end\", strand FROM features WHERE id='G2'").fetchone()
    assert row == ("chr2", 1000, 2500, "-")


def test_gtf_closure_after_synthesis(synth_path):
    con, _ = ingest.from_file(synth_path)
    # G1 -> T1 (depth 1), G1 -> exon (depth 2)
    rows = set(con.execute("SELECT ancestor, descendant, depth FROM closure").fetchall())
    assert any(a == "G1" and d == 1 for a, _, d in rows)
    assert any(a == "G1" and d == 2 for a, _, d in rows)


def test_indexes_built(hier_path):
    con, _ = ingest.from_file(hier_path)
    idx = [
        r[0]
        for r in con.execute(
            "SELECT index_name FROM duckdb_indexes() WHERE table_name IN ('features','attributes','edges','closure')"
        ).fetchall()
    ]
    assert "features_seqstart" in idx
    assert "attributes_kv" in idx
    assert "closure_ancestor" in idx


def test_meta_recorded(hier_path):
    con, _ = ingest.from_file(hier_path)
    rows = dict(con.execute("SELECT key, value FROM meta").fetchall())
    assert rows.get("schema_version") == "1"
    assert rows.get("fmt") == "gff3"


def test_no_duplicate_edges_or_closure(synth_path):
    """Regression: an earlier draft inserted edges twice during GTF synthesis,
    causing duplicate (parent, child) and (ancestor, descendant, depth) rows.
    Ensure neither table has duplicates."""
    con, _ = ingest.from_file(synth_path)
    n_edges = con.execute("SELECT COUNT(*) FROM edges").fetchone()[0]
    n_unique_edges = con.execute(
        "SELECT COUNT(*) FROM (SELECT DISTINCT parent, child FROM edges)"
    ).fetchone()[0]
    assert n_edges == n_unique_edges
    n_clo = con.execute("SELECT COUNT(*) FROM closure").fetchone()[0]
    n_unique_clo = con.execute(
        "SELECT COUNT(*) FROM (SELECT DISTINCT ancestor, descendant, depth FROM closure)"
    ).fetchone()[0]
    assert n_clo == n_unique_clo


def test_rtree_query_returns_overlaps(synth_path):
    """Sanity: with the spatial extension loaded we can run an R-tree backed
    overlap query and get the expected exons back."""
    con, stats = ingest.from_file(synth_path)
    if not stats.rtree_built:
        pytest.skip("spatial extension unavailable")
    # bbox we built is ST_MakeEnvelope(start, 0, end, 1).
    # Query for features overlapping chr1:150..400 (catches exon 100..200 and 300..500).
    rows = con.execute(
        """
        SELECT id FROM features
        WHERE seqid = 'chr1'
          AND ST_Intersects(bbox, ST_MakeEnvelope(150, 0, 400, 1))
          AND featuretype = 'exon'
        ORDER BY start
        """
    ).fetchall()
    ids = [r[0] for r in rows]
    assert ids == ["exon_1", "exon_2"]


def test_force_overwrites(tmp_path, hier_path):
    out = tmp_path / "test.duckdb"
    ingest.from_file(hier_path, dbfn=str(out))
    # Re-ingesting without force should error.
    with pytest.raises(ValueError):
        ingest.from_file(hier_path, dbfn=str(out))
    # With force it should succeed.
    con, _ = ingest.from_file(hier_path, dbfn=str(out), force=True)
    n = con.execute("SELECT COUNT(*) FROM features").fetchone()[0]
    assert n == 8


_DUP_GFF3 = (
    "##gff-version 3\n"
    "chr1\trs\tgene\t1\t1000\t.\t+\t.\tID=g1\n"
    "chr1\trs\tmRNA\t1\t1000\t.\t+\t.\tID=t1;Parent=g1\n"
    "chr1\trs\tCDS\t100\t200\t.\t+\t0\tID=cds-x;Parent=t1\n"
    "chr1\trs\tCDS\t300\t400\t.\t+\t0\tID=cds-x;Parent=t1\n"
    "chr1\trs\tCDS\t500\t600\t.\t+\t0\tID=cds-x;Parent=t1\n"
)


def _dup_source(tmp_path):
    src = tmp_path / "dup.gff3"
    src.write_text(_DUP_GFF3)
    return str(src)


def test_duplicate_ids_raise_by_default(tmp_path):
    """The default `merge_strategy="error"` must actually raise.

    RefSeq emits multiple GFF3 rows sharing one `ID=cds-...`. gffbase used to
    rename them to `cds-x__2` unconditionally, which meant the documented
    default strategy was unreachable and a corrupt file loaded silently.
    """
    from gffbase import DuplicateIDError

    with pytest.raises(DuplicateIDError, match="Duplicate ID cds-x"):
        ingest.from_file(_dup_source(tmp_path))

    # gffutils raises a bare ValueError here, so callers written against it
    # use `except ValueError`. That has to keep working.
    with pytest.raises(ValueError, match="Duplicate ID cds-x"):
        ingest.from_file(_dup_source(tmp_path))


def test_duplicate_ids_create_unique(tmp_path):
    """`create_unique` autoincrements on the ID, matching gffutils.

    The suffix is `_1`, `_2`, ... from `_increment_featuretype_autoid(f.id)` --
    not the `__2`, `__3` gffbase previously invented -- and only the renamed
    rows are recorded in `duplicates`.
    """
    con, _ = ingest.from_file(
        _dup_source(tmp_path),
        options=IngestOptions(merge_strategy="create_unique"),
    )
    ids = sorted(
        r[0] for r in con.execute("SELECT id FROM features WHERE featuretype = 'CDS'").fetchall()
    )
    assert ids == ["cds-x", "cds-x_1", "cds-x_2"]
    # No `duplicates` rows: gffutils records a rename only when `merge` falls
    # back to create_unique, since the table exists so a later merge can find
    # the sibling rows.
    assert con.execute("SELECT COUNT(*) FROM duplicates").fetchone()[0] == 0


def test_duplicate_ids_warning_keeps_only_the_first(tmp_path):
    con, _ = ingest.from_file(
        _dup_source(tmp_path), options=IngestOptions(merge_strategy="warning")
    )
    rows = con.execute("SELECT id, start FROM features WHERE featuretype = 'CDS'").fetchall()
    assert rows == [("cds-x", 100)]


def test_duplicate_ids_replace_keeps_only_the_last(tmp_path):
    con, _ = ingest.from_file(
        _dup_source(tmp_path), options=IngestOptions(merge_strategy="replace")
    )
    rows = con.execute("SELECT id, start FROM features WHERE featuretype = 'CDS'").fetchall()
    assert rows == [("cds-x", 500)]


def test_duplicate_ids_merge_falls_back_when_coordinates_differ(tmp_path):
    """`merge` only fuses rows whose other eight columns match.

    These three CDS rows have different coordinates, so gffutils routes them
    to `create_unique` and records each rename in `duplicates`.
    """
    con, _ = ingest.from_file(_dup_source(tmp_path), options=IngestOptions(merge_strategy="merge"))
    ids = sorted(
        r[0] for r in con.execute("SELECT id FROM features WHERE featuretype = 'CDS'").fetchall()
    )
    assert ids == ["cds-x", "cds-x_1", "cds-x_2"]
    dups = con.execute("SELECT original_id, new_id FROM duplicates ORDER BY new_id").fetchall()
    assert dups == [("cds-x", "cds-x_1"), ("cds-x", "cds-x_2")]


def test_duplicate_ids_merge_unions_attributes_when_rows_agree(tmp_path):
    """Identical rows differing only in attributes are fused into one."""
    src = tmp_path / "same.gff3"
    src.write_text(
        "##gff-version 3\n"
        "chr1\trs\tCDS\t100\t200\t.\t+\t0\tID=c1;Note=first\n"
        "chr1\trs\tCDS\t100\t200\t.\t+\t0\tID=c1;Note=second;Extra=yes\n"
    )
    con, _ = ingest.from_file(str(src), options=IngestOptions(merge_strategy="merge"))
    assert con.execute("SELECT COUNT(*) FROM features").fetchone()[0] == 1
    attrs = con.execute(
        "SELECT key, value FROM attributes WHERE feature_id = 'c1' ORDER BY key, value"
    ).fetchall()
    assert ("Note", "first") in attrs
    assert ("Note", "second") in attrs
    assert ("Extra", "yes") in attrs
