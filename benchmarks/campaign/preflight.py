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
"""Closed transactional preflight contracts and immutable bundle loading.

Pure request normalization is deliberately separate from host probes.  A run
is complete only when both ``campaign.json`` and the final
``preflight.json`` marker validate and cross-bind.
"""

from __future__ import annotations

import gzip
import hashlib
import json
import os
import re
import secrets
import stat
import zipfile
import zlib
from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import cast

from . import model, safe_io

REQUEST_SCHEMA = "campaign-request-v1"
PROBE_ROOT_NAME = ".gffbase-probes"

_REQUEST_KEYS = {
    "schema_version",
    "run_id",
    "campaign_root",
    "run_dir",
    "candidate_wheel",
    "interpreters",
    "transform",
    "parameters",
}
_TRANSFORM_KEYS = {"mode", "output", "manifest"}
_CAMPAIGN_REFERENCE_KEYS = {"path", "file_sha256", "spec_sha256"}
_PREFLIGHT_KEYS = {
    "schema_version",
    "run_id",
    "verified_utc",
    "request",
    "request_sha256",
    "campaign",
    "evidence",
    "gates",
}
_EVIDENCE_KEYS = {
    "repo_before",
    "repo_after",
    "candidate",
    "interpreters",
    "inputs",
    "transform",
    "resources",
    "filesystems",
    "topology",
    "matrix",
    "session",
}
_GATE_KEYS = {
    "clean_repo",
    "repo_stable",
    "candidate_identity",
    "interpreter_pins",
    "inputs",
    "parent_stripped_manifest",
    "resources",
    "filesystem_capabilities",
    "path_limits",
    "topology",
    "matrix",
    "session_names",
}
_SHA256_RE = re.compile(r"[0-9a-f]{64}\Z", re.ASCII)
_GIT_COMMIT_RE = re.compile(r"[0-9a-f]{40}\Z", re.ASCII)
_UTC_RE = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z\Z", re.ASCII)
_TOKEN_RE = re.compile(r"[0-9a-f]{32}\Z", re.ASCII)
_PROBE_NAMESPACE_RE = re.compile(r"probe-[0-9a-f]{32}\Z", re.ASCII)
_RELEASED_LOCK_RE = re.compile(r"released-[0-9a-f]{32}\Z", re.ASCII)
_RETIRED_RE = re.compile(r"retired-[0-9a-f]{32}\Z", re.ASCII)
_MOUNT_ESCAPE_RE = re.compile(r"\\([0-7]{3})")
_CAPABILITY_KEYS = {
    "probe_path",
    "st_dev",
    "name_max",
    "path_max",
    "directory_fsync",
    "rename_noreplace_same_directory",
    "rename_noreplace_collision",
    "rename_noreplace_cross_directory",
    "rename_exchange_same_directory",
    "parents_fsynced",
}
_REPO_IDENTITY_KEYS = {
    "root",
    "commit",
    "branch",
    "dirty",
    "status",
    "dirty_submodules",
}
_WHEEL_IDENTITY_KEYS = {"path", "name", "bytes", "sha256", "metadata_version"}
_PAIR_KEYS = {"before", "after"}
_INTERPRETER_PROBE_KEYS = {
    "resolved_executable",
    "requested_executable",
    "executable_identity",
    "prefix",
    "implementation",
    "python_version",
    "isolated",
    "sys_path",
    "packages",
    "modules",
    "distribution_roots",
    "direct_url",
    "pth_files",
    "role",
    "probe_sha256",
}
_EXECUTABLE_IDENTITY_KEYS = {
    "st_dev",
    "st_ino",
    "st_size",
    "st_mtime_ns",
    "st_ctime_ns",
}
_MODULE_IDENTITY_KEYS = {"path", "version", "sha256"}
_PTH_IDENTITY_KEYS = {"path", "sha256", "active_lines"}
_RESOURCE_KEYS = {
    "platform",
    "machine",
    "allowed_cpus",
    "online_cpus",
    "physical_cpu_ids",
    "numa_nodes",
    "free_bytes",
    "available_ram_bytes",
    "mount",
    "executables",
    "executable_versions",
    "executable_identities",
    "thresholds",
}
_MOUNT_KEYS = {"raw", "fstype", "probe_path"}
_EXECUTABLE_ROLES = {"findmnt", "taskset", "tmux"}
_THRESHOLD_KEYS = {
    "free_bytes",
    "available_ram_bytes",
    "disk_formula",
    "ram_formula",
}
_FILESYSTEM_EVIDENCE_KEYS = {
    "identity",
    "mutable_targets",
    "probe_target",
    "capabilities",
    "path_budget",
}
_FILESYSTEM_IDENTITY_KEYS = {
    "mount_id",
    "parent_mount_id",
    "device",
    "st_dev",
    "mount_root",
    "mount_point",
    "mount_options",
    "optional_fields",
    "filesystem_type",
    "source",
    "super_options",
}
_PATH_BUDGET_KEYS = {"name_max", "path_max", "checked_paths"}
_MATRIX_EVIDENCE_KEYS = {"job_count", "jobs_sha256"}
_SESSION_EVIDENCE_KEYS = {"names", "before", "after"}

FaultHook = Callable[[str], None]
TokenProvider = Callable[[], str]
FilesystemIdentityProvider = Callable[[Path], Mapping[str, object]]
CapabilityProbe = Callable[[Path], Mapping[str, object]]
PhaseValidator = Callable[[str], None]
RuntimeValidator = Callable[[Path, Mapping[str, object]], None]

_REGULAR_VERSION_FIELDS = (
    "st_dev",
    "st_ino",
    "st_mode",
    "st_nlink",
    "st_size",
    "st_mtime_ns",
    "st_ctime_ns",
)


def _fault(hook: FaultHook | None, stage: str) -> None:
    if hook is not None:
        hook(stage)


def _canonical_path(value: object, label: str) -> str:
    if type(value) is not str:
        raise model.CampaignError(f"{label} must be an absolute lexical path string")
    normalized = safe_io.lexical_absolute(value, label)
    if str(normalized) != value:
        raise model.CampaignError(f"{label} is not in canonical lexical form")
    return str(normalized)


def _path_argument(value: os.PathLike[str] | str, label: str) -> str:
    return str(safe_io.lexical_absolute(value, label))


@contextmanager
def _open_bound_regular(
    value: os.PathLike[str] | str,
    label: str,
) -> Iterator[tuple[Path, int, os.stat_result]]:
    path = safe_io.inspect_path(value, require_kind="file", require_unique_file=True)
    with safe_io._open_parent(path) as (parent_descriptor, name, parent, parent_identity):
        item = safe_io._lstat_at(parent_descriptor, name)
        if item is None or not stat.S_ISREG(item.st_mode) or item.st_nlink != 1:
            raise model.CampaignError(f"{label} is not a uniquely linked regular file")
        flags = os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC
        try:
            descriptor = os.open(name, flags, dir_fd=parent_descriptor)
        except OSError as exc:
            raise model.CampaignError(f"cannot open {label}: {exc}") from exc
        try:
            opened = os.fstat(descriptor)
            if any(
                getattr(opened, field) != getattr(item, field) for field in _REGULAR_VERSION_FIELDS
            ):
                raise model.CampaignError(f"{label} changed while it was opened")
            yield path, descriptor, opened
            final_opened = os.fstat(descriptor)
            final_item = safe_io._lstat_at(parent_descriptor, name)
            if final_item is None or any(
                getattr(final_opened, field) != getattr(opened, field)
                or getattr(final_item, field) != getattr(opened, field)
                for field in _REGULAR_VERSION_FIELDS
            ):
                raise model.CampaignError(f"{label} changed while it was read")
            safe_io._assert_parent_identity(parent, parent_descriptor, parent_identity)
        finally:
            safe_io._close_descriptor(descriptor)


def _sha256_descriptor(descriptor: int) -> str:
    digest = hashlib.sha256()
    os.lseek(descriptor, 0, os.SEEK_SET)
    while chunk := os.read(descriptor, 1 << 20):
        digest.update(chunk)
    return digest.hexdigest()


def verify_input_file(
    path: os.PathLike[str] | str,
    *,
    expected_size: int,
    expected_sha256: str,
    check_gzip: bool = True,
) -> dict[str, object]:
    """Verify one immutable input through a bound no-follow descriptor snapshot."""

    if type(expected_size) is not int or expected_size < 1:
        raise model.CampaignError("expected input size must be a positive integer")
    _require_sha256(expected_sha256, "expected input digest")
    if type(check_gzip) is not bool:
        raise model.CampaignError("check_gzip must be a boolean")
    with _open_bound_regular(path, "campaign input") as (opened_path, descriptor, opened):
        if opened.st_size != expected_size:
            raise model.CampaignError(
                f"{opened_path.name}: expected {expected_size} bytes, found {opened.st_size}"
            )
        digest = _sha256_descriptor(descriptor)
        if digest != expected_sha256:
            raise model.CampaignError(
                f"{opened_path.name}: expected sha256 {expected_sha256}, found {digest}"
            )
        gzip_crc_ok: bool | None = None
        if check_gzip and opened_path.name.endswith((".gz", ".gz.part")):
            os.lseek(descriptor, 0, os.SEEK_SET)
            try:
                with os.fdopen(os.dup(descriptor), "rb") as raw:
                    with gzip.GzipFile(fileobj=raw, mode="rb") as compressed:
                        while compressed.read(1 << 20):
                            pass
            except (OSError, EOFError, zlib.error) as exc:
                raise model.CampaignError(
                    f"gzip CRC/trailer verification failed for {opened_path}: {exc}"
                ) from exc
            gzip_crc_ok = True
    return {
        "path": str(opened_path),
        "bytes": opened.st_size,
        "sha256": digest,
        "gzip_crc_ok": gzip_crc_ok,
    }


