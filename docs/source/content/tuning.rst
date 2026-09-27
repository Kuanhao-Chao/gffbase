.. _tuning:

Memory and performance tuning
=============================

The defaults are chosen from measurements, and most users should not need
to change them. This page says what they are, why, and when to override
them.

Threads
-------

DuckDB's own default is one thread per core. gffbase uses at most **8** --
for ingest and for a handle it opens on a file -- because more bought no time
and cost memory: MANE ingest took 15.8-16.1 s at 8, 32 and 128 threads alike,
while peak RSS rose from 1.0 GiB to 1.2 and 1.6 GiB, and every single-call
query shape (``children``, ``parents``, ``db[id]``, ``region``) ran 10-24%
faster at 8 threads than at 128.

.. code-block:: bash

   GFFBASE_THREADS=4 python my_script.py    # any connection gffbase opens

``create_db(..., pragmas={"threads": n})`` sets it for one ingest, and
``db.set_pragmas({"threads": n})`` for an open handle. A connection you
open yourself and pass in keeps its own setting.

Memory during ingest
--------------------

Ingest into a file runs under a DuckDB memory budget of **512 MB**. A step
that needs more -- building the primary key or an index over tens of millions
of ids -- is retried with the budget doubled, up to DuckDB's own limit, and
checkpoints are taken at step boundaries so none of them can run short. The
connection you get back uses DuckDB's default.

Ingest wall time and peak resident memory at the defaults (8 threads, ingest
only, median of repeated runs on the benchmark machine):

.. list-table::
   :header-rows: 1

   * - Corpus
     - Features
     - Ingest
     - Peak RSS
   * - MANE v1.5
     - 0.5 M
     - 14.9 s
     - 1.0 GiB
   * - CHESS 3.1.3
     - 2.8 M
     - 36.2 s
     - 1.6 GiB
   * - RefSeq GRCh38.p14
     - 4.9 M
     - 119 s
     - 2.0 GiB
   * - GENCODE v49 GTF
     - 6.1 M
     - 209 s
     - 2.6 GiB
   * - GENCODE v49 GFF3
     - 6.1 M
     - 192 s
     - 2.8 GiB

To hold DuckDB to a fixed limit instead, set it yourself -- the budget then
stays out of the way:

.. code-block:: python

   limited = gffbase.create_db(HIERARCHY_PATH, "limited.duckdb", force=True,
                               pragmas={"memory_limit": "4GB"})

Too low a fixed limit can fail an index build, which cannot spill: FlyBase's
primary key over 31.8 M ids needs about 2 GB. An in-memory database
(``":memory:"``) is never budgeted, since its tables cannot be evicted.

Where ingest time goes
----------------------

``gffbase.ingest.from_file`` returns ``IngestStats``; its ``stages`` field
holds wall seconds per stage, in order -- ``parse``, ``append``,
``primary_key``, ``gtf_inference`` or ``edges``, ``closure``, ``indexes``, and
the rest. ``GFFBASE_INGEST_TRACE=1`` prints each stage as it ends, with the
process's peak RSS so far:

.. code-block:: text

   $ GFFBASE_INGEST_TRACE=1 gffbase create gencode.v49.gff3.gz gencode.duckdb --merge create_unique
   gffbase ingest: setup 0.128 s (peak RSS 0.13 GiB)
   gffbase ingest: parse 57.8 s (peak RSS 1.78 GiB)
   gffbase ingest: append 94.2 s (peak RSS 1.78 GiB)
   ...

``benchmarks/07_profile.py`` sweeps threads and batch sizes and captures the
query plans.

Loops, and when to batch
------------------------

A ``children()``, ``parents()`` or ``db[id]`` call on a feature that an open
iterator holds is **prefetched**: it is answered, with its neighbours', by
one query, and the loop runs within 1.4-3.5x of gffutils' speed.

.. code-block:: python

   # Prefetched: the genes come from a gffbase iterator.
   for gene in db.features_of_type("gene"):
       for transcript in db.children(gene, level=1):
           exons = list(db.children(transcript, featuretype="exon"))

A call on an id from your own list is a DuckDB query of its own -- about
1.3 ms for ``children()`` and 0.5 ms for ``db[id]``, against a few hundredths
of a millisecond for SQLite. For thousands of ids, ask once:

.. code-block:: python

   my_transcript_ids = [t.id for t in db.features_of_type("mRNA")]
   exons = db.children_batched(my_transcript_ids, featuretype="exon", format="arrow")

Prefetch applies to the default ordering with a level and an optional
featuretype; ``order_by``, ``reverse`` and ``limit`` use the per-call path.
Answers are identical either way. Writes through the ``FeatureDB`` --
``update``, ``delete``, ``add_relation`` -- are seen at once; SQL run directly
on ``db.conn`` inside a loop is not seen by that loop's prefetched answers.
``benchmarks/08_loops.py`` times the loops against gffutils.

Disk
----

A gffbase database is about half the size it was in 0.2 (GENCODE v49 GFF3:
3.7 GB), smaller than gffutils' SQLite file on the same input. The
``attributes(feature_id)`` index is built on the first write that needs it,
so a database that is never modified never stores it.
