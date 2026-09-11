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
    # `policy` as well as `qualification`: this job reads
    # `needs.policy.outputs.version`, and `needs` resolves only for direct
    # dependencies. This assertion previously pinned the broken form, so the
    # test agreed with the bug instead of catching it -- see
    # `test_no_job_reads_a_needs_output_it_does_not_depend_on`, which states the
    # rule generally.
    assert set(jobs["artifacts"]["needs"]) == {"policy", "qualification"}
    assert jobs["artifacts"]["uses"] == "./.github/workflows/release-artifacts.yml"

    publish_job = jobs["publish"] if filename == "release.yml" else jobs["publish-testpypi"]
    assert set(publish_job["needs"]) == {"policy", "artifacts"}
    assert publish_job["if"] == "needs.policy.outputs.publish == 'true'"


def _third_party_top_level_imports(path: Path) -> set[str]:
    """Distribution-level modules a script imports unconditionally at load time.

    Conditional imports (inside `try:`) are excluded: those are fallbacks the
    script survives without. Everything else must be importable before the
    first line of `main()` runs.
    """
    import ast
    import sys

    tree = ast.parse(path.read_text(encoding="utf-8"))
    names: set[str] = set()
    for node in tree.body:
        if isinstance(node, ast.Import):
            names.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            names.add(node.module.split(".")[0])
    return {n for n in names if n != "__future__" and n not in sys.stdlib_module_names}


@pytest.mark.parametrize("filename", ["release.yml", "testpypi-release.yml"])
def test_the_policy_job_installs_what_the_policy_script_imports(filename: str):
    """The policy job runs on a bare `setup-python` runner, not the dev env.

    `release_policy.py` imports `packaging`, which a fresh runner does not
    have, and neither publisher installed it -- so the policy step died with
    `ModuleNotFoundError` on every run and nothing downstream could publish.
    Every local check passed because the dev environment happens to carry
    `packaging`. Found by a build-only rehearsal dispatch before the rc tag was
    pushed; had the tag gone first, the candidate number would have been spent
    on it.

    Derived from the script rather than listing `packaging`, so a new
    third-party import there fails this test instead of the next release.
    """
    required = _third_party_top_level_imports(REPO_ROOT / "tools" / "release_policy.py")
    assert required, "expected release_policy.py to import at least one third-party module"

    steps = _workflow(filename)["jobs"]["policy"]["steps"]
    runs = [step.get("run", "") for step in steps]
    policy_index = next(i for i, run in enumerate(runs) if "python tools/release_policy.py" in run)
    installed_before = "\n".join(runs[:policy_index])

    missing = [
        name
        for name in sorted(required)
        if not re.search(rf"pip install\b[^\n]*['\"]?{re.escape(name)}==[0-9]", installed_before)
    ]
    assert not missing, (
        f"{filename}: the policy job runs tools/release_policy.py without first "
        f"installing {missing} with an exact pin"
    )


@pytest.mark.parametrize("filename", ["release.yml", "testpypi-release.yml"])
def test_thin_publisher_callers_never_build_package_artifacts(filename: str):
    text = (REPO_ROOT / ".github" / "workflows" / filename).read_text(encoding="utf-8")
    assert "maturin-action" not in text
    assert "python -m build" not in text
    assert "cargo build" not in text


def _commands(job: dict) -> str:
    return "\n".join(step.get("run", "") for step in job.get("steps", []))


_RUST_TOOLCHAIN_INPUTS = frozenset({"toolchain", "target", "targets", "components"})
_WORKFLOW_FILES = sorted(p.name for p in (REPO_ROOT / ".github" / "workflows").glob("*.yml"))


@pytest.mark.parametrize("filename", _WORKFLOW_FILES)
def test_rust_toolchain_steps_install_what_their_job_runs(filename: str):
    """`with: { components: clippy, rustfmt }` installed clippy and not rustfmt.

    In a YAML flow mapping the comma separates *entries*, so that line is
    `components: clippy` plus a stray key `rustfmt`. The action warned about
    the unexpected input and moved on; `cargo fmt` then failed with
    "'cargo-fmt' is not installed for the toolchain". `ci.yml` wrote the same
    list in block style, where it is one value, which is why only the release
    qualification broke.
    """
    for job_name, job in (_workflow(filename).get("jobs") or {}).items():
        runs = _commands(job)
        for step in job.get("steps") or []:
            if "dtolnay/rust-toolchain" not in step.get("uses", ""):
                continue
            inputs = step.get("with") or {}
            unexpected = sorted(set(inputs) - _RUST_TOOLCHAIN_INPUTS)
            assert not unexpected, (
                f"{filename}:{job_name}: rust-toolchain ignores inputs {unexpected} -- "
                "a flow mapping splits `components: a, b` at the comma"
            )
            components = {c.strip() for c in inputs.get("components", "").split(",") if c.strip()}
            for command, component in (("cargo fmt", "rustfmt"), ("cargo clippy", "clippy")):
                if command in runs:
                    assert component in components, (
                        f"{filename}:{job_name} runs `{command}` without installing {component}"
                    )


