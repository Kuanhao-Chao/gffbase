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
"""Basic correctness tests. Each one runs against both engines via the
`engine` fixture in conftest.py.
"""

from __future__ import annotations

from gffbase import detect_dialect, parse_gff


def test_simple_gff3_record_count(gff3_path, engine):
    feats = list(parse_gff(gff3_path, engine=engine))
    assert len(feats) == 5


def test_simple_gff3_first_record(gff3_path, engine):
    it = parse_gff(gff3_path, engine=engine)
    f = next(iter(it))
    assert f.seqid == "chr1"
    assert f.source == "source1"
    assert f.featuretype == "gene"
    assert f.start == 100
    assert f.end == 500
    assert f.strand == "+"
    attrs = f.attributes_dict()
    assert attrs["ID"] == ["gene1"]
    assert attrs["Name"] == ["alpha"]
    # percent-decoded
    assert attrs["Note"] == ["hello world"]


def test_simple_gff3_multivalue(gff3_path, engine):
    feats = list(parse_gff(gff3_path, engine=engine))
    last = feats[-1]
    assert last.featuretype == "gene"
    assert last.attributes_dict()["Parent"] == ["ancestorA", "ancestorB"]


def test_simple_gff3_dialect(gff3_path, engine):
    d = detect_dialect(gff3_path, engine=engine)
    assert d["fmt"] == "gff3"
    assert d["keyval separator"] == "="


def test_simple_gff3_directives(gff3_path, engine):
    it = parse_gff(gff3_path, engine=engine)
    list(it)  # drive iterator
    directives = it.directives()
    # `##` is stripped on capture, matching gffutils.
    assert any(d.startswith("gff-version") for d in directives)
    assert any(d.startswith("sequence-region") for d in directives)
    assert not any(d.startswith("#") for d in directives)


def test_simple_gtf_dialect(gtf_path, engine):
    d = detect_dialect(gtf_path, engine=engine)
    assert d["fmt"] == "gtf"
    assert d["keyval separator"] == " "
    assert d["quoted GFF2 values"] is True


def test_simple_gtf_attribute_extraction(gtf_path, engine):
    feats = list(parse_gff(gtf_path, engine=engine))
    assert len(feats) == 3
    attrs = feats[0].attributes_dict()
    assert attrs["gene_id"] == ["ENSG1"]
    assert attrs["transcript_id"] == ["ENST1"]
    assert attrs["exon_number"] == ["1"]


def test_simple_gtf_semicolon_in_quotes(gtf_path, engine):
    feats = list(parse_gff(gtf_path, engine=engine))
    last = feats[-1]
    note = last.attributes_dict()["note"]
    assert note == ["weird; with semi"]


def test_blob_round_trip(gff3_path, engine):
    feats = list(parse_gff(gff3_path, engine=engine))
    # The blob is the raw col-9 bytes; for the first feature it should match
    # the literal attribute string.
    expected = b"ID=gene1;Name=alpha;Note=hello%20world"
    assert feats[0].attributes_blob == expected
