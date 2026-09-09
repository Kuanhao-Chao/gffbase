.. _cookbooks--cookbooks:

Cookbooks
=========

Concrete recipes for the most common real-world annotation workflows.

These pages target the real multi-GB corpora, which is what makes them
useful and also what stops most of their snippets running in CI. Snippets
that *can* run against the small vendored fixtures are executed by
``tests/test_docs_snippets.py``; the rest carry an explicit skip marker naming
the corpus they need. Fetch those with ``python benchmarks/download_corpora.py``.

.. list-table::
   :header-rows: 1
   :widths: 50 50

   * - Cookbook
     - Topic
   * - :doc:`GENCODE / Ensembl <cookbook_gencode_ensembl>`
     - Deeply nested gene → transcript → exon hierarchies
   * - :doc:`NCBI RefSeq <cookbook_refseq>`
     - Massive chromosome records, ``Dbxref``, ``Note``, ``gbkey`` tags
   * - :doc:`MANE <cookbook_mane>`
     - Filtering for ``tag=MANE_Select`` and ``tag=MANE_Plus_Clinical``
   * - :doc:`Machine Learning Workflows <cookbook_ml_workflows>`
     - Bulk feature extraction → PyArrow → Hugging Face / PyTorch with zero per-row Python overhead

.. _cookbooks--conventions:

Conventions
-----------

.. docs-test: skip reason="illustrative: needs a real annotation file"

.. code-block:: python

   from gffbase import create_db, FeatureDB

   # Build once...
   with create_db("annotation.gff3", "annotation.duckdb", force=True) as db:
       ...

   # ...then re-open for querying.
   with FeatureDB("annotation.duckdb") as db:
       ...

**Always close the handle** -- with a ``with`` block, or ``db.close()``. A
writable connection holds an exclusive lock on the database file, so an
unclosed handle stops any other process from opening it and, on Windows,
stops the file being replaced at all. For read-only fan-out across worker
processes, open with ``FeatureDB(path, read_only=True)``; see
:doc:`Connections & concurrency <connections>`.

The cookbooks assume ``gffbase`` is on the import path (``pip install gffbase``,
or ``pip install -e .`` from a checkout) and that DuckDB's spatial extension is
available -- it downloads on first ingest, and gffbase falls back to a
B-tree index if that download cannot happen.

----

.. toctree::
   :maxdepth: 1

   cookbook_gencode_ensembl
   cookbook_refseq
   cookbook_mane
   cookbook_ml_workflows
