.. _cookbook_refseq--ncbi-refseq-gff3-cookbook:

NCBI RefSeq GFF3 Cookbook
=========================

NCBI RefSeq ships organism-wide annotations as GFF3 with chromosome
records that can exceed 1 GB uncompressed. RefSeq's idiosyncrasies are
worth calling out:

- ``Dbxref=GeneID:7157,HGNC:HGNC:11998`` — multi-value cross-references.
- ``gbkey=Gene``, ``gbkey=mRNA``, ``gbkey=CDS`` — RefSeq's redundant
  parallel "GenBank key" tags.

- ``Note=...`` — long, free-form text often containing semicolons that
  confuse naïve parsers.

- One ``region`` row per sequence (``ID=NC_000001.11:1..248956422``,
  with ``chromosome=``, ``genome=`` and ``mol_type=`` attributes). It is
  metadata about the sequence, not a parent: genes carry no ``Parent=`` and
  are roots of their own hierarchies.

- Chromosome names like ``NC_000001.11`` instead of ``chr1``.
- Split CDS: every line of a discontinuous CDS repeats the same ``ID=``, and
  alignment rows (``match``, ``cDNA_match``) do the same.

GFFBase handles all of these -- the attribute parser treats quoted and
percent-encoded GFF3 values correctly -- but the split CDS needs one decision
at ingest, below. This cookbook shows the patterns that come up most often.

.. _cookbook_refseq--1-ingest-a-refseq-genome-annotation:

1. Ingest a RefSeq genome annotation
------------------------------------

.. docs-test: skip reason="needs the RefSeq GRCh38.p14 corpus"

.. code-block:: python

   from gffbase import create_db

   db = create_db(
       "GCF_000001405.40_GRCh38.p14_genomic.gff.gz",   # NCBI Human RefSeq
       "refseq.duckdb",
       force=True,
       merge_strategy="create_unique",   # one row per CDS line, as gffutils
   )
   print(db.fmt)   # 'gff3'

With the defaults this raises ``DuplicateIDError``, as gffutils does: the
lines of a split CDS share one ``ID``, and ``merge_strategy="error"`` is the
default. Choose a reading:

- ``merge_strategy="create_unique"`` keeps one row per line and renames the
  repeats (``cds-NP_000537.3_1``, ...) -- what a ported gffutils script
  expects.

- ``mode="strict"`` fuses the lines into one discontinuous feature per ``ID``,
  with per-segment phase; see :doc:`modes` and :doc:`schema_v2`.

RefSeq files are GFF3, so ``Parent=`` attributes drive edge construction
directly (no inference passes -- ``disable_infer_*`` is moot here).

.. _cookbook_refseq--2-convert-ncbi-seqids-to-ucsc-style-chromosomes:

2. Convert NCBI seqids to UCSC-style chromosomes
------------------------------------------------

.. code-block:: python

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

.. _cookbook_refseq--3-filter-on-gbkey-and-dbxref:

3. Filter on ``gbkey`` and ``Dbxref``
-------------------------------------

The normalized ``attributes`` table (``feature_id, key, value, idx``) makes
multi-value filters cheap:

.. code-block:: python

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

Note that ``Dbxref=GeneID:7157,HGNC:HGNC:11998`` is split into two rows in
``attributes`` (idx=0, idx=1), so each cross-reference can be filtered
independently.

.. _cookbook_refseq--4-read-long-note-values-without-semicolon-corruption:

4. Read long ``Note`` values without semicolon corruption
---------------------------------------------------------

The Rust parser's hand-written state machine honors ``note "a;b;c"``
quoted ranges and percent-escapes (``%3B`` → ``;``). Quotes are
seamless:

.. code-block:: python

   for f in db.features_of_type("exon", limit="NC_000001.11"):
       note = f.attributes.get("Note", [])
       if note and "pseudogene" in note[0].lower():
           print(f.id, note[0])

``f.attributes`` materializes lazily from the original col-9 bytes
(via the ``_LazyAttributes`` wrapper); features whose attributes are never read
pay zero parsing cost.

.. _cookbook_refseq--5-one-row-per-sequence-the-region-records:

5. One row per sequence: the ``region`` records
-----------------------------------------------

Each sequence has a ``region`` row describing it. It is not the parent of the
genes on that sequence -- nothing names it in ``Parent=`` -- so walk it by
coordinates or by ``seqid``, not by hierarchy:

.. code-block:: python

   # Which sequences are placed chromosomes, and what are they called?
   for r in db.features_of_type("region"):
       if r.attributes.get("genome") == ["chromosome"]:
           print(r.seqid, r.attributes["chromosome"][0], r.end)

   # Feature counts per sequence, in one query rather than a Python loop:
   for seqid, n in db.execute(
       "SELECT seqid, count(*) FROM features GROUP BY seqid ORDER BY 2 DESC LIMIT 5"
   ).fetchall():
       print(seqid, n)

.. _cookbook_refseq--6-bulk-export-to-bed-for-downstream-tools:

6. Bulk export to BED for downstream tools
------------------------------------------

.. code-block:: python

   with open("refseq.cds.bed", "w") as fout:
       for cds in db.features_of_type("CDS"):
           fout.write("\t".join(str(x) for x in [
               cds.seqid, cds.start - 1, cds.end,
               cds.id or ".",
               cds.score if cds.score != "." else "0",
               cds.strand,
           ]) + "\n")

.. _cookbook_refseq--performance-notes:

Performance notes
-----------------

On the benchmark machine (see :doc:`performance`), RefSeq GRCh38.p14 --
4.9 million lines -- ingests in about two minutes at the default 8 threads,
against gffutils' three and a half.

Ingest memory is bounded by default: the parser streams, and DuckDB runs under
a budget raised only for the steps that need it. ``pragmas={"memory_limit":
"2GB"}`` holds DuckDB to a fixed limit instead, and ``GFFBASE_THREADS``
changes the thread count; :doc:`tuning` has the measurements.
