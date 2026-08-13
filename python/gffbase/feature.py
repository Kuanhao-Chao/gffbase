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
"""Feature objects.

This module hosts two distinct types:

* ``ParsedFeature`` — slotted dataclass emitted by the parser.
* ``Feature`` — full backward-compatible public class, mirroring legacy
  ``gffutils.Feature``. It supports lazy attribute parsing so that DB rows
  loaded but never inspected don't pay the cost of decoding col-9.
"""

from __future__ import annotations

import json
import sys
from collections.abc import Iterator, Mapping, MutableMapping
from dataclasses import dataclass, field
from typing import ClassVar

# `slots=True` landed in Python 3.10. `ParsedFeature` is instantiated once per
# parsed line — millions of times on a GENCODE-scale file — so the per-instance
# memory saving is worth keeping wherever it is available. On 3.9 we fall back
# to a plain dataclass rather than failing at import time.
_SLOTS = {"slots": True} if sys.version_info >= (3, 10) else {}


@dataclass(**_SLOTS)
class ParsedFeature:
    seqid: str
    source: str
    featuretype: str
    start: int | None
    end: int | None
    score: str
    strand: str
    frame: str
    # Raw col-9 bytes preserved for byte-faithful round-trip in Phase 4.
    attributes_blob: bytes
    # Long-form (key, value, multivalue_index) triples. `idx` preserves
    # the order of multi-valued attributes (e.g., Parent=a,b,c becomes
    # three rows with idx 0,1,2).
    attributes_pairs: list[tuple[str, str, int]] = field(default_factory=list)
    # Tab-separated columns past column 9, if any.
    extra: list[str] = field(default_factory=list)

    @property
    def chrom(self) -> str:
        return self.seqid

    @property
    def stop(self) -> int | None:
        return self.end

    def attributes_dict(self) -> dict:
        """Materialize attributes as `{key: [values...]}`. Preserves first-seen
        key order and multi-value ordering. Defers to `attributes_pairs` so the
        Rust and Python parsers remain trivially comparable."""
        out: dict = {}
        for k, v, _idx in self.attributes_pairs:
            out.setdefault(k, []).append(v)
        _drop_lone_empty_values(out)
        return out

    @classmethod
    def from_tuple(cls, tup) -> ParsedFeature:
        """Build from the 11-tuple shape that the Rust extension yields."""
        (
            seqid,
            source,
            featuretype,
            start,
            end,
            score,
            strand,
            frame,
            blob,
            pairs,
            extra,
        ) = tup
        return cls(
            seqid=seqid,
            source=source,
            featuretype=featuretype,
            start=start,
            end=end,
            score=score,
            strand=strand,
            frame=frame,
            attributes_blob=bytes(blob) if not isinstance(blob, bytes) else blob,
            attributes_pairs=[(k, v, int(i)) for (k, v, i) in pairs],
            extra=list(extra),
        )


# ---------------------------------------------------------------------------
# Public ``Feature`` class — backward-compatible with legacy ``gffutils.Feature``.
# ---------------------------------------------------------------------------


# Field order for integer-indexed __getitem__ / __setitem__ (legacy semantics).
_FIELD_ORDER = (
    "seqid",
    "source",
    "featuretype",
    "start",
    "end",
    "score",
    "strand",
    "frame",
    "attributes",
)


def _coord_to_int(v) -> int | None:
    """Normalize a coordinate. Legacy accepts int, '.', '', or None."""
    if v is None or v == "" or v == ".":
        return None
    if isinstance(v, int):
        return v
    return int(v)


def _drop_lone_empty_values(mapping: dict) -> None:
    """Normalize `{key: [""]}` to `{key: []}`, in place.

    gffutils' rule, which user code depends on: a wholly empty value string
    (`ID=`) yields the key with NO values, while a multi-valued attribute keeps
    its empty parts (`Parent=x,` -> `["x", ""]`). It matters because callers
    write `if f.attributes["ID"]:`, and `[""]` is truthy where `[]` is not. The
    raw col-9 bytes are untouched, so serialization still round-trips `ID=`.
    """
    for key, values in mapping.items():
        if values == [""]:
            mapping[key] = []


