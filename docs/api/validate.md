# validate

Post-ingest invariants. Each is a single set-based query, so the whole set is
cheap enough to run in CI — `gffbase validate --strict` is the command-line
form.

The one that matters most is INV-5: a fused feature whose envelope is narrower
than its segments simply stops being returned by `region()`, with nothing
raised anywhere. Errors and warnings are separate, because an error is a
broken invariant while a warning is something legal but suspect.

::: gffbase.validate
    options:
      show_root_heading: false
      show_root_toc_entry: false
      members_order: source
      filters:
        - "!^_"
