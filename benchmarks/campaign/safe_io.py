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
"""No-follow, durable filesystem primitives for benchmark campaigns.

The campaign is expected to live on NFS and survive process, shell, and node
failures.  These helpers therefore avoid advisory locks, never follow a path
component symlink, use canonical immutable JSON, and fsync both files and
their containing directories.  They perform no work at import time.

The canonical stable parent of a protected path is the trust anchor.  The
helpers detect lasting path replacement and races below that anchor, but no
POSIX pathname protocol can detect an adversary with the same credentials who
replaces the stable parent (or one of its ancestors) and restores the original
binding between checks.  Campaign operators must therefore keep those parent
directories non-replaceable by untrusted same-UID processes.
"""

from __future__ import annotations

import contextlib
import ctypes
import errno
import hashlib
import json
import os
import re
import secrets
import socket
import stat
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import TypeVar

from .model import ROOT, CampaignError, canonical_json_bytes, validate_identifier

LOCK_SCHEMA = "campaign-lock-v2"
MAX_JSON_BYTES = 16 * (1 << 20) + 1
MAX_JSON_INTEGER_TOKEN_CHARS = 256
MAX_JSON_FLOAT_TOKEN_CHARS = 256

_PRIVATE_JSON_MODES = frozenset({0o600})
_PUBLIC_JSON_MODES = frozenset({0o400, 0o440, 0o444, 0o600, 0o640, 0o644, 0o660, 0o664})
_PUBLIC_JSON_WRITE_MODE = 0o644
_PUBLIC_DIRECTORY_MODES = frozenset(
    {0o700, 0o750, 0o755, 0o770, 0o775, 0o2700, 0o2750, 0o2755, 0o2770, 0o2775}
)

_SHA256_RE = re.compile(r"[0-9a-f]{64}\Z", re.ASCII)
_NONCE_RE = re.compile(r"[0-9a-f]{32}\Z", re.ASCII)
_HOST_RE = re.compile(r"[A-Za-z0-9](?:[A-Za-z0-9._-]{0,253}[A-Za-z0-9])?\Z", re.ASCII)
_BOOT_ID_RE = re.compile(
    r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\Z",
    re.ASCII,
)
_TIMESTAMP_RE = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z\Z", re.ASCII)
_OWNER_KEYS = {
    "schema_version",
    "campaign_sha256",
    "host",
    "pid",
    "pgid",
    "process_start_ticks",
    "boot_id",
    "started_utc",
    "lock_nonce",
}
_IDENTITY_KEYS = {
    "host",
    "pid",
    "pgid",
    "process_start_ticks",
    "boot_id",
}
_STABLE_FILE_FIELDS = ("st_dev", "st_ino", "st_size", "st_mtime_ns", "st_ctime_ns")
LOCK_ROOT_NAME = ".gffbase-locks"
RETIRE_ROOT_NAME = ".gffbase-retired"
_RENAME_NOREPLACE = 1
_RENAME_EXCHANGE = 2

_T = TypeVar("_T")
FaultHook = Callable[[str], None]
Validator = Callable[[object], _T]


def internal_path_limit_components() -> tuple[str, ...]:
    """Return fixed worst-case internal components for byte-limit probes."""

    return (
        LOCK_ROOT_NAME,
        RETIRE_ROOT_NAME,
        f".gffbase-tmp-{'0' * 32}",
        f".gffbase-active-lock-{'0' * 64}",
        "0" * 64,
        f"released-{'0' * 32}",
        f"retired-{'0' * 32}",
        "owner.json",
        "entry",
    )


def _fault(hook: FaultHook | None, stage: str) -> None:
    if hook is not None:
        hook(stage)


def _require_posix_nofollow() -> None:
    required = ("O_CLOEXEC", "O_DIRECTORY", "O_NOFOLLOW")
    missing = [name for name in required if not hasattr(os, name)]
    if missing:
        raise CampaignError(f"safe campaign I/O requires POSIX flags: {', '.join(missing)}")


def _raw_path(value: os.PathLike[str] | str, label: str = "path") -> str:
    try:
        raw = os.fspath(value)
    except TypeError as exc:
        raise CampaignError(f"{label} must be a filesystem path") from exc
    if type(raw) is not str or not raw:
        raise CampaignError(f"{label} must be a non-empty text path")
    if "\x00" in raw or any(ord(char) < 32 or ord(char) == 127 for char in raw):
        raise CampaignError(f"{label} contains a control character")
    if ".." in Path(raw).parts:
        raise CampaignError(f"{label} contains a '..' component")
    return raw


def lexical_absolute(value: os.PathLike[str] | str, label: str = "path") -> Path:
    """Return an absolute lexical path without resolving filesystem aliases."""

    raw = _raw_path(value, label)
    candidate = Path(raw)
    if not candidate.is_absolute():
        candidate = Path.cwd() / candidate
    # normpath removes harmless repeated separators and '.' only after '..'
    # was rejected above.  It does not inspect or follow the filesystem.
    normalized = Path(os.path.normpath(os.fspath(candidate)))
    if not normalized.is_absolute():  # defensive for unusual platforms
        raise CampaignError(f"{label} did not normalize to an absolute path")
    return normalized


def _path_parts(path: Path) -> tuple[str, ...]:
    if not path.is_absolute() or path.anchor != os.sep:
        raise CampaignError(f"safe campaign I/O requires an absolute POSIX path: {path}")
    return tuple(part for part in path.parts if part != os.sep)


def inspect_path(
    value: os.PathLike[str] | str,
    *,
    allow_missing_leaf: bool = False,
    allow_missing_tail: bool = False,
    require_kind: str | None = None,
    require_unique_file: bool = False,
) -> Path:
    """Component-wise ``lstat`` inspection without following aliases."""

    path = lexical_absolute(value)
    current = Path(os.sep)
    parts = _path_parts(path)
    missing = False
    leaf_stat: os.stat_result | None = os.lstat(current)
    for index, part in enumerate(parts):
        current /= part
        is_leaf = index == len(parts) - 1
        if missing:
            continue
        try:
            item = os.lstat(current)
        except FileNotFoundError:
            if allow_missing_tail or (allow_missing_leaf and is_leaf):
                missing = True
                leaf_stat = None
                continue
            raise CampaignError(f"missing path component: {current}") from None
        except OSError as exc:
            raise CampaignError(f"cannot inspect path component {current}: {exc}") from exc
        if stat.S_ISLNK(item.st_mode):
            raise CampaignError(f"symlink path component is not allowed: {current}")
        if not is_leaf and not stat.S_ISDIR(item.st_mode):
            raise CampaignError(f"non-directory path ancestor is not allowed: {current}")
        leaf_stat = item

    if leaf_stat is not None and require_kind is not None:
        if require_kind == "file" and not stat.S_ISREG(leaf_stat.st_mode):
            raise CampaignError(f"expected a regular file: {path}")
        if require_kind == "directory" and not stat.S_ISDIR(leaf_stat.st_mode):
            raise CampaignError(f"expected a directory: {path}")
        if require_kind not in {"file", "directory"}:
            raise CampaignError(f"unknown required path kind: {require_kind!r}")
    if leaf_stat is not None and require_unique_file:
        if not stat.S_ISREG(leaf_stat.st_mode) or leaf_stat.st_nlink != 1:
            raise CampaignError(f"expected a uniquely linked regular file: {path}")
    return path


