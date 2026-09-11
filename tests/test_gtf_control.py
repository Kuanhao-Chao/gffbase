"""Crash and forgery tests for the parent-stripped GTF control artifact."""

from __future__ import annotations

import gzip
import os
import stat
from pathlib import Path

import pytest

from benchmarks import prepare_gtf_control as gtf_control
from benchmarks.campaign import safe_io
from benchmarks.campaign.model import CampaignError
from benchmarks.prepare_gtf_control import (
    TRANSFORM_DESCRIPTION,
    build_parent_stripped_gtf,
    validate_parent_stripped_gtf,
)
from tests._platform import LINUX_ONLY_CAMPAIGN

pytestmark = LINUX_ONLY_CAMPAIGN

SOURCE_TEXT = (
    "##gff-version 3\r\n"
    "\r\n"
    'chr1\tsrc\tgene\t1\t20\t.\t+\t.\tgene_id "g";\r\n'
    'chr1\tsrc\ttranscript\t1\t20\t.\t+\t.\tgene_id "g"; transcript_id "t";\n'
    'chr1\tsrc\texon\t1\t10\t.\t+\t.\tgene_id "g"; transcript_id "t";\r\n'
    'chr1\tsrc\tCDS\t3\t9\t.\t+\t0\tgene_id "g"; transcript_id "t";\n'
    'chr1\tsrc\tGene\t30\t40\t.\t+\t.\tgene_id "case-sensitive";'
)
EXPECTED_TEXT = (
    "##gff-version 3\r\n"
    "\r\n"
    'chr1\tsrc\texon\t1\t10\t.\t+\t.\tgene_id "g"; transcript_id "t";\r\n'
    'chr1\tsrc\tCDS\t3\t9\t.\t+\t0\tgene_id "g"; transcript_id "t";\n'
    'chr1\tsrc\tGene\t30\t40\t.\t+\t.\tgene_id "case-sensitive";'
)


def _write_gzip(path: Path, payload: bytes) -> None:
    with path.open("wb") as raw:
        with gzip.GzipFile(filename="", mode="wb", fileobj=raw, mtime=0) as compressed:
            compressed.write(payload)
    path.chmod(0o600)


def _paths(tmp_path: Path) -> tuple[Path, Path, Path]:
    source = tmp_path / "source.gtf.gz"
    output = tmp_path / "parent-stripped.gtf.gz"
    manifest = tmp_path / "parent-stripped.manifest.json"
    _write_gzip(source, SOURCE_TEXT.encode())
    return source, output, manifest


def _open_fd_count() -> int:
    return len(os.listdir("/proc/self/fd"))


def _descriptor_name(descriptor: int) -> str:
    try:
        return Path(os.readlink(f"/proc/self/fd/{descriptor}")).name
    except OSError:
        return ""


def test_transform_preserves_retained_bytes_and_commits_manifest_last(tmp_path: Path) -> None:
    source, output, manifest_path = _paths(tmp_path)
    manifest = build_parent_stripped_gtf(
        source,
        output,
        manifest_path,
        now_provider=lambda: "2026-08-30T12:00:00Z",
    )

    with gzip.open(output, "rt", encoding="utf-8", newline="") as handle:
        assert handle.read() == EXPECTED_TEXT
    assert manifest == validate_parent_stripped_gtf(source, output, manifest_path)
    assert manifest["schema_version"] == "1"
    assert manifest["transform"] == TRANSFORM_DESCRIPTION
    assert manifest["generated_utc"] == "2026-08-30T12:00:00Z"
    assert manifest["source"]["gzip_crc_ok"] is True
    assert manifest["output"]["gzip_crc_ok"] is True
    assert manifest["counts"] == {
        "input_feature_lines": 5,
        "output_feature_lines": 3,
        "comment_or_blank_lines": 2,
        "removed_gene_rows": 1,
        "removed_transcript_rows": 1,
    }
    assert stat.S_IMODE(output.stat().st_mode) == 0o600
    assert stat.S_IMODE(manifest_path.stat().st_mode) == 0o600
    assert safe_io.strict_json_load(manifest_path) == manifest


