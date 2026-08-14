# API Reference

GFFBase's public surface is intentionally compatible with legacy
`gffutils`. Pages in this section are auto-generated from the live
docstrings via
[mkdocstrings](https://mkdocstrings.github.io/python/).

| Module | What's there |
|---|---|
| [`FeatureDB`](featuredb.md) | The query database. The row-by-row legacy methods, the **vectorized** `children_batched` / `parents_batched` / `region_batched` (with `explode_segments=` for physical lines), the derived-feature methods, `attribute_search`, and `mode` / `derived_source`. |
| [`Feature`](feature.md) | The user-facing record type. Backwards-compatible with `gffutils.Feature`. |
| [`create_db`](create_db.md) | The ingestion entrypoint. |
| [`DataIterator` & `GFFWriter`](io.md) | Streaming I/O. |
| [`merge_criteria`](merge_criteria.md) | Predicates consumed by `FeatureDB.merge`. |
| [`Exceptions`](exceptions.md) | `GFFFormatError`, `FeatureNotFoundError`, `DuplicateIDError`, `AttributeStringError`, `EmptyInputError`, `SchemaVersionError`, `MultipartConstraintError`. |
| [Multipart features](multipart.md) | `MultipartFeature`, `FeatureSegment` — several GFF3 lines sharing one `ID` as a single logical feature. |
| [`validate`](validate.md) | 15 post-ingest invariants; `db.validate()` and `gffbase validate`. |
| [`migrate`](migrate.md) | Schema v1 → v2 upgrade, and the opt-in multipart coalesce. |
| [Compatibility modules](compat.md) | `bins`, `helpers`, `constants`, `attributes`, `convert`, `create`, `inspect`, `version`, and the optional integrations. |

If a method name is missing here it lives in the source under
`python/gffbase/`; cross-reference `MIGRATION.md` for the
legacy-equivalence table.
