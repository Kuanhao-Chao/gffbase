.. _schema_v2--schema-v2-multipart-features-and-the-compatstrict-axis:

Schema v2, multipart features, and the compat/strict axis
=========================================================

Status: **implemented in 0.2.0** (``gffbase.schema.SCHEMA_VERSION == "2"``)
Supersedes: schema v1

This document is kept as the design record. Where the implementation ended up
somewhere other than the recommendation, §12 says so rather than being quietly
edited to match — the disagreement is the useful part.

This is the design for the three linked changes in 0.2.0: a validation-profile
axis that makes ``create_db()`` usable as a drop-in again, a storage model that
can represent a discontinuous GFF3 feature, and the migration between them.

----

.. _schema_v2--1-why:

1. Why
------

.. _schema_v2--11-gffbase-rejects-files-gffutils-reads:

1.1 gffbase rejects files gffutils reads
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

The differential suite measures this: **6 of the 23 upstream fixtures that
gffutils ingests, gffbase refuses outright** — including ``FBgn0031208.gff``,
the canonical gffutils fixture.

.. list-table::
   :header-rows: 1
   :widths: 34 33 33

   * - Fixture
     - gffbase error
     - What the oracle does
   * - ``FBgn0031208.gff``, ``FBgn0031208.gtf``, ``wormbase_gff2_alt.txt``
     - ``InvalidPhase``
     - Loads. A ``CDS`` row with ``.`` phase is out of spec but FlyBase and WormBase both emit it.
   * - ``unsanitized.gff``
     - ``InvalidCoordinate``
     - Loads. ``end < start`` is exactly what ``sanitize_gff_file`` exists to repair — rejecting it makes sanitize impossible.
   * - ``wormbase_gff2.txt``
     - ``InvalidAttribute``
     - Loads. GFF2 ``Sequence "cTel33B"`` has no ``=``.
   * - ``issue167.gff``
     - ``TooFewFields``
     - Loads *degenerately*: the line is space-delimited, ``feature_from_line`` splits on tab, ``zip(_gffkeys, fields)`` truncates, and every column past seqid takes its default.

The root cause is a category error: ``rust/src/validate.rs`` validates to the
**NCBI GFF3 specification** unconditionally, but ``create_db()`` is the
*compatibility* entry point. Real annotation files violate that spec routinely.
Strict validation is a genuinely useful feature — it just cannot be the
behaviour of the drop-in API.

.. _schema_v2--12-a-discontinuous-feature-has-nowhere-to-live:

1.2 A discontinuous feature has nowhere to live
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

GFF3 lets one logical feature span several lines sharing an ``ID``, which is how
NCBI represents a split CDS. Schema v1 makes ``features.id`` a primary key, so
the second line collides. Every strategy loses information:

- ``error`` — the file will not load;
- ``create_unique`` — one feature becomes N unrelated features;
- ``merge`` — refuses, because the coordinates differ, and falls back to
  ``create_unique``.

gffutils has the same limitation, so this is a capability gffbase adds rather
than parity work. ``dmel-all-no-analysis-r5.49_50k_lines.gff`` contains **345**
genuine segment runs; ``synthetic.gff3`` and ``random-chr.gff`` one each.

----

.. _schema_v2--2-two-orthogonal-axes-one-user-facing-switch:

2. Two orthogonal axes, one user-facing switch
----------------------------------------------

Validation currently conflates two independent questions. They are separated:

.. list-table::
   :header-rows: 1
   :widths: 20 20 20 20 20

   * - Axis
     - Parameter
     - Values
     - Meaning
     - 
   * - Which rules apply
     - ``validation``
     - ``"gffutils"`` \
     - ``"ncbi"``
     - The rule set
   * - What a violation does
     - ``on_error``
     - ``"raise"`` \
     - ``"warn"``
     - The disposition

``mode`` is the switch users actually set; it picks both:

.. list-table::
   :header-rows: 1
   :widths: 34 33 33

   * - 
     - ``mode="compat"`` (default for ``create_db``)
     - ``mode="strict"`` (default for ``parse_gff``)
   * - ``validation``
     - ``"gffutils"``
     - ``"ncbi"``
   * - ``on_error``
     - ``"raise"``
     - ``"raise"``

