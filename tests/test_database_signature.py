"""Independent contract tests for benchmark database-signature-v3."""

from __future__ import annotations

import hashlib
import json
import sqlite3

import duckdb
import pytest
from gffbase import create_db

from benchmarks.common import database_signature, signatures_match, validate_database_signature

# The oracle lives in the `bench` extra, not `test` -- it is a pinned git
# checkout of gffutils, not a runtime dependency. A bare `import gffutils` here
# therefore failed at *collection* when the suite ran against an sdist
# installed with `[test]`, which aborts the entire run: 2,200 unrelated tests
# never execute because one module cannot import an optional comparator.
# `importorskip` turns that into a skip of this module alone.
gffutils = pytest.importorskip("gffutils")


SOURCE = """\
chr1\tsrc\tgene\t1\t100\t.\t+\t.\tID=g1;Name=gene
chr1\tsrc\tmRNA\t1\t100\t.\t+\t.\tID=t1;Parent=g1
chr1\tsrc\tCDS\t10\t20\t.\t+\t0\tID=cds1;Parent=t1;Note=first
chr1\tsrc\tCDS\t40\t50\t.\t+\t2\tID=cds1;Parent=t1;Note=second
"""

GTF_SOURCE = """\
chr1\tsrc\texon\t1\t10\t.\t+\t.\tgene_id "g"; transcript_id "t"; exon_number "1";
"""

EXPLICIT_GTF_SOURCE = """\
chr1\tsrc\tgene\t1\t100\t.\t+\t.\tgene_id "g";
chr1\tsrc\ttranscript\t1\t100\t.\t+\t.\tgene_id "g"; transcript_id "t";
chr1\tsrc\texon\t1\t20\t.\t+\t.\tgene_id "g"; transcript_id "t"; exon_number "1";
chr1\tsrc\texon\t40\t60\t.\t+\t.\tgene_id "g"; transcript_id "t"; exon_number "2";
chr1\tsrc\tCDS\t5\t15\t.\t+\t0\tgene_id "g"; transcript_id "t";
"""

EMPTY_SHA256 = hashlib.sha256(b"").hexdigest()


