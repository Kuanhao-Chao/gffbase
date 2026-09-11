"""Adversarial contracts for crash-safe campaign filesystem primitives."""

from __future__ import annotations

import json
import os
import stat
import time
from pathlib import Path

import pytest

from benchmarks.campaign import safe_io
from benchmarks.campaign.model import CampaignError
from tests._platform import LINUX_ONLY_CAMPAIGN

pytestmark = LINUX_ONLY_CAMPAIGN


def _owner() -> dict[str, object]:
    return {
        "host": "unit-host",
        "pid": 123,
        "pgid": 120,
        "process_start_ticks": 456,
        "boot_id": "12345678-1234-1234-1234-123456789abc",
    }


def _lock_path(campaign: Path, lock_name: str = "controller.lock") -> Path:
    return safe_io._campaign_lock_path(campaign, lock_name)


def _retired_entries(parent: Path) -> list[Path]:
    return list((parent / ".gffbase-retired").glob("retired-*/entry"))


def _open_fd_count() -> int:
    return len(os.listdir("/proc/self/fd"))


def _descriptor_name(descriptor: int) -> str:
    try:
        return Path(os.readlink(f"/proc/self/fd/{descriptor}")).name
    except OSError:
        return ""


def test_internal_parent_entry_contract_is_exact_scan_ready(tmp_path: Path) -> None:
    campaign = tmp_path / "run"
    campaign.mkdir()

    held = safe_io.campaign_lock_parent_entries(campaign, lock_held=True)
    released = safe_io.campaign_lock_parent_entries(campaign, lock_held=False)

    assert released == {safe_io.LOCK_ROOT_NAME: "directory"}
    assert set(held) == {safe_io.LOCK_ROOT_NAME, _lock_path(campaign).name}
    assert held[_lock_path(campaign).name] == "directory"
    assert safe_io.RETIRE_ROOT_NAME == ".gffbase-retired"
    replacement_held = safe_io.atomic_write_lock_parent_entries(
        campaign / "status.json",
        lock_held=True,
    )
    replacement_released = safe_io.atomic_write_lock_parent_entries(
        campaign / "status.json",
        lock_held=False,
    )
    assert replacement_released == {safe_io.LOCK_ROOT_NAME: "directory"}
    assert len(replacement_held) == 2
    assert set(replacement_held.values()) == {"directory"}
    assert set(replacement_held) != set(held)
    with pytest.raises(CampaignError, match="boolean"):
        safe_io.campaign_lock_parent_entries(campaign, lock_held=1)  # type: ignore[arg-type]


def test_directory_walk_closes_a_child_when_post_open_fstat_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = tmp_path / "parent" / "child"
    target.mkdir(parents=True)
    real_fstat = os.fstat
    injected = False

    def fail_child_fstat(descriptor: int) -> os.stat_result:
        nonlocal injected
        if not injected and _descriptor_name(descriptor) == "child":
            injected = True
            raise OSError("injected directory fstat failure")
        return real_fstat(descriptor)

    monkeypatch.setattr(safe_io.os, "fstat", fail_child_fstat)
    before = _open_fd_count()
    with pytest.raises(CampaignError, match="injected|open directory"):
        with safe_io._open_directory(target):
            pass
    assert injected
    assert _open_fd_count() == before


def test_private_directory_open_closes_descriptor_when_validation_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    real_fstat = os.fstat
    injected = False

    def fail_private_fstat(descriptor: int) -> os.stat_result:
        nonlocal injected
        if not injected and _descriptor_name(descriptor) == "private":
            injected = True
            raise OSError("injected private directory fstat failure")
        return real_fstat(descriptor)

    monkeypatch.setattr(safe_io.os, "fstat", fail_private_fstat)
    with safe_io._open_directory(tmp_path) as parent_descriptor:
        before = _open_fd_count()
        with pytest.raises(OSError, match="injected"):
            safe_io._open_or_create_private_directory_at(
                parent_descriptor,
                "private",
                "test private directory",
            )
        assert injected
        assert _open_fd_count() == before


def test_inspect_path_rejects_live_dangling_and_ancestor_symlinks(tmp_path: Path) -> None:
    target = tmp_path / "target"
    target.mkdir()
    live = tmp_path / "live"
    live.symlink_to(target, target_is_directory=True)
    dangling = tmp_path / "dangling"
    dangling.symlink_to(tmp_path / "missing")

    for path in (live, live / "child", dangling, dangling / "child"):
        with pytest.raises(CampaignError, match="symlink"):
            safe_io.inspect_path(path, allow_missing_tail=True)


def test_resolve_beneath_is_lexical_and_rejects_escape_and_aliases(tmp_path: Path) -> None:
    root = tmp_path / "root"
    root.mkdir()
    child = root / "new" / "artifact.json"
    assert safe_io.resolve_beneath(root, child, allow_missing_tail=True) == child
    assert (
        safe_io.resolve_beneath(root, Path("new/artifact.json"), allow_missing_tail=True) == child
    )

    for candidate in (Path("../escape"), root / ".." / "escape", tmp_path / "escape"):
        with pytest.raises(CampaignError, match=r"escape|\.\."):
            safe_io.resolve_beneath(root, candidate, allow_missing_tail=True)

    outside = tmp_path / "outside"
    outside.mkdir()
    (root / "alias").symlink_to(outside, target_is_directory=True)
    with pytest.raises(CampaignError, match="symlink"):
        safe_io.resolve_beneath(root, root / "alias" / "artifact", allow_missing_tail=True)


