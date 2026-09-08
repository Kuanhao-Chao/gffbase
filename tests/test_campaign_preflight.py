"""Transactional request, marker, and loader contracts for campaign preflight."""

from __future__ import annotations

import copy
import gzip
import hashlib
import os
import stat
import zipfile
from pathlib import Path

import pytest

from benchmarks import cluster_campaign as facade
from benchmarks.campaign import model, preflight, safe_io


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


def _interpreter_snapshot(path: Path, role: str) -> dict[str, object]:
    package_names = ("gffbase", "gffutils", "duckdb", "pyarrow", "psutil", "numpy")
    prefix = path.parent.parent
    unsigned: dict[str, object] = {
        "resolved_executable": str(path),
        "requested_executable": str(path),
        "executable_identity": {
            "st_dev": 1,
            "st_ino": 1,
            "st_size": 1,
            "st_mtime_ns": 1,
            "st_ctime_ns": 1,
        },
        "prefix": str(prefix),
        "implementation": "CPython",
        "python_version": "3.11.0",
        "isolated": 1,
        "sys_path": [str(prefix)],
        "packages": {name: None for name in package_names},
        "modules": {
            "stub": {
                "path": str(prefix / "stub.so"),
                "version": None,
                "sha256": "7" * 64,
            }
        },
        "distribution_roots": {},
        "direct_url": {name: None for name in package_names},
        "pth_files": [],
        "role": role,
    }
    return {**unsigned, "probe_sha256": model.sha256_json(unsigned)}


def _filesystem_evidence(run_dir: Path) -> list[dict[str, object]]:
    checked = [run_dir / "campaign.json", run_dir / "preflight.json"]
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
                "probe_path": str(run_dir / preflight.PROBE_ROOT_NAME / f"probe-{'f' * 32}"),
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
            "path_budget": preflight.validate_encoded_path_budget(
                checked,
                name_max=255,
                path_max=4096,
            ),
        }
    ]


def _resources(run_dir: Path) -> dict[str, object]:
    executable_identity = {
        "st_dev": 1,
        "st_ino": 1,
        "st_size": 1,
        "st_mtime_ns": 1,
        "sha256": "8" * 64,
    }
    return {
        "platform": "Linux",
        "machine": "x86_64",
        "allowed_cpus": list(range(50)),
        "online_cpus": list(range(50)),
        "physical_cpu_ids": {str(index): [str(index), "0"] for index in range(50)},
        "numa_nodes": {"node0": list(range(50))},
        "free_bytes": 100,
        "available_ram_bytes": 100,
        "mount": {
            "raw": "unit",
            "fstype": "unitfs",
            "probe_path": str(run_dir),
            "supports_noreplace_rename": True,
        },
        "executables": {name: f"/usr/bin/{name}" for name in ("findmnt", "taskset", "tmux")},
        "executable_versions": {name: "unit 1" for name in ("findmnt", "taskset", "tmux")},
        "executable_identities": {
            name: dict(executable_identity) for name in ("findmnt", "taskset", "tmux")
        },
        "thresholds": {
            "free_bytes": 1,
            "available_ram_bytes": 1,
            "disk_formula": "unit",
            "ram_formula": "unit",
        },
    }


