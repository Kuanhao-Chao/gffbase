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
"""Execute the Python snippets in the documentation.

Documentation code rots silently. `docs/cookbooks/index.md` claimed every
snippet had been validated while its own opening example opened two `FeatureDB`
handles and closed neither -- which, once the connection took an exclusive
DuckDB lock, taught readers a leak. Nothing caught it because nothing ran it.

So: every fenced ``python`` block in the docs is executed here, against the
small vendored fixtures in `tests/data/`.

Notebook semantics
------------------
A documentation page reads top to bottom, and its examples build on each
other: `usage_gallery.md` defines ``gene`` in one block and uses it in the
next five. So blocks in a file share ONE accumulating namespace, exactly as a
reader stepping through the page would experience them. A failure in an early
block therefore fails the later blocks that depend on it -- which is correct,
because a reader following the page would be stuck at the same point.

Opting OUT is explicit
----------------------
A block runs unless it is marked, which is the important half of the design. If
skipping were the default, a newly-added broken snippet would be silently
ignored; this way it fails until someone either fixes it or says in writing why
it cannot run.

Mark a block with an HTML comment on the line(s) before its fence::

    <!-- docs-test: skip reason="needs the multi-GB GENCODE corpus" -->
    ```python
    db = create_db("gencode.v49.annotation.gtf.gz", "gencode.duckdb")
    ```

Directives:

``skip reason="..."``
    Do not execute, and do not contribute names to the page's namespace. A
    reason is mandatory -- `test_every_skip_states_a_reason` fails without
    one, so the exemption list cannot quietly grow.

``isolated``
    Execute in a fresh namespace instead of the page's, for a block that
    deliberately redefines something.

``pandas``
    Exclude the block from the minimum-dependency documentation run and execute
    it in the dedicated ``pandas_docs`` gate, where ``gffbase[pandas]`` is an
    explicit dependency. A missing pandas install is a failure in that gate.

Each page starts from the fixtures in `_namespace()`: the public imports and a
`db` built from `tests/data/hierarchy.gff3`, so a page does not have to open
with boilerplate a reader would not need.
"""

from __future__ import annotations

import re
import textwrap
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = Path(__file__).parent / "data"
pytestmark = pytest.mark.filterwarnings("error::UserWarning")

# A missing import is exempt only when the snippet directly imports one of
# these deliberately external packages. Internal typos and missing transitive
# dependencies must fail: both previously disappeared behind the blanket
# ``except ModuleNotFoundError: skip`` below.
OPTIONAL_TOP_LEVEL_IMPORTS = frozenset({"gffutils", "torch"})

#: Files whose Python blocks are executed. Everything a user is likely to
#: copy from, which includes the two root documents rendered on GitHub and PyPI.
DOC_ROOTS = ["docs/source", "README.md", "MIGRATION.md"]

#: Markdown fences. Still needed: the two root documents stay Markdown because
#: GitHub and PyPI render them, even though the site itself is now RST.
_FENCE = re.compile(
    r"(?P<directives>(?:^[ \t]*<!--[ \t]*docs-test:[^\n]*-->[ \t]*\n)*)"
    r"^```(?P<lang>python|text)\n(?P<body>.*?)^```",
    re.M | re.S,
)
_DIRECTIVE = re.compile(r"<!--\s*docs-test:\s*(?P<verb>\w+)(?P<rest>[^>]*?)-->")

#: The RST equivalent. A `code-block` body is an indented block, so it runs
#: until the first non-blank line that is not indented; the body is dedented
#: before execution. Directives are RST comments carrying the same text the
#: HTML comments did, so the vocabulary did not change with the markup.
_RST_BLOCK = re.compile(
    r"(?P<directives>(?:^[ \t]*\.\.[ \t]+docs-test:[^\n]*\n[ \t]*\n?)*)"
    r"^\.\.[ \t]+code-block::[ \t]*(?P<lang>python|text)[ \t]*\n"
    r"[ \t]*\n"
    r"(?P<body>(?:(?:[ \t]+[^\n]*)?\n)+)",
    re.M,
)
_RST_DIRECTIVE = re.compile(r"\.\.\s+docs-test:\s*(?P<verb>\w+)(?P<rest>[^\n]*)")
_REASON = re.compile(r'reason\s*=\s*"(?P<reason>[^"]*)"')


