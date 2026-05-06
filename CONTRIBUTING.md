# Contributing to GFFBase

Thanks for your interest in contributing to GFFBase. This document is
the canonical guide for setting up a development environment, running
the test suite, and shepherding a change from idea to merged PR.

GFFBase is a hybrid Rust + Python project: a SIMD parser written in
Rust (under `rust/`) is compiled into a Python extension via PyO3 +
maturin and consumed by the Python public API (under
`python/gffbase/`). Most contributions touch one or the other; a few
touch both.

By participating in this project, you agree to abide by our
[Code of Conduct](CODE_OF_CONDUCT.md).

---

## 1. Quick links

| What | Where |
|---|---|
| Bug reports | [open an issue](https://github.com/Kuanhao-Chao/gffbase/issues/new?template=bug_report.md) |
| Feature requests | [open an issue](https://github.com/Kuanhao-Chao/gffbase/issues/new?template=feature_request.md) |
| Discussions / questions | [GitHub Discussions](https://github.com/Kuanhao-Chao/gffbase/discussions) |
| Architecture overview | [`PERFORMANCE_COMPARISON.md`](PERFORMANCE_COMPARISON.md) |
| Migration from `gffutils` | [`MIGRATION.md`](MIGRATION.md) |
| API reference | [`docs/api/`](docs/api/) (rendered: `mkdocs serve`) |

---

## 2. Prerequisites

You will need:

- **Python 3.9 – 3.13** (any one of them; CI matrix tests 3.9 / 3.11 / 3.13)
- **Rust ≥ 1.69** with `cargo` on `$PATH`. Install via
  [`rustup`](https://rustup.rs/).
- **maturin ≥ 1.5** for building the Rust extension.
- **git**, **make** (optional, for convenience targets), and a working
  C toolchain (clang on macOS, gcc on Linux, MSVC on Windows).

Verify:

```bash
python --version          # 3.9–3.13
rustc --version           # ≥ 1.69
maturin --version         # ≥ 1.5
```

---

## 3. Development setup

### 3.1 Clone and create a virtual environment

```bash
git clone https://github.com/Kuanhao-Chao/gffbase.git
cd gffbase
python -m venv .venv
source .venv/bin/activate      # Windows: .venv\Scripts\activate
```

### 3.2 Install Python dev dependencies

```bash
pip install -e .[dev,test,docs]
```

This installs:
- The runtime: `duckdb`, `pyarrow`.
- Test tools: `pytest`, `pytest-cov`, `hypothesis`.
- Lint tools: `ruff`, `mypy`.
- Doc tools: `mkdocs`, `mkdocs-material`, `mkdocstrings[python]`.
- Build tools: `maturin`.

### 3.3 Build the Rust extension in-place

```bash
maturin develop --release
```

`maturin develop` compiles `rust/src/lib.rs` into
`gffbase._native` and drops the resulting shared library into your
`python/gffbase/` source tree, so `import gffbase` resolves
immediately. Use `--release` for benchmarking; omit for a faster
debug-build cycle.

Rebuild after **any** change under `rust/`:

```bash
maturin develop --release
```

### 3.4 Verify your install

```bash
python -c "import gffbase; print(gffbase.__version__)"      # 0.1.0
python -c "from gffbase import native_available; print(native_available())"   # True
```

If `native_available()` returns `False`, the Rust extension didn't
build — check `maturin develop` output for compilation errors.

### 3.5 Optional extras

| Extra | When you need it |
|---|---|
| `polars` | If you want to test the `format="polars"` zero-copy path. |
| `pyfaidx` | If you want to test `Feature.sequence(fasta=str_path)`. |
| `gffutils>=0.13` | If you're running `benchmarks/06_mega.py` or any differential-correctness test against the legacy library. |

```bash
pip install polars pyfaidx gffutils
```

---

## 4. Running the test suite

GFFBase's correctness story rests on two things: the unit tests
(currently 523 passing at ≥ 99 % branch coverage) and the
differential-correctness checks against legacy `gffutils` in the
benchmark harness.

### 4.1 Full unit suite + coverage

```bash
pytest
```

This runs the complete suite and enforces the coverage gate
(`--cov-fail-under=99`, configured in `pyproject.toml`). Any drop
below 99 % fails CI.

The default invocation also writes:
- `coverage.xml` — for codecov / Codacy / your CI's coverage parser.
- `htmlcov/` — open `htmlcov/index.html` in a browser for an
  interactive missing-line view.

### 4.2 Faster iteration during development

```bash
# Run a single file:
pytest tests/test_featuredb_basic.py -q

# Run a single test:
pytest tests/test_featuredb_basic.py::test_seqids_iter -q

# Skip coverage (faster — useful for tight TDD loops):
pytest --no-cov

# Stop at the first failure:
pytest -x
```

### 4.3 Targeted coverage check

```bash
pytest --cov=gffbase --cov-report=term-missing tests/test_ingest_basic.py
```

The `term-missing` report prints uncovered lines per module. Use it
when you've added new code in a specific module to make sure your
tests exercise every branch.

### 4.4 Test the B-tree fallback path

GFFBase auto-picks an R-tree (when DuckDB's spatial extension loads)
or a multi-column B-tree (everywhere else). The CI matrix tests both:

```bash
GFFBASE_TEST_DISABLE_RTREE=1 pytest
```

This forces the B-tree path for every test. Both paths must pass.

### 4.5 Linting

```bash
ruff check python/ tests/ benchmarks/ bench/
ruff format --check python/ tests/ benchmarks/ bench/
```

Fix automatically with:

```bash
ruff check --fix python/ tests/ benchmarks/ bench/
ruff format python/ tests/ benchmarks/ bench/
```

CI requires both to pass.

### 4.6 Documentation

```bash
mkdocs serve              # http://localhost:8000 — live-reloads on edits
mkdocs build --strict     # what CI runs — fails on any broken anchor
```

`--strict` is mandatory before opening a docs PR.

### 4.7 Benchmarks (optional)

Long-running. Don't run as part of normal development.

```bash
pip install -e .[bench]
python benchmarks/download_corpora.py     # ~3 min, ~113 MB
python benchmarks/06_mega.py --legacy-timeout 900
```

---

## 5. The pull-request process

### 5.1 Before you start

- **Open an issue first** for any non-trivial change. A 50-line
  cleanup PR is welcome out of the blue; a new public-API surface or
  schema change should be discussed in an issue before code is
  written.
- **Search existing issues** to avoid duplicates.
- **Pick the right scope.** One PR = one logical change. A
  refactor + a feature = two PRs.

### 5.2 Branch naming

```
feat/<short-description>     # new feature
fix/<issue-number>-<gist>    # bug fix
docs/<area>                  # docs-only change
chore/<topic>                # tooling, deps, CI
perf/<area>                  # measurable perf change
```

### 5.3 Commit messages

We use [Conventional Commits](https://www.conventionalcommits.org/)
loosely. The first line is what shows up in `git log --oneline` and
the GitHub release notes — make it count.

```
feat(parser): support GFF3 directives spanning multiple lines
fix(ingest): handle RefSeq duplicate-ID rows on Windows path separators
docs(cookbook): add MANE.select example
chore(deps): bump duckdb to 1.6.0
perf(rtree): build index inline during Arrow batch insert
```

For multi-paragraph rationale, use the body of the commit — explain
**why**, not what (the diff already says what).

### 5.4 What every PR must include

- [ ] **Tests.** New behaviour: a new test. Bug fix: a regression
      test that fails on `main` and passes on your branch.
- [ ] **Coverage held at ≥ 99 %.** Run `pytest` locally and confirm.
- [ ] **`ruff check` clean.** No new lint warnings.
- [ ] **Documentation updated** if you touched a public API: the
      docstring, the migration guide, the cookbook, or the API
      reference.
- [ ] **`mkdocs build --strict` clean** if you touched `docs/`.
- [ ] **No unrelated changes.** A 200-line diff in a feature PR
      should not include a `ruff` reformat of an unrelated file.
- [ ] **Apache-2.0 header** on any new `.py` or `.rs` source file
      (copy from any existing file — same block).

The PR template that opens automatically when you click *New PR*
encodes all of the above as a checklist.

### 5.5 Reviews

- Two-eyes rule: at least one maintainer approval before merge.
- Maintainers may request changes; please don't take it personally —
  GFFBase is the data backbone for ML pipelines that train on
  whole-human-genome corpora, so the bar for correctness is high.
- We squash-merge by default, so make your PR commit history clean
  before requesting review.

### 5.6 After merge

- Your contribution is now under the Apache 2.0 license.
- We will mention your handle in the release notes.

---

## 6. Architecture pointers for new contributors

If you're trying to find your way around:

- `rust/src/parser.rs` — the SIMD line/tab splitter and the canonical
  11-tuple shape passed to Python.
- `python/gffbase/parser.py` — the dispatcher that picks Rust vs.
  pure-Python fallback.
- `python/gffbase/ingest.py` — the DuckDB ingestion engine
  (`_ArrowBatchBuilder`, `from_file`, GTF synthesis, R-tree finalize).
- `python/gffbase/interface.py` — `FeatureDB` and the smart
  R-tree/B-tree + closure/dynamic-CTE query routers.
- `python/gffbase/schema.py` — the 7-table DuckDB schema (one source
  of truth).
- `tests/conftest.py` — every shared fixture.
- `tests/test_coverage_gaps.py` — the targeted edge-case suite that
  guards the 99 % gate.

The `PHASE*_*_SUMMARY.md` files in `plans/` document the design
decisions phase-by-phase — useful when you want to know *why*
something works the way it does, not just what the current code says.

---

## 7. Things we will *not* accept

- Changes that drop the coverage gate.
- Performance "wins" that aren't backed by a benchmark in
  `benchmarks/` showing the before/after delta.
- New external runtime dependencies without an issue discussing
  necessity vs. an inline implementation.
- Breaking changes to the public API without a migration note in
  `MIGRATION.md` and a deprecation cycle of at least one minor
  release.
- Vendored copies of upstream code without a clear license-compatible
  attribution.

---

## 8. Questions?

Open a [GitHub Discussion](https://github.com/Kuanhao-Chao/gffbase/discussions)
or ping the maintainer at `kuanhao.chao@gmail.com`. Issues are for
bugs and feature requests; discussions are for "how do I…" and "is
this the right approach for…".

Thanks for helping make GFFBase better.