def _campaign(run_dir: Path) -> dict[str, object]:
    interpreters = {
        role: _interpreter_snapshot(run_dir.parent / "envs" / role / "python", role)
        for role in model.INTERPRETER_ROLES
    }
    inputs = _inputs(run_dir / "data")
    inputs["gencode-gtf-parent-stripped"]["path"] = str(
        run_dir.parent / "controls" / "parent-stripped.gtf.gz"
    )
    inputs["gencode-gtf-parent-stripped"]["manifest_path"] = str(
        run_dir.parent / "controls" / "parent-stripped.json"
    )
    parameters = model.binding_parameters()
    jobs = model.build_job_matrix(parameters, interpreters, inputs)
    filesystems = _filesystem_evidence(run_dir)
    resources = _resources(run_dir)
    spec = {
        "repo": {
            "root": "/repo",
            "commit": "a" * 40,
            "branch": "hardening",
            "dirty": False,
            "status": [],
            "dirty_submodules": [],
        },
        "candidate": {
            "wheel": {
                "path": str(run_dir.parent / "candidate.whl"),
                "name": "candidate.whl",
                "bytes": 1,
                "sha256": "b" * 64,
                "metadata_version": model.PUBLIC_VERSION,
            }
        },
        "interpreters": interpreters,
        "inputs": inputs,
        "topology": model.topology(),
        "parameters": parameters,
        "resources": {**resources, "filesystems": filesystems},
        "jobs": [job.to_dict() for job in jobs],
    }
    return {
        "schema_version": model.CAMPAIGN_SCHEMA,
        "run_id": "unit-run",
        "created_utc": "2026-08-31T12:00:00Z",
        "spec_sha256": model.sha256_json(spec),
        "spec": spec,
    }


def _request(run_dir: Path) -> dict[str, object]:
    return preflight.normalize_request(
        run_id="unit-run",
        campaign_root=run_dir.parent,
        candidate_wheel=run_dir.parent / "candidate.whl",
        interpreters={
            role: run_dir.parent / "envs" / role / "python" for role in model.INTERPRETER_ROLES
        },
        transform_mode="external",
        parent_stripped=run_dir.parent / "controls" / "parent-stripped.gtf.gz",
        parent_stripped_manifest=run_dir.parent / "controls" / "parent-stripped.json",
        parameters=model.binding_parameters(),
    )


def _evidence(campaign: dict[str, object]) -> dict[str, object]:
    spec = campaign["spec"]
    assert isinstance(spec, dict)
    candidate = spec["candidate"]
    assert isinstance(candidate, dict)
    resources = dict(spec["resources"])
    filesystems = resources.pop("filesystems")
    interpreters = spec["interpreters"]
    assert isinstance(interpreters, dict)
    inputs = spec["inputs"]
    assert isinstance(inputs, dict)
    jobs = spec["jobs"]
    session = f"gffbase-{campaign['spec_sha256'][:12]}-cluster"  # type: ignore[index]
    return {
        "repo_before": spec["repo"],
        "repo_after": copy.deepcopy(spec["repo"]),
        "candidate": {"before": candidate["wheel"], "after": copy.deepcopy(candidate["wheel"])},
        "interpreters": {
            role: {"before": interpreters[role], "after": copy.deepcopy(interpreters[role])}
            for role in model.INTERPRETER_ROLES
        },
        "inputs": {"before": inputs, "after": copy.deepcopy(inputs)},
        "transform": {
            "before": inputs["gencode-gtf-parent-stripped"],
            "after": copy.deepcopy(inputs["gencode-gtf-parent-stripped"]),
        },
        "resources": {"before": resources, "after": copy.deepcopy(resources)},
        "filesystems": filesystems,
        "topology": model.topology(),
        "matrix": {"job_count": 36, "jobs_sha256": model.sha256_json(jobs)},
        "session": {"names": [session, f"{session}-canonical"], "before": [], "after": []},
    }


def _preflight_document(run_dir: Path) -> tuple[dict[str, object], dict[str, object]]:
    if run_dir.is_dir():
        run_dir.chmod(0o700)
        if not (run_dir / preflight.PROBE_ROOT_NAME).exists():
            preflight.probe_filesystem_capabilities(run_dir, token_provider=lambda: "f" * 32)
    campaign = _campaign(run_dir)
    request = _request(run_dir)
    document = preflight.build_preflight_document(
        request=request,
        campaign=campaign,
        evidence=_evidence(campaign),
        verified_utc="2026-08-31T12:01:00Z",
    )
    return campaign, document


