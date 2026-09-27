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
"""Canonical benchmark corpus registry.

The filename, byte size, and SHA-256 digest are part of the benchmark
definition.  Keeping them in one module prevents the downloader, preflight,
and measurement harness from silently measuring different inputs.
"""

from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "benchmarks" / "data"


CORPORA: tuple[dict[str, object], ...] = (
    {
        "name": "MANE v1.5 (Ensembl IDs)",
        "key": "mane",
        "filename": "MANE.GRCh38.v1.5.ensembl_genomic.gff.gz",
        "fmt": "gff3",
        "bytes": 10_349_746,
        "sha256": "69089bbc84d1d3c3ce31c2ed3f85b6c3169fb8836d092a082623c59a43fd22ef",
        "url": (
            "https://ftp.ncbi.nlm.nih.gov/refseq/MANE/MANE_human/release_1.5/"
            "MANE.GRCh38.v1.5.ensembl_genomic.gff.gz"
        ),
    },
    {
        "name": "CHESS 3.1.3",
        "key": "chess",
        "filename": "chess3.1.3.GRCh38.gff.gz",
        "fmt": "gff3",
        "bytes": 20_435_645,
        "sha256": "28da847be976780fe38162a7c244749fdc7a0b446741ca8da2b64019c1606e03",
        "url": (
            "https://github.com/chess-genome/chess/releases/download/"
            "v.3.1.3/chess3.1.3.GRCh38.gff.gz"
        ),
    },
    {
        "name": "RefSeq GRCh38.p14",
        "key": "refseq",
        "filename": "GCF_000001405.40_GRCh38.p14_genomic.gff.gz",
        "fmt": "gff3",
        "bytes": 78_190_483,
        "sha256": "4920f0eae7e2197c50b67a201e06d657387137b49dd60f474b4f1d5b29334051",
        "url": (
            "https://ftp.ncbi.nlm.nih.gov/genomes/all/GCF/000/001/405/"
            "GCF_000001405.40_GRCh38.p14/"
            "GCF_000001405.40_GRCh38.p14_genomic.gff.gz"
        ),
    },
    {
        "name": "GENCODE v49 (GTF)",
        "key": "gencode-gtf",
        "filename": "gencode.v49.chr_patch_hapl_scaff.basic.annotation.gtf.gz",
        "fmt": "gtf",
        "bytes": 70_588_995,
        "sha256": "576dddae36169ad648afbe706535361309786e549ad7daf529cca7674fb0058f",
        "url": (
            "https://ftp.ebi.ac.uk/pub/databases/gencode/Gencode_human/release_49/"
            "gencode.v49.chr_patch_hapl_scaff.basic.annotation.gtf.gz"
        ),
    },
    {
        "name": "GENCODE v49 (GFF3)",
        "key": "gencode-gff3",
        "filename": "gencode.v49.chr_patch_hapl_scaff.basic.annotation.gff3.gz",
        "fmt": "gff3",
        "bytes": 89_385_177,
        "sha256": "22ffa691aac993603f7f21effacf19848262bec74545bd979863e7af602e5a1d",
        "url": (
            "https://ftp.ebi.ac.uk/pub/databases/gencode/Gencode_human/release_49/"
            "gencode.v49.chr_patch_hapl_scaff.basic.annotation.gff3.gz"
        ),
    },
)

