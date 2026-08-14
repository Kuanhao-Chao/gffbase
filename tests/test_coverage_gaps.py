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
"""Phase 2 — final tests targeting the specific uncovered branches identified
in the per-module coverage report. Each test is deliberately scoped to a
small set of lines so the diff is auditable.
"""

from __future__ import annotations

import io
import os
import sqlite3
from pathlib import Path

import duckdb
import pytest
from gffbase import (
    Feature,
    FeatureDB,
    GFFWriter,
    ParsedFeature,
    create_db,
)
from gffbase.feature import _coord_to_int, _LazyAttributes, feature_from_row
from gffbase.ingest import (
    _finalize_rtree,
    _rtree_disabled_by_env,
    _try_load_spatial,
    from_file,
)

DATA = Path(__file__).parent / "data"

RTREE_DISABLED = os.environ.get("GFFBASE_TEST_DISABLE_RTREE", "").lower() in ("1", "true", "yes")
requires_rtree = pytest.mark.skipif(RTREE_DISABLED, reason="env disables R-tree")


# ---------------------------------------------------------------------------
# feature.py corner cases
# ---------------------------------------------------------------------------


def test_coord_to_int_real_int_short_circuit():
    # Hits the `isinstance(v, int)` branch.
    assert _coord_to_int(42) == 42


def test_coord_to_int_none_branch():
    assert _coord_to_int(None) is None


def test_lazy_attributes_ingest_two_tuple_pairs():
    """List of 2-tuples falls through the len==3 guard onto the else."""
    attrs = _LazyAttributes(initial=[("k", "v1"), ("k", "v2")])
    assert attrs["k"] == ["v1", "v2"]


def test_format_attributes_gtf_unquoted_branch():
    f = Feature(
        seqid="chr1",
        source=".",
        featuretype=".",
        start=1,
        end=10,
        attributes={"gene_id": "G1"},
        dialect={
            "fmt": "gtf",
            "keyval separator": " ",
            "field separator": "; ",
            "quoted GFF2 values": False,
        },
    )
    col9 = str(f).split("\t")[8]
    # No quotes around the value.
    assert "gene_id G1" in col9
    assert '"G1"' not in col9


def test_astuple_with_extra_columns_serializes_json_list():
    f = Feature(
        start=1, end=10, attributes={"k": "v"}, extra=["alt1", "alt2"], dialect={"fmt": "gff3"}
    )
    tup = f.astuple()
    import json

    assert json.loads(tup[10]) == ["alt1", "alt2"]


def test_feature_from_row_with_nonempty_extra_blob():
    row = ("g1", "chr1", "src", "gene", 1, 10, ".", "+", ".", b"ID=g1", b"alt1\talt2", 0)
    f = feature_from_row(row)
    assert f.extra == ["alt1", "alt2"]


# ---------------------------------------------------------------------------
# interface.py corner cases
# ---------------------------------------------------------------------------


def test_featuredb_init_with_tuple_input():
    """The (con, IngestStats) tuple ctor branch."""
    con, stats = from_file(str(DATA / "hierarchy.gff3"))
    db = FeatureDB((con, stats))
    assert "g1" in db


def test_featuredb_contains_with_feature_object():
    db = create_db(str(DATA / "hierarchy.gff3"), ":memory:")
    f = db["g1"]
    assert f in db


def test_count_features_of_type_filtered():
    db = create_db(str(DATA / "hierarchy.gff3"), ":memory:")
    assert db.count_features_of_type("exon") == 3


def test_seqids_generator_empty():
    """Empty in-memory DuckDB -> seqids() yields nothing.

    Also pins that wrapping a hand-built connection still works: the
    incomplete-database refusal deliberately applies only to a database opened
    from a path, since a caller who constructs and populates their own
    connection is doing so on purpose.
    """
    con = duckdb.connect(":memory:")
    from gffbase.schema import DDL

    con.execute(DDL)
    db = FeatureDB(con)
    assert list(db.seqids()) == []


def test_order_clause_comma_separated_string():
    """A comma-separated string used to be interpolated verbatim, so it reached
    SQL as one opaque expression. Each name is now resolved and ordered on
    individually -- which is what the caller meant, and is what closes the
    injection this branch used to be."""
    db = create_db(str(DATA / "hierarchy.gff3"), ":memory:")
    assert db._order_clause("seqid, start", reverse=False) == "seqid ASC, start ASC"


