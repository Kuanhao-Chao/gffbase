"""Strict merge and controller-derived comparison tests."""

from __future__ import annotations

import copy
import hashlib

import pytest

from benchmarks import cluster_campaign as campaign
from benchmarks.campaign import results
from benchmarks.common import benchmark_env
from tests._platform import LINUX_ONLY_CAMPAIGN
from tests.test_cluster_campaign import _make_campaign, _signature, _worker_result

pytestmark = LINUX_ONLY_CAMPAIGN


@pytest.fixture(scope="module")
def merge_fixture(tmp_path_factory: pytest.TempPathFactory) -> tuple[dict, list[dict], dict]:
    value = _make_campaign(tmp_path_factory.mktemp("campaign-merge"))
    worker_results = [_worker_result(value, job) for job in campaign._jobs(value)]
    merged = results.merge_campaign(value, worker_results)
    return value, worker_results, merged


def _resign(value: dict) -> None:
    unsigned = {key: item for key, item in value.items() if key != "results_sha256"}
    value["results_sha256"] = campaign.sha256_json(unsigned)


def test_merge_is_closed_and_contains_exact_namespaces_and_worker_evidence(
    merge_fixture: tuple[dict, list[dict], dict],
) -> None:
    value, _workers, merged = merge_fixture
    validated = results.validate_campaign_results(merged)

    assert validated["artifact_kind"] == "run-local"
    assert set(validated["worker_results"]) == {job.job_id for job in campaign._jobs(value)}
    assert validated["gates"]["namespace_cardinalities"] == {
        "primary": 5,
        "controls": 2,
        "scaling": 25,
        "bridges": 4,
    }
    assert set(validated["results"]["primary"]) == set(campaign.CORPUS_ORDER)
    assert all(job_id.startswith("scaling-") for job_id in validated["results"]["scaling"])
    assert all(job_id.startswith("bridge-") for job_id in validated["results"]["bridges"])
    assert all(
        evidence["primary_eligible"] is (job_id.startswith("canonical-"))
        for job_id, evidence in validated["evidence"].items()
    )


def test_worker_result_evidence_uses_exact_canonical_storage_digest(
    merge_fixture: tuple[dict, list[dict], dict],
) -> None:
    _value, workers, merged = merge_fixture
    worker = workers[0]
    expected = hashlib.sha256(
        campaign._campaign_safe_io.canonical_storage_bytes(worker)
    ).hexdigest()

    assert merged["evidence"][worker["job_id"]]["worker_result_sha256"] == expected


@pytest.mark.parametrize("schema", ["campaign-results-v1", "2", None])
def test_stale_merged_schemas_are_rejected(
    merge_fixture: tuple[dict, list[dict], dict], schema: object
) -> None:
    _value, _workers, merged = merge_fixture
    forged = copy.deepcopy(merged)
    forged["schema_version"] = schema
    _resign(forged)

    with pytest.raises(campaign.CampaignError, match="schema"):
        results.validate_campaign_results(forged)


def test_unknown_merged_fields_are_rejected_even_with_a_fresh_digest(
    merge_fixture: tuple[dict, list[dict], dict],
) -> None:
    _value, _workers, merged = merge_fixture
    forged = copy.deepcopy(merged)
    forged["legacy_v1_summary"] = {"ratio": 999.0}
    _resign(forged)

    with pytest.raises(campaign.CampaignError, match="unknown keys"):
        results.validate_campaign_results(forged)


def test_comparisons_and_gates_cannot_be_forged_and_resigned(
    merge_fixture: tuple[dict, list[dict], dict],
) -> None:
    _value, _workers, merged = merge_fixture
    forged = copy.deepcopy(merged)
    forged["comparisons"]["primary"]["mane"]["ratio"] = 999.0
    forged["gates"]["publishable"] = True
    _resign(forged)

    with pytest.raises(campaign.CampaignError, match="independently derived"):
        results.validate_campaign_results(forged)


def test_timed_out_primary_comparator_is_valid_censored_evidence(
    merge_fixture: tuple[dict, list[dict], dict],
) -> None:
    value, workers, _merged = merge_fixture
    modified = copy.deepcopy(workers)
    result = next(item for item in modified if item["job_id"] == "canonical-mane")
    job = next(job for job in campaign._jobs(value) if job.job_id == "canonical-mane")
    legacy = result["payload"]["legacy"]
    label = legacy["label"]
    legacy.clear()
    legacy.update(
        {
            "label": label,
            "peak_rss_bytes": 0,
            "peak_rss_mb": 0.0,
            "exit_code": -15,
            "state": "timed_out",
            "benchmark_env": benchmark_env(job.threads),
            "cap_seconds": job.parameters["legacy_timeout"],
            "wall_seconds": None,
            "disk_bytes": 0,
            "stdout_bytes": 0,
            "stdout_sha256": hashlib.sha256(b"").hexdigest(),
            "stdout_parse_error": "invalid final JSON object",
        }
    )
    result["payload"]["ingest_speedup"] = None

    merged = results.merge_campaign(value, modified)
    comparison = merged["comparisons"]["primary"]["mane"]
    assert comparison == {
        "state": "censored",
        "reason": "comparator-timed-out",
        "cap_seconds": job.parameters["legacy_timeout"],
        "signature_match": None,
        "feature_counts_match": None,
        "ratio": None,
        "candidate_engine": campaign.PUBLIC_VERSION,
        "comparator_engine": "gffutils-0.14",
        "primary_eligible": True,
    }
    assert merged["gates"]["primary_signature_matches"]["mane"] is None
    assert merged["gates"]["publishable"] is True
    assert not any("mane" in failure for failure in merged["gates"]["failures"])
    assert "lower_bound" not in str(merged)


def test_completed_signature_mismatch_has_no_ratio_and_blocks_publication(
    merge_fixture: tuple[dict, list[dict], dict],
) -> None:
    value, workers, _merged = merge_fixture
    modified = copy.deepcopy(workers)
    result = next(item for item in modified if item["job_id"] == "canonical-mane")
    result["payload"]["legacy"]["correctness_signature"] = _signature("b")
    result["payload"]["ingest_speedup"] = None

    merged = results.merge_campaign(value, modified)
    comparison = merged["comparisons"]["primary"]["mane"]
    assert comparison["state"] == "signature-mismatch"
    assert comparison["signature_match"] is False
    assert comparison["ratio"] is None
    assert merged["gates"]["publishable"] is False
    assert merged["gates"]["failures"] == ["primary signature mismatch: mane"]
