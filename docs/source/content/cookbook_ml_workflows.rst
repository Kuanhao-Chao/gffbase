.. _cookbook_ml_workflows--machine-learning-workflows-cookbook:

Machine Learning Workflows Cookbook
===================================

The flagship cookbook. The patterns here are the reason
``children_batched(format='arrow')`` exists: bulk feature extraction for
ML pipelines with **zero Python** ``Feature`` **object construction** and
**zero per-row boundary crossings** between DuckDB and the consuming
ML framework.

The measurements behind every claim on this page are on the
:doc:`Performance <performance>` page, generated from a committed results file
rather than transcribed by hand.

----

.. _cookbook_ml_workflows--1-the-anti-pattern-dont-do-this-for-ml:

1. The anti-pattern (don't do this for ML)
------------------------------------------

The "obvious" Pythonic loop is the slow path. It instantiates one
``Feature`` object per row × N anchors:

.. docs-test: skip reason="illustrative: the id list stands for a real corpus"

.. code-block:: python

   # DON'T — this hits OLAP point-query overhead AND pays per-row Feature
   # construction. At 50 000 genes it takes 10+ minutes.
   for gene_id in fifty_thousand_gene_ids:
       for exon in db.children(gene_id, featuretype="exon"):
           starts.append(exon.start)
           ends.append(exon.end)
           ...

.. _cookbook_ml_workflows--2-the-vectorized-pattern:

2. The vectorized pattern
-------------------------

One SQL query for the entire batch. One zero-copy Arrow buffer back.
Never materializes a ``Feature``:

.. docs-test: skip reason="needs the GENCODE v49 corpus"

.. code-block:: python

   import pyarrow as pa
   from gffbase import FeatureDB

   db = FeatureDB("gencode.duckdb")

   # Pull 50 000 transcript IDs (any predicate works — here, MANE_Select).
   tx_ids = [r[0] for r in db.execute("""
       SELECT f.id FROM features f
       JOIN attributes a ON a.feature_id = f.id
       WHERE f.featuretype = 'transcript'
         AND a.key = 'tag' AND a.value = 'MANE_Select'
       LIMIT 50000
   """).fetchall()]

   # One call, one query, one Arrow table.
   exons: pa.Table = db.children_batched(
       tx_ids,
       featuretype="exon",
       format="arrow",
   )
   print(exons.num_rows, "exons in", exons.num_columns, "columns")
   print(exons.schema)
   # anchor: string                  ← input transcript_id (for groupby)
   # descendant_id: string           ← exon_id
   # seqid, source, featuretype: string
   # start, end: int64
   # score, strand, frame: string
   # file_order: int64
   # depth: int16

The columns are real ``pyarrow`` chunks backed by the same memory DuckDB
used during query execution. ``exons.column("start").to_numpy()`` is a
view on that buffer — no copy.

.. _cookbook_ml_workflows--3-hand-off-to-hugging-face-datasets:

3. Hand-off to Hugging Face ``datasets``
----------------------------------------

``datasets.Dataset.from_pyarrow_table()`` consumes the Arrow buffer
directly. The dataset shares memory with DuckDB until you write it to
disk:

.. docs-test: skip reason="needs the optional ``datasets`` package"

.. code-block:: python

   from datasets import Dataset

   ds = Dataset(exons)
   print(ds)
   # Dataset({
   #     features: ['anchor','descendant_id','seqid','source','featuretype',
   #                'start','end','score','strand','frame','file_order','depth'],
   #     num_rows: 287456
   # })

   # Group exons by their parent transcript (the anchor column):
   ds = ds.sort("anchor")

   # Add a sequence-extraction column via a UDF — vectorized over Arrow:
   import pyfaidx
   fa = pyfaidx.Fasta("GRCh38.primary_assembly.genome.fa")

   def add_seq(batch):
       seqs = []
       for seqid, s, e, strand in zip(batch["seqid"], batch["start"],
                                       batch["end"], batch["strand"]):
           seq = str(fa[seqid][s - 1 : e])
           if strand == "-":
               seq = seq.translate(str.maketrans("ACGTN", "TGCAN"))[::-1]
           seqs.append(seq)
       batch["sequence"] = seqs
       return batch

   ds = ds.map(add_seq, batched=True, batch_size=4096)

   # Ready to feed a tokenizer + model.
   ds.save_to_disk("mane_select_exons")

.. _cookbook_ml_workflows--4-hand-off-to-pytorch-dataloader:

4. Hand-off to PyTorch ``DataLoader``
-------------------------------------

Two equivalent paths — both bypass per-row Python:

.. _cookbook_ml_workflows--4a-direct-from-arrow-numpy-tensor:

4a. Direct from Arrow → NumPy → tensor
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

.. docs-test: skip reason="illustrative: contains an elided fragment"

.. code-block:: python

   import torch
   from torch.utils.data import TensorDataset, DataLoader

   starts = torch.from_numpy(exons.column("start").to_numpy())
   ends   = torch.from_numpy(exons.column("end").to_numpy())
   strands = exons.column("strand").to_pylist()    # small string column

   ds = TensorDataset(starts, ends)
   loader = DataLoader(ds, batch_size=512, shuffle=True, num_workers=4)

   for batch_starts, batch_ends in loader:
       ...   # train step

.. _cookbook_ml_workflows--4b-polars-dataframe-for-ad-hoc-feature-engineering:

4b. Polars ``DataFrame`` for ad-hoc feature engineering
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

.. docs-test: skip reason="illustrative: an id list the reader supplies"

.. code-block:: python

   exons_df = db.children_batched(tx_ids, featuretype="exon", format="polars")
   exon_lengths = (exons_df["end"] - exons_df["start"] + 1).to_numpy()

``format='polars'`` returns a ``polars.DataFrame`` built directly from the
Arrow buffers — same zero-copy property.

.. _cookbook_ml_workflows--5-spatial-bulk-feature-extraction-for-ml:

5. Spatial bulk feature extraction for ML
-----------------------------------------

``region_batched`` is the spatial counterpart. Want every CDS overlapping
a 50 000-row ATAC-seq peak BED file?

.. docs-test: skip reason="illustrative: contains an elided fragment"

.. code-block:: python

   peaks = [
       ("chr1", 1_000_000, 1_000_500),
       ("chr1", 1_002_000, 1_002_300),
       ...   # 50 000 entries
   ]

   cds = db.region_batched(peaks, featuretype="CDS", format="arrow")
   # query_idx column maps each result row back to its peak.
   print(cds.num_rows, "CDS overlaps across", len(peaks), "peaks")

   # Per-peak counts in a single Arrow operation:
   import pyarrow.compute as pc
   peak_overlap_counts = pc.value_counts(cds.column("query_idx"))

.. _cookbook_ml_workflows--6-why-this-is-fast:

6. Why this is fast
-------------------

.. list-table::
   :header-rows: 1
   :widths: 34 33 33

   * - Layer
     - Anti-pattern
     - Vectorized
   * - SQL
     - 50 000 separate ``SELECT`` calls
     - one ``SELECT … WHERE id IN (?, ?, …)``
   * - Python objects
     - 1.6 M ``Feature`` instances + dialect dicts + ``_LazyAttributes``
     - **zero** — Arrow buffers held by reference
   * - Result hand-off
     - per-row Python iteration
     - column-wise NumPy view
   * - Memory
     - Feature graveyard + GC pressure
     - DuckDB query buffer pool
   * - Wall
     - dominated by object construction
     - dominated by the query itself

``benchmarks/05_vectorized.py`` measures this head-to-head against legacy
``gffutils`` at 500 / 5 000 / 50 000 anchors; current numbers are on the
:doc:`Performance <performance>` page.

.. _cookbook_ml_workflows--7-tips-for-production-ml-pipelines:

7. Tips for production ML pipelines
-----------------------------------

- **Pin** ``format='arrow'`` unless you specifically need a pandas
  DataFrame — Arrow → pandas conversion is the only Python-side cost in
  the path.

- **Use** ``db.children_batched(level=1, featuretype="exon")`` when you
  only want direct children — DuckDB's planner can prune the closure
  table to depth-1 entries via the index on ``(ancestor, depth)``.

- **Reuse the** ``FeatureDB`` **instance** across batches. The per-DB-open
  cost is dominated by ``LOAD spatial`` and meta-table reads (~50 ms);
  amortize it across many ``children_batched`` calls.

- **Partition by chromosome** for embarrassingly parallel work:

  .. code-block:: python

     for seqid in db.seqids():
         ids_on_seqid = [r[0] for r in db.execute(
             "SELECT id FROM features WHERE featuretype='transcript' AND seqid=?",
             [seqid],
         ).fetchall()]
         table = db.children_batched(ids_on_seqid, featuretype="exon",
                                     format="arrow")
         yield table   # one Arrow table per chromosome → multiprocessing-ready

- **Multiple workers**: open the DB read-only with
  ``duckdb.connect(path, read_only=True)`` per worker — DuckDB's MVCC
  allows unlimited concurrent readers.
