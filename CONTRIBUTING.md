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
[Code of Conduct](https://github.com/Kuanhao-Chao/gffbase/blob/main/CODE_OF_CONDUCT.md).

---

## 1. Quick links

| What | Where |
|---|---|
| Bug reports | [open an issue](https://github.com/Kuanhao-Chao/gffbase/issues/new?template=bug_report.md) |
| Feature requests | [open an issue](https://github.com/Kuanhao-Chao/gffbase/issues/new?template=feature_request.md) |
| Discussions / questions | [GitHub Discussions](https://github.com/Kuanhao-Chao/gffbase/discussions) |
| Performance &amp; architecture notes | [Performance](https://khchao.com/gffbase/content/performance.html) |
| Migration from `gffutils` | [Migration guide](https://khchao.com/gffbase/content/migration.html) |
| API reference | [`docs/api/`](docs/api/) (rendered: `make -C docs html`) |

---

## 2. Prerequisites

You will need:

- **Python 3.10 – 3.14** (any one of them; the CI matrix tests 3.10 / 3.12 / 3.14)
- **Rust >= 1.83** with `cargo` on `$PATH`. Install via
  [`rustup`](https://rustup.rs/).
- **maturin ≥ 1.5** for building the Rust extension.
- **git**, **make** (optional, for convenience targets), and a working
  C toolchain (clang on macOS, gcc on Linux, MSVC on Windows).

Verify:

```bash
python --version          # 3.10-3.14
rustc --version           # >= 1.83
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
pip install -e .[dev,test]

# The docs extra is separate on purpose: Sphinx 9 requires Python >= 3.12,
# while the package floor is 3.10. On 3.10 or 3.11 the line above is
# everything you need, and `pip install -e .[dev,test,docs]` would fail to
# resolve rather than skip the docs.
pip install -e .[docs]          # Python >= 3.12 only
```

This installs:
- The runtime: `duckdb`, `pyarrow`.
- Test tools: `pytest`, `pytest-cov`, `pyyaml` (the release-hygiene suite parses the workflows).
- Lint tools: `ruff`, `mypy`.
- Doc tools: `Sphinx`, `furo`, `sphinx-design`, `sphinx-copybutton`
  (needs Python >= 3.12).
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
python -c "import gffbase; print(gffbase.__version__)"      # 0.2.0rc1
python -c "from gffbase import native_available; print(native_available())"   # True
```

If `native_available()` returns `False`, the Rust extension didn't
build — check `maturin develop` output for compilation errors.

### 3.5 Optional extras

| Extra | When you need it |
|---|---|
| `pandas` | If you want to test the `format="df"` path. |
| `polars` | If you want to test the `format="polars"` zero-copy path. |
| `fasta` | pyfaidx, for `Feature.sequence(fasta=str_path)`. |
| `all` | All three at once. |
| `gffutils>=0.13` | If you're running `benchmarks/06_mega.py` or any differential-correctness test against the legacy library. |

```bash
pip install -e '.[all]' gffutils
```

---

## 4. Running the test suite

GFFBase's correctness story rests on two things: the unit tests
(currently 530, all passing) and the differential-correctness checks against
legacy `gffutils` in the benchmark harness.

### 4.1 Full unit suite + coverage

```bash
pytest                                  # tests only
pytest --cov=gffbase --cov-report=term  # tests + coverage
```

Coverage is **not** part of the default invocation — a bare `pytest` reports on
tests, not on coverage. CI applies the gate explicitly with
`--cov-fail-under`; see `.github/workflows/ci.yml` for the enforced threshold.

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

# Stop at the first failure:
pytest -x

# Put the scratch databases somewhere with room (see below):
pytest --basetemp=/path/with/space
```

> **Disk:** a full run needs **~4 GB of temporary space**. DuckDB pre-allocates
> every database file it creates and the suite builds a lot of them;
> `tmp_path_retention_policy = "failed"` keeps only failed tests' directories,
> but nothing is reclaimed until the run ends. If `/tmp` is small, point
> `--basetemp` somewhere with room.

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
ruff check python/gffbase tests benchmarks tools
ruff format --check python/gffbase tests benchmarks tools
```

Fix automatically with:

```bash
ruff check --fix python/gffbase tests benchmarks tools
ruff format python/gffbase tests benchmarks tools
```

CI requires both to pass.

### 4.6 Documentation

```bash
make -C docs html            # build -> docs/build/html/index.html
make -C docs html SPHINXOPTS="-W --keep-going"   # what CI runs: warnings are errors
```

`--strict` is mandatory before opening a docs PR.

### 4.7 Benchmarks (optional)

Long-running — several hours for a full sweep. Don't run as part of normal
development.

```bash
pip install -e ".[bench,all]"
python benchmarks/download_corpora.py     # ~5 min, ~257 MB

# Full sweep. Keeps the GENCODE GFF3 databases for stages 01-05.
python benchmarks/06_mega.py --legacy-timeout 5400 --keep-db gencode-gff3

# Publish: copy the measurements in, then regenerate every table from them.
cp benchmarks/out/06_mega.json benchmarks/results/06_mega.json
python tools/gen_benchmark_tables.py --write
```

**Disk.** The five corpus pairs total ~38 GiB. Each is purged as soon as its
numbers are recorded, which holds the peak near 16 GiB; the harness refuses to
start a corpus it cannot finish. Set `GFFBASE_BENCH_OUT` to use another volume.

**Never hand-edit a published benchmark table.** They live between
`<!-- BEGIN GENERATED: ... -->` markers and are rendered from
`benchmarks/results/06_mega.json`.
`tools/gen_benchmark_tables.py --check` runs in the test suite and will fail.

**Never write a number that was not measured.** A run killed at the safety
valve reports `state: timed_out`, `wall_seconds: null`, and `cap_seconds`; it
renders as censored and produces no speedup or speedup floor. The generator
refuses current results that revive the old lower-bound fields or pair a
timeout with a wall. This is not a style preference — the previous harness
multiplied a timeout by two and that invented figure became a headline claim.

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
- [ ] **Coverage not regressed.** Run
      `pytest --cov=gffbase --cov-report=term` and confirm.
- [ ] **`ruff check` and `ruff format --check` clean.** No new lint warnings
      and no formatting drift.
- [ ] **Rust clean** if you touched `rust/`:
      `cargo fmt --manifest-path rust/Cargo.toml --all -- --check`,
      `cargo clippy --manifest-path rust/Cargo.toml --all-targets -- -D warnings`,
      and `cargo test --manifest-path rust/Cargo.toml`.
- [ ] **Documentation updated** if you touched a public API: the
      docstring, the migration guide, the cookbook, or the API
      reference.
- [ ] **`make -C docs html SPHINXOPTS="-W --keep-going"` clean** if you touched `docs/`.
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
- `python/gffbase/schema.py` — the 11-table DuckDB schema (plus 3 compatibility views) (one source
  of truth).
- `tests/conftest.py` — every shared fixture.
- `tests/test_coverage_gaps.py` — the targeted edge-case suite.

`CHANGELOG.md` records what changed in each release and why — useful when you
want to know *why* something works the way it does, not just what the current
code says.

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
