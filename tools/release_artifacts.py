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
"""Strict inspection and digest manifests for publishable package artifacts."""

from __future__ import annotations

import argparse
import base64
import csv
import hashlib
import io
import json
import os
import re
import stat
import sys
import tarfile
import zipfile
from collections.abc import Iterable, Mapping
from dataclasses import asdict, dataclass
from email import policy
from email.parser import BytesParser
from pathlib import Path, PurePosixPath
from typing import Any

from packaging.requirements import InvalidRequirement, Requirement
from packaging.tags import Tag, parse_tag
from packaging.utils import canonicalize_name, parse_sdist_filename, parse_wheel_filename
from packaging.version import InvalidVersion, Version

try:
    import tomllib
except ModuleNotFoundError:  # pragma: no cover - Python 3.10 only
    import tomli as tomllib  # type: ignore[no-redef]

MANIFEST_SCHEMA = "gffbase-release-artifact-manifest-v1"
SDIST_TARGET = "sdist"
WHEEL_TARGETS = frozenset(
    {
        "linux-x86_64",
        "linux-aarch64",
        "macos-x86_64",
        "macos-arm64",
        "windows-x86_64",
    }
)
RELEASE_TARGETS = frozenset({*WHEEL_TARGETS, SDIST_TARGET})
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_GIT_SHA = re.compile(r"^[0-9a-f]{40}$")
_MAX_MEMBER_SIZE = 512 * 1024 * 1024
_MAX_ARCHIVE_SIZE = 2 * 1024 * 1024 * 1024


class ArtifactValidationError(ValueError):
    """An archive or manifest does not satisfy the release contract."""


@dataclass(frozen=True, slots=True)
class ArtifactRecord:
    """Portable digest and target identity for one package artifact."""

    filename: str
    kind: str
    target: str
    size: int
    sha256: str
    tags: tuple[str, ...]


def _error(message: str) -> None:
    raise ArtifactValidationError(message)


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    try:
        with path.open("rb") as stream:
            while block := stream.read(1024 * 1024):
                digest.update(block)
    except OSError as exc:
        _error(f"cannot hash artifact {path}: {exc}")
    return digest.hexdigest()


def _safe_archive_name(name: str) -> bool:
    if not name or "\x00" in name or "\\" in name or name.startswith("/"):
        return False
    path = PurePosixPath(name)
    return not path.is_absolute() and all(part not in {"", ".", ".."} for part in path.parts)


def _single_header(message, name: str, *, context: str) -> str:
    values = message.get_all(name, failobj=[])
    if len(values) != 1 or not isinstance(values[0], str) or not values[0].strip():
        _error(f"{context} must contain exactly one nonempty {name} header")
    return values[0].strip()


def _metadata(data: bytes, *, context: str):
    try:
        message = BytesParser(policy=policy.compat32).parsebytes(data)
    except Exception as exc:  # email exposes several parse exception types
        _error(f"cannot parse {context}: {exc}")
    if message.defects:
        _error(f"{context} contains malformed headers: {message.defects}")
    return message


def _validate_distribution_metadata(data: bytes, *, expected_version: str, context: str) -> None:
    message = _metadata(data, context=context)
    name = _single_header(message, "Name", context=context)
    if canonicalize_name(name) != "gffbase":
        _error(f"{context} Name must be gffbase, found {name!r}")
    version = _single_header(message, "Version", context=context)
    if version != expected_version:
        _error(f"{context} Version must be {expected_version!r}, found {version!r}")
    requires_python = _single_header(message, "Requires-Python", context=context)
    if requires_python != ">=3.10":
        _error(f"{context} Requires-Python must be '>=3.10', found {requires_python!r}")

    requirements: dict[str, list[Requirement]] = {}
    for raw in message.get_all("Requires-Dist", failobj=[]):
        try:
            requirement = Requirement(raw)
        except InvalidRequirement as exc:
            _error(f"{context} has invalid Requires-Dist {raw!r}: {exc}")
        requirements.setdefault(canonicalize_name(requirement.name), []).append(requirement)
    expected_runtime = {"duckdb": ">=1.4.1", "pyarrow": ">=18.1"}
    for package, specifier in expected_runtime.items():
        matches = [req for req in requirements.get(package, []) if req.marker is None]
        if len(matches) != 1 or str(matches[0].specifier) != specifier:
            _error(f"{context} must contain one unconditional {package}{specifier} requirement")


