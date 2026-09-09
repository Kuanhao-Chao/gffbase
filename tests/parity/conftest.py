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
"""Skip the differential suite when the oracle is not installed.

`pyproject.toml` declares the marker as "differential comparison against an
**installed** gffutils oracle", and puts gffutils in the `bench` extra rather
than `test`: it is a pinned git checkout of a third-party library used as a
comparator, not a dependency of gffbase. So `pip install gffbase[test]` --
which is what an sdist consumer runs, and what `qualification.yml` installs
before the sdist smoke -- legitimately has no oracle.

Without this, every test here raised `ModuleNotFoundError` from a
function-level `import gffutils`. An error and a skip mean different things:
an error says gffbase is broken, when what happened is that an optional
comparator is absent. Anyone reading a red suite would have to work that out
from the traceback.

The related failure was worse: `test_database_signature.py` imported the
oracle at *module* scope, so pytest aborted collection of the entire session
and 2,200 unrelated tests never ran. That one is guarded at its own import.

To run these, install the oracle at the pinned commit -- see
`.github/workflows/ci.yml`'s parity job, which is the authority on which
commit the manifest was generated from.
"""

from __future__ import annotations

import pathlib

import pytest

HERE = pathlib.Path(__file__).resolve().parent


def pytest_collection_modifyitems(config, items):  # noqa: ARG001
    """Skip only the items under this directory.

    `pytest_collection_modifyitems` in a *subdirectory* conftest still receives
    the whole session's item list, not just this package's. Marking every item
    skipped here therefore skipped all 2,264 tests -- a suite that exits 0
    having run nothing, which is the exact failure mode this branch exists to
    remove. The path filter is load-bearing.
    """
    try:
        import gffutils  # noqa: F401

        return
    except ImportError:
        pass

    skip = pytest.mark.skip(
        reason="gffutils oracle not installed (it is in the `bench` extra, not `test`)"
    )
    for item in items:
        if HERE in pathlib.Path(str(item.fspath)).resolve().parents:
            item.add_marker(skip)
