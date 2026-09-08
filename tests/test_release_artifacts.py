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
"""Adversarial tests for the exact package artifacts handed to PyPI."""

from __future__ import annotations

import base64
import csv
import hashlib
import io
import json
import stat
import tarfile
import zipfile
from pathlib import Path

import pytest

from tools import release_artifacts

VERSION = "0.2.0rc1"
SOURCE_SHA = "a" * 40
PLATFORMS = {
    "linux-x86_64": "manylinux_2_17_x86_64",
    "linux-aarch64": "manylinux_2_17_aarch64",
    "macos-x86_64": "macosx_10_12_x86_64",
    "macos-arm64": "macosx_11_0_arm64",
    "windows-x86_64": "win_amd64",
}


def _record_digest(data: bytes) -> str:
    encoded = base64.urlsafe_b64encode(hashlib.sha256(data).digest()).rstrip(b"=")
    return "sha256=" + encoded.decode("ascii")


def _wheel(
    root: Path,
    target: str,
    *,
    version: str = VERSION,
    metadata_name: str = "gffbase",
    metadata_version: str | None = None,
    root_is_purelib: str = "false",
    include_native: bool = True,
    include_marker: bool = True,
    include_license: bool = True,
    extra_member: tuple[str, bytes] | None = None,
    corrupt_record_for: str | None = None,
    symlink_member: str | None = None,
) -> Path:
    platform = PLATFORMS[target]
    filename = f"gffbase-{version}-cp310-abi3-{platform}.whl"
    dist_info = f"gffbase-{version}.dist-info"
    native_suffix = ".pyd" if target == "windows-x86_64" else ".so"
    members = {
        "gffbase/__init__.py": b"from ._version import __version__\n",
        "gffbase/_version.py": f'__version__ = "{version}"\n'.encode(),
        f"{dist_info}/METADATA": (
            "Metadata-Version: 2.4\n"
            f"Name: {metadata_name}\n"
            f"Version: {metadata_version or version}\n"
            "Requires-Python: >=3.10\n"
            "Requires-Dist: duckdb>=1.4.1\n"
            "Requires-Dist: pyarrow>=18.1\n"
            "License-File: LICENSE\n\n"
        ).encode(),
        f"{dist_info}/WHEEL": (
            "Wheel-Version: 1.0\n"
            "Generator: test-suite\n"
            f"Root-Is-Purelib: {root_is_purelib}\n"
            f"Tag: cp310-abi3-{platform}\n\n"
        ).encode(),
    }
    if include_native:
        members[f"gffbase/_native.abi3{native_suffix}"] = b"native-extension"
    if include_marker:
        members["gffbase/py.typed"] = b""
    if include_license:
        members[f"{dist_info}/licenses/LICENSE"] = b"Apache License\n"
    if extra_member is not None:
        members[extra_member[0]] = extra_member[1]

    record_name = f"{dist_info}/RECORD"
    rows = []
    for name, data in members.items():
        digest = _record_digest(data)
        if name == corrupt_record_for:
            digest = "sha256=" + "A" * 43
        rows.append((name, digest, str(len(data))))
    rows.append((record_name, "", ""))
    output = io.StringIO(newline="")
    csv.writer(output, lineterminator="\n").writerows(rows)
    members[record_name] = output.getvalue().encode()

    path = root / filename
    with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for name, data in members.items():
            if name == symlink_member:
                info = zipfile.ZipInfo(name)
                info.create_system = 3
                info.external_attr = stat.S_IFLNK << 16
                archive.writestr(info, data)
            else:
                archive.writestr(name, data)
    return path


def _tar_bytes(text: str) -> bytes:
    return text.encode("utf-8")


