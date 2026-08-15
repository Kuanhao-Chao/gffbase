# GENCODE / Ensembl GTF Cookbook

GENCODE and Ensembl ship full mammalian annotations as gzipped GTF.
Their hierarchy is gene → transcript → exon / CDS / UTR /
start_codon / stop_codon — three levels deep, with several million
features per annotation and ~100 features per gene on average.

This cookbook covers the four most common tasks downstream pipelines
need.

## 1. Ingest

<!-- docs-test: skip reason="needs the GENCODE v49 corpus (~90 MB download)" -->
```python
from gffbase import create_db

db = create_db(
    "gencode.v49.chr_patch_hapl_scaff.basic.annotation.gtf.gz",
    "gencode.duckdb",
    force=True,
)
print(f"{db.count_features_of_type():,} features")    # ~6.5 M (incl. synth parents)
print(db.fmt)                                          # 'gtf'
```

GTF input is auto-detected; `disable_infer_genes` and
`disable_infer_transcripts` default to `False` so missing parent rows
are synthesized by a single set-based `GROUP BY` over the
attribute-normalized `transcript_id` / `gene_id` columns. (See
[Performance Comparison](../performance.md) — *"GTF Synthesis
Bottleneck"* — for why this matters.)

## 2. Walk a single gene's hierarchy

<!-- docs-test: skip reason="illustrative: uses a real accession id, not in the test fixtures" -->
```python
gene = db["ENSG00000139618"]                # BRCA2
print(gene.featuretype, gene.start, gene.end)

for tx in db.children(gene, level=1, featuretype="transcript"):
    n_exons = sum(1 for _ in db.children(tx, level=1, featuretype="exon"))
    print(f"  {tx.id}  {tx.start}-{tx.end}  ({n_exons} exons)")

# All descendants (exons + CDSs + UTRs) in one shot
for f in db.children(gene, level=None):
    pass
```

`children(level=1)` is a closure-cache point lookup; `children(level=None)`
returns the full descendant set. The relational dispatcher auto-routes
between the materialized closure and a recursive CTE.

## 3. Filter by attribute (e.g. all protein-coding genes)

GENCODE attributes are stored in a normalized long-form table indexed on
`(key, value)`. Attribute filtering is therefore an indexed query, not a
JSON scan:

```python
rows = db.execute("""
    SELECT f.id, f.seqid, f.start, f."end"
    FROM features f
    JOIN attributes a ON a.feature_id = f.id
    WHERE f.featuretype = 'gene'
      AND a.key = 'gene_type'
      AND a.value = 'protein_coding'
""").fetchall()
print(f"{len(rows):,} protein-coding genes")
```

## 4. BED12 export for genome-browser tracks

<!-- docs-test: skip reason="writes a genome-browser track from the GENCODE corpus" -->
```python
with open("gencode.bed", "w") as fout:
    for tx in db.features_of_type("transcript", limit="chr1"):
        fout.write(db.bed12(tx, name_field="transcript_id") + "\n")
```

## 5. Bulk extraction into PyArrow (the ML path)

For ML pipelines that need exons for many transcripts at once, prefer
the vectorized API — see
[`machine_learning_workflows.md`](machine_learning_workflows.md) for the
full pattern:

```python
gene_ids = [r[0] for r in db.execute(
    "SELECT id FROM features WHERE featuretype='gene' LIMIT 50000"
).fetchall()]
table = db.children_batched(gene_ids, featuretype="exon", format="arrow")
print(table.num_rows, "exons,", len(table.column_names), "columns")
```

## Performance notes (real GENCODE v49 numbers)

| Task | Wall | Source |
|---|---|---|
| Full ingest (6.07 M lines) | see [Performance](../performance.md) | measured per release |
| `children(g, level=1)` (single gene) | <1 ms | materialized closure cache |
| Bulk `children_batched()` | see [Performance](../performance.md) | measured per release |
| Random `region(seqid:start-end)` | ~0.7 ms | R-tree path |
