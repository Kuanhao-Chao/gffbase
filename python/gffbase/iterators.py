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
"""``DataIterator`` — legacy-compatible factory wrapping the new parser.

Yields ``Feature`` objects (not raw ``ParsedFeature``) so downstream code
that consumes the iterator and prints features Just Works.
"""

from __future__ import annotations

import os
from collections.abc import Iterator

from gffbase import parser as _parser
from gffbase.feature import Feature, ParsedFeature, _with_quote_rule

#: A transform's return value that drops the feature. gffutils documents "a
#: value that evaluates to False", and `create_db(transform=...)` already
#: drops on it; `DataIterator` used to keep the feature on `None`, so a
#: ported filter written as `lambda f: f if keep(f) else None` filtered
#: nothing. `True` keeps the original, as ingest does.
_SKIP = object()


def _apply_transform(transform, feature):
    """The feature to yield after `transform`, or `_SKIP`."""
    out = transform(feature)
    if out is True:
        return feature
    if isinstance(out, Feature):
        # Tested first: a Feature with no coordinates has length 0, so its
        # truthiness says nothing about whether the caller meant to drop it.
        return out
    if not out:
        return _SKIP
    return out


class _DataIterator:
    """Public iterator. Exposes ``.dialect`` and ``.directives`` like legacy.

    Reads with the same rules as `create_db` in compat mode: a record gffutils
    would load is yielded (a violation is recorded in `warnings`, not
    raised), a line that cannot be parsed at all is skipped and recorded, and
    a GFF3 value wholly in quotes is read without them. It used to apply the
    strict NCBI profile, so it refused files that `create_db` loads and that
    `gffutils.DataIterator` reads -- `ID="g001"` among them.
    """

    def __init__(
        self,
        data,
        checklines: int = 10,
        transform=None,
        force_dialect_check: bool = False,
        from_string: bool = False,
        **kwargs,
    ):
        if from_string:
            self._inner = _parser.parse_bytes(
                data.encode("utf-8") if isinstance(data, str) else data,
                checklines=checklines,
                force_dialect_check=force_dialect_check,
                strict=False,
                validation="gffutils",
            )
        else:
            self._inner = _parser.parse_gff(
                data,
                checklines=checklines,
                force_dialect_check=force_dialect_check,
                strict=False,
                validation="gffutils",
            )
        self._transform = transform

    def __iter__(self) -> Iterator[Feature]:
        return self

    def __next__(self) -> Feature:
        # A loop, not a recursive `__next__()` per dropped feature: a filter
        # that drops a few thousand records in a row used to hit the
        # recursion limit.
        while True:
            pf: ParsedFeature = next(self._inner)
            feat = _with_quote_rule(
                Feature(
                    seqid=pf.seqid,
                    source=pf.source,
                    featuretype=pf.featuretype,
                    start=pf.start,
                    end=pf.end,
                    score=pf.score,
                    strand=pf.strand,
                    frame=pf.frame,
                    attributes=pf.attributes_blob,
                    extra=("\t".join(pf.extra)) if pf.extra else None,
                    dialect=self._inner.dialect() or {"fmt": "gff3"},
                ),
                True,
            )
            if self._transform is None:
                return feat
            out = _apply_transform(self._transform, feat)
            if out is not _SKIP:
                return out

    @property
    def warnings(self) -> list[dict]:
        """Records that broke a rule or could not be parsed, as `create_db`
        reports them in `FeatureDB.warnings`: dicts with `line_no`, `kind` and
        `message`. Complete once iteration has finished."""
        return list(getattr(self._inner, "warnings", []) or [])

    @property
    def dialect(self) -> dict:
        return self._inner.dialect() or {"fmt": "gff3"}

    @property
    def directives(self) -> list[str]:
        return list(self._inner.directives())


def DataIterator(
    data,
    checklines: int = 10,
    transform=None,
    force_dialect_check: bool = False,
    from_string: bool = False,
    **kwargs,
) -> _DataIterator:
    """Legacy factory. Returns an iterator yielding ``Feature``.

    Dispatches on the input, the way the subclasses below have always
    described and the way `gffutils.DataIterator` behaves:

    * ``from_string=True`` -- `data` is the GFF text itself.
    * a URL -- fetched to a temporary file first (`_UrlIterator`).
    * any other path-like -- read from disk, gzipped or not (`_FileIterator`).
    * an iterable of `Feature` / `ParsedFeature` -- yielded straight back
      (`_FeatureIterator`), so a generator can be piped into `create_db` or
      `FeatureDB.update` without being written to a file first.

    The dispatch was missing: every input went to `_DataIterator`, which
    hands whatever it gets to `parse_gff(path)`. So a URL was opened as a
    filename and an in-memory feature list raised, while the subclasses that
    exist to handle both sat unreachable and their docstrings described a
    behaviour the factory did not have.
    """
    if from_string:
        return _DataIterator(
            data,
            checklines=checklines,
            transform=transform,
            force_dialect_check=force_dialect_check,
            from_string=True,
            **kwargs,
        )

    if isinstance(data, str | os.PathLike):
        cls = _UrlIterator if is_url(str(data)) else _FileIterator
        return cls(
            os.fspath(data) if isinstance(data, os.PathLike) else data,
            checklines=checklines,
            transform=transform,
            force_dialect_check=force_dialect_check,
            **kwargs,
        )

    if hasattr(data, "__iter__"):
        return _FeatureIterator(data, transform=transform, **kwargs)

    raise TypeError(
        "DataIterator accepts a path, a URL, GFF text with from_string=True, "
        f"or an iterable of features; got {type(data)!r}"
    )


