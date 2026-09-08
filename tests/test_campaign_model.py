"""Pure contracts for the cluster campaign model.

These tests never inspect the host, write campaign artifacts, or invoke tmux.
"""

from __future__ import annotations

import copy
import dataclasses
import hashlib
import math
from pathlib import Path

import pytest

from benchmarks.campaign import model


def _inputs(root: Path) -> dict[str, dict[str, object]]:
    values: dict[str, dict[str, object]] = {}
    for key in model.CORPUS_ORDER:
        values[key] = {
            "path": str(root / f"{key}.gz"),
            "bytes": len(key),
            "sha256": hashlib.sha256(key.encode()).hexdigest(),
            "gzip_crc_ok": True,
            "format": "gtf" if key == "gencode-gtf" else "gff3",
        }
    values["gencode-gtf-parent-stripped"] = {
        "path": str(root / "parent-stripped.gtf.gz"),
        "bytes": 123,
        "sha256": hashlib.sha256(b"parent-stripped").hexdigest(),
        "gzip_crc_ok": True,
        "format": "gtf",
        "manifest_path": str(root / "parent-stripped.manifest.json"),
        "manifest_sha256": hashlib.sha256(b"manifest").hexdigest(),
        "transform_version": "1",
        "counts": {
            "input_feature_lines": 12,
            "output_feature_lines": 10,
            "comment_or_blank_lines": 3,
            "removed_gene_rows": 1,
            "removed_transcript_rows": 1,
        },
    }
    return values


def _campaign(tmp_path: Path) -> dict[str, object]:
    spec = {
        "repo": {},
        # `path` as well as `name`: preflight records both, and the worker
        # environment has to hand the CONSUMER a path it can stat.
        "candidate": {
            "wheel": {
                "name": "gffbase-0.2.0rc1.whl",
                "path": "/staged/wheels/gffbase-0.2.0rc1.whl",
                "sha256": "a" * 64,
            }
        },
        "interpreters": {
            role: {"resolved_executable": str(tmp_path / role / "bin" / "python")}
            for role in model.INTERPRETER_ROLES
        },
        "inputs": _inputs(tmp_path),
        "topology": model.topology(),
        "parameters": model.binding_parameters(),
        "resources": {},
    }
    spec["jobs"] = [
        job.to_dict()
        for job in model.build_job_matrix(spec["parameters"], spec["interpreters"], spec["inputs"])
    ]
    return {
        "schema_version": model.CAMPAIGN_SCHEMA,
        "run_id": "unit-run",
        "created_utc": "2026-08-30T12:00:00Z",
        "spec_sha256": model.sha256_json(spec),
        "spec": spec,
    }


def test_schema_release_and_binding_constants_are_exact() -> None:
    assert model.CAMPAIGN_SCHEMA == "campaign-v2"
    assert model.PREFLIGHT_SCHEMA == "campaign-preflight-v2"
    assert model.STATUS_SCHEMA == "campaign-status-v2"
    assert model.WORKER_SCHEMA == "benchmark-worker-v2"
    assert model.RESULTS_SCHEMA == "campaign-results-v2"
    assert model.INDEX_SCHEMA == "results-index-v2"
    assert model.SIGNATURE_SCHEMA == "database-signature-v3"
    assert model.PUBLIC_VERSION == "0.2.0rc1"
    assert model.CARGO_VERSION == "0.2.0-rc.1"
    assert model.binding_parameters() == {
        "legacy_timeout": 5400,
        "gffbase_timeout": 3600,
        "validation_sample": "10000",
        "n_spatial": 5000,
        "n_batched": 5000,
        "repeats": 5,
        "region_seed": 20260501,
    }


