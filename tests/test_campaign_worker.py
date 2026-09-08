"""Pure tmux launch and pane-identity contracts for campaign workers."""

from __future__ import annotations

import copy
import json
import shlex
import signal
import stat
import subprocess
from pathlib import Path

import pytest

from benchmarks.campaign import model, safe_io, worker


def _job_matrix() -> list[model.JobSpec]:
    inputs: dict[str, object] = {}
    for key in model.CORPUS_ORDER:
        inputs[key] = {
            "path": f"/inputs/{key}.gz",
            "bytes": 1,
            "sha256": "1" * 64,
            "gzip_crc_ok": True,
            "format": "gtf" if key == "gencode-gtf" else "gff3",
        }
    inputs["gencode-gtf-parent-stripped"] = {
        "path": "/inputs/gencode-gtf-parent-stripped.gz",
        "bytes": 1,
        "sha256": "2" * 64,
        "gzip_crc_ok": True,
        "format": "gtf",
        "manifest_path": "/inputs/gencode-gtf-parent-stripped.manifest.json",
        "manifest_sha256": "3" * 64,
        "transform_version": "1",
        "counts": {
            "input_feature_lines": 3,
            "output_feature_lines": 1,
            "comment_or_blank_lines": 0,
            "removed_gene_rows": 1,
            "removed_transcript_rows": 1,
        },
    }
    return model.build_job_matrix({}, {}, inputs)


def _status_job() -> model.JobSpec:
    return _job_matrix()[0]


def _process(pid: int, *, ticks: int = 99) -> dict[str, object]:
    return {
        "pid": pid,
        "pgid": 4321,
        "process_start_ticks": ticks,
        "boot_id": "12345678-1234-1234-1234-123456789abc",
    }


def _running_status() -> tuple[model.JobSpec, dict[str, object]]:
    job = _status_job()
    attempt_dir = f"jobs/{job.job_id}/attempts/0001"
    return job, {
        "schema_version": model.STATUS_SCHEMA,
        "run_id": "unit-run",
        "campaign_sha256": "a" * 64,
        "job_id": job.job_id,
        "job_sha256": job.job_sha256,
        "worker_id": job.worker_id,
        "state": "running",
        "attempt": 1,
        "started_utc": "2026-08-31T12:00:00Z",
        "host": "cluster.example",
        "cpu_affinity": list(range(10)),
        "pane": {
            "session": "gffbase-aaaaaaaaaaaa-cluster",
            "window": job.worker_id,
            "pane_id": "%7",
            "pane_pid": 4321,
            "pane_dead": False,
        },
        "worker": _process(4321),
        "child": None,
        "paths": {
            "attempt_dir": attempt_dir,
            "stdout_log": f"{attempt_dir}/stdout.log",
            "stderr_log": f"{attempt_dir}/stderr.log",
            "result": f"{attempt_dir}/result.json",
        },
    }


def _attempt_result_validator(value: object, attempt: int, _path: Path) -> bool:
    record = model.require_exact_keys(value, {"job_id", "attempt", "state"}, "unit attempt result")
    if record["job_id"] != _status_job().job_id or record["attempt"] != attempt:
        raise model.CampaignError("unit attempt result identity mismatch")
    if record["state"] not in {"failed", "succeeded"}:
        raise model.CampaignError("unit attempt result state mismatch")
    return record["state"] == "succeeded"


def test_first_tmux_launch_places_window_and_print_options_before_the_command(
    tmp_path: Path,
) -> None:
    worker_argv = ["/env/bin/python", "-I", "/repo/cluster_campaign.py", "_worker"]
    argv = worker.build_tmux_launch_argv(
        session="gffbase-abc-cluster",
        window="scale-t01",
        cpu_list="0-9",
        worker_argv=worker_argv,
        cwd=tmp_path,
        first_window=True,
        tmux_path="/usr/bin/tmux",
        taskset_path="/usr/bin/taskset",
    )

    assert argv[:2] == ["/usr/bin/tmux", "new-session"]
    command_index = len(argv) - 1
    assert argv.index("-n") < command_index
    assert argv[argv.index("-n") + 1] == "scale-t01"
    assert argv.index("-P") < command_index
    assert argv[argv.index("-F") + 1] == worker.TMUX_PANE_FORMAT
    assert shlex.split(argv[-1]) == [
        "exec",
        "/usr/bin/taskset",
        "--cpu-list",
        "0-9",
        *worker_argv,
    ]


