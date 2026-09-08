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
"""Fail-closed release-channel policy shared by both publisher workflows.

The workflow trigger is only a convenience filter. This module is the runtime
authorization boundary: it distinguishes tags from version-looking branches,
requires the tag to match the source version exactly, and keeps manually
dispatched runs build-only unless the typed publish input is explicitly true.
"""

from __future__ import annotations

import argparse
import re
import sys
from dataclasses import dataclass
from datetime import date
from pathlib import Path

from packaging.version import InvalidVersion, Version

try:  # Python 3.10 support for running the release helper locally.
    import tomllib
except ModuleNotFoundError:  # pragma: no cover - exercised only on Python 3.10
    import tomli as tomllib  # type: ignore[no-redef]

_STABLE_TAG = re.compile(r"^v(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)$")
_RC_TAG = re.compile(r"^v(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)rc(0|[1-9][0-9]*)$")
_CHANNELS = frozenset({"production", "testpypi"})
_EVENTS = frozenset({"push", "workflow_dispatch"})


class ReleasePolicyError(ValueError):
    """The requested release route is not authorized by project policy."""


@dataclass(frozen=True, slots=True)
class ReleaseDecision:
    """Validated values exported to downstream qualification/build jobs."""

    version: str
    publish: bool
    reason: str


def parse_publish_requested(value: bool | str) -> bool:
    """Parse the GitHub typed-boolean input without truthy-string coercion."""
    if isinstance(value, bool):
        return value
    if value == "true":
        return True
    if value == "false":
        return False
    raise ReleasePolicyError("publish_requested must be the boolean true or false")


def _source_version(source_root: Path) -> str:
    path = source_root / "pyproject.toml"
    try:
        with path.open("rb") as stream:
            document = tomllib.load(stream)
        name = document["project"]["name"]
        value = document["project"]["version"]
    except (OSError, KeyError, TypeError, tomllib.TOMLDecodeError) as exc:
        raise ReleasePolicyError(f"cannot read project identity from {path}: {exc}") from exc
    if name != "gffbase" or not isinstance(value, str):
        raise ReleasePolicyError(
            "pyproject.toml must declare project name gffbase and a string version"
        )
    try:
        parsed = Version(value)
    except InvalidVersion as exc:
        raise ReleasePolicyError(f"source version {value!r} is not valid PEP 440") from exc
    if str(parsed) != value:
        raise ReleasePolicyError(
            f"source version {value!r} is not canonical PEP 440 (expected {str(parsed)!r})"
        )
    return value


