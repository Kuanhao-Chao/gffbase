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
"""Coverage-completing tests for small accessor / filter branches.

These cover the remaining gaps in `feature.py` and `interface.py`
that the structural test suites don't naturally hit — properties,
order_by variants, region-tuple shapes, and a few defensive
fallbacks.
"""

from __future__ import annotations

import pytest
from gffbase import Feature, FeatureDB, create_db


@pytest.fixture
def db(tmp_path):
    src = tmp_path / "small.gff3"
    src.write_text(
        "##gff-version 3\n"
        "chr1\trs\tgene\t100\t700\t.\t+\t.\tID=g1;Name=ALPHA\n"
        # The transcript spans exactly its exons (100..700), which is what
        # every real annotation does and what BED12 requires -- blockStarts
        # are offsets from chromStart and the last block must reach chromEnd.
        "chr1\trs\tmRNA\t100\t700\t.\t+\t.\tID=t1;Parent=g1\n"
        "chr1\trs\texon\t100\t200\t.\t+\t.\tID=e1;Parent=t1\n"
        "chr1\trs\texon\t300\t500\t.\t+\t.\tID=e2;Parent=t1\n"
        "chr1\trs\texon\t600\t700\t.\t-\t.\tID=e3;Parent=t1\n"
        "chr2\trs\texon\t100\t200\t.\t+\t.\tID=e_chr2;Parent=t1\n"
    )
    return create_db(str(src), str(tmp_path / "small.duckdb"), force=True)


# ---------------------------------------------------------------------------
# Feature property aliases (chrom / stop)
# ---------------------------------------------------------------------------


def test_feature_chrom_alias_returns_seqid():
    f = Feature(
        seqid="chr7", source="src", featuretype="exon", start=1, end=10, strand="+", frame="."
    )
    assert f.chrom == "chr7"
    assert f.chrom == f.seqid


def test_feature_stop_alias_returns_end():
    f = Feature(
        seqid="chr1", source="src", featuretype="exon", start=1, end=42, strand="+", frame="."
    )
    assert f.stop == 42
    assert f.stop == f.end


def test_feature_chrom_setter_writes_seqid():
    f = Feature(
        seqid="chr1", source="src", featuretype="exon", start=1, end=10, strand="+", frame="."
    )
    f.chrom = "chrM"
    assert f.seqid == "chrM"


def test_feature_stop_setter_writes_end():
    f = Feature(
        seqid="chr1", source="src", featuretype="exon", start=1, end=10, strand="+", frame="."
    )
    f.stop = 999
    assert f.end == 999


def test_feature_unicode_dunder_matches_str():
    f = Feature(
        seqid="chr1", source="src", featuretype="exon", start=1, end=10, strand="+", frame="."
    )
    assert f.__unicode__() == str(f)


def test_parsed_feature_chrom_and_stop_aliases():
    """`ParsedFeature` (the dataclass yielded by the parser) carries
    the same `chrom` / `stop` aliases as the user-facing `Feature`."""
    from gffbase import parse_bytes

    src = b"chr1\trs\texon\t100\t200\t.\t+\t.\tID=p1\n"
    pf = next(iter(parse_bytes(src)))
    assert pf.chrom == "chr1"
    assert pf.stop == 200


# ---------------------------------------------------------------------------
# region() filter combinations
# ---------------------------------------------------------------------------


def test_region_with_strand_filter(db):
    """Combine seqid + start/end + strand. Exercises the strand
    branch of `_region_sql_btree`."""
    plus = sorted(
        f.id
        for f in db.region(
            seqid="chr1",
            start=1,
            end=10_000,
            strand="+",
        )
    )
    minus = sorted(
        f.id
        for f in db.region(
            seqid="chr1",
            start=1,
            end=10_000,
            strand="-",
        )
    )
    assert "e1" in plus and "e2" in plus
    assert "e3" in minus
    assert "e3" not in plus
    assert "e1" not in minus


def test_region_with_featuretype_list(db):
    """Pass `featuretype` as a list, not a single string. Exercises
    the `featuretype IN (...)` branch."""
    rows = sorted(
        f.id
        for f in db.region(
            seqid="chr1",
            start=1,
            end=10_000,
            featuretype=["exon", "mRNA"],
        )
    )
    assert "t1" in rows
    assert "e1" in rows
    assert "g1" not in rows  # gene was excluded