class _LazyAttributes(MutableMapping):
    """Dict-like attribute store. Values are always lists.

    If constructed with a raw col-9 ``blob``, parsing is deferred until first
    access. This honors the Phase 2 §3.3 invariant: attributes never decoded
    unless someone reads them.
    """

    __slots__ = ("_d", "_blob", "_dialect_fmt", "_parsed")

    def __init__(
        self,
        initial: Mapping | list[tuple[str, str, int]] | None = None,
        blob: bytes | None = None,
        dialect_fmt: str = "gff3",
    ):
        self._d: dict = {}
        self._blob = blob
        self._dialect_fmt = dialect_fmt
        self._parsed = False
        if initial is not None:
            self._ingest(initial)
            self._parsed = True
            self._blob = None

    # ----- internal -----

    def _ingest(self, initial):
        if isinstance(initial, _LazyAttributes):
            initial._materialize()
            for k, v in initial._d.items():
                self._d[k] = list(v)
            return
        if isinstance(initial, Mapping):
            for k, v in initial.items():
                self._d[k] = v if isinstance(v, list) else [v]
            return
        if isinstance(initial, list):  # list of (key, value, idx)
            for triple in initial:
                if len(triple) == 3:
                    k, v, _idx = triple
                else:
                    k, v = triple
                self._d.setdefault(k, []).append(v)
            _drop_lone_empty_values(self._d)
            return
        raise TypeError(f"unsupported attributes init type: {type(initial)!r}")

    def _materialize(self):
        if self._parsed:
            return
        if self._blob is None or self._blob == b"":
            self._parsed = True
            return
        # Defer to the pure-Python attribute parser to avoid a Rust hop on a
        # single line. This is the warm path for users who *do* read attrs.
        from gffbase._pyfallback.attributes import parse_attributes

        text = self._blob.decode("utf-8", errors="replace")
        pairs, _obs = parse_attributes(text)
        for k, v, _idx in pairs:
            self._d.setdefault(k, []).append(v)
        _drop_lone_empty_values(self._d)
        self._parsed = True
        self._blob = None

    # ----- MutableMapping -----

    def __getitem__(self, key: str):
        self._materialize()
        return self._d[key]

    def __setitem__(self, key: str, value):
        self._materialize()
        self._d[key] = value if isinstance(value, list) else [value]

    def __delitem__(self, key: str):
        self._materialize()
        del self._d[key]

    def __iter__(self) -> Iterator[str]:
        self._materialize()
        return iter(self._d)

    def __len__(self) -> int:
        self._materialize()
        return len(self._d)

    def __contains__(self, key) -> bool:
        self._materialize()
        return key in self._d

    def __repr__(self) -> str:
        self._materialize()
        return f"Attributes({self._d!r})"

    # ----- helpers -----

    def items(self):
        self._materialize()
        return self._d.items()

    def keys(self):
        self._materialize()
        return self._d.keys()

    def values(self):
        self._materialize()
        return self._d.values()


