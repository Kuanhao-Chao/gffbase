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
"""Unit tests for ingestion policy: option validation and id_spec resolution.

These exercise `IdSpecResolver` directly rather than through `create_db`, so a
failure points at the policy rather than at everything downstream of it. The
expected values come from gffutils' `_id_handler` and `_increment_featuretype_autoid`.
"""

from __future__ import annotations

import pytest
from gffbase._options import (
    DEFAULT_ID_SPEC_GTF,
    IdSpecResolver,
    IngestOptions,
)
from gffbase.feature import ParsedFeature


def make_feature(featuretype="exon", attrs=(), **overrides):
    """A ParsedFeature with `attrs` as (key, value) pairs, index auto-assigned."""
    pairs = []
    counts: dict[str, int] = {}
    for key, value in attrs:
        idx = counts.get(key, 0)
        counts[key] = idx + 1
        pairs.append((key, value, idx))
    defaults = dict(
        seqid="chr1",
        source="src",
        featuretype=featuretype,
        start=1,
        end=100,
        score=".",
        strand="+",
        frame=".",
        attributes_blob=b"",
    )
    defaults.update(overrides)
    return ParsedFeature(attributes_pairs=pairs, **defaults)


# ---------------------------------------------------------------------------
# id_spec: the four supported shapes.
# ---------------------------------------------------------------------------


def test_string_id_spec_reads_that_attribute():
    r = IdSpecResolver("ID")
    auto: dict[str, int] = {}
    assert r.resolve(make_feature(attrs=[("ID", "e1")]), auto) == ("e1", "attribute")


def test_missing_attribute_falls_back_to_autoincrement():
    r = IdSpecResolver("ID")
    auto: dict[str, int] = {}
    assert r.resolve(make_feature(attrs=[("Parent", "p")]), auto) == ("exon_1", "autoincrement")
    assert r.resolve(make_feature(attrs=[("Parent", "p")]), auto) == ("exon_2", "autoincrement")
    assert auto == {"exon": 2}


def test_empty_attribute_value_is_treated_as_absent():
    """`ID=` must autoincrement, not produce an empty-string primary key.

    gffutils' attribute parser yields an empty *list* for such a key, so its
    `attributes[k][0]` raises IndexError and falls through. gffbase's parser
    preserves the empty string, so the emptiness has to be handled here.
    """
    r = IdSpecResolver("ID")
    auto: dict[str, int] = {}
    feat = make_feature(featuretype="protein", attrs=[("ID", ""), ("Parent", "x")])
    assert r.resolve(feat, auto) == ("protein_1", "autoincrement")


def test_list_id_spec_takes_the_first_key_that_resolves():
    r = IdSpecResolver(["ID", "Name", "gene_id"])
    auto: dict[str, int] = {}
    assert r.resolve(make_feature(attrs=[("Name", "n1")]), auto)[0] == "n1"
    assert r.resolve(make_feature(attrs=[("gene_id", "g1")]), auto)[0] == "g1"
    assert r.resolve(make_feature(attrs=[("ID", "i"), ("Name", "n")]), auto)[0] == "i"


def test_dict_id_spec_selects_by_featuretype():
    r = IdSpecResolver({"gene": "gene_id", "transcript": "transcript_id"})
    auto: dict[str, int] = {}
    gene = make_feature("gene", [("gene_id", "G1"), ("transcript_id", "T1")])
    tx = make_feature("transcript", [("gene_id", "G1"), ("transcript_id", "T1")])
    assert r.resolve(gene, auto)[0] == "G1"
    assert r.resolve(tx, auto)[0] == "T1"


def test_dict_id_spec_autoincrements_unlisted_featuretypes_immediately():
    """A featuretype absent from the mapping does NOT fall through to other keys.

    gffutils returns from the `KeyError` branch directly, so an `exon` row
    autoincrements even though it carries a `gene_id` the mapping mentions for
    a different featuretype.
    """
    r = IdSpecResolver({"gene": "gene_id"})
    auto: dict[str, int] = {}
    exon = make_feature("exon", [("gene_id", "G1")])
    assert r.resolve(exon, auto) == ("exon_1", "autoincrement")


def test_dict_id_spec_accepts_a_list_of_keys_per_featuretype():
    r = IdSpecResolver({"gene": ["missing", "gene_id"]})
    auto: dict[str, int] = {}
    assert r.resolve(make_feature("gene", [("gene_id", "G1")]), auto)[0] == "G1"


def test_callable_id_spec_receives_a_feature_shaped_object():
    seen = []

    def spec(f):
        seen.append((f.featuretype, f.seqid, dict(f.attributes)))
        return f.attributes["custom"][0].upper()

    r = IdSpecResolver(spec)
    auto: dict[str, int] = {}
    assert r.resolve(make_feature(attrs=[("custom", "abc")]), auto) == ("ABC", "attribute")
    assert seen == [("exon", "chr1", {"custom": ["abc"]})]


def test_callable_returning_falsy_falls_through_to_autoincrement():
    r = IdSpecResolver(lambda f: None)
    auto: dict[str, int] = {}
    assert r.resolve(make_feature(), auto) == ("exon_1", "autoincrement")


def test_callable_may_request_autoincrement_on_a_custom_base():
    r = IdSpecResolver(lambda f: "autoincrement:mybase")
    auto: dict[str, int] = {}
    assert r.resolve(make_feature(), auto) == ("mybase_1", "autoincrement")
    assert r.resolve(make_feature(), auto) == ("mybase_2", "autoincrement")


