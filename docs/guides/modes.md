---
title: Compatibility & strict modes
---

# Compatibility & strict modes

Real annotation files break the GFF3 specification. Routinely, and at every
major source. Any tool that reads them has to decide what to do about that, and
GFFBase makes the decision yours rather than guessing.

<!-- docs-test: skip reason="illustrative: names a file the reader supplies" -->
```python
create_db("annotation.gff3", "out.duckdb", mode="compat")   # default
create_db("annotation.gff3", "out.duckdb", mode="strict")
```

---

## The two axes underneath

`mode=` is a shorthand over two independent questions:

| Axis | Values | Question it answers |
| --- | --- | --- |
| `validation` | `"gffutils"`, `"ncbi"` | **Which rules apply?** |
| `on_error` | `"raise"`, `"warn"` | **What does a violation do?** |

And the two modes are just presets:

| Mode | `validation` | `on_error` | Behaviour |
| --- | --- | --- | --- |
| `compat` *(default)* | `gffutils` | `raise` | Every rule still runs, but a violation **annotates** the record instead of rejecting it. |
| `strict` | `ncbi` | `raise` | The full GFF3 specification. A violation **rejects** the line, with a line number. |

You can set them directly when a preset does not fit. Here is a file that
breaks the specification -- a CDS with no phase, which the GFF3 spec requires:

```python
from pathlib import Path

Path("messy.gff3").write_text("""\
##gff-version 3
chr1\tsrc\tgene\t1\t100\t.\t+\t.\tID=g1
chr1\tsrc\tCDS\t1\t50\t.\t+\t.\tID=c1;Parent=g1
""")
```

```python
from gffbase import create_db

# Apply the strict NCBI rules, but record violations instead of stopping.
audited = create_db("messy.gff3", "audit.duckdb", force=True,
                    validation="ncbi", on_error="warn")

for w in audited.warnings:
    print(w["line_no"], w["kind"], w["message"])
# 3 InvalidPhase CDS row missing required phase (must be 0, 1, or 2)

audited.close()
```

`on_error="warn"` is the one to reach for when you are auditing a file rather
than trusting it: the ingest completes, and `db.warnings` is the report.

---

## Why `compat` is the default

Because `gffutils` reads these files, and GFFBase is a drop-in replacement. A
default that rejected input its predecessor accepted would break every ported
script on day one, and the breakage would look like a GFFBase bug rather than a
long-standing property of the data.

Under `compat`, all 23 vendored upstream fixtures parse — including the
FlyBase file whose column 9 does not survive a strict reading.

---

## Discontinuous features: the case that actually matters

A CDS split across several lines shares one `ID=`. NCBI RefSeq and MANE both do
this; it is the single most common place the two modes visibly disagree.

Take three lines that all say `ID=cds-NP_001`:

=== "`mode="compat"`"

    Each line becomes **its own feature**, renamed the way
    `gffutils.merge_strategy="create_unique"` renames them — `cds-NP_001`,
    `cds-NP_001_1`, `cds-NP_001_2`. A ported script sees exactly what it
    expects to see.

=== "`mode="strict"`"

    The three lines become **one discontinuous feature** with three segments,
    stored in the `segments` table. This is what the GFF3 specification
    actually describes. `covered_length` sums the segments, `region()` decides
    overlap per segment rather than by the bounding envelope, and `to_lines()`
    reproduces all three input lines.

    ```python
    feature = db["cds-NP_001"]
    len(feature.segments)      # 3
    feature.covered_length     # sum of the three, not end - start
    ```

    See [Schema v2](../design/schema-v2.md) for the storage model.

!!! warning "Duplicate IDs are an error by default"
    `merge_strategy` defaults to `"error"` — as it does in `gffutils`, which
    raises on these same files. Ingesting RefSeq or MANE therefore requires
    saying which reading you want:

    ```python
    create_db(refseq, "out.duckdb", merge_strategy="create_unique")  # renamed
    create_db(refseq, "out.duckdb", mode="strict")                   # fused
    ```

    The default refuses rather than guessing, because both readings are
    defensible and a silent choice would give you a whole-genome answer nobody
    picked.

---

## What the parser enforces

Nine rules from the NCBI GFF3 specification, applied identically by the Rust
parser and the pure-Python fallback:

- column count, and non-empty required columns
- `start` / `end` parse as integers or `.`, and `start <= end`
- `strand` is one of `+`, `-`, `.`, `?`
- `phase` is `0`, `1`, `2` or `.` — and is **required** on a CDS
- `score` is a float or `.`
- no unescaped tab, newline, `;` or `=` inside an attribute value
- attribute pairs are well-formed `key=value`
- no whitespace in `seqid` or `featuretype`
- percent-encoding decodes

Under `validation="ncbi"` a violation rejects the line; under
`"gffutils"` the record is kept and annotated. Either way the error carries
`line_no`, `kind` and `message`, so you get a pointer into the file rather than
a stack trace:

```python
import gffbase

try:
    create_db("messy.gff3", "strict.duckdb", force=True, mode="strict")
except gffbase.GFFFormatError as exc:
    print(exc.line_no, exc.kind, exc.message)
# 3 InvalidPhase CDS row missing required phase (must be 0, 1, or 2)
```

---

## How the mode is recorded

The mode a database was built with is written into its `meta` table and read
back when you open it:

```python
from gffbase import FeatureDB

with FeatureDB("audit.duckdb") as reopened:
    print(reopened.mode)         # 'compat'
    print(reopened.validation)   # 'ncbi'   -- as it was built
    print(reopened.on_error)     # 'warn'
```

So a database always knows how it was made, and a script that reopens one does
not have to be told.

Mode also selects the `source` field of derived features — `gffutils_derived`
under `compat`, `gffbase_derived` under `strict` — so `create_introns()` output
round-trips through a `gffutils`-aware pipeline unchanged.
