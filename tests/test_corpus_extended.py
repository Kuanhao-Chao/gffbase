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
"""One real file per annotation source the headline corpora do not cover.

`test_corpus.py` runs the five human annotations the benchmarks quote. Those
are large but alike: four GFF3 files from two producers and one GTF. The files
here are the conventions they miss -- Ensembl's `gene:`/`transcript:`
prefixes, an NCBI prokaryote, FlyBase, WormBase, UCSC's GTFs (no gene rows,
one transcript id on several contigs), and a Liftoff annotation (`_1` copies).

    python benchmarks/download_corpora.py --extended    # once, ~1.9 GB
    pytest -m corpus tests/test_corpus_extended.py

FlyBase, WormBase and Ensembl are sliced to whole sequences, so each test
stays in minutes; the full files are for `benchmarks/`, not the test suite.
"""

from __future__ import annotations

import gzip
from pathlib import Path

import pytest

pytestmark = pytest.mark.corpus

#: key -> (seqids kept, or None for the whole file; minimum feature count).
#: The slices are whole sequences of a few hundred thousand rows: FlyBase and
#: WormBase are 32 M and 53 M rows, mostly alignments.
PLAN: dict[str, tuple[tuple[str, ...] | None, int]] = {
    "ensembl-mouse-gff3": (("19",), 250_000),
    "ensembl-mouse-gtf": (("19",), 250_000),
    "ncbi-ecoli": (None, 9_000),
    "flybase": (("4",), 400_000),
    "wormbase-gff3": (("MtDNA",), 50_000),
    "wormbase-gtf": (None, 200_000),
    "ucsc-knowngene": (None, 1_000_000),
    "ucsc-ncbirefseq": (None, 1_000_000),
    "liftoff-chm13": (None, 1_000_000),
}


def _registry(key: str) -> dict:
    from benchmarks.corpora import BY_KEY

    return BY_KEY[key]


def _source(key: str) -> Path:
    from benchmarks.corpora import corpus_path

    path = corpus_path(_registry(key))
    if not path.is_file():
        pytest.skip(f"{path.name} not downloaded; run benchmarks/download_corpora.py --extended")
    return path


def _slice(src: Path, seqids: tuple[str, ...] | None, dst: Path) -> tuple[Path, int]:
    """Copy the header and every feature line on `seqids`; count the features.

    A whole sequence keeps every hierarchy intact, where a coordinate window
    would cut genes in half. Stops at `##FASTA`.
    """
    n = 0
    with gzip.open(src, "rt", encoding="utf-8", newline="") as fin, dst.open("w") as fout:
        for line in fin:
            if line.startswith("##FASTA"):
                break
            if line.startswith("#"):
                fout.write(line)
                continue
            if not line.strip():
                continue
            if seqids is None or line.split("\t", 1)[0] in seqids:
                fout.write(line)
                n += 1
    return dst, n


@pytest.fixture(scope="module", params=sorted(PLAN))
def built(request, tmp_path_factory):
    """(key, database, number of feature lines in the input). One ingest per
    corpus, shared by the checks below."""
    from gffbase import create_db

    key = request.param
    seqids, _ = PLAN[key]
    work = tmp_path_factory.mktemp(key)
    src, lines = _slice(_source(key), seqids, work / "input")
    # The compat reading of a repeated id -- every line its own feature -- so
    # the line count below is exact.
    db = create_db(str(src), str(work / "c.duckdb"), merge_strategy="create_unique")
    return key, db, lines


def test_it_ingests_and_validates(built):
    """Every invariant holds, and no edge names a feature the file lacks
    (INV-6 is a warning, since real files may reference other files; these
    do not)."""
    key, db, _ = built
    total = db.count_features_of_type()
    assert total >= PLAN[key][1], f"{key}: only {total} features"
    report = db.validate(level="full")
    assert not report.errors, f"{key}: {[str(v) for v in report.errors][:5]}"
    dangling = [str(v) for v in report.warnings if v.invariant == "INV-6"]
    assert not dangling, f"{key}: {dangling}"


def test_no_line_is_lost(built):
    """Every feature line is a feature; only inferred GTF parents are extra."""
    key, db, lines = built
    authored = db.execute("SELECT count(*) FROM features WHERE NOT is_synthetic").fetchone()[0]
    assert authored == lines, f"{key}: {lines} feature lines in, {authored} authored features"


def test_every_gtf_row_has_a_transcript(built):
    """A GTF exon/CDS that is a root was lost from its transcript -- the
    failure the CDS-only and missing-transcript_id fixes are about."""
    key, db, _ = built
    if _registry(key)["fmt"] != "gtf":
        pytest.skip("GFF3")
    orphans = db.execute(
        """
        SELECT count(*) FROM features f
        WHERE f.featuretype IN ('exon', 'CDS', 'start_codon', 'stop_codon')
          AND NOT EXISTS (SELECT 1 FROM edges e WHERE e.child = f.id)
        """
    ).fetchone()[0]
    assert orphans == 0, f"{key}: {orphans} GTF rows hang from no transcript"
