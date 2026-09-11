"""Synthetic end-to-end campaign acceptance without real benchmarks or tmux."""

from __future__ import annotations

import gzip
import hashlib
import json
from collections.abc import Sequence
from pathlib import Path
from types import SimpleNamespace

import pytest

from benchmarks import cluster_campaign as campaign
from tests import test_cluster_campaign as fixtures
from tests._platform import LINUX_ONLY_CAMPAIGN

pytestmark = LINUX_ONLY_CAMPAIGN


def _write_tiny_gzip(path: Path, payload: bytes) -> None:
    with path.open("wb") as raw:
        with gzip.GzipFile(filename="", mode="wb", fileobj=raw, mtime=0) as compressed:
            compressed.write(payload)


def _install_tiny_valid_inputs(monkeypatch: pytest.MonkeyPatch) -> None:
    original_inputs = fixtures._inputs

    def valid_inputs(root: Path) -> dict:
        values = original_inputs(root)
        for key in campaign.CORPUS_ORDER:
            entry = values[key]
            path = Path(entry["path"])
            payload = (
                b'chr1\tsynthetic\texon\t1\t10\t.\t+\t.\tgene_id "g"; transcript_id "t";\n'
                if entry["format"] == "gtf"
                else b"##gff-version 3\nchr1\tsynthetic\tgene\t1\t10\t.\t+\t.\tID=g\n"
            )
            _write_tiny_gzip(path, payload)

        control = values["gencode-gtf-parent-stripped"]
        control_path = Path(control["path"])
        _write_tiny_gzip(
            control_path,
            b'chr1\tsynthetic\texon\t1\t10\t.\t+\t.\tgene_id "g"; transcript_id "t";\n',
        )
        manifest_path = Path(control["manifest_path"])
        manifest_path.write_bytes(b'{"schema_version":"synthetic-transform-v1"}\n')
        control.update(
            {
                "bytes": control_path.stat().st_size,
                "sha256": hashlib.sha256(control_path.read_bytes()).hexdigest(),
                "manifest_sha256": hashlib.sha256(manifest_path.read_bytes()).hexdigest(),
            }
        )
        return values

    monkeypatch.setattr(fixtures, "_inputs", valid_inputs)


