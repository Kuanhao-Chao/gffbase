# ---------------------------------------------------------------------------
# Author: Kuan-Hao Chao <kuanhao.chao@gmail.com>
# Copyright 2026 Kuan-Hao Chao
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
# ---------------------------------------------------------------------------
"""Regression tests for packaging and release-metadata invariants.

These guard defects that shipped in 0.1.0 and were invisible to the suite
because they lived in packaging metadata rather than in library code.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

import gffbase
import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent


def _read(name: str, *, required: bool = True) -> str:
    """Read a repository file.

    `required=True` by default, and that default is the point. This used to
    skip unconditionally when a file was missing, on the theory that the suite
    might be running against an installed package rather than a checkout. The
    MkDocs-to-Sphinx migration deleted every path four of these guards pointed
    at, and they all went green by skipping -- which is exactly how
    `docs/source/index.rst` came to claim version `0.2.0` while every other
    page said `0.2.0rc1`.

    A guard that cannot fail is not a guard. Pass `required=False` only where
    absence is genuinely expected.
    """
    path = REPO_ROOT / name
    if not path.is_file():
        if required:
            raise AssertionError(
                f"{name} does not exist. If it moved, repoint this check at its "
                f"successor; if it is genuinely optional here, pass required=False."
            )
        pytest.skip(f"{name} not present (running against an installed package, not a checkout)")
    return path.read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# The 0.1.0 showstopper: `@dataclass(slots=True)` is Python 3.10+, but the
# package declared `requires-python >=3.9` and shipped an abi3-py39 wheel.
# Every 3.9 install succeeded and then raised TypeError on first import.
# ---------------------------------------------------------------------------


def test_parsed_feature_is_constructible_on_this_interpreter():
    """`ParsedFeature` must be usable on every interpreter we claim to support.

    On 3.9 this fails at *import* time if `slots=True` is passed
    unconditionally, so merely importing gffbase already proves most of it;
    constructing one proves the dataclass is actually well-formed.
    """
    pf = gffbase.ParsedFeature(
        seqid="chr1",
        source="src",
        featuretype="exon",
        start=1,
        end=10,
        score=".",
        strand="+",
        frame=".",
        attributes_blob=b"ID=x",
    )
    assert pf.seqid == "chr1"
    assert pf.chrom == "chr1"
    assert pf.stop == 10


def test_parsed_feature_uses_slots_where_available():
    """3.10+ keeps the per-record memory saving; 3.9 does not get it.

    `ParsedFeature` is instantiated once per parsed line, so losing `__slots__`
    on the interpreters that support it would be a real regression.
    """
    has_slots = hasattr(gffbase.ParsedFeature, "__slots__")
    assert has_slots == (sys.version_info >= (3, 10))


# ---------------------------------------------------------------------------
# gffwriter referenced an undefined `io` in an annotation (ruff F821). With
# `from __future__ import annotations` this never raised at runtime, so no
# test caught it -- but it broke every type checker and the annotation was
# simply wrong.
# ---------------------------------------------------------------------------


def test_gffwriter_annotations_resolve():
    """Every name in GFFWriter.__init__'s annotations must actually exist."""
    import typing

    from gffbase.gffwriter import GFFWriter

    hints = typing.get_type_hints(GFFWriter.__init__)
    assert "out" in hints


# ---------------------------------------------------------------------------
# Version consistency. 0.1.0 carried the version in three places with no gate,
# so they could drift silently -- and a release tag could disagree with all of
# them.
# ---------------------------------------------------------------------------


def test_version_is_consistent_across_all_declarations():
    pyproject = _read("pyproject.toml")
    cargo = _read("rust/Cargo.toml")

    py_version = re.search(r'^version = "([^"]+)"', pyproject, re.MULTILINE)
    cargo_version = re.search(r'^version = "([^"]+)"', cargo, re.MULTILINE)

    assert py_version is not None, "pyproject.toml has no top-level version"
    assert cargo_version is not None, "rust/Cargo.toml has no package version"

    assert gffbase.__version__ == py_version.group(1), (
        f"gffbase.__version__ ({gffbase.__version__}) != pyproject version ({py_version.group(1)})"
    )
    cargo_public = cargo_version.group(1).replace("-rc.", "rc")
    assert gffbase.__version__ == cargo_public, (
        f"gffbase.__version__ ({gffbase.__version__}) != normalized "
        f"rust/Cargo.toml version ({cargo_public})"
    )


def test_changelog_documents_the_current_version():
    changelog = _read("CHANGELOG.md")
    assert f"[{gffbase.__version__}]" in changelog, (
        f"CHANGELOG.md has no section for {gffbase.__version__}"
    )


#: Every other place the version is written down. Three of these drifted to a
#: stale value precisely because only `pyproject.toml`, `rust/Cargo.toml` and
#: `__init__.py` were gated -- so the gate was extended to the full set rather
#: than the set that happened to be easy.
_VERSION_LITERALS = [
    ("CITATION.cff", r"^version: (.+)$"),
    ("README.md", r"^  version = \{([^}]+)\},$"),
    ("CONTRIBUTING.md", r"gffbase\.__version__\)\"\s+# (\S+)"),
    # Repointed at the Sphinx successors. The MkDocs paths these used to name
    # were deleted in the migration, and `_read` skipped rather than failed --
    # so these four checks silently stopped running.
    ("docs/source/content/citation.rst", r"\(Version ([^)]+)\)"),
    ("docs/source/index.rst", r"describes the unreleased ([0-9A-Za-z.]+)\s+candidate"),
    (
        "docs/source/content/installation.rst",
        r"\*\*([0-9A-Za-z.]+) is still a release candidate\*\*",
    ),
    ("docs/release_checklist.rst", r"native extension report exactly ``([^`]+)``"),
]


@pytest.mark.parametrize(("filename", "pattern"), sorted(_VERSION_LITERALS))
def test_secondary_version_literals_agree(filename, pattern):
    """The citation block, CITATION.cff and CONTRIBUTING all state a version.

    None of them was checked before, and all three drifted. A version literal
    nobody tests is a version literal that will be wrong at the next release --
    the citation metadata especially, since it is what other people's papers
    end up quoting.
    """
    text = _read(filename)
    found = re.search(pattern, text, re.MULTILINE)
    assert found is not None, f"{filename}: no version literal matched {pattern!r}"
    assert found.group(1).strip() == gffbase.__version__, (
        f"{filename} says {found.group(1).strip()!r}, "
        f"gffbase.__version__ is {gffbase.__version__!r}"
    )


