.. _methodology--benchmark-methodology:

Benchmark methodology
=====================

Every number published on the :doc:`Performance <performance>` page comes from
one run of one harness, and the file it comes from is committed to the
repository. This page describes how those numbers are produced and what they
do and do not claim.

----

.. _methodology--reproducing-a-run:

Reproducing a run
-----------------

.. code-block:: bash

   pip install -e ".[bench,all]"

   python benchmarks/download_corpora.py                # ~257 MB, five corpora
   python benchmarks/06_mega.py --legacy-timeout 5400 --publish

   # then regenerate the published tables from the measurements
   python tools/gen_benchmark_tables.py --write

``--publish`` writes ``benchmarks/results/06_mega.<platform>.json`` and refuses
a file that mixes runs: every corpus must have been measured by that same
invocation, on a clean tree, and the result must satisfy the schema-v3 evidence
contract. It never writes ``benchmarks/results/06_mega.json`` — that name holds
the preserved macOS artifact, whose bytes are pinned by three separate digests.

``tools/gen_benchmark_tables.py --check`` verifies that every published table
still matches the committed measurements, and is asserted by
``tests/test_release_hygiene.py`` — so a table cannot drift from the numbers
behind it without a test failing.

----

.. _methodology--what-is-measured:

What is measured
----------------

.. list-table::
   :header-rows: 1
   :widths: 50 50

   * - Metric
     - How
   * - **Ingest wall**
     - ``create_db()`` in a fresh subprocess, wall clock around the call
   * - **Peak RSS**
     - parent polls the child's RSS (plus its children) every 50 ms
   * - **On-disk size**
     - recursive size of the finished database
   * - **Spatial qps**
     - 5 000 regions sampled from the corpus's own per-seqid spans
   * - **Batched extraction**
     - ``children_batched(..., format="arrow")`` over 5 000 anchors

Region sampling uses a fixed seed (``20260501``), recorded in the results file,
so two runs sample the same regions.

.. _methodology--corpora:

Corpora
-------

The five canonical human-genome annotations, downloaded from their primary
sources by ``benchmarks/download_corpora.py``: GENCODE v49 basic (**both** the
GTF and the GFF3 release of the same biology), RefSeq GRCh38.p14, MANE v1.5
(Ensembl IDs) and CHESS 3.1.3.

.. _methodology--fairness:

Fairness
--------

- Both engines get ``merge_strategy="create_unique"``. This is not cosmetic:
  under the default ``"error"`` both refuse RefSeq and MANE outright, and timing
  gffbase under one duplicate-ID policy against gffutils under another would
  compare two different workloads on exactly the axis that decides whether the
  run completes.

- The primary GENCODE GTF comparison disables parent inference for **both**
  engines, which makes the authored records directly comparable. Separate
  non-headline controls exercise default inference and a parent-stripped GTF;
  their results are never mixed into the primary namespace.

- Each engine runs in its own subprocess, so peak RSS is attributable and
  neither inherits the other's warm caches.

- Both write to the same filesystem.

.. _methodology--what-is-not-comparable:

What is *not* comparable
------------------------

- **Spatial and batched columns are gffbase-only.** ``gffutils`` has no spatial
  index and no batched API, so there is nothing to put in the other column.
  The ``region()`` throughput comparison in the narrative sections is
  like-for-like; the per-corpus qps figures are not a head-to-head.

- **Query benchmarks run against a warm page cache**, immediately after the
  ingest that built the database. They measure steady-state query throughput,
  not cold-start.

----

.. _methodology--capped-runs-and-why-there-are-no-extrapolated-numbers:

Capped runs, and why there are no extrapolated numbers
------------------------------------------------------

The default-inference GENCODE v49 **GTF control** can exceed a practical run
window: GTF has no explicit parents, so every gene and transcript row must be
invented through the legacy Python/SQLite path. Every comparator arm therefore
has a ``--legacy-timeout`` safety valve (default 90 minutes), including arms that
normally finish well below it.

**A capped run is censored, not measured.** In a current result its state is
``timed_out``, ``cap_seconds`` records the safety valve, and ``wall_seconds`` is
``null``. It produces neither a speedup nor a speedup floor. Tables render only
``censored at 90 min`` in the comparator column.

The preserved 2026-08-15 Mac artifact predates this contract and uses schema
v2 names such as ``wall_seconds_lower_bound``. Its bytes remain historical
evidence, but the renderer treats those fields only as censoring metadata and
never repeats the old ``> N×`` claim.

