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
"""The `gffbase` command-line interface.

Argument names and output shapes follow `gffutils-cli` so a script written
against it keeps working. What does *not* follow it is the set of commands
that actually run: of the thirteen `gffutils-cli` defines, only five work.

    annotate      defined but never registered -- unreachable from the shell
    convert       defined but never registered
    clean         raises NotImplementedError
    common        raises NotImplementedError
    region        raises NotImplementedError
    fetch         raises TypeError -- `helpers.get_gff_db` returns a path
                  string on its common branch and the command indexes it as
                  though it were a database
    search        raises AttributeError -- it calls
                  `db.attribute_search(...)`, which exists nowhere in gffutils

So `create`, `children`, `parents`, `rmdups` and `sanitize` are the working
set upstream. Every command here works, including `region` and `search`, plus
`validate` and `migrate`, which are gffbase-only and are the natural CLI
surface for the schema-v2 work.

`argparse` deliberately: `gffutils-cli` needs `argh` and `argcomplete` as hard
runtime dependencies of the *library*, so importing gffutils at all pulls in a
CLI framework. gffbase adds no dependency for this.
"""

from __future__ import annotations

import argparse
import sys
from collections.abc import Callable

__all__ = ["build_parser", "main"]

_DB_HELP = (
    "Database to use. A GFF or GTF file is also accepted and a database will "
    "be built in memory for you, which can take some time on a large file -- "
    "prefer `gffbase create` up front."
)


def _open(path: str):
    """Open a database, or build one in memory from an annotation file.

    Unlike the upstream helper, this always returns a `FeatureDB`; there is no
    branch that hands back a path string.
    """
    from gffbase.create_db import create_db
    from gffbase.helpers import is_gff_db
    from gffbase.interface import FeatureDB

    if is_gff_db(path):
        return FeatureDB(path)
    return create_db(path, ":memory:", merge_strategy="merge", verbose=False)


def _emit(features, out=None) -> None:
    """Write features as GFF lines. Broken pipes are not errors here: `| head`
    closes the stream, and a traceback for that is noise."""
    stream = out or sys.stdout
    try:
        for feature in features:
            stream.write(str(feature) + "\n")
    except BrokenPipeError:  # pragma: no cover - depends on the consumer
        try:
            stream.close()
        except OSError:
            pass


def _split_ids(value: str | None) -> list[str] | None:
    if not value:
        return None
    return [part.strip() for part in value.split(",") if part.strip()]


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------


def cmd_create(args) -> int:
    from gffbase.create_db import create_db

    output = args.output or (args.filename + ".duckdb")
    db = create_db(
        args.filename,
        output,
        force=args.force,
        verbose=not args.quiet,
        merge_strategy=args.merge,
        disable_infer_genes=args.disable_infer_genes,
        disable_infer_transcripts=args.disable_infer_transcripts,
        mode=args.mode,
    )
    if not args.quiet:
        print(f"{output}: {db.count_features_of_type()} features", file=sys.stderr)
    return 0


def cmd_fetch(args) -> int:
    from gffbase.exceptions import FeatureNotFoundError

    db = _open(args.db)
    missing = False
    found = []
    for fid in _split_ids(args.ids) or []:
        try:
            found.append(db[fid])
        except FeatureNotFoundError:
            print(f"{fid} not found", file=sys.stderr)
            missing = True
    _emit(found)
    # A missing id is reported on stderr AND in the exit status, so a script
    # can tell. Upstream prints and exits 0.
    return 1 if missing else 0


def _walk(db, args, direction: str) -> int:
    exclude = set(_split_ids(args.exclude) or [])
    ids = _split_ids(args.ids)
    if not ids:
        ids = [f.id for f in db.features_of_type("gene")]

    step = db.children if direction == "children" else db.parents
    out = []
    for fid in ids:
        anchor = db[fid]
        if anchor.featuretype not in exclude and not args.exclude_self:
            out.append(anchor)
        # `level` is honoured. Upstream accepts `--limit` and raises
        # NotImplementedError for any value, while hardcoding a two-level walk.
        for feature in step(fid, level=args.level):
            if feature.featuretype not in exclude:
                out.append(feature)
    _emit(out)
    return 0


