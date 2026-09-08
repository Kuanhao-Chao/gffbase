.. _security--security-policy:

Security Policy
===============

.. _security--supported-versions:

Supported versions
------------------

.. list-table::
   :header-rows: 1
   :widths: 50 50

   * - Version
     - Supported
   * - 0.2.0 candidate
     - ✅ Fixed on the release branch; not yet published
   * - 0.1.0
     - ⚠️ Only public release; affected by known issues below
   * - < 0.1
     - ❌

0.1.0 is currently the only tagged and published release (0.1.1 was prepared
but never published). It carries two SQL injection vulnerabilities fixed on
the 0.2.0 release branch and not backported. Until 0.2.0 is published, install
the reviewed candidate from its exact commit or use the mitigations in
``docs/security/2026-sql-injection.md``. Do not treat ``pip install gffbase`` as an
upgrade to 0.2.0 until PyPI lists that version.

.. _security--reporting-a-vulnerability:

Reporting a vulnerability
-------------------------

Please **do not** open a public issue for a security problem.

Report privately through GitHub's
`private vulnerability reporting <https://github.com/Kuanhao-Chao/gffbase/security/advisories/new>`__,
or by email to kuanhao.chao@gmail.com with ``[gffbase security]`` in the subject.

Include, as far as you can:

- the gffbase version, Python version, and operating system;
- whether the compiled ``gffbase._native`` extension was in use or the pure-Python
  fallback;

- a minimal input file or script that reproduces the problem;
- what you observed and what you expected.

You should get an acknowledgement within 3 working days and an assessment within
10. If a fix is warranted we will agree a disclosure date with you, credit you in
the advisory unless you prefer otherwise, and publish a patched release together
with a GitHub Security Advisory.

.. _security--scope:

Scope
-----

gffbase parses untrusted third-party annotation files and executes SQL against a
DuckDB database. The following are in scope:

- memory-safety faults in the Rust parser (crashes, out-of-bounds reads,
  unbounded allocation) reachable from a malformed GFF3/GTF input;

- SQL injection through any public API parameter;
- path traversal or arbitrary file write via a database path, export path, or
  ``GFFWriter`` target;

- code execution triggered by opening a database file or parsing an input file;
- denial of service through a small input causing unbounded memory or CPU use.

Out of scope:

- vulnerabilities in DuckDB, PyArrow, or other dependencies — report those
  upstream, though we will bump the pin once a fix ships;

- attacks that require the operator to already have write access to the
  database file or the Python process;

- resource use that is proportional to a genuinely large input.

.. _security--a-note-on-featuredbexecute:

A note on ``FeatureDB.execute()``
---------------------------------

``FeatureDB.execute()`` runs arbitrary SQL against the underlying DuckDB
connection by design. DuckDB can read and write the local filesystem
(``read_csv``, ``COPY … TO``). Passing untrusted strings to ``execute()`` is
equivalent to handing over the process, and is not treated as a vulnerability in
gffbase. Everything *else* that reaches SQL must be parameterized or
whitelisted, and a failure to do so is in scope.
