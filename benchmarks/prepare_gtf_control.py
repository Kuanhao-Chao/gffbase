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
"""Build the controlled parent-stripped GENCODE GTF benchmark input."""

from __future__ import annotations

import argparse
import contextlib
import gzip
import hashlib
import io
import os
import re
import secrets
import stat
import sys
import zlib
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from benchmarks.campaign import safe_io
from benchmarks.campaign.model import CampaignError, sha256_json
from benchmarks.corpora import BY_KEY, corpus_path

TRANSFORM_VERSION = "1"
TRANSFORM_DESCRIPTION = "remove rows whose third GTF column is gene or transcript"
REMOVED_FEATURETYPES = frozenset({"gene", "transcript"})
MANIFEST_SCHEMA = "1"

_MANIFEST_KEYS = {
    "schema_version",
    "transform",
    "transform_version",
    "generated_utc",
    "source",
    "output",
    "counts",
}
_FILE_IDENTITY_KEYS = {"path", "bytes", "sha256", "gzip_crc_ok"}
_COUNT_KEYS = {
    "input_feature_lines",
    "output_feature_lines",
    "comment_or_blank_lines",
    "removed_gene_rows",
    "removed_transcript_rows",
}
_SHA256_RE = re.compile(r"[0-9a-f]{64}\Z", re.ASCII)
_TIMESTAMP_RE = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z\Z", re.ASCII)
_PATH_VERSION_FIELDS = (
    "st_dev",
    "st_ino",
    "st_mode",
    "st_nlink",
    "st_size",
    "st_mtime_ns",
    "st_ctime_ns",
)

FaultHook = Callable[[str], None]


class _DigestWriter:
    """Minimal binary sink used to replay the exact deterministic gzip bytes."""

    def __init__(self) -> None:
        self._digest = hashlib.sha256()
        self.bytes_written = 0

    def write(self, data: bytes) -> int:
        self._digest.update(data)
        self.bytes_written += len(data)
        return len(data)

    def flush(self) -> None:
        return None

    def tell(self) -> int:
        return self.bytes_written

    def hexdigest(self) -> str:
        return self._digest.hexdigest()


def _fault(hook: FaultHook | None, stage: str) -> None:
    if hook is not None:
        hook(stage)


def _utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _valid_timestamp(value: object) -> bool:
    if type(value) is not str or _TIMESTAMP_RE.fullmatch(value) is None:
        return False
    try:
        parsed = datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ")
    except ValueError:
        return False
    return parsed.strftime("%Y-%m-%dT%H:%M:%SZ") == value


def _path_state(path: Path) -> os.stat_result | None:
    try:
        return os.lstat(path)
    except FileNotFoundError:
        return None


def _transform_versions(paths: tuple[Path, Path, Path]) -> tuple[tuple[int, ...], ...]:
    with contextlib.ExitStack() as stack:
        opened = [
            stack.enter_context(_open_regular_descriptor(path, require_unique=index > 0))
            for index, path in enumerate(paths)
        ]
        return tuple(
            tuple(int(getattr(item, field)) for field in _PATH_VERSION_FIELDS)
            for _descriptor, item in opened
        )


def _validate_transform_paths(
    source: Path, destination: Path, manifest_path: Path
) -> tuple[Path, Path, Path]:
    paths = tuple(
        safe_io.lexical_absolute(path, label)
        for path, label in (
            (source, "GTF source"),
            (destination, "GTF output"),
            (manifest_path, "GTF manifest"),
        )
    )
    if len(set(paths)) != 3:
        raise CampaignError("GTF source, output, and manifest must be distinct paths")
    source_path, output_path, manifest = paths
    safe_io.inspect_path(source_path, require_kind="file")
    safe_io.inspect_path(output_path, allow_missing_leaf=True)
    safe_io.inspect_path(manifest, allow_missing_leaf=True)
    states = [_path_state(path) for path in paths]
    identities: dict[tuple[int, int], Path] = {}
    for path, item in zip(paths, states, strict=True):
        if item is None:
            continue
        if stat.S_ISLNK(item.st_mode):
            raise CampaignError(f"symlink transform path is not allowed: {path}")
        if not stat.S_ISREG(item.st_mode):
            raise CampaignError(f"transform path must be a regular file: {path}")
        identity = (item.st_dev, item.st_ino)
        if identity in identities:
            raise CampaignError(
                f"transform paths alias the same file: {identities[identity]} and {path}"
            )
        identities[identity] = path
    for path, item in zip((output_path, manifest), states[1:], strict=True):
        if item is not None and item.st_nlink != 1:
            raise CampaignError(f"transform artifact is not uniquely linked: {path}")
        if item is not None and stat.S_IMODE(item.st_mode) != 0o600:
            raise CampaignError(
                f"transform artifact has unsafe mode or permissions; expected 0600: {path}"
            )
    return source_path, output_path, manifest


