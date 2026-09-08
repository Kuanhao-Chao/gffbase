.. _gallery--analysis-gallery:

Analysis gallery
================

Complete analyses, each one a real question with the code that answers it and
the output it actually produces. Every Python block on this page runs in CI
against the vendored ``tests/data/hierarchy.gff3`` fixture, so the numbers below
are what the code prints — not what it printed once.

For per-method snippets see the :doc:`Usage gallery <usage_gallery>`; for
corpus-specific workflows see the :doc:`Cookbooks <cookbooks>`.

The setup every example below assumes:

.. code-block:: python

   from gffbase import create_db

   db = create_db(HIERARCHY_PATH, str(TMP / "gallery.duckdb"), force=True)

----

.. _gallery--extract-splice-sites:

Extract splice sites
--------------------

Every donor and acceptor in the annotation — the input a splice-site model
like `Splam <https://khchao.com/splam/>`__ or
`OpenSpliceAI <https://khchao.com/OpenSpliceAI/>`__ trains on. An intron is the
gap between consecutive exons of the same transcript, so this is one sort and
one pairwise walk.

.. code-block:: python

   for tx in db.features_of_type("mRNA"):
       exons = sorted(db.children(tx.id, featuretype="exon"), key=lambda e: e.start)
       for left, right in zip(exons, exons[1:]):
           print(
               f"{tx.id}  donor {left.end + 1}  acceptor {right.start - 1}  "
               f"intron {right.start - left.end - 1} bp"
           )

.. docs-test: skip reason="console output of the block above"

.. code-block:: text

   t1  donor 201  acceptor 499  intron 299 bp

.. tip::

   **At whole-genome scale, batch it**

   On a real annotation, fetch every exon in one query instead of looping:
   ``db.children_batched(transcript_ids, featuretype="exon", format="arrow")``
   returns an Arrow table with an ``anchor`` column naming each row's
   transcript, so you can group without re-querying. See
   :doc:`Machine learning workflows <cookbook_ml_workflows>`.

``create_introns()`` gives you the intron features directly if you want them as
``Feature`` objects rather than coordinates.

----

.. _gallery--pick-the-longest-isoform-per-gene:

Pick the longest isoform per gene
---------------------------------

The standard reduction to one transcript per gene — by **exonic** length, not
genomic span, which is what ``children_bp`` measures.

.. code-block:: python

   for gene in db.features_of_type("gene"):
       isoforms = list(db.children(gene.id, featuretype="mRNA"))
       longest = max(isoforms, key=lambda t: db.children_bp(t, child_featuretype="exon"))
       span = db.children_bp(longest, child_featuretype="exon")
       print(f"{gene.id}: {longest.id} ({span} exonic bp of {len(isoforms)} isoforms)")

.. docs-test: skip reason="console output of the block above"

.. code-block:: text

   g1: t1 (202 exonic bp of 2 isoforms)

For a curated answer rather than a computed one, MANE ships exactly one
representative transcript per gene — see :doc:`MANE <cookbook_mane>`.

----

.. _gallery--exon-count-distribution:

Exon-count distribution
-----------------------

Single-exon genes behave differently from multi-exon ones in almost every
analysis, so this histogram is worth looking at before trusting a result.

.. code-block:: python

   from collections import Counter

   counts = Counter(
       len(list(db.children(t.id, featuretype="exon")))
       for t in db.features_of_type("mRNA")
   )
   for n_exons, n_tx in sorted(counts.items()):
       print(f"{n_exons} exon(s): {n_tx} transcript(s)")

.. docs-test: skip reason="console output of the block above"

.. code-block:: text

   1 exon(s): 1 transcript(s)
   2 exon(s): 1 transcript(s)

----

.. _gallery--coding-fraction-per-transcript:

Coding fraction per transcript
------------------------------

How much of each transcript is protein-coding. A transcript with no CDS is a
non-coding isoform, which this makes visible rather than silently averaging
away.

.. code-block:: python

   for tx in db.features_of_type("mRNA"):
       coding = db.children_bp(tx, child_featuretype="CDS")
       exonic = db.children_bp(tx, child_featuretype="exon")
       pct = 100 * coding / exonic if exonic else 0
       print(f"{tx.id}: {coding}/{exonic} bp coding ({pct:.0f}%)")

.. docs-test: skip reason="console output of the block above"

.. code-block:: text

   t1: 162/202 bp coding (80%)
   t2: 0/201 bp coding (0%)

----

.. _gallery--hand-the-whole-annotation-to-pandas:

Hand the whole annotation to pandas
-----------------------------------

When the analysis is columnar, skip ``Feature`` objects entirely.
:doc:`to_table() <api>` takes the same filters as ``all_features()`` and
returns Arrow, pandas or polars.

.. code-block:: python

   exons = db.to_table("exon", format="arrow")
   print(exons.num_rows, "exons;", exons.schema.names[:6])

.. docs-test: skip reason="console output of the block above"

.. code-block:: text

   3 exons; ['id', 'seqid', 'source', 'featuretype', 'start', 'end']

Restrict to a locus with ``limit=``, and get a dataframe instead:

.. docs-test: pandas

.. code-block:: python

   region = db.to_table("exon", limit=("chr1", 100, 300), format="df")
   print(len(region), "exons in chr1:100-300")

----

.. _gallery--what-is-in-an-unfamiliar-annotation:

What is in an unfamiliar annotation?
------------------------------------

Before any of the above, from the shell:

.. code-block:: bash

   gffbase stats annotations.duckdb

Feature counts by type, sequence count and names, and how the database was
built. See :doc:`Command line <cli>`.

----

.. _gallery--where-to-go-next:

Where to go next
----------------

- :doc:`Cookbooks <cookbooks>` — the same techniques against GENCODE,
  RefSeq and MANE, at whole-genome scale.

- :doc:`Machine learning workflows <cookbook_ml_workflows>` —
  bulk extraction into PyTorch and Hugging Face with no per-row Python.

- :doc:`Usage gallery <usage_gallery>` — one snippet per public method.
