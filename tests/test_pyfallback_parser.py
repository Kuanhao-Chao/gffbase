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
"""The pure-Python parser, driven directly.

This module is easy to under-test and expensive to get wrong. It is:

* the **oracle** the Rust parser is differentially compared against, so a bug
  here can make a Rust bug look like agreement;
* the **only** parser on a wheel-less install -- source builds, unsupported
  platforms, anyone who pip-installs from an sdist without a toolchain.

It was exempted from coverage measurement entirely until 0.2.0, with a note
saying the exemption should be removed. Removing it revealed 73% coverage:
the whole directive/FASTA/error-demotion path in the main loop, and the
`_FallbackIterator` metadata drain, had no direct test.

Everything here passes `engine="python"` explicitly. `tests/conftest.py` has
an `engine` fixture that parametrizes over both engines, and
`test_engine_equivalence.py` uses it to prove the two agree -- but agreement
tests cannot cover a path neither engine is asked to take.
"""

from __future__ import annotations

import gzip
from pathlib import Path

import pytest
from gffbase import GFFFormatError
from gffbase.parser import detect_dialect, parse_bytes, parse_gff

DATA = Path(__file__).parent / "data"


def write(tmp_path: Path, text: str, name: str = "in.gff3") -> str:
    path = tmp_path / name
    path.write_text(text)
    return str(path)


def parse(tmp_path: Path, text: str, **kwargs):
    """Parse `text` through the Python engine and return the feature list."""
    return list(parse_gff(write(tmp_path, text), engine="python", **kwargs))


BASIC = """##gff-version 3
##sequence-region chr1 1 1000
chr1\tsrc\tgene\t100\t200\t.\t+\t.\tID=g1
chr1\tsrc\texon\t100\t150\t.\t+\t.\tID=e1;Parent=g1
"""


# ---------------------------------------------------------------------------
# Directives
# ---------------------------------------------------------------------------


def test_directives_are_stored_without_their_prefix(tmp_path):
    """`db.directives` is a documented attribute and the oracle strips `##`,
    so the stored form is part of the compatibility contract."""
    it = parse_gff(write(tmp_path, BASIC), engine="python")
    list(it)
    assert it.directives() == ["gff-version 3", "sequence-region chr1 1 1000"]
    assert not any(d.startswith("#") for d in it.directives())


def test_single_hash_comments_are_dropped(tmp_path):
    """A `#` comment is not a `##` directive and must not be stored."""
    text = "##gff-version 3\n# just a comment\nchr1\ts\tgene\t1\t9\t.\t+\t.\tID=g1\n"
    it = parse_gff(write(tmp_path, text), engine="python")
    features = list(it)
    assert len(features) == 1
    assert it.directives() == ["gff-version 3"]


def test_blank_lines_are_skipped(tmp_path):
    text = "##gff-version 3\n\n\nchr1\ts\tgene\t1\t9\t.\t+\t.\tID=g1\n\n"
    assert len(parse(tmp_path, text)) == 1


# ---------------------------------------------------------------------------
# Embedded FASTA -- both ways a file can end its feature section
# ---------------------------------------------------------------------------


def test_fasta_directive_ends_the_feature_section(tmp_path):
    text = "##gff-version 3\nchr1\ts\tgene\t1\t9\t.\t+\t.\tID=g1\n##FASTA\n>chr1\nACGTACGTAC\n"
    features = parse(tmp_path, text)
    assert [f.attributes_dict()["ID"] for f in features] == [["g1"]]


def test_a_bare_fasta_header_also_ends_it(tmp_path):
    """Some files omit the `##FASTA` directive and simply start a `>` record.
    Stopping only at the directive would parse `ACGTACGT` as a GFF line."""
    text = "##gff-version 3\nchr1\ts\tgene\t1\t9\t.\t+\t.\tID=g1\n>chr1\nACGTACGTAC\n"
    features = parse(tmp_path, text)
    assert len(features) == 1


def test_fasta_reached_during_the_dialect_peek(tmp_path):
    """The peek phase reads ahead to sniff the dialect, so it can hit the
    FASTA boundary before the main loop ever runs -- a separate code path
    from the one above."""
    text = "##gff-version 3\nchr1\ts\tgene\t1\t9\t.\t+\t.\tID=g1\n##FASTA\n>c\nACGT\n"
    features = parse(tmp_path, text, checklines=100)
    assert len(features) == 1


