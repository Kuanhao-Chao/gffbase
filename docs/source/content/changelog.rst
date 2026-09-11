.. _changelog--changelog:

Changelog
=========

All notable changes to GFFBase are documented here.

The format follows `Keep a Changelog <https://keepachangelog.com/en/1.1.0/>`__, and
this project adheres to `Semantic Versioning <https://semver.org/spec/v2.0.0.html>`__.

.. _changelog--020rc1-unreleased:

`0.2.0rc1 <https://github.com/Kuanhao-Chao/gffbase/compare/v0.1.0...v0.2.0rc1>`__ — Unreleased
----------------------------------------------------------------------------------------------

Genuine ``gffutils`` 0.14 API and CLI parity, first-class support for
discontinuous (multipart) GFF3 features, a ``compat``/``strict`` mode axis,
transactional storage, and a release pipeline gated on validation.

**0.1.1 is folded into this release.** It was prepared but never tagged and
never published, so its changes ship here; a changelog section for a version
nobody can install would only mislead. Everything below is the delta from
0.1.0, which remains the only prior release.

.. _changelog--security:

Security
~~~~~~~~

- **Invalid UTF-8 silently destroyed data, three different ways.** Found by
  running 18 hostile inputs through both parser engines and diffing the
  outcomes. Nothing crashed; everything corrupted quietly, which is worse:

  - The **attribute column** was decoded with ``unwrap_or("")``, so one bad byte
    replaced the whole column with an empty string. A gene whose ``Name``
    carried a stray Latin-1 byte was stored with **no attributes at all** --
    ID included, making the feature unreachable -- while the ingest reported
    success.
  - ``seqid`` **and** ``featuretype`` went through ``from_utf8_lossy``, silently
    yielding a U+FFFD chromosome name that matches nothing in any later query.
  - **Directives** kept whatever bytes they had.

  UTF-8 is now validated once for the whole line, before any field is read, and
  a failure is a ``GFFFormatError`` naming the line. Costs 2.9% of parser
  throughput, measured on MANE. Valid multi-byte UTF-8 (``café``, ``Ωmega``) is
  unaffected -- it was always legal and still parses.

- **A NUL byte in the database path truncated it, and wrote the file anyway.**
  DuckDB is C++ and takes the path as a C string, so it stops at the first
  NUL: ``FeatureDB("a\0b.duckdb")`` created a file called ``a`` -- a different
  path than the caller named. The stray-file cleanup then called ``os.unlink``
  with the original path, which raises ``ValueError`` rather than ``OSError``, so
  it escaped the ``except``, left the truncated file on disk, and replaced the
  real diagnosis with a confusing one. Anything deriving a database path from
  untrusted input could write to a location it never named. Both ``FeatureDB``
  and ``create_db`` now reject an embedded NUL before touching the filesystem.

- ``helpers.make_query``\ **'s raw-SQL slots are now documented as such.**
  ``featuretype``, ``limit`` and ``strand`` are bound parameters and ``order_by`` is
  whitelisted, but ``other`` and ``extra`` are interpolated verbatim -- they exist
  to carry the caller's own SQL, which is how upstream builds its relation
  joins. No gffbase code path routes caller data into either. The asymmetry is
  written down because the validation surrounding them makes it easy to assume
  otherwise. Audited alongside: ``execute()``, path handling (traversal, null
  bytes, absolute paths) and every parameterised query surface -- no injection
  found through any of them.

- **SQL injection through** ``order_by`` **(affects 0.1.0, the only published release).** The parameter
  was interpolated into the query, with anything outside a small set of known
  column names passed through verbatim as a deliberate escape hatch "for power
  users". DuckDB executes trailing statements, so

  ::

     db.all_features(order_by='start ASC; DROP TABLE attributes; SELECT …')

  dropped the table **and still returned rows** — the trailing ``SELECT``
  re-supplies the projection the result generator expects, so the call raises
  nothing and the damage is invisible from the call site. Any statement DuckDB
  accepts could be substituted, including ``COPY … TO`` to write local files.
  All four entry points were affected (``all_features``, ``features_of_type``,
  ``children``, ``parents``); the joined paths had their own copy of the
  pass-through.

  gffutils contains the same interpolation and is **not** exploitable, because
  SQLite refuses to execute more than one statement per call. gffbase inherited
  the API shape and lost that accidental protection when it changed storage
  engine.

  ``order_by`` is now a whitelist, shared by both clause builders so no future
  entry point can reacquire an escape hatch.

- **SQL injection through** ``set_pragmas`` **(affects 0.1.0, the only published release).** The same
  defect one method away, and quieter. ``FeatureDB.set_pragmas()`` built
  ``PRAGMA {name} = {value}`` by interpolating **both** halves of a
  caller-supplied dict, with the whole loop body inside
  ``except duckdb.Error: continue`` — so

  ::

     db.set_pragmas({"threads": "1; DROP TABLE attributes"})

  dropped the table, and a payload that *failed* was swallowed too, leaving no
  trace anywhere. The swallow existed for a real reason — ported gffutils code
  passes ``constants.default_pragmas`` (``synchronous``, ``journal_mode``,
  ``main.page_size``, ``main.cache_size``), none of which DuckDB has — but it could
  not tell "this is a SQLite pragma" from "DuckDB rejected this".

  Names are now matched against DuckDB's own settings catalog and values
  rendered as SQL literals, so neither reaches the parser as syntax. Matching
  the live catalog rather than a hardcoded list means the check cannot go stale
  against a newer DuckDB, and the compatibility behaviour is unchanged but now
  deliberate: an unrecognized name is skipped and logged, not guessed at.

  Unlike ``order_by``, **gffutils is vulnerable here too** — its version calls
  ``cursor.executescript()``, which exists precisely to run several statements.
  Verified against 0.14. Not reported upstream; that is a maintainer decision.

  An audit of every remaining f-string SQL site found no third instance.

  See the :doc:`SQL injection advisory <advisory_sql_injection>` for both write-ups and mitigations
  for anyone who cannot upgrade.

- The thread-count environment variable is now ``GFFBASE_THREADS``.
  ``GFFUTILS2_THREADS`` predates the rename to gffbase and was the last
  ``GFFUTILS2_*`` name left; it still works, and the new name wins where both
  are set. Silently ignoring an existing job script's thread limit on a shared
  machine is worse than an untidy variable name.

- ``GFFWriter.close()`` **closed a stream it did not open.** ``GFFWriter`` accepts
  either a path or an open file object, and closed both — so
  ``GFFWriter(sys.stdout).close()`` shut stdout down for the whole process and
  anything written afterwards raised ``ValueError: I/O operation on closed file``. A writer owns only the handles it opened; a caller's stream is
  flushed and left alone. gffutils has the same defect.

- **The B-tree CI job was red.** ``test_every_invariant_actually_ran`` required
  INV-8 to have run and ``report.skipped`` to be empty, but INV-8 compares ``bbox``
  against the coordinates it was built from and therefore only exists when an
  R-tree does. Under ``GFFBASE_TEST_DISABLE_RTREE=1`` the validator correctly
  records it as skipped, which the test read as a failure. The skip is the
  designed behaviour, so it is now what the test asserts.

.. _changelog--testing-and-ci:

Testing and CI
~~~~~~~~~~~~~~

- **The parity gate could not pass on any machine but the one that generated
  it.** Three independent causes, each making the manifest a description of
  the environment rather than of gffutils' API:

  - ``__firstlineno__`` and ``__static_attributes__`` are injected into every
    class body by CPython 3.13, so a manifest generated there could not
    validate on 3.11 or 3.12.
  - Members inherited from builtin bases (``Exception.add_note``, ``dict.keys``)
    were recorded, and their introspectability changes between releases.
  - ``gffutils.contrib.plotting`` does ``from pybedtools.contrib.plotting import Track``, and **pybedtools sets** ``Track = None`` **when matplotlib is absent** --
    so the manifest recorded the presence of a name bound to ``None``, and the
    inventory tracked a third-party optional dependency.

  All three are excluded now. ``--check`` also refuses to compare a
  checkout-generated manifest against a pip-installed oracle (a checkout ships
  ``contrib/`` and ``scripts/gffutils-cli``, which pip does not package), and it
  prints **what** differs rather than only that something does -- "out of
  date" alone sends the reader to regenerate a file that may be correct.

- ``test_api_parity.py`` **failed at import on Python 3.10**, the declared
  floor, because it uses ``tomllib`` (3.11+). Windows is tested at the floor, so
  this took out the parity module and both Windows cells. ``tomli`` is now a
  declared test dependency under an environment marker.

- ``pybedtools_integration`` **was measured at 9.8% coverage** because its tests
  skip without the ``bedtools`` binary and no runner had one. CI installs it, so
  the module is exercised rather than counted as untested. The coverage gate
  is now per-platform, because what is *reachable* is per-platform: Windows
  cannot install pybedtools at all.

- ``python -m gffbase`` is tested rather than assumed. It runs in a subprocess
  the tracer cannot follow, so it is excluded from measurement with the test
  that covers it named -- rather than left reading 0%.

.. _changelog--documentation:

Documentation
~~~~~~~~~~~~~

- **The published site rendered literal backticks in 92 places.** Markdown
  nests inline markup and reStructuredText does not, so the MkDocs conversion
  carried a code span inside a bold span across verbatim -- which RST renders
  with the backticks visible, because nothing nests inside a strong span.
  ``sphinx-build -W`` reports nothing: it is valid RST that means something
  else. Every span was split so both formattings survive
  (``order_by`` **is validated**), and the site is now checked by scanning the
  *rendered* HTML, which is the only thing that can see this class at all.

  Related: autodoc publishes docstrings verbatim, and the docstrings use a
  single backtick for code in the project's house style. RST's default role
  for a single backtick is ``title-reference``, so every ``FeatureDB`` and
  ``order_by`` on the API pages was rendering as italic prose. ``default_role``
  is now ``code``, which makes several hundred spans across 38 modules mean
  what they say without rewriting any of them.

- ``tools/md2rst.py`` **is gone.** It was the one-shot MkDocs to RST converter.
  The RST is now canonical and has been hand-edited since -- including the
  repairs above -- so re-running it would silently clobber the corrected
  sources with a fresh conversion of documents that no longer exist. No test
  and no workflow invoked it.

