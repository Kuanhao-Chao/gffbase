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
"""The compatibility modules must BEHAVE, not merely import.

`tests/parity/test_api_parity.py` proves the names line up, and it would pass
just as happily against ten modules of `pass`. These tests are what make the
surface real, and every `intentional` entry in `deviations.toml` names one of
them.
"""

from __future__ import annotations

import importlib.util
import os
import shutil
from pathlib import Path

import pytest
from gffbase import Feature, constants, create_db
from gffbase import bins as gbins
from gffbase.attributes import Attributes, dict_class
from gffbase.create import _DBCreator, _GFFDBCreator, _GTFDBCreator, deprecation_handler
from gffbase.helpers import (
    HERE,
    annotate_gff_db,
    canonical_transcripts,
    dialect_compare,
    example_filename,
    get_gff_db,
    infer_dialect,
    is_gff_db,
    make_query,
    merge_attributes,
    to_unicode,
)
from gffbase.inspect import inspect as gff_inspect
from gffbase.iterators import Directive, _FeatureIterator, is_url
from gffbase.version import version

DATA = Path(__file__).parent / "data"


@pytest.fixture
def db():
    return create_db(str(DATA / "hierarchy.gff3"), ":memory:")


# ---------------------------------------------------------------------------
# bins
# ---------------------------------------------------------------------------


def test_bins_one_is_the_smallest_containing_bin():
    assert gbins.bins(1, 100, fmt="gff", one=True) == 4681
    assert gbins.bins(0, 1, fmt="bed", one=True) == 4681


def test_bins_set_contains_every_level():
    """`one=False` must return the coarse bins too -- a feature stored in a
    coarse bin still overlaps a small query, and omitting them is how a
    `region()` query silently returns nothing."""
    found = gbins.bins(1, 100, fmt="gff", one=False)
    assert isinstance(found, set)
    assert 4681 in found  # the fine bin
    assert 1 in found  # the whole chromosome
    assert set(gbins.OFFSETS) <= found


def test_bins_degrade_past_the_addressable_range():
    assert gbins.bins(gbins.MAX_CHROM_SIZE, gbins.MAX_CHROM_SIZE + 10) == 1
    assert gbins.bins(-5, 10) == 1
    assert gbins.bins(-5, 10, one=False) == {1}


def test_bins_delegates_rather_than_reimplements():
    """The public module and the export path must not drift apart."""
    from gffbase._bins import bin_from_coords

    for start, stop in [(1, 100), (1000, 2000), (10**6, 10**6 + 5), (1, 2**20)]:
        assert gbins.bins(start, stop, one=True) == bin_from_coords(start, stop)


# ---------------------------------------------------------------------------
# constants + attributes, and the two live toggles
# ---------------------------------------------------------------------------


def test_the_column_lists_describe_the_legacy_schema():
    assert constants._keys[0] == "id"
    assert constants._keys[-1] == "bin"
    assert len(constants._keys) == 12
    assert constants._gffkeys_extra == constants._gffkeys + ["extra"]
    assert "CREATE TABLE features" in constants.SCHEMA


def test_a_feature_attributes_really_is_an_Attributes(db):
    assert isinstance(db["g1"].attributes, Attributes)
    assert dict_class is Attributes


def test_always_return_list_applies_to_getitem_only():
    """The deviation recorded for `gffbase.attributes`.

    Upstream routes `values()` and `items()` through `__getitem__` as well,
    which yields bare strings and makes every consumer that iterates a value
    walk it one character at a time -- gffutils' own `_reconstruct` renders
    `gene1` as `g,e,n,e,1`. gffbase's write paths all iterate `.items()`, so
    reproducing that would corrupt stored data.
    """
    attr = Attributes()
    attr["Name"] = "gene1"
    assert attr["Name"] == ["gene1"]

    constants.always_return_list = False
    try:
        assert attr["Name"] == "gene1"
        # ...but the iteration protocols stay list-valued.
        assert dict(attr.items()) == {"Name": ["gene1"]}
        assert list(attr.values()) == [["gene1"]]
    finally:
        constants.always_return_list = True
    assert attr["Name"] == ["gene1"]


def test_always_return_list_does_not_corrupt_serialization():
    """The concrete reason for the deviation above."""
    f = Feature(
        seqid="chr1",
        source="s",
        featuretype="gene",
        start=1,
        end=9,
        attributes={"Name": "gene1"},
        dialect={"fmt": "gff3"},
    )
    constants.always_return_list = False
    try:
        assert str(f).split("\t")[8] == "Name=gene1"
    finally:
        constants.always_return_list = True


