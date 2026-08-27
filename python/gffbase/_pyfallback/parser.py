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
"""Pure-Python streaming parser. Mirrors the Rust crate's behavior so it can
serve as a correctness oracle and as a fallback when the native extension is
unavailable.

Applies the same NCBI GFF3 validation rules as the Rust parser, with the same
`strict=True` raise / `strict=False` warning-collect semantics.
"""

from __future__ import annotations

import gzip
import io
import math
import unicodedata
from collections.abc import Iterator

from gffbase._pyfallback.attributes import parse_attributes
from gffbase.dialect import merge_dialects
from gffbase.feature import ParsedFeature


def _gff_format_error_class():
    """Resolve the canonical ``GFFFormatError`` lazily.

    Importing it at module load creates a circular import (``gffbase``'s
    ``__init__`` re-binds the Python class to the Rust class when the
    extension is built — but that re-binding hasn't happened yet at the
    time this submodule loads). Resolving it on first use lets callers
    catch the *same* class regardless of whether the Rust extension or
    the Python fallback raised.
    """
    try:
        from gffbase._native import GFFFormatError as _C

        return _C
    except Exception:  # pragma: no cover - only on a build without the extension
        from gffbase.exceptions import GFFFormatError as _C

        return _C


def _make_error(message: str, line_no: int, kind: str):
    cls = _gff_format_error_class()
    try:
        return cls(message, line_no=line_no, kind=kind)
    except TypeError:
        # The PyO3-derived class doesn't accept kwargs; build with the
        # positional message and attach the metadata as attributes
        # afterwards (matches what `gff_error_to_py` does in lib.rs).
        err = cls(message)
        try:
            err.line_no = line_no
            err.kind = kind
            err.message = message
        except (AttributeError, TypeError):  # pragma: no cover - defensive
            # A future PyO3 exception type might not accept attribute
            # assignment. The message is already correct; the structured
            # metadata is a nicety.
            pass
        return err


GFFFormatError = _gff_format_error_class()  # resolve eagerly enough for raise sites below


def _open(path: str):
    if path.endswith(".gz"):
        return gzip.open(path, "rb")
    return open(path, "rb")


def _decode_error(err: UnicodeDecodeError, line_no: int):
    """Turn a raw `UnicodeDecodeError` into the parser's own error type.

    The Rust engine reports invalid UTF-8 as a `GFFFormatError` naming the
    line. Letting the fallback surface Python's own exception instead made the
    two engines distinguishable, and cost the reader the line number -- the
    only part of the message that helps you find the byte.
    """
    return _make_error(
        f"line {line_no}: line is not valid UTF-8",
        line_no,
        "InvalidAttribute",
    )


def _iter_lines(stream) -> Iterator[str | UnicodeDecodeError]:
    # Every read in the fallback funnels through here, which is why the decode
    # guard lives here rather than at each call site. Text-mode decoding is
    # lazy, so the `UnicodeDecodeError` surfaces on iteration, not on open().
    while True:
        try:
            line = next(stream)
        except StopIteration:
            return
        if isinstance(line, bytes):
            try:
                line = line.decode("utf-8", errors="strict")
            except UnicodeDecodeError as err:
                # Yield the error rather than raising it here. The outer parser
                # owns the physical line counter and the strict/warn policy,
                # and a binary line stream remains usable after one bad line.
                yield err
                continue
        if line.endswith("\n"):
            line = line[:-1]
        if line.endswith("\r"):
            line = line[:-1]
        yield line