def test_colon_syntax_reads_a_gff_column_not_an_attribute():
    """`:seqid:` keys on column 1. Columns are scalars, so no `[0]`."""
    auto: dict[str, int] = {}
    assert IdSpecResolver(":seqid:").resolve(make_feature(), auto)[0] == "chr1"
    assert IdSpecResolver(":strand:").resolve(make_feature(), auto)[0] == "+"


def test_multi_valued_id_attribute_is_rejected():
    """A primary key must be single-valued; `Parent=a,b` cannot be one."""
    r = IdSpecResolver("Parent")
    auto: dict[str, int] = {}
    feat = make_feature(attrs=[("Parent", "a"), ("Parent", "b")])
    with pytest.raises(ValueError, match="more than one value"):
        r.resolve(feat, auto)


def test_uses_callable_is_only_true_when_a_callable_is_present():
    """Gates the per-row Feature adapter, so it must not over-report."""
    assert not IdSpecResolver("ID").uses_callable
    assert not IdSpecResolver(["ID", "Name"]).uses_callable
    assert not IdSpecResolver({"gene": "gene_id"}).uses_callable
    assert IdSpecResolver(lambda f: "x").uses_callable
    assert IdSpecResolver(["ID", lambda f: "x"]).uses_callable
    assert IdSpecResolver({"gene": lambda f: "x"}).uses_callable
    assert IdSpecResolver({"gene": ["a", lambda f: "x"]}).uses_callable


# ---------------------------------------------------------------------------
# Per-dialect defaults.
# ---------------------------------------------------------------------------


def test_default_id_spec_differs_by_dialect():
    opts = IngestOptions()
    assert opts.id_spec_for("gff3") == "ID"
    assert opts.id_spec_for("gtf") == DEFAULT_ID_SPEC_GTF


def test_explicit_id_spec_overrides_the_dialect_default():
    opts = IngestOptions(id_spec="Name")
    assert opts.id_spec_for("gff3") == "Name"
    assert opts.id_spec_for("gtf") == "Name"


def test_gtf_default_keys_genes_and_transcripts_on_their_real_ids():
    """The defect behind gencode-v19.gtf yielding 26 features against 21."""
    opts = IngestOptions()
    r = opts.resolver_for("gtf")
    auto: dict[str, int] = {}
    gene = make_feature("gene", [("gene_id", "G1"), ("transcript_id", "T1")])
    tx = make_feature("transcript", [("gene_id", "G1"), ("transcript_id", "T1")])
    exon = make_feature("exon", [("gene_id", "G1"), ("transcript_id", "T1")])
    assert r.resolve(gene, auto)[0] == "G1"
    assert r.resolve(tx, auto)[0] == "T1"
    # Exons are not in the mapping, so they autoincrement -- which is what
    # makes the synthesized gene/transcript rows unnecessary.
    assert r.resolve(exon, auto) == ("exon_1", "autoincrement")


# ---------------------------------------------------------------------------
# Option validation.
# ---------------------------------------------------------------------------


def test_invalid_merge_strategy_is_rejected():
    with pytest.raises(ValueError, match="Invalid merge strategy"):
        IngestOptions(merge_strategy="nonsense")


@pytest.mark.parametrize("strategy", ["error", "warning", "merge", "create_unique", "replace"])
def test_every_documented_merge_strategy_is_accepted(strategy):
    assert IngestOptions(merge_strategy=strategy).merge_strategy == strategy


@pytest.mark.parametrize("bad", ["start", "end"])
def test_force_merge_fields_rejects_coordinate_fields(bad):
    """Merging coordinates would mean joining integers into a string."""
    with pytest.raises(ValueError, match="must be integers"):
        IngestOptions(merge_strategy="merge", force_merge_fields=[bad])


def test_force_merge_fields_rejects_unknown_fields():
    with pytest.raises(ValueError, match="not a mergeable field"):
        IngestOptions(force_merge_fields=["nonsense"])


@pytest.mark.parametrize("risky", ["frame", "strand"])
def test_force_merge_fields_warns_on_semantically_risky_fields(risky):
    with pytest.warns(UserWarning, match="unusable features"):
        IngestOptions(merge_strategy="merge", force_merge_fields=[risky])


def test_force_dialect_check_conflicts_with_an_explicit_dialect():
    with pytest.raises(ValueError, match="force_dialect_check"):
        IngestOptions(force_dialect_check=True, dialect={"fmt": "gff3"})


def test_negative_checklines_is_rejected():
    with pytest.raises(ValueError, match="checklines"):
        IngestOptions(checklines=-1)


def test_infer_gene_extent_false_is_deprecated_and_sets_both_flags():
    with pytest.warns(FutureWarning, match="infer_gene_extent is deprecated"):
        opts = IngestOptions(infer_gene_extent=False)
    assert opts.disable_infer_genes
    assert opts.disable_infer_transcripts


def test_infer_gene_extent_true_is_silent_and_changes_nothing():
    import warnings as _w

    with _w.catch_warnings():
        _w.simplefilter("error")
        opts = IngestOptions(infer_gene_extent=True)
    assert not opts.disable_infer_genes
    assert not opts.disable_infer_transcripts