def test_candidate_version_is_the_canonical_pep440_rc():
    """The candidate tag spelling is intentional and must stay canonical."""
    from packaging.version import Version

    version = Version(gffbase.__version__)
    assert str(version) == gffbase.__version__ == "0.2.0rc1"
    assert version.is_prerelease


def test_every_advertised_extra_actually_exists():
    """An install hint must name an extra that installs something.

    `pybedtools_integration`, `biopython_integration` and `contrib.plotting`
    each told the reader to run `pip install gffbase[...]` for an extra that
    was never declared -- pip prints a warning, installs nothing, and the
    import fails again. An error message that sends someone somewhere useless
    is worse than one that just says "not installed".
    """
    pyproject = _read("pyproject.toml")
    block = re.search(r"\[project\.optional-dependencies\](.*?)^\[", pyproject, re.S | re.M)
    assert block is not None, "no [project.optional-dependencies] table"
    declared = set(re.findall(r"^([a-z][\w-]*) = \[", block.group(1), re.MULTILINE))

    advertised = set()
    for path in (REPO_ROOT / "python" / "gffbase").rglob("*.py"):
        advertised.update(re.findall(r"pip install gffbase\[([\w,-]+)\]", path.read_text()))
    # A hint may name several at once, e.g. `gffbase[a,b]`.
    advertised = {name for group in advertised for name in group.split(",")}

    missing = sorted(advertised - declared)
    assert not missing, (
        f"source files advertise extras that do not exist: {missing}\ndeclared: {sorted(declared)}"
    )


# ---------------------------------------------------------------------------
# Packaging manifests must not reference files that do not exist. 0.1.0 listed
# six deleted PHASE*.md files in two manifests and a Cargo.lock that was
# gitignored.
# ---------------------------------------------------------------------------


def test_py_typed_marker_ships_with_the_package():
    """The `Typing :: Typed` classifier is a promise; PEP 561 needs the marker."""
    marker = Path(gffbase.__file__).parent / "py.typed"
    assert marker.is_file(), "py.typed missing but 'Typing :: Typed' is declared"


def test_maturin_sdist_includes_resolve_to_real_files():
    pyproject = _read("pyproject.toml")
    block = re.search(r"include = \[(.*?)\]", pyproject, re.DOTALL)
    assert block is not None
    for path in re.findall(r'path = "([^"]+)"', block.group(1)):
        # Glob patterns are checked by expansion; literal paths must exist.
        if any(ch in path for ch in "*?["):
            assert list(REPO_ROOT.glob(path)), f"sdist include pattern matches nothing: {path}"
        else:
            assert (REPO_ROOT / path).exists(), f"sdist include references missing file: {path}"


def test_manifest_in_references_resolve_to_real_files():
    manifest = _read("MANIFEST.in")
    for line in manifest.splitlines():
        line = line.strip()
        if not line.startswith("include "):
            continue
        path = line.split(None, 1)[1].strip()
        if any(ch in path for ch in "*?["):
            continue
        assert (REPO_ROOT / path).exists(), f"MANIFEST.in references missing file: {path}"


def test_referenced_community_health_files_exist():
    """CONTRIBUTING.md and README.md link these; broken links shipped in 0.1.0."""
    for name in ("CODE_OF_CONDUCT.md", "SECURITY.md", "CHANGELOG.md", "CITATION.cff"):
        assert (REPO_ROOT / name).is_file(), f"{name} is referenced but missing"


def test_every_in_repo_path_named_in_prose_or_source_actually_exists():
    """A repo-relative path cited as a pointer must resolve.

    `docs/security/2026-sql-injection.md` was cited from `CHANGELOG.md`,
    `SECURITY.md`, `security.rst`, `interface.py`, `ingest.py` and
    `deviations.toml` -- six places -- for an entire release cycle after the
    file moved. Nothing noticed, because a path in prose is just a string:
    Sphinx does not resolve it, the linters do not read it, and the security
    policy pointing at a missing advisory is exactly the pointer a reader
    follows first.

    Scoped to `docs/`, `tests/` and `benchmarks/` prefixes, which are the ones
    used as pointers. A bare filename is not matched -- too many false
    positives from example output and log excerpts.

    The changelog is exempt, deliberately. It is a historical record: an entry
    saying "`docs/cookbooks/index.md` claimed every snippet had been validated"
    is a true statement about a file that existed under that name at the time,
    and repointing it at the successor would make the record false. A pointer
    written in the present tense there still has to resolve, but no rule can
    tell the two apart, so the changelog is checked by review instead.
    """
    referenced = re.compile(r"\b((?:docs|tests|benchmarks)/[\w./-]*\.\w{1,5})\b")
    sources = [
        REPO_ROOT / "README.md",
        REPO_ROOT / "SECURITY.md",
        REPO_ROOT / "CONTRIBUTING.md",
        REPO_ROOT / "tests" / "parity" / "deviations.toml",
        *sorted((REPO_ROOT / "docs" / "source").rglob("*.rst")),
        *sorted((REPO_ROOT / "python" / "gffbase").rglob("*.py")),
    ]
    # `changelog.rst` is the changelog, published; same exemption.
    sources = [s for s in sources if s.name != "changelog.rst"]

    missing = []
    for source in sources:
        if not source.is_file():
            continue
        for path in set(referenced.findall(source.read_text(encoding="utf-8"))):
            # A glob or a placeholder is not a claim that a file is there.
            if any(ch in path for ch in "*?<>{}"):
                continue
            # `benchmarks/out/` holds generated results, produced by running a
            # benchmark and not committed. Naming one is an instruction, not a
            # claim that it is present in a fresh clone.
            if path.startswith("benchmarks/out/"):
                continue
            if not (REPO_ROOT / path).exists():
                missing.append(f"{source.relative_to(REPO_ROOT)} -> {path}")

    assert not missing, "referenced paths that do not exist:\n" + "\n".join(sorted(missing))


def test_cargo_lock_is_committed():
    """Without a lock file the published wheel's dependency graph varies by
    build host, which makes the declared MSRV unverifiable."""
    assert (REPO_ROOT / "rust" / "Cargo.lock").is_file()


# ---------------------------------------------------------------------------
# Null-coordinate handling. A GFF row may legally carry `.` in columns 4/5.
# Several code paths did unguarded arithmetic on those, producing
# `TypeError: unsupported operand type(s) for -: 'NoneType' and 'int'`
# instead of anything a caller could act on.
# ---------------------------------------------------------------------------