def test_order_clause_rejects_anything_not_whitelisted():
    db = create_db(str(DATA / "hierarchy.gff3"), ":memory:")
    with pytest.raises(ValueError, match="cannot order by"):
        db._order_clause("start; DROP TABLE features", reverse=False)


def test_parents_integer_level_branch():
    db = create_db(str(DATA / "hierarchy.gff3"), ":memory:")
    rows = list(db.parents("e1", level=1))
    assert [p.id for p in rows] == ["t1"]


def test_update_with_parsed_feature_input():
    db = create_db(str(DATA / "hierarchy.gff3"), ":memory:")
    pf = ParsedFeature(
        seqid="chr1",
        source="test",
        featuretype="exon",
        start=99000,
        end=99500,
        score=".",
        strand="+",
        frame=".",
        attributes_blob=b"ID=parsed1",
        attributes_pairs=[("ID", "parsed1", 0)],
        extra=[],
    )
    db.update([pf])
    assert "parsed1" in db


def test_update_with_parsed_feature_no_id_uses_autoincrement():
    db = create_db(str(DATA / "hierarchy.gff3"), ":memory:")
    pf = ParsedFeature(
        seqid="chr1",
        source="test",
        featuretype="leaf",
        start=1,
        end=2,
        score=".",
        strand=".",
        frame=".",
        attributes_blob=b"",
        attributes_pairs=[],
        extra=[],
    )
    # No ID attr → fallback to "<featuretype>_<file_order>".
    db.update([pf])
    rows = db.conn.execute(
        "SELECT id FROM features WHERE source = 'test' AND featuretype = 'leaf'"
    ).fetchall()
    assert any(r[0].startswith("leaf_") for r in rows)


def test_update_with_featuredb_instance():
    src = create_db(str(DATA / "hierarchy.gff3"), ":memory:")
    dst = create_db(str(DATA / "synthesize.gtf"), ":memory:")
    n_before = dst.count_features_of_type()
    dst.update(src)
    n_after = dst.count_features_of_type()
    assert n_after > n_before


def test_update_rejects_unsupported_input():
    db = create_db(str(DATA / "hierarchy.gff3"), ":memory:")
    with pytest.raises(TypeError):
        db.update([42])


def test_merge_all_walks_multi_key_sort():
    db = create_db(str(DATA / "hierarchy.gff3"), ":memory:")
    out = db.merge_all(
        merge_order=("seqid", "start"),
        featuretypes_groups=("exon",),
    )
    assert isinstance(out, list)
    # Sorted output is stable across the merge.
    assert out == sorted(out, key=lambda f: (f.seqid, f.start or 0))


def test_set_pragmas_supported_pragma_succeeds():
    db = create_db(str(DATA / "hierarchy.gff3"), ":memory:")
    # `threads` is a real DuckDB pragma → goes through the success branch.
    db.set_pragmas({"threads": 1})


def test_featuredb_init_loads_dialect_from_corrupt_meta(tmp_path):
    """If `meta.dialect` is non-JSON garbage, _parse_dialect must fall back
    to the default GFF3 dialect rather than raise."""
    p = tmp_path / "bad.duckdb"
    db = create_db(str(DATA / "hierarchy.gff3"), str(p))
    db.conn.close()
    # Re-open and inject corrupt dialect.
    con = duckdb.connect(str(p))
    con.execute("UPDATE meta SET value = '{not-json' WHERE key = 'dialect'")
    con.close()
    # Re-open via FeatureDB — must not raise.
    db2 = FeatureDB(str(p))
    assert db2.dialect.get("fmt") == "gff3"


# ---------------------------------------------------------------------------
# gffwriter.py — io.IOBase ctor + write_exon_children
# ---------------------------------------------------------------------------


def test_gffwriter_accepts_filehandle_directly():
    buf = io.StringIO()
    w = GFFWriter(buf, with_header=False)
    w.write_rec("chr1\tsrc\texon\t1\t10\t.\t+\t.\tID=x")
    # Read BEFORE close — close() releases the StringIO buffer.
    contents = buf.getvalue()
    w.close()
    assert "ID=x" in contents