def test_region_string_with_strand_suffix(db):
    """`chr1:100-500:+` — string form with strand. Exercises the
    suffix-stripping branch of `_normalize_region_args`."""
    rows = sorted(f.id for f in db.region("chr1:100-500:+"))
    # exons in 100..500 on + strand → e1 (100..200), e2 (300..500).
    assert "e1" in rows
    assert "e2" in rows


def test_region_tuple_len_2_seqid_only(db):
    """A 2-tuple `(seqid, _)` shorthand: `_normalize_region_args`
    branches on tuple length 2 to mean "seqid only, no coord
    constraint." Exercises the `len(region) == 2` branch."""
    rows = list(db.region(("chr2", None)))  # all of chr2
    assert any(f.id == "e_chr2" for f in rows)
    rows = list(db.region(("chrZZ", None)))  # absent seqid
    assert rows == []


def test_region_invalid_tuple_length_raises(db):
    """Tuple of length 4 is not a valid shape — raises ValueError."""
    with pytest.raises(ValueError):
        list(db.region((1, 2, 3, 4)))


def test_region_unsupported_type_raises(db):
    with pytest.raises(TypeError):
        list(db.region(12345))


# ---------------------------------------------------------------------------
# order_by branches in features_of_type
# ---------------------------------------------------------------------------


def test_features_of_type_order_by_length(db):
    """`order_by="length"` sorts by `end - start`. Exercises the
    `length` synthetic-column branch."""
    rows = list(db.features_of_type("exon", order_by="length"))
    spans = [f.end - f.start for f in rows]
    assert spans == sorted(spans)


def test_features_of_type_order_by_id(db):
    """`id` is a real column and sorting by it is meaningful, so it is on the
    whitelist even though the oracle's documented list omits it."""
    rows = list(db.features_of_type("exon", order_by="id"))
    assert len(rows) >= 1
    ids = [f.id for f in rows]
    assert ids == sorted(ids)


def test_features_of_type_rejects_an_unknown_order_by(db):
    with pytest.raises(ValueError, match="cannot order by"):
        list(db.features_of_type("exon", order_by="no_such_column"))


def test_features_of_type_order_by_reverse(db):
    rows_asc = list(db.features_of_type("exon", order_by="start"))
    rows_desc = list(db.features_of_type("exon", order_by="start", reverse=True))
    assert [f.start for f in rows_asc] == sorted(f.start for f in rows_asc)
    assert [f.start for f in rows_desc] == sorted((f.start for f in rows_desc), reverse=True)


# ---------------------------------------------------------------------------
# bed12 name fallbacks
# ---------------------------------------------------------------------------


def test_bed12_name_is_a_dot_when_the_attribute_is_missing(db):
    """A missing name attribute yields BED's `.`, not the feature id.

    Substituting the id was friendlier but not what the oracle emits, so
    every BED12 line of a file whose features lack the requested attribute
    differed. `.` is BED's documented "no name" value; a caller who wants the
    id can ask for the attribute that holds it.
    """
    tx = db["t1"]
    cols = db.bed12(tx, name_field="nonexistent_attribute_key").split("\t")
    assert cols[3] == "."


def test_bed12_falls_back_when_attribute_value_empty(tmp_path):
    """Attribute key exists but its value list is empty (degenerate
    case of `attributes['name'] == []`). The IndexError handler kicks
    in and we use `feature.id`."""
    db_path = tmp_path / "edgecase.duckdb"
    src = tmp_path / "edgecase.gff3"
    src.write_text(
        "##gff-version 3\n"
        "chr1\trs\ttranscript\t1\t100\t.\t+\t.\tID=tx1\n"
        "chr1\trs\texon\t1\t100\t.\t+\t.\tID=ex1;Parent=tx1\n"
    )
    db = create_db(str(src), str(db_path), force=True)
    tx = db["tx1"]
    # Force `attributes['Name']` to exist but be empty so the
    # IndexError-from-[0] branch fires.
    tx.attributes["Name"] = []
    assert db.bed12(tx, name_field="Name").split("\t")[3] == "."


# ---------------------------------------------------------------------------
# delete / update id-coercion accepts FeatureDB iterables
# ---------------------------------------------------------------------------


