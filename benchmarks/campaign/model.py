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
"""Pure, closed contracts for the Linux benchmark campaign.

This module performs no host probes and no filesystem mutations.  It is the
single authority for schema names, immutable job identities, the binding
36-job release matrix, and deterministic process specifications.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
import shlex
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from types import MappingProxyType
from typing import Any, cast

from benchmarks.common import benchmark_env

ROOT = Path(__file__).resolve().parents[2]

CAMPAIGN_SCHEMA = "campaign-v2"
PREFLIGHT_SCHEMA = "campaign-preflight-v2"
STATUS_SCHEMA = "campaign-status-v2"
LAUNCH_SCHEMA = "campaign-launch-v2"
WORKER_SCHEMA = "benchmark-worker-v2"
RESULTS_SCHEMA = "campaign-results-v2"
INDEX_SCHEMA = "results-index-v2"
SIGNATURE_SCHEMA = "database-signature-v3"

PUBLIC_VERSION = "0.2.0rc1"
CARGO_VERSION = "0.2.0-rc.1"
REGION_SEED = 20260501

CORPUS_ORDER = ("mane", "chess", "refseq", "gencode-gtf", "gencode-gff3")
THREADS = (1, 2, 4, 8, 10)
LANE_CPUS = ("0-9", "10-19", "20-29", "30-39", "40-49")
INTERPRETER_ROLES = ("primary", "gffbase-0.1.0", "gffutils-0.13")

_IDENTIFIER_RE = re.compile(r"[a-z0-9][a-z0-9._-]{0,79}\Z", re.ASCII)
_CPU_COMPONENT_RE = re.compile(r"(?:0|[1-9][0-9]*)(?:-(?:0|[1-9][0-9]*))?\Z", re.ASCII)
_SHA256_RE = re.compile(r"[0-9a-f]{64}\Z", re.ASCII)
_UTC_RE = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}Z\Z", re.ASCII)

_INPUT_KEYS = {"path", "bytes", "sha256", "gzip_crc_ok", "format"}
_DERIVED_INPUT_KEYS = _INPUT_KEYS | {
    "manifest_path",
    "manifest_sha256",
    "transform_version",
    "counts",
}
_TRANSFORM_COUNT_KEYS = {
    "input_feature_lines",
    "output_feature_lines",
    "comment_or_blank_lines",
    "removed_gene_rows",
    "removed_transcript_rows",
}
_JOB_KEYS = {
    "job_id",
    "phase",
    "kind",
    "worker_id",
    "corpus_key",
    "result_key",
    "gtf_arm",
    "engine_set",
    "interpreter_role",
    "threads",
    "cpus",
    "input",
    "parameters",
    "primary_eligible",
    "job_sha256",
}
_PARAMETER_KEYS = {
    "legacy_timeout",
    "gffbase_timeout",
    "validation_sample",
    "n_spatial",
    "n_batched",
    "repeats",
    "region_seed",
}
_BRIDGE_PARAMETER_KEYS = {"bridge_engine", "cap_seconds"}
_RUNTIME_CAMPAIGN_KEYS = {
    "schema_version",
    "run_id",
    "created_utc",
    "spec_sha256",
    "spec",
    "_path",
}
_CAMPAIGN_KEYS = _RUNTIME_CAMPAIGN_KEYS - {"_path"}
_SPEC_KEYS = {
    "repo",
    "candidate",
    "interpreters",
    "inputs",
    "topology",
    "parameters",
    "resources",
    "jobs",
}
_BASE_ENVIRONMENT_ALLOWLIST = {
    "HOME",
    "LANG",
    "LC_ALL",
    "PATH",
    "SSL_CERT_DIR",
    "SSL_CERT_FILE",
    "TMPDIR",
    "TZ",
    "XDG_CACHE_HOME",
}
_MAX_CPU_ID = 4095
_MAX_CPU_COUNT = 4096
_MAX_CPU_TEXT = 32768
_MAX_JSON_DEPTH = 64
_MAX_JSON_NODES = 100_000
_MAX_JSON_STRING_CHARS = 1_000_000
_MAX_JSON_INTEGER_BITS = 512
_MAX_CANONICAL_JSON_BYTES = 16 * (1 << 20)


class CampaignError(RuntimeError):
    """A campaign artifact or pure identity contract is invalid."""


def require_exact_keys(
    value: object, expected: set[str] | frozenset[str], label: str
) -> dict[str, Any]:
    """Return *value* as a dict after enforcing an exact object key set."""

    if not isinstance(value, Mapping):
        raise CampaignError(f"{label} must be an object")
    keys = set(value)
    missing = expected - keys
    unknown = keys - expected
    if missing:
        raise CampaignError(f"{label} is missing keys: {sorted(missing)!r}")
    if unknown:
        rendered = sorted(repr(key) for key in unknown)
        raise CampaignError(f"{label} has unknown keys: {rendered!r}")
    if any(type(key) is not str for key in keys):
        raise CampaignError(f"{label} keys must be strings")
    return dict(value)


def require_schema(value: object, expected: str, label: str) -> dict[str, Any]:
    """Require an exact ``schema_version`` without accepting draft aliases."""

    if not isinstance(value, Mapping):
        raise CampaignError(f"{label} must be an object with schema_version")
    found = value.get("schema_version")
    if type(found) is not str or found != expected:
        raise CampaignError(f"{label} expected schema_version={expected!r}, found {found!r}")
    return dict(value)


def canonical_json_bytes(value: object) -> bytes:
    """Return deterministic UTF-8 JSON bytes for an identity digest."""

    _validate_json_value(value)
    try:
        text = json.dumps(
            value,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        )
        encoded = text.encode("utf-8", errors="strict")
        if len(encoded) > _MAX_CANONICAL_JSON_BYTES:
            raise CampaignError(f"canonical JSON exceeds {_MAX_CANONICAL_JSON_BYTES} encoded bytes")
        return encoded
    except (TypeError, ValueError, OverflowError, RecursionError, UnicodeError) as exc:
        raise CampaignError(f"value is not canonical JSON: {exc}") from exc


def _validate_json_value(value: object, label: str = "$") -> None:
    stack: list[tuple[object, int]] = [(value, 0)]
    nodes = 0
    string_chars = 0
    while stack:
        item, depth = stack.pop()
        nodes += 1
        if nodes > _MAX_JSON_NODES:
            raise CampaignError(f"{label} exceeds the JSON node bound")
        if depth > _MAX_JSON_DEPTH:
            raise CampaignError(f"{label} exceeds the JSON depth bound")
        item_type = type(item)
        if item is None or item_type is bool:
            continue
        if item_type is int:
            if abs(cast(int, item)).bit_length() > _MAX_JSON_INTEGER_BITS:
                raise CampaignError(f"{label} contains an integer outside the JSON bound")
            continue
        if item_type is float:
            if not math.isfinite(cast(float, item)):
                raise CampaignError(f"{label} contains a non-finite JSON number")
            continue
        if item_type is str:
            string_chars += len(cast(str, item))
            if (
                len(cast(str, item)) > _MAX_JSON_STRING_CHARS
                or string_chars > _MAX_CANONICAL_JSON_BYTES
            ):
                raise CampaignError(f"{label} contains a string outside the JSON bound")
            continue
        if item_type is list:
            list_children = cast(list[object], item)
            if nodes + len(stack) + len(list_children) > _MAX_JSON_NODES:
                raise CampaignError(f"{label} exceeds the JSON node bound")
            stack.extend((child, depth + 1) for child in list_children)
            continue
        if item_type is dict:
            dict_children = cast(dict[object, object], item)
            if nodes + len(stack) + len(dict_children) > _MAX_JSON_NODES:
                raise CampaignError(f"{label} exceeds the JSON node bound")
            for key, child in dict_children.items():
                if type(key) is not str:
                    raise CampaignError(f"{label} contains a non-string JSON object key")
                string_chars += len(key)
                if len(key) > _MAX_JSON_STRING_CHARS or string_chars > _MAX_CANONICAL_JSON_BYTES:
                    raise CampaignError(f"{label} contains a string outside the JSON bound")
                stack.append((child, depth + 1))
            continue
        raise CampaignError(f"{label} contains a non-JSON value of type {item_type.__name__}")


def sha256_json(value: object) -> str:
    """Digest a canonical JSON value."""

    return hashlib.sha256(canonical_json_bytes(value)).hexdigest()


def _require_string(value: object, label: str) -> str:
    if type(value) is not str or not value:
        raise CampaignError(f"{label} must be a non-empty string")
    return value


def _require_sha256(value: object, label: str) -> str:
    if type(value) is not str or _SHA256_RE.fullmatch(value) is None:
        raise CampaignError(f"{label} must be a lowercase SHA-256 digest")
    return value


def _require_positive_int(value: object, label: str) -> int:
    if type(value) is not int or value < 1:
        raise CampaignError(f"{label} must be a positive integer")
    return value


def _require_nonnegative_int(value: object, label: str) -> int:
    if type(value) is not int or value < 0:
        raise CampaignError(f"{label} must be a nonnegative integer")
    return value


def _require_utc_timestamp(value: object, label: str) -> str:
    if type(value) is not str or _UTC_RE.fullmatch(value) is None:
        raise CampaignError(f"{label} must be an integral UTC timestamp")
    try:
        parsed = datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ")
    except ValueError as exc:
        raise CampaignError(f"{label} must be a valid UTC timestamp") from exc
    if parsed.strftime("%Y-%m-%dT%H:%M:%SZ") != value:
        raise CampaignError(f"{label} must use canonical UTC timestamp spelling")
    return value


def validate_identifier(value: str, kind: str = "identifier") -> str:
    """Validate a path- and shell-independent ASCII identifier."""

    if type(value) is not str or _IDENTIFIER_RE.fullmatch(value) is None or ".." in value:
        raise CampaignError(
            f"invalid {kind} identifier {value!r}; expected 1-80 lowercase ASCII "
            "letters, digits, '.', '_' or '-' without '..'"
        )
    return value


def parse_cpu_list(value: str) -> tuple[int, ...]:
    """Parse a canonical, strictly increasing ASCII CPU-list expression."""

    if type(value) is not str or not value or len(value) > _MAX_CPU_TEXT:
        raise CampaignError(f"invalid CPU list: {value!r}")
    cpus: list[int] = []
    components = value.split(",")
    if len(components) > _MAX_CPU_COUNT:
        raise CampaignError(f"CPU list exceeds {_MAX_CPU_COUNT} components")
    for component in components:
        if len(component) > 9:
            raise CampaignError(f"invalid CPU component: {component!r}")
        if _CPU_COMPONENT_RE.fullmatch(component) is None:
            raise CampaignError(f"invalid CPU component: {component!r}")
        try:
            if "-" in component:
                start_text, end_text = component.split("-", 1)
                start, end = int(start_text), int(end_text)
            else:
                start = end = int(component)
        except ValueError as exc:
            raise CampaignError(f"invalid CPU component: {component!r}") from exc
        if "-" in component:
            if end < start:
                raise CampaignError(f"invalid CPU range: {component!r}")
            if end > _MAX_CPU_ID or end - start + 1 > _MAX_CPU_COUNT:
                raise CampaignError(f"CPU range is outside the bounded parser: {component!r}")
            values: Sequence[int] = range(start, end + 1)
        else:
            if start > _MAX_CPU_ID:
                raise CampaignError(f"CPU is outside the bounded parser: {component!r}")
            values = (start,)
        for cpu in values:
            if cpus and cpu <= cpus[-1]:
                raise CampaignError(f"CPU list is overlapping or not increasing: {value!r}")
            cpus.append(cpu)
            if len(cpus) > _MAX_CPU_COUNT:
                raise CampaignError(f"CPU list exceeds {_MAX_CPU_COUNT} entries")
    if value != _render_cpu_list(cpus):
        raise CampaignError(f"CPU list is not in canonical form: {value!r}")
    return tuple(cpus)


def _render_cpu_list(cpus: Sequence[int]) -> str:
    parts: list[str] = []
    start = previous = cpus[0]
    for cpu in cpus[1:]:
        if cpu == previous + 1:
            previous = cpu
            continue
        parts.append(str(start) if start == previous else f"{start}-{previous}")
        start = previous = cpu
    parts.append(str(start) if start == previous else f"{start}-{previous}")
    return ",".join(parts)


def _freeze(value: object) -> object:
    if isinstance(value, Mapping):
        return MappingProxyType({key: _freeze(item) for key, item in value.items()})
    if type(value) in {list, tuple}:
        return tuple(_freeze(item) for item in cast(Sequence[object], value))
    return value


def _thaw(value: object) -> object:
    if isinstance(value, Mapping):
        return {str(key): _thaw(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [_thaw(item) for item in value]
    return value


@dataclass(frozen=True, slots=True)
class CpuLane:
    """One immutable worker/CPU assignment."""

    worker_id: str
    threads: int
    cpus: str

    def __post_init__(self) -> None:
        validate_identifier(self.worker_id, "worker")
        _require_positive_int(self.threads, "lane threads")
        parse_cpu_list(self.cpus)

    @property
    def cpu_set(self) -> tuple[int, ...]:
        return parse_cpu_list(self.cpus)

    def to_dict(self) -> dict[str, object]:
        return {"worker_id": self.worker_id, "threads": self.threads, "cpus": self.cpus}

    @classmethod
    def from_dict(cls, value: Mapping[str, object]) -> CpuLane:
        record = require_exact_keys(value, {"worker_id", "threads", "cpus"}, "CPU lane")
        return cls(
            worker_id=cast(str, record["worker_id"]),
            threads=cast(int, record["threads"]),
            cpus=cast(str, record["cpus"]),
        )


def topology() -> dict[str, object]:
    """Return the binding five-lane exploratory and one-lane canonical topology."""

    lanes = [
        CpuLane(f"scale-t{threads:02d}", threads, cpus).to_dict()
        for threads, cpus in zip(THREADS, LANE_CPUS, strict=True)
    ]
    return {
        "lanes": lanes,
        "canonical": CpuLane("canonical", 10, "0-9").to_dict(),
    }


def binding_parameters() -> dict[str, object]:
    """Return a fresh copy of the immutable release measurement profile."""

    return {
        "legacy_timeout": 5400,
        "gffbase_timeout": 3600,
        "validation_sample": "10000",
        "n_spatial": 5000,
        "n_batched": 5000,
        "repeats": 5,
        "region_seed": REGION_SEED,
    }


def _validate_binding_parameters(value: object) -> dict[str, object]:
    record = require_exact_keys(value, _PARAMETER_KEYS, "campaign parameters")
    expected = binding_parameters()
    for key, expected_value in expected.items():
        actual = record[key]
        if type(actual) is not type(expected_value) or actual != expected_value:
            raise CampaignError(
                f"campaign parameter {key!r} must be the binding value "
                f"{expected_value!r}, found {actual!r}"
            )
    return record


def _normalize_config(config: Mapping[str, object]) -> dict[str, object]:
    if not isinstance(config, Mapping):
        raise CampaignError("campaign parameters must be an object")
    if not config:
        return binding_parameters()
    return _validate_binding_parameters(config)


def _validate_input_identity(value: object, *, label: str, derived: bool) -> dict[str, object]:
    expected_keys = _DERIVED_INPUT_KEYS if derived else _INPUT_KEYS
    record = require_exact_keys(value, expected_keys, label)
    _require_string(record["path"], f"{label}.path")
    _require_positive_int(record["bytes"], f"{label}.bytes")
    _require_sha256(record["sha256"], f"{label}.sha256")
    if type(record["gzip_crc_ok"]) is not bool or record["gzip_crc_ok"] is not True:
        raise CampaignError(f"{label}.gzip_crc_ok must be true")
    if type(record["format"]) is not str or record["format"] not in {"gff3", "gtf"}:
        raise CampaignError(f"{label}.format must be 'gff3' or 'gtf'")
    if derived:
        _require_string(record["manifest_path"], f"{label}.manifest_path")
        _require_sha256(record["manifest_sha256"], f"{label}.manifest_sha256")
        if type(record["transform_version"]) is not str or record["transform_version"] != "1":
            raise CampaignError(f"{label}.transform_version must be '1'")
        counts = require_exact_keys(record["counts"], _TRANSFORM_COUNT_KEYS, f"{label}.counts")
        for key in _TRANSFORM_COUNT_KEYS:
            _require_nonnegative_int(counts[key], f"{label}.counts.{key}")
        if counts["removed_gene_rows"] < 1 or counts["removed_transcript_rows"] < 1:
            raise CampaignError(f"{label}.counts must remove gene and transcript rows")
        if counts["input_feature_lines"] != (
            counts["output_feature_lines"]
            + counts["removed_gene_rows"]
            + counts["removed_transcript_rows"]
        ):
            raise CampaignError(f"{label}.counts do not balance")
        record["counts"] = counts
    return record


def _mega_parameters(profile: Mapping[str, object], validation_sample: str) -> dict[str, object]:
    values = dict(profile)
    values["validation_sample"] = validation_sample
    return values


def _validate_job_parameters(kind: str, value: object) -> dict[str, object]:
    if kind == "bridge":
        record = require_exact_keys(value, _BRIDGE_PARAMETER_KEYS, "bridge parameters")
        if type(record["bridge_engine"]) is not str or record["bridge_engine"] not in {
            "gffbase",
            "gffutils",
        }:
            raise CampaignError("bridge parameters contain an invalid engine")
        if type(record["cap_seconds"]) is not int or record["cap_seconds"] != 5400:
            raise CampaignError("bridge cap_seconds must be the binding 5400")
        return record
    record = require_exact_keys(value, _PARAMETER_KEYS, "benchmark parameters")
    expected = binding_parameters()
    expected["validation_sample"] = "10000" if kind == "scaling" else "all"
    for key, expected_value in expected.items():
        actual = record[key]
        if type(actual) is not type(expected_value) or actual != expected_value:
            raise CampaignError(
                f"benchmark parameter {key!r} must be {expected_value!r}, found {actual!r}"
            )
    return record


def _job_unsigned(
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
    input_value: Mapping[str, object],
    parameters: Mapping[str, object],
    primary_eligible: bool,
) -> dict[str, object]:
    return {
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
        "input": _thaw(input_value),
        "parameters": _thaw(parameters),
        "primary_eligible": primary_eligible,
    }


@dataclass(frozen=True, slots=True)
class JobSpec:
    """One fully validated and digest-bound campaign job."""

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
    input: Mapping[str, object]
    parameters: Mapping[str, object]
    primary_eligible: bool
    job_sha256: str

    def __post_init__(self) -> None:
        validate_identifier(self.job_id, "job")
        validate_identifier(self.worker_id, "worker")
        validate_identifier(self.corpus_key, "corpus")
        validate_identifier(self.result_key, "result")
        _require_string(self.phase, "job phase")
        _require_string(self.kind, "job kind")
        _require_string(self.engine_set, "job engine_set")
        _require_string(self.interpreter_role, "job interpreter_role")
        if type(self.gtf_arm) is not str and self.gtf_arm is not None:
            raise CampaignError("job gtf_arm must be a string or null")
        _require_positive_int(self.threads, "job threads")
        cpus = parse_cpu_list(self.cpus)
        if any(cpu > 49 for cpu in cpus):
            raise CampaignError("job CPU assignment must stay within CPUs 0-49")
        if type(self.primary_eligible) is not bool:
            raise CampaignError("job primary_eligible must be a boolean")
        if self.phase not in {"exploratory", "canonical"}:
            raise CampaignError(f"invalid job phase: {self.phase!r}")
        if self.kind not in {"scaling", "primary", "control", "bridge"}:
            raise CampaignError(f"invalid job kind: {self.kind!r}")
        if self.corpus_key not in CORPUS_ORDER:
            raise CampaignError(f"invalid campaign corpus: {self.corpus_key!r}")
        derived = self.kind == "control" and self.gtf_arm == "parent-stripped"
        input_value = _validate_input_identity(
            self.input, label=f"{self.job_id}.input", derived=derived
        )
        parameters = _validate_job_parameters(self.kind, self.parameters)
        self._validate_semantics(input_value, parameters)
        _require_sha256(self.job_sha256, f"{self.job_id}.job_sha256")
        unsigned = _job_unsigned(
            job_id=self.job_id,
            phase=self.phase,
            kind=self.kind,
            worker_id=self.worker_id,
            corpus_key=self.corpus_key,
            result_key=self.result_key,
            gtf_arm=self.gtf_arm,
            engine_set=self.engine_set,
            interpreter_role=self.interpreter_role,
            threads=self.threads,
            cpus=self.cpus,
            input_value=input_value,
            parameters=parameters,
            primary_eligible=self.primary_eligible,
        )
        if self.job_sha256 != sha256_json(unsigned):
            raise CampaignError(f"job digest mismatch for {self.job_id}")
        object.__setattr__(self, "input", _freeze(input_value))
        object.__setattr__(self, "parameters", _freeze(parameters))

    def _validate_semantics(
        self, input_value: Mapping[str, object], parameters: Mapping[str, object]
    ) -> None:
        expected_format = "gtf" if self.corpus_key == "gencode-gtf" else "gff3"
        if input_value["format"] != expected_format:
            raise CampaignError(f"input format mismatch for {self.job_id}")
        if self.kind == "scaling":
            lane = dict(zip(THREADS, LANE_CPUS, strict=True)).get(self.threads)
            expected = {
                "job_id": f"scaling-t{self.threads:02d}-{self.corpus_key}",
                "phase": "exploratory",
                "worker_id": f"scale-t{self.threads:02d}",
                "result_key": self.corpus_key,
                "gtf_arm": "no-infer" if self.corpus_key == "gencode-gtf" else None,
                "engine_set": "candidate-only",
                "interpreter_role": "primary",
                "cpus": lane,
                "primary_eligible": False,
            }
        elif self.kind == "primary":
            expected = {
                "job_id": f"canonical-{self.corpus_key}",
                "phase": "canonical",
                "worker_id": "canonical",
                "result_key": self.corpus_key,
                "gtf_arm": "no-infer" if self.corpus_key == "gencode-gtf" else None,
                "engine_set": "candidate-vs-gffutils-0.14",
                "interpreter_role": "primary",
                "cpus": "0-9",
                "primary_eligible": True,
            }
            if self.threads != 10:
                raise CampaignError(f"canonical job {self.job_id} must use 10 threads")
        elif self.kind == "control":
            if self.gtf_arm not in {"default", "parent-stripped"}:
                raise CampaignError(f"invalid GTF control arm for {self.job_id}")
            expected = {
                "job_id": f"control-gencode-gtf-{self.gtf_arm}",
                "phase": "canonical",
                "worker_id": "canonical",
                "result_key": f"gencode-gtf-{self.gtf_arm}",
                "gtf_arm": self.gtf_arm,
                "engine_set": "candidate-vs-gffutils-0.14",
                "interpreter_role": "primary",
                "cpus": "0-9",
                "primary_eligible": False,
            }
            if self.corpus_key != "gencode-gtf" or self.threads != 10:
                raise CampaignError(f"invalid GTF control dimensions for {self.job_id}")
        else:
            version_to_engine = {
                "gffbase-0.1.0": "gffbase",
                "gffutils-0.13": "gffutils",
            }
            bridge_engine = version_to_engine.get(self.interpreter_role)
            expected = {
                "job_id": f"bridge-{self.interpreter_role}-{self.corpus_key}",
                "phase": "canonical",
                "worker_id": "canonical",
                "result_key": self.corpus_key,
                "gtf_arm": None,
                "engine_set": self.interpreter_role,
                "interpreter_role": self.interpreter_role,
                "cpus": "0-9",
                "primary_eligible": False,
            }
            if (
                self.corpus_key not in {"mane", "chess"}
                or self.threads != 10
                or bridge_engine is None
                or parameters["bridge_engine"] != bridge_engine
            ):
                raise CampaignError(f"invalid bridge dimensions for {self.job_id}")
        actual = {
            "job_id": self.job_id,
            "phase": self.phase,
            "worker_id": self.worker_id,
            "result_key": self.result_key,
            "gtf_arm": self.gtf_arm,
            "engine_set": self.engine_set,
            "interpreter_role": self.interpreter_role,
            "cpus": self.cpus,
            "primary_eligible": self.primary_eligible,
        }
        if actual != expected:
            raise CampaignError(f"job semantics do not match the release matrix: {self.job_id}")

    def to_dict(self) -> dict[str, object]:
        value = _job_unsigned(
            job_id=self.job_id,
            phase=self.phase,
            kind=self.kind,
            worker_id=self.worker_id,
            corpus_key=self.corpus_key,
            result_key=self.result_key,
            gtf_arm=self.gtf_arm,
            engine_set=self.engine_set,
            interpreter_role=self.interpreter_role,
            threads=self.threads,
            cpus=self.cpus,
            input_value=self.input,
            parameters=self.parameters,
            primary_eligible=self.primary_eligible,
        )
        value["job_sha256"] = self.job_sha256
        return value

    @classmethod
    def from_dict(cls, value: Mapping[str, object]) -> JobSpec:
        record = require_exact_keys(value, _JOB_KEYS, "job")
        return cls(
            job_id=cast(str, record["job_id"]),
            phase=cast(str, record["phase"]),
            kind=cast(str, record["kind"]),
            worker_id=cast(str, record["worker_id"]),
            corpus_key=cast(str, record["corpus_key"]),
            result_key=cast(str, record["result_key"]),
            gtf_arm=cast(str | None, record["gtf_arm"]),
            engine_set=cast(str, record["engine_set"]),
            interpreter_role=cast(str, record["interpreter_role"]),
            threads=cast(int, record["threads"]),
            cpus=cast(str, record["cpus"]),
            input=cast(Mapping[str, object], record["input"]),
            parameters=cast(Mapping[str, object], record["parameters"]),
            primary_eligible=cast(bool, record["primary_eligible"]),
            job_sha256=cast(str, record["job_sha256"]),
        )


def _make_job(
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
    input_value: Mapping[str, object],
    parameters: Mapping[str, object],
    primary_eligible: bool = False,
) -> JobSpec:
    unsigned = _job_unsigned(
        job_id=job_id,
        phase=phase,
        kind=kind,
        worker_id=worker_id,
        corpus_key=corpus_key,
        result_key=result_key,
        gtf_arm=gtf_arm,
        engine_set=engine_set,
        interpreter_role=interpreter_role,
        threads=threads,
        cpus=cpus,
        input_value=input_value,
        parameters=parameters,
        primary_eligible=primary_eligible,
    )
    return JobSpec(**unsigned, job_sha256=sha256_json(unsigned))  # type: ignore[arg-type]


def build_job_matrix(
    config: Mapping[str, object],
    identities: Mapping[str, object],
    inputs: Mapping[str, object],
) -> tuple[JobSpec, ...]:
    """Build the binding ordered 25-scaling + 11-canonical job matrix."""

    if not isinstance(identities, Mapping):
        raise CampaignError("interpreter identities must be an object")
    profile = _normalize_config(config)
    input_records = require_exact_keys(
        inputs,
        {*CORPUS_ORDER, "gencode-gtf-parent-stripped"},
        "campaign inputs",
    )
    normalized_inputs = {
        key: _validate_input_identity(
            input_records[key],
            label=f"campaign inputs.{key}",
            derived=key == "gencode-gtf-parent-stripped",
        )
        for key in input_records
    }
    jobs: list[JobSpec] = []
    for threads, cpus in zip(THREADS, LANE_CPUS, strict=True):
        for corpus_key in CORPUS_ORDER:
            jobs.append(
                _make_job(
                    job_id=f"scaling-t{threads:02d}-{corpus_key}",
                    phase="exploratory",
                    kind="scaling",
                    worker_id=f"scale-t{threads:02d}",
                    corpus_key=corpus_key,
                    result_key=corpus_key,
                    gtf_arm="no-infer" if corpus_key == "gencode-gtf" else None,
                    engine_set="candidate-only",
                    interpreter_role="primary",
                    threads=threads,
                    cpus=cpus,
                    input_value=normalized_inputs[corpus_key],
                    parameters=_mega_parameters(profile, "10000"),
                )
            )
    for corpus_key in CORPUS_ORDER:
        jobs.append(
            _make_job(
                job_id=f"canonical-{corpus_key}",
                phase="canonical",
                kind="primary",
                worker_id="canonical",
                corpus_key=corpus_key,
                result_key=corpus_key,
                gtf_arm="no-infer" if corpus_key == "gencode-gtf" else None,
                engine_set="candidate-vs-gffutils-0.14",
                interpreter_role="primary",
                threads=10,
                cpus="0-9",
                input_value=normalized_inputs[corpus_key],
                parameters=_mega_parameters(profile, "all"),
                primary_eligible=True,
            )
        )
    for arm, input_key in (
        ("default", "gencode-gtf"),
        ("parent-stripped", "gencode-gtf-parent-stripped"),
    ):
        jobs.append(
            _make_job(
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
                input_value=normalized_inputs[input_key],
                parameters=_mega_parameters(profile, "all"),
            )
        )
    for version, engine in (
        ("gffbase-0.1.0", "gffbase"),
        ("gffutils-0.13", "gffutils"),
    ):
        for corpus_key in ("mane", "chess"):
            jobs.append(
                _make_job(
                    job_id=f"bridge-{version}-{corpus_key}",
                    phase="canonical",
                    kind="bridge",
                    worker_id="canonical",
                    corpus_key=corpus_key,
                    result_key=corpus_key,
                    gtf_arm=None,
                    engine_set=version,
                    interpreter_role=version,
                    threads=10,
                    cpus="0-9",
                    input_value=normalized_inputs[corpus_key],
                    parameters={"bridge_engine": engine, "cap_seconds": 5400},
                )
            )
    if len(jobs) != 36 or len({job.job_id for job in jobs}) != 36:
        raise CampaignError("release job matrix is not exactly 36 unique jobs")
    if len({job.job_sha256 for job in jobs}) != 36:
        raise CampaignError("release job matrix digests are not unique")
    return tuple(jobs)


def job_digest(job: JobSpec | Mapping[str, object]) -> str:
    """Validate and return a job's bound digest."""

    spec = job if isinstance(job, JobSpec) else JobSpec.from_dict(job)
    unsigned = spec.to_dict()
    expected = unsigned.pop("job_sha256")
    actual = sha256_json(unsigned)
    if expected != actual:
        raise CampaignError(f"job digest mismatch for {spec.job_id}")
    return actual