def _sdist(
    root: Path,
    *,
    version: str = VERSION,
    metadata_version: str | None = None,
    cargo_version: str = "0.2.0-rc.1",
    extra_member: str | None = None,
    symlink_member: str | None = None,
) -> Path:
    top = f"gffbase-{version}"
    members = {
        f"{top}/PKG-INFO": _tar_bytes(
            "Metadata-Version: 2.4\n"
            "Name: gffbase\n"
            f"Version: {metadata_version or version}\n"
            "Requires-Python: >=3.10\n"
            "Requires-Dist: duckdb>=1.4.1\n"
            "Requires-Dist: pyarrow>=18.1\n\n"
        ),
        f"{top}/pyproject.toml": _tar_bytes(
            "[build-system]\n"
            'requires = ["maturin>=1.5,<2.0"]\n'
            'build-backend = "maturin"\n\n'
            "[project]\n"
            'name = "gffbase"\n'
            f'version = "{version}"\n'
            'requires-python = ">=3.10"\n'
            'dependencies = ["duckdb>=1.4.1", "pyarrow>=18.1"]\n'
        ),
        f"{top}/rust/Cargo.toml": _tar_bytes(
            f'[package]\nname = "gffbase-core"\nversion = "{cargo_version}"\n'
        ),
        f"{top}/rust/Cargo.lock": b"# locked\n",
        f"{top}/rust/src/lib.rs": b"// native source\n",
        f"{top}/python/gffbase/__init__.py": b"from ._version import __version__\n",
        f"{top}/python/gffbase/_version.py": _tar_bytes(f'__version__ = "{version}"\n'),
        f"{top}/python/gffbase/py.typed": b"",
        f"{top}/tests/test_build_integrity.py": b"def test_sdist(): pass\n",
        f"{top}/tests/data/simple.gff3": b"##gff-version 3\n",
        f"{top}/LICENSE": b"Apache License\n",
    }
    if extra_member is not None:
        members[extra_member] = b"extra"
    if symlink_member is not None:
        members.setdefault(symlink_member, b"")

    path = root / f"gffbase-{version}.tar.gz"
    with tarfile.open(path, "w:gz", format=tarfile.PAX_FORMAT) as archive:
        for name, data in members.items():
            info = tarfile.TarInfo(name)
            info.mtime = 0
            if name == symlink_member:
                info.type = tarfile.SYMTYPE
                info.linkname = "../../outside"
                archive.addfile(info)
            else:
                info.size = len(data)
                info.mode = 0o644
                archive.addfile(info, io.BytesIO(data))
    return path


def _complete_set(root: Path) -> None:
    for target in PLATFORMS:
        _wheel(root, target)
    _sdist(root)


def test_exact_five_wheels_and_sdist_validate_with_closed_target_inventory(tmp_path: Path):
    _complete_set(tmp_path)
    records = release_artifacts.inspect_artifact_set(tmp_path, expected_version=VERSION)
    assert [record.target for record in records] == sorted(
        [*PLATFORMS, release_artifacts.SDIST_TARGET]
    )
    assert all(
        record.sha256 == hashlib.sha256((tmp_path / record.filename).read_bytes()).hexdigest()
        for record in records
    )
    assert all(record.size > 0 for record in records)


def test_missing_duplicate_and_extra_release_targets_fail_closed(tmp_path: Path):
    _complete_set(tmp_path)
    (tmp_path / f"gffbase-{VERSION}-cp310-abi3-win_amd64.whl").unlink()
    with pytest.raises(release_artifacts.ArtifactValidationError, match="target inventory"):
        release_artifacts.inspect_artifact_set(tmp_path, expected_version=VERSION)

    _wheel(tmp_path, "windows-x86_64")
    _wheel(tmp_path, "linux-x86_64", version="0.2.0rc2")
    with pytest.raises(release_artifacts.ArtifactValidationError, match="unexpected package file"):
        release_artifacts.inspect_artifact_set(tmp_path, expected_version=VERSION)


@pytest.mark.parametrize(
    ("options", "message"),
    [
        ({"metadata_name": "other"}, "METADATA Name"),
        ({"metadata_version": "0.2.0rc2"}, "METADATA Version"),
        ({"root_is_purelib": "true"}, "Root-Is-Purelib"),
        ({"include_native": False}, "native extension"),
        ({"include_marker": False}, "py.typed"),
        ({"include_license": False}, "license"),
        ({"corrupt_record_for": "gffbase/_version.py"}, "RECORD digest"),
    ],
)
def test_wheel_metadata_contents_and_record_are_verified(
    tmp_path: Path, options: dict, message: str
):
    _wheel(tmp_path, "linux-x86_64", **options)
    with pytest.raises(release_artifacts.ArtifactValidationError, match=message):
        release_artifacts.inspect_artifact_set(
            tmp_path,
            expected_version=VERSION,
            expected_targets={"linux-x86_64"},
        )


