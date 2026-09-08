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
"""Executable contracts for release routing and workflow dependencies."""

from __future__ import annotations

import re
from datetime import date, timedelta
from pathlib import Path

import pytest

from tools import release_policy

REPO_ROOT = Path(__file__).resolve().parent.parent


def _source_tree(
    tmp_path: Path,
    version: str,
    *,
    release_state: str | None = None,
    citation_date: str | None = None,
) -> Path:
    """Create the three release-metadata files consumed by the policy helper."""
    root = tmp_path / version
    root.mkdir()
    (root / "pyproject.toml").write_text(
        f'[project]\nname = "gffbase"\nversion = "{version}"\n',
        encoding="utf-8",
    )
    state = release_state or ("Unreleased" if "rc" in version else "2026-09-01")
    (root / "CHANGELOG.md").write_text(
        f"## [{version}] — {state}\n\n[{version}]: https://example.invalid/releases/{version}\n",
        encoding="utf-8",
    )
    cff = [
        "cff-version: 1.2.0",
        "message: This repository describes the unreleased candidate."
        if "rc" in version
        else "message: Cite this release.",
        f"version: {version}",
    ]
    if citation_date is not None:
        cff.append(f'date-released: "{citation_date}"')
    elif "rc" not in version:
        cff.append(f'date-released: "{state}"')
    (root / "CITATION.cff").write_text("\n".join(cff) + "\n", encoding="utf-8")
    return root


@pytest.mark.parametrize(
    ("channel", "version", "ref_name"),
    [
        ("production", "1.2.3", "v1.2.3"),
        ("testpypi", "1.2.3rc4", "v1.2.3rc4"),
    ],
)
def test_tag_push_routes_only_an_exact_matching_channel(
    tmp_path: Path, channel: str, version: str, ref_name: str
):
    root = _source_tree(tmp_path, version)
    decision = release_policy.evaluate_release(
        channel=channel,
        event_name="push",
        ref_type="tag",
        ref_name=ref_name,
        publish_requested=False,
        source_root=root,
    )
    assert decision.version == version
    assert decision.publish is True
    assert decision.reason == "validated tag push"


@pytest.mark.parametrize(
    ("channel", "source_version", "ref_name"),
    [
        ("production", "1.2.3rc1", "v1.2.3rc1"),
        ("testpypi", "1.2.3", "v1.2.3"),
        ("production", "1.2.3", "v1.2.4"),
        ("testpypi", "1.2.3rc1", "v1.2.3rc2"),
        ("production", "1.2.3", "v1.2.3.post1"),
        ("production", "1.2.3", "v1.2.3+local"),
        ("testpypi", "1.2.3rc1", "v1.2.3-rc.1"),
        ("testpypi", "1.2.3rc1", "v1.2.3RC1"),
        ("testpypi", "1.2.3rc1", "v1.2.3a1"),
        ("testpypi", "1.2.3rc1", "v1.2.3b1"),
        ("testpypi", "1.2.3rc1", "v1.2.3.dev1"),
        ("production", "1.2.3", "1.2.3"),
        ("production", "1.2.3", "not-a-version"),
    ],
)
def test_tag_push_rejects_wrong_channel_noncanonical_or_mismatched_tags(
    tmp_path: Path, channel: str, source_version: str, ref_name: str
):
    root = _source_tree(tmp_path, source_version)
    with pytest.raises(release_policy.ReleasePolicyError):
        release_policy.evaluate_release(
            channel=channel,
            event_name="push",
            ref_type="tag",
            ref_name=ref_name,
            publish_requested=False,
            source_root=root,
        )


@pytest.mark.parametrize("channel", ["production", "testpypi"])
def test_manual_dispatch_is_build_only_by_default_even_from_version_named_branch(
    tmp_path: Path, channel: str
):
    root = _source_tree(tmp_path, "1.2.3rc1")
    decision = release_policy.evaluate_release(
        channel=channel,
        event_name="workflow_dispatch",
        ref_type="branch",
        ref_name="v1.2.3rc1",
        publish_requested=False,
        source_root=root,
    )
    assert decision.publish is False
    assert decision.version == "1.2.3rc1"
    assert decision.reason == "manual build-only verification"


