# Changelog

All notable changes to GFFBase are documented here.

The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and
this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

Work toward 0.2.0: genuine `gffutils` 0.14 API/CLI parity, first-class support
for discontinuous (multipart) GFF3 features, a `compat`/`strict` mode axis,
transactional storage, and a release pipeline gated on validation.

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
