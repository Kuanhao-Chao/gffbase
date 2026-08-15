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
"""Column 9 serialization: percent-encoding and attribute-string reconstruction.

This is the inverse of `_pyfallback.attributes.parse_attributes` (and of the
Rust `unescape`). It lives in its own leaf module -- importing only
`gffbase.exceptions` -- because both `gffbase.feature` and `gffbase.parser`
need it and those two already form an import edge. `gffbase.parser` re-exports
everything here under the names the oracle uses.

Why an encoder has to exist at all
----------------------------------

Reading `feature.attributes` materializes decoded values and takes
serialization off the raw-bytes fast path. Without a re-encode step the
escaping is simply gone::

    Note=hello%20world       ->  Note=hello world
    pr_change=A%3B B         ->  pr_change=A; B      <- now TWO attributes

The second case is not cosmetic: the record stops being valid GFF3, and
nothing raises. Measured on `tests/data/upstream/nonascii`, one attribute
became five.
"""

from __future__ import annotations

import re

from gffbase.exceptions import AttributeStringError

# ---------------------------------------------------------------------------
# Percent-encoding
# ---------------------------------------------------------------------------

#: Characters that must be percent-encoded inside a GFF3 attribute VALUE.
#:
#: This is deliberately NOT `urllib.parse.quote`'s idea of unsafe. Two absences
#: are load-bearing and both are required to match gffutils:
#:
#: * **space is not encoded.** The GFF3 spec does not reserve it. Some files
#:   percent-encode it anyway, and those do not round-trip -- the `%20` decodes
#:   to a space on read and is not restored on write. That asymmetry is
#:   intentional upstream and reproduced here.
#: * **non-ASCII is not encoded.** `Name=CkIIα[Tik]-1` stays exactly that;
#:   `urllib.parse.quote` would render it `CkII%CE%B1[Tik]-1`.
#:
#: What IS encoded: the GFF3 reserved set, plus every C0 control character and
#: DEL, none of which can appear literally in a tab-delimited line.
_TO_QUOTE = frozenset("\n\t\r%;=&," + "".join(chr(i) for i in range(32)) + chr(127))


class Quoter(dict):
    """Caching percent-encoder, one character in, its encoding out.

    A `dict` subclass with `__missing__` rather than a function plus an
    `lru_cache`: attribute values are overwhelmingly ASCII alphanumerics that
    map to themselves, so the hot path is a plain dict hit and the cache fills
    itself with exactly the characters this process actually saw.

    Mirrors `gffutils.parser.Quoter`, including its guard against the empty
    string -- `"" in _TO_QUOTE` would be False anyway, but the explicit check
    is what upstream pins with a regression test.
    """

    def __missing__(self, char: str) -> str:
        encoded = f"%{ord(char):02X}" if char != "" and char in _TO_QUOTE else char
        self[char] = encoded
        return encoded


#: Process-wide instance. Safe to share: the only mutation is memoizing a pure
#: function of the key, so a race can at worst compute the same value twice.
quoter = Quoter()


def encode_value(value: str) -> str:
    """Percent-encode one GFF3 attribute value."""
    return "".join([quoter[char] for char in value])


#: Split on a separator only where the quotes to its right balance, so a
#: separator inside a quoted GFF2 value does not split the string. Keyed by
#: separator, longest first, matching `gffutils.parser.quoted_semicolon_patterns`.
#:
#: The loop variable is deliberately not left in the module namespace; upstream
#: leaks its `sep`, which `deviations.toml` records as `excluded` on the grounds
#: that a leaked local is not an API.
quoted_semicolon_patterns = {
    sep: re.compile(
        rf"""
            {re.escape(sep)}   # the separator under consideration
            (?=                # lookahead: does the remainder balance?
                (?:
                    [^"]       # a non-quote character
                    |          # or
                    "[^"]*"    # a complete quoted run
                )*
                $              # all the way to the end
            )
        """,
        re.VERBOSE,
    )
    for sep in (" ; ", "; ", ";")
}


# ---------------------------------------------------------------------------
# Attribute-string reconstruction
# ---------------------------------------------------------------------------


