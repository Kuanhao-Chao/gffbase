"""Phase 13 — vectorized batched API benchmark vs legacy per-id loop.

Drives `children_batched(format='arrow')` against `for id in ids:
list(db.children(id))` at three scales (500 / 5 000 / 50 000 genes).

Each engine runs in its own subprocess so peak-RSS measurements aren't
contaminated by the parent process's allocator state.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "python"))

import json

from benchmarks.common import (
    GFFBASE_DB, LEGACY_DB, OUT,
    pretty_bytes, pretty_seconds, run_subprocess, write_results,
)


def sample_common_ids(n: int, seed: int = 20260501):
    """Pick gene IDs that exist in BOTH DBs so per-engine descendant counts
    can be compared apples-to-apples."""
    import duckdb, random, sqlite3
    duck = duckdb.connect(str(GFFBASE_DB), read_only=True)
    duck_genes = {r[0] for r in duck.execute(
        "SELECT id FROM features WHERE featuretype = 'gene'"
    ).fetchall()}
    duck.close()
    sq = sqlite3.connect(str(LEGACY_DB))
    sq_genes = {r[0] for r in sq.execute(
        "SELECT id FROM features WHERE featuretype = 'gene'"
    ).fetchall()}
    sq.close()
    common = sorted(duck_genes & sq_genes)
    rng = random.Random(seed)
    return rng.sample(common, min(n, len(common)))


def _ids_path_payload(gene_ids, tag: str) -> Path:
    """Persist the gene_id list to a JSON file under benchmarks/out/ so the
    subprocess can read it via path argument (avoids ARG_MAX overflow at
    50k+ ids)."""
    p = OUT / f"_ids_{tag}.json"
    p.write_text(json.dumps(gene_ids))
    return p


def gffbase_batched_script(ids_path: Path):
    return f"""
import json, time, sys
sys.path.insert(0, {str(ROOT / 'python')!r})
from gffbase import FeatureDB
db = FeatureDB({str(GFFBASE_DB)!r})
gene_ids = json.load(open({str(ids_path)!r}))
t0 = time.perf_counter()
table = db.children_batched(gene_ids, format='arrow')
elapsed = time.perf_counter() - t0
print(json.dumps({{
    "wall_seconds": elapsed,
    "n_genes": len(gene_ids),
    "n_descendants": table.num_rows,
    "n_columns": len(table.column_names),
    "approach": "gffbase.children_batched(format='arrow')",
}}))
"""


def gffbase_loop_script(ids_path: Path):
    return f"""
import json, time, sys
sys.path.insert(0, {str(ROOT / 'python')!r})
from gffbase import FeatureDB
db = FeatureDB({str(GFFBASE_DB)!r})
gene_ids = json.load(open({str(ids_path)!r}))
t0 = time.perf_counter()
total = 0
for gid in gene_ids:
    for _ in db.children(gid, level=None):
        total += 1
elapsed = time.perf_counter() - t0
print(json.dumps({{
    "wall_seconds": elapsed,
    "n_genes": len(gene_ids),
    "n_descendants": total,
    "approach": "gffbase row-by-row children() loop",
}}))
"""


def legacy_loop_script(ids_path: Path):
    return f"""
import json, time, gffutils
db = gffutils.FeatureDB({str(LEGACY_DB)!r})
gene_ids = json.load(open({str(ids_path)!r}))
t0 = time.perf_counter()
total = 0
for gid in gene_ids:
    for _ in db.children(gid, level=None):
        total += 1