This replaced a hardcoded ``wall_seconds = timeout × 2.0``. That factor had no
measurement behind it, and it was the sole source of the previously published
"≥ 2 hr 30 min" legacy wall and "≥ 32×" headline. A number produced by
multiplying a timeout is not a result, and printing one beside real
measurements invites a reader to distrust all of them.

The table generator refuses to render a row whose legacy run timed out but
still carries a wall time, so the defect cannot come back quietly.

----

.. _methodology--uncertainty:

Uncertainty
-----------

The headline sweep is **n = 1 per cell**: legacy ingest of the large corpora
takes hours, and repeating the whole sweep three times is not a good use of a
day. Run-to-run spread is instead measured on the two cheap corpora, which
share the same code paths:

.. code-block:: bash

   python benchmarks/06_mega.py --repeats 5 --only mane --only chess

``--repeats`` applies to the two cheap in-process measurements — the spatial
sweep and the batched extraction — and **not** to the ingest walls, where a
single legacy GENCODE run already costs over an hour. When it is greater than
1, the first pass is discarded as a warm-up: a smoke test measured a 6.7×
max/min ratio that was entirely cold page cache, and a spread that is really a
cold-start artifact is worse than no spread, because it gets published as
measurement uncertainty. After the warm-up the same measurement spreads under
5%.

Treat that spread as the measurement uncertainty for every cell. Where a
result file reports ``n = 1`` it carries a ``value`` key and deliberately **no**
``median``, so a renderer cannot present a single sample as a central tendency.

.. _methodology--equal-work-or-no-ratio:

Equal work, or no ratio
~~~~~~~~~~~~~~~~~~~~~~~

A speedup is recorded only when both completed databases have the same strict
``database-signature-v3``. The signature covers every logical segment,
canonicalized per-segment attributes (including empty flags and value order),
direct relationships, minimum-depth closure, feature counts, and the
feature-type histogram. Feature counts remain a useful diagnostic but are not
accepted as a correctness proof by themselves.

Attribute **key** order is deliberately canonicalized lexically because GFF/GTF
key order is non-semantic and engines may expose inferred attributes
differently. Value order is retained within each key, so reordering values
changes the signature while reordering keys does not.

Two further normalizations exist because the comparison must not mistake a
difference in *representation* for a difference in *content*. Both were found
by whole-genome runs, and each had cost a corpus its published speedup.

**The closure is derived, not read.** gffutils stores relationships in a
``relations`` table, but what it records at ``level = 2`` is not a transitive
closure: it inserts, for each feature, the children of its children — one hop,
at a fixed level, with no iteration to a fixed point. On a four-deep chain it
records five ancestor–descendant pairs and omits the sixth. gffbase stores the
real closure, so comparing the two reported RefSeq as differing by 3,218 pairs
and GENCODE GFF3 by 108 — on hierarchies whose *direct* edges agreed exactly.
The closure is a function of those edges, so the signature now computes it from
them for the comparator rather than trusting the stored cache.

**A comma followed by a space is not a separator.** GFF3 says an unescaped
comma separates values and that a literal comma must be percent-encoded.
gffbase follows that; gffutils deliberately does not, keeping ``, `` inside the
value so an unescaped ``description=kinase, subunit 1`` survives as one value.
Ten CHESS genes record two names as ``gene_name=ADAM6, RPS8P1``, which reported
that corpus as a 20-row divergence. The signature therefore applies one rule to
both engines, and it is the comparator's coarser one: re-splitting on every
comma would shatter 2,294 correctly escaped CHESS descriptions in order to line
up twenty gene names. The parse-policy difference itself is a declared API
deviation, recorded in ``tests/parity/deviations.toml`` and pinned by a
differential test — it belongs there, not hidden inside a benchmark digest.

----

.. _methodology--what-the-ingest-numbers-do-and-do-not-say:

What the ingest numbers do and do not say
-----------------------------------------

Two properties of the ingest measurement bound how much weight a single figure
carries, and both were measured rather than assumed.

**Ingest is attribute-bound, and essentially serial.** Cost tracks the number of
attributes, not the number of features: gffbase moves roughly 160,000 attributes
per second across every corpus, so a GENCODE annotation at 16-18 attributes per
feature ingests at about half the *feature* rate of CHESS at 2.6. Raising the
DuckDB thread count barely helps. From the 25-job thread sweep of one cluster
campaign run, over five corpora at 1, 2, 4, 8 and 10 threads -- all twenty-five
figures from that single run, since thread scaling is only meaningful within
one:

