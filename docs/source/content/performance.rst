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

| **Measured on** Apple M1 Pro · 10 cores · 16.00 GB RAM · macOS-26.3-arm64-arm-64bit-Mach-O
| **Versions:** Python 3.13.5 · gffbase 0.2.0 · duckdb 1.5.2 · pyarrow 19.0.0 · gffutils 0.13
| **Commit:** ``1d52bf6738e0`` · **Run:** 2026-08-15T22:56:50Z
| *Generated from benchmarks/results/06_mega.json by tools/gen_benchmark_tables.py. Do not edit by hand.*

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
     - peak RSS
     - spatial qps
     - batched (5 k anchors)
   * - **GENCODE v49** (basic)
     - GTF
     - 6,068,892
     - **4 min 5 s**
     - censored at 1 hr 30 min
     - —
     - 5.62 GB
     - **1,457**
     - 522 ms / 1.93 M desc
   * - **RefSeq GRCh38.p14**
     - GFF3
     - 4,932,571
     - **3 min 1 s**
     - 3 min 37 s
     - **1.20×**
     - 4.73 GB
     - **1,188**
     - 352 ms / 999 k desc
   * - **CHESS 3.1.3**
     - GFF3
     - 2,761,061
     - **48.4 s**
     - 1 min 9 s
     - **1.43×**
     - 2.43 GB
     - **1,893**
     - 96 ms / 161 k desc
   * - **MANE v1.5** (Ensembl)
     - GFF3
     - 524,834
     - **19.8 s**
     - 26.5 s
     - **1.34×**
     - 1.61 GB
     - **2,086**
     - 80 ms / 156 k desc

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
   * - **Peak ingest RSS**
     - 1.61 GB – 4.73 GB
     - 174.50 MB – 217.52 MB
     - 9.44–24.24×
   * - **On-disk database**
     - 610.51 MB – 4.95 GB
     - 472.90 MB – 3.79 GB
     - 1.29–1.36×

.. END GENERATED: tradeoffs-table

*Measured across 3 corpora; ratios are gffbase ÷ legacy.*

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
