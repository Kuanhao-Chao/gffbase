"""Adversarial unit tests for closed campaign worker-result evidence."""

from __future__ import annotations

import copy
import math

import pytest

from benchmarks import bridge
from benchmarks import cluster_campaign as campaign
from benchmarks.campaign import results
from tests._platform import LINUX_ONLY_CAMPAIGN
from tests.test_cluster_campaign import _make_campaign, _worker_result

pytestmark = LINUX_ONLY_CAMPAIGN


@pytest.fixture(scope="module")
def unit_campaign(tmp_path_factory: pytest.TempPathFactory) -> dict:
    return _make_campaign(tmp_path_factory.mktemp("campaign-results"))


def _job(value: dict, job_id: str) -> campaign.JobSpec:
    return next(job for job in campaign._jobs(value) if job.job_id == job_id)


def test_all_36_job_specific_worker_result_variants_are_accepted(unit_campaign: dict) -> None:
    jobs = campaign._jobs(unit_campaign)
    accepted = [
        results.validate_worker_result(unit_campaign, job, _worker_result(unit_campaign, job))
        for job in jobs
    ]

    assert len(accepted) == 36
    assert {job.kind for job in jobs} == {"primary", "control", "scaling", "bridge"}


def test_closed_failure_variant_is_terminal_but_not_merge_accepted(unit_campaign: dict) -> None:
    job = _job(unit_campaign, "scaling-t01-mane")
    result = _worker_result(unit_campaign, job)
    result.update(
        {
            "state": "failed",
            "exit_code": 3,
            "error": "child exited with status 3",
            "payload": {},
            "harness_environment": {},
            "validation": {"accepted": False, "failures": ["child exited with status 3"]},
        }
    )

    assert results.validate_terminal_worker_result(unit_campaign, job, result)["state"] == "failed"
    with pytest.raises(campaign.CampaignError, match="not accepted"):
        results.validate_worker_result(unit_campaign, job, result)


@pytest.mark.parametrize("schema", ["benchmark-worker-v1", "1", None])
def test_stale_worker_schemas_are_rejected(unit_campaign: dict, schema: object) -> None:
    job = _job(unit_campaign, "scaling-t01-mane")
    result = _worker_result(unit_campaign, job)
    result["schema_version"] = schema

    with pytest.raises(campaign.CampaignError, match="schema"):
        results.validate_worker_result(unit_campaign, job, result)


def test_worker_attempt_log_and_process_links_are_exact(unit_campaign: dict) -> None:
    job = _job(unit_campaign, "scaling-t01-mane")
    mutations = []
    wrong_attempt = _worker_result(unit_campaign, job)
    wrong_attempt["attempt_dir"] += "-forged"
    mutations.append(wrong_attempt)
    wrong_log = _worker_result(unit_campaign, job)
    wrong_log["stdout_log"] = "stdout.log"
    mutations.append(wrong_log)
    wrong_pane = _worker_result(unit_campaign, job)
    wrong_pane["worker_process"]["pid"] += 1
    mutations.append(wrong_pane)
    wrong_child = _worker_result(unit_campaign, job)
    wrong_child["child_process"]["pgid"] += 1
    mutations.append(wrong_child)

    for forged in mutations:
        with pytest.raises(campaign.CampaignError):
            results.validate_worker_result(unit_campaign, job, forged)


def test_success_requires_exact_affinity_interpreter_argv_and_environment(
    unit_campaign: dict,
) -> None:
    job = _job(unit_campaign, "scaling-t01-mane")
    forged_values = []
    wrong_affinity = _worker_result(unit_campaign, job)
    wrong_affinity["cpu_affinity"] = [49]
    forged_values.append(wrong_affinity)
    wrong_probe = _worker_result(unit_campaign, job)
    wrong_probe["interpreter_probe"]["python_version"] = "3.12.0"
    forged_values.append(wrong_probe)
    wrong_argv = _worker_result(unit_campaign, job)
    wrong_argv["argv"].append("--publish")
    forged_values.append(wrong_argv)
    wrong_environment = _worker_result(unit_campaign, job)
    wrong_environment["environment"]["OMP_NUM_THREADS"] = "2"
    forged_values.append(wrong_environment)

    for forged in forged_values:
        with pytest.raises(campaign.CampaignError):
            results.validate_worker_result(unit_campaign, job, forged)


def test_scaling_validation_requires_exact_10000_sample_coverage(unit_campaign: dict) -> None:
    job = _job(unit_campaign, "scaling-t01-mane")
    result = _worker_result(unit_campaign, job)
    validation = result["payload"]["gffbase"]["validation"]
    assert validation["requested_sample"] == "10000"

    validation["sample_checked"] -= 1
    with pytest.raises(campaign.CampaignError, match="candidate evidence"):
        results.validate_worker_result(unit_campaign, job, result)