def cmd_children(args) -> int:
    return _walk(_open(args.db), args, "children")


def cmd_parents(args) -> int:
    return _walk(_open(args.db), args, "parents")


def cmd_region(args) -> int:
    """Upstream registers this and raises `NotImplementedError`."""
    db = _open(args.db)
    _emit(
        db.region(
            args.region,
            featuretype=_split_ids(args.featuretype),
            completely_within=args.completely_within,
        )
    )
    return 0


def cmd_search(args) -> int:
    """Upstream calls a `FeatureDB.attribute_search` that does not exist."""
    db = _open(args.db)
    _emit(db.attribute_search(args.text, featuretype=args.featuretype))
    return 0


def cmd_rmdups(args) -> int:
    from gffbase.create_db import create_db
    from gffbase.gffwriter import GFFWriter

    # Progress goes to stderr. Upstream prints its banner to stdout, where it
    # lands in the middle of the GFF it is writing.
    print(f"Removing duplicates from: {args.filename}", file=sys.stderr)
    db = create_db(args.filename, ":memory:", verbose=False, merge_strategy="merge")
    writer = (
        GFFWriter(args.filename, in_place=True) if args.in_place else GFFWriter(sys.stdout)  # type: ignore[arg-type]
    )
    for feature in db.all_features():
        writer.write_rec(feature)
    writer.close()
    return 0


def cmd_sanitize(args) -> int:
    from gffbase.helpers import sanitize_gff_file

    print(f"Sanitizing GFF {args.filename}", file=sys.stderr)
    sanitize_gff_file(args.filename, in_memory=not args.no_in_memory, in_place=args.in_place)
    return 0


def cmd_validate(args) -> int:
    """gffbase-only: run the post-ingest invariants over an existing database."""
    from gffbase.validate import validate_db

    db = _open(args.db)
    report = validate_db(db, level=args.level)
    for name in report.checked:
        print(f"ok      {name}")
    for name in report.skipped:
        print(f"skipped {name}")

    # Errors and warnings are reported separately because they mean different
    # things: an error is a broken invariant, a warning is something the data
    # does that is legal but suspect. Only errors fail by default.
    for violation in report.errors:
        print(
            f"ERROR   {violation.invariant} ({violation.name}): {violation.detail}", file=sys.stderr
        )
    for violation in report.warnings:
        print(
            f"WARN    {violation.invariant} ({violation.name}): {violation.detail}", file=sys.stderr
        )

    summary = (
        f"{len(report.checked)} invariants checked, "
        f"{len(report.errors)} error(s), {len(report.warnings)} warning(s)"
    )
    print(summary, file=sys.stderr)
    if report.errors:
        return 1
    if report.warnings and args.strict:
        return 1
    return 0


def cmd_migrate(args) -> int:
    """gffbase-only: upgrade a schema v1 database in place."""
    from gffbase.migrate import coalesce_multipart, migrate_v1_to_v2

    result = migrate_v1_to_v2(args.db)
    print(f"schema: {result.from_version} -> {result.to_version}", file=sys.stderr)
    if result.applied:
        print("applied: " + ", ".join(result.applied), file=sys.stderr)
    if args.coalesce:
        # Separate and opt-in: this one CHANGES query results, where the
        # migration proper is structural and does not.
        fused = coalesce_multipart(args.db)
        print(f"coalesced {fused} multipart feature(s)", file=sys.stderr)
    return 0