def test_cpu_parser_lane_and_topology_are_strict_and_exact() -> None:
    assert model.parse_cpu_list("0-2,4,6-7") == (0, 1, 2, 4, 6, 7)
    assert model.topology() == {
        "lanes": [
            {"worker_id": "scale-t01", "threads": 1, "cpus": "0-9"},
            {"worker_id": "scale-t02", "threads": 2, "cpus": "10-19"},
            {"worker_id": "scale-t04", "threads": 4, "cpus": "20-29"},
            {"worker_id": "scale-t08", "threads": 8, "cpus": "30-39"},
            {"worker_id": "scale-t10", "threads": 10, "cpus": "40-49"},
        ],
        "canonical": {"worker_id": "canonical", "threads": 10, "cpus": "0-9"},
    }
    lane = model.CpuLane.from_dict(model.topology()["lanes"][0])
    assert lane.cpu_set == tuple(range(10))
    with pytest.raises(dataclasses.FrozenInstanceError):
        lane.threads = 2  # type: ignore[misc]
    with pytest.raises(model.CampaignError):
        model.CpuLane.from_dict({"worker_id": "scale-t01", "threads": True, "cpus": "0-9"})
    with pytest.raises(model.CampaignError):
        model.CpuLane.from_dict(
            {
                "worker_id": "scale-t01",
                "threads": 1,
                "cpus": "0-9",
                "unknown": None,
            }
        )

    for malformed in (
        "",
        " 0",
        "0 ",
        "00",
        "+0",
        "٠",
        "1-",
        "-1",
        "2-1",
        "0,,1",
        "0-2,2",
        "1,0",
        "0-0",
        "0,1",
        "0-1,2",
        "0-1000000000",
        "9" * 5000,
    ):
        with pytest.raises(model.CampaignError, match="CPU"):
            model.parse_cpu_list(malformed)
    for malformed in (True, 1, None):
        with pytest.raises(model.CampaignError, match="CPU"):
            model.parse_cpu_list(malformed)  # type: ignore[arg-type]


def test_matrix_is_the_exact_ordered_36_job_release_profile(tmp_path: Path) -> None:
    jobs = model.build_job_matrix({}, {}, _inputs(tmp_path))
    expected_ids = [
        f"scaling-t{threads:02d}-{corpus}"
        for threads in model.THREADS
        for corpus in model.CORPUS_ORDER
    ]
    expected_ids.extend(f"canonical-{corpus}" for corpus in model.CORPUS_ORDER)
    expected_ids.extend(("control-gencode-gtf-default", "control-gencode-gtf-parent-stripped"))
    expected_ids.extend(
        f"bridge-{version}-{corpus}"
        for version in ("gffbase-0.1.0", "gffutils-0.13")
        for corpus in ("mane", "chess")
    )

    assert [job.job_id for job in jobs] == expected_ids
    assert len({job.job_sha256 for job in jobs}) == 36
    assert [job.kind for job in jobs[:25]] == ["scaling"] * 25
    assert [job.kind for job in jobs[25:]] == ["primary"] * 5 + ["control"] * 2 + ["bridge"] * 4

    for job in jobs[:25]:
        assert job.engine_set == "candidate-only"
        assert job.parameters["validation_sample"] == "10000"
        assert job.parameters["repeats"] == 5
        assert job.primary_eligible is False
    for job in jobs[25:32]:
        assert job.engine_set == "candidate-vs-gffutils-0.14"
        assert job.parameters["validation_sample"] == "all"
    assert all(job.cpus == "0-9" and job.threads == 10 for job in jobs[25:])
    assert all(job.primary_eligible == (job.kind == "primary") for job in jobs)
    mega_jobs = jobs[:32]
    assert all(job.parameters["legacy_timeout"] == 5400 for job in mega_jobs)
    assert all(job.parameters["gffbase_timeout"] == 3600 for job in mega_jobs)
    assert all(job.parameters["n_spatial"] == 5000 for job in mega_jobs)
    assert all(job.parameters["n_batched"] == 5000 for job in mega_jobs)
    assert all(job.parameters["region_seed"] == 20260501 for job in mega_jobs)
    assert [dict(job.parameters) for job in jobs[32:]] == [
        {"bridge_engine": "gffbase", "cap_seconds": 5400},
        {"bridge_engine": "gffbase", "cap_seconds": 5400},
        {"bridge_engine": "gffutils", "cap_seconds": 5400},
        {"bridge_engine": "gffutils", "cap_seconds": 5400},
    ]


