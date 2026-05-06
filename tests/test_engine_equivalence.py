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
"""Differential test: the Rust and Python engines must produce identical
output for the same input. Skipped automatically if the Rust extension is
not built.
"""

from __future__ import annotations

import pytest

from gffbase import parse_gff, native_available


@pytest.mark.skipif(not native_available(), reason="Rust extension not built")
def test_engines_agree_on_gff3(gff3_path):
    py_feats = list(parse_gff(gff3_path, engine="python"))
    rs_feats = list(parse_gff(gff3_path, engine="rust"))
    assert len(py_feats) == len(rs_feats)
    for a, b in zip(py_feats, rs_feats):
        assert a.seqid == b.seqid
        assert a.source == b.source
        assert a.featuretype == b.featuretype
        assert a.start == b.start
        assert a.end == b.end
        assert a.strand == b.strand
        assert a.attributes_blob == b.attributes_blob
        assert a.attributes_pairs == b.attributes_pairs


@pytest.mark.skipif(not native_available(), reason="Rust extension not built")
def test_engines_agree_on_gtf(gtf_path):
    py_feats = list(parse_gff(gtf_path, engine="python"))
    rs_feats = list(parse_gff(gtf_path, engine="rust"))
    assert len(py_feats) == len(rs_feats)
    for a, b in zip(py_feats, rs_feats):
        assert a.attributes_pairs == b.attributes_pairs