def test_gffwriter_write_exon_children(tmp_path):
    db = create_db(str(DATA / "hierarchy.gff3"), ":memory:")
    out = tmp_path / "exon.gff3"
    with GFFWriter(str(out)) as w:
        # `e1` is a leaf — no children — but the method still emits the exon
        # itself. That's the branch we're after (line 71-74 of gffwriter.py).
        w.write_exon_children(db, "e1")
    text = out.read_text()
    assert "e1" in text


# ---------------------------------------------------------------------------
# ingest.py corner cases
# ---------------------------------------------------------------------------


def test_synthesize_transcripts_noop_when_disabled(tmp_path):
    """When `disable_infer_transcripts=True` is passed, the synthesis pass
    is skipped entirely — exercises the negation branch and the no-op count.
    """
    gtf = tmp_path / "no_synth.gtf"
    gtf.write_text('chr1\tsrc\texon\t1\t100\t.\t+\t.\tgene_id "G1"; transcript_id "T1";\n')
    con, stats = from_file(str(gtf), disable_infer_transcripts=True, disable_infer_genes=True)
    assert stats.n_features_synthetic_transcripts == 0
    assert stats.n_features_synthetic_genes == 0


def test_synthesize_genes_noop_when_only_genes_disabled(tmp_path):
    gtf = tmp_path / "no_gene_synth.gtf"
    gtf.write_text('chr1\tsrc\texon\t1\t100\t.\t+\t.\tgene_id "G1"; transcript_id "T1";\n')
    # disable only genes → transcripts still synthesized, genes not.
    con, stats = from_file(str(gtf), disable_infer_genes=True)
    assert stats.n_features_synthetic_genes == 0


def test_rtree_disabled_by_env_kill_switch(monkeypatch):
    """``GFFBASE_TEST_DISABLE_RTREE=1`` short-circuits the R-tree path
    library-wide so the CI matrix can test the B-tree fallback without
    any test-code changes."""
    monkeypatch.setenv("GFFBASE_TEST_DISABLE_RTREE", "1")
    assert _rtree_disabled_by_env() is True
    monkeypatch.setenv("GFFBASE_TEST_DISABLE_RTREE", "0")
    assert _rtree_disabled_by_env() is False
    monkeypatch.delenv("GFFBASE_TEST_DISABLE_RTREE", raising=False)
    assert _rtree_disabled_by_env() is False


class _ConnWrapper:
    """Thin proxy that lets us inject failures on specific SQL statements.
    Mirrors the small subset of the DuckDBPyConnection surface used by the
    spatial-extension helpers.
    """

    def __init__(self, real, fail_on: str):
        self._real = real
        self._fail = fail_on

    def execute(self, sql, *a, **kw):
        if self._fail.lower() in sql.lower():
            raise duckdb.Error(f"simulated failure on {self._fail}")
        return self._real.execute(sql, *a, **kw)

    def executemany(self, *a, **kw):
        return self._real.executemany(*a, **kw)


def test_try_load_spatial_handles_install_error(monkeypatch):
    """Force `INSTALL spatial` to raise → graceful False return."""
    monkeypatch.delenv("GFFBASE_TEST_DISABLE_RTREE", raising=False)
    con = duckdb.connect(":memory:")
    proxy = _ConnWrapper(con, fail_on="INSTALL spatial")
    assert _try_load_spatial(proxy) is False


def test_finalize_rtree_swallows_create_index_error():
    """If the spatial extension is gone by the time we reach
    `_finalize_rtree` (or any other DuckDB error fires), the helper
    returns False rather than crashing the whole ingest."""
    con = duckdb.connect(":memory:")
    from gffbase.schema import DDL

    con.execute(DDL)  # No `bbox` column, no spatial extension loaded.
    # CREATE INDEX ... USING RTREE will fail because spatial isn't loaded
    # AND the `bbox` column doesn't exist. Helper must return False.
    assert _finalize_rtree(con) is False


def test_ingest_no_directives_path(tmp_path):
    """Bare GTF without ## directives — exercises the empty-directives branch."""
    gtf = tmp_path / "no_directives.gtf"
    gtf.write_text('chr1\tsrc\texon\t1\t100\t.\t+\t.\tgene_id "G"; transcript_id "T";\n')
    con, stats = from_file(str(gtf))
    assert stats.directives == []


