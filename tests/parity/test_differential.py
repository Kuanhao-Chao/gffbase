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
"""Behavioural parity: the same input through both libraries.

Structural parity (``test_api_parity.py``) proves the names line up. These
tests prove the *behaviour* does, which is the part that actually matters to
someone swapping one import for the other.

Every test is marked ``parity`` and needs gffutils installed::

    pytest -m parity

Known failures are marked ``xfail(strict=True)`` **per fixture**, with the
specific defect each one pins. Strict xfail means a fix cannot land unnoticed:
the moment the behaviour is corrected the test reports XPASS, which is a
failure, and the marker has to be removed in the same change.
"""

from __future__ import annotations

import pytest

from tests.parity import differential as D

pytestmark = pytest.mark.parity


@pytest.fixture(scope="module", autouse=True)
def _need_oracle():
    D.requires_gffutils()


# ---------------------------------------------------------------------------
# Corpus. These lists are measured, not aspirational.
# ---------------------------------------------------------------------------

SHARED_GFF3 = [
    "F3-unique-3.v2.gff",
    "c_elegans_WS199_ann_gff.txt",
    "gff_example1.gff3",
    "gms2_example.gff3",
    "hybrid1.gff3",
    "intro_docs_example.gff",
    "issue_197.gff",
    "jgi_gff2.txt",
    "keyval_sep_in_attrs.gff",
    "mouse_extra_comma.gff3",
    "nonascii",
]

SHARED_GTF = [
    "ensembl_gtf.txt",
    "gencode-v19.gtf",
    "issue174.gtf",
    "keep-order-test.gtf",
    "sharr.gtf",
]

#: Files the ORACLE ingests but gffbase rejects outright, and the validation
#: rule responsible. Each rule is stricter than the oracle *and* stricter than
#: real-world GFF3, which is what makes this a drop-in break rather than a
#: quality feature.
GFFBASE_REJECTS = {
    "FBgn0031208.gff": "InvalidPhase",
    "FBgn0031208.gtf": "InvalidPhase",
    "wormbase_gff2_alt.txt": "InvalidPhase",
    "issue167.gff": "TooFewFields",
    "unsanitized.gff": "InvalidCoordinate",
    "wormbase_gff2.txt": "InvalidAttribute",
}


# ---------------------------------------------------------------------------
# Known defects, keyed by fixture. Each string is one bug, described where the
# evidence for it lives.
# ---------------------------------------------------------------------------


_RAW_VS_NORMALIZED = (
    "Serialization model differs. The oracle re-serializes column 9 from its "
    "parsed model, normalizing as it goes: it adds GFF2 quotes the source "
    'omitted (`proteinId 873` -> `proteinId "873"`) and percent-encodes '
    "reserved characters the source left bare (`identity=99.58` -> "
    "`identity%3D99.58`). gffbase re-emits the original bytes, which is more "
    "faithful to the input but not identical to the oracle. Needs an explicit "
    "decision: match the oracle's normalized output and expose the raw line "
    "separately, or keep raw bytes and document the deviation."
)

_NULL_COORDS = (
    "Null coordinates are coerced to 0. A GFF row may legally carry `.` in "
    "columns 4 and 5; the oracle preserves that as `None`, but gffbase's Arrow "
    "batch builder maps it to 0 and the `features` DDL declares start/end NOT "
    "NULL, so the information cannot round-trip. Fixed by the nullable-"
    "coordinate work."
)

_DIALECT_VOTE_WEIGHTING = (
    "Dialect vote weighting differs. The oracle weights each sampled line by "
    "its number of attributes -- 'more complex attribute strings are more "
    "likely to be informative' (helpers._choose_dialect) -- so a six-attribute "
    "CDS line observing `; ` outvotes a one-attribute gene line observing `;`. "
    "gffbase gives every sampled line equal weight and breaks the resulting "
    "tie by first appearance, landing on `;`. The separator is then used when "
    "a feature is re-serialized, so the rendered text differs. Both are now "
    "deterministic (see tests/test_dialect_determinism.py); matching the "
    "oracle additionally requires adopting its weighting."
)

