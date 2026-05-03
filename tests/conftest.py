# ---------------------------------------------------------------------------
# Author: Kuan-Hao Chao <kuanhao.chao@gmail.com>
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