- **Documentation code is now executed by the test suite.**
  ``tests/test_docs_snippets.py`` extracts every fenced ``python`` block from
  ``docs/``, ``README.md`` and ``MIGRATION.md`` and runs it against the vendored
  fixtures, with each page treated as a notebook so its examples build on each
  other the way a reader experiences them. Skipping is opt-out and must state
  a reason, and the exemption count is a ratchet that may fall but never rise.

  ``docs/cookbooks/index.md`` claimed every snippet had been validated while its
  own opening example opened two ``FeatureDB`` handles and closed neither --
  teaching a lock leak. Nothing caught it because nothing ran it. Running them
  immediately found four more defects, listed below.

- ``create_db()`` **did not accept** ``validation=`` **or** ``on_error=``. Both axes are
  documented, both are supported by ``resolve_mode()`` and carried by
  ``IngestOptions``, and the public entry point simply never forwarded them --
  so the documented ``validation="ncbi", on_error="warn"`` audit combination
  raised ``TypeError: unhandled kwarg``. They are now parameters, and documented.

- ``docs/usage_gallery.md`` **documented** ``Feature.attributes_dict()``, which
  does not exist on ``Feature`` (only on ``ParsedFeature``, and not in gffutils at
  all). Replaced with ``dict(feature.attributes)``.

- **A troubleshooting snippet was not valid Python** -- an ``except`` clause with
  no ``try``. It is now a complete, executed example.

- **Four pages stated the wrong number of validator invariants** (15; there
  are 14). ``tests/test_release_hygiene.py`` now derives the number from the
  registry, so prose cannot drift from it again.

- ``MIGRATION.md`` **linked** ``docs/cli.md``, which 404s on the rendered site,
  and quoted an unsourced "5-550x query speedups". It also never mentioned
  connection lifecycle, despite being the first page a porting user reads and
  despite DuckDB's exclusive lock being the one operational difference from
  SQLite that will surprise them. It now opens with it.

- **Public API docstrings.** mkdocstrings publishes docstrings verbatim, so
  they *are* the API reference. ``count_features_of_type``, ``featuretypes``,
  ``seqids``, ``all_features``, ``features_of_type``, ``delete``, ``update``,
  ``children_bp``, ``bed12``, ``iter_by_parent_childs``, ``analyze``, the ``Feature``
  aliases and every ``GFFWriter`` method rendered as a bare signature with no
  description at all. All now document what they do, their parameters, what
  they return and what they raise, with examples on the high-traffic ones --
  and carry the type annotations that ``Typing :: Typed`` promises.

- **The site moved to** https://khchao.com/gffbase/\ **.** ``docs/CNAME`` is deleted,
  which is what was making GitHub Pages redirect the canonical path back to the
  retired ``gffbase.khchao.com`` subdomain. A release-hygiene test fails if the
  old host reappears anywhere.

- **New pages for everything a first-time reader needed and could not find:**
  `Installation <https://khchao.com/gffbase/content/installation.html>`__,
  a linear `Quickstart <https://khchao.com/gffbase/content/quickstart.html>`__
  (every snippet executed before publishing),
  `Compatibility & strict modes <https://khchao.com/gffbase/content/modes.html>`__ —
  a core concept previously explained only in passing —
  `Connections & concurrency <https://khchao.com/gffbase/content/connections.html>`__,
  `Troubleshooting & FAQ <https://khchao.com/gffbase/content/troubleshooting.html>`__,
  and `Benchmark methodology <https://khchao.com/gffbase/content/methodology.html>`__.
  The changelog, contributing guide and security policy are now on the site
  rather than GitHub-only.

- **The performance page no longer contradicts itself.** It opened with a
  preamble telling the reader that the numbers below it were stale, and then
  printed them. It is rewritten around generated tables, and the 35 KB
  ``PERFORMANCE_COMPARISON.md`` — whose §7 still asserted that legacy won on GFF3
  ingest, which the same document's headline table denied — is retired.

- **The landing page stopped being a copy of the README.** It duplicated it
  nearly verbatim, including the benchmark table, so the two drifted
  independently.

- ``mkdocs build --strict`` **runs in CI.** ``CONTRIBUTING.md`` and the PR template
  both claimed it did. It did not: the only mkdocs invocation was ``gh-deploy``,
  on ``main``, after merge — so a broken link was found by the deploy rather than
  by the PR that introduced it.

- Corrected counts that had gone stale: the test total, the coverage gate
  (96 % / 95 %, not 99 %), the schema table count in ``CONTRIBUTING.md``, the
  corpus download size, and a test-tool list naming a dependency the project
  does not use. ``docs/usage_gallery.md`` taught the deprecated
  ``GFFUTILS2_THREADS`` environment variable as the primary name.

- ``mkdocs`` is capped below 2.0. It removes the plugin system with no migration
  path, so an unpinned floor would let a routine ``pip install -e .[docs]`` break
  the documentation build with no change on our side.

.. _changelog--benchmarks-and-published-numbers:

Benchmarks and published numbers
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

The performance claims could not be reproduced from anything in the repository.
This release rebuilds the harness so that they can be, and re-measures
everything from scratch.

- **The published numbers were a macOS run of a version that was never built.**
  The tables came from ``benchmarks/results/06_mega.json``: four of five corpora,
  Apple Silicon, provenance naming a ``gffbase 0.2.0`` that no build ever
  produced, and GENCODE's comparator censored under a *different* GTF arm than
  the one the headline names. They claimed 1.20x-1.43x. A Linux run of all five
  corpora, under the schema-v3 evidence contract with every correctness signature
  matching, measures **1.14x, 1.21x, 0.93x, 0.90x and 0.69x** -- gffbase ahead
  where per-feature overhead dominates, behind on attribute-dense whole-genome
  annotations. Those are the numbers now published; the macOS bytes are retained
  untouched as the historical platform entry.

- **Five pages told the reader the table above them was something else.** The
  generated blocks moved to the Linux artifact; the hand-written prose wrapped
  around them did not. ``README.md`` and ``performance.rst`` introduced a
  five-corpus Linux table as "the retained historical Mac run", under a
  *Historical Mac sweep* heading, sourced to "the committed historical Mac
  file" -- while the provenance block three lines below named
  ``06_mega.linux-x86_64.json``. ``performance.rst`` also carried a note
  explaining that GENCODE GFF3 was *missing from this run*, directly above the
  row measuring it at 11 min 8 s, and a trade-off ledger still reading "faster
  in each completed, comparable corpus" over a table with three rows below
  1.0x. ``datasets.rst`` sent readers to the vanished note. None of it was
  caught, because the release guards check the generated blocks and three
  specific strings, and every one of these lived in the prose between them. The
  pages now say what was measured: the ingest column is a draw, ahead where
  per-feature overhead dominates and behind on attribute-dense whole-genome
  files; the GTF row is the inference-disabled arm, which is the arm least
  favourable to gffbase; ``peak RSS`` is ingest plus exhaustive validation and
  not what the default path costs; and ingest wall is measured once per corpus,
  with the measured spread stated so a single figure is worth what it is worth.

- **Ingest is attribute-bound and essentially serial, and the docs now say so.**
  Cost tracks attributes rather than features: across the three
  attribute-dense corpora, at 12.8 to 17.9 attributes per feature, gffbase holds
  157,000 to 168,000 attributes per second over a sixteen-fold range of corpus
  size. The rate then falls with the attribute density -- 130,600 on RefSeq at
  11.2, 62,400 on CHESS at 2.6 -- which is an attribute-bound cost with a fixed
  per-feature floor showing through, and is why gffbase wins on CHESS and not on
  GENCODE. A 25-job sweep over five
  corpora at 1, 2, 4, 8 and 10 threads shows raising DuckDB threads buys between
  1.05x and 1.25x: ten times the cores, at most a quarter more throughput. The
  GENCODE GTF result is not a GTF defect -- gffbase moves 157k attributes/s
  there, its own third-fastest of the five and within 7% of its best, just under
  GENCODE GFF3 at 162k and MANE at 168k -- it is that the
  comparator's ``no-infer`` GTF path is a plain bulk insert, its fastest case
  anywhere at 229k/s. Recorded in the methodology, with parallel ingest on the
  roadmap.

- **A published "peak RSS" that was six times the cost of ingesting.**
  ``peak_rss_bytes`` is the peak of the ingest *subprocess*, and that subprocess
  also runs ``validate(level="full", sample=None)``. Under the canonical
  exhaustive validation the validation dominates: 62.0 GB where the same corpus
  validated at ``sample=10000`` peaks at 10.0 GB. Sitting unlabelled beside an
  ingest time, it read as the memory needed to ingest. The column now names the
  work it measured, and the memory *ratio* is gone entirely -- gffbase's figure
  came from a process that validated exhaustively and the comparator's from one
  that did not, so their quotient (published as "496x") described neither
  engine. "Equal work, or no ratio" is the rule the rest of the harness follows.

- **A generated caption was silently dropped from every RST page.**
  ``markdown_to_rst`` routed any block containing a pipe to the table converter,
  which emitted the list-table and discarded everything after it -- including the
  caption naming how many corpora the ratios cover. A hand-written copy left
  outside the markers went on claiming three corpora after the run had grown to
  five: a stale number directly beneath a freshly generated table, which is the
  exact failure generated blocks exist to prevent.

- ``status`` **aborted healthy campaigns.** It validates an attempt directory
  with ``exact_directory_scan``, which stats every entry, rescans, and requires
  the two passes to agree byte for byte -- the right contract for settled
  evidence and an impossible one for a directory whose worker is still in it. A
  DuckDB ``.wal`` grows continuously, so a 36-job run died at job 14 on
  ``gencode-gff3.duckdb.gffbase-building.<pid>.wal``, with nothing actually
  wrong. An earlier fix had allowed those *names*; the exactness requirement
  remained. ``result.json`` is written once and last, so its absence is what
  "still running" looks like from outside: an attempt without it is now scanned
  without the second pass, keeping the name set, symlink rejection, entry kinds,
  modes and device/inode identity, and giving up only the claim that the bytes
  held still.

