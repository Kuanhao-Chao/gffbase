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


def test_example_filename_resolves_test_fixtures():
    # See test_compat_surface: `tests/data/` is in the source tree and the
    # sdist, but pip installs only the package directory, so this cannot
    # resolve from an installed environment. The helper is correct; the
    # fixture tree is absent.
    import gffbase as _g

    if not (Path(_g.__file__).resolve().parent.parent.parent / "tests" / "data").is_dir():
        pytest.skip("tests/data/ is not reachable from the installed package")
    p = example_filename("simple.gff3")
    assert Path(p).is_file()


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

    It is vendored under `tests/data/upstream/`, but that directory was not on
    the search path -- so the canonical example failed even in a source
    checkout, where the file was sitting on disk the whole time.
    """
    from pathlib import Path

    import gffbase as _g
    from gffbase import helpers

    # Checkout-only: `tests/data/upstream/` is not installed. See the note on
    # `test_example_filename_resolves_test_fixtures`.
    if not (Path(_g.__file__).resolve().parent.parent.parent / "tests" / "data").is_dir():
        pytest.skip("tests/data/ is not reachable from the installed package")

    for name in ("FBgn0031208.gff", "FBgn0031208.gtf"):
        assert Path(helpers.example_filename(name)).is_file()


def test_example_filename_explains_where_the_fixtures_actually_are():
    """A bare "file not found" sends the reader looking for a typo.

    The message used to say the fixtures are absent from the *binary wheel*
    and advise `pip install --no-binary gffbase gffbase`. That advice cannot
    work: `--no-binary` still builds a wheel and installs only the package
    directory, and `tests/` is not inside it -- so the suggested fix fails
    exactly like the thing it was meant to fix. Verified by installing the
    sdist into a clean venv, where the helper still raised.

    What it says now is the true constraint -- a checkout or an unpacked
    sdist, run from its root -- and an alternative that always works.
    Declared in `tests/parity/deviations.toml`.
    """
    import pytest
    from gffbase import helpers

    with pytest.raises(FileNotFoundError) as excinfo:
        helpers.example_filename("no_such_example.gff")

    message = str(excinfo.value)
    assert "tests/data/" in message
    assert "not installed" in message.lower()
    assert "checkout" in message
    # The advice that cannot work must not come back.
    assert "--no-binary" not in message
