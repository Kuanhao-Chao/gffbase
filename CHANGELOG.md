# Changelog

All notable changes to GFFBase are documented here.

The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and
this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

Work toward 0.2.0: genuine `gffutils` 0.14 API/CLI parity, first-class support
for discontinuous (multipart) GFF3 features, a `compat`/`strict` mode axis,
transactional storage, and a release pipeline gated on validation.

### Security

- **SQL injection through `order_by` (affects 0.1.0 and 0.1.1).** The parameter
  was interpolated into the query, with anything outside a small set of known
  column names passed through verbatim as a deliberate escape hatch "for power
  users". DuckDB executes trailing statements, so

      db.all_features(order_by='start ASC; DROP TABLE attributes; SELECT …')

  dropped the table **and still returned rows** — the trailing `SELECT`
  re-supplies the projection the result generator expects, so the call raises
  nothing and the damage is invisible from the call site. Any statement DuckDB
  accepts could be substituted, including `COPY … TO` to write local files.
  All four entry points were affected (`all_features`, `features_of_type`,
  `children`, `parents`); the joined paths had their own copy of the
  pass-through.

  gffutils contains the same interpolation and is **not** exploitable, because
  SQLite refuses to execute more than one statement per call. gffbase inherited
  the API shape and lost that accidental protection when it changed storage
  engine.

  `order_by` is now a whitelist, shared by both clause builders so no future
  entry point can reacquire an escape hatch.

- **SQL injection through `set_pragmas` (affects 0.1.0 and 0.1.1).** The same
  defect one method away, and quieter. `FeatureDB.set_pragmas()` built
  `PRAGMA {name} = {value}` by interpolating **both** halves of a
  caller-supplied dict, with the whole loop body inside
  `except duckdb.Error: continue` — so

      db.set_pragmas({"threads": "1; DROP TABLE attributes"})

  dropped the table, and a payload that *failed* was swallowed too, leaving no
  trace anywhere. The swallow existed for a real reason — ported gffutils code
  passes `constants.default_pragmas` (`synchronous`, `journal_mode`,
  `main.page_size`, `main.cache_size`), none of which DuckDB has — but it could
  not tell "this is a SQLite pragma" from "DuckDB rejected this".

  Names are now matched against DuckDB's own settings catalog and values
  rendered as SQL literals, so neither reaches the parser as syntax. Matching
  the live catalog rather than a hardcoded list means the check cannot go stale
  against a newer DuckDB, and the compatibility behaviour is unchanged but now
  deliberate: an unrecognized name is skipped and logged, not guessed at.

  Unlike `order_by`, **gffutils is vulnerable here too** — its version calls
  `cursor.executescript()`, which exists precisely to run several statements.
  Verified against 0.14. Not reported upstream; that is a maintainer decision.

  An audit of every remaining f-string SQL site found no third instance.

  See `docs/security/2026-sql-injection.md` for both write-ups and mitigations
  for anyone who cannot upgrade.

- **The B-tree CI job was red.** `test_every_invariant_actually_ran` required
  INV-8 to have run and `report.skipped` to be empty, but INV-8 compares `bbox`
  against the coordinates it was built from and therefore only exists when an
  R-tree does. Under `GFFBASE_TEST_DISABLE_RTREE=1` the validator correctly
  records it as skipped, which the test read as a failure. The skip is the
  designed behaviour, so it is now what the test asserts.

### Changed (breaking)

- **Minimum Python is now 3.10** (was 3.9), and 3.14 is supported. The wheel
  tag moves from `abi3-py39` to `abi3-py310`. This removes the split
  dependency story: current DuckDB and PyArrow both require 3.10+, so one
  dependency set now covers the whole supported range. Dependency floors were
  raised to the versions actually tested (`duckdb>=1.4.1`, `pyarrow>=18.1`)
  from the previously untested `duckdb>=1.0`, `pyarrow>=14`.
- **PyO3 0.22 → 0.29.** Migrated off the removed `*_bound` constructors,
  `into_py`, `value_bound`, and `get_type_bound`.

### Fixed

- `FeatureDB.bed12()` emitted a `blockCount` that counted *all* block
  children while `blockSizes`/`blockStarts` silently dropped any child with a
  missing coordinate, producing a BED12 line whose three block fields
  disagreed. All three now derive from the same filtered list.