- **The published tables could not render a Linux campaign at all.** The
  generator read one hardcoded path, ``benchmarks/results/06_mega.json``, which
  holds the historical macOS run — four corpora, schema v2, provenance naming a
  ``gffbase 0.2.0`` that was never built. ``merge --publish`` writes neither
  that file nor anything the generator can use: its portable projection drops
  ``input`` and ``db_paths``, two of the fourteen keys a schema-v3 row must
  carry. So a finished campaign left the site rendering the artifact it
  superseded. The generator now prefers a per-platform
  ``06_mega.<platform>.json``, falling back to the macOS artifact;
  ``tools/gen_published_measurements.py`` derives that file from a campaign's
  run-local record, where each primary job's payload already *is* a schema-v3
  row; and ``06_mega.py --publish`` writes under a platform key too, so a local
  sweep can no longer overwrite the pinned artifact. Selection is on
  ``primary_eligible``, never the corpus key — a scaling job carries the same
  ``payload["key"]`` as the canonical row for that corpus, so keying on it would
  publish a thread-sweep as the headline number. The three prose guards resolve
  the file through the generator rather than naming it, so they cannot check
  yesterday's numbers against today's tables.

- **Three of five corpora reported a content divergence that was not one.** The
  signature that decides whether a speedup may be published compared gffbase's
  transitive closure against gffutils' ``relations`` table, but what gffutils
  stores at ``level = 2`` is not a closure: ``_update_relations`` inserts, for
  each feature, the children of its children — one hop, no iteration to a fixed
  point. On a four-deep chain it records five ancestor/descendant pairs and
  omits the sixth. RefSeq differed by 3,218 pairs and GENCODE GFF3 by 108, on
  hierarchies whose **direct edges agreed exactly** — and the closure is a
  function of those edges. The comparator's closure is now derived from them
  rather than read from the cache.

- **A comma followed by a space cost CHESS its speedup.** GFF3 says an
  unescaped comma separates values and a literal comma must be percent-encoded;
  gffbase follows that, and gffutils deliberately does not, keeping ``, ``
  inside the value so an unescaped ``description=kinase, subunit 1`` survives.
  Ten CHESS genes record two names as ``gene_name=ADAM6, RPS8P1``, which showed
  up as a 20-row divergence. The signature now applies one rule to both engines
  — the comparator's coarser one, because re-splitting on every comma would
  shatter 2,294 correctly escaped CHESS descriptions to line up twenty gene
  names. The parse-policy difference itself is now a **declared API deviation**
  in ``tests/parity/deviations.toml``, pinned by a differential test; no fixture
  in the shared corpus contained a comma-space attribute, which is why nothing
  caught it.

- **The 0.1.0 arm of the version bridge could never have run.** Two independent
  defects, one hiding the other. ``bridge.py`` released its handle behind
  ``if hasattr(db, "close")`` — False for gffbase 0.1.0, whose missing
  ``close()`` is one of the defects this release fixes — so DuckDB kept its
  exclusive lock and signing the database raised. Behind that,
  ``database_signature`` assumed the current schema unconditionally and could
  not read schema v1 at all, which has no ``segments_all``, one row per feature,
  and no ``seg_idx`` on ``attributes``. Every bridge job must carry a valid
  signature, so those jobs were unsatisfiable by construction. Both are fixed
  and the arm now completes in 34 s on MANE.

- **A signature that never finished.** Normalizing gffutils for comparison
  built its temporary tables without the indexes the following joins need, and
  let SQLite spill its sorts to ``/var/tmp`` — a 32 GB root volume, not the
  scratch the databases sit on. On GENCODE GTF (6.07M features, 12.34M
  relations) the correlated ``NOT EXISTS`` over an unindexed table ran for
  2 h 39 m without finishing, and took the whole job down with the controller's
  timeout. Indexed, and spilling beside the work, it returns byte-identical
  components in under 16 minutes.

- **The results file no longer destroys itself.** ``06_mega.py`` built a payload
  containing only the corpora named by ``--only`` and wrote the whole file, so
  each targeted re-run silently deleted the others. ``benchmarks/out/06_mega.json``
  ended up holding **one of five corpora** while the performance document named
  it as the provenance for all five, and four published rows had no surviving
  measurement. Results now merge by corpus key, and are written after every
  corpus rather than once at the end.

- **No number is extrapolated any more.** A legacy run that exceeded the safety
  valve had ``wall_seconds = timeout × 2.0`` written into it — a factor with no
  measurement behind it, whose own comment conceded there was no way to observe
  ``gffutils``' progress. It was the sole source of the published "≥ 2 hr 30 min"
  legacy wall and the "≥ 32×" headline. A capped run now reports
  ``state: timed_out``, ``wall_seconds: null``, and the observed ``cap_seconds``.
  Timed-out comparators produce neither a ratio nor a ratio floor; tables label
  them as censored. The preserved schema-v2 Mac artifact retains its original
  lower-bound field names, but the renderer never presents them as a current
  performance claim.

- **Every result carries its provenance.** CPU model, physical and logical core
  count, RAM, OS, Python, DuckDB, PyArrow, ``gffutils``, ``gffbase`` and ``rustc``
  versions, the git commit and whether the tree was dirty, the region-sampling
  seed, and every run parameter. None of this was recorded before: the numbers
  carried their environment only as hand-typed prose that said "gffbase 0.1.0"
  throughout the 0.2.0 cycle, so nothing in the repository could have detected
  a regression.

- **Published tables are generated, not transcribed.** The corpus table lived
  hand-copied in five files. ``tools/gen_benchmark_tables.py`` now renders it
  from the committed measurements into marked blocks, and
  ``tests/test_release_hygiene.py`` fails both when a table drifts from its data
  and when a benchmark row is written outside a generated block.

- **Results are committed.** ``benchmarks/results/`` is tracked, so a published
  number has checked-in evidence. The 0.1.0-era artifacts are preserved under
  ``benchmarks/results/archive/0.1.0/`` with a README recording exactly which of
  them are unreliable and why.

- **The harness fits on a real disk.** Five corpus pairs total ~38 GiB. Each is
  purged (including ``.wal`` / ``-journal`` sidecars) as soon as its numbers are
  recorded, holding the peak near 16 GiB; ``--keep-db`` retains one for the later
  stages, ``--no-purge`` keeps everything, and ``GFFBASE_BENCH_OUT`` redirects to
  another volume. Free space is checked before each corpus so a five-hour sweep
  fails in seconds rather than at hour four.

- **Stages 01–05 can run from a clean checkout.** ``common.py`` pointed at a
  GENCODE **v45** file that ``download_corpora.py`` had stopped fetching, present
  locally only as a symlink into the retired ``bench/`` tree — so those stages
  worked on the maintainer's machine and raised ``FileNotFoundError`` everywhere
  else. They now use the v49 GFF3 corpus that the mega benchmark also uses, so
  the two harnesses can no longer report on different releases. The
  ``--reuse-cached`` flag and its hardcoded literals are gone, as is the
  cross-directory cache loader that presented old measurements as new ones.

.. _changelog--changed-breaking:

Changed (breaking)
~~~~~~~~~~~~~~~~~~

- ``order_by="score"`` **sorts numerically instead of lexicographically.** GFF
  column 6 is stored as text -- the spec allows ``.`` there, and the oracle
  stores it as text too -- so an ORDER BY on it ranked ``10 < 100 < 1e3 < 2.5 <
  9``. "The highest-scoring features" came back wrong, and nothing raised. The
  whitelist entry is now ``TRY_CAST(score AS DOUBLE)``, which yields NULL for
  ``.`` and for any non-numeric value; DuckDB's NULLS LAST default then puts
  unscored features at the end, which is what a caller asking to sort by score
  means.

  **gffutils has the same defect**, so this is a deliberate divergence rather
  than a parity fix, and it is recorded as one in
  ``tests/parity/deviations.toml``.

- **Ordered queries now have a total order.** No sort column is unique --
  features share a start, a featuretype, a score, and even ``file_order``
  repeats, because GTF synthesis stamps a synthesized parent with the
  ``MIN(file_order)`` of its children. DuckDB sorts in parallel and does not
  preserve ties, so the same query over the same database could return tied
  rows in a different order on consecutive runs. Every ordered query now
  appends ``file_order ASC, id ASC`` as a final tiebreak.

  ``file_order`` comes first so that tied rows come back in the order they
  appeared in the file, which is what the oracle does -- breaking ties by
  ``id`` alone put ``FBgn0031208:3`` ahead of ``exon_2`` at the same start,
  purely because ``F`` sorts before ``e``. Same features, same coordinates,
  different sequence: the kind of difference that surfaces only once someone's
  output does, and the parity suite caught it. ``id`` still follows, because
  ``file_order`` is not unique either.

  The tiebreak is ascending regardless of ``reverse``: it exists to be stable,
  not meaningful, and flipping it alongside the caller's key would make
  ``reverse=True`` something other than the exact reverse of the forward order
  for tied rows. Results are now reproducible run to run; they are not
  guaranteed to match the order a pre-0.2.0 run produced.

- ``order_by`` **accepts multiple keys, and** ``reverse`` **applies to every
  one.** A tuple, a list, or a comma-separated string all work. Only a single
  name worked before: a tuple was interpolated as a Python repr, which DuckDB
  parses as a constant struct, so the query silently sorted by nothing; a list
  raised ``TypeError: unhashable type: 'list'`` from a set membership test. The
  whitelist also gained ``id``, ``file_order`` and ``length``, none of which is
  in the oracle's documented list but all of which are real columns callers
  sort by.

  gffutils appends the direction once, which in SQL reverses only the *last*
  key. gffbase applies it to each key. Since multi-key sorting did not function
  here at all, no working gffbase behaviour changes, and reproducing the
  upstream shape in new code would be copying a defect.

- ``validation="ncbi"`` **accepts an unquoted GTF attribute value.** The GTF
  specification quotes values, but unquoted bare tokens are common in real
  annotation releases, and rejecting them meant strict mode could not read
  files every other tool accepts. A value now validates if it is properly
  double-quoted *or* is a bare token containing no whitespace, ``"`` or ``;``.
  Still rejected: an unbalanced quote, an unescaped quote inside a quoted
  value, and an unquoted value containing whitespace. Callers who used
  ``validation="ncbi"`` specifically to reject unquoted GTF no longer get that.

