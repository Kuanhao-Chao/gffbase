"""Phase 4 integration: ingest GFF3 / GTF into DuckDB and verify the schema,
the closure, and GTF synthesis. Runs against the auto-detected engine
(prefers Rust if built; otherwise the pure-Python fallback).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from gffbase import ingest

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
    assert ("g1", "t1", 1) in set(con.execute(
        "SELECT ancestor, descendant, depth FROM closure WHERE depth=1"
    ).fetchall())
    # depth 2: gene -> exon
    rows_d2 = set(con.execute(
        "SELECT ancestor, descendant FROM closure WHERE depth=2"
    ).fetchall())
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
    row = con.execute(
        "SELECT seqid, start, \"end\", strand FROM features WHERE id='T1'"
    ).fetchone()
    assert row == ("chr1", 100, 500, "+")
    # T2 = single exon 700..900
    row = con.execute(
        "SELECT start, \"end\" FROM features WHERE id='T2'"
    ).fetchone()
    assert row == (700, 900)
    # G1 covers T1 and T2: 100..900
    row = con.execute(
        "SELECT seqid, start, \"end\", strand FROM features WHERE id='G1'"
    ).fetchone()
    assert row == ("chr1", 100, 900, "+")
    # G2 covers T3 only: 1000..2500 on chr2
    row = con.execute(
        "SELECT seqid, start, \"end\", strand FROM features WHERE id='G2'"
    ).fetchone()
    assert row == ("chr2", 1000, 2500, "-")


def test_gtf_closure_after_synthesis(synth_path):
    con, _ = ingest.from_file(synth_path)
    # G1 -> T1 (depth 1), G1 -> exon (depth 2)
    rows = set(con.execute(
        "SELECT ancestor, descendant, depth FROM closure"
    ).fetchall())
    assert any(a == "G1" and d == 1 for a, _, d in rows)
    assert any(a == "G1" and d == 2 for a, _, d in rows)


def test_indexes_built(hier_path):
    con, _ = ingest.from_file(hier_path)
    idx = [r[0] for r in con.execute(
        "SELECT index_name FROM duckdb_indexes() WHERE table_name IN ('features','attributes','edges','closure')"
    ).fetchall()]
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