def _validate(
    *,
    line_no: int,
    seqid: str,
    featuretype: str,
    start: int | None,
    end: int | None,
    score: str,
    strand: str,
    frame: str,
    n_pairs: int,
    blob: str,
    is_gtf: bool,
) -> list[Exception]:
    """Mirror of `validate.rs::validate_fields` + `validate_attributes_pairs`."""
    errors: list[Exception] = []
    if not seqid:
        errors.append(
            _make_error(
                f"line {line_no}: seqid (col 1) is empty",
                line_no,
                "EmptySeqid",
            )
        )
    if not featuretype:
        errors.append(
            _make_error(
                f"line {line_no}: featuretype (col 3) is empty",
                line_no,
                "EmptyFeaturetype",
            )
        )
    if any(_is_token_whitespace_or_control(ch) for ch in featuretype):
        errors.append(
            _make_error(
                f"line {line_no}: featuretype contains whitespace: {featuretype!r}",
                line_no,
                "InvalidFeaturetype",
            )
        )
    if start is None:
        errors.append(
            _make_error(
                f"line {line_no}: start coordinate must be a positive integer; got '.'",
                line_no,
                "InvalidCoordinate",
            )
        )
    elif start < 1:
        errors.append(
            _make_error(
                f"line {line_no}: start coordinate must be >= 1 (got {start})",
                line_no,
                "InvalidCoordinate",
            )
        )
    if end is None:
        errors.append(
            _make_error(
                f"line {line_no}: end coordinate must be a positive integer; got '.'",
                line_no,
                "InvalidCoordinate",
            )
        )
    elif end < 1:
        errors.append(
            _make_error(
                f"line {line_no}: end coordinate must be >= 1 (got {end})",
                line_no,
                "InvalidCoordinate",
            )
        )
    elif start is not None and start > 0 and end < start:
        errors.append(
            _make_error(
                f"line {line_no}: end < start ({end} < {start})",
                line_no,
                "InvalidCoordinate",
            )
        )
    if strand not in ("+", "-", "?", "."):
        errors.append(
            _make_error(
                f"line {line_no}: strand must be one of '+', '-', '?', '.'; got {strand!r}",
                line_no,
                "InvalidStrand",
            )
        )
    if frame not in (".", "0", "1", "2"):
        errors.append(
            _make_error(
                f"line {line_no}: phase must be 0, 1, 2, or '.'; got {frame!r}",
                line_no,
                "InvalidPhase",
            )
        )
    if featuretype == "CDS" and frame == ".":
        errors.append(
            _make_error(
                f"line {line_no}: CDS row missing required phase (must be 0, 1, or 2)",
                line_no,
                "InvalidPhase",
            )
        )
    if score not in ("", "."):
        if score != score.strip():
            errors.append(
                _make_error(
                    f"line {line_no}: score must not contain surrounding whitespace; got {score!r}",
                    line_no,
                    "InvalidScore",
                )
            )
        else:
            try:
                parsed_score = float(score)
            except ValueError:
                errors.append(
                    _make_error(
                        f"line {line_no}: score must be a float or '.'; got {score!r}",
                        line_no,
                        "InvalidScore",
                    )
                )
            else:
                if not math.isfinite(parsed_score):
                    errors.append(
                        _make_error(
                            f"line {line_no}: score must be finite or '.'; got {score!r}",
                            line_no,
                            "InvalidScore",
                        )
                    )
    attr_message = _validate_attribute_syntax(blob, n_pairs=n_pairs, is_gtf=is_gtf)
    if attr_message is not None:
        errors.append(
            _make_error(
                f"line {line_no}: {attr_message}",
                line_no,
                "InvalidAttribute",
            )
        )
    return errors


def _attribute_segments(blob: str) -> tuple[list[str], bool]:
    """Split column 9 while tracking whether double quotes balance."""
    segments: list[str] = []
    start = 0
    in_quotes = False
    escaped = False
    for index, char in enumerate(blob):
        if escaped:
            escaped = False
            continue
        if char == "\\":
            escaped = True
            continue
        if char == '"' and not escaped:
            in_quotes = not in_quotes
        if char == ";" and not in_quotes:
            segments.append(blob[start:index])
            start = index + 1
    segments.append(blob[start:])
    return segments, not in_quotes


def _validate_percent_escapes(value: str) -> bool:
    index = 0
    while index < len(value):
        if value[index] != "%":
            index += 1
            continue
        if index + 2 >= len(value) or any(
            char not in "0123456789abcdefABCDEF" for char in value[index + 1 : index + 3]
        ):
            return False
        index += 3
    return True


def _valid_attribute_key(key: str) -> bool:
    return bool(key) and not any(
        _is_token_whitespace_or_control(char) or char in ';,=%&"' for char in key
    )


def _is_token_whitespace_or_control(char: str) -> bool:
    """Match Rust's explicit Unicode whitespace/control token predicate."""
    return char.isspace() or unicodedata.category(char) == "Cc"


def _is_gtf_separator_whitespace(char: str) -> bool:
    """Whitespace separates GTF fields; control characters never do."""
    return char.isspace() and unicodedata.category(char) != "Cc"


def _valid_gtf_quoted_value(value: str) -> bool:
    if len(value) < 2 or not value.startswith('"') or not value.endswith('"'):
        return False
    escaped = False
    for char in value[1:-1]:
        if char == '"' and not escaped:
            return False
        if char == "\\":
            escaped = not escaped
        else:
            escaped = False
    return True