def test_ignore_url_escape_characters_turns_the_whole_scheme_off():
    from gffbase.feature import feature_from_line

    line = "chr1\tsrc\tgene\t1\t9\t.\t+\t.\tNote=a%3Bb"
    assert feature_from_line(line).attributes["Note"] == ["a;b"]

    constants.ignore_url_escape_characters = True
    try:
        f = feature_from_line(line)
        assert f.attributes["Note"] == ["a%3Bb"], "value was decoded despite the toggle"
        # And it must not be double-encoded on the way back out.
        assert str(f).split("\t")[8] == "Note=a%3Bb"
    finally:
        constants.ignore_url_escape_characters = False


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def test_here_points_at_the_package():
    assert Path(HERE).is_dir()
    assert (Path(HERE) / "helpers.py").is_file()


def test_example_filename_resolves_a_shipped_fixture():
    assert Path(example_filename("hierarchy.gff3")).is_file()
    with pytest.raises(FileNotFoundError):
        example_filename("no_such_file.gff3")


def test_infer_dialect_reads_a_single_attribute_string():
    d = infer_dialect("ID=001;Name=gene1")
    assert d["fmt"] == "gff3"
    assert d["keyval separator"] == "="
    assert d["field separator"] == ";"
    assert infer_dialect('gene_id "g1"; transcript_id "t1";')["trailing semicolon"] is True


def test_merge_attributes_is_a_sorted_set_union():
    """Sorted, not insertion-ordered: two features must merge to the same
    attributes whichever was seen first."""
    a = {"k": ["b"], "only_a": ["1"]}
    b = {"k": ["a"], "only_b": ["2"]}
    assert merge_attributes(a, b) == merge_attributes(b, a)
    assert merge_attributes(a, b)["k"] == ["a", "b"]


def test_merge_attributes_numeric_sort():
    assert merge_attributes({"n": ["10"]}, {"n": ["2"]})["n"] == ["10", "2"]
    assert merge_attributes({"n": ["10"]}, {"n": ["2"]}, numeric_sort=True)["n"] == ["2", "10"]


def test_merge_attributes_numeric_sort_falls_back_on_mixed_values():
    got = merge_attributes({"n": ["abc"]}, {"n": ["2"]}, numeric_sort=True)
    assert got["n"] == ["2", "abc"]


def test_dialect_compare_handles_real_dialects():
    """The deviation: the oracle's `set(dict.items())` form raises
    `TypeError: unhashable type: 'list'` on any dialect carrying an `order`,
    which is all of them."""
    d1 = dict(constants.dialect)
    d2 = dict(constants.dialect, **{"trailing semicolon": True})
    diff = dialect_compare(d1, d2)
    assert diff["added"] == {"trailing semicolon": True}
    assert diff["removed"] == {"trailing semicolon": False}
    assert dialect_compare(d1, d1) == {"added": {}, "removed": {}}


def test_to_unicode_decodes_bytes():
    """The deviation: upstream's body is unreachable after 2to3 and returns
    bytes unchanged from a function named `to_unicode`."""
    assert to_unicode(b"hello") == "hello"
    assert to_unicode("hello") == "hello"
    assert to_unicode(b"caf\xc3\xa9") == "café"
    assert to_unicode(42) == 42


def test_is_gff_db_recognises_both_extensions(tmp_path):
    assert not is_gff_db(tmp_path / "missing.db")
    real = tmp_path / "x.gff3"
    real.write_text("chr1\ts\tgene\t1\t9\t.\t+\t.\tID=g\n")
    assert not is_gff_db(real)
    for name in ("a.db", "b.duckdb"):
        p = tmp_path / name
        p.write_bytes(b"")
        assert is_gff_db(p), name


def test_get_gff_db_always_returns_a_database(tmp_path):
    """The deviation: upstream returns a *path string* when a sibling `.db`
    exists and a FeatureDB otherwise, so its own CLI indexes a `str`."""
    from gffbase.interface import FeatureDB

    src = tmp_path / "in.gff3"
    src.write_text("chr1\ts\tgene\t1\t9\t.\t+\t.\tID=g1\n")
    got = get_gff_db(str(src))
    assert isinstance(got, FeatureDB)
    assert got["g1"].start == 1

    with pytest.raises(ValueError, match="does not exist"):
        get_gff_db(str(tmp_path / "nope.gff3"))


def test_annotate_gff_db_refuses_rather_than_pretending(db):
    """The deviation: upstream's body is `pass`, so it silently returns None
    and the caller believes their database was annotated."""
    with pytest.raises(NotImplementedError, match="stub upstream"):
        annotate_gff_db(db)


def test_sanitize_gff_db_orders_coordinates_and_stamps_a_gene_id():
    from gffbase.helpers import sanitize_gff_db

    src = (
        "chr1\ts\tgene\t100\t200\t.\t+\t.\tID=g1\n"
        "chr1\ts\tmRNA\t100\t200\t.\t+\t.\tID=t1;Parent=g1\n"
        "chr1\ts\texon\t150\t120\t.\t+\t.\tID=e1;Parent=t1\n"  # start > end
    )
    db = create_db(src, ":memory:", from_string=True)
    clean = sanitize_gff_db(db)
    for feature in clean.all_features():
        assert feature.start <= feature.end, feature.id
        assert feature.attributes["gid"] == ["g1"], feature.id


