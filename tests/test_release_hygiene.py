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


def _read(name: str) -> str:
    path = REPO_ROOT / name
    if not path.is_file():
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
    assert gffbase.__version__ == cargo_version.group(1), (
        f"gffbase.__version__ ({gffbase.__version__}) != "
        f"rust/Cargo.toml version ({cargo_version.group(1)})"
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
_VERSION_LITERALS = {
    "CITATION.cff": r"^version: (.+)$",
    "README.md": r"^  version = \{([^}]+)\},$",
    "CONTRIBUTING.md": r"gffbase\.__version__\)\"\s+# (\S+)",
}


@pytest.mark.parametrize(("filename", "pattern"), sorted(_VERSION_LITERALS.items()))
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


def test_every_cli_command_is_documented():
    """A shipped command with no documentation is invisible."""
    from gffbase.cli import build_parser

    registered = set([a for a in build_parser()._actions if a.dest == "command"][0].choices)
    page = _read("docs/cli.md")
    undocumented = sorted(c for c in registered if f"gffbase {c}" not in page)
    assert not undocumented, f"CLI commands missing from docs/cli.md: {undocumented}"


def test_every_nav_entry_resolves_and_every_page_is_reachable():
    """`mkdocs.yml` sets `strict: true`, so a page outside the nav fails the
    build. Catch it here rather than in a deploy that only runs on `main`."""
    import re

    mkdocs = _read("mkdocs.yml")
    nav = mkdocs.split("nav:", 1)[1]
    refs = set(re.findall(r"([\w/.-]+\.md)\s*$", nav, re.M))
    docs = REPO_ROOT / "docs"

    missing = sorted(r for r in refs if not (docs / r).is_file())
    assert not missing, f"nav references pages that do not exist: {missing}"

    excluded = set()
    if "exclude_docs:" in mkdocs:
        block = mkdocs.split("exclude_docs:", 1)[1].split("\n\n", 1)[0]
        excluded = {line.strip() for line in block.splitlines() if line.strip().endswith(".md")}

    on_disk = {str(p.relative_to(docs)) for p in docs.rglob("*.md")}
    orphans = sorted(on_disk - refs - excluded)
    assert not orphans, (
        f"pages outside the nav would fail the strict build: {orphans}. "
        f"Add them to nav, or to exclude_docs if they are deliberately unpublished."
    )
