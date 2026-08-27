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
"""Column-9 delimiters are structural only in the unescaped top-level state."""

from __future__ import annotations

import pytest
from gffbase import constants, create_db, native_available, parse_bytes

ENGINES = ["python"] + (["rust"] if native_available() else [])


@pytest.mark.parametrize("engine", ENGINES)
def test_gtf_quoted_equals_escaped_quotes_semicolons_and_unicode_are_data(engine):
    blob = 'gene_id "G=雪"; note "quoted \\"value\\" and escaped\\;semicolon %3B";'
    row = f"chr1\tsrc\texon\t1\t10\t.\t+\t.\t{blob}\n".encode()

    (record,) = list(parse_bytes(row, engine=engine))

    assert record.attributes_blob == blob.encode()
    assert record.attributes_pairs == [
        ("gene_id", "G=雪", 0),
        ("note", 'quoted \\"value\\" and escaped\\;semicolon %3B', 0),
    ]


@pytest.mark.parametrize("engine", ENGINES)
def test_gff3_escaped_semicolon_does_not_start_a_new_attribute(engine):
    blob = r"ID=x;Note=left\;right;Encoded=%E9%9B%AA%3Bdone"
    row = f"chr1\tsrc\texon\t1\t10\t.\t+\t.\t{blob}\n".encode()

    (record,) = list(parse_bytes(row, engine=engine))

    assert record.attributes_blob == blob.encode()
    assert record.attributes_pairs == [
        ("ID", "x", 0),
        ("Note", r"left\;right", 0),
        ("Encoded", "雪;done", 0),
    ]


@pytest.mark.parametrize("engine", ENGINES)
def test_gff3_value_whitespace_after_equals_is_literal_data(engine):
    row = b"chr1\tsrc\texon\t1\t10\t.\t+\t.\tID=x;Note= \n"

    (record,) = list(parse_bytes(row, engine=engine))

    assert record.attributes_pairs == [("ID", "x", 0), ("Note", " ", 0)]


@pytest.mark.parametrize("engine", ENGINES)
def test_compat_gff3_value_whitespace_after_equals_is_literal_data(engine):
    row = b"chr1\tsrc\texon\t1\t10\t.\t+\t.\tID=x;Note= \n"

    (record,) = list(parse_bytes(row, engine=engine, validation="gffutils"))

    assert record.attributes_pairs == [("ID", "x", 0), ("Note", " ", 0)]


def test_default_create_db_keeps_gff3_value_whitespace_and_revalidates():
    text = "chr1\tsrc\texon\t1\t10\t.\t+\t.\tID=x;Note= \n"

    db = create_db(text, ":memory:", from_string=True)

    assert db["x"].attributes["Note"] == [" "]
    assert db.validate(level="full", sample=None).ok


@pytest.mark.parametrize("engine", ENGINES)
def test_gff3_escape_decoding_matches_urllib_policy(engine):
    row = b"chr1\tsrc\texon\t1\t10\t.\t+\t.\tID=x;Note=ok%20bad%FFtail%GG%2\n"

    (record,) = list(parse_bytes(row, engine=engine, validation="gffutils"))

    assert record.attributes_pairs == [
        ("ID", "x", 0),
        ("Note", "ok bad\ufffdtail%GG%2", 0),
    ]


@pytest.mark.parametrize("engine", ENGINES)
def test_ignore_url_escape_characters_is_identical_in_every_engine(engine):
    row = b"chr1\tsrc\texon\t1\t10\t.\t+\t.\tID=x;Note=ok%20bad%3Btail\n"
    constants.ignore_url_escape_characters = True
    try:
        (record,) = list(parse_bytes(row, engine=engine, validation="gffutils"))
    finally:
        constants.ignore_url_escape_characters = False

    assert record.attributes_pairs == [
        ("ID", "x", 0),
        ("Note", "ok%20bad%3Btail", 0),
    ]


@pytest.mark.parametrize("engine", ENGINES)
def test_attribute_indices_do_not_wrap_after_unsigned_16_bit(engine):
    values = ",".join(f"p{index}" for index in range(65_537))
    row = f"chr1\tsrc\texon\t1\t10\t.\t+\t.\tParent={values}\n".encode()

    (record,) = list(parse_bytes(row, engine=engine))

    assert record.attributes_pairs[-1] == ("Parent", "p65536", 65_536)
