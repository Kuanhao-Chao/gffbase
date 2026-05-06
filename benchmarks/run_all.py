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
"""Orchestrator — runs all four benchmarks in order and aggregates their
JSON outputs into `benchmarks/out/results.json`.

Usage:
    python benchmarks/run_all.py --reuse-cached
    python benchmarks/run_all.py --n-spatial 5000 --n-relational 500
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "python"))

from benchmarks.common import OUT


STAGES = [
    ("01_ingest",     "01_ingest.py"),
    ("02_spatial",    "02_spatial.py"),
    ("03_relational", "03_relational.py"),
    ("04_disk",       "04_disk.py"),
]


def run_stage(script_relpath: str, extra_args: list[str]) -> int:
    cmd = [sys.executable, str(ROOT / "benchmarks" / script_relpath)] + extra_args
    print(f"\n=== {script_relpath} {' '.join(extra_args)} ===", flush=True)
    return subprocess.call(cmd)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--reuse-cached", action="store_true",
                    help="Reuse Phase 6 cached legacy ingest numbers + DBs")
    ap.add_argument("--n-spatial", type=int, default=5000)
    ap.add_argument("--n-relational", type=int, default=500)
    ap.add_argument("--legacy-timeout", type=int, default=7200)
    args = ap.parse_args()

    rc = run_stage("01_ingest.py",
                   (["--reuse-cached"] if args.reuse_cached else [])
                   + ["--legacy-timeout", str(args.legacy_timeout)])
    if rc != 0:
        sys.exit(f"01_ingest.py exited {rc}")

    rc = run_stage("02_spatial.py", ["--n-queries", str(args.n_spatial)])
    if rc != 0:
        sys.exit(f"02_spatial.py exited {rc}")

    rc = run_stage("03_relational.py", ["--n-genes", str(args.n_relational)])
    if rc != 0:
        sys.exit(f"03_relational.py exited {rc}")

    rc = run_stage("04_disk.py", [])
    if rc != 0:
        sys.exit(f"04_disk.py exited {rc}")

    # Aggregate.
    aggregated = {}
    for stage, _ in STAGES:
        f = OUT / f"{stage}.json"
        if f.exists():
            aggregated[stage] = json.loads(f.read_text())
    out = OUT / "results.json"
    out.write_text(json.dumps(aggregated, indent=2, default=str))
    print(f"\nAggregated → {out}", flush=True)


if __name__ == "__main__":
    main()
