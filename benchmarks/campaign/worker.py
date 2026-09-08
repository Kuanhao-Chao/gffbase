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
"""Pure tmux launch and pane-identity contracts for campaign workers.

The tmux session is an operator convenience, not a liveness authority.  These
helpers capture and parse the exact pane and pane-shell identities that later
reconciliation binds to a worker process.
"""

from __future__ import annotations

import os
import re
import shlex
import signal as signal_module
import stat
import subprocess
import time
from collections.abc import Callable, Mapping, Sequence
from contextlib import ExitStack
from dataclasses import dataclass
from datetime import datetime, timezone
from functools import partial
from pathlib import Path
from typing import Any, cast

from . import model, safe_io

TMUX_PANE_FORMAT = "#{session_name}\t#{window_name}\t#{pane_id}\t#{pane_pid}\t#{pane_dead}"

_PANE_ID_RE = re.compile(r"%(?:0|[1-9][0-9]{0,9})\Z", re.ASCII)
_POSITIVE_INTEGER_RE = re.compile(r"[1-9][0-9]*\Z", re.ASCII)
_SHA256_RE = re.compile(r"[0-9a-f]{64}\Z", re.ASCII)
_HOST_RE = re.compile(r"[A-Za-z0-9](?:[A-Za-z0-9._-]{0,253}[A-Za-z0-9])?\Z", re.ASCII)
_BOOT_ID_RE = re.compile(
    r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\Z",
    re.ASCII,
)
_TIMESTAMP_RE = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}Z\Z", re.ASCII)
_MAX_PID = (1 << 31) - 1

_PANE_KEYS = {"session", "window", "pane_id", "pane_pid", "pane_dead"}
_PROCESS_KEYS = {"pid", "pgid", "process_start_ticks", "boot_id"}
_PATH_KEYS = {"attempt_dir", "stdout_log", "stderr_log", "result"}
_STATUS_IDENTITY_KEYS = {
    "schema_version",
    "run_id",
    "campaign_sha256",
    "job_id",
    "job_sha256",
    "worker_id",
    "state",
    "attempt",
}
_ACTIVE_STATUS_KEYS = _STATUS_IDENTITY_KEYS | {
    "started_utc",
    "host",
    "cpu_affinity",
    "pane",
    "worker",
    "child",
    "paths",
}
_TERMINAL_STATUS_KEYS = _ACTIVE_STATUS_KEYS | {"finished_utc", "result_sha256"}
_TERMINAL_STATES = {"succeeded", "failed", "timed_out", "interrupted"}
_ATTEMPT_RE = re.compile(r"[0-9]{4}\Z", re.ASCII)
_LAUNCH_FILE_RE = re.compile(r"([0-9]{4})\.json\Z", re.ASCII)
_SIGNATURE_SCRATCH_RE = re.compile(r"\.gffbase-signature-[A-Za-z0-9._-]+\Z", re.ASCII)
_RELEASED_LOCK_RE = re.compile(r"released-[0-9a-f]{32}\Z", re.ASCII)
_RETIRED_RE = re.compile(r"retired-[0-9a-f]{32}\Z", re.ASCII)
_DATABASE_SUFFIXES = ("", ".wal", ".tmp", "-journal", "-wal", "-shm")

_LAUNCH_KEYS = {
    "schema_version",
    "run_id",
    "campaign_sha256",
    "worker_id",
    "phase",
    "cpus",
    "launch",
    "resume",
    "launched_utc",
    "pane",
    "worker_argv",
    "tmux_argv",
    "cwd",
    "first_window",
    "tmux_path",
    "taskset_path",
}

ResultValidator = Callable[[object, int, Path], bool]


def _positive_int(value: object, label: str, *, maximum: int | None = None) -> int:
    if type(value) is not int or value < 1:
        raise model.CampaignError(f"{label} must be a positive integer")
    result = value
    if maximum is not None and result > maximum:
        raise model.CampaignError(f"{label} exceeds {maximum}")
    return result


def _validate_sha256(value: object, label: str) -> str:
    if type(value) is not str or _SHA256_RE.fullmatch(value) is None:
        raise model.CampaignError(f"{label} must be a lowercase SHA-256 digest")
    return value


def _validate_host(value: object, label: str = "host") -> str:
    if type(value) is not str or _HOST_RE.fullmatch(value) is None:
        raise model.CampaignError(f"{label} is not a bounded host identity")
    return value


def _validate_timestamp(value: object, label: str) -> datetime:
    if type(value) is not str or _TIMESTAMP_RE.fullmatch(value) is None:
        raise model.CampaignError(f"{label} must be a canonical UTC timestamp")
    try:
        parsed = datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ")
    except ValueError as exc:
        raise model.CampaignError(f"{label} must be a valid UTC timestamp") from exc
    if parsed.strftime("%Y-%m-%dT%H:%M:%SZ") != value:
        raise model.CampaignError(f"{label} must use canonical UTC spelling")
    return parsed


@dataclass(frozen=True, slots=True)
class PaneIdentity:
    """The exact tmux pane identity returned at launch or reconciliation."""

    session: str
    window: str
    pane_id: str
    pane_pid: int
    pane_dead: bool

    def __post_init__(self) -> None:
        model.validate_identifier(self.session, "tmux session")
        model.validate_identifier(self.window, "tmux window")
        if type(self.pane_id) is not str or _PANE_ID_RE.fullmatch(self.pane_id) is None:
            raise model.CampaignError(f"invalid tmux pane id: {self.pane_id!r}")
        _positive_int(self.pane_pid, "tmux pane pid", maximum=_MAX_PID)
        if type(self.pane_dead) is not bool:
            raise model.CampaignError("tmux pane dead flag must be a boolean")

    def to_dict(self) -> dict[str, object]:
        """Return the closed JSON representation used by campaign evidence."""

        return {
            "session": self.session,
            "window": self.window,
            "pane_id": self.pane_id,
            "pane_pid": self.pane_pid,
            "pane_dead": self.pane_dead,
        }

    @classmethod
    def from_dict(cls, value: object) -> PaneIdentity:
        """Construct an identity from its exact closed JSON shape."""

        record = model.require_exact_keys(value, _PANE_KEYS, "tmux pane identity")
        return cls(
            session=cast(str, record["session"]),
            window=cast(str, record["window"]),
            pane_id=cast(str, record["pane_id"]),
            pane_pid=cast(int, record["pane_pid"]),
            pane_dead=cast(bool, record["pane_dead"]),
        )


@dataclass(frozen=True, slots=True)
class ProcessIdentity:
    """A Linux process identity resistant to ordinary PID reuse."""

    pid: int
    pgid: int
    process_start_ticks: int
    boot_id: str

    def __post_init__(self) -> None:
        _positive_int(self.pid, "process pid", maximum=_MAX_PID)
        _positive_int(self.pgid, "process group id", maximum=_MAX_PID)
        _positive_int(self.process_start_ticks, "process start ticks")
        if type(self.boot_id) is not str or _BOOT_ID_RE.fullmatch(self.boot_id) is None:
            raise model.CampaignError("process boot_id must be a lowercase UUID")

    def to_dict(self) -> dict[str, object]:
        """Return the closed JSON representation used by status evidence."""

        return {
            "pid": self.pid,
            "pgid": self.pgid,
            "process_start_ticks": self.process_start_ticks,
            "boot_id": self.boot_id,
        }

    @classmethod
    def from_dict(cls, value: object) -> ProcessIdentity:
        """Construct an identity from its exact closed JSON shape."""

        record = model.require_exact_keys(value, _PROCESS_KEYS, "process identity")
        return cls(
            pid=cast(int, record["pid"]),
            pgid=cast(int, record["pgid"]),
            process_start_ticks=cast(int, record["process_start_ticks"]),
            boot_id=cast(str, record["boot_id"]),
        )


@dataclass(frozen=True, slots=True)
class AttemptSuccess:
    """One fully validated immutable successful attempt."""

    attempt: int
    path: Path
    result_sha256: str
    result: Mapping[str, object]


@dataclass(frozen=True, slots=True)
class AttemptResult:
    """One fully validated immutable terminal attempt result."""

    attempt: int
    path: Path
    result_sha256: str
    result: Mapping[str, object]
    successful: bool


@dataclass(frozen=True, slots=True)
class AttemptInventory:
    """The exact contiguous attempt history for one job."""

    attempts: tuple[int, ...]
    successes: tuple[AttemptSuccess, ...]
    results: tuple[AttemptResult, ...] = ()

    @property
    def next_attempt(self) -> int:
        value = self.attempts[-1] + 1 if self.attempts else 1
        if value > 9999:
            raise model.CampaignError("campaign job exhausted its 9999-attempt namespace")
        return value


