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
"""The benchmark harness produces the published numbers, so it is testable code.

Nothing here runs a benchmark -- the sweep takes hours. These pin the
properties that decide whether a published number means anything:

* a speedup is only reported when both engines did the SAME work;
* a repeated measurement records a real spread, and a single one does not
  pretend to;
* the committed results file describes a measurement, not a machine.
"""

from __future__ import annotations

import hashlib
import json
import re
import subprocess
import sys
import textwrap
from pathlib import Path
from types import SimpleNamespace

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
MEGA = REPO_ROOT / "benchmarks" / "06_mega.py"
RESULTS = REPO_ROOT / "benchmarks" / "results" / "06_mega.json"


def _source() -> str:
    return MEGA.read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# The documented interface has to exist
# ---------------------------------------------------------------------------


def test_the_harness_imports_without_the_bench_extras():
    """`--help` must not need a measurement library.

    `benchmarks/common.py` imported `psutil` at module scope, so importing the
    harness at all -- including to print its usage -- failed without the
    `bench` extra. The test job installs `[test,all]`, so this took out 15 of
    18 CI jobs. `psutil` is now imported inside the two functions that measure
    with it.
    """
    code = textwrap.dedent("""
        import sys
        class Blocker:
            def find_module(self, name, path=None):
                if name == "psutil":
                    return self
            def load_module(self, name):
                raise ImportError("No module named 'psutil'")
        sys.meta_path.insert(0, Blocker())
        import benchmarks.common          # noqa: F401
        print("imported")
    """)
    proc = subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True,
        text=True,
        cwd=REPO_ROOT,
        timeout=120,
    )
    assert proc.returncode == 0, (
        f"benchmarks.common cannot be imported without psutil:\n{proc.stderr}"
    )


@pytest.mark.parametrize(
    "flag", ["--repeats", "--publish", "--only", "--legacy-timeout", "--validation-sample"]
)
def test_the_documented_flags_exist(flag):
    """`docs/performance/methodology.md` prints commands a reader will paste.

    It documented `--repeats 5` for months while the flag did not exist, so
    the one command offered for measuring uncertainty died on an argparse
    error. Documentation that cannot be run is worse than none.
    """
    proc = subprocess.run(
        [sys.executable, str(MEGA), "--help"], capture_output=True, text=True, timeout=120
    )
    assert proc.returncode == 0, proc.stderr
    assert flag in proc.stdout


def test_every_command_in_the_methodology_page_parses():
    """Extract the harness invocations from the docs and check argparse accepts them."""
    page = (REPO_ROOT / "docs" / "performance" / "methodology.md").read_text(encoding="utf-8")
    commands = re.findall(r"^python benchmarks/06_mega\.py (.+)$", page, re.M)
    assert commands, "no 06_mega.py invocations found in the methodology page"

    for args in commands:
        proc = subprocess.run(
            [sys.executable, str(MEGA), *args.split(), "--help"],
            capture_output=True,
            text=True,
            timeout=120,
        )
        assert proc.returncode == 0, f"documented command rejected: {args}\n{proc.stderr}"


def test_generator_check_runs_from_the_documented_repository_root():
    proc = subprocess.run(
        [sys.executable, "tools/gen_benchmark_tables.py", "--check"],
        capture_output=True,
        text=True,
        cwd=REPO_ROOT,
        timeout=120,
    )
    assert proc.returncode == 0, proc.stderr


# ---------------------------------------------------------------------------
# Fairness
# ---------------------------------------------------------------------------


def _mega_module():
    """Import the harness without executing its `main`."""
    import importlib.util

    spec = importlib.util.spec_from_file_location("_mega", MEGA)
    module = importlib.util.module_from_spec(spec)
    sys.modules["_mega"] = module
    spec.loader.exec_module(module)
    return module


def _derive_speedup():
    return _mega_module().derive_speedup