class Feature:
    """Backward-compatible public Feature object.

    Mirrors the legacy ``gffutils.Feature`` constructor and observable
    behavior: 1-based inclusive coordinates, list-wrapped multi-value
    attributes, dialect-faithful ``__str__`` round-trip.
    """

    #: False for every ordinary feature. `MultipartFeature` overrides it.
    #: A ClassVar rather than an instance attribute so the singleton case --
    #: which is every feature in almost every file -- costs nothing per object.
    is_multipart: ClassVar[bool] = False

    #: How many physical input lines this feature was built from.
    #:
    #: A class attribute, so an ordinary feature carries no per-instance cost
    #: for it -- but deliberately NOT a `ClassVar`, because `MultipartFeature`
    #: shadows it with a real slot and assigns per instance. Declaring it
    #: `ClassVar` would make that assignment a type error.
    n_segments: int = 1

    __slots__ = (
        "seqid",
        "source",
        "featuretype",
        "start",
        "end",
        "score",
        "strand",
        "frame",
        "attributes",
        "extra",
        "bin",
        "id",
        "dialect",
        "file_order",
        "keep_order",
        "sort_attribute_values",
        "_attributes_blob",
        "children",  # populated by FeatureDB.merge to expose component features
    )

    def __init__(
        self,
        seqid: str = ".",
        source: str = ".",
        featuretype: str = ".",
        start=".",
        end=".",
        score: str = ".",
        strand: str = ".",
        frame: str = ".",
        attributes=None,
        extra=None,
        bin: int | None = None,
        id: str | None = None,
        dialect: dict | None = None,
        file_order: int | None = None,
        keep_order: bool = False,
        sort_attribute_values: bool = False,
    ):
        self.seqid = seqid
        self.source = source
        self.featuretype = featuretype
        self.start = _coord_to_int(start)
        self.end = _coord_to_int(end)
        self.score = score if score is not None else "."
        self.strand = strand if strand is not None else "."
        self.frame = frame if frame is not None else "."
        self.bin = bin
        self.id = id
        self.dialect = dialect or {}
        self.file_order = file_order
        self.keep_order = keep_order
        self.sort_attribute_values = sort_attribute_values
        self._attributes_blob = None
        # Transient: populated by FeatureDB.merge() to expose the component
        # features that were merged into this one. Not persisted.
        self.children: list[Feature] | None = None

        fmt = (self.dialect or {}).get("fmt", "gff3")
        if isinstance(attributes, _LazyAttributes):
            self.attributes = attributes
        elif isinstance(attributes, (bytes, bytearray)):
            self._attributes_blob = bytes(attributes)
            self.attributes = _LazyAttributes(blob=self._attributes_blob, dialect_fmt=fmt)
        elif attributes is None:
            self.attributes = _LazyAttributes(initial={}, dialect_fmt=fmt)
        else:
            self.attributes = _LazyAttributes(initial=attributes, dialect_fmt=fmt)

        if extra is None:
            self.extra = []
        elif isinstance(extra, (bytes, bytearray)):
            text = extra.decode("utf-8", errors="replace")
            self.extra = text.split("\t") if text else []
        elif isinstance(extra, str):
            self.extra = extra.split("\t") if extra else []
        else:
            self.extra = list(extra)

    # ----- aliases -----

    @property
    def chrom(self) -> str:
        return self.seqid

    @chrom.setter
    def chrom(self, v: str):
        self.seqid = v

    @property
    def stop(self) -> int | None:
        return self.end

    @stop.setter
    def stop(self, v):
        self.end = _coord_to_int(v)

    # ----- dunders -----

    def __len__(self) -> int:
        if self.start is None or self.end is None:
            return 0
        return self.end - self.start + 1

    def __repr__(self) -> str:
        return (
            f"<Feature {self.featuretype} ({self.seqid}:{self.start}-{self.end}"
            f"[{self.strand}]) at {hex(id(self))}>"
        )

    def __str__(self) -> str:
        return self._format_line()

    def __unicode__(self) -> str:
        return self.__str__()

    def __hash__(self) -> int:
        return hash(self._format_line())

    def __eq__(self, other) -> bool:
        if not isinstance(other, Feature):
            return NotImplemented
        return self._format_line() == other._format_line()

    def __ne__(self, other) -> bool:
        eq = self.__eq__(other)
        if eq is NotImplemented:
            return eq
        return not eq

    def __getitem__(self, key):
        if isinstance(key, int):
            return getattr(self, _FIELD_ORDER[key])
        return self.attributes[key]

    def __setitem__(self, key, value):
        if isinstance(key, int):
            setattr(self, _FIELD_ORDER[key], value)
        else:
            self.attributes[key] = value

    # ----- formatting -----

    def _format_attributes(self, normalized: bool = False) -> str:
        # If we have the original col-9 bytes and the user hasn't materialized
        # / mutated the attributes mapping, re-emit them verbatim. This is the
        # byte-faithful round-trip path.
        blob = self._attributes_blob
        blob_is_authoritative = (
            blob is not None
            and isinstance(self.attributes, _LazyAttributes)
            and not self.attributes._parsed
        )
        if blob is not None and blob_is_authoritative and not normalized:
            return blob.decode("utf-8", errors="replace")

        if blob is not None and blob_is_authoritative:
            # `normalized=True` re-renders from the parsed pairs -- but reading
            # `self.attributes` would materialize the lazy mapping, and that
            # permanently invalidates the blob fast-path above. Asking for the
            # normalized form once would then silently change what `str()`
            # returns forever after. Parse into a throwaway instead.
            scratch = _LazyAttributes(
                blob=blob,
                dialect_fmt=(self.dialect or {}).get("fmt", "gff3"),
            )
            source = scratch
        else:
            source = self.attributes

        fmt = (self.dialect or {}).get("fmt", "gff3")
        sep = "; " if (self.dialect or {}).get("field separator") == "; " else ";"
        kv_sep = (self.dialect or {}).get("keyval separator") or ("=" if fmt == "gff3" else " ")
        multival = (self.dialect or {}).get("multival separator", ",")
        items = list(source.items())
        if self.sort_attribute_values:
            items = [(k, sorted(v)) for k, v in items]

        parts = []
        for k, vs in items:
            vs_list = vs if isinstance(vs, list) else [vs]
            if fmt == "gff3":
                joined = multival.join(str(v) for v in vs_list)
                parts.append(f"{k}{kv_sep}{joined}")
            else:
                # GTF: typically `key "value"; key "value";`
                quoted = (self.dialect or {}).get("quoted GFF2 values", True)
                for v in vs_list:
                    if quoted:
                        parts.append(f'{k}{kv_sep}"{v}"')
                    else:
                        parts.append(f"{k}{kv_sep}{v}")
        s = sep.join(parts)
        if (self.dialect or {}).get("trailing semicolon") and not s.endswith(";"):
            s = s + ";"
        if (self.dialect or {}).get("leading semicolon"):
            s = ";" + s
        return s

    def _format_line(self, normalized: bool = False) -> str:
        start_s = "." if self.start is None else str(self.start)
        end_s = "." if self.end is None else str(self.end)
        cols = [
            self.seqid,
            self.source,
            self.featuretype,
            start_s,
            end_s,
            self.score,
            self.strand,
            self.frame,
            self._format_attributes(normalized),
        ]
        cols.extend(self.extra)
        return "\t".join(cols)

    # ----- segments -----

    @property
    def segments(self) -> tuple[FeatureSegment, ...]:
        """This feature's physical input lines.

        An ordinary feature is its own sole segment, so callers can write
        ``for seg in feature.segments`` without first asking whether the
        feature is discontinuous.
        """
        return (_segment_from(self, 0),)

    def to_line(self, normalized: bool = False) -> str:
        """Render this feature as one GFF line.

        By default this is byte-faithful: if the original column 9 was never
        parsed or mutated, its bytes are re-emitted verbatim, so a file that
        round-trips through gffbase comes back unchanged. That is what
        ``str(feature)`` does too.

        ``normalized=True`` instead re-renders column 9 from the parsed
        attribute mapping, applying the dialect's separators and the
        ``sort_attribute_values`` setting. This is what gffutils always does,
        so it is the form to use when comparing against the oracle -- at the
        cost of losing whatever the source file's exact spacing was.

        (One known gap: gffbase has no percent-encoding path yet, so a value
        containing a reserved character is not re-escaped on the normalized
        path. Tracked as a deviation against `gffutils.parser.Quoter`.)
        """
        return self._format_line(normalized)

    def to_lines(self, normalized: bool = False) -> list[str]:
        """Every physical line of this feature. One, unless it is multipart."""
        return [self.to_line(normalized)]

    # ----- legacy methods -----

    def astuple(self, encoding=None):
        """Legacy 12-tuple shape used by the SQLite export path:
        ``(id, seqid, source, featuretype, start, end, score, strand, frame,
            attributes_json, extra_json, bin)``.
        """
        attrs_dict = {k: list(v) for k, v in self.attributes.items()}
        return (
            self.id,
            self.seqid,
            self.source,
            self.featuretype,
            self.start,
            self.end,
            self.score,
            self.strand,
            self.frame,
            json.dumps(attrs_dict, separators=(",", ":")),
            json.dumps(self.extra, separators=(",", ":")) if self.extra else "[]",
            self.bin if self.bin is not None else self.calc_bin(),
        )

    def calc_bin(self, _bin=None) -> int | None:
        if _bin is not None:
            self.bin = _bin
            return _bin
        if self.start is None or self.end is None:
            return None
        from gffbase._bins import bin_from_coords

        self.bin = bin_from_coords(self.start, self.end)
        return self.bin

    def sequence(self, fasta, use_strand: bool = True) -> str:
        """Extract sequence from a FASTA path or a pyfaidx-style mapping."""
        if isinstance(fasta, str):  # pragma: no cover - pyfaidx is optional
            try:
                import pyfaidx
            except ImportError as e:
                raise ImportError(
                    "Feature.sequence(path=...) requires the optional `pyfaidx` package"
                ) from e
            fa = pyfaidx.Fasta(fasta)
        else:
            fa = fasta
        if self.start is None or self.end is None:
            raise ValueError(
                f"cannot extract sequence for {self.id!r}: feature has no start/end coordinates"
            )
        seq = str(fa[self.seqid][self.start - 1 : self.end])
        if use_strand and self.strand == "-":
            seq = _revcomp(seq)
        return seq