@dataclass(frozen=True, slots=True)
class ReconcileDecision:
    """A controller action derived from status plus immutable attempts."""

    action: str
    next_attempt: int | None
    success: AttemptSuccess | None = None


@dataclass(frozen=True, slots=True)
class ChildOutcome:
    """Terminal evidence from the benchmark subprocess boundary."""

    state: str
    started_utc: str
    finished_utc: str
    child: ProcessIdentity | None
    exit_code: int | None
    error: str | None
    signal_number: int | None


class _ChildInterrupted(BaseException):
    def __init__(self, signum: int) -> None:
        self.signum = signum


def _validate_executable(value: str, label: str) -> str:
    if type(value) is not str or not value:
        raise model.CampaignError(f"{label} must be a non-empty string")
    if "\x00" in value or any(ord(char) < 32 or ord(char) == 127 for char in value):
        raise model.CampaignError(f"{label} contains a control character")
    if "/" in value:
        path = safe_io.lexical_absolute(value, label)
        if str(path) != value:
            raise model.CampaignError(f"{label} path must be absolute and canonical")
    return value


def _validate_worker_argv(value: Sequence[str]) -> list[str]:
    if isinstance(value, (str, bytes)) or not value:
        raise model.CampaignError("worker argv must be a non-empty string sequence")
    result = list(value)
    for entry in result:
        if type(entry) is not str or not entry:
            raise model.CampaignError("worker argv entries must be non-empty strings")
        if "\x00" in entry or any(ord(char) < 32 or ord(char) == 127 for char in entry):
            raise model.CampaignError("worker argv entries may not contain control characters")
    return result


def build_tmux_launch_argv(
    *,
    session: str,
    window: str,
    cpu_list: str,
    worker_argv: Sequence[str],
    cwd: Path,
    first_window: bool,
    tmux_path: str,
    taskset_path: str,
) -> list[str]:
    """Build one detached tmux launch with machine-readable pane output."""

    model.validate_identifier(session, "tmux session")
    model.validate_identifier(window, "tmux window")
    model.parse_cpu_list(cpu_list)
    if type(first_window) is not bool:
        raise model.CampaignError("first_window must be a boolean")
    tmux = _validate_executable(tmux_path, "tmux executable")
    taskset = _validate_executable(taskset_path, "taskset executable")
    command = shlex.join(
        ["exec", taskset, "--cpu-list", cpu_list, *_validate_worker_argv(worker_argv)]
    )
    common = [
        "-d",
        "-P",
        "-F",
        TMUX_PANE_FORMAT,
        "-n",
        window,
        "-c",
        str(safe_io.lexical_absolute(cwd, "tmux working directory")),
    ]
    if first_window:
        return [tmux, "new-session", *common, "-s", session, command]
    return [tmux, "new-window", *common, "-t", f"{session}:", command]


def parse_tmux_pane(line: str) -> PaneIdentity:
    """Parse one exact ``TMUX_PANE_FORMAT`` record without open fields."""

    if type(line) is not str or not line:
        raise model.CampaignError("tmux pane record must be a non-empty string")
    if "\n" in line or "\r" in line or "\x00" in line:
        raise model.CampaignError("tmux pane record contains a line/control delimiter")
    fields = line.split("\t")
    if len(fields) != 5:
        raise model.CampaignError("tmux pane record must contain exactly five fields")
    session, window, pane_id, pane_pid_text, pane_dead_text = fields
    model.validate_identifier(session, "tmux session")
    model.validate_identifier(window, "tmux window")
    if _PANE_ID_RE.fullmatch(pane_id) is None:
        raise model.CampaignError(f"invalid tmux pane id: {pane_id!r}")
    if _POSITIVE_INTEGER_RE.fullmatch(pane_pid_text) is None:
        raise model.CampaignError(f"invalid tmux pane pid: {pane_pid_text!r}")
    pane_pid = int(pane_pid_text)
    if pane_pid > _MAX_PID:
        raise model.CampaignError(f"tmux pane pid exceeds {_MAX_PID}: {pane_pid!r}")
    if pane_dead_text not in {"0", "1"}:
        raise model.CampaignError(f"invalid tmux pane dead flag: {pane_dead_text!r}")
    return PaneIdentity(
        session=session,
        window=window,
        pane_id=pane_id,
        pane_pid=pane_pid,
        pane_dead=pane_dead_text == "1",
    )


def build_tmux_list_panes_argv(session: str, *, tmux_path: str) -> list[str]:
    """Build the exact pane-identity query for one campaign session."""

    model.validate_identifier(session, "tmux session")
    tmux = _validate_executable(tmux_path, "tmux executable")
    return [tmux, "list-panes", "-t", f"{session}:", "-F", TMUX_PANE_FORMAT]


def parse_tmux_panes(output: str) -> tuple[PaneIdentity, ...]:
    """Parse bounded exact tmux pane output, rejecting duplicate identities."""

    if type(output) is not str:
        raise model.CampaignError("tmux pane output must be text")
    try:
        encoded_size = len(output.encode("utf-8", errors="strict"))
    except UnicodeError as exc:
        raise model.CampaignError("tmux pane output is not valid UTF-8 text") from exc
    if encoded_size > 65536:
        raise model.CampaignError("tmux pane output exceeds 65536 encoded bytes")
    if not output:
        return ()
    if "\r" in output or not output.endswith("\n"):
        raise model.CampaignError("tmux pane output must use complete LF-terminated records")
    lines = output[:-1].split("\n")
    if not lines or any(not line for line in lines) or len(lines) > 128:
        raise model.CampaignError("tmux pane output has an invalid record count")
    panes = tuple(parse_tmux_pane(line) for line in lines)
    pane_ids = [pane.pane_id for pane in panes]
    windows = [(pane.session, pane.window) for pane in panes]
    if len(set(pane_ids)) != len(pane_ids):
        raise model.CampaignError("tmux pane output contains a duplicate pane id")
    if len(set(windows)) != len(windows):
        raise model.CampaignError("campaign tmux output contains multiple panes in one window")
    return panes


def parse_tmux_launch_output(
    output: str,
    *,
    expected_session: str,
    expected_window: str,
) -> PaneIdentity:
    """Require exactly one live pane matching the requested launch target."""

    model.validate_identifier(expected_session, "tmux session")
    model.validate_identifier(expected_window, "tmux window")
    panes = parse_tmux_panes(output)
    if len(panes) != 1:
        raise model.CampaignError(f"tmux launch returned {len(panes)} pane records")
    pane = panes[0]
    if pane.session != expected_session or pane.window != expected_window:
        raise model.CampaignError("tmux launch pane differs from the requested session/window")
    if pane.pane_dead:
        raise model.CampaignError("tmux launch returned an already-dead pane")
    return pane


def build_tmux_kill_pane_argv(pane_id: str, *, tmux_path: str) -> list[str]:
    """Build an exact cleanup command for one captured pane, never a session."""

    if type(pane_id) is not str or _PANE_ID_RE.fullmatch(pane_id) is None:
        raise model.CampaignError(f"invalid tmux pane id: {pane_id!r}")
    tmux = _validate_executable(tmux_path, "tmux executable")
    return [tmux, "kill-pane", "-t", pane_id]


def _validate_internal_worker_argv(
    value: object,
    *,
    worker_id: str,
    campaign_sha256: str,
    resume: bool,
) -> list[str]:
    if not isinstance(value, Sequence):
        raise model.CampaignError("launch worker argv must be a sequence")
    argv = _validate_worker_argv(cast(Sequence[str], value))
    expected_length = 11 if resume else 10
    if len(argv) != expected_length:
        raise model.CampaignError("launch worker argv has an invalid closed shape")
    _validate_executable(argv[0], "worker interpreter")
    expected_prefix = [
        "-I",
        str(model.ROOT / "benchmarks" / "cluster_campaign.py"),
        "_worker",
        "--campaign",
    ]
    if argv[1:5] != expected_prefix:
        raise model.CampaignError("launch worker argv has an invalid isolated entry point")
    campaign_path = safe_io.lexical_absolute(argv[5], "worker campaign path")
    if argv[5] != str(campaign_path) or campaign_path.name != "campaign.json":
        raise model.CampaignError("launch worker argv has an invalid campaign path")
    expected_tail = [
        "--worker-id",
        worker_id,
        "--campaign-sha256",
        campaign_sha256,
    ]
    if argv[6:10] != expected_tail:
        raise model.CampaignError("launch worker argv differs from its bound identities")
    if resume and argv[10:] != ["--resume"]:
        raise model.CampaignError("resumed launch worker argv must end with --resume")
    return argv


