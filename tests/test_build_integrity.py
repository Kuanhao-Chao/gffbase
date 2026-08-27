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
"""Build-provenance checks that prevent mixed Python/native installations."""

from __future__ import annotations

from types import SimpleNamespace

import gffbase
import pytest


def test_loaded_native_extension_matches_python_package():
    if not gffbase.native_available():
        pytest.skip("native extension not installed")

    from gffbase import _native

    assert _native.__version__ == gffbase.__version__


def test_public_candidate_version_is_pep440_normalized():
    assert gffbase.__version__ == "0.2.0rc1"


@pytest.mark.parametrize("native_version", ["0.1.0", "0.2.0", None])
def test_native_version_guard_rejects_mixed_build(native_version):
    fake_native = SimpleNamespace(
        __version__=native_version,
        __file__="/tmp/stale/gffbase/_native.abi3.so",
    )
    with pytest.raises(RuntimeError, match="native-extension version mismatch") as excinfo:
        gffbase._require_matching_native_version(fake_native)
    assert "Rebuild or reinstall" in str(excinfo.value)
    assert gffbase.__version__ in str(excinfo.value)


def test_native_version_guard_accepts_matching_build():
    fake_native = SimpleNamespace(__version__=gffbase.__version__)
    gffbase._require_matching_native_version(fake_native)
