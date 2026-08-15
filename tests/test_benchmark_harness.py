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


def test_a_speedup_requires_both_engines_to_have_done_equal_work():
    """A ratio between two different workloads is not a speedup.

    Both feature counts were recorded and then never compared, so a corpus
    where they diverged -- a differing duplicate-ID policy, a parent-synthesis
    difference on GTF -- would have published a headline number comparing two
    different jobs.
    """
    src = _source()
    assert "counts_agree" in src, "the feature-count cross-check is gone"
    assert "if g_wall and counts_agree:" in src, (
        "the speedup is no longer gated on the two engines agreeing on n_features"
    )


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