def verify_candidate_wheel(
    wheel_path: os.PathLike[str] | str,
    *,
    public_version: str = model.PUBLIC_VERSION,
) -> dict[str, object]:
    """Validate and hash a candidate wheel from one bound regular-file snapshot."""

    with _open_bound_regular(wheel_path, "candidate wheel") as (wheel, descriptor, opened):
        if wheel.suffix != ".whl":
            raise model.CampaignError(f"candidate wheel must use the .whl suffix: {wheel}")
        os.lseek(descriptor, 0, os.SEEK_SET)
        try:
            with os.fdopen(os.dup(descriptor), "rb") as raw:
                with zipfile.ZipFile(raw) as archive:
                    names = archive.namelist()
                    if len(names) != len(set(names)) or len(names) > 100_000:
                        raise model.CampaignError(
                            "candidate wheel has duplicate or excessive entries"
                        )
                    metadata_names = [
                        name for name in names if name.endswith(".dist-info/METADATA")
                    ]
                    if len(metadata_names) != 1:
                        raise model.CampaignError(
                            "candidate wheel must contain exactly one METADATA file"
                        )
                    metadata_info = archive.getinfo(metadata_names[0])
                    if metadata_info.file_size > 1 << 20:
                        raise model.CampaignError("candidate wheel METADATA exceeds 1 MiB")
                    metadata = archive.read(metadata_info).decode("utf-8", errors="strict")
        except (OSError, zipfile.BadZipFile, UnicodeError) as exc:
            raise model.CampaignError(f"invalid candidate wheel {wheel}: {exc}") from exc
        name_match = re.search(r"^Name:\s*(.+)$", metadata, re.MULTILINE | re.IGNORECASE)
        version_match = re.search(r"^Version:\s*(.+)$", metadata, re.MULTILINE | re.IGNORECASE)
        if not name_match or not version_match:
            raise model.CampaignError("candidate wheel METADATA lacks Name/Version")
        package_name = name_match.group(1).strip()
        version = version_match.group(1).strip()
        if package_name.casefold().replace("_", "-") != "gffbase" or version != public_version:
            raise model.CampaignError(
                f"candidate wheel must be gffbase {public_version}, found {package_name} {version}"
            )
        filename_parts = wheel.name.split("-")
        if len(filename_parts) < 5 or filename_parts[1] != public_version:
            raise model.CampaignError(
                f"wheel filename version does not match {public_version}: {wheel.name}"
            )
        digest = _sha256_descriptor(descriptor)
    return {
        "path": str(wheel),
        "name": wheel.name,
        "bytes": opened.st_size,
        "sha256": digest,
        "metadata_version": version,
    }


def validate_encoded_path_budget(
    paths: list[Path] | tuple[Path, ...],
    *,
    name_max: int,
    path_max: int,
) -> dict[str, object]:
    """Validate component and absolute-path limits in filesystem bytes."""

    if type(name_max) is not int or name_max < 1:
        raise model.CampaignError("NAME_MAX must be a positive integer")
    if type(path_max) is not int or path_max < 2:
        raise model.CampaignError("PATH_MAX must be an integer greater than one")
    if not paths:
        raise model.CampaignError("path-budget validation requires at least one path")
    checked: list[str] = []
    for value in paths:
        path = safe_io.lexical_absolute(value, "path-budget candidate")
        for component in path.parts:
            if component == path.anchor:
                continue
            component_bytes = os.fsencode(component)
            if len(component_bytes) > name_max:
                raise model.CampaignError(
                    f"path component exceeds NAME_MAX={name_max} bytes: {component!r}"
                )
        encoded_length = len(os.fsencode(str(path))) + 1
        if encoded_length > path_max:
            raise model.CampaignError(
                f"absolute path including NUL exceeds PATH_MAX={path_max} bytes: {path}"
            )
        checked.append(str(path))
    return {
        "name_max": name_max,
        "path_max": path_max,
        "checked_paths": checked,
    }


def campaign_future_paths(
    request: Mapping[str, object],
    jobs: Sequence[model.JobSpec | Mapping[str, object]],
) -> tuple[Path, ...]:
    """Enumerate release-campaign paths whose encoded sizes must remain valid."""

    normalized_request = validate_request(request)
    run_dir = Path(cast(str, normalized_request["run_dir"]))
    parsed_jobs = tuple(
        job if isinstance(job, model.JobSpec) else model.JobSpec.from_dict(job) for job in jobs
    )
    if len(parsed_jobs) != 36 or len({job.job_id for job in parsed_jobs}) != 36:
        raise model.CampaignError("future-path planning requires exactly 36 unique jobs")
    for job in parsed_jobs:
        model.job_digest(job)

    paths: set[Path] = {
        run_dir,
        run_dir / "campaign.json",
        run_dir / "preflight.json",
        run_dir / "campaign-results.json",
        run_dir / "inputs",
        run_dir / "jobs",
    }
    transform = cast(dict[str, object], normalized_request["transform"])
    paths.add(Path(cast(str, transform["output"])))
    paths.add(Path(cast(str, transform["manifest"])))

    attempt_names = (
        "stdout.log",
        "stderr.log",
        "raw.json",
        "result.json",
        "scratch",
        "scratch/06_mega.json",
        "scratch/bridge.duckdb",
    )
    for job in parsed_jobs:
        job_dir = safe_io.job_directory(run_dir, job.job_id)
        paths.update({job_dir, job_dir / "status.json", job_dir / "attempts"})
        for attempt in (1, 9999):
            attempt_dir = safe_io.attempt_directory(run_dir, job.job_id, attempt)
            paths.add(attempt_dir)
            paths.update(attempt_dir / name for name in attempt_names)

        status_path = job_dir / "status.json"
        for name in safe_io.atomic_write_lock_parent_entries(status_path, lock_held=True):
            paths.add(job_dir.parent / name)

    deepest = safe_io.attempt_directory(run_dir, parsed_jobs[-1].job_id, 9999) / "scratch"
    for component in safe_io.internal_path_limit_components():
        paths.add(deepest / component)
    paths.update(
        {
            run_dir / PROBE_ROOT_NAME,
            run_dir / PROBE_ROOT_NAME / f"probe-{'0' * 32}",
            run_dir / PROBE_ROOT_NAME / f"probe-{'0' * 32}" / "left" / "exchange-a",
            run_dir / PROBE_ROOT_NAME / f"probe-{'0' * 32}" / "right" / "cross-final",
            run_dir / safe_io.RETIRE_ROOT_NAME / f"retired-{'0' * 32}" / "entry",
            run_dir / safe_io.LOCK_ROOT_NAME / ("0" * 64) / f"released-{'0' * 32}" / "owner.json",
        }
    )
    for name in safe_io.campaign_lock_parent_entries(run_dir, lock_held=True):
        paths.add(run_dir.parent / name)
    return tuple(sorted(paths, key=lambda path: os.fsencode(str(path))))


def _decode_mountinfo_field(value: str) -> str:
    return _MOUNT_ESCAPE_RE.sub(lambda match: chr(int(match.group(1), 8)), value)


def filesystem_identity(
    target_directory: os.PathLike[str] | str,
    *,
    mountinfo_path: os.PathLike[str] | str = "/proc/self/mountinfo",
) -> dict[str, object]:
    """Return the Linux mount identity that owns an existing target directory."""

    target = safe_io.inspect_path(target_directory, require_kind="directory")
    opened = os.lstat(target)
    try:
        text = Path(mountinfo_path).read_text(encoding="utf-8", errors="strict")
    except (OSError, UnicodeError) as exc:
        raise model.CampaignError(f"cannot read Linux mount identities: {exc}") from exc
    matches: list[tuple[int, list[str], int]] = []
    for line_number, line in enumerate(text.splitlines(), start=1):
        fields = line.split()
        try:
            separator = fields.index("-")
        except ValueError:
            continue
        if separator < 6 or len(fields) < separator + 4:
            raise model.CampaignError(f"malformed mountinfo record on line {line_number}")
        mount_point = Path(_decode_mountinfo_field(fields[4]))
        if not mount_point.is_absolute():
            raise model.CampaignError(f"mountinfo line {line_number} has a relative mount point")
        if target == mount_point or target.is_relative_to(mount_point):
            matches.append((len(os.fsencode(str(mount_point))), fields, separator))
    if not matches:
        raise model.CampaignError(f"no Linux mount identity contains target: {target}")
    _length, fields, separator = max(matches, key=lambda item: item[0])
    try:
        mount_id = int(fields[0])
        parent_mount_id = int(fields[1])
        device_major, device_minor = (int(value) for value in fields[2].split(":", 1))
    except (TypeError, ValueError) as exc:
        raise model.CampaignError("selected mountinfo identity is malformed") from exc
    if (os.major(opened.st_dev), os.minor(opened.st_dev)) != (device_major, device_minor):
        raise model.CampaignError("mountinfo device identity differs from the target inode")
    return {
        "mount_id": mount_id,
        "parent_mount_id": parent_mount_id,
        "device": fields[2],
        "st_dev": opened.st_dev,
        "mount_root": _decode_mountinfo_field(fields[3]),
        "mount_point": _decode_mountinfo_field(fields[4]),
        "mount_options": fields[5].split(","),
        "optional_fields": fields[6:separator],
        "filesystem_type": fields[separator + 1],
        "source": _decode_mountinfo_field(fields[separator + 2]),
        "super_options": fields[separator + 3].split(","),
    }


