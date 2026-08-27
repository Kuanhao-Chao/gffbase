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
"""Exception classes — verbatim port of legacy `gffutils.exceptions`.

Same names, same constructors, same attributes. Downstream code that catches
`gffutils.FeatureNotFoundError` works against `gffbase.FeatureNotFoundError`.
"""

from __future__ import annotations


class GFFFormatError(ValueError):
    """Raised when a GFF3 line violates the spec.

    Inherits from ``ValueError`` so legacy code that catches
    ``ValueError`` keeps working — but the dedicated subclass carries
    structured fields ``line_no``, ``kind``, and ``message`` that point
    callers straight to the offender in the input.

    When the Rust extension is loaded, the canonical class is
    ``gffbase._native.GFFFormatError`` (a PyO3 ``create_exception!``
    type). At import time, ``gffbase.__init__`` rebinds the public name
    to whichever class is actually live so ``isinstance`` and
    ``except`` clauses keep working regardless of which path raised.
    """

    def __init__(self, message: str = "", *, line_no: int = 0, kind: str = ""):
        super().__init__(message)
        self.message = message
        self.line_no = line_no
        self.kind = kind


class FeatureNotFoundError(Exception):
    """Raised by ``FeatureDB.__getitem__`` when the requested ID is absent."""

    def __init__(self, feature_id: str):
        Exception.__init__(self, f"feature not found: {feature_id}")
        self.feature_id = feature_id


# ---------------------------------------------------------------------------
# The three exceptions below subclass `ValueError`, where gffutils subclasses
# plain `Exception`. That is deliberate, and it makes gffbase catchable *both*
# ways rather than one.
#
# gffutils exports `DuplicateIDError` and documents it, but the code path that
# should raise it -- `_do_merge` under `merge_strategy="error"` -- raises a
# bare `ValueError("Duplicate ID ...")` instead. Real callers therefore write
# `except ValueError`. Raising a `DuplicateIDError` that *is* a `ValueError`
# satisfies those callers and the documented type at once, instead of forcing
# a choice between compatibility and correctness.
#
# `FeatureNotFoundError` is deliberately NOT rebased: gffutils raises it from
# `__getitem__`, where callers reach for `KeyError`-shaped handling, and
# making it a `ValueError` would be a gratuitous change.
# ---------------------------------------------------------------------------


class DuplicateIDError(ValueError):
    """Two features resolved to the same primary key.

    Raised during ingestion under ``merge_strategy="error"`` (the default).
    """


class SynthesisConflictError(DuplicateIDError):
    """One inferred GTF parent ID spans incompatible genomic groups.

    A gene or transcript cannot be synthesized safely when the same raw
    ``gene_id`` or ``transcript_id`` occurs on more than one ``(seqid,
    strand)`` pair.  The default is to raise rather than silently choose one
    location and build an envelope across chromosomes.  Callers that
    deliberately want one parent per group can opt into
    ``merge_strategy="create_unique"``.

    Subclassing :class:`DuplicateIDError` preserves compatibility with code
    that already catches duplicate identifiers during ingestion.
    """


class AttributeStringError(ValueError):
    """Raised on malformed col-9 attributes."""


class EmptyInputError(ValueError):
    """Raised when an input file or iterable yields no features."""


class SchemaVersionError(ValueError):
    """The database's schema version is one this build cannot read.

    Raised when opening a database written by a *newer* gffbase, or one whose
    ``meta.schema_version`` is unintelligible. An older version is not an error:
    it degrades to a read-only compatibility mode instead.

    This exists because the version was previously written and never read, so a
    schema mismatch surfaced as a missing-column error from whichever query
    happened to run first -- or, worse, as a silently wrong answer from one that
    did not touch the new columns.
    """


class MultipartConstraintError(ValueError):
    """Lines sharing one ``ID`` cannot be one discontinuous feature.

    GFF3 requires the segments of a discontinuous feature to agree on seqid,
    source, featuretype and strand. Raised in ``mode="strict"`` when they do
    not; pass ``on_multipart_conflict="split"`` to partition them instead.
    """


# ---------------------------------------------------------------------------
# Connection lifecycle. Both subclass `ValueError` for the same reason the
# block above does: existing `except ValueError` handlers keep working, and a
# caller who wants to distinguish these can.
#
# Neither has a gffutils counterpart, because gffutils has no lifecycle to get
# wrong -- SQLite hands out shared read connections and never locks a reader
# out. DuckDB takes an exclusive lock for the life of a write handle, so
# releasing it and opening read-only are operations that had to exist here.
# ---------------------------------------------------------------------------


class ReadOnlyError(ValueError):
    """A write was attempted on a database opened with ``read_only=True``."""


class ClosedDatabaseError(ValueError):
    """A `FeatureDB` was used after ``close()``.

    Raised in place of DuckDB's own ``ConnectionException``, which reports
    that a connection is closed without saying which object or which call.
    """