def test_later_tmux_window_uses_exact_session_target_and_same_capture_format(
    tmp_path: Path,
) -> None:
    argv = worker.build_tmux_launch_argv(
        session="gffbase-abc-cluster",
        window="scale-t04",
        cpu_list="10-19",
        worker_argv=["/env/bin/python", "worker.py"],
        cwd=tmp_path,
        first_window=False,
        tmux_path="tmux",
        taskset_path="taskset",
    )

    assert argv[:2] == ["tmux", "new-window"]
    assert argv[argv.index("-t") + 1] == "gffbase-abc-cluster:"
    assert argv[argv.index("-n") + 1] == "scale-t04"
    assert argv[argv.index("-F") + 1] == worker.TMUX_PANE_FORMAT


@pytest.mark.parametrize(
    "line",
    [
        "gffbase-abc-cluster\tscale-t01\t%1\t123\t0\textra",
        "gffbase-abc-cluster\tscale-t01\t1\t123\t0",
        "gffbase-abc-cluster\tscale-t01\t%1\t0\t0",
        "gffbase-abc-cluster\tscale-t01\t%1\t123\t2",
        "bad;session\tscale-t01\t%1\t123\t0",
    ],
)
def test_tmux_pane_parser_rejects_open_or_malformed_identity(line: str) -> None:
    with pytest.raises(model.CampaignError):
        worker.parse_tmux_pane(line)


def test_tmux_pane_parser_and_query_bind_all_liveness_fields() -> None:
    pane = worker.parse_tmux_pane("gffbase-abc-cluster\tscale-t01\t%7\t4321\t0")

    assert pane.to_dict() == {
        "session": "gffbase-abc-cluster",
        "window": "scale-t01",
        "pane_id": "%7",
        "pane_pid": 4321,
        "pane_dead": False,
    }
    assert worker.build_tmux_list_panes_argv("gffbase-abc-cluster", tmux_path="/usr/bin/tmux") == [
        "/usr/bin/tmux",
        "list-panes",
        "-t",
        "gffbase-abc-cluster:",
        "-F",
        worker.TMUX_PANE_FORMAT,
    ]


def test_pending_and_running_status_variants_are_closed_and_identity_bound() -> None:
    job, running = _running_status()
    pending = worker.pending_status(run_id="unit-run", campaign_sha256="a" * 64, job=job)

    assert (
        worker.validate_status(pending, run_id="unit-run", campaign_sha256="a" * 64, job=job)
        == pending
    )
    assert (
        worker.validate_status(running, run_id="unit-run", campaign_sha256="a" * 64, job=job)
        == running
    )

    forged_values = []
    forged = copy.deepcopy(running)
    forged["unexpected"] = True
    forged_values.append(forged)
    forged = copy.deepcopy(running)
    forged["pane"]["unexpected"] = True  # type: ignore[index]
    forged_values.append(forged)
    forged = copy.deepcopy(running)
    forged["worker"]["pid"] = True  # type: ignore[index]
    forged_values.append(forged)
    forged = copy.deepcopy(running)
    forged["cpu_affinity"] = [0]
    forged_values.append(forged)
    forged = copy.deepcopy(running)
    forged["paths"]["stdout_log"] = "../outside.log"  # type: ignore[index]
    forged_values.append(forged)
    forged = copy.deepcopy(pending)
    forged["started_utc"] = "2026-08-31T12:00:00Z"
    forged_values.append(forged)

    for forged in forged_values:
        with pytest.raises(model.CampaignError):
            worker.validate_status(
                forged,
                run_id="unit-run",
                campaign_sha256="a" * 64,
                job=job,
            )


def test_terminal_status_requires_an_immutable_result_digest_and_ordered_times() -> None:
    job, running = _running_status()
    terminal = {
        **running,
        "state": "interrupted",
        "finished_utc": "2026-08-31T12:01:00Z",
        "result_sha256": "b" * 64,
    }
    assert (
        worker.validate_status(terminal, run_id="unit-run", campaign_sha256="a" * 64, job=job)
        == terminal
    )

    missing_digest = dict(terminal)
    del missing_digest["result_sha256"]
    with pytest.raises(model.CampaignError):
        worker.validate_status(
            missing_digest,
            run_id="unit-run",
            campaign_sha256="a" * 64,
            job=job,
        )

    backwards = dict(terminal)
    backwards["finished_utc"] = "2026-08-31T11:59:59Z"
    with pytest.raises(model.CampaignError):
        worker.validate_status(
            backwards,
            run_id="unit-run",
            campaign_sha256="a" * 64,
            job=job,
        )