- ``FeatureDB.bed12()`` **now refuses a feature its blocks do not span**, with
  gffutils' exact message (``"End of last exon (600) does not match end of feature (1000)"``). BED12's blockStarts are offsets from chromStart and the
  last block has to reach chromEnd, so emitting a line for a transcript whose
  exons stop short produces a record naming a range it does not cover -- and
  sends it into a genome browser. gffutils raises here; gffbase emitted the
  line.

  Verified against real data before changing it: 5,000 of 5,000 MANE
  transcripts span their exons exactly, so no real annotation triggers this.
  The inputs that did were synthetic test fixtures declaring transcripts wider
  than their children, which have been corrected.

- ``FeatureDB.region_batched()`` **now raises on a region it cannot parse,
  instead of silently dropping it.** The ``query_idx`` column is documented as
  the way to map results back to the input, and it was assigned over the rows
  that *survived* normalization — so a single unusable region renumbered every
  later query and the caller attributed whole result groups to the wrong
  input, with nothing raised on either side.

  ``query_idx`` is now the item's index in ``regions`` as passed. The new
  ``on_invalid=`` argument selects the policy: ``"raise"`` (default) reports the
  offending position and value; ``"skip"`` restores the old dropping behaviour
  but leaves the surviving indices anchored to the input, so a gap is visible
  rather than closed up.

- **Minimum Python is now 3.10** (was 3.9), and 3.14 is supported. The wheel
  tag moves from ``abi3-py39`` to ``abi3-py310``. This removes the split
  dependency story: current DuckDB and PyArrow both require 3.10+, so one
  dependency set now covers the whole supported range. Dependency floors were
  raised to the versions actually tested (``duckdb>=1.4.1``, ``pyarrow>=18.1``)
  from the previously untested ``duckdb>=1.0``, ``pyarrow>=14``.

- **PyO3 0.22 → 0.29.** Migrated off the removed ``*_bound`` constructors,
  ``into_py``, ``value_bound``, and ``get_type_bound``.

- ``FeatureDB.schema`` **is a method again**, not a property. gffutils documents
  ``db.schema()`` and callers write it that way; as a property the documented
  call raised ``TypeError: 'str' object is not callable``.

- ``merge_strategy="error"`` **is now the real default**, so a file with
  duplicate IDs raises instead of loading with silently renamed rows.
  Ingestion previously renamed every duplicate to ``<id>__2`` unconditionally,
  which made the documented default unreachable. ``create_unique`` now produces
  the oracle's ``<id>_1``, ``<id>_2`` rather than ``<id>__2``, ``<id>__3``, and no
  longer writes ``duplicates`` rows -- the oracle records a rename only when
  ``merge`` falls back to ``create_unique``, because that table exists so a later
  merge can find the sibling rows.

- ``DuplicateIDError``, ``AttributeStringError`` **and** ``EmptyInputError``
  **now subclass** ``ValueError``\ **.** gffutils exports ``DuplicateIDError`` but raises a
  bare ``ValueError("Duplicate ID ...")``, so real callers write
  ``except ValueError``. Subclassing satisfies both the documented type and
  those callers instead of forcing a choice. ``FeatureNotFoundError`` is
  deliberately left on ``Exception``.

.. _changelog--changed:

Changed
~~~~~~~

- ``rust/Cargo.lock`` **is now committed.** ``rust/Cargo.toml`` and ``MANIFEST.in``
  both already claimed it was shipped; ``.gitignore`` excluded it. Dependency
  resolution for the published wheel therefore varied with build time and
  platform, contradicting the declared MSRV.

- **Coverage flags moved out of the default** ``pytest`` **invocation.** ``addopts``
  hard-required ``pytest-cov`` (absent from the ``dev`` extra) and made a bare
  ``pytest`` fail on coverage rather than on tests. Coverage is now applied
  explicitly in CI. ``pytest-cov`` was added to the ``dev`` extra.

- **Declared Rust MSRV raised from 1.69 to 1.83.** The 1.69 claim was justified
  by a ``Cargo.lock`` that was gitignored and absent, so it had never been
  verified; with the lock now committed, the resolved graph includes
  ``flate2 1.1.x``, which does not build on 1.69. 1.83 is the toolchain the
  ``lint`` CI job now compiles the whole crate with, so the floor is enforced
  rather than asserted. This affects source builds only — the published wheels
  are ``abi3`` and need no Rust toolchain.

- **CI now enforces what it claimed to.** The lint job runs
  ``ruff format --check`` (never run before, 42 files were drifting), includes
  ``benchmarks/`` in its scope (70 errors were invisible), and finally invokes
  the clippy that was being installed and discarded. The test job runs the full
  ``cargo test`` rather than ``--lib``, asserts ``native_available()`` instead of
  letting a broken extension build silently skip every Rust cell, and installs
  the ``all`` extra so the ``format="df"`` / ``format="polars"`` paths are exercised
  instead of skipped.

- Ruff configuration gained a ``[tool.ruff.format]`` section and per-file ``E402``
  ignores for the ``bench/`` and ``benchmarks/`` entry-point scripts, which must
  bootstrap ``sys.path`` before importing. The whole tree is now
  ``ruff format`` clean.

- ``UP006``/``UP007``/``UP035``/``UP045`` are ignored for this release. Every module
  carries ``from __future__ import annotations``, so ruff proposes PEP 585/604
  rewrites regardless of ``target-version``; applying them wholesale is not safe
  while 3.9 is supported. They are re-enabled in 0.2.0 when the floor moves
  to 3.10.

.. _changelog--added:

Added
~~~~~

- ``FeatureDB.to_table()`` — the whole database, or a filtered slice of it,
  as one ``pyarrow.Table`` / ``pandas.DataFrame`` / ``polars.DataFrame``. The
  columnar counterpart to ``all_features()``: same filters (``featuretype``,
  ``limit``, ``strand``, ``order_by``, ``completely_within``), but no ``Feature`` object
  is constructed at any layer.

  .. code-block:: python

     exons = db.to_table("exon", format="arrow")
     df = db.to_table(["exon", "CDS"], format="df", limit="chr1:1-10000")

  gffutils has no equivalent; the row-by-row path pays a Python object per
  row, which on a whole-genome corpus is millions of allocations and dominates
  everything else.

- ``gffbase stats`` — a summary of what is actually in a database: feature
  counts broken down by type with percentages, sequence count and names,
  discontinuous-feature count, and how the database was built (format, mode,
  schema version, which spatial index). The first question anyone asks of an
  unfamiliar annotation, which otherwise means writing the same throwaway
  script every time.

- **The compatibility submodules are bound on the package namespace.**
  gffutils binds ``attributes``, ``bins``, ``constants``, ``create`` and ``version`` on
  its package, so ``gffutils.constants.always_return_list = True`` works after a
  plain import. gffbase bound none of them, so the one-line migration its own
  README advertises -- ``import gffbase as gffutils`` -- raised ``AttributeError``
  on the first line of any script using one.

- ``FeatureDB.method()``, gffutils' alias for ``all_features()``. It is a plain
  alias upstream too, and ported code calls it.

- ``FeatureDB`` **connection lifecycle:** ``close()``\ **, context-manager
  support, and** ``read_only=True``\ **.** DuckDB holds an exclusive lock on the database file for
  the life of a writable connection, and there was no way to release it — no
  ``close``, no ``__enter__``/``__exit__``, no ``__del__``. Two things were therefore
  impossible: replacing or deleting a database file while any handle existed
  (fatal on Windows), and reading one annotation database from several worker
  processes at once — which is the shape of every PyTorch ``DataLoader`` job.

  .. code-block:: python

     with create_db("gencode.gtf.gz", "gencode.duckdb") as db:
         ...                                    # lock released at block exit

     db = FeatureDB("gencode.duckdb", read_only=True)   # N workers may share it

  ``close()`` is idempotent, and closes the connection only when this handle
  opened it: a caller who passes their own ``duckdb`` connection still owns it
  afterwards. ``create_db()`` transfers ownership explicitly, so the handle it
  returns does close the connection it created. The lazily-created segment
  cursor is always closed, because gffbase created it either way.

  Under ``read_only=True`` every mutator (``update``, ``delete``, ``add_relation``,
  ``add_relations``, ``analyze``) raises ``ReadOnlyError``, and ``upgrade="auto"`` is
  coerced to ``"never"`` so a v1 database cannot be migrated by a handle that
  promised not to write. ``set_pragmas()`` stays allowed: ``SET`` is session
  state, not a write to the file, and legacy callers pass
  ``constants.default_pragmas`` routinely. ``execute()`` is deliberately
  unguarded — it is the escape hatch, and DuckDB's own refusal names the
  statement it rejected.

  Using a closed handle raises ``ClosedDatabaseError`` naming the call and the
  remedy, rather than DuckDB's bare ``ConnectionException``. Both new exceptions
  subclass ``ValueError``, so existing ``except ValueError`` handlers keep working.

- ``FeatureDB`` **accepts** ``pathlib.Path``. The constructor took ``str`` only and
  rejected everything else with ``TypeError: dbfn must be a path`` — while
  refusing an actual ``Path``. ``create_db`` already accepted one, so the two
  entry points disagreed about their own documented type.

- **A** ``gffbase`` **command-line interface**, registered as a console script and
  runnable as ``python -m gffbase``. Ten commands: ``create``, ``fetch``,
  ``children``, ``parents``, ``region``, ``search``, ``rmdups``, ``sanitize``, plus
  gffbase-only ``validate`` and ``migrate``.

  Argument names and output shapes follow ``gffutils-cli`` so a script written
  against it keeps working. What does not follow it is how much of it runs. Of
  the thirteen commands ``gffutils-cli`` defines, **five work**: ``annotate`` and
  ``convert`` are defined but never registered, so they are unreachable from the
  shell; ``clean``, ``common`` and ``region`` raise ``NotImplementedError``; ``fetch``
  raises ``TypeError`` because ``helpers.get_gff_db`` hands it a path string on
  its common branch and it indexes that as a database; and ``search`` raises
  ``AttributeError`` because it calls ``db.attribute_search(...)``, a method that
  exists nowhere in gffutils. All ten gffbase commands work.

  Two conventions the tests enforce: **feature output goes to stdout and
  progress to stderr**, so ``gffbase rmdups in.gff > out.gff`` produces a valid
  file — upstream's ``rmdups`` prints its banner into the middle of the GFF it
  is writing — and a command that could not do what was asked says so in its
  **exit status**, not only in a message. ``gffbase validate --strict`` is the
  CI form.

  Uses ``argparse``, adding no dependency. ``gffutils`` makes ``argh`` and
  ``argcomplete`` hard runtime requirements of the library itself, so importing
  it at all pulls in a CLI framework.

