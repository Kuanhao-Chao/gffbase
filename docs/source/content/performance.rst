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

   **gffbase ingests every corpus faster**, 1.92× to 3.62× against ``gffutils`` on
   wall time, into a database 0.61× to 0.89× the size of the SQLite one. The
   narrowest margin is GENCODE GTF, the inference-disabled arm, where
   ``gffutils``' GTF path is a plain bulk insert. Each ingest was measured once;
   see :ref:`What the ingest numbers do and do not say
   <methodology--what-the-ingest-numbers-do-and-do-not-say>` for how much a
   single figure is worth.

.. BEGIN GENERATED: benchmark-provenance

| **Measured on** AMD EPYC 7702 64-Core Processor · 128 cores · 1007.22 GB RAM · Linux-5.14.0-503.15.1.el9_5.x86_64-x86_64-with-glibc2.34
| **Versions:** Python 3.11.16 · gffbase 0.3.0 · duckdb 1.5.5 · pyarrow 25.0.1 · gffutils 0.14
| **Commit:** ``632a4d80dee0`` · **Run:** 2026-09-27T15:07:53Z
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
     - **3 min 36 s**
     - 6 min 54 s
     - **1.92×**
     - 53.47 GB
     - **728** ±1% (n=5)
     - 614 ms / 1.93 M desc
   * - **GENCODE v49** (basic)
     - GFF3
     - 6,066,054
     - **3 min 13 s**
     - 9 min 45 s
     - **3.03×**
     - 61.93 GB
     - **770** ±1% (n=5)
     - 666 ms / 1.93 M desc
   * - **RefSeq GRCh38.p14**
     - GFF3
     - 4,932,571
     - **2 min 5 s**
     - 6 min 31 s
     - **3.13×**
     - 26.69 GB
     - **588** ±1% (n=5)
     - 443 ms / 999 k desc
   * - **CHESS 3.1.3**
     - GFF3
     - 2,761,061
     - **37.0 s**
     - 2 min 14 s
     - **3.62×**
     - 2.93 GB
     - **678** ±3% (n=5)
     - 155 ms / 161 k desc
   * - **MANE v1.5** (Ensembl)
     - GFF3
     - 524,834
     - **15.6 s**
     - 45.1 s
     - **2.89×**
     - 4.00 GB
     - **890** ±0% (n=5)
     - 136 ms / 156 k desc

.. END GENERATED: corpus-table

All five :doc:`corpora <datasets>` completed; no row is censored, and every row
published a speedup only because the two engines' correctness signatures agreed
on it. Had a comparator been killed at its safety valve the cell would read
“censored at”, which is cap evidence and never a wall time — see
:doc:`Capped runs <methodology>`.

.. note::

   **What a single figure here is worth**

   Ingest wall time was measured once per corpus, not repeated — only the query
   phases use ``--repeats 5``. A separate probe of the same wheel, ingest only,
   on the same eight pinned cores, put the run-to-run spread at 0.3-2.3 %,
   largest on the smallest corpus (MANE), and the published single figures
   sat 0.7-4.9 % above that probe's medians -- so the ratios above, if
   anything, understate gffbase. The per-corpus figures are in
   :ref:`What the ingest numbers do and do not say
   <methodology--what-the-ingest-numbers-do-and-do-not-say>`.

   The ``peak RSS`` column is the peak of a process that ingests **and then
   validates exhaustively** (``validation_sample="all"``), which is why it
   reaches tens of GB. It is not the cost of ingest, and it is not what a user
   pays: ``validate_db`` defaults to ``sample=200`` and the CLI never overrides
   it. Ingest alone, in the same probe, peaks at 1.0 GiB (MANE) to 2.8 GiB (GENCODE GFF3).

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
gffbase. On that arm ``gffutils`` reaches 232,400 attributes per
second, its fastest result on any corpus, because its GTF path becomes a plain
bulk insert with no synthesis; gffbase is at 446,700, in line with its
other attribute-dense corpora. That is why this row has the narrowest margin,
1.92×: the comparator is running at its fastest, not gffbase at its
slowest.

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

   ``for i in ids: db.children(i)``, over ids from your own list, is **slower in
   GFFBase than in** ``gffutils``. DuckDB pays vectorization startup on every
   call; SQLite, an OLTP engine, does not. (A loop over a gffbase iterator is
   prefetched instead -- see :ref:`the loop timings below
   <performance--loops-against-gffutils>`.) This is a real and inherent trade, not a defect — and it is why
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
     - 2.93 GB – 61.93 GB
     - 106.76 MB – 190.00 MB
     - not comparable
   * - **On-disk database**
     - 329.51 MB – 3.69 GB
     - 472.90 MB – 6.05 GB
     - 0.61–0.89×

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
     - faster; memory bounded by a DuckDB budget
     - streams to SQLite in a few hundred MB
   * - **Loop over a gffbase iterator** (``children``, ``parents``, ``db[id]``)
     - prefetched: close, see below
     - close
   * - **Single call on an id from your own list**
     - **slower**: one DuckDB statement, 0.5-1.5 ms
     - a B-tree seek, a few hundredths of a ms
   * - **Bulk batched extraction**
     - one query, zero ``Feature`` objects
     - not available
   * - **Spatial index**
     - R-tree, or a zone-map-pruned scan
     - none

The memory and disk costs buy the query behavior: a materialized transitive
closure so a hierarchy walk is one lookup rather than a recursion, and a
long-form attributes table so attribute search parses no JSON.

.. _performance--loops-against-gffutils:

Loops against ``gffutils``
~~~~~~~~~~~~~~~~~~~~~~~~~~

``benchmarks/08_loops.py`` times the loops people write, in both libraries on
the same input, and prints a ratio only where every answer agrees (a digest of
each call's result). Milliseconds per call, 0.3.0 at the default 8 threads:

.. list-table::
   :header-rows: 1
   :widths: 40 15 15 15 15

   * - Workload
     - MANE, gffbase
     - MANE, ``gffutils``
     - GENCODE GTF, gffbase
     - GENCODE GTF, ``gffutils``
   * - ``for g in features_of_type("gene"): children(g, level=1)``
     - 0.10
     - 0.07
     - 0.45
     - 0.30
   * - ``... for t in children(g, level=1): children(t, featuretype="exon")``
     - 0.34
     - 0.29
     - 0.37
     - 0.38
   * - ``for e in features_of_type("exon"): parents(e, featuretype="gene")``
     - 0.08
     - 0.05
     - 0.13
     - 0.05
   * - ``children(id, level=1)``, ids from a random sample
     - 1.3
     - 0.05
     - --
     - --
   * - ``db[id]``, ids from a random sample
     - 0.5
     - 0.04
     - --
     - --

The first three are loops over a gffbase iterator, where each call is answered
from a prefetch; the last two have no iterator to prefetch from, and each call
is a DuckDB statement. Bring those into one query with the batched API.

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