def test_running_liveness_requires_exact_pane_worker_and_child_identities() -> None:
    job, running = _running_status()
    validated = worker.validate_status(
        running, run_id="unit-run", campaign_sha256="a" * 64, job=job
    )
    pane = worker.PaneIdentity.from_dict(running["pane"])
    observed = {
        4321: worker.ProcessIdentity.from_dict(running["worker"]),
        5000: worker.ProcessIdentity.from_dict(_process(5000, ticks=100)),
    }

    def observe(pid: int) -> worker.ProcessIdentity | None:
        return observed.get(pid)

    assert (
        worker.classify_running_status(
            validated,
            local_host="cluster.example",
            panes=[pane],
            process_observer=observe,
        )
        == "live"
    )
    assert (
        worker.classify_running_status(
            validated,
            local_host="cluster.example",
            panes=[],
            process_observer=observe,
        )
        == "stale"
    )

    reused = dict(observed)
    reused[4321] = worker.ProcessIdentity.from_dict(_process(4321, ticks=100))
    assert (
        worker.classify_running_status(
            validated,
            local_host="cluster.example",
            panes=[pane],
            process_observer=reused.get,
        )
        == "stale"
    )
    assert (
        worker.classify_running_status(
            validated,
            local_host="different.example",
            panes=[],
            process_observer=lambda _pid: None,
        )
        == "foreign"
    )

    with_child = copy.deepcopy(running)
    with_child["child"] = _process(5000, ticks=100)
    validated_child = worker.validate_status(
        with_child, run_id="unit-run", campaign_sha256="a" * 64, job=job
    )
    assert (
        worker.classify_running_status(
            validated_child,
            local_host="cluster.example",
            panes=[pane],
            process_observer=lambda pid: observed.get(pid) if pid != 5000 else None,
        )
        == "stale"
    )

    dead_pane = worker.PaneIdentity(
        session=pane.session,
        window=pane.window,
        pane_id=pane.pane_id,
        pane_pid=pane.pane_pid,
        pane_dead=True,
    )
    assert (
        worker.classify_running_status(
            validated,
            local_host="cluster.example",
            panes=[dead_pane],
            process_observer=observe,
        )
        == "stale"
    )


def test_attempt_allocation_is_private_contiguous_and_success_scan_is_exact(
    tmp_path: Path,
) -> None:
    job = _status_job()
    first = worker.allocate_attempt(tmp_path, job)
    assert first == tmp_path / "jobs" / job.job_id / "attempts" / "0001"
    assert stat.S_IMODE(first.stat().st_mode) == 0o700
    assert stat.S_IMODE((first / "scratch").stat().st_mode) == 0o700
    safe_io.atomic_create_json(
        first / "result.json",
        {"job_id": job.job_id, "attempt": 1, "state": "failed"},
    )
    second = worker.allocate_attempt(tmp_path, job)
    safe_io.atomic_create_json(
        second / "result.json",
        {"job_id": job.job_id, "attempt": 2, "state": "succeeded"},
    )

    inventory = worker.scan_attempts(tmp_path, job, result_validator=_attempt_result_validator)
    assert inventory.attempts == (1, 2)
    assert inventory.next_attempt == 3
    assert [result.attempt for result in inventory.results] == [1, 2]
    assert [result.successful for result in inventory.results] == [False, True]
    assert [success.attempt for success in inventory.successes] == [2]
    assert inventory.successes[0].path == second / "result.json"


def test_attempt_scan_rejects_gaps_unknown_entries_and_symlinks(tmp_path: Path) -> None:
    job = _status_job()
    first = worker.allocate_attempt(tmp_path, job)
    attempts = first.parent

    (attempts / "0003").mkdir(mode=0o700)
    with pytest.raises(model.CampaignError, match="contiguous"):
        worker.scan_attempts(tmp_path, job, result_validator=_attempt_result_validator)

    (attempts / "0003").rmdir()
    (first / "unexpected").write_text("poison", encoding="utf-8")
    with pytest.raises(model.CampaignError, match="entries|unexpected"):
        worker.scan_attempts(tmp_path, job, result_validator=_attempt_result_validator)


def test_attempt_scan_rejects_symlinked_attempt_tree(tmp_path: Path) -> None:
    job = _status_job()
    first = worker.allocate_attempt(tmp_path, job)
    (first / "outside").symlink_to(tmp_path)
    with pytest.raises(model.CampaignError, match="symlink|entries"):
        worker.scan_attempts(tmp_path, job, result_validator=_attempt_result_validator)