def test_make_query_builds_a_legacy_query():
    sql, args = make_query([], featuretype="exon", strand="+")
    assert "FROM features" in sql
    assert "features.featuretype = ?" in sql
    assert sql.count("WHERE") == 1
    assert args == ["exon", "+"]


def test_make_query_limit_uses_the_bin_index():
    sql, args = make_query([], limit="chr1:100-200")
    assert "features.bin IN (" in sql
    assert args[0] == "chr1"


def test_make_query_validates_a_string_order_by():
    """The deviation, and it is a security one: upstream checks its whitelist
    only for the iterable form and interpolates a bare string verbatim."""
    sql, _ = make_query([], order_by="start")
    assert "ORDER BY start ASC" in sql
    sql, _ = make_query([], order_by=["start", "end"], reverse=True)
    assert "ORDER BY start,end DESC" in sql
    with pytest.raises(ValueError, match="not a valid order-by"):
        make_query([], order_by="start; DROP TABLE features")
    with pytest.raises(ValueError, match="not a valid order-by"):
        make_query([], order_by=["nonexistent"])


def test_make_query_length_becomes_an_expression():
    sql, _ = make_query([], order_by="length")
    assert "(end - start)" in sql


def test_make_query_counts_its_placeholders():
    with pytest.raises(ValueError, match="Not enough args"):
        make_query([], extra="AND features.id = ?")


def test_canonical_transcripts_picks_the_longest_and_prints_nothing(tmp_path, capsys):
    """Two deviations: upstream's no-CDS fallback selects the SHORTEST
    transcript (it sorts ascending and takes [0], against its own comment),
    and it prints an internal tuple to stdout for every gene."""
    pyfaidx = pytest.importorskip("pyfaidx")
    assert pyfaidx

    fasta = tmp_path / "g.fa"
    fasta.write_text(">chr1\n" + ("ACGT" * 50) + "\n")
    src = (
        "chr1\ts\tgene\t1\t100\t.\t+\t.\tID=g1\n"
        "chr1\ts\tmRNA\t1\t20\t.\t+\t.\tID=short;Parent=g1\n"
        "chr1\ts\texon\t1\t20\t.\t+\t.\tID=se;Parent=short\n"
        "chr1\ts\tmRNA\t1\t80\t.\t+\t.\tID=long;Parent=g1\n"
        "chr1\ts\texon\t1\t80\t.\t+\t.\tID=le;Parent=long\n"
    )
    db = create_db(src, ":memory:", from_string=True)
    got = list(canonical_transcripts(db, str(fasta)))
    assert [t.id for t, _seq in got] == ["long"]
    assert capsys.readouterr().out == "", "canonical_transcripts printed to stdout"


# ---------------------------------------------------------------------------
# iterators
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("http://example.com/x.gff3", True),
        ("https://example.com/x.gff3", True),
        ("ftp://example.com/x.gff3", True),
        ("/local/path.gff3", False),
        ("path.gff3", False),
        ("", False),
        (None, False),
        (42, False),
    ],
)
def test_is_url(value, expected):
    assert is_url(value) is expected


def test_directive_strips_its_prefix():
    d = Directive("##gff-version 3")
    assert str(d) == "gff-version 3"
    assert d == "gff-version 3"
    assert d == Directive("##gff-version 3")
    assert {Directive("##a"), Directive("##a")} == {Directive("##a")}


def test_feature_iterator_yields_what_it_was_given(db):
    feats = list(db.all_features())
    it = _FeatureIterator(feats)
    assert [f.id for f in it] == [f.id for f in feats]
    assert it.directives == []


def test_create_db_accepts_a_feature_iterable(db):
    """What `_FeatureIterator` exists for, and what `sanitize_gff_db` needs."""
    rebuilt = create_db((f for f in db.all_features()), ":memory:")
    assert sorted(f.id for f in rebuilt.all_features()) == sorted(f.id for f in db.all_features())
    # The hierarchy survives the round trip.
    assert sorted(p.id for p in rebuilt.parents("e1")) == ["g1", "t1"]


# ---------------------------------------------------------------------------
# create / inspect / convert / version
# ---------------------------------------------------------------------------


def test_db_creators_build_a_working_database():
    """The deviation: these are adapters over `create_db`, not subclassable
    row-level seams -- gffbase has no per-row Python hook to override."""
    for cls in (_DBCreator, _GFFDBCreator):
        built = cls(str(DATA / "hierarchy.gff3"), ":memory:").create()
        assert built["g1"].featuretype == "gene"
    gtf = _GTFDBCreator(str(DATA / "simple.gtf"), ":memory:")
    assert gtf.transcript_key == "transcript_id"
    assert gtf.create() is gtf.db


