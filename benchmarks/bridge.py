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
"""Cheap cross-version bridge ingest for MANE/CHESS.

Run this script with an interpreter from an isolated environment containing
either public gffbase 0.1.0 or gffutils 0.13.  It does not compare timings by
itself; the campaign merger connects these rows to the candidate/0.14 rows by
input checksum and correctness signature.
"""

from __future__ import annotations

import argparse
import importlib.metadata
import json
import os
import platform
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from benchmarks.common import (
    atomic_write_json,
    benchmark_env,
    database_signature,
    du,
    sha256_file,
)

BRIDGE_SCHEMA = "benchmark-bridge-v2"
BRIDGE_ENVIRONMENT_SCHEMA = "benchmark-bridge-environment-v1"


def bridge_environment(*, package: str, package_version: str, threads: int) -> dict:
    """Return closed provenance that works in either historical environment.

    The primary harness environment intentionally requires the current
    gffbase wheel and gffutils 0.14.  Calling it from a historical bridge made
    the gffbase-0.1.0 and gffutils-0.13 jobs impossible by construction.  A
    bridge records only its selected package plus the interpreter, affinity,
    platform, and exact bounded controls; the campaign worker independently
    binds all of those fields to its immutable preflight probe.
    """

    try:
        affinity = sorted(os.sched_getaffinity(0))
    except (AttributeError, OSError) as exc:
        raise RuntimeError("bridge requires an observable Linux CPU affinity") from exc
    return {
        "schema_version": BRIDGE_ENVIRONMENT_SCHEMA,
        "timestamp_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "hostname": platform.node(),
        "platform": platform.platform(),
        "machine": platform.machine(),
        "python": {
            "version": platform.python_version(),
            "implementation": platform.python_implementation(),
            "executable": Path(sys.executable).name,
        },
        "package": {"name": package, "version": package_version},
        "cpu_affinity": affinity,
        "benchmark_env": benchmark_env(threads),
    }


def run_bridge(
    *,
    engine: str,
    input_path: Path,
    database: Path,
    output: Path,
    fmt: str,
    threads: int,
    label: str,
) -> dict:
    if threads < 1:
        raise ValueError("threads must be >= 1")
    os.environ["GFFBASE_THREADS"] = str(threads)
    os.environ["GFFUTILS2_THREADS"] = str(threads)
    input_path = input_path.resolve()
    database = database.resolve()
    database.parent.mkdir(parents=True, exist_ok=True)
    for suffix in ("", ".wal", ".tmp", "-journal", "-wal", "-shm"):
        candidate = Path(str(database) + suffix)
        if candidate.is_file():
            candidate.unlink()

    if engine == "gffbase":
        import gffbase

        package_version = importlib.metadata.version("gffbase")
        module_version = getattr(gffbase, "__version__", None)
        if module_version is not None and module_version != package_version:
            raise RuntimeError(
                "gffbase distribution/module version mismatch: "
                f"{package_version!r} != {module_version!r}"
            )

        t0 = time.perf_counter()
        db = gffbase.create_db(
            str(input_path),
            str(database),
            force=True,
            merge_strategy="create_unique",
            force_gff=fmt != "gtf",
            pragmas={"threads": threads},
        )
        wall = time.perf_counter() - t0
        n_features = db.count_features_of_type()
        validation = None
        if hasattr(db, "validate"):
            report = db.validate(level="full")
            validation = {
                "ok": report.ok,
                "checked": list(report.checked),
                "errors": [str(item) for item in report.errors],
            }
        if hasattr(db, "close"):
            db.close()
    else:
        import gffutils

        t0 = time.perf_counter()
        db = gffutils.create_db(
            str(input_path),
            str(database),
            force=True,
            keep_order=False,
            sort_attribute_values=False,
            merge_strategy="create_unique",
            verbose=False,
        )
        wall = time.perf_counter() - t0
        n_features = db.count_features_of_type()
        validation = None
        package_version = importlib.metadata.version("gffutils")

    signature = database_signature(database, engine=engine)
    payload = {
        "schema_version": BRIDGE_SCHEMA,
        "label": label,
        "engine": engine,
        "package_version": package_version,
        "input": {
            "path": str(input_path),
            "bytes": input_path.stat().st_size,
            "sha256": sha256_file(input_path),
        },
        "database": {"path": str(database), "bytes": du(database)},
        "measurement": {
            "wall_seconds": wall,
            "n_features": n_features,
            "correctness_signature": signature,
            "validation": validation,
        },
        "params": {"fmt": fmt, "threads": threads},
        "environment": bridge_environment(
            package=engine,
            package_version=package_version,
            threads=threads,
        ),
    }
    atomic_write_json(output, payload)
    return payload


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--engine", choices=("gffbase", "gffutils"), required=True)
    parser.add_argument("--input", type=Path, required=True)
    parser.add_argument("--database", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--format", choices=("gff3", "gtf"), default="gff3")
    parser.add_argument("--threads", type=int, default=1)
    parser.add_argument("--label", required=True)
    args = parser.parse_args()
    if args.threads < 1:
        parser.error("--threads must be >= 1")
    os.environ["GFFBASE_THREADS"] = str(args.threads)
    os.environ["GFFUTILS2_THREADS"] = str(args.threads)
    result = run_bridge(
        engine=args.engine,
        input_path=args.input,
        database=args.database,
        output=args.output,
        fmt=args.format,
        threads=args.threads,
        label=args.label,
    )
    print(
        json.dumps(
            {"output": str(args.output), "wall_seconds": result["measurement"]["wall_seconds"]}
        )
    )


if __name__ == "__main__":
    main()
