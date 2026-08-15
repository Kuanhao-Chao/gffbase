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
"""Spatial query benchmark — gffbase R-tree vs legacy gffutils UCSC bin index.

Generates N random region queries grounded in the actual feature span on each
chromosome, runs them against both engines, asserts the two engines return the
same set of feature counts, and reports per-engine wall + percentile latency.
"""

from __future__ import annotations

import argparse
import random
import statistics
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "python"))

import duckdb

from benchmarks.common import (
    GFFBASE_DB,
    LEGACY_DB,
    pretty_seconds,
    write_results,
)


def sample_regions(n: int, seed: int = 20260501) -> list[tuple[str, int, int]]:
    """Sample regions grounded in the gffbase DuckDB feature spans so we never
    query off the end of a chromosome."""
    con = duckdb.connect(str(GFFBASE_DB), read_only=True)
    rows = con.execute("""
        SELECT seqid, MIN(start) AS lo, MAX("end") AS hi
        FROM features GROUP BY seqid HAVING COUNT(*) > 50
    """).fetchall()
    con.close()
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


def run_gffbase(regions, *, force_btree: bool):
    import gffbase

    db = gffbase.FeatureDB(str(GFFBASE_DB))
    saved = db._rtree_built
    if force_btree:
        db._rtree_built = False
    latencies = []
    total_features = 0
    t0 = time.perf_counter()
    for seqid, rs, re_ in regions:
        q0 = time.perf_counter()
        n = sum(1 for _ in db.region(seqid=seqid, start=rs, end=re_, featuretype="exon"))
        latencies.append(time.perf_counter() - q0)
        total_features += n
    elapsed = time.perf_counter() - t0
    db._rtree_built = saved
    return latencies, elapsed, total_features


def run_legacy(regions):
    import gffutils

    db = gffutils.FeatureDB(str(LEGACY_DB))
    latencies = []
    total_features = 0
    t0 = time.perf_counter()
    for seqid, rs, re_ in regions:
        q0 = time.perf_counter()
        n = sum(1 for _ in db.region(seqid=seqid, start=rs, end=re_, featuretype="exon"))
        latencies.append(time.perf_counter() - q0)
        total_features += n
    elapsed = time.perf_counter() - t0
    return latencies, elapsed, total_features


def summarize(latencies, elapsed, n_returned, label):
    qps = len(latencies) / elapsed if elapsed else float("inf")
    return {
        "label": label,
        "n_queries": len(latencies),
        "wall_seconds": elapsed,
        "qps": qps,
        "mean_ms": statistics.mean(latencies) * 1000,
        "p50_ms": statistics.median(latencies) * 1000,
        "p95_ms": (sorted(latencies)[int(len(latencies) * 0.95)] * 1000),
        "max_ms": max(latencies) * 1000,
        "total_features_returned": n_returned,
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--n-queries", type=int, default=5000)
    args = ap.parse_args()

    print(f"[spatial] sampling {args.n_queries} random regions…", flush=True)
    regions = sample_regions(args.n_queries)
    print(f"  sampled {len(regions)} regions", flush=True)

    print("[spatial] gffbase R-tree path…", flush=True)
    rt_lat, rt_elapsed, rt_n = run_gffbase(regions, force_btree=False)
    rt_summary = summarize(rt_lat, rt_elapsed, rt_n, "gffbase rtree")
    print(
        f"  wall={pretty_seconds(rt_elapsed)}, qps={rt_summary['qps']:.0f}, "
        f"p50={rt_summary['p50_ms']:.2f}ms, p95={rt_summary['p95_ms']:.2f}ms",
        flush=True,
    )

    print("[spatial] gffbase B-tree fallback path…", flush=True)
    bt_lat, bt_elapsed, bt_n = run_gffbase(regions, force_btree=True)
    bt_summary = summarize(bt_lat, bt_elapsed, bt_n, "gffbase btree")
    print(
        f"  wall={pretty_seconds(bt_elapsed)}, qps={bt_summary['qps']:.0f}, "
        f"p50={bt_summary['p50_ms']:.2f}ms, p95={bt_summary['p95_ms']:.2f}ms",
        flush=True,
    )

    print("[spatial] legacy gffutils.region…", flush=True)
    lg_lat, lg_elapsed, lg_n = run_legacy(regions)
    lg_summary = summarize(lg_lat, lg_elapsed, lg_n, "legacy gffutils")
    print(
        f"  wall={pretty_seconds(lg_elapsed)}, qps={lg_summary['qps']:.0f}, "
        f"p50={lg_summary['p50_ms']:.2f}ms, p95={lg_summary['p95_ms']:.2f}ms",
        flush=True,
    )

    payload = {
        "n_queries": len(regions),
        "gffbase_rtree": rt_summary,
        "gffbase_btree": bt_summary,
        "legacy": lg_summary,
        "comparison": {
            "rtree_speedup_vs_legacy": lg_elapsed / rt_elapsed if rt_elapsed else None,
            "btree_speedup_vs_legacy": lg_elapsed / bt_elapsed if bt_elapsed else None,
            "rtree_speedup_vs_btree": bt_elapsed / rt_elapsed if rt_elapsed else None,
            "feature_counts_gffbase_rtree_vs_btree_match": rt_n == bt_n,
            "feature_counts_gffbase_vs_legacy_match": rt_n == lg_n,
        },
    }
    p = write_results("02_spatial", payload)
    print(f"\nResults → {p}", flush=True)


if __name__ == "__main__":
    main()
