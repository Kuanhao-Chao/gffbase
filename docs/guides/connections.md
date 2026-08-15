---
title: Connections & concurrency
---

# Connections & concurrency

DuckDB takes an **exclusive lock** on a database file for the life of a
writable connection. That single fact drives everything on this page: how long
a handle should live, why `read_only=True` exists, and why a worker pool needs
it.

---

## Close what you open

<!-- docs-test: skip reason="illustrative: names a corpus file the reader supplies" -->
```python
from gffbase import FeatureDB, create_db

with create_db("gencode.gtf.gz", "gencode.duckdb") as db:
    ...                     # lock held here

with FeatureDB("gencode.duckdb") as db:
    ...                     # and here

# or explicitly
db = FeatureDB("gencode.duckdb")
try:
    ...
finally:
    db.close()
```

`close()` is idempotent — calling it twice is a no-op, not an error. Using a
handle afterwards raises `ClosedDatabaseError` naming the call, rather than a
bare DuckDB `ConnectionException`:

<!-- docs-test: isolated -->
```python
from gffbase import ClosedDatabaseError

db.close()
try:
    db["gene1"]
except ClosedDatabaseError as exc:
    print(exc)
# query() on a closed FeatureDB: the connection was released by close().
# Open a new FeatureDB, or use `with FeatureDB(path) as db:` to scope it.
```

### What happens if you don't

Nothing, until it matters — and then:

- **another process cannot open the file for writing**, which is what makes a
  worker pool fail;
- **on Windows the file cannot be deleted or replaced**, so a pipeline that
  rebuilds an annotation fails at the rebuild step;
- the connection is released whenever the object is garbage collected, which is
  not a time you control.

### Whose connection is it?

`close()` closes the connection **only when GFFBase opened it**:

<!-- docs-test: skip reason="illustrative: names gencode.duckdb, which the reader supplies" -->
```python
import duckdb

con = duckdb.connect("gencode.duckdb")
db = FeatureDB(con)
db.close()
con.execute("SELECT 1")     # still fine — you own `con`
```

`create_db()` opens its own connection, so the handle it returns does own it and
`with create_db(...)` releases it properly.

---

## Reading from many processes

This is the reason `read_only` exists.

<!-- docs-test: skip reason="illustrative: names gencode.duckdb, which the reader supplies" -->
```python
db = FeatureDB("gencode.duckdb", read_only=True)
```

A read-only connection does not take the exclusive lock, so **any number of
processes can hold one on the same file at once**. That is the shape of a
PyTorch `DataLoader` with `num_workers > 1`, and of any `multiprocessing.Pool`
fan-out over a shared annotation.

<!-- docs-test: skip reason="illustrative: names gencode.duckdb, which the reader supplies" -->
```python
import multiprocessing as mp

DB = "gencode.duckdb"

def exons_for(transcript_ids):
    # One handle per worker. Opening in the worker, not the parent, keeps the
    # connection out of the fork and avoids sharing a cursor across processes.
    with FeatureDB(DB, read_only=True) as db:
        table = db.children_batched(transcript_ids, featuretype="exon", format="arrow")
        return table.num_rows

if __name__ == "__main__":
    with FeatureDB(DB, read_only=True) as db:
        ids = [f.id for f in db.features_of_type("mRNA")]
    chunks = [ids[i::8] for i in range(8)]

    with mp.Pool(8) as pool:
        print(sum(pool.map(exons_for, chunks)))
```

!!! important "Open the handle inside the worker"
    Do not create a `FeatureDB` in the parent and rely on `fork` to share it. A
    DuckDB connection is not fork-safe; each worker should open its own.

### What read-only refuses

Every mutator raises `ReadOnlyError`:

`update()` · `delete()` · `add_relation()` · `add_relations()` · `analyze()`

Two deliberate exceptions:

- **`set_pragmas()` is allowed.** `SET` is session state, not a write to the
  file. Blocking it would be a false promise of safety and would break the
  `pragmas=` constructor argument that ported `gffutils` code passes routinely.
- **`execute()` is not guarded for writability.** It is the raw-SQL escape
  hatch; DuckDB's own refusal names the statement it rejected, which is more
  use than a generic message from us.

### Schema upgrades are disabled

Opening a schema-v1 database normally migrates it in place. Migration is DDL,
so under `read_only=True` the `upgrade="auto"` default is coerced to
`"never"` and the database opens in v1 compatibility mode instead. Migrate it
once, up front, with a writable handle:

```bash
gffbase migrate old.duckdb
```

---

## One handle, one thread

A `FeatureDB` is **not** safe to share across threads. A DuckDB connection
holds one result set at a time, so a query issued on one thread while another
is still streaming will truncate the first — silently, with no error.

Give each thread its own handle, or serialize access yourself.

The same constraint applies within a single thread when you interleave a
mutation into an open iteration:

<!-- docs-test: skip reason="deliberately demonstrates the wrong pattern" -->
```python
for feature in db.all_features():     # streaming
    db.delete([feature.id])           # DON'T — invalidates the stream
```

Materialize first:

<!-- docs-test: isolated -->
```python
for feature in list(db.all_features()):
    db.delete([feature.id])
```

GFFBase already keeps a second internal cursor for segment prefetching, which
is why iterating discontinuous features works; that cursor is closed by
`close()` regardless of who owns the connection.

---

## In-memory databases

<!-- docs-test: skip reason="illustrative: names a file the reader supplies" -->
```python
db = create_db("demo.gff3", ":memory:")
```

No file, no lock, nothing to clean up — and nothing persists. Useful for tests
and for one-shot transformations. Note that `:memory:` cannot be shared between
processes at all.