def test_sequence_on_null_coordinate_feature_raises_actionably():
    from gffbase import Feature

    f = Feature(seqid="chr1", source="s", featuretype="gene", start=None, end=None)
    with pytest.raises(ValueError, match="no start/end coordinates"):
        f.sequence({})


def test_bed12_on_null_coordinate_feature_raises_actionably():
    """The guard itself, exercised on a Feature that really has null coords."""
    from gffbase import Feature, FeatureDB

    f = Feature(seqid="chr1", source="rs", featuretype="gene", start=None, end=None, id="g_null")
    with pytest.raises(ValueError, match="no start/end coordinates"):
        FeatureDB.bed12(_NoChildrenDB(), f)


class _NoChildrenDB:
    """Minimal stand-in so `bed12`'s coordinate guard can be reached without
    building a database -- ingestion currently cannot store a null coordinate
    (see the xfail below)."""

    def children(self, *_args, **_kwargs):
        return iter(())


def test_null_coordinates_survive_a_database_round_trip(tmp_path):
    """A `.` in column 4/5 must survive ingest, reopen and re-serialization.

    It previously could not: the Arrow builder coerced a missing coordinate to
    0 because the `features` DDL declared start/end NOT NULL, so the feature
    reopened as 0..0 and wrote zeros where the source said `.`.
    """
    from gffbase import create_db

    src = tmp_path / "nullcoord.gff3"
    src.write_text(
        "##gff-version 3\nchr1\trs\tgene\t.\t.\t.\t+\t.\tID=g_null\n"
        "chr1\trs\tgene\t1\t100\t.\t+\t.\tID=g_ok\n"
    )
    db = create_db(str(src), ":memory:")
    assert db["g_null"].start is None
    assert db["g_null"].end is None


def test_query_with_idless_feature_reports_the_mistake(tmp_path):
    """A hand-built Feature has `id is None`. Passing one to a query used to
    bind NULL and silently match nothing."""
    from gffbase import Feature, create_db

    src = tmp_path / "small.gff3"
    src.write_text("##gff-version 3\nchr1\trs\tgene\t1\t100\t.\t+\t.\tID=g1\n")
    db = create_db(str(src), ":memory:")

    orphan = Feature(seqid="chr1", source="rs", featuretype="gene", start=1, end=100)
    assert orphan.id is None
    with pytest.raises(ValueError, match="no database id"):
        db[orphan]
    with pytest.raises(ValueError, match="no database id"):
        list(db.children(orphan))


# ---------------------------------------------------------------------------
# `_dbutil` exists because `con.execute(...).fetchone()[0]` raises
# `TypeError: 'NoneType' object is not subscriptable` on an empty result,
# which tells a caller nothing. These are the branches that replace it.
# ---------------------------------------------------------------------------


def test_scalar_reports_an_empty_result_instead_of_subscripting_none():
    import duckdb
    from gffbase._dbutil import scalar

    con = duckdb.connect(":memory:")
    con.execute("CREATE TABLE t (x INTEGER)")
    assert scalar(con, "SELECT COUNT(*) FROM t") == 0
    with pytest.raises(duckdb.Error, match="no rows where exactly one was expected"):
        scalar(con, "SELECT x FROM t")


def test_scalar_or_returns_the_default_for_no_row_and_for_a_null_row():
    """`MAX(...)` over an empty table yields one NULL row, not zero rows --
    the caller wants the default either way."""
    import duckdb
    from gffbase._dbutil import scalar_or

    con = duckdb.connect(":memory:")
    con.execute("CREATE TABLE t (x INTEGER)")
    assert scalar_or(con, "SELECT x FROM t", "fallback") == "fallback"
    assert scalar_or(con, "SELECT MAX(x) FROM t", "fallback") == "fallback"
    con.execute("INSERT INTO t VALUES (7)")
    assert scalar_or(con, "SELECT MAX(x) FROM t", "fallback") == 7


# ---------------------------------------------------------------------------
# Documentation that states a number must state the right one.
# ---------------------------------------------------------------------------
#
# Every count in the docs was wrong before 0.2.0: "530 passed" against 1680
# tests, "7-table schema" against 11 tables. Prose goes stale silently; a
# number can be checked.


def test_the_documented_table_count_is_right():
    import re

    schema = _read("python/gffbase/schema.py")
    tables = len(re.findall(r"CREATE TABLE(?: IF NOT EXISTS)? (\w+)", schema))
    views = len(re.findall(r"CREATE (?:OR REPLACE )?VIEW(?: IF NOT EXISTS)? (\w+)", schema))
    readme = _read("README.md")
    assert f"{tables}-table schema" in readme, (
        f"schema.py defines {tables} tables; README says otherwise"
    )
    assert f"{views} compatibility" in readme, f"schema.py defines {views} views"


def test_the_two_changelogs_carry_the_same_entries():
    """`CHANGELOG.md` and `docs/source/content/changelog.rst` are the same
    document in two markups, and nothing kept them together.

    They are maintained by hand, not generated, so an entry added to one is
    simply absent from the other -- which is what happened to four breaking
    `order_by` / `validation` changes: written into the Markdown, missing from
    the page users actually read.

    Compared structurally rather than textually: same section names, same
    number of top-level entries in each. That is strict enough to catch a
    forgotten entry and loose enough to allow the markup to differ, which it
    must.
    """
    import re

    def markdown_sections(text: str) -> dict[str, int]:
        counts: dict[str, int] = {}
        current = None
        for line in text.split("\n"):
            if line.startswith("### "):
                current = line[4:].strip()
                counts[current] = 0
            elif line.startswith("## "):
                current = None
            elif current and line.startswith("- "):
                counts[current] += 1
        return counts

    def rst_sections(text: str) -> dict[str, int]:
        lines = text.split("\n")
        counts: dict[str, int] = {}
        current = None
        for index, line in enumerate(lines):
            underline = lines[index + 1].strip() if index + 1 < len(lines) else ""
            if line.strip() and re.fullmatch(r"~{3,}", underline):
                current = line.strip()
                counts[current] = 0
            elif line.strip() and re.fullmatch(r"-{3,}", underline):
                current = None
            elif current and line.startswith("- "):
                counts[current] += 1
        return counts

    markdown = markdown_sections(_read("CHANGELOG.md"))
    rst = rst_sections(_read("docs/source/content/changelog.rst"))

    differences = [
        f"{section}: CHANGELOG.md has {markdown.get(section)}, changelog.rst has {rst.get(section)}"
        for section in sorted(set(markdown) | set(rst))
        if markdown.get(section) != rst.get(section)
    ]
    assert not differences, "the two changelogs disagree:\n" + "\n".join(differences)


