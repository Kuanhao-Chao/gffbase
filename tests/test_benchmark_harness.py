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
import stat
import struct
import subprocess
import sys
import textwrap
import zipfile
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


def test_clean_snapshot_contains_the_canonical_corpus_registry_and_harness_help():
    from benchmarks.corpora import CORPORA

    assert CORPORA == (
        {
            "name": "MANE v1.5 (Ensembl IDs)",
            "key": "mane",
            "filename": "MANE.GRCh38.v1.5.ensembl_genomic.gff.gz",
            "fmt": "gff3",
            "bytes": 10_349_746,
            "sha256": "69089bbc84d1d3c3ce31c2ed3f85b6c3169fb8836d092a082623c59a43fd22ef",
            "url": "https://ftp.ncbi.nlm.nih.gov/refseq/MANE/MANE_human/release_1.5/MANE.GRCh38.v1.5.ensembl_genomic.gff.gz",
        },
        {
            "name": "CHESS 3.1.3",
            "key": "chess",
            "filename": "chess3.1.3.GRCh38.gff.gz",
            "fmt": "gff3",
            "bytes": 20_435_645,
            "sha256": "28da847be976780fe38162a7c244749fdc7a0b446741ca8da2b64019c1606e03",
            "url": "https://github.com/chess-genome/chess/releases/download/v.3.1.3/chess3.1.3.GRCh38.gff.gz",
        },
        {
            "name": "RefSeq GRCh38.p14",
            "key": "refseq",
            "filename": "GCF_000001405.40_GRCh38.p14_genomic.gff.gz",
            "fmt": "gff3",
            "bytes": 78_190_483,
            "sha256": "4920f0eae7e2197c50b67a201e06d657387137b49dd60f474b4f1d5b29334051",
            "url": "https://ftp.ncbi.nlm.nih.gov/genomes/all/GCF/000/001/405/GCF_000001405.40_GRCh38.p14/GCF_000001405.40_GRCh38.p14_genomic.gff.gz",
        },
        {
            "name": "GENCODE v49 (GTF)",
            "key": "gencode-gtf",
            "filename": "gencode.v49.chr_patch_hapl_scaff.basic.annotation.gtf.gz",
            "fmt": "gtf",
            "bytes": 70_588_995,
            "sha256": "576dddae36169ad648afbe706535361309786e549ad7daf529cca7674fb0058f",
            "url": "https://ftp.ebi.ac.uk/pub/databases/gencode/Gencode_human/release_49/gencode.v49.chr_patch_hapl_scaff.basic.annotation.gtf.gz",
        },
        {
            "name": "GENCODE v49 (GFF3)",
            "key": "gencode-gff3",
            "filename": "gencode.v49.chr_patch_hapl_scaff.basic.annotation.gff3.gz",
            "fmt": "gff3",
            "bytes": 89_385_177,
            "sha256": "22ffa691aac993603f7f21effacf19848262bec74545bd979863e7af602e5a1d",
            "url": "https://ftp.ebi.ac.uk/pub/databases/gencode/Gencode_human/release_49/gencode.v49.chr_patch_hapl_scaff.basic.annotation.gff3.gz",
        },
    )
    if (REPO_ROOT / ".git").exists():
        tracked = subprocess.run(
            ["git", "ls-files", "--error-unmatch", "benchmarks/corpora.py"],
            cwd=REPO_ROOT,
            capture_output=True,
            text=True,
        )
        assert tracked.returncode == 0, "benchmarks/corpora.py is absent from committed snapshots"
    help_result = subprocess.run(
        [sys.executable, str(MEGA), "--help"], capture_output=True, text=True, timeout=120
    )
    assert help_result.returncode == 0, help_result.stderr


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


