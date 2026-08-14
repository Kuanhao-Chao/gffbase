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
    "FBgn0031208.gff",
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
    "unsanitized.gff",
    "wormbase_gff2.txt",
    "wormbase_gff2_alt.txt",
]

SHARED_GTF = [
    "FBgn0031208.gtf",
    "ensembl_gtf.txt",
    "gencode-v19.gtf",
    "issue174.gtf",
    "keep-order-test.gtf",
    "sharr.gtf",
]

#: Files that violate the GFF3 specification and are therefore rejected under
#: `mode="strict"`, with the rule responsible. Under the default
#: `mode="compat"` all of them load, exactly as they do under gffutils, with
#: the violation recorded in `FeatureDB.warnings`.
STRICT_MODE_REJECTS = {
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
    "INTENTIONAL DEVIATION -- serialization models differ, and the question is "
    "settled rather than open. The oracle re-serializes column 9 from its "
    "parsed model on every `str()`, normalizing as it goes: it adds GFF2 "
    'quotes the source omitted (`proteinId 873` -> `proteinId "873"`) and '
    "percent-encodes reserved characters the source left bare "
    "(`identity=99.58` -> `identity%3D99.58`). gffbase's `str()` re-emits the "
    "original bytes, so a file round-trips unchanged, and "
    "`to_line(normalized=True)` produces the oracle's form on demand. "
    "`test_normalized_rendering_matches_the_oracle` asserts that positively "
    "for these same fixtures, so this xfail marks a deliberate difference in "
    "the DEFAULT, not an unreachable output."
)