def _filesystem_key(identity: Mapping[str, object]) -> tuple[int, int]:
    mount_id = identity.get("mount_id")
    device = identity.get("st_dev")
    if type(mount_id) is not int or mount_id < 1:
        raise model.CampaignError("filesystem mount_id must be a positive integer")
    if type(device) is not int or device < 0:
        raise model.CampaignError("filesystem st_dev must be a non-negative integer")
    return mount_id, device


def qualify_mutable_filesystems(
    target_paths: Mapping[Path, Sequence[Path]],
    *,
    identity_provider: FilesystemIdentityProvider = filesystem_identity,
    capability_probe: CapabilityProbe | None = None,
) -> list[dict[str, object]]:
    """Probe each distinct mutable mount once and fence its identity and limits."""

    if not isinstance(target_paths, Mapping) or not target_paths:
        raise model.CampaignError("filesystem qualification requires mutable targets")
    records: dict[Path, tuple[dict[str, object], tuple[Path, ...]]] = {}
    groups: dict[tuple[int, int], dict[str, object]] = {}
    for raw_target, raw_paths in sorted(target_paths.items(), key=lambda item: str(item[0])):
        target = safe_io.inspect_path(raw_target, require_kind="directory")
        paths = tuple(
            sorted(
                {safe_io.lexical_absolute(path, "future campaign path") for path in raw_paths},
                key=lambda path: os.fsencode(str(path)),
            )
        )
        if not paths:
            raise model.CampaignError(f"mutable target has no future paths: {target}")
        identity = dict(identity_provider(target))
        key = _filesystem_key(identity)
        records[target] = (identity, paths)
        group = groups.setdefault(
            key,
            {"identity": identity, "targets": [], "paths": []},
        )
        if model.canonical_json_bytes(group["identity"]) != model.canonical_json_bytes(identity):
            raise model.CampaignError("one mount/device key produced conflicting identities")
        cast(list[Path], group["targets"]).append(target)
        cast(list[Path], group["paths"]).extend(paths)

    probe = capability_probe or probe_filesystem_capabilities
    evidence: list[dict[str, object]] = []
    for key in sorted(groups):
        group = groups[key]
        targets = cast(list[Path], group["targets"])
        representative = targets[0]
        capabilities = model.require_exact_keys(
            probe(representative),
            _CAPABILITY_KEYS,
            "filesystem capability evidence",
        )
        if capabilities["st_dev"] != key[1]:
            raise model.CampaignError("filesystem probe device differs from mount identity")
        for capability in _CAPABILITY_KEYS - {"probe_path", "st_dev", "name_max", "path_max"}:
            if capabilities[capability] is not True:
                raise model.CampaignError(f"filesystem capability {capability!r} did not pass")
        paths = tuple(
            sorted(set(cast(list[Path], group["paths"])), key=lambda path: os.fsencode(str(path)))
        )
        path_budget = validate_encoded_path_budget(
            paths,
            name_max=cast(int, capabilities["name_max"]),
            path_max=cast(int, capabilities["path_max"]),
        )
        evidence.append(
            {
                "identity": group["identity"],
                "mutable_targets": [str(target) for target in targets],
                "probe_target": str(representative),
                "capabilities": capabilities,
                "path_budget": path_budget,
            }
        )

    for target, (before, _paths) in records.items():
        after = dict(identity_provider(target))
        if model.canonical_json_bytes(after) != model.canonical_json_bytes(before):
            raise model.CampaignError(f"filesystem identity changed during probes: {target}")
    return evidence


def _fsync_directory(descriptor: int) -> None:
    before = os.fstat(descriptor)
    if not stat.S_ISDIR(before.st_mode):
        raise model.CampaignError("filesystem probe descriptor is not a directory")
    try:
        os.fsync(descriptor)
    except OSError as exc:
        raise model.CampaignError(f"directory fsync failed: {exc}") from exc
    after = os.fstat(descriptor)
    if (after.st_dev, after.st_ino) != (before.st_dev, before.st_ino):
        raise model.CampaignError("filesystem probe directory identity changed during fsync")


def ensure_campaign_root(path: os.PathLike[str] | str) -> Path:
    """Create a missing campaign-root tail through no-follow directory descriptors."""

    target = safe_io.lexical_absolute(path, "campaign root")
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
    try:
        descriptor = os.open(os.sep, flags)
    except OSError as exc:
        raise model.CampaignError(f"cannot open filesystem root: {exc}") from exc
    current = Path(os.sep)
    try:
        for component in (part for part in target.parts if part != os.sep):
            current /= component
            item = safe_io._lstat_at(descriptor, component)
            created = False
            if item is None:
                try:
                    os.mkdir(component, mode=0o700, dir_fd=descriptor)
                except FileExistsError:
                    pass
                except OSError as exc:
                    raise model.CampaignError(
                        f"cannot create campaign-root component {current}: {exc}"
                    ) from exc
                else:
                    created = True
                    _fsync_directory(descriptor)
                item = safe_io._lstat_at(descriptor, component)
            if item is None:
                raise model.CampaignError(f"campaign-root component disappeared: {current}")
            if stat.S_ISLNK(item.st_mode):
                raise model.CampaignError(f"symlink path component is not allowed: {current}")
            if not stat.S_ISDIR(item.st_mode):
                raise model.CampaignError(f"campaign-root component is not a directory: {current}")
            if created and stat.S_IMODE(item.st_mode) != 0o700:
                raise model.CampaignError(
                    f"new campaign-root component is not private mode 0700: {current}"
                )
            try:
                child_descriptor = os.open(component, flags, dir_fd=descriptor)
            except OSError as exc:
                raise model.CampaignError(
                    f"cannot open campaign-root component {current}: {exc}"
                ) from exc
            try:
                opened = os.fstat(child_descriptor)
                if (opened.st_dev, opened.st_ino) != (item.st_dev, item.st_ino):
                    raise model.CampaignError(
                        f"campaign-root component changed while opening: {current}"
                    )
            except BaseException:
                safe_io._close_descriptor(child_descriptor)
                raise
            safe_io._close_descriptor(descriptor)
            descriptor = child_descriptor
        _fsync_directory(descriptor)
        opened = os.fstat(descriptor)
        safe_io._assert_parent_identity(target, descriptor, (opened.st_dev, opened.st_ino))
    finally:
        safe_io._close_descriptor(descriptor)
    return safe_io.inspect_path(target, require_kind="directory")


def create_private_run_directory(
    campaign_root: os.PathLike[str] | str,
    run_id: str,
) -> Path:
    """Exclusively create and durably bind a private incomplete run directory."""

    root = safe_io.inspect_path(campaign_root, require_kind="directory")
    name = model.validate_identifier(run_id, "run id")
    run_dir = root / name
    with safe_io._open_directory(root) as root_descriptor:
        root_opened = os.fstat(root_descriptor)
        root_identity = (root_opened.st_dev, root_opened.st_ino)
        try:
            os.mkdir(name, mode=0o700, dir_fd=root_descriptor)
        except FileExistsError as exc:
            raise model.CampaignError(f"run directory already exists: {run_dir}") from exc
        except OSError as exc:
            raise model.CampaignError(f"cannot create run directory {run_dir}: {exc}") from exc
        _fsync_directory(root_descriptor)
        item = safe_io._lstat_at(root_descriptor, name)
        if item is None or not stat.S_ISDIR(item.st_mode) or stat.S_IMODE(item.st_mode) != 0o700:
            raise model.CampaignError("new run directory is not a private mode-0700 directory")
        flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
        try:
            run_descriptor = os.open(name, flags, dir_fd=root_descriptor)
        except OSError as exc:
            raise model.CampaignError(f"cannot open new run directory {run_dir}: {exc}") from exc
        try:
            opened = os.fstat(run_descriptor)
            if (opened.st_dev, opened.st_ino) != (item.st_dev, item.st_ino):
                raise model.CampaignError("run directory changed while it was opened")
            _fsync_directory(run_descriptor)
            safe_io._require_directory_entry_identity(
                root_descriptor,
                name,
                (item.st_dev, item.st_ino),
                "run directory",
            )
            safe_io._assert_parent_identity(root, root_descriptor, root_identity)
        finally:
            safe_io._close_descriptor(run_descriptor)
    return safe_io.inspect_path(run_dir, require_kind="directory")


def _create_private_probe_directory(
    parent_descriptor: int,
    name: str,
    label: str,
) -> tuple[int, tuple[int, int]]:
    try:
        os.mkdir(name, mode=0o700, dir_fd=parent_descriptor)
    except FileExistsError as exc:
        raise model.CampaignError(f"{label} already exists") from exc
    except OSError as exc:
        raise model.CampaignError(f"cannot create {label}: {exc}") from exc
    _fsync_directory(parent_descriptor)
    item = safe_io._lstat_at(parent_descriptor, name)
    if item is None or not stat.S_ISDIR(item.st_mode) or stat.S_IMODE(item.st_mode) != 0o700:
        raise model.CampaignError(f"{label} is not a private mode-0700 directory")
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
    try:
        descriptor = os.open(name, flags, dir_fd=parent_descriptor)
    except OSError as exc:
        raise model.CampaignError(f"cannot open {label}: {exc}") from exc
    try:
        opened = os.fstat(descriptor)
        if (opened.st_dev, opened.st_ino) != (item.st_dev, item.st_ino):
            raise model.CampaignError(f"{label} changed while it was opened")
    except BaseException:
        safe_io._close_descriptor(descriptor)
        raise
    return descriptor, (item.st_dev, item.st_ino)


