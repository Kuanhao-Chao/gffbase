.. _contributing--contributing-to-gffbase:

Contributing to GFFBase
=======================

Thanks for your interest in contributing to GFFBase. This document is
the canonical guide for setting up a development environment, running
the test suite, and shepherding a change from idea to merged PR.

GFFBase is a hybrid Rust + Python project: a SIMD parser written in
Rust (under ``rust/``) is compiled into a Python extension via PyO3 +
maturin and consumed by the Python public API (under
``python/gffbase/``). Most contributions touch one or the other; a few
touch both.

By participating in this project, you agree to abide by our
`Code of Conduct <https://github.com/Kuanhao-Chao/gffbase/blob/main/CODE_OF_CONDUCT.md>`__.

----

.. _contributing--1-quick-links:

1. Quick links
--------------

.. list-table::
   :header-rows: 1
   :widths: 50 50

   * - What
     - Where
   * - Bug reports
     - `open an issue <https://github.com/Kuanhao-Chao/gffbase/issues/new?template=bug_report.md>`__
   * - Feature requests
     - `open an issue <https://github.com/Kuanhao-Chao/gffbase/issues/new?template=feature_request.md>`__
   * - Discussions / questions
     - `GitHub Discussions <https://github.com/Kuanhao-Chao/gffbase/discussions>`__
   * - Performance & architecture notes
     - `Performance <https://khchao.com/gffbase/content/performance.html>`__
   * - Migration from ``gffutils``
     - `Migration guide <https://khchao.com/gffbase/content/migration.html>`__
   * - API reference
     - :doc:`api` (rendered by ``make -C docs html``)

----

.. _contributing--2-prerequisites:

2. Prerequisites
----------------

You will need:

- **Python 3.10 – 3.14** (any one of them; the CI matrix tests 3.10 / 3.12 / 3.14)
- **Rust >= 1.83** with ``cargo`` on ``$PATH``. Install via
  `rustup <https://rustup.rs/>`__.

- **maturin ≥ 1.5** for building the Rust extension.
- **git**, **make** (optional, for convenience targets), and a working
  C toolchain (clang on macOS, gcc on Linux, MSVC on Windows).

Verify:

.. code-block:: bash

   python --version          # 3.10-3.14
   rustc --version           # >= 1.83
   maturin --version         # ≥ 1.5

----

.. _contributing--3-development-setup:

3. Development setup
--------------------

.. _contributing--31-clone-and-create-a-virtual-environment:

3.1 Clone and create a virtual environment
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

.. code-block:: bash

   git clone https://github.com/Kuanhao-Chao/gffbase.git
   cd gffbase
   python -m venv .venv
   source .venv/bin/activate      # Windows: .venv\Scripts\activate

.. _contributing--32-install-python-dev-dependencies:

3.2 Install Python dev dependencies
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

.. code-block:: bash

   pip install -e .[dev,test,docs]

This installs:
- The runtime: ``duckdb``, ``pyarrow``.
- Test tools: ``pytest``, ``pytest-cov``, ``pyyaml`` (the release-hygiene suite parses the workflows).
- Lint tools: ``ruff``, ``mypy``.
- Doc tools: ``Sphinx``, ``furo``, ``sphinx-design``, ``sphinx-copybutton``. The docs extra needs Python 3.12 or newer, because Sphinx 9 does.
- Build tools: ``maturin``.

.. _contributing--33-build-the-rust-extension-in-place:

3.3 Build the Rust extension in-place
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

.. code-block:: bash

   maturin develop --release

``maturin develop`` compiles ``rust/src/lib.rs`` into
``gffbase._native`` and drops the resulting shared library into your
``python/gffbase/`` source tree, so ``import gffbase`` resolves
immediately. Use ``--release`` for benchmarking; omit for a faster
debug-build cycle.

Rebuild after **any** change under ``rust/``:

.. code-block:: bash

   maturin develop --release