def test_reconciliation_promotes_one_success_and_poisons_multiple(tmp_path: Path) -> None:
    job = _status_job()
    pending = worker.pending_status(run_id="unit-run", campaign_sha256="a" * 64, job=job)
    first = worker.allocate_attempt(tmp_path, job)
    safe_io.atomic_create_json(
        first / "result.json",
        {"job_id": job.job_id, "attempt": 1, "state": "succeeded"},
    )
    inventory = worker.scan_attempts(tmp_path, job, result_validator=_attempt_result_validator)
    decision = worker.decide_reconciliation(
        inventory, pending, resume=False, running_observation=None
    )
    assert decision.action == "promote"
    assert decision.success == inventory.successes[0]
    assert decision.next_attempt is None

    _job, running = _running_status()
    with pytest.raises(model.CampaignError, match="live"):
        worker.decide_reconciliation(
            inventory,
            running,
            resume=True,
            running_observation="live",
        )
    with pytest.raises(model.CampaignError, match="liveness"):
        worker.decide_reconciliation(
            inventory,
            running,
            resume=True,
            running_observation=None,
        )
    assert (
        worker.decide_reconciliation(
            inventory,
            running,
            resume=True,
            running_observation="stale",
        ).action
        == "promote"
    )

    second = worker.allocate_attempt(tmp_path, job)
    safe_io.atomic_create_json(
        second / "result.json",
        {"job_id": job.job_id, "attempt": 2, "state": "succeeded"},
    )
    poisoned = worker.scan_attempts(tmp_path, job, result_validator=_attempt_result_validator)
    with pytest.raises(model.CampaignError, match="multiple|poison"):
        worker.decide_reconciliation(poisoned, pending, resume=True, running_observation=None)


def test_reconciliation_requires_resume_for_terminal_or_stale_attempts(
    tmp_path: Path,
) -> None:
    job, running = _running_status()
    empty = worker.scan_attempts(tmp_path, job, result_validator=_attempt_result_validator)
    pending = worker.pending_status(run_id="unit-run", campaign_sha256="a" * 64, job=job)
    assert (
        worker.decide_reconciliation(empty, pending, resume=False, running_observation=None).action
        == "run"
    )

    worker.allocate_attempt(tmp_path, job)
    incomplete = worker.scan_attempts(tmp_path, job, result_validator=_attempt_result_validator)
    with pytest.raises(model.CampaignError, match="resume"):
        worker.decide_reconciliation(incomplete, pending, resume=False, running_observation=None)
    recovered = worker.decide_reconciliation(
        incomplete, pending, resume=True, running_observation=None
    )
    assert recovered.action == "recover"
    assert recovered.next_attempt == 2

    with pytest.raises(model.CampaignError, match="live"):
        worker.decide_reconciliation(incomplete, running, resume=True, running_observation="live")
    with pytest.raises(model.CampaignError, match="foreign"):
        worker.decide_reconciliation(
            incomplete, running, resume=True, running_observation="foreign"
        )
    assert (
        worker.decide_reconciliation(
            incomplete, running, resume=True, running_observation="stale"
        ).action
        == "recover"
    )

    terminal = {
        **running,
        "state": "failed",
        "finished_utc": "2026-08-31T12:01:00Z",
        "result_sha256": "b" * 64,
    }
    with pytest.raises(model.CampaignError, match="resume"):
        worker.decide_reconciliation(incomplete, terminal, resume=False, running_observation=None)
    assert (
        worker.decide_reconciliation(
            incomplete, terminal, resume=True, running_observation=None
        ).action
        == "run"
    )


def test_process_observation_binds_pid_group_start_ticks_and_boot(tmp_path: Path) -> None:
    proc_root = tmp_path / "proc"
    (proc_root / "123").mkdir(parents=True)
    (proc_root / "sys" / "kernel" / "random").mkdir(parents=True)
    boot_id = "12345678-1234-1234-1234-123456789abc"
    (proc_root / "sys" / "kernel" / "random" / "boot_id").write_text(
        f"{boot_id}\n", encoding="ascii"
    )
    fields = ["S", *(["0"] * 18), "99"]
    (proc_root / "123" / "stat").write_text(
        f"123 (worker (unit)) {' '.join(fields)}\n", encoding="ascii"
    )

    assert worker.observe_process(
        123, proc_root=proc_root, pgid_provider=lambda pid: 456 if pid == 123 else 0
    ) == worker.ProcessIdentity(
        pid=123,
        pgid=456,
        process_start_ticks=99,
        boot_id=boot_id,
    )
    assert worker.observe_process(124, proc_root=proc_root, pgid_provider=lambda _pid: 456) is None

    (proc_root / "123" / "stat").write_text("malformed\n", encoding="ascii")
    with pytest.raises(model.CampaignError, match="stat|process"):
        worker.observe_process(123, proc_root=proc_root, pgid_provider=lambda _pid: 456)