def _validate_interpreters(value: object) -> dict[str, object]:
    interpreters = require_exact_keys(value, set(INTERPRETER_ROLES), "campaign interpreters")
    for role in INTERPRETER_ROLES:
        probe = interpreters[role]
        if not isinstance(probe, Mapping):
            raise CampaignError(f"campaign interpreter {role!r} must be an object")
        _require_string(probe.get("resolved_executable"), f"campaign interpreter {role}")
    return interpreters


def validate_campaign_document(value: object) -> dict[str, object]:
    """Validate the closed on-disk campaign-v2 document and exact matrix."""

    campaign = require_exact_keys(value, _CAMPAIGN_KEYS, "campaign")
    require_schema(campaign, CAMPAIGN_SCHEMA, "campaign")
    validate_identifier(cast(str, campaign["run_id"]), "run")
    _require_utc_timestamp(campaign["created_utc"], "campaign created_utc")
    expected_digest = _require_sha256(campaign["spec_sha256"], "campaign spec_sha256")
    spec = require_exact_keys(campaign["spec"], _SPEC_KEYS, "campaign spec")
    if not isinstance(spec["repo"], Mapping):
        raise CampaignError("campaign spec.repo must be an object")
    if not isinstance(spec["candidate"], Mapping):
        raise CampaignError("campaign spec.candidate must be an object")
    if not isinstance(spec["resources"], Mapping):
        raise CampaignError("campaign spec.resources must be an object")
    interpreters = _validate_interpreters(spec["interpreters"])
    parameters = _validate_binding_parameters(spec["parameters"])
    if canonical_json_bytes(spec["topology"]) != canonical_json_bytes(topology()):
        raise CampaignError("campaign topology differs from the binding topology")
    inputs = require_exact_keys(
        spec["inputs"],
        {*CORPUS_ORDER, "gencode-gtf-parent-stripped"},
        "campaign inputs",
    )
    for key, input_value in inputs.items():
        _validate_input_identity(
            input_value,
            label=f"campaign inputs.{key}",
            derived=key == "gencode-gtf-parent-stripped",
        )
    jobs_value = spec["jobs"]
    if type(jobs_value) is not list:
        raise CampaignError("campaign spec.jobs must be an array")
    parsed_jobs = [JobSpec.from_dict(item) for item in jobs_value]
    expected_jobs = build_job_matrix(parameters, interpreters, inputs)
    if canonical_json_bytes([job.to_dict() for job in parsed_jobs]) != canonical_json_bytes(
        [job.to_dict() for job in expected_jobs]
    ):
        raise CampaignError("campaign job matrix differs from the exact ordered release matrix")
    if expected_digest != sha256_json(spec):
        raise CampaignError("campaign spec digest mismatch")
    # A canonical JSON round trip returns a detached, plain JSON object.
    return json.loads(canonical_json_bytes(campaign))


