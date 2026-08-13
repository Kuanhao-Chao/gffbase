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
"""Shared benchmarking utilities.

* `run_subprocess` — Popen + RSS sampler, returns merged dict.
* `du(path)` — recursive on-disk size.
* `pretty_*` formatters.
* Phase-6 cache loader so we don't re-run the 60-min legacy ingest.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path

import psutil

ROOT = Path(__file__).resolve().parent.parent
BENCH_DIR = ROOT / "benchmarks"
DATA = BENCH_DIR / "data"
OUT = BENCH_DIR / "out"

# Default DB locations.
GENCODE_GTF = DATA / "gencode.v45.basic.annotation.gtf.gz"
GFFBASE_DB = OUT / "gencode.duckdb"
LEGACY_DB = OUT / "gencode_legacy.sqlite"

# Phase-6 cache (so a 60-min legacy ingest doesn't have to be re-run).
PHASE6_BENCH_DIR = ROOT / "bench" / "out"
PHASE6_LEGACY_LOG = PHASE6_BENCH_DIR / "legacy_full.log"
PHASE6_LEGACY_RSS = PHASE6_BENCH_DIR / "legacy_rss.log"
PHASE6_GFFBASE_DB = PHASE6_BENCH_DIR / "gencode.duckdb"
PHASE6_LEGACY_DB = PHASE6_BENCH_DIR / "gencode_legacy.sqlite"


# ---------------------------------------------------------------------------
# Process / disk measurement
# ---------------------------------------------------------------------------


def run_subprocess(
    script: str,
    *,
    label: str,
    timeout: int | None = None,
    env_extra: dict[str, str] | None = None,
) -> dict:
    """Run a Python -c snippet in a fresh subprocess. Polls RSS at 50ms.
    Discards stderr (DuckDB progress bars) but captures stdout's last
    JSON line."""
    env = os.environ.copy()
    env["DUCKDB_DISABLE_PROGRESS_BAR"] = "1"
    env["PYTHONUNBUFFERED"] = "1"
    if env_extra:
        env.update(env_extra)
    proc = subprocess.Popen(
        [sys.executable, "-u", "-c", script],
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        env=env,
    )
    p = psutil.Process(proc.pid)
    peak = 0
    deadline = time.time() + timeout if timeout else None
    timed_out = False
    try:
        while proc.poll() is None:
            try:
                rss = p.memory_info().rss
                for c in p.children(recursive=True):
                    try:
                        rss += c.memory_info().rss
                    except psutil.Error:
                        pass
                if rss > peak:
                    peak = rss
            except psutil.Error:
                pass
            if deadline and time.time() > deadline:
                proc.kill()
                timed_out = True
                break
            time.sleep(0.05)
    finally:
        try:
            out, _ = proc.communicate(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
            out, _ = proc.communicate()

    text = out.decode("utf-8", errors="replace").strip()
    info: dict = {
        "label": label,
        "peak_rss_bytes": peak,
        "peak_rss_mb": peak / (1024 * 1024),
        "exit_code": proc.returncode,
        "timed_out": timed_out,
    }
    last = text.splitlines()[-1] if text else ""
    try:
        info.update(json.loads(last))
    except (ValueError, json.JSONDecodeError):
        info["raw_stdout_tail"] = text[-1000:]
    return info


def du(path: Path) -> int:
    """Total size in bytes — handles both files and directories."""
    p = Path(path)
    if not p.exists():
        return 0
    if p.is_file():
        return p.stat().st_size
    total = 0
    for f in p.rglob("*"):
        try:
            if f.is_file():
                total += f.stat().st_size
        except OSError:
            pass
    return total


# ---------------------------------------------------------------------------
# Formatters
# ---------------------------------------------------------------------------


def pretty_seconds(s: float) -> str:
    if s < 1:
        return f"{s * 1000:.1f} ms"
    if s < 60:
        return f"{s:.2f} s"
    m, s = divmod(s, 60)
    return f"{int(m)} min {s:.1f} s"


def pretty_bytes(n: float) -> str:
    if n < 1024:
        return f"{n:.0f} B"
    for unit in ("KB", "MB", "GB"):
        n /= 1024
        if n < 1024:
            return f"{n:.2f} {unit}"
    return f"{n:.2f} TB"


def pretty_qps(qps: float) -> str:
    return f"{qps:.0f} qps" if qps >= 1 else f"{qps:.3f} qps"


# ---------------------------------------------------------------------------
# Phase-6 cache loader
# ---------------------------------------------------------------------------


def load_phase6_legacy_ingest_numbers() -> dict | None:
    """Return the legacy gffutils ingest wall+RSS captured in Phase 6, or None
    if the cache files aren't present."""
    if not PHASE6_LEGACY_LOG.exists() or not PHASE6_LEGACY_RSS.exists():
        return None
    try:
        text = PHASE6_LEGACY_LOG.read_text(errors="replace")
        # Last JSON line in the log is the one we wrote: {"wall_seconds": ..., "n_features": ...}
        match = re.search(r'\{"wall_seconds":\s*([\d.]+).*?"n_features":\s*(\d+)\}', text)
        if not match:
            return None
        wall = float(match.group(1))
        n_features = int(match.group(2))
        rss_text = PHASE6_LEGACY_RSS.read_text()
        rss_match = re.search(r'"peak_rss_bytes":\s*(\d+)', rss_text)
        peak_rss = int(rss_match.group(1)) if rss_match else 0
        return {
            "label": "legacy gffutils create_db (Phase 6 cache)",
            "wall_seconds": wall,
            "n_features": n_features,
            "peak_rss_bytes": peak_rss,
            "peak_rss_mb": peak_rss / (1024 * 1024),
            "exit_code": 0,
            "cached": True,
            "source": str(PHASE6_LEGACY_LOG),
        }
    except (OSError, ValueError):
        return None


def write_results(stage: str, payload: dict) -> Path:
    """Write a stage's results to benchmarks/out/<stage>.json."""
    OUT.mkdir(parents=True, exist_ok=True)
    p = OUT / f"{stage}.json"
    p.write_text(json.dumps(payload, indent=2, default=str))
    return p
