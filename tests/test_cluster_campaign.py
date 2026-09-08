"""Fast contract tests for the cluster campaign controller.

All inputs and subprocess identities are tiny fakes.  These tests never start
tmux, download a corpus, or invoke the multi-hour benchmark primitives.
"""

from __future__ import annotations

import gzip
import hashlib
import json
import os
import shlex
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from benchmarks import cluster_campaign as campaign
from benchmarks.campaign import preflight as campaign_preflight
from benchmarks.common import FULL_VALIDATION_IDS, benchmark_env, sha256_file
from benchmarks.corpora import BY_KEY
from benchmarks.prepare_gtf_control import build_parent_stripped_gtf


def _signature(seed: str = "a") -> dict:
    digest = (seed * 64)[:64]
    value = {
        "schema_version": campaign.SIGNATURE_SCHEMA,
        "segment_count": 10,
        "segments_sha256": digest,
        "attribute_count": 20,
        "attributes_sha256": digest,
        "direct_relationship_count": 5,
        "direct_relationships_sha256": digest,
        "closure_count": 5,
        "closure_sha256": digest,
        "feature_count": 10,
        "featuretype_histogram": [["gene", 10]],
    }
    value["combined_sha256"] = hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    return value


def _inputs(tmp_path: Path) -> dict:
    values = {}
    for key in campaign.CORPUS_ORDER:
        registry = BY_KEY[key]
        path = tmp_path / str(registry["filename"])
        path.write_bytes(key.encode())
        values[key] = {
            "path": str(path),
            # Unit campaign files are tiny placeholders; the immutable
            # campaign identity still uses the real release corpus registry.
            "bytes": registry["bytes"],
            "sha256": registry["sha256"],
            "gzip_crc_ok": True,
            "format": "gtf" if key == "gencode-gtf" else "gff3",
        }
    control = tmp_path / "parent-stripped.gtf.gz"
    control.write_bytes(b"control")
    manifest = tmp_path / "parent-stripped.manifest.json"
    manifest.write_bytes(b"manifest")
    values["gencode-gtf-parent-stripped"] = {
        "path": str(control),
        "bytes": control.stat().st_size,
        "sha256": sha256_file(control),
        "gzip_crc_ok": True,
        "format": "gtf",
        "manifest_path": str(manifest),
        "manifest_sha256": sha256_file(manifest),
        "transform_version": "1",
        "counts": {
            "input_feature_lines": 3,
            "output_feature_lines": 1,
            "comment_or_blank_lines": 0,
            "removed_gene_rows": 1,
            "removed_transcript_rows": 1,
        },
    }
    return values


def _probe(role: str) -> dict:
    package_names = ("gffbase", "gffutils", "duckdb", "pyarrow", "psutil", "numpy")
    package_versions = {name: None for name in package_names}
    package_versions.update(
        {
            "primary": {
                "gffbase": campaign.PUBLIC_VERSION,
                "gffutils": "0.14",
                "duckdb": "1.4.1",
                "pyarrow": "21.0.0",
                "psutil": "7.0.0",
                "numpy": "2.3.2",
            },
            "gffbase-0.1.0": {
                "gffbase": "0.1.0",
                "duckdb": "1.4.1",
                "pyarrow": "21.0.0",
                "psutil": "7.0.0",
                "numpy": "2.3.2",
            },
            "gffutils-0.13": {"gffutils": "0.13", "psutil": "7.0.0"},
        }[role]
    )
    unsigned = {
        "role": role,
        "resolved_executable": sys.executable,
        "requested_executable": sys.executable,
        "executable_identity": {
            "st_dev": 1,
            "st_ino": 1,
            "st_size": 1,
            "st_mtime_ns": 1,
            "st_ctime_ns": 1,
        },
        "prefix": sys.prefix,
        "implementation": "CPython",
        "python_version": "3.11.0",
        "isolated": 1,
        "sys_path": [sys.prefix],
        "packages": package_versions,
        "modules": {
            "stub": {"path": str(Path(sys.prefix) / "stub.so"), "version": None, "sha256": "7" * 64}
        },
        "distribution_roots": {},
        "direct_url": {name: None for name in package_names},
        "pth_files": [],
    }
    return {**unsigned, "probe_sha256": campaign.sha256_json(unsigned)}


def _unit_filesystems(run_dir: Path) -> list[dict[str, object]]:
    paths = [run_dir / "campaign.json", run_dir / "preflight.json"]
    return [
        {
            "identity": {
                "mount_id": 1,
                "parent_mount_id": 0,
                "device": "0:1",
                "st_dev": 1,
                "mount_root": "/",
                "mount_point": "/",
                "mount_options": ["rw"],
                "optional_fields": [],
                "filesystem_type": "unitfs",
                "source": "unit",
                "super_options": ["rw"],
            },
            "mutable_targets": [str(run_dir)],
            "probe_target": str(run_dir),
            "capabilities": {
                "probe_path": str(
                    run_dir / campaign_preflight.PROBE_ROOT_NAME / f"probe-{'9' * 32}"
                ),
                "st_dev": 1,
                "name_max": 255,
                "path_max": 4096,
                "directory_fsync": True,
                "rename_noreplace_same_directory": True,
                "rename_noreplace_collision": True,
                "rename_noreplace_cross_directory": True,
                "rename_exchange_same_directory": True,
                "parents_fsynced": True,
            },
            "path_budget": campaign_preflight.validate_encoded_path_budget(
                paths,
                name_max=255,
                path_max=4096,
            ),
        }
    ]


def _unit_resources(run_dir: Path) -> dict[str, object]:
    executable_identity = {
        "st_dev": 1,
        "st_ino": 1,
        "st_size": 1,
        "st_mtime_ns": 1,
        "sha256": "8" * 64,
    }
    names = ("findmnt", "taskset", "tmux")
    return {
        "platform": "Linux",
        "machine": "x86_64",
        "allowed_cpus": list(range(50)),
        "online_cpus": list(range(50)),
        "physical_cpu_ids": {str(index): [str(index), "0"] for index in range(50)},
        "numa_nodes": {"node0": list(range(50))},
        "free_bytes": 100,
        "available_ram_bytes": 100,
        "mount": {"raw": "unit", "fstype": "unitfs", "probe_path": str(run_dir)},
        "executables": {name: f"/usr/bin/{name}" for name in names},
        "executable_versions": {name: "unit 1" for name in names},
        "executable_identities": {name: dict(executable_identity) for name in names},
        "thresholds": {
            "free_bytes": 1,
            "available_ram_bytes": 1,
            "disk_formula": "unit",
            "ram_formula": "unit",
        },
    }