# ---------------------------------------------------------------------------
# Parser
# ---------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="gffbase",
        description="Query and build GFF/GTF databases.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    from gffbase import __version__

    parser.add_argument("--version", action="version", version=f"gffbase {__version__}")
    sub = parser.add_subparsers(dest="command", metavar="COMMAND")

    def add(name: str, func: Callable, help_text: str):
        p = sub.add_parser(name, help=help_text, description=help_text)
        p.set_defaults(func=func)
        return p

    p = add("create", cmd_create, "Build a database from a GFF or GTF file.")
    p.add_argument("filename", help="GFF or GTF file to use")
    p.add_argument("--output", help='Database to create. Default: input + ".duckdb"')
    p.add_argument("--force", action="store_true", help="Overwrite an existing database")
    p.add_argument("--quiet", action="store_true", help="Suppress progress reporting")
    p.add_argument(
        "--merge",
        default="merge",
        choices=["error", "warning", "merge", "create_unique", "replace"],
        help="Strategy for duplicate IDs",
    )
    p.add_argument("--disable-infer-genes", action="store_true", help="GTF: do not infer genes")
    p.add_argument(
        "--disable-infer-transcripts", action="store_true", help="GTF: do not infer transcripts"
    )
    p.add_argument(
        "--mode", default="compat", choices=["compat", "strict"], help="Validation profile"
    )

    p = add("fetch", cmd_fetch, "Print features by ID.")
    p.add_argument("db", help=_DB_HELP)
    p.add_argument("ids", help="Comma-separated list of IDs to fetch")

    for name, func, what in (
        ("children", cmd_children, "children"),
        ("parents", cmd_parents, "parents"),
    ):
        p = add(name, func, f"Print the {what} of the given features.")
        p.add_argument("db", help=_DB_HELP)
        p.add_argument(
            "ids", nargs="?", help="Comma-separated IDs. Default: every gene in the database"
        )
        p.add_argument(
            "--level",
            type=int,
            default=None,
            help=f"Only {what} at this level. Default: all levels",
        )
        p.add_argument("--exclude", help="Comma-separated featuretypes to filter out")
        p.add_argument(
            "--exclude-self", action="store_true", help="Do not report the given features"
        )

    p = add("region", cmd_region, "Print features overlapping a genomic region.")
    p.add_argument("db", help=_DB_HELP)
    p.add_argument("region", help='Coordinates as "chrom:start-stop"')
    p.add_argument("--featuretype", help="Comma-separated featuretypes to restrict to")
    p.add_argument(
        "--completely-within",
        action="store_true",
        help="Only features contained entirely within the region",
    )

    p = add("search", cmd_search, "Search attribute values.")
    p.add_argument("db", help=_DB_HELP)
    p.add_argument("text", help="Text to search for. Case-insensitive; SQL LIKE syntax")
    p.add_argument("--featuretype", help="Restrict to one featuretype")

    p = add("rmdups", cmd_rmdups, "Remove duplicate features from a GFF file.")
    p.add_argument("filename", help="GFF or GTF file to use")
    p.add_argument("--in-place", action="store_true", help="Overwrite the input file")

    p = add("sanitize", cmd_sanitize, "Order coordinates and add a grep-able gene id.")
    p.add_argument("filename", help="GFF or GTF file (or database) to use")
    p.add_argument(
        "--no-in-memory",
        action="store_true",
        help="Build the database on disk rather than in memory",
    )
    p.add_argument("--in-place", action="store_true", help="Overwrite the input file")

    p = add("validate", cmd_validate, "Check a database's invariants (gffbase only).")
    p.add_argument("db", help=_DB_HELP)
    p.add_argument("--level", default="full", choices=["fast", "full"], help="How much to check")
    p.add_argument(
        "--strict",
        action="store_true",
        help="Exit non-zero on warnings too, not only on errors",
    )

    p = add("migrate", cmd_migrate, "Upgrade a schema v1 database in place (gffbase only).")
    p.add_argument("db", help="Database to upgrade")
    p.add_argument(
        "--coalesce",
        action="store_true",
        help="Also re-fuse v1's split multipart features. Changes query results, so opt-in.",
    )

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if not getattr(args, "func", None):
        parser.print_help()
        return 2
    return args.func(args)


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
