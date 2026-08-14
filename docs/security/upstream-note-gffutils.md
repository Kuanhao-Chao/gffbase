# DRAFT — private report to the gffutils maintainer

> **Status: DRAFT, NOT SENT.** Nothing has been emailed, filed, or disclosed.
> Whether to report this, to whom, and on what timeline are decisions for the
> maintainer of this project. gffutils has a `SECURITY.md`-equivalent contact
> path via GitHub private vulnerability reporting on `daler/gffutils`.

Prepared while auditing gffbase (a successor project) against gffutils 0.14,
commit `6b84330`.

---

## Subject

SQL injection in `FeatureDB.set_pragmas()`

## Summary

`gffutils.interface.FeatureDB.set_pragmas()` interpolates both the pragma name
and its value into a statement that is then run through
`sqlite3.Cursor.executescript()`. Because `executescript` exists specifically
to execute multiple statements, a caller-supplied value containing `;` runs
arbitrary SQL against the user's database.

## Affected code

`gffutils/interface.py:236-248`:

```python
def set_pragmas(self, pragmas):
    """
    Set pragmas for the current database connection.
    ...
    """
    self.pragmas = pragmas
    c = self.conn.cursor()
    c.executescript(";\n".join(["PRAGMA %s=%s" % i for i in self.pragmas.items()]))
```

`set_pragmas` is reachable from `create_db(..., pragmas=...)` and from
`FeatureDB(..., pragmas=...)`, both documented public parameters.

## Proof of concept

```python
import gffutils

db = gffutils.create_db("annotations.gff3", ":memory:")
db.set_pragmas({"page_size": "4096; DROP TABLE relations"})

db.conn.execute("SELECT count(*) FROM relations").fetchone()
# sqlite3.OperationalError: no such table: relations
```

The pragma *name* is interpolated too, so guarding only the value would be
insufficient.

## Why this is worth a fix even though SQLite is "safe by default"

The usual protection — `sqlite3.Cursor.execute()` refusing more than one
statement — does not apply, because this call site uses `executescript`.
By comparison, the `order_by` interpolation in `helpers.make_query()`
(`helpers.py:259-260`, where a plain string bypasses the `valid_order_by`
whitelist entirely) reaches `execute()` and is therefore *not* exploitable in
the same way, despite looking more dangerous.

So `set_pragmas` is, as far as I can tell, the one place in gffutils where the
interpolation actually executes.

## Suggested fix

Bind nothing (SQLite pragmas do not accept parameters), but validate:

```python
import re

_PRAGMA_NAME = re.compile(r"\A[A-Za-z_][A-Za-z0-9_]*(\.[A-Za-z_][A-Za-z0-9_]*)?\Z")

def set_pragmas(self, pragmas):
    self.pragmas = pragmas
    c = self.conn.cursor()
    for name, value in pragmas.items():
        if not _PRAGMA_NAME.match(str(name)):
            raise ValueError(f"invalid pragma name: {name!r}")
        if isinstance(value, bool):
            literal = "1" if value else "0"
        elif isinstance(value, (int, float)):
            literal = str(value)
        else:
            literal = "'" + str(value).replace("'", "''") + "'"
        c.execute(f"PRAGMA {name}={literal}")
    self.conn.commit()
```

The name pattern allows the schema-qualified forms gffutils itself uses in
`constants.default_pragmas` (`main.page_size`, `main.cache_size`). Switching
from `executescript` to per-statement `execute` also restores SQLite's
one-statement protection as defence in depth.

## Impact

Arbitrary SQL against the user's database. Lower severity than the equivalent
in gffbase: SQLite cannot read or write arbitrary files the way DuckDB's
`read_csv` / `COPY … TO` can, so the blast radius is the database itself
(`ATTACH` can reach other database files the process can already open).

Exploitation requires an application to pass untrusted input into `pragmas`,
which is unusual — a fixed tuning dict is the normal usage.

## Disclosure

No public disclosure has been made. gffbase's own advisory for the same defect
class is held as a draft and will note the upstream status according to
whatever you prefer.