def test_canonical_validation_requires_all_eligible_rows_and_inv12(unit_campaign: dict) -> None:
    job = _job(unit_campaign, "canonical-mane")
    result = _worker_result(unit_campaign, job)
    validation = result["payload"]["gffbase"]["validation"]
    assert validation["requested_sample"] == "all"

    validation["checked_ids"].remove("INV-12")
    validation["checked_ids"].append("INV-forged")
    with pytest.raises(campaign.CampaignError, match="candidate evidence"):
        results.validate_worker_result(unit_campaign, job, result)


def test_candidate_caps_controls_artifact_and_commit_are_bound(unit_campaign: dict) -> None:
    job = _job(unit_campaign, "canonical-mane")
    mutations = []
    wrong_cap = _worker_result(unit_campaign, job)
    wrong_cap["payload"]["gffbase"]["cap_seconds"] += 1
    mutations.append(wrong_cap)
    wrong_control = _worker_result(unit_campaign, job)
    wrong_control["payload"]["params"]["benchmark_env"]["OMP_NUM_THREADS"] = "2"
    mutations.append(wrong_control)
    wrong_artifact = _worker_result(unit_campaign, job)
    wrong_artifact["harness_environment"]["artifact"]["wheel_sha256"] = "0" * 64
    mutations.append(wrong_artifact)
    wrong_commit = _worker_result(unit_campaign, job)
    wrong_commit["harness_environment"]["git_commit"] = "0" * 40
    mutations.append(wrong_commit)

    for forged in mutations:
        with pytest.raises(campaign.CampaignError):
            results.validate_worker_result(unit_campaign, job, forged)


def test_query_records_are_candidate_gated_and_numerically_consistent(unit_campaign: dict) -> None:
    job = _job(unit_campaign, "canonical-mane")
    wrong_qps = _worker_result(unit_campaign, job)
    wrong_qps["payload"]["spatial"]["qps"] += 1.0
    with pytest.raises(campaign.CampaignError, match="spatial qps"):
        results.validate_worker_result(unit_campaign, job, wrong_qps)

    skipped = _worker_result(unit_campaign, job)
    skipped["payload"]["spatial"] = {"state": "skipped", "reason": "forged shortcut"}
    with pytest.raises(campaign.CampaignError, match="query evidence is incomplete"):
        results.validate_worker_result(unit_campaign, job, skipped)

    no_rtree = _worker_result(unit_campaign, job)
    no_rtree["payload"]["gffbase"]["rtree_built"] = False
    with pytest.raises(campaign.CampaignError, match="candidate evidence"):
        results.validate_worker_result(unit_campaign, job, no_rtree)


def test_nonfinite_measurements_are_rejected(unit_campaign: dict) -> None:
    job = _job(unit_campaign, "canonical-mane")
    for value in (math.nan, math.inf, -math.inf):
        result = _worker_result(unit_campaign, job)
        result["payload"]["gffbase"]["wall_seconds"] = value
        with pytest.raises(campaign.CampaignError, match="candidate evidence"):
            results.validate_worker_result(unit_campaign, job, result)


def test_timeout_comparator_is_censored_and_cannot_create_a_ratio(unit_campaign: dict) -> None:
    job = _job(unit_campaign, "canonical-mane")
    result = _worker_result(unit_campaign, job)
    legacy = result["payload"]["legacy"]
    legacy.clear()
    legacy.update(
        {
            "label": f"legacy gffutils ingest({job.input['path'].rsplit('/', 1)[-1]})",
            "peak_rss_bytes": 0,
            "peak_rss_mb": 0.0,
            "exit_code": -15,
            "state": "timed_out",
            "benchmark_env": campaign.benchmark_env(job.threads),
            "cap_seconds": job.parameters["legacy_timeout"],
            "wall_seconds": None,
            "disk_bytes": 0,
            "stdout_bytes": 0,
            "stdout_sha256": "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855",
            "stdout_parse_error": "invalid final JSON object",
        }
    )
    result["payload"]["ingest_speedup"] = 2.0

    with pytest.raises(campaign.CampaignError, match="carries a ratio"):
        results.validate_worker_result(unit_campaign, job, result)