def _probe_file(parent_descriptor: int, name: str, payload: bytes) -> os.stat_result:
    identity = safe_io._create_bytes_at(parent_descriptor, name, payload)
    item = safe_io._require_entry_identity(
        parent_descriptor,
        name,
        identity,
        f"filesystem probe file {name}",
    )
    return item


def _require_probe_bytes(
    parent_descriptor: int,
    name: str,
    expected: bytes,
    identity: tuple[int, int],
) -> None:
    data, item = safe_io._read_file_at(parent_descriptor, name, max_bytes=4096)
    if data != expected or (item.st_dev, item.st_ino) != identity:
        raise model.CampaignError(f"filesystem probe file changed: {name}")


def probe_filesystem_capabilities(
    target_directory: os.PathLike[str] | str,
    *,
    token_provider: TokenProvider = lambda: secrets.token_hex(16),
) -> dict[str, object]:
    """Exercise required durable rename primitives and retain probe evidence."""

    target = safe_io.inspect_path(target_directory, require_kind="directory")
    token = token_provider()
    if type(token) is not str or _TOKEN_RE.fullmatch(token) is None:
        raise model.CampaignError("filesystem probe token must be 32 lowercase hex characters")
    namespace_name = f"probe-{token}"
    target_inspected = os.lstat(target)
    target_identity = (target_inspected.st_dev, target_inspected.st_ino)
    with safe_io._open_directory(target) as target_descriptor:
        target_opened = os.fstat(target_descriptor)
        if (target_opened.st_dev, target_opened.st_ino) != target_identity:
            raise model.CampaignError("filesystem probe target changed while opening")
        probe_root_descriptor, probe_root_identity = safe_io._open_or_create_private_directory_at(
            target_descriptor,
            PROBE_ROOT_NAME,
            "filesystem probe root",
        )
        namespace_descriptor = -1
        left_descriptor = -1
        right_descriptor = -1
        try:
            namespace_descriptor, _namespace_identity = _create_private_probe_directory(
                probe_root_descriptor,
                namespace_name,
                "filesystem probe namespace",
            )
            left_descriptor, _left_identity = _create_private_probe_directory(
                namespace_descriptor,
                "left",
                "filesystem probe left directory",
            )
            right_descriptor, _right_identity = _create_private_probe_directory(
                namespace_descriptor,
                "right",
                "filesystem probe right directory",
            )
            _fsync_directory(namespace_descriptor)

            same = _probe_file(left_descriptor, "noreplace-source", b"same-directory")
            safe_io._rename_noreplace_at(
                left_descriptor,
                "noreplace-source",
                left_descriptor,
                "noreplace-final",
            )
            _require_probe_bytes(
                left_descriptor,
                "noreplace-final",
                b"same-directory",
                (same.st_dev, same.st_ino),
            )
            _fsync_directory(left_descriptor)

            collision_source = _probe_file(left_descriptor, "collision-source", b"source")
            collision_final = _probe_file(left_descriptor, "collision-final", b"destination")
            try:
                safe_io._rename_noreplace_at(
                    left_descriptor,
                    "collision-source",
                    left_descriptor,
                    "collision-final",
                )
            except FileExistsError:
                pass
            else:
                raise model.CampaignError("no-replace collision unexpectedly replaced a file")
            _require_probe_bytes(
                left_descriptor,
                "collision-source",
                b"source",
                (collision_source.st_dev, collision_source.st_ino),
            )
            _require_probe_bytes(
                left_descriptor,
                "collision-final",
                b"destination",
                (collision_final.st_dev, collision_final.st_ino),
            )

            cross = _probe_file(left_descriptor, "cross-source", b"cross-directory")
            safe_io._rename_noreplace_at(
                left_descriptor,
                "cross-source",
                right_descriptor,
                "cross-final",
            )
            _require_probe_bytes(
                right_descriptor,
                "cross-final",
                b"cross-directory",
                (cross.st_dev, cross.st_ino),
            )
            _fsync_directory(left_descriptor)
            _fsync_directory(right_descriptor)

            exchange_a = _probe_file(left_descriptor, "exchange-a", b"exchange-a")
            exchange_b = _probe_file(left_descriptor, "exchange-b", b"exchange-b")
            safe_io._rename_exchange_at(
                left_descriptor,
                "exchange-a",
                left_descriptor,
                "exchange-b",
            )
            _require_probe_bytes(
                left_descriptor,
                "exchange-a",
                b"exchange-b",
                (exchange_b.st_dev, exchange_b.st_ino),
            )
            _require_probe_bytes(
                left_descriptor,
                "exchange-b",
                b"exchange-a",
                (exchange_a.st_dev, exchange_a.st_ino),
            )
            _fsync_directory(left_descriptor)
            safe_io._rename_exchange_at(
                left_descriptor,
                "exchange-a",
                left_descriptor,
                "exchange-b",
            )
            _fsync_directory(left_descriptor)
            _require_probe_bytes(
                left_descriptor,
                "exchange-a",
                b"exchange-a",
                (exchange_a.st_dev, exchange_a.st_ino),
            )
            _require_probe_bytes(
                left_descriptor,
                "exchange-b",
                b"exchange-b",
                (exchange_b.st_dev, exchange_b.st_ino),
            )

            _fsync_directory(namespace_descriptor)
            _fsync_directory(probe_root_descriptor)
            _fsync_directory(target_descriptor)
            safe_io._require_directory_entry_identity(
                target_descriptor,
                PROBE_ROOT_NAME,
                probe_root_identity,
                "filesystem probe root",
            )
            safe_io._assert_parent_identity(target, target_descriptor, target_identity)
            name_max = os.pathconf(target_descriptor, "PC_NAME_MAX")
            path_max = os.pathconf(target_descriptor, "PC_PATH_MAX")
            if (
                type(name_max) is not int
                or name_max < 1
                or type(path_max) is not int
                or path_max < 2
            ):
                raise model.CampaignError("filesystem returned invalid path limits")
            return {
                "probe_path": str(target / PROBE_ROOT_NAME / namespace_name),
                "st_dev": target_opened.st_dev,
                "name_max": name_max,
                "path_max": path_max,
                "directory_fsync": True,
                "rename_noreplace_same_directory": True,
                "rename_noreplace_collision": True,
                "rename_noreplace_cross_directory": True,
                "rename_exchange_same_directory": True,
                "parents_fsynced": True,
            }
        finally:
            safe_io._close_descriptor(right_descriptor)
            safe_io._close_descriptor(left_descriptor)
            safe_io._close_descriptor(namespace_descriptor)
            safe_io._close_descriptor(probe_root_descriptor)


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


def _require_private_directory(path: Path, label: str) -> None:
    item = os.lstat(safe_io.inspect_path(path, require_kind="directory"))
    if stat.S_IMODE(item.st_mode) != 0o700:
        raise model.CampaignError(f"{label} must have private mode 0700")


def _require_private_files(entries: Mapping[str, os.stat_result], label: str) -> None:
    for name, item in entries.items():
        if stat.S_IMODE(item.st_mode) != 0o600:
            raise model.CampaignError(f"{label} file {name!r} must have private mode 0600")


def _require_file_bytes(path: Path, expected: bytes, label: str) -> None:
    with safe_io._open_parent(path) as (descriptor, name, parent, parent_identity):
        data, item = safe_io._read_file_at(descriptor, name, max_bytes=4096)
        safe_io._assert_parent_identity(parent, descriptor, parent_identity)
        safe_io._require_entry_identity(descriptor, name, item, label, compare_version=True)
    if data != expected:
        raise model.CampaignError(f"{label} bytes differ from the retained probe evidence")


def _validate_probe_tree(run_dir: Path) -> None:
    probe_root = run_dir / PROBE_ROOT_NAME
    _require_private_directory(probe_root, "filesystem probe root")
    namespaces = _directory_names(probe_root, "filesystem probe root")
    if not namespaces or any(_PROBE_NAMESPACE_RE.fullmatch(name) is None for name in namespaces):
        raise model.CampaignError("filesystem probe root has unexpected namespace entries")
    safe_io.exact_directory_scan(probe_root, {name: "directory" for name in namespaces})
    left_payloads = {
        "noreplace-final": b"same-directory",
        "collision-source": b"source",
        "collision-final": b"destination",
        "exchange-a": b"exchange-a",
        "exchange-b": b"exchange-b",
    }
    for namespace_name in sorted(namespaces):
        namespace = probe_root / namespace_name
        _require_private_directory(namespace, "filesystem probe namespace")
        safe_io.exact_directory_scan(namespace, {"left": "directory", "right": "directory"})
        left = namespace / "left"
        right = namespace / "right"
        _require_private_directory(left, "filesystem probe left directory")
        _require_private_directory(right, "filesystem probe right directory")
        left_entries = safe_io.exact_directory_scan(
            left,
            {name: "file" for name in left_payloads},
        )
        right_entries = safe_io.exact_directory_scan(right, {"cross-final": "file"})
        _require_private_files(left_entries, "filesystem probe")
        _require_private_files(right_entries, "filesystem probe")
        for name, payload in left_payloads.items():
            _require_file_bytes(left / name, payload, f"filesystem probe {name}")
        _require_file_bytes(right / "cross-final", b"cross-directory", "filesystem cross probe")


