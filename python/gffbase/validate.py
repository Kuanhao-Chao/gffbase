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
"""Post-ingest invariants.

Schema v2 spreads one logical feature across two tables, and the interesting
failure mode is not a crash -- it is a database that answers every query
plausibly and wrongly. The worst case is INV-5: if a fused feature's envelope
is narrower than its segments, `region()` silently stops returning it. Nothing
raises, no count looks odd, and the feature is simply gone from results.

So these run automatically at the end of a strict-mode ingest, and can be run
by hand at any time. Every check is a single set-based query returning a count
plus a few examples, so validating a GENCODE-scale database is a handful of
aggregate scans rather than a row-by-row walk.

Severity is not decoration. An `error` means the database will give wrong
answers; a `warning` means something is unusual but defensible in real data --
abutting segments, for instance, occur in files people actually ship.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

import duckdb

_log = logging.getLogger("gffbase.validate")

ERROR = "error"
WARNING = "warning"

#: `full` additionally re-parses stored attribute blobs, which is the only
#: check that scales with feature count rather than with table count.
LEVELS = ("fast", "full")

#: How many offending rows to carry back with a violation. Enough to debug
#: from, few enough that a database with a million broken rows does not
#: produce a million-line report.
_EXAMPLE_LIMIT = 5


@dataclass(frozen=True)
class Violation:
    """One invariant that did not hold."""

    invariant: str
    name: str
    severity: str
    count: int
    detail: str
    examples: tuple = ()

    def __str__(self) -> str:
        head = f"{self.invariant} ({self.name}): {self.detail} [{self.count} row(s)]"
        if not self.examples:
            return head
        return head + "\n  e.g. " + "\n       ".join(repr(e) for e in self.examples)


@dataclass
class ValidationReport:
    """What validation found. Falsy when anything failed at `error` severity."""

    level: str
    checked: list[str] = field(default_factory=list)
    violations: list[Violation] = field(default_factory=list)
    #: Checks skipped because the database predates the structure they test.
    skipped: list[str] = field(default_factory=list)

    @property
    def errors(self) -> list[Violation]:
        return [v for v in self.violations if v.severity == ERROR]

    @property
    def warnings(self) -> list[Violation]:
        return [v for v in self.violations if v.severity == WARNING]

    @property
    def ok(self) -> bool:
        """True when nothing failed at `error` severity. Warnings do not count:
        they flag data that is unusual, not a database that answers wrongly."""
        return not self.errors

    def __bool__(self) -> bool:
        return self.ok

    def __str__(self) -> str:
        if not self.violations:
            return f"validate({self.level}): {len(self.checked)} invariants held"
        return f"validate({self.level}): " + "; ".join(str(v) for v in self.violations)

    def raise_for_status(self) -> None:
        """Raise if any invariant failed at `error` severity."""
        if self.errors:
            raise ValidationError(
                f"{len(self.errors)} invariant(s) violated:\n"
                + "\n".join(str(v) for v in self.errors)
            )


class ValidationError(AssertionError):
    """A database violates an invariant that makes its answers wrong.

    `AssertionError` rather than `ValueError`: this is never bad input from the
    caller, it is gffbase having produced or been handed a database whose
    internal structure contradicts itself.
    """


# ---------------------------------------------------------------------------
# The checks
#
# Each returns (count, examples) from ONE query. The registry below pairs them
# with their number, name and severity so `validate_db` is a loop rather than a
# list of hand-written calls that can drift out of sync with the report.
# ---------------------------------------------------------------------------


def _count(con, sql: str) -> tuple[int, tuple]:
    """Run an offender-listing query; return how many and a few examples."""
    rows = con.execute(sql).fetchall()
    return len(rows), tuple(rows[:_EXAMPLE_LIMIT])


def _inv1_unique_ids(con):
    return _count(con, "SELECT id, COUNT(*) FROM features GROUP BY id HAVING COUNT(*) > 1")


def _inv2_no_orphan_segments(con):
    return _count(
        con,
        "SELECT s.feature_id, s.seg_idx FROM segments s "
        "WHERE NOT EXISTS (SELECT 1 FROM features f WHERE f.id = s.feature_id)",
    )


def _inv3_n_segments_matches(con):
    return _count(
        con,
        """
        SELECT f.id, f.n_segments, COALESCE(s.n, 0)
        FROM features f
        LEFT JOIN (SELECT feature_id, COUNT(*) AS n FROM segments GROUP BY feature_id) s
               ON s.feature_id = f.id
        WHERE f.n_segments <> COALESCE(s.n, 1)
        """,
    )


def _inv4_seg_idx_dense(con):
    return _count(
        con,
        """
        SELECT feature_id, lo, hi, n FROM (
            SELECT feature_id, MIN(seg_idx) AS lo, MAX(seg_idx) AS hi,
                   COUNT(*) AS n, COUNT(DISTINCT seg_idx) AS d
            FROM segments GROUP BY feature_id
        ) WHERE lo <> 0 OR hi <> n - 1 OR d <> n
        """,
    )


def _inv5_envelope_exact(con):
    """The critical one.

    A too-narrow envelope removes the feature from `region()` without any
    error; a too-wide one is only masked by the segment recheck. Either way the
    database answers plausibly and wrongly, which is exactly the class of
    failure a validator exists for.
    """
    return _count(
        con,
        """
        SELECT f.id, f.start, f."end", s.mn, s.mx
        FROM features f
        JOIN (SELECT feature_id, MIN(start) AS mn, MAX("end") AS mx
              FROM segments GROUP BY feature_id) s ON s.feature_id = f.id
        WHERE f.start IS DISTINCT FROM s.mn OR f."end" IS DISTINCT FROM s.mx
        """,
    )


def _inv6_edges_resolve(con):
    """Edge endpoints that name no feature.

    A WARNING, not an error, and calibrated against the oracle rather than
    against the specification: `mouse_extra_comma.gff3` has `Parent=a,` which
    yields an empty parent id, and gffutils writes that relation too --
    `('', 'e1', 1)` -- so refusing it would be a divergence, not a repair. Real
    files also reference parents that live in another file entirely.

    It is still worth reporting: a dangling endpoint means `parents()` and
    `children()` will name something `db[...]` cannot fetch.
    """
    return _count(
        con,
        """
        SELECT parent, child, which FROM (
            SELECT parent, child, 'parent' AS which FROM edges
            WHERE NOT EXISTS (SELECT 1 FROM features f WHERE f.id = edges.parent)
            UNION ALL
            SELECT parent, child, 'child' AS which FROM edges
            WHERE NOT EXISTS (SELECT 1 FROM features f WHERE f.id = edges.child)
        )
        """,
    )


def _inv7_segments_consistent(con):
    """Segments must not overlap each other.

    A warning, not an error: abutting and even overlapping segments turn up in
    real annotation files, and refusing to load them would be worse than
    reporting them. The logical columns cannot diverge -- the ingest predicate
    enforces that before anything is written -- so only the coordinates are
    checked here.
    """
    return _count(
        con,
        """
        SELECT a.feature_id, a.seg_idx, b.seg_idx
        FROM segments a JOIN segments b
          ON a.feature_id = b.feature_id AND a.seg_idx < b.seg_idx
        WHERE a.start IS NOT NULL AND a."end" IS NOT NULL
          AND b.start IS NOT NULL AND b."end" IS NOT NULL
          AND a.start <= b."end" AND b.start <= a."end"
        """,
    )


def _inv8_bbox_matches(con):
    """`bbox` must agree with the coordinates it was built from.

    A stale bbox is the R-tree equivalent of a wrong envelope: the B-tree path
    keeps answering correctly while the R-tree path quietly disagrees, so the
    two paths return different features for the same query.
    """
    return _count(
        con,
        """
        SELECT id, start, "end" FROM features
        WHERE (start IS NULL OR "end" IS NULL) <> (bbox IS NULL)
           OR (bbox IS NOT NULL AND (
                   -- LEAST/GREATEST, not start/end: `ST_MakeEnvelope`
                   -- normalizes its corners, so a row with `end < start` --
                   -- which compat mode accepts, and which `sanitize_gff_file`
                   -- exists to repair -- gets a correctly-ordered envelope.
                   -- Comparing against the raw columns flagged those as
                   -- corrupt when the bbox was right.
                   ST_XMin(bbox) <> LEAST(start, "end")
                OR ST_XMax(bbox) <> GREATEST(start, "end")
                OR ST_YMin(bbox) <> seqid_y))
        """,
    )


def _inv9_seqid_map_complete(con):
    return _count(
        con,
        """
        SELECT f.seqid, f.seqid_y, m.seqid_y
        FROM (SELECT DISTINCT seqid, seqid_y FROM features WHERE seqid_y IS NOT NULL) f
        LEFT JOIN seqid_map m ON m.seqid = f.seqid
        WHERE m.seqid IS NULL OR m.seqid_y <> f.seqid_y
        """,
    )


def _inv10_attribute_dedup_consistent(con):
    """A segment marked as repeating segment 0's column 9 must have no
    attribute rows of its own, and one marked as differing must have some.

    Get this backwards and attributes silently vanish for the segments that
    actually carried different ones.
    """
    return _count(
        con,
        """
        SELECT s.feature_id, s.seg_idx, s.attrs_same_as_seg0, COALESCE(a.n, 0)
        FROM segments s
        LEFT JOIN (SELECT feature_id, seg_idx, COUNT(*) AS n
                   FROM attributes GROUP BY feature_id, seg_idx) a
               ON a.feature_id = s.feature_id AND a.seg_idx = s.seg_idx
        WHERE s.seg_idx > 0
          AND ((s.attrs_same_as_seg0 AND COALESCE(a.n, 0) > 0)
            OR (NOT s.attrs_same_as_seg0 AND COALESCE(a.n, 0) = 0))
        """,
    )


def _inv11_closure_sound(con):
    """No self-ancestry, no non-positive depth, and depth 1 is exactly `edges`.

    A cycle makes the recursive fallback non-terminating, and a depth-1 row
    with no matching edge means the closure and the edges disagree about the
    hierarchy -- which shows up as `children()` and `parents()` contradicting
    each other.
    """
    return _count(
        con,
        """
        SELECT ancestor, descendant, depth, reason FROM (
            SELECT ancestor, descendant, depth, 'self-ancestry' AS reason
            FROM closure WHERE ancestor = descendant
            UNION ALL
            SELECT ancestor, descendant, depth, 'non-positive depth' FROM closure WHERE depth < 1
            UNION ALL
            SELECT c.ancestor, c.descendant, c.depth, 'depth-1 row with no edge'
            FROM closure c WHERE c.depth = 1
              AND NOT EXISTS (SELECT 1 FROM edges e
                              WHERE e.parent = c.ancestor AND e.child = c.descendant)
        )
        """,
    )


def _inv13_conflicts_resolve(con):
    """Every recorded id resolution must point at a feature that exists, and
    `id_origin` must take a value the code actually produces.

    The design phrased this as `id_origin='create_unique'`, which the
    implemented vocabulary ('attribute' / 'autoincrement') does not use; this
    checks the property that vocabulary supports.
    """
    return _count(
        con,
        """
        SELECT which, value FROM (
            SELECT 'id_conflicts.resolved_id' AS which, resolved_id AS value
            FROM id_conflicts
            WHERE NOT EXISTS (SELECT 1 FROM features f WHERE f.id = id_conflicts.resolved_id)
            UNION ALL
            SELECT 'duplicates.new_id', new_id FROM duplicates
            WHERE NOT EXISTS (SELECT 1 FROM features f WHERE f.id = duplicates.new_id)
            UNION ALL
            SELECT 'features.id_origin', id_origin FROM features
            WHERE id_origin NOT IN ('attribute', 'autoincrement')
        )
        """,
    )


def _inv14_file_order_is_first_segment(con):
    """A feature's `file_order` must be its first line's, or ordering by it
    puts a discontinuous feature somewhere its first line never was."""
    return _count(
        con,
        """
        SELECT f.id, f.file_order, s.mn
        FROM features f
        JOIN (SELECT feature_id, MIN(file_order) AS mn FROM segments GROUP BY feature_id) s
             ON s.feature_id = f.id
        WHERE f.file_order IS DISTINCT FROM s.mn
        """,
    )


#: (number, name, severity, function, requires_segments)
_CHECKS = (
    ("INV-1", "unique_ids", ERROR, _inv1_unique_ids, False),
    ("INV-2", "no_orphan_segments", ERROR, _inv2_no_orphan_segments, True),
    ("INV-3", "n_segments_matches", ERROR, _inv3_n_segments_matches, True),
    ("INV-4", "seg_idx_dense", ERROR, _inv4_seg_idx_dense, True),
    ("INV-5", "envelope_exact", ERROR, _inv5_envelope_exact, True),
    ("INV-6", "edges_resolve", WARNING, _inv6_edges_resolve, False),
    ("INV-7", "segments_do_not_overlap", WARNING, _inv7_segments_consistent, True),
    ("INV-8", "bbox_matches", ERROR, _inv8_bbox_matches, False),
    ("INV-9", "seqid_map_complete", ERROR, _inv9_seqid_map_complete, False),
    ("INV-10", "attribute_dedup_consistent", ERROR, _inv10_attribute_dedup_consistent, True),
    ("INV-11", "closure_sound", ERROR, _inv11_closure_sound, False),
    ("INV-13", "conflicts_resolve", ERROR, _inv13_conflicts_resolve, True),
    ("INV-14", "file_order_is_first_segment", ERROR, _inv14_file_order_is_first_segment, True),
)

_DETAILS = {
    "unique_ids": "duplicate feature id",
    "no_orphan_segments": "segment rows whose feature no longer exists",
    "n_segments_matches": "n_segments disagrees with the stored segment count",
    "seg_idx_dense": "seg_idx is not a dense 0..n-1 sequence",
    "envelope_exact": "feature coordinates are not MIN/MAX over its segments",
    "edges_resolve": "edge endpoint does not resolve to a feature",
    "segments_do_not_overlap": "segments of one feature overlap each other",
    "bbox_matches": "bbox disagrees with the coordinates it was built from",
    "seqid_map_complete": "seqid missing from seqid_map, or its band disagrees",
    "attribute_dedup_consistent": "attribute rows disagree with attrs_same_as_seg0",
    "closure_sound": "closure is cyclic, mis-depthed, or disagrees with edges",
    "conflicts_resolve": "recorded id resolution points at a missing feature",
    "file_order_is_first_segment": "file_order is not the first segment's",
    "attributes_reparse": "re-parsing attributes_blob does not reproduce the attributes rows",
}


def _inv12_attributes_reparse(con, sample: int):
    """`full` only: re-parse stored blobs and compare with the attribute rows.

    This is the check that catches escaping bugs -- a parser that drops a
    percent-encoded character writes attribute rows that no longer match the
    bytes they came from, and every other invariant here would still hold.

    Sampled rather than exhaustive: it is the one check that costs per feature.
    """
    from gffbase.feature import _LazyAttributes

    rows = con.execute(
        "SELECT id, CAST(attributes_blob AS BLOB) FROM features "
        "WHERE attributes_blob IS NOT NULL AND is_synthetic = FALSE "
        "ORDER BY file_order LIMIT ?",
        [sample],
    ).fetchall()
    offenders = []
    for fid, blob in rows:
        stored: dict[str, list[str]] = {}
        for key, value in con.execute(
            "SELECT key, value FROM attributes WHERE feature_id = ? AND seg_idx = 0 ORDER BY idx",
            [fid],
        ).fetchall():
            stored.setdefault(key, []).append(value)
        # A key with an EMPTY value list -- `pseudo`, `ID=`, `Complete` -- is
        # not representable as a `(key, value)` row, so the long-form table
        # correctly holds nothing for it while the parsed mapping still shows
        # the key. That difference is the schema's, not a parser bug, so it is
        # normalized away here; without this, four upstream fixtures reported
        # a violation for behaviour that is exactly right.
        reparsed = {k: list(v) for k, v in _LazyAttributes(blob=bytes(blob)).items() if v}
        if reparsed != stored:
            offenders.append((fid, stored, reparsed))
    return len(offenders), tuple(offenders[:_EXAMPLE_LIMIT])


def _has_table(con, name: str) -> bool:
    return bool(
        con.execute("SELECT COUNT(*) FROM duckdb_tables() WHERE table_name = ?", [name]).fetchone()[
            0
        ]
    )


def _has_column(con, table: str, column: str) -> bool:
    return bool(
        con.execute(
            "SELECT COUNT(*) FROM duckdb_columns() WHERE table_name = ? AND column_name = ?",
            [table, column],
        ).fetchone()[0]
    )


def validate_db(
    db,
    level: str = "fast",
    *,
    raise_on_error: bool = False,
    sample: int = 200,
) -> ValidationReport:
    """Check a database's structural invariants.

    `level="fast"` runs every check that is a fixed number of aggregate scans.
    `level="full"` adds INV-12, which re-parses stored attribute blobs for
    `sample` features and is the only check whose cost grows with the corpus.

    Checks that depend on structures a database does not have -- a v1 shim has
    no `segments` -- are recorded as skipped rather than silently passing, so a
    green report cannot mean "nothing was looked at".
    """
    if level not in LEVELS:
        raise ValueError(f"level must be one of {LEVELS}; got {level!r}")

    con = db.conn if hasattr(db, "conn") else db
    report = ValidationReport(level=level)

    has_segments = _has_table(con, "segments")
    has_bbox = _has_column(con, "features", "bbox")

    for number, name, severity, fn, needs_segments in _CHECKS:
        if needs_segments and not has_segments:
            report.skipped.append(f"{number} ({name}): this database has no `segments` table")
            continue
        if name == "bbox_matches" and not has_bbox:
            report.skipped.append(f"{number} ({name}): no R-tree was built for this database")
            continue
        try:
            count, examples = fn(con)
        except duckdb.Error as exc:  # pragma: no cover - a malformed database
            report.violations.append(
                Violation(number, name, severity, 1, f"check could not run: {exc}")
            )
            continue
        report.checked.append(f"{number} ({name})")
        if count:
            report.violations.append(
                Violation(number, name, severity, count, _DETAILS[name], examples)
            )

    if level == "full":
        count, examples = _inv12_attributes_reparse(con, sample)
        report.checked.append("INV-12 (attributes_reparse)")
        if count:
            report.violations.append(
                Violation(
                    "INV-12",
                    "attributes_reparse",
                    ERROR,
                    count,
                    _DETAILS["attributes_reparse"],
                    examples,
                )
            )

    for violation in report.violations:
        (_log.error if violation.severity == ERROR else _log.warning)("%s", violation)

    if raise_on_error:
        report.raise_for_status()
    return report
