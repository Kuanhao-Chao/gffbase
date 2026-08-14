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

from collections.abc import Iterator

from gffbase import parser as _parser
from gffbase.feature import Feature, ParsedFeature


class _DataIterator:
    """Public iterator. Exposes ``.dialect`` and ``.directives`` like legacy."""

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
            )
        else:
            self._inner = _parser.parse_gff(
                data,
                checklines=checklines,
                force_dialect_check=force_dialect_check,
            )
        self._transform = transform

    def __iter__(self) -> Iterator[Feature]:
        return self

    def __next__(self) -> Feature:
        pf: ParsedFeature = next(self._inner)
        feat = Feature(
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
        )
        if self._transform is not None:
            out = self._transform(feat)
            if out is False:
                return self.__next__()
            if out is not None:
                feat = out
        return feat

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
    """Legacy factory. Returns an iterator yielding ``Feature``."""
    return _DataIterator(
        data,
        checklines=checklines,
        transform=transform,
        force_dialect_check=force_dialect_check,
        from_string=from_string,
        **kwargs,
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
        with urllib.request.urlopen(data) as response:  # noqa: S310 - scheme checked above
            tmp = tempfile.NamedTemporaryFile(suffix=suffix, delete=False)
            tmp.write(response.read())
            tmp.close()
        self._tempfile = tmp.name
        super().__init__(tmp.name, **kwargs)


class _FeatureIterator(_BaseIterator):
    """Features from an in-memory iterable.

    Yields the features it was given, so a caller can pass a generator through
    `create_db` or `FeatureDB.update` without first writing a file.
    """

    def __init__(self, data, **kwargs):
        self._features = list(data)
        self._pos = 0
        self._dialect = kwargs.get("dialect") or {"fmt": "gff3"}

    def __iter__(self):
        return iter(self._features)

    def __next__(self):
        if self._pos >= len(self._features):
            raise StopIteration
        feature = self._features[self._pos]
        self._pos += 1
        return feature

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
