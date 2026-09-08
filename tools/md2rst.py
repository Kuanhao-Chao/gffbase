"""MkDocs Markdown -> Sphinx reStructuredText, in the house style.

pandoc does the bulk of the markup faithfully. What it cannot do is the
MkDocs-specific constructs, so those are rewritten before and after:

  * `!!! note "Title"` admonitions become fenced divs, which pandoc turns
    straight into `.. note::` with the body's inline markup fully converted.
    Doing it the other way round -- letting pandoc see the raw `!!!` -- flattens
    the whole block into one paragraph and silently loses the structure.
  * Markdown tables become `list-table` directives, emitted as raw RST so
    pandoc leaves them alone. pandoc's own grid tables are valid RST but the
    house style is list-table with explicit `:widths:`.
  * `<!-- docs-test: ... -->` markers are load-bearing (a test harness reads
    them) and pandoc drops HTML comments, so they travel as a sentinel.
  * Links to sibling `.md` pages become `:doc:` roles against the flat page
    set under `content/`.
"""

from __future__ import annotations

import html
import re
import subprocess
import sys
from pathlib import Path

# Every page stem that exists under content/. A link to anything else cannot
# become a :doc: role -- inventing one would fail the -W build.
STEMS = {
    "installation", "quickstart", "modes", "usage_gallery", "gallery", "cli",
    "connections", "migration", "cookbooks", "cookbook_gencode_ensembl",
    "cookbook_refseq", "cookbook_mane", "cookbook_ml_workflows", "performance",
    "methodology", "datasets", "architecture", "schema_v2", "api", "faq",
    "troubleshooting", "citation", "license", "contact", "testing", "roadmap",
    "release_checklist", "changelog", "contributing", "security",
}
# Source path (without .md) -> page stem, for links that use the old layout.
PATH_TO_STEM = {
    "getting-started/installation": "installation",
    "getting-started/quickstart": "quickstart",
    "guides/modes": "modes",
    "guides/connections": "connections",
    "guides/troubleshooting": "troubleshooting",
    "cookbooks/index": "cookbooks",
    "cookbooks/gencode_ensembl": "cookbook_gencode_ensembl",
    "cookbooks/refseq": "cookbook_refseq",
    "cookbooks/mane": "cookbook_mane",
    "cookbooks/machine_learning_workflows": "cookbook_ml_workflows",
    "performance/methodology": "methodology",
    "design/architecture": "architecture",
    "design/schema-v2": "schema_v2",
    "security-policy": "security",
    "release-checklist": "release_checklist",
    "usage_gallery": "usage_gallery",
    "index": "index",
    # The eleven mkdocstrings pages are one autodoc page now.
    "api/index": "api", "api/featuredb": "api", "api/feature": "api",
    "api/create_db": "api", "api/io": "api", "api/merge_criteria": "api",
    "api/exceptions": "api", "api/multipart": "api", "api/validate": "api",
    "api/migrate": "api", "api/compat": "api",
    # Sibling references inside a subdirectory use the bare filename.
    "machine_learning_workflows": "cookbook_ml_workflows",
    "gencode_ensembl": "cookbook_gencode_ensembl",
    "refseq": "cookbook_refseq",
    "mane": "cookbook_mane",
    "schema-v2": "schema_v2",
    "troubleshooting": "troubleshooting",
    "modes": "modes",
    "connections": "connections",
}
GITHUB = "https://github.com/Kuanhao-Chao/gffbase"

SENTINEL = "XDOCSTESTX"


def slugify(title: str) -> str:
    """Reproduce the anchor MkDocs generates for a heading.

    python-markdown's toc extension lowercases, drops everything that is not
    alphanumeric/space/hyphen, then maps spaces to hyphens. `## 6.7
    Discontinuous (multipart) features` becomes
    `67-discontinuous-multipart-features`, which is what the in-page links in
    the source actually target.
    """
    t = re.sub(r"`+", "", title).strip()
    t = re.sub(r"[^\w\s-]", "", t, flags=re.UNICODE).strip().lower()
    return re.sub(r"[\s]+", "-", t)


def add_heading_targets(text: str, stem: str) -> tuple[str, dict[str, str]]:
    """Emit an explicit RST target above every heading.

    Sphinx has no equivalent of MkDocs' implicit per-heading anchors, so an
    in-page link like `[text](#1-database-initialization)` has nothing to
    resolve against. Referencing by title text instead does not work either --
    these headings are numbered and the link text is not.

    Targets are prefixed with the page stem because a label is global in
    Sphinx: three pages with an `Installation` section would collide.
    """
    out, mapping, fence = [], {}, False
    for line in text.split("\n"):
        if line.lstrip().startswith("```"):
            fence = not fence
        m = None if fence else re.match(r"^(#{1,6})\s+(.*?)\s*$", line)
        if m:
            slug = slugify(m.group(2))
            if slug and slug not in mapping:
                label = f"{stem}--{slug}"
                mapping[slug] = label
                out += ["```{=rst}", f".. _{label}:", "```", ""]
        out.append(line)
    return "\n".join(out), mapping


