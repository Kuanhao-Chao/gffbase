.. _cookbook_mane--mane-matched-annotation-from-ncbi-and-ebi-cookbook:

MANE (Matched Annotation from NCBI and EBI) Cookbook
====================================================

The **MANE** project picks one transcript per protein-coding gene as
the canonical reference (``MANE_Select``) and adds a small
``MANE_Plus_Clinical`` set for clinically important alternates. Both
labels are exposed as ``tag`` attributes in the GENCODE GTF / Ensembl
GFF3 stream:

::

   9       HAVANA  transcript      ... gene_id "ENSG..."; transcript_id "ENST..."; tag "MANE_Select"; tag "Ensembl_canonical";

This cookbook shows how to filter for these tags efficiently using
GFFBase's normalized attribute index.

.. _cookbook_mane--1-ingest-gencodeensembl-then-filter:

1. Ingest GENCODE/Ensembl, then filter
--------------------------------------

.. docs-test: skip reason="illustrative: names gencode.v49, which the reader supplies"

.. code-block:: python

   from gffbase import create_db

   db = create_db("gencode.v49.chr_patch_hapl_scaff.basic.annotation.gtf.gz",
                  "gencode.duckdb", force=True)

   # Every MANE_Select transcript:
   mane_select = db.execute("""
       SELECT f.id, f.seqid, f.start, f."end", f.strand
       FROM features f
       JOIN attributes a ON a.feature_id = f.id
       WHERE f.featuretype = 'transcript'
         AND a.key = 'tag'
         AND a.value = 'MANE_Select'
       ORDER BY f.seqid, f.start
   """).fetchall()
   print(f"{len(mane_select):,} MANE_Select transcripts")

The ``attributes_kv`` index on ``(key, value)`` makes this an indexed seek,
not a full-table scan. On GENCODE v49 the query returns ~20 000 rows in
under 100 ms.

.. _cookbook_mane--2-combine-mane_select-and-mane_plus_clinical:

2. Combine ``MANE_Select`` and ``MANE_Plus_Clinical``
-----------------------------------------------------

.. code-block:: python

   mane_any = db.execute("""
       SELECT f.id, a.value AS mane_label
       FROM features f
       JOIN attributes a ON a.feature_id = f.id
       WHERE f.featuretype = 'transcript'
         AND a.key = 'tag'
         AND a.value IN ('MANE_Select', 'MANE_Plus_Clinical')
   """).fetchall()
   print(f"{len(mane_any):,} MANE-tagged transcripts")

.. _cookbook_mane--3-pull-the-mane-transcript-for-a-specific-gene:

3. Pull the MANE transcript for a specific gene
-----------------------------------------------

.. docs-test: skip reason="illustrative: uses a real accession id, not in the test fixtures"

.. code-block:: python

   gene_id = "ENSG00000139618"  # BRCA2
   row = db.execute("""
       SELECT f.id
       FROM features f
       JOIN attributes a_tag  ON a_tag.feature_id = f.id
       JOIN attributes a_gene ON a_gene.feature_id = f.id
       WHERE f.featuretype = 'transcript'
         AND a_tag.key = 'tag'  AND a_tag.value = 'MANE_Select'
         AND a_gene.key = 'gene_id' AND a_gene.value = ?
   """, [gene_id]).fetchone()
   mane_tx_id = row[0] if row else None
   print(f"BRCA2 MANE_Select transcript: {mane_tx_id}")

.. _cookbook_mane--4-get-every-exon-of-every-mane_select-transcript-vectorized:

4. Get every exon of every MANE_Select transcript (vectorized)
--------------------------------------------------------------

This is the bulk pattern that scales — pass *all* MANE transcript IDs
to ``children_batched`` for a zero-copy Arrow result:

.. code-block:: python

   mane_ids = [r[0] for r in db.execute("""
       SELECT f.id FROM features f
       JOIN attributes a ON a.feature_id = f.id
       WHERE f.featuretype = 'transcript' AND a.key = 'tag'
         AND a.value = 'MANE_Select'
   """).fetchall()]

   exons = db.children_batched(mane_ids, featuretype="exon", format="arrow")
   print(f"{exons.num_rows:,} exons across {len(mane_ids):,} MANE_Select transcripts")
   # Anchor column ('anchor') lets you group by transcript ID without a
   # Python loop:
   import pyarrow.compute as pc
   exon_count_per_tx = pc.value_counts(exons.column("anchor"))

.. _cookbook_mane--5-build-a-mane-only-sub-database-for-downstream-tooling:

5. Build a MANE-only sub-database for downstream tooling
--------------------------------------------------------

If you want a smaller ``.duckdb`` containing only the MANE subset, copy
through DuckDB's ``COPY ... TO``:

.. code-block:: python

   db.execute("""
       ATTACH 'mane_only.duckdb' AS mane_db (TYPE DUCKDB);
       CREATE TABLE mane_db.transcripts AS
       SELECT f.*, a.value AS mane_tag
       FROM features f
       JOIN attributes a ON a.feature_id = f.id
       WHERE a.key = 'tag' AND a.value IN ('MANE_Select','MANE_Plus_Clinical');
   """)

.. _cookbook_mane--why-this-is-fast:

Why this is fast
----------------

GENCODE's ``tag`` attribute appears on ~3 M attribute rows in the
normalized table. The ``attributes_kv`` index turns the
``WHERE key='tag' AND value='MANE_Select'`` predicate into a single
B-tree seek that returns ~19 k feature_ids. Compare to the legacy
``gffutils`` model where the entire col-9 JSON blob has to be parsed for
every feature row before the tag can even be examined.