def _on_disk_campaign(campaign: Mapping[str, object]) -> dict[str, object]:
    if not isinstance(campaign, Mapping):
        raise CampaignError("campaign must be an object")
    keys = set(campaign)
    unknown = keys - _RUNTIME_CAMPAIGN_KEYS
    if unknown:
        rendered = sorted(repr(key) for key in unknown)
        raise CampaignError(f"runtime campaign has unknown keys: {rendered!r}")
    if "_path" in campaign:
        _require_string(campaign["_path"], "runtime campaign path")
    return {key: campaign[key] for key in _CAMPAIGN_KEYS if key in campaign}


def campaign_digest(campaign: Mapping[str, object]) -> str:
    """Validate a campaign (plus optional runtime path) and return its spec digest."""

    document = validate_campaign_document(_on_disk_campaign(campaign))
    return cast(str, document["spec_sha256"])


def campaign_jobs(campaign: Mapping[str, object]) -> tuple[JobSpec, ...]:
    """Return validated jobs from a campaign with optional ``_path`` metadata."""

    document = validate_campaign_document(_on_disk_campaign(campaign))
    spec = cast(dict[str, object], document["spec"])
    jobs = cast(list[Mapping[str, object]], spec["jobs"])
    return tuple(JobSpec.from_dict(value) for value in jobs)


