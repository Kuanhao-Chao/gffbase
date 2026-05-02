# API Reference

GFFBase's public surface is intentionally compatible with legacy
`gffutils`. Pages in this section are auto-generated from the live
docstrings via
[mkdocstrings](https://mkdocstrings.github.io/python/).

| Module | What's there |
|---|---|
| [`FeatureDB`](featuredb.md) | The query database. Includes the row-by-row legacy methods and the **vectorized** `children_batched`, `parents_batched`, `region_batched` introduced in Phase 12. |
| [`Feature`](feature.md) | The user-facing record type. Backwards-compatible with `gffutils.Feature`. |
| [`create_db`](create_db.md) | The ingestion entrypoint. |
| [`DataIterator` & `GFFWriter`](io.md) | Streaming I/O. |
| [`merge_criteria`](merge_criteria.md) | Predicates consumed by `FeatureDB.merge`. |
| [`Exceptions`](exceptions.md) | `FeatureNotFoundError`, `DuplicateIDError`, `AttributeStringError`, `EmptyInputError`. |

If a method name is missing here it lives in the source under
`python/gffbase/`; cross-reference `MIGRATION.md` for the
legacy-equivalence table.
