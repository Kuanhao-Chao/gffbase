"""Independent contract tests for benchmark database-signature-v3."""

from __future__ import annotations

import hashlib
import json

import duckdb
import gffutils
from gffbase import create_db

from benchmarks.common import database_signature, signatures_match, validate_database_signature

SOURCE = """\
chr1\tsrc\tgene\t1\t100\t.\t+\t.\tID=g1;Name=gene
chr1\tsrc\tmRNA\t1\t100\t.\t+\t.\tID=t1;Parent=g1
chr1\tsrc\tCDS\t10\t20\t.\t+\t0\tID=cds1;Parent=t1;Note=first
chr1\tsrc\tCDS\t40\t50\t.\t+\t2\tID=cds1;Parent=t1;Note=second
"""


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
    target = tmp_path / "split.sqlite"
    source.write_text(SOURCE)
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
    assert signature["feature_count"] == 3
    assert signature["direct_relationship_count"] == 2
    assert validate_database_signature(signature)
    assert (
        signatures_match(database_signature(_database(tmp_path), engine="gffbase"), signature)
        is True
    )


def test_signature_rejects_stale_combined_digest_and_compares_full_object():
    signature = _independent_signature()
    assert validate_database_signature(signature)

    altered = dict(signature)
    altered["closure_count"] += 1
    assert not validate_database_signature(altered)

    same_digest_different_payload = dict(signature)
    same_digest_different_payload["attributes_sha256"] = "0" * 64
    assert signatures_match(signature, same_digest_different_payload) is None


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
