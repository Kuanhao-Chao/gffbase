"""`gffbase.inspect.inspect` against `gffutils.inspect.inspect`."""

from __future__ import annotations

import pytest

from . import differential as D

pytestmark = pytest.mark.parity

LOOK_FOR = [
    ["featuretype", "chrom", "attribute_keys", "feature_count"],
    ["source", "strand"],
    ["attribute_keys"],
]


@pytest.mark.parametrize("name", ["intro_docs_example.gff", "FBgn0031208.gtf", "hybrid1.gff3"])
@pytest.mark.parametrize("look_for", LOOK_FOR, ids=lambda x: "+".join(x))
@pytest.mark.parametrize("limit", [None, 5])
def test_inspect_matches_the_oracle(name, look_for, limit):
    D.requires_gffutils()
    from gffbase.inspect import inspect
    from gffutils.inspect import inspect as oracle_inspect

    path = D.fixture(name)
    theirs = oracle_inspect(path, look_for=look_for, limit=limit, verbose=False)
    ours = inspect(path, look_for=look_for, limit=limit, verbose=False)
    assert {k: dict(v) if isinstance(v, dict) else v for k, v in ours.items()} == {
        k: dict(v) if isinstance(v, dict) else v for k, v in theirs.items()
    }