- ``FeatureDB.attribute_search(text, featuretype=None)`` — case-insensitive
  ``LIKE`` over attribute values. gffutils' CLI calls this method; gffutils does
  not have it.

- **The gffutils module surface is complete.** All ten missing compatibility
  modules and every one of the 38 planned symbols are implemented — parity goes
  from **30/90 symbols (33%) to 87/90 (97%)**, with zero modules outstanding.
  ``deviations.toml`` no longer contains a single ``planned`` entry; what remains
  is a register of deliberate differences, each naming the test that pins it.

  New modules: ``gffbase.bins``, ``attributes``, ``constants``, ``convert``, ``create``,
  ``inspect``, ``version``, ``biopython_integration``, ``pybedtools_integration`` and
  ``contrib.plotting``. ``gffbase.helpers`` grows from one function to thirteen,
  including ``make_query``, ``infer_dialect``, ``sanitize_gff_db``,
  ``canonical_transcripts`` and ``get_gff_db``.

  These are not import shims. Each is tested for behaviour
  (``tests/test_compat_surface.py``), ``bins`` is differential-tested against the
  oracle over 20,044 comparisons with zero mismatches, and the two documented
  global toggles are wired into live code rather than merely exported:
  ``constants.always_return_list`` changes what ``feature.attributes[key]``
  returns, and ``constants.ignore_url_escape_characters`` turns percent
  decoding *and* re-encoding off together.

  Several upstream defects are deliberately not reproduced, and each is
  recorded with its reason: ``helpers.get_gff_db`` returns a ``FeatureDB`` rather
  than sometimes a path string (the inconsistency that makes
  ``gffutils-cli fetch`` raise ``TypeError`` in its common case);
  ``helpers.dialect_compare`` works on dialects carrying an ``order`` list, where
  the oracle raises ``TypeError: unhashable type: 'list'`` on all of them;
  ``helpers.to_unicode`` actually decodes bytes, where a ``2to3`` artifact left
  the oracle's body unreachable; ``helpers.canonical_transcripts`` selects the
  longest transcript rather than the shortest and does not print to stdout;
  ``helpers.make_query`` validates a string ``order_by`` instead of interpolating
  it verbatim; and ``helpers.annotate_gff_db`` raises rather than silently
  doing nothing.

  ``biopython_integration`` also fixes a genuine incompatibility: BioPython
  removed the ``SeqFeature(strand=...)`` argument and moved strand onto the
  location, so the oracle's call raises ``TypeError`` on any current install.
  The round trip here is exact for ``+``, ``-`` and ``.``.

- ``create_db`` **accepts an iterable of features**, not just a path or a
  string. This is what ``_FeatureIterator`` exists for upstream and what
  ``helpers.sanitize_gff_db`` needs.

- ``gffbase.interface.assign_child`` and ``no_children``, the two symbols
  ``merge_all`` is built from upstream, plus ``FeatureDB.add_relations`` for
  linking many pairs with a single closure rebuild.

- ``gffbase.helpers.merge_attributes``, the sorted-set union of two attribute
  mappings that every derived-feature method needs.

- ``FeatureDB.mode``, ``.validation`` and ``.on_error``, recovered from the database
  rather than assumed, and ``FeatureDB.derived_source`` — the ``source`` stamped on
  features gffbase derives rather than reads. It is ``gffutils_derived`` under
  ``mode="compat"`` so a ported script filtering on that string keeps working,
  and ``gffbase_derived`` under ``mode="strict"``, which reports honest provenance.

- **First-class discontinuous (multipart) GFF3 features.** Several lines sharing
  one ``ID`` — how NCBI represents a split CDS — are now one logical feature.
  Previously the second line collided against the primary key and every merge
  strategy lost information.

  - **Schema v2.** ``features`` keeps one row per *logical* feature, with its
    coordinates widened to the envelope, and ``segments`` is a sparse side table
    holding physical lines only where ``n_segments > 1``. Logical dedup stays
    structural (no ``DISTINCT`` anywhere), the R-tree stays the primary access
    path, storage grows with duplicate lines rather than corpus size, and a
    v1 → v2 migration touches zero feature rows. ``segments_all`` gives the
    one-row-per-input-line view.
  - ``MultipartFeature`` and ``FeatureSegment``, both subclassing ``Feature`` and
    overriding none of ``__str__``, ``__len__``, ``__hash__``, ``__eq__``,
    ``__getitem__`` or ``astuple`` — the compatibility surface is preserved by
    inaction. ``len()`` stays the envelope span; ``covered_length`` is the new
    quantity that excludes the gaps. Each segment carries its own phase, which
    is the reason the storage exists.
  - Fusing happens only under ``mode="strict"``, deliberately: gffutils' ``merge``
    requires all eight non-attribute columns to match, so it never merges a
    genuine split feature. ``on_multipart_conflict`` chooses between raising
    ``MultipartConstraintError`` and splitting when lines sharing an ``ID``
    disagree on seqid, source, featuretype or strand.
  - ``explode_segments=True`` on ``region_batched`` / ``children_batched`` /
    ``parents_batched`` yields one row per input line. Offered on the tabular
    APIs only — a ``FeatureSegment`` leaking into ``region()`` or ``children()``
    would corrupt legacy consumers.
  - Measured on the FlyBase 50k corpus: 345 discontinuous features over 690
    lines, and all 49,981 input lines round-trip byte for byte.

- ``Feature.to_line(normalized=False)`` and ``to_lines()``. The default is
  byte-faithful; ``normalized=True`` re-renders column 9 from the parsed mapping,
  which is what the oracle always does.

- ``gffbase.migrate`` — ``migrate_v1_to_v2()`` upgrades in place, in one
  transaction, idempotently, and is run automatically when a v1 database is
  opened (``FeatureDB(..., upgrade="auto"|"never"|"error")``). It is structural
  only and changes no query result, which is what makes doing it unasked
  acceptable. ``coalesce_multipart()`` is the separate, opt-in second step that
  re-fuses v1's ``x_1`` rows — it changes results, so the caller has to ask.
  Tested against a real v1 database built by the pre-v2 code, committed as
  ``tests/data/v1/``.

- ``gffbase.validate`` — 14 post-ingest invariants, run automatically at the
  end of a strict-mode ingest and available as ``db.validate()``. Every check is
  a single set-based query. The one that matters most is INV-5: a fused
  feature whose envelope is narrower than its segments simply stops being
  returned by ``region()``, with nothing raised anywhere.

- ``mypy`` runs clean over ``python/gffbase`` and is a CI gate, backing the
  ``Typing :: Typed`` classifier that 0.1.1 made honest by shipping ``py.typed``.

- **Full** ``create_db`` **option fidelity.** Twelve parameters were previously
  accepted and ignored; every one now changes behaviour or raises.

  - ``gffbase._options.IngestOptions`` validates the whole option set before any
    work starts -- in particular before the destination database is touched.
  - ``id_spec`` in all four shapes (attribute name, ordered list, per-featuretype
    mapping, callable), plus ``autoincrement:BASE`` and the ``:seqid:`` syntax for
    keying on a GFF column instead of an attribute. Defaults follow the
    dialect: ``"ID"`` for GFF3, ``{"gene": "gene_id", "transcript": "transcript_id"}`` for GTF -- which is what fixes ``gencode-v19.gtf``
    yielding 26 features against the oracle's 21, and ``ID=`` yielding an
    empty-string primary key instead of ``protein_1``.
  - All five ``merge_strategy`` values, and ``force_merge_fields`` with the
    oracle's ``ValueError`` on ``start``/``end`` and its warning on ``frame``/``strand``.
  - ``transform`` (a falsy return drops the feature, and mutations are
    persisted), ``checklines``, ``force_gff``, ``force_dialect_check``,
    ``from_string``, ``dialect``, ``_keep_tempfiles``, ``pragmas``, ``text_factory``,
    ``verbose``, and ``infer_gene_extent`` (deprecated: warns, then sets both
    ``disable_infer_*`` flags).
  - ``keep_order`` and ``sort_attribute_values`` now reach materialized features.
    They were stored on ``FeatureDB`` and never passed on, so both were inert.
  - Positional arguments work again, in the oracle's exact order. Every option
    had been made keyword-only, so any positional call written against
    gffutils raised ``TypeError``.
  - An unrecognized keyword raises ``TypeError``, matching the oracle's
    ``deprecation_handler``, rather than being absorbed by ``**kwargs``.

- Attribute values now follow the oracle's empty-value rule exactly: a wholly
  empty value (``ID=``) yields the key with *no* values, while a multi-valued
  attribute keeps its empty parts (``Parent=x,`` stays ``["x", ""]``). This
  matters because callers write ``if f.attributes["ID"]:``, and ``[""]`` is truthy
  where ``[]`` is not.

- **A gffutils parity harness**, pinned to upstream commit ``6b84330``:

  - ``tools/gen_parity_manifest.py`` generates a machine-readable inventory of
    the oracle's 19 modules and 90 public symbols, committed as
    ``tests/parity/gffutils_manifest.json`` so the structural checks run without
    gffutils installed. ``--check`` verifies it has not drifted.
  - ``tests/parity/deviations.toml`` records every difference, enforced in both
    directions: an undeclared gap fails, and so does a declaration that
    outlives the work it describes. Current state: **26 of 90 symbols (29%)**,
    with the remaining 10 modules and 30 symbols each declared and attributed
    to a delivering phase.
  - ``tests/parity/test_differential.py`` runs 30 vendored upstream fixtures
    through both libraries and compares feature ids, all nine GFF columns,
    attributes, dialect, directives, relations at every level, query results,
    serialization and failure modes. Known failures are ``xfail(strict=True)``
    per fixture, so a fix cannot land unnoticed.
  - ``tests/data/upstream/`` vendors the upstream corpus with full MIT
    attribution and provenance.