def test_delete_accepts_string_id(db):
    db.delete(["e1"])
    assert "e1" not in {f.id for f in db.features_of_type("exon")}


def test_delete_accepts_feature_objects(db):
    f = db["e2"]
    db.delete([f])
    assert "e2" not in {x.id for x in db.features_of_type("exon")}


def test_delete_accepts_mixed_iterable(db):
    """Mixed iterable of strings and Feature objects. Exercises the
    elif-Feature branch of `_coerce_ids`."""
    f = db["e1"]
    db.delete([f, "e2"])
    remaining = {x.id for x in db.features_of_type("exon")}
    assert "e1" not in remaining and "e2" not in remaining


def test_delete_empty_list_is_noop(db):
    """Empty input → return self without issuing SQL."""
    before = {f.id for f in db.features_of_type("exon")}
    db.delete([])
    after = {f.id for f in db.features_of_type("exon")}
    assert before == after


# ---------------------------------------------------------------------------
# Feature constructor branches: pre-built _LazyAttributes path
# ---------------------------------------------------------------------------


def test_feature_constructor_accepts_lazyattributes_directly():
    """The constructor's `isinstance(attributes, _LazyAttributes)` arm
    re-uses an already-built lazy object without re-parsing — used by
    the row-materialization path internally."""
    from gffbase.feature import _LazyAttributes

    lazy = _LazyAttributes(initial={"ID": ["x"]}, dialect_fmt="gff3")
    f = Feature(
        seqid="chr1",
        source="src",
        featuretype="exon",
        start=1,
        end=10,
        strand="+",
        frame=".",
        attributes=lazy,
    )
    assert f.attributes is lazy  # same object, not re-wrapped
    assert f.attributes.get("ID") == ["x"]


# ---------------------------------------------------------------------------
# children() with limit (region clipping) — relational + spatial overlap
# ---------------------------------------------------------------------------


def test_children_with_limit_seqid_only(db):
    """`children(..., limit="chr1:200-400")` clips the relational
    walk to a coordinate window. Exercises the seqid+coords branch
    of `_relation_region_filter`."""
    rows = sorted(
        f.id
        for f in db.children(
            "t1",
            limit="chr1:200-400",
        )
    )
    # Only e2 (300..500 — overlaps) makes the cut here. e1 (100..200
    # — overlaps at one base) might also match depending on overlap
    # semantics; the test simply pins that limit-filtering does
    # *something* and doesn't crash.
    assert "e3" not in rows  # 600..700, far outside


def test_children_with_limit_seqid_and_completely_within(db):
    """Combine limit + completely_within. Exercises the
    `completely_within=True` branch of `_relation_region_filter`."""
    rows = sorted(
        f.id
        for f in db.children(
            "t1",
            limit="chr1:50-550",
            completely_within=True,
        )
    )
    # e1 (100..200) and e2 (300..500) are fully within 50..550;
    # e3 (600..700) is not.
    assert "e1" in rows and "e2" in rows
    assert "e3" not in rows


# ---------------------------------------------------------------------------
# GFFWriter helper methods
# ---------------------------------------------------------------------------


def test_gffwriter_write_exon_children(tmp_path, db):
    """`write_exon_children` is a small convenience wrapper around
    `write_rec` + a `db.children(level=1)` walk. Smoke-test it
    writes the parent + its children to the output file."""
    from gffbase import GFFWriter

    out = tmp_path / "exonchildren.gff3"
    with GFFWriter(str(out)) as w:
        w.write_exon_children(db, "e1")  # the exon has no children
    text = out.read_text()
    assert "e1" in text


def test_gffwriter_write_mrna_children(tmp_path, db):
    """Symmetric helper: `write_mRNA_children` writes the mRNA plus
    all level-1 children."""
    from gffbase import GFFWriter

    out = tmp_path / "mrnachildren.gff3"
    with GFFWriter(str(out)) as w:
        w.write_mRNA_children(db, "t1")
    text = out.read_text()
    assert "t1" in text
    assert "e1" in text and "e2" in text and "e3" in text


# ---------------------------------------------------------------------------
# Closure-cache fallback paths — older DB without `closure_max_depth` meta
# ---------------------------------------------------------------------------


