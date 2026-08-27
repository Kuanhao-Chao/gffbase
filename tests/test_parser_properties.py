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
"""Bounded generative checks for parser engines and input transports."""

from __future__ import annotations

import gzip

import pytest
from gffbase import Feature, GFFFormatError, native_available, parse_bytes, parse_gff
from gffbase.dialect import default_dialect
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

pytestmark = pytest.mark.property


SAFE_TOKEN = st.text(
    alphabet="abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_.-",
    min_size=1,
    max_size=24,
)
POSITIVE_COORD = st.integers(min_value=1, max_value=2**31 - 1)
GFF3_VALUE = st.text(
    alphabet="abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789 _.-;,&=%雪α",
    min_size=1,
    max_size=36,
)
GTF_VALUE = st.lists(
    st.sampled_from(["a", "Z", "0", " ", "=", ";", r"\;", r"\"", "%3B", "雪", "α"]),
    min_size=1,
    max_size=18,
).map("".join)


def _encode_gff3(value: str) -> str:
    """Independent minimal GFF3 encoder for the generated alphabet."""
    return "".join(f"%{ord(char):02X}" if char in "%;&=," else char for char in value)


def _record_as_feature(record, *, fmt: str) -> Feature:
    dialect = default_dialect()
    if fmt == "gtf":
        dialect.update(
            {
                "fmt": "gtf",
                "field separator": "; ",
                "keyval separator": " ",
                "quoted GFF2 values": True,
                "trailing semicolon": True,
            }
        )
    dialect["order"] = [key for key, _value, _index in record.attributes_pairs]
    return Feature(
        seqid=record.seqid,
        source=record.source,
        featuretype=record.featuretype,
        start=record.start,
        end=record.end,
        score=record.score,
        strand=record.strand,
        frame=record.frame,
        attributes=record.attributes_pairs,
        dialect=dialect,
        keep_order=True,
    )


def _snapshot(record):
    return (
        record.seqid,
        record.source,
        record.featuretype,
        record.start,
        record.end,
        record.score,
        record.strand,
        record.frame,
        record.attributes_blob,
        record.attributes_pairs,
        record.extra,
    )


@settings(
    suppress_health_check=[HealthCheck.function_scoped_fixture],
)
@given(
    seqid=SAFE_TOKEN,
    featuretype=st.sampled_from(["gene", "mRNA", "exon", "region"]),
    start=POSITIVE_COORD,
    length=st.integers(min_value=0, max_value=100_000),
    strand=st.sampled_from(["+", "-", "?", "."]),
    identifier=SAFE_TOKEN,
    score=st.one_of(st.just("."), st.integers(-1_000_000, 1_000_000).map(str)),
)
def test_valid_record_is_transport_and_engine_invariant(
    tmp_path,
    seqid,
    featuretype,
    start,
    length,
    strand,
    identifier,
    score,
):
    end = start + length
    row = (
        f"{seqid}\tsrc\t{featuretype}\t{start}\t{end}\t{score}\t{strand}\t.\tID={identifier}\n"
    ).encode()

    expected = (
        seqid,
        "src",
        featuretype,
        start,
        end,
        score,
        strand,
        ".",
        f"ID={identifier}".encode(),
        [("ID", identifier, 0)],
        [],
    )
    assert _snapshot(next(iter(parse_bytes(row, engine="python")))) == expected

    plain = tmp_path / "generated.gff3"
    plain.write_bytes(row)
    assert _snapshot(next(iter(parse_gff(str(plain), engine="python")))) == expected

    compressed = tmp_path / "generated.gff3.gz"
    with gzip.open(compressed, "wb") as stream:
        stream.write(row)
    assert _snapshot(next(iter(parse_gff(str(compressed), engine="python")))) == expected

    if native_available():
        assert _snapshot(next(iter(parse_bytes(row, engine="rust")))) == expected
        assert _snapshot(next(iter(parse_gff(str(plain), engine="rust")))) == expected
        assert _snapshot(next(iter(parse_gff(str(compressed), engine="rust")))) == expected


@given(identifier=SAFE_TOKEN, note=GFF3_VALUE, start=POSITIVE_COORD)
def test_generated_gff3_parse_serialize_parse_matches_independent_values(identifier, note, start):
    encoded = _encode_gff3(note)
    row = f"chr雪\tsrc\tgene\t{start}\t{start + 9}\t.\t+\t.\tID={identifier};Note={encoded}\n"
    engines = ["python"] + (["rust"] if native_available() else [])

    for engine in engines:
        first = next(iter(parse_bytes(row.encode(), engine=engine)))
        assert first.seqid == "chr雪"
        assert first.start == start
        assert first.end == start + 9
        assert first.attributes_pairs == [("ID", identifier, 0), ("Note", note, 0)]

        rendered = _record_as_feature(first, fmt="gff3").to_line(normalized=True) + "\n"
        second = next(iter(parse_bytes(rendered.encode(), engine=engine)))
        assert second.attributes_pairs == first.attributes_pairs
        assert _snapshot(second)[:8] == _snapshot(first)[:8]


@given(gene_id=SAFE_TOKEN, transcript_id=SAFE_TOKEN, note=GTF_VALUE, start=POSITIVE_COORD)
def test_generated_gtf_parse_serialize_parse_matches_independent_values(
    gene_id, transcript_id, note, start
):
    attributes = f'gene_id "{gene_id}"; transcript_id "{transcript_id}"; note "{note}";'
    row = f"chr1\tsrc\texon\t{start}\t{start + 4}\t.\t-\t.\t{attributes}\n"
    expected_pairs = [
        ("gene_id", gene_id, 0),
        ("transcript_id", transcript_id, 0),
        ("note", note, 0),
    ]
    engines = ["python"] + (["rust"] if native_available() else [])

    for engine in engines:
        first = next(iter(parse_bytes(row.encode(), engine=engine)))
        assert first.attributes_pairs == expected_pairs
        rendered = _record_as_feature(first, fmt="gtf").to_line(normalized=True) + "\n"
        second = next(iter(parse_bytes(rendered.encode(), engine=engine)))
        assert second.attributes_pairs == expected_pairs
        assert _snapshot(second)[:8] == _snapshot(first)[:8]


@given(key=SAFE_TOKEN, unterminated=GTF_VALUE)
def test_generated_unterminated_gtf_quotes_are_rejected(key, unterminated):
    row = f'chr1\tsrc\texon\t1\t10\t.\t+\t.\t{key} "{unterminated}\n'.encode()
    for engine in ["python"] + (["rust"] if native_available() else []):
        with pytest.raises(GFFFormatError) as excinfo:
            list(parse_bytes(row, engine=engine))
        assert excinfo.value.kind == "InvalidAttribute"


@given(
    prefix=SAFE_TOKEN,
    invalid_escape=st.sampled_from(["%", "%0", "%GG", "%0G", "%G0"]),
)
def test_invalid_percent_escapes_reject_strict_and_warn_in_compat(prefix, invalid_escape):
    row = f"chr1\tsrc\texon\t1\t10\t.\t+\t.\tID={prefix}{invalid_escape}\n".encode()
    engines = ["python"] + (["rust"] if native_available() else [])

    for engine in engines:
        with pytest.raises(GFFFormatError) as excinfo:
            list(parse_bytes(row, engine=engine))
        assert excinfo.value.kind == "InvalidAttribute"

        iterator = parse_bytes(row, engine=engine, validation="gffutils")
        assert len(list(iterator)) == 1
        assert [warning["kind"] for warning in iterator.warnings] == ["InvalidAttribute"]
