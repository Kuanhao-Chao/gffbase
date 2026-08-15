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
"""Whole-genome annotation corpora.

The vendored fixtures are small by design -- the largest is 33 KB -- so they
exercise correctness but not scale. These run the same operations against the
real human-genome annotations the README quotes performance numbers for:
GENCODE v49 (GTF and GFF3), RefSeq GRCh38.p14, MANE v1.5, CHESS 3.1.3.

Excluded from a normal run: several GB of downloads and minutes per corpus.

    python benchmarks/download_corpora.py     # once, ~257 MB
    pytest -m corpus

The `corpus` marker was declared in `pyproject.toml` and used by no test at
all until this file existed -- a marker nothing carries is indistinguishable
from a marker that was deleted.
"""

from __future__ import annotations

from pathlib import Path

import pytest

pytestmark = pytest.mark.corpus

CORPORA = Path(__file__).resolve().parent.parent / "benchmarks" / "data"

#: Name, and whether it is a GTF (which exercises the gene/transcript
#: synthesis path that GFF3 does not).
KNOWN = [
    ("gencode.v49.chr_patch_hapl_scaff.basic.annotation.gtf.gz", True),
    ("gencode.v49.chr_patch_hapl_scaff.basic.annotation.gff3.gz", False),
    ("GCF_000001405.40_GRCh38.p14_genomic.gff.gz", False),
    ("MANE.GRCh38.v1.5.ensembl_genomic.gff.gz", False),
    ("chess3.1.3.GRCh38.gff.gz", False),
]

#: Corpora that use the split-CDS convention: several lines sharing one
#: `ID=`, describing one discontinuous feature. `merge_strategy` defaults to
#: `"error"` -- gffutils' default, and gffutils raises on these files too --
#: so ingesting them at all requires saying which reading you want. That is a
#: property of the DATA, so it is recorded here next to the data rather than
#: worked around inside each test.
SPLIT_CDS = {
    "GCF_000001405.40_GRCh38.p14_genomic.gff.gz",
    "MANE.GRCh38.v1.5.ensembl_genomic.gff.gz",
    # GENCODE's GFF3 edition does it too -- `ID=CDS:ENST...` repeats across the
    # lines of a split CDS. Its GTF edition does not, because GTF carries no
    # CDS ids at all. Three of the five canonical corpora need a duplicate-ID
    # policy, which is worth knowing before anyone calls this an edge case.
    "gencode.v49.chr_patch_hapl_scaff.basic.annotation.gff3.gz",
}


def ingest_options(name: str) -> dict:
    """The options a corpus needs to ingest, and why.

    `create_unique` is the compat reading: each duplicate line becomes its own
    renamed feature, which is what a ported gffutils script expects to see.
    `mode="strict"` is the other reading -- one discontinuous feature -- and
    `test_split_cds_corpora_fuse_under_strict_mode` covers that separately.
    """
    return {"merge_strategy": "create_unique"} if name in SPLIT_CDS else {}


def corpus(name: str) -> str:
    path = CORPORA / name
    if not path.is_file():
        pytest.skip(f"{name} not downloaded; run python benchmarks/download_corpora.py")
    return str(path)


@pytest.fixture(scope="module")
def gencode_gff3(tmp_path_factory):
    """One ingest shared across the module -- it takes minutes."""
    from gffbase import create_db

    name = "gencode.v49.chr_patch_hapl_scaff.basic.annotation.gff3.gz"
    src = corpus(name)
    out = tmp_path_factory.mktemp("corpus") / "gencode.duckdb"
    return create_db(src, str(out), **ingest_options(name))


@pytest.mark.parametrize(("name", "is_gtf"), KNOWN, ids=[n.split(".")[0] for n, _ in KNOWN])
def test_every_corpus_ingests_and_validates(name, is_gtf, tmp_path):
    """Ingest, then check every invariant.

    The validator is the point: a corpus this size will contain feature shapes
    the fixtures do not, and INV-5 in particular catches an envelope narrower
    than its segments -- a fused feature that silently stops being returned by
    `region()`, with nothing raised anywhere.
    """
    from gffbase import create_db

    db = create_db(corpus(name), str(tmp_path / "c.duckdb"), **ingest_options(name))
    total = db.count_features_of_type()
    assert total > 100_000, f"{name}: only {total} features, expected a whole-genome file"

    report = db.validate(level="full")
    assert not report.errors, f"{name}: {[str(v) for v in report.errors]}"

    if is_gtf:
        # GTF has no gene/transcript rows of its own; they are synthesized.
        assert db.count_features_of_type("gene") > 10_000
        assert db.count_features_of_type("transcript") > 10_000