def test_synthetic_preflight_crash_resume_merge_and_publication_end_to_end(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _install_tiny_valid_inputs(monkeypatch)
    campaign_root = tmp_path / "campaign"
    campaign_root.mkdir()
    value = fixtures._make_campaign(campaign_root)
    campaign_path = Path(value["_path"])
    run_dir = campaign_path.parent
    jobs = list(campaign._jobs(value))
    exploratory = [job for job in jobs if job.phase == "exploratory"]
    canonical = [job for job in jobs if job.phase == "canonical"]
    assert (len(jobs), len(exploratory), len(canonical)) == (36, 25, 11)

    for input_value in value["spec"]["inputs"].values():
        with gzip.open(input_value["path"], "rb") as stream:
            assert stream.read().startswith((b"chr1", b"##gff-version"))
    derived = value["spec"]["inputs"]["gencode-gtf-parent-stripped"]
    assert json.loads(Path(derived["manifest_path"]).read_text())["schema_version"] == (
        "synthetic-transform-v1"
    )

    monkeypatch.setattr(campaign.socket, "gethostname", lambda: "cluster.example")
    monkeypatch.setattr(campaign, "_tmux_sessions", lambda: set())
    monkeypatch.setattr(campaign, "_campaign_panes", lambda _campaign: ())
    monkeypatch.setattr(campaign, "_cheap_revalidate", lambda _campaign: None)

    launch_counts: dict[str, int] = {}
    current_launches: dict[
        str,
        tuple[
            dict[str, object],
            campaign._campaign_worker.PaneIdentity,
            campaign._campaign_worker.ProcessIdentity,
        ],
    ] = {}
    next_pane = 1

    def fake_execute_worker_launches(
        campaign_value: dict,
        worker_ids: Sequence[str],
        commands: Sequence[Sequence[str]],
        *,
        resume: bool,
    ) -> list[dict[str, object]]:
        nonlocal next_pane
        assert len(worker_ids) == len(commands)
        records: list[dict[str, object]] = []
        for worker_id in worker_ids:
            launch_number = launch_counts.get(worker_id, 0) + 1
            launch_counts[worker_id] = launch_number
            pane_pid = 5000 + next_pane
            launch, pane = fixtures._runtime_launch(
                campaign_value,
                worker_id,
                pane_id=f"%{next_pane}",
                pane_pid=pane_pid,
                launch_number=launch_number,
                resume=resume,
            )
            worker_identity = fixtures._runtime_identity(
                pane_pid,
                ticks=1000 + next_pane,
            )
            current_launches[worker_id] = (launch, pane, worker_identity)
            records.append(launch)
            next_pane += 1
        return records

    monkeypatch.setattr(campaign, "_execute_worker_launches", fake_execute_worker_launches)

    def forbid_measurement(*_args: object, **_kwargs: object) -> dict:
        raise AssertionError("synthetic resume remeasured an immutable result")

    monkeypatch.setattr(campaign, "run_job", forbid_measurement)

    assert (
        campaign.cmd_launch(SimpleNamespace(campaign=campaign_path, resume=False, execute=True))
        == 0
    )
    first_job = exploratory[0]
    first_launch, first_pane, first_worker = current_launches[first_job.worker_id]
    first_child = fixtures._runtime_identity(7001, ticks=2001)
    first_attempt = campaign._campaign_worker.allocate_attempt(run_dir, first_job)
    running = fixtures._write_runtime_running_status(
        value,
        first_job,
        pane=first_pane,
        worker_identity=first_worker,
        child_identity=first_child,
    )
    crashed_result = fixtures._runtime_success_result(
        value,
        first_job,
        pane=first_pane,
        worker_identity=first_worker,
        child_identity=first_child,
    )
    crashed_result["started_utc"] = running["started_utc"]
    crashed_result["finished_utc"] = "2026-08-31T12:02:00Z"
    campaign.atomic_create_json(first_attempt / "result.json", crashed_result)

    assert (
        campaign.cmd_launch(SimpleNamespace(campaign=campaign_path, resume=True, execute=True)) == 0
    )
    resumed_launch, _resumed_pane, resumed_worker = current_launches[first_job.worker_id]
    promoted = campaign._reconcile_worker_job(
        value,
        first_job,
        resume=True,
        launch=resumed_launch,
        worker_identity=resumed_worker,
        panes=(),
    )
    assert promoted == crashed_result
    assert campaign.read_job_state(value, first_job)["state"] == "succeeded"
    assert first_launch["pane"] == crashed_result["pane"]

    child_number = 7002

    def persist_and_promote(job: campaign.JobSpec) -> None:
        nonlocal child_number
        launch, pane, worker_identity = current_launches[job.worker_id]
        child_identity = fixtures._runtime_identity(child_number, ticks=child_number + 1000)
        child_number += 1
        attempt_dir = campaign._campaign_worker.allocate_attempt(run_dir, job)
        result = fixtures._runtime_success_result(
            value,
            job,
            pane=pane,
            worker_identity=worker_identity,
            child_identity=child_identity,
        )
        result["started_utc"] = "2026-08-31T12:01:00Z"
        result["finished_utc"] = "2026-08-31T12:02:00Z"
        campaign.atomic_create_json(attempt_dir / "result.json", result)
        accepted = campaign._reconcile_worker_job(
            value,
            job,
            resume=True,
            launch=launch,
            worker_identity=worker_identity,
            panes=(),
        )
        assert accepted == result

    for job in exploratory[1:]:
        persist_and_promote(job)
    assert len(campaign._accepted_results(value)) == 25

    assert (
        campaign.cmd_canonical(SimpleNamespace(campaign=campaign_path, resume=False, execute=True))
        == 0
    )
    assert set(current_launches) == {
        *(lane["worker_id"] for lane in value["spec"]["topology"]["lanes"]),
        "canonical",
    }
    for job in canonical:
        persist_and_promote(job)

    accepted = campaign._accepted_results(value)
    assert len(accepted) == 36
    assert campaign._campaign_panes(value) == ()

    publish_root = tmp_path / "published-results"
    publish_root.mkdir()
    historical_source = campaign.ROOT / "benchmarks" / "results" / "06_mega.json"
    historical = historical_source.read_bytes()
    (publish_root / "06_mega.json").write_bytes(historical)

    assert (
        campaign.cmd_merge(
            SimpleNamespace(
                campaign=campaign_path,
                execute=True,
                publish=True,
                publish_root=publish_root,
            )
        )
        == 0
    )

    merged_path = run_dir / "campaign-results.json"
    merged = campaign._campaign_safe_io.strict_json_load(
        merged_path,
        validator=campaign._campaign_results.validate_campaign_results,
    )
    assert merged["gates"]["publishable"] is True
    assert len(merged["worker_results"]) == 36

    index_path = publish_root / "index.json"
    index = campaign._campaign_safe_io.strict_public_json_load(index_path)
    validated_index = campaign._campaign_results.validate_results_index(index, publish_root)
    linux = validated_index["platforms"]["linux-x86_64"]
    run = linux["runs"][linux["canonical_run_id"]]
    portable_path = publish_root / run["artifact"]
    portable = campaign._campaign_safe_io.strict_public_json_load(
        portable_path,
        validator=campaign._campaign_results.validate_portable_campaign_results,
    )
    assert portable["source_results_sha256"] == merged["results_sha256"]
    assert (publish_root / "06_mega.json").read_bytes() == historical
    assert capsys.readouterr().err == ""