def test_the_disk_and_rss_ranges_in_prose_match_the_measurements():
    """Four pages quote a disk ratio and a legacy RSS figure in prose.

    Prose is outside every generated block, so `gen_benchmark_tables.py
    --check` cannot see it. All four had drifted: the pages said "~1.5×" where
    the committed measurements give 1.29-1.36x, and "roughly 170 MB" for
    legacy RSS, which is the *smallest* corpus (MANE, 174.50 MB) generalised
    to all of them -- GENCODE is 495.06 MB, nearly three times that.

    Recomputed here from `06_mega.json` so a refreshed campaign makes this
    fail rather than making the prose quietly wrong again.
    """
    import json

    results = REPO_ROOT / "benchmarks" / "results" / "06_mega.json"
    if not results.is_file():
        pytest.skip("benchmarks/results/06_mega.json is not present")
    corpora = json.loads(results.read_text(encoding="utf-8"))["corpora"].values()

    ratios = [c["gffbase"]["disk_bytes"] / c["legacy"]["disk_bytes"] for c in corpora]
    legacy_rss = [c["legacy"]["peak_rss_mb"] for c in corpora]
    gffbase_rss = [c["gffbase"]["peak_rss_mb"] / 1024 for c in corpora]

    ratio_claim = f"{min(ratios):.2f}× to {max(ratios):.2f}×"
    rss_claim = f"{min(legacy_rss):.0f}–{max(legacy_rss):.0f} MB"
    gb_claim = f"{min(gffbase_rss):.1f}–{max(gffbase_rss):.1f} GB"

    expected = {
        "docs/source/content/faq.rst": [ratio_claim],
        "docs/source/content/troubleshooting.rst": [ratio_claim, rss_claim, gb_claim],
        "docs/source/content/migration.rst": [ratio_claim, rss_claim, gb_claim],
    }
    wrong = []
    for rel, claims in expected.items():
        text = _read(rel)
        for claim in claims:
            if claim not in text:
                wrong.append(f"{rel}: expected to state {claim!r}")
    assert not wrong, (
        "prose disagrees with benchmarks/results/06_mega.json:\n"
        + "\n".join(wrong)
        + "\n\nRegenerate the campaign and update these sentences together."
    )


def test_the_headline_corpus_count_matches_the_corpora_actually_measured():
    """"Across the five canonical human-genome annotations" -- there were four.

    `06_mega.json` carries chess, gencode-gtf, mane and refseq; gencode-gff3 is
    in the display order but was never measured. The generated table directly
    below the sentence showed four rows while the sentence said five, and the
    README went further and said "every canonical human-genome annotation".

    The corpus *set* legitimately has five members -- `download_corpora.py`
    fetches five, and `datasets.rst` and `methodology.rst` say so correctly.
    What must match the measurements is the count in a head-to-head claim.
    """
    import json

    results = REPO_ROOT / "benchmarks" / "results" / "06_mega.json"
    if not results.is_file():
        pytest.skip("benchmarks/results/06_mega.json is not present")
    measured = len(json.loads(results.read_text(encoding="utf-8"))["corpora"])
    word = {1: "one", 2: "two", 3: "three", 4: "four", 5: "five", 6: "six"}[measured]

    claims = {
        "MIGRATION.md": f"across the {word} canonical human-genome",
        "docs/source/content/migration.rst": f"across the {word} canonical human-genome",
        "README.md": f"across {word} canonical human-genome annotations",
    }
    wrong = [rel for rel, claim in claims.items() if claim not in _read(rel)]
    assert not wrong, (
        f"{measured} corpora are measured, so these should say {word!r}: {wrong}"
    )


def test_published_benchmark_tables_match_the_committed_measurements():
    """A published number must be derivable from a checked-in measurement.

    The performance table used to be hand-transcribed into five files at once,
    with nothing comparing them to each other or to the harness output. They
    drifted: one row paired a GENCODE v49 ingest time with GENCODE *v45*
    spatial and batched figures, and the results file named as the provenance
    for all five corpora contained one.

    `tools/gen_benchmark_tables.py` renders every table from
    `benchmarks/results/06_mega.json`, and this runs its `--check` mode. If a
    table is edited by hand, or the measurements are refreshed without
    regenerating, this fails.
    """
    import subprocess

    script = REPO_ROOT / "tools" / "gen_benchmark_tables.py"
    results = REPO_ROOT / "benchmarks" / "results" / "06_mega.json"
    if not script.is_file() or not results.is_file():
        pytest.skip("benchmark results not present (running outside a full checkout)")

    proc = subprocess.run(
        [sys.executable, str(script), "--check"],
        capture_output=True,
        text=True,
        cwd=str(REPO_ROOT),
    )
    assert proc.returncode == 0, (
        "published benchmark tables are out of date.\n"
        f"{proc.stdout}\n{proc.stderr}\n"
        "Run: python tools/gen_benchmark_tables.py --write"
    )


def test_no_benchmark_corpus_row_lives_outside_a_generated_block():
    """Catch a hand-written table that the generator would never look at.

    `--check` only compares blocks it owns. A corpus row pasted somewhere
    without the marker pair would be invisible to it -- which is exactly how
    the five hand-maintained copies came to exist.
    """
    import re

    # A corpus row is a markdown table row naming one of the corpora AND
    # carrying a speedup cell, which is what makes it a results table rather
    # than prose that happens to mention GENCODE.
    row = re.compile(r"^\|.*(GENCODE|RefSeq|MANE|CHESS).*\|.*[0-9]\s*×.*\|", re.M)
    begin = re.compile(r"<!-- BEGIN GENERATED: [\w-]+ -->")
    end = re.compile(r"<!-- END GENERATED: [\w-]+ -->")

    offenders = []
    for rel in (
        "README.md",
        "MIGRATION.md",
        "docs/source/index.rst",
        "docs/source/content/performance.rst",
        "docs/source/content/migration.rst",
    ):
        path = REPO_ROOT / rel
        if not path.is_file():
            continue
        text = path.read_text(encoding="utf-8")
        # Blank out every generated block, then look for survivors.
        spans = []
        for b in begin.finditer(text):
            e = end.search(text, b.end())
            if e:
                spans.append((b.start(), e.end()))
        outside = text
        for start, stop in reversed(spans):
            outside = outside[:start] + outside[stop:]
        if row.search(outside):
            offenders.append(rel)
    assert not offenders, (
        f"hand-written benchmark rows outside a generated block in: {offenders}. "
        "Wrap them in <!-- BEGIN GENERATED: corpus-table --> markers so "
        "tools/gen_benchmark_tables.py owns them."
    )