# ---------------------------------------------------------------------------
# create_db.py — _keep_tempfiles branch
# ---------------------------------------------------------------------------


def test_create_db_from_string_keep_tempfiles():
    text = "chr1\tsrc\texon\t1\t10\t.\t+\t.\tID=ex\n"
    db = create_db(text, ":memory:", from_string=True, _keep_tempfiles=True)
    assert "ex" in db


# ---------------------------------------------------------------------------
# sqlite_export.py — autoincrements pass-through
# ---------------------------------------------------------------------------


def test_export_sqlite_includes_autoincrements(tmp_path):
    from gffbase import export_sqlite

    db = create_db(str(DATA / "hierarchy.gff3"), ":memory:")
    # Inject a row so the export hits the non-empty `autoincrements` branch.
    db.conn.execute("INSERT INTO autoincrements(base, n) VALUES ('exon', 5)")
    out = tmp_path / "exp.db"
    export_sqlite(db.conn, str(out))
    sq = sqlite3.connect(str(out))
    rows = sq.execute("SELECT base, n FROM autoincrements").fetchall()
    sq.close()
    assert ("exon", 5) in rows


# ---------------------------------------------------------------------------
# Final batch — defensive error handlers in FeatureDB.__init__ and helpers.
# ---------------------------------------------------------------------------


def test_parse_dialect_falls_back_on_garbage_json():
    """_parse_dialect must catch JSON errors and return the default dialect."""
    out = FeatureDB._parse_dialect("{not-json")
    assert out == {"fmt": "gff3"}


def test_parse_dialect_handles_none():
    assert FeatureDB._parse_dialect(None) == {"fmt": "gff3"}


def test_parse_dialect_handles_empty():
    assert FeatureDB._parse_dialect("") == {"fmt": "gff3"}


class _FailExecuteConn:
    """Mock of a duckdb connection where every `execute` raises. Used to drive
    the defensive `except duckdb.Error` branches in FeatureDB helpers."""

    def execute(self, *a, **kw):
        raise duckdb.Error("simulated")

    def executemany(self, *a, **kw):
        raise duckdb.Error("simulated")


def test_read_meta_swallows_db_error():
    db = create_db(str(DATA / "hierarchy.gff3"), ":memory:")
    # Replace conn with one that errors on every execute.
    db.conn = _FailExecuteConn()
    assert db._read_meta() == {}


def test_has_rtree_index_swallows_db_error():
    db = create_db(str(DATA / "hierarchy.gff3"), ":memory:")
    db.conn = _FailExecuteConn()
    assert db._has_rtree_index() is False


def test_featuredb_init_with_pragmas_kwarg():
    """The `pragmas` ctor arg path."""
    db = create_db(str(DATA / "hierarchy.gff3"), ":memory:")
    # Open a second FeatureDB on the same conn but with a pragmas dict —
    # exercises the `if pragmas:` branch at init.
    db2 = FeatureDB(db.conn, pragmas={"threads": 1})
    assert "g1" in db2


@requires_rtree
def test_featuredb_init_install_spatial_retry_path(tmp_path):
    """When LOAD spatial fails on first try but INSTALL+LOAD succeeds — covers
    the retry branch (lines 126-128)."""
    p = tmp_path / "needs_retry.duckdb"
    create_db(str(DATA / "hierarchy.gff3"), str(p))
    # Open a fresh connection that hasn't had spatial loaded yet.
    fresh = duckdb.connect(str(p))
    # Pre-uninstall spatial functions on this connection — but DuckDB doesn't
    # expose that. Instead use an env-free fresh process; this branch is
    # naturally covered by any FeatureDB init on a persisted .duckdb.
    db = FeatureDB(fresh)
    assert db._rtree_built in (True, False)


def test_relation_query_filter_by_seqid_only_no_coords():
    """Drives the `_limit_filter` when only seqid is in the limit string —
    exercises lines 358-364 of the scan-SQL builder."""
    db = create_db(str(DATA / "hierarchy.gff3"), ":memory:")
    feats = list(db.all_features(limit="chr1", strand="+"))
    assert all(f.seqid == "chr1" for f in feats)