def test_status_io_is_no_follow_transition_checked_and_stale_write_safe(
    tmp_path: Path,
) -> None:
    job, running = _running_status()
    worker.allocate_attempt(tmp_path, job)
    pending = worker.read_status(
        tmp_path,
        run_id="unit-run",
        campaign_sha256="a" * 64,
        job=job,
    )
    assert pending["state"] == "pending"
    assert (
        worker.write_status(
            tmp_path,
            running,
            previous=pending,
            run_id="unit-run",
            campaign_sha256="a" * 64,
            job=job,
        )
        == running
    )
    assert (
        worker.read_status(
            tmp_path,
            run_id="unit-run",
            campaign_sha256="a" * 64,
            job=job,
        )
        == running
    )

    running_with_child = copy.deepcopy(running)
    running_with_child["child"] = _process(5000, ticks=100)
    worker.write_status(
        tmp_path,
        running_with_child,
        previous=running,
        run_id="unit-run",
        campaign_sha256="a" * 64,
        job=job,
    )
    with pytest.raises(model.CampaignError, match="stale|changed"):
        worker.write_status(
            tmp_path,
            running,
            previous=pending,
            run_id="unit-run",
            campaign_sha256="a" * 64,
            job=job,
        )

    invalid_terminal = {
        **running_with_child,
        "state": "failed",
        "finished_utc": "2026-08-31T12:01:00Z",
        "result_sha256": "b" * 64,
    }
    with pytest.raises(model.CampaignError, match="result|digest"):
        worker.write_status(
            tmp_path,
            invalid_terminal,
            previous=running_with_child,
            run_id="unit-run",
            campaign_sha256="a" * 64,
            job=job,
        )


def test_status_reader_rejects_open_shapes_and_symlink_leaf(tmp_path: Path) -> None:
    job = _status_job()
    worker.allocate_attempt(tmp_path, job)
    status_path = tmp_path / "jobs" / job.job_id / "status.json"
    forged = worker.pending_status(run_id="unit-run", campaign_sha256="a" * 64, job=job)
    forged["unexpected"] = True
    status_path.write_text(
        json.dumps(forged, sort_keys=True, separators=(",", ":")) + "\n",
        encoding="utf-8",
    )
    status_path.chmod(0o600)
    with pytest.raises(model.CampaignError, match="unknown|keys|shape"):
        worker.read_status(
            tmp_path,
            run_id="unit-run",
            campaign_sha256="a" * 64,
            job=job,
        )

    status_path.unlink()
    outside = tmp_path / "outside.json"
    outside.write_text("{}\n", encoding="utf-8")
    status_path.symlink_to(outside)
    with pytest.raises(model.CampaignError, match="symlink|file|unsafe"):
        worker.read_status(
            tmp_path,
            run_id="unit-run",
            campaign_sha256="a" * 64,
            job=job,
        )


class _FakeProcess:
    def __init__(self, *, exit_code: int = 0, time_out: bool = False) -> None:
        self.pid = 5000
        self.returncode: int | None = None
        self._exit_code = exit_code
        self._time_out = time_out
        self.wait_calls = 0

    def wait(self, timeout: float | None = None) -> int:
        self.wait_calls += 1
        if self._time_out and self.wait_calls == 1:
            raise subprocess.TimeoutExpired(["unit"], timeout)
        self.returncode = self._exit_code
        return self._exit_code


def _child_identity() -> worker.ProcessIdentity:
    return worker.ProcessIdentity(
        pid=5000,
        pgid=5000,
        process_start_ticks=101,
        boot_id="12345678-1234-1234-1234-123456789abc",
    )


def test_child_execution_records_identity_and_private_logs(tmp_path: Path) -> None:
    process = _FakeProcess()
    popen_calls: list[tuple[tuple[str, ...], dict[str, object]]] = []
    started: list[worker.ProcessIdentity] = []

    def popen(argv: list[str], **kwargs: object) -> _FakeProcess:
        popen_calls.append((tuple(argv), kwargs))
        return process

    outcome = worker.execute_child(
        ["/env/bin/python", "bench.py"],
        cwd=tmp_path,
        environment={"PATH": "/usr/bin"},
        stdout_path=tmp_path / "stdout.log",
        stderr_path=tmp_path / "stderr.log",
        timeout_seconds=10,
        popen_factory=popen,
        process_observer=lambda _pid: _child_identity(),
        child_started=started.append,
        now_provider=iter(["2026-08-31T12:00:00Z", "2026-08-31T12:00:01Z"]).__next__,
    )

    assert outcome.state == "succeeded"
    assert outcome.exit_code == 0
    assert outcome.child == _child_identity()
    assert outcome.error is None
    assert started == [_child_identity()]
    assert popen_calls[0][0] == ("/env/bin/python", "bench.py")
    assert popen_calls[0][1]["start_new_session"] is True
    assert stat.S_IMODE((tmp_path / "stdout.log").stat().st_mode) == 0o600
    assert stat.S_IMODE((tmp_path / "stderr.log").stat().st_mode) == 0o600


