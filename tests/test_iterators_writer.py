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
"""Tests for DataIterator and GFFWriter."""

from __future__ import annotations

import io
from pathlib import Path

import pytest

from gffbase import DataIterator, Feature, GFFWriter, create_db

DATA = Path(__file__).parent / "data"


def test_data_iterator_yields_features(tmp_path):
    feats = list(DataIterator(str(DATA / "simple.gff3")))
    assert len(feats) == 5
    assert all(isinstance(f, Feature) for f in feats)


def test_data_iterator_dialect_and_directives(tmp_path):
    it = DataIterator(str(DATA / "simple.gff3"))
    list(it)
    assert it.dialect["fmt"] == "gff3"
    assert any(d.startswith("##gff-version") for d in it.directives)


def test_data_iterator_from_string():
    text = "chr1\tsrc\texon\t1\t10\t.\t+\t.\tID=x\n"
    it = DataIterator(text, from_string=True)
    feats = list(it)
    assert len(feats) == 1
    assert feats[0].id is None or feats[0].seqid == "chr1"


def test_data_iterator_transform_filters_features():
    it = DataIterator(
        str(DATA / "simple.gff3"),
        transform=lambda f: False if f.featuretype == "exon" else f,
    )
    feats = list(it)
    assert all(f.featuretype != "exon" for f in feats)


def test_data_iterator_transform_returns_modified_feature():
    def bump(f):
        f.source = "modified"
        return f
    feats = list(DataIterator(str(DATA / "simple.gff3"), transform=bump))
    assert all(f.source == "modified" for f in feats)


# ---------------------------------------------------------------------------
# GFFWriter
# ---------------------------------------------------------------------------


def test_gffwriter_writes_header_and_records(tmp_path):
    out = tmp_path / "out.gff3"
    with GFFWriter(str(out)) as w:
        w.write_rec("chr1\tsrc\texon\t1\t10\t.\t+\t.\tID=x")
    text = out.read_text()
    assert text.startswith("##gff-version 3\n")
    assert "chr1\tsrc\texon" in text


def test_gffwriter_no_header(tmp_path):
    out = tmp_path / "out.gff3"
    with GFFWriter(str(out), with_header=False) as w:
        w.write_rec("chr1\tsrc\texon\t1\t10\t.\t+\t.\tID=x")
    assert not out.read_text().startswith("##gff-version")


def test_gffwriter_write_recs(tmp_path):
    out = tmp_path / "out.gff3"
    feats = [
        "chr1\tsrc\texon\t1\t10\t.\t+\t.\tID=a",
        "chr1\tsrc\texon\t11\t20\t.\t+\t.\tID=b",
    ]
    with GFFWriter(str(out)) as w:
        w.write_recs(feats)
    lines = out.read_text().splitlines()
    assert lines[0].startswith("##")
    assert "ID=a" in lines[1]
    assert "ID=b" in lines[2]


def test_gffwriter_write_gene_recs(tmp_path):
    db = create_db(str(DATA / "hierarchy.gff3"), ":memory:")
    out = tmp_path / "gene.gff3"
    with GFFWriter(str(out)) as w:
        w.write_gene_recs(db, "g1")
    text = out.read_text()
    # Gene + 2 mRNAs + 3 exons + 2 CDSs = 8 features printed (plus header)
    assert text.count("\n") >= 8
    assert "g1" in text


def test_gffwriter_write_mRNA_children(tmp_path):
    db = create_db(str(DATA / "hierarchy.gff3"), ":memory:")
    out = tmp_path / "mrna.gff3"
    with GFFWriter(str(out)) as w:
        w.write_mRNA_children(db, "t1")
    lines = out.read_text().splitlines()
    # mRNA + 4 direct children (e1, e2, c1, c2)
    assert sum(1 for l in lines if not l.startswith("#")) == 5


def test_gffwriter_in_place_atomic_swap(tmp_path):
    out = tmp_path / "swap.gff3"
    out.write_text("placeholder\n")
    with GFFWriter(str(out), with_header=False, in_place=True) as w:
        w.write_rec("chr1\tsrc\texon\t1\t10\t.\t+\t.\tID=x")
    text = out.read_text()
    assert "placeholder" not in text
    assert "ID=x" in text


def test_gffwriter_close_idempotent(tmp_path):
    out = tmp_path / "close.gff3"
    w = GFFWriter(str(out), with_header=False)
    w.write_rec("x")
    w.close()
    w.close()  # second call must not raise
