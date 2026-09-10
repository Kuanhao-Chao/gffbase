.. _troubleshooting--troubleshooting:

Troubleshooting
===============

Organised by the error you are looking at. For "how does this work" questions,
see the :doc:`FAQ <faq>`.

----

.. _troubleshooting--ingest:

Ingest
------

.. _troubleshooting--duplicateiderror-duplicate-id-cds-np_:

``DuplicateIDError: Duplicate ID cds-NP_...``
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

The file uses the **split-CDS convention**: several lines sharing one ``ID=``,
describing one discontinuous feature. RefSeq and MANE both do this.

``merge_strategy`` defaults to ``"error"`` — the same default ``gffutils`` uses, and
it raises on these same files — because there are two defensible readings and
picking one for you would give you a whole-genome answer you did not choose:

.. docs-test: skip reason="illustrative: ``path`` stands for the reader's file"

.. code-block:: python

   create_db(path, "out.duckdb", merge_strategy="create_unique")  # renamed, gffutils-style
   create_db(path, "out.duckdb", mode="strict")                   # one discontinuous feature

See :doc:`Compatibility & strict modes <modes>`.

.. _troubleshooting--gffformaterror-on-a-file-other-tools-read:

``GFFFormatError`` on a file other tools read
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

You are almost certainly in ``mode="strict"``. Real annotations break the spec
routinely; the default ``mode="compat"`` annotates violations instead of
rejecting them. The exception carries a pointer into the file:

.. docs-test: skip reason="illustrative: names a file the reader supplies"

.. code-block:: python

   import gffbase

   try:
       gffbase.create_db("annotation.gff3", "out.duckdb", mode="strict")
   except gffbase.GFFFormatError as exc:
       print(exc.line_no, exc.kind, exc.message)

To audit rather than abort, keep the strict rules but downgrade the action:

.. docs-test: skip reason="illustrative: ``path`` stands for the reader's file"

.. code-block:: python

   db = create_db(path, "out.duckdb", validation="ncbi", on_error="warn")
   for w in db.warnings:
       print(w["line_no"], w["kind"], w["message"])

.. _troubleshooting--ingest-used-a-lot-of-memory:

Ingest used a lot of memory
~~~~~~~~~~~~~~~~~~~~~~~~~~~

Two different costs get confused here, so take them apart.

**Ingest itself** peaks at roughly 7–10 GB on a whole-genome annotation, against
111–194 MB for ``gffutils``. DuckDB allocates a vectorized buffer pool; cap it
with ``PRAGMA memory_limit='512MB'`` if that matters more than wall time.

**Exhaustive validation** is what makes the published figures large: the numbers
in the performance tables span 3.4–62.0 GB because those runs call
``validate(level="full", sample=None)``, which re-reads every stored attribute.
**No default path does that** — ``validate_db`` defaults to ``sample=200`` and
the CLI never overrides it. If you asked for exhaustive validation on a
six-million-feature annotation, budget tens of gigabytes; otherwise you will not
see these numbers.

To cap DuckDB's own threads (and with them its memory):

.. code-block:: bash

   GFFBASE_THREADS=4 python your_script.py

.. _troubleshooting--the-ingest-died-and-left-a-file-behind:

The ingest died and left a file behind
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

It should not have. Ingest is atomic: GFFBase writes to
``<dbfn>.gffbase-building.<pid>`` and only renames on success, so a failed run
leaves the original untouched and the scratch file is removed. If you find a
``.gffbase-building.*`` file, the process was killed hard (``SIGKILL``, power loss)
and it is safe to delete.

Note that atomicity relies on ``os.replace`` being atomic, which holds **within
one filesystem**. If ``dbfn`` is on a different mount than its temporary
neighbour, the guarantee is weaker.

----

.. _troubleshooting--querying:

Querying
--------

.. _troubleshooting--region-returns-nothing-but-the-feature-is-there:

``region()`` returns nothing, but the feature is there
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

Check the seqid spelling. ``chr1``, ``1`` and ``NC_000001.11`` are three different
sequences as far as the database is concerned — GENCODE uses ``chr1``, Ensembl
uses ``1``, RefSeq uses ``NC_000001.11``.

.. code-block:: python

   print(sorted(db.seqids())[:5])

.. _troubleshooting--children-returns-nothing-for-a-feature-that-has-children:

``children()`` returns nothing for a feature that has children
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

Either the file has no ``Parent=`` edges to build the hierarchy from, or you are
asking for the wrong level. ``level=1`` is direct children only:

.. code-block:: python

   list(db.children("gene1", level=None))     # everything below, any depth

For GTF input, missing ``gene`` and ``transcript`` rows can be synthesized from
``gene_id`` / ``transcript_id`` attributes. Many modern GTFs—including GENCODE
v49—already contain explicit parent rows; disable inference when you want to
preserve exactly that supplied hierarchy. If parent rows and identifying
attributes are both absent, there is nothing to connect or synthesize.

.. _troubleshooting--iterating-is-slower-than-gffutils:

Iterating is slower than ``gffutils``
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

For a **loop over many IDs**, yes, and this is inherent rather than a bug —
DuckDB pays vectorization startup per call, which an OLTP engine like SQLite
does not. Use the batched APIs:

.. docs-test: skip reason="illustrative: contains an elided fragment"

