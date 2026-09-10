.. _performance--performance:

Performance
===========

Historical head-to-head measurements against legacy
`gffutils <https://github.com/daler/gffutils>`__. The retained Mac run covers
four canonical human-genome annotations; the Linux five-corpus campaign is a
separate result set.

Every number below is **generated from the committed historical Mac file**
(``benchmarks/results/06_mega.json``) by ``tools/gen_benchmark_tables.py``. A test
in the release-hygiene suite fails if a published table stops matching the data
behind it, so these cannot drift from what was actually measured. How the
measurements are taken — and what they do and do not claim — is on the
:doc:`Methodology <methodology>` page.

.. BEGIN GENERATED: benchmark-provenance

| **Measured on** AMD EPYC 7702 64-Core Processor · 128 cores · 1007.22 GB RAM · Linux-5.14.0-503.15.1.el9_5.x86_64-x86_64-with-glibc2.34
| **Versions:** Python 3.11.16 · gffbase 0.2.0rc1 · duckdb 1.5.5 · pyarrow 25.0.1 · gffutils 0.14
| **Commit:** ``42bb900e328c`` · **Run:** 2026-09-10T19:56:52Z
| *Generated from benchmarks/results/06_mega.linux-x86_64.json by tools/gen_benchmark_tables.py. Do not edit by hand.*

.. END GENERATED: benchmark-provenance

----

.. _performance--historical-mac-sweep:

Historical Mac sweep
--------------------

.. BEGIN GENERATED: corpus-table

.. list-table::
   :header-rows: 1
   :widths: 12 11 11 11 11 11 11 11 11

   * - Corpus
     - Format
     - Lines
     - gffbase ingest
     - legacy ingest
     - speedup
     - peak RSS (ingest + full validation)
     - spatial qps
     - batched (5 k anchors)
   * - **GENCODE v49** (basic)
     - GTF
     - 6,068,892
     - **10 min 14 s**
     - 7 min 1 s
     - **0.69×**
     - 53.57 GB
     - **707** ±0% (n=5)
     - 963 ms / 1.93 M desc
   * - **GENCODE v49** (basic)
     - GFF3
     - 6,066,054
     - **11 min 8 s**
     - 9 min 59 s
     - **0.90×**
     - 62.00 GB
     - **705** ±0% (n=5)
     - 1096 ms / 1.93 M desc
   * - **RefSeq GRCh38.p14**
     - GFF3
     - 4,932,571
     - **7 min 2 s**
     - 6 min 34 s
     - **0.93×**
     - 26.80 GB
     - **540** ±0% (n=5)
     - 588 ms / 999 k desc
   * - **CHESS 3.1.3**
     - GFF3
     - 2,761,061
     - **1 min 53 s**
     - 2 min 17 s
     - **1.21×**
     - 3.38 GB
     - **702** ±0% (n=5)
     - 202 ms / 161 k desc
   * - **MANE v1.5** (Ensembl)
     - GFF3
     - 524,834
     - **40.0 s**
     - 45.7 s
     - **1.14×**
     - 3.98 GB
     - **840** ±0% (n=5)
     - 206 ms / 156 k desc

.. END GENERATED: corpus-table

“Censored at” means that comparator was **killed at the safety valve without
finishing**. The cap is shown as censoring evidence; no wall or speedup is
derived from it. See
:doc:`Capped runs <methodology>`.

.. note::

   **GENCODE v49 GFF3 is missing from this run**

   The sweep measures four of the five :doc:`corpora <datasets>`. GENCODE's
   GFF3 edition needs ~14 GiB free to hold both databases at once, and the
   run machine had 13.2 GiB, so the harness **refused to start it** rather
   than fail partway through. It is a gap in coverage, not a result: nothing
   here is inferred from the missing row, and the previous measurement for it
   was discarded rather than carried forward, because it came from a
   contaminated run on a different commit.

   Reproduce it on a machine with the headroom:

   .. code-block:: bash

      python benchmarks/06_mega.py --only gencode-gff3 --publish
      python tools/gen_benchmark_tables.py --write

----

.. _performance--controlled-gtf-inference-and-synthesis:

Controlled GTF inference and synthesis
--------------------------------------