def test_generator_imports_from_the_documented_repository_root():
    proc = subprocess.run(
        [sys.executable, "tools/gen_benchmark_tables.py", "--help"],
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
            "checked": [],
            "errors": [],
            "warnings": [],
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


def test_timed_out_subprocess_with_parseable_stdout_still_uses_timeout_diagnostics():
    from benchmarks.common import run_subprocess

    timed_out = run_subprocess(
        'import json, time; print(json.dumps({"wall_seconds": 0.1})); time.sleep(2)',
        label="parseable-before-timeout",
        timeout=1,
    )

    assert timed_out["state"] == "timed_out"
    assert timed_out["stdout_parse_error"] == "invalid final JSON object"


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

    data = _publishable_payload()
    data["corpora"]["gencode-gtf"]["ingest_speedup"] = 999.0

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


def test_runtime_libc_identity_is_closed_portable_and_canonical(monkeypatch):
    from benchmarks import common

    identity = getattr(common, "_runtime_libc_identity", None)
    assert callable(identity)

    monkeypatch.setattr(common.sys, "platform", "darwin")
    assert identity() == {"family": None, "version": None}

    monkeypatch.setattr(common.sys, "platform", "linux")
    monkeypatch.setattr(common.platform, "libc_ver", lambda: ("GNU libc", "02.034.0"))
    assert identity() == {"family": "glibc", "version": "2.34.0"}

    monkeypatch.setattr(common.platform, "libc_ver", lambda: ("unknown-libc", "1.2"))
    assert identity() == {"family": "unknown", "version": None}


def test_actual_environment_without_candidate_wheel_is_prepublication_only(monkeypatch):
    from benchmarks.common import benchmark_env, benchmark_results_evidence_error, environment

    monkeypatch.delenv("GFFBASE_BENCH_WHEEL", raising=False)
    monkeypatch.delenv("GFFBASE_BENCH_WHEEL_SHA256", raising=False)
    payload = _publishable_payload()
    payload["environment"] = environment(benchmark_controls=benchmark_env(4))

    assert payload["environment"]["artifact"] == {"wheel": None, "wheel_sha256": None}
    assert benchmark_results_evidence_error(payload) is not None


def _write_test_candidate_wheel(
    path,
    *,
    metadata_name="gffbase",
    metadata_version="0.2.0rc1",
    wheel_tags=("cp310-abi3-manylinux_2_28_x86_64",),
    native_members=(("gffbase/_native.abi3.so", b"candidate-native"),),
    dist_info="gffbase-0.2.0rc1.dist-info",
    extra_members=(),
):
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr(
            f"{dist_info}/METADATA",
            f"Metadata-Version: 2.4\nName: {metadata_name}\nVersion: {metadata_version}\n",
        )
        archive.writestr(
            f"{dist_info}/WHEEL",
            "Wheel-Version: 1.0\nGenerator: focused-test\nRoot-Is-Purelib: false\n"
            + "".join(f"Tag: {tag}\n" for tag in wheel_tags),
        )
        for member, content in native_members:
            archive.writestr(member, content)
        for member, content in extra_members:
            archive.writestr(member, content)
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _installed_candidate_identity(*, native_sha256):
    return {
        "distribution_version": "0.2.0rc1",
        "python_version": "0.2.0rc1",
        "python_module": "__init__.py",
        "native_version": "0.2.0rc1",
        "native_module": "_native.abi3.so",
        "native_sha256": native_sha256,
    }


def test_candidate_wheel_archive_limits_are_explicit_and_sane():
    from benchmarks import common

    assert 1 << 20 <= common._MAX_CANDIDATE_WHEEL_BYTES <= 1 << 30
    assert 100 <= common._MAX_CANDIDATE_WHEEL_MEMBERS <= 10_000
    assert common._MAX_CANDIDATE_NATIVE_BYTES <= common._MAX_CANDIDATE_WHEEL_UNCOMPRESSED_BYTES
    assert common._MAX_CANDIDATE_WHEEL_BYTES <= common._MAX_CANDIDATE_WHEEL_UNCOMPRESSED_BYTES


@pytest.mark.parametrize(
    "limit_name",
    [
        "_MAX_CANDIDATE_WHEEL_BYTES",
        "_MAX_CANDIDATE_WHEEL_MEMBERS",
        "_MAX_CANDIDATE_WHEEL_UNCOMPRESSED_BYTES",
        "_MAX_CANDIDATE_NATIVE_BYTES",
    ],
)
def test_environment_rejects_candidate_wheels_over_configured_archive_limits(
    tmp_path, monkeypatch, limit_name
):
    from benchmarks import common

    wheel = tmp_path / "gffbase-0.2.0rc1-cp310-abi3-manylinux_2_28_x86_64.whl"
    wheel_sha256 = _write_test_candidate_wheel(wheel)
    native_sha256 = hashlib.sha256(b"candidate-native").hexdigest()
    monkeypatch.setenv("GFFBASE_BENCH_WHEEL", str(wheel))
    monkeypatch.setenv("GFFBASE_BENCH_WHEEL_SHA256", wheel_sha256)
    monkeypatch.setattr(
        common,
        "_installed_gffbase",
        lambda: _installed_candidate_identity(native_sha256=native_sha256),
    )
    limits = {
        "_MAX_CANDIDATE_WHEEL_BYTES": wheel.stat().st_size - 1,
        "_MAX_CANDIDATE_WHEEL_MEMBERS": 2,
        "_MAX_CANDIDATE_WHEEL_UNCOMPRESSED_BYTES": 10,
        "_MAX_CANDIDATE_NATIVE_BYTES": len(b"candidate-native") - 1,
    }
    monkeypatch.setattr(common, limit_name, limits[limit_name])

    with pytest.raises(ValueError, match="limit"):
        common.environment(benchmark_controls=common.benchmark_env(4))


def test_environment_streams_the_native_member_when_hashing(tmp_path, monkeypatch):
    from benchmarks import common

    wheel = tmp_path / "gffbase-0.2.0rc1-cp310-abi3-manylinux_2_28_x86_64.whl"
    wheel_sha256 = _write_test_candidate_wheel(wheel)
    native_sha256 = hashlib.sha256(b"candidate-native").hexdigest()
    monkeypatch.setenv("GFFBASE_BENCH_WHEEL", str(wheel))
    monkeypatch.setenv("GFFBASE_BENCH_WHEEL_SHA256", wheel_sha256)
    monkeypatch.setattr(
        common,
        "_installed_gffbase",
        lambda: _installed_candidate_identity(native_sha256=native_sha256),
    )
    archive_read = zipfile.ZipFile.read

    def reject_buffered_native(self, name, *args, **kwargs):
        member_name = name.filename if isinstance(name, zipfile.ZipInfo) else name
        if str(member_name).endswith((".so", ".pyd")):
            raise AssertionError("native member was buffered")
        return archive_read(self, name, *args, **kwargs)

    monkeypatch.setattr(zipfile.ZipFile, "read", reject_buffered_native)

    artifact = common.environment(benchmark_controls=common.benchmark_env(4))["artifact"]

    assert artifact["native"]["sha256"] == native_sha256


@pytest.mark.parametrize(
    "member",
    [
        "/absolute.txt",
        "../outside.txt",
        "gffbase/../outside.txt",
        "gffbase/./ambiguous.txt",
        "gffbase//ambiguous.txt",
        "gffbase\\windows.txt",
        "C:/drive.txt",
    ],
)
def test_environment_rejects_nonportable_wheel_member_paths(tmp_path, monkeypatch, member):
    from benchmarks import common

    wheel = tmp_path / "gffbase-0.2.0rc1-cp310-abi3-manylinux_2_28_x86_64.whl"
    wheel_sha256 = _write_test_candidate_wheel(wheel, extra_members=((member, b"x"),))
    monkeypatch.setenv("GFFBASE_BENCH_WHEEL", str(wheel))
    monkeypatch.setenv("GFFBASE_BENCH_WHEEL_SHA256", wheel_sha256)

    with pytest.raises(ValueError, match="member path"):
        common.environment(benchmark_controls=common.benchmark_env(4))


@pytest.mark.parametrize(
    "extra_members",
    [
        (("GFFBASE/_NATIVE.ABI3.SO", b"collision"),),
        (("docs/é.txt", b"one"), ("docs/e\u0301.txt", b"two")),
    ],
)
def test_environment_rejects_casefold_and_unicode_member_collisions(
    tmp_path, monkeypatch, extra_members
):
    from benchmarks import common

    wheel = tmp_path / "gffbase-0.2.0rc1-cp310-abi3-manylinux_2_28_x86_64.whl"
    wheel_sha256 = _write_test_candidate_wheel(wheel, extra_members=extra_members)
    monkeypatch.setenv("GFFBASE_BENCH_WHEEL", str(wheel))
    monkeypatch.setenv("GFFBASE_BENCH_WHEEL_SHA256", wheel_sha256)

    with pytest.raises(ValueError, match="ambiguous"):
        common.environment(benchmark_controls=common.benchmark_env(4))


def test_environment_rejects_nul_in_raw_wheel_member_name(tmp_path, monkeypatch):
    from benchmarks import common

    wheel = tmp_path / "gffbase-0.2.0rc1-cp310-abi3-manylinux_2_28_x86_64.whl"
    _write_test_candidate_wheel(wheel, extra_members=(("docs/nulx.txt", b"x"),))
    raw = wheel.read_bytes().replace(b"docs/nulx.txt", b"docs/nul\x00.txt")
    assert raw.count(b"docs/nul\x00.txt") == 2
    wheel.write_bytes(raw)
    monkeypatch.setenv("GFFBASE_BENCH_WHEEL", str(wheel))
    monkeypatch.setenv("GFFBASE_BENCH_WHEEL_SHA256", hashlib.sha256(raw).hexdigest())

    with pytest.raises(ValueError, match="NUL"):
        common.environment(benchmark_controls=common.benchmark_env(4))


def test_environment_requires_the_exact_candidate_dist_info_directory(tmp_path, monkeypatch):
    from benchmarks import common

    wheel = tmp_path / "gffbase-0.2.0rc1-cp310-abi3-manylinux_2_28_x86_64.whl"
    wheel_sha256 = _write_test_candidate_wheel(wheel, dist_info="gffbase-0.2.0rc1.post1.dist-info")
    monkeypatch.setenv("GFFBASE_BENCH_WHEEL", str(wheel))
    monkeypatch.setenv("GFFBASE_BENCH_WHEEL_SHA256", wheel_sha256)

    with pytest.raises(ValueError, match="dist-info"):
        common.environment(benchmark_controls=common.benchmark_env(4))


@pytest.mark.parametrize("file_type", [stat.S_IFLNK, stat.S_IFDIR, stat.S_IFIFO])
def test_environment_requires_regular_wheel_evidence_members(tmp_path, monkeypatch, file_type):
    from benchmarks import common

    wheel = tmp_path / "gffbase-0.2.0rc1-cp310-abi3-manylinux_2_28_x86_64.whl"
    native = zipfile.ZipInfo("gffbase/_native.abi3.so")
    native.create_system = 3
    native.external_attr = (file_type | 0o755) << 16
    wheel_sha256 = _write_test_candidate_wheel(
        wheel, native_members=((native, b"candidate-native"),)
    )
    monkeypatch.setenv("GFFBASE_BENCH_WHEEL", str(wheel))
    monkeypatch.setenv("GFFBASE_BENCH_WHEEL_SHA256", wheel_sha256)

    with pytest.raises(ValueError, match="regular file"):
        common.environment(benchmark_controls=common.benchmark_env(4))


def test_environment_rejects_entry_count_before_zipfile_materialization(tmp_path, monkeypatch):
    from benchmarks import common

    wheel = tmp_path / "gffbase-0.2.0rc1-cp310-abi3-manylinux_2_28_x86_64.whl"
    _write_test_candidate_wheel(wheel)
    raw = bytearray(wheel.read_bytes())
    eocd = raw.rfind(b"PK\x05\x06")
    assert eocd >= 0
    struct.pack_into(
        "<HH",
        raw,
        eocd + 8,
        common._MAX_CANDIDATE_WHEEL_MEMBERS + 1,
        common._MAX_CANDIDATE_WHEEL_MEMBERS + 1,
    )
    wheel.write_bytes(raw)
    monkeypatch.setenv("GFFBASE_BENCH_WHEEL", str(wheel))
    monkeypatch.setenv("GFFBASE_BENCH_WHEEL_SHA256", hashlib.sha256(raw).hexdigest())
    monkeypatch.setattr(
        zipfile.ZipFile,
        "__init__",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("ZipFile materialized")),
    )

    with pytest.raises(ValueError, match="member-count limit"):
        common.environment(benchmark_controls=common.benchmark_env(4))


def test_zip_entry_count_preflight_reads_ordinary_and_zip64_metadata(tmp_path):
    from benchmarks import common

    ordinary = tmp_path / "ordinary.whl"
    _write_test_candidate_wheel(ordinary)
    assert common._zip_entry_count_before_open(ordinary) == 3

    zip64 = tmp_path / "zip64.whl"
    count = common._MAX_CANDIDATE_WHEEL_MEMBERS + 1
    zip64_eocd = struct.pack("<4sQ2H2L4Q", b"PK\x06\x06", 44, 45, 45, 0, 0, count, count, 0, 0)
    locator = struct.pack("<4sLQL", b"PK\x06\x07", 0, 0, 1)
    eocd = struct.pack("<4s4H2LH", b"PK\x05\x06", 0, 0, 0xFFFF, 0xFFFF, 0xFFFFFFFF, 0xFFFFFFFF, 0)
    zip64.write_bytes(zip64_eocd + locator + eocd)
    assert common._zip_entry_count_before_open(zip64) == count


def test_zip_entry_count_preflight_rejects_malformed_metadata(tmp_path):
    from benchmarks import common

    malformed = tmp_path / "malformed.whl"
    malformed.write_bytes(b"not-a-zip")

    with pytest.raises(ValueError, match="central directory"):
        common._zip_entry_count_before_open(malformed)