def _validate_lock_history(run_dir: Path) -> None:
    lock_root = run_dir / safe_io.LOCK_ROOT_NAME
    _require_private_directory(lock_root, "campaign lock-history root")
    namespaces = _directory_names(lock_root, "campaign lock-history root")
    if not namespaces or any(_SHA256_RE.fullmatch(name) is None for name in namespaces):
        raise model.CampaignError("campaign lock-history root has unexpected namespaces")
    safe_io.exact_directory_scan(lock_root, {name: "directory" for name in namespaces})
    for namespace_name in sorted(namespaces):
        namespace = lock_root / namespace_name
        _require_private_directory(namespace, "campaign lock-history namespace")
        released = _directory_names(namespace, "campaign lock-history namespace")
        if not released or any(_RELEASED_LOCK_RE.fullmatch(name) is None for name in released):
            raise model.CampaignError("campaign lock-history namespace has unexpected entries")
        safe_io.exact_directory_scan(namespace, {name: "directory" for name in released})
        for released_name in sorted(released):
            released_dir = namespace / released_name
            _require_private_directory(released_dir, "released campaign lock")
            entries = safe_io.exact_directory_scan(released_dir, {"owner.json": "file"})
            _require_private_files(entries, "released campaign lock")
            safe_io.strict_json_load(
                released_dir / "owner.json", validator=safe_io.validate_lock_owner
            )


def _validate_retirement_tree(run_dir: Path) -> None:
    retirement_root = run_dir / safe_io.RETIRE_ROOT_NAME
    _require_private_directory(retirement_root, "retirement root")
    namespaces = _directory_names(retirement_root, "retirement root")
    if not namespaces or any(_RETIRED_RE.fullmatch(name) is None for name in namespaces):
        raise model.CampaignError("retirement root has unexpected entries")
    safe_io.exact_directory_scan(retirement_root, {name: "directory" for name in namespaces})
    for namespace_name in sorted(namespaces):
        namespace = retirement_root / namespace_name
        _require_private_directory(namespace, "retirement namespace")
        entries = safe_io.exact_directory_scan(namespace, {"entry": "file"})
        _require_private_files(entries, "retirement namespace")


def validate_run_tree(
    run_directory: os.PathLike[str] | str,
    *,
    transform_mode: str,
    phase: str,
) -> None:
    """Validate the closed run namespace for one transactional commit phase."""

    if transform_mode not in {"prepare", "external"}:
        raise model.CampaignError(f"invalid run-tree transform mode: {transform_mode!r}")
    if phase not in {"probed", "campaign", "complete", "runtime"}:
        raise model.CampaignError(f"invalid run-tree phase: {phase!r}")
    run_dir = safe_io.inspect_path(run_directory, require_kind="directory")
    _require_private_directory(run_dir, "campaign run directory")
    names = _directory_names(run_dir, "campaign run directory")
    expected: dict[str, str] = {PROBE_ROOT_NAME: "directory"}
    if transform_mode == "prepare":
        expected["inputs"] = "directory"
    for optional_root in (safe_io.LOCK_ROOT_NAME, safe_io.RETIRE_ROOT_NAME):
        if optional_root in names:
            expected[optional_root] = "directory"
    if phase in {"campaign", "complete", "runtime"}:
        expected["campaign.json"] = "file"
    if phase in {"complete", "runtime"}:
        expected["preflight.json"] = "file"
    if phase == "runtime":
        runtime_entries = {
            "jobs": "directory",
            "launches": "directory",
            "campaign-results.json": "file",
        }
        for name, kind in runtime_entries.items():
            if name in names:
                expected[name] = kind
    entries = safe_io.exact_directory_scan(run_dir, expected)
    for name in ("campaign.json", "preflight.json"):
        if name in entries:
            _require_private_files({name: entries[name]}, "campaign marker")
    _validate_probe_tree(run_dir)
    if transform_mode == "prepare":
        inputs = run_dir / "inputs"
        _require_private_directory(inputs, "prepared-transform directory")
        input_entries = safe_io.exact_directory_scan(
            inputs,
            {
                "parent-stripped.gtf.gz": "file",
                "parent-stripped.manifest.json": "file",
            },
        )
        _require_private_files(input_entries, "prepared transform")
    if safe_io.LOCK_ROOT_NAME in names:
        _validate_lock_history(run_dir)
    if safe_io.RETIRE_ROOT_NAME in names:
        _validate_retirement_tree(run_dir)


def _validate_parameters(value: object) -> dict[str, object]:
    expected = model.binding_parameters()
    record = model.require_exact_keys(value, set(expected), "preflight parameters")
    for key, expected_value in expected.items():
        actual = record[key]
        if type(actual) is not type(expected_value) or actual != expected_value:
            raise model.CampaignError(
                f"preflight parameter {key!r} must be {expected_value!r}, found {actual!r}"
            )
    return record


def normalize_request(
    *,
    run_id: str,
    campaign_root: os.PathLike[str] | str,
    candidate_wheel: os.PathLike[str] | str,
    interpreters: Mapping[str, os.PathLike[str] | str],
    transform_mode: str,
    parent_stripped: os.PathLike[str] | str | None,
    parent_stripped_manifest: os.PathLike[str] | str | None,
    parameters: Mapping[str, object],
) -> dict[str, object]:
    """Purely normalize the complete immutable preflight request."""

    normalized_run_id = model.validate_identifier(run_id, "run id")
    root = Path(_path_argument(campaign_root, "campaign root"))
    run_dir = root / normalized_run_id
    interpreter_values = model.require_exact_keys(
        interpreters,
        set(model.INTERPRETER_ROLES),
        "preflight interpreters",
    )
    normalized_interpreters = {
        role: _path_argument(interpreter_values[role], f"{role} interpreter")
        for role in model.INTERPRETER_ROLES
    }
    if transform_mode == "prepare":
        if parent_stripped is not None or parent_stripped_manifest is not None:
            raise model.CampaignError(
                "prepared transform mode derives output paths and accepts no external paths"
            )
        output = run_dir / "inputs" / "parent-stripped.gtf.gz"
        manifest = run_dir / "inputs" / "parent-stripped.manifest.json"
    elif transform_mode == "external":
        if parent_stripped is None or parent_stripped_manifest is None:
            raise model.CampaignError("external transform mode requires output and manifest paths")
        output = Path(_path_argument(parent_stripped, "parent-stripped GTF"))
        manifest = Path(_path_argument(parent_stripped_manifest, "parent-stripped manifest"))
    else:
        raise model.CampaignError(f"unknown transform mode: {transform_mode!r}")
    request = {
        "schema_version": REQUEST_SCHEMA,
        "run_id": normalized_run_id,
        "campaign_root": str(root),
        "run_dir": str(run_dir),
        "candidate_wheel": _path_argument(candidate_wheel, "candidate wheel"),
        "interpreters": normalized_interpreters,
        "transform": {
            "mode": transform_mode,
            "output": str(output),
            "manifest": str(manifest),
        },
        "parameters": _validate_parameters(parameters),
    }
    return validate_request(request)


def validate_request(value: object) -> dict[str, object]:
    """Validate the exact normalized request shape without probing the host."""

    request = model.require_exact_keys(value, _REQUEST_KEYS, "preflight request")
    model.require_schema(request, REQUEST_SCHEMA, "preflight request")
    run_id = model.validate_identifier(cast(str, request["run_id"]), "run id")
    campaign_root = Path(_canonical_path(request["campaign_root"], "campaign root"))
    run_dir = _canonical_path(request["run_dir"], "run directory")
    if run_dir != str(campaign_root / run_id):
        raise model.CampaignError("preflight run directory is not root/run_id")
    _canonical_path(request["candidate_wheel"], "candidate wheel")
    interpreters = model.require_exact_keys(
        request["interpreters"],
        set(model.INTERPRETER_ROLES),
        "preflight interpreters",
    )
    for role in model.INTERPRETER_ROLES:
        _canonical_path(interpreters[role], f"{role} interpreter")
    transform = model.require_exact_keys(
        request["transform"],
        _TRANSFORM_KEYS,
        "preflight transform",
    )
    mode = transform["mode"]
    if type(mode) is not str or mode not in {"prepare", "external"}:
        raise model.CampaignError("preflight transform mode is invalid")
    output = _canonical_path(transform["output"], "parent-stripped GTF")
    manifest = _canonical_path(transform["manifest"], "parent-stripped manifest")
    if output == manifest:
        raise model.CampaignError("transform output and manifest paths must be distinct")
    if mode == "prepare":
        expected_output = str(Path(run_dir) / "inputs" / "parent-stripped.gtf.gz")
        expected_manifest = str(Path(run_dir) / "inputs" / "parent-stripped.manifest.json")
        if (output, manifest) != (expected_output, expected_manifest):
            raise model.CampaignError("prepared transform paths are not the derived run paths")
    _validate_parameters(request["parameters"])
    return json.loads(model.canonical_json_bytes(request))


def _validate_timestamp(value: object, label: str) -> str:
    if type(value) is not str or _UTC_RE.fullmatch(value) is None:
        raise model.CampaignError(f"{label} must be an integral-second UTC timestamp")
    try:
        parsed = datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ")
    except ValueError as exc:
        raise model.CampaignError(f"{label} is not a real UTC timestamp") from exc
    if parsed.strftime("%Y-%m-%dT%H:%M:%SZ") != value:
        raise model.CampaignError(f"{label} is not canonical")
    return value


