.. _installation--installation:

Installation
============

.. warning::

   **0.2.0rc1 is still a release candidate**

   PyPI currently serves 0.1.0. The commands and APIs in this documentation
   describe the 0.2.0rc1 candidate; verify ``gffbase.__version__`` after installing.
   Until 0.2.0 is tagged, install the candidate from an exact reviewed commit
   rather than assuming an unpinned ``pip install`` supplies it.

.. code-block:: bash

   pip install gffbase

That will be the normal 0.2.0 installation after publication. GFFBase builds
**abi3 wheels**, so one binary per platform covers CPython 3.10 through 3.14
and **no Rust toolchain is needed when a matching wheel is available**.

.. list-table::
   :header-rows: 1
   :widths: 50 50

   * - Platform
     - Wheels
   * - Linux
     - ``x86_64``, ``aarch64`` (manylinux)
   * - macOS
     - ``x86_64``, ``arm64`` (Apple silicon)
   * - Windows
     - ``x86_64``

Two runtime dependencies come with it: **DuckDB** (the storage engine) and
**PyArrow** (the parser → storage hand-off, and the zero-copy return type).

----

.. _installation--optional-extras:

Optional extras
---------------

Each extra corresponds to a code path that either works when the extra is
installed or raises a precise installation error when it is not. Nothing
degrades silently.

.. list-table::
   :header-rows: 1
   :widths: 34 33 33

   * - Extra
     - Install
     - Unlocks
   * - ``pandas``
     - ``pip install gffbase[pandas]``
     - ``format="df"`` on the batched APIs
   * - ``polars``
     - ``pip install gffbase[polars]``
     - ``format="polars"`` on the batched APIs
   * - ``fasta``
     - ``pip install gffbase[fasta]``
     - ``Feature.sequence()`` via ``pyfaidx``
   * - ``pybedtools``
     - ``pip install gffbase[pybedtools]``
     - ``to_bedtool()``, ``tsses()``
   * - ``biopython``
     - ``pip install gffbase[biopython]``
     - ``to_seqfeature()``, ``from_seqfeature()``
   * - ``plot``
     - ``pip install gffbase[plot]``
     - ``gffbase.contrib.plotting``
   * - ``all``
     - ``pip install gffbase[all]``
     - every integration above

----

.. _installation--verifying-the-install:

Verifying the install
---------------------

.. code-block:: python

   import gffbase

   print(gffbase.__version__)
   print(gffbase.native_available())   # True when the Rust extension is loaded

``native_available()`` is the one worth checking. GFFBase ships a **pure-Python
fallback parser** that produces identical results, so a wheel-less install
still works — it is simply slower. If this prints ``False`` on a platform that
has wheels, something went wrong with the install rather than with your data.

.. note::

   **The spatial extension**

   Spatial (R-tree) indexing uses DuckDB's ``spatial`` extension, which DuckDB
   downloads on first use. On a machine without network egress the download
   fails, and GFFBase **falls back to a multi-column B-tree index** — same
   answers, lower throughput on ``region()`` queries. Nothing is raised, because
   the fallback is correct; ``db._rtree_built`` tells you which path a database
   is using.

----

.. _installation--installing-from-source:

Installing from source
----------------------

You need this only to work on GFFBase itself, or to build for a platform with
no wheel.

**Prerequisites:** Rust ≥ 1.83 and `maturin <https://www.maturin.rs/>`__ ≥ 1.5.

.. code-block:: bash

   git clone https://github.com/Kuanhao-Chao/gffbase
   cd gffbase

   pip install -e ".[dev,test,all]"
   maturin develop --release --manifest-path rust/Cargo.toml

``maturin develop`` compiles the Rust extension and installs it into the active
environment alongside the Python sources. Use ``--release``: a debug build of the
parser is roughly an order of magnitude slower and will make every benchmark
you run meaningless.

Confirm it worked:

.. code-block:: bash

   python -c "import gffbase; print(gffbase.native_available())"   # True
   pytest -q

.. tip::

   **Running** ``cargo test`` **on macOS**

   The Rust unit tests link against libpython, which is not on the default
   search path in a conda environment:

   .. code-block:: bash

      DYLD_FALLBACK_LIBRARY_PATH="$(python -c 'import sysconfig;print(sysconfig.get_config_var("LIBDIR"))')" \
          cargo test --release --manifest-path rust/Cargo.toml

----

.. _installation--supported-versions:

Supported versions
------------------

.. list-table::
   :header-rows: 1
   :widths: 50 50

   * - 
     - Supported
   * - Python
     - 3.10, 3.11, 3.12, 3.13, 3.14
   * - DuckDB
     - ≥ 1.4.1
   * - PyArrow
     - ≥ 18.1
   * - Rust (source builds only)
     - ≥ 1.83

The DuckDB and PyArrow floors are not aspirational: a dedicated CI job installs
exactly those versions and runs the whole suite against them.