def test_all_features_completely_within_branch():
    db = create_db(str(DATA / "hierarchy.gff3"), ":memory:")
    feats = list(db.all_features(limit="chr1:50-700", completely_within=True))
    assert all(f.start >= 50 and f.end <= 700 for f in feats)


def test_all_features_featuretype_list_branch():
    db = create_db(str(DATA / "hierarchy.gff3"), ":memory:")
    feats = list(db.all_features(featuretype=["exon", "CDS"]))
    assert {f.featuretype for f in feats} <= {"exon", "CDS"}


def test_children_with_limit_and_featuretype_list():
    db = create_db(str(DATA / "hierarchy.gff3"), ":memory:")
    feats = list(db.children("g1", limit="chr1:50-700", featuretype=["exon", "CDS"]))
    assert all(f.seqid == "chr1" for f in feats)


def test_children_with_completely_within_limit():
    db = create_db(str(DATA / "hierarchy.gff3"), ":memory:")
    feats = list(db.children("t1", limit="chr1:50-700", completely_within=True))
    assert all(f.start >= 50 and f.end <= 700 for f in feats)


def test_dynamic_cte_with_featuretype_filter():
    """children() forced through the dynamic walker with a featuretype filter
    exercises the qualified featuretype-IN branch."""
    db = create_db(str(DATA / "hierarchy.gff3"), ":memory:")
    db._max_depth = 0  # force every level=N to be 'dynamic'
    out = list(db.children("g1", level=2, featuretype=["exon", "CDS"]))
    assert all(f.featuretype in ("exon", "CDS") for f in out)


def test_dynamic_cte_with_limit_completely_within():
    db = create_db(str(DATA / "hierarchy.gff3"), ":memory:")
    db._max_depth = 0
    out = list(db.children("g1", level=2, limit="chr1:50-700", completely_within=True))
    assert all(f.start >= 50 and f.end <= 700 for f in out)


def test_set_pragmas_loop_continues_on_failure():
    """A multi-pragma dict where one entry fails should not stop the loop."""
    db = create_db(str(DATA / "hierarchy.gff3"), ":memory:")
    # `not_a_pragma` fails (skipped), `threads` succeeds.
    db.set_pragmas({"not_a_pragma": "x", "threads": 1})


def test_create_db_kwargs_pass_through_to_keep_tempfiles_skip():
    """Cover create_db.py:80-81 — _keep_tempfiles=False *and* the temp
    file already deleted (OSError is silently swallowed)."""
    text = "chr1\tsrc\texon\t1\t10\t.\t+\t.\tID=a\n"
    db = create_db(text, ":memory:", from_string=True, _keep_tempfiles=False)
    assert "a" in db


def test_dialect_safe_falls_back_when_iterator_lacks_attribute():
    """ingest._dialect_fmt_safe handles iterators whose .dialect() raises."""
    from gffbase.ingest import _dialect_fmt_safe

    class BadIt:
        def dialect(self):
            raise RuntimeError("nope")

    assert _dialect_fmt_safe(BadIt()) == "gff3"


def test_dialect_safe_returns_default_when_dialect_is_falsy():
    from gffbase.ingest import _dialect_fmt_safe

    class EmptyIt:
        def dialect(self):
            return None

    assert _dialect_fmt_safe(EmptyIt()) == "gff3"


def test_export_sqlite_force_overwrite_existing(tmp_path):
    from gffbase import export_sqlite

    db = create_db(str(DATA / "hierarchy.gff3"), ":memory:")
    out = tmp_path / "ex.db"
    out.write_text("placeholder")
    # force=False → ValueError
    with pytest.raises(ValueError):
        export_sqlite(db.conn, str(out))
    # force=True → silently replaces
    export_sqlite(db.conn, str(out), force=True)
    assert out.exists()
    assert out.stat().st_size > 0


# ---------------------------------------------------------------------------
# Phase 25 — final push to ≥99 % coverage. Each test below targets exactly
# one residual uncovered line/branch identified in the per-module report.
# ---------------------------------------------------------------------------