#: Options `maturin sdist` accepts (maturin 1.14). It compiles nothing, so the
#: build-only flags -- `--release`, `--locked`, `--target` -- are rejected.
_MATURIN_SDIST_OPTIONS = frozenset({"--out", "-o", "--manifest-path", "-m", "--verbose", "-v"})


def test_the_sdist_step_passes_only_options_maturin_sdist_accepts():
    """`maturin sdist --locked` exits 2 with "unexpected argument".

    The sdist job copied the wheel builds' `--release --locked` minus
    `--release`, and nothing ran it until the first artifact build after a
    fully green qualification -- which it then failed, blocking publication.
    The lock file is shipped verbatim in the sdist; `--locked` means nothing
    to a command that resolves no dependencies.
    """
    steps = [
        step
        for job in _workflow("release-artifacts.yml")["jobs"].values()
        for step in job.get("steps") or []
        if "PyO3/maturin-action" in step.get("uses", "")
        and (step.get("with") or {}).get("command") == "sdist"
    ]
    assert steps, "expected the release artifacts workflow to build an sdist"
    for step in steps:
        flags = {arg for arg in step["with"].get("args", "").split() if arg.startswith("-")}
        assert flags <= _MATURIN_SDIST_OPTIONS, (
            f"`maturin sdist` rejects {sorted(flags - _MATURIN_SDIST_OPTIONS)}"
        )


def test_release_artifacts_py_gets_a_toml_reader_on_python_3_10():
    """`tools/release_artifacts.py` imports `tomllib`, falling back to `tomli`.

    `tomllib` is 3.11+, and the install job ran the script on 3.10 with only
    `packaging` installed -- so every Python 3.10 wheel-install check failed
    with both imports missing, on every platform, after the wheels themselves
    had built. Any job that may run the script below 3.11 must install `tomli`.
    """
    for job_name, job in _workflow("release-artifacts.yml")["jobs"].items():
        runs = _commands(job)
        if "tools/release_artifacts.py" not in runs:
            continue
        declared = [
            str((step.get("with") or {}).get("python-version", ""))
            for step in job.get("steps") or []
            if "actions/setup-python" in step.get("uses", "")
        ]
        matrix = ((job.get("strategy") or {}).get("matrix") or {}).get("python-version") or []
        versions = [v for v in declared if "$" not in v] + [str(v) for v in matrix]
        if any(tuple(int(x) for x in v.split(".")[:2]) < (3, 11) for v in versions):
            assert "tomli" in runs, f"release-artifacts.yml:{job_name} runs on 3.10 without tomli"


@pytest.mark.parametrize("job_name", ["wheel-smoke", "sdist-smoke"])
def test_clean_install_checks_run_in_a_fresh_virtualenv(job_name: str):
    """A clean-install check has to run in a clean environment.

    `wheel-smoke` installed the wheel into the runner's global interpreter and
    ran `pip check` over everything there -- including what the runner image
    preinstalls. On the Windows Python 3.12 image that is pipx, which requires
    `packaging>=26` against the `packaging==25.0` pin the job had just
    installed, so the check failed on a conflict gffbase has nothing to do
    with. `sdist-smoke` already used a venv; now both do.
    """
    runs = _commands(_workflow("release-artifacts.yml")["jobs"][job_name])
    assert "-m venv" in runs, f"{job_name} does not create a virtualenv"
    assert "python -m pip check" not in runs, (
        f"{job_name} runs `pip check` against the runner's global interpreter"
    )


def test_the_package_gate_builds_a_wheel_pypi_would_accept():
    """`python -m build` drives maturin's PEP 517 backend, which tags a host
    build `linux_x86_64` -- a tag PyPI rejects and `release_artifacts.py
    inspect` therefore refuses. The gate failed on its own build step.
    """
    steps = _workflow("qualification.yml")["jobs"]["package"]["steps"]
    build = next(step for step in steps if "python -m build" in step.get("run", ""))
    assert "--compatibility pypi" in (build.get("env") or {}).get("MATURIN_PEP517_ARGS", "")


