# Command line

Installing gffbase puts a `gffbase` command on your `PATH`. It also works as
`python -m gffbase`, which is useful when several environments are in play and
you want to be certain which interpreter is answering.

```bash
gffbase --version
gffbase --help
```

## Compatibility with `gffutils-cli`

Argument names and output shapes follow `gffutils-cli`, so a script written
against it keeps working. What differs is how much of it runs.

Of the thirteen commands `gffutils-cli` defines, **five work**:

| Command | Upstream | gffbase |
|---|---|---|
| `create` | works | works |
| `children` | works | works, and `--level` is honoured |
| `parents` | works | works, and `--level` is honoured |
| `rmdups` | works, but prints its banner into the GFF | works; progress on stderr |
| `sanitize` | works | works |
| `fetch` | `TypeError` — its helper returns a path string that the command indexes as a database | works |
| `search` | `AttributeError` — calls `db.attribute_search()`, which does not exist in gffutils | works |
| `region` | `NotImplementedError` | works |
| `clean` | `NotImplementedError` | not provided |
| `common` | `NotImplementedError` | not provided |
| `annotate` | defined but never registered — unreachable | not provided |
| `convert` | defined but never registered | not provided |
| — | — | `validate` (gffbase only) |
| — | — | `migrate` (gffbase only) |

Two conventions hold throughout:

**Feature output goes to stdout; progress goes to stderr.** So
`gffbase rmdups in.gff > out.gff` produces a valid file. (`gffutils-cli rmdups`
prints `Removing duplicates from: …` to stdout, into the middle of the GFF it
is writing.)

**A command that could not do what was asked says so in its exit status**, not
only in a message — `fetch` returns 1 when an id is missing, and
`validate --strict` returns 1 on any violation.

---

## Building a database

```bash
gffbase create annotations.gff3 --output annotations.duckdb
```

| Option | Default | Meaning |
|---|---|---|
| `--output` | `<input>.duckdb` | Where to write |
| `--force` | off | Overwrite an existing database |
| `--quiet` | off | Suppress progress |
| `--merge` | `merge` | Duplicate-ID strategy: `error`, `warning`, `merge`, `create_unique`, `replace` |
| `--disable-infer-genes` | off | GTF: do not synthesize gene rows |
| `--disable-infer-transcripts` | off | GTF: do not synthesize transcript rows |
| `--mode` | `compat` | `compat` reads what gffutils reads; `strict` applies the full GFF3 specification and fuses discontinuous features |

`--mode` is the one flag worth understanding before a large run. `compat`
accepts the specification violations real annotation files contain, because
that is what gffutils does and what most pipelines expect. `strict` rejects
them, and is also the only mode that fuses several lines sharing an `ID` into
one discontinuous feature.

## Querying

```bash
# By ID. Exit status is 1 if any id was not found.
gffbase fetch annotations.duckdb gene1,gene2

# Descendants. Without --level, the whole subtree.
gffbase children annotations.duckdb gene1
gffbase children annotations.duckdb gene1 --level 1
gffbase children annotations.duckdb gene1 --exclude exon,CDS
gffbase children annotations.duckdb gene1 --exclude-self

# Ancestors, same options.
gffbase parents annotations.duckdb exon1

# By position.
gffbase region annotations.duckdb chr1:1000-2000
gffbase region annotations.duckdb chr1:1000-2000 --featuretype exon
gffbase region annotations.duckdb chr1:1000-2000 --completely-within

# By attribute value, case-insensitively. SQL LIKE syntax, so % and _ are
# wildcards; a bare string matches as a substring.
gffbase search annotations.duckdb BRCA1
gffbase search annotations.duckdb BRCA% --featuretype gene
```

Every one of these accepts a GFF or GTF file in place of a database, building
one in memory first. That is convenient for a one-off question and wasteful in
a loop — build the database once with `gffbase create` if you are going to ask
more than one.

## Rewriting files

```bash
# Collapse duplicate IDs.
gffbase rmdups messy.gff3 > clean.gff3
gffbase rmdups messy.gff3 --in-place

# Order coordinates so start <= end, and stamp each record with its gene's id
# under `gid` so a whole gene can be found with one grep.
gffbase sanitize messy.gff3 > clean.gff3
gffbase sanitize messy.gff3 --in-place
```

## gffbase-only commands

### `validate`

Runs the post-ingest invariants against an existing database. Each is a single
set-based query, so it is fast enough to run in CI.

```bash
gffbase validate annotations.duckdb
gffbase validate annotations.duckdb --level fast
gffbase validate annotations.duckdb --strict
```

Errors and warnings are reported separately because they mean different
things: an error is a broken invariant, a warning is something the data does
that is legal but suspect. **Only errors fail by default**; `--strict` fails on
warnings too, which is the form to use in CI.

```
ok      INV-1 (unique_ids)
ok      INV-2 (no_orphan_segments)
...
15 invariants checked, 0 error(s), 0 warning(s)
```

### `migrate`

Upgrades a schema v1 database in place, in one transaction, idempotently.

```bash
gffbase migrate old.duckdb
gffbase migrate old.duckdb --coalesce
```

The migration proper is **structural**: it changes no query result, which is
what makes it safe to run unasked (and `FeatureDB` does run it automatically
when it opens a v1 database).

`--coalesce` is a separate, opt-in second step that re-fuses the rows v1's
`create_unique` split apart — turning what were N features into one. That
*does* change query results, so it only happens when you ask.

## Exit statuses

| Status | Meaning |
|---|---|
| 0 | Success |
| 1 | The command ran but could not do what was asked — a missing id in `fetch`, a violation in `validate` |
| 2 | Usage error: no command, an unknown flag, a bad argument |
