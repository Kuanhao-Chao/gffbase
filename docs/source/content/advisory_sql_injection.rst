.. _advisory_sql_injection--security-advisory-sql-injection-via-order_by-and-set_pragmas:

Security advisory — SQL injection via ``order_by`` and ``set_pragmas``
======================================================================

   **Published 2026-09-11** as `GHSA-5f5g-g3v5-prrg <https://github.com/Kuanhao-Chao/gffbase/security/advisories/GHSA-5f5g-g3v5-prrg>`__. Severity **High**, CVSS 3.1
   base score 8.1 (``CVSS:3.1/AV:N/AC:H/PR:N/UI:N/S:U/C:H/I:H/A:H``). A CVE has been requested. **Fixed in 0.2.0**;
   0.1.0 is affected.

.. _advisory_sql_injection--summary:

Summary
-------

Two ``FeatureDB`` parameters were interpolated directly into SQL:

1. ``order_by``, on ``all_features`` / ``features_of_type`` / ``children`` /
   ``parents``;

2. ``set_pragmas``, which interpolated both the pragma *name* and its
   *value*.

A caller who passes attacker-influenced text to either allows arbitrary SQL —
including DDL and DML — to run against their database. They are one defect
class, found in the same review of the query builders and fixed in the same
release, which is why they share an advisory.

.. _advisory_sql_injection--affected-versions:

Affected versions
-----------------

.. list-table::
   :header-rows: 1
   :widths: 50 50

   * - Version
     - Status
   * - 0.1.0
     - Affected — **the only published release** (PyPI, tag ``v0.1.0``)
   * - 0.2.0
     - Fixed

0.1.1 appears in neither row on purpose: it was prepared in tree but never
tagged and never published, and its changes ship inside 0.2.0. Nobody can be
running it unless they installed from a git checkout of the branch.

So the exposed population is exactly the people who installed 0.1.0 from
PyPI, plus anyone tracking the branch.

.. _advisory_sql_injection--severity:

Severity
--------

**High** — CVSS 3.1 8.1. Attack complexity is rated High because exploitation
requires the application to route untrusted input into these parameters; when
it does, confidentiality, integrity and availability are all fully exposed.
The factors behind that assessment:

- it is reachable from a **documented public parameter**, not an internal one;
- the payload runs with the full authority of the caller's DuckDB connection,
  which can also read and write the local filesystem (``read_csv``, ``COPY … TO``);

- it requires the application to pass untrusted input into ``order_by`` or
  ``set_pragmas``, which is plausible for a web service or notebook exposing a
  sort control or a tuning form, and not plausible for a batch analysis script
  with hard-coded values;

- ``set_pragmas`` additionally **swallowed every exception**, so an unsuccessful
  attempt left no trace in logs or return values.

``SECURITY.md`` names "SQL injection through any public API parameter" as in
scope, and closes with: *"Everything else that reaches SQL must be parameterized
or whitelisted, and a failure to do so is in scope."* This is that failure.

.. _advisory_sql_injection--affected-api:

Affected API
------------

Every method taking ``order_by``:

- ``FeatureDB.all_features()``
- ``FeatureDB.features_of_type()``
- ``FeatureDB.children()``
- ``FeatureDB.parents()``

The joined paths (``children``, ``parents``) carried their own copy of the
pass-through, so fixing only the unjoined ones would have left them exposed.

And:

- ``FeatureDB.set_pragmas()``

.. _advisory_sql_injection--details-1-order_by:

Details — 1. ``order_by``
-------------------------

``order_by`` was resolved against a small set of known column names, and anything
unrecognized was interpolated verbatim — an intentional escape hatch described
in the code as being "for power users":

.. docs-test: skip reason="illustrative: contains an elided fragment"

.. code-block:: python

   else:
       # Unknown / multi-field — accept as a literal for power users.
       col = order_by
   ...
   return f"{col} {direction}"

On SQLite this is inert, because ``sqlite3`` refuses to execute more than one
statement per call. **gffbase runs on DuckDB, which executes trailing
statements**, so the escape hatch became an injection when the storage engine
changed. gffutils contains the same interpolation and is not exploitable for
that reason alone.

.. _advisory_sql_injection--proof-of-concept:

Proof of concept
~~~~~~~~~~~~~~~~

.. docs-test: skip reason="illustrative: names a file the reader supplies"

.. code-block:: python

   import gffbase

   db = gffbase.create_db("annotations.gff3", ":memory:")

   payload = (
       'start ASC; DROP TABLE attributes; '
       'SELECT id, seqid, source, featuretype, start, "end", score, strand, '
       'frame, attributes_blob, extra_blob, file_order FROM features ORDER BY start'
   )

   list(db.all_features(order_by=payload))   # returns rows as normal

The ``attributes`` table is gone. The trailing ``SELECT`` re-supplies exactly the
projection the result generator expects, so the call **returns a normal-looking
feature list and raises nothing** — the damage is invisible from the call site.
Any statement DuckDB accepts can be substituted, including ``COPY … TO`` to write
files, or ``ATTACH`` to reach another database.

.. _advisory_sql_injection--details-2-set_pragmas:

Details — 2. ``set_pragmas``
----------------------------

``set_pragmas`` built its statement from a caller-supplied dict, interpolating
**both** the key and the value, and wrapped the whole loop body in a bare
handler:

.. docs-test: skip reason="excerpt of library internals, not caller code"

.. code-block:: python

   for k, v in pragmas.items():
       try:
           self.conn.execute(f"PRAGMA {k} = {v}")
       except duckdb.Error:
           continue

.. _proof-of-concept-1:

Proof of concept
~~~~~~~~~~~~~~~~

