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
"""The benchmark harness produces the published numbers, so it is testable code.

Nothing here runs a benchmark -- the sweep takes hours. These pin the
properties that decide whether a published number means anything:

* a speedup is only reported when both engines did the SAME work;
* a repeated measurement records a real spread, and a single one does not
  pretend to;
* the committed results file describes a measurement, not a machine.
"""

from __future__ import annotations

import json
import re
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
MEGA = REPO_ROOT / "benchmarks" / "06_mega.py"
RESULTS = REPO_ROOT / "benchmarks" / "results" / "06_mega.json"


def _source() -> str:
    return MEGA.read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# The documented interface has to exist
# ---------------------------------------------------------------------------


def test_the_harness_imports_without_the_bench_extras():
    """`--help` must not need a measurement library.

    `benchmarks/common.py` imported `psutil` at module scope, so importing the
    harness at all -- including to print its usage -- failed without the
    `bench` extra. The test job installs `[test,all]`, so this took out 15 of
    18 CI jobs. `psutil` is now imported inside the two functions that measure
    with it.
    """
    code = textwrap.dedent("""
        import sys
        class Blocker:
            def find_module(self, name, path=None):
                if name == "psutil":
                    return self
            def load_module(self, name):
                raise ImportError("No module named 'psutil'")
        sys.meta_path.insert(0, Blocker())
        import benchmarks.common          # noqa: F401
        print("imported")
    """)
    proc = subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True,
        text=True,
        cwd=REPO_ROOT,
        timeout=120,
    )
    assert proc.returncode == 0, (
        f"benchmarks.common cannot be imported without psutil:\n{proc.stderr}"
    )


@pytest.mark.parametrize("flag", ["--repeats", "--publish", "--only", "--legacy-timeout"])
def test_the_documented_flags_exist(flag):
    """`docs/performance/methodology.md` prints commands a reader will paste.

    It documented `--repeats 5` for months while the flag did not exist, so
    the one command offered for measuring uncertainty died on an argparse
    error. Documentation that cannot be run is worse than none.
    """
    proc = subprocess.run(
        [sys.executable, str(MEGA), "--help"], capture_output=True, text=True, timeout=120
    )
    assert proc.returncode == 0, proc.stderr
    assert flag in proc.stdout


def test_every_command_in_the_methodology_page_parses():
    """Extract the harness invocations from the docs and check argparse accepts them."""
    page = (REPO_ROOT / "docs" / "performance" / "methodology.md").read_text(encoding="utf-8")
    commands = re.findall(r"^python benchmarks/06_mega\.py (.+)$", page, re.M)
    assert commands, "no 06_mega.py invocations found in the methodology page"

    for args in commands:
        proc = subprocess.run(
            [sys.executable, str(MEGA), *args.split(), "--help"],
            capture_output=True,
            text=True,
            timeout=120,
        )
        assert proc.returncode == 0, f"documented command rejected: {args}\n{proc.stderr}"


# ---------------------------------------------------------------------------
# Fairness
# ---------------------------------------------------------------------------


def _derive_speedup():
    """Import the harness's derivation without executing its `main`."""
    import importlib.util

    spec = importlib.util.spec_from_file_location("_mega", MEGA)
    module = importlib.util.module_from_spec(spec)
    sys.modules["_mega"] = module
    spec.loader.exec_module(module)
    return module.derive_speedup


def test_a_speedup_requires_both_engines_to_have_done_equal_work():
    """A ratio between two different workloads is not a speedup.

    Both feature counts were recorded and then never compared, so a corpus
    where they diverged -- a differing duplicate-ID policy, a parent-synthesis
    difference on GTF -- would have published a headline number comparing two
    different jobs.
    """
    derive = _derive_speedup()
    speedup, bound, conflict = derive(
        {"wall_seconds": 100.0, "n_features": 1000},
        {"wall_seconds": 200.0, "n_features": 999},
    )
    assert conflict is True
    assert speedup is None and bound is None, "a conflicting count still produced a ratio"

    speedup, bound, conflict = derive(
        {"wall_seconds": 100.0, "n_features": 1000},
        {"wall_seconds": 200.0, "n_features": 1000},
    )
    assert conflict is False
    assert speedup == pytest.approx(2.0) and bound is None


