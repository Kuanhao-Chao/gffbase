.. _migration--migrating-from-gffutils-to-gffbase:

Migrating from ``gffutils`` to ``gffbase``
==========================================

GFFBase is a drop-in successor to legacy
`gffutils <https://github.com/daler/gffutils>`__. For most users, the
migration is one import change.

   .. rubric:: ⚠️ READ THIS FIRST — the OLAP/OLTP gotcha
      :name: read-this-first-the-olapoltp-gotcha

   **There is exactly one common code pattern that gets slower, not
   faster, when you migrate to gffbase.** It's the per-id Python loop:

   .. code-block:: python

      # ❌ ANTI-PATTERN with gffbase: 50 000 small queries.
      # Pays DuckDB's vectorization startup × 50 000 + per-row Feature
      # construction × 1.6 M. Can take many minutes on a full GENCODE v49 run.
      for transcript_id in fifty_thousand_transcript_ids:
          for exon in db.children(transcript_id, featuretype="exon"):
              starts.append(exon.start)
              ends.append(exon.end)

   DuckDB is an **OLAP** engine — designed for big set-based queries.
   Iterating it row-by-row pays vectorization startup *per call* and
   never amortizes. SQLite (legacy gffutils) is **OLTP** — its B-tree
   seek on a cache-warm file is microseconds per call.

   .. rubric:: ✅ The fix — one canonical PyArrow snippet
      :name: the-fix-one-canonical-pyarrow-snippet

   .. code-block:: python

      # ✅ ONE set-based SQL query for all 50 000 transcripts.
      # Returns a zero-copy pyarrow.Table — no `Feature` object is ever
      # constructed — one set-based query instead of N.
      exons = db.children_batched(
          fifty_thousand_transcript_ids,
          featuretype="exon",
          format="arrow",         # or "df" / "polars"
      )

      # NumPy / PyTorch / JAX / Hugging Face datasets — all native.
      starts = exons.column("start").to_numpy()
      ends   = exons.column("end").to_numpy()

      # The "anchor" column carries the input transcript_id for each row,
      # so you can groupby in Python or downstream Arrow tooling without
      # re-issuing N queries:
      import pyarrow.compute as pc
      per_tx_exon_count = pc.value_counts(exons.column("anchor"))

   If your code has a ``for x in ids: db.children(x, …)`` loop and you
   care about wall time, **convert it now**, before you migrate. It is
   the only change required for *performance*; §6 lists the behaviour
   changes that may require one for *correctness*.

----

.. _migration--1-drop-in-compatibility-the-easy-part:

1. Drop-in compatibility — the easy part
----------------------------------------

**86 of 89 symbols (97%)** of the legacy ``gffutils`` public surface are
preserved verbatim. The three that are not are deliberate, recorded in
``tests/parity/deviations.toml``, and are surfaces upstream does not implement
either. A differential suite compares gffbase against a pinned gffutils
checkout on every corpus fixture, so this figure is derived rather than
asserted -- ``tests/test_release_hygiene.py`` recomputes it from the manifest
and fails if this sentence drifts.

The mapping:

.. list-table::
   :header-rows: 1
   :widths: 50 50

   * - ``gffutils`` symbol
     - ``gffbase`` equivalent
   * - ``gffutils.create_db(path, dbfn, ...)``
     - ``gffbase.create_db(path, dbfn, ...)``
   * - ``gffutils.FeatureDB(dbfn)``
     - ``gffbase.FeatureDB(dbfn)``
   * - ``gffutils.Feature(...)``
     - ``gffbase.Feature(...)``
   * - ``gffutils.DataIterator(...)``
     - ``gffbase.DataIterator(...)``
   * - ``gffutils.GFFWriter(...)``
     - ``gffbase.GFFWriter(...)``
   * - ``gffutils.merge_criteria.*``
     - ``gffbase.merge_criteria.*``
   * - ``gffutils.example_filename(name)``
     - ``gffbase.example_filename(name)``
   * - Exceptions (``FeatureNotFoundError``, …)
     - same names

.. docs-test: skip reason="illustrative: needs a real annotation file and the gffutils package"

.. code-block:: python

   # Before
   import gffutils
   db = gffutils.create_db("annotation.gff3", "annotation.db")

   # After
   import gffbase as gffutils      # one-line alias migration
   db = gffutils.create_db("annotation.gff3", "annotation.duckdb")

.. _migration--the-one-addition-worth-making-straight-away-close-the-handle:

The one addition worth making straight away: close the handle
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

