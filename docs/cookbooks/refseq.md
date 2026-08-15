# NCBI RefSeq GFF3 Cookbook

NCBI RefSeq ships organism-wide annotations as GFF3 with chromosome
records that can exceed 1 GB uncompressed. RefSeq's idiosyncrasies are
worth calling out:

- **`Dbxref=GeneID:7157,HGNC:HGNC:11998`** — multi-value cross-references.
- **`gbkey=Gene`**, `gbkey=mRNA`, `gbkey=CDS` — RefSeq's redundant
  parallel "GenBank key" tags.
- **`Note=...`** — long, free-form text often containing semicolons that
  confuse naïve parsers.
- Mixed `chromosome` features at the top level (one per chromosome).
- Chromosome names like `NC_000001.11` instead of `chr1`.

GFFBase handles all of these out of the box — the Rust parser's state
machine treats quoted-and-percent-encoded GFF3 attribute values
correctly. This cookbook shows the patterns
that come up most often.

## 1. Ingest a RefSeq genome annotation

<!-- docs-test: skip reason="needs the RefSeq GRCh38.p14 corpus" -->
```python
from gffbase import create_db

db = create_db(
    "GCF_000001405.40_GRCh38.p14_genomic.gff.gz",   # NCBI Human RefSeq
    "refseq.duckdb",
    force=True,
)
print(db.fmt)   # 'gff3'
```

RefSeq files are GFF3, so `Parent=` attributes drive edge construction
directly (no inference passes — `disable_infer_*` is moot here).

## 2. Convert NCBI seqids to UCSC-style chromosomes

```python
import duckdb

# Quick lookup of what's actually in the file:
print(list(db.seqids())[:5])
# ['NC_000001.11', 'NC_000002.12', 'NC_000003.12', ...]

# Load a mapping (NCBI ships a `chromAlias.txt` per assembly):
ALIAS = {
    "NC_000001.11": "chr1",
    "NC_000002.12": "chr2",
    # …
}

# Use it in any region query:
ucsc_query = "chr1:1000000-2000000"
nc_seqid = next(k for k, v in ALIAS.items() if v == "chr1")
for f in db.region(seqid=nc_seqid, start=1_000_000, end=2_000_000):
    print(f)
```

## 3. Filter on `gbkey` and `Dbxref`

The normalized `attributes` table (`feature_id, key, value, idx`) makes
multi-value filters cheap:

```python
# All genes with an HGNC dbxref
rows = db.execute("""
    SELECT f.id, f.seqid, f.start, f."end"
    FROM features f
    JOIN attributes a_kind ON a_kind.feature_id = f.id
                          AND a_kind.key = 'gbkey'
                          AND a_kind.value = 'Gene'
    JOIN attributes a_xref ON a_xref.feature_id = f.id
                          AND a_xref.key = 'Dbxref'
                          AND a_xref.value LIKE 'HGNC:%'
""").fetchall()
print(f"{len(rows):,} HGNC-mapped genes")
```

Note that `Dbxref=GeneID:7157,HGNC:HGNC:11998` is split into two rows in
`attributes` (idx=0, idx=1), so each cross-reference can be filtered
independently.

## 4. Read long `Note` values without semicolon corruption

The Rust parser's hand-written state machine honors `note "a;b;c"`
quoted ranges and percent-escapes (`%3B` → `;`). Quotes are
seamless:

```python
for f in db.features_of_type("exon", limit="NC_000001.11"):
    note = f.attributes.get("Note", [])
    if note and "pseudogene" in note[0].lower():
        print(f.id, note[0])
```

`f.attributes` materializes lazily from the original col-9 bytes
(via the `_LazyAttributes` wrapper); features whose attributes are never read
pay zero parsing cost.

## 5. Coordinate the chromosome row + everything on it

RefSeq emits one `chromosome` feature per seqid as the root of the
hierarchy. `iter_by_parent_childs` gives you each chromosome plus
everything in it, in file order:

```python
for parent, *children in db.iter_by_parent_childs(featuretype="chromosome"):
    print(f"{parent.seqid}: {len(children):,} child features")
```

## 6. Bulk export to BED for downstream tools

```python
with open("refseq.cds.bed", "w") as fout:
    for cds in db.features_of_type("CDS"):
        fout.write("\t".join(str(x) for x in [
            cds.seqid, cds.start - 1, cds.end,
            cds.id or ".",
            cds.score if cds.score != "." else "0",
            cds.strand,
        ]) + "\n")
```

## Performance notes

RefSeq's largest GFFs (e.g. mouse GRCm39, ~3 GB uncompressed) ingest
in **~5–6 minutes** on a stock laptop with the same recursive-CTE
closure + bulk Arrow path used for GENCODE. Per-feature memory stays
flat thanks to the 50 000-row Arrow batching in `_ArrowBatchBuilder`.