def _expected_worker_phase_and_cpus(worker_id: str) -> tuple[str, str]:
    topology = model.topology()
    lanes = cast(list[dict[str, object]], topology["lanes"])
    for lane in lanes:
        if lane["worker_id"] == worker_id:
            return "exploratory", cast(str, lane["cpus"])
    canonical = cast(dict[str, object], topology["canonical"])
    if worker_id == canonical["worker_id"]:
        return "canonical", cast(str, canonical["cpus"])
    raise model.CampaignError(f"unknown campaign worker: {worker_id!r}")


def validate_launch_evidence(
    value: object,
    *,
    run_id: str,
    campaign_sha256: str,
    worker_id: str,
    launch: int,
) -> dict[str, object]:
    """Validate one immutable, internally reconstructable tmux launch record."""

    model.validate_identifier(run_id, "run")
    digest = _validate_sha256(campaign_sha256, "campaign_sha256")
    model.validate_identifier(worker_id, "worker")
    launch_number = _positive_int(launch, "launch number", maximum=9999)
    record = model.require_exact_keys(value, _LAUNCH_KEYS, "campaign launch evidence")
    expected_identity: dict[str, object] = {
        "schema_version": model.LAUNCH_SCHEMA,
        "run_id": run_id,
        "campaign_sha256": digest,
        "worker_id": worker_id,
        "launch": launch_number,
    }
    for key, expected in expected_identity.items():
        if type(record[key]) is not type(expected) or record[key] != expected:
            raise model.CampaignError(f"campaign launch identity {key!r} differs")
    expected_phase, expected_cpus = _expected_worker_phase_and_cpus(worker_id)
    if record["phase"] != expected_phase or record["cpus"] != expected_cpus:
        raise model.CampaignError("campaign launch differs from the bound worker topology")
    model.parse_cpu_list(cast(str, record["cpus"]))
    if type(record["resume"]) is not bool or type(record["first_window"]) is not bool:
        raise model.CampaignError("campaign launch boolean fields have invalid types")
    _validate_timestamp(record["launched_utc"], "campaign launch launched_utc")
    pane = PaneIdentity.from_dict(record["pane"])
    expected_session = f"gffbase-{digest[:12]}-cluster"
    if expected_phase == "canonical":
        expected_session += "-canonical"
    if pane.session != expected_session or pane.window != worker_id or pane.pane_dead:
        raise model.CampaignError("campaign launch pane differs from its worker/session")
    worker_argv = _validate_internal_worker_argv(
        record["worker_argv"],
        worker_id=worker_id,
        campaign_sha256=digest,
        resume=record["resume"],
    )
    if type(record["cwd"]) is not str:
        raise model.CampaignError("campaign launch cwd must be a string")
    cwd = safe_io.lexical_absolute(record["cwd"], "campaign launch cwd")
    if str(cwd) != record["cwd"]:
        raise model.CampaignError("campaign launch cwd must use canonical absolute spelling")
    tmux = _validate_executable(cast(str, record["tmux_path"]), "tmux executable")
    taskset = _validate_executable(cast(str, record["taskset_path"]), "taskset executable")
    expected_tmux_argv = build_tmux_launch_argv(
        session=expected_session,
        window=worker_id,
        cpu_list=expected_cpus,
        worker_argv=worker_argv,
        cwd=cwd,
        first_window=record["first_window"],
        tmux_path=tmux,
        taskset_path=taskset,
    )
    if record["tmux_argv"] != expected_tmux_argv:
        raise model.CampaignError("campaign launch tmux argv is not reconstructable")
    return record


def build_launch_evidence(
    *,
    run_id: str,
    campaign_sha256: str,
    worker_id: str,
    phase: str,
    cpus: str,
    launch: int,
    resume: bool,
    launched_utc: str,
    pane: PaneIdentity,
    worker_argv: Sequence[str],
    cwd: os.PathLike[str] | str,
    first_window: bool,
    tmux_path: str,
    taskset_path: str,
) -> dict[str, object]:
    """Build and self-validate immutable evidence for one launched pane."""

    root = safe_io.lexical_absolute(cwd, "campaign launch cwd")
    tmux_argv = build_tmux_launch_argv(
        session=pane.session,
        window=worker_id,
        cpu_list=cpus,
        worker_argv=worker_argv,
        cwd=root,
        first_window=first_window,
        tmux_path=tmux_path,
        taskset_path=taskset_path,
    )
    value: dict[str, object] = {
        "schema_version": model.LAUNCH_SCHEMA,
        "run_id": run_id,
        "campaign_sha256": campaign_sha256,
        "worker_id": worker_id,
        "phase": phase,
        "cpus": cpus,
        "launch": launch,
        "resume": resume,
        "launched_utc": launched_utc,
        "pane": pane.to_dict(),
        "worker_argv": list(worker_argv),
        "tmux_argv": tmux_argv,
        "cwd": str(root),
        "first_window": first_window,
        "tmux_path": tmux_path,
        "taskset_path": taskset_path,
    }
    return validate_launch_evidence(
        value,
        run_id=run_id,
        campaign_sha256=campaign_sha256,
        worker_id=worker_id,
        launch=launch,
    )


def _job_spec(job: model.JobSpec | Mapping[str, object]) -> model.JobSpec:
    spec = job if isinstance(job, model.JobSpec) else model.JobSpec.from_dict(job)
    model.job_digest(spec)
    return spec


def pending_status(
    *,
    run_id: str,
    campaign_sha256: str,
    job: model.JobSpec | Mapping[str, object],
) -> dict[str, object]:
    """Return the sole valid synthesized pending status for one bound job."""

    spec = _job_spec(job)
    model.validate_identifier(run_id, "run")
    digest = _validate_sha256(campaign_sha256, "campaign_sha256")
    return {
        "schema_version": model.STATUS_SCHEMA,
        "run_id": run_id,
        "campaign_sha256": digest,
        "job_id": spec.job_id,
        "job_sha256": spec.job_sha256,
        "worker_id": spec.worker_id,
        "state": "pending",
        "attempt": 0,
    }


def _validate_status_paths(value: object, spec: model.JobSpec, attempt: int) -> None:
    paths = model.require_exact_keys(value, _PATH_KEYS, "campaign status paths")
    base = f"jobs/{spec.job_id}/attempts/{attempt:04d}"
    expected = {
        "attempt_dir": base,
        "stdout_log": f"{base}/stdout.log",
        "stderr_log": f"{base}/stderr.log",
        "result": f"{base}/result.json",
    }
    for key, expected_value in expected.items():
        if type(paths[key]) is not str or paths[key] != expected_value:
            raise model.CampaignError(f"campaign status path {key!r} must be {expected_value!r}")


def validate_status(
    value: object,
    *,
    run_id: str,
    campaign_sha256: str,
    job: model.JobSpec | Mapping[str, object],
) -> dict[str, object]:
    """Validate one exact state-discriminated ``campaign-status-v2`` record."""

    spec = _job_spec(job)
    model.validate_identifier(run_id, "run")
    digest = _validate_sha256(campaign_sha256, "campaign_sha256")
    if not isinstance(value, Mapping):
        raise model.CampaignError("campaign status must be an object")
    state = value.get("state")
    if state == "pending":
        record = model.require_exact_keys(value, _STATUS_IDENTITY_KEYS, "pending status")
    elif state == "running":
        record = model.require_exact_keys(value, _ACTIVE_STATUS_KEYS, "running status")
    elif state in _TERMINAL_STATES:
        record = model.require_exact_keys(value, _TERMINAL_STATUS_KEYS, "terminal status")
    else:
        raise model.CampaignError(f"campaign status has invalid state: {state!r}")
    expected_identity: dict[str, object] = {
        "schema_version": model.STATUS_SCHEMA,
        "run_id": run_id,
        "campaign_sha256": digest,
        "job_id": spec.job_id,
        "job_sha256": spec.job_sha256,
        "worker_id": spec.worker_id,
    }
    for key, expected in expected_identity.items():
        if type(record[key]) is not type(expected) or record[key] != expected:
            raise model.CampaignError(
                f"campaign status identity {key!r} differs from the bound job"
            )
    if state == "pending":
        if type(record["attempt"]) is not int or record["attempt"] != 0:
            raise model.CampaignError("pending status attempt must be zero")
        return record

    attempt = _positive_int(record["attempt"], "campaign status attempt", maximum=9999)
    started = _validate_timestamp(record["started_utc"], "campaign status started_utc")
    _validate_host(record["host"], "campaign status host")
    affinity = record["cpu_affinity"]
    if type(affinity) is not list or affinity != list(model.parse_cpu_list(spec.cpus)):
        raise model.CampaignError("campaign status affinity differs from the bound CPU lane")
    pane = PaneIdentity.from_dict(record["pane"])
    expected_session = f"gffbase-{digest[:12]}-cluster"
    if spec.phase == "canonical":
        expected_session += "-canonical"
    if pane.session != expected_session or pane.window != spec.worker_id:
        raise model.CampaignError("campaign status pane differs from the bound worker")
    worker_identity = ProcessIdentity.from_dict(record["worker"])
    if worker_identity.pid != pane.pane_pid:
        raise model.CampaignError("campaign worker PID differs from the tmux pane PID")
    child_value = record["child"]
    child_identity = None if child_value is None else ProcessIdentity.from_dict(child_value)
    if child_identity is not None and child_identity.boot_id != worker_identity.boot_id:
        raise model.CampaignError("campaign worker and child boot identities differ")
    _validate_status_paths(record["paths"], spec, attempt)
    if state in _TERMINAL_STATES:
        finished = _validate_timestamp(record["finished_utc"], "campaign status finished_utc")
        if finished < started:
            raise model.CampaignError("campaign status finishes before it starts")
        _validate_sha256(record["result_sha256"], "campaign status result_sha256")
    return record