def test_existing_valid_pair_is_zero_write_idempotent(tmp_path: Path) -> None:
    source, output, manifest_path = _paths(tmp_path)
    first = build_parent_stripped_gtf(
        source,
        output,
        manifest_path,
        now_provider=lambda: "2026-08-30T12:00:00Z",
    )
    identities = {
        path: (path.stat().st_ino, path.stat().st_mtime_ns, path.read_bytes())
        for path in (output, manifest_path)
    }
    second = build_parent_stripped_gtf(
        source,
        output,
        manifest_path,
        now_provider=lambda: "2099-01-01T00:00:00Z",
    )
    assert second == first
    assert identities == {
        path: (path.stat().st_ino, path.stat().st_mtime_ns, path.read_bytes())
        for path in (output, manifest_path)
    }


@pytest.mark.parametrize("orphan", ["output", "manifest"])
def test_partial_existing_state_is_never_treated_as_complete(tmp_path: Path, orphan: str) -> None:
    source, output, manifest_path = _paths(tmp_path)
    if orphan == "output":
        _write_gzip(output, b"orphan\n")
    else:
        safe_io.atomic_create_json(manifest_path, {"schema_version": "1"})
    before = {path: path.read_bytes() for path in (output, manifest_path) if path.exists()}
    with pytest.raises(CampaignError, match="partial|both"):
        build_parent_stripped_gtf(source, output, manifest_path)
    assert before == {path: path.read_bytes() for path in before}


def test_transform_rejects_lexical_symlink_and_hardlink_aliases(tmp_path: Path) -> None:
    source, output, manifest_path = _paths(tmp_path)
    with pytest.raises(CampaignError, match="distinct|alias"):
        build_parent_stripped_gtf(source, source, manifest_path)
    with pytest.raises(CampaignError, match="distinct|alias"):
        build_parent_stripped_gtf(source, output, output)

    hard_output = tmp_path / "hard-output.gtf.gz"
    os.link(source, hard_output)
    with pytest.raises(CampaignError, match="alias"):
        build_parent_stripped_gtf(source, hard_output, manifest_path)

    linked_parent = tmp_path / "linked-parent"
    linked_parent.symlink_to(tmp_path, target_is_directory=True)
    with pytest.raises(CampaignError, match="symlink"):
        build_parent_stripped_gtf(
            source,
            linked_parent / "out.gtf.gz",
            linked_parent / "manifest.json",
        )


def test_existing_self_consistent_forgery_fails_streaming_replay(tmp_path: Path) -> None:
    source, output, manifest_path = _paths(tmp_path)
    manifest = build_parent_stripped_gtf(source, output, manifest_path)
    forged_text = EXPECTED_TEXT.replace("chr1\tsrc\texon", "chr2\tsrc\texon")
    _write_gzip(output, forged_text.encode())
    manifest["output"]["bytes"] = output.stat().st_size
    manifest["output"]["sha256"] = safe_io.sha256_regular_file(output)
    safe_io.atomic_write_json_durable(manifest_path, manifest)

    with pytest.raises(CampaignError, match="replay|retained|differ"):
        validate_parent_stripped_gtf(source, output, manifest_path)
    with pytest.raises(CampaignError):
        build_parent_stripped_gtf(source, output, manifest_path)


def test_existing_pair_must_use_the_deterministic_gzip_encoding(tmp_path: Path) -> None:
    source, output, manifest_path = _paths(tmp_path)
    manifest = build_parent_stripped_gtf(source, output, manifest_path)
    with output.open("wb") as raw:
        with gzip.GzipFile(
            filename="forged-parent-stripped.gtf",
            mode="wb",
            fileobj=raw,
            compresslevel=1,
            mtime=123,
        ) as compressed:
            compressed.write(EXPECTED_TEXT.encode())
    output.chmod(0o600)
    manifest["output"]["bytes"] = output.stat().st_size
    manifest["output"]["sha256"] = safe_io.sha256_regular_file(output)
    safe_io.atomic_write_json_durable(manifest_path, manifest)

    with pytest.raises(CampaignError, match="deterministic|gzip|compressed"):
        validate_parent_stripped_gtf(source, output, manifest_path)


def test_existing_output_with_unsafe_permissions_is_rejected(tmp_path: Path) -> None:
    source, output, manifest_path = _paths(tmp_path)
    build_parent_stripped_gtf(source, output, manifest_path)
    output.chmod(0o644)
    with pytest.raises(CampaignError, match="mode|permission"):
        validate_parent_stripped_gtf(source, output, manifest_path)


