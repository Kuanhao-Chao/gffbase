# ---------------------------------------------------------------------------
# Author: Kuan-Hao Chao <kuanhao.chao@gmail.com>
# Copyright 2026 Kuan-Hao Chao
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
# ---------------------------------------------------------------------------
"""Compatibility helpers."""

from __future__ import annotations

import copy
from pathlib import Path


def merge_attributes(attr1, attr2, numeric_sort: bool = False) -> dict:
    """Union two attribute mappings, key by key.

    Values are deduplicated and **sorted**, not kept in insertion order: the
    result is a set union with a deterministic rendering, which is what makes
    two features merge to the same attributes regardless of which was seen
    first. `numeric_sort` sorts numerically where every value of a key parses
    as a number, so `["2", "10"]` does not come back as `["10", "2"]`.

    Used by every derived-feature method; ported from `gffutils.helpers` so
    that a caller comparing derived output against the oracle sees the same
    ordering.
    """
    merged = {
        k: list(v) if isinstance(v, list) else [v] for k, v in copy.deepcopy(dict(attr2)).items()
    }
    for key, values in copy.deepcopy(dict(attr1)).items():
        values = list(values) if isinstance(values, list) else [values]
        if key in merged:
            merged[key].extend(values)
        else:
            merged[key] = values

    if not numeric_sort:
        return {k: sorted(set(v)) for k, v in merged.items()}

    out = {}
    for key, values in merged.items():
        try:
            out[key] = [text for _, text in sorted((float(v), v) for v in set(values))]
        except ValueError:
            # Not every value is numeric; fall back to lexicographic rather
            # than raising, since a mixed key is normal in real annotations.
            out[key] = sorted(set(values))
    return out


def example_filename(fn: str) -> str:
    """Return the absolute path to a packaged example file under ``tests/data``.

    Mirrors the legacy ``gffutils.example_filename`` API. The legacy package
    shipped fixtures inside the package itself; we point at the test fixtures
    directory bundled with the source distribution.
    """
    here = Path(__file__).resolve().parent
    candidates = [
        here / "data" / fn,
        here.parent.parent / "tests" / "data" / fn,
        # The 32 fixtures copied verbatim from the gffutils corpus live here,
        # and they are exactly the names upstream's own examples use --
        # `FBgn0031208.gff` among them. Leaving this off the search path made
        # the canonical example fail even in a source checkout.
        here.parent.parent / "tests" / "data" / "upstream" / fn,
    ]
    for c in candidates:
        if c.is_file():
            return str(c)
    raise FileNotFoundError(
        f"example file not found: {fn}\n"
        "Example fixtures ship with the source distribution and the git "
        "checkout, under tests/data/. They are NOT in the binary wheel, so "
        "this helper cannot find them in a `pip install gffbase` environment. "
        "Install from source (`pip install --no-binary gffbase gffbase`) or "
        "point at your own file."
    )


#: Directory this package lives in. Upstream locates bundled example data
#: relative to it; `example_filename` below does the same, and this is exported
#: because callers use it to find their own files next to the package.
HERE = str(Path(__file__).resolve().parent)


def infer_dialect(attributes: str) -> dict:
    """Infer a dialect from one attribute string.

    Returns the full dialect dict, with everything the single string does not
    speak to left at its default.
    """
    from gffbase._pyfallback.attributes import parse_attributes
    from gffbase.constants import dialect as default

    _pairs, observed = parse_attributes(attributes)
    out = dict(default)
    out.update(observed)
    return out


def dialect_compare(dialect1: dict, dialect2: dict) -> dict:
    """What changed between two dialects, as `{"added": ..., "removed": ...}`.

    Unhashable values (`order` is a list) are compared by key rather than
    through a set of items, so this works on real dialects. The oracle's
    version raises `TypeError: unhashable type: 'list'` on any dialect that
    carries an `order`, which is all of them.
    """
    added = {k: v for k, v in dialect2.items() if k not in dialect1 or dialect1[k] != v}
    removed = {k: v for k, v in dialect1.items() if k not in dialect2 or dialect2[k] != v}
    return {"added": added, "removed": removed}


def to_unicode(obj, encoding: str = "utf-8"):
    """Decode bytes to `str`; pass anything else through.

    A Python-2-era shim. Upstream's body is unreachable after `2to3` and is the
    identity for every input, including bytes; this one actually decodes,
    because returning bytes from a function named `to_unicode` is not useful to
    anybody.
    """
    if isinstance(obj, bytes):
        return obj.decode(encoding)
    return obj