The existing ``strict: bool`` on ``parse_gff``/``parse_bytes`` keeps working for one
deprecation cycle: ``strict=True`` → ``on_error="raise"``, ``strict=False`` →
``on_error="warn"``, with a ``DeprecationWarning`` naming ``on_error``. Passing both
``strict=`` and ``on_error=`` raises ``TypeError`` — an argument conflict, not a data
error, so this is the one place that is not a ``ValueError``.

.. _schema_v2--21-the-rule-table:

2.1 The rule table
~~~~~~~~~~~~~~~~~~

The ``gffutils`` profile enforces almost nothing, because **gffutils enforces
almost nothing**. Every rule below is one gffbase currently applies:

.. list-table::
   :header-rows: 1
   :widths: 34 33 33

   * - Rule
     - ``gffutils`` profile
     - ``ncbi`` profile
   * - Fewer than 9 tab fields
     - accept; ``zip``-truncate, missing columns take defaults
     - error
   * - Empty seqid
     - accept
     - error
   * - Empty featuretype
     - accept (defaults to ``.``)
     - error
   * - Whitespace in featuretype
     - accept
     - error
   * - ``start < 1``
     - accept
     - error
   * - ``end < start``
     - accept
     - error
   * - Strand not in ``+-?.``
     - accept
     - error
   * - Phase not in ``.012``
     - accept
     - error
   * - ``CDS`` with ``.`` phase
     - accept
     - error
   * - Non-numeric score
     - accept
     - error
   * - Column 9 parses to no pairs
     - accept
     - error

"Accept" does not mean "ignore": every violation is recorded as a warning,
reachable via ``FeatureDB.warnings`` and ``DataIterator.warnings``. A compat-mode
user gets exactly the data gffutils gives them, **plus** a diagnostic gffutils
never offered.

The degenerate too-few-fields case is reproduced deliberately. It is the one
rule where matching the oracle means matching a defect, and the alternative —
refusing a file the oracle reads — is worse for a drop-in. The warning is what
makes it safe.

----

.. _schema_v2--3-schema-v2:

3. Schema v2
------------

.. _schema_v2--31-the-decision-that-keeps-the-change-small:

3.1 The decision that keeps the change small
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

The obvious design splits ``features`` into logical rows and physical rows. That
would rewrite all eight SQL builders in ``interface.py`` and move the R-tree off
the table ``region()`` queries.

Instead:

   ``features`` **stays one row per logical feature. Its**
   ``start``/``end``/``bbox`` **become the envelope.** ``segments`` **is a
   SPARSE side table holding physical lines only for features with**
   ``n_segments > 1``\ **.**

Consequences, all of which fall out for free:

- **Logical dedup is structural.** ``region()`` cannot return a feature twice
  because ``features`` physically has one row for it. No ``DISTINCT``, no
  ``GROUP BY``.

- **The R-tree stays the primary access path.** Envelope overlap is a superset
  of segment overlap, so the only false positives are multipart features whose
  *gap* covers the query — rechecked by an ``EXISTS`` that is evaluated only for
  the ``n_segments > 1`` rows.

- **Zero cost when there are no multipart features.** With
  ``meta.n_multipart == 0`` the query builder omits the extra conjunct entirely
  and emits byte-identical SQL to v1. A test asserts that string equality.

- **Storage is proportional to duplicate lines, not to corpus size.** A full
  mirror table would double ``attributes_blob``, the dominant column at GENCODE
  scale.

- **Migration touches zero feature rows** — ``ALTER TABLE ADD COLUMN`` plus new
  tables. The existing ``features_rtree`` stays valid, because for a 1-segment
  feature the envelope *is* the line.

The cost is that "one row per physical line" needs a view. That is paid once,
in ``segments_all``.

.. _schema_v2--32-ddl:

3.2 DDL
~~~~~~~