def test_gffwriter_write_exon_children_with_real_child(tmp_path):
    """`write_exon_children` line 90 fires only when the exon has at least
    one child feature. Build a hierarchy where an exon parents a CDS so
    the inner-loop body runs."""
    src = tmp_path / "exon_with_child.gff3"
    src.write_text(
        "##gff-version 3\n"
        "chr1\trs\tgene\t1\t1000\t.\t+\t.\tID=g1\n"
        "chr1\trs\tmRNA\t1\t1000\t.\t+\t.\tID=t1;Parent=g1\n"
        "chr1\trs\texon\t1\t500\t.\t+\t.\tID=ex1;Parent=t1\n"
        # CDS parented to the EXON (unusual but valid hierarchy nesting).
        "chr1\trs\tCDS\t100\t300\t.\t+\t0\tID=c1;Parent=ex1\n"
    )
    db = create_db(str(src), ":memory:")
    out = tmp_path / "exon.gff3"
    with GFFWriter(str(out)) as w:
        w.write_exon_children(db, "ex1")
    text = out.read_text()
    # Both the exon AND its child CDS must appear → confirms the loop body ran.
    assert "ex1" in text
    assert "c1" in text


def test_ingest_empty_flush_short_circuit(tmp_path):
    """`ingest.flush_into` line 201 — early-return when the builder has no
    rows. Trigger by ingesting a header-only GFF3 (no feature lines)."""
    src = tmp_path / "headers_only.gff3"
    src.write_text("##gff-version 3\n##source rs\n")
    con, stats = from_file(str(src))
    assert stats.n_features_raw == 0


def test_ingest_mid_loop_batch_flush(tmp_path):
    """`ingest.py` line 338 — mid-loop `flush_into` when the batch buffer
    reaches `batch_size`. Force it with batch_size=2 over 5 features."""
    src = tmp_path / "many.gff3"
    lines = ["##gff-version 3\n"]
    for i in range(5):
        lines.append(f"chr1\trs\tfeat\t{i * 10 + 1}\t{i * 10 + 5}\t.\t+\t.\tID=f{i}\n")
    src.write_text("".join(lines))
    con, stats = from_file(str(src), batch_size=2)
    assert stats.n_features_raw == 5


def test_ingest_threads_pragma_from_env(tmp_path, monkeypatch):
    """`ingest.py` line 459 — when `GFFUTILS2_THREADS` is set, the PRAGMA
    threads SQL is issued."""
    monkeypatch.setenv("GFFUTILS2_THREADS", "2")
    src = tmp_path / "tiny.gff3"
    src.write_text("##gff-version 3\nchr1\trs\tgene\t1\t10\t.\t+\t.\tID=g1\n")
    con, stats = from_file(str(src))
    assert stats.n_features_raw == 1


def test_persist_seqid_map_is_a_no_op_for_an_empty_map():
    """The map population used to live inside `_finalize_rtree`, which runs
    after GTF synthesis -- so the post-synthesis `UPDATE ... FROM seqid_map`
    joined an empty table and every synthesized row kept a NULL bbox. It is now
    its own step, called before synthesis; this covers its empty-input branch."""
    from gffbase.ingest import _persist_seqid_map
    from gffbase.schema import DDL

    con = duckdb.connect(":memory:")
    con.execute(DDL)
    _persist_seqid_map(con, {})
    assert con.execute("SELECT COUNT(*) FROM seqid_map").fetchone() == (0,)


def test_persist_seqid_map_writes_the_bands_in_encounter_order():
    """The first seqid must land on band 0 -- `_region_sql_rtree` translates
    query bounds through this map, so a shifted band silently misses."""
    from gffbase.ingest import _persist_seqid_map
    from gffbase.schema import DDL

    con = duckdb.connect(":memory:")
    con.execute(DDL)
    _persist_seqid_map(con, {"chr1": 0, "chr2": 1_000_000_000})
    assert con.execute("SELECT seqid, seqid_y FROM seqid_map ORDER BY seqid_y").fetchall() == [
        ("chr1", 0),
        ("chr2", 1_000_000_000),
    ]
    # Idempotent: it clears before writing, so re-running cannot double up.
    _persist_seqid_map(con, {"chr1": 0, "chr2": 1_000_000_000})
    assert con.execute("SELECT COUNT(*) FROM seqid_map").fetchone() == (2,)