def classify_running_status(
    status: Mapping[str, object],
    *,
    local_host: str,
    panes: Sequence[PaneIdentity],
    process_observer: Callable[[int], ProcessIdentity | None],
) -> str:
    """Classify exact running evidence as ``live``, ``stale``, or ``foreign``."""

    if not isinstance(status, Mapping) or status.get("state") != "running":
        raise model.CampaignError("running liveness requires a validated running status")
    _validate_host(local_host, "local host")
    recorded_host = _validate_host(status.get("host"), "campaign status host")
    if recorded_host != local_host:
        return "foreign"
    if isinstance(panes, (str, bytes)) or not isinstance(panes, Sequence):
        raise model.CampaignError("tmux panes must be a sequence")
    if not callable(process_observer):
        raise model.CampaignError("process observer must be callable")
    recorded_pane = PaneIdentity.from_dict(status.get("pane"))
    matches: list[PaneIdentity] = []
    for pane in panes:
        if not isinstance(pane, PaneIdentity):
            raise model.CampaignError("tmux pane observation has an invalid type")
        if (
            pane.session,
            pane.window,
            pane.pane_id,
        ) == (
            recorded_pane.session,
            recorded_pane.window,
            recorded_pane.pane_id,
        ):
            matches.append(pane)
    if len(matches) != 1 or matches[0] != recorded_pane or recorded_pane.pane_dead:
        return "stale"
    recorded_worker = ProcessIdentity.from_dict(status.get("worker"))
    observed_worker = process_observer(recorded_worker.pid)
    if observed_worker is not None and not isinstance(observed_worker, ProcessIdentity):
        raise model.CampaignError("process observer returned an invalid worker identity")
    if observed_worker != recorded_worker:
        return "stale"
    child_value: Any = status.get("child")
    if child_value is not None:
        recorded_child = ProcessIdentity.from_dict(child_value)
        observed_child = process_observer(recorded_child.pid)
        if observed_child is not None and not isinstance(observed_child, ProcessIdentity):
            raise model.CampaignError("process observer returned an invalid child identity")
        if observed_child != recorded_child:
            return "stale"
    return "live"


def _require_private_directory(path: Path, label: str) -> None:
    inspected = safe_io.inspect_path(path, require_kind="directory")
    item = os.lstat(inspected)
    if stat.S_IMODE(item.st_mode) != 0o700:
        raise model.CampaignError(f"{label} must be a private mode-0700 directory")


def _directory_names(path: Path, label: str) -> set[str]:
    with safe_io._open_directory(path) as descriptor:
        before = os.fstat(descriptor)
        try:
            names = {entry.name for entry in os.scandir(descriptor)}
        except OSError as exc:
            raise model.CampaignError(f"cannot enumerate {label}: {exc}") from exc
        after = os.fstat(descriptor)
        if any(
            getattr(after, field) != getattr(before, field)
            for field in ("st_dev", "st_ino", "st_mode", "st_mtime_ns", "st_ctime_ns")
        ):
            raise model.CampaignError(f"{label} changed during enumeration")
    return names


def _existing_directory(path: Path, label: str) -> bool:
    try:
        item = os.lstat(path)
    except FileNotFoundError:
        return False
    if not stat.S_ISDIR(item.st_mode):
        kind = "symlink" if stat.S_ISLNK(item.st_mode) else "non-directory"
        raise model.CampaignError(f"{label} is an unsafe {kind}: {path}")
    _require_private_directory(path, label)
    return True


def _attempt_numbers(attempts_dir: Path) -> tuple[int, ...]:
    names = _directory_names(attempts_dir, "campaign attempts")
    numbers: list[int] = []
    for name in sorted(names):
        if _ATTEMPT_RE.fullmatch(name) is None:
            raise model.CampaignError(f"campaign attempts contain an unexpected entry: {name!r}")
        value = int(name)
        if not 1 <= value <= 9999:
            raise model.CampaignError(f"campaign attempt is outside 0001..9999: {name!r}")
        numbers.append(value)
    expected = list(range(1, len(numbers) + 1))
    if numbers != expected:
        raise model.CampaignError(
            f"campaign attempts must be contiguous from 0001; found {numbers!r}"
        )
    entries = safe_io.exact_directory_scan(
        attempts_dir, {f"{attempt:04d}": "directory" for attempt in numbers}
    )
    for name, item in entries.items():
        if stat.S_IMODE(item.st_mode) != 0o700:
            raise model.CampaignError(f"campaign attempt {name!r} must use mode 0700")
    return tuple(numbers)


def _scan_scratch(path: Path, spec: model.JobSpec) -> None:
    names = _directory_names(path, "campaign attempt scratch")
    expected: dict[str, str] = {}
    database_bases = (
        ("bridge.duckdb",)
        if spec.kind == "bridge"
        else (f"{spec.result_key}.duckdb", f"{spec.result_key}_legacy.sqlite")
    )
    allowed_files = {f"{base}{suffix}" for base in database_bases for suffix in _DATABASE_SUFFIXES}
    if spec.kind != "bridge":
        allowed_files.add("06_mega.json")
    for name in names:
        if name in allowed_files:
            expected[name] = "file"
        elif _SIGNATURE_SCRATCH_RE.fullmatch(name) is not None:
            expected[name] = "directory"
        else:
            raise model.CampaignError(
                f"campaign attempt scratch contains an unexpected entry: {name!r}"
            )
    entries = safe_io.exact_directory_scan(path, expected)
    for name, item in entries.items():
        if expected[name] == "file" and stat.S_IMODE(item.st_mode) != 0o600:
            raise model.CampaignError(f"campaign scratch file {name!r} must use mode 0600")
        if expected[name] == "directory":
            nested = path / name
            _require_private_directory(nested, "signature scratch directory")
            nested_names = _directory_names(nested, "signature scratch directory")
            nested_allowed = {f"normal.sqlite{suffix}" for suffix in _DATABASE_SUFFIXES}
            if not nested_names <= nested_allowed:
                raise model.CampaignError(
                    "signature scratch contains unexpected entries: "
                    f"{sorted(nested_names - nested_allowed)!r}"
                )
            nested_entries = safe_io.exact_directory_scan(
                nested, {nested_name: "file" for nested_name in nested_names}
            )
            if any(stat.S_IMODE(value.st_mode) != 0o600 for value in nested_entries.values()):
                raise model.CampaignError("signature scratch files must use mode 0600")


def _scan_attempt_directory(path: Path, spec: model.JobSpec) -> None:
    _require_private_directory(path, "campaign attempt")
    names = _directory_names(path, "campaign attempt")
    allowed = {
        "scratch": "directory",
        "stdout.log": "file",
        "stderr.log": "file",
        "raw.json": "file",
        "result.json": "file",
    }
    unknown = names - set(allowed)
    if unknown:
        raise model.CampaignError(
            f"campaign attempt contains unexpected entries: {sorted(unknown)!r}"
        )
    entries = safe_io.exact_directory_scan(path, {name: allowed[name] for name in names})
    for name, item in entries.items():
        expected_mode = 0o700 if allowed[name] == "directory" else 0o600
        if stat.S_IMODE(item.st_mode) != expected_mode:
            raise model.CampaignError(
                f"campaign attempt entry {name!r} must use mode {expected_mode:04o}"
            )
    if "scratch" in names:
        _scan_scratch(path / "scratch", spec)