def is_gff_db(db_fname) -> bool:
    """True if the path looks like an existing database file.

    Extension-based, as upstream: `.db` for a gffutils database, plus
    `.duckdb`, which is what gffbase writes.
    """
    import os

    if isinstance(db_fname, os.PathLike):
        db_fname = os.fspath(db_fname)
    if not isinstance(db_fname, str) or not Path(db_fname).is_file():
        return False
    return db_fname.endswith((".db", ".duckdb"))


def get_gff_db(gff_fname, ext: str = ".db"):
    """Open the database beside a GFF file, or build one.

    Always returns a `FeatureDB`. Upstream returns a *path string* when a
    sibling database exists and a `FeatureDB` when it had to build one, which
    is why `gffutils-cli fetch` raises `TypeError: string indices must be
    integers` in its common case -- it indexes whatever it gets. Returning one
    type is the fix.

    The sibling filename is `<gff_fname><ext>`; upstream's `"%s.%s"` inserts a
    second dot, so it looks for `foo.gff..db`.
    """
    import os

    from gffbase.create_db import create_db
    from gffbase.interface import FeatureDB

    if isinstance(gff_fname, os.PathLike):
        gff_fname = os.fspath(gff_fname)
    if not Path(gff_fname).is_file():
        raise ValueError(f"GFF {gff_fname} does not exist.")

    candidate = f"{gff_fname}{ext}"
    if Path(candidate).is_file():
        return FeatureDB(candidate)
    return create_db(gff_fname, ":memory:", merge_strategy="merge", verbose=False)


def sanitize_gff_db(db, gid_field: str = "gid"):
    """Return a copy of `db` with coordinates ordered and a gene id stamped on.

    Two repairs, both aimed at making a file greppable and self-consistent:
    every record gets `start <= end`, and every record inherits its gene's id
    under `gid_field`, so a gene's whole block can be found with one `grep`.
    """
    from gffbase.create_db import create_db

    def sanitized():
        for gene_records in db.iter_by_parent_childs():
            gene_id = gene_records[0].id
            for rec in gene_records:
                if rec.start is not None and rec.end is not None and rec.start > rec.end:
                    rec.start, rec.end = rec.end, rec.start
                rec.attributes[gid_field] = [gene_id]
                yield rec

    return create_db(sanitized(), ":memory:", verbose=False)


def sanitize_gff_file(gff_fname, in_memory: bool = True, in_place: bool = False) -> None:
    """Sanitize a GFF file, writing to stdout or over the input."""
    import sys

    from gffbase.gffwriter import GFFWriter
    from gffbase.interface import FeatureDB

    if is_gff_db(gff_fname):
        db = FeatureDB(gff_fname)
    elif in_memory:
        from gffbase.create_db import create_db

        db = create_db(gff_fname, ":memory:", verbose=False)
    else:
        db = get_gff_db(gff_fname)

    writer = GFFWriter(gff_fname, in_place=in_place) if in_place else GFFWriter(sys.stdout)  # type: ignore[arg-type]
    sanitized = sanitize_gff_db(db, gid_field="gid")
    for gene in sanitized.all_features(featuretype="gene"):
        writer.write_gene_recs(sanitized, gene.id)
    writer.close()


def annotate_gff_db(db):
    """Cross-reference a GFF database against another.

    **Not implemented, and not implemented upstream either** -- gffutils'
    body is a bare `pass`, so it silently returns None. The name exists so an
    import resolves; calling it raises rather than pretending to work, because
    a function that quietly does nothing is worse than one that says so.
    """
    raise NotImplementedError(
        "annotate_gff_db is a stub upstream (its body is `pass`) and is not "
        "implemented here either."
    )


def canonical_transcripts(db, fasta_filename):
    """Yield `(transcript, sequence)` for the canonical transcript of each gene.

    Canonical means the longest CDS, falling back to the longest transcript
    when a gene has no CDS at all.

    Two upstream defects are not reproduced: its fallback sorts ascending and
    takes `[0]`, selecting the *shortest* transcript against its own comment;
    and it `print()`s an internal tuple to stdout on every gene, which corrupts
    any piped output.
    """
    import pyfaidx

    fasta = pyfaidx.Fasta(str(fasta_filename), as_raw=False)
    for gene in db.features_of_type("gene"):
        candidates = []
        for transcript in db.children(gene, level=1):
            exons = list(db.children(transcript, level=1))
            cds_len = sum(len(e) for e in exons if e.featuretype == "CDS")
            total_len = sum(len(e) for e in exons)
            if cds_len:
                parts = [
                    e
                    for e in exons
                    if e.featuretype in ("CDS", "five_prime_UTR", "three_prime_UTR")
                ]
            else:
                parts = exons
            candidates.append((cds_len, total_len, transcript, parts))

        if not candidates:
            continue
        if max(c[0] for c in candidates) > 0:
            best = max(candidates, key=lambda c: c[0])
        else:
            best = max(candidates, key=lambda c: c[1])

        _cds_len, _total_len, transcript, parts = best
        seqs = [
            part.sequence(fasta)
            for part in sorted(parts, key=lambda p: p.start, reverse=transcript.strand != "+")
        ]
        yield transcript, "".join(seqs)