- ``mode="compat"`` / ``mode="strict"``. Validation conflated two independent
  questions -- which rules apply, and what a violation does. They are now
  separate axes (``validation``, ``on_error``) behind one switch, with ``compat`` as
  the default for ``create_db`` and ``strict`` for ``parse_gff``. ``strict=`` keeps
  working for one deprecation cycle; passing it together with ``on_error=``
  raises ``TypeError``.

- ``FeatureDB.warnings`` reports every specification violation tolerated while
  building the database, with kind, line number and message -- so a compat-mode
  caller gets exactly gffutils' data *plus* a diagnostic gffutils never
  offered.

- ``docs/design/schema-v2.md`` records the design for schema v2, the multipart
  feature model, and this mode axis.

- ``python/gffbase/py.typed``. The ``Typing :: Typed`` classifier was declared but
  no PEP 561 marker shipped, so downstream type checkers saw nothing.

- ``CODE_OF_CONDUCT.md`` — referenced by ``CONTRIBUTING.md`` but missing.

- ``CITATION.cff`` — referenced by ``README.md`` but missing.

- ``SECURITY.md`` and this ``CHANGELOG.md``.

- ``pandas``, ``polars``, ``fasta``, and ``all`` optional-dependency extras. The
  ``format="df"`` and ``format="polars"`` code paths were advertised with no way to
  install what they need.

- ``native``, ``rtree``, and ``slow`` pytest markers.

.. _changelog--fixed:

Fixed
~~~~~

- **CRLF files parsed differently on each engine.** The Rust parser trimmed
  the trailing ``\r`` only *after* directive handling had run, so every
  directive from a Windows-line-ended GFF3 was stored as
  ``sequence-region chr1 1 1000\r``, while the Python fallback -- which reads
  with universal newlines -- stored it clean. A blank ``\r\n`` line was also not
  empty by the time it was checked, so it fell through to the tab split and
  raised ``expected at least 9 tab-separated fields, found 1``. Both engines now
  trim before anything inspects the line.

- **A coordinate past** ``i64`` **was accepted by the Python fallback** (Python ints
  are unbounded) and deferred the failure to INSERT time, far from the line
  that caused it, and only on one engine. It is now rejected at the line, as
  the Rust engine already did.

