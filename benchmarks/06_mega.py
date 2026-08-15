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

Results MERGE into `06_mega.json` by corpus key, so `--only mane` updates
MANE and leaves every other corpus alone. The previous version wrote the whole
file from whatever `--only` selected, so each targeted re-run silently deleted
the others -- which is why the published five-row table was eventually backed
by a results file containing one row.

Safety valve: if legacy gffutils ingest exceeds ``--legacy-timeout`` it is
killed and reported as a LOWER BOUND (`wall_seconds` null,
`wall_seconds_lower_bound` set). No wall time is ever synthesized.

Disk: a corpus pair reaches ~13 GiB, and all five together do not fit on a
normal laptop. Each pair is purged as soon as its numbers are recorded unless
`--keep-db` names it. Set `GFFBASE_BENCH_OUT` to run against another volume.
"""

from __future__ import annotations

import argparse
import gzip
import random
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "python"))

from benchmarks.common import (
    OUT,
    RESULTS,
    du,
    merge_results,
    pretty_bytes,
    pretty_seconds,
    purge_db,
    repeat,
    require_free_disk,
    run_subprocess,
)

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
CORPORA: list[dict] = [
    {
        "name": "MANE v1.5 (Ensembl IDs)",
        "key": "mane",
        "input": DATA / "MANE.GRCh38.v1.5.ensembl_genomic.gff.gz",
        "fmt": "gff3",
    },
    {
        "name": "CHESS 3.1.3",
        "key": "chess",
        "input": DATA / "chess3.1.3.GRCh38.gff.gz",
        "fmt": "gff3",
    },
    {
        "name": "RefSeq GRCh38.p14",
        "key": "refseq",
        "input": DATA / "GCF_000001405.40_GRCh38.p14_genomic.gff.gz",
        "fmt": "gff3",
    },
    {
        "name": "GENCODE v49 (GTF)",
        "key": "gencode-gtf",
        "input": DATA / "gencode.v49.chr_patch_hapl_scaff.basic.annotation.gtf.gz",
        "fmt": "gtf",
    },
    {
        "name": "GENCODE v49 (GFF3)",
        "key": "gencode-gff3",
        "input": DATA / "gencode.v49.chr_patch_hapl_scaff.basic.annotation.gff3.gz",
        "fmt": "gff3",
    },
]


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


def gffbase_ingest_script(input_path: Path, dbfn: Path, fmt: str) -> str:
    return f"""
import json, time, sys
sys.path.insert(0, {str(ROOT / "python")!r})
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
               force_gff={"False" if fmt == "gtf" else "True"})
elapsed = time.perf_counter() - t0
print(json.dumps({{
    "wall_seconds": elapsed,
    "n_features":   db.count_features_of_type(),
    # Reported so the driver can refuse to publish a "spatial qps" number
    # that was actually measured on the B-tree fallback. `INSTALL spatial`
    # needs network egress, and on a firewalled node it fails silently.
    "rtree_built":  db._rtree_built,
    "fmt":          db.fmt,
}}))
"""


def legacy_ingest_script(input_path: Path, dbfn: Path) -> str:
    # Match the Phase-11 canonical legacy configuration: inference
    # enabled so the comparison is apples-to-apples against gffbase's
    # full ingest pipeline (which DOES synthesize gene/transcript
    # parents on GTF). The `disable_infer_*=True` flags would skip the
    # very work that makes legacy hours-slow on GENCODE.
    return f"""
