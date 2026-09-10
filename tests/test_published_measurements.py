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
"""Deriving the published measurement document from a finished campaign.

The campaign publishes a *portable projection* for the results index, which is
deliberately narrower than the evidence it was projected from -- it drops
`input` and `db_paths`, two of the fourteen keys a schema-v3 row must carry. So
the published tables cannot be rendered from it, and the run-local record, which
retains every worker result whole, is the source these tests pin.
"""

from __future__ import annotations

import copy
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tools import gen_published_measurements as gpm  # noqa: E402


def _record() -> dict:
    """A run-local campaign record, reduced to what the derivation reads."""

    def job(job_id: str, result_key: str, *, primary: bool) -> dict:
        return {
            "job_id": job_id,
            "result_key": result_key,
            "kind": "primary" if primary else "scaling",
            "primary_eligible": primary,
        }

    def result(stamp: str, key: str) -> dict:
        return {
            "payload": {"key": key, "measured": {"timestamp_utc": stamp}},
            "harness_environment": {"timestamp_utc": stamp, "marker": key},
        }

    return {
        "platform": {"platform": "Linux", "machine": "x86_64"},
        "campaign": {
            "spec": {
                "jobs": [
                    job("canonical-mane", "mane", primary=True),
                    job("canonical-chess", "chess", primary=True),
                    # Same corpus key as the canonical row, and not publishable.
                    job("scaling-t10-mane", "mane", primary=False),
                    # A primary corpus name, but its own result key.
                    job("control-gencode-gtf-default", "gencode-gtf-default", primary=False),
                ]
            }
        },
        "worker_results": {
            "canonical-mane": result("2026-09-10T06:30:08Z", "mane"),
            "canonical-chess": result("2026-09-10T07:41:02Z", "chess"),
            "scaling-t10-mane": result("2026-09-10T09:00:00Z", "mane"),
            "control-gencode-gtf-default": result("2026-09-10T09:30:00Z", "gencode-gtf-default"),
        },
    }


def test_only_primary_eligible_rows_are_published():
    """`primary_eligible` is the flag, not the corpus key.

    A scaling job carries the SAME `payload["key"]` as the canonical row for
    that corpus -- `scaling-t10-mane` says `mane` -- so selecting on the key
    would publish a thread-sweep row as the headline measurement. A control job
    is the mirror image: a primary corpus under its own result key.
    """
    document = gpm.derive_document(_record())

    assert set(document["corpora"]) == {"mane", "chess"}
    assert document["corpora"]["mane"]["measured"]["timestamp_utc"] == "2026-09-10T06:30:08Z"
    assert document["schema_version"] == "3"


def test_the_environment_is_the_latest_finishing_primary_job():
    """Every row must be measured no later than the environment stamp.

    `benchmark_results_evidence_error` rejects a document where any row's
    `measured.timestamp_utc` is after the environment's, and the five canonical
    jobs run sequentially over hours -- so the first job's environment cannot
    describe the last job's row.
    """
    document = gpm.derive_document(_record())

    assert document["environment"]["marker"] == "chess", "took an earlier job's environment"
    latest = max(row["measured"]["timestamp_utc"] for row in document["corpora"].values())
    assert document["environment"]["timestamp_utc"] >= latest


def test_a_missing_primary_result_is_refused_rather_than_partially_published():
    """Four corpora is not the five the evidence contract requires."""
    record = copy.deepcopy(_record())
    del record["worker_results"]["canonical-chess"]

    with pytest.raises(gpm.DerivationError, match="canonical-chess"):
        gpm.derive_document(record)


def test_the_platform_key_names_the_file():
    assert gpm.platform_key(_record()) == "linux-x86_64"
    assert gpm.output_path(_record()).name == "06_mega.linux-x86_64.json"


def test_a_document_the_evidence_contract_rejects_is_never_written(tmp_path):
    """The tool's whole job is to refuse. The stub rows above are not valid
    schema-v3 evidence, and writing them would put unpublishable numbers where
    the table generator looks."""
    target = tmp_path / "06_mega.linux-x86_64.json"

    with pytest.raises(gpm.DerivationError, match="not publishable"):
        gpm.write_document(gpm.derive_document(_record()), target)

    assert not target.exists()


def test_the_historical_macos_artifact_is_never_the_target(tmp_path):
    """It is pinned by three separate digests and a results-index entry."""
    with pytest.raises(gpm.DerivationError, match="historical"):
        gpm.write_document({"schema_version": "3"}, tmp_path / "06_mega.json")


def test_the_generator_and_this_tool_agree_on_where_the_file_goes():
    """Two modules, one location: the generator renders what this writes."""
    from tools import gen_benchmark_tables as tables

    assert gpm.output_path(_record()).parent == tables.RESULTS


def test_the_documents_json_is_canonical_and_reproducible():
    """Byte-identical output for identical input, so a re-derivation that
    changes nothing produces no diff to review."""
    first = json.dumps(gpm.derive_document(_record()), indent=2, sort_keys=True)
    second = json.dumps(gpm.derive_document(_record()), indent=2, sort_keys=True)
    assert first == second
