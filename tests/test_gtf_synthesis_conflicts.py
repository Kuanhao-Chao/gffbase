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
"""GTF inference must never make one parent out of unrelated loci."""

from __future__ import annotations

from pathlib import Path

import pytest
from gffbase import DuplicateIDError, SynthesisConflictError, create_db

CONFLICTING_GTF = (
    'chr1\trs\texon\t100\t200\t.\t+\t.\tgene_id "G1"; transcript_id "T1";\n'
    'chr2\trs\texon\t300\t400\t.\t-\t.\tgene_id "G1"; transcript_id "T1";\n'
)


def _write(tmp_path: Path, text: str = CONFLICTING_GTF) -> Path:
    path = tmp_path / "conflict.gtf"
    path.write_text(text)
    return path


def test_the_public_error_preserves_duplicate_id_catch_compatibility():
    assert issubclass(SynthesisConflictError, DuplicateIDError)


@pytest.mark.parametrize("mode", ["compat", "strict"])
@pytest.mark.parametrize("strategy", ["error", "warning", "merge", "replace"])
def test_every_non_splitting_strategy_rejects_an_ambiguous_parent(tmp_path, mode, strategy):
    with pytest.raises(SynthesisConflictError, match=r"T1.*2 \(seqid, strand\) groups"):
        create_db(
            str(_write(tmp_path)),
            ":memory:",
            mode=mode,
            merge_strategy=strategy,
        )


def test_create_unique_builds_one_coherent_tree_per_location(tmp_path):
    db = create_db(
        str(_write(tmp_path)),
        ":memory:",
        mode="strict",
        merge_strategy="create_unique",
    )

    synthetic = db.conn.execute(
        'SELECT id, raw_id, featuretype, seqid, strand, start, "end" '
        "FROM features WHERE is_synthetic ORDER BY file_order, featuretype"
    ).fetchall()
    assert synthetic == [
        ("G1", "G1", "gene", "chr1", "+", 100, 200),
        ("T1", "T1", "transcript", "chr1", "+", 100, 200),
        ("G1_1", "G1", "gene", "chr2", "-", 300, 400),
        ("T1_1", "T1", "transcript", "chr2", "-", 300, 400),
    ]
    assert db.conn.execute("SELECT parent, child FROM edges ORDER BY 1, 2").fetchall() == [
        ("G1", "T1"),
        ("G1_1", "T1_1"),
        ("T1", "exon_1"),
        ("T1_1", "exon_2"),
    ]
    assert db.validate(level="full").ok


def test_the_first_source_group_keeps_the_raw_id(tmp_path):
    text = (
        'chr9\trs\texon\t900\t999\t.\t-\t.\tgene_id "G"; transcript_id "T";\n'
        'chr1\trs\texon\t100\t199\t.\t+\t.\tgene_id "G"; transcript_id "T";\n'
    )
    db = create_db(str(_write(tmp_path, text)), ":memory:", merge_strategy="create_unique")
    assert db.conn.execute(
        "SELECT id, seqid, strand FROM features "
        "WHERE featuretype = 'transcript' ORDER BY file_order"
    ).fetchall() == [("T", "chr9", "-"), ("T_1", "chr1", "+")]


AUTHORED_PARENT_GTF = (
    'chr1\trs\texon\t100\t200\t.\t+\t.\tgene_id "G1"; transcript_id "T1";\n'
    'chr2\trs\tgene\t250\t450\t.\t-\t.\tgene_id "G1";\n'
    'chr2\trs\ttranscript\t275\t425\t.\t-\t.\tgene_id "G1"; transcript_id "T1";\n'
    'chr2\trs\texon\t300\t400\t.\t-\t.\tgene_id "G1"; transcript_id "T1";\n'
)

SAME_LOCUS_AUTHORED_PARENT_GTF = (
    'chr1\trs\ttranscript\t100\t400\t.\t+\t.\tID "A"; transcript_id "T1";\n'
    'chr1\trs\ttranscript\t100\t400\t.\t+\t.\tID "B"; transcript_id "T1";\n'
    'chr1\trs\texon\t150\t200\t.\t+\t.\tID "E"; transcript_id "T1";\n'
)


@pytest.mark.parametrize("strategy", ["error", "create_unique"])
def test_same_locus_authored_parent_ids_are_never_routed_arbitrarily(tmp_path, strategy):
    with pytest.raises(SynthesisConflictError, match=r"T1.*2 authored.*ambiguous"):
        create_db(
            str(_write(tmp_path, SAME_LOCUS_AUTHORED_PARENT_GTF)),
            ":memory:",
            id_spec="ID",
            disable_infer_genes=True,
            merge_strategy=strategy,
        )


