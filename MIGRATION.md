# Migrating from `gffutils` to `gffbase`

GFFBase is a drop-in successor to legacy
[`gffutils`](https://github.com/daler/gffutils). For most users, the
migration is one import change.

> ## ⚠️  READ THIS FIRST — loops over your own list of ids
>
> **Loops over a gffbase iterator are fast; loops over a list of ids you
> built yourself are not.** Since 0.3.0 a `children()`, `parents()` or
> `db[id]` call on a feature an open iterator holds is prefetched with its
> neighbours, so the canonical loop runs within 1.4–3.5× of gffutils' speed:
>
> ```python
> # ✅ Prefetched: the transcripts come from a gffbase iterator.
> for gene in db.features_of_type("gene"):
>     for transcript in db.children(gene, level=1):
>         exons = list(db.children(transcript, featuretype="exon"))
> ```
>
> A call on an id from your own list has no stream to prefetch from, so it
> is one DuckDB query -- about 1.5 ms, where SQLite answers in 0.05 ms:
>
> ```python
> # ❌ 50 000 separate queries: minutes on a whole genome.
> for transcript_id in fifty_thousand_transcript_ids:
>     for exon in db.children(transcript_id, featuretype="exon"):
>         starts.append(exon.start)
>         ends.append(exon.end)
> ```
>
> DuckDB is an **OLAP** engine, built for set-based queries; a single
> statement costs a fixed ~0.2 ms before it reads a row. SQLite (legacy
> gffutils) is **OLTP** -- its B-tree seek on a cache-warm file is
> microseconds.
>
> ### ✅ The fix — one canonical PyArrow snippet
>
> ```python
> # ✅ ONE set-based SQL query for all 50 000 transcripts.
> # Returns a zero-copy pyarrow.Table — no `Feature` object is ever
> # constructed — one set-based query instead of N.
> exons = db.children_batched(
>     fifty_thousand_transcript_ids,
>     featuretype="exon",
>     format="arrow",         # or "df" / "polars"
> )
>
> # NumPy / PyTorch / JAX / Hugging Face datasets — all native.
> starts = exons.column("start").to_numpy()
> ends   = exons.column("end").to_numpy()
>
> # The "anchor" column carries the input transcript_id for each row,
> # so you can groupby in Python or downstream Arrow tooling without
> # re-issuing N queries:
> import pyarrow.compute as pc
> per_tx_exon_count = pc.value_counts(exons.column("anchor"))
> ```
>
> If your code has a `for x in ids: db.children(x, …)` loop over ids that
> did not come from a gffbase iterator, and you care about wall time,
> **convert it now**. It is the only change required for *performance*; §6
> lists the behaviour changes that may require one for *correctness*.

---

## 1. Drop-in compatibility — the easy part

Every public surface from legacy `gffutils` is preserved verbatim:

| `gffutils` symbol | `gffbase` equivalent |
|---|---|
| `gffutils.create_db(path, dbfn, ...)` | `gffbase.create_db(path, dbfn, ...)` |
| `gffutils.FeatureDB(dbfn)` | `gffbase.FeatureDB(dbfn)` |
| `gffutils.Feature(...)` | `gffbase.Feature(...)` |
| `gffutils.DataIterator(...)` | `gffbase.DataIterator(...)` |
| `gffutils.GFFWriter(...)` | `gffbase.GFFWriter(...)` |
| `gffutils.merge_criteria.*` | `gffbase.merge_criteria.*` |
| `gffutils.example_filename(name)` | `gffbase.example_filename(name)` |
| Exceptions (`FeatureNotFoundError`, …) | same names |

<!-- docs-test: skip reason="illustrative: needs a real annotation file and the gffutils package" -->
```python
# Before
import gffutils
db = gffutils.create_db("annotation.gff3", "annotation.db")

# After
import gffbase as gffutils      # one-line alias migration
db = gffutils.create_db("annotation.gff3", "annotation.duckdb")
```

### The one addition worth making straight away: close the handle

`gffutils` uses SQLite, which hands out shared connections and never locks a
reader out. gffbase uses DuckDB, which takes an **exclusive lock on the
database file for the life of a writable handle**. Ported code that opens a
database and never closes it will work — right up until something else needs
that file:

<!-- docs-test: skip reason="illustrative: names annotation.gff3, which the reader supplies" -->
```python
from gffbase import FeatureDB, create_db

# Best: scope it.
with create_db("annotation.gff3", "annotation.duckdb", force=True) as db:
    ...

with FeatureDB("annotation.duckdb") as db:
    ...

# Or close it yourself.
db = FeatureDB("annotation.duckdb")
try:
    ...
finally:
    db.close()
```

Two symptoms tell you the lock is the problem: another process cannot open the
database, and on Windows the file cannot be deleted or replaced.

If you fan work out across processes — a PyTorch `DataLoader` with
`num_workers > 1`, or a `multiprocessing.Pool` — open each worker's handle
**read-only**, which takes no exclusive lock and so allows any number of
concurrent readers:

<!-- docs-test: skip reason="illustrative: names annotation.duckdb, which the reader supplies" -->
```python
with FeatureDB("annotation.duckdb", read_only=True) as db:
    ...
```

Full detail: [Connections & concurrency](https://khchao.com/gffbase/content/connections.html).

All `FeatureDB` methods (`children`, `parents`, `region`,
`features_of_type`, `interfeatures`, `merge`, `bed12`, `update`,
`delete`, `add_relation`, `execute`, …) accept the same arguments and
return generators of `Feature` objects — identical to the legacy API.

The **storage backend** changes (DuckDB instead of SQLite). This is
transparent for almost all callers, but raw SQL queries that hit the
legacy schema directly via `db.execute(...)` need rewriting against
the GFFBase schema (or against the SQLite-compat views; see §4). We
also ship `gffbase.export_sqlite(con, path)` to dump a GFFBase
database into a legacy `.sqlite` file when you need the old format.

---

## 2. What you gain immediately, no code changes

Head-to-head against legacy `gffutils` across the five canonical human-genome
annotation releases:

**Ingest is faster on every corpus, 1.92× to 3.62×,** into a database 0.61× to 0.89×
the size of the SQLite one. On top of that you gain the spatial index, batched
extraction, and SQL over the whole corpus — and no ratio is published at all
unless both engines' correctness signatures agree. The GTF row is the
inference-disabled arm, the configuration least favourable to gffbase.
`peak RSS` is ingest **plus exhaustive validation**; `validate_db` defaults
to `sample=200` and the CLI never overrides it.

<!-- BEGIN GENERATED: corpus-table -->
| Corpus | Format | Lines | gffbase ingest | legacy ingest | speedup | peak RSS (ingest + full validation) | spatial qps | batched (5 k anchors) |
| --- | :--: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| **GENCODE v49** (basic) | GTF | 6,068,892 | **3 min 36 s** | 6 min 54 s | **1.92×** | 53.47 GB | **728** ±1% (n=5) | 614 ms / 1.93 M desc |
| **GENCODE v49** (basic) | GFF3 | 6,066,054 | **3 min 13 s** | 9 min 45 s | **3.03×** | 61.93 GB | **770** ±1% (n=5) | 666 ms / 1.93 M desc |
| **RefSeq GRCh38.p14** | GFF3 | 4,932,571 | **2 min 5 s** | 6 min 31 s | **3.13×** | 26.69 GB | **588** ±1% (n=5) | 443 ms / 999 k desc |
| **CHESS 3.1.3** | GFF3 | 2,761,061 | **37.0 s** | 2 min 14 s | **3.62×** | 2.93 GB | **678** ±3% (n=5) | 155 ms / 161 k desc |
| **MANE v1.5** (Ensembl) | GFF3 | 524,834 | **15.6 s** | 45.1 s | **2.89×** | 4.00 GB | **890** ±0% (n=5) | 136 ms / 156 k desc |
<!-- END GENERATED: corpus-table -->

<!-- BEGIN GENERATED: benchmark-provenance -->
**Measured on** AMD EPYC 7702 64-Core Processor · 128 cores · 1007.22 GB RAM · Linux-5.14.0-503.15.1.el9_5.x86_64-x86_64-with-glibc2.34  
**Versions:** Python 3.11.16 · gffbase 0.3.0 · duckdb 1.5.5 · pyarrow 25.0.1 · gffutils 0.14  
**Commit:** `632a4d80dee0` · **Run:** 2026-09-27T15:07:53Z  
*Generated from `benchmarks/results/06_mega.linux-x86_64.json` by `tools/gen_benchmark_tables.py`. Do not edit by hand.*
<!-- END GENERATED: benchmark-provenance -->

“Censored at” marks a legacy run killed at its safety valve without finishing;
it supplies neither a completed wall nor a speedup. Method and fairness
constraints: [Methodology](https://khchao.com/gffbase/content/methodology.html).

| Workload (MANE v1.5, per call) | gffbase 0.3.0 | gffbase 0.2.1 | `gffutils` 0.14 |
|---|---|---|---|
| `db.region(seqid, start, end)`, 10 kb window | ~1.5 ms | ~2 ms | ~3.5 ms |
| `for g in db.features_of_type("gene"): db.children(g, level=1)` | ~0.15 ms | ~5 ms | ~0.06 ms |
| `... for t in db.children(g, level=1): db.children(t, featuretype="exon")` | ~0.39 ms | ~10 ms | ~0.27 ms |
| `for e in db.features_of_type("exon"): db.parents(e, featuretype="gene")` | ~0.08 ms | ~5 ms | ~0.05 ms |
| `db.children(id, level=1)` on an id from your own list | ~1.3 ms | ~5 ms | ~0.05 ms |
| `db[id]` on an id from your own list | ~0.5 ms | ~0.6 ms | ~0.03 ms |
| `db.children_batched(ids, format="arrow")` | one query for all ids, no Python `Feature` objects | same | not available |

Loops over a gffbase iterator are prefetched (rows 2-4) and run within
1.4–3.5× of gffutils; a call on an id from your own list is a DuckDB query of its own,
about ten to thirty times slower than SQLite's point lookup -- bring those
loops into one query with `children_batched`, `region_batched` or SQL through
`db.execute`, and the comparison reverses. Measured with
`benchmarks/08_loops.py` (loops, answers checked against gffutils) and
`benchmarks/07_profile.py` (single calls); see the
[tuning](https://khchao.com/gffbase/content/tuning.html) page.

Your existing `gffutils` script gets the spatial and whole-table query wins
the moment you swap the import; its loops over gffbase iterators stay within a
few times their speed, and its loops over id lists of its own get slower. To turn those
into the batched-extraction win, see the warning at the top of this page.

---

## 3. ⚠️  Deep-dive: the OLAP vs OLTP tradeoff

DuckDB is an **OLAP** engine. It's optimized for big set-based queries
(JOINs, aggregations, scans of millions of rows). SQLite is an **OLTP**
engine — optimized for tiny indexed point lookups against cache-warm
pages. **For tiny, repeated point queries against a cache-warm DB,
SQLite (and therefore legacy gffutils) is faster.**

The fix is the canonical PyArrow snippet at the top of this page. At the scale
of tens of thousands of anchors the row-by-row gffbase loop is the slowest
option available and the batched call is the fastest, by a wide margin in both
directions — because the batched call issues one set-based query and never
constructs a Python `Feature`. Current measurements:
[Performance](https://khchao.com/gffbase/content/performance.html).

### Vectorized methods at a glance

| Vectorized method | Replaces this loop |
|---|---|
| `db.children_batched(ids, level=…, featuretype=…, format='arrow')` | `for x in ids: db.children(x, …)` |
| `db.parents_batched(ids, …, format='arrow')` | `for x in ids: db.parents(x, …)` |
| `db.region_batched(regions, …, format='arrow')` | `for r in regions: db.region(r, …)` |

`format` accepts `"arrow"` (default — `pyarrow.Table`), `"df"`
(`pandas.DataFrame`), or `"polars"` (`polars.DataFrame`). All three
share memory with DuckDB's query buffers — no per-row Python
materialization happens at any layer.

### When you don't need to migrate the pattern

- One-off scripts that ask `db[gene_id]` or `db.children(gene)` for
  fewer than ~100 anchors.
- Small annotations (< 100 k features) where SQL startup overhead is
  not visible.

For everything else — ML feature extraction, BED12 export of every
transcript, "for each peak in this 50 000-row BED file find every
overlapping CDS" — switch to `*_batched`.

---

## 4. SQL-compat views (for raw `execute()` users)

Legacy code that did `db.execute("SELECT * FROM features WHERE …")`
hits the new DuckDB schema (`features`, `attributes`, `edges`,
`closure`). Two compatibility views provide the legacy column shapes:

```sql
-- features_compat: legacy SQLite-style 12-column features table.
SELECT * FROM features_compat WHERE seqid = 'chr1' LIMIT 5;

-- relations_compat: legacy parent/child/level table.
SELECT parent, child, level FROM relations_compat WHERE level = 1;
```

The `attributes` column on `features_compat` is the **raw col-9
bytes** (UTF-8), not legacy-style JSON. If your raw-SQL code parses
JSON out of that column, switch to querying the normalized
`attributes` table directly:

```sql
SELECT a.value FROM attributes a
WHERE a.feature_id = ? AND a.key = 'gene_biotype';
```

As one query this is fast: DuckDB scans the `key` and `value` columns with
the filter pushed down, and no JSON is parsed.

---

## 5. SQLite export — the safety valve

If a downstream tool only knows how to read legacy
`gffutils`-compatible SQLite files:

```python
from gffbase import export_sqlite
export_sqlite(db.conn, "legacy_compatible.sqlite")
```

Produces a SQLite database with the original gffutils schema,
populated UCSC `bin` column, and the closure flattened back into
`relations(parent, child, level)`. The downstream tool can open this
file with `gffutils.FeatureDB("legacy_compatible.sqlite")`.

---

## 6. Things that changed (small list)

- **Storage backend**: SQLite → DuckDB. Database file extension is
  `.duckdb` by convention. The legacy SQLite layout is reachable via
  `export_sqlite()` (above) or the compat views.
- **Disk size**: GFFBase databases are 0.61× to 0.89× the size of legacy SQLite
  across the benchmark corpora, though they also hold a materialized transitive
  closure and an R-tree -- which is what lets a hierarchy walk run as one query
  with no recursion and a spatial query use a real spatial index.
  Current measurements: [Performance](https://khchao.com/gffbase/content/performance.html).
- **Peak RSS**: higher -- ingest alone peaks at 1.0 GiB (MANE) to 2.8 GiB (GENCODE GFF3) on the benchmark
  corpora, under a 512 MB DuckDB budget raised only for the steps that need
  more, against 107–190 MB for `gffutils`, which writes through to SQLite.
  The published tables show 2.9–61.9 GB because those runs also validate
  exhaustively; `validate_db` defaults to `sample=200`, so no default path
  pays that. To hold DuckDB to a fixed limit instead, pass
  `pragmas={"memory_limit": ...}` (see [tuning](https://khchao.com/gffbase/content/tuning.html)).
- **Hierarchy depth**: GFFBase materializes the closure to depth 8 by
  default (vs depth 2 in legacy). Anything past 8 falls through to a
  dynamic recursive CTE — the dispatcher is automatic.
- **Attributes column shape**: in raw SQL, the legacy single-cell
  JSON blob is replaced by a normalized
  `attributes(feature_id, key, value, idx, seg_idx, ord)` long-form table.
  Filtering by attribute is now an indexed query, not a full scan.
- **Duplicate IDs**: NCBI RefSeq emits multiple GFF3 rows that share
  `ID=cds-NP_xxx`. Under the default `mode="compat"` gffbase renames the
  repeats as `gffutils.merge_strategy="create_unique"` would and records the
  remap in `duplicates`. Under `mode="strict"` it instead **fuses them into
  one discontinuous feature** — see below.

### Behaviour changes that can change your results

These are the ones worth reading before a production run. Each is small in
isolation; each can change what your script computes.

- **`merge_all` now persists.** It always documented that "the resulting
  records are added to the database", and did not. It also returned every
  input feature rather than only genuine merges, and accepted
  `exclude_components` while ignoring it. All three are fixed, so a script
  that called `merge_all` expecting a read-only generator now **writes to the
  database** and gets back a shorter list.
- **`merge_criteria.overlap_*_threshold` changed meaning.** They were distance
  tests (`abs(acc.end - cur.start) <= threshold`) and are now range tests, so
  a feature lying entirely inside the accumulator merges where it did not
  before. If you pass one of these to `merge` or `merge_all`, the set of
  features that merge has changed.
- **`create_introns` computes per transcript.** It treated
  `grandparent_featuretype="gene"` as the direct anchor and pooled every
  isoform's exons into one list, so for a multi-isoform gene the "introns"
  spanned transcript boundaries. On `FBgn0031208.gff` that was 1 where the
  oracle finds 3.
- **Splice sites are 2 bp**, strand-aware (`five_prime_cis_splice_site` /
  `three_prime_cis_splice_site`), and carry the intron's merged attributes.
  They were 1 bp, always typed `splice_site`, and attribute-less.
- **`bed12` output changed**: no trailing comma on `blockSizes`/`blockStarts`,
  `thin_featuretype` is honoured rather than ignored, and a feature with no
  CDS is now marked entirely *thick* rather than entirely thin.
- **Attribute values are percent-encoded on write.** Reading
  `feature.attributes` and re-serializing used to drop the escaping, which
  could emit structurally invalid GFF3 when a value contained `;` or `,`. If
  you diff gffbase output against gffutils output you will now see them agree
  where they previously did not. Note that space and non-ASCII are
  deliberately *not* encoded, per the specification.
- **Raw SQL against `features` returns ENVELOPE coordinates** for a
  discontinuous feature — `MIN(start)`, `MAX(end)` over its segments, not the
  coordinates of any one line. The `segments_all` view gives one row per
  physical input line, which is what a line-oriented consumer wants.
- **Coordinates can be NULL.** A GFF row may carry `.` in columns 4 and 5, and
  gffbase preserves that rather than coercing to 0. Such features are not in
  coordinate space and are skipped by `region()` and the derived-feature
  methods.
- **`type(f) is Feature` is no longer universally true.** A fused feature is a
  `MultipartFeature`, which subclasses `Feature` and overrides none of the
  compatibility surface. `isinstance` still holds.
- **Derived features carry a mode-dependent `source`.** `gffutils_derived`
  under `mode="compat"`, `gffbase_derived` under `mode="strict"`. If you
  filter on that string, `db.derived_source` gives you the right one.

### Command line

`gffutils-cli` becomes `gffbase`, with the same argument names. Seven of its
commands work there; eleven work here. See [the CLI reference](https://khchao.com/gffbase/content/cli.html) for
the mapping, including the four upstream commands that raise on every
invocation.

---

## 7. Migration checklist

- [ ] `pip install gffbase`
- [ ] Replace `import gffutils` with `import gffbase as gffutils` (or
      use the new name directly).
- [ ] Re-ingest your annotations (`create_db`) — old `.sqlite` files
      can still be read by legacy gffutils; they're not GFFBase
      databases.
- [ ] **Audit your code for `for x in ids: db.children(x, …)` loops
      and convert them to `db.children_batched(ids, format='arrow')`.**
      This is the only common change that requires user action.
- [ ] If you have raw `db.execute(...)` SQL: use
      `features_compat` / `relations_compat` views, or move attribute
      filters onto the normalized `attributes` table.
- [ ] Run your existing test suite. Everything else should be
      identical.

If anything breaks, please open an issue at
<https://github.com/Kuanhao-Chao/gffbase/issues> with a minimal
reproducer.