.. code-block:: python

   # slow
   for tid in transcript_ids:
       for exon in db.children(tid, featuretype="exon"):
           ...

   # fast: one query, no Python Feature objects
   exons = db.children_batched(transcript_ids, featuretype="exon", format="arrow")

.. _troubleshooting--my-iteration-stopped-early:

My iteration stopped early
~~~~~~~~~~~~~~~~~~~~~~~~~~

Something else queried the same connection while the iteration was still
streaming. A DuckDB connection holds one result set at a time. Materialize
first, or use a second handle — see
:doc:`Connections & concurrency <connections>`.

----

.. _troubleshooting--databases-and-files:

Databases and files
-------------------

.. _troubleshooting--ioexception-could-not-set-lock-on-file:

``IOException: Could not set lock on file``
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

Another connection holds the database. Either another process has it open, or
an earlier handle in this process was never closed. Use ``with``, or call
``close()``. For concurrent readers, open with ``read_only=True``.

.. _troubleshooting--closeddatabaseerror:

``ClosedDatabaseError``
~~~~~~~~~~~~~~~~~~~~~~~

The handle was closed — usually by leaving a ``with`` block earlier than
intended. Open a new one.

.. _troubleshooting--schemaversionerror:

``SchemaVersionError``
~~~~~~~~~~~~~~~~~~~~~~

Either the database was written by a **newer** GFFBase than the one reading it
(upgrade GFFBase), or it is a schema-v1 database opened with
``upgrade="error"`` or ``read_only=True``. Migrate it once:

.. code-block:: bash

   gffbase migrate old.duckdb

.. _troubleshooting--has-gffbase-tables-but-no-metadata-at-all:

``... has gffbase tables but no metadata at all``
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

An ingest that failed part-way, or a truncated file. Rebuild it with
``create_db(..., force=True)``. GFFBase refuses to open it rather than handing
back an empty database that reports itself as complete.

.. _troubleshooting--the-database-is-larger-than-the-sqlite-one:

The database is larger than the SQLite one
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

Expected — 1.18× to 1.36× across the benchmark corpora. GFFBase stores a
materialized transitive closure and a long-form attributes table so that
hierarchy and attribute queries are indexed lookups instead of scans. That is
the trade.

----

.. _troubleshooting--environment:

Environment
-----------

.. _troubleshooting--cargo-does-not-understand-this-lock-file:

``this version of Cargo does not understand this lock file``
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

Building from source with a Rust older than 1.78. gffbase requires **Rust
≥ 1.83**, but this failure happens before Cargo reads the ``rust-version``
field, so the message names neither gffbase nor the version you need:

.. code-block:: text

   error: failed to parse lock file at: .../rust/Cargo.lock
   Caused by: lock file version `4` was found, but this version of Cargo
   does not understand this lock file
   💥 maturin failed

Check what ``pip`` will actually use, which is not always what ``cargo
--version`` reports in your working directory — a rustup directory override
applies only inside the directory it names, and building an unpacked sdist
happens outside it:

.. code-block:: bash

   cargo --version                 # in the directory you are building in
   rustup toolchain install 1.83   # if it is older than 1.83
   RUSTUP_TOOLCHAIN=1.83 pip install gffbase

Installing a wheel needs no Rust at all; this only affects source builds.

.. _troubleshooting--native_available-returns-false:

``native_available()`` returns ``False``
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

The Rust extension did not load, so the pure-Python fallback parser is running.
Results are identical; throughput is not. On a platform with wheels this means
the install went wrong — try ``pip install --force-reinstall gffbase``. From a
source checkout, run ``maturin develop --release --manifest-path rust/Cargo.toml``.

.. _troubleshooting--spatial-queries-are-slower-than-the-published-numbers:

Spatial queries are slower than the published numbers
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

Check which index the database is using:

.. code-block:: python

   db._rtree_built     # False → B-tree fallback

DuckDB downloads its ``spatial`` extension on first use. On a machine without
network egress that fails and GFFBase falls back to a multi-column B-tree —
correct answers, lower throughput. Nothing is raised, because the fallback is
not an error.

.. _troubleshooting--importerror-mentioning-polars-pandas-pyfaidx-pybedtools:

``ImportError`` mentioning polars / pandas / pyfaidx / pybedtools
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

An optional integration whose extra is not installed. The message names the
exact command:

.. code-block:: bash

   pip install gffbase[polars]
   pip install gffbase[all]

----

.. _troubleshooting--compatibility:

Compatibility
-------------

.. _troubleshooting--is-it-really-a-drop-in-replacement-for-gffutils:

Is it really a drop-in replacement for ``gffutils``?
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

For the large majority of code, yes — ``import gffbase as gffutils`` and
carry on. The differences that remain are declared and tested in
``tests/parity/deviations.toml``, and the one behavioural gotcha (row-by-row
loops) is covered in the :doc:`Migration guide <migration>`.

.. _troubleshooting--can-i-hand-the-database-to-a-tool-that-expects-gffutils:

Can I hand the database to a tool that expects ``gffutils``?
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

Yes. Export a real SQLite database that ``gffutils`` itself can open:

.. code-block:: python

   from gffbase import export_sqlite
   export_sqlite(db.conn, "legacy.db")   # a connection, not the FeatureDB

.. _troubleshooting--which-python-versions-are-supported:

Which Python versions are supported?
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

3.10 through 3.14, one abi3 wheel per platform.
