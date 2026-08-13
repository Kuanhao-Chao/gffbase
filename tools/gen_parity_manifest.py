#!/usr/bin/env python
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
"""Generate the gffutils compatibility manifest.

Introspects an installed ``gffutils`` and writes a machine-readable inventory
of its public surface -- every module, symbol, callable signature, parameter
default, and CLI subcommand -- to ``tests/parity/gffutils_manifest.json``.

``tests/parity/test_api_parity.py`` reads that file and asserts gffbase
provides the same surface. The manifest is committed so the parity suite runs
without gffutils installed; regenerate it only when deliberately re-pinning the
oracle.

Usage::

    python tools/gen_parity_manifest.py                     # write the manifest
    python tools/gen_parity_manifest.py --check             # fail if it drifted
    python tools/gen_parity_manifest.py -o /tmp/other.json

The oracle is identified by repository commit, never by
``gffutils.__version__``: ``gffutils/version.py`` consults installed
distribution metadata first, so a source checkout happily reports the version
of some unrelated installed copy.
"""

from __future__ import annotations

import argparse
import inspect
import json
import re
import subprocess
import sys
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_OUTPUT = REPO_ROOT / "tests" / "parity" / "gffutils_manifest.json"

# Every module a downstream user could reasonably import. `gffutils.__all__`
# covers only eight names, but the documented API (doc/source/api.rst) and real
# downstream code reach into helpers, bins, inspect, convert, the integrations
# and contrib.plotting, so parity has to account for all of them.
MODULES = [
    "gffutils",
    "gffutils.attributes",
    "gffutils.bins",
    "gffutils.constants",
    "gffutils.convert",
    "gffutils.create",
    "gffutils.exceptions",
    "gffutils.feature",
    "gffutils.gffwriter",
    "gffutils.helpers",
    "gffutils.inspect",
    "gffutils.interface",
    "gffutils.iterators",
    "gffutils.merge_criteria",
    "gffutils.parser",
    "gffutils.version",
    # Optional integrations: recorded when importable, flagged when not, so a
    # missing pybedtools does not silently shrink the manifest.
    "gffutils.biopython_integration",
    "gffutils.pybedtools_integration",
    "gffutils.contrib.plotting",
]

# Underscore-prefixed names that are nonetheless part of the effective contract:
# they are referenced by the upstream docstrings, exercised by the upstream test
# suite, or constructed directly by upstream code paths a successor must keep.
SEMI_PUBLIC = {
    "gffutils.create": ["_DBCreator", "_GFFDBCreator", "_GTFDBCreator"],
    "gffutils.parser": ["_split_keyvals", "_reconstruct"],
    "gffutils.constants": [
        "_keys",
        "_gffkeys",
        "_gffkeys_extra",
        "_iterator_kwargs",
    ],
    "gffutils.iterators": [
        "_BaseIterator",
        "_FileIterator",
        "_UrlIterator",
        "_FeatureIterator",
    ],
    "gffutils.feature": ["_position_lookup"],
}


#: Matches the `0x7f...` address CPython bakes into the default `repr()` of an
#: object without one of its own. Sentinel defaults (`_marker = object()`) hit
#: this, and the address changes every process, so leaving it in would make the
#: manifest irreproducible and `--check` useless.
_ADDRESS_RE = re.compile(r" at 0x[0-9a-fA-F]+")


def _stable_repr(value: Any) -> str:
    return _ADDRESS_RE.sub(" at 0xSENTINEL", repr(value))


def _describe_signature(obj: Any) -> dict[str, Any] | None:
    """Render a callable's signature as plain data.

    Defaults are stringified: many are sentinels or mutable module-level
    objects (`constants.default_pragmas`) that do not survive a JSON round
    trip, and for parity purposes their *identity* matters less than the fact
    that a default exists and what it looks like.
    """
    try:
        sig = inspect.signature(obj)
    except (TypeError, ValueError):
        return None

    params = []
    for name, p in sig.parameters.items():
        params.append(
            {
                "name": name,
                "kind": p.kind.name,
                "has_default": p.default is not inspect.Parameter.empty,
                "default": (
                    None if p.default is inspect.Parameter.empty else _stable_repr(p.default)
                ),
            }
        )
    return {"parameters": params}


def _describe_class(cls: type) -> dict[str, Any]:
    members: dict[str, Any] = {}
    for name, member in inspect.getmembers(cls):
        if name.startswith("_") and not (name.startswith("__") and name.endswith("__")):
            continue
        # Dunders matter for parity (`__len__`, `__getitem__`, `__eq__`, ...),
        # but only the ones this class actually defines rather than the ones
        # every Python object inherits from `object`.
        if name.startswith("__") and name not in vars(cls):
            continue
        if inspect.isroutine(member):
            members[name] = {"kind": "method", "signature": _describe_signature(member)}
        elif isinstance(member, property):
            members[name] = {"kind": "property"}
        elif not inspect.isclass(member):
            members[name] = {"kind": "attribute"}
    return {
        "kind": "class",
        "bases": [b.__name__ for b in cls.__bases__],
        "signature": _describe_signature(cls),
        "members": members,
    }


