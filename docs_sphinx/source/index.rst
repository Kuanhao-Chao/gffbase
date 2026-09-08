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

      Head-to-head benchmarks on every canonical human annotation, with the
      provenance of each run recorded.

   .. grid-item-card:: 🧰 Work from the command line
      :link: content/cli
      :link-type: doc

      Build, inspect, validate, migrate and export a database without writing
      Python.

----

Install
-------

.. code-block:: bash

   pip install gffbase

.. warning::

   PyPI currently provides 0.1.0. This site describes the unreleased 0.2.0
   candidate; verify the installed version, and use an exact reviewed commit
   for candidate testing.

Quick start
-----------

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
   content/release_checklist
   content/changelog
   content/contributing
   content/security
