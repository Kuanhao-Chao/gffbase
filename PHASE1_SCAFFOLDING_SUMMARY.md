# Phase 1 — Scaffolding & Day-Zero Git Push

**Repository:** `/Users/chaokuan-hao/Documents/Projects/gff/gffbase`
**Branch:** `main`
**Initial commit:** `ccab9b6` — *chore: initial project scaffolding for GFFBase* (55 files)

---

## 1. Directory Structure

```
gffbase/
├── .github/
│   └── workflows/
│       ├── ci.yml                      # 14-cell matrix: 3 OS × 3 Py × {rtree,btree}
│       └── release.yml                 # PyO3/maturin-action wheels → PyPI (OIDC)
├── .gitignore                          # Python, Rust, DuckDB, raw genomics
├── LICENSE                             # MIT
├── MANIFEST.in                         # sdist include rules (legacy tooling)
├── README.md                           # Project overview + quickstart
├── pyproject.toml                      # maturin build backend, PyPI metadata
│
├── rust/                               # Rust extension crate (PyO3)
│   ├── Cargo.toml                      # gffbase-core 0.1.0, rust-version 1.71
│   └── src/
│       ├── lib.rs                      # PyO3 module entry — gffbase._native
│       ├── parser.rs                   # streaming GFF3/GTF parser
│       ├── attributes.rs               # column-9 state machine
│       ├── dialect.rs                  # dialect detection
│       └── escape.rs                   # GFF3 percent-encoding
│
├── python/
│   └── gffbase/                        # Python package (maturin python-source)
│       ├── __init__.py                 # public surface
│       ├── feature.py                  # Feature + ParsedFeature
│       ├── interface.py                # FeatureDB
│       ├── ingest.py                   # Rust→Arrow→DuckDB pipeline
│       ├── schema.py                   # DDL + set-based SQL constants
│       ├── parser.py                   # native/python dispatcher
│       ├── create_db.py                # legacy create_db() shim
│       ├── iterators.py                # DataIterator
│       ├── gffwriter.py                # GFFWriter
│       ├── merge_criteria.py
│       ├── helpers.py
│       ├── sqlite_export.py
│       ├── exceptions.py
│       ├── dialect.py
│       ├── _bins.py                    # UCSC binning (legacy SQLite export)
│       └── _pyfallback/                # pure-Python correctness oracle
│           ├── __init__.py
│           ├── parser.py
│           └── attributes.py
│
├── tests/                              # pytest suite (77 tests)
│   ├── __init__.py
│   ├── conftest.py
│   ├── data/                           # curated GFF3/GTF fixtures (re-included
│   │   ├── simple.gff3                 #  via .gitignore !tests/data/*.gff3)
│   │   ├── simple.gtf
│   │   ├── hierarchy.gff3
│   │   └── synthesize.gtf
│   ├── test_parser_basics.py
│   ├── test_engine_equivalence.py
│   ├── test_ingest_basic.py
│   ├── test_featuredb_basic.py
│   ├── test_featuredb_region.py
│   ├── test_featuredb_hierarchy.py
│   ├── test_featuredb_invariants.py
│   ├── test_featuredb_execute.py
│   └── test_featuredb_export.py
│
├── bench/                              # benchmark harness (output ignored)
│   ├── benchmark.py
│   ├── relational_quick.py
│   └── run_diff_only.py
│
└── docs/                               # placeholder for future Sphinx tree
```

## 2. Configuration Files

| File | Highlights |
|---|---|
| `pyproject.toml` | `[build-system] maturin>=1.5,<2.0`; `name="gffbase"`, `version=0.1.0`; runtime deps `duckdb>=1.0`, `pyarrow>=14`; `[tool.maturin]` produces `gffbase._native` as abi3 cdylib (one wheel per arch covers Py3.9–3.13). |
| `rust/Cargo.toml` | crate `gffbase-core 0.1.0`, `rust-version="1.71"`, deps `pyo3 0.22`, `memchr 2.7`, `flate2 1.0`, `memmap2 0.9`, release profile `opt-level=3 lto=thin codegen-units=1 strip=true`. |
| `.gitignore` | Ignores `__pycache__/`, `target/`, `*.so/.dylib/.dll`, `dist/`, `build/`, `.venv/`, `Cargo.lock`, **DuckDB artifacts** (`*.duckdb`, `*.duckdb.wal`, `*.sqlite`, `*.db`), **raw genomics** (`*.fa`, `*.bam`, `*.vcf`, `*.gff*`, `*.gtf*`), `bench/data/`, `bench/out/`. Test fixtures explicitly re-included via `!tests/data/*.gff3` / `!tests/data/*.gtf`. |
| `LICENSE` | MIT (referenced from `pyproject.toml::license-files`). |
| `README.md` | Project blurb, build instructions (`maturin develop --release`), quickstart snippet. |
| `MANIFEST.in` | Include rules for `python -m build --sdist` fallback path. |

## 3. CI / Release Wired In

- `.github/workflows/ci.yml` — runs lint + 14-cell matrix on every push/PR. Both **R-tree** and **B-tree fallback** routing paths are exercised on every OS × Python combination via the `GFFBASE_TEST_DISABLE_RTREE` env-var kill-switch read by `python/gffbase/ingest.py::_build_rtree`.
- `.github/workflows/release.yml` — on `git tag v*` push, builds wheels via `PyO3/maturin-action@v1` for Linux x86_64+aarch64, macOS x86_64+aarch64, Windows x86_64; sdist; publishes to PyPI via `pypa/gh-action-pypi-publish@release/v1` using **OIDC trusted publisher** (no API tokens in repo secrets).

## 4. Day-Zero Commit

```
$ git log --oneline
ccab9b6 (HEAD -> main) chore: initial project scaffolding for GFFBase

$ git status
On branch main
nothing to commit, working tree clean
```

55 files committed. Branch is `main` (set explicitly via `git init -b main`). The tree includes both the engine code (Phases 3–7) and the CI/CD machinery (Phase 8) so the very first push lights up GitHub Actions immediately.

---

## 5. Handoff — Push Commands

Run these in your terminal (replace `your-org/gffbase.git` with your actual GitHub repo URL):

```bash
cd /Users/chaokuan-hao/Documents/Projects/gff/gffbase

# 1. Add the GitHub remote (HTTPS)
git remote add origin https://github.com/your-org/gffbase.git

# …or SSH (preferred if your key is set up):
# git remote add origin git@github.com:your-org/gffbase.git

# 2. Push the main branch and set upstream tracking
git push -u origin main
```

If the GitHub repository doesn't exist yet, create it via the `gh` CLI in one shot:

```bash
gh repo create your-org/gffbase \
    --public \
    --source=. \
    --remote=origin \
    --description="GFFBase — Rust+DuckDB GFF3/GTF engine" \
    --push
```

After the push:
- The `.github/workflows/ci.yml` matrix will run automatically. Expect ~5–8 min for first run (cold Cargo cache); subsequent runs ~1–2 min thanks to `Swatinem/rust-cache@v2`.
- To cut a release: `git tag v0.1.0 && git push --tags` triggers `release.yml`, which builds wheels on 5 platforms and publishes to PyPI (after you configure the trusted publisher on PyPI's web UI per `PHASE8_PACKAGING_SUMMARY.md` §5).

---

**Stopping here. Awaiting your `git push` before we begin the Rust/DuckDB implementation phases.**