def test_child_execution_terminalizes_startup_and_timeout_failures(tmp_path: Path) -> None:
    startup = worker.execute_child(
        ["unit"],
        cwd=tmp_path,
        environment={},
        stdout_path=tmp_path / "startup.stdout.log",
        stderr_path=tmp_path / "startup.stderr.log",
        timeout_seconds=10,
        popen_factory=lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError("boom")),
        process_observer=lambda _pid: None,
        child_started=lambda _identity: None,
        now_provider=iter(["2026-08-31T12:00:00Z", "2026-08-31T12:00:01Z"]).__next__,
    )
    assert startup.state == "failed"
    assert startup.child is None
    assert "OSError" in (startup.error or "")

    process = _FakeProcess(exit_code=-signal.SIGTERM, time_out=True)
    signals: list[tuple[int, int]] = []
    timed_out = worker.execute_child(
        ["unit"],
        cwd=tmp_path,
        environment={},
        stdout_path=tmp_path / "timeout.stdout.log",
        stderr_path=tmp_path / "timeout.stderr.log",
        timeout_seconds=10,
        popen_factory=lambda *_args, **_kwargs: process,
        process_observer=lambda _pid: _child_identity(),
        child_started=lambda _identity: None,
        killpg=lambda pgid, sig: signals.append((pgid, sig)),
        now_provider=iter(["2026-08-31T12:00:00Z", "2026-08-31T12:00:11Z"]).__next__,
    )
    assert timed_out.state == "timed_out"
    assert timed_out.exit_code == -signal.SIGTERM
    assert signals == [(5000, signal.SIGTERM)]


def test_child_execution_terminalizes_callback_and_signal_interruptions(
    tmp_path: Path,
) -> None:
    process = _FakeProcess(exit_code=-signal.SIGTERM)
    signals: list[tuple[int, int]] = []
    callback_failure = worker.execute_child(
        ["unit"],
        cwd=tmp_path,
        environment={},
        stdout_path=tmp_path / "callback.stdout.log",
        stderr_path=tmp_path / "callback.stderr.log",
        timeout_seconds=10,
        popen_factory=lambda *_args, **_kwargs: process,
        process_observer=lambda _pid: _child_identity(),
        child_started=lambda _identity: (_ for _ in ()).throw(RuntimeError("status write")),
        killpg=lambda pgid, sig: signals.append((pgid, sig)),
        now_provider=iter(["2026-08-31T12:00:00Z", "2026-08-31T12:00:01Z"]).__next__,
    )
    assert callback_failure.state == "failed"
    assert "status write" in (callback_failure.error or "")
    assert signals == [(5000, signal.SIGTERM)]

    handlers: dict[int, object] = {}

    def registrar(signum: int, handler: object) -> object:
        prior = handlers.get(signum, f"old-{signum}")
        handlers[signum] = handler
        return prior

    interrupted_process = _FakeProcess(exit_code=-signal.SIGTERM)
    interrupted_waits = 0

    def wait_and_signal(timeout: float | None = None) -> int:
        nonlocal interrupted_waits
        del timeout
        interrupted_waits += 1
        if interrupted_waits > 1:
            interrupted_process.returncode = -signal.SIGTERM
            return -signal.SIGTERM
        handler = handlers[signal.SIGTERM]
        assert callable(handler)
        handler(signal.SIGTERM, None)
        raise AssertionError("signal handler must interrupt the wait")

    interrupted_process.wait = wait_and_signal  # type: ignore[method-assign]
    interrupted = worker.execute_child(
        ["unit"],
        cwd=tmp_path,
        environment={},
        stdout_path=tmp_path / "signal.stdout.log",
        stderr_path=tmp_path / "signal.stderr.log",
        timeout_seconds=10,
        popen_factory=lambda *_args, **_kwargs: interrupted_process,
        process_observer=lambda _pid: _child_identity(),
        child_started=lambda _identity: None,
        signal_registrar=registrar,
        killpg=lambda _pgid, _sig: None,
        now_provider=iter(["2026-08-31T12:00:00Z", "2026-08-31T12:00:01Z"]).__next__,
    )
    assert interrupted.state == "interrupted"
    assert interrupted.signal_number == signal.SIGTERM