#: Non-human sources and tool/browser dialects, for robustness rather than the
#: headline benchmark: each is a convention the five above do not exercise.
#: They live in `benchmarks/data/extended/` and are tested by
#: `tests/test_corpus_extended.py` (`pytest -m corpus`). `pinned` says whether
#: the URL names an immutable release; UCSC and NCBI replace some of these in
#: place, so a digest mismatch there means a new upstream build -- re-pin it.
EXTENDED_CORPORA: tuple[dict[str, object], ...] = (
    {
        "name": "Ensembl 116 mouse (GFF3)",
        "key": "ensembl-mouse-gff3",
        "filename": "Mus_musculus.GRCm39.116.gff3.gz",
        "fmt": "gff3",
        "bytes": 81_236_526,
        "sha256": "cece9cdee5cf260af0409c4cb4bc4f0bce5762a29d78ded6b879a1c57282c2de",
        "url": (
            "https://ftp.ensembl.org/pub/release-116/gff3/mus_musculus/"
            "Mus_musculus.GRCm39.116.gff3.gz"
        ),
        "pinned": True,
    },
    {
        "name": "Ensembl 116 mouse (GTF)",
        "key": "ensembl-mouse-gtf",
        "filename": "Mus_musculus.GRCm39.116.gtf.gz",
        "fmt": "gtf",
        "bytes": 107_856_522,
        "sha256": "5c29fd9e3157cf40fdbbf76ab25bfe7f79aa61313e0b672664ddb0cb251c02e1",
        "url": (
            "https://ftp.ensembl.org/pub/release-116/gtf/mus_musculus/"
            "Mus_musculus.GRCm39.116.gtf.gz"
        ),
        "pinned": True,
    },
    {
        "name": "NCBI RefSeq E. coli K-12 MG1655",
        "key": "ncbi-ecoli",
        "filename": "GCF_000005845.2_ASM584v2_genomic.gff.gz",
        "fmt": "gff3",
        "bytes": 387_627,
        "sha256": "afdf03dc1d06e423d874ee29d9e0df14f5d32baf893f4da9f93aa721eb5f495e",
        "url": (
            "https://ftp.ncbi.nlm.nih.gov/genomes/all/GCF/000/005/845/"
            "GCF_000005845.2_ASM584v2/GCF_000005845.2_ASM584v2_genomic.gff.gz"
        ),
        "pinned": False,
    },
    {
        "name": "FlyBase r6.69 (D. melanogaster)",
        "key": "flybase",
        "filename": "dmel-all-r6.69.gff.gz",
        "fmt": "gff3",
        "bytes": 824_217_523,
        "sha256": "8ea9571cffa69a52086bfa9630154fd8f6456786b953f3725c0d1ec036e72deb",
        "url": (
            "https://s3ftp.flybase.org/genomes/Drosophila_melanogaster/"
            "dmel_r6.69_FB2026_03/gff/dmel-all-r6.69.gff.gz"
        ),
        "pinned": True,
    },
    {
        "name": "WormBase WS298 (C. elegans, GFF3)",
        "key": "wormbase-gff3",
        "filename": "c_elegans.PRJNA13758.WS298.annotations.gff3.gz",
        "fmt": "gff3",
        "bytes": 778_503_806,
        "sha256": "4c84b835c5eba84d2128947ae5646329c0172e25c29b40b416b9bf7807146030",
        "url": (
            "https://ftp.ebi.ac.uk/pub/databases/wormbase/releases/WS298/species/"
            "c_elegans/PRJNA13758/c_elegans.PRJNA13758.WS298.annotations.gff3.gz"
        ),
        "pinned": True,
    },
    {
        "name": "WormBase WS298 canonical gene set (GTF)",
        "key": "wormbase-gtf",
        "filename": "c_elegans.PRJNA13758.WS298.canonical_geneset.gtf.gz",
        "fmt": "gtf",
        "bytes": 8_531_149,
        "sha256": "86678b0013c5bd92d7044a4c83eccee72238ce9ffd1799b1d547f08881009f81",
        "url": (
            "https://ftp.ebi.ac.uk/pub/databases/wormbase/releases/WS298/species/"
            "c_elegans/PRJNA13758/c_elegans.PRJNA13758.WS298.canonical_geneset.gtf.gz"
        ),
        "pinned": True,
    },
    {
        "name": "UCSC hg38 knownGene (GTF)",
        "key": "ucsc-knowngene",
        "filename": "hg38.knownGene.gtf.gz",
        "fmt": "gtf",
        "bytes": 38_959_957,
        "sha256": "148584b4054d01641ebe61b568fac717e91369eacc46d952457d9a3efd794c53",
        "url": "https://hgdownload.soe.ucsc.edu/goldenPath/hg38/bigZips/genes/hg38.knownGene.gtf.gz",
        "pinned": False,
    },
    {
        "name": "UCSC hg38 ncbiRefSeq (GTF)",
        "key": "ucsc-ncbirefseq",
        "filename": "hg38.ncbiRefSeq.gtf.gz",
        "fmt": "gtf",
        "bytes": 41_887_094,
        "sha256": "856919cfc5854079e70dd016048045092fd79b782aa8da9dbbd1c51a9046d8a4",
        "url": "https://hgdownload.soe.ucsc.edu/goldenPath/hg38/bigZips/genes/hg38.ncbiRefSeq.gtf.gz",
        "pinned": False,
    },
    {
        "name": "T2T CHM13v2.0 RefSeq Liftoff v5.2",
        "key": "liftoff-chm13",
        "filename": "chm13v2.0_RefSeq_Liftoff_v5.2.gff3.gz",
        "fmt": "gff3",
        "bytes": 56_368_414,
        "sha256": "a1c8e61cb4e60a3af3a18599b7d5551a72a1b0317bdffad42ae7fa36e73da968",
        "url": (
            "https://s3-us-west-2.amazonaws.com/human-pangenomics/T2T/CHM13/assemblies/"
            "annotation/chm13v2.0_RefSeq_Liftoff_v5.2.gff3.gz"
        ),
        "pinned": True,
    },
)

BY_KEY = {str(corpus["key"]): corpus for corpus in CORPORA + EXTENDED_CORPORA}
BY_FILENAME = {str(corpus["filename"]): corpus for corpus in CORPORA + EXTENDED_CORPORA}
_EXTENDED = {str(corpus["key"]) for corpus in EXTENDED_CORPORA}


def corpus_path(corpus: dict[str, object]) -> Path:
    """Return the local path for a registry entry."""

    if str(corpus["key"]) in _EXTENDED:
        return DATA / "extended" / str(corpus["filename"])
    return DATA / str(corpus["filename"])
