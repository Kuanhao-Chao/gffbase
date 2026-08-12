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