def allocate_attempt(
    campaign_dir: os.PathLike[str] | str,
    job: model.JobSpec | Mapping[str, object],
) -> Path:
    """Durably allocate the next contiguous private attempt directory."""

    spec = _job_spec(job)
    root = safe_io.lexical_absolute(campaign_dir, "campaign directory")
    safe_io.inspect_path(root, require_kind="directory")
    jobs_dir = safe_io.create_private_directory(root / "jobs", exist_ok=True)
    job_dir = safe_io.create_private_directory(jobs_dir / spec.job_id, exist_ok=True)
    attempts_dir = safe_io.create_private_directory(job_dir / "attempts", exist_ok=True)
    attempts = _attempt_numbers(attempts_dir)
    next_attempt = attempts[-1] + 1 if attempts else 1
    if next_attempt > 9999:
        raise model.CampaignError("campaign job exhausted its 9999-attempt namespace")
    attempt_dir = safe_io.attempt_directory(root, spec.job_id, next_attempt)
    safe_io.create_private_directory(attempt_dir)
    safe_io.create_private_directory(attempt_dir / "scratch")
    return attempt_dir


def scan_attempts(
    campaign_dir: os.PathLike[str] | str,
    job: model.JobSpec | Mapping[str, object],
    *,
    result_validator: ResultValidator,
) -> AttemptInventory:
    """Scan one exact contiguous attempt history without following aliases."""

    spec = _job_spec(job)
    if not callable(result_validator):
        raise model.CampaignError("attempt result validator must be callable")
    root = safe_io.lexical_absolute(campaign_dir, "campaign directory")
    safe_io.inspect_path(root, require_kind="directory")
    jobs_dir = root / "jobs"
    if not _existing_directory(jobs_dir, "campaign jobs directory"):
        return AttemptInventory((), (), ())
    job_dir = safe_io.job_directory(root, spec.job_id)
    if not _existing_directory(job_dir, "campaign job directory"):
        return AttemptInventory((), (), ())
    attempts_dir = job_dir / "attempts"
    if not _existing_directory(attempts_dir, "campaign attempts directory"):
        return AttemptInventory((), (), ())
    attempts = _attempt_numbers(attempts_dir)
    successes: list[AttemptSuccess] = []
    results: list[AttemptResult] = []
    for attempt in attempts:
        attempt_dir = safe_io.attempt_directory(root, spec.job_id, attempt)
        _scan_attempt_directory(attempt_dir, spec)
        result_path = attempt_dir / "result.json"
        if not result_path.exists():
            continue
        result = safe_io.strict_json_load(result_path)
        if not isinstance(result, Mapping):
            raise model.CampaignError(f"attempt {attempt:04d} result must be an object")
        accepted = result_validator(result, attempt, result_path)
        if type(accepted) is not bool:
            raise model.CampaignError("attempt result validator must return a boolean")
        result_sha256 = safe_io.sha256_regular_file(result_path, require_unique=True)
        result_record = dict(result)
        results.append(
            AttemptResult(
                attempt=attempt,
                path=result_path,
                result_sha256=result_sha256,
                result=result_record,
                successful=accepted,
            )
        )
        if accepted:
            successes.append(
                AttemptSuccess(
                    attempt=attempt,
                    path=result_path,
                    result_sha256=result_sha256,
                    result=result_record,
                )
            )
    return AttemptInventory(attempts, tuple(successes), tuple(results))


def scan_launch_evidence(
    campaign_dir: os.PathLike[str] | str,
    *,
    run_id: str,
    campaign_sha256: str,
    worker_id: str,
) -> tuple[dict[str, object], ...]:
    """Load the exact contiguous immutable launch history for one worker."""

    model.validate_identifier(run_id, "run")
    digest = _validate_sha256(campaign_sha256, "campaign_sha256")
    model.validate_identifier(worker_id, "worker")
    _expected_worker_phase_and_cpus(worker_id)
    root = safe_io.lexical_absolute(campaign_dir, "campaign directory")
    safe_io.inspect_path(root, require_kind="directory")
    launches_root = root / "launches"
    if not _existing_directory(launches_root, "campaign launches directory"):
        return ()
    worker_root = launches_root / worker_id
    if not _existing_directory(worker_root, "worker launch directory"):
        return ()
    names = _directory_names(worker_root, "worker launch history")
    numbered: list[tuple[int, str]] = []
    for name in sorted(names):
        match = _LAUNCH_FILE_RE.fullmatch(name)
        if match is None:
            raise model.CampaignError(
                f"worker launch history contains an unexpected entry: {name!r}"
            )
        launch = int(match.group(1))
        if not 1 <= launch <= 9999:
            raise model.CampaignError(f"worker launch number is invalid: {name!r}")
        numbered.append((launch, name))
    launches = [launch for launch, _name in numbered]
    if launches != list(range(1, len(launches) + 1)):
        raise model.CampaignError(
            f"worker launch history must be contiguous from 0001: {launches!r}"
        )
    safe_io.exact_directory_scan(worker_root, {name: "file" for _launch, name in numbered})
    records: list[dict[str, object]] = []
    for launch, name in numbered:
        loaded = safe_io.strict_json_load(
            worker_root / name,
            validator=partial(
                validate_launch_evidence,
                run_id=run_id,
                campaign_sha256=digest,
                worker_id=worker_id,
                launch=launch,
            ),
        )
        if not isinstance(loaded, dict):
            raise model.CampaignError("launch evidence validator returned an invalid object")
        records.append(cast(dict[str, object], loaded))
    return tuple(records)


def persist_launch_evidence(
    campaign_dir: os.PathLike[str] | str,
    evidence: Mapping[str, object],
) -> Path:
    """Append one immutable launch record to a contiguous worker history."""

    raw = model.require_exact_keys(evidence, _LAUNCH_KEYS, "campaign launch evidence")
    run_id = cast(str, raw["run_id"])
    digest = cast(str, raw["campaign_sha256"])
    worker_id = cast(str, raw["worker_id"])
    launch = cast(int, raw["launch"])
    validated = validate_launch_evidence(
        raw,
        run_id=run_id,
        campaign_sha256=digest,
        worker_id=worker_id,
        launch=launch,
    )
    root = safe_io.lexical_absolute(campaign_dir, "campaign directory")
    safe_io.inspect_path(root, require_kind="directory")
    launches_root = safe_io.create_private_directory(root / "launches", exist_ok=True)
    worker_root = safe_io.create_private_directory(launches_root / worker_id, exist_ok=True)
    existing = scan_launch_evidence(
        root,
        run_id=run_id,
        campaign_sha256=digest,
        worker_id=worker_id,
    )
    path = worker_root / f"{launch:04d}.json"
    if launch <= len(existing):
        if existing[launch - 1] != validated:
            raise model.CampaignError(f"conflicting immutable launch evidence: {path}")
        return path
    if launch != len(existing) + 1:
        raise model.CampaignError("launch evidence must append the next contiguous number")
    safe_io.atomic_create_json(path, validated)
    reloaded = safe_io.strict_json_load(
        path,
        validator=lambda value: validate_launch_evidence(
            value,
            run_id=run_id,
            campaign_sha256=digest,
            worker_id=worker_id,
            launch=launch,
        ),
    )
    if reloaded != validated:
        raise model.CampaignError("persisted launch evidence differs after reload")
    return path


def find_launch_evidence(
    launches: Sequence[Mapping[str, object]],
    *,
    pane_id: str,
    pane_pid: int,
) -> dict[str, object] | None:
    """Find one unique launch by exact pane ID and PID."""

    if isinstance(launches, (str, bytes)) or not isinstance(launches, Sequence):
        raise model.CampaignError("launch history must be a sequence")
    if type(pane_id) is not str or _PANE_ID_RE.fullmatch(pane_id) is None:
        raise model.CampaignError(f"invalid tmux pane id: {pane_id!r}")
    expected_pid = _positive_int(pane_pid, "tmux pane pid", maximum=_MAX_PID)
    matches: list[dict[str, object]] = []
    for launch in launches:
        if not isinstance(launch, Mapping):
            raise model.CampaignError("launch history contains a non-object")
        pane = PaneIdentity.from_dict(launch.get("pane"))
        if pane.pane_id == pane_id and pane.pane_pid == expected_pid:
            matches.append(dict(launch))
    if len(matches) > 1:
        raise model.CampaignError("multiple launch records claim the same pane identity")
    return matches[0] if matches else None