def test_transform_facade_uses_only_the_no_follow_validated_source_snapshot(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = tmp_path / "source.gtf.gz"
    output = tmp_path / "parent-stripped.gtf.gz"
    manifest = tmp_path / "parent-stripped.manifest.json"
    with source.open("wb") as raw:
        with gzip.GzipFile(filename="", mode="wb", fileobj=raw, mtime=0) as compressed:
            compressed.write(
                b'chr1\tsrc\tgene\t1\t2\t.\t+\t.\tgene_id "g";\n'
                b'chr1\tsrc\ttranscript\t1\t2\t.\t+\t.\tgene_id "g"; transcript_id "t";\n'
                b'chr1\tsrc\texon\t1\t2\t.\t+\t.\tgene_id "g";\n'
            )
    source.chmod(0o600)
    build_parent_stripped_gtf(source, output, manifest)
    registry = {
        "bytes": source.stat().st_size,
        "sha256": sha256_file(source),
    }
    monkeypatch.setattr(campaign, "corpus_path", lambda _entry: source)

    def forbidden(*args: object, **kwargs: object) -> None:
        raise AssertionError("legacy path-following corpus verification must not run")

    monkeypatch.setattr(campaign._campaign_preflight, "verify_input_file", forbidden)
    result = campaign.verify_transform_manifest(manifest, output, registry)

    assert result["sha256"] == sha256_file(output)


def _make_campaign(tmp_path: Path) -> dict:
    path = tmp_path / "unit-run" / "campaign.json"
    inputs = _inputs(tmp_path)
    interpreters = {role: _probe(role) for role in ("primary", "gffbase-0.1.0", "gffutils-0.13")}
    parameters = campaign.binding_parameters()
    jobs = campaign.build_job_matrix(parameters, interpreters, inputs)
    filesystems = _unit_filesystems(path.parent)
    resources = _unit_resources(path.parent)
    spec = {
        "repo": {
            "root": str(campaign.ROOT),
            "commit": "1" * 40,
            "branch": "unit",
            "dirty": False,
            "status": [],
            "dirty_submodules": [],
        },
        "candidate": {
            "public_version": campaign.PUBLIC_VERSION,
            "cargo_version": campaign.CARGO_VERSION,
            "git_commit": "1" * 40,
            "wheel": {
                "path": str(tmp_path / "gffbase-0.2.0rc1-cp310-abi3-manylinux_2_34_x86_64.whl"),
                "name": "gffbase-0.2.0rc1-cp310-abi3-manylinux_2_34_x86_64.whl",
                "bytes": 1,
                "sha256": "2" * 64,
                "metadata_version": campaign.PUBLIC_VERSION,
            },
        },
        "interpreters": interpreters,
        "inputs": inputs,
        "topology": campaign.topology(),
        "parameters": parameters,
        "resources": {**resources, "filesystems": filesystems},
        "jobs": [job.to_dict() for job in jobs],
    }
    value = {
        "schema_version": campaign.CAMPAIGN_SCHEMA,
        "run_id": "unit-run",
        "created_utc": "2026-08-26T00:00:00Z",
        "spec_sha256": campaign.sha256_json(spec),
        "spec": spec,
    }
    path.parent.mkdir(mode=0o700)
    path.parent.chmod(0o700)
    campaign_preflight.probe_filesystem_capabilities(
        path.parent,
        token_provider=lambda: "9" * 32,
    )
    request = campaign_preflight.normalize_request(
        run_id="unit-run",
        campaign_root=tmp_path,
        candidate_wheel=(tmp_path / "gffbase-0.2.0rc1-cp310-abi3-manylinux_2_34_x86_64.whl"),
        interpreters={role: probe["resolved_executable"] for role, probe in interpreters.items()},
        transform_mode="external",
        parent_stripped=inputs["gencode-gtf-parent-stripped"]["path"],
        parent_stripped_manifest=inputs["gencode-gtf-parent-stripped"]["manifest_path"],
        parameters=parameters,
    )
    session = f"gffbase-{value['spec_sha256'][:12]}-cluster"
    evidence = {
        "repo_before": spec["repo"],
        "repo_after": dict(spec["repo"]),
        "candidate": {
            "before": spec["candidate"]["wheel"],
            "after": json.loads(json.dumps(spec["candidate"]["wheel"])),
        },
        "interpreters": {
            role: {
                "before": interpreters[role],
                "after": json.loads(json.dumps(interpreters[role])),
            }
            for role in campaign.INTERPRETER_ROLES
        },
        "inputs": {"before": inputs, "after": json.loads(json.dumps(inputs))},
        "transform": {
            "before": inputs["gencode-gtf-parent-stripped"],
            "after": json.loads(json.dumps(inputs["gencode-gtf-parent-stripped"])),
        },
        "resources": {"before": resources, "after": json.loads(json.dumps(resources))},
        "filesystems": filesystems,
        "topology": campaign.topology(),
        "matrix": {"job_count": 36, "jobs_sha256": campaign.sha256_json(spec["jobs"])},
        "session": {"names": [session, f"{session}-canonical"], "before": [], "after": []},
    }
    marker = campaign_preflight.build_preflight_document(
        request=request,
        campaign=value,
        evidence=evidence,
        verified_utc="2026-08-26T00:00:01Z",
    )
    campaign_preflight.commit_campaign_bundle(
        path.parent,
        value,
        marker,
        phase_validator=lambda phase: campaign_preflight.validate_run_tree(
            path.parent,
            transform_mode="external",
            phase=phase,
        ),
    )
    return campaign._load_campaign(path)


def _mega_row(job: campaign.JobSpec, *, signature: dict | None = None) -> dict:
    signature = signature or _signature()
    checked_ids = list(FULL_VALIDATION_IDS)
    checked_ids.insert(7, "INV-8")
    sample_eligible = 10
    expected_filename = Path(str(job.input["path"])).name
    candidate = {
        "label": f"gffbase ingest({expected_filename})",
        "peak_rss_bytes": 8 * 1024 * 1024,
        "peak_rss_mb": 8.0,
        "exit_code": 0,
        "state": "completed",
        "benchmark_env": benchmark_env(job.threads),
        "cap_seconds": job.parameters["gffbase_timeout"],
        "wall_seconds": 10.0,
        "n_features": 10,
        "rtree_built": True,
        "correctness_signature": signature,
        "fmt": job.input["format"],
        "validation": {
            "ok": True,
            "level": "full",
            "checked": list(checked_ids),
            "skipped": [],
            "errors": [],
            "warnings": [],
            "requested_sample": job.parameters["validation_sample"],
            "sample_eligible": sample_eligible,
            "sample_checked": sample_eligible,
            "checked_ids": checked_ids,
        },
        "disk_bytes": 1024,
    }
    legacy = (
        {
            "state": "skipped",
            "reason": "requested by --skip-legacy",
            "exit_code": None,
            "wall_seconds": None,
            "cap_seconds": job.parameters["legacy_timeout"],
            "n_features": None,
        }
        if job.kind == "scaling"
        else {
            "label": f"legacy gffutils ingest({expected_filename})",
            "peak_rss_bytes": 16 * 1024 * 1024,
            "peak_rss_mb": 16.0,
            "exit_code": 0,
            "state": "completed",
            "benchmark_env": benchmark_env(job.threads),
            "cap_seconds": job.parameters["legacy_timeout"],
            "wall_seconds": 20.0,
            "disk_bytes": 2048,
            "n_features": 10,
            "correctness_signature": signature,
        }
    )
    if job.kind == "scaling":
        spatial = {
            "state": "skipped",
            "reason": "candidate completion/validation/signature/R-tree failed",
        }
        batched = {
            "state": "skipped",
            "reason": "candidate completion/validation/signature failed",
        }
    else:
        spatial = {
            "state": "completed",
            "n_queries": job.parameters["n_spatial"],
            "wall_seconds": 1.0,
            "qps": float(job.parameters["n_spatial"]),
            "total_features_returned": 10,
            "timing": {
                "median": 1.0,
                "min": 1.0,
                "max": 1.0,
                "values": [1.0] * job.parameters["repeats"],
                "n": job.parameters["repeats"],
            },
        }
        batched = {
            "state": "completed",
            "n_anchors": 10,
            "n_descendants": 20,
            "wall_seconds": 1.0,
            "qps": 10.0,
            "timing": {
                "median": 1.0,
                "min": 1.0,
                "max": 1.0,
                "values": [1.0] * job.parameters["repeats"],
                "n": job.parameters["repeats"],
            },
        }
    expected_arm = job.gtf_arm if job.corpus_key == "gencode-gtf" else None
    return {
        "name": BY_KEY[job.corpus_key]["name"],
        "key": job.result_key,
        "measured": {
            "timestamp_utc": "2026-08-26T00:00:00Z",
            "git_commit": "1" * 40,
            "git_dirty": False,
        },
        "input": f"unit/{expected_filename}",
        "input_bytes": job.input["bytes"],
        "input_sha256": job.input["sha256"],
        "feature_lines": (
            job.input["counts"]["output_feature_lines"]
            if job.kind == "control" and job.gtf_arm == "parent-stripped"
            else 10
        ),
        "gffbase": candidate,
        "legacy": legacy,
        "ingest_speedup": None if job.kind == "scaling" else 2.0,
        "spatial": spatial,
        "batched": batched,
        "db_paths": {
            "gffbase": f"unit/{job.result_key}.duckdb",
            "legacy": f"unit/{job.result_key}_legacy.sqlite",
        },
        "params": {
            "legacy_cap_seconds": job.parameters["legacy_timeout"],
            "gffbase_cap_seconds": job.parameters["gffbase_timeout"],
            "n_spatial": job.parameters["n_spatial"],
            "n_batched": job.parameters["n_batched"],
            "repeats": job.parameters["repeats"],
            "region_seed": job.parameters["region_seed"],
            "threads": job.threads,
            "validation_sample": job.parameters["validation_sample"],
            "gtf_arm": expected_arm,
            "infer_gtf_parents": (expected_arm != "no-infer" if expected_arm is not None else None),
            "benchmark_env": benchmark_env(job.threads),
        },
    }


def _harness_environment(value: dict, job: campaign.JobSpec) -> dict:
    wheel = value["spec"]["candidate"]["wheel"]
    native_sha256 = "9" * 64
    probe = value["spec"]["interpreters"]["primary"]
    return {
        "timestamp_utc": "2026-08-26T00:00:01Z",
        "git_commit": value["spec"]["repo"]["commit"],
        "git_dirty": False,
        "hostname": "unit-host",
        "platform": "Linux-6.0.0-x86_64-with-glibc2.34",
        "machine": "x86_64",
        "libc": {"family": "glibc", "version": "2.34"},
        "cpu_model": "Unit Test CPU",
        "cpu_cores_physical": 50,
        "cpu_cores_logical": 64,
        "total_ram_bytes": 128 * (1 << 30),
        "free_disk_bytes": 100 * (1 << 30),
        "cpu_affinity": list(campaign.parse_cpu_list(job.cpus)),
        "python": {
            "version": probe["python_version"],
            "implementation": probe["implementation"],
            "executable": Path(probe["resolved_executable"]).name,
        },
        "rustc_version": "rustc 1.89.0",
        "packages": {
            "gffbase": campaign.PUBLIC_VERSION,
            "duckdb": "1.4.1",
            "pyarrow": "21.0.0",
            "pandas": "2.3.2",
            "polars": "1.32.3",
            "gffutils": "0.14",
            "psutil": "7.0.0",
        },
        "gffbase_install": {
            "distribution_version": campaign.PUBLIC_VERSION,
            "python_version": campaign.PUBLIC_VERSION,
            "python_module": "__init__.py",
            "native_version": campaign.PUBLIC_VERSION,
            "native_module": "_native.abi3.so",
            "native_sha256": native_sha256,
        },
        "artifact": {
            "wheel": wheel["name"],
            "wheel_sha256": wheel["sha256"],
            "metadata": {"name": "gffbase", "version": campaign.PUBLIC_VERSION},
            "wheel_tags": ["cp310-abi3-manylinux_2_34_x86_64"],
            "native": {"member": "gffbase/_native.abi3.so", "sha256": native_sha256},
        },
        "env": benchmark_env(job.threads),
    }


def _worker_result(value: dict, job: campaign.JobSpec) -> dict:
    attempt_dir = Path(value["_path"]).parent / "jobs" / job.job_id / "attempts" / "0001"
    pane_pid = 1001
    child_pid = 2001
    boot_id = "12345678-1234-1234-1234-123456789abc"
    pane = {
        "session": (
            f"gffbase-{campaign.campaign_digest(value)[:12]}-cluster"
            + ("-canonical" if job.worker_id == "canonical" else "")
        ),
        "window": job.worker_id,
        "pane_id": "%1",
        "pane_pid": pane_pid,
        "pane_dead": False,
    }
    worker_process = {
        "pid": pane_pid,
        "pgid": pane_pid,
        "process_start_ticks": 100,
        "boot_id": boot_id,
    }
    child_process = {
        "pid": child_pid,
        "pgid": child_pid,
        "process_start_ticks": 200,
        "boot_id": boot_id,
    }
    if job.kind == "bridge":
        version = "0.1.0" if job.interpreter_role == "gffbase-0.1.0" else "0.13"
        probe = value["spec"]["interpreters"][job.interpreter_role]
        payload = {
            "schema_version": campaign._campaign_results.BRIDGE_SCHEMA,
            "label": job.job_id.removeprefix("bridge-"),
            "engine": job.parameters["bridge_engine"],
            "package_version": version,
            "input": {
                "path": str(Path(job.input["path"]).resolve()),
                "bytes": job.input["bytes"],
                "sha256": job.input["sha256"],
            },
            "database": {
                "path": str(attempt_dir / "scratch" / "bridge.duckdb"),
                "bytes": 1024,
            },
            "measurement": {
                "wall_seconds": 30.0,
                "n_features": 10,
                "correctness_signature": _signature(),
                "validation": (
                    {"ok": True, "checked": ["INV-1"], "errors": []}
                    if job.parameters["bridge_engine"] == "gffbase"
                    else None
                ),
            },
            "params": {"fmt": job.input["format"], "threads": job.threads},
            "environment": {
                "schema_version": campaign._campaign_results.BRIDGE_ENVIRONMENT_SCHEMA,
                "timestamp_utc": "2026-08-26T00:00:01Z",
                "hostname": "unit-host",
                "platform": "Linux-6.0.0-x86_64",
                "machine": "x86_64",
                "python": {
                    "version": probe["python_version"],
                    "implementation": probe["implementation"],
                    "executable": Path(probe["resolved_executable"]).name,
                },
                "package": {"name": job.parameters["bridge_engine"], "version": version},
                "cpu_affinity": list(campaign.parse_cpu_list(job.cpus)),
                "benchmark_env": benchmark_env(job.threads),
            },
        }
        argv = campaign.build_bridge_argv(job, attempt_dir, value)
        harness_environment = {}
    else:
        payload = _mega_row(job)
        argv = campaign.build_mega_argv(job, attempt_dir, value)
        harness_environment = _harness_environment(value, job)
    return {
        "schema_version": campaign.WORKER_SCHEMA,
        "run_id": value["run_id"],
        "campaign_sha256": campaign.campaign_digest(value),
        "job_id": job.job_id,
        "job_sha256": job.job_sha256,
        "attempt": 1,
        "attempt_dir": str(attempt_dir),
        "state": "succeeded",
        "started_utc": "2026-08-26T00:00:00Z",
        "finished_utc": "2026-08-26T00:00:01Z",
        "host": "test",
        "pid": child_pid,
        "pane": pane,
        "worker_process": worker_process,
        "child_process": child_process,
        "cpu_affinity": list(campaign.parse_cpu_list(job.cpus)),
        "interpreter_probe": json.loads(
            json.dumps(value["spec"]["interpreters"][job.interpreter_role])
        ),
        "argv": argv,
        "environment": benchmark_env(job.threads),
        "harness_environment": harness_environment,
        "exit_code": 0,
        "error": None,
        "signal_number": None,
        "stdout_log": f"jobs/{job.job_id}/attempts/0001/stdout.log",
        "stderr_log": f"jobs/{job.job_id}/attempts/0001/stderr.log",
        "payload": payload,
        "validation": {"accepted": True, "failures": []},
    }


def _runtime_identity(pid: int, *, ticks: int) -> campaign._campaign_worker.ProcessIdentity:
    return campaign._campaign_worker.ProcessIdentity(
        pid=pid,
        pgid=pid,
        process_start_ticks=ticks,
        boot_id="12345678-1234-1234-1234-123456789abc",
    )


def _runtime_launch(
    value: dict,
    worker_id: str,
    *,
    pane_id: str,
    pane_pid: int,
    launch_number: int,
    resume: bool,
) -> tuple[dict[str, object], campaign._campaign_worker.PaneIdentity]:
    digest = campaign.campaign_digest(value)
    job = next(job for job in campaign._jobs(value) if job.worker_id == worker_id)
    pane = campaign._campaign_worker.PaneIdentity(
        session=(
            f"gffbase-{digest[:12]}-cluster" + ("-canonical" if worker_id == "canonical" else "")
        ),
        window=worker_id,
        pane_id=pane_id,
        pane_pid=pane_pid,
        pane_dead=False,
    )
    launch = campaign._campaign_worker.build_launch_evidence(
        run_id=value["run_id"],
        campaign_sha256=digest,
        worker_id=worker_id,
        phase=job.phase,
        cpus=job.cpus,
        launch=launch_number,
        resume=resume,
        launched_utc=f"2026-08-31T12:00:{launch_number:02d}Z",
        pane=pane,
        worker_argv=campaign.build_worker_argv(value, worker_id, resume=resume),
        cwd=campaign.ROOT,
        first_window=True,
        tmux_path="/usr/bin/tmux",
        taskset_path="/usr/bin/taskset",
    )
    campaign._campaign_worker.persist_launch_evidence(
        Path(value["_path"]).parent,
        launch,
    )
    return launch, pane


def _runtime_success_result(
    value: dict,
    job: campaign.JobSpec,
    *,
    pane: campaign._campaign_worker.PaneIdentity,
    worker_identity: campaign._campaign_worker.ProcessIdentity,
    child_identity: campaign._campaign_worker.ProcessIdentity,
) -> dict:
    result = _worker_result(value, job)
    attempt_dir = Path(result["attempt_dir"])
    relative = attempt_dir.relative_to(Path(value["_path"]).parent).as_posix()
    result.update(
        {
            "host": "cluster.example",
            "pid": child_identity.pid,
            "pane": pane.to_dict(),
            "worker_process": worker_identity.to_dict(),
            "child_process": child_identity.to_dict(),
            "error": None,
            "signal_number": None,
            "stdout_log": f"{relative}/stdout.log",
            "stderr_log": f"{relative}/stderr.log",
        }
    )
    return result


def _write_runtime_running_status(
    value: dict,
    job: campaign.JobSpec,
    *,
    pane: campaign._campaign_worker.PaneIdentity,
    worker_identity: campaign._campaign_worker.ProcessIdentity,
    child_identity: campaign._campaign_worker.ProcessIdentity | None,
) -> dict[str, object]:
    run_dir = Path(value["_path"]).parent
    attempt_dir = run_dir / "jobs" / job.job_id / "attempts" / "0001"
    relative = attempt_dir.relative_to(run_dir).as_posix()
    running: dict[str, object] = {
        "schema_version": campaign.STATUS_SCHEMA,
        "run_id": value["run_id"],
        "campaign_sha256": campaign.campaign_digest(value),
        "job_id": job.job_id,
        "job_sha256": job.job_sha256,
        "worker_id": job.worker_id,
        "state": "running",
        "attempt": 1,
        "started_utc": "2026-08-31T12:01:00Z",
        "host": "cluster.example",
        "cpu_affinity": list(campaign.parse_cpu_list(job.cpus)),
        "pane": pane.to_dict(),
        "worker": worker_identity.to_dict(),
        "child": child_identity.to_dict() if child_identity is not None else None,
        "paths": {
            "attempt_dir": relative,
            "stdout_log": f"{relative}/stdout.log",
            "stderr_log": f"{relative}/stderr.log",
            "result": f"{relative}/result.json",
        },
    }
    pending = campaign._campaign_worker.read_status(
        run_dir,
        run_id=value["run_id"],
        campaign_sha256=campaign.campaign_digest(value),
        job=job,
    )
    return campaign._campaign_worker.write_status(
        run_dir,
        running,
        previous=pending,
        run_id=value["run_id"],
        campaign_sha256=campaign.campaign_digest(value),
        job=job,
    )


def test_help_has_only_five_public_commands_and_execute(capsys):
    parser = campaign.build_parser()
    with pytest.raises(SystemExit):
        parser.parse_args(["--help"])
    output = capsys.readouterr().out
    for command in ("preflight", "launch", "status", "canonical", "merge"):
        assert command in output
    assert "_worker" not in output
    for command in ("preflight", "launch", "canonical", "merge"):
        with pytest.raises(SystemExit):
            parser.parse_args([command, "--help"])
        assert "--execute" in capsys.readouterr().out


def test_preflight_is_dry_by_default(tmp_path, capsys, monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("dry preflight ran a subprocess")

    monkeypatch.setattr(campaign.subprocess, "run", forbidden)
    root = tmp_path / "out" / "cluster"
    code = campaign.main(
        [
            "preflight",
            "--run-id",
            "dry-run",
            "--campaign-root",
            str(root),
            "--candidate-wheel",
            str(tmp_path / "missing.whl"),
            "--primary-python",
            str(tmp_path / "missing-primary"),
            "--gffbase-010-python",
            str(tmp_path / "missing-010"),
            "--gffutils-013-python",
            str(tmp_path / "missing-013"),
        ]
    )
    assert code == 0
    assert not root.exists()
    preview = json.loads(capsys.readouterr().out)
    assert preview["mode"] == "dry-run"
    assert preview["request"]["run_id"] == "dry-run"
    assert preview["request"]["run_dir"] == str(root / "dry-run")
    assert preview["request"]["transform"] == {
        "mode": "prepare",
        "output": str(root / "dry-run" / "inputs" / "parent-stripped.gtf.gz"),
        "manifest": str(root / "dry-run" / "inputs" / "parent-stripped.manifest.json"),
    }
    assert preview["request_sha256"] == campaign.sha256_json(preview["request"])


def test_interpreter_probe_rejects_symlink_and_hardlink_aliases_before_execution(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    executable = tmp_path / "python3.11"
    executable.write_bytes(b"not actually invoked")
    executable.chmod(0o755)
    alias = tmp_path / "python"
    alias.symlink_to(executable)

    def forbidden(*args: object, **kwargs: object) -> None:
        raise AssertionError("unsafe interpreter reached subprocess execution")

    monkeypatch.setattr(campaign.subprocess, "run", forbidden)
    with pytest.raises(campaign.CampaignError, match="symlink"):
        campaign.probe_interpreter(alias, "primary")

    hardlink = tmp_path / "python-hardlink"
    os.link(executable, hardlink)
    with pytest.raises(campaign.CampaignError, match="uniquely linked"):
        campaign.probe_interpreter(executable, "primary")


def test_resource_stability_rejects_affinity_and_mount_drift() -> None:
    stable = {
        "platform": "Linux",
        "machine": "x86_64",
        "allowed_cpus": list(range(50)),
        "online_cpus": list(range(64)),
        "physical_cpu_ids": {str(index): [str(index), "0"] for index in range(50)},
        "numa_nodes": {"node0": list(range(64))},
        "mount": {"raw": "server:/ccb nfs4 /ccb rw", "fstype": "nfs4"},
        "executables": {
            "findmnt": "/usr/bin/findmnt",
            "taskset": "/usr/bin/taskset",
            "tmux": "/usr/bin/tmux",
        },
        "executable_versions": {"findmnt": "findmnt 1", "taskset": "taskset 1", "tmux": "tmux 3"},
        "executable_identities": {"findmnt": {}, "taskset": {}, "tmux": {}},
        "thresholds": {"free_bytes": 1, "available_ram_bytes": 1},
    }
    campaign._verify_resource_stability(stable, json.loads(json.dumps(stable)))

    for key, replacement in (
        ("allowed_cpus", list(range(49))),
        ("mount", {"raw": "other:/ccb nfs4 /ccb rw", "fstype": "nfs4"}),
    ):
        changed = json.loads(json.dumps(stable))
        changed[key] = replacement
        with pytest.raises(campaign.CampaignError, match="resource identity changed"):
            campaign._verify_resource_stability(stable, changed)


def test_tmux_session_probe_distinguishes_no_server_from_query_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(campaign.shutil, "which", lambda _name: "/usr/bin/tmux")
    monkeypatch.setattr(
        campaign.subprocess,
        "run",
        lambda *_a, **_k: campaign.subprocess.CompletedProcess([], 1, "", "no server running"),
    )
    assert campaign._tmux_sessions() == set()
    monkeypatch.setattr(
        campaign.subprocess,
        "run",
        lambda *_a, **_k: campaign.subprocess.CompletedProcess([], 2, "", "permission denied"),
    )
    with pytest.raises(campaign.CampaignError, match="cannot inspect"):
        campaign._tmux_sessions()


def test_executed_preflight_commits_final_marker_and_reentry_is_read_only(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "campaigns"
    wheel_path = tmp_path / "candidate.whl"
    interpreters = {
        "primary": tmp_path / "primary-python",
        "gffbase-0.1.0": tmp_path / "old-python",
        "gffutils-0.13": tmp_path / "gffutils-python",
    }
    output = tmp_path / "controls" / "parent-stripped.gtf.gz"
    manifest = tmp_path / "controls" / "parent-stripped.manifest.json"
    repo = {
        "root": str(campaign.ROOT),
        "commit": "1" * 40,
        "branch": "hardening/0.2.0-cluster",
        "dirty": False,
        "status": [],
        "dirty_submodules": [],
    }
    wheel = {
        "path": str(wheel_path),
        "name": wheel_path.name,
        "bytes": 10,
        "sha256": "2" * 64,
        "metadata_version": campaign.PUBLIC_VERSION,
    }
    resource = _unit_resources(root / "unit-run")
    qualified: list[Path] = []
    cheap_revalidations: list[str] = []

    monkeypatch.setattr(campaign, "_require_ignored_output", lambda _root: None)
    monkeypatch.setattr(campaign, "git_identity", lambda _root: dict(repo))
    monkeypatch.setattr(campaign, "wheel_identity", lambda _path: dict(wheel))

    def fake_interpreter(path: Path, role: str) -> dict:
        probe = _probe(role)
        probe["resolved_executable"] = str(path)
        probe["requested_executable"] = str(path)
        probe["prefix"] = str(path.parent)
        probe["sys_path"] = [str(path.parent)]
        probe["modules"]["stub"]["path"] = str(path.parent / "stub.so")
        if role == "primary":
            probe["packages"]["gffbase"] = campaign.PUBLIC_VERSION
            probe["direct_url"]["gffbase"] = json.dumps(
                {
                    "archive_info": {"hashes": {"sha256": wheel["sha256"]}},
                    "url": wheel_path.as_uri(),
                }
            )
        unsigned = dict(probe)
        unsigned.pop("probe_sha256")
        probe["probe_sha256"] = campaign.sha256_json(unsigned)
        return probe

    monkeypatch.setattr(campaign, "probe_interpreter", fake_interpreter)
    monkeypatch.setattr(campaign, "probe_resources", lambda _path: json.loads(json.dumps(resource)))
    monkeypatch.setattr(campaign, "verify_topology", lambda _resource: None)
    monkeypatch.setattr(
        campaign._campaign_preflight,
        "verify_input_file",
        lambda path, *, expected_size, expected_sha256: {
            "path": str(path),
            "bytes": expected_size,
            "sha256": expected_sha256,
            "gzip_crc_ok": True,
        },
    )
    monkeypatch.setattr(
        campaign,
        "verify_transform_manifest",
        lambda _manifest, _output, _entry: {
            "path": str(output),
            "bytes": 10,
            "sha256": "3" * 64,
            "gzip_crc_ok": True,
            "manifest_path": str(manifest),
            "manifest_sha256": "4" * 64,
            "transform_version": "1",
            "counts": {
                "input_feature_lines": 12,
                "output_feature_lines": 10,
                "comment_or_blank_lines": 0,
                "removed_gene_rows": 1,
                "removed_transcript_rows": 1,
            },
        },
    )

    def qualify(targets: dict[Path, tuple[Path, ...]]) -> list[dict[str, object]]:
        qualified.extend(targets)
        assert all(path.is_dir() for path in targets)
        return _unit_filesystems(next(iter(targets)))

    monkeypatch.setattr(campaign._campaign_preflight, "qualify_mutable_filesystems", qualify)
    monkeypatch.setattr(campaign._campaign_preflight, "validate_run_tree", lambda *_a, **_k: None)
    monkeypatch.setattr(campaign, "_tmux_sessions", lambda: set())
    monkeypatch.setattr(
        campaign,
        "_cheap_revalidate",
        lambda value: cheap_revalidations.append(str(value["run_id"])),
    )
    argv = [
        "preflight",
        "--run-id",
        "unit-run",
        "--campaign-root",
        str(root),
        "--candidate-wheel",
        str(wheel_path),
        "--primary-python",
        str(interpreters["primary"]),
        "--gffbase-010-python",
        str(interpreters["gffbase-0.1.0"]),
        "--gffutils-013-python",
        str(interpreters["gffutils-0.13"]),
        "--parent-stripped",
        str(output),
        "--parent-stripped-manifest",
        str(manifest),
        "--execute",
    ]

    assert campaign.main(argv) == 0
    run_dir = root / "unit-run"
    campaign_path = run_dir / "campaign.json"
    marker_path = run_dir / "preflight.json"
    assert qualified == [run_dir]
    loaded = campaign._load_campaign(campaign_path)
    assert loaded["run_id"] == "unit-run"
    before = {
        path: (path.read_bytes(), path.stat().st_ino) for path in (campaign_path, marker_path)
    }

    assert campaign.main(argv) == 0
    assert cheap_revalidations == ["unit-run"]
    assert {
        path: (path.read_bytes(), path.stat().st_ino) for path in (campaign_path, marker_path)
    } == before
    outputs = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    assert outputs[0]["jobs"] == 36
    assert outputs[1]["idempotent"] is True

    changed = list(argv)
    changed[changed.index(str(wheel_path))] = str(tmp_path / "different.whl")
    assert campaign.main(changed) == 2
    assert cheap_revalidations == ["unit-run"]
    assert "request differs" in capsys.readouterr().err


@pytest.mark.parametrize("value", ["../run", "bad/run", ".hidden", "UPPER", "a\nb"])
def test_identifiers_reject_traversal_and_control_characters(value):
    with pytest.raises(campaign.CampaignError):
        campaign.validate_identifier(value, "run id")


def test_atomic_create_is_immutable_and_durable(tmp_path):
    path = tmp_path / "artifact.json"
    campaign.atomic_create_json(path, {"schema_version": "x", "value": 1})
    campaign.atomic_create_json(path, {"schema_version": "x", "value": 1})
    with pytest.raises(campaign.CampaignError):
        campaign.atomic_create_json(path, {"schema_version": "x", "value": 2})
    assert json.loads(path.read_text())["value"] == 1


def test_matrix_is_exact_36_job_topology(tmp_path):
    jobs = campaign.build_job_matrix({}, {}, _inputs(tmp_path))
    assert (
        len(jobs)
        == len({job.job_id for job in jobs})
        == len({job.job_sha256 for job in jobs})
        == 36
    )
    assert sum(job.kind == "scaling" for job in jobs) == 25
    assert sum(job.kind == "primary" for job in jobs) == 5
    assert sum(job.kind == "control" for job in jobs) == 2
    assert sum(job.kind == "bridge" for job in jobs) == 4
    assert {(job.threads, job.cpus) for job in jobs if job.kind == "scaling"} == set(
        zip(campaign.THREADS, campaign.LANE_CPUS, strict=True)
    )
    assert all(job.primary_eligible == (job.kind == "primary") for job in jobs)
    gtf_primary = [job for job in jobs if job.kind == "primary" and job.corpus_key == "gencode-gtf"]
    assert [job.gtf_arm for job in gtf_primary] == ["no-infer"]
    assert {job.gtf_arm for job in jobs if job.kind == "control"} == {"default", "parent-stripped"}


def test_commands_and_environments_are_bounded_and_non_publishing(tmp_path):
    value = _make_campaign(tmp_path)
    jobs = campaign._jobs(value)
    scaling = next(job for job in jobs if job.job_id == "scaling-t04-mane")
    argv = campaign.build_mega_argv(scaling, tmp_path / "attempt", value)
    assert argv.count("--only") == 1
    assert "--skip-legacy" in argv
    assert not {"--publish", "--rederive", "--keep-db", "--no-purge"} & set(argv)
    assert argv[argv.index("--threads") + 1] == "4"

    bridge = next(job for job in jobs if job.job_id == "bridge-gffbase-0.1.0-mane")
    bridge_argv = campaign.build_bridge_argv(bridge, tmp_path / "attempt", value)
    assert bridge_argv[0] == value["spec"]["interpreters"]["gffbase-0.1.0"]["resolved_executable"]
    assert bridge_argv[bridge_argv.index("--engine") + 1] == "gffbase"

    env = campaign.worker_environment(value, scaling, tmp_path / "attempt")
    assert env["GFFBASE_THREADS"] == env["GFFUTILS2_THREADS"] == "4"
    for key in (
        "OMP_NUM_THREADS",
        "OPENBLAS_NUM_THREADS",
        "MKL_NUM_THREADS",
        "NUMEXPR_NUM_THREADS",
    ):
        assert env[key] == "1"
    assert "PYTHONPATH" not in env


def test_tmux_command_round_trips_without_shell_injection(tmp_path):
    value = _make_campaign(tmp_path)
    worker = campaign.build_worker_argv(value, "scale-t01")
    argv = campaign.build_tmux_argv("gffbase-abc-cluster", "0-9", worker, cwd=tmp_path)
    assert shlex.split(argv[-1])[3:] == worker
    with pytest.raises(campaign.CampaignError):
        campaign.build_tmux_argv("bad;touch-x", "0-9", worker)


def test_worker_command_plan_uses_one_session_exact_windows_and_pinned_tools(
    tmp_path: Path,
) -> None:
    value = _make_campaign(tmp_path)
    workers = [f"scale-t{threads:02d}" for threads in campaign.THREADS]
    commands = campaign._worker_commands(value, workers, resume=False)
    session = f"gffbase-{campaign.campaign_digest(value)[:12]}-cluster"

    assert len(commands) == 5
    for index, (worker_id, command) in enumerate(zip(workers, commands, strict=True)):
        assert command[0] == "/usr/bin/tmux"
        assert command[1] == ("new-session" if index == 0 else "new-window")
        assert command.index("-n") < len(command) - 1
        assert command[command.index("-n") + 1] == worker_id
        assert command[command.index("-F") + 1] == campaign._campaign_worker.TMUX_PANE_FORMAT
        if index == 0:
            assert command[command.index("-s") + 1] == session
        else:
            assert command[command.index("-t") + 1] == f"{session}:"
        shell = shlex.split(command[-1])
        assert shell[:3] == ["exec", "/usr/bin/taskset", "--cpu-list"]

    canonical = campaign._worker_commands(value, ["canonical"], resume=True)[0]
    assert canonical[1] == "new-session"
    assert canonical[canonical.index("-s") + 1] == f"{session}-canonical"
    assert canonical[canonical.index("-n") + 1] == "canonical"
    assert shlex.split(canonical[-1])[-1] == "--resume"


def test_executed_launch_captures_immutable_exact_pane_evidence(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    value = _make_campaign(tmp_path)
    monkeypatch.setattr(campaign, "_cheap_revalidate", lambda _campaign: None)
    monkeypatch.setattr(campaign, "_tmux_sessions", lambda: set())
    launched: list[list[str]] = []

    def fake_run(
        command: list[str],
        **_kwargs: object,
    ) -> subprocess.CompletedProcess[str]:
        launched.append(command)
        window = command[command.index("-n") + 1]
        if command[1] == "new-session":
            session = command[command.index("-s") + 1]
        else:
            session = command[command.index("-t") + 1].removesuffix(":")
        index = len(launched)
        return subprocess.CompletedProcess(
            command,
            0,
            stdout=f"{session}\t{window}\t%{index}\t{5000 + index}\t0\n",
            stderr="",
        )

    monkeypatch.setattr(campaign.subprocess, "run", fake_run)
    assert (
        campaign.main(
            [
                "launch",
                "--campaign",
                value["_path"],
                "--execute",
            ]
        )
        == 0
    )
    output = json.loads(capsys.readouterr().out)
    assert output["workers"] == [f"scale-t{threads:02d}" for threads in campaign.THREADS]
    assert len(launched) == 5

    run_dir = Path(value["_path"]).parent
    for index, worker_id in enumerate(output["workers"], start=1):
        records = campaign._campaign_worker.scan_launch_evidence(
            run_dir,
            run_id=value["run_id"],
            campaign_sha256=campaign.campaign_digest(value),
            worker_id=worker_id,
        )
        assert len(records) == 1
        assert records[0]["pane"]["pane_id"] == f"%{index}"
        assert records[0]["pane"]["pane_pid"] == 5000 + index
    reloaded = campaign._load_campaign(Path(value["_path"]))
    assert campaign.campaign_digest(reloaded) == campaign.campaign_digest(value)


def test_partial_tmux_launch_failure_cleans_only_captured_exact_panes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    value = _make_campaign(tmp_path)
    monkeypatch.setattr(campaign, "_cheap_revalidate", lambda _campaign: None)
    monkeypatch.setattr(campaign, "_tmux_sessions", lambda: set())
    calls: list[list[str]] = []

    def fake_run(
        command: list[str],
        **_kwargs: object,
    ) -> subprocess.CompletedProcess[str]:
        calls.append(command)
        if command[1] == "kill-pane":
            return subprocess.CompletedProcess(command, 0, stdout="", stderr="")
        launch_number = sum(call[1] != "kill-pane" for call in calls)
        if launch_number == 2:
            return subprocess.CompletedProcess(command, 1, stdout="", stderr="injected")
        window = command[command.index("-n") + 1]
        session = command[command.index("-s") + 1]
        return subprocess.CompletedProcess(
            command,
            0,
            stdout=f"{session}\t{window}\t%7\t5001\t0\n",
            stderr="",
        )

    monkeypatch.setattr(campaign.subprocess, "run", fake_run)
    assert campaign.main(["launch", "--campaign", value["_path"], "--execute"]) == 2
    assert calls[-1] == ["/usr/bin/tmux", "kill-pane", "-t", "%7"]
    assert all("kill-session" not in command for command in calls)
    assert "injected" in capsys.readouterr().err


def test_status_uses_exact_pane_and_process_identity_not_session_existence(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    value = _make_campaign(tmp_path)
    run_dir = Path(value["_path"]).parent
    digest = campaign.campaign_digest(value)
    worker_id = "scale-t01"
    pane = campaign._campaign_worker.PaneIdentity(
        session=f"gffbase-{digest[:12]}-cluster",
        window=worker_id,
        pane_id="%7",
        pane_pid=5001,
        pane_dead=False,
    )
    worker_argv = campaign.build_worker_argv(value, worker_id)
    launch = campaign._campaign_worker.build_launch_evidence(
        run_id=value["run_id"],
        campaign_sha256=digest,
        worker_id=worker_id,
        phase="exploratory",
        cpus="0-9",
        launch=1,
        resume=False,
        launched_utc="2026-08-31T12:00:00Z",
        pane=pane,
        worker_argv=worker_argv,
        cwd=campaign.ROOT,
        first_window=True,
        tmux_path="/usr/bin/tmux",
        taskset_path="/usr/bin/taskset",
    )
    campaign._campaign_worker.persist_launch_evidence(run_dir, launch)
    job = next(job for job in campaign._jobs(value) if job.worker_id == worker_id)
    attempt_dir = campaign._campaign_worker.allocate_attempt(run_dir, job)
    process_identity = campaign._campaign_worker.ProcessIdentity(
        pid=5001,
        pgid=5001,
        process_start_ticks=101,
        boot_id="12345678-1234-1234-1234-123456789abc",
    )
    relative_attempt = attempt_dir.relative_to(run_dir).as_posix()
    running = {
        "schema_version": campaign.STATUS_SCHEMA,
        "run_id": value["run_id"],
        "campaign_sha256": digest,
        "job_id": job.job_id,
        "job_sha256": job.job_sha256,
        "worker_id": worker_id,
        "state": "running",
        "attempt": 1,
        "started_utc": "2026-08-31T12:00:01Z",
        "host": "cluster.example",
        "cpu_affinity": list(range(10)),
        "pane": pane.to_dict(),
        "worker": process_identity.to_dict(),
        "child": None,
        "paths": {
            "attempt_dir": relative_attempt,
            "stdout_log": f"{relative_attempt}/stdout.log",
            "stderr_log": f"{relative_attempt}/stderr.log",
            "result": f"{relative_attempt}/result.json",
        },
    }
    pending = campaign._campaign_worker.read_status(
        run_dir,
        run_id=value["run_id"],
        campaign_sha256=digest,
        job=job,
    )
    campaign._campaign_worker.write_status(
        run_dir,
        running,
        previous=pending,
        run_id=value["run_id"],
        campaign_sha256=digest,
        job=job,
    )
    monkeypatch.setattr(campaign.socket, "gethostname", lambda: "cluster.example")
    monkeypatch.setattr(campaign, "_campaign_panes", lambda _campaign: (pane,), raising=False)
    monkeypatch.setattr(
        campaign._campaign_worker,
        "observe_process",
        lambda _pid: process_identity,
    )

    assert campaign.main(["status", "--campaign", value["_path"], "--json"]) == 0
    live = json.loads(capsys.readouterr().out)
    assert live["counts"]["running"] == 1

    reused = campaign._campaign_worker.ProcessIdentity(
        pid=5001,
        pgid=5001,
        process_start_ticks=102,
        boot_id=process_identity.boot_id,
    )
    monkeypatch.setattr(campaign._campaign_worker, "observe_process", lambda _pid: reused)
    assert campaign.main(["status", "--campaign", value["_path"], "--json"]) == 0
    stale = json.loads(capsys.readouterr().out)
    assert stale["counts"]["interrupted"] == 1
    assert stale["counts"]["running"] == 0


def test_run_job_terminalizes_interpreter_probe_failure_before_popen(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    value = _make_campaign(tmp_path)
    digest = campaign.campaign_digest(value)
    job = next(job for job in campaign._jobs(value) if job.job_id == "scaling-t01-mane")
    pane = campaign._campaign_worker.PaneIdentity(
        session=f"gffbase-{digest[:12]}-cluster",
        window=job.worker_id,
        pane_id="%7",
        pane_pid=5001,
        pane_dead=False,
    )
    launch = campaign._campaign_worker.build_launch_evidence(
        run_id=value["run_id"],
        campaign_sha256=digest,
        worker_id=job.worker_id,
        phase="exploratory",
        cpus=job.cpus,
        launch=1,
        resume=False,
        launched_utc="2026-08-31T12:00:00Z",
        pane=pane,
        worker_argv=campaign.build_worker_argv(value, job.worker_id),
        cwd=campaign.ROOT,
        first_window=True,
        tmux_path="/usr/bin/tmux",
        taskset_path="/usr/bin/taskset",
    )
    identity = campaign._campaign_worker.ProcessIdentity(
        pid=5001,
        pgid=5001,
        process_start_ticks=101,
        boot_id="12345678-1234-1234-1234-123456789abc",
    )

    def fail_probe(*_args: object, **_kwargs: object) -> dict[str, object]:
        raise campaign.CampaignError("injected interpreter probe failure")

    monkeypatch.setattr(campaign, "probe_interpreter", fail_probe)
    result = campaign.run_job(
        value,
        job,
        1,
        launch=launch,
        worker_identity=identity,
    )
    assert result["state"] == "failed"
    assert "interpreter probe" in " ".join(result["validation"]["failures"])
    run_dir = Path(value["_path"]).parent
    status = campaign._campaign_worker.read_status(
        run_dir,
        run_id=value["run_id"],
        campaign_sha256=digest,
        job=job,
    )
    assert status["state"] == "failed"
    assert status["result_sha256"] == sha256_file(run_dir / status["paths"]["result"])


def test_run_job_terminalizes_affinity_probe_failure_without_reprobing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    value = _make_campaign(tmp_path)
    job = next(job for job in campaign._jobs(value) if job.job_id == "scaling-t01-mane")
    launch, pane = _runtime_launch(
        value,
        job.worker_id,
        pane_id="%7",
        pane_pid=5001,
        launch_number=1,
        resume=False,
    )
    identity = _runtime_identity(5001, ticks=101)
    affinity_calls = 0

    monkeypatch.setattr(
        campaign,
        "probe_interpreter",
        lambda *_args, **_kwargs: value["spec"]["interpreters"][job.interpreter_role],
    )

    def fail_affinity(_pid: int) -> set[int]:
        nonlocal affinity_calls
        affinity_calls += 1
        raise OSError("injected affinity probe failure")

    monkeypatch.setattr(campaign.os, "sched_getaffinity", fail_affinity)
    result = campaign.run_job(
        value,
        job,
        1,
        launch=launch,
        worker_identity=identity,
    )

    assert result["state"] == "failed"
    assert result["cpu_affinity"] is None
    assert affinity_calls == 1
    status = campaign.read_job_state(value, job)
    assert status["state"] == "failed"
    assert status["cpu_affinity"] == list(campaign.parse_cpu_list(job.cpus))
    assert status["pane"] == pane.to_dict()


def test_worker_binds_itself_to_exact_launch_pane_and_process_identity(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    value = _make_campaign(tmp_path)
    launch, pane = _runtime_launch(
        value,
        "scale-t01",
        pane_id="%7",
        pane_pid=5001,
        launch_number=1,
        resume=False,
    )
    identity = _runtime_identity(5001, ticks=101)
    calls: list[list[str]] = []

    def fake_run(command: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
        calls.append(command)
        return subprocess.CompletedProcess(
            command,
            0,
            stdout=(f"{pane.session}\t{pane.window}\t{pane.pane_id}\t{pane.pane_pid}\t0\n"),
            stderr="",
        )

    monkeypatch.setenv("TMUX_PANE", pane.pane_id)
    monkeypatch.setattr(campaign.os, "getpid", lambda: pane.pane_pid)
    monkeypatch.setattr(campaign._campaign_worker, "observe_process", lambda _pid: identity)
    monkeypatch.setattr(campaign.subprocess, "run", fake_run)

    bound_launch, bound_identity, panes = campaign._bind_current_worker(
        value,
        "scale-t01",
    )
    assert bound_launch == launch
    assert bound_identity == identity
    assert panes == (pane,)
    assert calls == [
        campaign._campaign_worker.build_tmux_list_panes_argv(
            pane.session,
            tmux_path="/usr/bin/tmux",
        )
    ]

    wrong_pid = _runtime_identity(5002, ticks=102)
    monkeypatch.setattr(campaign._campaign_worker, "observe_process", lambda _pid: wrong_pid)
    with pytest.raises(campaign.CampaignError, match="process identity|PID"):
        campaign._bind_current_worker(value, "scale-t01")


def test_worker_campaign_load_retries_transient_parallel_runtime_mutation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    value = _make_campaign(tmp_path)
    calls = 0
    sleeps: list[float] = []

    def transient_loader(_path: Path) -> dict:
        nonlocal calls
        calls += 1
        if calls < 3:
            raise campaign.CampaignError("campaign runtime tree changed during exact scan")
        return value

    ticks = iter([0.0, 0.1, 0.2])
    monkeypatch.setattr(campaign, "_load_campaign", transient_loader)

    loaded = campaign._load_worker_campaign(
        Path(value["_path"]),
        timeout_seconds=1.0,
        monotonic=ticks.__next__,
        sleeper=sleeps.append,
    )
    assert loaded == value
    assert calls == 3
    assert sleeps == [0.1, 0.1]


def test_worker_promotes_crash_after_immutable_success_without_remeasurement(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    value = _make_campaign(tmp_path)
    job = next(job for job in campaign._jobs(value) if job.job_id == "scaling-t01-mane")
    launch, pane = _runtime_launch(
        value,
        job.worker_id,
        pane_id="%7",
        pane_pid=5001,
        launch_number=1,
        resume=True,
    )
    worker_identity = _runtime_identity(5001, ticks=101)
    child_identity = _runtime_identity(6001, ticks=102)
    run_dir = Path(value["_path"]).parent
    attempt_dir = campaign._campaign_worker.allocate_attempt(run_dir, job)
    running = _write_runtime_running_status(
        value,
        job,
        pane=pane,
        worker_identity=worker_identity,
        child_identity=child_identity,
    )
    result = _runtime_success_result(
        value,
        job,
        pane=pane,
        worker_identity=worker_identity,
        child_identity=child_identity,
    )
    result["started_utc"] = running["started_utc"]
    result["finished_utc"] = "2026-08-31T12:02:00Z"
    campaign.atomic_create_json(attempt_dir / "result.json", result)

    monkeypatch.setattr(campaign.socket, "gethostname", lambda: "cluster.example")
    monkeypatch.setattr(campaign, "_jobs", lambda _campaign: (job,))
    monkeypatch.setattr(
        campaign,
        "_bind_current_worker",
        lambda _campaign, _worker_id: (launch, worker_identity, (pane,)),
    )
    monkeypatch.setattr(
        campaign,
        "run_job",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("remeasured")),
    )
    args = SimpleNamespace(
        campaign=Path(value["_path"]),
        campaign_sha256=campaign.campaign_digest(value),
        worker_id=job.worker_id,
        resume=True,
    )

    assert campaign.cmd_worker(args) == 0
    promoted = campaign.read_job_state(value, job)
    assert promoted["state"] == "succeeded"
    assert promoted["attempt"] == 1
    assert promoted["result_sha256"] == sha256_file(attempt_dir / "result.json")
    accepted = campaign._accepted_results(value)
    assert len(accepted) == 1
    assert accepted[0]["job_id"] == job.job_id


def test_worker_terminalizes_stale_incomplete_attempt_before_retry(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    value = _make_campaign(tmp_path)
    job = next(job for job in campaign._jobs(value) if job.job_id == "scaling-t01-mane")
    _old_launch, old_pane = _runtime_launch(
        value,
        job.worker_id,
        pane_id="%7",
        pane_pid=5001,
        launch_number=1,
        resume=False,
    )
    new_launch, new_pane = _runtime_launch(
        value,
        job.worker_id,
        pane_id="%8",
        pane_pid=5002,
        launch_number=2,
        resume=True,
    )
    old_identity = _runtime_identity(5001, ticks=101)
    current_identity = _runtime_identity(5002, ticks=201)
    run_dir = Path(value["_path"]).parent
    attempt_dir = campaign._campaign_worker.allocate_attempt(run_dir, job)
    _write_runtime_running_status(
        value,
        job,
        pane=old_pane,
        worker_identity=old_identity,
        child_identity=None,
    )

    observed: list[tuple[int, str]] = []

    def fake_run_job(
        loaded: dict,
        selected: campaign.JobSpec,
        attempt: int,
        *,
        launch: dict[str, object],
        worker_identity: campaign._campaign_worker.ProcessIdentity,
    ) -> dict[str, object]:
        status = campaign.read_job_state(loaded, selected)
        observed.append((attempt, str(status["state"])))
        assert launch == new_launch
        assert worker_identity == current_identity
        assert status["result_sha256"] == sha256_file(attempt_dir / "result.json")
        return {"state": "succeeded"}

    monkeypatch.setattr(campaign.socket, "gethostname", lambda: "cluster.example")
    monkeypatch.setattr(campaign, "_jobs", lambda _campaign: (job,))
    monkeypatch.setattr(
        campaign,
        "_bind_current_worker",
        lambda _campaign, _worker_id: (new_launch, current_identity, (new_pane,)),
    )
    monkeypatch.setattr(campaign, "run_job", fake_run_job)
    args = SimpleNamespace(
        campaign=Path(value["_path"]),
        campaign_sha256=campaign.campaign_digest(value),
        worker_id=job.worker_id,
        resume=True,
    )

    assert campaign.cmd_worker(args) == 0
    assert observed == [(2, "interrupted")]
    recovered = json.loads((attempt_dir / "result.json").read_text(encoding="utf-8"))
    assert recovered["state"] == "interrupted"
    assert recovered["attempt"] == 1
    assert recovered["validation"]["accepted"] is False


def test_worker_validation_rejects_error_fast_validation_and_no_rtree(tmp_path):
    value = _make_campaign(tmp_path)
    primary = next(job for job in campaign._jobs(value) if job.job_id == "canonical-mane")
    result = _worker_result(value, primary)
    campaign.validate_worker_result(value, primary, result)

    broken = json.loads(json.dumps(result))
    broken["payload"]["error"] = "caught by harness"
    with pytest.raises(campaign.CampaignError):
        campaign.validate_worker_result(value, primary, broken)

    broken = json.loads(json.dumps(result))
    broken["payload"]["gffbase"]["validation"]["level"] = "fast"
    with pytest.raises(campaign.CampaignError):
        campaign.validate_worker_result(value, primary, broken)

    broken = json.loads(json.dumps(result))
    broken["payload"]["gffbase"]["rtree_built"] = False
    with pytest.raises(campaign.CampaignError):
        campaign.validate_worker_result(value, primary, broken)


def test_timeout_retains_wall_bound_but_never_speedup_floor(tmp_path):
    value = _make_campaign(tmp_path)
    primary = next(job for job in campaign._jobs(value) if job.job_id == "canonical-mane")
    result = _worker_result(value, primary)
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
            "benchmark_env": benchmark_env(primary.threads),
            "cap_seconds": primary.parameters["legacy_timeout"],
            "wall_seconds": None,
            "disk_bytes": 0,
            "stdout_bytes": 0,
            "stdout_sha256": hashlib.sha256(b"").hexdigest(),
            "stdout_parse_error": "invalid final JSON object",
        }
    )
    result["payload"]["ingest_speedup"] = None
    campaign.validate_worker_result(value, primary, result)
    comparison = campaign._strict_ratio(result["payload"]["gffbase"], legacy)
    assert comparison["ratio"] is None
    assert comparison["signature_match"] is None
    assert comparison["state"] == "censored"


def test_strict_ratio_requires_versioned_exact_signature():
    left = {
        "state": "completed",
        "exit_code": 0,
        "wall_seconds": 10.0,
        "n_features": 10,
        "correctness_signature": _signature("a"),
    }
    right = {
        "state": "completed",
        "exit_code": 0,
        "wall_seconds": 20.0,
        "n_features": 10,
        "correctness_signature": _signature("a"),
    }
    assert campaign._strict_ratio(left, right)["ratio"] == pytest.approx(2.0)
    right["correctness_signature"] = _signature("b")
    assert campaign._strict_ratio(left, right)["ratio"] is None
    del right["correctness_signature"]
    assert campaign._strict_ratio(left, right)["ratio"] is None


def test_worker_result_schema_rejects_unknown_top_level_keys(tmp_path):
    value = _make_campaign(tmp_path)
    job = campaign._jobs(value)[0]
    result = _worker_result(value, job)
    result["stale_v1_field"] = "must-not-be-silently-accepted"

    with pytest.raises(campaign.CampaignError, match="unknown keys"):
        campaign.validate_worker_result(value, job, result)


def test_merge_has_five_primary_and_separate_controls_bridges_scaling(tmp_path):
    value = _make_campaign(tmp_path)
    results = [_worker_result(value, job) for job in campaign._jobs(value)]
    merged = campaign.merge_campaign(value, results)
    assert merged["schema_version"] == campaign.RESULTS_SCHEMA
    assert set(merged["results"]["primary"]) == set(campaign.CORPUS_ORDER)
    assert len(merged["results"]["controls"]) == 2
    assert len(merged["results"]["bridges"]) == 4
    assert len(merged["results"]["scaling"]) == 25
    assert all(
        value["ratio"] == pytest.approx(2.0) for value in merged["comparisons"]["primary"].values()
    )
    assert merged["gates"]["publishable"] is True


def test_merge_rejects_missing_and_duplicate_results(tmp_path):
    value = _make_campaign(tmp_path)
    results = [_worker_result(value, job) for job in campaign._jobs(value)]
    with pytest.raises(campaign.CampaignError, match="exactly 36"):
        campaign.merge_campaign(value, results[:-1])
    with pytest.raises(campaign.CampaignError, match="duplicate"):
        campaign.merge_campaign(value, [*results[:-1], results[0]])


def test_publisher_contract_preserves_other_platforms_and_is_idempotent(tmp_path):
    campaign_root = tmp_path / "campaign"
    campaign_root.mkdir()
    value = _make_campaign(campaign_root)
    merged = campaign.merge_campaign(
        value, [_worker_result(value, job) for job in campaign._jobs(value)]
    )
    publish_root = tmp_path / "results"
    publish_root.mkdir()
    historical = campaign.ROOT / "benchmarks" / "results" / "06_mega.json"
    (publish_root / "06_mega.json").write_bytes(historical.read_bytes())
    target = campaign.publish_campaign(merged, publish_root)
    campaign.publish_campaign(merged, publish_root)
    published = json.loads(target.read_text())
    assert published["artifact_kind"] == "portable"
    assert str(tmp_path) not in json.dumps(published)
    index = json.loads((publish_root / "index.json").read_text())
    assert index["schema_version"] == campaign.INDEX_SCHEMA
    mac = index["platforms"]["macos-arm64"]
    assert mac["kind"] == "legacy-opaque"
    assert mac["runs"][mac["canonical_run_id"]]["sha256"] == (
        campaign._campaign_results.HISTORICAL_MAC_SHA256
    )
    linux = index["platforms"]["linux-x86_64"]
    assert linux["canonical_run_id"] == value["run_id"]
    entry = linux["runs"][value["run_id"]]
    assert entry["artifact"] == f"platform/linux-x86_64/{value['run_id']}/campaign-results.json"
    assert entry["sha256"] == sha256_file(target)


def test_internal_worker_rejects_wrong_campaign_digest_before_mutation(tmp_path):
    value = _make_campaign(tmp_path)
    owner = Path(value["_path"]).parent / "workers" / "scale-t01.owner"
    code = campaign.main(
        [
            "_worker",
            "--campaign",
            value["_path"],
            "--worker-id",
            "scale-t01",
            "--campaign-sha256",
            "0" * 64,
        ]
    )
    assert code == 2
    assert not owner.exists()


# ---------------------------------------------------------------------------
# The campaign root: probe the capability, do not infer it from the fstype
# ---------------------------------------------------------------------------
#
# `verify_topology` used to require the campaign root be NFS-backed, while
# `safe_io` publishes every result through `renameat2(RENAME_NOREPLACE)` --
# which NFS answers with EINVAL. The two requirements were mutually exclusive,
# so the campaign could not run on any filesystem:
#
#     on NFS   -> "filesystem does not support atomic no-replace rename"
#     on xfs   -> "campaign root must be NFS-backed, found xfs"
#
# It survived 511 tests because they feed a STUBBED mount probe claiming
# `fstype: nfs4` while exercising the rename primitive on a local `tmp_path`.
# No test could have caught it, because no test put the two together.
#
# What matters is not the name of the filesystem but whether it supports the
# primitive the design depends on, so that is what is checked now.


def _topology(**overrides):
    probe = {
        "platform": "Linux",
        "machine": "x86_64",
        "allowed_cpus": list(range(50)),
        "online_cpus": list(range(64)),
        "physical_cpu_ids": {str(index): [str(index), "0"] for index in range(50)},
        "numa_nodes": {"node0": list(range(64))},
        "free_bytes": campaign.MIN_FREE_BYTES,
        "available_ram_bytes": campaign.MIN_AVAILABLE_RAM_BYTES,
        "mount": {
            "raw": "/dev/nvme1n1 xfs /srv/nvme1 rw",
            "fstype": "xfs",
            "probe_path": "/srv/nvme1",
            "supports_noreplace_rename": True,
        },
        "executables": {
            "findmnt": "/usr/bin/findmnt",
            "taskset": "/usr/bin/taskset",
            "tmux": "/usr/bin/tmux",
        },
        "executable_versions": {"findmnt": "findmnt 1", "taskset": "taskset 1", "tmux": "tmux 3"},
        "executable_identities": {"findmnt": {}, "taskset": {}, "tmux": {}},
        "thresholds": {
            "free_bytes": campaign.MIN_FREE_BYTES,
            "available_ram_bytes": campaign.MIN_AVAILABLE_RAM_BYTES,
        },
    }
    probe.update(overrides)
    return probe


def test_a_local_root_that_supports_the_primitive_is_accepted() -> None:
    """xfs on local NVMe is the only thing on this cluster that can host a
    campaign: it supports the rename AND has the 80 GiB the run needs."""
    campaign.verify_topology(_topology())


def test_a_root_that_cannot_do_a_no_replace_rename_is_rejected() -> None:
    """The failure the fstype check was reaching for, stated directly."""
    probe = _topology()
    probe["mount"] = dict(probe["mount"], fstype="nfs4", supports_noreplace_rename=False)
    with pytest.raises(campaign.CampaignError, match="no-replace rename"):
        campaign.verify_topology(probe)


def test_an_unprobed_root_is_rejected_rather_than_assumed_good() -> None:
    """A probe from an older campaign has no such key. Treating a missing
    capability as present is how a fail-closed check becomes decorative."""
    probe = _topology()
    probe["mount"] = {k: v for k, v in probe["mount"].items() if k != "supports_noreplace_rename"}
    with pytest.raises(campaign.CampaignError, match="no-replace rename"):
        campaign.verify_topology(probe)