def test_deprecation_handler_translates_infer_gene_extent():
    assert deprecation_handler({"infer_gene_extent": False}) == {
        "disable_infer_genes": True,
        "disable_infer_transcripts": True,
    }
    assert deprecation_handler({"force": True}) == {"force": True}


def test_inspect_counts_without_building_a_database():
    got = gff_inspect(str(DATA / "hierarchy.gff3"), verbose=False)
    assert got["feature_count"] == 8
    assert got["featuretype"]["gene"] == 1
    assert "ID" in got["attribute_keys"]
    assert set(got["chrom"]) == {"chr1"}


def test_inspect_honours_limit(db):
    got = gff_inspect(db, limit=3, verbose=False)
    assert got["feature_count"] == 3


def test_to_bed12_ends_with_a_newline(tmp_path):
    """`hierarchy.gff3` declares transcripts wider than their exons, which
    BED12 cannot represent -- the blocks would not reach chromEnd. Uses a
    coherent transcript instead, as every real annotation has."""
    from gffbase.convert import to_bed12

    src = tmp_path / "b.gff3"
    src.write_text(
        "##gff-version 3\n"
        "chr1\tsrc\tgene\t100\t600\t.\t+\t.\tID=g1\n"
        "chr1\tsrc\tmRNA\t100\t600\t.\t+\t.\tID=t1;Parent=g1\n"
        "chr1\tsrc\texon\t100\t200\t.\t+\t.\tID=e1;Parent=t1\n"
        "chr1\tsrc\texon\t500\t600\t.\t+\t.\tID=e2;Parent=t1\n"
    )
    db = create_db(str(src), str(tmp_path / "b.duckdb"), force=True)

    line = to_bed12("t1", db)
    assert line.endswith("\n")
    assert len(line.rstrip("\n").split("\t")) == 12
    # This function predates thick/thin handling: the thick span is the whole
    # feature, whatever the CDS children say.
    cols = line.rstrip("\n").split("\t")
    assert (int(cols[6]), int(cols[7])) == (db["t1"].start, db["t1"].end)


def test_version_matches_the_package():
    import gffbase

    assert version == gffbase.__version__


# ---------------------------------------------------------------------------
# Optional integrations
# ---------------------------------------------------------------------------


def test_integration_modules_import_without_their_dependency():
    """The surface must exist whether or not the extra is installed.

    gffutils imports pybedtools unguarded at module scope, so
    `import gffutils.pybedtools_integration` raises outright without it. Here
    the module imports and the failure arrives at the point of use, naming the
    package to install.
    """
    import gffbase.biopython_integration as bio
    import gffbase.contrib.plotting as plotting
    import gffbase.pybedtools_integration as pbt

    assert callable(bio.to_seqfeature)
    assert callable(pbt.to_bedtool)
    assert plotting.Gene is not None


def test_biopython_round_trip_is_exact():
    """`Feature -> SeqFeature -> Feature` must not lose a column.

    The strand is the one that bites: BioPython removed the
    `SeqFeature(strand=...)` argument and moved it onto the location, so the
    oracle's call raises `TypeError` on any current install.
    """
    pytest.importorskip("Bio")
    from gffbase.biopython_integration import from_seqfeature, to_seqfeature
    from gffbase.feature import feature_from_line

    for strand in ("+", "-", "."):
        line = f"chr1\tsrc\tgene\t10\t20\t.\t{strand}\t.\tID=g1;Note=hi"
        original = feature_from_line(line)
        restored = from_seqfeature(to_seqfeature(original))
        assert str(restored) == str(original), strand


def test_biopython_carries_the_non_seqfeature_columns():
    pytest.importorskip("Bio")
    from gffbase.biopython_integration import to_seqfeature
    from gffbase.feature import feature_from_line

    sf = to_seqfeature(feature_from_line("chr1\tsrc\tgene\t10\t20\t5\t+\t2\tID=g1"))
    assert sf.qualifiers["source"] == ["src"]
    assert sf.qualifiers["score"] == ["5"]
    assert sf.qualifiers["seqid"] == ["chr1"]
    assert sf.qualifiers["frame"] == ["2"]
    # BioPython locations are 0-based half-open; GFF is 1-based closed.
    assert (int(sf.location.start), int(sf.location.end)) == (9, 20)


def test_to_seqfeature_refuses_a_non_feature():
    pytest.importorskip("Bio")
    from gffbase.biopython_integration import to_seqfeature

    with pytest.raises(TypeError, match="expected a Feature"):
        to_seqfeature(42)


# ---------------------------------------------------------------------------
# pybedtools, for real
# ---------------------------------------------------------------------------
#
# These were import-smoke tested only, which left ~52 statements across
# `pybedtools_integration` and `contrib.plotting` unreachable in CI -- most of
# the gap between measured coverage and the gate. The extras exist now, so
# these run.