def test_a_file_that_is_only_fasta_yields_nothing(tmp_path):
    features = parse(tmp_path, "##gff-version 3\n##FASTA\n>chr1\nACGT\n")
    assert features == []


# ---------------------------------------------------------------------------
# Error handling: raise, or demote to a warning
# ---------------------------------------------------------------------------


def test_a_malformed_line_raises_under_the_ncbi_profile(tmp_path):
    text = "##gff-version 3\nchr1\ts\tgene\tNOT_A_NUMBER\t9\t.\t+\t.\tID=g1\n"
    with pytest.raises(GFFFormatError) as excinfo:
        parse(tmp_path, text, validation="ncbi")
    assert "line 2" in str(excinfo.value)
    assert "NOT_A_NUMBER" in str(excinfo.value), "the message must name the offending value"


def test_the_same_line_is_demoted_to_a_warning_when_not_strict(tmp_path):
    """`strict=False` skips the offending line and records it, so a mostly
    good file still yields its good records."""
    text = (
        "##gff-version 3\n"
        "chr1\ts\tgene\tNOT_A_NUMBER\t9\t.\t+\t.\tID=g1\n"
        "chr1\ts\tgene\t100\t200\t.\t+\t.\tID=g2\n"
    )
    it = parse_gff(write(tmp_path, text), engine="python", validation="ncbi", strict=False)
    features = list(it)
    assert [f.attributes_dict()["ID"] for f in features] == [["g2"]]
    assert len(it.warnings) == 1
    assert it.warnings[0]["line_no"] == 2


def test_too_few_fields_is_reported_with_its_line_number(tmp_path):
    text = "##gff-version 3\nchr1\ts\tgene\t1\n"
    with pytest.raises(GFFFormatError) as excinfo:
        parse(tmp_path, text, validation="ncbi")
    assert "line 2" in str(excinfo.value)
    assert "9" in str(excinfo.value), "the message must say how many fields it found"


def test_the_error_class_is_the_one_gffbase_exports(tmp_path):
    """The fallback parser raises `gffbase._native.GFFFormatError` when the
    extension is built and the pure-Python class otherwise -- deliberately,
    so that `except gffbase.GFFFormatError` catches both. Importing the
    Python class directly from `gffbase.exceptions` and expecting it to catch
    does NOT work, which is a trap worth having written down."""
    import gffbase

    text = "##gff-version 3\nchr1\ts\tgene\tNOPE\t9\t.\t+\t.\tID=g1\n"
    with pytest.raises(gffbase.GFFFormatError):
        parse(tmp_path, text, validation="ncbi")


def test_compat_mode_accepts_what_ncbi_rejects(tmp_path):
    """The whole point of the two-profile axis: the same file loads under
    `gffutils` validation and is rejected under `ncbi`."""
    text = "##gff-version 3\nchr1\ts\tCDS\t1\t9\t.\t+\t9\tID=c1\n"  # phase 9 is invalid
    with pytest.raises(GFFFormatError):
        parse(tmp_path, text, validation="ncbi")
    assert len(parse(tmp_path, text, validation="gffutils")) == 1


# ---------------------------------------------------------------------------
# The iterator's metadata surface
# ---------------------------------------------------------------------------


def test_dialect_is_available_before_any_feature_is_consumed(tmp_path):
    """`.dialect()` drives the generator just far enough to sniff, then
    restores the record it pulled -- so asking for metadata must not swallow
    the first feature."""
    it = parse_gff(write(tmp_path, BASIC), engine="python")
    dialect = it.dialect()
    assert dialect["fmt"] == "gff3"
    assert len(list(it)) == 2, "asking for the dialect consumed a feature"


def test_directives_are_available_before_iterating(tmp_path):
    it = parse_gff(write(tmp_path, BASIC), engine="python")
    assert it.directives() == ["gff-version 3", "sequence-region chr1 1 1000"]
    assert len(list(it)) == 2


