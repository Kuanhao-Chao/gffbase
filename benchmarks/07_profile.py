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
"""Where ingest and query time go -- diagnostics, not headline numbers.

    # ingest: stages, wall, peak RSS and file size per (threads, batch) point
    python benchmarks/07_profile.py ingest --corpus mane --threads 1 8 16 128
    python benchmarks/07_profile.py ingest --corpus mane --batch 10000 50000 200000

    # queries: per-call latency per thread count, plus EXPLAIN ANALYZE of the
    # SQL each method actually issues
    python benchmarks/07_profile.py queries --db /path/mane.duckdb --threads 1 8 128

Each ingest point runs in a fresh subprocess, so its peak RSS is its own.
Results go to `benchmarks/out/07_profile_<kind>.json`. These feed the 0.3.0
design decisions (ingest rebuild, default thread count); the published tables
still come from `06_mega.py` only.
"""

from __future__ import annotations

import argparse
import json
import statistics
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from benchmarks.common import OUT, benchmark_env, du, run_subprocess  # noqa: E402
from benchmarks.corpora import BY_KEY, corpus_path  # noqa: E402

#: create_db options per corpus: the compat reading of a repeated id where the
#: corpus has split CDS lines (see tests/test_corpus.py).
_OPTIONS = {"merge_strategy": "create_unique"}


def _ingest_point(src: Path, dbfn: Path, threads: int, batch: int | None, timeout: int) -> dict:
    batch_kw = "" if batch is None else f", batch_size={batch}"
    script = f"""
import json, os, time
from gffbase._options import IngestOptions
from gffbase.ingest import from_file
t0 = time.perf_counter()
con, st = from_file({str(src)!r}, {str(dbfn)!r},
                    options=IngestOptions(force=True, **{_OPTIONS!r}){batch_kw})
wall = time.perf_counter() - t0
n = con.execute("SELECT count(*) FROM features").fetchone()[0]
con.close()
print(json.dumps({{"wall_seconds": wall, "stages": st.stages, "n_features": n}}))
"""
    info = run_subprocess(
        script,
        label=f"threads={threads} batch={batch or 'default'}",
        timeout=timeout,
        env_extra={**benchmark_env(threads), "PYTHONPATH": str(ROOT / "python")},
    )
    info.update({"threads": threads, "batch_size": batch, "db_bytes": du(dbfn)})
    return info


def cmd_ingest(args) -> dict:
    corpus = BY_KEY[args.corpus]
    src = corpus_path(corpus)
    if not src.is_file():
        raise SystemExit(f"{src} not downloaded; see benchmarks/download_corpora.py")
    work = Path(args.workdir)
    work.mkdir(parents=True, exist_ok=True)
    points = []
    for threads in args.threads:
        for batch in args.batch:
            for rep in range(args.repeat):
                dbfn = work / f"{args.corpus}.t{threads}.b{batch or 'default'}.duckdb"
                info = _ingest_point(src, dbfn, threads, batch, args.timeout)
                info["repeat"] = rep
                points.append(info)
                stages = info.get("stages") or {}
                top = sorted(stages.items(), key=lambda kv: -kv[1])[:4]
                wall = info.get("wall_seconds")
                print(
                    f"threads={threads:>3} batch={batch or 'default':>7} rep={rep}: "
                    f"{'-' if wall is None else f'{wall:.1f} s'}, "
                    f"peak {info['peak_rss_bytes'] / 2**30:.2f} GiB, "
                    f"file {info['db_bytes'] / 2**30:.2f} GiB; "
                    + ", ".join(f"{k} {v:.1f}" for k, v in top),
                    flush=True,
                )
                if not args.keep:
                    dbfn.unlink(missing_ok=True)
    return {"corpus": args.corpus, "points": points}


# ---------------------------------------------------------------------------
# Queries
# ---------------------------------------------------------------------------


def _shapes(db, genes, transcripts, leaves):
    """Name -> (callable taking an index). Every shape a loop issues."""
    seqid_spans = [(g.seqid, g.start, min(g.start + 10_000, g.end + 10_000)) for g in genes]
    return {
        "getitem": lambda i: db[genes[i].id],
        "children_level1": lambda i: list(db.children(genes[i].id, level=1)),
        "children_all": lambda i: list(db.children(genes[i].id)),
        "children_exon": lambda i: list(db.children(transcripts[i].id, featuretype="exon")),
        "parents_level1": lambda i: list(db.parents(leaves[i].id, level=1)),
        "parents_gene": lambda i: list(db.parents(leaves[i].id, featuretype="gene")),
        "region_10kb": lambda i: list(db.region(seqid_spans[i])),
    }


