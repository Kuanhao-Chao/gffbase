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
"""Helpers, dialect template, exceptions, and create_db kwargs."""

from __future__ import annotations

from pathlib import Path

import gffbase
import pytest
from gffbase import (
    AttributeStringError,
    DuplicateIDError,
    EmptyInputError,
    FeatureNotFoundError,
    create_db,
    example_filename,
)
from gffbase.dialect import default_dialect, merge_dialects

DATA = Path(__file__).parent / "data"


# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------


def test_feature_not_found_error_carries_id():
    err = FeatureNotFoundError("missing-id")
    assert err.feature_id == "missing-id"
    assert "missing-id" in str(err)


def test_other_exceptions_construct_without_args():
    DuplicateIDError()
    AttributeStringError()
    EmptyInputError()


# ---------------------------------------------------------------------------
# Dialect template
# ---------------------------------------------------------------------------


def test_default_dialect_shape():
    d = default_dialect()
    assert d["fmt"] == "gff3"
    assert d["keyval separator"] == "="
    assert d["multival separator"] == ","


def test_merge_dialects_empty_returns_default():
    d = merge_dialects([])
    assert d == default_dialect()


def test_merge_dialects_majority_gtf():
    samples = [
        {"fmt": "gtf", "field separator": "; "},
        {"fmt": "gtf", "field separator": "; "},
        {"fmt": "gff3", "field separator": ";"},
    ]
    out = merge_dialects(samples)
    assert out["fmt"] == "gtf"
    assert out["keyval separator"] == " "


def test_merge_dialects_propagates_boolean_flags():
    samples = [
        {"fmt": "gff3", "trailing semicolon": True},
        {"fmt": "gff3", "trailing semicolon": False},
    ]
    out = merge_dialects(samples)
    assert out["trailing semicolon"] is True


def test_merge_dialects_attribute_order_first_appearance():
    samples = [
        {"fmt": "gff3", "order": ["ID", "Name"]},
        {"fmt": "gff3", "order": ["Parent", "Name"]},
    ]
    out = merge_dialects(samples)
    assert out["order"][:2] == ["ID", "Name"]
    assert "Parent" in out["order"]


# ---------------------------------------------------------------------------
# example_filename
# ---------------------------------------------------------------------------


#: Fixtures that stay behind in `tests/data/`. `__pycache__` is generated,
#: `v1/` is a schema-migration artifact rather than an example, and
#: `attr_test_cases.py` is data wearing a `.py` extension -- inside the package
#: it would become importable and land in mypy's and ruff's scope.
UNSHIPPED_FIXTURES = {"upstream/attr_test_cases.py"}
UNSHIPPED_FIXTURE_DIRS = {"__pycache__", "v1"}


def _shipped_fixture_names() -> set[str]:
    names = set()
    for path in DATA.rglob("*"):
        if not path.is_file():
            continue
        rel = path.relative_to(DATA)
        if set(rel.parts) & UNSHIPPED_FIXTURE_DIRS or rel.as_posix() in UNSHIPPED_FIXTURES:
            continue
        names.add(rel.as_posix())
    return names


def test_example_filename_resolves_from_the_installed_package_alone():
    """The fixture every gffutils tutorial opens must work from a wheel.

    `example_filename` has always looked in `<package>/data/` first, but that
    directory did not exist, so every lookup fell through to `tests/data/` --
    which pip does not install, because `tests/` is not inside the package. The
    oracle ships its own examples, so upstream's copy worked from a wheel and
    ours raised, while `migration.rst` advertised the two as equivalent.

    Resolving from the package directory alone is the property that matters: a
    path found under `tests/` proves nothing about an installed environment.
    """
    packaged = Path(gffbase.__file__).resolve().parent / "data"

    assert (packaged / "simple.gff3").is_file()
    assert (packaged / "upstream" / "FBgn0031208.gff").is_file()
    assert Path(example_filename("simple.gff3")).resolve().is_relative_to(packaged)
    assert Path(example_filename("FBgn0031208.gff")).resolve().is_relative_to(packaged)


def test_the_packaged_fixtures_cannot_drift_from_the_test_fixtures():
    """One corpus, two locations: a fixture edited in one must reach both."""
    import hashlib

    packaged = Path(gffbase.__file__).resolve().parent / "data"
    expected = _shipped_fixture_names()
    assert expected, "no fixtures to ship -- the exclusion set is wrong"

    found = {p.relative_to(packaged).as_posix() for p in packaged.rglob("*") if p.is_file()}
    assert found == expected, (
        f"packaged fixtures differ from tests/data/: "
        f"missing {sorted(expected - found)}, unexpected {sorted(found - expected)}"
    )
    for name in sorted(expected):
        source = (DATA / name).read_bytes()
        shipped = (packaged / name).read_bytes()
        assert hashlib.sha256(shipped).hexdigest() == hashlib.sha256(source).hexdigest(), (
            f"{name} differs between tests/data/ and the packaged copy"
        )