@pytest.mark.parametrize(
    ("entries_on_disk", "entry_count", "central_size", "central_offset"),
    [
        (1, 0xFFFF, 0xFFFFFFFF, 0xFFFFFFFF),
        (0xFFFF, 1, 0xFFFFFFFF, 0xFFFFFFFF),
        (0xFFFF, 0xFFFF, 1, 0xFFFFFFFF),
        (0xFFFF, 0xFFFF, 0xFFFFFFFF, 1),
    ],
)
def test_zip64_metadata_rejects_disagreeing_non_sentinel_eocd_fields(
    tmp_path, entries_on_disk, entry_count, central_size, central_offset
):
    from benchmarks import common

    wheel = tmp_path / "inconsistent-zip64.whl"
    zip64_count = common._MAX_CANDIDATE_WHEEL_MEMBERS + 1
    zip64_eocd = struct.pack(
        "<4sQ2H2I4Q", b"PK\x06\x06", 44, 45, 45, 0, 0, zip64_count, zip64_count, 0, 0
    )
    locator = struct.pack("<4sIQI", b"PK\x06\x07", 0, 0, 1)
    eocd = struct.pack(
        "<4s4H2IH",
        b"PK\x05\x06",
        0,
        0,
        entries_on_disk,
        entry_count,
        central_size,
        central_offset,
        0,
    )
    wheel.write_bytes(zip64_eocd + locator + eocd)

    with pytest.raises(ValueError, match="ZIP64 central directory"):
        common._zip_entry_count_before_open(wheel)


def test_environment_validates_every_local_header_and_member_crc(tmp_path, monkeypatch):
    from benchmarks import common

    wheel = tmp_path / "gffbase-0.2.0rc1-cp310-abi3-manylinux_2_28_x86_64.whl"
    _write_test_candidate_wheel(wheel, extra_members=(("docs/good.txt", b"crc-payload"),))
    raw = wheel.read_bytes()
    assert raw.count(b"docs/good.txt") == 2
    corrupted_name = raw.replace(b"docs/good.txt", b"../x/evil.txt", 1)
    wheel.write_bytes(corrupted_name)
    monkeypatch.setenv("GFFBASE_BENCH_WHEEL", str(wheel))
    monkeypatch.setenv("GFFBASE_BENCH_WHEEL_SHA256", hashlib.sha256(corrupted_name).hexdigest())

    with pytest.raises(ValueError, match="valid ZIP"):
        common.environment(benchmark_controls=common.benchmark_env(4))

    _write_test_candidate_wheel(wheel, extra_members=(("docs/good.txt", b"crc-payload"),))
    corrupted_crc = wheel.read_bytes().replace(b"crc-payload", b"crc-payloae", 1)
    wheel.write_bytes(corrupted_crc)
    monkeypatch.setenv("GFFBASE_BENCH_WHEEL_SHA256", hashlib.sha256(corrupted_crc).hexdigest())

    with pytest.raises(ValueError, match="valid ZIP"):
        common.environment(benchmark_controls=common.benchmark_env(4))


@pytest.mark.parametrize(
    "extra_members",
    [
        (("docs", b"file"), ("docs/", b"")),
        (("docs", b"file"), ("docs/a.txt", b"child")),
    ],
)
def test_environment_rejects_file_directory_namespace_conflicts(
    tmp_path, monkeypatch, extra_members
):
    from benchmarks import common

    wheel = tmp_path / "gffbase-0.2.0rc1-cp310-abi3-manylinux_2_28_x86_64.whl"
    wheel_sha256 = _write_test_candidate_wheel(wheel, extra_members=extra_members)
    monkeypatch.setenv("GFFBASE_BENCH_WHEEL", str(wheel))
    monkeypatch.setenv("GFFBASE_BENCH_WHEEL_SHA256", wheel_sha256)

    with pytest.raises(ValueError, match="namespace|ambiguous"):
        common.environment(benchmark_controls=common.benchmark_env(4))


def test_environment_rejects_foreign_dist_info_trees(tmp_path, monkeypatch):
    from benchmarks import common

    wheel = tmp_path / "gffbase-0.2.0rc1-cp310-abi3-manylinux_2_28_x86_64.whl"
    wheel_sha256 = _write_test_candidate_wheel(
        wheel, extra_members=(("evil-1.dist-info/RECORD", b"foreign"),)
    )
    monkeypatch.setenv("GFFBASE_BENCH_WHEEL", str(wheel))
    monkeypatch.setenv("GFFBASE_BENCH_WHEEL_SHA256", wheel_sha256)

    with pytest.raises(ValueError, match="dist-info"):
        common.environment(benchmark_controls=common.benchmark_env(4))


def test_environment_normalizes_unsupported_member_compression_errors(tmp_path, monkeypatch):
    from benchmarks import common

    wheel = tmp_path / "gffbase-0.2.0rc1-cp310-abi3-manylinux_2_28_x86_64.whl"
    _write_test_candidate_wheel(wheel)
    raw = bytearray(wheel.read_bytes())
    metadata_name = b"gffbase-0.2.0rc1.dist-info/METADATA"
    local_name = raw.find(metadata_name)
    central_name = raw.rfind(metadata_name)
    assert local_name >= 30 and central_name >= 46 and local_name != central_name
    struct.pack_into("<H", raw, local_name - 30 + 8, 99)
    struct.pack_into("<H", raw, central_name - 46 + 10, 99)
    wheel.write_bytes(raw)
    monkeypatch.setenv("GFFBASE_BENCH_WHEEL", str(wheel))
    monkeypatch.setenv("GFFBASE_BENCH_WHEEL_SHA256", hashlib.sha256(raw).hexdigest())

    with pytest.raises(ValueError, match="valid ZIP"):
        common.environment(benchmark_controls=common.benchmark_env(4))


@pytest.mark.parametrize("compression", [zipfile.ZIP_DEFLATED, zipfile.ZIP_BZIP2])
def test_environment_normalizes_corrupt_supported_compression_errors(
    tmp_path, monkeypatch, compression
):
    from benchmarks import common

    wheel = tmp_path / "gffbase-0.2.0rc1-cp310-abi3-manylinux_2_28_x86_64.whl"
    dist_info = "gffbase-0.2.0rc1.dist-info"
    corrupt_name = "docs/corrupt.bin"
    with zipfile.ZipFile(wheel, "w", compression=compression) as archive:
        archive.writestr(
            f"{dist_info}/METADATA",
            "Metadata-Version: 2.4\nName: gffbase\nVersion: 0.2.0rc1\n",
        )
        archive.writestr(
            f"{dist_info}/WHEEL",
            "Wheel-Version: 1.0\nGenerator: focused-test\nRoot-Is-Purelib: false\n"
            "Tag: cp310-abi3-manylinux_2_28_x86_64\n",
        )
        archive.writestr("gffbase/_native.abi3.so", b"candidate-native")
        archive.writestr(corrupt_name, bytes(range(256)) * 64)
    with zipfile.ZipFile(wheel) as archive:
        corrupt = archive.getinfo(corrupt_name)
    raw = bytearray(wheel.read_bytes())
    name_size, extra_size = struct.unpack_from("<HH", raw, corrupt.header_offset + 26)
    data_offset = corrupt.header_offset + 30 + name_size + extra_size
    raw[data_offset : data_offset + corrupt.compress_size] = b"\xff" * corrupt.compress_size
    wheel.write_bytes(raw)
    monkeypatch.setenv("GFFBASE_BENCH_WHEEL", str(wheel))
    monkeypatch.setenv("GFFBASE_BENCH_WHEEL_SHA256", hashlib.sha256(raw).hexdigest())

    with pytest.raises(ValueError, match="valid ZIP"):
        common.environment(benchmark_controls=common.benchmark_env(4))


def test_environment_binds_installed_native_to_authoritative_wheel_evidence(tmp_path, monkeypatch):
    from benchmarks import common

    wheel = tmp_path / "gffbase-0.2.0rc1-cp310-abi3-manylinux_2_28_x86_64.whl"
    wheel_sha256 = _write_test_candidate_wheel(wheel)
    native_sha256 = hashlib.sha256(b"candidate-native").hexdigest()
    monkeypatch.setenv("GFFBASE_BENCH_WHEEL", str(wheel))
    monkeypatch.setenv("GFFBASE_BENCH_WHEEL_SHA256", wheel_sha256)
    monkeypatch.setattr(
        common,
        "_installed_gffbase",
        lambda: _installed_candidate_identity(native_sha256=native_sha256),
    )

    artifact = common.environment(benchmark_controls=common.benchmark_env(4))["artifact"]

    assert artifact == {
        "wheel": wheel.name,
        "wheel_sha256": wheel_sha256,
        "metadata": {"name": "gffbase", "version": "0.2.0rc1"},
        "wheel_tags": ["cp310-abi3-manylinux_2_28_x86_64"],
        "native": {
            "member": "gffbase/_native.abi3.so",
            "sha256": native_sha256,
        },
    }
    assert str(tmp_path) not in json.dumps(artifact)