import json, time, gffutils
t0 = time.perf_counter()
db = gffutils.create_db(
    {str(input_path)!r}, {str(dbfn)!r}, force=True,
    keep_order=False, sort_attribute_values=False,
    merge_strategy="create_unique", verbose=False,
)
elapsed = time.perf_counter() - t0
print(json.dumps({{
    "wall_seconds": elapsed,
    "n_features":   db.count_features_of_type(),
}}))
"""


def run_legacy_with_timeout(input_path: Path, dbfn: Path, timeout: int, n_input_lines: int) -> dict:
    """Run legacy gffutils ingest, capped at `timeout` seconds.

    On timeout the run yields a LOWER BOUND, not an estimate: `wall_seconds`
    is left null and `wall_seconds_lower_bound` is the cap. Downstream, that
    turns the speedup into `speedup_lower_bound` and the rendered table into
    `> N×`.

    This replaces a hardcoded `wall_seconds = timeout * 2.0`. That factor had
    no measurement behind it -- its own comment conceded there was no way to
    observe gffutils' progress -- yet it was the sole source of the published
    "≥ 2 hr 30 min" legacy wall and the "≥ 32×" headline speedup. A number
    invented by multiplying a timeout is not a benchmark result, and quoting
    it next to measured ones invites a reviewer to distrust all of them.

    A floor is weaker-sounding and unfalsifiable: legacy provably did not
    finish in `timeout` seconds, because we watched it not finish.
    """
    script = legacy_ingest_script(input_path, dbfn)
    result = run_subprocess(
        script,
        label=f"legacy gffutils ingest({input_path.name})",
        timeout=timeout,
    )
    if result.get("timed_out"):
        result["wall_seconds"] = None
        result["wall_seconds_lower_bound"] = float(timeout)
        result["cap_seconds"] = timeout
        result["note"] = (
            f"killed at the {timeout} s safety valve without finishing; "
            f"the true wall is greater than this, by an unmeasured amount"
        )
    return result


def sample_regions_from_db(
    db_path: Path, n: int = 5000, seed: int = REGION_SEED
) -> list[tuple[str, int, int]]:
    import duckdb

    con = duckdb.connect(str(db_path), read_only=True)
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


def bench_spatial(db_path: Path, regions, repeats: int = 1) -> dict:
    import gffbase

    # `with`, not a bare constructor: a writable DuckDB connection holds an
    # exclusive lock, and the sweep purges each corpus's databases as soon as
    # its numbers are recorded. Leaking the handle left the file locked, which
    # is merely untidy on POSIX and blocks the delete outright on Windows.
    with gffbase.FeatureDB(str(db_path), read_only=True) as db:
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


def bench_batched(db_path: Path, n_genes: int = 5000, repeats: int = 1) -> dict:
    import gffbase

    # See `bench_spatial` on why this is a `with` block.
    db = gffbase.FeatureDB(str(db_path), read_only=True)
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
        return str(path)


def run_one(corpus: dict, args) -> dict:
    name = corpus["name"]
    key = corpus["key"]
    inp = corpus["input"]
    print(f"\n=== {name} ({inp.name}) ===", flush=True)
    if not inp.exists():
        msg = f"missing input: {inp}"
        print("  SKIP —", msg, flush=True)
        return {"name": name, "key": key, "error": msg}

    require_free_disk(DISK_NEED_GIB.get(key, 8.0), what=f"corpus {key}")

    n_lines = count_feature_lines(inp)
    print(f"  feature lines: {n_lines:,}", flush=True)

    gffbase_db = OUT / f"{key}.duckdb"
    legacy_db = OUT / f"{key}_legacy.sqlite"

    # ---- gffbase ingest ----
    print("  [gffbase] ingest…", flush=True)
    if gffbase_db.exists():
        gffbase_db.unlink()
    g_info = run_subprocess(
        gffbase_ingest_script(inp, gffbase_db, corpus["fmt"]),
        label=f"gffbase ingest({inp.name})",
        timeout=args.gffbase_timeout,
    )
    g_info["disk_bytes"] = du(gffbase_db)
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
    print(f"  [legacy ] ingest (timeout {args.legacy_timeout}s)…", flush=True)
    if legacy_db.exists():
        legacy_db.unlink()
    l_info = run_legacy_with_timeout(inp, legacy_db, args.legacy_timeout, n_lines)
    l_info["disk_bytes"] = du(legacy_db)
    if l_info.get("timed_out"):
        print(
            f"    KILLED at the {args.legacy_timeout}s cap without finishing "
            f"— wall is > {pretty_seconds(float(args.legacy_timeout))}",
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
    g_n, l_n = g_info.get("n_features"), l_info.get("n_features")
    counts_agree = g_n is not None and l_n is not None and g_n == l_n
    if g_n is not None and l_n is not None and not counts_agree:
        print(
            f"    !! feature-count mismatch: gffbase={g_n:,} legacy={l_n:,}. "
            "Refusing to report a speedup between different workloads.",
            flush=True,
        )

    # A completed legacy run gives a speedup; a capped one gives a floor.
    # Keeping them in DIFFERENT keys is what stops a renderer printing a
    # bound as though it were a measurement.
    speedup = speedup_lower_bound = None
    g_wall = g_info.get("wall_seconds")
    if g_wall and counts_agree:
        if l_info.get("wall_seconds"):
            speedup = l_info["wall_seconds"] / g_wall
        elif l_info.get("wall_seconds_lower_bound"):
            speedup_lower_bound = l_info["wall_seconds_lower_bound"] / g_wall

    # ---- spatial routing ----
    print(f"  [gffbase] spatial — {args.n_spatial} regions…", flush=True)
    spatial = {"skipped": "no DB"} if not gffbase_db.exists() else None
    if spatial is None:
        regions = sample_regions_from_db(gffbase_db, n=args.n_spatial)
        if regions:
            spatial = bench_spatial(gffbase_db, regions, repeats=args.repeats)
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
        batched = bench_batched(gffbase_db, n_genes=args.n_batched, repeats=args.repeats)
        if "skipped" not in batched:
            print(
                f"    wall={pretty_seconds(batched['wall_seconds'])}, "
                f"anchors={batched['n_anchors']}, "
                f"descendants={batched['n_descendants']}",
                flush=True,
            )

    return {
        "name": name,
        "key": key,
        "input": rel(inp),
        "input_bytes": inp.stat().st_size if inp.exists() else 0,
        "feature_lines": n_lines,
        "gffbase": g_info,
        "legacy": l_info,
        "ingest_speedup": speedup,
        "ingest_speedup_lower_bound": speedup_lower_bound,
        "spatial": spatial,
        "batched": batched,
        "db_paths": {"gffbase": rel(gffbase_db), "legacy": rel(legacy_db)},
        "params": {
            "legacy_timeout_sec": args.legacy_timeout,
            "gffbase_timeout_sec": args.gffbase_timeout,
            "n_spatial": args.n_spatial,
            "n_batched": args.n_batched,
            "repeats": args.repeats,
            "region_seed": REGION_SEED,
        },
    }


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument(
        "--legacy-timeout",
        type=int,
        default=5400,
        help="seconds before legacy ingest is killed (default 5400 = 90 min). "
        "A killed run is reported as a lower bound, never extrapolated.",
    )
    ap.add_argument(
        "--gffbase-timeout", type=int, default=3600, help="seconds before gffbase ingest is killed"
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

    OUT.mkdir(parents=True, exist_ok=True)
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
        except SystemExit:
            raise
        except Exception as exc:  # pragma: no cover - top-level guard
            print(f"  ERROR on {corpus['name']}: {exc}", flush=True)
            row = {"name": corpus["name"], "key": key, "error": str(exc)}
        results[key] = row

        # Write after EVERY corpus, not once at the end. A five-hour sweep
        # that dies on the last corpus used to lose all of it.
        merge_results("06_mega", "corpora", {key: row})

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
        elif legacy.get("wall_seconds_lower_bound"):
            legacy_cell = "> " + pretty_seconds(legacy["wall_seconds_lower_bound"])
        else:
            legacy_cell = "fail"
        if row.get("ingest_speedup"):
            speed_cell = f"{row['ingest_speedup']:.2f}x"
        elif row.get("ingest_speedup_lower_bound"):
            speed_cell = f"> {row['ingest_speedup_lower_bound']:.2f}x"
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
    print(
        "'>' = legacy was killed at the safety valve without finishing, so the "
        "wall and the speedup are floors. No value is extrapolated.",
        flush=True,
    )


if __name__ == "__main__":
    main()