def _target_for_platform(platform: str) -> str:
    if (platform.startswith("manylinux") or platform.startswith("musllinux")) and platform.endswith(
        "_x86_64"
    ):
        return "linux-x86_64"
    if (platform.startswith("manylinux") or platform.startswith("musllinux")) and platform.endswith(
        "_aarch64"
    ):
        return "linux-aarch64"
    if platform.startswith("macosx") and platform.endswith("_x86_64"):
        return "macos-x86_64"
    if platform.startswith("macosx") and platform.endswith("_arm64"):
        return "macos-arm64"
    if platform == "win_amd64":
        return "windows-x86_64"
    _error(f"unsupported wheel platform tag {platform!r}")


def _wheel_identity(path: Path, expected_version: str) -> tuple[str, frozenset[Tag]]:
    try:
        name, version, _build, tags = parse_wheel_filename(path.name)
    except (InvalidVersion, ValueError) as exc:
        _error(f"unexpected package file {path.name!r}: invalid wheel filename ({exc})")
    if canonicalize_name(name) != "gffbase" or str(version) != expected_version:
        _error(f"unexpected package file {path.name!r}: expected gffbase {expected_version} wheel")
    if not tags:
        _error(f"wheel {path.name!r} has no compatibility tags")
    if any(tag.interpreter != "cp310" or tag.abi != "abi3" for tag in tags):
        _error(f"wheel {path.name!r} must use only cp310-abi3 tags")
    targets = {_target_for_platform(tag.platform) for tag in tags}
    if len(targets) != 1:
        _error(f"wheel {path.name!r} spans multiple release targets: {sorted(targets)}")
    return targets.pop(), tags


def _zip_members(path: Path) -> dict[str, bytes]:
    try:
        archive = zipfile.ZipFile(path)
    except (OSError, zipfile.BadZipFile) as exc:
        _error(f"cannot open wheel {path.name!r}: {exc}")
    with archive:
        infos = archive.infolist()
        names = [info.filename for info in infos]
        if len(names) != len(set(names)):
            _error(f"wheel {path.name!r} contains duplicate archive member names")
        total = 0
        members: dict[str, bytes] = {}
        for info in infos:
            if not _safe_archive_name(info.filename):
                _error(f"wheel {path.name!r} contains unsafe member {info.filename!r}")
            mode = (info.external_attr >> 16) & 0xFFFF
            file_type = stat.S_IFMT(mode)
            if info.is_dir() or file_type not in {0, stat.S_IFREG}:
                _error(f"wheel {path.name!r} member {info.filename!r} is not a regular file")
            if info.file_size > _MAX_MEMBER_SIZE:
                _error(f"wheel {path.name!r} member {info.filename!r} is unreasonably large")
            total += info.file_size
            if total > _MAX_ARCHIVE_SIZE:
                _error(f"wheel {path.name!r} expands beyond the release size limit")
            try:
                members[info.filename] = archive.read(info)
            except (OSError, RuntimeError, zipfile.BadZipFile) as exc:
                _error(f"cannot read wheel {path.name!r} member {info.filename!r}: {exc}")
        bad_crc = archive.testzip()
        if bad_crc is not None:
            _error(f"wheel {path.name!r} has a CRC failure in {bad_crc!r}")
    return members