def test_the_packaged_fixtures_are_not_gitignored():
    """Present on disk is not the same as present in the wheel.

    `.gitignore` blanket-ignores `*.gff`, `*.gff3`, `*.gtf` and `*.fa` so that
    real genomic data cannot land in the repo by accident, and the re-include
    that rescues the corpus is scoped to `tests/`. Without a second one for the
    package copy these files are untrackable -- and, because maturin collects
    the python source tree through gitignore, a wheel was built carrying 9 of
    the 35 files, none of them the ones the documented examples open. Nothing
    failed: the build was silent and the tests passed from the checkout.
    """
    import subprocess

    repo_root = Path(__file__).resolve().parent.parent
    packaged = repo_root / "python" / "gffbase" / "data"
    paths = [str(p.relative_to(repo_root)) for p in sorted(packaged.rglob("*")) if p.is_file()]
    assert paths, "no packaged fixtures found"

    result = subprocess.run(
        ["git", "check-ignore", *paths], cwd=repo_root, capture_output=True, text=True
    )
    # `git check-ignore` exits 1 and prints nothing when NO path is ignored.
    ignored = [line for line in result.stdout.splitlines() if line.strip()]
    assert not ignored, (
        "these packaged fixtures are gitignored, so they reach neither a clone "
        "nor the wheel:\n  " + "\n  ".join(ignored)
    )


def test_example_filename_missing_raises():
    with pytest.raises(FileNotFoundError):
        example_filename("does-not-exist.gff3")


# ---------------------------------------------------------------------------
# create_db kwargs / from_string
# ---------------------------------------------------------------------------


def test_create_db_from_string():
    text = "chr1\tsrc\texon\t1\t10\t.\t+\t.\tID=ex1\n"
    db = create_db(text, ":memory:", from_string=True)
    assert "ex1" in db


def test_create_db_force_overwrite(tmp_path):
    out = tmp_path / "ex.duckdb"
    create_db(str(DATA / "hierarchy.gff3"), str(out))
    # Re-create with force=True must succeed.
    db = create_db(str(DATA / "hierarchy.gff3"), str(out), force=True)
    assert "g1" in db


def test_create_db_accepts_legacy_kwargs_without_error():
    # All accepted-but-no-op kwargs from the legacy signature.
    db = create_db(
        str(DATA / "simple.gff3"),
        ":memory:",
        keep_order=False,
        sort_attribute_values=False,
        text_factory=str,
        merge_strategy="error",
        force_merge_fields=None,
        transform=None,
        gtf_transcript_key="transcript_id",
        gtf_gene_key="gene_id",
        gtf_subfeature="exon",
        infer_gene_extent=True,
        verbose=False,
        force_dialect_check=False,
        force_gff=False,
        checklines=10,
    )
    n = db.count_features_of_type()
    assert n > 0


def test_make_query_parameterises_the_value_arguments():
    """`featuretype`, `limit` and `strand` are data, and are bound as data.

    The register of what is and is not escaped matters more here than usual,
    because `make_query` also accepts raw-SQL slots (see the next test).
    """
    from gffbase import helpers

    for kwargs, expected in (
        ({"featuretype": "x' OR '1'='1"}, 1),
        ({"featuretype": ["a", "x' OR '1'='1"]}, 2),
        ({"strand": "+' OR '1'='1"}, 1),
        ({"limit": ("chr1'; DROP TABLE features; --", 1, 100)}, 3),
    ):
        args: list = []
        sql, bound = helpers.make_query(args, **kwargs)
        assert "DROP TABLE" not in sql
        assert "OR '1'='1" not in sql
        assert len(bound) == expected


def test_make_query_rejects_an_unvalidated_order_by():
    """Upstream interpolates a bare string verbatim; gffbase does not."""
    import pytest
    from gffbase import helpers

    with pytest.raises(ValueError):
        helpers.make_query([], order_by="start; DROP TABLE features")


def test_make_query_documents_other_and_extra_as_raw_sql():
    """A pinned expectation, not an aspiration.

    `other` and `extra` carry the caller's own SQL by design -- upstream builds
    its relation joins through `other`. That is defensible, but only while it
    is written down: everything *around* them is validated, which makes it easy
    to assume they are too.
    """
    from gffbase import helpers

    doc = helpers.make_query.__doc__ or ""
    assert "raw SQL" in doc
    collapsed = " ".join(doc.split())
    assert "`other` and `extra`\n        are interpolated verbatim" in doc or (
        "are interpolated verbatim" in collapsed
    )

    args: list = []
    sql, bound = helpers.make_query(args, other="WHERE 1=1")
    assert "WHERE 1=1" in sql and not bound


def test_example_filename_finds_the_upstream_corpus():
    """`FBgn0031208.gff` is the fixture every gffutils example opens.

    It is vendored from the gffutils corpus, and the `upstream/` subdirectory
    was once off the search path -- so the canonical example failed even in a
    source checkout, where the file was sitting on disk the whole time.
    """
    from gffbase import helpers

    for name in ("FBgn0031208.gff", "FBgn0031208.gtf"):
        assert Path(helpers.example_filename(name)).is_file()


def test_example_filename_names_the_directory_it_searched():
    """A bare "file not found" sends the reader looking for a typo.

    The message used to explain that the fixtures ship with the checkout and
    the sdist but are never installed, and advised cloning the repository.
    That was true and is no longer: the corpus travels inside the wheel, so a
    miss now means the name is wrong, and the useful thing to say is where it
    looked and what else it accepts.
    """
    import pytest
    from gffbase import helpers

    with pytest.raises(FileNotFoundError) as excinfo:
        helpers.example_filename("no_such_example.gff")

    message = str(excinfo.value)
    assert "no_such_example.gff" in message
    assert "data" in message
    # The advice that no longer applies must not come back.
    assert "--no-binary" not in message
    assert "checkout" not in message
