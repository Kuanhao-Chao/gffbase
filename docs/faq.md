---
title: FAQ
---

# Frequently asked questions

Short answers with a pointer to the page that covers each properly. For a
symptom you are actively hitting, go to
[Troubleshooting](guides/troubleshooting.md) instead — it is organised by
error message.

---

### Is gffbase really a drop-in replacement for gffutils?

For most code, yes: `import gffbase as gffutils` and carry on. 23 of the 24
`FeatureDB` methods match by name **and exact signature**, and the differences
that remain are declared in a register that CI checks against a pinned
`gffutils` build — it fails both on an undeclared gap and on a declaration
that has gone stale. One behavioural difference matters enough to read about
before a production run.
→ [Migration guide](migration.md)

### What is the one thing that will surprise me?

A **row-by-row loop over many IDs** is slower in gffbase than in gffutils.
DuckDB pays vectorization startup per call; SQLite, an OLTP engine, does not.
That is a real trade, not a defect, and it is why the batched API exists.
→ [Migration guide](migration.md)

### Why does my ingest fail with `DuplicateIDError`?

The file uses the split-CDS convention — several lines sharing one `ID`,
describing one discontinuous feature. RefSeq, MANE and GENCODE's GFF3 edition
all do this. `merge_strategy` defaults to `"error"`, as it does in gffutils,
so you choose the reading: `merge_strategy="create_unique"` renames the rows,
`mode="strict"` fuses them into one feature.
→ [Compatibility & strict modes](guides/modes.md)

### Do I need to close the database?

Yes. DuckDB takes an **exclusive lock** on the file for the life of a writable
handle, so an unclosed handle stops any other process opening it and, on
Windows, stops the file being replaced. Use `with`, or call `close()`.
→ [Connections & concurrency](guides/connections.md)

### Can several processes read one database at once?

Yes, with `FeatureDB(path, read_only=True)`. A read-only connection takes no
exclusive lock, so a `DataLoader` or a `multiprocessing.Pool` can fan out over
one annotation. Open the handle *inside* each worker: a DuckDB connection is
not fork-safe.
→ [Connections & concurrency](guides/connections.md)

### How do I get a dataframe out of it?

`db.to_table(featuretype, format="df")` returns the whole database, or a
filtered slice, as pandas — or `"polars"`, or `"arrow"` for a zero-copy
`pyarrow.Table`. No `Feature` objects are constructed at any layer.
→ [Usage gallery](usage_gallery.md)

### How do I get every exon for thousands of transcripts?

`db.children_batched(ids, featuretype="exon", format="arrow")` — one set-based
query returning Arrow buffers, with an `anchor` column carrying each row's
input ID so you can regroup without re-querying.
→ [Machine learning workflows](cookbooks/machine_learning_workflows.md)

### Why is my database bigger than the SQLite one?

Roughly 1.5×, and deliberately: gffbase materializes a transitive closure and
a long-form attributes table so hierarchy walks and attribute searches are
indexed lookups rather than scans. That is the trade for the query speed.
→ [Performance](performance.md)

### `native_available()` says False. Does that matter?

Results are identical — gffbase ships a pure-Python fallback parser that is
diffed against the Rust one in CI. Throughput is not identical. On a platform
with wheels it means the install went wrong.
→ [Installation](getting-started/installation.md)

### Do I need `bedtools` or `pybedtools`?

Only for `to_bedtool()` and `tsses()`. Everything else works without them.
`pybedtools` cannot be installed on Windows at all — its `pysam` dependency is
POSIX-only — so those two functions are Linux and macOS only.
→ [Installation](getting-started/installation.md)

### Can I still use my old gffutils database?

Not directly — it is SQLite and gffbase is DuckDB. Rebuild from the same GFF
source. Going the other way *is* supported: `export_sqlite(db, "legacy.db")`
writes a database real gffutils can open.
→ [Usage gallery](usage_gallery.md)

### What about a database made by gffbase 0.1.x?

It is upgraded in place when you open it. The migration is structural and
changes no query result, which is what makes it acceptable to run unasked; the
step that *does* change results is opt-in and separate.
→ [migrate](api/migrate.md)

### How do I know my database is sound?

`db.validate()` runs 14 structural invariants, each a single set-based query —
cheap enough for CI. `gffbase stats` summarises what is actually in it.
→ [validate](api/validate.md) · [Command line](cli.md)

### Which Python versions are supported?

3.10 through 3.14, one abi3 wheel per platform. DuckDB ≥ 1.4.1 and PyArrow
≥ 18.1, and those floors are enforced by a CI job that installs exactly them.
→ [Installation](getting-started/installation.md)

### How do I cite it?

→ [Citation](citation.md) — and please cite the annotation you used, and
`gffutils`, whose API and test corpus this builds on.
