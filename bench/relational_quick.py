"""Fast relational-routing measurement on the persisted GENCODE DuckDB.

Exercises three modes:
  1. Auto-routed (current default; for GENCODE depth=3 the dispatcher picks
     the dynamic CTE).
  2. Forced cache (set _max_depth high enough to skip overflow check, do not
     toggle the dispatcher; we monkey-patch _dispatch_relation to return False
     so the cache path is always used).
  3. Forced dynamic (monkey-patch to True).

Reports wall + per-query qps for `children(level=None)` on N random genes.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "python"))

from gffbase import FeatureDB


def bench(db, gene_ids, force):
    """force ∈ {None, 'cache', 'dynamic'}.
    None lets the dispatcher decide."""
    saved_dispatch = db._dispatch_relation
    if force == "cache":
        db._dispatch_relation = lambda *a, **kw: False
    elif force == "dynamic":
        db._dispatch_relation = lambda *a, **kw: True
    total = 0
    t0 = time.perf_counter()
    for fid in gene_ids:
        for _ in db.children(fid, level=None):
            total += 1
    elapsed = time.perf_counter() - t0
    db._dispatch_relation = saved_dispatch
    return {
        "wall_seconds": elapsed,
        "qps": len(gene_ids) / elapsed if elapsed else float("inf"),
        "total_descendants": total,
    }


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--db", default=str(ROOT / "bench" / "out" / "gencode.duckdb"))
    p.add_argument("--n", type=int, default=500)
    p.add_argument("--out", default=str(ROOT / "bench" / "out" / "relational_phase7.json"))
    args = p.parse_args()

    db = FeatureDB(args.db)
    print(f"closure_max_depth = {db._closure_max_depth}", flush=True)
    print(f"max_depth         = {db._max_depth}", flush=True)
    print(f"rtree_built       = {db._rtree_built}", flush=True)

    gene_ids = [r[0] for r in db.conn.execute(
        "SELECT id FROM features WHERE featuretype = 'gene' "
        "ORDER BY id LIMIT ?", [args.n],
    ).fetchall()]
    print(f"gene sample size  = {len(gene_ids)}", flush=True)

    results = {}
    for label in ("auto", "cache", "dynamic"):
        force = None if label == "auto" else label
        r = bench(db, gene_ids, force=force)
        print(f"{label:7s}: wall={r['wall_seconds']:.3f}s, "
              f"qps={r['qps']:.0f}, descs={r['total_descendants']}",
              flush=True)
        results[label] = r

    speedup_auto_vs_cache = (results["cache"]["wall_seconds"]
                             / results["auto"]["wall_seconds"]) if results["auto"]["wall_seconds"] else 0
    print(f"\nauto vs forced-cache speedup: {speedup_auto_vs_cache:.2f}×",
          flush=True)
    results["auto_speedup_vs_cache"] = speedup_auto_vs_cache
    Path(args.out).write_text(json.dumps(results, indent=2))


if __name__ == "__main__":
    main()