def test_the_documentation_url_is_canonical_everywhere():
    """One documentation URL, and it is the one the site is served from.

    The site moved from the `gffbase.khchao.com` subdomain to a path under
    the user site, `https://khchao.com/gffbase/` -- which is where GitHub
    Pages serves a project repo when the user site owns the apex domain, and
    where this author's other projects already live. The old host is retired,
    so a surviving reference is a dead link rather than a redirect.

    `docs/CNAME` in particular must stay deleted: a CNAME is copied into the
    published site, and its presence is what makes GitHub redirect
    `khchao.com/gffbase/` back to the subdomain.
    """
    assert not (REPO_ROOT / "docs" / "CNAME").exists(), (
        "docs/CNAME republishes the retired subdomain and redirects the canonical URL away"
    )

    canonical = "https://khchao.com/gffbase/"
    assert f'html_baseurl = "{canonical}"' in _read("docs/source/conf.py")

    # A LINK to the retired host, not a mention of it. The changelog entry that
    # records the move necessarily names the old subdomain, and that is correct
    # prose; what must not survive is anything a reader could click.
    dead_link = re.compile(r"https?://gffbase\.khchao\.com")
    offenders = []
    for path in REPO_ROOT.rglob("*"):
        if not path.is_file() or path.suffix not in {".md", ".yml", ".yaml", ".toml", ".cff"}:
            continue
        rel = path.relative_to(REPO_ROOT)
        if rel.parts[0] in {".git", "site", "htmlcov", "plans", "benchmarks"}:
            continue
        if dead_link.search(path.read_text(encoding="utf-8", errors="replace")):
            offenders.append(str(rel))
    assert not offenders, f"links to the retired docs domain remain in: {sorted(offenders)}"


def test_every_workflow_is_free_of_duplicate_keys():
    """A duplicate mapping key stops a workflow loading at all.

    `testpypi-release.yml` declared `name:` and `runs-on:` twice in its
    `verify` job. PyYAML's `safe_load` tolerates that -- last one wins -- so
    every naive parse of the file looked fine. GitHub Actions does not: it
    rejects duplicate keys outright, so the RC dress rehearsal could never
    have run, and nothing would have found out until a tag was pushed.

    This loads each workflow with a mapping constructor that refuses a
    repeated key, which is the rule the real parser applies.
    """
    yaml = pytest.importorskip("yaml")

    class NoDuplicates(yaml.SafeLoader):
        pass

    def _no_duplicate_mapping(loader, node, deep=False):
        seen: dict = {}
        for key_node, value_node in node.value:
            key = loader.construct_object(key_node, deep=deep)
            if key in seen:
                raise AssertionError(
                    f"duplicate key {key!r} at line {key_node.start_mark.line + 1}"
                )
            seen[key] = loader.construct_object(value_node, deep=deep)
        return seen

    NoDuplicates.add_constructor(
        yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, _no_duplicate_mapping
    )

    workflows = sorted((REPO_ROOT / ".github" / "workflows").glob("*.yml"))
    if not workflows:
        pytest.skip("no workflows present (not a checkout)")

    failures = []
    for path in workflows:
        try:
            yaml.load(path.read_text(encoding="utf-8"), NoDuplicates)
        except AssertionError as exc:
            failures.append(f"{path.name}: {exc}")
    assert not failures, "GitHub Actions would refuse to load: " + "; ".join(failures)


def test_release_workflows_partition_canonical_rc_and_stable_tags():
    """An RC must reach TestPyPI and must never reach production PyPI."""
    testpypi = _read(".github/workflows/testpypi-release.yml")
    production = _read(".github/workflows/release.yml")

    assert "'v*rc*'" in testpypi
    assert "'!v*rc*'" in production
    for workflow in (testpypi, production):
        assert "workflow_dispatch" in workflow

    # The version rules themselves moved into `tools/release_policy.py`, which
    # both publishers call as their first job. Keeping the assertions pointed at
    # the workflow text would mean requiring the two files to carry duplicate
    # copies of the logic -- the arrangement that let them drift apart.
    policy = _read("tools/release_policy.py")
    assert "from packaging.version import" in policy
    assert "is not canonical PEP 440" in policy
    assert "production publication refuses prerelease source versions" in policy
    assert "--channel production" in production
    assert "--channel testpypi" in testpypi


def test_release_identity_is_checked_from_the_built_wheel():
    """Source imports cannot stand in for inspecting the artifact we upload.

    The check now lives in the reusable artifact workflow rather than in each
    publisher. That is the point of the refactor: when both publishers carried
    their own copy, PyPI and TestPyPI could verify differently, and the dress
    rehearsal stopped rehearsing the thing it existed to rehearse. Asserting it
    here against the publishers would now contradict
    `test_thin_publisher_callers_never_build_package_artifacts`, which requires
    that they build nothing at all.
    """
    workflow = _read(".github/workflows/release-artifacts.yml")
    assert 'importlib.metadata.version("gffbase")' in workflow
    assert "gffbase._native" in workflow
    assert "pip install --force-reinstall" in workflow

    for filename in (".github/workflows/release.yml", ".github/workflows/testpypi-release.yml"):
        publisher = _read(filename)
        assert "./.github/workflows/release-artifacts.yml" in publisher, (
            f"{filename} must obtain artifacts from the one workflow that verifies them"
        )


