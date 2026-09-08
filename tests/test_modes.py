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
"""The compat/strict axis.

Validation used to conflate *which rules apply* with *what a violation does*,
which is why `create_db()` -- the compatibility entry point -- rejected six of
the twenty-three upstream fixtures gffutils reads. These tests pin both axes
and the behaviour each selects.
"""

from __future__ import annotations

import gffbase
import pytest
from gffbase.modes import (
    MODE_COMPAT,
    MODE_STRICT,
    ON_ERROR_RAISE,
    ON_ERROR_WARN,
    VALIDATION_GFFUTILS,
    VALIDATION_NCBI,
    resolve_mode,
)

# A CDS row with '.' phase: out of spec, emitted by FlyBase and WormBase alike.
CDS_NO_PHASE = (
    "##gff-version 3\n"
    "chr1\tsrc\tgene\t1\t100\t.\t+\t.\tID=g1\n"
    "chr1\tsrc\tCDS\t1\t100\t.\t+\t.\tID=c1;Parent=g1\n"
)

# end < start: exactly what sanitize tooling exists to repair.
BACKWARDS_COORDS = "##gff-version 3\nchr1\tsrc\tgene\t1000\t500\t.\t+\t.\tID=g1\n"


def _write(tmp_path, text, name="in.gff3"):
    p = tmp_path / name
    p.write_text(text)
    return str(p)


# ---------------------------------------------------------------------------
# resolve_mode
# ---------------------------------------------------------------------------


def test_compat_selects_the_gffutils_ruleset():
    r = resolve_mode(MODE_COMPAT)
    assert r.validation == VALIDATION_GFFUTILS
    assert r.on_error == ON_ERROR_RAISE
    assert r.raises


def test_strict_selects_the_ncbi_ruleset():
    r = resolve_mode(MODE_STRICT)
    assert r.validation == VALIDATION_NCBI


def test_axes_can_be_overridden_independently():
    """The point of two axes: rule set and disposition are separable."""
    r = resolve_mode(MODE_STRICT, on_error=ON_ERROR_WARN)
    assert r.validation == VALIDATION_NCBI
    assert not r.raises


def test_unknown_values_are_rejected():
    with pytest.raises(ValueError, match="mode must be"):
        resolve_mode("nonsense")
    with pytest.raises(ValueError, match="validation must be"):
        resolve_mode(MODE_COMPAT, validation="nonsense")
    with pytest.raises(ValueError, match="on_error must be"):
        resolve_mode(MODE_COMPAT, on_error="nonsense")


def test_strict_boolean_is_deprecated_but_still_maps_to_a_disposition():
    with pytest.warns(DeprecationWarning, match="strict= is deprecated"):
        assert resolve_mode(MODE_COMPAT, strict=False).on_error == ON_ERROR_WARN
    with pytest.warns(DeprecationWarning):
        assert resolve_mode(MODE_COMPAT, strict=True).on_error == ON_ERROR_RAISE


def test_passing_both_strict_and_on_error_is_an_argument_conflict():
    """TypeError, not ValueError: this is a caller mistake, not bad data."""
    with pytest.raises(TypeError, match="not both"):
        resolve_mode(MODE_COMPAT, strict=True, on_error=ON_ERROR_WARN)


# ---------------------------------------------------------------------------
# End-to-end behaviour
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("text", "kind"),
    [(CDS_NO_PHASE, "InvalidPhase"), (BACKWARDS_COORDS, "InvalidCoordinate")],
    ids=["cds-missing-phase", "end-before-start"],
)
def test_compat_loads_and_reports_what_strict_rejects(tmp_path, text, kind):
    path = _write(tmp_path, text)

    db = gffbase.create_db(path, ":memory:")
    assert db.count_features_of_type() > 0
    assert kind in {w["kind"] for w in db.warnings}

    with pytest.raises(ValueError) as excinfo:
        gffbase.create_db(path, ":memory:", mode="strict")
    assert getattr(excinfo.value, "kind", None) == kind


def test_compat_is_the_default(tmp_path):
    """The drop-in entry point must not require opting in to compatibility."""
    path = _write(tmp_path, CDS_NO_PHASE)
    assert gffbase.create_db(path, ":memory:").count_features_of_type() == 2


def test_a_clean_file_produces_no_warnings(tmp_path):
    """Compat must not be noisy: warnings mean something is actually wrong."""
    path = _write(
        tmp_path,
        "##gff-version 3\nchr1\tsrc\tgene\t1\t100\t.\t+\t.\tID=g1\n",
    )
    assert gffbase.create_db(path, ":memory:").warnings == []


def test_space_delimited_gff_loads_degenerately_like_the_oracle(tmp_path):
    """The one rule where matching the oracle means matching a defect.

    gffutils splits on tab and `zip`-truncates, so a space-delimited line
    becomes a single feature whose seqid is the whole line. Refusing the file
    outright is worse for a drop-in; the warning is what makes it safe.
    """
    path = _write(tmp_path, "chr1 src gene 1 100 . + . ID=g1\n")
    db = gffbase.create_db(path, ":memory:")
    assert db.count_features_of_type() == 1
    assert "TooFewFields" in {w["kind"] for w in db.warnings}
    (feature,) = db.all_features()
    assert feature.seqid == "chr1 src gene 1 100 . + . ID=g1"

    with pytest.raises(ValueError):
        gffbase.create_db(path, ":memory:", mode="strict")


def test_warnings_carry_the_line_number(tmp_path):
    """A diagnostic without a line number is useless on a 3 GB file."""
    path = _write(tmp_path, CDS_NO_PHASE)
    (warning,) = gffbase.create_db(path, ":memory:").warnings
    assert warning["line_no"] == 3
    assert "phase" in warning["message"].lower()