def test_a_capped_legacy_run_still_yields_a_floor():
    """The one case where the legacy feature count CANNOT exist.

    A killed process never reports one, so requiring the two counts to be
    equal suppressed the floor on exactly the runs the floor exists for --
    GENCODE-GTF lost a legitimate "> 22x" that way. Only a genuine conflict
    between two present counts disqualifies the comparison.
    """
    derive = _derive_speedup()
    speedup, bound, conflict = derive(
        {"wall_seconds": 245.1, "n_features": 6_068_892},
        {"wall_seconds": None, "wall_seconds_lower_bound": 5400.0, "n_features": None},
    )
    assert conflict is False
    assert speedup is None, "a capped run must not produce a measured speedup"
    assert bound == pytest.approx(5400.0 / 245.1)


def test_both_engines_are_given_the_same_duplicate_id_policy():
    """The axis that decides whether the run completes at all must match."""
    src = _source()
    assert src.count('merge_strategy="create_unique"') >= 2


def test_the_benchmark_handles_are_closed():
    """A writable DuckDB handle holds an exclusive lock, and the sweep purges
    each corpus as soon as its numbers are recorded. Leaking the handle blocks
    the delete outright on Windows."""
    src = _source()
    assert "with gffbase.FeatureDB(str(db_path), read_only=True) as db:" in src
    assert "db.close()" in src, "bench_batched must release its handle"


# ---------------------------------------------------------------------------
# The committed results file
# ---------------------------------------------------------------------------


@pytest.mark.skipif(not RESULTS.is_file(), reason="no committed results yet")
def test_committed_results_carry_no_absolute_home_paths():
    """The published artifact should describe a measurement, not a machine."""
    text = RESULTS.read_text(encoding="utf-8")
    for leak in ("/Users/", "/home/", "C:\\Users"):
        assert leak not in text, (
            f"{RESULTS.name} contains {leak!r} — paths should be repo-relative "
            "so the file describes the run rather than whoever made it"
        )


@pytest.mark.skipif(not RESULTS.is_file(), reason="no committed results yet")
def test_committed_results_never_pair_a_timeout_with_a_wall_time():
    """A killed run has a lower bound, not a wall. The previous harness wrote
    `wall_seconds = timeout * 2.0` on a kill, and the renderer could not tell
    that apart from a measurement."""
    data = json.loads(RESULTS.read_text(encoding="utf-8"))
    for key, row in (data.get("corpora") or {}).items():
        legacy = row.get("legacy") or {}
        if legacy.get("timed_out"):
            assert legacy.get("wall_seconds") is None, (
                f"{key}: legacy timed out but carries wall_seconds — that number "
                "was synthesized, not measured"
            )
            assert legacy.get("wall_seconds_lower_bound"), (
                f"{key}: a capped run must record its lower bound"
            )


@pytest.mark.skipif(not RESULTS.is_file(), reason="no committed results yet")
def test_a_single_sample_is_never_recorded_as_a_median():
    """`repeat()` deliberately omits `median` at n=1 so a renderer physically
    cannot present one sample as a central tendency."""
    data = json.loads(RESULTS.read_text(encoding="utf-8"))
    for key, row in (data.get("corpora") or {}).items():
        for section in ("spatial", "batched"):
            timing = (row.get(section) or {}).get("timing")
            if not timing:
                continue
            if timing.get("n") == 1:
                assert "median" not in timing, f"{key}.{section}: n=1 carries a median"
                assert "value" in timing
            else:
                assert {"median", "min", "max", "n"} <= set(timing), (
                    f"{key}.{section}: a repeated measurement must record its spread"
                )