def test_region_agrees_across_both_index_paths(gencode_gff3):
    """The R-tree and the B-tree must return the same features.

    This is what the fixtures cannot test convincingly: the R-tree only earns
    its place at scale, and a per-seqid banding bug shows up as a disagreement
    on a real chromosome rather than on a 5-line file.
    """
    db = gencode_gff3
    if not db._rtree_built:
        pytest.skip("no R-tree on this database")

    for seqid, start, end in [
        ("chr1", 1_000_000, 1_100_000),
        ("chr7", 55_000_000, 55_500_000),
        ("chrX", 48_000_000, 48_100_000),
    ]:
        rtree = sorted(f.id for f in db.region(seqid=seqid, start=start, end=end))
        db._rtree_built = False
        try:
            btree = sorted(f.id for f in db.region(seqid=seqid, start=start, end=end))
        finally:
            db._rtree_built = True
        assert rtree == btree, f"{seqid}:{start}-{end}: index paths disagree"


def test_round_trip_is_byte_faithful(gencode_gff3):
    """Every line back out exactly as it went in.

    Serialization is where a corpus earns its keep: the fixtures cover the
    dialects someone thought to write down, and a real annotation covers the
    ones nobody did.
    """
    import gzip

    db = gencode_gff3
    src = corpus("gencode.v49.chr_patch_hapl_scaff.basic.annotation.gff3.gz")

    with gzip.open(src, "rt", encoding="utf-8") as fh:
        original = [line.rstrip("\n") for line in fh if line.strip() and not line.startswith("#")]

    emitted = []
    for feature in db.all_features():
        emitted.extend(feature.to_lines())

    assert len(emitted) == len(original), (
        f"{len(emitted)} lines out, {len(original)} in -- a discontinuous feature "
        f"was collapsed or a row invented"
    )
    mismatches = [(a, b) for a, b in zip(original, emitted, strict=True) if a != b]
    assert not mismatches, f"{len(mismatches)} lines differ; first: {mismatches[0]}"


def test_the_hierarchy_survives_a_whole_genome(gencode_gff3):
    """`children`/`parents` at a scale where the closure table matters."""
    db = gencode_gff3
    genes = [f.id for f in db.features_of_type("gene")][:200]
    assert genes

    for gene_id in genes:
        transcripts = list(db.children(gene_id, level=1))
        for transcript in transcripts:
            back = [p.id for p in db.parents(transcript.id, level=1)]
            assert gene_id in back, f"{transcript.id} lost its parent {gene_id}"


# ---------------------------------------------------------------------------
# The split-CDS convention, on the real files that use it
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("name", sorted(SPLIT_CDS))
def test_split_cds_corpora_raise_by_default(name, tmp_path):
    """The default must refuse, not guess.

    RefSeq and MANE repeat one `ID=` across the lines of a split CDS. There
    are two defensible readings -- several renamed features, or one
    discontinuous feature -- and `merge_strategy` defaults to `"error"`
    precisely so the caller picks. gffutils defaults the same way and raises
    on these same files, so this is parity, not strictness for its own sake.

    Pinned because the alternative failure mode is silent: a default that
    picked for you would give a whole-genome answer nobody chose.
    """
    from gffbase import DuplicateIDError, create_db

    with pytest.raises(DuplicateIDError):
        create_db(corpus(name), str(tmp_path / "default.duckdb"))


@pytest.mark.parametrize("name", sorted(SPLIT_CDS))
def test_split_cds_corpora_fuse_under_strict_mode(name, tmp_path):
    """`mode="strict"` reads the repeated ids as ONE discontinuous feature.

    This is the reading the GFF3 specification actually describes, and the
    one the `segments` table exists for. The fused database must hold strictly
    fewer features than the renamed one, contain multipart features, and still
    satisfy every invariant.
    """
    from gffbase import create_db

    fused = create_db(corpus(name), str(tmp_path / "strict.duckdb"), mode="strict")
    renamed = create_db(
        corpus(name), str(tmp_path / "compat.duckdb"), merge_strategy="create_unique"
    )

    n_multipart = fused.execute("SELECT COUNT(*) FROM features WHERE n_segments > 1").fetchone()[0]
    assert n_multipart > 0, f"{name}: strict mode fused nothing, so nothing was discontinuous"
    assert fused.count_features_of_type() < renamed.count_features_of_type(), (
        "fusing lines into one feature must reduce the feature count"
    )
    assert not fused.validate(level="full").errors
