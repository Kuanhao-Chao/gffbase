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
"""Head-to-head ingest: gffbase vs legacy gffutils on GENCODE v49 GFF3.

Each engine runs in a fresh subprocess so peak RSS measurements are clean.

There is no `--reuse-cached` any more. It copied a database from the retired
`bench/` tree and reported hardcoded literals (`wall_seconds: 225.78`,
`n_features: 2_182_889`) captured from a log, labelled `"cached": true`, and
those values reached the published tables as though they had been measured on
the release they appeared in. If it did not run, it does not get published.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from benchmarks.common import (
    GENCODE_GFF3,
    GFFBASE_DB,
    LEGACY_DB,
    OUT,
    benchmark_env,
    du,
    pretty_bytes,
    pretty_seconds,
    run_subprocess,
    write_results,
)


def ingest_gffbase(threads: int = 1) -> dict:
    if GFFBASE_DB.exists():
        GFFBASE_DB.unlink()
    script = f"""
import json, time
from gffbase import create_db
t0 = time.perf_counter()
db = create_db(
    {str(GENCODE_GFF3)!r}, {str(GFFBASE_DB)!r}, force=True,
    merge_strategy="create_unique", pragmas={{"threads": {threads}}},
)
elapsed = time.perf_counter() - t0
print(json.dumps({{
    "wall_seconds": elapsed,
    "n_features": db.count_features_of_type(),
    "rtree_built": db._rtree_built,
    "fmt": db.fmt,
}}))
"""
    return run_subprocess(
        script,
        label="gffbase create_db",
        timeout=1200,
        env_extra=benchmark_env(threads),
    )


def ingest_legacy(timeout: int = 7200, *, threads: int = 1) -> dict:
    if LEGACY_DB.exists():
        LEGACY_DB.unlink()
    script = f"""
import json, time, gffutils
t0 = time.perf_counter()
db = gffutils.create_db(
    {str(GENCODE_GFF3)!r}, {str(LEGACY_DB)!r}, force=True,
    keep_order=False, sort_attribute_values=False,
    merge_strategy="create_unique", verbose=False,
    disable_infer_genes=False, disable_infer_transcripts=False,
)
elapsed = time.perf_counter() - t0
print(json.dumps({{"wall_seconds": elapsed, "n_features": db.count_features_of_type()}}))
"""
    return run_subprocess(
        script,
        label="legacy gffutils create_db",
        timeout=timeout,
        env_extra=benchmark_env(threads),
    )


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--legacy-timeout", type=int, default=7200)
    ap.add_argument("--threads", type=int, default=1)
    args = ap.parse_args()
    if args.threads < 1:
        ap.error("--threads must be >= 1")

    OUT.mkdir(parents=True, exist_ok=True)
    print("[ingest] gffbase ingest (subprocess)…", flush=True)
    gffbase_info = ingest_gffbase(args.threads)
    gffbase_info["disk_bytes"] = du(GFFBASE_DB)
    print(
        f"  wall={pretty_seconds(gffbase_info['wall_seconds'])}, "
        f"RSS={pretty_bytes(gffbase_info['peak_rss_bytes'])}, "
        f"disk={pretty_bytes(gffbase_info['disk_bytes'])}",
        flush=True,
    )

    print("[ingest] legacy gffutils ingest (subprocess)…", flush=True)
    legacy_info = ingest_legacy(args.legacy_timeout, threads=args.threads)
    legacy_info["disk_bytes"] = du(LEGACY_DB)
    if legacy_info.get("wall_seconds"):
        print(
            f"  wall={pretty_seconds(legacy_info['wall_seconds'])}, "
            f"RSS={pretty_bytes(legacy_info['peak_rss_bytes'])}, "
            f"disk={pretty_bytes(legacy_info['disk_bytes'])}",
            flush=True,
        )

    speedup = (legacy_info.get("wall_seconds") or 0) / (gffbase_info["wall_seconds"] or 1)
    rss_ratio = legacy_info["peak_rss_bytes"] / max(gffbase_info["peak_rss_bytes"], 1)

    payload = {
        "input": {
            "path": str(GENCODE_GFF3),
            "compressed_bytes": GENCODE_GFF3.stat().st_size if GENCODE_GFF3.exists() else 0,
        },
        "gffbase": gffbase_info,
        "legacy": legacy_info,
        "comparison": {
            "wall_speedup": speedup,
            "rss_ratio_legacy_over_gffbase": rss_ratio,
            "throughput_gffbase_features_per_sec": gffbase_info["n_features"]
            / gffbase_info["wall_seconds"]
            if gffbase_info.get("wall_seconds")
            else None,
            "throughput_legacy_features_per_sec": legacy_info["n_features"]
            / legacy_info["wall_seconds"]
            if legacy_info.get("wall_seconds")
            else None,
        },
    }
    p = write_results("01_ingest", payload)
    print(f"\nResults → {p}", flush=True)
    print(f"Speedup = {speedup:.2f}× (gffbase faster)", flush=True)


if __name__ == "__main__":
    main()
