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

import os
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
    # Directives are stored with the leading `##` stripped, matching
    # gffutils' `_directive_handler`. `db.directives` is a documented
    # attribute, so the stored form is part of the compatibility contract.
    assert any(d.startswith("gff-version") for d in it.directives)
    assert not any(d.startswith("#") for d in it.directives)


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
    assert sum(1 for line in lines if not line.startswith("#")) == 5


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


# ---------------------------------------------------------------------------
# `DataIterator` dispatches on its input
# ---------------------------------------------------------------------------
#
# The factory used to hand every input straight to `_DataIterator`, which
# calls `parse_gff(path)`. So a URL was opened as a filename and an in-memory
# feature list raised -- while `_UrlIterator` and `_FeatureIterator` sat
# unreachable underneath, their docstrings describing a dispatch that did not
# exist. gffutils' `DataIterator` accepts all of these.


def test_data_iterator_accepts_a_pathlib_path():
    feats = list(DataIterator(DATA / "simple.gff3"))
    assert feats and all(isinstance(f, Feature) for f in feats)


def test_data_iterator_accepts_an_in_memory_feature_list():
    """A list of features passes through untouched, so a generator can be
    piped into `create_db` / `FeatureDB.update` without a temporary file."""
    original = list(DataIterator(str(DATA / "simple.gff3")))
    assert original

    from gffbase.iterators import _FeatureIterator

    it = DataIterator(original)
    assert isinstance(it, _FeatureIterator)
    assert [f.featuretype for f in it] == [f.featuretype for f in original]
    # And the documented accessors are properties on this class too.
    assert not callable(it.dialect)
    assert not callable(it.directives)


def test_data_iterator_accepts_a_generator():
    original = list(DataIterator(str(DATA / "simple.gff3")))
    out = list(DataIterator(f for f in original))
    assert len(out) == len(original)


def test_transform_is_applied_to_in_memory_features():
    """`__iter__` returned the list's own iterator, bypassing `__next__` --
    so `transform`, which every other iterator here honours, was dropped."""
    original = list(DataIterator(str(DATA / "simple.gff3")))
    kinds = {f.featuretype for f in original}
    drop = sorted(kinds)[0]

    kept = list(
        DataIterator(original, transform=lambda f: False if f.featuretype == drop else None)
    )
    assert kept, "transform dropped everything; pick a different featuretype"
    assert all(f.featuretype != drop for f in kept)
    assert len(kept) < len(original)


def test_data_iterator_rejects_something_that_is_neither():
    with pytest.raises(TypeError, match="DataIterator accepts"):
        DataIterator(42)


def test_url_iterator_fetches_and_cleans_up_its_temporary_file(tmp_path):
    """The download used `NamedTemporaryFile(delete=False)` and never
    unlinked it, so every URL ingest leaked a full copy of the annotation."""
    import threading
    from functools import partial
    from http.server import HTTPServer, SimpleHTTPRequestHandler

    src = tmp_path / "served.gff3"
    src.write_text((DATA / "simple.gff3").read_text())

    handler = partial(SimpleHTTPRequestHandler, directory=str(tmp_path))
    server = HTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        from gffbase.iterators import _UrlIterator

        it = DataIterator(f"http://127.0.0.1:{server.server_port}/served.gff3")
        assert isinstance(it, _UrlIterator)
        feats = list(it)
        assert feats == list(DataIterator(str(src)))

        downloaded = it._tempfile
        assert downloaded and os.path.exists(downloaded)
        it.close()
        assert not os.path.exists(downloaded)
        it.close()  # idempotent
    finally:
        server.shutdown()
        server.server_close()
