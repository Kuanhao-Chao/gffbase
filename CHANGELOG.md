# Changelog

All notable changes to GFFBase are documented here.

The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and
this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

Work toward 0.2.0: genuine `gffutils` 0.14 API/CLI parity, first-class support
for discontinuous (multipart) GFF3 features, a `compat`/`strict` mode axis,
transactional storage, and a release pipeline gated on validation.

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

### Added

- `mypy` runs clean over `python/gffbase` and is a CI gate, backing the
  `Typing :: Typed` classifier that 0.1.1 made honest by shipping `py.typed`.
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

### Known gaps recorded by the new harness

Not yet fixed, but now measured and pinned rather than unknown:

- **gffbase rejects 6 of the 23 fixtures the oracle ingests**, including
  `FBgn0031208.gff`, the canonical gffutils fixture. The parser validates to
  the NCBI GFF3 spec unconditionally, but `create_db()` is the drop-in entry
  point and real files violate that spec routinely. Four rules are too strict
  for the compatibility path: `InvalidPhase` (a CDS row with `.` phase, which
  FlyBase and WormBase both emit), `TooFewFields` (space-delimited GFF),
  `InvalidCoordinate` (`end < start` -- which is exactly what the sanitize
  tooling exists to repair, so rejecting it makes sanitize impossible), and
  `InvalidAttribute` (GFF2 `key value` attributes with no `=`).
- A `ID=` with an empty value yields an empty-string primary key where the
  oracle autoincrements (`protein_1`).
- GTF identity: `gencode-v19.gtf` yields 26 features against the oracle's 21.
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