def _read_text(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except OSError as exc:
        raise ReleasePolicyError(f"cannot read release metadata {path}: {exc}") from exc


def _cff_scalar(text: str, key: str) -> str | None:
    match = re.search(rf"^{re.escape(key)}:\s*([^#\n]+?)\s*$", text, re.MULTILINE)
    if match is None:
        return None
    return match.group(1).strip().strip("\"'")


def validate_release_metadata(source_root: Path, version: str) -> None:
    """Require candidate/stable changelog and citation state to agree."""
    try:
        parsed = Version(version)
    except InvalidVersion as exc:  # defensive: callers normally use _source_version first
        raise ReleasePolicyError(f"source version {version!r} is not valid PEP 440") from exc

    changelog = _read_text(source_root / "CHANGELOG.md")
    citation = _read_text(source_root / "CITATION.cff")
    heading = re.search(
        rf"^## \[{re.escape(version)}\] — (Unreleased|\d{{4}}-\d{{2}}-\d{{2}})\s*$",
        changelog,
        re.MULTILINE,
    )
    if heading is None:
        raise ReleasePolicyError(
            f"CHANGELOG.md must contain an exact release-state heading for {version}"
        )
    if re.search(rf"^\[{re.escape(version)}\]:\s*\S+", changelog, re.MULTILINE) is None:
        raise ReleasePolicyError(f"CHANGELOG.md must define the [{version}] reference link")

    cited_version = _cff_scalar(citation, "version")
    if cited_version != version:
        raise ReleasePolicyError(
            f"CITATION.cff version {cited_version!r} does not match source version {version!r}"
        )
    cited_date = _cff_scalar(citation, "date-released")

    if parsed.is_prerelease:
        if heading.group(1) != "Unreleased":
            raise ReleasePolicyError("a prerelease CHANGELOG heading must be Unreleased")
        if cited_date is not None:
            raise ReleasePolicyError("an Unreleased prerelease must not declare date-released")
        if "unreleased" not in citation.lower():
            raise ReleasePolicyError("CITATION.cff must identify a prerelease as unreleased")
        return

    if heading.group(1) == "Unreleased":
        raise ReleasePolicyError("a stable version cannot remain Unreleased")
    if cited_date is None:
        raise ReleasePolicyError("a stable CITATION.cff must declare date-released")
    if cited_date != heading.group(1):
        raise ReleasePolicyError(
            f"CHANGELOG.md and CITATION.cff release dates disagree: "
            f"{heading.group(1)!r} != {cited_date!r}"
        )
    try:
        released = date.fromisoformat(cited_date)
    except ValueError as exc:
        raise ReleasePolicyError(f"date-released is not an ISO date: {cited_date!r}") from exc
    if released > date.today():
        raise ReleasePolicyError(f"stable release date {cited_date} is in the future")


def _validate_publishing_tag(*, channel: str, ref_type: str, ref_name: str, version: str) -> None:
    if ref_type != "tag":
        raise ReleasePolicyError(f"publication requires a tag ref; received ref_type={ref_type!r}")

    pattern = _STABLE_TAG if channel == "production" else _RC_TAG
    description = "vMAJOR.MINOR.PATCH" if channel == "production" else "vMAJOR.MINOR.PATCHrcN"
    if pattern.fullmatch(ref_name) is None:
        raise ReleasePolicyError(
            f"{channel} publication requires the exact tag grammar {description}; "
            f"received {ref_name!r}"
        )
    expected = f"v{version}"
    if ref_name != expected:
        raise ReleasePolicyError(
            f"release tag {ref_name!r} does not match source version {version!r} "
            f"(expected {expected!r})"
        )

    parsed = Version(version)
    if channel == "production" and parsed.is_prerelease:
        raise ReleasePolicyError("production publication refuses prerelease source versions")
    if channel == "testpypi" and (
        not parsed.is_prerelease or parsed.pre is None or parsed.pre[0] != "rc"
    ):
        raise ReleasePolicyError("TestPyPI publication requires a release-candidate source version")


def evaluate_release(
    *,
    channel: str,
    event_name: str,
    ref_type: str,
    ref_name: str,
    publish_requested: bool | str,
    source_root: Path,
) -> ReleaseDecision:
    """Authorize one workflow invocation and return immutable downstream inputs."""
    if channel not in _CHANNELS:
        raise ReleasePolicyError(f"unknown release channel {channel!r}")
    if event_name not in _EVENTS:
        raise ReleasePolicyError(f"unsupported release event {event_name!r}")

    requested = parse_publish_requested(publish_requested)
    version = _source_version(source_root)
    validate_release_metadata(source_root, version)

    if event_name == "workflow_dispatch" and not requested:
        return ReleaseDecision(
            version=version,
            publish=False,
            reason="manual build-only verification",
        )

    _validate_publishing_tag(
        channel=channel,
        ref_type=ref_type,
        ref_name=ref_name,
        version=version,
    )
    reason = "validated tag push" if event_name == "push" else "validated manual tag publication"
    return ReleaseDecision(version=version, publish=True, reason=reason)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--channel", choices=sorted(_CHANNELS), required=True)
    parser.add_argument("--event-name", required=True)
    parser.add_argument("--ref-type", required=True)
    parser.add_argument("--ref-name", required=True)
    parser.add_argument("--publish-requested", default="false")
    parser.add_argument("--source-root", type=Path, default=Path.cwd())
    parser.add_argument("--github-output", type=Path)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        decision = evaluate_release(
            channel=args.channel,
            event_name=args.event_name,
            ref_type=args.ref_type,
            ref_name=args.ref_name,
            publish_requested=args.publish_requested,
            source_root=args.source_root.resolve(),
        )
    except ReleasePolicyError as exc:
        print(f"release policy rejected this run: {exc}", file=sys.stderr)
        return 2

    lines = (
        f"version={decision.version}\n"
        f"publish={'true' if decision.publish else 'false'}\n"
        f"reason={decision.reason}\n"
    )
    if args.github_output is not None:
        with args.github_output.open("a", encoding="utf-8", newline="\n") as stream:
            stream.write(lines)
    print(lines, end="")
    return 0


if __name__ == "__main__":  # pragma: no cover - exercised by workflow invocation
    raise SystemExit(main())