def test_normalized_request_is_closed_canonical_and_full(tmp_path: Path) -> None:
    run_dir = tmp_path / "campaigns" / "unit-run"
    request = _request(run_dir)

    assert request == preflight.validate_request(request)
    assert request["run_id"] == "unit-run"
    assert request["campaign_root"] == str(run_dir.parent)
    assert request["run_dir"] == str(run_dir)
    assert request["parameters"] == model.binding_parameters()
    assert request["transform"] == {
        "mode": "external",
        "output": str(tmp_path / "campaigns" / "controls" / "parent-stripped.gtf.gz"),
        "manifest": str(tmp_path / "campaigns" / "controls" / "parent-stripped.json"),
    }

    for mutation in (
        lambda value: value.update({"unknown": True}),
        lambda value: value.__setitem__("run_dir", str(run_dir.parent / "other")),
        lambda value: value.__setitem__("parameters", {**model.binding_parameters(), "repeats": 1}),
        lambda value: value["transform"].__setitem__("mode", "guess"),  # type: ignore[union-attr]
    ):
        forged = copy.deepcopy(request)
        mutation(forged)
        with pytest.raises(model.CampaignError):
            preflight.validate_request(forged)


def test_preflight_document_is_closed_and_cross_digest_bound(tmp_path: Path) -> None:
    run_dir = tmp_path / "campaigns" / "unit-run"
    campaign, document = _preflight_document(run_dir)

    assert preflight.validate_preflight_document(document) == document
    assert document["request_sha256"] == model.sha256_json(document["request"])
    assert document["campaign"] == {
        "path": str(run_dir / "campaign.json"),
        "file_sha256": hashlib.sha256(safe_io.canonical_storage_bytes(campaign)).hexdigest(),
        "spec_sha256": campaign["spec_sha256"],
    }
    assert all(document["gates"].values())  # type: ignore[union-attr]

    for mutate in (
        lambda value: value.update({"unknown": None}),
        lambda value: value.__setitem__("request_sha256", "0" * 64),
        lambda value: value["campaign"].__setitem__("path", "/wrong/campaign.json"),  # type: ignore[union-attr]
        lambda value: value["gates"].__setitem__("matrix", 1),  # type: ignore[union-attr]
        lambda value: value["evidence"]["candidate"]["before"].update(  # type: ignore[index]
            {"unknown": True}
        ),
        lambda value: value["evidence"]["filesystems"][0].update(  # type: ignore[index]
            {"unknown": True}
        ),
    ):
        forged = copy.deepcopy(document)
        mutate(forged)
        with pytest.raises(model.CampaignError):
            preflight.validate_preflight_document(forged)


def test_bundle_crosslinks_reject_valid_looking_evidence_from_another_candidate(
    tmp_path: Path,
) -> None:
    run_dir = tmp_path / "campaigns" / "unit-run"
    run_dir.mkdir(parents=True)
    campaign, document = _preflight_document(run_dir)
    forged = copy.deepcopy(document)
    forged["evidence"]["candidate"]["before"]["sha256"] = "c" * 64  # type: ignore[index]
    forged["evidence"]["candidate"]["after"]["sha256"] = "c" * 64  # type: ignore[index]
    assert preflight.validate_preflight_document(forged) == forged

    with pytest.raises(model.CampaignError, match="candidate evidence"):
        preflight.commit_campaign_bundle(run_dir, campaign, forged)


def test_preflight_marker_is_last_and_campaign_only_is_not_loadable(tmp_path: Path) -> None:
    run_dir = tmp_path / "campaigns" / "unit-run"
    run_dir.mkdir(parents=True)
    campaign, document = _preflight_document(run_dir)

    def fail_after_campaign(stage: str) -> None:
        if stage == "after_campaign":
            raise RuntimeError("injected between immutable artifacts")

    with pytest.raises(RuntimeError, match="injected"):
        preflight.commit_campaign_bundle(
            run_dir,
            campaign,
            document,
            fault_hook=fail_after_campaign,
        )
    assert (run_dir / "campaign.json").is_file()
    assert not (run_dir / "preflight.json").exists()
    with pytest.raises(model.CampaignError, match="preflight|incomplete|marker"):
        preflight.load_completed_campaign(run_dir / "campaign.json")

    preflight.commit_campaign_bundle(run_dir, campaign, document)
    loaded = preflight.load_completed_campaign(run_dir / "campaign.json")
    assert loaded["run_id"] == "unit-run"
    assert loaded["_path"] == str(run_dir / "campaign.json")