def _signature(seed: str = "a") -> dict:
    """Independent v3 fixture; expected values do not call production code."""

    def digest(label: str) -> str:
        return hashlib.sha256(f"{seed}:{label}".encode()).hexdigest()

    payload = {
        "schema_version": "database-signature-v3",
        "segment_count": 1000,
        "segments_sha256": digest("segments"),
        "attribute_count": 2000,
        "attributes_sha256": digest("attributes"),
        "direct_relationship_count": 500,
        "direct_relationships_sha256": digest("direct"),
        "closure_count": 700,
        "closure_sha256": digest("closure"),
        "feature_count": 1000,
        "featuretype_histogram": [["gene", 1000]],
    }
    return {
        **payload,
        "combined_sha256": hashlib.sha256(
            json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest(),
    }


def _candidate(*, signature: dict | None = None, wall_seconds: float = 100.0) -> dict:
    return {
        "state": "completed",
        "exit_code": 0,
        "wall_seconds": wall_seconds,
        "n_features": 1000,
        "rtree_built": False,
        "correctness_signature": _signature() if signature is None else signature,
        "validation": {
            "ok": True,
            "level": "full",
            "errors": [],
            "skipped": ["INV-8 (bbox_matches): no R-tree was built for this database"],
            "requested_sample": "all",
            "sample_eligible": 1000,
            "sample_checked": 1000,
            "checked_ids": [
                "INV-1",
                "INV-2",
                "INV-3",
                "INV-4",
                "INV-5",
                "INV-6",
                "INV-7",
                "INV-9",
                "INV-10",
                "INV-11",
                "INV-11b",
                "INV-13",
                "INV-14",
                "INV-15",
                "INV-15a",
                "INV-16",
                "INV-11-exact",
                "INV-12",
            ],
        },
    }


def _comparator(*, signature: dict | None = None, wall_seconds: float = 200.0) -> dict:
    return {
        "state": "completed",
        "exit_code": 0,
        "wall_seconds": wall_seconds,
        "n_features": 1000,
        "correctness_signature": _signature() if signature is None else signature,
    }


def test_a_speedup_requires_both_engines_to_have_done_equal_work():
    """A ratio between two different workloads is not a speedup.

    Both feature counts were recorded and then never compared, so a corpus
    where they diverged -- a differing duplicate-ID policy, a parent-synthesis
    difference on GTF -- would have published a headline number comparing two
    different jobs.
    """
    derive = _derive_speedup()
    speedup, bound, conflict = derive(
        _candidate(),
        {**_comparator(), "n_features": 999},
    )
    assert conflict is True
    assert speedup is None and bound is None, "a conflicting count still produced a ratio"

    speedup, bound, conflict = derive(
        _candidate(),
        _comparator(),
    )
    assert conflict is False
    assert speedup == pytest.approx(2.0) and bound is None


def test_a_capped_legacy_run_keeps_only_its_raw_wall_bound():
    """A killed comparator has no completed database/signature for a ratio."""
    derive = _derive_speedup()
    speedup, bound, conflict = derive(
        _candidate(wall_seconds=245.1),
        {"state": "timed_out", "wall_seconds": None, "cap_seconds": 5400.0, "n_features": None},
    )
    assert conflict is False
    assert speedup is None, "a capped run must not produce a measured speedup"
    assert bound is None, "a timeout without a correctness signature must not produce a floor"


@pytest.mark.parametrize(
    "legacy_signature",
    [None, _signature("b")],
)
def test_missing_or_false_signature_suppresses_a_ratio(legacy_signature):
    derive = _derive_speedup()
    comparator = _comparator(wall_seconds=20.0)
    comparator["correctness_signature"] = legacy_signature
    speedup, bound, _ = derive(
        _candidate(signature=_signature("a"), wall_seconds=10.0),
        comparator,
    )
    assert speedup is None and bound is None


def test_candidate_requires_exhaustive_validation_and_completed_comparator():
    derive = _derive_speedup()
    sampled = _candidate()
    sampled["validation"] = {**sampled["validation"], "requested_sample": "10000"}
    speedup, bound, _ = derive(sampled, _comparator())
    assert speedup is None and bound is None

    speedup, bound, _ = derive(
        _candidate(),
        {**_comparator(), "state": "timed_out", "wall_seconds": None, "cap_seconds": 5},
    )
    assert speedup is None and bound is None


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("checked_ids", ["INV-12"]),
        ("errors", [{"invariant": "INV-X"}]),
        ("skipped", ["INV-8"]),
        ("sample_checked", 999),
        ("sample_eligible", True),
    ],
)
def test_candidate_gate_rejects_forged_validation_evidence(field, value):
    module = _mega_module()
    candidate = _candidate()
    candidate["validation"] = {**candidate["validation"], field: value}

    assert module._candidate_is_valid(candidate) is False