@pytest.mark.parametrize("channel", ["production", "testpypi"])
def test_manual_publication_rejects_a_version_named_branch(tmp_path: Path, channel: str):
    root = _source_tree(tmp_path, "1.2.3rc1")
    with pytest.raises(release_policy.ReleasePolicyError, match="tag"):
        release_policy.evaluate_release(
            channel=channel,
            event_name="workflow_dispatch",
            ref_type="branch",
            ref_name="v1.2.3rc1",
            publish_requested=True,
            source_root=root,
        )


def test_release_policy_rejects_unknown_events_and_non_boolean_confirmation(tmp_path: Path):
    root = _source_tree(tmp_path, "1.2.3rc1")
    with pytest.raises(release_policy.ReleasePolicyError, match="event"):
        release_policy.evaluate_release(
            channel="testpypi",
            event_name="pull_request",
            ref_type="branch",
            ref_name="main",
            publish_requested=False,
            source_root=root,
        )
    with pytest.raises(release_policy.ReleasePolicyError, match="boolean"):
        release_policy.parse_publish_requested("yes")


def test_prerelease_metadata_must_be_unreleased_and_undated(tmp_path: Path):
    dated = _source_tree(
        tmp_path,
        "1.2.3rc1",
        release_state="2026-09-01",
        citation_date="2026-09-01",
    )
    with pytest.raises(release_policy.ReleasePolicyError, match="Unreleased"):
        release_policy.validate_release_metadata(dated, "1.2.3rc1")


@pytest.mark.parametrize(
    ("state", "citation_date", "message"),
    [
        ("Unreleased", None, "stable"),
        ("2026-09-01", None, "date-released"),
        ("2026-09-01", "2026-09-02", "disagree"),
        (
            (date.today() + timedelta(days=1)).isoformat(),
            (date.today() + timedelta(days=1)).isoformat(),
            "future",
        ),
    ],
)
def test_stable_metadata_requires_one_nonfuture_release_date(
    tmp_path: Path, state: str, citation_date: str | None, message: str
):
    root = _source_tree(
        tmp_path,
        "1.2.3",
        release_state=state,
        citation_date=citation_date,
    )
    if citation_date is None:
        citation = root / "CITATION.cff"
        citation.write_text(
            "cff-version: 1.2.0\nmessage: Cite this release.\nversion: 1.2.3\n",
            encoding="utf-8",
        )
    with pytest.raises(release_policy.ReleasePolicyError, match=message):
        release_policy.validate_release_metadata(root, "1.2.3")


def _workflow(name: str) -> dict:
    yaml = pytest.importorskip("yaml")
    return yaml.load(
        (REPO_ROOT / ".github" / "workflows" / name).read_text(encoding="utf-8"),
        Loader=yaml.BaseLoader,
    )


@pytest.mark.parametrize("filename", ["release.yml", "testpypi-release.yml"])
def test_manual_release_input_is_a_typed_opt_in_and_policy_precedes_qualification(filename: str):
    workflow = _workflow(filename)
    publish_input = workflow["on"]["workflow_dispatch"]["inputs"]["publish"]
    assert publish_input["type"] == "boolean"
    assert publish_input["default"] == "false"
    assert publish_input["required"] == "true"

    jobs = workflow["jobs"]
    assert "python tools/release_policy.py" in "\n".join(
        step.get("run", "") for step in jobs["policy"]["steps"]
    )
    assert jobs["qualification"]["needs"] == "policy"
    assert jobs["qualification"]["uses"] == "./.github/workflows/qualification.yml"
    assert jobs["artifacts"]["needs"] == "qualification"
    assert jobs["artifacts"]["uses"] == "./.github/workflows/release-artifacts.yml"

    publish_job = jobs["publish"] if filename == "release.yml" else jobs["publish-testpypi"]
    assert publish_job["needs"] == "artifacts"
    assert publish_job["if"] == "needs.policy.outputs.publish == 'true'"


@pytest.mark.parametrize("filename", ["release.yml", "testpypi-release.yml"])
def test_thin_publisher_callers_never_build_package_artifacts(filename: str):
    text = (REPO_ROOT / ".github" / "workflows" / filename).read_text(encoding="utf-8")
    assert "maturin-action" not in text
    assert "python -m build" not in text
    assert "cargo build" not in text


def _commands(job: dict) -> str:
    return "\n".join(step.get("run", "") for step in job.get("steps", []))


