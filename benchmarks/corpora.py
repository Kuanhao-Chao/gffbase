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

BY_KEY = {str(corpus["key"]): corpus for corpus in CORPORA}
BY_FILENAME = {str(corpus["filename"]): corpus for corpus in CORPORA}


def corpus_path(corpus: dict[str, object]) -> Path:
    """Return the local path for a registry entry."""

    return DATA / str(corpus["filename"])