_EMPTY_VALUE_RENDERING = (
    "Rendering of an attribute with no values differs, and here the ORACLE is "
    "the lossy one. The source says `ID=`; gffutils re-serializes its "
    "`{'ID': []}` as a bare `ID`, dropping the `=`, so its round-trip does not "
    "reproduce the input. gffbase re-emits the original bytes, `ID=`. The "
    "parsed attributes now agree exactly (see the empty-value rule in "
    "`feature._drop_lone_empty_values`); only the rendering differs. Resolving "
    "this means deciding whether byte-faithful output or oracle-identical "
    "output wins -- the same open question as `_RAW_VS_NORMALIZED`."
)

_LOST_ESCAPING = (
    "Reading `feature.attributes` materializes decoded values and switches "
    "serialization off the raw-bytes fast path, but there is no re-encode "
    "step, so escapes are lost: `Note=hello%20world` re-emits as "
    "`Note=hello world`, and a value containing `;` or `,` produces "
    "structurally invalid GFF3. Fixed by the attribute-escaping work."
)


def _params(names, known: dict[str, str]):
    """Parametrize ``names``, strict-xfailing the ones with a known defect."""
    return [
        pytest.param(
            name,
            marks=pytest.mark.xfail(strict=True, reason=known[name]) if name in known else (),
            id=name,
        )
        for name in names
    ]


# ---------------------------------------------------------------------------
# Feature-level parity.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("name", _params(SHARED_GFF3, {}))
def test_gff3_feature_count_matches(name):
    """Same input, same number of features.

    The coarsest possible check, and the one most likely to catch a systematic
    identity bug: inventing or dropping rows changes the count even when every
    individual field looks right.
    """
    oracle, ours = D.build_both(D.fixture(name))
    assert ours.count_features_of_type() == oracle.count_features_of_type()


@pytest.mark.parametrize("name", _params(SHARED_GFF3, {}))
def test_gff3_feature_ids_match(name):
    oracle, ours = D.build_both(D.fixture(name))
    diff = D.compare_mappings(
        f"{name} feature ids",
        dict.fromkeys(D.feature_ids_in_order(oracle)),
        dict.fromkeys(D.feature_ids_in_order(ours)),
    )
    assert not diff, diff.report()


@pytest.mark.parametrize(
    "name", _params(SHARED_GFF3, {"c_elegans_WS199_ann_gff.txt": _NULL_COORDS})
)
def test_gff3_feature_fields_match(name):
    """All eight scalar GFF columns, per feature."""
    oracle, ours = D.build_both(D.fixture(name))
    theirs = D.features_by_id(oracle)
    mine = D.features_by_id(ours)
    shared = set(theirs) & set(mine)
    diff = D.compare_mappings(
        f"{name} fields",
        {k: {f: theirs[k][f] for f in D.GFF_FIELDS} for k in shared},
        {k: {f: mine[k][f] for f in D.GFF_FIELDS} for k in shared},
    )
    assert not diff, diff.report()


@pytest.mark.parametrize("name", _params(SHARED_GFF3, {}))
def test_gff3_attributes_match(name):
    oracle, ours = D.build_both(D.fixture(name))
    theirs = D.features_by_id(oracle)
    mine = D.features_by_id(ours)
    shared = set(theirs) & set(mine)
    diff = D.compare_mappings(
        f"{name} attributes",
        {k: theirs[k]["attributes"] for k in shared},
        {k: mine[k]["attributes"] for k in shared},
    )
    assert not diff, diff.report()


@pytest.mark.parametrize("name", _params(SHARED_GFF3, {}))
def test_gff3_featuretype_counts_match(name):
    oracle, ours = D.build_both(D.fixture(name))
    diff = D.compare_mappings(
        f"{name} featuretype counts",
        D.featuretype_counts(oracle),
        D.featuretype_counts(ours),
    )
    assert not diff, diff.report()