def _validate_attribute_syntax(blob: str, *, n_pairs: int, is_gtf: bool) -> str | None:
    """Validate strict GFF3/GTF column-9 grammar without changing parsing."""
    trimmed = blob.strip()
    if not trimmed or trimmed == ".":
        return None

    segments, quotes_balanced = _attribute_segments(trimmed)
    if not quotes_balanced:
        return "attribute string contains an unbalanced double quote"

    # A single final semicolon is conventional in GTF and tolerated in GFF3.
    if segments and not segments[-1].strip():
        segments.pop()
    if not segments or any(not segment.strip() for segment in segments):
        return "attribute string contains an empty attribute"

    for raw_segment in segments:
        segment = raw_segment.strip()
        if is_gtf:
            split_at = next(
                (index for index, char in enumerate(segment) if _is_gtf_separator_whitespace(char)),
                -1,
            )
            if split_at <= 0:
                return f"GTF attribute is not a quoted key/value pair: {segment[:60]!r}"
            key = segment[:split_at]
            value = segment[split_at:].strip()
            if not _valid_attribute_key(key):
                return f"attribute key is empty or contains a reserved character: {key!r}"
            if not _valid_gtf_quoted_value(value):
                return f"GTF attribute value must be double-quoted: {segment[:60]!r}"
            continue

        if "=" not in segment:
            return f"GFF3 attribute is missing '=': {segment[:60]!r}"
        key, value = segment.split("=", 1)
        if not _valid_attribute_key(key):
            return f"attribute key is empty or contains a reserved character: {key!r}"
        if '"' in value:
            return f"GFF3 attribute value contains an unescaped quote: {segment[:60]!r}"
        if not _validate_percent_escapes(value):
            return f"GFF3 attribute contains an invalid percent escape: {segment[:60]!r}"

    if n_pairs == 0:
        return f"attribute string did not parse into any key/value pair: {trimmed[:60]!r}"
    return None


#: The range a DuckDB BIGINT can hold, which is where coordinates are stored.
_I64_MIN = -(2**63)
_I64_MAX = 2**63 - 1


def _coord_or_error(s: str, line_no: int, which: str) -> int | None:
    """Returns the int, or raises GFFFormatError with structured info."""
    if s == "." or s == "":
        return None
    try:
        value = int(s)
    except ValueError as err:
        raise _make_error(
            f"line {line_no}: {which} coordinate is not an integer: {s!r}",
            line_no,
            "InvalidCoordinate",
        ) from err
    # Python ints are unbounded; the column they land in is a DuckDB BIGINT.
    # Without this the fallback accepted a 20-digit coordinate that the Rust
    # engine rejects, and deferred the failure to INSERT time -- a confusing
    # error, far from the line that caused it, and only on one engine.
    if not (_I64_MIN <= value <= _I64_MAX):
        raise _make_error(
            f"line {line_no}: {which} coordinate is not an integer: {s!r}",
            line_no,
            "InvalidCoordinate",
        )
    return value


def _parse_line_into_feature(line: str, line_no: int, profile: str = "ncbi"):
    """Parse one line into a ParsedFeature.

    Returns ``(feature, violations)``. Under the ``ncbi`` profile the first
    violation is raised; under ``gffutils`` every violation is returned with the
    feature, because the compatibility contract is to keep the record and
    annotate it. Mirrors `parser.rs` exactly -- the two engines are diffed
    against each other by `test_engine_equivalence`.
    """
    rejects = profile == "ncbi"
    violations: list[Exception] = []
    fields = line.split("\t")
    if len(fields) < 9:
        violation = _make_error(
            f"line {line_no}: expected at least 9 tab-separated fields, found {len(fields)}",
            line_no,
            "TooFewFields",
        )
        if rejects:
            raise violation
        violations.append(violation)
        # Compat: gffutils never errors here. `feature_from_line` splits on
        # tab and `zip(_gffkeys, fields)` truncates, so the missing columns
        # take their defaults and a space-delimited line becomes one feature
        # whose seqid is the whole line.
        while len(fields) < 9:
            fields.append("" if len(fields) == 8 else ".")
    elif len(fields) > 9:
        violation = _make_error(
            f"line {line_no}: expected exactly 9 tab-separated fields, found {len(fields)}",
            line_no,
            "TooManyFields",
        )
        if rejects:
            raise violation
        violations.append(violation)
    seqid, source, featuretype, start_s, end_s, score, strand, frame = fields[:8]
    blob = fields[8]
    extra = fields[9:]
    pairs, obs = parse_attributes(blob, compat_whole_value_quotes=not rejects)
    start = _coord_or_error(start_s, line_no, "start")
    end = _coord_or_error(end_s, line_no, "end")
    field_errors = _validate(
        line_no=line_no,
        seqid=seqid,
        featuretype=featuretype,
        start=start,
        end=end,
        score=score,
        strand=strand,
        frame=frame,
        n_pairs=len(pairs),
        blob=blob,
        is_gtf=obs["fmt"] == "gtf",
    )
    if field_errors:
        if rejects:
            raise field_errors[0]
        violations.extend(field_errors)
    return ParsedFeature(
        seqid=seqid,
        source=source,
        featuretype=featuretype,
        start=start,
        end=end,
        score=score,
        strand=strand,
        frame=frame,
        attributes_blob=blob.encode("utf-8"),
        attributes_pairs=pairs,
        extra=extra,
    ), violations