GENCODE v49 ships related GTF and GFF3 annotations, but the GTF is not
leaf-only: it contains explicit gene and transcript rows. Default gffutils
inference may therefore perform redundant work. The historical table retained
that default and cannot establish that synthesis caused the observed gap.

The Linux campaign corrects the design with three separately reported arms:

1. Unmodified GENCODE GTF with each engine's default compatibility behavior.
2. The same bytes with gene and transcript inference disabled in both engines;
   this is the recommended real-data headline.

3. A generated parent-stripped GTF with inference enabled in both engines;
   its source hash, transformation recipe, removed-row counts, and output hash
   make this the controlled synthesis workload.

Only the third arm supports conclusions about synthesis. No speedup is shown
for any arm unless normalized feature and relationship signatures agree.

----

.. _performance--bulk-extraction-for-ml:

Bulk extraction for ML
----------------------

The workload GFFBase exists for: pull every exon for a large set of
transcripts, hand the columns to a tensor, train.

.. docs-test: skip reason="illustrative: an id list the reader supplies"

.. code-block:: python

   exons = db.children_batched(transcript_ids, featuretype="exon", format="arrow")

One set-based SQL query returning a ``pyarrow.Table`` that shares memory with
DuckDB's buffers. **No Python** ``Feature`` **object is constructed at any layer** —
which is the whole difference, because constructing millions of them is what
dominates the row-by-row path in both libraries.

The per-corpus batched column in the table above shows this at 5 000 anchors.

.. warning::

   **The row-by-row loop is the wrong tool here**

   ``for i in ids: db.children(i)`` is **slower in GFFBase than in** ``gffutils``.
   DuckDB pays vectorization startup on every call; SQLite, an OLTP engine,
   does not. This is a real and inherent trade, not a defect — and it is why
   the batched API exists. The :doc:`Migration guide <migration>` covers it in
   the one place a ported script is likely to hit it.

----

.. _performance--the-trade-offs:

The trade-offs
--------------

Being fast at whole-corpus work costs something at the other end, and it is
worth being explicit about what:

The two costs that can be measured are, so they are generated from the same
run as the speed numbers rather than retyped:

.. BEGIN GENERATED: tradeoffs-table

.. list-table::
   :header-rows: 1
   :widths: 25 25 25 25

   * - 
     - gffbase
     - legacy ``gffutils``
     - ratio
   * - **Peak RSS** (gffbase: ingest + full validation)
     - 3.38 GB – 62.00 GB
     - 110.50 MB – 193.74 MB
     - not comparable
   * - **On-disk database**
     - 610.76 MB – 7.14 GB
     - 472.90 MB – 6.05 GB
     - 1.18–1.36×

| *Measured across 5 corpora; the disk ratio is gffbase ÷ legacy. The two RSS figures are not a ratio: gffbase's is the peak of a process that ingests and then validates exhaustively, the comparator's of one that only ingests. Ingest alone peaks far lower, and validate_db defaults to sample=200.*

.. END GENERATED: tradeoffs-table

The rest of the ledger is qualitative, and stays that way:

.. list-table::
   :header-rows: 1
   :widths: 34 33 33

   * - 
     - gffbase
     - legacy ``gffutils``
   * - **Ingest wall**
     - faster in each completed, comparable corpus
     - —
   * - **Single-feature point query**
     - comparable
     - comparable
   * - **Row-by-row loop over many IDs**
     - **slower**
     - faster
   * - **Bulk batched extraction**
     - one query, zero ``Feature`` objects
     - not available
   * - **Spatial index**
     - R-tree, or B-tree fallback
     - none

The memory and disk costs buy the speed: an Arrow batch builder that stages
columns before writing, a materialized transitive closure so hierarchy walks
are indexed lookups rather than recursion, and a long-form attributes table so
attribute search does not scan.

----

.. _performance--reproducing-this:

Reproducing this
----------------

.. code-block:: bash

   pip install -e ".[bench,all]"
   python benchmarks/download_corpora.py
   python benchmarks/06_mega.py --legacy-timeout 5400 --keep-db gencode-gff3

Full detail, including the fairness constraints and the repeat policy, on the
:doc:`Methodology <methodology>` page.