def test_reusable_qualification_has_the_literal_supported_compatibility_matrix():
    workflow = _workflow("qualification.yml")
    assert workflow["permissions"] == {"contents": "read"}
    job = workflow["jobs"]["compatibility"]
    matrix = job["strategy"]["matrix"]
    assert matrix == {
        "os": ["ubuntu-latest", "macos-latest", "windows-latest"],
        "python-version": ["3.10", "3.11", "3.12", "3.13", "3.14"],
        "spatial": ["rtree", "btree"],
    }
    assert job["runs-on"] == "${{ matrix.os }}"
    assert job["strategy"]["fail-fast"] == "false"
    commands = _commands(job)
    assert 'pip install -e ".[test]"' in commands
    assert "assert gffbase.native_available()" in commands
    assert "GFFBASE_TEST_DISABLE_RTREE" in str(job)
    assert "assert db._rtree_built is expected" in commands
    assert (
        'pytest -m "not corpus and not parity and not slow and not property and not pandas_docs"'
        in commands
    )


def test_reusable_qualification_names_every_independent_release_gate():
    workflow = _workflow("qualification.yml")
    jobs = workflow["jobs"]
    assert set(jobs) == {
        "compatibility",
        "python-fallback",
        "minimum-deps",
        "slow",
        "properties",
        "parity",
        "corpus",
        "rust",
        "documentation",
        "package",
        "lint",
        "workflow-security",
    }

    fallback = _commands(jobs["python-fallback"])
    assert "PYTHONPATH=python" in fallback
    assert "assert not gffbase.native_available()" in fallback
    assert "pip install -e" not in fallback

    minimum = jobs["minimum-deps"]
    assert minimum["strategy"]["matrix"] == {"spatial": ["rtree", "btree"]}
    minimum_commands = _commands(minimum)
    assert "duckdb==1.4.1" in minimum_commands
    assert "pyarrow==18.1.0" in minimum_commands
    assert "pip check" in minimum_commands

    properties = jobs["properties"]
    includes = properties["strategy"]["matrix"]["include"]
    assert {(item["profile"], item["test-file"]) for item in includes} == {
        ("quick", "tests/test_parser_properties.py"),
        ("quick", "tests/test_property_database.py"),
        ("extended", "tests/test_parser_properties.py"),
        ("extended", "tests/test_property_database.py"),
    }
    assert "hypothesis==6.165.5" in _commands(properties)
    assert "--durations=0" in _commands(properties)

    lint = _commands(jobs["lint"])
    assert "ruff check" in lint
    assert "ruff format --check" in lint
    assert "mypy" in lint
    assert "cargo clippy" in lint
    assert "cargo fmt" in lint

    assert "cargo test --locked --release" in _commands(jobs["rust"])
    assert "pytest -m parity" in _commands(jobs["parity"])
    assert "pytest -m corpus" in _commands(jobs["corpus"])


def test_qualification_package_gate_inspects_and_clean_installs_exact_archives():
    workflow = _workflow("qualification.yml")
    commands = _commands(workflow["jobs"]["package"])
    assert "python -m build --wheel --sdist" in commands
    assert "tools/release_artifacts.py inspect" in commands
    assert "--expected-target linux-x86_64" in commands
    assert "--expected-target sdist" in commands
    assert "qualified-wheel" in commands
    assert "qualified-sdist" in commands
    assert "pip check" in commands
    assert "native_available()" in commands
    assert "validate_db" in commands


def test_qualification_docs_are_strict_and_install_the_pinned_oracle():
    workflow = _workflow("qualification.yml")
    commands = _commands(workflow["jobs"]["documentation"])
    assert "git+https://github.com/daler/gffutils" in commands
    assert "6b84330f472dd2b4c69e36f319da7ade95bd5961" in commands
    assert 'tests/test_docs_snippets.py -m "not pandas_docs"' in commands
    assert "tests/test_docs_snippets.py -m pandas_docs" in commands
    assert "mkdocs build --strict" in commands