_DIALECT_INFERENCE = (
    "Dialect inference differs, so the re-rendered text does -- serialization "
    "is faithfully applying a different dialect, not misbehaving. Measured on "
    "`wormbase_gff2.txt`: gffbase infers `field separator='; '`, "
    "`repeated keys=True`, `semicolon in quotes=True` where the oracle infers "
    '`\' ; \'`, False, False. The source is `Sequence "cTel33B" ; Note "Clone '
    'cTel33B; Genbank AC199162"`, whose separator really is ` ; `; the `; ` '
    "*inside the quoted value* is being counted as evidence by gffbase's "
    "separator vote. Upstream avoids that with `quoted_semicolon_patterns`, "
    "which gffbase now has but wires only into the compatibility surface, not "
    "into the vote. On `wormbase_gff2_alt.txt` the difference is "
    "`trailing semicolon` alone. Both parse to IDENTICAL attributes -- "
    "verified -- so no data is lost either way."
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

_ATTR_KEY_WHITESPACE = (
    "INTENTIONAL DEVIATION -- gffbase strips whitespace around attribute keys "
    "and drops the empty key a trailing `;` produces; the oracle keeps both "
    "literally. This is not cosmetic: on `FBgn0031208.gff` line 84, "
    "`ID=CDS:Fk_gene_1:1; Parent=transcript_Fk_gene_1` uses `; ` while the "
    "file's inferred separator is `;`, so the oracle stores the key as "
    "`' Parent'` -- and because relationship building looks up `'Parent'`, it "
    "SILENTLY LOSES THE EDGE. `db.parents('CDS:Fk_gene_1:1')` returns [] under "
    "gffutils and ['Fk_gene_1', 'transcript_Fk_gene_1'] under gffbase. "
    "Compatibility mode preserves quirks, but not data-loss defects, so this "
    "one is deliberately not reproduced. Pinned by "
    "`test_stripped_attribute_keys_recover_an_edge_the_oracle_loses`."
)

#: Retained for the record: this was the reason the two escaping fixtures were
#: xfailed, and it is now fixed. Kept as documentation of what the fix bought,
#: since `test_features_serialize_identically_after_reading_attributes` passing
#: on `nonascii` is otherwise an unremarkable green dot.
_LOST_ESCAPING_FIXED = (
    "FIXED. Reading `feature.attributes` materialized decoded values and "
    "switched serialization off the raw-bytes fast path with no re-encode "
    "step, so escapes were lost: `Note=hello%20world` re-emitted as "
    "`Note=hello world`, and a value containing `;` or `,` produced "
    "structurally invalid GFF3 -- on `nonascii`, one attribute became five. "
    "`gffbase._serialize.encode_value` now re-encodes on that path."
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


@pytest.mark.parametrize("name", _params(SHARED_GFF3, {}))
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


@pytest.mark.parametrize(
    "name",
    _params(
        SHARED_GFF3,
        {
            "FBgn0031208.gff": _ATTR_KEY_WHITESPACE,
            "wormbase_gff2.txt": _ATTR_KEY_WHITESPACE,
            "wormbase_gff2_alt.txt": _ATTR_KEY_WHITESPACE,
        },
    ),
)
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


@pytest.mark.parametrize("name", _params(SHARED_GFF3, {"FBgn0031208.gff": _ATTR_KEY_WHITESPACE}))
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
    "wormbase_gff2.txt": _RAW_VS_NORMALIZED,
    "wormbase_gff2_alt.txt": _RAW_VS_NORMALIZED,
    "mouse_extra_comma.gff3": _EMPTY_VALUE_RENDERING,
    "jgi_gff2.txt": _RAW_VS_NORMALIZED,
    "keyval_sep_in_attrs.gff": _RAW_VS_NORMALIZED,
    "hybrid1.gff3": _RAW_VS_NORMALIZED,
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


#: The positive half of `_RAW_VS_NORMALIZED`. Every fixture xfailed above for
#: "gffbase does not normalize by default" is asserted HERE to produce the
#: oracle's exact bytes when asked to normalize -- otherwise that xfail would
#: be indistinguishable from "gffbase cannot produce the oracle's output".
#:
#: The two wormbase files are excluded for a different, measured reason: their
#: inferred dialects genuinely differ (see `_DIALECT_INFERENCE`), so faithful
#: serialization of a different dialect is expected to differ.
_NORMALIZED_KNOWN = {
    "wormbase_gff2.txt": _DIALECT_INFERENCE,
    "wormbase_gff2_alt.txt": _DIALECT_INFERENCE,
    "gms2_example.gff3": _DIALECT_VOTE_WEIGHTING,
    "FBgn0031208.gff": _ATTR_KEY_WHITESPACE,
}


@pytest.mark.parametrize("name", _params(SHARED_GFF3, _NORMALIZED_KNOWN))
def test_normalized_rendering_matches_the_oracle(name):
    """`to_line(normalized=True)` is the oracle's serialization model.

    gffutils re-renders column 9 from its parsed mapping on every `str()`;
    gffbase does that only on request, keeping `str()` byte-faithful. This
    test is what makes that a *choice of default* rather than a missing
    capability: asked for the normalized form, gffbase must produce the
    oracle's bytes exactly -- GFF2 quoting, percent-encoding, separators,
    valueless attributes and all.

    It is also the strongest single check on the `_reconstruct` port, since it
    compares full rendered lines across the whole shared corpus rather than
    the 18 hand-written cases in `attr_test_cases.py`.
    """
    oracle, ours = D.build_both(D.fixture(name))
    theirs = {f.id: str(f) for f in oracle.all_features()}
    mine = {f.id: f.to_line(normalized=True) for f in ours.all_features()}
    shared = set(theirs) & set(mine)
    assert shared, f"{name}: no overlapping ids to compare"
    diff = D.compare_mappings(
        f"{name} to_line(normalized=True)",
        {k: theirs[k] for k in shared},
        {k: mine[k] for k in shared},
    )
    assert not diff, diff.report()


# Only the escaping defect survives attribute materialization. `hybrid1.gff3`
# and `jgi_gff2.txt` differ on the *raw* path only: once attributes are
# materialized gffbase normalizes them, which is what the oracle does
# unconditionally, so the two agree again.
_SERIALIZE_READ_KNOWN = {
    "FBgn0031208.gff": _ATTR_KEY_WHITESPACE,
    "wormbase_gff2.txt": _ATTR_KEY_WHITESPACE,
    "wormbase_gff2_alt.txt": _ATTR_KEY_WHITESPACE,
    "gms2_example.gff3": _DIALECT_VOTE_WEIGHTING,
    # Three fixtures used to sit here and now pass:
    #
    # `keyval_sep_in_attrs.gff` and `nonascii` under _LOST_ESCAPING --
    # `_serialize.encode_value` re-encodes on the materialized path, so `%3B`
    # survives a read-then-write instead of splitting one attribute into five.
    #
    # `mouse_extra_comma.gff3` under _EMPTY_VALUE_RENDERING, which was not
    # expected: porting `_reconstruct` wholesale brought the valueless-attribute
    # rendering along with the encoder, so `{"ID": []}` now renders as a bare
    # `ID` here exactly as it does upstream. It stays xfailed on the *untouched*
    # path below, where gffbase re-emits the source's `ID=` byte-for-byte and
    # the oracle cannot.
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


@pytest.mark.parametrize("name", sorted(STRICT_MODE_REJECTS))
def test_strict_mode_rejects_specification_violations(name):
    """`mode="strict"` still enforces the full NCBI specification.

    Relaxing the compatibility path must not remove the strict checking --
    that is the whole point of having two profiles rather than one.
    """
    import gffbase

    _, exc = D.capture(gffbase.create_db, D.fixture(name), ":memory:", mode="strict")
    assert exc is not None, f"{name} no longer violates {STRICT_MODE_REJECTS[name]}"
    assert getattr(exc, "kind", None) == STRICT_MODE_REJECTS[name], (
        f"{name} fails with kind={getattr(exc, 'kind', None)!r}, "
        f"expected {STRICT_MODE_REJECTS[name]!r}"
    )


@pytest.mark.parametrize("name", sorted(STRICT_MODE_REJECTS))
def test_compat_mode_loads_what_strict_rejects_and_says_so(name):
    """The same file loads in compat mode, and the violation is reported.

    A compat-mode caller gets exactly gffutils' data *plus* a diagnostic
    gffutils never offered -- which is why relaxing the rules does not mean
    losing the information.
    """
    import gffbase

    db = gffbase.create_db(D.fixture(name), ":memory:")
    assert db.count_features_of_type() > 0
    kinds = {w["kind"] for w in db.warnings}
    assert STRICT_MODE_REJECTS[name] in kinds, (
        f"{name} loaded but did not report {STRICT_MODE_REJECTS[name]}; got {kinds}"
    )


def test_ingest_parity_across_the_corpus():
    """Anything the oracle can ingest, gffbase must ingest too.

    This is the headline compatibility check. It failed for six fixtures --
    including FBgn0031208.gff, the canonical gffutils fixture -- because the
    parser validated to the NCBI specification on the drop-in path.
    """
    import gffbase
    import gffutils

    rejected = []
    for name in sorted(STRICT_MODE_REJECTS) + SHARED_GFF3 + SHARED_GTF:
        path = D.fixture(name)
        _, oracle_exc = D.capture(gffutils.create_db, path, ":memory:")
        if oracle_exc is not None:
            continue  # the oracle rejects it too; not a parity gap
        _, our_exc = D.capture(gffbase.create_db, path, ":memory:")
        if our_exc is not None:
            rejected.append(f"{name}: {getattr(our_exc, 'kind', type(our_exc).__name__)}")

    assert not rejected, "gffbase rejects files gffutils accepts:\n  " + "\n  ".join(rejected)


def test_stripped_attribute_keys_recover_an_edge_the_oracle_loses():
    """The concrete payoff of not reproducing the oracle's key handling.

    `FBgn0031208.gff` line 84 separates its attributes with `; ` while the
    file's inferred separator is `;`. The oracle therefore stores the key as
    `' Parent'`, and since relationship building looks up `'Parent'`, the edge
    silently vanishes.
    """
    import gffbase
    import gffutils

    path = D.fixture("FBgn0031208.gff")
    fid = "CDS:Fk_gene_1:1"

    oracle = gffutils.create_db(path, ":memory:")
    assert " Parent" in dict(oracle[fid].attributes), (
        "fixture no longer exercises the oracle's unstripped-key path"
    )
    assert [f.id for f in oracle.parents(fid)] == [], "the oracle unexpectedly recovered the edge"

    ours = gffbase.create_db(path, ":memory:")
    assert "Parent" in dict(ours[fid].attributes)
    assert sorted(f.id for f in ours.parents(fid)) == [
        "Fk_gene_1",
        "transcript_Fk_gene_1",
    ]
