# Cookbooks

Concrete, runnable recipes for the most common real-world annotation
workflows. Every snippet has been validated against the GENCODE v45 basic
test corpus shipped in `bench/data/`.

| Cookbook | Topic |
|---|---|
| [GENCODE / Ensembl](gencode_ensembl.md) | Deeply nested gene → transcript → exon hierarchies |
| [NCBI RefSeq](refseq.md) | Massive chromosome records, `Dbxref`, `Note`, `gbkey` tags |
| [MANE](mane.md) | Filtering for `tag=MANE_Select` and `tag=MANE_Plus_Clinical` |
| [**Machine Learning Workflows**](machine_learning_workflows.md) | Bulk feature extraction → PyArrow → Hugging Face / PyTorch with zero per-row Python overhead |

## Conventions

```python
from gffbase import create_db, FeatureDB
db = create_db("annotation.gff3", "annotation.duckdb", force=True)
# …or re-open an existing DB:
db = FeatureDB("annotation.duckdb")
```

The cookbooks assume `gffbase` is on the import path
(`pip install gffbase` or `pip install -e .` from the repo root) and
DuckDB's spatial extension is available (it auto-installs on first
ingest; see Phase 7 R-tree work).