.. code-block:: sql

   CREATE TABLE features (
       id              VARCHAR NOT NULL,   -- logical key; UNIQUE INDEX post-load
       raw_id          VARCHAR NOT NULL,   -- id as derived from col 9, pre-resolution
       seqid           VARCHAR NOT NULL,   -- logical: invariant across segments
       source          VARCHAR,            -- logical
       featuretype     VARCHAR NOT NULL,   -- logical
       strand          VARCHAR,            -- logical
       start           BIGINT,             -- ENVELOPE = MIN(segment.start). NULLABLE.
       "end"           BIGINT,             -- ENVELOPE = MAX(segment.end).   NULLABLE.
       score           VARCHAR,            -- representative: segment 0
       frame           VARCHAR,            -- representative: segment 0
       attributes_blob BLOB,               -- representative: segment 0
       extra_blob      BLOB,               -- representative: segment 0
       file_order      BIGINT,             -- segment 0's line ordinal
       occ             INTEGER NOT NULL DEFAULT 0,   -- load-time occurrence of raw_id
       n_segments      INTEGER NOT NULL DEFAULT 1,
       is_synthetic    BOOLEAN NOT NULL DEFAULT FALSE,
       id_origin       VARCHAR NOT NULL DEFAULT 'attribute',
       seqid_y         BIGINT
       -- , bbox GEOMETRY  <- conditional ALTER when the spatial extension loads
   );

   CREATE TABLE segments (
       feature_id      VARCHAR NOT NULL,
       seg_idx         INTEGER NOT NULL,   -- 0-based, ordered by FILE APPEARANCE
       start           BIGINT,
       "end"           BIGINT,
       score           VARCHAR,
       frame           VARCHAR,            -- per-segment CDS phase: the main reason this table exists
       attributes_blob BLOB NOT NULL,
       extra_blob      BLOB,
       file_order      BIGINT NOT NULL,
       attrs_same_as_seg0 BOOLEAN NOT NULL DEFAULT TRUE,
       seqid_y         BIGINT
       -- , bbox GEOMETRY
   );

**Invariant:** ``segments`` holds rows only for ``feature_id``\ s with
``n_segments > 1``, and for those it holds *all* segments ``0 … n-1``. Segment 0 is
present even though ``features`` carries its blob, because ``features.start/end``
is the envelope and segment 0's own coordinates need somewhere to live.

``seg_idx`` is ordered by file appearance, not coordinate: GFF3 does not require
sorted segments and ``to_lines()`` must reproduce the input order. Coordinate
order is recovered with ``ORDER BY start``.

``raw_id`` and ``occ`` form the load-time surrogate identity ``(raw_id, occ)``. This
also **removes** the ``f"{fid}_{n}"`` string build from the per-row Python loop:
the builder appends an integer instead.

.. _schema_v2--33-segments_all-the-uniform-physical-view:

3.3 ``segments_all`` — the uniform physical view
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

.. code-block:: sql

   CREATE OR REPLACE VIEW segments_all AS
       SELECT id AS feature_id, 0 AS seg_idx, seqid, source, featuretype, strand,
              start, "end", score, frame, attributes_blob, extra_blob,
              file_order, seqid_y
       FROM features WHERE n_segments = 1
   UNION ALL
       SELECT s.feature_id, s.seg_idx, f.seqid, f.source, f.featuretype, f.strand,
              s.start, s."end", s.score, s.frame, s.attributes_blob, s.extra_blob,
              s.file_order, s.seqid_y
       FROM segments s JOIN features f ON f.id = s.feature_id;

Exactly one row per physical input line — the ``n_segments = 1`` filter is what
makes the branches disjoint. Every physical-level consumer (``explode_segments``,
``export_sqlite``, ``to_lines()``, the validator) reads this name.

.. _schema_v2--34-attributes:

3.4 ``attributes``
~~~~~~~~~~~~~~~~~~

.. code-block:: sql

   CREATE TABLE attributes (
       feature_id VARCHAR  NOT NULL,   -- LOGICAL id
       key        VARCHAR  NOT NULL,
       value      VARCHAR  NOT NULL,
       idx        SMALLINT NOT NULL DEFAULT 0,  -- multivalue index (v1 meaning)
       seg_idx    INTEGER  NOT NULL DEFAULT 0,  -- owning physical line
       ord        INTEGER  NOT NULL DEFAULT 0   -- pair position within that line
   );

**Storage rule:** rows with ``seg_idx > 0`` exist only where that segment's raw
blob differs byte-for-byte from segment 0's. NCBI repeats identical attributes
on every CDS line, so this keeps the row count effectively unchanged from v1.

``ord`` is new and load-bearing: v1 relies on physical insertion order to
reproduce key order, which no SQL engine guarantees.

.. _schema_v2--35-edges-closure:

3.5 ``edges`` / ``closure``
~~~~~~~~~~~~~~~~~~~~~~~~~~~

