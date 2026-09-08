---
title: Usage Gallery
---

# Usage Gallery — every public method, copy-pasteable

This is the cheat sheet. Every snippet below is **runnable as-is**
against any GFFBase database. The cookbook pages walk through specific
biological corpora; this page covers the **API surface in isolation**
so you can grep for the method name you need and lift the snippet.

If you only read three sections, make them:

* [Database initialization](#1-database-initialization)
* [Vectorized ML API](#5-vectorized-ml-api-the-ones-that-make-gffbase-fast) — the zero-copy PyArrow path
* [Raw SQL escape hatch](#7-advanced-escape-hatches)

---

## 1. Database initialization

### 1.1 Create a database from a file (most common)

<!-- docs-test: skip reason="needs a real annotation download" -->
```python
from gffbase import create_db

# GTF or GFF3, plain or gzipped — auto-detected from contents.
db = create_db(
    "annotation.gff3.gz",     # input path
    "annotation.duckdb",      # output DB
    force=True,               # overwrite if it already exists
)
print(db.fmt)                 # 'gff3' or 'gtf'
print(db.count_features_of_type())   # total feature count
```

### 1.2 In-memory database (no disk artifact)

<!-- docs-test: skip reason="illustrative: names annotation.gff3, which the reader supplies" -->
```python
from gffbase import create_db

# Pass `:memory:` as the dbfn. The DB lives only for the lifetime
# of the FeatureDB object — useful for small files in scripts.
db = create_db("small_annotation.gff3", ":memory:")
```

### 1.3 Re-open an existing database

<!-- docs-test: skip reason="illustrative: names annotation.duckdb, which the reader supplies" -->
```python
from gffbase import FeatureDB

# Cheap: no re-ingest. Only the meta + index DDL is read at open.
db = FeatureDB("annotation.duckdb")
print(db.schema())            # 'gff3' / 'gtf' / etc.
print(list(db.seqids())[:5])  # first 5 chromosomes
```

### 1.4 Ingest from a string instead of a file

```python
from gffbase import create_db

text = (
    "##gff-version 3\n"
    "chr1\trs\tgene\t1\t1000\t.\t+\t.\tID=g1\n"
    "chr1\trs\texon\t100\t200\t.\t+\t.\tID=e1;Parent=g1\n"
)
db = create_db(text, ":memory:", from_string=True)
```

### 1.5 Tune ingest with `max_depth` and parallel threads

<!-- docs-test: skip reason="illustrative: names annotation.gff3, which the reader supplies" -->
```python
import os
from gffbase import ingest, FeatureDB

# `max_depth` controls how deep the materialized closure goes.
# Default is 8 — fine for GENCODE/Ensembl. Bump it if your hierarchy
# is deeper (e.g. nested gene clusters, custom annotations).
con, stats = ingest.from_file(
    "deep_annotation.gff3",
    dbfn="deep.duckdb",
    force=True,
    max_depth=16,             # default 8
    batch_size=100_000,       # default 50k — bigger uses more RAM
)
con.close()
print(f"closure rows: {stats.n_closure_rows:,}")

# Parallel ingest: DuckDB respects the `GFFBASE_THREADS` env var.
# Set it BEFORE create_db() / ingest.from_file().
# (`GFFUTILS2_THREADS` is still honoured as a deprecated alias, but the
# new name wins when both are set.)
os.environ["GFFBASE_THREADS"] = "8"
db = FeatureDB("deep.duckdb")
db.set_pragmas({"threads": 8})       # also tunable post-open
```

### 1.6 `compat` vs `strict`

<!-- docs-test: skip reason="illustrative: names a file the reader supplies" -->
```python
from gffbase import create_db, GFFFormatError

# The DEFAULT is mode="compat": it reads what gffutils reads, including the
# specification violations real annotation files are full of.
db = create_db("messy.gff3", "messy.duckdb", force=True)

# mode="strict" applies the full GFF3 specification and refuses.
try:
    db = create_db("messy.gff3", "strict.duckdb", force=True, mode="strict")
except GFFFormatError as e:
    print(f"line {e.line_no}: [{e.kind}] {e.message}")
```

`strict` is also the only mode that fuses several lines sharing one `ID` into
a single discontinuous feature — see [§6.7](#67-discontinuous-multipart-features).
`on_multipart_conflict=` chooses between raising and splitting when those
lines disagree on seqid, source, featuretype or strand.

The two axes underneath (`validation=` selects the rule set, `on_error=`
selects what a violation does) can be set independently; `mode=` is the
shorthand for the two combinations that make sense together.

---

## 2. Standard relational queries

### 2.1 `children()` — walk down the hierarchy

<!-- docs-test: skip reason="illustrative: uses a real accession id, not in the test fixtures" -->
```python
# Direct children only (level=1):
for tx in db.children("ENSG00000139618", level=1):
    print(tx.id, tx.featuretype, tx.start, tx.end)

# All descendants (level=None):
for f in db.children("ENSG00000139618", level=None):
    pass

# Filter by featuretype:
exons = list(db.children("ENST00000380152", featuretype="exon"))
```

### 2.2 `parents()` — walk up the hierarchy

<!-- docs-test: skip reason="illustrative: uses a placeholder feature id" -->
```python
# All ancestors of an exon: its mRNA + the gene above it.
for ancestor in db.parents("exon_id_42", level=None):
    print(ancestor.id, ancestor.featuretype)

# One level up only:
mrna = next(db.parents("exon_id_42", level=1))
```

### 2.3 `features_of_type()` — every feature of a kind

```python
# Stream every gene:
for gene in db.features_of_type("gene"):
    print(gene.id, gene.seqid, gene.start, gene.end)

# Multiple types at once:
for f in db.features_of_type(["mRNA", "lncRNA", "miRNA"]):
    pass

# Region-clipped + ordered:
for exon in db.features_of_type(
    "exon",
    limit="chr17:43044295-43125483",
    order_by="start",
):
    pass
```

### 2.4 Random access by ID

<!-- docs-test: skip reason="illustrative: uses a real accession id, not in the test fixtures" -->
```python
# Square-bracket lookup is the fastest single-feature access:
gene = db["ENSG00000139618"]   # raises FeatureNotFoundError on miss

# Membership check:
if "ENSG00000139618" in db:
    pass

# Aggregate counts:
print(db.count_features_of_type())          # total
print(db.count_features_of_type("exon"))    # just exons
print(list(db.featuretypes()))              # distinct featuretype values
```

---

## 3. Spatial queries

### 3.1 `region()` — overlap query

```python
# String form: "seqid:start-end"
for f in db.region("chr17:43044295-43125483"):
    print(f.featuretype, f.start, f.end, f.id)

# Keyword form (equivalent):
for f in db.region(seqid="chr17", start=43_044_295, end=43_125_483):
    pass

# Filter by featuretype during the spatial query:
for exon in db.region(
    "chr17:43044295-43125483",
    featuretype="exon",
):
    pass

# Filter by strand:
plus_strand = list(db.region(
    seqid="chr1", start=1_000_000, end=2_000_000,
    strand="+",
))
```

### 3.2 `completely_within=True` — strict containment

```python
# Default (overlap): any feature touching the region.
overlapping = list(db.region("chr1:100-200"))

# Strict: only features whose [start, end] is fully inside [100, 200].
contained = list(db.region("chr1:100-200", completely_within=True))
```

### 3.3 Region from a Feature object (re-use coords)

<!-- docs-test: skip reason="illustrative: uses a real accession id, not in the test fixtures" -->
```python
gene = db["ENSG00000139618"]
# Pull every feature that overlaps this gene's footprint:
for f in db.region(gene):
    pass
```

---

## 4. Feature object — fields, attributes, mutation, export

### 4.1 Standard fields

<!-- docs-test: skip reason="illustrative: uses a real accession id, not in the test fixtures" -->
```python
gene = db["ENSG00000139618"]
print(gene.seqid, gene.start, gene.end)   # chr13 32315474 32400266
print(gene.strand, gene.frame, gene.score)
print(gene.featuretype, gene.source)
print(gene.chrom, gene.stop)              # aliases for seqid / end
```

### 4.2 Dynamic attributes (col-9)

<!-- docs-test: skip reason="illustrative: uses a real accession id, not in the test fixtures" -->
```python
# attributes is a multi-value mapping: each key maps to a list[str]
print(gene.attributes["gene_id"])         # ['ENSG00000139618']
print(gene.attributes.get("Note", []))    # safe default

# Multi-value example: NCBI Dbxref carries multiple cross-refs.
for xref in gene.attributes.get("Dbxref", []):
    print(xref)

# Dict-style materialization (handy for JSON/CSV export):
print(gene.attributes_dict())
# {'ID': ['gene-...'], 'Name': ['BRCA2'], 'gene_biotype': ['protein_coding'], ...}
```

### 4.3 Mutate + write back

<!-- docs-test: skip reason="mutates the page's shared database" -->
```python
# Edit in place — attributes is a mutable mapping.
gene.attributes["custom_tag"] = ["my_pipeline:v1"]
gene.start = gene.start - 1000   # extend by 1 kb upstream
gene.score = "0.95"

# Persist the change back to the DB:
db.update([gene])
```

### 4.4 Export to a GFF/GTF line

```python
# str(feature) emits a tab-separated GFF line, ready for I/O.
print(str(gene))
# chr13  HAVANA  gene  32315474  32400266  .  +  .  ID=...

# bytes / file-write friendly:
with open("out.gff3", "a") as fout:
    fout.write(str(gene) + "\n")
```

### 4.5 Sequence extraction (FASTA)

```python
# pyfaidx-style mapping (fasta[seqid][start:end]) works:
fasta = {"chr1": "AAAA" + "ACGT" * 100_000}
seq = gene.sequence(fasta)              # forward
rev = gene.sequence(fasta, use_strand=True)   # rev-comp on '-'
```

### 4.6 Dict-shape representation (for downstream tools)

```python
# Construct your own dict if you want a JSON-friendly shape.
record = {
    "seqid": gene.seqid,
    "start": gene.start, "end": gene.end,
    "strand": gene.strand, "featuretype": gene.featuretype,
    "attributes": dict(gene.attributes),   # {key: [values]}
}
import json; print(json.dumps(record, indent=2))
```

---

## 5. Vectorized ML API — the ones that make GFFBase fast

The headline win. **One** SQL query per call, results land in a
zero-copy `pyarrow.Table` (or `pandas.DataFrame` / `polars.DataFrame`)
without ever instantiating a Python `Feature` object.

### 5.1 `children_batched()` → PyArrow

<!-- docs-test: skip reason="illustrative: uses a real accession id, not in the test fixtures" -->
```python
# Pull every exon for 50 000 transcripts in one call.
transcript_ids = ["ENST00000380152", "ENST00000544455", ...]   # any size

table = db.children_batched(
    transcript_ids,
    featuretype="exon",
    format="arrow",     # default
)
# Columns: anchor, descendant_id, seqid, source, featuretype,
#          start, end, score, strand, frame, file_order, depth.
print(table.num_rows, "exon rows")

# Hand off to PyTorch / JAX without copying.
import torch
starts = torch.from_numpy(table.column("start").to_numpy())
```

### 5.2 `children_batched()` → pandas

<!-- docs-test: skip reason="illustrative: an id list the reader supplies" -->
```python
df = db.children_batched(
    transcript_ids,
    featuretype="exon",
    format="df",        # pandas.DataFrame
)

# Group back to per-transcript exon counts via the `anchor` column —
# no need to re-issue N queries:
print(df.groupby("anchor").size().describe())
```

### 5.3 `children_batched()` → polars

<!-- docs-test: skip reason="illustrative: an id list the reader supplies" -->
```python
# Polars is optional: `pip install polars`
plframe = db.children_batched(
    transcript_ids,
    featuretype="exon",
    format="polars",
)
print(plframe.group_by("anchor").len())
```

### 5.4 `parents_batched()` — symmetric upward walk

<!-- docs-test: skip reason="illustrative: an id list the reader supplies" -->
```python
# For each exon id, get its mRNA + gene ancestors in one query.
exon_ids = [r[0] for r in db.execute(
    "SELECT id FROM features WHERE featuretype='exon' LIMIT 50000"
).fetchall()]

ancestors = db.parents_batched(exon_ids, format="arrow")
print(ancestors.column_names)
# ['anchor', 'descendant_id', ...] — 'anchor' is the input id;
# 'descendant_id' here is actually the ancestor (kept for column-
# stability across the children/parents directions).
```

### 5.5 `region_batched()` — bulk spatial overlap

```python
# Common ML pattern: "for each ATAC-seq peak in this BED file,
# find every overlapping CDS." One SQL query.
peaks = [
    ("chr1", 100_000, 110_000),
    ("chr1", 200_000, 210_000),
    ("chr2", 1_500_000, 1_600_000),
    # ... up to millions of regions
]

overlaps = db.region_batched(peaks, featuretype="CDS", format="arrow")
# Columns: query_idx, query_seqid, query_start, query_end,
#          id, seqid, source, featuretype, start, end, score,
#          strand, frame, file_order.
print(overlaps.num_rows, "CDS overlaps across", len(peaks), "peaks")

# `query_idx` lets you rebuild per-peak hits without an N-query loop:
import pyarrow.compute as pc
hits_per_peak = pc.value_counts(overlaps.column("query_idx"))
```

### 5.6 `region_batched()` — `completely_within` flag

```python
contained = db.region_batched(
    peaks,
    featuretype="exon",
    completely_within=True,    # only fully-contained features
    format="arrow",
)
```

---

## 6. Synthesis & convenience methods

### 6.1 `interfeatures()` — derive intergenic regions

```python
# Yield "gap" features sitting between consecutive features.
genes = sorted(db.features_of_type("gene"), key=lambda g: (g.seqid, g.start))
for gap in db.interfeatures(genes, new_featuretype="intergenic"):
    print(gap.seqid, gap.start, gap.end)
```

### 6.2 `merge()` / `merge_all()` — collapse overlapping features

```python
from gffbase import merge_criteria as mc

exons_chr1 = list(db.features_of_type("exon", limit="chr1"))
for merged in db.merge(exons_chr1, merge_criteria=(mc.overlap_end_inclusive,)):
    print(merged.start, merged.end, len(merged.children))

# Whole-DB sweep. NOTE: merge_all returns a LIST and WRITES the merged
# features back to the database -- it always documented that it did, and as
# of 0.2.0 it actually does. Only genuine merges are returned; a feature that
# merged with nothing is left alone.
merged = db.merge_all(featuretypes_groups=("exon",))
print(f"{len(merged)} merged features written back")

# The components stay, linked to the merged feature by a Parent attribute...
merged = db.merge_all(featuretypes_groups=("exon",), exclude_components=False)
# ...or are deleted outright.
merged = db.merge_all(featuretypes_groups=("exon",), exclude_components=True)
```

### 6.3 `create_introns()` / `create_splice_sites()`

```python
# Introns are computed PER TRANSCRIPT: with grandparent_featuretype="gene",
# gffbase descends one level first, so a multi-isoform gene yields each
# isoform's introns rather than gaps spanning transcript boundaries.
introns = list(db.create_introns(exon_featuretype="exon",
                                 grandparent_featuretype="gene"))
print(f"{len(introns):,} introns")

# Or anchor directly on the transcript.
introns = list(db.create_introns(grandparent_featuretype=None,
                                 parent_featuretype="mRNA"))

# Splice sites are 2 bp -- a dinucleotide -- and typed by their position in
# the transcript: the left site of a minus-strand transcript is its 3' site.
sites = list(db.create_splice_sites(exon_featuretype="exon"))
print({s.featuretype for s in sites})
# {'five_prime_cis_splice_site', 'three_prime_cis_splice_site'}
```

### 6.4 `bed12()` — UCSC track export

<!-- docs-test: skip reason="illustrative: continues from a skipped example" -->
```python
# Emit a BED12 line for each transcript.
with open("transcripts.bed", "w") as fout:
    for tx in db.features_of_type("transcript", limit="chr1"):
        fout.write(db.bed12(tx, name_field="transcript_id") + "\n")

# `thick` is the translated span, taken from CDS children by default. Name the
# UNtranslated parts instead with thin_featuretype; the two are mutually
# exclusive and passing both raises.
line = db.bed12(tx, thin_featuretype=["five_prime_UTR", "three_prime_UTR"])
```

No trailing comma is emitted on `blockSizes` / `blockStarts`, and a feature
with no CDS children is marked entirely thick rather than entirely thin --
both were wrong before 0.2.0 and both made every line differ from what
gffutils writes.

### 6.5 `children_bp()` — total length of nested children

<!-- docs-test: skip reason="illustrative: uses a real accession id, not in the test fixtures" -->
```python
# Total exonic basepairs in a gene.
gene = db["ENSG00000139618"]
total_bp = db.children_bp(gene, child_featuretype="exon")
print(f"{total_bp:,} bp of exonic sequence")
```

### 6.6 `iter_by_parent_childs()` — yield each parent + its children

```python
# Stream gene + all-its-descendants groups in one pass.
for gene, *children in db.iter_by_parent_childs(featuretype="gene"):
    print(f"{gene.id}: {len(children):,} child features")
```

---

### 6.7 Discontinuous (multipart) features

Several GFF3 lines may share one `ID` — a CDS interrupted by a frameshift, or
NCBI RefSeq's split CDS convention. Under `mode="strict"` they are one logical
feature.

<!-- docs-test: skip reason="illustrative: names refseq.gff3, which the reader supplies" -->
```python
db = create_db("refseq.gff3", "refseq.duckdb", mode="strict")

cds = db["cds-NP_000001.1"]
len(cds)                 # envelope span, first start to last end
cds.covered_length       # the same, MINUS the gaps
len(cds.segments)        # the physical input lines
[s.frame for s in cds.segments]   # per-segment phase, the reason this exists

cds.to_lines()           # every line back, byte for byte
```

An ordinary feature is its own sole segment, so `for s in f.segments` works
without asking whether the feature is discontinuous first.

The batched APIs can emit one row per physical line instead of per feature:

<!-- docs-test: skip reason="illustrative: an id list the reader supplies" -->
```python
exons = db.children_batched(ids, format="arrow", explode_segments=True)
```

That is offered on the tabular APIs only, deliberately: a `FeatureSegment`
leaking out of `region()` or `children()` would confuse legacy consumers.

### 6.8 Validating a database

```python
report = db.validate()          # fast structural invariants
report.ok                       # False if any ERROR-severity check failed
report.errors, report.warnings  # separate: an error is a broken invariant,
                                # a warning is legal but suspect

full = db.validate(level="full")  # adds recursive closure/content checks
```

Run automatically at the end of a strict-mode ingest, and available from the
command line as `gffbase validate --strict`.

### 6.9 Upgrading a v1 database

<!-- docs-test: skip reason="illustrative: names a v1 database the reader supplies" -->
```python
from gffbase.migrate import coalesce_multipart, migrate_v1_to_v2

migrate_v1_to_v2("old.duckdb")     # structural; changes no query result
coalesce_multipart(db)             # opt-in; DOES change results
```

`FeatureDB(..., upgrade="auto")` runs the first of these for you when it opens
a v1 database — which is only acceptable because it is structural.

## 7. Advanced / escape hatches

### 7.1 `GFFWriter` — emit new annotation files

<!-- docs-test: skip reason="illustrative: uses a real accession id, not in the test fixtures" -->
```python
from gffbase import GFFWriter

with GFFWriter("output.gff3") as w:
    w.write_rec(db["ENSG00000139618"])
    w.write_recs(db.features_of_type("transcript", limit="chr13"))

    # Convenience helpers for hierarchy-walking output:
    w.write_gene_recs(db, "ENSG00000139618")          # gene + all descendants
    w.write_mRNA_children(db, "ENST00000380152")      # mRNA + level-1 children
    w.write_exon_children(db, "exon_id_42")           # exon + level-1 children
```

### 7.2 Iterate a raw file without building a database

<!-- docs-test: skip reason="illustrative: names annotation.gff3, which the reader supplies" -->
```python
from gffbase import parse_gff, GFFFormatError

# Stream parser — never materializes a DB.
for rec in parse_gff("annotation.gff3"):
    print(rec.seqid, rec.start, rec.end, rec.attributes_pairs)

# Non-strict mode: malformed lines are surfaced as warnings, valid
# records still flow.
it = parse_gff("messy.gff3", strict=False)
for rec in it:
    process(rec)
for w in it.warnings:
    print(w["line_no"], w["kind"], w["message"])
```

### 7.3 `DataIterator` — legacy-compatible streaming reader

<!-- docs-test: skip reason="illustrative: names annotation.gff3, which the reader supplies" -->
```python
from gffbase import DataIterator

# Read a whole file or a single string of GFF3 records.
for f in DataIterator("annotation.gff3"):
    print(f.id, f.attributes_dict())

print(DataIterator.__doc__)   # discovers the legacy API verbatim
```

### 7.4 `parse_bytes()` — drive the parser from in-memory bytes

```python
from gffbase import parse_bytes

raw = (b"##gff-version 3\n"
       b"chr1\trs\texon\t1\t100\t.\t+\t.\tID=e1\n")
for rec in parse_bytes(raw):
    print(rec.featuretype, rec.start, rec.end)
```

### 7.5 `execute()` — raw SQL escape hatch

```python
# Drop into the underlying DuckDB connection for anything the API
# doesn't expose. Returns the same cursor DuckDB-Python would.
rows = db.execute("""
    SELECT f.id, f.seqid, f.start, f."end"
    FROM features f
    JOIN attributes a ON a.feature_id = f.id
    WHERE f.featuretype = 'gene'
      AND a.key = 'gene_biotype'
      AND a.value = 'protein_coding'
    ORDER BY f.seqid, f.start
""").fetchall()
print(f"{len(rows):,} protein-coding genes")

# Back-compat: queries written for legacy gffutils' SQLite schema can
# hit `features_compat` and `relations_compat` views verbatim.
db.execute("SELECT * FROM features_compat WHERE seqid = 'chr1' LIMIT 5"
).fetchall()
```

### 7.6 `analyze()` + `set_pragmas()` — DuckDB tuning

```python
# After heavy mutation (delete/update), run ANALYZE so the planner
# picks the right join order. Idempotent — cheap to call.
db.analyze()

# Set DuckDB pragmas. Anything DuckDB rejects is silently skipped
# so legacy SQLite-style pragmas are safe to pass through.
db.set_pragmas({
    "threads": 8,
    "memory_limit": "4GB",
})
```

### 7.7 `delete()` / `update()` / `add_relation()`

<!-- docs-test: skip reason="illustrative: uses a real accession id, not in the test fixtures" -->
```python
# Remove features. Accepts ids, Feature objects, or another FeatureDB.
db.delete(["ENSG00000139618"])
db.delete([db["ENSG00000141510"]])      # by Feature
# db.delete(other_feature_db)           # delete every id present in `other`

# Insert new features.
from gffbase import Feature
new_exon = Feature(
    seqid="chr1", source="custom", featuretype="exon",
    start=100, end=200, strand="+", frame=".",
    attributes={"ID": ["custom_exon_1"], "Parent": ["ENST00000000000"]},
)
db.update([new_exon])

# Wire two existing features into a parent/child edge.
db.add_relation("ENSG00000139618", "custom_exon_1", level=2)
```

### 7.8 `export_sqlite()` — produce a legacy-gffutils-readable file

```python
from gffbase import export_sqlite

# Materialize the GFFBase data into a SQLite file with the legacy
# `features` / `relations` schema. Downstream tools that only know
# how to read `gffutils.FeatureDB("annotation.sqlite")` work
# unchanged.
out_path = export_sqlite(db.conn, "annotation.sqlite")
print(out_path)

# Round-trip:
import gffutils                         # pip install gffutils
# The export already contains SQLite ANALYZE statistics, so the legacy
# reader opens it without the "database has not had ANALYZE" warning.
legacy = gffutils.FeatureDB(out_path)
print(legacy.count_features_of_type())
```

---

## 8. Putting it all together — end-to-end ML pipeline

<!-- docs-test: skip reason="illustrative: names gencode.v49, which the reader supplies" -->
```python
from gffbase import create_db
import torch

# 1. Ingest once.
db = create_db(
    "gencode.v49.basic.annotation.gtf.gz",
    "gencode.duckdb",
    force=True,
)

# 2. Pick the transcripts you care about (any predicate).
target_ids = [r[0] for r in db.execute("""
    SELECT f.id
    FROM features f
    JOIN attributes a ON a.feature_id = f.id
    WHERE f.featuretype = 'transcript'
      AND a.key = 'tag'
      AND a.value = 'MANE_Select'
""").fetchall()]
print(f"{len(target_ids):,} MANE_Select transcripts")

# 3. ONE batched call → pyarrow.Table sharing memory with DuckDB.
exons = db.children_batched(
    target_ids, featuretype="exon", format="arrow",
)

# 4. Tensor handoff — zero copy.
starts = torch.from_numpy(exons.column("start").to_numpy())
ends   = torch.from_numpy(exons.column("end").to_numpy())
print(starts.shape, ends.shape)
```

That's the full GFFBase API. For real-corpus walkthroughs, see the
[Cookbooks](cookbooks/index.md). For numbers, see
[Performance Comparison](performance.md).