def _campaign_job(campaign: Mapping[str, object], job: JobSpec | Mapping[str, object]) -> JobSpec:
    candidate = job if isinstance(job, JobSpec) else JobSpec.from_dict(job)
    matches = [item for item in campaign_jobs(campaign) if item.job_id == candidate.job_id]
    if len(matches) != 1 or matches[0].to_dict() != candidate.to_dict():
        raise CampaignError(f"job is not bound to campaign: {candidate.job_id}")
    return candidate


def _campaign_interpreter(campaign: Mapping[str, object], role: str) -> str:
    document = validate_campaign_document(_on_disk_campaign(campaign))
    spec = cast(dict[str, object], document["spec"])
    interpreters = cast(dict[str, Mapping[str, object]], spec["interpreters"])
    return _require_string(
        interpreters[role]["resolved_executable"],
        f"campaign interpreter {role}",
    )


def build_mega_argv(
    job: JobSpec | Mapping[str, object],
    attempt_dir: Path,
    campaign: Mapping[str, object],
) -> list[str]:
    """Build one exact, shell-free 06_mega invocation."""

    del attempt_dir  # output routing is carried by the bounded environment.
    spec = _campaign_job(campaign, job)
    if spec.kind == "bridge":
        raise CampaignError("bridge jobs do not use 06_mega.py")
    args = [
        _campaign_interpreter(campaign, "primary"),
        "-I",
        str(ROOT / "benchmarks" / "06_mega.py"),
        "--only",
        spec.corpus_key,
        "--threads",
        str(spec.threads),
        "--gtf-arm",
        spec.gtf_arm or "no-infer",
        "--legacy-timeout",
        str(spec.parameters["legacy_timeout"]),
        "--gffbase-timeout",
        str(spec.parameters["gffbase_timeout"]),
        "--validation-sample",
        str(spec.parameters["validation_sample"]),
        "--n-spatial",
        str(spec.parameters["n_spatial"]),
        "--n-batched",
        str(spec.parameters["n_batched"]),
        "--repeats",
        str(spec.parameters["repeats"]),
    ]
    if spec.kind == "scaling":
        args.append("--skip-legacy")
    if spec.gtf_arm == "parent-stripped":
        args.extend(["--gtf-input", str(spec.input["path"])])
    forbidden = {"--publish", "--rederive", "--keep-db", "--no-purge"}
    if forbidden & set(args):
        raise CampaignError("benchmark argv contains a forbidden harness flag")
    return args