Unchanged in shape; both reference logical ids. A discontinuous CDS has one
parent edge, not N. ``EDGES_FROM_PARENT`` gains ``DISTINCT``, which also fixes a
latent v1 bug on ``Parent=a,a``.

Where segments disagree on ``Parent``, the logical feature takes the **union** —
legal GFF3, and forcing equality would fragment real files over vendor typos.
The validator flags it.

.. _schema_v2--36-nullable-coordinates:

3.6 Nullable coordinates
~~~~~~~~~~~~~~~~~~~~~~~~

``features.start/"end"`` become nullable, storing ``NULL`` for a ``.`` column. This
is the fix for the coercion-to-``0`` defect already pinned by a strict xfail.
``bbox`` is ``CASE WHEN start IS NULL THEN NULL ELSE ST_MakeEnvelope(...) END``, so
such rows are excluded from ``region()`` — matching gffutils, where ``NULL <= ?``
is unknown. In ``ncbi`` validation a null coordinate is an error.

.. _schema_v2--37-duplicates-vs-id_conflicts:

3.7 ``duplicates`` vs ``id_conflicts``
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

``duplicates`` must be exactly what gffutils writes, because ``export_sqlite``
copies it verbatim and the oracle's ``_candidate_merges`` reads it. As
established in Phase 4, gffutils populates it **only** from the
``merge``\ →\ ``create_unique`` fallback.

A gffbase-native ``id_conflicts`` table records every resolution
(``multipart`` / ``create_unique`` / ``merge`` / ``merge_fallback`` /
``warning_dropped`` / ``replaced`` / ``strict_split``) with the reason.

----

.. _schema_v2--4-the-multipart-predicate:

4. The multipart predicate
--------------------------

Over the physical lines sharing one ``ID``:

.. list-table::
   :header-rows: 1
   :widths: 25 25 25 25

   * - Col
     - Field
     - Must match?
     - Why
   * - 1
     - ``seqid``
     - **yes**
     - one feature cannot span two sequences
   * - 2
     - ``source``
     - **yes**
     - provenance is a property of the feature
   * - 3
     - ``featuretype``
     - **yes**
     - required by the GFF3 spec for discontinuous features
   * - 4
     - ``start``
     - no
     - the point of the exercise
   * - 5
     - ``end``
     - no
     - the point of the exercise
   * - 6
     - ``score``
     - no
     - per-segment alignment scores are legal
   * - 7
     - ``strand``
     - **yes**
     - one feature has one orientation
   * - 8
     - ``frame``
     - no
     - **the primary reason this exists** — per-segment CDS phase
   * - 9
     - attributes
     - no
     - ``Parent`` is unioned; divergence is recorded

.. code-block:: sql

   multipart_ok := COUNT(DISTINCT seqid) = 1
               AND COUNT(DISTINCT source) = 1
               AND COUNT(DISTINCT featuretype) = 1
               AND COUNT(DISTINCT strand) = 1

``ncbi_gff3.txt`` is the **negative** test: one id there is shared by a ``CDS``, a
``start_codon`` and a ``stop_codon``, and four separate ``gene`` rows at different
loci (one on the opposite strand) share another. That is malformed GFF3 and the
predicate must reject it. The FlyBase ``orthologous_region`` runs are the
positive case.

Note that gffutils' ``merge_strategy="merge"`` requires **all eight**
non-attribute columns to match, so it never merges a genuine discontinuous
feature — it routes them to ``create_unique``. Multipart fusion is therefore new
behaviour, not a change to gffutils behaviour, which is exactly why it belongs
behind ``mode="strict"`` and not in compat.

When the predicate fails in strict mode: ``on_multipart_conflict="error"``
(default) raises ``MultipartConstraintError`` naming the id, the diverging column
and both line numbers; ``"split"`` partitions by the constraint key, lowest
``file_order`` keeps the bare id, the rest get ``_1``, ``_2``.

----

.. _schema_v2--5-python-model:

5. Python model
---------------

``MultipartFeature`` and ``FeatureSegment`` both **subclass** ``Feature``.