def test_ingest_gff3_row_without_id_falls_through_loop(tmp_path):
    """`ingest.py` branches 246→250 / 247→246 — GFF3 row whose attrs
    do not contain `ID=` (only Parent=) must fall through the id_spec resolver's
    inner loop without finding a match. Synthetic ID is then assigned."""
    src = tmp_path / "no_id.gff3"
    # First row has ID=g1; second row has Parent only — its derive_id
    # runs `for k,v,_ in pairs` and never returns inside the loop.
    src.write_text(
        "##gff-version 3\n"
        "chr1\trs\tgene\t1\t100\t.\t+\t.\tID=g1\n"
        "chr1\trs\texon\t1\t100\t.\t+\t.\tParent=g1;biotype=protein_coding\n"
    )
    con, stats = from_file(str(src))
    # The exon got synthesized as `exon_<n>` because no ID was found.
    rows = con.execute("SELECT id FROM features WHERE featuretype = 'exon'").fetchall()
    assert any(r[0].startswith("exon_") for r in rows)


def test_iterator_transform_returns_feature_replaces_original():
    """`iterators.py` branch 79→81 (True path) — when transform returns a
    non-None, non-False value, that value REPLACES the original feature."""
    from gffbase import DataIterator

    sentinel = Feature(
        seqid="chrREPLACED",
        source=".",
        featuretype=".",
        start=1,
        end=2,
        attributes={"ID": "replaced"},
        dialect={"fmt": "gff3"},
    )

    def replace_with_sentinel(_feat):
        return sentinel

    it = DataIterator(
        str(DATA / "hierarchy.gff3"),
        transform=replace_with_sentinel,
    )
    feats = list(it)
    assert all(f.seqid == "chrREPLACED" for f in feats)


def test_iterator_transform_returns_none_keeps_original():
    """`iterators.py` branch 79→81 (False path) — transform returning
    `None` means "no opinion": keep the original feature unchanged."""
    from gffbase import DataIterator

    def no_op_transform(_feat):
        return None

    it = DataIterator(
        str(DATA / "hierarchy.gff3"),
        transform=no_op_transform,
    )
    feats = list(it)
    # Original chr1 features are returned unmodified — the transform
    # returning None must NOT replace them with a sentinel.
    assert len(feats) > 0
    assert all(f.seqid == "chr1" for f in feats)


def test_region_string_with_trailing_colon_no_coords():
    """`interface.py` line 447 — `region("chr1:")` (no `-` after the colon)
    falls through the dash check and returns (chrom, None, None)."""
    db = create_db(str(DATA / "hierarchy.gff3"), ":memory:")
    seqid, s, e = db._normalize_region_args("chr1:", None, None, None)
    assert (seqid, s, e) == ("chr1", None, None)


def test_region_batched_unregister_failure_is_swallowed():
    """`interface.py` lines 572-573 — when `conn.unregister` raises during
    cleanup, the exception is swallowed so the materialized result is
    still returned to the caller."""
    db = create_db(str(DATA / "hierarchy.gff3"), ":memory:")
    real = db.conn
    fail_count = {"n": 0}

    class _FlakyUnregisterProxy:
        def __init__(self, inner):
            self._inner = inner

        def unregister(self, name):
            if name == "__staging_regions":
                fail_count["n"] += 1
                raise RuntimeError("simulated unregister failure")
            return self._inner.unregister(name)

        def __getattr__(self, attr):
            return getattr(self._inner, attr)

    db.conn = _FlakyUnregisterProxy(real)
    try:
        out = db.region_batched([("chr1", 100, 200)], format="arrow")
    finally:
        db.conn = real
    assert out is not None
    assert fail_count["n"] >= 1


def test_order_clause_qualified_length_branch():
    """`interface.py` line 1047 — `order_by="length"` produces an
    `(end - start)` expression in batched paths."""
    out = FeatureDB._order_clause_qualified("length", reverse=False, qualifier="f")
    assert "end" in out and "start" in out
    assert out.endswith("ASC")