@pytest.mark.parametrize("name", _params(SHARED_GFF3, {}))
def test_gff3_relations_match(name):
    """Parent/child edges at every level.

    The two libraries store hierarchy differently -- a `relations` table versus
    a transitive closure -- so this compares what `children()` returns rather
    than what is on disk.
    """
    oracle, ours = D.build_both(D.fixture(name))
    theirs = D.relations(oracle)
    mine = D.relations(ours)
    diff = D.Diff(f"{name} relations")
    diff.only_oracle = sorted(f"{p}->{c}@{lvl}" for p, c, lvl in theirs - mine)
    diff.only_ours = sorted(f"{p}->{c}@{lvl}" for p, c, lvl in mine - theirs)
    assert not diff, diff.report()


@pytest.mark.parametrize("name", _params(SHARED_GFF3, {}))
def test_gff3_directives_match(name):
    oracle, ours = D.build_both(D.fixture(name))
    diff = D.compare_sequences(f"{name} directives", D.directives(oracle), D.directives(ours))
    assert not diff, diff.report()


# ---------------------------------------------------------------------------
# GTF. The identity model differs here, so these are separated out.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("name", _params(SHARED_GTF, {}))
def test_gtf_feature_count_matches(name):
    oracle, ours = D.build_both(D.fixture(name))
    assert ours.count_features_of_type() == oracle.count_features_of_type()


@pytest.mark.parametrize("name", _params(SHARED_GTF, {}))
def test_gtf_feature_ids_match(name):
    oracle, ours = D.build_both(D.fixture(name))
    diff = D.compare_mappings(
        f"{name} feature ids",
        dict.fromkeys(D.feature_ids_in_order(oracle)),
        dict.fromkeys(D.feature_ids_in_order(ours)),
    )
    assert not diff, diff.report()


# ---------------------------------------------------------------------------
# Dialect inference.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("name", _params([*SHARED_GFF3, *SHARED_GTF], {}))
def test_dialect_fmt_matches(name):
    """Whether a file is GFF3 or GTF must be agreed on.

    Only `fmt` is compared: the oracle's dialect dict carries presentation
    details (separator spelling, key order) that gffbase records differently,
    and those are covered by the serialization tests rather than by dict
    equality.
    """
    oracle, ours = D.build_both(D.fixture(name))
    assert ours.dialect.get("fmt") == oracle.dialect.get("fmt")


# ---------------------------------------------------------------------------
# Failure-mode parity.
# ---------------------------------------------------------------------------


def test_duplicate_ids_fail_the_same_way():
    """Real NCBI GFF3 repeats one ID across CDS/start_codon/stop_codon rows.

    The oracle rejects the whole file with a bare `ValueError("Duplicate ID
    ...")` -- note it exports `DuplicateIDError` but does not raise it. gffbase
    must be catchable the same way.
    """
    import gffbase
    import gffutils

    path = D.fixture("ncbi_gff3.txt")
    _, oracle_exc = D.capture(gffutils.create_db, path, ":memory:")
    _, our_exc = D.capture(gffbase.create_db, path, ":memory:")

    assert oracle_exc is not None, "fixture no longer triggers the oracle's duplicate-ID path"
    diff = D.compare_failures("ncbi_gff3 duplicate ids", oracle_exc, our_exc)
    assert not diff, diff.report()


# ---------------------------------------------------------------------------
# Serialization round-trip.
# ---------------------------------------------------------------------------

_SERIALIZE_UNTOUCHED_KNOWN = {
    "mouse_extra_comma.gff3": _EMPTY_VALUE_RENDERING,
    "jgi_gff2.txt": _RAW_VS_NORMALIZED,
    "keyval_sep_in_attrs.gff": _RAW_VS_NORMALIZED,
    "hybrid1.gff3": _RAW_VS_NORMALIZED,
    "c_elegans_WS199_ann_gff.txt": _RAW_VS_NORMALIZED,
}