A sibling or wrapper class is wrong here, for a concrete reason:
``isinstance(x, Feature)`` is load-bearing at nine call sites inside gffbase
itself — ``__getitem__``, ``__contains__``, ``children``, ``parents``, ``_coerce_ids``,
``_coerce_id_list``, ``_normalize_region_args``, ``update``, ``merge``. A sibling
breaks ``db[db["cds-x"]]`` and ``db.children(f)`` on day one. ``Feature.__eq__``
returns ``NotImplemented`` for non-``Feature`` operands, so a sibling would compare
unequal to an identical ``Feature``.

Subclassing lets us **not override** ``__str__``, ``__len__``, ``astuple``,
``__getitem__``, ``__eq__``, ``__hash__``. The compat surface is preserved by
*inaction*, which is the only reliable way to preserve it.

.. docs-test: skip reason="illustrative: contains an elided fragment"

.. code-block:: python

   class Feature:                      # unchanged
       is_multipart: ClassVar[bool] = False
       n_segments:   ClassVar[int]  = 1

       @property
       def segments(self) -> tuple[FeatureSegment, ...]:
           """A singleton feature is its own sole segment."""

       def to_lines(self) -> list[str]: ...


   class FeatureSegment(Feature):
       """One physical line. `self.id` is the LOGICAL id; the segment's own col-9
       `ID=` is preserved byte-for-byte in `_attributes_blob`."""
       __slots__ = ("seg_idx",)


   class MultipartFeature(Feature):
       """One logical feature over >1 line. Inherits gffutils-identical
       behaviour on the ENVELOPE row; nothing is overridden."""
       __slots__ = ("_n_segments", "_segments", "_segment_loader")
       is_multipart: ClassVar[bool] = True

       @property
       def segments(self) -> tuple[FeatureSegment, ...]: ...
       @property
       def covered_length(self) -> int:   # sum of segment lengths, gaps excluded
       def to_lines(self) -> list[str]: ...

``len(mf)`` stays the envelope span (``end - start + 1``), matching ``Feature``;
``covered_length`` is the new, different quantity.

**Avoiding an N+1:** ``MultipartFeature`` holds a ``segment_loader`` closure, and
``_yield_features`` prefetches at the existing ``fetchmany(10_000)`` chunk
boundary — one query per chunk, gated on ``if self._n_multipart:`` so corpora
without multipart features pay a single attribute test per 10 000 rows.

**Documented consequence:** ``type(f) is Feature`` stops being true for multipart
rows. ``isinstance`` is unaffected.

----

.. _schema_v2--6-query-shapes:

6. Query shapes
---------------

.. _schema_v2--61-region-r-tree-path:

6.1 Region, R-tree path
~~~~~~~~~~~~~~~~~~~~~~~

.. code-block:: sql

   SELECT f.id, ..., f.n_segments
   FROM features f
   WHERE f.seqid = ?
     AND ST_Intersects(f.bbox, ST_MakeEnvelope(?, ?, ?, ?))
     AND ( f.n_segments = 1                        -- emitted ONLY when n_multipart > 0
           OR EXISTS (SELECT 1 FROM segments s
                      WHERE s.feature_id = f.id
                        AND s.start <= ? AND s."end" >= ?) )
   ORDER BY f.start;

``ST_Intersects`` stays a top-level conjunct so the planner still chooses the
R-tree; the disjunction is parenthesized and never touches ``bbox``. A test
asserts ``RTREE_INDEX_SCAN`` in the ``EXPLAIN`` output.

``completely_within=True`` needs **no** recheck: envelope containment already
implies every segment is contained, because the envelope is ``MIN``/``MAX`` over
segments.

.. _schema_v2--62-explode_segmentstrue:

6.2 ``explode_segments=True``
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

On the tabular/batched APIs only (``region_batched``, ``children_batched``,
``parents_batched``) — never on ``region()``/``children()``/``all_features()``, which
must keep yielding ``Feature`` objects for parity. Letting ``FeatureSegment`` rows
leak into those would corrupt legacy consumers.

----

.. _schema_v2--7-ingest-order:

7. Ingest order
---------------

::

   parse → Arrow load → RESOLVE → edges → [GTF synth] → closure
         → blob regen → indexes → rtree → validate

The resolve pass runs **before** ``EDGES_FROM_PARENT``, so edges and closure are
built once against final logical ids and never need rewriting. GTF synthesis
runs **after** resolve, so synthesized rows are never duplicate candidates.

The pass is skipped entirely when no ``raw_id`` collided — the common case for
every GTF corpus and for GENCODE GFF3.

