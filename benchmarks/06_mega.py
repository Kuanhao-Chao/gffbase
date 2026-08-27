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
"""Comprehensive human-genome annotation benchmark.

Runs the same metrics across GENCODE (GTF and GFF3), RefSeq, MANE and CHESS:

  * Ingestion wall (gffbase + legacy gffutils)
  * Peak RSS during ingest
  * On-disk DB size
  * Spatial query qps (R-tree path)
  * Vectorized batched extraction wall (children_batched, format='arrow')

Each process writes beneath ``GFFBASE_BENCH_OUT``. Cluster workers always use
different output directories; a single post-run merger validates and combines
them, so parallel jobs never race through a shared read-modify-write result.

Safety valve: if legacy gffutils ingest exceeds ``--legacy-timeout`` it is
killed and reported as a censored timeout (`wall_seconds` null,
``cap_seconds`` set). It produces no comparison ratio.

Disk: a corpus pair reaches ~13 GiB, and all five together do not fit on a
normal laptop. Each pair is purged as soon as its numbers are recorded unless
`--keep-db` names it. Set `GFFBASE_BENCH_OUT` to run against another volume.
"""

from __future__ import annotations

import argparse
import gzip
import json
import random
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from benchmarks.common import (
    OUT,
    RESULTS,
    benchmark_env,
    configure_duckdb_connection,
    database_signature,
    du,
    merge_results,
    pretty_bytes,
    pretty_seconds,
    purge_db,
    repeat,
    require_free_disk,
    run_subprocess,
    sha256_file,
    signatures_match,
)
from benchmarks.corpora import CORPORA as CORPUS_REGISTRY
from benchmarks.corpora import corpus_path

DATA = ROOT / "benchmarks" / "data"

#: Fixed so region sampling is reproducible. Recorded in the results file --
#: it was deterministic before, but undocumented, so a reader could not tell
#: whether two runs sampled the same regions.
REGION_SEED = 20260501

#: Rough peak transient (GiB) for a corpus pair: gffbase DuckDB + legacy
#: SQLite + working space. Used for the pre-flight so a sweep fails before
#: spending an hour rather than after.
# Measured peaks from a real sweep, plus ~1 GiB of working room. Set from
# observation rather than guessed: the first estimates were high enough to
# refuse corpora that in fact fit.
DISK_NEED_GIB = {
    "gencode-gtf": 12.5,  # 6.6 GiB DuckDB + ~5 GiB legacy SQLite
    "gencode-gff3": 14.0,  # 7.0 GiB DuckDB + 6.1 GiB legacy SQLite
    "refseq": 10.5,  # 4.9 + 3.8
    "chess": 3.5,  # 1.5 + 1.1
    "mane": 2.0,  # 0.6 + 0.5
}


# ---------------------------------------------------------------------------
# Corpus registry
# ---------------------------------------------------------------------------

# Ordered CHEAPEST FIRST. Two reasons: a harness mistake surfaces after two
# minutes on MANE rather than ninety on GENCODE GTF, and the largest pair runs
# last so `--keep-db gencode-gff3` can hand it straight to stages 01-05
# without a second 6-minute ingest.
CORPORA: list[dict] = [{**corpus, "input": corpus_path(corpus)} for corpus in CORPUS_REGISTRY]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def count_feature_lines(path: Path) -> int:
    """Count non-comment, non-blank lines in a (possibly gzipped) file."""
    opener = gzip.open if path.suffix == ".gz" else open
    n = 0
    with opener(path, "rt", encoding="utf-8", errors="replace") as fin:
        for line in fin:
            if line and not line.startswith("#") and not line.isspace():
                n += 1
    return n


