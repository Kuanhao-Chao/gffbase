---
title: Performance Comparison
---

## v0.1.0 highlight — the GFF3 ingestion gap, reversed

An earlier multi-corpus benchmark surfaced an honest performance gap: while gffbase
crushed legacy gffutils on GTF ingest, it was **0.46×–0.87× *slower***
on the three GFF3 corpora (RefSeq, MANE, CHESS), where
legacy doesn't have to infer parent relationships and so its ingest
becomes effectively a streaming `INSERT`. An autonomous optimization
audit profiled the pipeline, found the offending stage (the post-hoc
R-tree build was **45 % of total ingest wall** on MANE — three
full-table `UPDATE` passes plus a tautological
`WHERE bbox IS NULL OR bbox IS NOT NULL` clause), and re-architected
the ingest to stamp `seqid_y` and `bbox` inline during the Arrow batch
INSERT. Result:

| Corpus              | Before          | **After**         | Self-improvement | **vs legacy**          |
| ------------------- | --------------: | ----------------: | ---------------: | ---------------------: |
| MANE v1.5           |          44.9 s |       **21.6 s**  |       **2.08×**  |    **2.09× faster**    |
| RefSeq GRCh38.p14   |         468.6 s |      **252.2 s**  |       **1.86×**  |    **1.45× faster**    |
| CHESS 3.1.3         |         196.7 s |       **53.6 s**  |       **3.67×**  |    **2.48× faster**    |

The R-tree build stage alone went from **17.09 s → 0.11 s** (155×).
Full root-cause analysis + before/after profiling tables are kept in
the project's internal architecture-audit notes.

The detailed `PERFORMANCE_COMPARISON.md` content below was authored
before this optimization landed and still reflects the older
conservative numbers. Where the §0 headline table cites 1.15×–2.16×
*legacy faster* on raw GFF3 ingest, the numbers above are the current
state — gffbase is now faster than legacy on **every** ingest matchup.

---

{%
   include-markdown "../PERFORMANCE_COMPARISON.md"
%}
