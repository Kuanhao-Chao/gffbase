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
"""Phase 6 — GENCODE benchmark + smart-routing ablation.

Four experiments, in order:

    1. Ingestion — gffbase (in-process) + legacy gffutils (subprocess).
       Reports wall time and peak RSS.

    2. Spatial routing ablation — random region() overlap queries with the
       R-tree path vs. the multi-column B-tree fallback.

    3. Relational routing ablation — children() queries served by the
       materialized closure cache vs. the dynamic recursive CTE.

    4. Differential correctness — gffbase ingest vs. legacy gffutils on a
       sliced subset; compare the (seqid, featuretype, start, end, strand)
       multiset of authored rows.

Run:

    cd gffbase
    python bench/benchmark.py
"""

from __future__ import annotations

import argparse
import gc
import gzip
import json
import os
import random
import subprocess
import sys
import tempfile
import threading
import time
from contextlib import contextmanager
from pathlib import Path

import psutil

ROOT = Path(__file__).resolve().parent.parent
BENCH = ROOT / "bench"
DATA = BENCH / "data"
OUT = BENCH / "out"
DEFAULT_INPUT = DATA / "gencode.v45.basic.annotation.gtf.gz"

sys.path.insert(0, str(ROOT / "python"))


# ---------------------------------------------------------------------------
# Memory + timing helpers
# ---------------------------------------------------------------------------


@contextmanager
def measure(label: str):
    """Wall time + peak RSS of the enclosed block.

    A daemon thread polls psutil at 50 ms intervals so we capture transient
    DuckDB peaks during index build.
    """
    proc = psutil.Process()
    result: dict = {"label": label}
    stop = threading.Event()
    peaks = {"rss_bytes": proc.memory_info().rss}

    def poll():
        while not stop.is_set():
            try:
                rss = proc.memory_info().rss
                if rss > peaks["rss_bytes"]:
                    peaks["rss_bytes"] = rss
            except psutil.Error:
                pass
            stop.wait(0.05)

    t = threading.Thread(target=poll, daemon=True)
    t.start()
    t0 = time.perf_counter()
    try:
        yield result
    finally:
        elapsed = time.perf_counter() - t0
        stop.set()
        t.join(timeout=1)
        result["wall_seconds"] = elapsed
        result["peak_rss_bytes"] = peaks["rss_bytes"]
        result["peak_rss_mb"] = peaks["rss_bytes"] / (1024 * 1024)


def fmt_bytes(n: float) -> str:
    if n < 1024:
        return f"{n:.0f} B"
    for unit in ("KB", "MB", "GB"):
        n /= 1024
        if n < 1024:
            return f"{n:.2f} {unit}"
    return f"{n:.2f} TB"


# ---------------------------------------------------------------------------
# Subprocess RSS sampler. Polls /proc-equivalent via psutil and discards
# stdout/stderr quickly so DuckDB progress bars can't deadlock the pipe.
# ---------------------------------------------------------------------------