@pytest.mark.parametrize("configured_hash", ["0" * 64, None])
def test_environment_rejects_missing_or_mismatched_configured_wheel_hash(
    tmp_path, monkeypatch, configured_hash
):
    from benchmarks import common

    wheel = tmp_path / "gffbase-0.2.0rc1-cp310-abi3-manylinux_2_28_x86_64.whl"
    _write_test_candidate_wheel(wheel)
    monkeypatch.setenv("GFFBASE_BENCH_WHEEL", str(wheel))
    if configured_hash is None:
        monkeypatch.delenv("GFFBASE_BENCH_WHEEL_SHA256", raising=False)
    else:
        monkeypatch.setenv("GFFBASE_BENCH_WHEEL_SHA256", configured_hash)

    with pytest.raises(ValueError, match="wheel SHA-256"):
        common.environment(benchmark_controls=common.benchmark_env(4))


@pytest.mark.parametrize(
    ("wheel_options", "installed_native_sha256", "message"),
    [
        ({"metadata_name": "forged"}, hashlib.sha256(b"candidate-native").hexdigest(), "metadata"),
        (
            {"metadata_version": "9.9.9"},
            hashlib.sha256(b"candidate-native").hexdigest(),
            "metadata",
        ),
        (
            {"wheel_tags": ("cp311-abi3-manylinux_2_28_x86_64",)},
            hashlib.sha256(b"candidate-native").hexdigest(),
            "WHEEL Tag",
        ),
        ({"native_members": ()}, hashlib.sha256(b"candidate-native").hexdigest(), "native member"),
        (
            {
                "native_members": (
                    ("gffbase/_native.abi3.so", b"candidate-native"),
                    ("gffbase/_native.extra.so", b"other"),
                )
            },
            hashlib.sha256(b"candidate-native").hexdigest(),
            "native member",
        ),
        ({}, hashlib.sha256(b"different-installed-native").hexdigest(), "installed native"),
    ],
)
def test_environment_rejects_forged_or_unbound_wheel_evidence(
    tmp_path, monkeypatch, wheel_options, installed_native_sha256, message
):
    from benchmarks import common

    wheel = tmp_path / "gffbase-0.2.0rc1-cp310-abi3-manylinux_2_28_x86_64.whl"
    wheel_sha256 = _write_test_candidate_wheel(wheel, **wheel_options)
    monkeypatch.setenv("GFFBASE_BENCH_WHEEL", str(wheel))
    monkeypatch.setenv("GFFBASE_BENCH_WHEEL_SHA256", wheel_sha256)
    monkeypatch.setattr(
        common,
        "_installed_gffbase",
        lambda: _installed_candidate_identity(native_sha256=installed_native_sha256),
    )

    with pytest.raises(ValueError, match=message):
        common.environment(benchmark_controls=common.benchmark_env(4))


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

    valid = _publishable_payload()
    assert benchmark_results_evidence_error(valid) is None

    failed = json.loads(json.dumps(valid))
    failed["corpora"]["mane"]["gffbase"]["state"] = "failed"
    assert "candidate" in benchmark_results_evidence_error(failed)

    forged = json.loads(json.dumps(valid))
    forged["corpora"]["mane"]["ingest_speedup"] = 999.0
    assert "speedup" in benchmark_results_evidence_error(forged)

    private = json.loads(json.dumps(valid))
    private["corpora"]["mane"]["gffbase"]["stdout_path"] = "/home/private-user/log"
    assert "absolute path" in benchmark_results_evidence_error(private)
    assert "benchmark_results_evidence_error(merged)" in _source()


def _publishable_payload(*, threads: int = 4) -> dict:
    """A real-shaped schema-v3 payload, assembled without production helpers."""

    controls = {
        "GFFBASE_THREADS": str(threads),
        "GFFUTILS2_THREADS": str(threads),
        "OMP_NUM_THREADS": "1",
        "OPENBLAS_NUM_THREADS": "1",
        "MKL_NUM_THREADS": "1",
        "NUMEXPR_NUM_THREADS": "1",
        "VECLIB_MAXIMUM_THREADS": "1",
        "BLIS_NUM_THREADS": "1",
        "DUCKDB_DISABLE_PROGRESS_BAR": "1",
        "PYTHONUNBUFFERED": "1",
    }
    corpora_contract = {
        "mane": (
            "MANE v1.5 (Ensembl IDs)",
            "MANE.GRCh38.v1.5.ensembl_genomic.gff.gz",
            "gff3",
            10_349_746,
            "69089bbc84d1d3c3ce31c2ed3f85b6c3169fb8836d092a082623c59a43fd22ef",
        ),
        "chess": (
            "CHESS 3.1.3",
            "chess3.1.3.GRCh38.gff.gz",
            "gff3",
            20_435_645,
            "28da847be976780fe38162a7c244749fdc7a0b446741ca8da2b64019c1606e03",
        ),
        "refseq": (
            "RefSeq GRCh38.p14",
            "GCF_000001405.40_GRCh38.p14_genomic.gff.gz",
            "gff3",
            78_190_483,
            "4920f0eae7e2197c50b67a201e06d657387137b49dd60f474b4f1d5b29334051",
        ),
        "gencode-gtf": (
            "GENCODE v49 (GTF)",
            "gencode.v49.chr_patch_hapl_scaff.basic.annotation.gtf.gz",
            "gtf",
            70_588_995,
            "576dddae36169ad648afbe706535361309786e549ad7daf529cca7674fb0058f",
        ),
        "gencode-gff3": (
            "GENCODE v49 (GFF3)",
            "gencode.v49.chr_patch_hapl_scaff.basic.annotation.gff3.gz",
            "gff3",
            89_385_177,
            "22ffa691aac993603f7f21effacf19848262bec74545bd979863e7af602e5a1d",
        ),
    }
    checked_ids = [
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
    ]

    candidate = {
        **_candidate(),
        "label": "gffbase ingest(input.gff3.gz)",
        "peak_rss_bytes": 1_048_576,
        "peak_rss_mb": 1.0,
        "benchmark_env": controls,
        "cap_seconds": 10,
        "disk_bytes": 1,
        "fmt": "gff3",
    }
    candidate["validation"] = {
        **candidate["validation"],
        "checked": [f"{stable_id} checked" for stable_id in checked_ids],
        "checked_ids": checked_ids,
    }
    legacy = {
        **_comparator(),
        "label": "legacy gffutils ingest(input.gff3.gz)",
        "peak_rss_bytes": 2_097_152,
        "peak_rss_mb": 2.0,
        "benchmark_env": controls,
        "cap_seconds": 10,
        "disk_bytes": 1,
    }
    row = {
        "name": "MANE v1.5 (Ensembl IDs)",
        "key": "mane",
        "measured": {
            "timestamp_utc": "2026-08-27T00:00:00+00:00",
            "git_commit": "a" * 40,
            "git_dirty": False,
        },
        "input": "benchmarks/data/MANE.GRCh38.v1.5.ensembl_genomic.gff.gz",
        "input_bytes": 10_349_746,
        "input_sha256": "69089bbc84d1d3c3ce31c2ed3f85b6c3169fb8836d092a082623c59a43fd22ef",
        "feature_lines": 1000,
        "gffbase": candidate,
        "legacy": legacy,
        "ingest_speedup": 2.0,
        "spatial": {"state": "skipped", "reason": "no qualifying seqids"},
        "batched": {"state": "skipped", "reason": "no top-level feature IDs found"},
        "db_paths": {
            "gffbase": "benchmarks/out/mane.duckdb",
            "legacy": "benchmarks/out/mane.sqlite",
        },
        "params": {
            "legacy_cap_seconds": 10,
            "gffbase_cap_seconds": 10,
            "n_spatial": 5,
            "n_batched": 5,
            "repeats": 1,
            "region_seed": 20260501,
            "threads": threads,
            "gtf_arm": None,
            "infer_gtf_parents": None,
            "validation_sample": "all",
            "benchmark_env": controls,
        },
    }
    env = {
        "timestamp_utc": "2026-08-27T01:02:03Z",
        "git_commit": "a" * 40,
        "git_dirty": False,
        "hostname": "benchmark-host",
        "platform": "Linux-6.12-x86_64",
        "machine": "x86_64",
        "libc": {"family": "glibc", "version": "2.34"},
        "cpu_model": "Example CPU",
        "cpu_cores_physical": 4,
        "cpu_cores_logical": 8,
        "total_ram_bytes": 17_179_869_184,
        "free_disk_bytes": 8_589_934_592,
        "cpu_affinity": [0, 1, 2, 3],
        "python": {
            "version": "3.13.5",
            "implementation": "CPython",
            "executable": "python",
        },
        "rustc_version": "rustc 1.90.0 (example 2026-01-01)",
        "packages": {
            "gffbase": "0.2.0rc1",
            "duckdb": "1.5.3",
            "pyarrow": "21.0.0",
            "pandas": "2.3.2",
            "polars": "1.32.3",
            "gffutils": "0.14",
            "psutil": "7.0.0",
        },
        "gffbase_install": {
            "distribution_version": "0.2.0rc1",
            "python_version": "0.2.0rc1",
            "python_module": "__init__.py",
            "native_version": "0.2.0rc1",
            "native_module": "_native.abi3.so",
            "native_sha256": "b" * 64,
        },
        "artifact": {
            "wheel": "gffbase-0.2.0rc1-cp310-abi3-manylinux_2_28_x86_64.whl",
            "wheel_sha256": "c" * 64,
            "metadata": {"name": "gffbase", "version": "0.2.0rc1"},
            "wheel_tags": ["cp310-abi3-manylinux_2_28_x86_64"],
            "native": {
                "member": "gffbase/_native.abi3.so",
                "sha256": "b" * 64,
            },
        },
        "env": controls,
    }
    corpora = {}
    for key, (name, filename, fmt, input_bytes, input_sha256) in corpora_contract.items():
        item = json.loads(json.dumps(row))
        item.update(
            {
                "key": key,
                "name": name,
                "input": f"benchmarks/data/{filename}",
                "input_bytes": input_bytes,
                "input_sha256": input_sha256,
            }
        )
        item["gffbase"]["label"] = f"gffbase ingest({filename})"
        item["gffbase"]["fmt"] = fmt
        item["legacy"]["label"] = f"legacy gffutils ingest({filename})"
        item["db_paths"] = {
            "gffbase": f"benchmarks/out/{key}.duckdb",
            "legacy": f"benchmarks/out/{key}_legacy.sqlite",
        }
        if key == "gencode-gtf":
            item["params"]["gtf_arm"] = "no-infer"
            item["params"]["infer_gtf_parents"] = False
        corpora[key] = item
    return {"schema_version": "3", "environment": env, "corpora": corpora}