#: A module-level `importorskip` would abort collection of this ENTIRE file,
#: silently discarding the 41 tests defined above it -- the same silent-skip
#: failure mode that let 23 fixtures go missing unnoticed. Skip per test
#: instead, so losing the extra costs exactly the tests that need it.
#:
#: The BINARY is checked as well as the package. `pybedtools` imports happily
#: without `bedtools` on PATH and then raises `NotImplementedError: "sortBed"
#: does not appear to be installed` from the method -- so a package-only guard
#: passes collection and fails at run time, which is what turned every CI cell
#: red: the runners install the extra and have no bedtools.
requires_pybedtools = pytest.mark.skipif(
    importlib.util.find_spec("pybedtools") is None or shutil.which("bedtools") is None,
    reason="needs the [pybedtools] extra AND the bedtools binary on PATH",
)

TSS_SRC = (
    "chr1\ts\tgene\t100\t900\t.\t+\t.\tID=g1;Name=alpha\n"
    "chr1\ts\ttranscript\t100\t900\t.\t+\t.\tID=t1;Parent=g1;Name=alpha\n"
    "chr1\ts\ttranscript\t300\t800\t.\t-\t.\tID=t2;Parent=g1;Name=beta\n"
    "chr1\ts\texon\t100\t200\t.\t+\t.\tID=e1;Parent=t1\n"
)


@pytest.fixture
def tss_db():
    return create_db(TSS_SRC, ":memory:", from_string=True)


@requires_pybedtools
def test_to_bedtool_converts_every_feature(db):
    from gffbase.pybedtools_integration import to_bedtool

    intervals = list(to_bedtool(db.all_features()))
    assert len(intervals) == 8
    assert {iv.chrom for iv in intervals} == {"chr1"}


@requires_pybedtools
def test_to_bedtool_result_can_be_iterated_more_than_once(db):
    """The deviation from gffutils, and the reason for it.

    A generator-backed `BedTool` is a one-shot stream that reports its
    emptiness inconsistently -- `len(list(bt))` is 0 while `len(bt)` is the
    real count. Verified against pybedtools 0.12.0 with no gffbase code
    involved. gffutils returns that object, so `list(to_bedtool(...))` there
    is silently empty, which reads as an empty database rather than as a bug.
    """
    from gffbase.pybedtools_integration import to_bedtool

    bt = to_bedtool(db.all_features())
    assert len(list(bt)) == 8
    assert len(list(bt)) == 8, "second iteration lost the records"
    assert len(bt) == 8, "len() and list() must agree"


@requires_pybedtools
def test_raw_pybedtools_still_has_the_trap_this_works_around():
    """Pins the upstream behaviour, so that if pybedtools ever fixes it the
    workaround above can be revisited rather than cargo-culted forever."""
    import pybedtools

    def gen():
        for i in range(3):
            yield pybedtools.create_interval_from_list(
                ["chr1", "s", "gene", str(10 * i + 1), str(10 * i + 5), ".", "+", ".", f"ID=g{i}"]
            )

    assert len(list(pybedtools.BedTool(gen()))) == 0
    assert len(pybedtools.BedTool(gen())) == 3


@requires_pybedtools
def test_tsses_puts_the_minus_strand_site_at_the_end(tss_db):
    """The TSS is the 5' end. Taking `start` on both strands is the classic
    error here and it is silent -- the output is still a valid BED file, just
    describing the wrong end of every reverse-strand gene."""
    from gffbase.pybedtools_integration import tsses

    sites = {iv.attrs["ID"]: (iv.start, iv.end, iv.strand) for iv in tsses(tss_db)}
    assert sites["t1"][:2] == (99, 100)  # + strand -> transcript.start
    assert sites["t2"][:2] == (799, 800)  # - strand -> transcript.end
    assert sites["t1"][2] == "+" and sites["t2"][2] == "-"


@requires_pybedtools
def test_tsses_are_one_base(tss_db):
    from gffbase.pybedtools_integration import tsses

    for interval in tsses(tss_db):
        assert interval.end - interval.start == 1, str(interval)


@requires_pybedtools
def test_tsses_carry_the_derived_source(tss_db):
    from gffbase.pybedtools_integration import tsses

    assert {iv.fields[1] for iv in tsses(tss_db)} == {tss_db.derived_source}


@requires_pybedtools
def test_tsses_as_bed6(tss_db):
    from gffbase.pybedtools_integration import tsses

    intervals = list(tsses(tss_db, as_bed6=True))
    assert all(len(iv.fields) == 6 for iv in intervals)
    assert {iv.name for iv in intervals} == {"t1", "t2"}


@requires_pybedtools
def test_tsses_names_from_one_attribute(tss_db):
    from gffbase.pybedtools_integration import tsses

    assert {iv.name for iv in tsses(tss_db, attrs="Name")} == {"alpha", "beta"}