# ---------------------------------------------------------------------------
# Compatibility surface.
# ---------------------------------------------------------------------------
#
# `FeatureDB.update()` and `delete()` point users at these class names in their
# docstrings, so they are part of the effective contract even though they are
# underscore-named. gffbase has one iterator that dispatches on its input
# rather than a class per source, so these are thin views over it.


def is_url(url) -> bool:
    """True if `url` has a protocol gffbase can fetch.

    Parameter is named `url` to match the oracle: the parity gate checks that
    every parameter name the oracle accepts is accepted here too, and a caller
    writing `is_url(url=...)` would otherwise break.
    """
    from urllib.parse import urlparse

    if not isinstance(url, str):
        return False
    try:
        parsed = urlparse(url)
    except ValueError:
        return False
    return parsed.scheme in ("http", "https", "ftp") and bool(parsed.netloc)


class Directive:
    """A `##` directive, with the prefix stripped.

    Defined upstream and never used there; kept so that code importing it
    resolves. Equality is on the text, so a directive compares equal to the
    string it wraps.
    """

    __slots__ = ("info",)

    def __init__(self, line: str):
        self.info = line.lstrip("#").strip() if line.startswith("#") else line

    def __str__(self) -> str:
        return self.info

    def __repr__(self) -> str:
        return f"Directive({self.info!r})"

    def __eq__(self, other) -> bool:
        if isinstance(other, Directive):
            return self.info == other.info
        return self.info == other

    def __hash__(self) -> int:
        return hash(self.info)


class _BaseIterator(_DataIterator):
    """Base of the iterator hierarchy. `DataIterator` dispatches to one of the
    subclasses below by inspecting its input; constructing one directly skips
    that dispatch and asserts the source kind."""


class _FileIterator(_BaseIterator):
    """Features from a file on disk (plain or gzipped)."""


class _UrlIterator(_BaseIterator):
    """Features from a URL.

    gffbase's parser reads local paths, so this fetches to a temporary file
    first and parses that. The download is not streamed: the dialect sniffing
    that precedes parsing needs to re-read the head of the input.
    """

    def __init__(self, data, **kwargs):
        import tempfile
        import urllib.request

        if not is_url(data):
            raise ValueError(f"not a URL: {data!r}")
        suffix = ".gz" if str(data).endswith(".gz") else ".gff3"
        self._tempfile: str | None = None
        with urllib.request.urlopen(data) as response:  # noqa: S310 - scheme checked above
            tmp = tempfile.NamedTemporaryFile(suffix=suffix, delete=False)
            tmp.write(response.read())
            tmp.close()
        self._tempfile = tmp.name
        try:
            super().__init__(tmp.name, **kwargs)
        except BaseException:
            # The download already landed on disk; if parsing the result
            # cannot even start, nothing else will ever remove it.
            self.close()
            raise

    def close(self) -> None:
        """Delete the downloaded temporary file. Idempotent.

        `NamedTemporaryFile(delete=False)` is the only way to hand the parser
        a path it can reopen, and the file was then never unlinked -- every
        `DataIterator(url)` left a full copy of the annotation in the temp
        directory for the life of the process.
        """
        path, self._tempfile = self._tempfile, None
        if path:
            try:
                os.unlink(path)
            except OSError:
                pass

    def __enter__(self) -> _UrlIterator:
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.close()

    def __del__(self) -> None:
        try:
            self.close()
        except Exception:  # pragma: no cover - interpreter teardown
            pass


class _FeatureIterator(_BaseIterator):
    """Features from an in-memory iterable.

    Yields the features it was given, so a caller can pass a generator through
    `create_db` or `FeatureDB.update` without first writing a file.
    """

    def __init__(self, data, transform=None, **kwargs):
        self._features = list(data)
        self._pos = 0
        self._transform = transform
        self._dialect = kwargs.get("dialect") or {"fmt": "gff3"}

    def __iter__(self):
        # NOT `iter(self._features)`. Returning the list's own iterator
        # bypasses `__next__`, so `transform` -- which every other iterator in
        # this module applies -- was silently dropped for in-memory features.
        self._pos = 0
        return self

    def __next__(self):
        while True:
            if self._pos >= len(self._features):
                raise StopIteration
            feature = self._features[self._pos]
            self._pos += 1
            if self._transform is None:
                return feature
            out = _apply_transform(self._transform, feature)
            if out is not _SKIP:
                return out

    # Properties, matching `_DataIterator`. These were written as methods and
    # the resulting mypy override error was silenced with a `type: ignore`,
    # which hid a real inconsistency: `it.directives` returned a bound method
    # on one iterator and a list on another, so the same caller code worked
    # against one and raised `TypeError: 'list' object is not callable`
    # against the other.
    @property
    def dialect(self) -> dict:
        return self._dialect

    @property
    def directives(self) -> list:
        return []