def test_loader_rejects_preflight_only_and_cross_digest_forgery(tmp_path: Path) -> None:
    run_dir = tmp_path / "campaigns" / "unit-run"
    run_dir.mkdir(parents=True)
    campaign, document = _preflight_document(run_dir)
    safe_io.atomic_create_json(run_dir / "preflight.json", document)
    with pytest.raises(model.CampaignError, match="campaign|incomplete"):
        preflight.load_completed_campaign(run_dir / "campaign.json")

    safe_io.atomic_create_json(run_dir / "campaign.json", campaign)
    forged = copy.deepcopy(document)
    forged["campaign"]["file_sha256"] = "0" * 64  # type: ignore[index]
    other = tmp_path / "forged" / "unit-run"
    other.mkdir(parents=True)
    safe_io.atomic_create_json(other / "campaign.json", campaign)
    forged["request"]["campaign_root"] = str(other.parent)  # type: ignore[index]
    forged["request"]["run_dir"] = str(other)  # type: ignore[index]
    forged["request_sha256"] = model.sha256_json(forged["request"])
    forged["campaign"]["path"] = str(other / "campaign.json")  # type: ignore[index]
    safe_io.atomic_create_json(other / "preflight.json", forged)
    with pytest.raises(model.CampaignError, match="digest|campaign"):
        preflight.load_completed_campaign(other / "campaign.json")


def test_legacy_facade_loader_requires_the_same_final_marker(tmp_path: Path) -> None:
    run_dir = tmp_path / "campaigns" / "unit-run"
    run_dir.mkdir(parents=True)
    campaign, document = _preflight_document(run_dir)
    safe_io.atomic_create_json(run_dir / "campaign.json", campaign)
    with pytest.raises(model.CampaignError, match="preflight|incomplete|marker"):
        facade._load_campaign(run_dir / "campaign.json")

    safe_io.atomic_create_json(run_dir / "preflight.json", document)
    loaded = facade._load_campaign(run_dir / "campaign.json")
    assert loaded["_path"] == str(run_dir / "campaign.json")


def test_completed_request_reentry_is_exact_and_does_not_rewrite_artifacts(
    tmp_path: Path,
) -> None:
    run_dir = tmp_path / "campaigns" / "unit-run"
    run_dir.mkdir(parents=True)
    campaign, document = _preflight_document(run_dir)
    preflight.commit_campaign_bundle(run_dir, campaign, document)
    campaign_path = run_dir / "campaign.json"
    marker_path = run_dir / "preflight.json"
    before = {
        path: (path.read_bytes(), path.stat().st_dev, path.stat().st_ino)
        for path in (campaign_path, marker_path)
    }

    loaded = preflight.load_completed_campaign_for_request(campaign_path, _request(run_dir))

    assert loaded["_path"] == str(campaign_path)
    assert {
        path: (path.read_bytes(), path.stat().st_dev, path.stat().st_ino)
        for path in (campaign_path, marker_path)
    } == before


@pytest.mark.parametrize(
    "field",
    ["candidate_wheel", "primary_interpreter", "transform_output", "transform_manifest"],
)
def test_completed_request_reentry_rejects_every_changed_external_identity(
    tmp_path: Path,
    field: str,
) -> None:
    run_dir = tmp_path / "campaigns" / "unit-run"
    run_dir.mkdir(parents=True)
    campaign, document = _preflight_document(run_dir)
    preflight.commit_campaign_bundle(run_dir, campaign, document)
    request = _request(run_dir)
    if field == "candidate_wheel":
        request["candidate_wheel"] = str(tmp_path / "other.whl")
    elif field == "primary_interpreter":
        request["interpreters"]["primary"] = str(tmp_path / "other-python")  # type: ignore[index]
    elif field == "transform_output":
        request["transform"]["output"] = str(tmp_path / "other.gtf.gz")  # type: ignore[index]
    else:
        request["transform"]["manifest"] = str(tmp_path / "other.json")  # type: ignore[index]

    with pytest.raises(model.CampaignError, match="request"):
        preflight.load_completed_campaign_for_request(run_dir / "campaign.json", request)