def _stream_features(
    stream,
    checklines: int,
    force_dialect_check: bool,
    force_gff: bool,
    strict: bool,
    warnings: list[dict],
    directives: list[str],
    profile: str = "ncbi",
):
    """Two-pass iteration: collect the first `checklines` features and their
    dialect observations, then continue streaming. Behaves identically when the
    file is shorter than `directives`. ``directives`` is mutated in place so
    callers can read it even if the file contains zero feature rows."""
    samples: list[dict] = []
    buffered: list[ParsedFeature] = []
    fasta_reached = False
    line_no = 0

    def _record(err) -> None:
        warnings.append(
            {
                "line_no": getattr(err, "line_no", 0),
                "kind": getattr(err, "kind", ""),
                "message": getattr(err, "message", str(err)),
            }
        )

    def _maybe_handle(err) -> bool:
        """Return True when the caller should skip the line, False when it
        should propagate (i.e. raise).

        Under the compat profile nothing propagates: a record that cannot be
        parsed at all is dropped with a warning rather than killing the load.
        """
        if strict and profile == "ncbi":
            return False
        warnings.append(
            {
                "line_no": getattr(err, "line_no", 0),
                "kind": getattr(err, "kind", ""),
                "message": getattr(err, "message", str(err)),
            }
        )
        return True

    for line in _iter_lines(stream):
        line_no += 1
        if isinstance(line, UnicodeDecodeError):
            err = _decode_error(line, line_no)
            if _maybe_handle(err):
                continue
            raise err from line
        if not line:
            continue
        if line.startswith("##"):
            if line.startswith("##FASTA"):
                fasta_reached = True
                break
            # Strip the leading `##`, matching gffutils' `_directive_handler`.
            # `db.directives` is a documented attribute, so the stored form is
            # part of the compatibility contract.
            directives.append(line[2:])
            continue
        if line.startswith("#"):
            continue
        if line.startswith(">"):
            # A bare `>` starts an embedded FASTA section even without a
            # preceding `##FASTA` directive; gffutils stops at either.
            fasta_reached = True
            break
        try:
            feat, violations = _parse_line_into_feature(line, line_no, profile)
        except _gff_format_error_class() as e:
            if _maybe_handle(e):
                continue
            raise
        for violation in violations:
            _record(violation)
        _, obs = parse_attributes(
            feat.attributes_blob.decode("utf-8", errors="replace"),
            compat_whole_value_quotes=profile == "gffutils",
        )
        samples.append(obs)
        buffered.append(feat)
        if not force_dialect_check and len(buffered) >= checklines:
            break

    dialect = merge_dialects(samples)
    if force_gff:
        dialect["fmt"] = "gff3"
        dialect["keyval separator"] = "="

    for f in buffered:
        yield f, directives, dialect

    if fasta_reached:
        return

    for line in _iter_lines(stream):
        line_no += 1
        if isinstance(line, UnicodeDecodeError):
            err = _decode_error(line, line_no)
            if _maybe_handle(err):
                continue
            raise err from line
        if not line:
            continue
        if line.startswith("##"):
            if line.startswith("##FASTA"):
                return
            # Strip the leading `##`, matching gffutils' `_directive_handler`.
            # `db.directives` is a documented attribute, so the stored form is
            # part of the compatibility contract.
            directives.append(line[2:])
            continue
        if line.startswith("#"):
            continue
        if line.startswith(">"):
            # See above: a bare `>` ends the feature section.
            return
        try:
            feat, violations = _parse_line_into_feature(line, line_no, profile)
        except _gff_format_error_class() as e:
            if _maybe_handle(e):
                continue
            raise
        for violation in violations:
            _record(violation)
        yield feat, directives, dialect