def test_tmux_output_parsing_is_bounded_exact_and_duplicate_safe() -> None:
    text = (
        "gffbase-abc-cluster\tscale-t01\t%7\t4321\t0\ngffbase-abc-cluster\tscale-t04\t%8\t5000\t1\n"
    )
    panes = worker.parse_tmux_panes(text)
    assert [(pane.pane_id, pane.pane_dead) for pane in panes] == [
        ("%7", False),
        ("%8", True),
    ]
    assert (
        worker.parse_tmux_launch_output(
            "gffbase-abc-cluster\tscale-t01\t%7\t4321\t0\n",
            expected_session="gffbase-abc-cluster",
            expected_window="scale-t01",
        )
        == panes[0]
    )

    for forged in (
        text + "gffbase-abc-cluster\tscale-t08\t%7\t6000\t0\n",
        "gffbase-abc-cluster\tscale-t01\t%7\t4321\t0\n\n",
        "gffbase-abc-cluster\tscale-t01\t%7\t4321\t1\n",
    ):
        with pytest.raises(model.CampaignError):
            if forged.endswith("\t1\n") and forged.count("\n") == 1:
                worker.parse_tmux_launch_output(
                    forged,
                    expected_session="gffbase-abc-cluster",
                    expected_window="scale-t01",
                )
            else:
                worker.parse_tmux_panes(forged)


def test_tmux_kill_targets_only_the_exact_pane() -> None:
    assert worker.build_tmux_kill_pane_argv("%17", tmux_path="/usr/bin/tmux") == [
        "/usr/bin/tmux",
        "kill-pane",
        "-t",
        "%17",
    ]
    for pane_id in ("17", "%7;kill-session", "%", "%01\n"):
        with pytest.raises(model.CampaignError):
            worker.build_tmux_kill_pane_argv(pane_id, tmux_path="tmux")


def _launch_evidence(
    tmp_path: Path,
    *,
    launch: int = 1,
    resume: bool = False,
    pane_id: str = "%7",
    pane_pid: int = 4321,
) -> dict[str, object]:
    campaign_sha256 = "a" * 64
    worker_argv = [
        "/env/bin/python",
        "-I",
        str(model.ROOT / "benchmarks" / "cluster_campaign.py"),
        "_worker",
        "--campaign",
        str(tmp_path / "campaign.json"),
        "--worker-id",
        "scale-t01",
        "--campaign-sha256",
        campaign_sha256,
    ]
    if resume:
        worker_argv.append("--resume")
    pane = worker.PaneIdentity(
        session="gffbase-aaaaaaaaaaaa-cluster",
        window="scale-t01",
        pane_id=pane_id,
        pane_pid=pane_pid,
        pane_dead=False,
    )
    return worker.build_launch_evidence(
        run_id="unit-run",
        campaign_sha256=campaign_sha256,
        worker_id="scale-t01",
        phase="exploratory",
        cpus="0-9",
        launch=launch,
        resume=resume,
        launched_utc=f"2026-08-31T12:00:{launch - 1:02d}Z",
        pane=pane,
        worker_argv=worker_argv,
        cwd=tmp_path,
        first_window=True,
        tmux_path="/usr/bin/tmux",
        taskset_path="/usr/bin/taskset",
    )


def test_launch_evidence_is_closed_self_consistent_and_immutable(tmp_path: Path) -> None:
    evidence = _launch_evidence(tmp_path)
    assert evidence["schema_version"] == model.LAUNCH_SCHEMA
    assert (
        worker.validate_launch_evidence(
            evidence,
            run_id="unit-run",
            campaign_sha256="a" * 64,
            worker_id="scale-t01",
            launch=1,
        )
        == evidence
    )
    assert evidence["tmux_argv"] == worker.build_tmux_launch_argv(
        session="gffbase-aaaaaaaaaaaa-cluster",
        window="scale-t01",
        cpu_list="0-9",
        worker_argv=evidence["worker_argv"],
        cwd=tmp_path,
        first_window=True,
        tmux_path="/usr/bin/tmux",
        taskset_path="/usr/bin/taskset",
    )

    for key, replacement in (
        ("unexpected", True),
        ("cpus", "1-9"),
        ("resume", True),
    ):
        forged = copy.deepcopy(evidence)
        forged[key] = replacement
        with pytest.raises(model.CampaignError):
            worker.validate_launch_evidence(
                forged,
                run_id="unit-run",
                campaign_sha256="a" * 64,
                worker_id="scale-t01",
                launch=1,
            )