def _independent_signature(seed: str = "fixture") -> dict:
    """A valid v3 payload made without the production validation helpers."""

    def digest(label: str) -> str:
        return hashlib.sha256(f"{seed}:{label}".encode()).hexdigest()

    payload = {
        "schema_version": "database-signature-v3",
        "segment_count": 4,
        "segments_sha256": digest("segments"),
        "attribute_count": 7,
        "attributes_sha256": digest("attributes"),
        "direct_relationship_count": 2,
        "direct_relationships_sha256": digest("direct"),
        "closure_count": 3,
        "closure_sha256": digest("closure"),
        "feature_count": 3,
        "featuretype_histogram": [["CDS", 1], ["gene", 1], ["mRNA", 1]],
    }
    return {
        **payload,
        "combined_sha256": hashlib.sha256(
            json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest(),
    }


def _independently_recomputed_signature(signature: dict, **changes: object) -> dict:
    """Mutate a literal fixture and recompute its digest without production code."""

    payload = {key: value for key, value in signature.items() if key != "combined_sha256"}
    payload.update(changes)
    return {
        **payload,
        "combined_sha256": hashlib.sha256(
            json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest(),
    }


def _database(tmp_path):
    source = tmp_path / "split.gff3"
    target = tmp_path / "split.duckdb"
    source.write_text(SOURCE)
    db = create_db(str(source), str(target), force=True, force_gff=True, mode="strict")
    db.close()
    return target


def test_signature_v3_is_complete_and_internally_valid(tmp_path):
    signature = database_signature(_database(tmp_path), engine="gffbase")

    assert signature["schema_version"] == "database-signature-v3"
    assert signature["segment_count"] == 4
    assert signature["feature_count"] == 3
    assert signature["featuretype_histogram"] == [["CDS", 1], ["gene", 1], ["mRNA", 1]]
    assert validate_database_signature(signature)
    assert signatures_match(signature, dict(signature)) is True


def test_signature_supports_the_gffutils_physical_representation(tmp_path):
    source = tmp_path / "split.gff3"
    candidate = tmp_path / "split.duckdb"
    target = tmp_path / "split.sqlite"
    source.write_text(SOURCE)
    create_db(
        str(source),
        str(candidate),
        force=True,
        force_gff=True,
        merge_strategy="create_unique",
    ).close()
    gffutils.create_db(
        str(source),
        str(target),
        force=True,
        merge_strategy="create_unique",
        keep_order=False,
        sort_attribute_values=False,
    )

    signature = database_signature(target, engine="gffutils")
    assert signature["schema_version"] == "database-signature-v3"
    assert signature["segment_count"] == 4
    assert signature["feature_count"] == 4
    assert signature["direct_relationship_count"] == 3
    assert validate_database_signature(signature)
    assert signatures_match(database_signature(candidate, engine="gffbase"), signature) is True


@pytest.mark.parametrize(
    "rows",
    [
        "chr1\ts\tgene\t1\t10\t.\t+\t.\tID=x\nchr1\ts\texon\t1\t10\t.\t+\t.\tID=x\n",
        "chr1\ts\tgene\t1\t10\t.\t+\t.\tID=x\nchr2\ts\tgene\t20\t30\t.\t-\t.\tID=x\n",
    ],
    ids=("different-featuretype", "different-locus"),
)
def test_signature_preserves_create_unique_duplicate_identities(tmp_path, rows):
    source = tmp_path / "duplicates.gff3"
    candidate = tmp_path / "duplicates.duckdb"
    comparator = tmp_path / "duplicates.sqlite"
    source.write_text(rows)
    create_db(
        str(source),
        str(candidate),
        force=True,
        force_gff=True,
        merge_strategy="create_unique",
    ).close()
    gffutils.create_db(
        str(source),
        str(comparator),
        force=True,
        merge_strategy="create_unique",
        keep_order=False,
        sort_attribute_values=False,
        disable_infer_genes=True,
        disable_infer_transcripts=True,
    )

    candidate_signature = database_signature(candidate, engine="gffbase")
    comparator_signature = database_signature(comparator, engine="gffutils")

    assert candidate_signature["feature_count"] == comparator_signature["feature_count"] == 2
    assert signatures_match(candidate_signature, comparator_signature) is True


@pytest.mark.parametrize("infer_parents", [False, True])
def test_signature_is_engine_neutral_for_gtf_parent_modes(tmp_path, infer_parents):
    source = tmp_path / "one.gtf"
    candidate = tmp_path / "one.duckdb"
    comparator = tmp_path / "one.sqlite"
    source.write_text(GTF_SOURCE)
    db = create_db(
        str(source),
        str(candidate),
        force=True,
        force_gff=False,
        merge_strategy="create_unique",
        disable_infer_genes=not infer_parents,
        disable_infer_transcripts=not infer_parents,
    )
    db.close()
    gffutils.create_db(
        str(source),
        str(comparator),
        force=True,
        merge_strategy="create_unique",
        keep_order=False,
        sort_attribute_values=False,
        disable_infer_genes=not infer_parents,
        disable_infer_transcripts=not infer_parents,
    )

    candidate_signature = database_signature(candidate, engine="gffbase")
    comparator_signature = database_signature(comparator, engine="gffutils")

    assert signatures_match(candidate_signature, comparator_signature) is True
    expected_relations = (2, 3) if infer_parents else (0, 0)
    assert (
        candidate_signature["direct_relationship_count"],
        candidate_signature["closure_count"],
    ) == expected_relations


@pytest.mark.parametrize("infer_parents", [False, True])
def test_signature_ignores_gffutils_self_ancestry_for_explicit_gtf_parents(tmp_path, infer_parents):
    source = tmp_path / "explicit.gtf"
    candidate = tmp_path / "explicit.duckdb"
    comparator = tmp_path / "explicit.sqlite"
    source.write_text(EXPLICIT_GTF_SOURCE)
    create_db(
        str(source),
        str(candidate),
        force=True,
        merge_strategy="create_unique",
        disable_infer_genes=not infer_parents,
        disable_infer_transcripts=not infer_parents,
    ).close()
    gffutils.create_db(
        str(source),
        str(comparator),
        force=True,
        merge_strategy="create_unique",
        keep_order=False,
        sort_attribute_values=False,
        disable_infer_genes=not infer_parents,
        disable_infer_transcripts=not infer_parents,
    )

    raw = sqlite3.connect(comparator)
    try:
        self_relations = raw.execute(
            "SELECT parent, child, level FROM relations WHERE parent = child ORDER BY parent, level"
        ).fetchall()
    finally:
        raw.close()
    assert self_relations == [("g", "g", 2), ("t", "t", 1)]

    candidate_signature = database_signature(candidate, engine="gffbase")
    comparator_signature = database_signature(comparator, engine="gffutils")

    assert (
        candidate_signature["direct_relationship_count"],
        candidate_signature["closure_count"],
    ) == (4, 7)
    assert signatures_match(candidate_signature, comparator_signature) is True


@pytest.mark.parametrize(
    ("kind", "rows", "artifact"),
    [
        (
            "gene",
            'chr1\tsrc\tgene\t1\t100\t.\t+\t.\tgene_id "g";\n'
            'chr2\tgffbase_derived\tgene\t1\t100\t.\t+\t.\tgene_id "g";\n',
            ("g", "g_1", 2),
        ),
        (
            "transcript",
            'chr1\tsrc\ttranscript\t1\t100\t.\t+\t.\ttranscript_id "t";\n'
            'chr2\tgffbase_derived\ttranscript\t1\t100\t.\t+\t.\ttranscript_id "t";\n',
            ("t", "t_1", 1),
        ),
    ],
)
def test_signature_handles_duplicate_explicit_gtf_parent_artifacts(tmp_path, kind, rows, artifact):
    """create_unique artifacts are identified by authored attributes, not dbid equality."""

    source = tmp_path / f"duplicate-{kind}.gtf"
    candidate = tmp_path / f"duplicate-{kind}.duckdb"
    comparator = tmp_path / f"duplicate-{kind}.sqlite"
    source.write_text(rows)
    create_db(
        str(source),
        str(candidate),
        force=True,
        merge_strategy="create_unique",
        disable_infer_genes=True,
        disable_infer_transcripts=True,
    ).close()
    gffutils.create_db(
        str(source),
        str(comparator),
        force=True,
        merge_strategy="create_unique",
        keep_order=False,
        sort_attribute_values=False,
        disable_infer_genes=True,
        disable_infer_transcripts=True,
    )

    raw = sqlite3.connect(comparator)
    try:
        assert (
            artifact
            in raw.execute(
                "SELECT parent, child, level FROM relations ORDER BY parent, child, level"
            ).fetchall()
        )
    finally:
        raw.close()

    candidate_signature = database_signature(candidate, engine="gffbase")
    comparator_signature = database_signature(comparator, engine="gffutils")
    assert signatures_match(candidate_signature, comparator_signature) is True

    candidate_con = duckdb.connect(str(candidate))
    candidate_con.execute("LOAD spatial")
    candidate_con.execute(
        "UPDATE features SET source = 'source-mutated' "
        "WHERE featuretype = ? AND source = 'gffbase_derived'",
        [kind],
    )
    candidate_con.close()
    changed = database_signature(candidate, engine="gffbase")

    assert changed["segments_sha256"] != comparator_signature["segments_sha256"]
    assert signatures_match(changed, comparator_signature) is False


def test_signature_includes_empty_attributes_and_ordered_values(tmp_path):
    source = tmp_path / "flags.gff3"
    candidate = tmp_path / "flags.duckdb"
    comparator = tmp_path / "flags.sqlite"
    source.write_text("chr1\ts\tgene\t1\t10\t.\t+\t.\tZZ=z;ID=g;Flag;AA=a,b\n")
    create_db(str(source), str(candidate), force=True, force_gff=True).close()
    gffutils.create_db(
        str(source),
        str(comparator),
        force=True,
        merge_strategy="create_unique",
        keep_order=False,
        sort_attribute_values=False,
    )

    candidate_signature = database_signature(candidate, engine="gffbase")
    comparator_signature = database_signature(comparator, engine="gffutils")

    assert candidate_signature["attribute_count"] == 5
    assert signatures_match(candidate_signature, comparator_signature) is True


def test_signature_treats_attribute_key_order_as_nonsemantic_but_preserves_value_order(tmp_path):
    first = tmp_path / "first.gff3"
    reordered = tmp_path / "reordered.gff3"
    values_reordered = tmp_path / "values-reordered.gff3"
    first.write_text("chr1\ts\tgene\t1\t10\t.\t+\t.\tID=g;Name=n;Alias=a,b\n")
    reordered.write_text("chr1\ts\tgene\t1\t10\t.\t+\t.\tAlias=a,b;Name=n;ID=g\n")
    values_reordered.write_text("chr1\ts\tgene\t1\t10\t.\t+\t.\tID=g;Name=n;Alias=b,a\n")

    paths = []
    for source in (first, reordered, values_reordered):
        target = source.with_suffix(".duckdb")
        create_db(str(source), str(target), force=True, force_gff=True).close()
        paths.append(target)
    baseline, key_reordered, value_reordered = (
        database_signature(path, engine="gffbase") for path in paths
    )

    assert signatures_match(baseline, key_reordered) is True
    assert signatures_match(baseline, value_reordered) is False


@pytest.mark.parametrize("engine", ["gffbase", "gffutils"])
@pytest.mark.parametrize("suffix", ["gff3", "gtf"])
def test_signature_preserves_authored_reserved_source_labels(tmp_path, engine, suffix):
    source = tmp_path / f"authored.{suffix}"
    target = tmp_path / ("authored.duckdb" if engine == "gffbase" else "authored.sqlite")
    if suffix == "gff3":
        source.write_text("chr1\tgffbase_derived\tgene\t1\t10\t.\t+\t.\tID=g\n")
    else:
        source.write_text('chr1\tgffbase_derived\tgene\t1\t10\t.\t+\t.\tgene_id "g";\n')
    if engine == "gffbase":
        create_db(str(source), str(target), force=True, force_gff=suffix == "gff3").close()
        baseline = database_signature(target, engine=engine)
        con = duckdb.connect(str(target))
        con.execute("LOAD spatial")
        con.execute("UPDATE features SET source = 'gffutils_derived'")
        con.close()
    else:
        gffutils.create_db(
            str(source),
            str(target),
            force=True,
            merge_strategy="create_unique",
            keep_order=False,
            sort_attribute_values=False,
            disable_infer_genes=True,
            disable_infer_transcripts=True,
        )
        baseline = database_signature(target, engine=engine)
        con = sqlite3.connect(target)
        con.execute("UPDATE features SET source = 'gffutils_derived'")
        con.commit()
        con.close()

    changed = database_signature(target, engine=engine)

    assert changed["segments_sha256"] != baseline["segments_sha256"]
    assert signatures_match(baseline, changed) is False


@pytest.mark.parametrize("featuretype", ["exon", "CDS"])
def test_signature_retains_semantic_gtf_self_relations_across_engines(tmp_path, featuretype):
    source = tmp_path / "explicit-self.gtf"
    candidate = tmp_path / "explicit-self.duckdb"
    comparator = tmp_path / "explicit-self.sqlite"
    source.write_text(EXPLICIT_GTF_SOURCE)
    create_db(
        str(source),
        str(candidate),
        force=True,
        merge_strategy="create_unique",
        disable_infer_genes=True,
        disable_infer_transcripts=True,
    ).close()
    gffutils.create_db(
        str(source),
        str(comparator),
        force=True,
        merge_strategy="create_unique",
        keep_order=False,
        sort_attribute_values=False,
        disable_infer_genes=True,
        disable_infer_transcripts=True,
    )
    before = database_signature(candidate, engine="gffbase")

    candidate_con = duckdb.connect(str(candidate))
    feature_id = candidate_con.execute(
        "SELECT id FROM features WHERE featuretype = ? ORDER BY id LIMIT 1", [featuretype]
    ).fetchone()[0]
    candidate_con.execute("INSERT INTO edges VALUES (?, ?)", [feature_id, feature_id])
    candidate_con.execute("INSERT INTO closure VALUES (?, ?, 1)", [feature_id, feature_id])
    candidate_con.close()

    comparator_con = sqlite3.connect(comparator)
    comparator_id = comparator_con.execute(
        "SELECT id FROM features WHERE featuretype = ? ORDER BY id LIMIT 1", [featuretype]
    ).fetchone()[0]
    assert comparator_id == feature_id
    comparator_con.execute("INSERT INTO relations VALUES (?, ?, 1)", [feature_id, feature_id])
    comparator_con.commit()
    comparator_con.close()

    candidate_signature = database_signature(candidate, engine="gffbase")
    comparator_signature = database_signature(comparator, engine="gffutils")

    assert candidate_signature["direct_relationship_count"] == (
        before["direct_relationship_count"] + 1
    )
    assert candidate_signature["closure_count"] == before["closure_count"] + 1
    assert signatures_match(candidate_signature, comparator_signature) is True


def test_signature_retains_gff3_relationships_to_unresolved_parents(tmp_path):
    source = tmp_path / "dangling.gff3"
    candidate = tmp_path / "dangling.duckdb"
    comparator = tmp_path / "dangling.sqlite"
    source.write_text("chr1\ts\texon\t1\t10\t.\t+\t.\tID=e;Parent=missing\n")
    create_db(str(source), str(candidate), force=True, force_gff=True).close()
    gffutils.create_db(
        str(source),
        str(comparator),
        force=True,
        merge_strategy="create_unique",
        keep_order=False,
        sort_attribute_values=False,
        disable_infer_genes=True,
        disable_infer_transcripts=True,
    )

    candidate_signature = database_signature(candidate, engine="gffbase")
    comparator_signature = database_signature(comparator, engine="gffutils")

    assert candidate_signature["direct_relationship_count"] == 1
    assert candidate_signature["closure_count"] == 1
    assert signatures_match(candidate_signature, comparator_signature) is True


def test_signature_rejects_stale_combined_digest_and_compares_full_object():
    signature = _independent_signature()
    assert validate_database_signature(signature)

    altered = dict(signature)
    altered["closure_count"] += 1
    assert not validate_database_signature(altered)

    same_digest_different_payload = dict(signature)
    same_digest_different_payload["attributes_sha256"] = "0" * 64
    assert signatures_match(signature, same_digest_different_payload) is None


@pytest.mark.parametrize(
    "changes",
    [
        {"segment_count": 2},
        {"closure_count": 1},
        {"featuretype_histogram": [["CDS", 1], ["gene", 1], ["mRNA", 1], ["zero", 0]]},
        {"featuretype_histogram": [["CDS", -1], ["gene", 2], ["mRNA", 2]]},
    ],
    ids=("fewer-segments-than-features", "closure-smaller-than-direct", "zero-bin", "negative-bin"),
)
def test_signature_rejects_independently_digest_consistent_impossible_counts(changes):
    forged = _independently_recomputed_signature(_independent_signature(), **changes)

    assert validate_database_signature(forged) is False
    assert signatures_match(forged, dict(forged)) is None


def test_empty_signature_histogram_is_valid_only_for_an_empty_database():
    empty = _independently_recomputed_signature(
        _independent_signature(),
        segment_count=0,
        attribute_count=0,
        direct_relationship_count=0,
        closure_count=0,
        feature_count=0,
        featuretype_histogram=[],
        segments_sha256=EMPTY_SHA256,
        attributes_sha256=EMPTY_SHA256,
        direct_relationships_sha256=EMPTY_SHA256,
        closure_sha256=EMPTY_SHA256,
    )

    assert validate_database_signature(empty) is True


@pytest.mark.parametrize(
    "changes",
    [
        {"feature_count": 0, "featuretype_histogram": [], "segment_count": 1},
        {
            "feature_count": 0,
            "featuretype_histogram": [],
            "segment_count": 0,
            "attribute_count": 1,
        },
        {"direct_relationship_count": 0, "closure_count": 1},
        {"direct_relationship_count": 1, "closure_count": 0},
    ],
    ids=(
        "empty-features-with-segment",
        "attributes-without-segment",
        "closure-without-direct",
        "direct-without-closure",
    ),
)
def test_signature_rejects_digest_consistent_cross_count_impossibilities(changes):
    forged = _independently_recomputed_signature(_independent_signature(), **changes)

    assert validate_database_signature(forged) is False


@pytest.mark.parametrize(
    ("count_name", "digest_name"),
    [
        ("segment_count", "segments_sha256"),
        ("attribute_count", "attributes_sha256"),
        ("direct_relationship_count", "direct_relationships_sha256"),
        ("closure_count", "closure_sha256"),
    ],
)
def test_signature_requires_empty_stream_digest_for_zero_component_count(count_name, digest_name):
    forged = _independently_recomputed_signature(
        _independent_signature(),
        **{count_name: 0, digest_name: hashlib.sha256(b"not empty").hexdigest()},
    )

    assert validate_database_signature(forged) is False


@pytest.mark.parametrize(
    "digest_name",
    [
        "segments_sha256",
        "attributes_sha256",
        "direct_relationships_sha256",
        "closure_sha256",
    ],
)
def test_signature_rejects_empty_stream_digest_for_nonzero_component_count(digest_name):
    forged = _independently_recomputed_signature(
        _independent_signature(), **{digest_name: EMPTY_SHA256}
    )

    assert validate_database_signature(forged) is False


def test_signature_changes_for_independent_semantic_mutations(tmp_path):
    target = _database(tmp_path)
    baseline = database_signature(target, engine="gffbase")

    def mutate(sql, params=None):
        con = duckdb.connect(str(target))
        con.execute("LOAD spatial")
        con.execute(sql, params or [])
        con.close()

    mutate("UPDATE features SET start = 2 WHERE id = 'g1'")
    coordinate = database_signature(target, engine="gffbase")
    assert coordinate["segments_sha256"] != baseline["segments_sha256"]

    mutate(
        "UPDATE segments SET attributes_blob = ? WHERE feature_id = 'cds1' AND seg_idx = 1",
        [b"ID=cds1;Parent=t1;Note=changed"],
    )
    segment_attribute = database_signature(target, engine="gffbase")
    assert segment_attribute["attributes_sha256"] != coordinate["attributes_sha256"]

    mutate("INSERT INTO edges VALUES ('g1', 'cds1')")
    direct_relation = database_signature(target, engine="gffbase")
    assert (
        direct_relation["direct_relationships_sha256"]
        != segment_attribute["direct_relationships_sha256"]
    )

    mutate("UPDATE closure SET depth = 7 WHERE ancestor = 'g1' AND descendant = 'cds1'")
    closure_depth = database_signature(target, engine="gffbase")
    assert closure_depth["closure_sha256"] != direct_relation["closure_sha256"]

    mutate("DELETE FROM features WHERE id = 'g1'")
    count = database_signature(target, engine="gffbase")
    assert count["feature_count"] == closure_depth["feature_count"] - 1

    mutate("UPDATE features SET featuretype = 'lncRNA' WHERE id = 't1'")
    histogram = database_signature(target, engine="gffbase")
    assert histogram["featuretype_histogram"] != count["featuretype_histogram"]


def test_gffutils_signature_streams_source_rows_in_bounded_batches(tmp_path, monkeypatch):
    target = tmp_path / "large.sqlite"
    con = sqlite3.connect(target)
    con.executescript(
        """
        CREATE TABLE features (
            id TEXT PRIMARY KEY, seqid TEXT, source TEXT, featuretype TEXT,
            start INT, end INT, score TEXT, strand TEXT, frame TEXT,
            attributes TEXT, extra TEXT, bin INT
        );
        CREATE TABLE relations (parent TEXT, child TEXT, level INT);
        CREATE TABLE meta (dialect TEXT, version TEXT);
        INSERT INTO meta VALUES ('{"fmt":"gff3"}', 'test');
        """
    )
    con.executemany(
        "INSERT INTO features VALUES (?, 'chr1', 'src', 'gene', ?, ?, '.', '+', '.', ?, '[]', 0)",
        ((f"g{index}", index, index, json.dumps({"ID": [f"g{index}"]})) for index in range(10_001)),
    )
    con.commit()
    con.close()

    real_connect = sqlite3.connect
    batches: list[int] = []

    class StreamingCursor:
        def __init__(self, cursor):
            self.cursor = cursor

        def fetchmany(self, size):
            batches.append(size)
            return self.cursor.fetchmany(size)

        def fetchall(self):
            raise AssertionError("source tables must not be materialized with fetchall()")

    class StreamingConnection:
        def __init__(self, connection):
            self.connection = connection

        def execute(self, sql, params=()):
            return StreamingCursor(self.connection.execute(sql, params))

        def close(self):
            self.connection.close()

    def guarded_connect(database, *args, **kwargs):
        connection = real_connect(database, *args, **kwargs)
        return StreamingConnection(connection) if kwargs.get("uri") else connection

    monkeypatch.setattr(sqlite3, "connect", guarded_connect)

    signature = database_signature(target, engine="gffutils")

    assert signature["feature_count"] == 10_001
    assert len(batches) >= 3  # two feature batches plus the empty relation cursor
    assert batches.count(10_000) >= 3
    assert set(batches) == {1, 10_000}  # one-row dialect probe plus bounded data batches