class _FallbackIterator:
    """Mirrors the Rust iterator's surface: __iter__/__next__, dialect(),
    directives(). Backwards-compat callers expect both forms.

    Also exposes ``warnings`` (a list of dicts populated when
    ``strict=False``).
    """

    def __init__(
        self,
        stream,
        checklines: int,
        force_dialect_check: bool,
        force_gff: bool,
        strict: bool = True,
        validation: str = "ncbi",
    ):
        self._warnings: list[dict] = []
        self._directives: list[str] = []
        self._gen = _stream_features(
            stream,
            checklines,
            force_dialect_check,
            force_gff,
            strict=strict,
            warnings=self._warnings,
            directives=self._directives,
            profile=validation,
        )
        self._dialect: dict | None = None
        self._exhausted = False

    def __iter__(self):
        return self

    def __next__(self) -> ParsedFeature:
        feat, directives, dialect = next(self._gen)
        self._dialect = dialect
        return feat

    def _drain_for_metadata(self) -> None:
        """Force the generator to its first yield (or to exhaustion) so
        that ``directives`` and ``dialect`` are populated even on
        directives-only / empty inputs. Idempotent."""
        if self._exhausted:
            return
        try:
            # The generator runs the dialect-peek phase before its first
            # yield, populating directives + dialect along the way. We
            # drive it just enough to reach that point.
            item = next(self._gen)
            # The generator yields `(feature, directives, dialect)`, and the
            # dialect has to be captured HERE as well as in `__next__`.
            # Draining without capturing left `.dialect()` returning `{}`
            # until someone happened to iterate -- so the same call gave a
            # populated dialect or an empty one depending on nothing the
            # caller could see. `directives` was fine because the generator
            # appends into a list this object already owns.
            _feat, _directives, dialect = item
            self._dialect = dialect
            # Restore: stash the first record so __next__ still sees it.
            self._gen = self._chain([item], self._gen)
        except StopIteration:
            self._exhausted = True
            if self._dialect is None:
                # A file with directives but no features never reaches a
                # yield, so nothing ever handed us a dialect. Returning `{}`
                # made `.dialect()["fmt"]` a KeyError on exactly the inputs
                # where a caller is most likely to be probing before deciding
                # what to do. The Rust engine reports the default here, so
                # report the same thing.
                from gffbase.dialect import default_dialect

                self._dialect = default_dialect()

    @staticmethod
    def _chain(prefix, suffix):
        for item in prefix:
            yield item
        for item in suffix:
            yield item

    def dialect(self) -> dict:
        if self._dialect is None:
            self._drain_for_metadata()
        return self._dialect or {}

    def directives(self) -> list[str]:
        if not self._directives:
            self._drain_for_metadata()
        return list(self._directives)

    @property
    def warnings(self) -> list[dict]:
        return list(self._warnings)


def parse_file(
    path: str,
    checklines: int = 10,
    force_dialect_check: bool = False,
    force_gff: bool = False,
    strict: bool = True,
    validation: str = "ncbi",
) -> _FallbackIterator:
    stream = _open(path)
    return _FallbackIterator(stream, checklines, force_dialect_check, force_gff, strict, validation)


def parse_bytes(
    data: bytes,
    checklines: int = 10,
    force_dialect_check: bool = False,
    force_gff: bool = False,
    strict: bool = True,
    validation: str = "ncbi",
) -> _FallbackIterator:
    # Keep the stream binary and decode one physical line at a time in
    # `_iter_lines`. That preserves line-aware diagnostics and lets
    # `strict=False` skip one invalid line and continue with the next.
    stream = io.BytesIO(data)
    return _FallbackIterator(stream, checklines, force_dialect_check, force_gff, strict, validation)


def detect_dialect(path: str, checklines: int = 10) -> dict:
    # Dialect detection is non-strict by design, and uses the permissive
    # profile so a malformed line in the sample cannot poison detection.
    it = parse_file(path, checklines=checklines, strict=False, validation="gffutils")
    drained: list[ParsedFeature] = []
    try:
        for _ in range(checklines):
            drained.append(next(it))
    except StopIteration:
        pass
    return it.dialect()