- `Feature.sequence()` and `FeatureDB.bed12()` did unguarded arithmetic on
  nullable coordinates, raising `TypeError: unsupported operand type(s) for -:
  'NoneType' and 'int'` instead of something actionable. Both now raise a
  `ValueError` naming the feature.
- Passing a hand-built `Feature` (which has `id is None`) to `db[...]`,
  `children()`, `parents()`, `delete()` or `update()` bound SQL NULL and
  silently matched nothing. It now raises.
- Roughly a dozen `con.execute(...).fetchone()[0]` call sites would raise
  `TypeError: 'NoneType' object is not subscriptable` on an empty result.
  They now go through `gffbase._dbutil.scalar` / `scalar_or`.
- **Dialect inference was nondeterministic.** Both engines resolved a tied
  plurality vote over the attribute field separator through a randomly-seeded
  hash container -- `HashMap` in Rust, `set()` in Python -- so the winner
  varied between processes. Since that separator is what a re-serialized
  feature is written with, *the same annotation file could round-trip to
  different text on different runs of identical code*. Measured at 3 of 20
  runs disagreeing on `gms2_example.gff3`. Both now tally in insertion order
  and break ties by first appearance. Guarded by
  `tests/test_dialect_determinism.py`, which compares across fresh
  interpreters because a single-process test cannot see this class of bug.
- **Directives kept their `##` prefix.** `db.directives` is a documented
  attribute and the oracle stores directives with the prefix stripped
  (`gff-version 3`, not `##gff-version 3`), so every consumer reading them
  saw the wrong strings.
- **Every synthesized GTF gene and transcript was invisible to R-tree
  `region()` queries.** `seqid_map` was populated during the R-tree build,
  which runs *after* GTF synthesis — so the pass that stamps a synthesized
  row's `seqid_y` and `bbox` joined an empty table and those rows kept a NULL
  envelope. On `ensembl_gtf.txt` the R-tree path returned 32 features where the
  B-tree path returned 33, silently omitting the transcript itself. Found by
  the new INV-8 within minutes of the validator existing.
- **`closure` could contain duplicate rows.** GFF3 permits a DAG — a feature
  may name several `Parent`s — so the same descendant is reachable by two paths
  of equal length, and the recursive CTE's `UNION ALL` emitted one row per
  path. On `random-chr.gff`, `children(gene, level=2)` returned five features
  of which only three were distinct. gffutils never had this because its
  `relations` table is keyed on exactly that triple.
- **A cyclic `Parent` graph made the hierarchy walks lap rather than
  terminate.** All three recursive walks followed the cycle until the depth
  budget ran out, so a two-feature cycle made `children()` return 64 rows — the
  same two features, thirty-two times each. Each walk now carries its path and
  refuses to revisit a node, which is free on well-formed data (in a DAG the
  filter cannot fire) and verified identical on the FlyBase 50k corpus. Cycles
  are logged and recorded rather than silently repaired.
- **A failed ingest left a file behind**: valid DuckDB with the full schema, no
  data and no metadata. Retrying then refused with "already exists. Pass
  force=True", and *opening the leftover produced an empty database that
  reported itself as current* — a missing `schema_version` looked like a v1
  database and was dutifully migrated. Ingest now builds beside the target and
  renames on success, so a failed `force=True` overwrite also leaves the
  original intact; being handed such a file from elsewhere is refused at open.
- **The UCSC `bin` column in the SQLite export was computed one level off** —
  `_BINOFFSETS` was missing its top entry and used 0 where the oracle uses 1,
  differing from `gffutils.bins` on ten of eleven representative ranges. Since
  `gffutils.FeatureDB.region(completely_within=True)` filters on `bin`, an
  exported database answered those queries with nothing at all.
- `export_sqlite` wrote one row per *logical* feature, so a discontinuous
  feature was exported with its envelope coordinates rather than its lines. It
  now flattens through `segments_all` into the N features gffutils itself would
  have made, fanning relations out over both endpoints and recording the
  grouping in `duplicates`.
- The `attributes` table disagreed with `Feature.attributes` for a wholly empty
  value: `pseudo=` was indexed as a row while the object reported `[]`, so a
  SQL query and the object model gave different answers for the same feature —
  and a bare `Parent=` created an edge to the empty id.