def wait_for_launch_evidence(
    campaign_dir: os.PathLike[str] | str,
    *,
    run_id: str,
    campaign_sha256: str,
    worker_id: str,
    pane_id: str,
    pane_pid: int,
    timeout_seconds: int | float = 30.0,
    monotonic: Callable[[], float] = time.monotonic,
    sleeper: Callable[[float], None] = time.sleep,
) -> dict[str, object]:
    """Wait boundedly for the controller to persist this worker's exact pane."""

    if (
        type(timeout_seconds) not in {int, float}
        or isinstance(timeout_seconds, bool)
        or timeout_seconds <= 0
    ):
        raise model.CampaignError("launch evidence timeout must be positive")
    if not callable(monotonic) or not callable(sleeper):
        raise model.CampaignError("launch evidence clock providers must be callable")
    start = monotonic()
    last_error: model.CampaignError | None = None
    while True:
        try:
            launches = scan_launch_evidence(
                campaign_dir,
                run_id=run_id,
                campaign_sha256=campaign_sha256,
                worker_id=worker_id,
            )
            matched = find_launch_evidence(
                launches,
                pane_id=pane_id,
                pane_pid=pane_pid,
            )
        except model.CampaignError as exc:
            last_error = exc
        else:
            if matched is not None:
                return matched
        elapsed = monotonic() - start
        if elapsed >= float(timeout_seconds):
            detail = f": {last_error}" if last_error is not None else ""
            raise model.CampaignError(
                f"timed out waiting for exact controller launch evidence{detail}"
            )
        sleeper(min(0.1, float(timeout_seconds) - elapsed))


def _validate_lock_history_tree(directory: Path) -> None:
    root = directory / safe_io.LOCK_ROOT_NAME
    _require_private_directory(root, "campaign lock-history root")
    namespaces = _directory_names(root, "campaign lock-history root")
    if any(_SHA256_RE.fullmatch(name) is None for name in namespaces):
        raise model.CampaignError("campaign lock-history root has unexpected entries")
    safe_io.exact_directory_scan(root, {name: "directory" for name in namespaces})
    for namespace_name in sorted(namespaces):
        namespace = root / namespace_name
        _require_private_directory(namespace, "campaign lock-history namespace")
        released = _directory_names(namespace, "campaign lock-history namespace")
        if any(_RELEASED_LOCK_RE.fullmatch(name) is None for name in released):
            raise model.CampaignError("campaign lock-history namespace has unexpected entries")
        safe_io.exact_directory_scan(namespace, {name: "directory" for name in released})
        for released_name in sorted(released):
            released_dir = namespace / released_name
            _require_private_directory(released_dir, "released campaign lock")
            safe_io.exact_directory_scan(released_dir, {"owner.json": "file"})
            safe_io.strict_json_load(
                released_dir / "owner.json", validator=safe_io.validate_lock_owner
            )


def _validate_status_retirement_tree(
    job_dir: Path,
    *,
    run_id: str,
    campaign_sha256: str,
    job: model.JobSpec,
) -> None:
    root = job_dir / safe_io.RETIRE_ROOT_NAME
    _require_private_directory(root, "status retirement root")
    retired = _directory_names(root, "status retirement root")
    if any(_RETIRED_RE.fullmatch(name) is None for name in retired):
        raise model.CampaignError("status retirement root has unexpected entries")
    safe_io.exact_directory_scan(root, {name: "directory" for name in retired})
    for retired_name in sorted(retired):
        namespace = root / retired_name
        _require_private_directory(namespace, "status retirement namespace")
        safe_io.exact_directory_scan(namespace, {"entry": "file"})
        safe_io.strict_json_load(
            namespace / "entry",
            validator=partial(
                validate_status,
                run_id=run_id,
                campaign_sha256=campaign_sha256,
                job=job,
            ),
        )


def _validate_structural_worker_result(
    value: object,
    attempt: int,
    path: Path,
    *,
    run_id: str,
    campaign_sha256: str,
    job: model.JobSpec,
) -> bool:
    if not isinstance(value, Mapping):
        raise model.CampaignError("worker result must be an object")
    expected = {
        "schema_version": model.WORKER_SCHEMA,
        "run_id": run_id,
        "campaign_sha256": campaign_sha256,
        "job_id": job.job_id,
        "job_sha256": job.job_sha256,
        "attempt": attempt,
    }
    for key, expected_value in expected.items():
        if value.get(key) != expected_value:
            raise model.CampaignError(f"worker result {path} has mismatched {key!r}")
    return (
        value.get("state") == "succeeded"
        and isinstance(value.get("validation"), Mapping)
        and cast(Mapping[str, object], value["validation"]).get("accepted") is True
    )


def validate_runtime_tree(
    campaign_dir: os.PathLike[str] | str,
    campaign: Mapping[str, object],
) -> None:
    """Validate worker-owned runtime namespaces beneath a completed campaign."""

    validated_campaign = model.validate_campaign_document(campaign)
    run_id = cast(str, validated_campaign["run_id"])
    digest = model.campaign_digest(validated_campaign)
    jobs = model.campaign_jobs(validated_campaign)
    job_by_id = {job.job_id: job for job in jobs}
    worker_ids = {job.worker_id for job in jobs}
    root = safe_io.lexical_absolute(campaign_dir, "campaign directory")
    safe_io.inspect_path(root, require_kind="directory")

    launches_root = root / "launches"
    if _existing_directory(launches_root, "campaign launches directory"):
        names = _directory_names(launches_root, "campaign launches directory")
        unknown = names - worker_ids
        if unknown:
            raise model.CampaignError(
                f"campaign launches contain unknown workers: {sorted(unknown)!r}"
            )
        safe_io.exact_directory_scan(launches_root, {name: "directory" for name in names})
        for worker_id in sorted(names):
            scan_launch_evidence(
                root,
                run_id=run_id,
                campaign_sha256=digest,
                worker_id=worker_id,
            )

    jobs_root = root / "jobs"
    if not _existing_directory(jobs_root, "campaign jobs directory"):
        return
    names = _directory_names(jobs_root, "campaign jobs directory")
    internal = {safe_io.LOCK_ROOT_NAME}
    unknown = names - set(job_by_id) - internal
    if unknown:
        raise model.CampaignError(f"campaign jobs contain unknown entries: {sorted(unknown)!r}")
    safe_io.exact_directory_scan(
        jobs_root,
        {name: "directory" for name in names},
    )
    if safe_io.LOCK_ROOT_NAME in names:
        _validate_lock_history_tree(jobs_root)
    for job_id in sorted(names - internal):
        job = job_by_id[job_id]
        job_dir = safe_io.job_directory(root, job_id)
        _require_private_directory(job_dir, "campaign job directory")
        job_names = _directory_names(job_dir, "campaign job directory")
        allowed = {
            "attempts": "directory",
            "status.json": "file",
            safe_io.RETIRE_ROOT_NAME: "directory",
        }
        extra = job_names - set(allowed)
        if extra:
            raise model.CampaignError(
                f"campaign job {job_id!r} contains unknown entries: {sorted(extra)!r}"
            )
        safe_io.exact_directory_scan(job_dir, {name: allowed[name] for name in job_names})
        if "status.json" in job_names:
            read_status(
                root,
                run_id=run_id,
                campaign_sha256=digest,
                job=job,
            )
        if safe_io.RETIRE_ROOT_NAME in job_names:
            _validate_status_retirement_tree(
                job_dir,
                run_id=run_id,
                campaign_sha256=digest,
                job=job,
            )
        if "attempts" in job_names:
            scan_attempts(
                root,
                job,
                result_validator=partial(
                    _validate_structural_worker_result,
                    run_id=run_id,
                    campaign_sha256=digest,
                    job=job,
                ),
            )


def decide_reconciliation(
    inventory: AttemptInventory,
    status: Mapping[str, object],
    *,
    resume: bool,
    running_observation: str | None,
) -> ReconcileDecision:
    """Choose promotion, recovery, or a fresh attempt from exact evidence."""

    if not isinstance(inventory, AttemptInventory):
        raise model.CampaignError("reconciliation requires an attempt inventory")
    if not isinstance(status, Mapping):
        raise model.CampaignError("reconciliation requires a validated status")
    if type(resume) is not bool:
        raise model.CampaignError("resume must be a boolean")
    if running_observation not in {None, "live", "stale", "foreign"}:
        raise model.CampaignError("invalid running observation")
    state = status.get("state")
    if state == "running" and running_observation == "live":
        raise model.CampaignError("recorded worker is still live")
    if state == "running" and running_observation == "foreign":
        raise model.CampaignError("foreign-host running identity blocks reconciliation")
    if state == "running" and running_observation is None:
        raise model.CampaignError("running status requires an exact liveness observation")
    if len(inventory.successes) > 1:
        raise model.CampaignError("multiple successful attempts poison campaign reconciliation")
    if len(inventory.successes) == 1:
        return ReconcileDecision("promote", None, inventory.successes[0])
    if state == "succeeded":
        raise model.CampaignError("succeeded status has no valid immutable success result")
    if state == "pending":
        if not inventory.attempts:
            return ReconcileDecision("run", inventory.next_attempt)
        if not resume:
            raise model.CampaignError("incomplete pending attempts require --resume")
        return ReconcileDecision("recover", inventory.next_attempt)
    if state == "running":
        if running_observation != "stale":
            raise model.CampaignError("running status requires an exact liveness observation")
        if not resume:
            raise model.CampaignError("stale running status requires --resume")
        return ReconcileDecision("recover", inventory.next_attempt)
    if state in _TERMINAL_STATES:
        if not resume:
            raise model.CampaignError(f"terminal state {state!r} requires --resume")
        return ReconcileDecision("run", inventory.next_attempt)
    raise model.CampaignError(f"cannot reconcile invalid campaign state: {state!r}")