@pytest.mark.parametrize("strategy", ["error", "create_unique"])
def test_same_locus_authored_parent_conflict_leaves_no_partial_database(tmp_path, strategy):
    destination = tmp_path / "same-locus.duckdb"

    with pytest.raises(SynthesisConflictError, match=r"T1.*2 authored.*ambiguous"):
        create_db(
            str(_write(tmp_path, SAME_LOCUS_AUTHORED_PARENT_GTF)),
            str(destination),
            id_spec="ID",
            disable_infer_genes=True,
            merge_strategy=strategy,
        )

    assert not destination.exists()
    assert list(tmp_path.glob("same-locus.duckdb.gffbase-building.*")) == []


@pytest.mark.parametrize("mode", ["compat", "strict"])
def test_an_authored_parent_conflict_raises_by_default(tmp_path, mode):
    with pytest.raises(SynthesisConflictError, match=r"T1.*2 \(seqid, strand\) groups"):
        create_db(str(_write(tmp_path, AUTHORED_PARENT_GTF)), ":memory:", mode=mode)


@pytest.mark.parametrize("mode", ["compat", "strict"])
def test_authored_conflicts_are_rejected_even_when_synthesis_is_disabled(tmp_path, mode):
    with pytest.raises(SynthesisConflictError, match=r"T1.*2 \(seqid, strand\) groups"):
        create_db(
            str(_write(tmp_path, AUTHORED_PARENT_GTF)),
            ":memory:",
            mode=mode,
            disable_infer_genes=True,
            disable_infer_transcripts=True,
        )


def test_create_unique_cannot_claim_to_split_a_parent_when_synthesis_is_disabled(tmp_path):
    with pytest.raises(SynthesisConflictError, match="inference is disabled"):
        create_db(
            str(_write(tmp_path, AUTHORED_PARENT_GTF)),
            ":memory:",
            mode="strict",
            merge_strategy="create_unique",
            disable_infer_genes=True,
            disable_infer_transcripts=True,
        )


def test_an_authored_parent_group_keeps_the_raw_id_when_split(tmp_path):
    db = create_db(
        str(_write(tmp_path, AUTHORED_PARENT_GTF)),
        ":memory:",
        merge_strategy="create_unique",
    )

    assert db.conn.execute(
        "SELECT id, raw_id, featuretype, seqid, strand, is_synthetic "
        "FROM features WHERE featuretype IN ('gene', 'transcript') "
        "ORDER BY seqid, featuretype"
    ).fetchall() == [
        ("G1_1", "G1", "gene", "chr1", "+", True),
        ("T1_1", "T1", "transcript", "chr1", "+", True),
        ("G1", "G1", "gene", "chr2", "-", False),
        ("T1", "T1", "transcript", "chr2", "-", False),
    ]
    assert db.conn.execute("SELECT parent, child FROM edges ORDER BY 1, 2").fetchall() == [
        ("G1", "T1"),
        ("G1_1", "T1_1"),
        ("T1", "exon_2"),
        ("T1_1", "exon_1"),
    ]
    assert db.conn.execute(
        "SELECT raw_id, resolved_id, file_order FROM id_conflicts "
        "WHERE kind = 'gtf_synthesis_split' ORDER BY raw_id, resolved_id"
    ).fetchall() == [
        ("G1", "G1", 2),
        ("G1", "G1_1", 1),
        ("T1", "T1", 3),
        ("T1", "T1_1", 1),
    ]


def test_a_generated_suffix_steps_past_an_existing_feature_id(tmp_path):
    text = (
        'chr1\trs\texon\t100\t200\t.\t+\t.\tgene_id "G1"; transcript_id "T1"; exon_id "E1";\n'
        'chr2\trs\texon\t300\t400\t.\t-\t.\tgene_id "G1"; transcript_id "T1"; exon_id "T1_1";\n'
    )
    db = create_db(
        str(_write(tmp_path, text)),
        ":memory:",
        merge_strategy="create_unique",
        id_spec={
            "gene": "gene_id",
            "transcript": "transcript_id",
            "exon": "exon_id",
        },
    )
    transcripts = db.conn.execute(
        "SELECT id FROM features WHERE featuretype = 'transcript' ORDER BY file_order"
    ).fetchall()
    assert transcripts == [("T1",), ("T1_2",)]


def test_split_routing_does_not_rewrite_raw_child_attributes(tmp_path):
    db = create_db(str(_write(tmp_path)), ":memory:", merge_strategy="create_unique")
    rows = db.conn.execute(
        "SELECT f.id, f.attributes_blob, a.key, a.value "
        "FROM features f JOIN attributes a ON a.feature_id = f.id "
        "WHERE f.featuretype = 'exon' ORDER BY f.file_order, a.ord"
    ).fetchall()
    for _fid, blob, key, value in rows:
        raw = bytes(blob)
        assert b'gene_id "G1"' in raw
        assert b'transcript_id "T1"' in raw
        if key == "gene_id":
            assert value == "G1"
        if key == "transcript_id":
            assert value == "T1"