- GTF-synthesized gene and transcript rows ignored a caller-supplied `id_spec`,
  taking the grouping key regardless. They now honour it, with the named
  attribute carried onto the inferred row first (so `{"gene": "gene_name"}`
  yields a gene actually named after `gene_name`, not an autoincremented
  fallback), and the rename applied *after* the edges are built so the
  hierarchy survives it.
- `merge_strategy="merge"` never regenerated `attributes_blob`, so a merge was
  invisible to every caller: the table held both values while the feature
  reported one.

### Added

- **First-class discontinuous (multipart) GFF3 features.** Several lines sharing
  one `ID` — how NCBI represents a split CDS — are now one logical feature.
  Previously the second line collided against the primary key and every merge
  strategy lost information.
  - **Schema v2.** `features` keeps one row per *logical* feature, with its
    coordinates widened to the envelope, and `segments` is a sparse side table
    holding physical lines only where `n_segments > 1`. Logical dedup stays
    structural (no `DISTINCT` anywhere), the R-tree stays the primary access
    path, storage grows with duplicate lines rather than corpus size, and a
    v1 → v2 migration touches zero feature rows. `segments_all` gives the
    one-row-per-input-line view.
  - `MultipartFeature` and `FeatureSegment`, both subclassing `Feature` and
    overriding none of `__str__`, `__len__`, `__hash__`, `__eq__`,
    `__getitem__` or `astuple` — the compatibility surface is preserved by
    inaction. `len()` stays the envelope span; `covered_length` is the new
    quantity that excludes the gaps. Each segment carries its own phase, which
    is the reason the storage exists.
  - Fusing happens only under `mode="strict"`, deliberately: gffutils' `merge`
    requires all eight non-attribute columns to match, so it never merges a
    genuine split feature. `on_multipart_conflict` chooses between raising
    `MultipartConstraintError` and splitting when lines sharing an `ID`
    disagree on seqid, source, featuretype or strand.
  - `explode_segments=True` on `region_batched` / `children_batched` /
    `parents_batched` yields one row per input line. Offered on the tabular
    APIs only — a `FeatureSegment` leaking into `region()` or `children()`
    would corrupt legacy consumers.
  - Measured on the FlyBase 50k corpus: 345 discontinuous features over 690
    lines, and all 49,981 input lines round-trip byte for byte.
- `Feature.to_line(normalized=False)` and `to_lines()`. The default is
  byte-faithful; `normalized=True` re-renders column 9 from the parsed mapping,
  which is what the oracle always does.
- **`gffbase.migrate`** — `migrate_v1_to_v2()` upgrades in place, in one
  transaction, idempotently, and is run automatically when a v1 database is
  opened (`FeatureDB(..., upgrade="auto"|"never"|"error")`). It is structural
  only and changes no query result, which is what makes doing it unasked
  acceptable. `coalesce_multipart()` is the separate, opt-in second step that
  re-fuses v1's `x_1` rows — it changes results, so the caller has to ask.
  Tested against a real v1 database built by the pre-v2 code, committed as
  `tests/data/v1/`.
- **`gffbase.validate`** — 14 post-ingest invariants, run automatically at the
  end of a strict-mode ingest and available as `db.validate()`. Every check is
  a single set-based query. The one that matters most is INV-5: a fused
  feature whose envelope is narrower than its segments simply stops being
  returned by `region()`, with nothing raised anywhere.
- `mypy` runs clean over `python/gffbase` and is a CI gate, backing the
  `Typing :: Typed` classifier that 0.1.1 made honest by shipping `py.typed`.