def asinterval(feature):
    """Convert a `Feature` to a `pybedtools.Interval`."""
    import pybedtools

    return pybedtools.create_interval_from_list(str(feature).split("\t"))


def make_query(
    args,
    other=None,
    limit=None,
    strand=None,
    featuretype=None,
    extra=None,
    order_by=None,
    reverse=False,
    completely_within=False,
):
    """Compose a legacy SQLite query and its arguments.

    Provided for code that builds queries against an **exported** database, or
    that reads gffutils' generated SQL. gffbase's own queries do not go through
    it -- they are built in `gffbase.interface` against the DuckDB schema.

    One deviation, and it is the point of having this here: **`order_by` is
    validated even when it is a plain string.** Upstream checks its whitelist
    only for the iterable form and interpolates a bare string verbatim, which
    is the same class of hole that `FeatureDB.order_by` had. See
    `docs/security/2026-sql-injection.md`.

    !!! danger "`other` and `extra` are raw SQL"
        `featuretype`, `limit` and `strand` become bound parameters, and
        `order_by` is checked against a whitelist -- but **`other` and `extra`
        are interpolated verbatim**, because they exist to carry a caller's own
        SQL fragment (upstream builds its relation joins through `other`).
        Passing untrusted input to either is equivalent to passing it to
        `execute()`. No gffbase code path routes caller data into them; the
        asymmetry is documented here because the surrounding validation makes
        it easy to assume otherwise.
    """
    from gffbase import bins as _bins
    from gffbase.constants import _gffkeys_extra

    _QUERY = "{_SELECT} {OTHER} {EXTRA} {FEATURETYPE} {LIMIT} {STRAND} {ORDER_BY}"
    select = (
        "SELECT id, seqid, source, featuretype, start, end, score, strand, frame, "
        "attributes, extra, bin, features.rowid as file_order FROM features "
    )
    d = dict(
        _SELECT=select,
        OTHER=other or "",
        FEATURETYPE="",
        LIMIT="",
        STRAND="",
        ORDER_BY="",
        EXTRA=extra or "",
    )

    required_args = (d["EXTRA"] + d["OTHER"]).count("?")
    if len(args) != required_args:
        raise ValueError(f"Not enough args ({args}) for subquery")

    if featuretype:
        if isinstance(featuretype, str):
            d["FEATURETYPE"] = "features.featuretype = ?"
            args.append(featuretype)
        else:
            placeholders = ",".join("?" for _ in featuretype)
            d["FEATURETYPE"] = f"features.featuretype IN  ({placeholders})"
            args.extend(featuretype)

    if limit:
        if isinstance(limit, str):
            seqid, startstop = limit.split(":")
            start, end = startstop.split("-")
        else:
            seqid, start, end = limit

        overlapping = _bins.bins(int(start), int(end), one=False)
        if completely_within:
            d["LIMIT"] = "features.seqid = ? AND features.start >= ? AND features.end <= ?"
            args.extend([seqid, start, end])
        else:
            d["LIMIT"] = "features.seqid = ? AND features.start <= ? AND features.end >= ?"
            args.extend([seqid, end, start])

        # Above ~900 bins the IN list stops helping and SQLite's parameter
        # limits start to bite, so the clause is dropped and correctness is
        # left to the coordinate comparison.
        if len(overlapping) < 900:
            bin_list = ",".join(map(str, overlapping))
            d["LIMIT"] += f" AND features.bin IN ({bin_list})"

    if strand:
        d["STRAND"] = "features.strand = ?"
        args.append(strand)

    valid_order_by = _gffkeys_extra + ["file_order", "length"]
    if order_by:
        names = [order_by] if isinstance(order_by, str) else list(order_by)
        resolved = []
        for name in names:
            if name not in valid_order_by:
                raise ValueError(f"{name} not a valid order-by value in {valid_order_by}")
            # There is no length column, so order by the span.
            resolved.append("(end - start)" if name == "length" else name)
        direction = "DESC" if reverse else "ASC"
        d["ORDER_BY"] = f"ORDER BY {','.join(resolved)} {direction}"

    # Exactly one WHERE; everything after it is an AND.
    where = "where" in d["OTHER"].lower()
    for key in ("EXTRA", "FEATURETYPE", "LIMIT", "STRAND"):
        if d[key]:
            d[key] = ("AND " if where else "WHERE ") + d[key]
            where = True

    return _QUERY.format(**d), args
