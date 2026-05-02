"""One-shot patch: redo the RefSeq row of 06_mega.json after the
RefSeq dedup fix landed in `python/gffbase/ingest.py` (NCBI emits
multiple GFF3 rows that share an `ID=cds-…`; we now suffix duplicates
the same way `gffutils.merge_strategy='create_unique'` does).

Reingests the RefSeq corpus, reruns spatial + batched, then merges
into ``benchmarks/out/06_mega.json`` in place. The legacy ingest wall
captured by the original mega run is preserved (it ran successfully).
"""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "python"))

from benchmarks.common import OUT, du, pretty_bytes, pretty_seconds, run_subprocess

# Re-import the helpers that 06_mega.py defined.
import importlib.util
spec = importlib.util.spec_from_file_location(
    "_mega", ROOT / "benchmarks" / "06_mega.py"
)
mega = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mega)

KEY = "refseq"
NAME = "RefSeq GRCh38.p14"
INPUT = mega.DATA / "GCF_000001405.40_GRCh38.p14_genomic.gff.gz"
DBPATH = OUT / f"{KEY}.duckdb"


def main() -> None:
    print(f"=== Patching {NAME} ===", flush=True)
    if DBPATH.exists():
        DBPATH.unlink()

    print("  [gffbase] reingest…", flush=True)
    info = run_subprocess(
        mega.gffbase_ingest_script(INPUT, DBPATH, "gff3"),
        label=f"gffbase ingest({INPUT.name})",
        timeout=1800,
    )
    info["disk_bytes"] = du(DBPATH)
    print(f"    wall={pretty_seconds(info['wall_seconds'])}, "
          f"RSS={pretty_bytes(info['peak_rss_bytes'])}, "
          f"disk={pretty_bytes(info['disk_bytes'])}", flush=True)

    print("  [gffbase] spatial — 5000 regions…", flush=True)
    regions = mega.sample_regions_from_db(DBPATH, n=5000)
    spatial = mega.bench_spatial(DBPATH, regions)
    print(f"    wall={pretty_seconds(spatial['wall_seconds'])}, "
          f"qps={spatial['qps']:.0f}, "
          f"features_returned={spatial['total_features_returned']}",
          flush=True)

    print("  [gffbase] batched — 5000 anchors…", flush=True)
    batched = mega.bench_batched(DBPATH, n_genes=5000)
    if "skipped" not in batched:
        print(f"    wall={pretty_seconds(batched['wall_seconds'])}, "
              f"anchors={batched['n_anchors']}, "
              f"descendants={batched['n_descendants']}",
              flush=True)

    # Merge into existing JSON.
    json_path = OUT / "06_mega.json"
    payload = json.loads(json_path.read_text())
    feature_lines = mega.count_feature_lines(INPUT)
    for i, c in enumerate(payload["corpora"]):
        if c.get("key") == KEY:
            old_legacy = c.get("legacy", {})
            speedup = (
                old_legacy.get("wall_seconds", 0) / info["wall_seconds"]
                if info.get("wall_seconds") and old_legacy.get("wall_seconds")
                else None
            )
            payload["corpora"][i] = {
                "name":          NAME,
                "key":           KEY,
                "input":         str(INPUT),
                "input_bytes":   INPUT.stat().st_size,
                "feature_lines": feature_lines,
                "gffbase":       info,
                "legacy":        old_legacy,
                "ingest_speedup": speedup,
                "spatial":       spatial,
                "batched":       batched,
            }
            break
    json_path.write_text(json.dumps(payload, indent=2, default=str))
    print(f"\nPatched {json_path}", flush=True)


if __name__ == "__main__":
    main()
