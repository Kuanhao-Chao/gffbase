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
"""`id_spec` reaches the gene and transcript rows GTF ingest infers.

A GTF file names no genes or transcripts; gffbase infers them by grouping on
`gene_id` and `transcript_id`. Those inferred rows used to be named after the
grouping key unconditionally, so a caller who asked for genes to be identified
by `gene_name` got `gene_id` anyway -- silently, since nothing about the result
looks wrong.

The default path is untouched and is not an approximation: naming an inferred
feature after the attribute it was grouped on IS what the default spec asks
for, and the output is identical to gffutils on all six GTF corpora. Only a
caller who asks for something else pays for the slower path.

Order is what makes this coherent. The rename runs AFTER `EDGES_FROM_GTF`,
because the edges are built by joining an exon's `transcript_id` against a
transcript's id -- rename first and that join stops finding anything. gffutils
renames without carrying the edges, which is why a custom `id_spec` there
returns disconnected genes and no transcript at all.
"""

from __future__ import annotations

import pytest
from gffbase import create_db
from gffbase._options import IngestOptions

GTF = (
    'chr1\trs\texon\t100\t200\t.\t+\t.\tgene_id "G1"; transcript_id "T1"; gene_name "ALPHA";\n'
    'chr1\trs\texon\t300\t400\t.\t+\t.\tgene_id "G1"; transcript_id "T1"; gene_name "ALPHA";\n'
)


def _build(tmp_path, **kwargs):
    src = tmp_path / "in.gtf"
    src.write_text(GTF)
    return create_db(str(src), ":memory:", merge_strategy="create_unique", **kwargs)


def _features(db):
    return sorted((f.id, f.featuretype) for f in db.all_features())


def _edges(db):
    return sorted(db.conn.execute("SELECT parent, child FROM edges").fetchall())


# ---------------------------------------------------------------------------
# The default, which must not move
# ---------------------------------------------------------------------------


def test_the_default_spec_names_inferred_rows_after_the_grouping_key(tmp_path):
    db = _build(tmp_path)
    assert _features(db) == [
        ("G1", "gene"),
        ("T1", "transcript"),
        ("exon_1", "exon"),
        ("exon_2", "exon"),
    ]
    assert _edges(db) == [("G1", "T1"), ("T1", "exon_1"), ("T1", "exon_2")]


def test_an_explicit_default_spec_is_still_the_fast_path(tmp_path):
    """Spelling out the default must not push the ingest onto the slow path,
    and must not change the answer."""
    explicit = _build(tmp_path, id_spec={"gene": "gene_id", "transcript": "transcript_id"})
    assert _features(explicit) == _features(_build(tmp_path))


@pytest.mark.parametrize(
    "spec,expected",
    [
        (None, False),
        ({"gene": "gene_id", "transcript": "transcript_id"}, False),
        ({"gene": "gene_name", "transcript": "transcript_id"}, True),
        ({"gene": "gene_id"}, True),
        ("ID", True),
    ],
)
def test_the_fast_path_predicate(spec, expected):
    """The pass is skipped entirely unless the spec asks for something other
    than what the synthesis SQL already produces."""
    assert IngestOptions(id_spec=spec).synthesized_ids_need_resolving("gtf") is expected


def test_gff3_never_takes_this_path():
    """GFF3 infers nothing, so there is nothing to rename."""
    assert IngestOptions(id_spec="Name").synthesized_ids_need_resolving("gff3") is False


# ---------------------------------------------------------------------------
# A custom spec
# ---------------------------------------------------------------------------


def test_a_custom_spec_names_the_inferred_gene(tmp_path):
    """The whole point: the caller asked for genes to be identified by
    `gene_name`, and the file says `ALPHA`."""
    db = _build(tmp_path, id_spec={"gene": "gene_name", "transcript": "transcript_id"})
    assert ("ALPHA", "gene") in _features(db)
    assert ("G1", "gene") not in _features(db)


def test_the_hierarchy_survives_the_rename(tmp_path):
    """The reason the pass runs after the edges are built. Renaming first would
    leave the exons' `transcript_id` pointing at a name no feature has, and the
    gene would come back childless."""
    db = _build(tmp_path, id_spec={"gene": "gene_name", "transcript": "transcript_id"})
    assert _edges(db) == [("ALPHA", "T1"), ("T1", "exon_1"), ("T1", "exon_2")]
    assert sorted(f.id for f in db.children("ALPHA")) == ["T1", "exon_1", "exon_2"]
    assert sorted(f.id for f in db.parents("exon_1")) == ["ALPHA", "T1"]


def test_the_attribute_is_carried_onto_the_inferred_row(tmp_path):
    """Without this the inferred gene has no `gene_name` at all, the spec finds
    nothing, and the id falls through to an autoincremented `gene_1` -- the
    spec applied in form, ignoring the name the caller asked for."""
    db = _build(tmp_path, id_spec={"gene": "gene_name", "transcript": "transcript_id"})
    assert db.conn.execute(
        "SELECT value FROM attributes WHERE feature_id = 'ALPHA' AND key = 'gene_name'"
    ).fetchall() == [("ALPHA",)]


def test_a_spec_naming_an_absent_attribute_autoincrements(tmp_path):
    """Nothing to carry across, so the resolver falls through -- the same rule
    it applies to a real row that lacks its id attribute."""
    db = _build(tmp_path, id_spec={"gene": "no_such_attribute", "transcript": "transcript_id"})
    genes = [fid for fid, ftype in _features(db) if ftype == "gene"]
    assert genes == ["gene_1"]
    assert sorted(f.id for f in db.children("gene_1")) == ["T1", "exon_1", "exon_2"]


def test_the_rename_is_recorded(tmp_path):
    db = _build(tmp_path, id_spec={"gene": "gene_name", "transcript": "transcript_id"})
    assert db.conn.execute(
        "SELECT raw_id, resolved_id FROM id_conflicts WHERE kind = 'synthesized_id_spec'"
    ).fetchall() == [("G1", "ALPHA")]


def test_a_rename_that_would_collide_is_stepped_past(tmp_path):
    """The renamed id has to be free: the features table has `id` as its
    primary key, and an exon may already have claimed the name."""
    src = tmp_path / "collide.gtf"
    src.write_text(
        'chr1\trs\texon\t100\t200\t.\t+\t.\tgene_id "G1"; transcript_id "T1"; gene_name "ALPHA";\n'
        'chr1\trs\texon\t300\t400\t.\t+\t.\tgene_id "G1"; transcript_id "T1"; gene_name "ALPHA";\n'
        'chr1\trs\tCDS\t100\t120\t.\t+\t0\tgene_id "G1"; transcript_id "T1"; exon_id "ALPHA";\n'
    )
    db = create_db(
        str(src),
        ":memory:",
        merge_strategy="create_unique",
        id_spec={"gene": "gene_name", "transcript": "transcript_id", "CDS": "exon_id"},
    )
    ids = [fid for fid, _ in _features(db)]
    assert len(ids) == len(set(ids)), f"duplicate ids: {ids}"
    assert "ALPHA" in ids


def test_the_database_still_validates_after_a_rename(tmp_path):
    """The rename touches features, attributes and edges; a missed one would
    leave a dangling reference."""
    db = _build(tmp_path, id_spec={"gene": "gene_name", "transcript": "transcript_id"})
    assert db.validate(level="full").ok


def test_nothing_is_recorded_when_the_spec_changes_nothing(tmp_path):
    db = _build(tmp_path)
    assert db.conn.execute(
        "SELECT COUNT(*) FROM id_conflicts WHERE kind = 'synthesized_id_spec'"
    ).fetchone() == (0,)