.. docs-test: skip reason="illustrative: names a file the reader supplies"

.. code-block:: python

   db = gffbase.create_db("annotations.gff3", ":memory:")
   db.set_pragmas({"threads": "1; DROP TABLE attributes"})
   # raises nothing
   db.conn.execute("SELECT count(*) FROM attributes")
   # CatalogException: Table with name attributes does not exist!

The key is exploitable the same way (``{"threads = 1; DROP TABLE attributes; SET threads": 1}``), so a fix that guarded only the value would have left it
open. Verified by mutation: reintroducing either half alone fails the suite.

The ``except duckdb.Error: continue`` existed for a real compatibility reason —
ported gffutils code passes ``constants.default_pragmas``
(``synchronous``, ``journal_mode``, ``main.page_size``, ``main.cache_size``), none of
which DuckDB has — but it could not distinguish "this is a SQLite pragma" from
"DuckDB rejected this", so it hid the failed attempts too.

.. _advisory_sql_injection--the-oracle-is-affected-here-unlike-order_by:

The oracle is affected here, unlike ``order_by``
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

gffutils' ``set_pragmas`` is:

.. docs-test: skip reason="excerpt of library internals, not caller code"

.. code-block:: python

   c.executescript(";\n".join(["PRAGMA %s=%s" % i for i in self.pragmas.items()]))

``executescript`` exists precisely to run several statements, so SQLite's
one-statement rule does not apply. **gffutils 0.14 is genuinely vulnerable
through its own** ``set_pragmas``, and has no fix available at the time of
writing; the mitigation below applies to it as well.

.. _advisory_sql_injection--impact:

Impact
------

Arbitrary SQL execution against the caller's database, and through DuckDB's
filesystem functions, arbitrary local file read and write as the running user.
Silent, because the query still succeeds.

.. _advisory_sql_injection--fix:

Fix
---

``order_by`` is now a whitelist of sort keys, and both clause builders share one
resolver so a future entry point cannot reacquire an escape hatch. Anything
outside the whitelist raises ``ValueError`` naming the accepted set.

The whitelist is not a restriction: it *restores* the contract gffutils
documents (``order_by`` items "must be in: 'seqid', 'source', 'featuretype',
'start', 'end', 'score', 'strand', 'frame', 'attributes', 'extra'"). Three of
those documented forms were in fact broken before the fix — ``attributes`` and
``extra`` raised a binder error, a tuple silently sorted by nothing, and a list
raised ``TypeError``.

Fix commit: ``89d9cc3`` — *"fix: make order_by a whitelist, closing a live SQL
injection"*.

``set_pragmas`` now matches each name against DuckDB's own settings catalog
(``SELECT name FROM duckdb_settings()``) and renders values as SQL literals, so
neither reaches the parser as syntax. Matching the live catalog rather than a
hardcoded list means the check cannot go stale against a newer DuckDB. The
compatibility behaviour is preserved and now deliberate: a name that is not a
DuckDB setting is skipped (and logged at debug), where previously it was
indistinguishable from a swallowed error.

Regression tests: ``tests/test_sql_injection_safety.py``, which runs the payloads
above against all five entry points and asserts the table survives.

.. _advisory_sql_injection--mitigation-without-upgrading:

Mitigation without upgrading
----------------------------

Validate before passing the value in:

.. code-block:: python

   ALLOWED = {
       "id", "seqid", "source", "featuretype", "start", "end",
       "score", "strand", "frame", "attributes", "extra", "file_order", "length",
   }

   def safe_order_by(value):
       if value is None:
           return None
       names = value.split(",") if isinstance(value, str) else list(value)
       names = [n.strip() for n in names]
       if not all(n in ALLOWED for n in names):
           raise ValueError(f"rejected order_by: {value!r}")
       return names

Note that on 0.1.x a tuple or list is not honoured (it sorts by nothing rather
than raising), so pass a single validated name.

For ``set_pragmas``, do not pass caller-supplied keys or values at all. If you
must, check the name against DuckDB's catalog and keep values non-string:

.. docs-test: skip reason="excerpt of library internals, not caller code"

.. code-block:: python

   KNOWN = {r[0] for r in db.conn.execute("SELECT name FROM duckdb_settings()").fetchall()}

   def safe_pragmas(d):
       out = {}
       for k, v in d.items():
           if k not in KNOWN:
               continue
           if not isinstance(v, (bool, int, float)):
               raise ValueError(f"rejected pragma value: {k}={v!r}")
           out[k] = v
       return out

The same mitigation applies to gffutils 0.14, which is vulnerable through
``set_pragmas`` and has no fix available.

.. _advisory_sql_injection--credit:

Credit
------

Found during the pre-0.2.0 security review of the query builders: ``order_by``
while whitelisting it for the release, and ``set_pragmas`` in the follow-up audit
of every remaining f-string SQL site. That audit found no third instance —
``migrate.py`` interpolates module constants, the ingest staging INSERTs derive
their column lists from the Arrow schema, the thread pragma coerces through
``int()``, and the region/relation CTEs interpolate only internally-derived
fragments with all caller values bound.

.. _advisory_sql_injection--disclosure:

Disclosure
----------

- Published as `GHSA-5f5g-g3v5-prrg <https://github.com/Kuanhao-Chao/gffbase/security/advisories/GHSA-5f5g-g3v5-prrg>`__ on 2026-09-11, together with the
  0.2.0 release that fixes it.
- Severity High, CVSS 3.1 8.1; CVE requested through GitHub.
- 0.1.0 stays on PyPI rather than being yanked: 0.2.0 carries breaking
  changes, so users who cannot migrate at once keep a working install and can
  apply the mitigation above. There is no 0.1.x backport.
