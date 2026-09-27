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
      :link: usage_gallery--3-spatial-queries
      :link-type: ref

      Ask for a region and get the features that overlap it, from an R-tree
      index built during ingest.

   .. grid-item-card:: 🌳 Walk the annotation hierarchy
      :link: usage_gallery--2-standard-relational-queries
      :link-type: ref

      ``children``, ``parents`` and the batched forms, backed by a transitive
      closure so depth costs nothing at query time.

   .. grid-item-card:: 🔄 Migrate from gffutils
      :link: content/migration
      :link-type: doc

      The legacy API is preserved surface-for-surface. Most scripts move by
      changing one import.

   .. grid-item-card:: ⚡ Extract in bulk, without a Python loop
      :link: usage_gallery--5-vectorized-ml-api-the-ones-that-make-gffbase-fast
      :link-type: ref

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

   .. grid-item-card:: 🧹 Read the files real sources publish
      :link: content/noisy_files
      :link-type: doc

      Byte-order marks, CDS-only and AUGUSTUS GTF, Liftoff ids, tar archives,
      truncated gzip: each read as it was meant, and reported.

   .. grid-item-card:: 🎛️ Tune memory and threads
      :link: content/tuning
      :link-type: doc

      The defaults and why they were chosen, what ingest costs stage by
      stage, and when a loop should become one batched query.

----

Measured performance
--------------------

Every figure below is generated from the committed benchmark artifact, never
typed in. See :doc:`content/performance` for the full sweep and
:doc:`content/methodology` for how it was run.

**gffbase ingests every corpus faster**, 1.92× to 3.62× against ``gffutils``,
into a smaller database. Batched extraction, spatial indexing and SQL over the
whole corpus come on top. ``peak RSS`` is ingest plus exhaustive validation,
not what the default path costs.

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

----

Install
-------

.. code-block:: bash

   pip install gffbase

.. note::

   This site documents gffbase 0.3.0, released 2026-09-27: ingest 2.4-2.9x
   faster, in under a third of the memory on whole-genome files; prefetched
   loops; and noisy files read as they were meant (see the
   :doc:`changelog <content/changelog>`). Upgrading from 0.2.0 fixes defects
   that silently lost data; upgrading from 0.1.0 also fixes two SQL injection
   vulnerabilities; see the
   :doc:`security advisory <content/advisory_sql_injection>`.

Quick start
-----------

.. docs-test: skip reason="needs the GENCODE v49 corpus"

.. code-block:: python

   from gffbase import create_db

   with create_db("gencode.v49.annotation.gtf.gz", "gencode.duckdb") as db:
       # GENCODE ids carry their version.
       for tx in db.children("ENSG00000139618.19", featuretype="transcript"):
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
   content/noisy_files
   content/cookbooks

.. toctree::
   :maxdepth: 2
   :caption: Background

   content/performance
   content/tuning
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
   content/advisory_sql_injection
