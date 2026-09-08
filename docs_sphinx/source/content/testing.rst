.. _testing--testing-and-release-gates:

Testing and release gates
=========================

GFFBase separates quick developer feedback from compatibility, large-corpus,
and publication work. A bare ``pytest`` intentionally excludes the latter tiers.

.. _testing--test-tiers:

Test tiers
----------

.. list-table::
   :header-rows: 1
   :widths: 34 33 33

   * - Tier
     - Command
     - Purpose
   * - Default
     - ``pytest``
     - Deterministic unit, integration, regression, and bounded property tests
   * - B-tree
     - ``GFFBASE_TEST_DISABLE_RTREE=1 pytest``
     - Run the same behavior through the spatial fallback
   * - Parity
     - ``pytest -m parity``
     - Compare the public compatibility surface with the pinned gffutils oracle
   * - Slow
     - ``pytest -m slow``
     - Longer local cases excluded from normal iteration
   * - Corpus
     - ``pytest -m corpus``
     - Full real-annotation ingestion and structural validation
   * - Rust
     - ``cargo test --manifest-path rust/Cargo.toml --locked --release``
     - Native parser and Rust-level edge cases
   * - Docs
     - ``mkdocs build --strict``
     - API imports, links, generated content, and navigation

Use an explicit marker expression when combining tiers, for example
``pytest -m "slow or corpus"``. Corpus tests require the verified files in
``benchmarks/data/`` and should use a scratch directory on a volume with enough
space.

.. _testing--parser-and-property-testing:

Parser and property testing
---------------------------

The native and fallback parsers are independent implementations of one public
contract. Generated cases compare records, warnings, metadata, and failure
classes across file, gzip, and byte entry points. Round-trip properties cover
Unicode, quoting, reserved characters, and percent encoding. Test generation
is bounded in normal CI; a longer profile is available for scheduled or manual
hardening runs. Every minimized failure becomes a deterministic regression.

.. _testing--database-oracles:

Database oracles
----------------

Small generated DAGs provide an implementation-independent reference for
parents, children, closure depth, and mutation. Tests compare:

- closure results with recursive traversal of direct edges;
- scalar and batched APIs;
- R-tree and B-tree region results;
- state before and after update, delete, and relationship mutation;
- default conflict rejection with explicit multipart and unique-creation
  policies.

Mutation tests also corrupt internal tables deliberately. The validator must
detect missing, extra, and wrong-depth closure rows, orphan attributes, and
incoherent synthetic parents.

.. _testing--installation-modes:

Installation modes
------------------

Release confidence requires more than an editable checkout:

1. Build a clean wheel outside the source package directory.
2. Install it into an empty environment.
3. Verify the imported package and native-extension paths, versions, and wheel
   checksum.

4. Run parser sentinels and a small database round trip.
5. Separately exercise editable native and forced fallback installations.

A Python/native version mismatch is a hard failure. Do not repair it by adding
the repository's ``python/`` directory to ``PYTHONPATH``; rebuild the extension or
install the intended wheel.

.. _testing--ci-and-release-acceptance:

CI and release acceptance
-------------------------

CI covers supported operating systems and Python versions, R-tree/B-tree
routing, minimum and current dependencies, a clean-wheel smoke test, the
pinned parity oracle, Rust lint/tests, typing, formatting, and strict docs.
Coverage may not fall below the platform gate, but coverage alone is not
evidence of correctness: every new decision branch needs an assertion about
its output or failure.

Large-corpus and cluster benchmark runs are release evidence rather than normal
PR jobs. A tag-ready candidate requires their recorded input hashes, successful
full validation, matching correctness signatures, and a clean tracked tree.