def build_bridge_argv(
    job: JobSpec | Mapping[str, object],
    attempt_dir: Path,
    campaign: Mapping[str, object],
) -> list[str]:
    """Build one exact, shell-free historical-version bridge invocation."""

    spec = _campaign_job(campaign, job)
    if spec.kind != "bridge":
        raise CampaignError("only bridge jobs use bridge.py")
    attempt = Path(attempt_dir).resolve()
    return [
        _campaign_interpreter(campaign, spec.interpreter_role),
        "-I",
        str(ROOT / "benchmarks" / "bridge.py"),
        "--engine",
        str(spec.parameters["bridge_engine"]),
        "--input",
        str(spec.input["path"]),
        "--database",
        str(attempt / "scratch" / "bridge.duckdb"),
        "--output",
        str(attempt / "raw.json"),
        "--format",
        str(spec.input["format"]),
        "--threads",
        str(spec.threads),
        "--label",
        spec.job_id.removeprefix("bridge-"),
    ]


def build_worker_argv(
    campaign: Mapping[str, object], worker_id: str, *, resume: bool = False
) -> list[str]:
    """Build the digest-bound hidden worker invocation without probing disk."""

    validate_identifier(worker_id, "worker")
    if type(resume) is not bool:
        raise CampaignError("resume must be a boolean")
    allowed_workers = {f"scale-t{threads:02d}" for threads in THREADS} | {"canonical"}
    if worker_id not in allowed_workers:
        raise CampaignError(f"unknown campaign worker: {worker_id}")
    if "_path" not in campaign:
        raise CampaignError("campaign object lacks its runtime campaign path")
    path = Path(_require_string(campaign["_path"], "runtime campaign path")).resolve()
    args = [
        _campaign_interpreter(campaign, "primary"),
        "-I",
        str(ROOT / "benchmarks" / "cluster_campaign.py"),
        "_worker",
        "--campaign",
        str(path),
        "--worker-id",
        worker_id,
        "--campaign-sha256",
        campaign_digest(campaign),
    ]
    if resume:
        args.append("--resume")
    return args


