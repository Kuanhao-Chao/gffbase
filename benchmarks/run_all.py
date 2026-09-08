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
"""Run detailed queries against databases retained by ``06_mega.py``.

Usage:
    python benchmarks/run_all.py --n-spatial 5000 --n-relational 500 --threads 10

Ingestion intentionally is not repeated here. Run ``06_mega.py --only
gencode-gff3 --keep-db gencode-gff3`` first, then this script measures stages
02-05 against those exact databases.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from benchmarks.common import OUT

STAGES = [
    ("02_spatial", "02_spatial.py"),
    ("03_relational", "03_relational.py"),
    ("04_disk", "04_disk.py"),
    ("05_vectorized", "05_vectorized.py"),
]


def run_stage(script_relpath: str, extra_args: list[str]) -> int:
    cmd = [sys.executable, str(ROOT / "benchmarks" / script_relpath)] + extra_args
    print(f"\n=== {script_relpath} {' '.join(extra_args)} ===", flush=True)
    return subprocess.call(cmd)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--n-spatial", type=int, default=5000)
    ap.add_argument("--n-relational", type=int, default=500)
    ap.add_argument("--vectorized-scales", default="500,5000,50000")
    ap.add_argument("--vectorized-timeout", type=int, default=1800)
    ap.add_argument("--threads", type=int, default=1)
    args = ap.parse_args()
    if args.threads < 1:
        ap.error("--threads must be >= 1")

    rc = run_stage(
        "02_spatial.py",
        ["--n-queries", str(args.n_spatial), "--threads", str(args.threads)],
    )
    if rc != 0:
        sys.exit(f"02_spatial.py exited {rc}")

    rc = run_stage(
        "03_relational.py",
        ["--n-genes", str(args.n_relational), "--threads", str(args.threads)],
    )
    if rc != 0:
        sys.exit(f"03_relational.py exited {rc}")

    rc = run_stage("04_disk.py", ["--threads", str(args.threads)])
    if rc != 0:
        sys.exit(f"04_disk.py exited {rc}")

    rc = run_stage(
        "05_vectorized.py",
        [
            "--scales",
            args.vectorized_scales,
            "--timeout-per-run",
            str(args.vectorized_timeout),
            "--threads",
            str(args.threads),
        ],
    )
    if rc != 0:
        sys.exit(f"05_vectorized.py exited {rc}")

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