def test_featuredb_handles_missing_closure_max_depth_meta(tmp_path):
    """Older databases (or hand-imported ones) may lack the
    `closure_max_depth` meta row. The opener falls back to a live
    `MAX(depth)` query — must not crash, must produce a working DB.
    """
    src = tmp_path / "small.gff3"
    src.write_text(
        "##gff-version 3\n"
        "chr1\trs\tgene\t1\t100\t.\t+\t.\tID=g1\n"
        "chr1\trs\texon\t1\t100\t.\t+\t.\tID=e1;Parent=g1\n"
    )
    out = tmp_path / "live.duckdb"
    db = create_db(str(src), str(out), force=True)
    db.conn.execute("DELETE FROM meta WHERE key = 'closure_max_depth'")
    db.conn.close()
    # Re-open: must take the live MAX(depth) fallback branch.
    db2 = FeatureDB(str(out))
    assert db2._closure_max_depth == 1
    # Functional: children walk still works.
    assert {f.id for f in db2.children("g1")} == {"e1"}


def test_featuredb_seqid_map_missing_falls_back_to_btree(tmp_path):
    """If a re-opened DB has the R-tree column but the `seqid_map`
    side table is missing or empty, the opener disables the R-tree
    path so `region()` falls through to the B-tree."""
    src = tmp_path / "smap.gff3"
    src.write_text("##gff-version 3\nchr1\trs\texon\t1\t100\t.\t+\t.\tID=e1\n")
    out = tmp_path / "smap.duckdb"
    db = create_db(str(src), str(out), force=True)
    if db._rtree_built:
        # Wipe the side table the R-tree path needs.
        db.conn.execute("DELETE FROM seqid_map")
        db.conn.close()
        db2 = FeatureDB(str(out))
        # The fallback path still answers correctly.
        rows = list(db2.region(seqid="chr1", start=1, end=200))
        assert any(f.id == "e1" for f in rows)


# ---------------------------------------------------------------------------
# Polars-specific empty-result branch
# ---------------------------------------------------------------------------


def test_empty_batched_polars_format(db):
    """`format='polars'` on an empty input list. Exercises the
    polars branch of `_empty_batched_result` if polars is
    installed; otherwise the import-error path raises with a
    helpful message."""
    pytest.importorskip("polars")
    out = db.children_batched([], format="polars")
    assert out.shape[0] == 0
    out_r = db.region_batched([], format="polars")
    assert out_r.shape[0] == 0


# ---------------------------------------------------------------------------
# _coerce_ids accepts a FeatureDB instance directly
# ---------------------------------------------------------------------------


def test_coerce_ids_accepts_featuredb_instance(tmp_path, db):
    """Passing a whole `FeatureDB` to `delete(...)` is documented
    legacy behavior — it deletes every feature in the source DB.
    Exercises the `isinstance(features, FeatureDB)` branch."""
    other_src = tmp_path / "other.gff3"
    other_src.write_text("##gff-version 3\nchr1\trs\texon\t300\t500\t.\t+\t.\tID=e2\n")
    other = create_db(str(other_src), str(tmp_path / "o.duckdb"), force=True)
    db.delete(other)  # deletes ids that exist in `other`, i.e. e2
    assert "e2" not in {f.id for f in db.features_of_type("exon")}


def test_coerce_ids_skips_non_string_non_feature_items(db):
    """Mixed iterable with stray non-string / non-Feature items —
    the `_coerce_ids` loop skips them silently rather than crashing
    with a TypeError. Exercises the `continue`-style branch."""
    f = db["e1"]
    db.delete([f, 42, None, "e2", {"not": "an id"}])
    remaining = {x.id for x in db.features_of_type("exon")}
    assert "e1" not in remaining
    assert "e2" not in remaining


# ---------------------------------------------------------------------------
# interfeatures generator branches
# ---------------------------------------------------------------------------


def test_interfeatures_with_merge_attributes(db):
    """`interfeatures` over two adjacent exons with attribute
    merging produces a dict with deduped keys."""
    e1, e2 = db["e1"], db["e2"]
    # Inject some overlap-able attributes.
    e1.attributes["tag"] = ["A", "B"]
    e2.attributes["tag"] = ["B", "C"]
    inter = list(
        db.interfeatures(
            [e1, e2],
            new_featuretype="intergap",
            merge_attributes=True,
        )
    )
    assert len(inter) == 1
    f = inter[0]
    assert f.featuretype == "intergap"
    assert f.start == e1.end + 1
    assert f.end == e2.start - 1
    # Deduped union: A, B, C in some order.
    assert set(f.attributes["tag"]) == {"A", "B", "C"}