def run_subprocess(
    script: str,
    *,
    label: str,
    timeout: int | None = None,
    quiet_stderr: bool = True,
) -> dict:
    """Run a Python -c script and stream the final JSON line back."""
    proc = subprocess.Popen(
        [sys.executable, "-u", "-c", script],
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL if quiet_stderr else subprocess.PIPE,
        env={**os.environ, "DUCKDB_DISABLE_PROGRESS_BAR": "1"},
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
    # The script must print one final JSON object on its last line.
    last = text.splitlines()[-1] if text else ""
    try:
        payload = json.loads(last)
        info.update(payload)
    except (ValueError, json.JSONDecodeError):
        info["raw_stdout_tail"] = text[-1000:]
    return info


# ---------------------------------------------------------------------------
# Ingestion
# ---------------------------------------------------------------------------


def ingest_gffbase_inproc(gtf_path: Path, dbfn: Path, *, skip_if_exists: bool = False) -> dict:
    """In-process measurement: gives us reliable wall + peak RSS without
    subprocess boundary issues."""
    if dbfn.exists() and skip_if_exists:
        # Re-open existing DB and report counts without re-ingesting. Used
        # when iterating on routing experiments to avoid re-paying the
        # ingest cost. Wall is reported as 0 (we did NOT ingest).
        from gffbase import FeatureDB

        db = FeatureDB(str(dbfn))
        info = {
            "label": "gffbase (cached)",
            "wall_seconds": 0.0,
            "peak_rss_bytes": 0,
            "peak_rss_mb": 0.0,
            "n_features": db.count_features_of_type(),
            "rtree_built": db._rtree_built,
            "fmt": db.fmt,
            "max_depth": db._max_depth,
            "cached": True,
        }
        db.conn.close()
        return info
    if dbfn.exists():
        dbfn.unlink()
    from gffbase import create_db

    with measure("gffbase create_db") as m:
        # Suppress DuckDB's progress bar via PRAGMA, set on the connection
        # opened inside create_db.
        os.environ["DUCKDB_DISABLE_PROGRESS_BAR"] = "1"
        db = create_db(str(gtf_path), str(dbfn), force=True)
        n = db.count_features_of_type()
        rtree = db._rtree_built
        fmt = db.fmt
        max_depth = db._max_depth
        # Drop the connection so RSS stays clean for the next phase. We re-open
        # below for routing experiments.
        db.conn.close()
        del db
        gc.collect()
    m["n_features"] = n
    m["rtree_built"] = rtree
    m["fmt"] = fmt
    m["max_depth"] = max_depth
    return m


def ingest_legacy_subproc(gtf_path: Path, dbfn: Path, max_seconds: int) -> dict:
    """Subprocess: run legacy gffutils.create_db. Killed after `max_seconds`."""
    if dbfn.exists():
        dbfn.unlink()
    script = f"""
import json, time, gffutils
t0 = time.perf_counter()
db = gffutils.create_db(
    {str(gtf_path)!r}, dbfn={str(dbfn)!r}, force=True,
    keep_order=False, sort_attribute_values=False,
    merge_strategy="create_unique", verbose=False,
    disable_infer_genes=False, disable_infer_transcripts=False,
)
elapsed = time.perf_counter() - t0
print(json.dumps({{"wall_seconds": elapsed, "n_features": db.count_features_of_type()}}))
"""
    return run_subprocess(script, label="legacy gffutils create_db", timeout=max_seconds)


# ---------------------------------------------------------------------------
# Routing ablation
# ---------------------------------------------------------------------------


def sample_regions(con, n: int, seed: int = 20260501) -> list[tuple[str, int, int]]:
    rows = con.execute("""
        SELECT seqid, MIN(start) AS lo, MAX("end") AS hi
        FROM features
        GROUP BY seqid
        HAVING COUNT(*) > 50
    """).fetchall()
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


def bench_region(db, regions, *, force_btree: bool) -> dict:
    saved = db._rtree_built
    if force_btree:
        db._rtree_built = False
    total = 0
    t0 = time.perf_counter()
    for seqid, rstart, rend in regions:
        for _ in db.region(seqid=seqid, start=rstart, end=rend):
            total += 1
    elapsed = time.perf_counter() - t0
    db._rtree_built = saved
    return {
        "n_queries": len(regions),
        "wall_seconds": elapsed,
        "qps": len(regions) / elapsed if elapsed else float("inf"),
        "total_features_returned": total,
        "path": "btree_fallback" if force_btree else ("rtree" if saved else "btree"),
    }


def bench_children(db, ids, *, force_dynamic: bool, level=None) -> dict:
    """force_dynamic=True drops `_max_depth` to 0 so every traversal trips
    the overflow-detection branch and runs the dynamic recursive CTE."""
    saved_md = db._max_depth
    if force_dynamic:
        db._max_depth = 0
    total = 0
    t0 = time.perf_counter()
    for fid in ids:
        for _ in db.children(fid, level=level):
            total += 1
    elapsed = time.perf_counter() - t0
    db._max_depth = saved_md
    return {
        "n_queries": len(ids),
        "wall_seconds": elapsed,
        "qps": len(ids) / elapsed if elapsed else float("inf"),
        "total_descendants": total,
        "path": "dynamic_cte" if force_dynamic else "closure_cache",
    }


# ---------------------------------------------------------------------------
# Differential correctness
# ---------------------------------------------------------------------------


def slice_gtf(src: Path, dst: Path, n_lines: int) -> int:
    n = 0
    with gzip.open(src, "rt", encoding="utf-8") as fin, dst.open("w") as fout:
        for line in fin:
            if line.startswith("#"):
                fout.write(line)
                continue
            fout.write(line)
            n += 1
            if n >= n_lines:
                break
    return n


def diff_correctness(slice_path: Path, tmpdir: Path) -> dict:
    """Ingest the slice with both engines, write the row dump to a JSON file
    (avoids the parent's poll-without-drain deadlock on a 64KB pipe), then
    diff the two multisets.
    """
    legacy_db = tmpdir / "legacy.db"
    new_db = tmpdir / "new.duckdb"
    legacy_json = tmpdir / "legacy_rows.json"
    new_json = tmpdir / "new_rows.json"
    legacy_script = f"""
import json, gffutils
db = gffutils.create_db({str(slice_path)!r}, {str(legacy_db)!r},
    force=True, merge_strategy='create_unique', verbose=False,
    disable_infer_genes=True, disable_infer_transcripts=True)
# legacy returns sqlite3.Row; cast to tuples for JSON serialization.
rows = [tuple(r) for r in db.execute('SELECT seqid, featuretype, start, end, strand FROM features').fetchall()]
with open({str(legacy_json)!r}, 'w') as f:
    json.dump({{"n": len(rows), "rows": rows}}, f)
print(json.dumps({{"n": len(rows), "out": {str(legacy_json)!r}}}))
"""
    new_script = f"""
import json, sys
sys.path.insert(0, {str(ROOT / "python")!r})
from gffbase import create_db
db = create_db({str(slice_path)!r}, {str(new_db)!r}, force=True,
               disable_infer_genes=True, disable_infer_transcripts=True)
rows = [tuple(r) for r in db.execute('SELECT seqid, featuretype, start, "end", strand FROM features '
                   'WHERE is_synthetic = FALSE').fetchall()]
with open({str(new_json)!r}, 'w') as f:
    json.dump({{"n": len(rows), "rows": rows}}, f)
print(json.dumps({{"n": len(rows), "out": {str(new_json)!r}}}))
"""
    legacy = run_subprocess(legacy_script, label="diff-legacy", timeout=900)
    new = run_subprocess(new_script, label="diff-new", timeout=600)
    if legacy.get("exit_code") != 0:
        return {"error": "legacy ingest failed", "legacy": legacy}
    if new.get("exit_code") != 0:
        return {"error": "new ingest failed", "new": new}

    legacy_data = json.loads(legacy_json.read_text())
    new_data = json.loads(new_json.read_text())
    a = sorted(tuple(r) for r in legacy_data["rows"])
    b = sorted(tuple(r) for r in new_data["rows"])
    only_legacy = sorted(set(a) - set(b))
    only_new = sorted(set(b) - set(a))
    return {
        "legacy_n": legacy_data["n"],
        "new_n": new_data["n"],
        "match": a == b,
        "n_only_legacy": len(only_legacy),
        "n_only_new": len(only_new),
        "only_legacy_first_5": only_legacy[:5],
        "only_new_first_5": only_new[:5],
    }


# ---------------------------------------------------------------------------
# Driver
# ---------------------------------------------------------------------------


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--gtf", default=str(DEFAULT_INPUT))
    parser.add_argument("--n-region", type=int, default=10_000)
    parser.add_argument("--n-children", type=int, default=2_000)
    parser.add_argument("--diff-lines", type=int, default=50_000)
    parser.add_argument("--legacy-timeout", type=int, default=1800)
    parser.add_argument("--skip-legacy-full", action="store_true")
    parser.add_argument(
        "--reuse-db",
        action="store_true",
        help="reuse existing gffbase DuckDB on disk; skip re-ingest",
    )
    parser.add_argument("--out", default=str(OUT / "results.json"))
    args = parser.parse_args()

    OUT.mkdir(parents=True, exist_ok=True)
    gtf = Path(args.gtf)
    if not gtf.exists():
        print(f"ERROR: {gtf} does not exist", file=sys.stderr)
        sys.exit(2)

    results: dict = {
        "input": {
            "path": str(gtf),
            "compressed_bytes": gtf.stat().st_size,
        },
        "ingestion": {},
        "routing": {},
        "correctness": {},
    }

    # --------------------------------------------------------------
    # 1. Ingestion (gffbase)
    # --------------------------------------------------------------
    print("[1/4] gffbase ingest …", flush=True)
    new_dbfn = OUT / "gencode.duckdb"
    new_info = ingest_gffbase_inproc(gtf, new_dbfn, skip_if_exists=args.reuse_db)
    results["ingestion"]["gffbase"] = new_info
    print(
        f"    wall = {new_info['wall_seconds']:.2f}s, peak RSS = "
        f"{fmt_bytes(new_info['peak_rss_bytes'])}, n_features = {new_info['n_features']}",
        flush=True,
    )

    # --------------------------------------------------------------
    # 1b. Ingestion (legacy gffutils) — slow; subprocess + timeout
    # --------------------------------------------------------------
    if not args.skip_legacy_full:
        print(f"[1/4] legacy gffutils ingest (timeout {args.legacy_timeout}s) …", flush=True)
        legacy_dbfn = OUT / "gencode_legacy.sqlite"
        legacy_info = ingest_legacy_subproc(gtf, legacy_dbfn, args.legacy_timeout)
        results["ingestion"]["legacy"] = legacy_info
        if legacy_info.get("wall_seconds") and not legacy_info.get("timed_out"):
            ratio = legacy_info["wall_seconds"] / new_info["wall_seconds"]
            results["ingestion"]["speedup_ratio"] = ratio
            print(
                f"    legacy wall = {legacy_info['wall_seconds']:.2f}s, peak RSS = "
                f"{fmt_bytes(legacy_info['peak_rss_bytes'])}, speedup = {ratio:.2f}×",
                flush=True,
            )
        else:
            print(
                f"    legacy did not complete: timed_out={legacy_info.get('timed_out')}, "
                f"exit_code={legacy_info.get('exit_code')}",
                flush=True,
            )

    # --------------------------------------------------------------
    # Re-open the DuckDB for routing experiments.
    # --------------------------------------------------------------
    from gffbase import FeatureDB

    db = FeatureDB(str(new_dbfn))

    # --------------------------------------------------------------
    # 2. Spatial routing ablation
    # --------------------------------------------------------------
    print(f"[2/4] Spatial routing — {args.n_region} regions …", flush=True)
    regions = sample_regions(db.conn, args.n_region)
    rt = bench_region(db, regions, force_btree=False)
    bt = bench_region(db, regions, force_btree=True)
    rt_speedup = (bt["wall_seconds"] / rt["wall_seconds"]) if rt["wall_seconds"] else float("inf")
    results["routing"]["region"] = {
        "rtree": rt,
        "btree": bt,
        "rtree_speedup_vs_btree": rt_speedup,
    }
    print(
        f"    rtree={rt['wall_seconds']:.3f}s ({rt['qps']:.0f} qps), "
        f"btree={bt['wall_seconds']:.3f}s ({bt['qps']:.0f} qps), "
        f"speedup={rt_speedup:.2f}×",
        flush=True,
    )

    # --------------------------------------------------------------
    # 3. Relational routing ablation
    # --------------------------------------------------------------
    print(f"[3/4] Relational routing — {args.n_children} genes …", flush=True)
    gene_ids = [
        r[0]
        for r in db.conn.execute(
            "SELECT id FROM features WHERE featuretype = 'gene' ORDER BY id LIMIT ?",
            [args.n_children],
        ).fetchall()
    ]
    cache = bench_children(db, gene_ids, force_dynamic=False, level=None)
    dyn = bench_children(db, gene_ids, force_dynamic=True, level=None)
    cache_speedup = (
        (dyn["wall_seconds"] / cache["wall_seconds"]) if cache["wall_seconds"] else float("inf")
    )
    results["routing"]["children"] = {
        "closure_cache": cache,
        "dynamic_cte": dyn,
        "cache_speedup_vs_dynamic": cache_speedup,
    }
    print(
        f"    cache={cache['wall_seconds']:.3f}s "
        f"({cache['qps']:.0f} qps, {cache['total_descendants']} descs), "
        f"dyn={dyn['wall_seconds']:.3f}s "
        f"({dyn['qps']:.0f} qps, {dyn['total_descendants']} descs), "
        f"speedup={cache_speedup:.2f}×",
        flush=True,
    )

    db.conn.close()

    # --------------------------------------------------------------
    # 4. Differential correctness
    # --------------------------------------------------------------
    print(f"[4/4] Differential correctness on first {args.diff_lines} lines …", flush=True)
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        slice_path = tmp / "slice.gtf"
        n_written = slice_gtf(gtf, slice_path, args.diff_lines)
        diff = diff_correctness(slice_path, tmp)
    diff["n_input_lines"] = n_written
    results["correctness"] = diff
    print(
        f"    legacy={diff.get('legacy_n')}, new={diff.get('new_n')}, "
        f"match={diff.get('match')}, only_legacy={diff.get('n_only_legacy')}, "
        f"only_new={diff.get('n_only_new')}",
        flush=True,
    )

    out_path = Path(args.out)
    out_path.write_text(json.dumps(results, indent=2))
    print(f"\nResults written to {out_path}", flush=True)


if __name__ == "__main__":
    main()