_COMPLEMENT = str.maketrans("ACGTNacgtn", "TGCANtgcan")


def _revcomp(seq: str) -> str:
    return seq.translate(_COMPLEMENT)[::-1]


# ---------------------------------------------------------------------------
# Discontinuous (multipart) features.
#
# GFF3 lets one logical feature span several lines sharing an `ID` -- how NCBI
# represents a split CDS. Both classes SUBCLASS `Feature`, and the reason is
# concrete rather than stylistic: `isinstance(x, Feature)` is load-bearing at
# nine call sites inside gffbase alone (`__getitem__`, `__contains__`,
# `children`, `parents`, `_coerce_ids`, `_coerce_id_list`,
# `_normalize_region_args`, `update`, `merge`), and `Feature.__eq__` returns
# `NotImplemented` for a non-`Feature` operand, so a sibling class would
# compare unequal to an otherwise identical `Feature`.
#
# Subclassing also lets both classes override NOTHING of `__str__`, `__len__`,
# `__hash__`, `__eq__`, `__getitem__`, `astuple`, `_format_line` or
# `_format_attributes`. The compatibility surface is preserved by inaction,
# which is the only way to preserve it reliably.
# ---------------------------------------------------------------------------


class FeatureSegment(Feature):
    """One physical input line of a discontinuous feature.

    Carries its OWN coordinates, score, phase and column 9 -- per-segment CDS
    phase is the main reason the storage exists -- while `seqid`, `source`,
    `featuretype` and `strand` come from the logical feature, which by
    definition shares them.

    ``self.id`` is the LOGICAL id, so `db[seg.id]` finds the whole feature.
    The segment's own ``ID=`` is preserved byte-for-byte in the attributes
    blob, so `str(segment)` reproduces the input line exactly.
    """

    __slots__ = ("seg_idx",)

    def __init__(self, *args, seg_idx: int = 0, **kwargs):
        super().__init__(*args, **kwargs)
        #: 0-based position in FILE order, not coordinate order. GFF3 does not
        #: require segments to be sorted, and `to_lines()` has to reproduce the
        #: input.
        self.seg_idx = seg_idx

    def __repr__(self) -> str:
        return (
            f"<FeatureSegment {self.featuretype}[{self.seg_idx}] "
            f"({self.seqid}:{self.start}-{self.end}[{self.strand}]) at {hex(id(self))}>"
        )


