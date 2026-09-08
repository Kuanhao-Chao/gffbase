.. _contact--contact:

Contact
=======

.. _contact--bugs-questions-and-feature-requests:

Bugs, questions and feature requests
------------------------------------

`Open a GitHub issue <https://github.com/Kuanhao-Chao/gffbase/issues>`__ —
that is the fastest route and it leaves an answer other people can find.

Please include:

- The **exact command or code** that fails, small enough to run.
- The **full traceback**, not just the last line.
- ``gffbase.__version__``, and whether ``gffbase.native_available()`` is ``True``.
- Your Python version and platform.
- Which **annotation** it happened on, and ideally a few lines that reproduce it.

For anything data-shaped, the smallest GFF3 that reproduces the problem is
worth more than a description of the file. A handful of lines is usually
enough.

.. code-block:: python

   import gffbase, sys
   print(gffbase.__version__, gffbase.native_available(), sys.version, sys.platform)

.. _contact--security:

Security
--------

Please **do not** open a public issue for a vulnerability. The reporting
process, the supported versions and what is in scope are in
:doc:`the security policy <security>`.

.. _contact--author:

Author
------

**Kuan-Hao Chao** — kuanhao.chao@gmail.com ·
`khchao.com <https://khchao.com/>`__ ·
`ORCID 0000-0003-3026-4266 <https://orcid.org/0000-0003-3026-4266>`__

.. _contact--contributing:

Contributing
------------

Pull requests are welcome. :doc:`CONTRIBUTING.md <contributing>` covers the
development setup, the test suite, and what a reviewable PR looks like.

----

.. _contact--other-tools:

Other tools
-----------

.. list-table::
   :header-rows: 1
   :widths: 50 50

   * - Tool
     - What it does
   * - `**LiftOn** <https://khchao.com/LiftOn/>`__
     - Accurate annotation lift-over combining DNA and protein alignment
   * - `**OpenSpliceAI** <https://khchao.com/OpenSpliceAI/>`__
     - An open, retrainable reimplementation of SpliceAI
   * - `**Splam** <https://khchao.com/splam/>`__
     - Splice-junction recognition and alignment cleanup
   * - `**Shorkie** <https://khchao.com/shorkie/>`__
     - Sequence-to-expression modelling in budding yeast