def test_shared_gate_rejects_query_shape_leaks_and_thread_mismatch():
    from benchmarks.common import benchmark_results_evidence_error

    payload = _publishable_payload()
    assert benchmark_results_evidence_error(payload) is None

    leaked = json.loads(json.dumps(payload))
    leaked["corpora"]["mane"]["spatial"]["qps"] = 99
    assert "spatial" in benchmark_results_evidence_error(leaked)

    mismatch = json.loads(json.dumps(payload))
    mismatch["environment"]["env"]["GFFBASE_THREADS"] = "1"
    assert "environment" in benchmark_results_evidence_error(mismatch)


@pytest.mark.parametrize(
    ("path", "value"),
    [
        (("environment", "artifact", "wheel"), "/home/private/candidate.whl"),
        (("environment", "git_dirty"), True),
        (("environment", "git_commit"), "not-a-commit"),
        (("environment", "timestamp_utc"), ""),
        (("environment", "cpu_cores_physical"), True),
        (("environment", "total_ram_bytes"), float("inf")),
        (("environment", "cpu_affinity"), [True]),
        (("environment", "python", "executable"), ""),
        (("environment", "packages", "gffbase"), None),
        (("environment", "gffbase_install"), {}),
        (("environment", "artifact", "wheel_sha256"), "forged"),
        (("corpora", "mane", "measured", "git_dirty"), True),
        (("corpora", "mane", "measured", "git_commit"), "d" * 40),
        (
            ("corpora", "mane", "gffbase", "label"),
            "gffbase ingest(/home/private/MANE.GRCh38.v1.5.ensembl_genomic.gff.gz)",
        ),
    ],
)
def test_full_gate_rejects_forged_environment_and_measured_identity(path, value):
    from benchmarks.common import benchmark_results_evidence_error

    payload = _publishable_payload()
    target = payload
    for key in path[:-1]:
        target = target[key]
    target[path[-1]] = value

    assert benchmark_results_evidence_error(payload) is not None


@pytest.mark.parametrize(
    ("path", "value"),
    [
        (("artifact", "metadata", "name"), "forged"),
        (("artifact", "metadata", "version"), "9.9.9"),
        (("artifact", "wheel_tags"), []),
        (
            ("artifact", "wheel_tags"),
            [
                "cp310-abi3-manylinux_2_28_x86_64",
                "cp310-abi3-manylinux_2_28_aarch64",
            ],
        ),
        (("artifact", "wheel_tags", 0), "cp311-abi3-manylinux_2_28_x86_64"),
        (("artifact", "native", "member"), "gffbase/_native.other.so"),
        (("artifact", "native", "sha256"), "d" * 64),
        (("gffbase_install", "native_sha256"), "d" * 64),
    ],
)
def test_full_gate_rejects_forged_wheel_archive_crosslinks(path, value):
    from benchmarks.common import benchmark_results_evidence_error

    payload = _publishable_payload()
    target = payload["environment"]
    for key in path[:-1]:
        target = target[key]
    target[path[-1]] = value

    assert benchmark_results_evidence_error(payload) is not None


@pytest.mark.parametrize(
    "reason",
    [
        "note,/home/private/secret",
        "note;/home/private/secret",
        "note</home/private/secret>",
    ],
)
def test_full_gate_rejects_punctuation_delimited_private_paths(reason):
    from benchmarks.common import benchmark_results_evidence_error

    payload = _publishable_payload()
    payload["corpora"]["mane"]["spatial"]["reason"] = reason

    assert benchmark_results_evidence_error(payload) is not None


def test_full_gate_requires_fixed_candidate_and_comparator_versions():
    from benchmarks.common import benchmark_results_evidence_error

    candidate = _publishable_payload()
    env = candidate["environment"]
    env["packages"]["gffbase"] = "9.9.9"
    for key in ("distribution_version", "python_version", "native_version"):
        env["gffbase_install"][key] = "9.9.9"
    env["artifact"]["wheel"] = "gffbase-9.9.9-cp313-cp313-manylinux_2_28_x86_64.whl"
    assert benchmark_results_evidence_error(candidate) is not None

    comparator = _publishable_payload()
    comparator["environment"]["packages"]["gffutils"] = "0.13"
    assert benchmark_results_evidence_error(comparator) is not None


@pytest.mark.parametrize(
    "wheel",
    [
        "gffbase-0.2.0rc1-cp312-cp312-manylinux_2_28_x86_64.whl",
        "gffbase-0.2.0rc1-cp313-cp312-manylinux_2_28_x86_64.whl",
        "gffbase-0.2.0rc1-cp313-cp39-manylinux_2_28_x86_64.whl",
        "gffbase-0.2.0rc1-cp313-none-manylinux_2_28_x86_64.whl",
    ],
)
def test_full_gate_cross_checks_wheel_tag_and_abi_with_python_runtime(wheel):
    from benchmarks.common import benchmark_results_evidence_error

    payload = _publishable_payload()
    payload["environment"]["artifact"]["wheel"] = wheel

    assert benchmark_results_evidence_error(payload) is not None


@pytest.mark.parametrize(
    ("wheel", "native_module"),
    [
        (
            "gffbase-0.2.0rc1-cp313-cp313-win_amd64.whl",
            "_native.cp313-win_amd64.pyd",
        ),
        (
            "gffbase-0.2.0rc1-cp313-cp313-manylinux_2_28_aarch64.whl",
            "_native.cpython-313-aarch64-linux-gnu.so",
        ),
        (
            "gffbase-0.2.0rc1-cp313-cp313-manylinux_2_28_x86_64.whl",
            "_native.cpython-312-x86_64-linux-gnu.so",
        ),
    ],
)
def test_full_gate_cross_checks_artifact_and_native_platform_identity(wheel, native_module):
    from benchmarks.common import benchmark_results_evidence_error

    payload = _publishable_payload()
    payload["environment"]["artifact"]["wheel"] = wheel
    payload["environment"]["gffbase_install"]["native_module"] = native_module

    assert benchmark_results_evidence_error(payload) is not None


@pytest.mark.parametrize(
    ("machine", "wheel_arch", "native_arch"),
    [
        ("AMD64", "x86_64", "x86_64"),
        ("arm64", "aarch64", "aarch64"),
    ],
)
def test_full_gate_accepts_normalized_linux_artifact_architectures(
    machine, wheel_arch, native_arch
):
    from benchmarks.common import benchmark_results_evidence_error

    payload = _publishable_payload()
    payload["environment"]["machine"] = machine
    wheel = f"gffbase-0.2.0rc1-cp313-cp313-manylinux_2_28_{wheel_arch}.whl"
    wheel_tag = f"cp313-cp313-manylinux_2_28_{wheel_arch}"
    native_module = f"_native.cpython-313-{native_arch}-linux-gnu.so"
    payload["environment"]["artifact"]["wheel"] = wheel
    payload["environment"]["artifact"]["wheel_tags"] = [wheel_tag]
    payload["environment"]["artifact"]["native"]["member"] = f"gffbase/{native_module}"
    payload["environment"]["gffbase_install"]["native_module"] = native_module

    assert benchmark_results_evidence_error(payload) is None