- **Full `create_db` option fidelity.** Twelve parameters were previously
  accepted and ignored; every one now changes behaviour or raises.
  - `gffbase._options.IngestOptions` validates the whole option set before any
    work starts -- in particular before the destination database is touched.
  - `id_spec` in all four shapes (attribute name, ordered list, per-featuretype
    mapping, callable), plus `autoincrement:BASE` and the `:seqid:` syntax for
    keying on a GFF column instead of an attribute. Defaults follow the
    dialect: `"ID"` for GFF3, `{"gene": "gene_id", "transcript":
    "transcript_id"}` for GTF -- which is what fixes `gencode-v19.gtf`
    yielding 26 features against the oracle's 21, and `ID=` yielding an
    empty-string primary key instead of `protein_1`.
  - All five `merge_strategy` values, and `force_merge_fields` with the
    oracle's `ValueError` on `start`/`end` and its warning on `frame`/`strand`.
  - `transform` (a falsy return drops the feature, and mutations are
    persisted), `checklines`, `force_gff`, `force_dialect_check`,
    `from_string`, `dialect`, `_keep_tempfiles`, `pragmas`, `text_factory`,
    `verbose`, and `infer_gene_extent` (deprecated: warns, then sets both
    `disable_infer_*` flags).
  - `keep_order` and `sort_attribute_values` now reach materialized features.
    They were stored on `FeatureDB` and never passed on, so both were inert.
  - Positional arguments work again, in the oracle's exact order. Every option
    had been made keyword-only, so any positional call written against
    gffutils raised `TypeError`.
  - An unrecognized keyword raises `TypeError`, matching the oracle's
    `deprecation_handler`, rather than being absorbed by `**kwargs`.
- Attribute values now follow the oracle's empty-value rule exactly: a wholly
  empty value (`ID=`) yields the key with *no* values, while a multi-valued
  attribute keeps its empty parts (`Parent=x,` stays `["x", ""]`). This
  matters because callers write `if f.attributes["ID"]:`, and `[""]` is truthy
  where `[]` is not.
- **A gffutils parity harness**, pinned to upstream commit `6b84330`:
  - `tools/gen_parity_manifest.py` generates a machine-readable inventory of
    the oracle's 19 modules and 90 public symbols, committed as
    `tests/parity/gffutils_manifest.json` so the structural checks run without
    gffutils installed. `--check` verifies it has not drifted.
  - `tests/parity/deviations.toml` records every difference, enforced in both
    directions: an undeclared gap fails, and so does a declaration that
    outlives the work it describes. Current state: **26 of 90 symbols (29%)**,
    with the remaining 10 modules and 30 symbols each declared and attributed
    to a delivering phase.
  - `tests/parity/test_differential.py` runs 30 vendored upstream fixtures
    through both libraries and compares feature ids, all nine GFF columns,
    attributes, dialect, directives, relations at every level, query results,
    serialization and failure modes. Known failures are `xfail(strict=True)`
    per fixture, so a fix cannot land unnoticed.
  - `tests/data/upstream/` vendors the upstream corpus with full MIT
    attribution and provenance.

### Changed (breaking)

- **`FeatureDB.schema` is a method again**, not a property. gffutils documents
  `db.schema()` and callers write it that way; as a property the documented
  call raised `TypeError: 'str' object is not callable`.
- **`merge_strategy="error"` is now the real default**, so a file with
  duplicate IDs raises instead of loading with silently renamed rows.
  Ingestion previously renamed every duplicate to `<id>__2` unconditionally,
  which made the documented default unreachable. `create_unique` now produces
  the oracle's `<id>_1`, `<id>_2` rather than `<id>__2`, `<id>__3`, and no
  longer writes `duplicates` rows -- the oracle records a rename only when
  `merge` falls back to `create_unique`, because that table exists so a later
  merge can find the sibling rows.
- **`DuplicateIDError`, `AttributeStringError` and `EmptyInputError` now
  subclass `ValueError`.** gffutils exports `DuplicateIDError` but raises a
  bare `ValueError("Duplicate ID ...")`, so real callers write
  `except ValueError`. Subclassing satisfies both the documented type and
  those callers instead of forcing a choice. `FeatureNotFoundError` is
  deliberately left on `Exception`.

### Added

- **`mode="compat"` / `mode="strict"`.** Validation conflated two independent
  questions -- which rules apply, and what a violation does. They are now
  separate axes (`validation`, `on_error`) behind one switch, with `compat` as
  the default for `create_db` and `strict` for `parse_gff`. `strict=` keeps
  working for one deprecation cycle; passing it together with `on_error=`
  raises `TypeError`.
- `FeatureDB.warnings` reports every specification violation tolerated while
  building the database, with kind, line number and message -- so a compat-mode
  caller gets exactly gffutils' data *plus* a diagnostic gffutils never
  offered.
- `docs/design/schema-v2.md` records the design for schema v2, the multipart
  feature model, and this mode axis.

### Fixed