@requires_pybedtools
def test_tsses_names_from_several_attributes(tss_db):
    """`gff2bed`'s `name_field` takes ONE key, so a joined name has to be
    computed before the conversion rather than passed through."""
    from gffbase.pybedtools_integration import tsses

    got = {iv.name for iv in tsses(tss_db, attrs=["ID", "Name"], attrs_sep="|")}
    assert got == {"t1|alpha", "t2|beta"}


@requires_pybedtools
def test_tsses_merge_overlapping(tss_db):
    from gffbase.pybedtools_integration import tsses

    merged = list(tsses(tss_db, merge_overlapping=True))
    assert merged, "merging must not empty the result"
    assert all(iv.end - iv.start >= 1 for iv in merged)


@requires_pybedtools
def test_tsses_skips_transcripts_with_no_coordinates():
    """A transcript with no position has no transcription start site.
    Inventing 0 would put it at the start of the chromosome."""
    from gffbase.pybedtools_integration import tsses

    src = (
        "chr1\ts\tgene\t100\t900\t.\t+\t.\tID=g1\n"
        "chr1\ts\ttranscript\t100\t200\t.\t+\t.\tID=t1;Parent=g1\n"
        "chr1\ts\ttranscript\t.\t.\t.\t+\t.\tID=t_null;Parent=g1\n"
    )
    db = create_db(src, ":memory:", from_string=True)
    assert {iv.attrs["ID"] for iv in tsses(db)} == {"t1"}


@requires_pybedtools
def test_asinterval_round_trips_a_feature(db):
    """`helpers.asinterval` is the primitive under `to_bedtool`."""
    from gffbase.helpers import asinterval

    feature = db["g1"]
    interval = asinterval(feature)
    assert interval.chrom == feature.seqid
    assert interval.start == feature.start - 1  # BED is 0-based
    assert interval.end == feature.end


# ---------------------------------------------------------------------------
# Paths that had no test at all
# ---------------------------------------------------------------------------


def test_print_bin_sizes_reports_every_level(capsys):
    """A debugging aid, but an exported one -- upstream examples call it."""
    gbins.print_bin_sizes()
    out = capsys.readouterr().out
    lines = [line for line in out.splitlines() if line.startswith("level:")]
    assert len(lines) == len(gbins.OFFSETS)
    assert "bin size" in lines[0]
    # The finest level is 128 Kb (2**17); the coarsest is 512 Mb.
    assert "128.0 Kb" in lines[0]
    assert "512.0 Mb" in lines[-1]


def test_python_m_gffbase_runs_the_cli():
    """`__main__.py` is only reachable through a subprocess, so an in-process
    test cannot cover it -- and it is how `python -m gffbase` works."""
    import subprocess
    import sys

    import gffbase

    result = subprocess.run(
        [sys.executable, "-m", "gffbase", "--version"],
        capture_output=True,
        text=True,
        cwd=Path(gffbase.__file__).parent.parent.parent,
        env={**os.environ, "PYTHONPATH": str(Path(gffbase.__file__).parent.parent)},
    )
    assert result.returncode == 0, result.stderr
    assert gffbase.__version__ in result.stdout


def test_url_iterator_fetches_and_parses(tmp_path, monkeypatch):
    """`_UrlIterator` downloads to a temp file first, because dialect sniffing
    re-reads the head of the input and a socket cannot be rewound."""
    import io

    from gffbase.iterators import _UrlIterator

    payload = b"##gff-version 3\nchr1\ts\tgene\t1\t9\t.\t+\t.\tID=g1\n"

    class _Response(io.BytesIO):
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    monkeypatch.setattr("urllib.request.urlopen", lambda url, *a, **k: _Response(payload))
    it = _UrlIterator("http://example.com/x.gff3")
    features = list(it)
    # `.id` is assigned at ingest, not by the iterator, so compare on the
    # attribute the file actually carries.
    assert [f.attributes["ID"] for f in features] == [["g1"]]
    assert [(f.seqid, f.start, f.end) for f in features] == [("chr1", 1, 9)]
    assert it.directives == ["gff-version 3"]


def test_url_iterator_refuses_a_non_url():
    from gffbase.iterators import _UrlIterator

    with pytest.raises(ValueError, match="not a URL"):
        _UrlIterator("/local/path.gff3")


def test_feature_iterator_next_protocol(db):
    """`__next__` is separate from `__iter__` here and had no test."""
    from gffbase.iterators import _FeatureIterator

    feats = list(db.all_features())[:2]
    it = _FeatureIterator(feats)
    assert next(it).id == feats[0].id
    assert next(it).id == feats[1].id
    with pytest.raises(StopIteration):
        next(it)
    assert it.dialect["fmt"] == "gff3"


def test_inspect_over_a_feature_iterable(db):
    """The third input shape: not a path, not a FeatureDB."""
    got = gff_inspect(list(db.all_features()), verbose=False)
    assert got["feature_count"] == 8


def test_inspect_reports_progress_when_verbose(db, capsys):
    gff_inspect(db, limit=2, verbose=True)
    assert "features inspected" in capsys.readouterr().err