def test_bridge_package_interpreter_affinity_input_and_database_are_bound(
    unit_campaign: dict,
) -> None:
    job = _job(unit_campaign, "bridge-gffbase-0.1.0-mane")
    mutations = []
    stale_schema = _worker_result(unit_campaign, job)
    stale_schema["payload"]["schema_version"] = "1"
    mutations.append(stale_schema)
    wrong_package = _worker_result(unit_campaign, job)
    wrong_package["payload"]["environment"]["package"]["version"] = "0.1.1"
    mutations.append(wrong_package)
    wrong_affinity = _worker_result(unit_campaign, job)
    wrong_affinity["payload"]["environment"]["cpu_affinity"] = [0]
    mutations.append(wrong_affinity)
    wrong_input = _worker_result(unit_campaign, job)
    wrong_input["payload"]["input"]["sha256"] = "0" * 64
    mutations.append(wrong_input)
    wrong_database = _worker_result(unit_campaign, job)
    wrong_database["payload"]["database"]["path"] += "-forged"
    mutations.append(wrong_database)

    for forged in mutations:
        with pytest.raises(campaign.CampaignError):
            results.validate_worker_result(unit_campaign, job, forged)


def test_bridge_signature_and_old_candidate_validation_must_be_complete(
    unit_campaign: dict,
) -> None:
    job = _job(unit_campaign, "bridge-gffbase-0.1.0-mane")
    invalid_signature = _worker_result(unit_campaign, job)
    invalid_signature["payload"]["measurement"]["correctness_signature"].pop("combined_sha256")
    with pytest.raises(campaign.CampaignError, match="signature"):
        results.validate_worker_result(unit_campaign, job, invalid_signature)

    failed_validation = _worker_result(unit_campaign, job)
    failed_validation["payload"]["measurement"]["validation"]["ok"] = False
    with pytest.raises(campaign.CampaignError, match="validation failed"):
        results.validate_worker_result(unit_campaign, job, failed_validation)


def test_unknown_nested_fields_fail_instead_of_being_projected_away(unit_campaign: dict) -> None:
    job = _job(unit_campaign, "canonical-mane")
    result = copy.deepcopy(_worker_result(unit_campaign, job))
    result["payload"]["gffbase"]["private_path"] = "/forged/private/database"

    with pytest.raises(campaign.CampaignError, match="candidate evidence"):
        results.validate_worker_result(unit_campaign, job, result)


def test_bridge_environment_is_historical_package_specific_and_closed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(bridge.os, "sched_getaffinity", lambda _pid: {0, 1})
    monkeypatch.setattr(bridge.platform, "node", lambda: "cluster.example")
    monkeypatch.setattr(bridge.platform, "platform", lambda: "Linux-unit")
    monkeypatch.setattr(bridge.platform, "machine", lambda: "x86_64")
    monkeypatch.setattr(bridge.platform, "python_version", lambda: "3.11.9")
    monkeypatch.setattr(bridge.platform, "python_implementation", lambda: "CPython")

    value = bridge.bridge_environment(package="gffutils", package_version="0.13", threads=2)
    assert set(value) == {
        "schema_version",
        "timestamp_utc",
        "hostname",
        "platform",
        "machine",
        "python",
        "package",
        "cpu_affinity",
        "benchmark_env",
    }
    assert value["schema_version"] == results.BRIDGE_ENVIRONMENT_SCHEMA
    assert value["package"] == {"name": "gffutils", "version": "0.13"}
    assert value["cpu_affinity"] == [0, 1]
    assert value["benchmark_env"] == campaign.benchmark_env(2)


def test_bridge_releases_a_database_handle_that_has_no_close_method() -> None:
    """gffbase 0.1.0 has no `close()`, no `__exit__` and no `__del__`.

    `bridge.py` released its handle behind `if hasattr(db, "close")`, which is
    False for the very version the version bridge exists to measure -- that
    missing method is one of the defects 0.2.0 fixed. So the write connection
    stayed open, DuckDB kept its exclusive lock, and the read-only connect in
    `database_signature` died with "Can't open a connection to same database
    file with a different configuration". Both `bridge-gffbase-0.1.0-*` jobs
    failed that way, after completing their measurement.

    0.1.0 does hold the connection as `db.conn`, so the release reaches past
    the public API when the method is absent -- deliberately, because the
    alternative is not measuring the previous release at all.
    """

    class Modern:
        def __init__(self) -> None:
            self.closed = False

        def close(self) -> None:
            self.closed = True

    class Connection:
        def __init__(self) -> None:
            self.closed = False

        def close(self) -> None:
            self.closed = True

    class Legacy:  # the 0.1.0 shape
        def __init__(self) -> None:
            self.conn = Connection()

    modern, legacy = Modern(), Legacy()
    bridge.release_database(modern)
    bridge.release_database(legacy)

    assert modern.closed is True
    assert legacy.conn.closed is True
    bridge.release_database(object())  # neither shape: must not raise


def test_bridge_environment_fails_when_linux_affinity_cannot_be_observed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def unavailable(_pid: int) -> set[int]:
        raise OSError("no affinity")

    monkeypatch.setattr(bridge.os, "sched_getaffinity", unavailable)
    with pytest.raises(RuntimeError, match="observable Linux CPU affinity"):
        bridge.bridge_environment(package="gffbase", package_version="0.1.0", threads=10)