.. _contributing--34-verify-your-install:

3.4 Verify your install
~~~~~~~~~~~~~~~~~~~~~~~

.. code-block:: bash

   python -c "import gffbase; print(gffbase.__version__)"      # 0.2.0rc1
   python -c "from gffbase import native_available; print(native_available())"   # True

If ``native_available()`` returns ``False``, the Rust extension didn't
build — check ``maturin develop`` output for compilation errors.

.. _contributing--35-optional-extras:

3.5 Optional extras
~~~~~~~~~~~~~~~~~~~

.. list-table::
   :header-rows: 1
   :widths: 50 50

   * - Extra
     - When you need it
   * - ``pandas``
     - If you want to test the ``format="df"`` path.
   * - ``polars``
     - If you want to test the ``format="polars"`` zero-copy path.
   * - ``fasta``
     - pyfaidx, for ``Feature.sequence(fasta=str_path)``.
   * - ``all``
     - All three at once.
   * - ``gffutils>=0.13``
     - If you're running ``benchmarks/06_mega.py`` or any differential-correctness test against the legacy library.

.. code-block:: bash

   pip install -e '.[all]' gffutils

----

.. _contributing--4-running-the-test-suite:

4. Running the test suite
-------------------------

GFFBase's correctness story rests on two things: the unit tests
(currently 530, all passing) and the differential-correctness checks against
legacy ``gffutils`` in the benchmark harness.

.. _contributing--41-full-unit-suite-coverage:

4.1 Full unit suite + coverage
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

.. code-block:: bash

   pytest                                  # tests only
   pytest --cov=gffbase --cov-report=term  # tests + coverage

Coverage is **not** part of the default invocation — a bare ``pytest`` reports on
tests, not on coverage. CI applies the gate explicitly with
``--cov-fail-under``; see ``.github/workflows/ci.yml`` for the enforced threshold.

The default invocation also writes:
- ``coverage.xml`` — for codecov / Codacy / your CI's coverage parser.
- ``htmlcov/`` — open ``htmlcov/index.html`` in a browser for an
interactive missing-line view.

.. _contributing--42-faster-iteration-during-development:

4.2 Faster iteration during development
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

.. code-block:: bash

   # Run a single file:
   pytest tests/test_featuredb_basic.py -q

   # Run a single test:
   pytest tests/test_featuredb_basic.py::test_seqids_iter -q

   # Stop at the first failure:
   pytest -x

   # Put the scratch databases somewhere with room (see below):
   pytest --basetemp=/path/with/space

..

   **Disk:** a full run needs **~4 GB of temporary space**. DuckDB pre-allocates
   every database file it creates and the suite builds a lot of them;
   ``tmp_path_retention_policy = "failed"`` keeps only failed tests' directories,
   but nothing is reclaimed until the run ends. If ``/tmp`` is small, point
   ``--basetemp`` somewhere with room.

.. _contributing--43-targeted-coverage-check:

4.3 Targeted coverage check
~~~~~~~~~~~~~~~~~~~~~~~~~~~

.. code-block:: bash

   pytest --cov=gffbase --cov-report=term-missing tests/test_ingest_basic.py

The ``term-missing`` report prints uncovered lines per module. Use it
when you've added new code in a specific module to make sure your
tests exercise every branch.

.. _contributing--44-test-the-b-tree-fallback-path:

4.4 Test the B-tree fallback path
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