@pytest.mark.parametrize("name", _params(SHARED_GFF3, _SERIALIZE_UNTOUCHED_KNOWN))
def test_untouched_features_serialize_identically(name):
    """`str(feature)` must match for a feature whose attributes were not read.

    gffbase keeps the raw column-9 bytes and re-emits them verbatim on this
    path, so wherever the source is already in canonical form the two agree.
    Where they disagree, the source was *not* canonical and the oracle
    normalized it -- see `_RAW_VS_NORMALIZED`.
    """
    oracle, ours = D.build_both(D.fixture(name))
    theirs = {f.id: str(f) for f in oracle.all_features()}
    mine = {f.id: str(f) for f in ours.all_features()}
    shared = set(theirs) & set(mine)
    diff = D.compare_mappings(
        f"{name} str(feature)",
        {k: theirs[k] for k in shared},
        {k: mine[k] for k in shared},
    )
    assert not diff, diff.report()


# Only the escaping defect survives attribute materialization. `hybrid1.gff3`
# and `jgi_gff2.txt` differ on the *raw* path only: once attributes are
# materialized gffbase normalizes them, which is what the oracle does
# unconditionally, so the two agree again.
_SERIALIZE_READ_KNOWN = {
    "mouse_extra_comma.gff3": _EMPTY_VALUE_RENDERING,
    "gms2_example.gff3": _DIALECT_VOTE_WEIGHTING,
    "c_elegans_WS199_ann_gff.txt": _LOST_ESCAPING,
    "keyval_sep_in_attrs.gff": _LOST_ESCAPING,
    "nonascii": _LOST_ESCAPING,
}


@pytest.mark.parametrize("name", _params(SHARED_GFF3, _SERIALIZE_READ_KNOWN))
def test_features_serialize_identically_after_reading_attributes(name):
    """The same check, but after forcing attribute materialization.

    This is the path real code takes -- read some attributes, write the feature
    back out -- and it is where escaping is currently lost.
    """
    oracle, ours = D.build_both(D.fixture(name))

    def render(db):
        out = {}
        for f in db.all_features():
            _ = dict(f.attributes)  # force materialization
            out[f.id] = str(f)
        return out

    theirs, mine = render(oracle), render(ours)
    shared = set(theirs) & set(mine)
    diff = D.compare_mappings(
        f"{name} str(feature) after attribute access",
        {k: theirs[k] for k in shared},
        {k: mine[k] for k in shared},
    )
    assert not diff, diff.report()


# ---------------------------------------------------------------------------
# Query parity.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("name", _params(SHARED_GFF3, {}))
def test_region_query_returns_the_same_features(name):
    """Region queries over the full extent of each seqid.

    Coordinates are 1-based closed in both libraries, and a query spanning
    everything is the strongest form of the check: any feature either library
    fails to index shows up immediately.
    """
    oracle, ours = D.build_both(D.fixture(name))
    for seqid in oracle.seqids():
        theirs = sorted(f.id for f in oracle.region(seqid=seqid, start=1, end=10**9))
        mine = sorted(f.id for f in ours.region(seqid=seqid, start=1, end=10**9))
        diff = D.compare_sequences(f"{name} region({seqid})", theirs, mine)
        assert not diff, diff.report()


@pytest.mark.parametrize("name", _params(SHARED_GFF3, {}))
def test_features_of_type_returns_the_same_features(name):
    oracle, ours = D.build_both(D.fixture(name))
    for featuretype in oracle.featuretypes():
        theirs = sorted(f.id for f in oracle.features_of_type(featuretype))
        mine = sorted(f.id for f in ours.features_of_type(featuretype))
        diff = D.compare_sequences(f"{name} features_of_type({featuretype})", theirs, mine)
        assert not diff, diff.report()


# ---------------------------------------------------------------------------
# The headline compatibility check.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("name", sorted(GFFBASE_REJECTS))
def test_rejected_fixture_is_rejected_for_the_recorded_reason(name):
    """Pin exactly which validation rule rejects each file.

    A characterization test, not an endorsement. It exists so the compat-mode
    work can be verified rule by rule: as each rule is relaxed, its entry is
    deleted from `GFFBASE_REJECTS` and the file joins the shared lists above.
    """
    import gffbase

    _, exc = D.capture(gffbase.create_db, D.fixture(name), ":memory:")
    assert exc is not None, (
        f"{name} now ingests -- remove it from GFFBASE_REJECTS and add it to "
        f"the SHARED_* list so the differential tests actually cover it"
    )
    assert getattr(exc, "kind", None) == GFFBASE_REJECTS[name], (
        f"{name} now fails with kind={getattr(exc, 'kind', None)!r}, "
        f"expected {GFFBASE_REJECTS[name]!r}"
    )