def test_release_qualification_names_every_required_gate():
    """Publishing waits for the complete candidate qualification contract."""
    qualification = _read(".github/workflows/qualification.yml")
    required_commands = [
        'pytest -m "not corpus and not parity and not slow and not property and not pandas_docs"',
        "pytest -m slow",
        "pytest -m parity",
        "pytest -m corpus",
        "cargo test --locked --release",
        "python -m build --wheel --sdist",
        'tests/test_docs_snippets.py -m "not pandas_docs"',
        "tests/test_docs_snippets.py -m pandas_docs",
        'make -C docs html SPHINXOPTS="-W --keep-going"',
    ]
    missing = [command for command in required_commands if command not in qualification]
    assert not missing, f"release qualification is missing explicit gates: {missing}"

    # Structural, not a literal string. This asserted `"needs: qualification"`
    # as raw text, which stopped matching the moment the list gained `policy` --
    # and would equally have matched a comment. What matters is that nothing
    # can reach the artifact stage without qualification having run.
    yaml = pytest.importorskip("yaml")
    for filename in (".github/workflows/release.yml", ".github/workflows/testpypi-release.yml"):
        spec = yaml.safe_load(_read(filename))
        jobs = spec["jobs"]
        assert jobs["qualification"]["uses"] == "./.github/workflows/qualification.yml"
        artifacts_needs = jobs["artifacts"]["needs"]
        if isinstance(artifacts_needs, str):
            artifacts_needs = [artifacts_needs]
        assert "qualification" in artifacts_needs, (
            f"{filename}: artifacts must not start before qualification passes"
        )


def test_the_documented_parity_percentage_is_derivable():
    """`docs/api/compat.md` quotes a parity figure; derive it, do not trust it.

    It is computable: the oracle manifest lists every symbol, and
    `deviations.toml` records the ones gffbase deliberately does not provide
    (`status = "excluded"`). Everything else is present, whether or not it
    behaves identically. A hand-maintained percentage in prose is exactly the
    claim a reader will check and a maintainer will forget.
    """
    import json

    manifest_path = REPO_ROOT / "tests" / "parity" / "gffutils_manifest.json"
    deviations_path = REPO_ROOT / "tests" / "parity" / "deviations.toml"
    # The MkDocs `docs/api/compat.md` that used to carry this figure was merged
    # into the autodoc API page during the Sphinx migration, and the figure was
    # lost with it -- leaving `migration.rst` claiming every symbol was
    # preserved. It lives on the migration page now, where a `gffutils` user
    # looks for it.
    page = REPO_ROOT / "docs" / "source" / "content" / "migration.rst"
    for path in (manifest_path, deviations_path, page):
        if not path.is_file():
            raise AssertionError(f"{path} is missing; repoint this check")

    manifest = json.loads(manifest_path.read_text())
    total = sum(
        len(mod.get("symbols", {}))
        for mod in manifest.get("modules", {}).values()
        if isinstance(mod, dict)
    )
    excluded = len(re.findall(r'status = "excluded"', deviations_path.read_text()))
    provided = total - excluded
    pct = round(100 * provided / total)

    text = page.read_text(encoding="utf-8")
    expected = f"{provided} of {total} symbols ({pct}%)"
    assert expected in text, (
        f"docs/source/content/migration.rst should say {expected!r}; "
        f"derived from the manifest ({total} symbols) minus "
        f"{excluded} excluded deviations"
    )


def test_the_documented_invariant_count_is_right():
    """The validator's size is quoted in four places; keep them honest.

    All four said 15 while the registry held 14. A count in prose is exactly
    the kind of claim that goes stale silently, so it is derived from the
    registry here rather than trusted.
    """
    from gffbase.validate import _CHECKS

    n = len(_CHECKS)
    wrong = []
    # Every published page, discovered rather than listed. The old hand list
    # named three MkDocs paths that the Sphinx migration deleted, so the check
    # quietly degraded to README-only -- and `faq.rst` drifted to 14.
    pages = [REPO_ROOT / "README.md", REPO_ROOT / "MIGRATION.md"]
    pages += sorted((REPO_ROOT / "docs" / "source").rglob("*.rst"))
    for path in pages:
        if not path.is_file():
            continue
        rel = path.relative_to(REPO_ROOT).as_posix()
        text = path.read_text(encoding="utf-8")
        # Any "<number> invariant(s)" claim must state the real number.
        for found in re.finditer(r"(\d+)\s+invariants?\b", text):
            if int(found.group(1)) != n:
                wrong.append(f"{rel}: says {found.group(1)}, registry has {n}")
    assert not wrong, "stale invariant counts: " + "; ".join(wrong)


def test_every_cli_command_is_documented():
    """A shipped command with no documentation is invisible."""
    from gffbase.cli import build_parser

    registered = set([a for a in build_parser()._actions if a.dest == "command"][0].choices)
    page = _read("docs/source/content/cli.rst")
    undocumented = sorted(c for c in registered if f"gffbase {c}" not in page)
    assert not undocumented, (
        f"CLI commands missing from docs/source/content/cli.rst: {undocumented}"
    )


def test_every_nav_entry_resolves_and_every_page_is_reachable():
    """Every toctree entry exists, and every page is in a toctree.

    The Sphinx build runs under `-W`, so it already fails on a page outside the
    toctree -- but that build needs the docs extra and a 3.12 interpreter,
    while this runs in the ordinary suite. It is the fast check that catches
    the mistake before the slow one has to.
    """
    source = REPO_ROOT / "docs" / "source"
    refs: set[str] = set()
    for rst in source.rglob("*.rst"):
        in_toctree = False
        for line in rst.read_text(encoding="utf-8").splitlines():
            if line.strip().startswith(".. toctree::"):
                in_toctree = True
                continue
            if not in_toctree:
                continue
            if not line.strip():
                continue
            # A toctree block ends at the first line that is not indented.
            if not line[:1].isspace():
                in_toctree = False
                continue
            entry = line.strip()
            if entry.startswith(":"):  # an option, not an entry
                continue
            base = source if entry.startswith("/") else rst.parent
            refs.add((base / entry.lstrip("/")).resolve().relative_to(source.resolve()).as_posix())

    missing = sorted(r for r in refs if not (source / f"{r}.rst").is_file())
    assert not missing, f"toctree references pages that do not exist: {missing}"

    excluded: set[str] = set()

    # `as_posix()`, not `str()`: on Windows the latter yields `content\api`,
    # which never matches the `content/api` in the toctree, so every nested
    # page was reported as an orphan and the test failed on Windows alone.
    on_disk = {
        p.relative_to(source).with_suffix("").as_posix()
        for p in source.rglob("*.rst")
        if p.name != "index.rst"
    }
    orphans = sorted(on_disk - refs - excluded)
    assert not orphans, (
        f"pages outside the toctree fail the -W build: {orphans}. "
        f"Add them to a toctree, or move them out of docs/source/ if they are "
        f"deliberately unpublished -- as the unsent gffutils report is."
    )