def test_encoded_path_budget_counts_multibyte_names_and_terminating_nul() -> None:
    assert preflight.validate_encoded_path_budget(
        [Path("/a/éé")],
        name_max=4,
        path_max=8,
    ) == {"name_max": 4, "path_max": 8, "checked_paths": ["/a/éé"]}
    with pytest.raises(model.CampaignError, match="NAME_MAX|component"):
        preflight.validate_encoded_path_budget(
            [Path("/a/ééé")],
            name_max=5,
            path_max=20,
        )
    with pytest.raises(model.CampaignError, match="PATH_MAX|path"):
        preflight.validate_encoded_path_budget(
            [Path("/a/éé")],
            name_max=4,
            path_max=7,
        )
    assert max(len(os.fsencode(name)) for name in safe_io.internal_path_limit_components()) <= 255


def test_input_verification_is_descriptor_bound_and_rejects_aliases(tmp_path: Path) -> None:
    source = tmp_path / "input.gff3.gz"
    with gzip.open(source, "wb") as handle:
        handle.write(b"##gff-version 3\nchr1\ts\tgene\t1\t2\t.\t+\t.\tID=g\n")
    digest = hashlib.sha256(source.read_bytes()).hexdigest()

    assert preflight.verify_input_file(
        source,
        expected_size=source.stat().st_size,
        expected_sha256=digest,
    ) == {
        "path": str(source),
        "bytes": source.stat().st_size,
        "sha256": digest,
        "gzip_crc_ok": True,
    }

    alias = tmp_path / "alias.gff3.gz"
    alias.symlink_to(source)
    with pytest.raises(model.CampaignError, match="symlink"):
        preflight.verify_input_file(
            alias,
            expected_size=source.stat().st_size,
            expected_sha256=digest,
        )
    hardlink = tmp_path / "hardlink.gff3.gz"
    os.link(source, hardlink)
    with pytest.raises(model.CampaignError, match="uniquely linked"):
        preflight.verify_input_file(
            source,
            expected_size=source.stat().st_size,
            expected_sha256=digest,
        )


def test_candidate_wheel_is_hashed_from_a_no_follow_snapshot(tmp_path: Path) -> None:
    wheel = tmp_path / "gffbase-0.2.0rc1-cp310-abi3-manylinux_x86_64.whl"
    with zipfile.ZipFile(wheel, "w") as archive:
        archive.writestr(
            "gffbase-0.2.0rc1.dist-info/METADATA",
            "Metadata-Version: 2.3\nName: gffbase\nVersion: 0.2.0rc1\n",
        )
    identity = preflight.verify_candidate_wheel(wheel)

    assert identity["path"] == str(wheel)
    assert identity["bytes"] == wheel.stat().st_size
    assert identity["sha256"] == hashlib.sha256(wheel.read_bytes()).hexdigest()
    alias = tmp_path / "gffbase-0.2.0rc1-cp311-abi3-manylinux_x86_64.whl"
    alias.symlink_to(wheel)
    with pytest.raises(model.CampaignError, match="symlink"):
        preflight.verify_candidate_wheel(alias)


def test_future_path_plan_covers_every_job_artifact_and_internal_namespace(
    tmp_path: Path,
) -> None:
    run_dir = tmp_path / "campaigns" / "unit-run"
    request = _request(run_dir)
    jobs = model.campaign_jobs(_campaign(run_dir))

    paths = preflight.campaign_future_paths(request, jobs)
    path_strings = {str(path) for path in paths}

    assert len(paths) == len(path_strings)
    assert all(path.is_absolute() for path in paths)
    assert str(run_dir / "campaign.json") in path_strings
    assert str(run_dir / "preflight.json") in path_strings
    assert str(run_dir / "campaign-results.json") in path_strings
    assert str(run_dir / "jobs" / jobs[0].job_id / "status.json") in path_strings
    deepest_attempt = run_dir / "jobs" / jobs[-1].job_id / "attempts" / "9999"
    for relative in (
        "stdout.log",
        "stderr.log",
        "raw.json",
        "result.json",
        "scratch/06_mega.json",
        "scratch/bridge.duckdb",
    ):
        assert str(deepest_attempt / relative) in path_strings
    components = {part for path in paths for part in path.parts}
    assert set(safe_io.internal_path_limit_components()) <= components
    assert preflight.PROBE_ROOT_NAME in components