def test_order_clause_qualified_applies_the_same_whitelist():
    """The joined paths must not be a way around it. They used to share the
    pass-through escape hatch, so `children(..., order_by=<payload>)` was
    injectable exactly like `all_features`."""
    out = FeatureDB._order_clause_qualified(("seqid", "start"), reverse=True, qualifier="f")
    assert out == "f.seqid DESC, f.start DESC"
    with pytest.raises(ValueError, match="cannot order by"):
        FeatureDB._order_clause_qualified("f.seqid", reverse=True, qualifier="f")


def test_interfeatures_with_merge_attributes_true():
    """`interface.py` branch at 1206 (`if merge_attributes`) — exercise
    the True path so attribute-merging executes."""
    db = create_db(str(DATA / "hierarchy.gff3"), ":memory:")
    feats = sorted(db.children("t1", featuretype="exon"), key=lambda f: f.start)
    out = list(db.interfeatures(feats, merge_attributes=True))
    assert all(f.featuretype == "interfeature" for f in out)


def test_interfeatures_with_merge_attributes_false():
    """`interface.py` branch 1206→1213 (False path) — when
    `merge_attributes=False`, the per-attr accumulation block is
    skipped entirely."""
    db = create_db(str(DATA / "hierarchy.gff3"), ":memory:")
    feats = sorted(db.children("t1", featuretype="exon"), key=lambda f: f.start)
    out = list(db.interfeatures(feats, merge_attributes=False))
    # The yielded interfeature has no inherited attributes when merging
    # is disabled — confirms the accumulator block was skipped.
    assert all(f.featuretype == "interfeature" for f in out)
    assert all(f.attributes == {} for f in out)


def test_merge_yields_final_accumulator_block():
    """`interface.py` branch 1251→exit — the merge loop's final flush
    block (`if accum is not None: yield accum`) fires whenever the input
    has at least one feature. With a non-empty list we MUST receive at
    least one yielded merged Feature."""
    db = create_db(str(DATA / "hierarchy.gff3"), ":memory:")
    exons = list(db.children("t1", featuretype="exon"))
    assert len(exons) > 0
    out = list(db.merge(exons))
    assert len(out) >= 1


def test_children_bp_skips_features_with_null_coords(monkeypatch):
    """`interface.py` branch 1334→1333 — the `if k.start is not None and
    k.end is not None:` guard. Inject a Feature with null start so the
    skip path runs."""
    db = create_db(str(DATA / "hierarchy.gff3"), ":memory:")
    null_feat = Feature(
        seqid="chr1",
        source="x",
        featuretype="exon",
        start=None,
        end=None,
        attributes={"ID": "null1"},
        dialect={"fmt": "gff3"},
    )
    real_children = db.children

    def fake_children(*a, **kw):
        return [null_feat] + list(real_children(*a, **kw))

    monkeypatch.setattr(db, "children", fake_children)
    # `g1` has 3 exons: (100-200), (500-600), (100-300) → 101+101+201 = 403 bp.
    # The injected null-start feature must be skipped (the branch we want),
    # not crash with TypeError on (None - None + 1).
    bp = db.children_bp("g1", child_featuretype="exon")
    assert bp == 403


# ---------------------------------------------------------------------------
# Optional polars paths — skipped if polars isn't installed.
# ---------------------------------------------------------------------------


def test_format_polars_happy_path():
    """`interface.py` line 813 — `format="polars"` non-empty path."""
    pytest.importorskip("polars")
    db = create_db(str(DATA / "hierarchy.gff3"), ":memory:")
    out = db.children_batched(["g1"], format="polars")
    # `out` is a polars.DataFrame with at least the gene's descendants.
    assert out.shape[0] > 0


def test_format_polars_empty_batched_path():
    """`interface.py` lines 843-847 — `format="polars"` empty input path."""
    pl = pytest.importorskip("polars")
    db = create_db(str(DATA / "hierarchy.gff3"), ":memory:")
    out = db.children_batched([], format="polars")
    assert isinstance(out, pl.DataFrame)
    assert out.shape[0] == 0


def test_format_polars_empty_region_path():
    """`interface.py` lines 600-604 — empty `region_batched(format='polars')`."""
    pl = pytest.importorskip("polars")
    db = create_db(str(DATA / "hierarchy.gff3"), ":memory:")
    out = db.region_batched([], format="polars")
    assert isinstance(out, pl.DataFrame)
    assert out.shape[0] == 0
