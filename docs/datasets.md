---
title: Datasets
---

# Datasets

The annotation corpora gffbase is tested and benchmarked against, where to get
them, and what each one is useful for. None is bundled: they are large, they
belong to their publishers, and they update on their own schedules.

Everything here is fetched by one script:

```bash
python benchmarks/download_corpora.py            # all five, ~257 MB
python benchmarks/download_corpora.py --only mane --only chess   # just two
```

Downloads land in `benchmarks/data/`, are skipped if already present, and
write through a `.part` file so an interrupted download cannot leave a
truncated corpus that looks complete.

---

## At a glance

| Corpus | Format | Feature lines | Download | Fetch with |
| --- | :--: | ---: | ---: | --- |
| **GENCODE v49** basic | GTF | 6,068,892 | 67 MB | `--only gencode-gtf` |
| **GENCODE v49** basic | GFF3 | 6,066,054 | 85 MB | `--only gencode-gff3` |
| **RefSeq GRCh38.p14** | GFF3 | 4,932,571 | 75 MB | `--only refseq` |
| **CHESS 3.1.3** | GFF3 | 2,761,061 | 20 MB | `--only chess` |
| **MANE v1.5** (Ensembl IDs) | GFF3 | 524,834 | 10 MB | `--only mane` |

GENCODE ships the **same biological release in both GTF and GFF3**, which is
why both are here: it is the cleanest available measurement of what the
surface format costs, with everything else held constant. The GFF3 half is not
in the current published sweep — see the note on
[Performance](performance.md). Method:
[Methodology](performance/methodology.md).

---

## What each one is for

**GENCODE v49** — the reference human annotation, and the flagship ingest
benchmark. The GTF edition is the demanding case: GTF carries no explicit
parent rows, so every gene and transcript has to be *synthesized* from the
span of its children.

**RefSeq GRCh38.p14** — NCBI's annotation, with `Dbxref`, `Note` and `gbkey`
attributes and `NC_000001.11`-style sequence names. The stress test for
attribute handling.

**CHESS 3.1.3** — dense relations and custom attributes, across 316 sequences
including alt contigs and scaffolds.

**MANE v1.5** — one representative transcript per gene, with Ensembl IDs.
Small and tidy: the corpus to reach for when you want a real annotation that
ingests in twenty seconds.

!!! important "Three of the five need a duplicate-ID policy"
    **RefSeq, MANE and GENCODE's GFF3 edition** all use the split-CDS
    convention: one CDS spread over several lines that share an `ID`.
    `merge_strategy` defaults to `"error"` — as it does in `gffutils`, which
    raises on these same files — so you say which reading you want:

    ```python
    create_db(path, "out.duckdb", merge_strategy="create_unique")  # renamed rows
    create_db(path, "out.duckdb", mode="strict")                   # one discontinuous feature
    ```

    See [Compatibility & strict modes](guides/modes.md).

---

## Sources

Each is downloaded from its publisher, never re-hosted:

| Corpus | Source |
| --- | --- |
| GENCODE v49 | [`ftp.ebi.ac.uk`](https://ftp.ebi.ac.uk/pub/databases/gencode/Gencode_human/release_49/) — GENCODE, EMBL-EBI |
| RefSeq GRCh38.p14 | [`ftp.ncbi.nlm.nih.gov`](https://ftp.ncbi.nlm.nih.gov/genomes/all/GCF/000/001/405/) — NCBI |
| MANE v1.5 | [`ftp.ncbi.nlm.nih.gov`](https://ftp.ncbi.nlm.nih.gov/refseq/MANE/MANE_human/release_1.5/) — NCBI / EMBL-EBI |
| CHESS 3.1.3 | [`github.com/chess-genome/chess`](https://github.com/chess-genome/chess/releases) — Salzberg lab |

**Licensing.** Each corpus carries its publisher's terms, which gffbase does
not alter and cannot grant. Cite the annotation you used alongside gffbase —
see [Citation](citation.md).

---

## Test fixtures

Separately from the corpora above, the test suite uses small vendored
fixtures, committed to the repository:

| Fixture | What it is |
| --- | --- |
| `tests/data/*.gff3`, `*.gtf` | Hand-written files covering the hierarchy, GTF synthesis and coordinate edge cases |
| `tests/data/upstream/` | 32 files copied verbatim from the `gffutils` test corpus, with `PROVENANCE.md` recording the commit and licence |
| `tests/data/v1/` | A schema-v1 database, gzipped, for the migration tests |

These total under a megabyte and need no download, which is why the default
`pytest` run needs no network. The corpus tests are opt-in:

```bash
python benchmarks/download_corpora.py
pytest -m corpus
```

They ingest each real annotation and run the full structural validator over
it — the strongest end-to-end correctness evidence in the project.
