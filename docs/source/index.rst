.. _main:

GFFBase
=======

**A genomic-annotation engine built for whole-genome scale: a Rust GFF3/GTF
parser, DuckDB columnar storage, and a zero-copy PyArrow interface — with the**
``gffutils`` **API preserved, so most code migrates by changing one import.**

GFFBase reads GFF3 and GTF at whole-genome scale and answers the questions a
genomics pipeline actually asks — spatial queries over intervals, hierarchy
walks from gene to transcript to exon, and bulk extraction into Arrow, pandas
or polars without a Python loop in the middle. The storage engine is DuckDB,
so a database is one portable file and every query is columnar.

.. image:: https://img.shields.io/pypi/v/gffbase.svg
   :target: https://pypi.org/project/gffbase/
   :alt: PyPI

.. image:: https://img.shields.io/pypi/pyversions/gffbase.svg
   :target: https://pypi.org/project/gffbase/
   :alt: Python versions

.. image:: https://img.shields.io/badge/license-Apache%202.0-blue.svg
   :target: https://github.com/Kuanhao-Chao/gffbase/blob/main/LICENSE
   :alt: License

----

What you can do with GFFBase
----------------------------

.. grid:: 1 1 2 2
   :gutter: 3

   .. grid-item-card:: 🧬 Query intervals at genome scale
      :link: content/usage_gallery
      :link-type: doc

      Ask for a region and get the features that overlap it, from an R-tree
      index built during ingest.

   .. grid-item-card:: 🌳 Walk the annotation hierarchy
      :link: content/usage_gallery
      :link-type: doc

      ``children``, ``parents`` and the batched forms, backed by a transitive
      closure so depth costs nothing at query time.

   .. grid-item-card:: 🔄 Migrate from gffutils
      :link: content/migration
      :link-type: doc

      The legacy API is preserved surface-for-surface. Most scripts move by
      changing one import.

   .. grid-item-card:: ⚡ Extract in bulk, without a Python loop
      :link: content/usage_gallery
      :link-type: doc

      ``children_batched(format="arrow")`` returns one Arrow table for
      thousands of anchors.

   .. grid-item-card:: 📊 Read the measured numbers
      :link: content/performance
      :link-type: doc

      Head-to-head benchmarks on the canonical human annotations, with the
      machine, versions and commit of each run recorded.

   .. grid-item-card:: 🧰 Work from the command line
      :link: content/cli
      :link-type: doc

      Build, inspect, validate and migrate a database without writing
      Python.

----

Measured performance
--------------------

Every figure below is generated from the committed benchmark artifact, never
typed in. See :doc:`content/performance` for the full sweep and
:doc:`content/methodology` for how it was run.

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

----

Install
-------

.. code-block:: bash

   pip install gffbase

.. warning::

   PyPI currently provides 0.1.0. This site describes the unreleased 0.2.0rc1
   candidate; verify the installed version, and use an exact reviewed commit
   for candidate testing.

Quick start
-----------

.. docs-test: skip reason="needs the GENCODE v49 corpus"

.. code-block:: python

   from gffbase import create_db

   with create_db("gencode.v49.annotation.gtf.gz", "gencode.duckdb") as db:
       for tx in db.children("ENSG00000139618", featuretype="transcript"):
           print(tx.id, tx.start, tx.end)

       for feature in db.region("chr17:43044295-43125483", featuretype="exon"):
           print(feature)

See :doc:`content/installation` for the supported platforms, and
:doc:`content/quickstart` for a worked example that runs end to end.

----

.. toctree::
   :maxdepth: 2
   :caption: Getting started

   content/installation
   content/quickstart
   content/modes

.. toctree::
   :maxdepth: 2
   :caption: Using GFFBase

   content/usage_gallery
   content/gallery
   content/cli
   content/connections
   content/migration
   content/cookbooks

.. toctree::
   :maxdepth: 2
   :caption: Background

   content/performance
   content/methodology
   content/datasets
   content/architecture
   content/schema_v2

.. toctree::
   :maxdepth: 2
   :caption: Reference

   content/api
   content/faq
   content/troubleshooting
   content/citation
   content/license
   content/contact

.. toctree::
   :maxdepth: 2
   :caption: Project

   content/testing
   content/roadmap
   content/changelog
   content/contributing
   content/security
