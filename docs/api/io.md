# I/O — `DataIterator` & `GFFWriter`

## `DataIterator`

Factory function that returns a streaming iterator over GFF3/GTF input
(file path, URL, raw string with `from_string=True`, or an iterable of
`Feature` objects).

::: gffbase.iterators.DataIterator
    options:
      show_root_heading: true
      show_signature_annotations: true
      separate_signature: true

## `GFFWriter`

::: gffbase.gffwriter.GFFWriter
    options:
      show_root_heading: true
      members_order: source
      show_signature_annotations: true
      separate_signature: true
      filters:
        - "!^_"

## `export_sqlite`

Serialize a GFFBase DuckDB connection back into a legacy
`gffutils`-compatible SQLite database.

::: gffbase.sqlite_export.export_sqlite
    options:
      show_root_heading: true
      show_signature_annotations: true
      separate_signature: true

## Low-level parser

::: gffbase.parser.parse_gff
    options:
      show_root_heading: true
      show_signature_annotations: true
      separate_signature: true

::: gffbase.parser.parse_bytes
    options:
      show_root_heading: true
      show_signature_annotations: true
      separate_signature: true

::: gffbase.parser.detect_dialect
    options:
      show_root_heading: true
      show_signature_annotations: true
      separate_signature: true

::: gffbase.parser.native_available
    options:
      show_root_heading: true
      show_signature_annotations: true
      separate_signature: true