- **gffbase rejected 6 of the 23 upstream fixtures gffutils reads**, including
  `FBgn0031208.gff`, gffutils' own canonical fixture. `rust/src/validate.rs`
  validated to the NCBI GFF3 specification unconditionally, but `create_db()`
  is the compatibility entry point and real annotation files break that spec
  routinely. Under `compat` the rules still run and every violation is
  reported, but the record is kept. Corpus-wide result: **0 rejections, and 23
  of 28 files now produce byte-identical feature counts.**
- **An embedded FASTA section without a `##FASTA` directive was parsed as
  features.** A bare `>` line ends the feature section in gffutils; gffbase
  only stopped at the directive, so `FBgn0031208.gff` gained three junk
  features from its sequence lines.
- **The two engines disagreed on padded coordinates.** Python's `int()` strips
  surrounding whitespace and Rust's `parse::<i64>()` does not, so the Rust
  engine dropped any record with a coordinate like `944828 ` while the
  pure-Python fallback kept it -- a silent, engine-dependent difference in
  which records exist. `wormbase_gff2.txt` exercises it.

### Fixed (continued)

- **Null coordinates now round-trip.** `features.start`/`"end"` were declared
  `NOT NULL`, so the Arrow batch builder coerced a `.` column to `0`: the
  feature reopened as `0..0` and serialized zeros where the source said `.`.
  The columns are nullable, the coercion is gone, and the R-tree envelope is
  CASE-guarded so a null coordinate yields a null bbox instead of failing the
  insert. Verified that the R-tree and B-tree paths agree on which rows a
  region query returns -- they reach that answer by different routes (a null
  envelope never intersects; a null comparison is never true), so agreement
  was not automatic.
- Six coordinate-space operations raised
  `TypeError: '<' not supported between instances of 'int' and 'NoneType'`
  once coordinates could be null: `merge`, `merge_all`, `interfeatures`,
  `create_introns`, `create_splice_sites` and `bed12`. They now skip features
  that have no position, via one shared `_with_coordinates` filter -- a
  feature outside coordinate space is not in the input domain of a coordinate
  operation, and raising instead would make `merge_all()` unusable on any file
  containing such a row (WormBase emits them).
- `bed12` filtered null-coordinate block children *after* sorting them, so the
  guard added earlier in this release was unreachable and the sort raised
  `TypeError` first.
- **`merge_strategy="merge"` did not actually merge, as far as any caller
  could tell.** It folded the incoming attributes into the `attributes` table
  but never regenerated `attributes_blob`, and `Feature.attributes` reads the
  blob -- so the table held both values and the feature reported one. Merged
  attributes now match the oracle exactly.
- Removed `ingest._derive_id`, dead since the id_spec work replaced it.

### Intentional deviations

- **Attribute keys are stripped of surrounding whitespace, and the empty key a
  trailing `;` produces is dropped.** The oracle keeps both literally, and on
  `FBgn0031208.gff` line 84 that costs it real data: the line separates
  attributes with `; ` while the file's inferred separator is `;`, so the key
  is stored as `' Parent'`, relationship building looks up `'Parent'`, and the
  edge silently vanishes -- `db.parents("CDS:Fk_gene_1:1")` returns `[]` under
  gffutils and `["Fk_gene_1", "transcript_Fk_gene_1"]` under gffbase.
  Compatibility mode preserves quirks, but not data-loss defects.

### Known gaps recorded by the new harness

Not yet fixed, but now measured and pinned rather than unknown:

- **GTF synthesis ignores `id_spec`.** The oracle applies the id_spec to
  inferred gene/transcript rows as well as authored ones; gffbase keys
  inferred rows on the `transcript_id` attribute unconditionally, so a custom
  scalar id_spec over a GTF yields extra rows.
- Attribute escaping is lost once attributes are materialized.
- The oracle weights its dialect vote by attribute count; gffbase weights all
  sampled lines equally.

---

## [0.1.1] — unreleased

A correctness and release-hygiene patch. No API changes.

### Fixed

- **`import gffbase` crashed on Python 3.9.** `ParsedFeature` used
  `@dataclass(slots=True)`, which is Python 3.10+, while the package declared
  `requires-python >=3.9`, shipped an `abi3-py39` wheel, and advertised a 3.9
  classifier. Every 3.9 install succeeded and then failed on first import with
  `TypeError: dataclass() got an unexpected keyword argument 'slots'`. `slots`
  is now applied conditionally, so 3.10+ keeps the per-record memory saving and
  3.9 works.