.. list-table::
   :header-rows: 1

   * - corpus
     - 1 thread
     - 10 threads
     - speedup
   * - MANE v1.5
     - 41.6 s
     - 36.0 s
     - 1.16x
   * - CHESS 3.1.3
     - 99.1 s
     - 94.6 s
     - 1.05x
   * - RefSeq GRCh38.p14
     - 425.4 s
     - 340.9 s
     - 1.25x
   * - GENCODE v49 (GTF)
     - 614.4 s
     - 498.3 s
     - 1.23x
   * - GENCODE v49 (GFF3)
     - 657.2 s
     - 534.8 s
     - 1.23x

Ten times the cores buys at most a quarter more throughput. The comparator is
single-threaded, so the published comparison is largely one serial ingest
against another, and neither engine's figure should be read as a parallel
result.

A second, partial sweep measured the same corpora 12-15 percent slower at every
thread count -- 47.4 s rather than 41.6 s for MANE at one thread, 506.8 s rather
than 425.4 s for RefSeq -- while reproducing the same shape. Absolute figures
from a thread sweep are therefore run-specific; the conclusion that ingest does
not parallelise is not.

**Ingest uncertainty is asymmetric, and it falls with corpus size.** Repeating
the same corpus with a byte-identical binary on ten pinned physical cores of an
otherwise idle machine:

.. list-table::
   :header-rows: 1

   * - corpus
     - repeated gffbase ingest
     - spread
   * - CHESS 3.1.3
     - 93.9 s – 113.1 s
     - 20%
   * - RefSeq GRCh38.p14
     - 419.5 s – 424.8 s
     - 1.3%
   * - GENCODE v49 (GTF)
     - 606.1 s – 622.2 s
     - 2.7%

The large corpora are reproducible to within a few percent; the small, fast one
is not, because fixed startup and page-cache effects are a large fraction of a
ninety-second run. ``gffutils`` on CHESS spanned 134.2 s to 137.0 s over the
same period -- 2 percent -- so the sensitivity is specific to the parallel,
allocation-heavy engine rather than to the machine. Hyperthreading,
temporary-directory placement and machine load were each ruled out as the cause.

Read the published ratios accordingly: the whole-genome figures are solid, and
the CHESS ratio is the one carrying real uncertainty.

The harness measures each ingest once, while the query phases run five times and
report their spread. Closing that asymmetry is future work; until it is closed, a
published ingest ratio near 1.0 should be read as "comparable", not as a ranking.

----

.. _methodology--publishing-a-run:

Publishing a run
----------------

A sweep writes to ``benchmarks/out/`` (gitignored). The committed file the
published tables are generated from is ``benchmarks/results/06_mega.json``, and
copying between them used to be an undocumented manual step — so a fresh run
could sit on disk while the docs kept rendering the previous measurement.

.. code-block:: bash

   python benchmarks/06_mega.py --legacy-timeout 5400 --publish
   python tools/gen_benchmark_tables.py --write

``--publish`` copies the finished run into place; the generator then rewrites
every table from it. ``tests/test_release_hygiene.py`` fails if a table stops
matching the numbers behind it.

----

.. _methodology--provenance:

Provenance
----------

Every results file embeds the environment it was produced in — CPU model, core
count, RAM, OS, Python, DuckDB, PyArrow, ``gffutils`` and ``gffbase`` versions,
``rustc``, the git commit and whether the working tree was dirty:

.. docs-test: skip reason="reads a results file produced by a benchmark run"

.. code-block:: python

   import json
   env = json.load(open("benchmarks/results/06_mega.json"))["environment"]

None of this was recorded before 0.2.0. The published numbers carried their
hardware and versions only as hand-typed prose, which said "gffbase 0.1.0"
throughout the 0.2.0 development cycle — so nothing in the repository could
have detected a regression.

----

.. _methodology--disk:

Disk
----

A full sweep is disk-bound before it is CPU-bound: the five corpus pairs total
roughly 38 GiB. Each pair is purged as soon as its numbers are recorded, which
holds the peak to about 16 GiB. ``--keep-db KEY`` retains one for later stages;
``--no-purge`` keeps everything and needs the full 38 GiB. Set
``GFFBASE_BENCH_OUT`` to run against another volume. The harness checks free
space before each corpus and refuses to start one it cannot finish.
