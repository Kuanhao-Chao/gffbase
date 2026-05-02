"""Run only the differential-correctness phase. Used when routing numbers are
already in `bench/out/results_no_legacy.json` and we just need to plug in the
correctness numbers."""

from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from benchmark import diff_correctness, slice_gtf, DEFAULT_INPUT, OUT


def main():
    diff_lines = int(sys.argv[1]) if len(sys.argv) > 1 else 50_000
    out_json = Path(sys.argv[2]) if len(sys.argv) > 2 else (OUT / "results_no_legacy.json")

    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        slice_path = tmp / "slice.gtf"
        n = slice_gtf(DEFAULT_INPUT, slice_path, diff_lines)
        diff = diff_correctness(slice_path, tmp)
    diff["n_input_lines"] = n

    if out_json.exists():
        results = json.loads(out_json.read_text())
    else:
        results = {}
    results["correctness"] = diff
    out_json.write_text(json.dumps(results, indent=2))
    print(json.dumps(diff, indent=2))


if __name__ == "__main__":
    main()