class MultipartFeature(Feature):
    """A logical feature assembled from more than one input line.

    Its own `start`/`end` are the ENVELOPE -- `MIN(segment.start)` and
    `MAX(segment.end)` -- and every inherited method operates on that envelope,
    so a caller that knows nothing about discontinuous features sees exactly
    the gffutils behaviour for a feature spanning that range.

    Consequently ``len(f)`` is the envelope span, matching `Feature`.
    `covered_length` is the different, new quantity.
    """

    # `n_segments` is a slot here, shadowing `Feature`'s class attribute. A
    # read-only property would not do: it cannot override a writeable
    # attribute, and a slot descriptor is cheaper to read anyway.
    __slots__ = ("n_segments", "_segments", "_segment_loader")

    is_multipart: ClassVar[bool] = True

    def __init__(self, *args, n_segments: int = 1, segments=None, segment_loader=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.n_segments = n_segments
        self._segments = tuple(segments) if segments is not None else None
        # Called with no arguments to fetch this feature's segments, for the
        # case where they were not prefetched. `_yield_features` prefetches one
        # chunk at a time so the common path never calls this -- without that,
        # iterating a multipart corpus would be an N+1.
        self._segment_loader = segment_loader

    @property
    def segments(self) -> tuple[FeatureSegment, ...]:
        if self._segments is None:
            if self._segment_loader is None:
                raise RuntimeError(
                    f"segments for {self.id!r} were neither prefetched nor loadable; "
                    "this feature was built without a database connection"
                )
            self._segments = tuple(self._segment_loader())
        return self._segments

    @property
    def covered_length(self) -> int:
        """Total length actually covered, with the gaps excluded.

        Differs from ``len(self)``, which is the envelope span. For a CDS split
        across two 100 bp exons 700 bp apart, `len` is 900 and this is 200.

        Segments of a discontinuous feature must not overlap; if a malformed
        file provides overlapping ones, the shared bases are counted twice.
        """
        return sum(len(seg) for seg in self.segments)

    def to_lines(self, normalized: bool = False) -> list[str]:
        """Every input line of this feature, in file order."""
        return [seg.to_line(normalized) for seg in self.segments]

    def __repr__(self) -> str:
        return (
            f"<MultipartFeature {self.featuretype} x{self.n_segments} "
            f"({self.seqid}:{self.start}-{self.end}[{self.strand}]) at {hex(id(self))}>"
        )


def _segment_from(feature: Feature, seg_idx: int) -> FeatureSegment:
    """View an ordinary `Feature` as its own sole segment."""
    seg = FeatureSegment(
        seqid=feature.seqid,
        source=feature.source,
        featuretype=feature.featuretype,
        start=feature.start,
        end=feature.end,
        score=feature.score,
        strand=feature.strand,
        frame=feature.frame,
        attributes=feature._attributes_blob
        if feature._attributes_blob is not None
        else feature.attributes,
        extra=list(feature.extra),
        id=feature.id,
        dialect=feature.dialect,
        file_order=feature.file_order,
        keep_order=feature.keep_order,
        sort_attribute_values=feature.sort_attribute_values,
        seg_idx=seg_idx,
    )
    return seg


# ---------------------------------------------------------------------------
# Construction from a DuckDB row.
# ---------------------------------------------------------------------------

#: The database columns `feature_from_row` consumes, in the order it unpacks
#: them. This is the SINGLE SOURCE OF TRUTH for the projection: `interface`
#: builds both its SQL column lists from it via `db_row_projection`.
#:
#: It used to be written out three times independently -- here, as
#: `interface._SELECT_FEATURE`, and again inside
#: `interface._select_feature_aliased`. Since `feature_from_row` unpacks
#: positionally, getting one of the three wrong did not raise; it silently
#: shifted every field by one.
_DB_ROW_FIELDS = (
    "id",
    "seqid",
    "source",
    "featuretype",
    "start",
    "end",
    "score",
    "strand",
    "frame",
    "attributes_blob",
    "extra_blob",
    "file_order",
)

#: Row fields that collide with SQL reserved words and need quoting.
_SQL_RESERVED_FIELDS = frozenset({"end"})


def db_row_projection(alias: str | None = None) -> str:
    """The SELECT column list that :func:`feature_from_row` expects.

    Pass ``alias`` to qualify each column for a joined query.
    """
    parts = []
    for name in _DB_ROW_FIELDS:
        column = f'"{name}"' if name in _SQL_RESERVED_FIELDS else name
        parts.append(f"{alias}.{column}" if alias else column)
    return ", ".join(parts)


#: Column order of the `segments` rows `feature_from_row` accepts, matching the
#: projection in `FeatureDB._prefetch_segments`.
_SEGMENT_ROW_FIELDS = (
    "feature_id",
    "seg_idx",
    "start",
    "end",
    "score",
    "frame",
    "attributes_blob",
    "extra_blob",
    "file_order",
)


def feature_from_row(
    row,
    dialect: dict | None = None,
    *,
    keep_order: bool = False,
    sort_attribute_values: bool = False,
    segments=None,
) -> Feature:
    """Build a ``Feature`` from a DuckDB row tuple. Lazy in attributes.

    `keep_order` and `sort_attribute_values` are database-wide settings that
    control how a feature renders itself, so they have to reach every feature
    the database hands out. They used to be stored on `FeatureDB` and never
    passed on, which made both options inert.

    Passing `segments` -- prefetched rows from the `segments` table -- produces
    a `MultipartFeature` instead. Presence of those rows IS the test: only a
    discontinuous feature has any, so no extra column is needed in the
    projection, which is also what keeps a v1 database readable.
    """
    (
        fid,
        seqid,
        source,
        featuretype,
        start,
        end,
        score,
        strand,
        frame,
        blob,
        extra_blob,
        file_order,
    ) = row
    score = score if score is not None else "."
    strand = strand if strand is not None else "."
    dialect = dialect or {"fmt": "gff3"}
    shared = {
        "seqid": seqid,
        "source": source,
        "featuretype": featuretype,
        "id": fid,
        "dialect": dialect,
        "keep_order": keep_order,
        "sort_attribute_values": sort_attribute_values,
    }
    if not segments:
        return Feature(
            start=start,
            end=end,
            score=score,
            strand=strand,
            frame=frame if frame is not None else ".",
            attributes=blob if blob is not None else None,
            extra=extra_blob if extra_blob else None,
            file_order=file_order,
            **shared,
        )

    # `seqid`, `source`, `featuretype` and `strand` are invariant across
    # segments -- the ingest predicate guarantees it -- so each segment takes
    # them from the logical row and supplies only its own coordinates, score,
    # phase and column 9. That is what makes `str(segment)` reproduce the
    # input line byte for byte.
    parts = [
        FeatureSegment(
            start=seg_start,
            end=seg_end,
            score=seg_score if seg_score is not None else ".",
            strand=strand,
            frame=seg_frame if seg_frame is not None else ".",
            attributes=bytes(seg_blob) if seg_blob is not None else None,
            extra=seg_extra if seg_extra else None,
            file_order=seg_order,
            seg_idx=seg_idx,
            **shared,
        )
        for (
            _fid,
            seg_idx,
            seg_start,
            seg_end,
            seg_score,
            seg_frame,
            seg_blob,
            seg_extra,
            seg_order,
        ) in segments
    ]
    return MultipartFeature(
        start=start,
        end=end,
        score=score,
        strand=strand,
        frame=frame if frame is not None else ".",
        attributes=blob if blob is not None else None,
        extra=extra_blob if extra_blob else None,
        file_order=file_order,
        n_segments=len(parts),
        segments=parts,
        **shared,
    )
