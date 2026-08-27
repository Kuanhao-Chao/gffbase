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
"""Attribute cardinality must agree across parser, Arrow, and DuckDB."""

from __future__ import annotations

import pytest
from gffbase import create_db
from gffbase.feature import ParsedFeature
from gffbase.ingest import _ArrowBatchBuilder


def test_attribute_idx_is_integer_and_accepts_more_than_smallint(tmp_path):
    values = ",".join(f"p{index}" for index in range(32_769))
    src = tmp_path / "wide.gff3"
    src.write_text(f"chr1\tsrc\texon\t1\t10\t.\t+\t.\tID=e;Parent={values}\n")

    db = create_db(str(src), ":memory:")

    idx_type = db.conn.execute(
        "SELECT data_type FROM duckdb_columns() "
        "WHERE table_name = 'attributes' AND column_name = 'idx'"
    ).fetchone()[0]
    assert idx_type == "INTEGER"
    assert db.conn.execute(
        "SELECT COUNT(*), MAX(idx) FROM attributes WHERE key = 'Parent'"
    ).fetchone() == (32_769, 32_768)


def test_attribute_idx_outside_integer_range_raises_explicit_overflow():
    feature = ParsedFeature(
        seqid="chr1",
        source="src",
        featuretype="gene",
        start=1,
        end=10,
        score=".",
        strand="+",
        frame=".",
        attributes_blob=b"Name=x",
        attributes_pairs=[("Name", "x", 2**31)],
    )
    builder = _ArrowBatchBuilder({})

    with pytest.raises(OverflowError, match="signed 32-bit INTEGER"):
        builder.append("g", feature, 1)