def gffbase_ingest_script(
    input_path: Path,
    dbfn: Path,
    fmt: str,
    *,
    threads: int = 1,
    infer_gtf_parents: bool = True,
    validation_sample: int | None = None,
    validation_requested: str = "all",
) -> str:
    return f"""
import dataclasses, json, time
from gffbase import create_db
t0 = time.perf_counter()
# CHESS / MANE / RefSeq are all GFF3; GENCODE is GTF. The hardened
# parser auto-detects but `force_gff=True` prevents quoted-attr lines
# from being mis-classified as GTF in edge cases.
#
# `merge_strategy` is NOT optional here, for two independent reasons:
#
# 1. Correctness of the run. It defaults to "error", and RefSeq
#    GRCh38.p14 legitimately repeats `ID=cds-*` across its discontinuous
#    CDS records. Without this the RefSeq ingest raises DuplicateIDError,
#    which the driver catches and files as an `{{"error": ...}}` row --
#    so the corpus silently drops out of the table instead of failing
#    the run. Two of five corpora were affected.
# 2. Fairness of the comparison. `legacy_ingest_script` below passes
#    `merge_strategy="create_unique"`. Timing gffbase under "error"
#    against gffutils under "create_unique" compares two different
#    workloads on the exact axis -- duplicate-ID handling -- that
#    decides whether the run completes at all.
db = create_db({str(input_path)!r}, {str(dbfn)!r}, force=True,
               merge_strategy="create_unique",
               force_gff={"False" if fmt == "gtf" else "True"},
               disable_infer_genes={not infer_gtf_parents!r},
               disable_infer_transcripts={not infer_gtf_parents!r},
               pragmas={{"threads": {threads}}})
elapsed = time.perf_counter() - t0
n_features = db.count_features_of_type()
report = db.validate(level="full", sample={validation_sample!r})
print(json.dumps({{
    "wall_seconds": elapsed,
    "n_features":   n_features,
    # Reported so the driver can refuse to publish a "spatial qps" number
    # that was actually measured on the B-tree fallback. `INSTALL spatial`
    # needs network egress, and on a firewalled node it fails silently.
    "rtree_built":  db._rtree_built,
    "fmt":          db.fmt,
    "validation": {{
        "ok": report.ok,
        "level": report.level,
        "checked": report.checked,
        "skipped": report.skipped,
        "errors": [dataclasses.asdict(v) for v in report.errors],
        "warnings": [dataclasses.asdict(v) for v in report.warnings],
        "requested_sample": {validation_requested!r},
        "sample_checked": n_features if {validation_sample!r} is None else min({validation_sample!r}, n_features),
        "checked_ids": report.checked_ids,
    }},
}}))
"""


def legacy_ingest_script(input_path: Path, dbfn: Path, *, infer_gtf_parents: bool = True) -> str:
    return f"""
import json, time, gffutils
t0 = time.perf_counter()
db = gffutils.create_db(
    {str(input_path)!r}, {str(dbfn)!r}, force=True,
    keep_order=False, sort_attribute_values=False,
    merge_strategy="create_unique", verbose=False,
    disable_infer_genes={not infer_gtf_parents!r},
    disable_infer_transcripts={not infer_gtf_parents!r},
)
elapsed = time.perf_counter() - t0
print(json.dumps({{
    "wall_seconds": elapsed,
    "n_features":   db.count_features_of_type(),
}}))
"""


def run_legacy_with_timeout(
    input_path: Path,
    dbfn: Path,
    timeout: int,
    n_input_lines: int,
    *,
    infer_gtf_parents: bool = True,
    threads: int = 1,
) -> dict:
    """Run legacy gffutils ingest under a recorded hard cap.

    A timeout is censored: it records its cap but never substitutes a wall
    time, lower bound or performance claim for the uncompleted comparator.
    """
    script = legacy_ingest_script(input_path, dbfn, infer_gtf_parents=infer_gtf_parents)
    result = run_subprocess(
        script,
        label=f"legacy gffutils ingest({input_path.name})",
        timeout=timeout,
        env_extra=benchmark_env(threads),
    )
    if result.get("state") == "timed_out":
        result["wall_seconds"] = None
        result["cap_seconds"] = timeout
        result.pop("timed_out", None)
    return result