def build_tmux_argv(
    session: str,
    cpu_list: str,
    worker_argv: Sequence[str],
    *,
    cwd: Path = ROOT,
) -> list[str]:
    """Build the deterministic one-pane tmux boundary used by compatibility code."""

    validate_identifier(session, "tmux session")
    parse_cpu_list(cpu_list)
    if isinstance(worker_argv, (str, bytes)) or not worker_argv:
        raise CampaignError("worker argv must be a non-empty string sequence")
    if any(type(value) is not str or not value for value in worker_argv):
        raise CampaignError("worker argv entries must be non-empty strings")
    command = shlex.join(["taskset", "--cpu-list", cpu_list, *worker_argv])
    return [
        "tmux",
        "new-session",
        "-d",
        "-s",
        session,
        "-c",
        str(Path(cwd).resolve()),
        command,
    ]


def worker_environment(
    campaign: Mapping[str, object],
    job: JobSpec | Mapping[str, object],
    attempt_dir: Path,
    *,
    base_environment: Mapping[str, str] | None = None,
) -> dict[str, str]:
    """Build a bounded worker environment without consulting ``os.environ``."""

    spec = _campaign_job(campaign, job)
    if base_environment is not None and not isinstance(base_environment, Mapping):
        raise CampaignError("base environment must be an object")
    env: dict[str, str] = {}
    for key, value in (base_environment or {}).items():
        if key not in _BASE_ENVIRONMENT_ALLOWLIST:
            continue
        if type(key) is not str or type(value) is not str or not value:
            raise CampaignError(f"invalid bounded environment entry: {key!r}")
        env[key] = value
    env.update(benchmark_env(spec.threads))
    document = validate_campaign_document(_on_disk_campaign(campaign))
    candidate = document["spec"]["candidate"]  # type: ignore[index]
    if not isinstance(candidate, Mapping) or not isinstance(candidate.get("wheel"), Mapping):
        raise CampaignError("campaign candidate wheel identity is missing")
    wheel = candidate["wheel"]
    # The PATH, not the name. `common._candidate_wheel_artifact` stats this
    # value and refuses to stamp an environment if it is not a file; a bare
    # filename resolves against the worker's cwd, which is the repo root, so
    # every job died after finishing its measurement with "candidate wheel path
    # is not a file". Preflight records both fields -- this is the one the
    # consumer can use.
    wheel_path = _require_string(wheel.get("path"), "campaign candidate wheel path")
    digest = _require_sha256(wheel.get("sha256"), "campaign candidate wheel sha256")
    env.update(
        {
            "GFFBASE_BENCH_OUT": str(Path(attempt_dir).resolve() / "scratch"),
            "GFFBASE_BENCH_WHEEL": wheel_path,
            "GFFBASE_BENCH_WHEEL_SHA256": digest,
        }
    )
    return env


__all__ = [
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
    "require_exact_keys",
    "require_schema",
    "sha256_json",
    "topology",
    "validate_campaign_document",
    "validate_identifier",
    "worker_environment",
]