@pytest.mark.parametrize(
    ("machine", "wheel_arch", "native_arch"),
    [
        ("AMD64", "amd64", "amd64"),
        ("aarch64", "arm64", "arm64"),
    ],
)
def test_full_gate_accepts_normalized_windows_artifact_architectures(
    machine, wheel_arch, native_arch
):
    from benchmarks.common import benchmark_results_evidence_error

    payload = _publishable_payload()
    payload["environment"]["platform"] = "Windows-11-10.0.26100-SP0"
    payload["environment"]["machine"] = machine
    payload["environment"]["libc"] = {"family": None, "version": None}
    wheel = f"gffbase-0.2.0rc1-cp313-cp313-win_{wheel_arch}.whl"
    wheel_tag = f"cp313-cp313-win_{wheel_arch}"
    native_module = f"_native.cp313-win_{native_arch}.pyd"
    payload["environment"]["artifact"]["wheel"] = wheel
    payload["environment"]["artifact"]["wheel_tags"] = [wheel_tag]
    payload["environment"]["artifact"]["native"]["member"] = f"gffbase/{native_module}"
    payload["environment"]["gffbase_install"]["native_module"] = native_module

    assert benchmark_results_evidence_error(payload) is None


def test_full_gate_accepts_valid_wheel_build_tag():
    from benchmarks.common import benchmark_results_evidence_error

    payload = _publishable_payload()
    payload["environment"]["artifact"]["wheel"] = (
        "gffbase-0.2.0rc1-1+cuda-cp310-abi3-manylinux_2_28_x86_64.whl"
    )

    assert benchmark_results_evidence_error(payload) is None


@pytest.mark.parametrize("build_tag", ["strict", "1-bad"])
def test_full_gate_rejects_invalid_wheel_build_tag(build_tag):
    from benchmarks.common import benchmark_results_evidence_error

    payload = _publishable_payload()
    payload["environment"]["artifact"]["wheel"] = (
        f"gffbase-0.2.0rc1-{build_tag}-cp310-abi3-manylinux_2_28_x86_64.whl"
    )

    assert benchmark_results_evidence_error(payload) is not None


@pytest.mark.parametrize(
    ("platform_name", "machine", "wheel_platform", "native_module"),
    [
        (
            "Linux-6.12-x86_64",
            "x86_64",
            "manylinux_2_28_x86_64",
            "_native.abi3.so",
        ),
        ("Windows-11-10.0.26100-SP0", "AMD64", "win_amd64", "_native.pyd"),
        ("macOS-15.6-arm64-arm-64bit-Mach-O", "arm64", "macosx_11_0_arm64", "_native.abi3.so"),
    ],
)
def test_full_gate_accepts_configured_abi3_artifact(
    platform_name, machine, wheel_platform, native_module
):
    from benchmarks.common import benchmark_results_evidence_error

    payload = _publishable_payload()
    payload["environment"]["platform"] = platform_name
    payload["environment"]["machine"] = machine
    if not platform_name.startswith("Linux-"):
        payload["environment"]["libc"] = {"family": None, "version": None}
    wheel = f"gffbase-0.2.0rc1-cp310-abi3-{wheel_platform}.whl"
    payload["environment"]["artifact"]["wheel"] = wheel
    payload["environment"]["artifact"]["wheel_tags"] = [f"cp310-abi3-{wheel_platform}"]
    payload["environment"]["artifact"]["native"]["member"] = f"gffbase/{native_module}"
    payload["environment"]["gffbase_install"]["native_module"] = native_module

    assert benchmark_results_evidence_error(payload) is None


def test_full_gate_rejects_wrong_candidate_abi3_floor():
    from benchmarks.common import benchmark_results_evidence_error

    payload = _publishable_payload()
    payload["environment"]["artifact"]["wheel"] = (
        "gffbase-0.2.0rc1-cp39-abi3-manylinux_2_28_x86_64.whl"
    )

    assert benchmark_results_evidence_error(payload) is not None


@pytest.mark.parametrize(
    ("wheel_platform", "native_libc"),
    [
        ("manylinux_2_28_x86_64", "musl"),
        ("musllinux_1_2_x86_64", "gnu"),
    ],
)
def test_full_gate_cross_checks_wheel_and_native_linux_libc(wheel_platform, native_libc):
    from benchmarks.common import benchmark_results_evidence_error

    payload = _publishable_payload()
    wheel = f"gffbase-0.2.0rc1-cp313-cp313-{wheel_platform}.whl"
    native_module = f"_native.cpython-313-x86_64-linux-{native_libc}.so"
    artifact = payload["environment"]["artifact"]
    artifact["wheel"] = wheel
    artifact["wheel_tags"] = [f"cp313-cp313-{wheel_platform}"]
    artifact["native"]["member"] = f"gffbase/{native_module}"
    payload["environment"]["gffbase_install"]["native_module"] = native_module

    assert benchmark_results_evidence_error(payload) is not None


def test_full_gate_accepts_matching_linux_libc_runtime_evidence():
    from benchmarks.common import benchmark_results_evidence_error

    payload = _publishable_payload()

    assert benchmark_results_evidence_error(payload) is None


@pytest.mark.parametrize(
    ("wheel_platform", "family", "version"),
    [
        ("manylinux_2_28_x86_64", "musl", "1.2"),
        ("musllinux_1_2_x86_64", "glibc", "2.34"),
        ("manylinux_2_28_x86_64", "glibc", "2.27"),
        ("musllinux_1_2_x86_64", "musl", "1.1"),
    ],
)
def test_full_gate_rejects_incompatible_or_too_old_linux_libc(wheel_platform, family, version):
    from benchmarks.common import benchmark_results_evidence_error

    payload = _publishable_payload()
    payload["environment"]["libc"] = {"family": family, "version": version}
    artifact = payload["environment"]["artifact"]
    artifact["wheel"] = f"gffbase-0.2.0rc1-cp310-abi3-{wheel_platform}.whl"
    artifact["wheel_tags"] = [f"cp310-abi3-{wheel_platform}"]

    assert benchmark_results_evidence_error(payload) is not None


@pytest.mark.parametrize(
    ("wheel_platform", "family", "version"),
    [
        ("manylinux_2_28_x86_64", "glibc", "2.28"),
        ("musllinux_1_2_x86_64", "musl", "1.2"),
        ("linux_x86_64", "glibc", "2.17"),
        ("linux_x86_64", "musl", "1.2.4"),
    ],
)
def test_full_gate_accepts_compatible_linux_libc_boundaries(wheel_platform, family, version):
    from benchmarks.common import benchmark_results_evidence_error

    payload = _publishable_payload()
    payload["environment"]["libc"] = {"family": family, "version": version}
    artifact = payload["environment"]["artifact"]
    artifact["wheel"] = f"gffbase-0.2.0rc1-cp310-abi3-{wheel_platform}.whl"
    artifact["wheel_tags"] = [f"cp310-abi3-{wheel_platform}"]

    assert benchmark_results_evidence_error(payload) is None


@pytest.mark.parametrize(
    ("wheel_platform", "family", "version"),
    [
        ("manylinux_0_0_x86_64", "glibc", "2.34"),
        ("manylinux_1_0_x86_64", "glibc", "2.34"),
        ("manylinux_2_4_x86_64", "glibc", "2.34"),
        ("musllinux_0_0_x86_64", "musl", "1.2"),
        ("musllinux_1_0_x86_64", "musl", "1.2"),
    ],
)
def test_full_gate_rejects_unsupported_linux_policy_tags(wheel_platform, family, version):
    from benchmarks.common import benchmark_results_evidence_error

    payload = _publishable_payload()
    payload["environment"]["libc"] = {"family": family, "version": version}
    artifact = payload["environment"]["artifact"]
    artifact["wheel"] = f"gffbase-0.2.0rc1-cp310-abi3-{wheel_platform}.whl"
    artifact["wheel_tags"] = [f"cp310-abi3-{wheel_platform}"]

    assert benchmark_results_evidence_error(payload) is not None


def test_full_gate_cross_checks_generic_linux_native_libc_suffix():
    from benchmarks.common import benchmark_results_evidence_error

    payload = _publishable_payload()
    payload["environment"]["libc"] = {"family": "musl", "version": "1.2"}
    artifact = payload["environment"]["artifact"]
    artifact["wheel"] = "gffbase-0.2.0rc1-cp313-cp313-linux_x86_64.whl"
    artifact["wheel_tags"] = ["cp313-cp313-linux_x86_64"]
    native_module = "_native.cpython-313-x86_64-linux-gnu.so"
    artifact["native"]["member"] = f"gffbase/{native_module}"
    payload["environment"]["gffbase_install"]["native_module"] = native_module

    assert benchmark_results_evidence_error(payload) is not None

    native_module = "_native.cpython-313-x86_64-linux-musl.so"
    artifact["native"]["member"] = f"gffbase/{native_module}"
    payload["environment"]["gffbase_install"]["native_module"] = native_module

    assert benchmark_results_evidence_error(payload) is None


@pytest.mark.parametrize(
    "libc",
    [
        {"family": "unknown", "version": None},
        {"family": "glibc", "version": None},
        {"family": "glibc", "version": "02.34"},
        {"family": None, "version": None},
    ],
)
def test_full_gate_rejects_unknown_linux_libc_identity(libc):
    from benchmarks.common import benchmark_results_evidence_error

    payload = _publishable_payload()
    payload["environment"]["libc"] = libc

    assert benchmark_results_evidence_error(payload) is not None


