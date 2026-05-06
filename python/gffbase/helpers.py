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
"""Compatibility helpers."""

from __future__ import annotations

from pathlib import Path


def example_filename(fn: str) -> str:
    """Return the absolute path to a packaged example file under ``tests/data``.

    Mirrors the legacy ``gffutils.example_filename`` API. The legacy package
    shipped fixtures inside the package itself; we point at the test fixtures
    directory bundled with the source distribution.
    """
    here = Path(__file__).resolve().parent
    candidates = [
        here.parent.parent / "tests" / "data" / fn,
        here / "data" / fn,
    ]
    for c in candidates:
        if c.is_file():
            return str(c)
    raise FileNotFoundError(f"example file not found: {fn}")
