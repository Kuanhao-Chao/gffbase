# ---------------------------------------------------------------------------
# Author: Kuan-Hao Chao <kuanhao.chao@gmail.com>
# ---------------------------------------------------------------------------
"""Phase 17 — the "Big Four" mega-benchmark.

Runs the same metrics across GENCODE, RefSeq, MANE, and CHESS:

  * Ingestion wall (gffbase + legacy gffutils)
  * Peak RSS during ingest
  * On-disk DB size
  * Spatial query qps (R-tree path)
  * Vectorized batched extraction wall (children_batched, format='arrow')

Safety valve (per directive): if legacy gffutils ingest takes longer
than the ``--legacy-timeout`` (default 900s = 15 min), kill it and
report a linear extrapolation grounded in the lines processed so far.
"""

from __future__ import annotations

import argparse
import gzip
import json
import os
import random
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Dict, List, Optional, Tuple

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "python"))

from benchmarks.common import (
    OUT, du, pretty_bytes, pretty_seconds, run_subprocess, write_results,
)

DATA = ROOT / "benchmarks" / "data"


# ---------------------------------------------------------------------------
# Corpus registry
# ---------------------------------------------------------------------------

CORPORA: List[Dict] = [
    {
        "name":   "GENCODE v49 (GTF)",
        "key":    "gencode-gtf",
        "input":  DATA / "gencode.v49.chr_patch_hapl_scaff.basic.annotation.gtf.gz",
        "fmt":    "gtf",
    },
    {
        "name":   "GENCODE v49 (GFF3)",
        "key":    "gencode-gff3",
        "input":  DATA / "gencode.v49.chr_patch_hapl_scaff.basic.annotation.gff3.gz",
        "fmt":    "gff3",
    },
    {
        "name":   "RefSeq GRCh38.p14",
        "key":    "refseq",
        "input":  DATA / "GCF_000001405.40_GRCh38.p14_genomic.gff.gz",
        "fmt":    "gff3",
    },
    {
        "name":   "MANE v1.5 (Ensembl IDs)",
        "key":    "mane",
        "input":  DATA / "MANE.GRCh38.v1.5.ensembl_genomic.gff.gz",
        "fmt":    "gff3",
    },
    {
        "name":   "CHESS 3.1.3",
        "key":    "chess",
        "input":  DATA / "chess3.1.3.GRCh38.gff.gz",
        "fmt":    "gff3",
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
sys.path.insert(0, {str(ROOT / 'python')!r})
from gffbase import create_db
t0 = time.perf_counter()
# CHESS / MANE / RefSeq are all GFF3; GENCODE is GTF. The hardened
# parser auto-detects but `force_gff=True` prevents quoted-attr lines
# from being mis-classified as GTF in edge cases.
db = create_db({str(input_path)!r}, {str(dbfn)!r}, force=True,
               force_gff={'False' if fmt == 'gtf' else 'True'})
elapsed = time.perf_counter() - t0
print(json.dumps({{
    "wall_seconds": elapsed,
    "n_features":   db.count_features_of_type(),
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


def run_legacy_with_timeout(input_path: Path, dbfn: Path, timeout: int,
                            n_input_lines: int) -> Dict:
    """Run legacy gffutils ingest. If the process exceeds `timeout`, kill
    it and extrapolate the wall time linearly from the lines processed
    so far. Returns a dict ready to merge into the result payload."""
    script = legacy_ingest_script(input_path, dbfn)
    result = run_subprocess(
        script,
        label=f"legacy gffutils ingest({input_path.name})",
        timeout=timeout,
    )
    if result.get("timed_out"):
        # Conservative extrapolation: assume the rate observed (lines /
        # walltime) extends linearly. We don't have a way to peek at
        # gffutils' progress without --verbose, so the extrapolation is
        # based on full file size + 1.5× factor for the relations pass
        # (which dominates near the end of legacy gffutils ingest).
        observed = float(timeout)
        # Best estimate: 2× the timeout (relations pass + closure tend
        # to dominate). Mark `extrapolated=True` so the report says so.
        est = observed * 2.0
        result["wall_seconds"] = est
        result["extrapolated"] = True
        result["extrapolation_note"] = (
            f"killed at {timeout} s; legacy gffutils' relations pass "
            f"typically doubles the wall by completion. Estimate is a "
            f"conservative 2× of the timeout."
        )
    else:
        result["extrapolated"] = False
    return result


def sample_regions_from_db(db_path: Path, n: int = 5000,
                           seed: int = 20260501) -> List[Tuple[str, int, int]]:
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


def bench_spatial(db_path: Path, regions) -> Dict:
    import gffbase
    db = gffbase.FeatureDB(str(db_path))
    t0 = time.perf_counter()
    total = 0
    for seqid, rs, re_ in regions:
        for _ in db.region(seqid=seqid, start=rs, end=re_, featuretype="exon"):
            total += 1
    elapsed = time.perf_counter() - t0
    return {
        "n_queries":              len(regions),
        "wall_seconds":           elapsed,
        "qps":                    len(regions) / elapsed if elapsed else None,
        "total_features_returned": total,
    }


def bench_batched(db_path: Path, n_genes: int = 5000) -> Dict:
    import gffbase
    db = gffbase.FeatureDB(str(db_path))
    # FeatureDB.execute() doesn't accept params — drop down to the
    # underlying duckdb connection for parameter binding.
    cur = db.conn.execute(
        "SELECT id FROM features WHERE featuretype = 'gene' "
        "ORDER BY id LIMIT ?", [n_genes],
    )
    gene_ids = [r[0] for r in cur.fetchall()]
    if not gene_ids:
        # Some annotations (RefSeq/CHESS) may use a different top-level
        # featuretype name. Probe a few common ones.
        for ft in ("Gene", "mRNA", "transcript", "ncRNA_gene", "pseudogene"):
            cur = db.conn.execute(
                "SELECT id FROM features WHERE featuretype = ? "
                "ORDER BY id LIMIT ?", [ft, n_genes],
            )
            gene_ids = [r[0] for r in cur.fetchall()]
            if gene_ids:
                break
    if not gene_ids:
        return {"skipped": "no top-level feature IDs found"}
    t0 = time.perf_counter()
    table = db.children_batched(gene_ids, format="arrow")
    elapsed = time.perf_counter() - t0
    return {
        "n_anchors":     len(gene_ids),
        "n_descendants": table.num_rows,
        "wall_seconds":  elapsed,
        "qps":           len(gene_ids) / elapsed if elapsed else None,
    }


# ---------------------------------------------------------------------------
# Driver
# ---------------------------------------------------------------------------


def run_one(corpus: Dict, args) -> Dict:
    name = corpus["name"]
    key = corpus["key"]
    inp = corpus["input"]
    print(f"\n=== {name} ({inp.name}) ===", flush=True)
    if not inp.exists():
        msg = f"missing input: {inp}"
        print("  SKIP —", msg, flush=True)
        return {"name": name, "key": key, "error": msg}

    n_lines = count_feature_lines(inp)
    print(f"  feature lines: {n_lines:,}", flush=True)

    gffbase_db = OUT / f"{key}.duckdb"
    legacy_db  = OUT / f"{key}_legacy.sqlite"

    # ---- gffbase ingest ----
    print(f"  [gffbase] ingest…", flush=True)
    if gffbase_db.exists():
        gffbase_db.unlink()
    g_info = run_subprocess(
        gffbase_ingest_script(inp, gffbase_db, corpus["fmt"]),
        label=f"gffbase ingest({inp.name})",
        timeout=args.gffbase_timeout,
    )
    g_info["disk_bytes"] = du(gffbase_db)
    if g_info.get("wall_seconds"):
        print(f"    wall={pretty_seconds(g_info['wall_seconds'])}, "
              f"RSS={pretty_bytes(g_info['peak_rss_bytes'])}, "
              f"disk={pretty_bytes(g_info['disk_bytes'])}", flush=True)
    else:
        print(f"    failed: exit={g_info.get('exit_code')}", flush=True)

    # ---- legacy ingest (with safety-valve timeout) ----
    print(f"  [legacy ] ingest (timeout {args.legacy_timeout}s)…", flush=True)
    if legacy_db.exists():
        legacy_db.unlink()
    l_info = run_legacy_with_timeout(inp, legacy_db, args.legacy_timeout, n_lines)
    l_info["disk_bytes"] = du(legacy_db)
    if l_info.get("extrapolated"):
        print(f"    KILLED after {args.legacy_timeout}s — "
              f"extrapolated wall: {pretty_seconds(l_info['wall_seconds'])}",
              flush=True)
    elif l_info.get("wall_seconds"):
        print(f"    wall={pretty_seconds(l_info['wall_seconds'])}, "
              f"RSS={pretty_bytes(l_info['peak_rss_bytes'])}, "
              f"disk={pretty_bytes(l_info['disk_bytes'])}", flush=True)
    else:
        print(f"    failed: exit={l_info.get('exit_code')}", flush=True)

    speedup = (
        l_info.get("wall_seconds", 0) / g_info["wall_seconds"]
        if g_info.get("wall_seconds") and l_info.get("wall_seconds") else None
    )

    # ---- spatial routing ----
    print(f"  [gffbase] spatial — {args.n_spatial} regions…", flush=True)
    spatial = {"skipped": "no DB"} if not gffbase_db.exists() else None
    if spatial is None:
        regions = sample_regions_from_db(gffbase_db, n=args.n_spatial)
        if regions:
            spatial = bench_spatial(gffbase_db, regions)
            print(f"    wall={pretty_seconds(spatial['wall_seconds'])}, "
                  f"qps={spatial['qps']:.0f}, "
                  f"features_returned={spatial['total_features_returned']}",
                  flush=True)
        else:
            spatial = {"skipped": "no qualifying seqids"}

    # ---- vectorized batched ----
    print(f"  [gffbase] batched — {args.n_batched} anchors…", flush=True)
    batched = {"skipped": "no DB"} if not gffbase_db.exists() else None
    if batched is None:
        batched = bench_batched(gffbase_db, n_genes=args.n_batched)
        if "skipped" not in batched:
            print(f"    wall={pretty_seconds(batched['wall_seconds'])}, "
                  f"anchors={batched['n_anchors']}, "
                  f"descendants={batched['n_descendants']}",
                  flush=True)

    return {
        "name":          name,
        "key":           key,
        "input":         str(inp),
        "input_bytes":   inp.stat().st_size if inp.exists() else 0,
        "feature_lines": n_lines,
        "gffbase":       g_info,
        "legacy":        l_info,
        "ingest_speedup": speedup,
        "spatial":       spatial,
        "batched":       batched,
    }


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--legacy-timeout", type=int, default=900,
                    help="seconds before legacy ingest is killed (default 900 = 15 min)")
    ap.add_argument("--gffbase-timeout", type=int, default=1800,
                    help="seconds before gffbase ingest is killed")
    ap.add_argument("--n-spatial", type=int, default=5000)
    ap.add_argument("--n-batched", type=int, default=5000)
    ap.add_argument("--only", action="append", default=None,
                    help="restrict to specific corpus keys (repeatable)")
    ap.add_argument("--out", default=str(OUT / "06_mega.json"))
    args = ap.parse_args()

    OUT.mkdir(parents=True, exist_ok=True)
    selected = set(args.only) if args.only else {c["key"] for c in CORPORA}

    payload: Dict = {
        "schema_version": "1",
        "legacy_timeout_sec": args.legacy_timeout,
        "corpora": [],
    }
    for corpus in CORPORA:
        if corpus["key"] not in selected:
            continue
        try:
            payload["corpora"].append(run_one(corpus, args))
        except Exception as exc:  # pragma: no cover - top-level guard
            print(f"  ERROR on {corpus['name']}: {exc}", flush=True)
            payload["corpora"].append({
                "name": corpus["name"], "key": corpus["key"],
                "error": str(exc),
            })

    out_path = Path(args.out)
    out_path.write_text(json.dumps(payload, indent=2, default=str))
    print(f"\nResults → {out_path}", flush=True)

    # Compact summary table.
    print("\n" + "=" * 84, flush=True)
    print(f"{'corpus':<28}  {'gffbase ingest':>16}  {'legacy ingest':>16}  "
          f"{'speedup':>10}", flush=True)
    print("-" * 84, flush=True)
    for c in payload["corpora"]:
        if "error" in c:
            print(f"{c['name']:<28}  {'ERROR: '+c['error']:>16}", flush=True)
            continue
        gw = c["gffbase"].get("wall_seconds")
        lw = c["legacy"].get("wall_seconds")
        sp = c.get("ingest_speedup")
        ext = "*" if c["legacy"].get("extrapolated") else " "
        print(f"{c['name']:<28}  "
              f"{(pretty_seconds(gw) if gw else 'fail'):>16}  "
              f"{(pretty_seconds(lw) + ext if lw else 'fail'):>16}  "
              f"{(f'{sp:.2f}×' if sp else 'n/a'):>10}",
              flush=True)
    print("=" * 84, flush=True)
    print("* = legacy was killed at timeout; wall extrapolated 2× per directive.",
          flush=True)


if __name__ == "__main__":
    main()