def sample_regions_from_db(
    db_path: Path, n: int = 5000, seed: int = REGION_SEED, *, threads: int = 1
) -> list[tuple[str, int, int]]:
    import duckdb

    con = duckdb.connect(str(db_path), read_only=True)
    configure_duckdb_connection(con, threads)
    rows = con.execute("""
        SELECT seqid, MIN(start) AS lo, MAX("end") AS hi
        FROM features GROUP BY seqid HAVING COUNT(*) > 50
    """).fetchall()
    con.close()
    if not rows:
        return []
    rng = random.Random(seed)
    out = []
    while len(out) < n:
        seqid, lo, hi = rng.choice(rows)
        if hi - lo < 5_000:
            continue
        rstart = rng.randint(lo, hi - 5_000)
        rend = rstart + rng.randint(1_000, 5_000)
        out.append((seqid, rstart, rend))
    return out


def bench_spatial(db_path: Path, regions, repeats: int = 1, *, threads: int = 1) -> dict:
    import gffbase

    # `with`, not a bare constructor: a writable DuckDB connection holds an
    # exclusive lock, and the sweep purges each corpus's databases as soon as
    # its numbers are recorded. Leaking the handle left the file locked, which
    # is merely untidy on POSIX and blocks the delete outright on Windows.
    with gffbase.FeatureDB(str(db_path), read_only=True) as db:
        configure_duckdb_connection(db.conn, threads)
        counted = {"total": 0}

        def once() -> float:
            t0 = time.perf_counter()
            total = 0
            for seqid, rs, re_ in regions:
                for _ in db.region(seqid=seqid, start=rs, end=re_, featuretype="exon"):
                    total += 1
            elapsed = time.perf_counter() - t0
            counted["total"] = total
            return elapsed

        # One discarded run when repeating: the first pass pays cold page
        # cache, and a smoke test measured max/min = 6.7x purely from that.
        # A spread that is really a cold-start artifact is worse than no
        # spread, because it gets published as measurement uncertainty.
        timing = repeat(once, repeats, warmup=1 if repeats > 1 else 0)

    seconds = timing.get("median", timing.get("value"))
    return {
        "n_queries": len(regions),
        "wall_seconds": seconds,
        "qps": len(regions) / seconds if seconds else None,
        "total_features_returned": counted["total"],
        "timing": timing,
    }


def bench_batched(
    db_path: Path, n_genes: int = 5000, repeats: int = 1, *, threads: int = 1
) -> dict:
    import gffbase

    # See `bench_spatial` on why this is a `with` block.
    db = gffbase.FeatureDB(str(db_path), read_only=True)
    configure_duckdb_connection(db.conn, threads)
    # FeatureDB.execute() doesn't accept params — drop down to the
    # underlying duckdb connection for parameter binding.
    cur = db.conn.execute(
        "SELECT id FROM features WHERE featuretype = 'gene' ORDER BY id LIMIT ?",
        [n_genes],
    )
    gene_ids = [r[0] for r in cur.fetchall()]
    if not gene_ids:
        # Some annotations (RefSeq/CHESS) may use a different top-level
        # featuretype name. Probe a few common ones.
        for ft in ("Gene", "mRNA", "transcript", "ncRNA_gene", "pseudogene"):
            cur = db.conn.execute(
                "SELECT id FROM features WHERE featuretype = ? ORDER BY id LIMIT ?",
                [ft, n_genes],
            )
            gene_ids = [r[0] for r in cur.fetchall()]
            if gene_ids:
                break
    if not gene_ids:
        db.close()
        return {"skipped": "no top-level feature IDs found"}

    rows = {"n": 0}

    def once() -> float:
        t0 = time.perf_counter()
        table = db.children_batched(gene_ids, format="arrow")
        elapsed = time.perf_counter() - t0
        rows["n"] = table.num_rows
        return elapsed

    try:
        timing = repeat(once, repeats, warmup=1 if repeats > 1 else 0)
    finally:
        db.close()

    seconds = timing.get("median", timing.get("value"))
    return {
        "n_anchors": len(gene_ids),
        "n_descendants": rows["n"],
        "wall_seconds": seconds,
        "qps": len(gene_ids) / seconds if seconds else None,
        "timing": timing,
    }