def test_candidate_gate_accepts_exact_numeric_and_synthetic_parent_coverage():
    module = _mega_module()
    candidate = _candidate()
    candidate["validation"] = {
        **candidate["validation"],
        "requested_sample": "10000",
        "sample_eligible": 1,
        "sample_checked": 1,
    }

    assert module._candidate_is_valid(candidate) is True
    assert module._candidate_is_valid(candidate, require_exhaustive=True) is False


def test_failed_candidate_never_runs_query_measurements(tmp_path, monkeypatch):
    module = _mega_module()
    source = tmp_path / "tiny.gff3"
    source.write_text("chr1\ts\tgene\t1\t2\t.\t+\t.\tID=g\n")
    module.OUT = tmp_path / "out"
    module.OUT.mkdir()
    monkeypatch.setattr(module, "require_free_disk", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(module, "count_feature_lines", lambda _path: 1)
    monkeypatch.setattr(
        module,
        "run_subprocess",
        lambda *_args, **_kwargs: {
            "state": "failed",
            "exit_code": 2,
            "wall_seconds": None,
            "cap_seconds": 10,
            "peak_rss_bytes": 0,
        },
    )

    def forbidden(*_args, **_kwargs):
        raise AssertionError("query measurement ran after candidate failure")

    monkeypatch.setattr(module, "sample_regions_from_db", forbidden)
    monkeypatch.setattr(module, "bench_batched", forbidden)
    args = SimpleNamespace(
        gtf_arm="no-infer",
        gtf_input=None,
        threads=1,
        validation_sample_value=None,
        validation_sample="all",
        gffbase_timeout=10,
        skip_legacy=True,
        legacy_timeout=10,
        n_spatial=5,
        n_batched=5,
        repeats=1,
    )

    result = module.run_one({"name": "tiny", "key": "tiny", "input": source, "fmt": "gff3"}, args)

    assert result["spatial"] == {
        "state": "skipped",
        "reason": "candidate completion/validation/signature/R-tree failed",
    }
    assert result["batched"] == {
        "state": "skipped",
        "reason": "candidate completion/validation/signature failed",
    }


def test_subprocess_states_are_authoritative_and_censored():
    from benchmarks.common import run_subprocess

    completed = run_subprocess(
        'import json; print(json.dumps({"wall_seconds": 0.1, "n_features": 1}))',
        label="completed",
        timeout=5,
    )
    failed = run_subprocess(
        'import json, sys; print(json.dumps({"wall_seconds": 99, "n_features": 1})); sys.exit(3)',
        label="failed",
        timeout=5,
    )
    timed_out = run_subprocess(
        "import time; time.sleep(2)",
        label="timed-out",
        timeout=1,
    )

    assert completed["state"] == "completed" and completed["exit_code"] == 0
    assert completed["wall_seconds"] == 0.1 and completed["cap_seconds"] == 5
    assert failed["state"] == "failed" and failed["exit_code"] == 3
    assert timed_out["state"] == "timed_out" and timed_out["exit_code"] != 0
    for censored in (failed, timed_out):
        assert censored["wall_seconds"] is None
        assert censored["cap_seconds"] > 0
        assert "timed_out" not in censored
        assert "wall_seconds_lower_bound" not in censored


def test_failed_subprocess_payloads_never_retain_private_stdout():
    """Portable evidence may identify a parse failure, never echo child output."""
    from benchmarks.common import run_subprocess

    failed = run_subprocess(
        'import sys; print("/home/private-user/secret/input.gff3"); sys.exit(2)',
        label="private-output",
        timeout=5,
    )

    assert failed["state"] == "failed"
    assert "raw_stdout_tail" not in failed
    assert "/home/private-user" not in json.dumps(failed)
    assert isinstance(failed["stdout_bytes"], int)
    assert re.fullmatch(r"[0-9a-f]{64}", failed["stdout_sha256"])


def test_schema3_renderer_censors_timeouts_and_rejects_speedup_floors():
    from tools import gen_benchmark_tables as tables

    row = {
        "key": "gencode-gtf",
        "legacy": {
            "state": "timed_out",
            "wall_seconds": None,
            "cap_seconds": 5400,
        },
        "ingest_speedup": None,
    }
    tables._check_no_invented_numbers(row, "3")
    assert tables._legacy_cell(row, "3") == "censored at 1 hr 30 min"
    assert tables._speedup_cell(row) == "—"

    stale = {**row, "ingest_speedup_lower_bound": 20.0}
    with pytest.raises(tables.Stale, match="stale fields"):
        tables._check_no_invented_numbers(stale, "3")


def test_schema3_renderer_rejects_forged_candidate_speedup_evidence():
    from tools import gen_benchmark_tables as tables

    row = {
        "key": "gencode-gtf",
        "gffbase": _candidate(),
        "legacy": _comparator(),
        "ingest_speedup": 999.0,
    }
    data = {"schema_version": "3", "corpora": {"gencode-gtf": row}}

    with pytest.raises(tables.Stale, match="speedup"):
        tables.render_corpus_table(data)


def test_schema2_historical_floor_is_never_rendered_as_a_claim():
    from tools import gen_benchmark_tables as tables

    row = {
        "key": "gencode-gtf",
        "legacy": {
            "timed_out": True,
            "wall_seconds": None,
            "wall_seconds_lower_bound": 5400.0,
        },
        "ingest_speedup": None,
        "ingest_speedup_lower_bound": 22.0,
    }

    tables._check_no_invented_numbers(row, "2")
    assert tables._legacy_cell(row, "2") == "censored at 1 hr 30 min"
    assert tables._speedup_cell(row) == "—"


def test_harness_contract_has_canonical_validation_and_no_timeout_floor_language():
    src = _source()
    assert 'default="all"' in src
    assert "validation_sample_value" in src
    assert "checked_ids" in src and "sample_checked" in src
    assert "wall_seconds_lower_bound" not in src


def test_complete_benchmark_environment_is_recorded_and_portable(monkeypatch):
    from benchmarks.common import benchmark_env, environment

    expected = {
        "GFFBASE_THREADS",
        "GFFUTILS2_THREADS",
        "OMP_NUM_THREADS",
        "OPENBLAS_NUM_THREADS",
        "MKL_NUM_THREADS",
        "NUMEXPR_NUM_THREADS",
        "VECLIB_MAXIMUM_THREADS",
        "BLIS_NUM_THREADS",
        "DUCKDB_DISABLE_PROGRESS_BAR",
        "PYTHONUNBUFFERED",
    }
    assert set(benchmark_env(3)) == expected
    monkeypatch.setenv("GFFBASE_THREADS", "4")
    payload = environment()
    assert payload["env"] == benchmark_env(4)
    assert payload["python"]["executable"].find("/") == -1
    install = payload["gffbase_install"]
    for key in ("python_module", "native_module"):
        if key in install:
            assert "/" not in install[key]


def test_candidate_without_rtree_is_not_eligible_for_spatial_work():
    module = _mega_module()
    candidate = _candidate()
    candidate["rtree_built"] = False

    assert module._candidate_is_valid(candidate) is True
    assert module._candidate_is_valid(candidate, require_rtree=True) is False


def test_run_one_skips_spatial_but_keeps_batched_work_without_an_rtree(tmp_path, monkeypatch):
    module = _mega_module()
    source = tmp_path / "tiny.gff3"
    source.write_text("chr1\ts\tgene\t1\t2\t.\t+\t.\tID=g\n")
    module.OUT = tmp_path / "out"
    module.OUT.mkdir()
    monkeypatch.setattr(module, "require_free_disk", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(module, "count_feature_lines", lambda _path: 1)

    def completed_candidate(*_args, **_kwargs):
        (module.OUT / "tiny.duckdb").touch()
        return {**_candidate(), "peak_rss_bytes": 0}

    monkeypatch.setattr(module, "run_subprocess", completed_candidate)
    monkeypatch.setattr(module, "database_signature", lambda *_args, **_kwargs: _signature())
    monkeypatch.setattr(
        module,
        "sample_regions_from_db",
        lambda *_args, **_kwargs: pytest.fail("spatial work ran without an R-tree"),
    )
    monkeypatch.setattr(
        module,
        "bench_batched",
        lambda *_args, **_kwargs: {
            "state": "completed",
            "wall_seconds": 1.0,
            "qps": 1.0,
            "n_anchors": 1,
            "n_descendants": 1,
        },
    )
    args = SimpleNamespace(
        gtf_arm="no-infer",
        gtf_input=None,
        threads=1,
        validation_sample_value=None,
        validation_sample="all",
        gffbase_timeout=10,
        skip_legacy=True,
        legacy_timeout=10,
        n_spatial=5,
        n_batched=5,
        repeats=1,
    )

    result = module.run_one({"name": "tiny", "key": "tiny", "input": source, "fmt": "gff3"}, args)

    assert result["spatial"]["state"] == "skipped"
    assert "R-tree" in result["spatial"]["reason"]
    assert result["batched"]["state"] == "completed"


def test_shared_publisher_gate_rejects_failed_forged_and_private_schema3_rows():
    from benchmarks.common import benchmark_results_evidence_error

    valid_row = {
        "key": "gencode-gtf",
        "gffbase": _candidate(),
        "legacy": _comparator(),
        "ingest_speedup": 2.0,
    }
    valid = {"schema_version": "3", "corpora": {"gencode-gtf": valid_row}}
    assert benchmark_results_evidence_error(valid) is None

    failed = json.loads(json.dumps(valid))
    failed["corpora"]["gencode-gtf"]["gffbase"]["state"] = "failed"
    assert "candidate" in benchmark_results_evidence_error(failed)

    forged = json.loads(json.dumps(valid))
    forged["corpora"]["gencode-gtf"]["ingest_speedup"] = 999.0
    assert "speedup" in benchmark_results_evidence_error(forged)

    private = json.loads(json.dumps(valid))
    private["corpora"]["gencode-gtf"]["gffbase"]["stdout_path"] = "/home/private-user/log"
    assert "absolute path" in benchmark_results_evidence_error(private)
    assert "benchmark_results_evidence_error(merged)" in _source()


def test_both_engines_are_given_the_same_duplicate_id_policy():
    """The axis that decides whether the run completes at all must match."""
    src = _source()
    assert src.count('merge_strategy="create_unique"') >= 2


def test_the_benchmark_handles_are_closed():
    """A writable DuckDB handle holds an exclusive lock, and the sweep purges
    each corpus as soon as its numbers are recorded. Leaking the handle blocks
    the delete outright on Windows."""
    src = _source()
    assert "with gffbase.FeatureDB(str(db_path), read_only=True) as db:" in src
    assert "db.close()" in src, "bench_batched must release its handle"


# ---------------------------------------------------------------------------
# The committed results file
# ---------------------------------------------------------------------------


@pytest.mark.skipif(not RESULTS.is_file(), reason="no committed results yet")
def test_committed_results_carry_no_absolute_home_paths():
    """The published artifact should describe a measurement, not a machine."""
    text = RESULTS.read_text(encoding="utf-8")
    for leak in ("/Users/", "/home/", "C:\\Users"):
        assert leak not in text, (
            f"{RESULTS.name} contains {leak!r} — paths should be repo-relative "
            "so the file describes the run rather than whoever made it"
        )


@pytest.mark.skipif(not RESULTS.is_file(), reason="no committed results yet")
def test_historical_result_is_explicitly_legacy_and_never_invents_a_wall():
    """The immutable Mac artifact predates v3 and is handled as opaque history."""
    data = json.loads(RESULTS.read_text(encoding="utf-8"))
    assert data.get("schema_version") == "2"
    for key, row in (data.get("corpora") or {}).items():
        legacy = row.get("legacy") or {}
        if legacy.get("timed_out"):
            assert legacy.get("wall_seconds") is None, (
                f"{key}: legacy timed out but carries wall_seconds — that number "
                "was synthesized, not measured"
            )
            assert legacy.get("wall_seconds_lower_bound"), f"{key}: legacy cap evidence missing"


@pytest.mark.skipif(not RESULTS.is_file(), reason="no committed results yet")
def test_a_single_sample_is_never_recorded_as_a_median():
    """`repeat()` deliberately omits `median` at n=1 so a renderer physically
    cannot present one sample as a central tendency."""
    data = json.loads(RESULTS.read_text(encoding="utf-8"))
    for key, row in (data.get("corpora") or {}).items():
        for section in ("spatial", "batched"):
            timing = (row.get(section) or {}).get("timing")
            if not timing:
                continue
            if timing.get("n") == 1:
                assert "median" not in timing, f"{key}.{section}: n=1 carries a median"
                assert "value" in timing
            else:
                assert {"median", "min", "max", "n"} <= set(timing), (
                    f"{key}.{section}: a repeated measurement must record its spread"
                )
