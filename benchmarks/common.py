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

* `environment()` — full provenance for a run: hardware, OS, every version,
  git commit. Stamped into every results file.
* `run_subprocess` — Popen + RSS sampler, returns merged dict.
* `merge_results` — update a results file BY KEY, preserving the rest.
* `repeat` — run a measurement N times, report median and spread.
* `purge_db` / `require_free_disk` — the multi-GB corpora do not all fit.
* `du(path)` — recursive on-disk size.
* `pretty_*` formatters.

Nothing here loads a cached measurement. A previous version of this module
scraped timings out of a sibling `bench/out/` directory and presented them as
current results, which is how the published table came to mix numbers from two
different GENCODE releases.
"""

from __future__ import annotations

import json
import os
import platform
import shutil
import statistics
import subprocess
import sys
import time
from pathlib import Path

# `psutil` is imported lazily, inside the two functions that measure with it.
# At module scope it made `06_mega.py --help` -- and every import of this
# module -- fail without the `bench` extra, which took out 15 of 18 CI jobs.
# A `--help` that dies on a missing measurement library is a bug of its own,
# independent of the tests that caught it.

ROOT = Path(__file__).resolve().parent.parent
BENCH_DIR = ROOT / "benchmarks"
DATA = BENCH_DIR / "data"
# Overridable so a run can target an external volume. A full sweep needs
# ~14 GiB transient, which is more than a full laptop disk has spare.
OUT = Path(os.environ.get("GFFBASE_BENCH_OUT") or (BENCH_DIR / "out"))
#: Committed results. `out/` is scratch and gitignored; this is the record.
RESULTS = BENCH_DIR / "results"

#: The corpus stages 01-05 measure. GFF3 rather than GTF because legacy
#: gffutils has to FINISH for those comparisons to mean anything, and legacy
#: GTF ingest on v49 does not finish inside any reasonable cap. v49 rather
#: than v45 because v45 is what `06_mega.py` stopped using, and having the two
#: harnesses read different releases is what let a v45 spatial number be
#: published in a row labelled v49.
GENCODE_GFF3 = DATA / "gencode.v49.chr_patch_hapl_scaff.basic.annotation.gff3.gz"
GENCODE_GTF = DATA / "gencode.v49.chr_patch_hapl_scaff.basic.annotation.gtf.gz"
GFFBASE_DB = OUT / "gencode-gff3.duckdb"
LEGACY_DB = OUT / "gencode-gff3_legacy.sqlite"

#: Results schema. Bump when the shape changes so a stale file is detectable
#: rather than silently misread.
SCHEMA_VERSION = "2"


# ---------------------------------------------------------------------------
# Provenance
# ---------------------------------------------------------------------------


def _cmd(*args: str) -> str | None:
    try:
        out = subprocess.run(args, capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.SubprocessError):
        return None
    return out.stdout.strip() or None if out.returncode == 0 else None


def _pkg_version(name: str) -> str | None:
    try:
        import importlib.metadata as md

        return md.version(name)
    except Exception:
        return None


def _cpu_model() -> str | None:
    if sys.platform == "darwin":
        return _cmd("sysctl", "-n", "machdep.cpu.brand_string")
    try:
        for line in Path("/proc/cpuinfo").read_text().splitlines():
            if line.startswith("model name"):
                return line.split(":", 1)[1].strip()
    except OSError:
        pass
    return platform.processor() or None


def environment() -> dict:
    """Everything needed to reproduce or fairly compare a run.

    None of this was recorded before: the published numbers carried their
    hardware and versions only as hand-typed prose in a markdown file, which
    said "gffbase 0.1.0" for the entire 0.2.0 development cycle. A benchmark
    result that does not say what it ran on is not a measurement, and one that
    does not say what version it measured cannot detect a regression.
    """
    import psutil

    free = shutil.disk_usage(str(OUT if OUT.exists() else BENCH_DIR)).free
    return {
        "timestamp_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "git_commit": _cmd("git", "-C", str(ROOT), "rev-parse", "HEAD"),
        "git_dirty": bool(_cmd("git", "-C", str(ROOT), "status", "--porcelain")),
        "hostname": platform.node(),
        "platform": platform.platform(),
        "machine": platform.machine(),
        "cpu_model": _cpu_model(),
        "cpu_cores_physical": psutil.cpu_count(logical=False),
        "cpu_cores_logical": psutil.cpu_count(logical=True),
        "total_ram_bytes": psutil.virtual_memory().total,
        "free_disk_bytes": free,
        "python_version": platform.python_version(),
        "rustc_version": _cmd("rustc", "--version"),
        "packages": {
            name: _pkg_version(name)
            for name in ("gffbase", "duckdb", "pyarrow", "pandas", "polars", "gffutils", "psutil")
        },
        "env": {
            k: os.environ[k]
            for k in ("GFFBASE_THREADS", "GFFBASE_TEST_DISABLE_RTREE", "GFFBASE_BENCH_OUT")
            if k in os.environ
        },
    }


# ---------------------------------------------------------------------------
# Disk management
# ---------------------------------------------------------------------------


def require_free_disk(gib: float, *, what: str = "this stage") -> None:
    """Abort before starting work that cannot finish.

    A corpus pair is up to 13 GiB and a partial run leaves it behind, so
    running out of space mid-sweep costs the hours already spent AND blocks
    the retry. Checking first is cheap.
    """
    free = shutil.disk_usage(str(OUT if OUT.exists() else BENCH_DIR)).free
    need = int(gib * (1 << 30))
    if free < need:
        raise SystemExit(
            f"refusing to start {what}: needs ~{gib:.1f} GiB free, "
            f"{free / (1 << 30):.1f} GiB available in {OUT}. "
            "Free space, or set GFFBASE_BENCH_OUT to a larger volume."
        )


def purge_db(*paths: Path) -> int:
    """Delete databases and their sidecars. Returns bytes reclaimed.

    The sidecars matter: DuckDB leaves `.wal`, SQLite leaves `-journal` and
    `-wal`, and unlinking only the bare filename leaks them. A sweep that
    leaks 13 GiB per corpus does not reach the end.
    """
    freed = 0
    for path in paths:
        for candidate in (
            path,
            Path(str(path) + ".wal"),
            Path(str(path) + ".tmp"),
            Path(str(path) + "-journal"),
            Path(str(path) + "-wal"),
            Path(str(path) + "-shm"),
        ):
            try:
                if candidate.is_file():
                    freed += candidate.stat().st_size
                    candidate.unlink()
            except OSError:
                pass
    return freed


# ---------------------------------------------------------------------------
# Repeats
# ---------------------------------------------------------------------------


def repeat(fn, n: int, *, warmup: int = 0) -> dict:
    """Run `fn` n times and summarize.

    For `n == 1` the result deliberately has NO `median` key, so a downstream
    renderer physically cannot print a median for a single sample. Every
    number published before this existed was n=1 presented to three
    significant figures under a claim of being "deterministic to ~10 %".
    """
    for _ in range(warmup):
        fn()
    values = [fn() for _ in range(n)]
    if n == 1:
        return {"value": values[0], "n": 1}
    return {
        "median": statistics.median(values),
        "min": min(values),
        "max": max(values),
        "values": values,
        "n": n,
    }


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
    import psutil

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
# Results
# ---------------------------------------------------------------------------


def write_results(stage: str, payload: dict) -> Path:
    """Write a stage's results, replacing the file. Stamps provenance."""
    OUT.mkdir(parents=True, exist_ok=True)
    body = {"schema_version": SCHEMA_VERSION, "environment": environment(), **payload}
    path = OUT / f"{stage}.json"
    path.write_text(json.dumps(body, indent=2, default=str))
    return path