def test_full_gate_rejects_equal_but_internally_impossible_signatures():
    from benchmarks.common import benchmark_results_evidence_error

    payload = _publishable_payload()
    signature = payload["corpora"]["mane"]["gffbase"]["correctness_signature"]
    forged = {key: value for key, value in signature.items() if key != "combined_sha256"}
    forged["segment_count"] = forged["feature_count"] - 1
    forged["combined_sha256"] = hashlib.sha256(
        json.dumps(
            {key: value for key, value in forged.items() if key != "combined_sha256"},
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
    ).hexdigest()
    payload["corpora"]["mane"]["gffbase"]["correctness_signature"] = dict(forged)
    payload["corpora"]["mane"]["legacy"]["correctness_signature"] = dict(forged)

    assert benchmark_results_evidence_error(payload) is not None


def test_full_gate_cross_checks_measured_timestamp_with_environment():
    from benchmarks.common import benchmark_results_evidence_error

    payload = _publishable_payload()
    payload["corpora"]["mane"]["measured"]["timestamp_utc"] = "2026-08-27T02:00:00Z"

    assert benchmark_results_evidence_error(payload) is not None


def test_full_gate_rejects_noncanonical_timestamp_chronology():
    from benchmarks.common import benchmark_results_evidence_error

    payload = _publishable_payload()
    payload["environment"]["timestamp_utc"] = "2026-9-30T01:02:03Z"
    payload["corpora"]["mane"]["measured"]["timestamp_utc"] = "2026-10-01T00:00:00Z"

    assert benchmark_results_evidence_error(payload) is not None


@pytest.mark.parametrize("python_version", ["3.9.19", "3.15.0", "3.99.1", "3.013.5"])
def test_full_gate_requires_canonical_supported_python_runtime(python_version):
    from benchmarks.common import benchmark_results_evidence_error

    payload = _publishable_payload()
    payload["environment"]["python"]["version"] = python_version

    assert benchmark_results_evidence_error(payload) is not None


@pytest.mark.parametrize(
    ("platform_name", "machine", "wheel_platform", "native_module"),
    [
        (
            "Solaris-11.4-x86_64",
            "x86_64",
            "manylinux_2_28_x86_64",
            "_native.cpython-313-x86_64-linux-gnu.so",
        ),
        (
            "Linux-6.12-mips64",
            "mips64",
            "manylinux_2_28_x86_64",
            "_native.cpython-313-x86_64-linux-gnu.so",
        ),
        (
            "Linux-6.12-x86_64",
            "x86_64",
            "manylinux_2_28_x86_64.manylinux_2_28_aarch64",
            "_native.cpython-313-x86_64-linux-gnu.so",
        ),
        (
            "Linux-6.12-x86_64",
            "x86_64",
            "manylinux_2_28_x86_64",
            "_native.abi3.so",
        ),
    ],
)
def test_full_gate_rejects_unknown_or_ambiguous_runtime_identity(
    platform_name, machine, wheel_platform, native_module
):
    from benchmarks.common import benchmark_results_evidence_error

    payload = _publishable_payload()
    payload["environment"]["platform"] = platform_name
    payload["environment"]["machine"] = machine
    payload["environment"]["artifact"]["wheel"] = (
        f"gffbase-0.2.0rc1-cp313-cp313-{wheel_platform}.whl"
    )
    payload["environment"]["gffbase_install"]["native_module"] = native_module

    assert benchmark_results_evidence_error(payload) is not None


@pytest.mark.parametrize("engine", ["gffbase", "legacy"])
def test_full_gate_requires_exact_ingest_labels(engine):
    from benchmarks.common import benchmark_results_evidence_error

    payload = _publishable_payload()
    payload["corpora"]["mane"][engine]["label"] = (
        "forged " + payload["corpora"]["mane"][engine]["label"]
    )

    assert benchmark_results_evidence_error(payload) is not None


def test_full_gate_requires_affinity_ids_within_logical_cpu_universe():
    from benchmarks.common import benchmark_results_evidence_error

    payload = _publishable_payload()
    payload["environment"]["cpu_affinity"] = [999]

    assert benchmark_results_evidence_error(payload) is not None


def test_schema_v3_type_is_literal_and_renderers_reject_unknown_schemas():
    from benchmarks.common import benchmark_results_evidence_error
    from tools import gen_benchmark_tables as tables

    numeric = _publishable_payload()
    numeric["schema_version"] = 3
    assert benchmark_results_evidence_error(numeric) is not None
    with pytest.raises(tables.Stale, match="unsupported benchmark schema"):
        tables.render_provenance(numeric)

    unknown = _publishable_payload()
    unknown["schema_version"] = "4"
    with pytest.raises(tables.Stale, match="unsupported benchmark schema"):
        tables.render_provenance(unknown)


@pytest.mark.parametrize(
    ("path", "value"),
    [
        (("gffbase", "peak_rss_bytes"), "forged"),
        (("gffbase", "peak_rss_mb"), 99.0),
        (("gffbase", "disk_bytes"), True),
        (("gffbase", "exit_code"), 0.0),
        (("gffbase", "label"), ""),
        (("gffbase", "fmt"), "bed"),
        (("gffbase", "validation", "checked"), []),
        (("gffbase", "validation", "warnings"), ["forged"]),
        (("legacy", "peak_rss_bytes"), "forged"),
        (("legacy", "peak_rss_mb"), 99.0),
        (("legacy", "disk_bytes"), True),
        (("legacy", "exit_code"), 0.0),
        (("legacy", "n_features"), 1000.0),
        (("legacy", "label"), ""),
    ],
)
def test_full_gate_rejects_forged_completed_ingest_values(path, value):
    from benchmarks.common import benchmark_results_evidence_error

    payload = _publishable_payload()
    target = payload["corpora"]["mane"]
    for key in path[:-1]:
        target = target[key]
    target[path[-1]] = value

    assert benchmark_results_evidence_error(payload) is not None


def test_full_gate_rejects_nonfinite_numbers_nested_in_validation_evidence():
    from benchmarks.common import benchmark_results_evidence_error

    payload = _publishable_payload()
    payload["corpora"]["mane"]["gffbase"]["validation"]["warnings"] = [
        {
            "invariant": "INV-X",
            "name": "example",
            "severity": "warning",
            "count": 1,
            "detail": "example warning",
            "examples": [float("nan")],
        }
    ]

    assert benchmark_results_evidence_error(payload) is not None


def _payload_with_timed_out_comparator() -> dict:
    payload = _publishable_payload()
    row = payload["corpora"]["mane"]
    old = row["legacy"]
    row["legacy"] = {
        "label": old["label"],
        "peak_rss_bytes": old["peak_rss_bytes"],
        "peak_rss_mb": old["peak_rss_mb"],
        "exit_code": -9,
        "state": "timed_out",
        "benchmark_env": old["benchmark_env"],
        "cap_seconds": old["cap_seconds"],
        "wall_seconds": None,
        "disk_bytes": 0,
        "stdout_bytes": 0,
        "stdout_sha256": hashlib.sha256(b"").hexdigest(),
        "stdout_parse_error": "invalid final JSON object",
    }
    row["ingest_speedup"] = None
    return payload


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("stdout_bytes", True),
        ("stdout_bytes", -1),
        ("stdout_sha256", "forged"),
        ("stdout_parse_error", "some parser detail"),
        ("peak_rss_mb", float("nan")),
    ],
)
def test_full_gate_requires_strict_timeout_diagnostics(field, value):
    from benchmarks.common import benchmark_results_evidence_error

    payload = _payload_with_timed_out_comparator()
    assert benchmark_results_evidence_error(payload) is None
    payload["corpora"]["mane"]["legacy"][field] = value

    assert benchmark_results_evidence_error(payload) is not None


def test_full_gate_requires_complete_timeout_diagnostics_and_no_completed_fields():
    from benchmarks.common import benchmark_results_evidence_error

    missing = _payload_with_timed_out_comparator()
    missing["corpora"]["mane"]["legacy"].pop("stdout_parse_error")
    assert benchmark_results_evidence_error(missing) is not None

    completed_leak = _payload_with_timed_out_comparator()
    completed_leak["corpora"]["mane"]["legacy"]["n_features"] = 1000
    assert benchmark_results_evidence_error(completed_leak) is not None


def test_tradeoffs_excludes_incomplete_engine_pairs_from_every_statistic():
    from benchmarks.common import benchmark_results_evidence_error
    from tools import gen_benchmark_tables as tables

    payload = _payload_with_timed_out_comparator()
    row = payload["corpora"]["mane"]
    row["gffbase"]["peak_rss_bytes"] = 8 << 40
    row["gffbase"]["peak_rss_mb"] = (8 << 40) / (1024 * 1024)
    row["gffbase"]["disk_bytes"] = 4 << 40
    row["legacy"]["peak_rss_bytes"] = 16 << 40
    row["legacy"]["peak_rss_mb"] = (16 << 40) / (1024 * 1024)

    assert benchmark_results_evidence_error(payload) is None
    rendered = tables.render_tradeoffs(payload)

    assert "Measured across 4 corpora" in rendered
    assert "8.00 TB" not in rendered
    assert "16.00 TB" not in rendered
    assert "4.00 TB" not in rendered
    assert "| 1.00 MB | 2.00 MB | 0.50× |" in rendered