def _require_sha256(value: object, label: str) -> str:
    if type(value) is not str or _SHA256_RE.fullmatch(value) is None:
        raise model.CampaignError(f"{label} must be a lowercase SHA-256 digest")
    return value


def _require_string_list(value: object, label: str, *, nonempty: bool = False) -> list[str]:
    if type(value) is not list or (nonempty and not value):
        raise model.CampaignError(f"{label} must be a{' non-empty' if nonempty else ''} array")
    if any(type(item) is not str or not item for item in value):
        raise model.CampaignError(f"{label} must contain non-empty strings")
    return cast(list[str], value)


def _validate_repo_identity(value: object, label: str) -> dict[str, object]:
    record = model.require_exact_keys(value, _REPO_IDENTITY_KEYS, label)
    _canonical_path(record["root"], f"{label}.root")
    commit = record["commit"]
    if type(commit) is not str or _GIT_COMMIT_RE.fullmatch(commit) is None:
        raise model.CampaignError(f"{label}.commit is invalid")
    branch = record["branch"]
    if type(branch) is not str or not branch or any(ord(char) < 32 for char in branch):
        raise model.CampaignError(f"{label}.branch is invalid")
    if record["dirty"] is not False:
        raise model.CampaignError(f"{label} is not clean")
    if _require_string_list(record["status"], f"{label}.status"):
        raise model.CampaignError(f"{label}.status is not empty")
    if _require_string_list(record["dirty_submodules"], f"{label}.dirty_submodules"):
        raise model.CampaignError(f"{label}.dirty_submodules is not empty")
    return record


def _validate_wheel_identity(value: object, label: str) -> dict[str, object]:
    record = model.require_exact_keys(value, _WHEEL_IDENTITY_KEYS, label)
    path = _canonical_path(record["path"], f"{label}.path")
    if type(record["name"]) is not str or record["name"] != Path(path).name:
        raise model.CampaignError(f"{label}.name does not match its path")
    if type(record["bytes"]) is not int or record["bytes"] < 1:
        raise model.CampaignError(f"{label}.bytes is invalid")
    _require_sha256(record["sha256"], f"{label}.sha256")
    if record["metadata_version"] != model.PUBLIC_VERSION:
        raise model.CampaignError(f"{label}.metadata_version is invalid")
    return record


def _validate_interpreter_snapshot(value: object, role: str, label: str) -> dict[str, object]:
    record = model.require_exact_keys(value, _INTERPRETER_PROBE_KEYS, label)
    if record["role"] != role:
        raise model.CampaignError(f"{label}.role is invalid")
    for key in ("resolved_executable", "requested_executable", "prefix"):
        _canonical_path(record[key], f"{label}.{key}")
    if record["implementation"] != "CPython":
        raise model.CampaignError(f"{label}.implementation is invalid")
    version = record["python_version"]
    if type(version) is not str or re.fullmatch(r"\d+\.\d+\.\d+", version) is None:
        raise model.CampaignError(f"{label}.python_version is invalid")
    if record["isolated"] != 1 or type(record["isolated"]) is not int:
        raise model.CampaignError(f"{label}.isolated is invalid")
    executable = model.require_exact_keys(
        record["executable_identity"],
        _EXECUTABLE_IDENTITY_KEYS,
        f"{label}.executable_identity",
    )
    for key, number in executable.items():
        if type(number) is not int or number < 0:
            raise model.CampaignError(f"{label}.executable_identity.{key} is invalid")
    for path in _require_string_list(record["sys_path"], f"{label}.sys_path", nonempty=True):
        _canonical_path(path, f"{label}.sys_path entry")
    package_names = {"gffbase", "gffutils", "duckdb", "pyarrow", "psutil", "numpy"}
    packages = model.require_exact_keys(record["packages"], package_names, f"{label}.packages")
    if any(value is not None and type(value) is not str for value in packages.values()):
        raise model.CampaignError(f"{label}.packages contains an invalid version")
    modules = record["modules"]
    if not isinstance(modules, Mapping) or not modules:
        raise model.CampaignError(f"{label}.modules must be a non-empty object")
    for name, item in modules.items():
        if type(name) is not str or not name:
            raise model.CampaignError(f"{label}.modules contains an invalid name")
        module = model.require_exact_keys(item, _MODULE_IDENTITY_KEYS, f"{label}.modules.{name}")
        _canonical_path(module["path"], f"{label}.modules.{name}.path")
        if module["version"] is not None and type(module["version"]) is not str:
            raise model.CampaignError(f"{label}.modules.{name}.version is invalid")
        _require_sha256(module["sha256"], f"{label}.modules.{name}.sha256")
    distributions = record["distribution_roots"]
    if not isinstance(distributions, Mapping):
        raise model.CampaignError(f"{label}.distribution_roots must be an object")
    for package, path in distributions.items():
        if package not in package_names:
            raise model.CampaignError(f"{label}.distribution_roots has an unknown package")
        _canonical_path(path, f"{label}.distribution_roots.{package}")
    direct_urls = model.require_exact_keys(
        record["direct_url"], package_names, f"{label}.direct_url"
    )
    if any(value is not None and type(value) is not str for value in direct_urls.values()):
        raise model.CampaignError(f"{label}.direct_url contains an invalid value")
    pth_files = record["pth_files"]
    if type(pth_files) is not list:
        raise model.CampaignError(f"{label}.pth_files must be an array")
    for index, item in enumerate(pth_files):
        pth = model.require_exact_keys(item, _PTH_IDENTITY_KEYS, f"{label}.pth_files[{index}]")
        _canonical_path(pth["path"], f"{label}.pth_files[{index}].path")
        _require_sha256(pth["sha256"], f"{label}.pth_files[{index}].sha256")
        _require_string_list(pth["active_lines"], f"{label}.pth_files[{index}].active_lines")
    expected_digest = _require_sha256(record["probe_sha256"], f"{label}.probe_sha256")
    unsigned = dict(record)
    unsigned.pop("probe_sha256")
    if model.sha256_json(unsigned) != expected_digest:
        raise model.CampaignError(f"{label}.probe_sha256 does not bind the probe")
    return record


def _validate_resource_snapshot(value: object, label: str) -> dict[str, object]:
    record = model.require_exact_keys(value, _RESOURCE_KEYS, label)
    if record["platform"] != "Linux" or type(record["machine"]) is not str:
        raise model.CampaignError(f"{label} platform identity is invalid")
    for key in ("allowed_cpus", "online_cpus"):
        values = record[key]
        if type(values) is not list or any(type(item) is not int or item < 0 for item in values):
            raise model.CampaignError(f"{label}.{key} is invalid")
        if len(values) != len(set(values)):
            raise model.CampaignError(f"{label}.{key} contains duplicates")
    if not isinstance(record["physical_cpu_ids"], Mapping) or not isinstance(
        record["numa_nodes"], Mapping
    ):
        raise model.CampaignError(f"{label} CPU topology objects are invalid")
    for key in ("free_bytes", "available_ram_bytes"):
        if type(record[key]) is not int or record[key] < 1:
            raise model.CampaignError(f"{label}.{key} is invalid")
    mount = model.require_exact_keys(record["mount"], _MOUNT_KEYS, f"{label}.mount")
    for key, item in mount.items():
        if type(item) is not str or not item:
            raise model.CampaignError(f"{label}.mount.{key} is invalid")
    for key in ("executables", "executable_versions", "executable_identities"):
        mapping = model.require_exact_keys(record[key], _EXECUTABLE_ROLES, f"{label}.{key}")
        if key != "executable_identities":
            if any(type(item) is not str or not item for item in mapping.values()):
                raise model.CampaignError(f"{label}.{key} contains an invalid value")
            continue
        for name, item in mapping.items():
            identity = model.require_exact_keys(
                item,
                {"st_dev", "st_ino", "st_size", "st_mtime_ns", "sha256"},
                f"{label}.executable_identities.{name}",
            )
            for number_key in ("st_dev", "st_ino", "st_size", "st_mtime_ns"):
                if type(identity[number_key]) is not int or identity[number_key] < 0:
                    raise model.CampaignError(
                        f"{label}.executable_identities.{name}.{number_key} is invalid"
                    )
            _require_sha256(identity["sha256"], f"{label}.executable_identities.{name}.sha256")
    thresholds = model.require_exact_keys(
        record["thresholds"], _THRESHOLD_KEYS, f"{label}.thresholds"
    )
    for key in ("free_bytes", "available_ram_bytes"):
        if type(thresholds[key]) is not int or thresholds[key] < 1:
            raise model.CampaignError(f"{label}.thresholds.{key} is invalid")
        if record[key] < thresholds[key]:
            raise model.CampaignError(f"{label}.{key} is below its threshold")
    for key in ("disk_formula", "ram_formula"):
        if type(thresholds[key]) is not str or not thresholds[key]:
            raise model.CampaignError(f"{label}.thresholds.{key} is invalid")
    return record