class Snippet:
    """One fenced ``python`` block, with its file, line and directives."""

    __slots__ = ("path", "line", "code", "verb", "reason", "lang", "expected_output")

    def __init__(
        self,
        path: Path,
        line: int,
        code: str,
        verb: str,
        reason: str,
        lang: str = "python",
    ):
        self.path = path
        self.line = line
        self.code = code
        self.verb = verb
        self.reason = reason
        self.lang = lang
        #: A ```text block immediately following this one, when it is labelled
        #: as this snippet's console output. Verified, not decorative.
        self.expected_output: str | None = None

    @property
    def id(self) -> str:
        return f"{self.path.relative_to(REPO_ROOT)}:{self.line}"

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"<Snippet {self.id} verb={self.verb!r}>"


#: Pages under `docs/source/content/` that are generated mirrors of a root
#: Markdown file. Both copies carry the same snippets, so parsing both would
#: execute every one of them twice -- and report a skip count that had silently
#: doubled. The root document is the canonical source: GitHub and PyPI render
#: it, and it is what a contributor edits.
_MIRRORED = {"migration", "changelog", "contributing", "security"}


def _doc_files() -> list[Path]:
    files: list[Path] = []
    for root in DOC_ROOTS:
        path = REPO_ROOT / root
        if path.is_dir():
            files.extend(sorted(p for p in path.rglob("*.rst") if p.stem not in _MIRRORED))
            files.extend(sorted(path.rglob("*.md")))
        elif path.is_file():
            files.append(path)
    return files


def _parse(path: Path) -> list[Snippet]:
    text = path.read_text(encoding="utf-8")
    rst = path.suffix == ".rst"
    pattern = _RST_BLOCK if rst else _FENCE
    directive_pattern = _RST_DIRECTIVE if rst else _DIRECTIVE
    out: list[Snippet] = []
    for match in pattern.finditer(text):
        verb, reason = "run", ""
        for directive in directive_pattern.finditer(match.group("directives") or ""):
            verb = directive.group("verb")
            found = _REASON.search(directive.group("rest") or "")
            reason = found.group("reason") if found else ""
        line = text.count("\n", 0, match.start("body")) + 1
        body = match.group("body")
        if rst:
            # The block is indented under its directive; run what the reader
            # would copy, not the indentation the markup needed.
            body = textwrap.dedent(body).strip("\n") + "\n"
        out.append(Snippet(path, line, body, verb, reason, match.group("lang")))

    # Pair `console output` text blocks with the python block above them.
    # Writing the output by hand is exactly as rot-prone as the benchmark
    # tables were -- the first draft of `docs/gallery.md` already had one
    # transcribed wrong -- so a block that claims to be output gets checked
    # against the output.
    paired: list[Snippet] = []
    for snippet in out:
        if (
            snippet.lang == "text"
            and snippet.verb == "skip"
            and "console output" in snippet.reason
            and paired
            and paired[-1].lang == "python"
        ):
            paired[-1].expected_output = snippet.code
            continue
        paired.append(snippet)
    return paired


def _all_snippets() -> list[Snippet]:
    return [s for path in _doc_files() for s in _parse(path)]


ALL_SNIPPETS = _all_snippets()
RUNNABLE = [s for s in ALL_SNIPPETS if s.lang == "python" and s.verb in {"run", "isolated"}]
PANDAS_SNIPPETS = [s for s in ALL_SNIPPETS if s.lang == "python" and s.verb == "pandas"]


# ---------------------------------------------------------------------------
# Fixtures the snippets are executed against
# ---------------------------------------------------------------------------


def _namespace(tmp_path: Path) -> dict:
    """The environment a documentation snippet starts in.

    Deliberately small: a snippet that needs more than this should say so in
    its own body, because the reader has no fixtures. What is pre-bound is
    only what a reader would already have in scope after following the page
    from the top -- the imports, and a database to query.
    """
    import gffbase
    from gffbase import (  # noqa: F401 - bound for snippet use
        DataIterator,
        Feature,
        FeatureDB,
        GFFWriter,
        create_db,
    )

    # A real database over the vendored hierarchy fixture: gene g1 -> mRNA
    # t1/t2 -> exons and CDSs. Small enough to build per test, real enough
    # that queries return rows.
    db_path = tmp_path / "docs.duckdb"
    db = create_db(str(DATA_DIR / "hierarchy.gff3"), str(db_path), force=True)

    return {
        "__name__": "__docs__",
        "gffbase": gffbase,
        "create_db": create_db,
        "FeatureDB": FeatureDB,
        "Feature": Feature,
        "DataIterator": DataIterator,
        "GFFWriter": GFFWriter,
        "db": db,
        # Paths a snippet can use instead of inventing a filename.
        "GFF3_PATH": str(DATA_DIR / "simple.gff3"),
        "GTF_PATH": str(DATA_DIR / "simple.gtf"),
        "HIERARCHY_PATH": str(DATA_DIR / "hierarchy.gff3"),
        "DB_PATH": str(db_path),
        "TMP": tmp_path,
    }