def test_readme_deep_links_resolve_to_pages_that_exist():
    """README links are absolute URLs, so the docs build cannot see them.

    Sphinx validates `:doc:` roles inside the source tree, but the README is
    rendered by GitHub and PyPI and therefore links to the published site by
    full URL. Nothing checked those, so a page rename would leave the two
    most-read documents in the project pointing at 404s -- which is exactly
    what the MkDocs-to-Sphinx move would have done to all thirteen of them.
    """
    import re

    readme = _read("README.md")
    source = REPO_ROOT / "docs" / "source"
    broken = []

    for url in sorted(set(re.findall(r"https://khchao\.com/gffbase/([\w/.-]*)", readme))):
        slug = url.strip("/")
        if not slug:  # the site root
            continue
        # Sphinx serves `docs/source/content/x.rst` at `/content/x.html`.
        if not slug.endswith(".html"):
            broken.append(f"/{slug} (not a Sphinx page URL)")
            continue
        if not (source / slug.removesuffix(".html")).with_suffix(".rst").is_file():
            broken.append(f"/{slug}")

    assert not broken, (
        f"README links to published pages that do not exist: {broken}. "
        "A renamed page leaves GitHub and PyPI pointing at a 404."
    )


def test_release_date_is_consistent_or_candidate_is_explicitly_unreleased():
    """A candidate must not carry a fabricated release date.

    `CITATION.cff` feeds GitHub's "Cite this repository" button and Zenodo;
    the changelog is what a human reads. Before publication both must describe
    an unreleased candidate; after publication their dates must agree.
    """
    import re

    changelog = _read("CHANGELOG.md")
    citation = _read("CITATION.cff")

    version = re.search(r'^version = "([^"]+)"', _read("pyproject.toml"), re.M)
    assert version, "pyproject.toml has no version"
    version = version.group(1)

    heading = re.search(
        rf"^## \[{re.escape(version)}\] — (Unreleased|\d{{4}}-\d{{2}}-\d{{2}})",
        changelog,
        re.M,
    )
    assert heading, f"CHANGELOG.md has no release-state section for {version}"

    released = re.search(r'^date-released:\s*"?(\d{4}-\d{2}-\d{2})"?', citation, re.M)
    if heading.group(1) == "Unreleased":
        assert released is None, "unreleased candidate must not declare date-released"
        assert "unreleased" in citation.lower(), (
            "CITATION.cff must label candidate metadata as unreleased"
        )
    else:
        assert released, "released version must declare date-released"
        assert heading.group(1) == released.group(1), (
            f"CHANGELOG.md dates {version} at {heading.group(1)} but CITATION.cff "
            f"says {released.group(1)}"
        )


def test_the_citation_version_tracks_the_package_version():
    """A stale `version:` in CITATION.cff makes every generated citation wrong."""
    import re

    version = re.search(r'^version = "([^"]+)"', _read("pyproject.toml"), re.M).group(1)
    cited = re.search(r"^version:\s*(\S+)", _read("CITATION.cff"), re.M)
    assert cited, "CITATION.cff has no version"
    assert cited.group(1).strip('"') == version, (
        f"CITATION.cff cites {cited.group(1)} but the package is {version}"
    )


def _svg_path_points(d: str) -> list[tuple[float, float]]:
    """Sample an SVG path, flattening cubics, so its extent can be measured.

    Small enough to keep here: the alternative is adding a dependency on an
    SVG library purely to assert one geometric fact about one committed file.
    """
    import re

    tokens = re.findall(r"[MmCcSsLlHhVvZz]|-?\d*\.?\d+", d)
    i, cur, start, points, cmd, prev_c2 = 0, (0.0, 0.0), (0.0, 0.0), [], None, None

    def cubic(p0, p1, p2, p3):
        for step in range(41):
            t = step / 40
            u = 1 - t
            points.append(
                (
                    u**3 * p0[0] + 3 * u * u * t * p1[0] + 3 * u * t * t * p2[0] + t**3 * p3[0],
                    u**3 * p0[1] + 3 * u * u * t * p1[1] + 3 * u * t * t * p2[1] + t**3 * p3[1],
                )
            )

    while i < len(tokens):
        tok = tokens[i]
        if re.match(r"[A-Za-z]", tok):
            cmd = tok
            i += 1
            continue
        num = lambda k: float(tokens[k])  # noqa: E731
        if cmd in "Mm":
            x, y = num(i), num(i + 1)
            i += 2
            cur = (cur[0] + x, cur[1] + y) if cmd == "m" else (x, y)
            start = cur
            points.append(cur)
            cmd = "l" if cmd == "m" else "L"
        elif cmd in "Cc":
            a = [num(i + k) for k in range(6)]
            i += 6
            rel = cmd == "c"
            p1 = (cur[0] + a[0], cur[1] + a[1]) if rel else (a[0], a[1])
            p2 = (cur[0] + a[2], cur[1] + a[3]) if rel else (a[2], a[3])
            p3 = (cur[0] + a[4], cur[1] + a[5]) if rel else (a[4], a[5])
            cubic(cur, p1, p2, p3)
            prev_c2, cur = p2, p3
        elif cmd in "Ss":
            a = [num(i + k) for k in range(4)]
            i += 4
            rel = cmd == "s"
            p2 = (cur[0] + a[0], cur[1] + a[1]) if rel else (a[0], a[1])
            p3 = (cur[0] + a[2], cur[1] + a[3]) if rel else (a[2], a[3])
            p1 = (2 * cur[0] - prev_c2[0], 2 * cur[1] - prev_c2[1]) if prev_c2 else cur
            cubic(cur, p1, p2, p3)
            prev_c2, cur = p2, p3
        elif cmd in "Ll":
            x, y = num(i), num(i + 1)
            i += 2
            cur = (cur[0] + x, cur[1] + y) if cmd == "l" else (x, y)
            points.append(cur)
        elif cmd in "Hh":
            x = num(i)
            i += 1
            cur = (cur[0] + x, cur[1]) if cmd == "h" else (x, cur[1])
            points.append(cur)
        elif cmd in "Vv":
            y = num(i)
            i += 1
            cur = (cur[0], cur[1] + y) if cmd == "v" else (cur[0], y)
            points.append(cur)
        elif cmd in "Zz":
            cur = start
            points.append(cur)
        else:
            i += 1
    return points


