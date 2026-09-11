.. _citation--citation:

Citation
========

If gffbase contributes to your research, please cite it.

..

   Chao, K.-H. (2026). *GFFBase: Rust-accelerated GFF3/GTF parser with a
   DuckDB-backed storage engine and zero-copy PyArrow interface* (Version 0.2.0)
   [Computer software]. https://github.com/Kuanhao-Chao/gffbase

.. _citation--bibtex:

BibTeX
------

.. code-block:: bibtex

   @software{chao_gffbase_2026,
     author  = {Chao, Kuan-Hao},
     title   = {{GFFBase}: Rust-accelerated GFF3/GTF parser with a
                DuckDB-backed storage engine and zero-copy PyArrow interface},
     year    = 2026,
     version = {0.2.0},
     url     = {https://github.com/Kuanhao-Chao/gffbase},
   }

The repository also ships a `CITATION.cff <https://github.com/Kuanhao-Chao/gffbase/blob/main/CITATION.cff>`__,
so GitHub's **"Cite this repository"** button has machine-readable metadata.
Per-version DOIs, when available, are tracked on the
`releases page <https://github.com/Kuanhao-Chao/gffbase/releases>`__.

----

.. _citation--please-also-cite-what-gffbase-is-built-on:

Please also cite what gffbase is built on
-----------------------------------------

gffbase is a successor to ``gffutils`` and reuses its API, its test corpus and
its semantics. If you are porting from it, cite it too:

   Dale, R. ``gffutils``: a Python package for working with GFF and GTF files.
   https://github.com/daler/gffutils

The storage engine and the columnar interface are likewise other people's
work:

- **DuckDB** — Raasveldt, M. & Mühleisen, H. (2019). *DuckDB: an Embeddable
  Analytical Database.* SIGMOD.

- **Apache Arrow** — https://arrow.apache.org/

.. _citation--and-cite-the-annotation-you-used:

And cite the annotation you used
--------------------------------

A result computed from GENCODE, RefSeq, MANE or CHESS depends on that
annotation as much as on the software that read it. Each publisher asks to be
cited; see :doc:`Datasets <datasets>` for the sources.
