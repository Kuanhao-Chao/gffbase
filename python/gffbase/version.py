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
"""Version, under the name gffutils exposes it.

The single source of truth is `gffbase.__version__`, a literal in
`__init__.py`. This module re-reads it rather than restating it, so the two
cannot drift -- `tests/test_release_hygiene.py` already pins that literal
against `pyproject.toml` and `Cargo.toml`.

Unlike the oracle's, this does NOT consult installed distribution metadata.
Doing so is what makes `gffutils.version.version` report some unrelated
installed copy's version when run from a source checkout -- the reason the
parity manifest pins its oracle by git commit rather than by version string.
"""

from __future__ import annotations

from gffbase import __version__ as version

__all__ = ["version"]
