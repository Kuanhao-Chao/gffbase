.. _roadmap--roadmap:

Roadmap
=======

The 0.2.0rc1 candidate is feature-frozen while correctness, reproducibility, and
release evidence are hardened. The items below are follow-up work, not promises
silently added to the release candidate.

.. _roadmap--priority-1-memory-and-trust:

Priority 1: memory and trust
----------------------------

- **Chunked native parsing and Arrow streaming.** Replace whole-input native
  materialization with bounded decompression and record batches. Acceptance:
  output remains parser-equivalent and peak RSS grows with batch size rather
  than corpus size.

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
