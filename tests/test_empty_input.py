"""Input with no features raises `EmptyInputError`, as gffutils does.

0.2.0 built a valid, empty database instead -- for an empty file, a file of
headers and comments, a FASTA file, and a binary file alike -- so pointing
`create_db` at the wrong path, or at a gzip stream the reader did not
recognize, looked like success until the first query came back empty.
"""

from __future__ import annotations

import gzip
import os

import gffbase
import pytest
from gffbase import create_db, native_available
from gffbase.exceptions import EmptyInputError
from gffbase.ingest import from_file

ENGINES = ["python"] + (["rust"] if native_available() else [])

NO_FEATURES = {
    "empty": b"",
    "header_only": b"##gff-version 3\n",
    "comments": b"##gff-version 3\n# a comment\n#another\n",
    "fasta_section_only": b"##gff-version 3\n##FASTA\n>chr1\nACGT\nACGT\n",
    "bare_fasta": b">chr1\nACGTACGT\nACGT\n",
    "gtf_comments": b"#!genome-build GRCh38\n#!genome-version GRCh38\n",
    "binary": bytes(range(256)).replace(b"\n", b"").replace(b"\t", b"") * 8,
}

ONE_FEATURE = "chr1\tt\tgene\t1\t10\t.\t+\t.\tID=g1\n"


@pytest.mark.parametrize("engine", ENGINES)
@pytest.mark.parametrize("name", sorted(NO_FEATURES))
def test_no_features_raises(tmp_path, engine, name):
    src = tmp_path / f"{name}.gff3"
    src.write_bytes(NO_FEATURES[name])
    with pytest.raises(EmptyInputError, match="no features in"):
        from_file(str(src), ":memory:", engine=engine)


@pytest.mark.parametrize("name", sorted(NO_FEATURES))
def test_create_db_raises_and_leaves_nothing_behind(tmp_path, name):
    src = tmp_path / f"{name}.gff3"
    src.write_bytes(NO_FEATURES[name])
    dbfn = tmp_path / "out.duckdb"
    with pytest.raises(EmptyInputError):
        create_db(str(src), str(dbfn))
    # No database, no scratch file, no DuckDB sidecar.
    assert os.listdir(tmp_path) == [src.name]


def test_the_message_names_the_file(tmp_path):
    src = tmp_path / "wrong_file.gff3"
    src.write_text("##gff-version 3\n")
    with pytest.raises(EmptyInputError, match="wrong_file.gff3"):
        create_db(str(src), ":memory:")


def test_from_string_does_not_name_a_temp_file():
    with pytest.raises(EmptyInputError, match="no features in the input"):
        create_db("##gff-version 3\n", ":memory:", from_string=True)


def test_empty_string_raises():
    with pytest.raises(EmptyInputError):
        create_db("", ":memory:", from_string=True)


def test_empty_gzip_raises(tmp_path):
    src = tmp_path / "empty.gff3.gz"
    src.write_bytes(gzip.compress(b"##gff-version 3\n"))
    with pytest.raises(EmptyInputError):
        create_db(str(src), ":memory:")


def test_a_transform_that_rejects_everything_raises():
    with pytest.raises(EmptyInputError, match="transform rejected all 2 features"):
        create_db(ONE_FEATURE * 2, ":memory:", from_string=True, transform=lambda f: None)


def test_a_transform_that_keeps_something_does_not_raise():
    src = ONE_FEATURE + ONE_FEATURE.replace("g1", "g2")
    db = create_db(
        src,
        ":memory:",
        from_string=True,
        transform=lambda f: f if f.attributes["ID"] == ["g2"] else None,
    )
    assert [f.id for f in db.all_features()] == ["g2"]


def test_one_feature_is_enough(tmp_path):
    db = create_db("##gff-version 3\n" + ONE_FEATURE, ":memory:", from_string=True)
    assert db.count_features_of_type() == 1


def test_it_is_a_value_error_and_exported():
    # Subclassing ValueError keeps `except ValueError` handlers working.
    assert issubclass(EmptyInputError, ValueError)
    assert gffbase.EmptyInputError is EmptyInputError