def test_metadata_on_a_file_with_no_features(tmp_path):
    """A directives-only file never reaches a yield, so nothing hands the
    iterator a dialect. Returning `{}` made `.dialect()["fmt"]` a KeyError on
    exactly the inputs a caller probes before deciding what to do; the
    default dialect is reported instead, matching the Rust engine."""
    it = parse_gff(write(tmp_path, "##gff-version 3\n"), engine="python")
    assert it.dialect()["fmt"] == "gff3"
    assert len(it.dialect()) == 10
    assert it.directives() == ["gff-version 3"]
    assert list(it) == []


def test_metadata_is_cached_not_recomputed(tmp_path):
    it = parse_gff(write(tmp_path, BASIC), engine="python")
    assert it.dialect() == it.dialect()
    assert it.directives() == it.directives()
    assert len(list(it)) == 2


# ---------------------------------------------------------------------------
# Input shapes
# ---------------------------------------------------------------------------


def test_gzipped_input_is_transparent(tmp_path):
    path = tmp_path / "in.gff3.gz"
    with gzip.open(path, "wt", encoding="utf-8") as fh:
        fh.write(BASIC)
    assert len(list(parse_gff(str(path), engine="python"))) == 2


def test_parse_bytes_matches_parse_gff(tmp_path):
    from_file = parse(tmp_path, BASIC)
    from_bytes = list(parse_bytes(BASIC.encode("utf-8"), engine="python"))
    assert [str(a) for a in from_bytes] == [str(b) for b in from_file]


def test_detect_dialect_without_parsing(tmp_path):
    dialect = detect_dialect(write(tmp_path, BASIC), engine="python")
    assert dialect["fmt"] == "gff3"
    assert dialect["keyval separator"] == "="


def test_force_gff_overrides_a_gtf_looking_file(tmp_path):
    """`force_gff` pins the format when the heuristic would guess GTF."""
    text = 'chr1\ts\texon\t1\t9\t.\t+\t.\tgene_id "g1"; transcript_id "t1";\n'
    dialect = list(parse_gff(write(tmp_path, text, "in.gtf"), engine="python", force_gff=True))
    assert dialect  # parsed at all
    it = parse_gff(write(tmp_path, text, "in2.gtf"), engine="python", force_gff=True)
    list(it)
    assert it.dialect()["fmt"] == "gff3"
    assert it.dialect()["keyval separator"] == "="


def test_force_dialect_check_scans_the_whole_file(tmp_path):
    """Without it, only `checklines` records are sampled. The flag exists for
    files whose first few lines are not representative."""
    lines = ["##gff-version 3"]
    lines += [f"chr1\ts\texon\t{i}\t{i + 5}\t.\t+\t.\tID=e{i}" for i in range(1, 30)]
    text = "\n".join(lines) + "\n"
    it = parse_gff(write(tmp_path, text), engine="python", force_dialect_check=True, checklines=2)
    assert len(list(it)) == 29


# ---------------------------------------------------------------------------
# Both engines must agree on all of the above
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "text",
    [
        pytest.param(BASIC, id="basic"),
        pytest.param(
            "##gff-version 3\nchr1\ts\tgene\t1\t9\t.\t+\t.\tID=g1\n##FASTA\n>c\nACGT\n",
            id="fasta-directive",
        ),
        pytest.param(
            "##gff-version 3\nchr1\ts\tgene\t1\t9\t.\t+\t.\tID=g1\n>c\nACGT\n",
            id="bare-fasta",
        ),
        pytest.param(
            "##gff-version 3\n# comment\n\nchr1\ts\tg\t1\t9\t.\t+\t.\tID=g1\n", id="noise"
        ),
    ],
)
def test_the_two_engines_agree(tmp_path, text):
    """The differential check, on exactly the inputs above.

    If the fallback and the Rust parser disagree here, one of them is wrong
    and the parity suite -- which compares gffbase against gffutils, not
    against itself -- would not necessarily say so.
    """
    from gffbase.parser import native_available

    if not native_available():
        pytest.skip("Rust extension not built")

    path = write(tmp_path, text)
    py = parse_gff(path, engine="python")
    rs = parse_gff(path, engine="rust")
    assert [str(f) for f in py] == [str(f) for f in rs]
    assert py.directives() == rs.directives()
    assert py.dialect() == rs.dialect()