def test_reusable_artifact_workflow_builds_each_release_target_once():
    workflow = _workflow("release-artifacts.yml")
    assert workflow["permissions"] == {"contents": "read"}
    assert workflow["on"]["workflow_call"]["inputs"] == {
        "expected_version": {"required": "true", "type": "string"},
        "source_sha": {"required": "true", "type": "string"},
    }
    jobs = workflow["jobs"]
    assert set(jobs) == {
        "build-linux",
        "build-macos",
        "build-windows",
        "build-sdist",
        "wheel-smoke",
        "sdist-smoke",
        "validate-artifacts",
    }

    linux = jobs["build-linux"]["strategy"]["matrix"]["include"]
    assert linux == [
        {"target": "linux-x86_64", "runner": "ubuntu-24.04", "rust-target": "x86_64"},
        {"target": "linux-aarch64", "runner": "ubuntu-24.04-arm", "rust-target": "aarch64"},
    ]
    macos = jobs["build-macos"]["strategy"]["matrix"]["include"]
    assert macos == [
        {
            "target": "macos-x86_64",
            "runner": "macos-15-intel",
            "rust-target": "x86_64-apple-darwin",
        },
        {
            "target": "macos-arm64",
            "runner": "macos-15",
            "rust-target": "aarch64-apple-darwin",
        },
    ]
    assert jobs["build-windows"]["runs-on"] == "windows-latest"

    all_commands = "\n".join(_commands(job) for job in jobs.values())
    all_uses = "\n".join(
        step.get("uses", "") for job in jobs.values() for step in job.get("steps", [])
    )
    assert all_uses.count("PyO3/maturin-action@") == 4
    assert "--locked" in str(workflow)
    assert all_commands.count("python -m build") == 0
    assert all_uses.count("actions/upload-artifact@") == 5
    assert str(workflow).count("if-no-files-found") == 6  # five packages plus manifest


def test_every_release_wheel_is_install_tested_on_five_supported_pythons():
    workflow = _workflow("release-artifacts.yml")
    job = workflow["jobs"]["wheel-smoke"]
    matrix = job["strategy"]["matrix"]
    assert matrix["target"] == sorted(PLATFORMS_FOR_WORKFLOW)
    assert matrix["python-version"] == ["3.10", "3.11", "3.12", "3.13", "3.14"]
    mapping = {item["target"]: item["runner"] for item in matrix["include"]}
    assert mapping == PLATFORMS_FOR_WORKFLOW
    commands = _commands(job)
    assert "tools/release_artifacts.py inspect" in commands
    assert "pip install --force-reinstall" in commands
    assert "gffbase._native" in commands
    assert "native.__version__" in commands
    assert "validate_db" in commands


PLATFORMS_FOR_WORKFLOW = {
    "linux-aarch64": "ubuntu-24.04-arm",
    "linux-x86_64": "ubuntu-24.04",
    "macos-arm64": "macos-15",
    "macos-x86_64": "macos-15-intel",
    "windows-x86_64": "windows-latest",
}


def test_artifact_aggregator_waits_for_native_smokes_and_emits_one_manifest():
    workflow = _workflow("release-artifacts.yml")
    job = workflow["jobs"]["validate-artifacts"]
    assert set(job["needs"]) == {
        "build-linux",
        "build-macos",
        "build-windows",
        "build-sdist",
        "wheel-smoke",
        "sdist-smoke",
    }
    commands = _commands(job)
    assert "tools/release_artifacts.py create-manifest" in commands
    assert "--source-sha" in commands
    uploads = [step for step in job["steps"] if "actions/upload-artifact@" in step.get("uses", "")]
    assert len(uploads) == 1
    assert uploads[0]["with"]["name"] == "release-verification"
    assert uploads[0]["with"]["path"].endswith("release-artifact-manifest.json")


def test_every_remote_action_in_release_qualification_is_commit_pinned():
    sha_ref = re.compile(r"^[^@]+@[0-9a-f]{40}$")
    offenders = []
    for filename in (
        "qualification.yml",
        "release-artifacts.yml",
        "release.yml",
        "testpypi-release.yml",
    ):
        workflow = _workflow(filename)
        for job_name, job in workflow["jobs"].items():
            job_uses = job.get("uses")
            if job_uses and not job_uses.startswith("./") and not sha_ref.fullmatch(job_uses):
                offenders.append(f"{filename}:{job_name}:{job_uses}")
            for index, step in enumerate(job.get("steps", [])):
                uses = step.get("uses")
                if uses and not uses.startswith("./") and not sha_ref.fullmatch(uses):
                    offenders.append(f"{filename}:{job_name}:{index}:{uses}")
    assert not offenders, f"mutable action references remain: {offenders}"