def test_interfeatures_skips_negative_gaps(db):
    """When two adjacent features overlap (`new_end < new_start`),
    the generator skips that pair instead of yielding a degenerate
    feature. Exercises the `continue` branch."""
    e1 = db["e1"]
    # Construct a synthetic overlapping pair.
    overlapping = Feature(
        seqid=e1.seqid,
        source="t",
        featuretype="exon",
        start=e1.start + 5,
        end=e1.end + 5,
        strand="+",
        frame=".",
        attributes={"ID": ["overlap_x"]},
    )
    inter = list(db.interfeatures([e1, overlapping]))
    assert inter == []  # no gap to fill


def test_interfeatures_with_attribute_func(db):
    """`attribute_func` callback is invoked per yielded interfeature
    and gets to rewrite the merged attribute dict."""
    e1, e2 = db["e1"], db["e2"]
    seen_calls = []

    def ftn(attrs):
        # Unary, matching the oracle: the callback sees ONE flank's
        # attributes at a time and returns a replacement.
        seen_calls.append(dict(attrs))
        return {**attrs, "custom": ["from_callback"]}

    inter = list(db.interfeatures([e1, e2], attribute_func=ftn))
    assert len(seen_calls) == 2
    assert [a["ID"] for a in seen_calls] == [["e1"], ["e2"]]
    assert inter[0].attributes["custom"] == ["from_callback"]


def test_interfeatures_empty_input_yields_nothing(db):
    assert list(db.interfeatures([])) == []


# ---------------------------------------------------------------------------
# merge() generator: accum-flush branch on featuretype change
# ---------------------------------------------------------------------------


def test_feature_sequence_with_dict_like_fasta():
    """`Feature.sequence(fasta=dict_like)` is the no-pyfaidx path.
    Pyfaidx itself uses dict-like indexing, so any object with the
    same `[seqid][start:end]` shape works as a drop-in. Exercises
    the `else: fa = fasta` arm of `Feature.sequence`."""

    class DictLikeFasta:
        def __init__(self, data):
            self._data = data

        def __getitem__(self, k):
            return self._data[k]

    fasta = DictLikeFasta({"chr1": "AAAACCCCGGGGTTTT"})
    f = Feature(seqid="chr1", source="t", featuretype="exon", start=5, end=8, strand="+", frame=".")
    seq = f.sequence(fasta)
    assert seq == "CCCC"


def test_feature_sequence_revcomp_on_minus_strand():
    """Sequence on the `-` strand is reverse-complemented when
    `use_strand=True` (the default). Exercises `_revcomp`."""
    fasta = {"chr1": "AAAACCCCGGGGTTTT"}
    f = Feature(seqid="chr1", source="t", featuretype="exon", start=5, end=8, strand="-", frame=".")
    # Forward bases at 5..8 (1-based, inclusive) are "CCCC";
    # revcomp is "GGGG".
    assert f.sequence(fasta) == "GGGG"
    # use_strand=False keeps the forward sequence.
    assert f.sequence(fasta, use_strand=False) == "CCCC"


def test_merge_flushes_on_featuretype_change(db):
    """`merge` groups consecutive features by criteria; when the
    criteria stop matching, the accumulator is flushed and a fresh
    one starts. Exercises the `accum.children = list(components);
    yield accum` arm of the loop."""
    from gffbase import merge_criteria as mc

    e1 = db["e1"]
    e2 = db["e2"]  # same featuretype as e1 (exon) — they merge
    # Force a featuretype change by cloning e2 with a different type.
    different = Feature(
        seqid=e1.seqid,
        source=e1.source,
        featuretype="CDS",
        start=e2.start,
        end=e2.end,
        strand=e2.strand,
        frame=".",
        attributes={"ID": ["c1"]},
    )
    merged = list(
        db.merge(
            [e1, different],
            merge_criteria=(mc.feature_type,),
        )
    )
    # Two distinct merge groups: one exon (e1), one CDS (different).
    assert len(merged) == 2
    fts = sorted(f.featuretype for f in merged)
    assert fts == ["CDS", "exon"]