def test_campaign_root_protection_allows_only_safe_nonoverlap(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    (repo / "benchmarks" / "data").mkdir(parents=True)
    (repo / "benchmarks" / "results").mkdir()
    allowed = repo / "benchmarks" / "out" / "cluster"

    assert safe_io.validate_campaign_root(allowed, repo_root=repo) == allowed
    for path in (
        Path("/"),
        repo,
        repo.parent,
        repo / "benchmarks" / "data",
        repo / "benchmarks" / "data" / "run",
        repo / "benchmarks" / "results" / "run",
    ):
        with pytest.raises(CampaignError, match="campaign root"):
            safe_io.validate_campaign_root(path, repo_root=repo)


@pytest.mark.parametrize("kind", ["file", "fifo"])
def test_campaign_root_rejects_existing_non_directory_leaf(tmp_path: Path, kind: str) -> None:
    repo = tmp_path / "repo"
    (repo / "benchmarks" / "data").mkdir(parents=True)
    (repo / "benchmarks" / "results").mkdir()
    candidate = repo / "benchmarks" / "out" / "cluster"
    candidate.parent.mkdir(parents=True)
    if kind == "file":
        candidate.write_text("not a directory", encoding="utf-8")
    else:
        os.mkfifo(candidate)

    with pytest.raises(CampaignError, match="directory"):
        safe_io.validate_campaign_root(candidate, repo_root=repo)


def test_campaign_root_rejects_a_descendant_of_a_repo_inode_alias(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = tmp_path / "repo"
    (repo / "benchmarks" / "data").mkdir(parents=True)
    (repo / "benchmarks" / "results").mkdir()
    alias = tmp_path / "repo-bind-alias"
    candidate = alias / "benchmarks" / "out" / "cluster"
    candidate.mkdir(parents=True)
    real_dev_ino = safe_io._dev_ino
    repo_identity = real_dev_ino(repo)

    def aliased_dev_ino(path: Path) -> tuple[int, int] | None:
        if path == alias:
            return repo_identity
        return real_dev_ino(path)

    monkeypatch.setattr(safe_io, "_dev_ino", aliased_dev_ino)
    with pytest.raises(CampaignError, match="alias|unsafe"):
        safe_io.validate_campaign_root(candidate, repo_root=repo)


def test_derived_job_attempt_paths_and_claims_are_exact(tmp_path: Path) -> None:
    campaign = tmp_path / "run"
    expected_job = campaign / "jobs" / "scaling-t01-mane"
    expected_attempt = expected_job / "attempts" / "0007"
    assert safe_io.job_directory(campaign, "scaling-t01-mane") == expected_job
    assert safe_io.attempt_directory(campaign, "scaling-t01-mane", 7) == expected_attempt
    assert (
        safe_io.require_exact_derived_path(
            expected_attempt, expected_attempt, allow_missing_tail=True
        )
        == expected_attempt
    )
    for attempt in (True, 0, -1, 10_000):
        with pytest.raises(CampaignError):
            safe_io.attempt_directory(campaign, "scaling-t01-mane", attempt)  # type: ignore[arg-type]
    with pytest.raises(CampaignError, match="derived"):
        safe_io.require_exact_derived_path(
            expected_attempt,
            expected_job / "attempts" / "7",
            allow_missing_tail=True,
        )
    with pytest.raises(CampaignError, match="derived|canonical"):
        safe_io.require_exact_derived_path(
            expected_attempt,
            str(expected_attempt).replace("/attempts/", "/attempts//"),
            allow_missing_tail=True,
        )


def test_atomic_create_is_canonical_immutable_durable_and_mode_0600(tmp_path: Path) -> None:
    path = tmp_path / "artifact.json"
    value = {"schema_version": "unit-v1", "unicode": "é", "value": 1}
    safe_io.atomic_create_json(path, value)
    before = path.stat()
    safe_io.atomic_create_json(path, value)

    assert path.read_bytes() == (b'{"schema_version":"unit-v1","unicode":"\xc3\xa9","value":1}\n')
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert (path.stat().st_dev, path.stat().st_ino) == (before.st_dev, before.st_ino)
    with pytest.raises(CampaignError, match="overwrite|different"):
        safe_io.atomic_create_json(path, {**value, "value": 2})


def test_public_json_io_accepts_checkout_modes_but_rejects_world_write(
    tmp_path: Path,
) -> None:
    path = tmp_path / "published.json"
    original = {"schema_version": "unit-v1", "value": 1}
    replacement = {"schema_version": "unit-v1", "value": 2}

    safe_io.atomic_create_public_json(path, original)
    assert stat.S_IMODE(path.stat().st_mode) == 0o644
    path.chmod(0o664)
    before = path.stat()
    assert safe_io.strict_public_json_load(path) == original
    safe_io.atomic_create_public_json(path, original)
    assert (path.stat().st_dev, path.stat().st_ino) == (before.st_dev, before.st_ino)

    safe_io.atomic_write_public_json_durable(path, replacement)
    assert stat.S_IMODE(path.stat().st_mode) == 0o644
    assert safe_io.strict_public_json_load(path) == replacement
    path.chmod(0o666)
    with pytest.raises(CampaignError, match="mode|permission"):
        safe_io.strict_public_json_load(path)
    with pytest.raises(CampaignError, match="mode|permission"):
        safe_io.atomic_create_public_json(path, replacement)


def test_private_and_public_directories_handle_setgid_repository_parents(
    tmp_path: Path,
) -> None:
    shared = tmp_path / "shared"
    shared.mkdir()
    shared.chmod(0o2775)

    private = shared / "private"
    safe_io.create_private_directory(private)
    assert stat.S_IMODE(private.stat().st_mode) == 0o700

    with safe_io._open_directory(shared) as parent_descriptor:
        descriptor, _identity = safe_io._open_or_create_private_directory_at(
            parent_descriptor,
            "lock-root",
            "test lock root",
        )
        os.close(descriptor)
    assert stat.S_IMODE((shared / "lock-root").stat().st_mode) == 0o700

    public = shared / "public"
    safe_io.create_public_directory(public)
    assert stat.S_IMODE(public.stat().st_mode) in {0o755, 0o2755}
    public.chmod(0o2775)
    safe_io.create_public_directory(public, exist_ok=True)
    public.chmod(0o2777)
    with pytest.raises(CampaignError, match="mode|permission"):
        safe_io.create_public_directory(public, exist_ok=True)


@pytest.mark.parametrize("writer", ["create", "replace"])
def test_json_temporary_descriptor_closes_when_initial_fstat_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, writer: str
) -> None:
    path = tmp_path / f"{writer}.json"
    real_fstat = os.fstat
    injected = False

    def fail_temporary_fstat(descriptor: int) -> os.stat_result:
        nonlocal injected
        if not injected and _descriptor_name(descriptor).startswith(".gffbase-tmp-"):
            injected = True
            raise OSError("injected JSON temporary fstat failure")
        return real_fstat(descriptor)

    monkeypatch.setattr(safe_io.os, "fstat", fail_temporary_fstat)
    before = _open_fd_count()
    with pytest.raises(OSError, match="injected"):
        if writer == "create":
            safe_io.atomic_create_json(path, {"schema_version": "unit-v1"})
        else:
            safe_io.atomic_write_json_durable(path, {"schema_version": "unit-v1"})
    assert injected
    assert _open_fd_count() == before
    assert not path.exists()


def test_atomic_noreplace_rename_preserves_both_names_on_collision(tmp_path: Path) -> None:
    source = tmp_path / "source"
    destination = tmp_path / "destination"
    source.write_bytes(b"owned")
    destination.write_bytes(b"foreign")

    with safe_io._open_directory(tmp_path) as descriptor:
        with pytest.raises(FileExistsError):
            safe_io._rename_noreplace_at(
                descriptor,
                source.name,
                descriptor,
                destination.name,
            )

    assert source.read_bytes() == b"owned"
    assert destination.read_bytes() == b"foreign"


def test_bound_noreplace_rollback_fsyncs_both_distinct_parents(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source_parent = tmp_path / "source"
    destination_parent = tmp_path / "destination"
    source_parent.mkdir()
    destination_parent.mkdir()
    source = source_parent / "temporary"
    destination = destination_parent / "final"
    displaced = source_parent / "displaced-owned"
    source.write_bytes(b"owned")
    source.chmod(0o600)
    expected = source.stat()
    real_noreplace = safe_io._rename_noreplace_at
    real_fsync = os.fsync
    swapped = False
    synced_directories: set[tuple[int, int]] = set()

    def swap_source_inside_rename(
        source_descriptor: int,
        source_name: str,
        destination_descriptor: int,
        destination_name: str,
    ) -> None:
        nonlocal swapped
        if destination_name == destination.name and not swapped:
            swapped = True
            source.rename(displaced)
            source.write_bytes(b"foreign")
            source.chmod(0o600)
        real_noreplace(
            source_descriptor,
            source_name,
            destination_descriptor,
            destination_name,
        )

    def recording_fsync(descriptor: int) -> None:
        item = os.fstat(descriptor)
        if stat.S_ISDIR(item.st_mode):
            synced_directories.add((item.st_dev, item.st_ino))
        real_fsync(descriptor)

    monkeypatch.setattr(safe_io, "_rename_noreplace_at", swap_source_inside_rename)
    monkeypatch.setattr(safe_io.os, "fsync", recording_fsync)
    with safe_io._open_directory(source_parent) as source_descriptor:
        with safe_io._open_directory(destination_parent) as destination_descriptor:
            with pytest.raises(CampaignError, match="identity|publication"):
                safe_io._rename_noreplace_bound_at(
                    source_descriptor,
                    source.name,
                    destination_descriptor,
                    destination.name,
                    expected,
                    "test publication",
                )

    assert not destination.exists()
    assert source.read_bytes() == b"foreign"
    assert displaced.read_bytes() == b"owned"
    assert (source_parent.stat().st_dev, source_parent.stat().st_ino) in synced_directories
    assert (
        destination_parent.stat().st_dev,
        destination_parent.stat().st_ino,
    ) in synced_directories


def test_atomic_create_idempotent_retry_fsyncs_existing_file_and_parent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "artifact.json"
    value = {"schema_version": "unit-v1", "value": 1}
    path.write_bytes(safe_io.canonical_storage_bytes(value))
    path.chmod(0o600)
    real_fsync = os.fsync
    synced_modes: list[int] = []

    def recording_fsync(descriptor: int) -> None:
        synced_modes.append(os.fstat(descriptor).st_mode)
        real_fsync(descriptor)

    monkeypatch.setattr(safe_io.os, "fsync", recording_fsync)
    safe_io.atomic_create_json(path, value)

    assert any(stat.S_ISREG(mode) for mode in synced_modes)
    assert any(stat.S_ISDIR(mode) for mode in synced_modes)


def test_atomic_create_fault_cleans_only_its_own_partial_file(tmp_path: Path) -> None:
    path = tmp_path / "artifact.json"

    def fail(stage: str) -> None:
        if stage == "after_write":
            raise RuntimeError("injected")

    with pytest.raises(RuntimeError, match="injected"):
        safe_io.atomic_create_json(path, {"schema_version": "unit-v1"}, fault_hook=fail)
    assert not path.exists()


def test_atomic_create_rejects_a_parent_swap_at_the_fsync_boundary(tmp_path: Path) -> None:
    parent = tmp_path / "parent"
    parent.mkdir()
    displaced = tmp_path / "displaced-parent"
    target = parent / "artifact.json"

    def swap_parent(stage: str) -> None:
        if stage == "before_parent_fsync":
            parent.rename(displaced)
            parent.mkdir()

    with pytest.raises(CampaignError, match="parent.*identity|changed"):
        safe_io.atomic_create_json(
            target,
            {"schema_version": "unit-v1"},
            fault_hook=swap_parent,
        )
    assert not target.exists()
    # Publication already happened before the directory durability boundary.
    # A complete visible immutable artifact is never erased after publication,
    # even when the lexical parent is subsequently replaced.
    assert safe_io.strict_json_load(displaced / "artifact.json") == {"schema_version": "unit-v1"}


def test_atomic_create_never_erases_an_identical_concurrent_success(tmp_path: Path) -> None:
    path = tmp_path / "artifact.json"
    value = {"schema_version": "unit-v1", "value": 1}
    nested_returned = False

    def publish_then_fail(stage: str) -> None:
        nonlocal nested_returned
        if stage == "after_write":
            safe_io.atomic_create_json(path, value)
            nested_returned = True
            raise RuntimeError("first creator failed")

    with pytest.raises(RuntimeError, match="first creator failed"):
        safe_io.atomic_create_json(path, value, fault_hook=publish_then_fail)
    assert nested_returned is True
    assert safe_io.strict_json_load(path) == value


def test_atomic_create_rolls_back_a_temporary_swapped_inside_noreplace(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "artifact.json"
    displaced = tmp_path / "displaced-owned-temp.json"
    owned = {"schema_version": "unit-v1", "value": "owned"}
    foreign = {"schema_version": "unit-v1", "value": "foreign"}
    real_noreplace = safe_io._rename_noreplace_at
    swapped = False

    def swap_source_inside_rename(
        source_descriptor: int,
        source_name: str,
        destination_descriptor: int,
        destination_name: str,
    ) -> None:
        nonlocal swapped
        if destination_name == path.name and not swapped:
            swapped = True
            (tmp_path / source_name).rename(displaced)
            (tmp_path / source_name).write_bytes(safe_io.canonical_storage_bytes(foreign))
            (tmp_path / source_name).chmod(0o600)
        real_noreplace(
            source_descriptor,
            source_name,
            destination_descriptor,
            destination_name,
        )

    monkeypatch.setattr(safe_io, "_rename_noreplace_at", swap_source_inside_rename)
    with pytest.raises(CampaignError, match="temporary|publish|replaced|identity"):
        safe_io.atomic_create_json(path, owned)

    assert not path.exists()
    assert safe_io.strict_json_load(displaced) == owned
    assert any(
        safe_io.strict_json_load(candidate) == foreign
        for candidate in tmp_path.glob(".gffbase-tmp-*")
    )


def test_atomic_create_detects_in_place_tampering_after_publication(tmp_path: Path) -> None:
    path = tmp_path / "artifact.json"
    attacker = {"schema_version": "unit-v1", "value": "foreign"}

    def tamper(stage: str) -> None:
        if stage != "before_parent_fsync":
            return
        descriptor = os.open(path, os.O_WRONLY | os.O_TRUNC | os.O_NOFOLLOW)
        try:
            safe_io._write_all(descriptor, safe_io.canonical_storage_bytes(attacker))
            os.fsync(descriptor)
        finally:
            os.close(descriptor)

    with pytest.raises(CampaignError, match="changed"):
        safe_io.atomic_create_json(
            path,
            {"schema_version": "unit-v1", "value": "owned"},
            fault_hook=tamper,
        )
    assert safe_io.strict_json_load(path) == attacker


def test_atomic_replace_preserves_old_value_until_commit(tmp_path: Path) -> None:
    path = tmp_path / "status.json"
    old = {"schema_version": "status-v1", "state": "old"}
    new = {"schema_version": "status-v1", "state": "new"}
    safe_io.atomic_write_json_durable(path, old)

    def fail(stage: str) -> None:
        if stage == "before_replace":
            raise RuntimeError("injected")

    with pytest.raises(RuntimeError, match="injected"):
        safe_io.atomic_write_json_durable(path, new, fault_hook=fail)
    assert safe_io.strict_json_load(path) == old
    tombstones = _retired_entries(tmp_path)
    assert len(tombstones) == 1
    assert tombstones[0].is_file()

    safe_io.atomic_write_json_durable(path, new)
    assert safe_io.strict_json_load(path) == new


def test_atomic_replace_fault_after_commit_leaves_complete_new_value(tmp_path: Path) -> None:
    path = tmp_path / "status.json"
    old = {"schema_version": "status-v1", "state": "old"}
    new = {"schema_version": "status-v1", "state": "new"}
    safe_io.atomic_write_json_durable(path, old)

    def fail(stage: str) -> None:
        if stage == "after_replace":
            raise RuntimeError("injected")

    with pytest.raises(RuntimeError, match="injected"):
        safe_io.atomic_write_json_durable(path, new, fault_hook=fail)
    assert safe_io.strict_json_load(path) == new


def test_atomic_replace_refuses_to_overwrite_a_target_swapped_at_commit(
    tmp_path: Path,
) -> None:
    path = tmp_path / "status.json"
    displaced = tmp_path / "displaced-status.json"
    old = {"schema_version": "status-v1", "state": "old"}
    replacement = {"schema_version": "status-v1", "state": "foreign"}
    safe_io.atomic_create_json(path, old)

    def swap_target(stage: str) -> None:
        if stage == "before_replace":
            path.rename(displaced)
            safe_io.atomic_create_json(path, replacement)

    with pytest.raises(CampaignError, match="changed|replacement"):
        safe_io.atomic_write_json_durable(
            path,
            {"schema_version": "status-v1", "state": "new"},
            fault_hook=swap_target,
        )
    assert safe_io.strict_json_load(path) == replacement
    assert safe_io.strict_json_load(displaced) == old


def test_atomic_replace_rolls_back_a_target_swapped_inside_atomic_exchange(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "status.json"
    displaced = tmp_path / "displaced-status.json"
    old = {"schema_version": "status-v1", "state": "old"}
    foreign = {"schema_version": "status-v1", "state": "foreign"}
    safe_io.atomic_create_json(path, old)
    real_exchange = safe_io._rename_exchange_at
    swapped = False

    def swap_inside_exchange(
        source_descriptor: int,
        source_name: str,
        destination_descriptor: int,
        destination_name: str,
    ) -> None:
        nonlocal swapped
        if not swapped:
            swapped = True
            path.rename(displaced)
            safe_io.atomic_create_json(path, foreign)
        real_exchange(
            source_descriptor,
            source_name,
            destination_descriptor,
            destination_name,
        )

    monkeypatch.setattr(safe_io, "_rename_exchange_at", swap_inside_exchange)
    with pytest.raises(CampaignError, match="changed|replacement"):
        safe_io.atomic_write_json_durable(
            path,
            {"schema_version": "status-v1", "state": "new"},
        )

    assert safe_io.strict_json_load(path) == foreign
    assert safe_io.strict_json_load(displaced) == old
    assert len(_retired_entries(tmp_path)) == 1


def _advance_ctime(descriptor: int, name: str) -> None:
    """Move an entry's ctime forward without touching its bytes, mtime or inode.

    This is what ext4, tmpfs and btrfs do to an inode as a side effect of
    renaming it -- POSIX permits either behaviour, and XFS does not do it. A
    same-value utime changes nothing but ctime. Inode timestamps come from a
    coarse clock, so retry until ctime has provably moved; the caller relies on
    the change having happened.
    """
    before = os.stat(name, dir_fd=descriptor, follow_symlinks=False)
    for _ in range(200):
        os.utime(
            name,
            ns=(before.st_atime_ns, before.st_mtime_ns),
            dir_fd=descriptor,
            follow_symlinks=False,
        )
        after = os.stat(name, dir_fd=descriptor, follow_symlinks=False)
        if after.st_ctime_ns != before.st_ctime_ns:
            assert (after.st_ino, after.st_size, after.st_mtime_ns) == (
                before.st_ino,
                before.st_size,
                before.st_mtime_ns,
            )
            return
        time.sleep(0.005)
    raise AssertionError(f"could not advance ctime on {name}")


def _ctime_advancing_exchange(real_exchange):  # type: ignore[no-untyped-def]
    """Wrap the real RENAME_EXCHANGE with ext4's side effect on both inodes."""

    def exchange(
        source_descriptor: int,
        source_name: str,
        destination_descriptor: int,
        destination_name: str,
    ) -> None:
        real_exchange(source_descriptor, source_name, destination_descriptor, destination_name)
        _advance_ctime(source_descriptor, source_name)
        _advance_ctime(destination_descriptor, destination_name)

    return exchange


def test_atomic_replace_accepts_a_rename_that_advances_ctime(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """On ext4 no existing file could ever be replaced.

    The exchange path compared the displaced inode against its pre-exchange
    stat including `st_ctime_ns`. ext4 advances an inode's ctime when it is
    renamed, so the comparison always failed, the code concluded a concurrent
    writer had swapped the target, rolled back, and then could not prove the
    rollback either -- because the rollback was a rename too. Every GitHub
    Ubuntu runner is ext4. Local runs passed only because this host is XFS,
    which leaves ctime alone. `_rename_noreplace_bound_at` already excluded
    ctime across its own rename, with a comment saying why; the exchange path
    never followed it.
    """
    path = tmp_path / "status.json"
    safe_io.atomic_create_json(path, {"schema_version": "status-v1", "state": "old"})
    monkeypatch.setattr(
        safe_io, "_rename_exchange_at", _ctime_advancing_exchange(safe_io._rename_exchange_at)
    )

    new = {"schema_version": "status-v1", "state": "new"}
    safe_io.atomic_write_json_durable(path, new)

    assert safe_io.strict_json_load(path) == new
    assert sorted(p.name for p in tmp_path.iterdir() if p.name.startswith(".gffbase-tmp")) == []


def test_ctime_advancing_exchange_still_rolls_back_a_real_swap(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Tolerating a ctime change must not blind the check to a replaced inode.

    The rollback is itself an exchange, so on ext4 it advances ctime again; the
    restoration proof must still hold, and the foreign file must be left in
    place with the original displaced beside it.
    """
    path = tmp_path / "status.json"
    displaced = tmp_path / "displaced-status.json"
    old = {"schema_version": "status-v1", "state": "old"}
    foreign = {"schema_version": "status-v1", "state": "foreign"}
    safe_io.atomic_create_json(path, old)
    ctime_exchange = _ctime_advancing_exchange(safe_io._rename_exchange_at)
    swapped = False

    def swap_inside_exchange(
        source_descriptor: int,
        source_name: str,
        destination_descriptor: int,
        destination_name: str,
    ) -> None:
        nonlocal swapped
        if not swapped:
            swapped = True
            path.rename(displaced)
            safe_io.atomic_create_json(path, foreign)
        ctime_exchange(source_descriptor, source_name, destination_descriptor, destination_name)

    monkeypatch.setattr(safe_io, "_rename_exchange_at", swap_inside_exchange)
    with pytest.raises(CampaignError, match="replacement target changed at atomic commit"):
        safe_io.atomic_write_json_durable(path, {"schema_version": "status-v1", "state": "new"})

    assert safe_io.strict_json_load(path) == foreign
    assert safe_io.strict_json_load(displaced) == old


def test_atomic_replace_rolls_back_an_unsafe_target_swapped_inside_exchange(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "status.json"
    displaced = tmp_path / "displaced-status.json"
    safe_io.atomic_create_json(path, {"schema_version": "status-v1", "state": "old"})
    real_exchange = safe_io._rename_exchange_at
    swapped = False

    def swap_directory_inside_exchange(
        source_descriptor: int,
        source_name: str,
        destination_descriptor: int,
        destination_name: str,
    ) -> None:
        nonlocal swapped
        if not swapped:
            swapped = True
            path.rename(displaced)
            path.mkdir()
            (path / "foreign.txt").write_text("foreign", encoding="utf-8")
        real_exchange(
            source_descriptor,
            source_name,
            destination_descriptor,
            destination_name,
        )

    monkeypatch.setattr(safe_io, "_rename_exchange_at", swap_directory_inside_exchange)
    with pytest.raises(CampaignError, match="changed|replacement"):
        safe_io.atomic_write_json_durable(
            path,
            {"schema_version": "status-v1", "state": "new"},
        )

    assert (path / "foreign.txt").read_text(encoding="utf-8") == "foreign"
    assert safe_io.strict_json_load(displaced)["state"] == "old"
    assert len(_retired_entries(tmp_path)) == 1


def test_atomic_replace_serializes_cooperating_writers_before_exchange(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "status.json"
    old = {"schema_version": "status-v1", "state": "old"}
    winner = {"schema_version": "status-v1", "state": "winner"}
    loser = {"schema_version": "status-v1", "state": "loser"}
    safe_io.atomic_create_json(path, old)
    real_exchange = safe_io._rename_exchange_at
    nested_attempted = False

    def attempt_nested_writer(
        source_descriptor: int,
        source_name: str,
        destination_descriptor: int,
        destination_name: str,
    ) -> None:
        nonlocal nested_attempted
        if not nested_attempted:
            nested_attempted = True
            with pytest.raises(CampaignError, match="locked"):
                safe_io.atomic_write_json_durable(path, loser)
            assert safe_io.strict_json_load(path) == old
        real_exchange(
            source_descriptor,
            source_name,
            destination_descriptor,
            destination_name,
        )

    monkeypatch.setattr(safe_io, "_rename_exchange_at", attempt_nested_writer)
    safe_io.atomic_write_json_durable(path, winner)

    assert nested_attempted
    assert safe_io.strict_json_load(path) == winner


def test_atomic_replace_rejects_in_place_temporary_tampering(tmp_path: Path) -> None:
    path = tmp_path / "status.json"
    old = {"schema_version": "status-v1", "state": "old"}
    attacker = {"schema_version": "status-v1", "state": "foreign"}
    safe_io.atomic_create_json(path, old)

    def tamper(stage: str) -> None:
        if stage != "before_replace":
            return
        temporary = next(tmp_path.glob(".gffbase-tmp-*"))
        descriptor = os.open(temporary, os.O_WRONLY | os.O_TRUNC | os.O_NOFOLLOW)
        try:
            safe_io._write_all(descriptor, safe_io.canonical_storage_bytes(attacker))
            os.fsync(descriptor)
        finally:
            os.close(descriptor)

    with pytest.raises(CampaignError, match="changed|temporary"):
        safe_io.atomic_write_json_durable(
            path,
            {"schema_version": "status-v1", "state": "new"},
            fault_hook=tamper,
        )
    assert safe_io.strict_json_load(path) == old


def test_json_writers_reject_symlink_special_and_hardlink_targets(tmp_path: Path) -> None:
    real = tmp_path / "real.json"
    safe_io.atomic_create_json(real, {"schema_version": "unit-v1"})
    link = tmp_path / "link.json"
    link.symlink_to(real)
    hard = tmp_path / "hard.json"
    os.link(real, hard)
    directory = tmp_path / "directory.json"
    directory.mkdir()

    for target in (link, hard, directory):
        with pytest.raises(CampaignError):
            safe_io.atomic_write_json_durable(target, {"schema_version": "unit-v1"})


@pytest.mark.parametrize(
    "raw",
    [
        b'{"schema_version":"unit-v1","x":1,"x":2}\n',
        b'{"schema_version":"unit-v1"}',
        b' {"schema_version":"unit-v1"}\n',
        b'\xef\xbb\xbf{"schema_version":"unit-v1"}\n',
        b'{"schema_version":"unit-v1","x":NaN}\n',
        b'{"schema_version":"unit-v1"}\ntrailing',
    ],
)
def test_strict_json_loader_rejects_duplicate_or_noncanonical_storage(
    tmp_path: Path, raw: bytes
) -> None:
    path = tmp_path / "artifact.json"
    path.write_bytes(raw)
    path.chmod(0o600)
    with pytest.raises(CampaignError, match="JSON|canonical|duplicate|UTF"):
        safe_io.strict_json_load(path)


def test_strict_json_loader_is_bounded_nofollow_and_closed_by_validator(tmp_path: Path) -> None:
    path = tmp_path / "artifact.json"
    safe_io.atomic_create_json(path, {"schema_version": "unit-v1", "value": 1})

    def validator(value: object) -> dict[str, object]:
        assert isinstance(value, dict)
        if set(value) != {"schema_version", "value"}:
            raise CampaignError("closed shape")
        return value

    assert safe_io.strict_json_load(path, validator=validator)["value"] == 1
    with pytest.raises(CampaignError, match="size"):
        safe_io.strict_json_load(path, max_bytes=4)
    link = tmp_path / "link.json"
    link.symlink_to(path)
    with pytest.raises(CampaignError, match="symlink|unsafe"):
        safe_io.strict_json_load(link)


def test_strict_json_loader_rejects_wrong_mode_and_excessive_nesting(tmp_path: Path) -> None:
    path = tmp_path / "artifact.json"
    path.write_bytes(b'{"schema_version":"unit-v1"}\n')
    path.chmod(0o644)
    with pytest.raises(CampaignError, match="mode|permission"):
        safe_io.strict_json_load(path)

    path.write_bytes(b"[" * 2000 + b"null" + b"]" * 2000 + b"\n")
    path.chmod(0o600)
    with pytest.raises(CampaignError, match="JSON|depth|recursion"):
        safe_io.strict_json_load(path)


@pytest.mark.parametrize(
    ("token", "kind"),
    [
        (b"9" * 5_000, "integer"),
        (b"1." + b"0" * 5_000, "float"),
    ],
)
def test_strict_json_loader_bounds_numeric_tokens_before_conversion(
    tmp_path: Path, token: bytes, kind: str
) -> None:
    path = tmp_path / "artifact.json"
    path.write_bytes(b'{"value":' + token + b"}\n")
    path.chmod(0o600)

    with pytest.raises(CampaignError, match=rf"JSON {kind} token exceeds"):
        safe_io.strict_json_load(path)


def test_strict_json_loader_rejects_a_hardlink_added_between_lstat_and_open(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "artifact.json"
    alias = tmp_path / "alias.json"
    safe_io.atomic_create_json(path, {"schema_version": "unit-v1"})
    real_open = os.open
    raced = False

    def racing_open(
        name: str | bytes,
        flags: int,
        mode: int = 0o777,
        *,
        dir_fd: int | None = None,
    ) -> int:
        nonlocal raced
        if name == path.name and dir_fd is not None and not raced:
            raced = True
            os.link(path, alias)
        return real_open(name, flags, mode, dir_fd=dir_fd)

    monkeypatch.setattr(safe_io.os, "open", racing_open)
    with pytest.raises(CampaignError, match="changed|linked|unsafe"):
        safe_io.strict_json_load(path)


def test_strict_json_loader_rejects_parent_replacement_during_read(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    parent = tmp_path / "parent"
    parent.mkdir()
    displaced = tmp_path / "displaced-parent"
    path = parent / "artifact.json"
    safe_io.atomic_create_json(path, {"schema_version": "unit-v1", "value": "old"})
    real_read = os.read
    swapped = False

    def racing_read(descriptor: int, size: int) -> bytes:
        nonlocal swapped
        if not swapped:
            swapped = True
            parent.rename(displaced)
            parent.mkdir()
            replacement = parent / path.name
            replacement.write_bytes(
                safe_io.canonical_storage_bytes({"schema_version": "unit-v1", "value": "new"})
            )
            replacement.chmod(0o600)
        return real_read(descriptor, size)

    monkeypatch.setattr(safe_io.os, "read", racing_read)
    with pytest.raises(CampaignError, match="parent|changed|replaced|protected"):
        safe_io.strict_json_load(path)


def test_strict_json_loader_rechecks_parent_after_final_entry_check(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    parent = tmp_path / "parent"
    parent.mkdir()
    displaced = tmp_path / "displaced-parent"
    path = parent / "artifact.json"
    safe_io.atomic_create_json(path, {"schema_version": "unit-v1", "value": "old"})
    real_require = safe_io._require_entry_identity
    swapped = False

    def swap_during_final_entry_check(*args: object, **kwargs: object):
        nonlocal swapped
        if not swapped:
            swapped = True
            parent.rename(displaced)
            parent.mkdir()
            replacement = parent / path.name
            replacement.write_bytes(
                safe_io.canonical_storage_bytes({"schema_version": "unit-v1", "value": "new"})
            )
            replacement.chmod(0o600)
        return real_require(*args, **kwargs)

    monkeypatch.setattr(safe_io, "_require_entry_identity", swap_during_final_entry_check)
    with pytest.raises(CampaignError, match="parent|changed"):
        safe_io.strict_json_load(path)


def test_sha256_rejects_parent_replacement_during_hash(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    parent = tmp_path / "parent"
    parent.mkdir()
    displaced = tmp_path / "displaced-parent"
    path = parent / "payload.bin"
    path.write_bytes(b"owned payload")
    real_read = os.read
    swapped = False

    def racing_read(descriptor: int, size: int) -> bytes:
        nonlocal swapped
        if not swapped:
            swapped = True
            parent.rename(displaced)
            parent.mkdir()
            (parent / path.name).write_bytes(b"replacement")
        return real_read(descriptor, size)

    monkeypatch.setattr(safe_io.os, "read", racing_read)
    with pytest.raises(CampaignError, match="parent|changed"):
        safe_io.sha256_regular_file(path)


def test_sha256_rechecks_parent_after_final_entry_check(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    parent = tmp_path / "parent"
    parent.mkdir()
    displaced = tmp_path / "displaced-parent"
    path = parent / "payload.bin"
    path.write_bytes(b"owned payload")
    real_lstat_at = safe_io._lstat_at
    calls = 0

    def swap_during_final_entry_check(descriptor: int, name: str):
        nonlocal calls
        calls += 1
        if calls == 2:
            parent.rename(displaced)
            parent.mkdir()
            (parent / path.name).write_bytes(b"replacement")
        return real_lstat_at(descriptor, name)

    monkeypatch.setattr(safe_io, "_lstat_at", swap_during_final_entry_check)
    with pytest.raises(CampaignError, match="parent|changed"):
        safe_io.sha256_regular_file(path)


@pytest.mark.parametrize("operation", ["create", "replace"])
def test_json_writers_recheck_parent_after_final_entry_check(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    operation: str,
) -> None:
    parent = tmp_path / "parent"
    parent.mkdir()
    displaced = tmp_path / "displaced-parent"
    path = parent / "artifact.json"
    if operation == "replace":
        safe_io.atomic_create_json(path, {"schema_version": "unit-v1", "value": "old"})
    real_require = safe_io._require_entry_identity
    armed = False
    swapped = False

    def arm_final_check(stage: str) -> None:
        nonlocal armed
        if stage == "after_parent_fsync":
            armed = True

    def swap_during_final_entry_check(*args: object, **kwargs: object):
        nonlocal swapped
        if armed and not swapped:
            swapped = True
            parent.rename(displaced)
            parent.mkdir()
        return real_require(*args, **kwargs)

    monkeypatch.setattr(safe_io, "_require_entry_identity", swap_during_final_entry_check)
    writer = (
        safe_io.atomic_create_json if operation == "create" else safe_io.atomic_write_json_durable
    )
    with pytest.raises(CampaignError, match="parent|changed|replaced|protected"):
        writer(
            path,
            {"schema_version": "unit-v1", "value": "new"},
            fault_hook=arm_final_check,
        )


def test_exact_scan_rejects_extra_symlink_and_wrong_types(tmp_path: Path) -> None:
    root = tmp_path / "scan"
    root.mkdir()
    (root / "a.json").write_text("{}", encoding="utf-8")
    (root / "logs").mkdir()
    assert set(safe_io.exact_directory_scan(root, {"a.json": "file", "logs": "directory"})) == {
        "a.json",
        "logs",
    }
    (root / "extra").write_text("x", encoding="utf-8")
    with pytest.raises(CampaignError, match="entries"):
        safe_io.exact_directory_scan(root, {"a.json": "file", "logs": "directory"})
    (root / "extra").unlink()
    (root / "alias").symlink_to(root / "a.json")
    with pytest.raises(CampaignError, match="entries|symlink"):
        safe_io.exact_directory_scan(root, {"a.json": "file", "logs": "directory", "alias": "file"})


def test_a_scan_can_tolerate_a_file_the_owner_is_still_writing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An in-flight attempt directory cannot be scanned exactly, and must not be.

    `exact_directory_scan` stats every entry, rescans, and requires the two
    passes to agree on `st_size` and `st_mtime_ns`. That is the right contract
    for settled evidence and an impossible one for a directory whose owner is
    still working in it: a DuckDB `.wal` grows continuously, so `status` on a
    healthy campaign aborted with "directory entry changed during scan" the
    moment a poll landed mid-write -- which ended a 36-job run at 14 jobs.

    `require_stable=False` keeps every check that detects tampering -- the name
    set, symlink rejection, entry types, and device/inode/mode/link-count
    identity across both passes -- and drops only the claim that the bytes did
    not move.
    """
    root = tmp_path / "scratch"
    root.mkdir()
    growing = root / "db.duckdb.wal"
    growing.write_bytes(b"x")
    expected = {"db.duckdb.wal": "file"}

    real_stat = os.stat
    appended = False

    def append_between_passes(*args, **kwargs):
        nonlocal appended
        item = real_stat(*args, **kwargs)
        if not appended and args and args[0] == "db.duckdb.wal":
            appended = True
            with open(growing, "ab") as handle:
                handle.write(b"y" * 4096)
        return item

    monkeypatch.setattr(os, "stat", append_between_passes)
    with pytest.raises(CampaignError, match="changed during scan|changed during exact scan"):
        safe_io.exact_directory_scan(root, expected)

    appended = False
    entries = safe_io.exact_directory_scan(root, expected, require_stable=False)
    assert set(entries) == {"db.duckdb.wal"}


def test_a_tolerant_scan_still_refuses_a_stray_entry_or_a_symlink(tmp_path: Path) -> None:
    """Tolerating a growing file is not tolerating a different directory."""
    root = tmp_path / "scratch"
    root.mkdir()
    (root / "db.duckdb").write_text("{}", encoding="utf-8")
    expected = {"db.duckdb": "file"}
    assert set(safe_io.exact_directory_scan(root, expected, require_stable=False)) == {"db.duckdb"}

    (root / "stray").write_text("x", encoding="utf-8")
    with pytest.raises(CampaignError, match="entries"):
        safe_io.exact_directory_scan(root, expected, require_stable=False)
    (root / "stray").unlink()

    (root / "alias").symlink_to(root / "db.duckdb")
    with pytest.raises(CampaignError, match="entries|symlink"):
        safe_io.exact_directory_scan(
            root, {"db.duckdb": "file", "alias": "file"}, require_stable=False
        )


def test_exact_scan_rechecks_for_entries_added_during_the_scan(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "scan"
    root.mkdir()
    (root / "a").write_text("a", encoding="utf-8")
    real_stat = os.stat
    injected = False

    def racing_stat(
        path: str | bytes | int,
        *,
        dir_fd: int | None = None,
        follow_symlinks: bool = True,
    ) -> os.stat_result:
        nonlocal injected
        if dir_fd is not None and not injected:
            injected = True
            (root / "extra").write_text("extra", encoding="utf-8")
        return real_stat(path, dir_fd=dir_fd, follow_symlinks=follow_symlinks)

    monkeypatch.setattr(safe_io.os, "stat", racing_stat)
    with pytest.raises(CampaignError, match="entries|changed"):
        safe_io.exact_directory_scan(root, {"a": "file"})


def test_digest_bound_lock_blocks_reentry_and_releases_exact_owner(tmp_path: Path) -> None:
    campaign = tmp_path / "run"
    campaign.mkdir()
    digest = "a" * 64
    with safe_io.campaign_lock(
        campaign,
        digest,
        identity_provider=_owner,
        nonce_provider=lambda: "b" * 32,
        now_provider=lambda: "2026-08-30T12:00:00Z",
    ) as owner:
        assert owner == {
            "schema_version": safe_io.LOCK_SCHEMA,
            "campaign_sha256": digest,
            **_owner(),
            "started_utc": "2026-08-30T12:00:00Z",
            "lock_nonce": "b" * 32,
        }
        with pytest.raises(CampaignError, match="locked"):
            with safe_io.campaign_lock(campaign, digest, identity_provider=_owner):
                pass
    assert not _lock_path(campaign).exists()


def test_lock_namespace_survives_campaign_directory_aba_replacement(tmp_path: Path) -> None:
    campaign = tmp_path / "run"
    campaign.mkdir()
    displaced = tmp_path / "displaced-run"
    digest = "a" * 64
    context = safe_io.campaign_lock(
        campaign,
        digest,
        identity_provider=_owner,
        nonce_provider=lambda: "b" * 32,
        now_provider=lambda: "2026-08-30T12:00:00Z",
    )
    context.__enter__()
    campaign.rename(displaced)
    campaign.mkdir()
    try:
        with pytest.raises(CampaignError, match="locked"):
            with safe_io.campaign_lock(campaign, digest, identity_provider=_owner):
                pass
    finally:
        campaign.rmdir()
        displaced.rename(campaign)
    context.__exit__(None, None, None)


def test_lock_never_breaks_malformed_or_foreign_existing_owner(tmp_path: Path) -> None:
    campaign = tmp_path / "run"
    campaign.mkdir()
    lock = _lock_path(campaign)
    lock.mkdir(parents=True, mode=0o700)
    (lock / "owner.json").write_text("not-json", encoding="utf-8")
    with pytest.raises(CampaignError, match="locked|owner"):
        with safe_io.campaign_lock(campaign, "a" * 64, identity_provider=_owner):
            pass
    assert lock.exists()


def test_lock_owner_rejects_control_characters_and_bool_process_ids() -> None:
    owner = {
        "schema_version": safe_io.LOCK_SCHEMA,
        "campaign_sha256": "a" * 64,
        **_owner(),
        "started_utc": "2026-08-30T12:00:00Z",
        "lock_nonce": "b" * 32,
    }
    for key, replacement in (("host", "bad\nhost"), ("pid", True)):
        broken = dict(owner)
        broken[key] = replacement
        with pytest.raises(CampaignError):
            safe_io.validate_lock_owner(broken)


def test_lock_tampering_is_detected_and_not_cleaned(tmp_path: Path) -> None:
    campaign = tmp_path / "run"
    campaign.mkdir()
    context = safe_io.campaign_lock(
        campaign,
        "a" * 64,
        identity_provider=_owner,
        nonce_provider=lambda: "b" * 32,
        now_provider=lambda: "2026-08-30T12:00:00Z",
    )
    context.__enter__()
    owner_path = _lock_path(campaign) / "owner.json"
    owner_path.write_text(json.dumps({"tampered": True}), encoding="utf-8")
    with pytest.raises(CampaignError, match="tamper|owner"):
        context.__exit__(None, None, None)
    assert _lock_path(campaign).exists()


def test_lock_release_rechecks_owner_after_atomic_history_move(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    campaign = tmp_path / "run"
    campaign.mkdir()
    lock = _lock_path(campaign)
    context = safe_io.campaign_lock(
        campaign,
        "a" * 64,
        identity_provider=_owner,
        nonce_provider=lambda: "b" * 32,
        now_provider=lambda: "2026-08-30T12:00:00Z",
    )
    context.__enter__()
    real_noreplace = safe_io._rename_noreplace_at
    tampered = False

    def tamper_before_history_move(
        source_descriptor: int,
        source_name: str,
        destination_descriptor: int,
        destination_name: str,
    ) -> None:
        nonlocal tampered
        if source_name == lock.name and not tampered:
            tampered = True
            (lock / "owner.json").write_text('{"tampered":true}\n', encoding="utf-8")
        real_noreplace(
            source_descriptor,
            source_name,
            destination_descriptor,
            destination_name,
        )

    monkeypatch.setattr(safe_io, "_rename_noreplace_at", tamper_before_history_move)
    with pytest.raises(CampaignError, match="tamper|owner"):
        context.__exit__(None, None, None)


def test_lock_release_rebinds_canonical_history_namespace(tmp_path: Path) -> None:
    campaign = tmp_path / "run"
    campaign.mkdir()
    context = safe_io.campaign_lock(
        campaign,
        "a" * 64,
        identity_provider=_owner,
        nonce_provider=lambda: "b" * 32,
        now_provider=lambda: "2026-08-30T12:00:00Z",
    )
    context.__enter__()
    lock_root = safe_io._campaign_lock_namespace_path(campaign).parent
    displaced_root = tmp_path / "displaced-lock-root"
    lock_root.rename(displaced_root)
    lock_root.mkdir(mode=0o700)

    with pytest.raises(CampaignError, match="lock root|namespace|replaced"):
        context.__exit__(None, None, None)
    assert _lock_path(campaign).is_dir()


def test_lock_closes_active_descriptor_when_owner_creation_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    campaign = tmp_path / "run"
    campaign.mkdir()
    captured: list[int] = []
    closed: list[int] = []
    real_close = os.close

    def fail_owner(descriptor: int, name: str, data: bytes) -> tuple[int, int]:
        captured.append(descriptor)
        raise RuntimeError("injected owner failure")

    def recording_close(descriptor: int) -> None:
        closed.append(descriptor)
        real_close(descriptor)

    monkeypatch.setattr(safe_io, "_create_bytes_at", fail_owner)
    monkeypatch.setattr(safe_io.os, "close", recording_close)
    context = safe_io.campaign_lock(
        campaign,
        "a" * 64,
        identity_provider=_owner,
        nonce_provider=lambda: "b" * 32,
        now_provider=lambda: "2026-08-30T12:00:00Z",
    )
    with pytest.raises(RuntimeError, match="owner failure"):
        context.__enter__()

    assert len(captured) == 1
    assert captured[0] in closed


def test_lock_closes_active_descriptor_when_directory_setup_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    campaign = tmp_path / "run"
    campaign.mkdir()
    active = _lock_path(campaign)
    real_fchmod = os.fchmod

    def fail_active_fchmod(descriptor: int, mode: int) -> None:
        try:
            opened_path = Path(os.readlink(f"/proc/self/fd/{descriptor}"))
        except OSError:
            opened_path = Path()
        if opened_path.name == active.name:
            raise PermissionError("injected active lock setup failure")
        real_fchmod(descriptor, mode)

    monkeypatch.setattr(safe_io.os, "fchmod", fail_active_fchmod)
    before = _open_fd_count()
    with pytest.raises(PermissionError, match="injected"):
        with safe_io.campaign_lock(
            campaign,
            "a" * 64,
            identity_provider=_owner,
            nonce_provider=lambda: "b" * 32,
            now_provider=lambda: "2026-08-30T12:00:00Z",
        ):
            pass
    assert _open_fd_count() == before


def test_lock_releases_cleanly_when_the_protected_body_raises(tmp_path: Path) -> None:
    campaign = tmp_path / "run"
    campaign.mkdir()
    with pytest.raises(RuntimeError, match="body"):
        with safe_io.campaign_lock(
            campaign,
            "a" * 64,
            identity_provider=_owner,
            nonce_provider=lambda: "b" * 32,
            now_provider=lambda: "2026-08-30T12:00:00Z",
        ):
            raise RuntimeError("body")
    assert not _lock_path(campaign).exists()


def test_lock_release_retains_owner_without_unlink_or_rmdir(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    campaign = tmp_path / "run"
    campaign.mkdir()
    context = safe_io.campaign_lock(
        campaign,
        "a" * 64,
        identity_provider=_owner,
        nonce_provider=lambda: "b" * 32,
        now_provider=lambda: "2026-08-30T12:00:00Z",
    )
    context.__enter__()

    def forbidden(*args: object, **kwargs: object) -> None:
        raise AssertionError("lock release must retain its forensic owner")

    monkeypatch.setattr(safe_io.os, "unlink", forbidden)
    monkeypatch.setattr(safe_io.os, "rmdir", forbidden)
    context.__exit__(None, None, None)

    namespace = safe_io._campaign_lock_namespace_path(campaign)
    released = list(namespace.glob("released-*"))
    assert not _lock_path(campaign).exists()
    assert len(released) == 1
    assert (released[0] / "owner.json").is_file()


def test_lock_release_fsyncs_both_cross_directory_rename_parents(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    campaign = tmp_path / "run"
    campaign.mkdir()
    context = safe_io.campaign_lock(
        campaign,
        "a" * 64,
        identity_provider=_owner,
        nonce_provider=lambda: "b" * 32,
        now_provider=lambda: "2026-08-30T12:00:00Z",
    )
    context.__enter__()
    stable_parent_identity = (tmp_path.stat().st_dev, tmp_path.stat().st_ino)
    namespace = safe_io._campaign_lock_namespace_path(campaign)
    namespace_identity = (namespace.stat().st_dev, namespace.stat().st_ino)
    real_fsync = os.fsync
    synced_directories: list[tuple[int, int]] = []

    def recording_fsync(descriptor: int) -> None:
        item = os.fstat(descriptor)
        if stat.S_ISDIR(item.st_mode):
            synced_directories.append((item.st_dev, item.st_ino))
        real_fsync(descriptor)

    monkeypatch.setattr(safe_io.os, "fsync", recording_fsync)
    context.__exit__(None, None, None)

    assert namespace_identity in synced_directories
    assert stable_parent_identity in synced_directories


def test_lock_release_never_removes_a_replacement_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    campaign = tmp_path / "run"
    campaign.mkdir()
    lock = _lock_path(campaign)
    displaced = lock.parent / "displaced-active"
    context = safe_io.campaign_lock(
        campaign,
        "a" * 64,
        identity_provider=_owner,
        nonce_provider=lambda: "b" * 32,
        now_provider=lambda: "2026-08-30T12:00:00Z",
    )
    context.__enter__()
    real_read = safe_io._read_file_at
    swapped = False

    def swap_before_read(*args: object, **kwargs: object):
        nonlocal swapped
        if not swapped:
            swapped = True
            lock.rename(displaced)
            lock.mkdir(mode=0o700)
        return real_read(*args, **kwargs)

    monkeypatch.setattr(safe_io, "_read_file_at", swap_before_read)
    with pytest.raises(CampaignError, match="replaced|removed|identity"):
        context.__exit__(None, None, None)
    assert lock.is_dir()
    assert displaced.is_dir()
    assert (displaced / "owner.json").is_file()


def test_lock_binds_the_canonical_campaign_directory(tmp_path: Path) -> None:
    campaign = tmp_path / "run"
    campaign.mkdir()
    displaced = tmp_path / "displaced-run"
    lock = _lock_path(campaign)
    with pytest.raises(CampaignError, match="campaign directory.*changed|identity|replaced"):
        with safe_io.campaign_lock(
            campaign,
            "a" * 64,
            identity_provider=_owner,
            nonce_provider=lambda: "b" * 32,
            now_provider=lambda: "2026-08-30T12:00:00Z",
        ):
            campaign.rename(displaced)
            campaign.mkdir()
    assert (lock / "owner.json").is_file()


def test_lock_rejects_directory_replacement_between_lstat_and_open(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    campaign = tmp_path / "run"
    campaign.mkdir()
    lock = _lock_path(campaign)
    displaced = lock.parent / "displaced-active"
    real_open = os.open
    swapped = False

    def racing_open(
        name: str | bytes,
        flags: int,
        mode: int = 0o777,
        *,
        dir_fd: int | None = None,
    ) -> int:
        nonlocal swapped
        if name == lock.name and dir_fd is not None and not swapped:
            swapped = True
            lock.rename(displaced)
            lock.mkdir(mode=0o700)
        return real_open(name, flags, mode, dir_fd=dir_fd)

    monkeypatch.setattr(safe_io.os, "open", racing_open)
    with pytest.raises(CampaignError, match="lock.*changed|identity"):
        with safe_io.campaign_lock(
            campaign,
            "a" * 64,
            identity_provider=_owner,
            nonce_provider=lambda: "b" * 32,
            now_provider=lambda: "2026-08-30T12:00:00Z",
        ):
            pass
    assert lock.is_dir()
    assert displaced.is_dir()


def test_identity_unlink_quarantines_a_path_replaced_at_rename(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "owned.json"
    displaced = tmp_path / "displaced-owned.json"
    replacement = {"schema_version": "unit-v1", "value": "foreign"}
    safe_io.atomic_create_json(path, {"schema_version": "unit-v1", "value": "owned"})
    expected = path.stat()
    real_rename = os.rename
    real_noreplace = safe_io._rename_noreplace_at
    swapped = False

    def racing_rename(
        source_descriptor: int,
        source: str,
        destination_descriptor: int,
        destination: str,
    ) -> None:
        nonlocal swapped
        if source == path.name and destination == "entry" and not swapped:
            swapped = True
            real_rename(path, displaced)
            path.write_bytes(safe_io.canonical_storage_bytes(replacement))
            path.chmod(0o600)
        real_noreplace(
            source_descriptor,
            source,
            destination_descriptor,
            destination,
        )

    monkeypatch.setattr(safe_io, "_rename_noreplace_at", racing_rename)
    with safe_io._open_parent(path) as (parent_descriptor, name, _parent, _identity):
        with pytest.raises(CampaignError, match="replaced"):
            safe_io._unlink_if_identity(
                parent_descriptor,
                name,
                (expected.st_dev, expected.st_ino),
            )
    assert safe_io.strict_json_load(path) == replacement
    assert safe_io.strict_json_load(displaced)["value"] == "owned"


def test_identity_retirement_never_calls_unlink_or_rmdir(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "owned.json"
    safe_io.atomic_create_json(path, {"schema_version": "unit-v1", "value": "owned"})
    expected = path.stat()

    def forbidden(*args: object, **kwargs: object) -> None:
        raise AssertionError("retirement must retain its forensic tombstone")

    monkeypatch.setattr(safe_io.os, "unlink", forbidden)
    monkeypatch.setattr(safe_io.os, "rmdir", forbidden)
    with safe_io._open_parent(path) as (parent_descriptor, name, _parent, _identity):
        safe_io._unlink_if_identity(
            parent_descriptor,
            name,
            (expected.st_dev, expected.st_ino),
        )

    assert not path.exists()
    tombstones = _retired_entries(tmp_path)
    assert len(tombstones) == 1
    entry = tombstones[0]
    assert (entry.stat().st_dev, entry.stat().st_ino) == (
        expected.st_dev,
        expected.st_ino,
    )


def test_identity_retirement_closes_descriptors_when_quarantine_allocation_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "owned.json"
    safe_io.atomic_create_json(path, {"schema_version": "unit-v1", "value": "owned"})
    expected = path.stat()
    real_mkdir = os.mkdir

    def fail_quarantine_mkdir(
        name: str | bytes,
        mode: int = 0o777,
        *,
        dir_fd: int | None = None,
    ) -> None:
        if str(name).startswith("retired-"):
            raise PermissionError("injected")
        real_mkdir(name, mode, dir_fd=dir_fd)

    monkeypatch.setattr(safe_io.os, "mkdir", fail_quarantine_mkdir)
    before = _open_fd_count()
    with safe_io._open_parent(path) as (parent_descriptor, name, _parent, _identity):
        with pytest.raises((CampaignError, PermissionError), match="injected|quarantine"):
            safe_io._unlink_if_identity(
                parent_descriptor,
                name,
                (expected.st_dev, expected.st_ino),
            )
    assert _open_fd_count() == before
    assert path.exists()