def test_every_split_is_recorded_in_conflict_provenance(tmp_path):
    db = create_db(str(_write(tmp_path)), ":memory:", merge_strategy="create_unique")
    rows = db.conn.execute(
        "SELECT raw_id, resolved_id, kind, file_order FROM id_conflicts "
        "WHERE kind = 'gtf_synthesis_split' ORDER BY raw_id, file_order"
    ).fetchall()
    assert rows == [
        ("G1", "G1", "gtf_synthesis_split", 1),
        ("G1", "G1_1", "gtf_synthesis_split", 2),
        ("T1", "T1", "gtf_synthesis_split", 1),
        ("T1", "T1_1", "gtf_synthesis_split", 2),
    ]


def test_an_on_disk_conflict_leaves_no_partial_database(tmp_path):
    destination = tmp_path / "out.duckdb"
    with pytest.raises(SynthesisConflictError):
        create_db(str(_write(tmp_path)), str(destination))
    assert not destination.exists()
    assert list(tmp_path.glob("out.duckdb.gffbase-building.*")) == []


def test_wrong_type_id_cannot_become_a_gtf_parent_when_inference_is_disabled(tmp_path):
    text = (
        'chr1\trs\texon\t100\t200\t.\t+\t.\tID "T1";\n'
        'chr1\trs\texon\t300\t400\t.\t+\t.\tID "E2"; transcript_id "T1";\n'
    )

    db = create_db(
        str(_write(tmp_path, text)),
        ":memory:",
        id_spec="ID",
        disable_infer_genes=True,
        disable_infer_transcripts=True,
    )

    assert db.conn.execute("SELECT parent, child FROM edges").fetchall() == []
    assert db.validate(level="full").ok


def test_authored_gtf_parent_attributes_route_to_the_resolved_own_ids(tmp_path):
    text = (
        'chr1\trs\tgene\t100\t400\t.\t+\t.\tID "ALPHA"; gene_id "G1";\n'
        'chr1\trs\ttranscript\t100\t400\t.\t+\t.\tID "BETA"; gene_id "G1"; '
        'transcript_id "T1";\n'
        'chr1\trs\texon\t150\t200\t.\t+\t.\tID "GAMMA"; gene_id "G1"; '
        'transcript_id "T1";\n'
    )

    db = create_db(
        str(_write(tmp_path, text)),
        ":memory:",
        id_spec="ID",
        disable_infer_genes=True,
        disable_infer_transcripts=True,
    )

    assert db.conn.execute("SELECT parent, child FROM edges ORDER BY 1, 2").fetchall() == [
        ("ALPHA", "BETA"),
        ("BETA", "GAMMA"),
    ]


def test_wrong_type_parent_id_collision_is_rejected_by_default(tmp_path):
    text = (
        'chr1\trs\texon\t100\t200\t.\t+\t.\tID "T1";\n'
        'chr1\trs\texon\t300\t400\t.\t+\t.\tID "E2"; transcript_id "T1";\n'
    )
    id_spec = {"gene": "gene_id", "transcript": "transcript_id", "exon": "ID"}

    with pytest.raises(SynthesisConflictError, match=r"transcript.*T1.*already used"):
        create_db(str(_write(tmp_path, text)), ":memory:", id_spec=id_spec)


def test_create_unique_suffixes_a_wrong_type_parent_collision_with_provenance(tmp_path):
    text = (
        'chr1\trs\texon\t100\t200\t.\t+\t.\tID "T1";\n'
        'chr1\trs\texon\t300\t400\t.\t+\t.\tID "E2"; transcript_id "T1";\n'
    )
    id_spec = {"gene": "gene_id", "transcript": "transcript_id", "exon": "ID"}

    db = create_db(
        str(_write(tmp_path, text)),
        ":memory:",
        id_spec=id_spec,
        merge_strategy="create_unique",
    )

    assert db.conn.execute(
        "SELECT id, raw_id, featuretype FROM features WHERE featuretype = 'transcript'"
    ).fetchall() == [("T1_1", "T1", "transcript")]
    assert db.conn.execute("SELECT parent, child FROM edges ORDER BY 1, 2").fetchall() == [
        ("T1_1", "E2")
    ]
    assert db.conn.execute(
        "SELECT raw_id, resolved_id, kind FROM id_conflicts WHERE kind = 'gtf_synthesis_split'"
    ).fetchall() == [("T1", "T1_1", "gtf_synthesis_split")]
