# Schema v1 migration fixture

`schema_v1.duckdb.gz` is a **real** gffbase database, written by the gffbase
code that predates schema v2 — not a reconstruction. It exists so the migration
is tested against the bytes v1 actually produced, including the DuckDB storage
format, rather than against a v2 database stripped back to a v1 shape.

| | |
|---|---|
| Built by | commit `a6d5e91` ("fix: let null coordinates round-trip"), the last commit with `SCHEMA_VERSION = "1"` |
| gffbase version | 0.1.1 |
| DuckDB version | 1.5.5 |
| Source file | `schema_v1_source.gff3` (in this directory) |
| Built with | `create_db(src, dst, merge_strategy="create_unique")` |

The source deliberately contains a discontinuous CDS — two lines sharing
`ID=cds1`. Under v1 that could only become two features, `cds1` and
`cds1_1`, which is exactly the state `coalesce_multipart` has to recognise
and re-fuse.

Stored gzipped: DuckDB pre-allocates, so the raw file is ~6 MB and compresses
to ~12 KB.

## Regenerating

    git worktree add /tmp/v1tree a6d5e91
    cd /tmp/v1tree
    cp <repo>/python/gffbase/_native.abi3.so python/gffbase/     # or use the fallback
    PYTHONPATH=.:python python -c "import gffbase; gffbase.create_db(
        '<repo>/tests/data/v1/schema_v1_source.gff3', '/tmp/v1.duckdb',
        force=True, merge_strategy='create_unique').conn.close()"
    gzip -c /tmp/v1.duckdb > <repo>/tests/data/v1/schema_v1.duckdb.gz