def test_the_two_iterator_layers_have_different_accessor_shapes():
    """`dialect`/`directives` are METHODS on the low-level iterator and
    PROPERTIES on the public one. Both are deliberate; nothing said so, and
    the mismatch cost real time.

    `parser._Iterator` wraps the Rust extension and mirrors its call-based
    surface. `iterators.DataIterator` is the gffutils-compatible face, and
    gffutils exposes `directives` as a plain attribute -- so a ported script
    writes `it.directives`, not `it.directives()`.

    `_FeatureIterator` sits under `DataIterator` and had them as methods,
    which mypy flagged and a `type: ignore` silenced. The result was that the
    same expression worked against one iterator and raised
    `TypeError: 'list' object is not callable` against another.
    """
    from gffbase.iterators import _FeatureIterator
    from gffbase.parser import parse_gff

    low = parse_gff(str(DATA / "simple.gff3"))
    assert callable(low.dialect), "parser._Iterator.dialect must stay a method"
    assert callable(low.directives)
    assert isinstance(low.dialect(), dict)

    from gffbase import DataIterator

    high = DataIterator(str(DATA / "simple.gff3"))
    assert not callable(high.dialect), "DataIterator.dialect must be a property"
    assert not callable(high.directives)
    assert isinstance(high.dialect, dict)

    # And the subclass agrees with its own base class.
    feature_it = _FeatureIterator([])
    assert not callable(feature_it.dialect)
    assert not callable(feature_it.directives)


# ---------------------------------------------------------------------------
# helpers: the argument shapes nothing exercised
# ---------------------------------------------------------------------------


def test_get_gff_db_accepts_a_path_object(tmp_path):
    from gffbase.interface import FeatureDB

    src = tmp_path / "in.gff3"
    src.write_text("chr1\ts\tgene\t1\t9\t.\t+\t.\tID=g1\n")
    assert isinstance(get_gff_db(src), FeatureDB)  # a Path, not a str


def test_get_gff_db_opens_an_existing_sibling_database(tmp_path):
    """The branch that made upstream's version return a path string instead
    of a database."""
    from gffbase.interface import FeatureDB

    src = tmp_path / "in.gff3"
    src.write_text("chr1\ts\tgene\t1\t9\t.\t+\t.\tID=g1\n")
    sibling = tmp_path / "in.gff3.db"
    create_db(str(src), str(sibling))

    got = get_gff_db(str(src))
    assert isinstance(got, FeatureDB)
    assert got["g1"].start == 1


def test_sanitize_gff_file_accepts_an_existing_database(tmp_path, capsys):
    from gffbase.helpers import sanitize_gff_file

    src = tmp_path / "in.gff3"
    src.write_text(
        "chr1\ts\tgene\t1\t99\t.\t+\t.\tID=g1\nchr1\ts\tmRNA\t1\t99\t.\t+\t.\tID=t1;Parent=g1\n"
    )
    dbfile = tmp_path / "in.db"
    create_db(str(src), str(dbfile))
    sanitize_gff_file(str(dbfile))
    assert "gid=" in capsys.readouterr().out


def test_sanitize_gff_file_without_in_memory(tmp_path, capsys):
    """`in_memory=False` routes through `get_gff_db` rather than building
    straight into `:memory:`."""
    from gffbase.helpers import sanitize_gff_file

    src = tmp_path / "in.gff3"
    src.write_text(
        "chr1\ts\tgene\t1\t99\t.\t+\t.\tID=g1\nchr1\ts\tmRNA\t1\t99\t.\t+\t.\tID=t1;Parent=g1\n"
    )
    sanitize_gff_file(str(src), in_memory=False)
    assert "gid=" in capsys.readouterr().out


def test_make_query_featuretype_as_a_list():
    sql, args = make_query([], featuretype=["gene", "mRNA"])
    assert "features.featuretype IN  (?,?)" in sql
    assert args == ["gene", "mRNA"]


def test_make_query_limit_as_a_tuple():
    """`limit` takes `"chr1:1-100"` or `("chr1", 1, 100)`; only the string
    form had a test."""
    sql, args = make_query([], limit=("chr1", 1, 100))
    assert "features.seqid = ?" in sql
    assert args[0] == "chr1"


def test_make_query_completely_within_flips_the_comparison():
    inside, args_in = make_query([], limit="chr1:100-200", completely_within=True)
    overlap, args_ov = make_query([], limit="chr1:100-200", completely_within=False)
    assert "features.start >= ? AND features.end <= ?" in inside
    assert "features.start <= ? AND features.end >= ?" in overlap
    # Note the argument order flips with the comparison.
    assert args_in[1:] == ["100", "200"]
    assert args_ov[1:] == ["200", "100"]