def _test_extra() -> list:
    from packaging.requirements import Requirement

    try:
        import tomllib
    except ModuleNotFoundError:  # pragma: no cover - Python 3.10
        import tomli as tomllib  # type: ignore[no-redef]

    with (REPO_ROOT / "pyproject.toml").open("rb") as stream:
        extra = tomllib.load(stream)["project"]["optional-dependencies"]["test"]
    return [Requirement(line) for line in extra]


def test_hand_written_test_environments_cover_the_test_extra():
    """Two qualification jobs list their test dependencies by hand instead of
    installing `.[test]` -- one must not build gffbase, the other pins exact
    floors -- and both lists predate `psutil` joining the extra. 41 tests per
    job died on `ModuleNotFoundError: No module named 'psutil'`.
    """
    jobs = _workflow("qualification.yml")["jobs"]
    checked = 0
    for job_name, job in jobs.items():
        steps = job.get("steps") or []
        runs = _commands(job)
        if "pip install" not in runs or "'pytest" not in runs:
            continue
        python = next(
            (
                (step.get("with") or {}).get("python-version", "")
                for step in steps
                if "actions/setup-python" in step.get("uses", "")
            ),
            "",
        )
        for requirement in _test_extra():
            if requirement.marker is not None and python and "$" not in python:
                if not requirement.marker.evaluate({"python_version": python}):
                    continue
            assert re.search(
                rf"['\"]{re.escape(requirement.name)}(\[|[<>=!~]|['\"])", runs, re.IGNORECASE
            ), (
                f"qualification.yml:{job_name} hand-lists test dependencies without {requirement.name}"
            )
        checked += 1
    assert checked >= 2, "expected the fallback and minimum-deps jobs to be checked"


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
    assert 'make -C docs html SPHINXOPTS="-W --keep-going"' in commands


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

    # Every upload must fail rather than publish nothing. Asserted as a
    # property of each step, not as a count of the string across the whole
    # file: the count said 6 while the step count said 5, and no arrangement
    # satisfies both -- an upload step cannot carry the key twice. A magic
    # total also goes stale the moment a job is matrixed, which is exactly how
    # the two numbers drifted apart. This form cannot.
    unguarded = [
        f"{job_name}:{index}"
        for job_name, job in jobs.items()
        for index, step in enumerate(job.get("steps", []))
        if "upload-artifact" in (step.get("uses") or "")
        and step.get("with", {}).get("if-no-files-found") != "error"
    ]
    assert not unguarded, f"uploads that would silently ship nothing: {unguarded}"


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


def _all_workflow_filenames() -> list[str]:
    """Every workflow, discovered -- not a list that a new file can miss.

    This used to name four files. `ci.yml`, `docs.yml` and `property.yml` were
    therefore unpinned and unnoticed, while `qualification.yml` ran
    `zizmor --pedantic` over *all* of them. Globbing means the next workflow
    added is covered the moment it exists.
    """
    return sorted(p.name for p in (REPO_ROOT / ".github" / "workflows").glob("*.yml"))


_SHA_REF = re.compile(r"^[^@]+@[0-9a-f]{40}$")
_SECRET_EXPRESSION = re.compile(r"\$\{\{[^}]*secrets\.")


def _unpinned_actions(name: str, workflow: dict) -> list[str]:
    offenders = []
    for job_name, job in workflow["jobs"].items():
        job_uses = job.get("uses")
        if job_uses and not job_uses.startswith("./") and not _SHA_REF.fullmatch(job_uses):
            offenders.append(f"{name}:{job_name}:{job_uses}")
        for index, step in enumerate(job.get("steps", [])):
            uses = step.get("uses")
            if uses and not uses.startswith("./") and not _SHA_REF.fullmatch(uses):
                offenders.append(f"{name}:{job_name}:{index}:{uses}")
    return offenders


def _checkouts_keeping_credentials(name: str, workflow: dict) -> list[str]:
    offenders = []
    for job_name, job in workflow["jobs"].items():
        for index, step in enumerate(job.get("steps", [])):
            if not step.get("uses", "").startswith("actions/checkout@"):
                continue
            # BaseLoader keeps scalars as strings, so `false` arrives as "false".
            if step.get("with", {}).get("persist-credentials") != "false":
                offenders.append(f"{name}:{job_name}:step {index}")
    return offenders


