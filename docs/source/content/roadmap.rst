.. _roadmap--roadmap:

Roadmap
=======

0.2.0 is released. The items below are follow-up work for later releases, not
promises about any particular one.

.. _roadmap--priority-1-memory-and-trust:

Priority 1: memory and trust
----------------------------

- **Chunked native parsing and Arrow streaming.** Replace whole-input native
  materialization with bounded decompression and record batches. Acceptance:
  output remains parser-equivalent and peak RSS grows with batch size rather
  than corpus size.

- **Parallel ingest.** Ingest is essentially serial today: measured across five
  corpora, raising DuckDB threads from 1 to 10 buys between 1.05x and 1.25x, so
  the ``threads`` setting cannot make ingest much faster whatever it is set to.
  The cost is attribute-bound: 157,000 to 168,000 attributes per second on the
  three corpora dense enough for attributes to dominate, falling to 62,400 on
  CHESS at 2.6 attributes per feature, where the per-feature floor is what is
  left to pay. Acceptance: the
  thread sweep in the benchmark harness shows the ingest scaling with cores, and
  the correctness signature is unchanged.

- **Persistent source provenance.** Store source checksum, size, parser mode,
  and build identity in database metadata. Acceptance: a database can explain
  exactly which bytes and implementation created it.

- **Machine-readable validation.** Add stable JSON output to the Python and CLI
  interfaces, including invariant IDs, severity, counts, and bounded examples.

.. _roadmap--priority-2-set-based-workflows:

Priority 2: set-based workflows
-------------------------------

- **Bulk interval joins.** Match two region collections without Python scalar
  loops, with Arrow/lazy output and R-tree/B-tree parity.

- **Streaming query results.** Expose bounded Arrow record batches for queries
  too large to materialize as one table.

- **Transactional multi-file append.** Add source namespaces, collision policy,
  rollback, and explicit index/closure rebuild behavior.

- **Parquet and Arrow dataset export.** Preserve logical features, physical
  segments, attributes, relations, and provenance in documented schemas.

.. _roadmap--priority-3-input-and-observability:

Priority 3: input and observability
-----------------------------------

- Remote, stdin, and indexed BGZF ingestion with checksums and retry-safe
  temporary files.

- Sequence Ontology and FASTA cross-validation beyond structural database
  invariants.

- Query-plan diagnostics and documented patterns for concurrent read-only
  services.

Each feature begins with an executable correctness oracle and a benchmark that
identifies the workload it improves. Schema-changing work also requires a
forward migration and an export/re-import compatibility test.
