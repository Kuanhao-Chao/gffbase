.. _quickstart--quickstart:

Quickstart
==========

Ten minutes, one annotation file, and the four things you will actually do
with it. If you are coming from ``gffutils``, read the
:doc:`Migration guide <migration>` instead — most of your code already works.

We use a small GFF3 throughout so you can follow along without a download.

.. code-block:: python

   from pathlib import Path

   Path("demo.gff3").write_text("""\
   ##gff-version 3
   chr1    demo    gene    1000    9000    .   +   .   ID=gene1;Name=BRCA-like
   chr1    demo    mRNA    1000    9000    .   +   .   ID=tx1;Parent=gene1
   chr1    demo    exon    1000    1200    .   +   .   ID=ex1;Parent=tx1
   chr1    demo    exon    3000    3902    .   +   .   ID=ex2;Parent=tx1
   chr1    demo    CDS 1050    1200    .   +   0   ID=cds1;Parent=tx1
   chr1    demo    CDS 3000    3500    .   +   2   ID=cds2;Parent=tx1
   """)

----

.. _quickstart--1-build-a-database:

1. Build a database
-------------------

.. code-block:: python

   from gffbase import create_db

   with create_db("demo.gff3", "demo.duckdb", force=True) as db:
       print(db.count_features_of_type())          # 6
       print(sorted(db.featuretypes()))            # ['CDS', 'exon', 'gene', 'mRNA']

``create_db`` auto-detects GFF3 vs GTF and reads gzipped input directly — you
never need to decompress a ``.gtf.gz`` first.

.. important::

   **Use ``with``, or call ``close()``**

   DuckDB takes an **exclusive lock** on the database file for the life of a
   writable handle. Until you release it, no other process can open that file
   for writing, and on Windows you cannot delete or replace it. The ``with``
   block above releases it at the end. See
   :doc:`Connections & concurrency <connections>`.

Ingest is a one-time cost. Everything below reopens the finished database:

.. code-block:: python

   from gffbase import FeatureDB

   db = FeatureDB("demo.duckdb")

----

.. _quickstart--2-fetch-a-feature-by-id:

2. Fetch a feature by ID
------------------------

.. code-block:: python

   gene = db["gene1"]

   print(gene.featuretype, gene.seqid, gene.start, gene.end, gene.strand)
   # gene chr1 1000 9000 +

   print(gene.attributes["Name"])   # ['BRCA-like']  — always a list
   print(str(gene))                 # the original line, byte-for-byte

Attributes are **always lists**, even for a single value. That is ``gffutils``'
contract and GFFBase keeps it, because column 9 genuinely permits repeats
(``Parent=a,b``).

----

.. _quickstart--3-walk-the-hierarchy:

3. Walk the hierarchy
---------------------

.. code-block:: python

   for tx in db.children("gene1", featuretype="mRNA"):
       exons = list(db.children(tx.id, featuretype="exon"))
       print(tx.id, "→", len(exons), "exons")

   # Everything below the gene, at any depth:
   print([f.id for f in db.children("gene1", level=None)])
   # ['tx1', 'ex1', 'ex2', 'cds1', 'cds2']

   # And back up:
   print([f.id for f in db.parents("ex1", level=None)])
   # ['gene1', 'tx1']

``level=1`` means direct children only; ``level=None`` means the whole subtree.
GFFBase materializes a transitive-closure table at ingest, so ``level=None`` is a
single indexed lookup rather than a recursive walk.

----

.. _quickstart--4-query-by-position:

4. Query by position
--------------------

.. code-block:: python

   for f in db.region("chr1:1000-1500"):
       print(f.featuretype, f.start, f.end)

   # Equivalent, and easier to build programmatically:
   for f in db.region(seqid="chr1", start=1000, end=1500, featuretype="exon"):
       print(f.id)

``region()`` picks its own index — an R-tree when one was built, a multi-column
B-tree otherwise. You do not choose, and both return the same features.

----

.. _quickstart--5-the-one-that-matters-at-scale:

5. The one that matters at scale
--------------------------------

Everything above returns Python ``Feature`` objects, one per row. That is the
right shape for exploring, and the wrong shape for feeding a model. When you
need every exon of fifty thousand transcripts, ask for them **all at once**:

.. code-block:: python

   transcript_ids = [f.id for f in db.features_of_type("mRNA")]

   exons = db.children_batched(transcript_ids, featuretype="exon", format="arrow")

   print(exons.schema.names)
   # ['anchor', 'descendant_id', 'seqid', ..., 'start', 'end', ...]

One SQL query, one Arrow table, **no Python ``Feature`` objects constructed at
any layer**. The ``anchor`` column carries the input ID for each row, so you can
regroup without re-querying:

.. code-block:: python

   import torch

   starts = torch.from_numpy(exons.column("start").to_numpy())
   ends = torch.from_numpy(exons.column("end").to_numpy())

``format="df"`` and ``format="polars"`` return pandas and polars instead.
``parents_batched()`` and ``region_batched()`` have the same contract.

.. warning::

   **Do not loop the row-by-row API over many IDs**

   ``for i in ids: db.children(i)`` is **slower in GFFBase than in ``gffutils``**
   — DuckDB pays vectorization startup on every call. That is not a bug; it is
   the trade an analytical engine makes. Use the ``_batched`` calls whenever you
   have more than a handful of anchors. The
   :doc:`Migration guide <migration>` explains the reasoning.

----

.. _quickstart--where-to-go-next:

Where to go next
----------------

- :doc:`Compatibility & strict modes <modes>` — what GFFBase does
  with real files that break the spec, and how to choose.

- :doc:`Usage gallery <usage_gallery>` — a snippet for every public method.
- :doc:`Cookbooks <cookbooks>` — GENCODE, RefSeq, MANE, and
  end-to-end ML pipelines.

- :doc:`Command line <cli>` — the same operations without writing Python.