def _validate_filesystem_evidence(value: object, label: str) -> dict[str, object]:
    record = model.require_exact_keys(value, _FILESYSTEM_EVIDENCE_KEYS, label)
    identity = model.require_exact_keys(
        record["identity"], _FILESYSTEM_IDENTITY_KEYS, f"{label}.identity"
    )
    _filesystem_key(identity)
    if type(identity["parent_mount_id"]) is not int or identity["parent_mount_id"] < 0:
        raise model.CampaignError(f"{label}.identity.parent_mount_id is invalid")
    for key in ("device", "mount_root", "mount_point", "filesystem_type", "source"):
        if type(identity[key]) is not str or not identity[key]:
            raise model.CampaignError(f"{label}.identity.{key} is invalid")
    for key in ("mount_options", "optional_fields", "super_options"):
        _require_string_list(identity[key], f"{label}.identity.{key}")
    targets = _require_string_list(
        record["mutable_targets"], f"{label}.mutable_targets", nonempty=True
    )
    normalized_targets = [_canonical_path(path, f"{label}.mutable target") for path in targets]
    if len(normalized_targets) != len(set(normalized_targets)):
        raise model.CampaignError(f"{label}.mutable_targets contains duplicates")
    probe_target = _canonical_path(record["probe_target"], f"{label}.probe_target")
    if probe_target not in normalized_targets:
        raise model.CampaignError(f"{label}.probe_target is not a mutable target")
    capabilities = model.require_exact_keys(
        record["capabilities"], _CAPABILITY_KEYS, f"{label}.capabilities"
    )
    if capabilities["st_dev"] != identity["st_dev"]:
        raise model.CampaignError(f"{label} capability/device identity mismatch")
    _canonical_path(capabilities["probe_path"], f"{label}.capabilities.probe_path")
    for key in _CAPABILITY_KEYS - {"probe_path", "st_dev", "name_max", "path_max"}:
        if capabilities[key] is not True:
            raise model.CampaignError(f"{label}.capabilities.{key} did not pass")
    budget = model.require_exact_keys(
        record["path_budget"], _PATH_BUDGET_KEYS, f"{label}.path_budget"
    )
    checked = _require_string_list(
        budget["checked_paths"], f"{label}.path_budget.checked_paths", nonempty=True
    )
    recomputed = validate_encoded_path_budget(
        [Path(path) for path in checked],
        name_max=cast(int, budget["name_max"]),
        path_max=cast(int, budget["path_max"]),
    )
    if model.canonical_json_bytes(recomputed) != model.canonical_json_bytes(budget):
        raise model.CampaignError(f"{label}.path_budget is not canonical")
    return record


def _validate_evidence(value: object) -> dict[str, object]:
    evidence = model.require_exact_keys(value, _EVIDENCE_KEYS, "preflight evidence")
    repo_before = _validate_repo_identity(evidence["repo_before"], "preflight repo_before")
    repo_after = _validate_repo_identity(evidence["repo_after"], "preflight repo_after")
    if model.canonical_json_bytes(repo_before) != model.canonical_json_bytes(repo_after):
        raise model.CampaignError("preflight repository identity changed across probes")
    candidate = model.require_exact_keys(evidence["candidate"], _PAIR_KEYS, "preflight candidate")
    candidate_before = _validate_wheel_identity(candidate["before"], "preflight candidate.before")
    candidate_after = _validate_wheel_identity(candidate["after"], "preflight candidate.after")
    if model.canonical_json_bytes(candidate_before) != model.canonical_json_bytes(candidate_after):
        raise model.CampaignError("preflight candidate identity changed across probes")
    interpreters = model.require_exact_keys(
        evidence["interpreters"],
        set(model.INTERPRETER_ROLES),
        "preflight evidence.interpreters",
    )
    for role in model.INTERPRETER_ROLES:
        pair = model.require_exact_keys(
            interpreters[role], _PAIR_KEYS, f"preflight interpreters.{role}"
        )
        before = _validate_interpreter_snapshot(
            pair["before"], role, f"preflight interpreters.{role}.before"
        )
        after = _validate_interpreter_snapshot(
            pair["after"], role, f"preflight interpreters.{role}.after"
        )
        if model.canonical_json_bytes(before) != model.canonical_json_bytes(after):
            raise model.CampaignError(f"preflight interpreter {role} changed across probes")
    for key in ("inputs", "transform"):
        pair = model.require_exact_keys(evidence[key], _PAIR_KEYS, f"preflight {key}")
        if not isinstance(pair["before"], Mapping) or not isinstance(pair["after"], Mapping):
            raise model.CampaignError(f"preflight {key} snapshots must be objects")
        if model.canonical_json_bytes(pair["before"]) != model.canonical_json_bytes(pair["after"]):
            raise model.CampaignError(f"preflight {key} identity changed across probes")
    resources = model.require_exact_keys(evidence["resources"], _PAIR_KEYS, "preflight resources")
    _validate_resource_snapshot(resources["before"], "preflight resources.before")
    _validate_resource_snapshot(resources["after"], "preflight resources.after")
    filesystems = evidence["filesystems"]
    if type(filesystems) is not list or not filesystems:
        raise model.CampaignError("preflight filesystem evidence must be a non-empty array")
    filesystem_keys: set[tuple[int, int]] = set()
    for index, item in enumerate(filesystems):
        filesystem = _validate_filesystem_evidence(item, f"preflight filesystems[{index}]")
        identity = cast(dict[str, object], filesystem["identity"])
        filesystem_key = _filesystem_key(identity)
        if filesystem_key in filesystem_keys:
            raise model.CampaignError("preflight filesystem evidence contains a duplicate mount")
        filesystem_keys.add(filesystem_key)
    if model.canonical_json_bytes(evidence["topology"]) != model.canonical_json_bytes(
        model.topology()
    ):
        raise model.CampaignError("preflight topology evidence differs from the binding topology")
    matrix = model.require_exact_keys(evidence["matrix"], _MATRIX_EVIDENCE_KEYS, "preflight matrix")
    if matrix["job_count"] != 36:
        raise model.CampaignError("preflight matrix job_count is not 36")
    _require_sha256(matrix["jobs_sha256"], "preflight matrix jobs_sha256")
    session = model.require_exact_keys(
        evidence["session"], _SESSION_EVIDENCE_KEYS, "preflight session"
    )
    names = _require_string_list(session["names"], "preflight session.names", nonempty=True)
    if len(names) != 2 or len(set(names)) != 2:
        raise model.CampaignError("preflight session.names must contain two unique names")
    before_sessions = _require_string_list(session["before"], "preflight session.before")
    after_sessions = _require_string_list(session["after"], "preflight session.after")
    if set(names) & (set(before_sessions) | set(after_sessions)):
        raise model.CampaignError("preflight session evidence contains a name collision")
    return evidence


def _validate_gates(value: object) -> dict[str, object]:
    gates = model.require_exact_keys(value, _GATE_KEYS, "preflight gates")
    for key, gate in gates.items():
        if type(gate) is not bool or gate is not True:
            raise model.CampaignError(f"preflight gate {key!r} is not exactly true")
    return gates


def validate_preflight_document(value: object) -> dict[str, object]:
    """Validate the closed final-marker document and its internal digests."""

    document = model.require_exact_keys(value, _PREFLIGHT_KEYS, "preflight")
    model.require_schema(document, model.PREFLIGHT_SCHEMA, "preflight")
    run_id = model.validate_identifier(cast(str, document["run_id"]), "run id")
    _validate_timestamp(document["verified_utc"], "preflight verified_utc")
    request = validate_request(document["request"])
    if request["run_id"] != run_id:
        raise model.CampaignError("preflight request run ID mismatch")
    request_sha256 = _require_sha256(document["request_sha256"], "preflight request digest")
    if request_sha256 != model.sha256_json(request):
        raise model.CampaignError("preflight request digest mismatch")
    campaign = model.require_exact_keys(
        document["campaign"],
        _CAMPAIGN_REFERENCE_KEYS,
        "preflight campaign reference",
    )
    campaign_path = _canonical_path(campaign["path"], "preflight campaign path")
    if campaign_path != str(Path(cast(str, request["run_dir"])) / "campaign.json"):
        raise model.CampaignError("preflight campaign path does not match the request")
    _require_sha256(campaign["file_sha256"], "preflight campaign file digest")
    _require_sha256(campaign["spec_sha256"], "preflight campaign spec digest")
    _validate_evidence(document["evidence"])
    _validate_gates(document["gates"])
    return json.loads(model.canonical_json_bytes(document))


def build_preflight_document(
    *,
    request: Mapping[str, object],
    campaign: Mapping[str, object],
    evidence: Mapping[str, object],
    verified_utc: str,
) -> dict[str, object]:
    """Build the closed final marker from already completed probe evidence."""

    normalized_request = validate_request(request)
    campaign_document = model.validate_campaign_document(campaign)
    if campaign_document["run_id"] != normalized_request["run_id"]:
        raise model.CampaignError("campaign and preflight request run IDs differ")
    campaign_bytes = safe_io.canonical_storage_bytes(campaign_document)
    document = {
        "schema_version": model.PREFLIGHT_SCHEMA,
        "run_id": normalized_request["run_id"],
        "verified_utc": verified_utc,
        "request": normalized_request,
        "request_sha256": model.sha256_json(normalized_request),
        "campaign": {
            "path": str(Path(cast(str, normalized_request["run_dir"])) / "campaign.json"),
            "file_sha256": hashlib.sha256(campaign_bytes).hexdigest(),
            "spec_sha256": campaign_document["spec_sha256"],
        },
        "evidence": dict(evidence),
        "gates": {key: True for key in sorted(_GATE_KEYS)},
    }
    return validate_preflight_document(document)


