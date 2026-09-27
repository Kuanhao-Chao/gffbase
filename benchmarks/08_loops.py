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
"""The loops people actually write, timed in gffutils and in gffbase.

    python benchmarks/08_loops.py \\
        --engine gffutils=benchmarks/out/mane_legacy.sqlite \\
        --engine gffbase=benchmarks/out/mane.duckdb \\
        [--engine gffbase-0.2.1=/path/to/db@/path/to/other/python]

Each workload runs in a fresh subprocess per engine and reports ms per call
and a digest of every answer. A ratio is only printed where the digests
agree: a faster wrong answer is not a speedup.

Workloads -- the first two walk a live stream, which is where gffbase
prefetches; the last two are scattered calls, where it cannot:

- genes -> children(level=1)            for gene in features_of_type("gene")
- transcripts -> exons                  ... for t in children(gene, level=1):
                                              children(t, featuretype="exon")
- exons -> parents(featuretype="gene")  for exon in features_of_type("exon")
- random children(level=1)              a seeded sample of gene ids
- random db[id]                         a seeded sample of feature ids
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

CHILD = r"""
import hashlib, itertools, json, random, sys, time
lib, path, n = sys.argv[1], sys.argv[2], int(sys.argv[3])
if lib == "gffutils":
    import gffutils
    db = gffutils.FeatureDB(path)
else:
    import gffbase
    db = gffbase.FeatureDB(path, read_only=True)

def digest(pairs):
    h = hashlib.sha256()
    for anchor, ids in sorted(pairs):
        h.update(repr((anchor, sorted(ids))).encode())
    return h.hexdigest()[:16]

def timed(fn):
    t = time.perf_counter(); pairs = fn(); dt = time.perf_counter() - t
    return {"calls": len(pairs), "ms_per_call": dt / max(len(pairs), 1) * 1000, "digest": digest(pairs)}

out = {}
out["genes_children_l1"] = timed(lambda: [
    (g.id, [c.id for c in db.children(g, level=1)])
    for g in itertools.islice(db.features_of_type("gene"), n)])
out["transcripts_exons"] = timed(lambda: [
    (t.id, [e.id for e in db.children(t, featuretype="exon")])
    for g in itertools.islice(db.features_of_type("gene"), n // 2)
    for t in db.children(g, level=1)])
out["exons_parents_gene"] = timed(lambda: [
    (e.id, [p.id for p in db.parents(e, featuretype="gene")])
    for e in itertools.islice(db.features_of_type("exon"), n * 5)])
rng = random.Random(7)
genes = sorted(g.id for g in db.features_of_type("gene"))
sample = rng.sample(genes, min(n // 5, len(genes)))
out["random_children_l1"] = timed(lambda: [(g, [c.id for c in db.children(g, level=1)]) for g in sample])
ids = sorted(f.id for f in itertools.islice(db.all_features(), 200_000))
points = rng.sample(ids, min(n // 5, len(ids)))
out["random_getitem"] = timed(lambda: [(i, [db[i].start]) for i in points])
print(json.dumps(out))
"""

LABELS = {
    "genes_children_l1": "genes -> children(level=1)",
    "transcripts_exons": "transcripts -> exons",
    "exons_parents_gene": "exons -> parents(gene)",
    "random_children_l1": "random children(level=1)",
    "random_getitem": "random db[id]",
}


def _run(name: str, spec: str, n: int) -> dict:
    path, _, python = spec.partition("@")
    lib = "gffutils" if name.startswith("gffutils") else "gffbase"
    env = None
    if lib == "gffbase" and not python:
        import os

        env = {**os.environ, "PYTHONPATH": str(ROOT / "python")}
    proc = subprocess.run(
        [python or sys.executable, "-c", CHILD, lib, path, str(n)],
        capture_output=True,
        text=True,
        env=env,
        check=False,
    )
    if proc.returncode != 0:
        raise SystemExit(f"{name} failed:\n{proc.stderr[-2000:]}")
    return json.loads(proc.stdout.strip().splitlines()[-1])


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument(
        "--engine",
        action="append",
        required=True,
        metavar="NAME=DB[@PYTHON]",
        help="gffutils=..., gffbase=...; a name starting 'gffutils' uses gffutils",
    )
    ap.add_argument("--n", type=int, default=5000, help="genes walked (exons: 5x)")
    ap.add_argument("--out", type=Path, default=None, help="write the results as JSON")
    args = ap.parse_args()

    results = {}
    for item in args.engine:
        name, _, spec = item.partition("=")
        results[name] = _run(name, spec, args.n)
    names = list(results)
    base = names[0]
    print(f"{'workload':30}" + "".join(f"{n:>22}" for n in names))
    for key, label in LABELS.items():
        cells = []
        for name in names:
            r = results[name][key]
            cell = f"{r['ms_per_call']:.3f} ms"
            if name != base:
                same = r["digest"] == results[base][key]["digest"]
                ratio = results[base][key]["ms_per_call"] / r["ms_per_call"]
                cell += f" ({ratio:.2f}x)" if same else " (answers differ)"
            cells.append(cell)
        print(f"{label:30}" + "".join(f"{c:>22}" for c in cells))
    if args.out:
        args.out.write_text(json.dumps({"n": args.n, "engines": results}, indent=1))


if __name__ == "__main__":
    main()
