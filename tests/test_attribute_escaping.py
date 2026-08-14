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
"""Column 9 must survive a read-then-write.

Reading `feature.attributes` materializes decoded values and takes
serialization off the raw-bytes fast path. Without a re-encode step the
escaping was simply gone, and the failure was silent:

    Note=hello%20world      ->  Note=hello world          (lossy)
    pr_change=A%3B B        ->  pr_change=A; B            (INVALID -- two
                                                           attributes now)

On `tests/data/upstream/nonascii` one attribute became five and the record
stopped being valid GFF3. `str(feature)` raised nothing, and neither did
anything downstream.

The encoder is deliberately not `urllib.parse.quote`. Two absences are
load-bearing: **space is not encoded** (the spec does not reserve it) and
**non-ASCII is not encoded** (`Name=CkIIα[Tik]-1` must stay as it is, where
`quote()` would emit `CkII%CE%B1[Tik]-1`).
"""

from __future__ import annotations

import urllib.parse
from pathlib import Path

import pytest
from gffbase import Feature, create_db
from gffbase._serialize import _TO_QUOTE, Quoter, _reconstruct, encode_value, quoter
from gffbase.exceptions import AttributeStringError

DATA = Path(__file__).parent / "data"
UPSTREAM = DATA / "upstream"


# ---------------------------------------------------------------------------
# The encoder itself
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("char", "expected"),
    [
        (";", "%3B"),
        (",", "%2C"),
        ("=", "%3D"),
        ("&", "%26"),
        ("%", "%25"),
        ("\t", "%09"),
        ("\n", "%0A"),
        ("\r", "%0D"),
        ("\x00", "%00"),
        ("\x1f", "%1F"),
        ("\x7f", "%7F"),
    ],
)
def test_reserved_characters_are_encoded(char, expected):
    assert quoter[char] == expected


@pytest.mark.parametrize("char", [" ", "a", "Z", "0", "α", "/", ":", "|", "(", '"', "-", "."])
def test_unreserved_characters_are_left_alone(char):
    """Especially the space and the non-ASCII. An encoder built on
    `urllib.parse.quote` would fail both and corrupt the `nonascii` fixture."""
    assert quoter[char] == char


def test_the_empty_string_encodes_to_itself():
    """`"" in _TO_QUOTE` is False anyway, but upstream guards it explicitly and
    pins it with a regression test, so the behaviour is contractual."""
    assert quoter[""] == ""


def test_quoter_caches_what_it_computes():
    q = Quoter()
    assert "\x02" not in q
    assert q["\x02"] == "%02"
    assert q["\x02"] == "%02"
    assert dict(q) == {"\x02": "%02"}


def test_the_encode_set_is_exactly_the_specs():
    """41 code points: the 8 GFF3-reserved characters, C0, and DEL. Stated as a
    number so that widening or narrowing the set has to be deliberate."""
    assert _TO_QUOTE == frozenset("\n\t\r%;=&," + "".join(chr(i) for i in range(32)) + chr(127))
    assert len(_TO_QUOTE) == 38  # \n \t \r overlap the 0-31 range
    assert " " not in _TO_QUOTE
    assert "α" not in _TO_QUOTE


@pytest.mark.parametrize(
    "value",
    [
        "plain",
        "has space",
        "semi;colon",
        "com,ma",
        "eq=uals",
        "amp&ersand",
        "per%cent",
        "already%3Bencoded",
        "CkIIα[Tik]-1",
        "tab\there",
        "newline\nhere",
        "everything ;,=&% at once",
        "",
    ],
)
def test_encode_then_unquote_is_the_identity(value):
    """The decode side is `urllib.parse.unquote`, so this is the actual
    round-trip a caller gets, not a self-consistent fiction."""
    assert urllib.parse.unquote(encode_value(value)) == value


def test_encoding_is_idempotent_only_via_decode():
    """Encoding twice is NOT the identity -- `%` is itself reserved. This is
    why the encoder must run exactly once, on the materialized path only."""
    assert encode_value("a;b") == "a%3Bb"
    assert encode_value(encode_value("a;b")) == "a%253Bb"


# ---------------------------------------------------------------------------
# _reconstruct against the vendored oracle table
# ---------------------------------------------------------------------------

#: `gffutils.constants.dialect`, inlined so this file needs no oracle install.
ORACLE_DEFAULT_DIALECT = {
    "leading semicolon": False,
    "trailing semicolon": False,
    "quoted GFF2 values": False,
    "field separator": ";",
    "semicolon in quotes": False,
    "keyval separator": "=",
    "multival separator": ",",
    "fmt": "gff3",
    "repeated keys": False,
    "order": ["ID", "Name", "gene_id", "transcript_id"],
}


