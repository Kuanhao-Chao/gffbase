# DRAFT security advisory — SQL injection via `order_by` and `set_pragmas`

> **Status: DRAFT, not published.** This file is a prepared advisory for review.
> Nothing has been filed with GitHub, sent to PyPI, or otherwise disclosed.
> Publishing it, requesting a CVE, and yanking or re-releasing any artefact are
> all decisions for the maintainer.

## Summary

Two `FeatureDB` parameters were interpolated directly into SQL:

1. **`order_by`**, on `all_features` / `features_of_type` / `children` /
   `parents`;
2. **`set_pragmas`**, which interpolated both the pragma *name* and its
   *value*.

A caller who passes attacker-influenced text to either allows arbitrary SQL —
including DDL and DML — to run against their database. They are one defect
class, found in the same review of the query builders and fixed in the same
release, which is why they share an advisory.

## Affected versions

| Version | Status |
|---------|--------|
| 0.1.0 | Affected — **the only published release** (PyPI, tag `v0.1.0`) |
| 0.2.0 | Fixed |

0.1.1 appears in neither row on purpose: it was prepared in tree but never
tagged and never published, and its changes ship inside 0.2.0. Nobody can be
running it unless they installed from a git checkout of the branch.

So the exposed population is exactly the people who installed 0.1.0 from
PyPI, plus anyone tracking the branch.

## Severity

Assessed by the maintainer. The relevant factors:

- it is reachable from a **documented public parameter**, not an internal one;
- the payload runs with the full authority of the caller's DuckDB connection,
  which can also read and write the local filesystem (`read_csv`, `COPY … TO`);
- it requires the application to pass untrusted input into `order_by` or
  `set_pragmas`, which is plausible for a web service or notebook exposing a
  sort control or a tuning form, and not plausible for a batch analysis script
  with hard-coded values;
- `set_pragmas` additionally **swallowed every exception**, so an unsuccessful
  attempt left no trace in logs or return values.

`SECURITY.md` names "SQL injection through any public API parameter" as in
scope, and closes with: *"Everything else that reaches SQL must be parameterized
or whitelisted, and a failure to do so is in scope."* This is that failure.

## Affected API

Every method taking `order_by`:

- `FeatureDB.all_features()`
- `FeatureDB.features_of_type()`
- `FeatureDB.children()`
- `FeatureDB.parents()`

The joined paths (`children`, `parents`) carried their own copy of the
pass-through, so fixing only the unjoined ones would have left them exposed.

And:

- `FeatureDB.set_pragmas()`

## Details — 1. `order_by`

`order_by` was resolved against a small set of known column names, and anything
unrecognized was interpolated verbatim — an intentional escape hatch described
in the code as being "for power users":

```python
else:
    # Unknown / multi-field — accept as a literal for power users.
    col = order_by
...
return f"{col} {direction}"
```

On SQLite this is inert, because `sqlite3` refuses to execute more than one
statement per call. **gffbase runs on DuckDB, which executes trailing
statements**, so the escape hatch became an injection when the storage engine
changed. gffutils contains the same interpolation and is not exploitable for
that reason alone.

### Proof of concept

```python
import gffbase

db = gffbase.create_db("annotations.gff3", ":memory:")

payload = (
    'start ASC; DROP TABLE attributes; '
    'SELECT id, seqid, source, featuretype, start, "end", score, strand, '
    'frame, attributes_blob, extra_blob, file_order FROM features ORDER BY start'
)

list(db.all_features(order_by=payload))   # returns rows as normal
```

The `attributes` table is gone. The trailing `SELECT` re-supplies exactly the
projection the result generator expects, so the call **returns a normal-looking
feature list and raises nothing** — the damage is invisible from the call site.
Any statement DuckDB accepts can be substituted, including `COPY … TO` to write
files, or `ATTACH` to reach another database.

## Details — 2. `set_pragmas`

`set_pragmas` built its statement from a caller-supplied dict, interpolating
**both** the key and the value, and wrapped the whole loop body in a bare
handler:

```python
for k, v in pragmas.items():
    try:
        self.conn.execute(f"PRAGMA {k} = {v}")
    except duckdb.Error:
        continue
```

### Proof of concept

```python
db = gffbase.create_db("annotations.gff3", ":memory:")
db.set_pragmas({"threads": "1; DROP TABLE attributes"})
# raises nothing
db.conn.execute("SELECT count(*) FROM attributes")
# CatalogException: Table with name attributes does not exist!
```

