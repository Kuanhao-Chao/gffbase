---
title: Architecture
---

# Architecture

This page is the maintainer map for GFFBase 0.2. It describes which layer owns
each invariant and where a change must be tested. The public API is Python, the
parser has native and fallback implementations, and DuckDB is the durable
source of truth after ingest.

## End-to-end data flow

```text
GFF3/GTF path or bytes
        │
        ▼
parser.py ── native PyO3 parser or pure-Python fallback
        │       ParsedFeature + directives + dialect + diagnostics
        ▼
ingest.py ── bounded Python/Arrow batches
        │
        ├── raw rows and attributes
        ├── duplicate / multipart resolution
        ├── optional GTF parent synthesis
        ├── direct edges and transitive closure
        └── R-tree when spatial is available, B-tree otherwise
        ▼
DuckDB schema v2
        │
        ▼
FeatureDB ── scalar iterators, batched Arrow/data-frame APIs,
             mutation, validation, migration, and export
```

The native parser currently reads the complete plain or gzip stream before it
returns records. Arrow batching bounds the Python-to-DuckDB handoff, but it does
not make native input decoding streaming. Peak input memory therefore remains a
known limitation and is measured independently from database memory.

## Layer responsibilities

### Parsing

`parser.py` selects `gffbase._native` when its compiled version matches the
Python package and otherwise uses `_pyfallback`. Both engines must return the
same records, warnings, directives, and failures for the same profile.

`validation="gffutils"` is the compatibility profile used by
`create_db(mode="compat")`. It preserves permissive legacy input handling and
attaches diagnostics. `validation="ncbi"` is the formal profile: malformed
rows fail at the source line rather than being repaired during ingest.

### Ingest and identity

`ingest.py` is the transaction boundary. It writes Arrow batches, resolves raw
identifiers, materializes multipart features, derives relationships, and only
commits after final indexes and metadata exist. A failure must leave neither a
partially usable database nor an orphan temporary file.

The stored `id` is a unique logical key. `raw_id` records the identifier found
in the annotation before collision handling. Physical pieces of a
discontinuous feature live in `segments`; the feature row stores the envelope.
Synthetic GTF parents use the same identity and provenance machinery as input
rows. A raw parent identifier spanning incompatible sequence/strand groups is
an error unless the caller explicitly selects deterministic unique creation.

### Schema and relationships

Schema v2 has one logical feature row, normalized attributes, direct edges,
materialized transitive closure, sparse multipart segments, provenance tables,
and compatibility views. Direct edges are authoritative. Closure is a query
acceleration structure and the full validator recomputes it from edges in both
directions.

Relationship construction order is fixed:

```text
load raw rows → resolve IDs/multipart rows → synthesize GTF parents
→ create direct edges → compute closure → build indexes → commit metadata
```

Changing that order requires migration, mutation, round-trip, and validator
tests because IDs embedded in an earlier structure cannot safely be repaired
afterward.

### Query routing

`FeatureDB` owns connection lifecycle and query routing. Region queries use the
R-tree only when the spatial extension and index were successfully created;
otherwise they use the multi-column B-tree. Multipart envelope candidates are
rechecked against physical segments. Relationship queries use closure when it
is valid and a recursive CTE where dynamic traversal is required.

The batched APIs execute one set-based query and return Arrow by default;
Pandas and Polars conversions are optional. A loop of scalar queries is not a
substitute for a batched performance measurement.

### Validation, migration, and export

Fast validation checks inexpensive structural invariants. Full validation adds
closure equivalence and checks that need recursive or sampled content work.
Every issue has a stable machine-readable invariant identifier and severity.

Migration changes physical schema only and remains atomic. SQLite export and
text serialization consume the logical/physical views rather than inventing a
second interpretation of multipart or synthetic features.

## Correctness boundaries

- Parser equivalence is checked at records, diagnostics, and exceptions—not
  merely at row counts.
- Ingest correctness is checked with deterministic feature, relationship, and
  feature-type signatures.
- R-tree/B-tree and scalar/batched routes must agree on IDs and ordering
  guarantees.
- Direct edges, closure, normalized attributes, and synthetic parents must not
  disagree, even if normal API queries still appear to work.
- Benchmarks run installed release wheels. Importing an ignored in-tree native
  artifact invalidates a result.

See [Testing](../testing.md) for the gates around these boundaries and
[Benchmark methodology](../performance/methodology.md) for measurement rules.