@pytest.mark.parametrize(
    "options",
    [
        {"extra_member": ("../escape", b"bad")},
        {"extra_member": ("/absolute", b"bad")},
        {"extra_member": ("gffbase\\escape.py", b"bad")},
        {"extra_member": ("gffbase/link", b"target"), "symlink_member": "gffbase/link"},
    ],
)
def test_wheel_rejects_unsafe_or_nonregular_members(tmp_path: Path, options: dict):
    _wheel(tmp_path, "linux-x86_64", **options)
    with pytest.raises(release_artifacts.ArtifactValidationError, match="unsafe|regular"):
        release_artifacts.inspect_artifact_set(
            tmp_path,
            expected_version=VERSION,
            expected_targets={"linux-x86_64"},
        )


@pytest.mark.parametrize(
    ("options", "message"),
    [
        ({"metadata_version": "0.2.0rc2"}, "PKG-INFO Version"),
        ({"cargo_version": "0.2.0-rc.2"}, "Cargo version"),
        ({"extra_member": "../escape"}, "single safe root"),
        ({"extra_member": "/absolute"}, "single safe root"),
        ({"symlink_member": f"gffbase-{VERSION}/link"}, "regular"),
    ],
)
def test_sdist_metadata_source_versions_and_members_are_verified(
    tmp_path: Path, options: dict, message: str
):
    _sdist(tmp_path, **options)
    with pytest.raises(release_artifacts.ArtifactValidationError, match=message):
        release_artifacts.inspect_artifact_set(
            tmp_path,
            expected_version=VERSION,
            expected_targets={release_artifacts.SDIST_TARGET},
        )


def test_manifest_binds_source_identity_artifact_bytes_and_target_set(tmp_path: Path):
    packages = tmp_path / "packages"
    packages.mkdir()
    _complete_set(packages)
    records = release_artifacts.inspect_artifact_set(packages, expected_version=VERSION)
    manifest_path = tmp_path / "release-artifact-manifest.json"
    release_artifacts.write_manifest(
        manifest_path,
        records=records,
        expected_version=VERSION,
        source_sha=SOURCE_SHA,
        workflow_identity={
            "repository": "Kuanhao-Chao/gffbase",
            "workflow_ref": "Kuanhao-Chao/gffbase/.github/workflows/release.yml@refs/tags/v0.2.0rc1",
            "run_id": "123",
            "run_attempt": "1",
        },
    )
    loaded = release_artifacts.verify_manifest(
        manifest_path,
        artifact_root=packages,
        expected_version=VERSION,
        source_sha=SOURCE_SHA,
    )
    assert loaded["schema"] == "gffbase-release-artifact-manifest-v1"
    assert len(loaded["artifacts"]) == 6

    wheel = next(packages.glob("*manylinux*x86_64.whl"))
    wheel.write_bytes(wheel.read_bytes() + b"tampered")
    with pytest.raises(release_artifacts.ArtifactValidationError, match="digest|size"):
        release_artifacts.verify_manifest(
            manifest_path,
            artifact_root=packages,
            expected_version=VERSION,
            source_sha=SOURCE_SHA,
        )


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("schema", "draft-v0", "schema"),
        ("source.version", "0.2.0rc2", "version"),
        ("source.commit", "b" * 40, "commit"),
        ("workflow.run_id", 123, "run_id"),
    ],
)
def test_manifest_schema_is_closed_and_strictly_typed(
    tmp_path: Path, field: str, value, message: str
):
    packages = tmp_path / "packages"
    packages.mkdir()
    _complete_set(packages)
    records = release_artifacts.inspect_artifact_set(packages, expected_version=VERSION)
    manifest_path = tmp_path / "manifest.json"
    release_artifacts.write_manifest(
        manifest_path,
        records=records,
        expected_version=VERSION,
        source_sha=SOURCE_SHA,
        workflow_identity={
            "repository": "Kuanhao-Chao/gffbase",
            "workflow_ref": "workflow@ref",
            "run_id": "123",
            "run_attempt": "1",
        },
    )
    payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    owner = payload
    pieces = field.split(".")
    for piece in pieces[:-1]:
        owner = owner[piece]
    owner[pieces[-1]] = value
    manifest_path.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(release_artifacts.ArtifactValidationError, match=message):
        release_artifacts.verify_manifest(
            manifest_path,
            artifact_root=packages,
            expected_version=VERSION,
            source_sha=SOURCE_SHA,
        )