def test_validator_rejects_output_hardlinked_between_lstat_and_open(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source, output, manifest_path = _paths(tmp_path)
    build_parent_stripped_gtf(source, output, manifest_path)
    alias = tmp_path / "output-alias.gtf.gz"
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
        if name == output.name and dir_fd is not None and not raced:
            raced = True
            os.link(output, alias)
        return real_open(name, flags, mode, dir_fd=dir_fd)

    monkeypatch.setattr(gtf_control.os, "open", racing_open)
    with pytest.raises(CampaignError, match="linked|changed|alias"):
        validate_parent_stripped_gtf(source, output, manifest_path)


def test_validator_rejects_manifest_replacement_during_replay(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source, output, manifest_path = _paths(tmp_path)
    build_parent_stripped_gtf(source, output, manifest_path)
    displaced = tmp_path / "displaced-manifest.json"
    replacement = {"schema_version": "foreign"}
    real_replay = gtf_control._replay_transform
    swapped = False

    def replay_then_swap(source_path: Path, output_path: Path):
        nonlocal swapped
        replayed = real_replay(source_path, output_path)
        if not swapped:
            swapped = True
            manifest_path.rename(displaced)
            safe_io.atomic_create_json(manifest_path, replacement)
        return replayed

    monkeypatch.setattr(gtf_control, "_replay_transform", replay_then_swap)
    with pytest.raises(CampaignError, match="changed|snapshot|identity"):
        validate_parent_stripped_gtf(source, output, manifest_path)
    assert safe_io.strict_json_load(manifest_path) == replacement


def test_validator_rejects_common_parent_symlink_swap_at_final_snapshot(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    parent = tmp_path / "artifacts"
    parent.mkdir()
    source, output, manifest_path = _paths(parent)
    build_parent_stripped_gtf(source, output, manifest_path)
    displaced = tmp_path / "displaced-artifacts"
    real_versions = gtf_control._transform_versions
    calls = 0

    def swap_before_final_versions(paths: tuple[Path, Path, Path]):
        nonlocal calls
        calls += 1
        if calls == 2:
            parent.rename(displaced)
            parent.symlink_to(displaced, target_is_directory=True)
        return real_versions(paths)

    monkeypatch.setattr(gtf_control, "_transform_versions", swap_before_final_versions)
    with pytest.raises(CampaignError, match="symlink|changed|identity|replaced"):
        validate_parent_stripped_gtf(source, output, manifest_path)


def test_public_validator_cannot_accept_marker_before_transform_lock_releases(
    tmp_path: Path,
) -> None:
    source, output, manifest_path = _paths(tmp_path)
    observed_lock = False

    def fail(stage: str) -> None:
        nonlocal observed_lock
        if stage != "manifest_after_commit":
            return
        with pytest.raises(CampaignError, match="locked"):
            validate_parent_stripped_gtf(source, output, manifest_path)
        observed_lock = True
        raise RuntimeError("injected")

    with pytest.raises(RuntimeError, match="injected"):
        build_parent_stripped_gtf(source, output, manifest_path, fault_hook=fail)
    assert observed_lock


def test_existing_pair_retry_repairs_manifest_parent_durability(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source_dir = tmp_path / "source"
    output_dir = tmp_path / "output"
    manifest_dir = tmp_path / "manifest"
    source_dir.mkdir()
    output_dir.mkdir()
    manifest_dir.mkdir()
    source = source_dir / "source.gtf.gz"
    output = output_dir / "parent-stripped.gtf.gz"
    manifest_path = manifest_dir / "parent-stripped.manifest.json"
    _write_gzip(source, SOURCE_TEXT.encode())

    def fail(stage: str) -> None:
        if stage == "manifest_before_parent_fsync":
            raise RuntimeError("injected")

    with pytest.raises(RuntimeError, match="injected"):
        build_parent_stripped_gtf(source, output, manifest_path, fault_hook=fail)

    manifest_parent_identity = (
        manifest_dir.stat().st_dev,
        manifest_dir.stat().st_ino,
    )
    real_fsync = os.fsync
    synced_directories: list[tuple[int, int]] = []

    def recording_fsync(descriptor: int) -> None:
        item = os.fstat(descriptor)
        if stat.S_ISDIR(item.st_mode):
            synced_directories.append((item.st_dev, item.st_ino))
        real_fsync(descriptor)

    monkeypatch.setattr(gtf_control.os, "fsync", recording_fsync)
    build_parent_stripped_gtf(source, output, manifest_path)

    assert manifest_parent_identity in synced_directories


@pytest.mark.parametrize(
    ("stage", "output_exists", "manifest_exists"),
    [
        ("before_output_commit", False, False),
        ("after_output_file_fsync", False, False),
        ("after_output_commit", True, False),
        ("after_output_parent_fsync", True, False),
        ("before_manifest_create", True, False),
        ("manifest_after_write", True, False),
        ("manifest_after_file_fsync", True, False),
        ("manifest_before_commit", True, False),
        ("manifest_after_commit", True, True),
        ("manifest_before_parent_fsync", True, True),
    ],
)
def test_transform_faults_expose_only_absent_or_complete_final_markers(
    tmp_path: Path,
    stage: str,
    output_exists: bool,
    manifest_exists: bool,
) -> None:
    source, output, manifest_path = _paths(tmp_path)

    def fail(current: str) -> None:
        if current == stage:
            raise RuntimeError("injected")

    with pytest.raises(RuntimeError, match="injected"):
        build_parent_stripped_gtf(source, output, manifest_path, fault_hook=fail)
    assert output.exists() is output_exists
    assert manifest_path.exists() is manifest_exists
    if manifest_exists:
        assert validate_parent_stripped_gtf(source, output, manifest_path)["counts"]
    else:
        with pytest.raises(CampaignError):
            validate_parent_stripped_gtf(source, output, manifest_path)


@pytest.mark.parametrize("stage", ["manifest_after_parent_fsync", "after_manifest_create"])
def test_fault_after_final_marker_leaves_a_valid_resumable_pair(tmp_path: Path, stage: str) -> None:
    source, output, manifest_path = _paths(tmp_path)

    def fail(current: str) -> None:
        if current == stage:
            raise RuntimeError("injected")

    with pytest.raises(RuntimeError, match="injected"):
        build_parent_stripped_gtf(source, output, manifest_path, fault_hook=fail)
    assert (
        validate_parent_stripped_gtf(source, output, manifest_path)["counts"][
            "output_feature_lines"
        ]
        == 3
    )


@pytest.mark.parametrize(
    "payload",
    [
        b"comment\nshort\trow\n",
        b'chr1\ts\tgene\t1\t2\t.\t+\t.\tgene_id "g";\nchr1\ts\texon\t1\t2\t.\t+\t.\tgene_id "g";\n',
        b"\xff\xfe\n",
    ],
)
def test_invalid_or_nonqualifying_source_never_commits(tmp_path: Path, payload: bytes) -> None:
    source = tmp_path / "source.gtf.gz"
    output = tmp_path / "output.gtf.gz"
    manifest = tmp_path / "manifest.json"
    _write_gzip(source, payload)
    with pytest.raises(CampaignError):
        build_parent_stripped_gtf(source, output, manifest)
    assert not output.exists()
    assert not manifest.exists()


def test_corrupt_source_gzip_trailer_is_rejected(tmp_path: Path) -> None:
    source, output, manifest = _paths(tmp_path)
    raw = source.read_bytes()
    source.write_bytes(raw[:-4] + b"BAD!")
    with pytest.raises(CampaignError, match="gzip|source|read"):
        build_parent_stripped_gtf(source, output, manifest)
    assert not output.exists()
    assert not manifest.exists()


def test_corrupt_source_deflate_payload_is_reported_as_campaign_error(
    tmp_path: Path,
) -> None:
    source = tmp_path / "source.gtf.gz"
    output = tmp_path / "output.gtf.gz"
    manifest = tmp_path / "manifest.json"
    gzip_header = bytes.fromhex("1f8b08000000000000ff")
    invalid_deflate_block = b"\x07"  # BFINAL=1 with the reserved BTYPE=3.
    source.write_bytes(gzip_header + invalid_deflate_block + (b"\x00" * 8))
    source.chmod(0o600)

    with pytest.raises(CampaignError, match="gzip|source|read"):
        build_parent_stripped_gtf(source, output, manifest)
    assert not output.exists()
    assert not manifest.exists()


@pytest.mark.parametrize("mode", ["rb", "wb"])
def test_transform_closes_duplicate_when_fdopen_construction_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mode: str
) -> None:
    source, output, manifest = _paths(tmp_path)
    real_fdopen = os.fdopen
    injected = False

    def fail_selected_fdopen(descriptor: int, selected_mode: str, *args: object, **kwargs: object):
        nonlocal injected
        if selected_mode == mode and not injected:
            injected = True
            raise OSError(f"injected {mode} fdopen failure")
        return real_fdopen(descriptor, selected_mode, *args, **kwargs)

    monkeypatch.setattr(gtf_control.os, "fdopen", fail_selected_fdopen)
    before = _open_fd_count()
    with pytest.raises(CampaignError, match="injected|gzip|stream"):
        build_parent_stripped_gtf(source, output, manifest)
    assert injected
    assert _open_fd_count() == before
    assert not output.exists()
    assert not manifest.exists()


def test_transform_temporary_descriptor_closes_when_initial_fstat_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source, output, manifest = _paths(tmp_path)
    real_fstat = os.fstat
    injected = False

    def fail_temporary_fstat(descriptor: int) -> os.stat_result:
        nonlocal injected
        if not injected and _descriptor_name(descriptor).startswith(".gffbase-tmp-"):
            injected = True
            raise OSError("injected transform temporary fstat failure")
        return real_fstat(descriptor)

    monkeypatch.setattr(gtf_control.os, "fstat", fail_temporary_fstat)
    before = _open_fd_count()
    with pytest.raises(OSError, match="injected"):
        build_parent_stripped_gtf(source, output, manifest)
    assert injected
    assert _open_fd_count() == before
    assert not output.exists()
    assert not manifest.exists()


def test_output_commit_rolls_back_a_temporary_swapped_inside_noreplace(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source, output, manifest = _paths(tmp_path)
    displaced = tmp_path / "displaced-owned-output.gtf.gz"
    foreign = b"foreign output bytes"
    real_noreplace = safe_io._rename_noreplace_at
    swapped = False

    def swap_source_inside_rename(
        source_descriptor: int,
        source_name: str,
        destination_descriptor: int,
        destination_name: str,
    ) -> None:
        nonlocal swapped
        if destination_name == output.name and not swapped:
            swapped = True
            (tmp_path / source_name).rename(displaced)
            (tmp_path / source_name).write_bytes(foreign)
            (tmp_path / source_name).chmod(0o600)
        real_noreplace(
            source_descriptor,
            source_name,
            destination_descriptor,
            destination_name,
        )

    monkeypatch.setattr(safe_io, "_rename_noreplace_at", swap_source_inside_rename)
    with pytest.raises(CampaignError, match="temporary|commit|publish|replaced|identity"):
        build_parent_stripped_gtf(source, output, manifest)

    assert not output.exists()
    assert not manifest.exists()
    assert gzip.decompress(displaced.read_bytes()).decode() == EXPECTED_TEXT
    assert any(candidate.read_bytes() == foreign for candidate in tmp_path.glob(".gffbase-tmp-*"))


def test_force_removes_final_marker_before_rebuilding(tmp_path: Path) -> None:
    source, output, manifest_path = _paths(tmp_path)
    build_parent_stripped_gtf(source, output, manifest_path)
    stages: list[str] = []
    rebuilt = build_parent_stripped_gtf(
        source,
        output,
        manifest_path,
        force=True,
        fault_hook=stages.append,
        now_provider=lambda: "2026-08-30T12:00:00Z",
    )
    assert rebuilt == validate_parent_stripped_gtf(source, output, manifest_path)
    assert stages.index("after_force_manifest_remove") < stages.index("after_force_output_remove")


def test_force_refuses_to_unlink_a_manifest_replaced_after_inspection(tmp_path: Path) -> None:
    source, output, manifest_path = _paths(tmp_path)
    build_parent_stripped_gtf(source, output, manifest_path)
    replacement = {"schema_version": "replacement-v1"}

    def replace_at_boundary(stage: str) -> None:
        if stage != "before_force_manifest_remove":
            return
        displaced = tmp_path / "displaced.manifest.json"
        manifest_path.rename(displaced)
        safe_io.atomic_create_json(manifest_path, replacement)

    with pytest.raises(CampaignError, match="changed|identity|replaced"):
        build_parent_stripped_gtf(
            source,
            output,
            manifest_path,
            force=True,
            fault_hook=replace_at_boundary,
        )
    assert safe_io.strict_json_load(manifest_path) == replacement
