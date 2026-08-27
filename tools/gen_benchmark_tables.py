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
"""Render the published benchmark tables from the committed measurements.

The performance table used to live, hand-transcribed, in five places at once:
`README.md`, `docs/index.md`, `MIGRATION.md`, `PERFORMANCE_COMPARISON.md` and
the v0.1.0 release notes. Nothing checked them against each other or against
the harness output, so they drifted -- one published row ended up pairing a
GENCODE v49 ingest time with GENCODE v45 spatial and batched numbers, and the
results file named as the provenance for all five corpora contained one.

So: one measurement file in, every rendered table out, and a `--check` mode
wired into `tests/test_release_hygiene.py` that fails the moment a table stops
matching the numbers behind it.

    python tools/gen_benchmark_tables.py --write    # regenerate
    python tools/gen_benchmark_tables.py --check    # verify (CI, tests)

Markdown rendered by GitHub and PyPI cannot use mkdocs' snippet include, so
the generated block is INJECTED between HTML-comment markers in each file:

    <!-- BEGIN GENERATED: corpus-table -->
    ...generated...
    <!-- END GENERATED: corpus-table -->
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from benchmarks.common import benchmark_row_evidence_error  # noqa: E402

RESULTS = ROOT / "benchmarks" / "results"
MEGA = RESULTS / "06_mega.json"

BEGIN = "<!-- BEGIN GENERATED: {name} -->"
END = "<!-- END GENERATED: {name} -->"

#: Display order and label for each corpus, independent of run order (the
#: harness runs cheapest-first; the table reads best largest-first).
DISPLAY_ORDER = [
    ("gencode-gtf", "**GENCODE v49** (basic)", "GTF"),
    ("gencode-gff3", "**GENCODE v49** (basic)", "GFF3"),
    ("refseq", "**RefSeq GRCh38.p14**", "GFF3"),
    ("chess", "**CHESS 3.1.3**", "GFF3"),
    ("mane", "**MANE v1.5** (Ensembl)", "GFF3"),
]


class Stale(RuntimeError):
    """A generated block does not match the measurements."""


# ---------------------------------------------------------------------------
# Formatting
# ---------------------------------------------------------------------------


def human_seconds(s: float | None) -> str:
    if s is None:
        return "—"
    if s < 60:
        return f"{s:.1f} s"
    m, rem = divmod(s, 60)
    if m < 60:
        return f"{int(m)} min {rem:.0f} s"
    h, m = divmod(int(m), 60)
    return f"{h} hr {m} min"


def human_bytes(n: float | None) -> str:
    if not n:
        return "—"
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024:
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.2f} {unit}"
        n /= 1024
    return f"{n:.2f} TB"


def _legacy_cell(row: dict, schema_version: str) -> str:
    legacy = row.get("legacy") or {}
    if legacy.get("wall_seconds"):
        return human_seconds(legacy["wall_seconds"])
    if schema_version == "3" and legacy.get("state") == "timed_out":
        return f"censored at {human_seconds(legacy.get('cap_seconds'))}"
    if schema_version == "2" and legacy.get("timed_out"):
        # Historical schema-v2 stored its cap under a misleading lower-bound
        # name.  Preserve the raw artifact, but render it only as censoring
        # evidence—not as a completed wall or a performance floor.
        return f"censored at {human_seconds(legacy.get('wall_seconds_lower_bound'))}"
    if legacy.get("state") == "skipped":
        return "not run"
    return "failed"


def _speedup_cell(row: dict) -> str:
    if row.get("ingest_speedup"):
        return f"**{row['ingest_speedup']:.2f}×**"
    return "—"


def _spread_note(row: dict) -> str:
    """A parenthetical spread, when the run actually measured one.

    `--repeats N` records `median`/`min`/`max`; a single-shot run records a
    bare `value` and no median, so this returns nothing rather than dressing
    one sample up as a central tendency.
    """
    timing = (row.get("spatial") or {}).get("timing") or {}
    if timing.get("n", 1) < 2 or "min" not in timing:
        return ""
    lo, hi, n = timing["min"], timing["max"], timing["n"]
    if not lo:
        return ""
    return f" ±{100 * (hi - lo) / (2 * lo):.0f}% (n={n})"


def _check_no_invented_numbers(row: dict, schema_version: str) -> None:
    """A capped run is censored and must never become a ratio or wall.

    Schema v2 is accepted only as the immutable historical artifact. Its old
    lower-bound fields are ignored by the renderer. New schema-v3 rows must
    use the strict state/cap contract and are rejected if stale floor fields
    reappear.
    """
    legacy = row.get("legacy") or {}
    if schema_version == "2":
        if legacy.get("timed_out") and legacy.get("wall_seconds") is not None:
            raise Stale(f"{row.get('key')}: historical timeout carries a synthesized wall")
        return
    if schema_version != "3":
        raise Stale(f"unsupported benchmark schema {schema_version!r}")
    forbidden = {"timed_out", "wall_seconds_lower_bound"}.intersection(legacy)
    if "ingest_speedup_lower_bound" in row:
        forbidden.add("ingest_speedup_lower_bound")
    if forbidden:
        raise Stale(f"{row.get('key')}: schema v3 carries stale fields {sorted(forbidden)}")
    if legacy.get("state") == "timed_out" and (
        legacy.get("wall_seconds") is not None
        or not isinstance(legacy.get("cap_seconds"), (int, float))
        or legacy.get("cap_seconds") <= 0
        or row.get("ingest_speedup") is not None
    ):
        raise Stale(
            f"{row.get('key')}: timed-out comparator violates the censored state/cap contract"
        )


# ---------------------------------------------------------------------------
# Renderers
# ---------------------------------------------------------------------------


def render_corpus_table(data: dict) -> str:
    corpora = data.get("corpora") or {}
    schema_version = str(data.get("schema_version"))
    lines = [
        "| Corpus | Format | Lines | gffbase ingest | legacy ingest | speedup | peak RSS | spatial qps | batched (5 k anchors) |",
        "| --- | :--: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |",
    ]
    for key, label, fmt in DISPLAY_ORDER:
        row = corpora.get(key)
        if not row or "error" in row:
            continue
        _check_no_invented_numbers(row, schema_version)
        if schema_version == "3":
            evidence_error = benchmark_row_evidence_error(row)
            if evidence_error:
                raise Stale(f"{key}: invalid speedup evidence: {evidence_error}")
        g = row.get("gffbase") or {}
        spatial = row.get("spatial") or {}
        batched = row.get("batched") or {}
        qps = spatial.get("qps")
        b_wall = batched.get("wall_seconds")
        b_desc = batched.get("n_descendants")
        batched_cell = (
            f"{b_wall * 1000:.0f} ms / {b_desc / 1e6:.2f} M desc"
            if b_wall and b_desc and b_desc >= 1e6
            else (
                f"{b_wall * 1000:.0f} ms / {b_desc / 1e3:.0f} k desc" if b_wall and b_desc else "—"
            )
        )
        lines.append(
            f"| {label} | {fmt} | {row.get('feature_lines', 0):,} "
            f"| **{human_seconds(g.get('wall_seconds'))}** "
            f"| {_legacy_cell(row, schema_version)} "
            f"| {_speedup_cell(row)} "
            f"| {human_bytes(g.get('peak_rss_bytes'))} "
            f"| {f'**{qps:,.0f}**' + _spread_note(row) if qps else '—'} "
            f"| {batched_cell} |"
        )
    return "\n".join(lines)


def render_provenance(data: dict) -> str:
    env = data.get("environment") or {}
    pkgs = env.get("packages") or {}
    ram = env.get("total_ram_bytes")
    cores = env.get("cpu_cores_physical")
    parts = [
        f"**Measured on** {env.get('cpu_model') or 'unknown CPU'}",
        f"{cores} cores" if cores else None,
        human_bytes(ram) + " RAM" if ram else None,
        env.get("platform"),
    ]
    line1 = " · ".join(p for p in parts if p)
    versions = " · ".join(
        f"{name} {pkgs[name]}"
        for name in ("gffbase", "duckdb", "pyarrow", "gffutils")
        if pkgs.get(name)
    )
    commit = (env.get("git_commit") or "")[:12]
    stamp = env.get("timestamp_utc", "")
    dirty = " (working tree dirty)" if env.get("git_dirty") else ""
    return (
        f"{line1}  \n"
        f"**Versions:** Python {env.get('python_version', '?')} · {versions}  \n"
        f"**Commit:** `{commit}`{dirty} · **Run:** {stamp}  \n"
        f"*Generated from `benchmarks/results/06_mega.json` by "
        f"`tools/gen_benchmark_tables.py`. Do not edit by hand.*"
    )


def render_tradeoffs(data: dict) -> str:
    """The cost side of the ledger, from the same measurements as the speed side.

    This table used to be hand-written, and had drifted: it claimed the
    database was "~1.5x larger" while the numbers in this very file put MANE at
    1.29x. The harness records `peak_rss_bytes` and `disk_bytes` for BOTH
    engines, so there is no reason for a human to be retyping either.

    Rows the harness cannot measure -- "is a point query comparable?" -- stay
    qualitative, and stay out of here.
    """
    corpora = data.get("corpora") or {}
    rows = [r for r in corpora.values() if r and "error" not in r]

    def ratio(section: str) -> str | None:
        pairs = [
            (r["gffbase"].get(section), r["legacy"].get(section))
            for r in rows
            if (r.get("gffbase") or {}).get(section) and (r.get("legacy") or {}).get(section)
        ]
        if not pairs:
            return None
        vals = sorted(g / legacy for g, legacy in pairs)
        lo, hi = vals[0], vals[-1]
        return f"{lo:.2f}×" if len(vals) == 1 or hi - lo < 0.05 else f"{lo:.2f}–{hi:.2f}×"

    def span(section: str, engine: str) -> str:
        vals = sorted(r[engine][section] for r in rows if (r.get(engine) or {}).get(section))
        if not vals:
            return "—"
        lo, hi = human_bytes(vals[0]), human_bytes(vals[-1])
        return lo if lo == hi else f"{lo} – {hi}"

    rss_ratio = ratio("peak_rss_bytes")
    disk_ratio = ratio("disk_bytes")
    n = len(rows)
    corpus_word = "corpus" if n == 1 else "corpora"

    return "\n".join(
        [
            "| | gffbase | legacy `gffutils` | ratio |",
            "| --- | ---: | ---: | ---: |",
            f"| **Peak ingest RSS** | {span('peak_rss_bytes', 'gffbase')} "
            f"| {span('peak_rss_bytes', 'legacy')} | {rss_ratio or '—'} |",
            f"| **On-disk database** | {span('disk_bytes', 'gffbase')} "
            f"| {span('disk_bytes', 'legacy')} | {disk_ratio or '—'} |",
            "",
            f"*Measured across {n} {corpus_word}; ratios are gffbase ÷ legacy.*",
        ]
    )


RENDERERS = {
    "corpus-table": render_corpus_table,
    "benchmark-provenance": render_provenance,
    "tradeoffs-table": render_tradeoffs,
}

#: Which generated blocks each file may contain. A file is only rewritten if
#: it actually carries the markers, so adding a table to a new page is a
#: matter of pasting the marker pair into it.
TARGETS = ["README.md", "MIGRATION.md", "docs/index.md", "docs/performance.md"]


# ---------------------------------------------------------------------------
# Injection
# ---------------------------------------------------------------------------


def inject(text: str, name: str, body: str) -> tuple[str, bool]:
    begin, end = BEGIN.format(name=name), END.format(name=name)
    if begin not in text or end not in text:
        return text, False
    head, rest = text.split(begin, 1)
    _, tail = rest.split(end, 1)
    return f"{head}{begin}\n{body}\n{end}{tail}", True


def process(data: dict, *, write: bool) -> list[str]:
    problems: list[str] = []
    rendered = {name: fn(data) for name, fn in RENDERERS.items()}
    for rel in TARGETS:
        path = ROOT / rel
        if not path.is_file():
            continue
        original = path.read_text(encoding="utf-8")
        updated = original
        touched = False
        for name, body in rendered.items():
            updated, found = inject(updated, name, body)
            touched = touched or found
        if not touched or updated == original:
            continue
        if write:
            path.write_text(updated, encoding="utf-8")
            print(f"  updated {rel}")
        else:
            problems.append(rel)
    return problems


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    group = ap.add_mutually_exclusive_group(required=True)
    group.add_argument("--write", action="store_true", help="regenerate the tables in place")
    group.add_argument("--check", action="store_true", help="fail if any table is out of date")
    args = ap.parse_args()

    if not MEGA.is_file():
        print(f"no measurements at {MEGA.relative_to(ROOT)}", file=sys.stderr)
        print(
            "Run: python benchmarks/06_mega.py, then copy out/06_mega.json there.", file=sys.stderr
        )
        return 1
    data = json.loads(MEGA.read_text())

    try:
        problems = process(data, write=args.write)
    except Stale as exc:
        print(f"refusing to render: {exc}", file=sys.stderr)
        return 1

    if args.check and problems:
        print("benchmark tables are out of date in:", file=sys.stderr)
        for rel in problems:
            print(f"  {rel}", file=sys.stderr)
        print("\nRun: python tools/gen_benchmark_tables.py --write", file=sys.stderr)
        return 1
    print("benchmark tables are current" if args.check else "done")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