@contextmanager
def _open_regular_descriptor(
    path: Path, *, require_unique: bool = False
) -> Iterator[tuple[int, os.stat_result]]:
    target = safe_io.inspect_path(path, require_kind="file")
    inspected = os.lstat(target)
    with safe_io._open_parent(target) as (
        parent_descriptor,
        name,
        parent,
        parent_identity,
    ):
        before = safe_io._lstat_at(parent_descriptor, name)
        if before is None or not stat.S_ISREG(before.st_mode):
            raise CampaignError(f"missing or unsafe regular file: {target}")
        if require_unique and before.st_nlink != 1:
            raise CampaignError(f"regular file is not uniquely linked: {target}")
        if any(
            getattr(before, field) != getattr(inspected, field)
            for field in ("st_dev", "st_ino", "st_size", "st_mtime_ns", "st_ctime_ns")
        ):
            raise CampaignError(f"transform file changed before it was opened: {target}")
        flags = os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC
        try:
            descriptor = os.open(name, flags, dir_fd=parent_descriptor)
        except OSError as exc:
            raise CampaignError(f"cannot safely open regular file {target}: {exc}") from exc
        try:
            opened = os.fstat(descriptor)
            stable_fields = (
                "st_dev",
                "st_ino",
                "st_size",
                "st_mtime_ns",
                "st_ctime_ns",
            )
            if not stat.S_ISREG(opened.st_mode):
                raise CampaignError(f"opened transform path is not regular: {target}")
            if require_unique and opened.st_nlink != 1:
                raise CampaignError(f"opened transform artifact is hard linked: {target}")
            if any(getattr(opened, field) != getattr(before, field) for field in stable_fields):
                raise CampaignError(f"file identity changed while opening: {target}")
            yield descriptor, opened
            after = os.fstat(descriptor)
            if not stat.S_ISREG(after.st_mode) or (require_unique and after.st_nlink != 1):
                raise CampaignError(f"transform file link/type changed while read: {target}")
            if any(getattr(opened, field) != getattr(after, field) for field in stable_fields):
                raise CampaignError(f"file changed while it was read: {target}")
            safe_io._assert_parent_identity(parent, parent_descriptor, parent_identity)
            current = safe_io._lstat_at(parent_descriptor, name)
            if current is None or any(
                getattr(current, field) != getattr(after, field) for field in stable_fields
            ):
                raise CampaignError(f"transform path changed while it was read: {target}")
            safe_io._assert_parent_identity(parent, parent_descriptor, parent_identity)
        finally:
            os.close(descriptor)


def _hash_descriptor(descriptor: int) -> str:
    os.lseek(descriptor, 0, os.SEEK_SET)
    digest = hashlib.sha256()
    while True:
        chunk = os.read(descriptor, 1 << 20)
        if not chunk:
            break
        digest.update(chunk)
    os.lseek(descriptor, 0, os.SEEK_SET)
    return digest.hexdigest()


@contextmanager
def _gzip_reader(
    path: Path, *, require_unique: bool = False
) -> Iterator[tuple[io.TextIOWrapper, dict[str, object]]]:
    try:
        with _open_regular_descriptor(path, require_unique=require_unique) as (
            descriptor,
            item,
        ):
            identity = {
                "path": str(path),
                "bytes": item.st_size,
                "sha256": _hash_descriptor(descriptor),
                "gzip_crc_ok": True,
            }
            duplicate = os.dup(descriptor)
            try:
                raw = os.fdopen(duplicate, "rb")
            except BaseException:
                os.close(duplicate)
                raise
            with raw:
                with gzip.GzipFile(fileobj=raw, mode="rb") as compressed:
                    with io.TextIOWrapper(
                        compressed,
                        encoding="utf-8",
                        errors="strict",
                        newline="",
                    ) as text:
                        yield text, identity
    except CampaignError:
        raise
    except (OSError, EOFError, UnicodeError, zlib.error) as exc:
        raise CampaignError(f"cannot safely read gzip text {path}: {exc}") from exc


