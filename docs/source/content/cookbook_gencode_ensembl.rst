.. _cookbook_gencode_ensembl--gencode-ensembl-gtf-cookbook:

GENCODE / Ensembl GTF Cookbook
==============================

GENCODE and Ensembl ship full mammalian annotations as gzipped GTF.
Their hierarchy is gene → transcript → exon / CDS / UTR /
start_codon / stop_codon — three levels deep, with several million
features per annotation and ~100 features per gene on average.

This cookbook covers the four most common tasks downstream pipelines
need.

.. _cookbook_gencode_ensembl--1-ingest:

1. Ingest
---------

.. docs-test: skip reason="needs the GENCODE v49 corpus (~90 MB download)"

.. code-block:: python

   from gffbase import create_db

   db = create_db(
       "gencode.v49.chr_patch_hapl_scaff.basic.annotation.gtf.gz",
       "gencode.duckdb",
       force=True,
   )
   print(f"{db.count_features_of_type():,} features")    # 6,068,892
   print(db.fmt)                                          # 'gtf'

GTF input is auto-detected. GENCODE v49 supplies a gene row for every
``gene_id`` and a transcript row for every ``transcript_id``, so inference finds
nothing to synthesize: the database holds exactly the file's 6,068,892 rows.
``disable_infer_genes=True`` and ``disable_infer_transcripts=True`` skip the
check, which the benchmarks do for a controlled comparison (see
:doc:`Benchmark methodology <methodology>`). Leave inference on for leaf-only
files -- StringTie, older Ensembl releases, anything derived: missing parents
are synthesized set-based from the ``transcript_id`` / ``gene_id`` attributes
(or the ones named by ``gtf_transcript_key`` / ``gtf_gene_key``).

GENCODE ids are **versioned**: a gene's id is its ``gene_id`` attribute,
``ENSG00000139618.19``, not the bare accession.

.. _cookbook_gencode_ensembl--2-walk-a-single-genes-hierarchy:

2. Walk a single gene's hierarchy
---------------------------------

.. docs-test: skip reason="illustrative: uses a real accession id, not in the test fixtures"

.. code-block:: python

   gene = db["ENSG00000139618.19"]             # BRCA2; ids carry the version
   print(gene.featuretype, gene.start, gene.end)

   # Only have the unversioned accession? Resolve it first:
   (gene_id,) = db.execute(
       "SELECT id FROM features WHERE featuretype = 'gene' AND split_part(id, '.', 1) = ?",
       ["ENSG00000139618"],
   ).fetchone()

   for tx in db.children(gene, level=1, featuretype="transcript"):
       n_exons = sum(1 for _ in db.children(tx, level=1, featuretype="exon"))
       print(f"  {tx.id}  {tx.start}-{tx.end}  ({n_exons} exons)")

   # All descendants (exons + CDSs + UTRs) in one shot
   for f in db.children(gene, level=None):
       pass

``children(level=1)`` reads the materialized closure; ``children(level=None)``
returns the full descendant set. The dispatcher routes between the closure and
a recursive CTE. Each call is one or two queries, a few milliseconds -- fine
for a handful of genes, slow as a loop over thousands. For those, use
``children_batched`` (section 5).

.. _cookbook_gencode_ensembl--3-filter-by-attribute-eg-all-protein-coding-genes:

3. Filter by attribute (e.g. all protein-coding genes)
------------------------------------------------------

GENCODE attributes are stored in a normalized long-form table,
``attributes(feature_id, key, value, idx)``, so an attribute filter is an
ordinary join that DuckDB runs as a columnar scan -- no JSON is parsed:

.. code-block:: python

   rows = db.execute("""
       SELECT f.id, f.seqid, f.start, f."end"
       FROM features f
       JOIN attributes a ON a.feature_id = f.id
       WHERE f.featuretype = 'gene'
         AND a.key = 'gene_type'
         AND a.value = 'protein_coding'
   """).fetchall()
   print(f"{len(rows):,} protein-coding genes")

.. _cookbook_gencode_ensembl--4-bed12-export-for-genome-browser-tracks:

4. BED12 export for genome-browser tracks
-----------------------------------------

.. docs-test: skip reason="writes a genome-browser track from the GENCODE corpus"

.. code-block:: python

   with open("gencode.bed", "w") as fout:
       for tx in db.features_of_type("transcript", limit="chr1"):
           fout.write(db.bed12(tx, name_field="transcript_id") + "\n")

.. _cookbook_gencode_ensembl--5-bulk-extraction-into-pyarrow-the-ml-path:

5. Bulk extraction into PyArrow (the ML path)
---------------------------------------------

For ML pipelines that need exons for many transcripts at once, prefer
the vectorized API — see
:doc:`machine_learning_workflows.md <cookbook_ml_workflows>` for the
full pattern:

.. code-block:: python

   gene_ids = [r[0] for r in db.execute(
       "SELECT id FROM features WHERE featuretype='gene' LIMIT 50000"
   ).fetchall()]
   table = db.children_batched(gene_ids, featuretype="exon", format="arrow")
   print(table.num_rows, "exons,", len(table.column_names), "columns")

.. _cookbook_gencode_ensembl--performance-notes-real-gencode-v49-numbers:

Performance notes
-----------------

Whole-corpus numbers -- ingest time, peak memory, batched extraction -- are on
the :doc:`performance` page, measured per release. Per-call latency, measured
on MANE v1.5 (19,363 genes) on the benchmark machine:

.. list-table::
   :header-rows: 1
   :widths: 50 25 25

   * - Call
     - Median
     - Path
   * - ``db[id]``
     - ~0.6 ms
     - primary-key lookup
   * - ``children(gene, level=1)``
     - ~5 ms
     - closure table
   * - ``children(gene)`` (all descendants)
     - ~10 ms
     - closure table
   * - ``region(seqid, start, end)``, 10 kb window
     - ~2 ms
     - R-tree

A per-feature loop pays those costs once per feature. That is why the batched
API exists: ``children_batched`` answers thousands of anchors in one query.