@pytest.mark.xfail(
    strict=True,
    reason=(
        "gffbase's parser validates to the NCBI GFF3 spec unconditionally, but "
        "create_db() is the drop-in entry point and real annotation files "
        "routinely violate that spec in ways the oracle accepts. Four rules are "
        "too strict for the compatibility path: InvalidPhase (a CDS row with "
        "'.' phase -- FlyBase and WormBase both emit these), TooFewFields "
        "(space-delimited GFF), InvalidCoordinate (end < start, which is "
        "precisely what the sanitize tooling exists to repair, so rejecting it "
        "makes sanitize impossible), and InvalidAttribute (GFF2 'key value' "
        "attributes with no '='). Six of the 23 fixtures the oracle loads are "
        "rejected outright, including FBgn0031208.gff, the canonical gffutils "
        "fixture. Fixed by the compat/strict mode work: compat collects these "
        "as warnings, strict keeps raising."
    ),
)
def test_ingest_parity_across_the_corpus():
    """Anything the oracle can ingest, gffbase must ingest too."""
    import gffbase
    import gffutils

    rejected = []
    for name in sorted(GFFBASE_REJECTS) + SHARED_GFF3 + SHARED_GTF:
        path = D.fixture(name)
        _, oracle_exc = D.capture(gffutils.create_db, path, ":memory:")
        if oracle_exc is not None:
            continue  # the oracle rejects it too; not a parity gap
        _, our_exc = D.capture(gffbase.create_db, path, ":memory:")
        if our_exc is not None:
            rejected.append(f"{name}: {getattr(our_exc, 'kind', type(our_exc).__name__)}")

    assert not rejected, "gffbase rejects files gffutils accepts:\n  " + "\n  ".join(rejected)


# ---------------------------------------------------------------------------
# create_db option fidelity.
#
# Every one of these was accepted and silently ignored before, so each test is
# checking that the option changes the database in the same way the oracle
# changes it -- not merely that it is tolerated.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("strategy", ["create_unique", "warning", "replace", "merge"])
def test_merge_strategy_matches(strategy):
    oracle, ours = D.build_both(D.fixture("ncbi_gff3.txt"), merge_strategy=strategy)
    diff = D.compare_mappings(
        f"merge_strategy={strategy} ids",
        dict.fromkeys(D.feature_ids_in_order(oracle)),
        dict.fromkeys(D.feature_ids_in_order(ours)),
    )
    assert not diff, diff.report()


def test_merge_strategy_error_is_the_default():
    """Both libraries reject `ncbi_gff3.txt` out of the box."""
    import gffbase
    import gffutils

    path = D.fixture("ncbi_gff3.txt")
    _, oracle_exc = D.capture(gffutils.create_db, path, ":memory:")
    _, our_exc = D.capture(gffbase.create_db, path, ":memory:")
    assert not D.compare_failures("default strategy", oracle_exc, our_exc)


def test_duplicates_table_matches_for_create_unique():
    """The renames recorded in `duplicates`, not just the resulting ids."""
    import gffbase
    import gffutils

    path = D.fixture("ncbi_gff3.txt")
    oracle = gffutils.create_db(path, ":memory:", merge_strategy="create_unique")
    ours = gffbase.create_db(path, ":memory:", merge_strategy="create_unique")

    theirs = sorted(tuple(r) for r in oracle.execute("SELECT idspecid, newid FROM duplicates"))
    mine = sorted(
        tuple(r) for r in ours.execute("SELECT original_id, new_id FROM duplicates").fetchall()
    )
    diff = D.compare_sequences("duplicates", theirs, mine)
    assert not diff, diff.report()