# ---------------------------------------------------------------------------
# Driver
# ---------------------------------------------------------------------------


def derive_speedup(g_info: dict, l_info: dict) -> tuple[float | None, None, bool]:
    """Return a ratio only for two completed, signature-equivalent ingests.

    Purely derived from values measured elsewhere, and defined once so the
    rule cannot drift between where it is computed and where it is repaired.

    Counts remain a useful diagnostic, but are not a correctness proof.  Both
    versioned database signatures must exist and compare exactly equal.  A
    Candidate completion and exhaustive validation are mandatory. A timed-out
    comparator is censored and cannot produce a speedup or a floor.
    """
    signature_equal = signatures_match(
        g_info.get("correctness_signature"), l_info.get("correctness_signature")
    )
    g_n, l_n = g_info.get("n_features"), l_info.get("n_features")
    count_conflict = g_n is not None and l_n is not None and g_n != l_n
    conflict = signature_equal is False or count_conflict

    g_wall = g_info.get("wall_seconds")
    validation = g_info.get("validation") or {}
    candidate_valid = (
        g_info.get("state") == "completed"
        and validation.get("ok") is True
        and validation.get("requested_sample") == "all"
        and validation.get("sample_checked") == g_info.get("n_features")
    )
    if not g_wall or not candidate_valid or conflict or signature_equal is not True:
        return None, None, conflict
    if l_info.get("state") == "completed" and l_info.get("wall_seconds"):
        return l_info["wall_seconds"] / g_wall, None, conflict
    return None, None, conflict


def _git_commit() -> str | None:
    import subprocess

    try:
        out = subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=ROOT, capture_output=True, text=True, timeout=20
        )
        return out.stdout.strip() or None
    except Exception:  # pragma: no cover - provenance is best-effort
        return None


def _git_dirty() -> bool | None:
    import subprocess

    try:
        out = subprocess.run(
            ["git", "status", "--porcelain"],
            cwd=ROOT,
            capture_output=True,
            text=True,
            timeout=20,
        )
        return bool(out.stdout.strip())
    except Exception:  # pragma: no cover - provenance is best-effort
        return None


def rel(path: Path) -> str:
    """Path relative to the repo, when it is inside it.

    The committed results file used to record `/Users/<someone>/Documents/...`
    for every input and database. That leaks whoever ran the sweep into a
    published artifact and makes the file describe one machine rather than one
    measurement.
    """
    try:
        return str(Path(path).resolve().relative_to(ROOT))
    except ValueError:
        # The checksum separately identifies an external input; its absolute
        # location must never enter a portable benchmark payload.
        return f"external/{Path(path).name}"


def _validation_sample(value: str) -> int | None:
    """Parse the portable validation mode used in benchmark payloads."""

    if value == "all":
        return None
    try:
        sample = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("must be 'all' or an integer >= 1") from exc
    if sample < 1:
        raise argparse.ArgumentTypeError("must be 'all' or an integer >= 1")
    return sample