def _empty_counts() -> dict[str, int]:
    return {
        "input_feature_lines": 0,
        "output_feature_lines": 0,
        "comment_or_blank_lines": 0,
        "removed_gene_rows": 0,
        "removed_transcript_rows": 0,
    }


def _classify_line(line: str, source: Path, line_number: int) -> str | None:
    if not line or line.startswith("#") or line.isspace():
        return None
    columns = line.rstrip("\r\n").split("\t")
    if len(columns) < 3:
        raise CampaignError(f"{source}:{line_number}: expected at least three tab columns")
    return columns[2]


def _write_transform(
    source: Path, output_descriptor: int
) -> tuple[dict[str, int], dict[str, object]]:
    counts = _empty_counts()
    with _gzip_reader(source) as (source_text, source_identity):
        try:
            duplicate = os.dup(output_descriptor)
            try:
                raw_output = os.fdopen(duplicate, "wb")
            except BaseException:
                os.close(duplicate)
                raise
            with raw_output:
                with gzip.GzipFile(
                    filename="",
                    mode="wb",
                    fileobj=raw_output,
                    compresslevel=9,
                    mtime=0,
                ) as compressed:
                    with io.TextIOWrapper(
                        compressed,
                        encoding="utf-8",
                        errors="strict",
                        newline="",
                    ) as output_text:
                        for line_number, line in enumerate(source_text, 1):
                            featuretype = _classify_line(line, source, line_number)
                            if featuretype is None:
                                counts["comment_or_blank_lines"] += 1
                                output_text.write(line)
                                continue
                            counts["input_feature_lines"] += 1
                            if featuretype in REMOVED_FEATURETYPES:
                                counts[f"removed_{featuretype}_rows"] += 1
                                continue
                            counts["output_feature_lines"] += 1
                            output_text.write(line)
        except (OSError, UnicodeError) as exc:
            raise CampaignError(f"cannot stream gzip GTF transform: {exc}") from exc
    if counts["removed_gene_rows"] < 1 or counts["removed_transcript_rows"] < 1:
        raise CampaignError("controlled GTF must remove both gene and transcript rows")
    return counts, source_identity


def _replay_transform(
    source: Path, output: Path
) -> tuple[dict[str, int], dict[str, object], dict[str, object]]:
    counts = _empty_counts()
    deterministic = _DigestWriter()
    with gzip.GzipFile(
        filename="",
        mode="wb",
        fileobj=deterministic,
        compresslevel=9,
        mtime=0,
    ) as expected_compressed:
        with io.TextIOWrapper(
            expected_compressed,
            encoding="utf-8",
            errors="strict",
            newline="",
        ) as expected_output:
            with _gzip_reader(source) as (source_text, source_identity):
                with _gzip_reader(output, require_unique=True) as (
                    output_text,
                    output_identity,
                ):
                    output_iterator = iter(output_text)
                    for line_number, source_line in enumerate(source_text, 1):
                        featuretype = _classify_line(source_line, source, line_number)
                        if featuretype is None:
                            counts["comment_or_blank_lines"] += 1
                            retained = True
                        else:
                            counts["input_feature_lines"] += 1
                            retained = featuretype not in REMOVED_FEATURETYPES
                            if retained:
                                counts["output_feature_lines"] += 1
                            else:
                                counts[f"removed_{featuretype}_rows"] += 1
                        if not retained:
                            continue
                        expected_output.write(source_line)
                        try:
                            output_line = next(output_iterator)
                        except StopIteration:
                            raise CampaignError(
                                "parent-stripped output ended before the source replay"
                            ) from None
                        if output_line != source_line:
                            raise CampaignError(
                                "parent-stripped output differs from the retained source replay"
                            )
                    try:
                        extra = next(output_iterator)
                    except StopIteration:
                        extra = None
                    if extra is not None:
                        raise CampaignError(
                            "parent-stripped output has rows absent from source replay"
                        )
    if counts["removed_gene_rows"] < 1 or counts["removed_transcript_rows"] < 1:
        raise CampaignError("controlled GTF must remove both gene and transcript rows")
    if (
        output_identity["bytes"] != deterministic.bytes_written
        or output_identity["sha256"] != deterministic.hexdigest()
    ):
        raise CampaignError("parent-stripped output is not the deterministic gzip encoding")
    return counts, source_identity, output_identity


