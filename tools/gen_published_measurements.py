#!/usr/bin/env python3
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
"""Derive the published measurement document from a finished campaign.

`cluster_campaign.py merge --publish` writes two things, and neither is what
the published tables render from. It writes a *portable projection* under
`benchmarks/results/platform/<key>/<run>/` for the results index, and that
projection is deliberately narrower than the evidence behind it: `_project_mega_row`
drops `input` and `db_paths`, two of the fourteen keys a schema-v3 row must
carry. Rendering a table from it is therefore impossible, and rendering one
from the *historical macOS artifact* -- four corpora, schema v2, provenance
naming a `gffbase 0.2.0` that was never built -- is what this replaces.

The run-local `campaign-results.json` retains every worker result whole. Each
primary job's `payload` is already exactly a schema-v3 corpus row, and its
`harness_environment` is already exactly the environment stamp, so the document
is a selection and an assembly rather than a transformation -- which is the
point: nothing here recomputes a number.

    python tools/gen_published_measurements.py --campaign <dir>/campaign-results.json --write
    python tools/gen_published_measurements.py --campaign <dir>/campaign-results.json --check
"""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Mapping
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from benchmarks.campaign.results import derive_linux_platform_key  # noqa: E402
from benchmarks.common import SCHEMA_VERSION, benchmark_results_evidence_error  # noqa: E402

RESULTS = ROOT / "benchmarks" / "results"

#: The macOS artifact, which this tool must never write to. Three separate
#: digests pin its bytes -- two in `gen_benchmark_tables.py`, one in
#: `campaign/results.py` -- and the campaign's results index registers it as a
#: legacy-opaque platform entry, re-checking the digest on every fresh publish.
HISTORICAL_MEGA_NAME = "06_mega.json"


class DerivationError(RuntimeError):
    """The campaign record cannot produce a publishable document."""


def _spec_jobs(record: Mapping) -> list[Mapping]:
    campaign = record["campaign"]
    spec = campaign["spec"] if isinstance(campaign, Mapping) and "spec" in campaign else campaign
    return list(spec["jobs"])


def _primary_result_keys(record: Mapping) -> dict[str, str]:
    """`job_id -> result_key` for the jobs whose rows may be published.

    Selection is on `primary_eligible`, never on the corpus key: a scaling job
    carries the SAME `payload["key"]` as the canonical row for that corpus, so
    keying on it would publish a thread-sweep row as the headline number. The
    controls are the mirror image -- a primary corpus under its own result key.
    """
    return {
        str(job["job_id"]): str(job["result_key"])
        for job in _spec_jobs(record)
        if job.get("primary_eligible")
    }


def derive_document(record: Mapping) -> dict:
    """Assemble a schema-v3 measurement document from a run-local record."""

    primaries = _primary_result_keys(record)
    if not primaries:
        raise DerivationError("campaign record names no primary-eligible job")
    results = record["worker_results"]

    corpora: dict[str, object] = {}
    environments: list[tuple[str, Mapping]] = []
    for job_id, result_key in sorted(primaries.items()):
        result = results.get(job_id)
        if not isinstance(result, Mapping) or not result.get("payload"):
            raise DerivationError(f"{job_id} has no payload; the campaign is not complete")
        corpora[result_key] = result["payload"]
        environment = result.get("harness_environment") or {}
        if not environment:
            raise DerivationError(f"{job_id} carries no harness environment")
        environments.append((str(environment["timestamp_utc"]), environment))

    # Every row must be measured no later than the environment stamp, and the
    # canonical jobs run sequentially over hours -- so the first job's
    # environment cannot describe the last job's row. Timestamps are
    # `%Y-%m-%dT%H:%M:%SZ`, which sorts lexically.
    environment = max(environments, key=lambda item: item[0])[1]
    return {
        "schema_version": SCHEMA_VERSION,
        "environment": dict(environment),
        "corpora": corpora,
    }


def platform_key(record: Mapping) -> str:
    platform = record["platform"]
    return derive_linux_platform_key(platform["platform"], platform["machine"])


def output_path(record: Mapping) -> Path:
    return RESULTS / f"06_mega.{platform_key(record)}.json"


def _serialize(document: Mapping) -> str:
    return json.dumps(document, indent=2, sort_keys=True) + "\n"


def write_document(document: Mapping, target: Path) -> None:
    """Validate, then write. A document that fails the contract is not written.

    The evidence gate is the same one `gen_benchmark_tables.py` applies before
    rendering, so a file that lands here is one the tables can already be
    generated from -- rather than one that fails at render time, after it has
    replaced the artifact it supersedes.
    """
    if target.name == HISTORICAL_MEGA_NAME:
        raise DerivationError(
            f"refusing to overwrite the historical macOS artifact at {target.name}; "
            "it is byte-pinned by three digests and the campaign results index"
        )
    error = benchmark_results_evidence_error(dict(document))
    if error:
        raise DerivationError(f"derived document is not publishable: {error}")
    target.write_text(_serialize(document), encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--campaign",
        type=Path,
        required=True,
        help="path to a finished campaign's campaign-results.json",
    )
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--write", action="store_true", help="write the derived document")
    group.add_argument("--check", action="store_true", help="derive and validate without writing")
    args = parser.parse_args()

    try:
        record = json.loads(args.campaign.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        print(f"cannot read {args.campaign}: {exc}", file=sys.stderr)
        return 1

    try:
        document = derive_document(record)
        target = output_path(record)
        if args.check:
            error = benchmark_results_evidence_error(document)
            if error:
                raise DerivationError(f"derived document is not publishable: {error}")
            print(f"derived a publishable document for {target.name}")
            return 0
        write_document(document, target)
    except (DerivationError, KeyError) as exc:
        print(f"refusing to publish: {exc}", file=sys.stderr)
        return 1

    print(f"wrote {target.relative_to(ROOT)} ({len(document['corpora'])} corpora)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
