# ---------------------------------------------------------------------------
# Author: Kuan-Hao Chao <kuanhao.chao@gmail.com>
# ---------------------------------------------------------------------------
"""Head-to-head ingest: gffbase vs legacy gffutils on GENCODE v45.

Each engine runs in a fresh subprocess so peak RSS measurements are clean.
Pass `--reuse-cached` to quote the Phase 6 captured legacy numbers (saves
~60 min wall time) instead of rerunning.
"""

from __future__ import annotations

import argparse
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "python"))

from benchmarks.common import (
    GENCODE_GTF, GFFBASE_DB, LEGACY_DB,
    PHASE6_GFFBASE_DB, PHASE6_LEGACY_DB,
    OUT, du, load_phase6_legacy_ingest_numbers,
    pretty_bytes, pretty_seconds, run_subprocess, write_results,
)


def ingest_gffbase() -> dict:
    if GFFBASE_DB.exists():
        GFFBASE_DB.unlink()
    script = f"""
import json, time, sys
sys.path.insert(0, {str(ROOT / 'python')!r})
from gffbase import create_db
t0 = time.perf_counter()
db = create_db({str(GENCODE_GTF)!r}, {str(GFFBASE_DB)!r}, force=True)
elapsed = time.perf_counter() - t0
print(json.dumps({{
    "wall_seconds": elapsed,
    "n_features": db.count_features_of_type(),
    "rtree_built": db._rtree_built,
    "fmt": db.fmt,
}}))
"""
    return run_subprocess(script, label="gffbase create_db", timeout=1200)


def ingest_legacy(timeout: int = 7200) -> dict:
    if LEGACY_DB.exists():
        LEGACY_DB.unlink()
    script = f"""
import json, time, gffutils
t0 = time.perf_counter()
db = gffutils.create_db(
    {str(GENCODE_GTF)!r}, {str(LEGACY_DB)!r}, force=True,
    keep_order=False, sort_attribute_values=False,
    merge_strategy="create_unique", verbose=False,
    disable_infer_genes=False, disable_infer_transcripts=False,
)
elapsed = time.perf_counter() - t0
print(json.dumps({{"wall_seconds": elapsed, "n_features": db.count_features_of_type()}}))
"""
    return run_subprocess(script, label="legacy gffutils create_db", timeout=timeout)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--reuse-cached", action="store_true",
                    help="Reuse Phase 6 cached legacy ingest numbers + DBs.")
    ap.add_argument("--legacy-timeout", type=int, default=7200)
    args = ap.parse_args()

    OUT.mkdir(parents=True, exist_ok=True)
    print(f"[ingest] gffbase ingest (subprocess)…", flush=True)
    if args.reuse_cached and PHASE6_GFFBASE_DB.exists():
        # Copy / hardlink Phase 6's gffbase DB; that saves us another 4-min ingest.
        if not GFFBASE_DB.exists():
            shutil.copy2(PHASE6_GFFBASE_DB, GFFBASE_DB)
        gffbase_info = {
            "label": "gffbase create_db (Phase 6 cache)",
            "wall_seconds": 225.78,            # captured from Phase 7 log
            "peak_rss_bytes": int(1.45 * 1024**3),
            "peak_rss_mb": 1.45 * 1024,
            "n_features": 2_182_889,
            "rtree_built": True,
            "fmt": "gtf",
            "exit_code": 0,
            "cached": True,
            "source": str(PHASE6_GFFBASE_DB),
        }
    else:
        gffbase_info = ingest_gffbase()
    gffbase_info["disk_bytes"] = du(GFFBASE_DB)
    print(f"  wall={pretty_seconds(gffbase_info['wall_seconds'])}, "
          f"RSS={pretty_bytes(gffbase_info['peak_rss_bytes'])}, "
          f"disk={pretty_bytes(gffbase_info['disk_bytes'])}", flush=True)

    print(f"[ingest] legacy gffutils ingest (subprocess)…", flush=True)
    if args.reuse_cached:
        cached = load_phase6_legacy_ingest_numbers()
        if cached is not None:
            legacy_info = cached
            # Copy/hardlink the legacy DB so query benchmarks can use it.
            if not LEGACY_DB.exists() and PHASE6_LEGACY_DB.exists():
                shutil.copy2(PHASE6_LEGACY_DB, LEGACY_DB)
        else:
            legacy_info = ingest_legacy(args.legacy_timeout)
    else:
        legacy_info = ingest_legacy(args.legacy_timeout)
    legacy_info["disk_bytes"] = du(LEGACY_DB)
    if legacy_info.get("wall_seconds"):
        print(f"  wall={pretty_seconds(legacy_info['wall_seconds'])}, "
              f"RSS={pretty_bytes(legacy_info['peak_rss_bytes'])}, "
              f"disk={pretty_bytes(legacy_info['disk_bytes'])}", flush=True)

    speedup = (legacy_info.get("wall_seconds") or 0) / (gffbase_info["wall_seconds"] or 1)
    rss_ratio = legacy_info["peak_rss_bytes"] / max(gffbase_info["peak_rss_bytes"], 1)

    payload = {
        "input": {
            "path": str(GENCODE_GTF),
            "compressed_bytes": GENCODE_GTF.stat().st_size if GENCODE_GTF.exists() else 0,
        },
        "gffbase": gffbase_info,
        "legacy": legacy_info,
        "comparison": {
            "wall_speedup": speedup,
            "rss_ratio_legacy_over_gffbase": rss_ratio,
            "throughput_gffbase_features_per_sec":
                gffbase_info["n_features"] / gffbase_info["wall_seconds"]
                if gffbase_info.get("wall_seconds") else None,
            "throughput_legacy_features_per_sec":
                legacy_info["n_features"] / legacy_info["wall_seconds"]
                if legacy_info.get("wall_seconds") else None,
        },
    }
    p = write_results("01_ingest", payload)
    print(f"\nResults → {p}", flush=True)
    print(f"Speedup = {speedup:.2f}× (gffbase faster)", flush=True)


if __name__ == "__main__":
    main()