def _validate_file_identity(value: object, *, expected_path: Path, label: str) -> dict[str, object]:
    if not isinstance(value, Mapping) or set(value) != _FILE_IDENTITY_KEYS:
        raise CampaignError(f"{label} identity has an invalid closed shape")
    identity = dict(value)
    if type(identity["path"]) is not str or identity["path"] != str(expected_path):
        raise CampaignError(f"{label} path identity differs")
    if type(identity["bytes"]) is not int or identity["bytes"] < 1:
        raise CampaignError(f"{label} byte identity is invalid")
    if type(identity["sha256"]) is not str or _SHA256_RE.fullmatch(identity["sha256"]) is None:
        raise CampaignError(f"{label} SHA-256 identity is invalid")
    if identity["gzip_crc_ok"] is not True:
        raise CampaignError(f"{label} gzip CRC identity is invalid")
    return identity


def _validate_counts(value: object) -> dict[str, int]:
    if not isinstance(value, Mapping) or set(value) != _COUNT_KEYS:
        raise CampaignError("transform counts have an invalid closed shape")
    counts = dict(value)
    for key in _COUNT_KEYS:
        if type(counts[key]) is not int or counts[key] < 0:
            raise CampaignError(f"transform count {key} is invalid")
    if counts["removed_gene_rows"] < 1 or counts["removed_transcript_rows"] < 1:
        raise CampaignError("transform must remove both gene and transcript rows")
    if counts["input_feature_lines"] != (
        counts["output_feature_lines"]
        + counts["removed_gene_rows"]
        + counts["removed_transcript_rows"]
    ):
        raise CampaignError("transform feature counts do not balance")
    return counts


def _manifest_validator(value: object, *, source: Path, output: Path) -> dict[str, object]:
    if not isinstance(value, Mapping) or set(value) != _MANIFEST_KEYS:
        raise CampaignError("GTF transform manifest has an invalid closed shape")
    manifest = dict(value)
    if manifest["schema_version"] != MANIFEST_SCHEMA:
        raise CampaignError("GTF transform manifest has a stale schema")
    if manifest["transform"] != TRANSFORM_DESCRIPTION:
        raise CampaignError("GTF transform description differs")
    if manifest["transform_version"] != TRANSFORM_VERSION:
        raise CampaignError("GTF transform version differs")
    if not _valid_timestamp(manifest["generated_utc"]):
        raise CampaignError("GTF transform timestamp is invalid")
    manifest["source"] = _validate_file_identity(
        manifest["source"], expected_path=source, label="source"
    )
    manifest["output"] = _validate_file_identity(
        manifest["output"], expected_path=output, label="output"
    )
    manifest["counts"] = _validate_counts(manifest["counts"])
    return manifest


def _transform_lock_binding(
    source: Path, destination: Path, manifest_path: Path
) -> tuple[str, str]:
    binding_sha = sha256_json(
        {
            "source": str(source),
            "output": str(destination),
            "manifest": str(manifest_path),
            "transform_version": TRANSFORM_VERSION,
        }
    )
    return binding_sha, f"transform-{binding_sha[:16]}.lock"