def test_identifiers_are_ascii_only_and_have_a_closed_type_contract() -> None:
    assert model.validate_identifier("a") == "a"
    assert model.validate_identifier("a" * 80) == "a" * 80
    for malformed in (
        "",
        "a" * 81,
        ".hidden",
        "a..b",
        "UPPER",
        "a/b",
        "a\\b",
        "é",
        "e\N{COMBINING ACUTE ACCENT}",
        "\N{CYRILLIC SMALL LETTER A}bc",
        "abc\N{ZERO WIDTH SPACE}",
        "abc\x00",
        "abc\n",
    ):
        with pytest.raises(model.CampaignError, match="identifier"):
            model.validate_identifier(malformed)
    for malformed in (True, 1, None):
        with pytest.raises(model.CampaignError, match="identifier"):
            model.validate_identifier(malformed)  # type: ignore[arg-type]


def test_canonical_json_and_digest_have_stable_utf8_semantics() -> None:
    left = {"b": 1, "a": "é"}
    right = {"a": "é", "b": 1}
    expected = b'{"a":"\xc3\xa9","b":1}'
    assert model.canonical_json_bytes(left) == expected
    assert model.canonical_json_bytes(right) == expected
    assert model.sha256_json(left) == (
        "aa58fba8483623bed37c1b02edfccbdd9a53123837c20bfa4cb4049993a2872e"
    )
    with pytest.raises(model.CampaignError):
        model.canonical_json_bytes({"value": math.nan})
    with pytest.raises(model.CampaignError):
        model.canonical_json_bytes({"value": Path("not-json")})
    with pytest.raises(model.CampaignError):
        model.canonical_json_bytes({1: "non-string-key"})
    with pytest.raises(model.CampaignError):
        model.canonical_json_bytes({"array": (1, 2)})


def test_canonical_json_rejects_bounded_resource_exhaustion_inputs() -> None:
    nested: object = None
    for _ in range(100):
        nested = [nested]
    with pytest.raises(model.CampaignError, match="depth"):
        model.canonical_json_bytes(nested)
    with pytest.raises(model.CampaignError, match="integer"):
        model.canonical_json_bytes({"value": 10**1000})
    with pytest.raises(model.CampaignError, match="string"):
        model.canonical_json_bytes({"value": "x" * 1_000_001})
    with pytest.raises(model.CampaignError, match="node"):
        model.canonical_json_bytes([None] * 100_001)


def test_job_spec_is_closed_digest_bound_and_deeply_immutable(tmp_path: Path) -> None:
    inputs = _inputs(tmp_path)
    job = model.build_job_matrix({}, {}, inputs)[0]
    value = job.to_dict()
    assert model.job_digest(job) == value["job_sha256"]
    assert model.JobSpec.from_dict(value) == job
    with pytest.raises(dataclasses.FrozenInstanceError):
        job.threads = 2  # type: ignore[misc]
    with pytest.raises(TypeError):
        job.parameters["repeats"] = 1  # type: ignore[index]
    with pytest.raises(TypeError):
        job.input["bytes"] = 0  # type: ignore[index]
    inputs["mane"]["bytes"] = 999
    assert job.input["bytes"] == len("mane")

    for mutation in (
        lambda item: item.pop("kind"),
        lambda item: item.__setitem__("unknown", None),
        lambda item: item.__setitem__("threads", True),
        lambda item: item["parameters"].__setitem__("repeats", True),
        lambda item: item["parameters"].__setitem__("extra", 1),
        lambda item: item["input"].__setitem__("extra", 1),
        lambda item: item.__setitem__("job_sha256", "0" * 64),
    ):
        broken = job.to_dict()
        mutation(broken)
        with pytest.raises(model.CampaignError):
            model.JobSpec.from_dict(broken)


@pytest.mark.parametrize(
    "config",
    [
        {"legacy_timeout": True},
        {"legacy_timeout": "5400"},
        {"legacy_timeout": 5400.0},
        {**model.binding_parameters(), "repeats": 1},
        {**model.binding_parameters(), "validation_sample": "all"},
        {**model.binding_parameters(), "unknown": 1},
    ],
)
def test_matrix_rejects_coerced_partial_or_non_binding_config(
    tmp_path: Path, config: dict[str, object]
) -> None:
    with pytest.raises(model.CampaignError):
        model.build_job_matrix(config, {}, _inputs(tmp_path))


