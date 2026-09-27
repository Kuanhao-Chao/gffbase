.. _roadmap--roadmap:

Roadmap
=======

0.3.0 rebuilt ingest around a streaming Rust producer, rewrote relation queries
around what DuckDB can index, and prefetches loops over a live stream (see the
:doc:`changelog`). The items below are follow-up work for later releases, not
promises about any particular one.

.. _roadmap--priority-1-memory-and-trust:

Priority 1: speed, memory and trust
-----------------------------------

- **Parallel parsing.** Parsing and per-record work is about a third of ingest
  time now and runs on one core; appending to DuckDB is most of the rest. Acceptance: the parse stage scales with cores and
  the correctness signature is unchanged.

- **Scattered point lookups.** A ``children()`` or ``db[id]`` call on an id
  that no open iterator holds is one DuckDB statement -- 0.5-1.5 ms, against
  SQLite's few hundredths. Loops over iterators are prefetched and close to
  gffutils; these calls are not. Acceptance: a measured per-call improvement
  in ``benchmarks/08_loops.py`` with every answer digest unchanged.

- **Peak memory under 2 GB on every whole-genome corpus.** GENCODE peaks at
  2.6-2.8 GiB, in the index stage. Acceptance: peak RSS in the benchmark
  harness, with no out-of-memory retry path left untested.

- **A reproducible spatial benchmark.** ``06_mega.py`` samples its regions from
  a per-seqid span list whose order DuckDB does not fix, so the seeded sample
  differs between runs and spatial queries per second compare only within one
  run. Acceptance: the list is ordered before sampling, and every canonical row
  is re-measured with it.

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