def _validate_parent_stripped_gtf_locked(
    source: Path,
    destination: Path,
    manifest_path: Path,
    *,
    repair_durability: bool,
) -> dict[str, object]:
    """Validate a transform while the caller holds its digest-bound lock."""

    source, destination, manifest_path = _validate_transform_paths(
        source, destination, manifest_path
    )
    if _path_state(destination) is None or _path_state(manifest_path) is None:
        raise CampaignError("parent-stripped output and manifest must both exist")
    paths = (source, destination, manifest_path)
    before_versions = _transform_versions(paths)
    manifest = safe_io.strict_json_load(
        manifest_path,
        validator=lambda value: _manifest_validator(
            value,
            source=source,
            output=destination,
        ),
    )
    if not isinstance(manifest, dict):  # keeps static and dynamic callers closed
        raise CampaignError("GTF transform manifest validation returned a non-object")
    counts, source_identity, output_identity = _replay_transform(source, destination)
    if manifest["counts"] != counts:
        raise CampaignError("GTF transform counts differ from streaming replay")
    if manifest["source"] != source_identity:
        raise CampaignError("GTF transform source identity differs from current source")
    if manifest["output"] != output_identity:
        raise CampaignError("GTF transform output identity differs from current output")
    if repair_durability:
        safe_io.fsync_regular_file_durable(destination)
        safe_io.fsync_regular_file_durable(manifest_path)
    final_paths = _validate_transform_paths(source, destination, manifest_path)
    if final_paths != paths or _transform_versions(final_paths) != before_versions:
        raise CampaignError(
            "GTF transform source/output/manifest snapshot changed during validation"
        )
    return manifest


def validate_parent_stripped_gtf(
    source: Path, destination: Path, manifest_path: Path
) -> dict[str, object]:
    """Lock, durably repair, and independently replay an immutable transform pair."""

    source, destination, manifest_path = _validate_transform_paths(
        source, destination, manifest_path
    )
    binding_sha, lock_name = _transform_lock_binding(source, destination, manifest_path)
    with safe_io.campaign_lock(
        destination.parent,
        binding_sha,
        lock_name=lock_name,
    ):
        return _validate_parent_stripped_gtf_locked(
            source,
            destination,
            manifest_path,
            repair_durability=True,
        )


def _safe_unlink(path: Path, expected: os.stat_result) -> None:
    with safe_io._open_parent(path) as (parent_descriptor, name, parent, identity):
        item = safe_io._lstat_at(parent_descriptor, name)
        safe_io._require_unique_regular(item, f"transform artifact {path}")
        if item is None or (item.st_dev, item.st_ino) != (
            expected.st_dev,
            expected.st_ino,
        ):
            raise CampaignError(f"transform artifact identity changed before unlink: {path}")
        safe_io._assert_parent_identity(parent, parent_descriptor, identity)
        safe_io._unlink_if_identity(
            parent_descriptor,
            name,
            (expected.st_dev, expected.st_ino),
        )
        os.fsync(parent_descriptor)