def test_mega_bridge_and_worker_argv_are_deterministic(tmp_path: Path) -> None:
    campaign = _campaign(tmp_path)
    campaign["_path"] = str(tmp_path / "campaign.json")
    jobs = model.campaign_jobs(campaign)
    attempt = tmp_path / "attempt"

    scaling = next(job for job in jobs if job.job_id == "scaling-t04-mane")
    scaling_argv = model.build_mega_argv(scaling, attempt, campaign)
    assert scaling_argv == [
        str(tmp_path / "primary" / "bin" / "python"),
        "-I",
        str(model.ROOT / "benchmarks" / "06_mega.py"),
        "--only",
        "mane",
        "--threads",
        "4",
        "--gtf-arm",
        "no-infer",
        "--legacy-timeout",
        "5400",
        "--gffbase-timeout",
        "3600",
        "--validation-sample",
        "10000",
        "--n-spatial",
        "5000",
        "--n-batched",
        "5000",
        "--repeats",
        "5",
        "--skip-legacy",
    ]
    assert not {"--publish", "--rederive", "--keep-db", "--no-purge"} & set(scaling_argv)

    primary = next(job for job in jobs if job.job_id == "canonical-mane")
    primary_argv = model.build_mega_argv(primary, attempt, campaign)
    assert primary_argv[primary_argv.index("--validation-sample") + 1] == "all"
    assert "--skip-legacy" not in primary_argv

    raw_control = next(job for job in jobs if job.job_id == "control-gencode-gtf-default")
    raw_control_argv = model.build_mega_argv(raw_control, attempt, campaign)
    assert raw_control_argv[raw_control_argv.index("--gtf-arm") + 1] == "default"
    assert "--gtf-input" not in raw_control_argv

    control = next(job for job in jobs if job.job_id == "control-gencode-gtf-parent-stripped")
    control_argv = model.build_mega_argv(control, attempt, campaign)
    assert control_argv[control_argv.index("--gtf-arm") + 1] == "parent-stripped"
    assert control_argv[control_argv.index("--gtf-input") + 1] == control.input["path"]

    bridge = next(job for job in jobs if job.job_id == "bridge-gffbase-0.1.0-mane")
    bridge_argv = model.build_bridge_argv(bridge, attempt, campaign)
    assert bridge_argv[0] == str(tmp_path / "gffbase-0.1.0" / "bin" / "python")
    assert bridge_argv[bridge_argv.index("--engine") + 1] == "gffbase"
    assert bridge_argv[bridge_argv.index("--label") + 1] == "gffbase-0.1.0-mane"

    gffutils_bridge = next(job for job in jobs if job.job_id == "bridge-gffutils-0.13-chess")
    gffutils_argv = model.build_bridge_argv(gffutils_bridge, attempt, campaign)
    assert gffutils_argv == [
        str(tmp_path / "gffutils-0.13" / "bin" / "python"),
        "-I",
        str(model.ROOT / "benchmarks" / "bridge.py"),
        "--engine",
        "gffutils",
        "--input",
        gffutils_bridge.input["path"],
        "--database",
        str((attempt / "scratch" / "bridge.duckdb").resolve()),
        "--output",
        str((attempt / "raw.json").resolve()),
        "--format",
        "gff3",
        "--threads",
        "10",
        "--label",
        "gffutils-0.13-chess",
    ]

    worker_argv = model.build_worker_argv(campaign, "scale-t04", resume=True)
    assert worker_argv[-1] == "--resume"
    assert worker_argv[worker_argv.index("--campaign-sha256") + 1] == campaign["spec_sha256"]