def _close_quietly(namespace: dict) -> None:
    for value in list(namespace.values()):
        close = getattr(value, "close", None)
        if close is not None and value.__class__.__name__ in {"FeatureDB", "GFFWriter"}:
            try:
                close()
            except Exception:  # pragma: no cover - teardown must not mask a failure
                pass


def _is_allowed_missing_import(snippet: Snippet, missing: str | None) -> bool:
    """Whether *missing* is a direct, explicitly allowlisted snippet import."""
    if missing not in OPTIONAL_TOP_LEVEL_IMPORTS:
        return False
    direct = re.compile(
        rf"^\s*(?:import\s+{re.escape(missing)}(?:\s|$)|"
        rf"from\s+{re.escape(missing)}(?:\.|\s))",
        re.MULTILINE,
    )
    return direct.search(snippet.code) is not None


# One accumulating namespace AND one working directory per document, built
# lazily. The directory matters as much as the namespace: `quickstart.md`
# writes `demo.gff3` in its first block and opens it in the second, which only
# works if both run in the same place -- exactly as they would for a reader.
_PAGE_STATE: dict[Path, dict] = {}
_PAGE_CWD: dict[Path, Path] = {}


# ---------------------------------------------------------------------------
# The tests
# ---------------------------------------------------------------------------


def _run_snippet(snippet: Snippet, tmp_path, tmp_path_factory, monkeypatch):
    """A documented example must actually work.

    Failure here means the documentation tells a user to do something that
    raises. Fix the documentation, or mark the block with a reason if it
    genuinely cannot run against a 400-byte fixture.
    """
    if snippet.verb == "isolated":
        monkeypatch.chdir(tmp_path)
        namespace = _namespace(tmp_path)
    else:
        cwd = _PAGE_CWD.get(snippet.path)
        if cwd is None or not cwd.is_dir():
            # `tmp_path_factory`, NOT `tmp_path`: this project sets
            # `tmp_path_retention_policy = "failed"`, so a passing test's
            # `tmp_path` is deleted the moment it finishes -- and the next
            # block on the page would then chdir into a directory that no
            # longer holds the file the previous block wrote.
            cwd = tmp_path_factory.mktemp("docs-" + snippet.path.stem)
            _PAGE_CWD[snippet.path] = cwd
        monkeypatch.chdir(cwd)
        namespace = _PAGE_STATE.get(snippet.path)
        if namespace is None:
            namespace = _namespace(cwd)
            _PAGE_STATE[snippet.path] = namespace

    import contextlib
    import io

    captured = io.StringIO()
    try:
        with contextlib.redirect_stdout(captured):
            exec(compile(snippet.code, snippet.id, "exec"), namespace)  # noqa: S102
    except ModuleNotFoundError as exc:
        # Skipped rather than passed, so a developer with the optional package
        # still runs it and ``-rs`` reports the exact unchecked example.
        if not _is_allowed_missing_import(snippet, exc.name):
            raise
        pytest.skip(f"{snippet.id} needs the optional package {exc.name!r}")
    except Exception as exc:
        raise AssertionError(
            f"documentation snippet at {snippet.id} raised "
            f"{type(exc).__name__}: {exc}\n\n{snippet.code}"
        ) from exc
    finally:
        if snippet.verb == "isolated":
            _close_quietly(namespace)

    if snippet.expected_output is not None:
        actual = captured.getvalue().strip()
        expected = snippet.expected_output.strip()
        assert actual == expected, (
            f"the console output shown under {snippet.id} is not what the "
            f"snippet prints.\n\nshown in the docs:\n{expected}\n\n"
            f"actually printed:\n{actual}"
        )


@pytest.mark.parametrize("snippet", RUNNABLE, ids=lambda s: s.id)
def test_documentation_snippet_runs(snippet: Snippet, tmp_path, tmp_path_factory, monkeypatch):
    """The minimum-dependency documentation examples execute without pandas."""
    _run_snippet(snippet, tmp_path, tmp_path_factory, monkeypatch)


@pytest.mark.pandas_docs
@pytest.mark.parametrize("snippet", PANDAS_SNIPPETS, ids=lambda s: s.id)
def test_pandas_documentation_snippet_runs(snippet, tmp_path, tmp_path_factory, monkeypatch):
    """Pandas-specific examples run only in the environment that promises pandas."""
    _run_snippet(snippet, tmp_path, tmp_path_factory, monkeypatch)