The key is exploitable the same way (`{"threads = 1; DROP TABLE attributes;
SET threads": 1}`), so a fix that guarded only the value would have left it
open. Verified by mutation: reintroducing either half alone fails the suite.

The `except duckdb.Error: continue` existed for a real compatibility reason —
ported gffutils code passes `constants.default_pragmas`
(`synchronous`, `journal_mode`, `main.page_size`, `main.cache_size`), none of
which DuckDB has — but it could not distinguish "this is a SQLite pragma" from
"DuckDB rejected this", so it hid the failed attempts too.

### The oracle is affected here, unlike `order_by`

gffutils' `set_pragmas` is:

```python
c.executescript(";\n".join(["PRAGMA %s=%s" % i for i in self.pragmas.items()]))
```

`executescript` exists precisely to run several statements, so SQLite's
one-statement rule does not apply. **gffutils 0.14 is genuinely vulnerable
through `set_pragmas`.** This has not been reported upstream; see the
checklist.

## Impact

Arbitrary SQL execution against the caller's database, and through DuckDB's
filesystem functions, arbitrary local file read and write as the running user.
Silent, because the query still succeeds.

## Fix

`order_by` is now a whitelist of sort keys, and both clause builders share one
resolver so a future entry point cannot reacquire an escape hatch. Anything
outside the whitelist raises `ValueError` naming the accepted set.

The whitelist is not a restriction: it *restores* the contract gffutils
documents (`order_by` items "must be in: 'seqid', 'source', 'featuretype',
'start', 'end', 'score', 'strand', 'frame', 'attributes', 'extra'"). Three of
those documented forms were in fact broken before the fix — `attributes` and
`extra` raised a binder error, a tuple silently sorted by nothing, and a list
raised `TypeError`.

Fix commit: `89d9cc3` — *"fix: make order_by a whitelist, closing a live SQL
injection"*.

`set_pragmas` now matches each name against DuckDB's own settings catalog
(`SELECT name FROM duckdb_settings()`) and renders values as SQL literals, so
neither reaches the parser as syntax. Matching the live catalog rather than a
hardcoded list means the check cannot go stale against a newer DuckDB. The
compatibility behaviour is preserved and now deliberate: a name that is not a
DuckDB setting is skipped (and logged at debug), where previously it was
indistinguishable from a swallowed error.

Regression tests: `tests/test_sql_injection_safety.py`, which runs the payloads
above against all five entry points and asserts the table survives.

## Mitigation without upgrading

Validate before passing the value in:

```python
ALLOWED = {
    "id", "seqid", "source", "featuretype", "start", "end",
    "score", "strand", "frame", "attributes", "extra", "file_order", "length",
}

def safe_order_by(value):
    if value is None:
        return None
    names = value.split(",") if isinstance(value, str) else list(value)
    names = [n.strip() for n in names]
    if not all(n in ALLOWED for n in names):
        raise ValueError(f"rejected order_by: {value!r}")
    return names
```

Note that on 0.1.x a tuple or list is not honoured (it sorts by nothing rather
than raising), so pass a single validated name.

For `set_pragmas`, do not pass caller-supplied keys or values at all. If you
must, check the name against DuckDB's catalog and keep values non-string:

```python
KNOWN = {r[0] for r in db.conn.execute("SELECT name FROM duckdb_settings()").fetchall()}

def safe_pragmas(d):
    out = {}
    for k, v in d.items():
        if k not in KNOWN:
            continue
        if not isinstance(v, (bool, int, float)):
            raise ValueError(f"rejected pragma value: {k}={v!r}")
        out[k] = v
    return out
```

The same mitigation applies to gffutils 0.14, which is vulnerable through
`set_pragmas` and has no fix available.

## Credit

Found during the pre-0.2.0 security review of the query builders: `order_by`
while whitelisting it for the release, and `set_pragmas` in the follow-up audit
of every remaining f-string SQL site. That audit found no third instance —
`migrate.py` interpolates module constants, the ingest staging INSERTs derive
their column lists from the Arrow schema, the thread pragma coerces through
`int()`, and the region/relation CTEs interpolate only internally-derived
fragments with all caller values bound.

## Checklist before publishing

- [ ] Decide severity and CVSS
- [ ] Report `set_pragmas` to the gffutils maintainer privately — 0.14 is
      affected and unpatched (draft in `docs/security/upstream-note-gffutils.md`)
- [ ] Decide whether 0.1.x gets a backported patch release or is yanked
- [ ] File the GitHub Security Advisory (draft privately first)
- [ ] Request a CVE if warranted
- [ ] Reference the advisory from `CHANGELOG.md` once it has an ID
