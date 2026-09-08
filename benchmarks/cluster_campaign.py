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
"""Crash-safe, dry-by-default Linux benchmark campaign controller.

The older benchmark scripts remain the measurement primitives.  This module
adds the part that a multi-hour cluster run needs: immutable identities,
disjoint workers, resumable attempts, strict correctness gates, and a single
post-run merger.  Importing this module never probes the host or changes disk.
"""

from __future__ import annotations

import argparse
import contextlib
import gzip
import hashlib
import json
import os
import platform
import re
import shlex
import shutil
import signal
import socket
import subprocess
import sys
import time
import urllib.parse
from collections.abc import Callable, Iterator, Mapping, Sequence
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, cast

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from benchmarks.campaign import model as _campaign_model
from benchmarks.campaign import preflight as _campaign_preflight
from benchmarks.campaign import results as _campaign_results
from benchmarks.campaign import safe_io as _campaign_safe_io
from benchmarks.campaign import worker as _campaign_worker
from benchmarks.common import (
    benchmark_env,
    sha256_file,
)
from benchmarks.corpora import BY_KEY, CORPORA, corpus_path

CAMPAIGN_SCHEMA = _campaign_model.CAMPAIGN_SCHEMA
PREFLIGHT_SCHEMA = _campaign_model.PREFLIGHT_SCHEMA
STATUS_SCHEMA = _campaign_model.STATUS_SCHEMA
LAUNCH_SCHEMA = _campaign_model.LAUNCH_SCHEMA
WORKER_SCHEMA = _campaign_model.WORKER_SCHEMA
RESULTS_SCHEMA = _campaign_model.RESULTS_SCHEMA
INDEX_SCHEMA = _campaign_model.INDEX_SCHEMA
SIGNATURE_SCHEMA = _campaign_model.SIGNATURE_SCHEMA
PUBLIC_VERSION = _campaign_model.PUBLIC_VERSION
CARGO_VERSION = _campaign_model.CARGO_VERSION
REGION_SEED = _campaign_model.REGION_SEED

CORPUS_ORDER = _campaign_model.CORPUS_ORDER
THREADS = _campaign_model.THREADS
LANE_CPUS = _campaign_model.LANE_CPUS
MIN_FREE_BYTES = 80 * (1 << 30)
MIN_AVAILABLE_RAM_BYTES = 40 * (1 << 30)
IDENTIFIER_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{0,79}$")
TRANSFORM_DESCRIPTION = "remove rows whose third GTF column is gene or transcript"


class CampaignError(RuntimeError):
    """A campaign artifact, identity, or safety gate is invalid."""


@dataclass(frozen=True)
class CpuLane:
    worker_id: str
    threads: int
    cpus: str

    @property
    def cpu_set(self) -> tuple[int, ...]:
        return parse_cpu_list(self.cpus)


@dataclass(frozen=True)
class JobSpec:
    job_id: str
    phase: str
    kind: str
    worker_id: str
    corpus_key: str
    result_key: str
    gtf_arm: str | None
    engine_set: str
    interpreter_role: str
    threads: int
    cpus: str
    input: dict[str, Any]
    parameters: dict[str, Any]
    primary_eligible: bool = False
    job_sha256: str = ""

    def to_dict(self) -> dict[str, Any]:
        value = asdict(self)
        if not value["job_sha256"]:
            unsigned = {key: item for key, item in value.items() if key != "job_sha256"}
            value["job_sha256"] = sha256_json(unsigned)
        return value

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> JobSpec:
        return cls(**{field: value[field] for field in cls.__dataclass_fields__})


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def canonical_json_bytes(value: object) -> bytes:
    """Canonical bytes used for all campaign identity digests."""

    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")


def sha256_json(value: object) -> str:
    return hashlib.sha256(canonical_json_bytes(value)).hexdigest()


def _reject_symlink(path: Path) -> None:
    _campaign_safe_io.inspect_path(path, allow_missing_tail=True)


def atomic_write_json_durable(path: Path, value: object) -> Path:
    return _campaign_safe_io.atomic_write_json_durable(path, value)


def atomic_create_json(path: Path, value: object) -> Path:
    return _campaign_safe_io.atomic_create_json(path, value)


def load_schema(path: Path, expected_schema: str) -> dict[str, Any]:
    def validate(value: object) -> dict[str, Any]:
        if not isinstance(value, dict) or value.get("schema_version") != expected_schema:
            found = value.get("schema_version") if isinstance(value, dict) else type(value).__name__
            raise CampaignError(
                f"{path}: expected schema_version={expected_schema!r}, found {found!r}"
            )
        return dict(value)

    loaded = _campaign_safe_io.strict_json_load(path, validator=validate)
    if not isinstance(loaded, dict):
        raise CampaignError(f"{path}: schema validator returned a non-object")
    return loaded


def validate_identifier(value: str, kind: str = "identifier") -> str:
    if not IDENTIFIER_RE.fullmatch(value):
        raise CampaignError(
            f"invalid {kind} {value!r}; use 1-80 lowercase letters, digits, '.', '_' or '-'"
        )
    if value.startswith(".") or ".." in value or "/" in value or "\\" in value:
        raise CampaignError(f"unsafe {kind}: {value!r}")
    return value


def resolve_beneath(root: Path, candidate: Path, *, must_exist: bool = False) -> Path:
    return _campaign_safe_io.resolve_beneath(
        root,
        candidate,
        must_exist=must_exist,
        allow_missing_tail=not must_exist,
    )


def _paths_overlap(left: Path, right: Path) -> bool:
    left = left.resolve(strict=False)
    right = right.resolve(strict=False)
    try:
        left.relative_to(right)
        return True
    except ValueError:
        pass
    try:
        right.relative_to(left)
        return True
    except ValueError:
        return False


def validate_campaign_root(root: Path, repo_root: Path = ROOT) -> Path:
    return _campaign_safe_io.validate_campaign_root(root, repo_root=repo_root)


def validate_job_path(campaign_root: Path, job_id: str, candidate: Path) -> Path:
    jobs_root = _campaign_safe_io.job_directory(campaign_root, job_id)
    return resolve_beneath(jobs_root, candidate)


@contextlib.contextmanager
def campaign_lock(campaign_dir: Path, campaign_sha256: str) -> Iterator[dict[str, object]]:
    with _campaign_safe_io.campaign_lock(campaign_dir, campaign_sha256) as owner:
        yield owner


def parse_cpu_list(value: str) -> tuple[int, ...]:
    cpus: list[int] = []
    for part in value.split(","):
        if "-" in part:
            start_text, end_text = part.split("-", 1)
            start, end = int(start_text), int(end_text)
            if start < 0 or end < start:
                raise CampaignError(f"invalid CPU range: {part}")
            cpus.extend(range(start, end + 1))
        else:
            cpu = int(part)
            if cpu < 0:
                raise CampaignError(f"invalid CPU: {part}")
            cpus.append(cpu)
    if len(set(cpus)) != len(cpus):
        raise CampaignError(f"overlapping CPU list: {value}")
    return tuple(cpus)


def topology() -> dict[str, Any]:
    lanes = [
        asdict(CpuLane(f"scale-t{threads:02d}", threads, cpus))
        for threads, cpus in zip(THREADS, LANE_CPUS, strict=True)
    ]
    return {
        "lanes": lanes,
        "canonical": asdict(CpuLane("canonical", 10, "0-9")),
    }


