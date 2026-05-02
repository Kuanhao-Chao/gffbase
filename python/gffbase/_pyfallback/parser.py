"""Pure-Python streaming parser. Mirrors the Rust crate's behavior so it can
serve as a correctness oracle and as a fallback when the native extension is
unavailable.
"""

from __future__ import annotations

import gzip
import io
from typing import Iterator, List, Optional

from gffbase.dialect import merge_dialects
from gffbase.feature import ParsedFeature
from gffbase._pyfallback.attributes import parse_attributes


def _open(path: str):
    if path.endswith(".gz"):
        return gzip.open(path, "rt", encoding="utf-8", newline="")
    return open(path, "r", encoding="utf-8", newline="")


def _iter_lines(stream) -> Iterator[str]:
    for line in stream:
        if line.endswith("\n"):
            line = line[:-1]
        if line.endswith("\r"):
            line = line[:-1]
        yield line


def _parse_line_into_feature(line: str, line_no: int) -> ParsedFeature:
    fields = line.split("\t")
    if len(fields) < 9:
        raise ValueError(
            f"line {line_no}: expected at least 9 tab-separated fields, found {len(fields)}"
        )
    seqid, source, featuretype, start_s, end_s, score, strand, frame = fields[:8]
    blob = fields[8]
    extra = fields[9:]
    pairs, _obs = parse_attributes(blob)
    return ParsedFeature(
        seqid=seqid,
        source=source,
        featuretype=featuretype,
        start=_parse_coord(start_s),
        end=_parse_coord(end_s),
        score=score,
        strand=strand,
        frame=frame,
        attributes_blob=blob.encode("utf-8"),
        attributes_pairs=pairs,
        extra=extra,
    )


def _parse_coord(s: str) -> Optional[int]:
    if s == "." or s == "":
        return None
    return int(s)


def _stream_features(stream, checklines: int, force_dialect_check: bool, force_gff: bool):
    """Two-pass iteration: collect the first `checklines` features and their
    dialect observations, then continue streaming. Behaves identically when the
    file is shorter than `checklines`."""
    directives: List[str] = []
    samples: List[dict] = []
    buffered: List[ParsedFeature] = []
    fasta_reached = False
    line_no = 0

    for line in _iter_lines(stream):
        line_no += 1
        if not line:
            continue
        if line.startswith("##"):
            if line.startswith("##FASTA"):
                fasta_reached = True
                break
            directives.append(line)
            continue
        if line.startswith("#"):
            continue
        feat = _parse_line_into_feature(line, line_no)
        _, obs = parse_attributes(feat.attributes_blob.decode("utf-8", errors="replace"))
        samples.append(obs)
        buffered.append(feat)
        if not force_dialect_check and len(buffered) >= checklines:
            break

    dialect = merge_dialects(samples)
    if force_gff:
        dialect["fmt"] = "gff3"
        dialect["keyval separator"] = "="

    # If forced full-pass, we keep collecting samples but we already have all
    # buffered features. Yield them, then continue streaming the rest.
    for f in buffered:
        yield f, directives, dialect

    if fasta_reached:
        return

    # Continue streaming the remainder.
    for line in _iter_lines(stream):
        line_no += 1
        if not line:
            continue
        if line.startswith("##"):
            if line.startswith("##FASTA"):
                return
            directives.append(line)
            continue
        if line.startswith("#"):
            continue
        feat = _parse_line_into_feature(line, line_no)
        yield feat, directives, dialect


class _FallbackIterator:
    """Mirrors the Rust iterator's surface: __iter__/__next__, dialect(),
    directives(). Backwards-compat callers expect both forms."""

    def __init__(self, stream, checklines: int, force_dialect_check: bool, force_gff: bool):
        self._gen = _stream_features(stream, checklines, force_dialect_check, force_gff)
        self._dialect = None
        self._directives: List[str] = []

    def __iter__(self):
        return self

    def __next__(self) -> ParsedFeature:
        feat, directives, dialect = next(self._gen)
        self._directives = directives
        self._dialect = dialect
        return feat

    def dialect(self) -> dict:
        return self._dialect or {}

    def directives(self) -> List[str]:
        return list(self._directives)


def parse_file(
    path: str,
    checklines: int = 10,
    force_dialect_check: bool = False,
    force_gff: bool = False,
) -> _FallbackIterator:
    stream = _open(path)
    return _FallbackIterator(stream, checklines, force_dialect_check, force_gff)


def parse_bytes(
    data: bytes,
    checklines: int = 10,
    force_dialect_check: bool = False,
    force_gff: bool = False,
) -> _FallbackIterator:
    stream = io.StringIO(data.decode("utf-8", errors="replace"))
    return _FallbackIterator(stream, checklines, force_dialect_check, force_gff)


def detect_dialect(path: str, checklines: int = 10) -> dict:
    it = parse_file(path, checklines=checklines)
    # Drive the iterator through the buffered prefix to populate the dialect.
    drained: List[ParsedFeature] = []
    try:
        for _ in range(checklines):
            drained.append(next(it))
    except StopIteration:
        pass
    return it.dialect()