def _secrets_spliced_into_scripts(name: str, workflow: dict) -> list[str]:
    offenders = []
    for job_name, job in workflow["jobs"].items():
        for index, step in enumerate(job.get("steps", [])):
            if _SECRET_EXPRESSION.search(step.get("run", "")):
                offenders.append(f"{name}:{job_name}:step {index}")
    return offenders


def test_every_remote_action_in_every_workflow_is_commit_pinned():
    offenders: list[str] = []
    for filename in _all_workflow_filenames():
        offenders += _unpinned_actions(filename, _workflow(filename))
    assert not offenders, f"mutable action references remain: {offenders}"


def test_every_checkout_in_every_workflow_drops_the_credential():
    """`actions/checkout` writes the job token into `.git/config` by default.

    Every later step in the job can then read it, including anything a
    dependency's build script decides to run. None of these jobs push through
    the checkout -- `docs.yml` publishes by `git init`-ing a fresh tree and
    pushing with an explicit token URL -- so none of them need it kept.

    This is zizmor's `artipacked` rule, which `qualification.yml` runs at
    `--pedantic` over every workflow.
    """
    offenders: list[str] = []
    for filename in _all_workflow_filenames():
        offenders += _checkouts_keeping_credentials(filename, _workflow(filename))
    assert not offenders, "checkout keeps the token in .git/config at: " + ", ".join(offenders)


def test_no_workflow_interpolates_a_secret_into_a_run_script():
    """`${{ secrets.* }}` inside `run:` is substituted before the shell starts.

    The expression is spliced into the generated script on disk, so the secret
    is written out in cleartext rather than only living in the step's
    environment. `docs.yml` did this with `GITHUB_TOKEN` in its push URL.
    """
    offenders: list[str] = []
    for filename in _all_workflow_filenames():
        offenders += _secrets_spliced_into_scripts(filename, _workflow(filename))
    assert not offenders, (
        "pass the secret through `env:` and reference it as a shell variable, at: "
        + ", ".join(offenders)
    )


# Contexts GitHub refuses to resolve in a job-level `env:` block. The list is
# the complement of what is documented as available there (`github`, `inputs`,
# `vars`, `needs`, `strategy`, `matrix`, `secrets`).
_CONTEXTS_UNAVAILABLE_AT_JOB_LEVEL = ("runner", "steps", "job", "env", "hashFiles")


def _job_env_using_a_step_only_context(name: str, workflow: dict) -> list[str]:
    offenders = []
    for job_name, job in workflow["jobs"].items():
        for key, value in (job.get("env") or {}).items():
            for context in _CONTEXTS_UNAVAILABLE_AT_JOB_LEVEL:
                if re.search(r"\$\{\{[^}]*\b" + context + r"\.", str(value)):
                    offenders.append(f"{name}:{job_name}:env.{key} reads {context}.*")
    return offenders


def test_no_job_level_env_reads_a_context_that_does_not_exist_there():
    """The same silent-empty-substitution family as the `needs` scoping bug.

    `runner.temp` is a *step* context. Used in a job-level `env:` GitHub does
    not error -- it substitutes the empty string. Five entries did this:

        ARTIFACT_DIR: ${{ runner.temp }}/gffbase-qualified-dist   -> /gffbase-qualified-dist
        WHEEL_DIR:    ${{ runner.temp }}/release-wheel            -> /release-wheel

    so `python -m build --outdir "$ARTIFACT_DIR"` tried to write to the
    filesystem root, and the smoke jobs inspected a directory the matching
    `download-artifact` step -- which *is* step-level, and did resolve -- had
    never written to. Both the qualification `package` job and every artifact
    smoke job were therefore incapable of passing.

    Resolved in a step that writes `$RUNNER_TEMP` into `$GITHUB_ENV`, which
    also removes the duplicated literal path that let the two drift.
    """
    offenders: list[str] = []
    for filename in _all_workflow_filenames():
        offenders += _job_env_using_a_step_only_context(filename, _workflow(filename))
    assert not offenders, "\n".join(offenders)