def _validate_record(members: Mapping[str, bytes], record_name: str, *, wheel: str) -> None:
    try:
        text = members[record_name].decode("utf-8")
    except UnicodeDecodeError as exc:
        _error(f"wheel {wheel!r} RECORD is not UTF-8: {exc}")
    rows: dict[str, tuple[str, str]] = {}
    try:
        reader = csv.reader(io.StringIO(text, newline=""))
        for row in reader:
            if len(row) != 3:
                _error(f"wheel {wheel!r} RECORD rows must have exactly three columns")
            name, digest, size_text = row
            if not _safe_archive_name(name):
                _error(f"wheel {wheel!r} RECORD contains unsafe path {name!r}")
            if name in rows:
                _error(f"wheel {wheel!r} RECORD contains duplicate path {name!r}")
            rows[name] = (digest, size_text)
    except csv.Error as exc:
        _error(f"wheel {wheel!r} RECORD is invalid CSV: {exc}")

    if set(rows) != set(members):
        missing = sorted(set(members) - set(rows))
        extra = sorted(set(rows) - set(members))
        _error(f"wheel {wheel!r} RECORD inventory mismatch; missing={missing}, extra={extra}")
    for name, data in members.items():
        digest, size_text = rows[name]
        if name == record_name:
            if digest or size_text:
                _error(f"wheel {wheel!r} RECORD must leave its own digest and size empty")
            continue
        if not size_text.isascii() or not size_text.isdecimal() or int(size_text) != len(data):
            _error(f"wheel {wheel!r} RECORD size mismatch for {name!r}")
        expected_digest = "sha256=" + base64.urlsafe_b64encode(
            hashlib.sha256(data).digest()
        ).rstrip(b"=").decode("ascii")
        if digest != expected_digest:
            _error(f"wheel {wheel!r} RECORD digest mismatch for {name!r}")


def _inspect_wheel(path: Path, expected_version: str) -> ArtifactRecord:
    target, filename_tags = _wheel_identity(path, expected_version)
    members = _zip_members(path)
    dist_info = f"gffbase-{expected_version}.dist-info"
    metadata_name = f"{dist_info}/METADATA"
    wheel_name = f"{dist_info}/WHEEL"
    record_name = f"{dist_info}/RECORD"
    for required in (metadata_name, wheel_name, record_name):
        if required not in members:
            _error(f"wheel {path.name!r} is missing required member {required!r}")
    foreign_dist_info = {
        name.split("/", 1)[0]
        for name in members
        if ".dist-info/" in name and name.split("/", 1)[0] != dist_info
    }
    if foreign_dist_info:
        _error(
            f"wheel {path.name!r} contains unexpected dist-info roots {sorted(foreign_dist_info)}"
        )

    _validate_distribution_metadata(
        members[metadata_name], expected_version=expected_version, context="wheel METADATA"
    )
    metadata = _metadata(members[metadata_name], context="wheel METADATA")
    license_headers = metadata.get_all("License-File", failobj=[])
    if "LICENSE" not in license_headers or f"{dist_info}/licenses/LICENSE" not in members:
        _error(f"wheel {path.name!r} is missing its declared license file")

    wheel_metadata = _metadata(members[wheel_name], context="wheel WHEEL")
    if _single_header(wheel_metadata, "Root-Is-Purelib", context="wheel WHEEL").lower() != "false":
        _error("wheel WHEEL Root-Is-Purelib must be false")
    wheel_version = _single_header(wheel_metadata, "Wheel-Version", context="wheel WHEEL")
    if wheel_version != "1.0":
        _error(f"wheel WHEEL version must be '1.0', found {wheel_version!r}")
    metadata_tags: set[Tag] = set()
    for raw in wheel_metadata.get_all("Tag", failobj=[]):
        try:
            metadata_tags.update(parse_tag(raw))
        except ValueError as exc:
            _error(f"wheel WHEEL contains invalid Tag {raw!r}: {exc}")
    if metadata_tags != set(filename_tags):
        _error(f"wheel {path.name!r} filename tags and WHEEL Tag headers disagree")

    version_member = "gffbase/_version.py"
    if version_member not in members:
        _error(f"wheel {path.name!r} is missing {version_member}")
    version_match = re.search(
        rb'^__version__\s*=\s*["\']([^"\']+)["\']\s*$',
        members[version_member],
        re.MULTILINE,
    )
    if (
        version_match is None
        or version_match.group(1).decode("ascii", "replace") != expected_version
    ):
        _error(f"wheel {path.name!r} Python version module does not match {expected_version}")
    if "gffbase/py.typed" not in members:
        _error(f"wheel {path.name!r} is missing gffbase/py.typed")

    native = [
        name for name in members if re.fullmatch(r"gffbase/_native(?:\.[^/]+)?\.(?:so|pyd)", name)
    ]
    if len(native) != 1:
        _error(f"wheel {path.name!r} must contain exactly one native extension; found {native}")
    expected_suffix = ".pyd" if target == "windows-x86_64" else ".so"
    if not native[0].endswith(expected_suffix):
        _error(f"wheel {path.name!r} native extension suffix disagrees with target {target}")

    _validate_record(members, record_name, wheel=path.name)
    return ArtifactRecord(
        filename=path.name,
        kind="wheel",
        target=target,
        size=path.stat().st_size,
        sha256=_file_sha256(path),
        tags=tuple(sorted(str(tag) for tag in filename_tags)),
    )


