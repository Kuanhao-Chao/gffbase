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
"""Closed validation and derivation contracts for benchmark campaign results.

The measurement processes write private, run-local evidence.  This module is
the trust boundary that accepts those immutable attempt results and later
derives merged/public artifacts.  Validation is deliberately pure: filesystem
inventory, immutable writes, and status/result linkage stay in the controller
and safe-I/O layers.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
from collections.abc import Mapping, Sequence
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, cast

from benchmarks.campaign import model, safe_io, worker
from benchmarks.common import (
    benchmark_bounded_environment_is_valid,
    benchmark_candidate_evidence_is_valid,
    benchmark_comparator_evidence_is_valid,
    benchmark_env,
    benchmark_environment_evidence_is_valid,
    benchmark_query_evidence_error,
    signatures_match,
    validate_database_signature,
)
from benchmarks.corpora import BY_KEY

BRIDGE_SCHEMA = "benchmark-bridge-v2"
BRIDGE_ENVIRONMENT_SCHEMA = "benchmark-bridge-environment-v1"
HISTORICAL_MAC_SHA256 = "d215d19fcf67d226dda401ed9069c75d524494904faced588f23cc81712db53d"
HISTORICAL_MAC_RUN_ID = "macos-20260815-legacy"

_WORKER_RESULT_KEYS = {
    "schema_version",
    "run_id",
    "campaign_sha256",
    "job_id",
    "job_sha256",
    "attempt",
    "attempt_dir",
    "state",
    "started_utc",
    "finished_utc",
    "host",
    "pid",
    "pane",
    "worker_process",
    "child_process",
    "cpu_affinity",
    "interpreter_probe",
    "argv",
    "environment",
    "harness_environment",
    "exit_code",
    "error",
    "signal_number",
    "stdout_log",
    "stderr_log",
    "payload",
    "validation",
}
_MEGA_ROW_KEYS = {
    "name",
    "key",
    "measured",
    "input",
    "input_bytes",
    "input_sha256",
    "feature_lines",
    "gffbase",
    "legacy",
    "ingest_speedup",
    "spatial",
    "batched",
    "db_paths",
    "params",
}
_MEGA_PARAM_KEYS = {
    "legacy_cap_seconds",
    "gffbase_cap_seconds",
    "n_spatial",
    "n_batched",
    "repeats",
    "region_seed",
    "threads",
    "gtf_arm",
    "infer_gtf_parents",
    "validation_sample",
    "benchmark_env",
}
_BRIDGE_KEYS = {
    "schema_version",
    "label",
    "engine",
    "package_version",
    "input",
    "database",
    "measurement",
    "params",
    "environment",
}
_BRIDGE_ENVIRONMENT_KEYS = {
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
_SHA256_RE = re.compile(r"[0-9a-f]{64}\Z", re.ASCII)
_HOST_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,254}\Z", re.ASCII)
_UTC_RE = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}Z\Z")
_UTC_OFFSET_RE = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}(?:Z|\+00:00)\Z")
_MAX_FAILURES = 128
_MAX_TEXT_BYTES = 2000

_RUN_LOCAL_RESULT_KEYS = {
    "schema_version",
    "artifact_kind",
    "run_id",
    "campaign_sha256",
    "merged_utc",
    "campaign",
    "campaign_path",
    "candidate",
    "platform",
    "inputs",
    "worker_results",
    "results",
    "evidence",
    "comparisons",
    "gates",
    "results_sha256",
}
_PORTABLE_RESULT_KEYS = {
    "schema_version",
    "artifact_kind",
    "run_id",
    "campaign_sha256",
    "merged_utc",
    "platform_key",
    "candidate",
    "platform",
    "inputs",
    "results",
    "evidence",
    "comparisons",
    "gates",
    "source_results_sha256",
    "projection_sha256",
}
_PORTABLE_MEGA_ROW_KEYS = _MEGA_ROW_KEYS - {"input", "db_paths"}
_PORTABLE_BRIDGE_KEYS = _BRIDGE_KEYS
_PORTABLE_EVIDENCE_KEYS = {
    "worker_result_sha256",
    "job_sha256",
    "attempt",
    "kind",
    "worker_id",
    "interpreter_role",
    "corpus_key",
    "result_key",
    "threads",
    "cpus",
    "primary_eligible",
    "started_utc",
    "finished_utc",
    "host",
    "cpu_affinity",
    "interpreter_probe_sha256",
    "benchmark_environment",
}
_PORTABLE_PLATFORM_KEYS = {
    "platform",
    "machine",
    "allowed_cpus",
    "online_cpus",
    "physical_cpu_ids",
    "numa_nodes",
    "free_bytes",
    "available_ram_bytes",
    "mount_fstype",
    "executable_versions",
}
_INDEX_KEYS = {"schema_version", "platforms"}
_INDEX_PLATFORM_KEYS = {"kind", "canonical_run_id", "runs"}
_CAMPAIGN_INDEX_RUN_KEYS = {
    "kind",
    "artifact",
    "sha256",
    "schema_version",
    "projection_sha256",
    "source_results_sha256",
    "campaign_sha256",
    "candidate",
    "published_utc",
}
_LEGACY_INDEX_RUN_KEYS = {
    "kind",
    "artifact",
    "sha256",
    "schema_version",
    "description",
}


def _positive_number(value: object) -> bool:
    return (
        isinstance(value, (int, float))
        and not isinstance(value, bool)
        and math.isfinite(value)
        and value > 0
    )


def _numbers_equal(left: object, right: object) -> bool:
    return (
        isinstance(left, (int, float))
        and not isinstance(left, bool)
        and isinstance(right, (int, float))
        and not isinstance(right, bool)
        and math.isfinite(left)
        and math.isfinite(right)
        and math.isclose(float(left), float(right), rel_tol=1e-12, abs_tol=1e-12)
    )


def _require_timestamp(value: object, label: str, *, allow_utc_offset: bool = False) -> datetime:
    pattern = _UTC_OFFSET_RE if allow_utc_offset else _UTC_RE
    if type(value) is not str or pattern.fullmatch(value) is None:
        raise model.CampaignError(f"{label} must be a canonical UTC timestamp")
    normalized = value.removesuffix("+00:00") + "Z" if value.endswith("+00:00") else value
    try:
        parsed = datetime.strptime(normalized, "%Y-%m-%dT%H:%M:%SZ")
    except ValueError as exc:
        raise model.CampaignError(f"{label} must be a valid UTC timestamp") from exc
    if parsed.strftime("%Y-%m-%dT%H:%M:%SZ") != normalized:
        raise model.CampaignError(f"{label} must use canonical UTC spelling")
    return parsed


def _require_text(value: object, label: str, *, maximum: int = _MAX_TEXT_BYTES) -> str:
    if (
        type(value) is not str
        or not value
        or "\x00" in value
        or len(value.encode("utf-8")) > maximum
    ):
        raise model.CampaignError(f"{label} must be non-empty bounded text")
    return value


def _portable_relative_path(value: object, label: str) -> str:
    text = _require_text(value, label)
    path = Path(text)
    if (
        path.is_absolute()
        or "\\" in text
        or "://" in text
        or any(part in {"", ".", ".."} for part in text.split("/"))
    ):
        raise model.CampaignError(f"{label} must be a portable relative path")
    return text


def _runtime_job(
    campaign: Mapping[str, object], job: model.JobSpec | Mapping[str, object]
) -> model.JobSpec:
    candidate = job if isinstance(job, model.JobSpec) else model.JobSpec.from_dict(job)
    matches = [item for item in model.campaign_jobs(campaign) if item.job_id == candidate.job_id]
    if len(matches) != 1 or matches[0].to_dict() != candidate.to_dict():
        raise model.CampaignError(f"job is not bound to campaign: {candidate.job_id}")
    return matches[0]


def _campaign_spec(campaign: Mapping[str, object]) -> dict[str, Any]:
    document = model.validate_campaign_document(
        {key: value for key, value in campaign.items() if key != "_path"}
    )
    return cast(dict[str, Any], document["spec"])


def _attempt_directory(campaign: Mapping[str, object], job: model.JobSpec, attempt: int) -> Path:
    if "_path" not in campaign or type(campaign["_path"]) is not str:
        raise model.CampaignError("worker-result validation requires the runtime campaign path")
    campaign_path = safe_io.lexical_absolute(campaign["_path"], "campaign path")
    return safe_io.attempt_directory(campaign_path.parent, job.job_id, attempt)


def _validate_harness_environment(
    campaign: Mapping[str, object], job: model.JobSpec, value: object
) -> dict[str, Any]:
    if not benchmark_environment_evidence_is_valid(value, threads=job.threads):
        raise model.CampaignError(f"{job.job_id}: primary harness environment is invalid")
    environment = cast(dict[str, Any], value)
    spec = _campaign_spec(campaign)
    repo = cast(Mapping[str, object], spec["repo"])
    candidate = cast(Mapping[str, object], spec["candidate"])
    wheel = cast(Mapping[str, object], candidate["wheel"])
    artifact = cast(Mapping[str, object], environment["artifact"])
    python = cast(Mapping[str, object], environment["python"])
    primary_probe = cast(Mapping[str, object], spec["interpreters"]["primary"])
    resources = cast(Mapping[str, object], spec["resources"])
    if environment["git_commit"] != repo.get("commit") or environment["git_dirty"] is not False:
        raise model.CampaignError(f"{job.job_id}: primary harness git identity differs")
    if (
        artifact.get("wheel") != wheel.get("name")
        or artifact.get("wheel_sha256") != wheel.get("sha256")
        or cast(Mapping[str, object], artifact.get("metadata", {})).get("version")
        != candidate.get("public_version")
    ):
        raise model.CampaignError(f"{job.job_id}: primary harness artifact differs")
    if environment["cpu_affinity"] != list(model.parse_cpu_list(job.cpus)):
        raise model.CampaignError(f"{job.job_id}: primary harness affinity differs")
    if environment["env"] != benchmark_env(job.threads):
        raise model.CampaignError(f"{job.job_id}: primary harness controls differ")
    if (
        python.get("version") != primary_probe.get("python_version")
        or python.get("implementation") != primary_probe.get("implementation")
        or python.get("executable") != Path(str(primary_probe.get("resolved_executable", ""))).name
    ):
        raise model.CampaignError(f"{job.job_id}: harness interpreter differs from its pin")
    resource_platform = str(resources.get("platform", "")).casefold()
    if not str(environment["platform"]).casefold().startswith(resource_platform):
        raise model.CampaignError(f"{job.job_id}: harness platform differs from preflight")
    if str(environment["machine"]).casefold() != str(resources.get("machine", "")).casefold():
        raise model.CampaignError(f"{job.job_id}: harness machine differs from preflight")
    return environment


def _expected_gtf_controls(job: model.JobSpec) -> tuple[str | None, bool | None]:
    if job.corpus_key != "gencode-gtf":
        return None, None
    arm = job.gtf_arm or "no-infer"
    return arm, arm != "no-infer"


def _validate_mega_payload(
    campaign: Mapping[str, object],
    job: model.JobSpec,
    payload: object,
    harness_environment: object,
) -> dict[str, Any]:
    row = model.require_exact_keys(payload, _MEGA_ROW_KEYS, f"{job.job_id} benchmark row")
    corpus = BY_KEY[job.corpus_key]
    if row["name"] != corpus["name"] or row["key"] != job.result_key:
        raise model.CampaignError(f"{job.job_id}: benchmark corpus identity differs")
    measured = model.require_exact_keys(
        row["measured"],
        {"timestamp_utc", "git_commit", "git_dirty"},
        f"{job.job_id} measured identity",
    )
    measured_utc = _require_timestamp(
        measured["timestamp_utc"],
        f"{job.job_id} measured time",
        allow_utc_offset=True,
    )
    spec = _campaign_spec(campaign)
    repo = cast(Mapping[str, object], spec["repo"])
    if measured["git_commit"] != repo.get("commit") or measured["git_dirty"] is not False:
        raise model.CampaignError(f"{job.job_id}: row git identity differs")
    input_path = _portable_relative_path(row["input"], f"{job.job_id} row input")
    expected_filename = Path(str(job.input["path"])).name
    if Path(input_path).name != expected_filename:
        raise model.CampaignError(f"{job.job_id}: row input filename differs")
    if row["input_bytes"] != job.input["bytes"] or row["input_sha256"] != job.input["sha256"]:
        raise model.CampaignError(f"{job.job_id}: row input identity differs")
    if type(row["feature_lines"]) is not int or row["feature_lines"] < 1:
        raise model.CampaignError(f"{job.job_id}: row feature count is invalid")
    if job.kind == "control" and job.gtf_arm == "parent-stripped":
        counts = cast(Mapping[str, object], job.input["counts"])
        if row["feature_lines"] != counts["output_feature_lines"]:
            raise model.CampaignError(f"{job.job_id}: transformed feature count differs")

    params = model.require_exact_keys(row["params"], _MEGA_PARAM_KEYS, f"{job.job_id} params")
    expected_arm, expected_inference = _expected_gtf_controls(job)
    expected_params = {
        "legacy_cap_seconds": job.parameters["legacy_timeout"],
        "gffbase_cap_seconds": job.parameters["gffbase_timeout"],
        "n_spatial": job.parameters["n_spatial"],
        "n_batched": job.parameters["n_batched"],
        "repeats": job.parameters["repeats"],
        "region_seed": job.parameters["region_seed"],
        "threads": job.threads,
        "gtf_arm": expected_arm,
        "infer_gtf_parents": expected_inference,
        "validation_sample": job.parameters["validation_sample"],
        "benchmark_env": benchmark_env(job.threads),
    }
    if params != expected_params:
        raise model.CampaignError(f"{job.job_id}: benchmark parameters differ")

    environment = _validate_harness_environment(campaign, job, harness_environment)
    environment_utc = _require_timestamp(environment["timestamp_utc"], f"{job.job_id} harness time")
    if measured_utc > environment_utc:
        raise model.CampaignError(f"{job.job_id}: row was measured after its environment")

    candidate = row["gffbase"]
    if not isinstance(candidate, dict) or not benchmark_candidate_evidence_is_valid(
        candidate,
        require_exhaustive=job.kind in {"primary", "control"},
        require_rtree=True,
    ):
        raise model.CampaignError(f"{job.job_id}: candidate evidence is invalid")
    if (
        candidate["benchmark_env"] != expected_params["benchmark_env"]
        or candidate["cap_seconds"] != job.parameters["gffbase_timeout"]
        or candidate["fmt"] != job.input["format"]
        or candidate["label"] != f"gffbase ingest({expected_filename})"
        or candidate["validation"]["requested_sample"] != job.parameters["validation_sample"]
    ):
        raise model.CampaignError(f"{job.job_id}: candidate controls differ")
    # Keep the signature validator an explicit campaign boundary, even though
    # the Task 2 candidate validator also checks it internally.
    if not validate_database_signature(candidate.get("correctness_signature")):
        raise model.CampaignError(f"{job.job_id}: candidate signature is invalid")

    for section in ("spatial", "batched"):
        error = benchmark_query_evidence_error(section, row[section], candidate, params)
        if error:
            raise model.CampaignError(f"{job.job_id}: {error}")
    if job.kind == "scaling":
        if row["spatial"] != {
            "state": "skipped",
            "reason": "candidate completion/validation/signature/R-tree failed",
        } or row["batched"] != {
            "state": "skipped",
            "reason": "candidate completion/validation/signature failed",
        }:
            raise model.CampaignError(f"{job.job_id}: scaling query skips differ")
    elif row["spatial"].get("state") != "completed" or row["batched"].get("state") != "completed":
        raise model.CampaignError(f"{job.job_id}: canonical query evidence is incomplete")

    legacy = row["legacy"]
    if job.kind == "scaling":
        expected_legacy = {
            "state": "skipped",
            "reason": "requested by --skip-legacy",
            "exit_code": None,
            "wall_seconds": None,
            "cap_seconds": job.parameters["legacy_timeout"],
            "n_features": None,
        }
        if legacy != expected_legacy or row["ingest_speedup"] is not None:
            raise model.CampaignError(f"{job.job_id}: scaling comparator evidence differs")
    else:
        if not isinstance(legacy, dict) or not benchmark_comparator_evidence_is_valid(legacy):
            raise model.CampaignError(f"{job.job_id}: comparator evidence is invalid")
        if (
            legacy["benchmark_env"] != expected_params["benchmark_env"]
            or legacy["cap_seconds"] != job.parameters["legacy_timeout"]
            or legacy["label"] != f"legacy gffutils ingest({expected_filename})"
        ):
            raise model.CampaignError(f"{job.job_id}: comparator controls differ")
        if legacy["state"] == "timed_out":
            if row["ingest_speedup"] is not None:
                raise model.CampaignError(f"{job.job_id}: timed-out comparator carries a ratio")
        else:
            matched = signatures_match(
                candidate.get("correctness_signature"), legacy.get("correctness_signature")
            )
            expected_ratio = (
                float(legacy["wall_seconds"]) / float(candidate["wall_seconds"])
                if matched is True
                else None
            )
            if (expected_ratio is None and row["ingest_speedup"] is not None) or (
                expected_ratio is not None
                and not _numbers_equal(row["ingest_speedup"], expected_ratio)
            ):
                raise model.CampaignError(f"{job.job_id}: serialized ratio differs from evidence")

    db_paths = model.require_exact_keys(
        row["db_paths"], {"gffbase", "legacy"}, f"{job.job_id} database paths"
    )
    for engine, suffix in (("gffbase", ".duckdb"), ("legacy", "_legacy.sqlite")):
        relative = _portable_relative_path(db_paths[engine], f"{job.job_id} {engine} database")
        if Path(relative).name != f"{job.result_key}{suffix}":
            raise model.CampaignError(f"{job.job_id}: {engine} database name differs")
    return row


def _validate_bridge_environment(
    campaign: Mapping[str, object], job: model.JobSpec, value: object
) -> dict[str, Any]:
    environment = model.require_exact_keys(
        value, _BRIDGE_ENVIRONMENT_KEYS, f"{job.job_id} bridge environment"
    )
    model.require_schema(environment, BRIDGE_ENVIRONMENT_SCHEMA, f"{job.job_id} bridge environment")
    _require_timestamp(environment["timestamp_utc"], f"{job.job_id} bridge environment time")
    _require_text(environment["hostname"], f"{job.job_id} bridge hostname", maximum=255)
    _require_text(environment["platform"], f"{job.job_id} bridge platform", maximum=512)
    _require_text(environment["machine"], f"{job.job_id} bridge machine", maximum=80)
    python = model.require_exact_keys(
        environment["python"],
        {"version", "implementation", "executable"},
        f"{job.job_id} bridge Python",
    )
    package = model.require_exact_keys(
        environment["package"], {"name", "version"}, f"{job.job_id} bridge package"
    )
    affinity = environment["cpu_affinity"]
    expected_affinity = list(model.parse_cpu_list(job.cpus))
    if affinity != expected_affinity:
        raise model.CampaignError(f"{job.job_id}: bridge affinity differs")
    if not benchmark_bounded_environment_is_valid(
        environment["benchmark_env"], threads=job.threads
    ):
        raise model.CampaignError(f"{job.job_id}: bridge controls differ")
    spec = _campaign_spec(campaign)
    probe = cast(Mapping[str, object], spec["interpreters"][job.interpreter_role])
    expected_package = job.parameters["bridge_engine"]
    expected_version = "0.1.0" if job.interpreter_role == "gffbase-0.1.0" else "0.13"
    versions = probe.get("packages")
    probed_version = versions.get(expected_package) if isinstance(versions, Mapping) else None
    if (
        python["version"] != probe.get("python_version")
        or python["implementation"] != probe.get("implementation")
        or python["executable"] != Path(str(probe.get("resolved_executable", ""))).name
        or package != {"name": expected_package, "version": expected_version}
        or probed_version != expected_version
    ):
        raise model.CampaignError(f"{job.job_id}: bridge package/interpreter pin differs")
    resources = cast(Mapping[str, object], spec["resources"])
    if (
        not str(environment["platform"])
        .casefold()
        .startswith(str(resources.get("platform", "")).casefold())
        or str(environment["machine"]).casefold() != str(resources.get("machine", "")).casefold()
    ):
        raise model.CampaignError(f"{job.job_id}: bridge platform differs from preflight")
    return environment


def _validate_bridge_payload(
    campaign: Mapping[str, object], job: model.JobSpec, payload: object, attempt_dir: Path
) -> dict[str, Any]:
    bridge = model.require_exact_keys(payload, _BRIDGE_KEYS, f"{job.job_id} bridge payload")
    model.require_schema(bridge, BRIDGE_SCHEMA, f"{job.job_id} bridge payload")
    expected_version = "0.1.0" if job.interpreter_role == "gffbase-0.1.0" else "0.13"
    if (
        bridge["label"] != job.job_id.removeprefix("bridge-")
        or bridge["engine"] != job.parameters["bridge_engine"]
        or bridge["package_version"] != expected_version
    ):
        raise model.CampaignError(f"{job.job_id}: bridge identity differs")
    input_value = model.require_exact_keys(
        bridge["input"], {"path", "bytes", "sha256"}, f"{job.job_id} bridge input"
    )
    if (
        input_value["path"] != str(Path(str(job.input["path"])).resolve())
        or input_value["bytes"] != job.input["bytes"]
        or input_value["sha256"] != job.input["sha256"]
    ):
        raise model.CampaignError(f"{job.job_id}: bridge input differs")
    database = model.require_exact_keys(
        bridge["database"], {"path", "bytes"}, f"{job.job_id} bridge database"
    )
    if database["path"] != str(attempt_dir / "scratch" / "bridge.duckdb") or (
        type(database["bytes"]) is not int or database["bytes"] < 1
    ):
        raise model.CampaignError(f"{job.job_id}: bridge database evidence differs")
    params = model.require_exact_keys(
        bridge["params"], {"fmt", "threads"}, f"{job.job_id} bridge params"
    )
    if params != {"fmt": job.input["format"], "threads": job.threads}:
        raise model.CampaignError(f"{job.job_id}: bridge parameters differ")
    measurement = model.require_exact_keys(
        bridge["measurement"],
        {"wall_seconds", "n_features", "correctness_signature", "validation"},
        f"{job.job_id} bridge measurement",
    )
    if not _positive_number(measurement["wall_seconds"]) or (
        type(measurement["n_features"]) is not int or measurement["n_features"] < 1
    ):
        raise model.CampaignError(f"{job.job_id}: bridge measurement is invalid")
    signature = measurement["correctness_signature"]
    if not isinstance(signature, dict) or not validate_database_signature(signature):
        raise model.CampaignError(f"{job.job_id}: bridge signature is invalid")
    if signature["feature_count"] != measurement["n_features"]:
        raise model.CampaignError(f"{job.job_id}: bridge feature count differs from signature")
    validation = measurement["validation"]
    if job.parameters["bridge_engine"] == "gffbase":
        report = model.require_exact_keys(
            validation, {"ok", "checked", "errors"}, f"{job.job_id} bridge validation"
        )
        if (
            report["ok"] is not True
            or type(report["checked"]) is not list
            or not report["checked"]
            or any(type(item) is not str or not item for item in report["checked"])
            or report["errors"] != []
        ):
            raise model.CampaignError(f"{job.job_id}: bridge validation failed")
    elif validation is not None:
        raise model.CampaignError(f"{job.job_id}: gffutils bridge validation must be null")
    _validate_bridge_environment(campaign, job, bridge["environment"])
    return bridge


def validate_terminal_worker_result(
    campaign: Mapping[str, object],
    job: model.JobSpec | Mapping[str, object],
    result: object,
    *,
    require_accepted: bool = False,
) -> dict[str, Any]:
    """Validate one immutable terminal ``benchmark-worker-v2`` result."""

    spec = _runtime_job(campaign, job)
    record = model.require_exact_keys(result, _WORKER_RESULT_KEYS, f"{spec.job_id} worker result")
    model.require_schema(record, model.WORKER_SCHEMA, f"{spec.job_id} worker result")
    expected_identity = {
        "run_id": campaign.get("run_id"),
        "campaign_sha256": model.campaign_digest(campaign),
        "job_id": spec.job_id,
        "job_sha256": spec.job_sha256,
    }
    for key, expected in expected_identity.items():
        if type(record[key]) is not type(expected) or record[key] != expected:
            raise model.CampaignError(f"{spec.job_id}: worker result {key} mismatch")
    attempt = record["attempt"]
    if type(attempt) is not int or not 1 <= attempt <= 9999:
        raise model.CampaignError(f"{spec.job_id}: worker attempt is invalid")
    attempt_dir = _attempt_directory(campaign, spec, attempt)
    if record["attempt_dir"] != str(attempt_dir):
        raise model.CampaignError(f"{spec.job_id}: worker attempt path differs")
    relative_attempt = f"jobs/{spec.job_id}/attempts/{attempt:04d}"
    if (
        record["stdout_log"] != f"{relative_attempt}/stdout.log"
        or record["stderr_log"] != f"{relative_attempt}/stderr.log"
    ):
        raise model.CampaignError(f"{spec.job_id}: worker log paths differ")
    started = _require_timestamp(record["started_utc"], f"{spec.job_id} started_utc")
    finished = _require_timestamp(record["finished_utc"], f"{spec.job_id} finished_utc")
    if finished < started:
        raise model.CampaignError(f"{spec.job_id}: worker result finishes before it starts")
    if type(record["host"]) is not str or _HOST_RE.fullmatch(record["host"]) is None:
        raise model.CampaignError(f"{spec.job_id}: worker host is invalid")

    pane = worker.PaneIdentity.from_dict(record["pane"])
    process = worker.ProcessIdentity.from_dict(record["worker_process"])
    expected_session = f"gffbase-{model.campaign_digest(campaign)[:12]}-cluster"
    if spec.worker_id == "canonical":
        expected_session += "-canonical"
    if (
        pane.session != expected_session
        or pane.window != spec.worker_id
        or pane.pane_dead
        or process.pid != pane.pane_pid
    ):
        raise model.CampaignError(f"{spec.job_id}: worker pane/process linkage differs")
    child_value = record["child_process"]
    child = None if child_value is None else worker.ProcessIdentity.from_dict(child_value)
    if record["pid"] != (None if child is None else child.pid):
        raise model.CampaignError(f"{spec.job_id}: worker child PID differs")
    if child is not None and (child.boot_id != process.boot_id or child.pgid != child.pid):
        raise model.CampaignError(f"{spec.job_id}: worker child process linkage differs")

    state = record["state"]
    if state not in {"succeeded", "failed", "timed_out", "interrupted"}:
        raise model.CampaignError(f"{spec.job_id}: worker state is invalid")
    validation = model.require_exact_keys(
        record["validation"], {"accepted", "failures"}, f"{spec.job_id} result validation"
    )
    failures = validation["failures"]
    if type(validation["accepted"]) is not bool or type(failures) is not list:
        raise model.CampaignError(f"{spec.job_id}: result validation fields are invalid")
    if len(failures) > _MAX_FAILURES or any(
        type(item) is not str or not item or len(item.encode("utf-8")) > _MAX_TEXT_BYTES
        for item in failures
    ):
        raise model.CampaignError(f"{spec.job_id}: result failures are invalid or unbounded")
    successful = state == "succeeded"
    if (
        validation["accepted"] is not successful
        or (successful and failures)
        or (not successful and not failures)
    ):
        raise model.CampaignError(f"{spec.job_id}: result state and acceptance differ")
    if require_accepted and not successful:
        raise model.CampaignError(f"{spec.job_id}: worker result was not accepted")

    error = record["error"]
    if error is not None and (
        type(error) is not str or len(error.encode("utf-8")) > _MAX_TEXT_BYTES
    ):
        raise model.CampaignError(f"{spec.job_id}: worker error is invalid or unbounded")
    if record["exit_code"] is not None and type(record["exit_code"]) is not int:
        raise model.CampaignError(f"{spec.job_id}: worker exit code is invalid")
    if record["signal_number"] is not None and (
        type(record["signal_number"]) is not int or not 1 <= record["signal_number"] <= 64
    ):
        raise model.CampaignError(f"{spec.job_id}: worker signal is invalid")

    expected_affinity = list(model.parse_cpu_list(spec.cpus))
    if record["cpu_affinity"] is not None and record["cpu_affinity"] != expected_affinity:
        raise model.CampaignError(f"{spec.job_id}: worker affinity differs")
    expected_probe = cast(Mapping[str, object], _campaign_spec(campaign)["interpreters"])[
        spec.interpreter_role
    ]
    if record["interpreter_probe"] is not None and model.canonical_json_bytes(
        record["interpreter_probe"]
    ) != model.canonical_json_bytes(expected_probe):
        raise model.CampaignError(f"{spec.job_id}: worker interpreter pin differs")

    expected_argv = (
        model.build_bridge_argv(spec, attempt_dir, campaign)
        if spec.kind == "bridge"
        else model.build_mega_argv(spec, attempt_dir, campaign)
    )
    if record["argv"] not in ([], expected_argv):
        raise model.CampaignError(f"{spec.job_id}: worker argv differs")
    expected_environment = benchmark_env(spec.threads)
    if record["environment"] not in ({}, expected_environment):
        raise model.CampaignError(f"{spec.job_id}: worker environment differs")

    if successful:
        if (
            child is None
            or record["exit_code"] != 0
            or record["error"] is not None
            or record["signal_number"] is not None
            or record["cpu_affinity"] != expected_affinity
            or record["interpreter_probe"] is None
            or record["argv"] != expected_argv
            or record["environment"] != expected_environment
        ):
            raise model.CampaignError(f"{spec.job_id}: successful worker lifecycle is incomplete")
        if spec.kind == "bridge":
            if record["harness_environment"] != {}:
                raise model.CampaignError(f"{spec.job_id}: bridge carries primary harness evidence")
            record["payload"] = _validate_bridge_payload(
                campaign, spec, record["payload"], attempt_dir
            )
        else:
            record["payload"] = _validate_mega_payload(
                campaign,
                spec,
                record["payload"],
                record["harness_environment"],
            )
    elif record["payload"] != {} or record["harness_environment"] != {}:
        raise model.CampaignError(f"{spec.job_id}: failed worker carries accepted payload evidence")
    return record


def validate_worker_result(
    campaign: Mapping[str, object],
    job: model.JobSpec | Mapping[str, object],
    result: object,
) -> dict[str, Any]:
    """Validate one accepted immutable worker result and return a detached copy."""

    return validate_terminal_worker_result(campaign, job, result, require_accepted=True)


def strict_ratio(left: Mapping[str, object], right: Mapping[str, object]) -> dict[str, object]:
    """Derive a measured ratio only from completed, signature-equal evidence."""

    if right.get("state") == "timed_out":
        cap = right.get("cap_seconds")
        if type(cap) is not int or cap < 1:
            raise model.CampaignError("timed-out comparator lacks a positive integer cap")
        return {
            "state": "censored",
            "reason": "comparator-timed-out",
            "cap_seconds": cap,
            "signature_match": None,
            "feature_counts_match": None,
            "ratio": None,
        }
    left_signature = left.get("correctness_signature")
    right_signature = right.get("correctness_signature")
    matched = signatures_match(
        cast(dict[str, object] | None, left_signature),
        cast(dict[str, object] | None, right_signature),
    )
    diagnostic: dict[str, object] = {
        "state": "unavailable",
        "signature_match": matched is True,
        "feature_counts_match": (
            left.get("n_features") is not None
            and right.get("n_features") is not None
            and left.get("n_features") == right.get("n_features")
        ),
        "ratio": None,
    }
    if (
        matched is True
        and left.get("state") == "completed"
        and right.get("state") == "completed"
        and left.get("exit_code") == 0
        and right.get("exit_code") == 0
        and _positive_number(left.get("wall_seconds"))
        and _positive_number(right.get("wall_seconds"))
    ):
        diagnostic["state"] = "completed"
        diagnostic["ratio"] = float(cast(float, right["wall_seconds"])) / float(
            cast(float, left["wall_seconds"])
        )
    elif matched is False:
        diagnostic["state"] = "signature-mismatch"
    return diagnostic


def _detached(value: object) -> Any:
    return json.loads(model.canonical_json_bytes(value))


def _worker_storage_digest(value: Mapping[str, object]) -> str:
    return hashlib.sha256(safe_io.canonical_storage_bytes(value)).hexdigest()


def _comparison_with_engines(
    left: Mapping[str, object],
    right: Mapping[str, object],
    *,
    candidate_engine: str,
    comparator_engine: str,
    primary_eligible: bool,
) -> dict[str, object]:
    return {
        **strict_ratio(left, right),
        "candidate_engine": candidate_engine,
        "comparator_engine": comparator_engine,
        "primary_eligible": primary_eligible,
    }


def _derive_run_local_result(
    campaign: Mapping[str, object],
    by_job: Mapping[str, Mapping[str, object]],
    *,
    merged_utc: str,
) -> dict[str, object]:
    """Derive every run-local field from validated campaign/worker evidence."""

    _require_timestamp(merged_utc, "campaign merged_utc")
    jobs = model.campaign_jobs(campaign)
    jobs_by_id = {job.job_id: job for job in jobs}
    if set(by_job) != set(jobs_by_id):
        missing = sorted(set(jobs_by_id) - set(by_job))
        extra = sorted(set(by_job) - set(jobs_by_id))
        raise model.CampaignError(
            f"merged worker-result namespace differs; missing={missing!r}, extra={extra!r}"
        )

    validated: dict[str, dict[str, Any]] = {}
    digests: set[str] = set()
    for job in jobs:
        result = validate_worker_result(campaign, job, by_job[job.job_id])
        digest = _worker_storage_digest(result)
        if digest in digests:
            raise model.CampaignError("duplicate worker result digest")
        digests.add(digest)
        validated[job.job_id] = result

    sections: dict[str, dict[str, object]] = {
        "primary": {},
        "controls": {},
        "scaling": {},
        "bridges": {},
    }
    comparisons: dict[str, dict[str, object]] = {
        "primary": {},
        "controls": {},
        "bridges": {},
    }
    evidence: dict[str, dict[str, object]] = {}
    signature_gates: dict[str, bool | None] = {}

    for job in jobs:
        result = validated[job.job_id]
        payload = _detached(result["payload"])
        if job.kind == "primary":
            sections["primary"][job.result_key] = payload
            comparison = _comparison_with_engines(
                payload["gffbase"],
                payload["legacy"],
                candidate_engine=model.PUBLIC_VERSION,
                comparator_engine="gffutils-0.14",
                primary_eligible=True,
            )
            comparisons["primary"][job.result_key] = comparison
            signature_gates[job.result_key] = cast(bool | None, comparison["signature_match"])
        elif job.kind == "control":
            sections["controls"][job.result_key] = payload
            comparisons["controls"][job.result_key] = _comparison_with_engines(
                payload["gffbase"],
                payload["legacy"],
                candidate_engine=model.PUBLIC_VERSION,
                comparator_engine="gffutils-0.14",
                primary_eligible=False,
            )
        elif job.kind == "scaling":
            sections["scaling"][job.job_id] = payload
        else:
            sections["bridges"][job.job_id] = payload

        probe = cast(Mapping[str, object], result["interpreter_probe"])
        probe_digest = probe.get("probe_sha256")
        if type(probe_digest) is not str or _SHA256_RE.fullmatch(probe_digest) is None:
            raise model.CampaignError(f"{job.job_id}: interpreter probe digest is invalid")
        evidence[job.job_id] = {
            "worker_result_sha256": _worker_storage_digest(result),
            "attempt": result["attempt"],
            "kind": job.kind,
            "worker_id": job.worker_id,
            "interpreter_role": job.interpreter_role,
            "primary_eligible": job.primary_eligible,
            "started_utc": result["started_utc"],
            "finished_utc": result["finished_utc"],
            "host": result["host"],
            "cpu_affinity": result["cpu_affinity"],
            "interpreter_probe_sha256": probe_digest,
            "pane": result["pane"],
            "worker_process": result["worker_process"],
            "child_process": result["child_process"],
            "stdout_log": result["stdout_log"],
            "stderr_log": result["stderr_log"],
        }

    for corpus_key in ("mane", "chess"):
        primary = cast(Mapping[str, object], sections["primary"][corpus_key])
        for version, reference_key in (
            ("gffbase-0.1.0", "gffbase"),
            ("gffutils-0.13", "legacy"),
        ):
            bridge_id = f"bridge-{version}-{corpus_key}"
            bridge = cast(Mapping[str, object], sections["bridges"][bridge_id])
            measurement = cast(Mapping[str, object], bridge["measurement"])
            completed_bridge = {**measurement, "state": "completed", "exit_code": 0}
            comparisons["bridges"][bridge_id] = _comparison_with_engines(
                cast(Mapping[str, object], primary[reference_key]),
                completed_bridge,
                candidate_engine=(
                    model.PUBLIC_VERSION if reference_key == "gffbase" else "gffutils-0.14"
                ),
                comparator_engine=version,
                primary_eligible=False,
            )

    expected_namespaces = {
        "primary": set(model.CORPUS_ORDER),
        "controls": {
            "gencode-gtf-default",
            "gencode-gtf-parent-stripped",
        },
        "scaling": {job.job_id for job in jobs if job.kind == "scaling"},
        "bridges": {job.job_id for job in jobs if job.kind == "bridge"},
    }
    for section, expected in expected_namespaces.items():
        if set(sections[section]) != expected:
            raise model.CampaignError(f"merged {section} namespace is incomplete")
    cardinalities = {section: len(values) for section, values in sections.items()}
    if cardinalities != {"primary": 5, "controls": 2, "scaling": 25, "bridges": 4}:
        raise model.CampaignError("merged namespace cardinalities differ from 5/2/25/4")

    failures = [
        f"primary signature mismatch: {key}"
        for key, matched in signature_gates.items()
        if matched is False
    ]
    campaign_document = model.validate_campaign_document(
        {key: value for key, value in campaign.items() if key != "_path"}
    )
    if "_path" not in campaign:
        raise model.CampaignError("run-local merge requires the runtime campaign path")
    spec = cast(Mapping[str, object], campaign_document["spec"])
    return {
        "schema_version": model.RESULTS_SCHEMA,
        "artifact_kind": "run-local",
        "run_id": campaign_document["run_id"],
        "campaign_sha256": campaign_document["spec_sha256"],
        "merged_utc": merged_utc,
        "campaign": campaign_document,
        "campaign_path": str(
            safe_io.lexical_absolute(cast(str, campaign["_path"]), "campaign path")
        ),
        "candidate": _detached(spec["candidate"]),
        "platform": _detached(spec["resources"]),
        "inputs": _detached(spec["inputs"]),
        "worker_results": {job.job_id: _detached(validated[job.job_id]) for job in jobs},
        "results": sections,
        "evidence": evidence,
        "comparisons": comparisons,
        "gates": {
            "all_36_jobs_present_once": True,
            "namespace_cardinalities": cardinalities,
            "identities_match": True,
            "full_validation": True,
            "canonical_rtree": True,
            "primary_signature_matches": signature_gates,
            "publishable": not failures,
            "failures": failures,
        },
    }


def merge_campaign(
    campaign: Mapping[str, object], results: Sequence[Mapping[str, object]]
) -> dict[str, object]:
    """Validate and merge exactly 36 unique accepted immutable results."""

    if isinstance(results, (str, bytes)) or not isinstance(results, Sequence):
        raise model.CampaignError("merge results must be a sequence")
    if len(results) != 36:
        raise model.CampaignError(
            f"merge requires exactly 36 accepted results, found {len(results)}"
        )
    jobs = {job.job_id: job for job in model.campaign_jobs(campaign)}
    by_job: dict[str, Mapping[str, object]] = {}
    for result in results:
        if not isinstance(result, Mapping):
            raise model.CampaignError("worker result must be an object")
        job_id = result.get("job_id")
        if type(job_id) is not str or job_id not in jobs:
            raise model.CampaignError(f"unexpected result job id: {job_id!r}")
        if job_id in by_job:
            raise model.CampaignError(f"duplicate accepted result for {job_id}")
        by_job[job_id] = result
    merged = _derive_run_local_result(
        campaign,
        by_job,
        merged_utc=datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
    )
    merged["results_sha256"] = model.sha256_json(merged)
    return validate_campaign_results(merged)


def validate_campaign_results(value: object) -> dict[str, object]:
    """Validate and independently rederive a closed run-local campaign result."""

    record = model.require_exact_keys(value, _RUN_LOCAL_RESULT_KEYS, "campaign results")
    model.require_schema(record, model.RESULTS_SCHEMA, "campaign results")
    if record["artifact_kind"] != "run-local":
        raise model.CampaignError("campaign results artifact_kind must be 'run-local'")
    model.validate_identifier(cast(str, record["run_id"]), "run")
    digest = record["campaign_sha256"]
    if type(digest) is not str or _SHA256_RE.fullmatch(digest) is None:
        raise model.CampaignError("campaign results campaign_sha256 is invalid")
    _require_timestamp(record["merged_utc"], "campaign results merged_utc")
    expected_digest = record["results_sha256"]
    if type(expected_digest) is not str or _SHA256_RE.fullmatch(expected_digest) is None:
        raise model.CampaignError("campaign results digest is invalid")
    unsigned = {key: item for key, item in record.items() if key != "results_sha256"}
    if model.sha256_json(unsigned) != expected_digest:
        raise model.CampaignError("campaign results digest mismatch")

    campaign_document = model.validate_campaign_document(record["campaign"])
    if (
        campaign_document["run_id"] != record["run_id"]
        or campaign_document["spec_sha256"] != digest
    ):
        raise model.CampaignError("campaign results identity differs from embedded campaign")
    campaign_path = safe_io.lexical_absolute(record["campaign_path"], "campaign results path")
    runtime_campaign = {**campaign_document, "_path": str(campaign_path)}
    jobs = model.campaign_jobs(runtime_campaign)
    worker_results = model.require_exact_keys(
        record["worker_results"], {job.job_id for job in jobs}, "campaign worker results"
    )
    expected = _derive_run_local_result(
        runtime_campaign,
        cast(Mapping[str, Mapping[str, object]], worker_results),
        merged_utc=cast(str, record["merged_utc"]),
    )
    if model.canonical_json_bytes(expected) != model.canonical_json_bytes(unsigned):
        raise model.CampaignError(
            "campaign results fields differ from independently derived evidence"
        )
    return _detached(record)


def derive_linux_platform_key(platform_value: object, machine_value: object) -> str:
    """Derive the only supported campaign publication key from recorded data."""

    if platform_value != "Linux" or type(machine_value) is not str:
        raise model.CampaignError("campaign publication requires recorded Linux platform data")
    architecture = {
        "x86_64": "x86_64",
        "amd64": "x86_64",
        "aarch64": "aarch64",
        "arm64": "aarch64",
    }.get(machine_value.casefold().replace("-", "_"))
    if architecture is None:
        raise model.CampaignError(f"unsupported Linux campaign architecture: {machine_value!r}")
    return f"linux-{architecture}"


def _project_candidate(value: object) -> dict[str, object]:
    candidate = model.require_exact_keys(
        value,
        {"public_version", "cargo_version", "git_commit", "wheel"},
        "campaign candidate",
    )
    wheel = model.require_exact_keys(
        candidate["wheel"],
        {"path", "name", "bytes", "sha256", "metadata_version"},
        "campaign candidate wheel",
    )
    return {
        "public_version": candidate["public_version"],
        "cargo_version": candidate["cargo_version"],
        "git_commit": candidate["git_commit"],
        "wheel": {
            "name": wheel["name"],
            "bytes": wheel["bytes"],
            "sha256": wheel["sha256"],
            "metadata_version": wheel["metadata_version"],
        },
    }


def _project_platform(value: object) -> dict[str, object]:
    platform_value = cast(Mapping[str, object], value)
    mount = cast(Mapping[str, object], platform_value["mount"])
    return {
        "platform": platform_value["platform"],
        "machine": platform_value["machine"],
        "allowed_cpus": _detached(platform_value["allowed_cpus"]),
        "online_cpus": _detached(platform_value["online_cpus"]),
        "physical_cpu_ids": _detached(platform_value["physical_cpu_ids"]),
        "numa_nodes": _detached(platform_value["numa_nodes"]),
        "free_bytes": platform_value["free_bytes"],
        "available_ram_bytes": platform_value["available_ram_bytes"],
        "mount_fstype": mount["fstype"],
        "executable_versions": _detached(platform_value["executable_versions"]),
    }


def _project_inputs(value: object) -> dict[str, object]:
    inputs = cast(Mapping[str, Mapping[str, object]], value)
    projected: dict[str, object] = {}
    for key in (*model.CORPUS_ORDER, "gencode-gtf-parent-stripped"):
        item = inputs[key]
        base: dict[str, object] = {
            "filename": Path(str(item["path"])).name,
            "bytes": item["bytes"],
            "sha256": item["sha256"],
            "gzip_crc_ok": item["gzip_crc_ok"],
            "format": item["format"],
        }
        if key == "gencode-gtf-parent-stripped":
            base.update(
                {
                    "manifest_sha256": item["manifest_sha256"],
                    "transform_version": item["transform_version"],
                    "counts": _detached(item["counts"]),
                }
            )
        projected[key] = base
    return projected


def _project_mega_row(value: object) -> dict[str, object]:
    row = cast(Mapping[str, object], value)
    return {
        "name": row["name"],
        "key": row["key"],
        "measured": _detached(row["measured"]),
        "input_bytes": row["input_bytes"],
        "input_sha256": row["input_sha256"],
        "feature_lines": row["feature_lines"],
        "gffbase": _detached(row["gffbase"]),
        "legacy": _detached(row["legacy"]),
        "ingest_speedup": row["ingest_speedup"],
        "spatial": _detached(row["spatial"]),
        "batched": _detached(row["batched"]),
        "params": _detached(row["params"]),
    }


def _project_bridge(value: object) -> dict[str, object]:
    bridge = cast(Mapping[str, object], value)
    input_value = cast(Mapping[str, object], bridge["input"])
    database = cast(Mapping[str, object], bridge["database"])
    return {
        "schema_version": bridge["schema_version"],
        "label": bridge["label"],
        "engine": bridge["engine"],
        "package_version": bridge["package_version"],
        "input": {"bytes": input_value["bytes"], "sha256": input_value["sha256"]},
        "database": {"bytes": database["bytes"]},
        "measurement": _detached(bridge["measurement"]),
        "params": _detached(bridge["params"]),
        "environment": _detached(bridge["environment"]),
    }


def project_campaign_results(value: object) -> dict[str, object]:
    """Build an explicit portable allowlist from a valid run-local result."""

    source = validate_campaign_results(value)
    campaign_document = cast(Mapping[str, object], source["campaign"])
    runtime_campaign = {**campaign_document, "_path": source["campaign_path"]}
    jobs = model.campaign_jobs(runtime_campaign)
    worker_results = cast(Mapping[str, Mapping[str, object]], source["worker_results"])
    source_results = cast(Mapping[str, Mapping[str, object]], source["results"])
    projected_results: dict[str, dict[str, object]] = {
        "primary": {
            key: _project_mega_row(item) for key, item in source_results["primary"].items()
        },
        "controls": {
            key: _project_mega_row(item) for key, item in source_results["controls"].items()
        },
        "scaling": {
            key: _project_mega_row(item) for key, item in source_results["scaling"].items()
        },
        "bridges": {key: _project_bridge(item) for key, item in source_results["bridges"].items()},
    }
    evidence: dict[str, object] = {}
    source_evidence = cast(Mapping[str, Mapping[str, object]], source["evidence"])
    for job in jobs:
        result = worker_results[job.job_id]
        benchmark_environment = (
            cast(Mapping[str, object], result["payload"])["environment"]
            if job.kind == "bridge"
            else result["harness_environment"]
        )
        evidence[job.job_id] = {
            "worker_result_sha256": source_evidence[job.job_id]["worker_result_sha256"],
            "job_sha256": job.job_sha256,
            "attempt": result["attempt"],
            "kind": job.kind,
            "worker_id": job.worker_id,
            "interpreter_role": job.interpreter_role,
            "corpus_key": job.corpus_key,
            "result_key": job.result_key,
            "threads": job.threads,
            "cpus": job.cpus,
            "primary_eligible": job.primary_eligible,
            "started_utc": result["started_utc"],
            "finished_utc": result["finished_utc"],
            "host": result["host"],
            "cpu_affinity": _detached(result["cpu_affinity"]),
            "interpreter_probe_sha256": source_evidence[job.job_id]["interpreter_probe_sha256"],
            "benchmark_environment": _detached(benchmark_environment),
        }
    platform_value = cast(Mapping[str, object], source["platform"])
    projection: dict[str, object] = {
        "schema_version": model.RESULTS_SCHEMA,
        "artifact_kind": "portable",
        "run_id": source["run_id"],
        "campaign_sha256": source["campaign_sha256"],
        "merged_utc": source["merged_utc"],
        "platform_key": derive_linux_platform_key(
            platform_value["platform"], platform_value["machine"]
        ),
        "candidate": _project_candidate(source["candidate"]),
        "platform": _project_platform(source["platform"]),
        "inputs": _project_inputs(source["inputs"]),
        "results": projected_results,
        "evidence": evidence,
        "comparisons": _detached(source["comparisons"]),
        "gates": _detached(source["gates"]),
        "source_results_sha256": source["results_sha256"],
    }
    projection["projection_sha256"] = model.sha256_json(projection)
    return validate_portable_campaign_results(projection)


def _validate_portable_candidate(value: object) -> dict[str, object]:
    candidate = model.require_exact_keys(
        value,
        {"public_version", "cargo_version", "git_commit", "wheel"},
        "portable candidate",
    )
    wheel = model.require_exact_keys(
        candidate["wheel"],
        {"name", "bytes", "sha256", "metadata_version"},
        "portable candidate wheel",
    )
    if (
        candidate["public_version"] != model.PUBLIC_VERSION
        or candidate["cargo_version"] != model.CARGO_VERSION
        or type(candidate["git_commit"]) is not str
        or not re.fullmatch(r"[0-9a-f]{40}", candidate["git_commit"])
        or type(wheel["name"]) is not str
        or Path(wheel["name"]).name != wheel["name"]
        or type(wheel["bytes"]) is not int
        or wheel["bytes"] < 1
        or type(wheel["sha256"]) is not str
        or _SHA256_RE.fullmatch(wheel["sha256"]) is None
        or wheel["metadata_version"] != model.PUBLIC_VERSION
    ):
        raise model.CampaignError("portable candidate identity is invalid")
    return candidate


def _validate_portable_platform(value: object, platform_key: object) -> dict[str, object]:
    platform_value = model.require_exact_keys(value, _PORTABLE_PLATFORM_KEYS, "portable platform")
    expected_key = derive_linux_platform_key(platform_value["platform"], platform_value["machine"])
    if platform_key != expected_key:
        raise model.CampaignError("portable platform key differs from recorded platform")
    for key in ("allowed_cpus", "online_cpus"):
        cpus = platform_value[key]
        if (
            type(cpus) is not list
            or not cpus
            or any(type(cpu) is not int or cpu < 0 for cpu in cpus)
            or len(cpus) != len(set(cpus))
        ):
            raise model.CampaignError(f"portable platform {key} is invalid")
    if not isinstance(platform_value["physical_cpu_ids"], Mapping) or not isinstance(
        platform_value["numa_nodes"], Mapping
    ):
        raise model.CampaignError("portable platform topology is invalid")
    if any(
        type(platform_value[key]) is not int or platform_value[key] < 1
        for key in ("free_bytes", "available_ram_bytes")
    ):
        raise model.CampaignError("portable platform capacity is invalid")
    if type(platform_value["mount_fstype"]) is not str or not platform_value["mount_fstype"]:
        raise model.CampaignError("portable platform filesystem type is invalid")
    versions = model.require_exact_keys(
        platform_value["executable_versions"],
        {"findmnt", "taskset", "tmux"},
        "portable executable versions",
    )
    if any(type(item) is not str or not item for item in versions.values()):
        raise model.CampaignError("portable executable version is invalid")
    return platform_value


def _validate_portable_inputs(value: object) -> dict[str, object]:
    inputs = model.require_exact_keys(
        value,
        {*model.CORPUS_ORDER, "gencode-gtf-parent-stripped"},
        "portable inputs",
    )
    base_keys = {"filename", "bytes", "sha256", "gzip_crc_ok", "format"}
    for key in model.CORPUS_ORDER:
        item = model.require_exact_keys(inputs[key], base_keys, f"portable input {key}")
        registry = BY_KEY[key]
        expected = {
            "filename": registry["filename"],
            "bytes": registry["bytes"],
            "sha256": registry["sha256"],
            "gzip_crc_ok": True,
            "format": registry["fmt"],
        }
        if item != expected:
            raise model.CampaignError(f"portable input {key} differs from the corpus registry")
    derived = model.require_exact_keys(
        inputs["gencode-gtf-parent-stripped"],
        base_keys | {"manifest_sha256", "transform_version", "counts"},
        "portable parent-stripped input",
    )
    if (
        type(derived["filename"]) is not str
        or Path(derived["filename"]).name != derived["filename"]
        or type(derived["bytes"]) is not int
        or derived["bytes"] < 1
        or type(derived["sha256"]) is not str
        or _SHA256_RE.fullmatch(derived["sha256"]) is None
        or derived["gzip_crc_ok"] is not True
        or derived["format"] != "gtf"
        or type(derived["manifest_sha256"]) is not str
        or _SHA256_RE.fullmatch(derived["manifest_sha256"]) is None
        or derived["transform_version"] != "1"
    ):
        raise model.CampaignError("portable parent-stripped identity is invalid")
    counts = model.require_exact_keys(
        derived["counts"],
        {
            "input_feature_lines",
            "output_feature_lines",
            "comment_or_blank_lines",
            "removed_gene_rows",
            "removed_transcript_rows",
        },
        "portable parent-stripped counts",
    )
    if any(type(item) is not int or item < 0 for item in counts.values()) or counts[
        "input_feature_lines"
    ] != (
        counts["output_feature_lines"]
        + counts["removed_gene_rows"]
        + counts["removed_transcript_rows"]
    ):
        raise model.CampaignError("portable parent-stripped counts are invalid")
    return inputs


def _portable_job_semantics() -> dict[str, dict[str, object]]:
    semantics: dict[str, dict[str, object]] = {}
    for threads, cpus in zip(model.THREADS, model.LANE_CPUS, strict=True):
        for corpus_key in model.CORPUS_ORDER:
            job_id = f"scaling-t{threads:02d}-{corpus_key}"
            semantics[job_id] = {
                "kind": "scaling",
                "worker_id": f"scale-t{threads:02d}",
                "interpreter_role": "primary",
                "corpus_key": corpus_key,
                "result_key": corpus_key,
                "threads": threads,
                "cpus": cpus,
                "primary_eligible": False,
            }
    for corpus_key in model.CORPUS_ORDER:
        job_id = f"canonical-{corpus_key}"
        semantics[job_id] = {
            "kind": "primary",
            "worker_id": "canonical",
            "interpreter_role": "primary",
            "corpus_key": corpus_key,
            "result_key": corpus_key,
            "threads": 10,
            "cpus": "0-9",
            "primary_eligible": True,
        }
    for arm in ("default", "parent-stripped"):
        job_id = f"control-gencode-gtf-{arm}"
        semantics[job_id] = {
            "kind": "control",
            "worker_id": "canonical",
            "interpreter_role": "primary",
            "corpus_key": "gencode-gtf",
            "result_key": f"gencode-gtf-{arm}",
            "threads": 10,
            "cpus": "0-9",
            "primary_eligible": False,
        }
    for version in ("gffbase-0.1.0", "gffutils-0.13"):
        for corpus_key in ("mane", "chess"):
            job_id = f"bridge-{version}-{corpus_key}"
            semantics[job_id] = {
                "kind": "bridge",
                "worker_id": "canonical",
                "interpreter_role": version,
                "corpus_key": corpus_key,
                "result_key": corpus_key,
                "threads": 10,
                "cpus": "0-9",
                "primary_eligible": False,
            }
    if len(semantics) != 36:
        raise AssertionError("portable campaign semantics must contain exactly 36 jobs")
    return semantics


def _validate_portable_bridge_environment(
    value: object,
    *,
    job_id: str,
    engine: str,
    version: str,
    threads: int,
    affinity: list[int],
    platform_value: Mapping[str, object],
) -> dict[str, object]:
    environment = model.require_exact_keys(
        value, _BRIDGE_ENVIRONMENT_KEYS, f"{job_id} portable bridge environment"
    )
    model.require_schema(
        environment,
        BRIDGE_ENVIRONMENT_SCHEMA,
        f"{job_id} portable bridge environment",
    )
    _require_timestamp(environment["timestamp_utc"], f"{job_id} bridge environment time")
    for key, maximum in (("hostname", 255), ("platform", 512), ("machine", 80)):
        _require_text(environment[key], f"{job_id} bridge {key}", maximum=maximum)
    python = model.require_exact_keys(
        environment["python"],
        {"version", "implementation", "executable"},
        f"{job_id} bridge Python",
    )
    package = model.require_exact_keys(
        environment["package"], {"name", "version"}, f"{job_id} bridge package"
    )
    if (
        python["implementation"] != "CPython"
        or type(python["version"]) is not str
        or re.fullmatch(r"3\.(?:10|11|12|13|14)\.(?:0|[1-9][0-9]*)", python["version"]) is None
        or type(python["executable"]) is not str
        or not python["executable"].startswith("python")
        or Path(python["executable"]).name != python["executable"]
        or package != {"name": engine, "version": version}
        or environment["cpu_affinity"] != affinity
        or not benchmark_bounded_environment_is_valid(environment["benchmark_env"], threads=threads)
        or not str(environment["platform"]).casefold().startswith("linux")
        or str(environment["machine"]).casefold() != str(platform_value["machine"]).casefold()
    ):
        raise model.CampaignError(f"{job_id}: portable bridge environment is invalid")
    return environment


def _validate_portable_evidence(
    value: object,
    *,
    candidate: Mapping[str, object],
    platform_value: Mapping[str, object],
) -> tuple[dict[str, object], dict[str, dict[str, object]]]:
    semantics = _portable_job_semantics()
    evidence = model.require_exact_keys(value, set(semantics), "portable job evidence")
    normalized: dict[str, dict[str, object]] = {}
    for job_id, expected in semantics.items():
        item = model.require_exact_keys(
            evidence[job_id], _PORTABLE_EVIDENCE_KEYS, f"portable evidence {job_id}"
        )
        for key, expected_value in expected.items():
            if item[key] != expected_value:
                raise model.CampaignError(f"portable evidence {job_id}.{key} differs")
        for key in ("worker_result_sha256", "job_sha256", "interpreter_probe_sha256"):
            if type(item[key]) is not str or _SHA256_RE.fullmatch(item[key]) is None:
                raise model.CampaignError(f"portable evidence {job_id}.{key} is invalid")
        if type(item["attempt"]) is not int or not 1 <= item["attempt"] <= 9999:
            raise model.CampaignError(f"portable evidence {job_id}.attempt is invalid")
        started = _require_timestamp(item["started_utc"], f"portable evidence {job_id} start")
        finished = _require_timestamp(item["finished_utc"], f"portable evidence {job_id} finish")
        if finished < started:
            raise model.CampaignError(f"portable evidence {job_id} finishes before it starts")
        if type(item["host"]) is not str or _HOST_RE.fullmatch(item["host"]) is None:
            raise model.CampaignError(f"portable evidence {job_id}.host is invalid")
        affinity = list(model.parse_cpu_list(cast(str, expected["cpus"])))
        if item["cpu_affinity"] != affinity:
            raise model.CampaignError(f"portable evidence {job_id}.cpu_affinity differs")
        environment = item["benchmark_environment"]
        if expected["kind"] == "bridge":
            engine = "gffbase" if expected["interpreter_role"] == "gffbase-0.1.0" else "gffutils"
            version = "0.1.0" if engine == "gffbase" else "0.13"
            _validate_portable_bridge_environment(
                environment,
                job_id=job_id,
                engine=engine,
                version=version,
                threads=cast(int, expected["threads"]),
                affinity=affinity,
                platform_value=platform_value,
            )
        else:
            threads = cast(int, expected["threads"])
            if not benchmark_environment_evidence_is_valid(environment, threads=threads):
                raise model.CampaignError(f"portable evidence {job_id} environment is invalid")
            harness = cast(Mapping[str, object], environment)
            artifact = cast(Mapping[str, object], harness["artifact"])
            wheel = cast(Mapping[str, object], candidate["wheel"])
            if (
                harness["git_commit"] != candidate["git_commit"]
                or harness["cpu_affinity"] != affinity
                or harness["env"] != benchmark_env(threads)
                or artifact["wheel"] != wheel["name"]
                or artifact["wheel_sha256"] != wheel["sha256"]
                or not str(harness["platform"]).casefold().startswith("linux")
                or str(harness["machine"]).casefold() != str(platform_value["machine"]).casefold()
            ):
                raise model.CampaignError(f"portable evidence {job_id} harness binding differs")
        normalized[job_id] = item
    return evidence, normalized


def _portable_input_for_job(
    inputs: Mapping[str, object], semantics: Mapping[str, object]
) -> Mapping[str, object]:
    if semantics["kind"] == "control" and semantics["result_key"] == (
        "gencode-gtf-parent-stripped"
    ):
        return cast(Mapping[str, object], inputs["gencode-gtf-parent-stripped"])
    return cast(Mapping[str, object], inputs[cast(str, semantics["corpus_key"])])


def _validate_portable_mega_row(
    value: object,
    *,
    job_id: str,
    semantics: Mapping[str, object],
    input_value: Mapping[str, object],
    evidence: Mapping[str, object],
    candidate_identity: Mapping[str, object],
) -> dict[str, object]:
    row = model.require_exact_keys(value, _PORTABLE_MEGA_ROW_KEYS, f"portable row {job_id}")
    corpus_key = cast(str, semantics["corpus_key"])
    result_key = cast(str, semantics["result_key"])
    threads = cast(int, semantics["threads"])
    kind = cast(str, semantics["kind"])
    if row["name"] != BY_KEY[corpus_key]["name"] or row["key"] != result_key:
        raise model.CampaignError(f"portable row {job_id} corpus identity differs")
    if row["input_bytes"] != input_value["bytes"] or row["input_sha256"] != input_value["sha256"]:
        raise model.CampaignError(f"portable row {job_id} input identity differs")
    if type(row["feature_lines"]) is not int or row["feature_lines"] < 1:
        raise model.CampaignError(f"portable row {job_id} feature count is invalid")
    if kind == "control" and result_key == "gencode-gtf-parent-stripped":
        counts = cast(Mapping[str, object], input_value["counts"])
        if row["feature_lines"] != counts["output_feature_lines"]:
            raise model.CampaignError(f"portable row {job_id} transform count differs")
    measured = model.require_exact_keys(
        row["measured"],
        {"timestamp_utc", "git_commit", "git_dirty"},
        f"portable row {job_id} measured",
    )
    measured_time = _require_timestamp(
        measured["timestamp_utc"], f"portable row {job_id} measured time", allow_utc_offset=True
    )
    if (
        measured["git_commit"] != candidate_identity["git_commit"]
        or measured["git_dirty"] is not False
    ):
        raise model.CampaignError(f"portable row {job_id} measured identity differs")
    params = model.require_exact_keys(
        row["params"], _MEGA_PARAM_KEYS, f"portable row {job_id} params"
    )
    arm: str | None = None
    inference: bool | None = None
    if corpus_key == "gencode-gtf":
        arm = (
            "default"
            if result_key == "gencode-gtf-default"
            else "parent-stripped"
            if result_key == "gencode-gtf-parent-stripped"
            else "no-infer"
        )
        inference = arm != "no-infer"
    expected_params = {
        "legacy_cap_seconds": 5400,
        "gffbase_cap_seconds": 3600,
        "n_spatial": 5000,
        "n_batched": 5000,
        "repeats": 5,
        "region_seed": model.REGION_SEED,
        "threads": threads,
        "gtf_arm": arm,
        "infer_gtf_parents": inference,
        "validation_sample": "10000" if kind == "scaling" else "all",
        "benchmark_env": benchmark_env(threads),
    }
    if params != expected_params:
        raise model.CampaignError(f"portable row {job_id} parameters differ")
    harness = cast(Mapping[str, object], evidence["benchmark_environment"])
    harness_time = _require_timestamp(harness["timestamp_utc"], f"portable row {job_id} harness")
    if measured_time > harness_time:
        raise model.CampaignError(f"portable row {job_id} was measured after its environment")
    candidate = row["gffbase"]
    if not isinstance(candidate, dict) or not benchmark_candidate_evidence_is_valid(
        candidate,
        require_exhaustive=kind in {"primary", "control"},
        require_rtree=True,
    ):
        raise model.CampaignError(f"portable row {job_id} candidate is invalid")
    filename = cast(str, input_value["filename"])
    if (
        candidate["benchmark_env"] != expected_params["benchmark_env"]
        or candidate["cap_seconds"] != expected_params["gffbase_cap_seconds"]
        or candidate["fmt"] != input_value["format"]
        or candidate["label"] != f"gffbase ingest({filename})"
        or candidate["validation"]["requested_sample"] != expected_params["validation_sample"]
    ):
        raise model.CampaignError(f"portable row {job_id} candidate controls differ")
    for section in ("spatial", "batched"):
        error = benchmark_query_evidence_error(section, row[section], candidate, params)
        if error:
            raise model.CampaignError(f"portable row {job_id}: {error}")
    if kind == "scaling":
        if row["spatial"] != {
            "state": "skipped",
            "reason": "candidate completion/validation/signature/R-tree failed",
        } or row["batched"] != {
            "state": "skipped",
            "reason": "candidate completion/validation/signature failed",
        }:
            raise model.CampaignError(f"portable row {job_id} scaling skips differ")
    elif (
        cast(Mapping[str, object], row["spatial"])["state"] != "completed"
        or cast(Mapping[str, object], row["batched"])["state"] != "completed"
    ):
        raise model.CampaignError(f"portable row {job_id} canonical queries are incomplete")
    legacy = row["legacy"]
    if kind == "scaling":
        if (
            legacy
            != {
                "state": "skipped",
                "reason": "requested by --skip-legacy",
                "exit_code": None,
                "wall_seconds": None,
                "cap_seconds": 5400,
                "n_features": None,
            }
            or row["ingest_speedup"] is not None
        ):
            raise model.CampaignError(f"portable row {job_id} scaling comparator differs")
    else:
        if not isinstance(legacy, dict) or not benchmark_comparator_evidence_is_valid(legacy):
            raise model.CampaignError(f"portable row {job_id} comparator is invalid")
        if (
            legacy["benchmark_env"] != benchmark_env(threads)
            or legacy["cap_seconds"] != 5400
            or legacy["label"] != f"legacy gffutils ingest({filename})"
        ):
            raise model.CampaignError(f"portable row {job_id} comparator controls differ")
        if legacy["state"] == "timed_out":
            if row["ingest_speedup"] is not None:
                raise model.CampaignError(f"portable row {job_id} timeout carries a ratio")
        else:
            matched = signatures_match(
                candidate["correctness_signature"], legacy["correctness_signature"]
            )
            ratio = (
                float(legacy["wall_seconds"]) / float(candidate["wall_seconds"])
                if matched is True
                else None
            )
            if (ratio is None and row["ingest_speedup"] is not None) or (
                ratio is not None and not _numbers_equal(row["ingest_speedup"], ratio)
            ):
                raise model.CampaignError(f"portable row {job_id} ratio differs")
    return row


def _validate_portable_bridge(
    value: object,
    *,
    job_id: str,
    semantics: Mapping[str, object],
    input_value: Mapping[str, object],
    evidence: Mapping[str, object],
    platform_value: Mapping[str, object],
) -> dict[str, object]:
    bridge = model.require_exact_keys(value, _PORTABLE_BRIDGE_KEYS, f"portable bridge {job_id}")
    model.require_schema(bridge, BRIDGE_SCHEMA, f"portable bridge {job_id}")
    role = cast(str, semantics["interpreter_role"])
    engine = "gffbase" if role == "gffbase-0.1.0" else "gffutils"
    version = "0.1.0" if engine == "gffbase" else "0.13"
    if (
        bridge["label"] != job_id.removeprefix("bridge-")
        or bridge["engine"] != engine
        or bridge["package_version"] != version
    ):
        raise model.CampaignError(f"portable bridge {job_id} identity differs")
    bridge_input = model.require_exact_keys(
        bridge["input"], {"bytes", "sha256"}, f"portable bridge {job_id} input"
    )
    if bridge_input != {
        "bytes": input_value["bytes"],
        "sha256": input_value["sha256"],
    }:
        raise model.CampaignError(f"portable bridge {job_id} input differs")
    database = model.require_exact_keys(
        bridge["database"], {"bytes"}, f"portable bridge {job_id} database"
    )
    if type(database["bytes"]) is not int or database["bytes"] < 1:
        raise model.CampaignError(f"portable bridge {job_id} database size is invalid")
    params = model.require_exact_keys(
        bridge["params"], {"fmt", "threads"}, f"portable bridge {job_id} params"
    )
    if params != {"fmt": input_value["format"], "threads": semantics["threads"]}:
        raise model.CampaignError(f"portable bridge {job_id} parameters differ")
    measurement = model.require_exact_keys(
        bridge["measurement"],
        {"wall_seconds", "n_features", "correctness_signature", "validation"},
        f"portable bridge {job_id} measurement",
    )
    if not _positive_number(measurement["wall_seconds"]) or (
        type(measurement["n_features"]) is not int or measurement["n_features"] < 1
    ):
        raise model.CampaignError(f"portable bridge {job_id} measurement is invalid")
    signature = measurement["correctness_signature"]
    if not isinstance(signature, dict) or not validate_database_signature(signature):
        raise model.CampaignError(f"portable bridge {job_id} signature is invalid")
    if signature["feature_count"] != measurement["n_features"]:
        raise model.CampaignError(f"portable bridge {job_id} feature count differs")
    if engine == "gffbase":
        report = model.require_exact_keys(
            measurement["validation"],
            {"ok", "checked", "errors"},
            f"portable bridge {job_id} validation",
        )
        if (
            report["ok"] is not True
            or type(report["checked"]) is not list
            or not report["checked"]
            or report["errors"] != []
        ):
            raise model.CampaignError(f"portable bridge {job_id} validation failed")
    elif measurement["validation"] is not None:
        raise model.CampaignError(f"portable bridge {job_id} validation must be null")
    affinity = cast(list[int], evidence["cpu_affinity"])
    environment = _validate_portable_bridge_environment(
        bridge["environment"],
        job_id=job_id,
        engine=engine,
        version=version,
        threads=cast(int, semantics["threads"]),
        affinity=affinity,
        platform_value=platform_value,
    )
    if model.canonical_json_bytes(environment) != model.canonical_json_bytes(
        evidence["benchmark_environment"]
    ):
        raise model.CampaignError(f"portable bridge {job_id} environment evidence differs")
    return bridge


def _derive_portable_comparisons_and_gates(
    sections: Mapping[str, Mapping[str, object]],
) -> tuple[dict[str, dict[str, object]], dict[str, object]]:
    comparisons: dict[str, dict[str, object]] = {
        "primary": {},
        "controls": {},
        "bridges": {},
    }
    signature_gates: dict[str, bool | None] = {}
    for key, value in sections["primary"].items():
        row = cast(Mapping[str, object], value)
        comparison = _comparison_with_engines(
            cast(Mapping[str, object], row["gffbase"]),
            cast(Mapping[str, object], row["legacy"]),
            candidate_engine=model.PUBLIC_VERSION,
            comparator_engine="gffutils-0.14",
            primary_eligible=True,
        )
        comparisons["primary"][key] = comparison
        signature_gates[key] = cast(bool | None, comparison["signature_match"])
    for key, value in sections["controls"].items():
        row = cast(Mapping[str, object], value)
        comparisons["controls"][key] = _comparison_with_engines(
            cast(Mapping[str, object], row["gffbase"]),
            cast(Mapping[str, object], row["legacy"]),
            candidate_engine=model.PUBLIC_VERSION,
            comparator_engine="gffutils-0.14",
            primary_eligible=False,
        )
    for corpus_key in ("mane", "chess"):
        primary = cast(Mapping[str, object], sections["primary"][corpus_key])
        for version, reference_key in (
            ("gffbase-0.1.0", "gffbase"),
            ("gffutils-0.13", "legacy"),
        ):
            bridge_id = f"bridge-{version}-{corpus_key}"
            bridge = cast(Mapping[str, object], sections["bridges"][bridge_id])
            measurement = cast(Mapping[str, object], bridge["measurement"])
            comparisons["bridges"][bridge_id] = _comparison_with_engines(
                cast(Mapping[str, object], primary[reference_key]),
                {**measurement, "state": "completed", "exit_code": 0},
                candidate_engine=(
                    model.PUBLIC_VERSION if reference_key == "gffbase" else "gffutils-0.14"
                ),
                comparator_engine=version,
                primary_eligible=False,
            )
    cardinalities = {section: len(values) for section, values in sections.items()}
    failures = [
        f"primary signature mismatch: {key}"
        for key, matched in signature_gates.items()
        if matched is False
    ]
    gates: dict[str, object] = {
        "all_36_jobs_present_once": True,
        "namespace_cardinalities": cardinalities,
        "identities_match": True,
        "full_validation": True,
        "canonical_rtree": True,
        "primary_signature_matches": signature_gates,
        "publishable": not failures,
        "failures": failures,
    }
    return comparisons, gates


def _contains_absolute_path(value: object) -> bool:
    if isinstance(value, Mapping):
        return any(
            _contains_absolute_path(key) or _contains_absolute_path(item)
            for key, item in value.items()
        )
    if isinstance(value, (list, tuple)):
        return any(_contains_absolute_path(item) for item in value)
    if type(value) is not str:
        return False
    if value.startswith(("/", "~/", "~\\")) or "://" in value:
        return True
    return bool(re.match(r"^[A-Za-z]:[\\/]", value))


def validate_portable_campaign_results(value: object) -> dict[str, object]:
    """Validate a standalone explicit portable ``campaign-results-v2``."""

    record = model.require_exact_keys(value, _PORTABLE_RESULT_KEYS, "portable campaign results")
    model.require_schema(record, model.RESULTS_SCHEMA, "portable campaign results")
    if record["artifact_kind"] != "portable":
        raise model.CampaignError("portable campaign results artifact_kind differs")
    model.validate_identifier(cast(str, record["run_id"]), "run")
    for key in ("campaign_sha256", "source_results_sha256", "projection_sha256"):
        if type(record[key]) is not str or _SHA256_RE.fullmatch(record[key]) is None:
            raise model.CampaignError(f"portable campaign results {key} is invalid")
    _require_timestamp(record["merged_utc"], "portable campaign results merged_utc")
    unsigned = {key: item for key, item in record.items() if key != "projection_sha256"}
    if model.sha256_json(unsigned) != record["projection_sha256"]:
        raise model.CampaignError("portable campaign projection digest mismatch")
    if _contains_absolute_path(record):
        raise model.CampaignError("portable campaign results contain a private absolute path")
    candidate = _validate_portable_candidate(record["candidate"])
    platform_value = _validate_portable_platform(record["platform"], record["platform_key"])
    inputs = _validate_portable_inputs(record["inputs"])
    _raw_evidence, evidence = _validate_portable_evidence(
        record["evidence"], candidate=candidate, platform_value=platform_value
    )
    sections = model.require_exact_keys(
        record["results"], {"primary", "controls", "scaling", "bridges"}, "portable results"
    )
    expected_sections = {
        "primary": set(model.CORPUS_ORDER),
        "controls": {"gencode-gtf-default", "gencode-gtf-parent-stripped"},
        "scaling": {
            f"scaling-t{threads:02d}-{corpus}"
            for threads in model.THREADS
            for corpus in model.CORPUS_ORDER
        },
        "bridges": {
            f"bridge-{version}-{corpus}"
            for version in ("gffbase-0.1.0", "gffutils-0.13")
            for corpus in ("mane", "chess")
        },
    }
    normalized_sections: dict[str, dict[str, object]] = {}
    semantics = _portable_job_semantics()
    for section, expected_keys in expected_sections.items():
        items = model.require_exact_keys(sections[section], expected_keys, f"portable {section}")
        normalized_sections[section] = items
        for result_key, item in items.items():
            if section == "primary":
                job_id = f"canonical-{result_key}"
            elif section == "controls":
                arm = result_key.removeprefix("gencode-gtf-")
                job_id = f"control-gencode-gtf-{arm}"
            else:
                job_id = result_key
            job_semantics = semantics[job_id]
            input_value = _portable_input_for_job(inputs, job_semantics)
            if section == "bridges":
                _validate_portable_bridge(
                    item,
                    job_id=job_id,
                    semantics=job_semantics,
                    input_value=input_value,
                    evidence=evidence[job_id],
                    platform_value=platform_value,
                )
            else:
                _validate_portable_mega_row(
                    item,
                    job_id=job_id,
                    semantics=job_semantics,
                    input_value=input_value,
                    evidence=evidence[job_id],
                    candidate_identity=candidate,
                )
    expected_comparisons, expected_gates = _derive_portable_comparisons_and_gates(
        normalized_sections
    )
    if model.canonical_json_bytes(record["comparisons"]) != model.canonical_json_bytes(
        expected_comparisons
    ):
        raise model.CampaignError("portable comparisons differ from measured evidence")
    if model.canonical_json_bytes(record["gates"]) != model.canonical_json_bytes(expected_gates):
        raise model.CampaignError("portable gates differ from measured evidence")
    return _detached(record)


def _publication_artifact_path(root: Path, value: object, label: str) -> Path:
    relative = _portable_relative_path(value, label)
    target = safe_io.lexical_absolute(root / relative, label)
    try:
        target.relative_to(root)
    except ValueError as exc:  # defensive after the lexical component checks
        raise model.CampaignError(f"{label} escapes the publication root") from exc
    return target


def _load_public_json(path: Path, validator: Any) -> dict[str, object]:
    loaded = safe_io.strict_public_json_load(path, validator=validator)
    if not isinstance(loaded, dict):
        raise model.CampaignError(f"JSON validator returned a non-object: {path}")
    return cast(dict[str, object], loaded)


def _validate_index_candidate(value: object, label: str) -> dict[str, object]:
    candidate = model.require_exact_keys(value, {"version", "wheel_sha256", "git_commit"}, label)
    if (
        candidate["version"] != model.PUBLIC_VERSION
        or type(candidate["wheel_sha256"]) is not str
        or _SHA256_RE.fullmatch(candidate["wheel_sha256"]) is None
        or type(candidate["git_commit"]) is not str
        or re.fullmatch(r"[0-9a-f]{40}", candidate["git_commit"]) is None
    ):
        raise model.CampaignError(f"{label} is invalid")
    return candidate


def validate_results_index(
    value: object,
    publish_root: Path,
    *,
    verify_references: bool = True,
) -> dict[str, object]:
    """Validate the closed index and, by default, every referenced artifact."""

    if type(verify_references) is not bool:
        raise model.CampaignError("verify_references must be a boolean")
    root = safe_io.lexical_absolute(publish_root, "publication root")
    safe_io.inspect_path(root, require_kind="directory")
    index = model.require_exact_keys(value, _INDEX_KEYS, "results index")
    model.require_schema(index, model.INDEX_SCHEMA, "results index")
    platforms = index["platforms"]
    if not isinstance(platforms, Mapping) or not platforms:
        raise model.CampaignError("results index platforms must be a non-empty object")
    normalized_platforms: dict[str, object] = {}
    for platform_key, raw_platform in platforms.items():
        if (
            type(platform_key) is not str
            or re.fullmatch(r"(?:linux-(?:x86_64|aarch64)|macos-(?:arm64|x86_64))", platform_key)
            is None
        ):
            raise model.CampaignError(f"results index platform key is invalid: {platform_key!r}")
        platform_entry = model.require_exact_keys(
            raw_platform, _INDEX_PLATFORM_KEYS, f"results index platform {platform_key}"
        )
        kind = platform_entry["kind"]
        if kind not in {"campaign-v2", "legacy-opaque"}:
            raise model.CampaignError(f"results index platform {platform_key} kind is invalid")
        if (kind == "campaign-v2") is not platform_key.startswith("linux-"):
            raise model.CampaignError(f"results index platform {platform_key} kind differs")
        canonical_run_id = model.validate_identifier(
            cast(str, platform_entry["canonical_run_id"]), "canonical run"
        )
        runs = platform_entry["runs"]
        if not isinstance(runs, Mapping) or canonical_run_id not in runs:
            raise model.CampaignError(
                f"results index platform {platform_key} lacks its canonical run"
            )
        normalized_runs: dict[str, object] = {}
        for run_id, raw_run in runs.items():
            if type(run_id) is not str:
                raise model.CampaignError("results index run id must be a string")
            model.validate_identifier(run_id, "indexed run")
            if kind == "campaign-v2":
                run = model.require_exact_keys(
                    raw_run,
                    _CAMPAIGN_INDEX_RUN_KEYS,
                    f"results index campaign run {run_id}",
                )
                if run["kind"] != kind or run["schema_version"] != model.RESULTS_SCHEMA:
                    raise model.CampaignError(f"results index campaign run {run_id} kind differs")
                for key in (
                    "sha256",
                    "projection_sha256",
                    "source_results_sha256",
                    "campaign_sha256",
                ):
                    if type(run[key]) is not str or _SHA256_RE.fullmatch(run[key]) is None:
                        raise model.CampaignError(
                            f"results index campaign run {run_id}.{key} is invalid"
                        )
                _require_timestamp(run["published_utc"], f"indexed run {run_id} publication")
                candidate = _validate_index_candidate(
                    run["candidate"], f"results index campaign run {run_id} candidate"
                )
                target = _publication_artifact_path(
                    root, run["artifact"], f"results index campaign run {run_id} artifact"
                )
                expected_relative = (
                    Path("platform") / platform_key / run_id / "campaign-results.json"
                ).as_posix()
                if run["artifact"] != expected_relative:
                    raise model.CampaignError(
                        f"results index campaign run {run_id} artifact path differs"
                    )
                if verify_references:
                    if safe_io.sha256_regular_file(target, require_unique=True) != run["sha256"]:
                        raise model.CampaignError(
                            f"results index campaign run {run_id} artifact digest differs"
                        )
                    portable = _load_public_json(target, validate_portable_campaign_results)
                    portable_candidate = cast(Mapping[str, object], portable["candidate"])
                    portable_wheel = cast(Mapping[str, object], portable_candidate["wheel"])
                    if (
                        portable["run_id"] != run_id
                        or portable["platform_key"] != platform_key
                        or portable["projection_sha256"] != run["projection_sha256"]
                        or portable["source_results_sha256"] != run["source_results_sha256"]
                        or portable["campaign_sha256"] != run["campaign_sha256"]
                        or candidate
                        != {
                            "version": portable_candidate["public_version"],
                            "wheel_sha256": portable_wheel["sha256"],
                            "git_commit": portable_candidate["git_commit"],
                        }
                    ):
                        raise model.CampaignError(
                            f"results index campaign run {run_id} metadata differs from artifact"
                        )
            else:
                run = model.require_exact_keys(
                    raw_run,
                    _LEGACY_INDEX_RUN_KEYS,
                    f"results index legacy run {run_id}",
                )
                if run["kind"] != kind or run["schema_version"] != "legacy-opaque":
                    raise model.CampaignError(f"results index legacy run {run_id} kind differs")
                if type(run["sha256"]) is not str or _SHA256_RE.fullmatch(run["sha256"]) is None:
                    raise model.CampaignError(
                        f"results index legacy run {run_id} digest is invalid"
                    )
                _require_text(run["description"], f"results index legacy run {run_id} description")
                target = _publication_artifact_path(
                    root, run["artifact"], f"results index legacy run {run_id} artifact"
                )
                if (
                    verify_references
                    and safe_io.sha256_regular_file(target, require_unique=True) != run["sha256"]
                ):
                    raise model.CampaignError(
                        f"results index legacy run {run_id} artifact digest differs"
                    )
            normalized_runs[run_id] = run
        normalized_platforms[platform_key] = {
            "kind": kind,
            "canonical_run_id": canonical_run_id,
            "runs": normalized_runs,
        }
    return {
        "schema_version": model.INDEX_SCHEMA,
        "platforms": normalized_platforms,
    }


def _initial_results_index(publish_root: Path) -> dict[str, object]:
    platforms: dict[str, object] = {}
    historical = publish_root / "06_mega.json"
    if historical.exists():
        digest = safe_io.sha256_regular_file(historical, require_unique=True)
        if digest != HISTORICAL_MAC_SHA256:
            raise model.CampaignError(
                "historical Mac artifact differs from its byte-preservation digest"
            )
        platforms["macos-arm64"] = {
            "kind": "legacy-opaque",
            "canonical_run_id": HISTORICAL_MAC_RUN_ID,
            "runs": {
                HISTORICAL_MAC_RUN_ID: {
                    "kind": "legacy-opaque",
                    "artifact": "06_mega.json",
                    "sha256": HISTORICAL_MAC_SHA256,
                    "schema_version": "legacy-opaque",
                    "description": "Historical macOS benchmark retained byte-for-byte",
                }
            },
        }
    return {"schema_version": model.INDEX_SCHEMA, "platforms": platforms}


def _ensure_publication_subdirectories(root: Path, relative_parent: Path) -> None:
    current = root
    for part in relative_parent.parts:
        current /= part
        safe_io.create_public_directory(current, exist_ok=True)


def publish_campaign(campaign_results: object, publish_root: Path) -> Path:
    """Publish one validated portable Linux result and update a strict v2 index."""

    source = validate_campaign_results(campaign_results)
    gates = cast(Mapping[str, object], source["gates"])
    if gates["publishable"] is not True:
        raise model.CampaignError("campaign result did not pass publication gates")
    projection = project_campaign_results(source)
    platform_key = cast(str, projection["platform_key"])
    run_id = cast(str, projection["run_id"])
    root = safe_io.lexical_absolute(publish_root, "publication root")
    safe_io.create_public_directory(root, exist_ok=True)
    relative = Path("platform") / platform_key / run_id / "campaign-results.json"
    _ensure_publication_subdirectories(root, relative.parent)
    target = root / relative
    safe_io.atomic_create_public_json(target, projection)
    reloaded = _load_public_json(target, validate_portable_campaign_results)
    if model.canonical_json_bytes(reloaded) != model.canonical_json_bytes(projection):
        raise model.CampaignError("reloaded portable projection differs from publication")
    artifact_sha256 = safe_io.sha256_regular_file(target, require_unique=True)

    candidate = cast(Mapping[str, object], projection["candidate"])
    wheel = cast(Mapping[str, object], candidate["wheel"])
    entry = {
        "kind": "campaign-v2",
        "artifact": relative.as_posix(),
        "sha256": artifact_sha256,
        "schema_version": model.RESULTS_SCHEMA,
        "projection_sha256": projection["projection_sha256"],
        "source_results_sha256": projection["source_results_sha256"],
        "campaign_sha256": projection["campaign_sha256"],
        "candidate": {
            "version": candidate["public_version"],
            "wheel_sha256": wheel["sha256"],
            "git_commit": candidate["git_commit"],
        },
        "published_utc": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
    }
    index_path = root / "index.json"
    with safe_io.campaign_lock(root, cast(str, projection["campaign_sha256"])):
        if index_path.exists():
            index = _load_public_json(
                index_path,
                lambda item: validate_results_index(item, root),
            )
        else:
            index = _initial_results_index(root)
            if cast(Mapping[str, object], index["platforms"]):
                validate_results_index(index, root)
        platforms = cast(dict[str, object], index["platforms"])
        raw_linux = platforms.get(platform_key)
        if raw_linux is None:
            linux: dict[str, object] = {
                "kind": "campaign-v2",
                "canonical_run_id": run_id,
                "runs": {},
            }
            platforms[platform_key] = linux
        else:
            linux = cast(dict[str, object], raw_linux)
            if linux.get("kind") != "campaign-v2":
                raise model.CampaignError(f"conflicting index kind for {platform_key}")
        runs = cast(dict[str, object], linux["runs"])
        existing = runs.get(run_id)
        if existing is not None:
            stable_existing = {
                key: value
                for key, value in cast(Mapping[str, object], existing).items()
                if key != "published_utc"
            }
            stable_entry = {key: value for key, value in entry.items() if key != "published_utc"}
            if stable_existing != stable_entry:
                raise model.CampaignError(f"conflicting published index entry for {run_id}")
            entry = cast(dict[str, object], existing)
        else:
            runs[run_id] = entry
        linux["canonical_run_id"] = run_id
        safe_io.atomic_write_public_json_durable(index_path, index)
    final_index = _load_public_json(
        index_path,
        lambda item: validate_results_index(item, root),
    )
    final_platform = cast(Mapping[str, object], final_index["platforms"])[platform_key]
    final_run = cast(Mapping[str, object], cast(Mapping[str, object], final_platform)["runs"])[
        run_id
    ]
    if cast(Mapping[str, object], final_run)["sha256"] != artifact_sha256:
        raise model.CampaignError("final results index digest differs from published artifact")
    return target


__all__ = [
    "BRIDGE_ENVIRONMENT_SCHEMA",
    "BRIDGE_SCHEMA",
    "HISTORICAL_MAC_RUN_ID",
    "HISTORICAL_MAC_SHA256",
    "derive_linux_platform_key",
    "merge_campaign",
    "project_campaign_results",
    "publish_campaign",
    "strict_ratio",
    "validate_campaign_results",
    "validate_portable_campaign_results",
    "validate_results_index",
    "validate_terminal_worker_result",
    "validate_worker_result",
]