elapsed = time.perf_counter() - t0
print(json.dumps({{
    "wall_seconds": elapsed,
    "n_genes": len(gene_ids),
    "n_descendants": total,
    "approach": "legacy gffutils row-by-row children() loop",
}}))
"""


def run_one_scale(n: int, *, timeout_per_run: int = 1800) -> dict:
    print(f"\n[vectorized] sampling {n} common gene IDs…", flush=True)
    gene_ids = sample_common_ids(n)
    print(f"  sampled {len(gene_ids)} genes", flush=True)

    ids_path = _ids_path_payload(gene_ids, str(n))

    print(f"[vectorized] gffbase children_batched (format='arrow')…", flush=True)
    batched = run_subprocess(gffbase_batched_script(ids_path),
                             label=f"gffbase.batched(n={n})",
                             timeout=timeout_per_run)
    print(f"  wall={pretty_seconds(batched['wall_seconds'])}, "
          f"RSS={pretty_bytes(batched['peak_rss_bytes'])}, "
          f"descendants={batched.get('n_descendants')}",
          flush=True)

    print(f"[vectorized] gffbase row-by-row loop…", flush=True)
    g_loop = run_subprocess(gffbase_loop_script(ids_path),
                            label=f"gffbase.loop(n={n})",
                            timeout=timeout_per_run)
    print(f"  wall={pretty_seconds(g_loop['wall_seconds'])}, "
          f"RSS={pretty_bytes(g_loop['peak_rss_bytes'])}, "
          f"descendants={g_loop.get('n_descendants')}",
          flush=True)

    print(f"[vectorized] legacy gffutils loop…", flush=True)
    legacy = run_subprocess(legacy_loop_script(ids_path),
                            label=f"legacy.loop(n={n})",
                            timeout=timeout_per_run)
    print(f"  wall={pretty_seconds(legacy['wall_seconds'])}, "
          f"RSS={pretty_bytes(legacy['peak_rss_bytes'])}, "
          f"descendants={legacy.get('n_descendants')}",
          flush=True)

    speedup_vs_loop = (g_loop["wall_seconds"] / batched["wall_seconds"]
                       if batched["wall_seconds"] else None)
    speedup_vs_legacy = (legacy["wall_seconds"] / batched["wall_seconds"]
                         if batched["wall_seconds"] else None)

    return {
        "n_genes": len(gene_ids),
        "gffbase_batched": batched,
        "gffbase_loop": g_loop,
        "legacy_loop": legacy,
        "comparison": {
            "batched_speedup_vs_gffbase_loop": speedup_vs_loop,
            "batched_speedup_vs_legacy_loop": speedup_vs_legacy,
            "batched_qps": (len(gene_ids) / batched["wall_seconds"]
                            if batched["wall_seconds"] else None),
            "legacy_loop_qps": (len(gene_ids) / legacy["wall_seconds"]
                                if legacy["wall_seconds"] else None),
            "rss_ratio_legacy_over_batched":
                legacy["peak_rss_bytes"] / max(batched["peak_rss_bytes"], 1),
        },
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--scales", default="500,5000,50000",
                    help="comma-separated batch sizes (default 500,5000,50000)")
    ap.add_argument("--timeout-per-run", type=int, default=1800)
    args = ap.parse_args()

    scales = [int(s) for s in args.scales.split(",") if s.strip()]
    payload: dict = {"scales": {}}
    for n in scales:
        payload["scales"][str(n)] = run_one_scale(n, timeout_per_run=args.timeout_per_run)

    p = write_results("05_vectorized", payload)
    print(f"\nResults → {p}", flush=True)

    # Print a compact summary table.
    print("\n" + "=" * 78, flush=True)
    print(f"{'scale':>8}  {'batched':>10}  {'gffbase loop':>14}  {'legacy loop':>14}  "
          f"{'speedup vs loop':>18}  {'speedup vs legacy':>20}",
          flush=True)
    print("-" * 78, flush=True)
    for n_str, r in payload["scales"].items():
        b = r["gffbase_batched"]["wall_seconds"]
        gl = r["gffbase_loop"]["wall_seconds"]
        ll = r["legacy_loop"]["wall_seconds"]
        sp1 = r["comparison"]["batched_speedup_vs_gffbase_loop"]
        sp2 = r["comparison"]["batched_speedup_vs_legacy_loop"]
        print(f"{n_str:>8}  "
              f"{pretty_seconds(b):>10}  "
              f"{pretty_seconds(gl):>14}  "
              f"{pretty_seconds(ll):>14}  "
              f"{(f'{sp1:.2f}×' if sp1 else 'n/a'):>18}  "
              f"{(f'{sp2:.2f}×' if sp2 else 'n/a'):>20}",
              flush=True)
    print("=" * 78, flush=True)


if __name__ == "__main__":
    main()