def _sample(db, n: int):
    """A fixed, spread-out sample: every k-th gene in file order."""
    genes = list(db.features_of_type("gene"))
    step = max(1, len(genes) // n)
    genes = genes[::step][:n]
    transcripts, leaves = [], []
    for g in genes:
        kids = list(db.children(g.id, level=1))
        transcripts.append(kids[0] if kids else g)
        grand = list(db.children(transcripts[-1].id, level=1)) if kids else []
        leaves.append(grand[0] if grand else transcripts[-1])
    return genes, transcripts, leaves


def _captured_sql(db, shapes):
    """The (sql, params) each shape streams through `_yield_features`."""
    captured: dict[str, list] = {}
    original = db._yield_features

    def spy(sql, params):
        captured.setdefault(current[0], []).append((sql, list(params)))
        return original(sql, params)

    current = [""]
    db._yield_features = spy
    try:
        for name, call in shapes.items():
            current[0] = name
            call(0)
    finally:
        del db._yield_features
    return captured


def _explain(db, sql: str, params: list) -> str:
    rows = db.conn.execute("EXPLAIN ANALYZE " + sql, params).fetchall()
    return "\n".join(str(r[-1]) for r in rows)


def cmd_queries(args) -> dict:
    import gffbase

    out: dict = {"db": str(args.db), "by_threads": {}}
    for threads in args.threads:
        db = gffbase.FeatureDB(str(args.db), read_only=True)
        db.conn.execute(f"SET threads = {int(threads)}")
        genes, transcripts, leaves = _sample(db, args.n)
        shapes = _shapes(db, genes, transcripts, leaves)
        n = len(genes)
        per_shape = {}
        for name, call in shapes.items():
            for i in range(min(20, n)):  # warm the cursors and caches
                call(i)
            times = []
            for i in range(n):
                t0 = time.perf_counter()
                call(i)
                times.append((time.perf_counter() - t0) * 1000)
            per_shape[name] = {
                "median_ms": statistics.median(times),
                "p90_ms": sorted(times)[int(0.9 * (len(times) - 1))],
                "calls": n,
            }
            print(
                f"threads={threads:>3} {name:>16}: median {per_shape[name]['median_ms']:.3f} ms, "
                f"p90 {per_shape[name]['p90_ms']:.3f} ms",
                flush=True,
            )
        entry: dict = {"shapes": per_shape}
        if args.explain:
            plans = {}
            for name, calls in _captured_sql(db, shapes).items():
                plans[name] = [{"sql": s, "plan": _explain(db, s, p)} for s, p in calls]
            entry["plans"] = plans
        out["by_threads"][str(threads)] = entry
        db.close()
    return out


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    sub = ap.add_subparsers(dest="kind", required=True)

    ing = sub.add_parser("ingest")
    ing.add_argument("--corpus", default="mane", choices=sorted(BY_KEY))
    ing.add_argument("--threads", type=int, nargs="+", default=[16])
    ing.add_argument(
        "--batch", type=int, nargs="+", default=[None], help="default: ingest's own batch size"
    )
    ing.add_argument("--repeat", type=int, default=1)
    ing.add_argument("--timeout", type=int, default=3600)
    ing.add_argument("--workdir", default=str(OUT / "profile"))
    ing.add_argument("--keep", action="store_true", help="keep each database file")

    q = sub.add_parser("queries")
    q.add_argument("--db", required=True)
    q.add_argument("--threads", type=int, nargs="+", default=[1, 8, 128])
    q.add_argument("--n", type=int, default=1000, help="genes sampled per shape")
    q.add_argument("--explain", action="store_true", help="EXPLAIN ANALYZE each shape's SQL")

    args = ap.parse_args()
    result = cmd_ingest(args) if args.kind == "ingest" else cmd_queries(args)
    OUT.mkdir(parents=True, exist_ok=True)
    path = OUT / f"07_profile_{args.kind}.json"
    path.write_text(json.dumps(result, indent=1, default=str))
    print(f"wrote {path}")


if __name__ == "__main__":
    main()