def test_worker_environment_is_bounded_and_ambient_secret_free(tmp_path: Path) -> None:
    campaign = _campaign(tmp_path)
    job = model.campaign_jobs(campaign)[0]
    env = model.worker_environment(
        campaign,
        job,
        tmp_path / "attempt",
        base_environment={
            "PATH": "/usr/bin",
            "HOME": "/safe/home",
            "LANG": "C.UTF-8",
            "PYTHONPATH": "/workspace/python",
            "SECRET_TOKEN": "must-not-leak",
        },
    )

    assert env["PATH"] == "/usr/bin"
    assert env["HOME"] == "/safe/home"
    assert env["GFFBASE_THREADS"] == env["GFFUTILS2_THREADS"] == "1"
    # The path, not the name: the consumer stats it. See
    # `test_the_candidate_wheel_is_passed_as_a_usable_path`.
    assert env["GFFBASE_BENCH_WHEEL"] == "/staged/wheels/gffbase-0.2.0rc1.whl"
    assert env["GFFBASE_BENCH_WHEEL_SHA256"] == "a" * 64
    assert env["GFFBASE_BENCH_OUT"] == str((tmp_path / "attempt" / "scratch").resolve())
    assert "PYTHONPATH" not in env
    assert "SECRET_TOKEN" not in env
    for key in (
        "OMP_NUM_THREADS",
        "OPENBLAS_NUM_THREADS",
        "MKL_NUM_THREADS",
        "NUMEXPR_NUM_THREADS",
        "VECLIB_MAXIMUM_THREADS",
        "BLIS_NUM_THREADS",
    ):
        assert env[key] == "1"


def test_worker_environment_default_is_independent_of_ambient_values(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    campaign = _campaign(tmp_path)
    job = model.campaign_jobs(campaign)[0]
    monkeypatch.setenv("PATH", "/hostile/bin")
    monkeypatch.setenv("HOME", "/hostile/home")
    monkeypatch.setenv("PYTHONPATH", "/hostile/python")
    monkeypatch.setenv("LD_PRELOAD", "/hostile/library.so")
    monkeypatch.setenv("SECRET_TOKEN", "secret")

    first = model.worker_environment(campaign, job, tmp_path / "attempt")
    monkeypatch.setenv("PATH", "/different/bin")
    second = model.worker_environment(campaign, job, tmp_path / "attempt")

    assert first == second
    assert not {"PATH", "HOME", "PYTHONPATH", "LD_PRELOAD", "SECRET_TOKEN"} & set(first)


@pytest.mark.parametrize("missing", ["path", "sha256"])
def test_worker_environment_rejects_missing_candidate_wheel_identity(
    tmp_path: Path, missing: str
) -> None:
    campaign = copy.deepcopy(_campaign(tmp_path))
    campaign["spec"]["candidate"]["wheel"].pop(missing)
    campaign["spec_sha256"] = model.sha256_json(campaign["spec"])
    job = model.campaign_jobs(campaign)[0]
    with pytest.raises(model.CampaignError, match="wheel"):
        model.worker_environment(campaign, job, tmp_path / "attempt")


# ---------------------------------------------------------------------------
# The candidate wheel travels as a PATH
# ---------------------------------------------------------------------------
#
# `common._candidate_wheel_artifact` reads `GFFBASE_BENCH_WHEEL`, calls
# `Path(...).is_file()` on it, and refuses to stamp an environment if that
# fails. `worker_environment` was passing the wheel's NAME, which resolves
# against the worker's cwd -- the repo root -- where no wheel exists, so every
# job died after finishing its measurement:
#
#     ValueError: candidate wheel path is not a file
#
# Both sides were tested and neither test could fail: the consumer's test sets
# the variable to a real path, and the producer's asserted only that the key
# was present. The contract between them is what nobody checked.


def test_the_candidate_wheel_is_passed_as_a_usable_path(tmp_path: Path) -> None:
    campaign = _campaign(tmp_path)
    job = model.campaign_jobs(campaign)[0]
    env = model.worker_environment(campaign, job, tmp_path / "attempt")

    wheel = campaign["spec"]["candidate"]["wheel"]
    assert env["GFFBASE_BENCH_WHEEL"] == wheel["path"]
    assert env["GFFBASE_BENCH_WHEEL"] != wheel["name"], (
        "a bare filename resolves against the worker's cwd, not the wheel"
    )
    assert Path(env["GFFBASE_BENCH_WHEEL"]).is_absolute()
    assert env["GFFBASE_BENCH_WHEEL_SHA256"] == wheel["sha256"]
