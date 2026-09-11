.. _performance--performance:

Performance
===========

Head-to-head measurements against legacy
`gffutils <https://github.com/daler/gffutils>`__ over the five canonical
human-genome annotations, from a single run on one machine, at one commit.

Every number below is **generated from a committed measurement file** —
``benchmarks/results/06_mega.linux-x86_64.json`` — by
``tools/gen_benchmark_tables.py``. A test in the release-hygiene suite fails if a
published table stops matching the data behind it, so these cannot drift from
what was actually measured. How the measurements are taken — and what they do
and do not claim — is on the :doc:`Methodology <methodology>` page.

.. important::

   **Read the ingest column as a draw, not a win.** Across the five corpora
   gffbase ranges from 1.21× to 0.69× against ``gffutils`` on ingest wall time:
   it is ahead where per-feature overhead dominates (CHESS, MANE) and behind on
   the attribute-dense whole-genome files. Both engines are attribute-bound and
   effectively serial, at a comparable rate. The durable advantages are
   elsewhere — batched extraction, spatial indexing, and SQL over the corpus —
   and those are unaffected by the ingest result. See
   :ref:`What the ingest numbers do and do not say
   <methodology--what-the-ingest-numbers-do-and-do-not-say>`.

.. BEGIN GENERATED: benchmark-provenance

| **Measured on** AMD EPYC 7702 64-Core Processor · 128 cores · 1007.22 GB RAM · Linux-5.14.0-503.15.1.el9_5.x86_64-x86_64-with-glibc2.34
| **Versions:** Python 3.11.16 · gffbase 0.2.0rc1 · duckdb 1.5.5 · pyarrow 25.0.1 · gffutils 0.14
| **Commit:** ``42bb900e328c`` · **Run:** 2026-09-10T19:56:52Z
| *Generated from benchmarks/results/06_mega.linux-x86_64.json by tools/gen_benchmark_tables.py. Do not edit by hand.*

.. END GENERATED: benchmark-provenance

----

.. _performance--the-five-corpus-run:

The five-corpus run
-------------------

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

All five :doc:`corpora <datasets>` completed; no row is censored, and every row
published a speedup only because the two engines' correctness signatures agreed
on it. Had a comparator been killed at its safety valve the cell would read
“censored at”, which is cap evidence and never a wall time — see
:doc:`Capped runs <methodology>`.

.. note::

   **What a single figure here is worth**

   Ingest wall time was measured once per corpus, not repeated — only the query
   phases use ``--repeats 5``. A separate probe of the same binary put the
   run-to-run spread at **1.3 % on RefSeq and 2.7 % on GENCODE GTF**, but at
   roughly **20 % on CHESS**, where a 90-second run is dominated by startup.
   Read the large-corpus ratios as reliable to a few percent and the CHESS
   ratio as indicative. The per-corpus figures and the conditions they were
   taken under are in :ref:`What the ingest numbers do and do not say
   <methodology--what-the-ingest-numbers-do-and-do-not-say>`.

   The ``peak RSS`` column is the peak of a process that ingests **and then
   validates exhaustively** (``validation_sample="all"``), which is why it
   reaches tens of GB. It is not the cost of ingest, and it is not what a user
   pays: ``validate_db`` defaults to ``sample=200`` and the CLI never overrides
   it. Measured at ``validation_sample=10000``, the same five corpora peak at
   1.3-10.2 GB instead of 3.3-62.2 GB -- GENCODE GFF3 alone falls from 62.2 GB
   to 9.5-10.2 GB.

----

.. _performance--controlled-gtf-inference-and-synthesis:

Controlled GTF inference and synthesis
--------------------------------------

GENCODE v49 ships related GTF and GFF3 annotations, but the GTF is not
leaf-only: it contains explicit gene and transcript rows, so leaving legacy
inference enabled makes ``gffutils`` re-derive parents that the file already
states. Comparing against that default would flatter gffbase for a reason that
has nothing to do with either engine's storage.

**The GTF row in the table above is the inference-disabled arm**
(``gtf_arm="no-infer"``, ``infer_gtf_parents=False`` on both sides) — the
recommended real-data configuration, and the one that is least favourable to
gffbase. On that arm ``gffutils`` reaches 229,000 attributes per second, its
fastest result on any corpus, because its GTF path becomes a plain bulk insert
with no synthesis; gffbase is at 157,000, squarely between its own MANE
(168,000) and GFF3 (162,000) figures. The 0.69× is the comparator running
unusually fast, not gffbase running unusually slow.

The 36-job cluster campaign reports three separate arms over these bytes, and
they are not mixed:

1. Unmodified GENCODE GTF with each engine's default compatibility behavior.
2. The same bytes with gene and transcript inference disabled in both engines —
   **the arm published above**.
3. A generated parent-stripped GTF with inference enabled in both engines; its
   source hash, transformation recipe, removed-row counts and output hash make
   this the controlled synthesis workload.

Only the third arm supports conclusions about synthesis, and it is not part of
this release's published numbers. No speedup is shown for any arm unless
normalized feature and relationship signatures agree.

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
     - faster where per-feature overhead dominates; **slower** on
       attribute-dense whole-genome files
     - the mirror of the same trade
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

The memory and disk costs buy the query behavior rather than the ingest wall:
an Arrow batch builder that stages columns before writing, a materialized
transitive closure so hierarchy walks are indexed lookups rather than recursion,
and a long-form attributes table so attribute search does not scan. Ingest pays
for all three up front, which is a large part of why it does not win outright on
the biggest files.

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