def run_one(corpus: dict, args) -> dict:
    name = corpus["name"]
    key = corpus["key"]
    inp = corpus["input"]
    result_key = key
    infer_gtf_parents = True
    if key == "gencode-gtf":
        infer_gtf_parents = args.gtf_arm != "no-infer"
        if args.gtf_arm == "default":
            result_key = "gencode-gtf-default"
        elif args.gtf_arm == "parent-stripped":
            if args.gtf_input is None:
                raise SystemExit("--gtf-arm parent-stripped requires --gtf-input")
            inp = args.gtf_input
            result_key = "gencode-gtf-parent-stripped"
        else:
            # This is the recommended real-data GTF headline.
            result_key = "gencode-gtf"
    print(f"\n=== {name} ({inp.name}) ===", flush=True)
    if not inp.exists():
        msg = f"missing input: {inp}"
        print("  SKIP —", msg, flush=True)
        return {"name": name, "key": key, "error": msg}

    require_free_disk(DISK_NEED_GIB.get(key, 8.0), what=f"corpus {key}")

    n_lines = count_feature_lines(inp)
    print(f"  feature lines: {n_lines:,}", flush=True)

    gffbase_db = OUT / f"{result_key}.duckdb"
    legacy_db = OUT / f"{result_key}_legacy.sqlite"

    # ---- gffbase ingest ----
    print("  [gffbase] ingest…", flush=True)
    if gffbase_db.exists():
        gffbase_db.unlink()
    g_info = run_subprocess(
        gffbase_ingest_script(
            inp,
            gffbase_db,
            corpus["fmt"],
            threads=args.threads,
            infer_gtf_parents=infer_gtf_parents,
            validation_sample=args.validation_sample_value,
            validation_requested=args.validation_sample,
        ),
        label=f"gffbase ingest({inp.name})",
        timeout=args.gffbase_timeout,
        env_extra=benchmark_env(args.threads),
    )
    g_info["cap_seconds"] = args.gffbase_timeout
    g_info.pop("timed_out", None)
    g_info["disk_bytes"] = du(gffbase_db)
    if (
        g_info.get("exit_code") == 0
        and gffbase_db.is_file()
        and (g_info.get("validation") or {}).get("ok")
    ):
        g_info["correctness_signature"] = database_signature(gffbase_db, engine="gffbase")
    if g_info.get("wall_seconds"):
        print(
            f"    wall={pretty_seconds(g_info['wall_seconds'])}, "
            f"RSS={pretty_bytes(g_info['peak_rss_bytes'])}, "
            f"disk={pretty_bytes(g_info['disk_bytes'])}",
            flush=True,
        )
    else:
        print(f"    failed: exit={g_info.get('exit_code')}", flush=True)

    # ---- legacy ingest (with safety-valve timeout) ----
    if args.skip_legacy:
        print("  [legacy ] skipped by --skip-legacy", flush=True)
        l_info = {
            "state": "skipped",
            "skipped": "requested by --skip-legacy",
            "n_features": None,
        }
    else:
        print(f"  [legacy ] ingest (timeout {args.legacy_timeout}s)…", flush=True)
        if legacy_db.exists():
            legacy_db.unlink()
        l_info = run_legacy_with_timeout(
            inp,
            legacy_db,
            args.legacy_timeout,
            n_lines,
            infer_gtf_parents=infer_gtf_parents,
            threads=args.threads,
        )
        l_info["cap_seconds"] = args.legacy_timeout
        l_info["disk_bytes"] = du(legacy_db)
        if l_info.get("exit_code") == 0 and legacy_db.is_file():
            l_info["correctness_signature"] = database_signature(legacy_db, engine="gffutils")
    if l_info.get("skipped"):
        pass
    elif l_info.get("state") == "timed_out":
        print(
            f"    timed out at the {args.legacy_timeout}s cap; comparator censored",
            flush=True,
        )
    elif l_info.get("wall_seconds"):
        print(
            f"    wall={pretty_seconds(l_info['wall_seconds'])}, "
            f"RSS={pretty_bytes(l_info['peak_rss_bytes'])}, "
            f"disk={pretty_bytes(l_info['disk_bytes'])}",
            flush=True,
        )
    else:
        print(f"    failed: exit={l_info.get('exit_code')}", flush=True)

    # Both engines must have done the SAME work, or the ratio between their
    # walls is not a speedup. Nothing checked this: each count was recorded and
    # then never compared, so a corpus where the two disagreed -- a different
    # duplicate-ID policy, a parent-synthesis difference on GTF -- would have
    # published a headline number comparing two different workloads.
    speedup, _, counts_conflict = derive_speedup(g_info, l_info)
    if counts_conflict:
        print(
            f"    !! feature-count mismatch: gffbase={g_info['n_features']:,} "
            f"legacy={l_info['n_features']:,}. Refusing to report a speedup "
            "between different workloads.",
            flush=True,
        )

    # ---- spatial routing ----
    print(f"  [gffbase] spatial — {args.n_spatial} regions…", flush=True)
    spatial = {"skipped": "no DB"} if not gffbase_db.exists() else None
    if spatial is None:
        regions = sample_regions_from_db(gffbase_db, n=args.n_spatial, threads=args.threads)
        if regions:
            spatial = bench_spatial(gffbase_db, regions, repeats=args.repeats, threads=args.threads)
            print(
                f"    wall={pretty_seconds(spatial['wall_seconds'])}, "
                f"qps={spatial['qps']:.0f}, "
                f"features_returned={spatial['total_features_returned']}",
                flush=True,
            )
        else:
            spatial = {"skipped": "no qualifying seqids"}

    # ---- vectorized batched ----
    print(f"  [gffbase] batched — {args.n_batched} anchors…", flush=True)
    batched = {"skipped": "no DB"} if not gffbase_db.exists() else None
    if batched is None:
        batched = bench_batched(
            gffbase_db,
            n_genes=args.n_batched,
            repeats=args.repeats,
            threads=args.threads,
        )
        if "skipped" not in batched:
            print(
                f"    wall={pretty_seconds(batched['wall_seconds'])}, "
                f"anchors={batched['n_anchors']}, "
                f"descendants={batched['n_descendants']}",
                flush=True,
            )

    return {
        "name": name,
        "key": result_key,
        # Per-row provenance. Results MERGE by key -- which is the fix for a
        # real bug, where `--only mane` used to wipe the other four corpora --
        # but it means one file can hold rows measured at different times from
        # different commits, under a single `environment` block describing only
        # the most recent write. Without this, a corpus that errored would keep
        # its stale row and publish it under a fresh, honest-looking stamp.
        "measured": {
            "timestamp_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "git_commit": _git_commit(),
            "git_dirty": _git_dirty(),
        },
        "input": rel(inp),
        "input_bytes": inp.stat().st_size if inp.exists() else 0,
        "input_sha256": sha256_file(inp) if inp.exists() else None,
        "feature_lines": n_lines,
        "gffbase": g_info,
        "legacy": l_info,
        "ingest_speedup": speedup,
        "spatial": spatial,
        "batched": batched,
        "db_paths": {"gffbase": rel(gffbase_db), "legacy": rel(legacy_db)},
        "params": {
            "legacy_cap_seconds": args.legacy_timeout,
            "gffbase_cap_seconds": args.gffbase_timeout,
            "n_spatial": args.n_spatial,
            "n_batched": args.n_batched,
            "repeats": args.repeats,
            "region_seed": REGION_SEED,
            "threads": args.threads,
            "gtf_arm": args.gtf_arm if key == "gencode-gtf" else None,
            "infer_gtf_parents": infer_gtf_parents if key == "gencode-gtf" else None,
            "validation_sample": args.validation_sample,
            "benchmark_env": benchmark_env(args.threads),
        },
    }


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument(
        "--legacy-timeout",
        type=int,
        default=5400,
        help="seconds before legacy ingest is killed (default 5400 = 90 min)",
    )
    ap.add_argument(
        "--gffbase-timeout", type=int, default=3600, help="seconds before gffbase ingest is killed"
    )
    ap.add_argument(
        "--rederive",
        action="store_true",
        help=(
            "recompute the DERIVED speedup field in an existing "
            "results file from the measurements already in it, then exit. For "
            "when the derivation rule is corrected and re-measuring would cost "
            "hours. Touches no measured value."
        ),
    )
    ap.add_argument(
        "--publish",
        action="store_true",
        help=(
            "copy the finished run to benchmarks/results/06_mega.json, which is "
            "the committed file the published tables are generated from. "
            "Without this the run lands only in benchmarks/out/ (gitignored) "
            "and the docs keep rendering the previous measurement."
        ),
    )
    ap.add_argument(
        "--repeats",
        type=int,
        default=1,
        help=(
            "how many times to repeat the two cheap in-process measurements "
            "(spatial, batched). n=1 records a bare `value`; n>1 records "
            "median/min/max/values so the spread is publishable. The ingest "
            "walls are NOT repeated -- legacy GENCODE alone takes over an hour."
        ),
    )
    ap.add_argument("--n-spatial", type=int, default=5000)
    ap.add_argument("--n-batched", type=int, default=5000)
    ap.add_argument(
        "--threads",
        type=int,
        default=int(__import__("os").environ.get("GFFBASE_THREADS", "1")),
        help="DuckDB threads for every ingest and query connection",
    )
    ap.add_argument(
        "--validation-sample",
        type=str,
        default="all",
        help="full validation attribute sample: 'all' (canonical) or an integer >= 1",
    )
    ap.add_argument(
        "--gtf-arm",
        choices=("no-infer", "default", "parent-stripped"),
        default="no-infer",
        help=(
            "GENCODE GTF control: recommended real-data run with redundant parent "
            "inference disabled, legacy defaults, or a derived parent-stripped input"
        ),
    )
    ap.add_argument(
        "--gtf-input",
        type=Path,
        help="derived GTF path required by --gtf-arm parent-stripped",
    )
    ap.add_argument(
        "--skip-legacy",
        action="store_true",
        help="run only gffbase (used for thread-scaling diagnostics)",
    )
    ap.add_argument(
        "--only",
        action="append",
        default=None,
        help="restrict to specific corpus keys (repeatable). Results MERGE, so "
        "this updates the named corpora and leaves the others untouched.",
    )
    ap.add_argument(
        "--keep-db",
        action="append",
        default=None,
        help="corpus keys whose databases survive the run (repeatable). "
        "Everything else is purged as soon as its numbers are recorded -- "
        "all five pairs together are ~38 GiB.",
    )
    ap.add_argument(
        "--no-purge",
        action="store_true",
        help="keep every database. Needs ~38 GiB free; the sweep will not fit "
        "on a normal laptop disk.",
    )
    args = ap.parse_args()

    if args.threads < 1:
        ap.error("--threads must be >= 1")
    if args.repeats < 1:
        ap.error("--repeats must be >= 1")
    try:
        args.validation_sample_value = _validation_sample(args.validation_sample)
    except argparse.ArgumentTypeError as exc:
        ap.error(f"--validation-sample {exc}")

    OUT.mkdir(parents=True, exist_ok=True)
    if args.rederive:
        path = OUT / "06_mega.json"
        data = json.loads(path.read_text())
        for key, row in (data.get("corpora") or {}).items():
            if "error" in row or not row.get("gffbase"):
                continue
            before = row.get("ingest_speedup")
            sp, _, _ = derive_speedup(row["gffbase"], row.get("legacy") or {})
            row["ingest_speedup"] = sp
            row.pop("ingest_speedup_lower_bound", None)
            if before != sp:
                print(f"  {key}: {before} -> {sp}", flush=True)
        path.write_text(json.dumps(data, indent=2) + "\n")
        print(f"rederived {path.relative_to(ROOT)} (no measured value changed)", flush=True)
        return

    selected = set(args.only) if args.only else {c["key"] for c in CORPORA}
    unknown = selected - {c["key"] for c in CORPORA}
    if unknown:
        raise SystemExit(f"unknown corpus key(s): {sorted(unknown)}")
    keep = set(args.keep_db or ())

    results: dict[str, dict] = {}
    for corpus in CORPORA:
        key = corpus["key"]
        if key not in selected:
            continue
        try:
            row = run_one(corpus, args)
        except SystemExit as exc:
            # `require_free_disk` raises SystemExit, which is a BaseException
            # and so sailed past the guard below -- aborting the process and
            # skipping `--publish` entirely. A five-hour sweep that measured
            # four corpora then hit the disk guard on the fifth published
            # NONE of them. Record it and carry on; the remaining corpora get
            # their own check, and what did complete still gets written.
            print(f"  SKIP {corpus['name']}: {exc}", flush=True)
            row = {"name": corpus["name"], "key": key, "error": str(exc)}
        except Exception as exc:  # pragma: no cover - top-level guard
            print(f"  ERROR on {corpus['name']}: {exc}", flush=True)
            row = {"name": corpus["name"], "key": key, "error": str(exc)}
        output_key = row.get("key", key)
        results[output_key] = row

        # Write after EVERY corpus, not once at the end. A five-hour sweep
        # that dies on the last corpus used to lose all of it.
        merge_results("06_mega", "corpora", {output_key: row})

        if not args.no_purge and key not in keep:
            paths = row.get("db_paths") or {}
            # Stored relative (see `rel`); resolve against the repo to delete.
            freed = purge_db(*(ROOT / v for v in paths.values()))
            if freed:
                print(f"  purged {key} databases ({pretty_bytes(freed)})", flush=True)

    out_path = merge_results("06_mega", "corpora", results)
    print(f"\nResults → {out_path}", flush=True)

    if args.publish:
        import shutil

        # Refuse to publish a file that mixes runs. `merge_results` preserves
        # rows this invocation did not touch, so a corpus that errored keeps
        # whatever was measured for it last time -- possibly on a different
        # commit, possibly on a dirty tree -- and the single `environment`
        # block would present the whole file as one clean run.
        merged = json.loads(Path(out_path).read_text())
        stale = []
        for key, row in (merged.get("corpora") or {}).items():
            stamp = row.get("measured") or {}
            if key not in results or "error" in row:
                stale.append(f"{key} (not measured in this run)")
            elif stamp.get("git_dirty"):
                stale.append(f"{key} (measured on a dirty tree)")
        if stale:
            print(
                "\nNOT publishing: the merged file would mix runs.\n  "
                + "\n  ".join(stale)
                + "\n\nRe-run the missing corpora, then publish. The measured rows are "
                f"safe in {Path(out_path).relative_to(ROOT)}.",
                flush=True,
            )
            return

        published = RESULTS / "06_mega.json"
        published.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(out_path, published)
        print(f"Published → {published.relative_to(ROOT)}", flush=True)
        print(
            "  Regenerate the tables with: python tools/gen_benchmark_tables.py --write",
            flush=True,
        )

    # Compact summary table.
    print("\n" + "=" * 92, flush=True)
    print(
        f"{'corpus':<28}  {'gffbase ingest':>16}  {'legacy ingest':>18}  {'speedup':>12}",
        flush=True,
    )
    print("-" * 92, flush=True)
    for key in (c["key"] for c in CORPORA):
        row = results.get(key)
        if row is None:
            continue
        if "error" in row:
            print(f"{row['name']:<28}  {'ERROR: ' + row['error']:>16}", flush=True)
            continue
        g_wall = row["gffbase"].get("wall_seconds")
        legacy = row["legacy"]
        if legacy.get("wall_seconds"):
            legacy_cell = pretty_seconds(legacy["wall_seconds"])
        elif legacy.get("state") == "timed_out":
            legacy_cell = f"timeout ({legacy.get('cap_seconds')} s)"
        else:
            legacy_cell = "fail"
        if row.get("ingest_speedup"):
            speed_cell = f"{row['ingest_speedup']:.2f}x"
        else:
            speed_cell = "n/a"
        print(
            f"{row['name']:<28}  "
            f"{(pretty_seconds(g_wall) if g_wall else 'fail'):>16}  "
            f"{legacy_cell:>18}  "
            f"{speed_cell:>12}",
            flush=True,
        )
    print("=" * 92, flush=True)
    print("timeout = comparator did not complete; no speedup is reported.", flush=True)


if __name__ == "__main__":
    main()