def _validate_bundle_crosslinks(
    run_dir: Path,
    campaign: Mapping[str, object],
    marker: Mapping[str, object],
) -> None:
    campaign_document = model.validate_campaign_document(campaign)
    preflight_document = validate_preflight_document(marker)
    request = cast(dict[str, object], preflight_document["request"])
    reference = cast(dict[str, object], preflight_document["campaign"])
    if Path(cast(str, request["run_dir"])) != run_dir:
        raise model.CampaignError("preflight request run directory differs from bundle location")
    if campaign_document["run_id"] != preflight_document["run_id"]:
        raise model.CampaignError("campaign/preflight run ID mismatch")
    if campaign_document["spec_sha256"] != reference["spec_sha256"]:
        raise model.CampaignError("campaign/preflight spec digest mismatch")
    campaign_bytes = safe_io.canonical_storage_bytes(campaign_document)
    if hashlib.sha256(campaign_bytes).hexdigest() != reference["file_sha256"]:
        raise model.CampaignError("campaign/preflight file digest mismatch")
    spec = cast(dict[str, object], campaign_document["spec"])
    evidence = cast(dict[str, object], preflight_document["evidence"])
    if model.canonical_json_bytes(evidence["repo_before"]) != model.canonical_json_bytes(
        spec["repo"]
    ):
        raise model.CampaignError("preflight repository evidence differs from the campaign spec")
    candidate = cast(dict[str, object], evidence["candidate"])
    spec_candidate = cast(dict[str, object], spec["candidate"])
    if model.canonical_json_bytes(candidate["before"]) != model.canonical_json_bytes(
        spec_candidate.get("wheel")
    ):
        raise model.CampaignError("preflight candidate evidence differs from the campaign spec")
    interpreter_evidence = cast(dict[str, object], evidence["interpreters"])
    spec_interpreters = cast(dict[str, object], spec["interpreters"])
    for role in model.INTERPRETER_ROLES:
        pair = cast(dict[str, object], interpreter_evidence[role])
        if model.canonical_json_bytes(pair["before"]) != model.canonical_json_bytes(
            spec_interpreters[role]
        ):
            raise model.CampaignError(
                f"preflight interpreter evidence differs from the campaign spec: {role}"
            )
    inputs = cast(dict[str, object], evidence["inputs"])
    if model.canonical_json_bytes(inputs["before"]) != model.canonical_json_bytes(spec["inputs"]):
        raise model.CampaignError("preflight input evidence differs from the campaign spec")
    transform = cast(dict[str, object], evidence["transform"])
    spec_inputs = cast(dict[str, object], spec["inputs"])
    if model.canonical_json_bytes(transform["before"]) != model.canonical_json_bytes(
        spec_inputs["gencode-gtf-parent-stripped"]
    ):
        raise model.CampaignError("preflight transform evidence differs from the campaign spec")
    resources = cast(dict[str, object], evidence["resources"])
    spec_resources = dict(cast(dict[str, object], spec["resources"]))
    spec_filesystems = spec_resources.pop("filesystems", None)
    if model.canonical_json_bytes(resources["before"]) != model.canonical_json_bytes(
        spec_resources
    ):
        raise model.CampaignError("preflight resource evidence differs from the campaign spec")
    if model.canonical_json_bytes(evidence["filesystems"]) != model.canonical_json_bytes(
        spec_filesystems
    ):
        raise model.CampaignError("preflight filesystem evidence differs from the campaign spec")
    matrix = cast(dict[str, object], evidence["matrix"])
    spec_jobs = cast(list[object], spec["jobs"])
    if matrix != {
        "job_count": 36,
        "jobs_sha256": model.sha256_json(spec_jobs),
    }:
        raise model.CampaignError("preflight matrix evidence differs from the campaign spec")
    session = cast(dict[str, object], evidence["session"])
    spec_sha256 = cast(str, campaign_document["spec_sha256"])
    expected_session = f"gffbase-{spec_sha256[:12]}-cluster"
    if session["names"] != [expected_session, f"{expected_session}-canonical"]:
        raise model.CampaignError("preflight session names differ from the campaign digest")
    if model.canonical_json_bytes(request["parameters"]) != model.canonical_json_bytes(
        spec["parameters"]
    ):
        raise model.CampaignError("preflight request parameters differ from the campaign spec")
    request_interpreters = cast(dict[str, object], request["interpreters"])
    for role in model.INTERPRETER_ROLES:
        probe = cast(dict[str, object], spec_interpreters[role])
        if request_interpreters[role] != probe["requested_executable"]:
            raise model.CampaignError(
                f"preflight requested interpreter differs from the campaign spec: {role}"
            )
    request_transform = cast(dict[str, object], request["transform"])
    derived = cast(dict[str, object], spec_inputs["gencode-gtf-parent-stripped"])
    if (
        request_transform["output"] != derived["path"]
        or request_transform["manifest"] != derived["manifest_path"]
    ):
        raise model.CampaignError("preflight requested transform differs from the campaign spec")
    if request["candidate_wheel"] != cast(dict[str, object], candidate["before"])["path"]:
        raise model.CampaignError("preflight requested wheel differs from the campaign spec")


def commit_campaign_bundle(
    run_dir: os.PathLike[str] | str,
    campaign: Mapping[str, object],
    marker: Mapping[str, object],
    *,
    fault_hook: FaultHook | None = None,
    phase_validator: PhaseValidator | None = None,
) -> tuple[Path, Path]:
    """Create campaign first and the complete preflight marker strictly last."""

    directory = safe_io.inspect_path(run_dir, require_kind="directory")
    _validate_bundle_crosslinks(directory, campaign, marker)
    campaign_path = directory / "campaign.json"
    preflight_path = directory / "preflight.json"
    if phase_validator is not None:
        phase_validator("probed")
    safe_io.atomic_create_json(campaign_path, campaign)
    if phase_validator is not None:
        phase_validator("campaign")
    _fault(fault_hook, "after_campaign")
    safe_io.atomic_create_json(preflight_path, marker)
    if phase_validator is not None:
        phase_validator("complete")
    _fault(fault_hook, "after_preflight")
    return campaign_path, preflight_path


def _load_completed_bundle(
    path: os.PathLike[str] | str,
    *,
    runtime_validator: RuntimeValidator | None = None,
) -> tuple[Path, dict[str, object], dict[str, object]]:
    campaign_path = safe_io.lexical_absolute(path, "campaign path")
    if campaign_path.name != "campaign.json":
        raise model.CampaignError("campaign loader requires the exact campaign.json path")
    preflight_path = campaign_path.parent / "preflight.json"
    try:
        campaign = safe_io.strict_json_load(
            campaign_path,
            validator=model.validate_campaign_document,
        )
        marker = safe_io.strict_json_load(
            preflight_path,
            validator=validate_preflight_document,
        )
    except model.CampaignError as exc:
        raise model.CampaignError(
            f"incomplete or invalid campaign/preflight marker pair: {exc}"
        ) from exc
    if not isinstance(campaign, dict) or not isinstance(marker, dict):
        raise model.CampaignError("campaign/preflight validators returned invalid objects")
    _validate_bundle_crosslinks(campaign_path.parent, campaign, marker)
    reference = cast(dict[str, object], marker["campaign"])
    if safe_io.sha256_regular_file(campaign_path, require_unique=True) != reference["file_sha256"]:
        raise model.CampaignError("stored campaign digest differs from the preflight marker")
    request = cast(dict[str, object], marker["request"])
    transform = cast(dict[str, object], request["transform"])
    names = _directory_names(campaign_path.parent, "campaign run directory")
    runtime_names = {"jobs", "launches", "campaign-results.json"} & names
    if runtime_names and runtime_validator is None:
        raise model.CampaignError(
            "runtime campaign artifacts require an explicit closed runtime validator"
        )
    validate_run_tree(
        campaign_path.parent,
        transform_mode=cast(str, transform["mode"]),
        phase="runtime" if runtime_names else "complete",
    )
    if runtime_names:
        cast(RuntimeValidator, runtime_validator)(campaign_path.parent, campaign)
    return campaign_path, campaign, marker


def load_completed_campaign(
    path: os.PathLike[str] | str,
    *,
    runtime_validator: RuntimeValidator | None = None,
) -> dict[str, object]:
    """Load a campaign only when its final preflight marker cross-validates."""

    campaign_path, campaign, _marker = _load_completed_bundle(
        path,
        runtime_validator=runtime_validator,
    )
    loaded = dict(campaign)
    loaded["_path"] = str(campaign_path)
    return loaded


def load_completed_campaign_for_request(
    path: os.PathLike[str] | str,
    request: Mapping[str, object],
) -> dict[str, object]:
    """Load a complete bundle only when the normalized request is identical."""

    normalized_request = validate_request(request)
    campaign_path, campaign, marker = _load_completed_bundle(path)
    recorded_request = validate_request(marker["request"])
    if model.canonical_json_bytes(recorded_request) != model.canonical_json_bytes(
        normalized_request
    ):
        raise model.CampaignError("completed campaign request differs from the requested preflight")
    loaded = dict(campaign)
    loaded["_path"] = str(campaign_path)
    return loaded


__all__ = [
    "PROBE_ROOT_NAME",
    "REQUEST_SCHEMA",
    "build_preflight_document",
    "campaign_future_paths",
    "commit_campaign_bundle",
    "create_private_run_directory",
    "ensure_campaign_root",
    "filesystem_identity",
    "load_completed_campaign",
    "load_completed_campaign_for_request",
    "normalize_request",
    "probe_filesystem_capabilities",
    "qualify_mutable_filesystems",
    "validate_encoded_path_budget",
    "validate_preflight_document",
    "validate_request",
    "validate_run_tree",
    "verify_candidate_wheel",
    "verify_input_file",
]