def _describe_module(mod_name: str) -> dict[str, Any]:
    try:
        mod = __import__(mod_name, fromlist=["__name__"])
    except Exception as exc:
        return {"importable": False, "import_error": f"{type(exc).__name__}: {exc}"}

    wanted = set(SEMI_PUBLIC.get(mod_name, ()))
    symbols: dict[str, Any] = {}

    for name, obj in vars(mod).items():
        if name.startswith("_") and name not in wanted:
            continue
        # Skip re-exported modules; each is inventoried in its own right.
        if inspect.ismodule(obj):
            continue
        # Skip symbols merely imported from elsewhere -- they belong to the
        # module that defines them. `gffutils.__init__` is the exception: its
        # re-exports *are* its public surface.
        origin = getattr(obj, "__module__", None)
        if origin and origin != mod_name and mod_name != "gffutils":
            continue

        if inspect.isclass(obj):
            symbols[name] = _describe_class(obj)
        elif inspect.isroutine(obj):
            symbols[name] = {"kind": "function", "signature": _describe_signature(obj)}
        else:
            symbols[name] = {"kind": "constant", "type": type(obj).__name__}

    return {
        "importable": True,
        "all": sorted(getattr(mod, "__all__", []) or []),
        "symbols": dict(sorted(symbols.items())),
    }


def _describe_cli() -> dict[str, Any]:
    """Inventory the ``gffutils-cli`` subcommands by parsing the script.

    The script is not importable (no ``.py`` extension, and it calls
    ``argh.dispatch_commands`` at import time), so this reads the source.
    """
    import gffutils

    script = Path(gffutils.__file__).parent / "scripts" / "gffutils-cli"
    if not script.is_file():
        return {"found": False}

    src = script.read_text(encoding="utf-8")
    dispatched = re.search(r"dispatch_commands\(\s*\[(.*?)\]", src, re.DOTALL)
    registered = (
        [n.strip() for n in dispatched.group(1).replace("\n", "").split(",") if n.strip()]
        if dispatched
        else []
    )

    commands: dict[str, Any] = {}
    for m in re.finditer(r"^def (\w+)\((.*?)\):", src, re.MULTILINE | re.DOTALL):
        name, arglist = m.group(1), " ".join(m.group(2).split())
        body_start = m.end()
        body = src[body_start : body_start + 400]
        commands[name] = {
            "args": arglist,
            "registered": name in registered,
            # Several upstream subcommands are registered stubs. A successor
            # should implement the documented contract rather than reproduce
            # the defect, so the manifest records which ones they are.
            "is_stub": "NotImplementedError" in body,
        }

    return {"found": True, "registered": registered, "commands": dict(sorted(commands.items()))}


def _oracle_identity() -> dict[str, Any]:
    import gffutils

    pkg_dir = Path(gffutils.__file__).resolve().parent
    repo = pkg_dir.parent

    def _git(*args: str) -> str | None:
        try:
            out = subprocess.run(
                ["git", "-C", str(repo), *args],
                capture_output=True,
                text=True,
                timeout=30,
                check=False,
            )
            return out.stdout.strip() or None
        except (OSError, subprocess.SubprocessError):
            return None

    return {
        "package_dir": str(pkg_dir),
        "commit": _git("rev-parse", "HEAD"),
        "describe": _git("describe", "--tags"),
        # Recorded for information only. `gffutils.version` reads installed
        # distribution metadata first, so from a source checkout this can be
        # the version of an entirely different installed copy.
        "reported_version": getattr(gffutils, "__version__", None),
        "generator_python": sys.version.split()[0],
    }


def build_manifest() -> dict[str, Any]:
    return {
        "schema": 1,
        "oracle": _oracle_identity(),
        "modules": {name: _describe_module(name) for name in MODULES},
        "cli": _describe_cli(),
    }


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("-o", "--output", type=Path, default=DEFAULT_OUTPUT)
    ap.add_argument(
        "--check",
        action="store_true",
        help="exit non-zero if the committed manifest differs from a fresh one",
    )
    args = ap.parse_args()

    manifest = build_manifest()
    rendered = json.dumps(manifest, indent=2, sort_keys=True) + "\n"

    if args.check:
        if not args.output.is_file():
            print(f"{args.output} does not exist", file=sys.stderr)
            return 1
        current = args.output.read_text(encoding="utf-8")
        # The oracle block records absolute paths and the generating
        # interpreter, neither of which is a property of the API surface.
        if json.loads(current).get("modules") != manifest["modules"]:
            print("parity manifest is out of date; re-run without --check", file=sys.stderr)
            return 1
        print("parity manifest is up to date")
        return 0

    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(rendered, encoding="utf-8")

    n_mod = sum(1 for m in manifest["modules"].values() if m.get("importable"))
    n_sym = sum(len(m.get("symbols", {})) for m in manifest["modules"].values())
    print(f"wrote {args.output}")
    print(f"  {n_mod}/{len(manifest['modules'])} modules importable, {n_sym} symbols")
    print(f"  oracle commit: {manifest['oracle']['commit']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