``gffutils`` uses SQLite, which hands out shared connections and never locks a
reader out. gffbase uses DuckDB, which takes an **exclusive lock on the
database file for the life of a writable handle**. Ported code that opens a
database and never closes it will work — right up until something else needs
that file:

.. docs-test: skip reason="illustrative: names annotation.gff3, which the reader supplies"

.. code-block:: python

   from gffbase import FeatureDB, create_db

   # Best: scope it.
   with create_db("annotation.gff3", "annotation.duckdb", force=True) as db:
       ...

   with FeatureDB("annotation.duckdb") as db:
       ...

   # Or close it yourself.
   db = FeatureDB("annotation.duckdb")
   try:
       ...
   finally:
       db.close()

Two symptoms tell you the lock is the problem: another process cannot open the
database, and on Windows the file cannot be deleted or replaced.

If you fan work out across processes — a PyTorch ``DataLoader`` with
``num_workers > 1``, or a ``multiprocessing.Pool`` — open each worker's handle
**read-only**, which takes no exclusive lock and so allows any number of
concurrent readers:

.. docs-test: skip reason="illustrative: names annotation.duckdb, which the reader supplies"

.. code-block:: python

   with FeatureDB("annotation.duckdb", read_only=True) as db:
       ...

Full detail: `Connections & concurrency <https://khchao.com/gffbase/content/connections.html>`__.

All ``FeatureDB`` methods (``children``, ``parents``, ``region``,
``features_of_type``, ``interfeatures``, ``merge``, ``bed12``, ``update``,
``delete``, ``add_relation``, ``execute``, …) accept the same arguments and
return generators of ``Feature`` objects — identical to the legacy API.

The **storage backend** changes (DuckDB instead of SQLite). This is
transparent for almost all callers, but raw SQL queries that hit the
legacy schema directly via ``db.execute(...)`` need rewriting against
the GFFBase schema (or against the SQLite-compat views; see §4). We
also ship ``gffbase.export_sqlite(con, path)`` to dump a GFFBase
database into a legacy ``.sqlite`` file when you need the old format.

----

.. _migration--2-what-you-gain-immediately-no-code-changes:

2. What you gain immediately, no code changes
---------------------------------------------

Head-to-head against legacy ``gffutils`` across the five canonical human-genome
annotation releases:

.. raw:: html

   <!-- BEGIN GENERATED: corpus-table -->

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

.. raw:: html

   <!-- END GENERATED: corpus-table -->

.. raw:: html

   <!-- BEGIN GENERATED: benchmark-provenance -->

| **Measured on** Apple M1 Pro · 10 cores · 16.00 GB RAM · macOS-26.3-arm64-arm-64bit-Mach-O
| **Versions:** Python 3.13.5 · gffbase 0.2.0 · duckdb 1.5.2 · pyarrow 19.0.0 · gffutils 0.13
| **Commit:** ``1d52bf6738e0`` · **Run:** 2026-08-15T22:56:50Z
| *Generated from ``benchmarks/results/06_mega.json`` by ``tools/gen_benchmark_tables.py``. Do not edit by hand.*

“Censored at” marks a legacy run killed at its safety valve without finishing;
it supplies neither a completed wall nor a speedup. Method and fairness
constraints: `Methodology <https://khchao.com/gffbase/content/methodology.html>`__.

.. list-table::
   :header-rows: 1
   :widths: 50 50

   * - Single-call workload
     - Versus legacy
   * - Spatial overlap (``db.region(...)``)
     - substantially lower latency — gffbase has a spatial index, ``gffutils`` has none
   * - ``db.children(id, level=1)`` indexed lookup
     - comparable
   * - ``db.children_batched(ids, format="arrow")``
     - one query, no Python ``Feature`` objects — see below

Your existing ``gffutils`` script gets the ingest, spatial and attribute-query
wins the moment you swap the import. To unlock the batched-extraction win, see
the warning at the top of this page.

----

.. _migration--3-deep-dive-the-olap-vs-oltp-tradeoff:

3. ⚠️ Deep-dive: the OLAP vs OLTP tradeoff
------------------------------------------

DuckDB is an **OLAP** engine. It's optimized for big set-based queries
(JOINs, aggregations, scans of millions of rows). SQLite is an **OLTP**
engine — optimized for tiny indexed point lookups against cache-warm
pages. **For tiny, repeated point queries against a cache-warm DB,
SQLite (and therefore legacy gffutils) is faster.**

The fix is the canonical PyArrow snippet at the top of this page. At the scale
of tens of thousands of anchors the row-by-row gffbase loop is the slowest
option available and the batched call is the fastest, by a wide margin in both
directions — because the batched call issues one set-based query and never
constructs a Python ``Feature``. Current measurements:
`Performance <https://khchao.com/gffbase/content/performance.html>`__.

