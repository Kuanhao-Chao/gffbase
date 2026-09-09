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
from __future__ import annotations

import os
from pathlib import Path

import pytest
from hypothesis import settings

DATA_DIR = Path(__file__).parent / "data"

# Property tests are ordinary, bounded CI tests by default. Maintainers can
# opt into the deeper deterministic campaign without editing test code:
# `GFFBASE_HYPOTHESIS_PROFILE=extended pytest -m property`.
settings.register_profile("quick", max_examples=40, deadline=None, derandomize=True)
settings.register_profile("extended", max_examples=500, deadline=None, derandomize=True)
settings.load_profile(os.environ.get("GFFBASE_HYPOTHESIS_PROFILE", "quick"))


@pytest.fixture
def gff3_path() -> str:
    return str(DATA_DIR / "simple.gff3")


@pytest.fixture
def gtf_path() -> str:
    return str(DATA_DIR / "simple.gtf")


@pytest.fixture(params=["python", "rust"])
def engine(request):
    """Run every test against both engines. Skips Rust if not built."""
    eng = request.param
    if eng == "rust":
        from gffbase import native_available

        if not native_available():
            pytest.skip("Rust extension not built. Run `maturin develop`.")
    return eng


# ---------------------------------------------------------------------------
# A suite that runs nothing exits 0.
#
# This has now happened twice on this branch. Six release-hygiene guards
# skipped for a whole release cycle because `_read` called `pytest.skip()` on
# a moved file. Then a `pytest_collection_modifyitems` hook added in
# `tests/parity/conftest.py` skip-marked every item in the session -- a
# subdirectory conftest receives the *whole* item list, not just its own --
# and the sdist run exited 0 having executed none of its 2,264 tests.
#
# Both were invisible because pytest's exit code cannot distinguish "nothing
# failed" from "nothing ran". This can.
#
# The floor is deliberately far below the real count (~2,260): it is here to
# catch a collapse, not to be a running total that needs maintenance. Set
# GFFBASE_MIN_TESTS=0 for a deliberately narrow run.
# ---------------------------------------------------------------------------

_DEFAULT_MIN_TESTS = 1500


def _default_marker_expression(config) -> str:
    """The `-m` this project's `addopts` applies to every bare `pytest`."""
    addopts = config.getini("addopts") or []
    for index, token in enumerate(addopts):
        if token == "-m" and index + 1 < len(addopts):
            return addopts[index + 1]
        if token.startswith("-m"):
            return token[2:].strip()
    return ""


def pytest_sessionfinish(session, exitstatus):
    # Only a full run makes a claim about coverage. A targeted invocation
    # (-k, a path, a user-supplied marker) legitimately runs a handful.
    #
    # The marker check compares against the default rather than testing for
    # any marker at all: `addopts` in pyproject.toml already applies
    # `-m 'not corpus and not parity and not slow and not pandas_docs'`, so
    # every bare `pytest` arrives here with `markexpr` set. Exempting on
    # "markexpr is truthy" made this guard unable to fire on the one
    # invocation it exists to check -- the same defect it is here to catch.
    if session.config.option.keyword:
        return
    if session.config.option.markexpr != _default_marker_expression(session.config):
        return
    if getattr(session.config.option, "file_or_dir", None) not in ([], None):
        return
    if exitstatus != 0:
        return

    floor = int(os.environ.get("GFFBASE_MIN_TESTS", _DEFAULT_MIN_TESTS))
    if floor <= 0:
        return

    reporter = session.config.pluginmanager.get_plugin("terminalreporter")
    if reporter is None:
        return
    executed = len(reporter.stats.get("passed", [])) + len(reporter.stats.get("failed", []))
    if executed < floor:
        reporter.write_line(
            f"ERROR: only {executed} tests executed, below the floor of {floor}. "
            f"A green run that executed almost nothing is not a passing suite -- "
            f"look for a conftest skip or a collection error before trusting this.",
            red=True,
        )
        session.exitstatus = 1