----

.. _schema_v2--8-migration:

8. Migration
------------

``FeatureDB.__init__`` reads ``meta.schema_version`` immediately:

::

   "2"          -> proceed
   missing/"1"  -> upgrade="auto" (default) + writable -> migrate in place, log INFO
                   read-only or upgrade="never"        -> v1 shim mode
                   upgrade="error"                     -> SchemaVersionError
   > "2"        -> SchemaVersionError("written by a newer gffbase")

**v1 shim mode** sets ``_n_multipart = 0``, which makes every query builder take
the branch that emits v1 SQL anyway. ``explode_segments=True`` raises
``NotImplementedError``; ``.segments`` returns the singleton.

The structural migration is ``ADD COLUMN``/``CREATE TABLE``/``CREATE VIEW`` in one
transaction, idempotent, touching zero feature rows.

Re-fusing v1's ``__2``/``__3`` rows into multipart features is **not** part of the
upgrade — an in-place upgrade must never change query results. It is a separate
explicit call, ``gffbase.migrate.coalesce_multipart(db)``, which reuses the same
resolve pass.

``attributes.ord`` cannot be recovered for a v1 database (insertion order is not
a guarantee), so it stays ``0`` and ``meta.attributes_ord_valid = 'false'`` records
that. Consumers needing key order fall back to the raw blob, which is intact.

----

.. _schema_v2--9-legacy-sqlite-export:

9. Legacy SQLite export
-----------------------

``export_sqlite`` writes **one legacy row per physical segment**, reading from
``segments_all`` — a seg-0 row must carry its own coordinates, not the envelope.

There is no ``flatten=`` parameter in the shipped ``export_sqlite(con, path, force=False)``: open question 1 was resolved by hardcoding the fan-out, so the
alternative was never built. The behaviour described here is what it does
unconditionally, reproducing what
``gffutils.create_db(file, merge_strategy="create_unique")`` would have produced:
``legacy_id(seg 0) = <logical id>``, ``legacy_id(seg k>0) = <logical id>_k``,
collision-hardened by anti-join. Relations fan out over both endpoints'
legacy ids. ``duplicates`` gets ``(logical_id, legacy_id)`` for every ``k > 0``,
which is both what the legacy table means and how a round-trip re-import can
rediscover the grouping.

----

.. _schema_v2--10-post-ingest-invariants:

10. Post-ingest invariants
--------------------------

``gffbase.validate.validate_db(db, level="fast"|"full")``, run automatically at
the end of a strict-mode ingest. All checks are single set-based queries.