def test_launch_history_is_contiguous_private_and_exactly_searchable(tmp_path: Path) -> None:
    first = _launch_evidence(tmp_path)
    first_path = worker.persist_launch_evidence(tmp_path, first)
    assert first_path == tmp_path / "launches" / "scale-t01" / "0001.json"
    assert stat.S_IMODE(first_path.parent.stat().st_mode) == 0o700
    assert stat.S_IMODE(first_path.stat().st_mode) == 0o600

    second = _launch_evidence(
        tmp_path,
        launch=2,
        resume=True,
        pane_id="%8",
        pane_pid=5000,
    )
    worker.persist_launch_evidence(tmp_path, second)
    launches = worker.scan_launch_evidence(
        tmp_path,
        run_id="unit-run",
        campaign_sha256="a" * 64,
        worker_id="scale-t01",
    )
    assert [record["launch"] for record in launches] == [1, 2]
    assert worker.find_launch_evidence(launches, pane_id="%8", pane_pid=5000) == second
    assert worker.find_launch_evidence(launches, pane_id="%9", pane_pid=5000) is None

    (first_path.parent / "latest.json").write_text("{}\n", encoding="utf-8")
    with pytest.raises(model.CampaignError, match="unexpected|contiguous"):
        worker.scan_launch_evidence(
            tmp_path,
            run_id="unit-run",
            campaign_sha256="a" * 64,
            worker_id="scale-t01",
        )


def test_worker_waits_for_its_exact_controller_launch_record(tmp_path: Path) -> None:
    evidence = _launch_evidence(tmp_path)
    worker.persist_launch_evidence(tmp_path, evidence)
    assert (
        worker.wait_for_launch_evidence(
            tmp_path,
            run_id="unit-run",
            campaign_sha256="a" * 64,
            worker_id="scale-t01",
            pane_id="%7",
            pane_pid=4321,
            timeout_seconds=1,
        )
        == evidence
    )

    clock = iter([0.0, 0.0, 2.0])
    with pytest.raises(model.CampaignError, match="timed out|launch evidence"):
        worker.wait_for_launch_evidence(
            tmp_path,
            run_id="unit-run",
            campaign_sha256="a" * 64,
            worker_id="scale-t01",
            pane_id="%9",
            pane_pid=9999,
            timeout_seconds=1,
            monotonic=clock.__next__,
            sleeper=lambda _seconds: None,
        )


# ---------------------------------------------------------------------------
# In-progress database files
# ---------------------------------------------------------------------------
#
# `gffbase.ingest.from_file` builds into `<target>.gffbase-building.<pid>` and
# renames on success, so the target never exists half-written. DuckDB puts its
# own `.wal` / `.tmp` beside that temporary. `status` scans a scratch directory
# while its job is still running, so it meets those files:
#
#     campaign attempt scratch contains an unexpected entry:
#     'chess.duckdb.gffbase-building.4026403.wal'
#
# They are legitimate artifacts of the tool the campaign drives, and transient.
# The scan is taught the shape rather than the exact pid.


def _chess_spec() -> model.JobSpec:
    """A non-bridge job whose `result_key` is `chess` -- that key decides which
    database names the scratch scan will accept."""
    for job in _job_matrix():
        if job.kind != "bridge" and job.result_key == "chess":
            return job
    raise AssertionError("no non-bridge chess job in the matrix")


@pytest.mark.parametrize(
    "name",
    [
        "chess.duckdb.gffbase-building.4026403",
        "chess.duckdb.gffbase-building.4026403.wal",
        "chess.duckdb.gffbase-building.1.tmp",
    ],
)
def test_an_in_progress_database_is_expected_in_scratch(tmp_path, name) -> None:
    scratch = tmp_path / "scratch"
    scratch.mkdir(mode=0o700)
    (scratch / name).write_bytes(b"")
    (scratch / name).chmod(0o600)
    worker._scan_scratch(scratch, _chess_spec())


@pytest.mark.parametrize(
    "name",
    [
        "chess.duckdb.gffbase-building",  # no pid at all
        "chess.duckdb.gffbase-building.abc",  # pid must be digits
        "other.duckdb.gffbase-building.7",  # a database this job never builds
        "chess.duckdb.gffbase-building.7.stray",  # not a DuckDB sidecar
    ],
)
def test_a_lookalike_is_still_rejected(tmp_path, name) -> None:
    """The exemption is for a known shape, not for anything containing the
    word: a stray file in scratch is what the scan exists to catch."""
    scratch = tmp_path / "scratch"
    scratch.mkdir(mode=0o700)
    (scratch / name).write_bytes(b"")
    (scratch / name).chmod(0o600)
    with pytest.raises(model.CampaignError, match="unexpected entry"):
        worker._scan_scratch(scratch, _chess_spec())