def _sdist_identity(path: Path, expected_version: str) -> None:
    try:
        name, version = parse_sdist_filename(path.name)
    except (InvalidVersion, ValueError) as exc:
        _error(f"unexpected package file {path.name!r}: invalid sdist filename ({exc})")
    if canonicalize_name(name) != "gffbase" or str(version) != expected_version:
        _error(f"unexpected package file {path.name!r}: expected gffbase {expected_version} sdist")


def _tar_members(path: Path, expected_root: str) -> dict[str, bytes]:
    try:
        archive = tarfile.open(path, mode="r:gz")
    except (OSError, tarfile.TarError) as exc:
        _error(f"cannot open sdist {path.name!r}: {exc}")
    with archive:
        infos = archive.getmembers()
        names = [item.name for item in infos]
        if len(names) != len(set(names)):
            _error(f"sdist {path.name!r} contains duplicate archive member names")
        members: dict[str, bytes] = {}
        total = 0
        for item in infos:
            if (
                not _safe_archive_name(item.name)
                or PurePosixPath(item.name).parts[0] != expected_root
            ):
                _error(
                    f"sdist members must have a single safe root {expected_root!r}; "
                    f"found {item.name!r}"
                )
            if item.isdir():
                continue
            if not item.isfile():
                _error(f"sdist member {item.name!r} is not a regular file")
            if item.size > _MAX_MEMBER_SIZE:
                _error(f"sdist member {item.name!r} is unreasonably large")
            total += item.size
            if total > _MAX_ARCHIVE_SIZE:
                _error(f"sdist {path.name!r} expands beyond the release size limit")
            stream = archive.extractfile(item)
            if stream is None:
                _error(f"cannot read sdist member {item.name!r}")
            try:
                data = stream.read(_MAX_MEMBER_SIZE + 1)
            finally:
                stream.close()
            if len(data) != item.size:
                _error(f"sdist member {item.name!r} size disagrees with its tar header")
            members[item.name] = data
    return members


def _toml(data: bytes, *, context: str) -> dict[str, Any]:
    try:
        return tomllib.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, tomllib.TOMLDecodeError) as exc:
        _error(f"cannot parse {context}: {exc}")


def _inspect_sdist(path: Path, expected_version: str) -> ArtifactRecord:
    _sdist_identity(path, expected_version)
    root = f"gffbase-{expected_version}"
    members = _tar_members(path, root)
    relative = {name.removeprefix(root + "/"): data for name, data in members.items()}
    required = {
        "PKG-INFO",
        "pyproject.toml",
        "rust/Cargo.toml",
        "rust/Cargo.lock",
        "rust/src/lib.rs",
        "python/gffbase/__init__.py",
        "python/gffbase/_version.py",
        "python/gffbase/py.typed",
        "tests/test_build_integrity.py",
        "tests/data/simple.gff3",
        "LICENSE",
    }
    missing = sorted(required - set(relative))
    if missing:
        _error(f"sdist {path.name!r} is missing required source members: {missing}")
    _validate_distribution_metadata(
        relative["PKG-INFO"], expected_version=expected_version, context="sdist PKG-INFO"
    )

    project = _toml(relative["pyproject.toml"], context="sdist pyproject.toml")
    try:
        project_table = project["project"]
        project_name = project_table["name"]
        project_version = project_table["version"]
        requires_python = project_table["requires-python"]
    except (KeyError, TypeError) as exc:
        _error(f"sdist pyproject.toml is missing project identity: {exc}")
    if (project_name, project_version, requires_python) != (
        "gffbase",
        expected_version,
        ">=3.10",
    ):
        _error("sdist pyproject.toml project identity does not match the release")

    cargo = _toml(relative["rust/Cargo.toml"], context="sdist Cargo.toml")
    try:
        cargo_version = cargo["package"]["version"]
        cargo_public = str(Version(cargo_version))
    except (KeyError, TypeError, InvalidVersion) as exc:
        _error(f"sdist Cargo version is missing or invalid: {exc}")
    if cargo_public != expected_version:
        _error(
            f"sdist Cargo version {cargo_version!r} normalizes to {cargo_public!r}, "
            f"not {expected_version!r}"
        )

    version_match = re.search(
        rb'^__version__\s*=\s*["\']([^"\']+)["\']\s*$',
        relative["python/gffbase/_version.py"],
        re.MULTILINE,
    )
    if (
        version_match is None
        or version_match.group(1).decode("ascii", "replace") != expected_version
    ):
        _error(f"sdist Python version module does not match {expected_version}")
    return ArtifactRecord(
        filename=path.name,
        kind="sdist",
        target=SDIST_TARGET,
        size=path.stat().st_size,
        sha256=_file_sha256(path),
        tags=(),
    )