.. _migration--vectorized-methods-at-a-glance:

Vectorized methods at a glance
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

.. list-table::
   :header-rows: 1
   :widths: 50 50

   * - Vectorized method
     - Replaces this loop
   * - ``db.children_batched(ids, level=…, featuretype=…, format='arrow')``
     - ``for x in ids: db.children(x, …)``
   * - ``db.parents_batched(ids, …, format='arrow')``
     - ``for x in ids: db.parents(x, …)``
   * - ``db.region_batched(regions, …, format='arrow')``
     - ``for r in regions: db.region(r, …)``

``format`` accepts ``"arrow"`` (default — ``pyarrow.Table``), ``"df"``
(``pandas.DataFrame``), or ``"polars"`` (``polars.DataFrame``). All three
share memory with DuckDB's query buffers — no per-row Python
materialization happens at any layer.

.. _migration--when-you-dont-need-to-migrate-the-pattern:

When you don't need to migrate the pattern
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

- One-off scripts that ask ``db[gene_id]`` or ``db.children(gene)`` for
  fewer than ~100 anchors.

- Small annotations (< 100 k features) where SQL startup overhead is
  not visible.

For everything else — ML feature extraction, BED12 export of every
transcript, "for each peak in this 50 000-row BED file find every
overlapping CDS" — switch to ``*_batched``.

----

.. _migration--4-sql-compat-views-for-raw-execute-users:

4. SQL-compat views (for raw ``execute()`` users)
-------------------------------------------------

Legacy code that did ``db.execute("SELECT * FROM features WHERE …")``
hits the new DuckDB schema (``features``, ``attributes``, ``edges``,
``closure``). Two compatibility views provide the legacy column shapes:

.. code-block:: sql

   -- features_compat: legacy SQLite-style 12-column features table.
   SELECT * FROM features_compat WHERE seqid = 'chr1' LIMIT 5;

   -- relations_compat: legacy parent/child/level table.
   SELECT parent, child, level FROM relations_compat WHERE level = 1;

The ``attributes`` column on ``features_compat`` is the **raw col-9
bytes** (UTF-8), not legacy-style JSON. If your raw-SQL code parses
JSON out of that column, switch to querying the normalized
``attributes`` table directly:

.. code-block:: sql

   SELECT a.value FROM attributes a
   WHERE a.feature_id = ? AND a.key = 'gene_biotype';

This is also faster — ``attributes_kv`` indexes ``(key, value)``, so
attribute filters become indexed seeks.

----

.. _migration--5-sqlite-export-the-safety-valve:

5. SQLite export — the safety valve
-----------------------------------

If a downstream tool only knows how to read legacy
``gffutils``-compatible SQLite files:

.. code-block:: python

   from gffbase import export_sqlite
   export_sqlite(db.conn, "legacy_compatible.sqlite")

Produces a SQLite database with the original gffutils schema,
populated UCSC ``bin`` column, and the closure flattened back into
``relations(parent, child, level)``. The downstream tool can open this
file with ``gffutils.FeatureDB("legacy_compatible.sqlite")``.

----

.. _migration--6-things-that-changed-small-list:

6. Things that changed (small list)
-----------------------------------

- **Storage backend**: SQLite → DuckDB. Database file extension is
  ``.duckdb`` by convention. The legacy SQLite layout is reachable via
  ``export_sqlite()`` (above) or the compat views.

- **Disk size**: GFFBase databases are ~1.5× larger than legacy
  SQLite -- the price of materializing the transitive closure and the R-tree,
  which is what turns hierarchy and spatial queries into indexed lookups.
  Current measurements: `Performance <https://khchao.com/gffbase/content/performance.html>`__.

- **Peak ingest RSS**: substantially higher -- a couple of GB against roughly
  150 MB, on a whole-genome corpus. DuckDB allocates a vectorized ingest
  buffer pool; cap it with ``GFFBASE_THREADS`` or
  ``PRAGMA memory_limit='512MB'`` if that matters more than wall time.

- **Hierarchy depth**: GFFBase materializes the closure to depth 8 by
  default (vs depth 2 in legacy). Anything past 8 falls through to a
  dynamic recursive CTE — the dispatcher is automatic.

- **Attributes column shape**: in raw SQL, the legacy single-cell
  JSON blob is replaced by a normalized
  ``attributes(feature_id, key, value, idx, seg_idx, ord)`` long-form table.
  Filtering by attribute is now an indexed query, not a full scan.