def test_filesystem_identity_matches_the_open_target(tmp_path: Path) -> None:
    identity = preflight.filesystem_identity(tmp_path)

    assert identity["st_dev"] == os.lstat(tmp_path).st_dev
    assert type(identity["mount_id"]) is int
    assert type(identity["mount_point"]) is str
    assert tmp_path.is_relative_to(Path(str(identity["mount_point"])))
    assert identity["filesystem_type"]


def test_mutable_filesystem_qualification_deduplicates_by_mount_and_device(
    tmp_path: Path,
) -> None:
    first = tmp_path / "first"
    second = tmp_path / "second"
    third = tmp_path / "third"
    for path in (first, second, third):
        path.mkdir()
    identities = {
        first: {"mount_id": 10, "st_dev": 1, "mount_point": "/one"},
        second: {"mount_id": 10, "st_dev": 1, "mount_point": "/one"},
        third: {"mount_id": 20, "st_dev": 2, "mount_point": "/two"},
    }
    probed: list[Path] = []

    def identify(path: Path) -> dict[str, object]:
        return identities[path]

    def probe(path: Path) -> dict[str, object]:
        probed.append(path)
        return {
            "probe_path": str(path / preflight.PROBE_ROOT_NAME / "probe-unit"),
            "st_dev": identities[path]["st_dev"],
            "name_max": 255,
            "path_max": 4096,
            "directory_fsync": True,
            "rename_noreplace_same_directory": True,
            "rename_noreplace_collision": True,
            "rename_noreplace_cross_directory": True,
            "rename_exchange_same_directory": True,
            "parents_fsynced": True,
        }

    evidence = preflight.qualify_mutable_filesystems(
        {
            first: [first / "a"],
            second: [second / "b"],
            third: [third / "c"],
        },
        identity_provider=identify,
        capability_probe=probe,
    )

    assert probed == [first, third]
    assert len(evidence) == 2
    assert evidence[0]["mutable_targets"] == [str(first), str(second)]
    assert evidence[0]["path_budget"]["checked_paths"] == [  # type: ignore[index]
        str(first / "a"),
        str(second / "b"),
    ]


def test_mutable_filesystem_qualification_rejects_identity_drift(tmp_path: Path) -> None:
    target = tmp_path / "target"
    target.mkdir()
    calls = 0

    def identify(_path: Path) -> dict[str, object]:
        nonlocal calls
        calls += 1
        return {
            "mount_id": 1,
            "st_dev": 1,
            "mount_point": "/before" if calls == 1 else "/after",
        }

    def probe(path: Path) -> dict[str, object]:
        return {
            "probe_path": str(path / "probe"),
            "st_dev": 1,
            "name_max": 255,
            "path_max": 4096,
            "directory_fsync": True,
            "rename_noreplace_same_directory": True,
            "rename_noreplace_collision": True,
            "rename_noreplace_cross_directory": True,
            "rename_exchange_same_directory": True,
            "parents_fsynced": True,
        }

    with pytest.raises(model.CampaignError, match="identity changed"):
        preflight.qualify_mutable_filesystems(
            {target: [target / "artifact"]},
            identity_provider=identify,
            capability_probe=probe,
        )


