# migrate

Upgrading a schema v1 database to v2, in place, in one transaction,
idempotently.

`migrate_v1_to_v2` is **structural**: it changes no query result, which is
what makes it acceptable to run unasked — `FeatureDB(..., upgrade="auto")`
does exactly that when it opens a v1 database.

`coalesce_multipart` is the separate, opt-in second step that re-fuses the
rows v1's `create_unique` split apart. That *does* change query results,
turning what were N features into one, so the caller has to ask.

::: gffbase.migrate
    options:
      show_root_heading: false
      show_root_toc_entry: false
      members_order: source
      filters:
        - "!^_"