- `gffbase.gffwriter` referenced an undefined `io` name in the `GFFWriter.__init__`
  type annotation (`F821`). The module is now imported.
- `_pyfallback.parser` re-raised a coordinate parse failure without chaining,
  masking the original `ValueError` (`B904`).
- The `cargo test` doc-test target failed to compile: a module doc comment in
  `rust/src/lib.rs` used an indented block that rustdoc interpreted as Rust
  source. CI only ran `cargo test --lib`, so this was never seen.
- Removed a dead `parse_coord` in `rust/src/parser.rs`, superseded by
  `parse_coord_strict`, which caused a `dead_code` warning.

### Changed

- **`rust/Cargo.lock` is now committed.** `rust/Cargo.toml` and `MANIFEST.in`
  both already claimed it was shipped; `.gitignore` excluded it. Dependency
  resolution for the published wheel therefore varied with build time and
  platform, contradicting the declared MSRV.
- **Coverage flags moved out of the default `pytest` invocation.** `addopts`
  hard-required `pytest-cov` (absent from the `dev` extra) and made a bare
  `pytest` fail on coverage rather than on tests. Coverage is now applied
  explicitly in CI. `pytest-cov` was added to the `dev` extra.
- **Declared Rust MSRV raised from 1.69 to 1.83.** The 1.69 claim was justified
  by a `Cargo.lock` that was gitignored and absent, so it had never been
  verified; with the lock now committed, the resolved graph includes
  `flate2 1.1.x`, which does not build on 1.69. 1.83 is the toolchain the
  `lint` CI job now compiles the whole crate with, so the floor is enforced
  rather than asserted. This affects source builds only — the published wheels
  are `abi3` and need no Rust toolchain.
- **CI now enforces what it claimed to.** The lint job runs
  `ruff format --check` (never run before, 42 files were drifting), includes
  `benchmarks/` in its scope (70 errors were invisible), and finally invokes
  the clippy that was being installed and discarded. The test job runs the full
  `cargo test` rather than `--lib`, asserts `native_available()` instead of
  letting a broken extension build silently skip every Rust cell, and installs
  the `all` extra so the `format="df"` / `format="polars"` paths are exercised
  instead of skipped.
- Ruff configuration gained a `[tool.ruff.format]` section and per-file `E402`
  ignores for the `bench/` and `benchmarks/` entry-point scripts, which must
  bootstrap `sys.path` before importing. The whole tree is now
  `ruff format` clean.
- `UP006`/`UP007`/`UP035`/`UP045` are ignored for this release. Every module
  carries `from __future__ import annotations`, so ruff proposes PEP 585/604
  rewrites regardless of `target-version`; applying them wholesale is not safe
  while 3.9 is supported. They are re-enabled in 0.2.0 when the floor moves
  to 3.10.

### Added

- `python/gffbase/py.typed`. The `Typing :: Typed` classifier was declared but
  no PEP 561 marker shipped, so downstream type checkers saw nothing.
- `CODE_OF_CONDUCT.md` — referenced by `CONTRIBUTING.md` but missing.
- `CITATION.cff` — referenced by `README.md` but missing.
- `SECURITY.md` and this `CHANGELOG.md`.
- `pandas`, `polars`, `fasta`, and `all` optional-dependency extras. The
  `format="df"` and `format="polars"` code paths were advertised with no way to
  install what they need.
- `native`, `rtree`, and `slow` pytest markers.

### Removed

- Six `PHASE*.md` entries from `pyproject.toml` and `MANIFEST.in` referring to
  files deleted in `44268ce`, plus a `recursive-include python/gffbase *.pyi`
  matching no files.

### Notes

Version 0.1.0 is being yanked from PyPI: its metadata advertises Python 3.9
support that the artifact cannot deliver.

---

## [0.1.0] — 2026-05-07

Initial public release.

[Unreleased]: https://github.com/Kuanhao-Chao/gffbase/compare/v0.1.1...HEAD
[0.1.1]: https://github.com/Kuanhao-Chao/gffbase/compare/v0.1.0...v0.1.1
[0.1.0]: https://github.com/Kuanhao-Chao/gffbase/releases/tag/v0.1.0