@pytest.mark.parametrize(
    ("name", "id_spec"),
    [
        ("gff_example1.gff3", "ID"),
        ("gff_example1.gff3", ["ID", "Name"]),
        ("gff_example1.gff3", ":seqid:"),
        ("gencode-v19.gtf", {"gene": "gene_id", "transcript": "transcript_id"}),
        pytest.param(
            "gencode-v19.gtf",
            "gene_id",
            marks=pytest.mark.xfail(
                strict=True,
                reason=(
                    "GTF synthesis ignores id_spec. The oracle applies the "
                    "id_spec to *inferred* gene/transcript rows as well as "
                    "authored ones, so under `id_spec='gene_id'` an inferred "
                    "transcript is keyed on gene_id and collapses into the "
                    "existing rows -- 4 transcripts either way, with or "
                    "without disable_infer_transcripts. gffbase's synthesis "
                    "keys inferred rows on the transcript_id attribute "
                    "unconditionally, so it adds 4 more (`ENST...`) for 8 "
                    "total. Fixed by the GTF synthesis work."
                ),
            ),
        ),
    ],
    ids=["str", "list", "gff-column", "dict", "gtf-single-key"],
)
def test_id_spec_produces_the_same_keys(name, id_spec):
    oracle, ours = D.build_both(D.fixture(name), id_spec=id_spec, merge_strategy="create_unique")
    diff = D.compare_mappings(
        f"{name} id_spec={id_spec!r}",
        dict.fromkeys(D.feature_ids_in_order(oracle)),
        dict.fromkeys(D.feature_ids_in_order(ours)),
    )
    assert not diff, diff.report()


def test_callable_id_spec_produces_the_same_keys():
    def upper_id(f):
        vals = f.attributes.get("ID")
        return vals[0].upper() if vals else None

    oracle, ours = D.build_both(D.fixture("gff_example1.gff3"), id_spec=upper_id)
    diff = D.compare_mappings(
        "callable id_spec",
        dict.fromkeys(D.feature_ids_in_order(oracle)),
        dict.fromkeys(D.feature_ids_in_order(ours)),
    )
    assert not diff, diff.report()


def test_transform_can_drop_features():
    """A falsy return drops the record in both libraries."""

    def drop_exons(f):
        if f.featuretype == "exon":
            return False
        return f

    oracle, ours = D.build_both(D.fixture("gff_example1.gff3"), transform=drop_exons)
    assert "exon" not in set(ours.featuretypes())
    diff = D.compare_mappings(
        "transform-filtered ids",
        dict.fromkeys(D.feature_ids_in_order(oracle)),
        dict.fromkeys(D.feature_ids_in_order(ours)),
    )
    assert not diff, diff.report()


def test_transform_can_modify_features():
    def relabel(f):
        f.source = "transformed"
        return f

    oracle, ours = D.build_both(D.fixture("gff_example1.gff3"), transform=relabel)
    assert {f.source for f in ours.all_features()} == {"transformed"}
    assert {f.source for f in oracle.all_features()} == {"transformed"}


def test_unknown_keyword_is_rejected_like_the_oracle():
    """gffutils' `deprecation_handler` raises TypeError; so must we."""
    import gffbase
    import gffutils

    path = D.fixture("gff_example1.gff3")
    _, oracle_exc = D.capture(gffutils.create_db, path, ":memory:", not_a_real_kwarg=1)
    _, our_exc = D.capture(gffbase.create_db, path, ":memory:", not_a_real_kwarg=1)
    assert isinstance(oracle_exc, TypeError)
    assert isinstance(our_exc, TypeError)


def test_positional_arguments_are_accepted_in_the_oracle_order():
    """`create_db(data, dbfn, id_spec, force, verbose, checklines, merge_strategy)`.

    gffbase previously made every option keyword-only, so any positional call
    written against gffutils raised TypeError.
    """
    import gffbase

    path = D.fixture("ncbi_gff3.txt")
    db = gffbase.create_db(path, ":memory:", None, False, False, 10, "create_unique")
    assert db.count_features_of_type() > 0