def strip_front_matter(text: str) -> str:
    if text.startswith("---\n"):
        end = text.find("\n---\n", 3)
        if end != -1:
            return text[end + 5 :]
    return text


def rewrite_admonitions(text: str) -> str:
    """`!!! type "Title"` + indented body -> a fenced div pandoc understands."""
    out, lines, i = [], text.split("\n"), 0
    pattern = re.compile(r'^(\s*)!!!\s+(\w+)(?:\s+"([^"]*)")?\s*$')
    while i < len(lines):
        m = pattern.match(lines[i])
        if not m:
            out.append(lines[i])
            i += 1
            continue
        indent, kind, title = m.group(1), m.group(2).lower(), m.group(3)
        # MkDocs types that RST spells differently or does not have.
        kind = {"info": "note", "tip": "tip", "success": "note", "question": "note",
                "example": "note", "quote": "note", "abstract": "note",
                "failure": "warning", "bug": "warning", "caution": "caution"}.get(kind, kind)
        if kind not in {"note", "warning", "tip", "important", "caution", "danger",
                        "attention", "hint", "error", "seealso", "admonition"}:
            kind = "note"
        i += 1
        body: list[str] = []
        while i < len(lines) and (lines[i].strip() == "" or lines[i].startswith(indent + "    ")):
            body.append(lines[i][len(indent) + 4 :] if lines[i].strip() else "")
            i += 1
        while body and body[-1] == "":
            body.pop()
        out.append(f"{indent}::: {{.{kind}}}")
        if title:
            out.append(f"{indent}**{title}**")
            out.append("")
        out.extend(indent + b if b else "" for b in body)
        out.append(f"{indent}:::")
        out.append("")
    return "\n".join(out)


def rewrite_docs_test_markers(text: str) -> str:
    """These gate a live test harness; pandoc would drop the HTML comment."""
    return re.sub(
        r"<!--\s*docs-test:\s*(.*?)\s*-->",
        lambda m: f"\n{SENTINEL}{m.group(1)}{SENTINEL}\n",
        text,
    )


def md_table_to_list_table(block: list[str]) -> list[str]:
    rows = []
    for line in block:
        s = line.strip()
        if not s.startswith("|"):
            continue
        cells = [c.strip() for c in s.strip("|").split("|")]
        if all(re.fullmatch(r":?-{2,}:?", c) for c in cells if c):
            continue
        rows.append(cells)
    if not rows:
        return block
    width = max(len(r) for r in rows)
    rows = [r + [""] * (width - len(r)) for r in rows]
    base, extra = divmod(100, width)
    widths = [base] * width
    widths[0] += extra
    out = ["```{=rst}", ".. list-table::", "   :header-rows: 1",
           f"   :widths: {' '.join(str(w) for w in widths)}", ""]
    for row in rows:
        for j, cell in enumerate(row):
            # Inline markdown that survives verbatim into RST.
            cell = re.sub(r"`([^`]+)`", r"``\1``", cell)
            cell = re.sub(r"\[([^\]]+)\]\((https?://[^)]+)\)", r"`\1 <\2>`__", cell)
            cell = cell.replace("<br>", " ").replace("<br/>", " ")
            out.append(("   * - " if j == 0 else "     - ") + cell)
    out += ["", "```", ""]
    return out


def rewrite_tables(text: str) -> str:
    lines, out, i = text.split("\n"), [], 0
    fence = False
    while i < len(lines):
        if lines[i].lstrip().startswith("```"):
            fence = not fence
            out.append(lines[i]); i += 1; continue
        if (not fence and lines[i].lstrip().startswith("|")
                and i + 1 < len(lines) and re.match(r"^\s*\|[\s:|-]+\|\s*$", lines[i + 1])):
            block = []
            while i < len(lines) and lines[i].lstrip().startswith("|"):
                block.append(lines[i]); i += 1
            out.extend(md_table_to_list_table(block)); continue
        out.append(lines[i]); i += 1
    return "\n".join(out)


def to_doc_role(target: str, label: str) -> str | None:
    t = target.split("#")[0].removesuffix(".md").removeprefix("./")
    t = re.sub(r"^(\.\./)+", "", t)
    stem = PATH_TO_STEM.get(t, t if t in STEMS else None)
    if stem == "index":
        return f":doc:`{label} </index>`" if label else ":doc:`/index`"
    if stem is None:
        return None
    return f":doc:`{label} <{stem}>`" if label else f":doc:`{stem}`"