- ``helpers.example_filename`` **could not find the canonical example.**
  ``FBgn0031208.gff`` -- the fixture every gffutils tutorial opens -- is vendored
  under ``tests/data/upstream/``, but that directory was not on the search path,
  so the call failed in a source checkout with the file sitting on disk. It
  then failed for a different reason everywhere else: ``tests/`` is not inside
  the package, so no ``pip install`` could reach the corpus at all, while the
  oracle ships its own examples and the migration guide advertised the two as
  equivalent. The corpus (35 files, 114 KB, with the vendored gffutils
  fixtures' MIT notice alongside them) now travels **inside the wheel**, and
  the error names the directory it searched. Note that ``.gitignore``
  blanket-ignores ``*.gff3``/``*.gtf``/``*.fa`` and maturin collects the package
  tree through gitignore -- so the first build of this shipped 9 of the 35
  files, silently, none of them the ones the documented examples open. A test
  now fails if any packaged fixture is ignored.

- ``"missing" in db`` **raised instead of returning** ``False``. ``gffutils.FeatureDB``
  defines neither ``__contains__`` nor ``__iter__``, so Python falls back to
  iterating via ``__getitem__``, which raises on the first missing key -- the
  ``in`` operator failing on precisely the question it exists to answer.
  Declared as an intentional deviation.

- ``region()`` **crashed on every database containing a discontinuous feature.**
  DuckDB's R-tree scan optimizer builds a projection map for the index scan,
  and any subquery sharing that ``WHERE`` clause throws its column numbering
  out — so pairing ``ST_Intersects`` with the multipart recheck aborted the
  planner with ``INTERNAL Error: Failed to bind column reference "file_order"``.
  Every region query against a RefSeq- or MANE-shaped corpus failed, on both
  ``region()`` and the ``_batched`` path.

  It is not about how the correlation is written: qualified, unqualified and
  rewritten-as-a-semi-join all fail identically, and the B-tree path is
  unaffected. The spatial scan is now wrapped in a derived table and the
  recheck applied outside it, keeping the two apart. ``EXPLAIN`` confirms the
  plan still contains ``RTREE_INDEX_SCAN (Index: features_rtree)``, so the index
  does the same work — the recheck filters its output rather than being fused
  into it.

- ``children_batched(level=None)`` **silently returned a truncated result set.**
  ``_batched_relation`` carried its own copy of the cache-vs-dynamic decision,
  and that copy was missing the overflow check: for ``level=None`` it asked only
  whether the closure was *empty*, never whether the hierarchy ran deeper than
  the cache. On a corpus deeper than ``max_depth`` it therefore chose the
  closure cache, which only reaches ``max_depth``.

  Measured on a six-level hierarchy with ``max_depth=2``: ``children()`` returned
  all six descendants and ``children_batched()`` returned two. Two APIs
  answering the same question differently, neither raising — in the batched
  API the project recommends for bulk ML extraction. Both now route through
  ``_dispatch_relation``; for a batch it asks "does any anchor overflow?" as a
  single query, so one overflowing anchor sends the whole batch to the
  dynamic CTE. ``parents_batched`` had the same defect.

- ``delete()`` **left orphaned rows in the transitive closure.** It removed only
  the closure rows that *named* the deleted id as ancestor or descendant. A
  depth-2 row names neither when it merely routed *through* the deleted
  node — delete the mRNA from ``gene → mRNA → exon`` and ``gene → exon`` survives —
  so ``children(gene, level=None)`` kept returning the exons of a transcript
  that no longer existed. The closure is now rebuilt from ``edges``, which is
  what ``update()`` already did.

- ``update()`` **and** ``add_relations()`` **left the dispatcher reading stale corpus
  statistics.** ``_closure_max_depth`` and ``_n_multipart`` are read once when a
  handle opens and trusted for its lifetime, but both mutators rebuilt the
  closure without refreshing either the instance attributes or the ``meta``
  rows — so relational routing kept deciding on the shape the database had
  before the write, and a handle opened later disagreed with the one that did
  it. All three mutators now refresh and persist both.

- ``DataIterator`` **never dispatched on its input.** The factory handed
  everything to the file-path iterator, so a URL was opened as a filename and
  an in-memory feature iterable raised — while ``_UrlIterator`` and
  ``_FeatureIterator`` sat unreachable beneath it, their docstrings describing a
  dispatch that did not exist. ``gffutils.DataIterator`` accepts all of these.
  The dispatch now exists; ``_UrlIterator`` also unlinks its download (it used
  ``NamedTemporaryFile(delete=False)`` and never removed it, leaking a full copy
  of the annotation per call) and gained ``close()`` plus context-manager
  support. ``_FeatureIterator.__iter__`` returned the underlying list's own
  iterator, bypassing ``__next__`` and silently dropping ``transform``.

- ``cargo test`` **could not link on macOS.** ``extension-module`` was enabled
  unconditionally in ``rust/Cargo.toml`` *and* passed by maturin
  (``features = ["pyo3/extension-module"]``). Enabling it tells the linker not
  to link libpython, which is right for the wheel and fatal for a test binary,
  so every ``cargo test`` died in a wall of "symbol(s) not found for architecture
  arm64" — on the platform ``CONTRIBUTING.md`` tells contributors to run it.
  maturin still supplies the feature for the wheel.

- ``.github/workflows/testpypi-release.yml`` **could not be loaded by GitHub
  Actions.** Its ``verify`` job declared ``name:`` and ``runs-on:`` twice. PyYAML's
  ``safe_load`` tolerates duplicate keys — last one wins — so a naive parse
  looked fine; the real parser rejects them, which means the release-candidate
  dress rehearsal had never been able to run. ``tests/test_release_hygiene.py``
  now parses every workflow with a duplicate-key-strict loader.

- **The sdist shipped a** ``MANIFEST.in`` **naming five files it did not contain.**
  maturin does not read ``MANIFEST.in`` — ``pyproject.toml`` says so — so
  ``CODE_OF_CONDUCT.md``, ``CONTRIBUTING.md``, ``SECURITY.md``, ``CITATION.cff`` and
  ``MIGRATION.md`` were referenced and absent. Found by unpacking a real sdist
  and running the suite from it, where the two hygiene tests that exist to
  check exactly this failed. The full suite now passes from an unpacked
  sdist, so "users can rebuild from sdist and run the suite" is true.

- The ``corpus`` pytest marker was declared and carried by **no test**, so
  ``pytest -m corpus`` selected nothing and reported success. It now has the
  harness it was declared for (``tests/test_corpus.py``): ingest-and-validate
  over all five whole-genome annotations, R-tree-vs-B-tree agreement at a
  scale where the spatial index earns its place, and a byte-faithful round
  trip. The ``native`` and ``rtree`` markers, also unused, are removed — both
  conditions are handled where they arise. ``hypothesis`` is no longer a
  declared test dependency; nothing imported it.

- **The release workflow could publish from a red tree.** ``ci.yml`` runs on
  pushes to ``main`` and ``release/*`` and **not on tags**, and the publish job
  depended only on the wheel builds — so a tag pushed from a failing tree went
  straight to PyPI with nothing having run the suite. Both release workflows
  now gate every builder on a ``verify`` job that builds the tagged commit,
  asserts the native extension is present, runs the tests, and refuses if the
  tag does not match ``gffbase.__version__``.

- **Neither release workflow could publish anything.** Both run
  ``tools/release_policy.py`` as their first job, on a bare ``setup-python``
  runner, and the script imports ``packaging`` -- which a fresh runner does not
  have and neither workflow installed. The policy step died with
  ``ModuleNotFoundError`` on every run, skipping qualification, artifacts and
  publish behind it. Every local check passed because the development
  environment carries ``packaging``. Found by a build-only rehearsal dispatch
  from the release branch, before the candidate tag was pushed; tagging first
  would have spent ``v0.2.0rc1`` on it, since a pushed tag is never moved. Both
  policy jobs now install ``packaging==26.2``, and a test derives the
  requirement from the script's own top-level imports, so a new third-party
  import there fails the suite rather than the next release.

- Both release workflows claimed ``abi3-py39`` covering "CPython 3.9-3.13"; the
  wheels are ``abi3-py310`` covering 3.10–3.14.

- ``gffbase migrate --coalesce`` **crashed on every invocation.** The command
  passed a path to ``coalesce_multipart``, which takes an open connection —
  ``AttributeError: 'str' object has no attribute 'execute'``. It also needed
  the connection to have the spatial extension loaded, or DuckDB refuses to
  modify a table carrying an R-tree index. Both fixed, and verified end to
  end against the committed v1 fixture: schema 1 → 2, one multipart feature
  re-fused.

- **The pure-Python fallback parser was 73% covered and had no direct tests.**
  It is the oracle the Rust parser is differentially compared against *and*
  the only parser on a wheel-less install, so it was the worst place in the
  codebase to be under-tested — a bug there could make a Rust bug look like
  agreement. Now 93%, with a dedicated ``tests/test_pyfallback_parser.py``.

  Two defects surfaced immediately. ``_FallbackIterator._drain_for_metadata``
  pulled a record to populate the dialect but never captured it, so
  ``.dialect()`` returned ``{}`` until something happened to iterate — the same
  call gave a populated dialect or an empty one depending on nothing the
  caller could see. And a file with directives but no features never reached
  a yield at all, so ``.dialect()["fmt"]`` was a ``KeyError`` on exactly the
  inputs a caller probes before deciding what to do. Both now match the Rust
  engine.

  The gap existed because every fallback test used a file smaller than
  ``checklines``, so the parser's *second* loop — which processes nearly every
  line of a real annotation — had never run.

- ``_FeatureIterator.dialect`` and ``.directives`` were methods where their base
  class has them as properties, with a ``type: ignore`` hiding the mypy error.
  The same expression worked against one iterator and raised
  ``TypeError: 'list' object is not callable`` against another.

- Removed two dead definitions from the fallback parser (``_parse_coord`` and
  ``_LazyGFFFormatErrorProxy``), neither referenced anywhere.

- ``FeatureDB.bed12()`` emitted a ``blockCount`` that counted *all* block
  children while ``blockSizes``/``blockStarts`` silently dropped any child with a
  missing coordinate, producing a BED12 line whose three block fields
  disagreed. All three now derive from the same filtered list.

- ``Feature.sequence()`` and ``FeatureDB.bed12()`` did unguarded arithmetic on
  nullable coordinates, raising ``TypeError: unsupported operand type(s) for -: 'NoneType' and 'int'`` instead of something actionable. Both now raise a
  ``ValueError`` naming the feature.

- Passing a hand-built ``Feature`` (which has ``id is None``) to ``db[...]``,
  ``children()``, ``parents()``, ``delete()`` or ``update()`` bound SQL NULL and
  silently matched nothing. It now raises.

- Roughly a dozen ``con.execute(...).fetchone()[0]`` call sites would raise
  ``TypeError: 'NoneType' object is not subscriptable`` on an empty result.
  They now go through ``gffbase._dbutil.scalar`` / ``scalar_or``.

- **Dialect inference was nondeterministic.** Both engines resolved a tied
  plurality vote over the attribute field separator through a randomly-seeded
  hash container -- ``HashMap`` in Rust, ``set()`` in Python -- so the winner
  varied between processes. Since that separator is what a re-serialized
  feature is written with, *the same annotation file could round-trip to
  different text on different runs of identical code*. Measured at 3 of 20
  runs disagreeing on ``gms2_example.gff3``. Both now tally in insertion order
  and break ties by first appearance. Guarded by
  ``tests/test_dialect_determinism.py``, which compares across fresh
  interpreters because a single-process test cannot see this class of bug.

- **Directives kept their** ``##`` **prefix.** ``db.directives`` is a documented
  attribute and the oracle stores directives with the prefix stripped
  (``gff-version 3``, not ``##gff-version 3``), so every consumer reading them
  saw the wrong strings.

- **Every derived-feature method disagreed with the oracle**, and there was no
  differential test over any of them — which is how each of these survived.

  - ``merge_all`` **did not do the two things it documents.** It returned every
    input feature, merged or not, so the result was the size of the database
    rather than the number of merges; and it persisted nothing, despite the
    docstring promising that "the resulting records are added to the
    database". It also **accepted** ``exclude_components`` **and ignored it**, so
    asking for the components to be removed silently did nothing. It now emits
    only genuine merges, inserts them, and either deletes the components or
    links them with a ``Parent`` pointing at the merged feature.

    The discriminator that makes this possible was missing too: ``merge()`` set
    ``children`` unconditionally, so every feature looked merged. A run of one
    now gets ``no_children``.

  - ``merge()`` **extended only** ``end``. With a caller-supplied ``merge_order``
    the run is not necessarily start-sorted, so a merged feature could be
    silently truncated at the front. It also re-sorted its input, discarding
    the very ordering ``merge_all`` had asked for; assigned no id, so the merged
    feature could not be deleted or linked; and never flagged ambiguity, so a
    merge across strands kept the first component's strand rather than ``.``.

  - ``create_introns`` **computed introns across transcript boundaries.**
    ``grandparent_featuretype="gene"`` was treated as the direct anchor, pooling
    every isoform's exons into one sorted list. On ``FBgn0031208.gff`` that was
    1 "intron" where the oracle finds 3, and for any multi-isoform gene the
    gaps produced were not introns of anything.

  - ``create_splice_sites`` **emitted 1 bp sites.** A splice site is a
    dinucleotide, so these named half of one. They were also always typed
    ``splice_site`` rather than by position in the transcript, and carried no
    attributes at all.

  - ``interfeatures`` stamped no derived ``source``, produced a nonsense
    feature spanning two different sequences whenever consecutive inputs
    changed seqid, typed unnamed gaps with a constant instead of
    ``inter_<a>_<b>``, never set ``strand`` to ``.`` on a mismatch, ignored
    ``numeric_sort``, and took a three-argument ``attribute_func`` where the
    oracle takes one — so any gffutils caller passing a callback got a
    ``TypeError``.

  - ``bed12`` put a trailing comma on ``blockSizes``/``blockStarts`` (making
    every line differ), accepted ``thin_featuretype`` and ignored it with no
    mutual-exclusion error, and on a feature with no CDS set both thickStart
    and thickEnd to ``chromStart`` — rendering it entirely *thin*, the opposite
    of what the oracle draws.

  - ``children_bp`` swallowed unknown keyword arguments, including the
    removed ``ignore_strand``, returning a plausible number instead of saying no.

  - **Three** ``merge_criteria`` **predicates were distance tests, not range
    tests.** ``overlap_end_threshold`` and friends computed
    ``abs(acc.end - cur.start) <= threshold``, which *rejects a feature lying
    entirely inside the accumulator* — the most unambiguous overlap there is.
    The three existing tests passed under both formulas and so had never
    pinned this; the case that separates them is now tested directly.

  - ``add_relation`` **discarded its callbacks' return values** and wrote
    nothing back, so ``child_func=assign_child`` set an attribute on a throwaway
    object. Callbacks were also skipped entirely when ids were passed instead
    of ``Feature``\ s. A batched ``add_relations`` was added because the closure is
    re-derived per call, which would have made ``merge_all`` quadratic.

  Backed by a new differential group comparing introns, interfeatures, bed12,
  children_bp and merge_all against gffutils 0.14 — none of which had any
  differential coverage before.

- **The ingest mode was not recorded anywhere.** ``meta`` held the dialect, the
  format, the R-tree flag and the depths, but nothing said whether a database
  had been built in ``compat`` or ``strict`` mode — so a file on disk could not
  report how it was made. It now stores ``mode``, ``validation`` and ``on_error``,
  readable as ``db.mode`` and friends. Additive, so no schema version bump; a
  database written earlier has no key and reads back as ``compat``, which is
  what it was.

- **Reading an attribute and writing the feature back out could emit
  structurally invalid GFF3.** Materializing ``feature.attributes`` takes
  serialization off the raw-bytes fast path, and there was no re-encode step,
  so percent-escaping was simply lost: ``Note=hello%20world`` came back as
  ``Note=hello world``, and — far worse — a value containing ``;`` or ``,`` came
  back bare. On ``tests/data/upstream/nonascii`` one attribute became five and
  column 9 gained three separators it should not have had. Nothing raised, in
  about the most ordinary usage pattern there is.

  The same defect had a *persistent* form: ``merge_strategy="merge"`` rebuilds
  ``features.attributes_blob`` from the decoded ``attributes`` rows, so an
  unescaped value was written into the database and every later read of that
  feature parsed one value as several.

  gffbase now has an encoder (``gffbase._serialize``), and ``gffutils.parser``'s
  ``Quoter``, ``quoter``, ``_reconstruct`` and ``quoted_semicolon_patterns`` are
  available under their upstream names. Note that it is deliberately **not**
  ``urllib.parse.quote``: the space is not a reserved character in GFF3 and
  non-ASCII is not escaped, so ``Name=CkIIα[Tik]-1`` survives unchanged where
  ``quote()`` would have produced ``CkII%CE%B1[Tik]-1``.

  Porting ``_reconstruct`` wholesale rather than only the encoder also fixed
  three things that were silently wrong on the normalized path: ``keep_order``
  and ``dialect["order"]`` were ignored, ``repeated keys`` was ignored (so
  ``Parent=a;Parent=b`` always collapsed to ``Parent=a,b``), and the field
  separator was flattened to ``;`` or ``;``, losing ``;``.

  Verified two ways. The vendored ``attr_test_cases.py`` table — upstream's own
  ground truth, 18 cases, shipped in ``tests/data/upstream/`` and until now used
  by nothing — round-trips exactly. And a new differential test asserts that
  ``to_line(normalized=True)`` reproduces the oracle's bytes for whole rendered
  lines across the shared corpus: 11 of 15 GFF3 fixtures match exactly, and
  the four that do not are dialect-inference differences, measured and
  recorded, not serialization ones.

  Three strict xfails retire.

- **Every synthesized GTF gene and transcript was invisible to R-tree**
  ``region()`` **queries.** ``seqid_map`` was populated during the R-tree build,
  which runs *after* GTF synthesis — so the pass that stamps a synthesized
  row's ``seqid_y`` and ``bbox`` joined an empty table and those rows kept a NULL
  envelope. On ``ensembl_gtf.txt`` the R-tree path returned 32 features where the
  B-tree path returned 33, silently omitting the transcript itself. Found by
  the new INV-8 within minutes of the validator existing.

- ``closure`` **could contain duplicate rows.** GFF3 permits a DAG — a feature
  may name several ``Parent``\ s — so the same descendant is reachable by two paths
  of equal length, and the recursive CTE's ``UNION ALL`` emitted one row per
  path. On ``random-chr.gff``, ``children(gene, level=2)`` returned five features
  of which only three were distinct. gffutils never had this because its
  ``relations`` table is keyed on exactly that triple.

- **A cyclic** ``Parent`` **graph made the hierarchy walks lap rather than
  terminate.** All three recursive walks followed the cycle until the depth
  budget ran out, so a two-feature cycle made ``children()`` return 64 rows — the
  same two features, thirty-two times each. Each walk now carries its path and
  refuses to revisit a node, which is free on well-formed data (in a DAG the
  filter cannot fire) and verified identical on the FlyBase 50k corpus. Cycles
  are logged and recorded rather than silently repaired.

- **A failed ingest left a file behind**: valid DuckDB with the full schema, no
  data and no metadata. Retrying then refused with "already exists. Pass
  force=True", and *opening the leftover produced an empty database that
  reported itself as current* — a missing ``schema_version`` looked like a v1
  database and was dutifully migrated. Ingest now builds beside the target and
  renames on success, so a failed ``force=True`` overwrite also leaves the
  original intact; being handed such a file from elsewhere is refused at open.

- **The UCSC** ``bin`` **column in the SQLite export was computed one level off** —
  ``_BINOFFSETS`` was missing its top entry and used 0 where the oracle uses 1,
  differing from ``gffutils.bins`` on ten of eleven representative ranges. Since
  ``gffutils.FeatureDB.region(completely_within=True)`` filters on ``bin``, an
  exported database answered those queries with nothing at all.

- ``export_sqlite`` wrote one row per *logical* feature, so a discontinuous
  feature was exported with its envelope coordinates rather than its lines. It
  now flattens through ``segments_all`` into the N features gffutils itself would
  have made, fanning relations out over both endpoints and recording the
  grouping in ``duplicates``.

- The ``attributes`` table disagreed with ``Feature.attributes`` for a wholly empty
  value: ``pseudo=`` was indexed as a row while the object reported ``[]``, so a
  SQL query and the object model gave different answers for the same feature —
  and a bare ``Parent=`` created an edge to the empty id.

- GTF-synthesized gene and transcript rows ignored a caller-supplied ``id_spec``,
  taking the grouping key regardless. They now honour it, with the named
  attribute carried onto the inferred row first (so ``{"gene": "gene_name"}``
  yields a gene actually named after ``gene_name``, not an autoincremented
  fallback), and the rename applied *after* the edges are built so the
  hierarchy survives it.

- ``merge_strategy="merge"`` never regenerated ``attributes_blob``, so a merge was
  invisible to every caller: the table held both values while the feature
  reported one.

- **gffbase rejected 6 of the 23 upstream fixtures gffutils reads**, including
  ``FBgn0031208.gff``, gffutils' own canonical fixture. ``rust/src/validate.rs``
  validated to the NCBI GFF3 specification unconditionally, but ``create_db()``
  is the compatibility entry point and real annotation files break that spec
  routinely. Under ``compat`` the rules still run and every violation is
  reported, but the record is kept. Corpus-wide result: **0 rejections, and 23
  of 28 files now produce byte-identical feature counts.**

- **An embedded FASTA section without a** ``##FASTA`` **directive was parsed as
  features.** A bare ``>`` line ends the feature section in gffutils; gffbase
  only stopped at the directive, so ``FBgn0031208.gff`` gained three junk
  features from its sequence lines.

- **The two engines disagreed on padded coordinates.** Python's ``int()`` strips
  surrounding whitespace and Rust's ``parse::<i64>()`` does not, so the Rust
  engine dropped any record with a coordinate like ``944828`` while the
  pure-Python fallback kept it -- a silent, engine-dependent difference in
  which records exist. ``wormbase_gff2.txt`` exercises it.

- **Null coordinates now round-trip.** ``features.start``/``"end"`` were declared
  ``NOT NULL``, so the Arrow batch builder coerced a ``.`` column to ``0``: the
  feature reopened as ``0..0`` and serialized zeros where the source said ``.``.
  The columns are nullable, the coercion is gone, and the R-tree envelope is
  CASE-guarded so a null coordinate yields a null bbox instead of failing the
  insert. Verified that the R-tree and B-tree paths agree on which rows a
  region query returns -- they reach that answer by different routes (a null
  envelope never intersects; a null comparison is never true), so agreement
  was not automatic.

- Six coordinate-space operations raised
  ``TypeError: '<' not supported between instances of 'int' and 'NoneType'``
  once coordinates could be null: ``merge``, ``merge_all``, ``interfeatures``,
  ``create_introns``, ``create_splice_sites`` and ``bed12``. They now skip features
  that have no position, via one shared ``_with_coordinates`` filter -- a
  feature outside coordinate space is not in the input domain of a coordinate
  operation, and raising instead would make ``merge_all()`` unusable on any file
  containing such a row (WormBase emits them).

- ``bed12`` filtered null-coordinate block children *after* sorting them, so the
  guard added earlier in this release was unreachable and the sort raised
  ``TypeError`` first.

- ``merge_strategy="merge"`` **did not actually merge, as far as any caller
  could tell.** It folded the incoming attributes into the ``attributes`` table
  but never regenerated ``attributes_blob``, and ``Feature.attributes`` reads the
  blob -- so the table held both values and the feature reported one. Merged
  attributes now match the oracle exactly.

- Removed ``ingest._derive_id``, dead since the id_spec work replaced it.

- ``import gffbase`` **crashed on Python 3.9.** ``ParsedFeature`` used
  ``@dataclass(slots=True)``, which is Python 3.10+, while the package declared
  ``requires-python >=3.9``, shipped an ``abi3-py39`` wheel, and advertised a 3.9
  classifier. Every 3.9 install succeeded and then failed on first import with
  ``TypeError: dataclass() got an unexpected keyword argument 'slots'``. ``slots``
  is now applied conditionally, so 3.10+ keeps the per-record memory saving and
  3.9 works.

- ``gffbase.gffwriter`` referenced an undefined ``io`` name in the ``GFFWriter.__init__``
  type annotation (``F821``). The module is now imported.

- ``_pyfallback.parser`` re-raised a coordinate parse failure without chaining,
  masking the original ``ValueError`` (``B904``).

- The ``cargo test`` doc-test target failed to compile: a module doc comment in
  ``rust/src/lib.rs`` used an indented block that rustdoc interpreted as Rust
  source. CI only ran ``cargo test --lib``, so this was never seen.

- Removed a dead ``parse_coord`` in ``rust/src/parser.rs``, superseded by
  ``parse_coord_strict``, which caused a ``dead_code`` warning.

.. _changelog--removed:

Removed
~~~~~~~

- Six ``PHASE*.md`` entries from ``pyproject.toml`` and ``MANIFEST.in`` referring to
  files deleted in ``44268ce``, plus a ``recursive-include python/gffbase *.pyi``
  matching no files.

- **The** ``memmap2`` **Rust dependency**, which was declared and never used — no
  ``Mmap`` appears anywhere in the crate. An unused dependency is still
  compiled, still locked, and still part of the supply chain of every
  published wheel.

- **The** ``bench/`` **directory.** It was the predecessor of ``benchmarks/``, and
  ``benchmarks/common.py`` reached into it for cached legacy timings, which is
  how measurements from an older corpus ended up presented as current ones.
  Its small result files are preserved under
  ``benchmarks/results/archive/0.1.0/``.

- **Thirty-three internal "Phase N" references** from docstrings and comments
  across ten modules. These rendered on the public mkdocstrings API reference
  — ``gffbase.__init__``'s module docstring opened "Phase 5: full drop-in public
  API surface … on top of the Phase 4 DuckDB ingestion engine" — and named a
  development schedule no reader has access to.

.. _changelog--intentional-deviations:

Intentional deviations
~~~~~~~~~~~~~~~~~~~~~~

- **Attribute keys are stripped of surrounding whitespace, and the empty key a
  trailing** ``;`` **produces is dropped.** The oracle keeps both literally, and on
  ``FBgn0031208.gff`` line 84 that costs it real data: the line separates
  attributes with ``;`` while the file's inferred separator is ``;``, so the key
  is stored as ``' Parent'``, relationship building looks up ``'Parent'``, and the
  edge silently vanishes -- ``db.parents("CDS:Fk_gene_1:1")`` returns ``[]`` under
  gffutils and ``["Fk_gene_1", "transcript_Fk_gene_1"]`` under gffbase.
  Compatibility mode preserves quirks, but not data-loss defects.

.. _changelog--known-gaps-recorded-by-the-new-harness:

Known gaps recorded by the new harness
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

Not yet fixed, but now measured and pinned rather than unknown:

- The oracle weights its dialect vote by attribute count; gffbase weights all
  sampled lines equally. Both are deterministic; only the winner can differ,
  and only where a file's attribute strings vary in length.

- The oracle renders a valueless attribute as a bare ``ID`` where gffbase
  re-emits the source's ``ID=``. This one is deliberate — ``str(feature)`` is
  byte-faithful by design, so gffbase reproduces the input and the oracle does
  not. ``to_line(normalized=True)`` matches the oracle exactly.

- gffbase strips whitespace around attribute keys where the oracle keeps it
  literally. Also deliberate: on ``FBgn0031208.gff`` the oracle's behaviour
  silently loses a ``Parent`` edge, and compatibility mode preserves quirks but
  not data-loss defects.

(Two entries left this list: GTF synthesis now honours ``id_spec``, and
attribute escaping now survives materialization.)

----

.. _changelog--notes:

Notes
~~~~~

Version 0.1.0 remains the only published release while 0.2.0 completes its
hardening and release-evidence gates. Its metadata advertises Python 3.9
support that the artifact cannot deliver; users who cannot install the 0.2.0
candidate from source should apply the mitigations in the security advisory.

----

.. _changelog--010-2026-05-07:

`0.1.0 <https://github.com/Kuanhao-Chao/gffbase/releases/tag/v0.1.0>`__ — 2026-05-07
------------------------------------------------------------------------------------

Initial public release.