def _package_paths(root: Path) -> list[Path]:
    if not root.is_dir() or root.is_symlink():
        _error(f"artifact root must be a real directory: {root}")
    paths: list[Path] = []
    names: set[str] = set()
    for path in sorted(root.rglob("*")):
        if path.is_symlink():
            _error(f"artifact input contains a symlink: {path}")
        if not path.is_file():
            continue
        if path.name.endswith(".whl") or path.name.endswith(".tar.gz"):
            if path.name in names:
                _error(f"artifact input contains duplicate filename {path.name!r}")
            names.add(path.name)
            paths.append(path)
    return paths


def inspect_artifact_set(
    artifact_root: Path,
    *,
    expected_version: str,
    expected_targets: Iterable[str] = RELEASE_TARGETS,
) -> tuple[ArtifactRecord, ...]:
    """Inspect package archives and require the exact requested target inventory."""
    try:
        if str(Version(expected_version)) != expected_version:
            _error(f"expected version {expected_version!r} is not canonical PEP 440")
    except InvalidVersion as exc:
        _error(f"expected version {expected_version!r} is invalid: {exc}")
    targets = frozenset(expected_targets)
    if not targets or not targets <= RELEASE_TARGETS:
        _error(f"invalid expected target inventory: {sorted(targets)}")

    records: list[ArtifactRecord] = []
    for path in _package_paths(artifact_root):
        record = (
            _inspect_wheel(path, expected_version)
            if path.name.endswith(".whl")
            else _inspect_sdist(path, expected_version)
        )
        records.append(record)
    actual = [record.target for record in records]
    if len(actual) != len(set(actual)):
        _error(f"duplicate release target coverage: {sorted(actual)}")
    if set(actual) != targets:
        _error(
            f"release target inventory mismatch; expected={sorted(targets)}, actual={sorted(actual)}"
        )
    return tuple(sorted(records, key=lambda record: record.target))


def _strict_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            _error(f"manifest contains duplicate JSON key {key!r}")
        result[key] = value
    return result


def _strict_json(path: Path) -> dict[str, Any]:
    try:
        raw = path.read_bytes()
    except OSError as exc:
        _error(f"cannot read manifest {path}: {exc}")
    if len(raw) > 1024 * 1024:
        _error("release artifact manifest is unreasonably large")
    try:
        value = json.loads(raw, object_pairs_hook=_strict_object)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        _error(f"release artifact manifest is not strict UTF-8 JSON: {exc}")
    if not isinstance(value, dict):
        _error("release artifact manifest must be a JSON object")
    return value


def _exact_keys(value: Any, expected: set[str], *, context: str) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != expected:
        found = sorted(value) if isinstance(value, dict) else type(value).__name__
        _error(f"{context} must have exactly keys {sorted(expected)}; found {found}")
    return value


