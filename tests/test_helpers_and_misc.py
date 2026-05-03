# ---------------------------------------------------------------------------
# Author: Kuan-Hao Chao <kuanhao.chao@gmail.com>
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
        str(DATA / "simple.gff3"), ":memory:",
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
