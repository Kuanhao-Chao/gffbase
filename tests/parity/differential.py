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
"""Differential-testing harness: run one input through both libraries, compare.

The point of this module is to make disagreements *legible*. A bare
``assert a == b`` over two databases produces an unreadable diff; these helpers
reduce each library to comparable, normalized structures and report the first
handful of concrete differences with enough context to act on.

Nothing here asserts. Callers get a :class:`Diff` and decide what to do with
it, which lets the same comparison serve a strict test, an expected-mismatch
test, and an exploratory script.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
UPSTREAM_DATA = REPO_ROOT / "tests" / "data" / "upstream"

#: Set ``GFFBASE_GFFUTILS_DATA`` to a gffutils ``test/data`` directory to
#: unlock fixtures too large to vendor (notably the 9 MB FlyBase file, which is
#: the only corpus here with genuine multipart features).
EXTERNAL_DATA_ENV = "GFFBASE_GFFUTILS_DATA"


def requires_gffutils():
    """Skip marker for tests that need the live oracle, not just the manifest."""
    return pytest.importorskip("gffutils", reason="differential tests need gffutils installed")


#: Set to opt out of the hard failure below, for someone deliberately working
#: from a checkout without the vendored corpus.
ALLOW_MISSING_ENV = "GFFBASE_ALLOW_MISSING_FIXTURES"


def fixture(name: str) -> str:
    """Absolute path to a vendored upstream fixture.

    A missing fixture is an **error**, not a skip.

    It used to be a skip, and that is precisely how 23 of these files came to
    be gitignored and untracked without anyone noticing: they existed on the
    machine that created them, so the suite was green there, while a fresh
    clone silently ran a fraction of the comparisons and still reported
    success. A compatibility gate that quietly shrinks to nothing is worse than
    no gate at all, because it is indistinguishable from a passing one.

    `GFFBASE_ALLOW_MISSING_FIXTURES=1` restores the skipping behaviour for
    anyone who genuinely wants to work without the corpus.
    """
    path = UPSTREAM_DATA / name
    if not path.is_file():
        if os.environ.get(ALLOW_MISSING_ENV):
            pytest.skip(f"upstream fixture not vendored: {name} ({ALLOW_MISSING_ENV} set)")
        raise FileNotFoundError(
            f"upstream fixture not vendored: {name}\n"
            f"Expected at {path}.\n"
            f"The differential corpus lives in tests/data/upstream/ and is committed; "
            f"if it is missing, the checkout is incomplete. Set {ALLOW_MISSING_ENV}=1 "
            f"to skip these tests instead of failing."
        )
    return str(path)


def external_fixture(name: str) -> str:
    """Absolute path to a fixture that lives only in a gffutils checkout."""
    root = os.environ.get(EXTERNAL_DATA_ENV)
    if not root:
        pytest.skip(f"set {EXTERNAL_DATA_ENV} to a gffutils test/data dir to run this")
    path = Path(root) / name
    if not path.is_file():
        pytest.skip(f"{name} not found under {EXTERNAL_DATA_ENV}={root}")
    return str(path)


# ---------------------------------------------------------------------------
# Diff reporting
# ---------------------------------------------------------------------------


@dataclass
class Diff:
    """Differences found between the oracle and gffbase for one comparison."""

    what: str
    entries: list[str] = field(default_factory=list)
    #: Populated when both sides produced a value for the same key, so callers
    #: can classify "different" separately from "missing".
    only_oracle: list[str] = field(default_factory=list)
    only_ours: list[str] = field(default_factory=list)

    def __bool__(self) -> bool:
        return bool(self.entries or self.only_oracle or self.only_ours)

    def report(self, limit: int = 12) -> str:
        if not self:
            return f"{self.what}: identical"
        lines = [f"{self.what}: {len(self.entries)} differing"]
        if self.only_oracle:
            lines.append(
                f"  only in gffutils ({len(self.only_oracle)}): {self.only_oracle[:limit]}"
            )
        if self.only_ours:
            lines.append(f"  only in gffbase  ({len(self.only_ours)}): {self.only_ours[:limit]}")
        for entry in self.entries[:limit]:
            lines.append(f"  {entry}")
        if len(self.entries) > limit:
            lines.append(f"  ... and {len(self.entries) - limit} more")
        return "\n".join(lines)


def compare_mappings(what: str, oracle: dict[str, Any], ours: dict[str, Any]) -> Diff:
    """Compare two keyed structures, separating missing keys from differing values."""
    diff = Diff(what)
    diff.only_oracle = sorted(set(oracle) - set(ours))
    diff.only_ours = sorted(set(ours) - set(oracle))
    for key in sorted(set(oracle) & set(ours)):
        if oracle[key] != ours[key]:
            diff.entries.append(
                f"{key}:\n      gffutils: {oracle[key]!r}\n      gffbase : {ours[key]!r}"
            )
    return diff


def compare_sequences(what: str, oracle: list[Any], ours: list[Any]) -> Diff:
    """Compare two ordered sequences positionally.

    Order is part of the contract for `all_features()`, `features_of_type()`
    and friends, so this deliberately does not sort.
    """
    diff = Diff(what)
    if len(oracle) != len(ours):
        diff.entries.append(f"length: gffutils={len(oracle)} gffbase={len(ours)}")
    for i, (a, b) in enumerate(zip(oracle, ours, strict=False)):
        if a != b:
            diff.entries.append(f"[{i}]:\n      gffutils: {a!r}\n      gffbase : {b!r}")
    return diff


# ---------------------------------------------------------------------------
# Normalization: reduce a feature from either library to comparable data.
# ---------------------------------------------------------------------------

#: The nine GFF columns, in order. `id` is the database primary key and is
#: compared separately -- it is not a GFF column.
GFF_FIELDS = (
    "seqid",
    "source",
    "featuretype",
    "start",
    "end",
    "score",
    "strand",
    "frame",
)


def feature_fields(feature) -> dict[str, Any]:
    """The eight scalar GFF columns of a feature, from either library."""
    return {name: getattr(feature, name) for name in GFF_FIELDS}


def feature_attributes(feature) -> dict[str, list[str]]:
    """Attributes as a plain ``{key: [values]}`` dict.

    Both libraries wrap values in lists, but the oracle's `Attributes` class
    can be configured to unwrap single values via
    `gffutils.constants.always_return_list`. Normalizing to lists here means
    the comparison does not depend on that global.
    """
    out: dict[str, list[str]] = {}
    for key in feature.attributes:
        value = feature.attributes[key]
        out[key] = list(value) if isinstance(value, (list, tuple)) else [value]
    return out


def feature_record(feature) -> dict[str, Any]:
    """Everything about a feature that both libraries should agree on."""
    record = feature_fields(feature)
    record["attributes"] = feature_attributes(feature)
    record["extra"] = list(feature.extra) if feature.extra else []
    return record


def features_by_id(db) -> dict[str, dict[str, Any]]:
    """All features in a database, keyed by primary key."""
    return {f.id: feature_record(f) for f in db.all_features()}


def feature_ids_in_order(db) -> list[str]:
    return [f.id for f in db.all_features()]


def relations(db) -> set[tuple[str, str, int]]:
    """Every (parent, child, level) triple the database exposes.

    Read through the public `children()` API rather than the storage layer, so
    the comparison is about observable behaviour and not about the fact that
    one library uses a `relations` table and the other a transitive closure.
    """
    out: set[tuple[str, str, int]] = set()
    for parent in db.all_features():
        for level in (1, 2, 3):
            for child in db.children(parent.id, level=level):
                out.add((parent.id, child.id, level))
    return out


def directives(db) -> list[str]:
    return list(getattr(db, "directives", []) or [])


def dialect(db) -> dict[str, Any]:
    """The stored dialect, with keys both libraries define.

    gffbase records extra keys the oracle never had; comparing only the shared
    ones keeps the check about disagreement rather than about additions.
    """
    theirs = dict(getattr(db, "dialect", {}) or {})
    return {k: v for k, v in theirs.items() if k != "order"}


def featuretype_counts(db) -> dict[str, int]:
    return {ft: db.count_features_of_type(ft) for ft in db.featuretypes()}


# ---------------------------------------------------------------------------
# Exception comparison
# ---------------------------------------------------------------------------


def capture(fn, *args, **kwargs) -> tuple[Any, Exception | None]:
    """Run ``fn`` and return ``(result, None)`` or ``(None, exception)``."""
    try:
        return fn(*args, **kwargs), None
    except Exception as exc:  # noqa: BLE001 - comparing failure modes is the point
        return None, exc


def compare_failures(what: str, oracle_exc: Exception | None, our_exc: Exception | None) -> Diff:
    """Compare how the two libraries fail (or do not) on the same input.

    Exception *class* identity cannot be required across libraries, so this
    compares the observable contract instead: whether it raised at all, and
    whether the raised type is catchable the same way. The oracle raises a bare
    `ValueError` for duplicate IDs while documenting `DuplicateIDError`;
    gffbase makes `DuplicateIDError` subclass `ValueError` so both hold.
    """
    diff = Diff(what)
    if (oracle_exc is None) != (our_exc is None):
        diff.entries.append(
            f"raised: gffutils={type(oracle_exc).__name__ if oracle_exc else 'no'} "
            f"gffbase={type(our_exc).__name__ if our_exc else 'no'}"
        )
        return diff
    if oracle_exc is None:
        return diff
    if not isinstance(our_exc, type(oracle_exc)) and not isinstance(oracle_exc, type(our_exc)):
        diff.entries.append(
            f"incompatible exception types: "
            f"gffutils={type(oracle_exc).__name__}({oracle_exc}) "
            f"gffbase={type(our_exc).__name__}({our_exc})"
        )
    return diff


# ---------------------------------------------------------------------------
# Database construction
# ---------------------------------------------------------------------------


def build_both(path: str, **create_kwargs):
    """Build the same database with both libraries.

    Returns ``(oracle_db, gffbase_db)``. Keyword arguments are passed verbatim
    to both `create_db` implementations -- which is the point: any argument the
    oracle honours and gffbase ignores shows up as a difference in the result.
    """
    import gffbase
    import gffutils

    oracle = gffutils.create_db(path, ":memory:", **create_kwargs)
    ours = gffbase.create_db(path, ":memory:", **create_kwargs)
    return oracle, ours