GFFBase auto-picks an R-tree (when DuckDB's spatial extension loads)
or a multi-column B-tree (everywhere else). The CI matrix tests both:

.. code-block:: bash

   GFFBASE_TEST_DISABLE_RTREE=1 pytest

This forces the B-tree path for every test. Both paths must pass.

.. _contributing--45-linting:

4.5 Linting
~~~~~~~~~~~

.. code-block:: bash

   ruff check python/gffbase tests benchmarks tools
   ruff format --check python/gffbase tests benchmarks tools

Fix automatically with:

.. code-block:: bash

   ruff check --fix python/gffbase tests benchmarks tools
   ruff format python/gffbase tests benchmarks tools

CI requires both to pass.

.. _contributing--46-documentation:

4.6 Documentation
~~~~~~~~~~~~~~~~~

.. code-block:: bash

   make -C docs html            # build -> docs/build/html/index.html
   make -C docs html SPHINXOPTS="-W --keep-going"   # what CI runs

``-W`` is mandatory before opening a docs PR: it turns a broken
cross-reference or a page missing from the toctree into an error.

.. _contributing--47-benchmarks-optional:

4.7 Benchmarks (optional)
~~~~~~~~~~~~~~~~~~~~~~~~~

Long-running — several hours for a full sweep. Don't run as part of normal
development.

.. code-block:: bash

   pip install -e ".[bench,all]"
   python benchmarks/download_corpora.py     # ~5 min, ~257 MB

   # Full sweep. Keeps the GENCODE GFF3 databases for stages 01-05.
   python benchmarks/06_mega.py --legacy-timeout 5400 --keep-db gencode-gff3

   # Publish: copy the measurements in, then regenerate every table from them.
   cp benchmarks/out/06_mega.json benchmarks/results/06_mega.json
   python tools/gen_benchmark_tables.py --write

**Disk.** The five corpus pairs total ~38 GiB. Each is purged as soon as its
numbers are recorded, which holds the peak near 16 GiB; the harness refuses to
start a corpus it cannot finish. Set ``GFFBASE_BENCH_OUT`` to use another volume.

**Never hand-edit a published benchmark table.** They live between
``<!-- BEGIN GENERATED: ... -->`` markers and are rendered from
``benchmarks/results/06_mega.json``.
``tools/gen_benchmark_tables.py --check`` runs in the test suite and will fail.

**Never write a number that was not measured.** A run killed at the safety
valve reports ``state: timed_out``, ``wall_seconds: null``, and ``cap_seconds``; it
renders as censored and produces no speedup or speedup floor. The generator
refuses current results that revive the old lower-bound fields or pair a
timeout with a wall. This is not a style preference — the previous harness
multiplied a timeout by two and that invented figure became a headline claim.

----

.. _contributing--5-the-pull-request-process:

5. The pull-request process
---------------------------

.. _contributing--51-before-you-start:

5.1 Before you start
~~~~~~~~~~~~~~~~~~~~

- **Open an issue first** for any non-trivial change. A 50-line
  cleanup PR is welcome out of the blue; a new public-API surface or
  schema change should be discussed in an issue before code is
  written.

- **Search existing issues** to avoid duplicates.
- **Pick the right scope.** One PR = one logical change. A
  refactor + a feature = two PRs.

.. _contributing--52-branch-naming:

5.2 Branch naming
~~~~~~~~~~~~~~~~~

::

   feat/<short-description>     # new feature
   fix/<issue-number>-<gist>    # bug fix
   docs/<area>                  # docs-only change
   chore/<topic>                # tooling, deps, CI
   perf/<area>                  # measurable perf change

.. _contributing--53-commit-messages:

5.3 Commit messages
~~~~~~~~~~~~~~~~~~~

We use `Conventional Commits <https://www.conventionalcommits.org/>`__
loosely. The first line is what shows up in ``git log --oneline`` and
the GitHub release notes — make it count.

::

   feat(parser): support GFF3 directives spanning multiple lines
   fix(ingest): handle RefSeq duplicate-ID rows on Windows path separators
   docs(cookbook): add MANE.select example
   chore(deps): bump duckdb to 1.6.0
   perf(rtree): build index inline during Arrow batch insert

For multi-paragraph rationale, use the body of the commit — explain
**why**, not what (the diff already says what).

.. _contributing--54-what-every-pr-must-include:

5.4 What every PR must include
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

- ☐ **Tests.** New behaviour: a new test. Bug fix: a regression
  test that fails on ``main`` and passes on your branch.

- ☐ **Coverage not regressed.** Run
  ``pytest --cov=gffbase --cov-report=term`` and confirm.

- ☐ **``ruff check`` and ``ruff format --check`` clean.** No new lint warnings
  and no formatting drift.

- ☐ **Rust clean** if you touched ``rust/``:
  ``cargo fmt --manifest-path rust/Cargo.toml --all -- --check``,
  ``cargo clippy --manifest-path rust/Cargo.toml --all-targets -- -D warnings``,
  and ``cargo test --manifest-path rust/Cargo.toml``.

- ☐ **Documentation updated** if you touched a public API: the
  docstring, the migration guide, the cookbook, or the API
  reference.

- ☐ **``make -C docs html SPHINXOPTS="-W --keep-going"`` clean** if you
  touched ``docs/``.
- ☐ **No unrelated changes.** A 200-line diff in a feature PR
  should not include a ``ruff`` reformat of an unrelated file.

- ☐ **Apache-2.0 header** on any new ``.py`` or ``.rs`` source file
  (copy from any existing file — same block).

The PR template that opens automatically when you click *New PR*
encodes all of the above as a checklist.

.. _contributing--55-reviews:

5.5 Reviews
~~~~~~~~~~~

- Two-eyes rule: at least one maintainer approval before merge.
- Maintainers may request changes; please don't take it personally —
  GFFBase is the data backbone for ML pipelines that train on
  whole-human-genome corpora, so the bar for correctness is high.

- We squash-merge by default, so make your PR commit history clean
  before requesting review.

.. _contributing--56-after-merge:

5.6 After merge
~~~~~~~~~~~~~~~

- Your contribution is now under the Apache 2.0 license.
- We will mention your handle in the release notes.

----

.. _contributing--6-architecture-pointers-for-new-contributors:

6. Architecture pointers for new contributors
---------------------------------------------

If you're trying to find your way around:

- ``rust/src/parser.rs`` — the SIMD line/tab splitter and the canonical
  11-tuple shape passed to Python.

- ``python/gffbase/parser.py`` — the dispatcher that picks Rust vs.
  pure-Python fallback.

- ``python/gffbase/ingest.py`` — the DuckDB ingestion engine
  (``_ArrowBatchBuilder``, ``from_file``, GTF synthesis, R-tree finalize).

- ``python/gffbase/interface.py`` — ``FeatureDB`` and the smart
  R-tree/B-tree + closure/dynamic-CTE query routers.

- ``python/gffbase/schema.py`` — the 11-table DuckDB schema (plus 3 compatibility views) (one source
  of truth).

- ``tests/conftest.py`` — every shared fixture.
- ``tests/test_coverage_gaps.py`` — the targeted edge-case suite.

``CHANGELOG.md`` records what changed in each release and why — useful when you
want to know *why* something works the way it does, not just what the current
code says.

----

.. _contributing--7-things-we-will-not-accept:

7. Things we will *not* accept
------------------------------

- Changes that drop the coverage gate.
- Performance "wins" that aren't backed by a benchmark in
  ``benchmarks/`` showing the before/after delta.

- New external runtime dependencies without an issue discussing
  necessity vs. an inline implementation.

- Breaking changes to the public API without a migration note in
  ``MIGRATION.md`` and a deprecation cycle of at least one minor
  release.

- Vendored copies of upstream code without a clear license-compatible
  attribution.

----

.. _contributing--8-questions:

8. Questions?
-------------

Open a `GitHub Discussion <https://github.com/Kuanhao-Chao/gffbase/discussions>`__
or ping the maintainer at ``kuanhao.chao@gmail.com``. Issues are for
bugs and feature requests; discussions are for "how do I…" and "is
this the right approach for…".

Thanks for helping make GFFBase better.
