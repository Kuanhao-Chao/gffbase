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
"""Relational query benchmark — children(level=None) over N random genes.

gffbase's auto-routed dispatcher (closure cache for shallow hierarchies +
dynamic CTE for overflow) vs. gffutils' SQL recursion through the relations
table.
"""

from __future__ import annotations

import argparse
import random
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "python"))

from benchmarks.common import (
    GFFBASE_DB,
    LEGACY_DB,
    pretty_seconds,
    write_results,
)


def sample_gene_ids(n: int, seed: int = 20260501):
    """Pick gene IDs that exist in BOTH databases so the per-engine descendant
    counts can be compared apples-to-apples."""
    import sqlite3

    import duckdb

    duck = duckdb.connect(str(GFFBASE_DB), read_only=True)
    duck_genes = {
        r[0] for r in duck.execute("SELECT id FROM features WHERE featuretype = 'gene'").fetchall()
    }
    duck.close()
    sq = sqlite3.connect(str(LEGACY_DB))
    sq_genes = {
        r[0] for r in sq.execute("SELECT id FROM features WHERE featuretype = 'gene'").fetchall()
    }
    sq.close()
    common = sorted(duck_genes & sq_genes)
    rng = random.Random(seed)
    return rng.sample(common, min(n, len(common)))


def run_gffbase(gene_ids, *, force_dynamic: bool = False):
    import gffbase

    db = gffbase.FeatureDB(str(GFFBASE_DB))
    # Forcing the dynamic recursive CTE means defeating the closure-table
    # cache. The knob for that is `_closure_max_depth`, NOT `_max_depth`.
    #
    # This used to set `_max_depth = 0`, which did nothing at all: for
    # `level=None` the dispatcher (`interface.py:_dispatch_relation`) tests
    # `self._closure_max_depth == 0`, and `_max_depth` is only consulted when
    # `level` is an int. So `force_dynamic=True` measured the cached path a
    # second time, and the published "forced dynamic CTE" column was really
    # two runs of the cache. It looked plausible precisely because the two
    # numbers were close.
    #
    # Restored in a `finally` so a raising benchmark cannot leave a
    # half-configured handle behind for whatever runs next in-process.
    saved = db._closure_max_depth
    if force_dynamic:
        db._closure_max_depth = 0
    total = 0
    try:
        t0 = time.perf_counter()
        for gid in gene_ids:
            for _ in db.children(gid, level=None):
                total += 1
        elapsed = time.perf_counter() - t0
    finally:
        db._closure_max_depth = saved
    return elapsed, total


def run_legacy(gene_ids):
    import gffutils

    db = gffutils.FeatureDB(str(LEGACY_DB))
    total = 0
    t0 = time.perf_counter()
    for gid in gene_ids:
        for _ in db.children(gid, level=None):
            total += 1
    elapsed = time.perf_counter() - t0
    return elapsed, total


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--n-genes", type=int, default=500)
    args = ap.parse_args()

    print(f"[relational] sampling {args.n_genes} common gene IDs…", flush=True)
    gene_ids = sample_gene_ids(args.n_genes)
    print(f"  sampled {len(gene_ids)} genes", flush=True)

    print("[relational] gffbase auto-routed (closure cache by default)…", flush=True)
    g_auto_wall, g_auto_total = run_gffbase(gene_ids, force_dynamic=False)
    print(
        f"  wall={pretty_seconds(g_auto_wall)}, "
        f"qps={len(gene_ids) / g_auto_wall:.1f}, "
        f"descendants={g_auto_total}",
        flush=True,
    )

    print("[relational] gffbase forced dynamic CTE…", flush=True)
    g_dyn_wall, g_dyn_total = run_gffbase(gene_ids, force_dynamic=True)
    print(
        f"  wall={pretty_seconds(g_dyn_wall)}, "
        f"qps={len(gene_ids) / g_dyn_wall:.1f}, "
        f"descendants={g_dyn_total}",
        flush=True,
    )

    print("[relational] legacy gffutils.children(level=None)…", flush=True)
    lg_wall, lg_total = run_legacy(gene_ids)
    print(
        f"  wall={pretty_seconds(lg_wall)}, "
        f"qps={len(gene_ids) / lg_wall:.1f}, "
        f"descendants={lg_total}",
        flush=True,
    )

    payload = {
        "n_genes": len(gene_ids),
        "gffbase_auto": {
            "wall_seconds": g_auto_wall,
            "qps": len(gene_ids) / g_auto_wall if g_auto_wall else None,
            "descendants": g_auto_total,
        },
        "gffbase_forced_dynamic": {
            "wall_seconds": g_dyn_wall,
            "qps": len(gene_ids) / g_dyn_wall if g_dyn_wall else None,
            "descendants": g_dyn_total,
        },
        "legacy": {
            "wall_seconds": lg_wall,
            "qps": len(gene_ids) / lg_wall if lg_wall else None,
            "descendants": lg_total,
        },
        "comparison": {
            "auto_speedup_vs_legacy": lg_wall / g_auto_wall if g_auto_wall else None,
            "dynamic_speedup_vs_legacy": lg_wall / g_dyn_wall if g_dyn_wall else None,
            # gffbase's IDs are sometimes synthetic (gene_id without ENSG prefix);
            # we report descendants returned but don't strict-equal them.
            "gffbase_legacy_descendants_match": g_auto_total == lg_total,
        },
    }
    p = write_results("03_relational", payload)
    print(f"\nResults → {p}", flush=True)


if __name__ == "__main__":
    main()