def _read_proc_start_ticks(path: Path, pid: int) -> int | None:
    try:
        raw = path.read_text(encoding="ascii")
    except FileNotFoundError:
        return None
    except (OSError, UnicodeError) as exc:
        raise model.CampaignError(f"cannot read process stat for PID {pid}: {exc}") from exc
    closing = raw.rstrip("\n").rfind(")")
    prefix = f"{pid} ("
    if not raw.startswith(prefix) or closing < len(prefix) or raw[closing + 1 : closing + 2] != " ":
        raise model.CampaignError(f"malformed process stat for PID {pid}")
    fields = raw[closing + 2 :].split()
    try:
        start_ticks = int(fields[19])
    except (IndexError, ValueError) as exc:
        raise model.CampaignError(f"malformed process stat for PID {pid}") from exc
    if start_ticks < 1:
        raise model.CampaignError(f"invalid process start ticks for PID {pid}")
    return start_ticks


def _read_boot_id(proc_root: Path) -> str:
    path = proc_root / "sys" / "kernel" / "random" / "boot_id"
    try:
        value = path.read_text(encoding="ascii").strip()
    except (OSError, UnicodeError) as exc:
        raise model.CampaignError(f"cannot read Linux boot identity: {exc}") from exc
    if _BOOT_ID_RE.fullmatch(value) is None:
        raise model.CampaignError("Linux boot identity is not a lowercase UUID")
    return value


def observe_process(
    pid: int,
    *,
    proc_root: os.PathLike[str] | str = "/proc",
    pgid_provider: Callable[[int], int] = os.getpgid,
) -> ProcessIdentity | None:
    """Observe a stable Linux process identity, returning ``None`` if it vanished."""

    process_pid = _positive_int(pid, "observed process pid", maximum=_MAX_PID)
    if not callable(pgid_provider):
        raise model.CampaignError("process-group provider must be callable")
    root = safe_io.lexical_absolute(proc_root, "proc root")
    stat_path = root / str(process_pid) / "stat"
    boot_before = _read_boot_id(root)
    start_before = _read_proc_start_ticks(stat_path, process_pid)
    if start_before is None:
        return None
    try:
        pgid = pgid_provider(process_pid)
    except ProcessLookupError:
        return None
    except OSError as exc:
        raise model.CampaignError(
            f"cannot observe process group for PID {process_pid}: {exc}"
        ) from exc
    process_pgid = _positive_int(pgid, "observed process group", maximum=_MAX_PID)
    start_after = _read_proc_start_ticks(stat_path, process_pid)
    if start_after is None or start_after != start_before:
        return None
    boot_after = _read_boot_id(root)
    if boot_after != boot_before:
        return None
    return ProcessIdentity(
        pid=process_pid,
        pgid=process_pgid,
        process_start_ticks=start_before,
        boot_id=boot_before,
    )


def _status_path(root: Path, spec: model.JobSpec) -> Path:
    return safe_io.job_directory(root, spec.job_id) / "status.json"


def read_status(
    campaign_dir: os.PathLike[str] | str,
    *,
    run_id: str,
    campaign_sha256: str,
    job: model.JobSpec | Mapping[str, object],
) -> dict[str, object]:
    """Read one status through bounded no-follow I/O or synthesize pending."""

    spec = _job_spec(job)
    root = safe_io.lexical_absolute(campaign_dir, "campaign directory")
    safe_io.inspect_path(root, require_kind="directory")
    job_dir = safe_io.job_directory(root, spec.job_id)
    safe_io.inspect_path(job_dir, allow_missing_tail=True)
    try:
        job_item = os.lstat(job_dir)
    except FileNotFoundError:
        return pending_status(run_id=run_id, campaign_sha256=campaign_sha256, job=spec)
    if not stat.S_ISDIR(job_item.st_mode):
        raise model.CampaignError(f"campaign job path is not a directory: {job_dir}")
    _require_private_directory(job_dir, "campaign job directory")
    path = _status_path(root, spec)
    try:
        os.lstat(path)
    except FileNotFoundError:
        return pending_status(run_id=run_id, campaign_sha256=campaign_sha256, job=spec)
    loaded = safe_io.strict_json_load(
        path,
        validator=lambda value: validate_status(
            value,
            run_id=run_id,
            campaign_sha256=campaign_sha256,
            job=spec,
        ),
    )
    if not isinstance(loaded, dict):
        raise model.CampaignError("campaign status validator returned an invalid object")
    return cast(dict[str, object], loaded)


def _validate_transition(previous: Mapping[str, object], current: Mapping[str, object]) -> None:
    old_state = previous.get("state")
    new_state = current.get("state")
    allowed = {
        "pending": {"running", "succeeded"},
        "running": {"running", *_TERMINAL_STATES},
        "failed": {"running", "succeeded"},
        "timed_out": {"running", "succeeded"},
        "interrupted": {"running", "succeeded"},
        "succeeded": {"succeeded"},
    }
    if new_state not in allowed.get(cast(str, old_state), set()):
        raise model.CampaignError(f"invalid status transition: {old_state!r} -> {new_state!r}")
    if old_state == "succeeded" and dict(previous) != dict(current):
        raise model.CampaignError("a succeeded status is immutable")
    if old_state == "running" and new_state == "running":
        previous_fixed = {key: value for key, value in previous.items() if key != "child"}
        current_fixed = {key: value for key, value in current.items() if key != "child"}
        if previous_fixed != current_fixed:
            raise model.CampaignError("running status identity changed during child update")
        old_child = previous.get("child")
        if old_child is not None and current.get("child") != old_child:
            raise model.CampaignError("running child identity is immutable once recorded")
    if old_state == "running" and new_state in _TERMINAL_STATES:
        for key in _ACTIVE_STATUS_KEYS - {"state"}:
            if previous.get(key) != current.get(key):
                raise model.CampaignError(f"terminal status changed active identity field {key!r}")
    if old_state in _TERMINAL_STATES and new_state == "running":
        old_attempt = previous.get("attempt")
        new_attempt = current.get("attempt")
        if (
            type(old_attempt) is not int
            or type(new_attempt) is not int
            or new_attempt <= old_attempt
        ):
            raise model.CampaignError("resumed status must advance the attempt number")


def write_status(
    campaign_dir: os.PathLike[str] | str,
    status: Mapping[str, object],
    *,
    previous: Mapping[str, object],
    run_id: str,
    campaign_sha256: str,
    job: model.JobSpec | Mapping[str, object],
) -> dict[str, object]:
    """Durably replace status after exact transition and stale-write checks."""

    spec = _job_spec(job)
    root = safe_io.lexical_absolute(campaign_dir, "campaign directory")
    old = validate_status(
        previous,
        run_id=run_id,
        campaign_sha256=campaign_sha256,
        job=spec,
    )
    new = validate_status(
        status,
        run_id=run_id,
        campaign_sha256=campaign_sha256,
        job=spec,
    )
    actual = read_status(
        root,
        run_id=run_id,
        campaign_sha256=campaign_sha256,
        job=spec,
    )
    if actual != old:
        raise model.CampaignError("campaign status changed; refusing a stale status write")
    _validate_transition(old, new)
    attempt_dir = safe_io.attempt_directory(root, spec.job_id, cast(int, new["attempt"]))
    safe_io.inspect_path(attempt_dir, require_kind="directory")
    if new["state"] in _TERMINAL_STATES:
        result_path = attempt_dir / "result.json"
        try:
            actual_digest = safe_io.sha256_regular_file(result_path, require_unique=True)
        except model.CampaignError as exc:
            raise model.CampaignError(
                f"terminal status result is missing or unsafe: {result_path}"
            ) from exc
        if actual_digest != new["result_sha256"]:
            raise model.CampaignError("terminal status result digest mismatch")
    path = _status_path(root, spec)
    safe_io.atomic_write_json_durable(path, new)
    persisted = read_status(
        root,
        run_id=run_id,
        campaign_sha256=campaign_sha256,
        job=spec,
    )
    if persisted != new:
        raise model.CampaignError("persisted campaign status differs after reload")
    return persisted