def _is_beneath(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
    except ValueError:
        return False
    return True


def _overlaps(left: Path, right: Path) -> bool:
    return _is_beneath(left, right) or _is_beneath(right, left)


def resolve_beneath(
    root: os.PathLike[str] | str,
    candidate: os.PathLike[str] | str,
    *,
    must_exist: bool = False,
    allow_missing_tail: bool = False,
) -> Path:
    """Validate a lexical child without resolving aliases."""

    root_path = lexical_absolute(root, "root")
    raw_candidate = _raw_path(candidate, "candidate path")
    candidate_path = Path(raw_candidate)
    if not candidate_path.is_absolute():
        candidate_path = root_path / candidate_path
    candidate_path = lexical_absolute(candidate_path, "candidate path")
    if not _is_beneath(candidate_path, root_path):
        raise CampaignError(f"candidate path escapes root {root_path}: {candidate_path}")
    inspect_path(root_path, allow_missing_tail=allow_missing_tail and not must_exist)
    inspect_path(
        candidate_path,
        allow_missing_tail=allow_missing_tail and not must_exist,
        allow_missing_leaf=not must_exist,
    )
    return candidate_path


def _dev_ino(path: Path) -> tuple[int, int] | None:
    try:
        item = os.lstat(path)
    except FileNotFoundError:
        return None
    if stat.S_ISLNK(item.st_mode):
        raise CampaignError(f"symlink path component is not allowed: {path}")
    return item.st_dev, item.st_ino


def _existing_ancestors(path: Path) -> Iterator[Path]:
    current = path
    while True:
        if _dev_ino(current) is not None:
            yield current
        if current.parent == current:
            break
        current = current.parent


def validate_campaign_root(
    root: os.PathLike[str] | str, *, repo_root: os.PathLike[str] | str = ROOT
) -> Path:
    """Reject broad/protected roots while allowing the intended output tree."""

    candidate = lexical_absolute(root, "campaign root")
    repo = lexical_absolute(repo_root, "repository root")
    home = lexical_absolute(Path.home(), "home directory")
    data = repo / "benchmarks" / "data"
    results = repo / "benchmarks" / "results"
    inspect_path(candidate, allow_missing_tail=True, require_kind="directory")

    if candidate == Path(os.sep) or candidate in {home, repo}:
        raise CampaignError(f"unsafe campaign root: {candidate}")
    if _is_beneath(home, candidate) or _is_beneath(repo, candidate):
        raise CampaignError(f"campaign root is an unsafe ancestor: {candidate}")
    if _overlaps(candidate, data) or _overlaps(candidate, results):
        raise CampaignError(f"campaign root overlaps protected benchmark data: {candidate}")

    protected_exact = {
        identity for path in (Path(os.sep), home, repo) if (identity := _dev_ino(path)) is not None
    }
    protected_ancestor_aliases: dict[tuple[int, int], set[Path]] = {}
    for protected_path in (home, repo):
        identity = _dev_ino(protected_path)
        if identity is not None:
            protected_ancestor_aliases.setdefault(identity, set()).add(protected_path)
    protected_subtrees = {
        identity for path in (data, results) if (identity := _dev_ino(path)) is not None
    }
    candidate_identity = _dev_ino(candidate)
    if candidate_identity is not None and candidate_identity in protected_exact:
        raise CampaignError(f"campaign root aliases a protected root: {candidate}")
    for ancestor in _existing_ancestors(candidate):
        identity = _dev_ino(ancestor)
        if (
            identity in protected_ancestor_aliases
            and ancestor not in protected_ancestor_aliases[identity]
        ):
            raise CampaignError(
                f"campaign root descends from an alias of a protected root: {candidate}"
            )
        if identity in protected_subtrees:
            raise CampaignError(f"campaign root aliases protected benchmark data: {candidate}")
    return candidate


def job_directory(campaign_dir: os.PathLike[str] | str, job_id: str) -> Path:
    root = lexical_absolute(campaign_dir, "campaign directory")
    validate_identifier(job_id, "job")
    return root / "jobs" / job_id


def attempt_directory(campaign_dir: os.PathLike[str] | str, job_id: str, attempt: int) -> Path:
    if type(attempt) is not int or not 1 <= attempt <= 9999:
        raise CampaignError("attempt must be an integer from 1 through 9999")
    return job_directory(campaign_dir, job_id) / "attempts" / f"{attempt:04d}"


def require_exact_derived_path(
    expected: os.PathLike[str] | str,
    claimed: os.PathLike[str] | str,
    *,
    allow_missing_tail: bool = False,
) -> Path:
    expected_path = lexical_absolute(expected, "expected derived path")
    claimed_raw = _raw_path(claimed, "claimed derived path")
    claimed_path = lexical_absolute(claimed_raw, "claimed derived path")
    if not Path(claimed_raw).is_absolute() or claimed_raw != str(expected_path):
        raise CampaignError(
            "claimed path does not use the exact canonical derived spelling: "
            f"{claimed_raw!r} != {str(expected_path)!r}"
        )
    inspect_path(claimed_path, allow_missing_tail=allow_missing_tail)
    return claimed_path


@contextmanager
def _open_directory(path: Path) -> Iterator[int]:
    _require_posix_nofollow()
    path = inspect_path(path, require_kind="directory")
    inspected = os.lstat(path)
    expected_identity = (inspected.st_dev, inspected.st_ino)
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
    descriptor = -1
    try:
        descriptor = os.open(os.sep, flags)
        for component in _path_parts(path):
            next_descriptor = os.open(component, flags, dir_fd=descriptor)
            try:
                item = os.fstat(next_descriptor)
                if not stat.S_ISDIR(item.st_mode):
                    raise CampaignError(f"path component is not a directory: {component}")
                os.close(descriptor)
            except BaseException:
                _close_descriptor(next_descriptor)
                raise
            descriptor = next_descriptor
        opened = os.fstat(descriptor)
        if (opened.st_dev, opened.st_ino) != expected_identity:
            raise CampaignError(f"directory identity changed while it was opened: {path}")
    except OSError as exc:
        if descriptor >= 0:
            with contextlib.suppress(OSError):
                os.close(descriptor)
        raise CampaignError(f"cannot safely open directory {path}: {exc}") from exc
    except BaseException:
        if descriptor >= 0:
            with contextlib.suppress(OSError):
                os.close(descriptor)
        raise
    try:
        yield descriptor
    finally:
        with contextlib.suppress(OSError):
            os.close(descriptor)


@contextmanager
def _open_parent(path: Path) -> Iterator[tuple[int, str, Path, tuple[int, int]]]:
    path = lexical_absolute(path)
    if path == Path(os.sep):
        raise CampaignError("a filesystem root cannot be used as a file")
    parent = path.parent
    with _open_directory(parent) as descriptor:
        item = os.fstat(descriptor)
        yield descriptor, path.name, parent, (item.st_dev, item.st_ino)


def _assert_parent_identity(parent: Path, descriptor: int, expected: tuple[int, int]) -> None:
    inspected = inspect_path(parent, require_kind="directory")
    current = os.lstat(inspected)
    opened = os.fstat(descriptor)
    actual = (opened.st_dev, opened.st_ino)
    if actual != expected or (current.st_dev, current.st_ino) != expected:
        raise CampaignError(f"parent directory identity changed during mutation: {parent}")


def _lstat_at(descriptor: int, name: str) -> os.stat_result | None:
    try:
        return os.stat(name, dir_fd=descriptor, follow_symlinks=False)
    except FileNotFoundError:
        return None


def _require_unique_regular(
    item: os.stat_result | None,
    label: str,
    *,
    allowed_modes: frozenset[int] = _PRIVATE_JSON_MODES,
) -> os.stat_result:
    if item is None or not stat.S_ISREG(item.st_mode) or item.st_nlink != 1:
        raise CampaignError(f"{label} is not a uniquely linked regular file")
    mode = stat.S_IMODE(item.st_mode)
    if mode not in allowed_modes:
        expectation = "0600" if allowed_modes == _PRIVATE_JSON_MODES else "safe public JSON"
        raise CampaignError(f"{label} has unsafe mode or permissions; expected {expectation}")
    return item


def _write_all(descriptor: int, data: bytes) -> None:
    view = memoryview(data)
    written = 0
    while written < len(view):
        try:
            count = os.write(descriptor, view[written:])
        except InterruptedError:
            continue
        if count < 1:
            raise CampaignError("filesystem write made no progress")
        written += count


def _close_descriptor(descriptor: int) -> None:
    if descriptor >= 0:
        with contextlib.suppress(OSError):
            os.close(descriptor)


def _fstat_or_close(descriptor: int) -> os.stat_result:
    try:
        return os.fstat(descriptor)
    except BaseException:
        _close_descriptor(descriptor)
        raise


def _rename_noreplace_at(
    source_descriptor: int,
    source_name: str,
    destination_descriptor: int,
    destination_name: str,
) -> None:
    """Atomically rename without replacement, failing closed if unsupported."""

    try:
        renameat2 = ctypes.CDLL(None, use_errno=True).renameat2
    except AttributeError as exc:  # pragma: no cover - Linux CI/cluster contract
        raise CampaignError("safe campaign publication requires Linux renameat2") from exc
    renameat2.argtypes = (
        ctypes.c_int,
        ctypes.c_char_p,
        ctypes.c_int,
        ctypes.c_char_p,
        ctypes.c_uint,
    )
    renameat2.restype = ctypes.c_int
    ctypes.set_errno(0)
    result = renameat2(
        source_descriptor,
        os.fsencode(source_name),
        destination_descriptor,
        os.fsencode(destination_name),
        _RENAME_NOREPLACE,
    )
    if result == 0:
        return
    error = ctypes.get_errno()
    if error in {errno.EEXIST, errno.ENOTEMPTY}:
        raise FileExistsError(error, os.strerror(error), destination_name)
    if error in {errno.EINVAL, errno.ENOSYS, errno.ENOTSUP, errno.EOPNOTSUPP}:
        raise CampaignError(
            "filesystem does not support atomic no-replace rename required for safe campaign I/O"
        )
    raise OSError(error, os.strerror(error), destination_name)


def _rename_exchange_at(
    source_descriptor: int,
    source_name: str,
    destination_descriptor: int,
    destination_name: str,
) -> None:
    """Atomically exchange two names, failing closed if unsupported."""

    try:
        renameat2 = ctypes.CDLL(None, use_errno=True).renameat2
    except AttributeError as exc:  # pragma: no cover - Linux CI/cluster contract
        raise CampaignError("safe campaign replacement requires Linux renameat2") from exc
    renameat2.argtypes = (
        ctypes.c_int,
        ctypes.c_char_p,
        ctypes.c_int,
        ctypes.c_char_p,
        ctypes.c_uint,
    )
    renameat2.restype = ctypes.c_int
    ctypes.set_errno(0)
    result = renameat2(
        source_descriptor,
        os.fsencode(source_name),
        destination_descriptor,
        os.fsencode(destination_name),
        _RENAME_EXCHANGE,
    )
    if result == 0:
        return
    error = ctypes.get_errno()
    if error in {errno.EINVAL, errno.ENOSYS, errno.ENOTSUP, errno.EOPNOTSUPP}:
        raise CampaignError(
            "filesystem does not support atomic exchange rename required for safe campaign I/O"
        )
    raise OSError(error, os.strerror(error), destination_name)


def _rename_noreplace_bound_at(
    source_descriptor: int,
    source_name: str,
    destination_descriptor: int,
    destination_name: str,
    expected: os.stat_result,
    label: str,
    *,
    allowed_modes: frozenset[int] = _PRIVATE_JSON_MODES,
) -> os.stat_result:
    """Publish only the expected source version or roll an observed swap back."""

    _require_entry_identity(
        source_descriptor,
        source_name,
        expected,
        f"{label} source",
        compare_version=True,
        allowed_modes=allowed_modes,
    )
    _rename_noreplace_at(
        source_descriptor,
        source_name,
        destination_descriptor,
        destination_name,
    )
    moved = _lstat_at(destination_descriptor, destination_name)
    # A successful rename may advance ctime even though it preserves the inode
    # and bytes.  Bind the inode plus every other stable/type/link field, then
    # use the post-rename stat as the publication version fence.
    fields = ("st_dev", "st_ino", "st_size", "st_mtime_ns", "st_mode", "st_nlink")
    if moved is not None and not any(
        getattr(moved, field) != getattr(expected, field) for field in fields
    ):
        return _require_unique_regular(moved, label, allowed_modes=allowed_modes)

    source_after = _lstat_at(source_descriptor, source_name)
    if moved is not None and source_after is None:
        try:
            _rename_noreplace_at(
                destination_descriptor,
                destination_name,
                source_descriptor,
                source_name,
            )
        except (FileExistsError, OSError, CampaignError) as exc:
            raise CampaignError(
                f"{label} source identity changed and publication rollback failed"
            ) from exc
        restored = _lstat_at(source_descriptor, source_name)
        if (
            _lstat_at(destination_descriptor, destination_name) is not None
            or restored is None
            or (restored.st_dev, restored.st_ino) != (moved.st_dev, moved.st_ino)
        ):
            raise CampaignError(f"{label} source identity changed and rollback could not be proved")
        source_parent_before = os.fstat(source_descriptor)
        destination_parent_before = os.fstat(destination_descriptor)
        os.fsync(source_descriptor)
        if (source_parent_before.st_dev, source_parent_before.st_ino) != (
            destination_parent_before.st_dev,
            destination_parent_before.st_ino,
        ):
            os.fsync(destination_descriptor)
        source_parent_after = os.fstat(source_descriptor)
        destination_parent_after = os.fstat(destination_descriptor)
        restored_after = _lstat_at(source_descriptor, source_name)
        if (
            (source_parent_after.st_dev, source_parent_after.st_ino)
            != (source_parent_before.st_dev, source_parent_before.st_ino)
            or (destination_parent_after.st_dev, destination_parent_after.st_ino)
            != (destination_parent_before.st_dev, destination_parent_before.st_ino)
            or _lstat_at(destination_descriptor, destination_name) is not None
            or restored_after is None
            or (restored_after.st_dev, restored_after.st_ino) != (moved.st_dev, moved.st_ino)
        ):
            raise CampaignError(
                f"{label} source identity changed and durable rollback could not be proved"
            )
    raise CampaignError(f"{label} source identity changed at atomic publication")


def _read_file_at(
    parent_descriptor: int,
    name: str,
    *,
    max_bytes: int,
    require_unique: bool = True,
    allowed_modes: frozenset[int] = _PRIVATE_JSON_MODES,
) -> tuple[bytes, os.stat_result]:
    _require_posix_nofollow()
    before = _lstat_at(parent_descriptor, name)
    if before is None or stat.S_ISLNK(before.st_mode) or not stat.S_ISREG(before.st_mode):
        raise CampaignError(f"missing or unsafe regular file: {name}")
    if require_unique and before.st_nlink != 1:
        raise CampaignError(f"regular file is not uniquely linked: {name}")
    if stat.S_IMODE(before.st_mode) not in allowed_modes:
        raise CampaignError(f"JSON file has unsafe mode or permissions: {name}")
    if before.st_size > max_bytes:
        raise CampaignError(f"file exceeds the {max_bytes}-byte size bound: {name}")
    flags = os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC
    try:
        descriptor = os.open(name, flags, dir_fd=parent_descriptor)
    except OSError as exc:
        raise CampaignError(f"cannot safely open regular file {name}: {exc}") from exc
    try:
        opened = os.fstat(descriptor)
        if not stat.S_ISREG(opened.st_mode):
            raise CampaignError(f"opened file is not regular: {name}")
        if require_unique and opened.st_nlink != 1:
            raise CampaignError(f"opened regular file is not uniquely linked: {name}")
        if stat.S_IMODE(opened.st_mode) not in allowed_modes:
            raise CampaignError(f"opened JSON file has unsafe mode or permissions: {name}")
        if any(getattr(opened, field) != getattr(before, field) for field in _STABLE_FILE_FIELDS):
            raise CampaignError(f"file identity changed while opening: {name}")
        chunks: list[bytes] = []
        total = 0
        while True:
            chunk = os.read(descriptor, min(1 << 20, max_bytes + 1 - total))
            if not chunk:
                break
            chunks.append(chunk)
            total += len(chunk)
            if total > max_bytes:
                raise CampaignError(f"file exceeds the {max_bytes}-byte size bound: {name}")
        after = os.fstat(descriptor)
        if not stat.S_ISREG(after.st_mode) or (require_unique and after.st_nlink != 1):
            raise CampaignError(f"file link/type changed while it was read: {name}")
        if stat.S_IMODE(after.st_mode) not in allowed_modes:
            raise CampaignError(f"JSON file mode changed while it was read: {name}")
        if any(getattr(opened, field) != getattr(after, field) for field in _STABLE_FILE_FIELDS):
            raise CampaignError(f"file changed while it was read: {name}")
        return b"".join(chunks), after
    finally:
        os.close(descriptor)


class _DuplicateKey(ValueError):
    pass


def _closed_pairs(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise _DuplicateKey(f"duplicate JSON object key: {key!r}")
        result[key] = value
    return result


def _invalid_constant(value: str) -> object:
    raise ValueError(f"invalid JSON constant: {value}")


def _bounded_json_integer(value: str) -> int:
    if len(value) > MAX_JSON_INTEGER_TOKEN_CHARS:
        raise ValueError(f"JSON integer token exceeds {MAX_JSON_INTEGER_TOKEN_CHARS} characters")
    return int(value)


def _bounded_json_float(value: str) -> float:
    if len(value) > MAX_JSON_FLOAT_TOKEN_CHARS:
        raise ValueError(f"JSON float token exceeds {MAX_JSON_FLOAT_TOKEN_CHARS} characters")
    return float(value)


def canonical_storage_bytes(value: object) -> bytes:
    return canonical_json_bytes(value) + b"\n"


def _strict_json_load(
    path: os.PathLike[str] | str,
    *,
    validator: Validator[_T] | None = None,
    max_bytes: int = MAX_JSON_BYTES,
    allowed_modes: frozenset[int],
) -> _T | object:
    """Load exact canonical JSON through a no-follow, bounded descriptor."""

    if type(max_bytes) is not int or max_bytes < 1 or max_bytes > MAX_JSON_BYTES:
        raise CampaignError(f"invalid JSON size bound: {max_bytes!r}")
    target = lexical_absolute(path)
    inspect_path(target, require_kind="file", require_unique_file=True)
    inspected_item = os.lstat(target)
    with _open_parent(target) as (
        parent_descriptor,
        name,
        parent,
        parent_identity,
    ):
        raw, read_item = _read_file_at(
            parent_descriptor,
            name,
            max_bytes=max_bytes,
            allowed_modes=allowed_modes,
        )
        if any(
            getattr(read_item, field) != getattr(inspected_item, field)
            for field in _STABLE_FILE_FIELDS
        ):
            raise CampaignError(f"JSON artifact changed before it was opened: {target}")
    if raw.startswith(b"\xef\xbb\xbf"):
        raise CampaignError(f"JSON artifact has a forbidden UTF-8 BOM: {target}")
    try:
        text = raw.decode("utf-8", errors="strict")
        value = json.loads(
            text,
            object_pairs_hook=_closed_pairs,
            parse_constant=_invalid_constant,
            parse_int=_bounded_json_integer,
            parse_float=_bounded_json_float,
        )
    except (
        _DuplicateKey,
        UnicodeError,
        json.JSONDecodeError,
        ValueError,
        RecursionError,
    ) as exc:
        raise CampaignError(f"invalid JSON artifact {target}: {exc}") from exc
    try:
        expected = canonical_storage_bytes(value)
    except CampaignError as exc:
        raise CampaignError(f"invalid canonical JSON artifact {target}: {exc}") from exc
    if raw != expected:
        raise CampaignError(f"JSON artifact is not in canonical storage form: {target}")
    validated = validator(value) if validator is not None else value
    with _open_parent(target) as (
        final_parent_descriptor,
        final_name,
        _final_parent,
        final_parent_identity,
    ):
        if final_parent_identity != parent_identity:
            raise CampaignError(f"JSON artifact parent changed while it was validated: {parent}")
        _require_entry_identity(
            final_parent_descriptor,
            final_name,
            read_item,
            f"JSON artifact {target}",
            compare_version=True,
            allowed_modes=allowed_modes,
        )
        _assert_parent_identity(parent, final_parent_descriptor, parent_identity)
    return validated


def strict_json_load(
    path: os.PathLike[str] | str,
    *,
    validator: Validator[_T] | None = None,
    max_bytes: int = MAX_JSON_BYTES,
) -> _T | object:
    """Load exact canonical private JSON through a no-follow descriptor."""

    return _strict_json_load(
        path,
        validator=validator,
        max_bytes=max_bytes,
        allowed_modes=_PRIVATE_JSON_MODES,
    )


def strict_public_json_load(
    path: os.PathLike[str] | str,
    *,
    validator: Validator[_T] | None = None,
    max_bytes: int = MAX_JSON_BYTES,
) -> _T | object:
    """Load canonical Git-tracked JSON while rejecting unsafe public modes."""

    return _strict_json_load(
        path,
        validator=validator,
        max_bytes=max_bytes,
        allowed_modes=_PUBLIC_JSON_MODES,
    )


def sha256_regular_file(
    path: os.PathLike[str] | str,
    *,
    require_unique: bool = False,
) -> str:
    """Stream a stable no-follow regular file into SHA-256."""

    _require_posix_nofollow()
    target = lexical_absolute(path)
    inspect_path(target, require_kind="file")
    inspected_item = os.lstat(target)
    with _open_parent(target) as (parent_descriptor, name, parent, parent_identity):
        before = _lstat_at(parent_descriptor, name)
        if before is None or not stat.S_ISREG(before.st_mode):
            raise CampaignError(f"missing or unsafe regular file: {target}")
        if require_unique and before.st_nlink != 1:
            raise CampaignError(f"regular file is not uniquely linked: {target}")
        if any(
            getattr(before, field) != getattr(inspected_item, field)
            for field in _STABLE_FILE_FIELDS
        ):
            raise CampaignError(f"file changed before it was opened: {target}")
        flags = os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC
        descriptor = os.open(name, flags, dir_fd=parent_descriptor)
        try:
            opened = os.fstat(descriptor)
            if not stat.S_ISREG(opened.st_mode):
                raise CampaignError(f"opened file is not regular: {target}")
            if require_unique and opened.st_nlink != 1:
                raise CampaignError(f"opened regular file is not uniquely linked: {target}")
            if any(
                getattr(opened, field) != getattr(before, field) for field in _STABLE_FILE_FIELDS
            ):
                raise CampaignError(f"file identity changed while opening: {target}")
            digest = hashlib.sha256()
            while True:
                chunk = os.read(descriptor, 1 << 20)
                if not chunk:
                    break
                digest.update(chunk)
            after = os.fstat(descriptor)
            if not stat.S_ISREG(after.st_mode) or (require_unique and after.st_nlink != 1):
                raise CampaignError(f"file link/type changed while it was hashed: {target}")
            if any(
                getattr(opened, field) != getattr(after, field) for field in _STABLE_FILE_FIELDS
            ):
                raise CampaignError(f"file changed while it was hashed: {target}")
            _assert_parent_identity(parent, parent_descriptor, parent_identity)
            current = _lstat_at(parent_descriptor, name)
            if current is None or any(
                getattr(current, field) != getattr(after, field) for field in _STABLE_FILE_FIELDS
            ):
                raise CampaignError(f"file path changed while it was hashed: {target}")
            _assert_parent_identity(parent, parent_descriptor, parent_identity)
            return digest.hexdigest()
        finally:
            os.close(descriptor)


def fsync_regular_file_durable(path: os.PathLike[str] | str) -> os.stat_result:
    """Synchronize one immutable mode-0600 file and its exact lexical parent."""

    target = lexical_absolute(path)
    inspect_path(target, require_kind="file", require_unique_file=True)
    inspected = os.lstat(target)
    with _open_parent(target) as (parent_descriptor, name, parent, parent_identity):
        current = _require_unique_regular(
            _lstat_at(parent_descriptor, name), f"durable artifact {target}"
        )
        if any(
            getattr(current, field) != getattr(inspected, field) for field in _STABLE_FILE_FIELDS
        ):
            raise CampaignError(f"durable artifact changed before synchronization: {target}")
        _fsync_existing_regular(
            parent_descriptor,
            name,
            current,
            f"durable artifact {target}",
        )
        _assert_parent_identity(parent, parent_descriptor, parent_identity)
        _require_entry_identity(
            parent_descriptor,
            name,
            current,
            f"durable artifact {target}",
            compare_version=True,
        )
        _assert_parent_identity(parent, parent_descriptor, parent_identity)
        os.fsync(parent_descriptor)
        _assert_parent_identity(parent, parent_descriptor, parent_identity)
        final = _require_entry_identity(
            parent_descriptor,
            name,
            current,
            f"durable artifact {target}",
            compare_version=True,
        )
        _assert_parent_identity(parent, parent_descriptor, parent_identity)
        return final


def _unlink_if_identity(
    parent_descriptor: int,
    name: str,
    identity: tuple[int, int],
    *,
    allowed_modes: frozenset[int] = _PRIVATE_JSON_MODES,
) -> None:
    """Retire an exact inode into a private retained tombstone without deleting it."""

    candidate_item = _lstat_at(parent_descriptor, name)
    if candidate_item is None:
        return
    current = _require_unique_regular(
        candidate_item,
        f"retired artifact {name}",
        allowed_modes=allowed_modes,
    )
    if (current.st_dev, current.st_ino) != identity:
        raise CampaignError(f"refusing to retire a replaced temporary artifact: {name}")

    file_flags = os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC
    try:
        file_descriptor = os.open(name, file_flags, dir_fd=parent_descriptor)
    except OSError as exc:
        raise CampaignError(f"cannot safely open artifact for retirement {name}: {exc}") from exc
    opened_file = _fstat_or_close(file_descriptor)
    if (
        not stat.S_ISREG(opened_file.st_mode)
        or opened_file.st_nlink != 1
        or (opened_file.st_dev, opened_file.st_ino) != identity
        or stat.S_IMODE(opened_file.st_mode) not in allowed_modes
    ):
        _close_descriptor(file_descriptor)
        raise CampaignError(f"refusing to retire a replaced temporary artifact: {name}")

    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
    retirement_root_descriptor = -1
    quarantine_descriptor = -1
    try:
        retirement_root_descriptor, retirement_root_identity = _open_or_create_private_directory_at(
            parent_descriptor,
            RETIRE_ROOT_NAME,
            "artifact retirement root",
        )
        quarantine_name = ""
        for _ in range(16):
            candidate = f"retired-{secrets.token_hex(16)}"
            try:
                os.mkdir(candidate, mode=0o700, dir_fd=retirement_root_descriptor)
            except FileExistsError:
                continue
            except OSError as exc:
                raise CampaignError(
                    f"cannot allocate a deletion quarantine for {name}: {exc}"
                ) from exc
            quarantine_name = candidate
            try:
                quarantine_descriptor = os.open(
                    candidate,
                    flags,
                    dir_fd=retirement_root_descriptor,
                )
                opened = os.fstat(quarantine_descriptor)
            except BaseException:
                _close_descriptor(quarantine_descriptor)
                quarantine_descriptor = -1
                raise
            if not stat.S_ISDIR(opened.st_mode) or stat.S_IMODE(opened.st_mode) != 0o700:
                raise CampaignError(f"deletion quarantine is not private: {quarantine_name}")
            os.fsync(retirement_root_descriptor)
            break
        else:
            raise CampaignError(f"cannot allocate a deletion quarantine for {name}")

        entry_name = "entry"
        _rename_noreplace_at(
            parent_descriptor,
            name,
            quarantine_descriptor,
            entry_name,
        )
        moved = _lstat_at(quarantine_descriptor, entry_name)
        if moved is None or (moved.st_dev, moved.st_ino) != identity:
            if moved is not None and _lstat_at(parent_descriptor, name) is None:
                with contextlib.suppress(FileExistsError, OSError, CampaignError):
                    _rename_noreplace_at(
                        quarantine_descriptor,
                        entry_name,
                        parent_descriptor,
                        name,
                    )
            raise CampaignError(f"refusing to retire a replaced temporary artifact: {name}")
        _require_entry_identity(
            quarantine_descriptor,
            entry_name,
            identity,
            f"retained retirement tombstone for {name}",
            allowed_modes=allowed_modes,
        )
        final_opened = os.fstat(file_descriptor)
        if (final_opened.st_dev, final_opened.st_ino) != identity or stat.S_IMODE(
            final_opened.st_mode
        ) not in allowed_modes:
            raise CampaignError(f"retired artifact descriptor changed: {name}")
        os.fsync(quarantine_descriptor)
        os.fsync(parent_descriptor)
        _require_directory_entry_identity(
            parent_descriptor,
            RETIRE_ROOT_NAME,
            retirement_root_identity,
            "artifact retirement root",
        )
    finally:
        _close_descriptor(quarantine_descriptor)
        _close_descriptor(retirement_root_descriptor)
        _close_descriptor(file_descriptor)


def _require_directory_entry_identity(
    parent_descriptor: int,
    name: str,
    expected: tuple[int, int],
    label: str,
) -> os.stat_result:
    current = _lstat_at(parent_descriptor, name)
    if (
        current is None
        or not stat.S_ISDIR(current.st_mode)
        or (current.st_dev, current.st_ino) != expected
    ):
        raise CampaignError(f"{label} was replaced or removed")
    return current


def _require_entry_identity(
    parent_descriptor: int,
    name: str,
    expected: os.stat_result | tuple[int, int],
    label: str,
    *,
    compare_version: bool = False,
    allowed_modes: frozenset[int] = _PRIVATE_JSON_MODES,
) -> os.stat_result:
    current = _require_unique_regular(
        _lstat_at(parent_descriptor, name),
        label,
        allowed_modes=allowed_modes,
    )
    expected_identity = (
        (expected.st_dev, expected.st_ino) if isinstance(expected, os.stat_result) else expected
    )
    if (current.st_dev, current.st_ino) != expected_identity:
        raise CampaignError(f"{label} identity changed")
    if compare_version and isinstance(expected, os.stat_result):
        if any(
            getattr(current, field) != getattr(expected, field) for field in _STABLE_FILE_FIELDS
        ):
            raise CampaignError(f"{label} changed")
    return current


def _fsync_existing_regular(
    parent_descriptor: int,
    name: str,
    expected: os.stat_result,
    label: str,
    *,
    allowed_modes: frozenset[int] = _PRIVATE_JSON_MODES,
) -> None:
    flags = os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC
    try:
        descriptor = os.open(name, flags, dir_fd=parent_descriptor)
    except OSError as exc:
        raise CampaignError(f"cannot safely reopen {label}: {exc}") from exc
    try:
        opened = os.fstat(descriptor)
        _require_entry_identity(
            parent_descriptor,
            name,
            expected,
            label,
            compare_version=True,
            allowed_modes=allowed_modes,
        )
        if stat.S_IMODE(opened.st_mode) not in allowed_modes:
            raise CampaignError(f"{label} has unsafe mode while it was reopened")
        if any(getattr(opened, field) != getattr(expected, field) for field in _STABLE_FILE_FIELDS):
            raise CampaignError(f"{label} changed while it was reopened")
        os.fsync(descriptor)
        after = os.fstat(descriptor)
        if stat.S_IMODE(after.st_mode) not in allowed_modes:
            raise CampaignError(f"{label} mode changed while it was synchronized")
        if any(getattr(after, field) != getattr(expected, field) for field in _STABLE_FILE_FIELDS):
            raise CampaignError(f"{label} changed while it was synchronized")
    finally:
        os.close(descriptor)


def _atomic_create_json(
    path: os.PathLike[str] | str,
    value: object,
    *,
    fault_hook: FaultHook | None,
    storage_mode: int,
    accepted_modes: frozenset[int],
) -> Path:
    """Durably create immutable canonical JSON, idempotent by exact bytes."""

    _require_posix_nofollow()
    target = lexical_absolute(path)
    data = canonical_storage_bytes(value)
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC
    with _open_parent(target) as (parent_descriptor, name, parent, parent_identity):
        initial = _lstat_at(parent_descriptor, name)
        if initial is not None:
            existing, existing_item = _read_file_at(
                parent_descriptor,
                name,
                max_bytes=MAX_JSON_BYTES,
                allowed_modes=accepted_modes,
            )
            if existing != data:
                raise CampaignError(f"refusing to overwrite a different artifact: {target}")
            _fsync_existing_regular(
                parent_descriptor,
                name,
                existing_item,
                f"immutable artifact {target}",
                allowed_modes=accepted_modes,
            )
            _assert_parent_identity(parent, parent_descriptor, parent_identity)
            _require_entry_identity(
                parent_descriptor,
                name,
                existing_item,
                f"immutable artifact {target}",
                compare_version=True,
                allowed_modes=accepted_modes,
            )
            _assert_parent_identity(parent, parent_descriptor, parent_identity)
            os.fsync(parent_descriptor)
            _assert_parent_identity(parent, parent_descriptor, parent_identity)
            _require_entry_identity(
                parent_descriptor,
                name,
                existing_item,
                f"immutable artifact {target}",
                compare_version=True,
                allowed_modes=accepted_modes,
            )
            _assert_parent_identity(parent, parent_descriptor, parent_identity)
            return target
        for _ in range(16):
            temporary = f".gffbase-tmp-{secrets.token_hex(16)}"
            try:
                descriptor = os.open(temporary, flags, storage_mode, dir_fd=parent_descriptor)
            except FileExistsError:
                continue
            except OSError as exc:
                raise CampaignError(
                    f"cannot create immutable JSON temporary for {target}: {exc}"
                ) from exc
            break
        else:
            raise CampaignError(f"cannot allocate an immutable JSON temporary for {target}")
        created = _fstat_or_close(descriptor)
        temporary_identity = (created.st_dev, created.st_ino)
        temporary_present = True
        final_linked = False
        accepted_item: os.stat_result | None = None
        try:
            os.fchmod(descriptor, storage_mode)
            _fault(fault_hook, "after_create")
            _write_all(descriptor, data)
            _fault(fault_hook, "after_write")
            os.fsync(descriptor)
            written = os.fstat(descriptor)
            _fault(fault_hook, "after_file_fsync")
            _assert_parent_identity(parent, parent_descriptor, parent_identity)
            _require_entry_identity(
                parent_descriptor,
                temporary,
                written,
                f"immutable JSON temporary for {target}",
                compare_version=True,
                allowed_modes=accepted_modes,
            )
            _fault(fault_hook, "before_commit")
            _assert_parent_identity(parent, parent_descriptor, parent_identity)
            _require_entry_identity(
                parent_descriptor,
                temporary,
                written,
                f"immutable JSON temporary for {target}",
                compare_version=True,
                allowed_modes=accepted_modes,
            )
            try:
                published = _rename_noreplace_bound_at(
                    parent_descriptor,
                    temporary,
                    parent_descriptor,
                    name,
                    written,
                    f"immutable artifact {target}",
                    allowed_modes=accepted_modes,
                )
            except FileExistsError:
                existing, existing_item = _read_file_at(
                    parent_descriptor,
                    name,
                    max_bytes=MAX_JSON_BYTES,
                    allowed_modes=accepted_modes,
                )
                if existing != data:
                    raise CampaignError(
                        f"refusing to overwrite a different artifact: {target}"
                    ) from None
                _fsync_existing_regular(
                    parent_descriptor,
                    name,
                    existing_item,
                    f"immutable artifact {target}",
                    allowed_modes=accepted_modes,
                )
                _assert_parent_identity(parent, parent_descriptor, parent_identity)
                _require_entry_identity(
                    parent_descriptor,
                    name,
                    existing_item,
                    f"immutable artifact {target}",
                    compare_version=True,
                    allowed_modes=accepted_modes,
                )
                accepted_item = existing_item
            else:
                final_linked = True
                temporary_present = False
                accepted_item = published
                _fault(fault_hook, "after_commit")
                _fault(fault_hook, "before_parent_fsync")
                _assert_parent_identity(parent, parent_descriptor, parent_identity)
                _require_entry_identity(
                    parent_descriptor,
                    name,
                    published,
                    f"immutable artifact {target}",
                    compare_version=True,
                    allowed_modes=accepted_modes,
                )
                os.fsync(parent_descriptor)
                _assert_parent_identity(parent, parent_descriptor, parent_identity)
                _require_entry_identity(
                    parent_descriptor,
                    name,
                    published,
                    f"immutable artifact {target}",
                    compare_version=True,
                    allowed_modes=accepted_modes,
                )
        except BaseException:
            _close_descriptor(descriptor)
            if temporary_present:
                _unlink_if_identity(
                    parent_descriptor,
                    temporary,
                    temporary_identity,
                    allowed_modes=accepted_modes,
                )
                os.fsync(parent_descriptor)
            raise
        else:
            _close_descriptor(descriptor)
            if temporary_present:
                _unlink_if_identity(
                    parent_descriptor,
                    temporary,
                    temporary_identity,
                    allowed_modes=accepted_modes,
                )
                temporary_present = False
                os.fsync(parent_descriptor)
            elif not final_linked:
                os.fsync(parent_descriptor)
        _fault(fault_hook, "after_parent_fsync")
        _assert_parent_identity(parent, parent_descriptor, parent_identity)
        if accepted_item is None:
            raise CampaignError(f"immutable artifact disappeared after creation: {target}")
        _require_entry_identity(
            parent_descriptor,
            name,
            accepted_item,
            f"immutable artifact {target}",
            compare_version=True,
            allowed_modes=accepted_modes,
        )
        _assert_parent_identity(parent, parent_descriptor, parent_identity)
    return target


def atomic_create_json(
    path: os.PathLike[str] | str,
    value: object,
    *,
    fault_hook: FaultHook | None = None,
) -> Path:
    """Durably create immutable private canonical JSON, idempotent by bytes."""

    return _atomic_create_json(
        path,
        value,
        fault_hook=fault_hook,
        storage_mode=0o600,
        accepted_modes=_PRIVATE_JSON_MODES,
    )


def atomic_create_public_json(
    path: os.PathLike[str] | str,
    value: object,
    *,
    fault_hook: FaultHook | None = None,
) -> Path:
    """Durably create immutable canonical JSON suitable for Git publication."""

    return _atomic_create_json(
        path,
        value,
        fault_hook=fault_hook,
        storage_mode=_PUBLIC_JSON_WRITE_MODE,
        accepted_modes=_PUBLIC_JSON_MODES,
    )


def _atomic_write_json_durable_under_lock(
    path: os.PathLike[str] | str,
    value: object,
    *,
    fault_hook: FaultHook | None,
    storage_mode: int,
    accepted_modes: frozenset[int],
) -> Path:
    """Replace canonical JSON while the target-specific mutation lock is held."""

    _require_posix_nofollow()
    target = lexical_absolute(path)
    data = canonical_storage_bytes(value)
    with _open_parent(target) as (parent_descriptor, name, parent, parent_identity):
        existing = _lstat_at(parent_descriptor, name)
        if existing is not None:
            _require_unique_regular(
                existing,
                f"replacement target {target}",
                allowed_modes=accepted_modes,
            )
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC
        for _ in range(16):
            temporary = f".gffbase-tmp-{secrets.token_hex(16)}"
            try:
                descriptor = os.open(
                    temporary,
                    flags,
                    storage_mode,
                    dir_fd=parent_descriptor,
                )
            except FileExistsError:
                continue
            except OSError as exc:
                raise CampaignError(
                    f"cannot create replacement temporary for {target}: {exc}"
                ) from exc
            break
        else:
            raise CampaignError(f"cannot allocate a unique replacement temporary for {target}")
        created = _fstat_or_close(descriptor)
        temp_identity = (created.st_dev, created.st_ino)
        temporary_present = True
        cleanup_identity = temp_identity
        try:
            os.fchmod(descriptor, storage_mode)
            _fault(fault_hook, "after_temp_create")
            _write_all(descriptor, data)
            _fault(fault_hook, "after_write")
            os.fsync(descriptor)
            written = os.fstat(descriptor)
            _fault(fault_hook, "after_file_fsync")
            closing_descriptor = descriptor
            descriptor = -1
            os.close(closing_descriptor)
            _assert_parent_identity(parent, parent_descriptor, parent_identity)
            current = _lstat_at(parent_descriptor, name)
            if existing is None:
                if current is not None:
                    raise CampaignError(f"replacement target appeared during write: {target}")
            else:
                _require_entry_identity(
                    parent_descriptor,
                    name,
                    existing,
                    f"replacement target {target}",
                    compare_version=True,
                    allowed_modes=accepted_modes,
                )
            _fault(fault_hook, "before_replace")
            _assert_parent_identity(parent, parent_descriptor, parent_identity)
            _require_entry_identity(
                parent_descriptor,
                temporary,
                written,
                f"replacement temporary for {target}",
                compare_version=True,
                allowed_modes=accepted_modes,
            )
            current = _lstat_at(parent_descriptor, name)
            if existing is None:
                if current is not None:
                    raise CampaignError(f"replacement target appeared at commit: {target}")
            else:
                _require_entry_identity(
                    parent_descriptor,
                    name,
                    existing,
                    f"replacement target {target}",
                    compare_version=True,
                    allowed_modes=accepted_modes,
                )
            if existing is None:
                try:
                    _rename_noreplace_bound_at(
                        parent_descriptor,
                        temporary,
                        parent_descriptor,
                        name,
                        written,
                        f"replacement target {target}",
                        allowed_modes=accepted_modes,
                    )
                except FileExistsError as exc:
                    raise CampaignError(
                        f"replacement target appeared at atomic commit: {target}"
                    ) from exc
                temporary_present = False
            else:
                _rename_exchange_at(
                    parent_descriptor,
                    temporary,
                    parent_descriptor,
                    name,
                )
                cleanup_identity = (existing.st_dev, existing.st_ino)
                published_after_exchange = _require_entry_identity(
                    parent_descriptor,
                    name,
                    temp_identity,
                    f"replacement target {target}",
                    allowed_modes=accepted_modes,
                )
                displaced = _lstat_at(parent_descriptor, temporary)
                displaced_fields = (*_STABLE_FILE_FIELDS, "st_mode", "st_nlink")
                if displaced is None or any(
                    getattr(displaced, field) != getattr(existing, field)
                    for field in displaced_fields
                ):
                    _rename_exchange_at(
                        parent_descriptor,
                        temporary,
                        parent_descriptor,
                        name,
                    )
                    cleanup_identity = temp_identity
                    restored = _lstat_at(parent_descriptor, name)
                    if (
                        displaced is None
                        or restored is None
                        or any(
                            getattr(restored, field) != getattr(displaced, field)
                            for field in displaced_fields
                        )
                    ):
                        raise CampaignError(
                            f"cannot prove restoration of concurrent replacement target: {target}"
                        )
                    _require_entry_identity(
                        parent_descriptor,
                        temporary,
                        written,
                        f"rolled-back replacement temporary for {target}",
                        compare_version=True,
                        allowed_modes=accepted_modes,
                    )
                    _assert_parent_identity(parent, parent_descriptor, parent_identity)
                    raise CampaignError(f"replacement target changed at atomic commit: {target}")
                _require_unique_regular(
                    displaced,
                    f"displaced replacement target {target}",
                    allowed_modes=accepted_modes,
                )
                if any(
                    getattr(published_after_exchange, field) != getattr(written, field)
                    for field in _STABLE_FILE_FIELDS
                ):
                    raise CampaignError(f"replacement target changed during exchange: {target}")
                _unlink_if_identity(
                    parent_descriptor,
                    temporary,
                    cleanup_identity,
                    allowed_modes=accepted_modes,
                )
                temporary_present = False
            published = _require_entry_identity(
                parent_descriptor,
                name,
                temp_identity,
                f"replacement target {target}",
                allowed_modes=accepted_modes,
            )
            _fault(fault_hook, "after_replace")
            _fault(fault_hook, "before_parent_fsync")
            _assert_parent_identity(parent, parent_descriptor, parent_identity)
            _require_entry_identity(
                parent_descriptor,
                name,
                published,
                f"replacement target {target}",
                compare_version=True,
                allowed_modes=accepted_modes,
            )
            os.fsync(parent_descriptor)
            _assert_parent_identity(parent, parent_descriptor, parent_identity)
            _require_entry_identity(
                parent_descriptor,
                name,
                published,
                f"replacement target {target}",
                compare_version=True,
                allowed_modes=accepted_modes,
            )
        except BaseException:
            if descriptor >= 0:
                _close_descriptor(descriptor)
            if temporary_present:
                _unlink_if_identity(
                    parent_descriptor,
                    temporary,
                    cleanup_identity,
                    allowed_modes=accepted_modes,
                )
                os.fsync(parent_descriptor)
            raise
        _fault(fault_hook, "after_parent_fsync")
        _assert_parent_identity(parent, parent_descriptor, parent_identity)
        _require_entry_identity(
            parent_descriptor,
            name,
            published,
            f"replacement target {target}",
            compare_version=True,
            allowed_modes=accepted_modes,
        )
        _assert_parent_identity(parent, parent_descriptor, parent_identity)
    return target


def _atomic_write_lock_binding(target: Path) -> tuple[str, str]:
    binding_sha256 = hashlib.sha256(
        f"gffbase-atomic-write-v1\0{target}".encode("utf-8", errors="strict")
    ).hexdigest()
    return binding_sha256, f"atomic-write-{binding_sha256[:32]}"


def atomic_write_lock_parent_entries(
    path: os.PathLike[str] | str,
    *,
    lock_held: bool,
) -> dict[str, str]:
    """Return exact stable-parent entries for a target-derived writer lock."""

    target = lexical_absolute(path)
    _binding_sha256, lock_name = _atomic_write_lock_binding(target)
    return campaign_lock_parent_entries(
        target.parent,
        lock_name,
        lock_held=lock_held,
    )


def atomic_write_json_durable(
    path: os.PathLike[str] | str,
    value: object,
    *,
    fault_hook: FaultHook | None = None,
) -> Path:
    """Serialize cooperating writers and durably replace canonical JSON."""

    target = lexical_absolute(path)
    binding_sha256, lock_name = _atomic_write_lock_binding(target)
    with campaign_lock(
        target.parent,
        binding_sha256,
        lock_name=lock_name,
    ):
        return _atomic_write_json_durable_under_lock(
            target,
            value,
            fault_hook=fault_hook,
            storage_mode=0o600,
            accepted_modes=_PRIVATE_JSON_MODES,
        )


def atomic_write_public_json_durable(
    path: os.PathLike[str] | str,
    value: object,
    *,
    fault_hook: FaultHook | None = None,
) -> Path:
    """Serialize writers and durably replace Git-publishable canonical JSON."""

    target = lexical_absolute(path)
    binding_sha256, lock_name = _atomic_write_lock_binding(target)
    with campaign_lock(
        target.parent,
        binding_sha256,
        lock_name=lock_name,
    ):
        return _atomic_write_json_durable_under_lock(
            target,
            value,
            fault_hook=fault_hook,
            storage_mode=_PUBLIC_JSON_WRITE_MODE,
            accepted_modes=_PUBLIC_JSON_MODES,
        )


def _scan_directory_fd(
    descriptor: int, expected: Mapping[str, str], *, require_stable: bool = True
) -> dict[str, os.stat_result]:
    expected_record = dict(expected)
    for name, kind in expected_record.items():
        if type(name) is not str or not name or name in {".", ".."} or "/" in name:
            raise CampaignError(f"invalid expected directory entry: {name!r}")
        if kind not in {"file", "directory"}:
            raise CampaignError(f"invalid expected entry kind for {name!r}: {kind!r}")
    directory_before = os.fstat(descriptor)
    try:
        entries = sorted(os.scandir(descriptor), key=lambda entry: entry.name)
    except OSError as exc:
        raise CampaignError(f"cannot scan campaign directory: {exc}") from exc
    names = {entry.name for entry in entries}
    if names != set(expected_record):
        raise CampaignError(
            f"directory entries differ: expected {sorted(expected_record)!r}, found {sorted(names)!r}"
        )
    result: dict[str, os.stat_result] = {}
    for entry in entries:
        if entry.is_symlink():
            raise CampaignError(f"symlink directory entry is not allowed: {entry.name}")
        item = os.stat(entry.name, dir_fd=descriptor, follow_symlinks=False)
        kind = expected_record[entry.name]
        if kind == "file" and (not stat.S_ISREG(item.st_mode) or item.st_nlink != 1):
            raise CampaignError(f"unsafe file directory entry: {entry.name}")
        if kind == "directory" and not stat.S_ISDIR(item.st_mode):
            raise CampaignError(f"unsafe directory entry: {entry.name}")
        result[entry.name] = item

    if not require_stable:
        # The caller is looking at a directory whose owner is still working in
        # it, so "nothing moved while I looked" is not a property it can have.
        # Everything above still held: the exact name set, no symlink, the right
        # entry kinds. What is given up is only the second pass, which exists to
        # prove the bytes did not change -- and a live DuckDB `.wal` changes by
        # design. Requiring it here aborted `status` on healthy campaigns.
        return result

    try:
        final_entries = sorted(os.scandir(descriptor), key=lambda entry: entry.name)
    except OSError as exc:
        raise CampaignError(f"cannot rescan campaign directory: {exc}") from exc
    final_names = {entry.name for entry in final_entries}
    if final_names != set(expected_record):
        raise CampaignError(
            "directory entries changed during scan: "
            f"expected {sorted(expected_record)!r}, found {sorted(final_names)!r}"
        )
    for entry in final_entries:
        if entry.is_symlink():
            raise CampaignError(f"symlink directory entry is not allowed: {entry.name}")
        final_item = os.stat(entry.name, dir_fd=descriptor, follow_symlinks=False)
        prior = result[entry.name]
        comparison_fields = (
            "st_dev",
            "st_ino",
            "st_mode",
            "st_nlink",
            "st_size",
            "st_mtime_ns",
            "st_ctime_ns",
        )
        if any(getattr(final_item, field) != getattr(prior, field) for field in comparison_fields):
            raise CampaignError(f"directory entry changed during scan: {entry.name}")
        result[entry.name] = final_item
    directory_after = os.fstat(descriptor)
    if any(
        getattr(directory_after, field) != getattr(directory_before, field)
        for field in ("st_dev", "st_ino", "st_mode", "st_mtime_ns", "st_ctime_ns")
    ):
        raise CampaignError("directory changed during exact scan")
    return result


def exact_directory_scan(
    path: os.PathLike[str] | str,
    expected: Mapping[str, str],
    *,
    require_stable: bool = True,
) -> dict[str, os.stat_result]:
    """Scan a directory whose exact contents are known, and stat every entry.

    `require_stable` defaults to True, which additionally proves the directory
    did not change while it was being read: every entry is re-stat'd and must
    match on device, inode, mode, link count, size and timestamps. That is the
    contract settled evidence must meet.

    Pass False only for a directory the campaign itself is still writing -- an
    in-flight attempt. The name set, symlink rejection and entry kinds are still
    enforced; only the "nothing moved" claim is dropped, because a database
    being built moves by design.
    """
    directory = lexical_absolute(path)
    with _open_directory(directory) as descriptor:
        opened = os.fstat(descriptor)
        identity = (opened.st_dev, opened.st_ino)
        _assert_parent_identity(directory, descriptor, identity)
        result = _scan_directory_fd(descriptor, expected, require_stable=require_stable)
        _assert_parent_identity(directory, descriptor, identity)
        return result


def create_private_directory(
    path: os.PathLike[str] | str,
    *,
    exist_ok: bool = False,
) -> Path:
    """Durably create one mode-0700 directory through a no-follow parent."""

    _require_posix_nofollow()
    if type(exist_ok) is not bool:
        raise CampaignError("exist_ok must be a boolean")
    target = lexical_absolute(path, "private directory")
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
    with _open_parent(target) as (parent_descriptor, name, parent, parent_identity):
        initial = _lstat_at(parent_descriptor, name)
        if initial is not None and not exist_ok:
            raise CampaignError(f"private directory already exists: {target}")
        created = initial is None
        if created:
            try:
                os.mkdir(name, mode=0o700, dir_fd=parent_descriptor)
            except OSError as exc:
                raise CampaignError(f"cannot create private directory {target}: {exc}") from exc
            os.fsync(parent_descriptor)
        item = _lstat_at(parent_descriptor, name)
        if item is None or not stat.S_ISDIR(item.st_mode):
            raise CampaignError(f"private directory must be mode 0700: {target}")
        if not created and stat.S_IMODE(item.st_mode) != 0o700:
            raise CampaignError(f"private directory must be mode 0700: {target}")
        identity = (item.st_dev, item.st_ino)
        try:
            descriptor = os.open(name, flags, dir_fd=parent_descriptor)
        except OSError as exc:
            raise CampaignError(f"cannot safely open private directory {target}: {exc}") from exc
        try:
            opened = os.fstat(descriptor)
            if (opened.st_dev, opened.st_ino) != identity:
                raise CampaignError(f"private directory changed while opened: {target}")
            if created:
                # A setgid repository parent may add S_ISGID to a new child.
                # Clear every inherited bit through the bound descriptor.
                os.fchmod(descriptor, 0o700)
                os.fsync(descriptor)
                opened = os.fstat(descriptor)
            if stat.S_IMODE(opened.st_mode) != 0o700:
                raise CampaignError(f"private directory mode changed while opened: {target}")
            os.fsync(descriptor)
            current = _require_directory_entry_identity(
                parent_descriptor,
                name,
                identity,
                f"private directory {target}",
            )
            if stat.S_IMODE(current.st_mode) != 0o700:
                raise CampaignError(f"private directory mode changed at its path: {target}")
            _assert_parent_identity(parent, parent_descriptor, parent_identity)
            os.fsync(parent_descriptor)
            final = _require_directory_entry_identity(
                parent_descriptor,
                name,
                identity,
                f"private directory {target}",
            )
            if stat.S_IMODE(final.st_mode) != 0o700:
                raise CampaignError(f"private directory mode changed after sync: {target}")
        finally:
            _close_descriptor(descriptor)
    return target


def create_public_directory(
    path: os.PathLike[str] | str,
    *,
    exist_ok: bool = False,
) -> Path:
    """Durably create a no-follow directory suitable for tracked artifacts."""

    _require_posix_nofollow()
    if type(exist_ok) is not bool:
        raise CampaignError("exist_ok must be a boolean")
    target = lexical_absolute(path, "public directory")
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
    with _open_parent(target) as (parent_descriptor, name, parent, parent_identity):
        initial = _lstat_at(parent_descriptor, name)
        if initial is not None and not exist_ok:
            raise CampaignError(f"public directory already exists: {target}")
        if initial is None:
            try:
                os.mkdir(name, mode=0o755, dir_fd=parent_descriptor)
            except OSError as exc:
                raise CampaignError(f"cannot create public directory {target}: {exc}") from exc
            os.fsync(parent_descriptor)
        item = _lstat_at(parent_descriptor, name)
        if (
            item is None
            or not stat.S_ISDIR(item.st_mode)
            or stat.S_IMODE(item.st_mode) not in _PUBLIC_DIRECTORY_MODES
        ):
            raise CampaignError(f"public directory has unsafe mode or permissions: {target}")
        identity = (item.st_dev, item.st_ino)
        try:
            descriptor = os.open(name, flags, dir_fd=parent_descriptor)
        except OSError as exc:
            raise CampaignError(f"cannot safely open public directory {target}: {exc}") from exc
        try:
            opened = os.fstat(descriptor)
            if (opened.st_dev, opened.st_ino) != identity:
                raise CampaignError(f"public directory changed while opened: {target}")
            if stat.S_IMODE(opened.st_mode) not in _PUBLIC_DIRECTORY_MODES:
                raise CampaignError(f"public directory mode changed while opened: {target}")
            os.fsync(descriptor)
            current = _require_directory_entry_identity(
                parent_descriptor,
                name,
                identity,
                f"public directory {target}",
            )
            if stat.S_IMODE(current.st_mode) not in _PUBLIC_DIRECTORY_MODES:
                raise CampaignError(f"public directory mode changed at its path: {target}")
            _assert_parent_identity(parent, parent_descriptor, parent_identity)
            os.fsync(parent_descriptor)
            final = _require_directory_entry_identity(
                parent_descriptor,
                name,
                identity,
                f"public directory {target}",
            )
            if stat.S_IMODE(final.st_mode) not in _PUBLIC_DIRECTORY_MODES:
                raise CampaignError(f"public directory mode changed after sync: {target}")
        finally:
            _close_descriptor(descriptor)
    return target


def _utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _process_start_ticks(pid: int) -> int:
    try:
        raw = Path(f"/proc/{pid}/stat").read_text(encoding="ascii")
        closing = raw.rfind(")")
        fields = raw[closing + 2 :].split()
        value = int(fields[19])
    except (OSError, UnicodeError, ValueError, IndexError) as exc:
        raise CampaignError(f"cannot read process start ticks for PID {pid}: {exc}") from exc
    if value < 1:
        raise CampaignError(f"invalid process start ticks for PID {pid}")
    return value


def process_identity() -> dict[str, object]:
    pid = os.getpid()
    try:
        boot_id = Path("/proc/sys/kernel/random/boot_id").read_text(encoding="ascii").strip()
    except (OSError, UnicodeError) as exc:
        raise CampaignError(f"cannot read Linux boot identity: {exc}") from exc
    return {
        "host": socket.gethostname(),
        "pid": pid,
        "pgid": os.getpgid(pid),
        "process_start_ticks": _process_start_ticks(pid),
        "boot_id": boot_id,
    }


def _valid_utc(value: object) -> bool:
    if type(value) is not str or _TIMESTAMP_RE.fullmatch(value) is None:
        return False
    try:
        parsed = datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ")
    except ValueError:
        return False
    return parsed.strftime("%Y-%m-%dT%H:%M:%SZ") == value


def validate_lock_owner(value: object) -> dict[str, object]:
    if not isinstance(value, Mapping):
        raise CampaignError("lock owner must be an object")
    owner = dict(value)
    if set(owner) != _OWNER_KEYS:
        raise CampaignError("lock owner has an invalid closed shape")
    if owner["schema_version"] != LOCK_SCHEMA:
        raise CampaignError("lock owner has a stale schema")
    if (
        type(owner["campaign_sha256"]) is not str
        or _SHA256_RE.fullmatch(owner["campaign_sha256"]) is None
    ):
        raise CampaignError("lock owner campaign digest is invalid")
    if type(owner["host"]) is not str or _HOST_RE.fullmatch(owner["host"]) is None:
        raise CampaignError("lock owner host is invalid")
    for key in ("pid", "pgid", "process_start_ticks"):
        if type(owner[key]) is not int or owner[key] < 1:
            raise CampaignError(f"lock owner {key} is invalid")
    if type(owner["boot_id"]) is not str or _BOOT_ID_RE.fullmatch(owner["boot_id"]) is None:
        raise CampaignError("lock owner boot ID is invalid")
    if not _valid_utc(owner["started_utc"]):
        raise CampaignError("lock owner start timestamp is invalid")
    if type(owner["lock_nonce"]) is not str or _NONCE_RE.fullmatch(owner["lock_nonce"]) is None:
        raise CampaignError("lock owner nonce is invalid")
    return owner


def _validate_identity(value: object) -> dict[str, object]:
    if not isinstance(value, Mapping) or set(value) != _IDENTITY_KEYS:
        raise CampaignError("process identity has an invalid closed shape")
    synthetic = {
        "schema_version": LOCK_SCHEMA,
        "campaign_sha256": "0" * 64,
        **dict(value),
        "started_utc": "2000-01-01T00:00:00Z",
        "lock_nonce": "0" * 32,
    }
    validate_lock_owner(synthetic)
    return dict(value)


def _create_bytes_at(parent_descriptor: int, name: str, data: bytes) -> tuple[int, int]:
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC
    descriptor = os.open(name, flags, 0o600, dir_fd=parent_descriptor)
    try:
        os.fchmod(descriptor, 0o600)
        item = os.fstat(descriptor)
        identity = (item.st_dev, item.st_ino)
        _write_all(descriptor, data)
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    os.fsync(parent_descriptor)
    return identity


def _campaign_lock_namespace_path(
    campaign_dir: os.PathLike[str] | str, lock_name: str = "controller.lock"
) -> Path:
    directory = lexical_absolute(campaign_dir, "campaign lock directory")
    validate_identifier(lock_name, "lock")
    namespace_digest = hashlib.sha256(
        f"{directory}\0{lock_name}".encode("utf-8", errors="strict")
    ).hexdigest()
    return directory.parent / LOCK_ROOT_NAME / namespace_digest


def _campaign_lock_path(
    campaign_dir: os.PathLike[str] | str, lock_name: str = "controller.lock"
) -> Path:
    namespace = _campaign_lock_namespace_path(campaign_dir, lock_name)
    return namespace.parent.parent / f".gffbase-active-lock-{namespace.name}"


def campaign_lock_parent_entries(
    campaign_dir: os.PathLike[str] | str,
    lock_name: str = "controller.lock",
    *,
    lock_held: bool,
) -> dict[str, str]:
    """Return internal stable-parent entries for an exact lock-aware scan."""

    if type(lock_held) is not bool:
        raise CampaignError("lock_held must be a boolean")
    entries = {LOCK_ROOT_NAME: "directory"}
    if lock_held:
        entries[_campaign_lock_path(campaign_dir, lock_name).name] = "directory"
    return entries


def _open_or_create_private_directory_at(
    parent_descriptor: int, name: str, label: str
) -> tuple[int, tuple[int, int]]:
    created = False
    try:
        os.mkdir(name, mode=0o700, dir_fd=parent_descriptor)
    except FileExistsError:
        pass
    except OSError as exc:
        raise CampaignError(f"cannot create {label}: {exc}") from exc
    else:
        created = True
        os.fsync(parent_descriptor)

    item = _lstat_at(parent_descriptor, name)
    if item is None or not stat.S_ISDIR(item.st_mode):
        raise CampaignError(f"{label} is not a private mode-0700 directory")
    if not created and stat.S_IMODE(item.st_mode) != 0o700:
        raise CampaignError(f"{label} is not a private mode-0700 directory")
    identity = (item.st_dev, item.st_ino)
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
    try:
        descriptor = os.open(name, flags, dir_fd=parent_descriptor)
    except OSError as exc:
        raise CampaignError(f"cannot safely open {label}: {exc}") from exc
    try:
        opened = os.fstat(descriptor)
        if (opened.st_dev, opened.st_ino) != identity:
            raise CampaignError(f"{label} changed while it was opened")
        if created:
            os.fchmod(descriptor, 0o700)
            os.fsync(descriptor)
            opened = os.fstat(descriptor)
        if stat.S_IMODE(opened.st_mode) != 0o700:
            raise CampaignError(f"{label} is not a private mode-0700 directory")
        current = _require_directory_entry_identity(parent_descriptor, name, identity, label)
        if stat.S_IMODE(current.st_mode) != 0o700:
            raise CampaignError(f"{label} mode changed at its path")
        if created:
            os.fsync(parent_descriptor)
            final = _require_directory_entry_identity(parent_descriptor, name, identity, label)
            if stat.S_IMODE(final.st_mode) != 0o700:
                raise CampaignError(f"{label} mode changed after synchronization")
    except BaseException:
        _close_descriptor(descriptor)
        raise
    return descriptor, identity


@contextmanager
def campaign_lock(
    campaign_dir: os.PathLike[str] | str,
    campaign_sha256: str,
    *,
    identity_provider: Callable[[], Mapping[str, object]] = process_identity,
    nonce_provider: Callable[[], str] = lambda: secrets.token_hex(16),
    now_provider: Callable[[], str] = _utc_now,
    lock_name: str = "controller.lock",
) -> Iterator[dict[str, object]]:
    """Acquire a digest lock below a trusted stable parent; never auto-break it."""

    _require_posix_nofollow()
    if type(campaign_sha256) is not str or _SHA256_RE.fullmatch(campaign_sha256) is None:
        raise CampaignError("campaign lock requires a lowercase SHA-256 digest")
    directory = inspect_path(campaign_dir, require_kind="directory")
    identity = _validate_identity(identity_provider())
    owner = validate_lock_owner(
        {
            "schema_version": LOCK_SCHEMA,
            "campaign_sha256": campaign_sha256,
            **identity,
            "started_utc": now_provider(),
            "lock_nonce": nonce_provider(),
        }
    )
    owner_bytes = canonical_storage_bytes(owner)
    validate_identifier(lock_name, "lock")
    namespace_path = _campaign_lock_namespace_path(directory, lock_name)
    lock_path = _campaign_lock_path(directory, lock_name)
    active_name = lock_path.name
    namespace_name = namespace_path.name
    inspected_directory = os.lstat(directory)
    directory_identity = (inspected_directory.st_dev, inspected_directory.st_ino)

    with _open_directory(directory) as directory_descriptor:
        opened_directory = os.fstat(directory_descriptor)
        if (opened_directory.st_dev, opened_directory.st_ino) != directory_identity:
            raise CampaignError("campaign directory changed while acquiring its lock")
        with _open_parent(directory) as (
            stable_parent_descriptor,
            directory_name,
            stable_parent,
            stable_parent_identity,
        ):

            def require_protected_binding() -> None:
                _assert_parent_identity(
                    stable_parent,
                    stable_parent_descriptor,
                    stable_parent_identity,
                )
                _require_directory_entry_identity(
                    stable_parent_descriptor,
                    directory_name,
                    directory_identity,
                    "protected campaign directory",
                )
                _assert_parent_identity(directory, directory_descriptor, directory_identity)
                _assert_parent_identity(
                    stable_parent,
                    stable_parent_descriptor,
                    stable_parent_identity,
                )

            require_protected_binding()
            lock_root_descriptor, lock_root_identity = _open_or_create_private_directory_at(
                stable_parent_descriptor,
                LOCK_ROOT_NAME,
                "campaign lock root",
            )
            try:
                namespace_descriptor, namespace_identity = _open_or_create_private_directory_at(
                    lock_root_descriptor,
                    namespace_name,
                    "campaign lock namespace",
                )
                try:

                    def require_history_binding() -> None:
                        _assert_parent_identity(
                            stable_parent,
                            stable_parent_descriptor,
                            stable_parent_identity,
                        )
                        _require_directory_entry_identity(
                            stable_parent_descriptor,
                            LOCK_ROOT_NAME,
                            lock_root_identity,
                            "campaign lock root",
                        )
                        _assert_parent_identity(
                            stable_parent,
                            stable_parent_descriptor,
                            stable_parent_identity,
                        )
                        _require_directory_entry_identity(
                            lock_root_descriptor,
                            namespace_name,
                            namespace_identity,
                            "campaign lock namespace",
                        )
                        _require_directory_entry_identity(
                            stable_parent_descriptor,
                            LOCK_ROOT_NAME,
                            lock_root_identity,
                            "campaign lock root",
                        )

                    require_protected_binding()
                    require_history_binding()
                    try:
                        os.mkdir(active_name, mode=0o700, dir_fd=stable_parent_descriptor)
                    except FileExistsError:
                        try:
                            prior = strict_json_load(
                                lock_path / "owner.json",
                                validator=validate_lock_owner,
                            )
                        except CampaignError as exc:
                            raise CampaignError(
                                f"campaign is locked by an invalid or ambiguous owner: {exc}"
                            ) from exc
                        raise CampaignError(f"campaign is already locked: {prior}") from None
                    except OSError as exc:
                        raise CampaignError(f"cannot acquire campaign lock: {exc}") from exc
                    os.fsync(stable_parent_descriptor)
                    lock_item = _lstat_at(stable_parent_descriptor, active_name)
                    if lock_item is None or not stat.S_ISDIR(lock_item.st_mode):
                        raise CampaignError("new campaign lock is not a directory")
                    lock_identity = (lock_item.st_dev, lock_item.st_ino)
                    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
                    lock_descriptor = os.open(
                        active_name,
                        flags,
                        dir_fd=stable_parent_descriptor,
                    )
                    try:
                        opened_lock = os.fstat(lock_descriptor)
                        if (opened_lock.st_dev, opened_lock.st_ino) != lock_identity:
                            raise CampaignError("campaign lock changed while it was opened")
                        os.fchmod(lock_descriptor, 0o700)
                        _require_directory_entry_identity(
                            stable_parent_descriptor,
                            active_name,
                            lock_identity,
                            "campaign lock directory",
                        )
                        owner_identity = _create_bytes_at(
                            lock_descriptor,
                            "owner.json",
                            owner_bytes,
                        )
                        require_protected_binding()
                        require_history_binding()
                        _require_directory_entry_identity(
                            stable_parent_descriptor,
                            active_name,
                            lock_identity,
                            "campaign lock directory",
                        )
                    except BaseException:
                        _close_descriptor(lock_descriptor)
                        raise

                    try:
                        yield dict(owner)
                    finally:
                        try:
                            require_protected_binding()
                            _require_directory_entry_identity(
                                stable_parent_descriptor,
                                active_name,
                                lock_identity,
                                "campaign lock directory",
                            )
                            found, found_owner = _read_file_at(
                                lock_descriptor,
                                "owner.json",
                                max_bytes=MAX_JSON_BYTES,
                            )
                            if found != owner_bytes:
                                raise CampaignError("campaign lock owner was tampered with")
                            if (found_owner.st_dev, found_owner.st_ino) != owner_identity:
                                raise CampaignError("campaign lock owner identity changed")
                            _scan_directory_fd(lock_descriptor, {"owner.json": "file"})
                            require_protected_binding()
                            require_history_binding()
                            _require_directory_entry_identity(
                                stable_parent_descriptor,
                                active_name,
                                lock_identity,
                                "campaign lock directory",
                            )
                            for _ in range(16):
                                released_name = f"released-{secrets.token_hex(16)}"
                                if _lstat_at(namespace_descriptor, released_name) is None:
                                    break
                            else:
                                raise CampaignError(
                                    "cannot allocate a campaign lock release tombstone"
                                )
                            _rename_noreplace_at(
                                stable_parent_descriptor,
                                active_name,
                                namespace_descriptor,
                                released_name,
                            )
                            released = _require_directory_entry_identity(
                                namespace_descriptor,
                                released_name,
                                lock_identity,
                                "released campaign lock directory",
                            )
                            opened_release = os.fstat(lock_descriptor)
                            if (released.st_dev, released.st_ino) != (
                                opened_release.st_dev,
                                opened_release.st_ino,
                            ):
                                raise CampaignError(
                                    "released campaign lock descriptor identity changed"
                                )
                            found_after, found_owner_after = _read_file_at(
                                lock_descriptor,
                                "owner.json",
                                max_bytes=MAX_JSON_BYTES,
                            )
                            if found_after != owner_bytes:
                                raise CampaignError(
                                    "released campaign lock owner was tampered with"
                                )
                            if (
                                found_owner_after.st_dev,
                                found_owner_after.st_ino,
                            ) != owner_identity:
                                raise CampaignError("released campaign lock owner identity changed")
                            _scan_directory_fd(lock_descriptor, {"owner.json": "file"})
                            os.fsync(namespace_descriptor)
                            os.fsync(stable_parent_descriptor)
                            if _lstat_at(stable_parent_descriptor, active_name) is not None:
                                raise CampaignError(
                                    "released campaign lock reappeared after synchronization"
                                )
                            _require_directory_entry_identity(
                                namespace_descriptor,
                                released_name,
                                lock_identity,
                                "released campaign lock directory",
                            )
                            require_protected_binding()
                            require_history_binding()
                        finally:
                            _close_descriptor(lock_descriptor)
                finally:
                    _close_descriptor(namespace_descriptor)
            finally:
                _close_descriptor(lock_root_descriptor)


__all__ = [
    "LOCK_ROOT_NAME",
    "LOCK_SCHEMA",
    "MAX_JSON_BYTES",
    "MAX_JSON_FLOAT_TOKEN_CHARS",
    "MAX_JSON_INTEGER_TOKEN_CHARS",
    "RETIRE_ROOT_NAME",
    "atomic_create_json",
    "atomic_create_public_json",
    "atomic_write_lock_parent_entries",
    "atomic_write_json_durable",
    "atomic_write_public_json_durable",
    "attempt_directory",
    "campaign_lock",
    "campaign_lock_parent_entries",
    "canonical_storage_bytes",
    "create_private_directory",
    "create_public_directory",
    "exact_directory_scan",
    "fsync_regular_file_durable",
    "inspect_path",
    "internal_path_limit_components",
    "job_directory",
    "lexical_absolute",
    "process_identity",
    "require_exact_derived_path",
    "resolve_beneath",
    "sha256_regular_file",
    "strict_json_load",
    "strict_public_json_load",
    "validate_campaign_root",
    "validate_lock_owner",
]
