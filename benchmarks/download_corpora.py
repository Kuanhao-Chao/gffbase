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
"""Download the canonical human-genome annotation corpora used by
``benchmarks/06_mega.py``.

Files land in ``benchmarks/data/``:

  * ``gencode.v49.chr_patch_hapl_scaff.basic.annotation.gtf.gz``  —
    GENCODE GRCh38 v49 basic, GTF format (no explicit parents — gene
    and transcript rows are *implicit*, defined only by the
    aggregation of their child exons).
  * ``gencode.v49.chr_patch_hapl_scaff.basic.annotation.gff3.gz`` —
    GENCODE GRCh38 v49 basic, GFF3 format (explicit Parent= columns).
    The GTF / GFF3 pair is the head-to-head fixture for the GTF
    Synthesis Advantage analysis.
  * ``GCF_000001405.40_GRCh38.p14_genomic.gff.gz`` — RefSeq GRCh38 p14.
  * ``MANE.GRCh38.v1.5.ensembl_genomic.gff.gz`` — MANE v1.5 (Ensembl IDs).
  * ``chess3.1.3.GRCh38.gff.gz`` — CHESS 3.1.3 (latest GitHub release).

Idempotent: an existing file is skipped (compare by size + suffix).
"""

from __future__ import annotations

import argparse
import os
import sys
import urllib.request
from pathlib import Path
from typing import Dict

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "benchmarks" / "data"

CORPORA: Dict[str, str] = {
    # Primary GENCODE pair: same biological release in both formats so
    # the GTF-vs-GFF3 head-to-head is apples-to-apples.
    "gencode.v49.chr_patch_hapl_scaff.basic.annotation.gtf.gz": (
        "https://ftp.ebi.ac.uk/pub/databases/gencode/Gencode_human/release_49/"
        "gencode.v49.chr_patch_hapl_scaff.basic.annotation.gtf.gz"
    ),
    "gencode.v49.chr_patch_hapl_scaff.basic.annotation.gff3.gz": (
        "https://ftp.ebi.ac.uk/pub/databases/gencode/Gencode_human/release_49/"
        "gencode.v49.chr_patch_hapl_scaff.basic.annotation.gff3.gz"
    ),
    "GCF_000001405.40_GRCh38.p14_genomic.gff.gz": (
        "https://ftp.ncbi.nlm.nih.gov/genomes/all/GCF/000/001/405/"
        "GCF_000001405.40_GRCh38.p14/"
        "GCF_000001405.40_GRCh38.p14_genomic.gff.gz"
    ),
    "MANE.GRCh38.v1.5.ensembl_genomic.gff.gz": (
        "https://ftp.ncbi.nlm.nih.gov/refseq/MANE/MANE_human/release_1.5/"
        "MANE.GRCh38.v1.5.ensembl_genomic.gff.gz"
    ),
    "chess3.1.3.GRCh38.gff.gz": (
        "https://github.com/chess-genome/chess/releases/download/v.3.1.3/"
        "chess3.1.3.GRCh38.gff.gz"
    ),
}


def _human(n: int) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024:
            return f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} TB"


def fetch(name: str, url: str, *, force: bool = False) -> Path:
    DATA.mkdir(parents=True, exist_ok=True)
    dst = DATA / name
    if dst.exists() and not force:
        sz = dst.stat().st_size
        print(f"[skip] {name} already present ({_human(sz)})", flush=True)
        return dst
    tmp = dst.with_suffix(dst.suffix + ".part")
    print(f"[fetch] {name} ← {url}", flush=True)
    req = urllib.request.Request(url, headers={"User-Agent": "gffbase-bench/0.1"})
    with urllib.request.urlopen(req) as resp, open(tmp, "wb") as fout:
        total = int(resp.headers.get("Content-Length") or 0)
        seen = 0
        while True:
            chunk = resp.read(1 << 16)
            if not chunk:
                break
            fout.write(chunk)
            seen += len(chunk)
            if total:
                pct = 100 * seen / total
                sys.stdout.write(
                    f"\r        {_human(seen)} / {_human(total)} ({pct:.1f}%)"
                )
                sys.stdout.flush()
        sys.stdout.write("\n")
    tmp.rename(dst)
    print(f"[ok]    {name} → {_human(dst.stat().st_size)}", flush=True)
    return dst


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--force", action="store_true", help="re-download even if present")
    ap.add_argument(
        "--only",
        choices=["gencode-gtf", "gencode-gff3", "gencode", "refseq", "mane", "chess"],
        action="append",
        help="restrict to one or more corpora (repeatable). "
             "`gencode` is shorthand for both `gencode-gtf` and `gencode-gff3`.",
    )
    args = ap.parse_args()

    selected = set(args.only or ["gencode", "refseq", "mane", "chess"])
    if "gencode" in selected:
        selected.update({"gencode-gtf", "gencode-gff3"})
        selected.discard("gencode")
    pick: Dict[str, str] = {}
    if "gencode-gtf" in selected:
        name = "gencode.v49.chr_patch_hapl_scaff.basic.annotation.gtf.gz"
        pick[name] = CORPORA[name]
    if "gencode-gff3" in selected:
        name = "gencode.v49.chr_patch_hapl_scaff.basic.annotation.gff3.gz"
        pick[name] = CORPORA[name]
    if "refseq" in selected:
        pick["GCF_000001405.40_GRCh38.p14_genomic.gff.gz"] = CORPORA[
            "GCF_000001405.40_GRCh38.p14_genomic.gff.gz"
        ]
    if "mane" in selected:
        pick["MANE.GRCh38.v1.5.ensembl_genomic.gff.gz"] = CORPORA[
            "MANE.GRCh38.v1.5.ensembl_genomic.gff.gz"
        ]
    if "chess" in selected:
        pick["chess3.1.3.GRCh38.gff.gz"] = CORPORA["chess3.1.3.GRCh38.gff.gz"]

    for name, url in pick.items():
        fetch(name, url, force=args.force)
    print("\nAll requested corpora present in:", DATA, flush=True)


if __name__ == "__main__":
    main()
