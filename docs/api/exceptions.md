# Exceptions

Every exception GFFBase raises, and what it means.

Most of these subclass **`ValueError`** rather than `Exception`, which is a
deliberate departure from `gffutils`. Upstream exports `DuplicateIDError` and
documents it, but the code path that should raise it raises a bare
`ValueError("Duplicate ID …")` instead — so real callers in the wild write
`except ValueError`. Making the dedicated classes *also* `ValueError`
satisfies those callers and the documented type at once, instead of forcing a
choice between compatibility and correctness.

`FeatureNotFoundError` is deliberately **not** rebased: it comes out of
`__getitem__`, where callers reach for `KeyError`-shaped handling.

| Exception | Raised when |
| --- | --- |
| `GFFFormatError` | A line violates the GFF3 specification. Carries `line_no`, `kind` and `message`, so you get a pointer into the file rather than a stack trace. When the Rust extension is loaded this is the PyO3-defined class; `gffbase.GFFFormatError` is rebound at import so `isinstance` works either way. |
| `FeatureNotFoundError` | `db[feature_id]` and the id is not in the database. |
| `DuplicateIDError` | Two features resolve to the same primary key under `merge_strategy="error"` (the default). Usually the split-CDS convention — see [Modes](../guides/modes.md). |
| `SynthesisConflictError` | One inferred GTF gene/transcript identifier spans incompatible sequence or strand groups. It subclasses `DuplicateIDError`. The default rejects the ambiguous hierarchy; explicit `merge_strategy="create_unique"` creates deterministic per-group parents. |
| `AttributeStringError` | Column 9 is malformed beyond parsing. |
| `EmptyInputError` | The input file or iterable yielded no features. |
| `SchemaVersionError` | The database was written by a newer GFFBase, or its `meta.schema_version` is unintelligible, or it is a v1 database opened with `upgrade="error"`. An *older* readable version is not an error — it degrades to compatibility mode. |
| `MultipartConstraintError` | Under `mode="strict"`, lines sharing an `ID` disagree on seqid, source, featuretype or strand, so they cannot be one discontinuous feature. Pass `on_multipart_conflict="split"` to partition them instead. |
| `ReadOnlyError` | A write was attempted on a handle opened with `read_only=True`. See [Connections & concurrency](../guides/connections.md). |
| `ClosedDatabaseError` | A `FeatureDB` was used after `close()`. Raised in place of DuckDB's `ConnectionException`, which says a connection is closed without saying which object or which call. |
| `ValidationError` | Raised by `validate_db(..., raise_on_error=True)`. Subclasses `AssertionError`. |

::: gffbase.exceptions
    options:
      show_root_heading: false
      show_root_toc_entry: false
      members_order: source
      filters:
        - "!^_"