@pytest.mark.parametrize(
    ("path", "value"),
    [
        (("legacy_cap_seconds",), -7),
        (("n_spatial",), True),
        (("n_batched",), 0),
        (("repeats",), True),
        (("region_seed",), 20260502),
        (("validation_sample",), "10000"),
        (("gtf_arm",), "default"),
        (("infer_gtf_parents",), True),
    ],
)
def test_full_gate_rejects_forged_or_inconsistent_params(path, value):
    from benchmarks.common import benchmark_results_evidence_error

    payload = _publishable_payload()
    row = payload["corpora"]["gencode-gtf"]
    target = row["params"]
    target[path[-1]] = value
    if path[-1] == "legacy_cap_seconds":
        row["legacy"]["cap_seconds"] = value

    assert benchmark_results_evidence_error(payload) is not None


def _payload_with_completed_queries() -> dict:
    payload = _publishable_payload()
    row = payload["corpora"]["mane"]
    candidate = row["gffbase"]
    candidate["rtree_built"] = True
    candidate["validation"]["skipped"] = []
    candidate["validation"]["checked_ids"].insert(7, "INV-8")
    candidate["validation"]["checked"].insert(7, "INV-8 checked")
    row["params"].update({"n_spatial": 6, "n_batched": 5, "repeats": 3})
    row["spatial"] = {
        "state": "completed",
        "n_queries": 6,
        "wall_seconds": 2.0,
        "qps": 3.0,
        "total_features_returned": 11,
        "timing": {
            "median": 2.0,
            "min": 1.0,
            "max": 3.0,
            "values": [1.0, 3.0, 2.0],
            "n": 3,
        },
    }
    row["batched"] = {
        "state": "completed",
        "n_anchors": 5,
        "n_descendants": 11,
        "wall_seconds": 0.6,
        "qps": 5 / 0.6,
        "timing": {
            "median": 0.6,
            "min": 0.5,
            "max": 0.7,
            "values": [0.5, 0.7, 0.6],
            "n": 3,
        },
    }
    return payload


@pytest.mark.parametrize(
    ("section", "path", "value"),
    [
        ("spatial", ("timing", "min"), 0.75),
        ("spatial", ("timing", "max"), 4.0),
        ("spatial", ("timing", "n"), 2),
        ("spatial", ("n_queries",), 5),
        ("spatial", ("total_features_returned",), True),
        ("batched", ("timing", "median"), 0.61),
        ("batched", ("n_anchors",), 6),
        ("batched", ("n_descendants",), True),
        ("batched", ("qps",), float("inf")),
    ],
)
def test_full_gate_recomputes_completed_query_evidence(section, path, value):
    from benchmarks.common import benchmark_results_evidence_error

    payload = _payload_with_completed_queries()
    assert benchmark_results_evidence_error(payload) is None
    target = payload["corpora"]["mane"][section]
    for key in path[:-1]:
        target = target[key]
    target[path[-1]] = value

    assert benchmark_results_evidence_error(payload) is not None


def test_full_gate_requires_nonempty_skipped_query_reason():
    from benchmarks.common import benchmark_results_evidence_error

    payload = _publishable_payload()
    payload["corpora"]["mane"]["spatial"]["reason"] = "  "

    assert benchmark_results_evidence_error(payload) is not None


@pytest.mark.parametrize(
    ("path", "value"),
    [
        (("name",), "Almost MANE"),
        (("input",), "benchmarks/data/not-mane.gff.gz"),
        (("input_bytes",), True),
        (("input_bytes",), 1),
        (("input_sha256",), "d" * 64),
        (("feature_lines",), True),
        (("gffbase", "fmt"), "gtf"),
    ],
)
def test_full_gate_requires_canonical_corpus_identity_and_format(path, value):
    from benchmarks.common import benchmark_results_evidence_error

    payload = _publishable_payload()
    target = payload["corpora"]["mane"]
    for key in path[:-1]:
        target = target[key]
    target[path[-1]] = value

    assert benchmark_results_evidence_error(payload) is not None


def test_shared_gate_requires_exact_primary_corpora_and_closed_shapes():
    from benchmarks.common import benchmark_results_evidence_error

    payload = _publishable_payload()
    assert benchmark_results_evidence_error(payload) is None

    partial = json.loads(json.dumps(payload))
    partial["corpora"] = {"mane": partial["corpora"]["mane"]}
    assert "primary" in benchmark_results_evidence_error(partial)

    extra = json.loads(json.dumps(payload))
    extra["corpora"]["gencode-gtf-default"] = {
        **extra["corpora"]["mane"],
        "key": "gencode-gtf-default",
    }
    assert "primary" in benchmark_results_evidence_error(extra)

    stale = json.loads(json.dumps(payload))
    stale["corpora"]["mane"]["legacy"]["wall_seconds_lower_bound"] = 10
    assert "legacy" in benchmark_results_evidence_error(stale)


@pytest.mark.parametrize(
    ("path", "value", "needle"),
    [
        (("corpora", "mane", "gffbase", "cap_seconds"), None, "candidate"),
        (("corpora", "mane", "legacy", "cap_seconds"), 11, "comparator cap"),
        (("corpora", "mane", "params", "unexpected"), True, "params shape"),
        (("corpora", "mane", "db_paths", "gffbase"), "/private/db", "absolute path"),
    ],
)
def test_shared_gate_rejects_closed_state_cap_and_path_mutations(path, value, needle):
    from benchmarks.common import benchmark_results_evidence_error

    payload = _publishable_payload()
    target = payload
    for key in path[:-1]:
        target = target[key]
    target[path[-1]] = value

    assert needle in benchmark_results_evidence_error(payload)


def test_external_benchmark_out_purge_uses_actual_run_paths(tmp_path, monkeypatch):
    module = _mega_module()
    external_out = tmp_path / "external-out"
    external_out.mkdir()
    monkeypatch.setattr(module, "OUT", external_out)
    candidate = external_out / "mane.duckdb"
    legacy = external_out / "mane_legacy.sqlite"
    candidate.write_bytes(b"candidate")
    legacy.write_bytes(b"legacy")

    assert module._purge_run_databases(candidate, legacy) == len(b"candidatelegacy")
    assert not candidate.exists() and not legacy.exists()


def test_renderer_reads_schema_v3_nested_python_provenance_version():
    from tools import gen_benchmark_tables as tables

    payload = _publishable_payload()
    assert "Python 3.13.5" in tables.render_provenance(payload)


def test_renderer_reads_actual_schema_v2_top_level_python_version():
    from tools import gen_benchmark_tables as tables

    historical = json.loads(RESULTS.read_text(encoding="utf-8"))
    rendered = tables.render_provenance(historical)

    assert "Python 3.13.5" in rendered
    assert "Python ?" not in rendered


def test_historical_schema_v2_raw_bytes_are_immutable():
    assert hashlib.sha256(RESULTS.read_bytes()).hexdigest() == (
        "d215d19fcf67d226dda401ed9069c75d524494904faced588f23cc81712db53d"
    )


@pytest.mark.parametrize(
    "renderer_name", ["render_corpus_table", "render_provenance", "render_tradeoffs"]
)
def test_schema_v2_renderers_reject_mutated_historical_objects(renderer_name):
    from tools import gen_benchmark_tables as tables

    historical = json.loads(RESULTS.read_text(encoding="utf-8"))
    historical["corpora"]["gencode-gtf"]["ingest_speedup"] = 999

    with pytest.raises(tables.Stale, match="historical schema-v2"):
        getattr(tables, renderer_name)(historical)


def test_generator_main_rejects_schema_v2_with_changed_raw_bytes(tmp_path, monkeypatch):
    from tools import gen_benchmark_tables as tables

    altered = tmp_path / "06_mega.json"
    altered.write_bytes(RESULTS.read_bytes() + b" ")
    process_called = False

    def unexpected_process(*_args, **_kwargs):
        nonlocal process_called
        process_called = True
        return []

    monkeypatch.setattr(tables, "MEGA", altered)
    monkeypatch.setattr(tables, "process", unexpected_process)
    monkeypatch.setattr(sys, "argv", ["gen_benchmark_tables.py", "--check"])

    assert tables.main() == 1
    assert process_called is False


@pytest.mark.parametrize(
    "renderer_name", ["render_corpus_table", "render_provenance", "render_tradeoffs"]
)
def test_every_schema_v3_renderer_routes_through_the_complete_payload_gate(renderer_name):
    from tools import gen_benchmark_tables as tables

    payload = _publishable_payload()
    payload["environment"]["artifact"]["wheel"] = "/home/private/candidate.whl"

    with pytest.raises(tables.Stale, match="invalid schema-v3 evidence"):
        getattr(tables, renderer_name)(payload)


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