def test_the_three_workflow_security_detectors_actually_detect():
    """Red-green for the three guards above, without breaking a real workflow.

    A guard that scans real files and finds nothing is indistinguishable from
    a guard that cannot find anything -- which is exactly how six checks in
    `test_release_hygiene.py` sat dead for a release cycle. These synthetic
    jobs are the violations, so each detector is proven to fire.
    """
    unpinned = {
        "jobs": {
            "j": {"steps": [{"uses": "actions/checkout@v4"}, {"uses": "./local/action"}]},
            "k": {"uses": "org/reusable/.github/workflows/w.yml@main"},
        }
    }
    # The `./local/action` step is intentionally absent: a path-local action
    # is not a remote reference and has nothing to pin.
    assert sorted(_unpinned_actions("x.yml", unpinned)) == [
        "x.yml:j:0:actions/checkout@v4",
        "x.yml:k:org/reusable/.github/workflows/w.yml@main",
    ]

    bare = {"jobs": {"j": {"steps": [{"uses": "actions/checkout@" + "a" * 40}]}}}
    kept = {
        "jobs": {
            "j": {"steps": [{"uses": "actions/checkout@" + "a" * 40, "with": {"fetch-depth": "0"}}]}
        }
    }
    dropped = {
        "jobs": {
            "j": {
                "steps": [
                    {
                        "uses": "actions/checkout@" + "a" * 40,
                        "with": {"persist-credentials": "false"},
                    }
                ]
            }
        }
    }
    assert _checkouts_keeping_credentials("x.yml", bare) == ["x.yml:j:step 0"]
    assert _checkouts_keeping_credentials("x.yml", kept) == ["x.yml:j:step 0"]
    assert _checkouts_keeping_credentials("x.yml", dropped) == []

    spliced = {"jobs": {"j": {"steps": [{"run": "curl -H ${{ secrets.TOKEN }} https://x"}]}}}
    via_env = {
        "jobs": {"j": {"steps": [{"run": "curl -H $TOKEN https://x", "env": {"TOKEN": "s"}}]}}
    }
    assert _secrets_spliced_into_scripts("x.yml", spliced) == ["x.yml:j:step 0"]
    assert _secrets_spliced_into_scripts("x.yml", via_env) == []

    # `runner.*` in a job-level env is the bug; `inputs.*` and `matrix.*` in
    # the same place are legitimate and must not be reported.
    step_context = {"jobs": {"j": {"env": {"DIR": "${{ runner.temp }}/x"}}}}
    job_context = {"jobs": {"j": {"env": {"V": "${{ inputs.expected_version }}"}}}}
    assert _job_env_using_a_step_only_context("x.yml", step_context) == [
        "x.yml:j:env.DIR reads runner.*"
    ]
    assert _job_env_using_a_step_only_context("x.yml", job_context) == []


def _needs_of(job: dict) -> set[str]:
    needs = job.get("needs")
    if needs is None:
        return set()
    return {needs} if isinstance(needs, str) else set(needs)


@pytest.mark.parametrize("filename", _all_workflow_filenames())
def test_no_job_reads_a_needs_output_it_does_not_depend_on(filename: str) -> None:
    """`needs.<job>` resolves ONLY for direct dependencies.

    GitHub does not error on `needs.policy.outputs.version` in a job that does
    not declare `needs: policy` -- it silently substitutes the empty string.
    Both publishers did exactly that:

        artifacts:  needs: qualification   with: expected_version: needs.policy...
        publish:    needs: artifacts       if:   needs.policy.outputs.publish == 'true'

    So `expected_version` arrived empty and every `release_artifacts.py inspect`
    failed, and the publish `if` evaluated `'' == 'true'` -- meaning **publish
    was skipped on every run, including a valid tag push**. The publish path had
    never been exercised, so nothing noticed.

    Asserted as a property over every job rather than as a fixed graph: the
    shape can change, but a job may never read an output from a job it has not
    said it depends on.
    """
    workflow = _workflow(filename)
    reference = re.compile(r"needs\.([A-Za-z0-9_-]+)\.")
    offenders = []

    for name, job in workflow["jobs"].items():
        declared = _needs_of(job)
        # Everything a job can interpolate into: its condition, the inputs it
        # passes to a reusable workflow, its env, and its steps.
        scanned = [str(job.get("if", "")), str(job.get("with", "")), str(job.get("env", ""))]
        scanned += [str(step) for step in job.get("steps", [])]
        for blob in scanned:
            for referenced in reference.findall(blob):
                if referenced not in declared:
                    offenders.append(
                        f"{filename}: job {name!r} reads needs.{referenced} "
                        f"but declares needs={sorted(declared) or None}"
                    )

    assert not offenders, "\n".join(sorted(set(offenders)))