def postprocess(rst: str, anchors: dict[str, str] | None = None) -> str:
    # pandoc writes `.. code::`; `.. code-block::` is the Sphinx spelling used
    # across the sibling sites.
    rst = re.sub(r"^(\s*)\.\. code:: (\S+)", r"\1.. code-block:: \2", rst, flags=re.M)
    rst = re.sub(r"^(\s*)\.\. code::\s*$", r"\1.. code-block:: text", rst, flags=re.M)

    # docs-test sentinels -> RST comments the harness can read.
    rst = re.sub(rf"{SENTINEL}(.*?){SENTINEL}", lambda m: f".. docs-test: {m.group(1)}", rst,
                 flags=re.S)

    # RST forbids an inline literal inside a link label; pandoc emits
    # ```code`` <url>`__ for [`code`](url), which does not parse. Must run
    # BEFORE the rule below, which would otherwise match from the label's
    # trailing backtick and leave the leading pair behind.
    rst = re.sub(r"`+``([^`]+)``\s*<([^>\n]+)>`__",
                 lambda m: f"`{m.group(1)} <{m.group(2)}>`__", rst)

    # Internal links -> :doc: roles. pandoc emits `label <target>`__ or `<target>`__.
    anchors = anchors or {}

    def link(m):
        label, target = m.group(1), m.group(2)
        if target.startswith(("http://", "https://", "mailto:")):
            return m.group(0)
        if target.startswith("#"):
            ref = anchors.get(target[1:])
            return f":ref:`{label} <{ref}>`" if ref else label
        role = to_doc_role(target, label.strip())
        if role:
            return role
        return f"`{label} <{GITHUB}>`__"

    # Both character classes exclude a newline. `[^>]+` is greedy ACROSS lines,
    # so an inline literal like ``abs(a - b) <= threshold`` matched as a link
    # whose target ran on until the next `>` anywhere later in the document,
    # rewriting a paragraph of prose into a URL.
    rst = re.sub(r"`([^`<\n]*?)\s*<([^>\n]+)>`__?", link, rst)

    # A bare `.md` reference pandoc left as plain text.
    rst = re.sub(r"(?<![\w/`])([\w-]+/)*[\w-]+\.md(?![\w`])",
                 lambda m: f"``{m.group(0)}``", rst)

    # House style uses a short rule, not pandoc's long one -- but ONLY for a
    # transition. A dash run that directly follows a non-blank line is a
    # SECTION UNDERLINE, and pandoc sizes it to the title; shortening those
    # produced 134 "Title underline too short" errors in one pass.
    def _rule(match):
        return "----"

    rst = re.sub(r"(?<=\n\n)-{4,}(?=\n)", _rule, rst)

    # An explicit target must be separated from what follows it. pandoc strips
    # the trailing blank line from a raw block, so the injected `.. _label:`
    # ran straight into its heading.
    rst = re.sub(r"(?m)^(\.\. _[^\n:]+:)\n(?=\S)", r"\1\n\n", rst)

    # A dedent from an indented block back to column 0 always needs a blank
    # line in RST. pandoc strips the trailing blank from a raw block, so every
    # injected `list-table` ran straight into the paragraph after it. Enforcing
    # the rule generally is safe -- there is no construct where a column-0 line
    # may legally abut an indented one.
    fixed, prev = [], ""
    for line in rst.split("\n"):
        if (prev.strip() and prev[:1].isspace() and line.strip()
                and not line[:1].isspace()):
            fixed.append("")
        fixed.append(line)
        prev = line
    rst = "\n".join(fixed)

    rst = re.sub(r"(?m)^----\n\n(?=----\n)", "", rst)
    rst = re.sub(r"\n{4,}", "\n\n\n", rst)
    return html.unescape(rst).rstrip() + "\n"


def convert(src: Path, stem: str) -> str:
    text = src.read_text(encoding="utf-8")
    text = strip_front_matter(text)
    text = rewrite_docs_test_markers(text)
    text = rewrite_admonitions(text)
    text = rewrite_tables(text)
    text, anchors = add_heading_targets(text, stem)
    proc = subprocess.run(
        ["pandoc", "-f", "markdown-smart+fenced_divs+raw_attribute", "-t", "rst",
         "--wrap=preserve"],
        input=text, capture_output=True, text=True, check=True,
    )
    return postprocess(proc.stdout, anchors)


if __name__ == "__main__":
    sys.stdout.write(convert(Path(sys.argv[1]), sys.argv[2]))