def _load_attr_cases():
    """The vendored `attr_test_cases.py` table -- upstream's own ground truth
    for `_split_keyvals` / `_reconstruct`, shipped in `tests/data/upstream/`
    and until now used by nothing."""
    import importlib.util

    spec = importlib.util.spec_from_file_location(
        "_attr_test_cases", UPSTREAM / "attr_test_cases.py"
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod.attrs


ATTR_CASES = _load_attr_cases()


def test_the_vendored_table_is_actually_populated():
    """A silently-empty parametrize would make the next test vacuous, so pin
    the exact count: if the vendored file is ever re-synced from upstream and
    grows or shrinks, that should be a visible decision."""
    assert len(ATTR_CASES) == 18
    assert all({"str", "attrs", "ok", "dialect_mods"} <= set(c) for c in ATTR_CASES)


@pytest.mark.parametrize("case", ATTR_CASES, ids=lambda c: c["str"][:48])
def test_reconstruct_matches_the_oracles_expected_output(case):
    """Upstream's contract: reconstructing the parsed dict reproduces the
    original attribute string, except for the cases where quoting is not
    self-consistent, which the table records in `ok`."""
    dialect = dict(ORACLE_DEFAULT_DIALECT)
    dialect.update(case["dialect_mods"])
    got = _reconstruct(case["attrs"], dialect, keep_order=True)
    assert got == (case["ok"] or case["str"])


# ---------------------------------------------------------------------------
# _reconstruct unit behaviour
# ---------------------------------------------------------------------------


def test_reconstruct_refuses_an_empty_dialect():
    with pytest.raises(AttributeStringError):
        _reconstruct({"ID": ["x"]}, {})


def test_reconstruct_of_nothing_is_the_empty_string():
    assert _reconstruct({}, ORACLE_DEFAULT_DIALECT) == ""


def test_repeated_keys_are_split_rather_than_joined():
    d = dict(ORACLE_DEFAULT_DIALECT, **{"repeated keys": True})
    assert _reconstruct({"Parent": ["a", "b"]}, d) == "Parent=a;Parent=b"


def test_without_repeated_keys_values_join_on_the_multival_separator():
    assert _reconstruct({"Parent": ["a", "b"]}, ORACLE_DEFAULT_DIALECT) == "Parent=a,b"


def test_keep_order_follows_the_dialect_order():
    d = dict(ORACLE_DEFAULT_DIALECT, order=["Name", "ID"])
    got = _reconstruct({"ID": ["1"], "Name": ["n"]}, d, keep_order=True)
    assert got == "Name=n;ID=1"


def test_keys_absent_from_the_order_go_last_in_insertion_order():
    d = dict(ORACLE_DEFAULT_DIALECT, order=["ID"])
    got = _reconstruct({"zz": ["1"], "ID": ["2"], "aa": ["3"]}, d, keep_order=True)
    assert got == "ID=2;zz=1;aa=3"


def test_sort_attribute_values_sorts_within_a_key():
    got = _reconstruct({"P": ["c", "a", "b"]}, ORACLE_DEFAULT_DIALECT, sort_attribute_values=True)
    assert got == "P=a,b,c"


def test_a_valueless_attribute_renders_as_a_bare_key_in_gff3():
    assert _reconstruct({"pseudo": []}, ORACLE_DEFAULT_DIALECT) == "pseudo"


def test_a_valueless_attribute_renders_as_empty_quotes_in_gtf():
    d = dict(ORACLE_DEFAULT_DIALECT, fmt="gtf")
    d.update({"keyval separator": " ", "field separator": "; ", "quoted GFF2 values": True})
    assert _reconstruct({"gene_id": ["g"], "is_gene": []}, d) == 'gene_id "g"; is_gene ""'


def test_gtf_values_are_never_percent_encoded():
    """Neither engine decodes GTF on the way in, so encoding on the way out
    would invent escapes the source never had."""
    d = dict(ORACLE_DEFAULT_DIALECT, fmt="gtf")
    d.update({"keyval separator": " ", "quoted GFF2 values": True})
    assert _reconstruct({"note": ["a;b"]}, d) == 'note "a;b"'


def test_the_field_separator_is_used_verbatim():
    """Not collapsed to `;`/`; ` -- ` ; ` is a real separator that appears in
    the wild and used to be silently rewritten."""
    d = dict(ORACLE_DEFAULT_DIALECT, **{"field separator": " ; "})
    assert _reconstruct({"a": ["1"], "b": ["2"]}, d) == "a=1 ; b=2"


# ---------------------------------------------------------------------------
# End to end, through Feature
# ---------------------------------------------------------------------------


def test_reading_an_attribute_no_longer_corrupts_the_record():
    """The original defect, on the fixture that exposed it.

    `pr_change` holds four `%3B`-escaped semicolons. Unescaped, column 9 gains
    three extra separators and one attribute becomes five.
    """
    db = create_db(str(UPSTREAM / "nonascii"), ":memory:")
    target = next(f for f in db.all_features() if "pr_change" in str(f))

    before = str(target).split("\t")[8].count(";")
    _ = target.attributes["pr_change"]  # materialize
    after = str(target).split("\t")[8]

    assert after.count(";") == before, "materializing changed the attribute count"
    assert "%3B" in after
    assert "α" in after, "non-ASCII must not be encoded"
    assert "%CE%B1" not in after


def test_a_space_that_arrived_encoded_is_not_re_encoded():
    """`hybrid1.gff3` has `Note=growth%20hormone%201`. The spec does not
    reserve the space, so the decoded form is what gets written back. This
    round-trip is lossy by design and matches the oracle."""
    db = create_db(str(UPSTREAM / "hybrid1.gff3"), ":memory:")
    target = next(f for f in db.all_features() if "growth" in str(f))
    _ = target.attributes["Note"]
    col9 = str(target).split("\t")[8]
    assert "Note=growth hormone 1" in col9
    assert "%20" not in col9


def test_a_bare_equals_inside_a_value_is_encoded_on_the_way_out():
    """`keyval_sep_in_attrs.gff` has `Note=...|identity=99.58|escore=2e-126`:
    the value legally contains the key/value separator. Re-emitted bare it
    would parse back as a different attribute set."""
    db = create_db(str(UPSTREAM / "keyval_sep_in_attrs.gff"), ":memory:")
    target = next(iter(db.all_features()))
    _ = target.attributes["Note"]
    col9 = str(target).split("\t")[8]
    assert "identity%3D99.58" in col9
    assert "identity=99.58" not in col9


def test_the_raw_path_is_still_byte_faithful():
    """Encoding must not touch a feature whose attributes were never read --
    those bytes are already whatever the file said."""
    db = create_db(str(UPSTREAM / "nonascii"), ":memory:")
    target = next(f for f in db.all_features() if "pr_change" in str(f))
    raw = str(target)
    assert "%3B" in raw
    # and again, having never materialized
    assert str(target) == raw


def test_round_trip_through_a_database_survives_every_reserved_character():
    values = "semi;comma,eq=amp&pct%end"
    src = f"chr1\tsrc\tgene\t1\t100\t.\t+\t.\tID=g1;Note={encode_value(values)}\n"
    db = create_db(src, ":memory:", from_string=True)
    f = db["g1"]
    assert f.attributes["Note"] == [values]
    # Re-serialize, re-parse, and the value must come back identical.
    line = str(f)
    db2 = create_db(line + "\n", ":memory:", from_string=True)
    assert db2["g1"].attributes["Note"] == [values]


def test_a_hand_built_feature_with_no_dialect_still_serializes():
    """`_reconstruct` refuses an empty dialect; `Feature` supplies the GFF3
    default rather than making every caller pass one."""
    f = Feature(seqid="chr1", source="s", featuretype="gene", start=1, end=9, attributes={"a": "b"})
    assert str(f).split("\t")[8] == "a=b"


def test_setting_an_attribute_containing_a_separator_is_safe():
    """The other way into the materialized path: mutate rather than read."""
    f = Feature(
        seqid="chr1",
        source="s",
        featuretype="gene",
        start=1,
        end=9,
        attributes={"ID": "g1"},
        dialect={"fmt": "gff3"},
    )
    f.attributes["Note"] = "a;b"
    col9 = str(f).split("\t")[8]
    assert col9 == "ID=g1;Note=a%3Bb"
    assert col9.count(";") == 1


# ---------------------------------------------------------------------------
# The persistent case: merge rewrites the stored blob
# ---------------------------------------------------------------------------


def test_merge_strategy_merge_stores_an_escaped_blob():
    """`_regenerate_attributes_blob` rebuilds `features.attributes_blob` from
    the DECODED `attributes` rows. Unescaped, the corruption is written to the
    database and outlives the process -- every later read of that feature
    parses one value as several attributes.
    """
    src = (
        "chr1\tsrc\tgene\t1\t100\t.\t+\t.\tID=g1;Note=first%3Bsemi\n"
        "chr1\tsrc\tgene\t1\t100\t.\t+\t.\tID=g1;Note=second\n"
    )
    db = create_db(src, ":memory:", from_string=True, merge_strategy="merge")

    blob = db.conn.execute("SELECT attributes_blob FROM features WHERE id = 'g1'").fetchone()[0]
    text = bytes(blob).decode()
    assert "%3B" in text, f"merged blob was stored unescaped: {text!r}"

    # And the stored bytes must re-parse to the same values, not to more.
    assert sorted(db["g1"].attributes["Note"]) == ["first;semi", "second"]
    assert len(db["g1"].attributes) == 2  # ID and Note, not ID + Note + a stray
