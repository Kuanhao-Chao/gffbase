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

DATA_DIR = Path(__file__).parent / "data"


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


def pytest_configure(config):
    # Ensure the in-tree package is importable without install when running
    # `pytest` directly from the repo.
    repo_root = Path(__file__).parent.parent
    py_src = repo_root / "python"
    os.environ["PYTHONPATH"] = (
        f"{py_src}{os.pathsep}{os.environ.get('PYTHONPATH', '')}"
    )
