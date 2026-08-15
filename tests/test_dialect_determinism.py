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
"""Dialect inference must be a pure function of the input.

Both engines used to resolve a tied plurality vote through a randomly-seeded
hash container -- `HashMap` in Rust, `set()` in Python -- so the winner varied
between processes. Because the chosen separator is what a re-serialized
feature is written with, the same annotation file could round-trip to
different text on different runs of identical code. For a tool whose output
feeds downstream pipelines that is a reproducibility defect, not a cosmetic
one, and it is invisible to any single-run test.

These tests fail with roughly p = 1 - 2^-N per run under the old
implementations, and deterministically under the current ones.
"""

from __future__ import annotations

import os
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest
from gffbase.dialect import merge_dialects

REPO_ROOT = Path(__file__).resolve().parent.parent

# A dead-even tie: two samples each. Any hash-order-dependent tie-break has a
# 50% chance of flipping per process.
TIED_SAMPLES = [
    {"field separator": ";"},
    {"field separator": "; "},
    {"field separator": ";"},
    {"field separator": "; "},
]


def test_python_dialect_vote_is_deterministic_in_process():
    first = merge_dialects(TIED_SAMPLES)["field separator"]
    for _ in range(200):
        assert merge_dialects(TIED_SAMPLES)["field separator"] == first


def test_python_dialect_vote_breaks_ties_by_first_appearance():
    """A defined rule, not just a stable one."""
    assert merge_dialects(TIED_SAMPLES)["field separator"] == ";"
    flipped = [
        {"field separator": "; "},
        {"field separator": ";"},
        {"field separator": "; "},
        {"field separator": ";"},
    ]
    assert merge_dialects(flipped)["field separator"] == "; "


def _run_in_fresh_interpreter(code: str) -> str:
    """Run `code` in a new process so it gets a fresh hash seed.

    PYTHONHASHSEED is randomized per process, and Rust's RandomState is seeded
    per process too, so cross-run agreement can only be observed this way.
    """
    # Inherit the environment and override only PYTHONPATH. Replacing it
    # wholesale with a POSIX `PATH` broke Windows outright: without
    # `SystemRoot` the interpreter cannot reach the OS crypto API and dies
    # before running a line -- `Fatal Python error:
    # _Py_HashRandomization_Init: failed to get random numbers`. The point of
    # this helper is a fresh HASH SEED, which a new process gives regardless
    # of what else it inherits.
    env = dict(os.environ)
    env["PYTHONPATH"] = str(REPO_ROOT / "python")
    result = subprocess.run(
        [sys.executable, "-c", textwrap.dedent(code)],
        capture_output=True,
        text=True,
        cwd=REPO_ROOT,
        env=env,
        timeout=120,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    return result.stdout.strip()


@pytest.mark.slow
def test_python_dialect_vote_is_deterministic_across_processes():
    code = """
        from gffbase.dialect import merge_dialects
        samples = [
            {"field separator": ";"},
            {"field separator": "; "},
            {"field separator": ";"},
            {"field separator": "; "},
        ]
        print(merge_dialects(samples)["field separator"])
    """
    results = {_run_in_fresh_interpreter(code) for _ in range(12)}
    assert len(results) == 1, f"dialect vote varied across processes: {results}"


@pytest.mark.slow
def test_end_to_end_dialect_is_deterministic_across_processes():
    """The property that actually matters: same file in, same dialect out.

    `gms2_example.gff3` mixes single-attribute lines (which observe `;`) with
    multi-attribute lines (which observe `; `), so it produces exactly the
    tied vote this guards.
    """
    fixture = REPO_ROOT / "tests" / "data" / "upstream" / "gms2_example.gff3"
    if not fixture.is_file():
        pytest.skip("upstream fixture not vendored")

    code = f"""
        import warnings
        warnings.filterwarnings("ignore")
        import gffbase
        db = gffbase.create_db({str(fixture)!r}, ":memory:")
        print(db.dialect["field separator"])
    """
    results = {_run_in_fresh_interpreter(code) for _ in range(8)}
    assert len(results) == 1, f"end-to-end dialect varied across processes: {results}"