def _reconstruct(
    keyvals,
    dialect,
    keep_order: bool = False,
    sort_attribute_values: bool = False,
) -> str:
    """Render an attribute mapping back into a column-9 string.

    A faithful port of `gffutils.parser._reconstruct`, kept oracle-exact so it
    can be compared against directly. In particular it does **not** re-add a
    leading semicolon even when the dialect records one -- upstream drops it,
    so its own round-trip is lossy there. `Feature._format_attributes` adds it
    back afterwards, which is why gffbase round-trips such a file and gffutils
    does not.

    Order of operations matters and is the upstream order: encode per value,
    then split repeated keys, then sort by dialect order, then sort values,
    then join with the multival separator, then GFF2-quote, then join key to
    value, then join the parts, then append a trailing semicolon.

    Parameters
    ----------
    keyvals
        Mapping of attribute key to list of values.
    dialect
        Dialect dict. Must be non-empty.
    keep_order
        Sort keys into `dialect["order"]`; anything absent goes to the end.
    sort_attribute_values
        Sort each key's values. Mostly for making output comparable.
    """
    if not dialect:
        raise AttributeStringError("cannot reconstruct attributes without a dialect")
    if not keyvals:
        return ""

    fmt = dialect.get("fmt", "gff3")

    from gffbase import constants

    # Encode first, so that everything downstream is manipulating text that is
    # already safe to concatenate. GTF is never percent-encoded -- neither
    # engine decodes it on the way in, so encoding on the way out would invent
    # escapes the source never had. `ignore_url_escape_characters` turns the
    # whole scheme off, for callers whose values contain literal `%` that must
    # not be touched.
    if fmt != "gff3" or constants.ignore_url_escape_characters:
        attributes = {k: list(v) for k, v in keyvals.items()}
    else:
        attributes = {
            key: [encode_value(str(value)) for value in values] for key, values in keyvals.items()
        }

    # `Parent=a;Parent=b` versus `Parent=a,b` -- the dialect records which one
    # the source used.
    if dialect.get("repeated keys"):
        items: list[tuple[str, list[str]]] = []
        for key, values in attributes.items():
            if len(values) > 1:
                items.extend((key, [value]) for value in values)
            else:
                items.append((key, values))
    else:
        items = list(attributes.items())

    if keep_order:
        order = dialect.get("order") or []

        def sort_key(item):
            try:
                return order.index(item[0])
            except ValueError:
                # Not in the recorded order: park it at the end, stably.
                return len(order) + 1

        items.sort(key=sort_key)

    multival = dialect.get("multival separator", ",")
    kv_sep = dialect.get("keyval separator") or ("=" if fmt == "gff3" else " ")
    parts = []
    for key, values in items:
        if values:
            if sort_attribute_values:
                values = sorted(values)
            joined = multival.join(values)
            if joined:
                if dialect.get("quoted GFF2 values"):
                    joined = f'"{joined}"'
                parts.append(kv_sep.join([key, joined]))
            else:
                parts.append(key)
        elif fmt == "gtf":
            # GTF convention: a valueless attribute is written with an empty
            # quoted string, `gene_id "g1"; is_gene "";`.
            parts.append(kv_sep.join([key, '""']))
        else:
            parts.append(key)

    rendered = dialect.get("field separator", ";").join(parts)
    if dialect.get("trailing semicolon"):
        rendered += ";"
    return rendered


def _split_keyvals(keyval_str, dialect=None):
    """Parse an attribute string into `(mapping, dialect)`.

    The inverse of `_reconstruct`, and the entry point upstream's
    `parser_test.py` exercises directly. Delegates to the same attribute
    parser the ingest path uses, so the compatibility surface and the
    production path cannot give different answers.

    `dialect`, if supplied, is returned unchanged rather than inferred -- the
    caller has already decided.
    """
    from gffbase._pyfallback.attributes import parse_attributes
    from gffbase.dialect import default_dialect
    from gffbase.feature import _LazyAttributes

    pairs, observed = parse_attributes(keyval_str or "")
    resolved = dict(dialect) if dialect else {**default_dialect(), **observed}
    return _LazyAttributes(initial=pairs), resolved