@pytest.mark.parametrize("svg_name", ["logo.svg", "logo-white.svg"])
def test_the_g_descender_clears_the_exon_block(svg_name):
    """The wordmark's two tiers must not collide.

    The `g` descender used to reach y=342 while the first exon block spans
    y300-340, so at the 36px the site renders the logo at, the tail and the
    exon merged into a single black mass. It was also 0.51x cap height, about
    twice the typographic norm for a grotesque -- too long independent of the
    overlap.

    Asserted on the geometry rather than on a rendering, so it needs no SVG
    toolchain and fails with a number rather than "the logo looks wrong".
    """
    import re

    svg = (REPO_ROOT / "docs" / "source" / "_static" / svg_name).read_text()

    exons = [
        (int(m.group(1)), int(m.group(2)), int(m.group(3)), int(m.group(4)))
        for m in re.finditer(
            r'<rect x="(\d+)" y="(\d+)" width="(\d+)" height="(\d+)" rx="4"/>', svg
        )
    ]
    assert exons, f"{svg_name}: no exon rectangles found"

    match = re.search(r"<!-- g : bowl \+ descender -->\s*<path d=\"([^\"]+)\"", svg)
    assert match, f"{svg_name}: the g glyph is not where this test expects it"
    points = _svg_path_points(match.group(1))

    lowest = max(y for _, y in points)
    for ex, ey, ew, eh in exons:
        intruding = [p for p in points if p[1] > ey and ex <= p[0] <= ex + ew]
        assert not intruding, (
            f"{svg_name}: the g descender reaches y={lowest:.0f} and enters the "
            f"exon block at x{ex}-{ex + ew} y{ey}-{ey + eh} "
            f"({len(intruding)} sampled points). The two tiers must clear each other."
        )

    depth = (lowest - BASELINE) / EM
    assert 0.18 <= depth <= 0.24, (
        f"{svg_name}: the g descender is {depth:.3f} em below the baseline. A "
        "grotesque sits at 0.20-0.21. This band rejects BOTH failures this "
        "glyph has had: the original 0.265 (too deep, ran through the exon) "
        "and the 0.069 'fix' (a third of normal, read as stunted)."
    )


#: Measured from the outlines, not from the file's own comment -- which
#: asserted 240 and was wrong, which is how the descender came to be judged
#: against a baseline 18 units too high.
BASELINE = 258
EM = 318  # ascender 228 above the baseline / 0.718, the Helvetica ratio

#: Each letter, and the label its path carries in the SVG.
LETTERS = ["g : bowl + descender", "f", "f", "a", "s", "e"]


@pytest.mark.parametrize("svg_name", ["logo.svg", "logo-white.svg"])
def test_the_wordmark_sits_on_one_baseline(svg_name):
    """Every letter lands on the same line, give or take optical overshoot.

    `a`, `s` and `e` were each drawn too tall (175, 169 and 158 against an
    x-height of 150) and so hung 25, 19 and 8 units below the line while `f`,
    `f` and the `b` stem sat on it. "ase" visibly drooped away from "gff b".

    Round letters are allowed a couple of units of overshoot below the line --
    that is a real typographic convention, not slop -- and flat-bottomed ones
    are not. Both fit inside the band asserted here; 25 units does not.
    """
    import re

    svg = (REPO_ROOT / "docs" / "source" / "_static" / svg_name).read_text()
    paths = re.findall(r'<path d="([^"]+)"', svg)
    # Document order: g, f, f, cylinder, band, a, s, e.
    by_label = {
        "g": paths[0],
        "f1": paths[1],
        "f2": paths[2],
        "a": paths[5],
        "s": paths[6],
        "e": paths[7],
    }

    offenders = []
    for label, d in by_label.items():
        bottom = max(y for _, y in _svg_path_points(d))
        if label == "g":
            continue  # the g has a descender; checked separately
        if not (BASELINE - 1 <= bottom <= BASELINE + 4):
            offenders.append(f"{label} bottom y={bottom:.1f} ({bottom - BASELINE:+.1f})")

    # The `b` stem is a rect, not a path, and defines the line.
    stem = re.search(r'<rect x="560" y="(\d+)" width="46" height="(\d+)"', svg)
    assert stem, f"{svg_name}: the b stem is not where this test expects it"
    stem_bottom = int(stem.group(1)) + int(stem.group(2))
    if stem_bottom != BASELINE:
        offenders.append(f"b-stem bottom y={stem_bottom} ({stem_bottom - BASELINE:+d})")

    assert not offenders, f"{svg_name}: letters are off the baseline (y={BASELINE}): " + "; ".join(
        offenders
    )


def test_no_published_page_still_tells_a_reader_to_run_mkdocs():
    """The site is Sphinx. Four pages went on instructing MkDocs anyway.

    `407d0ca` is literally titled "retire MkDocs; the Sphinx site is the one
    that ships", and `CONTRIBUTING.md` was updated with it -- but the mirrored
    `contributing.rst` and `testing.rst` were not, so the *published* pages told
    a new contributor to install a toolchain the repo no longer has and run a
    command CI does not run.

    The changelog is exempt by construction: it describes what was true at each
    past release, and rewriting that would be falsifying history.
    """
    offenders = []
    for path in sorted((REPO_ROOT / "docs" / "source").rglob("*.rst")):
        if path.name == "changelog.rst":
            continue
        for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            if "mkdocs" in line.lower():
                offenders.append(f"{path.relative_to(REPO_ROOT)}:{number}: {line.strip()[:70]}")

    assert not offenders, "published pages still reference MkDocs:\n" + "\n".join(offenders)


def test_readme_relative_links_and_images_resolve():
    """GitHub and PyPI render the README from the repo root.

    The absolute-URL check next to this one only ever looked at
    `https://khchao.com/...`, so a relative path could rot unnoticed — and two
    did. The logo `src` still pointed at `docs/assets/`, deleted in the Sphinx
    migration, so the image at the very top of the README was broken on GitHub
    AND on the PyPI project page; and `[the CLI reference](cli.md)` named a
    root-level file that has never existed.
    """
    import re

    readme = _read("README.md")
    broken = []

    for match in re.finditer(r"\[([^\]]{1,60})\]\((?!https?://|mailto:|#)([^)]+)\)", readme):
        target = match.group(2).split("#")[0]
        if target and not (REPO_ROOT / target).exists():
            broken.append(f"link [{match.group(1)}]({match.group(2)})")

    for match in re.finditer(r'src="([^"]+)"', readme):
        target = match.group(1).split("#")[0]
        if target.startswith(("http://", "https://")):
            continue
        if not (REPO_ROOT / target).exists():
            broken.append(f"image src={match.group(1)}")

    assert not broken, "README points at paths that do not exist: " + "; ".join(broken)