def test_every_skip_states_a_reason():
    """An exemption has to say why, so the list cannot quietly grow.

    A bare `skip` is indistinguishable from "this was broken and someone made
    the failure go away", which is the failure mode this whole module exists
    to prevent.
    """
    bare = [s.id for s in ALL_SNIPPETS if s.verb == "skip" and not s.reason.strip()]
    assert not bare, (
        "documentation snippets skipped without a reason: "
        + ", ".join(bare)
        + '\nUse: <!-- docs-test: skip reason="..." -->'
    )


def test_every_directive_is_recognised():
    """Catch a typo'd directive, which would otherwise silently mean `run`."""
    known = {"run", "skip", "isolated", "pandas"}
    unknown = [(s.id, s.verb) for s in ALL_SNIPPETS if s.verb not in known]
    assert not unknown, f"unrecognised docs-test directives: {unknown}"


def test_the_docs_actually_contain_executable_snippets():
    """Guard against the extractor silently matching nothing.

    If a future docs restructure moved every example into a tabbed block or a
    snippet include, this module would pass by testing zero snippets -- which
    looks identical to passing by testing everything.
    """
    assert len(RUNNABLE) >= 40, (
        f"only {len(RUNNABLE)} runnable snippets found across {len(_doc_files())} "
        "documents; the extractor is probably not matching the fences any more"
    )


def test_generic_snippets_do_not_require_pandas():
    offenders = [snippet.id for snippet in RUNNABLE if 'format="df"' in snippet.code]
    assert not offenders, f"generic docs snippets request pandas: {offenders}"


def test_pandas_examples_are_explicit_and_exercised():
    assert PANDAS_SNIPPETS, "no explicit pandas documentation snippets were found"
    assert all('format="df"' in snippet.code for snippet in PANDAS_SNIPPETS)


def _synthetic_snippet(code: str) -> Snippet:
    return Snippet(REPO_ROOT / "docs" / "synthetic-import-test.md", 1, code, "isolated", "")


def test_allowlisted_direct_optional_import_is_the_only_missing_import_skip(
    tmp_path, tmp_path_factory, monkeypatch
):
    snippet = _synthetic_snippet("import torch\n")
    real_import = __import__

    def missing_torch(name, *args, **kwargs):
        if name == "torch":
            raise ModuleNotFoundError("No module named 'torch'", name="torch")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr("builtins.__import__", missing_torch)
    with pytest.raises(pytest.skip.Exception, match="optional package 'torch'"):
        _run_snippet(snippet, tmp_path, tmp_path_factory, monkeypatch)


@pytest.mark.parametrize(
    "code",
    [
        "import gffbase.nonexistent\n",
        "import not_an_allowlisted_package\n",
        "import torch\nraise ModuleNotFoundError('broken torch dependency', name='torch._broken')\n",
    ],
)
def test_internal_unlisted_and_transitive_import_failures_are_not_skipped(
    code, tmp_path, tmp_path_factory, monkeypatch
):
    snippet = _synthetic_snippet(code)
    with pytest.raises(ModuleNotFoundError):
        _run_snippet(snippet, tmp_path, tmp_path_factory, monkeypatch)


#: How many snippets are currently exempt. A RATCHET, not a target: lower it
#: when you make a snippet runnable, never raise it to make a failure go away.
#:
#: Most of the current exemptions are placeholder filenames and accession ids
#: -- `create_db("annotation.gff3", ...)` -- which a reader substitutes. Many
#: could be turned into self-contained examples that build their own input,
#: the way `docs/guides/modes.md` and `docs/getting-started/quickstart.md` now
#: do. That is a documentation improvement waiting to happen, and this number
#: is the scoreboard for it.
MAX_SKIPPED = 62


def test_the_skip_list_does_not_grow():
    """Exemptions may be removed, never added.

    Without a ratchet the easiest way to fix a failing snippet is to skip it,
    and the executable-documentation guarantee erodes one commit at a time.
    """
    # Python blocks only. A ```text block is never runnable Python -- it is
    # console output or a file listing -- so counting one would inflate the
    # scoreboard without anything having been exempted.
    skipped = [s for s in ALL_SNIPPETS if s.lang == "python" and s.verb == "skip"]
    assert len(skipped) <= MAX_SKIPPED, (
        f"{len(skipped)} snippets are skipped, up from {MAX_SKIPPED}. "
        "Make the new one runnable, or justify raising MAX_SKIPPED in the "
        "commit message."
    )
    if len(skipped) < MAX_SKIPPED:
        pytest.fail(
            f"good news: only {len(skipped)} snippets are skipped now, "
            f"down from {MAX_SKIPPED}. Lower MAX_SKIPPED to {len(skipped)} "
            "to lock the improvement in."
        )
