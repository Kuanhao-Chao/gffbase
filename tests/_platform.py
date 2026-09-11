"""Platform guards shared by test modules."""

from __future__ import annotations

import sys

import pytest

# The cluster campaign harness is Linux-only by design -- it publishes with
# renameat2, binds runs to /proc boot and mount identity, and supervises
# process groups -- and it refuses to run anywhere else. Its tests therefore
# exercised a deliberate refusal on macOS (115 failures, 44 errors) and could
# not even be imported on Windows. They run on every Linux cell.
LINUX_ONLY_CAMPAIGN = pytest.mark.skipif(
    sys.platform != "linux",
    reason=(
        "the cluster campaign harness is Linux-only by design: it needs renameat2, "
        "/proc boot and mount identity, and process groups"
    ),
)