- **Duplicate IDs**: NCBI RefSeq emits multiple GFF3 rows that share
  ``ID=cds-NP_xxx``. Under the default ``mode="compat"`` gffbase renames the
  repeats as ``gffutils.merge_strategy="create_unique"`` would and records the
  remap in ``duplicates``. Under ``mode="strict"`` it instead **fuses them into
  one discontinuous feature** — see below.

.. _migration--behaviour-changes-that-can-change-your-results:

Behaviour changes that can change your results
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

These are the ones worth reading before a production run. Each is small in
isolation; each can change what your script computes.

- **``merge_all`` now persists.** It always documented that "the resulting
  records are added to the database", and did not. It also returned every
  input feature rather than only genuine merges, and accepted
  ``exclude_components`` while ignoring it. All three are fixed, so a script
  that called ``merge_all`` expecting a read-only generator now **writes to the
  database** and gets back a shorter list.

- **``merge_criteria.overlap_*_threshold`` changed meaning.** They were distance
  tests (``abs(acc.end - cur.start) <= threshold``) and are now range tests, so
  a feature lying entirely inside the accumulator merges where it did not
  before. If you pass one of these to ``merge`` or ``merge_all``, the set of
  features that merge has changed.

- **``create_introns`` computes per transcript.** It treated
  ``grandparent_featuretype="gene"`` as the direct anchor and pooled every
  isoform's exons into one list, so for a multi-isoform gene the "introns"
  spanned transcript boundaries. On ``FBgn0031208.gff`` that was 1 where the
  oracle finds 3.

- **Splice sites are 2 bp**, strand-aware (``five_prime_cis_splice_site`` /
  ``three_prime_cis_splice_site``), and carry the intron's merged attributes.
  They were 1 bp, always typed ``splice_site``, and attribute-less.

- **``bed12`` output changed**: no trailing comma on ``blockSizes``/``blockStarts``,
  ``thin_featuretype`` is honoured rather than ignored, and a feature with no
  CDS is now marked entirely *thick* rather than entirely thin.

- **Attribute values are percent-encoded on write.** Reading
  ``feature.attributes`` and re-serializing used to drop the escaping, which
  could emit structurally invalid GFF3 when a value contained ``;`` or ``,``. If
  you diff gffbase output against gffutils output you will now see them agree
  where they previously did not. Note that space and non-ASCII are
  deliberately *not* encoded, per the specification.

- **Raw SQL against ``features`` returns ENVELOPE coordinates** for a
  discontinuous feature — ``MIN(start)``, ``MAX(end)`` over its segments, not the
  coordinates of any one line. The ``segments_all`` view gives one row per
  physical input line, which is what a line-oriented consumer wants.

- **Coordinates can be NULL.** A GFF row may carry ``.`` in columns 4 and 5, and
  gffbase preserves that rather than coercing to 0. Such features are not in
  coordinate space and are skipped by ``region()`` and the derived-feature
  methods.

- **``type(f) is Feature`` is no longer universally true.** A fused feature is a
  ``MultipartFeature``, which subclasses ``Feature`` and overrides none of the
  compatibility surface. ``isinstance`` still holds.

- **Derived features carry a mode-dependent ``source``.** ``gffutils_derived``
  under ``mode="compat"``, ``gffbase_derived`` under ``mode="strict"``. If you
  filter on that string, ``db.derived_source`` gives you the right one.

.. _migration--command-line:

Command line
~~~~~~~~~~~~

``gffutils-cli`` becomes ``gffbase``, with the same argument names. Seven of its
commands work there; ten work here. See `the CLI reference <https://khchao.com/gffbase/content/cli.html>`__ for
the mapping, including the four upstream commands that raise on every
invocation.

----

.. _migration--7-migration-checklist:

7. Migration checklist
----------------------

- ☐ ``pip install gffbase``
- ☐ Replace ``import gffutils`` with ``import gffbase as gffutils`` (or
  use the new name directly).

- ☐ Re-ingest your annotations (``create_db``) — old ``.sqlite`` files
  can still be read by legacy gffutils; they're not GFFBase
  databases.

- ☐ **Audit your code for ``for x in ids: db.children(x, …)`` loops
  and convert them to ``db.children_batched(ids, format='arrow')``.**
  This is the only common change that requires user action.

- ☐ If you have raw ``db.execute(...)`` SQL: use
  ``features_compat`` / ``relations_compat`` views, or move attribute
  filters onto the normalized ``attributes`` table.

- ☐ Run your existing test suite. Everything else should be
  identical.

If anything breaks, please open an issue at
https://github.com/Kuanhao-Chao/gffbase/issues with a minimal
reproducer.