# ---------------------------------------------------------------------------
# Past the dialect peek
# ---------------------------------------------------------------------------
#
# The parser has TWO loops: the peek phase samples up to `checklines` records
# to infer the dialect, then a second loop streams the rest. Every test above
# uses a file small enough to finish inside the peek, so the streaming loop --
# and its own copies of the directive, comment, FASTA and error handling --
# was never entered. On a real annotation file it is where nearly every line
# is processed.


def big(n_features: int = 40, extras: str = "") -> str:
    lines = ["##gff-version 3"]
    lines += [
        f"chr1\ts\texon\t{i * 10}\t{i * 10 + 5}\t.\t+\t.\tID=e{i}" for i in range(1, n_features + 1)
    ]
    if extras:
        lines.append(extras)
    return "\n".join(lines) + "\n"


def test_streaming_loop_handles_all_the_records(tmp_path):
    """`checklines=5` leaves 35 records for the second loop."""
    features = parse(tmp_path, big(40), checklines=5)
    assert len(features) == 40
    assert [f.attributes_dict()["ID"][0] for f in features][-1] == "e40"


def test_a_directive_after_the_peek_is_still_collected(tmp_path):
    """Directives are handled in both loops, and the second copy had no test."""
    text = big(30) + "##sequence-region chr2 1 500\n"
    it = parse_gff(write(tmp_path, text), engine="python", checklines=3)
    list(it)
    assert "sequence-region chr2 1 500" in it.directives()


def test_a_comment_after_the_peek_is_dropped(tmp_path):
    lines = big(30).rstrip("\n").split("\n")
    lines.insert(20, "# a late comment")
    features = parse(tmp_path, "\n".join(lines) + "\n", checklines=3)
    assert len(features) == 30


def test_blank_lines_after_the_peek_are_skipped(tmp_path):
    lines = big(30).rstrip("\n").split("\n")
    lines.insert(20, "")
    features = parse(tmp_path, "\n".join(lines) + "\n", checklines=3)
    assert len(features) == 30


def test_fasta_directive_after_the_peek_stops_the_stream(tmp_path):
    text = big(30) + "##FASTA\n>chr1\nACGTACGT\n"
    features = parse(tmp_path, text, checklines=3)
    assert len(features) == 30


def test_a_bare_fasta_header_after_the_peek_stops_the_stream(tmp_path):
    text = big(30) + ">chr1\nACGTACGT\n"
    features = parse(tmp_path, text, checklines=3)
    assert len(features) == 30


def test_a_bad_line_after_the_peek_raises(tmp_path):
    import gffbase

    text = big(30) + "chr1\ts\tgene\tNOPE\t9\t.\t+\t.\tID=bad\n"
    with pytest.raises(gffbase.GFFFormatError, match="line 32"):
        parse(tmp_path, text, checklines=3, validation="ncbi")


def test_a_bad_line_after_the_peek_is_demoted_when_not_strict(tmp_path):
    text = big(30) + "chr1\ts\tgene\tNOPE\t9\t.\t+\t.\tID=bad\n"
    it = parse_gff(
        write(tmp_path, text), engine="python", checklines=3, validation="ncbi", strict=False
    )
    features = list(it)
    assert len(features) == 30, "the good records must still come through"
    assert len(it.warnings) == 1
    assert it.warnings[0]["line_no"] == 32


def test_a_compat_violation_after_the_peek_is_recorded_not_raised(tmp_path):
    """Under `gffutils` validation the record is kept AND annotated -- the
    `_record(violation)` branch of the streaming loop."""
    text = big(30) + "chr1\ts\tCDS\t500\t600\t.\t+\t9\tID=c1\n"
    it = parse_gff(write(tmp_path, text), engine="python", checklines=3, validation="gffutils")
    features = list(it)
    assert len(features) == 31, "the violating record is kept under compat"
    assert any(w["kind"] == "InvalidPhase" for w in it.warnings)


def test_both_engines_agree_past_the_peek(tmp_path):
    from gffbase.parser import native_available

    if not native_available():
        pytest.skip("Rust extension not built")

    text = big(40) + "##sequence-region chr2 1 500\n##FASTA\n>c\nACGT\n"
    path = write(tmp_path, text)
    py = parse_gff(path, engine="python", checklines=5)
    rs = parse_gff(path, engine="rust", checklines=5)
    assert [str(f) for f in py] == [str(f) for f in rs]
    assert py.directives() == rs.directives()
