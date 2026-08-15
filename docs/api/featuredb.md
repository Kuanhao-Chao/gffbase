# `FeatureDB`

The query database. All methods below are rendered from the live docstrings.

!!! important "Close it, or use `with`"
    DuckDB takes an **exclusive lock** on the database file for the life of a
    writable handle. Use a `with` block, or call `close()`:

    ```python
    with FeatureDB("gencode.duckdb") as db:
        ...

    db = FeatureDB("gencode.duckdb", read_only=True)   # shareable by N processes
    ```

    `read_only=True` is what lets several worker processes read one annotation
    database at once. See
    [Connections & concurrency](../guides/connections.md).

!!! tip "Use the `_batched` methods for bulk work"
    `children()`, `parents()` and `region()` return `Feature` objects one at a
    time — right for exploring, wrong for feeding a model. `children_batched()`,
    `parents_batched()` and `region_batched()` answer the same question for
    thousands of anchors in a single query, returning Arrow / pandas / polars
    without constructing any `Feature` objects.

::: gffbase.interface.FeatureDB
    options:
      show_root_heading: true
      members_order: source
      show_signature_annotations: true
      separate_signature: true
      filters:
        - "!^_"