def _utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _open_private_log(path: Path) -> Any:
    target = safe_io.lexical_absolute(path, "campaign log")
    safe_io.inspect_path(target.parent, require_kind="directory")
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC
    try:
        descriptor = os.open(target, flags, 0o600)
    except OSError as exc:
        raise model.CampaignError(f"cannot create private campaign log {target}: {exc}") from exc
    try:
        item = os.fstat(descriptor)
        if (
            not stat.S_ISREG(item.st_mode)
            or item.st_nlink != 1
            or stat.S_IMODE(item.st_mode) != 0o600
        ):
            raise model.CampaignError(f"campaign log is not a private unique file: {target}")
        return os.fdopen(descriptor, "wb")
    except BaseException:
        os.close(descriptor)
        raise


def _bounded_error(exc: BaseException) -> str:
    message = str(exc).replace("\r", " ").replace("\n", " ")
    text = f"{type(exc).__name__}: {message}"
    return text[:2000]


def _terminate_child_group(
    process: Any,
    identity: ProcessIdentity,
    *,
    process_observer: Callable[[int], ProcessIdentity | None],
    killpg: Callable[[int, int], None],
    grace_seconds: float,
) -> None:
    observed = process_observer(identity.pid)
    if observed != identity:
        return
    try:
        killpg(identity.pgid, signal_module.SIGTERM)
    except ProcessLookupError:
        return
    try:
        process.wait(timeout=grace_seconds)
        return
    except subprocess.TimeoutExpired:
        pass
    observed = process_observer(identity.pid)
    if observed != identity:
        return
    try:
        killpg(identity.pgid, signal_module.SIGKILL)
    except ProcessLookupError:
        return
    process.wait()


def execute_child(
    argv: Sequence[str],
    *,
    cwd: os.PathLike[str] | str,
    environment: Mapping[str, str],
    stdout_path: os.PathLike[str] | str,
    stderr_path: os.PathLike[str] | str,
    timeout_seconds: int,
    popen_factory: Callable[..., Any] = subprocess.Popen,
    process_observer: Callable[[int], ProcessIdentity | None] = observe_process,
    child_started: Callable[[ProcessIdentity], None],
    signal_registrar: Callable[[int, Any], Any] = signal_module.signal,
    killpg: Callable[[int, int], None] = os.killpg,
    log_opener: Callable[[Path], Any] = _open_private_log,
    now_provider: Callable[[], str] = _utc_now,
    termination_grace_seconds: float = 15.0,
) -> ChildOutcome:
    """Run one child under a total catchable-failure and process-group boundary."""

    command = _validate_worker_argv(argv)
    workdir = safe_io.lexical_absolute(cwd, "child working directory")
    safe_io.inspect_path(workdir, require_kind="directory")
    if not isinstance(environment, Mapping) or any(
        type(key) is not str or type(value) is not str for key, value in environment.items()
    ):
        raise model.CampaignError("child environment must be a string mapping")
    env = dict(environment)
    stdout = safe_io.lexical_absolute(stdout_path, "child stdout log")
    stderr = safe_io.lexical_absolute(stderr_path, "child stderr log")
    if stdout == stderr:
        raise model.CampaignError("child stdout and stderr logs must differ")
    timeout = _positive_int(timeout_seconds, "child timeout")
    if not callable(popen_factory) or not callable(process_observer):
        raise model.CampaignError("child process providers must be callable")
    if not callable(child_started) or not callable(signal_registrar):
        raise model.CampaignError("child lifecycle callbacks must be callable")
    if not callable(killpg) or not callable(log_opener) or not callable(now_provider):
        raise model.CampaignError("child lifecycle I/O providers must be callable")
    if (
        type(termination_grace_seconds) not in {int, float}
        or isinstance(termination_grace_seconds, bool)
        or termination_grace_seconds <= 0
    ):
        raise model.CampaignError("termination grace must be a positive number")

    started_utc = now_provider()
    _validate_timestamp(started_utc, "child started_utc")
    process: Any = None
    child: ProcessIdentity | None = None
    exit_code: int | None = None
    error: str | None = None
    signal_number: int | None = None
    state = "failed"
    old_handlers: dict[int, Any] = {}

    def interrupt(signum: int, _frame: object) -> None:
        raise _ChildInterrupted(signum)

    try:
        for signum in (signal_module.SIGINT, signal_module.SIGTERM):
            old_handlers[signum] = signal_registrar(signum, interrupt)
        with ExitStack() as stack:
            stdout_stream = stack.enter_context(log_opener(stdout))
            stderr_stream = stack.enter_context(log_opener(stderr))
            process = popen_factory(
                command,
                cwd=workdir,
                env=env,
                stdout=stdout_stream,
                stderr=stderr_stream,
                start_new_session=True,
            )
            process_pid = _positive_int(
                getattr(process, "pid", None), "child process pid", maximum=_MAX_PID
            )
            observed = process_observer(process_pid)
            if observed is None:
                raise model.CampaignError("child exited before its process identity was recorded")
            if observed.pid != process_pid or observed.pgid != process_pid:
                raise model.CampaignError(
                    "start_new_session child did not establish its own process group"
                )
            child = observed
            child_started(child)
            try:
                waited = process.wait(timeout=timeout)
            except subprocess.TimeoutExpired:
                state = "timed_out"
                error = f"child exceeded the {timeout}-second controller timeout"
                _terminate_child_group(
                    process,
                    child,
                    process_observer=process_observer,
                    killpg=killpg,
                    grace_seconds=float(termination_grace_seconds),
                )
                returned = getattr(process, "returncode", None)
                exit_code = returned if type(returned) is int else None
            else:
                if type(waited) is not int:
                    raise model.CampaignError("child wait returned a non-integer exit code")
                exit_code = waited
                if waited == 0:
                    state = "succeeded"
                else:
                    state = "failed"
                    error = f"child exited with status {waited}"
    except _ChildInterrupted as exc:
        state = "interrupted"
        signal_number = exc.signum
        error = f"worker received signal {exc.signum}"
        if process is not None and child is not None:
            _terminate_child_group(
                process,
                child,
                process_observer=process_observer,
                killpg=killpg,
                grace_seconds=float(termination_grace_seconds),
            )
            returned = getattr(process, "returncode", None)
            exit_code = returned if type(returned) is int else None
    except KeyboardInterrupt as exc:
        state = "interrupted"
        signal_number = signal_module.SIGINT
        error = _bounded_error(exc)
        if process is not None and child is not None:
            _terminate_child_group(
                process,
                child,
                process_observer=process_observer,
                killpg=killpg,
                grace_seconds=float(termination_grace_seconds),
            )
            returned = getattr(process, "returncode", None)
            exit_code = returned if type(returned) is int else None
    except BaseException as exc:
        state = "failed"
        error = _bounded_error(exc)
        if process is not None and child is not None:
            _terminate_child_group(
                process,
                child,
                process_observer=process_observer,
                killpg=killpg,
                grace_seconds=float(termination_grace_seconds),
            )
            returned = getattr(process, "returncode", None)
            exit_code = returned if type(returned) is int else None
    finally:
        for restore_signum, old_handler in reversed(tuple(old_handlers.items())):
            try:
                signal_registrar(restore_signum, old_handler)
            except BaseException as exc:
                if state == "succeeded":
                    state = "failed"
                    error = _bounded_error(exc)

    finished_utc = now_provider()
    finished = _validate_timestamp(finished_utc, "child finished_utc")
    if finished < _validate_timestamp(started_utc, "child started_utc"):
        raise model.CampaignError("child finished before it started")
    return ChildOutcome(
        state=state,
        started_utc=started_utc,
        finished_utc=finished_utc,
        child=child,
        exit_code=exit_code,
        error=error,
        signal_number=signal_number,
    )


__all__ = [
    "TMUX_PANE_FORMAT",
    "AttemptInventory",
    "AttemptResult",
    "AttemptSuccess",
    "ChildOutcome",
    "PaneIdentity",
    "ProcessIdentity",
    "ReconcileDecision",
    "allocate_attempt",
    "build_tmux_launch_argv",
    "build_tmux_kill_pane_argv",
    "build_tmux_list_panes_argv",
    "classify_running_status",
    "decide_reconciliation",
    "execute_child",
    "build_launch_evidence",
    "find_launch_evidence",
    "parse_tmux_pane",
    "parse_tmux_panes",
    "parse_tmux_launch_output",
    "pending_status",
    "observe_process",
    "read_status",
    "persist_launch_evidence",
    "scan_attempts",
    "scan_launch_evidence",
    "validate_status",
    "validate_launch_evidence",
    "validate_runtime_tree",
    "wait_for_launch_evidence",
    "write_status",
]
