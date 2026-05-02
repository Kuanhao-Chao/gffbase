---
title: Performance Comparison
---

## Phase 19 highlight — the GFF3 ingestion gap, reversed

Phase 17's mega-bench surfaced an honest performance gap: while gffbase
crushed legacy gffutils on GTF ingest (17.83×), it was **0.46×–0.87×
*slower*** on the three GFF3 corpora (RefSeq, MANE, CHESS), where
legacy doesn't have to infer parent relationships and so its ingest
becomes effectively a streaming `INSERT`. Phase 19 profiled the
pipeline, found the offending stage (the post-hoc R-tree build was
**45 % of total ingest wall** on MANE — three full-table `UPDATE`
passes plus a tautological `WHERE bbox IS NULL OR bbox IS NOT NULL`
clause), and re-architected the ingest to stamp `seqid_y` and `bbox`
inline during the Arrow batch INSERT. Result:

| Corpus              | Phase 17 gffbase | **Phase 19 gffbase** | Self-improvement | **Phase 19 vs legacy** |
| ------------------- | ---------------: | -------------------: | ---------------: | ---------------------: |
| MANE v1.5           |          44.9 s  |          **21.6 s**  |       **2.08×**  |    **2.09× faster**    |
| RefSeq GRCh38.p14   |         468.6 s  |         **252.2 s**  |       **1.86×**  |    **1.45× faster**    |
| CHESS 3.1.3         |         196.7 s  |          **53.6 s**  |       **3.67×**  |    **2.48× faster**    |

The R-tree build stage alone went from **17.09 s → 0.11 s** (155×).
Full root-cause analysis + before/after profiling tables:
[`plans/PHASE19_PERF_OPTIMIZATION_SUMMARY.md`](https://github.com/your-org/gffbase/blob/main/plans/PHASE19_PERF_OPTIMIZATION_SUMMARY.md).

The `PERFORMANCE_COMPARISON.md` content below was authored at Phase
17 and still reflects the conservative numbers from that run. Where
the Big Four §0 table cites 1.15×–2.16× *legacy faster*, the
post-Phase-19 numbers above are the current state — gffbase is now
faster than legacy on **every** ingest matchup.

---

{%
   include-markdown "../PERFORMANCE_COMPARISON.md"
%}
