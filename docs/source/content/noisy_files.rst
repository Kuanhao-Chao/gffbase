.. _noisy-files:

Noisy and non-standard files
============================

Real annotation files break the GFF3 specification routinely: gene predictors
write GTF without exon rows, browsers write the same transcript id on several
contigs, editors add a byte-order mark, and one of the biggest model-organism
databases ships its GFF as a tar archive. gffbase reads each of these the way
it was meant, in both engines, and says what it did.

Everything on this page applies to ``mode="compat"`` (the default). Where the
result differs from ``gffutils``, the difference is declared in
``tests/parity/deviations.toml`` and pinned by a test in
``tests/test_noisy_input.py``.

Where problems are reported
---------------------------

A line gffbase keeps but that breaks a rule is kept *and* reported: after
``create_db``, ``db.warnings`` lists each one with its line number, kind and
message. A line it cannot use at all -- not valid UTF-8, or binary -- is
skipped and reported the same way. Under ``mode="strict"`` the same problems
raise ``GFFFormatError`` instead, naming the line.

Lines and bytes
---------------

.. list-table::
   :header-rows: 1
   :widths: 40 60

   * - In the file
     - What gffbase does
   * - A UTF-8 byte-order mark
     - Ignored; it is not part of the first seqid (``gffutils`` reads
       ``﻿chr1``).
   * - Lines ending in a lone ``\r`` (classic Mac), or ``\r\n``
     - Every ``\n``, ``\r\n`` and ``\r`` ends a line.
   * - A line of only spaces or tabs
     - A blank line (``gffutils`` makes it a feature named after its spaces).
   * - A line holding a NUL byte
     - Binary data: reported and skipped. A file that is all binary raises
       ``EmptyInputError``.
   * - A line that is not valid UTF-8
     - Reported and skipped.
   * - A gzip file, whatever its name (``.gz``, ``.bgz``, none)
     - Recognized by content; every member of a bgzip stream is read.
   * - A truncated or corrupt gzip file
     - The complete lines before the damage are read, then
       ``GFFFormatError(kind="ReadError")`` -- whatever the strictness, since
       the input stops there.
   * - A tar archive holding one file (FlyBase's ``dmel-all-*.gff.gz``)
     - Read as the file it holds. An archive of several files is refused.

Column 9
--------

.. list-table::
   :header-rows: 1
   :widths: 40 60

   * - In the file
     - What gffbase does
   * - ``.`` as the whole column
     - No attributes (``gffutils`` reads a key named ``.``).
   * - ``ID = g1 ; Name = x`` -- spaces around a spaced ``=``
     - The spaces are separator, not data: ``ID`` is ``g1``. A value that is
       only a space (``Note=`` then one space) is still data.
   * - ``Parent=a,`` -- a trailing comma
     - The value list is ``["a", ""]``, as gffutils reads it, but no edge is
       made to an empty parent.
   * - Quoted GFF3 values, ``ID="g1"``
     - Read as ``g1`` in compat mode, as gffutils does.

GTF from gene predictors and browsers
-------------------------------------

.. list-table::
   :header-rows: 1
   :widths: 40 60

   * - In the file
     - What gffbase does
   * - CDS rows and no exon rows (AUGUSTUS without UTRs, GeneMark)
     - Transcripts and genes are inferred from the CDS rows. (gffutils infers
       from exons only, so every CDS stays an orphan.) An explicit
       ``gtf_subfeature`` is honoured as given.
   * - A row with ``gene_id`` and no ``transcript_id``
     - Linked directly to its gene, and reported in ``db.warnings``.
   * - AUGUSTUS ``gene`` / ``transcript`` rows whose column 9 is a bare id
       (``g1``, ``g1.t1``)
     - Read as that row's ``gene_id`` / ``transcript_id``, so the hierarchy
       holds together.
   * - An authored transcript with no ``gene_id``
     - Takes the gene its children name.
   * - One ``transcript_id`` on several contigs (UCSC ``knownGene``,
       ``ncbiRefSeq``)
     - Refused by default with ``SynthesisConflictError``;
       ``merge_strategy="create_unique"`` builds one parent per contig.
   * - ``gene_id "X"; transcript_id "X"`` (UCSC)
     - One inferred gene ``X`` -- no suffixed copy is invented.

Identifiers
-----------

.. list-table::
   :header-rows: 1
   :widths: 40 60

   * - In the file
     - What gffbase does
   * - The same ``ID`` on several lines (split CDS: RefSeq, MANE, GENCODE
       GFF3)
     - ``DuplicateIDError`` by default, naming both lines; choose a reading
       with ``merge_strategy`` (``create_unique`` renames, ``merge`` folds) or
       ``mode="strict"`` (one discontinuous feature).
   * - Ids that already end in ``_1``, ``_2`` (Liftoff's extra copies)
     - Renames and generated ids skip names the file uses literally, where
       gffutils fails on a uniqueness constraint.

Tested on real files
--------------------

Besides the five human corpora the benchmarks use, nine files from other
sources are ingested and fully validated by ``tests/test_corpus_extended.py``:
Ensembl mouse (GFF3 and GTF), NCBI *E. coli*, FlyBase, WormBase (GFF3 and the
canonical GTF), UCSC ``knownGene`` and ``ncbiRefSeq``, and a T2T Liftoff
annotation. See :doc:`datasets` for where they come from.
