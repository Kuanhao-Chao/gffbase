"""Closed v2 artifact and reconstruction contracts for campaign model data."""

from __future__ import annotations

import copy
import hashlib
import json
from pathlib import Path

import pytest

from benchmarks import cluster_campaign as facade
from benchmarks.campaign import model, safe_io
from benchmarks.common import validate_database_signature


def _signature_v3() -> dict[str, object]:
    def digest(label: str) -> str:
        return hashlib.sha256(label.encode()).hexdigest()

    payload: dict[str, object] = {
        "schema_version": "database-signature-v3",
        "segment_count": 3,
        "segments_sha256": digest("segments"),
        "attribute_count": 4,
        "attributes_sha256": digest("attributes"),
        "direct_relationship_count": 1,
        "direct_relationships_sha256": digest("direct"),
        "closure_count": 1,
        "closure_sha256": digest("closure"),
        "feature_count": 2,
        "featuretype_histogram": [["gene", 1], ["mRNA", 1]],
    }
    return {
        **payload,
        "combined_sha256": hashlib.sha256(
            json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest(),
    }


def _inputs(root: Path) -> dict[str, dict[str, object]]:
    values = {
        key: {
            "path": str(root / f"{key}.gz"),
            "bytes": index + 1,
            "sha256": hashlib.sha256(key.encode()).hexdigest(),
            "gzip_crc_ok": True,
            "format": "gtf" if key == "gencode-gtf" else "gff3",
        }
        for index, key in enumerate(model.CORPUS_ORDER)
    }
    values["gencode-gtf-parent-stripped"] = {
        "path": str(root / "parent-stripped.gtf.gz"),
        "bytes": 10,
        "sha256": hashlib.sha256(b"derived").hexdigest(),
        "gzip_crc_ok": True,
        "format": "gtf",
        "manifest_path": str(root / "parent-stripped.manifest.json"),
        "manifest_sha256": hashlib.sha256(b"manifest").hexdigest(),
        "transform_version": "1",
        "counts": {
            "input_feature_lines": 12,
            "output_feature_lines": 10,
            "comment_or_blank_lines": 0,
            "removed_gene_rows": 1,
            "removed_transcript_rows": 1,
        },
    }
    return values


def _campaign(tmp_path: Path) -> dict[str, object]:
    interpreters = {
        role: {"resolved_executable": str(tmp_path / role / "python")}
        for role in model.INTERPRETER_ROLES
    }
    inputs = _inputs(tmp_path)
    parameters = model.binding_parameters()
    jobs = model.build_job_matrix(parameters, interpreters, inputs)
    spec = {
        "repo": {"commit": "a" * 40},
        "candidate": {"wheel": {"name": "candidate.whl", "sha256": "b" * 64}},
        "interpreters": interpreters,
        "inputs": inputs,
        "topology": model.topology(),
        "parameters": parameters,
        "resources": {"platform": "Linux"},
        "jobs": [job.to_dict() for job in jobs],
    }
    return {
        "schema_version": model.CAMPAIGN_SCHEMA,
        "run_id": "linux-unit",
        "created_utc": "2026-08-30T12:00:00Z",
        "spec_sha256": model.sha256_json(spec),
        "spec": spec,
    }


def _redigest(value: dict[str, object]) -> None:
    value["spec_sha256"] = model.sha256_json(value["spec"])


def test_contract_fixture_uses_complete_database_signature_v3() -> None:
    signature = _signature_v3()
    assert signature["schema_version"] == model.SIGNATURE_SCHEMA
    assert validate_database_signature(signature) is True


def test_exact_object_helper_rejects_missing_unknown_and_non_object() -> None:
    assert model.require_exact_keys({"a": 1, "b": 2}, {"a", "b"}, "fixture") == {
        "a": 1,
        "b": 2,
    }
    with pytest.raises(model.CampaignError, match="missing"):
        model.require_exact_keys({"a": 1}, {"a", "b"}, "fixture")
    with pytest.raises(model.CampaignError, match="unknown"):
        model.require_exact_keys({"a": 1, "b": 2, "c": 3}, {"a", "b"}, "fixture")
    with pytest.raises(model.CampaignError, match="object"):
        model.require_exact_keys([], set(), "fixture")  # type: ignore[arg-type]


@pytest.mark.parametrize(
    ("expected", "stale"),
    [
        ("campaign-v2", "campaign-v1"),
        ("campaign-preflight-v2", "campaign-preflight-v1"),
        ("campaign-status-v2", "campaign-status-v1"),
        ("campaign-launch-v2", "campaign-launch-v1"),
        ("benchmark-worker-v2", "benchmark-worker-v1"),
        ("campaign-results-v2", "campaign-results-v1"),
        ("results-index-v2", "results-index-v1"),
    ],
)
def test_schema_helper_rejects_stale_missing_and_legacy_key(expected: str, stale: str) -> None:
    assert model.require_schema({"schema_version": expected}, expected, "fixture") == {
        "schema_version": expected
    }
    for value in (
        {"schema_version": stale},
        {"schema": expected},
        {},
    ):
        with pytest.raises(model.CampaignError, match="schema_version"):
            model.require_schema(value, expected, "fixture")


def test_cluster_campaign_facade_reexports_the_model_authority() -> None:
    identity_names = (
        "CampaignError",
        "CpuLane",
        "JobSpec",
        "canonical_json_bytes",
        "sha256_json",
        "validate_identifier",
        "parse_cpu_list",
        "topology",
        "binding_parameters",
        "build_job_matrix",
        "job_digest",
        "build_mega_argv",
        "build_bridge_argv",
        "build_worker_argv",
        "build_tmux_argv",
        "worker_environment",
        "campaign_digest",
        "campaign_jobs",
        "validate_campaign_document",
    )
    for name in identity_names:
        assert getattr(facade, name) is getattr(model, name)
    for name in (
        "ROOT",
        "CAMPAIGN_SCHEMA",
        "PREFLIGHT_SCHEMA",
        "STATUS_SCHEMA",
        "WORKER_SCHEMA",
        "RESULTS_SCHEMA",
        "INDEX_SCHEMA",
        "LAUNCH_SCHEMA",
        "SIGNATURE_SCHEMA",
        "PUBLIC_VERSION",
        "CARGO_VERSION",
        "REGION_SEED",
        "CORPUS_ORDER",
        "THREADS",
        "LANE_CPUS",
    ):
        assert getattr(facade, name) == getattr(model, name)

    for name in (
        "atomic_create_json",
        "atomic_write_json_durable",
        "campaign_lock",
    ):
        assert getattr(facade, name) is getattr(safe_io, name)


def test_campaign_document_round_trips_and_reconstructs_exact_matrix(tmp_path: Path) -> None:
    value = _campaign(tmp_path)
    validated = model.validate_campaign_document(value)
    jobs = model.campaign_jobs(validated)

    assert validated == value
    assert len(jobs) == 36
    assert [job.to_dict() for job in jobs] == value["spec"]["jobs"]
    assert model.campaign_digest(value) == value["spec_sha256"]


@pytest.mark.parametrize(
    "mutation",
    [
        lambda value: value.__setitem__("schema_version", "campaign-v1"),
        lambda value: value.pop("created_utc"),
        lambda value: value.__setitem__("unknown", None),
        lambda value: value["spec"].pop("resources"),
        lambda value: value["spec"].__setitem__("unknown", None),
        lambda value: value["spec"].__setitem__("topology", {"lanes": []}),
        lambda value: value["spec"].__setitem__(
            "interpreters", {"primary": value["spec"]["interpreters"]["primary"]}
        ),
    ],
)
def test_campaign_document_rejects_stale_open_or_incomplete_shapes(
    tmp_path: Path, mutation
) -> None:
    value = _campaign(tmp_path)
    mutation(value)
    if value.get("schema_version") == model.CAMPAIGN_SCHEMA and "spec" in value:
        _redigest(value)
    with pytest.raises(model.CampaignError):
        model.validate_campaign_document(value)


def test_campaign_document_rejects_a_forged_spec_digest(tmp_path: Path) -> None:
    value = _campaign(tmp_path)
    value["spec_sha256"] = "0" * 64
    with pytest.raises(model.CampaignError, match="digest"):
        model.validate_campaign_document(value)


@pytest.mark.parametrize(
    "timestamp",
    [
        "2026-02-30T12:00:00Z",
        "2026-08-30T99:00:00Z",
        "2026-08-30T12:60:00Z",
        "2026-08-30T12:00:60Z",
        "2026-08-30T12:00:00+00:00",
    ],
)
def test_campaign_document_rejects_impossible_or_noncanonical_utc_timestamps(
    tmp_path: Path, timestamp: str
) -> None:
    value = _campaign(tmp_path)
    value["created_utc"] = timestamp
    with pytest.raises(model.CampaignError, match="created_utc"):
        model.validate_campaign_document(value)


def test_campaign_rejects_reordered_omitted_extra_or_forged_jobs(tmp_path: Path) -> None:
    mutations = []

    def reorder(value: dict[str, object]) -> None:
        jobs = value["spec"]["jobs"]
        jobs[0], jobs[1] = jobs[1], jobs[0]

    mutations.append(reorder)
    mutations.append(lambda value: value["spec"]["jobs"].pop())
    mutations.append(lambda value: value["spec"]["jobs"].append(value["spec"]["jobs"][0]))

    def forge(value: dict[str, object]) -> None:
        job = value["spec"]["jobs"][0]
        job["threads"] = 2
        unsigned = {key: item for key, item in job.items() if key != "job_sha256"}
        job["job_sha256"] = model.sha256_json(unsigned)

    mutations.append(forge)

    for mutation in mutations:
        value = _campaign(tmp_path)
        mutation(value)
        _redigest(value)
        with pytest.raises(model.CampaignError, match="matrix|job"):
            model.validate_campaign_document(value)


def test_campaign_rejects_bool_as_int_and_nested_unknown_keys(tmp_path: Path) -> None:
    value = _campaign(tmp_path)
    value["spec"]["parameters"]["repeats"] = True
    _redigest(value)
    with pytest.raises(model.CampaignError):
        model.validate_campaign_document(value)

    value = _campaign(tmp_path)
    value["spec"]["inputs"]["mane"]["unknown"] = "forgery"
    value["spec"]["jobs"][0]["input"]["unknown"] = "forgery"
    unsigned = {key: item for key, item in value["spec"]["jobs"][0].items() if key != "job_sha256"}
    value["spec"]["jobs"][0]["job_sha256"] = model.sha256_json(unsigned)
    _redigest(value)
    with pytest.raises(model.CampaignError):
        model.validate_campaign_document(value)


def test_runtime_path_is_allowed_only_by_pure_runtime_helpers(tmp_path: Path) -> None:
    value = _campaign(tmp_path)
    runtime = copy.deepcopy(value)
    runtime["_path"] = str(tmp_path / "campaign.json")

    with pytest.raises(model.CampaignError, match="unknown"):
        model.validate_campaign_document(runtime)
    assert len(model.campaign_jobs(runtime)) == 36
    assert model.campaign_digest(runtime) == value["spec_sha256"]

    runtime["another_runtime_field"] = True
    with pytest.raises(model.CampaignError):
        model.campaign_jobs(runtime)