def build_parent_stripped_gtf(
    source: Path,
    destination: Path,
    manifest_path: Path,
    *,
    force: bool = False,
    fault_hook: FaultHook | None = None,
    now_provider: Callable[[], str] = _utc_now,
) -> dict[str, object]:
    """Remove explicit parent rows and publish an immutable final manifest."""

    if type(force) is not bool:
        raise CampaignError("force must be a boolean")
    source, destination, manifest_path = _validate_transform_paths(
        source, destination, manifest_path
    )
    binding_sha, lock_name = _transform_lock_binding(source, destination, manifest_path)
    with safe_io.campaign_lock(
        destination.parent,
        binding_sha,
        lock_name=lock_name,
    ):
        source, destination, manifest_path = _validate_transform_paths(
            source, destination, manifest_path
        )
        output_state = _path_state(destination)
        manifest_state = _path_state(manifest_path)
        output_exists = output_state is not None
        manifest_exists = manifest_state is not None
        if output_exists and manifest_exists and not force:
            return _validate_parent_stripped_gtf_locked(
                source,
                destination,
                manifest_path,
                repair_durability=True,
            )
        if output_exists != manifest_exists and not force:
            raise CampaignError(
                "partial transform state: output and manifest must either both exist or both be absent"
            )
        if force:
            if manifest_exists:
                if manifest_state is None:  # narrows the guarded optional value
                    raise CampaignError("manifest identity disappeared before force removal")
                _fault(fault_hook, "before_force_manifest_remove")
                _safe_unlink(manifest_path, manifest_state)
                _fault(fault_hook, "after_force_manifest_remove")
            if output_exists:
                if output_state is None:  # narrows the guarded optional value
                    raise CampaignError("output identity disappeared before force removal")
                _fault(fault_hook, "before_force_output_remove")
                _safe_unlink(destination, output_state)
                _fault(fault_hook, "after_force_output_remove")
            output_exists = manifest_exists = False
        if output_exists or manifest_exists:
            raise CampaignError("conflicting transform artifacts already exist")

        with safe_io._open_parent(destination) as (
            parent_descriptor,
            destination_name,
            parent,
            parent_identity,
        ):
            flags = os.O_RDWR | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC
            for _ in range(16):
                temporary_name = f".gffbase-tmp-{secrets.token_hex(16)}"
                try:
                    descriptor = os.open(
                        temporary_name,
                        flags,
                        0o600,
                        dir_fd=parent_descriptor,
                    )
                except FileExistsError:
                    continue
                except OSError as exc:
                    raise CampaignError(
                        f"cannot create transform temporary for {destination}: {exc}"
                    ) from exc
                break
            else:
                raise CampaignError(
                    f"cannot allocate a unique transform temporary for {destination}"
                )
            created = safe_io._fstat_or_close(descriptor)
            temporary_identity = (created.st_dev, created.st_ino)
            committed = False
            try:
                os.fchmod(descriptor, 0o600)
                _fault(fault_hook, "after_temp_create")
                counts, source_identity = _write_transform(source, descriptor)
                _fault(fault_hook, "after_transform")
                os.fsync(descriptor)
                _fault(fault_hook, "after_output_file_fsync")
                safe_io._assert_parent_identity(
                    parent,
                    parent_descriptor,
                    parent_identity,
                )
                if safe_io._lstat_at(parent_descriptor, destination_name) is not None:
                    raise CampaignError(f"transform output raced into existence: {destination}")
                _fault(fault_hook, "before_output_commit")
                try:
                    safe_io._rename_noreplace_bound_at(
                        parent_descriptor,
                        temporary_name,
                        parent_descriptor,
                        destination_name,
                        os.fstat(descriptor),
                        f"transform output {destination}",
                    )
                except FileExistsError as exc:
                    raise CampaignError(
                        f"transform output raced into existence: {destination}"
                    ) from exc
                committed = True
                _fault(fault_hook, "after_output_commit")
                os.fsync(parent_descriptor)
                _fault(fault_hook, "after_output_parent_fsync")
            except BaseException:
                os.close(descriptor)
                if not committed:
                    with contextlib.suppress(FileNotFoundError):
                        safe_io._unlink_if_identity(
                            parent_descriptor,
                            temporary_name,
                            temporary_identity,
                        )
                        os.fsync(parent_descriptor)
                raise
            else:
                os.close(descriptor)

        replay_counts, replay_source, output_identity = _replay_transform(
            source,
            destination,
        )
        if replay_counts != counts or replay_source != source_identity:
            raise CampaignError("post-commit transform replay differs from the measured source")
        generated_utc = now_provider()
        if not _valid_timestamp(generated_utc):
            raise CampaignError("generated transform timestamp is invalid")
        manifest = {
            "schema_version": MANIFEST_SCHEMA,
            "transform": TRANSFORM_DESCRIPTION,
            "transform_version": TRANSFORM_VERSION,
            "generated_utc": generated_utc,
            "source": replay_source,
            "output": output_identity,
            "counts": replay_counts,
        }
        _fault(fault_hook, "before_manifest_create")

        def manifest_fault(stage: str) -> None:
            _fault(fault_hook, f"manifest_{stage}")

        safe_io.atomic_create_json(
            manifest_path,
            manifest,
            fault_hook=manifest_fault if fault_hook is not None else None,
        )
        _fault(fault_hook, "after_manifest_create")
        return _validate_parent_stripped_gtf_locked(
            source,
            destination,
            manifest_path,
            repair_durability=True,
        )


def main() -> None:
    canonical = corpus_path(BY_KEY["gencode-gtf"])
    parser = argparse.ArgumentParser()
    parser.add_argument("--source", type=Path, default=canonical)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--force", action="store_true")
    args = parser.parse_args()
    manifest = build_parent_stripped_gtf(args.source, args.output, args.manifest, force=args.force)
    counts = manifest["counts"]
    output_identity = manifest["output"]
    if not isinstance(counts, Mapping) or not isinstance(output_identity, Mapping):
        raise CampaignError("validated transform manifest has invalid nested objects")
    print(
        f"parent-stripped GTF: {counts['output_feature_lines']:,} rows; "
        f"sha256 {output_identity['sha256']}",
        flush=True,
    )


if __name__ == "__main__":
    main()