.. list-table::
   :header-rows: 1
   :widths: 50 50

   * - #
     - Invariant
   * - INV-1
     - ``features.id`` is unique
   * - INV-2
     - No orphan segments
   * - INV-3
     - ``n_segments`` agrees with the ``segments`` row count
   * - INV-4
     - ``seg_idx`` is dense ``0…n-1`` and unique per feature
   * - INV-5
     - **Envelope is exact**: ``features.start = MIN(seg.start)``, ``"end" = MAX(seg."end")`` — the single most important check; a wrong envelope silently loses features from ``region()``
   * - INV-6
     - Every ``edges`` endpoint resolves to a ``features.id`` (warn — a dangling ``Parent=`` is common in hand-edited files and costs an edge, not correctness elsewhere)
   * - INV-7
     - Segments of one feature share the logical columns and do not overlap each other (warn — abutting segments occur in real files)
   * - INV-8
     - ``bbox`` agrees with ``(start, seqid_y, "end", seqid_y+1)``, and is NULL exactly when coords are NULL
   * - INV-9
     - Every ``features.seqid`` has a ``seqid_map`` row with a matching band
   * - INV-10
     - Attribute dedup is consistent with ``attrs_same_as_seg0``
   * - INV-11
     - ``closure`` is acyclic and depth-consistent
   * - INV-11b
     - No feature is its own ancestor (warn — a self-loop is recorded rather than repaired, since silently rewriting a user's hierarchy is worse)
   * - INV-12
     - (``full`` only) Re-parsing ``attributes_blob`` for a sample reproduces the normalized ``attributes`` rows — the check that catches escaping bugs
   * - INV-13
     - ``id_origin='create_unique'`` ⟺ an ``id_conflicts`` row exists; ``duplicates`` is non-empty only under ``merge``
   * - INV-14
     - ``features.file_order`` equals ``MIN(file_order)`` over its segments

----

.. _schema_v2--11-implementation-order-and-risk:

11. Implementation order and risk
---------------------------------

.. list-table::
   :header-rows: 1
   :widths: 34 33 33

   * - #
     - Change
     - Risk
   * - 1
     - ``exceptions.py``: add ``MultipartConstraintError``, ``CyclicRelationError``, ``SchemaVersionError``
     - **low** — additive
   * - 2
     - ``modes.py`` (new): ``Mode``, ``ValidationProfile``, ``resolve_mode()``
     - **low** — single source of truth
   * - 3
     - ``validate.rs`` + ``_pyfallback``: gate rules on the profile
     - **medium** — must be mirrored exactly in both engines or ``test_engine_equivalence`` fails; needs a wheel rebuild in CI
   * - 4
     - ``schema.py``: v2 DDL, views, resolve SQL, migration SQL
     - low to write, **high blast radius** — land first, review hardest
   * - 5
     - ``feature.py``: ``FeatureSegment``, ``MultipartFeature``
     - **medium-high** — this is the compat surface. Rule: *override nothing*
   * - 6
     - ``ingest.py``: ``raw_id``/``occ``, resolve pass, nullable coords
     - **highest** — see below
   * - 7
     - ``interface.py``: schema gate, ``_n_multipart``, ``OR EXISTS``, prefetch, ``explode_segments``; ``delete()``/``update()`` must cascade to ``segments``
     - **high** — 8 SQL builders
   * - 8
     - ``migrate.py`` (new)
     - medium — must be idempotent and transactional
   * - 9
     - ``validate.py`` (new)
     - low
   * - 10
     - ``sqlite_export.py``: flattening
     - medium — verified by opening the output with real gffutils

**Specific hazards.**

- Making ``start``/``end`` nullable turns every ``start <= ?`` into three-valued
  logic, so NULL-coordinate rows silently disappear from ``region()``. That is
  the correct, gffutils-matching behaviour, but it must be a deliberate tested
  decision, and ``ST_MakeEnvelope`` must be CASE-guarded or the first ``.``
  coordinate fails the whole insert.

- Resolve must precede ``EDGES_FROM_PARENT``, and GTF synthesis must follow
  resolve. Getting the order wrong produces edges pointing at ids that no
  longer exist — and ``closure`` will silently drop them rather than error.

- ``delete()`` currently issues four ``DELETE``\ s and none touches ``segments``.
  Forgetting the fifth leaves orphans that ``segments_all`` joins back,
  resurrecting deleted data. INV-2 catches it.

- The ``OR EXISTS`` can defeat the R-tree if it is not written as a separate
  parenthesized conjunct.

----

.. _schema_v2--12-open-questions-all-resolved:

12. Open questions — all resolved
---------------------------------

1. ``export_sqlite`` **default flatten mode** — resolved as ``"gffutils"``
   (fan-out), and **hardcoded**: no ``flatten=`` parameter exists, because
   nothing wanted the alternative once the parity claim was the point.
   Verified by opening a gffbase export with real gffutils 0.14.

2. **Default** ``on_multipart_conflict`` — resolved as ``"error"``, as
   recommended. It only applies under ``mode="strict"``, which is opt-in, so
   refusing an ambiguous fusion is the conservative default; ``"split"``
   partitions the run instead.

3. **Serialization model** — resolved **opposite to the recommendation**, and
   worth recording as such. The recommendation was to match the oracle in
   ``str(feature)`` and expose the original line separately. What shipped is the
   reverse: ``str(feature)`` is byte-faithful, and ``to_line(normalized=True)``
   produces the oracle's rendering on demand.

   The deciding evidence arrived after this document was written. Once the
   attribute encoder existed, ``to_line(normalized=True)`` was measured against
   gffutils across the whole shared corpus and matched exactly on 11 of 15
   GFF3 fixtures — the four that differ are dialect-*inference* differences,
   not serialization ones. So both properties are available either way, and
   the question became which should be the default. Byte-faithful won because
   a round trip that silently rewrites a user's file is the more surprising
   default, and because ``_EMPTY_VALUE_RENDERING`` turned out to be a case where
   the ORACLE is lossy: it renders ``ID=`` as a bare ``ID``.