def git_identity(repo_root: Path = ROOT) -> dict[str, Any]:
    def run(*args: str) -> str:
        proc = subprocess.run(
            ["git", "-C", str(repo_root), *args],
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
        if proc.returncode:
            raise CampaignError(f"git {' '.join(args)} failed: {proc.stderr.strip()}")
        return proc.stdout.strip()

    commit = run("rev-parse", "HEAD")
    branch = run("rev-parse", "--abbrev-ref", "HEAD")
    status = run("status", "--porcelain", "--untracked-files=all")
    submodules = run("submodule", "status", "--recursive")
    dirty_submodules = [line for line in submodules.splitlines() if line[:1] in {"+", "-", "U"}]
    return {
        "root": str(Path(repo_root).resolve()),
        "commit": commit,
        "branch": branch,
        "dirty": bool(status or dirty_submodules),
        "status": status.splitlines(),
        "dirty_submodules": dirty_submodules,
    }


def _normalize_version(value: str | None) -> str | None:
    if value is None:
        return None
    return re.sub(r"-rc\.?([0-9]+)$", r"rc\1", value)


def wheel_identity(wheel: Path) -> dict[str, Any]:
    return dict(
        _campaign_preflight.verify_candidate_wheel(
            wheel,
            public_version=PUBLIC_VERSION,
        )
    )


_PROBE_SCRIPT = r"""
import hashlib, importlib, importlib.metadata as md, json, pathlib, platform, site, sys
role = sys.argv[1]
package_names = ("gffbase", "gffutils", "duckdb", "pyarrow", "psutil", "numpy")
def version(name):
    try: return md.version(name)
    except md.PackageNotFoundError: return None
def module_info(name):
    module = importlib.import_module(name)
    path = pathlib.Path(module.__file__).resolve()
    value = {"path": str(path), "version": getattr(module, "__version__", None)}
    if path.is_file(): value["sha256"] = hashlib.sha256(path.read_bytes()).hexdigest()
    return value
packages = {name: version(name) for name in package_names}
module_names = {
    "primary": ("gffbase", "gffbase._native", "gffutils", "duckdb", "pyarrow", "psutil", "numpy"),
    "gffbase-0.1.0": ("gffbase", "duckdb", "pyarrow", "psutil", "numpy"),
    "gffutils-0.13": ("gffutils", "psutil"),
}[role]
targets = {name: module_info(name) for name in module_names}
direct_url = {}
distribution_roots = {}
for name in package_names:
    try:
        dist = md.distribution(name)
        direct_url[name] = dist.read_text("direct_url.json")
        distribution_roots[name] = str(pathlib.Path(dist.locate_file("")).resolve())
    except md.PackageNotFoundError:
        direct_url[name] = None
pth_files = []
for base_text in site.getsitepackages():
    base = pathlib.Path(base_text).resolve()
    if not base.is_dir(): continue
    for path in sorted(base.glob("*.pth")):
        raw = path.read_bytes()
        text = raw.decode("utf-8", errors="strict")
        pth_files.append({
            "path": str(path.resolve()),
            "sha256": hashlib.sha256(raw).hexdigest(),
            "active_lines": [line.strip() for line in text.splitlines()
                             if line.strip() and not line.lstrip().startswith("#")],
        })
payload = {
    "resolved_executable": str(pathlib.Path(sys.executable).resolve()),
    "prefix": str(pathlib.Path(sys.prefix).resolve()),
    "implementation": platform.python_implementation(),
    "python_version": platform.python_version(),
    "isolated": sys.flags.isolated,
    "sys_path": [str(pathlib.Path(value).resolve()) for value in sys.path if value],
    "packages": packages,
    "modules": targets,
    "distribution_roots": distribution_roots,
    "direct_url": direct_url,
    "pth_files": pth_files,
}
print(json.dumps(payload, sort_keys=True))
"""


def probe_interpreter(python: Path, role: str) -> dict[str, Any]:
    if role not in {"primary", "gffbase-0.1.0", "gffutils-0.13"}:
        raise CampaignError(f"unknown interpreter role: {role}")
    python = _campaign_safe_io.inspect_path(
        python,
        require_kind="file",
        require_unique_file=True,
    )
    before = os.lstat(python)
    if before.st_mode & 0o111 == 0:
        raise CampaignError(f"interpreter is not executable: {python}")
    try:
        proc = subprocess.run(
            [str(python), "-I", "-c", _PROBE_SCRIPT, role],
            capture_output=True,
            text=True,
            timeout=120,
            check=False,
            env={
                "PATH": os.defpath,
                "LANG": "C.UTF-8",
                "LC_ALL": "C.UTF-8",
                "PYTHONNOUSERSITE": "1",
            },
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise CampaignError(f"failed to execute {role} interpreter probe: {exc}") from exc
    try:
        after = os.lstat(python)
    except OSError as exc:
        raise CampaignError(f"interpreter disappeared during probe: {python}") from exc
    version_fields = (
        "st_dev",
        "st_ino",
        "st_mode",
        "st_nlink",
        "st_size",
        "st_mtime_ns",
        "st_ctime_ns",
    )
    if any(getattr(after, field) != getattr(before, field) for field in version_fields):
        raise CampaignError(f"interpreter identity changed during probe: {python}")
    if proc.returncode:
        raise CampaignError(f"failed to probe {role} interpreter: {proc.stderr.strip()}")
    try:
        probe = json.loads(proc.stdout.splitlines()[-1])
    except (IndexError, json.JSONDecodeError) as exc:
        raise CampaignError(f"invalid probe output from {python}") from exc
    _validate_interpreter_probe(probe, role)
    probe["requested_executable"] = str(python)
    probe["executable_identity"] = {
        "st_dev": before.st_dev,
        "st_ino": before.st_ino,
        "st_size": before.st_size,
        "st_mtime_ns": before.st_mtime_ns,
        "st_ctime_ns": before.st_ctime_ns,
    }
    probe["role"] = role
    probe["probe_sha256"] = sha256_json(probe)
    return probe


def _validate_interpreter_probe(probe: Mapping[str, Any], role: str) -> None:
    if probe.get("implementation") != "CPython":
        raise CampaignError(f"{role} requires CPython")
    version_text = probe.get("python_version")
    if not isinstance(version_text, str) or re.fullmatch(r"\d+\.\d+\.\d+", version_text) is None:
        raise CampaignError(f"{role} reported an invalid Python version")
    if tuple(int(value) for value in version_text.split(".")[:2]) < (3, 10):
        raise CampaignError(f"{role} requires Python 3.10 or newer")
    if probe.get("isolated") != 1:
        raise CampaignError(f"{role} interpreter probe did not run in isolated mode")
    versions = probe.get("packages")
    modules = probe.get("modules")
    package_names = {"gffbase", "gffutils", "duckdb", "pyarrow", "psutil", "numpy"}
    if not isinstance(versions, Mapping) or set(versions) != package_names:
        raise CampaignError(f"{role} package probe has an invalid closed shape")
    expected_modules = {
        "primary": {
            "gffbase",
            "gffbase._native",
            "gffutils",
            "duckdb",
            "pyarrow",
            "psutil",
            "numpy",
        },
        "gffbase-0.1.0": {"gffbase", "duckdb", "pyarrow", "psutil", "numpy"},
        "gffutils-0.13": {"gffutils", "psutil"},
    }[role]
    if not isinstance(modules, Mapping) or set(modules) != expected_modules:
        raise CampaignError(f"{role} module probe has an invalid closed shape")
    if role == "primary":
        expected = {"gffbase": PUBLIC_VERSION, "gffutils": "0.14"}
        module_version = (modules.get("gffbase") or {}).get("version")
        native_version = (modules.get("gffbase._native") or {}).get("version")
        if _normalize_version(module_version) != PUBLIC_VERSION:
            raise CampaignError(
                f"primary gffbase module is {module_version!r}, not {PUBLIC_VERSION}"
            )
        if _normalize_version(native_version) != PUBLIC_VERSION:
            raise CampaignError(
                f"primary native module is {native_version!r}, not {PUBLIC_VERSION}"
            )
    elif role == "gffbase-0.1.0":
        expected = {"gffbase": "0.1.0"}
    else:
        expected = {"gffutils": "0.13"}
    for package, version in expected.items():
        if versions.get(package) != version:
            raise CampaignError(
                f"{role} requires {package} {version}, found {versions.get(package)!r}"
            )
    required_dependencies = {
        "primary": {"duckdb", "pyarrow", "psutil", "numpy"},
        "gffbase-0.1.0": {"duckdb", "pyarrow", "psutil", "numpy"},
        "gffutils-0.13": {"psutil"},
    }[role]
    for package in required_dependencies:
        if not isinstance(versions.get(package), str):
            raise CampaignError(f"{role} lacks required benchmark dependency {package}")

    prefix = Path(str(probe.get("prefix", ""))).resolve(strict=False)
    executable = Path(str(probe.get("resolved_executable", ""))).resolve(strict=False)
    try:
        executable.relative_to(prefix)
    except ValueError as exc:
        raise CampaignError(f"{role} executable escapes its environment prefix") from exc
    for name, item in modules.items():
        if not isinstance(item, Mapping) or set(item) != {"path", "version", "sha256"}:
            raise CampaignError(f"{role} module {name} identity has an invalid shape")
        if (
            not isinstance(item.get("sha256"), str)
            or re.fullmatch(r"[0-9a-f]{64}", item["sha256"]) is None
        ):
            raise CampaignError(f"{role} module {name} lacks a valid file digest")
        module_path = Path(str(item.get("path", ""))).resolve(strict=False)
        if _paths_overlap(module_path, ROOT):
            raise CampaignError(f"{role} imports {name} from the repository: {module_path}")
        try:
            module_path.relative_to(prefix)
        except ValueError as exc:
            raise CampaignError(
                f"{role} module {name} escapes interpreter prefix: {module_path}"
            ) from exc
    distribution_roots = probe.get("distribution_roots")
    expected_distributions = {name for name, version in versions.items() if version is not None}
    if (
        not isinstance(distribution_roots, Mapping)
        or set(distribution_roots) != expected_distributions
    ):
        raise CampaignError(f"{role} distribution roots have an invalid closed shape")
    for package, root_value in distribution_roots.items():
        distribution_root = Path(str(root_value)).resolve(strict=False)
        if _paths_overlap(distribution_root, ROOT):
            raise CampaignError(f"{role} distribution {package} comes from the repository")
        try:
            distribution_root.relative_to(prefix)
        except ValueError as exc:
            raise CampaignError(
                f"{role} distribution {package} escapes interpreter prefix"
            ) from exc
    sys_path = probe.get("sys_path")
    if not isinstance(sys_path, list) or not sys_path:
        raise CampaignError(f"{role} sys.path probe is invalid")
    for raw_path in sys_path:
        if not isinstance(raw_path, str):
            raise CampaignError(f"{role} sys.path contains a non-string entry")
        search_path = Path(raw_path).resolve(strict=False)
        if _paths_overlap(search_path, ROOT):
            raise CampaignError(f"{role} sys.path includes the repository: {search_path}")
        try:
            search_path.relative_to(prefix)
        except ValueError as exc:
            raise CampaignError(
                f"{role} sys.path escapes interpreter prefix: {search_path}"
            ) from exc
    pth_files = probe.get("pth_files")
    if not isinstance(pth_files, list):
        raise CampaignError(f"{role} .pth probe is invalid")
    for item in pth_files:
        if not isinstance(item, Mapping) or set(item) != {"path", "sha256", "active_lines"}:
            raise CampaignError(f"{role} .pth identity has an invalid shape")
        pth_path = Path(str(item["path"])).resolve(strict=False)
        try:
            pth_path.relative_to(prefix)
        except ValueError as exc:
            raise CampaignError(f"{role} .pth file escapes interpreter prefix") from exc
        if (
            not isinstance(item["sha256"], str)
            or re.fullmatch(r"[0-9a-f]{64}", item["sha256"]) is None
        ):
            raise CampaignError(f"{role} .pth file digest is invalid")
        active_lines = item["active_lines"]
        if not isinstance(active_lines, list) or any(
            not isinstance(line, str) for line in active_lines
        ):
            raise CampaignError(f"{role} .pth active lines are invalid")
        for line in active_lines:
            if str(ROOT) in urllib.parse.unquote(line):
                raise CampaignError(f"{role} .pth file references the repository")
            if line.startswith(("import ", "import\t")):
                continue
            injected = (pth_path.parent / line).resolve(strict=False)
            try:
                injected.relative_to(prefix)
            except ValueError as exc:
                raise CampaignError(f"{role} .pth file injects a path outside its prefix") from exc
    direct_urls = probe.get("direct_url")
    if not isinstance(direct_urls, Mapping) or set(direct_urls) != package_names:
        raise CampaignError(f"{role} direct-url probe has an invalid closed shape")
    for package, direct in direct_urls.items():
        if direct and '"editable": true' in direct.lower():
            raise CampaignError(f"{role} package {package} is an editable install")
        if direct and str(ROOT) in urllib.parse.unquote(direct):
            raise CampaignError(f"{role} package {package} references the repository")


def verify_interpreter_pin(expected: Mapping[str, Any], actual: Mapping[str, Any]) -> None:
    if canonical_json_bytes(expected) != canonical_json_bytes(actual):
        raise CampaignError(
            f"interpreter dependency probe drift for {expected.get('role', 'unknown role')}"
        )


def _verify_primary_install_matches_wheel(
    wheel: Mapping[str, Any],
    probe: Mapping[str, Any],
) -> None:
    direct_urls = probe.get("direct_url")
    raw = direct_urls.get("gffbase") if isinstance(direct_urls, Mapping) else None
    if not isinstance(raw, str):
        raise CampaignError("primary gffbase install lacks direct wheel provenance")
    try:
        provenance = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise CampaignError("primary gffbase direct_url.json is invalid") from exc
    if not isinstance(provenance, dict) or set(provenance) != {"archive_info", "url"}:
        raise CampaignError("primary gffbase direct wheel provenance has an invalid shape")
    archive = provenance["archive_info"]
    if not isinstance(archive, dict):
        raise CampaignError("primary gffbase direct wheel provenance lacks archive_info")
    hashes = archive.get("hashes")
    digest = hashes.get("sha256") if isinstance(hashes, Mapping) else None
    legacy_hash = archive.get("hash")
    if digest is None and isinstance(legacy_hash, str) and legacy_hash.startswith("sha256="):
        digest = legacy_hash.removeprefix("sha256=")
    if digest != wheel.get("sha256"):
        raise CampaignError("primary gffbase install does not match the candidate wheel digest")
    url = provenance["url"]
    if not isinstance(url, str):
        raise CampaignError("primary gffbase direct wheel provenance URL is invalid")
    installed_name = Path(urllib.parse.unquote(urllib.parse.urlparse(url).path)).name
    if installed_name != wheel.get("name"):
        raise CampaignError("primary gffbase install does not name the candidate wheel")


def _gzip_crc(path: Path) -> bool:
    try:
        with gzip.open(path, "rb") as handle:
            while handle.read(1 << 20):
                pass
    except (OSError, EOFError) as exc:
        raise CampaignError(f"gzip CRC/trailer verification failed for {path}: {exc}") from exc
    return True


def verify_transform_manifest(
    path: Path, output: Path, registry_entry: Mapping[str, Any]
) -> dict[str, Any]:
    from benchmarks.prepare_gtf_control import validate_parent_stripped_gtf

    source = corpus_path(dict(registry_entry))
    manifest = validate_parent_stripped_gtf(source, output, path)
    source_info = manifest["source"]
    output_info = manifest["output"]
    counts = manifest["counts"]
    if not all(isinstance(value, Mapping) for value in (source_info, output_info, counts)):
        raise CampaignError("validated parent-stripped manifest has invalid nested objects")
    if source_info["bytes"] != registry_entry["bytes"]:
        raise CampaignError("parent-stripped manifest source size mismatch")
    if source_info["sha256"] != registry_entry["sha256"]:
        raise CampaignError("parent-stripped manifest source checksum mismatch")
    return {
        "path": str(output_info["path"]),
        "bytes": output_info["bytes"],
        "sha256": output_info["sha256"],
        "gzip_crc_ok": True,
        "manifest_path": str(_campaign_safe_io.lexical_absolute(path)),
        "manifest_sha256": hashlib.sha256(
            _campaign_safe_io.canonical_storage_bytes(manifest)
        ).hexdigest(),
        "transform_version": "1",
        "counts": dict(counts),
    }


def verify_inputs(parent_stripped_path: Path, manifest_path: Path) -> dict[str, dict[str, Any]]:
    inputs: dict[str, dict[str, Any]] = {}
    for corpus in CORPORA:
        key = str(corpus["key"])
        verified = _campaign_preflight.verify_input_file(
            corpus_path(corpus),
            expected_size=int(corpus["bytes"]),
            expected_sha256=str(corpus["sha256"]),
        )
        verified["format"] = corpus["fmt"]
        inputs[key] = verified
    inputs["gencode-gtf-parent-stripped"] = verify_transform_manifest(
        manifest_path, parent_stripped_path, BY_KEY["gencode-gtf"]
    )
    inputs["gencode-gtf-parent-stripped"]["format"] = "gtf"
    return inputs


def _nearest_existing(path: Path) -> Path:
    path = path.resolve(strict=False)
    while not path.exists() and path.parent != path:
        path = path.parent
    return path


def probe_resources(campaign_root: Path) -> dict[str, Any]:
    campaign_root = Path(campaign_root).resolve(strict=False)
    probe_path = _nearest_existing(campaign_root)
    findmnt = shutil.which("findmnt")
    taskset = shutil.which("taskset")
    tmux = shutil.which("tmux")
    if not findmnt or not taskset or not tmux:
        raise CampaignError("cluster campaign requires findmnt, taskset, and tmux")

    def executable_version(path: str, *arguments: str) -> str:
        proc = subprocess.run(
            [path, *arguments],
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
            env={"PATH": os.defpath, "LANG": "C.UTF-8", "LC_ALL": "C.UTF-8"},
        )
        if proc.returncode:
            raise CampaignError(f"cannot identify executable {path}: {proc.stderr.strip()}")
        output = (proc.stdout or proc.stderr).strip().splitlines()
        if not output or len(output[0]) > 1000:
            raise CampaignError(f"executable returned invalid version output: {path}")
        return output[0]

    executable_paths = {
        "findmnt": str(Path(findmnt).resolve()),
        "taskset": str(Path(taskset).resolve()),
        "tmux": str(Path(tmux).resolve()),
    }
    executable_versions = {
        "findmnt": executable_version(findmnt, "--version"),
        "taskset": executable_version(taskset, "--version"),
        "tmux": executable_version(tmux, "-V"),
    }
    executable_identities: dict[str, dict[str, Any]] = {}
    for name, executable_path in executable_paths.items():
        path = _campaign_safe_io.inspect_path(executable_path, require_kind="file")
        item = os.lstat(path)
        executable_identities[name] = {
            "st_dev": item.st_dev,
            "st_ino": item.st_ino,
            "st_size": item.st_size,
            "st_mtime_ns": item.st_mtime_ns,
            "sha256": sha256_file(path),
        }
    mount_proc = subprocess.run(
        [findmnt, "-T", str(probe_path), "-n", "-o", "SOURCE,FSTYPE,TARGET,OPTIONS"],
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    if mount_proc.returncode:
        raise CampaignError(f"findmnt failed for {probe_path}: {mount_proc.stderr.strip()}")
    mount_fields = mount_proc.stdout.strip().split(maxsplit=3)
    fstype = mount_fields[1] if len(mount_fields) >= 2 else ""
    free_bytes = shutil.disk_usage(probe_path).free
    available_ram = 0
    try:
        for line in Path("/proc/meminfo").read_text(encoding="utf-8").splitlines():
            if line.startswith("MemAvailable:"):
                available_ram = int(line.split()[1]) * 1024
                break
    except OSError:
        pass
    try:
        affinity = sorted(os.sched_getaffinity(0))
    except (AttributeError, OSError) as exc:
        raise CampaignError("Linux sched_getaffinity is required") from exc
    try:
        online_text = Path("/sys/devices/system/cpu/online").read_text(encoding="ascii").strip()
        online_cpus = list(parse_cpu_list(online_text))
    except (OSError, UnicodeError, ValueError) as exc:
        raise CampaignError(f"cannot establish online CPU identity: {exc}") from exc

    physical: dict[int, tuple[str, str]] = {}
    for cpu in range(50):
        core = Path(f"/sys/devices/system/cpu/cpu{cpu}/topology/core_id")
        package = Path(f"/sys/devices/system/cpu/cpu{cpu}/topology/physical_package_id")
        try:
            physical[cpu] = (core.read_text().strip(), package.read_text().strip())
        except OSError:
            physical[cpu] = (str(cpu), "unknown")
    numa_nodes: dict[str, list[int]] = {}
    for node in sorted(Path("/sys/devices/system/node").glob("node[0-9]*")):
        try:
            cpulist = (node / "cpulist").read_text(encoding="ascii").strip()
            numa_nodes[node.name] = list(parse_cpu_list(cpulist))
        except (OSError, UnicodeError, ValueError) as exc:
            raise CampaignError(f"cannot establish NUMA identity for {node.name}: {exc}") from exc
    return {
        "platform": platform.system(),
        "machine": platform.machine(),
        "allowed_cpus": affinity,
        "online_cpus": online_cpus,
        "physical_cpu_ids": {str(key): list(value) for key, value in physical.items()},
        "numa_nodes": numa_nodes,
        "free_bytes": free_bytes,
        "available_ram_bytes": available_ram,
        "mount": {
            "raw": mount_proc.stdout.strip(),
            "fstype": fstype,
            "probe_path": str(probe_path),
        },
        "executables": executable_paths,
        "executable_versions": executable_versions,
        "executable_identities": executable_identities,
        "thresholds": {
            "free_bytes": MIN_FREE_BYTES,
            "available_ram_bytes": MIN_AVAILABLE_RAM_BYTES,
            "disk_formula": "5 * 14 GiB concurrent slots + 10 GiB margin",
            "ram_formula": "5 * 5.7 GiB observed peak + margin, rounded to 40 GiB",
        },
    }


def verify_topology(resource_probe: Mapping[str, Any]) -> None:
    if resource_probe.get("platform") != "Linux":
        raise CampaignError("cluster campaign requires Linux")
    allowed = set(resource_probe.get("allowed_cpus") or [])
    online = set(resource_probe.get("online_cpus") or [])
    required = set(range(50))
    if not required <= allowed:
        raise CampaignError(
            f"campaign requires allowed CPUs 0-49; missing {sorted(required - allowed)}"
        )
    if not required <= online:
        raise CampaignError(
            f"campaign requires online CPUs 0-49; missing {sorted(required - online)}"
        )
    physical = resource_probe.get("physical_cpu_ids") or {}
    identities = [tuple(physical.get(str(cpu), ())) for cpu in range(50)]
    if len(set(identities)) != 50:
        raise CampaignError("CPUs 0-49 are not 50 distinct physical cores")
    lanes = [set(parse_cpu_list(value)) for value in LANE_CPUS]
    if any(left & right for index, left in enumerate(lanes) for right in lanes[index + 1 :]):
        raise CampaignError("campaign CPU lanes overlap")
    fstype = str((resource_probe.get("mount") or {}).get("fstype", "")).lower()
    if not fstype.startswith("nfs"):
        raise CampaignError(f"campaign root must be NFS-backed, found {fstype or 'unknown'}")
    if int(resource_probe.get("free_bytes", 0)) < MIN_FREE_BYTES:
        raise CampaignError("campaign root has less than 80 GiB free")
    if int(resource_probe.get("available_ram_bytes", 0)) < MIN_AVAILABLE_RAM_BYTES:
        raise CampaignError("host has less than 40 GiB available RAM")
    if set(resource_probe.get("executables") or {}) != {"findmnt", "taskset", "tmux"}:
        raise CampaignError("resource probe lacks required executables")
    if set(resource_probe.get("executable_versions") or {}) != {"findmnt", "taskset", "tmux"}:
        raise CampaignError("resource probe lacks executable versions")
    if set(resource_probe.get("executable_identities") or {}) != {"findmnt", "taskset", "tmux"}:
        raise CampaignError("resource probe lacks executable identities")


def _verify_resource_stability(
    before: Mapping[str, Any],
    after: Mapping[str, Any],
) -> None:
    stable_keys = {
        "platform",
        "machine",
        "allowed_cpus",
        "online_cpus",
        "physical_cpu_ids",
        "numa_nodes",
        "mount",
        "executables",
        "executable_versions",
        "executable_identities",
        "thresholds",
    }
    for key in stable_keys:
        if canonical_json_bytes(before.get(key)) != canonical_json_bytes(after.get(key)):
            raise CampaignError(f"resource identity changed during preflight: {key}")


def _input_for(inputs: Mapping[str, Any], key: str) -> dict[str, Any]:
    try:
        value = dict(inputs[key])
    except KeyError as exc:
        raise CampaignError(f"missing verified input identity: {key}") from exc
    return value


def _job(
    *,
    job_id: str,
    phase: str,
    kind: str,
    worker_id: str,
    corpus_key: str,
    result_key: str,
    gtf_arm: str | None,
    engine_set: str,
    interpreter_role: str,
    threads: int,
    cpus: str,
    input_info: Mapping[str, Any],
    parameters: Mapping[str, Any],
    primary_eligible: bool = False,
) -> JobSpec:
    unsigned = {
        "job_id": job_id,
        "phase": phase,
        "kind": kind,
        "worker_id": worker_id,
        "corpus_key": corpus_key,
        "result_key": result_key,
        "gtf_arm": gtf_arm,
        "engine_set": engine_set,
        "interpreter_role": interpreter_role,
        "threads": threads,
        "cpus": cpus,
        "input": dict(input_info),
        "parameters": dict(parameters),
        "primary_eligible": primary_eligible,
    }
    return JobSpec(**unsigned, job_sha256=sha256_json(unsigned))


def build_job_matrix(
    config: Mapping[str, Any], identities: Mapping[str, Any], inputs: Mapping[str, Any]
) -> tuple[JobSpec, ...]:
    """Return the binding 25 scaling + 11 canonical/control/bridge jobs."""

    del identities  # identities live once in campaign spec; jobs name their role.
    parameters = {
        "legacy_timeout": int(config.get("legacy_timeout", 5400)),
        "gffbase_timeout": int(config.get("gffbase_timeout", 3600)),
        "validation_sample": int(config.get("validation_sample", 10_000)),
        "n_spatial": int(config.get("n_spatial", 5000)),
        "n_batched": int(config.get("n_batched", 5000)),
        "repeats": int(config.get("repeats", 1)),
        "region_seed": int(config.get("region_seed", REGION_SEED)),
    }
    if any(parameters[name] < 1 for name in parameters if name != "region_seed"):
        raise CampaignError("benchmark numeric parameters must be positive")
    jobs: list[JobSpec] = []
    for threads, cpus in zip(THREADS, LANE_CPUS, strict=True):
        worker = f"scale-t{threads:02d}"
        for key in CORPUS_ORDER:
            jobs.append(
                _job(
                    job_id=f"scaling-t{threads:02d}-{key}",
                    phase="exploratory",
                    kind="scaling",
                    worker_id=worker,
                    corpus_key=key,
                    result_key=key,
                    gtf_arm="no-infer" if key == "gencode-gtf" else None,
                    engine_set="candidate-only",
                    interpreter_role="primary",
                    threads=threads,
                    cpus=cpus,
                    input_info=_input_for(inputs, key),
                    parameters=parameters,
                )
            )

    for key in CORPUS_ORDER:
        jobs.append(
            _job(
                job_id=f"canonical-{key}",
                phase="canonical",
                kind="primary",
                worker_id="canonical",
                corpus_key=key,
                result_key=key,
                gtf_arm="no-infer" if key == "gencode-gtf" else None,
                engine_set="candidate-vs-gffutils-0.14",
                interpreter_role="primary",
                threads=10,
                cpus="0-9",
                input_info=_input_for(inputs, key),
                parameters=parameters,
                primary_eligible=True,
            )
        )
    for arm, input_key in (
        ("default", "gencode-gtf"),
        ("parent-stripped", "gencode-gtf-parent-stripped"),
    ):
        jobs.append(
            _job(
                job_id=f"control-gencode-gtf-{arm}",
                phase="canonical",
                kind="control",
                worker_id="canonical",
                corpus_key="gencode-gtf",
                result_key=f"gencode-gtf-{arm}",
                gtf_arm=arm,
                engine_set="candidate-vs-gffutils-0.14",
                interpreter_role="primary",
                threads=10,
                cpus="0-9",
                input_info=_input_for(inputs, input_key),
                parameters=parameters,
            )
        )
    for version, role, engine in (
        ("gffbase-0.1.0", "gffbase-0.1.0", "gffbase"),
        ("gffutils-0.13", "gffutils-0.13", "gffutils"),
    ):
        for key in ("mane", "chess"):
            jobs.append(
                _job(
                    job_id=f"bridge-{version}-{key}",
                    phase="canonical",
                    kind="bridge",
                    worker_id="canonical",
                    corpus_key=key,
                    result_key=key,
                    gtf_arm=None,
                    engine_set=version,
                    interpreter_role=role,
                    threads=10,
                    cpus="0-9",
                    input_info=_input_for(inputs, key),
                    parameters={**parameters, "bridge_engine": engine},
                )
            )
    _validate_matrix(jobs)
    return tuple(jobs)


def _validate_matrix(jobs: Sequence[JobSpec]) -> None:
    if len(jobs) != 36:
        raise CampaignError(f"job matrix must contain 36 jobs, found {len(jobs)}")
    ids = [job.job_id for job in jobs]
    digests = [job.job_sha256 for job in jobs]
    if len(set(ids)) != 36 or len(set(digests)) != 36:
        raise CampaignError("job IDs and digests must be unique")
    counts = {
        kind: sum(job.kind == kind for job in jobs)
        for kind in ("scaling", "primary", "control", "bridge")
    }
    if counts != {"scaling": 25, "primary": 5, "control": 2, "bridge": 4}:
        raise CampaignError(f"invalid job-kind matrix: {counts}")
    if sum(job.phase == "exploratory" for job in jobs) != 25:
        raise CampaignError("exploratory matrix must contain exactly 25 jobs")
    for job in jobs:
        validate_identifier(job.job_id, "job id")
        if job.primary_eligible != (job.kind == "primary"):
            raise CampaignError(f"invalid primary eligibility: {job.job_id}")
        if set(parse_cpu_list(job.cpus)) - set(range(50)):
            raise CampaignError(f"job has CPUs outside 0-49: {job.job_id}")


def job_digest(job: JobSpec | Mapping[str, Any]) -> str:
    value = job.to_dict() if isinstance(job, JobSpec) else dict(job)
    expected = value.pop("job_sha256", None)
    digest = sha256_json(value)
    if expected and expected != digest:
        raise CampaignError(f"job digest mismatch for {value.get('job_id')}")
    return digest


def _campaign_spec(campaign: Mapping[str, Any]) -> Mapping[str, Any]:
    return campaign.get("spec") or {}


def _campaign_interpreter(campaign: Mapping[str, Any], role: str) -> str:
    probe = (_campaign_spec(campaign).get("interpreters") or {}).get(role) or {}
    executable = probe.get("resolved_executable")
    if not executable:
        raise CampaignError(f"campaign lacks interpreter for role {role}")
    return str(executable)


def build_mega_argv(
    job: JobSpec | Mapping[str, Any], attempt_dir: Path, campaign: Mapping[str, Any]
) -> list[str]:
    job = job if isinstance(job, JobSpec) else JobSpec.from_dict(job)
    if job.kind == "bridge":
        raise CampaignError("bridge jobs do not use 06_mega.py")
    args = [
        _campaign_interpreter(campaign, "primary"),
        "-I",
        str(ROOT / "benchmarks" / "06_mega.py"),
        "--only",
        job.corpus_key,
        "--threads",
        str(job.threads),
        "--gtf-arm",
        job.gtf_arm or "no-infer",
        "--legacy-timeout",
        str(job.parameters["legacy_timeout"]),
        "--gffbase-timeout",
        str(job.parameters["gffbase_timeout"]),
        "--validation-sample",
        str(job.parameters["validation_sample"]),
        "--n-spatial",
        str(job.parameters["n_spatial"]),
        "--n-batched",
        str(job.parameters["n_batched"]),
        "--repeats",
        str(job.parameters["repeats"]),
    ]
    if job.kind == "scaling":
        args.append("--skip-legacy")
    if job.gtf_arm == "parent-stripped":
        args.extend(["--gtf-input", str(Path(str(job.input["path"])).resolve())])
    forbidden = {"--publish", "--rederive", "--keep-db", "--no-purge"}
    if forbidden & set(args):  # defensive assertion against later edits
        raise CampaignError("worker argv contains a forbidden legacy harness flag")
    return args


def build_bridge_argv(
    job: JobSpec | Mapping[str, Any], attempt_dir: Path, campaign: Mapping[str, Any]
) -> list[str]:
    job = job if isinstance(job, JobSpec) else JobSpec.from_dict(job)
    if job.kind != "bridge":
        raise CampaignError("only bridge jobs use bridge.py")
    return [
        _campaign_interpreter(campaign, job.interpreter_role),
        "-I",
        str(ROOT / "benchmarks" / "bridge.py"),
        "--engine",
        str(job.parameters["bridge_engine"]),
        "--input",
        str(Path(str(job.input["path"])).resolve()),
        "--database",
        str(Path(attempt_dir).resolve() / "scratch" / "bridge.duckdb"),
        "--output",
        str(Path(attempt_dir).resolve() / "raw.json"),
        "--format",
        str(job.input.get("format", "gff3")),
        "--threads",
        str(job.threads),
        "--label",
        job.job_id.removeprefix("bridge-"),
    ]


def campaign_digest(campaign: Mapping[str, Any]) -> str:
    spec = campaign.get("spec")
    digest = campaign.get("spec_sha256")
    if not isinstance(spec, dict) or digest != sha256_json(spec):
        raise CampaignError("campaign spec digest mismatch")
    return str(digest)


def build_worker_argv(
    campaign: Mapping[str, Any], worker_id: str, *, resume: bool = False
) -> list[str]:
    validate_identifier(worker_id, "worker id")
    campaign_path = Path(str(campaign.get("_path", ""))).resolve()
    if not campaign_path.is_file():
        raise CampaignError("campaign object lacks its campaign.json path")
    args = [
        _campaign_interpreter(campaign, "primary"),
        "-I",
        str(ROOT / "benchmarks" / "cluster_campaign.py"),
        "_worker",
        "--campaign",
        str(campaign_path),
        "--worker-id",
        worker_id,
        "--campaign-sha256",
        campaign_digest(campaign),
    ]
    if resume:
        args.append("--resume")
    return args


def build_tmux_argv(
    session: str, cpu_list: str, worker_argv: Sequence[str], *, cwd: Path = ROOT
) -> list[str]:
    validate_identifier(session, "tmux session")
    if len(session) > 80:
        raise CampaignError("tmux session name exceeds 80 characters")
    parse_cpu_list(cpu_list)
    tmux = shutil.which("tmux") or "tmux"
    taskset = shutil.which("taskset") or "taskset"
    shell_command = shlex.join([taskset, "--cpu-list", cpu_list, *worker_argv])
    return [tmux, "new-session", "-d", "-s", session, "-c", str(Path(cwd).resolve()), shell_command]


def worker_environment(
    campaign: Mapping[str, Any], job: JobSpec, attempt_dir: Path
) -> dict[str, str]:
    env = {key: value for key, value in os.environ.items() if key != "PYTHONPATH"}
    env.update(benchmark_env(job.threads))
    env.update(
        {
            "GFFBASE_BENCH_OUT": str(Path(attempt_dir).resolve() / "scratch"),
            "GFFBASE_BENCH_WHEEL": str(
                ((_campaign_spec(campaign).get("candidate") or {}).get("wheel") or {}).get("name")
            ),
            "GFFBASE_BENCH_WHEEL_SHA256": str(
                ((_campaign_spec(campaign).get("candidate") or {}).get("wheel") or {}).get("sha256")
            ),
        }
    )
    return env


def _load_campaign(path: Path) -> dict[str, Any]:
    return dict(
        _campaign_preflight.load_completed_campaign(
            path,
            runtime_validator=_campaign_worker.validate_runtime_tree,
        )
    )


def _load_worker_campaign(
    path: Path,
    *,
    timeout_seconds: int | float = 30.0,
    monotonic: Callable[[], float] = time.monotonic,
    sleeper: Callable[[float], None] = time.sleep,
) -> dict[str, Any]:
    """Retry a worker's read-only load across brief parallel runtime writes."""

    if (
        type(timeout_seconds) not in {int, float}
        or isinstance(timeout_seconds, bool)
        or timeout_seconds <= 0
    ):
        raise CampaignError("worker campaign-load timeout must be positive")
    if not callable(monotonic) or not callable(sleeper):
        raise CampaignError("worker campaign-load clock providers must be callable")
    started = monotonic()
    while True:
        try:
            return _load_campaign(path)
        except CampaignError as exc:
            elapsed = monotonic() - started
            if elapsed >= float(timeout_seconds):
                raise CampaignError(
                    f"worker campaign remained unsafe after bounded retry: {exc}"
                ) from exc
            sleeper(min(0.1, float(timeout_seconds) - elapsed))


def _jobs(campaign: Mapping[str, Any]) -> tuple[JobSpec, ...]:
    return tuple(JobSpec.from_dict(value) for value in (_campaign_spec(campaign).get("jobs") or []))


def _campaign_dir(campaign: Mapping[str, Any]) -> Path:
    return Path(str(campaign["_path"])).resolve().parent


def _session_name(campaign: Mapping[str, Any]) -> str:
    return f"gffbase-{campaign_digest(campaign)[:12]}-cluster"


def _status_path(campaign: Mapping[str, Any], job: JobSpec) -> Path:
    return _campaign_dir(campaign) / "jobs" / job.job_id / "status.json"


def read_job_state(campaign: Mapping[str, Any], job: JobSpec) -> dict[str, Any]:
    return dict(
        _campaign_worker.read_status(
            _campaign_dir(campaign),
            run_id=str(campaign["run_id"]),
            campaign_sha256=campaign_digest(campaign),
            job=job,
        )
    )


def classify_stale_running(
    state: Mapping[str, Any], tmux_sessions: set[str], host: str, now: float | None = None
) -> str:
    del tmux_sessions, host, now
    if state.get("state") != "running":
        return str(state.get("state", "pending"))
    raise CampaignError(
        "session-only liveness is forbidden; use exact pane and process observations"
    )


def next_attempt(job_dir: Path) -> int:
    attempts = Path(job_dir) / "attempts"
    if not attempts.exists():
        return 1
    numbers = []
    for path in attempts.iterdir():
        if path.is_dir() and re.fullmatch(r"[0-9]{4}", path.name):
            numbers.append(int(path.name))
    return max(numbers, default=0) + 1


def _load_json(path: Path) -> dict[str, Any]:
    if not path.is_file() or path.is_symlink():
        raise CampaignError(f"missing or unsafe JSON: {path}")
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise CampaignError(f"invalid JSON {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise CampaignError(f"JSON object required: {path}")
    return value


def _write_status(
    campaign: Mapping[str, Any], job: JobSpec, *, previous: Mapping[str, Any] | None, **updates: Any
) -> dict[str, Any]:
    previous_state = (previous or {}).get("state", "pending")
    new_state = updates["state"]
    allowed = {
        "pending": {"running"},
        "running": {"succeeded", "failed", "timed_out", "interrupted"},
        "failed": {"running"},
        "timed_out": {"running"},
        "interrupted": {"running"},
        "succeeded": set(),
    }
    if new_state not in allowed.get(str(previous_state), set()):
        raise CampaignError(
            f"invalid state transition for {job.job_id}: {previous_state} -> {new_state}"
        )
    value = {
        "run_id": campaign["run_id"],
        "campaign_sha256": campaign_digest(campaign),
        "job_id": job.job_id,
        "job_sha256": job.job_sha256,
        **updates,
    }
    atomic_write_json_durable(_status_path(campaign, job), value)
    return value


def _terminate_process(proc: subprocess.Popen[bytes]) -> None:
    with contextlib.suppress(ProcessLookupError):
        os.killpg(proc.pid, signal.SIGTERM)
    try:
        proc.wait(timeout=15)
    except subprocess.TimeoutExpired:
        with contextlib.suppress(ProcessLookupError):
            os.killpg(proc.pid, signal.SIGKILL)
        proc.wait()


def run_job(
    campaign: Mapping[str, Any],
    job: JobSpec,
    attempt: int,
    *,
    launch: Mapping[str, object],
    worker_identity: _campaign_worker.ProcessIdentity,
) -> dict[str, Any]:
    """Run one immutable attempt under a total catchable-failure boundary."""

    campaign_dir = _campaign_dir(campaign)
    digest = campaign_digest(campaign)
    launch_number = launch.get("launch")
    if type(launch_number) is not int:
        raise CampaignError("worker launch evidence lacks its launch number")
    validated_launch = _campaign_worker.validate_launch_evidence(
        launch,
        run_id=str(campaign["run_id"]),
        campaign_sha256=digest,
        worker_id=job.worker_id,
        launch=launch_number,
    )
    pane = _campaign_worker.PaneIdentity.from_dict(validated_launch["pane"])
    if worker_identity.pid != pane.pane_pid:
        raise CampaignError("worker process identity differs from its launch pane")

    prior = read_job_state(campaign, job)
    attempt_dir = _campaign_worker.allocate_attempt(campaign_dir, job)
    if attempt_dir.name != f"{attempt:04d}":
        raise CampaignError(
            f"allocated attempt {attempt_dir.name!r} differs from requested {attempt:04d}"
        )
    stdout_path = attempt_dir / "stdout.log"
    stderr_path = attempt_dir / "stderr.log"
    relative_attempt = attempt_dir.relative_to(campaign_dir).as_posix()
    paths = {
        "attempt_dir": relative_attempt,
        "stdout_log": f"{relative_attempt}/stdout.log",
        "stderr_log": f"{relative_attempt}/stderr.log",
        "result": f"{relative_attempt}/result.json",
    }
    running: dict[str, object] = {
        "schema_version": STATUS_SCHEMA,
        "run_id": campaign["run_id"],
        "campaign_sha256": digest,
        "job_id": job.job_id,
        "job_sha256": job.job_sha256,
        "worker_id": job.worker_id,
        "state": "running",
        "attempt": attempt,
        "started_utc": utc_now(),
        "host": socket.gethostname(),
        "cpu_affinity": list(parse_cpu_list(job.cpus)),
        "pane": pane.to_dict(),
        "worker": worker_identity.to_dict(),
        "child": None,
        "paths": paths,
    }
    running = _campaign_worker.write_status(
        campaign_dir,
        running,
        previous=prior,
        run_id=str(campaign["run_id"]),
        campaign_sha256=digest,
        job=job,
    )

    actual_probe: dict[str, Any] | None = None
    actual_affinity: list[int] | None = None
    argv: list[str] = []
    env: dict[str, str] = {}
    failures: list[str] = []
    payload: dict[str, Any] = {}
    harness_environment: dict[str, Any] = {}
    outcome: _campaign_worker.ChildOutcome
    try:
        pinned = (_campaign_spec(campaign).get("interpreters") or {}).get(job.interpreter_role)
        actual_probe = probe_interpreter(
            Path(_campaign_interpreter(campaign, job.interpreter_role)),
            job.interpreter_role,
        )
        verify_interpreter_pin(pinned, actual_probe)
        actual_affinity = sorted(os.sched_getaffinity(0))
        if actual_affinity != list(parse_cpu_list(job.cpus)):
            raise CampaignError(
                f"worker affinity drift: expected {job.cpus}, found {actual_affinity!r}"
            )
        argv = (
            build_bridge_argv(job, attempt_dir, campaign)
            if job.kind == "bridge"
            else build_mega_argv(job, attempt_dir, campaign)
        )
        env = worker_environment(campaign, job, attempt_dir)
        timeout = int(job.parameters.get("cap_seconds", job.parameters.get("gffbase_timeout", 0)))
        if job.kind != "bridge":
            timeout += 0 if job.kind == "scaling" else int(job.parameters["legacy_timeout"])
            timeout += 3600

        def record_child(identity: _campaign_worker.ProcessIdentity) -> None:
            nonlocal running
            updated = dict(running)
            updated["child"] = identity.to_dict()
            running = _campaign_worker.write_status(
                campaign_dir,
                updated,
                previous=running,
                run_id=str(campaign["run_id"]),
                campaign_sha256=digest,
                job=job,
            )

        outcome = _campaign_worker.execute_child(
            argv,
            cwd=ROOT,
            environment=env,
            stdout_path=stdout_path,
            stderr_path=stderr_path,
            timeout_seconds=timeout,
            child_started=record_child,
        )
    except BaseException as exc:
        now = utc_now()
        outcome = _campaign_worker.ChildOutcome(
            state="interrupted" if isinstance(exc, KeyboardInterrupt) else "failed",
            started_utc=cast(str, running["started_utc"]),
            finished_utc=now,
            child=None,
            exit_code=None,
            error=f"{type(exc).__name__}: {str(exc).replace(chr(10), ' ')[:1900]}",
            signal_number=signal.SIGINT if isinstance(exc, KeyboardInterrupt) else None,
        )

    if outcome.error is not None:
        failures.append(outcome.error)
    if outcome.state == "succeeded":
        try:
            if job.kind == "bridge":
                payload = _load_json(attempt_dir / "raw.json")
            else:
                raw = _load_json(attempt_dir / "scratch" / "06_mega.json")
                raw = _campaign_model.require_exact_keys(
                    raw,
                    {"schema_version", "environment", "corpora"},
                    f"{job.job_id} raw 06_mega payload",
                )
                _campaign_model.require_schema(raw, "3", f"{job.job_id} raw 06_mega payload")
                corpora = _campaign_model.require_exact_keys(
                    raw["corpora"], {job.result_key}, f"{job.job_id} raw corpora"
                )
                if not isinstance(corpora[job.result_key], Mapping):
                    raise CampaignError(f"{job.job_id}: raw benchmark row must be an object")
                payload = dict(corpora[job.result_key])
                if not isinstance(raw["environment"], Mapping):
                    raise CampaignError(f"{job.job_id}: raw harness environment must be an object")
                harness_environment = dict(raw["environment"])
        except BaseException as exc:
            failures.append(f"{type(exc).__name__}: {str(exc).replace(chr(10), ' ')[:1900]}")

    if outcome.state in {"timed_out", "interrupted"}:
        state = outcome.state
    else:
        state = "succeeded" if outcome.state == "succeeded" and not failures else "failed"
    result = {
        "schema_version": WORKER_SCHEMA,
        "run_id": campaign["run_id"],
        "campaign_sha256": digest,
        "job_id": job.job_id,
        "job_sha256": job.job_sha256,
        "attempt": attempt,
        "attempt_dir": str(attempt_dir),
        "state": state,
        "started_utc": running["started_utc"],
        "finished_utc": outcome.finished_utc,
        "host": running["host"],
        "pid": outcome.child.pid if outcome.child is not None else None,
        "pane": pane.to_dict(),
        "worker_process": worker_identity.to_dict(),
        "child_process": outcome.child.to_dict() if outcome.child is not None else None,
        "cpu_affinity": actual_affinity,
        "interpreter_probe": actual_probe,
        "argv": argv,
        "environment": {key: env[key] for key in benchmark_env(job.threads) if key in env},
        "harness_environment": harness_environment,
        "exit_code": outcome.exit_code,
        "error": outcome.error,
        "signal_number": outcome.signal_number,
        "stdout_log": paths["stdout_log"],
        "stderr_log": paths["stderr_log"],
        "payload": payload,
        "validation": {"accepted": state == "succeeded" and not failures, "failures": failures},
    }
    if state == "succeeded":
        try:
            _campaign_results.validate_worker_result(campaign, job, result)
        except BaseException as exc:
            failures.append(f"{type(exc).__name__}: {str(exc).replace(chr(10), ' ')[:1900]}")
            state = "failed"
            payload = {}
            harness_environment = {}
            result.update(
                {
                    "state": state,
                    "payload": payload,
                    "harness_environment": harness_environment,
                    "validation": {"accepted": False, "failures": failures},
                }
            )
    _campaign_results.validate_terminal_worker_result(campaign, job, result)
    result_path = attempt_dir / "result.json"
    atomic_create_json(result_path, result)
    terminal = {
        **running,
        "state": state,
        "finished_utc": result["finished_utc"],
        "result_sha256": _campaign_safe_io.sha256_regular_file(result_path, require_unique=True),
    }
    _campaign_worker.write_status(
        campaign_dir,
        terminal,
        previous=running,
        run_id=str(campaign["run_id"]),
        campaign_sha256=digest,
        job=job,
    )
    return result


def _accepted_results(campaign: Mapping[str, Any]) -> list[dict[str, Any]]:
    accepted: list[dict[str, Any]] = []
    for job in _jobs(campaign):
        status = read_job_state(campaign, job)
        inventory = _scan_job_attempts(campaign, job)
        if len(inventory.successes) > 1:
            raise CampaignError(
                f"{job.job_id}: expected at most one accepted attempt, "
                f"found {len(inventory.successes)}"
            )
        if status.get("state") != "succeeded":
            if inventory.successes:
                raise CampaignError(
                    f"{job.job_id}: immutable success exists without a promoted succeeded status"
                )
            continue
        if len(inventory.successes) != 1:
            raise CampaignError(
                f"{job.job_id}: succeeded status requires exactly one immutable success, "
                f"found {len(inventory.successes)}"
            )
        success = inventory.successes[0]
        result = validate_worker_result(campaign, job, success.result)
        expected_status = _terminal_status_from_attempt_result(
            campaign,
            job,
            result,
            result_sha256=success.result_sha256,
        )
        if canonical_json_bytes(status) != canonical_json_bytes(expected_status):
            raise CampaignError(f"{job.job_id}: succeeded status/result linkage differs")
        accepted.append(result)
    return accepted


def _tmux_sessions() -> set[str]:
    tmux = shutil.which("tmux")
    if not tmux:
        raise CampaignError("tmux is required to inspect campaign session names")
    proc = subprocess.run(
        [tmux, "list-sessions", "-F", "#{session_name}"],
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    if proc.returncode:
        message = proc.stderr.strip().casefold()
        if "no server running" in message or "failed to connect to server" in message:
            return set()
        raise CampaignError(f"cannot inspect tmux sessions: {proc.stderr.strip()}")
    return {line.strip() for line in proc.stdout.splitlines() if line.strip()}


def _cheap_revalidate(campaign: Mapping[str, Any]) -> None:
    spec = _campaign_spec(campaign)
    current_git = git_identity(ROOT)
    recorded_git = spec.get("repo") or {}
    if current_git.get("commit") != recorded_git.get("commit") or current_git.get("dirty"):
        raise CampaignError("repository identity changed or worktree is dirty")
    actual_wheel = wheel_identity(
        Path(str((spec.get("candidate") or {}).get("wheel", {}).get("path")))
    )
    if canonical_json_bytes(actual_wheel) != canonical_json_bytes(
        (spec.get("candidate") or {}).get("wheel")
    ):
        raise CampaignError("candidate wheel identity changed")
    for role, expected in (spec.get("interpreters") or {}).items():
        actual = probe_interpreter(Path(str(expected["resolved_executable"])), role)
        verify_interpreter_pin(expected, actual)
    recorded_inputs = spec.get("inputs") or {}
    for key, expected in recorded_inputs.items():
        path = Path(str(expected["path"]))
        actual = _campaign_preflight.verify_input_file(
            path,
            expected_size=int(expected["bytes"]),
            expected_sha256=str(expected["sha256"]),
        )
        actual["format"] = expected["format"]
        expected_core = {
            name: expected[name] for name in ("path", "bytes", "sha256", "gzip_crc_ok", "format")
        }
        if canonical_json_bytes(actual) != canonical_json_bytes(expected_core):
            raise CampaignError(f"input identity changed: {key}")
        if key == "gencode-gtf-parent-stripped":
            manifest_path = Path(str(expected["manifest_path"]))
            manifest_sha256 = _campaign_safe_io.sha256_regular_file(
                manifest_path,
                require_unique=True,
            )
            if manifest_sha256 != expected["manifest_sha256"]:
                raise CampaignError("parent-stripped transform manifest identity changed")
    resources = probe_resources(_campaign_dir(campaign).parent)
    verify_topology(resources)


def _placeholder_inputs(run_dir: Path) -> dict[str, dict[str, Any]]:
    values = {
        str(corpus["key"]): {
            "path": str(corpus_path(corpus).resolve()),
            "bytes": int(corpus["bytes"]),
            "sha256": str(corpus["sha256"]),
            "gzip_crc_ok": True,
            "format": corpus["fmt"],
        }
        for corpus in CORPORA
    }
    values["gencode-gtf-parent-stripped"] = {
        "path": str((run_dir / "inputs" / "parent-stripped.gtf.gz").resolve()),
        "bytes": 1,
        "sha256": "0" * 64,
        "gzip_crc_ok": True,
        "format": "gtf",
        "manifest_path": str((run_dir / "inputs" / "parent-stripped.manifest.json").resolve()),
        "manifest_sha256": "0" * 64,
        "transform_version": "1",
        "counts": {
            "input_feature_lines": 3,
            "output_feature_lines": 1,
            "comment_or_blank_lines": 0,
            "removed_gene_rows": 1,
            "removed_transcript_rows": 1,
        },
    }
    return values


def _config_from_args(args: argparse.Namespace) -> dict[str, Any]:
    return {
        "legacy_timeout": args.legacy_timeout,
        "gffbase_timeout": args.gffbase_timeout,
        "validation_sample": args.validation_sample,
        "n_spatial": args.n_spatial,
        "n_batched": args.n_batched,
        "repeats": args.repeats,
        "region_seed": REGION_SEED,
    }


def _require_ignored_output(root: Path) -> None:
    try:
        root.relative_to(ROOT)
    except ValueError:
        return
    proc = subprocess.run(
        ["git", "-C", str(ROOT), "check-ignore", "-q", str(root)],
        capture_output=True,
        timeout=30,
        check=False,
    )
    if proc.returncode != 0:
        raise CampaignError(f"campaign output must be gitignored: {root}")


def cmd_preflight(args: argparse.Namespace) -> int:
    if args.prepare_parent_stripped or (
        args.parent_stripped is None and args.parent_stripped_manifest is None
    ):
        transform_mode = "prepare"
    else:
        transform_mode = "external"
    request = _campaign_preflight.normalize_request(
        run_id=args.run_id,
        campaign_root=args.campaign_root,
        candidate_wheel=args.candidate_wheel,
        interpreters={
            "primary": args.primary_python,
            "gffbase-0.1.0": args.gffbase_010_python,
            "gffutils-0.13": args.gffutils_013_python,
        },
        transform_mode=transform_mode,
        parent_stripped=args.parent_stripped,
        parent_stripped_manifest=args.parent_stripped_manifest,
        parameters=_config_from_args(args),
    )
    run_id = str(request["run_id"])
    campaign_root = Path(str(request["campaign_root"]))
    run_dir = Path(str(request["run_dir"]))
    if not args.execute:
        jobs = build_job_matrix(_config_from_args(args), {}, _placeholder_inputs(run_dir))
        preview = {
            "mode": "dry-run",
            "writes": [],
            "run_id": run_id,
            "campaign": str(run_dir / "campaign.json"),
            "request": request,
            "request_sha256": sha256_json(request),
            "session": f"gffbase-{'<spec-sha>'}-cluster",
            "topology": topology(),
            "jobs": [job.to_dict() for job in jobs],
            "execute_required": True,
        }
        print(json.dumps(preview, indent=2))
        return 0

    campaign_root = validate_campaign_root(campaign_root)
    run_dir = resolve_beneath(campaign_root, run_dir)
    _require_ignored_output(campaign_root)
    existing_campaign = run_dir / "campaign.json"
    if os.path.lexists(run_dir):
        campaign = _campaign_preflight.load_completed_campaign_for_request(
            existing_campaign,
            request,
        )
        _cheap_revalidate(campaign)
        print(json.dumps({"campaign": str(existing_campaign), "idempotent": True}))
        return 0

    _campaign_preflight.ensure_campaign_root(campaign_root)
    run_dir = _campaign_preflight.create_private_run_directory(campaign_root, run_id)

    repo_before = git_identity(ROOT)
    if repo_before["dirty"]:
        raise CampaignError("executed preflight requires a clean worktree and clean submodules")
    wheel_before = wheel_identity(Path(str(request["candidate_wheel"])))
    interpreters_before = {
        "primary": probe_interpreter(args.primary_python, "primary"),
        "gffbase-0.1.0": probe_interpreter(args.gffbase_010_python, "gffbase-0.1.0"),
        "gffutils-0.13": probe_interpreter(args.gffutils_013_python, "gffutils-0.13"),
    }
    primary_version = (interpreters_before["primary"].get("packages") or {}).get("gffbase")
    if primary_version != wheel_before["metadata_version"]:
        raise CampaignError("installed primary package does not match candidate wheel metadata")
    _verify_primary_install_matches_wheel(wheel_before, interpreters_before["primary"])
    resources_before = probe_resources(run_dir)
    verify_topology(resources_before)

    if transform_mode == "prepare":
        input_dir = _campaign_preflight.create_private_run_directory(run_dir, "inputs")
        parent_path = input_dir / "parent-stripped.gtf.gz"
        manifest_path = input_dir / "parent-stripped.manifest.json"
        from benchmarks.prepare_gtf_control import build_parent_stripped_gtf

        build_parent_stripped_gtf(
            corpus_path(BY_KEY["gencode-gtf"]), parent_path, manifest_path, force=False
        )
    else:
        transform_request = dict(request["transform"])
        parent_path = Path(str(transform_request["output"]))
        manifest_path = Path(str(transform_request["manifest"]))

    def collect_inputs() -> tuple[dict[str, dict[str, Any]], dict[str, Any]]:
        raw_inputs: dict[str, dict[str, Any]] = {}
        for corpus in CORPORA:
            verified = _campaign_preflight.verify_input_file(
                corpus_path(corpus),
                expected_size=int(corpus["bytes"]),
                expected_sha256=str(corpus["sha256"]),
            )
            verified["format"] = corpus["fmt"]
            raw_inputs[str(corpus["key"])] = verified
        derived = verify_transform_manifest(manifest_path, parent_path, BY_KEY["gencode-gtf"])
        derived["format"] = "gtf"
        return {**raw_inputs, "gencode-gtf-parent-stripped": derived}, derived

    inputs_before, transform_before = collect_inputs()
    config = _config_from_args(args)
    jobs = build_job_matrix(config, interpreters_before, inputs_before)
    future_paths = _campaign_preflight.campaign_future_paths(request, jobs)
    filesystem_evidence = _campaign_preflight.qualify_mutable_filesystems({run_dir: future_paths})
    candidate = {
        "public_version": PUBLIC_VERSION,
        "cargo_version": CARGO_VERSION,
        "git_commit": repo_before["commit"],
        "wheel": wheel_before,
    }
    spec_resources = {**resources_before, "filesystems": filesystem_evidence}
    spec = {
        "repo": repo_before,
        "candidate": candidate,
        "interpreters": interpreters_before,
        "inputs": inputs_before,
        "topology": topology(),
        "parameters": config,
        "resources": spec_resources,
        "jobs": [job.to_dict() for job in jobs],
    }
    campaign = {
        "schema_version": CAMPAIGN_SCHEMA,
        "run_id": run_id,
        "created_utc": utc_now(),
        "spec_sha256": sha256_json(spec),
        "spec": spec,
    }
    session_names = [
        f"gffbase-{campaign['spec_sha256'][:12]}-cluster",
        f"gffbase-{campaign['spec_sha256'][:12]}-cluster-canonical",
    ]
    sessions_before = _tmux_sessions()
    collisions = sorted(set(session_names) & sessions_before)
    if collisions:
        raise CampaignError(f"tmux session names already exist: {collisions}")

    wheel_after = wheel_identity(Path(str(request["candidate_wheel"])))
    if canonical_json_bytes(wheel_after) != canonical_json_bytes(wheel_before):
        raise CampaignError("candidate wheel identity changed during preflight")
    interpreters_after = {
        role: probe_interpreter(Path(str(request["interpreters"][role])), role)
        for role in _campaign_model.INTERPRETER_ROLES
    }
    for role in _campaign_model.INTERPRETER_ROLES:
        verify_interpreter_pin(interpreters_before[role], interpreters_after[role])
    inputs_after, transform_after = collect_inputs()
    if canonical_json_bytes(inputs_after) != canonical_json_bytes(inputs_before):
        raise CampaignError("input or transform identity changed during preflight")
    resources_after = probe_resources(run_dir)
    verify_topology(resources_after)
    _verify_resource_stability(resources_before, resources_after)
    repo_after = git_identity(ROOT)
    if canonical_json_bytes(repo_after) != canonical_json_bytes(repo_before):
        raise CampaignError("repository identity changed during preflight")
    sessions_after = _tmux_sessions()
    if set(session_names) & sessions_after:
        raise CampaignError("tmux session collision appeared during preflight")

    evidence = {
        "repo_before": repo_before,
        "repo_after": repo_after,
        "candidate": {"before": wheel_before, "after": wheel_after},
        "interpreters": {
            role: {"before": interpreters_before[role], "after": interpreters_after[role]}
            for role in _campaign_model.INTERPRETER_ROLES
        },
        "inputs": {"before": inputs_before, "after": inputs_after},
        "transform": {"before": transform_before, "after": transform_after},
        "resources": {"before": resources_before, "after": resources_after},
        "filesystems": filesystem_evidence,
        "topology": topology(),
        "matrix": {
            "job_count": len(jobs),
            "jobs_sha256": sha256_json([job.to_dict() for job in jobs]),
        },
        "session": {
            "names": session_names,
            "before": sorted(sessions_before),
            "after": sorted(sessions_after),
        },
    }
    preflight_document = _campaign_preflight.build_preflight_document(
        request=request,
        campaign=campaign,
        evidence=evidence,
        verified_utc=utc_now(),
    )
    _campaign_preflight.commit_campaign_bundle(
        run_dir,
        campaign,
        preflight_document,
        phase_validator=lambda phase: _campaign_preflight.validate_run_tree(
            run_dir,
            transform_mode=transform_mode,
            phase=phase,
        ),
    )
    _campaign_preflight.load_completed_campaign_for_request(run_dir / "campaign.json", request)
    print(
        json.dumps(
            {
                "campaign": str(run_dir / "campaign.json"),
                "campaign_sha256": campaign["spec_sha256"],
                "jobs": len(jobs),
            }
        )
    )
    return 0


def _result_for_status(campaign: Mapping[str, Any], job: JobSpec, state: Mapping[str, Any]) -> bool:
    paths = state.get("paths")
    if state.get("state") != "succeeded" or not isinstance(paths, Mapping):
        return False
    relative = paths.get("result")
    if type(relative) is not str:
        return False
    path = _campaign_safe_io.resolve_beneath(_campaign_dir(campaign), relative, must_exist=True)
    if _campaign_safe_io.sha256_regular_file(path, require_unique=True) != state.get(
        "result_sha256"
    ):
        raise CampaignError(f"status result digest mismatch: {job.job_id}")
    result = load_schema(path, WORKER_SCHEMA)
    validate_worker_result(campaign, job, result)
    return True


def _worker_commands(
    campaign: Mapping[str, Any], worker_ids: Sequence[str], *, resume: bool
) -> list[list[str]]:
    session = _session_name(campaign)
    topo = _campaign_spec(campaign)["topology"]
    lane_by_worker = {lane["worker_id"]: lane for lane in topo["lanes"]}
    lane_by_worker["canonical"] = topo["canonical"]
    resources = _campaign_spec(campaign)["resources"]
    executables = resources["executables"]
    tmux_path = str(executables["tmux"])
    taskset_path = str(executables["taskset"])
    commands: list[list[str]] = []
    for index, worker_id in enumerate(worker_ids):
        worker = build_worker_argv(campaign, worker_id, resume=resume)
        cpus = lane_by_worker[worker_id]["cpus"]
        if worker_id == "canonical":
            worker_session = session + "-canonical"
        else:
            worker_session = session
        command = _campaign_worker.build_tmux_launch_argv(
            session=worker_session,
            window=worker_id,
            cpu_list=cpus,
            worker_argv=worker,
            cwd=ROOT,
            first_window=worker_id == "canonical" or index == 0,
            tmux_path=tmux_path,
            taskset_path=taskset_path,
        )
        commands.append(command)
    return commands


def _campaign_panes(campaign: Mapping[str, Any]) -> tuple[_campaign_worker.PaneIdentity, ...]:
    spec = _campaign_spec(campaign)
    tmux_path = str(spec["resources"]["executables"]["tmux"])
    sessions_proc = subprocess.run(
        [tmux_path, "list-sessions", "-F", "#{session_name}"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    if sessions_proc.returncode:
        message = (sessions_proc.stderr or "").strip().casefold()
        if "no server running" in message or "failed to connect to server" in message:
            return ()
        raise CampaignError(f"cannot inspect tmux sessions: {sessions_proc.stderr.strip()}")
    if len(sessions_proc.stdout.encode("utf-8", errors="strict")) > 65536:
        raise CampaignError("tmux session output exceeds the safety bound")
    session_names = {
        validate_identifier(line, "tmux session")
        for line in sessions_proc.stdout.splitlines()
        if line
    }
    base = _session_name(campaign)
    expected_sessions = {base, f"{base}-canonical"}
    panes: list[_campaign_worker.PaneIdentity] = []
    for session in sorted(session_names & expected_sessions):
        command = _campaign_worker.build_tmux_list_panes_argv(session, tmux_path=tmux_path)
        proc = subprocess.run(
            command,
            cwd=ROOT,
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
        if proc.returncode:
            message = (proc.stderr or "").strip().casefold()
            if "can't find session" in message or "no server running" in message:
                continue
            raise CampaignError(f"cannot inspect tmux panes for {session}: {proc.stderr.strip()}")
        observed = _campaign_worker.parse_tmux_panes(proc.stdout)
        if any(pane.session != session for pane in observed):
            raise CampaignError(f"tmux pane query escaped requested session {session!r}")
        panes.extend(observed)

    digest = campaign_digest(campaign)
    launch_records: list[dict[str, object]] = []
    for worker_id in sorted({job.worker_id for job in _jobs(campaign)}):
        launch_records.extend(
            _campaign_worker.scan_launch_evidence(
                _campaign_dir(campaign),
                run_id=str(campaign["run_id"]),
                campaign_sha256=digest,
                worker_id=worker_id,
            )
        )
    for pane in panes:
        matched = _campaign_worker.find_launch_evidence(
            launch_records,
            pane_id=pane.pane_id,
            pane_pid=pane.pane_pid,
        )
        if matched is None or _campaign_worker.PaneIdentity.from_dict(matched["pane"]) != pane:
            raise CampaignError(
                f"live campaign tmux pane lacks exact immutable launch evidence: {pane.pane_id}"
            )
    return tuple(panes)


def _bind_current_worker(
    campaign: Mapping[str, Any],
    worker_id: str,
) -> tuple[
    dict[str, object],
    _campaign_worker.ProcessIdentity,
    tuple[_campaign_worker.PaneIdentity, ...],
]:
    """Bind this process to its immutable launch and exact live tmux pane."""

    validate_identifier(worker_id, "worker id")
    pane_id = os.environ.get("TMUX_PANE")
    if type(pane_id) is not str or re.fullmatch(r"%(?:0|[1-9][0-9]{0,9})", pane_id) is None:
        raise CampaignError("internal worker requires a canonical TMUX_PANE identity")
    worker_pid = os.getpid()
    if type(worker_pid) is not int or worker_pid < 1:
        raise CampaignError("internal worker PID is invalid")
    run_dir = _campaign_dir(campaign)
    digest = campaign_digest(campaign)
    launch = _campaign_worker.wait_for_launch_evidence(
        run_dir,
        run_id=str(campaign["run_id"]),
        campaign_sha256=digest,
        worker_id=worker_id,
        pane_id=pane_id,
        pane_pid=worker_pid,
    )
    launch_number = launch.get("launch")
    if type(launch_number) is not int:
        raise CampaignError("bound launch evidence has an invalid launch number")
    launch = _campaign_worker.validate_launch_evidence(
        launch,
        run_id=str(campaign["run_id"]),
        campaign_sha256=digest,
        worker_id=worker_id,
        launch=launch_number,
    )
    recorded_pane = _campaign_worker.PaneIdentity.from_dict(launch["pane"])
    if recorded_pane.pane_id != pane_id or recorded_pane.pane_pid != worker_pid:
        raise CampaignError("internal worker differs from its recorded tmux pane PID")
    identity = _campaign_worker.observe_process(worker_pid)
    if identity is None or identity.pid != recorded_pane.pane_pid:
        raise CampaignError("internal worker process identity disappeared or changed")

    tmux_path = str(launch["tmux_path"])
    command = _campaign_worker.build_tmux_list_panes_argv(
        recorded_pane.session,
        tmux_path=tmux_path,
    )
    try:
        proc = subprocess.run(
            command,
            cwd=ROOT,
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise CampaignError(f"cannot observe bound worker tmux pane: {exc}") from exc
    if proc.returncode:
        detail = (proc.stderr or "").strip().replace("\n", " ")[:1000]
        raise CampaignError(f"cannot observe bound worker tmux pane: {detail}")
    panes = _campaign_worker.parse_tmux_panes(proc.stdout)
    if any(pane.session != recorded_pane.session for pane in panes):
        raise CampaignError("bound worker pane query escaped its recorded session")
    matches = [pane for pane in panes if pane == recorded_pane]
    if len(matches) != 1:
        raise CampaignError("bound worker lacks one exact live tmux pane identity")
    return launch, identity, panes


def _observed_job_state(
    state: Mapping[str, Any],
    *,
    panes: Sequence[_campaign_worker.PaneIdentity],
    host: str,
) -> str:
    recorded = str(state.get("state", "pending"))
    if recorded != "running":
        return recorded
    observation = _campaign_worker.classify_running_status(
        state,
        local_host=host,
        panes=panes,
        process_observer=_campaign_worker.observe_process,
    )
    return {
        "live": "running",
        "stale": "interrupted",
        "foreign": "foreign",
    }[observation]


def _check_launch_states(
    campaign: Mapping[str, Any], jobs: Sequence[JobSpec], *, resume: bool
) -> list[JobSpec]:
    pending: list[JobSpec] = []
    host = socket.gethostname()
    states = [(job, read_job_state(campaign, job)) for job in jobs]
    panes = (
        _campaign_panes(campaign)
        if any(state.get("state") == "running" for _job, state in states)
        else ()
    )
    for job, state in states:
        observed = _observed_job_state(state, panes=panes, host=host)
        if state.get("state") == "succeeded":
            if not _result_for_status(campaign, job, state):
                raise CampaignError(f"invalid succeeded result: {job.job_id}")
            continue
        if observed in {"running", "foreign"}:
            raise CampaignError(f"worker is already live for {job.job_id}")
        if state.get("state") != "pending" and not resume:
            raise CampaignError(f"{job.job_id} requires --resume from state {observed}")
        pending.append(job)
    return pending


def _execute_worker_launches(
    campaign: Mapping[str, Any],
    worker_ids: Sequence[str],
    commands: Sequence[Sequence[str]],
    *,
    resume: bool,
) -> list[dict[str, object]]:
    if len(worker_ids) != len(commands):
        raise CampaignError("worker launch plan length mismatch")
    spec = _campaign_spec(campaign)
    topology_spec = spec["topology"]
    lanes = {lane["worker_id"]: lane for lane in topology_spec["lanes"]}
    lanes["canonical"] = topology_spec["canonical"]
    resources = spec["resources"]
    executables = resources["executables"]
    tmux_path = str(executables["tmux"])
    taskset_path = str(executables["taskset"])
    run_dir = _campaign_dir(campaign)
    digest = campaign_digest(campaign)
    base_session = _session_name(campaign)
    captured: list[_campaign_worker.PaneIdentity] = []
    evidence_records: list[dict[str, object]] = []
    try:
        for worker_id, command_value in zip(worker_ids, commands, strict=True):
            command = list(command_value)
            proc = subprocess.run(
                command,
                cwd=ROOT,
                capture_output=True,
                text=True,
                check=False,
            )
            if proc.returncode:
                detail = (proc.stderr or "").strip().replace("\n", " ")[:1000]
                raise CampaignError(f"tmux launch failed for {worker_id}: {detail}")
            session = base_session + ("-canonical" if worker_id == "canonical" else "")
            pane = _campaign_worker.parse_tmux_launch_output(
                proc.stdout,
                expected_session=session,
                expected_window=worker_id,
            )
            captured.append(pane)
            prior = _campaign_worker.scan_launch_evidence(
                run_dir,
                run_id=str(campaign["run_id"]),
                campaign_sha256=digest,
                worker_id=worker_id,
            )
            worker_argv = build_worker_argv(campaign, worker_id, resume=resume)
            lane = lanes[worker_id]
            phase = "canonical" if worker_id == "canonical" else "exploratory"
            evidence = _campaign_worker.build_launch_evidence(
                run_id=str(campaign["run_id"]),
                campaign_sha256=digest,
                worker_id=worker_id,
                phase=phase,
                cpus=str(lane["cpus"]),
                launch=len(prior) + 1,
                resume=resume,
                launched_utc=utc_now(),
                pane=pane,
                worker_argv=worker_argv,
                cwd=ROOT,
                first_window=command[1] == "new-session",
                tmux_path=tmux_path,
                taskset_path=taskset_path,
            )
            if evidence["tmux_argv"] != command:
                raise CampaignError("executed tmux command differs from immutable launch evidence")
            _campaign_worker.persist_launch_evidence(run_dir, evidence)
            evidence_records.append(evidence)
    except BaseException as exc:
        cleanup_failures: list[str] = []
        for pane in reversed(captured):
            cleanup = _campaign_worker.build_tmux_kill_pane_argv(pane.pane_id, tmux_path=tmux_path)
            try:
                stopped = subprocess.run(
                    cleanup,
                    cwd=ROOT,
                    capture_output=True,
                    text=True,
                    check=False,
                )
            except OSError as cleanup_exc:
                cleanup_failures.append(f"{pane.pane_id}: {cleanup_exc}")
            else:
                if stopped.returncode:
                    cleanup_failures.append(
                        f"{pane.pane_id}: {(stopped.stderr or '').strip()[:500]}"
                    )
        if cleanup_failures:
            raise CampaignError(
                f"worker launch failed ({exc}); exact pane cleanup also failed: "
                f"{cleanup_failures!r}"
            ) from exc
        if isinstance(exc, CampaignError):
            raise
        raise CampaignError(f"worker launch failed: {type(exc).__name__}: {exc}") from exc
    return evidence_records


def cmd_launch(args: argparse.Namespace) -> int:
    campaign = _load_campaign(args.campaign)
    jobs = [job for job in _jobs(campaign) if job.phase == "exploratory"]
    pending = _check_launch_states(campaign, jobs, resume=args.resume)
    workers = [lane["worker_id"] for lane in _campaign_spec(campaign)["topology"]["lanes"]]
    workers = [worker for worker in workers if any(job.worker_id == worker for job in pending)]
    commands = _worker_commands(campaign, workers, resume=args.resume)
    if not args.execute:
        print(
            json.dumps(
                {"mode": "dry-run", "session": _session_name(campaign), "commands": commands},
                indent=2,
            )
        )
        return 0
    _cheap_revalidate(campaign)
    if _session_name(campaign) in _tmux_sessions():
        raise CampaignError(f"tmux session already exists: {_session_name(campaign)}")
    with campaign_lock(_campaign_dir(campaign), campaign_digest(campaign)):
        _execute_worker_launches(campaign, workers, commands, resume=args.resume)
    print(json.dumps({"session": _session_name(campaign), "workers": workers}))
    return 0


def cmd_canonical(args: argparse.Namespace) -> int:
    campaign = _load_campaign(args.campaign)
    exploratory = [job for job in _jobs(campaign) if job.phase == "exploratory"]
    exploratory_session = _session_name(campaign)
    if any(pane.session == exploratory_session for pane in _campaign_panes(campaign)):
        raise CampaignError("canonical phase requires every exploratory pane to be gone")
    for job in exploratory:
        state = read_job_state(campaign, job)
        if state.get("state") != "succeeded" or not _result_for_status(campaign, job, state):
            raise CampaignError(f"canonical phase requires valid exploratory result: {job.job_id}")
    jobs = [job for job in _jobs(campaign) if job.phase == "canonical"]
    pending = _check_launch_states(campaign, jobs, resume=args.resume)
    commands = _worker_commands(campaign, ["canonical"] if pending else [], resume=args.resume)
    if not args.execute:
        print(
            json.dumps(
                {
                    "mode": "dry-run",
                    "session": _session_name(campaign) + "-canonical",
                    "commands": commands,
                },
                indent=2,
            )
        )
        return 0
    _cheap_revalidate(campaign)
    canonical_session = _session_name(campaign) + "-canonical"
    if canonical_session in _tmux_sessions():
        raise CampaignError(f"tmux session already exists: {canonical_session}")
    with campaign_lock(_campaign_dir(campaign), campaign_digest(campaign)):
        _execute_worker_launches(
            campaign,
            ["canonical"] if pending else [],
            commands,
            resume=args.resume,
        )
    print(json.dumps({"session": canonical_session, "jobs": len(pending)}))
    return 0


def cmd_status(args: argparse.Namespace) -> int:
    campaign = _load_campaign(args.campaign)
    panes = _campaign_panes(campaign)
    host = socket.gethostname()
    counts = {
        state: 0
        for state in (
            "pending",
            "running",
            "foreign",
            "succeeded",
            "failed",
            "timed_out",
            "interrupted",
        )
    }
    workers: dict[str, dict[str, Any]] = {}
    jobs_output = []
    for job in _jobs(campaign):
        state = read_job_state(campaign, job)
        observed = _observed_job_state(state, panes=panes, host=host)
        counts[observed] += 1
        entry = {
            "job_id": job.job_id,
            "worker_id": job.worker_id,
            "recorded_state": state.get("state"),
            "observed_state": observed,
            "attempt": state.get("attempt", 0),
            "last_update": state.get("finished_utc") or state.get("started_utc"),
            "log": (state.get("paths") or {}).get("stdout_log"),
        }
        jobs_output.append(entry)
        if observed == "running":
            workers[job.worker_id] = entry
    output = {
        "run_id": campaign["run_id"],
        "campaign_sha256": campaign_digest(campaign),
        "panes": [pane.to_dict() for pane in panes],
        "counts": counts,
        "workers": workers,
        "jobs": jobs_output,
        "next_action": (
            "merge"
            if counts["succeeded"] == 36
            else "canonical"
            if counts["succeeded"] >= 25 and not counts["running"] and not counts["foreign"]
            else "wait-or-resume"
        ),
    }
    if args.json:
        print(json.dumps(output, sort_keys=True))
    else:
        print(f"campaign {campaign['run_id']} ({campaign_digest(campaign)[:12]})")
        print(" ".join(f"{key}={value}" for key, value in counts.items()))
        print(f"next: {output['next_action']}")
    return 0


def _terminal_status_from_attempt_result(
    campaign: Mapping[str, Any],
    job: JobSpec,
    result: Mapping[str, object],
    *,
    result_sha256: str,
) -> dict[str, object]:
    """Reconstruct the sole strict terminal status represented by a result."""

    attempt = result.get("attempt")
    if type(attempt) is not int:
        raise CampaignError(f"{job.job_id}: result attempt is not an integer")
    base = f"jobs/{job.job_id}/attempts/{attempt:04d}"
    status: dict[str, object] = {
        "schema_version": STATUS_SCHEMA,
        "run_id": campaign["run_id"],
        "campaign_sha256": campaign_digest(campaign),
        "job_id": job.job_id,
        "job_sha256": job.job_sha256,
        "worker_id": job.worker_id,
        "state": result.get("state"),
        "attempt": attempt,
        "started_utc": result.get("started_utc"),
        "host": result.get("host"),
        "cpu_affinity": list(parse_cpu_list(job.cpus)),
        "pane": result.get("pane"),
        "worker": result.get("worker_process"),
        "child": result.get("child_process"),
        "paths": {
            "attempt_dir": base,
            "stdout_log": f"{base}/stdout.log",
            "stderr_log": f"{base}/stderr.log",
            "result": f"{base}/result.json",
        },
        "finished_utc": result.get("finished_utc"),
        "result_sha256": result_sha256,
    }
    return _campaign_worker.validate_status(
        status,
        run_id=str(campaign["run_id"]),
        campaign_sha256=campaign_digest(campaign),
        job=job,
    )


def _validate_attempt_result(
    value: object,
    attempt: int,
    path: Path,
    *,
    campaign: Mapping[str, Any],
    job: JobSpec,
) -> bool:
    """Validate an immutable attempt result against its derived path and launch."""

    if not isinstance(value, Mapping):
        raise CampaignError(f"{job.job_id}: worker result must be an object")
    result = _campaign_results.validate_terminal_worker_result(campaign, job, value)
    expected = {
        "schema_version": WORKER_SCHEMA,
        "run_id": campaign["run_id"],
        "campaign_sha256": campaign_digest(campaign),
        "job_id": job.job_id,
        "job_sha256": job.job_sha256,
        "attempt": attempt,
    }
    for key, expected_value in expected.items():
        if type(result.get(key)) is not type(expected_value) or result.get(key) != expected_value:
            raise CampaignError(f"{job.job_id}: worker result {key} mismatch")
    run_dir = _campaign_dir(campaign)
    expected_dir = _campaign_safe_io.attempt_directory(run_dir, job.job_id, attempt)
    expected_path = expected_dir / "result.json"
    if _campaign_safe_io.lexical_absolute(path, "attempt result") != expected_path:
        raise CampaignError(f"{job.job_id}: result file is outside its derived attempt path")
    if result.get("attempt_dir") != str(expected_dir):
        raise CampaignError(f"{job.job_id}: result attempt_dir differs from its derived path")
    relative = expected_dir.relative_to(run_dir).as_posix()
    if (
        result.get("stdout_log") != f"{relative}/stdout.log"
        or result.get("stderr_log") != f"{relative}/stderr.log"
    ):
        raise CampaignError(f"{job.job_id}: result log paths differ from the attempt")

    state = result.get("state")
    if state not in {"succeeded", "failed", "timed_out", "interrupted"}:
        raise CampaignError(f"{job.job_id}: worker result has an invalid terminal state")
    validation = result.get("validation")
    if not isinstance(validation, Mapping) or set(validation) != {"accepted", "failures"}:
        raise CampaignError(f"{job.job_id}: worker result validation has an invalid shape")
    accepted = validation.get("accepted")
    failures = validation.get("failures")
    if type(accepted) is not bool or type(failures) is not list:
        raise CampaignError(f"{job.job_id}: worker result validation fields are invalid")
    if len(failures) > 128 or any(
        type(item) is not str or not item or len(item.encode("utf-8")) > 2000 for item in failures
    ):
        raise CampaignError(f"{job.job_id}: worker result failures are invalid or unbounded")
    successful = state == "succeeded"
    if accepted is not successful or (successful and failures) or (not successful and not failures):
        raise CampaignError(f"{job.job_id}: worker result state and validation disagree")

    recorded_affinity = result.get("cpu_affinity")
    if recorded_affinity is not None and (
        type(recorded_affinity) is not list
        or any(type(cpu) is not int or cpu < 0 for cpu in recorded_affinity)
        or len(set(recorded_affinity)) != len(recorded_affinity)
    ):
        raise CampaignError(f"{job.job_id}: worker result affinity observation is invalid")

    error = result.get("error")
    if error is not None and (type(error) is not str or len(error.encode("utf-8")) > 2000):
        raise CampaignError(f"{job.job_id}: worker result error is invalid or unbounded")
    child_value = result.get("child_process")
    child = None if child_value is None else _campaign_worker.ProcessIdentity.from_dict(child_value)
    expected_pid = None if child is None else child.pid
    if result.get("pid") != expected_pid:
        raise CampaignError(f"{job.job_id}: worker result child PID mismatch")
    _terminal_status_from_attempt_result(
        campaign,
        job,
        result,
        result_sha256="0" * 64,
    )

    pane = _campaign_worker.PaneIdentity.from_dict(result.get("pane"))
    launches = _campaign_worker.scan_launch_evidence(
        run_dir,
        run_id=str(campaign["run_id"]),
        campaign_sha256=campaign_digest(campaign),
        worker_id=job.worker_id,
    )
    launch = _campaign_worker.find_launch_evidence(
        launches,
        pane_id=pane.pane_id,
        pane_pid=pane.pane_pid,
    )
    if launch is None or _campaign_worker.PaneIdentity.from_dict(launch["pane"]) != pane:
        raise CampaignError(f"{job.job_id}: result lacks exact immutable launch evidence")
    if successful:
        validate_worker_result(campaign, job, cast(Mapping[str, Any], result))
    return successful


def _scan_job_attempts(
    campaign: Mapping[str, Any],
    job: JobSpec,
) -> _campaign_worker.AttemptInventory:
    def validate(value: object, attempt: int, path: Path) -> bool:
        return _validate_attempt_result(
            value,
            attempt,
            path,
            campaign=campaign,
            job=job,
        )

    return _campaign_worker.scan_attempts(
        _campaign_dir(campaign),
        job,
        result_validator=validate,
    )


def _write_recovery_result(
    campaign: Mapping[str, Any],
    job: JobSpec,
    attempt: int,
    *,
    status: Mapping[str, object],
    launch: Mapping[str, object],
    worker_identity: _campaign_worker.ProcessIdentity,
) -> Path:
    """Terminalize one allocated attempt that has no immutable result."""

    run_dir = _campaign_dir(campaign)
    attempt_dir = _campaign_safe_io.attempt_directory(run_dir, job.job_id, attempt)
    relative = attempt_dir.relative_to(run_dir).as_posix()
    use_recorded = status.get("state") == "running" and status.get("attempt") == attempt
    if use_recorded:
        pane_value = status.get("pane")
        worker_value = status.get("worker")
        child_value = status.get("child")
        started_utc = cast(str, status.get("started_utc"))
        host = status.get("host")
    else:
        pane_value = launch.get("pane")
        worker_value = worker_identity.to_dict()
        child_value = None
        started_utc = utc_now()
        host = socket.gethostname()
    child = None if child_value is None else _campaign_worker.ProcessIdentity.from_dict(child_value)
    finished_utc = max(started_utc, utc_now())
    failure = (
        "reconciled stale worker attempt without a terminal result"
        if use_recorded
        else "reconciled allocated attempt whose running status was not persisted"
    )
    result: dict[str, object] = {
        "schema_version": WORKER_SCHEMA,
        "run_id": campaign["run_id"],
        "campaign_sha256": campaign_digest(campaign),
        "job_id": job.job_id,
        "job_sha256": job.job_sha256,
        "attempt": attempt,
        "attempt_dir": str(attempt_dir),
        "state": "interrupted",
        "started_utc": started_utc,
        "finished_utc": finished_utc,
        "host": host,
        "pid": None if child is None else child.pid,
        "pane": pane_value,
        "worker_process": worker_value,
        "child_process": child_value,
        "cpu_affinity": list(parse_cpu_list(job.cpus)),
        "interpreter_probe": None,
        "argv": [],
        "environment": {},
        "harness_environment": {},
        "exit_code": None,
        "error": failure,
        "signal_number": None,
        "stdout_log": f"{relative}/stdout.log",
        "stderr_log": f"{relative}/stderr.log",
        "payload": {},
        "validation": {"accepted": False, "failures": [failure]},
    }
    result_path = attempt_dir / "result.json"
    atomic_create_json(result_path, result)
    _validate_attempt_result(
        result,
        attempt,
        result_path,
        campaign=campaign,
        job=job,
    )
    return result_path


def _write_result_status(
    campaign: Mapping[str, Any],
    job: JobSpec,
    status: Mapping[str, object],
    result: _campaign_worker.AttemptResult | _campaign_worker.AttemptSuccess,
) -> dict[str, object]:
    terminal = _terminal_status_from_attempt_result(
        campaign,
        job,
        result.result,
        result_sha256=result.result_sha256,
    )
    if dict(status) == terminal:
        return terminal
    return _campaign_worker.write_status(
        _campaign_dir(campaign),
        terminal,
        previous=status,
        run_id=str(campaign["run_id"]),
        campaign_sha256=campaign_digest(campaign),
        job=job,
    )


def _reconcile_worker_job(
    campaign: Mapping[str, Any],
    job: JobSpec,
    *,
    resume: bool,
    launch: Mapping[str, object],
    worker_identity: _campaign_worker.ProcessIdentity,
    panes: Sequence[_campaign_worker.PaneIdentity],
) -> dict[str, Any]:
    """Reconcile exact attempt evidence, then promote or run at most once."""

    status = read_job_state(campaign, job)
    inventory = _scan_job_attempts(campaign, job)
    result_by_attempt = {result.attempt: result for result in inventory.results}
    state = status.get("state")
    if state in {"succeeded", "failed", "timed_out", "interrupted"}:
        attempt = cast(int, status.get("attempt"))
        recorded = result_by_attempt.get(attempt)
        if recorded is None:
            raise CampaignError(f"{job.job_id}: terminal status lacks its immutable result")
        reconstructed = _terminal_status_from_attempt_result(
            campaign,
            job,
            recorded.result,
            result_sha256=recorded.result_sha256,
        )
        if reconstructed != status:
            raise CampaignError(f"{job.job_id}: terminal status differs from its result")

    observation: str | None = None
    if state == "running":
        observation = _campaign_worker.classify_running_status(
            status,
            local_host=socket.gethostname(),
            panes=panes,
            process_observer=_campaign_worker.observe_process,
        )
    decision = _campaign_worker.decide_reconciliation(
        inventory,
        status,
        resume=resume,
        running_observation=observation,
    )
    if decision.action == "promote":
        if decision.success is None:
            raise CampaignError(f"{job.job_id}: promotion lacks a successful attempt")
        _write_result_status(campaign, job, status, decision.success)
        return dict(decision.success.result)

    if decision.action not in {"run", "recover"} or decision.next_attempt is None:
        raise CampaignError(f"{job.job_id}: invalid reconciliation decision")
    if state == "running":
        running_attempt = cast(int, status["attempt"])
        recorded = result_by_attempt.get(running_attempt)
        if recorded is None:
            _write_recovery_result(
                campaign,
                job,
                running_attempt,
                status=status,
                launch=launch,
                worker_identity=worker_identity,
            )
            inventory = _scan_job_attempts(campaign, job)
            result_by_attempt = {result.attempt: result for result in inventory.results}
            recorded = result_by_attempt[running_attempt]
        status = _write_result_status(campaign, job, status, recorded)

    missing_attempts = sorted(set(inventory.attempts) - set(result_by_attempt))
    for missing_attempt in missing_attempts:
        _write_recovery_result(
            campaign,
            job,
            missing_attempt,
            status=status,
            launch=launch,
            worker_identity=worker_identity,
        )
    inventory = _scan_job_attempts(campaign, job)
    if inventory.successes:
        raise CampaignError(f"{job.job_id}: success appeared during retry reconciliation")
    if inventory.next_attempt != decision.next_attempt:
        raise CampaignError(f"{job.job_id}: attempt history changed during reconciliation")
    return run_job(
        campaign,
        job,
        decision.next_attempt,
        launch=launch,
        worker_identity=worker_identity,
    )


def cmd_worker(args: argparse.Namespace) -> int:
    campaign = _load_worker_campaign(args.campaign)
    if args.campaign_sha256 != campaign_digest(campaign):
        raise CampaignError("internal worker campaign digest mismatch")
    worker_id = validate_identifier(args.worker_id, "worker id")
    jobs = [job for job in _jobs(campaign) if job.worker_id == worker_id]
    if not jobs:
        raise CampaignError(f"campaign has no worker {worker_id}")
    launch, worker_identity, panes = _bind_current_worker(campaign, worker_id)
    if launch.get("resume") is not bool(args.resume):
        raise CampaignError("internal worker resume flag differs from launch evidence")
    worker_argv = launch.get("worker_argv")
    if not isinstance(worker_argv, list) or len(worker_argv) < 6:
        raise CampaignError("internal worker launch argv has an invalid shape")
    if worker_argv[5] != str(Path(args.campaign).resolve()):
        raise CampaignError("internal worker campaign path differs from launch evidence")

    failures: list[str] = []
    for job in jobs:
        result = _reconcile_worker_job(
            campaign,
            job,
            resume=bool(args.resume),
            launch=launch,
            worker_identity=worker_identity,
            panes=panes,
        )
        if result.get("state") != "succeeded":
            failures.append(job.job_id)
    return 1 if failures else 0


def cmd_merge(args: argparse.Namespace) -> int:
    campaign = _load_campaign(args.campaign)
    results = _accepted_results(campaign)
    if len(results) != 36:
        print(
            json.dumps(
                {
                    "mode": "dry-run" if not args.execute else "execute",
                    "accepted": len(results),
                    "expected": 36,
                    "target": str(_campaign_dir(campaign) / "campaign-results.json"),
                    "publish": bool(args.publish),
                    "publishable": False,
                }
            )
        )
        return 1
    _cheap_revalidate(campaign)
    merged = merge_campaign(campaign, results)
    preview = {
        "mode": "execute" if args.execute else "dry-run",
        "target": str(_campaign_dir(campaign) / "campaign-results.json"),
        "publish": bool(args.publish),
        "publishable": merged["gates"]["publishable"],
        "gates": merged["gates"],
    }
    if not args.execute:
        print(json.dumps(preview, indent=2))
        return 0 if merged["gates"]["publishable"] else 1
    if not merged["gates"]["publishable"]:
        raise CampaignError(f"campaign failed publication gates: {merged['gates']['failures']}")
    with campaign_lock(_campaign_dir(campaign), campaign_digest(campaign)):
        result_path = _campaign_dir(campaign) / "campaign-results.json"
        atomic_create_json(result_path, merged)
        reloaded = _campaign_safe_io.strict_json_load(
            result_path,
            validator=_campaign_results.validate_campaign_results,
        )
        if not isinstance(reloaded, dict):
            raise CampaignError("run-local result validator returned a non-object")
        if args.publish:
            publish_campaign(reloaded, args.publish_root)
    print(json.dumps(preview))
    return 0


def cmd_prepare_gtf(args: argparse.Namespace) -> int:
    if not args.execute:
        print(
            json.dumps(
                {
                    "mode": "dry-run",
                    "source": str(args.source),
                    "output": str(args.output),
                    "manifest": str(args.manifest),
                }
            )
        )
        return 0
    from benchmarks.prepare_gtf_control import build_parent_stripped_gtf

    build_parent_stripped_gtf(args.source, args.output, args.manifest, force=False)
    return 0


def _add_execute(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--execute",
        action="store_true",
        help="perform the proposed mutation (commands are dry-run by default)",
    )


def _add_campaign_resume(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--campaign", type=Path, required=True)
    parser.add_argument("--resume", action="store_true")
    _add_execute(parser)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(
        dest="command",
        required=True,
        metavar="{preflight,launch,status,canonical,merge}",
    )

    preflight = subparsers.add_parser(
        "preflight", help="verify identities/resources and create an immutable campaign"
    )
    preflight.add_argument("--run-id", required=True)
    preflight.add_argument(
        "--campaign-root", type=Path, default=ROOT / "benchmarks" / "out" / "cluster"
    )
    preflight.add_argument("--candidate-wheel", type=Path, required=True)
    preflight.add_argument("--primary-python", type=Path, required=True)
    preflight.add_argument("--gffbase-010-python", type=Path, required=True)
    preflight.add_argument("--gffutils-013-python", type=Path, required=True)
    preflight.add_argument("--legacy-timeout", type=int, default=5400)
    preflight.add_argument("--gffbase-timeout", type=int, default=3600)
    preflight.add_argument("--validation-sample", type=str, default="10000")
    preflight.add_argument("--n-spatial", type=int, default=5000)
    preflight.add_argument("--n-batched", type=int, default=5000)
    preflight.add_argument("--repeats", type=int, default=5)
    preflight.add_argument("--prepare-parent-stripped", action="store_true")
    preflight.add_argument("--parent-stripped", type=Path)
    preflight.add_argument("--parent-stripped-manifest", type=Path)
    _add_execute(preflight)
    preflight.set_defaults(handler=cmd_preflight)

    launch = subparsers.add_parser("launch", help="launch five exploratory tmux workers")
    _add_campaign_resume(launch)
    launch.set_defaults(handler=cmd_launch)

    status = subparsers.add_parser("status", help="read authoritative job and tmux state")
    status.add_argument("--campaign", type=Path, required=True)
    status.add_argument("--json", action="store_true")
    status.set_defaults(handler=cmd_status)

    canonical = subparsers.add_parser(
        "canonical", help="launch the isolated sequential canonical worker"
    )
    _add_campaign_resume(canonical)
    canonical.set_defaults(handler=cmd_canonical)

    merge = subparsers.add_parser("merge", help="validate and merge all 36 immutable jobs")
    merge.add_argument("--campaign", type=Path, required=True)
    merge.add_argument("--publish", action="store_true")
    merge.add_argument("--publish-root", type=Path, default=ROOT / "benchmarks" / "results")
    _add_execute(merge)
    merge.set_defaults(handler=cmd_merge)

    worker = subparsers.add_parser("_worker", help=argparse.SUPPRESS)
    worker.add_argument("--campaign", type=Path, required=True)
    worker.add_argument("--worker-id", required=True)
    worker.add_argument("--campaign-sha256", required=True)
    worker.add_argument("--resume", action="store_true")
    worker.set_defaults(handler=cmd_worker)
    # argparse has no public API for a parseable-but-hidden subcommand.  Remove
    # only its help pseudo-action; it remains in ``choices`` and parses.
    subparsers._choices_actions.pop()  # type: ignore[attr-defined]

    prepare = subparsers.add_parser("_prepare-gtf", help=argparse.SUPPRESS)
    prepare.add_argument("--source", type=Path, required=True)
    prepare.add_argument("--output", type=Path, required=True)
    prepare.add_argument("--manifest", type=Path, required=True)
    _add_execute(prepare)
    prepare.set_defaults(handler=cmd_prepare_gtf)
    subparsers._choices_actions.pop()  # type: ignore[attr-defined]
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.command == "preflight":
        values = (
            args.legacy_timeout,
            args.gffbase_timeout,
            args.n_spatial,
            args.n_batched,
            args.repeats,
        )
        if any(value < 1 for value in values):
            parser.error("benchmark timeouts, samples, counts, and repeats must be >= 1")
        if args.validation_sample != "10000":
            parser.error("--validation-sample is fixed at 10000 for the release campaign")
        if args.execute and not args.prepare_parent_stripped:
            if args.parent_stripped is None or args.parent_stripped_manifest is None:
                parser.error(
                    "executed preflight requires --prepare-parent-stripped or both "
                    "--parent-stripped and --parent-stripped-manifest"
                )
    try:
        return int(args.handler(args))
    except CampaignError as exc:
        print(f"cluster campaign error: {exc}", file=sys.stderr)
        return 2


# Slice 1 compatibility facade.  The remaining controller implementation is
# intentionally left in place until its owning I/O/worker/results slices move
# it behind these pure contracts.
_MODEL_EXPORTS = (
    "CARGO_VERSION",
    "CAMPAIGN_SCHEMA",
    "CORPUS_ORDER",
    "CampaignError",
    "CpuLane",
    "INDEX_SCHEMA",
    "INTERPRETER_ROLES",
    "JobSpec",
    "LAUNCH_SCHEMA",
    "LANE_CPUS",
    "PREFLIGHT_SCHEMA",
    "PUBLIC_VERSION",
    "REGION_SEED",
    "RESULTS_SCHEMA",
    "ROOT",
    "SIGNATURE_SCHEMA",
    "STATUS_SCHEMA",
    "THREADS",
    "WORKER_SCHEMA",
    "binding_parameters",
    "build_bridge_argv",
    "build_job_matrix",
    "build_mega_argv",
    "build_tmux_argv",
    "build_worker_argv",
    "campaign_digest",
    "campaign_jobs",
    "canonical_json_bytes",
    "job_digest",
    "parse_cpu_list",
    "sha256_json",
    "topology",
    "validate_campaign_document",
    "validate_identifier",
    "worker_environment",
)
globals().update({name: getattr(_campaign_model, name) for name in _MODEL_EXPORTS})

_SAFE_IO_EXPORTS = (
    "atomic_create_json",
    "atomic_write_json_durable",
    "campaign_lock",
)
globals().update({name: getattr(_campaign_safe_io, name) for name in _SAFE_IO_EXPORTS})

# Slice 5 result contracts replace the permissive draft implementations above
# while the CLI remains a compatibility facade for existing tools/tests.
validate_worker_result = _campaign_results.validate_worker_result
_strict_ratio = _campaign_results.strict_ratio
merge_campaign = _campaign_results.merge_campaign
validate_campaign_results = _campaign_results.validate_campaign_results
publish_campaign = _campaign_results.publish_campaign


if __name__ == "__main__":
    raise SystemExit(main())
