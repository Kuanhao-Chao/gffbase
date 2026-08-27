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
"""Public parser entry points. Dispatches to the Rust extension when available,
falls back to the pure-Python implementation otherwise. The two implementations
are required by tests to produce identical output.
"""

from __future__ import annotations

from gffbase._pyfallback import parser as _pyparser

# Column-9 serialization. Defined in a leaf module so `gffbase.feature` can use
# it without importing this one (which imports `gffbase.feature`), and
# re-exported here under the names gffutils uses, which is where callers and
# the upstream test suite look for them.
from gffbase._serialize import (
    Quoter,
    _reconstruct,
    _split_keyvals,
    encode_value,
    quoted_semicolon_patterns,
    quoter,
)
from gffbase.feature import ParsedFeature

__all__ = [
    "Quoter",
    "_reconstruct",
    "_split_keyvals",
    "detect_dialect",
    "encode_value",
    "native_available",
    "parse_bytes",
    "parse_gff",
    "quoted_semicolon_patterns",
    "quoter",
]

try:  # pragma: no cover - import availability is env-dependent
    from gffbase import _native as _rust

    _NATIVE = True
except ImportError:
    _rust = None
    _NATIVE = False


def native_available() -> bool:
    """True if the compiled extension is importable."""
    return _NATIVE


class _Iterator:
    """Adapter: wraps either the Rust iterator (yielding 11-tuples) or the
    pure-Python iterator (yielding ParsedFeature) and always yields
    ParsedFeature.

    Also exposes ``.warnings`` — a list of structured-error
    dicts (``line_no``, ``kind``, ``message``) collected when the
    iterator was created with ``strict=False``.
    """

    __slots__ = ("_inner", "_native")

    def __init__(self, inner, native: bool):
        self._inner = inner
        self._native = native

    def __iter__(self):
        return self

    def __next__(self) -> ParsedFeature:
        item = next(self._inner)
        if self._native:
            return ParsedFeature.from_tuple(item)
        return item

    def dialect(self) -> dict:
        return self._inner.dialect()

    def directives(self) -> list:
        return list(self._inner.directives())

    @property
    def warnings(self) -> list[dict]:
        """Errors that were demoted to warnings during a non-strict run.

        Each item is a dict with keys ``line_no``, ``kind``, and
        ``message``. Empty for strict runs (errors raise instead).
        """
        if hasattr(self._inner, "warnings"):
            w = self._inner.warnings
            return list(w() if callable(w) else w)
        return []


def _resolve_engine(engine: str | None) -> str:
    if engine is None or engine == "auto":
        return "rust" if _NATIVE else "python"
    if engine == "rust" and not _NATIVE:
        raise RuntimeError(
            "Rust extension not built. Run `maturin develop --release` or pass engine='python'."
        )
    if engine not in {"rust", "python"}:
        raise ValueError(f"unknown engine '{engine}'")
    return engine


def _resolve_validation(validation: str) -> str:
    if validation in {"ncbi", "strict"}:
        return "ncbi"
    if validation in {"gffutils", "compat"}:
        return "gffutils"
    raise ValueError(f"validation must be 'gffutils' or 'ncbi'; got {validation!r}")


def parse_gff(
    path: str,
    *,
    checklines: int = 10,
    force_dialect_check: bool = False,
    force_gff: bool = False,
    strict: bool = True,
    validation: str = "ncbi",
    engine: str | None = "auto",
) -> _Iterator:
    """Parse a GFF3/GTF file (plain text or ``.gz``).

    Returns an iterator of ``ParsedFeature`` plus ``.dialect()``,
    ``.directives()`` and ``.warnings`` accessors.

    Parameters
    ----------
    validation : {"ncbi", "gffutils"}
        Which rule set to apply. ``"ncbi"`` (default here) is the full GFF3
        specification. ``"gffutils"`` is the compatibility profile used by
        `create_db`: every rule still runs, but a violation annotates the
        record instead of rejecting it, because real annotation files break
        the spec routinely and gffutils reads them anyway.
    strict : bool
        What a *rejection* does. True (default) raises ``GFFFormatError`` on
        the first offending line; False skips it and records it in
        ``iterator.warnings``. Under ``validation="gffutils"`` nothing is
        rejected, so this only affects lines that cannot be parsed at all.
    """
    eng = _resolve_engine(engine)
    validation = _resolve_validation(validation)
    if eng == "rust":
        from gffbase import constants

        it = _rust.parse_file(
            path,
            checklines=checklines,
            force_dialect_check=force_dialect_check,
            force_gff=force_gff,
            strict=strict,
            validation=validation,
            ignore_url_escape_characters=constants.ignore_url_escape_characters,
        )
        return _Iterator(it, native=True)
    it = _pyparser.parse_file(
        path,
        checklines=checklines,
        force_dialect_check=force_dialect_check,
        force_gff=force_gff,
        strict=strict,
        validation=validation,
    )
    return _Iterator(it, native=False)


def parse_bytes(
    data: bytes,
    *,
    checklines: int = 10,
    force_dialect_check: bool = False,
    force_gff: bool = False,
    strict: bool = True,
    validation: str = "ncbi",
    engine: str | None = "auto",
) -> _Iterator:
    eng = _resolve_engine(engine)
    validation = _resolve_validation(validation)
    if eng == "rust":
        from gffbase import constants

        it = _rust.parse_bytes(
            data,
            checklines=checklines,
            force_dialect_check=force_dialect_check,
            force_gff=force_gff,
            strict=strict,
            validation=validation,
            ignore_url_escape_characters=constants.ignore_url_escape_characters,
        )
        return _Iterator(it, native=True)
    it = _pyparser.parse_bytes(
        data,
        checklines=checklines,
        force_dialect_check=force_dialect_check,
        force_gff=force_gff,
        strict=strict,
        validation=validation,
    )
    return _Iterator(it, native=False)


def detect_dialect(path: str, *, checklines: int = 10, engine: str | None = "auto") -> dict:
    eng = _resolve_engine(engine)
    if eng == "rust":
        return _rust.detect_dialect(path, checklines=checklines)
    return _pyparser.detect_dialect(path, checklines=checklines)