def _manifest_payload(
    *,
    records: Iterable[ArtifactRecord],
    expected_version: str,
    source_sha: str,
    workflow_identity: Mapping[str, str],
) -> dict[str, Any]:
    if _GIT_SHA.fullmatch(source_sha) is None:
        _error("source commit must be one lowercase 40-character Git SHA")
    workflow = _exact_keys(
        dict(workflow_identity),
        {"repository", "workflow_ref", "run_id", "run_attempt"},
        context="workflow identity",
    )
    if any(not isinstance(value, str) or not value for value in workflow.values()):
        _error("workflow identity values must be nonempty strings")
    if not workflow["run_id"].isdigit() or not workflow["run_attempt"].isdigit():
        _error("workflow run_id and run_attempt must be decimal strings")
    ordered = sorted(records, key=lambda record: record.target)
    if {record.target for record in ordered} != RELEASE_TARGETS or len(ordered) != len(
        RELEASE_TARGETS
    ):
        _error("manifest requires the exact full release target inventory")
    return {
        "schema": MANIFEST_SCHEMA,
        "source": {"name": "gffbase", "version": expected_version, "commit": source_sha},
        "workflow": workflow,
        "artifacts": [asdict(record) for record in ordered],
    }


def write_manifest(
    path: Path,
    *,
    records: Iterable[ArtifactRecord],
    expected_version: str,
    source_sha: str,
    workflow_identity: Mapping[str, str],
) -> None:
    """Create a deterministic manifest without replacing prior evidence."""
    payload = _manifest_payload(
        records=records,
        expected_version=expected_version,
        source_sha=source_sha,
        workflow_identity=workflow_identity,
    )
    encoded = (json.dumps(payload, sort_keys=True, indent=2, ensure_ascii=True) + "\n").encode()
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o644)
    except OSError as exc:
        _error(f"cannot create immutable release artifact manifest {path}: {exc}")
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(encoded)
            stream.flush()
            os.fsync(stream.fileno())
    except Exception:
        try:
            path.unlink()
        except OSError:
            pass
        raise


def _validate_manifest_shape(
    payload: dict[str, Any], *, expected_version: str, source_sha: str
) -> tuple[ArtifactRecord, ...]:
    root = _exact_keys(
        payload,
        {"schema", "source", "workflow", "artifacts"},
        context="release artifact manifest",
    )
    if root["schema"] != MANIFEST_SCHEMA:
        _error(f"unsupported release artifact manifest schema {root['schema']!r}")
    source = _exact_keys(root["source"], {"name", "version", "commit"}, context="manifest source")
    if source["name"] != "gffbase":
        _error("manifest source name must be gffbase")
    if source["version"] != expected_version:
        _error(f"manifest source version {source['version']!r} does not match {expected_version!r}")
    if source["commit"] != source_sha or _GIT_SHA.fullmatch(source["commit"] or "") is None:
        _error("manifest source commit does not match the expected Git commit")
    workflow = _exact_keys(
        root["workflow"],
        {"repository", "workflow_ref", "run_id", "run_attempt"},
        context="manifest workflow",
    )
    if not isinstance(workflow["repository"], str) or not workflow["repository"]:
        _error("manifest workflow repository must be a nonempty string")
    if not isinstance(workflow["workflow_ref"], str) or not workflow["workflow_ref"]:
        _error("manifest workflow workflow_ref must be a nonempty string")
    if not isinstance(workflow["run_id"], str) or not workflow["run_id"].isdigit():
        _error("manifest workflow run_id must be a decimal string")
    if not isinstance(workflow["run_attempt"], str) or not workflow["run_attempt"].isdigit():
        _error("manifest workflow run_attempt must be a decimal string")

    raw_artifacts = root["artifacts"]
    if not isinstance(raw_artifacts, list):
        _error("manifest artifacts must be a list")
    records: list[ArtifactRecord] = []
    for index, raw in enumerate(raw_artifacts):
        item = _exact_keys(
            raw,
            {"filename", "kind", "target", "size", "sha256", "tags"},
            context=f"manifest artifact {index}",
        )
        if (
            not isinstance(item["filename"], str)
            or PurePosixPath(item["filename"]).name != item["filename"]
            or not _safe_archive_name(item["filename"])
        ):
            _error(f"manifest artifact {index} filename is unsafe")
        if item["kind"] not in {"wheel", "sdist"}:
            _error(f"manifest artifact {index} kind is invalid")
        if item["target"] not in RELEASE_TARGETS:
            _error(f"manifest artifact {index} target is invalid")
        if type(item["size"]) is not int or item["size"] <= 0:
            _error(f"manifest artifact {index} size must be a positive integer")
        if not isinstance(item["sha256"], str) or _SHA256.fullmatch(item["sha256"]) is None:
            _error(f"manifest artifact {index} sha256 is invalid")
        if not isinstance(item["tags"], list) or any(
            not isinstance(tag, str) or not tag for tag in item["tags"]
        ):
            _error(f"manifest artifact {index} tags must be a string list")
        records.append(
            ArtifactRecord(
                filename=item["filename"],
                kind=item["kind"],
                target=item["target"],
                size=item["size"],
                sha256=item["sha256"],
                tags=tuple(item["tags"]),
            )
        )
    if (
        len(records) != len(RELEASE_TARGETS)
        or {record.target for record in records} != RELEASE_TARGETS
    ):
        _error("manifest artifact target inventory is not the exact release inventory")
    if records != sorted(records, key=lambda record: record.target):
        _error("manifest artifacts must be sorted by target")
    if len({record.filename for record in records}) != len(records):
        _error("manifest artifact filenames must be unique")
    return tuple(records)


