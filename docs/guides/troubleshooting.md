---
title: Troubleshooting
---

# Troubleshooting

Organised by the error you are looking at. For "how does this work" questions,
see the [FAQ](../faq.md).

---

## Ingest

### `DuplicateIDError: Duplicate ID cds-NP_...`

The file uses the **split-CDS convention**: several lines sharing one `ID=`,
describing one discontinuous feature. RefSeq and MANE both do this.

`merge_strategy` defaults to `"error"` — the same default `gffutils` uses, and
it raises on these same files — because there are two defensible readings and
picking one for you would give you a whole-genome answer you did not choose:

<!-- docs-test: skip reason="illustrative: `path` stands for the reader's file" -->
```python
create_db(path, "out.duckdb", merge_strategy="create_unique")  # renamed, gffutils-style
create_db(path, "out.duckdb", mode="strict")                   # one discontinuous feature
```

See [Compatibility & strict modes](modes.md).

### `GFFFormatError` on a file other tools read

You are almost certainly in `mode="strict"`. Real annotations break the spec
routinely; the default `mode="compat"` annotates violations instead of
rejecting them. The exception carries a pointer into the file:

<!-- docs-test: skip reason="illustrative: names a file the reader supplies" -->
```python
import gffbase

try:
    gffbase.create_db("annotation.gff3", "out.duckdb", mode="strict")
except gffbase.GFFFormatError as exc:
    print(exc.line_no, exc.kind, exc.message)
```

To audit rather than abort, keep the strict rules but downgrade the action:

<!-- docs-test: skip reason="illustrative: `path` stands for the reader's file" -->
```python
db = create_db(path, "out.duckdb", validation="ncbi", on_error="warn")
for w in db.warnings:
    print(w["line_no"], w["kind"], w["message"])
```

### Ingest used a lot of memory

Expected. GFFBase trades memory for speed — peak RSS runs to a couple of GB on
a whole-genome corpus, against roughly 170 MB for `gffutils`. The Arrow batch
builder is the reason, and it is also why ingest is faster.

To cap DuckDB's own threads (and with them its memory):

```bash
GFFBASE_THREADS=4 python your_script.py
```

### The ingest died and left a file behind

It should not have. Ingest is atomic: GFFBase writes to
`<dbfn>.gffbase-building.<pid>` and only renames on success, so a failed run
leaves the original untouched and the scratch file is removed. If you find a
`.gffbase-building.*` file, the process was killed hard (`SIGKILL`, power loss)
and it is safe to delete.

Note that atomicity relies on `os.replace` being atomic, which holds **within
one filesystem**. If `dbfn` is on a different mount than its temporary
neighbour, the guarantee is weaker.

---

## Querying

### `region()` returns nothing, but the feature is there

Check the seqid spelling. `chr1`, `1` and `NC_000001.11` are three different
sequences as far as the database is concerned — GENCODE uses `chr1`, Ensembl
uses `1`, RefSeq uses `NC_000001.11`.

```python
print(sorted(db.seqids())[:5])
```

### `children()` returns nothing for a feature that has children

Either the file has no `Parent=` edges to build the hierarchy from, or you are
asking for the wrong level. `level=1` is direct children only:

```python
list(db.children("gene1", level=None))     # everything below, any depth
```

For GTF input, missing `gene` and `transcript` rows can be synthesized from
`gene_id` / `transcript_id` attributes. Many modern GTFs—including GENCODE
v49—already contain explicit parent rows; disable inference when you want to
preserve exactly that supplied hierarchy. If parent rows and identifying
attributes are both absent, there is nothing to connect or synthesize.

### Iterating is slower than `gffutils`

For a **loop over many IDs**, yes, and this is inherent rather than a bug —
DuckDB pays vectorization startup per call, which an OLTP engine like SQLite
does not. Use the batched APIs:

<!-- docs-test: skip reason="illustrative: contains an elided fragment" -->
```python
# slow
for tid in transcript_ids:
    for exon in db.children(tid, featuretype="exon"):
        ...

# fast: one query, no Python Feature objects
exons = db.children_batched(transcript_ids, featuretype="exon", format="arrow")
```

### My iteration stopped early

Something else queried the same connection while the iteration was still
streaming. A DuckDB connection holds one result set at a time. Materialize
first, or use a second handle — see
[Connections & concurrency](connections.md).

---

## Databases and files

### `IOException: Could not set lock on file`

Another connection holds the database. Either another process has it open, or
an earlier handle in this process was never closed. Use `with`, or call
`close()`. For concurrent readers, open with `read_only=True`.

### `ClosedDatabaseError`

The handle was closed — usually by leaving a `with` block earlier than
intended. Open a new one.

### `SchemaVersionError`

Either the database was written by a **newer** GFFBase than the one reading it
(upgrade GFFBase), or it is a schema-v1 database opened with
`upgrade="error"` or `read_only=True`. Migrate it once:

```bash
gffbase migrate old.duckdb
```

### `... has gffbase tables but no metadata at all`

An ingest that failed part-way, or a truncated file. Rebuild it with
`create_db(..., force=True)`. GFFBase refuses to open it rather than handing
back an empty database that reports itself as complete.

### The database is larger than the SQLite one

Expected — roughly 1.5×. GFFBase stores a materialized transitive closure and a
long-form attributes table so that hierarchy and attribute queries are indexed
lookups instead of scans. That is the trade.

---

## Environment

### `native_available()` returns `False`

The Rust extension did not load, so the pure-Python fallback parser is running.
Results are identical; throughput is not. On a platform with wheels this means
the install went wrong — try `pip install --force-reinstall gffbase`. From a
source checkout, run `maturin develop --release --manifest-path rust/Cargo.toml`.

### Spatial queries are slower than the published numbers

Check which index the database is using:

```python
db._rtree_built     # False → B-tree fallback
```

DuckDB downloads its `spatial` extension on first use. On a machine without
network egress that fails and GFFBase falls back to a multi-column B-tree —
correct answers, lower throughput. Nothing is raised, because the fallback is
not an error.

### `ImportError` mentioning polars / pandas / pyfaidx / pybedtools

An optional integration whose extra is not installed. The message names the
exact command:

```bash
pip install gffbase[polars]
pip install gffbase[all]
```

---

## Compatibility

### Is it really a drop-in replacement for `gffutils`?

For the large majority of code, yes — `import gffbase as gffutils` and
carry on. The differences that remain are declared and tested in
`tests/parity/deviations.toml`, and the one behavioural gotcha (row-by-row
loops) is covered in the [Migration guide](../migration.md).

### Can I hand the database to a tool that expects `gffutils`?

Yes. Export a real SQLite database that `gffutils` itself can open:

```python
from gffbase import export_sqlite
export_sqlite(db, "legacy.db")
```

### Which Python versions are supported?

3.10 through 3.14, one abi3 wheel per platform.
