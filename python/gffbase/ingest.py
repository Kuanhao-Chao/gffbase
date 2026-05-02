"""Phase 4 ingestion engine.

Streams the Rust parser's output through PyArrow record batches into DuckDB,
then runs a fixed sequence of set-based SQL passes for normalization, GTF
synthesis, transitive closure, and indexing. No per-feature Python loops in
the hot path — everything that scales with feature count goes through Arrow
or pure SQL.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Iterable, Iterator, List, Optional, Tuple

import duckdb
import pyarrow as pa

from gffbase import parser as _parser
from gffbase.feature import ParsedFeature
from gffbase.schema import (
    DDL,
    POST_LOAD_INDEXES,
    EDGES_FROM_PARENT,
    EDGES_FROM_GTF,
    GTF_SYNTHESIZE_TRANSCRIPTS,
    GTF_SYNTHESIZE_TRANSCRIPT_ATTRS,
    GTF_PROPAGATE_GENE_ID,
    GTF_SYNTHESIZE_GENES,
    CLOSURE_RECURSIVE_CTE,
    COMPAT_VIEWS_SQL,
    SCHEMA_VERSION,
)


DEFAULT_BATCH_SIZE = 50_000
DEFAULT_MAX_DEPTH = 8


@dataclass
class IngestStats:
    """Reported back to the caller for benchmarking and tests."""
    n_features_raw: int = 0
    n_features_synthetic_transcripts: int = 0
    n_features_synthetic_genes: int = 0
    n_attributes: int = 0
    n_edges: int = 0
    n_closure_rows: int = 0
    rtree_built: bool = False
    fmt: str = "gff3"
    dialect: dict = None  # type: ignore[assignment]
    directives: List[str] = None  # type: ignore[assignment]


# ---------------------------------------------------------------------------
# Arrow batch builder. Accumulates ParsedFeature output until a fixed row
# count, then yields one PyArrow Table per category (features, attributes).
# Edges and directives are derived later by SQL.
# ---------------------------------------------------------------------------

class _ArrowBatchBuilder:
    """Accumulates parsed features into column-oriented Python lists, then
    produces PyArrow tables on flush. We deliberately keep the schema explicit
    so DuckDB sees the right column types (no INFER passes)."""

    FEATURES_SCHEMA = pa.schema([
        ("id",              pa.string()),
        ("seqid",           pa.string()),
        ("source",          pa.string()),
        ("featuretype",     pa.string()),
        ("start",           pa.int64()),
        ("end",             pa.int64()),
        ("score",           pa.string()),
        ("strand",          pa.string()),
        ("frame",           pa.string()),
        ("attributes_blob", pa.binary()),
        ("extra_blob",      pa.binary()),
        ("file_order",      pa.int64()),
        ("is_synthetic",    pa.bool_()),
    ])

    ATTRIBUTES_SCHEMA = pa.schema([
        ("feature_id", pa.string()),
        ("key",        pa.string()),
        ("value",      pa.string()),
        ("idx",        pa.int16()),
    ])

    def __init__(self):
        self._reset()

    def _reset(self):
        # Feature columns
        self.f_id: list = []
        self.f_seqid: list = []
        self.f_source: list = []
        self.f_type: list = []
        self.f_start: list = []
        self.f_end: list = []
        self.f_score: list = []
        self.f_strand: list = []
        self.f_frame: list = []
        self.f_blob: list = []
        self.f_extra: list = []
        self.f_order: list = []
        self.f_synth: list = []
        # Attribute columns
        self.a_fid: list = []
        self.a_key: list = []
        self.a_val: list = []
        self.a_idx: list = []

    def append(self, feat_id: str, feat: ParsedFeature, file_order: int):
        self.f_id.append(feat_id)
        self.f_seqid.append(feat.seqid)
        self.f_source.append(feat.source)
        self.f_type.append(feat.featuretype)
        self.f_start.append(feat.start if feat.start is not None else 0)
        self.f_end.append(feat.end if feat.end is not None else 0)
        self.f_score.append(feat.score)
        self.f_strand.append(feat.strand)
        self.f_frame.append(feat.frame)
        self.f_blob.append(feat.attributes_blob)
        self.f_extra.append(("\t".join(feat.extra)).encode("utf-8") if feat.extra else b"")
        self.f_order.append(file_order)
        self.f_synth.append(False)
        for k, v, idx in feat.attributes_pairs:
            self.a_fid.append(feat_id)
            self.a_key.append(k)
            self.a_val.append(v)
            self.a_idx.append(idx)

    def __len__(self) -> int:
        return len(self.f_id)

    def features_table(self) -> pa.Table:
        return pa.table({
            "id": self.f_id,
            "seqid": self.f_seqid,
            "source": self.f_source,
            "featuretype": self.f_type,
            "start": self.f_start,
            "end": self.f_end,
            "score": self.f_score,
            "strand": self.f_strand,
            "frame": self.f_frame,
            "attributes_blob": self.f_blob,
            "extra_blob": self.f_extra,
            "file_order": self.f_order,
            "is_synthetic": self.f_synth,
        }, schema=self.FEATURES_SCHEMA)

    def attributes_table(self) -> pa.Table:
        return pa.table({
            "feature_id": self.a_fid,
            "key": self.a_key,
            "value": self.a_val,
            "idx": self.a_idx,
        }, schema=self.ATTRIBUTES_SCHEMA)

    def flush_into(self, con: duckdb.DuckDBPyConnection):
        if not self.f_id:
            return
        feats = self.features_table()
        attrs = self.attributes_table()
        # Register and INSERT ... SELECT — DuckDB's fastest Arrow path.
        # We enumerate columns explicitly because the `features` table now
        # has a trailing `seqid_y` column populated post-load by the R-tree
        # build pass; the Arrow batch only carries the original 13 columns.
        con.register("__staging_features", feats)
        con.register("__staging_attributes", attrs)
        con.execute(
            "INSERT INTO features ("
            "id, seqid, source, featuretype, start, \"end\", "
            "score, strand, frame, attributes_blob, extra_blob, "
            "file_order, is_synthetic"
            ") SELECT * FROM __staging_features"
        )
        con.execute("INSERT INTO attributes SELECT * FROM __staging_attributes")
        con.unregister("__staging_features")
        con.unregister("__staging_attributes")
        self._reset()


# ---------------------------------------------------------------------------
# ID resolution. Pulled out so the bulk loop has zero branches that hit Python
# attribute parsing twice.
# ---------------------------------------------------------------------------

def _derive_id(feat: ParsedFeature, dialect_fmt: str, autoincrement: dict) -> str:
    """Compute the row's primary key. GFF3 prefers `ID=`; GTF synthesizes
    `<featuretype>_<n>` so leaf rows still get unique IDs (gene/transcript
    IDs are filled in by the synthesis pass)."""
    if dialect_fmt == "gff3":
        for k, v, _idx in feat.attributes_pairs:
            if k == "ID":
                return v
    # Fall back: synthesize an auto-id by featuretype.
    n = autoincrement.get(feat.featuretype, 0) + 1
    autoincrement[feat.featuretype] = n
    return f"{feat.featuretype}_{n}"


# ---------------------------------------------------------------------------
# Public entry point.
# ---------------------------------------------------------------------------

def from_file(
    path: str,
    dbfn: str = ":memory:",
    *,
    force: bool = False,
    batch_size: int = DEFAULT_BATCH_SIZE,
    max_depth: int = DEFAULT_MAX_DEPTH,
    disable_infer_genes: bool = False,
    disable_infer_transcripts: bool = False,
    gtf_subfeature: str = "exon",
    engine: Optional[str] = "auto",
    build_rtree: bool = True,
) -> Tuple[duckdb.DuckDBPyConnection, IngestStats]:
    """Ingest a GFF3 or GTF file into a DuckDB database.

    Returns the open connection plus an `IngestStats` summary. The connection
    is the canonical handle the (Phase 5) `FeatureDB` will wrap.
    """
    if dbfn != ":memory:":
        if os.path.exists(dbfn) and not force:
            raise ValueError(
                f"{dbfn} already exists. Pass force=True to overwrite."
            )
        if os.path.exists(dbfn) and force:
            os.unlink(dbfn)

    con = duckdb.connect(dbfn)
    _apply_pragmas(con)
    con.execute(DDL)

    # Drive the parser.
    it = _parser.parse_gff(path, engine=engine)
    builder = _ArrowBatchBuilder()
    autoinc: dict = {}
    # NCBI RefSeq emits multiple GFF3 rows that share an `ID=cds-…` (a CDS
    # is "split" across exon-segments). Our schema has `id` as a primary
    # key, so we mimic gffutils' `merge_strategy="create_unique"`: track
    # how many times we've seen each base id and append `__N` (N >= 2)
    # when needed. The first occurrence keeps the bare id.
    id_counts: dict = {}
    duplicate_pairs: list = []  # (base_id, new_id)
    file_order = 0
    n_raw = 0

    for feat in it:
        file_order += 1
        n_raw += 1
        fid = _derive_id(feat, _dialect_fmt_safe(it), autoinc)
        seen = id_counts.get(fid, 0)
        if seen:
            new_fid = f"{fid}__{seen + 1}"
            duplicate_pairs.append((fid, new_fid))
            id_counts[fid] = seen + 1
            fid = new_fid
        else:
            id_counts[fid] = 1
        builder.append(fid, feat, file_order)
        if len(builder) >= batch_size:
            builder.flush_into(con)
    builder.flush_into(con)

    # Record duplicate-id remappings (informational; the schema already has
    # this table — Phase 5).
    if duplicate_pairs:
        dup_tbl = pa.table({
            "original_id": [b for b, _ in duplicate_pairs],
            "new_id":      [n for _, n in duplicate_pairs],
        })
        con.register("__staging_dups", dup_tbl)
        con.execute(
            "INSERT INTO duplicates (original_id, new_id) "
            "SELECT original_id, new_id FROM __staging_dups"
        )
        con.unregister("__staging_dups")

    dialect = it.dialect()
    directives = list(it.directives())
    fmt = (dialect or {}).get("fmt", "gff3")

    # Persist directives in one shot (set-based).
    if directives:
        dir_table = pa.table({"directive": directives})
        con.register("__staging_directives", dir_table)
        con.execute("INSERT INTO directives (directive) SELECT directive FROM __staging_directives")
        con.unregister("__staging_directives")

    # Set-based normalization passes.
    n_synth_t = 0
    n_synth_g = 0

    if fmt == "gtf":
        if not disable_infer_transcripts:
            n_synth_t = _synthesize_transcripts(con, gtf_subfeature)
        if not disable_infer_genes:
            n_synth_g = _synthesize_genes(con, gtf_subfeature)
        con.execute(EDGES_FROM_GTF)
    else:
        con.execute(EDGES_FROM_PARENT)

    # Closure via recursive CTE.
    con.execute(CLOSURE_RECURSIVE_CTE, [max_depth])

    # Indexes — only after all data is materialized.
    con.execute(POST_LOAD_INDEXES)

    # Optional R-tree on coordinates.
    rtree_built = False
    if build_rtree:
        rtree_built = _build_rtree(con)

    # SQLite-compat views (must run after closure has been populated).
    con.execute(COMPAT_VIEWS_SQL)

    # Stats.
    n_attributes = con.execute("SELECT COUNT(*) FROM attributes").fetchone()[0]
    n_edges = con.execute("SELECT COUNT(*) FROM edges").fetchone()[0]
    n_closure = con.execute("SELECT COUNT(*) FROM closure").fetchone()[0]

    # Meta — record dialect, fmt, and the rtree availability so a re-opened
    # DB can route queries correctly without probing.
    _write_meta(con, dialect, fmt, rtree_built=rtree_built, max_depth=max_depth)

    return con, IngestStats(
        n_features_raw=n_raw,
        n_features_synthetic_transcripts=n_synth_t,
        n_features_synthetic_genes=n_synth_g,
        n_attributes=n_attributes,
        n_edges=n_edges,
        n_closure_rows=n_closure,
        rtree_built=rtree_built,
        fmt=fmt,
        dialect=dialect,
        directives=directives,
    )


# ---------------------------------------------------------------------------
# Helpers.
# ---------------------------------------------------------------------------

def _dialect_fmt_safe(it) -> str:
    """The Rust iterator commits to a dialect after the peek phase. The
    Python fallback only sets it after the first record yields. Both are
    populated by the time we land in this function on the first feature."""
    try:
        d = it.dialect()
        if d:
            return d.get("fmt", "gff3")
    except Exception:
        pass
    return "gff3"


def _apply_pragmas(con: duckdb.DuckDBPyConnection):
    # DuckDB's defaults are excellent; we only nudge memory & threads. Anything
    # missing here is left to the caller via DUCKDB_THREADS env var.
    threads = os.environ.get("GFFUTILS2_THREADS")
    if threads:
        con.execute(f"PRAGMA threads = {int(threads)}")
    # Suppress the interactive progress bar — it floods stderr in batch and
    # subprocess scenarios and offers no value for benchmarking or scripting.
    try:
        con.execute("PRAGMA disable_progress_bar")
    except duckdb.Error:
        pass


def _synthesize_transcripts(con, subfeature: str) -> int:
    """Run the GROUP BY transcript synthesis. Returns rows inserted."""
    before = con.execute(
        "SELECT COUNT(*) FROM features WHERE featuretype = 'transcript'"
    ).fetchone()[0]
    con.execute(GTF_SYNTHESIZE_TRANSCRIPTS, [subfeature])
    after = con.execute(
        "SELECT COUNT(*) FROM features WHERE featuretype = 'transcript'"
    ).fetchone()[0]
    n = after - before
    # Make the synthesized transcripts visible to subsequent passes by giving
    # them a self-referential transcript_id attribute, then propagate the
    # gene_id from the children. The propagation joins attributes directly,
    # so no temporary edge inserts are needed (duplicate-edge-free).
    con.execute(GTF_SYNTHESIZE_TRANSCRIPT_ATTRS)
    con.execute(GTF_PROPAGATE_GENE_ID)
    return n


def _synthesize_genes(con, subfeature: str) -> int:
    before = con.execute(
        "SELECT COUNT(*) FROM features WHERE featuretype = 'gene'"
    ).fetchone()[0]
    con.execute(GTF_SYNTHESIZE_GENES, [subfeature])
    after = con.execute(
        "SELECT COUNT(*) FROM features WHERE featuretype = 'gene'"
    ).fetchone()[0]
    n = after - before
    # Mirror the synthesized gene_id into attributes so downstream queries
    # treat synthesized genes the same as authored ones.
    con.execute(
        """
        INSERT INTO attributes (feature_id, key, value, idx)
        SELECT f.id, 'gene_id', f.id, 0
        FROM features f
        WHERE f.featuretype = 'gene' AND f.is_synthetic = TRUE
        """
    )
    return n


SEQID_Y_BAND = 1_000_000  # gap between adjacent seqids' y-bands.


def _build_rtree(con: duckdb.DuckDBPyConnection) -> bool:
    """Try to build a true R-tree index. Falls back to a multi-column B-tree
    if the spatial extension is unavailable. Returns True iff R-tree was
    actually built.

    Phase 7 fix: each distinct seqid is assigned its own y-band so the R-tree
    split heuristics segregate chromosomes. Per-seqid envelopes look like
    ``ST_MakeEnvelope(start, seqid_y, "end", seqid_y + 1)`` with seqid_y
    values 1,000,000 apart — a single internal R-tree node cannot accidentally
    union two seqids.

    Phase 8 hook: ``GFFBASE_TEST_DISABLE_RTREE=1`` short-circuits the build so
    the CI matrix can exercise the multi-column B-tree fallback path without
    test-code changes. The env var is honored library-wide (not just by tests)
    so debugging users on offline machines can switch identically.
    """
    if os.environ.get("GFFBASE_TEST_DISABLE_RTREE", "").lower() in ("1", "true", "yes"):
        return False
    try:
        con.execute("INSTALL spatial")
        con.execute("LOAD spatial")
    except duckdb.Error:
        return False
    try:
        # 1. Build the seqid → y-band map. Sorted for determinism, gaps of
        #    SEQID_Y_BAND so different chromosomes never share R-tree leaves.
        rows = con.execute(
            "SELECT DISTINCT seqid FROM features ORDER BY seqid"
        ).fetchall()
        # Bulk-load the side table.
        seqid_rows = [(r[0], i * SEQID_Y_BAND) for i, r in enumerate(rows)]
        con.execute("DELETE FROM seqid_map")
        con.executemany("INSERT INTO seqid_map(seqid, seqid_y) VALUES (?, ?)", seqid_rows)

        # 2. Stamp seqid_y on every feature row.
        con.execute("""
            UPDATE features
            SET seqid_y = m.seqid_y
            FROM seqid_map m
            WHERE features.seqid = m.seqid
        """)

        # 3. Add the bbox geometry column and populate using the per-seqid band.
        con.execute("""
            ALTER TABLE features
            ADD COLUMN IF NOT EXISTS bbox GEOMETRY
        """)
        con.execute("""
            UPDATE features
            SET bbox = ST_MakeEnvelope(start, seqid_y, "end", seqid_y + 1)
            WHERE bbox IS NULL OR bbox IS NOT NULL  -- always re-stamp; cheap on small tables
        """)
        con.execute("CREATE INDEX IF NOT EXISTS features_rtree ON features USING RTREE (bbox)")
        return True
    except duckdb.Error:
        return False


def _write_meta(con: duckdb.DuckDBPyConnection, dialect: dict, fmt: str,
                *, rtree_built: bool = False, max_depth: int = DEFAULT_MAX_DEPTH):
    import json
    # Closure max depth: used by FeatureDB's relational dispatcher (Phase 7) to
    # pick the cache vs. dynamic CTE without a per-call query.
    row = con.execute("SELECT MAX(depth) FROM closure").fetchone()
    closure_max_depth = int(row[0]) if row and row[0] is not None else 0
    rows = [
        ("schema_version", SCHEMA_VERSION),
        ("dialect", json.dumps(dialect or {})),
        ("fmt", fmt),
        ("rtree_built", "true" if rtree_built else "false"),
        ("max_depth", str(int(max_depth))),
        ("closure_max_depth", str(closure_max_depth)),
    ]
    con.executemany("INSERT OR REPLACE INTO meta(key, value) VALUES (?, ?)", rows)