def verify_manifest(
    manifest_path: Path,
    *,
    artifact_root: Path,
    expected_version: str,
    source_sha: str,
) -> dict[str, Any]:
    """Reload a manifest and independently re-inspect every bound package byte."""
    payload = _strict_json(manifest_path)
    manifest_records = _validate_manifest_shape(
        payload, expected_version=expected_version, source_sha=source_sha
    )
    fresh = inspect_artifact_set(artifact_root, expected_version=expected_version)
    if fresh != manifest_records:
        expected = {record.target: asdict(record) for record in manifest_records}
        actual = {record.target: asdict(record) for record in fresh}
        _error(
            f"artifact digest, size, tags, or filenames differ from manifest: {expected} != {actual}"
        )
    return payload


def _targets(values: list[str] | None) -> frozenset[str]:
    return RELEASE_TARGETS if values is None else frozenset(values)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)

    inspect_parser = subparsers.add_parser("inspect", help="inspect an artifact set")
    inspect_parser.add_argument("--artifact-dir", type=Path, required=True)
    inspect_parser.add_argument("--expected-version", required=True)
    inspect_parser.add_argument(
        "--expected-target", action="append", choices=sorted(RELEASE_TARGETS)
    )

    create = subparsers.add_parser("create-manifest", help="inspect and bind a full release set")
    create.add_argument("--artifact-dir", type=Path, required=True)
    create.add_argument("--manifest", type=Path, required=True)
    create.add_argument("--expected-version", required=True)
    create.add_argument("--source-sha", required=True)
    create.add_argument("--repository", required=True)
    create.add_argument("--workflow-ref", required=True)
    create.add_argument("--run-id", required=True)
    create.add_argument("--run-attempt", required=True)

    verify = subparsers.add_parser("verify-manifest", help="reverify packages against a manifest")
    verify.add_argument("--artifact-dir", type=Path, required=True)
    verify.add_argument("--manifest", type=Path, required=True)
    verify.add_argument("--expected-version", required=True)
    verify.add_argument("--source-sha", required=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        if args.command == "inspect":
            records = inspect_artifact_set(
                args.artifact_dir,
                expected_version=args.expected_version,
                expected_targets=_targets(args.expected_target),
            )
            print(json.dumps([asdict(record) for record in records], sort_keys=True, indent=2))
        elif args.command == "create-manifest":
            records = inspect_artifact_set(
                args.artifact_dir, expected_version=args.expected_version
            )
            write_manifest(
                args.manifest,
                records=records,
                expected_version=args.expected_version,
                source_sha=args.source_sha,
                workflow_identity={
                    "repository": args.repository,
                    "workflow_ref": args.workflow_ref,
                    "run_id": args.run_id,
                    "run_attempt": args.run_attempt,
                },
            )
            verify_manifest(
                args.manifest,
                artifact_root=args.artifact_dir,
                expected_version=args.expected_version,
                source_sha=args.source_sha,
            )
            print(args.manifest)
        else:
            verify_manifest(
                args.manifest,
                artifact_root=args.artifact_dir,
                expected_version=args.expected_version,
                source_sha=args.source_sha,
            )
            print(args.manifest)
    except ArtifactValidationError as exc:
        print(f"release artifact validation failed: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":  # pragma: no cover - workflow CLI entry point
    raise SystemExit(main())