def test_canonical_transcripts_prefers_the_longest_cds(tmp_path, capsys):
    """The CDS-bearing branch. The no-CDS fallback is covered elsewhere; this
    is the path a real annotation takes."""
    pytest.importorskip("pyfaidx")

    fasta = tmp_path / "g.fa"
    fasta.write_text(">chr1\n" + ("ACGT" * 60) + "\n")
    src = (
        "chr1\ts\tgene\t1\t200\t.\t+\t.\tID=g1\n"
        "chr1\ts\tmRNA\t1\t60\t.\t+\t.\tID=short;Parent=g1\n"
        "chr1\ts\tCDS\t1\t60\t.\t+\t0\tID=sc;Parent=short\n"
        "chr1\ts\tmRNA\t1\t180\t.\t+\t.\tID=long;Parent=g1\n"
        "chr1\ts\tCDS\t1\t180\t.\t+\t0\tID=lc;Parent=long\n"
        # A gene with no children at all -- the `continue` branch.
        "chr1\ts\tgene\t500\t600\t.\t+\t.\tID=g_empty\n"
    )
    db = create_db(src, ":memory:", from_string=True)
    got = list(canonical_transcripts(db, str(fasta)))
    assert [t.id for t, _seq in got] == ["long"]
    assert capsys.readouterr().out == ""


def test_split_keyvals_honours_a_supplied_dialect():
    """With a dialect given it is returned unchanged rather than inferred --
    the caller has already decided."""
    from gffbase.parser import _split_keyvals

    supplied = {"fmt": "gtf", "keyval separator": " "}
    _parsed, dialect = _split_keyvals("ID=x", dialect=supplied)
    assert dialect == supplied


def test_is_url_survives_a_malformed_address():
    """`urlparse` raises on a truncated IPv6 literal."""
    assert is_url("http://[::1") is False


def test_directive_repr():
    assert repr(Directive("##gff-version 3")) == "Directive('gff-version 3')"


# ---------------------------------------------------------------------------
# Drop-in fidelity: the shapes a ported gffutils script actually relies on
# ---------------------------------------------------------------------------


def test_compatibility_submodules_are_bound_on_the_package():
    """`import gffbase as gffutils` has to give the oracle's attribute access.

    gffutils binds these submodules on its package, so `gffutils.constants.
    always_return_list = True` -- a documented idiom -- works after a plain
    `import gffutils`. gffbase bound none of them, so the one-line migration
    the README advertises raised `AttributeError` on the first line of any
    script that used one.
    """
    import gffbase as aliased

    for name in ("attributes", "bins", "constants", "create", "version"):
        assert hasattr(aliased, name), f"gffbase.{name} is not bound on the package"


def test_the_constants_toggle_is_settable_through_the_alias():
    """The specific idiom, end to end."""
    import gffbase as aliased

    original = aliased.constants.always_return_list
    try:
        aliased.constants.always_return_list = not original
        assert aliased.constants.always_return_list is not original
    finally:
        aliased.constants.always_return_list = original


def test_featuredb_method_is_an_alias_for_all_features(db):
    """gffutils defines `method = all_features`, and ported code calls it."""
    assert [f.id for f in db.method()] == [f.id for f in db.all_features()]
    assert [f.id for f in db.method(featuretype="exon")] == [
        f.id for f in db.all_features(featuretype="exon")
    ]


def test_bed12_refuses_blocks_that_do_not_span_the_feature(tmp_path):
    """BED12 cannot represent a feature its blocks do not cover.

    blockStarts are offsets from chromStart and the last block has to reach
    chromEnd, so emitting a line for a transcript whose exons stop short
    produces a record that names a range it does not cover -- and sends it
    into a genome browser. gffutils refuses with this exact message; gffbase
    used to emit the line.
    """
    src = tmp_path / "short.gff3"
    src.write_text(
        "##gff-version 3\n"
        "chr1\tsrc\tgene\t100\t1000\t.\t+\t.\tID=g1\n"
        "chr1\tsrc\tmRNA\t100\t1000\t.\t+\t.\tID=t1;Parent=g1\n"
        "chr1\tsrc\texon\t100\t200\t.\t+\t.\tID=e1;Parent=t1\n"
        "chr1\tsrc\texon\t500\t600\t.\t+\t.\tID=e2;Parent=t1\n"
    )
    db = create_db(str(src), str(tmp_path / "short.duckdb"), force=True)

    with pytest.raises(ValueError, match=r"End of last exon \(600\).*feature \(1000\)"):
        db.bed12("t1")


def test_featuredb_contains_returns_false_for_a_missing_id(db):
    """`in` answers a question; it does not raise it back at you.

    `gffutils.FeatureDB` defines neither `__contains__` nor `__iter__`, so
    Python falls back to iterating through `__getitem__` -- which raises
    `FeatureNotFoundError` on the first missing key. `"x" in db` therefore
    fails on exactly the case the operator exists for. Declared in
    `tests/parity/deviations.toml`.
    """
    assert "g1" in db
    assert "no_such_feature" not in db