@pytest.mark.parametrize("engine", ["rust", "python"])
def test_both_engines_apply_the_profile_identically(tmp_path, engine):
    """The two parsers are diffed against each other; the profile is no exception."""
    if engine == "rust" and not gffbase.native_available():
        pytest.skip("native extension not built")
    path = _write(tmp_path, CDS_NO_PHASE)

    it = gffbase.parse_gff(path, engine=engine, validation="gffutils", strict=False)
    feats = list(it)
    assert len(feats) == 2
    assert [w["kind"] for w in it.warnings] == ["InvalidPhase"]

    strict_it = gffbase.parse_gff(path, engine=engine, validation="ncbi", strict=True)
    with pytest.raises(ValueError):
        list(strict_it)


@pytest.mark.parametrize("engine", ["rust", "python"])
def test_embedded_fasta_ends_the_feature_section_in_both_engines(tmp_path, engine):
    """A bare `>` starts FASTA even without a `##FASTA` directive.

    Without this the sequence lines became degenerate features -- three extra
    of them on `FBgn0031208.gff`.
    """
    if engine == "rust" and not gffbase.native_available():
        pytest.skip("native extension not built")
    path = _write(
        tmp_path,
        "##gff-version 3\n"
        "chr1\tsrc\tgene\t1\t100\t.\t+\t.\tID=g1\n"
        ">chr1\n"
        "ACGTACGTACGT\n"
        "ACGTACGTACGT\n",
    )
    assert len(list(gffbase.parse_gff(path, engine=engine, validation="gffutils"))) == 1


@pytest.mark.parametrize("engine", ["rust", "python"])
def test_padded_coordinates_parse_in_both_engines(tmp_path, engine):
    """Python's `int()` strips whitespace; Rust's `parse::<i64>()` does not.

    Without trimming, the two engines disagreed on any file with a padded
    coordinate column -- `wormbase_gff2.txt` has `944828 `, and the Rust engine
    dropped that record while the fallback kept it.
    """
    if engine == "rust" and not gffbase.native_available():
        pytest.skip("native extension not built")
    path = _write(tmp_path, "##gff-version 3\nchr1\tsrc\tgene\t100 \t 200\t.\t+\t.\tID=g1\n")
    (feature,) = list(gffbase.parse_gff(path, engine=engine, validation="gffutils"))
    assert (feature.start, feature.end) == (100, 200)


# ---------------------------------------------------------------------------
# GTF quoting: what GTF2.2 actually requires
# ---------------------------------------------------------------------------
#
# GTF2.2 says free-text attributes SHOULD be double-quoted. It does not require
# quotes on numeric or enumerated values, and the corpora rely on that: GENCODE
# emits `level 2;` on every one of its 6,068,892 lines, and `tag "..."`
# alongside it.
#
# gffbase demanded quotes on every value. Two consequences, both measured on
# the real file:
#
#   * `validation="ncbi"` -- the strict profile -- could not read GENCODE at
#     all. It raised on line 6, which is the first annotation line.
#   * compat mode accepted the file but recorded one InvalidAttribute warning
#     per feature: 6,068,892 warnings, one for every line, for something that
#     is not a defect.
#
# A bare token with no whitespace, quote or separator in it is unambiguous, so
# it is accepted. Anything containing a space, a semicolon or a quote still
# needs quoting -- that is where the ambiguity the rule exists to catch lives.
#
# Both engines carry this rule and both are changed; `engine` parametrizes over
# them so neither can drift.

GENCODE_STYLE_GTF = (
    "chr1\tHAVANA\tgene\t11869\t14409\t.\t+\t.\t"
    'gene_id "ENSG00000290825.2"; gene_type "lncRNA"; level 2; '
    'tag "overlaps_pseudogene";\n'
)


def test_an_unquoted_numeric_gtf_value_is_accepted(tmp_path, engine):
    """`level 2;` is legal GTF2.2 and appears on every GENCODE line."""
    src = tmp_path / "gencode_style.gtf"
    src.write_text(GENCODE_STYLE_GTF)

    features = list(gffbase.parse_gff(str(src), engine=engine, validation="ncbi", strict=True))

    assert len(features) == 1
    assert features[0].attributes_dict()["level"] == ["2"]


def test_an_unquoted_value_records_no_warning_in_compat(tmp_path, engine):
    """The compat cost of the old rule was one warning per line."""
    src = tmp_path / "gencode_style.gtf"
    src.write_text(GENCODE_STYLE_GTF)

    it = gffbase.parse_gff(str(src), engine=engine, validation="gffutils", strict=False)
    list(it)

    assert [w for w in it.warnings if "quoted" in str(w)] == []


def test_a_value_containing_a_space_still_needs_quotes(tmp_path, engine):
    """The ambiguity the rule exists to catch: an unquoted value with a space
    cannot be told from a second key/value pair."""
    src = tmp_path / "spaced.gtf"
    src.write_text('chr1\trs\texon\t1\t9\t.\t+\t.\tgene_id "G1"; note two words;\n')

    with pytest.raises(gffbase.GFFFormatError, match="quoted"):
        list(gffbase.parse_gff(str(src), engine=engine, validation="ncbi", strict=True))


def test_a_quoted_value_is_still_accepted(tmp_path, engine):
    """Relaxing the rule must not stop accepting what it accepted before."""
    src = tmp_path / "quoted.gtf"
    src.write_text('chr1\trs\texon\t1\t9\t.\t+\t.\tgene_id "G1"; transcript_id "T1";\n')

    features = list(gffbase.parse_gff(str(src), engine=engine, validation="ncbi", strict=True))
    assert features[0].attributes_dict()["gene_id"] == ["G1"]
