"""Exception classes — verbatim port of legacy `gffutils.exceptions`.

Same names, same constructors, same attributes. Downstream code that catches
`gffutils.FeatureNotFoundError` works against `gffbase.FeatureNotFoundError`.
"""

from __future__ import annotations


class FeatureNotFoundError(Exception):
    """Raised by ``FeatureDB.__getitem__`` when the requested ID is absent."""

    def __init__(self, feature_id: str):
        Exception.__init__(self, f"feature not found: {feature_id}")
        self.feature_id = feature_id


class DuplicateIDError(Exception):
    """Raised during ingestion when a duplicate ID is encountered with
    ``merge_strategy='error'`` (Phase 6 will wire `merge_strategy`)."""


class AttributeStringError(Exception):
    """Raised on malformed col-9 attributes."""


class EmptyInputError(Exception):
    """Raised when an input file or iterable yields no features."""