def merge_results(stage: str, section: str, entries: dict) -> Path:
    """Update `entries` inside `<stage>.json[section]`, preserving the rest.

    This is the fix for the defect that destroyed the published record. The
    mega benchmark used to build a payload containing only the corpora
    selected by `--only` and write the WHOLE file, so each targeted re-run
    silently deleted every other corpus's results. `06_mega.json` ended up
    holding one of five corpora while the performance document named it as the
    provenance for all five, and four rows had no surviving measurement at all.

    Merging by key means a `--only mane` run updates MANE and touches nothing
    else, which is what anyone would assume it already did.
    """
    OUT.mkdir(parents=True, exist_ok=True)
    path = OUT / f"{stage}.json"
    existing: dict = {}
    if path.is_file():
        try:
            existing = json.loads(path.read_text())
        except (OSError, ValueError):
            existing = {}
    if existing.get("schema_version") not in (None, SCHEMA_VERSION):
        # Different shape: start clean rather than blend two schemas.
        existing = {}
    merged = dict(existing.get(section) or {})
    merged.update(entries)
    body = {
        "schema_version": SCHEMA_VERSION,
        "environment": environment(),
        **{
            k: v for k, v in existing.items() if k not in {"schema_version", "environment", section}
        },
        section: merged,
    }
    path.write_text(json.dumps(body, indent=2, default=str))
    return path