def test_campaign_root_and_run_directory_are_created_privately_and_exclusively(
    tmp_path: Path,
) -> None:
    campaign_root = tmp_path / "nested" / "campaigns"

    assert preflight.ensure_campaign_root(campaign_root) == campaign_root
    assert campaign_root.is_dir()
    assert stat.S_IMODE(campaign_root.stat().st_mode) == 0o700
    run_dir = preflight.create_private_run_directory(campaign_root, "unit-run")
    assert run_dir == campaign_root / "unit-run"
    assert stat.S_IMODE(run_dir.stat().st_mode) == 0o700

    with pytest.raises(model.CampaignError, match="already exists"):
        preflight.create_private_run_directory(campaign_root, "unit-run")


def test_campaign_root_creation_rejects_symlinked_ancestor(tmp_path: Path) -> None:
    real = tmp_path / "real"
    real.mkdir()
    alias = tmp_path / "alias"
    alias.symlink_to(real, target_is_directory=True)

    with pytest.raises(model.CampaignError, match="symlink"):
        preflight.ensure_campaign_root(alias / "campaigns")
    assert not (real / "campaigns").exists()


def test_run_directory_creation_failure_leaves_an_unlaunchable_partial_directory(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    campaign_root = tmp_path / "campaigns"
    campaign_root.mkdir()
    original = preflight._fsync_directory
    calls = 0

    def fail_second_fsync(descriptor: int) -> None:
        nonlocal calls
        calls += 1
        if calls == 2:
            raise model.CampaignError("injected run-directory fsync failure")
        original(descriptor)

    monkeypatch.setattr(preflight, "_fsync_directory", fail_second_fsync)
    with pytest.raises(model.CampaignError, match="injected"):
        preflight.create_private_run_directory(campaign_root, "partial")

    run_dir = campaign_root / "partial"
    assert run_dir.is_dir()
    assert not (run_dir / "campaign.json").exists()
    assert not (run_dir / "preflight.json").exists()


def test_filesystem_capability_probe_exercises_and_retains_every_rename_case(
    tmp_path: Path,
) -> None:
    target = tmp_path / "run"
    target.mkdir()
    evidence = preflight.probe_filesystem_capabilities(
        target,
        token_provider=lambda: "a" * 32,
    )

    assert evidence["directory_fsync"] is True
    assert evidence["rename_noreplace_same_directory"] is True
    assert evidence["rename_noreplace_collision"] is True
    assert evidence["rename_noreplace_cross_directory"] is True
    assert evidence["rename_exchange_same_directory"] is True
    assert evidence["parents_fsynced"] is True
    probe_dir = Path(str(evidence["probe_path"]))
    assert probe_dir.is_dir()
    assert (probe_dir / "left" / "noreplace-final").read_bytes() == b"same-directory"
    assert (probe_dir / "left" / "collision-source").read_bytes() == b"source"
    assert (probe_dir / "left" / "collision-final").read_bytes() == b"destination"
    assert (probe_dir / "right" / "cross-final").read_bytes() == b"cross-directory"
    assert (probe_dir / "left" / "exchange-a").read_bytes() == b"exchange-a"
    assert (probe_dir / "left" / "exchange-b").read_bytes() == b"exchange-b"


def test_run_tree_exact_scan_accepts_only_the_phase_derived_probe_tree(tmp_path: Path) -> None:
    root = tmp_path / "campaigns"
    root.mkdir()
    run_dir = preflight.create_private_run_directory(root, "unit-run")
    preflight.probe_filesystem_capabilities(run_dir, token_provider=lambda: "c" * 32)

    preflight.validate_run_tree(run_dir, transform_mode="external", phase="probed")

    (run_dir / ".gffbase-probes-lookalike").write_text("forged", encoding="utf-8")
    with pytest.raises(model.CampaignError, match="entries|unexpected|differ"):
        preflight.validate_run_tree(run_dir, transform_mode="external", phase="probed")


def test_run_tree_exact_scan_rejects_malformed_retained_probe(tmp_path: Path) -> None:
    root = tmp_path / "campaigns"
    root.mkdir()
    run_dir = preflight.create_private_run_directory(root, "unit-run")
    evidence = preflight.probe_filesystem_capabilities(
        run_dir,
        token_provider=lambda: "d" * 32,
    )
    probe_dir = Path(str(evidence["probe_path"]))
    (probe_dir / "left" / "unexpected").write_text("forged", encoding="utf-8")

    with pytest.raises(model.CampaignError, match="entries|unexpected|differ"):
        preflight.validate_run_tree(run_dir, transform_mode="external", phase="probed")


def test_bundle_commit_runs_exact_phase_scans_around_the_final_marker(tmp_path: Path) -> None:
    run_dir = tmp_path / "campaigns" / "unit-run"
    run_dir.mkdir(parents=True, mode=0o700)
    run_dir.chmod(0o700)
    campaign, document = _preflight_document(run_dir)
    preflight.probe_filesystem_capabilities(run_dir, token_provider=lambda: "e" * 32)
    phases: list[str] = []

    def validate_phase(phase: str) -> None:
        preflight.validate_run_tree(run_dir, transform_mode="external", phase=phase)
        phases.append(phase)

    preflight.commit_campaign_bundle(
        run_dir,
        campaign,
        document,
        phase_validator=validate_phase,
    )

    assert phases == ["probed", "campaign", "complete"]
    assert preflight.load_completed_campaign(run_dir / "campaign.json")["run_id"] == "unit-run"


@pytest.mark.parametrize("primitive", ["fsync", "noreplace", "exchange"])
def test_filesystem_capability_probe_fails_closed_on_unsupported_primitive(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    primitive: str,
) -> None:
    target = tmp_path / primitive
    target.mkdir()

    def unsupported(*args: object, **kwargs: object) -> None:
        raise model.CampaignError(f"injected unsupported {primitive}")

    if primitive == "fsync":
        monkeypatch.setattr(preflight, "_fsync_directory", unsupported)
    elif primitive == "noreplace":
        monkeypatch.setattr(safe_io, "_rename_noreplace_at", unsupported)
    else:
        monkeypatch.setattr(safe_io, "_rename_exchange_at", unsupported)

    with pytest.raises(model.CampaignError, match="unsupported"):
        preflight.probe_filesystem_capabilities(
            target,
            token_provider=lambda: "b" * 32,
        )


# ---------------------------------------------------------------------------
# The mount record carries a measured capability, not just strings
# ---------------------------------------------------------------------------
#
# `supports_noreplace_rename` records whether the campaign root can actually do
# the atomic publish `safe_io` depends on. It is a bool, and every other value
# in the mount block is a non-empty string, so the validator has to distinguish
# them rather than rejecting the one that matters.


def _mount(**overrides):
    record = {
        "raw": "/dev/nvme1n1 xfs /srv/nvme1 rw",
        "fstype": "xfs",
        "probe_path": "/srv/nvme1",
        "supports_noreplace_rename": True,
    }
    record.update(overrides)
    return record


def test_the_measured_rename_capability_is_part_of_the_mount_record(tmp_path: Path) -> None:
    resources = _resources(tmp_path)
    resources["mount"] = _mount(probe_path=str(tmp_path))
    preflight._validate_resource_snapshot(resources, "resources.before")


def test_a_mount_record_without_the_capability_is_rejected(tmp_path: Path) -> None:
    """A probe from before the key existed must not validate: the campaign
    would then publish into a root nobody checked."""
    resources = _resources(tmp_path)
    record = _mount(probe_path=str(tmp_path))
    del record["supports_noreplace_rename"]
    resources["mount"] = record
    with pytest.raises(model.CampaignError, match="mount"):
        preflight._validate_resource_snapshot(resources, "resources.before")


def test_the_capability_must_be_a_real_boolean(tmp_path: Path) -> None:
    """`"false"` is a non-empty string and therefore truthy; accepting it would
    turn a fail-closed check into a decorative one."""
    resources = _resources(tmp_path)
    resources["mount"] = _mount(probe_path=str(tmp_path), supports_noreplace_rename="false")
    with pytest.raises(model.CampaignError, match="mount"):
        preflight._validate_resource_snapshot(resources, "resources.before")
